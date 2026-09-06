#!/usr/bin/env python3
"""墓碑召回场景测试（组装级，2026-09-06）。

与 test_auto_recall.py（零件单测）互补：按管线环节组装
12.5 → 13 → 14 → 15 → 17 → 18 → 19 → 20 真实 stage 段，
用完整报文（PipelineContext + 客户端 Anthropic 历史）驱动，
观测各环节出口——单测绿≠组装对（stage 19 会删悬空 tool_use，
与 stage 20 墓碑注入存在环节互斥的可能，组装实验才能显影）。

场景矩阵：
  SC1 组装探针     悬空调用+寄存 → 12.5 注入 + 19/20 真实形态
  SC2 双机制抑制   12.5 已回填的调用，stage 20 不再注记 hint
  SC3 末位豁免     末条 assistant 未决调用（在途）不触发
  SC4 无寄存       悬空但 manifest 无行 → 无注入、裸墓碑
  SC5 dup 优先     两旗标同开，dup 命中时墓碑路径本轮让位
  SC6 append-only  注入只追加尾部，既有历史字节不变
  SC7 aux 分域     ::aux-haiku 会话整段跳过
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import proxy_state as _ps  # noqa: E402
import memory_stores  # noqa: E402
import session_ledger  # noqa: E402
from pipeline import (  # noqa: E402
    AutoRecallStage,
    ContextTruncator,
    DateNormalizer,
    FormatConverter,
    HighDropRatioNotice,
    OOMSafetyFIFO,
    Pipeline,
    PipelineContext,
    PrefixRatioComputer,
    ToolPairingRepair,
    _TOMBSTONE_MARK,
)
from test.lib.config_fixture import patch_config  # noqa: E402
from test.lib import state_fixture as sf  # noqa: E402

TARGET = "/repo/src/loop_file.py"
CONTENT = "def loop():\n    return 42\n" * 20
TID = "call_deadbeef"


def _tombstone_content(cid):
    return json.dumps(
        {"error": "Tool result was not provided in the conversation history.",
         "tool_call_id": cid}, ensure_ascii=False)


def _plant_deposit(sid, tid):
    """寄存: manifest 行(r:<tid>) + archive 原文（写入期压缩的同款落盘）。"""
    pair = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Read",
             "input": {"file_path": TARGET}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": [{"type": "text", "text": CONTENT}]}]},
    ]
    memory_stores.record_dropped_messages(sid, 2, "compressed", pair)
    sf.plant_archive_tool_result(sid, 2, tid, CONTENT, diag_dir=_ps._DIAG_DIR)


class TombstoneScenarioBase(unittest.TestCase):
    """组装段 + 报文构造底座。"""

    STAGES = None  # 组装段（类级共享，stage 均无状态）

    @classmethod
    def setUpClass(cls):
        cls.STAGES = Pipeline([
            AutoRecallStage(),          # 12.5
            DateNormalizer(),           # 13
            ContextTruncator(),         # 14
            HighDropRatioNotice(),      # 15
            OOMSafetyFIFO(),            # 17
            PrefixRatioComputer(),      # 18
            ToolPairingRepair(),        # 19
            FormatConverter(),          # 20
        ])

    def setUp(self):
        self._diag = sf.isolated_diag()
        self._diag.__enter__()
        self.addCleanup(self._diag.__exit__, None, None, None)
        memory_stores.MANIFEST.reset()
        self._orig_ledger = session_ledger.LEDGER
        session_ledger.LEDGER = session_ledger.LedgerStore()
        self.addCleanup(setattr, session_ledger, "LEDGER", self._orig_ledger)
        _ps._AUTO_RECALL_STATE.clear()

    def _run(self, messages, sid="s-sc", flags=None, seed_ledger=True):
        """报文 → 组装段。flags: (auto_recall, tombstone)。"""
        auto, tomb = flags if flags is not None else (False, True)
        if seed_ledger:
            session_ledger.LEDGER.record_request(sid, messages, 1)
        _plant_deposit(sid, TID)
        ctx = PipelineContext(body={"max_tokens": 100}, messages=messages,
                              session_id=sid, total_chars=1000)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=auto,
                          PROXY_TOMBSTONE_RECALL_ENABLED=tomb,
                          PROXY_CTX_LIMIT_ENABLED=False,
                          PROXY_OOM_SAFE_TOKENS=10 ** 9):
            self.STAGES.run(ctx)
        return ctx

    def _tombstones(self, ctx):
        """stage 20 出口中的墓碑 tool 消息列表。"""
        out = []
        for m in ctx.openai_messages or []:
            c = m.get("content")
            if m.get("role") == "tool" and isinstance(c, str) \
                    and _TOMBSTONE_MARK in c:
                out.append(json.loads(c))
        return out


class TestScenarios(TombstoneScenarioBase):
    def _dangling_messages(self):
        return [
            {"role": "user", "content": [{"type": "text", "text": "start"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": TID, "name": "Read",
                 "input": {"file_path": TARGET}}]},
            {"role": "user", "content": [{"type": "text", "text": "next"}]},
            {"role": "assistant", "content": [{"type": "text",
                                               "text": "thinking"}]},
        ]

    def test_sc1_assembly_probe(self):
        """SC1 组装探针: 12.5 注入有效 + 组装事实固化。

        实测显影（单测测不出的环节互斥）: stage 19 会删除悬空 tool_use
        （防 400 的既有职责）→ stage 20 看不到墓碑注入条件 → 被动注记
        在主链路不可达；主动代答（12.5，先于 19 执行、只追加尾消息）
        不受影响——这正是墓碑召回以主动代答为主路径的原因。
        """
        msgs = self._dangling_messages()
        snapshot = json.dumps(msgs, sort_keys=True)
        ctx = self._run(msgs)
        # E1: 12.5 出口——寄存回填注入（先于 stage 19，不受其删减影响）
        self.assertEqual(ctx.auto_recall_info.get("trigger"), "tombstone")
        tail = ctx.messages[-1]["content"][0]["text"]
        self.assertIn("AUTO-RECALL", tail)
        self.assertIn("r:%s" % TID, tail)
        # E2: stage 19 组装事实——悬空 tool_use 被删（孤儿清理既有语义）
        use_ids = [b.get("id") for m in ctx.messages if m.get("role") == "assistant"
                   for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                   if isinstance(b, dict) and b.get("type") == "tool_use"]
        self.assertNotIn(TID, use_ids)
        # E3: stage 20 出口——无悬空调用则无墓碑（被动注记无可注记对象）
        self.assertEqual(self._tombstones(ctx), [])
        # 回填尾消息原样进入 openai 视图
        texts = [m.get("content") for m in ctx.openai_messages or []
                 if m.get("role") == "user"]
        self.assertTrue(any(isinstance(t, str) and "AUTO-RECALL" in t
                            for t in texts))

    def test_sc1b_append_only_of_125(self):
        """SC1b: 12.5 自身的 append-only 纪律——注入前历史字节不变。

        隔离验证（不含 19）：只跑 12.5，注入之外的既有消息逐字节不变。
        """
        msgs = self._dangling_messages()
        snapshot = json.dumps(msgs, sort_keys=True)
        ctx = PipelineContext(body={"max_tokens": 100}, messages=msgs,
                              session_id="s-sc", total_chars=1000)
        _plant_deposit("s-sc", TID)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=False,
                          PROXY_TOMBSTONE_RECALL_ENABLED=True):
            AutoRecallStage().process(ctx)
        self.assertEqual(json.dumps(ctx.messages[:-1], sort_keys=True),
                         snapshot)

    def test_sc3_last_assistant_exempt(self):
        """SC3: 末条 assistant 的未决调用是在途，非丢失。"""
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "start"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": TID, "name": "Read",
                 "input": {"file_path": TARGET}}]},
        ]
        ctx = self._run(msgs)
        self.assertEqual(ctx.auto_recall_info, {"injected": 0})

    def test_sc4_no_deposit_bare_tombstone(self):
        """SC4: 无寄存 → 无注入；stage 19/20 形态按真实结果观测。"""
        msgs = self._dangling_messages()
        session_ledger.LEDGER.record_request("s-sc4", msgs, 1)
        ctx = PipelineContext(body={"max_tokens": 100}, messages=msgs,
                              session_id="s-sc4", total_chars=1000)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=False,
                          PROXY_TOMBSTONE_RECALL_ENABLED=True,
                          PROXY_CTX_LIMIT_ENABLED=False,
                          PROXY_OOM_SAFE_TOKENS=10 ** 9):
            self.STAGES.run(ctx)
        self.assertEqual(ctx.auto_recall_info, {"injected": 0})

    def test_sc5_dup_wins_when_both_armed(self):
        """SC5: dup 与墓碑同轮命中，dup 路径优先（本轮墓碑让位）。

        dup 注入要求按路径可召回（u: 行 handle 匹配）——植入含 handle 的
        u: 行 + 配对 r: 行 + archive。
        """
        sid = "s-sc5"
        # 历史: 3 次同路径读取（id 不同, ledger 按 (tool,target) 聚合 count=3）
        # + 1 个悬空调用（墓碑触发条件同时在位）
        msgs = [{"role": "user", "content": [{"type": "text", "text": "start"}]}]
        for i, cid in enumerate(("a1", "a2", "a3")):
            msgs += [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": cid, "name": "Read",
                     "input": {"file_path": TARGET}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": cid,
                     "content": [{"type": "text",
                                  "text": "content %d" % i}]}]},
            ]
        msgs += [
            {"role": "user", "content": [{"type": "text", "text": "next"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": TID, "name": "Read",
                 "input": {"file_path": TARGET}}]},
            {"role": "user", "content": [{"type": "text", "text": "next2"}]},
            {"role": "assistant", "content": [{"type": "text",
                                               "text": "thinking"}]},
        ]
        # 植入 a1 的可召回寄存: assistant(u: 行带 handle) + result(r: 行) + archive
        # (内容须过结构摘录零头门槛 ≥200 chars, 与生产行为一致)
        body = "content %d " % 0 + "z" * 300
        memory_stores.record_dropped_messages(sid, 1, "compressed", [msgs[1]])
        memory_stores.record_dropped_messages(sid, 1, "compressed", [msgs[2]])
        sf.plant_archive_tool_result(sid, 1, "a1", body,
                                     diag_dir=_ps._DIAG_DIR)
        session_ledger.LEDGER.record_request(sid, msgs, 1)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True,
                          PROXY_TOMBSTONE_RECALL_ENABLED=True,
                          PROXY_CTX_LIMIT_ENABLED=False,
                          PROXY_OOM_SAFE_TOKENS=10 ** 9):
            ctx = PipelineContext(body={"max_tokens": 100}, messages=msgs,
                                  session_id=sid, total_chars=1000)
            self.STAGES.run(ctx)
        self.assertEqual(ctx.auto_recall_info.get("trigger"), "dup")

    def test_sc7_aux_session_skipped(self):
        """SC7: aux 分域会话整段跳过（stage 不运行, info 不落）。"""
        msgs = self._dangling_messages()
        ctx = self._run(msgs, sid="s-scaux::aux-haiku")
        self.assertIn(ctx.auto_recall_info, (None, {"injected": 0}))


if __name__ == "__main__":
    unittest.main()
