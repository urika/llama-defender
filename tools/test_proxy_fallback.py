#!/usr/bin/env python3
"""Unit tests for anthropic_proxy's content-tools fallback.

Run directly:
    python3 tools/test_proxy_fallback.py
Or via unittest discovery from repo root:
    python3 -m unittest discover -s tools -p 'test_*.py' -v
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

# Make sibling anthropic_proxy.py importable.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anthropic_proxy as proxy


class TestExtractContentToolCalls(unittest.TestCase):
    """Pure tests on _extract_content_tool_calls."""

    def test_empty_input(self):
        self.assertEqual(proxy._extract_content_tool_calls(""), {"text": "", "tools": []})
        self.assertEqual(proxy._extract_content_tool_calls(None), {"text": "", "tools": []})

    def test_no_tools_block(self):
        r = proxy._extract_content_tool_calls("just some prose without tool tags")
        self.assertEqual(r["tools"], [])
        self.assertEqual(r["text"], "just some prose without tool tags")

    def test_single_block(self):
        text = '<tools>\n{"name": "get_weather", "arguments": {"city": "Beijing"}}\n</tools>'
        r = proxy._extract_content_tool_calls(text)
        self.assertEqual(len(r["tools"]), 1)
        self.assertEqual(r["tools"][0], {"name": "get_weather", "arguments": {"city": "Beijing"}})
        self.assertEqual(r["text"], "")

    def test_two_blocks(self):
        text = (
            '<tools>\n{"name": "a", "arguments": {"x": 1}}\n</tools>'
            '<tools>\n{"name": "b", "arguments": {"y": 2}}\n</tools>'
        )
        r = proxy._extract_content_tool_calls(text)
        self.assertEqual(len(r["tools"]), 2)
        self.assertEqual([t["name"] for t in r["tools"]], ["a", "b"])

    def test_mixed_prose(self):
        text = 'Sure, calling the tool: <tools>\n{"name": "x", "arguments": {}}\n</tools> done.'
        r = proxy._extract_content_tool_calls(text)
        self.assertEqual(len(r["tools"]), 1)
        self.assertIn("Sure, calling the tool:", r["text"])
        self.assertIn("done.", r["text"])

    def test_malformed_json_preserved(self):
        text = '<tools>\n{this is not json}\n</tools>'
        r = proxy._extract_content_tool_calls(text)
        self.assertEqual(r["tools"], [])
        self.assertIn("<tools>", r["text"])
        self.assertIn("</tools>", r["text"])

    def test_unterminated_block(self):
        text = '<tools>\n{"name": "x", "arguments": {}}\n  (no closing tag)'
        r = proxy._extract_content_tool_calls(text)
        self.assertEqual(r["tools"], [])
        self.assertIn("<tools>", r["text"])

    def test_literal_close_tag_in_args(self):
        # Args containing literal "</tools>" — rfind rescues a JSON-valid parse.
        text = '<tools>\n{"name": "echo", "arguments": {"msg": "watch </tools> here"}}\n</tools>'
        r = proxy._extract_content_tool_calls(text)
        self.assertEqual(len(r["tools"]), 1)
        self.assertEqual(r["tools"][0]["arguments"]["msg"], "watch </tools> here")

    def test_arguments_as_json_string(self):
        text = '<tools>\n{"name": "x", "arguments": "{\\"k\\": 7}"}\n</tools>'
        r = proxy._extract_content_tool_calls(text)
        self.assertEqual(len(r["tools"]), 1)
        self.assertEqual(r["tools"][0]["arguments"], {"k": 7})

    def test_unexpected_json_shape_preserved(self):
        text = '<tools>\n["array", "not", "dict"]\n</tools>'
        r = proxy._extract_content_tool_calls(text)
        self.assertEqual(r["tools"], [])
        self.assertIn("<tools>", r["text"])

    def test_env_var_disabled(self):
        text = '<tools>\n{"name": "x", "arguments": {}}\n</tools>'
        with patch.object(proxy, "CONTENT_TOOLS_FALLBACK_ENABLED", False):
            r = proxy._extract_content_tool_calls(text)
        self.assertEqual(r["tools"], [])
        self.assertEqual(r["text"], text)


class TestStreamingStateMachine(unittest.TestCase):
    """Tests on _StreamingToolsExtractor."""

    def _drain(self, ext, chunks):
        """Feed chunks, finalize, return list of (kind, value) events."""
        events = []
        for c in chunks:
            events.extend(ext.feed(c))
        events.extend(ext.finalize())
        return events

    def _text(self, events):
        return "".join(v for k, v in events if k == "text")

    def _tools(self, events):
        return [v for k, v in events if k == "tool"]

    def test_whole_block_one_chunk(self):
        ext = proxy._StreamingToolsExtractor()
        ev = self._drain(ext, [
            'Hello <tools>\n{"name":"f","arguments":{"a":1}}\n</tools> done',
        ])
        self.assertEqual(self._tools(ev), [{"name": "f", "arguments": {"a": 1}}])
        self.assertEqual(self._text(ev).strip(), "Hello  done".strip())

    def test_block_split_in_body(self):
        ext = proxy._StreamingToolsExtractor()
        ev = self._drain(ext, [
            'Hello <tools>\n{"name":"f","arg',
            'uments":{"a":1}}\n</tools> done',
        ])
        self.assertEqual(self._tools(ev), [{"name": "f", "arguments": {"a": 1}}])
        self.assertIn("Hello ", self._text(ev))
        self.assertIn(" done", self._text(ev))

    def test_trigger_split_at_every_offset(self):
        # Split "<tools>" at positions 1..6.
        body = '\n{"name":"x","arguments":{}}\n</tools> done'
        for split in range(1, 7):
            with self.subTest(split=split):
                ext = proxy._StreamingToolsExtractor()
                chunks = ["pre <tools>"[:4 + split], "<tools>"[split:] + body]
                # Reassemble carefully: the prefix should be "pre " + "<tools>"[:split]
                prefix = "pre " + "<tools>"[:split]
                suffix = "<tools>"[split:] + body
                ev = self._drain(ext, [prefix, suffix])
                self.assertEqual(
                    self._tools(ev),
                    [{"name": "x", "arguments": {}}],
                    f"failed at split={split}; events={ev}",
                )
                self.assertIn("pre ", self._text(ev))

    def test_false_partial_trigger_released(self):
        ext = proxy._StreamingToolsExtractor()
        # "<tags>" is not "<tools>" — should be released as text after diverging.
        ev = self._drain(ext, ["<tags>some xml</tags>"])
        self.assertEqual(self._tools(ev), [])
        self.assertEqual(self._text(ev), "<tags>some xml</tags>")

    def test_pure_text_passthrough(self):
        ext = proxy._StreamingToolsExtractor()
        ev = self._drain(ext, ["hello ", "world", "!"])
        self.assertEqual(self._tools(ev), [])
        self.assertEqual(self._text(ev), "hello world!")

    def test_adjacent_blocks(self):
        ext = proxy._StreamingToolsExtractor()
        ev = self._drain(ext, [
            '<tools>\n{"name":"a","arguments":{}}\n</tools>'
            '<tools>\n{"name":"b","arguments":{}}\n</tools>',
        ])
        names = [t["name"] for t in self._tools(ev)]
        self.assertEqual(names, ["a", "b"])

    def test_eof_inside_tools(self):
        ext = proxy._StreamingToolsExtractor()
        ev = self._drain(ext, ['<tools>\n{"name":"x"'])  # no closing tag
        self.assertEqual(self._tools(ev), [])
        # Raw block preserved as text.
        self.assertIn("<tools>", self._text(ev))
        self.assertIn('"name":"x"', self._text(ev))

    def test_literal_close_tag_in_args_streaming(self):
        ext = proxy._StreamingToolsExtractor()
        ev = self._drain(ext, [
            '<tools>\n{"name":"echo","arguments":{"msg":"see </tools> inside"}}\n</tools>',
        ])
        tools = self._tools(ev)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["arguments"]["msg"], "see </tools> inside")

    def test_trigger_at_stream_start(self):
        ext = proxy._StreamingToolsExtractor()
        ev = self._drain(ext, [
            '<tools>\n{"name":"x","arguments":{}}\n</tools>after',
        ])
        self.assertEqual(self._tools(ev), [{"name": "x", "arguments": {}}])
        self.assertEqual(self._text(ev), "after")

    def test_pending_text_bounded(self):
        """pending_text must never exceed len('<tools>')-1 = 6 chars."""
        ext = proxy._StreamingToolsExtractor()
        # Send a long stream of partial-trigger-like chars one at a time.
        for c in "<<<<<<<<<<<<<<<<":
            ext.feed(c)
            self.assertLess(len(ext.pending_text), len(proxy.TOOLS_TRIGGER))

    def test_disabled_passthrough(self):
        with patch.object(proxy, "CONTENT_TOOLS_FALLBACK_ENABLED", False):
            ext = proxy._StreamingToolsExtractor()
            ev = self._drain(ext, [
                '<tools>\n{"name":"x","arguments":{}}\n</tools>',
            ])
        self.assertEqual(self._tools(ev), [])
        self.assertIn("<tools>", self._text(ev))


class TestNonStreamingConversion(unittest.TestCase):
    """Tests on convert_openai_response_to_anthropic."""

    def _openai_resp(self, content="", tool_calls=None, finish="stop"):
        return {
            "id": "chatcmpl-test",
            "choices": [{
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls or [],
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    def test_plain_text(self):
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(content="hello world"),
            anthropic_model="claude-sonnet-4-6",
        )
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertEqual(len(r["content"]), 1)
        self.assertEqual(r["content"][0]["type"], "text")

    def test_structured_tool_calls_qwen3x_path(self):
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(
                content="",
                tool_calls=[{
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Beijing"}'},
                }],
                finish="tool_calls",
            ),
            anthropic_model="claude-sonnet-4-6",
        )
        self.assertEqual(r["stop_reason"], "tool_use")
        tool_blocks = [b for b in r["content"] if b["type"] == "tool_use"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["name"], "get_weather")
        self.assertEqual(tool_blocks[0]["input"], {"city": "Beijing"})

    def test_content_tools_qwen25coder_path(self):
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(
                content='<tools>\n{"name": "get_weather", "arguments": {"city": "Beijing"}}\n</tools>',
                finish="stop",
            ),
            anthropic_model="claude-sonnet-4-6",
        )
        self.assertEqual(r["stop_reason"], "tool_use")
        tool_blocks = [b for b in r["content"] if b["type"] == "tool_use"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["name"], "get_weather")
        self.assertEqual(tool_blocks[0]["input"], {"city": "Beijing"})

    def test_structured_wins_over_content(self):
        """If both structured tool_calls AND content <tools> are present, prefer structured."""
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(
                content='<tools>\n{"name": "wrong", "arguments": {}}\n</tools>',
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "right", "arguments": "{}"},
                }],
                finish="tool_calls",
            ),
            anthropic_model="claude-sonnet-4-6",
        )
        tool_blocks = [b for b in r["content"] if b["type"] == "tool_use"]
        # Structured wins: only "right" should appear.
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["name"], "right")

    def test_malformed_content_tools_preserved_as_text(self):
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(content='<tools>\n{not json}\n</tools>'),
            anthropic_model="claude-sonnet-4-6",
        )
        self.assertEqual(r["stop_reason"], "end_turn")
        text_blocks = [b for b in r["content"] if b["type"] == "text"]
        self.assertEqual(len(text_blocks), 1)
        self.assertIn("<tools>", text_blocks[0]["text"])

    def test_max_tokens_not_overridden(self):
        """When finish_reason=length, stop_reason should stay max_tokens even if content tools found."""
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(
                content='<tools>\n{"name": "x", "arguments": {}}\n</tools>',
                finish="length",
            ),
            anthropic_model="claude-sonnet-4-6",
        )
        # max_tokens is preserved — synthesize block is appended but stop_reason indicates truncation.
        self.assertEqual(r["stop_reason"], "max_tokens")


if __name__ == "__main__":
    unittest.main(verbosity=2)
