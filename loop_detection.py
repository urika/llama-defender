"""Auto-extracted loop_detection module."""
import hashlib
from datetime import datetime
import proxy_state as _ps

# --- Session-tier helpers (Phase 4 / 建议4) ---


def _effective_session_tier(session_id: str, total_chars: int = 0) -> str:
    """Return 'short' | 'long' | 'very_long' based on context size and request count.

    Priority: context size > request count. If total_chars >= 100K, session is
    'very_long' regardless of request count (DEF-109: long context = higher loop risk).
    Falls back to 'short' when session_id is empty or unknown.
    """
    if total_chars >= _ps.PROXY_LOOP_CHARS_VERY_LONG:
        return "very_long"
    if total_chars >= _ps.PROXY_LOOP_CHARS_LONG:
        return "long"
    if not session_id:
        return "short"
    cnt = _ps._SESSION_REQUEST_COUNT.get(session_id, 0)
    if cnt <= _ps.PROXY_LOOP_SESSION_SHORT_BOUND:
        return "short"
    if cnt <= _ps.PROXY_LOOP_SESSION_LONG_BOUND:
        return "long"
    return "very_long"


def _effective_loop_threshold(session_id: str, total_chars: int = 0) -> int:
    """Dynamic loop threshold by session tier (considers context size)."""
    tier = _effective_session_tier(session_id, total_chars)
    if tier == "long":
        return _ps.PROXY_LOOP_THRESHOLD_LONG
    if tier == "very_long":
        return _ps.PROXY_LOOP_THRESHOLD_VERY_LONG
    return _ps.PROXY_LOOP_THRESHOLD


def _effective_text_loop_threshold(session_id: str, total_chars: int = 0) -> int:
    """Dynamic text-loop threshold by session tier (considers context size)."""
    tier = _effective_session_tier(session_id, total_chars)
    if tier == "long":
        return _ps.PROXY_TEXT_LOOP_THRESHOLD_LONG
    if tier == "very_long":
        return _ps.PROXY_TEXT_LOOP_THRESHOLD_VERY_LONG
    return _ps.PROXY_TEXT_LOOP_THRESHOLD


def _effective_blocker_threshold(session_id: str, total_chars: int = 0) -> int:
    """Dynamic blocker threshold by session tier (considers context size)."""
    tier = _effective_session_tier(session_id, total_chars)
    if tier == "long":
        return _ps.PROXY_BLOCKER_THRESHOLD_LONG
    if tier == "very_long":
        return _ps.PROXY_BLOCKER_THRESHOLD_VERY_LONG
    return _ps.PROXY_BLOCKER_THRESHOLD


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
def _detect_text_loop(tail_assistant, threshold=None, session_id="",
                      min_chars=None, similarity_threshold=None):
    """Detect repeated similar text output in assistant messages.

    When session_id is provided, threshold is computed dynamically from the
    session tier (Phase 4 / 建议4).  Pass explicit threshold to override.
    Returns (max_run, is_text_loop) tuple.
    """
    if threshold is None:
        threshold = _effective_text_loop_threshold(session_id)
    if min_chars is None:
        min_chars = _ps.PROXY_TEXT_LOOP_MIN_CHARS
    if similarity_threshold is None:
        similarity_threshold = _ps.PROXY_TEXT_LOOP_SIMILARITY
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
    # 上下文工程引擎硬上限(§4.9): 会话该结束,而非失真压缩——413 不可重试
    if type(e).__name__ == "ContextOverflowError":
        return 413, "context_window_exceeded", False
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
def _detect_blocker_pattern(messages, session_id=""):
    """
    Walk messages backward and detect a tail of consecutive same-error-type
    tool_result rejections. Stops at the first non-error tool_result, the first
    plain user text message, or when the run length drops to zero.

    When session_id is provided, threshold is computed dynamically from the
    session tier (Phase 4 / 建议4).

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

    blocker_threshold = _effective_blocker_threshold(session_id)
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
    if run_length < blocker_threshold:
        return {"triggered": False, "run_length": run_length, "threshold": blocker_threshold}

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
# --- _count_ctx_recall_calls ---
def _count_ctx_recall_calls(messages, max_messages=None):
    """客户端视图的 ctx_recall 调用计数（P0(ii) 配套指标，2026-09-30）。

    只数**调用/查询词**，不判结局——客户端视图里 ctx_recall 的 tool_result
    恒为 "No such tool available"（CLI 工具白名单不含该工具），真实结局
    （命中/未命中/回退）由代答路径记入 ctx_recall.CTX_RECALL_STATS。
    按 tool_use_id 去重（客户端每轮重发全量历史）；无 id 的调用逐次计数。
    """
    scan = messages if not max_messages else messages[-max_messages:]
    seen = set()
    queries = []
    attempts = 0
    for msg in scan:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "ctx_recall":
                continue
            tid = block.get("id") or ""
            if tid:
                if tid in seen:
                    continue
                seen.add(tid)
            attempts += 1
            inp = block.get("input")
            q = str((inp or {}).get("query") or "") if isinstance(inp, dict) else ""
            if q:
                queries.append(q)
    return {
        "attempts": attempts,
        "distinct_queries": len(set(queries)),
        "last_query": queries[-1] if queries else "",
    }


# --- _count_rereads_without_edit ---
_WASTED_RESULT_MARKERS = ("未发生变化", "wasted call")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def _count_rereads_without_edit(messages, max_messages=None):
    """同文件重读无编辑计数（P0(ii) 配套指标，2026-09-30）。

    L-53 出典里"55 次同目标盲重读"到 2026-09-29 仍只有定性描述，此函数把
    它变成可核验的 run 级数：结构式判据（不依赖客户端自报的 Wasted call）。

      - key = (file_path, offset, limit)：分页参数参与（L-15 口径——分页读
        不同切片是新内容，不算浪费；同窗重读才算）；
      - 某 key 已读过、且此后**无**对该 file_path 的写 ⇒ 该次 Read 记
        no_edit_rereads += 1（no_edit_targets 记去重文件路径）；
      - 写操作清掉该文件全部已读标记（内容已变，再读不是浪费）。
    附 wasted_calls：结果文本已被判定为无效读的次数（客户端原文
    "Wasted call" 或翻译层改写后的 "未发生变化"）。
    """
    scan = messages if not max_messages else messages[-max_messages:]
    read_keys = set()
    reads = 0
    no_edit = 0
    targets = set()
    writes = 0
    wasted = 0
    for msg in scan:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if role == "assistant" and block.get("type") == "tool_use":
                name = block.get("name") or ""
                inp = block.get("input")
                inp = inp if isinstance(inp, dict) else {}
                fp = inp.get("file_path") or inp.get("path") or ""
                if name == "Read" and fp:
                    reads += 1
                    key = (fp, str(inp.get("offset") or ""),
                           str(inp.get("limit") or ""))
                    if key in read_keys:
                        no_edit += 1
                        targets.add(fp)
                    read_keys.add(key)
                elif name in _WRITE_TOOLS and fp:
                    writes += 1
                    for k in [k for k in read_keys if k[0] == fp]:
                        read_keys.discard(k)
            elif role == "user" and block.get("type") == "tool_result":
                text = block.get("content")
                if isinstance(text, list):
                    text = " ".join(b.get("text", "") for b in text
                                    if isinstance(b, dict))
                if not isinstance(text, str):
                    text = str(text or "")
                low = text.lower()
                if any(m in low for m in _WASTED_RESULT_MARKERS):
                    wasted += 1
    return {
        "reads": reads,
        "writes": writes,
        "no_edit_rereads": no_edit,
        "no_edit_targets": len(targets),
        "wasted_calls": wasted,
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
# --- F3'：会话级升级记忆（2026-10-26 方案 v0.1 §2；旗标缺省关=零行为变化）---
# 修正根因（swe-eval 1025-5）：代理日志权威面证实阶梯「工作正常且已重度触发」
# （L1=902/L2=57/L3=8；swe71877 会话 49 次干预）——原 F3「变体逃逸致零触发」
# 前提失效。真问题=干预饱和但**无记忆无升级**：三次干预输出都不携带「你已被
# 干预 N 次」的会话级信息（L1 每次同文、L2 摘除仅当轮、下轮恢复即重新饱和）。
# F3' = 会话级累积计数（注入带「第 N 次」）+ L2 sticky（同错误形态 N 轮内重现
# → 维持摘除并升级提示）。
_LOOP_ESCALATED = {}       # (session_id, tool_name) -> 触发次数
_LOOP_ESCALATED_MAX = 512  # DEF-313 模式：会话数上限，驱逐同 _SESSION_TOOL_FREEZE
_LOOP_STICKY = {}          # (session_id, tool_name, shape) -> until_turn
_LOOP_STICKY_MAX = 512
_LOOP_STICKY_TURNS = 3     # sticky 窗（轮）


def _f3p_enabled():
    """F3' 总开关（缺省关 ⇒ 行为与改造前完全一致）。"""
    return bool(getattr(_ps, "PROXY_LOOP_ESCALATION_MEMORY_ENABLED", False))


def _f3p_now_turn(session_id):
    """当前会话请求序号（= 轮次代理；stage 11 时已含本轮）。"""
    try:
        return int(_ps._SESSION_REQUEST_COUNT.get(session_id, 0))
    except Exception:
        return 0


def _f3p_error_shape(consecutive, tool_name):
    """同错误形态签名：优先 fnf 路径（Bash 变体聚合键），否则 generic。"""
    prefix = str(tool_name) + ":"
    for k in consecutive or {}:
        if not str(k).startswith(prefix):
            continue
        if ":fnf:" in str(k):
            return "fnf:" + str(k).split(":fnf:", 1)[1]
    return "generic"


def _f3p_bump(session_id, tool_name):
    """会话级触发计数 +1（cap 512 驱逐）。"""
    key = (session_id, tool_name)
    n = int(_LOOP_ESCALATED.get(key, 0)) + 1
    _LOOP_ESCALATED[key] = n
    while len(_LOOP_ESCALATED) > _LOOP_ESCALATED_MAX:
        try:
            _LOOP_ESCALATED.pop(next(iter(_LOOP_ESCALATED)))
        except (StopIteration, KeyError):
            break
    return n


def _f3p_sticky_active(session_id, tool_name, shape, now_turn):
    """sticky 是否仍在窗内（窗内 → 同形态重现维持摘除）。"""
    key = (session_id, tool_name, shape)
    until = _LOOP_STICKY.get(key)
    if until is None:
        return False
    if now_turn > until:
        _LOOP_STICKY.pop(key, None)
        return False
    return True


def _f3p_sticky_set(session_id, tool_name, shape, now_turn):
    """记 sticky（会话+工具+错误形态），窗宽 PROXY_LOOP_STICKY_TURNS。"""
    turns = int(getattr(_ps, "PROXY_LOOP_STICKY_TURNS", _LOOP_STICKY_TURNS) or 3)
    _LOOP_STICKY[(session_id, tool_name, shape)] = now_turn + turns
    while len(_LOOP_STICKY) > _LOOP_STICKY_MAX:
        try:
            _LOOP_STICKY.pop(next(iter(_LOOP_STICKY)))
        except (StopIteration, KeyError):
            break


def _f3p_note_text(n, sticky=False):
    """会话级升级提示文本（确定性：同 (会话, 工具, N) 同文本）。"""
    if not n:
        return ""
    txt = f"[System escalation memory: 本会话第 {n} 次对该工具触发循环干预。]"
    if sticky:
        txt += "[该工具已因重复失败被连续限制——同错误形态在限制窗内重现，维持限制。]"
    return txt + " "


# --- _apply_loop_intervention ---
def _apply_loop_intervention(
    raw_messages, raw_tools, max_run, consecutive,
    threshold=None, level2_threshold=None,
    level3_threshold=None, pattern_tool_name=None,
    is_text_loop=False, text_loop_run=0, session_id="",
    total_chars=0, ctx_recall_meta=None,
):
    """Escalating loop intervention (R2.1). Returns (messages, tools, level, tool_name).

    When session_id is provided, threshold/level2/level3 are computed dynamically
    from the session tier (considers context size via total_chars).  Pass explicit
    values to override.

    - max_run < threshold          → no-op, returns (raw_messages, raw_tools, 0, "")
    - threshold <= max_run < L2    → Level 1: append hint user message
    - L2 <= max_run < L3           → Level 2: remove ALL high-count tools from raw_tools
    - max_run >= L3                → Level 3: strip ALL tools (force plain text)
    """
    if threshold is None:
        threshold = _effective_loop_threshold(session_id, total_chars)
    if level2_threshold is None:
        level2_threshold = threshold * 2  # follows existing LEVEL2 = THRESHOLD * 2 pattern
    if level3_threshold is None:
        level3_threshold = threshold * 3  # follows existing LEVEL3 = THRESHOLD * 3 pattern

    # P0(ii) 复发-交错（2026-09-30，L-61 出典）：ctx_recall 结局连败在**精确
    # 重复阈值以下**也触发 Lev1——换关键词重试使 key 每次都不同，交错的其它
    # 工具调用又把早前失败挤出 15 条尾部窗，故 consecutive/max_run 结构性抓
    # 不到这类空转（081154 实测 38 次调用零触发）。streak 由代答路径的结局
    # 台账给出，交错的其它工具不打断，一次命中即清零；fresh=False（空转已
    # 停）即不触发。置于 L2/L3 之后、通用 Lev1 之前：工具摘除降级仍优先，
    # 而这条专属提示优先于"重复同一动作"泛化文案。
    _cr = ctx_recall_meta or {}
    _cr_streak = int(_cr.get("fail_streak") or 0)
    _cr_trigger = bool(
        getattr(_ps, "PROXY_CTX_RECALL_STREAK_ENABLED", True)
        and _cr.get("fresh", True)
        and _cr_streak >= int(getattr(_ps, "PROXY_CTX_RECALL_STREAK_THRESHOLD", 3))
    )

    if max_run < threshold and not is_text_loop and not _cr_trigger:
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
    # 无精确重复触发且无 ctx_recall 连败 ⇒ 工具路径无事可做（文本环已在上面
    # 返回；此处恢复改造前的早退语义，避免 max_run=0 时误发通用 Lev1）。
    if max_run < threshold and not _cr_trigger:
        return raw_messages, raw_tools, 0, ""

    loop_keys = [k for k, v in consecutive.items() if v >= threshold]
    high_count_tools = sorted(set(k.split(":")[0] for k in loop_keys))
    tool_name = high_count_tools[0] if high_count_tools else (pattern_tool_name or "unknown")

    # ---- F3'：会话级升级记忆（2026-10-26 方案 v0.1 §2；旗标缺省关=零行为变化）----
    _f3p_on = bool(session_id) and _f3p_enabled()
    _f3p_n = 0
    _f3p_sticky = False
    _f3p_shape = "generic"
    _f3p_now = 0
    if _f3p_on:
        _f3p_now = _f3p_now_turn(session_id)
        _f3p_shape = _f3p_error_shape(consecutive, tool_name)
        _f3p_sticky = _f3p_sticky_active(session_id, tool_name, _f3p_shape, _f3p_now)

    if max_run >= level3_threshold:
        restricted = sorted(set(t.get("name", "") for t in (raw_tools or []) if isinstance(t, dict)))
        _note = ""
        if _f3p_on:
            _f3p_n = _f3p_bump(session_id, tool_name)
            _note = _f3p_note_text(_f3p_n)
        new_messages = list(raw_messages) + [{
            "role": "user",
            "content": [{"type": "text", "text":
                f"[CRITICAL: You have been looping for {max_run} repetitions across tools "
                f"({', '.join(restricted[:5])}). ALL tools have been DISABLED for this turn. "
                f"You MUST respond with plain text only. "
                f"Describe the problem you are stuck on and what you have tried. "
                f"Do NOT attempt to call any tools. {_note}"
            }]
        }]
        return new_messages, [], 3, tool_name

    if max_run >= level2_threshold:
        remove_set = set(high_count_tools)
        new_tools = [t for t in (raw_tools or [])
                     if not (isinstance(t, dict) and t.get("name", "") in remove_set)]
        removed_names = ", ".join(sorted(remove_set))
        _note = ""
        if _f3p_on:
            _f3p_n = _f3p_bump(session_id, tool_name)
            # L2 sticky：记 (会话, 工具, 错误形态) + until_turn
            _f3p_sticky_set(session_id, tool_name, _f3p_shape, _f3p_now)
            _note = _f3p_note_text(_f3p_n, sticky=True)
        new_messages = list(raw_messages) + [{
            "role": "user",
            "content": [{"type": "text", "text":
                f"[IMPORTANT: You have called tools {max_run} times with no progress. "
                f"The following tools have been REMOVED: {removed_names}. "
                f"You MUST use a completely different approach. "
                f"If you are stuck, describe the problem and ask for guidance. {_note}"
            }]
        }]
        return new_messages, new_tools, 2, tool_name

    if _cr_trigger:
        # P0(ii) 复发-交错专属提示（L-61）：不邀请换关键词重试——存储里没有
        # 就是没有；把模型拽回"陈述已知 + 单次定向取用 + 明确继续"。
        _q = (_cr.get("last_query") or "").strip()[:60]
        _breakdown = "misses=%d" % int(_cr.get("misses") or 0)
        if int(_cr.get("fallbacks") or 0):
            _breakdown += ", fallbacks=%d" % int(_cr.get("fallbacks") or 0)
        new_messages = list(raw_messages) + [{
            "role": "user",
            "content": [{"type": "text", "text":
                f"[System notice: ctx_recall 已连续 {_cr_streak} 次未取回内容"
                f"（{_breakdown}"
                + (f"；最近 query={_q!r}" if _q else "") + "）。"
                "不要再换关键词重试 recall——该内容不在折叠存储里。"
                "改为：用一句话写下你已知的事实与假设，然后要么对目标文件 "
                "Read/Grep 一次，要么明确说明该信息不可得并继续推进。]"
            }]
        }]
        return new_messages, raw_tools, 1, "ctx_recall_streak"

    if tool_name == "ctx_recall":
        # 0923 混合批次泄漏(0923 会话 be6274e9 实测): ctx_recall 必须单独调用,
        # 与其他工具并行会被客户端以 No such tool available 拒绝(代理无法代答
        # 混合批次)。Lev1 即注入专属纪律, 不等 Lev2。
        new_messages = list(raw_messages) + [{
            "role": "user",
            "content": [{"type": "text", "text":
                f"[System notice: ctx_recall is a PROXY-SIDE tool that is answered "
                f"automatically. It MUST be called ALONE - never batch it with other "
                f"tools in the same turn (parallel batches are rejected by the client "
                f"with 'No such tool available'). You have now repeated it {max_run} "
                f"times without success: STOP retrying the same query; either issue "
                f"ctx_recall alone with a DIFFERENT query/anchor, or continue without "
                f"that content.]"
            }]
        }]
        return new_messages, raw_tools, 1, tool_name

    # F3'（2026-10-26 方案 v0.1 §2）：L1 通用提示 + 会话级升级记忆。
    # sticky 命中 ⇒ 同错误形态在限制窗内重现 → 维持摘除（不只提示，直接受限）
    # 并附升级说明——打破「L2 摘除仅当轮、下轮恢复、模型重试即重新饱和」循环。
    _note = ""
    if _f3p_on:
        _f3p_n = _f3p_bump(session_id, tool_name)
        if _f3p_sticky:
            _note = _f3p_note_text(_f3p_n, sticky=True)
            remove_set = {tool_name}
            new_tools = [t for t in (raw_tools or [])
                         if not (isinstance(t, dict) and t.get("name", "") in remove_set)]
            new_messages = list(raw_messages) + [{
                "role": "user",
                "content": [{"type": "text", "text":
                    f"[IMPORTANT: You have repeated the same action {max_run} times "
                    f"(tool: {tool_name}). This is likely a loop. "
                    f"The tool {tool_name} REMAINS DISABLED: it was already restricted "
                    f"for repeated failures and the restriction window is still open. "
                    f"STOP using {tool_name} immediately and try a completely different approach. "
                    f"If file content was cleared, assume the file is unchanged and work from memory. "
                    f"{_note}"
                }]
            }]
            return new_messages, new_tools, 2, tool_name
        _note = _f3p_note_text(_f3p_n)

    new_messages = list(raw_messages) + [{
        "role": "user",
        "content": [{"type": "text", "text":
            f"[System notice: You have repeated the same action {max_run} times "
            f"(tool: {tool_name}). This is likely a loop. "
            f"STOP using {tool_name} immediately and try a completely different approach. "
            f"If file content was cleared, assume the file is unchanged and work from memory. "
            f"{_note}"
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
    "_f3p_enabled",
    "_f3p_now_turn",
    "_f3p_error_shape",
    "_f3p_bump",
    "_f3p_sticky_active",
    "_f3p_sticky_set",
    "_f3p_note_text",
    "_count_ctx_recall_calls",
    "_count_rereads_without_edit",
    "_effective_session_tier",
    "_effective_loop_threshold",
    "_effective_text_loop_threshold",
    "_effective_blocker_threshold",
]
