#!/usr/bin/env python3
"""
Anthropic-to-OpenAI proxy for local llama-server.
Handles Qwen3.6 reasoning_content, streaming, and tool use correctly.
Includes XML->JSON fallback for Qwen tool calling quirks.
"""
import json
import os
import re
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime

LLAMA_BASE = os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8081/v1")
LLAMA_API_KEY = os.environ.get("LLAMA_API_KEY", "sk-1234")
# Backend type: "local" (llama-server/rapid-mlx) or "cloud" (DeepSeek/OpenAI)
BACKEND_TYPE = os.environ.get("BACKEND_TYPE", "")
if not BACKEND_TYPE:
    # Auto-detect from URL
    if "deepseek" in LLAMA_BASE.lower() or "openai" in LLAMA_BASE.lower() or "api." in LLAMA_BASE.lower():
        BACKEND_TYPE = "cloud"
    else:
        BACKEND_TYPE = "local"
IS_CLOUD = BACKEND_TYPE == "cloud"

# ---------------------------------------------------------------------------
# Concurrency control: backend-aware request serialization
# llama-server on Metal single-GPU suffers from time-slicing with -np > 1,
# so it defaults to 1 (serialized). rapid-mlx handles 2-4 concurrent requests
# well, so its config sets this higher.
# Cloud APIs handle concurrency natively, so we allow more.
# ---------------------------------------------------------------------------
PROXY_MAX_CONCURRENT = int(os.environ.get("PROXY_MAX_CONCURRENT", "4" if IS_CLOUD else "1"))
_llama_lock = threading.Semaphore(PROXY_MAX_CONCURRENT)
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-v4-pro" if IS_CLOUD else "mlx-community/Qwen3.6-35B-A3B-4bit")

# ---------------------------------------------------------------------------
# Tool-result clearing: proxy-side context management
# Defaults are tied to BACKEND_TYPE:
#   - Cloud APIs (DeepSeek/OpenAI): disabled by default (1M+ token context)
#   - Local backends (llama-server/rapid-mlx): enabled by default (limited VRAM)
# Override via env vars: PROXY_CLEAR_ENABLED, PROXY_CLEAR_THRESHOLD, PROXY_TOOL_KEEP
# ---------------------------------------------------------------------------
PROXY_CLEAR_ENABLED = os.environ.get("PROXY_CLEAR_ENABLED", "false" if IS_CLOUD else "true").lower() in ("1", "true", "yes")
PROXY_CLEAR_THRESHOLD = int(os.environ.get("PROXY_CLEAR_THRESHOLD", "30000" if IS_CLOUD else "15000"))  # chars, not tokens
PROXY_TOOL_KEEP = int(os.environ.get("PROXY_TOOL_KEEP", "10" if IS_CLOUD else "2"))  # keep last N tool_use/tool_result pairs
CONTENT_TOOLS_FALLBACK_ENABLED = os.environ.get("PROXY_CONTENT_TOOLS_FALLBACK", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Context-limit truncation: proxy-side message dropping when total context
# exceeds backend capacity. Complements tool-result clearing by dropping
# entire old messages (not just tool payloads).
# Defaults tied to BACKEND_TYPE (disabled for cloud, enabled for local).
# ---------------------------------------------------------------------------
PROXY_CTX_LIMIT_ENABLED = os.environ.get("PROXY_CTX_LIMIT_ENABLED", "false" if IS_CLOUD else "true").lower() in ("1", "true", "yes")
PROXY_CTX_CHARS_LIMIT = int(os.environ.get("PROXY_CTX_CHARS_LIMIT", "500000" if IS_CLOUD else "180000"))  # chars heuristic
PROXY_CTX_KEEP_HEAD = int(os.environ.get("PROXY_CTX_KEEP_HEAD", "2"))  # keep first N messages (system context + skills)
PROXY_CTX_KEEP_TAIL = int(os.environ.get("PROXY_CTX_KEEP_TAIL", "4"))  # keep last N messages
# ---------------------------------------------------------------------------
# Structured request logging: JSON Lines to logs/proxy_requests.jsonl
# ---------------------------------------------------------------------------
_LOG_DIR = os.path.join(_SCRIPT_DIR, "logs")
_JSONL_PATH = os.path.join(_LOG_DIR, "proxy_requests.jsonl")
_jsonl_lock = threading.Lock()
# Maps request token -> output_chars (set by response handlers)
_jsonl_output_map = {}
_jsonl_counter = 0


def _next_jsonl_token():
    """Generate a unique request token for correlating request log entries."""
    global _jsonl_counter
    _jsonl_counter += 1
    return f"req_{_jsonl_counter}_{os.urandom(4).hex()}"


def _ensure_jsonl_dir():
    """Create logs/ directory if it doesn't exist."""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
    except OSError:
        pass


def log_request(model: str, input_chars: int, output_chars: int,
                status: int, duration_ms: float):
    """Append one JSON Lines record to proxy_requests.jsonl (thread-safe)."""
    _ensure_jsonl_dir()
    record = {
        "timestamp": datetime.now().isoformat(),
        "method": "POST",
        "path": "/v1/messages",
        "model": model,
        "input_chars": input_chars,
        "output_chars": output_chars,
        "status": status,
        "duration_ms": round(duration_ms, 1),
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _jsonl_lock:
            with open(_JSONL_PATH, "a") as f:
                f.write(line)
    except OSError:
        pass


MODEL_ALIASES = [
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-7",
    "default",
    MODEL_NAME,
]

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    log_path = os.environ.get("PROXY_LOG_PATH", "/tmp/anthropic_proxy.log")
    try:
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# XML -> JSON fallback for Qwen3.5/3.6 tool calling
# llama.cpp issue #21495: model occasionally emits XML instead of JSON
# ---------------------------------------------------------------------------

def _extract_xml_params(raw: str) -> dict:
    """Extract parameters from Qwen XML-style tool calls."""
    result = {}
    # Pattern 1: <parameter=key>value</parameter>
    for m in re.finditer(r'<parameter=(\w+)>([^<]*)</parameter>', raw, re.DOTALL):
        result[m.group(1)] = m.group(2).strip()
    # Pattern 2: <param name="key">value</param>
    for m in re.finditer(r'<param\s+name="(\w+)">([^<]*)</param>', raw, re.DOTALL):
        result[m.group(1)] = m.group(2).strip()
    # Pattern 3: <key>value</key> inside a tool block
    for m in re.finditer(r'<(\w+)>([^<]+)</\1>', raw, re.DOTALL):
        k = m.group(1)
        if k in ("function", "tool_call", "name", "arguments", "parameter"):
            continue
        result[k] = m.group(2).strip()
    return result


def _extract_xml_tool_name(raw: str) -> str:
    """Try to extract function name from XML-style output."""
    # <function=func_name>...
    m = re.search(r'<function=(\w+)>', raw)
    if m:
        return m.group(1)
    # <tool_call><name>func_name</name>
    m = re.search(r'<(?:tool_call|function)[^>]*>.*?<name>(\w+)</name>', raw, re.DOTALL)
    if m:
        return m.group(1)
    return ""


def parse_tool_arguments(raw: str, tool_name_hint: str = "") -> dict:
    """
    Parse tool arguments from backend response.
    Falls back from JSON -> XML extraction -> empty dict.
    """
    raw = raw.strip() if raw else ""
    if not raw:
        return {}

    # 1. Try standard JSON
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except json.JSONDecodeError:
        pass

    # 2. Try to find a JSON object embedded inside the text
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        try:
            parsed = json.loads(raw[brace_start:brace_end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 3. XML fallback
    xml_params = _extract_xml_params(raw)
    if xml_params:
        log(f"  [XML_FALLBACK] extracted {len(xml_params)} params from XML for tool={tool_name_hint}")
        return xml_params

    # 4. Last resort: treat the whole string as a single "command" or "query" param
    # based on common tool patterns
    if tool_name_hint in ("exec", "bash", "shell"):
        return {"command": raw.strip("`\n ")}
    if tool_name_hint in ("read", "view", "file"):
        return {"file_path": raw.strip("`\n ")}

    log(f"  [XML_FALLBACK] failed to parse args for tool={tool_name_hint}, raw={raw[:200]!r}")
    return {}


# ---------------------------------------------------------------------------
# Content-text tool-call fallback for Qwen2.5-Coder
# The model emits <tools>\n{"name":"X","arguments":{...}}\n</tools> as plain
# content text instead of populating the OpenAI tool_calls array. This helper
# (and the streaming state-machine class below) extract those blocks and
# synthesize standard Anthropic tool_use blocks. Gated by env var so it can
# be disabled for models where <tools> might be legitimate prose.
# ---------------------------------------------------------------------------
TOOLS_TRIGGER = "<tools>"
TOOLS_END_TAG = "</tools>"


def _parse_tools_block_body(body):
    """Parse the JSON body inside a <tools>...</tools> block.
    Returns {"name": str, "arguments": dict} on success, None on failure."""
    try:
        obj = json.loads(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            return None
        args = obj.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        return {"name": obj["name"], "arguments": args}
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _extract_content_tool_calls(text):
    """Scan plain content text for <tools>...</tools> blocks (Qwen2.5-Coder).
    Returns {"text": <cleaned text>, "tools": [{"name", "arguments"}, ...]}.
    Failed/malformed blocks are left verbatim in the cleaned text so the
    user doesn't lose visible output.
    Tries find() first (handles adjacent blocks); on parse failure retries
    with rfind() so literal </tools> substrings inside JSON args also work."""
    if not CONTENT_TOOLS_FALLBACK_ENABLED or not text:
        return {"text": text or "", "tools": []}
    parts = []
    tools = []
    i = 0
    while i < len(text):
        open_i = text.find(TOOLS_TRIGGER, i)
        if open_i < 0:
            parts.append(text[i:])
            break
        parts.append(text[i:open_i])
        body_start = open_i + len(TOOLS_TRIGGER)
        first_close = text.find(TOOLS_END_TAG, body_start)
        if first_close < 0:
            # No closing tag at all — preserve raw from here on
            parts.append(text[open_i:])
            break
        # Try first occurrence; this is correct for adjacent blocks.
        body = text[body_start:first_close].strip()
        parsed = _parse_tools_block_body(body)
        close_i = first_close
        if parsed is None:
            # Retry with rfind in case arguments JSON contained "</tools>".
            last_close = text.rfind(TOOLS_END_TAG, body_start)
            if last_close > first_close:
                body2 = text[body_start:last_close].strip()
                parsed2 = _parse_tools_block_body(body2)
                if parsed2 is not None:
                    parsed = parsed2
                    body = body2
                    close_i = last_close
        if parsed is None:
            log(f"  [CONTENT_TOOLS_FALLBACK] parse failed for body[:80]={body[:80]!r}")
            parts.append(text[open_i:close_i + len(TOOLS_END_TAG)])
        else:
            tools.append(parsed)
            log(f"  [CONTENT_TOOLS_FALLBACK] extracted tool={parsed['name']}")
        i = close_i + len(TOOLS_END_TAG)
    return {"text": "".join(parts).strip(), "tools": tools}


class _StreamingToolsExtractor:
    """State machine for stripping <tools>...</tools> blocks from a token stream.

    Usage:
        ext = _StreamingToolsExtractor()
        for chunk in incoming_deltas:
            for kind, value in ext.feed(chunk):
                ...  # kind in {"text", "tool"}; value is str or {"name","arguments"}
        for kind, value in ext.finalize():
            ...

    Holds back the last few characters of each delta only if they could still
    extend to the opening trigger (max len(TRIGGER)-1 = 6 chars).
    Disabled (passthrough) when CONTENT_TOOLS_FALLBACK_ENABLED is false.
    """

    def __init__(self):
        self.pending_text = ""        # buffered text that may include partial <tools> prefix
        self.in_tools_block = False   # True between <tools> and </tools>
        self.tools_content_buf = ""   # accumulated body of current <tools> block
        self._enabled = CONTENT_TOOLS_FALLBACK_ENABLED

    def feed(self, incoming):
        if not incoming:
            return []
        if not self._enabled:
            return [("text", incoming)]
        events = []
        text = self.pending_text + incoming
        self.pending_text = ""

        while text:
            if self.in_tools_block:
                self.tools_content_buf += text
                text = ""
                # Prefer first occurrence; if parse fails we retry with rfind below.
                end_idx = self.tools_content_buf.find(TOOLS_END_TAG)
                if end_idx < 0:
                    break  # need more deltas
                body = self.tools_content_buf[:end_idx].strip()
                remainder = self.tools_content_buf[end_idx + len(TOOLS_END_TAG):]
                parsed = _parse_tools_block_body(body)
                if parsed is None:
                    # Retry with rfind in case arguments JSON contained "</tools>"
                    last_end = self.tools_content_buf.rfind(TOOLS_END_TAG)
                    if last_end > end_idx:
                        body2 = self.tools_content_buf[:last_end].strip()
                        remainder2 = self.tools_content_buf[last_end + len(TOOLS_END_TAG):]
                        parsed2 = _parse_tools_block_body(body2)
                        if parsed2 is not None:
                            parsed = parsed2
                            body = body2
                            remainder = remainder2
                if parsed is not None:
                    events.append(("tool", parsed))
                    log(f"  [CONTENT_TOOLS_FALLBACK] streamed tool={parsed['name']}")
                else:
                    log(f"  [CONTENT_TOOLS_FALLBACK] streamed parse failed for body[:80]={body[:80]!r}")
                    events.append(("text", TOOLS_TRIGGER + body + TOOLS_END_TAG))
                self.in_tools_block = False
                self.tools_content_buf = ""
                text = remainder
                continue

            trigger_idx = text.find(TOOLS_TRIGGER)
            if trigger_idx >= 0:
                if trigger_idx > 0:
                    events.append(("text", text[:trigger_idx]))
                self.in_tools_block = True
                text = text[trigger_idx + len(TOOLS_TRIGGER):]
                continue

            # No trigger in this batch — hold back any suffix that could still
            # extend to <tools>, emit the rest.
            holdback = 0
            for h in range(1, min(len(TOOLS_TRIGGER), len(text)) + 1):
                if text[-h:] == TOOLS_TRIGGER[:h]:
                    holdback = h
            if holdback > 0 and len(text) > holdback:
                events.append(("text", text[:-holdback]))
                self.pending_text = text[-holdback:]
            elif holdback > 0:
                self.pending_text = text
            else:
                events.append(("text", text))
            break

        return events

    def finalize(self):
        """Emit any unflushed state. Call once after the stream ends."""
        events = []
        if not self._enabled:
            return events
        if self.pending_text:
            events.append(("text", self.pending_text))
            self.pending_text = ""
        if self.in_tools_block:
            # No closing </tools> ever arrived — preserve buffered content as text.
            log("  [CONTENT_TOOLS_FALLBACK] EOF inside <tools>, emitting raw")
            events.append(("text", TOOLS_TRIGGER + self.tools_content_buf))
            self.in_tools_block = False
            self.tools_content_buf = ""
        return events


def convert_anthropic_tools_to_openai(tools):
    """Convert Anthropic tool format to OpenAI tool format."""
    if not tools:
        return None
    openai_tools = []
    for tool in tools:
        if tool.get("type") == "custom":
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
        else:
            total += len(str(content))
    return total


def clear_old_tool_results(messages):
    """
    Proxy-side tool-result clearing.
    Replaces old tool_result contents with a placeholder, keeping the most
    recent PROXY_TOOL_KEEP pairs intact so the model still knows the calls
    happened but doesn't pay to carry the full payloads forward.
    Operates on Anthropic-format messages in-place.
    Returns (messages, stats_dict).
    """
    if not PROXY_CLEAR_ENABLED:
        return messages, {"enabled": False}

    total_chars = _estimate_message_chars(messages)
    if total_chars < PROXY_CLEAR_THRESHOLD:
        return messages, {
            "enabled": True,
            "skipped": True,
            "reason": "below_threshold",
            "chars": total_chars,
        }

    # Locate every tool_result block: (message_index, block_index)
    tool_result_indices = []
    for msg_idx, msg in enumerate(messages):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block_idx, block in enumerate(content):
                if block.get("type") == "tool_result":
                    tool_result_indices.append((msg_idx, block_idx))

    if len(tool_result_indices) <= PROXY_TOOL_KEEP:
        return messages, {
            "enabled": True,
            "skipped": True,
            "reason": "few_tool_results",
            "count": len(tool_result_indices),
        }

    # Keep the newest PROXY_TOOL_KEEP; clear the rest
    keep_set = set(tool_result_indices[-PROXY_TOOL_KEEP:])
    cleared_count = 0
    cleared_chars = 0
    for msg_idx, block_idx in tool_result_indices:
        if (msg_idx, block_idx) in keep_set:
            continue
        block = messages[msg_idx]["content"][block_idx]
        original = block.get("content", "")
        original_len = len(str(original)) if original else 0
        block["content"] = f"[cleared to save context: {original_len} chars]"
        cleared_count += 1
        cleared_chars += original_len

    return messages, {
        "enabled": True,
        "cleared": True,
        "cleared_tool_results": cleared_count,
        "cleared_chars": cleared_chars,
        "kept": PROXY_TOOL_KEEP,
        "total_chars_before": total_chars,
    }


def truncate_messages_if_needed(messages):
    """
    Proxy-side message truncation when total context exceeds backend capacity.
    Drops entire old messages (starting after the head-keep window) until the
    rough character count falls below PROXY_CTX_CHARS_LIMIT.
    Preserves the first PROXY_CTX_KEEP_HEAD messages (usually system context +
    skills) and the last PROXY_CTX_KEEP_TAIL messages (recent conversation).
    Operates on Anthropic-format messages in-place.
    Returns (messages, stats_dict).
    """
    if not PROXY_CTX_LIMIT_ENABLED:
        return messages, {"enabled": False}

    total_chars = _estimate_message_chars(messages)
    if total_chars < PROXY_CTX_CHARS_LIMIT:
        return messages, {
            "enabled": True,
            "skipped": True,
            "reason": "below_limit",
            "chars": total_chars,
        }

    n = len(messages)
    if n <= PROXY_CTX_KEEP_HEAD + PROXY_CTX_KEEP_TAIL:
        # Not enough messages to safely drop anything
        return messages, {
            "enabled": True,
            "skipped": True,
            "reason": "too_few_messages",
            "count": n,
            "chars": total_chars,
        }

    # Indices we must preserve: head [0, keep_head) + tail [n-keep_tail, n)
    preserved = set(range(PROXY_CTX_KEEP_HEAD))
    preserved.update(range(n - PROXY_CTX_KEEP_TAIL, n))

    # Build a list of deletable indices in order (oldest first)
    deletable = [i for i in range(PROXY_CTX_KEEP_HEAD, n - PROXY_CTX_KEEP_TAIL)]

    dropped_count = 0
    dropped_chars = 0
    current_chars = total_chars

    for idx in deletable:
        if idx in preserved:
            continue
        msg = messages[idx]
        msg_chars = _estimate_message_chars([msg])
        current_chars -= msg_chars
        dropped_count += 1
        dropped_chars += msg_chars
        # Mark as dropped (replace with lightweight sentinel)
        messages[idx] = None
        if current_chars < PROXY_CTX_CHARS_LIMIT:
            break

    # Compact the list: remove None entries
    messages = [m for m in messages if m is not None]

    return messages, {
        "enabled": True,
        "truncated": True,
        "dropped_messages": dropped_count,
        "dropped_chars": dropped_chars,
        "chars_before": total_chars,
        "chars_after": current_chars,
        "limit": PROXY_CTX_CHARS_LIMIT,
    }


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
                # Add text message first if there's text
                if text_parts:
                    openai_messages.append({
                        "role": "user",
                        "content": "\n".join(text_parts),
                    })
                # Then add tool results
                for tr in tool_results:
                    tr_content = tr["content"]
                    if tr_content is None:
                        tr_content = ""
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": str(tr_content),
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
    existing_tool_calls = msg.get("tool_calls", [])
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
import subprocess
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_SCRIPT_DIR, "logs", "llama-server.log")
_PID_PATH = os.path.join(_SCRIPT_DIR, "llama-server.pid")


def _run(cmd, timeout=3):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=timeout).strip()
    except Exception:
        return ""


def _get_process_info(pattern, name, fallback_port=None):
    """Return dict with pid, rss_mb, cpu, elapsed for a process matching pattern."""
    # Try pgrep first
    pid = _run(f"pgrep -f '{pattern}' | head -1")
    # Fallback: detect by listening port (for proxy itself)
    # Use -sTCP:LISTEN to only match the listening process, not client connections
    if not pid and fallback_port:
        pid = _run(f"lsof -i :{fallback_port} -sTCP:LISTEN -t | head -1")
    if not pid:
        return {"running": False, "name": name}
    info = _run(f"ps -o pid=,rss=,pcpu=,etime= -p {pid}")
    parts = info.split()
    if len(parts) >= 4:
        rss_kb = int(parts[1])
        return {
            "running": True,
            "name": name,
            "pid": parts[0],
            "rss_mb": f"{rss_kb / 1024:.1f}",
            "cpu": parts[2],
            "elapsed": parts[3],
        }
    return {"running": False, "name": name}


def _get_system_memory():
    out = _run("vm_stat")
    data = {}
    page_size = 16384
    for line in out.splitlines():
        if "Pages free:" in line:
            data["free_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages wired down:" in line:
            data["wired_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages active:" in line:
            data["active_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages inactive:" in line:
            data["inactive_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages stored in compressor:" in line:
            data["compress_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
    total = 48.0
    used = total - data.get("free_gb", 0)
    data["total_gb"] = total
    data["used_gb"] = used
    data["used_pct"] = f"{used/total*100:.1f}"
    return data


def _read_log_tail(path, max_bytes=200000):
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", errors="ignore") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes), 0)
            return f.read()
    except OSError:
        return ""


def _get_log_stats():
    """Count recent OOMs, forced cache clears, and requests from log tail.
    Requests get accurate timestamps from proxy logs [REQ_SUMMARY].
    OOM/CacheClear have no timestamp (backend logs don't include wall-clock time).
    For cloud backends, OOM/cache-clear metrics are not available."""
    backend_tail = _read_log_tail(_LOG_PATH, 200000) if not IS_CLOUD else ""
    proxy_log_path = os.environ.get("PROXY_LOG_PATH", "/tmp/anthropic_proxy.log")
    proxy_tail = _read_log_tail(proxy_log_path, 100000)

    # --- Extract request events from proxy logs ([HH:MM:SS] [REQ_SUMMARY] chars=X tools=Y) ---
    proxy_req_events = []
    for line in proxy_tail.splitlines()[-40:]:
        m = re.search(r'\[(\d{2}:\d{2}:\d{2})\].*\[REQ_SUMMARY\].*chars=(\d+).*tools=(\d+)', line)
        if m:
            proxy_req_events.append((m.group(1), m.group(2), m.group(3)))

    # --- Build recent events list ---
    events = []
    req_idx = 0
    if not IS_CLOUD:
        for line in backend_tail.splitlines()[-40:]:
            if "Insufficient Memory" in line:
                events.append(("—", "🔴 OOM", line.split(":")[-1].strip()[-80:]))
            elif "forced cache clear" in line:
                events.append(("—", "🟡 CacheClear", line.split(":")[-1].strip()[-80:]))
            elif "[REQUEST]" in line and "total_chars=" in line:
                m = re.search(r"total_chars=(\d+).*?tools=(\d+)", line)
                if m:
                    ts = proxy_req_events[req_idx][0] if req_idx < len(proxy_req_events) else "—"
                    events.append((ts, "📨 Request", f"{m.group(1)} chars, {m.group(2)} tools"))
                    req_idx += 1
    events = events[-12:]

    # --- Detailed lists for modal popup ---
    if IS_CLOUD:
        oom_details = []
        clear_details = []
    else:
        oom_details = [("—", line.split(":")[-1].strip()[-120:])
                       for line in backend_tail.splitlines() if "Insufficient Memory" in line]
        clear_details = [("—", line.split(":")[-1].strip()[-120:])
                         for line in backend_tail.splitlines() if "forced cache clear" in line]
    req_details = [(ts, f"{chars} chars, {tools} tools") for ts, chars, tools in proxy_req_events]

    return {
        "ooms": len(oom_details),
        "clears": len(clear_details),
        "requests": len(req_details),
        "last_events": events,
        "oom_details": oom_details[-20:],
        "clear_details": clear_details[-20:],
        "req_details": req_details[-20:],
    }


def _build_status_html():
    backend_info = _get_process_info("rapid-mlx|llama-server", "Backend")
    proxy_info = _get_process_info("anthropic_proxy.py", "Proxy", fallback_port=4000)
    mem = _get_system_memory()
    log = _get_log_stats()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    backend_color = "#2ecc71" if backend_info.get("running") else "#e74c3c"
    proxy_color = "#2ecc71" if proxy_info.get("running") else "#e74c3c"
    mem_warn = float(mem.get("used_pct", 0)) > 90
    mem_color = "#e74c3c" if mem_warn else "#2ecc71"

    # Cloud-backend status card (no PID/memory/uptime)
    if IS_CLOUD:
        backend_card = f"""<div class="card">
    <h2>Backend</h2>
    <div class="row"><span class="label">Type</span><span class="value">Cloud API ({BACKEND_TYPE})</span></div>
    <div class="row"><span class="label">Endpoint</span><span class="value">{LLAMA_BASE}</span></div>
    <div class="row"><span class="label">Model</span><span class="value">{MODEL_NAME}</span></div>
    <div class="row"><span class="label">API Key</span><span class="value">{LLAMA_API_KEY[:8]}****</span></div>
  </div>"""
    else:
        backend_card = f"""<div class="card">
    <h2>Backend</h2>
    <div class="row"><span class="label">Status</span><span class="value"><span class="status-dot" style="background:{backend_color}"></span>{"Running" if backend_info.get("running") else "Stopped"}</span></div>
    <div class="row"><span class="label">Name</span><span class="value">{backend_info.get("name", "N/A")}</span></div>
    <div class="row"><span class="label">PID</span><span class="value">{backend_info.get("pid", "N/A")}</span></div>
    <div class="row"><span class="label">Memory</span><span class="value">{backend_info.get("rss_mb", "N/A")} MB</span></div>
    <div class="row"><span class="label">CPU</span><span class="value">{backend_info.get("cpu", "N/A")}%</span></div>
    <div class="row"><span class="label">Uptime</span><span class="value">{backend_info.get("elapsed", "N/A")}</span></div>
  </div>"""

    # Conditional log-stat rows (avoid backslashes inside f-strings)
    oom_row = ""
    cache_row = ""
    if not IS_CLOUD:
        oom_row = '<div class="row"><span class="label">OOM Crashes</span><span class="value oom clickable" onclick="showModal(' + "'oom', '🔴 OOM Crashes Detail')" + f'">{log["ooms"]}</span></div>'
        cache_row = '<div class="row"><span class="label">Forced Cache Clear</span><span class="value clear clickable" onclick="showModal(' + "'clear', '🟡 Forced Cache Clear Detail')" + f'">{log["clears"]}</span></div>'

    events_html = ""
    for ts, evt_type, evt_msg in log["last_events"]:
        ts_display = f'<span class="evt-ts">{ts}</span>' if ts != "—" else '<span class="evt-ts" style="color:#666">—</span>'
        events_html += f'<div class="evt">{ts_display} <span class="evt-tag">{evt_type}</span> {evt_msg}</div>'
    if not events_html:
        events_html = '<div class="evt">No recent events</div>'

    # JSON data for modal popups
    import json as _json
    modal_data = _json.dumps({
        "oom": log.get("oom_details", []),
        "clear": log.get("clear_details", []),
        "request": log.get("req_details", []),
    })

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>Local LLM Stack Status</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .ts {{ color: #888; font-size: 12px; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
  .card {{ background: #16213e; border-radius: 10px; padding: 16px; }}
  .card h2 {{ font-size: 14px; margin: 0 0 12px 0; color: #a0a0c0; text-transform: uppercase; letter-spacing: 1px; }}
  .row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #2a2a4a; font-size: 13px; }}
  .row:last-child {{ border-bottom: none; }}
  .label {{ color: #888; }}
  .value {{ font-weight: 600; }}
  .status-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
  .mem-bar {{ height: 10px; background: #2a2a4a; border-radius: 5px; margin-top: 8px; overflow: hidden; }}
  .mem-fill {{ height: 100%; border-radius: 5px; transition: width 0.5s; }}
  .evt {{ font-size: 12px; padding: 4px 0; border-bottom: 1px solid #2a2a4a; color: #ccc; }}
  .evt:last-child {{ border-bottom: none; }}
  .evt-tag {{ display: inline-block; min-width: 80px; font-weight: 600; font-size: 11px; }}
  .evt-ts {{ display: inline-block; min-width: 60px; font-family: monospace; font-size: 11px; color: #888; margin-right: 4px; }}
  .oom {{ color: #e74c3c; }}
  .clear {{ color: #f39c12; }}
  .req {{ color: #3498db; }}
  .clickable {{ cursor: pointer; text-decoration: underline; }}
  .clickable:hover {{ opacity: 0.8; }}
  .footer {{ margin-top: 20px; font-size: 11px; color: #666; text-align: center; }}
  .modal {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.75); z-index: 100; justify-content: center; align-items: center; }}
  .modal-content {{ background: #16213e; border-radius: 10px; padding: 20px; max-width: 800px; width: 90%; max-height: 80vh; overflow-y: auto; border: 1px solid #2a2a4a; }}
  .close-btn {{ float: right; font-size: 24px; cursor: pointer; color: #888; line-height: 1; }}
  .close-btn:hover {{ color: #fff; }}
  .modal-row {{ padding: 8px 0; border-bottom: 1px solid #2a2a4a; font-size: 12px; color: #ccc; display: flex; gap: 12px; }}
  .modal-row:last-child {{ border-bottom: none; }}
  .modal-time {{ color: #888; font-family: monospace; min-width: 60px; flex-shrink: 0; }}
  .modal-msg {{ word-break: break-word; }}
</style>
</head>
<body>
<h1>🖥️ Local LLM Stack Status</h1>
<div class="ts">Updated: {now} &nbsp;•&nbsp; Auto-refresh every 5s</div>

<div class="grid">
  {backend_card}

  <div class="card">
    <h2>Proxy</h2>
    <div class="row"><span class="label">Status</span><span class="value"><span class="status-dot" style="background:{proxy_color}"></span>{"Running" if proxy_info.get("running") else "Stopped"}</span></div>
    <div class="row"><span class="label">Name</span><span class="value">{proxy_info.get("name", "N/A")}</span></div>
    <div class="row"><span class="label">PID</span><span class="value">{proxy_info.get("pid", "N/A")}</span></div>
    <div class="row"><span class="label">Memory</span><span class="value">{proxy_info.get("rss_mb", "N/A")} MB</span></div>
    <div class="row"><span class="label">Listen</span><span class="value">127.0.0.1:4000</span></div>
    <div class="row"><span class="label">Backend</span><span class="value">{LLAMA_BASE}</span></div>
  </div>

  <div class="card">
    <h2>System Memory</h2>
    <div class="row"><span class="label">Total</span><span class="value">{mem.get("total_gb", 48):.0f} GB</span></div>
    <div class="row"><span class="label">Used</span><span class="value" style="color:{mem_color}">{mem.get("used_gb", 0):.1f} GB ({mem.get("used_pct", "0")}%)</span></div>
    <div class="row"><span class="label">Free</span><span class="value">{mem.get("free_gb", 0):.2f} GB</span></div>
    <div class="row"><span class="label">Wired</span><span class="value">{mem.get("wired_gb", 0):.1f} GB</span></div>
    <div class="row"><span class="label">Active</span><span class="value">{mem.get("active_gb", 0):.1f} GB</span></div>
    <div class="row"><span class="label">Inactive</span><span class="value">{mem.get("inactive_gb", 0):.1f} GB</span></div>
    <div class="row"><span class="label">Compressed</span><span class="value">{mem.get("compress_gb", 0):.1f} GB</span></div>
    <div class="mem-bar"><div class="mem-fill" style="width:{mem.get("used_pct", 0)}%;background:{mem_color}"></div></div>
  </div>

  <div class="card">
    <h2>Log Stats (recent tail)</h2>
    {oom_row}
    {cache_row}
    <div class="row"><span class="label">Requests</span><span class="value req clickable" onclick="showModal('request', '📨 Requests Detail')">{log["requests"]}</span></div>
    <div class="row"><span class="label">Config</span><span class="value">CLEAR={'on' if PROXY_CLEAR_ENABLED else 'off'}, LIMIT={'on' if PROXY_CTX_LIMIT_ENABLED else 'off'}, MAX_CONCURRENT={PROXY_MAX_CONCURRENT}</span></div>
    <div class="row"><span class="label">Model</span><span class="value">{MODEL_NAME}</span></div>
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>Recent Events</h2>
    {events_html}
  </div>
</div>

<div class="footer">Open http://127.0.0.1:4000/status in your browser</div>

<!-- Modal -->
<div id="modal" class="modal" onclick="closeModal(event)">
  <div class="modal-content" onclick="event.stopPropagation()">
    <span class="close-btn" onclick="closeModal()">&times;</span>
    <h3 id="modal-title" style="margin-top:0;color:#a0a0c0;font-size:14px;text-transform:uppercase;letter-spacing:1px;">Detail</h3>
    <div id="modal-body"></div>
  </div>
</div>

<script>
var eventData = {modal_data};
function showModal(type, title) {{
  document.getElementById('modal-title').innerText = title;
  var body = document.getElementById('modal-body');
  body.innerHTML = '';
  var items = eventData[type] || [];
  if (items.length === 0) {{
    body.innerHTML = '<div class="modal-row">No events found</div>';
  }} else {{
    items.forEach(function(item) {{
      var row = document.createElement('div');
      row.className = 'modal-row';
      var ts = item[0] || '—';
      var msg = item[1] || '';
      row.innerHTML = '<span class="modal-time">' + ts + '</span><span class="modal-msg">' + msg + '</span>';
      body.appendChild(row);
    }});
  }}
  document.getElementById('modal').style.display = 'flex';
}}
function closeModal(e) {{
  if (!e || e.target.id === 'modal') {{
    document.getElementById('modal').style.display = 'none';
  }}
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') closeModal();
}});
</script>
</body>
</html>"""
    return html


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        log(f"GET {self.path}")
        log(f"  Headers: {dict(self.headers)}")
        if self.path == "/v1/models":
            models = [{"id": name, "object": "model", "created": 1677610602, "owned_by": "anthropic"}
                      for name in MODEL_ALIASES]
            self._respond_json({"object": "list", "data": models})
        elif self.path == "/status":
            html = _build_status_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        else:
            self._respond_json({"detail": "Not found"}, 404)

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")
        log(f"POST {self.path}")
        log(f"  Headers: {dict(self.headers)}")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            log(f"  Body (invalid JSON): {body[:500]}")
            self._respond_json(
                {"error": {"type": "invalid_request_error", "message": "Invalid JSON"}},
                400,
            )
            return

        try:
            with open("/tmp/anthropic_request_body.json", "w") as f:
                f.write(json.dumps(parsed, ensure_ascii=False, indent=2))
        except OSError as e:
            log(f"  debug body write failed: {e}")
        log(f"  Body: {json.dumps(parsed, ensure_ascii=False)[:1500]}")

        if self.path == "/v1/messages" or self.path.startswith("/v1/messages?"):
            # Log request summary with timestamp for status page tracking
            msgs = parsed.get("messages", [])
            total_chars = len(json.dumps(msgs, ensure_ascii=False)) if msgs else 0
            tools = parsed.get("tools", [])
            log(f"[REQ_SUMMARY] chars={total_chars} tools={len(tools)}")
            # Timing wrapper for structured logging
            import time as _time
            _t0 = _time.monotonic()
            try:
                self._handle_messages(parsed)
                # Non-streaming: log after response is sent
                _dur = (_time.monotonic() - _t0) * 1000
                log_request(
                    model=parsed.get("model", "unknown"),
                    input_chars=total_chars,
                    output_chars=0,  # filled by _handle_non_streaming_response
                    status=200,
                    duration_ms=_dur,
                )
            except Exception as e:
                _dur = (_time.monotonic() - _t0) * 1000
                log(f"  -> Error: {e}")
                log_request(
                    model=parsed.get("model", "unknown"),
                    input_chars=total_chars,
                    output_chars=0,
                    status=500,
                    duration_ms=_dur,
                )
                raise
        else:
            log(f"  -> 404 (unknown path)")
            self._respond_json({"detail": "Not found"}, 404)

    def _handle_messages(self, body):
        is_stream = body.get("stream", False)
        model = body.get("model", "unknown")
        log(f"  -> Handling model={model}, stream={is_stream}")

        # Proxy-side tool-result clearing
        raw_messages = body.get("messages", [])
        raw_messages, clear_stats = clear_old_tool_results(raw_messages)
        if clear_stats.get("cleared"):
            log(f"  -> Tool clearing: {clear_stats['cleared_tool_results']} tool_results cleared, {clear_stats['cleared_chars']:,} chars freed (kept last {clear_stats['kept']})")
        elif not clear_stats.get("enabled"):
            log(f"  -> Tool clearing: disabled ({BACKEND_TYPE} backend)")
        elif clear_stats.get("enabled") and not clear_stats.get("skipped"):
            log(f"  -> Tool clearing: active (threshold={PROXY_CLEAR_THRESHOLD}, keep={PROXY_TOOL_KEEP})")

        # Normalize system-reminder date to stabilize prefix for KV cache hits
        if raw_messages and raw_messages[0].get("role") == "user":
            content = raw_messages[0].get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        # Replace dynamic date with fixed placeholder to stabilize prefix
                        new_text = re.sub(r"Today's date is \d{4}/\d{2}/\d{2}\.", "Today's date is DATE_PLACEHOLDER.", text)
                        if new_text != text:
                            block["text"] = new_text
                            log(f"  -> Standardized date in msg0 block")
            else:
                new_content = re.sub(r"Today's date is \d{4}/\d{2}/\d{2}\.", "Today's date is DATE_PLACEHOLDER.", str(content))
                if new_content != content:
                    raw_messages[0]["content"] = new_content
                    log(f"  -> Standardized date in msg0")

        # Proxy-side message truncation when total context exceeds backend limit
        raw_messages, trunc_stats = truncate_messages_if_needed(raw_messages)
        if trunc_stats.get("truncated"):
            log(f"  -> Context truncation: {trunc_stats['dropped_messages']} messages dropped, {trunc_stats['dropped_chars']:,} chars removed ({trunc_stats['chars_before']:,} -> {trunc_stats['chars_after']:,})")
        elif not trunc_stats.get("enabled"):
            log(f"  -> Context truncation: disabled ({BACKEND_TYPE} backend)")

        # Debug: compute hashes of first two messages to diagnose cache misses
        if raw_messages:
            def _msg_hash(m):
                import hashlib
                c = m.get("content", "")
                if isinstance(c, list):
                    c = "".join(b.get("text", "") for b in c if b.get("type") == "text")
                elif not isinstance(c, str):
                    c = str(c)
                return hashlib.md5((m.get("role", "") + ":" + c).encode()).hexdigest()[:8]
            h0 = _msg_hash(raw_messages[0]) if len(raw_messages) > 0 else "none"
            h1 = _msg_hash(raw_messages[1]) if len(raw_messages) > 1 else "none"
            log(f"  -> Msg hashes: msg0={h0}, msg1={h1}, total_msgs={len(raw_messages)}")

        if trunc_stats.get("enabled") and not trunc_stats.get("skipped") and not trunc_stats.get("truncated"):
            log(f"  -> Context truncation: active (limit={PROXY_CTX_CHARS_LIMIT:,} chars, keep_head={PROXY_CTX_KEEP_HEAD}, keep_tail={PROXY_CTX_KEEP_TAIL})")

        # Convert messages
        messages = convert_anthropic_messages_to_openai(raw_messages)

        # Handle system prompt
        system_msg = body.get("system")
        if system_msg:
            if isinstance(system_msg, list):
                system_text = "\n".join([b.get("text", "") for b in system_msg if b.get("type") == "text"])
            else:
                system_text = str(system_msg)
            if system_text.strip():
                messages = [{"role": "system", "content": system_text}] + messages

        openai_body = {
            "model": MODEL_NAME,
            "messages": messages,
            "max_tokens": body.get("max_tokens", 4096),
            "temperature": body.get("temperature", 0.7),
            "stream": is_stream,
        }
        if "top_p" in body:
            openai_body["top_p"] = body["top_p"]
        if "stop_sequences" in body:
            openai_body["stop"] = body["stop_sequences"]

        # Handle tools
        tools = convert_anthropic_tools_to_openai(body.get("tools"))
        if tools:
            openai_body["tools"] = tools
            log(f"  -> Tools: {[t['function']['name'] for t in tools]}")

        tool_choice = convert_anthropic_tool_choice_to_openai(body.get("tool_choice"))
        if tool_choice:
            openai_body["tool_choice"] = tool_choice

        log(f"  -> Forwarding to {LLAMA_BASE}/chat/completions")
        try:
            with _llama_lock:
                req = urllib.request.Request(
                    f"{LLAMA_BASE}/chat/completions",
                    data=json.dumps(openai_body).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLAMA_API_KEY}"},
                    method="POST"
                )
                resp = urllib.request.urlopen(req, timeout=300)
                log(f"  <- llama-server status: {resp.status}")
                if is_stream:
                    self._handle_streaming_response(resp, body)
                else:
                    self._handle_non_streaming_response(resp, body)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8")
            log(f"  <- llama-server error: {e.code} - {err[:500]}")
            self._respond_json({"error": {"message": err}}, e.code)
            return

    def _handle_non_streaming_response(self, resp, anthropic_body):
        openai_resp = json.loads(resp.read().decode("utf-8"))
        anthropic_resp = convert_openai_response_to_anthropic(
            openai_resp,
            anthropic_body.get("model", "claude-3-5-sonnet-20241022")
        )
        content_summary = ""
        for block in anthropic_resp.get("content", []):
            if block.get("type") == "text":
                content_summary += block.get("text", "")[:100]
            elif block.get("type") == "tool_use":
                content_summary += f"[tool_use: {block.get('name', '')}] "
        log(f"  <- Responding: {content_summary[:200]}")
        self._respond_json(anthropic_resp)

    def _handle_streaming_response(self, resp, anthropic_body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        model_name = anthropic_body.get("model", "claude-3-5-sonnet-20241022")
        msg_id = f"msg_{os.urandom(8).hex()}"
        total_text = ""
        tool_calls_buffer = {}
        input_tokens = 0
        output_tokens = 0
        text_block_started = False
        tools_extractor = _StreamingToolsExtractor()
        content_tools_pending = []

        def _emit_text_delta(t):
            """Emit a text delta SSE event, opening the text block lazily."""
            nonlocal text_block_started, total_text
            if not t:
                return
            if not text_block_started:
                text_block_started = True
                self.wfile.write(
                    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                )
            total_text += t
            ev = f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":{json.dumps(t)}}}}}\n\n'
            self.wfile.write(ev.encode("utf-8"))
            self.wfile.flush()

        # Send message_start (usage will be updated from llama-server timings)
        event = {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model_name,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}
            }
        }
        self.wfile.write(f"event: message_start\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

        stream_finish_reason = None
        last_chunk = None
        for line in resp:
            line = line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                last_chunk = chunk
            except json.JSONDecodeError:
                continue

            choice = chunk.get("choices", [{}])[0]
            delta = choice.get("delta", {})

            # Track finish_reason from the stream
            if choice.get("finish_reason"):
                stream_finish_reason = choice["finish_reason"]

            # Extract usage from llama-server timings or OpenAI/DeepSeek usage field
            timings = chunk.get("timings")
            if timings:
                input_tokens = timings.get("prompt_n", input_tokens)
                output_tokens = timings.get("predicted_n", output_tokens)
            usage = chunk.get("usage")
            if usage:
                input_tokens = usage.get("prompt_tokens", input_tokens)
                output_tokens = usage.get("completion_tokens", output_tokens)

            # Handle tool_calls in streaming
            tc_delta = delta.get("tool_calls")
            if tc_delta:
                for tc in tc_delta:
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    if tc.get("id"):
                        tool_calls_buffer[idx]["id"] += tc["id"]
                    if tc.get("function", {}).get("name"):
                        tool_calls_buffer[idx]["function"]["name"] += tc["function"]["name"]
                    if tc.get("function", {}).get("arguments"):
                        tool_calls_buffer[idx]["function"]["arguments"] += tc["function"]["arguments"]
                continue

            # Handle content text — pass through state machine that strips <tools> blocks
            text = delta.get("content", "") or delta.get("reasoning_content", "")
            if not text:
                continue
            for kind, value in tools_extractor.feed(text):
                if kind == "text":
                    _emit_text_delta(value)
                else:  # "tool"
                    content_tools_pending.append(value)

        # Flush any unfinished state-machine state.
        for kind, value in tools_extractor.finalize():
            if kind == "text":
                _emit_text_delta(value)
            else:
                content_tools_pending.append(value)

        # Prefer structured tool_calls; only inject content-extracted tools if buffer is empty.
        if content_tools_pending and not tool_calls_buffer:
            for i, t in enumerate(content_tools_pending):
                tool_calls_buffer[i] = {
                    "id": f"call_{os.urandom(8).hex()}",
                    "type": "function",
                    "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])},
                }

        # Send content_block_stop for text (only if text was output)
        if text_block_started:
            self.wfile.write(
                f'event: content_block_stop\ndata: {{"type":"content_block_stop","index":0}}\n\n'
                .encode("utf-8")
            )

        # Send tool_use blocks if any
        tool_call_idx = 1 if not text_block_started else 1
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]
            if not tc["function"].get("name"):
                continue
            # Ensure tool_call id is present (some backends omit it in streaming)
            tc_id = tc.get("id", "") or f"call_{os.urandom(8).hex()}"
            tool_name = tc["function"].get("name", "")
            raw_args = tc["function"].get("arguments", "{}")
            input_data = parse_tool_arguments(raw_args, tool_name)

            # content_block_start for tool_use (Anthropic SDK expects input to start empty)
            event = {
                "type": "content_block_start",
                "index": tool_call_idx,
                "content_block": {
                    "type": "tool_use",
                    "id": tc_id,
                    "name": tool_name,
                    "input": {},
                }
            }
            self.wfile.write(f"event: content_block_start\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

            # Send input_json_delta with the actual parameters
            input_json = json.dumps(input_data, ensure_ascii=False)
            event = {
                "type": "content_block_delta",
                "index": tool_call_idx,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": input_json,
                }
            }
            self.wfile.write(f"event: content_block_delta\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

            # content_block_stop for tool_use
            event = {"type": "content_block_stop", "index": tool_call_idx}
            self.wfile.write(f"event: content_block_stop\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
            tool_call_idx += 1

        # Determine stop_reason
        stop_reason = "end_turn"
        if stream_finish_reason == "tool_calls" or tool_calls_buffer:
            stop_reason = "tool_use"
        elif stream_finish_reason == "length":
            stop_reason = "max_tokens"

        # Send message_delta with usage (required by Anthropic SDK)
        event = {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens}
        }
        self.wfile.write(f"event: message_delta\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

        # Send message_stop
        event = {"type": "message_stop"}
        self.wfile.write(f"event: message_stop\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
        self.wfile.flush()
        log(f"  <- Streamed text={len(total_text)} chars, tools={len(tool_calls_buffer)}")

    def _respond_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        raw = json.dumps(data, ensure_ascii=False)
        log(f"  <- Response body: {raw[:500]}")
        self.wfile.write(raw.encode("utf-8"))


def main():
    port = int(os.environ.get("PORT", "4000"))
    host = os.environ.get("HOST", "127.0.0.1")
    log(f"=== Starting Anthropic proxy on http://{host}:{port} ===")
    log(f"Backend type: {BACKEND_TYPE}")
    log(f"Forwarding to: {LLAMA_BASE}")
    log(f"Model: {MODEL_NAME}")
    log(f"Concurrency: {PROXY_MAX_CONCURRENT}")
    log(f"Tool clearing: {'enabled (threshold=' + str(PROXY_CLEAR_THRESHOLD) + ', keep=' + str(PROXY_TOOL_KEEP) + ')' if PROXY_CLEAR_ENABLED else 'disabled (' + BACKEND_TYPE + ' backend)'}")
    log(f"Context limit: {'enabled (limit=' + str(PROXY_CTX_CHARS_LIMIT) + ')' if PROXY_CTX_LIMIT_ENABLED else 'disabled (' + BACKEND_TYPE + ' backend)'}")
    if IS_CLOUD:
        log(f"Cloud API mode — no local backend required")
    ThreadingHTTPServer((host, port), Handler).serve_forever()

if __name__ == "__main__":
    main()
