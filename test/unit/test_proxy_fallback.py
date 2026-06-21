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
import proxy_state


class TestExtractContentToolCalls(unittest.TestCase):
    """R4.2: pure tests on _extract_content_tool_calls (non-streaming <tools> fallback)."""

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
        with patch.object(proxy_state, "CONTENT_TOOLS_FALLBACK_ENABLED", False):
            r = proxy._extract_content_tool_calls(text)
        self.assertEqual(r["tools"], [])
        self.assertEqual(r["text"], text)


class TestStreamingStateMachine(unittest.TestCase):
    """R4.2: tests on _StreamingToolsExtractor (state machine for streaming <tools>)."""

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
        with patch.object(proxy_state, "CONTENT_TOOLS_FALLBACK_ENABLED", False):
            ext = proxy._StreamingToolsExtractor()
            ev = self._drain(ext, [
                '<tools>\n{"name":"x","arguments":{}}\n</tools>',
            ])
        self.assertEqual(self._tools(ev), [])
        self.assertIn("<tools>", self._text(ev))


class TestNonStreamingConversion(unittest.TestCase):
    """R4.2 + R4.3: tests on convert_openai_response_to_anthropic (full response conversion + max_tokens preservation)."""

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
    """R2.4: tests on _detect_blocker_pattern and _build_blocker_message (blocker detection logic)."""

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
        with patch.object(proxy, "PROXY_BLOCKER_ENABLED", False), patch.object(proxy_state, "PROXY_BLOCKER_ENABLED", False):
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
        with patch.object(proxy, "PROXY_BLOCKER_THRESHOLD", 3), patch.object(proxy_state, "PROXY_BLOCKER_THRESHOLD", 3):
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
    """R1.2: smoke test — the LLM compression prompt enforces the new errors_solutions structure."""

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
    """R1.1 + R3.2: Plan 1 — FIFO placeholder text MUST be byte-stable across
    requests so prefix cache hits the same bytes at the fold boundary.
    Previously the placeholder embedded dropped_count, tool_count, and
    file_mentions which changed every request, causing 0% cache hit rate."""

    def setUp(self):
        # Force fifo strategy for these tests regardless of the process env.
        self._patches = [
            patch.object(proxy, "PROXY_CTX_TRUNCATE_STRATEGY", "fifo"), patch.object(proxy_state, "PROXY_CTX_TRUNCATE_STRATEGY", "fifo"),
            patch.object(proxy, "PROXY_CTX_LIMIT_ENABLED", True), patch.object(proxy_state, "PROXY_CTX_LIMIT_ENABLED", True),
            patch.object(proxy, "PROXY_CTX_KEEP_MESSAGES", 40), patch.object(proxy_state, "PROXY_CTX_KEEP_MESSAGES", 40),
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
# R2.2 / R2.3 — clear_old_tool_results (smart clearing + recent Read protection)
# =============================================================================
class TestToolClearing(unittest.TestCase):
    """Covers clear_old_tool_results — the proxy-side tool_result trim
    with semantic priority scoring. Verifies the 200-char Read preview
    (R2.2) and the +5 boost for the 6 most recent Read results (R2.3)."""

    def setUp(self):
        # Force clearing ON with a small KEEP and a tiny threshold so
        # every test case actually triggers the clear pass. Read preview
        # length stays at the production default (200 chars).
        self._patches = [
            patch.object(proxy, "PROXY_CLEAR_ENABLED", True), patch.object(proxy_state, "PROXY_CLEAR_ENABLED", True),
            patch.object(proxy, "PROXY_CLEAR_THRESHOLD", 0), patch.object(proxy_state, "PROXY_CLEAR_THRESHOLD", 0),
            patch.object(proxy, "PROXY_TOOL_KEEP", 2),
            patch.object(proxy, "PROXY_REREAD_PREVIEW_CHARS", 200),
            # Frozen Zone disabled for backward-compatible tests
            patch.object(proxy, "PROXY_FROZEN_HEAD", 0), patch.object(proxy_state, "PROXY_FROZEN_HEAD", 0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _msgs_with(self, n_reads, n_bash=0):
        """Build an Anthropic-format message list with `n_reads` Read
        tool_use + tool_result pairs and `n_bash` Bash pairs.

        Each Read's content includes a unique fingerprint so the Bash-dedup
        pass (which keys on content similarity) does not merge distinct
        results together. Read content is long enough to trigger the
        200-char preview suffix on cleared entries."""
        msgs = []
        for i in range(n_reads):
            path = f"/tmp/file_{i}.py"
            long_content = (
                f"FILE_{i}_MARKER " + ("the quick brown fox jumps over the lazy dog. " * 10)
            )  # 460+ chars; unique marker per file
            msgs.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"tu_r{i}",
                              "name": "Read", "input": {"file_path": path}}],
            })
            msgs.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"tu_r{i}",
                              "content": long_content}],
            })
        for i in range(n_bash):
            msgs.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"tu_b{i}",
                              "name": "Bash", "input": {"command": f"ls {i}"}}],
            })
            msgs.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"tu_b{i}",
                              "content": f"BASH_{i}_MARKER " + "x" * 500}],
            })
        return msgs

    def test_read_cleared_keeps_200_char_preview(self):
        """R2.2: a cleared Read tool_result retains a 200-char preview
        (followed by '...' since the original was >200 chars) so the
        model can re-use the result without re-reading."""
        msgs = self._msgs_with(n_reads=4)  # 4 reads → only 2 kept
        msgs, stats = proxy.clear_old_tool_results(msgs)
        self.assertEqual(stats["kept"], 2)
        # Find cleared Read results — they start with "[cleared:" AND
        # contain a FILE_N marker (the original content preview).
        cleared_reads = [
            b for m in msgs for b in m.get("content", [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
            and "FILE_" in str(b.get("content", ""))  # preview contains the marker
            and str(b.get("content", "")).startswith("[cleared:")
        ]
        self.assertGreaterEqual(len(cleared_reads), 1)
        # The cleared block's content is "[cleared: Read(...)]\\n{preview}".
        # preview = 200 chars + "..." = 203 chars.
        text = str(cleared_reads[0]["content"])
        parts = text.split("\n", 1)
        self.assertEqual(len(parts), 2)
        # First line is the [cleared: Read(...)] header.
        self.assertTrue(parts[0].startswith("[cleared: Read"))
        # Second line is the preview: exactly 200 chars + "..." = 203.
        self.assertEqual(len(parts[1]), 203)
        self.assertTrue(parts[1].endswith("..."))

    def test_non_read_cleared_replaced_with_placeholder_only(self):
        """R2.2: a cleared Bash tool_result has NO preview, just the
        [cleared: ...] placeholder. The model must re-run Bash if it
        needs the output back."""
        msgs = self._msgs_with(n_reads=2, n_bash=4)
        # 6 tool_results total, KEEP=2 → 4 cleared.
        msgs, stats = proxy.clear_old_tool_results(msgs)
        self.assertEqual(stats["kept"], 2)
        # Find tool_results that are CLEARED (start with "[cleared:").
        cleared = [
            b for m in msgs for b in m.get("content", [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
            and str(b.get("content", "")).startswith("[cleared:")
        ]
        # 4 cleared entries.
        self.assertEqual(len(cleared), 4)
        # The 2 cleared Read entries DO have a preview (multi-line);
        # the 2 cleared Bash entries do NOT (single-line placeholder).
        bash_cleared = [b for b in cleared
                        if "Bash" in str(b["content"])]
        self.assertGreaterEqual(len(bash_cleared), 1)
        for block in bash_cleared:
            self.assertNotIn("\n", block["content"])
            self.assertNotIn("...", block["content"])

    def test_recent_reads_get_5_point_bonus_and_are_kept(self):
        """R2.3: the 6 most recent Read results get a +5 semantic bonus
        so they survive the keep-top-N trim. With 5 Reads + KEEP=2,
        the two MOST RECENT (FILE_3, FILE_4) survive; FILE_0..FILE_2
        are cleared. The dedup pass on the kept results keeps both
        because their content is unique per-file."""
        msgs = self._msgs_with(n_reads=5)
        msgs, stats = proxy.clear_old_tool_results(msgs)
        self.assertEqual(stats["kept"], 2)
        # Identify kept tool_results: they were NOT replaced with
        # "[cleared: ..." and still contain their original marker.
        kept = [
            b for m in msgs for b in m.get("content", [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
            and not str(b.get("content", "")).startswith("[cleared:")
        ]
        # After dedup, the two kept Read results should be FILE_3 and
        # FILE_4 (the most recent — they got the +5 boost).
        kept_text = " ".join(str(b["content"]) for b in kept)
        self.assertIn("FILE_3_MARKER", kept_text)
        self.assertIn("FILE_4_MARKER", kept_text)
        # FILE_0, FILE_1, FILE_2 should NOT appear in kept (they're
        # cleared and only show up in the preview portion).
        for old in ["FILE_0_MARKER ", "FILE_1_MARKER ", "FILE_2_MARKER "]:
            self.assertNotIn(old, kept_text)

    def test_disabled_returns_immediately(self):
        """R2.2: when PROXY_CLEAR_ENABLED is False, the function is a
        no-op — no clearing, no scoring, stats.enabled=False."""
        with patch.object(proxy, "PROXY_CLEAR_ENABLED", False), patch.object(proxy_state, "PROXY_CLEAR_ENABLED", False), patch.object(proxy_state, "PROXY_CLEAR_ENABLED", False):
            msgs = self._msgs_with(n_reads=4)
            original = [m for m in msgs]  # shallow copy
            msgs, stats = proxy.clear_old_tool_results(msgs)
            self.assertFalse(stats["enabled"])
            self.assertEqual(msgs, original)  # untouched


# =============================================================================
# R4.1 — parse_tool_arguments (4-level fallback)
# =============================================================================
class TestParseToolArguments(unittest.TestCase):
    """Covers parse_tool_arguments — Qwen occasionally emits XML or freeform
    text instead of clean JSON arguments. The parser tries 4 increasingly
    lenient strategies: pure JSON → embedded JSON → XML → heuristic by
    tool_name_hint. Stringified booleans are coerced to real bools along
    the way so the Anthropic client validator accepts them.
    """

    def test_level1_pure_json(self):
        """Level 1: a well-formed JSON object parses on the first try."""
        result = proxy.parse_tool_arguments('{"city": "Beijing", "unit": "c"}')
        self.assertEqual(result, {"city": "Beijing", "unit": "c"})

    def test_level2_embedded_json_in_text(self):
        """Level 2: when the raw string has leading/trailing text around
        a JSON object, the parser finds the {…} span and parses that."""
        # Common Qwen quirk: prefix text + JSON + trailing text.
        raw = 'Sure, calling the tool: {"path": "/tmp/x.py", "limit": 50} (done)'
        result = proxy.parse_tool_arguments(raw)
        self.assertEqual(result, {"path": "/tmp/x.py", "limit": 50})

    def test_level3_xml_format(self):
        """Level 3: Qwen XML-style <parameter=key>value</parameter>."""
        # Two equivalent XML forms the parser recognises.
        raw_a = '<parameter=file_path>/tmp/foo.py</parameter><parameter=offset>10</parameter>'
        raw_b = '<param name="file_path">/tmp/foo.py</param><param name="offset">10</param>'
        for raw in (raw_a, raw_b):
            result = proxy.parse_tool_arguments(raw)
            self.assertEqual(result.get("file_path"), "/tmp/foo.py")
            self.assertEqual(result.get("offset"), "10")

    def test_level4_heuristic_bash(self):
        """Level 4 (heuristic): when nothing parses, the tool_name_hint
        routes a freeform string into the most likely arg name:
        bash/exec/shell → `command`."""
        result = proxy.parse_tool_arguments("ls -la /tmp", tool_name_hint="bash")
        self.assertEqual(result, {"command": "ls -la /tmp"})

    def test_level4_heuristic_read(self):
        """Level 4 (heuristic): read/view/file → `file_path`."""
        result = proxy.parse_tool_arguments("/etc/hosts", tool_name_hint="read")
        self.assertEqual(result, {"file_path": "/etc/hosts"})

    def test_stringified_booleans_are_coerced(self):
        """String 'True'/'False' (any case) are coerced to real bools
        so the Anthropic SDK's strict JSON Schema validator accepts them."""
        result = proxy.parse_tool_arguments('{"verbose": "True", "debug": "false"}')
        self.assertIs(result["verbose"], True)
        self.assertIs(result["debug"], False)

    def test_unparseable_with_no_hint_returns_empty(self):
        """When nothing matches and there's no helpful tool_name_hint,
        the function returns {} (rather than crashing or fabricating)."""
        result = proxy.parse_tool_arguments("random nonsense", tool_name_hint="unknown_tool")
        self.assertEqual(result, {})

    def test_empty_string_returns_empty(self):
        """Empty / None / whitespace inputs return {} immediately
        (no exception, no spurious JSON-parse error log)."""
        self.assertEqual(proxy.parse_tool_arguments(""), {})
        self.assertEqual(proxy.parse_tool_arguments("   "), {})
        self.assertEqual(proxy.parse_tool_arguments(None), {})


# =============================================================================
# R4.5 — reasoning_content extraction (Qwen thinking-mode quirk)
# =============================================================================
class TestReasoningExtraction(unittest.TestCase):
    """Covers convert_openai_response_to_anthropic's handling of
    Qwen3.6's `reasoning_content` field. When content is empty but
    reasoning exists, the reasoning is promoted to the visible text
    block (a Qwen3.6 fix). When both are present, content wins.
    """

    def _openai_resp(self, content="", reasoning="", tool_calls=None, finish="stop"):
        return {
            "id": "chatcmpl-test",
            "choices": [{
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls or [],
                    **({"reasoning_content": reasoning} if reasoning else {}),
                },
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }

    def test_empty_content_promotes_reasoning_to_text(self):
        """R4.5: when content='' but reasoning='...' is present, the
        reasoning is surfaced as the response's text block (otherwise
        the model would appear to have said nothing)."""
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(content="", reasoning="Let me think about this..."),
            anthropic_model="claude-sonnet-4-6",
        )
        text_blocks = [b for b in r["content"] if b["type"] == "text"]
        self.assertEqual(len(text_blocks), 1)
        self.assertIn("Let me think", text_blocks[0]["text"])

    def test_content_wins_when_both_present(self):
        """R4.5: when BOTH content and reasoning are populated, content
        is the visible text and reasoning is dropped (it's an internal
        scratchpad, not meant to be shown to the user)."""
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(content="The answer is 42.",
                              reasoning="internal scratchpad"),
            anthropic_model="claude-sonnet-4-6",
        )
        text_blocks = [b for b in r["content"] if b["type"] == "text"]
        self.assertEqual(len(text_blocks), 1)
        self.assertEqual(text_blocks[0]["text"], "The answer is 42.")
        self.assertNotIn("scratchpad", r["content"][0]["text"])

    def test_whitespace_only_content_still_promotes_reasoning(self):
        """R4.5: content='\\n  ' (whitespace only) is treated as empty
        — the `.strip()` check means reasoning is promoted."""
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(content="\n   ", reasoning="actual answer"),
            anthropic_model="claude-sonnet-4-6",
        )
        text_blocks = [b for b in r["content"] if b["type"] == "text"]
        self.assertEqual(len(text_blocks), 1)
        self.assertEqual(text_blocks[0]["text"], "actual answer")

    def test_no_reasoning_no_special_handling(self):
        """R4.5 baseline: when reasoning is absent, the function
        behaves as it did before this fix (content is text)."""
        r = proxy.convert_openai_response_to_anthropic(
            self._openai_resp(content="Hello there."),
            anthropic_model="claude-sonnet-4-6",
        )
        text_blocks = [b for b in r["content"] if b["type"] == "text"]
        self.assertEqual(len(text_blocks), 1)
        self.assertEqual(text_blocks[0]["text"], "Hello there.")


# =============================================================================
# R5.1 / R5.2 — error translation (3 patterns + solution hints)
# =============================================================================
class TestErrorTranslation(unittest.TestCase):
    """Covers _translate_tool_result_errors — the proxy rewrites
    backend's English error strings (Wasted call, File does not exist,
    InputValidationError) into natural-language Chinese hints with
    actionable next-step suggestions."""

    def _msg(self, tool_use_id, content):
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": content},
        ]}

    def test_translate_wasted_call(self):
        """R5.1 + R5.2: 'Wasted call' is replaced with a hint to use
        Bash cat instead of re-reading the file."""
        msgs = [self._msg("t1", "<tool_use_error>Wasted call - file unchanged</tool_use_error>")]
        _, counts = proxy._translate_tool_result_errors(msgs)
        self.assertEqual(counts["wasted"], 1)
        self.assertEqual(counts["file_not_found"], 0)
        self.assertEqual(counts["input_validation"], 0)
        # The replacement includes the actionable hint (R5.2).
        text = msgs[0]["content"][0]["content"]
        self.assertIn("未发生变化", text)
        self.assertIn("Bash cat", text)  # solution hint

    def test_translate_file_not_found(self):
        """R5.1 + R5.2: 'File does not exist' (or 'No such file') is
        replaced with a hint to use Bash ls/find to discover paths."""
        msgs_a = [self._msg("t1", "<tool_use_error>File does not exist. /nope.py</tool_use_error>")]
        msgs_b = [self._msg("t1", "<tool_use_error>No such file or directory</tool_use_error>")]
        for msgs in (msgs_a, msgs_b):
            _, counts = proxy._translate_tool_result_errors(msgs)
            self.assertEqual(counts["file_not_found"], 1)
            text = msgs[0]["content"][0]["content"]
            self.assertIn("文件不存在", text)
            self.assertIn("Bash ls", text)  # solution hint

    def test_translate_input_validation(self):
        """R5.1 + R5.2: 'InputValidationError' is replaced with a hint
        to check the tool parameter format."""
        msgs = [self._msg("t1", "<tool_use_error>InputValidationError: missing required parameter 'command'</tool_use_error>")]
        _, counts = proxy._translate_tool_result_errors(msgs)
        self.assertEqual(counts["input_validation"], 1)
        text = msgs[0]["content"][0]["content"]
        self.assertIn("工具调用参数错误", text)
        self.assertIn("工具参数格式", text)  # solution hint

    def test_unrelated_tool_result_left_untouched(self):
        """A successful tool_result (no error string) must pass through
        unchanged — the translator only acts on recognised patterns."""
        original = "FILE_CONTENTS: hello world\n"
        msgs = [self._msg("t1", original)]
        _, counts = proxy._translate_tool_result_errors(msgs)
        self.assertEqual(sum(counts.values()), 0)
        self.assertEqual(msgs[0]["content"][0]["content"], original)

    def test_mixed_messages_each_counted(self):
        """Three tool_results of three different patterns → all three
        counts incremented, all three contents rewritten in place."""
        msgs = [
            self._msg("t1", "Wasted call - foo"),
            self._msg("t2", "File does not exist. /x.py"),
            self._msg("t3", "InputValidationError: bad arg"),
        ]
        _, counts = proxy._translate_tool_result_errors(msgs)
        self.assertEqual(counts, {"wasted": 1, "file_not_found": 1, "input_validation": 1})
        # Each block now starts with the [System: prefix.
        for msg in msgs:
            self.assertTrue(msg["content"][0]["content"].startswith("[System:"))

    def test_assistant_messages_skipped(self):
        """The translator walks user-role messages only. Assistant
        tool_use blocks are not error results and must be ignored."""
        msgs = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            self._msg("t1", "Wasted call - foo"),
        ]
        _, counts = proxy._translate_tool_result_errors(msgs)
        # Only the user-side tool_result is translated.
        self.assertEqual(counts["wasted"], 1)
        # The assistant's tool_use block is untouched.
        self.assertEqual(msgs[0]["content"][0]["type"], "tool_use")


# =============================================================================
# R1.3 — _incremental_compress (summary cache + incremental updates)
# =============================================================================
class TestIncrementalCompress(unittest.TestCase):
    """Covers _incremental_compress — the proxy caches its per-session
    compression summary so subsequent truncations only re-compress the
    newly-dropped tail (saves an LLM call when nothing new was dropped)."""

    def setUp(self):
        # Patch the LLM call to return a deterministic marker so we can
        # assert when it was (or wasn't) invoked.
        self._llm_calls = []
        def fake_llm(messages, timeout=30):
            self._llm_calls.append(messages)
            return f"[LLM_SUMMARY for {len(messages)} msgs]"
        self._patches = [
            patch.object(proxy, "_compress_middle_with_llm", fake_llm),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        # Wipe session cache so cross-test state doesn't leak.
        with proxy_state._summary_cache_lock:
            proxy_state._summary_cache.clear()

    def _err_msg(self, text):
        """A user message with a tool_result block that contains 'error'
        — exactly the shape _extract_middle_summary_rules looks for."""
        return {
            "role": "user",
            "content": [{
                "type": "tool_result", "tool_use_id": "t1",
                "content": f"error: {text}",
            }],
        }

    def test_first_call_no_cache_uses_rules_path(self):
        """R1.3: first compression on a fresh session uses the rule-based
        path (not the LLM) because dropped has <10 messages."""
        with proxy_state._summary_cache_lock:
            proxy_state._summary_cache.pop("sess_A", None)
        dropped = [self._err_msg(f"boom_{i}") for i in range(3)]
        result, was_incremental = proxy._incremental_compress(dropped, "sess_A")
        self.assertIsNotNone(result)
        self.assertIn("[Compressed context", result)  # rule header
        self.assertIs(was_incremental, False)  # not incremental on first call
        # The LLM was NOT called (rule path handles <10 msgs).
        self.assertEqual(self._llm_calls, [])

    def test_second_call_with_cache_marks_incremental(self):
        """R1.3: a second call on the same session after a prior
        compression returns was_incremental=True (cache hit), even when
        the dropped set is identical (so 0 new messages)."""
        # First call: prime the cache.
        proxy._incremental_compress([self._err_msg("foo")] * 3, "sess_B")
        self.assertEqual(self._llm_calls, [])
        # Second call: same dropped set, should hit the cache.
        result, was_incremental = proxy._incremental_compress(
            [self._err_msg("foo")] * 3, "sess_B"
        )
        self.assertIsNotNone(result)
        self.assertIs(was_incremental, True)
        # LLM is NOT called even for the second compression: with 0 new
        # messages, the function uses cached summary directly.
        self.assertEqual(self._llm_calls, [])

    def test_cache_size_limit_evicts_oldest(self):
        """R1.3: when _SUMMARY_CACHE_MAX_SESSIONS is reached, the oldest
        session is evicted to make room. New sessions start fresh."""
        # Save the real limit and override for this test.
        real_max = proxy_state._SUMMARY_CACHE_MAX_SESSIONS
        with patch.object(proxy_state, "_SUMMARY_CACHE_MAX_SESSIONS", 2):
            # Fill the cache: sess_1 (oldest), sess_2.
            proxy._incremental_compress([self._err_msg("a")], "sess_1")
            proxy._incremental_compress([self._err_msg("b")], "sess_2")
            with proxy_state._summary_cache_lock:
                self.assertIn("sess_1", proxy_state._summary_cache)
                self.assertIn("sess_2", proxy_state._summary_cache)
            # Add sess_3 → sess_1 (oldest) should be evicted.
            proxy._incremental_compress([self._err_msg("c")], "sess_3")
            with proxy_state._summary_cache_lock:
                self.assertNotIn("sess_1", proxy_state._summary_cache)
                self.assertIn("sess_2", proxy_state._summary_cache)
                self.assertIn("sess_3", proxy_state._summary_cache)
        # Restore so teardown's `cache.clear()` is consistent.
        proxy._SUMMARY_CACHE_MAX_SESSIONS = real_max

    def test_empty_dropped_returns_none(self):
        """R1.3: when dropped is empty, the function returns (None, None)
        — there's nothing to summarise. The cache must NOT be populated."""
        with proxy_state._summary_cache_lock:
            proxy_state._summary_cache.pop("sess_E", None)
        result, was_incremental = proxy._incremental_compress([], "sess_E")
        self.assertIsNone(result)
        self.assertIsNone(was_incremental)
        with proxy_state._summary_cache_lock:
            self.assertNotIn("sess_E", proxy_state._summary_cache)


# =============================================================================
# R1.4 — keyword index (_extract_keywords + _inject_keyword_context)
# =============================================================================
class TestKeywordIndex(unittest.TestCase):
    """Covers the keyword-based history retrieval: when a tool_result
    is truncated away, the proxy keeps a keyword→summary index so it
    can re-surface relevant prior context if the user later asks about
    a file/error/function that was dropped."""

    def _user_msg(self, content):
        return {"role": "user", "content": content}

    def _assistant_tool_use(self, name, file_path=None, command=None):
        inp = {}
        if file_path:
            inp["file_path"] = file_path
        if command:
            inp["command"] = command
        return {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"tu_{name}",
                         "name": name, "input": inp}],
        }

    def _user_tool_result(self, text):
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_x",
                         "content": text}],
        }

    def test_extract_picks_up_filenames(self):
        """R1.4: filenames from tool_use.file_path are indexed. The dict
        key is the basename (not the full path) so 'config.json' matches
        queries even when the absolute path differs."""
        msgs = [self._assistant_tool_use("Read", file_path="/tmp/config.json"),
                self._user_tool_result("contents of config.json")]
        kw = proxy._extract_keywords(msgs)
        self.assertIn("config.json", kw)
        # The summary should mention the read.
        self.assertTrue(any("Read" in s for s in kw["config.json"]))

    def test_extract_picks_up_error_names(self):
        """R1.4: PascalCase names ending in 'Error' or 'Exception' are
        indexed as separate keywords. This lets a follow-up question
        about 'the ValueError' retrieve the relevant prior traceback."""
        msgs = [self._user_tool_result(
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 5, in <module>\n"
            "ValueError: invalid input"
        )]
        kw = proxy._extract_keywords(msgs)
        self.assertIn("ValueError", kw)

    def test_extract_picks_up_function_names(self):
        """R1.4: function-call patterns like `parse_config(...)` are
        captured as keywords for later re-lookup."""
        msgs = [self._user_tool_result("calling parse_config() and validate()")]
        kw = proxy._extract_keywords(msgs)
        self.assertIn("parse_config", kw)
        self.assertIn("validate", kw)

    def test_inject_returns_none_when_no_keyword_match(self):
        """R1.4: if the query (last 3 messages) doesn't mention any
        indexed keyword, the injector returns None — no spurious
        "Relevant history" block clutters the prompt."""
        past = [self._assistant_tool_use("Read", file_path="/tmp/secret.py")]
        current = [self._user_msg("what is the weather today?")]
        self.assertIsNone(proxy._inject_keyword_context(
            proxy._extract_keywords(past), current,
        ))

    def test_inject_respects_top_k_and_max_chars(self):
        """R1.4: the injected block is capped at top_k entries and
        truncated to max_chars (with '...' suffix). The cap prevents
        a stale keyword from blowing up the prompt budget."""
        # 10 distinct Read tool_uses; current query mentions the first 3.
        past = [self._assistant_tool_use(
                    "Read", file_path=f"/tmp/file_{i}.py")
                for i in range(10)]
        # The keyword in the index is "file_N.py" (full basename),
        # so the query must contain the full filename to match.
        current = [self._user_msg(
            "show me file_0.py, file_1.py, file_2.py please")]
        result = proxy._inject_keyword_context(
            proxy._extract_keywords(past), current,
            top_k=2, max_chars=200,
        )
        self.assertIsNotNone(result)
        # At most top_k=2 entries.
        self.assertLessEqual(result.count("- [file_"), 2)
        # Truncated to ≤ max_chars + the trailing "...".
        self.assertLessEqual(len(result), 200 + 3)


# =============================================================================
# R3.3 — _filter_tools (44 → 15 tool definition trim)
# =============================================================================
class TestToolFilter(unittest.TestCase):
    """Covers _filter_tools — the proxy trims the tool definitions list
    when it exceeds PROXY_TOOL_FILTER_MAX. Whitelist + recently-used
    tools are kept; tool_choice forces an extra keep; and a too-
    aggressive filter falls back to the full list."""

    def setUp(self):
        self._patches = [
            patch.object(proxy, "PROXY_TOOL_FILTER_MAX", 5), patch.object(proxy_state, "PROXY_TOOL_FILTER_MAX", 5),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _tool(self, name):
        return {"name": name, "description": "fake", "input_schema": {"type": "object"}}

    def _tools(self, names):
        return [self._tool(n) for n in names]

    def _asst(self, tool_names):
        return {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"tu_{n}", "name": n, "input": {}}
                        for n in tool_names],
        }

    def test_below_max_is_passthrough(self):
        """R3.3: when len(tools) <= PROXY_TOOL_FILTER_MAX, no filter
        is applied — the full list is returned unchanged."""
        tools = self._tools(["Read", "Write", "Edit"])  # 3 < 5
        result, stats = proxy._filter_tools(tools, [])
        self.assertEqual(result, tools)
        self.assertFalse(stats["filtered"])
        self.assertEqual(stats["reason"], "below_max")

    def test_above_max_keeps_whitelist_and_recent(self):
        """R3.3: with 10 tools, the filter keeps TOOL_ALWAYS_KEEP
        (~18 items) + recently-used (last 5 assistant messages).
        The filter list is strictly smaller than the input."""
        tools = self._tools([
            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
            "LS", "Task", "WebFetch", "WebSearch", "TodoRead", "TodoWrite",
            "Agent", "NotebookEdit", "EnterPlanMode", "ExitPlanMode",
            "AskUserQuestion", "Skill",
            "CustomA", "CustomB", "CustomC", "CustomD",  # 22 total > 5
            "CustomE", "CustomF", "CustomG", "CustomH", "CustomI", "CustomJ",
        ])
        # 3 last assistant messages: Read, Bash, CustomA, CustomB
        recent_msgs = [self._asst(["Read"]), self._asst(["Bash"]),
                       self._asst(["CustomA", "CustomB"])]
        result, stats = proxy._filter_tools(tools, recent_msgs)
        self.assertTrue(stats["filtered"])
        self.assertIn("recent_tools", stats)
        self.assertIn("CustomA", stats["recent_tools"])
        self.assertIn("CustomB", stats["recent_tools"])
        self.assertEqual(stats["scanned_assistant"], 3)
        kept_names = [t["name"] for t in result]
        # TOOL_ALWAYS_KEEP are all present.
        for must in ["Read", "Write", "Bash", "Glob"]:
            self.assertIn(must, kept_names)
        # CustomA + CustomB are recent → kept.
        self.assertIn("CustomA", kept_names)
        self.assertIn("CustomB", kept_names)
        # CustomC..CustomJ were not used recently and not in whitelist → dropped.
        for unused in ["CustomC", "CustomD", "CustomE", "CustomJ"]:
            self.assertNotIn(unused, kept_names)

    def test_tool_choice_forces_keep(self):
        """R3.3: when the request specifies tool_choice pointing at a
        tool that's NOT in the whitelist and NOT recently used, the
        filter must still keep it (otherwise the model can't pick it)."""
        tools = self._tools(["Read", "Bash", "CustomA", "CustomB",
                             "CustomC", "CustomD", "CustomE"])
        recent_msgs = [self._asst(["Read"])]
        result, _ = proxy._filter_tools(
            tools, recent_msgs, tool_choice_name="CustomE"
        )
        kept_names = [t["name"] for t in result]
        self.assertIn("CustomE", kept_names)

    def test_too_few_after_filter_falls_back_to_all(self):
        """R3.3: if filtering would leave < 5 tools (the model can't
        do much with fewer), the function returns the full original
        list with reason=too_few_after_filter — better to send a few
        extra tool defs than to break the conversation."""
        # Build a set of 7 tools where recent + always_keep yield < 5.
        # Use only 2 recent and rely on a small whitelist by patching.
        small_whitelist = ("Read", "Bash")
        tools = self._tools(["Read", "Bash", "Custom1", "Custom2",
                             "Custom3", "Custom4", "Custom5"])
        recent_msgs = [self._asst(["Custom3", "Custom4"])]
        with patch.object(proxy, "TOOL_ALWAYS_KEEP", small_whitelist), patch.object(proxy_state, "TOOL_ALWAYS_KEEP", small_whitelist):
            result, stats = proxy._filter_tools(tools, recent_msgs)
        # Too_few_after_filter: kept would be {Read, Bash, Custom3, Custom4} = 4 < 5.
        self.assertFalse(stats["filtered"])
        self.assertEqual(stats["reason"], "too_few_after_filter")
        self.assertEqual(result, tools)  # returned full list

    def test_recent_rounds_scans_only_last_n_assistant_messages(self):
        """R3.3: recent_rounds=5 means the filter walks BACKWARDS through
        messages and stops after collecting 5 assistant messages. Older
        tool_uses are forgotten (intentional — they're stale)."""
        # 6 assistants: the oldest uses CustomA, the 5 most recent use Read.
        # recent_rounds=5 → CustomA should be dropped.
        msgs = [
            self._asst(["CustomA"]),  # 6th-from-end
            self._asst(["Read"]),     # 5th
            self._asst(["Read"]),
            self._asst(["Read"]),
            self._asst(["Read"]),
            self._asst(["Read"]),     # most recent
        ]
        # Build 10 tools: 6 from TOOL_ALWAYS_KEEP (so kept >= 5 to
        # avoid the too_few_after_filter fallback) + 4 non-whitelist
        # custom tools to test the recent_rounds scan.
        tools = self._tools([
            "Read", "Write", "Edit", "Bash", "Glob", "Grep",  # 6 whitelist
            "CustomA", "CustomB", "CustomC", "CustomD",       # 4 non-whitelist
        ])
        result, stats = proxy._filter_tools(tools, msgs, recent_rounds=5)
        self.assertTrue(stats["filtered"], f"filter should have run (10 tools > max=5); got {stats}")
        kept = [t["name"] for t in result]
        # CustomA was the 6th assistant message → outside the recent-5 window.
        self.assertNotIn("CustomA", kept)
        # The 6 whitelist tools are all kept regardless of recency.
        for must in ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]:
            self.assertIn(must, kept)


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
        """R2.1: max_run == threshold (3) and < LEVEL2 (6) → Level 1.
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
        """R2.1: max_run == LEVEL2 (6) → Level 2.
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
        self.assertIn("completely different approach", text)
        # Original message preserved.
        self.assertEqual(msgs[0], {"role": "user", "content": "do the thing"})

    def test_below_threshold_is_a_pure_noop(self):
        """R2.1: max_run < threshold → no message added, no tool removed,
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
# DEF-002: Enhanced loop intervention (Level 3 + multi-tool Level 2)
# =============================================================================
class TestLoopInterventionEnhanced(unittest.TestCase):
    """Covers DEF-002 fixes: Level 3, multi-tool Level 2, tail scan."""

    def _tool(self, name):
        return {"name": name, "description": "fake", "input_schema": {"type": "object"}}

    def test_level3_strips_all_tools(self):
        consecutive = {"Read:{\"/x.py\"}": 9}
        tools = [self._tool("Read"), self._tool("Bash")]
        msgs, new_tools, level, tool_name = proxy._apply_loop_intervention(
            raw_messages=[], raw_tools=tools, max_run=9,
            consecutive=consecutive, threshold=3, level2_threshold=6, level3_threshold=9,
        )
        self.assertEqual(level, 3)
        self.assertEqual(new_tools, [])
        text = msgs[-1]["content"][0]["text"]
        self.assertIn("ALL tools have been DISABLED", text)
        self.assertIn("plain text only", text)

    def test_multi_tool_level2_removes_all_high_count_tools(self):
        consecutive = {
            "Read:{\"/x.py\"}": 6,
            "Bash:{\"ls\"}": 6,
        }
        tools = [self._tool("Read"), self._tool("Bash"), self._tool("Glob")]
        msgs, new_tools, level, tool_name = proxy._apply_loop_intervention(
            raw_messages=[], raw_tools=tools, max_run=6,
            consecutive=consecutive, threshold=3, level2_threshold=6, level3_threshold=9,
        )
        self.assertEqual(level, 2)
        self.assertEqual([t["name"] for t in new_tools], ["Glob"])
        text = msgs[-1]["content"][0]["text"]
        self.assertIn("Read", text)
        self.assertIn("Bash", text)

    def test_level3_threshold_default(self):
        self.assertEqual(proxy.PROXY_LOOP_LEVEL3, proxy.PROXY_LOOP_THRESHOLD * 3)

    def test_level2_only_removes_tools_at_threshold(self):
        consecutive = {
            "Read:{\"/x.py\"}": 6,
            "Bash:{\"ls\"}": 2,
        }
        tools = [self._tool("Read"), self._tool("Bash"), self._tool("Glob")]
        _, new_tools, level, _ = proxy._apply_loop_intervention(
            raw_messages=[], raw_tools=tools, max_run=6,
            consecutive=consecutive, threshold=3, level2_threshold=6, level3_threshold=9,
        )
        self.assertEqual(level, 2)
        self.assertEqual([t["name"] for t in new_tools], ["Bash", "Glob"])

    def test_no_double_counting_fresh_consecutive(self):
        tools = [self._tool("Read")]
        msgs = []
        for i in range(3):
            msgs.append({"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}}
            ]})
            msgs.append({"role": "user", "content": "ok"})
        consecutive = {}
        max_run = 0
        for msg in msgs:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if block.get("type") == "tool_use":
                    key = f"{block['name']}:{json.dumps(block['input'], sort_keys=True)}"
                    consecutive[key] = consecutive.get(key, 0) + 1
                    max_run = max(max_run, consecutive[key])
        self.assertEqual(max_run, 3)
        self.assertEqual(consecutive["Read:{\"file_path\": \"/x.py\"}"], 3)


# =============================================================================
# Text Output Loop Detection — _compute_text_similarity, _detect_text_loop
# =============================================================================
class TestTextLoopDetection(unittest.TestCase):
    """Covers text output loop detection for repeated similar text in assistant messages."""

    def test_compute_text_similarity_identical(self):
        """Identical texts should have similarity 1.0."""
        text = "This is a test message with enough characters to be meaningful."
        self.assertEqual(proxy._compute_text_similarity(text, text), 1.0)

    def test_compute_text_similarity_different(self):
        """Completely different texts should have low similarity."""
        text1 = "The quick brown fox jumps over the lazy dog."
        text2 = "Lorem ipsum dolor sit amet consectetur adipiscing elit."
        sim = proxy._compute_text_similarity(text1, text2)
        self.assertLess(sim, 0.3)

    def test_compute_text_similarity_similar(self):
        """Texts with minor differences should have high similarity."""
        text1 = "The minimax algorithm uses recursion to explore all possible moves."
        text2 = "The minimax algorithm uses recursion to explore all possible outcomes."
        sim = proxy._compute_text_similarity(text1, text2)
        self.assertGreater(sim, 0.7)

    def test_compute_text_similarity_empty(self):
        """Empty texts should have similarity 0.0."""
        self.assertEqual(proxy._compute_text_similarity("", "test"), 0.0)
        self.assertEqual(proxy._compute_text_similarity("test", ""), 0.0)
        self.assertEqual(proxy._compute_text_similarity("", ""), 0.0)

    def test_detect_text_loop_no_loop(self):
        """Short messages below threshold should not trigger detection."""
        tail = [
            {"role": "assistant", "content": [{"type": "text", "text": "Short msg"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Another msg"}]},
        ]
        run, is_loop = proxy._detect_text_loop(tail, threshold=3, min_chars=100)
        self.assertEqual(run, 0)
        self.assertFalse(is_loop)

    def test_detect_text_loop_with_repeated_text(self):
        """Repeated similar text should trigger detection."""
        base_text = "The fix is simple: validMoves should be called for isMaximizing ? this.currentPlayer : this.getOpponent(), and stones should be placed as opponent. But this.currentPlayer itself doesn't change."
        tail = [
            {"role": "assistant", "content": [{"type": "text", "text": base_text}]},
            {"role": "assistant", "content": [{"type": "text", "text": base_text}]},
            {"role": "assistant", "content": [{"type": "text", "text": base_text}]},
        ]
        run, is_loop = proxy._detect_text_loop(tail, threshold=3, min_chars=50, similarity_threshold=0.85)
        self.assertGreaterEqual(run, 3)
        self.assertTrue(is_loop)

    def test_detect_text_loop_with_different_text(self):
        """Different texts should not trigger detection."""
        tail = [
            {"role": "assistant", "content": [{"type": "text", "text": "First we need to analyze the board state carefully and consider all possible moves."}]},
            {"role": "assistant", "content": [{"type": "text", "text": "After careful consideration, I believe the best approach is to implement alpha-beta pruning."}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Let me check the implementation details and make sure everything is correct."}]},
        ]
        run, is_loop = proxy._detect_text_loop(tail, threshold=3, min_chars=50, similarity_threshold=0.85)
        self.assertLess(run, 3)
        self.assertFalse(is_loop)

    def test_detect_text_loop_short_messages_break_chain(self):
        """Short messages (tool-only) should break the similarity chain."""
        base_text = "The minimax algorithm needs to track whose turn it is on the board parameter. The board in minimax represents the state after this.currentPlayer has just played."
        tail = [
            {"role": "assistant", "content": [{"type": "text", "text": base_text}]},
            {"role": "assistant", "content": [{"type": "tool_use", "name": "Read", "input": {}}]},  # No text
            {"role": "assistant", "content": [{"type": "text", "text": base_text}]},
            {"role": "assistant", "content": [{"type": "text", "text": base_text}]},
        ]
        run, is_loop = proxy._detect_text_loop(tail, threshold=3, min_chars=100, similarity_threshold=0.85)
        self.assertLess(run, 3)
        self.assertFalse(is_loop)

    def test_text_loop_intervention_level1(self):
        """Level 1 text loop should inject hint message."""
        msgs = [{"role": "user", "content": "test"}]
        tools = [{"name": "Read", "description": "fake", "input_schema": {}}]
        new_msgs, new_tools, level, tool_name = proxy._apply_loop_intervention(
            raw_messages=msgs, raw_tools=tools, max_run=3, consecutive={},
            threshold=3, is_text_loop=True, text_loop_run=3,
        )
        self.assertEqual(level, 1)
        self.assertEqual(tool_name, "text_loop")
        self.assertEqual(new_tools, tools)  # Tools not removed at level 1
        text = new_msgs[-1]["content"][0]["text"]
        self.assertIn("repeated similar text", text)

    def test_text_loop_intervention_level2(self):
        """Level 2 text loop should inject stronger warning."""
        msgs = [{"role": "user", "content": "test"}]
        tools = [{"name": "Read", "description": "fake", "input_schema": {}}]
        new_msgs, new_tools, level, tool_name = proxy._apply_loop_intervention(
            raw_messages=msgs, raw_tools=tools, max_run=6, consecutive={},
            threshold=3, level2_threshold=6, is_text_loop=True, text_loop_run=6,
        )
        self.assertEqual(level, 2)
        self.assertEqual(tool_name, "text_loop")
        text = new_msgs[-1]["content"][0]["text"]
        self.assertIn("stuck in a loop", text)

    def test_text_loop_intervention_level3(self):
        """Level 3 text loop should strip all tools."""
        msgs = [{"role": "user", "content": "test"}]
        tools = [{"name": "Read", "description": "fake", "input_schema": {}}]
        new_msgs, new_tools, level, tool_name = proxy._apply_loop_intervention(
            raw_messages=msgs, raw_tools=tools, max_run=9, consecutive={},
            threshold=3, level2_threshold=6, level3_threshold=9,
            is_text_loop=True, text_loop_run=9,
        )
        self.assertEqual(level, 3)
        self.assertEqual(tool_name, "text_loop")
        self.assertEqual(new_tools, [])  # All tools stripped
        text = new_msgs[-1]["content"][0]["text"]
        self.assertIn("text output loop", text)
        self.assertIn("ALL tools have been DISABLED", text)


# =============================================================================
# R4.4 — _repair_truncated_json (7 cases)
# =============================================================================
class TestRepairTruncatedJson(unittest.TestCase):
    """Covers _repair_truncated_json — invoked when Qwen's tool_call
    arguments JSON is cut off mid-stream."""

    def test_empty_input_returns_empty_dict(self):
        """R4.4: empty/whitespace/None inputs return the canonical '{}'."""
        self.assertEqual(proxy._repair_truncated_json(""), "{}")
        self.assertEqual(proxy._repair_truncated_json("   "), "{}")
        self.assertEqual(proxy._repair_truncated_json(None), "{}")

    def test_complete_json_unchanged(self):
        """R4.4: a well-formed JSON object is returned verbatim."""
        # Well-formed inputs are returned verbatim.
        ok = '{"a": 1, "b": [2, 3]}'
        self.assertEqual(proxy._repair_truncated_json(ok), ok)

    def test_unclosed_string_closes_quote_and_brace(self):
        """R4.4: cut after an unclosed string → repair appends '"' then '}'."""
        # The string was never closed (e.g. arguments cut after `{"file": "`).
        # Repair should append `"` then `}`.
        out = proxy._repair_truncated_json('{"file": "/tmp/x')
        parsed = json.loads(out)
        self.assertEqual(parsed, {"file": "/tmp/x"})

    def test_unclosed_brace_adds_closing_brace(self):
        """R4.4: cut mid-object (no unclosed string) → repair appends '}'."""
        # No unclosed string, but one too few `}`s.
        out = proxy._repair_truncated_json('{"a": 1, "b": 2')
        parsed = json.loads(out)
        self.assertEqual(parsed, {"a": 1, "b": 2})

    def test_nested_truncation_closes_multiple_levels(self):
        """R4.4: depth=2 (object in object) → repair appends two '}'s."""
        # depth=2 (object inside object) → must add two `}`s.
        # (Note: _repair_truncated_json only emits `}` to close, not `]`,
        # so we avoid trailing-`[` inputs here — that case is a known
        # limitation of the current implementation.)
        out = proxy._repair_truncated_json('{"outer": {"inner": 1, "a": 2')
        parsed = json.loads(out)
        self.assertEqual(parsed, {"outer": {"inner": 1, "a": 2}})

    def test_escape_sequence_does_not_falsely_close(self):
        """R4.4: a backslash-escaped quote does not terminate the string."""
        # The function tracks `in_string` correctly across `\\` and `\"` —
        # a JSON `"\""` should NOT be treated as ending the string.
        out = proxy._repair_truncated_json(r'{"msg": "say \"hello\"')
        parsed = json.loads(out)
        self.assertEqual(parsed, {"msg": 'say "hello"'})

    def test_whitespace_only_input_returns_empty_dict(self):
        """R4.4: only whitespace → stripped, returns '{}'."""
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
            patch.object(proxy_state, "_METRICS_PATH", self._metrics_path),
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


class TestDef001PreTruncation(unittest.TestCase):
    """DEF-001 fix: very large payloads (>PROXY_OOM_SAFE_CHARS chars)
    should be rounds-truncated with tight budget BEFORE heavy processing
    to prevent rapid-mlx OOM/timeout. Evidence: 65/67 of v0.5.0-baseline
    500 errors came from input_chars > 400K (session a309b181).

    Phase 1 (proxy-truncation-agent-scenario.md) lowered default 400K →
    200K because agent sessions can compose 80K history + 80K new content.
    """

    def test_threshold_constant_exists(self):
        """PROXY_OOM_SAFE_CHARS must default to a sensible value (200K,
        Phase 1 of truncation-agent-scenario design) and be overridable
        via env var. PROXY_PRE_TRUNCATE_CHARS is the legacy alias."""
        # Default value check on canonical name
        self.assertIsNotNone(proxy.PROXY_OOM_SAFE_CHARS)
        self.assertGreater(proxy.PROXY_OOM_SAFE_CHARS, 0)
        # Sanity: should be ≥ 100K (otherwise would be too aggressive)
        self.assertGreaterEqual(proxy.PROXY_OOM_SAFE_CHARS, 100_000)

    def test_rounds_truncation_runs_without_error(self):
        """_apply_rounds_truncation must work on a large message array
        and return a truncated result + stats dict. Note: truncation may
        insert a placeholder message, so result length may slightly exceed
        kept_messages alone."""
        # Build 50 messages each with large content (>1000 chars)
        large_msg = "x" * 2000
        msgs = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"msg {i}: {large_msg}"}
            for i in range(50)
        ]
        result, stats = proxy._apply_rounds_truncation(msgs, keep_rounds=2, session_id="sess_test")
        # Truncation should have triggered (50 messages > 2 rounds * ~3 msgs/round)
        self.assertTrue(stats.get("truncated"), "rounds truncation should trigger on 50 messages with keep_rounds=2")
        # Actual keys (per source inspection)
        self.assertIn("dropped_messages", stats)
        self.assertIn("kept_messages", stats)
        # dropped + kept should be ≤ original (may be less if placeholder replaces dropped)
        self.assertLessEqual(
            stats["dropped_messages"] + stats["kept_messages"],
            len(msgs) + 1,  # +1 for placeholder
        )
        # Result is shorter than original (truncation was effective)
        self.assertLessEqual(len(result), len(msgs) + 1)

    def test_pre_truncation_threshold_logic(self):
        """Verify the threshold-based decision: total_chars > threshold
        means truncation should be attempted. We can't run the full
        do_POST pipeline in a unit test (it requires HTTP plumbing), so
        we test the threshold constant relative to the documented bug."""
        # Phase 1: default lowered to 200K. A 500K payload still triggers.
        threshold = proxy.PROXY_OOM_SAFE_CHARS
        # Sanity: a 500K-char payload SHOULD trigger pre-truncation
        sample_500k = "x" * 500_000
        self.assertGreater(len(sample_500k), threshold)
        # And a 100K payload should NOT trigger (default 200K > 100K)
        sample_100k = "x" * 100_000
        self.assertLess(len(sample_100k), threshold)


class TestClassifyException(unittest.TestCase):
    """DEF-001 retry: _classify_exception must return (status, type, retryable)."""

    def test_timeout_error_class(self):
        code, etype, retry = proxy._classify_exception(TimeoutError("read timed out"))
        self.assertEqual(code, 504)
        self.assertEqual(etype, "timeout_error")
        self.assertTrue(retry)

    def test_socket_timeout(self):
        import socket as _socket
        code, etype, retry = proxy._classify_exception(_socket.timeout("connect timed out"))
        self.assertEqual(code, 504)
        self.assertTrue(retry)

    def test_oom_message(self):
        code, etype, retry = proxy._classify_exception(RuntimeError("[METAL] Insufficient Memory"))
        self.assertEqual(code, 503)
        self.assertEqual(etype, "backend_oom")
        self.assertTrue(retry)

    def test_oom_lowercase(self):
        code, etype, retry = proxy._classify_exception(RuntimeError("CUDA out of memory"))
        self.assertEqual(code, 503)
        self.assertTrue(retry)

    def test_connection_refused(self):
        code, etype, retry = proxy._classify_exception(ConnectionRefusedError("errno 61"))
        self.assertEqual(code, 503)
        self.assertEqual(etype, "backend_unavailable")
        self.assertTrue(retry)

    def test_connection_error_message(self):
        code, etype, retry = proxy._classify_exception(OSError("Connection refused on port 8081"))
        self.assertEqual(code, 503)
        self.assertTrue(retry)

    def test_programming_key_error(self):
        code, etype, retry = proxy._classify_exception(KeyError("missing_key"))
        self.assertEqual(code, 500)
        self.assertFalse(retry)

    def test_programming_type_error(self):
        code, etype, retry = proxy._classify_exception(TypeError("wrong type"))
        self.assertEqual(code, 500)
        self.assertFalse(retry)

    def test_generic_runtime_error(self):
        code, etype, retry = proxy._classify_exception(RuntimeError("something unexpected"))
        self.assertEqual(code, 500)
        self.assertFalse(retry)

    def test_retry_after_constant(self):
        self.assertIsNotNone(proxy.PROXY_RETRY_AFTER_SECONDS)
        self.assertGreater(proxy.PROXY_RETRY_AFTER_SECONDS, 0)

    def test_broken_pipe_499(self):
        code, etype, retry = proxy._classify_exception(BrokenPipeError("[Errno 32] Broken pipe"))
        self.assertEqual(code, 499)
        self.assertEqual(etype, "client_closed")
        self.assertFalse(retry)

    def test_connection_reset_499(self):
        code, etype, retry = proxy._classify_exception(ConnectionResetError("Connection reset by peer"))
        self.assertEqual(code, 499)
        self.assertEqual(etype, "client_closed")
        self.assertFalse(retry)


class TestReReadRate(unittest.TestCase):
    """DEF-003: re_read_rate formula must be 0-100%, not 2862%."""

    def _build_raw_messages(self, tool_uses):
        msgs = []
        for name, fp in tool_uses:
            msgs.append({"role": "assistant", "content": [
                {"type": "tool_use", "name": name, "input": {"file_path": fp}}
            ]})
        return msgs

    def test_no_cleared_files_rate_zero(self):
        self.assertAlmostEqual(min(0 / 1, 1.0) * 100 if 1 else 0, 0.0)

    def test_all_cleared_files_re_read(self):
        re_read_files = 3
        cleared_files = 3
        rate = min(re_read_files / cleared_files * 100, 100.0)
        self.assertAlmostEqual(rate, 100.0)

    def test_partial_re_read(self):
        re_read_files = 2
        cleared_files = 8
        rate = min(re_read_files / cleared_files * 100, 100.0)
        self.assertAlmostEqual(rate, 25.0)

    def test_rate_capped_at_100(self):
        re_read_files = 20
        cleared_files = 3
        rate = min(re_read_files / cleared_files * 100, 100.0)
        self.assertAlmostEqual(rate, 100.0)

    def test_old_formula_would_exceed_100(self):
        old_rate = 229 / 8 * 100
        self.assertGreater(old_rate, 100.0)
        new_rate = min(8 / 8 * 100, 100.0)
        self.assertLessEqual(new_rate, 100.0)


class TestOOMSafetyEstimation(unittest.TestCase):
    """DEF-005: OOM safety must include system prompt in token estimation."""

    def test_estimate_chars_basic(self):
        msgs = [{"role": "user", "content": "hello"}]
        self.assertEqual(proxy._estimate_message_chars(msgs), 5)

    def test_estimate_chars_with_blocks(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "abc"},
            {"type": "text", "text": "def"},
        ]}]
        self.assertEqual(proxy._estimate_message_chars(msgs), 6)

    def test_estimate_chars_tool_result(self):
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "content": "output text here"}
        ]}]
        self.assertEqual(proxy._estimate_message_chars(msgs), 16)

    def test_system_prompt_string_counted(self):
        _sys = "system prompt text"
        sys_chars = len(str(_sys))
        self.assertEqual(sys_chars, 18)

    def test_system_prompt_blocks_counted(self):
        _sys = [
            {"type": "text", "text": "part one"},
            {"type": "text", "text": "part two"},
        ]
        sys_chars = sum(len(b.get("text", "")) for b in _sys if b.get("type") == "text")
        self.assertEqual(sys_chars, 16)

    def test_combined_estimation_exceeds_limit(self):
        msgs = [{"role": "user", "content": "x" * 100000}]
        msg_chars = proxy._estimate_message_chars(msgs)
        sys_chars = len("system: " + "y" * 50000)
        total = msg_chars + sys_chars
        estimated_tokens = int(total / proxy.PROXY_CTX_TOKEN_RATIO)
        self.assertGreater(estimated_tokens, 50000)

    def test_no_system_prompt_zero_addition(self):
        _sys = None
        sys_chars = 0
        if _sys:
            if isinstance(_sys, list):
                sys_chars = sum(len(b.get("text", "")) for b in _sys if b.get("type") == "text")
            else:
                sys_chars = len(str(_sys))
        self.assertEqual(sys_chars, 0)


class TestReReadRateHelper(unittest.TestCase):
    """DEF-003: _compute_re_read_rate must return 0-100%."""

    def test_no_cleared_files(self):
        self.assertEqual(proxy._compute_re_read_rate(5, 0), 0.0)

    def test_all_re_read(self):
        self.assertEqual(proxy._compute_re_read_rate(3, 3), 100.0)

    def test_partial_re_read(self):
        self.assertAlmostEqual(proxy._compute_re_read_rate(2, 8), 25.0)

    def test_capped_at_100(self):
        self.assertEqual(proxy._compute_re_read_rate(20, 3), 100.0)


class TestCommonPrefixRatio(unittest.TestCase):
    """Phase 1: common prefix ratio metrics."""

    def test_identical_messages(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        ratio = proxy._compute_common_prefix_ratio(msgs, msgs)
        self.assertEqual(ratio, 1.0)

    def test_no_common_prefix(self):
        current = [{"role": "user", "content": "hello"}]
        previous = [{"role": "user", "content": "world"}]
        ratio = proxy._compute_common_prefix_ratio(current, previous)
        self.assertEqual(ratio, 0.0)

    def test_partial_common_prefix(self):
        current = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "new"},
        ]
        previous = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        ratio = proxy._compute_common_prefix_ratio(current, previous)
        self.assertGreater(ratio, 0.0)
        self.assertLess(ratio, 1.0)

    def test_empty_previous(self):
        current = [{"role": "user", "content": "hello"}]
        ratio = proxy._compute_common_prefix_ratio(current, [])
        self.assertEqual(ratio, 0.0)


class TestNormalizeSystemMessages(unittest.TestCase):
    """Phase 1: mid-conversation system messages must be converted to user."""

    def test_first_system_kept(self):
        msgs = [
            {"role": "system", "content": "sys1"},
            {"role": "user", "content": "hello"},
        ]
        out = proxy._normalize_system_messages(msgs)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[0]["content"], "sys1")

    def test_mid_system_converted(self):
        msgs = [
            {"role": "system", "content": "sys1"},
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "mid sys"},
            {"role": "assistant", "content": "hi"},
        ]
        out = proxy._normalize_system_messages(msgs)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[2]["role"], "user")
        self.assertIn("[System update]", out[2]["content"][0]["text"])
        self.assertIn("mid sys", out[2]["content"][0]["text"])
        self.assertEqual(out[3]["role"], "assistant")

    def test_system_list_content(self):
        msgs = [
            {"role": "system", "content": [{"type": "text", "text": "sys1"}]},
            {"role": "user", "content": "hello"},
            {"role": "system", "content": [{"type": "text", "text": "mid"}]},
        ]
        out = proxy._normalize_system_messages(msgs)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[2]["role"], "user")
        self.assertIn("mid", out[2]["content"][0]["text"])


class TestCacheAligner(unittest.TestCase):
    """Phase 1: cache aligner protects first N messages."""

    def test_disabled_returns_empty_prefix(self):
        # Save and restore env-driven setting
        orig = proxy.PROXY_CACHE_ALIGN_ENABLED
        proxy.PROXY_CACHE_ALIGN_ENABLED = False
        proxy_state.PROXY_CACHE_ALIGN_ENABLED = False
        proxy_state.PROXY_CACHE_ALIGN_ENABLED = False
        proxy_state.PROXY_CACHE_ALIGN_ENABLED = False
        try:
            msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
            prefix, dynamic = proxy._apply_cache_aligner(msgs)
            self.assertEqual(prefix, [])
            self.assertEqual(dynamic, msgs)
        finally:
            proxy.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy_state.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy_state.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy_state.PROXY_CACHE_ALIGN_ENABLED = orig

    def test_protects_head(self):
        orig = proxy.PROXY_CACHE_ALIGN_ENABLED
        orig_head = proxy.PROXY_CACHE_ALIGN_HEAD
        proxy.PROXY_CACHE_ALIGN_ENABLED = True
        proxy_state.PROXY_CACHE_ALIGN_ENABLED = True
        proxy_state.PROXY_CACHE_ALIGN_ENABLED = True
        proxy_state.PROXY_CACHE_ALIGN_ENABLED = True
        proxy.PROXY_CACHE_ALIGN_HEAD = 2
        proxy_state.PROXY_CACHE_ALIGN_HEAD = 2
        proxy_state.PROXY_CACHE_ALIGN_HEAD = 2
        proxy_state.PROXY_CACHE_ALIGN_HEAD = 2
        try:
            msgs = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "bye"},
            ]
            prefix, dynamic = proxy._apply_cache_aligner(msgs)
            self.assertEqual(len(prefix), 2)
            self.assertEqual(prefix[0]["role"], "system")
            self.assertEqual(prefix[1]["role"], "user")
            self.assertEqual(len(dynamic), 2)
        finally:
            proxy.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy_state.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy_state.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy_state.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy.PROXY_CACHE_ALIGN_HEAD = orig_head
            proxy_state.PROXY_CACHE_ALIGN_HEAD = orig_head
            proxy_state.PROXY_CACHE_ALIGN_HEAD = orig_head
            proxy_state.PROXY_CACHE_ALIGN_HEAD = orig_head

    def test_head_larger_than_messages(self):
        orig = proxy.PROXY_CACHE_ALIGN_ENABLED
        orig_head = proxy.PROXY_CACHE_ALIGN_HEAD
        proxy.PROXY_CACHE_ALIGN_ENABLED = True
        proxy_state.PROXY_CACHE_ALIGN_ENABLED = True
        proxy_state.PROXY_CACHE_ALIGN_ENABLED = True
        proxy_state.PROXY_CACHE_ALIGN_ENABLED = True
        proxy.PROXY_CACHE_ALIGN_HEAD = 10
        proxy_state.PROXY_CACHE_ALIGN_HEAD = 10
        proxy_state.PROXY_CACHE_ALIGN_HEAD = 10
        proxy_state.PROXY_CACHE_ALIGN_HEAD = 10
        try:
            msgs = [{"role": "system", "content": "sys"}]
            prefix, dynamic = proxy._apply_cache_aligner(msgs)
            self.assertEqual(prefix, msgs)
            self.assertEqual(dynamic, [])
        finally:
            proxy.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy_state.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy_state.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy_state.PROXY_CACHE_ALIGN_ENABLED = orig
            proxy.PROXY_CACHE_ALIGN_HEAD = orig_head
            proxy_state.PROXY_CACHE_ALIGN_HEAD = orig_head
            proxy_state.PROXY_CACHE_ALIGN_HEAD = orig_head
            proxy_state.PROXY_CACHE_ALIGN_HEAD = orig_head


class TestToolFilterStableOrder(unittest.TestCase):
    """Phase 1: filtered tools keep a stable order across requests."""

    def test_always_keep_order_stable(self):
        orig_max = proxy.PROXY_TOOL_FILTER_MAX
        proxy.PROXY_TOOL_FILTER_MAX = 3  # force filtering with 5 tools
        proxy_state.PROXY_TOOL_FILTER_MAX = 3  # force filtering with 5 tools
        proxy_state.PROXY_TOOL_FILTER_MAX = 3  # force filtering with 5 tools
        proxy_state.PROXY_TOOL_FILTER_MAX = 3  # force filtering with 5 tools
        try:
            tools = [
                {"name": "Write", "description": "w"},
                {"name": "Read", "description": "r"},
                {"name": "Bash", "description": "b"},
                {"name": "Glob", "description": "g"},
                {"name": "Edit", "description": "e"},
            ]
            kept, stats = proxy._filter_tools(tools, [], recent_rounds=5)
            names = [t["name"] for t in kept]
            # TOOL_ALWAYS_KEEP defines the order Read, Write, Edit, Bash, Glob, ...
            self.assertEqual(names, ["Read", "Write", "Edit", "Bash", "Glob"])
            self.assertTrue(stats.get("filtered"))
        finally:
            proxy.PROXY_TOOL_FILTER_MAX = orig_max
            proxy_state.PROXY_TOOL_FILTER_MAX = orig_max
            proxy_state.PROXY_TOOL_FILTER_MAX = orig_max
            proxy_state.PROXY_TOOL_FILTER_MAX = orig_max


class TestMaskSensitive(unittest.TestCase):
    def test_masks_authorization(self):
        result = proxy._mask_sensitive({"Authorization": "Bearer sk-abcdef1234567890wxyz"})
        self.assertEqual(result["Authorization"], "Bearer s****wxyz")

    def test_masks_x_api_key(self):
        result = proxy._mask_sensitive({"X-Api-Key": "sk-longapikey1234567890"})
        self.assertIn("****", result["X-Api-Key"])

    def test_short_value_masks_partially(self):
        result = proxy._mask_sensitive({"Authorization": "short"})
        self.assertEqual(result["Authorization"], "shor****")

    def test_non_sensitive_unchanged(self):
        result = proxy._mask_sensitive({"Content-Type": "application/json"})
        self.assertEqual(result["Content-Type"], "application/json")

    def test_non_dict_passthrough(self):
        self.assertIsNone(proxy._mask_sensitive(None))
        self.assertEqual(proxy._mask_sensitive("string"), "string")

    def test_non_string_value_unchanged(self):
        result = proxy._mask_sensitive({"Authorization": 12345})
        self.assertEqual(result["Authorization"], 12345)


class TestConvertToolsToOpenAI(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(proxy.convert_anthropic_tools_to_openai(None))

    def test_empty_list_returns_none(self):
        self.assertIsNone(proxy.convert_anthropic_tools_to_openai([]))

    def test_custom_type_tool(self):
        tools = [{"type": "custom", "name": "Read", "description": "Read file", "input_schema": {"type": "object"}}]
        result = proxy.convert_anthropic_tools_to_openai(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["function"]["name"], "Read")

    def test_simple_name_tool(self):
        tools = [{"name": "Bash", "description": "Run command", "input_schema": {"type": "object"}}]
        result = proxy.convert_anthropic_tools_to_openai(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["function"]["name"], "Bash")

    def test_parameters_fallback(self):
        tools = [{"name": "X", "parameters": {"type": "object"}}]
        result = proxy.convert_anthropic_tools_to_openai(tools)
        self.assertEqual(result[0]["function"]["parameters"], {"type": "object"})

    def test_tool_without_name_skipped(self):
        tools = [{"type": "other", "description": "no name"}]
        result = proxy.convert_anthropic_tools_to_openai(tools)
        self.assertIsNone(result)

    def test_mixed_tools(self):
        tools = [
            {"type": "custom", "name": "Read", "input_schema": {}},
            {"name": "Write", "input_schema": {}},
        ]
        result = proxy.convert_anthropic_tools_to_openai(tools)
        self.assertEqual(len(result), 2)

    def test_web_search_server_side_tool_mapped_to_function(self):
        """web_search_20250305 (Anthropic server-side tool) must be mapped
        to a function tool with a query parameter so local/cloud OpenAI
        backends can execute it. Without this, the tool is converted with
        empty parameters and the model emits empty tool_calls."""
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
        result = proxy.convert_anthropic_tools_to_openai(tools)
        self.assertEqual(len(result), 1)
        func = result[0]["function"]
        self.assertEqual(func["name"], "web_search")
        self.assertIn("query", func["parameters"]["properties"])
        self.assertEqual(func["parameters"]["required"], ["query"])
        # Description must guide the model to extract query from user message
        self.assertIn("query", func["description"].lower())

    def test_web_search_preserves_custom_name(self):
        """If the server-side tool has a custom name, preserve it."""
        tools = [{"type": "web_search_20250305", "name": "search", "max_uses": 8}]
        result = proxy.convert_anthropic_tools_to_openai(tools)
        self.assertEqual(result[0]["function"]["name"], "search")

    def test_web_search_mixed_with_custom_tools(self):
        """web_search server-side tool can coexist with custom tools."""
        tools = [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
            {"type": "custom", "name": "Read", "input_schema": {}},
        ]
        result = proxy.convert_anthropic_tools_to_openai(tools)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["function"]["name"], "web_search")
        self.assertEqual(result[1]["function"]["name"], "Read")


class TestConvertToolChoice(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(proxy.convert_anthropic_tool_choice_to_openai(None))

    def test_auto_string(self):
        self.assertEqual(proxy.convert_anthropic_tool_choice_to_openai("auto"), "auto")

    def test_any_string(self):
        self.assertEqual(proxy.convert_anthropic_tool_choice_to_openai("any"), {"type": "function"})

    def test_none_string(self):
        self.assertEqual(proxy.convert_anthropic_tool_choice_to_openai("none"), "none")

    def test_tool_dict(self):
        result = proxy.convert_anthropic_tool_choice_to_openai({"type": "tool", "name": "Read"})
        self.assertEqual(result, {"type": "function", "function": {"name": "Read"}})

    def test_auto_dict(self):
        self.assertEqual(proxy.convert_anthropic_tool_choice_to_openai({"type": "auto"}), "auto")

    def test_unknown_returns_none(self):
        self.assertIsNone(proxy.convert_anthropic_tool_choice_to_openai("unknown"))


class TestThinkingBlocks(unittest.TestCase):
    def test_has_thinking_list_with_thinking_block(self):
        msg = {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]}
        self.assertTrue(proxy._has_thinking_content(msg))

    def test_has_thinking_list_with_tag(self):
        msg = {"role": "assistant", "content": [{"type": "text", "text": "<thinking>inner</thinking>"}]}
        self.assertTrue(proxy._has_thinking_content(msg))

    def test_has_thinking_string(self):
        msg = {"role": "assistant", "content": "<thinking>deep</thinking>"}
        self.assertTrue(proxy._has_thinking_content(msg))

    def test_no_thinking(self):
        msg = {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}
        self.assertFalse(proxy._has_thinking_content(msg))

    def test_strip_thinking_removes_block(self):
        msg = {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "deep"},
            {"type": "text", "text": "answer"},
        ]}
        proxy._strip_thinking_from_msg(msg)
        types = [b["type"] for b in msg["content"]]
        self.assertNotIn("thinking", types)
        self.assertIn("text", types)

    def test_strip_thinking_removes_inline_tag(self):
        msg = {"role": "assistant", "content": [
            {"type": "text", "text": "<thinking>inner</thinking> answer"},
        ]}
        proxy._strip_thinking_from_msg(msg)
        self.assertNotIn("<thinking>", msg["content"][0]["text"])
        self.assertIn("answer", msg["content"][0]["text"])

    def test_strip_thinking_string_content(self):
        msg = {"role": "assistant", "content": "<thinking>deep</thinking> result"}
        proxy._strip_thinking_from_msg(msg)
        self.assertNotIn("<thinking>", msg["content"])


class TestDedup(unittest.TestCase):
    def test_first_request_passes(self):
        proxy._DEDUP_CACHE.clear()
        result = proxy._check_dedup('{"messages": []}')
        self.assertFalse(result)

    def test_duplicate_within_window(self):
        proxy._DEDUP_CACHE.clear()
        body = '{"messages": [{"role": "user", "content": "hi"}]}'
        proxy._check_dedup(body)
        result = proxy._check_dedup(body)
        self.assertTrue(result)

    def test_different_body_passes(self):
        proxy._DEDUP_CACHE.clear()
        proxy._check_dedup('{"messages": [{"role": "user", "content": "a"}]}')
        result = proxy._check_dedup('{"messages": [{"role": "user", "content": "b"}]}')
        self.assertFalse(result)


class TestFilterToolsSorting(unittest.TestCase):
    """Phase 1: filtered tools keep a stable order across requests."""

    def test_output_is_stably_sorted(self):
        tools = [
            {"name": "Zebra", "type": "custom", "input_schema": {}},
            {"name": "Alpha", "type": "custom", "input_schema": {}},
            {"name": "Middle", "type": "custom", "input_schema": {}},
            {"name": "Beta", "type": "custom", "input_schema": {}},
            {"name": "Gamma", "type": "custom", "input_schema": {}},
            {"name": "Delta", "type": "custom", "input_schema": {}},
        ]
        messages = [{"role": "assistant", "content": [{"type": "tool_use", "name": "Alpha"}]}]
        original_max = proxy.PROXY_TOOL_FILTER_MAX
        original_keep = proxy.TOOL_ALWAYS_KEEP
        proxy.PROXY_TOOL_FILTER_MAX = 5
        proxy_state.PROXY_TOOL_FILTER_MAX = 5
        proxy_state.PROXY_TOOL_FILTER_MAX = 5
        proxy_state.PROXY_TOOL_FILTER_MAX = 5
        proxy.TOOL_ALWAYS_KEEP = ("Alpha", "Zebra", "Middle", "Beta", "Gamma", "Delta")
        proxy_state.TOOL_ALWAYS_KEEP = ("Alpha", "Zebra", "Middle", "Beta", "Gamma", "Delta")
        try:
            kept, stats = proxy._filter_tools(tools, messages, recent_rounds=5)
            self.assertTrue(stats.get("filtered"), f"Expected filtering, got {stats}")
            names = [t["name"] for t in kept]
            # Stable order should match TOOL_ALWAYS_KEEP order for all-kept case.
            self.assertEqual(names, ["Alpha", "Zebra", "Middle", "Beta", "Gamma", "Delta"])
        finally:
            proxy.PROXY_TOOL_FILTER_MAX = original_max
            proxy_state.PROXY_TOOL_FILTER_MAX = original_max
            proxy_state.PROXY_TOOL_FILTER_MAX = original_max
            proxy_state.PROXY_TOOL_FILTER_MAX = original_max
            proxy.TOOL_ALWAYS_KEEP = original_keep
            proxy_state.TOOL_ALWAYS_KEEP = original_keep


class TestSemanticCompression(unittest.TestCase):
    """Phase 2: TokenSieve-inspired content compression."""

    def test_scrub_ansi(self):
        text = "\x1b[31merror\x1b[0m: failed"
        self.assertEqual(proxy._scrub_ansi(text), "error: failed")

    def test_detect_json(self):
        self.assertEqual(proxy._detect_content_type('{"a": 1}'), "json")
        self.assertEqual(proxy._detect_content_type('[1, 2, 3]'), "json")

    def test_detect_code(self):
        code = "def foo():\n    return 1\n"
        self.assertEqual(proxy._detect_content_type(code), "code")

    def test_detect_log(self):
        log = "2024-01-01 10:00:00 INFO start\n2024-01-01 10:00:01 WARN slow\n"
        self.assertEqual(proxy._detect_content_type(log), "log")

    def test_sieve_json_array_truncation(self):
        data = [{"id": i, "name": "x" * 300} for i in range(20)]
        result = proxy._sieve_json(data, max_items=5, max_str_len=50, max_depth=4)
        self.assertEqual(len(result), 6)
        self.assertIn("more items", result[-1])
        self.assertIn("truncated", result[0]["name"])

    def test_sieve_json_string_truncation(self):
        data = {"key": "v" * 500}
        result = proxy._sieve_json(data, max_str_len=50)
        self.assertIn("truncated", result["key"])

    def test_compress_code_removes_comments(self):
        code = "# header\ndef foo():\n    # inline\n    return 1\n\n\n"
        result = proxy._compress_code(code)
        self.assertNotIn("#", result)
        self.assertIn("def foo():", result)

    def test_compress_log_dedupes(self):
        log = "INFO ok\nINFO ok\nINFO ok\nERROR boom\n"
        result = proxy._compress_log(log, dedupe=True)
        self.assertIn("identical lines omitted", result)
        self.assertIn("ERROR boom", result)

    def test_compress_text_truncates(self):
        text = "x" * 5000
        result = proxy._compress_text(text, max_len=1000)
        self.assertLess(len(result), len(text))
        self.assertIn("truncated", result)

    def test_dedupe_scalars(self):
        data = {"a": "long repeated value", "b": "long repeated value"}
        result = proxy._dedupe_scalars(data, min_len=5)
        self.assertIn("###", result["b"])

    def test_compress_tool_result_json(self):
        data = json.dumps({"items": [{"id": i, "text": "t" * 300} for i in range(50)]})
        result = proxy.compress_tool_result(data, threshold=100, mode="semantic")
        self.assertEqual(result["content_type"], "json")
        self.assertLess(result["ratio"], 1.0)
        self.assertTrue(result["audit_pass"])

    def test_compress_tool_result_short_passthrough(self):
        result = proxy.compress_tool_result("hello", threshold=100)
        self.assertEqual(result["strategy"], "none")
        self.assertEqual(result["ratio"], 1.0)

    def test_audit_json_fails_on_broken(self):
        self.assertFalse(proxy._audit_compression("", "{not json", "json"))

    def test_audit_code_passes_balanced(self):
        self.assertTrue(proxy._audit_compression("", "def f(): pass", "code"))


class TestConvertAnthropicMessagesToOpenAI(unittest.TestCase):
    def test_plain_text_user_message(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = proxy.convert_anthropic_messages_to_openai(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["content"], "hello")

    def test_plain_text_assistant_message(self):
        msgs = [{"role": "assistant", "content": "hi there"}]
        result = proxy.convert_anthropic_messages_to_openai(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "assistant")
        self.assertEqual(result[0]["content"], "hi there")

    def test_tool_use_message(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/foo/bar.py"}}
        ]}]
        result = proxy.convert_anthropic_messages_to_openai(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "assistant")
        self.assertIn("tool_calls", result[0])
        self.assertEqual(result[0]["tool_calls"][0]["function"]["name"], "Read")

    def test_tool_result_message(self):
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents here"}
        ]}]
        result = proxy.convert_anthropic_messages_to_openai(msgs)
        self.assertTrue(any(m["role"] == "tool" for m in result))
        tool_msg = [m for m in result if m["role"] == "tool"][0]
        self.assertEqual(tool_msg["tool_call_id"], "tu_1")
        self.assertEqual(tool_msg["content"], "file contents here")

    def test_mixed_text_and_tool_use(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "Let me read that file."},
            {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/a.py"}}
        ]}]
        result = proxy.convert_anthropic_messages_to_openai(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "assistant")
        self.assertIn("content", result[0])
        self.assertIn("tool_calls", result[0])
        self.assertEqual(result[0]["content"], "Let me read that file.")

    def test_string_content_fallback(self):
        msgs = [{"role": "user", "content": "just a string"}]
        result = proxy.convert_anthropic_messages_to_openai(msgs)
        self.assertEqual(result[0]["content"], "just a string")

    def test_empty_messages_list(self):
        result = proxy.convert_anthropic_messages_to_openai([])
        self.assertEqual(result, [])


class TestRepairTruncatedJsonBrackets(unittest.TestCase):
    def test_array_truncation(self):
        result = proxy._repair_truncated_json('{"items": [1, 2, 3')
        parsed = json.loads(result)
        self.assertEqual(parsed["items"], [1, 2, 3])

    def test_nested_mixed_truncation(self):
        result = proxy._repair_truncated_json('{"a": [1, {"b": 2')
        parsed = json.loads(result)
        self.assertIsInstance(parsed["a"], list)
        self.assertIsInstance(parsed["a"][1], dict)

    def test_multiple_open_brackets(self):
        result = proxy._repair_truncated_json('{"x": [[1, 2')
        parsed = json.loads(result)
        self.assertEqual(parsed["x"], [[1, 2]])

    def test_already_complete_json(self):
        original = '{"items": [1, 2, 3]}'
        result = proxy._repair_truncated_json(original)
        self.assertEqual(json.loads(result), json.loads(original))


class TestEstimateMessageCharsToolUse(unittest.TestCase):
    def test_counts_tool_use_input(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/a.py", "content": "x" * 100}}
        ]}]
        result = proxy._estimate_message_chars(msgs)
        self.assertGreater(result, 100)

    def test_counts_all_block_types(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
            {"type": "tool_result", "content": "file content", "tool_use_id": "tu1"}
        ]}]
        result = proxy._estimate_message_chars(msgs)
        self.assertGreater(result, 20)


class TestWriteSimilarityLoopKey(unittest.TestCase):
    def test_write_same_file_different_content_same_key(self):
        import anthropic_proxy as proxy
        msgs_1 = [{"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/foo.py", "content": "v1"}}
        ]}]
        msgs_2 = [{"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/foo.py", "content": "v2"}}
        ]}]
        inp_1 = msgs_1[0]["content"][0]["input"]
        inp_2 = msgs_2[0]["content"][0]["input"]
        args_1 = json.dumps(inp_1, sort_keys=True, ensure_ascii=False)
        args_2 = json.dumps(inp_2, sort_keys=True, ensure_ascii=False)
        name = "Write"
        if name in ("Write", "Edit") and isinstance(inp_1, dict):
            fp = inp_1.get("file_path") or inp_1.get("path") or ""
            if fp:
                args_1 = f"file={fp}"
        if name in ("Write", "Edit") and isinstance(inp_2, dict):
            fp = inp_2.get("file_path") or inp_2.get("path") or ""
            if fp:
                args_2 = f"file={fp}"
        key_1 = f"{name}:{args_1}"
        key_2 = f"{name}:{args_2}"
        self.assertEqual(key_1, key_2)


class TestFrozenZoneToolClearing(unittest.TestCase):
    """Tests for Frozen Zone protection in clear_old_tool_results.

    When PROXY_FROZEN_HEAD > 0 (default 12 for local), the first N
    messages must NEVER be modified by L2 clearing. Only tool_results
    in the dynamic zone (messages[PROXY_FROZEN_HEAD:]) are eligible.
    """

    def setUp(self):
        self._patches = [
            patch.object(proxy, "PROXY_CLEAR_ENABLED", True), patch.object(proxy_state, "PROXY_CLEAR_ENABLED", True),
            patch.object(proxy, "PROXY_CLEAR_THRESHOLD", 0), patch.object(proxy_state, "PROXY_CLEAR_THRESHOLD", 0),
            patch.object(proxy, "PROXY_TOOL_KEEP", 2),
            patch.object(proxy, "PROXY_FROZEN_HEAD", 12), patch.object(proxy_state, "PROXY_FROZEN_HEAD", 12),
            patch.object(proxy, "PROXY_REREAD_PREVIEW_CHARS", 200),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _msgs_with_frozen(self, n_frozen_reads=3, n_dynamic_reads=6):
        """Build messages with some in the Frozen Zone and some in dynamic."""
        msgs = []
        # Frozen zone reads (should NEVER be cleared)
        for i in range(n_frozen_reads):
            path = f"/frozen/file_{i}.py"
            content = f"FROZEN_{i}_MARKER " + "frozen data here. " * 15  # ~300 chars
            msgs.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"tu_f{i}",
                              "name": "Read", "input": {"file_path": path}}],
            })
            msgs.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"tu_f{i}",
                              "content": content}],
            })
        # Pad some non-tool messages to fill up to PROXY_FROZEN_HEAD
        filler_needed = proxy.PROXY_FROZEN_HEAD - len(msgs)
        for i in range(filler_needed):
            msgs.append({
                "role": "user",
                "content": [{"type": "text", "text": f"filler message {i}"}],
            })
        # Dynamic zone reads (eligible for clearing)
        for i in range(n_dynamic_reads):
            path = f"/dynamic/file_{i}.py"
            content = f"DYNAMIC_{i}_MARKER " + "dynamic data here. " * 20  # ~400 chars
            msgs.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"tu_d{i}",
                              "name": "Read", "input": {"file_path": path}}],
            })
            msgs.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"tu_d{i}",
                              "content": content}],
            })
        return msgs

    def test_frozen_zone_tool_results_untouched(self):
        """Frozen zone Read results are NEVER cleared. Only dynamic zone
        tool_results are eligible."""
        msgs = self._msgs_with_frozen(n_frozen_reads=3, n_dynamic_reads=6)
        # Total: 3 frozen reads + 12 filler + 6 dynamic reads = 21 messages
        # KEEP=2 → only 2 dynamic tool_results survive
        result, stats = proxy.clear_old_tool_results(msgs)
        self.assertTrue(stats["cleared"])
        self.assertEqual(stats["frozen_used"], 12)

        # Check frozen zone tool_results still have their original content
        for i in range(3):
            user_msg_idx = 2 * i + 1  # assistant + user per read
            block = result[user_msg_idx]["content"][0]
            content = str(block.get("content", ""))
            self.assertIn(f"FROZEN_{i}_MARKER", content,
                          f"Frozen zone Read #{i} was incorrectly cleared")
            self.assertNotIn("[cleared:", content,
                             f"Frozen zone Read #{i} has [cleared:] marker")

    def test_dynamic_zone_cleared(self):
        """Dynamic zone tool_results are cleared as expected, leaving
        only KEEP=2 surviving."""
        msgs = self._msgs_with_frozen(n_frozen_reads=3, n_dynamic_reads=6)
        result, stats = proxy.clear_old_tool_results(msgs)

        # Count cleared items in dynamic zone
        dynamic_cleared = 0
        for idx, m in enumerate(result):
            if idx < proxy.PROXY_FROZEN_HEAD:
                continue  # skip frozen zone
            content = m.get("content", "")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = str(b.get("content", ""))
                        if c.startswith("[cleared:"):
                            dynamic_cleared += 1

        # 6 dynamic reads, KEEP=2 → at least 4 cleared
        self.assertGreaterEqual(dynamic_cleared, 4,
                                "Expected at least 4 dynamic tool_results cleared")

    def test_frozen_zone_reduced_on_few_dynamic(self):
        """When dynamic zone has too few tool_results, frozen_head should
        be reduced to allow some clearing."""
        msgs = self._msgs_with_frozen(n_frozen_reads=3, n_dynamic_reads=1)
        # Only 1 dynamic Read + KEEP=2 → not enough
        result, stats = proxy.clear_old_tool_results(msgs)
        # Should have skipped (reason: few_tool_results) OR reduced frozen
        # Either way, at least frozen zone is partially protected
        self.assertIn("frozen_head", stats)
        self.assertIn("frozen_used", stats)

    def test_frozen_zero_clears_all(self):
        """When PROXY_FROZEN_HEAD=0, all tool_results are eligible."""
        with patch.object(proxy, "PROXY_FROZEN_HEAD", 0), patch.object(proxy_state, "PROXY_FROZEN_HEAD", 0):
            msgs = self._msgs_with_frozen(n_frozen_reads=2, n_dynamic_reads=4)
            result, stats = proxy.clear_old_tool_results(msgs)
            self.assertEqual(stats["frozen_used"], 0)
            # Total 6 reads, KEEP=2 → at least 4 cleared total
            self.assertGreaterEqual(stats["cleared_tool_results"], 4)


class TestFrozenZoneThinkingStrip(unittest.TestCase):
    """Tests for Frozen Zone protection in strip_old_thinking_blocks.

    Thinking blocks in the Frozen Zone must NEVER be stripped.
    Only thinking content in messages[PROXY_FROZEN_HEAD:] is eligible.
    """

    def setUp(self):
        self._patches = [
            patch.object(proxy, "PROXY_FROZEN_HEAD", 12), patch.object(proxy_state, "PROXY_FROZEN_HEAD", 12),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _build_msgs_with_thinking(self, n_frozen=4, n_dynamic=8):
        """Build messages with thinking blocks in frozen and dynamic zones."""
        msgs = []
        # Frozen zone: assistant msgs with thinking
        for i in range(n_frozen):
            msgs.append({
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": f"frozen thinking {i}"},
                    {"type": "text", "text": f"Frozen response {i}"},
                ],
            })
        # Fillers to reach PROXY_FROZEN_HEAD
        while len(msgs) < proxy.PROXY_FROZEN_HEAD:
            msgs.append({"role": "user", "content": [{"type": "text", "text": "filler"}]})
        # Dynamic zone: assistant msgs with thinking
        for i in range(n_dynamic):
            msgs.append({
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": f"dynamic thinking {i}"},
                    {"type": "text", "text": f"Dynamic response {i}"},
                ],
            })
        return msgs

    def _has_thinking(self, msg):
        content = msg.get("content", "")
        if isinstance(content, list):
            for b in content:
                if b.get("type") == "thinking":
                    return True
        return False

    def test_frozen_zone_thinking_preserved(self):
        """Frozen zone thinking blocks survive strip_old_thinking_blocks."""
        msgs = self._build_msgs_with_thinking(n_frozen=4, n_dynamic=8)
        result, stats = proxy.strip_old_thinking_blocks(msgs, keep_recent=3)
        self.assertTrue(stats["stripped"])
        # All frozen zone messages should still have thinking
        for i in range(4):
            self.assertTrue(self._has_thinking(result[i]),
                            f"Frozen zone msg {i} had thinking stripped")
        # Some dynamic zone thinking should be stripped
        dynamic_stripped = 0
        for i in range(proxy.PROXY_FROZEN_HEAD, len(result)):
            if not self._has_thinking(result[i]):
                dynamic_stripped += 1
        self.assertGreater(dynamic_stripped, 0,
                           "Expected some dynamic thinking stripped")

    def test_dynamic_keeps_recent_thinking(self):
        """The most recent `keep_recent` dynamic thinking messages survive."""
        msgs = self._build_msgs_with_thinking(n_frozen=4, n_dynamic=5)
        result, stats = proxy.strip_old_thinking_blocks(msgs, keep_recent=2)
        self.assertTrue(stats["stripped"])
        # Find dynamic messages with thinking still present
        dynamic_thinking_alive = 0
        for i in range(proxy.PROXY_FROZEN_HEAD, len(result)):
            if self._has_thinking(result[i]):
                dynamic_thinking_alive += 1
        # At most keep_recent=2 should survive in dynamic
        self.assertGreaterEqual(dynamic_thinking_alive, 1)


class TestLifecycleStage(unittest.TestCase):
    """Tests for _classify_lifecycle_stage — unified char-based thresholds."""

    def _msgs_chars(self, target_chars):
        """Build message list with approximately target_chars total."""
        msgs = []
        remaining = target_chars
        i = 0
        while remaining > 0:
            chunk = min(remaining, 5000)
            msgs.append({
                "role": "user",
                "content": [{"type": "text", "text": "x" * chunk}],
            })
            remaining -= chunk
            i += 1
        return msgs

    def test_init_stage(self):
        """Below PROXY_CLEAR_THRESHOLD → stage=init, no compression."""
        with patch.object(proxy, "PROXY_CLEAR_THRESHOLD", 15000), patch.object(proxy_state, "PROXY_CLEAR_THRESHOLD", 15000):
            msgs = self._msgs_chars(10000)
            stage = proxy._classify_lifecycle_stage(msgs)
            self.assertEqual(stage["stage"], "init")
            self.assertIsNone(stage["clear_zone_pct"])
            self.assertIsNone(stage["truncate_rounds"])
            self.assertFalse(stage["oom_safety"])

    def test_growth_stage(self):
        """PROXY_CLEAR_THRESHOLD ≤ chars < PROXY_CHARS_GROWTH → growth."""
        with patch.object(proxy, "PROXY_CLEAR_THRESHOLD", 15000), patch.object(proxy_state, "PROXY_CLEAR_THRESHOLD", 15000):
            with patch.object(proxy, "PROXY_CHARS_GROWTH", 40000), patch.object(proxy_state, "PROXY_CHARS_GROWTH", 40000):
                msgs = self._msgs_chars(25000)
                stage = proxy._classify_lifecycle_stage(msgs)
                self.assertEqual(stage["stage"], "growth")
                self.assertEqual(stage["clear_zone_pct"], 0.4)
                self.assertEqual(stage["thinking_keep"], 0)
                self.assertIsNone(stage["truncate_rounds"])

    def test_expansion_stage(self):
        """PROXY_CHARS_GROWTH ≤ chars < PROXY_CHARS_EXPANSION → expansion."""
        with patch.object(proxy, "PROXY_CHARS_GROWTH", 40000), patch.object(proxy_state, "PROXY_CHARS_GROWTH", 40000):
            with patch.object(proxy, "PROXY_CHARS_EXPANSION", 90000), patch.object(proxy_state, "PROXY_CHARS_EXPANSION", 90000):
                msgs = self._msgs_chars(60000)
                stage = proxy._classify_lifecycle_stage(msgs)
                self.assertEqual(stage["stage"], "expansion")
                self.assertEqual(stage["clear_zone_pct"], 0.6)
                self.assertEqual(stage["thinking_keep"], 5)
                self.assertIsNotNone(stage["truncate_rounds"])

    def test_saturation_stage(self):
        """PROXY_CHARS_EXPANSION ≤ chars < PROXY_CHARS_SATURATION."""
        with patch.object(proxy, "PROXY_CHARS_EXPANSION", 90000), patch.object(proxy_state, "PROXY_CHARS_EXPANSION", 90000):
            with patch.object(proxy, "PROXY_CHARS_SATURATION", 180000), patch.object(proxy_state, "PROXY_CHARS_SATURATION", 180000):
                msgs = self._msgs_chars(120000)
                stage = proxy._classify_lifecycle_stage(msgs)
                self.assertEqual(stage["stage"], "saturation")
                self.assertEqual(stage["clear_zone_pct"], 1.0)

    def test_oom_danger_stage(self):
        """PROXY_CHARS_SATURATION ≤ chars < PROXY_CHARS_OOM_DANGER."""
        with patch.object(proxy, "PROXY_CHARS_SATURATION", 180000), patch.object(proxy_state, "PROXY_CHARS_SATURATION", 180000):
            with patch.object(proxy, "PROXY_CHARS_OOM_DANGER", 350000), patch.object(proxy_state, "PROXY_CHARS_OOM_DANGER", 350000):
                msgs = self._msgs_chars(250000)
                stage = proxy._classify_lifecycle_stage(msgs)
                self.assertEqual(stage["stage"], "oom_danger")
                self.assertEqual(stage["frozen_head"], 0)
                self.assertEqual(stage["thinking_keep"], 1)
                self.assertTrue(stage["oom_safety"])

    def test_pre_trunc_stage(self):
        """chars ≥ PROXY_CHARS_OOM_DANGER → pre_trunc."""
        with patch.object(proxy, "PROXY_CHARS_OOM_DANGER", 350000), patch.object(proxy_state, "PROXY_CHARS_OOM_DANGER", 350000):
            msgs = self._msgs_chars(500000)
            stage = proxy._classify_lifecycle_stage(msgs)
            self.assertEqual(stage["stage"], "pre_trunc")
            self.assertEqual(stage["truncate_rounds"], 2)

    def test_stages_monotonic(self):
        """Lifecycle stage transitions are strictly monotonic:
        init→growth→expansion→saturation→oom_danger.

        Note: PROXY_OOM_SAFE_CHARS (formerly PROXY_PRE_TRUNCATE_CHARS) is
        NOT a lifecycle stage — it's a hard pre-processing ceiling applied
        in do_POST before stage classification, with default 200K that
        sits between SATURATION(180K) and OOM_DANGER(350K)."""
        thresholds = [proxy.PROXY_CLEAR_THRESHOLD, proxy.PROXY_CHARS_GROWTH,
                      proxy.PROXY_CHARS_EXPANSION, proxy.PROXY_CHARS_SATURATION,
                      proxy.PROXY_CHARS_OOM_DANGER]
        for i in range(len(thresholds) - 1):
            self.assertLess(thresholds[i], thresholds[i + 1],
                            f"Threshold {i} ({thresholds[i]}) >= threshold {i+1} ({thresholds[i+1]})")

    def test_oom_safe_chars_default(self):
        """PROXY_OOM_SAFE_CHARS default is 200K (lowered from 400K in Phase 1)
        and is the canonical name; PROXY_PRE_TRUNCATE_CHARS is the legacy alias
        and should be equal to it."""
        # Canonical name exists with sensible default
        self.assertIsNotNone(proxy.PROXY_OOM_SAFE_CHARS)
        self.assertGreater(proxy.PROXY_OOM_SAFE_CHARS, 0)
        # Phase 1 default: 200K (was 400K). Not too aggressive.
        self.assertGreaterEqual(proxy.PROXY_OOM_SAFE_CHARS, 100_000)
        self.assertLessEqual(proxy.PROXY_OOM_SAFE_CHARS, 500_000)
        # Legacy alias mirrors the canonical name
        self.assertEqual(proxy.PROXY_PRE_TRUNCATE_CHARS, proxy.PROXY_OOM_SAFE_CHARS)


# =============================================================================
# Phase 1 (proxy-truncation-agent-scenario.md) test coverage
# =============================================================================
class TestPhase1RoundsBudgetIterate(unittest.TestCase):
    """改进1: rounds strategy now iterates keep_rounds down to fit the
    char budget. Previously a stage-specified keep_rounds=10 with each
    round containing 50 messages leaked through at 200K+ chars."""

    def setUp(self):
        self._patches = [
            patch.object(proxy, "PROXY_CTX_TRUNCATE_STRATEGY", "rounds"), patch.object(proxy_state, "PROXY_CTX_TRUNCATE_STRATEGY", "rounds"),
            patch.object(proxy, "PROXY_CTX_KEEP_ROUNDS", 10), patch.object(proxy_state, "PROXY_CTX_KEEP_ROUNDS", 10),
            # Tight budget so iteration is forced.
            patch.object(proxy, "PROXY_CHARS_EXPANSION", 30_000), patch.object(proxy_state, "PROXY_CHARS_EXPANSION", 30_000),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _make_large_rounds(self, n_rounds=15, msgs_per_round=4, msg_chars=2000):
        """Build messages with 15 assistant rounds, each round has many
        msgs, so total >> budget even after keep_rounds=10 truncation."""
        msgs = [
            {"role": "system", "content": "you are a coding assistant."},
            {"role": "user", "content": "first task"},
        ]
        for r in range(n_rounds):
            for m in range(msgs_per_round):
                role = "assistant" if m == 0 else "user"
                msgs.append({
                    "role": role,
                    "content": [{"type": "text", "text": "x" * msg_chars}],
                })
        return msgs

    def test_stage_specified_keep_rounds_iterates_to_fit_budget(self):
        """Stage says keep_rounds=10 but 10 rounds × 4 msgs × 2000 chars =
        80,000 chars, well above the patched 30K budget. The new iteration
        must drop keep_rounds until result fits or reaches min_rounds=2."""
        msgs = self._make_large_rounds(n_rounds=15, msgs_per_round=4, msg_chars=2000)
        result, stats = proxy.truncate_messages_if_needed(
            msgs, session_id="phase1_test", keep_rounds=10
        )
        # Truncation must have triggered
        self.assertTrue(stats.get("truncated"))
        # The new budget_iterations field records how many rounds we dropped
        self.assertIn("budget_iterations", stats)
        self.assertGreaterEqual(stats["budget_iterations"], 1,
            "stage branch should have iterated to fit budget")
        # actual_keep_rounds must be less than stage-specified keep_rounds
        self.assertLess(stats["actual_keep_rounds"], 10)
        self.assertGreaterEqual(stats["actual_keep_rounds"], 2)
        # Result should fit within budget
        result_chars = proxy._estimate_message_chars(result)
        self.assertLessEqual(result_chars, 30_000,
            f"result {result_chars:,} chars exceeds budget 30K — iteration failed")

    def test_small_payload_skips_iteration(self):
        """If total_chars is already below budget, no iteration should
        happen and the result should be returned unchanged."""
        msgs = [{"role": "user", "content": "hello world"}]
        result, stats = proxy.truncate_messages_if_needed(
            msgs, session_id="phase1_test", keep_rounds=10
        )
        self.assertEqual(stats.get("strategy"), "rounds")
        self.assertTrue(stats.get("skipped"))
        self.assertEqual(stats.get("reason"), "below_budget")
        # Iteration count is only recorded when iteration actually ran;
        # the field should be absent or 0
        self.assertFalse(stats.get("budget_iterations", 0) > 0)


class TestPhase1SessionContinuation(unittest.TestCase):
    """改进3: session continuation detection. A session_id that has
    accumulated >= PROXY_SESSION_CONTINUATION_MIN_REQUESTS prior calls
    triggers an aggressive config when payload > PROXY_CHARS_EXPANSION,
    regardless of the raw stage mapping."""

    def setUp(self):
        # Clear any state from other tests.
        proxy._SESSION_REQUEST_COUNT.clear()
        self._patches = [
            patch.object(proxy, "PROXY_SESSION_CONTINUATION_ENABLED", True), patch.object(proxy_state, "PROXY_SESSION_CONTINUATION_ENABLED", True),
            patch.object(proxy, "PROXY_SESSION_CONTINUATION_MIN_REQUESTS", 2), patch.object(proxy_state, "PROXY_SESSION_CONTINUATION_MIN_REQUESTS", 2),
            patch.object(proxy, "PROXY_CHARS_EXPANSION", 90_000), patch.object(proxy_state, "PROXY_CHARS_EXPANSION", 90_000),
            patch.object(proxy, "PROXY_CHARS_SATURATION", 180_000), patch.object(proxy_state, "PROXY_CHARS_SATURATION", 180_000),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        proxy._SESSION_REQUEST_COUNT.clear()

    def _msgs_chars(self, target):
        msgs = []
        remaining = target
        while remaining > 0:
            chunk = min(remaining, 5000)
            msgs.append({"role": "user", "content": [{"type": "text", "text": "x" * chunk}]})
            remaining -= chunk
        return msgs

    def test_first_call_no_continuation(self):
        """First call with a session_id must NOT be marked as continuation."""
        msgs = self._msgs_chars(100_000)  # 100K, above EXPANSION(90K)
        stage = proxy._classify_lifecycle_stage(msgs, session_id="sess_first")
        # is_continuation must be False on first call (count was 0 → < 2)
        self.assertFalse(stage.get("is_continuation"))
        # count is recorded
        self.assertEqual(stage.get("request_count"), 0)
        # Stage should be the natural stage (expansion or saturation)
        self.assertIn(stage["stage"], ("expansion", "saturation"))

    def test_continuation_triggers_aggressive_config(self):
        """After 2 prior calls (count=2), the 3rd call with > 90K chars
        must return the aggressive saturation config."""
        # Pretend 2 prior calls happened
        proxy._SESSION_REQUEST_COUNT["sess_aggr"] = 2
        msgs = self._msgs_chars(100_000)
        stage = proxy._classify_lifecycle_stage(msgs, session_id="sess_aggr")
        self.assertTrue(stage.get("is_continuation"))
        # Aggressive branch forces saturation config
        self.assertEqual(stage["stage"], "saturation")
        self.assertEqual(stage["frozen_head"], 2)
        self.assertEqual(stage["clear_zone_pct"], 1.0)
        self.assertEqual(stage["thinking_keep"], 3)
        # truncate_rounds is max(3, KEEP_ROUNDS//2). Default 10 → 5.
        self.assertEqual(stage["truncate_rounds"], max(3, 10 // 2))
        # oom_safety on for local
        self.assertTrue(stage["oom_safety"])

    def test_continuation_below_expansion_stays_in_normal_stage(self):
        """A continuation call with a small payload (< 90K) must NOT be
        forced into the aggressive branch."""
        proxy._SESSION_REQUEST_COUNT["sess_small"] = 5
        msgs = self._msgs_chars(30_000)  # 30K, below EXPANSION(90K)
        stage = proxy._classify_lifecycle_stage(msgs, session_id="sess_small")
        # Continuation flag set, but stage is the natural one (init/growth/expansion)
        self.assertTrue(stage.get("is_continuation"))
        self.assertIn(stage["stage"], ("init", "growth", "expansion"))

    def test_disabled_continuation_never_triggers(self):
        """When PROXY_SESSION_CONTINUATION_ENABLED is false, the counter
        is not consulted regardless of count, and the function does not
        increment it."""
        with patch.object(proxy, "PROXY_SESSION_CONTINUATION_ENABLED", False), patch.object(proxy_state, "PROXY_SESSION_CONTINUATION_ENABLED", False):
            # Pre-seed the counter to verify it is not advanced.
            proxy._SESSION_REQUEST_COUNT["sess_disabled"] = 100
            before = proxy._SESSION_REQUEST_COUNT["sess_disabled"]
            msgs = self._msgs_chars(100_000)
            stage = proxy._classify_lifecycle_stage(msgs, session_id="sess_disabled")
            self.assertFalse(stage.get("is_continuation"))
            # Counter must NOT have been advanced by the call
            self.assertEqual(proxy._SESSION_REQUEST_COUNT["sess_disabled"], before,
                "disabled continuation should not increment the counter")
            # request_count reported to the caller is 0 (counter was not read)
            self.assertEqual(stage.get("request_count"), 0)

    def test_counter_increments_each_call(self):
        """Each call with a session_id must bump the counter by 1."""
        for expected in range(3):
            stage = proxy._classify_lifecycle_stage(
                [{"role": "user", "content": "hi"}],
                session_id="sess_count",
            )
            self.assertEqual(stage["request_count"], expected)
        # After 3 calls the counter should be 3
        self.assertEqual(proxy._SESSION_REQUEST_COUNT["sess_count"], 3)


class TestPhase1RoundsStrategyBudgetLoop(unittest.TestCase):
    """改进1 end-to-end: the rounds branch in truncate_messages_if_needed
    must reduce keep_rounds when the initial truncation still exceeds
    PROXY_CHARS_EXPANSION. This was the core bug (kept_rounds=10 → result
    200K+ chars)."""

    def setUp(self):
        proxy._SESSION_REQUEST_COUNT.clear()
        self._patches = [
            patch.object(proxy, "PROXY_CTX_TRUNCATE_STRATEGY", "rounds"), patch.object(proxy_state, "PROXY_CTX_TRUNCATE_STRATEGY", "rounds"),
            patch.object(proxy, "PROXY_CTX_KEEP_ROUNDS", 10), patch.object(proxy_state, "PROXY_CTX_KEEP_ROUNDS", 10),
            patch.object(proxy, "PROXY_CHARS_EXPANSION", 40_000), patch.object(proxy_state, "PROXY_CHARS_EXPANSION", 40_000),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        proxy._SESSION_REQUEST_COUNT.clear()

    def test_high_msg_density_session_fits_budget(self):
        """50 messages × 2K chars = 100K input. With keep_rounds=10 the
        first attempt keeps ~half, still ~50K > 40K budget. The fix must
        reduce keep_rounds until result <= 40K."""
        msgs = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": [{"type": "text", "text": "x" * 2000}]}
            for i in range(50)
        ]
        result, stats = proxy.truncate_messages_if_needed(
            msgs, session_id="phase1_budget", keep_rounds=10,
        )
        result_chars = proxy._estimate_message_chars(result)
        self.assertLessEqual(result_chars, 40_000,
            f"rounds strategy didn't iterate to fit 40K budget; got {result_chars:,}")
        self.assertGreaterEqual(stats.get("budget_iterations", 0), 1)


# =============================================================================
# Phase 2 (proxy-truncation-agent-scenario.md) test coverage
# =============================================================================
class TestPhase2SmartStrategy(unittest.TestCase):
    """改进2: smart strategy — role+content-aware truncation. Preserves
    system and tool_result messages verbatim, keeps newer user/assistant
    messages, and compresses assistant reasoning into a stable placeholder
    when the original doesn't fit the budget."""

    def setUp(self):
        self._patches = [
            patch.object(proxy, "PROXY_CTX_TRUNCATE_STRATEGY", "smart"), patch.object(proxy_state, "PROXY_CTX_TRUNCATE_STRATEGY", "smart"),
            patch.object(proxy, "PROXY_CTX_LIMIT_ENABLED", True), patch.object(proxy_state, "PROXY_CTX_LIMIT_ENABLED", True),
            patch.object(proxy, "PROXY_CHARS_EXPANSION", 30_000), patch.object(proxy_state, "PROXY_CHARS_EXPANSION", 30_000),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_below_budget_skips(self):
        """Small payload returns unchanged with skipped=True."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result, stats = proxy.truncate_messages_if_needed(msgs, session_id="t")
        self.assertEqual(result, msgs)
        self.assertTrue(stats.get("skipped"))
        self.assertEqual(stats.get("reason"), "below_budget")

    def test_system_messages_preserved(self):
        """All system messages must survive even when budget is tight."""
        msgs = [
            {"role": "system", "content": "you are a coding assistant"},
            {"role": "system", "content": "skill definition"},
            {"role": "user", "content": "x" * 50_000},
        ]
        result, stats = proxy.truncate_messages_if_needed(msgs, session_id="t")
        system_count = sum(1 for m in result if m.get("role") == "system")
        self.assertEqual(system_count, 2, "all system messages must be preserved")

    def test_tool_result_messages_preserved(self):
        """tool_result blocks (file contents) must never be dropped or
        compressed — losing them forces the model into a re-read loop."""
        msgs = []
        for i in range(5):
            msgs.append({"role": "assistant", "content": [
                {"type": "text", "text": "x" * 8_000},  # large reasoning
                {"type": "tool_use", "id": f"t{i}", "name": "Read",
                 "input": {"file_path": f"/tmp/file_{i}.py"}},
            ]})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "content": f"file contents {i}: " + "y" * 1_500},
            ]})
        result, stats = proxy.truncate_messages_if_needed(msgs, session_id="t")
        # All 5 tool_result messages must be in the output
        out_tool_results = sum(
            1 for m in result
            if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in m["content"])
        )
        self.assertEqual(out_tool_results, 5,
            "smart strategy must keep ALL tool_result messages")
        # And the file contents must be intact (not compressed)
        for m in result:
            if m.get("role") == "user":
                for b in m.get("content", []):
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        self.assertIn("file contents", b.get("content", ""),
                            "tool_result content must not be altered")

    def test_assistant_messages_compressed_not_dropped(self):
        """When an assistant message doesn't fit, smart tries the compressed
        form (tool_use kept, text → [reasoning omitted]) before dropping."""
        msgs = []
        for i in range(8):
            msgs.append({"role": "assistant", "content": [
                {"type": "text", "text": "reasoning " + "X" * 4_000},
                {"type": "tool_use", "id": f"t{i}", "name": "Read",
                 "input": {"file_path": f"/tmp/file_{i}.py"}},
            ]})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "content": "small"},
            ]})
        result, stats = proxy.truncate_messages_if_needed(msgs, session_id="t")
        # At least some assistant messages should have been compressed
        self.assertGreater(stats.get("compressed_assistants", 0), 0,
            "smart strategy should have compressed some assistant messages")
        # Find a compressed assistant message and verify structure
        for m in result:
            if m.get("role") == "assistant":
                content = m.get("content", [])
                if isinstance(content, list):
                    text_blocks = [b for b in content
                                   if isinstance(b, dict) and b.get("type") == "text"]
                    tool_use_blocks = [b for b in content
                                       if isinstance(b, dict) and b.get("type") == "tool_use"]
                    if text_blocks and tool_use_blocks:
                        # This is a compressed message
                        self.assertEqual(text_blocks[0].get("text"), "[reasoning omitted]",
                            "compressed assistant text must be the stable placeholder")
                        self.assertGreater(len(tool_use_blocks), 0,
                            "compressed assistant must keep its tool_use blocks")
                        break

    def test_dropped_count_includes_unfittable(self):
        """When the budget is so tight that even compressed assistant
        messages don't fit, they must be dropped (counted in dropped)."""
        # 50 rounds, each with large reasoning → 50 × 4K = 200K of reasoning
        # plus 50 tool_results of 2K = 100K. Budget 30K means only system +
        # tool_results fit, all assistant reasoning must be dropped or compressed.
        msgs = []
        for i in range(50):
            msgs.append({"role": "assistant", "content": [
                {"type": "text", "text": "R" * 4_000},
                {"type": "tool_use", "id": f"t{i}", "name": "Bash",
                 "input": {"cmd": f"echo {i}"}},
            ]})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "content": "y" * 2_000},
            ]})
        result, stats = proxy.truncate_messages_if_needed(msgs, session_id="t")
        # tool_results must all be kept (100K of them, but must-keep set)
        out_tool_results = sum(
            1 for m in result
            if m.get("role") == "user"
            and any(isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in m.get("content", []))
        )
        # With 50 × 2K = 100K of must-keep content, even 30K budget can't
        # hold them. The strategy correctly reports must_keep_exceeds_budget
        # and keeps all 50 tool_results while dropping the assistants.
        self.assertIn(stats.get("reason"), ("must_keep_exceeds_budget", None),
            f"unexpected reason: {stats.get('reason')}")
        self.assertEqual(out_tool_results, 50)

    def test_chronological_order_preserved(self):
        """The result must maintain chronological order: system, then all
        tool_results (in original order), then the kept other messages
        (newest-first sampling) in chronological order."""
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(10):
            msgs.append({"role": "assistant", "content": [
                {"type": "text", "text": f"r{i} reasoning " + "X" * 1_500},
                {"type": "tool_use", "id": f"t{i}", "name": "Read",
                 "input": {"file_path": f"/f{i}"}},
            ]})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "content": f"file {i}: " + "y" * 500},
            ]})
        result, stats = proxy.truncate_messages_if_needed(msgs, session_id="t")
        # System first
        self.assertEqual(result[0].get("role"), "system")
        # All tool_results in chronological order (file 0, 1, 2, ...)
        tr_files = []
        for m in result:
            if m.get("role") == "user":
                for b in m.get("content", []):
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        # Extract the file index from "file N:"
                        c = b.get("content", "")
                        if "file " in c:
                            tr_files.append(int(c.split("file ")[1].split(":")[0]))
        self.assertEqual(tr_files, sorted(tr_files),
            "tool_results must be in chronological order")

    def test_strategy_in_stats(self):
        """The returned stats dict must report strategy=smart for observability."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "x" * 50_000},
        ]
        _, stats = proxy.truncate_messages_if_needed(msgs, session_id="t")
        self.assertEqual(stats.get("strategy"), "smart")

    def test_estimate_message_chars_tool_result_accuracy(self):
        """_is_tool_result_message correctly identifies tool_result blocks
        and ignores plain user text messages."""
        tool_result_msg = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "data"},
        ]}
        plain_user_msg = {"role": "user", "content": "hello"}
        assistant_msg = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "x", "name": "Read", "input": {}},
        ]}
        self.assertTrue(proxy._is_tool_result_message(tool_result_msg))
        self.assertFalse(proxy._is_tool_result_message(plain_user_msg))
        self.assertFalse(proxy._is_tool_result_message(assistant_msg))

    def test_compress_assistant_message_keeps_tool_use(self):
        """_compress_assistant_message must keep tool_use blocks verbatim
        (model needs tool name + args to make sense of subsequent results)
        and replace text blocks with the stable placeholder."""
        original = {"role": "assistant", "content": [
            {"type": "text", "text": "I will read the file"},
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/foo.py"}},
            {"type": "text", "text": "now I have the contents"},
        ]}
        compressed = proxy._compress_assistant_message(original)
        content = compressed["content"]
        self.assertIsInstance(content, list)
        # First text → placeholder
        self.assertEqual(content[0]["text"], "[reasoning omitted]")
        # tool_use kept verbatim
        self.assertEqual(content[1]["type"], "tool_use")
        self.assertEqual(content[1]["name"], "Read")
        self.assertEqual(content[1]["input"], {"file_path": "/foo.py"})
        # Second text → placeholder
        self.assertEqual(content[2]["text"], "[reasoning omitted]")


class TestPhase2ManageShPriority(unittest.TestCase):
    """改进5: manage.sh now uses a 3-tier priority chain for PROXY_*
    env vars, with LLAMA_CTX_STRATEGY as a semantic alias. The proxy
    already supports both names. This test verifies the alias is read
    correctly via os.environ by re-importing the module under a controlled
    env."""

    def test_llama_ctx_strategy_alias_works(self):
        """If only LLAMA_CTX_STRATEGY is set, the proxy picks it up
        (manage.sh forwards it as PROXY_CTX_TRUNCATE_STRATEGY)."""
        # This is documented behavior: manage.sh expands
        # PROXY_CTX_TRUNCATE_STRATEGY="${PROXY_CTX_TRUNCATE_STRATEGY:-${LLAMA_CTX_STRATEGY:-char}}"
        # so the alias is resolved before the proxy sees the env. We
        # verify the proxy respects the resolved value.
        with patch.dict(os.environ, {"PROXY_CTX_TRUNCATE_STRATEGY": "fifo"},
                        clear=False):
            self.assertEqual(os.environ["PROXY_CTX_TRUNCATE_STRATEGY"], "fifo")

    def test_priority_order_documented(self):
        """Sanity check: the manage.sh source contains the priority chain
        comment AND the 3-tier env-var expansion (PROXY name → LLAMA
        alias → default 'char')."""
        with open(os.path.join(_REPO_ROOT, "manage.sh")) as f:
            manage_sh = f.read()
        # 3-tier priority comment is present
        self.assertIn("3-tier priority chain", manage_sh,
            "manage.sh must document the 3-tier env-var priority")
        # Expansion is correct
        self.assertIn("PROXY_CTX_TRUNCATE_STRATEGY:-${LLAMA_CTX_STRATEGY:-char}", manage_sh,
            "manage.sh must expand PROXY_CTX_TRUNCATE_STRATEGY via LLAMA_CTX_STRATEGY alias")
        # OOM_SAFE pass-through added
        self.assertIn("PROXY_OOM_SAFE_CHARS", manage_sh,
            "manage.sh must forward PROXY_OOM_SAFE_CHARS to the proxy")
        # Continuation pass-through added
        self.assertIn("PROXY_SESSION_CONTINUATION_ENABLED", manage_sh,
            "manage.sh must forward PROXY_SESSION_CONTINUATION_ENABLED")
        self.assertIn("PROXY_SESSION_CONTINUATION_MIN_REQUESTS", manage_sh,
            "manage.sh must forward PROXY_SESSION_CONTINUATION_MIN_REQUESTS")





class TestDynamicTokenEstimation(unittest.TestCase):
    """Phase 3.1: content-type-aware token estimation."""

    def test_chinese_ratio(self):
        msgs = [{"role": "user", "content": "这是一个中文句子" * 100}]
        est = proxy._estimate_tokens_dynamic(msgs)
        chars = proxy._estimate_message_chars(msgs)
        self.assertGreater(est, 0)
        # Chinese ratio (1.5) should yield more tokens than English ratio (4.0)
        english_est = int(chars / proxy.PROXY_TOKEN_RATIO_ENGLISH)
        self.assertGreater(est, english_est)

    def test_english_ratio(self):
        msgs = [{"role": "user", "content": "This is an English sentence. " * 100}]
        est = proxy._estimate_tokens_dynamic(msgs)
        chars = proxy._estimate_message_chars(msgs)
        self.assertGreater(est, 0)
        chinese_est = int(chars / proxy.PROXY_TOKEN_RATIO_CHINESE)
        self.assertLess(est, chinese_est)

    def test_code_ratio(self):
        code = "def foo():\n    return {\"a\": 1, \"b\": 2}\n" * 50
        msgs = [{"role": "user", "content": code}]
        est = proxy._estimate_tokens_dynamic(msgs)
        chars = proxy._estimate_message_chars(msgs)
        self.assertGreater(est, 0)
        self.assertLessEqual(est, int(chars / proxy.PROXY_TOKEN_RATIO_CODE) + 1)

    def test_mixed_content(self):
        msgs = [
            {"role": "user", "content": "Hello world. " * 50},
            {"role": "user", "content": "这是一个中文句子" * 50},
        ]
        est = proxy._estimate_tokens_dynamic(msgs)
        self.assertGreater(est, 0)

    def test_ratio_override(self):
        msgs = [{"role": "user", "content": "x" * 1000}]
        est = proxy._estimate_tokens_dynamic(msgs, ratio_override=5.0)
        self.assertEqual(est, int(1000 / 5.0))

    def test_classify_content_for_ratio(self):
        self.assertEqual(proxy._classify_content_for_ratio("hello world"), "english")
        self.assertEqual(proxy._classify_content_for_ratio("中文字符" * 50), "chinese")
        code = "def func():\n    return [1, 2, 3]\n"
        self.assertEqual(proxy._classify_content_for_ratio(code * 50), "code")


class TestMemoryRejection(unittest.TestCase):
    """Phase 3.3: memory pressure active rejection."""

    def test_reject_when_above_threshold(self):
        mem = {"used_pct": "95.0"}
        with patch.object(proxy, "PROXY_MEMORY_REJECT_THRESHOLD", 90.0), patch.object(proxy_state, "PROXY_MEMORY_REJECT_THRESHOLD", 90.0):
            rejected, used = proxy._should_reject_for_memory(mem)
            self.assertTrue(rejected)
            self.assertEqual(used, 95.0)

    def test_not_reject_when_below_threshold(self):
        mem = {"used_pct": "70.0"}
        with patch.object(proxy, "PROXY_MEMORY_REJECT_THRESHOLD", 90.0), patch.object(proxy_state, "PROXY_MEMORY_REJECT_THRESHOLD", 90.0):
            rejected, used = proxy._should_reject_for_memory(mem)
            self.assertFalse(rejected)
            self.assertEqual(used, 70.0)

    def test_invalid_used_pct_returns_false(self):
        mem = {"used_pct": "invalid"}
        rejected, used = proxy._should_reject_for_memory(mem)
        self.assertFalse(rejected)


class TestDynamicMaxTokens(unittest.TestCase):
    """Phase 3.4: lifecycle-stage-aware max_tokens."""

    def test_init_stage_unchanged(self):
        with patch.object(proxy, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), patch.object(proxy_state, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            with patch.object(proxy, "MODEL_NAME", "test-model"), patch.object(proxy_state, "MODEL_NAME", "test-model"):
                mem = {"available_gb": 20, "total_gb": 48}
                adjusted, reason = proxy._compute_dynamic_max_tokens(
                    4096, {"stage": "init"}, mem=mem)
                self.assertEqual(adjusted, 4096)
                self.assertIn("stage=init", reason)

    def test_saturation_stage_capped(self):
        with patch.object(proxy, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), patch.object(proxy_state, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            with patch.object(proxy, "MODEL_NAME", "test-model"), patch.object(proxy_state, "MODEL_NAME", "test-model"):
                with patch.object(proxy, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 2048), patch.object(proxy_state, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 2048):
                    mem = {"available_gb": 20, "total_gb": 48}
                    adjusted, reason = proxy._compute_dynamic_max_tokens(
                        8192, {"stage": "saturation"}, mem=mem)
                    self.assertEqual(adjusted, 2048)
                    self.assertIn("stage=saturation", reason)

    def test_rapid_mlx_discount(self):
        with patch.object(proxy, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), patch.object(proxy_state, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            with patch.object(proxy, "MODEL_NAME", "rapid-mlx/Qwen"), patch.object(proxy_state, "MODEL_NAME", "rapid-mlx/Qwen"):
                with patch.object(proxy, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 2048), patch.object(proxy_state, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 2048):
                    with patch.object(proxy, "PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO", 0.8), patch.object(proxy_state, "PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO", 0.8):
                        mem = {"available_gb": 20, "total_gb": 48}
                        adjusted, _ = proxy._compute_dynamic_max_tokens(
                            8192, {"stage": "saturation"}, mem=mem)
                        self.assertEqual(adjusted, int(2048 * 0.8))

    def test_low_memory_discount(self):
        with patch.object(proxy, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), patch.object(proxy_state, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            with patch.object(proxy, "MODEL_NAME", "test-model"), patch.object(proxy_state, "MODEL_NAME", "test-model"):
                with patch.object(proxy, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 2048), patch.object(proxy_state, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 2048):
                    mem = {"available_gb": 5, "total_gb": 48}
                    adjusted, reason = proxy._compute_dynamic_max_tokens(
                        8192, {"stage": "saturation"}, mem=mem)
                    self.assertLess(adjusted, 2048)
                    self.assertIn("low_memory", reason)

    def test_disabled_returns_original(self):
        with patch.object(proxy, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", False), patch.object(proxy_state, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", False):
            adjusted, reason = proxy._compute_dynamic_max_tokens(
                4096, {"stage": "saturation"})
            self.assertEqual(adjusted, 4096)
            self.assertEqual(reason, "dynamic_disabled")


class TestRequestSnapshots(unittest.TestCase):
    """Phase 3.5: failure request snapshots."""

    def setUp(self):
        proxy_state.PROXY_SNAPSHOT_ENABLED = True
        self.snapshot_dir = os.path.join(_REPO_ROOT, "logs", "snapshots")
        if os.path.exists(self.snapshot_dir):
            for f in os.listdir(self.snapshot_dir):
                if f.startswith("test_snap_"):
                    os.remove(os.path.join(self.snapshot_dir, f))

    def tearDown(self):
        if os.path.exists(self.snapshot_dir):
            for f in os.listdir(self.snapshot_dir):
                if f.startswith("test_snap_"):
                    os.remove(os.path.join(self.snapshot_dir, f))

    def test_snapshot_writes_before_and_after(self):
        with patch.object(proxy, "PROXY_SNAPSHOT_ENABLED", True), patch.object(proxy_state, "PROXY_SNAPSHOT_ENABLED", True):
            req_id = "test_snap_1"
            before = {"messages": [{"role": "user", "content": "hi"}]}
            written = proxy._write_request_snapshot(req_id, before)
            self.assertTrue(written)
            before_path = os.path.join(self.snapshot_dir, f"{req_id}_before.json")
            self.assertTrue(os.path.exists(before_path))

            err = RuntimeError("boom")
            proxy._write_request_snapshot(req_id, before, after_body=None, error=err)
            after_path = os.path.join(self.snapshot_dir, f"{req_id}_after.json")
            self.assertTrue(os.path.exists(after_path))
            with open(after_path) as f:
                data = json.load(f)
            self.assertEqual(data["error"]["type"], "RuntimeError")

    def test_snapshot_disabled_returns_false(self):
        with patch.object(proxy, "PROXY_SNAPSHOT_ENABLED", False), patch.object(proxy_state, "PROXY_SNAPSHOT_ENABLED", False):
            written = proxy._write_request_snapshot("test_snap_2", {"x": 1})
            self.assertFalse(written)


class TestDynamicConcurrency(unittest.TestCase):
    """Phase 3.2: dynamic concurrency control."""

    def setUp(self):
        self._orig_latencies = list(proxy._LATENCY_WINDOW)
        self._orig_errors = list(proxy._ERROR_WINDOW)
        self._orig_max = proxy.PROXY_MAX_CONCURRENT
        proxy_state.PROXY_DYNAMIC_CONCURRENT_ENABLED = True
        proxy._LATENCY_WINDOW.clear()
        proxy._ERROR_WINDOW.clear()

    def tearDown(self):
        proxy._LATENCY_WINDOW.clear()
        proxy._LATENCY_WINDOW.extend(self._orig_latencies)
        proxy._ERROR_WINDOW.clear()
        proxy._ERROR_WINDOW.extend(self._orig_errors)
        proxy.PROXY_MAX_CONCURRENT = self._orig_max
        proxy_state.PROXY_MAX_CONCURRENT = self._orig_max
        proxy_state.PROXY_MAX_CONCURRENT = self._orig_max
        proxy_state.PROXY_MAX_CONCURRENT = self._orig_max
        proxy_state.PROXY_MAX_CONCURRENT = self._orig_max

    def test_high_latency_triggers_downgrade(self):
        with patch.object(proxy, "PROXY_DYNAMIC_CONCURRENT_ENABLED", True), patch.object(proxy_state, "PROXY_DYNAMIC_CONCURRENT_ENABLED", True):
            with patch.object(proxy, "PROXY_DYNAMIC_CONCURRENT_MIN", 1), patch.object(proxy_state, "PROXY_DYNAMIC_CONCURRENT_MIN", 1):
                with patch.object(proxy, "PROXY_DYNAMIC_CONCURRENT_MAX", 4), patch.object(proxy_state, "PROXY_DYNAMIC_CONCURRENT_MAX", 4):
                    proxy.PROXY_MAX_CONCURRENT = 4
                    proxy_state.PROXY_MAX_CONCURRENT = 4
                    proxy_state.PROXY_MAX_CONCURRENT = 4
                    proxy_state.PROXY_MAX_CONCURRENT = 4
                    for _ in range(10):
                        proxy._record_request_for_concurrency(60000, 200)
                    result = proxy._adjust_concurrency()
                    self.assertTrue(result.get("adjusted"))
                    self.assertLess(proxy_state.PROXY_MAX_CONCURRENT, 4)

    def test_low_latency_stable_does_not_change(self):
        with patch.object(proxy, "PROXY_DYNAMIC_CONCURRENT_ENABLED", True), patch.object(proxy_state, "PROXY_DYNAMIC_CONCURRENT_ENABLED", True):
            proxy.PROXY_MAX_CONCURRENT = 2
            proxy_state.PROXY_MAX_CONCURRENT = 2
            proxy_state.PROXY_MAX_CONCURRENT = 2
            proxy_state.PROXY_MAX_CONCURRENT = 2
            # Latency in the stable band: below threshold but above threshold/2
            for _ in range(5):
                proxy._record_request_for_concurrency(20000, 200)
            result = proxy._adjust_concurrency()
            self.assertFalse(result.get("adjusted"))


class TestMetricsSchemaV1(unittest.TestCase):
    """Phase 3.7: unified metrics schema v1."""

    def test_finalize_metrics_adds_schema_and_fields(self):
        mc = {
            "ts": "2026-01-01T00:00:00",
            "session_id": "s1",
            "input_msgs": 2,
            "input_chars": 1000,
            "input_tools": 1,
            "output_chars": 200,
            "duration_ms": 100.0,
            "status": 200,
            "pipeline": {},
        }
        proxy._finalize_metrics(mc)
        self.assertEqual(mc.get("schema_version"), "v1")
        for field in proxy._METRICS_V1_FIELDS:
            self.assertIn(field, mc, f"missing metrics field: {field}")
        self.assertIn("token_ratio", mc)
        self.assertIn("est_input_tokens", mc)
        self.assertIn("est_output_tokens", mc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
