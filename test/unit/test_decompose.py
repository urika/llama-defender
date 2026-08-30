#!/usr/bin/env python3
"""test_decompose.py — P1 Decompose 协议测试（判据/递归/哈希/契约对齐）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from decompose import (
    CapacityCriterion, Decomposer, EntropyCriterion, task_hash,
)
from protocol_types import build_task
from signal_types import build_signal_snapshot


def _big_task(n_files: int = 3, file_len: int = 60000):
    return build_task(
        task_id="T1",
        description="修复登录模块的三个文件",
        input_files=[f"src/file{i}.py " + "x" * file_len for i in range(n_files)],
        expected_output_type="patch",
    )


def _small_task():
    return build_task(
        task_id="T2",
        description="修复 typo",
        input_files=["src/small.py"],
        expected_output_type="patch",
    )


class TestCapacityCriterion(unittest.TestCase):

    def setUp(self):
        self.crit = CapacityCriterion(budget_tokens=10000)  # = 40000 chars

    def test_no_split_small_task(self):
        signal = build_signal_snapshot()
        self.assertFalse(self.crit.should_split(_small_task(), signal))

    def test_split_large_task(self):
        # 3 files × 60000 chars = 180000 chars ≈ 45000 tokens > 10000
        signal = build_signal_snapshot()
        self.assertTrue(self.crit.should_split(_big_task(), signal))

    def test_split_by_files(self):
        task = _big_task()
        subs = self.crit.split(task)
        self.assertEqual(len(subs), 3)
        # 每个子任务只含一个文件
        for s in subs:
            self.assertEqual(len(s["input_files"]), 1)
        # id 带文件序号
        self.assertIn(":f0", subs[0]["id"])
        # depth 递增
        self.assertEqual(subs[0].get("depth", 0), 1)

    def test_split_preserves_description(self):
        task = _big_task()
        subs = self.crit.split(task)
        for s in subs:
            self.assertEqual(s["description"], task["description"])


class TestEntropyCriterion(unittest.TestCase):

    def setUp(self):
        self.crit = EntropyCriterion(theta_h=0.9)

    def test_no_split_low_entropy(self):
        signal = build_signal_snapshot(h_be=0.3)
        self.assertFalse(self.crit.should_split(_big_task(), signal))

    def test_split_high_entropy(self):
        signal = build_signal_snapshot(h_be=0.95)
        self.assertTrue(self.crit.should_split(_big_task(), signal))

    def test_none_entropy_no_split(self):
        # Signal 层故障 → fail-open 不分解
        signal = build_signal_snapshot(h_be=None)
        self.assertFalse(self.crit.should_split(_big_task(), signal))

    def test_split_returns_single(self):
        # 熵判据不拆文件——返回原任务让编排器处理
        task = _big_task()
        subs = self.crit.split(task)
        self.assertEqual(len(subs), 1)


class TestDecomposer(unittest.TestCase):

    def setUp(self):
        self.dec = Decomposer()

    def test_atomic_task_passthrough(self):
        signal = build_signal_snapshot()
        subs = self.dec.decompose(_small_task(), signal)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["id"], "T2")

    def test_recursive_split(self):
        signal = build_signal_snapshot()
        subs = self.dec.decompose(_big_task(), signal)
        self.assertEqual(len(subs), 3)
        # 所有子任务为 SubTask 类型（含验证规则）
        for s in subs:
            self.assertIn("verification_rules", s)
            self.assertIn("dependencies", s)
            self.assertIn("budget_tokens", s)

    def test_subtask_has_verification_rules(self):
        signal = build_signal_snapshot()
        subs = self.dec.decompose(_small_task(), signal)
        rules = subs[0]["verification_rules"]
        # patch 类任务默认带 citation + diff 规则
        names = [r["name"] for r in rules]
        self.assertIn("citation_existence", names)
        self.assertIn("diff_validity", names)

    def test_text_task_rules(self):
        task = build_task("T3", "总结文档", ["doc.md"], expected_output_type="text")
        subs = self.dec.decompose(task, build_signal_snapshot())
        names = [r["name"] for r in subs[0]["verification_rules"]]
        self.assertIn("format_compliance", names)
        self.assertNotIn("diff_validity", names)

    def test_budget_positive(self):
        subs = self.dec.decompose(_small_task(), build_signal_snapshot())
        self.assertGreater(subs[0]["budget_tokens"], 0)

    def test_depth_guard_prevents_infinite_recursion(self):
        # 单文件超大 → 拆后仍是单文件 → 递归必须被 depth 守卫终止
        task = build_task(
            "T4", "单文件超大任务",
            input_files=["huge.py " + "y" * 500000])
        subs = self.dec.decompose(task, build_signal_snapshot())
        # 不死循环, 返回原任务（或其子任务）
        self.assertGreaterEqual(len(subs), 1)


class TestTaskHash(unittest.TestCase):

    def test_same_content_same_hash(self):
        t1 = build_task("A", "fix bug", ["f1.py", "f2.py"])
        t2 = build_task("B", "fix bug", ["f2.py", "f1.py"])  # 文件序无关
        self.assertEqual(task_hash(t1), task_hash(t2))

    def test_different_description_different_hash(self):
        t1 = build_task("A", "fix bug", ["f1.py"])
        t2 = build_task("B", "other bug", ["f1.py"])
        self.assertNotEqual(task_hash(t1), task_hash(t2))

    def test_different_files_different_hash(self):
        t1 = build_task("A", "fix bug", ["f1.py"])
        t2 = build_task("B", "fix bug", ["f9.py"])
        self.assertNotEqual(task_hash(t1), task_hash(t2))

    def test_hash_stable_length(self):
        h = task_hash(_small_task())
        self.assertEqual(len(h), 12)


class TestContractAlignment(unittest.TestCase):
    """Spec-A 契约对齐——SubTask 仍是 Task 子类, 字段兼容。"""

    def test_subtask_is_task(self):
        subs = Decomposer().decompose(_small_task(), build_signal_snapshot())
        self.assertIn("contract_version", subs[0])
        self.assertEqual(subs[0]["contract_version"], 1)

    def test_signal_fail_open(self):
        # 完全空信号 → 依然可分解（fail-open）
        subs = Decomposer().decompose(_small_task(), build_signal_snapshot())
        self.assertEqual(len(subs), 1)


if __name__ == "__main__":
    unittest.main()
