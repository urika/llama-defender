#!/usr/bin/env python3
"""test_ctx_recall.py — R10.2 召回核心测试(FTS5 trigram/中文/短查询降级/kind 过滤)。"""
import os
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


if __name__ == "__main__":
    unittest.main()
