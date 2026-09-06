#!/usr/bin/env python3
"""structure_aware_excerpt 单测（2026-09-06 auto-recall 结构感知摘录）。

断言：ast 摘录（骨架含签名+行号区间+字符偏移、预算约束、块完整性与
按序填充）、损坏 Python 回退行边界、非代码内容行边界、极小预算退化、
auto_recall_for_target 首次注入接入摘录、anchor@offset 续读协议不受影响。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import proxy_state as _ps  # noqa: E402
import ctx_recall  # noqa: E402
import memory_stores  # noqa: E402
from test.lib.config_fixture import patch_config  # noqa: E402
from test.lib import state_fixture as sf  # noqa: E402


TARGET = "/repo/src/sample.py"
PY_CONTENT = (
    "import os\n"
    "import sys\n"
    "\n"
    "CONSTANT = 42\n"
    "\n"
    "def alpha(x):\n"
    "    return x + 1\n"
    "\n"
    "class Beta:\n"
    "    def method(self):\n"
    "        return CONSTANT\n"
    "\n"
    "def gamma():\n"
    "    return alpha(CONSTANT)\n"
)
BROKEN_PY = "def broken(:\n    pass\n" * 30
TXT_CONTENT = "log line %04d\n" * 500  # 非代码长文本


def _big_py(n_fns=10, body_pad=570):
    """骨架装得下、全体块装不下的合法 Python（~6K chars）。

    填充用 return 长字符串而非注释——ast 的 end_lineno 不含注释行。
    """
    parts = []
    for i in range(n_fns):
        parts.append("def fn_%02d():\n    return \"%s\"\n"
                     % (i, "x" * body_pad))
    return "".join(parts)


def _pair(tid, text, target=TARGET):
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Read",
             "input": {"file_path": target}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": [{"type": "text", "text": text}]}]},
    ]


class TestStructureAwareExcerpt(unittest.TestCase):
    """纯函数面：摘录策略与预算语义。"""

    def test_ast_skeleton_has_signatures_lines_and_offsets(self):
        big = _big_py()
        res = ctx_recall.structure_aware_excerpt(big, TARGET, 2500)
        self.assertEqual(res["strategy"], "ast")
        self.assertTrue(res["truncated"])  # 预算装不下全部块
        self.assertLessEqual(len(res["text"]), 2500)
        # 骨架列出全部顶层块（含因预算未填充的）：签名 + 行号区间 + 字符偏移
        self.assertIn("[skeleton: 10 top-level blocks", res["text"])
        self.assertIn("def fn_05():", res["text"])
        off = big.index("def fn_05():")
        self.assertIn("@%d]" % off, res["text"])  # 偏移与原文一致

    def test_ast_filled_blocks_verbatim_not_cut_midway(self):
        big = _big_py()
        res = ctx_recall.structure_aware_excerpt(big, TARGET, 2500)
        # 填充的块完整原样；装不下的块整体略去（不截半）
        pad = '    return "' + "x" * 570 + '"'
        self.assertIn("def fn_00():\n" + pad, res["text"])
        self.assertNotIn("def fn_05():\n" + pad, res["text"])

    def test_ast_fill_order(self):
        big = _big_py()
        res = ctx_recall.structure_aware_excerpt(big, TARGET, 2500)
        body = res["text"]
        self.assertLess(body.index("def fn_00():\n    return"),
                        body.index("def fn_01():\n    return"))
        self.assertLess(body.index("def fn_01():\n    return"),
                        body.index("def fn_02():\n    return"))

    def test_broken_python_falls_back_to_line(self):
        # mid-edit 损坏代码 ast.parse 必须兜住
        res = ctx_recall.structure_aware_excerpt(BROKEN_PY, TARGET, 200)
        self.assertEqual(res["strategy"], "line")
        self.assertTrue(res["truncated"])
        self.assertLessEqual(len(res["text"]), 200)
        self.assertTrue(BROKEN_PY.startswith(res["text"]))
        self.assertTrue(res["text"].endswith("\n"))  # 对齐完整行尾

    def test_non_python_uses_line_boundary(self):
        res = ctx_recall.structure_aware_excerpt(
            TXT_CONTENT, "/repo/logs/run.log", 205)
        self.assertEqual(res["strategy"], "line")
        self.assertLessEqual(len(res["text"]), 205)
        self.assertTrue(res["text"].endswith("\n"))
        self.assertTrue(res["truncated"])

    def test_tiny_budget_degrades_safely(self):
        # 预算连骨架都装不下 → 退化为行截断；更小/非法预算不炸
        res = ctx_recall.structure_aware_excerpt(PY_CONTENT, TARGET, 50)
        self.assertEqual(res["strategy"], "line")
        self.assertLessEqual(len(res["text"]), 50)
        self.assertTrue(res["truncated"])
        res = ctx_recall.structure_aware_excerpt(PY_CONTENT, TARGET, 0)
        self.assertEqual(res["text"], "")
        self.assertTrue(res["truncated"])
        res = ctx_recall.structure_aware_excerpt(None, TARGET, 100)
        self.assertEqual(res, {"text": "", "strategy": "line",
                               "truncated": False})

    def test_content_within_budget_returned_verbatim(self):
        res = ctx_recall.structure_aware_excerpt(PY_CONTENT, TARGET, 4000)
        self.assertEqual(res["text"], PY_CONTENT)
        self.assertFalse(res["truncated"])


class TestAutoRecallExcerptIntegration(unittest.TestCase):
    """接入面：首次注入走摘录；续读协议（anchor@offset）原样。"""

    def setUp(self):
        self._diag = sf.isolated_diag()
        self._diag.__enter__()
        self.addCleanup(self._diag.__exit__, None, None, None)
        memory_stores.MANIFEST.reset()
        _ps._AUTO_RECALL_STATE.clear()

    def _plant(self, sid, text, target=TARGET, tid="t0", turn=1):
        memory_stores.record_dropped_messages(
            sid, turn, "epoch_collapse", _pair(tid, text, target))
        sf.plant_archive_tool_result(sid, turn, tid, text,
                                     diag_dir=_ps._DIAG_DIR)

    def test_first_injection_uses_ast_excerpt(self):
        big = _big_py()
        self._plant("s-ast", big)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            rec = ctx_recall.auto_recall_for_target("s-ast", TARGET)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["strategy"], "ast")
        self.assertEqual(rec["chars"], len(rec["content"]))
        self.assertLessEqual(rec["chars"], 4000)  # 默认预算
        self.assertIn("[skeleton:", rec["content"])
        self.assertIn("@0]", rec["content"])
        # 骨架偏移可直接喂给既有分页协议精确定位续读
        off = big.index("def fn_05():")
        self.assertIn("@%d]" % off, rec["content"])
        page = ctx_recall.recover_full_content("s-ast", "r:t0", 1,
                                               max_chars=20, offset=off)
        self.assertTrue(page.startswith("def fn_05():"))

    def test_non_python_target_gets_line_excerpt(self):
        self._plant("s-txt", TXT_CONTENT, target="/repo/logs/run.log")
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            rec = ctx_recall.auto_recall_for_target(
                "s-txt", "/repo/logs/run.log")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["strategy"], "line")
        self.assertLessEqual(len(rec["content"]), 4000)
        self.assertTrue(rec["content"].endswith("\n"))

    def test_continuation_protocol_unaffected(self):
        # 续读（offset>0）不经过摘录层：原文裸窗口原样返回
        big = _big_py()
        self._plant("s-cont", big)
        page = ctx_recall.recover_full_content("s-cont", "r:t0", 1,
                                               max_chars=10, offset=5)
        self.assertEqual(page, big[5:10 + 5])
        tail = ctx_recall.recover_full_content("s-cont", "r:t0", 1,
                                               max_chars=0, offset=len(big) - 8)
        self.assertEqual(tail, big[-8:])

    def test_stage_injection_notes_skeleton_offsets(self):
        # ast 摘录注入时, 锚点提示行补充骨架 offset 可直接用于分页
        import session_ledger
        from pipeline import AutoRecallStage, PipelineContext
        orig = session_ledger.LEDGER
        session_ledger.LEDGER = session_ledger.LedgerStore()
        self.addCleanup(setattr, session_ledger, "LEDGER", orig)
        big = _big_py()
        msgs = [{"role": "user", "content": "start"}]
        for i in range(4):
            msgs.extend(_pair("t%d" % i, big))
        msgs.append({"role": "user", "content": "next"})
        session_ledger.LEDGER.record_request("s-stg", msgs[:5], 1)
        session_ledger.LEDGER.record_request("s-stg", msgs, 2)
        self._plant("s-stg", big)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            ctx = AutoRecallStage().process(
                PipelineContext(body={"messages": []}, session_id="s-stg"))
        self.assertEqual(len(ctx.messages), 1)
        text = ctx.messages[0]["content"][0]["text"]
        self.assertIn("AUTO-RECALL", text)
        self.assertIn("[skeleton:", text)
        self.assertIn("skeleton @N markers are char offsets", text)


if __name__ == "__main__":
    unittest.main()
