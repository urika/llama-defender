#!/usr/bin/env python3
"""test_underpull_fix.py — PDC-L1/L2/L3 欠拉修复测试。

L1: fifo 折叠占位符含 ctx_recall 指引
L2: wasted-call / HARD BLOCK 提示改指向 ctx_recall
L3: manifest 跨批轮转(SESSION_GAP_SECONDS)
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import memory_stores
import proxy_state as _ps


class TestL1FoldedPlaceholder(unittest.TestCase):
    """折叠占位符必须携带 ctx_recall 召回指引（欠拉修复主通道）。"""

    def _truncate(self, messages):
        import truncation
        # fifo 策略(生产策略) + 小 keep 窗口触发真实截断
        # (keep_rounds 显式传参优先于全局 PROXY_CTX_KEEP_MESSAGES, 但入口
        #  校验用全局值判 below_limit → 需同时压低全局)
        orig = _ps.PROXY_CTX_KEEP_MESSAGES
        _ps.PROXY_CTX_KEEP_MESSAGES = 2
        try:
            return truncation.truncate_messages_if_needed(
                messages, session_id=None, strategy="fifo", keep_rounds=2)
        finally:
            _ps.PROXY_CTX_KEEP_MESSAGES = orig

    def test_placeholder_carries_recall_hint(self):
        # 12 条消息 > keep_rounds=2 → 触发截断 → 占位符应含 ctx_recall
        msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"message {i} " + "x" * 60} for i in range(12)]
        out = self._truncate(msgs)
        text = json.dumps(out, ensure_ascii=False)
        self.assertIn("ctx_recall", text)

    def test_placeholder_still_structured(self):
        # 高 drop 比例 + 有文件提及 → 结构化摘要保留 + 指引
        msgs = [{"role": "user", "content": "seed"}]
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/src/app.py"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "x" * 300}]}]})
        for i in range(4):
            msgs.append({"role": "user", "content": f"tail {i} " + "y" * 30})
        out = self._truncate(msgs)
        text = json.dumps(out, ensure_ascii=False)
        self.assertIn("ctx_recall", text)

    def test_hint_text_stable(self):
        """指引是静态文本——同输入两次截断产出一致(prefix-cache 稳定性)。"""
        msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"m{i} " + "x" * 60} for i in range(12)]
        out1 = self._truncate([dict(m) for m in msgs])
        out2 = self._truncate([dict(m) for m in msgs])
        self.assertEqual(json.dumps(out1), json.dumps(out2))


class TestL2Hints(unittest.TestCase):
    """reread 提示改指向 ctx_recall。"""

    def test_wasted_hint_points_to_ctx_recall(self):
        from tool_filter import _translate_tool_result_errors
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "Wasted call: file unchanged"}]}]
        out, counts = _translate_tool_result_errors(msgs)
        self.assertEqual(counts["wasted"], 1)
        text = str(out[0]["content"][0]["content"])
        self.assertIn("ctx_recall", text)
        # 保留 Bash 兜底语义(召回失败仍有出路)
        self.assertIn("Bash", text)

    def test_file_not_found_hint_unchanged(self):
        from tool_filter import _translate_tool_result_errors
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "File does not exist: /nope.py"}]}]
        out, counts = _translate_tool_result_errors(msgs)
        self.assertEqual(counts["file_not_found"], 1)
        text = str(out[0]["content"][0]["content"])
        self.assertIn("ls", text)  # 原指引保留

    def test_hard_block_text_carries_hint(self):
        # HARD BLOCK 是 pipeline.RereadDetector 的内联文本——直接断言文案
        import inspect
        from pipeline import RereadDetector
        src = inspect.getsource(RereadDetector)
        self.assertIn("ctx_recall", src)


class TestL3BatchRotation(unittest.TestCase):
    """manifest 跨批轮转。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l3test_")
        self.orig_diag = getattr(_ps, "_DIAG_DIR", None)
        _ps._DIAG_DIR = self.tmp
        memory_stores.MANIFEST.reset()

    def tearDown(self):
        _ps._DIAG_DIR = self.orig_diag
        memory_stores.MANIFEST.reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_stale_manifest(self, sid, age_hours):
        """写一个 N 小时前的 manifest 文件。"""
        d = os.path.join(self.tmp, "manifest")
        os.makedirs(d, exist_ok=True)
        ts = (datetime.now() - timedelta(hours=age_hours)).isoformat(
            timespec="seconds")
        path = os.path.join(d, f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn": 1, "reason": "fifo_drop",
                                "anchor": "r:old", "kind": "tool_result",
                                "tool": "Read", "head": "old content",
                                "size_chars": 100, "ts": ts},
                               ensure_ascii=False) + "\n")

    def test_fresh_file_not_rotated(self):
        self._write_stale_manifest("s-fresh", age_hours=0.1)
        memory_stores.MANIFEST.record_units(
            "s-fresh", 2, "fifo_drop",
            [{"anchor": "r:new", "kind": "tool_result", "role": "user",
              "tool": "Read", "handle": None, "size_chars": 5,
              "head": "new"}])
        d = os.path.join(self.tmp, "manifest")
        self.assertTrue(os.path.exists(os.path.join(d, "s-fresh.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(d, "s-fresh.jsonl.prev")))

    def test_stale_file_rotated_on_first_write(self):
        self._write_stale_manifest("s-stale", age_hours=3)  # > 2h 阈值
        memory_stores.MANIFEST.record_units(
            "s-stale", 2, "fifo_drop",
            [{"anchor": "r:new", "kind": "tool_result", "role": "user",
              "tool": "Read", "handle": None, "size_chars": 5,
              "head": "new"}])
        d = os.path.join(self.tmp, "manifest")
        self.assertTrue(os.path.exists(os.path.join(d, "s-stale.jsonl.prev")),
                        "旧文件应轮转为 .prev")
        # 当前 manifest 只含新行(旧内容不混入)
        lines = memory_stores.MANIFEST.lines("s-stale")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["anchor"], "r:new")

    def test_lines_read_also_rotates(self):
        self._write_stale_manifest("s-read", age_hours=5)
        # 不经 record, 直接 lines() → 也应轮转(重启后首查询路径)
        lines = memory_stores.MANIFEST.lines("s-read")
        self.assertEqual(lines, [])
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "manifest", "s-read.jsonl.prev")))

    def test_rotation_fail_open(self):
        # 破损 ts 不应崩溃
        d = os.path.join(self.tmp, "manifest")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "s-bad.jsonl"), "w") as f:
            f.write('{"turn": 1, "ts": "not-a-date"}\n')
            f.write("broken json\n")
        memory_stores.MANIFEST.record_units(
            "s-bad", 2, "fifo_drop",
            [{"anchor": "r:n", "kind": "tool_result", "role": "user",
              "tool": "", "handle": None, "size_chars": 1, "head": "x"}])
        lines = memory_stores.MANIFEST.lines("s-bad")
        self.assertEqual(len(lines), 1)  # 新行正常写入


if __name__ == "__main__":
    unittest.main()
