#!/usr/bin/env python3
"""test_rcm_metrics.py — R-CM-7 召回/注入采纳率聚合（admin_server 端点体）。"""
import unittest
from unittest.mock import patch

import session_ledger
from admin_server import _build_session_metrics_json


def _rec(turn, rcm=None, inj=None, ctx_recall=None):
    return {
        "turn": turn,
        "rcm": rcm,
        "injection_details": inj or {},
        "ctx_recall": ctx_recall,
    }


class TestRcmMetrics(unittest.TestCase):
    def _records(self):
        return [
            _rec(1, rcm={"memory": 1, "handoff": 0, "recall": 0},
                 inj={"memory": {"items": 1}}),
            _rec(2, rcm={"memory": 0, "handoff": 1, "recall": 0},
                 inj={"handoff": {"chars": 40}}),
            _rec(3, rcm={"memory": 0, "handoff": 0, "recall": 1},
                 inj={"auto_recall": {"target": "/a.py"}},
                 ctx_recall={"hits": 3, "misses": 1}),
            _rec(4, rcm={"memory": 0, "handoff": 0, "recall": 1},
                 inj={"auto_recall": {"target": "/b.py"}}),
        ]

    def test_counts_leadlag_and_adoption(self):
        led = {"dup_queries": [
            {"target": "/a.py", "count": 4, "last_turn": 5},   # 注入后再读 → 未采纳
            {"target": "/b.py", "count": 2, "last_turn": 3},   # 注入后未再读 → 采纳
        ]}
        with patch.object(session_ledger.LEDGER, "build_ledger_json",
                          lambda *a, **k: led):
            out = _build_session_metrics_json("s", self._records())
        rcm = out["rcm"]
        self.assertEqual(rcm["memory_injected_turns"], 1)
        self.assertEqual(rcm["handoff_injected_turns"], 1)
        self.assertEqual(rcm["recall_injected_turns"], 2)
        self.assertEqual(rcm["first_injection_turn"], 1)
        self.assertEqual(rcm["turns_after_first_injection"], 3)
        self.assertEqual(rcm["recall_adoption"]["adopted"], 1)
        self.assertEqual(rcm["recall_adoption"]["total"], 2)
        self.assertAlmostEqual(rcm["recall_adoption"]["rate"], 0.5)
        self.assertAlmostEqual(rcm["recall_hit_rate"], 0.75)

    def test_ledger_unavailable_adoption_null(self):
        with patch.object(session_ledger.LEDGER, "build_ledger_json",
                          side_effect=RuntimeError("no ledger")):
            out = _build_session_metrics_json("s", self._records())
        rcm = out["rcm"]
        self.assertEqual(rcm["recall_adoption"]["total"], 0)
        self.assertIsNone(rcm["recall_adoption"]["rate"])

    def test_no_injections(self):
        out = _build_session_metrics_json("s", [_rec(1), _rec(2)])
        rcm = out["rcm"]
        self.assertEqual(rcm["memory_injected_turns"], 0)
        self.assertIsNone(rcm["first_injection_turn"])
        self.assertIsNone(rcm["turns_after_first_injection"])
        self.assertIsNone(rcm["recall_hit_rate"])


if __name__ == "__main__":
    unittest.main()
