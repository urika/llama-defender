#!/usr/bin/env python3
"""
分析最近 N 小时的模型日志与 metrics
默认最近 1 小时
用法:
    python3 tools/analyze_last_hour.py [小时数]
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

LOG_DIR = "logs"
METRICS_FILE = os.path.join(LOG_DIR, "proxy_metrics.jsonl")
PROXY_LOG = os.path.join(LOG_DIR, "anthropic_proxy.log")
MONITOR_DIR = os.path.join(LOG_DIR, "monitor")

os.makedirs(MONITOR_DIR, exist_ok=True)


def get_hours():
    return float(sys.argv[1]) if len(sys.argv) > 1 else 1.0


def now_local():
    return datetime.now().astimezone()


def parse_log_ts(ts_str, default_date):
    """把 HH:MM:SS 解析为带本地时区的 datetime"""
    dt = datetime.strptime(f"{default_date} {ts_str}", "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=now_local().tzinfo)


def analyze_metrics(cutoff):
    if not os.path.exists(METRICS_FILE):
        return {"error": "proxy_metrics.jsonl not found"}

    records = []
    with open(METRICS_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                ts = r.get("ts", "")
                if not ts:
                    continue
                # ISO 格式可能带 Z 或 +00:00
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.astimezone() >= cutoff:
                    records.append(r)
            except Exception:
                continue

    total = len(records)
    if total == 0:
        return {"error": "no metrics in window", "window_start": cutoff.isoformat()}

    ok = sum(1 for r in records if r.get("status") == 200)
    durs = sorted((r.get("duration_ms") or 0) for r in records)
    avg_dur = sum(durs) / total
    avg_in = sum((r.get("input_chars") or 0) for r in records) / total
    avg_out = sum((r.get("output_chars") or 0) for r in records) / total
    avg_msgs = sum((r.get("input_msgs") or 0) for r in records) / total

    def pct(x):
        i = int(total * x / 100)
        return durs[max(0, min(i, total - 1))]

    trunc = sum(1 for r in records if r.get("pipeline", {}).get("truncate", {}).get("applied"))
    clear = sum(1 for r in records if r.get("pipeline", {}).get("tool_clear", {}).get("applied"))
    blocker = sum(1 for r in records if r.get("pipeline", {}).get("blocker_detect", {}).get("triggered"))
    loop = sum(1 for r in records if r.get("pipeline", {}).get("loop_detect", {}).get("max_run", 0) >= 3)
    text_loop = sum(1 for r in records if r.get("pipeline", {}).get("loop_detect", {}).get("is_text_loop"))

    flags = Counter()
    for r in records:
        for f in r.get("quality_flags", []):
            flags[f] += 1

    statuses = Counter(str(r.get("status")) for r in records)
    sessions = Counter(r.get("session_id") for r in records if r.get("session_id"))

    # 错误分类
    errors = [r for r in records if r.get("status") not in (200, None)]
    err_types = Counter()
    for r in errors:
        cls = r.get("pipeline", {}).get("error", {}).get("classified", "unknown")
        err_types[cls] += 1

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
        "error_classification": dict(err_types),
        "active_sessions": len(sessions),
        "top_sessions": sessions.most_common(10),
    }


def time_to_seconds(t):
    return t.hour * 3600 + t.minute * 60 + t.second


def infer_log_dates(lines):
    """
    为每行 [HH:MM:SS] 推断完整日期。
    锚定策略：用文件修改日期作为最后一行的时间所在日期，然后从后往前推导；
    若前一行时间比后一行时间晚超过 6 小时，则认为前一行属于前一天。
    返回 (ts_str, date_str) 列表，与 lines 一一对应。
    """
    file_mtime = datetime.fromtimestamp(os.path.getmtime(PROXY_LOG))
    # 先收集所有带时间戳的行索引
    indexed = []
    for idx, line in enumerate(lines):
        m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", line)
        if m:
            ts_str = m.group(1)
            t = datetime.strptime(ts_str, "%H:%M:%S").time()
            indexed.append((idx, ts_str, t))

    if not indexed:
        return [(None, None)] * len(lines)

    # 从最后一行开始，日期 = 文件修改日期
    date_by_index = {}
    current_date = file_mtime.date()
    # 如果最后一行时间比文件修改时间晚很多（>6h），可能文件是昨天写的，但这种情况少见
    last_idx, last_ts, last_t = indexed[-1]
    date_by_index[last_idx] = current_date

    for i in range(len(indexed) - 2, -1, -1):
        idx, ts_str, t = indexed[i]
        next_idx, next_ts, next_t = indexed[i + 1]
        cur_sec = time_to_seconds(t)
        next_sec = time_to_seconds(next_t)
        # 若当前行时间比后一行晚很多，说明跨天了（当前行属于前一天）
        if cur_sec > next_sec + 6 * 3600:
            current_date -= timedelta(days=1)
        date_by_index[idx] = current_date

    # 用字典加速索引查找
    idx_to_ts = {idx: ts_str for idx, ts_str, _ in indexed}
    results = []
    for idx in range(len(lines)):
        if idx in date_by_index:
            results.append((idx_to_ts[idx], date_by_index[idx].isoformat()))
        else:
            results.append((None, None))
    return results


def analyze_proxy_log(cutoff):
    if not os.path.exists(PROXY_LOG):
        return {"error": "anthropic_proxy.log not found"}

    with open(PROXY_LOG, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    dates = infer_log_dates(lines)
    events = []
    for line, (ts_str, date_str) in zip(lines, dates):
        line = line.strip()
        if not line or ts_str is None:
            continue
        m = re.match(r"\[(\d{2}:\d{2}:\d{2})\](?:\s+\[\w+\])?(?:\s+\[sess=([\w-]+)\])?\s+(.*)", line)
        if not m:
            continue
        _, sess, msg = m.group(1), m.group(2), m.group(3)
        try:
            dt = parse_log_ts(ts_str, date_str)
            if dt >= cutoff:
                events.append({"ts": ts_str, "dt": dt, "sess": sess, "msg": msg})
        except Exception:
            continue

    if not events:
        return {"error": "no proxy log events in window", "window_start": cutoff.isoformat()}

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

    # OpenAI 透传请求分析（/v1/chat/completions）
    openai_posts = [e for e in events if "POST /v1/chat/completions" in e["msg"]]
    openai_stream_true = 0
    openai_stream_false = 0
    openai_models = Counter()
    openai_content_lengths = []
    openai_response_status = Counter()

    i = 0
    while i < len(events):
        e = events[i]
        if "POST /v1/chat/completions" in e["msg"]:
            # 在接下来的几行找 Body、Content-Length 和 Response，直到下一个请求开始
            for j in range(i + 1, len(events)):
                lookahead = events[j]
                # 下一个新请求开始，停止
                if re.search(r'(GET|POST)\s+/\S+', lookahead["msg"]):
                    break
                body_m = re.search(r'Body:\s*(\{.*\})', lookahead["msg"])
                if body_m:
                    try:
                        body = json.loads(body_m.group(1))
                        if body.get("stream") is True:
                            openai_stream_true += 1
                        elif body.get("stream") is False:
                            openai_stream_false += 1
                        model = body.get("model", "")
                        if model:
                            openai_models[model] += 1
                    except Exception:
                        if '"stream": true' in lookahead["msg"]:
                            openai_stream_true += 1
                        elif '"stream": false' in lookahead["msg"]:
                            openai_stream_false += 1
                cl_m = re.search(r"'Content-Length':\s*'(\d+)'", lookahead["msg"])
                if cl_m:
                    openai_content_lengths.append(int(cl_m.group(1)))
                resp_m = re.search(r'<- Response:\s*(\d{3})', lookahead["msg"])
                if resp_m:
                    openai_response_status[resp_m.group(1)] += 1
        i += 1

    # 全量端点分布
    endpoints = Counter()
    for e in events:
        m = re.search(r'(GET|POST)\s+(/\S+)', e["msg"])
        if m:
            endpoints[f"{m.group(1)} {m.group(2)}"] += 1

    # User-Agent 分布（大小写不敏感）
    user_agents = Counter()
    for e in events:
        m = re.search(r"'User-Agent':\s*'([^']+)'", e["msg"], re.IGNORECASE)
        if m:
            user_agents[m.group(1)] += 1

    # 会话类型区分
    claude_sessions = set(e["sess"] for e in events if e["sess"] and re.match(r"^[a-f0-9]{8}$", e["sess"]))
    request_ids = set(e["sess"] for e in events if e["sess"] and e["sess"].startswith("req_"))

    # 提取最近的动作/工具（仅在 Anthropic 工具调用上下文中）
    actions = Counter()
    tool_calls = Counter()
    for e in events[-1000:]:
        if "tool_use" in e["msg"] or "tool_result" in e["msg"]:
            for action in ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "LS", "Task", "WebFetch", "WebSearch"]:
                if action in e["msg"] and "cleared" not in e["msg"] and "filter" not in e["msg"]:
                    actions[action] += 1
            m = re.search(r'"name":"(\w+)"', e["msg"])
            if m:
                tool_calls[m.group(1)] += 1

    # 最近的异常
    recent_errors = []
    for e in events[-200:]:
        if any(k in e["msg"] for k in ["ERROR", "WARN", "TEXT LOOP", "BLOCKER", "timeout", "backend_unavailable", "retryable", "Backend timeout"]):
            recent_errors.append(f"[{e['ts']}] {e['msg'][:200]}")

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
        "avg_chars": round(sum(s["chars"] for s in summaries) / len(summaries), 0) if summaries else 0,
        "avg_tools": round(sum(s["tools"] for s in summaries) / len(summaries), 1) if summaries else 0,
        "openai_requests": len(openai_posts),
        "openai_stream_true": openai_stream_true,
        "openai_stream_false": openai_stream_false,
        "openai_model_distribution": dict(openai_models.most_common(10)),
        "openai_avg_content_length": round(sum(openai_content_lengths) / len(openai_content_lengths), 0) if openai_content_lengths else 0,
        "openai_response_status": dict(openai_response_status),
        "endpoint_distribution": dict(endpoints.most_common(10)),
        "user_agents": dict(user_agents.most_common(10)),
        "claude_session_count": len(claude_sessions),
        "request_id_count": len(request_ids),
        "top_actions": dict(actions.most_common(10)),
        "top_tool_calls": dict(tool_calls.most_common(10)),
        "recent_errors": recent_errors[-10:],
    }


def main():
    hours = get_hours()
    cutoff = now_local() - timedelta(hours=hours)
    now_str = datetime.now().strftime("%Y%m%d-%H%M%S")

    perf = analyze_metrics(cutoff)
    sem = analyze_proxy_log(cutoff)

    report = {
        "generated_at": datetime.now().isoformat(),
        "window_hours": hours,
        "window_start": cutoff.isoformat(),
        "performance": perf,
        "semantics": sem,
    }

    out_json = os.path.join(MONITOR_DIR, f"last-hour-{now_str}.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    out_txt = os.path.join(MONITOR_DIR, f"last-hour-{now_str}.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(f"=== 最近 {hours} 小时模型日志分析报告 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===\n\n")
        f.write(f"时间窗口: {cutoff.strftime('%H:%M:%S')} -> {datetime.now().strftime('%H:%M:%S')} (本地时间)\n\n")

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
            f.write(f"  错误分类: {perf['error_classification']}\n")
            f.write(f"  活跃会话: {perf['active_sessions']}\n")

        f.write("\n【Claude 语义动作】\n")
        if "error" in sem:
            f.write(f"  错误: {sem['error']}\n")
        else:
            f.write(f"  日志行数: {sem['log_lines']}\n")
            f.write(f"  时间范围: {sem['time_range']}\n")
            f.write(f"  活跃会话: {sem['active_sessions']}\n")
            f.write(f"  Anthropic 格式请求: stream=True={sem['stream_true']} stream=False={sem['stream_false']}\n")
            f.write(f"  响应分布: 纯文本={sem['response_text_only']} 纯工具={sem['response_tools_only']} 混合={sem['response_mixed']}\n")
            f.write(f"  Tool clearing 次数: {sem['tool_clearing_count']}\n")
            f.write(f"  Anthropic 模型分布: {sem['model_distribution']}\n")
            f.write(f"  Anthropic 平均请求: {sem['avg_chars']} chars, {sem['avg_tools']} tools\n")

        f.write("\n【OpenAI 透传流量】\n")
        if "error" in sem:
            f.write(f"  错误: {sem['error']}\n")
        else:
            f.write(f"  POST /v1/chat/completions: {sem['openai_requests']}\n")
            f.write(f"  stream=True: {sem['openai_stream_true']}, stream=False: {sem['openai_stream_false']}\n")
            f.write(f"  透传模型分布: {sem['openai_model_distribution']}\n")
            f.write(f"  平均 Content-Length: {sem['openai_avg_content_length']} bytes\n")
            f.write(f"  响应状态: {sem['openai_response_status']}\n")
            f.write(f"  端点分布: {sem['endpoint_distribution']}\n")
            f.write(f"  User-Agent: {sem['user_agents']}\n")
            f.write(f"  会话类型: Claude会话={sem['claude_session_count']}, 请求ID={sem['request_id_count']}\n")
            f.write(f"  高频动作: {sem['top_actions']}\n")
            f.write(f"  高频工具调用: {sem['top_tool_calls']}\n")

        f.write("\n【最近异常/告警】\n")
        if "error" not in sem and sem["recent_errors"]:
            for err in sem["recent_errors"]:
                f.write(f"  {err}\n")
        else:
            f.write("  无\n")

        f.write("\n【核心洞察】\n")
        if "error" not in perf:
            if perf["success_rate"] < 90:
                f.write("  ⚠ 成功率低于90%，建议检查后端稳定性\n")
            if perf["p99_duration_ms"] > 120000:
                f.write("  ⚠ P99延迟超过120秒，后端可能存在阻塞或OOM\n")
            if perf["loop_detected"] > 0:
                f.write(f"  ⚠ 检测到 {perf['loop_detected']} 次循环调用模式\n")
            if perf["blocker_triggered"] > 0:
                f.write(f"  ⚠ Blocker 干预触发 {perf['blocker_triggered']} 次\n")
            if perf["tool_clear_applied"] > 0:
                f.write(f"  ℹ ToolClear 触发 {perf['tool_clear_applied']} 次\n")
        if "error" not in sem:
            if sem["claude_session_count"] > 0:
                f.write(f"  ℹ 检测到 {sem['claude_session_count']} 个 Claude Code 会话\n")
            else:
                f.write("  ℹ 最近一小时无 Claude Code 会话；流量来自 OpenAI 透传客户端\n")
            if sem["request_id_count"] > 0:
                f.write(f"  ℹ 独立请求 ID: {sem['request_id_count']} 个\n")
            if sem["openai_requests"] > 0 and sem["openai_response_status"].get('200', 0) < sem["openai_requests"]:
                pending = sem["openai_requests"] - sem["openai_response_status"].get('200', 0)
                f.write(f"  ⚠ {pending} 个 OpenAI 透传请求尚未记录 200 响应（可能仍在处理或失败）\n")

    print(f"Report written to {out_txt}")
    with open(out_txt, "r", encoding="utf-8") as f:
        print(f.read())


if __name__ == "__main__":
    main()
