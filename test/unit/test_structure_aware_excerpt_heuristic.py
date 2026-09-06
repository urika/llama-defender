#!/usr/bin/env python3
"""structure_aware_excerpt 多语言启发式单测（2026-09-06 第三级梯队）。

断言：js/go/java/rs/c/rb/sh 启发式摘录（骨架格式与 ast 路径完全一致：
签名行 + 行号区间 + 字符偏移 + 按序整块填充）、嵌套函数不单独成块、
整行 // 注释含花括号不误计、无结构退化 line、ast 失败的坏 Python 走
启发式、骨架超预算退 line、auto-recall 接入与注入文案对 heuristic 生效。
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


def _js_content(pad=400):
    return (
        "const ALPHA = () => {\n"
        "  return \"" + "a" * pad + "\";\n"
        "};\n"
        "\n"
        "function beta() {\n"
        "  return ALPHA() + \"" + "b" * pad + "\";\n"
        "}\n"
        "\n"
        "class Gamma {\n"
        "  run() {\n"
        "    return beta();\n"
        "  }\n"
        "}\n"
    )

GO_CONTENT = (
    "package main\n"
    "\n"
    "func alpha() string {\n"
    "\treturn \"" + "a" * 400 + "\"\n"
    "}\n"
    "\n"
    "func (s *Server) beta() string {\n"
    "\treturn alpha() + \"" + "b" * 400 + "\"\n"
    "}\n"
)

JAVA_CONTENT = (
    "public class Alpha {\n"
    "    public String run() {\n"
    "        return \"" + "a" * 500 + "\";\n"
    "    }\n"
    "}\n"
    "\n"
    "interface Beta {\n"
    "    String call();\n"
    "}\n"
)

RS_CONTENT = (
    "pub fn alpha(x: i32) -> i32 {\n"
    "    let s = \"" + "a" * 400 + "\";\n"
    "    x + s.len() as i32\n"
    "}\n"
    "\n"
    "fn beta() -> i32 {\n"
    "    alpha(1)\n"
    "}\n"
    "\n"
    "struct Gamma {\n"
    "    value: i32,\n"
    "}\n"
)

C_CONTENT = (
    "#include <stdio.h>\n"
    "\n"
    "int alpha(int x) {\n"
    "    char *s = \"" + "a" * 400 + "\";\n"
    "    return x + 1;\n"
    "}\n"
    "\n"
    "static int beta(int x)\n"
    "{\n"
    "    return alpha(x) * 2;\n"
    "}\n"
)

RB_CONTENT = (
    "class Alpha\n"
    "  def run\n"
    "    \"" + "a" * 400 + "\"\n"
    "  end\n"
    "end\n"
    "\n"
    "def beta\n"
    "  Alpha.new.run\n"
    "end\n"
)

SH_CONTENT = (
    "#!/bin/bash\n"
    "# 块外注释里的 { 与 } 不参与配平\n"
    "\n"
    "alpha() {\n"
    "  echo \"" + "a" * 400 + "\"\n"
    "}\n"
    "\n"
    "beta() {\n"
    "  alpha\n"
    "}\n"
)

NESTED_JS = (
    "function outer() {\n"
    "  function inner() {\n"
    "    return \"" + "x" * 600 + "\";\n"
    "  }\n"
    "  return inner();\n"
    "}\n"
    "\n"
    "function second() {\n"
    "  return outer() + \"" + "y" * 600 + "\";\n"
    "}\n"
)

COMMENT_BRACES_JS = (
    "function alpha() {\n"
    "  // } } } 整行注释里的花括号不应提前关闭块\n"
    "  return \"" + "x" * 500 + "\";\n"
    "}\n"
    "\n"
    "function beta() {\n"
    "  return alpha();\n"
    "}\n"
)

BROKEN_PY = (
    "def alpha():\n"
    "    return \"" + "a" * 400 + "\"\n"
    "\n"
    "def broken(:\n"
    "    pass\n"
    "\n"
    "def beta():\n"
    "    return alpha() + \"" + "b" * 400 + "\"\n"
)


class TestHeuristicExcerptPerLang(unittest.TestCase):
    """每语言正常用例：启发式摘录骨架格式与 ast 路径一致。"""

    def _assert_skeleton(self, res, content, n_blocks, sigs, budget):
        self.assertEqual(res["strategy"], "heuristic")
        self.assertLessEqual(len(res["text"]), budget)
        self.assertIn("[skeleton: %d top-level blocks" % n_blocks, res["text"])
        for sig in sigs:
            self.assertIn(sig, res["text"])
            off = content.index(sig)
            self.assertIn("@%d]" % off, res["text"])  # 偏移与原文一致

    def test_js(self):
        content = _js_content()
        res = ctx_recall.structure_aware_excerpt(
            content, "/repo/src/app.js", 700)
        self._assert_skeleton(res, content, 3,
                              ["const ALPHA = () => {", "function beta() {",
                               "class Gamma {"], 700)
        self.assertTrue(res["truncated"])  # 预算装不下全部块
        # 填充的块完整原样；装不下的块整体略去（不截半）
        pad = '  return "' + "a" * 400 + '";'
        self.assertIn("const ALPHA = () => {\n" + pad, res["text"])
        self.assertNotIn('ALPHA() + "' + "b" * 400, res["text"])

    def test_go(self):
        res = ctx_recall.structure_aware_excerpt(
            GO_CONTENT, "/repo/src/main.go", 700)
        self._assert_skeleton(res, GO_CONTENT, 2,
                              ["func alpha() string {",
                               "func (s *Server) beta() string {"], 700)

    def test_java(self):
        res = ctx_recall.structure_aware_excerpt(
            JAVA_CONTENT, "/repo/src/Alpha.java", 450)
        self._assert_skeleton(res, JAVA_CONTENT, 2,
                              ["public class Alpha {", "interface Beta {"],
                              450)

    def test_rust(self):
        res = ctx_recall.structure_aware_excerpt(
            RS_CONTENT, "/repo/src/lib.rs", 450)
        self._assert_skeleton(res, RS_CONTENT, 3,
                              ["pub fn alpha(x: i32) -> i32 {",
                               "fn beta() -> i32 {", "struct Gamma {"], 450)

    def test_c(self):
        res = ctx_recall.structure_aware_excerpt(
            C_CONTENT, "/repo/src/sample.c", 450)
        self._assert_skeleton(res, C_CONTENT, 2,
                              ["int alpha(int x) {", "static int beta(int x)"],
                              450)

    def test_ruby(self):
        res = ctx_recall.structure_aware_excerpt(
            RB_CONTENT, "/repo/src/alpha.rb", 450)
        self._assert_skeleton(res, RB_CONTENT, 2,
                              ["class Alpha", "def beta"], 450)
        # end 配对定块尾：class 块止于列 0 end（L1-L5），小块按序完整填充
        self.assertIn("[L1-L5 @0] class Alpha", res["text"])
        self.assertIn("def beta\n  Alpha.new.run\nend", res["text"])

    def test_shell(self):
        res = ctx_recall.structure_aware_excerpt(
            SH_CONTENT, "/repo/src/run.sh", 450)
        self._assert_skeleton(res, SH_CONTENT, 2,
                              ["alpha() {", "beta() {"], 450)


class TestHeuristicEdge(unittest.TestCase):
    """边界：嵌套、注释花括号、无结构、坏 Python、骨架超预算。"""

    def test_nested_functions_not_top_level_blocks(self):
        res = ctx_recall.structure_aware_excerpt(
            NESTED_JS, "/repo/src/nested.js", 900)
        self.assertEqual(res["strategy"], "heuristic")
        self.assertTrue(res["truncated"])
        head = res["text"].split("\n\n")[0]  # 仅骨架头
        self.assertIn("[skeleton: 2 top-level blocks", head)
        self.assertIn("function outer() {", head)
        self.assertIn("function second() {", head)
        self.assertNotIn("inner", head)  # 嵌套函数不单独成块

    def test_full_line_comment_braces_ignored(self):
        res = ctx_recall.structure_aware_excerpt(
            COMMENT_BRACES_JS, "/repo/src/c.js", 500)
        self.assertEqual(res["strategy"], "heuristic")
        # // 行内 3 个 } 被跳过, 块尾正确落在第 4 行的 }
        self.assertIn("[L1-L4 @0] function alpha() {", res["text"])
        self.assertIn("function beta() {", res["text"])

    def test_no_structure_falls_back_to_line(self):
        # 起始正则命中 <2 → 无结构 → 行边界
        flat = "const x = 1;\n" + "data row\n" * 300
        res = ctx_recall.structure_aware_excerpt(
            flat, "/repo/src/app.js", 200)
        self.assertEqual(res["strategy"], "line")
        self.assertLessEqual(len(res["text"]), 200)
        self.assertTrue(res["text"].endswith("\n"))
        # 0 命中同理
        plain = "package main\n\n" + "// just notes\n" * 200
        res = ctx_recall.structure_aware_excerpt(
            plain, "/repo/src/main.go", 200)
        self.assertEqual(res["strategy"], "line")

    def test_broken_python_uses_heuristic(self):
        # ast.parse 失败的 .py 先尝试启发式而非直接退行边界
        res = ctx_recall.structure_aware_excerpt(
            BROKEN_PY, "/repo/src/broken.py", 700)
        self.assertEqual(res["strategy"], "heuristic")
        self.assertIn("[skeleton: 3 top-level blocks", res["text"])
        self.assertIn("def broken(:", res["text"])  # 坏行也能进骨架签名
        pad = '    return "' + "a" * 400 + '"'
        self.assertIn("def alpha():\n" + pad, res["text"])  # 块完整原样
        self.assertTrue(res["truncated"])

    def test_good_python_still_uses_ast(self):
        good = ("def a():\n    return \"" + "x" * 600 + "\"\n\n"
                "def b():\n    return a()\n")
        res = ctx_recall.structure_aware_excerpt(
            good, "/repo/src/ok.py", 400)
        self.assertEqual(res["strategy"], "ast")

    def test_skeleton_over_budget_falls_back_to_line(self):
        big = "".join("function f%02d() {\n  return %d;\n}\n\n"
                      % (i, i) for i in range(20))
        res = ctx_recall.structure_aware_excerpt(
            big, "/repo/src/big.js", 120)
        self.assertEqual(res["strategy"], "line")  # 预算连骨架都装不下
        self.assertLessEqual(len(res["text"]), 120)
        self.assertTrue(res["truncated"])


def _pair(tid, text, target):
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Read",
             "input": {"file_path": target}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": [{"type": "text", "text": text}]}]},
    ]


class TestHeuristicIntegration(unittest.TestCase):
    """接入面：非 .py 目标首次注入走启发式摘录；注入文案带骨架 offset 提示。"""

    def setUp(self):
        self._diag = sf.isolated_diag()
        self._diag.__enter__()
        self.addCleanup(self._diag.__exit__, None, None, None)
        memory_stores.MANIFEST.reset()
        _ps._AUTO_RECALL_STATE.clear()

    def _plant(self, sid, text, target, tid="t0", turn=1):
        memory_stores.record_dropped_messages(
            sid, turn, "epoch_collapse", _pair(tid, text, target))
        sf.plant_archive_tool_result(sid, turn, tid, text,
                                     diag_dir=_ps._DIAG_DIR)

    def test_auto_recall_js_target_uses_heuristic(self):
        big = _js_content(pad=2200)  # 超默认预算 4000
        target = "/repo/src/app.js"
        self._plant("s-js", big, target)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            rec = ctx_recall.auto_recall_for_target("s-js", target)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["strategy"], "heuristic")
        self.assertLessEqual(rec["chars"], 4000)  # 默认预算
        self.assertIn("[skeleton: 3 top-level blocks", rec["content"])
        # 骨架偏移可直接喂给既有分页协议精确定位续读
        off = big.index("function beta() {")
        self.assertIn("@%d]" % off, rec["content"])
        page = ctx_recall.recover_full_content("s-js", "r:t0", 1,
                                               max_chars=20, offset=off)
        self.assertTrue(page.startswith("function beta() {"))

    def test_stage_injection_notes_skeleton_offsets_for_heuristic(self):
        # heuristic 摘录注入时, 锚点提示行同样补充骨架 offset 用法
        import session_ledger
        from pipeline import AutoRecallStage, PipelineContext
        orig = session_ledger.LEDGER
        session_ledger.LEDGER = session_ledger.LedgerStore()
        self.addCleanup(setattr, session_ledger, "LEDGER", orig)
        big = _js_content(pad=2200)
        target = "/repo/src/app.js"
        msgs = [{"role": "user", "content": "start"}]
        for i in range(4):
            msgs.extend(_pair("t%d" % i, big, target))
        msgs.append({"role": "user", "content": "next"})
        session_ledger.LEDGER.record_request("s-hstg", msgs[:5], 1)
        session_ledger.LEDGER.record_request("s-hstg", msgs, 2)
        self._plant("s-hstg", big, target)
        with patch_config(PROXY_AUTO_RECALL_ENABLED=True):
            ctx = AutoRecallStage().process(
                PipelineContext(body={"messages": []}, session_id="s-hstg"))
        self.assertEqual(len(ctx.messages), 1)
        text = ctx.messages[0]["content"][0]["text"]
        self.assertIn("AUTO-RECALL", text)
        self.assertIn("[skeleton:", text)
        self.assertIn("skeleton @N markers are char offsets", text)


if __name__ == "__main__":
    unittest.main()
