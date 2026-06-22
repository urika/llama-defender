#!/usr/bin/env python3
"""Analyze recent proxy logs for smart-routing performance and Claude semantic behavior."""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
METRICS_PATH = os.path.join(LOG_DIR, "proxy_metrics.jsonl")
REQUESTS_PATH = os.path.join(LOG_DIR, "proxy_requests.jsonl")


def load_jsonl(path, max_lines=200000):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        print(f"[warn] {path} not found")
    return rows


def parse_ts(ts_str):
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def fmt_ms(ms):
    if ms is None:
        return "N/A"
    if ms >= 1000:
        return f"{ms/1000:.2f}s"
    return f"{ms:.0f}ms"


def percentile(vals, p):
    if not vals:
        return 0
    s = sorted(vals)
    idx = int(len(s) * p)
    return s[min(idx, len(s) - 1)]


def summarize_latency(name, vals):
    if not vals:
        print(f"  {name:12s}: no data")
        return
    n = len(vals)
    print(f"  {name:12s}: n={n:4d}  avg={fmt_ms(sum(vals)/n):>8s}  p50={fmt_ms(percentile(vals,0.5)):>8s}  p95={fmt_ms(percentile(vals,0.95)):>8s}  p99={fmt_ms(percentile(vals,0.99)):>8s}  max={fmt_ms(max(vals)):>8s}")


def analyze_period(label, metrics, total_all):
    total = len(metrics)
    if total == 0:
        print(f"\n{'='*60}\n📅 {label}: 无数据\n{'='*60}")
        return

    print(f"\n{'='*60}\n📅 {label}（n={total}, 占比 {total/total_all*100:.1f}%）\n{'='*60}")

    # Routing
    route_targets = Counter()
    route_reasons = Counter()
    fallback = 0
    emergency = 0
    sensitive = 0
    for m in metrics:
        bd = m.get("pipeline", {}).get("backend_dispatcher", {})
        target = bd.get("route_target", "local")
        route_targets[target] += 1
        if bd.get("route_fallback"):
            fallback += 1
        if bd.get("emergency_fallback"):
            emergency += 1
        if bd.get("sensitive_blocked"):
            sensitive += 1
        sr = m.get("pipeline", {}).get("smart_router", {})
        reason = sr.get("reason", "")
        if reason:
            route_reasons[reason] += 1

    print("🔀 路由分布")
    for target, cnt in route_targets.most_common():
        print(f"  {target:12s}: {cnt:5d} ({cnt/total*100:5.1f}%)")
    print(f"  fallback={fallback} emergency={emergency} sensitive_blocked={sensitive}")
    print("  路由原因 Top 5:")
    for reason, cnt in route_reasons.most_common(5):
        print(f"    {reason[:50]:50s}: {cnt:4d}")
    print()

    # Latency by route
    print("⏱️ 延迟（按路由目标）")
    cloud_durs = [m.get("duration_ms") or 0 for m in metrics if m.get("pipeline", {}).get("backend_dispatcher", {}).get("route_target") == "cloud"]
    local_durs = [m.get("duration_ms") or 0 for m in metrics if m.get("pipeline", {}).get("backend_dispatcher", {}).get("route_target") != "cloud"]
    summarize_latency("cloud", cloud_durs)
    summarize_latency("local", local_durs)

    # Latency by char-size bucket (建议3 per-bucket analysis)
    bucket_order = ["xs", "sm", "md", "lg", "xl", "xxl", "unknown"]
    bucket_latencies = defaultdict(list)
    bucket_disp = defaultdict(list)
    for m in metrics:
        bd = m.get("pipeline", {}).get("backend_dispatcher", {})
        bucket = bd.get("input_chars_bucket")
        if bucket is None:
            continue
        bucket_latencies[bucket].append(m.get("duration_ms") or 0)
        disp = bd.get("dispatch_latency_ms")
        if disp is not None and disp >= 0:
            bucket_disp[bucket].append(disp)
    has_bucket_data = any(bucket_latencies.values()) or any(bucket_disp.values())
    if has_bucket_data:
        print("\n📦 延迟（按输入大小分桶）")
        print(f"  {'bucket':8s} {'count':>6s}  {'req_dur avg/p95':>22s}  {'dispatch_lat avg/p95':>22s}")
        for bucket in bucket_order:
            reqs = bucket_latencies.get(bucket, [])
            disps = bucket_disp.get(bucket, [])
            if not reqs and not disps:
                continue
            req_str = (f"avg={sum(reqs)/len(reqs)/1000:.2f}s p95={percentile(reqs,0.95)/1000:.2f}s"
                       if reqs else "N/A")
            disp_str = (f"avg={sum(disps)/len(disps):.0f}ms p95={percentile(disps,0.95):.0f}ms"
                        if disps else "N/A")
            print(f"  {bucket:8s} {len(reqs) or len(disps):>6d}  {req_str:>22s}  {disp_str:>22s}")

    # Stages
    stages = Counter()
    for m in metrics:
        stage = m.get("pipeline", {}).get("lifecycle_stage", {}).get("stage", "unknown")
        stages[stage] += 1
    print("\n🌱 生命周期阶段")
    for stage, cnt in stages.most_common():
        print(f"  {stage:15s}: {cnt:5d} ({cnt/total*100:5.1f}%)")

    # Semantic behavior
    error_trans = Counter()
    blocker = 0
    loop_l2 = 0
    pre_trunc = 0
    oom_safety = 0
    for m in metrics:
        p = m.get("pipeline", {})
        et = p.get("error_translator", {})
        for k, v in et.items():
            if isinstance(v, int) and v > 0 and k != "count":
                error_trans[k] += v
        if p.get("blocker_detect", {}).get("triggered"):
            blocker += 1
        if p.get("loop_detect", {}).get("level", 0) >= 2:
            loop_l2 += 1
        if p.get("pre_truncate", {}).get("triggered"):
            pre_trunc += 1
        if p.get("oom_safety", {}).get("triggered"):
            oom_safety += 1

    print("\n🤖 Claude 语义行为")
    print(f"  Blocker 触发: {blocker}")
    print(f"  L2+ 循环: {loop_l2}")
    print(f"  Pre-truncate: {pre_trunc}")
    print(f"  OOM safety: {oom_safety}")
    if error_trans:
        print("  错误翻译:")
        for k, v in error_trans.most_common():
            print(f"    {k}: {v}")

    # Context
    input_chars = [m.get("input_chars") or 0 for m in metrics]
    output_chars = [m.get("output_chars") or 0 for m in metrics]
    est_input_tokens = [m.get("est_input_tokens") or 0 for m in metrics]
    max_dyn = [m.get("max_tokens_dynamic") for m in metrics]
    dyn_capped = sum(1 for x in max_dyn if x is not None and x < 4096)

    print("\n📏 上下文")
    print(f"  输入字符  avg={sum(input_chars)/len(input_chars):.0f}  p95={percentile(input_chars,0.95)}  max={max(input_chars)}")
    print(f"  输出字符  avg={sum(output_chars)/len(output_chars):.0f}  p95={percentile(output_chars,0.95)}  max={max(output_chars)}")
    print(f"  估算输入 tokens avg={sum(est_input_tokens)/len(est_input_tokens):.0f}  p95={percentile(est_input_tokens,0.95)}")
    print(f"  max_tokens 被动态压到 <4096: {dyn_capped} ({dyn_capped/total*100:.1f}%)")

    # Cost
    cost = 0.0
    for m in metrics:
        bd = m.get("pipeline", {}).get("backend_dispatcher", {})
        if bd.get("route_target") == "cloud" and not bd.get("route_fallback"):
            cost += ((m.get("est_input_tokens") or 0) * 0.5 + (m.get("est_output_tokens") or 0) * 1.5) / 1_000_000
    print(f"\n💰 估算云端成本: ¥{cost:.4f}")


def analyze():
    metrics = load_jsonl(METRICS_PATH)
    if not metrics:
        print("No metrics rows found.")
        return

    total = len(metrics)
    print(f"📊 总 metrics 行数: {total}")
    print(f"📅 时间范围: {metrics[0].get('ts')} -> {metrics[-1].get('ts')}")

    # Find first cloud route
    first_cloud_ts = None
    for m in metrics:
        if m.get("pipeline", {}).get("backend_dispatcher", {}).get("route_target") == "cloud":
            first_cloud_ts = m.get("ts")
            break
    print(f"☁️  首次 cloud 路由: {first_cloud_ts}")

    if first_cloud_ts:
        before = [m for m in metrics if (m.get("ts") or "") < first_cloud_ts]
        after = [m for m in metrics if (m.get("ts") or "") >= first_cloud_ts]
        analyze_period("智能路由启用前（纯 local）", before, total)
        analyze_period("智能路由启用后", after, total)
    else:
        analyze_period("全部", metrics, total)

    # Session-level detail for cloud-routed sessions
    print(f"\n{'='*60}\n👥 曾触发 cloud 路由的会话明细\n{'='*60}")
    sessions = defaultdict(list)
    for m in metrics:
        sid = m.get("session_id") or "none"
        sessions[sid].append(m)

    cloud_sessions = {sid: rows for sid, rows in sessions.items()
                      if any(r.get("pipeline", {}).get("backend_dispatcher", {}).get("route_target") == "cloud" for r in rows)}
    for sid, rows in sorted(cloud_sessions.items(), key=lambda x: len(x[1]), reverse=True):
        local = sum(1 for r in rows if r.get("pipeline", {}).get("backend_dispatcher", {}).get("route_target") != "cloud")
        cloud = sum(1 for r in rows if r.get("pipeline", {}).get("backend_dispatcher", {}).get("route_target") == "cloud")
        durs = [r.get("duration_ms") or 0 for r in rows]
        chars = [r.get("input_chars") or 0 for r in rows]
        print(f"  {sid}: total={len(rows)} local={local} cloud={cloud} avg_dur={fmt_ms(sum(durs)/len(durs))} max_chars={max(chars)}")


if __name__ == "__main__":
    analyze()
