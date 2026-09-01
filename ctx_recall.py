#!/usr/bin/env python3
"""ctx_recall.py — PDC 渐进披露拉取服务核心（stdlib only）。

R10.2（PRD v3.1 / PDC 设计 §2.2）：把「被折叠/压缩丢弃的上下文」从不可恢复
变为可查询。v0 范围 = 检索核心 + 工具 schema；**管线挂载（tool_filter 注入与
次请求 tool_result 改写）等待 pipeline.py 并行工作合并后一行接入**——本模块
不得 import pipeline（薄封装规则 + 循环规避）。

检索分层：
  L1  内存过滤: MANIFEST 行的 handle.value / tool / anchor 子串匹配（快，短查询友好）
  L2  FTS5 全文: logs/diag/index/<sid>.db，trigram 分词（CJK 3 字滑窗，子串可查）；
      索引行文本 = "tool handle 行内摘要"；正文按 turn/anchor 回档案定位（v0 返回
      索引行即「可寻址性」，正文回填随挂载一起交付）
  中文 2 字短查询走 L1 子串兜底（trigram 最小 3 字符，PDC 设计 §3.4 预案）。

FTS5/WAL 本机已验证（SQLite 3.51.0，2026-08-29，存储选型文档 §3.2）。
连接纪律：每次查询新建连接（召回低频，避免跨线程复用）；索引库为只读派生
——行数与 MANIFEST 不一致时自动重建。
"""
import json
import os
import sqlite3
import threading

import proxy_state as _ps

import memory_stores
from session_ledger import sanitize_session_key

# ctx_recall 工具定义（Anthropic tools 格式；OpenAI 端点由 message_converter
# 现有转换覆盖）。挂载点：tool_filter 注入钩子（待接入）。
TOOL_SCHEMA = {
    "name": "ctx_recall",
    "description": (
        "检索本会话早前被代理折叠/截断/压缩的上下文单元。按文件路径、工具名或"
        "关键词查询，返回匹配单元的索引行（轮次/工具/句柄/规模），tool_result "
        "类单元附带完整原文。会话约定：被折叠的内容一直保留在本会话存储中，"
        "随时可查——当你发现自己缺少早前工作过的信息（改过哪些文件、当时的结论、"
        "读过的内容）时先查这里，不要直接重读文件；若折叠提示中给出了锚点"
        "（如 r:call_xxx），直接把锚点作为 query 可精确取回；大文件原文分页披露——续读用 锚点@偏移（如 r:call_x@4000）。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "关键词/文件路径/工具名（≥3 字符走全文索引，短词自动降级子串匹配）",
            },
            "kind": {
                "type": "string",
                "enum": ["tool_use", "tool_result", "text"],
                "description": "可选：只查某类单元",
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 20,
                "description": "返回行数上限（默认 8）",
            },
        },
        "required": ["query"],
    },
}

DEFAULT_LIMIT = 8
_FTS_BUILD_LOCK = threading.Lock()
# 锚点直查模式: "r:tool_use_id" / "u:tool_use_id"(tool description 承诺语义)
import re as _re
_ANCHOR_RE = _re.compile(r'^[ru]:[A-Za-z0-9_\-]+$')
# 分页续读(借鉴 skill 协议按任务粒度披露): "r:xxx@4000" = 从 4000 字节续读
_ANCHOR_OFFSET_RE = _re.compile(r'^([ru]:[A-Za-z0-9_\-]+)@(\d{1,9})$')
# PDC §5 护栏: 单次 pull 结果总体积上限(路径 A 改写此前无上限,
# limit=8 × 恢复 4K/条 最高可回填 ~32K chars; 微轮路径本有 2000 截断)
RESULT_TOTAL_MAX_CHARS = 4000
# 分页恢复页大小(借鉴 skill 按任务粒度披露; 单页内语义尽量完整)
RECOVERY_PAGE_CHARS = 4000


# ============================================================================
# 索引行文本化（FTS 可检索体）
# ============================================================================

def line_text(line):
    """索引行 → 可检索文本（工具名 + 句柄值 + 头部摘录 + 类别；确定性无 LLM）。"""
    handle = line.get("handle") or {}
    hval = handle.get("value", "") if isinstance(handle, dict) else ""
    return " ".join(x for x in [
        line.get("tool") or "",
        hval,
        line.get("head") or "",
        line.get("kind") or "",
        line.get("reason") or "",
        line.get("triggers") or "",  # §3.1: 指称性实体入索引词表
    ] if x)


def _index_dir():
    return os.path.join(_ps._DIAG_DIR, "index")


def _db_path(session_key):
    return os.path.join(_index_dir(), sanitize_session_key(session_key) + ".db")


def _ensure_fts(session_key):
    """确保 FTS 索引与 MANIFEST 同步（行数不一致 → 重建）。返回连接或 None。"""
    lines = memory_stores.MANIFEST.lines(session_key)
    path = _db_path(session_key)
    os.makedirs(_index_dir(), exist_ok=True)
    with _FTS_BUILD_LOCK:
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS manifest_fts USING fts5("
                "text, turn UNINDEXED, anchor UNINDEXED, tokenize='trigram')")
            n = conn.execute("SELECT COUNT(*) FROM manifest_fts").fetchone()[0]
            if n != len(lines):
                conn.execute("DELETE FROM manifest_fts")
                conn.executemany(
                    "INSERT INTO manifest_fts(text, turn, anchor) VALUES (?,?,?)",
                    [(line_text(l), l.get("turn"), l.get("anchor")) for l in lines])
            conn.commit()
            return conn
        except sqlite3.Error:
            conn.close()
            return None


# ============================================================================
# 查询入口
# ============================================================================

def _memory_filter(session_key, query, kind=None, limit=DEFAULT_LIMIT):
    """L1 内存子串过滤（短查询兜底 + FTS 不可用降级）。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    for line in memory_stores.MANIFEST.lines(session_key):
        if kind and line.get("kind") != kind:
            continue
        if q in line_text(line).lower():
            out.append(line)
            if len(out) >= limit:
                break
    return out


def _query_tokens(query, max_tokens=8):
    """自然语言查询 → 可检索 token 列表(≥3 字符, 截断防炸)。

    模型实际发的查询是描述性长句(实测 "openlibrary/core/lists/model.py
    Seed class List"), 整句短语匹配永不命中。拆成 token 后按"命中 token
    数"聚合排序, 才能命中包含部分词汇的索引行。
    """
    import re
    toks = []
    seen = set()
    for t in re.split(r'[\s/\\,;:()\[\]{}\'"_.\-]+', query or ""):
        t = t.strip().lower()
        if len(t) >= 3 and t not in seen:
            seen.add(t)
            toks.append(t)
        if len(toks) >= max_tokens:
            break
    return toks



def parse_query_offset(query):
    """解析 "r:xxx@4000" 分页语法 → (base_query, offset)。

    借鉴 skill 协议按任务粒度披露: 大单元不再 4000 字节硬截断(语义
    中间砍断且截掉部分永久丢失), 而是分页续读——截断提示给出下一页
    的精确 query。非分页语法返回 (原查询, 0)。
    """
    q = (query or "").strip()
    m = _ANCHOR_OFFSET_RE.match(q)
    if m:
        return m.group(1), int(m.group(2))
    return q, 0


def fts_search(session_key, query, kind=None, limit=DEFAULT_LIMIT):
    """L2 FTS5 trigram 全文检索；<3 字符或索引异常 → L1 降级。

    检索三级穿透(2026-09-01, 修复自然语言查询零命中 + 自引用洪泛):
      1. 整句短语精确匹配(模型复述路径/锚点原文时精度最高)
      2. 短语无 primary → token 分查聚合, SQL 层仅取 r: 锚点
         (tool_result, 有原文可恢复), 按锚点去重
      3. 无 r: 命中 → token 全量(含 u: 索引行, 仅可寻址性)

    生产依据: 模型 30+ 次重复查询的同锚点自引用行在 bm25 top-N 层洪泛,
    r: 偏好不下推到 SQL 则内容行永远进不了窗口; 且无匹配消息会原样
    记录查询文本, 短语命中可能全部是自查询行——故以 primary 为准
    逐级穿透, 而非"行数非零即接受"。
    """
    q = (query or "").strip()
    if not q:
        return []
    # 锚点直查(tool description 承诺"锚点可精确取回"; 2026-09-01 review
    # 发现为虚假承诺——FTS 只索引 text 列不含 anchor, 此前必然 miss)
    # 含分页: "r:xxx@4000" → base "r:xxx"(offset 由 format 层消费)
    if _ANCHOR_RE.match(q) or _ANCHOR_OFFSET_RE.match(q):
        q = _ANCHOR_OFFSET_RE.match(q).group(1) if _ANCHOR_OFFSET_RE.match(q) else q
        kind_map = {"r": "tool_result", "u": "tool_use"}
        want_kind = kind or kind_map.get(q[0])
        for line in memory_stores.MANIFEST.lines(session_key):
            if line.get("anchor") == q and (not want_kind
                                            or line.get("kind") == want_kind):
                return [line]
        return []
    if len(q) < 3:
        return _memory_filter(session_key, q, kind, limit)
    phrase = '"' + q.replace('"', '""') + '"'
    conn = None
    try:
        conn = _ensure_fts(session_key)
        if conn is None:
            return _memory_filter(session_key, q, kind, limit)

        def _collect(match_text, want, anchor_prefix=None):
            sql = ("SELECT turn, anchor FROM manifest_fts "
                   "WHERE manifest_fts MATCH ? ")
            if anchor_prefix:
                sql += f"AND anchor LIKE '{anchor_prefix}%' "
            sql += f"ORDER BY bm25(manifest_fts) LIMIT {want}"
            return conn.execute(sql, (match_text,)).fetchall()

        by_anchor = {}
        for line in memory_stores.MANIFEST.lines(session_key):
            by_anchor[(line.get("turn"), line.get("anchor"))] = line

        def _classify(rows):
            """行分类: primary=可恢复内容, secondary=仅可寻址(自查询等)。"""
            primary, secondary = [], []
            seen = set()
            for turn, anchor in rows:
                line = by_anchor.get((turn, anchor))
                if line is None or (kind and line.get("kind") != kind):
                    continue
                if anchor in seen:  # 同锚点多轮副本 → 只留一份
                    continue
                seen.add(anchor)
                if (line.get("tool") == "ctx_recall"
                        and line.get("kind") == "tool_use"):
                    secondary.append(line)  # 自引用: 查询记录非内容
                    continue
                (primary if line.get("kind") == "tool_result"
                 else secondary).append(line)
            return primary, secondary

        # 策略 1: 整句短语精确匹配
        primary, secondary = _classify(_collect(
            phrase, max(limit * 3, limit)))
        if primary:
            return (primary + secondary)[:limit]

        toks = _query_tokens(q)

        # 策略 2: token 分查, SQL 层仅取 r: 内容行, 按锚点去重聚合
        token_rows = {}
        for i, t in enumerate(toks):
            tp = '"' + t.replace('"', '""') + '"'
            for turn, anchor in _collect(tp, limit * 10, "r:"):
                cnt, fi, bturn = token_rows.get(anchor, (0, 999, -1))
                token_rows[anchor] = (cnt + 1, min(fi, i), max(bturn, turn))
        if token_rows:
            ranked = sorted(token_rows.items(),
                            key=lambda kv: (-kv[1][0], kv[1][1]))
            p2, s2 = _classify([(bturn, anchor) for anchor,
                                (_c, _i, bturn) in ranked[:limit]])
            if p2:
                return (p2 + secondary)[:limit]
            secondary = (s2 or []) + secondary

        # 策略 3: 回落全量(含 u: 索引行, 仅可寻址性)
        token_rows = {}
        for i, t in enumerate(toks):
            tp = '"' + t.replace('"', '""') + '"'
            for turn, anchor in _collect(tp, limit * 3):
                key = (turn, anchor)
                cnt, fi = token_rows.get(key, (0, 999))
                token_rows[key] = (cnt + 1, min(fi, i))
        if not token_rows:
            return secondary[:limit]
        ranked = sorted(token_rows.items(),
                        key=lambda kv: (-kv[1][0], kv[1][1]))
        p3, s3 = _classify([(turn, anchor) for (turn, anchor), _ in
                            ranked[:max(limit * 3, limit)]])
        return (p3 + s3 + secondary)[:limit]
    except sqlite3.Error:
        return _memory_filter(session_key, q, kind, limit)
    finally:
        if conn is not None:
            conn.close()


def lookup(session_key, query, kind=None, limit=DEFAULT_LIMIT):
    """检索入口（挂载后由召回服务调用）：返回匹配索引行列表。"""
    if not getattr(_ps, "PROXY_PD_ENABLED", True) or not session_key:
        return []
    return fts_search(session_key, query, kind, limit)


# ============================================================================
# Archive 全文恢复(MVP 增强): manifest 索引行 → archive 完整 tool_result
# ============================================================================

def recover_full_content(session_key, anchor, turn, max_chars=4000, offset=0):
    """从 manifest 索引行恢复完整被丢弃的内容(支持分页续读)。

    数据流: manifest(地址: anchor+turn) → archive(内容: payload) → 完整 tool_result。
    anchor "r:t1" → tool_use_id "t1"; "u:t1" → 搜索 tool_use 块的 input(不适合恢复全文)。
    回退链尾部: orig/<sid>.jsonl(压缩时寄存的原文)——压缩标记 key=r:x 的兑现。
    offset>0 时返回原文的 [offset : offset+max_chars] 窗口(分页续读协议)。
    返回 str 或 None(未找到/archive 不存在)。
    """
    if not session_key or not anchor:
        return None
    tool_use_id = anchor[2:] if anchor.startswith("r:") else None
    if not tool_use_id:
        return None  # tool_use 锚不含 result 正文

    archive_path = os.path.join(_ps._DIAG_DIR, "archive",
                                session_key + ".jsonl")
    try:
        with open(archive_path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("turn") != turn:
                    continue
                # payload 是 JSON 字符串(R15 落盘格式)
                payload = entry.get("payload")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, ValueError):
                        continue
                if not isinstance(payload, dict):
                    continue
                for msg in payload.get("messages", []):
                    if msg.get("role") != "user":
                        continue
                    for block in (msg.get("content") or []):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result" and \
                           block.get("tool_use_id") == tool_use_id:
                            content = block.get("content")
                            if isinstance(content, list):
                                text = "\n".join(
                                    b.get("text", "") for b in content
                                    if isinstance(b, dict))
                            elif isinstance(content, str):
                                text = content
                            else:
                                text = ""
                            if text:
                                window = (text[offset:offset + max_chars]
                                          if max_chars else text[offset:])
                                return window or None
    except (FileNotFoundError, OSError):
        pass
    # 回退链尾部: 压缩时寄存的原文(标记 key=r:x 的兑现)
    try:
        import memory_stores
        full = memory_stores.read_orig_content(session_key, anchor)
        if full:
            window = (full[offset:offset + max_chars]
                      if max_chars else full[offset:])
            return window or None
    except Exception:
        return None
    return None


def format_recall_result(lines, query, session_key=None, offset=0):
    """索引行 → 工具结果文本（含配对 tool_result 行 + archive 全文恢复）。"""
    if not lines:
        # S1(2026-09-01): 空结果附存储概况——模型需要区分"换词重查有意义"
        # 还是"存储里根本没有"; 空结果是最需要给线索的时刻(压召回 churn)。
        hint = ""
        if session_key:
            try:
                all_lines = memory_stores.MANIFEST.lines(session_key)
                if all_lines:
                    from collections import Counter
                    hv = Counter()
                    for l in all_lines:
                        h = l.get("handle") or {}
                        v = h.get("value") if isinstance(h, dict) else ""
                        if v:
                            hv[v[:60]] += 1
                    top = ", ".join(f"{k}({n})" for k, n in hv.most_common(3))
                    hint = (f" 当前存储共 {len(all_lines)} 条折叠单元"
                            + (f"，高频句柄: {top}" if top else "")
                            + "。可改用上述路径/关键词重查。")
            except Exception:
                pass
        return ("ctx_recall: 无匹配的已折叠单元（query=%s）。该信息可能从未被丢弃，"
                "或在本会话开始前。%s" % (query, hint))
    out = ["ctx_recall: 命中 %d 条已折叠上下文索引:" % len(lines)]

    # 收集需要补充的配对 tool_result 行(tool_use 命中时拉取对应的 result)
    paired_lines = []
    if session_key:
        seen_anchors = {l.get("anchor") for l in lines}
        for line in lines:
            if line.get("kind") != "tool_use":
                continue
            # tool_use anchor "u:t1" → tool_result anchor "r:t1"
            result_anchor = "r:" + line.get("anchor", "")[2:]
            if result_anchor in seen_anchors:
                continue  # 已在结果中
            for candidate in memory_stores.MANIFEST.lines(session_key):
                if candidate.get("anchor") == result_anchor:
                    paired_lines.append(candidate)
                    break

    all_lines = list(lines) + paired_lines
    if paired_lines:
        out[0] = f"ctx_recall: 命中 {len(lines)} 条(含 {len(paired_lines)} 条配对 tool_result):"

    for l in all_lines:
        handle = l.get("handle") or {}
        hval = handle.get("value", "") if isinstance(handle, dict) else ""
        basic = "- turn %s | %s %s | %s | %s chars | %s" % (
            l.get("turn"), l.get("tool") or l.get("kind"), hval[:80],
            l.get("reason"), l.get("size_chars"), l.get("anchor"))

        # Archive 全文恢复: tool_result 单元尝试恢复完整内容
        # 分页披露(借鉴 skill 协议): 单条 >RECOVERY_PAGE_CHARS 时按页给,
        # 页尾附续读指令(锚点@下一offset), 不再从语义中间永久砍断
        if session_key and l.get("kind") == "tool_result":
            anchor = l.get("anchor") or ""
            page = RECOVERY_PAGE_CHARS
            full = recover_full_content(session_key, anchor, l.get("turn"),
                                        max_chars=None)
            if full:
                total = len(full)
                use_offset = offset if len(lines) == 1 else 0
                chunk = full[use_offset:use_offset + page] or full[:page]
                end = use_offset + len(chunk)
                if total <= page and use_offset == 0:
                    basic += "\n  [恢复内容 (%d chars)]:\n%s" % (total, full)
                else:
                    cont = ("" if end >= total else
                            f"; 续读: query=\"{anchor}@{end}\"")
                    basic += (f"\n  [恢复内容 第{end - len(chunk)}-{end}/"
                              f"{total} chars{cont}]:\n{chunk}")
            elif l.get("head"):
                basic += "\n  [摘录]: %s" % l["head"]

        out.append(basic)
    result = "\n".join(out)
    if len(result) > RESULT_TOTAL_MAX_CHARS:
        # S2(2026-09-01): 截断带计数——模型需知道还有多少条没看到
        total = len(out) - 1  # out[0] 是标题行
        shown = result[:RESULT_TOTAL_MAX_CHARS].count("- turn ")
        result = (result[:RESULT_TOTAL_MAX_CHARS]
                  + f"\n…(已截断: 仅显示 {shown}/{total} 条; "
                    "可用更具体的 query 或减小 limit 重查)")
    return result


# ============================================================================
# IFC-3 微轮自答(方案 B, 2026-08-30): 同请求内构造 follow-up 消息对
# ============================================================================

MICRO_TURN_RESULT_MAX_CHARS = 2000  # PDC 设计 §5 护栏: pull 结果体积上限


def build_follow_up_messages(session_key, tool_calls):
    """把模型流出的 ctx_recall 调用转为 OpenAI 格式微轮消息对。

    输入 tool_calls: [{"id":..., "function": {"name":..., "arguments": json-str}}]
    （OpenAI chat completions 流式累计后的结构）。
    返回 [assistant(tool_calls), tool(result), ...]；全部调用均为 ctx_recall 且
    参数可解析时才返回，否则 None（调用方走路径 A 客户端回环）。
    结果截断至 MICRO_TURN_RESULT_MAX_CHARS（护栏，防拉取洪泛回填上下文）。
    """
    if not session_key or not tool_calls:
        return None
    assistant_tc = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        if fn.get("name") != "ctx_recall":
            return None  # 混有其他工具 → 无法全部自答
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
            if not isinstance(args, dict):
                raise ValueError("arguments not an object")
        except (json.JSONDecodeError, ValueError):
            return None
        assistant_tc.append({
            "id": tc.get("id") or "call_%s" % os.urandom(8).hex(),
            "type": "function",
            "function": {"name": "ctx_recall",
                         "arguments": json.dumps(args, ensure_ascii=False)},
        })

    follow_up = [{"role": "assistant", "content": "", "tool_calls": assistant_tc}]
    for tc in assistant_tc:
        args = json.loads(tc["function"]["arguments"])
        query = str(args.get("query", "")).strip()
        if not query:
            result = "ctx_recall: 缺少 query 参数。用法: {\"query\": \"文件路径/工具名/关键词/锚点(可加@偏移分页)\", \"kind\": \"tool_use|tool_result|text\", \"limit\": 8}"
        else:
            try:
                kind = args.get("kind") or None
                if kind in ("", "any", "all"):
                    kind = None
                try:
                    limit = max(1, min(int(args.get("limit") or 8), 20))
                except (TypeError, ValueError):
                    limit = 8
                base_q, paged_off = parse_query_offset(query)
                lines = lookup(session_key, base_q, kind=kind, limit=limit)
                result = format_recall_result(lines, base_q, session_key,
                                              offset=paged_off)
            except Exception as e:  # 检索失败 → 结果性错误文本(非 fail-open:
                # 模型需要知道拉取失败, 与路径 A 的 error result 语义一致)
                result = "ctx_recall: 检索失败(%s)。" % e
        follow_up.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": result[:MICRO_TURN_RESULT_MAX_CHARS],
        })
    return follow_up


__all__ = [
    "TOOL_SCHEMA", "lookup", "fts_search", "format_recall_result",
    "line_text", "recover_full_content", "DEFAULT_LIMIT",
    "build_follow_up_messages", "MICRO_TURN_RESULT_MAX_CHARS",
]
