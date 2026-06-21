"""Unit tests for individual PipelineStage classes.

Each stage is tested in isolation by constructing a PipelineContext with
known inputs, running the stage, and asserting on the output context fields.
Existing functions (lifecycle.py, loop_detection.py, etc.) are called
through the stages — their own unit tests already cover edge cases.
"""
import json
import unittest
from unittest.mock import patch

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
        self.assertIn("tool_clear", metrics)


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
# BackendDispatcher — stage 21
# ===========================================================================

class TestBackendDispatcher(unittest.TestCase):
    def test_requires_constructor_args(self):
        stage = BackendDispatcher(llama_lock=None, handler=None)
        self.assertEqual(stage.name, "backend_dispatcher")

    def test_backend_status_initialized_none(self):
        stage = BackendDispatcher(llama_lock=None, handler=None)
        self.assertIsNone(stage._backend_status)

    def test_output_metrics_structure(self):
        stage = BackendDispatcher(llama_lock=None, handler=None)
        ctx = PipelineContext(openai_body={"model": "test"}, is_stream=True)
        metrics = stage.output_metrics(ctx)
        self.assertIn("backend_status", metrics)
        self.assertIn("stream", metrics)
        self.assertEqual(metrics["stream"], 1)


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


if __name__ == "__main__":
    unittest.main()
