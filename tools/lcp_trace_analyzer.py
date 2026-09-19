#!/usr/bin/env python3
"""lcp_trace_analyzer.py — LCP snap-down 遥测离线分析器（D2 欠项②）。

解析 rapid-mlx 后端日志的 LCP 遥测行（联合协调 J2 / 0920-12）——**双格式并存**
（本地补丁 0.12.12 口径 + 上游 0.14.3 口径；历史归档仍可解析）：

  本地补丁（0.12.12，`patches/vllm-mlx/`）:
    [lcp_boundaries] candidates=N boundaries=[...] total=T msgs=M
    [cache_fetch] LCP snapdown: B=P reused=P remaining=Y (entry_len=E lcp=L)
  上游内置（0.14.3，`rapid_mlx/hybrid_state_checkpoints.py`）:
    [cache_fetch] LCP snapped to checkpoint: shared=S entry_len=E resumed_at=P remaining=Y
    [cache_fetch] supersequence snapped to checkpoint: requested=N entry_len=E resumed_at=P
  失配（两版本同款两变体）:
    [cache_fetch] LCP unavailable: shared=S entry_len=E requested_len=R non_trimmable=True
    [cache_fetch] LCP unavailable: shared=S entry_len=E cache could not rewind exactly by X tokens

语义要点（经 0.14.3 wheel 源码核验，`tokens_saved += position`）：
  **复用量 = 检查点位置 P（resumed_at / B）**；shared 是 LCP 上限（P ≤ shared）。
  请求总长 = P + remaining，故 saved_pct = P/(P+remaining)；
  `checkpoint_gap = shared − P` = 检查点粒度损失 —— **调 RAPID_MLX_HYBRID_CHECKPOINT_MAX/
  STRIDE（或本地补丁 M/间距）的直接依据**：gap 大 = 窗口/密度不足。

输出: 命中率 / 复用 token 分布 / 检查点粒度损失 / 失配分型（trivial=小条目不值得
vs meaningful=真实失配）/ 边界计算画像。`--archive` 把匹配原始行切段落盘
logs/diag/lcp_trace/（防日志轮转蒸发——联合文档风险节"证据不过夜"）。

用法:
  python3 tools/lcp_trace_analyzer.py                     # 汇总当前日志
  python3 tools/lcp_trace_analyzer.py --archive           # 汇总+原始行归档
  python3 tools/lcp_trace_analyzer.py --tail-bytes 52428800 --json
纯离线读日志，零引擎交互（透明类·离线档，联合协调 J6.1 三档制）。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

DEFAULT_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "logs", "llama-server.log")
ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "logs", "diag", "lcp_trace")
# 小条目判据: 失配行中 entry_len/shared 均低于此值 → trivial（LCP 本就无意义）
TRIVIAL_TOKENS = 1024

RE_BOUND = re.compile(r"\[lcp_boundaries\] candidates=(\d+) boundaries=\[([\d, ]*)\] total=(\d+) msgs=(\d+)")
RE_SNAP = re.compile(r"\[cache_fetch\] LCP snapdown: B=(\d+) reused=(\d+) remaining=(\d+) \(entry_len=(\d+) lcp=(\d+)\)")
RE_MISS = re.compile(r"\[cache_fetch\] LCP unavailable: shared=(\d+) entry_len=(\d+) requested_len=(\d+) non_trimmable=(\w+)")
RE_MISS_UNREWIND = re.compile(r"\[cache_fetch\] LCP unavailable: shared=(\d+) entry_len=(\d+) cache could not rewind exactly by (\d+) tokens")
# 0.14.3 上游内置检查点口径（0920-12 事实③；格式与字段语义经 wheel 源码核验）
RE_SNAP_V143 = re.compile(r"\[cache_fetch\] LCP snapped to checkpoint: shared=(\d+) entry_len=(\d+) resumed_at=(\d+) remaining=(\d+)")
RE_SUPER_SNAP_V143 = re.compile(r"\[cache_fetch\] supersequence snapped to checkpoint: requested=(\d+) entry_len=(\d+) resumed_at=(\d+)")


def parse_lines(lines):
    """逐行解析遥测 → dict 计数器与样本列表（纯函数，可单测）。"""
    out = {
        "boundary_events": 0, "boundary_candidates": 0, "boundary_total_tokens": 0,
        "snap_hits": 0, "snap_reused": [], "snap_remaining": [], "snap_saved_pct": [],
        "snap_hits_v143": 0,            # 0.14.3 上游口径 LCP 命中
        "snap_hits_supersequence": 0,   # 0.14.3 上游 supersequence 命中（新增路径）
        "checkpoint_gap": [],           # shared − resumed_at（0.14.3 LCP 口径）
        "miss_total": 0, "miss_trivial": 0, "miss_meaningful": 0,
        "miss_meaningful_samples": [],
    }
    for line in lines:
        m = RE_BOUND.search(line)
        if m:
            out["boundary_events"] += 1
            out["boundary_candidates"] += int(m.group(1))
            out["boundary_total_tokens"] += int(m.group(3))
            continue
        m = RE_SNAP.search(line)
        if m:
            b, reused, remaining = int(m.group(1)), int(m.group(2)), int(m.group(3))
            out["snap_hits"] += 1
            out["snap_reused"].append(reused)
            out["snap_remaining"].append(remaining)
            total = reused + remaining          # = 请求总长（B 即复用位置）
            if total > 0:
                out["snap_saved_pct"].append(round(100.0 * reused / total, 1))
            continue
        m = RE_SNAP_V143.search(line)
        if m:
            # 上游口径：**复用量 = resumed_at**（上游 tokens_saved += position）；
            # shared 为 LCP 上限，差值即检查点粒度损失
            shared, entry_len, resumed, remaining = (int(m.group(i)) for i in (1, 2, 3, 4))
            out["snap_hits"] += 1
            out["snap_hits_v143"] += 1
            out["snap_reused"].append(resumed)
            out["snap_remaining"].append(remaining)
            total = resumed + remaining         # = 请求总长
            if total > 0:
                out["snap_saved_pct"].append(round(100.0 * resumed / total, 1))
            if shared >= resumed:
                out["checkpoint_gap"].append(shared - resumed)
            continue
        m = RE_SUPER_SNAP_V143.search(line)
        if m:
            requested, entry_len, resumed = (int(m.group(i)) for i in (1, 2, 3))
            out["snap_hits"] += 1
            out["snap_hits_supersequence"] += 1
            out["snap_reused"].append(resumed)
            out["snap_remaining"].append(max(0, requested - resumed))
            if requested > 0:
                out["snap_saved_pct"].append(round(100.0 * resumed / requested, 1))
            continue
        m = RE_MISS.search(line)
        if m:
            shared, entry_len, req, variant = int(m.group(1)), int(m.group(2)), int(m.group(3)), "non_trimmable"
        else:
            m = RE_MISS_UNREWIND.search(line)
            if not m:
                continue
            shared, entry_len, req, variant = int(m.group(1)), int(m.group(2)), None, "unrewind"
        out["miss_total"] += 1
        trivial = entry_len < TRIVIAL_TOKENS and shared < TRIVIAL_TOKENS
        if trivial:
            out["miss_trivial"] += 1
        else:
            out["miss_meaningful"] += 1
            if len(out["miss_meaningful_samples"]) < 20:
                sample = {"shared": shared, "entry_len": entry_len, "variant": variant}
                if req is not None:
                    sample["requested_len"] = req
                out["miss_meaningful_samples"].append(sample)
    return out


def summarize(stats):
    """计数器 → 报告 dict（分位数/命中率/分型）。"""
    def _pct(vals, q):
        if not vals:
            return None
        s = sorted(vals)
        return s[min(int(len(s) * q), len(s) - 1)]

    hits, misses = stats["snap_hits"], stats["miss_total"]
    lcp_events = hits + misses
    reused = stats["snap_reused"]
    gap = stats["checkpoint_gap"]
    return {
        "ts": datetime.now().isoformat(),
        "lcp_events": lcp_events,
        "boundary_events": stats["boundary_events"],
        "avg_boundaries_per_req": round(stats["boundary_candidates"] / stats["boundary_events"], 1)
            if stats["boundary_events"] else None,
        "hit_rate_pct": round(100.0 * hits / lcp_events, 1) if lcp_events else None,
        "snap_hits": hits,
        "snap_hits_v143_upstream": stats["snap_hits_v143"],
        "snap_hits_supersequence": stats["snap_hits_supersequence"],
        "reused_tokens_total": sum(reused),
        "reused_tokens_p50": _pct(reused, 0.5),
        "reused_tokens_p90": _pct(reused, 0.9),
        "reused_tokens_max": max(reused) if reused else None,
        "saved_pct_p50": _pct(stats["snap_saved_pct"], 0.5),
        "checkpoint_gap_p50": _pct(gap, 0.5),
        "checkpoint_gap_p90": _pct(gap, 0.9),
        "checkpoint_gap_max": max(gap) if gap else None,
        "misses": misses,
        "miss_trivial_small_entries": stats["miss_trivial"],
        "miss_meaningful": stats["miss_meaningful"],
        "miss_meaningful_samples": stats["miss_meaningful_samples"],
        "nontrivial_hit_rate_pct": round(100.0 * hits / (hits + stats["miss_meaningful"]), 1)
            if (hits + stats["miss_meaningful"]) else None,
    }


def read_matched_lines(log_path, tail_bytes=None):
    """读日志并返回匹配遥测的原始行（供归档）+ 全部行迭代。"""
    matched, raw = [], []
    with open(log_path, "r", errors="replace") as f:
        if tail_bytes:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
        for line in f:
            raw.append(line)
            if ("[lcp_boundaries]" in line) or ("[cache_fetch] LCP" in line) \
                    or ("[cache_fetch] supersequence" in line):
                matched.append(line)
    return matched, raw


def main():
    ap = argparse.ArgumentParser(description="LCP snap-down 遥测离线分析器")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--tail-bytes", type=int, default=None)
    ap.add_argument("--archive", action="store_true",
                    help="匹配原始行切段落盘 logs/diag/lcp_trace/（防轮转）")
    ap.add_argument("--json", action="store_true", help="JSON 输出（否则表格）")
    args = ap.parse_args()

    if not os.path.exists(args.log):
        sys.exit("日志不存在: %s" % args.log)
    matched, raw = read_matched_lines(args.log, args.tail_bytes)
    if not raw:
        sys.exit("日志为空")
    report = summarize(parse_lines(raw))

    if args.archive and matched:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        raw_path = os.path.join(ARCHIVE_DIR, "lcp-%s.jsonl" % stamp)
        with open(raw_path, "w", encoding="utf-8") as f:
            f.writelines(matched)
        rpt_path = os.path.join(ARCHIVE_DIR, "stats-%s.json" % stamp)
        with open(rpt_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        report["archived_to"] = raw_path
        report["archived_lines"] = len(matched)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        r = report
        print("LCP trace 报告  (%s, 源=%s%s)" % (
            r["ts"], args.log,
            " 尾部 %dB" % args.tail_bytes if args.tail_bytes else ""))
        print("-" * 56)
        print("边界计算事件      : %s (平均 %.1f boundaries/请求)" % (
            r["boundary_events"], r["avg_boundaries_per_req"] or 0))
        print("LCP cache_fetch   : %d 事件" % r["lcp_events"])
        print("  snapdown 命中    : %d  (%s%%)  [0.14.3 上游口径: %d｜supersequence: %d]" % (
            r["snap_hits"], r["hit_rate_pct"], r["snap_hits_v143_upstream"],
            r["snap_hits_supersequence"]))
        print("  非琐碎命中率     : %s%%  (剔除 trivial 小条目失配 %d 次)" % (
            r["nontrivial_hit_rate_pct"], r["miss_trivial_small_entries"]))
        print("  meaningful 失配  : %d" % r["miss_meaningful"])
        print("复用 tokens        : 合计 %s | p50 %s | p90 %s | max %s" % (
            r["reused_tokens_total"], r["reused_tokens_p50"],
            r["reused_tokens_p90"], r["reused_tokens_max"]))
        print("  单次节省比例 p50 : %s%%" % r["saved_pct_p50"])
        if r["checkpoint_gap_p50"] is not None:
            print("  检查点粒度损失   : p50 %s | p90 %s | max %s tokens"
                  "（shared−resumed_at；调 MAX/STRIDE 依据）" % (
                      r["checkpoint_gap_p50"], r["checkpoint_gap_p90"],
                      r["checkpoint_gap_max"]))
        if r["miss_meaningful_samples"]:
            print("meaningful 失配样本(前 3):")
            for smp in r["miss_meaningful_samples"][:3]:
                req = smp.get("requested_len")
                print("  shared=%d entry_len=%d requested=%s [%s]" % (
                    smp["shared"], smp["entry_len"],
                    req if req is not None else "—", smp["variant"]))
        if r.get("archived_to"):
            print("归档: %s (%d 行)" % (r["archived_to"], r["archived_lines"]))


if __name__ == "__main__":
    main()
