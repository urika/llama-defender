"""Additional unit tests for message_converter module."""
import json
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import message_converter as mc


class TestExtractTextFromMessages(unittest.TestCase):
    def test_simple_text(self):
        msgs = [{"role": "user", "content": "hello"}]
        self.assertEqual(mc._extract_text_from_messages(msgs), "hello")

    def test_text_blocks(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]
        self.assertEqual(mc._extract_text_from_messages(msgs), "a\nb")

    def test_tool_result_included(self):
        msgs = [{"role": "user", "content": [{"type": "tool_result", "content": "result"}]}]
        self.assertEqual(mc._extract_text_from_messages(msgs), "result")

    def test_tool_use_json(self):
        msgs = [{"role": "assistant", "content": [{"type": "tool_use", "input": {"x": 1}}]}]
        self.assertEqual(mc._extract_text_from_messages(msgs), '{"x": 1}')

    def test_mixed_messages(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ]
        self.assertEqual(mc._extract_text_from_messages(msgs), "hi\nhello")


class TestEstimateTokensDynamic(unittest.TestCase):
    def test_english_ratio(self):
        msgs = [{"role": "user", "content": "hello world " * 100}]
        tok = mc._estimate_tokens_dynamic(msgs)
        self.assertGreater(tok, 0)

    def test_chinese_ratio(self):
        msgs = [{"role": "user", "content": "人工智能" * 100}]
        tok = mc._estimate_tokens_dynamic(msgs)
        self.assertGreater(tok, 0)

    def test_code_ratio(self):
        msgs = [{"role": "user", "content": "def foo():\n    return [x for x in range(100)]\n" * 50}]
        tok = mc._estimate_tokens_dynamic(msgs)
        self.assertGreater(tok, 0)

    def test_ratio_override(self):
        msgs = [{"role": "user", "content": "hello" * 100}]
        tok = mc._estimate_tokens_dynamic(msgs, ratio_override=2.0)
        self.assertEqual(tok, 250)

    def test_short_text_blending(self):
        msgs = [{"role": "user", "content": "hello"}]
        tok = mc._estimate_tokens_dynamic(msgs)
        self.assertGreater(tok, 0)


class TestConvertAnthropicTools(unittest.TestCase):
    def test_web_search_mapping(self):
        tools = [{"type": "web_search_20250305", "name": "web_search"}]
        out = mc.convert_anthropic_tools_to_openai(tools)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "function")
        self.assertEqual(out[0]["function"]["name"], "web_search")
        self.assertIn("query", out[0]["function"]["parameters"]["required"])

    def test_simple_tool(self):
        tools = [{"name": "Read", "description": "read file", "parameters": {"type": "object"}}]
        out = mc.convert_anthropic_tools_to_openai(tools)
        self.assertEqual(out[0]["function"]["name"], "Read")

    def test_empty_tools(self):
        self.assertIsNone(mc.convert_anthropic_tools_to_openai([]))
        self.assertIsNone(mc.convert_anthropic_tools_to_openai(None))


class TestToolChoiceConversion(unittest.TestCase):
    def test_auto_string(self):
        self.assertEqual(mc.convert_anthropic_tool_choice_to_openai("auto"), "auto")

    def test_any_string(self):
        self.assertEqual(mc.convert_anthropic_tool_choice_to_openai("any"), {"type": "function"})

    def test_none_string(self):
        self.assertEqual(mc.convert_anthropic_tool_choice_to_openai("none"), "none")

    def test_tool_dict(self):
        self.assertEqual(
            mc.convert_anthropic_tool_choice_to_openai({"type": "tool", "name": "Read"}),
            {"type": "function", "function": {"name": "Read"}}
        )

    def test_none_input(self):
        self.assertIsNone(mc.convert_anthropic_tool_choice_to_openai(None))


class TestConvertOpenAIToolsToAnthropic(unittest.TestCase):
    def test_function_tool_mapping(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "Read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }]
        out = mc.convert_openai_tools_to_anthropic(tools)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "custom")
        self.assertEqual(out[0]["name"], "Read")
        self.assertEqual(out[0]["input_schema"]["type"], "object")

    def test_empty_tools(self):
        self.assertIsNone(mc.convert_openai_tools_to_anthropic([]))
        self.assertIsNone(mc.convert_openai_tools_to_anthropic(None))


class TestConvertOpenAIToolChoiceToAnthropic(unittest.TestCase):
    def test_auto_string(self):
        self.assertEqual(mc.convert_openai_tool_choice_to_anthropic("auto"), "auto")

    def test_none_string(self):
        self.assertEqual(mc.convert_openai_tool_choice_to_anthropic("none"), "none")

    def test_required_string(self):
        self.assertEqual(mc.convert_openai_tool_choice_to_anthropic("required"), "any")

    def test_function_dict(self):
        self.assertEqual(
            mc.convert_openai_tool_choice_to_anthropic({"type": "function", "function": {"name": "Read"}}),
            {"type": "tool", "name": "Read"},
        )


class TestConvertOpenAIRequestToAnthropic(unittest.TestCase):
    def test_basic_messages(self):
        body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        }
        out = mc.convert_openai_request_to_anthropic(body)
        self.assertEqual(out["model"], "claude-sonnet-4-6")
        self.assertEqual(out["max_tokens"], 1024)
        self.assertEqual(out["messages"][0]["role"], "user")
        self.assertEqual(out["messages"][0]["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(out["messages"][1]["content"], [{"type": "text", "text": "hi there"}])

    def test_system_messages_collected(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {"role": "system", "content": "You are a coder."},
                {"role": "system", "content": [{"type": "text", "text": "Be concise."}]},
                {"role": "user", "content": "ok"},
            ],
        }
        out = mc.convert_openai_request_to_anthropic(body)
        self.assertEqual(out["system"], "You are a coder.\nBe concise.")
        self.assertEqual(len(out["messages"]), 1)

    def test_tool_calls_and_results(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"path": "/tmp/a"}'},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
            ],
        }
        out = mc.convert_openai_request_to_anthropic(body)
        assistant = out["messages"][0]
        self.assertEqual(assistant["content"][0]["type"], "tool_use")
        self.assertEqual(assistant["content"][0]["input"], {"path": "/tmp/a"})
        user = out["messages"][1]
        self.assertEqual(user["content"][0]["type"], "tool_result")
        self.assertEqual(user["content"][0]["tool_use_id"], "call_1")

    def test_tools_and_tool_choice(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{
                "type": "function",
                "function": {"name": "Read", "description": "read", "parameters": {"type": "object"}},
            }],
            "tool_choice": {"type": "function", "function": {"name": "Read"}},
        }
        out = mc.convert_openai_request_to_anthropic(body)
        self.assertEqual(out["tools"][0]["type"], "custom")
        self.assertEqual(out["tool_choice"], {"type": "tool", "name": "Read"})

    def test_stop_and_sampling_preserved(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
            "top_p": 0.9,
            "stream": True,
            "stop": ["end", "stop"],
        }
        out = mc.convert_openai_request_to_anthropic(body)
        self.assertEqual(out["temperature"], 0.5)
        self.assertEqual(out["top_p"], 0.9)
        self.assertTrue(out["stream"])
        self.assertEqual(out["stop_sequences"], ["end", "stop"])

    def test_preserves_route_override(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
            "_x_proxy_route_to": "cloud",
        }
        out = mc.convert_openai_request_to_anthropic(body)
        self.assertEqual(out["_x_proxy_route_to"], "cloud")


if __name__ == "__main__":
    unittest.main()
