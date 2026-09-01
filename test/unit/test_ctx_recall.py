#!/usr/bin/env python3
"""test_ctx_recall.py — R10.2 召回核心测试(FTS5 trigram/中文/短查询降级/kind 过滤)。"""
import os
import shutil
import tempfile
import unittest

import proxy_state as _ps

import ctx_recall as cr
import memory_stores as ms


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


if __name__ == "__main__":
    unittest.main()
