#!/usr/bin/env python3
"""test_ifc_metrics.py — R9.1 Tier-0 结构指标测试(锚点差分/四指标/基线存储)。"""
import unittest

import ifc_metrics as im


def _anth_msg(role, blocks):
    return {"role": role, "content": blocks}


def _anth_tool_use(tid, name, args):
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


def _anth_tool_result(tid, text):
    return {"type": "tool_result", "tool_use_id": tid,
            "content": [{"type": "text", "text": text}]}


class TestUnitAnchors(unittest.TestCase):
    def test_anthropic_tool_use_and_result(self):
        m = _anth_msg("assistant", [
            {"type": "text", "text": "thinking"},
            _anth_tool_use("t1", "Read", {"file_path": "/a/b.py"})])
        units = im.unit_anchors(m)
        tools = [u for u in units if u["kind"] == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["anchor"], "u:t1")
        self.assertEqual(tools[0]["handle"], {"type": "path", "value": "/a/b.py"})
        r = im.unit_anchors(_anth_msg("user", [_anth_tool_result("t1", "x" * 10)]))
        self.assertEqual(r[0]["anchor"], "r:t1")
        self.assertEqual(r[0]["kind"], "tool_result")

    def test_openai_tool_calls_and_tool_role(self):
        m = {"role": "assistant", "content": "ok",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "Bash",
                                          "arguments": "{\"command\": \"ls\"}"}}]}
        units = im.unit_anchors(m)
        tools = [u for u in units if u["kind"] == "tool_use"]
        self.assertEqual(tools[0]["anchor"], "u:c1")
        self.assertEqual(tools[0]["handle"], {"type": "command", "value": "ls"})
        t = im.unit_anchors({"role": "tool", "tool_call_id": "c1", "content": "out"})
        self.assertEqual(t[0]["anchor"], "r:c1")
        self.assertEqual(t[0]["size_chars"], 3)

    def test_text_message_fallback(self):
        units = im.unit_anchors(_anth_msg("user", [{"type": "text", "text": "hello"}]))
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["kind"], "text")
        self.assertTrue(units[0]["anchor"].startswith("h:"))


class TestViewDiff(unittest.TestCase):
    def _prev_view(self):
        msgs = [
            _anth_msg("user", [{"type": "text", "text": "请重构登录模块" + "详" * 200}]),
            _anth_msg("assistant", [_anth_tool_use("t1", "Read", {"file_path": "/a.py"})]),
            _anth_msg("user", [_anth_tool_result("t1", "x" * 2000)]),
            _anth_msg("user", [{"type": "text", "text": "继续"}]),
        ]
        return im.view_summary(msgs)

    def test_drop_and_shrink_detected(self):
        prev = self._prev_view()
        cur_msgs = [
            # 首条 rationale 消息被 fifo 丢弃(锚消失)
            _anth_msg("assistant", [_anth_tool_use("t1", "Read", {"file_path": "/a.py"})]),
            # tool_result 被就地压缩: 同锚, 2000→300(超过 SHRINK_MIN_CHARS)
            _anth_msg("user", [_anth_tool_result("t1", "y" * 300)]),
            _anth_msg("user", [{"type": "text", "text": "继续"}]),
            # 新增单元
            _anth_msg("assistant", [_anth_tool_use("t2", "Edit", {"file_path": "/a.py"})]),
        ]
        cur = im.view_summary(cur_msgs)
        diff = im.diff_views(prev, cur)
        self.assertEqual(diff["dropped_units"], 1)          # 首 rationale 消息
        self.assertGreater(diff["dropped_chars"], 200)
        self.assertGreater(diff["dropped_rationale_chars"], 100)
        self.assertEqual(diff["shrunk_units"], 1)           # tool_result 压缩
        self.assertGreaterEqual(diff["shrunk_chars"], 128)
        self.assertEqual(diff["added_units"], 1)
        self.assertFalse(diff["view_reset"])                # 尾部存活=持续会话
        self.assertEqual(im.infer_ile_kinds(diff), ["unit_drop", "compress_drop"])
        # 原始事实层: 分类器版本 + 丢弃明细(可脱离 archive 重分类)
        sec = im.build_ifc_section(prev, cur, [])
        self.assertEqual(sec["cls_version"], im.IFC_CLS_VERSION)
        self.assertEqual(len(sec["dropped_detail"]), 1)
        self.assertEqual(sec["dropped_detail"][0]["kind"], "text")
        r = im.retention(diff)
        self.assertIsNotNone(r)
        self.assertLess(r, 1.0)
        rr = im.rationale_ratio(diff)
        self.assertLess(rr, 1.0)

    def test_identical_view_full_retention(self):
        prev = self._prev_view()
        cur = im.view_summary([
            _anth_msg("user", [{"type": "text", "text": "请重构登录模块" + "详" * 200}]),
            _anth_msg("assistant", [_anth_tool_use("t1", "Read", {"file_path": "/a.py"})]),
            _anth_msg("user", [_anth_tool_result("t1", "x" * 2000)]),
            _anth_msg("user", [{"type": "text", "text": "继续"}]),
        ])
        diff = im.diff_views(prev, cur)
        self.assertEqual(diff["dropped_units"], 0)
        self.assertEqual(diff["shrunk_chars"], 0)
        self.assertEqual(im.retention(diff), 1.0)
        self.assertEqual(im.rationale_ratio(diff), 1.0)
        self.assertEqual(im.infer_ile_kinds(diff), [])

    def test_shrink_below_threshold_ignored(self):
        prev = im.view_summary([_anth_msg("user", [_anth_tool_result("t1", "x" * 200)])])
        cur = im.view_summary([_anth_msg("user", [_anth_tool_result("t1", "x" * 150)])])
        diff = im.diff_views(prev, cur)
        self.assertEqual(diff["shrunk_units"], 0)  # 50 chars < SHRINK_MIN_CHARS

    def test_system_units_excluded_from_diff(self):
        # system 是每请求重注入的运行时上下文——内容变化不构成信息损失
        prev = im.view_summary([
            {"role": "system", "content": "sys v1 env=abc"},
            _anth_msg("user", [{"type": "text", "text": "任务A" + "x" * 200}])])
        cur = im.view_summary([
            {"role": "system", "content": "sys v2 env=xyz 日期 2026-08-30"},
            _anth_msg("user", [{"type": "text", "text": "任务A" + "x" * 200}])])
        diff = im.diff_views(prev, cur)
        self.assertEqual(diff["dropped_units"], 0)
        self.assertEqual(im.infer_ile_kinds(diff), [])

    def test_task_switch_classified_view_reset(self):
        # 同会话键下任务切换(swe-eval 命名请求→正式任务): 上一视图尾部
        # (工具对/末条消息)整体消失 → view_reset,不算 ILE
        prev = im.view_summary([
            _anth_msg("user", [{"type": "text", "text": "命名会话请求"}]),
            _anth_msg("assistant", [_anth_tool_use("t1", "Read", {"file_path": "/a"})]),
            _anth_msg("user", [_anth_tool_result("t1", "r" * 300)])])
        cur = im.view_summary([
            _anth_msg("user", [{"type": "text", "text": "完全不同的正式任务" + "y" * 300}])])
        diff = im.diff_views(prev, cur)
        self.assertTrue(diff["view_reset"])
        self.assertGreater(diff["dropped_units"], 0)   # 事实保留
        self.assertEqual(im.infer_ile_kinds(diff), [])  # 但不算信息损失
        sec = im.build_ifc_section(prev, cur, [])
        self.assertFalse(sec["ile"])
        self.assertTrue(sec["view_reset"])


class TestBehaviorMetrics(unittest.TestCase):
    def test_action_diversity_extremes(self):
        self.assertEqual(im.action_diversity(["Read"] * 20), 0.0)
        varied = ["Read", "Edit", "Bash", "Grep", "Read", "Write", "Bash", "Glob"] * 3
        self.assertGreater(im.action_diversity(varied), 0.5)
        self.assertIsNone(im.action_diversity(["Read", "Edit"]))

    def test_reread_pressure(self):
        actions = [{"target_hash": h} for h in ["a", "b", "a", "c", "a"]]
        self.assertEqual(im.reread_pressure(actions), 2)
        self.assertEqual(im.reread_pressure([{"target_hash": "x"}]), 0)
        self.assertEqual(im.reread_pressure([]), 0)


class TestReconcile(unittest.TestCase):
    """D_ledger 台账对账(精度轴)——双轴度量的另一半。"""

    LEDGER = [
        "/repo/txt-abc/src/app.py",
        "/repo/txt-abc/output/result.txt",
        "/repo/txt-abc/docs/guide.md",
    ]

    def test_full_recall_zero_deviation(self):
        ans = "已读取 src/app.py, 写入 output/result.txt 与 docs/guide.md"
        r = im.reconcile(ans, self.LEDGER)
        self.assertEqual(r["d_ledger"], 0.0)
        self.assertEqual(r["hit"], 3)

    def test_partial_recall(self):
        ans = "进度: src/app.py 已读, 其余不记得了"
        r = im.reconcile(ans, self.LEDGER)
        self.assertAlmostEqual(r["d_ledger"], 2 / 3, places=3)
        self.assertEqual(r["hit"], 1)

    def test_no_ground_truth_returns_none(self):
        self.assertIsNone(im.reconcile("什么都还没读", []))

    def test_extras_counted_not_judged(self):
        ans = "我计划写 output/plan.txt(还没动), src/app.py 已读"
        r = im.reconcile(ans, self.LEDGER)
        self.assertEqual(r["hit"], 1)
        self.assertGreaterEqual(r["extras"], 0)  # 计数口径,不下幻觉判定

    def test_bare_filename_suffix_match(self):
        r = im.reconcile("guide.md 已更新", self.LEDGER)
        self.assertEqual(r["hit"], 1)


class TestBuildSectionAndStore(unittest.TestCase):

    def test_first_turn_no_baseline(self):
        cur = im.view_summary([_anth_msg("user", [{"type": "text", "text": "hi"}])])
        sec = im.build_ifc_section(None, cur, [])
        self.assertIsNone(sec["retention"])
        self.assertFalse(sec["ile"])
        self.assertEqual(sec["n_msgs"], 1)

    def test_section_with_manifest_count(self):
        prev = cur = im.view_summary([_anth_msg("user", [{"type": "text", "text": "hi"}])])
        sec = im.build_ifc_section(prev, cur, [{"target_hash": "z"}] * 12, manifest_lines=7)
        self.assertEqual(sec["manifest_lines"], 7)
        self.assertEqual(sec["reread_pressure"], 9)  # 窗口取尾 10 个,同 hash → 9 重复
        self.assertEqual(sec["retention"], 1.0)

    def test_baseline_store_eviction(self):
        store = im.ViewBaselineStore(max_sessions=2)
        s = im.view_summary([_anth_msg("user", [{"type": "text", "text": "x"}])])
        store.update("s1", s)
        store.update("s2", s)
        store.update("s3", s)
        self.assertIsNone(store.get("s1"))
        self.assertIsNotNone(store.get("s3"))
        store.clear()
        self.assertIsNone(store.get("s3"))


if __name__ == "__main__":
    unittest.main()
