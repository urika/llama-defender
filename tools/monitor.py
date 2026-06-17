#!/usr/bin/env python3
"""
定期监控：报文处理性能 + Claude 语义动作
输出到 logs/monitor/ 目录
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

LOG_DIR = "logs"
MONITOR_DIR = os.path.join(LOG_DIR, "monitor")
METRICS_FILE = os.path.join(LOG_DIR, "proxy_metrics.jsonl")
PROXY_LOG = os.path.join(LOG_DIR, "anthropic_proxy.log")

os.makedirs(MONITOR_DIR, exist_ok=True)


def analyze_performance(last_n=500):
    if not os.path.exists(METRICS_FILE):
        return {"error": "proxy_metrics.jsonl not found"}

    lines = []
    with open(METRICS_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                lines.append(line)

    last = []
    for line in lines[-last_n:]:
        try:
            last.append(json.loads(line))
        except Exception:
            continue

    total = len(last)
    if total == 0:
        return {"error": "no valid metrics"}

    ok = sum(1 for r in last if r.get("status") == 200)
    durs = sorted(r.get("duration_ms", 0) for r in last)
    avg_dur = sum(durs) / total
    avg_in = sum(r.get("input_chars", 0) for r in last) / total
    avg_out = sum(r.get("output_chars", 0) for r in last) / total
    avg_msgs = sum(r.get("input_msgs", 0) for r in last) / total

    def pct(x):
        i = int(total * x / 100)
        return durs[max(0, min(i, total - 1))]

    trunc = sum(1 for r in last if r.get("pipeline", {}).get("truncate", {}).get("applied"))
    clear = sum(1 for r in last if r.get("pipeline", {}).get("tool_clear", {}).get("applied"))
    blocker = sum(1 for r in last if r.get("pipeline", {}).get("blocker_detect", {}).get("triggered"))
    loop = sum(1 for r in last if r.get("pipeline", {}).get("loop_detect", {}).get("max_run", 0) >= 3)
    text_loop = sum(1 for r in last if r.get("pipeline", {}).get("loop_detect", {}).get("is_text_loop"))

    flags = Counter()
    for r in last:
        for f in r.get("quality_flags", []):
            flags[f] += 1

    statuses = Counter(str(r.get("status")) for r in last)

    return {
        "sample_size": total,
        "success": ok,
        "errors": total - ok,
        "success_rate": round(ok / total * 100, 1),
        "avg_duration_ms": round(avg_dur, 1),
        "p50_duration_ms": round(pct(50), 1),
        "p90_duration_ms": round(pct(90), 1),
        "p95_duration_ms": round(pct(95), 1),
        "p99_duration_ms": round(pct(99), 1),
        "avg_input_chars": round(avg_in, 0),
        "avg_output_chars": round(avg_out, 0),
        "avg_input_msgs": round(avg_msgs, 1),
        "truncate_applied": trunc,
        "tool_clear_applied": clear,
        "blocker_triggered": blocker,
        "loop_detected": loop,
        "text_loop_detected": text_loop,
        "quality_flags": dict(flags.most_common()),
        "status_distribution": dict(statuses),
    }


def analyze_semantics():
    if not os.path.exists(PROXY_LOG):
        return {"error": "anthropic_proxy.log not found"}

    events = []
    with open(PROXY_LOG, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = re.match(r"\[(\d{2}:\d{2}:\d{2})\](?:\s+\[sess=([\w-]+)\])?\s+(.*)", line)
            if m:
                events.append({"ts": m.group(1), "sess": m.group(2), "msg": m.group(3)})

    if not events:
        return {"error": "no events parsed"}

    sessions = Counter(e["sess"] for e in events if e["sess"])
    handling = [e for e in events if "Handling model=" in e["msg"]]
    stream_true = sum(1 for e in handling if "stream=True" in e["msg"])
    stream_false = sum(1 for e in handling if "stream=False" in e["msg"])
    total_stream = stream_true + stream_false

    streamed = [e for e in events if "Streamed text=" in e["msg"]]
    text_only = sum(1 for e in streamed if re.search(r"text=(\d+) chars, tools=0", e["msg"]))
    tools_only = sum(1 for e in streamed if re.search(r"text=0 chars, tools=[1-9]", e["msg"]))
    mixed = len(streamed) - text_only - tools_only

    clearings = [e for e in events if "Tool clearing:" in e["msg"]]

    models = Counter()
    for e in events:
        m = re.search(r"Handling model=([\w\-/.]+)", e["msg"])
        if m:
            models[m.group(1)] += 1

    summaries = []
    for e in events:
        m = re.search(r"chars=(\d+)\s+tools=(\d+)", e["msg"])
        if m and "REQ_SUMMARY" in e["msg"]:
            summaries.append({"chars": int(m.group(1)), "tools": int(m.group(2))})

    recent_reqs = []
    for e in events[-1000:]:
        m = re.search(r"REQ_SUMMARY.*chars=(\d+)\s+tools=(\d+)", e["msg"])
        if m:
            recent_reqs.append({"chars": int(m.group(1)), "tools": int(m.group(2))})

    return {
        "log_lines": len(events),
        "time_range": f"{events[0]['ts']} -> {events[-1]['ts']}",
        "active_sessions": len(sessions),
        "top_sessions": sessions.most_common(10),
        "stream_true": stream_true,
        "stream_false": stream_false,
        "stream_true_pct": round(stream_true / total_stream * 100, 1) if total_stream else 0,
        "response_text_only": text_only,
        "response_tools_only": tools_only,
        "response_mixed": mixed,
        "tool_clearing_count": len(clearings),
        "model_distribution": dict(models.most_common(10)),
        "total_req_summary": len(summaries),
        "recent_avg_chars": round(sum(r["chars"] for r in recent_reqs) / len(recent_reqs), 0) if recent_reqs else 0,
        "recent_avg_tools": round(sum(r["tools"] for r in recent_reqs) / len(recent_reqs), 1) if recent_reqs else 0,
    }


def main():
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    perf = analyze_performance()
    sem = analyze_semantics()

    report = {
        "generated_at": datetime.now().isoformat(),
        "performance": perf,
        "semantics": sem,
    }

    out_json = os.path.join(MONITOR_DIR, f"report-{now}.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    out_txt = os.path.join(MONITOR_DIR, f"report-{now}.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(f"=== 模型服务监控报告 ({now}) ===\n\n")
        f.write("【报文处理性能】\n")
        if "error" in perf:
            f.write(f"  错误: {perf['error']}\n")
        else:
            f.write(f"  样本数: {perf['sample_size']}\n")
            f.write(f"  成功率: {perf['success']}/{perf['sample_size']} ({perf['success_rate']}%)\n")
            f.write(f"  平均延迟: {perf['avg_duration_ms']}ms\n")
            f.write(f"  延迟分位: P50={perf['p50_duration_ms']}ms P90={perf['p90_duration_ms']}ms P95={perf['p95_duration_ms']}ms P99={perf['p99_duration_ms']}ms\n")
            f.write(f"  平均输入: {perf['avg_input_chars']} chars, 平均输出: {perf['avg_output_chars']} chars\n")
            f.write(f"  平均消息数: {perf['avg_input_msgs']}\n")
            f.write(f"  Truncate: {perf['truncate_applied']}, ToolClear: {perf['tool_clear_applied']}, Blocker: {perf['blocker_triggered']}\n")
            f.write(f"  Loop: {perf['loop_detected']}, TextLoop: {perf['text_loop_detected']}\n")
            f.write(f"  Quality flags: {perf['quality_flags']}\n")
            f.write(f"  状态码: {perf['status_distribution']}\n")

        f.write("\n【Claude 语义动作】\n")
        if "error" in sem:
            f.write(f"  错误: {sem['error']}\n")
        else:
            f.write(f"  日志行数: {sem['log_lines']}\n")
            f.write(f"  时间范围: {sem['time_range']}\n")
            f.write(f"  活跃会话: {sem['active_sessions']}\n")
            f.write(f"  Stream=True: {sem['stream_true']} ({sem['stream_true_pct']}%)\n")
            f.write(f"  响应分布: 纯文本={sem['response_text_only']} 纯工具={sem['response_tools_only']} 混合={sem['response_mixed']}\n")
            f.write(f"  Tool clearing 次数: {sem['tool_clearing_count']}\n")
            f.write(f"  模型分布: {sem['model_distribution']}\n")
            f.write(f"  最近平均请求: {sem['recent_avg_chars']} chars, {sem['recent_avg_tools']} tools\n")

        # Insights
        f.write("\n【核心洞察】\n")
        if "error" not in perf and perf["success_rate"] < 90:
            f.write("  ⚠ 成功率低于90%，建议检查后端稳定性\n")
        if "error" not in perf and perf["p99_duration_ms"] > 120000:
            f.write("  ⚠ P99延迟超过120秒，后端可能存在阻塞或OOM\n")
        if "error" not in sem and sem["active_sessions"] > 1:
            f.write(f"  ℹ 检测到 {sem['active_sessions']} 个并发会话\n")
        if "error" not in perf and perf["loop_detected"] > 0:
            f.write(f"  ⚠ 检测到 {perf['loop_detected']} 次循环调用模式\n")
        if "error" not in perf and perf["blocker_triggered"] > 0:
            f.write(f"  ⚠ Blocker 干预触发 {perf['blocker_triggered']} 次\n")

    print(f"Report written to {out_txt}")
    # 同时打印最新摘要到 stdout
    with open(out_txt, "r", encoding="utf-8") as f:
        print(f.read())


if __name__ == "__main__":
    main()
