#!/usr/bin/env python3
"""DEF-308 轨道①场景验收：发送视图字节稳定化（2026-09-06）。

复刻 s38beef8 实测模式：客户端 SDK 每轮把旧 tool_result 墓碑化（改写历史）
→ 修复前视图逐轮收缩/回写 → 后端 hybrid 整条匹配 MISS → 每轮 46K 全额冷
prefill。修复后（PROXY_CTX_VIEW_STABLE_ENABLED）engine absorb 跳过已应答
exchange 的改写副本 → 视图只增不缩 → 前缀字节逐位稳定。

场景：
  VS1 前缀字节稳定   改写轮的视图 = 前轮视图 + 纯追加（字节级断言）
  VS2 完整版保留     已应答 exchange 的完整 tool_result 不被改写副本挤掉
  VS3 混合消息剥离   改写副本混有新文本 → 剥离已应答块, 新文本照常追加
  VS4 新结果照常     未应答 tid 的结果正常追加（不误伤）
  VS5 旗标关回旧轨   默认关时行为不变（副本照旧追加）
  VS6 keep-first     重复 tool_result 场景下 stage 19 保留首个完整版
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import proxy_state as _ps  # noqa: E402
import context_engine  # noqa: E402
import truncation  # noqa: E402
from test.lib.config_fixture import patch_config  # noqa: E402
from test.lib import state_fixture as sf  # noqa: E402

TID = "call_psrp"
FULL = "1\t# Copyright (c) 2018 Ansible\n" + "x" * 400
TOMBSTONE = {"type": "tool_result", "tool_use_id": TID,
             "content": [{"type": "text", "text":
                          "Tool result was not provided in the conversation history."}]}


def _client_turn1():
    return [
        {"role": "user", "content": [{"type": "text", "text": "start"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": TID, "name": "Read",
             "input": {"file_path": "/repo/psrp.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": TID,
             "content": [{"type": "text", "text": FULL}]}]},
    ]


def _client_turn2_rewritten():
    """SDK 改写: 旧 result 换墓碑 + 追加新一轮问答。"""
    return [
        {"role": "user", "content": [{"type": "text", "text": "start"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": TID, "name": "Read",
             "input": {"file_path": "/repo/psrp.py"}}]},
        {"role": "user", "content": [TOMBSTONE]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "I have the content"}]},
        {"role": "user", "content": [{"type": "text", "text": "continue"}]},
    ]


class TestViewStable(unittest.TestCase):
    def setUp(self):
        self._diag = sf.isolated_diag()
        self._diag.__enter__()
        self.addCleanup(self._diag.__exit__, None, None, None)

    def _session(self):
        return context_engine.CanonicalSession("vs")

    def test_vs1_prefix_byte_stable(self):
        """VS1: 改写轮视图 = 前轮视图 + 纯追加（字节级）。"""
        sess = self._session()
        with patch_config(PROXY_CTX_VIEW_STABLE_ENABLED=True):
            v1, _, _ = sess.absorb(_client_turn1())
            snap1 = json.dumps(v1, sort_keys=True, ensure_ascii=False)
            v2, _, new = sess.absorb(_client_turn2_rewritten())
        snap2 = json.dumps(v2, sort_keys=True, ensure_ascii=False)
        self.assertTrue(snap2.startswith(snap1[:-1] + ",") or
                        snap2.startswith(snap1[:-1]),
                        "前缀被回写（字节不稳定）")
        self.assertGreater(new, 0)  # 新内容照常追加

    def test_vs2_full_version_kept(self):
        """VS2: 已应答 exchange 的完整 tool_result 不被改写副本挤掉。"""
        sess = self._session()
        with patch_config(PROXY_CTX_VIEW_STABLE_ENABLED=True):
            sess.absorb(_client_turn1())
            v2, _, _ = sess.absorb(_client_turn2_rewritten())
        whole = json.dumps(v2, ensure_ascii=False)
        self.assertIn("Copyright (c) 2018 Ansible", whole)  # 完整版仍在视图
        self.assertNotIn("Tool result was not provided", whole)  # 副本未入

    def test_vs3_mixed_message_stripped(self):
        """VS3: 改写副本混新文本 → 剥离已应答块, 新文本追加。"""
        sess = self._session()
        with patch_config(PROXY_CTX_VIEW_STABLE_ENABLED=True):
            sess.absorb(_client_turn1())
            mixed = [
                {"role": "user", "content": [
                    TOMBSTONE,
                    {"type": "text", "text": "note from client"}]},
            ]
            v2, _, new = sess.absorb(_client_turn1() + mixed)
        self.assertEqual(new, 1)  # 只追加剥离后的 1 条
        last = v2[-1]["content"]
        kinds = [b.get("type") for b in last if isinstance(b, dict)]
        self.assertNotIn("tool_result", kinds)  # 已应答块被剥离
        self.assertIn("text", kinds)

    def test_vs4_new_result_appended(self):
        """VS4: 未应答 tid 的结果照常追加（不误伤）。"""
        sess = self._session()
        with patch_config(PROXY_CTX_VIEW_STABLE_ENABLED=True):
            sess.absorb(_client_turn1())
            turn2 = _client_turn2_rewritten() + [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "call_new", "name": "Read",
                     "input": {"file_path": "/repo/other.py"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_new",
                     "content": [{"type": "text", "text": "fresh"}]}]},
            ]
            v2, _, new = sess.absorb(turn2)
        whole = json.dumps(v2, ensure_ascii=False)
        self.assertIn("fresh", whole)

    def test_vs5_flag_off_legacy(self):
        """VS5: 旗标关 → 旧轨（改写副本照旧追加, 契约不静默变化）。"""
        sess = self._session()
        with patch_config(PROXY_CTX_VIEW_STABLE_ENABLED=False):
            sess.absorb(_client_turn1())
            v2, _, _ = sess.absorb(_client_turn2_rewritten())
        whole = json.dumps(v2, ensure_ascii=False)
        self.assertIn("Tool result was not provided", whole)  # 副本照旧入栈

    def test_vs6_keep_first_duplicate(self):
        """VS6: stage 19 keep-first——重复 tool_result 保留首个完整版。"""
        msgs = _client_turn1() + [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": TID,
                 "content": [{"type": "text",
                              "text": "Tool result was not provided"}]}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "next"}]},
        ]
        fixed = truncation._fix_tool_pairings(msgs)
        whole = json.dumps(fixed, ensure_ascii=False)
        self.assertIn("Copyright (c) 2018 Ansible", whole)  # 首个完整版保留
        self.assertEqual(whole.count("Tool result was not provided"), 0)


if __name__ == "__main__":
    unittest.main()
