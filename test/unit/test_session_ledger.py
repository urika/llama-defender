"""Unit tests for session_ledger.py — R14 台账 / R15 档案。

Covers: 规范化（dup 判定基础）、增量扫描与前缀失配重建（canonical_mismatch）、
dup/last_dup_turn 派生、材料启发式、TTL/FIFO 驱逐（410 语义）、sent_view 落盘
与读取（截断保护）。
设计依据: docs/02-architecture-design/diagnostics-dataplane-design-20260819.md
"""
import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import proxy_state as _ps
import session_ledger as sl


class TestNormalizeTarget(unittest.TestCase):
    """normalize_target — 确定性规范化（设计 D3）。"""

    def test_search_query_normalized(self):
        t1, h1 = sl.normalize_target("WebSearch", {"query": "Foo  BAR baz"})
        t2, h2 = sl.normalize_target("WebSearch", {"query": "foo bar   BAZ"})
        self.assertEqual(t1, "foo bar baz")
        self.assertEqual(h1, h2)  # 大小写/空白差异 → 同一 dup 组

    def test_read_path(self):
        t, h = sl.normalize_target("Read", {"file_path": "/a/b/c.py"})
        self.assertEqual(t, "/a/b/c.py")

    def test_url_strips_query(self):
        t1, h1 = sl.normalize_target("fetch", {"url": "https://x.com/a?ts=1"})
        t2, h2 = sl.normalize_target("fetch", {"url": "https://x.com/a?ts=2"})
        self.assertEqual(t1, "https://x.com/a")
        self.assertEqual(h1, h2)  # 查询串抖动不算 dup 差异

    def test_different_tools_same_target_differ(self):
        _, h1 = sl.normalize_target("Read", {"file_path": "/a"})
        _, h2 = sl.normalize_target("Edit", {"file_path": "/a"})
        self.assertNotEqual(h1, h2)

    def test_sanitize_session_key(self):
        self.assertEqual(sl.sanitize_session_key("cli_AB12"), "cli_AB12")
        self.assertEqual(sl.sanitize_session_key("../etc/passwd"), "___etc_passwd")
        self.assertEqual(sl.sanitize_session_key(""), "_anon")


def _tu(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_tool(tool, args, tid):
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": tool, "input": args}]}


def _tool_result(tid, content):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content}]}


class TestLedgerStore(unittest.TestCase):
    """增量扫描 / dup 派生 / 材料启发式 / 驱逐。"""

    def setUp(self):
        self._saved_enabled = _ps.PROXY_DIAG_ENABLED
        _ps.PROXY_DIAG_ENABLED = True
        self.store = sl.LedgerStore()

    def tearDown(self):
        _ps.PROXY_DIAG_ENABLED = self._saved_enabled

    def test_incremental_append(self):
        msgs1 = [_tu("hi"), _assistant_tool("WebSearch", {"query": "ansible 80376"}, "t1")]
        self.assertFalse(self.store.record_request("s1", msgs1, 1))
        msgs2 = msgs1 + [_tool_result("t1", "results..."),
                         _assistant_tool("WebSearch", {"query": "ansible 80376"}, "t2")]
        self.assertFalse(self.store.record_request("s1", msgs2, 2))
        ledger = self.store.build_ledger_json("s1")
        self.assertEqual(ledger["turns_seen"], 2)
        self.assertEqual(len(ledger["actions"]), 2)
        # 同一规范化查询出现两次 → dup 派生
        self.assertEqual(len(ledger["dup_queries"]), 1)
        self.assertEqual(ledger["dup_queries"][0]["count"], 2)
        # action 级 dup: 第二次出现 dup=2
        dup_vals = sorted(a["dup"] for a in ledger["actions"])
        self.assertEqual(dup_vals, [1, 2])
        # result_chars 已配对（第一条），第二条待 result 为 None
        chars = [a["result_chars"] for a in ledger["actions"]]
        self.assertIn(len("results..."), chars)
        self.assertIn(None, chars)

    def test_prefix_mismatch_rebuilds(self):
        msgs1 = [_tu("v1 original"), _assistant_tool("Read", {"file_path": "/a"}, "t1")]
        self.store.record_request("s2", msgs1, 1)
        # 客户端侧裁剪（模拟 /compact）：前缀变化 → 全量重建 + canonical_mismatch
        msgs2 = [_tu("v1 rewritten"), _assistant_tool("Read", {"file_path": "/a"}, "t1")]
        self.assertTrue(self.store.record_request("s2", msgs2, 2))
        ledger = self.store.build_ledger_json("s2")
        self.assertEqual(ledger["canonical_mismatch_count"], 1)
        # 重建后 actions 不重复
        self.assertEqual(len(ledger["actions"]), 1)

    def test_materials_from_write_edit(self):
        msgs = [
            _tu("go"),
            _assistant_tool("Write", {"file_path": "work/fix.py"}, "t1"),
            _tool_result("t1", "File written successfully"),
            _assistant_tool("Edit", {"file_path": "src/main.py"}, "t2"),
            _tool_result("t2", "ok"),
        ]
        self.store.record_request("s3", msgs, 1)
        ledger = self.store.build_ledger_json("s3")
        paths = {m["path"] for m in ledger["materials"]}
        self.assertEqual(paths, {"work/fix.py", "src/main.py"})

    def test_materials_from_bash_output(self):
        msgs = [
            _tu("go"),
            _assistant_tool("Bash", {"command": "cp a b"}, "t1"),
            _tool_result("t1", "saved output/to/backup.tar.gz\n1 file created"),
        ]
        self.store.record_request("s4", msgs, 1)
        ledger = self.store.build_ledger_json("s4")
        paths = {m["path"] for m in ledger["materials"]}
        self.assertIn("output/to/backup.tar.gz", paths)

    def test_unknown_session_returns_none(self):
        self.assertIsNone(self.store.build_ledger_json("nope"))

    def test_fifo_eviction_and_410(self):
        saved_max = _ps.PROXY_DIAG_SESSION_MAX
        try:
            _ps.PROXY_DIAG_SESSION_MAX = 4
            for i in range(6):
                self.store.record_request(f"sk{i}", [_tu(f"m{i}")], 1)
            listing = self.store.list_sessions()
            self.assertEqual(len(listing["sessions"]), 4)
            self.assertIsNone(self.store.build_ledger_json("sk0"))       # 最老被逐
            self.assertIsNotNone(self.store.evicted_at("sk0"))            # 410 语义
            self.assertIsNotNone(self.store.build_ledger_json("sk5"))     # 最新保留
        finally:
            _ps.PROXY_DIAG_SESSION_MAX = saved_max

    def test_ttl_eviction(self):
        import time as _time
        entry_time = _time.time() - 999 * 60  # 远超默认 180min TTL
        self.store.record_request("skTTL", [_tu("x")], 1)
        with self.store._lock:
            self.store._sessions["skTTL"]["last_seen"] = entry_time
        self.store.record_request("skOther", [_tu("y")], 1)  # 触发 sweep
        self.assertFalse(self.store.session_alive("skTTL"))
        self.assertIsNotNone(self.store.evicted_at("skTTL"))

    def test_actions_soft_cap(self):
        saved = sl.MAX_ACTIONS_PER_SESSION
        try:
            sl.MAX_ACTIONS_PER_SESSION = 20
            msgs = [_assistant_tool(f"Bash", {"command": f"cmd{i}"}, f"t{i}")
                    for i in range(40)]
            self.store.record_request("sCap", msgs, 1)
            ledger = self.store.build_ledger_json("sCap")
            self.assertLess(len(ledger["actions"]), 40)
            self.assertGreater(ledger["aggregated_dropped"], 0)
        finally:
            sl.MAX_ACTIONS_PER_SESSION = saved

    def test_limit_turns_filter(self):
        for turn in range(1, 5):
            self.store.record_request("sL", [_assistant_tool("Bash", {"command": f"c{turn}"}, f"t{turn}")], turn)
        ledger = self.store.build_ledger_json("sL", limit_turns=2)
        turns = {a["turn"] for a in ledger["actions"]}
        self.assertTrue(turns.issuperset({4}))
        self.assertNotIn(1, turns)


class TestArchiveStore(unittest.TestCase):
    """sent_view 落盘 / 读取 / 截断保护（R15）。"""

    def setUp(self):
        self._saved_enabled = _ps.PROXY_DIAG_ENABLED
        self._saved_archive = _ps.PROXY_DIAG_ARCHIVE_ENABLED
        _ps.PROXY_DIAG_ENABLED = True
        _ps.PROXY_DIAG_ARCHIVE_ENABLED = True
        self._tmp = tempfile.mkdtemp(prefix="ledger_archive_")
        self._saved_dir = _ps._DIAG_ARCHIVE_DIR
        _ps._DIAG_ARCHIVE_DIR = os.path.join(self._tmp, "archive")
        self.archive = sl.ArchiveStore()

    def tearDown(self):
        _ps.PROXY_DIAG_ENABLED = self._saved_enabled
        _ps.PROXY_DIAG_ARCHIVE_ENABLED = self._saved_archive
        _ps._DIAG_ARCHIVE_DIR = self._saved_dir

    def test_append_and_index_read(self):
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        self.archive.append_turn("sA", 1, payload, ["loop_l1"], {"model": "m"})
        self.archive.append_turn("sA", 2, payload, [], {"model": "m"})
        result, err = self.archive.read("sA")
        self.assertIsNone(err)
        self.assertEqual(result["total"], 2)
        # 索引模式默认不含 payload
        self.assertNotIn("payload", result["turns"][0])
        self.assertEqual(result["turns"][0]["injections"], ["loop_l1"])

    def test_read_with_payload(self):
        payload = {"model": "m", "messages": []}
        self.archive.append_turn("sB", 3, payload, [], {"model": "m"})
        result, _ = self.archive.read("sB", include_payload=True)
        self.assertIn("payload", result["turns"][0])
        self.assertEqual(json.loads(result["turns"][0]["payload"]), payload)

    def test_turn_filter(self):
        self.archive.append_turn("sC", 1, {}, [], {})
        self.archive.append_turn("sC", 2, {}, [], {})
        result, _ = self.archive.read("sC", turn=2)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["turns"][0]["turn"], 2)

    def test_missing_session_not_found(self):
        _, err = self.archive.read("nope")
        self.assertEqual(err, "not_found")

    def test_has_archive(self):
        """G-D: 端点 key 归并判据——有档案文件返回 True。"""
        self.assertFalse(self.archive.has_archive("sH"))
        self.archive.append_turn("sH", 1, {"a": 1}, [], {})
        self.assertTrue(self.archive.has_archive("sH"))
        self.assertFalse(self.archive.has_archive("other"))

    def test_payload_truncation(self):
        saved = sl.MAX_ARCHIVE_PAYLOAD_CHARS
        try:
            sl.MAX_ARCHIVE_PAYLOAD_CHARS = 100
            big = {"messages": ["x" * 500]}
            self.archive.append_turn("sD", 1, big, [], {})
            result, _ = self.archive.read("sD", include_payload=True)
            self.assertTrue(result["turns"][0]["payload_truncated"])
        finally:
            sl.MAX_ARCHIVE_PAYLOAD_CHARS = saved

    def test_disabled_archive_noop(self):
        _ps.PROXY_DIAG_ARCHIVE_ENABLED = False
        self.archive.append_turn("sE", 1, {"a": 1}, [], {})
        _, err = self.archive.read("sE")
        self.assertEqual(err, "not_found")


if __name__ == "__main__":
    unittest.main()
