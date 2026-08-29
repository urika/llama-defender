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
        "关键词查询，返回匹配单元的索引行（轮次/工具/句柄/规模）。当你发现自己"
        "缺少早前工作过的信息（改过哪些文件、当时的结论、读过的内容）时先查"
        "这里，不要直接重读文件。"
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


def format_recall_result(lines, query):
    """索引行 → 工具结果文本（挂载后作为 ctx_recall 的 tool_result 体）。"""
    if not lines:
        return "ctx_recall: 无匹配的已折叠单元（query=%s）。该信息可能从未被丢弃，或在本会话开始前。" % query
    out = ["ctx_recall: 命中 %d 条已折叠上下文索引（按 turn 定位，正文可再取）:" % len(lines)]
    for l in lines:
        handle = l.get("handle") or {}
        hval = handle.get("value", "") if isinstance(handle, dict) else ""
        out.append("- turn %s | %s %s | %s | %s chars | %s" % (
            l.get("turn"), l.get("tool") or l.get("kind"), hval[:80],
            l.get("reason"), l.get("size_chars"), l.get("anchor")))
    return "\n".join(out)


__all__ = [
    "TOOL_SCHEMA", "lookup", "fts_search", "format_recall_result",
    "line_text", "DEFAULT_LIMIT",
]
