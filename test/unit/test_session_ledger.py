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
        # A3: 台账持久化默认开启——测试一律重定向到临时目录,不污染 logs/
        self._tmp = tempfile.mkdtemp(prefix="ledger_persist_")
        self._saved_ledger_dir = _ps._DIAG_LEDGER_DIR
        self._saved_ledger_enabled = getattr(_ps, "PROXY_DIAG_LEDGER_ENABLED", True)
        _ps._DIAG_LEDGER_DIR = os.path.join(self._tmp, "ledger")
        _ps.PROXY_DIAG_LEDGER_ENABLED = True
        self.store = sl.LedgerStore()

    def tearDown(self):
        _ps.PROXY_DIAG_ENABLED = self._saved_enabled
        _ps._DIAG_LEDGER_DIR = self._saved_ledger_dir
        _ps.PROXY_DIAG_LEDGER_ENABLED = self._saved_ledger_enabled
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

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
            # A3 新契约: 内存驱逐 ≠ 档案删除——被逐会话有台账档案仍可查
            self.assertIsNotNone(self.store.build_ledger_json("sk0"))
            self.assertIsNotNone(self.store.evicted_at("sk0"))            # 410 语义保留
            self.assertIsNotNone(self.store.build_ledger_json("sk5"))     # 最新保留
            # 档案也被清理(磁盘 cap 删除/未启用落盘)后 → None → 端点走 410
            os.remove(os.path.join(_ps._DIAG_LEDGER_DIR, "sk0.jsonl"))
            self.assertIsNone(self.store.build_ledger_json("sk0"))
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


class TestLedgerPersistence(unittest.TestCase):
    """A3(2026-08-20): 台账增量落盘 + 跨重启/驱逐后 R14 端点档案兜底。

    契约验收(logging-trajectory-improvement-design §3.1): 代理重启/TTL 驱逐后
    build_ledger_json 仍可从 logs/diag/ledger/<sid>.jsonl 重建——agent_go 轮级
    看门狗不失忆;内存驱逐 ≠ 档案删除。
    """

    def setUp(self):
        self._saved_enabled = _ps.PROXY_DIAG_ENABLED
        _ps.PROXY_DIAG_ENABLED = True
        self._tmp = tempfile.mkdtemp(prefix="ledger_a3_")
        self._saved_dir = _ps._DIAG_LEDGER_DIR
        self._saved_ledger_enabled = getattr(_ps, "PROXY_DIAG_LEDGER_ENABLED", True)
        _ps._DIAG_LEDGER_DIR = os.path.join(self._tmp, "ledger")
        _ps.PROXY_DIAG_LEDGER_ENABLED = True

    def tearDown(self):
        _ps.PROXY_DIAG_ENABLED = self._saved_enabled
        _ps._DIAG_LEDGER_DIR = self._saved_dir
        _ps.PROXY_DIAG_LEDGER_ENABLED = self._saved_ledger_enabled
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _path(self, key):
        return os.path.join(_ps._DIAG_LEDGER_DIR, sl.sanitize_session_key(key) + ".jsonl")

    def test_persist_and_rebuild_after_restart(self):
        store = sl.LedgerStore()
        msgs1 = [_tu("q"), _assistant_tool("WebSearch", {"query": "same q"}, "t1")]
        store.record_request("sA", msgs1, 1, key_source="header")
        # 第二请求: 前缀一致 + 新增 tool_result 回填 + 新 dup action(跨请求回填路径)
        msgs2 = msgs1 + [_tool_result("t1", "x" * 42),
                         _assistant_tool("WebSearch", {"query": "same q"}, "t2")]
        store.record_request("sA", msgs2, 2, key_source="header")
        self.assertTrue(os.path.isfile(self._path("sA")))
        with open(self._path("sA")) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 2)
        # 第二行: 1 个新增 action + 1 条跨请求回填
        self.assertEqual(len(lines[1]["actions"]), 1)
        self.assertEqual(len(lines[1]["filled"]), 1)
        self.assertEqual(lines[1]["filled"][0]["result_chars"], 42)
        # "重启": 新 LedgerStore 实例(内存空) → 档案兜底重建
        store2 = sl.LedgerStore()
        led = store2.build_ledger_json("sA")
        self.assertIsNotNone(led)
        self.assertEqual(len(led["actions"]), 2)
        self.assertTrue(all(a.get("result_chars") == 42 for a in led["actions"]
                            if a.get("dup") == 1))
        # dup 派生正确(同 tool+target_hash → count=2)
        self.assertEqual(len(led["dup_queries"]), 1)
        self.assertEqual(led["dup_queries"][0]["count"], 2)
        self.assertEqual(led["key_source"], "header")
        self.assertEqual(led["turns_seen"], 2)

    def test_evicted_session_serves_from_file(self):
        store = sl.LedgerStore()
        msgs = [_tu("q"), _assistant_tool("Read", {"file_path": "/a.py"}, "t1")]
        store.record_request("sB", msgs, 1)
        # 模拟 TTL/FIFO 驱逐: 从内存移除并记 evicted(410 语义)
        with store._lock:
            del store._sessions["sB"]
            store._evicted["sB"] = "2026-08-20T00:00:00"
        # 档案仍在 → 端点 200(内存驱逐 ≠ 档案删除),不因 evicted 变 None
        led = store.build_ledger_json("sB")
        self.assertIsNotNone(led)
        self.assertEqual(len(led["actions"]), 1)

    def test_disabled_writes_no_file(self):
        _ps.PROXY_DIAG_LEDGER_ENABLED = False
        store = sl.LedgerStore()
        msgs = [_tu("q"), _assistant_tool("Bash", {"command": "ls"}, "t1")]
        store.record_request("sC", msgs, 1)
        self.assertFalse(os.path.exists(self._path("sC")))
        # 内存路径不受影响
        self.assertIsNotNone(store.build_ledger_json("sC"))

    def test_mismatch_line_rebuilds_state(self):
        store = sl.LedgerStore()
        msgs1 = [_tu("q1"), _assistant_tool("Read", {"file_path": "/old.py"}, "t1")]
        store.record_request("sD", msgs1, 1)
        # 客户端裁剪: 前缀变化 → mismatch 全量重建(旧 action 不留)
        msgs2 = [_tu("q2"), _assistant_tool("Read", {"file_path": "/new.py"}, "t2")]
        store.record_request("sD", msgs2, 2)
        store2 = sl.LedgerStore()
        led = store2.build_ledger_json("sD")
        self.assertIsNotNone(led)
        tools = [a["target"] for a in led["actions"]]
        self.assertEqual(tools, ["/new.py"])
        self.assertEqual(led["canonical_mismatch_count"], 1)

    def test_unknown_session_no_file_returns_none(self):
        store = sl.LedgerStore()
        self.assertIsNone(store.build_ledger_json("never_seen"))


class TestResolveSessionKey(unittest.TestCase):
    """resolve_session_key — DEF-309 查询 key → 存储 key 解析。

    背景: 引擎 key 自 DEF-309 起为 header sid 全量,[:8] 仅日志显示;
    消费方(harness run_agent/analyze/status)仍持 sid[:8] 查询,需前缀
    唯一解析;截断时代 8 字符存量走旧形式精确命中。
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="resolve_key_")
        self._saved = (_ps._DIAG_LEDGER_DIR, _ps._DIAG_ARCHIVE_DIR, _ps._DIAG_DIR)
        _ps._DIAG_LEDGER_DIR = os.path.join(self._tmp, "ledger")
        _ps._DIAG_ARCHIVE_DIR = os.path.join(self._tmp, "archive")
        _ps._DIAG_DIR = self._tmp
        os.makedirs(_ps._DIAG_LEDGER_DIR, exist_ok=True)

    def tearDown(self):
        _ps._DIAG_LEDGER_DIR, _ps._DIAG_ARCHIVE_DIR, _ps._DIAG_DIR = self._saved

    @staticmethod
    def _touch(d, name):
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, name), "w").close()

    def test_exact_full_key_hit(self):
        full = "s38beef8ab-instance_ansible__ansible-1a4644ff15355f"
        self._touch(_ps._DIAG_LEDGER_DIR, sl.sanitize_session_key(full) + ".jsonl")
        self.assertEqual(sl.resolve_session_key(full), full)

    def test_short_query_resolves_unique_full_key(self):
        full = "s38beef8ab-instance_ansible__ansible-1a4644ff15355f"
        self._touch(_ps._DIAG_LEDGER_DIR, sl.sanitize_session_key(full) + ".jsonl")
        self.assertEqual(sl.resolve_session_key(full[:8]), full)

    def test_aux_sidecar_merges_into_main_stem(self):
        full = "s38beef8ab-instance_ansible__ansible-1a4644ff15355f"
        self._touch(_ps._DIAG_ARCHIVE_DIR, sl.sanitize_session_key(full) + ".jsonl")
        self._touch(_ps._DIAG_ARCHIVE_DIR,
                    sl.sanitize_session_key(full) + "__aux-haiku.jsonl")
        self.assertEqual(sl.resolve_session_key(full[:8]), full)

    def test_ambiguous_prefix_fails_closed(self):
        a = "s38beef8ab-instance_one"
        b = "s38beef8ab-instance_two"
        self._touch(_ps._DIAG_LEDGER_DIR, sl.sanitize_session_key(a) + ".jsonl")
        self._touch(_ps._DIAG_LEDGER_DIR, sl.sanitize_session_key(b) + ".jsonl")
        # 歧义 → 原样返回(端点层 404),不误并
        self.assertEqual(sl.resolve_session_key("s38beef8ab"), "s38beef8ab")

    def test_legacy_short_store_hit(self):
        # 截断时代存量: 磁盘名就是 8 字符(manifest 目录派生自 _DIAG_DIR)
        self._touch(os.path.join(_ps._DIAG_DIR, "manifest"), "s38beef8.jsonl")
        self.assertEqual(sl.resolve_session_key("s38beef8"), "s38beef8")

    def test_legacy_full_header_query_falls_back_to_short_store(self):
        # 旧会话(存储名 8 字符) + 新客户端持全量 sid 重连 → 回落 [:8] 命中
        self._touch(_ps._DIAG_LEDGER_DIR, "s38beef8.jsonl")
        full = "s38beef8ab-instance_ansible__ansible-1a4644ff15355f"
        self.assertEqual(sl.resolve_session_key(full), "s38beef8")

    def test_unknown_key_unchanged(self):
        self.assertEqual(sl.resolve_session_key("cli_2566"), "cli_2566")

    def test_stale_short_archive_does_not_shadow_live_full_session(self):
        # EXP-2R 重跑实测回归: archive 里截断时代的 8 字符旧档 + 活跃全 key
        # 会话并存时, 8 字符查询必须解析到活跃会话(ledger 前缀), 而非被
        # archive 精确命中短路——否则 harness ?since 拉取 404, 机制证据丢失
        full = "s38beef8a4f-instance_ansible__ansible-1a4644ff15355f"
        self._touch(_ps._DIAG_ARCHIVE_DIR, "s38beef8.jsonl")
        self._touch(_ps._DIAG_LEDGER_DIR, sl.sanitize_session_key(full) + ".jsonl")
        self.assertEqual(sl.resolve_session_key("s38beef8"), full)


if __name__ == "__main__":
    unittest.main()
