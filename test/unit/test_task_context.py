#!/usr/bin/env python3
"""R18 task-context 单元测试：build_task_context_bundle 聚合口径（契约 §3.3 冻结版）。

覆盖：命中恢复/优雅缺页/无命中 200 空包/预算裁剪与封顶/跨会话遍历/触发词回填。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import ctx_recall  # noqa: E402


def _line(anchor="r:t1", turn=3, head="auth 模块超时现场"):
    return {"anchor": anchor, "turn": turn, "head": head,
            "tool": "Read", "kind": "tool_result",
            "triggers": "登录 timeout", "handle": {"type": "value", "value": "src/auth.py"}}


class TestBuildTaskContextBundle(unittest.TestCase):
    def test_hit_with_recovery(self):
        with patch.object(ctx_recall, "_manifest_search",
                          return_value=[_line()]), \
             patch.object(ctx_recall, "recover_full_content",
                          return_value="FULL " * 100):
            bundle = ctx_recall.build_task_context_bundle(
                {"description": "修复登录超时", "keywords": ["登录"]},
                session_key="sess0001", budget_chars=6000)
        self.assertTrue(bundle["bundle_id"].startswith("b-"))
        self.assertEqual(len(bundle["items"]), 1)
        item = bundle["items"][0]
        self.assertEqual(item["unit_id"], "r:t1")
        self.assertEqual(item["source"], "manifest")
        self.assertTrue(item["full_available"])
        self.assertTrue(item["content"].startswith("FULL "))
        self.assertIn("登录", item["trigger_why"])
        self.assertGreaterEqual(bundle["budget_remaining"], 0)
        self.assertEqual(bundle["total_chars"] + bundle["budget_remaining"], 6000)

    def test_unrecoverable_anchor_graceful_missing_page(self):
        with patch.object(ctx_recall, "_manifest_search",
                          return_value=[_line(anchor="u:t9")]), \
             patch.object(ctx_recall, "recover_full_content") as m_rec:
            bundle = ctx_recall.build_task_context_bundle(
                {"keywords": ["超时"]}, session_key="sess0001")
        m_rec.assert_not_called()                      # u: 锚不可恢复, 不做无谓 IO
        item = bundle["items"][0]
        self.assertFalse(item["full_available"])       # 优雅缺页: 仅索引
        self.assertEqual(item["content"], "auth 模块超时现场")  # head 预览兜底

    def test_no_hits_returns_empty_200_semantics(self):
        with patch.object(ctx_recall, "_manifest_search", return_value=[]):
            bundle = ctx_recall.build_task_context_bundle(
                {"description": "毫无命中的任务"}, session_key="sess0001")
        self.assertEqual(bundle["items"], [])
        self.assertEqual(bundle["total_chars"], 0)
        self.assertEqual(bundle["budget_remaining"], 6000)

    def test_budget_clips_recovery_and_limits_items(self):
        lines = [_line(anchor="r:t%d" % i, turn=i, head="h%d" % i) for i in range(4)]

        def fake_search(sid, queries, kind=None, limit=8):
            return list(lines)

        with patch.object(ctx_recall, "_manifest_search", side_effect=fake_search), \
             patch.object(ctx_recall, "recover_full_content",
                          return_value="x" * 500):
            bundle = ctx_recall.build_task_context_bundle(
                {"keywords": ["x"]}, session_key="sess0001", budget_chars=600)
        self.assertLessEqual(bundle["total_chars"], 600)
        self.assertTrue(all(len(i["content"]) <= 600 for i in bundle["items"]))
        self.assertEqual(bundle["total_chars"] + bundle["budget_remaining"], 600)

    def test_max_budget_clamped(self):
        with patch.object(ctx_recall, "_manifest_search", return_value=[]):
            bundle = ctx_recall.build_task_context_bundle(
                {}, session_key="s", budget_chars=999999)
        self.assertEqual(bundle["budget_remaining"], 20000)  # max_budget 封顶

    def test_cross_session_when_no_session_key(self):
        lines_by_sid = {
            "s1": [_line(anchor="r:a1", head="s1 现场")],
            "s2": [_line(anchor="r:a2", turn=9, head="s2 现场")],
        }
        with patch.object(ctx_recall, "_manifest_search",
                          side_effect=lambda sid, queries, kind=None, limit=8:
                              list(lines_by_sid.get(sid, []))), \
             patch("memory_stores.MANIFEST.known_sessions",
                   return_value=["s1", "s2"]), \
             patch.object(ctx_recall, "recover_full_content", return_value="C"):
            bundle = ctx_recall.build_task_context_bundle(
                {"keywords": ["现场"]})
        self.assertEqual({i["unit_id"] for i in bundle["items"]}, {"r:a1", "r:a2"})


if __name__ == "__main__":
    unittest.main()
