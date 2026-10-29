"""Auto-extracted tool_filter module."""
import os
import re
import proxy_state as _ps

# --- _filter_tools ---
def _inject_ctx_recall(tools_list):
    """P4 Recall(MVP): ctx_recall 工具注入——独立于任何过滤护栏。

    below_max / too_few_after_filter 早退分支同样注入（TC01/TC02 契约：
    注入不依赖裁剪是否发生）。幂等：已在列不重复。fail-open：ctx_recall
    不可导入时原样返回。

    **必须同时满足代答能力（2026-10-29 修复）**：ctx_recall 是**代理侧**工具，
    只有 `PROXY_PD_MICRO_TURN_ENABLED` 开时代答通道才会真正应答它
    （anthropic_proxy.py 的三处 MICRO_TURN 门）。此前注入只看 `PROXY_PD_ENABLED`
    （缺省 true）而代答缺省关 → 工具被广告给模型但永远无人应答：
    实测 claude-cli 会话（88845194）连回 4 次 `No such tool available`，
    模型「Context overflowed → 从 session store 恢复」的补救路径直接死掉并成环
    （代码内注释记有同型先例：38/38 次零开火、死环烧 38 轮）。
    ⇒ 不能应答就不广告：宁可模型看不见，也不要给一个必然失败的工具。
    """
    if not tools_list or not getattr(_ps, "PROXY_PD_ENABLED", True):
        return tools_list
    if not getattr(_ps, "PROXY_PD_MICRO_TURN_ENABLED", False):
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
    # 2026-09-15 修正: proxy_state 对新 registry 项无手写物化行(属性不存在,
    # getattr 默认空串使 denylist 永远空转——seq47 MCP 泄漏实锤)。改为直读
    # env(CONFIG_REGISTRY 注册项的运行时值即 env, reload 场景由 conf 热载)。
    deny = os.environ.get("PROXY_TOOLS_DENYLIST", "") or getattr(
        _ps, "PROXY_TOOLS_DENYLIST", "") or ""
    names = {n.strip() for n in deny.split(",") if n.strip()}
    if not names or not tools:
        return tools, 0
    kept = [t for t in tools
            if not (isinstance(t, dict) and t.get("name", "") in names)]
    return kept, len(tools) - len(kept)


# --- DEF-313 P1: per-(session, tool-profile) freeze -----------------------
# Qwen renders the tools block at the head of the prompt, so any change to the
# emitted set/order invalidates the whole prefix cache (30-90 s cold prefill at
# 40-70K context). Dynamic keep-set churn (auto-promote growing, recent-window
# sliding) rewrote the set every few turns, leaving no stable cached entry.
#
# Freeze the FILTERED OUTPUT per (session_id, client tool-name set):
#   - the same profile always emits byte-identical tools -> one stable cache
#     entry per profile (splash keeps several, so 28-tool and 22-tool profiles
#     can both stay warm);
#   - different profiles (sub-agents with a restricted toolset) never bleed
#     into each other, so we never offer a child a tool its parent had.
# Snapshot is taken before ctx_recall injection; ctx_recall is re-appended on
# replay so its schema stays current.
_SESSION_TOOL_FREEZE = {}          # (session_id, profile) -> [tool dict]
_SESSION_TOOL_FREEZE_MAX = 512


def _tool_profile(tools):
    return tuple(sorted(
        t.get("name", "") for t in tools
        if isinstance(t, dict) and t.get("name")))


def _freeze_tools(session_id, profile, tools):
    if not session_id or not profile:
        return
    _SESSION_TOOL_FREEZE[(session_id, profile)] = [
        dict(t) for t in tools if isinstance(t, dict)]
    while len(_SESSION_TOOL_FREEZE) > _SESSION_TOOL_FREEZE_MAX:
        try:
            _SESSION_TOOL_FREEZE.pop(next(iter(_SESSION_TOOL_FREEZE)))
        except (StopIteration, KeyError):
            break


def _replay_frozen(session_id, profile, tools):
    """Frozen output for this profile, or None. Definitions are refreshed from
    the current request when the tool is still offered."""
    frozen = _SESSION_TOOL_FREEZE.get((session_id, profile))
    if not frozen:
        return None
    by_name = {t.get("name", ""): t for t in tools if isinstance(t, dict)}
    out = []
    for f in frozen:
        cur = by_name.get(f.get("name", ""))
        out.append(cur if cur is not None else dict(f))
    return out


def _filter_tools(tools, messages, recent_rounds=5, tool_choice_name=None, session_id=""):
    # L-26: denylist 先于一切早退分支(含 below_max)——黑名单剥离无条件生效
    tools, _denied = _apply_tool_denylist(tools)

    # DEF-313 P1: replay this profile's frozen set before any dynamic logic.
    _stable = getattr(_ps, "PROXY_TOOLS_STABLE_SESSION", False)
    _profile = _tool_profile(tools) if (_stable and session_id and tools) else ()
    if _profile:
        _frozen = _replay_frozen(session_id, _profile, tools)
        if _frozen is not None:
            # re-apply: PROXY_TOOLS_DENYLIST is reloadable and a frozen set may
            # predate the current denylist
            _frozen, _denied_frozen = _apply_tool_denylist(_frozen)
            _denied += _denied_frozen
            _out = _inject_ctx_recall(_frozen)
            return _out, {
                "filtered": False, "reason": "session_frozen", "denied": _denied,
                "frozen": True, "kept": len(_out), "original": len(tools),
                "always_keep": 0, "recent_only": 0, "recent_tools": [],
                "scanned_assistant": 0, "filtered_out": [], "auto_promoted": [],
            }

    if not tools or len(tools) <= _ps.PROXY_TOOL_FILTER_MAX:
        if _profile:
            _freeze_tools(session_id, _profile, tools)
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

    # DEF-313 P1: freeze the filtered set for this profile (ctx_recall excluded)
    if _profile:
        _freeze_tools(session_id, _profile, kept)

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
# --- fnf 一期 F1/F2 会话账本（2026-10-25 方案定稿 §1；旗标缺省关=零行为变化）---
_FNF_SEEN = {}        # (session_id, profile) -> {path_norm: count}
_FNF_SEEN_MAX = 512   # 会话数上限，驱逐沿 _SESSION_TOOL_FREEZE
_FNF_DIR_CACHE = {}   # (session_id, dir) -> 截断的 ls 输出（每会话每目录至多一次）
_FNF_SUPPRESS = {}    # (session_id, profile) -> True（L 级注入轮次跳过升级提示）
_FNF_TUID_PATH = {}   # tool_use_id -> 路径（F3 计数键用，转译时记账）
_FNF_TUID_MAX = 1024

# Bash 错误行窄正则（§1.2：只取错误行自报的路径，不从命令串猜）
_FNF_BASH_RES = (
    re.compile(r"(\S+?): No such file or directory"),
    re.compile(r"File does not exist: (.+)"),
)


def _fnf_extract_paths(tool_name, tool_input, error_text):
    """§1.2 分工具路径提取：Read/Grep/Glob 直接取 input；Bash 只认错误行。"""
    paths = []
    if tool_name in ("Read", "Grep", "Glob") and isinstance(tool_input, dict):
        for k in ("file_path", "path", "pattern"):
            v = tool_input.get(k)
            if isinstance(v, str) and v.strip():
                paths.append(v.strip())
                break
    elif tool_name == "Bash":
        seen = set()
        for rx in _FNF_BASH_RES:
            for m in rx.finditer(error_text or ""):
                p = (m.group(1) or "").strip()
                if p and p not in seen:
                    seen.add(p)
                    paths.append(p)
    return paths


def _fnf_workspace_roots(messages):
    """§1.4 工作区根证明：同请求中「成功」Read 的 file_path 公共前缀（≥2 层目录）。

    无成功 Read ⇒ 空集（不取候选，防会话外路径信息泄漏——量测实证存在
    /tmp/config_copy.py 类会话外探测）。"""
    tool_inputs = {}
    results = {}
    for msg in messages:
        c = msg.get("content", "")
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "Read":
                tool_inputs[b.get("id", "")] = b.get("input")
            elif b.get("type") == "tool_result":
                results[b.get("tool_use_id", "")] = str(b.get("content", ""))
    dirs = []
    for tid, inp in tool_inputs.items():
        rc = results.get(tid)
        if rc is None:
            continue
        if ("No such file" in rc or "File does not exist" in rc
                or rc.startswith("[System: 文件不存在")):
            continue  # 失败的 Read 不作数
        fp = (inp or {}).get("file_path") if isinstance(inp, dict) else None
        if isinstance(fp, str) and fp.strip():
            dirs.append(os.path.dirname(fp.strip()))
    if not dirs:
        return set()
    try:
        root = os.path.commonpath(dirs)
    except ValueError:
        return set()
    if root.count(os.sep) < 2:  # ≥2 层目录才算「根」
        return set()
    return {root}


def _fnf_candidates(session_id, parent, messages):
    """§1.4 候选填充器：ls 父目录（子进程 2s 超时、输出截断 512 chars）。

    安全边界（1025-1⑤）：parent 必须落在已证工作区根内，否则返回 ''。"""
    import subprocess
    roots = _fnf_workspace_roots(messages)
    if not roots:
        return ""
    if not any(parent == r or parent.startswith(r.rstrip("/") + os.sep)
               for r in roots):
        return ""
    key = (session_id, parent)
    if key in _FNF_DIR_CACHE:
        return _FNF_DIR_CACHE[key]
    try:
        out = subprocess.run(["ls", "-1", parent], timeout=2,
                             capture_output=True, text=True).stdout
    except Exception:
        out = ""
    cand = (out or "").strip()[:512]
    _FNF_DIR_CACHE[key] = cand
    return cand


def _fnf_hint(path, count, candidates):
    """§1.3 提示分级（确定性文本：同 (path,count) 同文本）。count==2 同 1 文案。"""
    cand_line = f"\n父目录实况：{candidates}" if candidates else ""
    if count >= 3:
        return (f"[System: 该路径已确认不存在（本会话第 {count} 次）：{path}。"
                f"禁止再次读取此路径。{cand_line}\n"
                f"请改用以上真实存在的文件；若确需该文件，先说明它应从何而来。]")
    tail = f"父目录实况：{candidates}。" if candidates else ""
    return (f"[System: 文件不存在：{path}。{tail}"
            f"请改用以上存在的文件，或先确认路径。]")


def _fnf_count(session_id, path_norm):
    key = (session_id, "")
    book = _FNF_SEEN.setdefault(key, {})
    while len(_FNF_SEEN) > _FNF_SEEN_MAX:
        try:
            _FNF_SEEN.pop(next(iter(_FNF_SEEN)))
        except (StopIteration, KeyError):
            break
    book[path_norm] = book.get(path_norm, 0) + 1
    return book[path_norm]


def _fnf_clear(session_id, path_norm):
    """§1.1 清零口：同路径后续成功读取 → 计数清零（文件后来被创建的逃逸口）。"""
    _FNF_SEEN.get((session_id, ""), {}).pop(path_norm, None)


def _fnf_bash_error_paths(error_text):
    """供 loop_detection F3 复用（§1.2 同款窄正则）。"""
    paths = []
    seen = set()
    for rx in _FNF_BASH_RES:
        for m in rx.finditer(error_text or ""):
            p = (m.group(1) or "").strip()
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def _fnf_tuid_path(tool_use_id):
    """F3 计数键取路径：转译通道在 Bash fnf 命中时记账（与旗标无关）。"""
    return _FNF_TUID_PATH.get(tool_use_id, "")


def _translate_tool_result_errors_ex(messages, session_id="", profile=""):
    """F1/F2 增强版转译：fnf 会话账本 + 分级升级提示 + 最近邻候选。

    旗标 PROXY_FNF_GUIDE_ENABLED / PROXY_FNF_CANDIDATES_ENABLED 缺省关
    ⇒ 行为与旧入口完全一致。账本/记账（_FNF_TUID_PATH）与旗标无关。"""
    guide = bool(getattr(_ps, "PROXY_FNF_GUIDE_ENABLED", False))
    cand_flag = bool(getattr(_ps, "PROXY_FNF_CANDIDATES_ENABLED", False))
    sid = session_id or ""
    pkey = (sid, profile or "")
    suppress = _FNF_SUPPRESS.pop(pkey, False) if sid else False
    # L 级互斥（方案 v0.1 §5「L 级注入轮跳过 F1 避免双注入」）：上一轮已触发
    # 循环干预（_LOOP_SESSION_STATE.level>0）⇒ 本轮 fnf 只给基础提示，不叠加
    # 「第 N 次＋禁止再试」升级文案（阶梯注入已承载会话级升级语言）。
    if not suppress and sid:
        try:
            _lp = getattr(_ps, "_LOOP_SESSION_STATE", {}).get(sid) or {}
            suppress = int(_lp.get("level") or 0) > 0
        except Exception:
            suppress = False

    error_count = {"wasted": 0, "file_not_found": 0, "input_validation": 0,
                   "fnf_guided": 0, "fnf_cand_hits": 0, "fnf_cand_misses": 0}

    # tool_use 索引：id -> (name, input)
    tu_by_id = {}
    for msg in messages:
        c = msg.get("content", "")
        if msg.get("role") == "assistant" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tu_by_id[b.get("id", "")] = (b.get("name", ""), b.get("input"))

    def _old_fnf_hint():
        return ("[System: 文件不存在。请先用 Bash ls 或 find 命令确认项目结构，"
                "然后使用正确的文件路径。]")

    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            bc = str(block.get("content", ""))
            if "Wasted call" in bc:
                block["content"] = (
                    "[System: 该文件自上次读取后未发生变化，不要再使用 Read 工具反复读取。"
                    "先用 ctx_recall 工具查询（query=文件路径或关键词）取回此前内容；"
                    "ctx_recall 无结果再用 Bash cat 命令代替。]"
                )
                error_count["wasted"] += 1
            elif "File does not exist" in bc or "No such file" in bc:
                error_count["file_not_found"] += 1
                tid = block.get("tool_use_id", "")
                tname, tinput = tu_by_id.get(tid, ("", None))
                paths = _fnf_extract_paths(tname, tinput, bc)
                if not (guide and sid and paths):
                    block["content"] = _old_fnf_hint()
                    continue
                path = paths[0]
                n = _fnf_count(sid, path)
                if tname == "Bash":
                    _FNF_TUID_PATH[tid] = path
                    while len(_FNF_TUID_PATH) > _FNF_TUID_MAX:
                        _FNF_TUID_PATH.pop(next(iter(_FNF_TUID_PATH)))
                cand = ""
                if cand_flag:
                    cand = _fnf_candidates(sid, os.path.dirname(path), messages)
                    if cand:
                        error_count["fnf_cand_hits"] += 1
                    else:
                        error_count["fnf_cand_misses"] += 1
                if suppress or n < 3:
                    block["content"] = _fnf_hint(path, 1, cand)
                else:
                    block["content"] = _fnf_hint(path, n, cand)
                    error_count["fnf_guided"] += 1
            elif "InputValidationError" in bc or "invalid x" in bc.lower():
                block["content"] = (
                    "[System: 工具调用参数错误。请检查工具参数格式，"
                    "确保所有必填参数正确提供。]"
                )
                error_count["input_validation"] += 1
            else:
                # §1.1 清零口：同路径后续「成功」读取 → 计数清零（逃逸口）。
                # 已翻译/系统注入块以 "[System:" 开头，天然不误判为成功。
                if bc.startswith("[System:"):
                    continue
                tid = block.get("tool_use_id", "")
                tname, tinput = tu_by_id.get(tid, ("", None))
                if tname in ("Read", "Grep", "Glob") and isinstance(tinput, dict):
                    p = tinput.get("file_path") or tinput.get("path") or ""
                    if p:
                        _fnf_clear(sid, p.strip())
    return messages, error_count


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

def _translate_tool_result_errors(messages):
    """兼容入口：转发到 F1/F2 增强版（旗标缺省关 ⇒ 行为不变）。"""
    return _translate_tool_result_errors_ex(messages)

__all__ = [
    "_filter_tools",
    "_extract_keywords",
    "_inject_keyword_context",
    "_translate_tool_result_errors",
    "_translate_tool_result_errors_ex",
]
