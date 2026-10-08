#!/usr/bin/env python3
"""DEF-309/EXP-3 计量补全单测：anthropic SSE usage 解析与云端计费回填。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from anthropic_proxy import (  # noqa: E402
    _anthropic_input_total,
    _parse_anthropic_sse_usage,
)


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

    def test_message_start_cache_fields_recorded(self):
        # 缓存命中轮：input 仅未命中部分，真量 = input + cache_read
        acc = {}
        _parse_anthropic_sse_usage(_sse(
            "{'type':'message_start','message':{'usage':{'input_tokens':43,"
            "'cache_read_input_tokens':16768}}}"), acc)
        self.assertEqual(acc, {"input_tokens": 43,
                               "cache_read_input_tokens": 16768})

    def test_message_start_cache_creation_recorded(self):
        acc = {}
        _parse_anthropic_sse_usage(_sse(
            "{'type':'message_start','message':{'usage':{'input_tokens':100,"
            "'cache_creation_input_tokens':50}}}"), acc)
        self.assertEqual(acc, {"input_tokens": 100,
                               "cache_creation_input_tokens": 50})

    def test_no_cache_keeps_legacy_shape(self):
        # 无缓存时 acc 形态不变(旧断言精确相等仍成立)
        acc = {}
        _parse_anthropic_sse_usage(_sse(
            "{'type':'message_start','message':{'usage':{'input_tokens':123,'output_tokens':1}}}"), acc)
        self.assertEqual(acc, {"input_tokens": 123, "output_tokens": 1})


class TestAnthropicInputTotal(unittest.TestCase):
    def test_sums_cache_hits(self):
        self.assertEqual(
            _anthropic_input_total({"input_tokens": 43,
                                    "cache_read_input_tokens": 16768}),
            16811)

    def test_sums_cache_creation(self):
        self.assertEqual(
            _anthropic_input_total({"input_tokens": 8,
                                    "cache_read_input_tokens": 33600,
                                    "cache_creation_input_tokens": 10}),
            33618)

    def test_no_cache_equals_input(self):
        self.assertEqual(_anthropic_input_total({"input_tokens": 123}), 123)

    def test_missing_usage_is_zero(self):
        self.assertEqual(_anthropic_input_total({}), 0)
        self.assertEqual(_anthropic_input_total(None), 0)


if __name__ == "__main__":
    unittest.main()
