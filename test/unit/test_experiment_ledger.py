#!/usr/bin/env python3
"""test_experiment_ledger.py — 实验台账工具测试(状态机/登记/隔离)。"""
import json
import os
import sys
import tempfile
import unittest

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import experiment_ledger as el  # noqa: E402


class TestExperimentLedger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="expledger_")
        self.path = os.path.join(self._tmp, "experiments.jsonl")

    def _run(self, *argv):
        return el.main(list(argv) + ["--path", self.path])

    def test_begin_note_end_lifecycle(self):
        self.assertEqual(self._run(
            "begin", "--id", "exp-x", "--hypothesis", "H1",
            "--treatment", "keep_messages=12,hbe_min_chars=8000",
            "--batch", "syn-30 --force"), 0)
        self.assertEqual(self._run("note", "--id", "exp-x", "--text", "pilot ok"), 0)
        self.assertEqual(self._run(
            "end", "--id", "exp-x", "--conclusion", "report.md"), 0)
        rows = [json.loads(l) for l in open(self.path, encoding="utf-8")]
        self.assertEqual([r["event"] for r in rows], ["begin", "note", "end"])
        self.assertEqual(rows[0]["treatment"]["keep_messages"], "12")
        self.assertEqual(rows[0]["status"], "running")
        self.assertEqual(rows[2]["status"], "done")

    def test_double_begin_rejected(self):
        self.assertEqual(self._run("begin", "--id", "e1", "--hypothesis", "h"), 0)
        self.assertEqual(self._run("begin", "--id", "e1", "--hypothesis", "h"), 1)
        # end 后可重开(续批)
        self.assertEqual(self._run("end", "--id", "e1", "--conclusion", "c"), 0)
        self.assertEqual(self._run("begin", "--id", "e1", "--hypothesis", "h2"), 0)

    def test_end_without_begin_rejected(self):
        self.assertEqual(self._run("end", "--id", "ghost", "--conclusion", "x"), 1)

    def test_list_empty_and_filtered(self):
        self.assertEqual(self._run("list"), 0)
        self._run("begin", "--id", "a", "--hypothesis", "h")
        self._run("begin", "--id", "b", "--hypothesis", "h")
        self.assertEqual(self._run("list", "--id", "b"), 0)


if __name__ == "__main__":
    unittest.main()
