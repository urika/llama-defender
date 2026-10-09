#!/usr/bin/env python3
"""test_recall_symbol.py — R-CM-4 符号级检索增强（提取/解析/渲染/注入）。"""
import os
import tempfile
import unittest
from unittest.mock import patch

import proxy_state as _ps

import ctx_recall as cr


class _Completed:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class TestRecallSymbol(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rcm4_")
        self._saved = {k: getattr(_ps, k, None) for k in (
            "PROXY_RECALL_SYMBOL_ENABLED", "PROXY_RECALL_SYMBOL_TOOL",
            "PROXY_RECALL_SYMBOL_CMD", "PROXY_RECALL_SYMBOL_MAX_REFS",
            "PROXY_RECALL_SYMBOL_TIMEOUT_S", "PROXY_RECALL_SYMBOL_ROOT")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                setattr(_ps, k, v)

    # ---- _symbol_ident ----
    def test_ident_prefers_snake(self):
        self.assertEqual(cr._symbol_ident("看看 load_config 的用法"), "load_config")

    def test_ident_prefers_camel(self):
        self.assertEqual(cr._symbol_ident("explain HTTPClient now"), "HTTPClient")

    def test_ident_fallback_plain(self):
        self.assertEqual(cr._symbol_ident("openlibrary core lists"), "openlibrary")

    def test_ident_rejects_short_and_pathish(self):
        self.assertIsNone(cr._symbol_ident("a/b/c.py"))

    # ---- symbol_refs ----
    def test_symbol_refs_parses_grep(self):
        out = "src/a.py:12:def load_config(x):\n src/b.py:7:  load_config(y)\n"
        with patch.object(cr.subprocess, "run", return_value=_Completed(out)):
            refs = cr.symbol_refs("load_config", ["a.py", "b.py"], max_refs=5)
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["path"], "src/a.py")
        self.assertEqual(refs[0]["line"], 12)
        self.assertIn("load_config", refs[0]["text"])

    def test_symbol_refs_limit_and_failempty(self):
        out = "\n".join("f.py:%d:x" % i for i in range(1, 20))
        with patch.object(cr.subprocess, "run", return_value=_Completed(out)):
            refs = cr.symbol_refs("x", ["f.py"], max_refs=3)
        self.assertEqual(len(refs), 3)

    def test_symbol_refs_fail_open(self):
        with patch.object(cr.subprocess, "run", side_effect=OSError("no grep")):
            self.assertEqual(cr.symbol_refs("x", ["f.py"]), [])

    # ---- format_symbol_section ----
    def test_section_disabled_returns_empty(self):
        _ps.PROXY_RECALL_SYMBOL_ENABLED = False
        self.assertEqual(cr.format_symbol_section("load_config x", "s1"), "")

    def test_section_enabled_renders(self):
        _ps.PROXY_RECALL_SYMBOL_ENABLED = True
        _ps.PROXY_RECALL_SYMBOL_MAX_REFS = 8
        with patch.object(cr, "_candidate_files", return_value=["a.py"]), \
             patch.object(cr, "symbol_refs",
                          return_value=[{"path": "a.py", "line": 3, "text": "def load_config"}]), \
             patch.object(cr.subprocess, "run", return_value=_Completed("")):
            sec = cr.format_symbol_section("load_config 在哪", "s1")
        self.assertIn("[符号索引]", sec)
        self.assertIn("a.py:3", sec)

    def test_section_no_ident_empty(self):
        _ps.PROXY_RECALL_SYMBOL_ENABLED = True
        self.assertEqual(cr.format_symbol_section("a/b/c.py", "s1"), "")

    # ---- format_recall_result 注入 ----
    def test_format_recall_appends_symbol(self):
        _ps.PROXY_RECALL_SYMBOL_ENABLED = True
        with patch.object(cr, "format_symbol_section", return_value="[符号索引] X"):
            out = cr.format_recall_result(
                [], "load_config", session_key="s1")
        self.assertIn("[符号索引] X", out)

    def test_format_recall_disabled_no_symbol(self):
        _ps.PROXY_RECALL_SYMBOL_ENABLED = False
        out = cr.format_recall_result([], "load_config", session_key="s1")
        self.assertNotIn("[符号索引]", out)


if __name__ == "__main__":
    unittest.main()
