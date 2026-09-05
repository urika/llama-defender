#!/usr/bin/env python3
"""memory_stores.py — PDC 披露清单存储（stdlib only，A3 模式）。

R10.1（PRD v3.1 / PDC 设计 R1「索引永不丢」）：manifest 是会话的页表——
每个被丢弃单元恰留一条可寻址索引行，内容驱逐后索引行仍保留（优雅缺页
的 not-present 语义载体）。行格式对齐台账 action 行（确定性、无 LLM）。

写入方（全部为零冲突模块内 fail-open 钩子）：
  - truncation.py   fifo 截断 / OOMSafetyFIFO 紧急截断
  - context_engine  epoch 折叠（_collapse 收编轮次）
有界性：每会话行数软上限（超出丢最老并计 dropped）；会话数 FIFO 上限；
磁盘 logs/diag/manifest/<sid>.jsonl 增量落盘 + 全局 MB 上限（最老会话驱逐）。

配置：PROXY_PD_ENABLED 经 getattr(_ps,...,True) 读取；CONFIG_REGISTRY
正式注册待并行工作合并后补。
"""
import json
import os
import threading
from datetime import datetime

import proxy_state as _ps

from session_ledger import sanitize_session_key

# 每会话索引行软上限（对齐 MAX_ACTIONS_PER_SESSION 的降级思路，量级更小：
# manifest 只记丢弃单元，长会话典型 <500 行）
MAX_LINES_PER_SESSION = 2000
MANIFEST_MAX_SESSIONS = 64
MANIFEST_MAX_MB = 100
_SIZE_CHECK_INTERVAL = 64
# PDC-L3(2026-08-31): 跨批隔离——重启/换批后同一会话键的旧 manifest 会稀释
# FTS 召回(实测 s384d8ac 混入 8/25 旧任务内容)。超过此间隔的旧文件视为
# 上一批次, 轮转归档(.prev)后重建。
SESSION_GAP_SECONDS = 2 * 3600


class ManifestStore(object):
    """会话 → 索引行列表（内存权威 + 增量落盘，重启后可从文件重建）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}      # key → list[dict]
        self._order = []         # FIFO 驱逐依赖有序
        self._writes = 0

    # ------------------------------------------------------------- record --
    def record_units(self, session_key, turn, reason, units, meta=None):
        """记录一批被丢弃单元的索引行。units: ifc_metrics.unit_anchors 输出。"""
        if not getattr(_ps, "PROXY_PD_ENABLED", True) or not session_key:
            return 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        lines = []
        for u in units or []:
            lines.append({
                "turn": turn,
                "reason": reason,
                "anchor": u.get("anchor"),
                "kind": u.get("kind"),
                "role": u.get("role"),
                "tool": u.get("tool") or "",
                "handle": u.get("handle"),
                "size_chars": u.get("size_chars", 0),
                "head": (u.get("head") or "")[:240],  # PDC 索引扩容: 与 ifc_metrics head 240 对齐(此前 120 截断抵消扩容)
                "triggers": (u.get("triggers") or "")[:240],  # §3.1: 指称性实体入索引词表
                "ts": now_iso,
            })
        if not lines:
            return 0
        dropped_overflow = 0
        with self._lock:
            entry = self._sessions.get(session_key)
            if entry is None:
                # PDC-L3: 首次写入前检查磁盘残留是否上一批次(跨批隔离)
                self._rotate_stale_file(session_key)
                if len(self._order) >= MANIFEST_MAX_SESSIONS:
                    oldest = self._order.pop(0)
                    self._sessions.pop(oldest, None)
                self._order.append(session_key)
                entry = []
                self._sessions[session_key] = entry
            entry.extend(lines)
            if len(entry) > MAX_LINES_PER_SESSION:
                dropped_overflow = len(entry) - MAX_LINES_PER_SESSION
                del entry[:dropped_overflow]
            self._persist_locked(session_key, lines)
            self._writes += 1
            if self._writes % _SIZE_CHECK_INTERVAL == 0:
                self._enforce_cap_locked()
        return len(lines)

    # --------------------------------------------------------------- read --
    def lines(self, session_key, limit=None):
        """只读快照（时间正序）。内存缺失时尝试从磁盘重建（重启恢复）。

        PDC-L3: 磁盘残留超批次间隔时先轮转再读——避免把上一批的索引行
        当作当前会话的可寻址性基线（召回精度 + IFC 锚点差分同样受益）。
        """
        with self._lock:
            entry = self._sessions.get(session_key)
            if entry is None:
                self._rotate_stale_file(session_key)
                entry = self._load_from_file(session_key)
                if entry:
                    self._sessions[session_key] = entry
                    self._order.append(session_key)
            out = list(entry or [])
        return out if limit is None else out[-limit:]

    def known_sessions(self):
        """已知会话 key 列表（内存 ∪ 磁盘 manifest 文件名，跨会话检索用，R18）。

        上限 MANIFEST_MAX_SESSIONS——与内存驱逐上限对齐，防无界枚举。
        """
        keys = set(self._sessions.keys())
        try:
            d = self._manifest_dir()
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    if fn.endswith(".jsonl"):
                        keys.add(fn[:-len(".jsonl")])
        except OSError:
            pass
        return sorted(keys)[:MANIFEST_MAX_SESSIONS]

    def count(self, session_key):
        return len(self.lines(session_key))

    # -------------------------------------------------------- persistence --
    def _manifest_dir(self):
        return os.path.join(_ps._DIAG_DIR, "manifest")

    def _path(self, session_key):
        # 不缓存: _DIAG_DIR 可被测试重定向,缓存会造成跨目录串写(2026-08-29 实测)
        return os.path.join(self._manifest_dir(),
                            sanitize_session_key(session_key) + ".jsonl")

    def _rotate_stale_file(self, session_key):
        """PDC-L3: 磁盘 manifest 与当前时间间隔超 SESSION_GAP_SECONDS →
        视为上一批次, 轮转 .prev 后由调用方从空重建。fail-open。

        判据: 文件最后一行的 ts(写入时戳)。批跑断点续跑/同日重跑共用会话键,
        旧行若混入新批 FTS 会以旧任务内容稀释召回精度。
        """
        try:
            path = self._path(session_key)
            with open(path, "r", encoding="utf-8") as f:
                last_ts = None
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        last_ts = json.loads(raw).get("ts")
                    except (json.JSONDecodeError, ValueError):
                        continue
            if not last_ts:
                return
            from datetime import datetime as _dt
            age = _dt.now() - _dt.fromisoformat(last_ts)
            if age.total_seconds() > SESSION_GAP_SECONDS:
                archived = path + ".prev"
                try:
                    os.replace(path, archived)
                except OSError:
                    return
                # FTS 索引同源失效——一并轮转, 下次查询按空索引重建
                try:
                    import ctx_recall as _cr  # 延迟导入防循环
                    idx = _cr._db_path(session_key)
                    if os.path.exists(idx):
                        os.replace(idx, idx + ".prev")
                except Exception:
                    pass  # 索引轮转失败不阻塞——行数校验会触发重建
        except (OSError, ValueError):
            pass

    def _persist_locked(self, session_key, new_lines):
        """增量落盘（调用方持锁）。OSError 静默——manifest 故障不影响请求路径。"""
        try:
            d = self._manifest_dir()
            os.makedirs(d, exist_ok=True)
            os.chmod(d, 0o700)
            with open(self._path(session_key), "a", encoding="utf-8") as f:
                for line in new_lines:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _load_from_file(self, session_key):
        try:
            with open(self._path(session_key), "r", encoding="utf-8") as f:
                lines = []
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        lines.append(json.loads(raw))
                    except (json.JSONDecodeError, ValueError):
                        continue
                return lines[-MAX_LINES_PER_SESSION:]
        except (FileNotFoundError, OSError):
            return []

    def _enforce_cap_locked(self):
        """全局磁盘 MB 上限：超限删最老会话文件（台账 _enforce 同款）。"""
        try:
            d = self._manifest_dir()
            if not os.path.isdir(d):
                return
            total = 0
            files = []
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    st = os.stat(p)
                    files.append((st.st_mtime, p, st.st_size))
                    total += st.st_size
                except OSError:
                    continue
            cap = MANIFEST_MAX_MB * 1024 * 1024
            if total <= cap:
                return
            files.sort()
            for _, p, size in files:
                if total <= cap:
                    break
                try:
                    os.remove(p)
                    total -= size
                    # 路径反查会话键(sanitize 不可逆,按键集比对;驱逐低频 O(n) 无碍)
                    key = next((k for k in list(self._sessions)
                                if self._path(k) == p), None)
                    if key:
                        self._sessions.pop(key, None)
                        if key in self._order:
                            self._order.remove(key)
                except OSError:
                    continue
        except OSError:
            pass  # 磁盘巡检失败静默——manifest 故障不影响请求路径

    def reset(self):
        """测试辅助：清空内存态（不动磁盘）。"""
        with self._lock:
            self._sessions.clear()
            self._order.clear()


MANIFEST = ManifestStore()


def record_dropped_messages(session_key, turn, reason, messages, msg_index_base=0):
    """便捷钩子：一批被丢弃的完整消息 → 索引行（truncation/epoch 调用点）。

    text 单元逐条记录（anchor 含消息指纹，可对账）；返回写入行数。
    fail-open：任何异常吞掉由调用方 warn（manifest 故障不得影响请求路径）。
    """
    import ifc_metrics
    import unit_model as _um
    units = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        _msg_units = ifc_metrics.unit_anchors(msg)
        # 2026-09-05(TC17/D3): unit_anchors 不产 triggers 的单元（tool_result
        # 等）按消息文本做实体提取补齐——检索关键词面的登记侧兜底。fail-open
        try:
            _text = _um.result_text(msg) or _um.text_str(msg.get("content"))
            _ents = _um.extract_key_entities(_text) if _text else []
            if _ents:
                for u in _msg_units:
                    if not (u.get("triggers") or "").strip():
                        u["triggers"] = " ".join(_ents)[:240]
        except Exception:
            pass
        units.extend(_msg_units)
    # 2026-09-05(TC16/D2): 登记幂等——同会话已存在的锚点不再重复登记
    # （fifo 无状态：客户端每轮重发全量历史，同一批老消息每轮被重复裁剪，
    # 生产实测 3x 膨胀即此因）。检索侧锚点去重仍保留作存量兜底。
    try:
        _existing = {r.get("anchor") for r in MANIFEST.lines(session_key)}
        if _existing:
            units = [u for u in units if u.get("anchor") not in _existing]
    except Exception:
        pass
    return MANIFEST.record_units(session_key, turn, reason, units)


# ============================================================================
# 原文寄存(可恢复压缩, 2026-09-01): 压缩标记带 key 的前提是原文有处可查。
# 压缩时把原文写入 orig/<sid>.jsonl, 锚点直查/ctx_recall 均可取回——
# 压缩从"有损"变"可恢复", [ctx:...key=r:x] 的承诺才有实现。
# ============================================================================

ORIG_MAX_LINES_PER_SESSION = 500  # 每会话上限(FIFO), 防大文本寄存失控


def record_orig_content(session_key, anchor, content, diag_dir=None):
    """寄存压缩前原文(锚点 → 原文)。fail-open。"""
    from session_ledger import sanitize_session_key
    from datetime import datetime as _dt
    d = diag_dir or getattr(_ps, "_DIAG_DIR", os.path.join("logs", "diag"))
    try:
        od = os.path.join(d, "orig")
        os.makedirs(od, exist_ok=True)
        path = os.path.join(od, sanitize_session_key(session_key)[:8] + ".jsonl")
        lines = []
        if os.path.exists(path):
            try:
                lines = open(path, encoding="utf-8").readlines()
            except OSError:
                lines = []
        lines.append(json.dumps({"anchor": anchor,
                                 "content": content,
                                 "ts": _dt.now().isoformat(timespec="seconds")},
                                ensure_ascii=False) + "\n")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-ORIG_MAX_LINES_PER_SESSION:])
    except (OSError, ValueError):
        pass


def read_orig_content(session_key, anchor, diag_dir=None):
    """按锚点读回压缩前原文; 未找到返回 None。"""
    from session_ledger import sanitize_session_key
    d = diag_dir or getattr(_ps, "_DIAG_DIR", os.path.join("logs", "diag"))
    path = os.path.join(d, "orig", sanitize_session_key(session_key)[:8] + ".jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            best = None
            for raw in f:
                try:
                    rec = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("anchor") == anchor:
                    best = rec  # 取最后一条(最新)
            if best is not None:
                return best.get("content")
    except OSError:
        pass
    return None


__all__ = ["ManifestStore", "MANIFEST", "record_dropped_messages",
           "MAX_LINES_PER_SESSION", "MANIFEST_MAX_SESSIONS", "MANIFEST_MAX_MB",
           "record_orig_content", "read_orig_content",
           "ORIG_MAX_LINES_PER_SESSION"]
