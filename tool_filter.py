"""Auto-extracted tool_filter module."""
import re
import proxy_state as _ps

# --- _filter_tools ---
def _filter_tools(tools, messages, recent_rounds=5, tool_choice_name=None):
    if not tools or len(tools) <= _ps.PROXY_TOOL_FILTER_MAX:
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

    always_keep_set = set(_ps.TOOL_ALWAYS_KEEP)
    keep_set = always_keep_set | recent_tools
    if tool_choice_name:
        keep_set.add(tool_choice_name)

    # Phase 1: sort kept tools by a stable order (always-keep first, then recent,
    # then alphabetical) so the prefix token sequence is identical across requests
    # when the same tools are available.
    def _tool_sort_key(t):
        name = t.get("name", "")
        if name in always_keep_set:
            # preserve the order defined in TOOL_ALWAYS_KEEP
            return (0, _ps.TOOL_ALWAYS_KEEP.index(name))
        if name in recent_tools:
            return (1, name)
        return (2, name)

    kept = sorted(
        [t for t in tools if isinstance(t, dict) and t.get("name", "") in keep_set],
        key=_tool_sort_key
    )

    if len(kept) < 5:
        return tools, {"filtered": False, "reason": "too_few_after_filter"}

    kept_names = {t.get("name", "") for t in kept if isinstance(t, dict)}
    if len(kept) < _ps.PROXY_TOOL_FILTER_MAX:
        remaining = sorted(
            [t for t in tools if isinstance(t, dict) and t.get("name", "") not in kept_names],
            key=lambda t: t.get("name", "")
        )
        kept.extend(remaining[:_ps.PROXY_TOOL_FILTER_MAX - len(kept)])
        kept.sort(key=_tool_sort_key)
        kept_names = {t.get("name", "") for t in kept if isinstance(t, dict)}

    all_names = {t.get("name", "") for t in tools if isinstance(t, dict)}
    filtered_out = sorted(all_names - kept_names)

    return kept, {
        "filtered": True,
        "original": len(tools),
        "kept": len(kept),
        "always_keep": len(always_keep_set & kept_names),
        "recent_only": len(recent_tools - always_keep_set),
        "recent_tools": sorted(recent_tools),
        "scanned_assistant": assistant_count,
        "filtered_out": filtered_out,
    }
# --- _extract_keywords ---
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
# --- _inject_keyword_context ---
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
# --- _translate_tool_result_errors ---
def _translate_tool_result_errors(messages):
    """Walk the user-side tool_result blocks and rewrite known backend
    error patterns into natural-language Chinese hints. Returns
    (messages, counts_dict) and mutates `messages` in place.

    Three patterns are recognised:
      - "Wasted call"     → "文件自上次读取后未发生变化"  (R5.1 wasted)
      - "File does not exist" / "No such file" → "文件不存在..."  (R5.1 file_not_found)
      - "InputValidationError" / "invalid x" → "工具调用参数错误..."  (R5.1 input_validation)

    Each replacement includes a solution hint (R5.2):
      - wasted: 用 Bash cat 代替
      - file_not_found: 用 Bash ls 或 find 确认项目结构
      - input_validation: 检查工具参数格式
    """
    error_count = {"wasted": 0, "file_not_found": 0, "input_validation": 0}
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
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
    return messages, error_count

__all__ = [
    "_filter_tools",
    "_extract_keywords",
    "_inject_keyword_context",
    "_translate_tool_result_errors",
]
