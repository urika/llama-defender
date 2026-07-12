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

    # ---- Phase 1a: BM25 scoring (TS-1 W3 d4) ----
    # TS-2 cross-segment pair protection: tool_results whose tool_use is in
    # the frozen prefix are excluded from BM25 scoring and compression.
    protected_tr_indices = set()
    if frozen_head > 0:
        protected_pairs = _protected_pair_indices(messages, frozen_head)
        for msg_idx, block_idx in all_tool_result_indices:
            if msg_idx in protected_pairs and msg_idx < frozen_head:
                protected_tr_indices.add((msg_idx, block_idx))

    bm25_scores = {}
    if _ps.PROXY_BM25_ENABLED and _ps.PROXY_COMPRESS_ENABLED:
        from content_compressor import bm25_score_message, _extract_last_user_text, _update_idf
        query = _extract_last_user_text(messages)
        if query:
            _update_idf(messages)
            for msg_idx, block_idx in all_tool_result_indices:
                if frozen_head > 0 and msg_idx < frozen_head:
                    continue
                if (msg_idx, block_idx) in protected_tr_indices:
                    continue
                block = messages[msg_idx]["content"][block_idx]
                content = block.get("content", "")
                if not content:
                    continue
                score = bm25_score_message(
                    {"role": "user", "content": [{"type": "text", "text": content}]},
                    query=query,
                )
                bm25_scores[(msg_idx, block_idx)] = score

    # ---- Phase 1b: semantic compression of tool_result contents (Phase 2) ----
    compress_stats_list = []
    if _ps.PROXY_COMPRESS_ENABLED:
        # Sort by BM25 score ascending (low-score first) when BM25 is active.
        if bm25_scores:
            ordered_indices = sorted(all_tool_result_indices, key=lambda i: bm25_scores.get(i, 0.0))
        else:
            ordered_indices = list(all_tool_result_indices)

        for msg_idx, block_idx in ordered_indices:
            if frozen_head > 0 and msg_idx < frozen_head:
                continue
            if (msg_idx, block_idx) in protected_tr_indices:
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

            result = compress_tool_result(
                content, mime_hint=mime_hint,
                bm25_score=bm25_scores.get((msg_idx, block_idx)),
                bm25_drop_threshold=_ps.PROXY_BM25_DROP_THRESHOLD,
                bm25_keep_threshold=_ps.PROXY_BM25_KEEP_THRESHOLD,
            )
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
                # TS-1: BM25 low-score penalty — low relevance tool_results get cleared first.
                bm25 = bm25_scores.get((msg_idx, block_idx))
                if bm25 is not None and bm25 < _ps.PROXY_BM25_DROP_THRESHOLD:
                    score -= 10
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

    # Compute compression_ratio (I-4: 1 - compressed/original, higher = more preserved)
    original_total_chars = total_chars
    compressed_total_chars = original_total_chars
    if aggregated_compress_stats.get("enabled"):
        compressed_total_chars = original_total_chars - aggregated_compress_stats.get("saved_chars", 0)
    compression_ratio = 1.0
    if original_total_chars > 0:
        compression_ratio = round(compressed_total_chars / original_total_chars, 4)

    # Compute protected_indices from frozen_head
    protected_indices = list(range(frozen_head)) if frozen_head > 0 else []

    return messages, {
        "clear": clear_stats,
        "think": think_stats,
        "compress": aggregated_compress_stats,
        "strategy": "bm25" if bm25_scores else "rule_based",
        "enabled": aggregated_compress_stats.get("enabled", False) or clear_stats.get("cleared", False),
        "skipped": not (aggregated_compress_stats.get("enabled", False) or clear_stats.get("cleared", False)),
        "compression_ratio": compression_ratio,
        "protected_indices": protected_indices,
        "bm25_scores": {str(k): v for k, v in bm25_scores.items()} if bm25_scores else {},
        "sub": {
            "compress": compress_stats_list,
            "clear": clear_stats,
            "think": think_stats,
        },
    }
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
    # TS-3: add CompressionResult top-level fields
    stats["strategy"] = "rule_based"
    stats["enabled"] = combined.get("enabled", False)
    stats["skipped"] = combined.get("skipped", True)
    stats["compression_ratio"] = combined.get("compression_ratio", 1.0)
    stats["protected_indices"] = combined.get("protected_indices", [])
    stats["bm25_scores"] = combined.get("bm25_scores", {})
    stats["sub"] = {
        "compress": combined.get("sub", {}).get("compress", []),
        "clear": clear_stats,
        "think": combined.get("sub", {}).get("think", {}),
    }
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
def _apply_smart_truncation(messages, budget_chars=None, session_id=None,
                           protected_pairs=None, pair_index_map=None):
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

    TS-2 (W1 d3-4): tool_use/tool_result 配对原子保护.
      当 protected_pairs (set of msg idx) + pair_index_map (idx → (a, u)) 提供时,
      drop 决策点遵守:
        - 被保护集成员不可单边 drop; 必须整对 drop 或整对保留
        - 若无可 drop 的非保护项 → skipped_reason="invalid_anthropic_tool_sequence"
      未提供时 (legacy 调用路径) 退化为旧行为 + 末尾 _fix_tool_pairings 兜底.

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
            "skipped_reason": "below_budget",
            "reason": "below_budget",
            "chars": total_input_chars,
            "budget_chars": budget_chars,
            "protected_indices": list(_protected_pair_indices(messages, _ps.PROXY_CACHE_ALIGN_HEAD)),
            "dropped_indices": [],
            "dropped_messages": 0,
            "kept_messages": len(messages),
            "compressed_assistants": 0,
            "kept_chars": total_input_chars,
        }

    # TS-2: 准备配对保护集 (若调用方未提供, 自行计算).
    if protected_pairs is None:
        protected_pairs = _protected_pair_indices(
            messages, _ps.PROXY_CACHE_ALIGN_HEAD)
    if pair_index_map is None:
        pair_index_map = _pair_index_map(messages)

    # TS-2: 索引 → msg 映射, 用于按索引访问 other_msgs 中的成员.
    # other_msgs 是 messages 减去 system + tool_result; 我们需要原始索引.
    # Build id → original idx 映射 (用 id() 避免 dict 不可哈希问题).
    msg_id_to_idx = {id(m): i for i, m in enumerate(messages)}

    # Step 1: classify. Use id-based sets to avoid relying on dict equality
    # (Anthropic SDK message dicts are not hashable, so `m in system`
    # would also be O(n²) and unreliable for nested mutations).
    system_msgs = [m for m in messages if m.get("role") == "system"]
    system_ids = {id(m) for m in system_msgs}
    tool_result_msgs = [m for m in messages if id(m) not in system_ids and _is_tool_result_message(m)]
    tool_result_ids = {id(m) for m in tool_result_msgs}
    other_msgs = [m for m in messages
                  if id(m) not in system_ids and id(m) not in tool_result_ids]

    # TS-2: 记录 tool_result 在原 messages 中的索引集合, 用于 Step 3 判断 partner 是否在 kept.
    tool_result_orig_idxs = {msg_id_to_idx.get(id(m), -1) for m in tool_result_msgs}

    # Step 2: always keep system + tool_result; measure their cost.
    kept = list(system_msgs) + list(tool_result_msgs)
    kept_chars = _estimate_message_chars(kept)

    # If the must-keep set alone exceeds budget, we still have to keep
    # them (otherwise the model would lose file contents and trigger a
    # re-read loop). The caller is expected to catch this with the
    # OOM_SAFE hard ceiling before reaching here.
    if kept_chars > budget_chars:
        # TS-2: 区分两种超预算:
        #   (a) drop 所有非配对 other 后仍超 budget → 无有效 drop, 标 skipped_reason.
        #   (b) drop 非配对 other 后能达标 → must_keep_exceeds_budget,
        #       走 _fix_tool_pairings 兜底.
        unprotected_other = [m for m in other_msgs
                             if msg_id_to_idx.get(id(m)) not in pair_index_map]
        unprotected_chars = _estimate_message_chars(unprotected_other)
        if kept_chars - unprotected_chars > budget_chars:
            # drop 所有非配对项后仍超 budget: 无有效 drop (配对保护让剩余项不可单边 drop).
            return messages, {
                "enabled": True,
                "strategy": "smart",
                "skipped": True,
                "reason": "invalid_anthropic_tool_sequence",
                "skipped_reason": "invalid_anthropic_tool_sequence",
                "chars": total_input_chars,
                "kept_chars": kept_chars,
                "unprotected_chars": unprotected_chars,
                "budget_chars": budget_chars,
                "protected_indices": list(protected_pairs),
                "dropped_indices": [],
                "dropped_messages": 0,
                "kept_messages": len(messages),
                "compressed_assistants": 0,
            }
        result = _fix_tool_pairings(kept)
        return result, {
            "enabled": True,
            "strategy": "smart",
            "truncated": True,
            "dropped_messages": len(other_msgs),
            "kept_messages": len(result),
            "compressed_assistants": 0,
            "kept_chars": _estimate_message_chars(result),
            "budget_chars": budget_chars,
            "reason": "must_keep_exceeds_budget",
            "skipped_reason": "must_keep_exceeds_budget",
            "protected_indices": list(protected_pairs),
            "dropped_indices": [msg_id_to_idx.get(id(m)) for m in other_msgs
                                if msg_id_to_idx.get(id(m)) is not None],
        }

    # Step 3: walk other_msgs newest-first. For each message, try to keep
    # it as-is. If it doesn't fit and is an assistant, try the compressed
    # form. Otherwise drop.
    compressed_count = 0
    dropped_count = 0
    # Insert in chronological order, so we prepend and reverse at the end.
    chosen = []
    # TS-2: 跟踪被整对 drop 的索引, 避免后续误处理其配对成员.
    dropped_idxs = set()
    for msg in reversed(other_msgs):
        orig_idx = msg_id_to_idx.get(id(msg))
        # TS-2: 若 msg 是被保护对成员且其配对成员尚未处理, 需检查配对状态.
        if orig_idx is not None and orig_idx in pair_index_map:
            a_idx, u_idx = pair_index_map[orig_idx]
            partner_idx = u_idx if orig_idx == a_idx else a_idx
            if partner_idx in dropped_idxs:
                # 配对成员已被 drop, 此条也 drop (整对 drop).
                dropped_count += 1
                dropped_idxs.add(orig_idx)
                continue
            # 否则 msg 的配对成员尚未处理 (可能 system/tool_result/must_keep, 或稍后才处理).
            # 此时 msg 单独处理: 若能 keep 就 keep, 若不能 keep 则需 partner 一起 drop.
            msg_chars = _estimate_message_chars([msg])
            if kept_chars + msg_chars <= budget_chars:
                chosen.append(msg)
                kept_chars += msg_chars
                continue
            # 不能 fit: 尝试压缩 (仅 assistant 走压缩).
            if msg.get("role") == "assistant":
                compressed = _compress_assistant_message(msg)
                comp_chars = _estimate_message_chars([compressed])
                if kept_chars + comp_chars <= budget_chars:
                    chosen.append(compressed)
                    kept_chars += comp_chars
                    compressed_count += 1
                    continue
            # 既不能 keep 也不能压缩: 整对 drop. 标记 dropped_idxs, partner 后续遇到时也 drop.
            # 但 partner 可能已在 must_keep 中 (system/tool_result),
            # 此时不可 drop, 必须强制保留 msg (整对保留).
            partner_in_must_keep = partner_idx in tool_result_orig_idxs or partner_idx < len(system_msgs)
            if partner_in_must_keep:
                # partner 强制 keep, 此条也强制 keep (即使超 budget).
                chosen.append(msg)
                kept_chars += msg_chars
                continue
            # partner 尚未处理且不在 must_keep: 整对 drop.
            dropped_count += 1
            dropped_idxs.add(orig_idx)
            continue

        # 非配对成员, 走原有逻辑.
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

    # TS-2: 若未发生任何 drop 且总长仍超 budget, 说明所有可 drop 项都被配对保护.
    # 此时返回 skipped_reason, 让上层走 fallback (如 rounds / OOMSafetyFIFO).
    if dropped_count == 0 and kept_chars > budget_chars:
        return messages, {
            "enabled": True,
            "strategy": "smart",
            "skipped": True,
            "reason": "invalid_anthropic_tool_sequence",
            "skipped_reason": "invalid_anthropic_tool_sequence",
            "chars": total_input_chars,
            "kept_chars": kept_chars,
            "budget_chars": budget_chars,
            "protected_indices": list(protected_pairs),
            "dropped_indices": [],
            "dropped_messages": 0,
            "kept_messages": len(messages),
            "compressed_assistants": 0,
        }

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
        "protected_indices": list(protected_pairs),
        "dropped_indices": list(dropped_idxs),
    }
# --- truncate_messages_if_needed ---
def truncate_messages_if_needed(messages, session_id=None, keep_rounds=None,
                                strategy=None, budget_chars=None):
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

    TS-2 (W1 d3-4): 可显式传 strategy= 覆盖 PROXY_CTX_TRUNCATE_STRATEGY;
    budget_chars= 覆盖 PROXY_CHARS_EXPANSION (仅 smart 路径使用).
    """
    # TS-2: 显式 strategy 覆盖全局配置.
    effective_strategy = strategy if strategy is not None else _ps.PROXY_CTX_TRUNCATE_STRATEGY

    if not _ps.PROXY_CTX_LIMIT_ENABLED and effective_strategy != "rounds":
        return messages, {"enabled": False, "strategy": effective_strategy, "skipped": True,
                          "skipped_reason": "disabled"}

    # ---------- rounds strategy ----------
    if effective_strategy == "rounds":
        # keep_rounds=None: stage says skip truncation entirely
        if keep_rounds is None:
            return messages, {"enabled": True, "strategy": "rounds", "skipped": True,
                              "skipped_reason": "stage_skip", "reason": "stage_skip"}
        total_chars = _estimate_message_chars(messages)
        # Char-based budget check: skip if within _ps.PROXY_CHARS_EXPANSION
        if total_chars <= _ps.PROXY_CHARS_EXPANSION:
            return messages, {
                "enabled": True, "strategy": "rounds", "skipped": True,
                "skipped_reason": "below_budget",
                "reason": "below_budget",
                "chars": total_chars,
                "budget_chars": _ps.PROXY_CHARS_EXPANSION,
                "protected_indices": list(_protected_pair_indices(messages, _ps.PROXY_CACHE_ALIGN_HEAD)),
                "dropped_indices": [],
                "dropped_messages": 0,
                "kept_messages": len(messages),
                "compressed_assistants": 0,
                "kept_chars": total_chars,
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

        return messages, {"enabled": True, "strategy": "rounds", "skipped": True,
                          "skipped_reason": "no_reduction", "reason": "no_reduction"}

    # ---------- fifo strategy ----------
    if effective_strategy == "fifo":
        n = len(messages)
        keep_total = _ps.PROXY_CTX_KEEP_MESSAGES
        if n <= keep_total:
            return messages, {
                "enabled": True, "strategy": "fifo", "skipped": True,
                "skipped_reason": "below_limit",
                "reason": "below_limit", "count": n, "limit": keep_total,
                "protected_indices": list(_protected_pair_indices(messages, _ps.PROXY_CACHE_ALIGN_HEAD)),
                "dropped_indices": [],
                "dropped_messages": 0,
                "kept_messages": n,
                "compressed_assistants": 0,
                "kept_chars": _estimate_message_chars(messages),
                "budget_chars": _ps.PROXY_CHARS_EXPANSION,
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

        # DEF-107: when drop ratio is high, inject a structured summary
        # instead of a bare placeholder. The summary helps the model
        # understand what was lost without needing to re-read files.
        # The text is still kept stable across requests sharing the same
        # truncation boundary (prefix cache compatible).
        drop_ratio = dropped_count / n if n > 0 else 0
        if drop_ratio > 0.7 and (tool_count > 0 or file_mentions):
            parts = ["[Context folded: earlier messages omitted."]
            if tool_count > 0:
                parts.append(f" {tool_count} tool calls were removed")
            if file_mentions:
                parts.append(f" referenced files: {', '.join(sorted(file_mentions)[:8])}")
            parts.append("]")
            compressed_text = "".join(parts)
        else:
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
            "protected_indices": list(_protected_pair_indices(messages, _ps.PROXY_CACHE_ALIGN_HEAD)),
            "dropped_indices": list(range(_ps.PROXY_CTX_KEEP_HEAD, n - tail_count)),
            "compressed_assistants": 0,
            "kept_chars": _estimate_message_chars(result),
            "budget_chars": _ps.PROXY_CHARS_EXPANSION,
        }

    # ---------- smart strategy (Phase 2 改进2) ----------
    # Role+content-aware truncation. Keeps system + tool_result verbatim
    # (preserves file contents to avoid re-read loops), then keeps newer
    # user/assistant messages in reverse-chronological order until the
    # _ps.PROXY_CHARS_EXPANSION budget is filled. Assistant messages that
    # don't fit are first attempted in compressed form (tool_use blocks
    # kept, reasoning text replaced by a stable placeholder) before being
    # dropped entirely.
    if effective_strategy == "smart":
        effective_budget = budget_chars if budget_chars is not None else _ps.PROXY_CHARS_EXPANSION
        return _apply_smart_truncation(
            messages, budget_chars=effective_budget, session_id=session_id,
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
        "strategy": effective_strategy,
        "skipped": True,
        "truncated": False,
        "skipped_reason": "char_strategy_uses_noop_fallback",
        "reason": "char_strategy_uses_noop_fallback",
        "protected_indices": list(_protected_pair_indices(messages, _ps.PROXY_CACHE_ALIGN_HEAD)),
        "dropped_indices": [],
        "dropped_messages": 0,
        "kept_messages": len(messages),
        "compressed_assistants": 0,
        "kept_chars": _estimate_message_chars(messages),
        "budget_chars": _ps.PROXY_CHARS_EXPANSION,
    }
# --- _find_tool_pairs (TS-2 W1 d1-2) ---
# Anthropic 工具配对原子单元: 事前识别 assistant tool_use → user tool_result 配对区间.
# 设计参考 docs/02-architecture-design/tool-pair-atomicity-design-2026-07-05.md §4.
# 与 _fix_tool_pairings (事后修复) 共存; 本函数供 truncate/clear 决策时事前参考.

def _iter_tool_use_blocks(msg):
    """Yield (tool_use_id, block) for each tool_use block in an assistant message."""
    if not isinstance(msg, dict):
        return
    if msg.get("role") != "assistant":
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            tid = b.get("id", "")
            if tid:
                yield tid


def _iter_tool_result_blocks(msg):
    """Yield (tool_use_id, block) for each tool_result block in a user message."""
    if not isinstance(msg, dict):
        return
    if msg.get("role") != "user":
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            tid = b.get("tool_use_id", "")
            if tid:
                yield tid


def _find_tool_pairs(messages):
    """识别 assistant tool_use → user tool_result 的配对区间 (design §4.1).

    返回: List[(assistant_msg_idx, user_msg_idx)] 按 (a_idx, u_idx) 升序.
      - 正常配对: (a_idx, u_idx)
      - 孤儿 user tool_result (无 sender 或 sender 已被首配对占用): (-1, u_idx)
      - 孤儿 assistant tool_use (无 result 或 result 已被首配对占用): 不出现在结果中
      - 重复 tool_use_id (协议异常): 取首个 assistant + 首个 user 为 (a, u),
        其余同 id 的 user 标 (-1, u_idx); 其余同 id 的 assistant 不返回.
    """
    # Pass 1: 收集每个 tool_use_id 的首个 assistant 索引 (按消息顺序).
    first_assistant_for_id = {}
    for idx, msg in enumerate(messages):
        for tid in _iter_tool_use_blocks(msg):
            if tid not in first_assistant_for_id:
                first_assistant_for_id[tid] = idx

    # Pass 2: 为每个 tool_use_id 配首个 user tool_result 为正式配对,
    # 余下同 id 的 user (sender 已被首配对占用) 标孤儿 (-1, u_idx);
    # 无 sender 的孤儿 user tool_result 不返回 (由 _fix_tool_pairings 兜底).
    first_user_for_id = {}
    orphan_user_indices = []
    for idx, msg in enumerate(messages):
        for tid in _iter_tool_result_blocks(msg):
            if tid in first_assistant_for_id and tid not in first_user_for_id:
                first_user_for_id[tid] = idx
            elif tid in first_assistant_for_id and tid in first_user_for_id:
                # 同 id 重复出现的 user tool_result: sender 已被首配对占用, 标孤儿.
                orphan_user_indices.append((-1, idx))
            # else: 无 sender 的孤儿 user tool_result → 不返回.

    pairs = []
    for tid, a_idx in first_assistant_for_id.items():
        if tid in first_user_for_id:
            pairs.append((a_idx, first_user_for_id[tid]))

    pairs.extend(orphan_user_indices)

    # 排序: 正常对按 (a_idx, u_idx) 升序; 孤儿 (-1, u_idx) 排在最后.
    pairs.sort(key=lambda p: (p[0] if p[0] >= 0 else (1 << 30), p[0], p[1]))
    return pairs


def _protected_pair_indices(messages, protected_prefix_n):
    """返回所有不可单边截断的消息索引集合 (design §4.2).

    集合构成:
      1. 前 protected_prefix_n 条消息 → 全部入集 (CacheAligner protected 段)
      2. _find_tool_pairs 返回的所有 (a_idx, u_idx) 配对索引 → 全部入集
      3. 跨段配对: 若 a_idx 或 u_idx 任一在 protected 段内, 另一个强制入集

    孤儿 (-1, u_idx) 不入保护集 (孤儿由 _fix_tool_pairings 兜底).
    """
    n = len(messages)
    if protected_prefix_n > n:
        protected_prefix_n = n
    protected = set(range(protected_prefix_n))

    pairs = _find_tool_pairs(messages)
    for a_idx, u_idx in pairs:
        if a_idx < 0:
            continue
        protected.add(a_idx)
        protected.add(u_idx)
        # 跨段配对强制保护已在 add 中体现 (无论是否在 protected 段).
    return protected


def _pair_index_map(messages):
    """返回 {msg_idx: (a_idx, u_idx) pair} 反查表,供 truncate 决策点使用.

    若 msg_idx 是某对配对成员, 返回该对; 否则不在表中.
    孤儿 (-1, u_idx) 不在此表中.
    """
    pairs = _find_tool_pairs(messages)
    idx_to_pair = {}
    for a_idx, u_idx in pairs:
        if a_idx < 0:
            continue
        idx_to_pair[a_idx] = (a_idx, u_idx)
        idx_to_pair[u_idx] = (a_idx, u_idx)
    return idx_to_pair


def _oom_safety_fifo(messages, max_chars=None, keep_head=None, keep_tail=None):
    """OOMSafetyFIFO 紧急 FIFO 截断 (design §6.1 I-3).

    紧急路径: 可打破 TS-2 配对保护集, 优先避免 OOM.
    返回 (result_messages, stats_dict), stats 含:
      skipped_reason='oom_emergency'  — 标识打破保护集
      dropped_messages, kept_messages, iterations, chars, budget_chars

    兜底由 _fix_tool_pairings 清理孤儿 (调用方应在收到 oom_emergency 后调用 _fix_tool_pairings).

    参数:
      max_chars  — 字符预算上限 (默认 PROXY_CHARS_OOM_DANGER)
      keep_head  — 保留头部消息数 (默认 PROXY_CTX_KEEP_HEAD)
      keep_tail  — 保留尾部消息数 (默认 PROXY_CTX_KEEP_TAIL)
    """
    if max_chars is None:
        max_chars = _ps.PROXY_CHARS_OOM_DANGER
    if keep_head is None:
        keep_head = _ps.PROXY_CTX_KEEP_HEAD
    if keep_tail is None:
        keep_tail = _ps.PROXY_CTX_KEEP_TAIL

    result = list(messages)
    iteration = 0
    min_keep = max(keep_head + keep_tail, 4)

    while True:
        est_chars = _estimate_message_chars(result)
        if est_chars <= max_chars or len(result) <= min_keep:
            break
        iteration += 1
        if len(result) > min_keep:
            dropped = len(result) - min_keep
            result = result[:keep_head] + result[-(min_keep - keep_head):]
        else:
            break

    # OOM 紧急路径打破保护集, 兜底清理孤儿.
    result = _fix_tool_pairings(result)

    dropped_count = len(messages) - len(result)
    return result, {
        "enabled": True,
        "strategy": "oom_safety_fifo",
        "truncated": dropped_count > 0,
        "dropped_messages": dropped_count,
        "kept_messages": len(result),
        "iterations": iteration,
        "chars": _estimate_message_chars(result),
        "budget_chars": max_chars,
        "skipped_reason": "oom_emergency",
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
        return messages, {
            "enabled": True, "strategy": "rounds", "skipped": True,
            "skipped_reason": "below_limit",
            "protected_indices": list(_protected_pair_indices(messages, _ps.PROXY_CACHE_ALIGN_HEAD)),
            "dropped_indices": [],
            "dropped_messages": 0,
            "kept_messages": len(messages),
            "compressed_assistants": 0,
            "kept_chars": _estimate_message_chars(messages),
            "budget_chars": _ps.PROXY_CHARS_EXPANSION,
        }

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
        "protected_indices": list(_protected_pair_indices(messages, _ps.PROXY_CACHE_ALIGN_HEAD)),
        "dropped_indices": list(range(_ps.PROXY_CTX_KEEP_HEAD, len(messages) - len(tail))),
        "compressed_assistants": 0,
        "kept_chars": _estimate_message_chars(result),
        "budget_chars": _ps.PROXY_CHARS_EXPANSION,
        "sub": {
            "rounds_compression": "llm" if "LLM" in compressed_text else ("rules" if "rule-based" in compressed_text else "folded"),
        },
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
