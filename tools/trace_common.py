#!/usr/bin/env python3
"""trace_common.py — Phase B 轨迹工具共享层（stdlib only）。

数据流契约（logging-trajectory-improvement-design-20260820.md）:
  logs/proxy_requests.jsonl   请求流（A1 起含 session_id/request_id）
  logs/proxy_metrics.jsonl    24 管线阶段指标（request_id 关联）
  logs/diag/sessions.jsonl    R16 per-turn 深度记录（session_key + turn + request_id）
  logs/diag/ledger/<sid>.jsonl A3 台账增量（actions/filled/materials/mismatch）
  logs/diag/archive/<sid>.jsonl R15 sent_view（模型实际所见，payload 为 JSON 串）

关联键: request_id（请求↔指标↔诊断）、session_key（诊断↔台账↔档案）、
(session_key, turn)（轮次对齐）。所有读取只读主文件（.1 轮转件不含）。
"""
import json
import os
import re
from datetime import datetime

DEFAULT_LOGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def iter_jsonl(path):
    """逐行产出 JSON 记录（坏行跳过）。文件不存在静默结束。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return


def parse_ts(value):
    """ISO 时间串 → datetime；失败返回 None（过滤用，宽松处理）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def sanitize_key(session_key):
    """会话 key → 文件系统安全形式（与 session_ledger.sanitize_session_key 一致）。"""
    return re.sub(r'[^A-Za-z0-9_-]', '_', session_key or "")[:64] or "_anon"


class TraceStore(object):
    """五个数据流的懒加载只读索引。"""

    def __init__(self, logs_dir=None):
        self.logs_dir = logs_dir or DEFAULT_LOGS_DIR
        self.diag_dir = os.path.join(self.logs_dir, "diag")
        self._metrics_by_req = None      # request_id → metrics 记录
        self._diag_by_session = None     # session_key → [turn 记录...]（按 turn 升序）
        self._ledger_by_session = None   # session_key → [delta 行...]
        self._archive_by_session = None  # session_key → [sent_view 行...]

    # ------------------------------------------------------------ paths --
    def ledger_path(self, key):
        return os.path.join(self.diag_dir, "ledger", sanitize_key(key) + ".jsonl")

    def archive_path(self, key):
        return os.path.join(self.diag_dir, "archive", sanitize_key(key) + ".jsonl")

    @property
    def requests_path(self):
        return os.path.join(self.logs_dir, "proxy_requests.jsonl")

    @property
    def metrics_path(self):
        return os.path.join(self.logs_dir, "proxy_metrics.jsonl")

    @property
    def diag_sessions_path(self):
        return os.path.join(self.diag_dir, "sessions.jsonl")

    # ----------------------------------------------------------- indexes --
    def metrics_by_request(self):
        if self._metrics_by_req is None:
            idx = {}
            for rec in iter_jsonl(self.metrics_path):
                rid = rec.get("request_id")
                if rid:
                    idx[rid] = rec
            self._metrics_by_req = idx
        return self._metrics_by_req

    def diag_by_session(self):
        if self._diag_by_session is None:
            idx = {}
            for rec in iter_jsonl(self.diag_sessions_path):
                key = rec.get("session_key")
                if key:
                    idx.setdefault(key, []).append(rec)
            for rows in idx.values():
                rows.sort(key=lambda r: (r.get("turn") or 0,))
            self._diag_by_session = idx
        return self._diag_by_session

    def ledger_deltas(self, key):
        if self._ledger_by_session is None:
            self._ledger_by_session = {}
        if key not in self._ledger_by_session:
            self._ledger_by_session[key] = list(iter_jsonl(self.ledger_path(key)))
        return self._ledger_by_session[key]

    def archive_turns(self, key):
        if self._archive_by_session is None:
            self._archive_by_session = {}
        if key not in self._archive_by_session:
            self._archive_by_session[key] = list(iter_jsonl(self.archive_path(key)))
        return self._archive_by_session[key]

    # ------------------------------------------------------------ derived --
    def sessions_overview(self):
        """跨流会话清单: key → {turns, last_ts, sources[]}（并集，含已驱逐的档案会话）。"""
        overview = {}

        def touch(key, source, ts=None, turns=None):
            if not key:
                return
            item = overview.setdefault(key, {"sources": set()})
            item["sources"].add(source)
            if ts and (not item.get("last_ts") or ts > item["last_ts"]):
                item["last_ts"] = ts
            if turns:
                item["turns"] = max(item.get("turns") or 0, turns)

        for key, rows in self.diag_by_session().items():
            touch(key, "diag",
                  ts=rows[-1].get("ts") if rows else None,
                  turns=rows[-1].get("turn") if rows else 0)
        for key, rows in self._ledger_by_session.items() if self._ledger_by_session else {}:
            touch(key, "ledger",
                  ts=rows[-1].get("ts") if rows else None,
                  turns=rows[-1].get("turn") if rows else 0)
        for name in self._list_ledger_files():
            touch(name, "ledger-file")
        for name in self._list_archive_files():
            touch(name, "archive-file")
        return overview

    def _list_ledger_files(self):
        return self._list_dir(os.path.join(self.diag_dir, "ledger"))

    def _list_archive_files(self):
        return self._list_dir(os.path.join(self.diag_dir, "archive"))

    @staticmethod
    def _list_dir(path):
        names = []
        try:
            for fn in os.listdir(path):
                if fn.endswith(".jsonl"):
                    names.append(fn[:-len(".jsonl")])
        except OSError:
            pass
        return names


# ---------------------------------------------------------------- stats --
def percentile(values, ratio):
    """简单分位（与 admin_server._percentile 同语义;空列表 → None）。"""
    if not values:
        return None
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, int(round(ratio * (len(vals) - 1)))))
    return vals[k]


def fmt_ms(value):
    """毫秒 → 人类可读（<1s 用 ms，其余 s，保留 1 位）。"""
    if not isinstance(value, (int, float)):
        return "-"
    if value < 1000:
        return "%.0fms" % value
    return "%.1fs" % (value / 1000.0)


def replay_ledger_actions(deltas):
    """A3 台账增量 → 动作列表 + dup 派生（离线版 LedgerStore 重建逻辑）。

    返回 (actions, materials, mismatch_count, dropped)。actions 含
    dup/first_turn/last_dup_turn 派生字段（与 R14 端点口径一致）。
    """
    import collections
    actions = []
    materials = []
    material_keys = set()
    mismatch_count = 0
    dropped = 0
    for rec in deltas or []:
        if rec.get("mismatch"):
            actions = []
            materials = []
            material_keys = set()
            mismatch_count += 1
        for a in rec.get("actions") or []:
            if isinstance(a, dict) and a.get("tool"):
                actions.append(dict(a))
        for fl in rec.get("filled") or []:
            tid = fl.get("_tid")
            for act in reversed(actions):
                if act.get("_tid") == tid and act.get("result_chars") is None:
                    act["result_chars"] = fl.get("result_chars")
                    break
        for m in rec.get("materials") or []:
            key = m.get("path")
            if key and key not in material_keys:
                material_keys.add(key)
                materials.append(dict(m))
        dropped += rec.get("dropped") or 0
    groups = collections.defaultdict(list)
    for a in actions:
        groups[(a.get("tool"), a.get("target_hash"))].append(a)
    dup_queries = []
    for (tool, thash), group in groups.items():
        first_turn = group[0].get("turn", 0)
        last_turn = group[-1].get("turn", 0)
        for i, a in enumerate(group):
            a.pop("_tid", None)
            a["dup"] = i + 1
            a["first_turn"] = first_turn
            a["last_dup_turn"] = last_turn
        if len(group) > 1:
            dup_queries.append({
                "tool": tool, "target": group[0].get("target"),
                "count": len(group), "first_turn": first_turn, "last_turn": last_turn,
            })
    actions.sort(key=lambda a: a.get("turn", 0))
    dup_queries.sort(key=lambda d: -d["count"])
    return actions, materials, mismatch_count, dropped, dup_queries
