#!/usr/bin/env python3
"""ctx_recall 自闭环 auto-recall 单测（2026-09-05 设计 §4/§5）。

断言：auto_recall_for_target 的 u:→r: 两段式匹配与 fail-open；
AutoRecallStage 五环（台账 dup 检测 → manifest 折叠确认 → 召回 →
尾部注入 → 记账）及全部护栏（阈值/限次/同目标去重/aux 跳过/开关关）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import proxy_state as _ps  # noqa: E402
import ctx_recall  # noqa: E402
import memory_stores  # noqa: E402
import session_ledger  # noqa: E402
from pipeline import AutoRecallStage, PipelineContext  # noqa: E402
from test.lib.config_fixture import patch_config  # noqa: E402
from test.lib import state_fixture as sf  # noqa: E402


TARGET = "/repo/src/loop_file.py"
CONTENT = "def loop():\n    return 42\n" * 20


def _pair(tid, tool="Read", args=None, text=CONTENT):
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": tool,
             "input": args if args is not None else {"file_path": TARGET}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": [{"type": "text", "text": text}]}]},
    ]


def _client_msgs(n_reads=4):
    """n 次同目标 Read 的客户端历史（跨两轮，触发累计 dup=4）。"""
    msgs = [{"role": "user", "content": "start"}]
    for i in range(n_reads):
        msgs.extend(_pair("t%d" % i))
    msgs.append({"role": "user", "content": "next"})
    return msgs


def _plant_folded(sid, diag_dir, tid="t0", turn=1):
    """把一对 (tool_use, tool_result) 按 epoch 折叠路径写入 manifest+archive。"""
    memory_stores.record_dropped_messages(
        sid, turn, "epoch_collapse", _pair(tid))
    sf.plant_archive_tool_result(sid, turn, tid, CONTENT, diag_dir=diag_dir)


class _IsolatedCase(unittest.TestCase):
    """diag 目录隔离 + 会话级单例替换的公共底座。"""

    def setUp(self):
        self._diag = sf.isolated_diag()
        self._diag.__enter__()
        self.addCleanup(self._diag.__exit__, None, None, None)
        memory_stores.MANIFEST.reset()
        # stage 消费全局 LEDGER 单例——替换为干净实例防跨测试串扰
        self._orig_ledger = session_ledger.LEDGER
        session_ledger.LEDGER = session_ledger.LedgerStore()
        self.addCleanup(setattr, session_ledger, "LEDGER", self._orig_ledger)
        _ps._AUTO_RECALL_STATE.clear()

    def _seed_session(self, sid, n_reads=4):
        """台账记录 n 次同目标 Read（默认 dup=4 ≥ 阈值 3）。"""
        msgs = _client_msgs(n_reads)
        session_ledger.LEDGER.record_request(sid, msgs[:1 + 2 * (n_reads // 2)], 1)
        session_ledger.LEDGER.record_request(sid, msgs, 2)
        return sid


class TestAutoRecallForTarget(_IsolatedCase):
    def test_disabled_returns_none(self):
        _plant_folded("s-dis", _ps._DIAG_DIR)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=False):
            self.assertIsNone(
                ctx_recall.auto_recall_for_target("s-dis", TARGET))

    def test_u_to_r_pairing_recovers_content(self):
        _plant_folded("s-hit", _ps._DIAG_DIR)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            rec = ctx_recall.auto_recall_for_target("s-hit", TARGET)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["anchor"], "r:t0")
        self.assertEqual(rec["turn"], 1)
        self.assertEqual(rec["content"], CONTENT)
        self.assertEqual(rec["chars"], len(CONTENT))

    def test_max_chars_respected(self):
        _plant_folded("s-cap", _ps._DIAG_DIR)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            rec = ctx_recall.auto_recall_for_target(
                "s-cap", TARGET, max_chars=300)
        self.assertEqual(len(rec["content"]), 300)

    def test_unknown_target_returns_none(self):
        _plant_folded("s-miss", _ps._DIAG_DIR)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            self.assertIsNone(
                ctx_recall.auto_recall_for_target("s-miss", "/other/x.py"))

    def test_empty_inputs_return_none(self):
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            self.assertIsNone(ctx_recall.auto_recall_for_target("", TARGET))
            self.assertIsNone(ctx_recall.auto_recall_for_target("s", "  "))


class TestAutoRecallStage(_IsolatedCase):
    def _ctx(self, sid):
        return PipelineContext(body={"messages": []}, session_id=sid)

    def _run(self, sid, enabled=True, n_reads=4, **cfg):
        self._seed_session(sid, n_reads=n_reads)
        _plant_folded(sid, _ps._DIAG_DIR)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=enabled, **cfg):
            return AutoRecallStage().process(self._ctx(sid))

    def test_should_run_gates(self):
        stage = AutoRecallStage()
        with patch_config(PROXY_AUTO_RECALL_ENABLED=False):
            self.assertFalse(stage.should_run(self._ctx("s1")))
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            self.assertFalse(stage.should_run(self._ctx("")))
            self.assertFalse(stage.should_run(self._ctx("sess::aux-haiku")))
            self.assertTrue(stage.should_run(self._ctx("s1")))

    def test_happy_path_injects_tail_message(self):
        ctx = self._run("s-ok")
        self.assertEqual(len(ctx.messages), 1)
        self.assertEqual(ctx.messages[0]["role"], "user")
        text = ctx.messages[0]["content"][0]["text"]
        self.assertIn("AUTO-RECALL", text)
        self.assertIn(TARGET, text)
        self.assertIn(CONTENT.strip().splitlines()[0], text)
        self.assertIn("r:t0", text)
        info = ctx.auto_recall_info
        self.assertEqual(info["injected"], 1)
        self.assertEqual(info["target"], TARGET)
        self.assertEqual(_ps._AUTO_RECALL_STATE["s-ok"]["count"], 1)

    def test_disabled_no_injection(self):
        ctx = self._run("s-off", enabled=False)
        self.assertEqual(ctx.messages, [])
        self.assertEqual(ctx.auto_recall_info, {"injected": 0})

    def test_below_threshold_no_injection(self):
        # dup=2 < 阈值 3
        ctx = self._run("s-low", n_reads=2)
        self.assertEqual(ctx.messages, [])

    def test_unfolded_target_no_injection(self):
        # 台账 dup 达标，但内容从未被折叠（无 manifest 行）→ ②决策环拦下
        sid = "s-live"
        self._seed_session(sid)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            ctx = AutoRecallStage().process(self._ctx(sid))
        self.assertEqual(ctx.messages, [])

    def test_per_session_cap(self):
        sid = "s-cap"
        self._run(sid)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True,
                          PROXY_AUTO_RECALL_PER_SESSION=1):
            ctx = AutoRecallStage().process(self._ctx(sid))
        self.assertEqual(ctx.messages, [])  # 已注入 1 次，达上限

    def test_same_target_injected_once(self):
        sid = "s-dup"
        self._run(sid)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            ctx = AutoRecallStage().process(self._ctx(sid))
        self.assertEqual(ctx.messages, [])  # 同目标去重，不重复注入
        self.assertEqual(_ps._AUTO_RECALL_STATE[sid]["count"], 1)

    def test_unknown_session_fail_open(self):
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            ctx = AutoRecallStage().process(self._ctx("s-none"))
        self.assertEqual(ctx.messages, [])
        self.assertEqual(ctx.auto_recall_info, {"injected": 0})

    def test_metrics_reported(self):
        stage = AutoRecallStage()
        ctx = self._run("s-met")
        self.assertEqual(stage.output_metrics(ctx)["injected"], 1)


if __name__ == "__main__":
    unittest.main()
