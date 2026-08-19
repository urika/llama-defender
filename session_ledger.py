#!/usr/bin/env python3
"""session_ledger.py — R14/R15 diagnostics stores (stdlib only).

设计依据: docs/02-architecture-design/diagnostics-dataplane-design-20260819.md
  - D3: 台账从客户端原始历史(每请求全量重发)增量派生,不依赖 canonical history
        (与上下文工程 Phase 1 解耦,先行落地)。前缀失配 → 全量重建 + canonical_mismatch。
  - D4: turn = 代理所见该会话的请求序号(一次请求内多工具调用同 turn)。
  - D5: 会话 key 沿用 X-Claude-Code-Session-Id[:8];key_source 记录来源。
  - D7: sent_view(实际发给后端的最终 payload)是"模型实际所见"的唯一权威,
        常态每轮落盘 logs/diag/archive/<sid>.jsonl,受 MB 上限 + TTL 约束。

线程安全: 单例 store,内部 threading.Lock(仿 _metrics_lock 模式)。
有界性: 会话数 FIFO 上限、每会话 action 数软上限、archive 磁盘 MB 上限。
"""
import collections
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime

import proxy_state as _ps

# 每会话 action 软上限: 超出丢弃最老 occurrence 并累计 aggregated_dropped 计数
# (设计 §9 风险表: >2000 触发降级聚合的简化实现,保 dup 可见性)
MAX_ACTIONS_PER_SESSION = 5000
# 单轮 sent_view payload 落盘字符上限(约 400KB),超出截断加标记
MAX_ARCHIVE_PAYLOAD_CHARS = 400_000
# 每隔多少次 archive append 检查一次磁盘总量
ARCHIVE_SIZE_CHECK_INTERVAL = 64

# Bash 输出中识别"产出文件"的保守启发式(材料清单,设计 D3)
_MATERIAL_RE = re.compile(
    r'(?:saved|created|wrote|written)\s+(?:to\s+)?([~/\w][\w./@-]{2,200})', re.IGNORECASE)

# 目标参数选择优先级(规范化用): 工具参数里最能代表"这次调用是什么"的字段
_TARGET_ARG_KEYS = ("query", "q", "search", "url", "file_path", "path",
                    "pattern", "command", "cmd", "name", "body")
_SEARCH_TOOLS = ("search", "websearch", "web_fetch", "webfetch", "query")
_FETCH_TOOLS = ("fetch", "curl", "download")
_FILE_TOOLS = ("read", "write", "edit", "glob", "grep", "ls", "notebookedit")
_WRITE_TOOLS = ("write", "edit")


def sanitize_session_key(sid):
    """会话 key → 文件系统安全形式(用于 archive 路径)。"""
    return re.sub(r'[^A-Za-z0-9_-]', '_', sid or "")[:64] or "_anon"


def _msg_hash(msg):
    """消息指纹(前缀 diff 用)——sort_keys 保证 key 顺序不稳定不误判。"""
    try:
        raw = json.dumps(msg, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = repr(msg)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def normalize_target(tool_name, args):
    """规范化 (tool, args) → (target 展示串, target_hash)。

    确定性、无 LLM(设计 §4.2 规则 2): dup 判定的基础。
    search→query 小写压空白; file→路径; fetch→URL; 其余→首个可用参数。
    """
    tool = (tool_name or "").lower()
    args = args if isinstance(args, dict) else {}
    target = ""
    for key in _TARGET_ARG_KEYS:
        if key in args and isinstance(args[key], (str, int, float)):
            target = str(args[key]).strip()
            break
    if not target:
        # 兜底: 排序后的首个字符串参数
        for key in sorted(args.keys()):
            v = args[key]
            if isinstance(v, (str, int, float)):
                target = str(v).strip()
                break
    norm = target
    if any(t in tool for t in _SEARCH_TOOLS):
        norm = re.sub(r'\s+', ' ', target.lower())
    elif any(t in tool for t in _FILE_TOOLS):
        norm = target.rstrip('/')
    elif any(t in tool for t in _FETCH_TOOLS):
        norm = target.split('?')[0].lower().rstrip('/')
    else:
        norm = re.sub(r'\s+', ' ', target)[:200]
    h = hashlib.md5(f"{tool}|{norm}".encode("utf-8")).hexdigest()[:12]
    return norm[:200], h


def _extract_handle(tool, args):
    """句柄(Manus 可恢复原则,上游设计 §4.3): 重新取得该内容的充分信息。"""
    tool_l = (tool or "").lower()
    args = args if isinstance(args, dict) else {}
    for key in ("url", "file_path", "path", "query"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            kind = {"url": "url", "file_path": "path", "path": "path"}.get(key, "query")
            return {"type": kind, "value": v.strip()[:300]}
    if any(t in tool_l for t in _SEARCH_TOOLS):
        return {"type": "tool", "value": tool}
    return None


def _iter_blocks(msg):
    """遍历一条消息的 content block(list 形态);string content 不产出 block。"""
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _result_chars(block):
    """tool_result 内容规模(字符)。"""
    content = block.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(json.dumps(b, ensure_ascii=False)) for b in content
                   if isinstance(b, (dict, str)))
    return 0


def _result_text(block, limit=2000):
    """tool_result 文本(材料启发式用,截断)。"""
    content = block.get("content")
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)[:limit]
    return ""


class LedgerStore(object):
    """R14 会话台账: 每请求增量扫描客户端原始 Anthropic 历史。

    记录 action 轨迹(tool_use/tool_result 配对)→ dup/last_dup_turn/材料清单。
    dup 在查询时派生(便宜且对增量 append 天然一致)。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = collections.OrderedDict()  # key → entry (FIFO 驱逐依赖有序)
        self._evicted = {}  # key → evicted_at iso (410 语义)
        self._archive_writes = 0

    # ------------------------------------------------------------- record --
    def record_request(self, session_key, messages, turn, key_source="unknown"):
        """扫描一次客户端全量历史(增量),更新台账。返回 canonical_mismatch 布尔。

        在管线 stage 0(RequestParser)之后调用——messages 必须是客户端原始
        Anthropic 格式,任何压缩/注入之前的视图。
        """
        if not _ps.PROXY_DIAG_ENABLED or not session_key:
            return False
        now = time.time()
        hashes = [_msg_hash(m) for m in (messages or [])]
        with self._lock:
            entry = self._sessions.get(session_key)
            if entry is None:
                entry = {
                    "actions": [],
                    "msg_count": 0,
                    "msg_hashes": [],
                    "materials": [],
                    "material_keys": set(),
                    "key_source": key_source,
                    "first_seen": now,
                    "last_seen": now,
                    "turn": 0,
                    "canonical_mismatch_count": 0,
                    "aggregated_dropped": 0,
                }
                self._sessions[session_key] = entry
            entry["last_seen"] = now
            entry["turn"] = max(entry["turn"], turn or 0)
            if key_source != "unknown":
                entry["key_source"] = key_source

            prev_hashes = entry["msg_hashes"]
            common = 0
            for a, b in zip(prev_hashes, hashes):
                if a != b:
                    break
                common += 1
            mismatch = common < len(prev_hashes)
            if mismatch:
                # 客户端侧裁剪/微压缩(上游设计 §4.7.4): 全量重建,保正确性
                entry["actions"] = []
                entry["materials"] = []
                entry["material_keys"] = set()
                entry["canonical_mismatch_count"] += 1
                window = messages or []
            else:
                window = (messages or [])[len(prev_hashes):]
            entry["msg_count"] = len(messages or [])
            entry["msg_hashes"] = hashes

            self._scan_window(entry, window, turn or 1)
            self._evict_locked(now)
            return mismatch

    def _scan_window(self, entry, window, turn):
        """处理增量窗口内的消息: tool_use/tool_result 配对 → action 记录。"""
        for msg in window:
            role = msg.get("role")
            if role == "assistant":
                for block in _iter_blocks(msg):
                    if block.get("type") == "tool_use":
                        tool = block.get("name", "")
                        args = block.get("input") or {}
                        target, thash = normalize_target(tool, args)
                        action = {
                            "turn": turn,
                            "tool": tool,
                            "target": target,
                            "target_hash": thash,
                            "result_chars": None,
                            "handle": _extract_handle(tool, args),
                            # 同窗口的 tool_result 立即可匹配
                            "_tid": block.get("id") or f"{thash}:{len(entry['actions'])}",
                        }
                        self._append_action(entry, action)
                        self._collect_material_from_args(entry, tool, args, turn)
            elif role == "user":
                for block in _iter_blocks(msg):
                    if block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        # 找最近一个未填 result 的同 id action(增量场景 id 唯一)
                        for act in reversed(entry["actions"]):
                            if act.get("_tid") == tid and act.get("result_chars") is None:
                                act["result_chars"] = _result_chars(block)
                                # Bash 类命令输出跑材料启发式(saved/created 模式)
                                if "bash" in (act.get("tool") or "").lower() \
                                        or "shell" in (act.get("tool") or "").lower() \
                                        or "exec" in (act.get("tool") or "").lower():
                                    self._collect_material_from_text(
                                        entry, _result_text(block), turn)
                                break
                        else:
                            # result 无配对(孤儿/压缩后残留)——材料启发式仍可跑
                            self._collect_material_from_text(entry, _result_text(block), turn)

    def _append_action(self, entry, action):
        actions = entry["actions"]
        actions.append(action)
        if len(actions) > MAX_ACTIONS_PER_SESSION:
            drop_n = len(actions) - MAX_ACTIONS_PER_SESSION + MAX_ACTIONS_PER_SESSION // 10
            entry["aggregated_dropped"] += drop_n
            del actions[:drop_n]

    def _collect_material_from_args(self, entry, tool, args, turn):
        tool_l = (tool or "").lower()
        if any(t in tool_l for t in _WRITE_TOOLS):
            fp = args.get("file_path") or args.get("path") if isinstance(args, dict) else None
            if isinstance(fp, str) and fp.strip():
                self._add_material(entry, fp.strip(), turn, via=tool_l)

    def _collect_material_from_text(self, entry, text, turn):
        if not text:
            return
        for m in _MATERIAL_RE.finditer(text):
            self._add_material(entry, m.group(1), turn, via="bash")

    def _add_material(self, entry, path, turn, via):
        key = path
        if key in entry["material_keys"]:
            return
        entry["material_keys"].add(key)
        entry["materials"].append({"path": path[:300], "turn": turn, "via": via})

    def _evict_locked(self, now):
        """TTL + FIFO 驱逐(设计 P4: 全状态有界)。"""
        ttl_sec = max(60, _ps.PROXY_DIAG_SESSION_TTL_MIN) * 60
        max_sessions = max(4, _ps.PROXY_DIAG_SESSION_MAX)
        # TTL
        stale = [k for k, e in self._sessions.items() if now - e["last_seen"] > ttl_sec]
        for k in stale:
            self._evicted[k] = datetime.now().isoformat()
            del self._sessions[k]
        # FIFO
        while len(self._sessions) > max_sessions:
            k, _ = self._sessions.popitem(last=False)
            self._evicted[k] = datetime.now().isoformat()
        # 驱逐记录自身有界
        if len(self._evicted) > 512:
            for k in list(self._evicted.keys())[:256]:
                del self._evicted[k]

    # -------------------------------------------------------------- query --
    def build_ledger_json(self, session_key, limit_turns=None):
        """R14: GET /api/session/<key>/ledger 响应体。未知会话 → None。"""
        with self._lock:
            entry = self._sessions.get(session_key)
            if entry is None:
                return None
            actions = [dict(a) for a in entry["actions"]]
            materials = list(entry["materials"])
            entry_turn = entry["turn"]
            stats_common = {
                "canonical_mismatch_count": entry["canonical_mismatch_count"],
                "key_source": entry["key_source"],
                "aggregated_dropped": entry["aggregated_dropped"],
            }
        if limit_turns and entry_turn:
            lo = max(1, entry_turn - limit_turns + 1)
            actions = [a for a in actions if a.get("turn", 0) >= lo]
        # dup 派生: (tool, target_hash) 分组计数
        groups = collections.defaultdict(list)
        for a in actions:
            groups[(a["tool"], a["target_hash"])].append(a)
        out_actions = []
        dup_queries = []
        for (tool, thash), group in groups.items():
            count = len(group)
            first_turn = group[0]["turn"]
            last_turn = group[-1]["turn"]
            for i, a in enumerate(group):
                a.pop("_tid", None)
                a["dup"] = i + 1
                a["first_turn"] = first_turn
                a["last_dup_turn"] = last_turn
                out_actions.append(a)
            if count > 1:
                dup_queries.append({
                    "tool": tool,
                    "target": group[0]["target"],
                    "count": count,
                    "first_turn": first_turn,
                    "last_turn": last_turn,
                })
        out_actions.sort(key=lambda a: a.get("turn", 0))
        dup_queries.sort(key=lambda d: -d["count"])
        return {
            "session_key": session_key,
            "turns_seen": entry_turn,
            "messages_seen": entry["msg_count"],
            "updated_at": datetime.fromtimestamp(entry["last_seen"]).isoformat(),
            "actions": out_actions,
            "dup_queries": dup_queries[:50],
            "materials": materials,
            **stats_common,
        }

    def list_sessions(self):
        """GET /api/sessions 列表(发现端点,设计 §4.5)。"""
        now = time.time()
        with self._lock:
            items = []
            for key, entry in self._sessions.items():
                items.append({
                    "key": key,
                    "key_source": entry["key_source"],
                    "turns": entry["turn"],
                    "actions": len(entry["actions"]),
                    "materials": len(entry["materials"]),
                    "last_seen": datetime.fromtimestamp(entry["last_seen"]).isoformat(),
                    "idle_min": round((now - entry["last_seen"]) / 60, 1),
                })
            return {"sessions": items, "evicted_seen": len(self._evicted)}

    def evicted_at(self, session_key):
        with self._lock:
            return self._evicted.get(session_key)

    def session_alive(self, session_key):
        with self._lock:
            return session_key in self._sessions


class ArchiveStore(object):
    """R15 sent_view 档案: 每轮实际发给后端的最终 payload 落盘 + 索引读取。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._writes = 0

    def _session_path(self, session_key):
        return os.path.join(_ps._DIAG_ARCHIVE_DIR, sanitize_session_key(session_key) + ".jsonl")

    def append_turn(self, session_key, turn, payload, injections, meta=None):
        """追加一轮 sent_view。payload 为发给后端的完整请求体 dict。

        截断保底: 单轮 payload 超 MAX_ARCHIVE_PAYLOAD_CHARS 截断加标记;
        磁盘总量每 ARCHIVE_SIZE_CHECK_INTERVAL 次检查一次(设计 §9)。
        """
        if not _ps.PROXY_DIAG_ENABLED or not _ps.PROXY_DIAG_ARCHIVE_ENABLED:
            return
        if not session_key:
            return
        meta = meta or {}
        try:
            payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            payload_str = json.dumps({"_unserializable": True}, ensure_ascii=False)
        truncated = False
        if len(payload_str) > MAX_ARCHIVE_PAYLOAD_CHARS:
            payload_str = payload_str[:MAX_ARCHIVE_PAYLOAD_CHARS] + '…"_truncated_by_proxy"'
            truncated = True
        record = {
            "ts": datetime.now().isoformat(),
            "turn": turn,
            "view": "sent",
            "injections": injections or [],
            "chars": len(payload_str),
            "payload_truncated": truncated,
            "model": meta.get("model"),
            "route_target": meta.get("route_target"),
            "messages": meta.get("messages"),
            "payload": payload_str,
        }
        os.makedirs(_ps._DIAG_ARCHIVE_DIR, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                with open(self._session_path(session_key), "a", encoding="utf-8") as f:
                    f.write(line)
                self._writes += 1
                if self._writes % ARCHIVE_SIZE_CHECK_INTERVAL == 0:
                    self._enforce_cap_locked()
            except OSError:
                pass

    def _enforce_cap_locked(self):
        """archive 总量 MB 上限: 删最老会话文件。"""
        cap = max(10, _ps.PROXY_DIAG_ARCHIVE_MAX_MB) * 1024 * 1024
        try:
            files = []
            for name in os.listdir(_ps._DIAG_ARCHIVE_DIR):
                p = os.path.join(_ps._DIAG_ARCHIVE_DIR, name)
                if os.path.isfile(p):
                    files.append((os.path.getmtime(p), os.path.getsize(p), p))
            total = sum(sz for _, sz, _ in files)
            if total <= cap:
                return
            files.sort()
            for _, sz, p in files:
                if total <= cap:
                    break
                try:
                    os.remove(p)
                    total -= sz
                except OSError:
                    pass
        except OSError:
            pass

    def read(self, session_key, turn=None, limit=50, offset=0, include_payload=False):
        """读取档案。返回 (records, error) — error: None|"not_found"|"unsupported"。"""
        path = self._session_path(session_key)
        if not os.path.isfile(path):
            return None, "not_found"
        records = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if turn is not None and rec.get("turn") != turn:
                        continue
                    records.append(rec)
        except OSError:
            return None, "not_found"
        total = len(records)
        window = records[offset:offset + max(1, limit)]
        if not include_payload:
            for rec in window:
                rec.pop("payload", None)
        else:
            for rec in window:
                # payload 以 JSON 字符串存储(截断安全),消费方自行 json.loads
                pass
        return {"total": total, "offset": offset, "limit": limit,
                "turns": window}, None


# 模块级单例(pipeline/admin 经此访问)
LEDGER = LedgerStore()
ARCHIVE = ArchiveStore()

__all__ = [
    "LedgerStore", "ArchiveStore", "LEDGER", "ARCHIVE",
    "normalize_target", "sanitize_session_key", "_msg_hash",
    "MAX_ACTIONS_PER_SESSION", "MAX_ARCHIVE_PAYLOAD_CHARS",
]
