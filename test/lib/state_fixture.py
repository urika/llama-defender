#!/usr/bin/env python3
"""state_fixture.py — 测试状态夹具库（stdlib only）。

把散落在 12+ 个测试文件里的存储状态隔离/植入/清理逻辑收拢为共享夹具。
状态维度 taxonomy 见 test/README.md「状态矩阵」节。

四类能力:
  1. isolated_diag()     上下文管理器: _DIAG_DIR/_SESSIONS_PATH 等整体重定向 tmp
  2. new_sid(prefix)     唯一 8 字符安全会话键(R14 截断契约, 防撞生产数据)
  3. plant_session()     已知答案植入(manifest/archive/ledger, 供检索正交验证)
  4. scrub_sessions()    全局 sessions.jsonl 行过滤(测试足迹清理)
"""
import contextlib
import json
import os
import shutil
import tempfile
import time

# 会话键硬上限: R14 D5 约定 sanitize_session_key 截断 8 字符,
# 植入/清理的文件名必须与截断后键一致, 否则读到的是另一份文件
# (2026-09-01 集成测试实测踩坑: itestplant→itestpla)。
SID_MAX_LEN = 8


# ==================================================================
# 1. 隔离: 整体重定向 diag 存储
# ==================================================================

_DIAG_PATH_ATTRS = (
    "_DIAG_DIR",
    "_SESSIONS_PATH",
    "_SESSIONS_PATH_BAK",
)


class isolated_diag:
    """上下文管理器: 把 proxy_state 的 diag 存储路径重定向到临时目录。

    用法:
        with isolated_diag() as tmp:
            ...  # 被测代码写进 tmp/diag/..., 与生产 logs/diag 隔离
        # 退出时恢复原路径; tmp 目录默认保留供断言, 传 cleanup=True 自动删除

    覆盖属性: _DIAG_DIR(manifest/ledger/archive/index 根) 与
    _SESSIONS_PATH(sessions.jsonl 全局流)。属性缺失(未来改名)记入
    restored, 不假设存在——与 proxy_state 的松耦合。
    """

    def __init__(self, cleanup=False):
        self.cleanup_tmp = cleanup
        self.tmp = None
        self._saved = {}

    def __enter__(self):
        import proxy_state as _ps
        self.tmp = tempfile.mkdtemp(prefix="statefix_")
        os.makedirs(os.path.join(self.tmp, "diag"), exist_ok=True)
        self._saved = {}
        new_dir = os.path.join(self.tmp, "diag")
        for attr in _DIAG_PATH_ATTRS:
            if hasattr(_ps, attr):
                self._saved[attr] = getattr(_ps, attr)
                if attr == "_DIAG_DIR":
                    setattr(_ps, attr, new_dir)
                else:
                    setattr(_ps, attr, os.path.join(new_dir, os.path.basename(
                        str(self._saved[attr]) or "sessions.jsonl")))
        return new_dir

    def __exit__(self, *exc):
        import proxy_state as _ps
        for attr, val in self._saved.items():
            setattr(_ps, attr, val)
        if self.cleanup_tmp and self.tmp and os.path.isdir(self.tmp):
            shutil.rmtree(self.tmp, ignore_errors=True)
        return False


# ==================================================================
# 2. 会话键: 唯一 + 截断安全
# ==================================================================

_sid_counter = [0]


def new_sid(prefix="t"):
    """生成唯一且 ≤8 字符的会话键(避开 sanitize 截断与并行/生产撞键)。

    组成: prefix[:2] + 进程内单调计数(4 位 hex) + 随机 2 位 hex——
    计数位保证进程内严格唯一, 随机位降低跨进程同刻碰撞。
    """
    _sid_counter[0] += 1
    tail = "%04x%s" % (_sid_counter[0] % 0xFFFF, os.urandom(1).hex())
    return (prefix[:2] + tail)[:SID_MAX_LEN]


# ==================================================================
# 3. 植入: 已知答案会话状态(检索正交验证用)
# ==================================================================

def _default_diag_dir():
    """跟随 isolated_diag 重定向(与真实存储同源); 无 proxy_state 时退相对路径。"""
    try:
        import proxy_state as _ps
        return getattr(_ps, "_DIAG_DIR", os.path.join("logs", "diag"))
    except ImportError:
        return os.path.join("logs", "diag")


def plant_manifest_line(sid, turn, anchor, kind="tool_result", tool="",
                        head="", size_chars=100, reason="fifo_drop",
                        handle=None, diag_dir=None, ts=None):
    """追加一条 manifest 索引行。sid 会被截断对齐(与代理同规则)。"""
    from session_ledger import sanitize_session_key
    d = diag_dir or _default_diag_dir()
    os.makedirs(d, exist_ok=True)
    ts = ts or datetime_now()
    line = {"turn": turn, "reason": reason, "anchor": anchor, "kind": kind,
            "role": "user", "tool": tool, "handle": handle,
            "size_chars": size_chars, "head": head[:120], "ts": ts}
    man_dir = os.path.join(d, "manifest")
    os.makedirs(man_dir, exist_ok=True)
    path = os.path.join(man_dir,
                        sanitize_session_key(sid)[:SID_MAX_LEN] + ".jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def plant_archive_tool_result(sid, turn, tool_use_id, content, diag_dir=None,
                              ts=None):
    """写入一条 archive 记录(recover_full_content 的已知答案源)。

    content: tool_result 的正文文本; 内嵌为标准 payload 结构。
    """
    from session_ledger import sanitize_session_key
    d = diag_dir or _default_diag_dir()
    os.makedirs(d, exist_ok=True)
    ts = ts or datetime_now()
    payload = json.dumps({"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id,
         "content": [{"type": "text", "text": content}]}]}]},
        ensure_ascii=False)
    arc_dir = os.path.join(d, "archive")
    os.makedirs(arc_dir, exist_ok=True)
    path = os.path.join(arc_dir,
                        sanitize_session_key(sid)[:SID_MAX_LEN] + ".jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"turn": turn, "ts": ts, "payload": payload},
                           ensure_ascii=False) + "\n")


def plant_session(sid, manifest=None, archive=None, diag_dir=None):
    """一次植入完整已知答案会话。

    manifest: [{"turn","anchor","kind","tool","head",...}] (anchor r:xx 的
              会有配对 archive 记录, content 取 "content" 键)
    archive:  [{"turn","tool_use_id","content"}]
    返回植入文件路径列表(断言/清理用)。
    """
    from session_ledger import sanitize_session_key
    d = diag_dir or _default_diag_dir()
    key = sanitize_session_key(sid)[:SID_MAX_LEN]
    paths = []
    for m in manifest or []:
        plant_manifest_line(sid, diag_dir=d, **{
            k: v for k, v in m.items() if k != "content"})
    for a in archive or []:
        plant_archive_tool_result(sid, a["turn"], a["tool_use_id"],
                                  a["content"], diag_dir=d)
    for sub in ("manifest", "archive"):
        p = os.path.join(d, sub, key + ".jsonl")
        if os.path.exists(p):
            paths.append(p)
    return paths


def datetime_now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ==================================================================
# 4. 清理: 全局 sessions.jsonl 行过滤(3 份拷贝合一)
# ==================================================================

def scrub_sessions(sid, sessions_path=None, prefix_chars=400):
    """从 sessions.jsonl 移除含 sid 的行(测试足迹清理)。返回删除行数。"""
    path = sessions_path
    if path is None:
        import proxy_state as _ps
        path = getattr(_ps, "_SESSIONS_PATH",
                       os.path.join("logs", "diag", "sessions.jsonl"))
    try:
        lines = open(path, encoding="utf-8").readlines()
    except OSError:
        return 0
    kept = [l for l in lines if sid not in l[:prefix_chars]]
    removed = len(lines) - len(kept)
    if removed:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(kept)
    return removed


__all__ = [
    "isolated_diag", "new_sid", "SID_MAX_LEN",
    "plant_manifest_line", "plant_archive_tool_result", "plant_session",
    "scrub_sessions",
]
