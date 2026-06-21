"""Content compressor: TokenSieve-inspired semantic compression for tool results.
"""
import json
import re

import proxy_state

# Phase 2: TokenSieve-inspired content compression for tool_result payloads.
# ---------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*m')


def _scrub_ansi(text):
    """Remove ANSI color/control escape sequences from terminal output."""
    if not isinstance(text, str):
        text = str(text)
    return _ANSI_ESCAPE_RE.sub('', text)


def _detect_content_type(text, mime_hint=None):
    """Detect whether a tool_result payload is json, code, log, or plain text."""
    if mime_hint:
        hint = mime_hint.lower()
        if "json" in hint:
            return "json"
        if any(k in hint for k in ("html", "xml", "markdown")):
            return "text"
        if any(k in hint for k in ("python", "javascript", "typescript", "rust", "go", "c++", "c", "java")):
            return "code"
    if not isinstance(text, str):
        return "text"
    stripped = text.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or \
       (stripped.startswith("[") and stripped.endswith("]")):
        try:
            json.loads(stripped)
            return "json"
        except Exception:
            pass
    # Log heuristic: lines starting with timestamps or level keywords.
    lines = stripped.splitlines()
    if len(lines) >= 2:
        log_markers = 0
        for line in lines[:10]:
            if re.search(r"^\d{4}[-/]\d{2}[-/]\d{2}|^\d{2}:\d{2}:\d{2}|\b(ERROR|WARN|INFO|DEBUG|FATAL)\b", line):
                log_markers += 1
        if log_markers >= 1:
            return "log"
    # Code heuristic: significant syntax markers or indentation pattern.
    code_markers = sum(1 for kw in ("def ", "class ", "import ", "function ", "const ", "let ", "var ", "#include", "return ")
                       if kw in text)
    indented_lines = sum(1 for line in lines if line.startswith(("    ", "\t")))
    if code_markers >= 1 or (len(lines) >= 3 and indented_lines >= 1):
        return "code"
    return "text"


def _sieve_json(obj, max_items=None, max_str_len=None, max_depth=None,
                seen_strings=None, enable_dedupe=False, _depth=0):
    """Summarize JSON while preserving structure.

    - Arrays keep first max_items entries plus a count note.
    - Strings longer than max_str_len are truncated.
    - Nesting deeper than max_depth is stringified.
    - Optional scalar dedupe (first-seen-wins) for long repeated strings.
    """
    if max_items is None:
        max_items = proxy_state.PROXY_SIEVE_JSON_MAX_ITEMS
    if max_str_len is None:
        max_str_len = proxy_state.PROXY_SIEVE_JSON_MAX_STR_LEN
    if max_depth is None:
        max_depth = proxy_state.PROXY_SIEVE_JSON_MAX_DEPTH
    if seen_strings is None:
        seen_strings = {}

    if _depth > max_depth:
        return str(obj)[:max_str_len]

    if isinstance(obj, str):
        if enable_dedupe and len(obj) > 20:
            if obj in seen_strings:
                return f"###(repeated: {seen_strings[obj]})"
            seen_strings[obj] = len(seen_strings) + 1
        if len(obj) > max_str_len:
            return obj[:max_str_len] + f"...[truncated {len(obj) - max_str_len} chars]"
        return obj

    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if obj is None:
        return None

    if isinstance(obj, list):
        if len(obj) > max_items:
            summarized = [_sieve_json(item, max_items, max_str_len, max_depth,
                                      seen_strings, enable_dedupe, _depth + 1)
                          for item in obj[:max_items]]
            return summarized + [f"...({len(obj) - max_items} more items)"]
        return [_sieve_json(item, max_items, max_str_len, max_depth,
                            seen_strings, enable_dedupe, _depth + 1)
                for item in obj]

    if isinstance(obj, dict):
        return {
            k: _sieve_json(v, max_items, max_str_len, max_depth,
                           seen_strings, enable_dedupe, _depth + 1)
            for k, v in obj.items()
        }

    return str(obj)[:max_str_len]


def _compress_code(text):
    """Remove comments and collapse excessive blank lines while keeping code."""
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    result = []
    prev_blank = False
    for line in lines:
        stripped = line.strip()
        # Drop full-line comments (common languages)
        if stripped.startswith(("#", "//", "/*", "*", "--", ";")):
            continue
        # Drop trailing comments
        for marker in ("//", "#", "--"):
            idx = line.find(marker)
            if idx >= 0:
                line = line[:idx]
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        result.append(line.rstrip())
        prev_blank = is_blank
    return "\n".join(result).strip()


def _compress_log(text, dedupe=True):
    """Deduplicate adjacent log lines and strip common timestamps.
    Keep lines containing error/exception/warning keywords."""
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    out = []
    last_line = None
    dup_count = 0
    for line in lines:
        # Strip common timestamp prefixes
        cleaned = re.sub(r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?\s*", "", line)
        cleaned = re.sub(r"^\d{2}:\d{2}:\d{2}\s*", "", cleaned)
        if dedupe and cleaned == last_line:
            dup_count += 1
            continue
        if dup_count > 0:
            out.append(f"...({dup_count} identical lines omitted)")
            dup_count = 0
        last_line = cleaned
        # Prioritize error lines by keeping them verbatim; compress benign lines
        if re.search(r"\b(ERROR|Exception|Traceback|FATAL|CRITICAL)\b", cleaned, re.IGNORECASE):
            out.append(line)  # keep original with timestamp for errors
        else:
            out.append(cleaned)
    if dup_count > 0:
        out.append(f"...({dup_count} identical lines omitted)")
    return "\n".join(out).strip()


def _compress_text(text, max_len=2000):
    """Truncate very long plain text while keeping first/last context."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_len:
        return text
    head = text[:max_len // 2]
    tail = text[-max_len // 2:]
    return f"{head}\n\n...[truncated {len(text) - max_len} chars]\n\n{tail}"


def _dedupe_scalars(obj, min_len=20, seen=None):
    """First-seen-wins scalar deduplication for JSON/string values."""
    if seen is None:
        seen = {}
    if isinstance(obj, str):
        if len(obj) >= min_len:
            if obj in seen:
                return f"###(repeated: {seen[obj]})"
            seen[obj] = len(seen) + 1
        return obj
    if isinstance(obj, list):
        return [_dedupe_scalars(item, min_len, seen) for item in obj]
    if isinstance(obj, dict):
        return {k: _dedupe_scalars(v, min_len, seen) for k, v in obj.items()}
    return obj


def _audit_compression(original, compressed, content_type):
    """Validate that compression did not destroy syntax/semantics."""
    if not proxy_state.PROXY_COMPRESS_AUDIT:
        return True
    if content_type == "json":
        try:
            json.loads(compressed)
            return True
        except Exception:
            return False
    if content_type == "code":
        # Simple balance check: brackets and quotes should still be roughly paired.
        open_br = compressed.count("(") + compressed.count("[") + compressed.count("{")
        close_br = compressed.count(")") + compressed.count("]") + compressed.count("}")
        return abs(open_br - close_br) <= 2
    # text/log: always pass the audit
    return True


def compress_tool_result(content, mime_hint=None, threshold=None, mode=None):
    """Compress a single tool_result content payload.

    Returns a dict:
        {
            "original": str,
            "compressed": str,
            "content_type": str,
            "strategy": str,
            "audit_pass": bool,
            "ratio": float,
        }
    """
    if threshold is None:
        threshold = proxy_state.PROXY_COMPRESS_THRESHOLD
    if mode is None:
        mode = proxy_state.PROXY_COMPRESS_MODE

    original = content if isinstance(content, str) else str(content)

    if mode == "lossless" or len(original) < threshold:
        return {
            "original": original,
            "compressed": original,
            "content_type": "short",
            "strategy": "none",
            "audit_pass": True,
            "ratio": 1.0,
        }

    # Stage 1: scrub ANSI
    scrubbed = _scrub_ansi(original) if proxy_state.PROXY_SCRUB_ANSI else original

    # Stage 2: detect content type
    content_type = _detect_content_type(scrubbed, mime_hint=mime_hint)

    # Stage 3: route to compressor
    if content_type == "json":
        try:
            parsed = json.loads(scrubbed)
            enable_dedupe = proxy_state.PROXY_DEDUPE_SCALARS and mode == "aggressive"
            compressed_obj = _sieve_json(parsed, enable_dedupe=enable_dedupe)
            if proxy_state.PROXY_DEDUPE_SCALARS and mode == "aggressive":
                compressed_obj = _dedupe_scalars(compressed_obj)
            compressed = json.dumps(compressed_obj, ensure_ascii=False, separators=(',', ':'))
            strategy = "json_sieve"
        except Exception:
            compressed = scrubbed
            strategy = "json_passthrough"
    elif content_type == "code":
        compressed = _compress_code(scrubbed)
        strategy = "code_compress"
    elif content_type == "log":
        compressed = _compress_log(scrubbed, dedupe=proxy_state.PROXY_LOG_DEDUPE)
        strategy = "log_compress"
    else:
        compressed = _compress_text(scrubbed)
        strategy = "text_truncate"

    # Stage 4: audit
    audit_pass = _audit_compression(scrubbed, compressed, content_type)
    if not audit_pass:
        compressed = scrubbed
        strategy = "audit_fallback"

    ratio = len(compressed) / len(original) if original else 1.0
    return {
        "original": original,
        "compressed": compressed,
        "content_type": content_type,
        "strategy": strategy,
        "audit_pass": audit_pass,
        "ratio": round(ratio, 4),
    }


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



__all__ = [
    "_scrub_ansi",
    "_detect_content_type",
    "_sieve_json",
    "_compress_code",
    "_compress_log",
    "_compress_text",
    "_dedupe_scalars",
    "_audit_compression",
    "compress_tool_result",
    "_generate_tool_summary",
]
