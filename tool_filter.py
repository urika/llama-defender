"""Auto-extracted tool_filter module."""
import re
import proxy_state as _ps

# --- _filter_tools ---
def _inject_ctx_recall(tools_list):
    """P4 Recall(MVP): ctx_recall 工具注入——独立于任何过滤护栏。

    below_max / too_few_after_filter 早退分支同样注入（TC01/TC02 契约：
    注入只依赖 PROXY_PD_ENABLED，不依赖裁剪是否发生）。幂等：已在列不
    重复。fail-open：ctx_recall 不可导入时原样返回。
    """
    if not tools_list or not getattr(_ps, "PROXY_PD_ENABLED", True):
        return tools_list
    try:
        from ctx_recall import TOOL_SCHEMA as _CTX_RECALL_TOOL
        if _CTX_RECALL_TOOL["name"] not in {t.get("name") for t in tools_list if isinstance(t, dict)}:
            return tools_list + [_CTX_RECALL_TOOL]
    except ImportError:
        pass
    return tools_list


def _apply_tool_denylist(tools):
    """L-26 纵深防御(2026-09-12): 按名字剥离工具定义——模型"看不见"即不会调用。

    PROXY_TOOLS_DENYLIST（reloadable, 逗号分隔, 默认空=不过滤）。
    背景: SW-2 闭网经 --disallowedTools 在 CLI 侧拦截, 但 CLI 版本/模式差异
    可致穿透(bypassPermissions 下"移出 allowedTools ≠ 禁用", EXP-6 实测
    17/47 runs 泄漏)。代理层剥 tools 定义是独立于 CLI 的第二道闸。
    只剥定义不动历史消息: 历史 tool_use 不在 tools 定义里是合法状态
    (tools 只约束新调用), 不碰消息对避免配对断裂。
    """
    deny = getattr(_ps, "PROXY_TOOLS_DENYLIST", "") or ""
    names = {n.strip() for n in deny.split(",") if n.strip()}
    if not names or not tools:
        return tools, 0
    kept = [t for t in tools
            if not (isinstance(t, dict) and t.get("name", "") in names)]
    return kept, len(tools) - len(kept)


def _filter_tools(tools, messages, recent_rounds=5, tool_choice_name=None, session_id=""):
    # L-26: denylist 先于一切早退分支(含 below_max)——黑名单剥离无条件生效
    tools, _denied = _apply_tool_denylist(tools)
    if not tools or len(tools) <= _ps.PROXY_TOOL_FILTER_MAX:
        return _inject_ctx_recall(tools), {"filtered": False, "reason": "below_max",
                                           "denied": _denied}

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

    # DEF-104: Auto-promote frequently used tools from session history.
    # Tools used >= PROXY_TOOL_AUTO_PROMOTE_THRESHOLD times in this session
    # are added to keep_set, preventing first-use filtering when Claude Code
    # introduces new tools.
    auto_promoted = set()
    if _ps.PROXY_TOOL_AUTO_PROMOTE_THRESHOLD > 0 and session_id:
        freq = _ps._SESSION_TOOL_FREQ.get(session_id, {})
        for tool_name, count in freq.items():
            if count >= _ps.PROXY_TOOL_AUTO_PROMOTE_THRESHOLD and tool_name not in always_keep_set:
                auto_promoted.add(tool_name)
                keep_set.add(tool_name)

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

    # 2026-09-05(TC01): 移除 too_few_after_filter 早退——keep 集不足时
    # 原样放行会连 ctx_recall 注入一并放弃（零触发链第一环）；饥饿由
    # 下方 filler 补足到 MAX 解决，同序稳定性不受影响。

    kept_names = {t.get("name", "") for t in kept if isinstance(t, dict)}
    if len(kept) < _ps.PROXY_TOOL_FILTER_MAX:
        # DEF-203: Use canonical filler tools from TOOL_ALWAYS_KEEP instead of
        # client-dependent remaining tools. This ensures the tool definition
        # sequence is identical across sessions/requests, maximizing prefix cache hits.
        canonical_fillers = [t for t in tools if isinstance(t, dict) and t.get("name", "") in _ps.TOOL_ALWAYS_KEEP and t.get("name", "") not in kept_names]
        canonical_fillers.sort(key=lambda t: _ps.TOOL_ALWAYS_KEEP.index(t.get("name", "")))
        needed = _ps.PROXY_TOOL_FILTER_MAX - len(kept)
        kept.extend(canonical_fillers[:needed])
        # If still not enough, fall back to client tools (alphabetical)
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

    # P4 Recall(MVP): 注入 ctx_recall 工具——渐进披露的拉取接口。
    # 无论过滤与否都追加到列表末尾(不占 PROXY_TOOL_FILTER_MAX 名额)。
    kept = _inject_ctx_recall(kept)

    return kept, {
        "filtered": True,
        "original": len(tools),
        "kept": len(kept),
        "always_keep": len(always_keep_set & kept_names),
        "recent_only": len(recent_tools - always_keep_set),
        "recent_tools": sorted(recent_tools),
        "scanned_assistant": assistant_count,
        "filtered_out": filtered_out,
        "auto_promoted": sorted(auto_promoted) if auto_promoted else [],
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
                # PDC-L2(2026-08-31): 提示改指向 ctx_recall——原"用 Bash cat
                # 代替"把模型推向环境重读; 该文件此前读过, 内容在折叠区可召回。
                block["content"] = (
                    "[System: 该文件自上次读取后未发生变化，不要再使用 Read 工具反复读取。"
                    "先用 ctx_recall 工具查询（query=文件路径或关键词）取回此前内容；"
                    "ctx_recall 无结果再用 Bash cat 命令代替。]"
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
