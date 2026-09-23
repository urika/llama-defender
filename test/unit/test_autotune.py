"""Unit tests for autotune (BVR 自调优闭环 Phase 1).

Covers: proposal schema + validation (forbidden/legal surface/review targets),
rule-engine extractor (event -> proposal, dedup, value math), gate checks
(no_evidence / no_prediction / capability_raise / limits / malformed),
risk classification (low/medium/high), three-nature labels, journal append +
list, applied_counts, and apply/rollback with tiered signatures.
"""
import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import autotune as at


class TestValidation(unittest.TestCase):
    def test_forbidden_exact(self):
        self.assertEqual(at.validate_target_param("LLAMA_API_KEY")[0], False)
        self.assertEqual(at.validate_target_param("PROXY_ADMIN_TOKEN")[0], False)

    def test_forbidden_suffix(self):
        self.assertEqual(at.validate_target_param("ZHIPU_API_KEY")[0], False)
        # 后缀 _TOKEN 只命中真密钥, 不误伤 PROXY_OOM_SAFE_TOKENS
        self.assertEqual(at.validate_target_param("PROXY_OOM_SAFE_TOKENS")[0], True)

    def test_forbidden_prefix_governance(self):
        self.assertEqual(at.validate_target_param("GOVERNANCE_X")[0], False)

    def test_legal_reloadable(self):
        self.assertEqual(at.validate_target_param("PROXY_LOOP_THRESHOLD")[0], True)

    def test_outside_legal_surface(self):
        ok, reason = at.validate_target_param("SOME_STARTUP_PARAM",
                                              reloadable_set={"A"})
        self.assertEqual((ok, reason), (False, "outside_legal_surface"))

    def test_review_target_is_legal(self):
        self.assertEqual(at.validate_target_param("route_policy_review")[0], True)

    def test_reloadable_set_derived_from_registry(self):
        s = at.reloadable_param_set()
        self.assertIsNotNone(s)
        self.assertIn("PROXY_LOOP_THRESHOLD", s)


class TestExtractor(unittest.TestCase):
    VALS = {
        "PROXY_LOOP_THRESHOLD": "7",
        "PROXY_CTX_KEEP_MESSAGES": "80",
        "PROXY_OOM_SAFE_TOKENS": "600000",
    }

    def test_each_rule_emits_proposal(self):
        events = [
            {"type": "loop", "ref": "fact://loop/s/1"},
            {"type": "amnesia", "ref": "fact://am/s/1"},
            {"type": "prefix_break", "ref": "fact://pb/s/1"},
            {"type": "oom_evict", "ref": "fact://oom/s/1"},
            {"type": "dup_reread", "ref": "fact://dup/s/1"},
            {"type": "off_track", "ref": "fact://off/s/1"},
        ]
        props = at.extract_proposals_from_events(events, self.VALS)
        by_type = {}
        for p in props:
            by_type[p["evidence_ref"][0]] = p
        self.assertEqual(len(props), len(events))
        self.assertEqual(by_type["fact://loop/s/1"]["to"], "8")
        self.assertEqual(by_type["fact://am/s/1"]["to"], "90")
        self.assertEqual(by_type["fact://pb/s/1"]["to"], "90")
        self.assertEqual(by_type["fact://oom/s/1"]["to"], "595000")
        self.assertEqual(by_type["fact://dup/s/1"]["to"], "85")
        # off_track 是重划界提案: to=None, risk=high
        self.assertIsNone(by_type["fact://off/s/1"]["to"])
        self.assertEqual(by_type["fact://off/s/1"]["risk_class"], "high")

    def test_dedup_by_ref(self):
        events = [
            {"type": "loop", "ref": "fact://x/1"},
            {"type": "loop", "ref": "fact://x/1"},  # 同 ref 去重
        ]
        props = at.extract_proposals_from_events(events, self.VALS)
        self.assertEqual(len(props), 1)

    def test_unknown_type_skipped(self):
        props = at.extract_proposals_from_events(
            [{"type": "mystery", "ref": "fact://x/1"}], self.VALS)
        self.assertEqual(props, [])

    def test_missing_current_value_skipped(self):
        props = at.extract_proposals_from_events(
            [{"type": "loop", "ref": "fact://x/1"}], {})
        self.assertEqual(props, [])

    def test_proposal_schema(self):
        p = at.build_proposal("PROXY_LOOP_THRESHOLD", "7", "8",
                              ["fact://loop/s/1"], "loop_l1_rate", "-20%")
        self.assertEqual(p["three_nature"], "unverified_proposal")
        self.assertEqual(p["status"], "pending")
        self.assertEqual(p["risk_class"], "low")
        self.assertIn("proposal_id", p)
        self.assertTrue(p["proposal_id"].startswith("at_"))


class TestGate(unittest.TestCase):
    def _prop(self, **kw):
        base = dict(
            target_param="PROXY_LOOP_THRESHOLD", from_="7", to="8",
            evidence_ref=["fact://loop/s/1"],
            prediction={"metric": "loop_l1_rate", "delta": "-20%"},
            status="pending",
        )
        base.update(kw)
        return base

    def test_pass(self):
        ok, reason = at.gate_proposal(self._prop())
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_no_evidence(self):
        ok, reason = at.gate_proposal(self._prop(evidence_ref=[]))
        self.assertEqual((ok, reason), (False, "no_evidence"))

    def test_no_prediction(self):
        ok, reason = at.gate_proposal(self._prop(prediction={}))
        self.assertEqual((ok, reason), (False, "no_prediction"))

    def test_forbidden_param(self):
        ok, reason = at.gate_proposal(self._prop(target_param="LLAMA_API_KEY"))
        self.assertEqual((ok, reason), (False, "forbidden_param"))

    def test_outside_surface(self):
        ok, reason = at.gate_proposal(
            self._prop(target_param="NOPE_PARAM"),
            reloadable_set={"PROXY_LOOP_THRESHOLD"})
        self.assertEqual((ok, reason), (False, "outside_legal_surface"))

    def test_capability_raise_rejected(self):
        ok, reason = at.gate_proposal(
            self._prop(target_param="PROXY_MAX_CONCURRENT", to="2"))
        self.assertEqual((ok, reason), (False, "capability_raise"))

    def test_over_session_limit(self):
        ok, reason = at.gate_proposal(self._prop(), session_applied=1,
                                      session_limit=1)
        self.assertEqual((ok, reason), (False, "over_session_limit"))

    def test_over_daily_limit(self):
        ok, reason = at.gate_proposal(self._prop(), daily_applied=5,
                                      daily_limit=5)
        self.assertEqual((ok, reason), (False, "over_daily_limit"))

    def test_malformed(self):
        ok, reason = at.gate_proposal({"foo": "bar"})
        self.assertEqual((ok, reason), (False, "malformed"))

    def test_review_target_requires_review_prediction(self):
        ok, reason = at.gate_proposal(
            self._prop(target_param="route_policy_review", to=None,
                       prediction={"metric": "stagnation_advisor",
                                   "delta": "review"}))
        self.assertTrue(ok)


class TestRiskAndNature(unittest.TestCase):
    def test_risk_low(self):
        self.assertEqual(at.classify_risk("PROXY_LOOP_THRESHOLD"), "low")

    def test_risk_medium(self):
        self.assertEqual(at.classify_risk("PROXY_CTX_KEEP_MESSAGES"), "medium")
        self.assertEqual(at.classify_risk("PROXY_MAX_CONCURRENT"), "medium")

    def test_risk_high(self):
        self.assertEqual(at.classify_risk("route_policy_review"), "high")

    def test_nature_mapping(self):
        self.assertEqual(at.label_nature("applied"), "verified")
        self.assertEqual(at.label_nature("pending"), "unverified_proposal")
        self.assertEqual(at.label_nature("rejected"), "unverified_proposal")
        self.assertEqual(at.label_nature("rolled_back"), "unverified_proposal")
        self.assertEqual(at.label_nature("whatever"), "observed")


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_append_and_list(self):
        rec = at.journal_record(
            {"proposal_id": "at_1", "target_param": "P", "status": "pending"},
            status="applied", signature="auto", base_dir=self.tmp)
        self.assertEqual(rec["status"], "applied")
        self.assertEqual(rec["three_nature"], "verified")
        records = at.list_journal(self.tmp)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["proposal_id"], "at_1")

    def test_list_missing_dir(self):
        self.assertEqual(at.list_journal(os.path.join(self.tmp, "nope")), [])

    def test_applied_counts(self):
        at.journal_record({"proposal_id": "a", "status": "pending"},
                          status="applied", base_dir=self.tmp)
        counts = at.applied_counts(self.tmp)
        self.assertEqual(counts["daily"], 1)
        self.assertEqual(counts["session"], 1)


class TestApplyRollback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reloadable = {"PROXY_LOOP_THRESHOLD", "PROXY_CTX_KEEP_MESSAGES"}

    def test_low_auto(self):
        p = at.build_proposal("PROXY_LOOP_THRESHOLD", "7", "8",
                              ["fact://loop/s/1"], "loop_l1_rate", "-20%")
        ok, detail = at.apply_proposal(p, "auto", base_dir=self.tmp,
                                       reloadable_set=self.reloadable)
        self.assertTrue(ok)
        self.assertEqual(detail, "applied")
        # overrides.conf 记录
        with open(os.path.join(self.tmp, "overrides.conf")) as f:
            content = f.read()
        self.assertIn("PROXY_LOOP_THRESHOLD=8", content)

    def test_medium_requires_human(self):
        p = at.build_proposal("PROXY_CTX_KEEP_MESSAGES", "80", "90",
                              ["fact://am/s/1"], "amnesia_turn_gap", "-30%")
        ok, detail = at.apply_proposal(p, "auto", base_dir=self.tmp,
                                       reloadable_set=self.reloadable)
        self.assertEqual((ok, detail), (False, "human_signature_required"))
        ok, detail = at.apply_proposal(p, "human", base_dir=self.tmp,
                                       reloadable_set=self.reloadable)
        self.assertTrue(ok)

    def test_high_requires_human(self):
        p = at.build_proposal("route_policy_review", None, None,
                              ["fact://off/s/1"], "stagnation_advisor",
                              "review")
        ok, detail = at.apply_proposal(p, "auto", base_dir=self.tmp)
        self.assertEqual((ok, detail), (False, "human_signature_required"))
        ok, detail = at.apply_proposal(p, "human", base_dir=self.tmp)
        self.assertTrue(ok)

    def test_apply_rejected_records_journal(self):
        p = at.build_proposal("PROXY_LOOP_THRESHOLD", "7", "8",
                              [], "loop_l1_rate", "-20%")
        ok, detail = at.apply_proposal(p, "auto", base_dir=self.tmp,
                                       reloadable_set=self.reloadable)
        self.assertEqual((ok, detail), (False, "no_evidence"))
        recs = at.list_journal(self.tmp)
        self.assertEqual(recs[0]["status"], "rejected")
        self.assertEqual(recs[0]["reason"], "no_evidence")

    def test_rollback(self):
        p = at.build_proposal("PROXY_LOOP_THRESHOLD", "7", "8",
                              ["fact://loop/s/1"], "loop_l1_rate", "-20%")
        at.apply_proposal(p, "auto", base_dir=self.tmp,
                          reloadable_set=self.reloadable)
        ov_path = os.path.join(self.tmp, "overrides.conf")
        with open(ov_path) as f:
            before = f.read()
        self.assertIn(p["proposal_id"], before)
        ok, detail = at.rollback_proposal(p["proposal_id"], base_dir=self.tmp)
        self.assertEqual((ok, detail), (True, "rolled_back"))
        # 审计末条为 rolled_back
        recs = at.list_journal(self.tmp)
        self.assertEqual(recs[-1]["status"], "rolled_back")
        # overrides.conf 中该提案的注释行+参数行已被精确删除
        with open(ov_path) as f:
            after = f.read()
        self.assertNotIn(p["proposal_id"], after)
        self.assertNotIn("PROXY_LOOP_THRESHOLD=8", after)

    def test_rollback_not_found(self):
        ok, detail = at.rollback_proposal("at_nope", base_dir=self.tmp)
        self.assertEqual((ok, detail), (False, "not_found"))


class TestProposalId(unittest.TestCase):
    def test_format(self):
        pid = at.make_proposal_id("2026-09-16T10:00:00")
        self.assertTrue(pid.startswith("at_20260916100000_"))
        # 同一秒两次调用不撞车
        pid2 = at.make_proposal_id("2026-09-16T10:00:00")
        self.assertNotEqual(pid, pid2)


if __name__ == "__main__":
    unittest.main()