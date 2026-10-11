"""L-85② 注入预算门单元测试。

覆盖：
  - 门函数缺省关（budget=0 → 永远放行，计数器不动）
  - budget=N → 前 N 次放行、之后拒绝，且拒绝路径计数器继续推进（首耗尽只记一次）
  - BlockerDetector 端到端：预算内注入、预算外 skip（消息零改动、budget_skipped 标记）
  - LoopIntervention 端到端：预算耗尽 → loop_level=0 消息零改动
无会话 id（session_id 为空）→ 门不生效（放行），与现行为一致。
"""
import os
import sys
import unittest
from unittest.mock import patch

import proxy_state as _ps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from pipeline import (  # noqa: E402
    PipelineContext, BlockerDetector, LoopIntervention,
    _inject_budget_allow)


class TestInjectBudgetAllow(unittest.TestCase):
    def setUp(self):
        _ps._INJECT_BUDGET_STATE.clear()

    def tearDown(self):
        _ps._INJECT_BUDGET_STATE.clear()

    @patch.object(_ps, "PROXY_LOOP_INJECT_BUDGET", 0)
    @patch.object(_ps, "PROXY_DIAG_ENABLED", False)
    def test_default_off_always_allows(self):
        for _ in range(10):
            self.assertTrue(_inject_budget_allow("sessA", "blocker"))
        self.assertIsNone(_ps._INJECT_BUDGET_STATE.get("sessA"))  # 缺省关=计数器零触碰

    @patch.object(_ps, "PROXY_LOOP_INJECT_BUDGET", 2)
    @patch.object(_ps, "PROXY_DIAG_ENABLED", False)
    def test_budget_two_then_deny(self):
        self.assertTrue(_inject_budget_allow("sessA", "blocker"))
        self.assertTrue(_inject_budget_allow("sessA", "loop"))
        self.assertFalse(_inject_budget_allow("sessA", "session_loop"))
        self.assertFalse(_inject_budget_allow("sessA", "blocker"))
        self.assertEqual(_ps._INJECT_BUDGET_STATE["sessA"], 4)

    @patch.object(_ps, "PROXY_LOOP_INJECT_BUDGET", 1)
    @patch.object(_ps, "PROXY_DIAG_ENABLED", False)
    def test_sessions_are_independent(self):
        self.assertTrue(_inject_budget_allow("sessA", "blocker"))
        self.assertFalse(_inject_budget_allow("sessA", "blocker"))
        self.assertTrue(_inject_budget_allow("sessB", "blocker"))

    @patch.object(_ps, "PROXY_LOOP_INJECT_BUDGET", 2)
    @patch.object(_ps, "PROXY_DIAG_ENABLED", False)
    def test_empty_session_id_bypasses(self):
        for _ in range(5):
            self.assertTrue(_inject_budget_allow("", "blocker"))
        self.assertEqual(_ps._INJECT_BUDGET_STATE.get(""), None)


_BLOCKER_MSG = "[System: 文件不存在。请先用 Bash ls 或 find 命令确认项目结构，然后使用正确的文件路径。]"


def _blocker_messages():
    """两条连续同错误 tool_result——_detect_blocker_pattern 的触发形态。"""
    msgs = []
    for tid in ("t1", "t2"):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "id": tid,
             "input": {"file_path": "/f"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": _BLOCKER_MSG}]})
    return msgs


class TestBlockerBudgetGate(unittest.TestCase):
    def setUp(self):
        _ps._INJECT_BUDGET_STATE.clear()
        self.stage = BlockerDetector()

    def tearDown(self):
        _ps._INJECT_BUDGET_STATE.clear()

    @patch.object(_ps, "PROXY_LOOP_INJECT_BUDGET", 0)
    @patch.object(_ps, "PROXY_BLOCKER_ENABLED", True)
    @patch.object(_ps, "PROXY_DIAG_ENABLED", False)
    def test_default_off_injects_every_time(self):
        for _ in range(3):
            ctx = PipelineContext(messages=_blocker_messages(),
                                  session_id="sessA")
            n0 = len(ctx.messages)
            ctx = self.stage.process(ctx)
            self.assertEqual(len(ctx.messages), n0 + 1)
        self.assertIsNone(_ps._INJECT_BUDGET_STATE.get("sessA"))  # 缺省关=计数器零触碰

    @patch.object(_ps, "PROXY_LOOP_INJECT_BUDGET", 1)
    @patch.object(_ps, "PROXY_BLOCKER_ENABLED", True)
    @patch.object(_ps, "PROXY_DIAG_ENABLED", False)
    def test_budget_one_second_blocked_with_marker(self):
        ctx = PipelineContext(messages=_blocker_messages(),
                              session_id="sessA")
        n0 = len(ctx.messages)
        ctx = self.stage.process(ctx)
        self.assertEqual(len(ctx.messages), n0 + 1)  # 预算内：注入
        self.assertFalse(ctx.blocker_info.get("budget_skipped"))

        ctx = PipelineContext(messages=_blocker_messages(),
                              session_id="sessA")
        n0 = len(ctx.messages)
        ctx = self.stage.process(ctx)
        self.assertEqual(len(ctx.messages), n0)  # 预算外：零改动
        self.assertTrue(ctx.blocker_info.get("budget_skipped"))
        self.assertEqual(_ps._INJECT_BUDGET_STATE["sessA"], 2)


class TestLoopInterventionBudgetGate(unittest.TestCase):
    def setUp(self):
        _ps._INJECT_BUDGET_STATE.clear()

    def tearDown(self):
        _ps._INJECT_BUDGET_STATE.clear()

    @patch.object(_ps, "PROXY_LOOP_INJECT_BUDGET", 0)
    @patch.object(_ps, "PROXY_LOOP_THRESHOLD", 3)
    @patch.object(_ps, "PROXY_DIAG_ENABLED", False)
    def test_default_off_no_gate(self):
        ctx = PipelineContext(messages=[], session_id="sessA")
        ctx.max_run = 10
        ctx.consecutive = {"Bash": 10}
        ctx.is_text_loop = False
        ctx.body = {}
        ctx = LoopIntervention().process(ctx)
        # budget=0：门不拦，走原干预路径（loop_level 由干预逻辑决定，非 0 强制）
        self.assertNotEqual(ctx.loop_tool_name, "budget_skipped")

    @patch.object(_ps, "PROXY_LOOP_INJECT_BUDGET", 1)
    @patch.object(_ps, "PROXY_LOOP_THRESHOLD", 3)
    @patch.object(_ps, "PROXY_DIAG_ENABLED", False)
    def test_exhausted_loop_zero_mutation(self):
        self.assertTrue(_inject_budget_allow("sessA", "loop"))  # 耗尽预算
        ctx = PipelineContext(messages=[{"role": "user", "content": "x"}],
                              session_id="sessA")
        ctx.max_run = 10
        ctx.consecutive = {"Bash": 10}
        ctx.is_text_loop = False
        ctx.body = {}
        n0 = len(ctx.messages)
        ctx = LoopIntervention().process(ctx)
        self.assertEqual(len(ctx.messages), n0)  # 消息零改动
        self.assertEqual(ctx.loop_level, 0)
        self.assertEqual(ctx.loop_tool_name, "budget_skipped")


if __name__ == "__main__":
    unittest.main()
