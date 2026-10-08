#!/usr/bin/env python3
"""test_handoff.py — R-CM-2 自动交接（构建/渲染/落盘/蒸馏联动）。"""
import os
import tempfile
import unittest

import proxy_state as _ps

import handoff
import session_ledger


class TestHandoff(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rm2_ho_")
        self._orig = {
            "PROXY_HANDOFF_DIR": getattr(_ps, "PROXY_HANDOFF_DIR", None),
            "PROXY_HANDOFF_MAX_CHARS": getattr(_ps, "PROXY_HANDOFF_MAX_CHARS", None),
            "PROXY_MEMORY_ENABLED": getattr(_ps, "PROXY_MEMORY_ENABLED", None),
            "PROXY_MEMORY_DB_PATH": getattr(_ps, "PROXY_MEMORY_DB_PATH", None),
        }
        _ps.PROXY_HANDOFF_DIR = os.path.join(self._tmp, "handoff")
        _ps.PROXY_HANDOFF_MAX_CHARS = 3000
        _ps.PROXY_MEMORY_ENABLED = True
        _ps.PROXY_MEMORY_DB_PATH = os.path.join(self._tmp, "memory.db")
        self._orig_snap = session_ledger.LEDGER.snapshot_actions
        session_ledger.LEDGER.snapshot_actions = self._fake_actions

    def tearDown(self):
        session_ledger.LEDGER.snapshot_actions = self._orig_snap
        for k, v in self._orig.items():
            if v is not None:
                setattr(_ps, k, v)

    @staticmethod
    def _fake_actions(session_key, limit=200):
        return [
            {"turn": 1, "tool": "Read", "target": "/repo/a.py", "dup": 1},
            {"turn": 2, "tool": "Edit", "target": "/repo/a.py", "dup": 1},
            {"turn": 3, "tool": "Bash", "target": "pytest -q", "dup": 1},
            {"turn": 4, "tool": "Read", "target": "/repo/a.py", "dup": 3},
            {"turn": 5, "tool": "Write", "target": "/repo/b.py", "dup": 1},
        ]

    def test_build_fields(self):
        h = handoff.build("sess_x", goal="修复 pytest 失败")
        self.assertEqual(h["session_key"], "sess_x")
        self.assertEqual(h["goal"], "修复 pytest 失败")
        self.assertIn("/repo/a.py", h["files_touched"])
        self.assertIn("/repo/b.py", h["files_touched"])
        self.assertGreaterEqual(h["tools_used"].get("Read", 0), 2)
        self.assertEqual(h["turns"], 5)
        self.assertIn("/repo/a.py", h["repeated_targets"])

    def test_build_empty_ledger(self):
        session_ledger.LEDGER.snapshot_actions = lambda *a, **k: []
        h = handoff.build("sess_empty")
        self.assertEqual(h["files_touched"], [])
        self.assertEqual(h["turns"], 0)

    def test_render_block_contains_sections(self):
        h = handoff.build("sess_x", goal="目标A")
        handoff.apply_overrides(h, decisions=["用 Drizzle"], next_steps=["补测试"])
        blk = handoff.render_block(h)
        self.assertIn("HANDOFF", blk)
        self.assertIn("目标A", blk)
        self.assertIn("用 Drizzle", blk)
        self.assertIn("补测试", blk)
        self.assertIn("/repo/a.py", blk)

    def test_render_respects_cap(self):
        h = handoff.build("sess_x")
        h["decisions"] = ["d" * 500 for _ in range(20)]
        blk = handoff.render_block(h, max_chars=200)
        self.assertLessEqual(len(blk), 200)

    def test_store_and_latest_roundtrip(self):
        h = handoff.build("sess_y", goal="g")
        self.assertTrue(handoff.store(h))
        rec = handoff.latest("sess_y")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["goal"], "g")
        # markdown 文件已生成
        self.assertTrue(os.path.exists(
            os.path.join(_ps.PROXY_HANDOFF_DIR,
                         session_ledger.sanitize_session_key("sess_y") + ".md")))

    def test_latest_missing_returns_none(self):
        self.assertIsNone(handoff.latest("nope"))

    def test_distill_memory(self):
        import memory_store as ms
        ms.MEMORY.reset()
        h = handoff.build("sess_z")
        handoff.apply_overrides(h, decisions=["决策X"])
        n = handoff.distill_memory(h)
        self.assertGreaterEqual(n, 1)
        rows = ms.MEMORY.search("决策X", project=ms.DEFAULT_PROJECT)
        self.assertTrue(rows)

    def test_enabled_default_false(self):
        _ps.PROXY_HANDOFF_ENABLED = False
        self.assertFalse(handoff.enabled())


if __name__ == "__main__":
    unittest.main()
