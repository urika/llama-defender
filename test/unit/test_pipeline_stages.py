"""Unit tests for individual PipelineStage classes.

Each stage is tested in isolation by constructing a PipelineContext with
known inputs, running the stage, and asserting on the output context fields.
Existing functions (lifecycle.py, loop_detection.py, etc.) are called
through the stages — their own unit tests already cover edge cases.
"""
import io
import json
import threading
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

import proxy_state as _ps
from pipeline import (
    PipelineContext,
    RequestParser,
    LifecycleClassifier,
    DynamicMaxTokens,
    ErrorTranslator,
    BlockerDetector,
    SystemNormalizer,
    CacheAligner,
    ContentCompressor,
    _char_bucket,
    ToolLoopDetector,
    TextLoopDetector,
    SessionLoopState,
    LoopIntervention,
    RereadDetector,
    DateNormalizer,
    ContextTruncator,
    HighDropRatioNotice,
    MessageHashDebug,
    OOMSafetyFIFO,
    PrefixRatioComputer,
    ToolPairingRepair,
    FormatConverter,
    BackendDispatcher,
)


# ===========================================================================
# RequestParser — stage 0
# ===========================================================================

class TestRequestParser(unittest.TestCase):
    def setUp(self):
        self.body = {
            "model": "claude-sonnet-4-6",
            "stream": True,
            "max_tokens": 8192,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ],
            "tools": [
                {"name": "Read", "description": "Read files"},
                {"name": "Bash", "description": "Run commands"},
            ],
        }
        _ps._log_ctx.session_id = "sess_test_123"

    def test_extracts_basic_fields(self):
        ctx = RequestParser().process(PipelineContext(body=self.body))
        self.assertEqual(ctx.model, "claude-sonnet-4-6")
        self.assertTrue(ctx.is_stream)
        self.assertEqual(ctx.max_tokens_orig, 8192)

    def test_extracts_tools_list(self):
        ctx = RequestParser().process(PipelineContext(body=self.body))
        self.assertEqual(ctx.tools_list, ["Read", "Bash"])
        self.assertEqual(ctx.raw_tools_orig, self.body["tools"])

    def test_extracts_session_id(self):
        ctx = RequestParser().process(PipelineContext(body=self.body))
        self.assertEqual(ctx.session_id, "sess_test_123")

    def test_computes_total_chars(self):
        ctx = RequestParser().process(PipelineContext(body=self.body))
        self.assertGreater(ctx.total_chars, 0)

    def test_initializes_messages(self):
        ctx = RequestParser().process(PipelineContext(body=self.body))
        self.assertEqual(len(ctx.messages), 2)

    def test_no_tools(self):
        body = {"model": "x", "messages": [], "stream": False}
        ctx = RequestParser().process(PipelineContext(body=body))
        self.assertEqual(ctx.tools_list, [])
        self.assertFalse(ctx.is_stream)

    def test_defaults(self):
        ctx = RequestParser().process(PipelineContext(body={"messages": []}))
        self.assertEqual(ctx.model, "unknown")
        self.assertEqual(ctx.max_tokens_orig, 4096)
        self.assertFalse(ctx.is_stream)

    def test_output_metrics(self):
        ctx = RequestParser().process(PipelineContext(body=self.body))
        metrics = RequestParser().output_metrics(ctx)
        self.assertEqual(metrics["msg_count"], 2)
        self.assertEqual(metrics["tool_count"], 2)
        self.assertGreater(metrics["input_chars"], 0)
        self.assertEqual(metrics["is_stream"], 1)


# ===========================================================================
# LifecycleClassifier — stage 1
# ===========================================================================

class TestLifecycleClassifier(unittest.TestCase):
    def setUp(self):
        _ps._SESSION_REQUEST_COUNT.clear()

    def test_classifies_small_context_as_init(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = PipelineContext(messages=messages, session_id="s1")
        ctx = LifecycleClassifier().process(ctx)
        self.assertIsNotNone(ctx.stage_config)
        self.assertIn("stage", ctx.stage_config)
        self.assertIn("frozen_head", ctx.stage_config)
        self.assertIn("thinking_keep", ctx.stage_config)

    def test_increments_session_request_count(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = PipelineContext(messages=messages, session_id="s2")
        LifecycleClassifier().process(ctx)
        self.assertIn("s2", _ps._SESSION_REQUEST_COUNT)

    def test_output_metrics(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = PipelineContext(messages=messages, session_id="s3")
        ctx = LifecycleClassifier().process(ctx)
        with patch.object(_ps, "PROXY_METRICS_ENABLED", True):
            metrics = LifecycleClassifier().output_metrics(ctx)
        self.assertIsNotNone(metrics)
        self.assertIn("stage", metrics)


# ===========================================================================
# DynamicMaxTokens — stage 2
# ===========================================================================

class TestDynamicMaxTokens(unittest.TestCase):
    def setUp(self):
        self.stage = DynamicMaxTokens()
        self.stage_config = {"stage": "growth", "total_chars": 50000}

    @patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True)
    def test_should_run_when_enabled(self):
        self.assertTrue(self.stage.should_run(PipelineContext()))

    @patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", False)
    @patch.object(_ps, "PROXY_MAX_TOKENS_OVERRIDE", 0)
    def test_should_not_run_when_disabled_and_no_override(self):
        self.assertFalse(self.stage.should_run(PipelineContext()))

    @patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", False)
    @patch.object(_ps, "PROXY_MAX_TOKENS_OVERRIDE", 4096)
    def test_should_run_when_override_present(self):
        self.assertTrue(self.stage.should_run(PipelineContext()))

    @patch.object(_ps, "PROXY_MAX_TOKENS_OVERRIDE", 100)
    def test_hard_override_applied(self):
        ctx = PipelineContext(
            body={"max_tokens": 8192},
            max_tokens_orig=8192,
            stage_config=self.stage_config,
        )
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", False):
            ctx = self.stage.process(ctx)
        self.assertEqual(ctx.body["max_tokens"], 100)


# ===========================================================================
# ErrorTranslator — stage 3
# ===========================================================================

class TestErrorTranslator(unittest.TestCase):
    def test_translates_wasted_call_error(self):
        messages = [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1",
                         "content": "Wasted call: file has not changed since last read"}]
        }]
        ctx = PipelineContext(messages=messages)
        ctx = ErrorTranslator().process(ctx)
        self.assertIsNotNone(ctx.error_count)
        self.assertGreater(ctx.error_count.get("wasted", 0), 0)

    def test_no_error_count_zero(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "ok"}]}]
        ctx = PipelineContext(messages=messages)
        ctx = ErrorTranslator().process(ctx)
        total = sum(ctx.error_count.values())
        self.assertEqual(total, 0)

    def test_output_metrics_with_errors(self):
        messages = [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1",
                         "content": "Wasted call: file has not changed since last read"}]
        }]
        ctx = PipelineContext(messages=messages)
        ctx = ErrorTranslator().process(ctx)
        metrics = ErrorTranslator().output_metrics(ctx)
        self.assertIsNotNone(metrics)
        self.assertGreater(metrics["count"], 0)

    def test_output_metrics_without_errors(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "ok"}]}]
        ctx = PipelineContext(messages=messages)
        ctx = ErrorTranslator().process(ctx)
        metrics = ErrorTranslator().output_metrics(ctx)
        # error_count is always a dict (keys: wasted, file_not_found, input_validation)
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["count"], 0)


# ===========================================================================
# BlockerDetector — stage 4
# ===========================================================================

class TestBlockerDetector(unittest.TestCase):
    def setUp(self):
        self.stage = BlockerDetector()

    @patch.object(_ps, "PROXY_BLOCKER_ENABLED", True)
    def test_should_run_when_enabled(self):
        self.assertTrue(self.stage.should_run(PipelineContext()))

    @patch.object(_ps, "PROXY_BLOCKER_ENABLED", False)
    def test_should_not_run_when_disabled(self):
        self.assertFalse(self.stage.should_run(PipelineContext()))

    @patch.object(_ps, "PROXY_BLOCKER_ENABLED", True)
    def test_no_blocker_on_normal_messages(self):
        messages = [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]
        ctx = PipelineContext(messages=messages)
        ctx = self.stage.process(ctx)
        self.assertIsNotNone(ctx.blocker_info)
        self.assertFalse(ctx.blocker_info.get("triggered", False))

    @patch.object(_ps, "PROXY_BLOCKER_ENABLED", True)
    def test_detects_blocker_on_two_consecutive_same_errors(self):
        # Blocker detection runs AFTER error translation.  The error-translation
        # pass rewrites "File does not exist" → a Chinese system message, and
        # _detect_blocker_pattern checks markers in the *translated* content.
        # Simulate that by using the translated form directly.
        translated = "[System: 文件不存在。请先用 Bash ls 或 find 命令确认项目结构，然后使用正确的文件路径。]"
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "id": "t1", "input": {"file_path": "/f"}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": translated}
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "id": "t2", "input": {"file_path": "/f"}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": translated}
            ]},
        ]
        ctx = PipelineContext(messages=messages)
        ctx = self.stage.process(ctx)
        self.assertTrue(ctx.blocker_info.get("triggered"))
        self.assertTrue(any("BLOCKER" in str(m) for m in ctx.messages))


# ===========================================================================
# SystemNormalizer — stage 5
# ===========================================================================

class TestSystemNormalizer(unittest.TestCase):
    def test_converts_second_system_to_user(self):
        messages = [
            {"role": "system", "content": [{"type": "text", "text": "You are a helpful assistant"}]},
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "system", "content": [{"type": "text", "text": "system reminder"}]},
        ]
        ctx = PipelineContext(messages=messages)
        ctx = SystemNormalizer().process(ctx)
        roles = [m["role"] for m in ctx.messages]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles[2], "user")


# ===========================================================================
# CacheAligner — stage 6
# ===========================================================================

class TestCacheAligner(unittest.TestCase):
    @patch.object(_ps, "PROXY_CACHE_ALIGN_ENABLED", True)
    @patch.object(_ps, "PROXY_CACHE_ALIGN_HEAD", 2)
    def test_splits_messages_at_align_head(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        ctx = PipelineContext(messages=messages)
        ctx = CacheAligner().process(ctx)
        self.assertEqual(len(ctx._cache_prefix), 2)
        self.assertEqual(len(ctx._cache_dynamic), 2)
        self.assertEqual(ctx.messages, ctx._cache_dynamic)

    @patch.object(_ps, "PROXY_CACHE_ALIGN_ENABLED", False)
    def test_no_split_when_disabled(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = PipelineContext(messages=messages)
        ctx = CacheAligner().process(ctx)
        self.assertEqual(ctx._cache_prefix, [])
        self.assertEqual(ctx._cache_dynamic, messages)


# ===========================================================================
# ContentCompressor — stage 7
# ===========================================================================

class TestContentCompressor(unittest.TestCase):
    def test_reassembles_prefix_and_dynamic(self):
        prefix = [{"role": "system", "content": "sys"}]
        dynamic = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]
        ctx = PipelineContext(
            messages=dynamic,
            _cache_prefix=prefix,
            _cache_dynamic=dynamic,
            stage_config={"stage": "init", "frozen_head": 0, "clear_zone_pct": None,
                          "thinking_keep": 0, "truncate_rounds": None, "oom_safety": False},
            tools_list=["Read"],
        )
        ctx = ContentCompressor().process(ctx)
        self.assertEqual(ctx.messages[0], prefix[0])

    def test_output_metrics(self):
        prefix = []
        dynamic = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        ctx = PipelineContext(
            messages=dynamic, _cache_prefix=prefix, _cache_dynamic=dynamic,
            stage_config={"stage": "init", "frozen_head": 0, "clear_zone_pct": None,
                          "thinking_keep": 0, "truncate_rounds": None, "oom_safety": False},
        )
        ctx = ContentCompressor().process(ctx)
        with patch.object(_ps, "PROXY_METRICS_ENABLED", True):
            metrics = ContentCompressor().output_metrics(ctx)
        self.assertIsNotNone(metrics)
        self.assertIn("compression", metrics)
        comp = metrics["compression"]
        self.assertIn("strategy", comp)
        self.assertIn("ratio", comp)
        self.assertIn("dropped", comp)


# ===========================================================================
# ToolLoopDetector — stage 8
# ===========================================================================

class TestToolLoopDetector(unittest.TestCase):
    def test_no_loop_on_single_call(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "id": "t1",
                 "input": {"file_path": "/f"}}
            ]}
        ]
        ctx = PipelineContext(messages=messages)
        ctx = ToolLoopDetector().process(ctx)
        self.assertLess(ctx.max_run, 2)

    def test_detects_repeated_tool_call(self):
        messages = []
        for i in range(5):
            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "id": f"t{i}",
                 "input": {"file_path": "/same_file"}}
            ]})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": "content"}
            ]})
        ctx = PipelineContext(messages=messages)
        ctx = ToolLoopDetector().process(ctx)
        self.assertGreaterEqual(ctx.max_run, 5)
        self.assertTrue(any(k.startswith("Read:") for k in ctx.consecutive))

    def test_write_edit_uses_file_key(self):
        messages = []
        for i in range(3):
            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "name": "Write", "id": f"t{i}",
                 "input": {"file_path": "/same", "content": f"v{i}"}}
            ]})
        ctx = PipelineContext(messages=messages)
        ctx = ToolLoopDetector().process(ctx)
        self.assertGreaterEqual(ctx.max_run, 3)


# ===========================================================================
# TextLoopDetector — stage 9
# ===========================================================================

class TestTextLoopDetector(unittest.TestCase):
    @patch.object(_ps, "PROXY_TEXT_LOOP_ENABLED", True)
    def test_merges_with_tool_loop_max(self):
        messages = [{"role": "assistant", "content": [{"type": "text", "text": "unique text"}]}]
        ctx = PipelineContext(messages=messages, max_run=5)
        ctx = TextLoopDetector().process(ctx)
        self.assertEqual(ctx.max_run, 5)

    @patch.object(_ps, "PROXY_TEXT_LOOP_ENABLED", False)
    def test_skips_when_disabled(self):
        self.assertFalse(TextLoopDetector().should_run(PipelineContext()))

    @patch.object(_ps, "PROXY_TEXT_LOOP_ENABLED", True)
    def test_sets_text_loop_fields(self):
        messages = [{"role": "assistant", "content": [{"type": "text", "text": "a"}]}]
        ctx = PipelineContext(messages=messages)
        ctx = TextLoopDetector().process(ctx)
        self.assertIsInstance(ctx.is_text_loop, bool)
        self.assertIsInstance(ctx.text_loop_run, int)


# ===========================================================================
# SessionLoopState — stage 10
# ===========================================================================

class TestSessionLoopState(unittest.TestCase):
    def setUp(self):
        _ps._LOOP_SESSION_STATE.clear()

    def tearDown(self):
        _ps._LOOP_SESSION_STATE.clear()

    def test_no_injection_when_no_prior_loop(self):
        ctx = PipelineContext(messages=[], session_id="s_noloop", max_run=1)
        ctx = SessionLoopState().process(ctx)
        self.assertEqual(len(ctx.messages), 0)

    def test_injects_warning_when_prior_level2(self):
        _ps._LOOP_SESSION_STATE["s_wasloop"] = {"level": 2, "triggers": 3}
        ctx = PipelineContext(messages=[], session_id="s_wasloop", max_run=1)
        ctx = SessionLoopState().process(ctx)
        self.assertGreater(len(ctx.messages), 0)
        self.assertIn("previously looping", str(ctx.messages[0]))


# ===========================================================================
# LoopIntervention — stage 11
# ===========================================================================

class TestLoopIntervention(unittest.TestCase):
    def setUp(self):
        _ps._LOOP_SESSION_STATE.clear()

    def tearDown(self):
        _ps._LOOP_SESSION_STATE.clear()

    def test_no_intervention_below_threshold(self):
        ctx = PipelineContext(messages=[{"role": "user", "content": "hi"}], max_run=1,
                            consecutive={}, is_text_loop=False, text_loop_run=0)
        ctx = LoopIntervention().process(ctx)
        self.assertEqual(ctx.loop_level, 0)
        self.assertIsNone(ctx.loop_tool_name)

    @patch.object(_ps, "PROXY_LOOP_THRESHOLD", 3)
    def test_level1_hint_injected(self):
        ctx = PipelineContext(
            messages=[{"role": "user", "content": "hi"}],
            body={"tools": []},
            max_run=4,
            consecutive={"Read:file=/f": 4},
            is_text_loop=False, text_loop_run=0, session_id="s1",
        )
        ctx = LoopIntervention().process(ctx)
        self.assertGreaterEqual(ctx.loop_level, 1)

    @patch.object(_ps, "PROXY_LOOP_THRESHOLD", 3)
    @patch.object(_ps, "PROXY_LOOP_LEVEL2", 6)
    def test_level2_tool_removed(self):
        tools = [{"name": "Read"}, {"name": "Bash"}]
        body = {"tools": tools}
        ctx = PipelineContext(
            messages=[{"role": "user", "content": "hi"}],
            body=body, max_run=6,
            consecutive={"Read:file=/f": 6},
            is_text_loop=False, text_loop_run=0, session_id="s2",
        )
        ctx = LoopIntervention().process(ctx)
        self.assertGreaterEqual(ctx.loop_level, 2)
        self.assertLess(len(ctx.body.get("tools", [])), len(tools))

    @patch.object(_ps, "PROXY_LOOP_THRESHOLD", 3)
    @patch.object(_ps, "PROXY_LOOP_LEVEL2", 6)
    @patch.object(_ps, "PROXY_LOOP_LEVEL3", 9)
    def test_level3_all_tools_stripped(self):
        tools = [{"name": "Read"}, {"name": "Bash"}]
        body = {"tools": tools}
        ctx = PipelineContext(
            messages=[{"role": "user", "content": "hi"}],
            body=body, max_run=9,
            consecutive={"Read:file=/f": 9},
            is_text_loop=False, text_loop_run=0, session_id="s3",
        )
        ctx = LoopIntervention().process(ctx)
        self.assertEqual(ctx.loop_level, 3)
        self.assertEqual(ctx.body.get("tools"), [])

    def test_output_metrics_always_returned(self):
        ctx = PipelineContext(messages=[], max_run=1)
        ctx = LoopIntervention().process(ctx)
        metrics = LoopIntervention().output_metrics(ctx)
        self.assertIsNotNone(metrics)
        self.assertIn("max_run", metrics)
        self.assertIn("level", metrics)


# ===========================================================================
# RereadDetector — stage 12
# ===========================================================================

class TestRereadDetector(unittest.TestCase):
    def test_no_detection_without_cleared_files(self):
        ctx = PipelineContext(messages=[], cleared_files=[])
        ctx = RereadDetector().process(ctx)
        self.assertEqual(ctx.re_read_info["count"], 0)

    def test_detects_read_on_cleared_file(self):
        cleared = ["/tmp/old_file.txt"]
        messages = [{
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Read", "id": "t1",
                         "input": {"file_path": "/tmp/old_file.txt"}}]
        }]
        ctx = PipelineContext(messages=messages, cleared_files=cleared)
        ctx = RereadDetector().process(ctx)
        self.assertGreater(ctx.re_read_info["count"], 0)
        self.assertTrue(any("HARD BLOCK" in str(m) for m in ctx.messages))

    def test_no_detection_on_different_file(self):
        cleared = ["/tmp/old_file.txt"]
        messages = [{
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Read", "id": "t1",
                         "input": {"file_path": "/tmp/other.txt"}}]
        }]
        ctx = PipelineContext(messages=messages, cleared_files=cleared)
        ctx = RereadDetector().process(ctx)
        self.assertEqual(ctx.re_read_info["count"], 0)

    def test_no_detection_on_non_read_tool(self):
        cleared = ["/tmp/old_file.txt"]
        messages = [{
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "id": "t1",
                         "input": {"command": "cat /tmp/old_file.txt"}}]
        }]
        ctx = PipelineContext(messages=messages, cleared_files=cleared)
        ctx = RereadDetector().process(ctx)
        self.assertEqual(ctx.re_read_info["count"], 0)


# ===========================================================================
# DateNormalizer — stage 13
# ===========================================================================

class TestDateNormalizer(unittest.TestCase):
    def test_normalizes_date_in_list_content(self):
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": "Today's date is 2026/06/21. Do something."}]
        }]
        ctx = PipelineContext(messages=messages)
        ctx = DateNormalizer().process(ctx)
        text = ctx.messages[0]["content"][0]["text"]
        self.assertIn("DATE_PLACEHOLDER", text)
        self.assertNotIn("2026/06/21", text)

    def test_normalizes_date_in_string_content(self):
        messages = [{
            "role": "user",
            "content": "Today's date is 2026/01/15. Hello."
        }]
        ctx = PipelineContext(messages=messages)
        ctx = DateNormalizer().process(ctx)
        self.assertIn("DATE_PLACEHOLDER", ctx.messages[0]["content"])

    def test_no_change_when_no_date(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "Hello world."}]}]
        ctx = PipelineContext(messages=messages)
        ctx = DateNormalizer().process(ctx)
        self.assertEqual(ctx.messages[0]["content"][0]["text"], "Hello world.")

    def test_skips_non_user_first_message(self):
        messages = [{"role": "system", "content": "Today's date is 2026/06/21."}]
        ctx = PipelineContext(messages=messages)
        result = DateNormalizer().process(ctx)
        self.assertIn("2026/06/21", str(result.messages[0]))


# ===========================================================================
# ContextTruncator — stage 14
# ===========================================================================

class TestContextTruncator(unittest.TestCase):
    @patch.object(_ps, "PROXY_CTX_LIMIT_ENABLED", True)
    def test_should_run_when_enabled(self):
        self.assertTrue(ContextTruncator().should_run(PipelineContext()))

    @patch.object(_ps, "PROXY_CTX_LIMIT_ENABLED", False)
    def test_should_not_run_when_disabled(self):
        self.assertFalse(ContextTruncator().should_run(PipelineContext()))

    @patch.object(_ps, "PROXY_CTX_LIMIT_ENABLED", True)
    def test_sets_trunc_stats(self):
        messages = [{"role": "user", "content": "hi"}] * 5
        ctx = PipelineContext(messages=messages, session_id="s1",
                            stage_config={"truncate_rounds": None})
        ctx = ContextTruncator().process(ctx)
        self.assertIsNotNone(ctx.trunc_stats)

    @patch.object(_ps, "PROXY_CTX_LIMIT_ENABLED", True)
    def test_output_metrics_when_not_truncated(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = PipelineContext(messages=messages, session_id="s1",
                            stage_config={"truncate_rounds": None})
        ctx = ContextTruncator().process(ctx)
        metrics = ContextTruncator().output_metrics(ctx)
        self.assertIsNotNone(metrics)


# ===========================================================================
# HighDropRatioNotice — stage 15
# ===========================================================================

class TestHighDropRatioNotice(unittest.TestCase):
    def test_injects_notice_when_high_drop_ratio(self):
        ctx = PipelineContext(
            messages=[{"role": "user", "content": "kept"}],
            trunc_stats={"truncated": True, "dropped_messages": 90, "kept_messages": 5},
        )
        ctx = HighDropRatioNotice().process(ctx)
        self.assertTrue(ctx.high_drop_notice_injected)
        self.assertTrue(any("severely truncated" in str(m) for m in ctx.messages))

    def test_no_notice_when_low_drop_ratio(self):
        ctx = PipelineContext(
            messages=[{"role": "user", "content": "kept"}],
            trunc_stats={"truncated": True, "dropped_messages": 1, "kept_messages": 10},
        )
        ctx = HighDropRatioNotice().process(ctx)
        self.assertFalse(ctx.high_drop_notice_injected)

    def test_no_notice_when_not_truncated(self):
        ctx = PipelineContext(
            messages=[{"role": "user", "content": "kept"}],
            trunc_stats={"truncated": False},
        )
        ctx = HighDropRatioNotice().process(ctx)
        self.assertFalse(ctx.high_drop_notice_injected)


# ===========================================================================
# MessageHashDebug — stage 16
# ===========================================================================

class TestMessageHashDebug(unittest.TestCase):
    def test_hashes_messages_without_crashing(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "world"}]},
        ]
        ctx = PipelineContext(messages=messages)
        result = MessageHashDebug().process(ctx)
        self.assertEqual(result.messages, messages)

    def test_empty_messages_no_crash(self):
        ctx = PipelineContext(messages=[])
        result = MessageHashDebug().process(ctx)
        self.assertEqual(result.messages, [])


# ===========================================================================
# OOMSafetyFIFO — stage 17
# ===========================================================================

class TestOOMSafetyFIFO(unittest.TestCase):
    @patch.object(_ps, "IS_CLOUD", False)
    @patch.object(_ps, "PROXY_CTX_TRUNCATE_STRATEGY", "char")
    def test_should_run_when_oom_safety_true(self):
        ctx = PipelineContext(stage_config={"oom_safety": True})
        self.assertTrue(OOMSafetyFIFO().should_run(ctx))

    @patch.object(_ps, "IS_CLOUD", True)
    def test_should_not_run_when_cloud(self):
        ctx = PipelineContext(stage_config={"oom_safety": True})
        self.assertFalse(OOMSafetyFIFO().should_run(ctx))

    def test_should_not_run_when_no_stage_config(self):
        ctx = PipelineContext()
        self.assertFalse(OOMSafetyFIFO().should_run(ctx))

    @patch.object(_ps, "IS_CLOUD", False)
    @patch.object(_ps, "PROXY_CTX_TRUNCATE_STRATEGY", "char")
    @patch.object(_ps, "PROXY_CHARS_OOM_DANGER", 100)
    @patch.object(_ps, "PROXY_OOM_SAFE_TOKENS", 10)
    @patch.object(_ps, "PROXY_CTX_KEEP_HEAD", 1)
    @patch.object(_ps, "PROXY_CTX_KEEP_TAIL", 1)
    @patch.object(_ps, "PROXY_CTX_TOKEN_RATIO", 3.5)
    def test_drops_messages_when_exceeding_limit(self):
        ctx = PipelineContext(
            messages=[{"role": "user", "content": "x" * 200}] * 20,
            body={},
            stage_config={"oom_safety": True},
        )
        ctx = OOMSafetyFIFO().process(ctx)
        self.assertGreater(ctx.oom_iterations, 0)


# ===========================================================================
# PrefixRatioComputer — stage 18
# ===========================================================================

class TestPrefixRatioComputer(unittest.TestCase):
    def setUp(self):
        _ps._SESSION_LAST_MESSAGES.clear()

    def tearDown(self):
        _ps._SESSION_LAST_MESSAGES.clear()

    def test_computes_ratio(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = PipelineContext(messages=messages, session_id="s_ratio")
        ctx = PrefixRatioComputer().process(ctx)
        self.assertGreaterEqual(ctx.common_prefix_ratio, 0.0)
        self.assertLessEqual(ctx.common_prefix_ratio, 1.0)

    def test_stores_snapshot_for_next_request(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = PipelineContext(messages=messages, session_id="s_snap")
        ctx = PrefixRatioComputer().process(ctx)
        self.assertIn("s_snap", _ps._SESSION_LAST_MESSAGES)

    def test_output_metrics(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = PipelineContext(messages=messages, session_id="s_met")
        ctx = PrefixRatioComputer().process(ctx)
        with patch.object(_ps, "PROXY_METRICS_ENABLED", True):
            metrics = PrefixRatioComputer().output_metrics(ctx)
        self.assertIsNotNone(metrics)
        self.assertIn("ratio", metrics)


# ===========================================================================
# ToolPairingRepair — stage 19
# ===========================================================================

class TestToolPairingRepair(unittest.TestCase):
    def test_passes_messages_through(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = PipelineContext(messages=messages)
        ctx = ToolPairingRepair().process(ctx)
        self.assertIsInstance(ctx.messages, list)

    def test_removes_orphaned_tool_use(self):
        """An orphaned tool_use (no matching tool_result) should be removed."""
        messages = [{
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Read", "id": "orphan",
                         "input": {"file_path": "/f"}}]
        }]
        ctx = PipelineContext(messages=messages)
        ctx = ToolPairingRepair().process(ctx)
        # Orphaned tool_use message is removed entirely
        self.assertEqual(len(ctx.messages), 0)


# ===========================================================================
# FormatConverter — stage 20
# ===========================================================================

class TestFormatConverter(unittest.TestCase):
    def test_converts_messages_to_openai_format(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        body = {"max_tokens": 4096}
        ctx = PipelineContext(messages=messages, body=body, is_stream=False)
        ctx = FormatConverter().process(ctx)
        self.assertIsNotNone(ctx.openai_messages)
        self.assertIsNotNone(ctx.openai_body)
        self.assertIn("model", ctx.openai_body)

    def test_handles_system_prompt(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        body = {"max_tokens": 4096, "system": [{"type": "text", "text": "You are helpful"}]}
        ctx = PipelineContext(messages=messages, body=body, is_stream=False)
        ctx = FormatConverter().process(ctx)
        self.assertEqual(ctx.openai_messages[0]["role"], "system")

    def test_handles_top_p_and_stop(self):
        messages = [{"role": "user", "content": "hi"}]
        body = {"max_tokens": 100, "top_p": 0.9, "stop_sequences": ["END"]}
        ctx = PipelineContext(messages=messages, body=body, is_stream=True)
        ctx = FormatConverter().process(ctx)
        self.assertEqual(ctx.openai_body["top_p"], 0.9)
        self.assertEqual(ctx.openai_body["stop"], ["END"])

    @patch.object(_ps, "IS_CLOUD", True)
    @patch.object(_ps, "MODEL_NAME", "deepseek-v4-flash")
    def test_disables_thinking_for_flash_models(self):
        messages = [{"role": "user", "content": "hi"}]
        body = {"max_tokens": 100}
        ctx = PipelineContext(messages=messages, body=body, is_stream=False)
        ctx = FormatConverter().process(ctx)
        self.assertIn("thinking", ctx.openai_body)
        self.assertEqual(ctx.openai_body["thinking"]["type"], "disabled")

    def test_converts_tools(self):
        messages = [{"role": "user", "content": "hi"}]
        body = {
            "max_tokens": 100,
            "tools": [{"name": "Read", "description": "Read files",
                       "input_schema": {"type": "object", "properties": {}}}],
        }
        ctx = PipelineContext(messages=messages, body=body, is_stream=False)
        ctx = FormatConverter().process(ctx)
        self.assertIn("tools", ctx.openai_body)


# ===========================================================================
# Phase 3+ (建议3): _char_bucket() helper for latency long-tail analysis
# ===========================================================================

class TestCharBucket(unittest.TestCase):
    """The coarse size buckets used to group dispatch_latency_ms in metrics JSONL."""

    def test_xs_below_10k(self):
        self.assertEqual(_char_bucket(0), "xs")
        self.assertEqual(_char_bucket(5000), "xs")
        self.assertEqual(_char_bucket(9999), "xs")

    def test_sm_10k_to_50k(self):
        self.assertEqual(_char_bucket(10000), "sm")
        self.assertEqual(_char_bucket(49999), "sm")

    def test_md_50k_to_150k(self):
        self.assertEqual(_char_bucket(50000), "md")
        self.assertEqual(_char_bucket(149999), "md")

    def test_lg_150k_to_400k(self):
        self.assertEqual(_char_bucket(150000), "lg")
        self.assertEqual(_char_bucket(399999), "lg")

    def test_xl_400k_to_1m(self):
        self.assertEqual(_char_bucket(400000), "xl")
        self.assertEqual(_char_bucket(999999), "xl")

    def test_xxl_above_1m(self):
        self.assertEqual(_char_bucket(1_000_000), "xxl")
        self.assertEqual(_char_bucket(10_000_000), "xxl")

    def test_none_returns_unknown(self):
        self.assertEqual(_char_bucket(None), "unknown")

    def test_invalid_returns_unknown(self):
        self.assertEqual(_char_bucket("not a number"), "unknown")


# ===========================================================================
# BackendDispatcher — stage 21
# ===========================================================================

class TestBackendDispatcher(unittest.TestCase):
    """Comprehensive BackendDispatcher tests — local/cloud dispatch, fallback, headers."""

    def setUp(self):
        self._patches = []
        self._mock_handler = MagicMock()
        self._mock_handler._handle_streaming_response = MagicMock()
        self._mock_handler._handle_non_streaming_response = MagicMock()
        self._mock_handler._respond_json = MagicMock()
        self._mock_handler._route_response_headers = None
        self._mock_lock = MagicMock()
        self._mock_lock.__enter__ = MagicMock(return_value=None)
        self._mock_lock.__exit__ = MagicMock(return_value=None)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        _ps._cloud_fail_count.clear()
        _ps._cloud_cooldown_start.clear()
        # Phase B: per-provider breaker state must not leak between tests
        # (a tripped provider cooldown makes the cloud key-gate short-circuit
        #  every later test in this class into the local fallback path).
        _ps._PROVIDER_FAIL_COUNT.clear()
        _ps._PROVIDER_COOLDOWN_START.clear()

    def tearDown(self):
        # Also clear on exit: leftover breaker state leaks into later test
        # MODULES in the same discover process (e.g. test_payload_limit).
        _ps._PROVIDER_FAIL_COUNT.clear()
        _ps._PROVIDER_COOLDOWN_START.clear()
        _ps._SESSION_ROUTE_MAP.clear()

    def _make_ctx(self, target="local", is_stream=False, total_chars=5000, session_id="s1"):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6", "max_tokens": 4096},
            is_stream=is_stream,
            total_chars=total_chars,
            session_id=session_id,
            openai_body={"model": "test-model", "messages": []},
        )
        ctx._route_target = target
        ctx._route_cloud_model = "deepseek-v4-flash"
        return ctx

    def _mock_urlopen(self, status=200, body=b'{"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20}}'):
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.read.return_value = body
        return mock_resp

    def test_requires_constructor_args(self):
        stage = BackendDispatcher(llama_lock=None, handler=None)
        self.assertEqual(stage.name, "backend_dispatcher")

    def test_backend_status_initialized_none(self):
        stage = BackendDispatcher(llama_lock=None, handler=None)
        self.assertIsNone(stage._backend_status)

    def test_fallback_flags_initialized(self):
        stage = BackendDispatcher(llama_lock=None, handler=None)
        self.assertFalse(stage._route_fallback)
        self.assertFalse(stage._emergency_fallback)
        self.assertEqual(stage._fallback_reason, "")

    def test_local_dispatch_success(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="local")
        mock_resp = self._mock_urlopen(200)
        with patch("pipeline.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            stage.process(ctx)
            mock_open.assert_called_once()
            self.assertIn(_ps.LLAMA_BASE, mock_open.call_args[0][0].full_url)

    def test_local_dispatch_http_error(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="local")
        http_err = urllib.error.HTTPError("http://x", 500, "Internal Error", {}, io.BytesIO(b"boom"))
        with patch("pipeline.urllib.request.urlopen", side_effect=http_err):
            stage.process(ctx)
        self._mock_handler._respond_json.assert_called_once()
        self.assertEqual(stage._backend_status, 500)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "")
    def test_cloud_without_api_key_falls_back_to_local(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="cloud", total_chars=200000)
        mock_resp = self._mock_urlopen(200)
        with patch("pipeline.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            stage.process(ctx)
            self.assertIn(_ps.LLAMA_BASE, mock_open.call_args[0][0].full_url)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "cloud_no_api_key")
        # Regression: FormatConverter set openai_body["model"] to the cloud model
        # before the fallback; BackendDispatcher must rewrite it to the local
        # MODEL_NAME so the local backend doesn't 404 on a foreign model id.
        self.assertEqual(ctx.openai_body["model"], _ps.MODEL_NAME)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-real-key")
    def test_cloud_dispatch_success(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="cloud")
        mock_resp = self._mock_urlopen(200)
        with patch("pipeline.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            stage.process(ctx)
            self.assertIn(_ps.PROXY_CLOUD_BASE_URL, mock_open.call_args[0][0].full_url)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    @patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True)
    def test_cloud_httperror_fallback_to_local(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="cloud", total_chars=5000, session_id="s_fb")
        call_count = [0]

        def side_effect(req, timeout):
            call_count[0] += 1
            if "deepseek" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"down"))
            return self._mock_urlopen(200)

        with patch("pipeline.urllib.request.urlopen", side_effect=side_effect):
            stage.process(ctx)
        self.assertEqual(call_count[0], 2)
        self.assertTrue(stage._route_fallback)
        self.assertEqual(ctx._route_target, "local_forced")
        # Regression: openai_body["model"] must be rewritten to local
        # MODEL_NAME when falling back from cloud so local backend recognises it.
        self.assertEqual(ctx.openai_body["model"], _ps.MODEL_NAME)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    @patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", False)
    def test_cloud_httperror_fallback_disabled_503(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="cloud")
        http_err = urllib.error.HTTPError("http://x", 503, "Unavailable", {}, io.BytesIO(b"down"))
        with patch("pipeline.urllib.request.urlopen", side_effect=http_err):
            stage.process(ctx)
        self._mock_handler._respond_json.assert_called_once()

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    @patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_SENSITIVE_PATTERNS", ".env,.secret")
    def test_cloud_fallback_blocked_by_sensitive_path(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="cloud", session_id="s_sens")
        ctx.messages = [{"role": "user", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/app/.env"}}
        ]}]
        http_err = urllib.error.HTTPError("http://x", 503, "Unavailable", {}, io.BytesIO(b"err"))
        with patch("pipeline.urllib.request.urlopen", side_effect=http_err):
            stage.process(ctx)
        self._mock_handler._respond_json.assert_called_once()
        self.assertTrue(stage._sensitive_blocked)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    @patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_MAX_CLOUD_FAILS", 3)
    def test_cooldown_activated_after_max_failures(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)

        def side_effect(req, timeout):
            if "deepseek" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"down"))
            return self._mock_urlopen(200)

        with patch("pipeline.urllib.request.urlopen", side_effect=side_effect):
            for _ in range(3):
                ctx = self._make_ctx(target="cloud", session_id="s_cool")
                ctx.messages = []
                stage.process(ctx)

        with _ps._state_lock:
            # Failure count is reset when cooldown activates so the session can
            # recover after the cooldown period expires.
            self.assertEqual(_ps._cloud_fail_count.get("s_cool", 0), 0)
            self.assertIn("s_cool", _ps._cloud_cooldown_start)
            self.assertEqual(_ps._SESSION_ROUTE_MAP.get("s_cool"), "local_forced")

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    @patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True)
    def test_non_retryable_cloud_error_does_not_cooldown(self):
        """4xx cloud errors do not trigger cooldown."""
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        http_err = urllib.error.HTTPError("http://deepseek", 401, "Unauthorized", {}, io.BytesIO(b"bad key"))

        def side_effect(req, timeout):
            if "deepseek" in req.full_url:
                raise http_err
            return self._mock_urlopen(200)

        with patch("pipeline.urllib.request.urlopen", side_effect=side_effect):
            ctx = self._make_ctx(target="cloud", session_id="s_401")
            stage.process(ctx)
        self.assertNotIn("s_401", _ps._cloud_cooldown_start)
        self.assertNotIn("s_401", _ps._SESSION_ROUTE_MAP)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    @patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True)
    def test_local_urLError_fallback_to_cloud(self):
        """Local backend connection failure clears cloud cooldown and retries cloud."""
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        _ps._cloud_cooldown_start["s_local_down"] = 0.0
        _ps._SESSION_ROUTE_MAP["s_local_down"] = "local_forced"
        _ps._SESSION_ROUTE_FORCE_SOURCE["s_local_down"] = "cloud_failures"

        def side_effect(req, timeout):
            if _ps.LLAMA_BASE in req.full_url:
                raise urllib.error.URLError("Connection refused")
            return self._mock_urlopen(200)

        with patch("pipeline.urllib.request.urlopen", side_effect=side_effect):
            ctx = self._make_ctx(target="local", session_id="s_local_down")
            stage.process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertEqual(ctx._route_reason, "local_failure_fallback")
        self.assertTrue(stage._route_fallback)
        self.assertNotIn("s_local_down", _ps._cloud_cooldown_start)
        self.assertNotIn("s_local_down", _ps._SESSION_ROUTE_MAP)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    @patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True)
    def test_local_manually_forced_does_not_fallback(self):
        """If user/admin forced local, backend failure does not fallback to cloud."""
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        _ps._SESSION_ROUTE_MAP["s_manual"] = "local_forced"
        _ps._SESSION_ROUTE_FORCE_SOURCE["s_manual"] = "user_manual"
        http_err = urllib.error.HTTPError("http://local", 503, "OOM", {}, io.BytesIO(b"oom"))
        with patch("pipeline.urllib.request.urlopen", side_effect=http_err):
            ctx = self._make_ctx(target="local", session_id="s_manual")
            ctx._route_reason = "session_force_local"
            stage.process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self._mock_handler._respond_json.assert_called_once()
        self.assertFalse(stage._route_fallback)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    @patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True)
    def test_emergency_truncation_triggers_after_fallback(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(40)]
        for i in range(4):
            msgs.append({"role": "assistant", "content": f"resp{i}"})
        ctx = self._make_ctx(target="cloud", total_chars=300000, session_id="s_et")
        ctx.messages = list(msgs)
        ctx.stage_config = {"total_chars": 300000, "stage": "oom_danger"}
        msg_count_before = len(ctx.messages)

        def side_effect(req, timeout):
            if "deepseek" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"down"))
            return self._mock_urlopen(200)

        with patch("pipeline.urllib.request.urlopen", side_effect=side_effect):
            stage.process(ctx)
        self.assertLess(len(ctx.messages), msg_count_before)
        self.assertTrue(stage._emergency_fallback)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    def test_route_response_headers_set_on_handler(self):
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="cloud")
        ctx._route_reason = "chars_exceed_threshold"
        mock_resp = self._mock_urlopen(200)
        with patch("pipeline.urllib.request.urlopen", return_value=mock_resp):
            stage.process(ctx)
        headers = self._mock_handler._route_response_headers
        self.assertIsNotNone(headers)
        # R8 contract names (llama-defender-integration-requirements.md §R8)
        self.assertEqual(headers["X-Proxy-Route-Target"], "cloud")
        self.assertEqual(headers["X-Proxy-Route-Reason"], "chars_exceed_threshold")
        self.assertIn("X-Proxy-Route-Actual-Model", headers)
        self.assertIn("X-Proxy-Route-Cost", headers)
        # Old pre-contract names must be gone (direct switch, no aliases).
        self.assertNotIn("X-Route-Target", headers)
        self.assertNotIn("X-Actual-Model", headers)

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-key")
    def test_route_headers_local_forced_display(self):
        """Forced-local reasons map the display target to local_forced (R8)."""
        stage = BackendDispatcher(llama_lock=self._mock_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="local")
        ctx._route_reason = "session_force_local"
        with patch("pipeline.urllib.request.urlopen", return_value=self._mock_urlopen(200)):
            stage.process(ctx)
        headers = self._mock_handler._route_response_headers
        self.assertEqual(headers["X-Proxy-Route-Target"], "local_forced")
        self.assertEqual(headers["X-Proxy-Route-Cost"], "0.000000")

    @patch.dict(_ps._provider_locks, clear=True)
    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-real-key")
    def test_cloud_concurrency_lock_held_during_dispatch(self):
        """Verify cloud backend dispatch holds concurrency lock.
        
        Regression guard: P0#1 — if code drops `with self._cloud_lock:`,
        this test blocks (second thread is not serialised) and eventually
        detects `urlopen` calls without lock protection.
        """
        real_lock = threading.Semaphore(1)
        real_lock.acquire()  # Pre-occupy the lock
        stage = BackendDispatcher(cloud_lock=real_lock, llama_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="cloud")
        call_count = [0]

        def blocking_urlopen(req, timeout=None):
            call_count[0] += 1
            return self._mock_urlopen(200)

        with patch("pipeline.urllib.request.urlopen", side_effect=blocking_urlopen) as mock_open:
            t = threading.Thread(target=stage.process, args=(ctx,))
            t.start()
            import time
            time.sleep(0.05)
            mock_open.assert_not_called()  # Lock held → no urlopen yet
            real_lock.release()             # Release so dispatch can proceed
            t.join(timeout=2)
        self.assertEqual(call_count[0], 1)  # Exactly one dispatch

    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-real-key")
    def test_local_concurrency_lock_held_during_dispatch(self):
        """Verify local backend dispatch holds concurrency lock.
        
        Regression guard: same as test_cloud_concurrency_lock_held_during_dispatch
        but for the local backend path.
        """
        real_lock = threading.Semaphore(1)
        real_lock.acquire()  # Pre-occupy the lock
        stage = BackendDispatcher(llama_lock=real_lock, cloud_lock=self._mock_lock, handler=self._mock_handler)
        ctx = self._make_ctx(target="local")
        call_count = [0]

        def blocking_urlopen(req, timeout=None):
            call_count[0] += 1
            return self._mock_urlopen(200)

        with patch("pipeline.urllib.request.urlopen", side_effect=blocking_urlopen) as mock_open:
            t = threading.Thread(target=stage.process, args=(ctx,))
            t.start()
            import time
            time.sleep(0.05)
            mock_open.assert_not_called()  # Lock held → blocked
            real_lock.release()
            t.join(timeout=2)
        self.assertEqual(call_count[0], 1)

    @patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True)
    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-real-key")
    def test_force_cloud_does_not_fallback_on_503(self):
        """Force mode cloud failure returns 503, does NOT fallback to local.
        
        Regression guard: force mode (model_forced_cloud reason) should never
        trigger auto-fallback to local backend. User should /model to switch.
        """
        ctx = self._make_ctx(target="cloud")
        ctx._route_reason = "model_forced_cloud(claude-opus-4-7)"
        stage = BackendDispatcher(llama_lock=MagicMock(), cloud_lock=self._mock_lock, handler=self._mock_handler)
        err_resp = MagicMock()
        err_resp.read.return_value = b'{"error":"cloud down"}'
        err_resp.code = 503

        with patch("pipeline.urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            "http://cloud/api", 503, "Service Unavailable", {}, err_resp
        )):
            stage.process(ctx)

        self.assertEqual(stage._backend_status, 503)
        self.assertFalse(stage._route_fallback)
        # Handler should have received a 503 JSON response
        written = self._mock_handler._respond_json.call_args
        self.assertIsNotNone(written)
        self.assertEqual(written[0][1], 503)
        self.assertIn("cloud_unavailable", written[0][0].get("error", {}).get("type", ""))

    def test_output_metrics_structure(self):
        stage = BackendDispatcher(llama_lock=None, handler=None)
        ctx = PipelineContext(openai_body={"model": "test"}, is_stream=True)
        ctx._route_target = "cloud"
        ctx._route_cloud_model = "pro"
        metrics = stage.output_metrics(ctx)
        for k in ("backend_status", "stream", "route_target", "route_cloud_model",
                  "route_fallback", "emergency_fallback"):
            self.assertIn(k, metrics)
        self.assertEqual(metrics["route_target"], "cloud")

    def test_output_metrics_contains_latency_buckets(self):
        """Phase 3+ (建议3): output_metrics must include dispatch_latency_ms and input_chars_bucket."""
        stage = BackendDispatcher(llama_lock=None, handler=None)
        ctx = PipelineContext(openai_body={"model": "test"}, total_chars=75000)
        ctx._route_target = "cloud"
        ctx._route_cloud_model = "deepseek-v4-flash"
        metrics = stage.output_metrics(ctx)
        self.assertIn("dispatch_latency_ms", metrics)
        self.assertIsInstance(metrics["dispatch_latency_ms"], (int, float))
        self.assertIn("input_chars_bucket", metrics)
        self.assertEqual(metrics["input_chars_bucket"], "md")  # 75K is in md bucket (50K-150K)


# ===========================================================================
# Pipeline Integration — multi-stage data flow
# ===========================================================================

class TestPipelineIntegration(unittest.TestCase):
    def setUp(self):
        _ps._LOOP_SESSION_STATE.clear()
        _ps._SESSION_REQUEST_COUNT.clear()
        _ps._SESSION_LAST_MESSAGES.clear()
        _ps._log_ctx.session_id = "itest_sess"

    def tearDown(self):
        _ps._LOOP_SESSION_STATE.clear()
        _ps._SESSION_REQUEST_COUNT.clear()
        _ps._SESSION_LAST_MESSAGES.clear()

    @patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True)
    @patch.object(_ps, "PROXY_MAX_TOKENS_OVERRIDE", 0)
    @patch.object(_ps, "PROXY_BLOCKER_ENABLED", False)
    @patch.object(_ps, "PROXY_TEXT_LOOP_ENABLED", False)
    @patch.object(_ps, "PROXY_CACHE_ALIGN_ENABLED", True)
    @patch.object(_ps, "PROXY_CACHE_ALIGN_HEAD", 2)
    @patch.object(_ps, "PROXY_CTX_LIMIT_ENABLED", False)
    def test_stages_0_through_7_flow(self):
        body = {
            "model": "test-model",
            "stream": False,
            "max_tokens": 4096,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Read /tmp/f.txt"}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Read", "id": "t1",
                     "input": {"file_path": "/tmp/f.txt"}}
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": "file content here"}
                ]},
            ],
            "tools": [{"name": "Read", "description": "Read files"}],
        }
        ctx = RequestParser().process(PipelineContext(body=body))
        ctx = LifecycleClassifier().process(ctx)
        ctx = DynamicMaxTokens().process(ctx)
        ctx = ErrorTranslator().process(ctx)
        ctx = BlockerDetector().process(ctx)
        ctx = SystemNormalizer().process(ctx)
        ctx = CacheAligner().process(ctx)
        ctx = ContentCompressor().process(ctx)

        self.assertIsNotNone(ctx.stage_config)
        self.assertIsNotNone(ctx.compress_stats)
        self.assertIsNotNone(ctx.cleared_files)
        self.assertGreaterEqual(len(ctx._cache_prefix), 0)

    @patch.object(_ps, "PROXY_CTX_LIMIT_ENABLED", False)
    @patch.object(_ps, "IS_CLOUD", True)
    def test_truncation_chain(self):
        body = {
            "model": "test",
            "stream": False,
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": [{"type": "text",
                 "text": "Today's date is 2026/06/21. Hello."}]},
            ],
        }
        ctx = RequestParser().process(PipelineContext(body=body))
        ctx = LifecycleClassifier().process(ctx)
        ctx = DateNormalizer().process(ctx)
        ctx = ContextTruncator().process(ctx)
        ctx = HighDropRatioNotice().process(ctx)

        self.assertIsNotNone(ctx.stage_config)
        self.assertIsNotNone(ctx.trunc_stats)
        self.assertIn("DATE_PLACEHOLDER", str(ctx.messages[0]))


class TestBytesIOResponse(unittest.TestCase):
    """Wraps raw bytes into a file-like response for non-streaming dispatch."""

    def test_wraps_status_and_bytes(self):
        from pipeline import _BytesIOResponse
        body = b'{"key": "value"}'
        wrapped = _BytesIOResponse(200, body)
        self.assertEqual(wrapped.status, 200)
        self.assertEqual(wrapped.read(), body)

    def test_empty_body(self):
        from pipeline import _BytesIOResponse
        wrapped = _BytesIOResponse(503, b"")
        self.assertEqual(wrapped.status, 503)
        self.assertEqual(wrapped.read(), b"")


if __name__ == "__main__":
    unittest.main()


# ===========================================================================
# Phase B: multi-provider dispatch (model catalog)
# ===========================================================================

class TestMultiProviderDispatch(unittest.TestCase):
    """Catalog-driven dispatch: per-provider credentials/locks, fallback
    chains, isolated circuit breakers, cloud URLError fallback, R8 cost."""

    CATALOG = {
        "providers": {
            "p1": {"base_url": "https://p1.example/v1", "key_env": "P1_KEY", "concurrent": 1},
            "p2": {"base_url": "https://p2.example/v1", "key_env": "P2_KEY", "concurrent": 1},
            "local": {"base_url_env": "LLAMA_BASE_URL", "key_env": "LLAMA_API_KEY",
                      "concurrent_env": "PROXY_MAX_CONCURRENT"},
        },
        "models": {
            "m1": {"provider": "p1", "tier": "flagship",
                   "price": {"input": 1.0, "output": 2.0}},
            "m2": {"provider": "p2", "tier": "flagship",
                   "price": {"input": 3.0, "output": 6.0}},
            "local-default": {"provider": "local", "tier": "standard"},
        },
        "routes": {
            "claude-sonnet-4-6": {
                "route_bias": "auto", "cloud_model": ["m1", "m2"],
                "behavior": "prefer", "threshold_factor": 1.0, "memory_bias": 0,
            },
        },
        "defaults": {"cloud_model": "m1"},
    }

    def setUp(self):
        import os
        import tempfile
        import model_registry

        self._model_registry = model_registry
        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "models.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.CATALOG, f)
        model_registry._reset()
        assert model_registry.load(
            path=path, env_cloud_model_getter=lambda: "m1")

        self._patches = [
            patch.object(_ps, "P1_KEY", "sk-p1", create=True),
            patch.object(_ps, "P2_KEY", "sk-p2", create=True),
            patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._restore)

        self._mock_handler = MagicMock()
        self._mock_handler._handle_streaming_response = MagicMock()
        self._mock_handler._handle_non_streaming_response = MagicMock()
        self._mock_handler._respond_json = MagicMock()
        self._mock_handler._route_response_headers = None
        _ps._cloud_fail_count.clear()
        _ps._cloud_cooldown_start.clear()
        _ps._PROVIDER_FAIL_COUNT.clear()
        _ps._PROVIDER_COOLDOWN_START.clear()

    def _restore(self):
        """Restore the real catalog + clean breaker state for later tests."""
        _ps._PROVIDER_FAIL_COUNT.clear()
        _ps._PROVIDER_COOLDOWN_START.clear()
        self._tmp.cleanup()
        self._model_registry._reset()
        self._model_registry.load(
            env_cloud_model_getter=lambda: _ps.PROXY_CLOUD_MODEL)

    def _make_ctx(self, target="cloud", model="m1", fallbacks=None):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6", "max_tokens": 4096},
            is_stream=False,
            total_chars=5000,
            session_id="s_multi",
            openai_body={"model": model, "messages": []},
        )
        ctx._route_target = target
        ctx._route_cloud_model = model
        ctx._route_fallback_models = list(fallbacks or [])
        return ctx

    def _mock_urlopen(self, status=200,
                      body=b'{"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20}}'):
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.read.return_value = body
        return mock_resp

    def _stage(self):
        return BackendDispatcher(llama_lock=MagicMock(), cloud_lock=MagicMock(),
                                 handler=self._mock_handler)

    def test_provider_credentials_resolved_per_model(self):
        """Dispatch URL follows the model's catalog provider, not globals."""
        stage = self._stage()
        ctx = self._make_ctx(model="m2")
        with patch("pipeline.urllib.request.urlopen",
                   return_value=self._mock_urlopen(200)) as mo:
            stage.process(ctx)
        url = str(mo.call_args[0][0].full_url)
        self.assertIn("p2.example", url)
        self.assertEqual(ctx._route_provider, "p2")

    def test_chain_falls_back_to_second_provider(self):
        """Primary provider 500 → next chain entry on another provider serves."""
        stage = self._stage()
        ctx = self._make_ctx(model="m1", fallbacks=["m2"])

        def side_effect(req, timeout=None):
            if "p1.example" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 500, "boom", {}, io.BytesIO(b"{}"))
            return self._mock_urlopen(200)

        with patch("pipeline.urllib.request.urlopen", side_effect=side_effect) as mo:
            stage.process(ctx)
        urls = [str(c[0][0].full_url) for c in mo.call_args_list]
        self.assertTrue(any("p1.example" in u for u in urls))
        self.assertTrue(any("p2.example" in u for u in urls))
        self.assertEqual(ctx._route_cloud_model, "m2")
        self.assertEqual(ctx._route_provider, "p2")
        # Chain success is still a cloud success — no local route_fallback.
        self.assertFalse(stage._route_fallback)
        # Provider breaker counted the p1 failure (1 < MAX, not tripped).
        self.assertEqual(_ps._PROVIDER_FAIL_COUNT.get("p1", 0), 1)
        with _ps._state_lock:
            self.assertNotIn("p1", _ps._PROVIDER_COOLDOWN_START)

    def test_cooldown_provider_is_skipped(self):
        """A provider in cooldown is skipped in favour of the chain fallback."""
        import time
        stage = self._stage()
        with _ps._state_lock:
            _ps._PROVIDER_COOLDOWN_START["p1"] = time.monotonic()
        ctx = self._make_ctx(model="m1", fallbacks=["m2"])
        with patch("pipeline.urllib.request.urlopen",
                   return_value=self._mock_urlopen(200)) as mo:
            stage.process(ctx)
        urls = [str(c[0][0].full_url) for c in mo.call_args_list]
        self.assertFalse([u for u in urls if "p1.example" in u])
        self.assertEqual(ctx._route_provider, "p2")

    def test_keyless_provider_is_skipped(self):
        """Missing provider key skips that chain entry instead of failing."""
        self._patches.append(patch.object(_ps, "P1_KEY", "", create=True))
        self._patches[-1].start()
        stage = self._stage()
        ctx = self._make_ctx(model="m1", fallbacks=["m2"])
        with patch("pipeline.urllib.request.urlopen",
                   return_value=self._mock_urlopen(200)) as mo:
            stage.process(ctx)
        urls = [str(c[0][0].full_url) for c in mo.call_args_list]
        self.assertFalse([u for u in urls if "p1.example" in u])
        self.assertEqual(ctx._route_provider, "p2")

    def test_all_providers_unusable_falls_to_local(self):
        """Every candidate keyless → gate falls back to local (no 503 for prefer)."""
        self._patches.append(patch.object(_ps, "P1_KEY", "", create=True))
        self._patches.append(patch.object(_ps, "P2_KEY", "", create=True))
        for p in self._patches[-2:]:
            p.start()
        stage = self._stage()
        ctx = self._make_ctx(model="m1", fallbacks=["m2"])
        with patch("pipeline.urllib.request.urlopen",
                   return_value=self._mock_urlopen(200)):
            stage.process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "cloud_no_api_key")

    def test_cloud_urlerror_falls_back_to_local(self):
        """Connection-level cloud failure (URLError) triggers local fallback."""
        stage = self._stage()
        ctx = self._make_ctx(model="m1")

        def side_effect(req, timeout=None):
            if "p1.example" in req.full_url:
                raise urllib.error.URLError("connection refused")
            return self._mock_urlopen(200)

        with patch("pipeline.urllib.request.urlopen", side_effect=side_effect):
            stage.process(ctx)
        self.assertEqual(ctx._route_target, "local_forced")
        self.assertTrue(stage._route_fallback)
        # ctx flag is set on every emergency fallback; the stage flag only
        # when truncation actually dropped messages (5K chars → nothing to drop).
        self.assertTrue(ctx._emergency_fallback)
        headers = self._mock_handler._route_response_headers
        self.assertEqual(headers["X-Proxy-Route-Target"], "local_forced")

    def test_r8_cost_header_present_for_cloud(self):
        """Cloud responses carry a positive estimated X-Proxy-Route-Cost."""
        stage = self._stage()
        ctx = self._make_ctx(model="m1")
        with patch("pipeline.urllib.request.urlopen",
                   return_value=self._mock_urlopen(200)):
            stage.process(ctx)
        headers = self._mock_handler._route_response_headers
        self.assertGreater(float(headers["X-Proxy-Route-Cost"]), 0.0)
        self.assertEqual(headers["X-Proxy-Route-Actual-Model"], "m1")

    def test_provider_breaker_trips_after_max_failures(self):
        """MAX_CLOUD_FAILS provider failures trip only that provider's cooldown."""
        import time
        stage = self._stage()
        for _ in range(_ps.PROXY_ROUTE_MAX_CLOUD_FAILS):
            ctx = self._make_ctx(model="m1", fallbacks=[])
            def _raise_503(req, timeout=None):
                raise urllib.error.HTTPError(
                    req.full_url, 503, "down", {}, io.BytesIO(b"{}"))
            with patch("pipeline.urllib.request.urlopen", side_effect=_raise_503):
                with patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", False):
                    stage.process(ctx)
        with _ps._state_lock:
            self.assertIn("p1", _ps._PROVIDER_COOLDOWN_START)
            self.assertNotIn("p2", _ps._PROVIDER_COOLDOWN_START)


class TestCatalogRequestQuirks(unittest.TestCase):
    """Catalog request_quirks applied by FormatConverter (Phase B/C)."""

    def _ctx(self, target, cloud_model):
        ctx = PipelineContext(
            messages=[{"role": "user", "content": "hi"}],
            body={"max_tokens": 64, "temperature": 0.7},
            is_stream=False)
        ctx._route_target = target
        ctx._route_cloud_model = cloud_model
        return ctx

    def test_kimi_thinking_only_forces_temperature_1(self):
        """kimi thinking-only models reject temperature != 1 (live 400 found)."""
        ctx = FormatConverter().process(self._ctx("cloud", "k3"))
        self.assertEqual(ctx.openai_body["model"], "k3")
        self.assertEqual(ctx.openai_body["temperature"], 1)

    def test_temperature_override_beats_client_value(self):
        ctx = FormatConverter().process(self._ctx("cloud", "kimi-for-coding"))
        self.assertEqual(ctx.openai_body["temperature"], 1)

    def test_non_quirk_model_keeps_temperature(self):
        ctx = FormatConverter().process(self._ctx("cloud", "deepseek-v4-flash"))
        self.assertEqual(ctx.openai_body["temperature"], 0.7)
        self.assertEqual(ctx.openai_body["thinking"], {"type": "disabled"})


# ===========================================================================
# Phase D: anthropic-protocol cloud dispatch
# ===========================================================================

class TestAnthropicProtocolDispatch(unittest.TestCase):
    """provider.protocol=anthropic dispatch: URL/headers/body + client-protocol skip."""

    CATALOG = {
        "providers": {
            "zai": {
                "protocol": "anthropic",
                "base_url": "https://open.example/v4",          # openai pay-per-use (unused)
                "anthropic_base_url": "https://zai.example/api/anthropic",
                "anthropic_key_env": "ZAI_KEY",
                "key_env": "ZHIPU_KEY",                          # different key per system
                "anthropic_compatible": True,
                "concurrent": 1,
            },
            "p2": {"base_url": "https://p2.example/v1", "key_env": "P2_KEY", "concurrent": 1},
            "local": {"base_url_env": "LLAMA_BASE_URL", "key_env": "LLAMA_API_KEY",
                      "concurrent_env": "PROXY_MAX_CONCURRENT"},
        },
        "models": {
            "glm-x": {"provider": "zai", "tier": "flagship"},
            "m2": {"provider": "p2", "tier": "flagship"},
            "local-default": {"provider": "local", "tier": "standard"},
        },
        "routes": {
            "claude-sonnet-4-6": {"route_bias": "auto", "cloud_model": "glm-x",
                                  "behavior": "prefer", "threshold_factor": 1.0,
                                  "memory_bias": 0},
        },
        "defaults": {"cloud_model": "glm-x"},
    }

    def setUp(self):
        import os
        import tempfile
        import model_registry
        self._model_registry = model_registry
        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "models.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.CATALOG, f)
        model_registry._reset()
        assert model_registry.load(path=path, env_cloud_model_getter=lambda: "glm-x")
        self._patches = [
            patch.object(_ps, "ZAI_KEY", "sk-zai", create=True),
            patch.object(_ps, "P2_KEY", "sk-p2", create=True),
            patch.object(_ps, "PROXY_ROUTE_FALLBACK_ENABLED", True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._restore)
        self._mock_handler = MagicMock()
        self._mock_handler._handle_streaming_response = MagicMock()
        self._mock_handler._handle_non_streaming_response = MagicMock()
        self._mock_handler._handle_anthropic_stream_passthrough = MagicMock()
        self._mock_handler._handle_anthropic_response = MagicMock()
        self._mock_handler._respond_json = MagicMock()
        self._mock_handler._route_response_headers = None
        self._mock_handler._openai_mode = False
        _ps._cloud_fail_count.clear()
        _ps._cloud_cooldown_start.clear()
        _ps._PROVIDER_FAIL_COUNT.clear()
        _ps._PROVIDER_COOLDOWN_START.clear()

    def _restore(self):
        _ps._PROVIDER_FAIL_COUNT.clear()
        _ps._PROVIDER_COOLDOWN_START.clear()
        self._tmp.cleanup()
        self._model_registry._reset()
        self._model_registry.load(
            env_cloud_model_getter=lambda: _ps.PROXY_CLOUD_MODEL)

    def _make_ctx(self, target="cloud", model="glm-x", fallbacks=None, stream=False):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6", "max_tokens": 64,
                  "messages": [{"role": "user", "content": "hi"}]},
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            is_stream=stream, total_chars=100, session_id="s_anthropic",
            openai_body={"model": model, "messages": [{"role": "user", "content": "hi"}],
                         "max_tokens": 64, "stream": stream},
        )
        ctx._route_target = target
        ctx._route_cloud_model = model
        ctx._route_fallback_models = list(fallbacks or [])
        return ctx

    def _mock_urlopen(self):
        m = MagicMock()
        m.status = 200
        m.read.return_value = json.dumps({
            "id": "msg_x", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode("utf-8")
        return m

    def test_anthropic_dispatch_url_headers_body(self):
        stage = BackendDispatcher(llama_lock=MagicMock(), cloud_lock=MagicMock(),
                                  handler=self._mock_handler)
        ctx = self._make_ctx()
        with patch("pipeline.urllib.request.urlopen",
                   return_value=self._mock_urlopen()) as mo:
            stage.process(ctx)
        req = mo.call_args[0][0]
        self.assertTrue(str(req.full_url).endswith("/v1/messages"), req.full_url)
        self.assertIn("zai.example", str(req.full_url))
        # urllib normalizes header names via .capitalize() → "X-api-key"
        self.assertEqual(req.headers.get("X-api-key"), "sk-zai")  # subscription key, not ZHIPU_KEY
        self.assertEqual(req.headers.get("Anthropic-version"), "2023-06-01")
        sent = json.loads(req.data.decode("utf-8"))
        self.assertEqual(sent["model"], "glm-x")
        # Anthropic-format messages (content blocks), produced via the tested
        # convert_openai_request_to_anthropic round-trip.
        self.assertEqual(sent["messages"][0]["role"], "user")
        self._mock_handler._handle_anthropic_response.assert_called_once()
        # proxy_route attribution injected into the relayed body
        relayed = json.loads(
            self._mock_handler._handle_anthropic_response.call_args[0][1])
        self.assertEqual(relayed["proxy_route"]["actual_model"], "glm-x")
        self.assertEqual(ctx._route_provider, "zai")

    def test_openai_mode_client_skips_anthropic_candidate(self):
        """OpenAI-protocol clients can't consume anthropic backends — chain falls through."""
        self._mock_handler._openai_mode = True
        stage = BackendDispatcher(llama_lock=MagicMock(), cloud_lock=MagicMock(),
                                  handler=self._mock_handler)
        ctx = self._make_ctx(model="glm-x", fallbacks=["m2"])
        with patch("pipeline.urllib.request.urlopen",
                   return_value=self._mock_urlopen()) as mo:
            stage.process(ctx)
        url = str(mo.call_args[0][0].full_url)
        self.assertIn("p2.example/v1/chat/completions", url)   # openai fallback served
        self.assertEqual(ctx._route_provider, "p2")

    def test_anthropic_stream_uses_passthrough(self):
        stage = BackendDispatcher(llama_lock=MagicMock(), cloud_lock=MagicMock(),
                                  handler=self._mock_handler)
        ctx = self._make_ctx(stream=True)
        with patch("pipeline.urllib.request.urlopen", return_value=self._mock_urlopen()):
            stage.process(ctx)
        self._mock_handler._handle_anthropic_stream_passthrough.assert_called_once()
        self._mock_handler._handle_anthropic_response.assert_not_called()


class TestReasoningEffortPassthrough(unittest.TestCase):
    """Effort 贯通: output_config.effort → 按模型 levels 映射 → openai_body.reasoning_effort."""

    def _ctx(self, body_extra, cloud_model="k3"):
        body = {"model": "claude-sonnet-4-6", "max_tokens": 64}
        body.update(body_extra)
        ctx = PipelineContext(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            body=body, is_stream=False)
        ctx._route_target = "cloud"
        ctx._route_cloud_model = cloud_model
        return ctx

    def test_map_effort_exact_and_step_up(self):
        from pipeline import _map_effort_to_levels
        kimi = ["low", "high", "max"]
        self.assertEqual(_map_effort_to_levels("low", kimi), "low")
        self.assertEqual(_map_effort_to_levels("high", kimi), "high")
        self.assertEqual(_map_effort_to_levels("max", kimi), "max")
        # medium/xhigh 不在 kimi 集合 → 就近向上取（质量保守）
        self.assertEqual(_map_effort_to_levels("medium", kimi), "high")
        self.assertEqual(_map_effort_to_levels("xhigh", kimi), "max")
        # 向下回退（后端只有更低档时）
        self.assertEqual(_map_effort_to_levels("xhigh", ["low", "medium"]), "medium")
        self.assertIsNone(_map_effort_to_levels("bogus", kimi))
        self.assertIsNone(_map_effort_to_levels("high", []))

    def test_kimi_model_gets_mapped_effort(self):
        ctx = FormatConverter().process(
            self._ctx({"output_config": {"effort": "xhigh"}}, cloud_model="k3"))
        self.assertEqual(ctx.openai_body["reasoning_effort"], "max")  # kimi: xhigh→max

    def test_openai_reasoning_effort_normalized(self):
        """OpenAI 协议顶层 reasoning_effort（经入口归一为 output_config）同样生效。"""
        ctx = FormatConverter().process(
            self._ctx({"reasoning_effort": "low"}, cloud_model="kimi-for-coding"))
        self.assertEqual(ctx.openai_body["reasoning_effort"], "low")

    def test_backend_without_levels_gets_nothing(self):
        ctx = FormatConverter().process(
            self._ctx({"output_config": {"effort": "high"}},
                      cloud_model="deepseek-v4-flash"))
        self.assertNotIn("reasoning_effort", ctx.openai_body)

    def test_no_client_value_no_injection(self):
        ctx = FormatConverter().process(self._ctx({}, cloud_model="k3"))
        self.assertNotIn("reasoning_effort", ctx.openai_body)

    def test_converter_openai_to_anthropic_effort_nested(self):
        from message_converter import convert_openai_request_to_anthropic
        out = convert_openai_request_to_anthropic(
            {"model": "k3", "max_tokens": 8, "reasoning_effort": "low",
             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(out.get("output_config", {}).get("effort"), "low")
        self.assertNotIn("reasoning_effort", out)  # 顶层杂键不得进入 Anthropic 载荷


class TestMinMaxTokensFloor(unittest.TestCase):
    """F1: thinking-only 模型 max_tokens 下限（GLM 空响应修复）."""

    def _convert(self, max_tokens, model="glm-5.2"):
        body = {"model": "claude-opus-4-7", "max_tokens": max_tokens}
        ctx = PipelineContext(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            body=body, is_stream=False)
        ctx._route_target = "cloud"
        ctx._route_cloud_model = model
        return FormatConverter().process(ctx)

    def test_small_max_tokens_floored(self):
        ctx = self._convert(1000)
        self.assertEqual(ctx.openai_body["max_tokens"], 8192)

    def test_large_max_tokens_untouched(self):
        ctx = self._convert(32000)
        self.assertEqual(ctx.openai_body["max_tokens"], 32000)

    def test_model_without_quirk_untouched(self):
        ctx = self._convert(1000, model="deepseek-v4-flash")
        self.assertEqual(ctx.openai_body["max_tokens"], 1000)
