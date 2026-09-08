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
import ast
import json
import os
import re
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
        "（如 r:call_xxx），直接把锚点作为 query 可精确取回；代码文件支持符号寻址："
        "query=锚点#sym:函数名 直接取该函数源码（如 r:call_x#sym:_load_extras），"
        "query=路径::函数名（如 plugins/psrp.py::load_extras）可定位符号；"
        "大文件原文分页披露——续读用 锚点@偏移（如 r:call_x@4000）。"
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
# 折叠召回线索 RECALL_CUE（2026-09-03 folded-recall-cue 设计）
# 唯一源头：两条折叠路径（truncation fifo/DEF-107 = A 路、
# context_engine epoch 面板 = B 路）的召回指引句均引用此处，
# 字节级一致（跨路径/跨会话利于 prefix-cache）。fail-open：
# 引用方 deferred import，失败用 inline 字面量兜底。
# ============================================================================
RECALL_CUE = (
    "Full text of folded content is preserved in this session store — "
    "use ctx_recall to recover instead of re-reading files, "
    "e.g. ctx_recall(query='<file-path-or-keyword>') or "
    "ctx_recall(query='<anchor>'). "
    "Recall first; re-read only if ctx_recall returns nothing. "
    "When asked for a specific value (token/key/number/config) that is not "
    "in your current context: you MUST recall it via ctx_recall first; if "
    "recall finds nothing, say explicitly that the information is no longer "
    "available — NEVER guess or invent a value. (IFC-11 2026-09-07)"
)

# 面板附带查询键上限（精确 anchor 优先；多了费 token 且无收益）
FOLDED_KEYS_LIMIT = 6


def recall_keys_line(anchors, limit=FOLDED_KEYS_LIMIT):
    """查询键行纯函数：去重保序截断；空输入返回 ""（调用方自行决定是否拼装）。"""
    seen = []
    for a in anchors or []:
        if isinstance(a, str) and a and a not in seen:
            seen.append(a)
        if len(seen) >= limit:
            break
    if not seen:
        return ""
    return "Folded keys: %s." % ", ".join(seen)


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


def _manifest_search(session_key, queries, kind=None, limit=DEFAULT_LIMIT):
    """R18: task-context 轻量检索路径（避免 FTS 建索引开销）。

    只读 MANIFEST 行一次, 在 head/handle/tool/anchor 上做子串匹配。
    无命中 → []；存储故障 → []（fail-open）。
    """
    if not session_key:
        return []
    qset = [q.strip().lower() for q in (queries or []) if q and q.strip()]
    if not qset:
        return []
    out, seen = [], set()
    try:
        for line in memory_stores.MANIFEST.lines(session_key):
            if kind and line.get("kind") != kind:
                continue
            anchor = line.get("anchor") or ""
            if anchor in seen:
                continue
            haystack = line_text(line).lower()
            for q in qset:
                if q in haystack or q in anchor.lower():
                    seen.add(anchor)
                    out.append(line)
                    break
            if len(out) >= limit:
                break
    except Exception:
        return []
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
    # 锚点 #sym: 后缀容忍(TC29 治理: 模型可写 r:xxx#sym:func 直取符号)
    if "#sym:" in q:
        q = q.split("#sym:", 1)[0]
    if "::" in q and not _ANCHOR_RE.match(q.split("::")[0]):
        # path::symbol 符号定位: 持久索引在场时返回带源码获取提示的注记行
        path_part, _, sym = q.rpartition("::")
        path_part, sym = path_part.strip(), sym.strip()
        if path_part and sym:
            for line in memory_stores.MANIFEST.lines(session_key):
                hv = (line.get("handle") or {}).get("value", "") \
                    if isinstance(line.get("handle"), dict) else ""
                base_anchor = line.get("anchor", "")
                if (hv and hv.endswith(path_part) and line.get("kind") == "tool_use"
                        and base_anchor.startswith("u:")):
                    r_anchor = "r:" + base_anchor[2:]
                    located = None
                    idx = chunk_index_load(session_key, r_anchor)
                    for sym_row in (idx or {}).get("symbols") or []:
                        if sym_row.get("name", "").endswith(sym):
                            located = "[symbol %s: %s | L%d-L%d]" % (
                                sym_row.get("name"), str(sym_row.get("sig", ""))[:60],
                                sym_row.get("line_start"), sym_row.get("line_end"))
                            break
                    if not located:
                        # 方法级符号不在 top-level 索引——回收内容行扫描定位
                        try:
                            _content = _recover_full_content_raw(
                                session_key, r_anchor, None, 0, 0)
                            if _content:
                                slines = re.sub(r"(?m)^\s*\d+\t", "",
                                                _content).splitlines()
                                _pat = re.compile(
                                    r"^\s*def\s+" + re.escape(sym) + r"\s*\(")
                                for i, sl in enumerate(slines):
                                    if _pat.match(sl):
                                        located = "[symbol %s | %s]" % (
                                            sym, sl.strip()[:70])
                                        break
                        except Exception:
                            pass
                    if located:
                        annotated = dict(line)
                        annotated["head"] = "%s — 源码: ctx_recall query=\"%s#sym:%s\"" % (
                            located, r_anchor, sym)
                        return [annotated]
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

# ============================================================================
# 符号块索引 sidecar(2026-09-07): chunk 持久化 + #sym:/path::symbol 寻址
# ============================================================================

def _chunk_sidecar_path(session_key):
    d = os.path.join(_ps._DIAG_DIR, "chunks")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return os.path.join(d, memory_stores.sanitize_session_key(session_key)[:64] + ".jsonl")


def chunk_index_store(session_key, anchor, path, excerpt):
    """structure_aware_excerpt 的 symbols → sidecar 持久化。
    strategy=line 无符号可存。fail-open。"""
    syms = (excerpt or {}).get("symbols") or []
    if not syms or (excerpt or {}).get("strategy") == "line":
        return False
    try:
        with open(_chunk_sidecar_path(session_key), "a", encoding="utf-8") as f:
            f.write(json.dumps({"anchor": anchor, "path": path,
                                "strategy": excerpt.get("strategy"),
                                "symbols": syms}, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def chunk_index_load(session_key, anchor):
    """读取该锚的符号块索引; 无/损坏 → None。"""
    try:
        with open(_chunk_sidecar_path(session_key), encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if d.get("anchor") == anchor and d.get("symbols"):
                    return d
    except OSError:
        pass
    return None


def chunk_symbol_slice(session_key, anchor, symbol, content, ext=".py"):
    """锚+#sym: 的源码切片: 持久索引优先, 缺失时对 content 现算。
    行号切片基于 content 自身(行号前缀不影响按行对齐)。失败 → None。"""
    stripped = re.sub(r"(?m)^\s*\d+\t", "", content or "")
    lines = stripped.splitlines(keepends=True)
    blocks, _strategy = _code_blocks(stripped, ext)
    if not blocks:
        return None
    target = None
    idx = chunk_index_load(session_key, anchor)
    if idx:
        for s in idx.get("symbols") or []:
            nm = s.get("name", "")
            if nm == symbol or nm.split(".")[-1] == symbol.split(".")[-1]:
                target = (nm, s.get("sig", ""), s.get("line_start"),
                          s.get("line_end"))
                break
    if target is None:
        for s, e, sig_line in blocks:
            sig = lines[sig_line - 1].strip()
            nm = re.search(r"(?:class|def)\s+([A-Za-z_][\w]*)", sig)
            if nm and (nm.group(1) == symbol or
                       symbol.endswith("." + nm.group(1))):
                target = (nm.group(1), sig, s, e)
                break
    if target is None:
        # 方法级兜底: 任意缩进的 def <symbol>( 行扫描, 切到同级/低缩进边界
        # (top-level 符号表不含嵌套方法——类方法是最常被点名的召回对象)
        pat = re.compile(r"^(\s*)def\s+" + re.escape(symbol.split(".")[-1])
                         + r"\s*\(")
        slines = stripped.splitlines()
        for i, l in enumerate(slines):
            m = pat.match(l)
            if not m:
                continue
            base_indent = len(m.group(1))
            j = i + 1
            while j < len(slines):
                lj = slines[j]
                if lj.strip() and (len(lj) - len(lj.lstrip())) <= base_indent:
                    break
                j += 1
            target = (symbol, l.strip(), i + 1, j)
            break
    if target is None:
        return None
    nm, sig, ls, le = target
    seg = "".join(lines[ls - 1: le]).rstrip("\n")
    return "[symbol: %s | %s | L%d-L%d]\n%s" % (nm, sig[:80], ls, le, seg)


def recover_full_content(session_key, anchor, turn, max_chars=4000, offset=0):
    """恢复完整被丢弃内容; 锚含 #sym:函数名 时按符号切片(ast/索引),
    切片失败回退全文。其余口径见 _recover_full_content_raw。"""
    sym = None
    if anchor and "#sym:" in anchor:
        anchor, _, sym = anchor.partition("#sym:")
        sym = sym.strip() or None
    text = _recover_full_content_raw(session_key, anchor, turn,
                                     max_chars if not sym else 0, offset)
    if text and sym:
        sliced = chunk_symbol_slice(session_key, anchor, sym, text)
        if sliced:
            return sliced
    return text


def _recover_full_content_raw(session_key, anchor, turn, max_chars=4000, offset=0):
    """从 manifest 索引行恢复完整被丢弃的内容(支持分页续读)。

    数据流: manifest(地址: anchor+turn) → archive(内容: payload) → 完整 tool_result。
    anchor "r:t1" → tool_use_id "t1"; "u:t1" → 搜索 tool_use 块的 input(不适合恢复全文)。
    回退链尾部: orig/<sid>.jsonl(压缩时寄存的原文)——压缩标记 key=r:x 的兑现。
    offset>0 时返回原文的 [offset : offset+max_chars] 窗口(分页续读协议)。
    L-11/DEF-307(2026-09-06): epoch_collapse 行的 turn 是折叠时刻轮号, 内容
    躺在 archive 早期轮——精确轮号 miss 后回落**全扫**(取最后一次出现的
    非墓碑原文)。墓碑占位/零头(客户端改写产物)不作恢复来源。
    返回 str 或 None(未找到/archive 不存在)。
    """
    if not session_key or not anchor:
        return None
    tool_use_id = anchor[2:] if anchor.startswith("r:") else None
    if not tool_use_id:
        return None  # tool_use 锚不含 result 正文

    archive_path = os.path.join(_ps._DIAG_DIR, "archive",
                                session_key + ".jsonl")
    _tomb = "Tool result was not provided"
    fallback = None
    try:
        with open(archive_path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
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
                            if not text or _tomb in text:
                                continue  # 墓碑占位不作恢复来源
                            if turn is not None and entry.get("turn") == turn:
                                window = (text[offset:offset + max_chars]
                                          if max_chars else text[offset:])
                                return window or None
                            fallback = text  # 全扫兜底: 记最后一次有效原文
    except (FileNotFoundError, OSError):
        pass
    if fallback is not None:
        window = (fallback[offset:offset + max_chars]
                  if max_chars else fallback[offset:])
        return window or None
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


# ============================================================================
# 结构感知摘录(2026-09-06): ast 骨架 → 多语言启发式 → 行边界兜底 三级梯队
# ============================================================================

# ---- 第三级: 多语言启发式结构摘录(2026-09-06) ----
# 列 0 起始正则 + 配平定块尾; 命中 <2 视为无结构(调用方退行边界 fallback)。
# 已知边界(详见设计文档 §4.1): 字符串内花括号不感知、仅跳过整行 // 注释与
# #! 行、行尾注释/多行块注释含花括号会误计、Python 启发式按「下一列 0 起始
# 行」定块尾(仅 ast 失败时启用, 块间非块代码并入前块)。
_BRACE_START_RE = re.compile(r"^(?:"
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+|"  # js/ts
    r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+\w+|"  # js/ts/java
    r"(?:export\s+)?interface\s+\w+|"                             # ts/java
    r"(?:export\s+)?(?:const|let|var)\s+\w+\s*=|"                 # 箭头函数等
    r"func\s+(?:\([^)]*\)\s*)?\w+\s*\(|"                          # go(含方法)
    r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+\w+|"            # rust
    r"(?:pub\s+)?(?:struct|enum|impl|trait|mod)\s+\w+|"           # rust
    r"(?:(?:public|private|protected|static|final|abstract)\s+)*"
    r"(?:class|interface|enum)\s+\w+|"                            # java
    r"[A-Za-z_][\w\s\*]*\s\w+\s*\([^;]*\)\s*\{?"                  # c/cpp 函数
    r")")
_RB_START_RE = re.compile(r"^(?:def|class|module)\s")
_RB_END_RE = re.compile(r"^end\b")
_SH_START_RE = re.compile(r"^[A-Za-z_][\w-]*\s*\(\)\s*\{")
_PY_START_RE = re.compile(r"^(?:async\s+)?(?:def|class)\s+\w")

# 扩展名 → (起始正则, 配平模式); .py 仅 ast 失败(mid-edit 损坏代码)时到达
_HEURISTIC_LANGS = {}
for _ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".java",
             ".rs", ".c", ".h", ".cpp", ".cc", ".hpp"):
    _HEURISTIC_LANGS[_ext] = (_BRACE_START_RE, "brace")
_HEURISTIC_LANGS[".rb"] = (_RB_START_RE, "ruby")
_HEURISTIC_LANGS[".sh"] = (_SH_START_RE, "brace")
_HEURISTIC_LANGS[".bash"] = (_SH_START_RE, "brace")
_HEURISTIC_LANGS[".py"] = (_PY_START_RE, "nextstart")


def _heuristic_blocks(text, start_re, mode):
    """启发式顶层块检测：返回 [(start, end, sig)]（1-based 行号，含端点）。

    起始正则（列 0 锚定）命中数 <2 视为无结构，返回 None。
    mode：
      "brace"     —— 花括号配平（跳过整行 // 注释与 #! 行）；无花括号的
                     起始（箭头函数赋值等）在下一起始行前结束；
      "ruby"      —— 列 0 def/class/module 计数 + 列 0 end 配对；
      "nextstart" —— 块延伸至下一列 0 起始行前（ast 失败的 .py 用）。
    """
    lines = text.splitlines()
    idxs = [i for i, ln in enumerate(lines) if start_re.match(ln)]
    if len(idxs) < 2:
        return None
    blocks = []
    for k, s in enumerate(idxs):
        e = len(lines) - 1
        if mode == "nextstart":
            e = idxs[k + 1] - 1 if k + 1 < len(idxs) else len(lines) - 1
        elif mode == "ruby":
            depth = 0
            for i in range(s, len(lines)):
                if start_re.match(lines[i]):
                    depth += 1
                elif _RB_END_RE.match(lines[i]):
                    depth -= 1
                if i > s and depth <= 0:
                    e = i  # 列 0 关键字配平归零: 块结束
                    break
        else:  # brace
            depth, opened = 0, False
            for i in range(s, len(lines)):
                ln = lines[i]
                st = ln.strip()
                if st.startswith("//") or st.startswith("#!"):
                    continue
                if i > s and not opened and start_re.match(ln):
                    e = i - 1  # 无花括号声明在下一起始行前结束
                    break
                depth += ln.count("{") - ln.count("}")
                if depth > 0:
                    opened = True
                elif opened:
                    e = i  # 深度回到 0: 块结束
                    break
        blocks.append((s + 1, e + 1, s + 1))
    return blocks


def _code_blocks(text, ext):
    """→ (blocks, strategy)。无结构 → (None, "line")。行号前缀须先剥离。"""
    blocks, strategy = None, "line"
    if ext == ".py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            tree = None
        if tree is not None:
            cand = []
            for node in tree.body:
                if not isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                         ast.AsyncFunctionDef, ast.Assign,
                                         ast.Import, ast.ImportFrom)):
                    continue
                start = node.lineno
                for dec in getattr(node, "decorator_list", []):
                    start = min(start, dec.lineno)  # 装饰器并入块区间
                end = getattr(node, "end_lineno", None) or node.lineno
                cand.append((start, end, node.lineno))
            if cand:
                blocks, strategy = cand, "ast"
    if blocks is None:
        spec = _HEURISTIC_LANGS.get(ext)  # ast 失败/无块时也先尝试启发式
        if spec is not None:
            cand = _heuristic_blocks(text, spec[0], spec[1])
            if cand:
                blocks, strategy = cand, "heuristic"
    return blocks, strategy


def structure_aware_excerpt(content, target_path, budget_chars,
                            focus_terms=None):
    """按结构摘录文件内容（auto-recall 首次注入用）。

    三级梯队（strategy 枚举 "ast"|"heuristic"|"line"）：
      1. .py 且 ast.parse 成功 → stdlib ast 提取顶层块（class/def/assign/
         import）行区间，strategy="ast"；
      2. 其他受支持代码语言（_HEURISTIC_LANGS：js/ts/go/java/rs/c/cpp/rb/
         sh）或 ast 失败（mid-edit 损坏代码必须兜住）→ 多语言启发式
         结构摘录（见 _heuristic_blocks 及其已知边界注释），
         strategy="heuristic"；起始正则命中 <2 视为无结构；
      3. 非代码 / 启发式无命中 / 骨架超预算 → 行边界 fallback：取
         [0:budget] 对齐到最后一个完整行尾，strategy="line"。
    Read 工具产出的 "N\t" 行号前缀在解析/摘录/offset 前统一剥离
    （DEF-310/seq5：前缀使 ast.parse 必炸 → 恒落 line 兜底）。
    ast 与 heuristic 共用同一渲染：输出 = 文件骨架（签名行 + L 行号区间 +
    @ 字符偏移，偏移与 recover_full_content 的 anchor@offset 分页协议同
    口径，模型可直接拿骨架 offset 续读）+ 完整顶层块填充。
    focus_terms: 查询引导词（dup 目标/近期探查词）——命中多的块优先进入
    预算（2026-09-07 seq5 实证：文件序填充会让模型要的函数落在预算外）。
    返回 {"text", "strategy", "truncated",
          "symbols": [{name, sig, line_start, line_end, offset}]}——
    symbols 是 chunk 索引持久化与 #sym: 符号寻址的数据源。
    """
    text = content if isinstance(content, str) else ""
    try:
        budget = int(budget_chars)
    except (TypeError, ValueError):
        budget = 0
    if not text or budget <= 0:
        return {"text": "", "strategy": "line", "truncated": bool(text),
                "symbols": []}
    if len(text) <= budget:
        return {"text": text, "strategy": "line", "truncated": False,
                "symbols": []}

    def _line_fallback():
        head = text[:budget]
        cut = head.rfind("\n")
        if cut > 0:
            head = head[:cut + 1]  # 对齐到最后一个完整行尾
        return {"text": head, "strategy": "line", "truncated": True,
                "symbols": []}

    ext = os.path.splitext(str(target_path or ""))[1].lower()
    stripped = re.sub(r"(?m)^\s*\d+\t", "", text)
    if stripped != text:
        text = stripped
    blocks, strategy = _code_blocks(text, ext)
    if blocks is None:
        return _line_fallback()

    # 行号 → 字符偏移(与 recover_full_content 的 offset 切片同口径)
    lines = text.splitlines(keepends=True)
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln)

    def _start_off(line_no):
        return starts[line_no - 1] if 0 < line_no <= len(starts) else len(text)

    def _end_off(line_no):
        return starts[line_no] if line_no < len(starts) else len(text)

    # 符号表: chunk 索引持久化与 #sym: 符号寻址的数据源
    symbols = []
    for s, e, sig_line in blocks:
        sig = lines[sig_line - 1].strip()
        nm = re.search(r"(?:class|def)\s+([A-Za-z_][\w]*)", sig)
        symbols.append({"name": nm.group(1) if nm else sig[:48],
                        "sig": sig[:120], "line_start": s, "line_end": e,
                        "offset": _start_off(s)})

    header = ["[skeleton: %d top-level blocks; @N = char offset usable with "
              "anchor@N paging]" % len(blocks)]
    for s, e, sig_line in blocks:
        header.append("[L%d-L%d @%d] %s"
                      % (s, e, _start_off(s), lines[sig_line - 1].strip()))
    header_text = "\n".join(header)
    if len(header_text) >= budget:
        return _line_fallback()  # 预算连骨架都装不下 → 退化为行截断

    parts, used, omitted = [header_text], len(header_text), 0
    # focus 查询引导: 命中多的块优先(稳定序: 命中降序 → 文件序)
    order = list(range(len(blocks)))
    if focus_terms:
        ft = [t.lower() for t in focus_terms if isinstance(t, str) and t.strip()]
        if ft:
            def _hits(i):
                s, e, _ = blocks[i]
                seg = text[_start_off(s):_end_off(e)].lower()
                return sum(1 for t in ft if t in seg)
            order.sort(key=lambda i: (-_hits(i), i))
    for i in order:
        s, e, _ = blocks[i]
        blk = text[_start_off(s):_end_off(e)].rstrip("\n")
        need = len(blk) + 2  # "\n\n" 分隔
        if used + need <= budget:
            parts.append(blk)
            used += need
        else:
            omitted += 1
    return {"text": "\n\n".join(parts), "strategy": strategy,
            "truncated": omitted > 0, "symbols": symbols}


def auto_recall_for_target(session_key, target, max_chars=None,
                           focus_terms=None):
    """auto-recall 执行器（ctx-recall 自闭环设计 2026-09-05 §4③）：按目标取回折叠原文。

    数据源优先级：
      ① manifest 两段式精确匹配——unit_anchors 只给 tool_use 行带 handle
         (Read→file value)，tool_result 行 handle=None，故先找 handle.value
         == target 的 u: 行（取最新 turn），再配对同 tool_use_id 的 r: 行
         （r: 行才有正文可恢复）；
      ② 回落 fts_search(target, kind=tool_result)（head/triggers 模糊命中）。
    命中后取全量原文（max_chars=0 走 recover_full_content 不截断路径）再经
    structure_aware_excerpt 做结构感知摘录（预算 max_chars，缺省
    PROXY_AUTO_RECALL_MAX_CHARS；分页锚点保留，续读由模型经 ctx_recall
    query="锚点@偏移" 完成，续读路径不经过本函数、不受影响）。
    总开关关闭 / 无命中 / 恢复失败 → None（fail-open，调用方原样转发）。
    返回 {"anchor","turn","reason","chars","content","strategy"} 或 None。
    """
    if not session_key or not (target or "").strip():
        return None
    if not getattr(_ps, "PROXY_AUTO_RECALL_ENABLED", False):
        return None
    tgt = target.strip()
    if max_chars:
        try:
            limit = max(200, int(max_chars))
        except (TypeError, ValueError):
            limit = 4000
    else:
        try:
            limit = max(200, int(getattr(_ps, "PROXY_AUTO_RECALL_MAX_CHARS", 4000)))
        except (TypeError, ValueError):
            limit = 4000
    try:
        lines = memory_stores.MANIFEST.lines(session_key)
        # 候选集(全部同目标 r: 行, turn 降序=最新优先): 最新行不可恢复时
        # 回退旧副本(L-11 方案 C——epoch 行 turn=折叠时刻, archive 精确轮
        # 号 miss, 但该锚更早的行往往可恢复)
        candidates = []
        # ①a: u: 行按 handle 精确匹配 → 同 id r: 行
        uids = {}
        for line in lines:
            if line.get("kind") != "tool_use":
                continue
            handle = line.get("handle") or {}
            hval = handle.get("value", "") if isinstance(handle, dict) else ""
            if hval and hval.rstrip("/") == tgt.rstrip("/"):
                anchor = line.get("anchor") or ""
                if anchor.startswith("u:"):
                    uids[anchor[2:]] = line.get("turn") or 0
        if uids:
            for line in lines:
                anchor = line.get("anchor") or ""
                if (line.get("kind") == "tool_result"
                        and anchor.startswith("r:")
                        and anchor[2:] in uids):
                    candidates.append(line)
        # ②: 回落全文检索（head/triggers 含路径片段即命中）
        if not candidates:
            candidates.extend(
                l for l in fts_search(session_key, tgt, kind="tool_result",
                                      limit=3)
                if (l.get("anchor") or "").startswith("r:"))
        candidates.sort(key=lambda l: l.get("turn") or 0, reverse=True)
        content = None
        cand = None
        for line in candidates:
            got = recover_full_content(session_key, line["anchor"],
                                       line.get("turn"), max_chars=0)
            if got:
                content, cand = got, line
                break
        if not content:
            return None
        exc = structure_aware_excerpt(content, tgt, limit,
                                      focus_terms=focus_terms)
        if not exc["text"]:
            return None
        chunk_index_store(session_key, cand["anchor"], tgt, exc)
        return {"anchor": cand["anchor"], "turn": cand.get("turn"),
                "reason": cand.get("reason") or "",
                "chars": len(exc["text"]), "content": exc["text"],
                "strategy": exc["strategy"],
                "symbols": exc.get("symbols") or []}
    except Exception:
        return None


def build_task_context_bundle(task_descriptor, session_key=None,
                              budget_chars=6000, max_budget=20000,
                              max_items=12):
    """R18: 任务描述 → 上下文证据包（集成契约 §3.3 R18 冻结版，stdlib only）。

    检索面: session_key 给定 → 本会话 manifest FTS + 逐关键词二级查询；
    缺省 → 跨会话（MANIFEST.known_sessions 逐一 lookup，上限 64 会话）。
    内容面: anchor "r:*" 经 recover_full_content 取正文（预算内裁剪，
    单条 ≤4000）；不可恢复锚 → full_available=False 仅索引（优雅缺页）。
    纯检索无副作用；无命中返回空 items（契约: 不 404）。source 恒
    "manifest"——"semantic" 为 SEM 语义卡预留扩展点（gap-closure §2）。
    """
    import hashlib
    try:
        budget = int(budget_chars or 6000)
    except (TypeError, ValueError):
        budget = 6000
    budget = max(0, min(budget, max_budget))
    if not isinstance(task_descriptor, dict):
        task_descriptor = {}
    desc = str(task_descriptor.get("description") or "")
    keywords = [str(k) for k in (task_descriptor.get("keywords") or []) if str(k)]
    files = [os.path.basename(str(f)) for f in (task_descriptor.get("input_files") or []) if str(f)]
    queries = []
    base_q = " ".join(([desc] if desc else []) + keywords + files)[:400]
    if base_q.strip():
        queries.append(base_q)
    queries.extend(dict.fromkeys(keywords))

    def _search(sid):
        return _manifest_search(sid, queries, limit=max_items * 3)

    if session_key:
        sessions = [session_key]
    else:
        try:
            sessions = memory_stores.MANIFEST.known_sessions()
        except Exception:
            sessions = []

    candidates = []
    for sid in sessions:
        for ln in _search(sid):
            candidates.append((sid, ln))
            if len(candidates) >= max_items * 3:
                break
        if len(candidates) >= max_items * 3:
            break

    items, total = [], 0
    for sid, ln in candidates[:max_items]:
        anchor = ln.get("anchor") or ""
        turn = ln.get("turn")
        recoverable = isinstance(anchor, str) and anchor.startswith("r:")
        remaining = budget - total
        content = ""
        if recoverable and remaining >= 200:
            try:
                content = recover_full_content(
                    sid, anchor, turn,
                    max_chars=min(remaining, 4000)) or ""
            except Exception:
                content = ""
        used = len(content)
        if used == 0:
            head = (ln.get("head") or "")[:max(0, remaining)] if remaining > 0 else ""
            content = head
            used = len(content)
        matched = [t for t in dict.fromkeys(keywords + ([desc.strip()[:60]] if desc.strip() else []))
                   if t and t in line_text(ln)]
        trigger_why = ("命中: " + ", ".join(matched[:4])) if matched else "语义匹配"
        items.append({
            "unit_id": anchor or "u:{}:{}".format(sid, ln.get("turn")),
            "source": "manifest",
            "trigger_why": trigger_why,
            "preview_chars": len(ln.get("head") or ""),
            "content": content,
            "full_available": bool(recoverable),
        })
        total += used
        if total >= budget:
            break
    bundle_id = "b-" + hashlib.md5(json.dumps(
        {"q": base_q, "s": session_key or "",
         "a": [i["unit_id"] for i in items[:4]]},
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
    return {"bundle_id": bundle_id, "items": items,
            "total_chars": total, "budget_remaining": max(0, budget - total)}


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
    "auto_recall_for_target",
]
