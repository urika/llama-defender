#!/usr/bin/env python3
"""
Anthropic-to-OpenAI proxy for local llama-server.
Handles Qwen3.6 reasoning_content, streaming, and tool use correctly.
Includes XML->JSON fallback for Qwen tool calling quirks.
"""
import collections
import hashlib
import json
import os
import re
import signal
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime

from proxy_state import *
import proxy_state as _ps
import proxy_config

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_SCHEMA_VERSION = "v1"


# ---------------------------------------------------------------------------
# SIGHUP hot-reload: re-read active.conf and update config in proxy_state + self.
# Dual setattr (proxy_state + self_mod) ensures both sub-modules (which read
# proxy_state.PROXY_* at call time) and local functions (which reference
# module-level names imported via `from proxy_state import *`) see updates.
# ---------------------------------------------------------------------------

import reload_config

def _reload_config(signum=None, frame=None):
    reload_config.reload_config(signum, frame, target_module=sys.modules[__name__])

# Register SIGHUP handler
signal.signal(signal.SIGHUP, _reload_config)


import loop_detection
from loop_detection import *

# ---------------------------------------------------------------------------
# Pipeline abstraction: refactored _handle_messages processing stages.
# ---------------------------------------------------------------------------
from pipeline import (
    PipelineContext,
    InstrumentedPipeline,
    RequestParser,
    LifecycleClassifier,
    DynamicMaxTokens,
    SmartRouter,
    RouteNotification,
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








# ---------------------------------------------------------------------------
# Re-read prevention: when a Read tool targets a file whose content was just
# cleared, keep a preview of the original content to reduce re-read desire.
PROXY_REREAD_PREVIEW_CHARS = int(os.environ.get("PROXY_REREAD_PREVIEW_CHARS", "200"))

# ---------------------------------------------------------------------------
# Blocker detection: track consecutive same-error-type tool_result rejections
# (e.g. Read repeatedly returns "File does not exist"). When a tool fails the
# same way >= PROXY_BLOCKER_THRESHOLD times in a row, inject a [BLOCKER] user
# message nudging the model to switch tools or escalate. Disabled by default
# for cloud backends (1M+ token context, low marginal value).
# ---------------------------------------------------------------------------
PROXY_BLOCKER_ENABLED = os.environ.get("PROXY_BLOCKER_ENABLED", "true" if not IS_CLOUD else "false").lower() in ("1", "true", "yes")
PROXY_BLOCKER_THRESHOLD = int(os.environ.get("PROXY_BLOCKER_THRESHOLD", "2"))

# Markers written by the error-translation pass (lines ~2702-2737). Kept in
# one place so the blocker detector and the translation stay in sync.
# Order matters: longer/more specific markers are checked first.
_BLOCKER_ERROR_MARKERS = (
    ("wasted",            ["该文件自上次读取后未发生变化", "wasted call"]),
    ("file_not_found",    ["文件不存在", "file does not exist", "no such file"]),
    ("input_validation",  ["工具调用参数错误", "inputvalidationerror"]),
)

# ---------------------------------------------------------------------------
# Dynamic tool definition filtering: reduce token overhead from tool schemas
# ---------------------------------------------------------------------------
PROXY_TOOL_FILTER_ENABLED = os.environ.get("PROXY_TOOL_FILTER_ENABLED", "true" if not IS_CLOUD else "false").lower() in ("1", "true", "yes")
PROXY_TOOL_FILTER_MAX = int(os.environ.get("PROXY_TOOL_FILTER_MAX", "20"))
PROXY_TOOL_FILTER_RECENT = int(os.environ.get("PROXY_TOOL_FILTER_RECENT", "5"))
# Phase 1: use a tuple to preserve stable order for prefix cache alignment.
TOOL_ALWAYS_KEEP = (
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "LS", "Task", "WebFetch", "WebSearch",
    "TodoRead", "TodoWrite",
    "Skill", "Agent", "NotebookEdit",
    "EnterPlanMode", "ExitPlanMode",
    "AskUserQuestion",
    # MCP search tools — not in Claude Code's built-in set but essential
    # for real search results (SearXNG, Serper, WeChat).
    "mcp__searxng__search",
    "mcp__serper__google_search",
    "mcp__wechat-search__search_wechat",
)


# ---------------------------------------------------------------------------
# Keyword index (BM25 MVP): extract keywords from dropped messages and
# inject relevant context into tail for better continuity.
# ---------------------------------------------------------------------------
# TODO(roadmap-U1): BM25 Phase 2 — Bigram tokenization + inverted index
# TODO(roadmap-U1): BM25 Phase 3 — JSONL persistence for cross-session memory
PROXY_HISTORY_INDEX = os.environ.get("PROXY_HISTORY_INDEX", "rule")
PROXY_HISTORY_TOP_K = int(os.environ.get("PROXY_HISTORY_TOP_K", "5"))
PROXY_HISTORY_MAX_CHARS = int(os.environ.get("PROXY_HISTORY_MAX_CHARS", "500"))

# ---------------------------------------------------------------------------
# Semantic tool-result clearing: priority-based scoring
# ---------------------------------------------------------------------------
TOOL_SEMANTIC_PRIORITY = {
    "Read": 3, "Agent": 3, "WebFetch": 2, "WebSearch": 2,
    "Bash": 1, "Edit": 1, "Write": 1,
}
TOOL_RESULT_HIGH_VALUE_PATTERNS = [
    (re.compile(r'(function |class |def |import |from |\{\s*"[a-z]|\#include)', re.IGNORECASE), 3),
    (re.compile(r'(total \d+|drwx|\.py$|\.js$|\.ts$)', re.IGNORECASE), 1),
    (re.compile(r'(error|traceback|exception)', re.IGNORECASE), 2),
    (re.compile(r'Wasted call', re.IGNORECASE), 0),
]

# ---------------------------------------------------------------------------
# Structured request logging: JSON Lines to logs/proxy_requests.jsonl
# ---------------------------------------------------------------------------
_LOG_DIR = os.path.join(_SCRIPT_DIR, "logs")
_JSONL_PATH = os.path.join(_LOG_DIR, "proxy_requests.jsonl")
_jsonl_lock = threading.Lock()
# Maps request token -> output_chars (set by response handlers)
_jsonl_output_map = {}
_jsonl_counter = 0

# ---------------------------------------------------------------------------
# Structured metrics logging: JSON Lines to logs/proxy_metrics.jsonl
# Per-request pipeline stats for observability and tuning.
# ---------------------------------------------------------------------------
# All PROXY_METRICS_ENABLED, PROXY_METRICS_DIR, _METRICS_PATH, _metrics_lock,
# MODEL_ALIASES, _log_ctx, _metrics_ctx, and _state_lock are defined in
# proxy_state (single source of truth) and imported via from proxy_state import *.

import proxy_logging
from proxy_logging import *


from proxy_logging import *


from proxy_logging import *




from proxy_logging import *


from proxy_logging import *


LOG_SCHEMA_VERSION = "v1"


from proxy_logging import *


from proxy_logging import *


# ---------------------------------------------------------------------------
import tool_parser
from tool_parser import *
tool_parser._log = log  # wire structured logging without circular import

# (above moved to tool_parser.py: XML fallback, content-tools extraction,
#  parse_tool_arguments, _StreamingToolsExtractor, and helpers)


from message_converter import *

import lifecycle
from lifecycle import *
import content_compressor
from content_compressor import *



import truncation
from truncation import *


from truncation import *


from truncation import *


from truncation import *


# TODO(roadmap-U2): Phase-aware compression — detect exploration/implementation/debug stages
from truncation import *




from truncation import *


from truncation import *


from truncation import *


from truncation import *


from truncation import *


from truncation import *








from truncation import *


from truncation import *


from truncation import *


# ---------------------------------------------------------------------------
# Thinking/reasoning block stripping: remove old assistant thinking content
# to reduce context size. Operates defensively since current clients rarely
# send explicit thinking blocks (reasoning is usually inline text).
# (conversion helpers moved to message_converter.py)

import subprocess
import time

_LOG_PATH = os.path.join(_SCRIPT_DIR, "logs", "llama-server.log")
_PID_PATH = os.path.join(_SCRIPT_DIR, "llama-server.pid")


import admin_server
from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


from admin_server import *


# TODO(roadmap-U4): Auto-tune thresholds/budget from quality_flags history
from admin_server import *


from admin_server import *


import tool_filter
from tool_filter import *


from tool_filter import *


from tool_filter import *


# ---------------------------------------------------------------------------
# Error translation (R5.1 / R5.2) — extracted from _handle_messages for testability
# ---------------------------------------------------------------------------
from tool_filter import *


# ---------------------------------------------------------------------------
# Loop intervention (R2.1) — extracted from _handle_messages for testability
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    @staticmethod
    def _early_route_decision(parsed, total_chars):
        """Fast cloud/local heuristic used before OOM safety pre-truncation.

        Returns 'cloud' only when the request is unambiguously a cloud candidate
        based on model preference or context size. The full pipeline SmartRouter
        may still override this early hint (e.g. cloud cooldown / memory pressure).
        """
        if not PROXY_ROUTE_ENABLED:
            return "local"
        route_override = parsed.get("_x_proxy_route_to", "")
        if route_override == "cloud":
            return "cloud"
        if route_override == "local":
            return "local"

        requested_model = parsed.get("model", "")
        pref = MODEL_ROUTE_PREFERENCES.get(requested_model, {})
        behavior = pref.get("behavior", "prefer")
        route_bias = pref.get("route_bias", "auto")

        if behavior in ("force", "force_fallback") and route_bias == "prefer_cloud":
            return "cloud"
        if behavior == "prefer_cloud":
            return "cloud"

        threshold = int(PROXY_ROUTE_THRESHOLD_CHARS * pref.get("threshold_factor", 1.0))
        if total_chars > threshold:
            return "cloud"
        return "local"

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        raw_sid = self.headers.get("X-Claude-Code-Session-Id", "")[:8]
        if not raw_sid:
            client_addr = getattr(self, 'client_address', ('127.0.0.1', 0))
            client_key = f"{client_addr[0]}:{self.headers.get('User-Agent', '')}:{datetime.now().strftime('%Y-%m-%d')}"
            raw_sid = "cli_" + hashlib.md5(client_key.encode()).hexdigest()[:8]
        _log_ctx.session_id = raw_sid[:8]
        try:
            try:
                if self.path != "/status":
                    log(f"GET {self.path}")
                    log(f"  Headers: {_mask_sensitive(dict(self.headers))}")
                if self.path == "/v1/models":
                    aliases = _ps.get_model_aliases()
                    models = []
                    for name in aliases:
                        pref = _ps.MODEL_ROUTE_PREFERENCES.get(name, {})
                        route = pref.get("route_bias", "auto")
                        behavior = pref.get("behavior", "prefer")
                        if behavior in ("force", "force_fallback") and route == "prefer_cloud":
                            meta_route = "cloud"
                        elif behavior in ("force", "force_fallback") and route == "prefer_local":
                            meta_route = "local"
                        else:
                            meta_route = "auto"
                        models.append({
                            "id": name,
                            "object": "model",
                            "created": 1677610602,
                            "owned_by": "proxy-router",
                            "metadata": {"route": meta_route},
                        })
                    self._respond_json({"object": "list", "data": models})
                elif self.path == "/status":
                    html = _build_status_html()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    if not getattr(self, "_request_id", None):
                        self._request_id = f"req_{os.urandom(8).hex()}"
                    self.send_header("request-id", self._request_id)
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                elif self.path == "/metrics" or self.path.startswith("/metrics?"):
                    self._handle_metrics_endpoint()
                elif self.path == "/metrics/history":
                    self._handle_metrics_history_endpoint()
                elif self.path == "/session" or self.path.startswith("/session?"):
                    from urllib.parse import parse_qs, urlparse
                    params = parse_qs(urlparse(self.path).query)
                    sid = params.get("sid", [""])[0].strip()
                    if not sid:
                        self._respond_json({"detail": "missing sid"}, 400)
                    else:
                        analysis = _analyze_session(sid)
                        accept = self.headers.get("Accept", "")
                        if "application/json" in accept:
                            self._respond_json(analysis)
                        else:
                            html = _build_session_html(sid)
                            self.send_response(200)
                            self.send_header("Content-Type", "text/html; charset=utf-8")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            if not getattr(self, "_request_id", None):
                                self._request_id = f"req_{os.urandom(8).hex()}"
                            self.send_header("request-id", self._request_id)
                            self.end_headers()
                            self.wfile.write(html.encode("utf-8"))
                else:
                    self._respond_json({"detail": "Not found"}, 404)
            except Exception as e:
                log(f"  -> GET error: {e}", level="ERROR")
                self._respond_json({"error": {"type": "internal_error", "message": str(e)[:200]}}, 500)
        finally:
            _log_ctx.session_id = None

    def do_POST(self):
        raw_sid = self.headers.get("X-Claude-Code-Session-Id", "")[:8]
        if not raw_sid:
            client_addr = getattr(self, 'client_address', ('127.0.0.1', 0))
            client_key = f"{client_addr[0]}:{self.headers.get('User-Agent', '')}:{datetime.now().strftime('%Y-%m-%d')}"
            raw_sid = "cli_" + hashlib.md5(client_key.encode()).hexdigest()[:8]
        _log_ctx.session_id = raw_sid[:8]
        if PROXY_METRICS_ENABLED:
            _metrics_ctx.mc = {
                "ts": datetime.now().isoformat(),
                "session_id": getattr(_log_ctx, 'session_id', None) or "",
                "client_type": _detect_client_type(self.headers.get('User-Agent', '')),
                "pipeline": {},
            }
        try:
            content_len = int(self.headers.get("Content-Length", 0))

            # Phase 1: read body and parse JSON. We intentionally do NOT reject
            # oversized payloads here; instead we let SmartRouter decide whether
            # the request should go to cloud (which can accept larger bodies) or
            # local. The local size guard now lives in BackendDispatcher just
            # before forwarding to the local backend.
            body = self.rfile.read(content_len).decode("utf-8")
            log(f"POST {self.path}")
            log(f"  Headers: {_mask_sensitive(dict(self.headers))}")
            if _check_dedup(body):
                log(f"  -> Duplicate request detected (body hash match within {PROXY_DEDUP_WINDOW}s), skipping", level="WARN")
                self._respond_json(
                    {"error": {"type": "duplicate_request", "message": "Duplicate request within dedup window"}},
                    429,
                    extra_headers={"Retry-After": str(PROXY_DEDUP_WINDOW)},
                )
                return
            try:
                parsed = json.loads(body)
                # Extract X-Proxy-Route-To header for single-request route override
                parsed["_x_proxy_route_to"] = self.headers.get("X-Proxy-Route-To", "")
            except json.JSONDecodeError:
                log(f"  Body (invalid JSON): {body[:500]}", level="WARN")
                self._respond_json(
                    {"error": {"type": "invalid_request_error", "message": "Invalid JSON"}},
                    400,
                )
                return

            try:
                with open("/tmp/anthropic_request_body.json", "w") as f:
                    f.write(json.dumps(parsed, ensure_ascii=False, indent=2))
            except OSError as e:
                log(f"  debug body write failed: {e}")
            log(f"  Body: {json.dumps(parsed, ensure_ascii=False)[:1500]}")

            # Admin: force route target for a session
            if self.path == "/admin/route/force-local":
                self._handle_admin_route_force(parsed, "local")
                return
            if self.path == "/admin/route/force-cloud":
                self._handle_admin_route_force(parsed, "cloud")
                return

            if self.path == "/v1/messages" or self.path.startswith("/v1/messages?") or \
               self.path == "/v1/chat/completions" or self.path.startswith("/v1/chat/completions?"):
                is_openai_chat = self.path.startswith("/v1/chat/completions")
                if is_openai_chat:
                    openai_model = parsed.get("model", MODEL_NAME)
                    parsed = convert_openai_request_to_anthropic(parsed)
                    self._openai_mode = True
                    self._openai_model = openai_model
                    log(f"  -> OpenAI chat request converted to Anthropic pipeline (model={openai_model})")
                # Phase 3: memory pressure active rejection
                mem_rejected, used_pct = _should_reject_for_memory()
                if mem_rejected:
                    log(f"  -> Memory pressure rejection: used_pct={used_pct:.1f}% > threshold={PROXY_MEMORY_REJECT_THRESHOLD:.1f}%", level="WARN")
                    if PROXY_METRICS_ENABLED:
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["memory_rejected"] = True
                            mc["used_pct"] = used_pct
                            _finalize_metrics(mc)
                            log_metrics(mc)
                    self._respond_json(
                        {
                            "error": {
                                "type": "backend_oom",
                                "message": f"System memory pressure is high (used {used_pct:.1f}%). Retry after {PROXY_RETRY_AFTER_SECONDS}s.",
                                "retryable": True,
                            }
                        },
                        503,
                        extra_headers={"Retry-After": str(PROXY_RETRY_AFTER_SECONDS)},
                    )
                    return

                # Log request summary with timestamp for status page tracking
                msgs = parsed.get("messages", [])
                total_chars = len(json.dumps(msgs, ensure_ascii=False)) if msgs else 0
                tools = parsed.get("tools", [])
                log(f"[REQ_SUMMARY] chars={total_chars} tools={len(tools)}")
                # DEF-001 fix: pre-truncate very large payloads to prevent rapid-mlx
                # OOM and 500 errors. Evidence: 65/67 of v0.5.0-baseline 500s came
                # from input_chars > 400K (session a309b181). Force rounds truncation
                # with tight budget when payload exceeds threshold.
                # Skip pre-truncation if the request is already a clear cloud candidate;
                # otherwise aggressive pre-truncation can drop context below the routing
                # threshold and prevent automatic cloud fallback.
                early_route = self._early_route_decision(parsed, total_chars)
                if early_route == "cloud":
                    log(f"  -> OOM safety pre-truncation skipped: early route decision=cloud ({total_chars:,} chars)")
                elif total_chars > PROXY_OOM_SAFE_CHARS and msgs:
                        log(f"  -> OOM safety pre-truncation triggered: {total_chars:,} chars > {PROXY_OOM_SAFE_CHARS:,} threshold", level="WARN")
                        pre_session_id = getattr(_log_ctx, 'session_id', None) or ""
                        msgs_truncated, pre_stats = _apply_rounds_truncation(
                            msgs, keep_rounds=2, session_id=pre_session_id
                        )
                        if pre_stats.get("truncated"):
                            parsed = {**parsed, "messages": msgs_truncated}
                            msgs = msgs_truncated
                            total_chars = len(json.dumps(msgs_truncated, ensure_ascii=False))
                            log(f"  -> Pre-truncated: dropped={pre_stats.get('dropped_msgs', 0)}, "
                                f"kept={len(msgs_truncated)} msgs, now {total_chars:,} chars")
                            if PROXY_METRICS_ENABLED:
                                mc = getattr(_metrics_ctx, 'mc', None)
                                if mc:
                                    _mc_put("pre_truncate", {
                                        "triggered": True,
                                        "original_chars": pre_stats.get("original_chars", 0),
                                        "truncated_chars": total_chars,
                                        "dropped_msgs": pre_stats.get("dropped_msgs", 0),
                                        "kept_rounds": pre_stats.get("actual_keep_rounds", 2),
                                    })
                        else:
                            log(f"  -> Pre-truncation did not reduce payload, proceeding")
                # Timing wrapper for structured logging
                import time as _time
                _t0 = _time.monotonic()
                _req_start_time = datetime.now().isoformat()
                if not getattr(self, "_request_id", None):
                    self._request_id = f"req_{os.urandom(8).hex()}"
                _req_id = self._request_id
                self._last_jsonl_token = _next_jsonl_token()
                _jsonl_output_map[self._last_jsonl_token] = 0
                # Phase 3: request failure snapshot — save original body before processing
                _write_request_snapshot(_req_id, parsed)
                _snapshot_written = False
                try:
                    self._handle_messages(parsed)
                    _dur = (_time.monotonic() - _t0) * 1000
                    _out_chars = _jsonl_output_map.pop(self._last_jsonl_token, 0)
                    log_request(
                        model=parsed.get("model", "unknown"),
                        input_chars=total_chars,
                        output_chars=_out_chars,
                        status=200,
                        duration_ms=_dur,
                        start_time=_req_start_time,
                    )
                    _record_request_for_concurrency(_dur, 200)
                    if PROXY_METRICS_ENABLED:
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["output_chars"] = _out_chars
                            mc["duration_ms"] = round(_dur, 1)
                            mc["status"] = 200
                            _finalize_metrics(mc)
                            log_metrics(mc)
                except Exception as e:
                    _dur = (_time.monotonic() - _t0) * 1000
                    log(f"  -> Error: {e}", level="ERROR")
                    _jsonl_output_map.pop(self._last_jsonl_token, None)
                    status_code, _, _ = _classify_exception(e)
                    log_request(
                        model=parsed.get("model", "unknown"),
                        input_chars=total_chars,
                        output_chars=0,
                        status=500,
                        duration_ms=_dur,
                        start_time=_req_start_time,
                    )
                    _record_request_for_concurrency(_dur, status_code)
                    if PROXY_METRICS_ENABLED:
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["output_chars"] = 0
                            mc["duration_ms"] = round(_dur, 1)
                            mc["status"] = status_code
                            mc["error_type"] = type(e).__name__
                            mc["error"] = str(e)[:200]
                            _finalize_metrics(mc)
                            log_metrics(mc)
                    # Phase 3: failure snapshot — record pipeline state and error
                    if status_code >= 500:
                        _snapshot_written = _write_request_snapshot(_req_id, parsed, after_body=None, error=e)
                    # DEF-001 fix: classify error and return proper JSON response.
                    # Uses _classify_exception to pick 503/504/499/500 + Retry-After
                    # header for retryable errors (OOM, timeout, connection refused).
                    status_code, error_type, retryable = _classify_exception(e)
                    if status_code == 499:
                        # Client already disconnected, no point sending a response.
                        log(f"  -> Client disconnected (499): {type(e).__name__}", level="WARN")
                    else:
                        hdrs = {"Retry-After": str(PROXY_RETRY_AFTER_SECONDS)} if retryable else None
                        try:
                            self._respond_json(
                                {
                                    "error": {
                                        "type": error_type,
                                        "message": f"Proxy error: {type(e).__name__}: {str(e)[:500]}",
                                        "request_id": _req_id,
                                        "retryable": retryable,
                                    }
                                },
                                status_code,
                                extra_headers=hdrs,
                            )
                        except Exception as respond_err:
                            log(f"  -> CRITICAL: failed to send error response: {respond_err}", level="ERROR")
                    # No raise — let the connection close cleanly
                finally:
                    # Reset OpenAI chat mode flags for this connection
                    self._openai_mode = False
                    self._openai_model = None
                    # Phase 3: dynamic concurrency adjustment after every request
                    try:
                        _adjust_concurrency()
                    except Exception:
                        pass
                    if PROXY_METRICS_ENABLED:
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["snapshot_written"] = _snapshot_written
            else:
                log(f"  -> 404 (unknown path)", level="WARN")
                self._respond_json({"detail": "Not found"}, 404)
        finally:
            _log_ctx.session_id = None
            if PROXY_METRICS_ENABLED:
                _metrics_ctx.mc = None

    def _handle_messages(self, body):
        """Pipeline-based message processing — 22 stages."""
        # Emergency rollback: set PROXY_PIPELINE_DISABLED=1 to use the old path
        # (requires reverting to a prior commit that still has the legacy code).
        ctx = PipelineContext(
            body=body,
            request_id=getattr(self, '_request_id', ''),
            client_type=_detect_client_type(self.headers.get('User-Agent', '')),
        )
        InstrumentedPipeline([
            RequestParser(),              # 0
            LifecycleClassifier(),        # 1
            DynamicMaxTokens(),           # 2
            SmartRouter(),                # 2.5 — route decision (local vs cloud)
            RouteNotification(),          # 2.6 — route notification (log only in Phase 1)
            ErrorTranslator(),            # 3
            BlockerDetector(),            # 4
            SystemNormalizer(),           # 5
            CacheAligner(),               # 6
            ContentCompressor(),          # 7
            ToolLoopDetector(),           # 8
            TextLoopDetector(),           # 9
            SessionLoopState(),           # 10
            LoopIntervention(),           # 11
            RereadDetector(),             # 12
            DateNormalizer(),             # 13
            ContextTruncator(),           # 14
            HighDropRatioNotice(),        # 15
            MessageHashDebug(),           # 16
            OOMSafetyFIFO(),              # 17
            PrefixRatioComputer(),        # 18
            ToolPairingRepair(),          # 19
            FormatConverter(),            # 20
            BackendDispatcher(            # 21
                llama_lock=_llama_lock,
                cloud_lock=_ps._cloud_lock,
                handler=self,
            ),
        ]).run(ctx)
        # Response already written to self.wfile by BackendDispatcher.

    def _handle_non_streaming_response(self, resp, anthropic_body):
        openai_resp = json.loads(resp.read().decode("utf-8"))

        if getattr(self, '_openai_mode', False):
            # Pipeline processed an OpenAI-format request; return the backend's
            # OpenAI-format response directly (after optional output truncation).
            max_tokens = anthropic_body.get("max_tokens", 4096)
            output_token_hard_limit = int(max_tokens * PROXY_OUTPUT_TOKEN_LIMIT_RATIO)
            output_chars_limit = int(output_token_hard_limit / 0.4)
            output_chars = 0
            force_stopped = False

            choice = openai_resp.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content") or ""
            if content:
                output_chars += len(content)
                if output_chars > output_chars_limit:
                    message["content"] = (
                        content[:output_chars_limit - (output_chars - len(content))]
                        + "\n\n[Output truncated by proxy: exceeded token limit]"
                    )
                    force_stopped = True
                    output_chars = output_chars_limit

            for tc in message.get("tool_calls") or []:
                raw_args = tc.get("function", {}).get("arguments", "{}")
                output_chars += len(raw_args)
                if output_chars > output_chars_limit and not force_stopped:
                    tc["function"]["arguments"] = raw_args[:output_chars_limit - (output_chars - len(raw_args))]
                    force_stopped = True
                    output_chars = output_chars_limit

            if force_stopped:
                choice["finish_reason"] = "length"
                log(f"  -> FORCE_STOPPED at {output_chars} chars (limit={output_chars_limit})", level="WARN")

            content_summary = message.get("content", "")[:100]
            for tc in message.get("tool_calls") or []:
                content_summary += f"[tool_call: {tc.get('function', {}).get('name', '')}] "
            _jsonl_output_map[self._last_jsonl_token] = output_chars
            log(f"  <- Responding OpenAI mode: {content_summary[:200]} (output_chars={output_chars})")
            self._respond_json(openai_resp)
            return

        anthropic_resp = convert_openai_response_to_anthropic(
            openai_resp,
            anthropic_body.get("model", "claude-3-5-sonnet-20241022")
        )

        # Output token truncation for non-streaming path
        max_tokens = anthropic_body.get("max_tokens", 4096)
        output_token_hard_limit = int(max_tokens * PROXY_OUTPUT_TOKEN_LIMIT_RATIO)
        output_chars_limit = int(output_token_hard_limit / 0.4)
        output_chars = 0
        force_stopped = False

        for block in anthropic_resp.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                output_chars += len(text)
                if output_chars > output_chars_limit:
                    block["text"] = text[:output_chars_limit - (output_chars - len(text))]
                    block["text"] += "\n\n[Output truncated by proxy: exceeded token limit]"
                    force_stopped = True
                    output_chars = output_chars_limit
                    break

        if force_stopped:
            anthropic_resp["stop_reason"] = "max_tokens"
            log(f"  -> FORCE_STOPPED at {output_chars} chars (limit={output_chars_limit})", level="WARN")
            for block in anthropic_resp.get("content", []):
                if block.get("type") == "tool_use" and block.get("input") == {}:
                    tool_name = block.get("name", "")
                    for tc in (openai_resp.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []):
                        if tc.get("function", {}).get("name") == tool_name:
                            raw_args = tc["function"].get("arguments", "{}")
                            try:
                                json.loads(raw_args)
                            except json.JSONDecodeError:
                                repaired = _repair_truncated_json(raw_args)
                                parsed = json.loads(repaired) if repaired else {}
                                block["input"] = parsed
                                log(f"  -> Repaired truncated JSON for tool {tool_name}")
                            break

        content_summary = ""
        output_chars = 0
        for block in anthropic_resp.get("content", []):
            if block.get("type") == "text":
                content_summary += block.get("text", "")[:100]
                output_chars += len(block.get("text", ""))
            elif block.get("type") == "tool_use":
                content_summary += f"[tool_use: {block.get('name', '')}] "
                output_chars += len(json.dumps(block.get("input", {}), ensure_ascii=False))
        _jsonl_output_map[self._last_jsonl_token] = output_chars
        log(f"  <- Responding: {content_summary[:200]} (output_chars={output_chars})")
        self._respond_json(anthropic_resp)

    # TODO(roadmap-U7): Stream reasoning progress — emit partial thinking events during long TTFT
    def _handle_streaming_response(self, resp, anthropic_body):
        if getattr(self, '_openai_mode', False):
            self._handle_openai_streaming_response(resp)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        if not getattr(self, "_request_id", None):
            self._request_id = f"req_{os.urandom(8).hex()}"
        self.send_header("request-id", self._request_id)
        route_headers = getattr(self, '_route_response_headers', None) or {}
        for hk, hv in route_headers.items():
            self.send_header(hk, str(hv))
        self._route_response_headers = None
        self.end_headers()

        model_name = anthropic_body.get("model", "claude-3-5-sonnet-20241022")
        msg_id = f"msg_{os.urandom(8).hex()}"
        total_text = ""
        tool_calls_buffer = {}
        input_tokens = 0
        output_tokens = 0
        text_block_started = False
        tools_extractor = _StreamingToolsExtractor()
        content_tools_pending = []
        _first_token_time = None

        # Output token truncation for streaming path
        max_tokens = anthropic_body.get("max_tokens", 4096)
        output_token_hard_limit = int(max_tokens * PROXY_OUTPUT_TOKEN_LIMIT_RATIO)
        output_char_count = 0
        output_force_stopped = False

        def _emit_text_delta(t):
            """Emit a text delta SSE event, opening the text block lazily."""
            nonlocal text_block_started, total_text, output_char_count, output_force_stopped
            if not t:
                return
            try:
                if not text_block_started:
                    text_block_started = True
                    self.wfile.write(
                        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                    )
                total_text += t
                output_char_count += len(t)
                ev = f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":{json.dumps(t)}}}}}\n\n'
                self.wfile.write(ev.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Client disconnected, stop emitting

        # Send message_start (usage will be updated from llama-server timings)
        event = {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model_name,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}
            }
        }
        self.wfile.write(f"event: message_start\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

        stream_finish_reason = None
        last_chunk = None
        for line in resp:
            line = line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                last_chunk = chunk
            except json.JSONDecodeError:
                continue

            if output_force_stopped:
                break

            if _first_token_time is None:
                _first_token_time = time.monotonic()

            choice = chunk.get("choices", [{}])[0]
            delta = choice.get("delta", {})

            # Track finish_reason from the stream
            if choice.get("finish_reason"):
                stream_finish_reason = choice["finish_reason"]

            # Extract usage from llama-server timings or OpenAI/DeepSeek usage field
            timings = chunk.get("timings")
            if timings:
                input_tokens = timings.get("prompt_n", input_tokens)
                output_tokens = timings.get("predicted_n", output_tokens)
            usage = chunk.get("usage")
            if usage:
                input_tokens = usage.get("prompt_tokens", input_tokens)
                output_tokens = usage.get("completion_tokens", output_tokens)

            # Handle tool_calls in streaming
            tc_delta = delta.get("tool_calls")
            if tc_delta:
                for tc in tc_delta:
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    if tc.get("id"):
                        tool_calls_buffer[idx]["id"] += tc["id"]
                    if tc.get("function", {}).get("name"):
                        tool_calls_buffer[idx]["function"]["name"] += tc["function"]["name"]
                    if tc.get("function", {}).get("arguments"):
                        args_chunk = tc["function"]["arguments"]
                        tool_calls_buffer[idx]["function"]["arguments"] += args_chunk
                        output_char_count += len(args_chunk)
                        est_tokens = output_char_count * 0.4
                        if est_tokens > output_token_hard_limit:
                            tool_name = tool_calls_buffer[idx]["function"].get("name", "?")
                            log(f"  -> !! Output token limit on tool_call: est={int(est_tokens)}, limit={output_token_hard_limit}, tool={tool_name}, forcing stop", level="WARN")
                            output_force_stopped = True
                            break
                continue

            # Check text output token limit
            if output_char_count * 0.4 > output_token_hard_limit:
                log(f"  -> FORCE_STOPPED: output est={int(output_char_count * 0.4)} tokens, limit={output_token_hard_limit}", level="WARN")
                output_force_stopped = True
                break

            # Handle content text — pass through state machine that strips <tools> blocks
            text = delta.get("content", "") or delta.get("reasoning_content", "")
            if not text:
                continue
            for kind, value in tools_extractor.feed(text):
                if kind == "text":
                    _emit_text_delta(value)
                else:  # "tool"
                    content_tools_pending.append(value)

        # Flush any unfinished state-machine state.
        for kind, value in tools_extractor.finalize():
            if kind == "text":
                _emit_text_delta(value)
            else:
                content_tools_pending.append(value)

        # Prefer structured tool_calls; fallback to content-extracted tools if buffer is empty
        # or if structured args are empty/incomplete (Qwen3.6 qwen3_coder_xml parser bug).
        if content_tools_pending:
            if not tool_calls_buffer:
                for i, t in enumerate(content_tools_pending):
                    tool_calls_buffer[i] = {
                        "id": f"call_{os.urandom(8).hex()}",
                        "type": "function",
                        "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])},
                    }
            else:
                # Replace empty/incomplete structured args with content-extracted ones
                for idx in list(tool_calls_buffer.keys()):
                    raw_args = tool_calls_buffer[idx]["function"].get("arguments", "")
                    parsed = parse_tool_arguments(raw_args, tool_calls_buffer[idx]["function"].get("name", ""))
                    if not parsed:
                        for t in content_tools_pending:
                            if t["name"] == tool_calls_buffer[idx]["function"].get("name"):
                                tool_calls_buffer[idx]["function"]["arguments"] = json.dumps(t["arguments"])
                                log(f"  [CONTENT_TOOLS_FALLBACK] replaced empty args for {t['name']} from content text")
                                break

        # Repair truncated JSON in tool_call arguments unconditionally
        for idx in tool_calls_buffer:
            tc = tool_calls_buffer[idx]
            raw_args = tc["function"].get("arguments", "{}")
            try:
                json.loads(raw_args)
            except json.JSONDecodeError:
                if _is_truncated_json(raw_args):
                    log(f"  [JSON_TRUNCATED] streamed tool={tc['function'].get('name', '?')}, raw={raw_args[:200]!r}")
                repaired = _repair_truncated_json(raw_args)
                tc["function"]["arguments"] = repaired
                tool_name = tc["function"].get("name", "?")
                try:
                    json.loads(repaired)
                    log(f"  [JSON_REPAIRED] streamed tool={tool_name}: {len(raw_args)} -> {len(repaired)} chars")
                except json.JSONDecodeError:
                    log(f"  [JSON_TRUNCATED_REPAIR_FAILED] streamed tool={tool_name}: {len(raw_args)} -> {len(repaired)} chars")

        # Send content_block_stop for text (only if text was output)
        if text_block_started:
            self.wfile.write(
                f'event: content_block_stop\ndata: {{"type":"content_block_stop","index":0}}\n\n'
                .encode("utf-8")
            )

        # Send tool_use blocks if any
        tool_call_idx = 1 if not text_block_started else 1
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]
            if not tc["function"].get("name"):
                continue
            # Ensure tool_call id is present (some backends omit it in streaming)
            tc_id = tc.get("id", "") or f"call_{os.urandom(8).hex()}"
            tool_name = tc["function"].get("name", "")
            raw_args = tc["function"].get("arguments", "{}")
            input_data = parse_tool_arguments(raw_args, tool_name)

            # content_block_start for tool_use (Anthropic SDK expects input to start empty)
            event = {
                "type": "content_block_start",
                "index": tool_call_idx,
                "content_block": {
                    "type": "tool_use",
                    "id": tc_id,
                    "name": tool_name,
                    "input": {},
                }
            }
            self.wfile.write(f"event: content_block_start\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

            # Send input_json_delta with the actual parameters
            input_json = json.dumps(input_data, ensure_ascii=False)
            event = {
                "type": "content_block_delta",
                "index": tool_call_idx,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": input_json,
                }
            }
            self.wfile.write(f"event: content_block_delta\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

            # content_block_stop for tool_use
            event = {"type": "content_block_stop", "index": tool_call_idx}
            self.wfile.write(f"event: content_block_stop\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
            tool_call_idx += 1

        # Determine stop_reason
        stop_reason = "end_turn"
        if stream_finish_reason == "tool_calls" or tool_calls_buffer:
            stop_reason = "tool_use"
        elif stream_finish_reason == "length":
            stop_reason = "max_tokens"

        # Send message_delta with usage (required by Anthropic SDK)
        event = {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens}
        }
        try:
            self.wfile.write(f"event: message_delta\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

            # Send message_stop
            event = {"type": "message_stop"}
            self.wfile.write(f"event: message_stop\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected before stream end
        if _first_token_time is not None:
            mc = getattr(_metrics_ctx, 'mc', None)
            if mc:
                mc["ttft_ms"] = round((time.monotonic() - _first_token_time) * 1000, 1)
        _jsonl_output_map[self._last_jsonl_token] = len(total_text)
        log(f"  <- Streamed text={len(total_text)} chars, tools={len(tool_calls_buffer)}")

    def _handle_openai_streaming_response(self, resp):
        """Passthrough an OpenAI-format streaming response for /v1/chat/completions."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        if not getattr(self, "_request_id", None):
            self._request_id = f"req_{os.urandom(8).hex()}"
        self.send_header("request-id", self._request_id)
        route_headers = getattr(self, '_route_response_headers', None) or {}
        for hk, hv in route_headers.items():
            self.send_header(hk, str(hv))
        self._route_response_headers = None
        self.end_headers()

        total_text = ""
        try:
            for line in resp:
                self.wfile.write(line)
                try:
                    decoded = line.decode("utf-8").strip()
                    if decoded.startswith("data: "):
                        data_str = decoded[6:].strip()
                        if data_str and data_str != "[DONE]":
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            total_text += delta.get("content", "") or ""
                except Exception:
                    pass
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected
        _jsonl_output_map[self._last_jsonl_token] = len(total_text)
        log(f"  <- Streamed OpenAI mode text={len(total_text)} chars")

    def _handle_admin_route_force(self, parsed, target):
        """Handle POST /admin/route/force-local|force-cloud — force route target for a session."""
        session_id = parsed.get("session_id", "")
        if not session_id:
            self._respond_json({"error": {"message": "Missing session_id"}}, 400)
            return
        with _ps._state_lock:
            _ps._SESSION_ROUTE_MAP[session_id] = "local_forced" if target == "local" else "cloud"
            _ps._SESSION_ROUTE_FORCE_SOURCE[session_id] = "user_manual"
            # Clear any existing cooldown/failure state
            _ps._cloud_cooldown_start.pop(session_id, None)
            _ps._cloud_fail_count.pop(session_id, None)
        log(f"  -> [admin] Session {session_id} route forced to {target} (user_manual)")
        self._respond_json({"ok": True, "session_id": session_id, "route_target": target})

    def _respond_json(self, data, status=200, extra_headers=None):
        # When serving an OpenAI-format client, shape error responses in the
        # OpenAI style {error: {message, type, param, code}}.
        if getattr(self, '_openai_mode', False) and status != 200 and isinstance(data, dict) and 'error' in data:
            err = data['error']
            data = {
                "error": {
                    "message": err.get("message", ""),
                    "type": err.get("type", "invalid_request_error"),
                    "param": err.get("param"),
                    "code": err.get("code", status),
                }
            }
        raw = json.dumps(data, ensure_ascii=False)
        raw_bytes = raw.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if not getattr(self, "_request_id", None):
            self._request_id = f"req_{os.urandom(8).hex()}"
        self.send_header("request-id", self._request_id)
        route_headers = getattr(self, '_route_response_headers', None) or {}
        for hk, hv in route_headers.items():
            self.send_header(hk, str(hv))
        self._route_response_headers = None
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        log(f"  <- Response body: {raw[:500]}")
        self.wfile.write(raw_bytes)

    def _handle_metrics_endpoint(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        last_n = int(params.get("n", ["100"])[0])
        metrics_dir = os.environ.get("PROXY_METRICS_DIR", "logs")
        metrics_path = os.path.join(metrics_dir, "proxy_metrics.jsonl")
        records = []
        try:
            with open(metrics_path, "r") as f:
                lines = f.readlines()
            for line in lines[-last_n:]:
                try:
                    records.append(json.loads(line.strip()))
                except (json.JSONDecodeError, ValueError):
                    pass
        except (FileNotFoundError, OSError):
            pass
        total = len(records)
        if total == 0:
            self._respond_json({"schema": "v2", "total": 0})
            return
        status_counts = {}
        quality_flag_counts = {}
        tool_usage = {}
        loop_triggered = 0
        blocker_triggered = 0
        truncation_triggered = 0
        memory_rejected = 0
        snapshot_written = 0
        dynamic_concurrent_events = 0
        input_chars_all = []
        ttft_all = []
        for r in records:
            s = r.get("status", "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1
            for qf in r.get("quality_flags", []):
                quality_flag_counts[qf] = quality_flag_counts.get(qf, 0) + 1
            pipeline = r.get("pipeline", {})
            if pipeline.get("loop_detect", {}).get("max_run", 0) >= PROXY_LOOP_THRESHOLD:
                loop_triggered += 1
            if pipeline.get("blocker_detect", {}).get("triggered"):
                blocker_triggered += 1
            if pipeline.get("truncate", {}).get("triggered"):
                truncation_triggered += 1
            for t in r.get("tools") or []:
                tool_usage[t] = tool_usage.get(t, 0) + 1
            if r.get("memory_rejected"):
                memory_rejected += 1
            if r.get("snapshot_written"):
                snapshot_written += 1
            dc = r.get("dynamic_concurrent", {})
            if isinstance(dc, dict) and dc.get("adjusted"):
                dynamic_concurrent_events += 1
            ic = r.get("input_chars", 0)
            if isinstance(ic, (int, float)) and ic > 0:
                input_chars_all.append(ic)
            ttft = r.get("ttft_ms", 0)
            if isinstance(ttft, (int, float)) and ttft > 0:
                ttft_all.append(ttft)
        top_tools = sorted(tool_usage.items(), key=lambda x: -x[1])[:10]

        def _percentile(vals, p):
            if not vals:
                return 0
            s = sorted(vals)
            k = (len(s) - 1) * p
            f = int(k)
            c = min(f + 1, len(s) - 1)
            if f == c:
                return float(s[f])
            return s[f] + (s[c] - s[f]) * (k - f)

        session_size = {}
        if input_chars_all:
            session_size = {
                "p50": int(_percentile(input_chars_all, 0.50)),
                "p95": int(_percentile(input_chars_all, 0.95)),
                "p99": int(_percentile(input_chars_all, 0.99)),
                "max": max(input_chars_all),
                "avg": int(sum(input_chars_all) / len(input_chars_all)),
            }
        ttft_stats = {}
        if ttft_all:
            ttft_stats = {
                "p50_ms": round(_percentile(ttft_all, 0.50), 1),
                "p95_ms": round(_percentile(ttft_all, 0.95), 1),
                "p99_ms": round(_percentile(ttft_all, 0.99), 1),
                "max_ms": round(max(ttft_all), 1),
                "avg_ms": round(sum(ttft_all) / len(ttft_all), 1),
            }

        self._respond_json({
            "schema": "v2",
            "total": total,
            "status": status_counts,
            "quality_flags": quality_flag_counts,
            "loop_triggered": loop_triggered,
            "blocker_triggered": blocker_triggered,
            "truncation_triggered": truncation_triggered,
            "memory_rejected": memory_rejected,
            "snapshot_written": snapshot_written,
            "dynamic_concurrent_events": dynamic_concurrent_events,
            "top_tools": [{"name": n, "count": c} for n, c in top_tools],
            "session_size": session_size,
            "ttft": ttft_stats,
            "last_n": last_n,
        })

    def _handle_metrics_history_endpoint(self):
        from collections import defaultdict
        metrics_dir = os.environ.get("PROXY_METRICS_DIR", "logs")
        metrics_path = os.path.join(metrics_dir, "proxy_metrics.jsonl")
        records = []
        try:
            with open(metrics_path, "r") as f:
                for line in f:
                    try:
                        records.append(json.loads(line.strip()))
                    except (json.JSONDecodeError, ValueError):
                        pass
        except (FileNotFoundError, OSError):
            pass
        if not records:
            self._respond_json({"schema": "v2", "buckets": []})
            return

        hourly = defaultdict(lambda: {
            "count": 0, "status_200": 0, "status_500": 0, "status_503": 0, "status_499": 0,
            "latencies": [], "input_chars": [], "output_chars": [],
            "loop_injected": 0, "blocker_injected": 0, "high_drop_ratio": 0,
            "truncation_triggered": 0, "memory_rejected": 0,
        })
        for r in records:
            ts_str = r.get("ts", "")
            if not ts_str:
                continue
            try:
                dt = datetime.fromisoformat(ts_str)
                bucket = dt.strftime("%Y-%m-%d %H:00")
            except ValueError:
                continue
            b = hourly[bucket]
            b["count"] += 1
            status = r.get("status", 0)
            if status == 200:
                b["status_200"] += 1
            elif status == 500:
                b["status_500"] += 1
            elif status == 503:
                b["status_503"] += 1
            elif status == 499:
                b["status_499"] += 1
            dur = r.get("duration_ms", 0)
            if isinstance(dur, (int, float)) and dur > 0:
                b["latencies"].append(dur)
            ic = r.get("input_chars", 0)
            if isinstance(ic, (int, float)) and ic > 0:
                b["input_chars"].append(ic)
            oc = r.get("output_chars", 0)
            if isinstance(oc, (int, float)) and oc > 0:
                b["output_chars"].append(oc)
            for qf in r.get("quality_flags", []):
                if qf == "loop_injected":
                    b["loop_injected"] += 1
                elif qf == "blocker_injected":
                    b["blocker_injected"] += 1
                elif qf == "high_drop_ratio":
                    b["high_drop_ratio"] += 1
            pipeline = r.get("pipeline", {})
            if pipeline.get("truncate", {}).get("triggered"):
                b["truncation_triggered"] += 1
            if r.get("memory_rejected"):
                b["memory_rejected"] += 1

        def _p50(vals):
            if not vals:
                return 0
            s = sorted(vals)
            return s[len(s) // 2]

        def _p95(vals):
            if not vals:
                return 0
            s = sorted(vals)
            k = int(len(s) * 0.95)
            return s[min(k, len(s) - 1)]

        buckets = []
        for bucket_key in sorted(hourly.keys()):
            b = hourly[bucket_key]
            buckets.append({
                "ts": bucket_key,
                "count": b["count"],
                "status_200": b["status_200"],
                "status_500": b["status_500"],
                "status_503": b["status_503"],
                "status_499": b["status_499"],
                "success_rate": round(b["status_200"] / max(b["count"], 1) * 100, 1),
                "latency_p50_ms": round(_p50(b["latencies"]), 1),
                "latency_p95_ms": round(_p95(b["latencies"]), 1),
                "avg_input_chars": round(sum(b["input_chars"]) / max(len(b["input_chars"]), 1), 0) if b["input_chars"] else 0,
                "avg_output_chars": round(sum(b["output_chars"]) / max(len(b["output_chars"]), 1), 0) if b["output_chars"] else 0,
                "loop_injected": b["loop_injected"],
                "blocker_injected": b["blocker_injected"],
                "high_drop_ratio": b["high_drop_ratio"],
                "truncation_triggered": b["truncation_triggered"],
                "memory_rejected": b["memory_rejected"],
            })
        self._respond_json({"schema": "v2", "buckets": buckets})


lifecycle._get_system_memory = _get_system_memory


admin_server._log = log

proxy_logging._log = log

truncation._log = log
def main():
    port = int(os.environ.get("PORT", "4000"))
    host = os.environ.get("HOST", "127.0.0.1")
    log(f"=== Starting Anthropic proxy on http://{host}:{port} ===")
    log(f"Backend type: {BACKEND_TYPE}")
    log(f"Forwarding to: {LLAMA_BASE}")
    log(f"Model: {MODEL_NAME}")
    log(f"Concurrency: {PROXY_MAX_CONCURRENT}")
    log(f"Tool clearing: {'enabled (threshold=' + str(PROXY_CLEAR_THRESHOLD) + ', keep=' + str(PROXY_TOOL_KEEP) + ')' if PROXY_CLEAR_ENABLED else 'disabled (' + BACKEND_TYPE + ' backend)'}")
    log(f"Blocker tracker: {'enabled (threshold=' + str(PROXY_BLOCKER_THRESHOLD) + ' consecutive)' if PROXY_BLOCKER_ENABLED else 'disabled (' + BACKEND_TYPE + ' backend)'}")
    _trunc_info = f"strategy={PROXY_CTX_TRUNCATE_STRATEGY}"
    if PROXY_CTX_TRUNCATE_STRATEGY == "rounds":
        _trunc_info += f", rounds={PROXY_CTX_KEEP_ROUNDS}, budget={PROXY_CTX_TOKEN_BUDGET}"
    elif PROXY_CTX_TRUNCATE_STRATEGY == "fifo":
        _trunc_info += f", keep_messages={PROXY_CTX_KEEP_MESSAGES}"
    elif PROXY_CTX_TRUNCATE_STRATEGY == "smart":
        _trunc_info += f", budget_chars={PROXY_CHARS_EXPANSION}"
    else:
        _trunc_info += f", limit={PROXY_CTX_CHARS_LIMIT}"
    log(f"Context limit: {'enabled (' + _trunc_info + ')' if PROXY_CTX_LIMIT_ENABLED else 'disabled (' + BACKEND_TYPE + ' backend)'}")
    log(f"Backend timeout: {PROXY_BACKEND_TIMEOUT}s, output token limit: {PROXY_OUTPUT_TOKEN_LIMIT_RATIO}x max_tokens, max_tokens override: {PROXY_MAX_TOKENS_OVERRIDE}")
    log(f"Dynamic token ratio: chinese={PROXY_TOKEN_RATIO_CHINESE}, english={PROXY_TOKEN_RATIO_ENGLISH}, code={PROXY_TOKEN_RATIO_CODE}")
    log(f"Memory reject threshold: {PROXY_MEMORY_REJECT_THRESHOLD}%")
    log(f"Dynamic max_tokens: {'enabled' if PROXY_DYNAMIC_MAX_TOKENS_ENABLED else 'disabled'}")
    log(f"Dynamic concurrency: {'enabled' if PROXY_DYNAMIC_CONCURRENT_ENABLED else 'disabled'} (min={PROXY_DYNAMIC_CONCURRENT_MIN}, max={PROXY_DYNAMIC_CONCURRENT_MAX})")
    log(f"Failure snapshots: {'enabled' if PROXY_SNAPSHOT_ENABLED else 'disabled'}")
    if IS_CLOUD:
        log(f"Cloud API mode — no local backend required")
    class ReusableThreadingHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True

    ReusableThreadingHTTPServer((host, port), Handler).serve_forever()

if __name__ == "__main__":
    main()
