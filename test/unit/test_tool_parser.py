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


if __name__ == "__main__":
    unittest.main()
