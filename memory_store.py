#!/usr/bin/env python3
"""memory_store.py — R-CM-1 跨会话持久记忆（stdlib only）。

需求：docs/01-requirements-product/context-engineering-enhancement-requirements-20261008.md
（R-CM-1）。给代理一个**项目级**持久记忆库，跨会话/跨进程留存
「事实 / 决策 / 回归 / 偏好」，供后续会话在首请求头部/尾部注入。

设计要点（对齐 ai-memory / claude-mem / basic-memory 的朴素版）：
  - 存储：stdlib `sqlite3`（FTS5 可用则建全文索引，否则 LIKE 回退）。
    磁盘 logs/memory/memory.db；人可读导出 markdown。
  - 分域：project（默认 "default"，可用请求头 X-Proxy-Project 覆盖）。
  - 生命周期：每条带 kind/status/confidence/source，支持遗忘（硬删）。
  - 全链路 fail-open：任何异常吞掉（记忆故障不得影响请求路径）。
  - 零新增依赖：不 import 第三方。

写入方：/admin/memory 端点（显式）、后续会话蒸馏钩子。
读取方：pipeline.MemoryInjectStage（首请求注入一次）。
"""
import os
import re
import sqlite3
import threading
from datetime import datetime

import proxy_state as _ps

# 记忆类型（对齐 claude-mem / repomemory 的分类：事实/决策/回归/偏好）
MEMORY_KINDS = ("fact", "decision", "regression", "preference", "note")
DEFAULT_PROJECT = "default"
MAX_TEXT_CHARS = 4000          # 单条上限（防大文本失控）
MAX_ROWS_PER_PROJECT = 5000    # 每项目软上限（超出丢最老）
EXPORT_MAX_ROWS = 500


def _script_dir():
    return getattr(_ps, "_SCRIPT_DIR", os.getcwd())


def _db_path():
    raw = getattr(_ps, "PROXY_MEMORY_DB_PATH", "logs/memory/memory.db") or "logs/memory/memory.db"
    return raw if os.path.isabs(raw) else os.path.join(_script_dir(), raw)


def enabled():
    """总开关（默认关）。fail-open：属性缺失视为关。"""
    try:
        return bool(getattr(_ps, "PROXY_MEMORY_ENABLED", False))
    except Exception:
        return False


def normalize_project(project):
    p = (project or "").strip() or DEFAULT_PROJECT
    # 只留安全字符，防路径/注入问题
    p = re.sub(r"[^A-Za-z0-9._:-]", "_", p)[:128]
    return p or DEFAULT_PROJECT


class MemoryStore(object):
    """跨会话记忆库（惰性建库；每调用独立连接，线程安全）。"""

    def __init__(self):
        # RLock: _ensure_schema 在 _connect 内会再次取锁（add/search 已持锁）
        self._lock = threading.RLock()
        self._init_done = False
        self._fts = False

    # ------------------------------------------------------------ schema --
    def _connect(self):
        path = _db_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        conn = sqlite3.connect(path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        if not self._init_done:
            self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn):
        with self._lock:
            if self._init_done:
                return
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    session_key TEXT DEFAULT '',
                    turn INTEGER DEFAULT 0,
                    confidence REAL DEFAULT 0.7,
                    status TEXT DEFAULT 'active',
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_project ON memory(project, status);
                CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind);
                """
            )
            try:
                conn.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                        USING fts5(text, content='memory', content_rowid='id');
                    CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
                      INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
                      INSERT INTO memory_fts(memory_fts, rowid, text) VALUES('delete', old.id, old.text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
                      INSERT INTO memory_fts(memory_fts, rowid, text) VALUES('delete', old.id, old.text);
                      INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
                    END;
                    """
                )
                self._fts = True
            except sqlite3.DatabaseError:
                self._fts = False  # FTS5 不可用 → LIKE 回退
            conn.commit()
            self._init_done = True

    # ------------------------------------------------------------- write --
    def add(self, text, kind="note", project=DEFAULT_PROJECT, source="",
            session_key="", turn=0, confidence=0.7):
        """写入一条记忆；返回 id 或 None（fail-open）。"""
        try:
            text = (text or "").strip()
            if not text:
                return None
            text = text[:MAX_TEXT_CHARS]
            kind = kind if kind in MEMORY_KINDS else "note"
            project = normalize_project(project)
            now = datetime.now().isoformat(timespec="seconds")
            with self._lock:
                conn = self._connect()
                try:
                    cur = conn.execute(
                        "INSERT INTO memory (project, kind, text, source, "
                        "session_key, turn, confidence, status, created, updated) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (project, kind, text, source or "", session_key or "",
                         int(turn or 0), float(confidence), "active", now, now),
                    )
                    new_id = cur.lastrowid
                    self._enforce_cap(conn, project)
                    conn.commit()
                    return new_id
                finally:
                    conn.close()
        except Exception:
            return None

    def forget(self, mem_id):
        """硬删一条；返回是否删除。"""
        try:
            with self._lock:
                conn = self._connect()
                try:
                    cur = conn.execute("DELETE FROM memory WHERE id=?", (int(mem_id),))
                    conn.commit()
                    return cur.rowcount > 0
                finally:
                    conn.close()
        except Exception:
            return False

    def _enforce_cap(self, conn, project):
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE project=?", (project,)
            ).fetchone()[0]
            if n > MAX_ROWS_PER_PROJECT:
                conn.execute(
                    "DELETE FROM memory WHERE id IN ("
                    "SELECT id FROM memory WHERE project=? ORDER BY id ASC LIMIT ?)",
                    (project, n - MAX_ROWS_PER_PROJECT),
                )
        except Exception:
            pass

    # -------------------------------------------------------------- read --
    def search(self, query, project=DEFAULT_PROJECT, top_k=5, kinds=None):
        """FTS5/ LIKE 检索，返回 [{id,kind,text,source,created,score}]。fail-open。"""
        try:
            project = normalize_project(project)
            top_k = max(1, min(int(top_k or 5), 20))
            klist = [k for k in (kinds or []) if k in MEMORY_KINDS]
            with self._lock:
                conn = self._connect()
                try:
                    rows = self._search_conn(conn, query, project, top_k, klist)
                finally:
                    conn.close()
            return rows
        except Exception:
            return []

    def _search_conn(self, conn, query, project, top_k, klist):
        q = (query or "").strip()
        kind_clause = ""
        params = []
        if klist:
            kind_clause = " AND kind IN (%s)" % ",".join("?" * len(klist))
            params.extend(klist)
        if q and self._fts:
            try:
                # FTS5 查询：token 加 * 前缀匹配，**OR 连接**（空格=隐式 AND，
                # 会话首条 user 消息几百 token 时 AND 永不满足 → 恒 0 行，
                # 2026-10-10 生产确诊）。去重 + 上限 12：控制查询串长度，
                # BM25 对多命中行仍按词频-逆文档频率排序。
                toks = []
                for t in q.split():
                    safe_t = re.sub(r"[^A-Za-z0-9_]", "", t) + "*"
                    if safe_t != "*" and safe_t not in toks:
                        toks.append(safe_t)
                if len(toks) > 24:
                    # 长度降序优先（长 token=更特异；稳定排序保留查询序），
                    # 避免信号词排在 200 字符窗口尾部时被截断丢弃。
                    # 24 覆盖实测真实首条消息的 18 token 且 MATCH 串有界。
                    toks = sorted(toks, key=len, reverse=True)[:24]
                safe = " OR ".join(toks)
                if safe:
                    sql = (
                        "SELECT m.id, m.kind, m.text, m.source, m.created, "
                        "bm25(memory_fts) AS score FROM memory_fts "
                        "JOIN memory m ON m.id = memory_fts.rowid "
                        "WHERE memory_fts MATCH ? AND m.project=? AND m.status='active'"
                        + kind_clause +
                        " ORDER BY score LIMIT ?"
                    )
                    rows = conn.execute(
                        sql, [safe, project] + params + [top_k]
                    ).fetchall()
                    if rows:
                        return [dict(r) for r in rows]
            except sqlite3.DatabaseError:
                pass  # 语法/索引异常 → LIKE 回退
        # LIKE 回退（含无 query 时取最近）
        if q:
            sql = (
                "SELECT id, kind, text, source, created, 0 AS score FROM memory "
                "WHERE project=? AND status='active' AND text LIKE ?"
                + kind_clause +
                " ORDER BY id DESC LIMIT ?"
            )
            rows = conn.execute(
                sql, [project, "%" + q + "%"] + params + [top_k]
            ).fetchall()
        else:
            sql = (
                "SELECT id, kind, text, source, created, 0 AS score FROM memory "
                "WHERE project=? AND status='active'"
                + kind_clause +
                " ORDER BY id DESC LIMIT ?"
            )
            rows = conn.execute(sql, [project] + params + [top_k]).fetchall()
        return [dict(r) for r in rows]

    def list(self, project=DEFAULT_PROJECT, limit=100):
        try:
            project = normalize_project(project)
            limit = max(1, min(int(limit or 100), EXPORT_MAX_ROWS))
            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT id, kind, text, source, created, updated, "
                        "confidence, status FROM memory WHERE project=? "
                        "ORDER BY id DESC LIMIT ?",
                        (project, limit),
                    ).fetchall()
                finally:
                    conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def stats(self):
        try:
            with self._lock:
                conn = self._connect()
                try:
                    total = conn.execute(
                        "SELECT COUNT(*) FROM memory").fetchone()[0]
                    by_kind = dict(conn.execute(
                        "SELECT kind, COUNT(*) FROM memory GROUP BY kind").fetchall())
                    projs = dict(conn.execute(
                        "SELECT project, COUNT(*) FROM memory GROUP BY project").fetchall())
                finally:
                    conn.close()
            return {"total": total, "by_kind": by_kind, "by_project": projs,
                    "fts": self._fts, "db": _db_path()}
        except Exception:
            return {"total": 0, "by_kind": {}, "by_project": {}, "fts": False,
                    "db": _db_path()}

    # ------------------------------------------------------------ export --
    def export_markdown(self, project=DEFAULT_PROJECT, limit=EXPORT_MAX_ROWS):
        """人可读导出（basic-memory 式 disk-source-of-truth）。fail-open。"""
        try:
            rows = self.list(project=project, limit=limit)
            lines = ["# Memory: %s" % normalize_project(project), ""]
            for r in rows:
                lines.append("- **[%s]** %s  _(%s)_" % (
                    r.get("kind"), (r.get("text") or "").strip(),
                    r.get("created", "")))
            return "\n".join(lines) + "\n"
        except Exception:
            return "# Memory\n"

    def reset(self):
        """测试辅助：清空内存态标志（不动磁盘）。"""
        with self._lock:
            self._init_done = False
            self._fts = False


MEMORY = MemoryStore()


__all__ = ["MemoryStore", "MEMORY", "MEMORY_KINDS", "DEFAULT_PROJECT",
           "MAX_TEXT_CHARS", "normalize_project", "enabled"]
