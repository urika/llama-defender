#!/usr/bin/env python3
"""
rapid-mlx / llama-server 后端日志性能分析器

从后端日志（默认 logs/llama-server.log）解析 rapid-mlx 的
"Chat completion: N tokens in Ts (X tok/s)"、"first token after Xs"、
cache_fetch/cache_store、schedule、Metal memory 等行，
统计 TTFT / 吞吐率 / prefix cache 命中 / 内存 / 错误码。

用法:
  python3 tools/analyze_backend_perf.py                          # 全量日志（自动识别模型段）
  python3 tools/analyze_backend_perf.py --model qwen3.8-27b-4bit # 按模型名过滤（支持别名/路径子串）
  python3 tools/analyze_backend_perf.py --model mlx-community/Qwen3.8-27B-4bit
  python3 tools/analyze_backend_perf.py --log /path/to/log       # 指定日志文件
  python3 tools/analyze_backend_perf.py --start-line 65141       # 从指定行号开始解析
  python3 tools/analyze_backend_perf.py --all-sessions           # 统计所有匹配会话（默认仅最近一次）
  python3 tools/analyze_backend_perf.py --json                   # 输出 JSON 摘要

说明:
  - 默认只统计"最近一次匹配模型加载点之后"的请求，避免历史会话混叠。
  - TTFT 来自流式请求日志（first token after Xs）；包含排队等待时间。
  - prefix cache 命中（cached）与全量 prefill（冷启动）分开统计。
"""

import argparse
import json
import os
import re
import statistics
import sys

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
DEFAULT_LOG = os.path.join(LOG_DIR, "llama-server.log")

LOAD_RE = re.compile(r"Model loaded: (?P<model>[^ ]+)")
CHAT_RE = re.compile(
    r"Chat completion(?P<stream> \(stream\))?: "
    r"(?P<tok>\d+) tokens in (?P<secs>[\d.]+)s \((?P<tokps>[\d.]+) tok/s\)"
)
TTFT_RE = re.compile(r"\[stream_outputs\] (?P<rid>[0-9a-f-]+) first token after (?P<secs>[\d.]+)s")
SCHED_RE = re.compile(
    r"\[schedule\] request=(?P<rid>[0-9a-f-]+) uid=\d+ prompt_tokens=(?P<pt>\d+) "
    r"tokens_to_prefill=(?P<prefill>\d+)(?P<cached>, \d+ cached)?"
)
CACHE_FETCH_RE = re.compile(
    r"\[cache_fetch\] request=(?P<rid>[0-9a-f-]+) (?P<status>MISS|HIT) prompt_tokens=\d+( cached=\d+)?( remaining=\d+)?"
)
LCP_UNAVAIL_RE = re.compile(
    r"\[cache_fetch\] LCP unavailable: shared=(?P<shared>\d+)"
)
MEM_RE = re.compile(r"\[Metal memory\] active=(?P<active>[\d.]+)GB peak=(?P<peak>[\d.]+)GB cache=(?P<cache>[\d.]+)GB")
HTTP_RE = re.compile(r'HTTP/1.1" (?P<code>\d{3}) ')


def percentile(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(int(len(s) * p), len(s) - 1)]


def fmt_secs(v):
    return f"{v:.1f}s"


def parse_log(logfile, model_filter, start_line, all_sessions):
    """返回分析结果 dict。"""
    load_lines = []
    chats = []
    ttfts = []          # (rid, secs)
    sched = {}          # rid -> (prompt_tokens, tokens_to_prefill, is_cached)
    cache_fetch = {}    # rid -> MISS/HIT
    mem_actives, mem_peaks, mem_caches = [], [], []
    errs = {}
    lcp_unavailable = 0
    lcp_shared = []

    # 确定起始行：--start-line 优先；否则收集所有匹配模型加载点，
    # 默认从最后一个加载点开始（避免历史会话混叠），--all-sessions 从第一个开始
    if start_line is None:
        with open(logfile, "r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                lm = LOAD_RE.search(line)
                if lm and (model_filter is None or model_filter in lm.group("model")):
                    load_lines.append((lineno, lm.group("model")))
        if load_lines:
            start_line = load_lines[0][0] if all_sessions else load_lines[-1][0]
    else:
        # 仅记录元信息，避免重复读日志
        with open(logfile, "r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                lm = LOAD_RE.search(line)
                if lm:
                    load_lines.append((lineno, lm.group("model")))
        if model_filter is not None:
            load_lines = [x for x in load_lines if model_filter in x[1]]

    with open(logfile, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if lineno < start_line:
                continue

            m = CHAT_RE.search(line)
            if m:
                chats.append((int(m.group("tok")), float(m.group("secs")),
                              float(m.group("tokps")), bool(m.group("stream"))))
                continue
            m = TTFT_RE.search(line)
            if m:
                ttfts.append((m.group("rid"), float(m.group("secs"))))
                continue
            m = CACHE_FETCH_RE.search(line)
            if m:
                cache_fetch[m.group("rid")] = m.group("status")
                continue
            m = LCP_UNAVAIL_RE.search(line)
            if m:
                lcp_unavailable += 1
                lcp_shared.append(int(m.group("shared")))
                continue
            m = SCHED_RE.search(line)
            if m:
                d = m.groupdict()
                sched[m.group("rid")] = (int(d["pt"]), int(d["prefill"]), bool(d["cached"]))
                continue
            m = MEM_RE.search(line)
            if m:
                mem_actives.append(float(m.group("active")))
                mem_peaks.append(float(m.group("peak")))
                mem_caches.append(float(m.group("cache")))
                continue
            m = HTTP_RE.search(line)
            if m:
                code = m.group("code")
                if code[0] in "45":
                    errs[code] = errs.get(code, 0) + 1
                continue

    return {
        "logfile": logfile,
        "load_points": load_lines,
        "chats": chats,
        "ttfts": ttfts,
        "sched": sched,
        "cache_fetch": cache_fetch,
        "lcp_unavailable": lcp_unavailable,
        "lcp_shared": lcp_shared,
        "mem_actives": mem_actives,
        "mem_peaks": mem_peaks,
        "mem_caches": mem_caches,
        "errs": errs,
    }


def summarize(res):
    chats = res["chats"]
    ttfts = res["ttfts"]
    sched = res["sched"]
    cache_fetch = res["cache_fetch"]

    out = {}

    # ── 吞吐 ──
    if chats:
        toks = [c[0] for c in chats]
        secs = [c[1] for c in chats]
        tokps = [c[2] for c in chats]
        streams = [c[3] for c in chats]
        weighted = sum(toks) / sum(secs)
        long = [(t, s) for t, s, _, _ in chats if t >= 500]
        sub = [(t, s) for t, s, _, _ in chats if t > 2]
        out["throughput"] = {
            "n": len(chats),
            "total_tokens": sum(toks),
            "mean": round(statistics.mean(tokps), 2),
            "median": round(statistics.median(tokps), 2),
            "p10": round(percentile(tokps, 0.1), 2),
            "p90": round(percentile(tokps, 0.9), 2),
            "weighted": round(weighted, 2),
            "weighted_gt2tok": round(sum(t for t, _ in sub) / sum(s for _, s in sub), 2),
            "weighted_long500": round(sum(t for t, _ in long) / sum(s for _, s in long), 2) if long else None,
            "stream": sum(streams),
            "non_stream": len(streams) - sum(streams),
        }
        print(f"  请求完成: {len(chats)}  输出 tokens: {sum(toks)}")
        print(f"  吞吐 tok/s: 均值={statistics.mean(tokps):.2f}  中位={statistics.median(tokps):.2f}  "
              f"P10={percentile(tokps, 0.1):.2f}  P90={percentile(tokps, 0.9):.2f}")
        print(f"  加权吞吐(总token/总耗时): {weighted:.2f} tok/s")
        print(f"  排除<=2 token 探测: {sum(t for t, _ in sub)/sum(s for _, s in sub):.2f} tok/s"
              f"  >=500 token 长输出: {sum(t for t, _ in long)/sum(s for _, s in long):.2f} tok/s" if long else
              f"  排除<=2 token 探测: {sum(t for t, _ in sub)/sum(s for _, s in sub):.2f} tok/s")
        print(f"  流式: {sum(streams)}  非流式: {len(streams)-sum(streams)}")

    # ── TTFT ──
    cold, hot = [], []
    for rid, secs in ttfts:
        if rid in sched:
            _, _, cached = sched[rid]
            (hot if cached else cold).append((secs, sched[rid][0]))
        else:
            cold.append((secs, None))
    buckets = [(0, 100, "<100"), (100, 1000, "0.1K-1K"), (1000, 8000, "1K-8K"),
               (8000, 30000, "8K-30K"), (30000, 100000, "30K-100K"), (100000, 10 ** 12, ">100K")]
    ttft_summary = {}
    if ttfts:
        all_s = [s for _, s in ttfts]
        out["ttft"] = {
            "n": len(ttfts),
            "mean": round(statistics.mean(all_s), 2),
            "median": round(percentile(all_s, 0.5), 2),
            "min": round(min(all_s), 2),
            "max": round(max(all_s), 2),
        }
        print(f"\n  TTFT (流式, n={len(ttfts)}): 均值={statistics.mean(all_s):.2f}s "
              f"中位={percentile(all_s, 0.5):.2f}s min={min(all_s):.2f}s max={max(all_s):.2f}s")
        for label, rows in (("冷启动(全量prefill)", cold), ("热启动(cache命中)", hot)):
            if not rows:
                continue
            s = [t for t, _ in rows]
            print(f"\n  {label} n={len(s)}: 均值={statistics.mean(s):.2f}s 中位={percentile(s, 0.5):.2f}s")
            bl = [(t, pt) for t, pt in rows if pt is not None]
            for lo, hi, name in buckets:
                sub = [t for t, pt in bl if lo <= pt < hi]
                if sub:
                    print(f"    prompt {name:>9}: n={len(sub):2d}  均值={sum(sub)/len(sub):7.2f}s")
            ttft_summary[label] = round(statistics.mean(s), 2)
        out["ttft"]["cold_mean"] = ttft_summary.get("冷启动(全量prefill)")
        out["ttft"]["hot_mean"] = ttft_summary.get("热启动(cache命中)")

    # ── cache ──
    hits = sum(1 for v in cache_fetch.values() if v == "HIT")
    misses = sum(1 for v in cache_fetch.values() if v == "MISS")
    if hits or misses:
        out["cache"] = {"hit": hits, "miss": misses,
                        "hit_rate": round(hits / (hits + misses) * 100, 1) if (hits + misses) else 0.0}
        print(f"\n  cache_fetch: HIT={hits}  MISS={misses}  命中率={hits/(hits+misses)*100:.1f}%")

    # ── LCP 诊断（hybrid 模型禁用 LCP 路径）──
    lcp_n = res["lcp_unavailable"]
    lcp_shared = res["lcp_shared"]
    if lcp_n:
        out["cache"]["lcp_unavailable"] = lcp_n
        out["cache"]["lcp_shared_tokens_mean"] = round(statistics.mean(lcp_shared), 1) if lcp_shared else 0
        out["cache"]["lcp_shared_tokens_max"] = max(lcp_shared) if lcp_shared else 0
        print(f"  LCP unavailable (non_trimmable=True, hybrid 模型禁用): {lcp_n} 次"
              f"  (shared 均值={statistics.mean(lcp_shared):.0f} 最大={max(lcp_shared)})")

    # ── 内存 ──
    if res["mem_actives"]:
        out["memory_gb"] = {
            "active_mean": round(statistics.mean(res["mem_actives"]), 1),
            "peak_max": round(max(res["mem_peaks"]), 1),
            "cache_mean": round(statistics.mean(res["mem_caches"]), 1),
        }
        print(f"\n  Metal 内存: active均值={statistics.mean(res['mem_actives']):.1f}GB  "
              f"peak最大值={max(res['mem_peaks']):.1f}GB  cache均值={statistics.mean(res['mem_caches']):.1f}GB")

    # ── 错误 ──
    if res["errs"]:
        out["http_errors"] = res["errs"]
        print(f"\n  HTTP 错误码: {res['errs']}")
    else:
        out["http_errors"] = {}
        print("\n  HTTP 错误码: 无")

    return out


def main():
    ap = argparse.ArgumentParser(description="rapid-mlx 后端日志性能分析")
    ap.add_argument("--log", default=DEFAULT_LOG, help=f"日志文件 (默认 {DEFAULT_LOG})")
    ap.add_argument("--model", default=None, help="模型名子串过滤 (默认自动识别: 无参数时取最近一次加载的模型)")
    ap.add_argument("--start-line", type=int, default=None, help="从指定行号开始解析")
    ap.add_argument("--all-sessions", action="store_true", help="统计所有匹配会话（默认仅最近一次）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = ap.parse_args()

    if not os.path.exists(args.log):
        print(f"[error] 日志文件不存在: {args.log}")
        sys.exit(1)

    # 无参数时：识别日志中最近一次加载的模型
    model_filter = args.model
    start_line = args.start_line
    if model_filter is None and start_line is None:
        last_model = None
        with open(args.log, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LOAD_RE.search(line)
                if m:
                    last_model = m.group("model")
        if last_model:
            model_filter = last_model
            print(f"[info] 自动识别最近加载模型: {model_filter}")
        else:
            print("[warn] 日志中没有 Model loaded 记录，将解析全量日志")
            args.all_sessions = True

    res = parse_log(args.log, model_filter, start_line, args.all_sessions)

    if not res["chats"] and not res["ttfts"]:
        print("[warn] 未解析到任何 completion 记录，请检查 --model / --start-line / --log 参数")
        sys.exit(1)

    print("=" * 62)
    print(f"rapid-mlx 后端性能分析: {res['load_points'][-1][1] if res['load_points'] else 'unknown'}")
    print(f"日志: {res['logfile']}  匹配会话: {len(res['load_points'])}")
    print("=" * 62)

    summary = summarize(res)

    if args.json:
        print("\n=== JSON 摘要 ===")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
