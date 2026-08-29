#!/usr/bin/env python3
"""test_unit_model.py — 消息原子化统一词汇表测试(词汇表对齐 2026-08-29)。

覆盖: Handle 统一形态(extract_handle 键优先级/搜索兜底/command·pattern 覆盖)、
展示串与 join 键、msg_hash 规范口径、msg_text_hash 与旧 pipeline 内联实现的
逐字节回归对照、text_str list 防御、result_text 截断语义。
"""
import hashlib
import unittest

import unit_model as um


class TestExtractHandle(unittest.TestCase):
    def test_structured_form_is_canonical(self):
        # 规范形态 = ledger 版结构化 dict(agent_go R14 契约消费)
        h = um.extract_handle("Read", {"file_path": "/a/b.py"})
        self.assertEqual(h, {"type": "path", "value": "/a/b.py"})

    def test_key_priority_order(self):
        # HANDLE_KEYS 优先级: url > file_path > path > query > command > pattern
        h = um.extract_handle("X", {"query": "q", "file_path": "/f", "url": "https://u"})
        self.assertEqual(h["type"], "url")
        h2 = um.extract_handle("X", {"query": "q", "command": "ls"})
        self.assertEqual(h2["type"], "query")

    def test_command_and_pattern_covered(self):
        # ctx_engine 版键集(command/pattern)合并后仍可提取
        self.assertEqual(um.extract_handle("Bash", {"command": "ls -la"}),
                         {"type": "command", "value": "ls -la"})
        self.assertEqual(um.extract_handle("Grep", {"pattern": "def foo"}),
                         {"type": "pattern", "value": "def foo"})

    def test_search_tool_fallback(self):
        # 兜底返回原始工具名(与 ledger 原实现一致,未小写化)
        h = um.extract_handle("WebSearch", {})
        self.assertEqual(h, {"type": "tool", "value": "WebSearch"})
        self.assertIsNone(um.extract_handle("Read", {}))

    def test_value_truncated_and_stripped(self):
        h = um.extract_handle("Read", {"file_path": "  " + "x" * 400})
        self.assertEqual(len(h["value"]), 300)
        self.assertTrue(h["value"].startswith("x"))

    def test_non_dict_args_safe(self):
        self.assertIsNone(um.extract_handle("Read", "not-a-dict"))
        self.assertIsNone(um.extract_handle(None, None))


class TestHandleHelpers(unittest.TestCase):
    def test_handle_value_display(self):
        self.assertEqual(um.handle_value({"type": "path", "value": "/a"}), "/a")
        self.assertEqual(um.handle_value(None), "")
        self.assertEqual(um.handle_value({}), "")

    def test_handle_key_join_form(self):
        self.assertEqual(um.handle_key({"type": "url", "value": "https://x"}), "url:https://x")
        self.assertEqual(um.handle_key(None), "")


class TestMsgHash(unittest.TestCase):
    def test_sort_keys_stable(self):
        # key 顺序不稳定不误判(前缀 diff 语义)
        import json
        h1 = um.msg_hash(json.loads('{"b": 1, "a": 2}'))
        h2 = um.msg_hash(json.loads('{"a": 2, "b": 1}'))
        self.assertEqual(h1, h2)

    def test_unserializable_fallback(self):
        class Weird:
            pass
        h = um.msg_hash({"x": Weird()})
        self.assertEqual(len(h), 32)  # md5 hexdigest

    def test_parity_with_session_ledger_wrapper(self):
        import session_ledger as sl
        msg = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        self.assertEqual(um.msg_hash(msg), sl._msg_hash(msg))


class TestMsgTextHashRegression(unittest.TestCase):
    """逐字节回归: 与原 pipeline MessageHashDebug 内联实现输出一致。"""

    def _old_inline(self, m, content_compressor):
        c = m.get("content", "")
        if isinstance(c, list):
            c = "".join(content_compressor._text_str(b.get("text", ""))
                        for b in c if b.get("type") == "text")
        elif not isinstance(c, str):
            c = str(c)
        return hashlib.md5((m.get("role", "") + ":" + c).encode()).hexdigest()[:8]

    def test_byte_parity_with_old_inline(self):
        import content_compressor
        cases = [
            {"role": "user", "content": "plain"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "a"}, {"type": "tool_use", "text": "ignored"}]},
            {"role": "user", "content": [{"type": "text", "text": ["list", "form"]}]},
            {"role": "user", "content": 12345},
            {"role": "user", "content": [{"type": "text", "text": None}]},
        ]
        for m in cases:
            self.assertEqual(um.msg_text_hash(m), self._old_inline(m, content_compressor),
                             msg="parity broken for %r" % (m,))

    def test_length_param(self):
        h = um.msg_text_hash({"role": "u", "content": "x"}, length=12)
        self.assertEqual(len(h), 12)


class TestTextStr(unittest.TestCase):
    def test_defensive_forms(self):
        self.assertEqual(um.text_str("s"), "s")
        self.assertEqual(um.text_str(["a", 1]), "a1")
        self.assertEqual(um.text_str(None), "None")
        self.assertEqual(um.text_str(5), "5")


class TestBlockAtomization(unittest.TestCase):
    def test_iter_blocks(self):
        msg = {"content": ["str-not-block", {"type": "text", "text": "t"},
                           {"type": "tool_result", "content": "r"}]}
        blocks = list(um.iter_blocks(msg))
        self.assertEqual(len(blocks), 2)
        self.assertEqual(list(um.iter_blocks({"content": "plain"})), [])

    def test_result_text_variants(self):
        self.assertEqual(um.result_text({"content": "abc"}), "abc")
        self.assertEqual(um.result_text({"content": [
            {"type": "text", "text": "a"}, "raw", {"type": "other"}]}), "a\nraw")
        self.assertEqual(um.result_text({"content": "abcdef"}, limit=3), "abc")
        self.assertEqual(um.result_text({"content": "abc"}, limit=None), "abc")
        self.assertEqual(um.result_text({}), "")

    def test_result_chars(self):
        self.assertEqual(um.result_chars({"content": "abcd"}), 4)
        self.assertEqual(um.result_chars({"content": [{"type": "text", "text": "ab"}]}) > 0, True)
        self.assertEqual(um.result_chars({}), 0)


if __name__ == "__main__":
    unittest.main()
