#!/usr/bin/env python3
"""重跑前置门（首轮 gate）——批跑前 2–3 run 校验，不达标立刻停批。

背景（2026-09-21，0921-3/§18.4）：EXP-9 死于 64K 多工具 agentic 形态下的
驱逐/延迟螺旋；该形态在**生产配置**下从未复验。影子与合成 pilot 已证与生产
不可比（同 16GiB：影子 0 驱逐 vs 生产 103 驱逐 + Metal pressure 反升）。
故重跑前须以**真实形态**跑 2–3 run，校验四件套，不达标停批
（成本：数十分钟 vs 重赔 38–49h 批次）。

四件套判据（2026-09-22 定稿，联合文档 0921-21/0921-22；一律以 /metrics
**计数器增量**为准，非日志行——日志受窗口截断低估）：
  1. **Metal 有界 + cache-self 有限**（0922-3 裁决口径；不再要求 Metal=0）：
     - **Metal 有界**（日志归因 `[prefix-pressure-evict] ... under Metal pressure`）：
       ① `metal_peak < 34.0GB`（cap 36.1 − 2.1GB 天花板，绝对量纲，与事件率解耦）
       ② 滚动 30min 内 Metal 事件 < 8（thrash；12GB 段为数十次/30min，判别充分）
       少量瞬时 → WARN 不停批；`metal_peak ≥ 33.0GB` 早警。
       **不用「Metal=0」**：窗 A 2-run 短样本碰巧成立，长轨迹样本（63.7K）注定 FAIL；
       也**不用「≤ 小阈值/run」**（小样本 per-run 归一噪声大 + 强依赖轨迹长度）。
     - **cache-self 有限**：`pressure_evictions`（Metal+cache-self 共用计数）总量
       **≤864**（2× 窗 A 校准 432）。两触发同机制、危害同源；Metal 是进程级红线，
       cache-self 是容器泄压阀。
  2. prefix 命中率 ≥90%（请求级口径）
  3. 每轮墙钟不劣化 >10%（`sessions.jsonl` duration_ms；中位数抗长尾）
  4. `lru_evictions`（rapid_mlx_prefix_cache_evictions_total）增量
     ≤2× 窗 A 校准（432），即 ≤864（容量驱逐护栏，与命中率联动）

族（profile）适配（2026-10-05，S-16 补门）：
  引擎按 /metrics 暴露的族自动判别并记入快照 `profile` 字段——
  - `rapid_mlx`：上方原四件套（Metal 有界/压力驱逐/cache 驱逐/折叠/墙钟）。
  - `splash`：Splash 后端**无 `rapid_mlx_*` 计数器**，改用等价硬判据（v1）：
    · `splash_memory_pressure{state="critical"}` 为 0（内存顶格在场）
    · `splash_requests_failed_total` / `splash_capacity_failures_total` 增量为 0
    · `splash_kv_pages_free` > 0（KV 页耗尽）
    · ctx_config 漂移为空（S/K/engine）
    · 墙钟比（R16 sessions.jsonl，中位）与折叠增量为两族**共用**判据
    软项（待校准，进 `verdict.splash.notes`，不阻断）：decode tps 比、ITL p95、
    cache_cold_misses 增量、cancelled 增量、admission_waiting_memory。
    吞吐硬判据待校准后经 `--min-decode-tps-ratio` 启用（默认不 armed）。

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
BACKEND_LOG = os.path.join(REPO, "logs", "llama-server.log")

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
# Splash 族（2026-10-05，S-16 补门）：无 rapid_mlx_* 计数器，见 docstring 族适配
SPLASH_COUNTERS = {
    "requests_submitted": "splash_requests_submitted_total",
    "requests_completed": "splash_requests_completed_total",
    "requests_cancelled": "splash_requests_cancelled_total",
    "requests_failed": "splash_requests_failed_total",
    "capacity_failures": "splash_capacity_failures_total",
    "cache_cold_misses": "splash_cache_cold_misses_total",
    "cache_hits": "splash_cache_hits_total",
    "cache_reused_tokens": "splash_cache_reused_tokens_total",
}
SPLASH_GAUGES = {
    "mem_pressure_normal": 'splash_memory_pressure{state="normal"}',
    "mem_pressure_warning": 'splash_memory_pressure{state="warning"}',
    "mem_pressure_critical": 'splash_memory_pressure{state="critical"}',
    "transport_pending": "splash_transport_pending",
    "admission_waiting_memory": "splash_admission_waiting_memory",
    "admission_suspended": "splash_admission_suspended",
    "kv_pages_free": "splash_kv_pages_free",
    "kv_pages_active": "splash_kv_pages_active",
    "decode_tps": "splash_decode_tokens_per_second",
    "itl_p50_ms": "splash_itl_p50_milliseconds",
    "itl_p95_ms": "splash_itl_p95_milliseconds",
}


def _fetch(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _metric_value(txt, name):
    m = re.search(rf"^\s*{re.escape(name)}\s+([\d.eE+-]+)", txt, re.M)
    return float(m.group(1)) if m else None


def _detect_profile(txt):
    """按 /metrics 暴露的族判别：rapid_mlx > splash > unknown（未知按旧族处理）。"""
    if "rapid_mlx_prefix_cache_" in txt:
        return "rapid_mlx"
    if "splash_" in txt:
        return "splash"
    return "unknown"


def _backend_metrics(text=None):
    txt = text if text is not None else _fetch(BACKEND_METRICS)
    out = {}
    for label, name in COUNTERS.items():
        v = _metric_value(txt, name)
        if v is not None:
            out[label] = int(v)
    for label, name in GAUGES.items():
        v = _metric_value(txt, name)
        if v is not None:
            out[label] = int(v)
    for label, name in SPLASH_COUNTERS.items():
        v = _metric_value(txt, name)
        if v is not None:
            out[label] = int(v)
    for label, name in SPLASH_GAUGES.items():
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
    """从 sessions.jsonl 统计各会话**折叠事件数**（0921-15 B① 口径校准）。

    原实现数 `logs/diag/manifest/<sid>.jsonl` 的 `epoch_collapse` **条目数**
    ——那是被收编的 message 行数（一个会话数百行），"条目数 >0" 对任何长会话
    恒真、**不具判别力**（swe-eval 0921-11/0921-13 指出）。
    改用 R16 `sessions.jsonl` 的 `epoch_count`（会话级**折叠事件**累计值）：
    取各会话**末次** epoch_count，即当前累计折叠次数。
    """
    p = os.path.join(REPO, "logs", "diag", "sessions.jsonl")
    if not os.path.exists(p):
        return {}
    latest = {}
    try:
        with open(p, "r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                sid = d.get("session_key") or d.get("session") or d.get("key")
                ec = d.get("epoch_count")
                if sid and isinstance(ec, int):
                    latest[sid] = ec          # 后写覆盖先写 → 末次值
    except OSError:
        return {}
    return {k: v for k, v in latest.items() if v}


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


METAL_EVICT_RE = re.compile(
    r"\[prefix-pressure-evict\] evicted (\d+) entries under (Metal\+cache-self|Metal|cache-self) pressure")


def _log_evict_attribution(since_bytes=0):
    """从后端日志分型压力驱逐：Metal / cache-self / 两者。

    判据 (1) 的 "Metal = 0" 无法从共用计数器区分，须靠日志归因
    （0921-21/0921-22）。日志行受窗口截断低估，**只用于分型佐证**，
    总量仍以 /metrics 计数器为准。
    返回 (metal_count, cache_self_count, both_count)。
    """
    if not os.path.exists(BACKEND_LOG):
        return 0, 0, 0
    try:
        size = os.path.getsize(BACKEND_LOG)
        with open(BACKEND_LOG, "rb") as f:
            if since_bytes > size:
                since_bytes = 0
            f.seek(since_bytes)
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return 0, 0, 0
    metal = cache_self = both = 0
    for m in METAL_EVICT_RE.finditer(chunk):
        n, kind = int(m.group(1)), m.group(2)
        if kind == "Metal":
            metal += n
        elif kind == "cache-self":
            cache_self += n
        else:
            both += n
    return metal, cache_self, both


def snapshot(out_path):
    metrics_text = _fetch(BACKEND_METRICS)
    snap = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "epoch_utc": time.time(),
        "profile": _detect_profile(metrics_text),
        "metrics": _backend_metrics(metrics_text),
        "ctx_config": _proxy_ctx(),
        "epoch_collapse_by_session": _epoch_collapse_by_session(),
        "wall_samples_ms": _wall_times_from_sessions(),
        "wall_median_ms": (sorted(_wall_times_from_sessions())[len(_wall_times_from_sessions())//2]
                           if _wall_times_from_sessions() else None),
        "log_evict_attribution": dict(zip(
            ("metal", "cache_self", "both"), _log_evict_attribution())),
        "log_size_bytes": (os.path.getsize(BACKEND_LOG)
                           if os.path.exists(BACKEND_LOG) else 0),
    }
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    return snap


def check(before_path, wall_threshold_ratio, max_pressure_evict, max_lru_evict,
          max_epoch_collapse, out_path, max_metal_evict=0,
          max_metal_peak_gb=34.0, max_metal_events_30min=8,
          min_decode_tps_ratio=None):
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

    profile = before.get("profile") or "rapid_mlx"   # 旧快照按旧族判
    reasons = []
    splash_block = None
    if profile == "splash":
        # Splash 族硬判据（v1；阈值口径见模块 docstring「族适配」）
        sdelta = {k: am.get(k, 0) - bm.get(k, 0) for k in SPLASH_COUNTERS
                  if k in bm and k in am}
        if am.get("mem_pressure_critical"):
            reasons.append("splash memory_pressure=critical 在场（内存顶格）")
        if sdelta.get("requests_failed", 0) > 0:
            reasons.append(f"requests_failed +{sdelta['requests_failed']} > 0")
        if sdelta.get("capacity_failures", 0) > 0:
            reasons.append(f"capacity_failures +{sdelta['capacity_failures']} > 0")
        kv_free = am.get("kv_pages_free")
        if kv_free is not None and kv_free <= 0:
            reasons.append("kv_pages_free <= 0（KV 页耗尽）")
        notes = [
            f"decode_tps before/after={bm.get('decode_tps')}/{am.get('decode_tps')}",
            f"itl_p95_ms={am.get('itl_p95_ms')}",
            f"cache_cold_misses delta={sdelta.get('cache_cold_misses')}",
            f"requests_cancelled delta={sdelta.get('requests_cancelled')}",
            f"admission_waiting_memory={am.get('admission_waiting_memory')}",
        ]
        if min_decode_tps_ratio is not None:
            b_tps, a_tps = bm.get("decode_tps"), am.get("decode_tps")
            if b_tps and a_tps is not None and a_tps / b_tps < min_decode_tps_ratio:
                reasons.append(
                    f"decode_tps ratio {a_tps / b_tps:.2f} < {min_decode_tps_ratio}")
        splash_block = {
            "delta": sdelta,
            "notes": notes,
            "criteria_pending_calibration": [
                "decode_tps_ratio（--min-decode-tps-ratio 校准后启用）",
                "itl_p95", "cache_cold_misses_delta",
                "cancelled_delta", "admission_waiting_memory",
            ],
        }
        metal_n = cache_n = both_n = 0
        metal_events = 0
        peak_gb = None
    else:
        if delta.get("pressure_evictions", 0) > max_pressure_evict:
            reasons.append(f"pressure_evictions +{delta['pressure_evictions']} > {max_pressure_evict}")
        # 判据 (1)："Metal 有界"（0922-3）——两段式：
        #   1a 天花板：metal_peak < max_metal_peak_gb（绝对量纲）
        #   1b thrash：窗内 Metal 日志事件 < max_metal_events_30min
        # 关键：日志归因只扫 before 快照之后的增量（否则读全量历史数月事件）。
        since_bytes = int(before.get("log_size_bytes") or 0)
        metal_n, cache_n, both_n = _log_evict_attribution(since_bytes=since_bytes)
        metal_events = metal_n + both_n
        peak_bytes = am.get("metal_peak_bytes")
        peak_gb = (peak_bytes / 1e9) if peak_bytes else None
        if peak_gb is not None and peak_gb >= max_metal_peak_gb:
            reasons.append(f"metal_peak {peak_gb:.1f}GB >= {max_metal_peak_gb}GB (1a ceiling)")
        if metal_events >= max_metal_events_30min:
            reasons.append(
                f"Metal events {metal_events} >= {max_metal_events_30min} (1b thrash)")
        if peak_gb is not None and peak_gb >= 33.0:
            reasons.append(f"WARN: metal_peak {peak_gb:.1f}GB >= 33.0GB (early warning)")
        if delta.get("lru_evictions", 0) > max_lru_evict:
            reasons.append(f"lru_evictions +{delta['lru_evictions']} > {max_lru_evict}")
    # 共用判据（两族）：墙钟比 / epoch 折叠 / ctx 配置漂移
    if wall_ratio is not None and wall_ratio > wall_threshold_ratio:
        reasons.append(f"wall_ratio {wall_ratio:.2f} > {wall_threshold_ratio}")
    if epoch_total > max_epoch_collapse:
        reasons.append(f"epoch_collapse +{epoch_total} > {max_epoch_collapse}")
    if cfg_drift:
        reasons.append(f"ctx_config drift {cfg_drift}")

    verdict = {
        "ts": after["ts"],
        "profile": profile,
        "verdict": "PASS" if not reasons else "STOP",
        "reasons": reasons,
        "delta": delta,
        "splash": splash_block,
        "log_evict_attribution": {"metal": metal_n, "cache_self": cache_n, "both": both_n},
        "metal_peak_gb": peak_gb,
        "epoch_collapse_delta": epoch_delta,
        "wall_ratio": wall_ratio,
        "wall_stat": wall_stat,
        "ctx_config_drift": cfg_drift,
        "metrics_after": am,
        "thresholds": {
            "wall_threshold_ratio": wall_threshold_ratio,
            "max_pressure_evict": max_pressure_evict,
            "max_metal_evict": max_metal_evict,
            "max_lru_evict": max_lru_evict,
            "max_epoch_collapse": max_epoch_collapse,
            "min_decode_tps_ratio": min_decode_tps_ratio,
        },
    }
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(verdict, f, ensure_ascii=False, indent=2)

    print(f"[gate] 判定: {verdict['verdict']} (profile={profile})")
    if profile == "splash":
        print(f"        splash 计数增量: {splash_block['delta']}")
        for note in splash_block["notes"]:
            print(f"        note: {note}")
        print(f"        待校准软项: {splash_block['criteria_pending_calibration']}")
    else:
        print(f"        计数器增量: {delta}")
        print(f"        日志归因: Metal事件={metal_events} cache-self={cache_n} metal_peak={peak_gb}GB")
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
    s2.add_argument("--max-pressure-evict", type=int, default=864,
                    help="压力驱逐总量上限（默认 864 = 2× 窗 A 校准 432/run）")
    s2.add_argument("--max-metal-events-30min", type=int, default=8, dest="max_metal_events_30min",
                    help="1b thrash：窗内 Metal 事件上限（默认 8，0922-3）")
    s2.add_argument("--max-metal-peak-gb", type=float, default=34.0,
                    help="1a 天花板：metal_peak 上限 GB（默认 34.0 = cap−2.1）")
    s2.add_argument("--max-lru-evict", type=int, default=864,
                    help="容量驱逐上限（默认 864 = 2× 窗 A 校准 432）")
    s2.add_argument("--max-epoch-collapse", type=int, default=0)
    s2.add_argument("--min-decode-tps-ratio", type=float, default=None,
                    dest="min_decode_tps_ratio",
                    help="Splash 族吞吐硬判据（after/before decode tps 比下限；"
                         "默认不 armed，校准后启用）")
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
                 args.max_lru_evict, args.max_epoch_collapse, args.out,
                 max_metal_evict=0,
                 max_metal_peak_gb=args.max_metal_peak_gb,
                 max_metal_events_30min=args.max_metal_events_30min,
                 min_decode_tps_ratio=args.min_decode_tps_ratio)


if __name__ == "__main__":
    sys.exit(main())
