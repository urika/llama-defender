#!/usr/bin/env python3
"""test_memory_store.py — R-CM-1 跨会话记忆（写/检索/分域/遗忘/导出）。"""
import os
import tempfile
import unittest

import proxy_state as _ps

import memory_store as ms


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rm1_mem_")
        self._orig = {
            "PROXY_MEMORY_DB_PATH": getattr(_ps, "PROXY_MEMORY_DB_PATH", None),
            "PROXY_MEMORY_ENABLED": getattr(_ps, "PROXY_MEMORY_ENABLED", None),
        }
        _ps.PROXY_MEMORY_DB_PATH = os.path.join(self._tmp, "memory.db")
        _ps.PROXY_MEMORY_ENABLED = True
        ms.MEMORY.reset()

    def tearDown(self):
        for k, v in self._orig.items():
            if v is not None:
                setattr(_ps, k, v)
        ms.MEMORY.reset()

    def test_add_and_search_fts(self):
        mid = ms.MEMORY.add("我们决定使用 Drizzle 而非 Prisma 作为 ORM",
                            kind="decision", project="proj_a")
        self.assertIsNotNone(mid)
        rows = ms.MEMORY.search("Drizzle", project="proj_a", top_k=5)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["kind"], "decision")
        self.assertIn("Drizzle", rows[0]["text"])

    def test_project_isolation(self):
        ms.MEMORY.add("alpha 项目事实", kind="fact", project="proj_a")
        ms.MEMORY.add("beta 项目事实", kind="fact", project="proj_b")
        a = ms.MEMORY.search("项目事实", project="proj_a", top_k=10)
        self.assertTrue(all("alpha" in r["text"] for r in a))
        self.assertFalse(any("beta" in r["text"] for r in a))

    def test_list_and_forget(self):
        mid = ms.MEMORY.add("临时条目", kind="note", project="p")
        self.assertEqual(len(ms.MEMORY.list(project="p")), 1)
        self.assertTrue(ms.MEMORY.forget(mid))
        self.assertEqual(len(ms.MEMORY.list(project="p")), 0)

    def test_empty_text_rejected(self):
        self.assertIsNone(ms.MEMORY.add("   ", kind="note", project="p"))

    def test_invalid_kind_falls_back_to_note(self):
        mid = ms.MEMORY.add("x", kind="bogus", project="p")
        rows = ms.MEMORY.list(project="p")
        self.assertEqual(rows[0]["kind"], "note")
        self.assertIsNotNone(mid)

    def test_project_normalization(self):
        self.assertEqual(ms.normalize_project(""), ms.DEFAULT_PROJECT)
        self.assertEqual(ms.normalize_project("a/b c"), "a_b_c")

    def test_stats_and_export(self):
        ms.MEMORY.add("f1", kind="fact", project="p")
        ms.MEMORY.add("d1", kind="decision", project="p")
        st = ms.MEMORY.stats()
        self.assertGreaterEqual(st["total"], 2)
        md = ms.MEMORY.export_markdown(project="p")
        self.assertIn("# Memory", md)
        self.assertIn("f1", md)

    def test_guard_off_by_default_returns_false(self):
        _ps.PROXY_MEMORY_ENABLED = False
        self.assertFalse(ms.enabled())


if __name__ == "__main__":
    unittest.main()
