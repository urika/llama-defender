#!/usr/bin/env python3
"""test_underpull_fix.py — PDC-L1/L2/L3 欠拉修复 + 协议补全测试。

L1: fifo 折叠占位符含 ctx_recall 指引(+锚点样本)
L2: wasted-call / HARD BLOCK 提示改指向 ctx_recall
L3: manifest 跨批轮转(SESSION_GAP_SECONDS)
PDC+: 索引 head 扩容 240 / 原地压缩头标记
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
import memory_stores as ms
import ctx_recall as cr
import proxy_state as _ps


class TestL1FoldedPlaceholder(unittest.TestCase):
    """折叠占位符必须携带 ctx_recall 召回指引(欠拉修复主通道)。"""

    def _truncate(self, messages):
        import truncation
        orig = _ps.PROXY_CTX_KEEP_MESSAGES
        _ps.PROXY_CTX_KEEP_MESSAGES = 2
        try:
            return truncation.truncate_messages_if_needed(
                messages, session_id=None, strategy="fifo", keep_rounds=2)
        finally:
            _ps.PROXY_CTX_KEEP_MESSAGES = orig

    def test_placeholder_carries_recall_hint(self):
        msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"message {i} " + "x" * 60} for i in range(12)]
        out = self._truncate(msgs)
        self.assertIn("ctx_recall", json.dumps(out, ensure_ascii=False))

    def test_placeholder_still_structured(self):
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
        self.assertIn("ctx_recall", json.dumps(out, ensure_ascii=False))

    def test_hint_text_stable(self):
        """指引为静态文本——同输入两次截断产出一致(prefix-cache 稳定性)。"""
        msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"m{i} " + "x" * 60} for i in range(12)]
        out1 = self._truncate([dict(m) for m in msgs])
        out2 = self._truncate([dict(m) for m in msgs])
        self.assertEqual(json.dumps(out1), json.dumps(out2))

    def test_anchors_in_placeholder_when_no_file_mentions(self):
        """无文件提及但有无路径 tool_use 被丢 → 占位符给锚点查询键。

        用 OpenAI 格式(tool_calls 独立字段): tool_count/file_mentions
        均为 0(计数器只扫 Anthropic content 块), 但 unit_anchors 仍能
        提取锚点 → 走锚点分支。这是双协议端点的真实路径。
        """
        import truncation
        orig_head = _ps.PROXY_CTX_KEEP_HEAD
        orig_keep = _ps.PROXY_CTX_KEEP_MESSAGES
        _ps.PROXY_CTX_KEEP_HEAD = 0
        _ps.PROXY_CTX_KEEP_MESSAGES = 2
        try:
            msgs = [{"role": "user", "content": "seed"},
                    {"role": "assistant", "tool_calls": [
                        {"id": "call_anch1", "type": "function",
                         "function": {"name": "Bash", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "call_anch1",
                     "content": "out " + "y" * 200}]
            for i in range(4):
                msgs.append({"role": "user", "content": f"tail {i} " + "z" * 30})
            out = truncation.truncate_messages_if_needed(
                msgs, session_id=None, strategy="fifo", keep_rounds=2)
            text = json.dumps(out[0] if isinstance(out, tuple) else out,
                              ensure_ascii=False)
        finally:
            _ps.PROXY_CTX_KEEP_HEAD = orig_head
            _ps.PROXY_CTX_KEEP_MESSAGES = orig_keep
        self.assertIn("sample anchors", text)
        self.assertIn("u:call_anch1", text)  # 锚点即查询键


class TestCompressionMarker(unittest.TestCase):
    """原地压缩头标记(感知性协议: 只声明事实, 不承诺恢复)。"""

    def test_marker_prepended_on_compression(self):
        import truncation
        orig_clear = _ps.PROXY_CLEAR_ENABLED
        orig_comp = _ps.PROXY_COMPRESS_ENABLED
        orig_thr = _ps.PROXY_COMPRESS_THRESHOLD
        _ps.PROXY_CLEAR_ENABLED = False
        _ps.PROXY_COMPRESS_ENABLED = True
        _ps.PROXY_COMPRESS_THRESHOLD = 100  # big(≈4.8K chars) 必触发压缩
        try:
            big = ("line" + chr(10)) * 800 + "TAILMARKER"
            msgs = [{"role": "user", "content": "q"}]
            msgs.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": "tm1", "name": "Read",
                 "input": {"file_path": "/tmp/data.txt"}}]})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tm1",
                 "content": [{"type": "text", "text": big}]}]})
            msgs.append({"role": "user", "content": "analyze"})
            out = truncation._compress_content_pass(
                [dict(m) for m in msgs], stage_config={"frozen_head": 0})
            out_msgs = out[0] if isinstance(out, tuple) else out
            found = False
            for m in out_msgs:
                c = m.get("content")
                if isinstance(c, list):
                    for b in c:
                        if (isinstance(b, dict) and b.get("type") == "tool_result"
                                and "TAILMARKER" in str(b.get("content"))):
                            found = str(b["content"]).startswith("[ctx:")
            self.assertTrue(found, "压缩结果应带 [ctx:...] 头标记")
        finally:
            _ps.PROXY_CLEAR_ENABLED = orig_clear
            _ps.PROXY_COMPRESS_ENABLED = orig_comp
            _ps.PROXY_COMPRESS_THRESHOLD = orig_thr


class TestHeadIndexExpansion(unittest.TestCase):
    """ifc 索引 head 扩容 120→240: 概念词可被检索。"""

    def test_head_covers_200_plus_region(self):
        import ifc_metrics
        body = "A" * 200 + "QUANTUMUNIQUE" + "B" * 30
        msg = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_h1",
             "content": [{"type": "text", "text": body}]}]}
        units = ifc_metrics.unit_anchors(msg)
        self.assertTrue(any("QUANTUMUNIQUE" in (u.get("head") or "")
                            for u in units),
                        "head 240 应覆盖 200+ 字符区间的概念词")


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
        self.assertIn("Bash", text)  # 保留 Bash 兜底语义

    def test_file_not_found_hint_unchanged(self):
        from tool_filter import _translate_tool_result_errors
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "File does not exist: /nope.py"}]}]
        out, counts = _translate_tool_result_errors(msgs)
        self.assertEqual(counts["file_not_found"], 1)
        self.assertIn("ls", str(out[0]["content"][0]["content"]))

    def test_hard_block_text_carries_hint(self):
        import inspect
        from pipeline import RereadDetector
        self.assertIn("ctx_recall", inspect.getsource(RereadDetector))


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
        d = os.path.join(self.tmp, "manifest")
        os.makedirs(d, exist_ok=True)
        ts = (datetime.now() - timedelta(hours=age_hours)).isoformat(
            timespec="seconds")
        with open(os.path.join(d, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
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
              "tool": "Read", "handle": None, "size_chars": 5, "head": "n"}])
        d = os.path.join(self.tmp, "manifest")
        self.assertTrue(os.path.exists(os.path.join(d, "s-fresh.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(d, "s-fresh.jsonl.prev")))

    def test_stale_file_rotated_on_first_write(self):
        self._write_stale_manifest("s-stale", age_hours=3)
        memory_stores.MANIFEST.record_units(
            "s-stale", 2, "fifo_drop",
            [{"anchor": "r:new", "kind": "tool_result", "role": "user",
              "tool": "Read", "handle": None, "size_chars": 5, "head": "n"}])
        d = os.path.join(self.tmp, "manifest")
        self.assertTrue(os.path.exists(os.path.join(d, "s-stale.jsonl.prev")))
        lines = memory_stores.MANIFEST.lines("s-stale")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["anchor"], "r:new")

    def test_lines_read_also_rotates(self):
        self._write_stale_manifest("s-read", age_hours=5)
        self.assertEqual(memory_stores.MANIFEST.lines("s-read"), [])
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "manifest", "s-read.jsonl.prev")))

    def test_rotation_fail_open(self):
        d = os.path.join(self.tmp, "manifest")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "s-bad.jsonl"), "w") as f:
            f.write('{"turn": 1, "ts": "not-a-date"}\nbroken json\n')
        memory_stores.MANIFEST.record_units(
            "s-bad", 2, "fifo_drop",
            [{"anchor": "r:n", "kind": "tool_result", "role": "user",
              "tool": "", "handle": None, "size_chars": 1, "head": "x"}])
        self.assertEqual(len(memory_stores.MANIFEST.lines("s-bad")), 1)




class TestRecoverableCompression(unittest.TestCase):
    """可恢复压缩(2026-09-01): 压缩标记带 key, 原文寄存可取回。"""

    def setUp(self):
        self._diag = tempfile.mkdtemp(prefix="rc_")
        self._orig = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._diag
        ms.MANIFEST.reset()

    def tearDown(self):
        _ps._DIAG_DIR = self._orig
        ms.MANIFEST.reset()
        shutil.rmtree(self._diag, ignore_errors=True)

    def test_marker_carries_key_and_orig_recoverable(self):
        import truncation
        orig_clear, orig_comp, orig_thr = (_ps.PROXY_CLEAR_ENABLED,
                                           _ps.PROXY_COMPRESS_ENABLED,
                                           _ps.PROXY_COMPRESS_THRESHOLD)
        _ps.PROXY_CLEAR_ENABLED = False
        _ps.PROXY_COMPRESS_ENABLED = True
        _ps.PROXY_COMPRESS_THRESHOLD = 100
        sid = "rc-demo"
        _ps._SESSION_REQUEST_COUNT[sid] = 5
        try:
            big = chr(10).join(f"data row {i} value={i*7} status=ok"
                               for i in range(400)) + chr(10) + "TAILORIG9"
            msgs = [{"role": "user", "content": "q"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "call_orig9", "name": "Read",
                         "input": {"file_path": "/tmp/data.log"}}]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "call_orig9",
                         "content": [{"type": "text", "text": big}]}]},
                    {"role": "user", "content": "analyze"}]
            res = truncation._compress_content_pass(
                [dict(m) for m in msgs],
                stage_config={"frozen_head": 0}, session_id=sid)
            out_msgs = res[0] if isinstance(res, tuple) else res
            marker = ""
            for m in out_msgs:
                c = m.get("content")
                if isinstance(c, list):
                    for b in c:
                        if b.get("type") == "tool_result":
                            t = str(b.get("content"))
                            if t.startswith("[ctx:"):
                                marker = t.split(chr(10))[0]
            self.assertIn("key=r:call_orig9", marker, f"标记缺 key: {marker}")
            # 锚点直查(与工具描述承诺对齐)
            lines = cr.lookup(sid, "r:call_orig9", limit=5)
            self.assertTrue(lines and lines[0]["anchor"] == "r:call_orig9")
            # 原文取回(orig 寄存回退链, 分页: 首页含开头, 末页含结尾)
            txt0 = cr.recover_full_content(sid, "r:call_orig9", 5)
            self.assertTrue(txt0 and "data row 0" in txt0)
            txt_last = cr.recover_full_content(sid, "r:call_orig9", 5,
                                               offset=12000)
            self.assertTrue(txt_last and "TAILORIG9" in txt_last)
        finally:
            _ps.PROXY_CLEAR_ENABLED = orig_clear
            _ps.PROXY_COMPRESS_ENABLED = orig_comp
            _ps.PROXY_COMPRESS_THRESHOLD = orig_thr
            _ps._SESSION_REQUEST_COUNT.pop(sid, None)

    def test_keyless_marker_without_session(self):
        """无会话作用域 → 无 key 诚实标记(不承诺无法兑现的恢复)。"""
        import truncation
        orig_clear, orig_comp, orig_thr = (_ps.PROXY_CLEAR_ENABLED,
                                           _ps.PROXY_COMPRESS_ENABLED,
                                           _ps.PROXY_COMPRESS_THRESHOLD)
        _ps.PROXY_CLEAR_ENABLED = False
        _ps.PROXY_COMPRESS_ENABLED = True
        _ps.PROXY_COMPRESS_THRESHOLD = 100
        try:
            big = chr(10).join(f"row {i} status=ok" for i in range(400))
            msgs = [{"role": "user", "content": "q"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "tm2", "name": "Read",
                         "input": {"file_path": "/tmp/d.log"}}]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "tm2",
                         "content": [{"type": "text", "text": big}]}]},
                    {"role": "user", "content": "go"}]
            res = truncation._compress_content_pass(
                [dict(m) for m in msgs],
                stage_config={"frozen_head": 0}, session_id=None)
            out_msgs = res[0] if isinstance(res, tuple) else res
            marker = ""
            for m in out_msgs:
                c = m.get("content")
                if isinstance(c, list):
                    for b in c:
                        if b.get("type") == "tool_result":
                            t = str(b.get("content"))
                            if t.startswith("[ctx:"):
                                marker = t.split(chr(10))[0]
            self.assertTrue(marker.startswith("[ctx:"), marker)
            self.assertNotIn("key=", marker)
        finally:
            _ps.PROXY_CLEAR_ENABLED = orig_clear
            _ps.PROXY_COMPRESS_ENABLED = orig_comp
            _ps.PROXY_COMPRESS_THRESHOLD = orig_thr


if __name__ == "__main__":
    unittest.main()
