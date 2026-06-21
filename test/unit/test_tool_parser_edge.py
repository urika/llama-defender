"""Unit tests for tool parser edge cases in anthropic_proxy.

Covers _extract_xml_params, _extract_xml_tool_name, _is_truncated_json,
_parse_tools_block_body, _generate_tool_summary.
"""
import unittest

import anthropic_proxy as proxy


class TestExtractXmlParams(unittest.TestCase):
    """Tests for _extract_xml_params() — XML parameter extraction."""

    def test_pattern1_parameter_equals_key(self):
        raw = "<parameter=query>What is AI?</parameter>"
        result = proxy._extract_xml_params(raw)
        self.assertEqual(result, {"query": "What is AI?"})

    def test_pattern2_param_name_attr(self):
        raw = '<param name="city">Beijing</param>'
        result = proxy._extract_xml_params(raw)
        self.assertEqual(result, {"city": "Beijing"})

    def test_pattern3_generic_tags(self):
        raw = "<city>Shanghai</city><unit>celsius</unit>"
        result = proxy._extract_xml_params(raw)
        self.assertIn("city", result)
        self.assertIn("unit", result)

    def test_reserved_tags_skipped(self):
        raw = "<function>get_weather</function><name>test</name><arguments>{}</arguments>"
        result = proxy._extract_xml_params(raw)
        for reserved in ("function", "name", "arguments"):
            self.assertNotIn(reserved, result)

    def test_multiple_patterns_combined(self):
        raw = '<parameter=query>weather</parameter><param name="city">NYC</param><unit>fahrenheit</unit>'
        result = proxy._extract_xml_params(raw)
        self.assertEqual(result.get("query"), "weather")
        self.assertEqual(result.get("city"), "NYC")
        self.assertEqual(result.get("unit"), "fahrenheit")

    def test_empty_input(self):
        self.assertEqual(proxy._extract_xml_params(""), {})

    def test_no_xml(self):
        self.assertEqual(proxy._extract_xml_params('{"key": "value"}'), {})


class TestExtractXmlToolName(unittest.TestCase):
    """Tests for _extract_xml_tool_name() — tool name from XML."""

    def test_function_equals_format(self):
        self.assertEqual(proxy._extract_xml_tool_name("<function=get_weather>"), "get_weather")

    def test_tool_call_with_name_tag(self):
        raw = "<tool_call><name>calculator</name></tool_call>"
        self.assertEqual(proxy._extract_xml_tool_name(raw), "calculator")

    def test_function_with_name_tag(self):
        raw = "<function><name>search</name></function>"
        self.assertEqual(proxy._extract_xml_tool_name(raw), "search")

    def test_no_match_returns_empty(self):
        self.assertEqual(proxy._extract_xml_tool_name("plain text"), "")
        self.assertEqual(proxy._extract_xml_tool_name(""), "")

    def test_multiline_content(self):
        raw = "<function=get_file>\n<parameter=path>/tmp/x</parameter>\n</function>"
        self.assertEqual(proxy._extract_xml_tool_name(raw), "get_file")


class TestIsTruncatedJson(unittest.TestCase):
    """Tests for _is_truncated_json() — detect truncated vs malformed JSON."""

    def test_complete_json_not_truncated(self):
        self.assertFalse(proxy._is_truncated_json('{"key": "value"}'))
        self.assertFalse(proxy._is_truncated_json('["a", "b"]'))
        self.assertFalse(proxy._is_truncated_json("{}"))
        self.assertFalse(proxy._is_truncated_json("[]"))

    def test_empty_not_truncated(self):
        self.assertFalse(proxy._is_truncated_json(""))
        self.assertFalse(proxy._is_truncated_json(None))

    def test_single_opener_truncated(self):
        self.assertTrue(proxy._is_truncated_json("{"))
        self.assertTrue(proxy._is_truncated_json("["))

    def test_unclosed_string(self):
        self.assertTrue(proxy._is_truncated_json('{"key": "value'))

    def test_unmatched_braces(self):
        self.assertTrue(proxy._is_truncated_json('{"key": "value"'))
        self.assertTrue(proxy._is_truncated_json('{"outer": {"inner": "x"}'))

    def test_ends_mid_value(self):
        self.assertTrue(proxy._is_truncated_json('{"key":'))
        self.assertTrue(proxy._is_truncated_json('{"key": "val",'))

    def test_malformed_not_truncated(self):
        self.assertFalse(proxy._is_truncated_json("{not json}"))
        self.assertFalse(proxy._is_truncated_json("just text"))

    def test_nested_array_truncated(self):
        self.assertTrue(proxy._is_truncated_json('{"items": [1, 2, 3'))


class TestParseToolsBlockBody(unittest.TestCase):
    """Tests for _parse_tools_block_body() — parsing <tools> block body."""

    def test_valid_block(self):
        body = '{"name": "Read", "arguments": {"file_path": "/tmp/x"}}'
        result = proxy._parse_tools_block_body(body)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Read")
        self.assertEqual(result["arguments"]["file_path"], "/tmp/x")

    def test_arguments_as_string(self):
        body = '{"name": "Bash", "arguments": "{\\"cmd\\": \\"ls\\"}"}'
        result = proxy._parse_tools_block_body(body)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Bash")
        self.assertEqual(result["arguments"]["cmd"], "ls")

    def test_invalid_json(self):
        self.assertIsNone(proxy._parse_tools_block_body("not json"))

    def test_missing_name(self):
        self.assertIsNone(proxy._parse_tools_block_body('{"arguments": {}}'))

    def test_name_not_string(self):
        self.assertIsNone(proxy._parse_tools_block_body('{"name": 123}'))

    def test_non_dict(self):
        self.assertIsNone(proxy._parse_tools_block_body('["array"]'))

    def test_empty_arguments(self):
        body = '{"name": "Task", "arguments": {}}'
        result = proxy._parse_tools_block_body(body)
        self.assertEqual(result["arguments"], {})

    def test_arguments_not_dict_becomes_empty(self):
        body = '{"name": "Tool", "arguments": [1, 2, 3]}'
        result = proxy._parse_tools_block_body(body)
        self.assertEqual(result["arguments"], {})


class TestGenerateToolSummary(unittest.TestCase):
    """Tests for _generate_tool_summary() — deterministic tool result summary."""

    def test_empty_name(self):
        self.assertEqual(proxy._generate_tool_summary("", ""), "tool")
        self.assertEqual(proxy._generate_tool_summary(None, ""), "tool")

    def test_read_with_file(self):
        result = proxy._generate_tool_summary("Read", " file=/tmp/test.py")
        self.assertEqual(result, 'Read("/tmp/test.py")')

    def test_bash_with_cmd(self):
        result = proxy._generate_tool_summary("Bash", " cmd=ls -la")
        self.assertEqual(result, 'Bash("ls -la")')

    def test_other_tool_no_meta_prefix(self):
        result = proxy._generate_tool_summary("WebSearch", " some query")
        self.assertEqual(result, "WebSearch")

    def test_deterministic_output(self):
        a = proxy._generate_tool_summary("Read", " file=/a/b.txt")
        b = proxy._generate_tool_summary("Read", " file=/a/b.txt")
        self.assertEqual(a, b)

    def test_agent_tool_summary(self):
        result = proxy._generate_tool_summary("Agent", '{"key":"value"}')
        self.assertEqual(result, "Agent")


if __name__ == "__main__":
    unittest.main()
