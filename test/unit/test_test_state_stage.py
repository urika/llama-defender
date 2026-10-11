"""Unit tests for TestStateStage (K1 状态摘要 pin 常驻, 2026-10-10).

覆盖: 旗标门/pytest 计数解析(两种词序)/注入与原位刷新/aux 排除/无 pytest 不注入。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proxy_state as _ps
from pipeline import TestStateStage, _parse_pytest_summary


def _mk_ctx(msgs, sid="swe-test-1"):
    class _C:
        pass
    c = _C()
    c.messages = msgs
    c.session_id = sid
    return c


def _bash_use(cmd):
    return {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}


def _bash_result(text):
    return {"type": "tool_result", "content": [{"type": "text", "text": text}]}


def _read_use(fp):
    return {"type": "tool_use", "name": "Read", "input": {"file_path": fp}}



def _blk_text(m):
    ct = m.get("content")
    if isinstance(ct, str):
        return ct
    if isinstance(ct, list):
        return " ".join(x.get("text", "") for x in ct if isinstance(x, dict))
    return ""


class TestParseSummary(unittest.TestCase):
    def test_passed_then_failed(self):
        r = _parse_pytest_summary("8 passed, 3 failed in 1.2s")
        self.assertEqual(r["passed"], 8)
        self.assertEqual(r["failed"], 3)

    def test_failed_before_passed(self):
        r = _parse_pytest_summary("3 failed, 8 passed in 1.2s")
        self.assertEqual(r["passed"], 8)
        self.assertEqual(r["failed"], 3)

    def test_failed_names_collected(self):
        t = "=== SHORT ===\nFAILED test_a.py::x\nFAILED test_b.py::y\n2 failed, 5 passed\n"
        r = _parse_pytest_summary(t)
        self.assertEqual(sorted(r["failed_names"]), ["test_a.py::x", "test_b.py::y"])

    def test_no_counts_returns_none(self):
        self.assertIsNone(_parse_pytest_summary("no pytest output here"))


class TestTestStateStage(unittest.TestCase):
    def setUp(self):
        self.stage = TestStateStage()

    def _ctx(self, msgs, sid="swe-test-1"):
        return _mk_ctx(msgs, sid)

    def test_flag_off_no_injection(self):
        with patch.object(_ps, "PROXY_TEST_STATE_ENABLED", False):
            self.assertFalse(self.stage.should_run(self._ctx([])))

    def test_flag_on_and_aux_excluded(self):
        with patch.object(_ps, "PROXY_TEST_STATE_ENABLED", True):
            aux = _mk_ctx([], sid="swe-x::aux")
            self.assertTrue(self.stage.should_run(self._ctx([])))
            self.assertFalse(self.stage.should_run(aux))

    def test_injects_summary_block_once(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "fix it"}]},
            {"role": "assistant", "content": [_bash_use("PYTHONPATH=lib .venv/bin/pytest test_x.py")]},
            {"role": "user", "content": [_bash_result("3 failed, 8 passed in 1.2s")]},
        ]
        ctx = self._ctx(msgs)
        self.stage.process(ctx)
        def _blk_text(m):
            ct = m.get("content")
            if isinstance(ct, str):
                return ct
            if isinstance(ct, list):
                return " ".join(x.get("text", "") for x in ct if isinstance(x, dict))
            return ""
        blocks = [m for m in ctx.messages if "TEST-STATE" in _blk_text(m)]
        self.assertEqual(len(blocks), 1)
        self.assertIn("8 passed", _blk_text(blocks[0]))
        self.assertIn("3 failed", _blk_text(blocks[0]))

    def test_in_place_refresh_no_growth(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "fix"}]},
            {"role": "assistant", "content": [_bash_use("pytest a.py")]},
            {"role": "user", "content": [_bash_result("4 passed in 0.3s")]},
        ]
        ctx = self._ctx(msgs)
        self.stage.process(ctx)
        n1 = len(ctx.messages)
        self.stage.process(ctx)
        self.assertEqual(len(ctx.messages), n1, "重复调用不得增长消息数")

    def test_no_pytest_no_injection(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        ctx = self._ctx(msgs)
        self.stage.process(ctx)
        self.assertFalse(any("TEST-STATE" in json_dumps(m) for m in ctx.messages))

    def test_failed_names_surfaced(self):
        msgs = [
            {"role": "assistant", "content": [_bash_use("pytest test_x.py")]},
            {"role": "user", "content": [_bash_result(
                "FAILED test_x.py::test_one\nFAILED test_x.py::test_two\n2 failed in 0.4s")]},
        ]
        ctx = self._ctx(msgs)
        self.stage.process(ctx)
        blk = [m for m in ctx.messages if "TEST-STATE" in _blk_text(m)]
        self.assertTrue(blk and "test_x.py::test_one" in _blk_text(blk[0]))


def json_dumps(m):
    import json
    return json.dumps(m, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
