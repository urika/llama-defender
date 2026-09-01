#!/usr/bin/env python3
"""test_ctx_recall.py — R10.2 召回核心测试(FTS5 trigram/中文/短查询降级/kind 过滤)。"""
import os
import shutil
import tempfile
import threading
import unittest

import proxy_state as _ps
from test.lib import state_fixture as sf

import ctx_recall as cr
import memory_stores as ms
import ifc_metrics


def _seed(session_key):
    msgs = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/src/login/auth.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "def login()..." * 50}]}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "WebSearch",
             "input": {"query": "中文检索测试关键词"}}]},
        {"role": "user", "content": [
            {"type": "text", "text": "早期架构决策记录" + "y" * 200}]},
    ]
    ms.record_dropped_messages(session_key, turn=5, reason="fifo_drop", messages=msgs)


class TestCtxRecall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ifc_recall_")
        self._orig_dir = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._tmp
        ms.MANIFEST.reset()
        _seed("sess_r1")

    def tearDown(self):
        _ps._DIAG_DIR = self._orig_dir
        ms.MANIFEST.reset()

    def test_fts_path_query_hits_tool_line(self):
        hits = cr.lookup("sess_r1", "auth.py")
        self.assertTrue(hits)
        self.assertTrue(any(l["anchor"] == "u:t1" for l in hits))

    def test_fts_chinese_trigram(self):
        # 4 字中文查询走 trigram 全文索引
        hits = cr.lookup("sess_r1", "中文检索")
        self.assertTrue(any(l["tool"] == "WebSearch" for l in hits))

    def test_short_query_falls_back_to_substring(self):
        # 2 字中文 < trigram 下限 → L1 内存子串降级(handle value 含该词)
        hits = cr.lookup("sess_r1", "中文")
        self.assertTrue(any(l["tool"] == "WebSearch" for l in hits))

    def test_kind_filter(self):
        hits = cr.lookup("sess_r1", "auth.py", kind="tool_use")
        self.assertTrue(hits)
        self.assertTrue(all(l["kind"] == "tool_use" for l in hits))
        none = cr.lookup("sess_r1", "auth.py", kind="text")
        self.assertEqual(none, [])

    def test_no_match_returns_empty(self):
        self.assertEqual(cr.lookup("sess_r1", "zzz_not_there"), [])
        self.assertEqual(cr.lookup("sess_unknown", "auth.py"), [])

    def test_format_result_both_branches(self):
        hits = cr.lookup("sess_r1", "auth.py")
        text = cr.format_recall_result(hits, "auth.py")
        self.assertIn("u:t1", text)
        self.assertIn("fifo_drop", text)
        empty = cr.format_recall_result([], "q")
        self.assertIn("无匹配", empty)

    def test_index_file_created_and_stale_rebuild(self):
        cr.lookup("sess_r1", "auth.py")
        db = os.path.join(self._tmp, "index", "sess_r1.db")
        self.assertTrue(os.path.exists(db))
        # manifest 增量后行数不一致 → 下次查询自动重建
        ms.record_dropped_messages("sess_r1", turn=6, reason="fifo_drop", messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t9", "name": "Grep",
                 "input": {"pattern": "TODO marker"}}]}])
        hits = cr.lookup("sess_r1", "TODO")
        self.assertTrue(any(l["anchor"] == "u:t9" for l in hits))

    def test_text_unit_recallable_by_content(self):
        # 被丢弃的动机文本经 head 摘录可按内容词找回(2 字短查询走子串降级)
        ms.record_dropped_messages("sess_r1", turn=7, reason="fifo_drop", messages=[
            {"role": "user", "content": [
                {"type": "text", "text": "架构决策:放弃 JWT 改用 session" + "z" * 200}]}])
        short = cr.lookup("sess_r1", "JWT")   # 3 字符 → FTS
        self.assertTrue(any(l["kind"] == "text" for l in short))
        cjk = cr.lookup("sess_r1", "架构决策")  # 中文 4 字 → trigram
        self.assertTrue(any(l["kind"] == "text" for l in cjk))

    def test_tool_schema_shape(self):
        self.assertEqual(cr.TOOL_SCHEMA["name"], "ctx_recall")
        self.assertIn("query", cr.TOOL_SCHEMA["input_schema"]["properties"])
        self.assertEqual(cr.TOOL_SCHEMA["input_schema"]["required"], ["query"])




class TestQueryTokenization(unittest.TestCase):
    """自然语言查询 token 化 + 多级检索策略(2026-09-01 欠拉根因修复)。"""

    def setUp(self):
        self._diag = tempfile.mkdtemp(prefix="qrt_")
        self._orig = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._diag
        ms.MANIFEST.reset()

    def tearDown(self):
        _ps._DIAG_DIR = self._orig
        ms.MANIFEST.reset()
        shutil.rmtree(self._diag, ignore_errors=True)

    def test_query_tokens_split_and_filter(self):
        toks = cr._query_tokens("openlibrary/core/lists/model.py Seed class List")
        self.assertIn("openlibrary", toks)
        self.assertIn("model", toks)
        self.assertIn("seed", toks)
        self.assertNotIn("py", toks)  # <3 字符被滤

    def test_token_aggregation_hits_content_rows(self):
        """多 token 查询应命中 r: 内容行(非 u: 自引用洪泛)。"""
        # 植入: 30 条自引用 u: 行(洪泛) + 1 条 r: 内容行
        for i in range(30):
            ms.MANIFEST.record_units(
                "qrt-flood", i + 1, "fifo_drop",
                [{"anchor": f"u:call_f{i}", "kind": "tool_use", "role": "assistant",
                  "tool": "ctx_recall", "handle": {"type": "query", "value": "alpha beta"},
                  "size_chars": 60, "head": "ctx_recall alpha beta"}])
        ms.MANIFEST.record_units(
            "qrt-flood", 99, "fifo_drop",
            [{"anchor": "r:call_content", "kind": "tool_result", "role": "user",
              "tool": "", "handle": None, "size_chars": 3000,
              "head": "alpha implementation beta details deep content"}])
        lines = cr.lookup("qrt-flood", "alpha beta", limit=5)
        kinds = [l.get("kind") for l in lines]
        self.assertIn("tool_result", kinds, "r: 内容行必须优先命中")
        self.assertEqual(lines[0]["kind"], "tool_result", "首行应为内容行")
        self.assertNotEqual(lines[0]["anchor"], "u:call_f0", "首行不得为自引用行")

    def test_triggers_field_indexed(self):
        """§3.1: 正文深处的指称性实体(>240 区间)经 triggers 字段可检索。"""
        msg = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ct1",
             "content": [{"type": "text",
                          "text": "A" * 400
                                  + " /var/log/session_9aa.json crashed "
                                  + "B" * 50}]}]}
        units = ifc_metrics.unit_anchors(msg)
        self.assertTrue(units[0].get("triggers"), "triggers 字段缺失")
        ms.MANIFEST.record_units("qt-s", 1, "fifo_drop", units)
        lines = cr.lookup("qt-s", "session_9aa.json", limit=5)
        self.assertTrue(any(l.get("anchor") == units[0]["anchor"] for l in lines))

    def test_phrase_miss_falls_to_tokens(self):
        """整句短语 miss(自然语言描述) → token 聚合命中。"""
        ms.MANIFEST.record_units(
            "qrt-desc", 1, "fifo_drop",
            [{"anchor": "r:d1", "kind": "tool_result", "role": "user",
              "tool": "", "handle": None, "size_chars": 500,
              "head": "the QuickBrownFox jumps over lazy dogs"}])
        # 查询是描述性变体, 非逐字子串
        lines = cr.lookup("qrt-desc", "quickbrownfox jumps", limit=5)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["anchor"], "r:d1")

    def test_total_miss_returns_empty(self):
        ms.MANIFEST.record_units(
            "qrt-none", 1, "fifo_drop",
            [{"anchor": "r:x1", "kind": "tool_result", "role": "user",
              "tool": "", "handle": None, "size_chars": 10, "head": "totally unrelated"}])
        self.assertEqual(cr.lookup("qrt-none", "zzzqqqxxx", limit=5), [])




class TestReviewFixes(unittest.TestCase):
    """ctx_recall review 修复(2026-09-01): kind 提示一致/head 240 端到端/总量护栏。"""

    def test_follow_up_hint_matches_schema_enum(self):
        import inspect
        src = inspect.getsource(cr.build_follow_up_messages)
        self.assertNotIn("file_edit", src)
        self.assertIn("tool_use|tool_result|text", src)

    def test_head_240_survives_manifest_write(self):
        """端到端: unit_anchors head 240 不被 manifest 写入层 [:120] 抵消。"""
        body = "A" * 200 + "QUANTUMUNIQUE" + "B" * 30
        msg = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ch1",
             "content": [{"type": "text", "text": body}]}]}
        ms.MANIFEST.reset()
        try:
            ms.record_dropped_messages("s-hd", 1, "fifo_drop", [msg])
            line = ms.MANIFEST.lines("s-hd")[0]
            self.assertIn("QUANTUMUNIQUE", line["head"])
        finally:
            ms.MANIFEST.reset()

    def test_result_total_cap(self):
        tmp = tempfile.mkdtemp(prefix="cap_")
        orig = _ps._DIAG_DIR
        _ps._DIAG_DIR = tmp
        try:
            from test.lib import state_fixture as sf
            for i in range(8):
                sf.plant_archive_tool_result("s-cap", turn=1,
                                             tool_use_id="c%d" % i,
                                             content="R" * 5000, diag_dir=tmp)
                sf.plant_manifest_line("s-cap", turn=1, anchor="r:c%d" % i,
                                       head="hit marker", diag_dir=tmp)
            lines = [{"turn": 1, "kind": "tool_result", "tool": "",
                      "handle": None, "anchor": "r:c%d" % i,
                      "reason": "fifo_drop", "size_chars": 5000,
                      "head": "hit marker"} for i in range(8)]
            r = cr.format_recall_result(lines, "q", "s-cap")
            self.assertLessEqual(len(r), cr.RESULT_TOTAL_MAX_CHARS + 120)
            self.assertIn("已截断", r)
        finally:
            _ps._DIAG_DIR = orig
            shutil.rmtree(tmp, ignore_errors=True)




class TestAdversarialQueries(unittest.TestCase):
    """对抗性查询——模型可发出任意字符串, 检索不得崩溃或静默降级为空。"""

    def setUp(self):
        self._diag = tempfile.mkdtemp(prefix="qa_")
        self._orig = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._diag
        ms.MANIFEST.reset()
        ms.MANIFEST.record_units(
            "qa-s", 1, "fifo_drop",
            [{"anchor": "r:qa1", "kind": "tool_result", "role": "user",
              "tool": "", "handle": None, "size_chars": 100,
              "head": "readme contains FTS operators NEAR( OR NOT seed data"}])
        self._queries_must_not_raise = [
            'NEAR(seed OR "unclosed',      # FTS5 操作符注入
            '"unclosed quote',             # 未闭合双引号
            "seed NOT (a OR b)",           # 操作符词
            "*", "?", "^:",                # 通配/前缀符
            "x" * 10000,                   # 超长
            "seed 🎉 emoji 中文混排",  # emoji/CJK
            "   ",                         # 空白
            "a",                           # 单字符(走 L1)
        ]

    def tearDown(self):
        _ps._DIAG_DIR = self._orig
        ms.MANIFEST.reset()
        shutil.rmtree(self._diag, ignore_errors=True)

    def test_adversarial_queries_never_raise(self):
        for q in self._queries_must_not_raise:
            try:
                lines = cr.lookup("qa-s", q, limit=5)
                self.assertIsInstance(lines, list)
            except Exception as e:
                self.fail(f"query {q[:30]!r} raised {e!r}")

    def test_operator_injection_still_finds_verbatim(self):
        # 真实词查询在操作符噪声旁仍工作
        lines = cr.lookup("qa-s", "seed", limit=5)
        self.assertEqual(len(lines), 1)


class TestQueryParaphraseBattery(unittest.TestCase):
    """查询改述组: 已知植入单元 × 模型风格查询 → 锁定必中/已知限制。

    背景: 31 次 0 命中事故的本质是自然语言改述 miss。本 battery 把
    "哪些说法必须能查到"固化为回归契约。
    """

    def setUp(self):
        self._diag = tempfile.mkdtemp(prefix="qb_")
        self._orig = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._diag
        ms.MANIFEST.reset()
        content = ("1	from openlibrary.core.lists.model import Seed\n"
                   "2\tclass List:\n" + "x" * 200)
        ms.MANIFEST.record_units(
            "qb-s", 42, "fifo_drop",
            [{"anchor": "r:call_list42", "kind": "tool_result", "role": "user",
              "tool": "", "handle": None, "size_chars": 32355,
              "head": content}])

    def tearDown(self):
        _ps._DIAG_DIR = self._orig
        ms.MANIFEST.reset()
        shutil.rmtree(self._diag, ignore_errors=True)

    def _hit(self, q):
        lines = cr.lookup("qb-s", q, limit=5)
        return any(l.get("anchor") == "r:call_list42" for l in lines)

    def test_must_hit_queries(self):
        # 每条 = 一次真实事故场景或模型实测查询风格; miss 即回归
        must_hit = [
            "openlibrary/core/lists/model.py",   # 完整路径(逐字子串)
            "lists/model.py",                    # 部分路径
            "Seed class List",                   # 模型实测查询(31 次事故原句)
            "lists model seed",                  # 空格分词小写
            "r:call_list42",                     # 锚点直查
            "from openlibrary.core.lists.model import Seed",  # 代码行逐字
        ]
        for q in must_hit:
            self.assertTrue(self._hit(q), f"必中查询 miss: {q!r}")

    def test_anchor_direct_query(self):
        """锚点直查: tool description 的承诺必须有实现。"""
        self.assertTrue(self._hit("r:call_list42"))
        # 不存在的锚点 → 空而非崩
        self.assertFalse(self._hit("r:ghost_anchor"))

    def test_known_limitation_queries(self):
        # 概念词不在 head 文本中 → 当前确定性管线已知限制(gist 阶段解决)
        self.assertFalse(self._hit("annotate public notes"), "gist 落地后此断言需翻转")


class TestConcurrencyAndLatency(unittest.TestCase):
    """并发查询 + 折叠期重建 + 延迟预算(PDC §5 护栏)。"""

    def setUp(self):
        self._diag = tempfile.mkdtemp(prefix="qc_")
        self._orig = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._diag
        ms.MANIFEST.reset()
        # 2000 行真实量级 manifest(生产实测 1081-3133 行)
        for batch in range(40):
            units = [{"anchor": f"r:c{batch}_{i}", "kind": "tool_result",
                      "role": "user", "tool": "Read", "handle": None,
                      "size_chars": 500,
                      "head": f"file content batch{batch} item{i} lists model seed"} for i in range(50)]
            ms.MANIFEST.record_units("qc-s", batch + 1, "fifo_drop", units)

    def tearDown(self):
        _ps._DIAG_DIR = self._orig
        ms.MANIFEST.reset()
        shutil.rmtree(self._diag, ignore_errors=True)

    def test_concurrent_queries_with_live_drops(self):
        errors = []
        def querier():
            try:
                for i in range(25):
                    lines = cr.lookup("qc-s", f"lists model item{i % 50}", limit=5)
                    self.assertIsInstance(lines, list)
            except Exception as e:
                errors.append(e)
        def dropper():
            try:
                for b in range(40, 55):
                    ms.MANIFEST.record_units("qc-s", b + 1, "fifo_drop",
                        [{"anchor": f"r:late{b}_{i}", "kind": "tool_result",
                          "role": "user", "tool": "Read", "handle": None,
                          "size_chars": 100,
                          "head": f"late drop {b} {i} lists seed"} for i in range(10)])
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=querier) for _ in range(4)] + \
                  [threading.Thread(target=dropper)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_latency_budget_on_full_manifest(self):
        import time
        # 预热(首次建索引)
        cr.lookup("qc-s", "lists model", limit=5)
        t0 = time.perf_counter()
        for i in range(20):
            cr.lookup("qc-s", f"lists model item{i}", limit=5)
        p50_ms = (time.perf_counter() - t0) / 20 * 1000
        self.assertLess(p50_ms, 100, f"2000 行 manifest 查询 p50={p50_ms:.1f}ms 超 100ms 预算")




class TestSemanticHints(unittest.TestCase):
    """S1/S2 语义补全(2026-09-01): 空结果线索 + 截断计数。"""

    def setUp(self):
        self._diag = tempfile.mkdtemp(prefix="qs_")
        self._orig = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._diag
        ms.MANIFEST.reset()

    def tearDown(self):
        _ps._DIAG_DIR = self._orig
        ms.MANIFEST.reset()
        shutil.rmtree(self._diag, ignore_errors=True)

    def test_empty_result_carries_storage_overview(self):
        """空结果附存储概况——模型可据此决定换词重查或放弃。"""
        for i in range(3):
            ms.MANIFEST.record_units(
                "s-emp", i + 1, "fifo_drop",
                [{"anchor": "r:e%d" % i, "kind": "tool_result", "role": "user",
                  "tool": "", "handle": {"type": "path",
                                         "value": "/src/lists/model.py"},
                  "size_chars": 100, "head": "content"}])
        r = cr.format_recall_result([], "nonexistent query", "s-emp")
        self.assertIn("3 条折叠单元", r)
        self.assertIn("lists/model.py", r)  # 高频句柄线索
        self.assertIn("重查", r)

    def test_empty_result_without_session_stays_simple(self):
        r = cr.format_recall_result([], "q", None)
        self.assertIn("无匹配", r)
        self.assertNotIn("条折叠单元", r)  # 概况线索仅在有会话时附加

    def test_truncation_message_carries_counts(self):
        from test.lib import state_fixture as sf
        for i in range(8):
            sf.plant_archive_tool_result("s-c2", turn=1,
                                         tool_use_id="c%d" % i,
                                         content="R" * 5000, diag_dir=self._diag)
            sf.plant_manifest_line("s-c2", turn=1, anchor="r:c%d" % i,
                                   head="hit", diag_dir=self._diag)
        lines = [{"turn": 1, "kind": "tool_result", "tool": "",
                  "handle": None, "anchor": "r:c%d" % i,
                  "reason": "fifo_drop", "size_chars": 5000,
                  "head": "hit"} for i in range(8)]
        r = cr.format_recall_result(lines, "q", "s-c2")
        self.assertIn("仅显示", r)
        self.assertIn("/8 条", r)




class TestPagedDisclosure(unittest.TestCase):
    """分页续读协议(借鉴 skill 协议按任务粒度披露, 2026-09-01)。"""

    def setUp(self):
        self._diag = tempfile.mkdtemp(prefix="pg_")
        self._orig = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._diag
        ms.MANIFEST.reset()
        self.body = ("class List:" + chr(10)
                     + chr(10).join(f"    def method_{i}(self): pass"
                                    for i in range(1200)))
        sf.plant_archive_tool_result("pg-t", turn=42, tool_use_id="call_big",
                                     content=self.body, diag_dir=self._diag)
        ms.MANIFEST.record_units("pg-t", 42, "fifo_drop",
            [{"anchor": "r:call_big", "kind": "tool_result", "role": "user",
              "tool": "", "handle": None, "size_chars": len(self.body),
              "head": self.body[:240]}])

    def tearDown(self):
        _ps._DIAG_DIR = self._orig
        ms.MANIFEST.reset()
        shutil.rmtree(self._diag, ignore_errors=True)

    def test_first_page_carries_continuation(self):
        lines = cr.lookup("pg-t", "r:call_big", limit=3)
        r = cr.format_recall_result(lines, "r:call_big", "pg-t")
        self.assertIn("第0-4000/37301 chars", r)
        self.assertIn('r:call_big@4000', r)  # 续读指令

    def test_offset_page_serves_next_window(self):
        lines = cr.lookup("pg-t", "r:call_big@4000", limit=3)
        r = cr.format_recall_result(lines, "r:call_big@4000", "pg-t",
                                    offset=4000)
        self.assertIn("第4000-8000/", r)
        self.assertIn("r:call_big@8000", r)

    def test_pages_are_seamless(self):
        full = cr.recover_full_content("pg-t", "r:call_big", 42,
                                       max_chars=None)
        p0 = cr.format_recall_result(
            cr.lookup("pg-t", "r:call_big"), "q", "pg-t")
        p1 = cr.format_recall_result(
            cr.lookup("pg-t", "r:call_big@4000"), "q", "pg-t", offset=4000)
        self.assertIn(full[100:200], p0)
        self.assertIn(full[4100:4200], p1)

    def test_small_unit_still_full_recovery(self):
        """≤ 页大小的单元保持旧行为(单块全文, 无分页噪音)。"""
        sf.plant_archive_tool_result("pg-t", turn=1, tool_use_id="c_small",
                                     content="tiny content", diag_dir=self._diag)
        ms.MANIFEST.record_units("pg-t", 1, "fifo_drop",
            [{"anchor": "r:c_small", "kind": "tool_result", "role": "user",
              "tool": "", "handle": None, "size_chars": 12,
              "head": "tiny content"}])
        lines = cr.lookup("pg-t", "r:c_small", limit=3)
        r = cr.format_recall_result(lines, "r:c_small", "pg-t")
        self.assertIn("[恢复内容 (12 chars)]", r)
        self.assertNotIn("续读", r)

    def test_parse_query_offset(self):
        self.assertEqual(cr.parse_query_offset("r:x@4000"), ("r:x", 4000))
        self.assertEqual(cr.parse_query_offset("lists/model.py"), ("lists/model.py", 0))
        self.assertEqual(cr.parse_query_offset("r:x@abc"), ("r:x@abc", 0))


if __name__ == "__main__":
    unittest.main()
