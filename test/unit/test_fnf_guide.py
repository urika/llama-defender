"""file_not_found 循环缓解一期（F1/F2/F3）离线夹具测试。

方案: swe-eval/docs/fnf-loop-mitigation-solution-20261025.md（§3.1 四断言）
旗标缺省关 = 零行为变化；本文件逐旗标打开验证行为面。
"""
import os
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxy_state
import tool_filter as tf

ROOT = "/tmp/fnf-ut/src"
ERR = f"File does not exist: {ROOT}/app.py"


def tu(i, name, inp):
    return {"role": "assistant",
            "content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}


def tr(i, content):
    return {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": i, "content": content}]}


class FnfGuideTest(unittest.TestCase):
    def setUp(self):
        tf._FNF_SEEN.clear()
        tf._FNF_DIR_CACHE.clear()
        tf._FNF_SUPPRESS.clear()
        tf._FNF_TUID_PATH.clear()
        self.flags = patch.object(proxy_state, "PROXY_FNF_GUIDE_ENABLED", True)

    def test_escalation_at_count_ge3(self):
        """断言①: count≥3 出现升级提示（第 N 次＋禁止），文本确定。"""
        with self.flags:
            msgs = []
            for i in ("t1", "t2", "t3"):
                msgs += [tu(i, "Read", {"file_path": f"{ROOT}/app.py"}), tr(i, ERR)]
            out, cnt = tf._translate_tool_result_errors_ex(
                msgs, session_id="sA", profile="t")
        self.assertEqual(cnt["file_not_found"], 3)
        self.assertEqual(cnt["fnf_guided"], 1)
        h = out[-1]["content"][0]["content"]
        self.assertIn("第 3 次", h)
        self.assertIn("禁止再次读取", h)
        # 前两次为基础文案（无计数无禁令）
        for m in (out[1], out[3]):
            self.assertNotIn("禁止再次读取", m["content"][0]["content"])

    def test_candidates_only_within_proven_root(self):
        """断言②: 候选只在工作区根内取（根外路径不注入父目录实况）。"""
        os.makedirs(ROOT, exist_ok=True)
        with open(f"{ROOT}/ok.py", "w") as f:
            f.write("x")
        self.addCleanup(lambda: os.remove(f"{ROOT}/ok.py"))
        with self.flags, patch.object(proxy_state, "PROXY_FNF_CANDIDATES_ENABLED", True):
            msgs = [tu("u1", "Read", {"file_path": f"{ROOT}/ok.py"}), tr("u1", "x"),
                    tu("u2", "Read", {"file_path": f"{ROOT}/app.py"}), tr("u2", ERR),
                    tu("u3", "Read", {"file_path": "/opt/etc/outside.py"}),
                    tr("u3", "File does not exist: /opt/etc/outside.py")]
            out, _ = tf._translate_tool_result_errors_ex(
                msgs, session_id="sB", profile="t")
        h_in = out[3]["content"][0]["content"]
        h_out = out[5]["content"][0]["content"]
        self.assertIn("父目录实况", h_in)
        self.assertIn("ok.py", h_in)
        self.assertNotIn("父目录实况", h_out)

    def test_success_read_clears_count(self):
        """断言④: 成功读取清零口——文件后来被创建时计数归零（逃逸口）。"""
        os.makedirs(ROOT, exist_ok=True)
        with self.flags:
            msgs = [tu("v1", "Read", {"file_path": f"{ROOT}/app.py"}), tr("v1", ERR),
                    tu("v2", "Read", {"file_path": f"{ROOT}/app.py"}), tr("v2", "recovered")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sC", profile="t")
            self.assertEqual(
                tf._FNF_SEEN.get(("sC", ""), {}).get(f"{ROOT}/app.py"), None)
            msgs2 = [tu("w1", "Read", {"file_path": f"{ROOT}/app.py"}), tr("w1", ERR),
                     tu("w2", "Read", {"file_path": f"{ROOT}/app.py"}), tr("w2", ERR)]
            out, _ = tf._translate_tool_result_errors_ex(
                msgs2, session_id="sC", profile="t")
        self.assertNotIn("禁止再次读取", out[-1]["content"][0]["content"])

    def test_flags_off_legacy_behavior(self):
        """旗标缺省关 = 零行为变化（旧三键提示，无 fnf 键副作用）。"""
        with patch.object(proxy_state, "PROXY_FNF_GUIDE_ENABLED", False):
            msgs = [tu("z1", "Read", {"file_path": f"{ROOT}/app.py"}), tr("z1", ERR)]
            out, cnt = tf._translate_tool_result_errors_ex(
                msgs, session_id="sD", profile="t")
        self.assertEqual(cnt["fnf_guided"], 0)
        self.assertEqual(cnt["fnf_cand_hits"], 0)
        hint = out[1]["content"][0]["content"]
        self.assertTrue(hint.startswith("[System: 文件不存在。请先用"))


class F3BashPathKeysTest(unittest.TestCase):
    """断言③: F3 后确定性阶梯对 Bash 变体逃逸真正可达。"""

    def _run_detector(self, n_pairs, flag):
        import pipeline
        with patch.object(proxy_state, "PROXY_LOOP_BASH_PATH_KEYS_ENABLED", flag), \
             patch.object(proxy_state, "PROXY_FNF_GUIDE_ENABLED", True):
            msgs = []
            for i in range(1, n_pairs + 1):
                msgs += [tu(f"b{i}", "Bash",
                            {"command": f"cat {ROOT}/app{i}.py"}),
                         tr(f"b{i}", f"cat: {ROOT}/app.py: No such file or directory")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sE", profile="t")
            ctx = SimpleNamespace(messages=msgs)
            pipeline.ToolLoopDetector().process(ctx)
        return ctx

    def test_bash_variants_aggregate_to_ladder(self):
        ctx = self._run_detector(22, True)
        n = ctx.consecutive.get(f"Bash:fnf:{ROOT}/app.py")
        self.assertIsNotNone(n)
        self.assertGreaterEqual(ctx.max_run, 6)  # L1 阈值（tier 动态下限 6）

    def test_flag_off_no_derived_keys(self):
        ctx = self._run_detector(22, False)
        self.assertFalse(any("fnf:" in k for k in ctx.consecutive))


class F3EscalationMemoryTest(unittest.TestCase):
    """F3'（方案 v0.1 §2）：会话级升级记忆 + L2 sticky。"""

    def setUp(self):
        import loop_detection
        loop_detection._LOOP_ESCALATED.clear()
        loop_detection._LOOP_STICKY.clear()
        proxy_state._SESSION_REQUEST_COUNT.clear()

    def tearDown(self):
        proxy_state._SESSION_REQUEST_COUNT.clear()

    def _apply(self, flag, count, session_id="sF", turn=0, tools=None):
        import loop_detection
        consecutive = {f"Read:{{\"file_path\": \"a.py\"}}": count}
        tools = tools if tools is not None else [{"name": "Read"}, {"name": "Bash"}]
        with patch.object(proxy_state, "PROXY_LOOP_ESCALATION_MEMORY_ENABLED", flag):
            proxy_state._SESSION_REQUEST_COUNT[session_id] = turn
            return loop_detection._apply_loop_intervention(
                [{"role": "user", "content": "x"}], tools,
                count, consecutive,
                threshold=3, level2_threshold=6, level3_threshold=9,
                session_id=session_id, total_chars=1000)

    def _text(self, messages):
        return messages[-1]["content"][0]["text"]

    def test_flag_off_zero_behavior_change(self):
        """旗标缺省关 = 零行为变化（注入文本不含会话级升级语言）。"""
        msgs, tools, level, tool = self._apply(False, 4)
        self.assertEqual(level, 1)
        self.assertNotIn("本会话第", self._text(msgs))
        self.assertEqual(len(tools), 2)

    def test_escalation_counter_accumulates(self):
        """F3'：L1 注入带会话级累积计数（第 N 次）。"""
        msgs1, _, lvl1, _ = self._apply(True, 4)
        self.assertEqual(lvl1, 1)
        self.assertIn("本会话第 1 次", self._text(msgs1))
        msgs2, _, lvl2, _ = self._apply(True, 4)
        self.assertEqual(lvl2, 1)
        self.assertIn("本会话第 2 次", self._text(msgs2))

    def test_l2_records_sticky_and_keeps_removal(self):
        """F3'：L2 记 sticky；同形态重现（窗内）→ 维持摘除 + 升级说明。"""
        msgs_l2, tools_l2, lvl_l2, _ = self._apply(True, 7)
        self.assertEqual(lvl_l2, 2)
        self.assertEqual([t["name"] for t in tools_l2], ["Bash"])
        self.assertIn("本会话第 1 次", self._text(msgs_l2))
        # 同错误形态重现（窗内，turn 未越过）→ 维持摘除（level 2）
        msgs_l1, tools_l1, lvl_sticky, _ = self._apply(True, 4, turn=1)
        self.assertEqual(lvl_sticky, 2)
        self.assertEqual([t["name"] for t in tools_l1], ["Bash"])
        self.assertIn("REMAINS DISABLED", self._text(msgs_l1))
        self.assertIn("连续限制", self._text(msgs_l1))

    def test_sticky_expires_after_window(self):
        """F3'：sticky 窗（默认 3 轮）过后不再维持摘除。"""
        self._apply(True, 7, turn=0)
        msgs, tools, level, _ = self._apply(True, 4, turn=9)
        self.assertEqual(level, 1)
        self.assertEqual(len(tools), 2)
        self.assertNotIn("REMAINS DISABLED", self._text(msgs))


if __name__ == "__main__":
    unittest.main()
