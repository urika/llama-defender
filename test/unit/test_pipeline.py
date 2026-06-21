"""Unit tests for pipeline infrastructure: PipelineContext, PipelineStage,
Pipeline, InstrumentedPipeline.  Does NOT test individual stage behaviour
(see test_pipeline_stages.py for that).
"""
import unittest
from unittest.mock import patch

from pipeline import (
    PipelineContext,
    PipelineStage,
    ConditionalStage,
    Pipeline,
    InstrumentedPipeline,
)


class FakeStage(PipelineStage):
    """A minimal stage that appends a marker to ctx.messages."""
    name = "fake"

    def __init__(self, marker="processed"):
        self.marker = marker

    def process(self, ctx):
        ctx.messages.append(self.marker)
        return ctx


class MetricsStage(PipelineStage):
    """Stage that returns output_metrics."""
    name = "metrics_stage"

    def process(self, ctx):
        ctx.total_chars = 99
        return ctx

    def output_metrics(self, ctx):
        return {"chars": ctx.total_chars}


class NullMetricsStage(PipelineStage):
    """Stage whose output_metrics returns None."""
    name = "null_metrics"

    def process(self, ctx):
        return ctx


class SkippableStage(ConditionalStage):
    """Conditional stage that skips when ctx.model == 'skip'."""
    name = "skippable"

    def should_run(self, ctx):
        return ctx.model != "skip"

    def process(self, ctx):
        ctx.messages.append("should_not_run")
        return ctx


class ExplodingStage(PipelineStage):
    """Stage that always raises."""
    name = "exploder"

    def process(self, ctx):
        raise RuntimeError("BOOM")


class TestPipelineContext(unittest.TestCase):
    def test_default_construction(self):
        ctx = PipelineContext()
        self.assertEqual(ctx.request_id, "")
        self.assertEqual(ctx.model, "unknown")
        self.assertFalse(ctx.is_stream)
        self.assertEqual(ctx.max_tokens_orig, 4096)
        self.assertEqual(ctx.messages, [])
        self.assertEqual(ctx.body, {})

    def test_custom_construction(self):
        ctx = PipelineContext(
            request_id="req_1",
            model="claude-sonnet",
            is_stream=True,
            body={"messages": [{"role": "user"}]},
        )
        self.assertEqual(ctx.request_id, "req_1")
        self.assertEqual(ctx.model, "claude-sonnet")
        self.assertTrue(ctx.is_stream)

    def test_stage_outputs_default_to_none(self):
        ctx = PipelineContext()
        self.assertIsNone(ctx.stage_config)
        self.assertIsNone(ctx.error_count)
        self.assertIsNone(ctx.blocker_info)
        self.assertIsNone(ctx.cleared_files)
        self.assertIsNone(ctx.compress_stats)
        self.assertIsNone(ctx.openai_messages)
        self.assertIsNone(ctx.openai_body)
        self.assertEqual(ctx.max_run, 0)
        self.assertEqual(ctx.loop_level, 0)
        self.assertFalse(ctx.is_text_loop)

    def test_mutable_state(self):
        ctx = PipelineContext()
        ctx.messages.append({"role": "user", "content": "hello"})
        self.assertEqual(len(ctx.messages), 1)
        ctx.messages = [{"role": "assistant"}]
        self.assertEqual(len(ctx.messages), 1)

    def test_internal_cache_fields_hidden_from_repr(self):
        ctx = PipelineContext(_cache_prefix=[1, 2], _cache_dynamic=[3, 4])
        r = repr(ctx)
        self.assertNotIn("_cache_prefix", r)
        self.assertNotIn("_cache_dynamic", r)


class TestPipelineStage(unittest.TestCase):
    def test_cannot_instantiate_abstract(self):
        with self.assertRaises(TypeError):
            PipelineStage()

    def test_concrete_subclass_works(self):
        stage = FakeStage()
        self.assertEqual(stage.name, "fake")

    def test_default_output_metrics_returns_none(self):
        stage = FakeStage()
        self.assertIsNone(stage.output_metrics(PipelineContext()))

    def test_repr(self):
        stage = FakeStage(marker="test")
        self.assertIn("FakeStage", repr(stage))
        self.assertIn("fake", repr(stage))


class TestConditionalStage(unittest.TestCase):
    def test_cannot_instantiate_abstract(self):
        with self.assertRaises(TypeError):
            ConditionalStage()

    def test_default_should_run_returns_true(self):
        class DefaultCond(ConditionalStage):
            name = "default_cond"

            def process(self, ctx):
                return ctx

        stage = DefaultCond()
        self.assertTrue(stage.should_run(PipelineContext()))


class TestPipeline(unittest.TestCase):
    def test_empty_pipeline(self):
        pipeline = Pipeline([])
        ctx = PipelineContext(messages=["a"])
        result = pipeline.run(ctx)
        self.assertEqual(result.messages, ["a"])

    def test_single_stage(self):
        pipeline = Pipeline([FakeStage(marker="done")])
        ctx = PipelineContext(messages=["start"])
        result = pipeline.run(ctx)
        self.assertEqual(result.messages, ["start", "done"])

    def test_multi_stage_ordering(self):
        pipeline = Pipeline([
            FakeStage(marker="first"),
            FakeStage(marker="second"),
        ])
        ctx = PipelineContext(messages=["init"])
        result = pipeline.run(ctx)
        self.assertEqual(result.messages, ["init", "first", "second"])

    def test_stage_output_is_next_input(self):
        seen = []

        class TrackStage(PipelineStage):
            name = "track"
            _id = 0

            def __init__(self):
                self._my_id = TrackStage._id
                TrackStage._id += 1

            def process(self, ctx):
                seen.append(self._my_id)
                ctx.messages.append(f"s{self._my_id}")
                return ctx

        pipeline = Pipeline([TrackStage(), TrackStage(), TrackStage()])
        ctx = PipelineContext(messages=["s"])
        result = pipeline.run(ctx)
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(result.messages, ["s", "s0", "s1", "s2"])

    def test_conditional_stage_skips(self):
        pipeline = Pipeline([FakeStage("a"), SkippableStage(), FakeStage("b")])
        ctx = PipelineContext(model="skip", messages=["x"])
        result = pipeline.run(ctx)
        self.assertEqual(result.messages, ["x", "a", "b"])

    def test_conditional_stage_runs(self):
        pipeline = Pipeline([FakeStage("a"), SkippableStage(), FakeStage("b")])
        ctx = PipelineContext(model="run", messages=["x"])
        result = pipeline.run(ctx)
        self.assertEqual(result.messages, ["x", "a", "should_not_run", "b"])


class TestInstrumentedPipeline(unittest.TestCase):
    def setUp(self):
        self._mc_patcher = patch("pipeline._import_admin_server")
        self.mock_admin = self._mc_patcher.start()
        self.mock_admin.return_value._mc_put = lambda k, v: None

    def tearDown(self):
        self._mc_patcher.stop()

    def test_instrumented_runs_all_stages(self):
        pipeline = InstrumentedPipeline([FakeStage("a"), FakeStage("b")])
        ctx = PipelineContext(messages=["x"])
        result = pipeline.run(ctx)
        self.assertEqual(result.messages, ["x", "a", "b"])

    def test_instrumented_handles_conditional_skip(self):
        pipeline = InstrumentedPipeline([FakeStage("a"), SkippableStage(), FakeStage("b")])
        ctx = PipelineContext(model="skip", messages=["x"])
        result = pipeline.run(ctx)
        self.assertEqual(result.messages, ["x", "a", "b"])

    def test_instrumented_calls_output_metrics(self):
        pipeline = InstrumentedPipeline([MetricsStage()])
        ctx = PipelineContext(messages=[])
        result = pipeline.run(ctx)
        self.assertEqual(result.total_chars, 99)

    def test_instrumented_handles_null_metrics(self):
        pipeline = InstrumentedPipeline([NullMetricsStage()])
        ctx = PipelineContext(messages=[])
        result = pipeline.run(ctx)

    def test_exception_propagates(self):
        pipeline = Pipeline([ExplodingStage()])
        ctx = PipelineContext(messages=[])
        with self.assertRaises(RuntimeError) as cm:
            pipeline.run(ctx)
        self.assertIn("BOOM", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
