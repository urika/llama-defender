#!/usr/bin/env python3
"""Aggregate proxy_metrics.jsonl by session for behavior/routing analysis."""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "proxy_metrics.jsonl"


def tier_for_request_count(n):
    if n <= 10:
        return "short"
    if n <= 25:
        return "long"
    return "very_long"


def main():
    # Default: today; pass YYYY-MM-DD as first arg
    date_filter = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    sessions = defaultdict(lambda: {
        "client_type": "",
        "count": 0,
        "first_ts": None,
        "last_ts": None,
        "target_local": 0,
        "target_cloud": 0,
        "target_other": 0,
        "reasons": defaultdict(int),
        "stages": defaultdict(int),
        "input_chars": [],
        "output_chars": [],
        "duration_ms": [],
        "dispatch_ms": [],
        "max_run": 0,
        "loop_interventions": 0,
        "blocker_triggers": 0,
        "truncate_triggers": 0,
        "fallback": 0,
        "emergency_fallback": 0,
        "non_200": 0,
        "statuses": defaultdict(int),
        "errors": {"wasted": 0, "file_not_found": 0, "input_validation": 0},
        "compress": {"compressed_count": 0, "saved_chars": 0},
        "max_tokens_original": [],
        "max_tokens_dynamic": [],
        "tools_count": [],
        "cloud_model": set(),
    })

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get("ts", "")
            if not ts.startswith(date_filter):
                continue
            sid = r.get("session_id", "unknown")
            s = sessions[sid]
            s["count"] += 1
            if s["first_ts"] is None or ts < s["first_ts"]:
                s["first_ts"] = ts
            if s["last_ts"] is None or ts > s["last_ts"]:
                s["last_ts"] = ts
            s["client_type"] = r.get("client_type") or s["client_type"]

            pipe = r.get("pipeline", {})
            bd = pipe.get("backend_dispatcher", {})
            sr = pipe.get("smart_router", {})
            lc = pipe.get("lifecycle_stage", {})
            err = pipe.get("error_translator", {})
            compress = pipe.get("semantic_compress", {})
            loop = pipe.get("loop_detect", {})

            target = bd.get("route_target") or sr.get("target", "unknown")
            if target in ("local", "local_forced"):
                s["target_local"] += 1
            elif target == "cloud":
                s["target_cloud"] += 1
            else:
                s["target_other"] += 1

            reason = sr.get("reason", bd.get("route_reason", ""))
            s["reasons"][reason] += 1
            stage = sr.get("stage") or lc.get("stage", "unknown")
            s["stages"][stage] += 1

            s["input_chars"].append(r.get("input_chars") or 0)
            s["output_chars"].append(r.get("output_chars") or 0)
            s["duration_ms"].append(r.get("duration_ms") or 0)
            s["dispatch_ms"].append(bd.get("dispatch_latency_ms") or 0)
            s["tools_count"].append(r.get("input_tools") or 0)

            s["max_run"] = max(s["max_run"], loop.get("max_run", 0))
            if loop.get("level", 0) >= 1:
                s["loop_interventions"] += 1
            if pipe.get("blocker_detect", {}).get("triggered"):
                s["blocker_triggers"] += 1
            if pipe.get("truncate", {}).get("triggered"):
                s["truncate_triggers"] += 1
            if bd.get("route_fallback"):
                s["fallback"] += 1
            if bd.get("emergency_fallback"):
                s["emergency_fallback"] += 1

            status = r.get("status", 200)
            s["statuses"][status] += 1
            if status not in (200, None):
                s["non_200"] += 1

            s["errors"]["wasted"] += err.get("wasted", 0)
            s["errors"]["file_not_found"] += err.get("file_not_found", 0)
            s["errors"]["input_validation"] += err.get("input_validation", 0)

            s["compress"]["compressed_count"] += compress.get("compressed_count", 0)
            s["compress"]["saved_chars"] += compress.get("saved_chars", 0)

            s["max_tokens_original"].append(r.get("max_tokens_original", 0))
            s["max_tokens_dynamic"].append(r.get("max_tokens_dynamic", 0))

            cloud_model = bd.get("route_cloud_model") or sr.get("route_cloud_model")
            if cloud_model:
                s["cloud_model"].add(cloud_model)

    def avg(lst):
        return sum(lst) / len(lst) if lst else 0

    def pct(part, total):
        return f"{part / total * 100:.1f}%" if total else "0.0%"

    print(f"Session-level analysis for {date_filter} (source: {LOG_PATH})")
    print(f"Total sessions: {len(sessions)}")

    total_reqs = sum(s["count"] for s in sessions.values())
    total_local = sum(s["target_local"] for s in sessions.values())
    total_cloud = sum(s["target_cloud"] for s in sessions.values())
    total_non200 = sum(s["non_200"] for s in sessions.values())
    total_loop_iv = sum(s["loop_interventions"] for s in sessions.values())
    total_fallback = sum(s["fallback"] for s in sessions.values())
    total_emergency = sum(s["emergency_fallback"] for s in sessions.values())
    print(f"Total requests: {total_reqs}")
    print(f"Global routing: local={total_local} ({pct(total_local, total_reqs)}), "
          f"cloud={total_cloud} ({pct(total_cloud, total_reqs)})")
    print(f"Non-200: {total_non200}, loop_interventions: {total_loop_iv}, "
          f"fallback: {total_fallback}, emergency_fallback: {total_emergency}\n")

    # Sort by request count descending; only print active sessions
    printed = 0
    for sid, s in sorted(sessions.items(), key=lambda x: -x[1]["count"]):
        cnt = s["count"]
        if cnt < 2:
            continue
        if printed >= 15:
            break
        printed += 1
        print(f"## Session: {sid}")
        print(f"  client_type: {s['client_type'] or 'unknown'}")
        print(f"  requests: {cnt}")
        print(f"  time span: {s['first_ts']} -> {s['last_ts']}")
        print(f"  routing: local={s['target_local']} ({pct(s['target_local'], cnt)}), "
              f"cloud={s['target_cloud']} ({pct(s['target_cloud'], cnt)}), "
              f"other={s['target_other']}")
        print(f"  reasons (top): {dict(sorted(s['reasons'].items(), key=lambda kv: -kv[1])[:5])}")
        print(f"  stages: {dict(sorted(s['stages'].items(), key=lambda kv: -kv[1]))}")
        print(f"  input_chars: avg={avg(s['input_chars']):.0f}, max={max(s['input_chars']) if s['input_chars'] else 0:,}")
        print(f"  output_chars: avg={avg(s['output_chars']):.0f}, max={max(s['output_chars']) if s['output_chars'] else 0:,}")
        print(f"  duration: avg={avg(s['duration_ms']):.0f}ms, max={max(s['duration_ms']) if s['duration_ms'] else 0:.0f}ms")
        print(f"  dispatch_latency: avg={avg(s['dispatch_ms']):.0f}ms, max={max(s['dispatch_ms']) if s['dispatch_ms'] else 0:.0f}ms")
        print(f"  max_tokens: orig_avg={avg(s['max_tokens_original']):.0f}, dynamic_avg={avg(s['max_tokens_dynamic']):.0f}")
        print(f"  loop: max_run={s['max_run']}, interventions={s['loop_interventions']}")
        print(f"  blocker_triggers={s['blocker_triggers']}, truncate_triggers={s['truncate_triggers']}")
        print(f"  fallback={s['fallback']}, emergency_fallback={s['emergency_fallback']}")
        print(f"  non-200 statuses: {s['non_200']}, breakdown={dict(s['statuses'])}")
        print(f"  error_translator: {s['errors']}")
        print(f"  semantic_compress: {s['compress']}")
        print(f"  tools_count_avg={avg(s['tools_count']):.1f}")
        print(f"  cloud_models={sorted(s['cloud_model'])}")
        print()


if __name__ == "__main__":
    main()
