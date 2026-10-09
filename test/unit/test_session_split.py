#!/usr/bin/env python3
"""test_session_split.py — 会话键按 system 分叉（PROXY_SESSION_KEY_SPLIT_BY_SYSTEM）。"""
import unittest

import proxy_state as _ps
from pipeline import PipelineContext, _split_session_key_by_system, _SPLIT_MARKER


def _mk(sid, system):
    return PipelineContext(session_id=sid, body={"system": system} if system is not None else {})


class TestSessionKeySplit(unittest.TestCase):
    def setUp(self):
        self._orig = getattr(_ps, "PROXY_SESSION_KEY_SPLIT_BY_SYSTEM", None)
        _ps.PROXY_SESSION_KEY_SPLIT_BY_SYSTEM = True

    def tearDown(self):
        if self._orig is not None:
            _ps.PROXY_SESSION_KEY_SPLIT_BY_SYSTEM = self._orig

    def test_disabled_no_split(self):
        _ps.PROXY_SESSION_KEY_SPLIT_BY_SYSTEM = False
        ctx = _mk("sweABC123-x", "main prompt")
        _split_session_key_by_system(ctx)
        self.assertEqual(ctx.session_id, "sweABC123-x")

    def test_split_appends_stable_hash(self):
        c1 = _mk("sweA", "main prompt")
        c2 = _mk("sweA", "main prompt")
        _split_session_key_by_system(c1)
        _split_session_key_by_system(c2)
        self.assertTrue(c1.session_id.startswith("sweA" + _SPLIT_MARKER))
        self.assertEqual(c1.session_id, c2.session_id)  # 同 system 确定性同 key
        self.assertEqual(len(c1.session_id.split(_SPLIT_MARKER)[1]), 8)

    def test_different_systems_diverge(self):
        a = _mk("sweA", "main prompt")
        b = _mk("sweA", "file search specialist")
        _split_session_key_by_system(a)
        _split_session_key_by_system(b)
        self.assertNotEqual(a.session_id, b.session_id)

    def test_block_list_system_supported(self):
        ctx = _mk("sweA", [{"type": "text", "text": "block system"}])
        _split_session_key_by_system(ctx)
        self.assertIn(_SPLIT_MARKER, ctx.session_id)

    def test_no_system_unchanged(self):
        ctx = PipelineContext(session_id="sweA", body={"messages": []})
        _split_session_key_by_system(ctx)
        self.assertEqual(ctx.session_id, "sweA")

    def test_no_double_suffix(self):
        ctx = _mk("sweA" + _SPLIT_MARKER + "abcd1234", "main prompt")
        _split_session_key_by_system(ctx)
        self.assertEqual(ctx.session_id.count(_SPLIT_MARKER), 1)


if __name__ == "__main__":
    unittest.main()
