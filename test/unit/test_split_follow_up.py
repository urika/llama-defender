#!/usr/bin/env python3
"""test_split_follow_up.py — split micro-turn 混合批次拆分自答（0923 B 根治）。"""
import json, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
import ctx_recall as cr


def _tc(name, args):
    return {"id": "call_%s" % name, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class TestSplitFollowUp(unittest.TestCase):
    def test_mixed_batch_returns_followup(self):
        """cr + 其他工具混合 → assistant(仅cr子集) + tool(cr结果)，不再整单 None。"""
        tcs = [_tc("ctx_recall", {"query": "bank-risk-report"}),
               _tc("google_search", {"q": "test"})]
        fu, split = cr.split_follow_up_messages("sess_x", tcs)
        self.assertTrue(split)
        self.assertIsNotNone(fu)
        self.assertEqual(fu[0]["role"], "assistant")
        self.assertEqual(len(fu[0]["tool_calls"]), 1)      # 仅 cr 子集（全量保留会 400）
        self.assertEqual(fu[0]["tool_calls"][0]["function"]["name"], "ctx_recall")
        tool_results = [m for m in fu if m["role"] == "tool"]
        self.assertEqual(len(tool_results), 1)             # 仅 cr 自答
        self.assertEqual(tool_results[0]["tool_call_id"], "call_ctx_recall")

    def test_mixed_batch_pairing_complete(self):
        """P1 回归：每个 tool_call 都有对应 tool 结果（OpenAI 配对要求，后端不 400）。"""
        tcs = [_tc("ctx_recall", {"query": "a"}),
               _tc("ctx_recall", {"query": "b"}),
               _tc("google_search", {"q": "x"})]
        fu, split = cr.split_follow_up_messages("sess_x", tcs)
        self.assertTrue(split)
        call_ids = {tc["id"] for tc in fu[0]["tool_calls"]}
        result_ids = {m["tool_call_id"] for m in fu if m["role"] == "tool"}
        self.assertEqual(call_ids, result_ids)
        self.assertNotIn("call_google_search", call_ids)  # 非 cr 调用不进重派

    def test_pure_batch_delegates(self):
        """纯 cr 批次 → 与 build_follow_up_messages 等价。"""
        tcs = [_tc("ctx_recall", {"query": "x"})]
        fu, split = cr.split_follow_up_messages("sess_x", tcs)
        self.assertTrue(split)
        self.assertEqual(len(fu), 2)

    def test_no_cr_returns_none(self):
        """无 cr → (None, False)（走原透传）。"""
        fu, split = cr.split_follow_up_messages(
            "sess_x", [_tc("google_search", {"q": "x"})])
        self.assertIsNone(fu)
        self.assertFalse(split)

    def test_bad_args_cr_still_answered(self):
        """cr 参数坏 JSON → 结果为错误提示而非放弃。"""
        tc = {"id": "call_bad", "type": "function",
              "function": {"name": "ctx_recall", "arguments": "{bad json"}}
        fu, split = cr.split_follow_up_messages("sess_x", [tc])
        self.assertTrue(split)
        self.assertIn("missing query", fu[1]["content"])

    def test_empty(self):
        fu, split = cr.split_follow_up_messages("sess_x", [])
        self.assertIsNone(fu)
        self.assertFalse(split)


if __name__ == "__main__":
    unittest.main(verbosity=2)
