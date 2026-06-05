#!/usr/bin/env python3
"""Prefix Cache Efficiency Analyzer for Rapid-MLX backend logs.

Parses cache_fetch and schedule lines from llama-server.log to compute
HIT/MISS rates, prefill savings, and prompt-length distributions.

Usage:
    python3 tools/cache_analyzer.py              # one-shot report
    python3 tools/cache_analyzer.py --watch      # refresh every 10s
    python3 tools/cache_analyzer.py --log logs/llama-server.log --scope all
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime


def parse_log(log_path, scope="startup"):
    """Parse backend log and return list of request dicts.

    scope:
        "all"      - entire log file
        "startup"  - since most recent MemoryAwarePrefixCache initialized
    """
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except (OSError, IOError) as e:
        print(f"Error reading {log_path}: {e}", file=sys.stderr)
        return []

    # Determine start index
    start_idx = 0
    if scope == "startup":
        for i, line in enumerate(lines):
            if "MemoryAwarePrefixCache initialized" in line:
                start_idx = i

    # Parse schedule lines: extract request_id -> prefill info
    schedule = {}
    for i, line in enumerate(lines[start_idx:], start=start_idx):
        m = re.search(
            r'schedule\] request=([a-f0-9-]+)\s+uid=(\d+)\s+prompt_tokens=(\d+)\s+tokens_to_prefill=([0-9,]+)(?:\s+cached)?',
            line,
        )
        if m:
            req_id, uid, prompt_tokens, prefill_str = m.groups()
            prefill_clean = int(prefill_str.replace(",", ""))
            schedule[req_id] = {
                "line": i,
                "uid": int(uid),
                "prompt_tokens": int(prompt_tokens),
                "prefill": prefill_clean,
            }

    # Parse cache_fetch lines and match with schedule
    results = []
    for i, line in enumerate(lines[start_idx:], start=start_idx):
        m = re.search(
            r'cache_fetch\] request=([a-f0-9-]+)\s+(HIT|MISS)\s+prompt_tokens=(\d+)\s+(?:cached=(\d+)\s+remaining=(\d+)\s+)?time=([0-9.]+)s(?:\s+entries=(\d+))?',
            line,
        )
        if m:
            req_id, status, prompt_tokens, cached, remaining, fetch_time, entries = m.groups()
            sched = schedule.get(req_id, {})
            results.append(
                {
                    "line": i,
                    "request_id": req_id,
                    "status": status,
                    "prompt_tokens": int(prompt_tokens),
                    "cached": int(cached) if cached else 0,
                    "remaining": int(remaining) if remaining else int(prompt_tokens),
                    "prefill": sched.get("prefill", int(remaining) if remaining else int(prompt_tokens)),
                    "fetch_time": float(fetch_time),
                    "entries": int(entries) if entries else 0,
                }
            )

    return results


def build_report(data, log_path, scope):
    """Build formatted text report from parsed data."""
    total = len(data)
    hits = [r for r in data if r["status"] == "HIT"]
    misses = [r for r in data if r["status"] == "MISS"]

    hit_count = len(hits)
    miss_count = len(misses)
    rate = (hit_count / total * 100) if total > 0 else 0

    total_prompt = sum(r["prompt_tokens"] for r in data)
    total_cached = sum(r["cached"] for r in hits)
    total_prefill = sum(r["prefill"] for r in data)
    savings = ((1 - total_prefill / total_prompt) * 100) if total_prompt > 0 else 0

    avg_cached = (total_cached / hit_count) if hit_count > 0 else 0
    avg_prefill_hit = (sum(r["prefill"] for r in hits) / hit_count) if hit_count > 0 else 0

    # Prompt length distribution
    buckets = [(0, 1000), (1000, 5000), (5000, 10000), (10000, 20000),
               (20000, 30000), (30000, 50000), (50000, 999999)]
    dist = []
    for lo, hi in buckets:
        h = sum(1 for r in hits if lo <= r["prompt_tokens"] < hi)
        m = sum(1 for r in misses if lo <= r["prompt_tokens"] < hi)
        dist.append((f"{lo//1000}K-{hi//1000}K" if hi < 999999 else f"{lo//1000}K+", h, m))

    lines = []
    lines.append("=" * 60)
    lines.append("     Prefix Cache Efficiency Report")
    lines.append("=" * 60)
    lines.append(f"Log file : {log_path}")
    lines.append(f"Scope    : {'current startup' if scope == 'startup' else 'entire log'}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Summary table
    lines.append("┌─────────────────┬──────────┬──────────┬──────────┐")
    lines.append("│ Metric          │ HIT      │ MISS     │ Total    │")
    lines.append("├─────────────────┼──────────┼──────────┼──────────┤")
    lines.append(f"│ Requests        │ {hit_count:>8} │ {miss_count:>8} │ {total:>8} │")
    lines.append(f"│ Prompt tokens   │ {sum(r['prompt_tokens'] for r in hits):>8,} │ {sum(r['prompt_tokens'] for r in misses):>8,} │ {total_prompt:>8,} │")
    lines.append(f"│ Cached tokens   │ {total_cached:>8,} │ {'—':>8} │ {total_cached:>8,} │")
    lines.append(f"│ Prefill tokens  │ {sum(r['prefill'] for r in hits):>8,} │ {sum(r['prefill'] for r in misses):>8,} │ {total_prefill:>8,} │")
    lines.append("└─────────────────┴──────────┴──────────┴──────────┘")
    lines.append("")

    lines.append(f"Hit Rate        : {rate:.1f}%")
    lines.append(f"Prefill Savings : {savings:.1f}%  (actual / total_prompt)")
    if hit_count > 0:
        lines.append(f"Avg cached/HIT  : {avg_cached:,.0f} tokens")
        lines.append(f"Avg prefill/HIT : {avg_prefill_hit:,.0f} tokens")
    lines.append("")

    # Distribution
    lines.append("Prompt Length Distribution:")
    lines.append("┌──────────┬────────┬────────┬──────────┐")
    lines.append("│ Range    │ HIT    │ MISS   │ Hit Rate │")
    lines.append("├──────────┼────────┼────────┼──────────┤")
    for label, h, m in dist:
        t = h + m
        r = (h / t * 100) if t > 0 else 0
        lines.append(f"│ {label:<8} │ {h:>6} │ {m:>6} │ {r:>6.1f}% │")
    lines.append("└──────────┴────────┴────────┴──────────┘")
    lines.append("")

    # Recent requests
    lines.append("Recent Requests (last 15):")
    lines.append("┌──────────┬──────────────────┬───────┬────────┬────────┬─────────┐")
    lines.append("│ Line     │ Request ID       │Status │ Prompt │ Cached │ Prefill │")
    lines.append("├──────────┼──────────────────┼───────┼────────┼────────┼─────────┤")
    for r in data[-15:]:
        status_icon = "✅" if r["status"] == "HIT" else "❌"
        lines.append(
            f"│ {r['line']:<8} │ {r['request_id']:<16} │{status_icon} {r['status']:<3}│ "
            f"{r['prompt_tokens']:>6,} │ {r['cached']:>6,} │ {r['prefill']:>7,} │"
        )
    lines.append("└──────────┴──────────────────┴───────┴────────┴────────┴─────────┘")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Prefix Cache Efficiency Analyzer")
    parser.add_argument(
        "--log",
        default="logs/llama-server.log",
        help="Path to backend log file (default: logs/llama-server.log)",
    )
    parser.add_argument(
        "--scope",
        choices=["all", "startup"],
        default="startup",
        help="Analyze scope: 'startup'=since last init, 'all'=entire log",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch mode: refresh report every N seconds",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Refresh interval in seconds (default: 10)",
    )
    args = parser.parse_args()

    log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), args.log) if not os.path.isabs(args.log) else args.log

    if args.watch:
        try:
            while True:
                os.system("clear" if os.name != "nt" else "cls")
                data = parse_log(log_path, args.scope)
                print(build_report(data, log_path, args.scope))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        data = parse_log(log_path, args.scope)
        print(build_report(data, log_path, args.scope))


if __name__ == "__main__":
    main()
