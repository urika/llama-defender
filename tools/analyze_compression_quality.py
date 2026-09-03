#!/usr/bin/env python3
"""Analyze compression efficiency and quality from proxy_metrics.jsonl.

The proxy records raw facts; this script computes derived quality metrics.
No third-party dependencies are required.
"""
import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict


DEFAULT_INPUT = os.path.join("logs", "proxy_metrics.jsonl")


def load_jsonl(path):
    records = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except OSError:
        return []
    return records


def percentile(values, fraction):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = int(math.floor(position))
    upper = min(lower + 1, len(values) - 1)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _stage(record, name):
    pipeline = record.get("pipeline")
    if not isinstance(pipeline, dict):
        return {}
    value = pipeline.get(name, {})
    return value if isinstance(value, dict) else {}


def _compression(record):
    return _stage(record, "content_compressor").get("compression", {})


def _truncation(record):
    for name in ("context_truncator", "truncate", "oom_safety"):
        value = _stage(record, name).get("compression", {})
        if isinstance(value, dict) and value:
            return value
    return {}


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _distribution(values):
    values = [value for value in values if _number(value)]
    if not values:
        return {"count": 0, "p50": None, "p90": None, "avg": None}
    return {
        "count": len(values),
        "p50": round(percentile(values, 0.50), 4),
        "p90": round(percentile(values, 0.90), 4),
        "avg": round(sum(values) / len(values), 4),
    }


def analyze_records(records):
    savings = []
    stage_deltas = []
    semantic_savings = []
    reread = []
    strategies = Counter()
    compression_stage_records = 0
    active_compression_requests = 0
    semantic_events = 0
    semantic_audit_failures = 0
    semantic_original_chars = 0
    semantic_saved_chars = 0
    context_before_chars = 0
    context_after_chars = 0
    truncation_count = 0
    status_ok = 0
    loop_count = 0
    blocker_count = 0
    high_drop_count = 0
    field_counts = Counter()

    for record in records:
        if record.get("status") == 200:
            status_ok += 1
        compression = _compression(record)
        truncation = _truncation(record)
        before = compression.get("context_before_chars")
        after = compression.get("context_after_chars")
        if _number(before) and before > 0:
            field_counts["context_before_chars"] += 1
            context_before_chars += before
            if _number(after):
                field_counts["context_after_chars"] += 1
                context_after_chars += after
                stage_deltas.append(1.0 - after / before)
        if compression:
            compression_stage_records += 1
        strategy = compression.get("strategy")
        if strategy:
            strategies[strategy] += 1
        semantic_count = compression.get("semantic_compressed", 0)
        if _number(semantic_count):
            semantic_events += int(semantic_count)
        audit_failures = compression.get("semantic_audit_failures", 0)
        if _number(audit_failures):
            semantic_audit_failures += int(audit_failures)
        original = compression.get("semantic_original_chars", 0)
        saved = compression.get("semantic_saved_chars", 0)
        if _number(original):
            semantic_original_chars += original
            if original > 0:
                field_counts["semantic_original_chars"] += 1
        if _number(saved):
            semantic_saved_chars += saved
        active = (compression.get("semantic_compressed", 0) or
                compression.get("cleared", False) or
                compression.get("think_stripped", 0) or
                compression.get("semantic_saved_chars", 0) or
                compression.get("cleared_chars", 0))
        if active:
            active_compression_requests += 1
            if _number(before) and before > 0:
                savings.append((
                    (compression.get("semantic_saved_chars", 0) or 0) +
                    (compression.get("cleared_chars", 0) or 0)
                ) / before)
        if _number(original) and original > 0 and _number(saved):
            semantic_savings.append(saved / original)
        if truncation.get("truncated"):
            truncation_count += 1
        flags = set(record.get("quality_flags") or [])
        loop_count += "loop_injected" in flags
        blocker_count += "blocker_injected" in flags
        high_drop_count += "high_drop_ratio" in flags
        if _number(record.get("reread_pressure")):
            reread.append(record["reread_pressure"])
        if _number(record.get("ifc", {}).get("reread_pressure")):
            reread.append(record["ifc"]["reread_pressure"])

    record_count = len(records)
    audit_total = semantic_events + semantic_audit_failures
    result = {
        "schema": "compression-quality-v1",
        "records": record_count,
        "successful_records": status_ok,
        "success_rate": _rate(status_ok, record_count),
        "compression_stage_records": compression_stage_records,
        "active_compression_requests": active_compression_requests,
        "truncation_requests": truncation_count,
        "strategy_counts": dict(strategies),
        "efficiency": {
            "context_savings_ratio": _distribution(savings),
            "stage_net_delta_ratio": _distribution(stage_deltas),
            "semantic_savings_ratio": _distribution(semantic_savings),
            "total_context_before_chars": context_before_chars,
            "total_context_after_chars": context_after_chars,
            "total_semantic_original_chars": semantic_original_chars,
            "total_semantic_saved_chars": semantic_saved_chars,
        },
        "fidelity_proxies": {
            "semantic_audit_pass_rate": _rate(semantic_events, audit_total),
            "semantic_audit_failures": semantic_audit_failures,
            "reread_pressure": _distribution(reread),
            "loop_injection_rate": _rate(loop_count, record_count),
            "blocker_rate": _rate(blocker_count, record_count),
            "high_drop_ratio_rate": _rate(high_drop_count, record_count),
        },
        "raw_field_coverage": {
            name: _rate(count, record_count)
            for name, count in field_counts.items()
        },
        "unavailable_without_payloads": [
            "entity_recall",
            "anchor_retention",
            "manifest_coverage",
            "recall_success_rate",
            "task_success_delta",
            "prefix_cache_delta",
        ],
    }
    result["quality_gate"] = quality_gate(result)
    return result


def quality_gate(summary, min_savings=0.15):
    fidelity = summary["fidelity_proxies"]
    efficiency = summary["efficiency"]["context_savings_ratio"]
    checks = {
        "audit_pass_rate": fidelity["semantic_audit_pass_rate"] in (None, 1.0),
        "no_loop_injection": (fidelity["loop_injection_rate"] or 0.0) == 0.0,
        "no_blocker": (fidelity["blocker_rate"] or 0.0) == 0.0,
        "minimum_savings": efficiency["count"] == 0 or (efficiency["avg"] or 0.0) >= min_savings,
    }
    return {"passed": all(checks.values()), "checks": checks}


def compare(baseline, treatment):
    def delta(path):
        left = baseline
        right = treatment
        for key in path:
            left = left.get(key) if isinstance(left, dict) else None
            right = right.get(key) if isinstance(right, dict) else None
        if _number(left) and _number(right):
            return round(right - left, 4)
        return None

    return {
        "schema": "compression-quality-ab-v1",
        "baseline_records": baseline.get("records", 0),
        "treatment_records": treatment.get("records", 0),
        "delta": {
            "success_rate": delta(["success_rate"]),
            "context_savings_avg": delta(["efficiency", "context_savings_ratio", "avg"]),
            "semantic_audit_pass_rate": delta(["fidelity_proxies", "semantic_audit_pass_rate"]),
            "reread_pressure_avg": delta(["fidelity_proxies", "reread_pressure", "avg"]),
            "loop_injection_rate": delta(["fidelity_proxies", "loop_injection_rate"]),
            "blocker_rate": delta(["fidelity_proxies", "blocker_rate"]),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT, help="treatment metrics JSONL")
    parser.add_argument("--baseline", help="baseline metrics JSONL for A/B comparison")
    parser.add_argument("--min-savings", type=float, default=0.15)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    treatment = analyze_records(load_jsonl(args.input))
    treatment["quality_gate"] = quality_gate(treatment, args.min_savings)
    output = {"treatment": treatment}
    if args.baseline:
        baseline = analyze_records(load_jsonl(args.baseline))
        output["baseline"] = baseline
        output["ab"] = compare(baseline, treatment)
    if args.as_json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"records={treatment['records']} success_rate={treatment['success_rate']}")
        efficiency = treatment["efficiency"]
        print("context_savings=" + json.dumps(efficiency["context_savings_ratio"], ensure_ascii=False))
        print("semantic_savings=" + json.dumps(efficiency["semantic_savings_ratio"], ensure_ascii=False))
        print("fidelity=" + json.dumps(treatment["fidelity_proxies"], ensure_ascii=False))
        print("quality_gate=" + ("PASS" if treatment["quality_gate"]["passed"] else "FAIL"))
        if args.baseline:
            print("ab_delta=" + json.dumps(output["ab"]["delta"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
