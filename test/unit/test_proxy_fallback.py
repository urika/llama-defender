#!/usr/bin/env python3
"""Unit tests for anthropic_proxy's content-tools fallback.

Run directly:
    python3 test/unit/test_proxy_fallback.py
Or via unittest discovery from repo root:
    python3 -m unittest discover -s test/unit -p 'test_*.py' -v
Or via the unified runner:
    bash test/run_tests.sh --unit
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

# Walk up to the repo root (test/unit/ → test/ → repo root).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


class TestBlockerDetection(unittest.TestCase):
    """Tests on _detect_blocker_pattern and _build_blocker_message."""

    def _assistant_tool_use(self, name, args=None, tool_id="t1"):
        return {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args or {}}],
        }

    def _user_tool_result(self, content, tool_id="t1"):
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}],
        }

    def _user_text(self, text="hi"):
        return {"role": "user", "content": text}

    def test_disabled_short_circuits(self):
        with patch.object(proxy, "PROXY_BLOCKER_ENABLED", False):
            msgs = [
                self._assistant_tool_use("Read"),
                self._user_tool_result("[System: 文件不存在。请先用 Bash ls 或 find 命令确认项目结构，然后使用正确的文件路径。]"),
                self._assistant_tool_use("Read"),
                self._user_tool_result("[System: 文件不存在。请先用 Bash ls 或 find 命令确认项目结构，然后使用正确的文件路径。]"),
            ]
            r = proxy._detect_blocker_pattern(msgs)
        self.assertFalse(r["triggered"])
        self.assertEqual(r.get("reason"), "disabled")

    def test_below_threshold(self):
        with patch.object(proxy, "PROXY_BLOCKER_THRESHOLD", 3):
            msgs = [
                self._assistant_tool_use("Read"),
                self._user_tool_result("[System: 文件不存在]"),
                self._assistant_tool_use("Read"),
                self._user_tool_result("[System: 文件不存在]"),
            ]
            r = proxy._detect_blocker_pattern(msgs)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["run_length"], 2)

    def test_at_threshold_file_not_found(self):
        msgs = [
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
        ]
        r = proxy._detect_blocker_pattern(msgs)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["tool_name"], "Read")
        self.assertEqual(r["error_type"], "file_not_found")
        self.assertEqual(r["run_length"], 2)

    def test_at_threshold_wasted(self):
        msgs = [
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 该文件自上次读取后未发生变化]"),
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 该文件自上次读取后未发生变化]"),
        ]
        r = proxy._detect_blocker_pattern(msgs)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["error_type"], "wasted")
        self.assertEqual(r["tool_name"], "Read")

    def test_at_threshold_input_validation(self):
        msgs = [
            self._assistant_tool_use("Bash"),
            self._user_tool_result("[System: 工具调用参数错误]"),
            self._assistant_tool_use("Bash"),
            self._user_tool_result("[System: 工具调用参数错误]"),
        ]
        r = proxy._detect_blocker_pattern(msgs)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["error_type"], "input_validation")
        self.assertEqual(r["tool_name"], "Bash")

    def test_breaks_on_non_error_result(self):
        """A non-error tool_result in the middle breaks the consecutive run."""
        msgs = [
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
            self._assistant_tool_use("Read"),
            self._user_tool_result("File contents: def foo(): pass\n"),
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
        ]
        r = proxy._detect_blocker_pattern(msgs)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["run_length"], 1)

    def test_breaks_on_user_text(self):
        """A user text message breaks the consecutive-error tail."""
        msgs = [
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
            self._user_text("maybe try a different path?"),
        ]
        r = proxy._detect_blocker_pattern(msgs)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["run_length"], 0)

    def test_three_in_a_row(self):
        msgs = [
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),
        ]
        r = proxy._detect_blocker_pattern(msgs)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["run_length"], 3)
        self.assertEqual(r["tool_name"], "Read")

    def test_mixed_error_types_do_not_trigger(self):
        """Mixed error types (wasted + file_not_found) should not trigger
        because the run is broken by the type change."""
        msgs = [
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 该文件自上次读取后未发生变化]"),  # wasted
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),  # file_not_found
        ]
        r = proxy._detect_blocker_pattern(msgs)
        # The most recent is file_not_found; the one before is wasted (different type).
        # Walk: t2 (file_not_found) → run starts. t1 (wasted) → different type → break.
        # Net run_length=1, below threshold.
        self.assertFalse(r["triggered"])
        self.assertEqual(r["run_length"], 1)

    def test_two_same_after_different_breaks_run(self):
        """2 file_not_found followed by 1 wasted followed by 1 file_not_found:
        the wasted breaks the run, so only the most recent file_not_found counts."""
        msgs = [
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),  # file_not_found
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),  # file_not_found
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 该文件自上次读取后未发生变化]"),  # wasted — breaks run
            self._assistant_tool_use("Read"),
            self._user_tool_result("[System: 文件不存在]"),  # file_not_found (most recent)
        ]
        r = proxy._detect_blocker_pattern(msgs)
        # Walk backward: t4 (file_not_found) → run starts. t3 (wasted) → break.
        # Net run_length=1.
        self.assertFalse(r["triggered"])
        self.assertEqual(r["run_length"], 1)

    def test_blocker_message_is_cache_stable(self):
        """Same (tool, error_type, run_length) → identical text → prefix cache hits."""
        m1 = proxy._build_blocker_message("Read", "file_not_found", 2)
        m2 = proxy._build_blocker_message("Read", "file_not_found", 2)
        self.assertEqual(m1["content"][0]["text"], m2["content"][0]["text"])

    def test_blocker_message_mentions_tool_and_error(self):
        msg = proxy._build_blocker_message("Bash", "input_validation", 3)
        text = msg["content"][0]["text"]
        self.assertIn("Bash", text)
        self.assertIn("input_validation", text)
        self.assertIn("3", text)
        self.assertIn("[BLOCKER]", text)


class TestCompressPromptStructure(unittest.TestCase):
    """Smoke test: the LLM compression prompt enforces the new errors_solutions structure."""

    def test_prompt_requires_root_cause_structure(self):
        # Force the LLM call path to capture the prompt without hitting the network.
        captured = {}

        def fake_urlopen(req, timeout=None):
            class _Resp:
                def __enter__(self2): return self2
                def __exit__(self2, *a): return False
                def read(self2): return b'{"choices":[{"message":{"content":"<errors_solutions>none</errors_solutions>"}}]}'
            captured["prompt"] = json.loads(req.data)["messages"][0]["content"]
            return _Resp()

        with patch.object(proxy.urllib.request, "urlopen", fake_urlopen):
            proxy._compress_middle_with_llm([
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "ok"},
            ], timeout=1)
        prompt = captured["prompt"]
        self.assertIn("Root cause:", prompt)
        self.assertIn("Fix:", prompt)
        self.assertIn("Avoidance:", prompt)
        self.assertIn("<errors_solutions>", prompt)
        self.assertIn("<pending>", prompt)


class TestFifoPlaceholderStability(unittest.TestCase):
    """Plan 1: FIFO placeholder text MUST be byte-stable across requests so
    prefix cache hits the same bytes at the fold boundary. Previously the
    placeholder embedded dropped_count, tool_count, and file_mentions which
    changed every request, causing 0% cache hit rate."""

    def setUp(self):
        # Force fifo strategy for these tests regardless of the process env.
        self._patches = [
            patch.object(proxy, "PROXY_CTX_TRUNCATE_STRATEGY", "fifo"),
            patch.object(proxy, "PROXY_CTX_LIMIT_ENABLED", True),
            patch.object(proxy, "PROXY_CTX_KEEP_MESSAGES", 40),
            patch.object(proxy, "PROXY_CTX_KEEP_HEAD", 2),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _make_msgs(self, n_total):
        msgs = [
            {"role": "system", "content": "you are a helpful coding assistant."},
            {"role": "user", "content": "please help me"},
        ]
        for i in range(n_total - 2):
            if i % 2 == 0:
                msgs.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": f"t{i}", "name": "Read",
                     "input": {"file_path": f"/tmp/file_{i}.py"}},
                ]})
            else:
                msgs.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": f"t{i-1}",
                     "content": f"contents {i-1}"},
                ]})
        return msgs

    def _find_placeholder(self, result):
        for m in result:
            if isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and "Context folded" in b.get("text", ""):
                        return b["text"]
        return None

    def test_placeholder_stable_across_message_counts(self):
        """5 different total message counts must produce the same placeholder."""
        seen = set()
        for n in [50, 55, 60, 70, 80]:
            msgs = self._make_msgs(n)
            result, stats = proxy.truncate_messages_if_needed(msgs, session_id="t")
            self.assertTrue(stats.get("truncated"), f"n={n} should trigger truncation")
            pt = self._find_placeholder(result)
            self.assertIsNotNone(pt, f"n={n} should produce a placeholder")
            seen.add(pt)
        self.assertEqual(len(seen), 1,
            f"placeholder text must be identical for cache stability; got {len(seen)} variants: {seen}")

    def test_placeholder_text_is_static(self):
        """The placeholder must be exactly the fixed Plan 1 text."""
        msgs = self._make_msgs(60)
        result, _ = proxy.truncate_messages_if_needed(msgs, session_id="t")
        pt = self._find_placeholder(result)
        self.assertEqual(pt, "[Context folded: earlier messages omitted.]")

    def test_placeholder_dynamic_info_still_in_stats(self):
        """Plan 1 keeps the dropped/tool/file_mentions data available for
        metrics logging — only the prompt text is fixed."""
        msgs = self._make_msgs(60)
        _, stats = proxy.truncate_messages_if_needed(msgs, session_id="t")
        self.assertIn("dropped_messages", stats)
        self.assertIn("tool_count", stats)
        self.assertIn("file_mentions", stats)
        self.assertGreater(stats["dropped_messages"], 0)


# =============================================================================
# R2.1 — Loop intervention (Level 1 + Level 2 escalation)
# =============================================================================
class TestLoopIntervention(unittest.TestCase):
    """Covers _apply_loop_intervention — extracted from _handle_messages
    so we can assert on the level-mapping rules in isolation.
    """

    def _tool(self, name):
        return {"name": name, "description": "fake", "input_schema": {"type": "object"}}

    def test_level1_injects_stop_message_and_keeps_tool(self):
        """max_run == threshold (3) and < LEVEL2 (6) → Level 1.
        The user message must mention 'STOP using' and the tool must remain
        in the tools list (Level 1 does not remove it)."""
        consecutive = {"Read:{\"/x.py\"}": 3}
        tools = [self._tool("Read"), self._tool("Bash")]
        msgs, new_tools, level, tool_name = proxy._apply_loop_intervention(
            raw_messages=[],
            raw_tools=tools,
            max_run=3,
            consecutive=consecutive,
            threshold=3, level2_threshold=6,
        )
        self.assertEqual(level, 1)
        self.assertEqual(tool_name, "Read")
        # The new user message was appended.
        self.assertEqual(len(msgs), 1)
        text = msgs[0]["content"][0]["text"]
        self.assertIn("STOP using Read", text)
        self.assertIn("[System notice:", text)
        # Tools list is unchanged at Level 1.
        self.assertEqual(new_tools, tools)

    def test_level2_removes_tool_and_emits_strong_message(self):
        """max_run == LEVEL2 (6) → Level 2.
        The looping tool must be filtered out of the tools list and the
        message must use the 'REMOVED' wording."""
        consecutive = {"Read:{\"/x.py\"}": 6}
        tools = [self._tool("Read"), self._tool("Bash"), self._tool("Glob")]
        msgs, new_tools, level, tool_name = proxy._apply_loop_intervention(
            raw_messages=[{"role": "user", "content": "do the thing"}],
            raw_tools=tools,
            max_run=6,
            consecutive=consecutive,
            threshold=3, level2_threshold=6,
        )
        self.assertEqual(level, 2)
        self.assertEqual(tool_name, "Read")
        # Tool removed.
        self.assertEqual([t["name"] for t in new_tools], ["Bash", "Glob"])
        # Strong message injected (replaces the Level-1 message).
        text = msgs[-1]["content"][0]["text"]
        self.assertIn("REMOVED", text)
        self.assertIn("Read", text)
        self.assertIn("MUST use a different approach", text)
        # Original message preserved.
        self.assertEqual(msgs[0], {"role": "user", "content": "do the thing"})

    def test_below_threshold_is_a_pure_noop(self):
        """max_run < threshold → no message added, no tool removed,
        level == 0, tool_name == ''."""
        consecutive = {"Read:{\"/x.py\"}": 2}
        tools = [self._tool("Read"), self._tool("Bash")]
        msgs, new_tools, level, tool_name = proxy._apply_loop_intervention(
            raw_messages=[{"role": "user", "content": "hi"}],
            raw_tools=tools,
            max_run=2,
            consecutive=consecutive,
            threshold=3, level2_threshold=6,
        )
        self.assertEqual(level, 0)
        self.assertEqual(tool_name, "")
        # No new user message; the original is untouched.
        self.assertEqual(msgs, [{"role": "user", "content": "hi"}])
        self.assertEqual(new_tools, tools)


# =============================================================================
# R4.4 — _repair_truncated_json (7 cases)
# =============================================================================
class TestRepairTruncatedJson(unittest.TestCase):
    """Covers _repair_truncated_json — invoked when Qwen's tool_call
    arguments JSON is cut off mid-stream."""

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(proxy._repair_truncated_json(""), "{}")
        self.assertEqual(proxy._repair_truncated_json("   "), "{}")
        self.assertEqual(proxy._repair_truncated_json(None), "{}")

    def test_complete_json_unchanged(self):
        # Well-formed inputs are returned verbatim.
        ok = '{"a": 1, "b": [2, 3]}'
        self.assertEqual(proxy._repair_truncated_json(ok), ok)

    def test_unclosed_string_closes_quote_and_brace(self):
        # The string was never closed (e.g. arguments cut after `{"file": "`).
        # Repair should append `"` then `}`.
        out = proxy._repair_truncated_json('{"file": "/tmp/x')
        parsed = json.loads(out)
        self.assertEqual(parsed, {"file": "/tmp/x"})

    def test_unclosed_brace_adds_closing_brace(self):
        # No unclosed string, but one too few `}`s.
        out = proxy._repair_truncated_json('{"a": 1, "b": 2')
        parsed = json.loads(out)
        self.assertEqual(parsed, {"a": 1, "b": 2})

    def test_nested_truncation_closes_multiple_levels(self):
        # depth=2 (object inside object) → must add two `}`s.
        # (Note: _repair_truncated_json only emits `}` to close, not `]`,
        # so we avoid trailing-`[` inputs here — that case is a known
        # limitation of the current implementation.)
        out = proxy._repair_truncated_json('{"outer": {"inner": 1, "a": 2')
        parsed = json.loads(out)
        self.assertEqual(parsed, {"outer": {"inner": 1, "a": 2}})

    def test_escape_sequence_does_not_falsely_close(self):
        # The function tracks `in_string` correctly across `\\` and `\"` —
        # a JSON `"\""` should NOT be treated as ending the string.
        out = proxy._repair_truncated_json(r'{"msg": "say \"hello\"')
        parsed = json.loads(out)
        self.assertEqual(parsed, {"msg": 'say "hello"'})

    def test_whitespace_only_input_returns_empty_dict(self):
        # Newlines and tabs are stripped; if nothing remains we get "{}".
        self.assertEqual(proxy._repair_truncated_json("\n\t  \n"), "{}")


# =============================================================================
# R6.1 / R6.2 — structured metrics + quality_flags
# =============================================================================
class TestMetrics(unittest.TestCase):
    """Covers log_metrics (R6.1) and _finalize_metrics quality_flags (R6.2)."""

    def setUp(self):
        # Redirect PROXY_METRICS_DIR to a temp file so log_metrics doesn't
        # touch the real logs/proxy_metrics.jsonl.
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._metrics_path = os.path.join(self._tmpdir, "metrics.jsonl")
        self._patches = [
            patch.object(proxy, "_METRICS_PATH", self._metrics_path),
            patch.object(proxy, "_ensure_jsonl_dir", lambda: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_log_metrics_writes_valid_jsonl_line(self):
        """R6.1: a metrics dict is serialized as one line of valid JSON
        and appended to the configured metrics file."""
        proxy.log_metrics({
            "session_id": "sess_abc",
            "input_msgs": 42,
            "input_chars": 1000,
            "input_tools": 15,
            "pipeline": {"tool_filter": {"original": 44, "kept": 15}},
            "compression_ratio": 0.74,
            "quality_flags": ["loop_injected"],
        })
        with open(self._metrics_path) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        # Required fields per PRD §5.8 schema are all present and well-typed.
        self.assertEqual(record["session_id"], "sess_abc")
        self.assertEqual(record["input_msgs"], 42)
        self.assertEqual(record["input_tools"], 15)
        self.assertEqual(record["compression_ratio"], 0.74)
        self.assertEqual(record["quality_flags"], ["loop_injected"])
        self.assertIn("tool_filter", record["pipeline"])

    def test_finalize_metrics_quality_flags(self):
        """R6.2: _finalize_metrics derives the four quality_flags from
        the pipeline state. Verify each flag is correctly emitted under
        the conditions that should trigger it, and stays absent otherwise."""
        # (a) high_drop_ratio: dropped / (dropped + kept) > 0.7
        mc_a = {"pipeline": {"truncate": {"triggered": True, "dropped": 80, "kept": 10, "compression": "llm",
                                          "est_tokens_after": 5000, "budget": 5000}}}
        proxy._finalize_metrics(mc_a)
        self.assertIn("high_drop_ratio", mc_a["quality_flags"])
        # (b) llm_compress_failed: compression in {rules, folded} and dropped >= 10
        mc_b = {"pipeline": {"truncate": {"triggered": True, "dropped": 12, "kept": 5, "compression": "folded",
                                          "est_tokens_after": 3000, "budget": 5000}}}
        proxy._finalize_metrics(mc_b)
        self.assertIn("llm_compress_failed", mc_b["quality_flags"])
        # (c) budget_overflow: est_after > budget * 1.1
        mc_c = {"pipeline": {"truncate": {"triggered": True, "dropped": 5, "kept": 20, "compression": "llm",
                                          "est_tokens_after": 6000, "budget": 5000}}}
        proxy._finalize_metrics(mc_c)
        self.assertIn("budget_overflow", mc_c["quality_flags"])
        # (d) loop_injected: loop_detect.max_run >= PROXY_LOOP_THRESHOLD (default 3)
        mc_d = {"pipeline": {"truncate": {"triggered": False, "dropped": 0, "kept": 0,
                                          "est_tokens_after": 0, "budget": 0},
                             "loop_detect": {"max_run": 5}}}
        proxy._finalize_metrics(mc_d)
        self.assertIn("loop_injected", mc_d["quality_flags"])
        # (e) clean request: no flag should fire
        mc_e = {"pipeline": {"truncate": {"triggered": False, "dropped": 0, "kept": 0,
                                          "est_tokens_after": 0, "budget": 0},
                             "loop_detect": {"max_run": 1}}}
        proxy._finalize_metrics(mc_e)
        self.assertEqual(mc_e["quality_flags"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
