#!/usr/bin/env python3
"""重跑前置门（首轮 gate）——批跑前 2–3 run 校验，不达标立刻停批。

背景（2026-09-21，0921-3/§18.4）：EXP-9 死于 64K 多工具 agentic 形态下的
驱逐/延迟螺旋；该形态在**生产配置**下从未复验。影子与合成 pilot 已证与生产
不可比（同 16GiB：影子 0 驱逐 vs 生产 103 驱逐 + Metal pressure 反升）。
故重跑前须以**真实形态**跑 2–3 run，校验四件套，不达标停批
（成本：数十分钟 vs 重赔 38–49h 批次）。

四件套（判据一律以 /metrics **计数器增量**为准，非日志行——日志受窗口截断低估）：
  1. pressure_evictions 增量（rapid_mlx_prefix_cache_pressure_evictions_total）
  2. lru_evictions 增量（rapid_mlx_prefix_cache_evictions_total）
  3. 每轮墙钟（run 记录；劣化 > 阈值即警报）
  4. epoch_collapse 数（代理台账 manifest；折叠异常即警报）

用法（由 swe-eval 批跑侧编排调用）：
  # 批前快照
  python3 tools/rerun_gate.py snapshot --out /tmp/gate-before.json
  # ... 跑 2–3 个真实 run ...
  # 判读（与批前快照对比）
  python3 tools/rerun_gate.py check --before /tmp/gate-before.json \\
      --wall-threshold-ratio 1.5 --out /tmp/gate-verdict.json
  # exit 0 = 放行；exit 1 = STOP（停批）

依赖：Python 3 stdlib only。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_METRICS = "http://127.0.0.1:8081/metrics"
PROXY_STATUS = "http://127.0.0.1:4000/api/status"
PROXY_SESSIONS = "http://127.0.0.1:4000/api/sessions"
MANIFEST_DIR = os.path.join(REPO, "logs", "diag", "manifest")

# /metrics 计数器名（权威源）
COUNTERS = {
    "pressure_evictions": "rapid_mlx_prefix_cache_pressure_evictions_total",
    "lru_evictions": "rapid_mlx_prefix_cache_evictions_total",
    "snapdown_hits": "rapid_mlx_prefix_cache_snapdown_hits_total",
    "prefix_cache_hits": "rapid_mlx_prefix_cache_hits_total",
}
GAUGES = {
    "metal_active_bytes": "rapid_mlx_metal_active_memory_bytes",
    "metal_peak_bytes": "rapid_mlx_metal_peak_memory_bytes",
    "prefix_cache_current_bytes": "rapid_mlx_prefix_cache_current_bytes",
    "prefix_cache_cap_bytes": "rapid_mlx_prefix_cache_cap_bytes",
}


def _fetch(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _metric_value(txt, name):
    m = re.search(rf"^\s*{re.escape(name)}\s+([\d.eE+-]+)", txt, re.M)
    return float(m.group(1)) if m else None


def _backend_metrics():
    txt = _fetch(BACKEND_METRICS)
    out = {}
    for label, name in COUNTERS.items():
        v = _metric_value(txt, name)
        if v is not None:
            out[label] = int(v)
    for label, name in GAUGES.items():
        v = _metric_value(txt, name)
        if v is not None:
            out[label] = int(v)
    return out


def _proxy_ctx():
    try:
        d = json.loads(_fetch(PROXY_STATUS))
        return d.get("ctx_config", {}) or {}
    except Exception as e:
        return {"_error": str(e)}


def _epoch_collapse_by_session():
    """从 manifest 文件统计各会话 epoch_collapse 行数。"""
    out = {}
    if not os.path.isdir(MANIFEST_DIR):
        return out
    for fn in os.listdir(MANIFEST_DIR):
        if not fn.endswith(".jsonl"):
            continue
        sid = fn[:-6]
        n = 0
        try:
            with open(os.path.join(MANIFEST_DIR, fn), "r") as f:
                for line in f:
                    if '"epoch_collapse"' in line:
                        n += 1
        except OSError:
            continue
        if n:
            out[sid] = n
    return out


def _wall_times_from_sessions(since_ts=None, limit=200):
    """从 sessions.jsonl 抽取最近 turn 墙钟（duration_ms）。

    关键：必须按 since_ts 过滤，否则读到全量历史（含 EXP-9 灾难期 15min/run
    长尾），before/after 样本池几乎相同 → wall_ratio≈1.0 永远 PASS，
    检测不出真实劣化（0921-4 自查修正）。
    """
    p = os.path.join(REPO, "logs", "diag", "sessions.jsonl")
    if not os.path.exists(p):
        return []
    rows = []
    try:
        with open(p, "r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = d.get("ts") or ""
                if since_ts and ts and ts < since_ts:
                    continue
                ms = d.get("duration_ms") or d.get("wall_ms")
                if ms:
                    rows.append(float(ms))
    except OSError:
        return []
    return rows[-limit:]


def snapshot(out_path):
    snap = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "epoch_utc": time.time(),
        "metrics": _backend_metrics(),
        "ctx_config": _proxy_ctx(),
        "epoch_collapse_by_session": _epoch_collapse_by_session(),
        "wall_samples_ms": _wall_times_from_sessions(),
        "wall_median_ms": (sorted(_wall_times_from_sessions())[len(_wall_times_from_sessions())//2]
                           if _wall_times_from_sessions() else None),
    }
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    return snap


def check(before_path, wall_threshold_ratio, max_pressure_evict, max_lru_evict,
          max_epoch_collapse, out_path):
    with open(before_path, "r") as f:
        before = json.load(f)
    after = snapshot(None)

    bm, am = before.get("metrics", {}), after.get("metrics", {})
    delta = {}
    for k in ("pressure_evictions", "lru_evictions", "snapdown_hits", "prefix_cache_hits"):
        if k in bm and k in am:
            delta[k] = am[k] - bm[k]

    # epoch_collapse 增量
    bc = before.get("epoch_collapse_by_session", {})
    ac = after.get("epoch_collapse_by_session", {})
    epoch_delta = {}
    for sid, n in ac.items():
        d = n - bc.get(sid, 0)
        if d:
            epoch_delta[sid] = d
    epoch_total = sum(epoch_delta.values())

    # 墙钟：只取 before 快照之后的样本（避免历史长尾污染，0921-4 修正）
    before_ts = before.get("ts")
    wa_all = _wall_times_from_sessions(since_ts=before_ts)
    wall_ratio = None
    wall_stat = None
    if wa_all:
        # 中位数抗长尾（历史有 15min/run 灾难期样本）
        ws = sorted(wa_all)
        med = ws[len(ws) // 2]
        # 基线参考：EXP-9 后期普通轮 48–100s（0921-3 旁证），但以本批实测中位为准
        wall_stat = {"n": len(wa_all), "median_ms": med,
                     "p90_ms": ws[int(len(ws) * 0.9)], "max_ms": ws[-1]}
        # 若 before 快照带基线中位，则计算比
        bmed = before.get("wall_median_ms")
        if bmed:
            wall_ratio = med / bmed if bmed > 0 else None

    # ctx_config 漂移（S/window_k）
    bc_cfg, ac_cfg = before.get("ctx_config", {}), after.get("ctx_config", {})
    cfg_drift = {
        k: [bc_cfg.get(k), ac_cfg.get(k)]
        for k in ("epoch_S", "window_K", "engine_enabled")
        if bc_cfg.get(k) != ac_cfg.get(k)
    }

    reasons = []
    if delta.get("pressure_evictions", 0) > max_pressure_evict:
        reasons.append(f"pressure_evictions +{delta['pressure_evictions']} > {max_pressure_evict}")
    if delta.get("lru_evictions", 0) > max_lru_evict:
        reasons.append(f"lru_evictions +{delta['lru_evictions']} > {max_lru_evict}")
    if wall_ratio is not None and wall_ratio > wall_threshold_ratio:
        reasons.append(f"wall_ratio {wall_ratio:.2f} > {wall_threshold_ratio}")
    if epoch_total > max_epoch_collapse:
        reasons.append(f"epoch_collapse +{epoch_total} > {max_epoch_collapse}")
    if cfg_drift:
        reasons.append(f"ctx_config drift {cfg_drift}")

    verdict = {
        "ts": after["ts"],
        "verdict": "PASS" if not reasons else "STOP",
        "reasons": reasons,
        "delta": delta,
        "epoch_collapse_delta": epoch_delta,
        "wall_ratio": wall_ratio,
        "wall_stat": wall_stat,
        "ctx_config_drift": cfg_drift,
        "metrics_after": am,
        "thresholds": {
            "wall_threshold_ratio": wall_threshold_ratio,
            "max_pressure_evict": max_pressure_evict,
            "max_lru_evict": max_lru_evict,
            "max_epoch_collapse": max_epoch_collapse,
        },
    }
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(verdict, f, ensure_ascii=False, indent=2)

    print(f"[gate] 判定: {verdict['verdict']}")
    print(f"        计数器增量: {delta}")
    print(f"        epoch_collapse 增量: {epoch_total}")
    print(f"        墙钟比: {wall_ratio}")
    if reasons:
        for r in reasons:
            print(f"        ✗ {r}")
    return 0 if not reasons else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("snapshot", help="批前/批后采集快照")
    s1.add_argument("--out", required=True)

    s2 = sub.add_parser("check", help="对比快照并判读")
    s2.add_argument("--before", required=True)
    s2.add_argument("--wall-threshold-ratio", type=float, default=1.5)
    s2.add_argument("--max-pressure-evict", type=int, default=0)
    s2.add_argument("--max-lru-evict", type=int, default=200)
    s2.add_argument("--max-epoch-collapse", type=int, default=0)
    s2.add_argument("--out", default=None)

    args = ap.parse_args()
    if args.cmd == "snapshot":
        snap = snapshot(args.out)
        print(f"[gate] 快照已落 {args.out}")
        print(f"        metrics: {snap['metrics']}")
        print(f"        ctx_config epoch_S={snap['ctx_config'].get('epoch_S')} "
              f"window_K={snap['ctx_config'].get('window_K')}")
        return 0
    return check(args.before, args.wall_threshold_ratio, args.max_pressure_evict,
                 args.max_lru_evict, args.max_epoch_collapse, args.out)


if __name__ == "__main__":
    sys.exit(main())
