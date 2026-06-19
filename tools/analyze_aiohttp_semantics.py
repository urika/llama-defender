#!/usr/bin/env python3
"""
aiohttp 客户端语义行为深度分析器
聚焦最近 N 小时内 User-Agent = Python/3.11 aiohttp 的 OpenAI 透传请求
用法:
    python3 tools/analyze_aiohttp_semantics.py [小时数]
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

LOG_DIR = "logs"
PROXY_LOG = os.path.join(LOG_DIR, "anthropic_proxy.log")
MONITOR_DIR = os.path.join(LOG_DIR, "monitor")
os.makedirs(MONITOR_DIR, exist_ok=True)


def time_to_seconds(t):
    return t.hour * 3600 + t.minute * 60 + t.second


def infer_log_dates(lines):
    """从文件末尾倒推日期，处理跨天日志"""
    file_mtime = datetime.fromtimestamp(os.path.getmtime(PROXY_LOG))
    indexed = []
    for idx, line in enumerate(lines):
        m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", line)
        if m:
            ts_str = m.group(1)
            t = datetime.strptime(ts_str, "%H:%M:%S").time()
            indexed.append((idx, ts_str, t))
    if not indexed:
        return [(None, None)] * len(lines)

    date_by_index = {}
    current_date = file_mtime.date()
    last_idx, _, _ = indexed[-1]
    date_by_index[last_idx] = current_date

    for i in range(len(indexed) - 2, -1, -1):
        idx, _, t = indexed[i]
        _, _, next_t = indexed[i + 1]
        cur_sec = time_to_seconds(t)
        next_sec = time_to_seconds(next_t)
        if cur_sec > next_sec + 6 * 3600:
            current_date -= timedelta(days=1)
        date_by_index[idx] = current_date

    idx_to_ts = {idx: ts_str for idx, ts_str, _ in indexed}
    results = []
    for idx in range(len(lines)):
        if idx in date_by_index:
            results.append((idx_to_ts[idx], date_by_index[idx].isoformat()))
        else:
            results.append((None, None))
    return results


def parse_log_line(line):
    m = re.match(r"\[(\d{2}:\d{2}:\d{2})\](?:\s+\[\w+\])?(?:\s+\[sess=([\w-]+)\])?\s+(.*)", line)
    if not m:
        return None
    return {"ts": m.group(1), "sess": m.group(2), "msg": m.group(3)}


def classify_intent(content):
    """基于用户/系统消息内容做简单意图分类"""
    text = content.lower()
    if any(k in text for k in ["天气", "temperature", "forecast"]):
        return "weather"
    if any(k in text for k in ["小说", "三体", "故事", "story", "novel"]):
        return "creative_writing"
    if any(k in text for k in ["follow-up", "follow up", "follow_ups", "相关问题", "后续问题"]):
        return "follow_up_generation"
    if any(k in text for k in ["search queries", "queries", "搜索查询", "查询"]):
        return "search_query_generation"
    if "chat history" in text or "chat_history" in text:
        return "chat_history_analysis"
    if any(k in text for k in ["summary", "总结", "摘要"]):
        return "summarization"
    if any(k in text for k in ["code", "代码", "function", "编程"]):
        return "coding"
    return "general_chat"


def detect_language(content):
    """简单语言检测"""
    if not content:
        return "empty"
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", content))
    total_chars = len(re.sub(r"\s", "", content))
    if total_chars == 0:
        return "empty"
    ratio = chinese_chars / total_chars
    if ratio > 0.3:
        return "chinese"
    return "english"


def extract_last_user_message(messages):
    """提取最后一条 user 消息内容"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                return "\n".join(texts)
            return content
    return ""


def extract_user_queries(messages):
    """提取所有 user 消息，用于分析对话轮次"""
    queries = []
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                queries.append(content)
    return queries


def analyze_aiohttp(cutoff):
    if not os.path.exists(PROXY_LOG):
        return {"error": "anthropic_proxy.log not found"}

    with open(PROXY_LOG, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    dates = infer_log_dates(lines)
    requests = []
    current_req = None

    for line, (ts_str, date_str) in zip(lines, dates):
        line = line.strip()
        if not line or ts_str is None:
            continue
        parsed = parse_log_line(line)
        if not parsed:
            continue

        dt = datetime.strptime(f"{date_str} {ts_str}", "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if dt < cutoff:
            continue

        msg = parsed["msg"]
        sess = parsed["sess"]

        # 检测到新的 POST /v1/chat/completions 开始（后续根据 Headers 筛选 aiohttp）
        if "POST /v1/chat/completions" in msg:
            current_req = {
                "ts": ts_str,
                "dt": dt,
                "sess": sess,
                "headers": line,
                "is_aiohttp": False,
                "body_lines": [],
                "response_status": None,
                "response_body": None,
            }
        elif current_req is not None:
            # 下一个请求开始
            if re.search(r'(GET|POST)\s+/\S+', msg):
                if current_req.get("is_aiohttp"):
                    requests.append(current_req)
                current_req = None
                if "POST /v1/chat/completions" in msg:
                    current_req = {
                        "ts": ts_str,
                        "dt": dt,
                        "sess": sess,
                        "headers": line,
                        "is_aiohttp": False,
                        "body_lines": [],
                        "response_status": None,
                        "response_body": None,
                    }
            else:
                current_req["body_lines"].append(msg)
                if "aiohttp" in line:
                    current_req["is_aiohttp"] = True
                status_m = re.search(r'<- Response:\s*(\d{3})', msg)
                if status_m:
                    current_req["response_status"] = int(status_m.group(1))

    if current_req and current_req.get("is_aiohttp"):
        requests.append(current_req)

    # 解析 Body（支持截断/缺失的日志）
    parsed_requests = []
    body_available = 0
    body_parseable = 0

    for req in requests:
        body_text = " ".join(req["body_lines"])

        # 从 Headers 行提取 Content-Length（Headers 在 body_lines 中）
        headers_text = req["headers"]
        for bl in req["body_lines"]:
            if "Headers:" in bl:
                headers_text = bl
                break
        cl_m = re.search(r"'Content-Length':\s*'(\d+)'", headers_text)
        content_length = int(cl_m.group(1)) if cl_m else 0

        # 尝试完整解析 JSON Body
        body = None
        body_full_m = re.search(r'Body:\s*(\{.*\})', body_text)
        if body_full_m:
            body_available += 1
            try:
                body = json.loads(body_full_m.group(1))
                body_parseable += 1
            except Exception:
                pass

        if body:
            messages = body.get("messages", [])
            last_user = extract_last_user_message(messages)
            all_user_queries = extract_user_queries(messages)
            model = body.get("model", "")
            stream = body.get("stream")
            tools_count = len(body.get("tools", []))
            has_system_msg = any(m.get("role") == "system" for m in messages)
            message_count = len(messages)
            user_turn_count = len(all_user_queries)
            body_size = len(json.dumps(body))
        else:
            # 从截断文本中尽量提取字段
            last_user = ""
            stream_match = re.search(r'"stream":\s*(true|false)', body_text, re.IGNORECASE)
            stream = stream_match.group(1).lower() == "true" if stream_match else None
            model_match = re.search(r'"model":\s*"([^"]+)"', body_text)
            model = model_match.group(1) if model_match else ""
            tools_count = 1 if '"tools":' in body_text else 0
            has_system_msg = '"role": "system"' in body_text or '"role":"system"' in body_text
            message_count = body_text.count('"role":') + body_text.count('"role" :')
            user_turn_count = body_text.count('"role": "user"') + body_text.count('"role":"user"')
            body_size = content_length

        # 即使 JSON 截断，也尝试提取最后的 user content
        if not last_user:
            user_contents = re.findall(r'"role":\s*"user"[^}]*?"content":\s*"([^"]+)', body_text)
            if user_contents:
                last_user = user_contents[-1]

        intent = classify_intent(last_user)
        language = detect_language(last_user)

        parsed_requests.append({
            "ts": req["ts"],
            "dt": req["dt"],
            "sess": req["sess"],
            "model": model,
            "stream": stream,
            "message_count": message_count,
            "user_turn_count": user_turn_count,
            "last_user_content": last_user[:500],
            "last_user_length": len(last_user),
            "tools_count": tools_count,
            "has_system_msg": has_system_msg,
            "response_status": req["response_status"],
            "content_length": content_length,
            "intent": intent,
            "language": language,
            "body_size": body_size,
            "body_parsed": body is not None,
        })

    total = len(parsed_requests)
    if total == 0:
        return {"error": "no aiohttp chat completions in window"}

    # 语义统计
    intents = Counter(r["intent"] for r in parsed_requests)
    languages = Counter(r["language"] for r in parsed_requests)
    models = Counter(r["model"] for r in parsed_requests if r["model"])
    streams = Counter(str(r["stream"]) for r in parsed_requests if r["stream"] is not None)
    response_statuses = Counter(str(r["response_status"]) for r in parsed_requests if r["response_status"])
    user_turns = Counter(r["user_turn_count"] for r in parsed_requests)
    tools_usage = Counter("tools" if r["tools_count"] > 0 else "no_tools" for r in parsed_requests)
    parsed_bodies = Counter("parsed" if r["body_parsed"] else "truncated_or_missing" for r in parsed_requests)

    avg_msg_count = sum(r["message_count"] for r in parsed_requests) / total
    avg_user_len = sum(r["last_user_length"] for r in parsed_requests) / total
    avg_body_size = sum(r["body_size"] for r in parsed_requests) / total
    avg_content_length = sum(r["content_length"] for r in parsed_requests) / total

    # 会话连续性：按 sess 聚合
    sessions = {}
    for r in parsed_requests:
        sid = r["sess"] or "unknown"
        sessions.setdefault(sid, []).append(r)

    multi_turn_sessions = sum(1 for reqs in sessions.values() if len(reqs) > 1)
    max_turns_in_session = max((len(reqs) for reqs in sessions.values()), default=0)

    # 时间分布：按 10 分钟分桶
    buckets = Counter()
    for r in parsed_requests:
        bucket = r["dt"].replace(minute=(r["dt"].minute // 10) * 10, second=0, microsecond=0)
        buckets[bucket.strftime("%H:%M")] += 1

    # 典型请求样例（每种意图一个，优先用解析完整的）
    examples = {}
    for r in parsed_requests:
        intent = r["intent"]
        if intent not in examples or r["body_parsed"]:
            examples[intent] = {
                "ts": r["ts"],
                "model": r["model"],
                "stream": r["stream"],
                "user_turn_count": r["user_turn_count"],
                "content": r["last_user_content"][:300],
                "body_parsed": r["body_parsed"],
            }

    return {
        "total_requests": total,
        "time_range": f"{parsed_requests[0]['ts']} -> {parsed_requests[-1]['ts']}",
        "body_available": body_available,
        "body_parseable": body_parseable,
        "body_availability_pct": round(body_available / total * 100, 1) if total else 0,
        "body_parseable_pct": round(body_parseable / total * 100, 1) if total else 0,
        "intents": dict(intents.most_common()),
        "languages": dict(languages.most_common()),
        "models": dict(models.most_common()),
        "stream_distribution": dict(streams.most_common()),
        "response_statuses": dict(response_statuses.most_common()),
        "user_turn_distribution": dict(user_turns.most_common()),
        "tools_usage": dict(tools_usage.most_common()),
        "parsed_bodies": dict(parsed_bodies.most_common()),
        "avg_message_count": round(avg_msg_count, 1),
        "avg_last_user_length": round(avg_user_len, 0),
        "avg_body_size_bytes": round(avg_body_size, 0),
        "avg_content_length_bytes": round(avg_content_length, 0),
        "unique_sessions": len(sessions),
        "multi_turn_sessions": multi_turn_sessions,
        "max_turns_in_session": max_turns_in_session,
        "time_buckets": dict(sorted(buckets.items())),
        "examples": examples,
    }


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    now_str = datetime.now().strftime("%Y%m%d-%H%M%S")

    result = analyze_aiohttp(cutoff)

    report = {
        "generated_at": datetime.now().isoformat(),
        "window_hours": hours,
        "window_start": cutoff.isoformat(),
        "aiohttp_semantics": result,
    }

    out_json = os.path.join(MONITOR_DIR, f"aiohttp-semantics-{now_str}.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    out_txt = os.path.join(MONITOR_DIR, f"aiohttp-semantics-{now_str}.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(f"=== aiohttp 客户端语义行为分析 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===\n")
        f.write(f"时间窗口: 最近 {hours} 小时\n\n")

        if "error" in result:
            f.write(f"错误: {result['error']}\n")
        else:
            f.write(f"【基础统计】\n")
            f.write(f"  总请求数: {result['total_requests']}\n")
            f.write(f"  时间范围: {result['time_range']}\n")
            f.write(f"  独立会话/请求ID: {result['unique_sessions']}\n")
            f.write(f"  多轮会话数: {result['multi_turn_sessions']}\n")
            f.write(f"  单会话最大轮次: {result['max_turns_in_session']}\n")
            f.write(f"  平均消息数/请求: {result['avg_message_count']}\n")
            f.write(f"  平均最后 user 长度: {result['avg_last_user_length']} chars\n")
            f.write(f"  平均 Content-Length: {result['avg_content_length_bytes']} bytes\n")
            f.write(f"  平均请求体大小: {result['avg_body_size_bytes']} bytes\n")
            f.write(f"  Body 可见率: {result['body_available']}/{result['total_requests']} ({result['body_availability_pct']}%)\n")
            f.write(f"  Body 可解析率: {result['body_parseable']}/{result['total_requests']} ({result['body_parseable_pct']}%)\n\n")

            f.write(f"【意图分布】\n")
            for intent, count in result["intents"].items():
                f.write(f"  {intent}: {count}\n")
            f.write(f"\n")

            f.write(f"【语言分布】\n")
            for lang, count in result["languages"].items():
                f.write(f"  {lang}: {count}\n")
            f.write(f"\n")

            f.write(f"【模型使用】\n")
            for model, count in result["models"].items():
                f.write(f"  {model}: {count}\n")
            f.write(f"\n")

            f.write(f"【流式偏好】\n")
            for stream, count in result["stream_distribution"].items():
                f.write(f"  stream={stream}: {count}\n")
            f.write(f"\n")

            f.write(f"【响应状态】\n")
            for status, count in result["response_statuses"].items():
                f.write(f"  {status}: {count}\n")
            f.write(f"\n")

            f.write(f"【用户轮次分布】\n")
            for turns, count in sorted(result["user_turn_distribution"].items()):
                f.write(f"  {turns} turns: {count}\n")
            f.write(f"\n")

            f.write(f"【工具使用】\n")
            for item, count in result["tools_usage"].items():
                f.write(f"  {item}: {count}\n")
            f.write(f"\n")

            f.write(f"【Body 解析质量】\n")
            for item, count in result["parsed_bodies"].items():
                f.write(f"  {item}: {count}\n")
            f.write(f"\n")

            f.write(f"【时间分布（10分钟桶）】\n")
            for bucket, count in result["time_buckets"].items():
                f.write(f"  {bucket}: {count}\n")
            f.write(f"\n")

            f.write(f"【典型请求样例】\n")
            for intent, ex in result["examples"].items():
                f.write(f"\n  [{intent}] ts={ex['ts']} model={ex['model']} stream={ex['stream']} turns={ex['user_turn_count']}\n")
                f.write(f"  内容: {ex['content']}\n")

            # 语义洞察
            f.write(f"\n【语义行为洞察】\n")
            top_intent = result["intents"].most_common(1)[0][0] if hasattr(result["intents"], "most_common") else list(result["intents"].items())[0][0]
            # Counter converted to dict, so use sorted
            top_intent = max(result["intents"].items(), key=lambda x: x[1])[0]
            f.write(f"  • 主导意图: {top_intent} — 客户端主要在用模型做「{intent_description(top_intent)}」\n")

            if result["multi_turn_sessions"] == 0:
                f.write(f"  • 全部为单轮请求 — 客户端没有维护长对话，每个请求都是独立调用\n")
            else:
                f.write(f"  • 存在 {result['multi_turn_sessions']} 个多轮会话 — 客户端在部分场景下维护上下文\n")

            stream_true = result["stream_distribution"].get("True", 0)
            stream_false = result["stream_distribution"].get("False", 0)
            if stream_true > stream_false:
                f.write(f"  • 偏好流式输出 (stream=True {stream_true} vs stream=False {stream_false}) — 注重实时响应体验\n")
            else:
                f.write(f"  • 偏好非流式输出 — 可能是批量处理或需要完整 JSON\n")

            if result["languages"].get("chinese", 0) > result["languages"].get("english", 0):
                f.write(f"  • 以中文交互为主 — 用户/提示词主要为中文\n")
            else:
                f.write(f"  • 以英文交互为主\n")

            if result["tools_usage"].get("tools", 0) > 0:
                f.write(f"  • 使用工具调用能力 — 客户端主动传入 tools 定义，模型可触发外部工具\n")
            else:
                f.write(f"  • 未使用 tools 参数 — 纯文本/聊天交互\n")

            pending = result["total_requests"] - sum(result["response_statuses"].values())
            if pending > 0:
                f.write(f"  • {pending} 个请求未记录响应状态 — 可能为流式长连接未结束或连接异常\n")

            if result["body_parseable_pct"] < 50:
                f.write(f"  • Body 可解析率仅 {result['body_parseable_pct']}% — 代理日志对请求体有截断/省略，深度语义分析受限\n")

    print(f"Report written to {out_txt}")
    with open(out_txt, "r", encoding="utf-8") as f:
        print(f.read())


def intent_description(intent):
    mapping = {
        "weather": "天气/实时信息查询",
        "creative_writing": "创意写作/小说生成",
        "follow_up_generation": "后续问题推荐生成",
        "search_query_generation": "搜索查询生成",
        "chat_history_analysis": "聊天记录分析/推理",
        "summarization": "文本摘要",
        "coding": "代码生成/编程辅助",
        "general_chat": "通用对话",
    }
    return mapping.get(intent, intent)


if __name__ == "__main__":
    main()
