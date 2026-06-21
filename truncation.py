"""Auto-extracted truncation module."""
import json
import re
import urllib.request
import proxy_state as _ps
from content_compressor import compress_tool_result, _generate_tool_summary
from lifecycle import _classify_lifecycle_stage
from loop_detection import _build_tool_use_map
from message_converter import _estimate_message_chars, _strip_thinking_from_msg
from tool_filter import _extract_keywords, _inject_keyword_context
from message_converter import _estimate_message_chars

def _log(msg, level="INFO"):
    pass

# --- _compress_content_pass ---
def _compress_content_pass(messages, tools_list=None, stage_config=None):
    """
    Single-pass content compression: combines L2 tool-result clearing and
    L4 thinking block stripping into one traversal.

    Scans messages once to locate all tool_results and thinking blocks,
    then applies semantic clearing and thinking stripping in a second pass.
    Both operations respect Frozen Zone protection — messages before
    frozen_head are never modified.

    Returns (messages, combined_stats_dict).
    """
    if stage_config is None:
        stage_config = _classify_lifecycle_stage(messages)

    frozen_head = stage_config.get("frozen_head", _ps.PROXY_FROZEN_HEAD)
    clear_zone_pct = stage_config.get("clear_zone_pct")
    thinking_keep = stage_config.get("thinking_keep", 3)
    total_chars = stage_config.get("total_chars", _estimate_message_chars(messages))

    # ---- Phase 1: collect indices (single scan) ----
    all_tool_result_indices = []
    thinking_indices = []

    for msg_idx, msg in enumerate(messages):
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block_idx, block in enumerate(content):
            bt = block.get("type", "")
            if bt == "tool_result":
                all_tool_result_indices.append((msg_idx, block_idx))
            elif bt == "thinking" and msg.get("role") == "assistant":
                if msg_idx not in thinking_indices:
                    thinking_indices.append(msg_idx)
        # Also check for inline <thinking> tags in text blocks
        if msg.get("role") == "assistant":
            for block in content:
                if block.get("type") == "text":
                    txt = block.get("text", "")
                    if "<thinking>" in txt or "</thinking>" in txt:
                        if msg_idx not in thinking_indices:
                            thinking_indices.append(msg_idx)
                        break

    # ---- Phase 1b: semantic compression of tool_result contents (Phase 2) ----
    compress_stats_list = []
    if _ps.PROXY_COMPRESS_ENABLED:
        for msg_idx, block_idx in all_tool_result_indices:
            if frozen_head > 0 and msg_idx < frozen_head:
                continue
            block = messages[msg_idx]["content"][block_idx]
            content = block.get("content", "")
            if not content:
                continue
            # Guess mime hint from tool name when possible.
            mime_hint = None
            tool_use_id = block.get("tool_use_id", "")
            for m_idx in range(msg_idx - 1, -1, -1):
                m = messages[m_idx]
                if m.get("role") == "assistant":
                    c = m.get("content", "")
                    if isinstance(c, list):
                        for b in c:
                            if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                                tool_name = b.get("name", "")
                                if tool_name == "Read":
                                    inp = b.get("input", {})
                                    if isinstance(inp, dict):
                                        fp = inp.get("file_path", inp.get("path", ""))
                                        if fp:
                                            mime_hint = fp.lower().split(".")[-1] if "." in fp else None
                                break
                    break

            result = compress_tool_result(content, mime_hint=mime_hint)
            if result["ratio"] < 1.0:
                block["content"] = result["compressed"]
                compress_stats_list.append({
                    "msg_idx": msg_idx,
                    "block_idx": block_idx,
                    "content_type": result["content_type"],
                    "strategy": result["strategy"],
                    "ratio": result["ratio"],
                    "audit_pass": result["audit_pass"],
                    "original_len": len(result["original"]),
                    "compressed_len": len(result["compressed"]),
                })

    # ---- Phase 2a: tool-result clearing logic ----
    clear_stats = {"enabled": False, "skipped": True, "reason": "disabled"}
    cleared_files = []
    if _ps.PROXY_CLEAR_ENABLED and total_chars >= _ps.PROXY_CLEAR_THRESHOLD:
        # Filter to dynamic zone
        if frozen_head > 0:
            tool_result_indices = [(mi, bi) for mi, bi in all_tool_result_indices if mi >= frozen_head]
        else:
            tool_result_indices = list(all_tool_result_indices)

        # Apply zone-pct filter
        if clear_zone_pct is not None and clear_zone_pct < 1.0 and tool_result_indices:
            eligible_count = max(1, int(len(tool_result_indices) * clear_zone_pct))
            tool_result_indices = tool_result_indices[:eligible_count]
            # log handled by caller

        # Reduce frozen if too few
        _frozen = frozen_head
        if len(tool_result_indices) <= _ps.PROXY_TOOL_KEEP and _frozen > 0:
            _frozen = max(0, _frozen // 2)
            tool_result_indices = [(mi, bi) for mi, bi in all_tool_result_indices if mi >= _frozen]

        if len(tool_result_indices) > _ps.PROXY_TOOL_KEEP:
            keep = _ps.PROXY_TOOL_KEEP
            if tools_list is not None:
                has_agent = any(t == "Agent" or t == "EnterPlanMode" for t in tools_list)
                if not has_agent and len(tools_list) > 0:
                    keep = max(_ps.PROXY_TOOL_KEEP, 15)

            total_tr = len(tool_result_indices)
            recent_cutoff = max(0, total_tr - 6)
            keep_positions = set()
            cleared_files_set = set()

            # Score and keep
            scored = []
            for idx_pos, (msg_idx, block_idx) in enumerate(tool_result_indices):
                block = messages[msg_idx]["content"][block_idx]
                tool_use_id = block.get("tool_use_id", "")
                content_str = str(block.get("content", ""))
                score = 0
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
                score += _ps.TOOL_SEMANTIC_PRIORITY.get(tool_name, 1)
                for pat, pts in _ps.TOOL_RESULT_HIGH_VALUE_PATTERNS:
                    if pat.search(content_str[:500]):
                        score += pts
                if tool_name == "Read" and idx_pos >= recent_cutoff:
                    score += 5
                if "[System:" in content_str and any(kw in content_str for kw in ("未发生变化", "文件不存在", "参数错误")):
                    score += 10
                scored.append((score, idx_pos, msg_idx, block_idx, tool_name, content_str))

            scored.sort(key=lambda x: (-x[0], -x[1]))
            keep_positions = set(x[1] for x in scored[:keep])

            # Apply clearing
            cleared_count = 0
            cleared_chars = 0
            for idx_pos, (msg_idx, block_idx) in enumerate(tool_result_indices):
                if idx_pos in keep_positions:
                    continue
                block = messages[msg_idx]["content"][block_idx]
                original = block.get("content", "")
                original_len = len(str(original)) if original else 0
                tool_use_id = block.get("tool_use_id", "")
                # Extract meta_info
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
                                            cleared_files_set.add(fp)
                                        elif cmd:
                                            meta_info = f" cmd={cmd[:60]}"
                                    break
                        if meta_info:
                            break
                # Tool name for summary
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
                if tool_name == "Read" and _ps.PROXY_REREAD_PREVIEW_CHARS > 0:
                    preview = str(original)[:_ps.PROXY_REREAD_PREVIEW_CHARS]
                    if len(str(original)) > _ps.PROXY_REREAD_PREVIEW_CHARS:
                        preview += "..."
                    block["content"] = f"[cleared: {summary}]\n{preview}"
                else:
                    block["content"] = f"[cleared: {summary}]"
                cleared_count += 1
                cleared_chars += original_len

            cleared_files = list(cleared_files_set)
            clear_stats = {
                "enabled": True, "cleared": True,
                "cleared_tool_results": cleared_count,
                "cleared_chars": cleared_chars, "kept": keep,
                "cleared_files": cleared_files,
                "total_chars_before": total_chars,
                "frozen_used": _frozen,
            }
        else:
            clear_stats = {
                "enabled": True, "skipped": True,
                "reason": "few_tool_results",
                "count": len(tool_result_indices),
                "frozen_used": _frozen,
            }
    elif not _ps.PROXY_CLEAR_ENABLED:
        clear_stats = {"enabled": False}
    else:
        clear_stats = {"enabled": True, "skipped": True, "reason": "below_threshold", "chars": total_chars}

    # ---- Phase 2b: thinking block stripping ----
    think_stats = {"enabled": True, "skipped": True, "reason": "stage_skip"}
    if thinking_keep > 0 and thinking_indices:
        dynamic_thinking = [idx for idx in thinking_indices if idx >= frozen_head]
        if len(dynamic_thinking) > thinking_keep:
            keep_set = set(dynamic_thinking[-thinking_keep:])
            stripped = 0
            for idx in dynamic_thinking:
                if idx not in keep_set:
                    _strip_thinking_from_msg(messages[idx])
                    stripped += 1
            think_stats = {
                "enabled": True, "stripped": True,
                "stripped_count": stripped, "kept": thinking_keep,
                "total_thinking": len(thinking_indices),
                "frozen_thinking_count": len(thinking_indices) - len(dynamic_thinking),
            }
        elif dynamic_thinking:
            think_stats = {"enabled": True, "skipped": True, "reason": "few_dynamic_thinking",
                           "count": len(dynamic_thinking)}

    # Aggregate compression stats
    aggregated_compress_stats = {"enabled": False, "compressed_count": 0, "saved_chars": 0}
    if compress_stats_list:
        original_total = sum(s["original_len"] for s in compress_stats_list)
        compressed_total = sum(s["compressed_len"] for s in compress_stats_list)
        strategies = {}
        for s in compress_stats_list:
            strategies[s["strategy"]] = strategies.get(s["strategy"], 0) + 1
        aggregated_compress_stats = {
            "enabled": True,
            "compressed_count": len(compress_stats_list),
            "original_chars": original_total,
            "compressed_chars": compressed_total,
            "saved_chars": original_total - compressed_total,
            "ratio": round(compressed_total / original_total, 4) if original_total else 1.0,
            "strategies": strategies,
            "audit_failures": sum(1 for s in compress_stats_list if not s["audit_pass"]),
        }

    return messages, {"clear": clear_stats, "think": think_stats, "compress": aggregated_compress_stats}
# --- clear_old_tool_results ---
def clear_old_tool_results(messages, tools_list=None, clear_zone_pct=None):
    """
    Legacy wrapper around _compress_content_pass.

    Originally a standalone 250-line implementation, now delegates to the
    unified single-pass compressor to eliminate duplication.  Preserves the
    original return signature (messages, flat_stats_dict) so existing unit
    tests and documentation continue to work.
    """
    total_chars = _estimate_message_chars(messages)
    stage_config = {
        "stage": "legacy",
        "total_chars": total_chars,
        "frozen_head": _ps.PROXY_FROZEN_HEAD,
        "clear_zone_pct": clear_zone_pct,
        "thinking_keep": 0,
        "truncate_rounds": None,
        "oom_safety": False,
    }
    messages, combined = _compress_content_pass(
        messages, tools_list=tools_list, stage_config=stage_config
    )
    clear_stats = combined.get("clear", {})
    # Map nested format back to the flat dict expected by legacy callers/tests
    stats = dict(clear_stats)
    stats.setdefault("high_prio", 0)
    stats.setdefault("dedup_bash", 0)
    stats.setdefault("dedup_chars_saved", 0)
    stats.setdefault("frozen_head", stage_config["frozen_head"])
    return messages, stats
# --- _compute_adaptive_rounds ---
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
# --- _extract_middle_summary_rules ---
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
# --- _compress_middle_with_llm ---
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
            "model": _ps.MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
            "temperature": 0.3,
            "stream": False,
        }
        req_data = json.dumps(payload).encode("utf-8")
        with _ps._llama_lock:
            req = urllib.request.Request(
                f"{_ps.LLAMA_BASE}/chat/completions",
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
        _log(f"  -> LLM compression failed: {e}, falling back to rules")
        return None
# --- _merge_summaries_with_llm ---
def _merge_summaries_with_llm(old_summary, new_summary, timeout=15):
    try:
        prompt = (
            "Merge these two session summaries into one concise summary. "
            "Keep all errors, file states, and decisions. Remove redundancy.\n\n"
            f"<previous_summary>\n{old_summary[:3000]}\n</previous_summary>\n\n"
            f"<new_summary>\n{new_summary[:3000]}\n</new_summary>"
        )
        payload = {
            "model": _ps.MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 800,
            "temperature": 0.3,
            "stream": False,
        }
        req_data = json.dumps(payload).encode("utf-8")
        with _ps._llama_lock:
            req = urllib.request.Request(
                f"{_ps.LLAMA_BASE}/chat/completions",
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
        _log(f"  -> Summary merge failed: {e}, concatenating")
        return old_summary + "\n\n" + new_summary
# --- _incremental_compress ---
def _incremental_compress(dropped, session_id):
    with _ps._summary_cache_lock:
        cache = _ps._summary_cache.get(session_id)

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
            if len(combined) > _ps._SUMMARY_CACHE_MAX_CHARS:
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

    with _ps._summary_cache_lock:
        if len(_ps._summary_cache) >= _ps._SUMMARY_CACHE_MAX_SESSIONS:
            oldest_key = next(iter(_ps._summary_cache))
            del _ps._summary_cache[oldest_key]
        _ps._summary_cache[session_id] = {
            "last_compressed_msg_index": len(dropped),
            "summary": compressed_text[:_ps._SUMMARY_CACHE_MAX_CHARS],
        }

    return compressed_text, cache is not None
# --- _is_tool_result_message ---
def _is_tool_result_message(msg):
    """Return True iff `msg` is a user message whose content contains at
    least one `tool_result` block. Used by the smart strategy to classify
    messages that should never be dropped/compressed (their content is the
    high-value file/exec output the model will re-read if lost)."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content", [])
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False
# --- _compress_assistant_message ---
def _compress_assistant_message(msg):
    """Return a copy of an assistant message with non-tool_use text blocks
    replaced by a fixed placeholder. Tool_use blocks are kept verbatim
    because the model's subsequent tool_result depends on the tool name +
    args. The fixed placeholder text preserves prefix-cache stability."""
    content = msg.get("content", [])
    if isinstance(content, list):
        compressed_blocks = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                compressed_blocks.append(b)
            else:
                compressed_blocks.append({"type": "text", "text": "[reasoning omitted]"})
        return {**msg, "content": compressed_blocks}
    if isinstance(content, str):
        return {**msg, "content": "[reasoning omitted]"}
    return msg
# --- _apply_smart_truncation ---
def _apply_smart_truncation(messages, budget_chars=None, session_id=None):
    """Phase 2 改进2 (proxy-truncation-agent-scenario.md): role+content-aware
    truncation. Preserves high-value content (system, tool_result, recent
    user turns) verbatim; compresses assistant reasoning text into a fixed
    placeholder. Iterates from newest to oldest, fitting each message into
    the char budget and falling back to a compressed form for assistant
    messages when the original would overflow.

    Priority (high → low):
      1. system messages           — always kept
      2. tool_result messages      — always kept (file contents survive)
      3. user messages (no tool)   — newest first, then older
      4. assistant messages        — newest first; reasoning compressed
                                     if original doesn't fit, dropped if
                                     compressed form still doesn't fit

    Returns (result_messages, stats_dict). The stats dict has:
      strategy='smart', truncated, dropped_messages, kept_messages,
      compressed_assistants, kept_chars, budget_chars
    """
    if budget_chars is None:
        budget_chars = _ps.PROXY_CHARS_EXPANSION

    total_input_chars = _estimate_message_chars(messages)
    # Below budget: nothing to do.
    if total_input_chars <= budget_chars:
        return messages, {
            "enabled": True,
            "strategy": "smart",
            "skipped": True,
            "reason": "below_budget",
            "chars": total_input_chars,
            "budget_chars": budget_chars,
        }

    # Step 1: classify. Use id-based sets to avoid relying on dict equality
    # (Anthropic SDK message dicts are not hashable, so `m in system`
    # would also be O(n²) and unreliable for nested mutations).
    system_msgs = [m for m in messages if m.get("role") == "system"]
    system_ids = {id(m) for m in system_msgs}
    tool_result_msgs = [m for m in messages if id(m) not in system_ids and _is_tool_result_message(m)]
    tool_result_ids = {id(m) for m in tool_result_msgs}
    other_msgs = [m for m in messages
                  if id(m) not in system_ids and id(m) not in tool_result_ids]

    # Step 2: always keep system + tool_result; measure their cost.
    kept = list(system_msgs) + list(tool_result_msgs)
    kept_chars = _estimate_message_chars(kept)

    # If the must-keep set alone exceeds budget, we still have to keep
    # them (otherwise the model would lose file contents and trigger a
    # re-read loop). The caller is expected to catch this with the
    # OOM_SAFE hard ceiling before reaching here.
    if kept_chars > budget_chars:
        return kept, {
            "enabled": True,
            "strategy": "smart",
            "truncated": True,
            "dropped_messages": len(other_msgs),
            "kept_messages": len(kept),
            "compressed_assistants": 0,
            "kept_chars": kept_chars,
            "budget_chars": budget_chars,
            "reason": "must_keep_exceeds_budget",
        }

    # Step 3: walk other_msgs newest-first. For each message, try to keep
    # it as-is. If it doesn't fit and is an assistant, try the compressed
    # form. Otherwise drop.
    compressed_count = 0
    dropped_count = 0
    # Insert in chronological order, so we prepend and reverse at the end.
    chosen = []
    for msg in reversed(other_msgs):
        msg_chars = _estimate_message_chars([msg])
        if kept_chars + msg_chars <= budget_chars:
            chosen.append(msg)
            kept_chars += msg_chars
            continue
        if msg.get("role") == "assistant":
            compressed = _compress_assistant_message(msg)
            comp_chars = _estimate_message_chars([compressed])
            if kept_chars + comp_chars <= budget_chars:
                chosen.append(compressed)
                kept_chars += comp_chars
                compressed_count += 1
                continue
        dropped_count += 1

    # Reverse `chosen` to restore chronological order, then assemble.
    chosen.reverse()
    result = kept + chosen
    result = _fix_tool_pairings(result)
    return result, {
        "enabled": True,
        "strategy": "smart",
        "truncated": dropped_count > 0 or compressed_count > 0,
        "dropped_messages": dropped_count,
        "kept_messages": len(result),
        "compressed_assistants": compressed_count,
        "kept_chars": kept_chars,
        "budget_chars": budget_chars,
    }
# --- truncate_messages_if_needed ---
def truncate_messages_if_needed(messages, session_id=None, keep_rounds=None):
    """
    Proxy-side message truncation with dual strategy support.

    Strategy 'char' (default): drop old messages until total chars fall below
    _ps.PROXY_CTX_CHARS_LIMIT. Preserves head + tail window.

    Strategy 'rounds': keep only the most recent N assistant rounds,
    replacing dropped messages with a lightweight placeholder.
    When keep_rounds is provided (from lifecycle stage config), it overrides
    the default adaptive_rounds computation.

    Char-based budget: uses _ps.PROXY_CHARS_EXPANSION (chars) as the unified
    trigger threshold, replacing the old token-budget _ps.PROXY_CTX_TOKEN_BUDGET.
    Operates on Anthropic-format messages in-place.
    Returns (messages, stats_dict).
    """
    if not _ps.PROXY_CTX_LIMIT_ENABLED and _ps.PROXY_CTX_TRUNCATE_STRATEGY != "rounds":
        return messages, {"enabled": False}

    # ---------- rounds strategy ----------
    if _ps.PROXY_CTX_TRUNCATE_STRATEGY == "rounds":
        # keep_rounds=None: stage says skip truncation entirely
        if keep_rounds is None:
            return messages, {"enabled": True, "strategy": "rounds", "skipped": True, "reason": "stage_skip"}
        total_chars = _estimate_message_chars(messages)
        # Char-based budget check: skip if within _ps.PROXY_CHARS_EXPANSION
        if total_chars <= _ps.PROXY_CHARS_EXPANSION:
            return messages, {
                "enabled": True, "strategy": "rounds", "skipped": True,
                "reason": "below_budget",
                "chars": total_chars,
                "budget_chars": _ps.PROXY_CHARS_EXPANSION,
            }

        # Use stage-config keep_rounds if provided, else adaptive.
        # P0 fix: both branches iterate down from the initial rounds to fit
        # the char budget. Previously the stage-specified branch did a single
        # pass and returned even if the result was still over budget — which
        # let oversized agent sessions (50+ msgs/round, 200+ rounds) leak
        # through with hundreds of thousands of chars.
        min_rounds = 2
        if keep_rounds is not None:
            adaptive_rounds = keep_rounds
            for rounds in range(keep_rounds, min_rounds - 1, -1):
                result, stats = _apply_rounds_truncation(messages, rounds, session_id=session_id)
                if not stats.get("truncated"):
                    return result, stats
                result_chars = _estimate_message_chars(result)
                if result_chars <= _ps.PROXY_CHARS_EXPANSION or rounds == min_rounds:
                    stats["chars"] = result_chars
                    stats["budget_chars"] = _ps.PROXY_CHARS_EXPANSION
                    stats["actual_keep_rounds"] = rounds
                    stats["stage_keep_rounds"] = keep_rounds
                    stats["adaptive_rounds"] = adaptive_rounds
                    stats["budget_iterations"] = keep_rounds - rounds
                    return result, stats
        else:
            # Backward-compatible: adaptive rounds + LLM/rule compression
            adaptive_rounds = _compute_adaptive_rounds(messages, _ps.PROXY_CTX_KEEP_ROUNDS)
            for rounds in range(adaptive_rounds, min_rounds - 1, -1):
                result, stats = _apply_rounds_truncation(messages, rounds, session_id=session_id)
                if not stats.get("truncated"):
                    return result, stats
                result_chars = _estimate_message_chars(result)
                if result_chars <= _ps.PROXY_CHARS_EXPANSION or rounds == min_rounds:
                    stats["chars"] = result_chars
                    stats["budget_chars"] = _ps.PROXY_CHARS_EXPANSION
                    stats["actual_keep_rounds"] = rounds
                    stats["adaptive_rounds"] = adaptive_rounds
                    return result, stats

        return messages, {"enabled": True, "strategy": "rounds", "skipped": True, "reason": "no_reduction"}

    # ---------- fifo strategy ----------
    if _ps.PROXY_CTX_TRUNCATE_STRATEGY == "fifo":
        n = len(messages)
        keep_total = _ps.PROXY_CTX_KEEP_MESSAGES
        if n <= keep_total:
            return messages, {
                "enabled": True, "strategy": "fifo", "skipped": True,
                "reason": "below_limit", "count": n, "limit": keep_total,
            }

        head = messages[:_ps.PROXY_CTX_KEEP_HEAD]
        tail_count = keep_total - _ps.PROXY_CTX_KEEP_HEAD
        tail = messages[-tail_count:]
        dropped = messages[_ps.PROXY_CTX_KEEP_HEAD : n - tail_count]
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

        # Plan 1 (prefix-cache fix): the placeholder text MUST be byte-for-byte
        # identical across requests. Including dropped_count / tool_count /
        # file_mentions here changes the text every request, breaking the
        # cache at the placeholder boundary and dropping hit rate to 0%.
        # Dynamic info is still kept in the stats dict below (used for
        # proxy_metrics.jsonl) — it just doesn't leak into the prompt.
        compressed_text = "[Context folded: earlier messages omitted.]"

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

        result = _fix_tool_pairings(result)

        return result, {
            "enabled": True,
            "strategy": "fifo",
            "truncated": True,
            "dropped_messages": dropped_count,
            "kept_messages": len(result),
            "tool_count": tool_count,
            "file_mentions": len(file_mentions),
        }

    # ---------- smart strategy (Phase 2 改进2) ----------
    # Role+content-aware truncation. Keeps system + tool_result verbatim
    # (preserves file contents to avoid re-read loops), then keeps newer
    # user/assistant messages in reverse-chronological order until the
    # _ps.PROXY_CHARS_EXPANSION budget is filled. Assistant messages that
    # don't fit are first attempted in compressed form (tool_use blocks
    # kept, reasoning text replaced by a stable placeholder) before being
    # dropped entirely.
    if _ps.PROXY_CTX_TRUNCATE_STRATEGY == "smart":
        return _apply_smart_truncation(
            messages, budget_chars=_ps.PROXY_CHARS_EXPANSION, session_id=session_id,
        )

    # ---------- char strategy (and any other unhandled strategy) ----------
    # Falls back to no-op truncation when strategy is "char" or anything other
    # than rounds/fifo/smart. The actual char-window implementation lives
    # (misnamed) inside _apply_rounds_truncation above and is currently only
    # invoked through that path. Returning a no-op stats dict here prevents
    # the caller from hitting `TypeError: cannot unpack non-iterable
    # NoneType object`.
    return messages, {
        "enabled": True,
        "strategy": _ps.PROXY_CTX_TRUNCATE_STRATEGY,
        "skipped": True,
        "reason": "char_strategy_uses_noop_fallback",
    }
# --- _fix_tool_pairings ---
def _fix_tool_pairings(messages):
    """Repair orphaned tool_use/tool_result blocks after truncation.

    After rounds/fifo truncation, the message list may contain:
    - tool_result blocks referencing tool_use_ids that were dropped
    - tool_use blocks whose tool_result was dropped

    Both cases cause OpenAI-compatible backends (DeepSeek/OpenAI) to reject
    the request with 400: "tool_calls must be followed by tool messages".

    This function:
    1. Collects all tool_use_ids from assistant messages
    2. Removes orphaned tool_result blocks (no matching tool_use)
    3. Removes orphaned tool_use blocks from assistant messages (no matching tool_result)
    4. Drops user messages that become empty after tool_result removal
    """
    valid_tool_use_ids = set()
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tid = b.get("id", "")
                if tid:
                    valid_tool_use_ids.add(tid)

    answered_tool_use_ids = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id", "")
                if tid:
                    answered_tool_use_ids.add(tid)

    result = []
    removed_results = 0
    removed_uses = 0
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")

        if role == "user" and isinstance(content, list):
            new_blocks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id", "")
                    if tid not in valid_tool_use_ids:
                        removed_results += 1
                        continue
                new_blocks.append(b)
            if not new_blocks:
                continue
            m = dict(m)
            m["content"] = new_blocks

        elif role == "assistant" and isinstance(content, list):
            new_blocks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tid = b.get("id", "")
                    if tid and tid not in answered_tool_use_ids:
                        removed_uses += 1
                        continue
                new_blocks.append(b)
            if not new_blocks:
                continue
            m = dict(m)
            m["content"] = new_blocks

        result.append(m)

    if removed_results or removed_uses:
        _log(f"  -> Tool pairing fix: removed {removed_results} orphaned tool_results, "
            f"{removed_uses} orphaned tool_uses")

    result = _reorder_tool_results(result)

    return result
# --- _reorder_tool_results ---
def _reorder_tool_results(messages):
    """Ensure tool_result messages immediately follow their tool_use.

    OpenAI/DeepSeek strictly require that every tool_calls message is followed
    by tool role messages (one per tool_call_id) before any other role.
    Anthropic format allows text user messages between tool_use and tool_result.

    Only reorders when needed: if tool_result already immediately follows
    its tool_use, no change is made.
    """
    tool_result_msg_idx = {}
    for i, m in enumerate(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id", "")
                if tid:
                    tool_result_msg_idx[tid] = i

    tool_use_to_assistant_idx = {}
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tid = b.get("id", "")
                if tid:
                    tool_use_to_assistant_idx[tid] = i

    needs_reorder = False
    for tid, result_idx in tool_result_msg_idx.items():
        assistant_idx = tool_use_to_assistant_idx.get(tid)
        if assistant_idx is not None and result_idx != assistant_idx + 1:
            needs_reorder = True
            break

    if not needs_reorder:
        return messages

    _log(f"  -> Tool pairing fix: reordering {len(tool_result_msg_idx)} tool_results for adjacency")

    # Build set of all tool_use_ids so we can defer tool_results that appear
    # before their assistant message (they'll be emitted inline later).
    all_tool_use_ids = set()
    for m in messages:
        if m.get("role") == "assistant":
            content = m.get("content", "")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        all_tool_use_ids.add(b.get("id", ""))

    seen_assistant_tool_uses = set()
    emitted_indices = set()
    result = []
    for i, m in enumerate(messages):
        if i in emitted_indices:
            continue
        role = m.get("role", "")
        content = m.get("content", "")

        if role == "user" and isinstance(content, list):
            tids_in_msg = [b.get("tool_use_id", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "tool_result"]
            if tids_in_msg:
                deferred = all(tid not in seen_assistant_tool_uses for tid in tids_in_msg if tid)
                if deferred:
                    continue
        result.append(m)
        if role == "assistant" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tid = b.get("id", "")
                    seen_assistant_tool_uses.add(tid)
                    result_idx = tool_result_msg_idx.get(tid)
                    if result_idx is not None and result_idx != i + 1:
                        tr_msg = messages[result_idx]
                        result.append(tr_msg)
                        emitted_indices.add(result_idx)

    return result
# --- _apply_rounds_truncation ---
def _apply_rounds_truncation(messages, keep_rounds, session_id=None):
    head = messages[:_ps.PROXY_CTX_KEEP_HEAD]

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

    dropped = messages[_ps.PROXY_CTX_KEEP_HEAD : len(messages) - len(tail)]

    # NEW: Preserve Read tool_results from the dropped zone to prevent re-read loops.
    # Read tool_results contain file contents that LLM summaries cannot replace.
    tool_map = _build_tool_use_map(messages)
    read_results = []
    remaining_dropped = []
    for m in dropped:
        is_read_result = False
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                for b in content:
                    if b.get("type") == "tool_result":
                        if tool_map.get(b.get("tool_use_id", "")) == "Read":
                            is_read_result = True
                            break
        if is_read_result:
            read_results.append(m)
        else:
            remaining_dropped.append(m)

    dropped = remaining_dropped
    dropped_count = len(dropped)

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

    if _ps.PROXY_HISTORY_INDEX == "rule" and dropped_count >= 5:
        keywords = _extract_keywords(dropped)
        keyword_ctx = _inject_keyword_context(
            keywords, tail,
            top_k=_ps.PROXY_HISTORY_TOP_K,
            max_chars=_ps.PROXY_HISTORY_MAX_CHARS,
        )
        if keyword_ctx:
            compressed_text += "\n\n" + keyword_ctx

    # Assemble result with preserved Read results inserted between summary and tail
    result = list(head)

    if compressed_text:
        if tail and tail[0].get("role") == "user":
            # Copy first tail msg to avoid mutating original messages list
            modified_tail0 = dict(tail[0])
            tail_content = modified_tail0.get("content", [])
            summary_block = {"type": "text", "text": compressed_text}
            if isinstance(tail_content, list):
                modified_tail0["content"] = [summary_block] + list(tail_content)
            else:
                modified_tail0["content"] = [summary_block, {"type": "text", "text": str(tail_content)}]
            result.append(modified_tail0)
            result.extend(tail[1:])
        else:
            summary = {"role": "user", "content": [{"type": "text", "text": compressed_text}]}
            result.append(summary)
            result.extend(tail)
    else:
        result.extend(tail)

    # Insert preserved Read results between head/summary and tail
    tail_start_in_result = len(head)
    if not (tail and tail[0].get("role") == "user") and compressed_text:
        tail_start_in_result += 1
    if read_results:
        result = result[:tail_start_in_result] + read_results + result[tail_start_in_result:]

    result = _fix_tool_pairings(result)

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

__all__ = [
    "_compress_content_pass",
    "clear_old_tool_results",
    "_compute_adaptive_rounds",
    "_extract_middle_summary_rules",
    "_compress_middle_with_llm",
    "_merge_summaries_with_llm",
    "_incremental_compress",
    "_is_tool_result_message",
    "_compress_assistant_message",
    "_apply_smart_truncation",
    "truncate_messages_if_needed",
    "_fix_tool_pairings",
    "_reorder_tool_results",
    "_apply_rounds_truncation",
]
