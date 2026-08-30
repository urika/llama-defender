#!/usr/bin/env python3
"""test_verification_chain.py — P3 Verify 职责链测试（三级/短路/裁剪）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from protocol_types import build_patch
from verification_chain import (
    MechanicalHandler, SemanticHandler, LedgerHandler,
    build_verification_chain,
)


def _good_patch(**extra):
    return build_patch(
        task_id="T1", diff="--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x\n+y",
        citations=[{"anchor": "u:t1", "quote": "old line"}],
        **extra)


class TestMechanicalHandler(unittest.TestCase):

    def test_good_patch_passes(self):
        verdict = MechanicalHandler().handle(_good_patch())
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["verify_level"], "mechanical")

    def test_empty_diff_fails(self):
        p = build_patch(task_id="T1", diff="", citations=[])
        verdict = MechanicalHandler().handle(p)
        self.assertFalse(verdict["passed"])
        failed = [c for c in verdict["checks"] if not c["passed"]]
        self.assertTrue(any(c["rule_name"] == "diff_validity" for c in failed))

    def test_missing_citation_anchor_fails(self):
        p = build_patch(task_id="T1", diff="some change",
                        citations=[{"quote": "no anchor"}])
        verdict = MechanicalHandler().handle(p)
        self.assertFalse(verdict["passed"])
        failed = [c for c in verdict["checks"] if not c["passed"]]
        self.assertTrue(any(c["rule_name"] == "citation_existence" for c in failed))

    def test_unknown_anchor_fails_when_known_set_given(self):
        p = build_patch(task_id="T1", diff="change",
                        citations=[{"anchor": "u:ghost"}])
        p["known_anchors"] = ["u:t1", "u:t2"]
        verdict = MechanicalHandler().handle(p)
        self.assertFalse(verdict["passed"])

    def test_known_anchor_passes(self):
        p = build_patch(task_id="T1", diff="change",
                        citations=[{"anchor": "u:t1"}])
        p["known_anchors"] = ["u:t1"]
        verdict = MechanicalHandler().handle(p)
        self.assertTrue(verdict["passed"])


class TestSemanticHandler(unittest.TestCase):

    def test_non_empty_relevant_output_passes(self):
        p = _good_patch()
        p["task_description"] = "fix the login handler timeout"
        p["output"] = "Fixed login handler by setting timeout=30"
        verdict = SemanticHandler().handle(p)
        self.assertTrue(verdict["passed"])

    def test_empty_output_fails(self):
        p = _good_patch()
        p["task_description"] = "fix login"
        p["output"] = ""
        verdict = SemanticHandler().handle(p)
        self.assertFalse(verdict["passed"])

    def test_off_topic_fails(self):
        p = _good_patch()
        p["task_description"] = "refactor authentication middleware"
        p["output"] = "The weather is nice today. Banana apple orange."
        verdict = SemanticHandler().handle(p)
        self.assertFalse(verdict["passed"])


class TestLedgerHandler(unittest.TestCase):

    def test_fail_open_without_data(self):
        verdict = LedgerHandler().handle(_good_patch())
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["verify_level"], "ledger")

    def test_high_d_ledger_passes(self):
        rec = {"n_probes": 10, "n_match": 9, "d_ledger": 0.9}
        verdict = LedgerHandler(reconcile_result=rec).handle(_good_patch())
        self.assertTrue(verdict["passed"])

    def test_low_d_ledger_fails(self):
        rec = {"n_probes": 10, "n_match": 3, "d_ledger": 0.3}
        verdict = LedgerHandler(reconcile_result=rec).handle(_good_patch())
        self.assertFalse(verdict["passed"])

    def test_zero_probes_fail_open(self):
        rec = {"n_probes": 0, "n_match": 0, "d_ledger": None}
        verdict = LedgerHandler(reconcile_result=rec).handle(_good_patch())
        self.assertTrue(verdict["passed"])


class TestChainAssembly(unittest.TestCase):

    def test_full_chain_levels(self):
        head = build_verification_chain()
        self.assertIsInstance(head, MechanicalHandler)
        self.assertIsInstance(head.successor, SemanticHandler)
        self.assertIsInstance(head.successor.successor, LedgerHandler)

    def test_short_circuit_on_mechanical_failure(self):
        # 机械级失败 → 语义级不执行（检查结果里无 semantic 级规则）
        p = build_patch(task_id="T1", diff="", citations=[])
        head = build_verification_chain()
        verdict = head.handle(p)
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["verify_level"], "mechanical")
        rule_names = {c["rule_name"] for c in verdict["checks"]}
        self.assertNotIn("topic_relevance", rule_names)

    def test_chain_passes_good_patch(self):
        p = _good_patch()
        p["task_description"] = "fix login handler"
        p["output"] = "login handler fixed with timeout"
        verdict = build_verification_chain().handle(p)
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["verify_level"], "ledger")

    def test_mechanical_only_chain(self):
        head = build_verification_chain(include_semantic=False, include_ledger=False)
        self.assertIsNone(head.successor)
        p = _good_patch()
        p["output"] = ""  # 语义会失败, 但语义被裁掉
        verdict = head.handle(p)
        self.assertTrue(verdict["passed"])

    def test_set_successor_chaining(self):
        h1 = MechanicalHandler()
        h2 = SemanticHandler()
        returned = h1.set_successor(h2)
        self.assertIs(returned, h2)


if __name__ == "__main__":
    unittest.main()
