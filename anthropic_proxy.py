#!/usr/bin/env python3
"""
Anthropic-to-OpenAI proxy for local llama-server.
Handles Qwen3.6 reasoning_content, streaming, and tool use correctly.
Includes XML->JSON fallback for Qwen tool calling quirks.
"""
import hashlib
import json
import os
import re
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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
PROXY_CTX_TRUNCATE_STRATEGY = os.environ.get("PROXY_CTX_TRUNCATE_STRATEGY", "char")
PROXY_CTX_KEEP_ROUNDS = int(os.environ.get("PROXY_CTX_KEEP_ROUNDS", "10"))
PROXY_CTX_KEEP_MESSAGES = int(os.environ.get("PROXY_CTX_KEEP_MESSAGES", "40"))  # fifo strategy: total messages to keep
PROXY_CTX_TOKEN_BUDGET = int(os.environ.get("PROXY_CTX_TOKEN_BUDGET", "30000"))
PROXY_CTX_TOKEN_RATIO = float(os.environ.get("PROXY_CTX_TOKEN_RATIO", "2.0"))

# ---------------------------------------------------------------------------
# Output token control: prevent rapid-mlx from generating unbounded output
# Known Issue #1: rapid-mlx ignores max_tokens
# ---------------------------------------------------------------------------
PROXY_MAX_TOKENS_OVERRIDE = int(os.environ.get("PROXY_MAX_TOKENS_OVERRIDE", "0"))
PROXY_OUTPUT_TOKEN_LIMIT_RATIO = float(os.environ.get("PROXY_OUTPUT_TOKEN_LIMIT_RATIO", "2.0"))
PROXY_BACKEND_TIMEOUT = int(os.environ.get("PROXY_BACKEND_TIMEOUT", "300"))

# ---------------------------------------------------------------------------
# Loop detection: detect consecutive identical tool_use calls
# ---------------------------------------------------------------------------
PROXY_LOOP_THRESHOLD = int(os.environ.get("PROXY_LOOP_THRESHOLD", "3"))
PROXY_LOOP_LEVEL2 = int(os.environ.get("PROXY_LOOP_LEVEL2", str(PROXY_LOOP_THRESHOLD * 2)))
PROXY_LOOP_LEVEL3 = int(os.environ.get("PROXY_LOOP_LEVEL3", str(PROXY_LOOP_THRESHOLD * 3)))

# ---------------------------------------------------------------------------
# Re-read prevention: when a Read tool targets a file whose content was just
# cleared, keep a preview of the original content to reduce re-read desire.
PROXY_REREAD_PREVIEW_CHARS = int(os.environ.get("PROXY_REREAD_PREVIEW_CHARS", "200"))

# ---------------------------------------------------------------------------
# Blocker detection: track consecutive same-error-type tool_result rejections
# (e.g. Read repeatedly returns "File does not exist"). When a tool fails the
# same way >= PROXY_BLOCKER_THRESHOLD times in a row, inject a [BLOCKER] user
# message nudging the model to switch tools or escalate. Disabled by default
# for cloud backends (1M+ token context, low marginal value).
# ---------------------------------------------------------------------------
PROXY_BLOCKER_ENABLED = os.environ.get("PROXY_BLOCKER_ENABLED", "true" if not IS_CLOUD else "false").lower() in ("1", "true", "yes")
PROXY_BLOCKER_THRESHOLD = int(os.environ.get("PROXY_BLOCKER_THRESHOLD", "2"))

# Markers written by the error-translation pass (lines ~2702-2737). Kept in
# one place so the blocker detector and the translation stay in sync.
# Order matters: longer/more specific markers are checked first.
_BLOCKER_ERROR_MARKERS = (
    ("wasted",            ["该文件自上次读取后未发生变化", "wasted call"]),
    ("file_not_found",    ["文件不存在", "file does not exist", "no such file"]),
    ("input_validation",  ["工具调用参数错误", "inputvalidationerror"]),
)

# ---------------------------------------------------------------------------
# Dynamic tool definition filtering: reduce token overhead from tool schemas
# ---------------------------------------------------------------------------
PROXY_TOOL_FILTER_ENABLED = os.environ.get("PROXY_TOOL_FILTER_ENABLED", "true" if not IS_CLOUD else "false").lower() in ("1", "true", "yes")
PROXY_TOOL_FILTER_MAX = int(os.environ.get("PROXY_TOOL_FILTER_MAX", "20"))
PROXY_TOOL_FILTER_RECENT = int(os.environ.get("PROXY_TOOL_FILTER_RECENT", "5"))
TOOL_ALWAYS_KEEP = {
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "LS", "Task", "WebFetch", "WebSearch",
    "TodoRead", "TodoWrite",
}

# ---------------------------------------------------------------------------
# Keyword index (BM25 MVP): extract keywords from dropped messages and
# inject relevant context into tail for better continuity.
# ---------------------------------------------------------------------------
PROXY_HISTORY_INDEX = os.environ.get("PROXY_HISTORY_INDEX", "rule")
PROXY_HISTORY_TOP_K = int(os.environ.get("PROXY_HISTORY_TOP_K", "5"))
PROXY_HISTORY_MAX_CHARS = int(os.environ.get("PROXY_HISTORY_MAX_CHARS", "500"))

# ---------------------------------------------------------------------------
# Semantic tool-result clearing: priority-based scoring
# ---------------------------------------------------------------------------
TOOL_SEMANTIC_PRIORITY = {
    "Read": 3, "Agent": 3, "WebFetch": 2, "WebSearch": 2,
    "Bash": 1, "Edit": 1, "Write": 1,
}
TOOL_RESULT_HIGH_VALUE_PATTERNS = [
    (re.compile(r'(function |class |def |import |from |\{\s*"[a-z]|\#include)', re.IGNORECASE), 3),
    (re.compile(r'(total \d+|drwx|\.py$|\.js$|\.ts$)', re.IGNORECASE), 1),
    (re.compile(r'(error|traceback|exception)', re.IGNORECASE), 2),
    (re.compile(r'Wasted call', re.IGNORECASE), 0),
]

# ---------------------------------------------------------------------------
# Structured request logging: JSON Lines to logs/proxy_requests.jsonl
# ---------------------------------------------------------------------------
_LOG_DIR = os.path.join(_SCRIPT_DIR, "logs")
_JSONL_PATH = os.path.join(_LOG_DIR, "proxy_requests.jsonl")
_jsonl_lock = threading.Lock()
# Maps request token -> output_chars (set by response handlers)
_jsonl_output_map = {}
_jsonl_counter = 0

# ---------------------------------------------------------------------------
# Structured metrics logging: JSON Lines to logs/proxy_metrics.jsonl
# Per-request pipeline stats for observability and tuning.
# ---------------------------------------------------------------------------
PROXY_METRICS_ENABLED = os.environ.get("PROXY_METRICS_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_METRICS_DIR = os.environ.get("PROXY_METRICS_DIR", "logs")
_METRICS_PATH = os.path.join(_SCRIPT_DIR, PROXY_METRICS_DIR, "proxy_metrics.jsonl")
_metrics_lock = threading.Lock()


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
                status: int, duration_ms: float, start_time: str = ""):
    """Append one JSON Lines record to proxy_requests.jsonl (thread-safe)."""
    _ensure_jsonl_dir()
    now_iso = datetime.now().isoformat()
    record = {
        "start_time": start_time or now_iso,
        "end_time": now_iso,
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

# Thread-local context for per-request logging (session_id prefix)
_log_ctx = threading.local()

# Thread-local context for per-request metrics collection
_metrics_ctx = threading.local()


def log_metrics(metrics: dict):
    _ensure_jsonl_dir()
    line = json.dumps(metrics, ensure_ascii=False) + "\n"
    try:
        with _metrics_lock:
            with open(_METRICS_PATH, "a") as f:
                f.write(line)
    except OSError:
        pass


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    sess = getattr(_log_ctx, 'session_id', None)
    if sess:
        line = f"[{ts}] [sess={sess}] {msg}"
    else:
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


def _repair_truncated_json(raw: str) -> str:
    """Attempt to repair truncated JSON by closing open strings/braces."""
    if not raw or not raw.strip():
        return "{}"
    s = raw.strip()
    in_string = False
    escape_next = False
    depth = 0
    i = 0
    while i < len(s):
        c = s[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if c == '\\':
            if in_string:
                escape_next = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if c in ('{', '['):
            depth += 1
        elif c in ('}', ']'):
            depth -= 1
        i += 1
    if in_string:
        s += '"'
    while depth > 0:
        s += '}'
        depth -= 1
    return s


def _coerce_booleans(obj):
    """Recursively coerce stringified booleans ('True'/'False'/'true'/'false') to bool."""
    if isinstance(obj, dict):
        return {k: _coerce_booleans(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_booleans(v) for v in obj]
    if isinstance(obj, str):
        if obj.lower() == "true":
            return True
        if obj.lower() == "false":
            return False
    return obj


def parse_tool_arguments(raw: str, tool_name_hint: str = "") -> dict:
    """
    Parse tool arguments from backend response.
    Falls back from JSON -> XML extraction -> empty dict.
    Stringified booleans are coerced to real bools to satisfy client validation.
    """
    raw = raw.strip() if raw else ""
    if not raw:
        return {}

    # 1. Try standard JSON
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return _coerce_booleans(parsed)
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
                return _coerce_booleans(parsed)
        except json.JSONDecodeError:
            pass

    # 3. XML fallback
    xml_params = _extract_xml_params(raw)
    if xml_params:
        log(f"  [XML_FALLBACK] extracted {len(xml_params)} params from XML for tool={tool_name_hint}")
        return _coerce_booleans(xml_params)

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


def _generate_tool_summary(tool_name, meta_info):
    """Generate deterministic summary for a cleared tool result.
    Same (tool_name, meta_info) always produces the same output,
    enabling prefix cache hits when the same tool call appears across requests.
    """
    if not tool_name:
        return "tool"
    if meta_info.startswith(" file="):
        return f'{tool_name}("{meta_info[6:]}")'
    elif meta_info.startswith(" cmd="):
        cmd = meta_info[5:].strip()
        return f'{tool_name}("{cmd}")'
    return tool_name


def clear_old_tool_results(messages, tools_list=None):
    """
    Proxy-side tool-result clearing with semantic priority scoring.
    Replaces old tool_result contents with a placeholder, keeping the most
    important tool_results based on tool name + content patterns.
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

    # Dynamic KEEP: detect sub-agent by checking tool set
    keep = PROXY_TOOL_KEEP
    if tools_list is not None:
        has_agent = any(t == "Agent" or t == "EnterPlanMode" for t in tools_list)
        if not has_agent and len(tools_list) > 0:
            keep = max(PROXY_TOOL_KEEP, 15)
            log(f"  -> Sub-agent detected ({len(tools_list)} tools, no Agent/Plan), dynamic KEEP={keep}")

    total_tool_results = len(tool_result_indices)
    recent_cutoff = max(0, total_tool_results - 6)

    # Score each tool_result by semantic priority
    scored = []
    for idx_pos, (msg_idx, block_idx) in enumerate(tool_result_indices):
        block = messages[msg_idx]["content"][block_idx]
        tool_use_id = block.get("tool_use_id", "")
        content_str = str(block.get("content", ""))
        score = 0

        # Find the paired tool_use to get tool name
        tool_name = ""
        for m_idx in range(msg_idx - 1, -1, -1):
            m = messages[m_idx]
            if m.get("role") == "assistant":
                c = m.get("content", "")
                if isinstance(c, list):
                    for b in c:
                        if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                            tool_name = b.get("name", "")
                            break
                if tool_name:
                    break

        score += TOOL_SEMANTIC_PRIORITY.get(tool_name, 1)
        for pat, pts in TOOL_RESULT_HIGH_VALUE_PATTERNS:
            if pat.search(content_str[:500]):
                score += pts

        # Boost recent Read results to prevent re-read loops
        if tool_name == "Read" and idx_pos >= recent_cutoff:
            score += 5

        scored.append((score, idx_pos, msg_idx, block_idx, tool_name, content_str))

    # Sort by score descending, keep top N by score (prefer high-value)
    scored.sort(key=lambda x: (-x[0], -x[1]))
    keep_positions = set(x[1] for x in scored[:keep])

    # Build set of tool_use indices to scan for file_path metadata
    cleared_files = set()
    cleared_count = 0
    cleared_chars = 0
    high_prio_count = 0

    for idx_pos, (msg_idx, block_idx) in enumerate(tool_result_indices):
        if idx_pos in keep_positions:
            score_entry = next((x for x in scored if x[1] == idx_pos), None)
            if score_entry and score_entry[0] >= 3:
                high_prio_count += 1
            continue
        block = messages[msg_idx]["content"][block_idx]
        original = block.get("content", "")
        original_len = len(str(original)) if original else 0

        # Extract file path from paired tool_use
        tool_use_id = block.get("tool_use_id", "")
        meta_info = ""
        for m_idx in range(msg_idx - 1, -1, -1):
            m = messages[m_idx]
            if m.get("role") == "assistant":
                c = m.get("content", "")
                if isinstance(c, list):
                    for b in c:
                        if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                            inp = b.get("input", {})
                            if isinstance(inp, dict):
                                fp = inp.get("file_path", inp.get("path", ""))
                                cmd = inp.get("command", "")
                                if fp:
                                    meta_info = f" file={fp}"
                                    cleared_files.add(fp)
                                elif cmd:
                                    meta_info = f" cmd={cmd[:60]}"
                            break
                if meta_info:
                    break

        # Find paired tool_use to get tool_name for deterministic summary
        tool_name = ""
        for m_idx in range(msg_idx - 1, -1, -1):
            m = messages[m_idx]
            if m.get("role") == "assistant":
                c = m.get("content", "")
                if isinstance(c, list):
                    for b in c:
                        if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                            tool_name = b.get("name", "")
                            break
                if tool_name:
                    break

        summary = _generate_tool_summary(tool_name, meta_info)
        # For Read tool results, keep a preview to reduce re-read desire
        if tool_name == "Read" and PROXY_REREAD_PREVIEW_CHARS > 0:
            preview = content_str[:PROXY_REREAD_PREVIEW_CHARS]
            if len(content_str) > PROXY_REREAD_PREVIEW_CHARS:
                preview += "..."
            block["content"] = f"[cleared: {summary}]\n{preview}"
        else:
            block["content"] = f"[cleared: {summary}]"
        cleared_count += 1
        cleared_chars += original_len

    # Bash dedup among kept results: merge consecutive similar Bash outputs
    dedup_bash = 0
    dedup_chars = 0
    kept_indices = [tool_result_indices[i] for i in sorted(keep_positions)]
    for ki in range(len(kept_indices) - 1):
        msg_idx_a, block_idx_a = kept_indices[ki]
        msg_idx_b, block_idx_b = kept_indices[ki + 1]
        ca = str(messages[msg_idx_a]["content"][block_idx_a].get("content", ""))
        cb = str(messages[msg_idx_b]["content"][block_idx_b].get("content", ""))
        if not ca or not cb:
            continue
        lines_a = set(ca.splitlines())
        lines_b = set(cb.splitlines())
        if not lines_a or not lines_b:
            continue
        intersection = lines_a & lines_b
        union = lines_a | lines_b
        jaccard = len(intersection) / len(union) if union else 0
        if jaccard >= 0.7:
            # Deterministic placeholder for deduplicated Bash
            messages[msg_idx_b]["content"][block_idx_b]["content"] = "[cleared: Bash(deduplicated)]"
            dedup_bash += 1
            dedup_chars += len(cb)

    return messages, {
        "enabled": True,
        "cleared": True,
        "cleared_tool_results": cleared_count,
        "cleared_chars": cleared_chars,
        "kept": keep,
        "high_prio": high_prio_count,
        "cleared_files": list(cleared_files),
        "dedup_bash": dedup_bash,
        "dedup_chars_saved": dedup_chars,
        "total_chars_before": total_chars,
    }


def _compute_adaptive_rounds(messages, base_rounds):
    extra = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tc = b.get("content", "")
                        if isinstance(tc, str):
                            low = tc.lower()
                            if any(kw in low for kw in ["error", "exception", "failed", "traceback"]):
                                extra += 1
                                break
            elif isinstance(content, str):
                low = content.lower()
                if any(kw in low for kw in ["error", "exception", "failed", "traceback"]):
                    extra += 1
        elif role == "assistant":
            if isinstance(content, list):
                write_count = 0
                edit_count = 0
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        name = b.get("name", "")
                        if name in ("Write", "NotebookEdit"):
                            write_count += 1
                        elif name == "Edit":
                            edit_count += 1
                if write_count + edit_count > 2:
                    extra += 1
    adaptive = min(base_rounds + extra, base_rounds * 2)
    return adaptive


def _extract_middle_summary_rules(messages):
    errors_solutions = []
    code_changes = []
    decisions = []
    file_states = {}

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tc = b.get("content", "")
                        if isinstance(tc, str):
                            low = tc.lower()
                            if any(kw in low for kw in ["error", "exception", "failed", "traceback"]):
                                errors_solutions.append(tc[:500])
                            if "successfully" in low or "updated" in low or "created" in low:
                                errors_solutions.append(f"[resolved] {tc[:200]}")
        elif role == "assistant":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "tool_use":
                            name = b.get("name", "")
                            inp = b.get("input", {})
                            if isinstance(inp, dict):
                                fp = inp.get("file_path", inp.get("path", ""))
                                if fp:
                                    file_states[fp] = name
                                if name in ("Write", "Edit"):
                                    code_changes.append(f"{name}({fp})")
                        elif b.get("type") == "text":
                            txt = b.get("text", "")
                            if any(kw in txt for kw in ["DECISION", "TODO", "FIXME", "IMPORTANT", "NOTE"]):
                                decisions.append(txt[:200])

    parts = []
    if errors_solutions:
        parts.append("<errors_solutions>")
        for e in errors_solutions[:5]:
            parts.append(f"- {e}")
        parts.append("</errors_solutions>")
    if code_changes:
        parts.append("<code_changes>")
        for c in code_changes[:10]:
            parts.append(f"- {c}")
        parts.append("</code_changes>")
    if file_states:
        parts.append("<file_states>")
        for fp, op in sorted(file_states.items())[-10:]:
            parts.append(f"- {fp}: last {op}")
        parts.append("</file_states>")
    if decisions:
        parts.append("<decisions>")
        for d in decisions[:5]:
            parts.append(f"- {d}")
        parts.append("</decisions>")

    if not parts:
        return None
    header = f"[Compressed context from {len(messages)} earlier messages (rule-based):]"
    return header + "\n".join(parts)


def _compress_middle_with_llm(messages, timeout=30):
    try:
        conversation_text = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            parts.append(b.get("text", "")[:300])
                        elif b.get("type") == "tool_use":
                            name = b.get("name", "")
                            inp = b.get("input", {})
                            parts.append(f"[tool:{name}({json.dumps(inp, ensure_ascii=False)[:200]})]")
                        elif b.get("type") == "tool_result":
                            tc = b.get("content", "")
                            if isinstance(tc, str):
                                parts.append(f"[result:{tc[:200]}]")
                text = " ".join(parts)
            elif isinstance(content, str):
                text = content[:300]
            else:
                continue
            conversation_text.append(f"{role}: {text}")

        conv_str = "\n".join(conversation_text)
        if len(conv_str) > 8000:
            conv_str = conv_str[:8000] + "...[truncated]"

        prompt = (
            "Summarize the following coding session into these XML sections. "
            "Be concise. Keep error messages verbatim. Keep file paths. Remove narration.\n\n"
            "<current_focus>What is being worked on (1-2 sentences)</current_focus>\n"
            "<errors_solutions>\n"
            "For each non-trivial error encountered, output ONE entry in this EXACT format:\n"
            "  - Error: <short verbatim error message or symptom>\n"
            "    Root cause: <why it happened — 1 sentence>\n"
            "    Fix: <what was done to resolve it — 1 sentence>\n"
            "    Avoidance: <what to verify next time to prevent recurrence — 1 sentence or 'N/A'>\n"
            "If no errors: output 'none'.\n"
            "</errors_solutions>\n"
            "<code_state>Current file states, key code signatures (function names, important constants)</code_state>\n"
            "<decisions>Architecture/design decisions and the reason behind each</decisions>\n"
            "<pending>Unfinished tasks, blockers, and what is needed to unblock each</pending>\n\n"
            f"Session log ({len(messages)} messages):\n{conv_str}"
        )

        payload = {
            "model": "compressor",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
            "temperature": 0.3,
            "stream": False,
        }
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{LLAMA_BASE}/chat/completions",
            data=req_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        text = ""
        for choice in result.get("choices", []):
            msg = choice.get("message", {})
            text += msg.get("content", "")
        if text.strip():
            return f"[Compressed context from {len(messages)} earlier messages (LLM):]\n{text.strip()}"
        return None
    except Exception as e:
        log(f"  -> LLM compression failed: {e}, falling back to rules")
        return None


_summary_cache = {}
_summary_cache_lock = threading.Lock()
_SUMMARY_CACHE_MAX_SESSIONS = 10
_SUMMARY_CACHE_MAX_CHARS = 3000


def _merge_summaries_with_llm(old_summary, new_summary, timeout=15):
    try:
        prompt = (
            "Merge these two session summaries into one concise summary. "
            "Keep all errors, file states, and decisions. Remove redundancy.\n\n"
            f"<previous_summary>\n{old_summary[:3000]}\n</previous_summary>\n\n"
            f"<new_summary>\n{new_summary[:3000]}\n</new_summary>"
        )
        payload = {
            "model": "compressor",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 800,
            "temperature": 0.3,
            "stream": False,
        }
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{LLAMA_BASE}/chat/completions",
            data=req_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        text = ""
        for choice in result.get("choices", []):
            msg = choice.get("message", {})
            text += msg.get("content", "")
        return text.strip() if text.strip() else old_summary + "\n\n" + new_summary
    except Exception as e:
        log(f"  -> Summary merge failed: {e}, concatenating")
        return old_summary + "\n\n" + new_summary


def _incremental_compress(dropped, session_id):
    with _summary_cache_lock:
        cache = _summary_cache.get(session_id)

    if cache and cache.get("last_compressed_msg_index", 0) > 0:
        new_start = min(cache["last_compressed_msg_index"], len(dropped))
        new_dropped = dropped[new_start:]
        if len(new_dropped) >= 5:
            new_summary = _compress_middle_with_llm(new_dropped, timeout=30)
            if not new_summary:
                new_summary = _extract_middle_summary_rules(new_dropped)
        else:
            new_summary = _extract_middle_summary_rules(new_dropped) if new_dropped else None

        if new_summary:
            combined = cache["summary"] + "\n\n" + new_summary
            if len(combined) > _SUMMARY_CACHE_MAX_CHARS:
                combined = _merge_summaries_with_llm(cache["summary"], new_summary)
            compressed_text = combined
        else:
            compressed_text = cache["summary"]
    else:
        if len(dropped) >= 10:
            compressed_text = _compress_middle_with_llm(dropped, timeout=30)
        else:
            compressed_text = None
        if not compressed_text:
            compressed_text = _extract_middle_summary_rules(dropped)
        if not compressed_text:
            return None, None

    with _summary_cache_lock:
        if len(_summary_cache) >= _SUMMARY_CACHE_MAX_SESSIONS:
            oldest_key = next(iter(_summary_cache))
            del _summary_cache[oldest_key]
        _summary_cache[session_id] = {
            "last_compressed_msg_index": len(dropped),
            "summary": compressed_text[:_SUMMARY_CACHE_MAX_CHARS],
        }

    return compressed_text, cache is not None


def truncate_messages_if_needed(messages, session_id=None):
    """
    Proxy-side message truncation with dual strategy support.

    Strategy 'char' (default): drop old messages until total chars fall below
    PROXY_CTX_CHARS_LIMIT. Preserves head + tail window.

    Strategy 'rounds': keep only the most recent N assistant rounds,
    replacing dropped messages with a lightweight placeholder.
    Operates on Anthropic-format messages in-place.
    Returns (messages, stats_dict).
    """
    if not PROXY_CTX_LIMIT_ENABLED and PROXY_CTX_TRUNCATE_STRATEGY != "rounds":
        return messages, {"enabled": False}

    # ---------- rounds strategy ----------
    if PROXY_CTX_TRUNCATE_STRATEGY == "rounds":
        # Token budget check: skip if already within budget
        estimated_tokens = _estimate_message_chars(messages) * PROXY_CTX_TOKEN_RATIO
        if estimated_tokens <= PROXY_CTX_TOKEN_BUDGET and len(messages) <= PROXY_CTX_KEEP_HEAD + PROXY_CTX_KEEP_ROUNDS * 3:
            return messages, {
                "enabled": True, "strategy": "rounds", "skipped": True,
                "reason": "below_budget",
                "estimated_tokens": int(estimated_tokens),
                "budget": PROXY_CTX_TOKEN_BUDGET,
            }

        # Adaptive rounds + LLM/rule compression
        adaptive_rounds = _compute_adaptive_rounds(messages, PROXY_CTX_KEEP_ROUNDS)
        min_rounds = 2
        for rounds in range(adaptive_rounds, min_rounds - 1, -1):
            result, stats = _apply_rounds_truncation(messages, rounds, session_id=session_id)
            if not stats.get("truncated"):
                return result, stats
            result_tokens = _estimate_message_chars(result) * PROXY_CTX_TOKEN_RATIO
            if result_tokens <= PROXY_CTX_TOKEN_BUDGET or rounds == min_rounds:
                stats["estimated_tokens"] = int(result_tokens)
                stats["budget"] = PROXY_CTX_TOKEN_BUDGET
                stats["actual_keep_rounds"] = rounds
                stats["adaptive_rounds"] = adaptive_rounds
                return result, stats

        return messages, {"enabled": True, "strategy": "rounds", "skipped": True, "reason": "no_reduction"}

    # ---------- fifo strategy ----------
    if PROXY_CTX_TRUNCATE_STRATEGY == "fifo":
        n = len(messages)
        keep_total = PROXY_CTX_KEEP_MESSAGES
        if n <= keep_total:
            return messages, {
                "enabled": True, "strategy": "fifo", "skipped": True,
                "reason": "below_limit", "count": n, "limit": keep_total,
            }

        head = messages[:PROXY_CTX_KEEP_HEAD]
        tail_count = keep_total - PROXY_CTX_KEEP_HEAD
        tail = messages[-tail_count:]
        dropped = messages[PROXY_CTX_KEEP_HEAD : n - tail_count]
        dropped_count = len(dropped)

        # Count tools in dropped messages
        tool_count = 0
        for m in dropped:
            if m.get("role") == "assistant":
                content = m.get("content", [])
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tool_count += 1
                elif isinstance(content, dict) and content.get("type") == "tool_use":
                    tool_count += 1

        # Extract file mentions from dropped messages
        file_mentions = set()
        for m in dropped:
            if m.get("role") == "assistant":
                content = m.get("content", [])
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            args = ""
                            fn = b.get("function") or {}
                            if isinstance(fn, dict):
                                args = fn.get("arguments", "")
                            if not args:
                                args = b.get("input", "")
                            if isinstance(args, dict):
                                args = json.dumps(args)
                            if isinstance(args, str):
                                for pat in [r'"path":\s*"([^"]+)"', r'"file":\s*"([^"]+)"',
                                            r'"filePath":\s*"([^"]+)"', r'"directory":\s*"([^"]+)"']:
                                    file_mentions.update(re.findall(pat, args))

        file_info = f" Files: {', '.join(sorted(file_mentions)[:10])}." if file_mentions else ""

        compressed_text = (
            f"[Context folded: {dropped_count} earlier messages omitted."
            f" Previous work included {tool_count} tool interactions.{file_info}]"
        )

        if tail and tail[0].get("role") == "user":
            tail_content = tail[0].get("content", [])
            summary_block = {"type": "text", "text": compressed_text}
            if isinstance(tail_content, list):
                tail[0]["content"] = [summary_block] + tail_content
            else:
                tail[0]["content"] = [summary_block, {"type": "text", "text": str(tail_content)}]
            result = head + tail
        else:
            summary = {"role": "user", "content": [{"type": "text", "text": compressed_text}]}
            result = head + [summary] + tail

        return result, {
            "enabled": True,
            "strategy": "fifo",
            "truncated": True,
            "dropped_messages": dropped_count,
            "kept_messages": len(result),
            "tool_count": tool_count,
            "file_mentions": len(file_mentions),
        }


def _detect_blocker_pattern(messages):
    """
    Walk messages backward and detect a tail of consecutive same-error-type
    tool_result rejections. Stops at the first non-error tool_result, the first
    plain user text message, or when the run length drops to zero.

    Returns a dict:
        {
          "triggered": bool,
          "tool_name": str,            # last assistant tool_use name in the run
          "error_type": str,           # wasted | file_not_found | input_validation
          "run_length": int,           # count of consecutive errors at the tail
        }
    or {"triggered": False} when no run meets the threshold.

    Detection is based on the rewritten content produced by the error-
    translation pass (which substitutes the original tool_result with a fixed
    Chinese system message). This means the detector is best-effort: it relies
    on the upstream pass having run, and on the marker substrings matching.
    """
    if not PROXY_BLOCKER_ENABLED:
        return {"triggered": False, "reason": "disabled"}

    last_tool_name = None
    run = []  # list of (tool_name, error_type), index 0 = oldest in run

    for msg in reversed(messages):
        role = msg.get("role")
        if role == "assistant":
            content = msg.get("content", "")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        last_tool_name = b.get("name", "unknown")
                        break
            continue
        if role != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            # Plain user text breaks the consecutive-error tail.
            break
        tool_result_block = None
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tool_result_block = b
                break
        if tool_result_block is None:
            # User message with no tool_result breaks the tail (e.g. a text turn).
            break
        tc = str(tool_result_block.get("content", ""))
        tc_lower = tc.lower()
        matched = None
        for err_type, markers in _BLOCKER_ERROR_MARKERS:
            for m in markers:
                if m in tc_lower:
                    matched = err_type
                    break
            if matched:
                break
        if matched is None:
            # Non-error tool_result breaks the run.
            break
        if run and run[-1][1] != matched:
            # Different error type from the most recent in the run: treat
            # the run as broken (the model is hitting two different
            # problems, not a single recurring one).
            break
        run.append((last_tool_name or "unknown", matched))

    run_length = len(run)
    if run_length < PROXY_BLOCKER_THRESHOLD:
        return {"triggered": False, "run_length": run_length, "threshold": PROXY_BLOCKER_THRESHOLD}

    tool_name, error_type = run[-1]  # most recent
    return {
        "triggered": True,
        "tool_name": tool_name,
        "error_type": error_type,
        "run_length": run_length,
    }


def _build_blocker_message(tool_name, error_type, run_length):
    """
    Construct a [BLOCKER] user message to inject into the tail when a tool
    has been failing the same way repeatedly. Text is intentionally short and
    identical for the same (tool_name, error_type) pair, so prefix cache hits
    are preserved across requests.
    """
    return {
        "role": "user",
        "content": [{
            "type": "text",
            "text": (
                f"[BLOCKER] The tool '{tool_name}' has failed with the same "
                f"error ({error_type}) {run_length} times in a row. "
                f"Do NOT call '{tool_name}' again with similar arguments. "
                f"Either switch to a different tool, change your approach, "
                f"or report this blocker to the user."
            ),
        }],
    }


def _apply_rounds_truncation(messages, keep_rounds, session_id=None):
    head = messages[:PROXY_CTX_KEEP_HEAD]

    tail = []
    assistant_count = 0
    for msg in reversed(messages):
        tail.insert(0, msg)
        if msg.get("role") == "assistant":
            assistant_count += 1
        if assistant_count >= keep_rounds:
            break

    dropped_count = len(messages) - len(head) - len(tail)
    if dropped_count <= 0:
        return messages, {"enabled": True, "strategy": "rounds", "skipped": True}

    dropped = messages[PROXY_CTX_KEEP_HEAD : len(messages) - len(tail)]

    tool_count = 0
    for m in dropped:
        if m.get("role") == "assistant":
            content = m.get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_count += 1
            elif isinstance(content, dict) and content.get("type") == "tool_use":
                tool_count += 1

    file_mentions = set()
    for m in dropped:
        if m.get("role") == "assistant":
            content = m.get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        args = ""
                        fn = b.get("function") or {}
                        if isinstance(fn, dict):
                            args = fn.get("arguments", "")
                        if not args:
                            args = b.get("input", "")
                        if isinstance(args, dict):
                            args = json.dumps(args)
                        if isinstance(args, str):
                            for pat in [r'"path":\s*"([^"]+)"', r'"file":\s*"([^"]+)"',
                                        r'"filePath":\s*"([^"]+)"', r'"directory":\s*"([^"]+)"']:
                                file_mentions.update(re.findall(pat, args))

    file_info = f" Files previously accessed: {', '.join(sorted(file_mentions)[:10])}." if file_mentions else ""

    compressed_text = None
    cache_hit = False
    if session_id:
        compressed_text, cache_hit = _incremental_compress(dropped, session_id)
    if not compressed_text:
        if dropped_count >= 10:
            compressed_text = _compress_middle_with_llm(dropped, timeout=30)
        if not compressed_text:
            compressed_text = _extract_middle_summary_rules(dropped)
    if not compressed_text:
        compressed_text = (
            f"[Context folded: {dropped_count} earlier messages omitted. "
            f"Previous work included {tool_count} tool interactions."
            f"{file_info} "
            f"Retaining last {keep_rounds} conversation rounds.]"
        )

    if PROXY_HISTORY_INDEX == "rule" and dropped_count >= 5:
        keywords = _extract_keywords(dropped)
        keyword_ctx = _inject_keyword_context(
            keywords, tail,
            top_k=PROXY_HISTORY_TOP_K,
            max_chars=PROXY_HISTORY_MAX_CHARS,
        )
        if keyword_ctx:
            compressed_text += "\n\n" + keyword_ctx

    if tail and tail[0].get("role") == "user":
        tail_content = tail[0].get("content", [])
        summary_block = {"type": "text", "text": compressed_text}
        if isinstance(tail_content, list):
            tail[0]["content"] = [summary_block] + tail_content
        else:
            tail[0]["content"] = [summary_block, {"type": "text", "text": str(tail_content)}]
        result = head + tail
    else:
        summary = {"role": "user", "content": [{"type": "text", "text": compressed_text}]}
        result = head + [summary] + tail

    return result, {
        "enabled": True,
         "strategy": "rounds",
         "truncated": True,
         "dropped_messages": dropped_count,
         "kept_messages": len(result),
         "tool_count": tool_count,
         "file_mentions": len(file_mentions),
         "compression": "llm" if "LLM" in compressed_text else ("rules" if "rule-based" in compressed_text else "folded"),
     }

    # ---------- char strategy (existing logic) ----------
    total_chars = _estimate_message_chars(messages)
    if total_chars < PROXY_CTX_CHARS_LIMIT:
        return messages, {
            "enabled": True,
            "strategy": "char",
            "skipped": True,
            "reason": "below_limit",
            "chars": total_chars,
        }

    n = len(messages)
    if n <= PROXY_CTX_KEEP_HEAD + PROXY_CTX_KEEP_TAIL:
        return messages, {
            "enabled": True,
            "strategy": "char",
            "skipped": True,
            "reason": "too_few_messages",
            "count": n,
            "chars": total_chars,
        }

    preserved = set(range(PROXY_CTX_KEEP_HEAD))
    preserved.update(range(n - PROXY_CTX_KEEP_TAIL, n))
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
        messages[idx] = None
        if current_chars < PROXY_CTX_CHARS_LIMIT:
            break

    messages = [m for m in messages if m is not None]

    return messages, {
        "enabled": True,
        "strategy": "char",
        "truncated": True,
        "dropped_messages": dropped_count,
        "dropped_chars": dropped_chars,
        "chars_before": total_chars,
        "chars_after": current_chars,
        "limit": PROXY_CTX_CHARS_LIMIT,
    }


# ---------------------------------------------------------------------------
# Thinking/reasoning block stripping: remove old assistant thinking content
# to reduce context size. Operates defensively since current clients rarely
# send explicit thinking blocks (reasoning is usually inline text).
# ---------------------------------------------------------------------------
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


def strip_old_thinking_blocks(messages, keep_recent=3):
    """
    Remove thinking/reasoning content from old assistant messages.
    Keeps the most recent keep_recent assistant messages with thinking intact.
    Returns (messages, stats_dict).
    """
    if not messages:
        return messages, {"enabled": False}

    thinking_indices = []
    for idx, msg in enumerate(messages):
        if msg.get("role") == "assistant" and _has_thinking_content(msg):
            thinking_indices.append(idx)

    if not thinking_indices:
        return messages, {"enabled": True, "skipped": True, "reason": "no_thinking_found"}

    if len(thinking_indices) <= keep_recent:
        return messages, {"enabled": True, "skipped": True, "reason": "few_thinking", "count": len(thinking_indices)}

    keep_set = set(thinking_indices[-keep_recent:])
    stripped_count = 0
    for idx in thinking_indices:
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
    }


# ---------------------------------------------------------------------------
# Cleared tool-result compression: merge consecutive user messages that
# contain only cleared tool_results into a single user message.
# Reduces per-message JSON structural overhead (~50-100 tokens each).
# ---------------------------------------------------------------------------
def _is_pure_tool_use_msg(msg):
    """Check if an assistant message contains only tool_use blocks (no text)."""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content", "")
    if isinstance(content, list):
        if not content:
            return True
        for block in content:
            if block.get("type") != "tool_use":
                return False
        return True
    elif isinstance(content, dict):
        return content.get("type") == "tool_use"
    return False


# Prefixes used by cleared tool_results (supports old and new formats)
_CLEARED_PREFIXES = ("[cleared:", "[cleared to save context:", "[Result of tool call hidden]")


def _is_cleared_tool_result_msg(msg):
    """Check if a user message contains only cleared tool_results."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content", "")
    if isinstance(content, list):
        if not content:
            return False
        for block in content:
            if block.get("type") != "tool_result":
                return False
            block_content = str(block.get("content", ""))
            if not any(p in block_content for p in _CLEARED_PREFIXES):
                return False
        return True
    elif isinstance(content, dict):
        if content.get("type") != "tool_result":
            return False
        block_content = str(content.get("content", ""))
        return any(p in block_content for p in _CLEARED_PREFIXES)
    return False


def compress_cleared_tool_results(messages):
    """
    Compress tool-use cycles where the assistant message is a pure tool_use
    (no explanatory text) and the following user message contains only cleared
    tool_results. Consecutive such cycles are merged into a single lightweight
    assistant+user placeholder pair, saving ~50-100 tokens of JSON structural
    overhead per eliminated message.

    Also merges consecutive user messages that contain only cleared tool_results
    into a single user message.
    Returns (messages, stats_dict).
    """
    if len(messages) < 4:
        return messages, {"enabled": False}

    compressed = []
    stats = {"merged_cycles": 0, "merged_msgs": 0, "saved_overhead": 0}
    i = 0

    while i < len(messages):
        # Pattern: assistant(pure tool_use) + user(cleared tool_result)
        if (i + 1 < len(messages) and
                _is_pure_tool_use_msg(messages[i]) and
                _is_cleared_tool_result_msg(messages[i + 1])):

            # Look ahead for consecutive cycles
            cycles = [(messages[i], messages[i + 1])]
            j = i + 2
            while (j + 1 < len(messages) and
                   _is_pure_tool_use_msg(messages[j]) and
                   _is_cleared_tool_result_msg(messages[j + 1])):
                cycles.append((messages[j], messages[j + 1]))
                j += 2

            if len(cycles) > 1:
                tool_names = []
                for assistant_msg, _ in cycles:
                    content = assistant_msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if block.get("type") == "tool_use":
                                tool_names.append(block.get("name", "?"))
                    elif isinstance(content, dict) and content.get("type") == "tool_use":
                        tool_names.append(content.get("name", "?"))

                # Use sorted unique tool names for deterministic output
                tool_names_sorted = sorted(set(tool_names))
                compressed.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"[Previous tool calls: {', '.join(tool_names_sorted)}]"}],
                })
                compressed.append({
                    "role": "user",
                    "content": [{"type": "text", "text": "[tool results cleared]"}],
                })
                stats["merged_cycles"] += len(cycles) - 1
                stats["merged_msgs"] += len(cycles) * 2 - 2
                stats["saved_overhead"] += (len(cycles) * 2 - 2) * 50
                i = j
                continue

        # Fallback: merge consecutive user messages with only cleared tool_results
        if _is_cleared_tool_result_msg(messages[i]):
            cleared_run = [messages[i]]
            j = i + 1
            while j < len(messages) and _is_cleared_tool_result_msg(messages[j]):
                cleared_run.append(messages[j])
                j += 1

            if len(cleared_run) > 1:
                merged_blocks = []
                for m in cleared_run:
                    content = m.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if block.get("type") == "tool_result":
                                merged_blocks.append(block)
                    elif isinstance(content, dict) and content.get("type") == "tool_result":
                        merged_blocks.append(content)

                merged_msg = {
                    "role": "user",
                    "content": merged_blocks,
                }
                compressed.append(merged_msg)
                stats["merged_cycles"] += len(cleared_run) - 1
                stats["merged_msgs"] += len(cleared_run) - 1
                stats["saved_overhead"] += (len(cleared_run) - 1) * 50
                i = j
                continue

        compressed.append(messages[i])
        i += 1

    if stats["merged_cycles"] > 0:
        return compressed, {"enabled": True, "merged": True, **stats}
    return messages, {"enabled": True, "merged": False}


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
    # macOS: Free is always tiny; Inactive is reclaimable cache.
    # Show meaningful metrics: true used (wired+active) vs available (free+inactive).
    true_used = data.get("wired_gb", 0) + data.get("active_gb", 0)
    available = data.get("free_gb", 0) + data.get("inactive_gb", 0)
    data["total_gb"] = total
    data["used_gb"] = true_used          # Wired + Active (truly in use)
    data["available_gb"] = available     # Free + Inactive (reclaimable)
    data["used_pct"] = f"{true_used/total*100:.1f}"
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


def _get_cache_stats():
    """Parse backend log for prefix cache HIT/MISS since current startup.
    Returns {"hit": N, "miss": N, "total": N, "rate_str": "X.X%"}.
    Cloud backends return zeros."""
    if IS_CLOUD:
        return {"hit": 0, "miss": 0, "total": 0, "rate_str": "N/A"}
    try:
        with open(_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except (OSError, IOError):
        return {"hit": 0, "miss": 0, "total": 0, "rate_str": "N/A"}

    # Find the most recent startup (MemoryAwarePrefixCache initialized)
    start_idx = 0
    for i, line in enumerate(lines):
        if "MemoryAwarePrefixCache initialized" in line:
            start_idx = i

    hit = miss = 0
    for line in lines[start_idx:]:
        if "cache_fetch" in line:
            if "HIT" in line:
                hit += 1
            elif "MISS" in line:
                miss += 1
    total = hit + miss
    rate = (hit / total * 100) if total > 0 else 0
    return {"hit": hit, "miss": miss, "total": total, "rate_str": f"{rate:.1f}%"}


def _get_traffic_stats():
    """Read proxy_requests.jsonl and compute traffic metrics + anomaly detection."""
    try:
        with open(_JSONL_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, IOError):
        return _empty_traffic_stats()

    if not lines:
        return _empty_traffic_stats()

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts_str = rec.get("start_time", "") or rec.get("timestamp", "") or rec.get("end_time", "")
            if ts_str:
                try:
                    rec["_ts"] = datetime.fromisoformat(ts_str)
                except ValueError:
                    continue
            else:
                continue
            records.append(rec)
        except json.JSONDecodeError:
            continue

    if not records:
        return _empty_traffic_stats()

    now = datetime.now()
    records_1h = [r for r in records if (now - r["_ts"]).total_seconds() <= 3600]
    records_10m = [r for r in records_1h if (now - r["_ts"]).total_seconds() <= 600]

    if not records_1h:
        return _empty_traffic_stats()

    def _stats(recs):
        if not recs:
            return {}
        durations = [r.get("duration_ms", 0) for r in recs]
        durations.sort()
        inputs = [r.get("input_chars", 0) for r in recs]
        outputs = [r.get("output_chars", 0) for r in recs]
        statuses = [r.get("status", 200) for r in recs]
        n = len(recs)
        return {
            "count": n,
            "avg_latency_ms": round(sum(durations) / n, 1) if n else 0,
            "p50_latency_ms": durations[n // 2] if n else 0,
            "p95_latency_ms": durations[int(n * 0.95)] if n else 0,
            "max_latency_ms": round(max(durations), 1) if durations else 0,
            "avg_input_chars": round(sum(inputs) / n, 0) if n else 0,
            "avg_output_chars": round(sum(outputs) / n, 0) if n else 0,
            "max_input_chars": max(inputs) if inputs else 0,
            "max_output_chars": max(outputs) if outputs else 0,
            "success_rate": round(sum(1 for s in statuses if s == 200) / n * 100, 1) if n else 100.0,
        }

    stats_1h = _stats(records_1h)
    stats_10m = _stats(records_10m)

    # --- Anomaly detection ---
    alerts = []
    # Duplicate requests: same input_chars within same second
    sec_to_inputs = {}
    for r in records_10m:
        sec_key = r["_ts"].strftime("%H:%M:%S")
        sec_to_inputs.setdefault(sec_key, []).append(r.get("input_chars", 0))
    for sec_key, inputs in sec_to_inputs.items():
        from collections import Counter
        c = Counter(inputs)
        for inp_chars, cnt in c.items():
            if cnt >= 2:
                alerts.append(("warn", f"重复请求: {sec_key} 内 {cnt} 个请求 input_chars={inp_chars:,}"))
    # Oversized requests
    for r in records_10m:
        inp = r.get("input_chars", 0)
        if inp > 100000:
            alerts.append(("warn", f"超大报文: {r['_ts'].strftime('%H:%M:%S')} input_chars={inp:,}"))
    # Slow requests
    for r in records_10m:
        dur = r.get("duration_ms", 0)
        if dur > 60000:
            et = r.get("end_time", "")
            et_short = datetime.fromisoformat(et).strftime('%H:%M:%S') if et else "?"
            alerts.append(("warn", f"超长耗时: {r['_ts'].strftime('%H:%M:%S')}→{et_short} {dur/1000:.1f}s"))
    # Very slow requests (critical)
    for r in records_10m:
        dur = r.get("duration_ms", 0)
        if dur > 120000:
            et = r.get("end_time", "")
            et_short = datetime.fromisoformat(et).strftime('%H:%M:%S') if et else "?"
            alerts.append(("critical", f"严重超时: {r['_ts'].strftime('%H:%M:%S')}→{et_short} {dur/1000:.1f}s"))

    # Latency distribution buckets for visualization
    all_durations = [r.get("duration_ms", 0) for r in records_1h]
    buckets = [
        ("<5s", 0), ("5-15s", 0), ("15-30s", 0),
        ("30-60s", 0), ("60-120s", 0), (">120s", 0),
    ]
    for d in all_durations:
        if d < 5000:
            buckets[0] = (buckets[0][0], buckets[0][1] + 1)
        elif d < 15000:
            buckets[1] = (buckets[1][0], buckets[1][1] + 1)
        elif d < 30000:
            buckets[2] = (buckets[2][0], buckets[2][1] + 1)
        elif d < 60000:
            buckets[3] = (buckets[3][0], buckets[3][1] + 1)
        elif d < 120000:
            buckets[4] = (buckets[4][0], buckets[4][1] + 1)
        else:
            buckets[5] = (buckets[5][0], buckets[5][1] + 1)

    return {
        "stats_1h": stats_1h,
        "stats_10m": stats_10m,
        "alerts": alerts,
        "latency_buckets": buckets,
        "last_record_time": records[-1]["_ts"].strftime("%H:%M:%S") if records else "—",
    }


def _empty_traffic_stats():
    return {
        "stats_1h": {},
        "stats_10m": {},
        "alerts": [],
        "latency_buckets": [("<5s", 0), ("5-15s", 0), ("15-30s", 0), ("30-60s", 0), ("60-120s", 0), (">120s", 0)],
        "last_record_time": "—",
    }


def _get_session_trace():
    """Parse /tmp/anthropic_request_body.json and build an HTML snippet showing
    the semantic message timeline (roles, tool calls, text previews, errors).
    Returns (html_str, tools_list) where tools_list is [(msg_idx, name, params), ...]
    for modal popup display."""
    try:
        with open("/tmp/anthropic_request_body.json", "r", encoding="utf-8") as f:
            body = json.load(f)
    except (OSError, json.JSONDecodeError):
        return '<div class="evt">No active request body</div>', [], []

    try:
        mtime = os.path.getmtime("/tmp/anthropic_request_body.json")
        saved_at = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
    except OSError:
        saved_at = None

    msgs = body.get("messages", [])
    model = body.get("model", "unknown")
    max_tokens = body.get("max_tokens", "?")
    total_chars = len(json.dumps(msgs, ensure_ascii=False)) if msgs else 0

    # Count roles and tool actions
    user_count = sum(1 for m in msgs if m.get("role") == "user")
    assistant_count = sum(1 for m in msgs if m.get("role") == "assistant")
    tool_uses = 0
    tool_results = 0
    errors = 0
    # Collect detailed tool use and error info for modal
    tools_detail = []
    errors_detail = []
    for idx, m in enumerate(msgs):
        content = m.get("content", [])
        if isinstance(content, list):
            for c in content:
                if c.get("type") == "tool_use":
                    tool_uses += 1
                    name = c.get("name", "?")
                    inp = c.get("input", {})
                    params = ", ".join(f"{k}={v!r}" for k, v in list(inp.items())[:4])
                    if len(inp) > 4:
                        params += ", ..."
                    tools_detail.append((saved_at or "—", f"Msg {idx}: {name}({params})"))
                elif c.get("type") == "tool_result":
                    tool_results += 1
                    tr = c.get("content", "")
                    err_text = ""
                    if isinstance(tr, str) and "tool_use_error" in tr:
                        errors += 1
                        err_text = tr[:120]
                    elif isinstance(tr, list) and tr:
                        t = tr[0].get("text", "")
                        if "tool_use_error" in str(t):
                            errors += 1
                            err_text = str(t)[:120]
                    if err_text:
                        err_summary = err_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        errors_detail.append((saved_at or "—", f"Msg {idx}: {err_summary}"))

    # Build timeline HTML (last 8 messages)
    timeline = []
    ts_html = f'<span class="evt-ts">{saved_at}</span> ' if saved_at else ''
    for idx, m in enumerate(msgs):
        if idx < len(msgs) - 8:
            continue
        role = m.get("role", "?")
        content = m.get("content", [])
        prefix = f"Msg {idx}"
        line = ""
        if isinstance(content, list):
            texts = []
            tools = []
            has_error = False
            for c in content:
                ctype = c.get("type", "")
                if ctype == "text":
                    t = c.get("text", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    texts.append(t[:60] + ("..." if len(t) > 60 else ""))
                elif ctype == "tool_use":
                    name = c.get("name", "?")
                    inp = c.get("input", {})
                    # Show a key param preview
                    preview = ""
                    if isinstance(inp, dict):
                        for k in ("command", "file_path", "subject", "description", "old_string"):
                            if k in inp:
                                v = str(inp[k])[:40]
                                preview = f" {k}={v}"
                                break
                    tools.append(f"{name}{preview}")
                elif ctype == "tool_result":
                    tr = c.get("content", "")
                    if isinstance(tr, str) and "tool_use_error" in tr:
                        has_error = True
                    elif isinstance(tr, list) and tr:
                        t = tr[0].get("text", "")
                        if "tool_use_error" in str(t):
                            has_error = True
            parts = []
            if texts:
                parts.append(texts[0])
            if tools:
                parts.append(" | ".join(tools))
            if has_error:
                parts.append("❌ ERROR")
            line = " | ".join(parts) if parts else "[empty]"
        else:
            t = str(content).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:60]
            line = t + ("..." if len(str(content)) > 60 else "")

        role_color = "#3498db" if role == "user" else ("#2ecc71" if role == "assistant" else "#888")
        timeline.append(
            f'<div class="evt">{ts_html}<span style="color:{role_color};font-weight:600;">{prefix} ({role})</span> {line}</div>'
        )

    if not timeline:
        timeline.append('<div class="evt">No messages</div>')

    summary = (
        f'<div class="row"><span class="label">Messages</span>'
        f'<span class="value">{len(msgs)}</span></div>'
        f'<div class="row"><span class="label">Model</span>'
        f'<span class="value">{model}</span></div>'
        f'<div class="row"><span class="label">Max Tokens</span>'
        f'<span class="value">{max_tokens}</span></div>'
        f'<div class="row"><span class="label">Total Chars</span>'
        f'<span class="value">{total_chars:,}</span></div>'
        f'<div class="row"><span class="label">User / Assistant</span>'
        f'<span class="value">{user_count} / {assistant_count}</span></div>'
        f'<div class="row"><span class="label">Tool Uses</span>'
        f'<span class="value clickable" onclick="showModal(\'tools\', \'🔧 Tool Calls Detail\')">{tool_uses}</span></div>'
        f'<div class="row"><span class="label">Errors</span>'
        f'<span class="value clickable" style="color:{"#e74c3c" if errors else "#2ecc71"}" onclick="showModal(' + "'errors', '❌ Errors Detail')" + f'">{errors}</span></div>'
    )
    if saved_at:
        summary += f'<div class="row"><span class="label">Captured At</span><span class="value">{saved_at}</span></div>'

    return summary + "\n".join(timeline), tools_detail, errors_detail


def _build_status_html():
    backend_info = _get_process_info("rapid-mlx|llama-server", "Backend")
    proxy_info = _get_process_info("anthropic_proxy.py", "Proxy", fallback_port=4000)
    mem = _get_system_memory()
    log = _get_log_stats()
    traffic = _get_traffic_stats()
    session_trace, tools_detail, errors_detail = _get_session_trace()
    cache_stats = _get_cache_stats()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    backend_color = "#2ecc71" if backend_info.get("running") else "#e74c3c"
    cache_rate_color = "#888"
    if cache_stats["total"] > 0:
        rate = cache_stats["hit"] / cache_stats["total"] * 100
        cache_rate_color = "#2ecc71" if rate >= 50 else "#f39c12" if rate >= 20 else "#e74c3c"
    proxy_color = "#2ecc71" if proxy_info.get("running") else "#e74c3c"
    mem_warn = float(mem.get("used_pct", 0)) > 75
    mem_color = "#e74c3c" if mem_warn else "#2ecc71"

    # --- Traffic Stats card ---
    s1h = traffic.get("stats_1h", {})
    s10m = traffic.get("stats_10m", {})
    qps = round(s10m.get("count", 0) / 600, 3) if s10m.get("count") else 0
    traffic_card = f"""<div class="card">
    <h2>📊 Traffic Stats</h2>
    <div class="row"><span class="label">Requests (1h / 10m)</span><span class="value">{s1h.get("count", 0)} / {s10m.get("count", 0)}</span></div>
    <div class="row"><span class="label">Avg Latency</span><span class="value">{s1h.get("avg_latency_ms", 0)/1000:.1f}s</span></div>
    <div class="row"><span class="label">P95 Latency</span><span class="value">{s1h.get("p95_latency_ms", 0)/1000:.1f}s</span></div>
    <div class="row"><span class="label">Max Latency</span><span class="value">{s1h.get("max_latency_ms", 0)/1000:.1f}s</span></div>
    <div class="row"><span class="label">Avg In / Out</span><span class="value">{s1h.get("avg_input_chars", 0):.0f} / {s1h.get("avg_output_chars", 0):.0f} chars</span></div>
    <div class="row"><span class="label">Max In / Out</span><span class="value">{s1h.get("max_input_chars", 0):,.0f} / {s1h.get("max_output_chars", 0):,.0f}</span></div>
    <div class="row"><span class="label">Success Rate</span><span class="value" style="color:{"#2ecc71" if s1h.get("success_rate", 100) >= 95 else "#f39c12" if s1h.get("success_rate", 100) >= 80 else "#e74c3c"}">{s1h.get("success_rate", 100):.1f}%</span></div>
    <div class="row"><span class="label">Est. QPS (10m)</span><span class="value">{qps:.3f}</span></div>
    <div class="row"><span class="label">Last Record</span><span class="value">{traffic.get("last_record_time", "—")}</span></div>
  </div>"""

    # --- Alerts card ---
    alerts = traffic.get("alerts", [])
    if alerts:
        alerts_html = ""
        for severity, msg in alerts:
            color = "#e74c3c" if severity == "critical" else "#f39c12"
            icon = "🔴" if severity == "critical" else "⚠️"
            alerts_html += f'<div class="evt"><span style="color:{color};font-weight:600;">{icon} {msg}</span></div>'
    else:
        alerts_html = '<div class="evt" style="color:#2ecc71;">✅ No anomalies detected (last 10m)</div>'

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
        "tools": tools_detail,
        "errors": errors_detail,
    })
    # Escape </script> inside <script> to prevent premature tag closure
    # when eventData contains nested HTML/JS (e.g. Write tool content).
    modal_data = modal_data.replace("</script>", "<\\/script>")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<!-- auto-refresh disabled when modal is open -->
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
    <div class="row"><span class="label">Used (Wired+Active)</span><span class="value" style="color:{mem_color}">{mem.get("used_gb", 0):.1f} GB ({mem.get("used_pct", "0")}%)</span></div>
    <div class="row"><span class="label">Available</span><span class="value">{mem.get("available_gb", 0):.1f} GB (Free+Inactive)</span></div>
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
    <div class="row"><span class="label">Prefix Cache</span><span class="value" style="color:{cache_rate_color}">{cache_stats["hit"]}/{cache_stats["total"]} ({cache_stats["rate_str"]})</span></div>
    <div class="row"><span class="label">Config</span><span class="value">CLEAR={'on' if PROXY_CLEAR_ENABLED else 'off'}, LIMIT={'on' if PROXY_CTX_LIMIT_ENABLED else 'off'}, MAX_CONCURRENT={PROXY_MAX_CONCURRENT}</span></div>
    <div class="row"><span class="label">Model</span><span class="value">{MODEL_NAME}</span></div>
  </div>

  {traffic_card}

  <div class="card" style="grid-column: 1 / -1;">
    <h2>🚨 Alerts (last 10m)</h2>
    {alerts_html}
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>Session Trace</h2>
    {session_trace}
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
setInterval(function() {{
  if (document.getElementById('modal').style.display !== 'flex') {{
    location.reload();
  }}
}}, 5000);
</script>
</body>
</html>"""
    return html


def _finalize_metrics(mc):
    pipeline = mc.get("pipeline", {})
    quality_flags = []
    trunc = pipeline.get("truncate", {})
    if trunc.get("triggered"):
        dropped = trunc.get("dropped", 0)
        kept = trunc.get("kept", 0)
        if kept + dropped > 0 and dropped / (dropped + kept) > 0.7:
            quality_flags.append("high_drop_ratio")
        if trunc.get("compression") in ("rules", "folded") and dropped >= 10:
            quality_flags.append("llm_compress_failed")
        est_after = trunc.get("est_tokens_after", 0)
        budget = trunc.get("budget", 0)
        if budget > 0 and est_after > budget * 1.1:
            quality_flags.append("budget_overflow")
    loop = pipeline.get("loop_detect", {})
    if loop.get("max_run", 0) >= PROXY_LOOP_THRESHOLD:
        quality_flags.append("loop_injected")
    blocker = pipeline.get("blocker_detect", {})
    if blocker.get("triggered"):
        quality_flags.append("blocker_injected")
    mc["quality_flags"] = quality_flags
    input_est = mc.get("input_chars", 0) / max(PROXY_CTX_TOKEN_RATIO, 0.1)
    est_after = trunc.get("est_tokens_after", input_est) if trunc.get("triggered") else input_est
    if input_est > 0:
        mc["compression_ratio"] = round(est_after / input_est, 2)
    else:
        mc["compression_ratio"] = 1.0


def _mc_put(step_key, data):
    mc = getattr(_metrics_ctx, 'mc', None)
    if mc and PROXY_METRICS_ENABLED:
        mc["pipeline"][step_key] = data


def _filter_tools(tools, messages, recent_rounds=5, tool_choice_name=None):
    if not tools or len(tools) <= PROXY_TOOL_FILTER_MAX:
        return tools, {"filtered": False, "reason": "below_max"}

    recent_tools = set()
    assistant_count = 0
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            assistant_count += 1
            content = msg.get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        recent_tools.add(b.get("name", ""))
            if assistant_count >= recent_rounds:
                break

    keep_set = TOOL_ALWAYS_KEEP | recent_tools
    if tool_choice_name:
        keep_set.add(tool_choice_name)

    kept = [t for t in tools if isinstance(t, dict) and t.get("name", "") in keep_set]

    if len(kept) < 5:
        return tools, {"filtered": False, "reason": "too_few_after_filter"}

    return kept, {
        "filtered": True,
        "original": len(tools),
        "kept": len(kept),
        "always_keep": len(TOOL_ALWAYS_KEEP & {t.get("name", "") for t in kept}),
        "recent_only": len(recent_tools - TOOL_ALWAYS_KEEP),
    }


def _extract_keywords(messages):
    keywords = {}
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        text = ""
        files = []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text += b.get("text", "") + " "
                elif b.get("type") == "tool_use":
                    name = b.get("name", "")
                    inp = b.get("input", {})
                    if isinstance(inp, dict):
                        for k in ("file_path", "path", "directory"):
                            fp = inp.get(k, "")
                            if fp:
                                files.append(fp)
                    text += f"{name} "
                elif b.get("type") == "tool_result":
                    tc = b.get("content", "")
                    if isinstance(tc, str):
                        text += tc[:200] + " "
                    elif isinstance(tc, list):
                        for tb in tc:
                            if isinstance(tb, dict):
                                text += str(tb.get("text", ""))[:200] + " "
        summary = f"{role}: {text[:100].strip()}"
        for path in files:
            fname = path.split("/")[-1]
            keywords.setdefault(fname, []).append(summary)
        for err in re.findall(r'\b([A-Z]\w*(?:Error|Exception))\b', text):
            keywords.setdefault(err, []).append(summary)
        for func in re.findall(r'\b([a-z][a-zA-Z0-9_]{3,})\s*\(', text):
            keywords.setdefault(func, []).append(summary)
    return keywords


def _inject_keyword_context(keywords, current_messages, top_k=5, max_chars=500):
    query_text = ""
    for msg in list(reversed(current_messages))[:3]:
        content = msg.get("content", "")
        if isinstance(content, str):
            query_text += content + " "
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    query_text += b.get("text", "") + " "
    if not query_text.strip():
        return None
    matches = []
    seen = set()
    for kw, entries in keywords.items():
        if kw.lower() in query_text.lower():
            for entry in entries:
                if entry not in seen:
                    seen.add(entry)
                    matches.append(f"[{kw}]: {entry}")
    if not matches:
        return None
    matches = matches[:top_k]
    result = "[Relevant history context:]\n" + "\n".join(f"- {m}" for m in matches)
    if len(result) > max_chars:
        result = result[:max_chars] + "..."
    return result


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
        _log_ctx.session_id = self.headers.get("X-Claude-Code-Session-Id", "")[:8] or None
        try:
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
        finally:
            _log_ctx.session_id = None

    def do_POST(self):
        _log_ctx.session_id = self.headers.get("X-Claude-Code-Session-Id", "")[:8] or None
        if PROXY_METRICS_ENABLED:
            _metrics_ctx.mc = {
                "ts": datetime.now().isoformat(),
                "session_id": getattr(_log_ctx, 'session_id', None) or "",
                "pipeline": {},
            }
        try:
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
                _req_start_time = datetime.now().isoformat()
                self._last_jsonl_token = _next_jsonl_token()
                _jsonl_output_map[self._last_jsonl_token] = 0
                try:
                    self._handle_messages(parsed)
                    _dur = (_time.monotonic() - _t0) * 1000
                    _out_chars = _jsonl_output_map.pop(self._last_jsonl_token, 0)
                    log_request(
                        model=parsed.get("model", "unknown"),
                        input_chars=total_chars,
                        output_chars=_out_chars,
                        status=200,
                        duration_ms=_dur,
                        start_time=_req_start_time,
                    )
                    if PROXY_METRICS_ENABLED:
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["output_chars"] = _out_chars
                            mc["duration_ms"] = round(_dur, 1)
                            mc["status"] = 200
                            _finalize_metrics(mc)
                            log_metrics(mc)
                except Exception as e:
                    _dur = (_time.monotonic() - _t0) * 1000
                    log(f"  -> Error: {e}")
                    _jsonl_output_map.pop(self._last_jsonl_token, None)
                    log_request(
                        model=parsed.get("model", "unknown"),
                        input_chars=total_chars,
                        output_chars=0,
                        status=500,
                        duration_ms=_dur,
                        start_time=_req_start_time,
                    )
                    if PROXY_METRICS_ENABLED:
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["output_chars"] = 0
                            mc["duration_ms"] = round(_dur, 1)
                            mc["status"] = 500
                            mc["error"] = str(e)[:200]
                            _finalize_metrics(mc)
                            log_metrics(mc)
                    raise
            else:
                log(f"  -> 404 (unknown path)")
                self._respond_json({"detail": "Not found"}, 404)
        finally:
            _log_ctx.session_id = None
            if PROXY_METRICS_ENABLED:
                _metrics_ctx.mc = None

    def _handle_messages(self, body):
        is_stream = body.get("stream", False)
        model = body.get("model", "unknown")
        _req_start_time = datetime.now().isoformat()
        max_tokens_orig = body.get("max_tokens", 4096)

        # REQ_SUMMARY with session_id
        tools_list = None
        raw_tools = body.get("tools", [])
        if raw_tools:
            tools_list = [t.get("name", "") for t in raw_tools if isinstance(t, dict)]
        session_prefix = getattr(_log_ctx, 'session_id', None) or ""
        total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in body.get("messages", []))
        log(f"  [REQ_SUMMARY] chars={total_chars} tools={len(tools_list or [])}")

        if PROXY_METRICS_ENABLED:
            mc = getattr(_metrics_ctx, 'mc', None)
            if mc:
                mc["input_msgs"] = len(body.get("messages", []))
                mc["input_chars"] = total_chars
                mc["input_tools"] = len(tools_list or [])

        log(f"  -> Handling model={model}, stream={is_stream}")

        # Backend timeout & output token limit logging
        log(f"  -> Backend timeout: {PROXY_BACKEND_TIMEOUT}s, output token limit: {PROXY_OUTPUT_TOKEN_LIMIT_RATIO}x max_tokens, max_tokens override: {PROXY_MAX_TOKENS_OVERRIDE}")

        # max_tokens override
        if PROXY_MAX_TOKENS_OVERRIDE > 0 and max_tokens_orig > PROXY_MAX_TOKENS_OVERRIDE:
            body["max_tokens"] = PROXY_MAX_TOKENS_OVERRIDE
            log(f"  -> max_tokens override: {max_tokens_orig} -> {PROXY_MAX_TOKENS_OVERRIDE}")

        # Error translation: intercept tool_result errors and rewrite to natural language
        error_count = {"wasted": 0, "file_not_found": 0, "input_validation": 0}
        raw_messages = body.get("messages", [])
        for msg in raw_messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") != "tool_result":
                        continue
                    bc = str(block.get("content", ""))
                    if "Wasted call" in bc:
                        block["content"] = (
                            "[System: 该文件自上次读取后未发生变化，不要再使用 Read 工具反复读取。"
                            "如果需要查看文件内容，用 Bash cat 命令代替。]"
                        )
                        error_count["wasted"] += 1
                    elif "File does not exist" in bc or "No such file" in bc:
                        block["content"] = (
                            "[System: 文件不存在。请先用 Bash ls 或 find 命令确认项目结构，"
                            "然后使用正确的文件路径。]"
                        )
                        error_count["file_not_found"] += 1
                    elif "InputValidationError" in bc or "invalid x" in bc.lower():
                        block["content"] = (
                            "[System: 工具调用参数错误。请检查工具参数格式，"
                            "确保所有必填参数正确提供。]"
                        )
                        error_count["input_validation"] += 1
        total_errors = sum(error_count.values())
        if total_errors > 0:
            log(f"  -> Error translation: {total_errors} tool_result errors rewritten "
                f"(wasted={error_count['wasted']}, file_not_found={error_count['file_not_found']}, "
                f"input_validation={error_count['input_validation']})")
            _mc_put("error_translation", {"count": total_errors, **error_count})

        # Proxy-side tool-result clearing with semantic priority
        raw_messages, clear_stats = clear_old_tool_results(raw_messages, tools_list=tools_list)
        cleared_files = clear_stats.get("cleared_files", [])
        if PROXY_METRICS_ENABLED and clear_stats.get("cleared"):
            _mc_put("tool_clear", {
                "cleared": clear_stats.get("cleared_tool_results", 0),
                "kept": clear_stats.get("kept", 0),
                "chars_freed": clear_stats.get("cleared_chars", 0),
            })
        if clear_stats.get("cleared"):
            log(f"  -> Tool clearing: {clear_stats['cleared_tool_results']} tool_results cleared, "
                f"{clear_stats['cleared_chars']:,} chars freed (kept {clear_stats['kept']}, high_prio={clear_stats.get('high_prio', 0)})")
            if clear_stats.get("dedup_bash", 0) > 0:
                log(f"  -> Bash dedup: {clear_stats['dedup_bash']} similar results merged, "
                    f"{clear_stats['dedup_chars_saved']} chars saved")
        elif not clear_stats.get("enabled"):
            log(f"  -> Tool clearing: disabled ({BACKEND_TYPE} backend)")
        elif clear_stats.get("enabled") and not clear_stats.get("skipped"):
            log(f"  -> Tool clearing: active (threshold={PROXY_CLEAR_THRESHOLD}, keep={PROXY_TOOL_KEEP})")

        # Consecutive calls tracking (max_run)
        # Track both: (A) identical args per tool, (B) same text+tool_name pattern
        consecutive = {}
        max_run = 0
        pattern_run = 0
        last_pattern = None
        pattern_tool_name = None
        for msg in raw_messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                tool_names_in_msg = []
                text_parts = []
                for block in content:
                    if block.get("type") == "tool_use":
                        name = block.get("name", "")
                        tool_names_in_msg.append(name)
                        inp = block.get("input", {})
                        args_str = json.dumps(inp, sort_keys=True, ensure_ascii=False) if isinstance(inp, dict) else str(inp)
                        key = f"{name}:{args_str}"
                        consecutive[key] = consecutive.get(key, 0) + 1
                        max_run = max(max_run, consecutive[key])
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                for _ in tool_names_in_msg:
                    consecutive = {}
                pattern = ("".join(text_parts)[:200], tuple(sorted(set(tool_names_in_msg))))
                if pattern == last_pattern and pattern[1]:
                    pattern_run += 1
                    if pattern_run > max_run:
                        max_run = pattern_run
                        pattern_tool_name = tool_names_in_msg[0] if tool_names_in_msg else "unknown"
                    for k in list(consecutive.keys()):
                        if not k.startswith(pattern_tool_name + ":"):
                            del consecutive[k]
                else:
                    pattern_run = 1
                    last_pattern = pattern
            else:
                consecutive = {}
                pattern_run = 0
                last_pattern = None
        if max_run > 1:
            log(f"  -> Consecutive calls: max_run={max_run} (tools tracked)")
        _mc_put("loop_detect", {"max_run": max_run})

        # Loop detection: escalating intervention based on severity
        if max_run >= PROXY_LOOP_THRESHOLD:
            loop_keys = [k for k, v in consecutive.items() if v >= PROXY_LOOP_THRESHOLD]
            tool_name = loop_keys[0].split(":")[0] if loop_keys else (pattern_tool_name or "unknown")

            if max_run >= PROXY_LOOP_LEVEL3:
                # Level 3 (severe): suppress tool_use in last assistant, force text-only
                log(f"  -> LOOP LEVEL 3: {tool_name} called {max_run} times, suppressing tool calls in last assistant")
                for msg in reversed(raw_messages):
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            new_content = []
                            suppressed = 0
                            for block in content:
                                if block.get("type") == "tool_use":
                                    suppressed += 1
                                else:
                                    new_content.append(block)
                            if suppressed > 0:
                                new_content.append({
                                    "type": "text",
                                    "text": f"[System: {suppressed} tool call(s) suppressed due to loop detection "
                                            f"(tool: {tool_name}, repeated {max_run} times). "
                                            f"Respond with text only — describe what you know and what to do next.]"
                                })
                                msg["content"] = new_content
                        break
                raw_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text":
                        f"[CRITICAL SYSTEM NOTICE: You are in an infinite loop. "
                        f"Tool '{tool_name}' has been called {max_run} times with no progress. "
                        f"The tool is now DISABLED. You MUST respond with text only. "
                        f"Summarize what you have learned and propose next steps WITHOUT any tool calls.]"
                    }]
                })
                loop_level = 3
            elif max_run >= PROXY_LOOP_LEVEL2:
                # Level 2 (medium): remove looping tool from tools list + strong message
                log(f"  -> LOOP LEVEL 2: {tool_name} called {max_run} times, removing tool + injecting strong break")
                removed_count = 0
                if raw_tools:
                    filtered_tools = []
                    for t in raw_tools:
                        if isinstance(t, dict) and t.get("name", "") == tool_name:
                            removed_count += 1
                        else:
                            filtered_tools.append(t)
                    if removed_count > 0:
                        body["tools"] = filtered_tools
                        log(f"    removed {tool_name} from tools list ({len(filtered_tools)} remaining)")
                raw_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text":
                        f"[IMPORTANT: You have called '{tool_name}' {max_run} times in a row with no progress. "
                        f"This tool has been temporarily REMOVED from your available tools. "
                        f"You MUST use a different approach. "
                        f"If you need file content, use Bash with cat/head/tail. "
                        f"If you are stuck, describe the problem and ask for guidance.]"
                    }]
                })
                loop_level = 2
            else:
                # Level 1 (mild): enhanced break message
                log(f"  -> LOOP LEVEL 1: {tool_name} called {max_run} times, injecting break message")
                raw_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text":
                        f"[System notice: You have repeated the same action {max_run} times "
                        f"(tool: {tool_name}). This is likely a loop. "
                        f"STOP using {tool_name} immediately and try a completely different approach. "
                        f"If file content was cleared, assume the file is unchanged and work from memory.]"
                    }]
                })
                loop_level = 1
            _mc_put("loop_detect", {"max_run": max_run, "level": loop_level, "tool": tool_name})
        else:
            _mc_put("loop_detect", {"max_run": max_run})

        # Blocker detection: same-error-type consecutive tool_result rejections
        blocker_info = _detect_blocker_pattern(raw_messages)
        if blocker_info.get("triggered"):
            log(
                f"  -> Blocker detected: {blocker_info['tool_name']} failed "
                f"({blocker_info['error_type']}) {blocker_info['run_length']} times in a row, "
                f"injecting [BLOCKER] message"
            )
            raw_messages.append(_build_blocker_message(
                blocker_info["tool_name"],
                blocker_info["error_type"],
                blocker_info["run_length"],
            ))
        _mc_put("blocker_detect", blocker_info)

        # Re-read detection: check if recent tool_uses target cleared files
        if cleared_files:
            re_read_count = 0
            recent_msgs = raw_messages[-6:]
            for msg in recent_msgs:
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_use" and block.get("name") == "Read":
                            inp = block.get("input", {})
                            if isinstance(inp, dict):
                                fp = inp.get("file_path", inp.get("path", ""))
                                if fp in cleared_files:
                                    re_read_count += 1
            if re_read_count > 0:
                log(f"  -> Re-read detected: {re_read_count} Read calls targeting cleared files "
                    f"(cleared_files={len(cleared_files)})")

        # Normalize system-reminder date to stabilize prefix for KV cache hits
        if raw_messages and raw_messages[0].get("role") == "user":
            content = raw_messages[0].get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        new_text = re.sub(r"Today's date is \d{4}/\d{2}/\d{2}\.", "Today's date is DATE_PLACEHOLDER.", text)
                        if new_text != text:
                            block["text"] = new_text
                            log(f"  -> Standardized date in msg0 block")
            else:
                new_content = re.sub(r"Today's date is \d{4}/\d{2}/\d{2}\.", "Today's date is DATE_PLACEHOLDER.", str(content))
                if new_content != content:
                    raw_messages[0]["content"] = new_content
                    log(f"  -> Standardized date in msg0")

        # Proxy-side thinking block stripping
        raw_messages, think_stats = strip_old_thinking_blocks(raw_messages, keep_recent=3)
        if think_stats.get("stripped"):
            log(f"  -> Thinking stripped: {think_stats['stripped_count']} old assistant messages cleaned (kept last {think_stats['kept']})")
            _mc_put("think_strip", {"stripped": think_stats["stripped_count"]})
        elif think_stats.get("enabled") and not think_stats.get("skipped"):
            log(f"  -> Thinking strip: active (keep_recent=3)")

        # Proxy-side cleared tool-result compression
        raw_messages, compress_stats = compress_cleared_tool_results(raw_messages)
        if compress_stats.get("merged"):
            log(f"  -> Tool-result compression: {compress_stats['merged_cycles']} cycles merged, {compress_stats['merged_msgs']} msgs removed (saved ~{compress_stats['saved_overhead']} tokens overhead)")
            _mc_put("compress", {"merged_cycles": compress_stats["merged_cycles"], "msgs_removed": compress_stats["merged_msgs"]})
        elif compress_stats.get("enabled") and not compress_stats.get("merged"):
            log(f"  -> Tool-result compression: no consecutive cleared cycles to merge")

        # Context truncation
        raw_messages, trunc_stats = truncate_messages_if_needed(
            raw_messages,
            session_id=getattr(_log_ctx, 'session_id', None),
        )
        if trunc_stats.get("truncated"):
            strategy = trunc_stats.get("strategy", "char")
            trunc_metrics = {
                "triggered": True,
                "strategy": strategy,
                "dropped": trunc_stats.get("dropped_messages", 0),
                "kept": trunc_stats.get("kept_messages", 0),
            }
            if strategy == "rounds":
                est_tok = trunc_stats.get("estimated_tokens", "?")
                actual_r = trunc_stats.get("actual_keep_rounds", "?")
                comp = trunc_stats.get("compression", "folded")
                adaptive = trunc_stats.get("adaptive_rounds", "")
                extra_info = f", adaptive={adaptive}" if adaptive else ""
                log(f"  -> Context truncation (rounds): {trunc_stats['dropped_messages']} messages dropped, {trunc_stats.get('kept_messages', '?')} kept (rounds={actual_r}, ~{est_tok} tokens, budget={PROXY_CTX_TOKEN_BUDGET}, compress={comp}{extra_info})")
                trunc_metrics["compression"] = comp
                trunc_metrics["est_tokens_after"] = trunc_stats.get("estimated_tokens", 0)
                trunc_metrics["budget"] = PROXY_CTX_TOKEN_BUDGET
                trunc_metrics["rounds"] = actual_r
                trunc_metrics["adaptive_rounds"] = adaptive
            elif strategy == "fifo":
                log(f"  -> Context truncation (fifo): {trunc_stats['dropped_messages']} messages dropped, {trunc_stats.get('kept_messages', '?')} kept (limit={PROXY_CTX_KEEP_MESSAGES})")
            else:
                log(f"  -> Context truncation (char): {trunc_stats['dropped_messages']} messages dropped, {trunc_stats['dropped_chars']:,} chars removed ({trunc_stats['chars_before']:,} -> {trunc_stats['chars_after']:,})")
            _mc_put("truncate", trunc_metrics)
        elif not trunc_stats.get("enabled"):
            log(f"  -> Context truncation: disabled ({BACKEND_TYPE} backend)")
        elif trunc_stats.get("enabled") and not trunc_stats.get("truncated") and not trunc_stats.get("skipped"):
            log(f"  -> Context truncation: active (strategy={trunc_stats.get('strategy', '?')})")

        # Debug: compute hashes of first two messages
        if raw_messages:
            def _msg_hash(m):
                c = m.get("content", "")
                if isinstance(c, list):
                    c = "".join(b.get("text", "") for b in c if b.get("type") == "text")
                elif not isinstance(c, str):
                    c = str(c)
                return hashlib.md5((m.get("role", "") + ":" + c).encode()).hexdigest()[:8]
            h0 = _msg_hash(raw_messages[0]) if len(raw_messages) > 0 else "none"
            h1 = _msg_hash(raw_messages[1]) if len(raw_messages) > 1 else "none"
            log(f"  -> Msg hashes: msg0={h0}, msg1={h1}, total_msgs={len(raw_messages)}")

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
        raw_tools = body.get("tools")
        if raw_tools and PROXY_TOOL_FILTER_ENABLED:
            tc_raw = body.get("tool_choice")
            tc_name = None
            if isinstance(tc_raw, dict) and tc_raw.get("type") == "tool":
                tc_name = tc_raw.get("name", "")
            raw_tools, tf_stats = _filter_tools(
                raw_tools, raw_messages,
                recent_rounds=PROXY_TOOL_FILTER_RECENT,
                tool_choice_name=tc_name,
            )
            if tf_stats.get("filtered"):
                body["tools"] = raw_tools
                log(f"  -> Tool filter: {tf_stats['original']} -> {tf_stats['kept']} "
                    f"(always={tf_stats['always_keep']}, recent={tf_stats['recent_only']})")
                _mc_put("tool_filter", tf_stats)
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
                resp = urllib.request.urlopen(req, timeout=PROXY_BACKEND_TIMEOUT)
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

        # Output token truncation for non-streaming path
        max_tokens = anthropic_body.get("max_tokens", 4096)
        output_token_hard_limit = int(max_tokens * PROXY_OUTPUT_TOKEN_LIMIT_RATIO)
        output_chars_limit = int(output_token_hard_limit / 0.4)
        output_chars = 0
        force_stopped = False

        for block in anthropic_resp.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                output_chars += len(text)
                if output_chars > output_chars_limit:
                    block["text"] = text[:output_chars_limit - (output_chars - len(text))]
                    block["text"] += "\n\n[Output truncated by proxy: exceeded token limit]"
                    force_stopped = True
                    output_chars = output_chars_limit
                    break

        if force_stopped:
            anthropic_resp["stop_reason"] = "max_tokens"
            log(f"  -> FORCE_STOPPED at {output_chars} chars (limit={output_chars_limit})")

        content_summary = ""
        for block in anthropic_resp.get("content", []):
            if block.get("type") == "text":
                content_summary += block.get("text", "")[:100]
                output_chars += len(block.get("text", ""))
            elif block.get("type") == "tool_use":
                content_summary += f"[tool_use: {block.get('name', '')}] "
                output_chars += len(json.dumps(block.get("input", {}), ensure_ascii=False))
        _jsonl_output_map[self._last_jsonl_token] = output_chars
        log(f"  <- Responding: {content_summary[:200]} (output_chars={output_chars})")
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

        # Output token truncation for streaming path
        max_tokens = anthropic_body.get("max_tokens", 4096)
        output_token_hard_limit = int(max_tokens * PROXY_OUTPUT_TOKEN_LIMIT_RATIO)
        output_char_count = 0
        output_force_stopped = False

        def _emit_text_delta(t):
            """Emit a text delta SSE event, opening the text block lazily."""
            nonlocal text_block_started, total_text, output_char_count, output_force_stopped
            if not t:
                return
            try:
                if not text_block_started:
                    text_block_started = True
                    self.wfile.write(
                        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                    )
                total_text += t
                output_char_count += len(t)
                ev = f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":{json.dumps(t)}}}}}\n\n'
                self.wfile.write(ev.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Client disconnected, stop emitting

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

            if output_force_stopped:
                break

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
                        args_chunk = tc["function"]["arguments"]
                        tool_calls_buffer[idx]["function"]["arguments"] += args_chunk
                        output_char_count += len(args_chunk)
                        est_tokens = output_char_count * 0.4
                        if est_tokens > output_token_hard_limit:
                            tool_name = tool_calls_buffer[idx]["function"].get("name", "?")
                            log(f"  -> !! Output token limit on tool_call: est={int(est_tokens)}, limit={output_token_hard_limit}, tool={tool_name}, forcing stop")
                            output_force_stopped = True
                            break
                continue

            # Check text output token limit
            if output_char_count * 0.4 > output_token_hard_limit:
                log(f"  -> FORCE_STOPPED: output est={int(output_char_count * 0.4)} tokens, limit={output_token_hard_limit}")
                output_force_stopped = True
                break

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

        # Repair truncated JSON in tool_call arguments if FORCE_STOPPED
        if output_force_stopped:
            for idx in tool_calls_buffer:
                tc = tool_calls_buffer[idx]
                raw_args = tc["function"].get("arguments", "{}")
                try:
                    json.loads(raw_args)
                except json.JSONDecodeError:
                    repaired = _repair_truncated_json(raw_args)
                    tc["function"]["arguments"] = repaired
                    tool_name = tc["function"].get("name", "?")
                    log(f"  -> Repaired truncated JSON for tool={tool_name}: {len(raw_args)} -> {len(repaired)} chars")

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
        try:
            self.wfile.write(f"event: message_delta\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

            # Send message_stop
            event = {"type": "message_stop"}
            self.wfile.write(f"event: message_stop\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected before stream end
        _jsonl_output_map[self._last_jsonl_token] = len(total_text)
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
    log(f"Blocker tracker: {'enabled (threshold=' + str(PROXY_BLOCKER_THRESHOLD) + ' consecutive)' if PROXY_BLOCKER_ENABLED else 'disabled (' + BACKEND_TYPE + ' backend)'}")
    _trunc_info = f"strategy={PROXY_CTX_TRUNCATE_STRATEGY}"
    if PROXY_CTX_TRUNCATE_STRATEGY == "rounds":
        _trunc_info += f", rounds={PROXY_CTX_KEEP_ROUNDS}, budget={PROXY_CTX_TOKEN_BUDGET}"
    elif PROXY_CTX_TRUNCATE_STRATEGY == "fifo":
        _trunc_info += f", keep_messages={PROXY_CTX_KEEP_MESSAGES}"
    else:
        _trunc_info += f", limit={PROXY_CTX_CHARS_LIMIT}"
    log(f"Context limit: {'enabled (' + _trunc_info + ')' if PROXY_CTX_LIMIT_ENABLED else 'disabled (' + BACKEND_TYPE + ' backend)'}")
    log(f"Backend timeout: {PROXY_BACKEND_TIMEOUT}s, output token limit: {PROXY_OUTPUT_TOKEN_LIMIT_RATIO}x max_tokens, max_tokens override: {PROXY_MAX_TOKENS_OVERRIDE}")
    if IS_CLOUD:
        log(f"Cloud API mode — no local backend required")
    ThreadingHTTPServer((host, port), Handler).serve_forever()

if __name__ == "__main__":
    main()
