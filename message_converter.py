"""Message converter: Anthropic <-> OpenAI format conversion.
"""
import hashlib
import json
import os
import re

import proxy_state
from tool_parser import parse_tool_arguments, _extract_content_tool_calls

def convert_anthropic_tools_to_openai(tools):
    """Convert Anthropic tool format to OpenAI tool format.

    Handles three tool types:
    - Anthropic custom tools (type="custom") → OpenAI function tools
    - Simple tools (name only, no type) → OpenAI function tools
    - Anthropic server-side web_search_20250305 → mapped to a function tool
      with a `query` parameter so local/cloud OpenAI-compatible backends can
      execute it. The model extracts the query from the user message and
      emits a tool_call; proxy returns it as a tool_use block to Claude Code.
      See AGENTS.md "Server-side web_search mapping" for details.
    """
    if not tools:
        return None
    openai_tools = []
    for tool in tools:
        tool_type = tool.get("type", "")
        # Server-side web_search tool → function with query parameter
        if tool_type == "web_search_20250305":
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", "web_search"),
                    "description": (
                        "Search the web for up-to-date information. "
                        "Extract the search query from the user's message "
                        "(the text after 'Perform a web search for the query: ') "
                        "and pass it as the `query` parameter. Returns search "
                        "results with titles, URLs, and snippets."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "description": "The search query string",
                                "type": "string",
                            },
                        },
                        "required": ["query"],
                    },
                }
            })
        elif tool_type == "custom":
            # Anthropic custom tool
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                }
            })
        elif "name" in tool:
            # Simple tool definition
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", tool.get("parameters", {})),
                }
            })
    return openai_tools if openai_tools else None


def convert_anthropic_tool_choice_to_openai(tool_choice):
    """Convert Anthropic tool_choice to OpenAI tool_choice."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return "auto"
        elif tool_choice == "any":
            return {"type": "function"}
        elif tool_choice == "none":
            return "none"
    elif isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type", "")
        if tc_type == "tool":
            return {
                "type": "function",
                "function": {"name": tool_choice.get("name", "")}
            }
        elif tc_type == "auto":
            return "auto"
        elif tc_type == "any":
            return {"type": "function"}
        elif tc_type == "none":
            return "none"
    return None


def convert_openai_tools_to_anthropic(tools):
    """Convert OpenAI tool definitions to Anthropic tool format.

    OpenAI: {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    Anthropic: {"type": "custom", "name": ..., "description": ..., "input_schema": {...}}
    """
    if not tools:
        return None
    anthropic_tools = []
    for tool in tools:
        tool_type = tool.get("type", "")
        if tool_type == "function":
            func = tool.get("function", {})
            anthropic_tools.append({
                "type": "custom",
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })
        elif "name" in tool:
            # Fallback for simple/non-standard tool definitions
            anthropic_tools.append({
                "type": "custom",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters", tool.get("input_schema", {})),
            })
    return anthropic_tools if anthropic_tools else None


def convert_openai_tool_choice_to_anthropic(tool_choice):
    """Convert OpenAI tool_choice to Anthropic tool_choice."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return "auto"
        if tool_choice == "none":
            return "none"
        if tool_choice == "required":
            return "any"
    elif isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type", "")
        if tc_type == "function":
            return {
                "type": "tool",
                "name": tool_choice.get("function", {}).get("name", "")
            }
        if tc_type == "auto":
            return "auto"
        if tc_type == "none":
            return "none"
        if tc_type in ("required", "any"):
            return "any"
    return None


def _openai_content_to_anthropic(content):
    """Convert OpenAI message content (str or list of parts) to Anthropic content blocks."""
    if isinstance(content, str):
        if content:
            return [{"type": "text", "text": content}]
        return []
    if isinstance(content, list):
        blocks = []
        for part in content:
            if not isinstance(part, dict):
                continue
            pt = part.get("type", "")
            if pt == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            # image_url / multi-modal parts are skipped for now
        return blocks
    return []


def _convert_openai_user_msg(msg):
    """Convert an OpenAI user message to Anthropic format."""
    return {"role": "user", "content": _openai_content_to_anthropic(msg.get("content", ""))}


def _convert_openai_assistant_msg(msg):
    """Convert an OpenAI assistant message (with optional tool_calls) to Anthropic format."""
    content = msg.get("content", "")
    tool_calls = msg.get("tool_calls") or []
    blocks = _openai_content_to_anthropic(content)
    for tc in tool_calls:
        if tc.get("type") != "function":
            continue
        func = tc.get("function", {})
        raw_args = func.get("arguments", "{}")
        try:
            input_data = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            input_data = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": func.get("name", ""),
            "input": input_data if isinstance(input_data, dict) else {},
        })
    return {"role": "assistant", "content": blocks}


def _convert_openai_tool_msg(msg):
    """Convert an OpenAI tool message to an Anthropic tool_result block."""
    content = msg.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return {
        "type": "tool_result",
        "tool_use_id": msg.get("tool_call_id", ""),
        "content": content,
    }


def convert_openai_request_to_anthropic(body):
    """Convert an OpenAI chat-completion request body to Anthropic messages format.

    This allows /v1/chat/completions requests (e.g. OpenCode / OpenWebUI) to enter
    the same Anthropic-format pipeline as /v1/messages, gaining SmartRouter,
    lifecycle classification, truncation, compression, etc.
    """
    anthropic_body = {
        "model": body.get("model", ""),
        "max_tokens": body.get("max_tokens", 4096),
        "messages": [],
    }
    if "temperature" in body:
        anthropic_body["temperature"] = body["temperature"]
    if "top_p" in body:
        anthropic_body["top_p"] = body["top_p"]
    if "stream" in body:
        anthropic_body["stream"] = body["stream"]
    # 透传 thinking / response_format（DeepSeek 推理模型 v4-pro 必须 thinking enabled；
    # response_format json_object 用于 JSON 输出。OpenAI 请求转 Anthropic 时不丢弃）
    if "thinking" in body:
        anthropic_body["thinking"] = body["thinking"]
    if "response_format" in body:
        anthropic_body["response_format"] = body["response_format"]
    stop = body.get("stop")
    if stop is not None:
        if isinstance(stop, str):
            anthropic_body["stop_sequences"] = [stop]
        elif isinstance(stop, list):
            anthropic_body["stop_sequences"] = stop

    # Collect system messages into Anthropic top-level system field
    system_texts = []
    for msg in body.get("messages", []):
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                system_texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_texts.append(part.get("text", ""))
    if system_texts:
        anthropic_body["system"] = "\n".join(system_texts)

    # Convert remaining messages
    anthropic_messages = []
    for msg in body.get("messages", []):
        role = msg.get("role")
        if role == "system":
            continue
        if role == "user":
            anthropic_messages.append(_convert_openai_user_msg(msg))
        elif role == "assistant":
            anthropic_messages.append(_convert_openai_assistant_msg(msg))
        elif role == "tool":
            # Attach tool_result to the most recent user message to preserve
            # Anthropic's user/tool_result pairing requirement.
            if not anthropic_messages:
                anthropic_messages.append({"role": "user", "content": []})
            last = anthropic_messages[-1]
            if last.get("role") != "user":
                anthropic_messages.append({"role": "user", "content": []})
                last = anthropic_messages[-1]
            content = last.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}] if content else []
                last["content"] = content
            content.append(_convert_openai_tool_msg(msg))

    anthropic_body["messages"] = anthropic_messages

    tools = body.get("tools")
    if tools:
        anthropic_body["tools"] = convert_openai_tools_to_anthropic(tools)
    tc = body.get("tool_choice")
    if tc is not None:
        anthropic_body["tool_choice"] = convert_openai_tool_choice_to_anthropic(tc)

    # Preserve single-request route override if present
    if "_x_proxy_route_to" in body:
        anthropic_body["_x_proxy_route_to"] = body["_x_proxy_route_to"]

    return anthropic_body


def _estimate_message_chars(messages):
    """Rough character count for threshold checking (no tokenizer)."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    total += len(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    total += len(str(block.get("content", "")))
                elif block.get("type") == "tool_use":
                    total += len(json.dumps(block.get("input", {}), ensure_ascii=False))
        else:
            total += len(str(content))
    return total


def _extract_text_from_messages(messages):
    """Concatenate all text content from messages for content-type analysis."""
    parts = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    parts.append(str(block.get("content", "")))
                elif block.get("type") == "tool_use":
                    parts.append(json.dumps(block.get("input", {}), ensure_ascii=False))
        else:
            parts.append(str(content))
    return "\n".join(parts)


def _classify_content_for_ratio(text):
    """Return dominant content type for dynamic token ratio selection.

    Heuristics:
      - cjk: ratio of CJK characters > 0.4
      - code: high density of code tokens (brackets, semicolons, keywords)
      - english: fallback
    """
    if not text:
        return "english"
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    total = len(text)
    if total == 0:
        return "english"
    if cjk / total > 0.4:
        return "chinese"
    code_tokens = len(re.findall(r"[{}\[\];()=]", text))
    keywords = len(re.findall(
        r"\b(def|class|function|const|let|var|import|from|return|if|else|for|while|try|except|async|await)\b",
        text, re.IGNORECASE))
    if total > 200 and (code_tokens / total > 0.08 or keywords >= 5):
        return "code"
    return "english"


def _estimate_tokens_dynamic(messages, ratio_override=None):
    """Estimate token count using content-type-aware ratios.

    Falls back to proxy_state.PROXY_CTX_TOKEN_RATIO when ratio_override is provided or
    content classification is inconclusive.
    """
    if ratio_override:
        return int(_estimate_message_chars(messages) / max(ratio_override, 0.1))
    text = _extract_text_from_messages(messages)
    content_type = _classify_content_for_ratio(text)
    ratio_map = {
        "chinese": proxy_state.PROXY_TOKEN_RATIO_CHINESE,
        "english": proxy_state.PROXY_TOKEN_RATIO_ENGLISH,
        "code": proxy_state.PROXY_TOKEN_RATIO_CODE,
    }
    ratio = ratio_map.get(content_type, proxy_state.PROXY_CTX_TOKEN_RATIO)
    # For mixed content, weight by detected type but blend with the default ratio
    # to avoid over-correction on short or ambiguous inputs.
    if len(text) < 500:
        ratio = (ratio + proxy_state.PROXY_CTX_TOKEN_RATIO) / 2.0
    return int(_estimate_message_chars(messages) / max(ratio, 0.1))


def _compute_re_read_rate(re_read_files, cleared_files):
    """Compute re-read rate as a percentage capped at 100.

    DEF-003: rate must be re_read_files / cleared_files, not raw call count.
    Returns 0.0 when there are no cleared files.
    """
    if not cleared_files:
        return 0.0
    return min(float(re_read_files) / float(cleared_files) * 100.0, 100.0)


def _message_stable_hash(msg):
    """Return a stable hash for a message dict used in prefix comparison."""
    try:
        return hashlib.sha256(json.dumps(msg, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return hashlib.sha256(str(msg).encode("utf-8")).hexdigest()


def _compute_common_prefix_ratio(current, previous):
    """Compute the ratio of chars in the common prefix of two message lists.

    Walks from the first message until messages differ, then returns
    common_chars / total_chars. Used to quantify prefix cache stability.
    """
    if not current or not previous:
        return 0.0
    common_chars = 0
    min_len = min(len(current), len(previous))
    for i in range(min_len):
        if _message_stable_hash(current[i]) != _message_stable_hash(previous[i]):
            break
        common_chars += _estimate_message_chars([current[i]])
    total_chars = _estimate_message_chars(current)
    if total_chars <= 0:
        return 0.0
    return round(common_chars / total_chars, 4)


def _has_thinking_content(msg):
    """Check if an assistant message contains thinking/reasoning content."""
    content = msg.get("content", "")
    if isinstance(content, list):
        for block in content:
            if block.get("type") == "thinking":
                return True
            if block.get("type") == "text":
                text = block.get("text", "")
                if "<thinking>" in text or "</thinking>" in text:
                    return True
    elif isinstance(content, str):
        if "<thinking>" in content or "</thinking>" in content:
            return True
    return False


def _strip_thinking_from_msg(msg):
    """Remove thinking content from a message (in-place)."""
    content = msg.get("content", "")
    if isinstance(content, list):
        new_content = []
        for block in content:
            if block.get("type") == "thinking":
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
                text = text.strip()
                if text:
                    new_content.append({"type": "text", "text": text})
            else:
                new_content.append(block)
        msg["content"] = new_content
    elif isinstance(content, str):
        content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL).strip()
        msg["content"] = content


def strip_old_thinking_blocks(messages, keep_recent=3, frozen_head=None):
    """
    Remove thinking/reasoning content from old assistant messages.
    Keeps the most recent keep_recent assistant messages with thinking intact.
    Set keep_recent=0 to skip thinking stripping entirely (INIT/GROWTH stages).
    When frozen_head > 0, the first N messages are scanned but their thinking
    blocks are NEVER stripped — this preserves prefix KV cache stability.
    Only thinking blocks in the dynamic zone (messages frozen_head+) are
    eligible for stripping (oldest dynamic thinking stripped first).
    Returns (messages, stats_dict).
    """
    if not messages:
        return messages, {"enabled": False}
    if frozen_head is None:
        frozen_head = proxy_state.PROXY_FROZEN_HEAD

    # keep_recent=0 means skip entirely (lightweight stages)
    if keep_recent <= 0:
        return messages, {"enabled": True, "skipped": True, "reason": "stage_skip", "keep_recent": 0}

    # Find all assistant messages with thinking, but only strip in dynamic zone
    thinking_indices = []
    for idx, msg in enumerate(messages):
        if msg.get("role") == "assistant" and _has_thinking_content(msg):
            thinking_indices.append(idx)

    if not thinking_indices:
        return messages, {"enabled": True, "skipped": True, "reason": "no_thinking_found"}

    # Filter to dynamic zone indices (those in frozen zone are protected)
    dynamic_thinking = [idx for idx in thinking_indices if idx >= frozen_head]
    frozen_thinking = [idx for idx in thinking_indices if idx < frozen_head]

    if len(dynamic_thinking) <= keep_recent:
        return messages, {
            "enabled": True,
            "skipped": True,
            "reason": "few_dynamic_thinking",
            "count": len(dynamic_thinking),
            "frozen_thinking_count": len(frozen_thinking),
            "frozen_head": frozen_head,
        }

    # Keep the most recent `keep_recent` dynamic thinking messages
    keep_set = set(dynamic_thinking[-keep_recent:])
    stripped_count = 0
    for idx in dynamic_thinking:
        if idx in keep_set:
            continue
        _strip_thinking_from_msg(messages[idx])
        stripped_count += 1

    return messages, {
        "enabled": True,
        "stripped": True,
        "stripped_count": stripped_count,
        "kept": keep_recent,
        "total_thinking": len(thinking_indices),
        "frozen_thinking_count": len(frozen_thinking),
        "frozen_head": frozen_head,
    }


# ---------------------------------------------------------------------------
def convert_anthropic_messages_to_openai(messages):
    """Convert Anthropic message format to OpenAI message format."""
    openai_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            # Complex content with tool_use / tool_result
            text_parts = []
            tool_calls = []
            tool_results = []

            for block in content:
                block_type = block.get("type", "")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    tool_input = block.get("input", {})
                    if tool_input is None:
                        tool_input = {}
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(tool_input) if isinstance(tool_input, dict) else (tool_input if isinstance(tool_input, str) else "{}"),
                        }
                    })
                elif block_type == "tool_result":
                    tr_content = block.get("content", "")
                    if tr_content is None:
                        tr_content = ""
                    tool_results.append({
                        "tool_call_id": block.get("tool_use_id", ""),
                        "role": "tool",
                        "content": tr_content,
                    })

            if role == "assistant" and tool_calls:
                openai_msg = {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else None,
                    "tool_calls": tool_calls,
                }
                if not openai_msg["content"]:
                    del openai_msg["content"]
                openai_messages.append(openai_msg)
            elif role == "user" and tool_results:
                # Tool results MUST come immediately after the assistant
                # tool_calls that triggered them. OpenAI/DeepSeek strictly
                # validate that every tool_calls message is followed by tool
                # messages (one per tool_call_id) before any other role.
                # Inserting a user text message here would break that pairing
                # ("insufficient tool messages following tool_calls").
                # So: emit tool results first, then any trailing text.
                for tr in tool_results:
                    tr_content = tr["content"]
                    if tr_content is None:
                        tr_content = ""
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": str(tr_content),
                    })
                if text_parts:
                    openai_messages.append({
                        "role": "user",
                        "content": "\n".join(text_parts),
                    })
            else:
                openai_messages.append({
                    "role": role,
                    "content": "\n".join(text_parts) if text_parts else json.dumps(content),
                })
        else:
            openai_msg = {
                "role": role,
                "content": str(content) if content else "",
            }
            # Preserve OpenAI-compatible tool message fields when the input
            # already uses role="tool" with tool_call_id (e.g. OpenCode).
            if role == "tool":
                tool_call_id = msg.get("tool_call_id")
                if tool_call_id:
                    openai_msg["tool_call_id"] = tool_call_id
                name = msg.get("name")
                if name:
                    openai_msg["name"] = name
            openai_messages.append(openai_msg)

    # OpenCode (and some other clients) may emit assistant messages that use
    # plain text like "[Calling tool..." to indicate tool calls, while the
    # corresponding tool results arrive as standalone role="tool" messages
    # with a tool_call_id.  OpenAI/DeepSeek requires every role="tool" message
    # to follow an assistant message that contains a matching tool_calls entry.
    # When that precondition is violated, downstream APIs reject the request
    # with a 400 error.  As a safety net, convert orphaned tool messages into
    # user messages so the conversation remains valid OpenAI format.
    openai_messages = _normalize_orphan_tool_messages(openai_messages)

    # Then ensure every assistant tool_calls block has matching tool responses.
    # Inject tombstones for missing responses so strict backends don't reject
    # the conversation.
    openai_messages = _ensure_tool_chain_integrity(openai_messages)

    return openai_messages


def _extract_tool_call_ids_from_text(text):
    """Extract likely tool_call_ids embedded in assistant text.

    Matches common patterns emitted by clients such as OpenCode
    (e.g. "[Calling tool bash with id call_abc123...]").

    Returns a set of matched ids; empty set if text is not a string.
    """
    if not isinstance(text, str):
        return set()
    ids = set()
    # OpenAI-style ids: call_xxxxxxxxxxxxxxxxxxxxxxxx
    ids.update(re.findall(r"\bcall_[a-f0-9]{24}\b", text))
    # Anthropic-style ids: toolu_xxxxxxxxxxxxxxxxxxxxxxxx
    ids.update(re.findall(r"\btoolu_[a-zA-Z0-9]{24}\b", text))
    return ids


def _tombstone_tool_msg(tool_call_id):
    """Return a placeholder tool message for a missing tool result."""
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps({
            "error": "Tool result was not provided in the conversation history.",
            "tool_call_id": tool_call_id,
        }, ensure_ascii=False),
    }


def _ensure_tool_chain_integrity(openai_messages):
    """Inject tombstone tool messages for dangling assistant tool_calls.

    Strict backends (DeepSeek / OpenAI) require that every assistant message
    with `tool_calls` is followed by one `role="tool"` message for each
    `tool_call_id`. When the conversation history is missing a response (e.g.
    an aborted tool call, a context-compaction bug, or a client-side
    serialization issue), this function inserts a tombstone message so the
    request remains valid.

    This function is intentionally conservative:
    - It runs *after* `_normalize_orphan_tool_messages`, so orphan tool
      results have already been converted to user messages.
    - It only injects tombstones for `tool_call_id`s that are still pending
      when the tool-response zone ends (non-tool message or end of list).
    """
    result = []
    pending_tool_calls = {}  # id -> tool_call dict

    for msg in openai_messages:
        role = msg.get("role")

        if role == "assistant":
            # Flush any still-pending tool_calls from a previous assistant as
            # tombstones before starting a new tool-response zone.
            for tc_id in list(pending_tool_calls.keys()):
                result.append(_tombstone_tool_msg(tc_id))
            pending_tool_calls.clear()

            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tc_id = tc.get("id")
                    if tc_id:
                        pending_tool_calls[tc_id] = tc
            result.append(msg)

        elif role == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id and tc_id in pending_tool_calls:
                del pending_tool_calls[tc_id]
            result.append(msg)

        else:
            # Non-tool message ends the current tool-response zone. Flush
            # tombstones *before* this message so they remain contiguous with
            # the assistant tool_calls.
            for tc_id in list(pending_tool_calls.keys()):
                result.append(_tombstone_tool_msg(tc_id))
            pending_tool_calls.clear()
            result.append(msg)

    # End of conversation: flush any remaining pending tool_calls.
    for tc_id in list(pending_tool_calls.keys()):
        result.append(_tombstone_tool_msg(tc_id))

    return result


def _normalize_orphan_tool_messages(openai_messages):
    """Convert role="tool" messages that lack a matching assistant tool_calls
    into role="user" messages.  Preserves the original tool_call_id inside the
    content so no information is lost, and keeps the message ordering intact.

    This handles clients such as OpenCode that emit plain-text tool calls in
    assistant messages (e.g. "[Calling tool...") followed by one or more
    role="tool" results.  OpenAI/DeepSeek requires every role="tool" message
    to follow an assistant message that contains a matching tool_calls entry.
    When that precondition is violated, the entire orphaned tool-result run is
    converted into user messages so the conversation remains valid OpenAI
    format.

    Enhanced to cover:
    - assistant messages that contain some tool_calls but are followed by
      additional tool results whose ids are not in those tool_calls;
    - multiple consecutive orphan tool messages;
    - assistant text that explicitly mentions a tool_call_id.
    """
    known_tool_call_ids = set()
    mentioned_tool_call_ids = set()
    for msg in openai_messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id:
                    known_tool_call_ids.add(tc_id)
            # Also capture ids mentioned in plain-text assistant messages;
            # these indicate the assistant intended a tool call even though it
            # did not emit a structured tool_calls block.
            content = msg.get("content", "")
            if isinstance(content, str):
                mentioned_tool_call_ids.update(_extract_tool_call_ids_from_text(content))

    def _is_in_tool_response_zone(idx):
        """Return True if the message at idx sits in the tool-response zone
        immediately following an assistant message (i.e. walking backwards from
        idx we see only tool messages until we hit an assistant)."""
        for j in range(idx - 1, -1, -1):
            role = openai_messages[j].get("role")
            if role == "assistant":
                return True
            if role != "tool":
                return False
        return False

    # First pass: mark orphan tool messages that are part of a tool-response
    # zone following an assistant message.
    convert_to_user = [False] * len(openai_messages)
    for i, msg in enumerate(openai_messages):
        if msg.get("role") != "tool":
            continue
        tc_id = msg.get("tool_call_id")
        if not tc_id or tc_id in known_tool_call_ids:
            continue
        if _is_in_tool_response_zone(i):
            convert_to_user[i] = True

    # Second pass: build normalized messages.
    normalized = []
    for i, msg in enumerate(openai_messages):
        if convert_to_user[i]:
            tc_id = msg.get("tool_call_id", "")
            content = msg.get("content", "")
            prefix = f"[tool result for {tc_id}]:"
            if tc_id in mentioned_tool_call_ids:
                prefix = f"[tool result for {tc_id} (referenced in previous assistant message)]:"
            normalized.append({
                "role": "user",
                "content": f"{prefix}\n{content}",
            })
        else:
            normalized.append(msg)
    return normalized


def convert_openai_response_to_anthropic(openai_resp, anthropic_model):
    """Convert OpenAI response to Anthropic response format."""
    choice = openai_resp["choices"][0]
    msg = choice["message"]
    content_text = msg.get("content", "") or ""
    reasoning = msg.get("reasoning_content", "")

    # Qwen3.6 fix: if content is empty but reasoning exists, use reasoning as content
    if not content_text.strip() and reasoning:
        content_text = reasoning.strip()

    content = []
    existing_tool_calls = msg.get("tool_calls") or []
    synthesized = False

    # Content-text fallback for Qwen2.5-Coder: <tools>{...}</tools> in plain text.
    # Only fires when no structured tool_calls were returned (structured wins).
    extracted = _extract_content_tool_calls(content_text)
    if extracted["tools"] and not existing_tool_calls:
        if extracted["text"]:
            content.append({"type": "text", "text": extracted["text"]})
        for t in extracted["tools"]:
            content.append({
                "type": "tool_use",
                "id": f"call_{os.urandom(8).hex()}",
                "name": t["name"],
                "input": t["arguments"],
            })
        synthesized = True
    elif content_text:
        content.append({"type": "text", "text": content_text})

    # Handle structured tool_calls -> tool_use
    for tc in existing_tool_calls:
        if tc.get("type") == "function":
            func = tc["function"]
            tool_name = func.get("name", "")
            raw_args = func.get("arguments", "{}")
            input_data = parse_tool_arguments(raw_args, tool_name)
            # Ensure tool_call id is present (some backends omit it)
            tc_id = tc.get("id", "") or f"call_{os.urandom(8).hex()}"
            content.append({
                "type": "tool_use",
                "id": tc_id,
                "name": tool_name,
                "input": input_data,
            })

    stop_reason = choice.get("finish_reason", "stop")
    anthropic_stop_reason = "end_turn"
    if stop_reason == "tool_calls":
        anthropic_stop_reason = "tool_use"
    elif stop_reason == "length":
        anthropic_stop_reason = "max_tokens"
    elif stop_reason == "stop":
        anthropic_stop_reason = "end_turn"
    # Override when we synthesized tool_use from content fallback
    if synthesized and anthropic_stop_reason != "max_tokens":
        anthropic_stop_reason = "tool_use"

    return {
        "id": f"msg_{openai_resp['id'][:16]}",
        "type": "message",
        "role": "assistant",
        "model": anthropic_model,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_resp.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_resp.get("usage", {}).get("completion_tokens", 0),
        },
        "content": content,
        "stop_reason": anthropic_stop_reason,
    }


# ---------------------------------------------------------------------------
# Status page: system monitoring dashboard
# ---------------------------------------------------------------------------

__all__ = [
    "convert_anthropic_tools_to_openai",
    "convert_anthropic_tool_choice_to_openai",
    "convert_openai_tools_to_anthropic",
    "convert_openai_tool_choice_to_anthropic",
    "convert_openai_request_to_anthropic",
    "_estimate_message_chars",
    "_extract_text_from_messages",
    "_classify_content_for_ratio",
    "_estimate_tokens_dynamic",
    "_message_stable_hash",
    "_compute_common_prefix_ratio",
    "_compute_re_read_rate",
    "_has_thinking_content",
    "_strip_thinking_from_msg",
    "strip_old_thinking_blocks",
    "convert_anthropic_messages_to_openai",
    "convert_openai_response_to_anthropic",
]
