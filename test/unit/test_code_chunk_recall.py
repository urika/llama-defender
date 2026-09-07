# -*- coding: utf-8 -*-
"""符号级分块召回的常驻功能测试（2026-09-07，符号增强三项的回归面）。

覆盖: focus 查询引导填充 / symbols 返回 / chunk 索引 sidecar 往返 /
方法级切片 / 锚#sym 恢复切片 / path::symbol 注记行 / #sym 容忍。
全部离线（_DIAG_DIR 重定向 + archive 种子），无后端依赖。
"""
import json
import os
import tempfile
import unittest

import proxy_state as _ps
import memory_stores
import ctx_recall as cr

CODE_LINES = [
    "import os", "", "class Connection:", "    def _connect(self):",
    "        return 'psrp-ok'", "", "    def _load_extras(self, extras):",
    "        return extras", "", "def unrelated():",
    "        pass", "", "def other(psrp_log):",
    "        return psrp_log",
]
CODE = "\n".join("%d\t%s" % (i + 1, l)
                 for i, l in enumerate(CODE_LINES))
CODE_PADDED = CODE + "\n" + "pad" * 3000  # 超出 4K 预算, 触发摘录选择


class TestCodeChunkRecall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="chunk_recall_")
        self._orig_dir = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._tmp
        os.makedirs(os.path.join(self._tmp, "archive"), exist_ok=True)

    def tearDown(self):
        _ps._DIAG_DIR = self._orig_dir
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_archive(self, session, tool_use_id, content):
        payload = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": content}]}]}
        path = os.path.join(_ps._DIAG_DIR, "archive", session + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"turn": 1,
                                "payload": json.dumps(payload)},
                               ensure_ascii=False) + "\n")

    def _seed_manifest_pair(self, session, path):
        memory_stores.MANIFEST.record_units(session, 1, "fifo_drop", [
            {"anchor": "u:call_x", "kind": "tool_use", "role": "assistant",
             "tool": "Read",
             "handle": {"type": "path", "value": path},
             "size_chars": 100},
            {"anchor": "r:call_x", "kind": "tool_result", "role": "user",
             "handle": None, "size_chars": len(CODE)}])

    # ---- focus 查询引导(增强②) ----
    def test_focus_terms_flip_fill_order(self):
        # 预算 350: 骨架(~150)之外只装得下一个块体——focus 决定装谁
        r = cr.structure_aware_excerpt(CODE_PADDED, "/repo/psrp.py", 350,
                                       focus_terms=["_load_extras"])
        self.assertEqual(r["strategy"], "ast")
        self.assertIn("_load_extras", r["text"])       # Connection 块体入选
        r2 = cr.structure_aware_excerpt(CODE_PADDED, "/repo/psrp.py", 350,
                                        focus_terms=["unrelated"])
        self.assertIn("return psrp_log", r2["text"])   # unrelated 块体入选
        self.assertNotIn("return extras", r2["text"])  # Connection 块体让位

    def test_symbols_returned_with_positions(self):
        r = cr.structure_aware_excerpt(CODE_PADDED, "/repo/psrp.py", 4000)
        names = [s["name"] for s in r["symbols"]]
        self.assertIn("Connection", names)
        self.assertIn("unrelated", names)
        for s in r["symbols"]:
            self.assertIn("line_start", s)
            self.assertIn("line_end", s)
            self.assertIn("offset", s)

    # ---- sidecar 往返(增强①) ----
    def test_chunk_index_roundtrip(self):
        exc = cr.structure_aware_excerpt(CODE_PADDED, "/repo/psrp.py", 4000)
        self.assertTrue(cr.chunk_index_store("t_cr", "r:call_x",
                                             "/repo/psrp.py", exc))
        idx = cr.chunk_index_load("t_cr", "r:call_x")
        self.assertIsNotNone(idx)
        self.assertEqual(idx["path"], "/repo/psrp.py")
        self.assertEqual(len(idx["symbols"]), len(exc["symbols"]))
        self.assertFalse(cr.chunk_index_store(
            "t_cr", "r:call_x", "/repo/psrp.py",
            {"text": "x", "strategy": "line", "symbols": []}))

    # ---- 方法级切片(增强③前置) ----
    def test_chunk_symbol_slice_method_level(self):
        sl = cr.chunk_symbol_slice("t_cr", "r:call_x", "_load_extras", CODE)
        self.assertIsNotNone(sl)
        self.assertIn("def _load_extras", sl)
        self.assertIn("return extras", sl)
        self.assertNotIn("def _connect", sl)

    # ---- 锚#sym 恢复切片(增强③) ----
    def test_recover_with_sym_suffix(self):
        self._seed_archive("t_crsym", "call_x", CODE)
        self._seed_manifest_pair("t_crsym", "/repo/psrp.py")
        exc = cr.structure_aware_excerpt(CODE, "/repo/psrp.py", 4000)
        cr.chunk_index_store("t_crsym", "r:call_x", "/repo/psrp.py", exc)
        got = cr.recover_full_content("t_crsym", "r:call_x#sym:_load_extras",
                                      None)
        self.assertIsNotNone(got)
        self.assertIn("[symbol: _load_extras", got)
        self.assertIn("return extras", got)
        self.assertNotIn("def _connect", got)

    # ---- path::symbol 注记行(增强③) ----
    def test_lookup_path_symbol_annotated(self):
        self._seed_archive("t_crps", "call_x", CODE)
        self._seed_manifest_pair("t_crps", "/repo/psrp.py")
        exc = cr.structure_aware_excerpt(CODE, "/repo/psrp.py", 4000)
        cr.chunk_index_store("t_crps", "r:call_x", "/repo/psrp.py", exc)
        got = cr.fts_search("t_crps", "/repo/psrp.py::_connect")
        self.assertEqual(len(got), 1)
        self.assertIn("源码: ctx_recall", got[0].get("head", ""))

    # ---- #sym 容忍(锚直查不受后缀影响) ----
    def test_anchor_query_tolerates_sym_suffix(self):
        self._seed_archive("t_crtol", "call_x", CODE)
        self._seed_manifest_pair("t_crtol", "/repo/psrp.py")
        got = cr.fts_search("t_crtol", "r:call_x#sym:_load_extras")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["anchor"], "r:call_x")


if __name__ == "__main__":
    unittest.main()
