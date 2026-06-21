"""Lifecycle stage classification and dynamic max_tokens computation."""
import proxy_state as _ps
from backend_strategy import BackendStrategy
_strategy = BackendStrategy.create(_ps.IS_CLOUD)
from message_converter import _estimate_message_chars

# External function delegate — set by anthropic_proxy after import via
#   lifecycle._get_system_memory = admin_server._get_system_memory
_get_system_memory = None  # set by caller before first request

# --- _normalize_system_messages ---
def _normalize_system_messages(messages):
    """Convert mid-conversation system messages to user messages.

    Qwen chat templates require the system message to be at the beginning.
    Claude Code's mid-conversation-system beta inserts system messages later,
    which breaks rapid-mlx/Qwen. We keep the first system message (if any) and
    convert subsequent ones to user messages prefixed with [System update].
    """
    if not messages:
        return messages
    result = []
    seen_system = False
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            if not seen_system:
                result.append(msg)
                seen_system = True
            else:
                content = msg.get("content", "")
                text = content
                if isinstance(content, list):
                    text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
                new_msg = {
                    "role": "user",
                    "content": [{"type": "text", "text": f"[System update]: {text}"}]
                }
                result.append(new_msg)
        else:
            result.append(msg)
    return result
# --- _apply_cache_aligner ---
def _apply_cache_aligner(messages):
    """Cache Aligner MVP: protect the first N messages from truncation.

    Returns (prefix_messages, dynamic_messages). The caller should run
    truncation only on dynamic_messages, then reassemble prefix + dynamic.
    """
    if not _ps.PROXY_CACHE_ALIGN_ENABLED:
        return [], messages
    head = min(_ps.PROXY_CACHE_ALIGN_HEAD, len(messages))
    prefix = messages[:head]
    dynamic = messages[head:]
    return prefix, dynamic
# --- _classify_lifecycle_stage ---
def _classify_lifecycle_stage(messages, session_id=None):
    """
    Classify the current request into a lifecycle stage based on total chars.
    All thresholds are in chars (from _estimate_message_chars), guaranteeing
    monotonic escalation: lighter compression at lower stages, heavier at
    higher stages.

    Phase 1 改进3: When session_id is provided AND the session has already
    accumulated >= _ps.PROXY_SESSION_CONTINUATION_MIN_REQUESTS prior requests
    (i.e. this is a continuation, not the first call) AND the payload is
    past _ps.PROXY_CHARS_EXPANSION, the function returns an aggressive config
    even if the raw char count would map to a milder stage. This addresses
    the "agent 累积后大请求" 盲区 in proxy-truncation-agent-scenario.md.

    Returns a dict:
      {
        "stage": "init"|"growth"|"expansion"|"saturation"|"oom_danger"|"pre_trunc",
        "total_chars": int,
        "frozen_head": int,          # Frozen Zone protection
        "clear_zone_pct": float|None, # tail-clear zone (None=skip)
        "thinking_keep": int,         # thinking keep_recent (0=skip)
        "truncate_rounds": int|None,  # L5 rounds (None=skip truncation)
        "oom_safety": bool,           # Enable OOM safety iterative FIFO
        "is_continuation": bool,      # True when session_id is a known continuation
        "request_count": int,         # # of prior requests in this session
      }
    """
    total_chars = _estimate_message_chars(messages)
    cloud = not _strategy.oom_safety_enabled

    # Phase 1: detect session continuation. The increment happens here so the
    # counter advances exactly once per request, atomically w.r.t. the
    # classification decision.
    is_continuation = False
    request_count = 0
    if _ps.PROXY_SESSION_CONTINUATION_ENABLED and session_id:
        with _ps._state_lock:
            request_count = _ps._SESSION_REQUEST_COUNT.get(session_id, 0)
            is_continuation = request_count >= _ps.PROXY_SESSION_CONTINUATION_MIN_REQUESTS
            _ps._SESSION_REQUEST_COUNT[session_id] = request_count + 1

    # Aggressive branch: continuation + above-EXPANSION payload. Return a
    # saturation-grade config regardless of the raw stage mapping. This
    # catches the "agent 多轮累积后大请求" case where the in-memory history
    # alone is > 90K chars but hasn't yet tripped the OOM_DANGER threshold.
    if is_continuation and total_chars > _ps.PROXY_CHARS_EXPANSION:
        aggressive_rounds = max(3, _ps.PROXY_CTX_KEEP_ROUNDS // 2)
        return {
            "stage": "saturation",
            "total_chars": total_chars,
            "frozen_head": 2,
            "clear_zone_pct": 1.0,
            "thinking_keep": 3,
            "truncate_rounds": aggressive_rounds,
            "oom_safety": not cloud,
            "is_continuation": True,
            "request_count": request_count,
        }

    # Defaults per stage — ordered by increasing severity
    if total_chars < _ps.PROXY_CLEAR_THRESHOLD:
        return {
            "stage": "init", "total_chars": total_chars,
            "frozen_head": _ps.PROXY_FROZEN_HEAD if not cloud else 0,
            "clear_zone_pct": None, "thinking_keep": 0,
            "truncate_rounds": None, "oom_safety": False,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    elif total_chars < _ps.PROXY_CHARS_GROWTH:
        return {
            "stage": "growth", "total_chars": total_chars,
            "frozen_head": _ps.PROXY_FROZEN_HEAD if not cloud else 0,
            "clear_zone_pct": 0.4, "thinking_keep": 0,
            "truncate_rounds": None, "oom_safety": False,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    elif total_chars < _ps.PROXY_CHARS_EXPANSION:
        return {
            "stage": "expansion", "total_chars": total_chars,
            "frozen_head": _ps.PROXY_FROZEN_HEAD if not cloud else 0,
            "clear_zone_pct": 0.6, "thinking_keep": 5,
            "truncate_rounds": _ps.PROXY_CTX_KEEP_ROUNDS, "oom_safety": False,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    elif total_chars < _ps.PROXY_CHARS_SATURATION:
        return {
            "stage": "saturation", "total_chars": total_chars,
            "frozen_head": max(2, (_ps.PROXY_FROZEN_HEAD if not cloud else 0) // 2),
            "clear_zone_pct": 1.0, "thinking_keep": 3,
            "truncate_rounds": _ps.PROXY_CTX_KEEP_ROUNDS, "oom_safety": False,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    elif total_chars < _ps.PROXY_CHARS_OOM_DANGER:
        return {
            "stage": "oom_danger", "total_chars": total_chars,
            "frozen_head": 0, "clear_zone_pct": 1.0, "thinking_keep": 1,
            "truncate_rounds": 3, "oom_safety": not cloud,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    else:
        return {
            "stage": "pre_trunc", "total_chars": total_chars,
            "frozen_head": 0, "clear_zone_pct": 1.0, "thinking_keep": 1,
            "truncate_rounds": 2, "oom_safety": not cloud,
            "is_continuation": is_continuation, "request_count": request_count,
        }
# --- _compute_dynamic_max_tokens ---
def _compute_dynamic_max_tokens(max_tokens_orig, stage_config, mem=None):
    """Compute a context-aware max_tokens ceiling.

    - Heavy lifecycle stages get a lower ceiling.
    - rapid-mlx backend gets an additional discount (known to ignore max_tokens).
    - Low available memory lowers the ceiling one more notch.
    Returns (adjusted_max_tokens, reason_string).
    """
    if not _ps.PROXY_DYNAMIC_MAX_TOKENS_ENABLED:
        return max_tokens_orig, "dynamic_disabled"

    stage = stage_config.get("stage", "init")
    if stage == "init":
        cap = _ps.PROXY_DYNAMIC_MAX_TOKENS_INIT
    elif stage in ("growth", "expansion"):
        cap = _ps.PROXY_DYNAMIC_MAX_TOKENS_GROWTH
    else:  # saturation, oom_danger, pre_trunc
        cap = _ps.PROXY_DYNAMIC_MAX_TOKENS_SATURATION

    adjusted = min(max_tokens_orig, cap)
    reasons = [f"stage={stage}"]

    if _strategy.oom_safety_enabled and "rapid-mlx" in (_ps.MODEL_NAME or ""):
        adjusted = int(adjusted * _ps.PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO)
        reasons.append("rapid-mlx_discount")

    try:
        if mem is None:
            mem = _get_system_memory()
        available_gb = float(mem.get("available_gb", 48))
        total_gb = float(mem.get("total_gb", 48))
        if total_gb > 0 and available_gb / total_gb < 0.20:
            adjusted = int(adjusted * 0.7)
            reasons.append("low_memory")
    except Exception:
        pass

    adjusted = max(1, adjusted)
    return adjusted, ",".join(reasons)

__all__ = [
    "_normalize_system_messages",
    "_apply_cache_aligner",
    "_classify_lifecycle_stage",
    "_compute_dynamic_max_tokens",
]
