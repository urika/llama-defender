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
import unit_model as _um

# 每会话 action 软上限: 超出丢弃最老 occurrence 并累计 aggregated_dropped 计数
# (设计 §9 风险表: >2000 触发降级聚合的简化实现,保 dup 可见性)
MAX_ACTIONS_PER_SESSION = 5000
# 单轮 sent_view payload 落盘字符上限(约 400KB),超出截断加标记
MAX_ARCHIVE_PAYLOAD_CHARS = 400_000
# 每隔多少次 archive append 检查一次磁盘总量
ARCHIVE_SIZE_CHECK_INTERVAL = 64
# A3: 每隔多少次 ledger 落盘检查一次磁盘总量(同 archive 模式)
LEDGER_SIZE_CHECK_INTERVAL = 64

# Bash 输出中识别"产出文件"的保守启发式(材料清单,设计 D3)
_MATERIAL_RE = re.compile(
    r'(?:saved|created|wrote|written)\s+(?:to\s+)?([~/\w][\w./@-]{2,200})', re.IGNORECASE)

# 目标参数选择优先级(规范化用): 工具参数里最能代表"这次调用是什么"的字段
_TARGET_ARG_KEYS = ("query", "q", "search", "url", "file_path", "path",
                    "pattern", "command", "cmd", "name", "body")
_SEARCH_TOOLS = _um.SEARCH_TOOLS  # 词汇表统一至 unit_model(extract_handle 共用)
_FETCH_TOOLS = ("fetch", "curl", "download")
_FILE_TOOLS = ("read", "write", "edit", "glob", "grep", "ls", "notebookedit")
_WRITE_TOOLS = ("write", "edit")


def sanitize_session_key(sid):
    """会话 key → 文件系统安全形式(用于 archive 路径)。"""
    return re.sub(r'[^A-Za-z0-9_-]', '_', sid or "")[:64] or "_anon"


def _msg_hash(msg):
    """消息指纹——已迁移 unit_model.msg_hash(词汇表统一);保留薄委托供 __all__ 导出。"""
    return _um.msg_hash(msg)


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


# _extract_handle/_iter_blocks/_result_chars/_result_text 已统一至 unit_model
# (词汇表对齐,2026-08-29 模块化 review 调整 A);调用点直接走 _um.*。


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
        self._ledger_writes = 0

    # ------------------------------------------------------------- record --
    def record_request(self, session_key, messages, turn, key_source="unknown"):
        """扫描一次客户端全量历史(增量),更新台账。返回 canonical_mismatch 布尔。

        在管线 stage 0(RequestParser)之后调用——messages 必须是客户端原始
        Anthropic 格式,任何压缩/注入之前的视图。

        A3: 每请求的动作增量落盘 logs/diag/ledger/<sid>.jsonl(内存丢失后
        R14 端点可从档案重建——agent_go 轮级看门狗跨重启不失忆)。
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

            # A3: 采集本轮增量(供落盘)——计数器在扫描前后取差值
            _dropped_before = entry["aggregated_dropped"]
            _materials_before = len(entry["materials"])
            new_actions, cross_fills = self._scan_window(entry, window, turn or 1)
            _new_materials = list(entry["materials"][_materials_before:])
            self._persist_delta_locked(
                session_key, entry, turn or 1, mismatch, new_actions, cross_fills,
                _new_materials, entry["aggregated_dropped"] - _dropped_before)
            self._evict_locked(now)
            return mismatch

    def _scan_window(self, entry, window, turn):
        """处理增量窗口内的消息: tool_use/tool_result 配对 → action 记录。

        A3: 返回 (new_actions, cross_fills)——本轮新增 action 与对既往
        action 的 result 回填,供台账落盘增量重建使用。
        """
        new_actions = []
        cross_fills = []
        _new_action_ids = set()  # id() 身份集合: 区分本窗口新增 vs 跨请求回填
        for msg in window:
            role = msg.get("role")
            if role == "assistant":
                for block in _um.iter_blocks(msg):
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
                            "handle": _um.extract_handle(tool, args),
                            # 同窗口的 tool_result 立即可匹配
                            "_tid": block.get("id") or f"{thash}:{len(entry['actions'])}",
                        }
                        self._append_action(entry, action)
                        new_actions.append(action)
                        _new_action_ids.add(id(action))
                        self._collect_material_from_args(entry, tool, args, turn)
            elif role == "user":
                for block in _um.iter_blocks(msg):
                    if block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        # 找最近一个未填 result 的同 id action(增量场景 id 唯一)
                        for act in reversed(entry["actions"]):
                            if act.get("_tid") == tid and act.get("result_chars") is None:
                                act["result_chars"] = _um.result_chars(block)
                                # 本窗口新增的 action 已随 action 行落盘(含回填值);
                                # 仅跨请求回填需要单独记录
                                if id(act) not in _new_action_ids:
                                    cross_fills.append(
                                        {"_tid": tid, "result_chars": act["result_chars"]})
                                # Bash 类命令输出跑材料启发式(saved/created 模式)
                                if "bash" in (act.get("tool") or "").lower() \
                                        or "shell" in (act.get("tool") or "").lower() \
                                        or "exec" in (act.get("tool") or "").lower():
                                    self._collect_material_from_text(
                                        entry, _um.result_text(block, 2000), turn)
                                break
                        else:
                            # result 无配对(孤儿/压缩后残留)——材料启发式仍可跑
                            self._collect_material_from_text(
                                entry, _um.result_text(block, 2000), turn)
        return new_actions, cross_fills

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
        """R14: GET /api/session/<key>/ledger 响应体。未知会话 → None。

        A3: 内存优先,内存无(重启/TTL 驱逐)时从台账档案重建(契约验收:
        跨重启 5 分钟内可查;内存驱逐 ≠ 档案删除——驱逐会话有档案仍 200)。
        """
        with self._lock:
            entry = self._sessions.get(session_key)
            if entry is None:
                entry = self._load_from_file(session_key)
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

    def snapshot_actions(self, session_key, limit=200):
        """IFC Tier-0 只读消费: 最近 N 条 action 快照(时间正序,剥离内部字段)。"""
        with self._lock:
            entry = self._sessions.get(session_key)
            if not entry:
                return []
            return [{k: v for k, v in a.items() if not k.startswith("_")}
                    for a in entry["actions"][-limit:]]

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

    # ---------------------------------------------------- A3 persistence --
    def _ledger_path(self, session_key):
        return os.path.join(_ps._DIAG_LEDGER_DIR, sanitize_session_key(session_key) + ".jsonl")

    def _persist_delta_locked(self, session_key, entry, turn, mismatch,
                              new_actions, cross_fills, new_materials, dropped_delta):
        """A3: 每请求动作增量落盘(调用方持 self._lock;写失败静默——archive 同模式)。

        增量行 schema: {ts, turn, mismatch, msg_count, key_source, dropped,
                        actions: [action dict...], filled: [{_tid, result_chars}...],
                        materials: [{path, turn, via}...]}
        mismatch 行的 actions 为全量重建结果(重放时清空再灌)。
        """
        if not getattr(_ps, "PROXY_DIAG_LEDGER_ENABLED", True):
            return
        try:
            record = {
                "ts": datetime.now().isoformat(),
                "turn": turn,
                "mismatch": bool(mismatch),
                "msg_count": entry["msg_count"],
                "key_source": entry["key_source"],
                "dropped": int(dropped_delta),
                "actions": [dict(a) for a in new_actions],
                "filled": list(cross_fills),
                "materials": [dict(m) for m in new_materials],
            }
            os.makedirs(_ps._DIAG_LEDGER_DIR, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with open(self._ledger_path(session_key), "a", encoding="utf-8") as f:
                f.write(line)
            self._ledger_writes += 1
            if self._ledger_writes % LEDGER_SIZE_CHECK_INTERVAL == 0:
                self._enforce_ledger_cap_locked()
        except OSError:
            pass

    def _enforce_ledger_cap_locked(self):
        """ledger 目录总量 MB 上限: 删最老会话文件(archive 同模式)。"""
        cap = max(10, getattr(_ps, "PROXY_DIAG_LEDGER_MAX_MB", 100)) * 1024 * 1024
        try:
            files = []
            for name in os.listdir(_ps._DIAG_LEDGER_DIR):
                p = os.path.join(_ps._DIAG_LEDGER_DIR, name)
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

    def _load_from_file(self, session_key):
        """从台账档案重建 entry(内存未命中时;无档案 → None)。调用方持锁。"""
        path = self._ledger_path(session_key)
        if not os.path.isfile(path):
            return None
        entry = {
            "actions": [], "msg_count": 0, "msg_hashes": [], "materials": [],
            "material_keys": set(), "key_source": "unknown",
            "first_seen": time.time(), "last_seen": time.time(),
            "turn": 0, "canonical_mismatch_count": 0, "aggregated_dropped": 0,
        }
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
                    if rec.get("mismatch"):
                        # 客户端侧裁剪触发的全量重建行——重放时同样清空
                        entry["actions"] = []
                        entry["materials"] = []
                        entry["material_keys"] = set()
                        entry["canonical_mismatch_count"] += 1
                    for a in rec.get("actions") or []:
                        if isinstance(a, dict) and "tool" in a:
                            entry["actions"].append(dict(a))
                    for fl in rec.get("filled") or []:
                        tid = fl.get("_tid")
                        for act in reversed(entry["actions"]):
                            if act.get("_tid") == tid and act.get("result_chars") is None:
                                act["result_chars"] = fl.get("result_chars")
                                break
                    for m in rec.get("materials") or []:
                        key = m.get("path")
                        if key and key not in entry["material_keys"]:
                            entry["material_keys"].add(key)
                            entry["materials"].append(dict(m))
                    entry["msg_count"] = max(entry["msg_count"], rec.get("msg_count") or 0)
                    entry["turn"] = max(entry["turn"], rec.get("turn") or 0)
                    entry["aggregated_dropped"] += rec.get("dropped") or 0
                    ks = rec.get("key_source")
                    if ks and ks != "unknown":
                        entry["key_source"] = ks
        except OSError:
            return None
        if not entry["actions"] and not entry["materials"] and entry["turn"] == 0:
            return None  # 空档案视同无档案
        return entry


class ArchiveStore(object):
    """R15 sent_view 档案: 每轮实际发给后端的最终 payload 落盘 + 索引读取。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._writes = 0

    def _session_path(self, session_key):
        return os.path.join(_ps._DIAG_ARCHIVE_DIR, sanitize_session_key(session_key) + ".jsonl")

    def has_archive(self, session_key):
        """该会话是否有档案文件（G-D：端点 key 归并判据之一）。"""
        try:
            return os.path.isfile(self._session_path(session_key))
        except OSError:
            return False

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
