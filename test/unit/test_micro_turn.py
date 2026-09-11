#!/usr/bin/env python3
"""test_micro_turn.py — IFC-3 方案B 微轮重派测试（构造器/预算/回退）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import proxy_state as _ps
from ctx_recall import build_follow_up_messages, MICRO_TURN_RESULT_MAX_CHARS


def _tc(name="ctx_recall", args='{"query": "app.py"}', tc_id="call_1"):
    return {"id": tc_id, "type": "function",
            "function": {"name": name, "arguments": args}}


class TestBuildFollowUp(unittest.TestCase):

    def test_valid_single_call(self):
        msgs = build_follow_up_messages("sess_m", [_tc()])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "assistant")
        self.assertIn("tool_calls", msgs[0])
        self.assertEqual(msgs[0]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(msgs[1]["role"], "tool")
        self.assertEqual(msgs[1]["tool_call_id"], "call_1")
        self.assertIn("ctx_recall", msgs[1]["content"])

    def test_missing_session_returns_none(self):
        self.assertIsNone(build_follow_up_messages("", [_tc()]))

    def test_empty_calls_returns_none(self):
        self.assertIsNone(build_follow_up_messages("sess_m", []))

    def test_mixed_tools_returns_none(self):
        # 混有其他工具调用 → 无法全部自答 → 走路径 A
        self.assertIsNone(build_follow_up_messages(
            "sess_m", [_tc(), _tc(name="Read", args='{"file_path": "x"}')]))

    def test_bad_json_returns_none(self):
        self.assertIsNone(build_follow_up_messages(
            "sess_m", [_tc(args='{"query": broken')]))

    def test_missing_query_gets_usage_hint(self):
        msgs = build_follow_up_messages("sess_m", [_tc(args="{}")])
        self.assertIn("缺少 query 参数", msgs[1]["content"])

    def test_result_truncated_to_guard(self):
        # 构造超长命中: 无 manifest 时返回"无匹配"短文本——验证截断逻辑
        # 用异常路径: monkeypatch lookup 抛异常 → 错误文本; 再直接测截断上限
        import ctx_recall
        orig = ctx_recall.lookup
        try:
            ctx_recall.lookup = lambda *a, **k: [{"anchor": "r:x%d" % i, "kind": "message",
                                                  "turn": i, "handle": {}, "reason": "compress",
                                                  "size_chars": 10, "head": "x" * 500, "tool": ""}
                                                 for i in range(20)]
            msgs = build_follow_up_messages("sess_m", [_tc(args='{"query": "x", "limit": 20}')])
            self.assertLessEqual(len(msgs[1]["content"]), MICRO_TURN_RESULT_MAX_CHARS + 100)
        finally:
            ctx_recall.lookup = orig

    def test_lookup_exception_becomes_error_text(self):
        import ctx_recall
        orig = ctx_recall.lookup
        try:
            def boom(*a, **k):
                raise RuntimeError("db locked")
            ctx_recall.lookup = boom
            msgs = build_follow_up_messages("sess_m", [_tc()])
            self.assertIn("检索失败", msgs[1]["content"])
        finally:
            ctx_recall.lookup = orig

    def test_limit_clamped(self):
        # limit 超界钳到 [1,20]; 非法值回落 8
        import ctx_recall
        calls = []
        orig = ctx_recall.lookup
        try:
            def spy(session_key, query, kind=None, limit=8):
                calls.append(limit)
                return []
            ctx_recall.lookup = spy
            build_follow_up_messages("sess_m", [_tc(args='{"query": "x", "limit": 999}')])
            build_follow_up_messages("sess_m", [_tc(args='{"query": "x", "limit": "abc"}')])
            self.assertEqual(calls, [20, 8])
        finally:
            ctx_recall.lookup = orig

    def test_kind_any_normalized_to_none(self):
        import ctx_recall
        kinds = []
        orig = ctx_recall.lookup
        try:
            def spy(session_key, query, kind=None, limit=8):
                kinds.append(kind)
                return []
            ctx_recall.lookup = spy
            build_follow_up_messages("sess_m", [_tc(args='{"query": "x", "kind": "any"}')])
            self.assertEqual(kinds, [None])
        finally:
            ctx_recall.lookup = orig

    def test_missing_id_generated(self):
        msgs = build_follow_up_messages("sess_m", [{"type": "function",
                                                    "function": {"name": "ctx_recall",
                                                                 "arguments": '{"query": "x"}'}}])
        self.assertTrue(msgs[0]["tool_calls"][0]["id"].startswith("call_"))
        self.assertEqual(msgs[1]["tool_call_id"], msgs[0]["tool_calls"][0]["id"])


class TestMicroTurnDispatchClosure(unittest.TestCase):
    """pipeline 闭包的预算/开关语义（不触网络——仅测拒绝路径）。"""

    def _make_closure(self, ctx):
        from pipeline import BackendDispatcher
        # 只需要实例方法, 不触 __init__(其依赖 handler)
        bd = object.__new__(BackendDispatcher)
        calls = []
        def fake_redispatch(c, b, k):
            calls.append(1)
            return None
        bd._do_dispatch = fake_redispatch
        closure = bd._make_micro_turn_dispatch(ctx, "http://x", "k")
        return closure, calls

    def test_disabled_returns_false(self):
        # L-22 门语义: 闭包门=PD + (MICRO_TURN or RESCUE); 两个子开关全关
        # 才拒绝(各调用点另有自己的子开关门)。
        _ps.PROXY_PD_MICRO_TURN_ENABLED = False
        _ps.PROXY_RESCUE_ENABLED = False
        try:
            from pipeline import PipelineContext
            ctx = PipelineContext()
            closure, calls = self._make_closure(ctx)
            self.assertFalse(closure([{"role": "tool", "tool_call_id": "x", "content": "y"}]))
            self.assertEqual(calls, [])
        finally:
            _ps.PROXY_PD_MICRO_TURN_ENABLED = False
            _ps.PROXY_RESCUE_ENABLED = True

    def test_rescue_bypass_keeps_gate_open(self):
        # L-22: ctx_recall 子开关关但 rescue 开 → 闭包门仍开(rescue 通道)。
        _ps.PROXY_PD_MICRO_TURN_ENABLED = False
        _ps.PROXY_RESCUE_ENABLED = True
        try:
            from pipeline import PipelineContext
            ctx = PipelineContext()
            ctx.openai_body = {"messages": [{"role": "user", "content": "hi"}]}
            closure, calls = self._make_closure(ctx)
            follow = [{"role": "assistant", "content": "bad"},
                      {"role": "user", "content": "corrective tip"}]
            self.assertTrue(closure(follow))
            self.assertEqual(len(calls), 1)
            self.assertEqual(ctx._micro_turn_used, 1)
        finally:
            _ps.PROXY_PD_MICRO_TURN_ENABLED = False
            _ps.PROXY_RESCUE_ENABLED = True

    def test_budget_exhaustion_returns_false(self):
        _ps.PROXY_PD_MICRO_TURN_ENABLED = True
        try:
            from pipeline import PipelineContext
            ctx = PipelineContext()
            ctx._micro_turn_used = 2  # 已达默认上限
            closure, calls = self._make_closure(ctx)
            self.assertFalse(closure([{"role": "tool", "tool_call_id": "x", "content": "y"}]))
            self.assertEqual(calls, [])
        finally:
            _ps.PROXY_PD_MICRO_TURN_ENABLED = False

    def test_enabled_dispatches_and_counts(self):
        _ps.PROXY_PD_MICRO_TURN_ENABLED = True
        try:
            from pipeline import PipelineContext
            ctx = PipelineContext()
            ctx.openai_body = {"messages": [{"role": "user", "content": "hi"}]}
            closure, calls = self._make_closure(ctx)
            follow = [{"role": "assistant", "content": "", "tool_calls": []},
                      {"role": "tool", "tool_call_id": "x", "content": "y"}]
            self.assertTrue(closure(follow))
            self.assertEqual(len(calls), 1)
            self.assertEqual(ctx._micro_turn_used, 1)
            # follow-up 已追加
            self.assertEqual(len(ctx.openai_body["messages"]), 3)
        finally:
            _ps.PROXY_PD_MICRO_TURN_ENABLED = False


if __name__ == "__main__":
    unittest.main()
