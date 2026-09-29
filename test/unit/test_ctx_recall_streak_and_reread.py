#!/usr/bin/env python3
"""test_ctx_recall_streak_and_reread.py — P0(ii) 复发-交错计数 + 配套指标。

背景（L-61，2026-09-29 登记）：ctx_recall 的"检索空转"对既有 loop 判据
结构性不可见——精确重复计数（consecutive，key=tool:args）在模型"换关键词
重试"时每次 key 都不同；交错的其它工具调用又把早前失败挤出 15 条尾部窗。
081154 实测单会话 38 次调用零触发。P0(ii) 改为按**结局**数连败（交错的
其它工具不打断，命中即清零），并把两个"无效重复劳动"事实提为 run 级指标：
① ctx_recall 调用/结局分型；② 同文件重读无编辑（L-53 的 55 次盲重读）。

本测试钉死的契约：
  - 结局台账按 call_id 去重（客户端每轮重发全量历史 ⇒ 重改写不重复计数）
  - 连败跨交错保持，命中清零
  - 新鲜度闸：空转已停（距上次调用超过 stale_turns）不再注入
  - 同文件重读：分页参数参与（不同切片不算浪费）；写后重读不算浪费
  - 连败触发 Lev1 专属提示（不邀请换词重试），且精确重复路径行为不变
"""
import unittest
from unittest.mock import patch

import proxy_state as _ps

import ctx_recall as cr
import loop_detection as ld
from pipeline import PipelineContext, ToolLoopDetector, LoopIntervention

_ERR = "<tool_use_error>No such tool available: ctx_recall</tool_use_error>"


def _recall_round(tid, query, interleaved=None):
    """一轮：assistant 调 ctx_recall（可选并行其它工具，模拟交错），
    客户端回 error tool_result。"""
    blocks = [{"type": "tool_use", "id": tid, "name": "ctx_recall",
               "input": {"query": query}}]
    if interleaved:
        blocks.append({"type": "tool_use", "id": interleaved[0],
                       "name": interleaved[1], "input": interleaved[2]})
    return [
        {"role": "assistant", "content": blocks},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": [{"type": "text", "text": _ERR}]}]},
    ]


class TestRecallStatsLedger(unittest.TestCase):
    """CTX_RECALL_STATS 台账：去重 / 连败 / 新鲜度 / 有界。"""

    def setUp(self):
        cr.CTX_RECALL_STATS.reset()

    def tearDown(self):
        cr.CTX_RECALL_STATS.reset()

    def test_dedup_by_call_id(self):
        s = "s-ledger-dedup"
        self.assertTrue(cr.CTX_RECALL_STATS.record(s, "c1", "miss", turn=1))
        self.assertFalse(cr.CTX_RECALL_STATS.record(s, "c1", "miss", turn=1))
        self.assertFalse(cr.CTX_RECALL_STATS.record(s, "c1", "hit", turn=1))
        snap = cr.CTX_RECALL_STATS.snapshot(s)
        self.assertEqual(snap["calls"], 1)
        self.assertEqual(snap["misses"], 1)
        self.assertEqual(snap["hits"], 0)

    def test_streak_tolerates_interleave_and_resets_on_hit(self):
        s = "s-ledger-streak"
        for i, o in enumerate(("miss", "fallback", "miss")):
            cr.CTX_RECALL_STATS.record(s, "c%d" % i, o, turn=2)
        snap = cr.CTX_RECALL_STATS.snapshot(s)
        self.assertEqual(snap["fail_streak"], 3)
        self.assertEqual(snap["failures"], 3)
        self.assertEqual(snap["misses"], 2)
        self.assertEqual(snap["fallbacks"], 1)
        cr.CTX_RECALL_STATS.record(s, "c9", "hit", turn=3)
        self.assertEqual(cr.CTX_RECALL_STATS.snapshot(s)["fail_streak"], 0)

    def test_streak_meta_staleness_gate(self):
        s = "s-ledger-fresh"
        cr.CTX_RECALL_STATS.record(s, "c1", "miss", turn=5)
        fresh = cr.CTX_RECALL_STATS.streak_meta(s, current_turn=6, stale_turns=2)
        self.assertTrue(fresh["fresh"])
        self.assertEqual(fresh["fail_streak"], 1)
        stale = cr.CTX_RECALL_STATS.streak_meta(s, current_turn=9, stale_turns=2)
        self.assertFalse(stale["fresh"])
        # 空会话 → 同形零值（消费方不必分支）
        empty = cr.CTX_RECALL_STATS.streak_meta("s-none", 1, 2)
        self.assertEqual(empty["fail_streak"], 0)
        self.assertFalse(empty["fresh"])

    def test_entries_bounded_and_reset(self):
        s = "s-ledger-bound"
        cap = cr._RecallStats._MAX_ENTRIES
        for i in range(cap + 5):
            cr.CTX_RECALL_STATS.record(s, "c%d" % i, "miss", turn=1)
        snap = cr.CTX_RECALL_STATS.snapshot(s)
        self.assertEqual(snap["calls"], cap)
        cr.CTX_RECALL_STATS.reset()
        self.assertEqual(cr.CTX_RECALL_STATS.snapshot(s), {})

    def test_invalid_outcome_rejected(self):
        self.assertFalse(cr.CTX_RECALL_STATS.record("s", "c1", "bogus"))
        self.assertFalse(cr.CTX_RECALL_STATS.record("", "c1", "miss"))


class TestRewriteRecording(unittest.TestCase):
    """代答路径记账：命中/未命中/回退三型都进台账（按 call_id 去重）。"""

    def setUp(self):
        import tempfile
        import memory_stores as ms
        self._tmp = tempfile.mkdtemp(prefix="p0ii_recall_")
        self._orig_dir = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._tmp
        self._orig_pd = _ps.PROXY_PD_ENABLED
        _ps.PROXY_PD_ENABLED = True
        ms.MANIFEST.reset()
        cr.CTX_RECALL_STATS.reset()
        _ps._SESSION_REQUEST_COUNT["s-p0ii"] = 7

    def tearDown(self):
        import shutil
        import memory_stores as ms
        _ps._DIAG_DIR = self._orig_dir
        _ps.PROXY_PD_ENABLED = self._orig_pd
        ms.MANIFEST.reset()
        cr.CTX_RECALL_STATS.reset()
        _ps._SESSION_REQUEST_COUNT.pop("s-p0ii", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _msgs(self, tool_input, tid="rc1"):
        return [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": "ctx_recall",
                 "input": tool_input}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid,
                 "content": [{"type": "text", "text": _ERR}]}]},
        ]

    def test_miss_recorded(self):
        from content_compressor import rewrite_ctx_recall_results
        rewrite_ctx_recall_results(self._msgs({"query": "nothing-here"}), "s-p0ii")
        snap = cr.CTX_RECALL_STATS.snapshot("s-p0ii")
        self.assertEqual(snap["misses"], 1)
        self.assertEqual(snap["last_outcome"], "miss")
        self.assertEqual(snap["last_turn"], 7)

    def test_hit_recorded(self):
        from content_compressor import rewrite_ctx_recall_results
        with patch.object(cr, "lookup", return_value=[{"anchor": "u:t1"}]), \
             patch.object(cr, "format_recall_result",
                          return_value="ctx_recall: 命中 1 条已折叠上下文索引:"):
            rewrite_ctx_recall_results(self._msgs({"query": "needle"}), "s-p0ii")
        self.assertEqual(cr.CTX_RECALL_STATS.snapshot("s-p0ii")["hits"], 1)

    def test_fallback_recorded_on_missing_query(self):
        from content_compressor import rewrite_ctx_recall_results
        rewrite_ctx_recall_results(self._msgs({}), "s-p0ii")
        snap = cr.CTX_RECALL_STATS.snapshot("s-p0ii")
        self.assertEqual(snap["fallbacks"], 1)

    def test_rewrite_again_does_not_double_count(self):
        from content_compressor import rewrite_ctx_recall_results
        msgs = self._msgs({"query": "nothing-here"})
        rewrite_ctx_recall_results(msgs, "s-p0ii")
        # 客户端每轮重发全量历史 ⇒ 同一 result 会被再改写一次
        msgs2 = self._msgs({"query": "nothing-here"})
        rewrite_ctx_recall_results(msgs2, "s-p0ii")
        self.assertEqual(cr.CTX_RECALL_STATS.snapshot("s-p0ii")["calls"], 1)


class TestCounters(unittest.TestCase):
    """调用侧扫描（重启不失忆）+ 同文件重读无编辑结构判据。"""

    def test_ctx_recall_calls_dedup_and_queries(self):
        messages = []
        messages += _recall_round("c1", "alpha", interleaved=("g1", "Grep", {}))
        messages += [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "g2", "name": "Grep",
             "input": {"pattern": "x"}}]}]
        messages += _recall_round("c2", "beta")
        # 客户端重发全量历史（含旧轮）——去重后仍是 2 次调用
        messages += _recall_round("c1", "alpha")
        stats = ld._count_ctx_recall_calls(messages)
        self.assertEqual(stats["attempts"], 2)
        self.assertEqual(stats["distinct_queries"], 2)
        self.assertEqual(stats["last_query"], "beta")

    def test_ctx_recall_calls_ignores_other_tools(self):
        messages = [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "r1", "name": "Read",
             "input": {"file_path": "/f"}}]}]
        self.assertEqual(ld._count_ctx_recall_calls(messages)["attempts"], 0)

    def _read(self, tid, path, offset=None, limit=None):
        inp = {"file_path": path}
        if offset is not None:
            inp["offset"] = offset
        if limit is not None:
            inp["limit"] = limit
        return {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Read", "input": inp}]}

    def _write(self, tid, path):
        return {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Edit",
             "input": {"file_path": path, "old_string": "a",
                       "new_string": "b"}}]}

    def test_reread_without_edit_counted(self):
        messages = [self._read("r1", "/a.py"), self._read("r2", "/a.py")]
        stats = ld._count_rereads_without_edit(messages)
        self.assertEqual(stats["reads"], 2)
        self.assertEqual(stats["no_edit_rereads"], 1)
        self.assertEqual(stats["no_edit_targets"], 1)

    def test_reread_after_edit_not_counted(self):
        messages = [self._read("r1", "/a.py"), self._write("w1", "/a.py"),
                    self._read("r2", "/a.py")]
        stats = ld._count_rereads_without_edit(messages)
        self.assertEqual(stats["no_edit_rereads"], 0)
        self.assertEqual(stats["writes"], 1)

    def test_paged_reads_are_not_rereads(self):
        # L-15 口径：分页读不同切片是新内容，不算浪费；同窗重读才算
        messages = [self._read("r1", "/a.py", offset=0, limit=100),
                    self._read("r2", "/a.py", offset=100, limit=100),
                    self._read("r3", "/a.py", offset=100, limit=100)]
        stats = ld._count_rereads_without_edit(messages)
        self.assertEqual(stats["no_edit_rereads"], 1)

    def test_other_file_read_not_counted(self):
        messages = [self._read("r1", "/a.py"), self._read("r2", "/b.py")]
        self.assertEqual(ld._count_rereads_without_edit(messages)["no_edit_rereads"], 0)

    def test_wasted_markers_counted_both_forms(self):
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "r1",
                 "content": "Wasted call — file unchanged"}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "r2",
                 "content": "[System: 该文件自上次读取后未发生变化，不要再使用 Read 工具反复读取。]"}]},
        ]
        self.assertEqual(ld._count_rereads_without_edit(messages)["wasted_calls"], 2)


class TestStreakIntervention(unittest.TestCase):
    """_apply_loop_intervention 的 P0(ii) 分支：连败触发 / 闸门 / 兼容旧行为。"""

    def _msgs(self):
        return [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]

    def test_streak_over_threshold_injects_notice(self):
        msgs, tools, level, tool_name = ld._apply_loop_intervention(
            self._msgs(), [], 1, {},
            ctx_recall_meta={"fail_streak": 3, "misses": 3, "fallbacks": 0,
                             "last_query": "abc", "fresh": True},
        )
        self.assertEqual(level, 1)
        self.assertEqual(tool_name, "ctx_recall_streak")
        text = str(msgs[-1])
        self.assertIn("连续 3 次未取回内容", text)
        self.assertIn("abc", text)
        self.assertNotIn("重试一次", text)   # 不邀请换词重试（L-61 反面）

    def test_below_threshold_is_noop(self):
        msgs, _, level, tool_name = ld._apply_loop_intervention(
            self._msgs(), [], 1, {},
            ctx_recall_meta={"fail_streak": 2, "fresh": True},
        )
        self.assertEqual((level, tool_name), (0, ""))
        self.assertEqual(len(msgs), 1)

    def test_stale_streak_is_noop(self):
        _, _, level, _ = ld._apply_loop_intervention(
            self._msgs(), [], 1, {},
            ctx_recall_meta={"fail_streak": 9, "fresh": False},
        )
        self.assertEqual(level, 0)

    def test_disabled_flag_is_noop(self):
        with patch.object(_ps, "PROXY_CTX_RECALL_STREAK_ENABLED", False):
            _, _, level, _ = ld._apply_loop_intervention(
                self._msgs(), [], 1, {},
                ctx_recall_meta={"fail_streak": 9, "fresh": True},
            )
        self.assertEqual(level, 0)

    def test_exact_repeat_path_unchanged_without_meta(self):
        # 无台账（旧代理/未启用）⇒ 精确重复路径行为不变
        _, _, level, tool_name = ld._apply_loop_intervention(
            self._msgs(), [], 1, {},
        )
        self.assertEqual((level, tool_name), (0, ""))
        consecutive = {"Read:/f": 3}
        msgs, _, level, tool_name = ld._apply_loop_intervention(
            self._msgs(), [{"name": "Read"}], 3, consecutive)
        self.assertEqual(level, 1)
        self.assertEqual(tool_name, "Read")


class TestPipelineWiring(unittest.TestCase):
    """stage 8/11 接线：统计落到 ctx；连败在 max_run 之下也触发。"""

    def setUp(self):
        cr.CTX_RECALL_STATS.reset()
        _ps._LOOP_SESSION_STATE.clear()

    def tearDown(self):
        cr.CTX_RECALL_STATS.reset()
        _ps._LOOP_SESSION_STATE.clear()
        _ps._SESSION_REQUEST_COUNT.pop("s-p0ii-wire", None)

    def test_tool_loop_detector_fills_stats(self):
        messages = []
        messages += _recall_round("c1", "alpha")
        messages += [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "r1", "name": "Read",
             "input": {"file_path": "/a.py"}}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "r2", "name": "Read",
                 "input": {"file_path": "/a.py"}}]}]
        cr.CTX_RECALL_STATS.record("s-p0ii-wire", "c1", "miss", turn=3)
        ctx = PipelineContext(messages=messages, session_id="s-p0ii-wire")
        ctx = ToolLoopDetector().process(ctx)
        self.assertEqual(ctx.ctx_recall_stats["attempts"], 1)
        self.assertEqual(ctx.ctx_recall_stats["misses"], 1)
        self.assertEqual(ctx.reread_stats["no_edit_rereads"], 1)

    def test_intervention_triggers_on_interleaved_streak(self):
        s = "s-p0ii-wire"
        for i in range(3):
            cr.CTX_RECALL_STATS.record(s, "c%d" % i, "miss", turn=5)
        _ps._SESSION_REQUEST_COUNT[s] = 6
        ctx = PipelineContext(messages=self._msgs_with_interleave(), session_id=s,
                              max_run=1)
        ctx = LoopIntervention().process(ctx)
        self.assertEqual(ctx.loop_tool_name, "ctx_recall_streak")
        self.assertIn("ctx_recall", str(ctx.messages[-1]))

    def test_intervention_silent_when_streak_stale(self):
        s = "s-p0ii-wire"
        for i in range(3):
            cr.CTX_RECALL_STATS.record(s, "c%d" % i, "miss", turn=1)
        _ps._SESSION_REQUEST_COUNT[s] = 20   # 距 last_turn=1 已远超 stale 窗
        messages = self._msgs_with_interleave()
        ctx = PipelineContext(messages=messages, session_id=s, max_run=1)
        ctx = LoopIntervention().process(ctx)
        self.assertEqual(ctx.loop_level, 0)
        self.assertEqual(len(ctx.messages), len(messages))   # 未追加注入

    def _msgs_with_interleave(self):
        messages = []
        for i, q in enumerate(("alpha", "beta", "gamma")):
            messages += _recall_round("c%d" % i, q,
                                      interleaved=("g%d" % i, "Grep", {}))
        return messages


if __name__ == "__main__":
    unittest.main()
