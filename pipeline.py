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
import collections
import io
import json
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import proxy_state as _ps
import model_registry
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


# Phase 3+ (建议3): coarse character buckets for latency long-tail analysis.
# Boundaries aligned with smart-router lifecycle thresholds (init/growth/
# saturation/oom).  Used in metrics JSONL so the analyzer can group
# `dispatch_latency_ms` by request size without re-parsing the body.
_CHAR_BUCKET_BOUNDARIES = (
    (10000,    "xs"),    # 0–10K
    (50000,    "sm"),    # 10K–50K
    (150000,   "md"),    # 50K–150K
    (400000,   "lg"),    # 150K–400K
    (1_000_000, "xl"),   # 400K–1M
)
_CHAR_BUCKET_DEFAULT = "xxl"          # > 1M


def _char_bucket(total_chars: int) -> str:
    """Return a coarse size code (xs/sm/md/lg/xl/xxl) for the input character count.

    Empty string or 0 → 'xs' (numeric falsy kept as zero). Other non-numeric
    inputs (None, non-numeric strings) → 'unknown'.
    """
    if total_chars is None:
        return "unknown"
    try:
        n = int(total_chars)
    except (TypeError, ValueError):
        # Empty string was historically treated as 0 / xs.
        if total_chars == "" or total_chars == 0 or total_chars is False:
            return "xs"
        return "unknown"
    for boundary, label in _CHAR_BUCKET_BOUNDARIES:
        if n < boundary:
            return label
    return _CHAR_BUCKET_DEFAULT


def _log_cloud_error(ctx, status, response_body, exc_info=None):
    """Log full request/response bodies for cloud API errors to a dedicated JSONL file.

    The log path rotates daily: logs/cloud_errors_YYYYMMDD.jsonl (under the
    project root by default). Override via PROXY_CLOUD_ERROR_LOG_DIR env var.
    The request body is deep-copied and sanitized (api_key masked) before writing.
    """
    try:
        log_dir = os.environ.get("PROXY_CLOUD_ERROR_LOG_DIR")
        if not log_dir:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        log_path = os.path.join(log_dir, f"cloud_errors_{date_str}.jsonl")

        request_body = {}
        if hasattr(ctx, 'openai_body') and isinstance(ctx.openai_body, dict):
            request_body = json.loads(json.dumps(ctx.openai_body, ensure_ascii=False))
            # Sanitize any accidental api_key field.
            if "api_key" in request_body:
                request_body["api_key"] = "***"

        record = {
            "ts": datetime.now().isoformat(),
            "session_id": getattr(ctx, '_session_id', '') or getattr(ctx, 'request_id', ''),
            "request_id": getattr(ctx, 'request_id', ''),
            "route_target": getattr(ctx, '_route_target', ''),
            "route_reason": getattr(ctx, '_route_reason', ''),
            "status": status,
            "response_body": (response_body or "")[:2000],
            "request_body": request_body,
            "model": request_body.get("model", ""),
        }
        if exc_info:
            record["exception"] = exc_info

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as le:
        log(f"  -> Failed to write cloud error log: {le}")


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
    client_type: str = "unknown"

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

    # --- Route fields (Phase 1) ---
    _route_target: str = "local"          # "local" | "cloud" | "local_forced"
    _route_reason: str = ""               # decision reason tag
    _route_header_override: str = ""      # X-Proxy-Route-To header value
    _route_model_bias: str = ""           # model force direction ("prefer_cloud"/"prefer_local"/"")
    _route_actual_cost: float = 0.0       # actual cloud cost (post-request)
    _route_cloud_model: str = ""          # selected cloud model name
    _route_fallback_models: list = field(default_factory=list)  # catalog fallback chain
    _route_provider: str = ""             # provider name of the dispatched model
    _emergency_fallback: bool = False     # emergency fallback mode flag
    _agent_model_tier: str = "sonnet"     # Agent-selected tier ("opus"/"sonnet"/"haiku")


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
            try:
                ctx = stage.process(ctx)
            except Exception as e:
                log(f"  -> PIPELINE FAILURE at stage '{stage.name}': {type(e).__name__}: {e}")
                raise RuntimeError(f"Pipeline stage '{stage.name}' failed: {type(e).__name__}: {e}") from e
            elapsed = (time.monotonic() - t0) * 1000
            total_ms += elapsed
            if elapsed > slowest_ms:
                slowest_ms = elapsed
                slowest_name = stage.name
            data = stage.output_metrics(ctx)
            if data is not None:
                # Multi-key mode: if 2+ values are all dicts, write each key
                # separately (legacy support for ContentCompressor's old
                # semantic_compress + tool_clear + think_strip split).
                # Single-key dicts like {"compression": {...}} write under
                # stage.name so the metrics path is pipeline.<stage>.compression.
                if len(data) >= 2 and all(isinstance(v, dict) for v in data.values()):
                    for sub_key, sub_data in data.items():
                        admin._mc_put(sub_key, sub_data)
                else:
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
        log(f"  [REQ_SUMMARY] client={ctx.client_type} chars={ctx.total_chars} tools={tools_count}")

        # Structured REQ_SUMMARY
        from proxy_logging import log_structured
        log_structured("REQ_SUMMARY", client=ctx.client_type, chars=ctx.total_chars,
                       tools=tools_count, model=ctx.model, stream=ctx.is_stream)

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

        # Extract X-Proxy-Route-To header override (pre-extracted by Handler.do_POST)
        route_override = body.get("_x_proxy_route_to", "")
        ctx._route_header_override = route_override if route_override in ("local", "cloud") else ""

        # Classify Agent model tier from request body
        ctx._agent_model_tier = _classify_tier(body.get("model", ""))

        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        return {
            "msg_count": len(ctx.messages),
            "tool_count": len(ctx.tools_list or []),
            "input_chars": ctx.total_chars,
            "is_stream": 1 if ctx.is_stream else 0,
            "client_type": ctx.client_type,
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
# Stage 2.5: SmartRouter — decide local vs cloud backend
# ============================================================================

def _classify_tier(model_id: str) -> str:
    """Extract agent model tier from model ID.

    Uses exact match for known model IDs first, then substring match
    with protection against false positives (e.g. \"opus\" in \"my-opus-model\").
    """
    m = model_id.lower()
    # Exact known tiers
    if m in ("claude-opus-4-7", "claude-opus-4", "claude-3-opus-20240229"):
        return "opus"
    if m in ("claude-haiku-4-5", "claude-haiku-4", "claude-3-5-haiku-20241022"):
        return "haiku"
    # Loose substring match — protect against "sonnetopus" or "haiku-sonnet-mix"
    if "opus" in m and "sonnet" not in m:
        return "opus"
    if "haiku" in m and "sonnet" not in m:
        return "haiku"
    return "sonnet"


def _is_sensitive_request(ctx) -> bool:
    """Check if request touches sensitive file paths (best-effort).

    Only scans tool_use parameters (file_path / path fields).
    Does NOT scan free-text user/assistant message content.
    Patterns are compiled once and cached in proxy_state.
    """
    sensitive_re = _ps._compile_sensitive_patterns()
    if sensitive_re is None:
        return False
    for msg in ctx.messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_use":
                continue
            inp = block.get("input", {})
            if not isinstance(inp, dict):
                continue
            file_path = inp.get("file_path") or inp.get("path") or ""
            if file_path and sensitive_re.search(file_path):
                return True
    return False


class SmartRouter(PipelineStage):
    """Stage 2.5: Decide local vs cloud backend for this request.

    Position: after LifecycleClassifier (2) and DynamicMaxTokens (2),
              before ErrorTranslator (3) and all content-processing stages.

    Reads total_chars + stage from ctx.stage_config, memory pressure,
    session route state, and header override to make a 10-level
    priority decision.  Model ID preference adjusts thresholds but
    does NOT force route direction (safety always overrides).
    """

    name = "smart_router"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        target, reason = self._routing_decision(ctx)
        ctx._route_target = target
        ctx._route_reason = reason
        log(f"  -> [smart_router] {target} ({reason})")
        return ctx

    def _routing_decision(self, ctx):
        # Priority 0: routing disabled
        if not _ps.PROXY_ROUTE_ENABLED:
            return "local", "disabled"

        # Priority 0.5: Model ID preference → adjust thresholds (do NOT force route)
        requested_model = ctx.body.get("model", "")
        pref = _ps.MODEL_ROUTE_PREFERENCES.get(requested_model, {})
        effective_threshold = int(
            _ps.PROXY_ROUTE_THRESHOLD_CHARS * pref.get("threshold_factor", 1.0)
        )
        effective_memory_pct = _ps.PROXY_ROUTE_MEMORY_PCT + pref.get("memory_bias", 0)
        ctx._route_cloud_model = pref.get("cloud_model", _ps.PROXY_CLOUD_MODEL)
        # Phase B: catalog fallback chain (cross-provider degradation order)
        ctx._route_fallback_models = list(pref.get("fallback_models", []))
        ctx._agent_model_tier = _classify_tier(requested_model)

        # Store model force direction in ctx, don't return yet.
        # This allows Priority 0.6 (header override) to run first.
        behavior = pref.get("behavior", "prefer")
        route_bias = pref.get("route_bias", "auto")
        if behavior in ("force", "force_fallback") and route_bias in ("prefer_cloud", "prefer_local"):
            ctx._route_model_bias = route_bias
        else:
            ctx._route_model_bias = ""

        # Priority 0.6: X-Proxy-Route-To header (single-request, no session sticky)
        if ctx._route_header_override == "local":
            ctx._route_model_bias = ""  # header explicitly overrides model bias
            return "local", "header_override"
        if ctx._route_header_override == "cloud":
            ctx._route_model_bias = ""
            return "cloud", "header_override"
        # M-1: log warning for invalid header values
        if ctx._route_header_override:
            log(f"  [warn] Invalid X-Proxy-Route-To value: {ctx._route_header_override!r} — ignored")

        # Priority 0.55: Model force bias (after header check, so header always wins)
        if ctx._route_model_bias == "prefer_cloud":
            if behavior == "force_fallback":
                return "cloud", f"model_forced_fallback_cloud({requested_model}->{ctx._route_cloud_model})"
            return "cloud", f"model_forced_cloud({requested_model}->{ctx._route_cloud_model})"
        if ctx._route_model_bias == "prefer_local":
            return "local", f"model_forced_local({requested_model})"

        session_id = ctx.session_id

        # Priority 1: Cloud cooldown expiration cleanup.
        # Cooldown is now a *preference* for local, not a hard lock: safety
        # conditions (memory pressure, context size, lifecycle stage) can still
        # override it and route to cloud.
        cooldown_active = False
        if session_id:
            with _ps._state_lock:
                cooldown_start = _ps._cloud_cooldown_start.get(session_id)
            if cooldown_start:
                elapsed = time.monotonic() - cooldown_start
                if elapsed < _ps.PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS:
                    cooldown_active = True
                else:
                    with _ps._state_lock:
                        _ps._cloud_fail_count.pop(session_id, None)
                        _ps._cloud_cooldown_start.pop(session_id, None)
                        _ps._SESSION_ROUTE_MAP.pop(session_id, None)
                        _ps._SESSION_ROUTE_FORCE_SOURCE.pop(session_id, None)
                        _ps._ROUTE_NOTIFIED_SESSIONS.discard(session_id)
                        _ps._SESSION_BELOW_THRESHOLD.pop(session_id, None)

        # Compute the natural target ignoring cooldown-induced local_forced
        target, reason = self._natural_routing_decision(
            ctx, session_id, effective_threshold, effective_memory_pct
        )

        # Cooldown acts as a local preference: safety overrides still route to cloud.
        if target == "cloud":
            if cooldown_active:
                # Reset failure count so the cloud attempt gets a clean slate.
                with _ps._state_lock:
                    _ps._cloud_fail_count.pop(session_id, None)
                return target, f"{reason}_cooldown_override"
            return target, reason

        # target == "local"
        if cooldown_active and not reason.startswith(("session_force_local", "daily_budget_exceeded")):
            return "local", "cloud_cooldown_active"
        return target, reason

    def _natural_routing_decision(self, ctx, session_id, effective_threshold, effective_memory_pct):
        """Determine route target based on session state, memory, threshold and lifecycle.

        Does NOT consider cloud cooldown; cooldown is applied by the caller.
        """
        # Priority 2/3: Session-level route state
        if session_id:
            with _ps._state_lock:
                session_route = _ps._SESSION_ROUTE_MAP.get(session_id)
                force_source = _ps._SESSION_ROUTE_FORCE_SOURCE.get(session_id)
            if session_route == "cloud":
                SUGGESTION1_RETURN_ROUNDS = _ps.PROXY_ROUTE_STICKY_RETURN_ROUNDS
                SUGGESTION1_RETURN_RATIO = _ps.PROXY_ROUTE_STICKY_RETURN_RATIO
                # In sticky default mode, once cloud, forever cloud — no early
                # return even if context drops. This is the existing behavior.
                if _ps.PROXY_ROUTE_STICKY:
                    return "cloud", "session_already_cloud"
                # Non-sticky: allow session to return to local if context has
                # dropped below threshold for N consecutive requests.  This
                # saves cloud cost when an agentic session's early rounds were
                # large but subsequent rounds settled to short replies.
                total_chars = (
                    ctx.stage_config.get("total_chars", 0)
                    if ctx.stage_config else ctx.total_chars
                )
                is_below = total_chars <= int(
                    effective_threshold * SUGGESTION1_RETURN_RATIO
                )
                with _ps._state_lock:
                    if is_below:
                        _ps._SESSION_BELOW_THRESHOLD[session_id] = (
                            _ps._SESSION_BELOW_THRESHOLD.get(session_id, 0) + 1
                        )
                    else:
                        _ps._SESSION_BELOW_THRESHOLD[session_id] = 0
                    below_count = _ps._SESSION_BELOW_THRESHOLD.get(session_id, 0)
                if below_count >= SUGGESTION1_RETURN_ROUNDS:
                    with _ps._state_lock:
                        _ps._SESSION_ROUTE_MAP.pop(session_id, None)
                        _ps._SESSION_BELOW_THRESHOLD.pop(session_id, None)
                    return "local", f"sticky_expired({below_count}/{SUGGESTION1_RETURN_ROUNDS})"
                return "cloud", f"session_already_cloud(below={below_count}/{SUGGESTION1_RETURN_ROUNDS})"
            # Hard local only when the user/admin explicitly forced it.
            if session_route == "local_forced" and force_source != "cloud_failures":
                return "local", "session_force_local"

        # Priority 3.5: Daily budget exceeded (hard-stop)
        if (
            _ps.PROXY_ROUTE_DAILY_BUDGET > 0
            and _ps.PROXY_ROUTE_DAILY_BUDGET_HARD_STOP
        ):
            with _ps._state_lock:
                daily_date = getattr(_ps, '_route_daily_date', '')
                daily_cost = getattr(_ps, '_route_daily_cost', 0.0)
            today = time.strftime("%Y-%m-%d")
            if daily_date == today and daily_cost >= _ps.PROXY_ROUTE_DAILY_BUDGET:
                return "local", f"daily_budget_exceeded(¥{daily_cost:.2f}/¥{_ps.PROXY_ROUTE_DAILY_BUDGET:.0f})"

        # Priority 3.6: per-provider daily budget cap (catalog defaults.per_provider_budget)
        _pb_model = getattr(ctx, '_route_cloud_model', '')
        _pb_creds = model_registry.get_model_credentials(_pb_model, env_lookup=_ps._env_lookup) if _pb_model else None
        _pb_name = (_pb_creds or {}).get("name", "")
        if _pb_name and _ps._provider_budget_exceeded(_pb_name):
            _pb_spent = 0.0
            with _ps._state_lock:
                _pb_spent = _ps._route_provider_cost.get(_pb_name, 0.0)
            _pb_cap = model_registry.get_provider_budget(_pb_name) or 0
            return "local", f"provider_budget_exceeded({_pb_name} ¥{_pb_spent:.2f}/¥{_pb_cap:.2f})"

        # Priority 6: Memory pressure
        try:
            mem = _ps._get_system_memory()
            used_pct = float(mem.get("used_pct", 0))
            available_gb = float(mem.get("available_gb", 48))
            if used_pct > effective_memory_pct and available_gb < 5:
                if session_id:
                    with _ps._state_lock:
                        _ps._SESSION_ROUTE_MAP[session_id] = "cloud"
                return "cloud", f"memory_pressure({used_pct:.0f}%/{available_gb:.0f}GB)"
        except Exception:
            pass  # memory check is best-effort

        # Priority 7: Context size threshold
        total_chars = (
            ctx.stage_config.get("total_chars", 0)
            if ctx.stage_config else ctx.total_chars
        )
        if total_chars > effective_threshold:
            if session_id:
                with _ps._state_lock:
                    _ps._SESSION_ROUTE_MAP[session_id] = "cloud"
            return "cloud", f"chars_exceed_threshold({total_chars}>{effective_threshold})"

        # Priority 8: Lifecycle stage (belt-and-suspenders)
        stage = ctx.stage_config.get("stage", "init") if ctx.stage_config else "init"
        if stage in ("saturation", "oom_danger", "pre_trunc"):
            if session_id:
                with _ps._state_lock:
                    _ps._SESSION_ROUTE_MAP[session_id] = "cloud"
            return "cloud", f"lifecycle_stage({stage})"

        # Priority 9: Default local
        return "local", "under_threshold"

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        # Determine route_bias from model preference
        requested_model = ctx.body.get("model", "")
        pref = _ps.MODEL_ROUTE_PREFERENCES.get(requested_model, {})
        return {
            "target": ctx._route_target,
            "reason": ctx._route_reason,
            "agent_tier": ctx._agent_model_tier,
            "route_bias": pref.get("route_bias", "auto"),
            "chars": (
                ctx.stage_config.get("total_chars", 0)
                if ctx.stage_config else 0
            ),
            "stage": (
                ctx.stage_config.get("stage", "")
                if ctx.stage_config else ""
            ),
        }


# ============================================================================
# Stage 2.6: RouteNotification — log route-switch events (Phase 1: log only)
# ============================================================================

def _catalog_model_prices(model):
    """(input, output) ¥/M-token price for a model; global pair as fallback.

    Shared by BackendDispatcher (cost accounting) and RouteNotification
    (notice wording) so both stay consistent with the catalog — subscription
    models (price 0/0) read as "no per-token charge".
    """
    price = (model_registry.get_model(model) or {}).get("price") or {}
    pin = price.get("input", _ps.PROXY_CLOUD_PRICE_INPUT)
    pout = price.get("output", _ps.PROXY_CLOUD_PRICE_OUTPUT)
    if not isinstance(pin, (int, float)) or isinstance(pin, bool):
        pin = _ps.PROXY_CLOUD_PRICE_INPUT
    if not isinstance(pout, (int, float)) or isinstance(pout, bool):
        pout = _ps.PROXY_CLOUD_PRICE_OUTPUT
    return pin, pout


# Effort ladder used to map between the Anthropic five-level scale
# (output_config.effort) and backend-specific reasoning_effort sets.
_EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]


def _map_effort_to_levels(value, levels):
    """Map an Anthropic effort level onto the levels a backend accepts.

    Exact match wins; otherwise step toward the nearest accepted level,
    rounding UP when possible (quality-preserving: a client asking for
    `medium` depth gets `high` on a low/high/max backend, not `low`)
    and falling back to the highest accepted level below otherwise.
    """
    if not levels or value not in _EFFORT_ORDER:
        return None
    if value in levels:
        return value
    idx = _EFFORT_ORDER.index(value)
    for up in range(idx + 1, len(_EFFORT_ORDER)):
        if _EFFORT_ORDER[up] in levels:
            return _EFFORT_ORDER[up]
    for down in range(idx - 1, -1, -1):
        if _EFFORT_ORDER[down] in levels:
            return _EFFORT_ORDER[down]
    return None


def _effective_effort(ctx, quirks):
    """Resolve the requested reasoning effort, Anthropic scale.

    Priority: client `output_config.effort` (Anthropic protocol) or
    `reasoning_effort` (OpenAI-protocol entry, normalized by
    convert_openai_request_to_anthropic) > catalog quirk default > None.
    """
    body = ctx.body if hasattr(ctx, "body") else {}
    oc = body.get("output_config") if isinstance(body, dict) else None
    if isinstance(oc, dict) and oc.get("effort"):
        return str(oc["effort"])
    if isinstance(body, dict) and body.get("reasoning_effort"):
        return str(body["reasoning_effort"])
    d = quirks.get("reasoning_effort_default")
    return str(d) if d else None

class RouteNotification(PipelineStage):
    """Stage 2.6: Inject route-switch notification when target changes.

    Phase 2: Full message injection — appends a [System: ...] user message
    so the model sees the switch.  Differentiates first-route vs emergency-fallback.
    Each session is notified at most once.
    """

    name = "route_notification"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        target = getattr(ctx, '_route_target', 'local')
        if target != 'cloud':
            return ctx

        session_id = ctx.session_id
        if session_id:
            with _ps._state_lock:
                if session_id in _ps._ROUTE_NOTIFIED_SESSIONS:
                    return ctx

        if getattr(ctx, '_emergency_fallback', False):
            notice = self._build_emergency_notice(ctx)
        else:
            notice = self._build_first_route_notice(ctx)

        ctx.messages.append({
            "role": "user",
            "content": [{"type": "text", "text": notice}],
        })

        log(
            f"  -> [route_notification] Session {session_id} switched to cloud "
            f"(model={getattr(ctx, '_route_cloud_model', _ps.PROXY_CLOUD_MODEL)}, "
            f"reason={ctx._route_reason})"
        )

        if session_id:
            with _ps._state_lock:
                _ps._ROUTE_NOTIFIED_SESSIONS.add(session_id)

        return ctx

    def _build_first_route_notice(self, ctx):
        total_chars = (ctx.stage_config.get("total_chars", 0)
                       if ctx.stage_config else ctx.total_chars)
        threshold = _ps.PROXY_ROUTE_THRESHOLD_CHARS
        model = getattr(ctx, '_route_cloud_model', '') or _ps.PROXY_CLOUD_MODEL
        reason = getattr(ctx, '_route_reason', '')
        why = self._reason_phrase(ctx, reason, total_chars, threshold)
        # Cost wording follows the catalog price of the SELECTED model —
        # subscription models (0/0) are "no per-token charge", pay-per-use
        # models get a char-ratio estimate at their own price.
        price_in, price_out = _catalog_model_prices(model)
        if price_in == 0 and price_out == 0:
            cost_clause = "Subscription quota — no per-token charge."
        else:
            est_input = total_chars / max(_ps.PROXY_CTX_TOKEN_RATIO, 0.1) * price_in / 1_000_000
            est_out = est_input * (price_out / price_in) if price_in else 0.0
            cost_clause = (f"Estimated cost ~¥{est_input + est_out:.4f}/request "
                           f"(input ¥{price_in:.2f}/M, output ¥{price_out:.2f}/M).")
        return (
            f"[System: Switched to cloud model — {why}. Using {model}. "
            f"{cost_clause} "
            f"Session will stay on cloud. New sessions return to local. "
            f"To force local: `./manage.sh route-force-local {ctx.session_id or 'SESSION_ID'}`.]"
        )

    @staticmethod
    def _reason_phrase(ctx, reason, total_chars, threshold):
        """Human phrasing for the route reason (was hardcoded "exceeds limit")."""
        if reason.startswith("chars_exceed") or reason.startswith("lifecycle_stage"):
            return f"context {total_chars:,} chars exceeds local {threshold:,} limit"
        if reason == "header_override":
            return "per-request route override (X-Proxy-Route-To header)"
        if reason.startswith("model_forced"):
            req = ctx.body.get("model", "") if hasattr(ctx, "body") else ""
            return f"model preference ({req}) routes this session to cloud"
        if reason.startswith("memory_pressure"):
            return f"local memory pressure ({reason})"
        if reason.startswith("session_"):
            return f"session routing state ({reason})"
        return f"routing policy ({reason})" if reason else "routing policy decision"

    def _build_emergency_notice(self, ctx):
        total_chars = (ctx.stage_config.get("total_chars", 0)
                       if ctx.stage_config else ctx.total_chars)
        target = min(_ps.PROXY_OOM_SAFE_CHARS // 2, _ps.PROXY_CHARS_EXPANSION)
        return (
            f"[System: Cloud API unavailable, emergency fallback to local. "
            f"Context severely truncated from {total_chars:,} to ~{target:,} chars "
            f"(kept last 3 rounds). "
            f"Consider /compact or retry when cloud recovers. "
            f"To force cloud retry: `./manage.sh route-force-cloud {ctx.session_id or 'SESSION_ID'}`.]"
        )


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
        blocker_info = loop_detection._detect_blocker_pattern(ctx.messages, session_id=ctx.session_id)
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

class CacheAligner(ConditionalStage):
    """Stage 6: Split messages into protected prefix and mutable dynamic zone.

    Calls _apply_cache_aligner() from lifecycle.py. The prefix is protected
    from compression and truncation so the KV cache prefix stays stable.
    Places the split parts into ctx._cache_prefix / ctx._cache_dynamic,
    and sets ctx.messages = ctx._cache_dynamic so downstream stages only
    see the dynamic portion.

    Skipped when routing to cloud (no local KV cache to align).
    """

    name = "cache_aligner"

    def should_run(self, ctx: PipelineContext) -> bool:
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            return False
        return _ps.PROXY_CACHE_ALIGN_ENABLED

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

class ContentCompressor(ConditionalStage):
    """Stage 7: Compress tool results, strip thinking blocks, semantic compression.

    Operates on ctx.messages (which is the dynamic zone set by CacheAligner).
    After compression, reassembles: ctx.messages = _cache_prefix + _cache_dynamic.

    Calls _compress_content_pass() from truncation.py.

    Skipped when routing to cloud (cloud has ample context window, no need to compress).
    """

    name = "content_compressor"

    def should_run(self, ctx: PipelineContext) -> bool:
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            return False
        return True  # always runs for local

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
        compress_stats = ctx.compress_stats or {}
        semantic = compress_stats.get("compress", {"enabled": False})
        clear_stats = compress_stats.get("clear", {})
        think_stats = compress_stats.get("think", {})

        strategy = compress_stats.get("strategy", "rule_based")
        ratio = compress_stats.get("compression_ratio", 1.0)
        dropped = clear_stats.get("cleared_tool_results", 0)
        protected_n = len(compress_stats.get("protected_indices", []))
        bm25_scores = compress_stats.get("bm25_scores", {})
        bm25_avg = round(sum(bm25_scores.values()) / len(bm25_scores), 2) if bm25_scores else 0.0

        return {
            "compression": {
                "strategy": strategy,
                "ratio": ratio,
                "dropped": dropped,
                "protected_n": protected_n,
                "bm25_scores_avg": bm25_avg,
                "cleared": clear_stats.get("cleared", False),
                "cleared_chars": clear_stats.get("cleared_chars", 0),
                "think_stripped": think_stats.get("stripped_count", 0),
                "semantic_compressed": semantic.get("compressed_count", 0),
                "semantic_saved_chars": semantic.get("saved_chars", 0),
            }
        }


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
        text_loop_run, is_text_loop = loop_detection._detect_text_loop(tail_assistant, session_id=ctx.session_id)
        eff_threshold = loop_detection._effective_text_loop_threshold(ctx.session_id, ctx.total_chars)

        if text_loop_run > 1:
            log(f"  -> Text loop scan: text_run={text_loop_run} (threshold={eff_threshold}, "
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
            session_id=ctx.session_id,
            total_chars=ctx.total_chars,
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
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            return False
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
        strategy = trunc_stats.get("strategy", "char")
        ratio = trunc_stats.get("compression_ratio", 1.0)
        skipped_reason = trunc_stats.get("skipped_reason")
        dropped = trunc_stats.get("dropped_messages", 0)
        kept = trunc_stats.get("kept_messages", 0)
        budget_chars = trunc_stats.get("budget_chars", _ps.PROXY_CHARS_EXPANSION)

        if not trunc_stats.get("enabled"):
            return {"compression": {"strategy": strategy, "ratio": 1.0, "enabled": False}}
        if trunc_stats.get("skipped"):
            return {"compression": {"strategy": strategy, "ratio": 1.0, "skipped": True,
                                    "skipped_reason": skipped_reason or "unknown"}}
        if trunc_stats.get("truncated"):
            result = {
                "compression": {
                    "strategy": strategy,
                    "ratio": ratio,
                    "skipped_reason": skipped_reason,
                    "dropped": dropped,
                    "kept": kept,
                    "budget_chars": budget_chars,
                }
            }
            if strategy == "rounds":
                result["compression"]["compression_type"] = trunc_stats.get("compression", "folded")
                result["compression"]["rounds"] = trunc_stats.get("actual_keep_rounds", "?")
            elif strategy == "smart":
                result["compression"]["compressed_assistants"] = trunc_stats.get("compressed_assistants", 0)
            return result
        return {"compression": {"strategy": strategy, "ratio": 1.0, "enabled": True}}


# ============================================================================
# Stage 15: HighDropRatioNotice — warn when context loss is severe
# ============================================================================

class HighDropRatioNotice(ConditionalStage):
    """Stage 15: Inject a context-loss notice when >85% of messages were dropped.

    DEF-107: Prevents silent context loss that degrades response quality.
    Skipped when routing to cloud (no truncation occurs on cloud path).
    """

    name = "high_drop_ratio_notice"

    def should_run(self, ctx: PipelineContext) -> bool:
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            return False
        return ctx.trunc_stats is not None

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
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            return False
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
        original_msg_count = len(raw_messages)
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
        ctx.oom_dropped = original_msg_count - len(raw_messages)
        return ctx

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        if ctx.oom_iterations > 0:
            return {
                "compression": {
                    "strategy": "oom_safety_fifo",
                    "dropped": getattr(ctx, 'oom_dropped', 0),
                    "iterations": ctx.oom_iterations,
                    "budget_chars": _ps.PROXY_CHARS_OOM_DANGER,
                }
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

        # 3. Build OpenAI body — route-aware model selection
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            openai_body_model = getattr(ctx, '_route_cloud_model', '') or _ps.PROXY_CLOUD_MODEL
        else:
            openai_body_model = _ps.MODEL_NAME
        openai_body = {
            "model": openai_body_model,
            "messages": messages,
            "max_tokens": body.get("max_tokens", 4096),
            "temperature": body.get("temperature", 0.7),
            "stream": ctx.is_stream,
        }
        if "top_p" in body:
            openai_body["top_p"] = body["top_p"]
        if "stop_sequences" in body:
            openai_body["stop"] = body["stop_sequences"]
        # 透传客户端 thinking / response_format（DeepSeek 推理模型 v4-pro 必须 thinking
        # enabled 否则返回空；response_format json_object 用于 JSON 输出场景）
        if "thinking" in body:
            openai_body["thinking"] = body["thinking"]
        if "response_format" in body:
            openai_body["response_format"] = body["response_format"]

        # 4. Model request quirks from the catalog (e.g. deepseek-v4-flash
        #    force-disables thinking — request_quirks.force_thinking_disabled).
        #    Legacy substring heuristic kept for models absent from the catalog.
        sel_model = openai_body.get("model", "")
        sel_entry = model_registry.get_model(sel_model)
        quirks = (sel_entry or {}).get("request_quirks", {})
        if quirks.get("force_thinking_disabled"):
            openai_body["thinking"] = {"type": "disabled"}
        elif sel_entry is None and _ps.IS_CLOUD and "flash" in sel_model.lower():
            openai_body["thinking"] = {"type": "disabled"}
        # Kimi thinking-only models reject any temperature != 1
        # ("invalid temperature: only 1 is allowed for this model").
        if "force_temperature" in quirks:
            openai_body["temperature"] = quirks["force_temperature"]
        # Thinking-only models (glm on z.ai ignores the thinking param entirely
        # and always thinks; kimi likewise) can burn the whole output budget on
        # reasoning and return no text block. Floor max_tokens so text always
        # has room (catalog quirk `min_max_tokens`, e.g. 8192).
        floor = quirks.get("min_max_tokens")
        if floor and isinstance(openai_body.get("max_tokens"), int):
            if openai_body["max_tokens"] < floor:
                log(f"  -> [quirk] max_tokens {openai_body['max_tokens']} -> {floor} "
                    f"(thinking-only model: reserve room for text after reasoning)")
                openai_body["max_tokens"] = floor
        # Reasoning-effort passthrough: map the client's Anthropic-scale
        # output_config.effort (or OpenAI reasoning_effort, or the catalog's
        # reasoning_effort_default quirk) onto the backend's accepted levels.
        # Backends without reasoning_effort_levels (deepseek) get nothing.
        levels = ((sel_entry or {}).get("capabilities") or {}).get("reasoning_effort_levels")
        if levels:
            effort = _effective_effort(ctx, quirks)
            mapped = _map_effort_to_levels(effort, levels) if effort else None
            if mapped:
                openai_body["reasoning_effort"] = mapped

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
                session_id=ctx.session_id,
            )
            if tf_stats.get("filtered"):
                body["tools"] = raw_tools
                recent_names = tf_stats.get("recent_tools", [])
                recent_info = f", recent_names={recent_names}" if recent_names else ""
                filtered_out = tf_stats.get("filtered_out", [])
                filtered_info = f", removed={filtered_out}" if filtered_out else ""
                auto_promoted = tf_stats.get("auto_promoted", [])
                auto_info = f", auto_promoted={auto_promoted}" if auto_promoted else ""
                log(f"  -> Tool filter: {tf_stats['original']} -> {tf_stats['kept']} "
                    f"(always={tf_stats['always_keep']}, recent={tf_stats['recent_only']}, "
                    f"scanned={tf_stats.get('scanned_assistant', 0)}{recent_info}{filtered_info}{auto_info})")

            # DEF-104: Update session tool frequency after filtering
            if ctx.session_id and _ps.PROXY_TOOL_AUTO_PROMOTE_THRESHOLD > 0:
                for t in raw_tools:
                    name = t.get("name", "") if isinstance(t, dict) else ""
                    if name:
                        freq = _ps._SESSION_TOOL_FREQ.setdefault(ctx.session_id, {})
                        freq[name] = freq.get(name, 0) + 1

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

class _BytesIOResponse:
    """Tiny wrapper to let a pre-read bytes body be consumed like urlopen response."""

    def __init__(self, status, body_bytes):
        self.status = status
        self._body = io.BytesIO(body_bytes)

    def read(self, amt=-1):
        return self._body.read(amt)

    def getheader(self, name, default=None):
        return default

    def getheaders(self):
        return []


class BackendDispatcher(PipelineStage):
    """Stage 21: Forward the OpenAI-format request to local or cloud backend.

    Routes based on ctx._route_target:
      - "local" / "local_forced": use LLAMA_BASE + _llama_lock
      - "cloud": use PROXY_CLOUD_BASE_URL + _cloud_lock

    If PROXY_CLOUD_API_KEY is unset and target is cloud, falls back to local
    automatically.
    Phase 2: Cloud HTTPError triggers fallback to local with emergency truncation.

    Constructor args:
      - llama_lock: threading.Semaphore for local concurrency
      - cloud_lock: threading.Semaphore for cloud concurrency
      - handler: the Handler instance for writing the HTTP response
    """

    name = "backend_dispatcher"

    def __init__(self, llama_lock=None, cloud_lock=None, handler=None):
        self._llama_lock = llama_lock
        self._cloud_lock = cloud_lock
        self._handler = handler
        self._backend_status = None
        self._route_fallback = False
        self._emergency_fallback = False
        self._fallback_reason = ""
        self._sensitive_blocked = False
        self._input_tokens = 0
        self._output_tokens = 0
        self._dispatch_latency_ms = 0.0

    # ------------------------------------------------------------------
    # Phase B: catalog-driven cloud target resolution
    # ------------------------------------------------------------------
    def _resolve_cloud_target(self, model_name):
        """Resolve a cloud model to {model, provider, base_url, api_key, key_env, lock}.

        Catalog providers win; unknown models (or providers without a base_url)
        fall back to the PROXY_CLOUD_* globals so pre-catalog deployments keep
        working. Provider lock falls back to the constructor cloud_lock.
        """
        creds = model_registry.get_model_credentials(model_name, env_lookup=_ps._env_lookup)
        if creds and creds.get("base_url"):
            lock = _ps._provider_locks.get(creds["name"]) or self._cloud_lock
            return {
                "model": model_name,
                "provider": creds["name"],
                "protocol": creds.get("protocol", "openai"),
                "base_url": creds["base_url"],
                "api_key": creds.get("api_key", ""),
                "key_env": creds.get("key_env", ""),
                "lock": lock,
            }
        return {
            "model": model_name,
            "provider": "default",
            "protocol": "openai",
            "base_url": _ps.PROXY_CLOUD_BASE_URL,
            "api_key": _ps.PROXY_CLOUD_API_KEY,
            "key_env": "PROXY_CLOUD_API_KEY",
            "lock": self._cloud_lock,
        }

    def _cloud_candidates(self, ctx):
        """Primary cloud model + catalog fallback chain, deduped, resolved."""
        primary = getattr(ctx, '_route_cloud_model', '') or _ps.PROXY_CLOUD_MODEL
        names = [primary] + [
            m for m in getattr(ctx, '_route_fallback_models', [])
            if m and m != primary
        ]
        seen, out = set(), []
        for n in names:
            if n in seen:
                continue
            seen.add(n)
            out.append(self._resolve_cloud_target(n))
        return out

    def _model_prices(self, model):
        """(input, output) ¥/M-token price for a model; global pair as fallback."""
        return _catalog_model_prices(model)

    def _route_cost_estimate(self, ctx):
        """Pre-dispatch cost estimate (char-ratio input, price-ratio output)."""
        model = getattr(ctx, '_route_cloud_model', '') or _ps.PROXY_CLOUD_MODEL
        pin, pout = self._model_prices(model)
        est_in = max(1, int(ctx.total_chars / max(_ps.PROXY_CTX_TOKEN_RATIO, 0.1)))
        est_input_cost = est_in * pin / 1_000_000
        est_output_cost = est_input_cost * (pout / pin) if pin else 0.0
        return est_input_cost + est_output_cost

    def _set_route_headers(self, ctx):
        """R8 route-attribution response headers (contract names X-Proxy-Route-*).

        Target display value: cloud | local | local_forced (forced-by-session
        or -header local, and the emergency cloud→local fallback path).
        Cost is a pre-dispatch estimate for streaming compatibility; the
        actual usage-based cost lands in metrics and the OpenAI-mode body.
        """
        target = getattr(ctx, '_route_target', 'local')
        reason = getattr(ctx, '_route_reason', '')
        if target == 'cloud':
            actual = getattr(ctx, '_route_cloud_model', '') or _ps.PROXY_CLOUD_MODEL
            cost = self._route_cost_estimate(ctx)
        else:
            actual = _ps.MODEL_NAME
            cost = 0.0
        display = target
        if target == 'local' and reason in ('session_force_local', 'header_override'):
            display = 'local_forced'
        self._handler._route_response_headers = {
            "X-Proxy-Route-Target": display,
            "X-Proxy-Route-Actual-Model": actual,
            "X-Proxy-Route-Reason": reason,
            "X-Proxy-Route-Cost": "%.6f" % cost,
        }

    def process(self, ctx: PipelineContext) -> PipelineContext:
        target = getattr(ctx, '_route_target', 'local')
        self._route_fallback = False
        self._emergency_fallback = False
        self._fallback_reason = ""
        self._sensitive_blocked = False
        self._input_tokens = 0
        self._output_tokens = 0
        self._dispatch_latency_ms = 0.0

        # Resolve catalog-driven cloud candidates (primary + fallback chain).
        candidates = self._cloud_candidates(ctx) if target == 'cloud' else []
        # OpenAI-protocol clients can't be served by anthropic-protocol
        # backends (response shape mismatch) — such candidates are unusable.
        _oai_client = bool(getattr(self._handler, '_openai_mode', False))

        def _usable(c):
            return (
                c["api_key"]
                and not _ps._provider_cooldown_active(c["provider"])
                and not (_oai_client and c.get("protocol") == "anthropic")
            )

        # Validate cloud API key availability across candidates — force mode:
        # error; force_fallback/prefer: fall back to local. Cooldown-active
        # providers don't count as usable here.
        if target == 'cloud' and not any(_usable(c) for c in candidates):
            route_reason = getattr(ctx, '_route_reason', '')
            if route_reason and route_reason.startswith("model_forced_") \
                    and not route_reason.startswith("model_forced_fallback_"):
                key_env = candidates[0]["key_env"] if candidates else "PROXY_CLOUD_API_KEY"
                log("  -> [ERROR] Force mode but no cloud API key configured — returning 503")
                self._handler._respond_json({
                    "error": {
                        "type": "cloud_unavailable",
                        "message": (
                            f"Cloud API key not configured for force-routed model. "
                            f"Set {key_env} in configs/secret.local.conf, or switch to a "
                            f"prefer-routed model via `/model claude-sonnet-4-6`."
                        ),
                    }
                }, 503)
                return ctx
            log("  -> [ERROR] Cloud API key not configured — falling back to local")
            ctx._route_target = 'local'
            ctx._route_reason = 'cloud_no_api_key'
            target = 'local'
            # FormatConverter already built openai_body with the cloud model;
            # switch to local MODEL_NAME so the local backend recognises it.
            if hasattr(ctx, 'openai_body') and isinstance(ctx.openai_body, dict):
                ctx.openai_body["model"] = _ps.MODEL_NAME

        # R8 route-attribution response headers (X-Proxy-Route-* contract names)
        self._set_route_headers(ctx)

        if target == 'cloud':
            # Iterate the catalog candidates: primary model first, then the
            # fallback chain. Skips providers in cooldown or without a key.
            dispatch_exc = None
            dispatch_raw_err = ""
            idx = 0
            while idx < len(candidates):
                cand = candidates[idx]
                idx += 1
                if _ps._provider_cooldown_active(cand["provider"]):
                    log(f"  -> Skip {cand['model']} — provider '{cand['provider']}' in cooldown")
                    continue
                if not cand["api_key"]:
                    log(f"  -> Skip {cand['model']} — no API key ({cand['key_env']})")
                    continue
                if _oai_client and cand.get("protocol") == "anthropic":
                    log(f"  -> Skip {cand['model']} — anthropic-protocol backend "
                        f"cannot serve an OpenAI-protocol client")
                    continue
                ctx._route_cloud_model = cand["model"]
                ctx._route_provider = cand["provider"]
                if hasattr(ctx, 'openai_body') and isinstance(ctx.openai_body, dict):
                    ctx.openai_body["model"] = cand["model"]
                self._set_route_headers(ctx)
                try:
                    if cand.get("protocol") == "anthropic":
                        log(f"  -> Forwarding to {cand['base_url']}/v1/messages "
                            f"(cloud, anthropic protocol, provider={cand['provider']}, "
                            f"model={cand['model']})")
                        with cand["lock"]:
                            self._do_dispatch_anthropic(ctx, cand)
                    else:
                        log(f"  -> Forwarding to {cand['base_url']}/chat/completions "
                            f"(cloud, provider={cand['provider']}, model={cand['model']})")
                        with cand["lock"]:
                            self._do_dispatch(ctx, cand["base_url"], cand["api_key"])
                    _ps._record_provider_success(cand["provider"])
                    return ctx
                except urllib.error.HTTPError as e:
                    raw_err = e.read().decode("utf-8")
                    dispatch_exc, dispatch_raw_err = e, raw_err
                    self._backend_status = e.code
                    self._fallback_reason = str(e.code)
                    log(f"  <- Cloud API failed ({e.code}), checking fallback...")
                    _log_cloud_error(ctx, e.code, raw_err)
                    _ps._record_provider_failure(
                        cand["provider"],
                        retryable=e.code in (408, 429, 500, 502, 503, 504),
                    )
                except urllib.error.URLError as ue:
                    # Connection-level failure (refused/timeout) — also feeds
                    # the chain and the local fallback.
                    dispatch_exc, dispatch_raw_err = ue, str(ue)
                    self._backend_status = 503
                    self._fallback_reason = str(ue)
                    log(f"  <- Cloud API unreachable ({ue}), checking fallback...")
                    _log_cloud_error(ctx, 503, str(ue))
                    _ps._record_provider_failure(cand["provider"], retryable=True)

                # Gates below end the request regardless of remaining chain.
                # Force mode: do NOT fallback (model_forced_cloud) unless force_fallback
                route_reason = getattr(ctx, '_route_reason', '')
                if route_reason and route_reason.startswith("model_forced_") \
                        and not route_reason.startswith("model_forced_fallback_"):
                    log(f"  <- Force mode ('{route_reason}') — no fallback, returning 503")
                    req_model = str(ctx.body.get("model", "unknown")) if hasattr(ctx, 'body') else "?"
                    self._handler._respond_json({
                        "error": {
                            "type": "cloud_unavailable",
                            "message": (
                                f"Cloud API unavailable in force mode. "
                                f"The model you selected ({req_model}) requires the cloud backend, "
                                f"but it is currently unreachable. "
                                f"Use `/model claude-sonnet-4-6` or `/model claude-haiku-4-5` to switch "
                                f"to a prefer-routed model that can use the local backend."
                            ),
                        }
                    }, 503)
                    return ctx

                if not _ps.PROXY_ROUTE_FALLBACK_ENABLED:
                    log(f"  -> Fallback disabled — returning 503")
                    status = getattr(dispatch_exc, "code", 503)
                    self._handler._respond_json({
                        "error": {
                            "type": "cloud_unavailable",
                            "message": f"Cloud API failed with status {status} and fallback is disabled.",
                        }
                    }, 503)
                    return ctx

                if _is_sensitive_request(ctx):
                    log(f"  -> Sensitive path detected — blocking fallback")
                    self._sensitive_blocked = True
                    self._handler._respond_json({
                        "error": {
                            "type": "sensitive_fallback_blocked",
                            "message": "Cloud API failed and request contains sensitive file paths.",
                        }
                    }, 403)
                    return ctx

                # Attempt one in-place message repair + same-provider retry for
                # repairable 400 format errors before moving down the chain.
                if isinstance(dispatch_exc, urllib.error.HTTPError) and \
                        self._is_repairable_format_error(dispatch_exc.code, dispatch_raw_err):
                    log(f"  -> Detected repairable format error, attempting message repair + cloud retry")
                    if self._repair_openai_messages(ctx):
                        try:
                            with cand["lock"]:
                                self._do_dispatch(ctx, cand["base_url"], cand["api_key"])
                            log(f"  <- Cloud retry succeeded after message repair")
                            _ps._record_provider_success(cand["provider"])
                            return ctx
                        except urllib.error.HTTPError as e2:
                            raw_err2 = e2.read().decode("utf-8")
                            log(f"  <- Cloud retry failed ({e2.code}: {raw_err2[:200]}), trying next candidate...")
                            _log_cloud_error(ctx, e2.code, raw_err2, exc_info="retry_after_repair")
                            dispatch_exc, dispatch_raw_err = e2, raw_err2
                            _ps._record_provider_failure(
                                cand["provider"],
                                retryable=e2.code in (408, 429, 500, 502, 503, 504),
                            )
                        except urllib.error.URLError as ue2:
                            log(f"  <- Cloud retry failed ({ue2}), trying next candidate...")
                            dispatch_exc, dispatch_raw_err = ue2, str(ue2)
                            _ps._record_provider_failure(cand["provider"], retryable=True)
                    else:
                        log(f"  -> No message repair possible, trying next candidate...")
                # next chain candidate

            # Chain exhausted — record session failure and fall back to local.
            # Record failure and manage cooldown
            self._record_cloud_failure(ctx, dispatch_exc)
            self._route_fallback = True

            # Emergency truncation before retrying on local
            self._emergency_truncate(ctx)
            ctx._emergency_fallback = True
            ctx._route_target = 'local_forced'
            self._set_route_headers(ctx)

            # Retry with local backend
            log(f"  -> Fallback to local backend")
            # openai_body.model still holds the cloud model name from
            # FormatConverter; re-point to local MODEL_NAME for the retry.
            if hasattr(ctx, 'openai_body') and isinstance(ctx.openai_body, dict):
                ctx.openai_body["model"] = _ps.MODEL_NAME
            try:
                with self._llama_lock:
                    self._do_dispatch(ctx, _ps.LLAMA_BASE, _ps.LLAMA_API_KEY)
            except (urllib.error.HTTPError, urllib.error.URLError) as ue:
                # Local backend is also down — surface the original cloud error
                # rather than a misleading connection-refused message.
                err_code = getattr(dispatch_exc, "code", None) or 503
                err_body = dispatch_raw_err[:500] if dispatch_raw_err else ""
                log(f"  <- Local fallback unavailable ({ue}); returning original cloud error {err_code}")
                self._handler._respond_json({
                    "error": {
                        "type": "cloud_unavailable",
                        "message": f"Cloud API failed ({err_code}: {err_body}); local backend also unavailable.",
                    }
                }, err_code)
                return ctx
        else:
            base_url = _ps.LLAMA_BASE
            api_key = _ps.LLAMA_API_KEY
            log(f"  -> Forwarding to {base_url}/chat/completions (local)")
            try:
                with self._llama_lock:
                    self._do_dispatch(ctx, base_url, api_key)
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                if isinstance(e, urllib.error.HTTPError):
                    err_body = e.read().decode("utf-8")[:500]
                    err_msg = f"{e.code} - {err_body}"
                    self._backend_status = e.code
                else:
                    err_msg = str(e)
                    self._backend_status = 503
                self._fallback_reason = err_msg
                log(f"  <- Local backend failed ({err_msg}), checking fallback...")

                route_reason = getattr(ctx, '_route_reason', '')
                # Do NOT fallback if the user explicitly forced local.
                if route_reason == 'session_force_local':
                    log(f"  <- Local manually forced — no fallback")
                    self._handler._respond_json({"error": {"message": err_msg}}, self._backend_status)
                    return ctx

                if not _ps.PROXY_ROUTE_FALLBACK_ENABLED:
                    log(f"  -> Fallback disabled — returning {self._backend_status}")
                    self._handler._respond_json({"error": {"message": err_msg}}, self._backend_status)
                    return ctx

                if _is_sensitive_request(ctx):
                    log(f"  -> Sensitive path detected — blocking fallback")
                    self._sensitive_blocked = True
                    self._handler._respond_json({
                        "error": {
                            "type": "sensitive_fallback_blocked",
                            "message": "Local backend failed and request contains sensitive file paths.",
                        }
                    }, 403)
                    return ctx

                # Clear any cloud cooldown so the retry can actually reach cloud.
                self._clear_cloud_cooldown(ctx.session_id)
                self._route_fallback = True
                ctx._route_target = 'cloud'
                ctx._route_reason = 'local_failure_fallback'

                # Retry with cloud backend — resolve via the catalog so the
                # correct provider endpoint/key/lock is used.
                cloud_cand = None
                for _c in candidates or self._cloud_candidates(ctx):
                    if _c["api_key"] and not _ps._provider_cooldown_active(_c["provider"]):
                        cloud_cand = _c
                        break
                if cloud_cand is None:
                    cloud_cand = self._resolve_cloud_target(
                        getattr(ctx, '_route_cloud_model', '') or _ps.PROXY_CLOUD_MODEL)
                ctx._route_cloud_model = cloud_cand["model"]
                ctx._route_provider = cloud_cand["provider"]
                log(f"  -> Fallback to cloud backend (provider={cloud_cand['provider']}, {cloud_cand['model']})")
                if hasattr(ctx, 'openai_body') and isinstance(ctx.openai_body, dict):
                    ctx.openai_body["model"] = cloud_cand["model"]
                self._set_route_headers(ctx)
                try:
                    with cloud_cand["lock"]:
                        self._do_dispatch(ctx, cloud_cand["base_url"], cloud_cand["api_key"])
                except Exception as e2:
                    log(f"  <- Cloud fallback also failed: {e2}")
                    self._handler._respond_json({
                        "error": {
                            "message": f"Local failed: {err_msg}; cloud fallback failed: {e2}",
                            "type": "backend_unavailable",
                        }
                    }, 503)

        return ctx

    def _is_repairable_format_error(self, status, response_body):
        """Return True if the cloud error looks like a message-format issue.

        These errors can sometimes be fixed by re-running defensive
        normalization on the OpenAI-format messages before falling back to
        local.
        """
        if status != 400:
            return False
        text = (response_body or "").lower()
        patterns = [
            "tool_call_id",
            "tool_calls",
            "tool_use",
            "tool_result",
            "missing field",
            "must be followed by tool",
            "without tool_result",
            "invalid_request_error",
        ]
        return any(p in text for p in patterns)

    def _repair_openai_messages(self, ctx):
        """Re-run orphan-tool normalization and tombstone injection.

        Returns True if messages were modified.
        """
        try:
            msg_converter = _import_message_converter()
            messages = ctx.openai_body.get("messages", [])
            original = json.dumps(messages, sort_keys=True)
            messages = msg_converter._normalize_orphan_tool_messages(messages)
            messages = msg_converter._ensure_tool_chain_integrity(messages)
            ctx.openai_body["messages"] = messages
            modified = json.dumps(messages, sort_keys=True) != original
            if modified:
                log(f"  -> Repaired message chain: normalized orphan tools / injected tombstones")
            return modified
        except Exception as ex:
            log(f"  -> Message repair failed: {ex}")
            return False

    def _do_dispatch(self, ctx, base_url, api_key):
        """Send HTTP POST to backend and dispatch response to handler.

        Caller must already hold the appropriate concurrency lock.
        """
        # P0: enforce per-backend payload size guard just before forwarding.
        body_bytes = json.dumps(ctx.openai_body, ensure_ascii=False).encode("utf-8")
        target = getattr(ctx, '_route_target', 'local')
        max_bytes = _ps.PROXY_CLOUD_MAX_REQUEST_BYTES if target == 'cloud' else _ps.PROXY_MAX_REQUEST_BYTES
        if len(body_bytes) > max_bytes:
            limit_name = "cloud" if target == 'cloud' else "local"
            log(f"  -> Request body too large for {limit_name} backend: {len(body_bytes)} bytes > {max_bytes} limit")
            self._handler._respond_json(
                {"error": {
                    "type": "payload_too_large",
                    "message": f"Request body ({len(body_bytes)} bytes) exceeds {limit_name} backend maximum allowed size ({max_bytes} bytes).",
                    "max_bytes": max_bytes,
                    "received_bytes": len(body_bytes),
                    "route_target": target,
                }},
                413,
            )
            return

        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        _dispatch_t0 = time.monotonic()
        resp = urllib.request.urlopen(req, timeout=_ps.PROXY_BACKEND_TIMEOUT)
        self._backend_status = resp.status
        log(f"  <- backend status: {resp.status}")

        # Estimate input tokens once for cost tracking (used for both streaming
        # and non-streaming; non-streaming is refined with actual usage below).
        self._input_tokens = max(1, int(ctx.total_chars / max(_ps.PROXY_CTX_TOKEN_RATIO, 0.1)))
        self._output_tokens = int(ctx.body.get("max_tokens", 4096))

        if ctx.is_stream:
            self._handler._handle_streaming_response(resp, ctx.body)
        else:
            # Pre-read non-streaming body so we can extract actual usage for
            # accurate cost tracking, then hand a BytesIO wrapper to the handler.
            body_bytes = resp.read()
            try:
                openai_resp = json.loads(body_bytes.decode("utf-8"))
                usage = openai_resp.get("usage") or {}
                self._input_tokens = usage.get("prompt_tokens", self._input_tokens)
                self._output_tokens = usage.get("completion_tokens", self._output_tokens)
                # R8 (非流式): usage-based actual cost + proxy_route body field
                # on OpenAI-protocol responses (Anthropic-format responses carry
                # attribution via headers — body field would be dropped by the
                # converter anyway).
                if getattr(ctx, '_route_target', 'local') == 'cloud':
                    pin, pout = self._model_prices(getattr(ctx, '_route_cloud_model', ''))
                    ctx._route_actual_cost = round(
                        (self._input_tokens * pin + self._output_tokens * pout) / 1_000_000, 6)
                    if getattr(self._handler, '_openai_mode', False):
                        openai_resp["proxy_route"] = {
                            "target": "cloud",
                            "actual_model": getattr(ctx, '_route_cloud_model', ''),
                            "reason": getattr(ctx, '_route_reason', ''),
                            "cost": ctx._route_actual_cost,
                        }
                        body_bytes = json.dumps(openai_resp).encode("utf-8")
            except Exception:
                pass
            wrapped = _BytesIOResponse(resp.status, body_bytes)
            self._handler._handle_non_streaming_response(wrapped, ctx.body)

        # Phase 3+ (建议3): record backend-only dispatch latency so cloud vs
        # long-tail comparison is decoupled from proxy-side pipeline overhead.
        dispatch_ms = (time.monotonic() - _dispatch_t0) * 1000
        self._dispatch_latency_ms = dispatch_ms
        try:
            target_key = getattr(ctx, '_route_target', 'local')
            # Only record primary route latencies; fallback retries would
            # contaminate the cloud bucket with local timing.
            if not self._route_fallback:
                _ps._LATENCY_BY_TARGET.setdefault(
                    target_key, collections.deque(maxlen=100)
                ).append(dispatch_ms)
        except Exception:
            pass

        # Accumulate daily route cost if cloud succeeded
        if getattr(ctx, '_route_target', 'local') == 'cloud' and not self._route_fallback:
            self._accumulate_daily_cost(ctx)

    def _do_dispatch_anthropic(self, ctx, cand):
        """Send the Anthropic-format body to an anthropic-protocol endpoint (Phase D).

        Reuses the pipeline's openai_body (all stage adjustments — compression,
        tool filtering, quirks — already applied) and converts it back via
        convert_openai_request_to_anthropic, the same round-trip the
        /v1/chat/completions entry uses. Response is relayed to the client
        unchanged (SSE passthrough / raw JSON + proxy_route attribution).
        Caller must already hold the provider concurrency lock.
        """
        msg_converter = _import_message_converter()
        try:
            anthropic_req = msg_converter.convert_openai_request_to_anthropic(ctx.openai_body)
        except Exception as e:
            log(f"  -> Anthropic body conversion failed ({e}); falling back to raw messages", level="WARN")
            anthropic_req = {
                "model": cand["model"],
                "max_tokens": ctx.body.get("max_tokens", 4096),
                "messages": ctx.messages,
            }
        anthropic_req["model"] = cand["model"]
        anthropic_req["stream"] = bool(ctx.is_stream)
        anthropic_req.pop("_x_proxy_route_to", None)
        body_bytes = json.dumps(anthropic_req, ensure_ascii=False).encode("utf-8")

        if len(body_bytes) > _ps.PROXY_CLOUD_MAX_REQUEST_BYTES:
            log(f"  -> Request body too large for anthropic backend: "
                f"{len(body_bytes)} > {_ps.PROXY_CLOUD_MAX_REQUEST_BYTES}")
            self._handler._respond_json(
                {"error": {
                    "type": "payload_too_large",
                    "message": f"Request body ({len(body_bytes)} bytes) exceeds anthropic "
                               f"backend maximum ({_ps.PROXY_CLOUD_MAX_REQUEST_BYTES} bytes).",
                }}, 413)
            return

        req = urllib.request.Request(
            f"{cand['base_url']}/v1/messages",
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "x-api-key": cand["api_key"],
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        _dispatch_t0 = time.monotonic()
        resp = urllib.request.urlopen(req, timeout=_ps.PROXY_BACKEND_TIMEOUT)
        self._backend_status = resp.status
        log(f"  <- backend status: {resp.status} (anthropic)")

        self._input_tokens = max(1, int(ctx.total_chars / max(_ps.PROXY_CTX_TOKEN_RATIO, 0.1)))
        self._output_tokens = int(ctx.body.get("max_tokens", 4096))

        if ctx.is_stream:
            self._handler._handle_anthropic_stream_passthrough(resp, ctx.body)
        else:
            resp_bytes = resp.read()
            try:
                anth_resp = json.loads(resp_bytes.decode("utf-8"))
                usage = anth_resp.get("usage") or {}
                self._input_tokens = usage.get("input_tokens", self._input_tokens)
                self._output_tokens = usage.get("output_tokens", self._output_tokens)
                pin, pout = self._model_prices(cand["model"])
                ctx._route_actual_cost = round(
                    (self._input_tokens * pin + self._output_tokens * pout) / 1_000_000, 6)
                # R8 (非流式): proxy_route attribution in the response body.
                anth_resp["proxy_route"] = {
                    "target": "cloud",
                    "actual_model": cand["model"],
                    "reason": getattr(ctx, '_route_reason', ''),
                    "cost": ctx._route_actual_cost,
                }
                resp_bytes = json.dumps(anth_resp, ensure_ascii=False).encode("utf-8")
            except Exception:
                pass
            self._handler._handle_anthropic_response(resp.status, resp_bytes, ctx)

        dispatch_ms = (time.monotonic() - _dispatch_t0) * 1000
        self._dispatch_latency_ms = dispatch_ms
        try:
            if not self._route_fallback:
                _ps._LATENCY_BY_TARGET.setdefault(
                    "cloud", collections.deque(maxlen=100)).append(dispatch_ms)
        except Exception:
            pass
        if getattr(ctx, '_route_target', 'local') == 'cloud' and not self._route_fallback:
            self._accumulate_daily_cost(ctx)

    def _record_cloud_failure(self, ctx, exc):
        """Record cloud failure and manage cooldown state.

        Only retryable errors (5xx, timeout, connection issues) trigger cooldown.
        Authentication/authorization errors (4xx) do not lock the session to local.
        """
        session_id = ctx.session_id
        if not session_id:
            return
        retryable = False
        if isinstance(exc, urllib.error.HTTPError):
            retryable = exc.code in (408, 429, 500, 502, 503, 504)
        elif isinstance(exc, urllib.error.URLError):
            retryable = True
        if not retryable:
            code = getattr(exc, 'code', type(exc).__name__)
            log(f"  -> Cloud failure non-retryable ({code}), no cooldown")
            return
        with _ps._state_lock:
            _ps._cloud_fail_count[session_id] = _ps._cloud_fail_count.get(session_id, 0) + 1
            fail_count = _ps._cloud_fail_count[session_id]
            if fail_count >= _ps.PROXY_ROUTE_MAX_CLOUD_FAILS:
                _ps._cloud_cooldown_start[session_id] = time.monotonic()
                _ps._SESSION_ROUTE_MAP[session_id] = "local_forced"
                _ps._SESSION_ROUTE_FORCE_SOURCE[session_id] = "cloud_failures"
                # Reset failure count so cooldown can eventually clear cleanly.
                _ps._cloud_fail_count[session_id] = 0
                log(f"  -> Cloud cooldown activated for session {session_id} "
                    f"({fail_count} failures, {_ps.PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS}s)")

    def _clear_cloud_cooldown(self, session_id):
        """Clear cloud cooldown state for a session.

        Used when local backend fails so we can immediately retry cloud rather
        than being stuck in cooldown.
        """
        if not session_id:
            return
        with _ps._state_lock:
            _ps._cloud_fail_count.pop(session_id, None)
            _ps._cloud_cooldown_start.pop(session_id, None)
            force_source = _ps._SESSION_ROUTE_FORCE_SOURCE.get(session_id)
            if force_source == "cloud_failures":
                _ps._SESSION_ROUTE_MAP.pop(session_id, None)
                _ps._SESSION_ROUTE_FORCE_SOURCE.pop(session_id, None)

    def _emergency_truncate(self, ctx):
        """Emergency context reduction when cloud fallback to overloaded local."""
        total_chars = (ctx.stage_config.get("total_chars", 0)
                       if ctx.stage_config else ctx.total_chars)
        target = min(_ps.PROXY_OOM_SAFE_CHARS // 2, _ps.PROXY_CHARS_EXPANSION)

        if total_chars <= target:
            log(f"  -> Emergency truncation skipped: {total_chars} chars within {target} limit")
            return

        # Keep last 3 assistant rounds (walk backwards, count assistant messages)
        msgs = ctx.messages
        assistant_count = 0
        cutoff = len(msgs)
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "assistant":
                assistant_count += 1
                if assistant_count >= 3:
                    cutoff = i
                    break

        if cutoff > 0 and len(msgs) - cutoff < len(msgs):
            dropped = cutoff
            kept = len(msgs) - cutoff
            ctx.messages = msgs[cutoff:]
            log(f"  -> EMERGENCY TRUNCATION: {dropped} messages dropped, "
                f"{kept} kept (target={target:,} chars, kept last 3 rounds)")
            self._emergency_fallback = True

    def _accumulate_daily_cost(self, ctx):
        """Accumulate daily cloud API cost (best-effort estimation).

        Phase B: per-model catalog pricing and per-provider totals.
        """
        try:
            total = _ps._accumulate_route_daily_cost(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                model=getattr(ctx, '_route_cloud_model', ''),
                provider=getattr(ctx, '_route_provider', ''),
            )
            log(f"  -> [route_cost] daily cost now ¥{total:.4f}")
        except Exception:
            pass

    def output_metrics(self, ctx: PipelineContext) -> Optional[dict]:
        return {
            "backend_status": self._backend_status,
            "stream": 1 if ctx.is_stream else 0,
            "route_target": getattr(ctx, '_route_target', 'local'),
            "route_reason": getattr(ctx, '_route_reason', ''),
            "route_cloud_model": getattr(ctx, '_route_cloud_model', ''),
            "route_provider": getattr(ctx, '_route_provider', ''),
            "route_fallback": self._route_fallback,
            "emergency_fallback": self._emergency_fallback,
            "fallback_reason": self._fallback_reason,
            "sensitive_blocked": self._sensitive_blocked,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "dispatch_latency_ms": round(getattr(self, '_dispatch_latency_ms', 0.0), 1),
            "input_chars_bucket": _char_bucket(ctx.total_chars),
            "client_type": ctx.client_type,
        }
