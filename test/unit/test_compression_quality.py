import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TOOLS = os.path.join(_REPO_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import analyze_compression_quality as aq


class TestCompressionQualityAnalysis(unittest.TestCase):
    def _record(self, before=1000, after=700, status=200, flags=None,
                semantic_original=500, semantic_saved=150, audits=0,
                reread=0, strategy="bm25"):
        return {
            "status": status,
            "quality_flags": flags or [],
            "reread_pressure": reread,
            "pipeline": {
                "content_compressor": {
                    "compression": {
                        "strategy": strategy,
                        "context_before_chars": before,
                        "context_after_chars": after,
                        "semantic_compressed": 1,
                        "semantic_original_chars": semantic_original,
                        "semantic_saved_chars": semantic_saved,
                        "semantic_audit_failures": audits,
                    }
                }
            },
        }

    def test_efficiency_and_fidelity_metrics(self):
        summary = aq.analyze_records([self._record()])
        self.assertEqual(summary["records"], 1)
        self.assertAlmostEqual(
            summary["efficiency"]["context_savings_ratio"]["avg"], 0.15)
        self.assertAlmostEqual(
            summary["efficiency"]["semantic_savings_ratio"]["avg"], 0.3)
        self.assertEqual(summary["fidelity_proxies"]["semantic_audit_pass_rate"], 1.0)
        self.assertTrue(summary["quality_gate"]["passed"])

    def test_quality_gate_flags_behavior_regression(self):
        summary = aq.analyze_records([
            self._record(flags=["loop_injected", "blocker_injected"], reread=3)
        ])
        self.assertFalse(summary["quality_gate"]["passed"])
        self.assertEqual(summary["fidelity_proxies"]["loop_injection_rate"], 1.0)
        self.assertEqual(summary["fidelity_proxies"]["blocker_rate"], 1.0)

    def test_audit_failure_is_not_hidden(self):
        summary = aq.analyze_records([self._record(audits=1)])
        self.assertEqual(summary["fidelity_proxies"]["semantic_audit_failures"], 1)
        self.assertEqual(summary["fidelity_proxies"]["semantic_audit_pass_rate"], 0.5)
        self.assertFalse(summary["quality_gate"]["passed"])

    def test_compare_reports_treatment_delta(self):
        baseline = aq.analyze_records([self._record(after=1000, semantic_saved=0)])
        treatment = aq.analyze_records([self._record(after=500, semantic_saved=250)])
        result = aq.compare(baseline, treatment)
        self.assertAlmostEqual(result["delta"]["context_savings_avg"], 0.25)
        self.assertAlmostEqual(result["delta"]["success_rate"], 0.0)

    def test_jsonl_loader_skips_malformed_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as handle:
            handle.write("not-json\n")
            handle.write(json.dumps(self._record()) + "\n")
            path = handle.name
        try:
            self.assertEqual(len(aq.load_jsonl(path)), 1)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
