#!/usr/bin/env python3
"""折叠召回线索单测（2026-09-03 folded-recall-cue 设计 §4）。

断言：A 路（fifo 占位符）与 B 路（epoch 面板）含字节一致的 RECALL_CUE；
查询键上限 6、去重保序；空键 fail-open（无键行但不断裂）。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import ctx_recall  # noqa: E402
from test.lib.config_fixture import patch_config  # noqa: E402
import truncation  # noqa: E402
import context_engine  # noqa: E402


def _pair(tid, text):
    return [
        {"role": "assistant",
         "content": [{"type": "tool_use", "id": tid, "name": "Read",
                      "input": {"file_path": "src/%s.py" % tid}}]},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": tid,
                      "content": [{"type": "text", "text": text}]}]},
    ]


def _msgs(n_pairs=6):
    msgs = [{"role": "user", "content": "head 基线"}]
    for i in range(n_pairs):
        msgs.extend(_pair("k%d" % i, "现场 %d" % i))
    msgs.append({"role": "user", "content": "tail 收尾"})
    return msgs


class TestRecallKeysLine(unittest.TestCase):
    def test_dedup_order_limit(self):
        keys = ["r:a", "u:b", "r:a", "", None, "r:c", "r:d", "r:e", "r:f", "r:g", "r:h"]
        line = ctx_recall.recall_keys_line(keys)
        self.assertTrue(line.startswith("Folded keys: "))
        items = line[len("Folded keys: "):].rstrip(".").split(", ")
        self.assertEqual(items, ["r:a", "u:b", "r:c", "r:d", "r:e", "r:f"])  # 去重保序，上限 6

    def test_empty_returns_empty(self):
        self.assertEqual(ctx_recall.recall_keys_line([]), "")
        self.assertEqual(ctx_recall.recall_keys_line(None), "")

    def test_custom_limit(self):
        line = ctx_recall.recall_keys_line(["r:1", "r:2", "r:3"], limit=2)
        self.assertIn("r:1", line)
        self.assertNotIn("r:3", line)


class TestPathACue(unittest.TestCase):
    def test_fifo_placeholder_contains_shared_cue(self):
        # KEEP_MESSAGES=4 → 14 条中裁 10 条，drop_ratio≈0.71>0.7，走键分支
        with patch_config(PROXY_CTX_LIMIT_ENABLED=True,
                          PROXY_CTX_TRUNCATE_STRATEGY="fifo",
                          PROXY_CTX_KEEP_MESSAGES=4,
                          PROXY_CTX_KEEP_HEAD=2):
            result, _ = truncation.truncate_messages_if_needed(
                _msgs(), session_id="cue_a_01")
        joined = json.dumps(result, ensure_ascii=False)
        self.assertIn(ctx_recall.RECALL_CUE, joined)
        self.assertIn("Folded keys:", joined)


class TestPathBCue(unittest.TestCase):
    def test_epoch_panel_contains_shared_cue(self):
        sess = context_engine.CanonicalSession("cue_b_01")
        sess.absorb(_msgs())
        out, _dm, _dt = sess._collapse(window_k=2, halve=False)  # L-71: 返回 (msgs, 流失计量)
        panels = [m for m in out if m.get("_ctx_engine_epoch")]
        self.assertTrue(panels)
        text = panels[0]["content"][0]["text"]
        self.assertIn(ctx_recall.RECALL_CUE, text)
        self.assertIn("Folded keys:", text)
        self.assertIn("r:k", text)  # 被收编轮次的 tool_result anchor

    def test_no_collect_no_panel_no_crash(self):
        sess = context_engine.CanonicalSession("cue_b_02")
        sess.absorb([{"role": "user", "content": "只有一轮"}])
        out, _dm, _dt = sess._collapse(window_k=4, halve=False)
        self.assertFalse([m for m in out if m.get("_ctx_engine_epoch")])


class TestCueContractL53(unittest.TestCase):
    """L-53 P1: RECALL_CUE 的 post-miss 契约修正（窗口优先序 + 不可得前置条件）。"""

    def test_window_first_order_and_conditional_give_up(self):
        cue = ctx_recall.RECALL_CUE
        self.assertNotIn("Recall first", cue)          # 回归锚：旧"召回优先"序已移除
        self.assertIn("look in your current context", cue)
        self.assertIn("last resort", cue)
        self.assertIn("confirmed absent from your current context", cue)  # IFC-11 前置条件

    def test_cue_keeps_single_source_header(self):
        # 唯一源头约定不变：A/B 两路仍按常量标识引用（字节一致）
        import truncation  # noqa: F401  (path A deferred import 不炸)
        self.assertTrue(ctx_recall.RECALL_CUE.startswith(
            "Full text of folded content is preserved"))


if __name__ == "__main__":
    unittest.main()
