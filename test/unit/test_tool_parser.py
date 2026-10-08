"""Unit tests for tool_parser module."""
import json
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import tool_parser as tp


class TestCoerceBooleans(unittest.TestCase):
    def test_string_true(self):
        self.assertTrue(tp._coerce_booleans("true"))
        self.assertTrue(tp._coerce_booleans("True"))

    def test_string_false(self):
        self.assertFalse(tp._coerce_booleans("false"))
        self.assertFalse(tp._coerce_booleans("False"))

    def test_nested(self):
        data = {"a": "true", "b": ["false", "True"], "c": 1}
        out = tp._coerce_booleans(data)
        self.assertTrue(out["a"])
        self.assertFalse(out["b"][0])
        self.assertTrue(out["b"][1])
        self.assertEqual(out["c"], 1)


class TestUnescapeDoubleEscapedJson(unittest.TestCase):
    def test_array_wrapped_string(self):
        # After outer json.loads the inner value is '[{"q": "hello"}]'.
        data = {"questions": '[{"q": "hello"}]'}
        out = tp._unescape_double_escaped_json(data)
        self.assertIsInstance(out["questions"], list)
        self.assertEqual(out["questions"][0]["q"], "hello")

    def test_object_wrapped_string(self):
        data = {"config": '{"x": 1}'}
        out = tp._unescape_double_escaped_json(data)
        self.assertEqual(out["config"], {"x": 1})

    def test_plain_scalar(self):
        self.assertEqual(tp._unescape_double_escaped_json("hello"), "hello")


class TestIsTruncatedJson(unittest.TestCase):
    def test_closing_bracket(self):
        self.assertTrue(tp._is_truncated_json("]"))

    def test_opening_bracket(self):
        self.assertTrue(tp._is_truncated_json("["))

    def test_opening_brace(self):
        self.assertTrue(tp._is_truncated_json("{"))

    def test_complete_json(self):
        self.assertFalse(tp._is_truncated_json('{"a":1}'))


class TestParseToolArguments(unittest.TestCase):
    def test_valid_json(self):
        self.assertEqual(tp.parse_tool_arguments('{"x": 1}'), {"x": 1})

    def test_coerces_booleans(self):
        self.assertEqual(tp.parse_tool_arguments('{"flag": "true"}'), {"flag": True})

    def test_repair_truncated(self):
        self.assertEqual(tp.parse_tool_arguments('{"x": 1'), {"x": 1})

    def test_unescape_double_escaped(self):
        # Inner value after outer json.loads is a JSON-array string.
        raw = '{"arr": "[{\\"k\\": \\"v\\"}]"}'
        result = tp.parse_tool_arguments(raw)
        self.assertIsInstance(result.get("arr"), list)

    def test_embedded_json(self):
        raw = 'Some text before {"x": 1} and after'
        self.assertEqual(tp.parse_tool_arguments(raw), {"x": 1})

    def test_xml_fallback(self):
        raw = "<parameter=path>/tmp/x</parameter>"
        self.assertEqual(tp.parse_tool_arguments(raw, tool_name_hint="read"), {"path": "/tmp/x"})

    def test_read_heuristic(self):
        self.assertEqual(tp.parse_tool_arguments("/tmp/file.txt", tool_name_hint="read"), {"file_path": "/tmp/file.txt"})

    def test_bash_heuristic(self):
        self.assertEqual(tp.parse_tool_arguments("ls -la", tool_name_hint="bash"), {"command": "ls -la"})

    def test_empty_input(self):
        self.assertEqual(tp.parse_tool_arguments(""), {})


class TestParseToolsBlockBody(unittest.TestCase):
    def test_valid(self):
        body = '{"name": "Read", "arguments": {"file_path": "/tmp/x"}}'
        out = tp._parse_tools_block_body(body)
        self.assertEqual(out["name"], "Read")
        self.assertEqual(out["arguments"], {"file_path": "/tmp/x"})

    def test_arguments_as_string(self):
        body = '{"name": "Read", "arguments": "{\\"file_path\\": \\"/tmp/x\\"}"}'
        out = tp._parse_tools_block_body(body)
        self.assertEqual(out["arguments"], {"file_path": "/tmp/x"})

    def test_invalid_json(self):
        self.assertIsNone(tp._parse_tools_block_body("not json"))


class TestExtractContentToolCalls(unittest.TestCase):
    def test_single_block(self):
        text = "Before\n<tools>{\"name\": \"Read\", \"arguments\": {\"file_path\": \"/tmp/x\"}}</tools>\nAfter"
        out = tp._extract_content_tool_calls(text)
        self.assertEqual(out["text"], "Before\n\nAfter")
        self.assertEqual(len(out["tools"]), 1)
        self.assertEqual(out["tools"][0]["name"], "Read")

    def test_disabled(self):
        import proxy_state
        original = proxy_state.CONTENT_TOOLS_FALLBACK_ENABLED
        try:
            proxy_state.CONTENT_TOOLS_FALLBACK_ENABLED = False
            text = "<tools>{\"name\": \"Read\"}</tools>"
            out = tp._extract_content_tool_calls(text)
            self.assertEqual(out["text"], text)
            self.assertEqual(out["tools"], [])
        finally:
            proxy_state.CONTENT_TOOLS_FALLBACK_ENABLED = original

    def test_no_closing_tag(self):
        text = "<tools>{\"name\": \"Read\"}"
        out = tp._extract_content_tool_calls(text)
        self.assertEqual(out["text"], text)
        self.assertEqual(out["tools"], [])


class TestToolArgsCutAtLimit(unittest.TestCase):
    """L-73(2026-10-07): 区分「输出触顶被截」与「轻微语法不合法」。

    事故样本: 饱和期 max_tokens=8192, Write 大文件的 content 在 23813 字符处
    被切断, 代理补齐 JSON 后客户端把残缺文件当成功写入落盘。
    """

    # 事故真实形态: content 字符串未闭合, 停在 '<div'
    CUT_RAW = ('{"file_path": "/tmp/实验4.html", "content": "<!DOCTYPE html>\\n'
               '<html lang=\\"zh-CN\\">\\n<head>\\n<div')

    def test_incident_payload_is_incomplete(self):
        self.assertTrue(tp._is_truncated_json(self.CUT_RAW))

    def test_finish_reason_length_blocks(self):
        self.assertTrue(tp._tool_args_cut_at_limit(self.CUT_RAW, finish_reason="length"))

    def test_force_stopped_blocks(self):
        self.assertTrue(tp._tool_args_cut_at_limit(self.CUT_RAW, force_stopped=True))

    def test_output_tokens_at_ceiling_blocks(self):
        self.assertTrue(tp._tool_args_cut_at_limit(
            self.CUT_RAW, effective_max_tokens=8192, output_tokens=8192))

    def test_estimated_tokens_at_ceiling_blocks(self):
        # 事故实测: 文本 1626 + 参数 25089 ≈ 26715 字符 → 估算 10686 tokens ≥ 8192
        self.assertTrue(tp._tool_args_cut_at_limit(
            self.CUT_RAW, effective_max_tokens=8192, output_chars=26715))

    def test_incomplete_without_cut_evidence_is_not_blocked(self):
        """载荷不完整但无触顶证据 → 维持旧的补齐行为(仅语法不合法)。"""
        self.assertFalse(tp._tool_args_cut_at_limit(
            self.CUT_RAW, effective_max_tokens=8192,
            output_chars=1000, output_tokens=100))

    def test_no_limit_info_is_not_blocked(self):
        self.assertFalse(tp._tool_args_cut_at_limit(self.CUT_RAW))

    def test_complete_args_never_blocked(self):
        full = '{"file_path": "x.html", "content": "<div>hi</div>"}'
        self.assertFalse(tp._tool_args_cut_at_limit(full, finish_reason="length"))

    def test_sloppy_but_complete_args_not_blocked(self):
        """多余逗号属语法问题、载荷完整, 不应被当截断拦下。"""
        sloppy = '{"file_path": "x.html", "content": "abc",}'
        self.assertFalse(tp._is_truncated_json(sloppy))
        self.assertFalse(tp._tool_args_cut_at_limit(sloppy, finish_reason="length"))


if __name__ == "__main__":
    unittest.main()
