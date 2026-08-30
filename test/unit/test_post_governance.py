#!/usr/bin/env python3
"""test_post_governance.py — Spec-E 后治理测试（校准/编译/数据质量/并发）。"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from post_governance import (
    EntryLayerCalibrator, PatternCompiler, PostGovernance,
    data_quality_report, Attribution,
)
from protocol_types import build_task


def _task(ttype="patch"):
    return build_task("T1", "do work", ["f.py"], expected_output_type=ttype)


def _traj(*steps):
    """_traj((0,'fail'),(1,'fail'),(2,'success')) → layer_trajectory"""
    return [{"layer": l, "outcome": o} for l, o in steps]


class TestEntryLayerCalibrator(unittest.TestCase):
    """验收: 同类任务历史显示总在层 2 成功 → 推荐入场层从 0 调到 2。"""

    def test_no_history_recommends_zero(self):
        cal = EntryLayerCalibrator()
        self.assertEqual(cal.recommend("patch"), 0)

    def test_insufficient_samples_conservative(self):
        cal = EntryLayerCalibrator(min_samples=3)
        for _ in range(2):  # 只有 2 次成功 < min_samples
            cal.update("patch", Attribution(entry_layer=0, exit_layer=2,
                                            success=True))
        self.assertEqual(cal.recommend("patch"), 0)

    def test_always_layer2_success_recommends_2(self):
        cal = EntryLayerCalibrator(min_samples=3)
        for _ in range(3):
            cal.update("patch", Attribution(entry_layer=0, exit_layer=2,
                                            success=True))
        self.assertEqual(cal.recommend("patch"), 2)

    def test_cheapest_successful_layer_wins(self):
        # 层 1 一次成功 + 层 2 两次成功 → 推荐最便宜的充分层 1
        cal = EntryLayerCalibrator(min_samples=3)
        cal.update("patch", Attribution(entry_layer=0, exit_layer=1, success=True))
        cal.update("patch", Attribution(entry_layer=0, exit_layer=2, success=True))
        cal.update("patch", Attribution(entry_layer=0, exit_layer=2, success=True))
        self.assertEqual(cal.recommend("patch"), 1)

    def test_failures_ignored_in_recommendation(self):
        cal = EntryLayerCalibrator(min_samples=3)
        for _ in range(5):  # 失败不算样本
            cal.update("patch", Attribution(entry_layer=0, exit_layer=3,
                                            success=False))
        self.assertEqual(cal.recommend("patch"), 0)

    def test_recommend_clamped_to_max_layer(self):
        cal = EntryLayerCalibrator(min_samples=1)
        cal.update("patch", Attribution(entry_layer=3, exit_layer=3, success=True))
        self.assertLessEqual(cal.recommend("patch"), 3)

    def test_task_types_isolated(self):
        cal = EntryLayerCalibrator(min_samples=3)
        for _ in range(3):
            cal.update("patch", Attribution(entry_layer=0, exit_layer=2,
                                            success=True))
        self.assertEqual(cal.recommend("text"), 0)  # text 无历史

    def test_window_evicts_old(self):
        cal = EntryLayerCalibrator(min_samples=3, window=3)
        for _ in range(3):
            cal.update("patch", Attribution(entry_layer=0, exit_layer=2,
                                            success=True))
        # 再压入 3 条层 0 失败记录 → 成功记录被挤出窗口
        for _ in range(3):
            cal.update("patch", Attribution(entry_layer=0, exit_layer=0,
                                            success=False))
        self.assertEqual(cal.recommend("patch"), 0)

    def test_stats_shape(self):
        cal = EntryLayerCalibrator()
        cal.update("patch", Attribution(entry_layer=0, exit_layer=2, success=True))
        s = cal.stats("patch")
        self.assertEqual(s["samples"], 1)
        self.assertEqual(s["successes"], 1)
        self.assertIn("recommended_entry_layer", s)


class TestPatternCompiler(unittest.TestCase):
    """验收: 同类成功模式出现 ≥3 次后生成 Skill。"""

    def _pattern(self, task_type="patch", sig="0F>2S", exit_layer=2):
        return {"task_type": task_type, "signature": sig,
                "entry_layer": 0, "exit_layer": exit_layer, "success": True}

    def test_below_threshold_no_skill(self):
        pc = PatternCompiler(threshold=3)
        pc.observe(self._pattern())
        pc.observe(self._pattern())
        self.assertIsNone(pc.compile(self._pattern()))
        self.assertEqual(pc.count(self._pattern()), 2)

    def test_at_threshold_compiles(self):
        pc = PatternCompiler(threshold=3)
        for _ in range(3):
            pc.observe(self._pattern())
        skill = pc.compile(self._pattern())
        self.assertIsNotNone(skill)
        self.assertEqual(skill["source_layer"], 2)
        self.assertEqual(skill["target_layer"], 1)  # 层 2 → 编译到层 1
        self.assertEqual(skill["compiled_from_count"], 3)
        self.assertIn("patch", skill["name"])

    def test_layer0_pattern_targets_layer0(self):
        # 层 0 的模式不能再下沉 → target 钳到 0
        pc = PatternCompiler(threshold=3)
        p = self._pattern(sig="0S", exit_layer=0)
        for _ in range(3):
            pc.observe(p)
        skill = pc.compile(p)
        self.assertEqual(skill["target_layer"], 0)

    def test_idempotent_compile(self):
        pc = PatternCompiler(threshold=3)
        for _ in range(5):  # 超过阈值也不重复编译
            pc.observe(self._pattern())
        first = pc.compile(self._pattern())
        second = pc.compile(self._pattern())
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # 已编译过
        self.assertEqual(len(pc.skills), 1)

    def test_different_signatures_independent(self):
        pc = PatternCompiler(threshold=3)
        pc.observe(self._pattern(sig="0F>1S"))
        pc.observe(self._pattern(sig="0F>2S"))
        self.assertEqual(pc.count(self._pattern(sig="0F>1S")), 1)
        self.assertEqual(pc.count(self._pattern(sig="0F>2S")), 1)


class TestPostGovernance(unittest.TestCase):
    """验收: 后治理记录每次 solve 的层级轨迹。"""

    def test_process_returns_attribution(self):
        pg = PostGovernance()
        attr = pg.process(_task(), _traj((0, "fail"), (2, "success")),
                          {"status": "completed"})
        self.assertEqual(attr["entry_layer"], 0)
        self.assertEqual(attr["exit_layer"], 2)
        self.assertTrue(attr["success"])
        self.assertEqual(attr["pattern"], "0F>2S")
        self.assertEqual(attr["pattern_count"], 1)

    def test_bool_outcome(self):
        pg = PostGovernance()
        attr = pg.process(_task(), _traj((1, "success")), True)
        self.assertTrue(attr["success"])
        attr2 = pg.process(_task(), _traj((1, "fail")), False)
        self.assertFalse(attr2["success"])

    def test_calibrator_integration(self):
        pg = PostGovernance()
        traj = _traj((0, "fail"), (0, "fail"), (2, "success"))
        for _ in range(3):
            pg.process(_task(), traj, {"status": "completed"})
        # 3 次同类成功 → 推荐入场层升到 2
        self.assertEqual(pg.recommend_entry_layer("patch"), 2)

    def test_pattern_compiled_via_process(self):
        pg = PostGovernance()
        traj = _traj((0, "fail"), (2, "success"))
        for _ in range(3):
            attr = pg.process(_task(), traj, {"status": "completed"})
        self.assertTrue(attr["repeated_pattern"])
        self.assertIn("compiled_skill", attr)
        self.assertEqual(len(pg.compiler.skills), 1)

    def test_failed_solves_do_not_compile(self):
        pg = PostGovernance()
        traj = _traj((0, "fail"), (1, "fail"))
        for _ in range(4):
            attr = pg.process(_task(), traj, {"status": "escalated"})
        # 失败轨迹也会累计签名（负模式同样有价值）, 但本实现只在 process 时记录
        # 推荐入场层不受失败记录影响
        self.assertEqual(pg.recommend_entry_layer("patch"), 0)

    def test_empty_trajectory_defaults(self):
        pg = PostGovernance()
        attr = pg.process(_task(), [], {"status": "completed"})
        self.assertEqual(attr["entry_layer"], 0)
        self.assertEqual(attr["exit_layer"], 0)

    def test_thread_safe_process(self):
        pg = PostGovernance()
        errors = []
        def worker(wid):
            try:
                for i in range(50):
                    pg.process(build_task(f"T{wid}", "d", ["f"]),
                               _traj((0, "fail"), (2, "success")),
                               {"status": "completed"})
            except Exception as e:  # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 每线程 50 次 × 8 = 400 条历史（窗口 50 截断, 但无崩溃）
        self.assertGreater(pg.calibrator.stats("patch")["samples"], 0)


class TestDataQualityReport(unittest.TestCase):
    """验收: 数据质量端点返回各产品的 SLA 满足情况。"""

    def test_fresh_signal_satisfied(self):
        r = data_quality_report({
            "SignalSnapshot": {"last_emit_age_s": 12, "ifc_enabled": True}})
        snap = r["SignalSnapshot"]
        self.assertTrue(snap["satisfied"])
        self.assertEqual(snap["failed_checks"], [])

    def test_stale_signal_fails_freshness(self):
        r = data_quality_report({
            "SignalSnapshot": {"last_emit_age_s": 300, "ifc_enabled": True}})
        snap = r["SignalSnapshot"]
        self.assertFalse(snap["satisfied"])
        self.assertIn("freshness", snap["failed_checks"])

    def test_ifc_disabled_fails_completeness(self):
        r = data_quality_report({
            "SignalSnapshot": {"last_emit_age_s": 5, "ifc_enabled": False}})
        snap = r["SignalSnapshot"]
        self.assertFalse(snap["satisfied"])
        self.assertIn("completeness", snap["failed_checks"])

    def test_missing_product_stats_fail(self):
        # 有 SLA 但无 live 统计 → 不满足（不能证明满足）
        r = data_quality_report({})
        snap = r["SignalSnapshot"]
        self.assertFalse(snap["satisfied"])

    def test_manifest_full_coverage(self):
        r = data_quality_report({"ManifestLine": {"coverage": 1.0}})
        self.assertTrue(r["ManifestLine"]["satisfied"])

    def test_manifest_partial_coverage_fails(self):
        r = data_quality_report({"ManifestLine": {"coverage": 0.98}})
        m = r["ManifestLine"]
        self.assertFalse(m["satisfied"])
        self.assertIn("coverage", m["failed_checks"])

    def test_summary_counts(self):
        r = data_quality_report({
            "SignalSnapshot": {"last_emit_age_s": 5, "ifc_enabled": True},
            "ManifestLine": {"coverage": 0.5}})
        summary = r["_summary"]
        self.assertEqual(summary["products"], 2)
        self.assertEqual(summary["satisfied"], 1)
        self.assertIn("generated_at", summary)

    def test_products_without_sla_skipped(self):
        r = data_quality_report({})
        # Task/Patch 等无 SLA 的契约不出现在报告里
        self.assertNotIn("Task", r)
        self.assertNotIn("Patch", r)
        self.assertIn("SignalSnapshot", r)  # 有 SLA 的出现


class TestOrchestratorWiring(unittest.TestCase):
    """编排器结果 → 后治理的契约对接（层级轨迹从子任务状态合成）。"""

    def test_orchestrator_result_feeds_governance(self):
        from protocol_orchestrator import ProtocolOrchestrator
        from protocol_types import build_patch
        from signal_types import build_signal_snapshot

        orch = ProtocolOrchestrator(executor=lambda st: build_patch(
            task_id=st["id"], diff="--- a\n+++ b\n@@\n-a\n+b",
            citations=[{"anchor": "u:1"}]))
        result = orch.solve(build_task("T1", "d", ["f.py"]),
                            build_signal_snapshot(session_key="s"))

        # 层级轨迹合成: 每个子任务一次执行 = 一层跃迁（Phase 1 简化映射）
        traj = []
        for st in result["subtasks"]:
            if st["state"] == "passed":
                traj.append({"layer": 2, "outcome": "success"})  # memory 层成功
            else:
                traj.append({"layer": 0, "outcome": "fail"})

        pg = PostGovernance()
        attr = pg.process(_task(), traj, result)  # outcome 直接传编排器结果 dict
        self.assertTrue(attr["success"])  # "completed" 被识别为成功
        self.assertEqual(attr["exit_layer"], 2)

    def test_escalated_result_not_success(self):
        pg = PostGovernance()
        attr = pg.process(_task(), _traj((0, "fail")),
                          {"status": "escalated", "patches": []})
        self.assertFalse(attr["success"])


if __name__ == "__main__":
    unittest.main()
