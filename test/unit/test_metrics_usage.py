#!/usr/bin/env python3
"""DEF-309/EXP-3 计量补全单测：anthropic SSE usage 解析与云端计费回填。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from anthropic_proxy import _parse_anthropic_sse_usage  # noqa: E402


def _sse(event):
    return ('data: ' + event.replace("'", '"')).encode("utf-8")


class TestParseAnthropicSseUsage(unittest.TestCase):
    def test_message_start_input_tokens(self):
        acc = {}
        _parse_anthropic_sse_usage(_sse(
            "{'type':'message_start','message':{'usage':{'input_tokens':123,'output_tokens':1}}}"), acc)
        self.assertEqual(acc, {"input_tokens": 123, "output_tokens": 1})

    def test_message_delta_output_tokens(self):
        acc = {"input_tokens": 123, "output_tokens": 0}
        _parse_anthropic_sse_usage(_sse(
            "{'type':'message_delta','delta':{},'usage':{'output_tokens':456}}"), acc)
        self.assertEqual(acc, {"input_tokens": 123, "output_tokens": 456})

    def test_non_usage_lines_ignored(self):
        acc = {}
        _parse_anthropic_sse_usage(
            b'data: {"type":"content_block_delta","delta":{"text":"hi"}}', acc)
        _parse_anthropic_sse_usage(b'event: ping', acc)
        _parse_anthropic_sse_usage(b'garbage bytes \xb8', acc)
        self.assertEqual(acc, {})

    def test_zero_usage_not_recorded(self):
        acc = {}
        _parse_anthropic_sse_usage(_sse(
            "{'type':'message_start','message':{'usage':{'input_tokens':0}}}"), acc)
        self.assertEqual(acc, {})


if __name__ == "__main__":
    unittest.main()
