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

    def test_multitoken_query_or_semantics(self):
        """R-CM-1 回归（2026-10-10 生产确诊）：FTS5 空格=隐式 AND，会话首条
        user 消息几百 token 全量 AND 永不满足 → 注入恒 0。修复后多 token
        query 必须 OR 召回。"""
        ms.MEMORY.add("决定使用 Drizzle ORM 方案", kind="decision", project="p2")
        ms.MEMORY.add("SWE 实例用 pytest 验证改动", kind="fact", project="p2")
        # 真实形态：首条 user 消息长且混有无关 token（环境序言等）
        big = ("Environment You are an agent pytest drizzle TODO list "
               "workspace files read tools bash editor " * 3)
        rows = ms.MEMORY.search(big, project="p2", top_k=5)
        self.assertEqual(len(rows), 2)
        kinds = {r["kind"] for r in rows}
        self.assertEqual(kinds, {"decision", "fact"})

    def test_multitoken_query_token_cap(self):
        """token 上限 24 + 长度优先：超长 query 不炸，长信号词不被同长
        filler 挤出 MATCH 串。"""
        ms.MEMORY.add("目标条目 alpha", kind="fact", project="p3")
        big = " ".join("tok%d" % i for i in range(30)) + " alpha"
        rows = ms.MEMORY.search(big, project="p3", top_k=5)
        self.assertTrue(any("alpha" in r["text"] for r in rows))

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
