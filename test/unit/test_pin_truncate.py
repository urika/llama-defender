#!/usr/bin/env python3
"""R19 pin 单元测试：fifo 中段 pin 跳过（truncation）+ OOM 挂起语义（stage 17）。

契约（集成契约 §3.3 冻结版）：①预算 stage 0 强制（另行覆盖）②OOMSafetyFIFO
默认不豁免 → pin_suspended="oom_danger" ③仅本批消息保留生效，不回溯历史。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import proxy_state as _ps  # noqa: E402
from test.lib.config_fixture import patch_config  # noqa: E402
import truncation  # noqa: E402
import pipeline  # noqa: E402


def _msg(text, role="user"):
    return {"role": role, "content": text}


def _tool_result_pair(tid, text):
    """一对原子 tool_use/tool_result（TS-2 保护口径），result 锚 = r:<tid>。"""
    return [
        {"role": "assistant",
         "content": [{"type": "tool_use", "id": tid, "name": "Read",
                      "input": {"file_path": "src/%s.py" % tid}}]},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": tid,
                      "content": [{"type": "text", "text": text}]}]},
    ]


def _fifo_msgs():
    """6 对 tool_result 消息（12 条）：KEEP_MESSAGES=6/KEEP_HEAD=2 下中段必裁。"""
    msgs = [_msg("head 基线消息", role="user")]
    for i in range(6):
        msgs.extend(_tool_result_pair("t%d" % i, "现场内容 %d" % i))
    msgs.append(_msg("tail 收尾消息", role="user"))
    return msgs


class TestFifoPinSkip(unittest.TestCase):
    def test_pinned_middle_message_survives_fifo(self):
        with patch_config(PROXY_CTX_LIMIT_ENABLED=True,
                          PROXY_CTX_TRUNCATE_STRATEGY="fifo",
                          PROXY_CTX_KEEP_MESSAGES=6,
                          PROXY_CTX_KEEP_HEAD=2):
            result, stats = truncation.truncate_messages_if_needed(
                _fifo_msgs(), session_id="pin_test_01a",
                pinned_anchors=["r:t2"])
        joined = __import__("json").dumps(result, ensure_ascii=False)
        self.assertIn("现场内容 2", joined)              # pinned 保留
        self.assertNotIn("现场内容 0", joined)            # 非 pinned 中段照常裁
        self.assertIn("head 基线消息", joined)            # head 不动
        self.assertIn("tail 收尾消息", joined)            # tail 不动
        # 保留序: pinned 仍在 head 之后、tail 之前(append-only 顺序不变)
        idx_head = joined.index("head 基线消息")
        idx_pin = joined.index("现场内容 2")
        idx_tail = joined.index("tail 收尾消息")
        self.assertLess(idx_head, idx_pin)
        self.assertLess(idx_pin, idx_tail)
        # TS-2: 原子对完整保留——tool_use t2 也必须在(否则配对修复会裁孤儿)
        self.assertIn('"id": "t2"', joined)

    def test_no_pins_behaves_unchanged(self):
        with patch_config(PROXY_CTX_LIMIT_ENABLED=True,
                          PROXY_CTX_TRUNCATE_STRATEGY="fifo",
                          PROXY_CTX_KEEP_MESSAGES=6,
                          PROXY_CTX_KEEP_HEAD=2):
            r1, s1 = truncation.truncate_messages_if_needed(
                _fifo_msgs(), session_id="pin_test_02a")
            r2, s2 = truncation.truncate_messages_if_needed(
                _fifo_msgs(), session_id="pin_test_02b",
                pinned_anchors=[])
        self.assertEqual(__import__("json").dumps(r1), __import__("json").dumps(r2))


class TestOomPinSuspend(unittest.TestCase):
    def test_oom_drop_of_pinned_message_sets_suspended(self):
        stage = pipeline.OOMSafetyFIFO()
        ctx = pipeline.PipelineContext(
            request_id="r_test", session_id="pin_test_03",
            total_chars=5000,
            messages=_fifo_msgs() + [_msg("extra %d" % i) for i in range(8)],
            body={}, stage_config={"oom_safety": True})
        ctx.pinned_anchors = ["r:t3"]                     # r:t3 会落进被逐出的中段
        with patch_config(PROXY_CHARS_OOM_DANGER=500,
                          PROXY_OOM_SAFE_TOKENS=50,
                          PROXY_CTX_KEEP_HEAD=2,
                          PROXY_CTX_KEEP_TAIL=2):
            stage.process(ctx)
        self.assertEqual(getattr(ctx, "pin_suspended", ""), "oom_danger")
        self.assertGreater(ctx.oom_iterations, 0)

    def test_oom_without_pins_no_suspended(self):
        stage = pipeline.OOMSafetyFIFO()
        ctx = pipeline.PipelineContext(
            request_id="r_test", session_id="pin_test_04",
            total_chars=5000,
            messages=_fifo_msgs() + [_msg("extra %d" % i) for i in range(8)],
            body={}, stage_config={"oom_safety": True})
        with patch_config(PROXY_CHARS_OOM_DANGER=500,
                          PROXY_OOM_SAFE_TOKENS=50):
            stage.process(ctx)
        self.assertEqual(getattr(ctx, "pin_suspended", ""), "")


if __name__ == "__main__":
    unittest.main()
