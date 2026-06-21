"""
Pipeline abstraction for message processing stages.

Refactors _handle_messages() from ~537 inline lines into 22 composable,
independently testable PipelineStage components.  Each stage is a thin
wrapper around existing functions in lifecycle.py, loop_detection.py,
tool_filter.py, truncation.py, message_converter.py, and content_compressor.py.

Called by: anthropic_proxy.py:Handler._handle_messages()

Usage:
    ctx = RequestParser().process(PipelineContext(body=body, request_id=...))
    pipeline = InstrumentedPipeline([LifecycleClassifier(), ..., BackendDispatcher(...)])
    pipeline.run(ctx)
"""
import json
import os
import time
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import proxy_state as _ps
from proxy_logging import log


# ---------------------------------------------------------------------------
# Deferred imports — resolved at stage construction time to avoid circular
# import issues with modules that reference proxy_state globals.
# ---------------------------------------------------------------------------
def _import_lifecycle():
    import lifecycle
    return lifecycle


def _import_loop_detection():
    import loop_detection
    return loop_detection


def _import_tool_filter():
    import tool_filter
    return tool_filter


def _import_truncation():
    import truncation
    return truncation


def _import_message_converter():
    import message_converter
    return message_converter


def _import_admin_server():
    import admin_server
    return admin_server


# ============================================================================
# PipelineContext — request-level state container
# ============================================================================

@dataclass
class PipelineContext:
    """Request-level state threaded through every pipeline stage.

    Fields grouped by usage:
      - Immutable inputs: set at construction, never modified by stages
      - Mutable primary state: stages may mutate in-place or reassign
      - Stage outputs: populated by one stage, consumed by another
      - Internal state: used by specific stage pairs (prefixed with _)
    """

    # --- Immutable inputs ---
    request_id: str = ""
    model: str = "unknown"
    is_stream: bool = False
    max_tokens_orig: int = 4096
    raw_tools_orig: list = field(default_factory=list)
    session_id: str = ""
    total_chars: int = 0
    tools_list: list = field(default_factory=list)

    # --- Mutable primary state ---
    messages: list = field(default_factory=list)
    body: dict = field(default_factory=dict)

    # --- Stage outputs (populated by stages, consumed downstream) ---
    stage_config: Optional[dict] = None
    max_tokens_curr: Optional[int] = None
    error_count: Optional[dict] = None
    blocker_info: Optional[dict] = None
    cleared_files: Optional[list] = None
    compress_stats: Optional[dict] = None
    max_run: int = 0
    consecutive: dict = field(default_factory=dict)
    pattern_tool_name: Optional[str] = None
    is_text_loop: bool = False
    text_loop_run: int = 0
    loop_level: int = 0
    loop_tool_name: Optional[str] = None
    re_read_info: Optional[dict] = None
    trunc_stats: Optional[dict] = None
    common_prefix_ratio: float = 0.0
    openai_messages: Optional[list] = None
    openai_body: Optional[dict] = None
    oom_iterations: int = 0
    high_drop_notice_injected: bool = False

    # --- CacheAligner internal state ---
    _cache_prefix: list = field(default_factory=list, repr=False)
    _cache_dynamic: list = field(default_factory=list, repr=False)


# ============================================================================
# PipelineStage — abstract base classes
# ============================================================================

class PipelineStage(ABC):
    """Single processing stage in the message pipeline.

    Subclasses must:
      1. Set ``name`` to a unique stage identifier (e.g. "error_translator")
      2. Implement ``process(ctx) -> PipelineContext``

    Optionally override ``output_metrics(ctx) -> dict | None`` to provide
    metrics data that InstrumentedPipeline writes via _mc_put().
    """

    name: str = ""

    @abstractmethod
    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Transform the pipeline context. May mutate ctx in-place or return
        a modified copy — the Pipeline always uses the return value as the
        next stage's input.
        """
        ...

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        """Return metrics dict for mc[\"pipeline\"][self.name], or None to skip."""
        return None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name='{self.name}')"


class ConditionalStage(PipelineStage):
    """Stage that conditionally executes based on pipeline context.

    Override ``should_run(ctx) -> bool``. When False, the stage is skipped
    (ctx passes through unchanged, no metrics collected).
    """

    def should_run(self, ctx: PipelineContext) -> bool:
        return True


# ============================================================================
# Pipeline — orchestrators
# ============================================================================

class Pipeline:
    """Ordered list of PipelineStages executed sequentially.

    Each stage's output becomes the next stage's input.
    ConditionalStage instances are checked via should_run().
    """

    def __init__(self, stages: list[PipelineStage]):
        self.stages = stages

    def run(self, ctx: PipelineContext) -> PipelineContext:
        for stage in self.stages:
            if isinstance(stage, ConditionalStage) and not stage.should_run(ctx):
                continue
            ctx = stage.process(ctx)
        return ctx


class InstrumentedPipeline(Pipeline):
    """Pipeline with automatic timing, logging, and metrics per stage.

    For each stage that executes:
      - Elapsed time is measured and logged
      - output_metrics() is called; if non-None, written via _mc_put(stage.name, data)
    """

    def run(self, ctx: PipelineContext) -> PipelineContext:
        admin = _import_admin_server()
        executed = 0
        skipped = 0
        total_ms = 0.0
        slowest_name = None
        slowest_ms = 0.0

        for stage in self.stages:
            if isinstance(stage, ConditionalStage) and not stage.should_run(ctx):
                skipped += 1
                continue
            executed += 1
            t0 = time.monotonic()
            ctx = stage.process(ctx)
            elapsed = (time.monotonic() - t0) * 1000
            total_ms += elapsed
            if elapsed > slowest_ms:
                slowest_ms = elapsed
                slowest_name = stage.name
            data = stage.output_metrics(ctx)
            if data is not None:
                admin._mc_put(stage.name, data)
            log(f"  -> [{stage.name}] completed in {elapsed:.1f}ms")

        # Pipeline-level aggregate: one summary per request
        admin._mc_put("pipeline_summary", {
            "total_stages": len(self.stages),
            "executed": executed,
            "skipped": skipped,
            "pipeline_total_ms": round(total_ms, 1),
            "slowest_stage": slowest_name,
            "slowest_ms": round(slowest_ms, 1),
        })
        return ctx


# ============================================================================
# Stage 0: RequestParser — parse raw body into PipelineContext
# ============================================================================

class RequestParser(PipelineStage):
    """Stage 0: Parse the raw Anthropic request body into a PipelineContext.

    Extracts model, stream, tools, session_id, and character counts.
    Logs REQ_SUMMARY and populates initial metrics.
    """

    name = "request_parser"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        body = ctx.body
        ctx.is_stream = body.get("stream", False)
        ctx.model = body.get("model", "unknown")
        ctx.max_tokens_orig = body.get("max_tokens", 4096)

        # Extract tool names
        raw_tools = body.get("tools", [])
        if raw_tools:
            ctx.tools_list = [t.get("name", "") for t in raw_tools if isinstance(t, dict)]
            ctx.raw_tools_orig = raw_tools

        # Session ID (from thread-local logging context)
        ctx.session_id = getattr(_ps._log_ctx, 'session_id', None) or ""

        # Character count
        ctx.total_chars = sum(
            len(json.dumps(m, ensure_ascii=False)) for m in body.get("messages", [])
        )

        # REQ_SUMMARY logging
        tools_count = len(ctx.tools_list or [])
        log(f"  [REQ_SUMMARY] chars={ctx.total_chars} tools={tools_count}")

        # Structured REQ_SUMMARY
        from proxy_logging import log_structured
        log_structured("REQ_SUMMARY", chars=ctx.total_chars, tools=tools_count,
                       model=ctx.model, stream=ctx.is_stream)

        # Initial metrics
        if _ps.PROXY_METRICS_ENABLED:
            mc = getattr(_ps._metrics_ctx, 'mc', None)
            if mc:
                mc["input_msgs"] = len(body.get("messages", []))
                mc["input_chars"] = ctx.total_chars
                mc["input_tools"] = tools_count
                mc["tools"] = ctx.tools_list or []

        log(f"  -> Handling model={ctx.model}, stream={ctx.is_stream}")
        log(f"  -> Backend timeout: {_ps.PROXY_BACKEND_TIMEOUT}s, "
            f"output token limit: {_ps.PROXY_OUTPUT_TOKEN_LIMIT_RATIO}x max_tokens, "
            f"max_tokens override: {_ps.PROXY_MAX_TOKENS_OVERRIDE}")

        # Initialize messages from body
        ctx.messages = body.get("messages", [])

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        return {
            "msg_count": len(ctx.messages),
            "tool_count": len(ctx.tools_list or []),
            "input_chars": ctx.total_chars,
            "is_stream": 1 if ctx.is_stream else 0,
        }


# ============================================================================
# Stage 1: LifecycleClassifier — classify context size into lifecycle stage
# ============================================================================

class LifecycleClassifier(PipelineStage):
    """Stage 1: Classify the request's context size into a lifecycle stage.

    Calls _classify_lifecycle_stage() from lifecycle.py, which also increments
    _SESSION_REQUEST_COUNT as a side effect (session continuation detection).
    """

    name = "lifecycle_stage"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        lifecycle = _import_lifecycle()
        stage_config = lifecycle._classify_lifecycle_stage(
            ctx.messages,
            session_id=ctx.session_id,
        )
        ctx.stage_config = stage_config

        log(f"  -> Stage: {stage_config['stage']} (chars={stage_config['total_chars']:,}, "
            f"frozen={stage_config['frozen_head']}, clear_zone={stage_config['clear_zone_pct']}, "
            f"thinking_keep={stage_config['thinking_keep']}, "
            f"truncate_rounds={stage_config['truncate_rounds']}, oom_safety={stage_config['oom_safety']}, "
            f"continuation={stage_config.get('is_continuation', False)}, "
            f"req_count={stage_config.get('request_count', 0)})")

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        if _ps.PROXY_METRICS_ENABLED:
            return ctx.stage_config
        return None


# ============================================================================
# Stage 2: DynamicMaxTokens — adjust max_tokens based on lifecycle + memory
# ============================================================================

class DynamicMaxTokens(ConditionalStage):
    """Stage 2: Dynamically adjust max_tokens based on lifecycle stage + memory pressure.

    Condition: PROXY_DYNAMIC_MAX_TOKENS_ENABLED or PROXY_MAX_TOKENS_OVERRIDE > 0.
    """

    name = "dynamic_max_tokens"

    def should_run(self, ctx: PipelineContext) -> bool:
        return (_ps.PROXY_DYNAMIC_MAX_TOKENS_ENABLED
                or _ps.PROXY_MAX_TOKENS_OVERRIDE > 0)

    def process(self, ctx: PipelineContext) -> PipelineContext:
        admin = _import_admin_server()
        lifecycle = _import_lifecycle()

        current_mem = admin._get_system_memory()
        dynamic_max, dynamic_reason = lifecycle._compute_dynamic_max_tokens(
            ctx.max_tokens_orig, ctx.stage_config, mem=current_mem)

        if dynamic_max != ctx.max_tokens_orig:
            ctx.body["max_tokens"] = dynamic_max
            log(f"  -> max_tokens dynamic: {ctx.max_tokens_orig} -> {dynamic_max} ({dynamic_reason})")

        if _ps.PROXY_METRICS_ENABLED:
            mc = getattr(_ps._metrics_ctx, 'mc', None)
            if mc:
                mc["max_tokens_original"] = ctx.max_tokens_orig
                mc["max_tokens_dynamic"] = dynamic_max
                mc["used_pct"] = float(current_mem.get("used_pct", 0))

        # Hard override (takes final precedence)
        if _ps.PROXY_MAX_TOKENS_OVERRIDE > 0 and ctx.body.get("max_tokens", ctx.max_tokens_orig) > _ps.PROXY_MAX_TOKENS_OVERRIDE:
            ctx.body["max_tokens"] = _ps.PROXY_MAX_TOKENS_OVERRIDE
            log(f"  -> max_tokens override: {ctx.max_tokens_orig} -> {_ps.PROXY_MAX_TOKENS_OVERRIDE}")

        ctx.max_tokens_curr = ctx.body.get("max_tokens", ctx.max_tokens_orig)
        return ctx


# ============================================================================
# Stage 3: ErrorTranslator — rewrite known backend errors to natural language
# ============================================================================

class ErrorTranslator(PipelineStage):
    """Stage 3: Translate tool_result error patterns into Chinese system messages.

    Calls _translate_tool_result_errors() from tool_filter.py.
    Mutates ctx.messages in-place.
    """

    name = "error_translator"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        tool_filter = _import_tool_filter()
        raw_messages, error_count = tool_filter._translate_tool_result_errors(ctx.messages)
        ctx.messages = raw_messages
        ctx.error_count = error_count

        total_errors = sum(error_count.values())
        if total_errors > 0:
            log(f"  -> Error translation: {total_errors} tool_result errors rewritten "
                f"(wasted={error_count['wasted']}, file_not_found={error_count['file_not_found']}, "
                f"input_validation={error_count['input_validation']})")

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        if ctx.error_count:
            total = sum(ctx.error_count.values())
            return {"count": total, **ctx.error_count}
        return None


# ============================================================================
# Stage 4: BlockerDetector — detect consecutive same-error tool failures
# ============================================================================

class BlockerDetector(ConditionalStage):
    """Stage 4: Detect consecutive same-error tool_result rejections.

    Condition: PROXY_BLOCKER_ENABLED is true.
    When triggered, appends a [BLOCKER] user message to ctx.messages.
    """

    name = "blocker_detect"

    def should_run(self, ctx: PipelineContext) -> bool:
        return _ps.PROXY_BLOCKER_ENABLED

    def process(self, ctx: PipelineContext) -> PipelineContext:
        loop_detection = _import_loop_detection()
        blocker_info = loop_detection._detect_blocker_pattern(ctx.messages)
        ctx.blocker_info = blocker_info

        if blocker_info.get("triggered"):
            log(f"  -> Blocker detected: {blocker_info['tool_name']} failed "
                f"({blocker_info['error_type']}) {blocker_info['run_length']} times in a row, "
                f"injecting [BLOCKER] message")
            ctx.messages.append(loop_detection._build_blocker_message(
                blocker_info["tool_name"],
                blocker_info["error_type"],
                blocker_info["run_length"],
            ))

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        return ctx.blocker_info


# ============================================================================
# Stage 5: SystemNormalizer — normalize mid-conversation system messages
# ============================================================================

class SystemNormalizer(PipelineStage):
    """Stage 5: Convert subsequent system messages to user messages.

    Qwen models crash on mid-conversation system messages. This keeps only
    the first system message and converts the rest to [System update]: user
    messages.  Calls _normalize_system_messages() from lifecycle.py.
    """

    name = "system_normalizer"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        lifecycle = _import_lifecycle()
        ctx.messages = lifecycle._normalize_system_messages(ctx.messages)
        return ctx


# ============================================================================
# Stage 6: CacheAligner — protect prefix messages from compression/truncation
# ============================================================================

class CacheAligner(PipelineStage):
    """Stage 6: Split messages into protected prefix and mutable dynamic zone.

    Calls _apply_cache_aligner() from lifecycle.py. The prefix is protected
    from compression and truncation so the KV cache prefix stays stable.
    Places the split parts into ctx._cache_prefix / ctx._cache_dynamic,
    and sets ctx.messages = ctx._cache_dynamic so downstream stages only
    see the dynamic portion.
    """

    name = "cache_aligner"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        lifecycle = _import_lifecycle()
        cache_prefix, cache_dynamic = lifecycle._apply_cache_aligner(ctx.messages)

        if cache_prefix:
            log(f"  -> Cache aligner: protecting first {len(cache_prefix)} messages from compression/truncation")

        ctx._cache_prefix = cache_prefix
        ctx._cache_dynamic = cache_dynamic
        # Downstream stages see only the dynamic zone
        ctx.messages = cache_dynamic
        return ctx


# ============================================================================
# Stage 7: ContentCompressor — single-pass tool clearing + thinking strip + semantic compress
# ============================================================================

class ContentCompressor(PipelineStage):
    """Stage 7: Compress tool results, strip thinking blocks, semantic compression.

    Operates on ctx.messages (which is the dynamic zone set by CacheAligner).
    After compression, reassembles: ctx.messages = _cache_prefix + _cache_dynamic.

    Calls _compress_content_pass() from truncation.py.
    """

    name = "content_compressor"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        truncation = _import_truncation()
        cache_dynamic = ctx._cache_dynamic
        compress_stats = {"clear": {"enabled": False}, "think": {"enabled": False}}

        if cache_dynamic:
            dynamic_stage_config = dict(ctx.stage_config) if ctx.stage_config else {}
            dynamic_stage_config["frozen_head"] = 0  # prefix already protected
            cache_dynamic, compress_stats = truncation._compress_content_pass(
                cache_dynamic,
                tools_list=ctx.tools_list,
                stage_config=dynamic_stage_config,
            )

        # Reassemble: prefix + compressed dynamic
        ctx._cache_dynamic = cache_dynamic
        ctx.messages = ctx._cache_prefix + ctx._cache_dynamic
        ctx.compress_stats = compress_stats

        # Extract sub-stats
        clear_stats = compress_stats.get("clear", {})
        think_stats = compress_stats.get("think", {})
        semantic_compress_stats = compress_stats.get("compress", {"enabled": False})
        cleared_files = clear_stats.get("cleared_files", [])
        ctx.cleared_files = cleared_files

        # Log semantic compression
        if semantic_compress_stats.get("enabled"):
            log(f"  -> Semantic compression: {semantic_compress_stats['compressed_count']} tool_results compressed, "
                f"{semantic_compress_stats['saved_chars']:,} chars saved "
                f"(ratio={semantic_compress_stats['ratio']:.2%}, strategies={semantic_compress_stats.get('strategies', {})})")
        elif _ps.PROXY_COMPRESS_ENABLED:
            log(f"  -> Semantic compression: active (threshold={_ps.PROXY_COMPRESS_THRESHOLD}, mode={_ps.PROXY_COMPRESS_MODE})")

        # Log tool clearing
        if clear_stats.get("cleared"):
            log(f"  -> Tool clearing: {clear_stats['cleared_tool_results']} tool_results cleared, "
                f"{clear_stats['cleared_chars']:,} chars freed (kept {clear_stats['kept']})")
        elif not clear_stats.get("enabled"):
            log(f"  -> Tool clearing: disabled ({_ps.BACKEND_TYPE} backend)")
        elif clear_stats.get("enabled") and not clear_stats.get("skipped"):
            log(f"  -> Tool clearing: active (threshold={_ps.PROXY_CLEAR_THRESHOLD}, keep={_ps.PROXY_TOOL_KEEP})")

        # Log thinking strip
        if think_stats.get("stripped"):
            log(f"  -> Thinking stripped: {think_stats['stripped_count']} old assistant messages cleaned (kept last {think_stats['kept']})")
        elif think_stats.get("enabled") and not think_stats.get("skipped"):
            reason = think_stats.get("reason", "")
            if reason == "stage_skip":
                log(f"  -> Thinking strip: skipped (stage={ctx.stage_config.get('stage', '?')})")
            else:
                log(f"  -> Thinking strip: active (keep_recent={ctx.stage_config.get('thinking_keep', '?')})")

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        if not _ps.PROXY_METRICS_ENABLED:
            return None
        result = {}
        compress_stats = ctx.compress_stats or {}
        # Semantic compression metrics
        semantic = compress_stats.get("compress", {"enabled": False})
        if semantic.get("enabled"):
            result["semantic_compress"] = semantic
        # Tool clearing metrics
        clear_stats = compress_stats.get("clear", {})
        result["tool_clear"] = {
            "applied": clear_stats.get("cleared", False),
            "cleared": clear_stats.get("cleared_tool_results", 0),
            "kept": clear_stats.get("kept", 0),
            "chars_freed": clear_stats.get("cleared_chars", 0),
            "total_chars_before": clear_stats.get("total_chars_before", 0),
            "cleared_files_count": len(ctx.cleared_files or []),
            "enabled": clear_stats.get("enabled", True),
            "skipped": clear_stats.get("skipped", False),
            "reason": clear_stats.get("reason", ""),
        }
        # Thinking strip metrics
        think_stats = compress_stats.get("think", {})
        if think_stats.get("stripped"):
            result["think_strip"] = {"stripped": think_stats["stripped_count"]}
        return result


# ============================================================================
# Stage 8: ToolLoopDetector — scan last assistant messages for repeated tool calls
# ============================================================================

class ToolLoopDetector(PipelineStage):
    """Stage 8: Scan last 15 assistant messages for exact (tool, args) repeats
    and pattern repeats (same text prefix + same tool set).

    Populates ctx.max_run, ctx.consecutive, and ctx.pattern_tool_name.
    These are consumed by LoopIntervention (stage 11).
    """

    name = "tool_loop_detector"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        consecutive = {}
        max_run = 0
        pattern_run = 0
        last_pattern = None
        pattern_tool_name = None

        tail_assistant = [m for m in ctx.messages if m.get("role") == "assistant"][-15:]
        for msg in tail_assistant:
            content = msg.get("content", "")
            if isinstance(content, list):
                tool_names_in_msg = []
                text_parts = []
                for block in content:
                    if block.get("type") == "tool_use":
                        name = block.get("name", "")
                        tool_names_in_msg.append(name)
                        inp = block.get("input", {})
                        args_str = json.dumps(inp, sort_keys=True, ensure_ascii=False) if isinstance(inp, dict) else str(inp)
                        if name in ("Write", "Edit") and isinstance(inp, dict):
                            fp = inp.get("file_path") or inp.get("path") or ""
                            if fp:
                                args_str = f"file={fp}"
                        key = f"{name}:{args_str}"
                        consecutive[key] = consecutive.get(key, 0) + 1
                        max_run = max(max_run, consecutive[key])
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                pattern = ("".join(text_parts)[:200], tuple(sorted(set(tool_names_in_msg))))
                if pattern == last_pattern and pattern[1]:
                    pattern_run += 1
                    if pattern_run > max_run:
                        max_run = pattern_run
                        pattern_tool_name = tool_names_in_msg[0] if tool_names_in_msg else "unknown"
                else:
                    pattern_run = 1
                    last_pattern = pattern
            else:
                consecutive = {}
                pattern_run = 0
                last_pattern = None

        if max_run > 1:
            log(f"  -> Loop scan: max_run={max_run} (tail={len(tail_assistant)} msgs)")

        ctx.consecutive = consecutive
        ctx.max_run = max_run
        ctx.pattern_tool_name = pattern_tool_name
        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        # Base metrics emitted here; LoopIntervention may override with level info
        return {"max_run": ctx.max_run, "text_loop_run": 0, "is_text_loop": False}


# ============================================================================
# Stage 9: TextLoopDetector — detect repeated similar text output
# ============================================================================

class TextLoopDetector(ConditionalStage):
    """Stage 9: Detect repeated semantically-similar text in assistant responses.

    Condition: PROXY_TEXT_LOOP_ENABLED is true.
    Uses bigram Jaccard similarity to detect text loops.
    Merges results with ToolLoopDetector's max_run.
    """

    name = "text_loop_detector"

    def should_run(self, ctx: PipelineContext) -> bool:
        return _ps.PROXY_TEXT_LOOP_ENABLED

    def process(self, ctx: PipelineContext) -> PipelineContext:
        loop_detection = _import_loop_detection()
        tail_assistant = [m for m in ctx.messages if m.get("role") == "assistant"][-15:]
        text_loop_run, is_text_loop = loop_detection._detect_text_loop(tail_assistant)

        if text_loop_run > 1:
            log(f"  -> Text loop scan: text_run={text_loop_run} (threshold={_ps.PROXY_TEXT_LOOP_THRESHOLD}, "
                f"similarity>={_ps.PROXY_TEXT_LOOP_SIMILARITY})")

        # Merge with tool loop: take the higher count
        if text_loop_run > ctx.max_run:
            ctx.max_run = text_loop_run

        ctx.is_text_loop = is_text_loop
        ctx.text_loop_run = text_loop_run
        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        # Merged metrics (text_loop info now included)
        return {"max_run": ctx.max_run, "text_loop_run": ctx.text_loop_run, "is_text_loop": ctx.is_text_loop}


# ============================================================================
# Stage 10: SessionLoopState — persist loop level across requests
# ============================================================================

class SessionLoopState(PipelineStage):
    """Stage 10: Read session-level loop state and inject persistent warning.

    If the session was previously at loop level 2+ but current max_run is below
    threshold, inject a warning asking the model to change approach.
    """

    name = "session_loop_state"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        session_id = ctx.session_id
        session_loop = _ps._LOOP_SESSION_STATE.get(session_id, {"level": 0, "triggers": 0})

        if session_loop["level"] >= 2 and ctx.max_run < _ps.PROXY_LOOP_THRESHOLD:
            log(f"  -> Session had Level {session_loop['level']}, injecting persistent warning (max_run={ctx.max_run})")
            ctx.messages.append({
                "role": "user",
                "content": [{"type": "text", "text":
                    f"[System: You were previously looping and had tools restricted. "
                    f"Continue with a DIFFERENT approach. Do NOT repeat previous actions.]"
                }]
            })

        return ctx


# ============================================================================
# Stage 11: LoopIntervention — escalate loop response
# ============================================================================

class LoopIntervention(PipelineStage):
    """Stage 11: Apply loop intervention based on detected repetition levels.

    Always runs (to guarantee \"loop_detect\" metrics), but only intervenes
    when max_run >= PROXY_LOOP_THRESHOLD or is_text_loop.

    Levels:
      - Level 0: no-op (metrics only)
      - Level 1: inject hint message, keep all tools
      - Level 2: remove high-count tools + inject warning
      - Level 3: strip ALL tools + force plain text

    Mutates ctx.messages, ctx.body["tools"], and _LOOP_SESSION_STATE.
    """

    name = "loop_detect"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        # Only intervene above threshold
        if ctx.max_run < _ps.PROXY_LOOP_THRESHOLD and not ctx.is_text_loop:
            # No intervention needed. Reset session loop state if it was previously set.
            if ctx.session_id:
                session_loop = _ps._LOOP_SESSION_STATE.get(ctx.session_id, {"level": 0, "triggers": 0})
                if session_loop["level"] > 0:
                    _ps._LOOP_SESSION_STATE[ctx.session_id] = {"level": 0, "triggers": session_loop.get("triggers", 0)}
            ctx.loop_level = 0
            ctx.loop_tool_name = None
            return ctx

        loop_detection = _import_loop_detection()
        raw_tools = ctx.body.get("tools")

        new_messages, new_tools, loop_level, loop_tool_name = loop_detection._apply_loop_intervention(
            ctx.messages, raw_tools, ctx.max_run, ctx.consecutive,
            pattern_tool_name=ctx.pattern_tool_name,
            is_text_loop=ctx.is_text_loop,
            text_loop_run=ctx.text_loop_run,
        )

        if loop_level >= 1:
            if loop_level >= 2 and raw_tools is not None and new_tools != raw_tools:
                ctx.body["tools"] = new_tools
            ctx.messages = new_messages
            if loop_tool_name == "text_loop":
                log(f"  -> TEXT LOOP LEVEL {loop_level}: text_run={ctx.text_loop_run} max_run={ctx.max_run}")
            else:
                log(f"  -> LOOP LEVEL {loop_level}: tool={loop_tool_name} max_run={ctx.max_run} "
                    f"consecutive={{k: v for k, v in ctx.consecutive.items() if v >= _ps.PROXY_LOOP_THRESHOLD}}")
            if loop_level == 2:
                if loop_tool_name != "text_loop":
                    removed = sorted(set(
                        k.split(":")[0] for k, v in ctx.consecutive.items()
                        if v >= _ps.PROXY_LOOP_THRESHOLD
                    ))
                    log(f"    removed tools: {removed} ({len(new_tools)} remaining)")
            elif loop_level == 3:
                log(f"    ALL tools stripped — force plain text response")

            # Persist loop state
            if ctx.session_id:
                session_loop = _ps._LOOP_SESSION_STATE.get(ctx.session_id, {"level": 0, "triggers": 0})
                _ps._LOOP_SESSION_STATE[ctx.session_id] = {
                    "level": loop_level,
                    "triggers": session_loop.get("triggers", 0) + 1,
                }
        else:
            # Reset session loop state if we were looping but now below threshold
            if ctx.session_id:
                session_loop = _ps._LOOP_SESSION_STATE.get(ctx.session_id, {"level": 0, "triggers": 0})
                if session_loop["level"] > 0 and ctx.max_run < _ps.PROXY_LOOP_THRESHOLD:
                    _ps._LOOP_SESSION_STATE[ctx.session_id] = {"level": 0, "triggers": session_loop.get("triggers", 0)}

        ctx.loop_level = loop_level
        ctx.loop_tool_name = loop_tool_name
        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        return {
            "max_run": ctx.max_run,
            "level": ctx.loop_level,
            "tool": ctx.loop_tool_name,
            "text_loop_run": ctx.text_loop_run,
            "is_text_loop": ctx.is_text_loop,
        }


# ============================================================================
# Stage 12: RereadDetector — detect Read tool calls targeting cleared files
# ============================================================================

class RereadDetector(PipelineStage):
    """Stage 12: Detect when the last assistant's Read call targets a cleared file.

    Checks whether any Read tool_use in the last assistant message references
    a file from ctx.cleared_files (populated by ContentCompressor, stage 7).

    When detected, injects a HARD BLOCK user message asking the model to
    use existing knowledge instead of re-reading unchanged files.
    """

    name = "re_read"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        cleared_files = ctx.cleared_files or []
        re_read_info = {"count": 0, "cleared_files": len(cleared_files), "rate_pct": 0.0}

        if cleared_files:
            re_read_count = 0
            re_read_targets = set()
            last_assistant = None
            for msg in reversed(ctx.messages):
                if msg.get("role") == "assistant":
                    last_assistant = msg
                    break

            if last_assistant:
                content = last_assistant.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_use" and block.get("name") == "Read":
                            inp = block.get("input", {})
                            if isinstance(inp, dict):
                                fp = inp.get("file_path", inp.get("path", ""))
                                if fp in cleared_files:
                                    re_read_count += 1
                                    re_read_targets.add(fp)

            if re_read_count > 0:
                msg_converter = _import_message_converter()
                rate = msg_converter._compute_re_read_rate(len(re_read_targets), len(cleared_files))
                re_read_info = {
                    "count": re_read_count,
                    "cleared_files": len(cleared_files),
                    "re_read_files": len(re_read_targets),
                    "rate_pct": round(rate, 1),
                }
                log(f"  -> Re-read detected: {re_read_count} Read calls targeting "
                    f"{len(re_read_targets)}/{len(cleared_files)} cleared files (rate={rate:.1f}%)")

                # P0-FIX: Hard-block re-reads
                blocked_files = ", ".join(sorted(re_read_targets))
                ctx.messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text":
                        f"[System: HARD BLOCK — Read calls to the following files were intercepted "
                        f"because their contents were previously cleared and have not changed: {blocked_files}. "
                        f"DO NOT attempt to read these files again. Use your existing knowledge or "
                        f"proceed without re-reading. If you need file content, ask the user explicitly.]"
                    }]
                })
                log(f"  -> Re-read HARD BLOCK injected for: {blocked_files}")

        ctx.re_read_info = re_read_info
        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        return ctx.re_read_info


# ============================================================================
# Stage 13: DateNormalizer — stabilize date placeholder for KV cache
# ============================================================================

class DateNormalizer(PipelineStage):
    """Stage 13: Normalize system-reminder date to a placeholder.

    Replaces 'Today's date is YYYY/MM/DD.' with 'Today's date is DATE_PLACEHOLDER.'
    in msg0 to stabilize the prefix for KV cache hits across requests on different days.
    """

    name = "date_normalizer"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        import re
        messages = ctx.messages
        if messages and messages[0].get("role") == "user":
            content = messages[0].get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        new_text = re.sub(
                            r"Today's date is \d{4}/\d{2}/\d{2}\.",
                            "Today's date is DATE_PLACEHOLDER.",
                            text,
                        )
                        if new_text != text:
                            block["text"] = new_text
                            log(f"  -> Standardized date in msg0 block")
            else:
                new_content = re.sub(
                    r"Today's date is \d{4}/\d{2}/\d{2}\.",
                    "Today's date is DATE_PLACEHOLDER.",
                    str(content),
                )
                if new_content != content:
                    messages[0]["content"] = new_content
                    log(f"  -> Standardized date in msg0")

        return ctx


# ============================================================================
# Stage 14: ContextTruncator — truncate messages to fit context budget
# ============================================================================

class ContextTruncator(ConditionalStage):
    """Stage 14: Truncate messages when context exceeds budget.

    Condition: PROXY_CTX_LIMIT_ENABLED is true (disabled for cloud backends).

    Calls truncate_messages_if_needed() from truncation.py. Supports multiple
    strategies: rounds, fifo, smart, char.
    """

    name = "truncate"

    def should_run(self, ctx: PipelineContext) -> bool:
        return _ps.PROXY_CTX_LIMIT_ENABLED

    def process(self, ctx: PipelineContext) -> PipelineContext:
        truncation = _import_truncation()
        messages, trunc_stats = truncation.truncate_messages_if_needed(
            ctx.messages,
            session_id=ctx.session_id,
            keep_rounds=ctx.stage_config.get("truncate_rounds") if ctx.stage_config else None,
        )
        ctx.messages = messages
        ctx.trunc_stats = trunc_stats

        if trunc_stats.get("truncated"):
            strategy = trunc_stats.get("strategy", "char")
            if strategy == "rounds":
                chars_after = trunc_stats.get("chars", trunc_stats.get("estimated_tokens", "?"))
                actual_r = trunc_stats.get("actual_keep_rounds", "?")
                comp = trunc_stats.get("compression", "folded")
                adaptive = trunc_stats.get("adaptive_rounds", "")
                stage_r = trunc_stats.get("stage_keep_rounds", "")
                budget_iter = trunc_stats.get("budget_iterations", 0)
                extra = ""
                if adaptive:
                    extra += f", adaptive={adaptive}"
                if stage_r:
                    extra += f", stage_rounds={stage_r}"
                if budget_iter:
                    extra += f", budget_iter={budget_iter}"
                log(f"  -> Context truncation (rounds): {trunc_stats['dropped_messages']} messages dropped, "
                    f"{trunc_stats.get('kept_messages', '?')} kept "
                    f"(rounds={actual_r}, ~{chars_after} chars, budget={_ps.PROXY_CHARS_EXPANSION:,}"
                    f", compress={comp}{extra})")
            elif strategy == "fifo":
                log(f"  -> Context truncation (fifo): {trunc_stats['dropped_messages']} messages dropped, "
                    f"{trunc_stats.get('kept_messages', '?')} kept (limit={_ps.PROXY_CTX_KEEP_MESSAGES})")
            elif strategy == "smart":
                smart_compressed = trunc_stats.get("compressed_assistants", 0)
                smart_kept_chars = trunc_stats.get("kept_chars", 0)
                smart_budget = trunc_stats.get("budget_chars", _ps.PROXY_CHARS_EXPANSION)
                log(f"  -> Context truncation (smart): {trunc_stats['dropped_messages']} messages dropped, "
                    f"{trunc_stats.get('kept_messages', '?')} kept, "
                    f"{smart_compressed} assistant reasoning compressed "
                    f"({smart_kept_chars:,} chars, budget={smart_budget:,})")
            else:
                log(f"  -> Context truncation (char): {trunc_stats['dropped_messages']} messages dropped, "
                    f"{trunc_stats['dropped_chars']:,} chars removed "
                    f"({trunc_stats['chars_before']:,} -> {trunc_stats['chars_after']:,})")
        elif not trunc_stats.get("enabled"):
            log(f"  -> Context truncation: disabled ({_ps.BACKEND_TYPE} backend)")
        elif trunc_stats.get("enabled") and not trunc_stats.get("truncated") and not trunc_stats.get("skipped"):
            log(f"  -> Context truncation: active (strategy={trunc_stats.get('strategy', '?')})")

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        trunc_stats = ctx.trunc_stats or {}
        if not trunc_stats.get("enabled"):
            return {"applied": False, "enabled": False}
        if trunc_stats.get("skipped"):
            return {"applied": False, "enabled": True, "skipped": True}
        if trunc_stats.get("truncated"):
            strategy = trunc_stats.get("strategy", "char")
            metrics = {
                "applied": True,
                "triggered": True,
                "strategy": strategy,
                "dropped": trunc_stats.get("dropped_messages", 0),
                "kept": trunc_stats.get("kept_messages", 0),
            }
            if strategy == "rounds":
                metrics["compression"] = trunc_stats.get("compression", "folded")
                metrics["chars_after"] = trunc_stats.get("chars", 0)
                metrics["budget_chars"] = _ps.PROXY_CHARS_EXPANSION
                metrics["rounds"] = trunc_stats.get("actual_keep_rounds", "?")
                metrics["adaptive_rounds"] = trunc_stats.get("adaptive_rounds", "")
                metrics["budget_iterations"] = trunc_stats.get("budget_iterations", 0)
            elif strategy == "smart":
                metrics["compressed_assistants"] = trunc_stats.get("compressed_assistants", 0)
                metrics["chars_after"] = trunc_stats.get("kept_chars", 0)
                metrics["budget_chars"] = trunc_stats.get("budget_chars", _ps.PROXY_CHARS_EXPANSION)
            return metrics
        if not trunc_stats.get("enabled") and not trunc_stats.get("truncated") and not trunc_stats.get("skipped"):
            return {"applied": False, "enabled": True, "strategy": trunc_stats.get("strategy", "")}
        return {"applied": False, "enabled": True}


# ============================================================================
# Stage 15: HighDropRatioNotice — warn when context loss is severe
# ============================================================================

class HighDropRatioNotice(PipelineStage):
    """Stage 15: Inject a context-loss notice when >85% of messages were dropped.

    DEF-107: Prevents silent context loss that degrades response quality.
    """

    name = "high_drop_ratio_notice"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        trunc_stats = ctx.trunc_stats or {}
        if trunc_stats.get("truncated"):
            dropped = trunc_stats.get("dropped_messages", 0)
            kept = trunc_stats.get("kept_messages", 0)
            if kept + dropped > 0 and dropped / (kept + dropped) > 0.85:
                notice = (
                    f"[System: Context severely truncated — "
                    f"{dropped} of {dropped + kept} messages dropped. "
                    f"Consider using /compact or starting a new session "
                    f"to maintain context quality.]"
                )
                ctx.messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": notice}],
                })
                ctx.high_drop_notice_injected = True
                log(f"  -> High drop ratio notice injected ({dropped}/{dropped + kept} = "
                    f"{dropped / (kept + dropped) * 100:.0f}%)")

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        if ctx.high_drop_notice_injected:
            trunc_stats = ctx.trunc_stats or {}
            return {
                "high_drop_ratio": True,
                "dropped": trunc_stats.get("dropped_messages", 0),
                "kept": trunc_stats.get("kept_messages", 0),
            }
        return None


# ============================================================================
# Stage 17: OOMSafetyFIFO — iterative FIFO truncation to prevent OOM
# ============================================================================

class OOMSafetyFIFO(ConditionalStage):
    """Stage 17: Iterative message dropping to stay within OOM-safe limits.

    Only enabled for local backends at OOM_DANGER/PRE_TRUNC stages.
    Uses char/token estimation to iteratively drop middle messages while
    preserving head and tail.

    Condition: stage_config["oom_safety"] is True, not cloud, not rounds strategy.
    """

    name = "oom_safety"

    def should_run(self, ctx: PipelineContext) -> bool:
        if not ctx.stage_config:
            return False
        return (ctx.stage_config.get("oom_safety", False)
                and not _ps.IS_CLOUD
                and _ps.PROXY_CTX_TRUNCATE_STRATEGY != "rounds")

    def process(self, ctx: PipelineContext) -> PipelineContext:
        msg_converter = _import_message_converter()

        body = ctx.body
        _sys = body.get("system")
        _tools = body.get("tools")
        static_chars = 0
        if _sys:
            if isinstance(_sys, list):
                static_chars += sum(len(b.get("text", "")) for b in _sys if b.get("type") == "text")
            else:
                static_chars += len(str(_sys))
        if _tools:
            static_chars += sum(len(json.dumps(t, ensure_ascii=False)) for t in _tools if isinstance(t, dict))

        iteration = 0
        raw_messages = ctx.messages
        while True:
            est_chars = msg_converter._estimate_message_chars(raw_messages) + static_chars
            est_tokens = msg_converter._estimate_tokens_dynamic(raw_messages) + int(
                static_chars / max(_ps.PROXY_CTX_TOKEN_RATIO, 0.1)
            )
            if (est_chars <= _ps.PROXY_CHARS_OOM_DANGER and est_tokens <= _ps.PROXY_OOM_SAFE_TOKENS) or len(raw_messages) <= 4:
                break
            iteration += 1
            keep = max(_ps.PROXY_CTX_KEEP_HEAD + _ps.PROXY_CTX_KEEP_TAIL, 4)
            if len(raw_messages) > keep:
                dropped = len(raw_messages) - keep
                raw_messages[:] = raw_messages[:_ps.PROXY_CTX_KEEP_HEAD] + raw_messages[-(keep - _ps.PROXY_CTX_KEEP_HEAD):]
                log(f"  -> OOM safety (iter {iteration}): est_chars={est_chars}, est_tokens={est_tokens}, "
                    f"dropped {dropped} msgs, kept {len(raw_messages)}")
            else:
                break

        ctx.oom_iterations = iteration
        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        if ctx.oom_iterations > 0:
            return {
                "triggered": True,
                "chars": _ps.PROXY_CHARS_OOM_DANGER,
                "limit_tokens": _ps.PROXY_OOM_SAFE_TOKENS,
                "iterations": ctx.oom_iterations,
                "final_msgs": len(ctx.messages),
            }
        return None


# ============================================================================
# Stage 16: MessageHashDebug — diagnostic: hash first two messages
# ============================================================================

class MessageHashDebug(PipelineStage):
    """Stage 16: Compute and log MD5 hashes of the first two messages.

    Diagnostic-only.  No mutation.  Helps with prefix-stability debugging.
    """

    name = "message_hash_debug"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        import hashlib
        messages = ctx.messages
        if messages:

            def _msg_hash(m):
                c = m.get("content", "")
                if isinstance(c, list):
                    c = "".join(b.get("text", "") for b in c if b.get("type") == "text")
                elif not isinstance(c, str):
                    c = str(c)
                return hashlib.md5((m.get("role", "") + ":" + c).encode()).hexdigest()[:8]

            h0 = _msg_hash(messages[0]) if len(messages) > 0 else "none"
            h1 = _msg_hash(messages[1]) if len(messages) > 1 else "none"
            log(f"  -> Msg hashes: msg0={h0}, msg1={h1}, total_msgs={len(messages)}")
        return ctx


# ============================================================================
# Stage 18: PrefixRatioComputer — compute KV cache prefix stability
# ============================================================================

class PrefixRatioComputer(PipelineStage):
    """Stage 18: Compute common prefix ratio against previous request in session.

    Quantifies KV cache prefix stability. High ratio = stable prefix = better
    cache hits.  Writes a snapshot of current messages to _SESSION_LAST_MESSAGES
    for the next request's comparison.

    Calls _compute_common_prefix_ratio() from message_converter.py.
    """

    name = "common_prefix_ratio"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        msg_converter = _import_message_converter()
        session_id = ctx.session_id

        previous_messages = _ps._SESSION_LAST_MESSAGES.get(session_id) if session_id else None
        ratio = msg_converter._compute_common_prefix_ratio(ctx.messages, previous_messages or [])
        ctx.common_prefix_ratio = ratio

        log(f"  -> Common prefix ratio: {ratio:.2%} "
            f"(current={len(ctx.messages)} msgs, "
            f"previous={len(previous_messages) if previous_messages else 0} msgs)")

        if session_id:
            with _ps._state_lock:
                # Bound memory for the session message cache
                if len(_ps._SESSION_LAST_MESSAGES) > 1000:
                    _ps._SESSION_LAST_MESSAGES.pop(next(iter(_ps._SESSION_LAST_MESSAGES)), None)
                _ps._SESSION_LAST_MESSAGES[session_id] = [dict(m) for m in ctx.messages]

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        if _ps.PROXY_METRICS_ENABLED:
            session_id = ctx.session_id
            previous_messages = _ps._SESSION_LAST_MESSAGES.get(session_id) if session_id else None
            return {
                "ratio": ctx.common_prefix_ratio,
                "current_msgs": len(ctx.messages),
                "previous_msgs": len(previous_messages) if previous_messages else 0,
            }
        return None


# ============================================================================
# Stage 19: ToolPairingRepair — fix orphaned tool_use/tool_result blocks
# ============================================================================

class ToolPairingRepair(PipelineStage):
    """Stage 19: Repair orphaned tool_use/tool_result blocks after truncation.

    Calls _fix_tool_pairings() from truncation.py. This must run after all
    pipeline modifications that could create orphaned pairs (truncation, loop
    intervention, compression).
    """

    name = "tool_pairing_repair"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        truncation = _import_truncation()
        ctx.messages = truncation._fix_tool_pairings(ctx.messages)
        return ctx


# ============================================================================
# Stage 20: FormatConverter — Anthropic → OpenAI format + tool conversion
# ============================================================================

class FormatConverter(PipelineStage):
    """Stage 20: Convert messages to OpenAI format and build the backend request body.

    Steps:
      1. Convert Anthropic messages → OpenAI messages
      2. Handle system prompt (prepend as system message)
      3. Build openai_body dict (model, messages, max_tokens, temperature, ...)
      4. Disable thinking for DeepSeek flash models (cloud)
      5. Filter tools via _filter_tools() if enabled
      6. Convert tools and tool_choice to OpenAI format

    Output: ctx.openai_messages + ctx.openai_body (consumed by BackendDispatcher).
    """

    name = "format_converter"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        msg_converter = _import_message_converter()
        tool_filter = _import_tool_filter()

        # 1. Convert messages
        messages = msg_converter.convert_anthropic_messages_to_openai(ctx.messages)

        # 2. Handle system prompt
        body = ctx.body
        system_msg = body.get("system")
        if system_msg:
            if isinstance(system_msg, list):
                system_text = "\n".join([b.get("text", "") for b in system_msg if b.get("type") == "text"])
            else:
                system_text = str(system_msg)
            if system_text.strip():
                messages = [{"role": "system", "content": system_text}] + messages

        # 3. Build OpenAI body
        openai_body = {
            "model": _ps.MODEL_NAME,
            "messages": messages,
            "max_tokens": body.get("max_tokens", 4096),
            "temperature": body.get("temperature", 0.7),
            "stream": ctx.is_stream,
        }
        if "top_p" in body:
            openai_body["top_p"] = body["top_p"]
        if "stop_sequences" in body:
            openai_body["stop"] = body["stop_sequences"]

        # 4. DeepSeek flash: disable thinking
        if _ps.IS_CLOUD and "flash" in _ps.MODEL_NAME.lower():
            openai_body["thinking"] = {"type": "disabled"}

        # 5. Tool filtering
        raw_tools = body.get("tools")
        if raw_tools and _ps.PROXY_TOOL_FILTER_ENABLED:
            tc_raw = body.get("tool_choice")
            tc_name = None
            if isinstance(tc_raw, dict) and tc_raw.get("type") == "tool":
                tc_name = tc_raw.get("name", "")
            raw_tools, tf_stats = tool_filter._filter_tools(
                raw_tools, ctx.messages,
                recent_rounds=_ps.PROXY_TOOL_FILTER_RECENT,
                tool_choice_name=tc_name,
            )
            if tf_stats.get("filtered"):
                body["tools"] = raw_tools
                recent_names = tf_stats.get("recent_tools", [])
                recent_info = f", recent_names={recent_names}" if recent_names else ""
                filtered_out = tf_stats.get("filtered_out", [])
                filtered_info = f", removed={filtered_out}" if filtered_out else ""
                log(f"  -> Tool filter: {tf_stats['original']} -> {tf_stats['kept']} "
                    f"(always={tf_stats['always_keep']}, recent={tf_stats['recent_only']}, "
                    f"scanned={tf_stats.get('scanned_assistant', 0)}{recent_info}{filtered_info})")

        # 6. Convert tools and tool_choice
        tools = msg_converter.convert_anthropic_tools_to_openai(body.get("tools"))
        if tools:
            openai_body["tools"] = tools
            log(f"  -> Tools: {[t['function']['name'] for t in tools]}")

        tool_choice = msg_converter.convert_anthropic_tool_choice_to_openai(body.get("tool_choice"))
        if tool_choice:
            openai_body["tool_choice"] = tool_choice

        ctx.openai_messages = messages
        ctx.openai_body = openai_body
        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        result = {}
        # Tool filter metrics are handled inside process — but we also want to capture filter stats
        raw_tools = ctx.body.get("tools")
        if raw_tools and _ps.PROXY_TOOL_FILTER_ENABLED:
            # We don't have tf_stats here — but the key metric is whether tools were filtered
            if ctx.body.get("tools") != ctx.raw_tools_orig:
                result["tool_filter"] = {
                    "applied": True,
                    "original": len(ctx.raw_tools_orig),
                    "kept": len(ctx.body.get("tools", [])),
                }
        return result if result else None


# ============================================================================
# Stage 21: BackendDispatcher — send request to backend and handle response
# ============================================================================

class BackendDispatcher(PipelineStage):
    """Stage 21: Forward the OpenAI-format request to the backend LLM.

    Acquires _llama_lock (concurrency semaphore), sends an HTTP POST to
    {LLAMA_BASE}/chat/completions, and dispatches the response to the
    handler's streaming or non-streaming handler methods.

    Constructor args:
      - llama_lock: threading.Semaphore for concurrency control
      - handler: the Handler instance for writing the HTTP response

    HTTPError is caught and handled inline (calls handler._respond_json).
    """

    name = "backend_dispatcher"

    def __init__(self, llama_lock=None, handler=None):
        self._llama_lock = llama_lock
        self._handler = handler
        self._backend_status = None

    def process(self, ctx: PipelineContext) -> PipelineContext:
        log(f"  -> Forwarding to {_ps.LLAMA_BASE}/chat/completions")

        try:
            with self._llama_lock:
                req = urllib.request.Request(
                    f"{_ps.LLAMA_BASE}/chat/completions",
                    data=json.dumps(ctx.openai_body).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {_ps.LLAMA_API_KEY}",
                    },
                    method="POST",
                )
                resp = urllib.request.urlopen(req, timeout=_ps.PROXY_BACKEND_TIMEOUT)
                self._backend_status = resp.status
                log(f"  <- backend status: {resp.status}")

                if ctx.is_stream:
                    self._handler._handle_streaming_response(resp, ctx.body)
                else:
                    self._handler._handle_non_streaming_response(resp, ctx.body)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8")
            self._backend_status = e.code
            log(f"  <- backend error: {e.code} - {err[:500]}")
            self._handler._respond_json({"error": {"message": err}}, e.code)

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        return {
            "backend_status": self._backend_status,
            "stream": 1 if ctx.is_stream else 0,
        }
