"""Auto-extracted loop_detection module."""
import hashlib
from datetime import datetime
import proxy_state as _ps

# --- _check_dedup ---
def _check_dedup(body_str):
    """Hash-based request dedup with TTL.
    Uses MD5 instead of Python hash() for cross-process stability and collision resistance."""
    h = hashlib.md5(body_str.encode("utf-8")).hexdigest()
    now = datetime.now().timestamp()
    with _ps._state_lock:
        for k in list(_ps._DEDUP_CACHE):
            if now - _ps._DEDUP_CACHE[k] > _ps.PROXY_DEDUP_WINDOW:
                del _ps._DEDUP_CACHE[k]
        if h in _ps._DEDUP_CACHE:
            return True
        _ps._DEDUP_CACHE[h] = now
    return False
# --- _compute_text_similarity ---
def _compute_text_similarity(text1, text2):
    """Compute similarity between two texts using character-level Jaccard index.
    Returns float 0.0 (completely different) to 1.0 (identical)."""
    if not text1 or not text2:
        return 0.0
    # Use bigrams for better granularity
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1)) if len(s) >= 2 else set(s)
    b1 = bigrams(text1)
    b2 = bigrams(text2)
    if not b1 or not b2:
        return 0.0
    intersection = len(b1 & b2)
    union = len(b1 | b2)
    return intersection / union if union > 0 else 0.0
# --- _detect_text_loop ---
def _detect_text_loop(tail_assistant, threshold=_ps.PROXY_TEXT_LOOP_THRESHOLD,
                      min_chars=_ps.PROXY_TEXT_LOOP_MIN_CHARS,
                      similarity_threshold=_ps.PROXY_TEXT_LOOP_SIMILARITY):
    """Detect repeated similar text output in assistant messages.
    Returns (max_run, is_text_loop) tuple."""
    if not _ps.PROXY_TEXT_LOOP_ENABLED or len(tail_assistant) < threshold:
        return 0, False

    # Extract text content from assistant messages
    text_history = []
    for msg in tail_assistant:
        content = msg.get("content", "")
        text = ""
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    text += block.get("text", "")
        elif isinstance(content, str):
            text = content
        # Only consider substantial text (ignore short tool-only messages)
        if len(text) >= min_chars:
            text_history.append(text[:500])  # Cap at 500 chars for comparison
        else:
            text_history.append("")  # Short messages break the chain

    if len(text_history) < threshold:
        return 0, False

    # Check for consecutive similar texts from the end
    max_run = 1
    current_run = 1
    for i in range(len(text_history) - 1, 0, -1):
        curr = text_history[i]
        prev = text_history[i - 1]
        if curr and prev:
            sim = _compute_text_similarity(curr, prev)
            if sim >= similarity_threshold:
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                current_run = 1
        else:
            current_run = 1

    return max_run, max_run >= threshold
# --- _classify_exception ---
def _classify_exception(e):
    import socket as _socket
    exc_name = type(e).__name__
    msg = str(e).lower()
    if isinstance(e, (TimeoutError, _socket.timeout)) or "timeout" in exc_name.lower():
        return 504, "timeout_error", True
    if "timed out" in msg or "timeout" in msg or "read timed out" in msg:
        return 504, "timeout_error", True
    if "memory" in exc_name.lower() or "memory" in msg or \
       "resource limit" in msg or "oom" in msg or "out of memory" in msg:
        return 503, "backend_oom", True
    if isinstance(e, (BrokenPipeError, ConnectionResetError)):
        return 499, "client_closed", False
    if isinstance(e, (ConnectionError, ConnectionRefusedError)) or \
       "connection refused" in msg or \
       "failed to connect" in msg:
        return 503, "backend_unavailable", True
    if isinstance(e, (KeyError, TypeError, AttributeError, NameError, ValueError)):
        return 500, "internal_error", False
    return 500, "unknown_error", False
# --- _detect_blocker_pattern ---
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
    if not _ps.PROXY_BLOCKER_ENABLED:
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
        for err_type, markers in _ps._BLOCKER_ERROR_MARKERS:
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
    if run_length < _ps.PROXY_BLOCKER_THRESHOLD:
        return {"triggered": False, "run_length": run_length, "threshold": _ps.PROXY_BLOCKER_THRESHOLD}

    tool_name, error_type = run[-1]  # most recent
    return {
        "triggered": True,
        "tool_name": tool_name,
        "error_type": error_type,
        "run_length": run_length,
    }
# --- _build_blocker_message ---
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
# --- _build_tool_use_map ---
def _build_tool_use_map(messages):
    """Build a mapping from tool_use_id to tool name across all messages."""
    tool_map = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tool_map[b.get("id", "")] = b.get("name", "")
    return tool_map
# --- _apply_loop_intervention ---
def _apply_loop_intervention(
    raw_messages, raw_tools, max_run, consecutive,
    threshold=_ps.PROXY_LOOP_THRESHOLD, level2_threshold=_ps.PROXY_LOOP_LEVEL2,
    level3_threshold=_ps.PROXY_LOOP_LEVEL3, pattern_tool_name=None,
    is_text_loop=False, text_loop_run=0,
):
    """Escalating loop intervention (R2.1). Returns (messages, tools, level, tool_name).

    Pure-ish: given the conversation state, returns the (possibly extended) message
    list and (possibly filtered) tools list. Caller is responsible for assigning
    them back to body["messages"]/body["tools"] and emitting the metrics step.

    - max_run < threshold          → no-op, returns (raw_messages, raw_tools, 0, "")
    - threshold <= max_run < L2    → Level 1: append hint user message
    - L2 <= max_run < L3           → Level 2: remove ALL high-count tools from raw_tools
    - max_run >= L3                → Level 3: strip ALL tools (force plain text)
    """
    if max_run < threshold:
        return raw_messages, raw_tools, 0, ""

    # Text loop detection: different intervention for repeated similar text
    if is_text_loop and text_loop_run >= threshold:
        if text_loop_run >= level3_threshold:
            new_messages = list(raw_messages) + [{
                "role": "user",
                "content": [{"type": "text", "text":
                    f"[CRITICAL: You have been repeating the same text output {text_loop_run} times. "
                    f"This is a text output loop. ALL tools have been DISABLED for this turn. "
                    f"You MUST stop repeating yourself. "
                    f"If you are stuck, say 'I am stuck' and explain what is wrong. "
                    f"Do NOT repeat the same explanation again.]"
                }]
            }]
            return new_messages, [], 3, "text_loop"
        elif text_loop_run >= level2_threshold:
            new_messages = list(raw_messages) + [{
                "role": "user",
                "content": [{"type": "text", "text":
                    f"[IMPORTANT: You have repeated similar text {text_loop_run} times. "
                    f"This indicates you are stuck in a loop. "
                    f"STOP repeating the same explanation. "
                    f"If you cannot solve the problem, say so clearly and ask for help. "
                    f"Do NOT repeat your previous analysis.]"
                }]
            }]
            return new_messages, raw_tools, 2, "text_loop"
        else:
            new_messages = list(raw_messages) + [{
                "role": "user",
                "content": [{"type": "text", "text":
                    f"[System notice: You have repeated similar text output {text_loop_run} times. "
                    f"This appears to be a loop. "
                    f"STOP repeating yourself and either provide a NEW response or acknowledge you are stuck. "
                    f"Do NOT repeat the same explanation.]"
                }]
            }]
            return new_messages, raw_tools, 1, "text_loop"

    # Tool loop detection (original logic)
    loop_keys = [k for k, v in consecutive.items() if v >= threshold]
    high_count_tools = sorted(set(k.split(":")[0] for k in loop_keys))
    tool_name = high_count_tools[0] if high_count_tools else (pattern_tool_name or "unknown")

    if max_run >= level3_threshold:
        restricted = sorted(set(t.get("name", "") for t in (raw_tools or []) if isinstance(t, dict)))
        new_messages = list(raw_messages) + [{
            "role": "user",
            "content": [{"type": "text", "text":
                f"[CRITICAL: You have been looping for {max_run} repetitions across tools "
                f"({', '.join(restricted[:5])}). ALL tools have been DISABLED for this turn. "
                f"You MUST respond with plain text only. "
                f"Describe the problem you are stuck on and what you have tried. "
                f"Do NOT attempt to call any tools.]"
            }]
        }]
        return new_messages, [], 3, tool_name

    if max_run >= level2_threshold:
        remove_set = set(high_count_tools)
        new_tools = [t for t in (raw_tools or [])
                     if not (isinstance(t, dict) and t.get("name", "") in remove_set)]
        removed_names = ", ".join(sorted(remove_set))
        new_messages = list(raw_messages) + [{
            "role": "user",
            "content": [{"type": "text", "text":
                f"[IMPORTANT: You have called tools {max_run} times with no progress. "
                f"The following tools have been REMOVED: {removed_names}. "
                f"You MUST use a completely different approach. "
                f"If you are stuck, describe the problem and ask for guidance.]"
            }]
        }]
        return new_messages, new_tools, 2, tool_name

    new_messages = list(raw_messages) + [{
        "role": "user",
        "content": [{"type": "text", "text":
            f"[System notice: You have repeated the same action {max_run} times "
            f"(tool: {tool_name}). This is likely a loop. "
            f"STOP using {tool_name} immediately and try a completely different approach. "
            f"If file content was cleared, assume the file is unchanged and work from memory.]"
        }]
    }]
    return new_messages, raw_tools, 1, tool_name

__all__ = [
    "_check_dedup",
    "_compute_text_similarity",
    "_detect_text_loop",
    "_classify_exception",
    "_detect_blocker_pattern",
    "_build_blocker_message",
    "_build_tool_use_map",
    "_apply_loop_intervention",
]
