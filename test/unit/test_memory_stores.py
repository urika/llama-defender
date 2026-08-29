#!/usr/bin/env python3
"""test_memory_stores.py — R10.1 ManifestStore 测试(记录/上限/落盘重建)。"""
import os
import tempfile
import unittest

import proxy_state as _ps

import memory_stores as ms


def _dropped_msgs():
    return [
        {"role": "user", "content": [{"type": "text", "text": "早期任务说明" + "x" * 300}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/src/a.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "file body" * 100}]}]},
    ]


class TestManifestStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ifc_manifest_")
        self._orig_dir = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._tmp
        ms.MANIFEST.reset()

    def tearDown(self):
        _ps._DIAG_DIR = self._orig_dir
        ms.MANIFEST.reset()

    def test_record_and_lines_shape(self):
        n = ms.record_dropped_messages("sess_m1", turn=3, reason="fifo_drop",
                                       messages=_dropped_msgs())
        self.assertEqual(n, 3)  # text + tool_use + tool_result 三单元
        lines = ms.MANIFEST.lines("sess_m1")
        self.assertEqual(len(lines), 3)
        tool_lines = [l for l in lines if l["kind"] == "tool_use"]
        self.assertEqual(tool_lines[0]["tool"], "Read")
        self.assertEqual(tool_lines[0]["handle"], {"type": "path", "value": "/src/a.py"})
        self.assertEqual(tool_lines[0]["reason"], "fifo_drop")
        self.assertEqual(tool_lines[0]["turn"], 3)
        self.assertIn("u:t1", [l["anchor"] for l in lines])

    def test_persistence_rebuild_after_memory_reset(self):
        ms.record_dropped_messages("sess_m2", turn=1, reason="fifo_drop",
                                   messages=_dropped_msgs())
        self.assertTrue(os.path.exists(
            os.path.join(self._tmp, "manifest", "sess_m2.jsonl")))
        ms.MANIFEST.reset()  # 模拟重启(内存丢失)
        self.assertEqual(ms.MANIFEST.count("sess_m2"), 3)

    def test_disabled_flag_blocks_recording(self):
        old = getattr(_ps, "PROXY_PD_ENABLED", True)
        _ps.PROXY_PD_ENABLED = False
        try:
            n = ms.record_dropped_messages("sess_m3", turn=1, reason="fifo_drop",
                                           messages=_dropped_msgs())
            self.assertEqual(n, 0)
            self.assertEqual(ms.MANIFEST.count("sess_m3"), 0)
        finally:
            _ps.PROXY_PD_ENABLED = old

    def test_per_session_line_cap(self):
        old_cap = ms.MAX_LINES_PER_SESSION
        ms.MAX_LINES_PER_SESSION = 4
        try:
            for _ in range(3):  # 每批 3 行 → 共 9,只留最后 4
                ms.record_dropped_messages("sess_m4", turn=1, reason="fifo_drop",
                                           messages=_dropped_msgs())
            self.assertEqual(ms.MANIFEST.count("sess_m4"), 4)
        finally:
            ms.MAX_LINES_PER_SESSION = old_cap

    def test_unknown_session_empty(self):
        self.assertEqual(ms.MANIFEST.lines("nope"), [])
        self.assertEqual(ms.MANIFEST.count("nope"), 0)


if __name__ == "__main__":
    unittest.main()
