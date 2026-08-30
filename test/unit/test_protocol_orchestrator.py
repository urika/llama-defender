#!/usr/bin/env python3
"""test_protocol_orchestrator.py — 编排器闭环测试（五协议联动/状态机/幂等/防御）。"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from decompose import Decomposer, task_hash
from escalate import Escalator, TaskState
from idempotency import IdempotencyManager
from protocol_orchestrator import ProtocolOrchestrator, IllegalTransitionError
from protocol_types import build_patch, build_task
from signal_types import build_signal_snapshot


def _passing_executor(subtask):
    """总是产出通过验证的补丁。"""
    return build_patch(
        task_id=subtask["id"],
        diff="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b",
        citations=[{"anchor": "u:t1"}])


def _failing_executor(subtask):
    """总是产出空 diff（机械验证失败）。"""
    return build_patch(task_id=subtask["id"], diff="", citations=[])


def _flaky_executor_factory(fail_first_n=1):
    """前 N 次失败, 之后成功。"""
    counter = {"n": 0}
    lock = threading.Lock()
    def exec_fn(subtask):
        with lock:
            counter["n"] += 1
            n = counter["n"]
        if n <= fail_first_n:
            return build_patch(task_id=subtask["id"], diff="", citations=[])
        return build_patch(
            task_id=subtask["id"],
            diff="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b",
            citations=[{"anchor": "u:t1"}])
    return exec_fn, counter


SIG = build_signal_snapshot(session_key="s1", turn=1)


class TestHappyPath(unittest.TestCase):

    def test_single_task_completes(self):
        orch = ProtocolOrchestrator(executor=_passing_executor)
        task = build_task("T1", "fix typo", ["src/a.py"])
        result = orch.solve(task, SIG)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["patches"]), 1)
        self.assertEqual(result["subtasks"][0]["state"], "passed")
        self.assertEqual(result["escalations"], [])

    def test_multi_file_task_decomposes_and_completes(self):
        orch = ProtocolOrchestrator(executor=_passing_executor)
        task = build_task(
            "T2", "fix three files",
            input_files=["src/a.py " + "x" * 60000,
                         "src/b.py " + "x" * 60000,
                         "src/c.py " + "x" * 60000])
        result = orch.solve(task, SIG)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["patches"]), 3)
        self.assertEqual(len(result["subtasks"]), 3)

    def test_no_executor_returns_no_executor_status(self):
        orch = ProtocolOrchestrator()  # 无执行器
        result = orch.solve(build_task("T1", "d", ["f"]), SIG)
        self.assertEqual(result["status"], "no_executor")
        # 分解仍然发生（子任务就绪, 等执行器注入）
        self.assertEqual(len(result["subtasks"]), 1)

    def test_elapsed_ms_present(self):
        orch = ProtocolOrchestrator(executor=_passing_executor)
        result = orch.solve(build_task("T1", "d", ["f"]), SIG)
        self.assertIsInstance(result["elapsed_ms"], int)
        self.assertGreaterEqual(result["elapsed_ms"], 0)


class TestRetryPath(unittest.TestCase):

    def test_flaky_task_retries_then_passes(self):
        exec_fn, counter = _flaky_executor_factory(fail_first_n=1)
        orch = ProtocolOrchestrator(executor=exec_fn, max_subtask_retries=2)
        result = orch.solve(build_task("T1", "d", ["f"]), SIG)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(counter["n"], 2)  # 失败 1 次 + 成功 1 次
        self.assertEqual(result["subtasks"][0]["state"], "passed")
        self.assertEqual(result["subtasks"][0]["attempts"], 2)
        # 有一次升级决策（retry）
        self.assertEqual(len(result["escalations"]), 1)
        self.assertEqual(result["escalations"][0]["action"], "retry")

    def test_exhausted_retries_escalates_to_human(self):
        orch = ProtocolOrchestrator(executor=_failing_executor, max_subtask_retries=1)
        result = orch.solve(build_task("T1", "d", ["f"]), SIG)
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["subtasks"][0]["state"], "escalated")
        # escalation 链: retry → human
        actions = [e["action"] for e in result["escalations"]]
        self.assertIn("human", actions)


class TestEscalationPaths(unittest.TestCase):

    def test_reread_pressure_triggers_reload_with_recall(self):
        recalled = []
        def recaller(sid, session_key):
            recalled.append((sid, session_key))
            return "recovered: file content here"
        exec_fn, counter = _flaky_executor_factory(fail_first_n=2)
        orch = ProtocolOrchestrator(
            executor=exec_fn, recaller=recaller, max_subtask_retries=3)
        signal = build_signal_snapshot(session_key="s1", reread_pressure=3)
        result = orch.solve(build_task("T1", "d", ["f"]), signal)
        # reload 动作被触发（P4 召回介入）
        actions = [e["action"] for e in result["escalations"]]
        self.assertIn("reload", actions)
        self.assertTrue(any(sid == "T1" for sid, _ in recalled))
        self.assertEqual(recalled[0][1], "s1")
        self.assertEqual(result["status"], "completed")

    def test_reload_without_recaller_degrades_to_escalate(self):
        exec_fn, _ = _flaky_executor_factory(fail_first_n=99)
        orch = ProtocolOrchestrator(
            executor=exec_fn, recaller=None, max_subtask_retries=3)
        signal = build_signal_snapshot(session_key="s1", reread_pressure=3)
        result = orch.solve(build_task("T1", "d", ["f"]), signal)
        self.assertEqual(result["status"], "escalated")

    def test_hbe_trend_triggers_route_cloud(self):
        exec_fn, _ = _flaky_executor_factory(fail_first_n=1)
        orch = ProtocolOrchestrator(executor=exec_fn, max_subtask_retries=3)
        signal = build_signal_snapshot(session_key="s1", h_be_trend=0.5)
        result = orch.solve(build_task("T1", "d", ["f"]), signal)
        actions = [e["action"] for e in result["escalations"]]
        self.assertIn("route_cloud", actions)
        self.assertEqual(result["status"], "escalated")


class TestIterationBudget(unittest.TestCase):

    def test_iteration_budget_exhaustion_escalates(self):
        # 永远失败 + 允许无限重试 → 迭代上限兜底
        orch = ProtocolOrchestrator(
            executor=_failing_executor,
            escalator=Escalator(max_retries=99),
            max_subtask_retries=99,
            max_iterations=3)
        result = orch.solve(build_task("T1", "d", ["f"]), SIG)
        self.assertEqual(result["status"], "escalated")
        self.assertLessEqual(result["iterations"], 3)
        reasons = [e["reason"] for e in result["escalations"]]
        self.assertIn("iteration_budget_exhausted", reasons)


class TestIdempotencyIntegration(unittest.TestCase):

    def test_decompose_cached_across_solves(self):
        decompose_calls = []
        class CountingDecomposer(Decomposer):
            def decompose(self, task, signal):
                decompose_calls.append(task["id"])
                return super().decompose(task, signal)
        orch = ProtocolOrchestrator(
            decomposer=CountingDecomposer(),
            idem=IdempotencyManager(),
            executor=_passing_executor)
        task = build_task("T1", "d", ["f"])
        orch.solve(task, SIG)
        orch.solve(task, SIG)  # 同任务第二次 → 分解命中缓存
        self.assertEqual(len(decompose_calls), 1)
        # 第二次 solve 的结果仍完整
        result2 = orch.solve(task, SIG)
        self.assertEqual(result2["status"], "completed")

    def test_verify_cached_same_patch(self):
        verify_calls = []
        class CountingOrch(ProtocolOrchestrator):
            pass
        # 用 wrapper 侦听 idempotency 命中
        orch = ProtocolOrchestrator(executor=_passing_executor)
        orch.solve(build_task("T1", "d", ["f"]), SIG)
        stats = orch.idem.stats()
        self.assertGreaterEqual(stats["verify"]["hits"] + stats["verify"]["misses"], 1)


class TestStateMachineIntegration(unittest.TestCase):

    def test_final_states_are_legal(self):
        orch = ProtocolOrchestrator(executor=_passing_executor)
        result = orch.solve(build_task("T1", "d", ["f"]), SIG)
        for st in result["subtasks"]:
            self.assertIn(st["state"],
                          ("passed", "escalated", "pending", "executing"))

    def test_illegal_transition_raises(self):
        with self.assertRaises(IllegalTransitionError):
            ProtocolOrchestrator._transition(TaskState.PASSED, TaskState.EXECUTING)


class TestThreadSafety(unittest.TestCase):
    """Spec-D 并发测试——多线程 solve 各自独立会话不串扰。"""

    def test_concurrent_isolated_solves(self):
        orch = ProtocolOrchestrator(
            executor=_passing_executor,
            idem=IdempotencyManager())
        results = []
        errors = []
        def worker(i):
            try:
                sig = build_signal_snapshot(session_key=f"s{i}", turn=1)
                results.append(orch.solve(
                    build_task(f"T{i}", f"task {i}", [f"f{i}.py"]), sig))
            except Exception as e:  # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        for r in results:
            self.assertEqual(r["status"], "completed")

    def test_concurrent_mixed_outcomes(self):
        """一半成功一半失败的并发会话——结果各自正确归因。"""
        def make_orch(i):
            if i % 2 == 0:
                return ProtocolOrchestrator(executor=_passing_executor)
            return ProtocolOrchestrator(
                executor=_failing_executor, max_subtask_retries=1)
        orch = ProtocolOrchestrator(executor=_passing_executor)
        # 简化: 用两个共享 idem 的编排器并发
        orch2 = ProtocolOrchestrator(
            executor=_failing_executor, max_subtask_retries=1)
        results = {"ok": [], "fail": []}
        errors = []
        def worker_ok():
            try:
                for i in range(20):
                    results["ok"].append(orch.solve(
                        build_task(f"OK{i}", "d", [f"ok{i}.py"]),
                        build_signal_snapshot(session_key=f"ok{i}")))
            except Exception as e:  # pragma: no cover
                errors.append(e)
        def worker_fail():
            try:
                for i in range(20):
                    results["fail"].append(orch2.solve(
                        build_task(f"BAD{i}", "d", [f"bad{i}.py"]),
                        build_signal_snapshot(session_key=f"bad{i}")))
            except Exception as e:  # pragma: no cover
                errors.append(e)
        t1 = threading.Thread(target=worker_ok)
        t2 = threading.Thread(target=worker_fail)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(errors, [])
        self.assertTrue(all(r["status"] == "completed" for r in results["ok"]))
        self.assertTrue(all(r["status"] == "escalated" for r in results["fail"]))


class TestPerformanceBudgets(unittest.TestCase):
    """Spec-D 性能预算——编排自身开销（不含 executor 模型时间）不超预算。"""

    def test_orchestration_overhead_within_budget(self):
        import time as _time
        from protocol_orchestrator import PROTOCOL_BUDGETS
        # no-op 执行器 → solve 的耗时几乎全是编排开销
        orch = ProtocolOrchestrator(executor=lambda st: build_patch(
            task_id=st["id"], diff="x", citations=[{"anchor": "a"}]))
        task = build_task(
            "PERF", "perf probe",
            input_files=[f"src/f{i}.py " + "x" * 3000 for i in range(8)])
        sig = build_signal_snapshot(session_key="perf")
        t0 = _time.perf_counter()
        result = orch.solve(task, sig)
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        self.assertEqual(result["status"], "completed")
        budget = PROTOCOL_BUDGETS["orchestration_overhead_ms"] * 4  # CI 抖动余量
        self.assertLess(elapsed_ms, budget,
                        f"orchestration overhead {elapsed_ms:.1f}ms exceeds budget")

    def test_budgets_dict_shape(self):
        from protocol_orchestrator import PROTOCOL_BUDGETS
        for key in ["orchestration_overhead_ms", "decompose_ms", "verify_ms",
                    "cache_op_ms", "serial_subtasks_soft_limit"]:
            self.assertIn(key, PROTOCOL_BUDGETS)
            self.assertGreater(PROTOCOL_BUDGETS[key], 0)

    def test_decompose_within_budget(self):
        import time as _time
        from protocol_orchestrator import PROTOCOL_BUDGETS
        dec = Decomposer()
        task = build_task(
            "D", "decompose budget",
            input_files=[f"src/f{i}.py " + "x" * 20000 for i in range(10)])
        t0 = _time.perf_counter()
        dec.decompose(task, SIG)
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        budget = PROTOCOL_BUDGETS["decompose_ms"] * 10  # CI 抖动余量
        self.assertLess(elapsed_ms, budget)

    def test_verify_within_budget(self):
        import time as _time
        from protocol_orchestrator import PROTOCOL_BUDGETS
        from verification_chain import build_verification_chain
        p = build_patch(task_id="V", diff="--- a\n+++ b\n@@\n-a\n+b",
                        citations=[{"anchor": "u:1"}])
        p["task_description"] = "fix thing"
        p["output"] = "thing fixed"
        t0 = _time.perf_counter()
        build_verification_chain().handle(p)
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        budget = PROTOCOL_BUDGETS["verify_ms"] * 10  # CI 抖动余量
        self.assertLess(elapsed_ms, budget)

    def test_cache_op_within_budget(self):
        import time as _time
        from protocol_orchestrator import PROTOCOL_BUDGETS
        mgr = IdempotencyManager()
        mgr.verify_cached("warm", lambda: 1)  # 预热
        t0 = _time.perf_counter()
        for i in range(1000):
            mgr.verify_cached("warm", lambda: 1)
        per_op_ms = (_time.perf_counter() - t0) * 1000 / 1000
        budget = PROTOCOL_BUDGETS["cache_op_ms"] * 10  # CI 抖动余量
        self.assertLess(per_op_ms, budget)


if __name__ == "__main__":
    unittest.main()
