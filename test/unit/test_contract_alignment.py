#!/usr/bin/env python3
"""test_contract_alignment.py — 契约注册表与协议对齐验证。"""
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contract_registry import (
    CONTRACT_REGISTRY, validate_contract, get_consumers,
    get_producer_contracts, list_contracts,
)
from signal_types import build_signal_snapshot, SignalSnapshot
from protocol_types import (
    build_task, build_subtask, build_patch, build_verdict, build_escalation,
    Task, SubTask, Patch, Verdict, EscalationDecision,
)


class TestContractRegistry(unittest.TestCase):
    """契约注册表完整性"""

    def test_registry_not_empty(self):
        self.assertGreater(len(CONTRACT_REGISTRY), 0)

    def test_all_have_version(self):
        for name, meta in CONTRACT_REGISTRY.items():
            self.assertIn("version", meta, f"{name} 缺少 version")
            self.assertIsInstance(meta["version"], int)
            self.assertGreater(meta["version"], 0)

    def test_all_have_module(self):
        for name, meta in CONTRACT_REGISTRY.items():
            self.assertIn("module", meta, f"{name} 缺少 module")

    def test_all_have_join_keys(self):
        for name, meta in CONTRACT_REGISTRY.items():
            self.assertIn("join_keys", meta, f"{name} 缺少 join_keys")

    def test_list_contracts(self):
        contracts = list_contracts()
        self.assertIn("SignalSnapshot", contracts)
        self.assertIn("Task", contracts)
        self.assertIn("Patch", contracts)
        self.assertIn("Verdict", contracts)
        self.assertIn("ManifestLine", contracts)


class TestProtocolAlignment(unittest.TestCase):
    """协议与契约的对齐关系"""

    def test_signal_consumed_by_decompose_and_escalate(self):
        consumers = get_consumers("SignalSnapshot")
        self.assertIn("P1_decompose", consumers)
        self.assertIn("P5_escalate", consumers)

    def test_patch_produced_by_execute_consumed_by_verify(self):
        info = get_producer_contracts("P2_execute")
        self.assertIn("Patch", info["produces"])
        consumers = get_consumers("Patch")
        self.assertIn("P3_verify", consumers)

    def test_verdict_produced_by_verify_consumed_by_escalate(self):
        info = get_producer_contracts("P3_verify")
        self.assertIn("Verdict", info["produces"])
        consumers = get_consumers("Verdict")
        self.assertIn("P5_escalate", consumers)

    def test_manifest_consumed_by_recall(self):
        consumers = get_consumers("ManifestLine")
        self.assertIn("P4_recall", consumers)

    def test_all_protocols_have_contracts(self):
        for protocol in ["P1_decompose", "P2_execute", "P3_verify",
                         "P4_recall", "P5_escalate"]:
            info = get_producer_contracts(protocol)
            self.assertTrue(info["produces"] or info["consumes"],
                          f"{protocol} 无契约关联")

    def test_subtask_produced_by_decompose_consumed_by_execute(self):
        info = get_producer_contracts("P1_decompose")
        self.assertIn("SubTask", info["produces"])
        consumers = get_consumers("SubTask")
        self.assertIn("P2_execute", consumers)


class TestSignalTypes(unittest.TestCase):
    """Signal 类型工厂和验证"""

    def test_build_signal_snapshot(self):
        snap = build_signal_snapshot(
            h_be=1.2, retention=0.8, ile=True, session_key="test", turn=5)
        self.assertEqual(snap["h_be"], 1.2)
        self.assertEqual(snap["retention"], 0.8)
        self.assertTrue(snap["ile"])
        self.assertEqual(snap["contract_version"], 1)

    def test_signal_snapshot_defaults(self):
        snap = build_signal_snapshot()
        self.assertIsNone(snap["h_be"])
        self.assertFalse(snap["ile"])
        self.assertEqual(snap["reread_pressure"], 0)

    def test_validate_signal_snapshot(self):
        snap = build_signal_snapshot(session_key="test", turn=1)
        errors = validate_contract(snap, "SignalSnapshot")
        # join_keys 存在即可（total=False 所有字段可选）
        self.assertEqual(errors, [])

    def test_validate_unknown_contract(self):
        errors = validate_contract({}, "NonExistent")
        self.assertGreater(len(errors), 0)


class TestProtocolTypes(unittest.TestCase):
    """Protocol 类型的工厂函数"""

    def test_build_task(self):
        task = build_task("t1", "Fix bug", ["src/a.py"])
        self.assertEqual(task["id"], "t1")
        self.assertEqual(task["description"], "Fix bug")
        self.assertEqual(task["input_files"], ["src/a.py"])
        self.assertEqual(task["contract_version"], 1)

    def test_build_subtask(self):
        task = build_task("t1", "Fix bug", ["src/a.py"])
        sub = build_subtask(task, [], [], 30000)
        self.assertEqual(sub["id"], "t1")
        self.assertEqual(sub["budget_tokens"], 30000)
        self.assertEqual(sub["contract_version"], 1)

    def test_build_patch(self):
        patch = build_patch("t1", "- old\n+ new",
                           [{"file": "a.py", "quote": "old", "line_range": [1, 1], "context": "fix"}])
        self.assertEqual(patch["task_id"], "t1")
        self.assertEqual(len(patch["citations"]), 1)

    def test_build_verdict(self):
        v = build_verdict("p1", True, [])
        self.assertTrue(v["passed"])
        self.assertEqual(v["verify_level"], "mechanical")

    def test_build_escalation(self):
        e = build_escalation("t1", "retry", "verification_failed")
        self.assertEqual(e["action"], "retry")
        self.assertEqual(e["attempt_count"], 0)

    def test_validate_task(self):
        task = build_task("t1", "Fix bug", ["src/a.py"])
        errors = validate_contract(task, "Task")
        self.assertEqual(errors, [])  # join_keys "id" 存在

    def test_validate_task_missing_join_key(self):
        task = {"description": "no id"}
        errors = validate_contract(task, "Task")
        self.assertGreater(len(errors), 0)


if __name__ == "__main__":
    unittest.main()
