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
            openai_messages.append({
                "role": role,
                "content": str(content) if content else "",
            })

    return openai_messages


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
