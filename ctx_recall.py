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
        "（如 r:call_xxx），直接把锚点作为 query 可精确取回。"
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


def fts_search(session_key, query, kind=None, limit=DEFAULT_LIMIT):
    """L2 FTS5 trigram 全文检索；<3 字符或索引异常 → L1 降级。"""
    q = (query or "").strip()
    if not q:
        return []
    if len(q) < 3:
        return _memory_filter(session_key, q, kind, limit)
    # FTS5 MATCH 语法元字符转义：查询按字面短语处理（双引号包裹）
    phrase = '"' + q.replace('"', '""') + '"'
    conn = None
    try:
        conn = _ensure_fts(session_key)
        if conn is None:
            return _memory_filter(session_key, q, kind, limit)
        rows = conn.execute(
            "SELECT turn, anchor FROM manifest_fts WHERE manifest_fts MATCH ? "
            "ORDER BY bm25(manifest_fts) LIMIT ?",
            (phrase, max(limit * 3, limit))).fetchall()
        if not rows:
            return []
        by_anchor = {}
        for line in memory_stores.MANIFEST.lines(session_key):
            by_anchor[(line.get("turn"), line.get("anchor"))] = line
        out = []
        seen = set()
        for turn, anchor in rows:
            line = by_anchor.get((turn, anchor))
            if line is None or (kind and line.get("kind") != kind):
                continue
            key = (turn, anchor)
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
            if len(out) >= limit:
                break
        return out
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

def recover_full_content(session_key, anchor, turn, max_chars=4000):
    """从 manifest 索引行恢复完整被丢弃的内容。

    数据流: manifest(地址: anchor+turn) → archive(内容: payload) → 完整 tool_result。
    anchor "r:t1" → tool_use_id "t1"; "u:t1" → 搜索 tool_use 块的 input(不适合恢复全文)。
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
                            return text[:max_chars] if text else None
    except (FileNotFoundError, OSError):
        return None
    return None


def format_recall_result(lines, query, session_key=None):
    """索引行 → 工具结果文本（含配对 tool_result 行 + archive 全文恢复）。"""
    if not lines:
        return "ctx_recall: 无匹配的已折叠单元（query=%s）。该信息可能从未被丢弃，或在本会话开始前。" % query
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
        if session_key and l.get("kind") == "tool_result":
            full = recover_full_content(
                session_key, l.get("anchor"), l.get("turn"))
            if full:
                basic += "\n  [恢复内容 (%d chars)]:\n%s" % (len(full), full)
            elif l.get("head"):
                basic += "\n  [摘录]: %s" % l["head"]

        out.append(basic)
    return "\n".join(out)


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
            result = "ctx_recall: 缺少 query 参数。用法: {\"query\": \"文件路径/工具名/关键词\", \"kind\": \"file_edit|tool_use|tool_result|message\", \"limit\": 8}"
        else:
            try:
                kind = args.get("kind") or None
                if kind in ("", "any", "all"):
                    kind = None
                try:
                    limit = max(1, min(int(args.get("limit") or 8), 20))
                except (TypeError, ValueError):
                    limit = 8
                lines = lookup(session_key, query, kind=kind, limit=limit)
                result = format_recall_result(lines, query, session_key)
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
