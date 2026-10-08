#!/usr/bin/env python3
"""test_epoch_rubric.py — R-CM-3 门控式 epoch 折叠（rubric）。"""
import unittest
from unittest.mock import patch

import proxy_state as _ps
import session_ledger
import context_engine as ce


class _Sess:
    """最小 stub：只需 maybe_epoch 门控路径与 build_epoch_rubric 读取的属性。"""
    def __init__(self, canonical, user_msgs=3, last_epoch_user_msgs=0):
        self.canonical = canonical
        self.user_msgs = user_msgs
        self.last_epoch_user_msgs = last_epoch_user_msgs
        self.compression_region = []

    def _trigger_scale(self):
        return 10 ** 9  # 恒超预算，强制进入触发分支


def _closed_msgs():
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/a.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "body"}]}]},
    ]


def _dangling_msgs():
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/a.py"}}]},
    ]


class TestEpochRubric(unittest.TestCase):
    def test_last_round_closed(self):
        self.assertTrue(ce._last_round_closed(_closed_msgs()))
        self.assertFalse(ce._last_round_closed(_dangling_msgs()))
        self.assertTrue(ce._last_round_closed([]))

    def test_build_rubric_progress(self):
        s = _Sess(_closed_msgs(), user_msgs=5, last_epoch_user_msgs=3)
        r = ce.build_epoch_rubric(s, session_key="")
        self.assertTrue(r["closed"])
        self.assertTrue(r["progress"])
        # 无新增轮 → progress False
        s2 = _Sess(_closed_msgs(), user_msgs=3, last_epoch_user_msgs=3)
        self.assertFalse(ce.build_epoch_rubric(s2, session_key="")["progress"])

    def test_build_rubric_stuck(self):
        with patch.object(session_ledger.LEDGER, "build_ledger_json",
                          lambda *a, **k: {"dup_queries": [
                              {"target": "/a.py", "count": 99}]}):
            r = ce.build_epoch_rubric(_Sess(_closed_msgs()), session_key="s")
        self.assertFalse(r["not_stuck"])
        with patch.object(session_ledger.LEDGER, "build_ledger_json",
                          lambda *a, **k: {"dup_queries": []}):
            r2 = ce.build_epoch_rubric(_Sess(_closed_msgs()), session_key="s")
        self.assertTrue(r2["not_stuck"])

    def test_maybe_epoch_gated_when_rubric_fails(self):
        s = _Sess(_closed_msgs())
        trig, final = ce.CanonicalSession.maybe_epoch(
            s, 1, 24, rubric={"closed": False, "progress": True, "not_stuck": True})
        self.assertFalse(trig)
        self.assertEqual(final, s.canonical)
        self.assertEqual(s.last_rubric["closed"], False)
        # 未折叠 → epoch_count 不增（stub 无该属性也说明未走到折叠段）
        self.assertFalse(hasattr(s, "epoch_count"))

    def test_maybe_epoch_rubric_none_skips_gate(self):
        # rubric=None + 未超预算 → 正常返回未触发（旧行为）
        s = _Sess(_closed_msgs())
        s._trigger_scale = lambda: 1  # 未超预算(1 <= 1)
        trig, final = ce.CanonicalSession.maybe_epoch(s, 1, 24, rubric=None)
        self.assertFalse(trig)


if __name__ == "__main__":
    unittest.main()
