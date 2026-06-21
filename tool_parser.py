"""Tool-call parser: XML->JSON fallback, content-tools extraction, streaming extractor.

Handles Qwen3.5/3.6 XML-style tool calls (llama.cpp issue #21495) and
Qwen2.5-Coder <tools> content-text fallback.

The _log delegate is set by anthropic_proxy after import to enable structured
logging without creating a circular import.
"""
import json
import re

import proxy_state

# Logging delegate — set by anthropic_proxy after import
def _log(msg, level="INFO"):
    pass

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
    """Attempt to repair truncated JSON by closing open strings/braces/brackets."""
    if not raw or not raw.strip():
        return "{}"
    s = raw.strip()
    in_string = False
    escape_next = False
    bracket_stack = []
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
        if c == '{':
            bracket_stack.append('}')
        elif c == '[':
            bracket_stack.append(']')
        elif c in ('}', ']'):
            if bracket_stack:
                bracket_stack.pop()
        i += 1
    if in_string:
        s += '"'
    while bracket_stack:
        s += bracket_stack.pop()
    return s


def _is_truncated_json(raw: str) -> bool:
    """Check if raw string is an *incomplete* JSON (truncated) vs. a malformed one."""
    s = raw.strip() if raw else ""
    if not s:
        return False
    # Single-char openers – clear truncation
    if s in ("{", "[", "{"):
        return True
    # Unclosed string
    if s.count('"') % 2 == 1:
        return True
    # Unmatched braces / brackets
    open_braces = s.count('{') - s.count('}')
    open_brackets = s.count('[') - s.count(']')
    if open_braces > 0 or open_brackets > 0:
        return True
    # Ends mid-value / mid-key (common truncation patterns)
    s_end = s.rstrip()
    if s_end.endswith('":') or s_end.endswith('",') or s_end.endswith(':'):
        return True
    if s_end.endswith(',') or s_end.endswith('"'):
        return True
    return False


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


def _unescape_double_escaped_json(obj):
    """Recursively unescape double-escaped JSON strings.
    Qwen3.6 with qwen3_coder_xml parser occasionally emits arguments where
    array/object values are wrapped as strings, e.g.:
        {"questions": "[{\\\"question\\\": \\\"...\\\"}]"}
    After outer json.loads, the inner value becomes a string like
    '[{"question": "..."}]'.  This helper detects such string-wrapped
    JSON and turns it back into real nested structures.
    """
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            unescaped = _unescape_double_escaped_json(v)
            result[k] = unescaped
        return result
    if isinstance(obj, list):
        return [_unescape_double_escaped_json(v) for v in obj]
    if isinstance(obj, str):
        stripped = obj.strip()
        if (stripped.startswith('[') and stripped.endswith(']')) or \
           (stripped.startswith('{') and stripped.endswith('}')):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                # Try repair for truncated inner JSON
                repaired = _repair_truncated_json(stripped)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass
    return obj


def _finalize_parsed_args(parsed):
    """Apply post-parse fixes: unescape double-escaped JSON, then coerce booleans."""
    if not isinstance(parsed, dict):
        return {}
    parsed = _unescape_double_escaped_json(parsed)
    return _coerce_booleans(parsed)


def parse_tool_arguments(raw: str, tool_name_hint: str = "") -> dict:
    """
    Parse tool arguments from backend response.
    Falls back from JSON -> XML extraction -> empty dict.
    Stringified booleans are coerced to real bools to satisfy client validation.
    Detects truncated JSON vs. malformed JSON and logs explicitly.
    Also unescapes double-escaped JSON arrays/objects (Qwen3.6 qwen3_coder_xml bug).
    """
    raw = raw.strip() if raw else ""
    if not raw:
        return {}

    # 1. Try standard JSON
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return _finalize_parsed_args(parsed)
        return {}
    except json.JSONDecodeError:
        pass

    # 2. Attempt repair unconditionally — _is_truncated_json misses cases where
    # the JSON ends with } but inner structures are truncated (rapid-mlx
    # qwen3_coder_xml output: {"questions": "[{\\\"q\\\": \\\"...\\\"}]")
    repaired = _repair_truncated_json(raw)
    try:
        parsed = json.loads(repaired)
        if isinstance(parsed, dict):
            if repaired != raw:
                _log(f"  [JSON_REPAIRED] tool={tool_name_hint}, {len(raw)} -> {len(repaired)} chars")
            return _finalize_parsed_args(parsed)
    except json.JSONDecodeError:
        if repaired != raw:
            _log(f"  [JSON_TRUNCATED_REPAIR_FAILED] tool={tool_name_hint}, repaired={repaired[:200]!r}")

    # 3. Try to find a JSON object embedded inside the text
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        try:
            parsed = json.loads(raw[brace_start:brace_end + 1])
            if isinstance(parsed, dict):
                return _finalize_parsed_args(parsed)
        except json.JSONDecodeError:
            pass

    # 4. XML fallback
    xml_params = _extract_xml_params(raw)
    if xml_params:
        _log(f"  [XML_FALLBACK] extracted {len(xml_params)} params from XML for tool={tool_name_hint}")
        return _finalize_parsed_args(xml_params)

    # 5. Last resort: treat the whole string as a single "command" or "query" param
    # based on common tool patterns
    if tool_name_hint in ("exec", "bash", "shell"):
        return {"command": raw.strip("`\n ")}
    if tool_name_hint in ("read", "view", "file"):
        return {"file_path": raw.strip("`\n ")}

    _log(f"  [JSON_MALFORMED] failed to parse args for tool={tool_name_hint}, raw={raw[:200]!r}")
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
    if not proxy_state.CONTENT_TOOLS_FALLBACK_ENABLED or not text:
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
            _log(f"  [CONTENT_TOOLS_FALLBACK] parse failed for body[:80]={body[:80]!r}")
            parts.append(text[open_i:close_i + len(TOOLS_END_TAG)])
        else:
            tools.append(parsed)
            _log(f"  [CONTENT_TOOLS_FALLBACK] extracted tool={parsed['name']}")
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
        self._enabled = proxy_state.CONTENT_TOOLS_FALLBACK_ENABLED

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
                    _log(f"  [CONTENT_TOOLS_FALLBACK] streamed tool={parsed['name']}")
                else:
                    _log(f"  [CONTENT_TOOLS_FALLBACK] streamed parse failed for body[:80]={body[:80]!r}")
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
            _log("  [CONTENT_TOOLS_FALLBACK] EOF inside <tools>, emitting raw")
            events.append(("text", TOOLS_TRIGGER + self.tools_content_buf))
            self.in_tools_block = False
            self.tools_content_buf = ""
        return events


__all__ = [
    "_extract_xml_params",
    "_extract_xml_tool_name",
    "_repair_truncated_json",
    "_is_truncated_json",
    "_coerce_booleans",
    "_unescape_double_escaped_json",
    "_finalize_parsed_args",
    "parse_tool_arguments",
    "TOOLS_TRIGGER",
    "TOOLS_END_TAG",
    "_parse_tools_block_body",
    "_extract_content_tool_calls",
    "_StreamingToolsExtractor",
]
