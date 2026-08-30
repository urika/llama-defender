#!/usr/bin/env python3
"""test_escalate.py — P5 Escalate 协议测试（状态机/熔断器/决策表）。"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from escalate import (
    TaskState, TRANSITIONS, can_transition,
    TaskCircuitBreaker, Escalator,
)
from protocol_types import build_patch, build_verdict, build_escalation
from signal_types import build_signal_snapshot


class TestTaskState(unittest.TestCase):
    """状态机转换合法性"""

    def test_pending_to_executing(self):
        self.assertTrue(can_transition(TaskState.PENDING, TaskState.EXECUTING))

    def test_executing_to_verifying(self):
        self.assertTrue(can_transition(TaskState.EXECUTING, TaskState.VERIFYING))

    def test_verifying_to_passed(self):
        self.assertTrue(can_transition(TaskState.VERIFYING, TaskState.PASSED))

    def test_verifying_to_failed(self):
        for target in [TaskState.FAILED_RETRYABLE, TaskState.FAILED_SPLITTABLE,
                       TaskState.FAILED_ROUTE]:
            self.assertTrue(can_transition(TaskState.VERIFYING, target))

    def test_terminal_states_frozen(self):
        for terminal in [TaskState.PASSED, TaskState.ESCALATED, TaskState.COMMITTED]:
            for target in TaskState:
                self.assertFalse(can_transition(terminal, target),
                                f"{terminal} 不应能转到 {target}")

    def test_invalid_transition(self):
        self.assertFalse(can_transition(TaskState.PENDING, TaskState.PASSED))
        self.assertFalse(can_transition(TaskState.PENDING, TaskState.COMMITTED))


class TestCircuitBreaker(unittest.TestCase):
    """熔断器"""

    def setUp(self):
        self.breaker = TaskCircuitBreaker(threshold=3, cooldown_s=1)

    def test_initially_closed(self):
        self.assertTrue(self.breaker.can_execute("code_fix"))

    def test_opens_after_threshold(self):
        for _ in range(3):
            self.breaker.record_failure("code_fix")
        self.assertFalse(self.breaker.can_execute("code_fix"))

    def test_resets_on_success(self):
        self.breaker.record_failure("code_fix")
        self.breaker.record_failure("code_fix")
        self.breaker.record_success("code_fix")
        self.breaker.record_failure("code_fix")
        self.assertTrue(self.breaker.can_execute("code_fix"))  # count=1 < 3

    def test_independent_per_type(self):
        for _ in range(3):
            self.breaker.record_failure("code_fix")
        self.assertFalse(self.breaker.can_execute("code_fix"))
        self.assertTrue(self.breaker.can_execute("text_gen"))  # 不受影响

    def test_cooldown_expires(self):
        breaker = TaskCircuitBreaker(threshold=1, cooldown_s=0.1)
        breaker.record_failure("test")
        self.assertFalse(breaker.can_execute("test"))
        time.sleep(0.15)
        self.assertTrue(breaker.can_execute("test"))

    def test_status_report(self):
        breaker = TaskCircuitBreaker(threshold=2, cooldown_s=60)
        breaker.record_failure("type_a")
        breaker.record_failure("type_a")
        status = breaker.status("type_a")
        self.assertTrue(status["open"])
        self.assertGreater(status["remaining_cooldown_s"], 0)


class TestEscalator(unittest.TestCase):
    """升级决策器——决策表全分支"""

    def setUp(self):
        self.breaker = TaskCircuitBreaker(threshold=3, cooldown_s=300)
        self.esc = Escalator(max_retries=2, breaker=self.breaker)
        self.patch = build_patch("t1", "diff", [])
        self.pass_verdict = build_verdict("t1", True, [])
        self.fail_verdict = build_verdict("t1", False, [
            {"rule_name": "citation", "passed": False, "detail": "quote not found"}])

    def test_default_retry(self):
        sig = build_signal_snapshot()
        d = self.esc.escalate(self.patch, self.fail_verdict, sig, 0)
        self.assertEqual(d["action"], "retry")
        self.assertIn("quote not found", d["reason"])

    def test_max_retries_human(self):
        sig = build_signal_snapshot()
        d = self.esc.escalate(self.patch, self.fail_verdict, sig, 2)
        self.assertEqual(d["action"], "human")
        self.assertEqual(d["reason"], "max_retries_exceeded")

    def test_reread_reload(self):
        sig = build_signal_snapshot(reread_pressure=2)
        d = self.esc.escalate(self.patch, self.fail_verdict, sig, 0)
        self.assertEqual(d["action"], "reload")
        self.assertIn("reread_pressure=2", d["reason"])

    def test_cognitive_load_split(self):
        sig = build_signal_snapshot(cognitive_load=0.9)
        d = self.esc.escalate(self.patch, self.fail_verdict, sig, 0)
        self.assertEqual(d["action"], "split")
        self.assertIn("cognitive_load=0.90", d["reason"])

    def test_hbe_trend_route_cloud(self):
        sig = build_signal_snapshot(h_be_trend=0.05)
        d = self.esc.escalate(self.patch, self.fail_verdict, sig, 0)
        self.assertEqual(d["action"], "route_cloud")
        self.assertIn("hbe_trend=0.050", d["reason"])

    def test_circuit_breaker_route_cloud(self):
        for _ in range(3):
            self.breaker.record_failure("code_fix")
        sig = build_signal_snapshot()
        d = self.esc.escalate(self.patch, self.fail_verdict, sig, 0, task_type="code_fix")
        self.assertEqual(d["action"], "route_cloud")
        self.assertEqual(d["reason"], "circuit_breaker_open")

    def test_signals_recorded(self):
        sig = build_signal_snapshot(reread_pressure=1, cognitive_load=0.3)
        d = self.esc.escalate(self.patch, self.fail_verdict, sig, 1)
        self.assertEqual(d["signals_at_decision"]["reread_pressure"], 1)
        self.assertEqual(d["signals_at_decision"]["cognitive_load"], 0.3)
        self.assertEqual(d["attempt_count"], 1)

    def test_record_success_resets_breaker(self):
        for _ in range(2):
            self.breaker.record_failure("type_a")
        self.esc.record_success("type_a")
        self.assertTrue(self.breaker.can_execute("type_a"))

    def test_decision_priority_breaker_over_retries(self):
        """熔断器优先于幂等闸——即使 attempt < max, 熔断打开也不重试"""
        for _ in range(3):
            self.breaker.record_failure("type_b")
        sig = build_signal_snapshot(reread_pressure=5)  # 同时有重读压力
        d = self.esc.escalate(self.patch, self.fail_verdict, sig, 0, task_type="type_b")
        self.assertEqual(d["action"], "route_cloud")  # 熔断胜出


if __name__ == "__main__":
    unittest.main()
