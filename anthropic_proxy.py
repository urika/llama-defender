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
import proxy_state
import proxy_config

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_SCHEMA_VERSION = "v1"


# ---------------------------------------------------------------------------
# SIGHUP hot-reload: re-read active.conf and update config in proxy_state + self.
# Dual setattr (proxy_state + self_mod) ensures both sub-modules (which read
# proxy_state.PROXY_* at call time) and local functions (which reference
# module-level names imported via `from proxy_state import *`) see updates.
# ---------------------------------------------------------------------------

def _reload_config(signum=None, frame=None):
    """SIGHUP handler: re-read active.conf and update proxy_state + self module."""
    with _RELOAD_LOCK:
        self_mod = sys.modules[__name__]
        env = _parse_conf_env(RELOAD_CONFIG_PATH)
        secret_env = _parse_conf_env(RELOAD_SECRET_PATH)
        if secret_env:
            env.update({k: v for k, v in secret_env.items() if k not in env})
        if not env:
            log("[RELOAD] no config parsed from %s — keeping current values"
                % RELOAD_CONFIG_PATH, level="WARN")
            return

        # --- Backend routing ---
        if "LLAMA_BASE_URL" in env:
            base = env["LLAMA_BASE_URL"]
        else:
            host = env.get("LLAMA_HOST", "127.0.0.1")
            port = env.get("LLAMA_PORT", "8081")
            base = "http://%s:%s/v1" % (host, port)
        setattr(proxy_state, "LLAMA_BASE", base)
        setattr(self_mod, "LLAMA_BASE", base)
        api_key = env.get("LLAMA_API_KEY", getattr(self_mod, "LLAMA_API_KEY"))
        setattr(proxy_state, "LLAMA_API_KEY", api_key)
        setattr(self_mod, "LLAMA_API_KEY", api_key)

        bt = env.get("BACKEND_TYPE", "")
        if not bt:
            low = base.lower()
            if "deepseek" in low or "openai" in low or "api." in low:
                bt = "cloud"
            else:
                bt = "local"
        setattr(proxy_state, "BACKEND_TYPE", bt)
        setattr(self_mod, "BACKEND_TYPE", bt)
        is_cloud = bt == "cloud"
        setattr(proxy_state, "IS_CLOUD", is_cloud)
        setattr(self_mod, "IS_CLOUD", is_cloud)

        # MODEL_NAME
        model = env.get("MODEL_NAME") or env.get("LLAMA_MODEL",
                                                  getattr(self_mod, "MODEL_NAME"))
        setattr(proxy_state, "MODEL_NAME", model)
        setattr(self_mod, "MODEL_NAME", model)

        # --- Concurrency + Semaphore rebuild ---
        new_max = int(env.get("PROXY_MAX_CONCURRENT", "4" if is_cloud else "1"))
        old_max = getattr(self_mod, "PROXY_MAX_CONCURRENT")
        setattr(proxy_state, "PROXY_MAX_CONCURRENT", new_max)
        setattr(self_mod, "PROXY_MAX_CONCURRENT", new_max)
        if new_max != old_max:
            setattr(proxy_state, "_llama_lock", threading.Semaphore(new_max))
            setattr(self_mod, "_llama_lock", threading.Semaphore(new_max))
            log("[RELOAD] Semaphore rebuilt: %d -> %d" % (old_max, new_max))

        # --- MODEL_ALIASES rebuild ---
        aliases = [
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
            "claude-3-5-haiku-20241022",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-opus-4-7",
            "default",
            model,
        ]
        setattr(proxy_state, "MODEL_ALIASES", aliases)
        setattr(self_mod, "MODEL_ALIASES", aliases)

        # --- Tier 1 scalars ---
        for env_key, py_name, cast, cloud_def, local_def in _RELOAD_SPEC:
            default = cloud_def if is_cloud else local_def
            raw = env.get(env_key, default)
            val = _cast_config_value(raw, cast)
            setattr(proxy_state, py_name, val)
            setattr(self_mod, py_name, val)

        # --- Dependent defaults ---
        loop_thr = int(env.get("PROXY_LOOP_THRESHOLD",
                               getattr(self_mod, "PROXY_LOOP_THRESHOLD")))
        setattr(proxy_state, "PROXY_LOOP_THRESHOLD", loop_thr)
        setattr(self_mod, "PROXY_LOOP_THRESHOLD", loop_thr)
        setattr(proxy_state, "PROXY_LOOP_LEVEL2",
                int(env.get("PROXY_LOOP_LEVEL2", str(loop_thr * 2))))
        setattr(self_mod, "PROXY_LOOP_LEVEL2",
                int(env.get("PROXY_LOOP_LEVEL2", str(loop_thr * 2))))
        setattr(proxy_state, "PROXY_LOOP_LEVEL3",
                int(env.get("PROXY_LOOP_LEVEL3", str(loop_thr * 3))))
        setattr(self_mod, "PROXY_LOOP_LEVEL3",
                int(env.get("PROXY_LOOP_LEVEL3", str(loop_thr * 3))))

        sat = (env.get("PROXY_CHARS_SATURATION")
               or env.get("PROXY_CTX_CHARS_LIMIT",
                          "500000" if is_cloud else "180000"))
        setattr(proxy_state, "PROXY_CHARS_SATURATION", int(sat))
        setattr(self_mod, "PROXY_CHARS_SATURATION", int(sat))

        oom = (env.get("PROXY_OOM_SAFE_CHARS")
               or env.get("PROXY_PRE_TRUNCATE_CHARS",
                          "10000000" if is_cloud else "200000"))
        setattr(proxy_state, "PROXY_OOM_SAFE_CHARS", int(oom))
        setattr(self_mod, "PROXY_OOM_SAFE_CHARS", int(oom))
        setattr(proxy_state, "PROXY_PRE_TRUNCATE_CHARS", int(oom))
        setattr(self_mod, "PROXY_PRE_TRUNCATE_CHARS", int(oom))

        log("[RELOAD] OK: backend=%s base=%s model=%s concurrent=%d "
            "clear=%s ctx_limit=%s frozen=%d truncate=%s"
            % (bt, base[:60], model, new_max,
               getattr(self_mod, "PROXY_CLEAR_ENABLED"),
               getattr(self_mod, "PROXY_CTX_LIMIT_ENABLED"),
               getattr(self_mod, "PROXY_FROZEN_HEAD"),
               getattr(self_mod, "PROXY_CTX_TRUNCATE_STRATEGY")))


# Register SIGHUP handler (must be in main thread at import time).
signal.signal(signal.SIGHUP, _reload_config)


import loop_detection
from loop_detection import *








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

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        raw_sid = self.headers.get("X-Claude-Code-Session-Id", "")[:8]
        _log_ctx.session_id = raw_sid or f"req_{os.urandom(4).hex()}"
        try:
            if self.path != "/status":
                log(f"GET {self.path}")
                log(f"  Headers: {_mask_sensitive(dict(self.headers))}")
            if self.path == "/v1/models":
                models = [{"id": name, "object": "model", "created": 1677610602, "owned_by": "anthropic"}
                          for name in MODEL_ALIASES]
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
            else:
                self._respond_json({"detail": "Not found"}, 404)
        finally:
            _log_ctx.session_id = None

    def do_POST(self):
        raw_sid = self.headers.get("X-Claude-Code-Session-Id", "")[:8]
        _log_ctx.session_id = raw_sid or f"req_{os.urandom(4).hex()}"
        if PROXY_METRICS_ENABLED:
            _metrics_ctx.mc = {
                "ts": datetime.now().isoformat(),
                "session_id": getattr(_log_ctx, 'session_id', None) or "",
                "pipeline": {},
            }
        try:
            content_len = int(self.headers.get("Content-Length", 0))

            # P0: 请求体大小硬上限 — 在读取 body 前检查，防止超大请求穿透到后端
            if content_len > PROXY_MAX_REQUEST_BYTES:
                log(f"  -> Request body too large: {content_len} bytes > {PROXY_MAX_REQUEST_BYTES} limit, rejecting")
                self._respond_json(
                    {"error": {
                        "type": "payload_too_large",
                        "message": f"Request body ({content_len} bytes) exceeds maximum allowed size ({PROXY_MAX_REQUEST_BYTES} bytes)",
                        "max_bytes": PROXY_MAX_REQUEST_BYTES,
                        "received_bytes": content_len,
                    }},
                    413,
                )
                return

            body = self.rfile.read(content_len).decode("utf-8")
            log(f"POST {self.path}")
            log(f"  Headers: {_mask_sensitive(dict(self.headers))}")
            if _check_dedup(body):
                log(f"  -> Duplicate request detected (body hash match within {PROXY_DEDUP_WINDOW}s), skipping")
                self._respond_json(
                    {"error": {"type": "duplicate_request", "message": "Duplicate request within dedup window"}},
                    429,
                    extra_headers={"Retry-After": str(PROXY_DEDUP_WINDOW)},
                )
                return
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                log(f"  Body (invalid JSON): {body[:500]}")
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

            if self.path == "/v1/messages" or self.path.startswith("/v1/messages?"):
                # Phase 3: memory pressure active rejection
                mem_rejected, used_pct = _should_reject_for_memory()
                if mem_rejected:
                    log(f"  -> Memory pressure rejection: used_pct={used_pct:.1f}% > threshold={PROXY_MEMORY_REJECT_THRESHOLD:.1f}%")
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
                if total_chars > PROXY_OOM_SAFE_CHARS and msgs:
                    log(f"  -> OOM safety pre-truncation triggered: {total_chars:,} chars > {PROXY_OOM_SAFE_CHARS:,} threshold")
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
                    log(f"  -> Error: {e}")
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
                        log(f"  -> Client disconnected (499): {type(e).__name__}")
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
                            log(f"  -> CRITICAL: failed to send error response: {respond_err}")
                    # No raise — let the connection close cleanly
                finally:
                    # Phase 3: dynamic concurrency adjustment after every request
                    try:
                        _adjust_concurrency()
                    except Exception:
                        pass
                    if PROXY_METRICS_ENABLED:
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["snapshot_written"] = _snapshot_written
            elif self.path == "/v1/chat/completions" or self.path.startswith("/v1/chat/completions?"):
                # OpenAI-compatible chat completions endpoint for Open WebUI
                # Forward directly to backend
                log(f"  -> Forwarding to {LLAMA_BASE}/chat/completions (passthrough)")
                try:
                    req = urllib.request.Request(
                        f"{LLAMA_BASE}/chat/completions",
                        data=body.encode("utf-8"),
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLAMA_API_KEY}"},
                        method="POST"
                    )
                    with _llama_lock:
                        resp = urllib.request.urlopen(req, timeout=PROXY_BACKEND_TIMEOUT)
                    resp_body = resp.read().decode("utf-8")
                    self.send_response(resp.status)
                    resp_body_bytes = resp_body.encode("utf-8")
                    for header, value in resp.getheaders():
                        hl = header.lower()
                        if hl not in ("transfer-encoding", "connection", "keep-alive", "content-length"):
                            self.send_header(header, value)
                    self.send_header("Content-Length", str(len(resp_body_bytes)))
                    self.end_headers()
                    self.wfile.write(resp_body_bytes)
                    log(f"  <- Response: {resp.status}")
                except Exception as e:
                    log(f"  -> Error: {e}")
                    status_code, error_type, retryable = _classify_exception(e)
                    hdrs = {"Retry-After": str(PROXY_RETRY_AFTER_SECONDS)} if retryable else None
                    self._respond_json(
                        {"error": {"type": error_type, "message": str(e)[:500], "retryable": retryable}},
                        status_code,
                        extra_headers=hdrs,
                    )
            else:
                log(f"  -> 404 (unknown path)")
                self._respond_json({"detail": "Not found"}, 404)
        finally:
            _log_ctx.session_id = None
            if PROXY_METRICS_ENABLED:
                _metrics_ctx.mc = None

    def _handle_messages(self, body):
        is_stream = body.get("stream", False)
        model = body.get("model", "unknown")
        _req_start_time = datetime.now().isoformat()
        max_tokens_orig = body.get("max_tokens", 4096)

        # REQ_SUMMARY with session_id
        tools_list = None
        raw_tools = body.get("tools", [])
        if raw_tools:
            tools_list = [t.get("name", "") for t in raw_tools if isinstance(t, dict)]
        session_prefix = getattr(_log_ctx, 'session_id', None) or ""
        session_id = session_prefix
        total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in body.get("messages", []))
        log(f"  [REQ_SUMMARY] chars={total_chars} tools={len(tools_list or [])}")
        log_structured("REQ_SUMMARY", chars=total_chars, tools=len(tools_list or []),
                       model=body.get("model", ""), stream=body.get("stream", False))

        if PROXY_METRICS_ENABLED:
            mc = getattr(_metrics_ctx, 'mc', None)
            if mc:
                mc["input_msgs"] = len(body.get("messages", []))
                mc["input_chars"] = total_chars
                mc["input_tools"] = len(tools_list or [])
                mc["tools"] = tools_list or []

        log(f"  -> Handling model={model}, stream={is_stream}")

        # Backend timeout & output token limit logging
        log(f"  -> Backend timeout: {PROXY_BACKEND_TIMEOUT}s, output token limit: {PROXY_OUTPUT_TOKEN_LIMIT_RATIO}x max_tokens, max_tokens override: {PROXY_MAX_TOKENS_OVERRIDE}")

        # Unified lifecycle stage classification: determines compression intensity
        # based on total chars. Guarantees monotonic escalation.
        # Phase 1 改进3: pass session_id so the function can detect multi-round
        # agent continuations and return an aggressive config for large payloads.
        stage_config = _classify_lifecycle_stage(
            body.get("messages", []),
            session_id=getattr(_log_ctx, 'session_id', None),
        )
        log(f"  -> Stage: {stage_config['stage']} (chars={stage_config['total_chars']:,}, "
            f"frozen={stage_config['frozen_head']}, clear_zone={stage_config['clear_zone_pct']}, "
            f"thinking_keep={stage_config['thinking_keep']}, "
            f"truncate_rounds={stage_config['truncate_rounds']}, oom_safety={stage_config['oom_safety']}, "
            f"continuation={stage_config.get('is_continuation', False)}, "
            f"req_count={stage_config.get('request_count', 0)})")
        if PROXY_METRICS_ENABLED:
            _mc_put("lifecycle_stage", stage_config)

        # Phase 3: dynamic max_tokens based on lifecycle stage and memory pressure
        _current_mem = _get_system_memory()
        dynamic_max, dynamic_reason = _compute_dynamic_max_tokens(
            max_tokens_orig, stage_config, mem=_current_mem)
        if dynamic_max != max_tokens_orig:
            body["max_tokens"] = dynamic_max
            log(f"  -> max_tokens dynamic: {max_tokens_orig} -> {dynamic_max} ({dynamic_reason})")
        if PROXY_METRICS_ENABLED:
            mc = getattr(_metrics_ctx, 'mc', None)
            if mc:
                mc["max_tokens_original"] = max_tokens_orig
                mc["max_tokens_dynamic"] = dynamic_max
                mc["used_pct"] = float(_current_mem.get("used_pct", 0))

        # max_tokens override (hard cap from env, takes final precedence)
        if PROXY_MAX_TOKENS_OVERRIDE > 0 and body.get("max_tokens", max_tokens_orig) > PROXY_MAX_TOKENS_OVERRIDE:
            body["max_tokens"] = PROXY_MAX_TOKENS_OVERRIDE
            log(f"  -> max_tokens override: {max_tokens_orig} -> {PROXY_MAX_TOKENS_OVERRIDE}")

        # Error translation: intercept tool_result errors and rewrite to natural language
        raw_messages, error_count = _translate_tool_result_errors(body.get("messages", []))
        total_errors = sum(error_count.values())
        if total_errors > 0:
            log(f"  -> Error translation: {total_errors} tool_result errors rewritten "
                f"(wasted={error_count['wasted']}, file_not_found={error_count['file_not_found']}, "
                f"input_validation={error_count['input_validation']})")
            _mc_put("error_translation", {"count": total_errors, **error_count})

        # Blocker detection MUST run BEFORE tool-result clearing, which overwrites
        # error markers with [cleared: ...] summaries (DEF-108 pipeline ordering fix)
        blocker_info = _detect_blocker_pattern(raw_messages)
        if PROXY_BLOCKER_ENABLED and blocker_info.get("triggered"):
            log(
                f"  -> Blocker detected: {blocker_info['tool_name']} failed "
                f"({blocker_info['error_type']}) {blocker_info['run_length']} times in a row, "
                f"injecting [BLOCKER] message"
            )
            raw_messages.append(_build_blocker_message(
                blocker_info["tool_name"],
                blocker_info["error_type"],
                blocker_info["run_length"],
            ))
        _mc_put("blocker_detect", blocker_info)

        # Phase 1: normalize mid-conversation system messages for Qwen compatibility.
        raw_messages = _normalize_system_messages(raw_messages)

        # Phase 1: Cache Aligner — protect the first N messages from compression
        # and truncation so the prefix token sequence stays stable across requests.
        cache_prefix, cache_dynamic = _apply_cache_aligner(raw_messages)
        if cache_prefix:
            log(f"  -> Cache aligner: protecting first {len(cache_prefix)} messages from compression/truncation")

        # Single-pass content compression (L2 tool clearing + L4 thinking strip)
        # applied only to the dynamic zone; prefix is protected.
        compress_stats = {"clear": {"enabled": False}, "think": {"enabled": False}}
        if cache_dynamic:
            dynamic_stage_config = dict(stage_config)
            dynamic_stage_config["frozen_head"] = 0  # prefix already protected
            cache_dynamic, compress_stats = _compress_content_pass(
                cache_dynamic, tools_list=tools_list, stage_config=dynamic_stage_config,
            )
        raw_messages = cache_prefix + cache_dynamic
        clear_stats = compress_stats.get("clear", {})
        think_stats = compress_stats.get("think", {})
        semantic_compress_stats = compress_stats.get("compress", {"enabled": False})
        cleared_files = clear_stats.get("cleared_files", [])

        # Log and metrics for semantic compression (Phase 2)
        if semantic_compress_stats.get("enabled"):
            log(f"  -> Semantic compression: {semantic_compress_stats['compressed_count']} tool_results compressed, "
                f"{semantic_compress_stats['saved_chars']:,} chars saved "
                f"(ratio={semantic_compress_stats['ratio']:.2%}, strategies={semantic_compress_stats.get('strategies', {})})")
            if PROXY_METRICS_ENABLED:
                _mc_put("semantic_compress", semantic_compress_stats)
        elif PROXY_COMPRESS_ENABLED:
            log(f"  -> Semantic compression: active (threshold={PROXY_COMPRESS_THRESHOLD}, mode={PROXY_COMPRESS_MODE})")

        # Log and metrics for tool clearing
        if PROXY_METRICS_ENABLED:
            _mc_put("tool_clear", {
                "applied": clear_stats.get("cleared", False),
                "cleared": clear_stats.get("cleared_tool_results", 0),
                "kept": clear_stats.get("kept", 0),
                "chars_freed": clear_stats.get("cleared_chars", 0),
                "total_chars_before": clear_stats.get("total_chars_before", 0),
                "cleared_files_count": len(cleared_files),
                "enabled": clear_stats.get("enabled", True),
                "skipped": clear_stats.get("skipped", False),
                "reason": clear_stats.get("reason", ""),
            })
        if clear_stats.get("cleared"):
            log(f"  -> Tool clearing: {clear_stats['cleared_tool_results']} tool_results cleared, "
                f"{clear_stats['cleared_chars']:,} chars freed (kept {clear_stats['kept']})")
        elif not clear_stats.get("enabled"):
            log(f"  -> Tool clearing: disabled ({BACKEND_TYPE} backend)")
        elif clear_stats.get("enabled") and not clear_stats.get("skipped"):
            log(f"  -> Tool clearing: active (threshold={PROXY_CLEAR_THRESHOLD}, keep={PROXY_TOOL_KEEP})")

        # Log and metrics for thinking strip
        if think_stats.get("stripped"):
            log(f"  -> Thinking stripped: {think_stats['stripped_count']} old assistant messages cleaned (kept last {think_stats['kept']})")
            _mc_put("think_strip", {"stripped": think_stats["stripped_count"]})
        elif think_stats.get("enabled") and not think_stats.get("skipped"):
            reason = think_stats.get("reason", "")
            if reason == "stage_skip":
                log(f"  -> Thinking strip: skipped (stage={stage_config['stage']})")
            else:
                log(f"  -> Thinking strip: active (keep_recent={stage_config['thinking_keep']})")

        # DEF-002: Loop detection — tail-based scan (no cross-request double-counting)
        # Scan last N assistant messages for exact (tool, args) and pattern repeats.
        consecutive = {}
        max_run = 0
        pattern_run = 0
        last_pattern = None
        pattern_tool_name = None
        tail_assistant = [m for m in raw_messages if m.get("role") == "assistant"][-15:]
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

        # Text output loop detection: detect repeated similar text in assistant messages
        text_loop_run = 0
        is_text_loop = False
        if PROXY_TEXT_LOOP_ENABLED:
            text_loop_run, is_text_loop = _detect_text_loop(tail_assistant)
            if text_loop_run > 1:
                log(f"  -> Text loop scan: text_run={text_loop_run} (threshold={PROXY_TEXT_LOOP_THRESHOLD}, "
                    f"similarity>={PROXY_TEXT_LOOP_SIMILARITY})")
            # Merge with tool loop: take the higher count
            if text_loop_run > max_run:
                max_run = text_loop_run

        _mc_put("loop_detect", {"max_run": max_run, "text_loop_run": text_loop_run, "is_text_loop": is_text_loop})

        # Session-level loop state: persist level across requests for early intervention
        session_loop = _LOOP_SESSION_STATE.get(session_id, {"level": 0, "triggers": 0})
        if session_loop["level"] >= 2 and max_run < PROXY_LOOP_THRESHOLD:
            log(f"  -> Session had Level {session_loop['level']}, injecting persistent warning (max_run={max_run})")
            raw_messages.append({
                "role": "user",
                "content": [{"type": "text", "text":
                    f"[System: You were previously looping and had tools restricted. "
                    f"Continue with a DIFFERENT approach. Do NOT repeat previous actions.]"
                }]
            })

        new_messages, new_tools, loop_level, loop_tool_name = _apply_loop_intervention(
            raw_messages, raw_tools, max_run, consecutive,
            pattern_tool_name=pattern_tool_name,
            is_text_loop=is_text_loop,
            text_loop_run=text_loop_run,
        )
        if loop_level >= 1:
            if loop_level >= 2 and raw_tools is not None and new_tools != raw_tools:
                body["tools"] = new_tools
            raw_messages[:] = new_messages
            if loop_tool_name == "text_loop":
                log(f"  -> TEXT LOOP LEVEL {loop_level}: text_run={text_loop_run} max_run={max_run}")
            else:
                log(f"  -> LOOP LEVEL {loop_level}: tool={loop_tool_name} max_run={max_run} consecutive={{k: v for k, v in consecutive.items() if v >= PROXY_LOOP_THRESHOLD}}")
            if loop_level == 2:
                if loop_tool_name != "text_loop":
                    removed = sorted(set(k.split(":")[0] for k, v in consecutive.items() if v >= PROXY_LOOP_THRESHOLD))
                    log(f"    removed tools: {removed} ({len(new_tools)} remaining)")
            elif loop_level == 3:
                log(f"    ALL tools stripped — force plain text response")
            _mc_put("loop_detect", {"max_run": max_run, "level": loop_level, "tool": loop_tool_name, "text_loop_run": text_loop_run, "is_text_loop": is_text_loop})
            if session_id:
                _LOOP_SESSION_STATE[session_id] = {"level": loop_level, "triggers": session_loop.get("triggers", 0) + 1}
        else:
            _mc_put("loop_detect", {"max_run": max_run, "text_loop_run": text_loop_run, "is_text_loop": is_text_loop})
            if session_id and session_loop["level"] > 0 and max_run < PROXY_LOOP_THRESHOLD:
                _LOOP_SESSION_STATE[session_id] = {"level": 0, "triggers": session_loop.get("triggers", 0)}

        # Re-read detection: check if LAST assistant message has Read targeting cleared files
        # TODO(roadmap-U5): Hard-block re-reads — intercept and reject Read calls to cleared files
        re_read_info = {"count": 0, "cleared_files": len(cleared_files), "rate_pct": 0.0}
        if cleared_files:
            re_read_count = 0
            re_read_targets = set()
            last_assistant = None
            for msg in reversed(raw_messages):
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
                rate = _compute_re_read_rate(len(re_read_targets), len(cleared_files))
                re_read_info = {"count": re_read_count, "cleared_files": len(cleared_files), "re_read_files": len(re_read_targets), "rate_pct": round(rate, 1)}
                log(f"  -> Re-read detected: {re_read_count} Read calls targeting {len(re_read_targets)}/{len(cleared_files)} cleared files "
                    f"(rate={rate:.1f}%)")
                # P0-FIX: Hard-block re-reads by injecting a rejection message
                blocked_files = ", ".join(sorted(re_read_targets))
                raw_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text":
                        f"[System: HARD BLOCK — Read calls to the following files were intercepted "
                        f"because their contents were previously cleared and have not changed: {blocked_files}. "
                        f"DO NOT attempt to read these files again. Use your existing knowledge or "
                        f"proceed without re-reading. If you need file content, ask the user explicitly.]"
                    }]
                })
                log(f"  -> Re-read HARD BLOCK injected for: {blocked_files}")
        _mc_put("re_read", re_read_info)

        # Normalize system-reminder date to stabilize prefix for KV cache hits
        if raw_messages and raw_messages[0].get("role") == "user":
            content = raw_messages[0].get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        new_text = re.sub(r"Today's date is \d{4}/\d{2}/\d{2}\.", "Today's date is DATE_PLACEHOLDER.", text)
                        if new_text != text:
                            block["text"] = new_text
                            log(f"  -> Standardized date in msg0 block")
            else:
                new_content = re.sub(r"Today's date is \d{4}/\d{2}/\d{2}\.", "Today's date is DATE_PLACEHOLDER.", str(content))
                if new_content != content:
                    raw_messages[0]["content"] = new_content
                    log(f"  -> Standardized date in msg0")

        # Context truncation (stage-driven)
        raw_messages, trunc_stats = truncate_messages_if_needed(
            raw_messages,
            session_id=getattr(_log_ctx, 'session_id', None),
            keep_rounds=stage_config["truncate_rounds"],
        )
        if trunc_stats.get("truncated"):
            strategy = trunc_stats.get("strategy", "char")
            trunc_metrics = {
                "applied": True,
                "triggered": True,
                "strategy": strategy,
                "dropped": trunc_stats.get("dropped_messages", 0),
                "kept": trunc_stats.get("kept_messages", 0),
            }
            if strategy == "rounds":
                chars_after = trunc_stats.get("chars", trunc_stats.get("estimated_tokens", "?"))
                actual_r = trunc_stats.get("actual_keep_rounds", "?")
                comp = trunc_stats.get("compression", "folded")
                adaptive = trunc_stats.get("adaptive_rounds", "")
                stage_r = trunc_stats.get("stage_keep_rounds", "")
                budget_iter = trunc_stats.get("budget_iterations", 0)
                extra_info = f", adaptive={adaptive}" if adaptive else ""
                extra_info += f", stage_rounds={stage_r}" if stage_r else ""
                extra_info += f", budget_iter={budget_iter}" if budget_iter else ""
                log(f"  -> Context truncation (rounds): {trunc_stats['dropped_messages']} messages dropped, {trunc_stats.get('kept_messages', '?')} kept (rounds={actual_r}, ~{chars_after} chars, budget={PROXY_CHARS_EXPANSION:,}, compress={comp}{extra_info})")
                trunc_metrics["compression"] = comp
                trunc_metrics["chars_after"] = trunc_stats.get("chars", 0)
                trunc_metrics["budget_chars"] = PROXY_CHARS_EXPANSION
                trunc_metrics["rounds"] = actual_r
                trunc_metrics["adaptive_rounds"] = adaptive
                trunc_metrics["budget_iterations"] = budget_iter
            elif strategy == "fifo":
                log(f"  -> Context truncation (fifo): {trunc_stats['dropped_messages']} messages dropped, {trunc_stats.get('kept_messages', '?')} kept (limit={PROXY_CTX_KEEP_MESSAGES})")
            elif strategy == "smart":
                smart_compressed = trunc_stats.get("compressed_assistants", 0)
                smart_kept_chars = trunc_stats.get("kept_chars", 0)
                smart_budget = trunc_stats.get("budget_chars", PROXY_CHARS_EXPANSION)
                log(f"  -> Context truncation (smart): {trunc_stats['dropped_messages']} messages dropped, {trunc_stats.get('kept_messages', '?')} kept, {smart_compressed} assistant reasoning compressed ({smart_kept_chars:,} chars, budget={smart_budget:,})")
                trunc_metrics["compressed_assistants"] = smart_compressed
                trunc_metrics["chars_after"] = smart_kept_chars
                trunc_metrics["budget_chars"] = smart_budget
            else:
                log(f"  -> Context truncation (char): {trunc_stats['dropped_messages']} messages dropped, {trunc_stats['dropped_chars']:,} chars removed ({trunc_stats['chars_before']:,} -> {trunc_stats['chars_after']:,})")
            _mc_put("truncate", trunc_metrics)
        elif not trunc_stats.get("enabled"):
            log(f"  -> Context truncation: disabled ({BACKEND_TYPE} backend)")
            _mc_put("truncate", {"applied": False, "enabled": False})
        elif trunc_stats.get("enabled") and not trunc_stats.get("truncated") and not trunc_stats.get("skipped"):
            log(f"  -> Context truncation: active (strategy={trunc_stats.get('strategy', '?')})")
            _mc_put("truncate", {"applied": False, "enabled": True, "strategy": trunc_stats.get("strategy", "")})
        elif trunc_stats.get("skipped"):
            _mc_put("truncate", {"applied": False, "enabled": True, "skipped": True})

        # DEF-107: inject context-loss notice when drop ratio is very high
        if trunc_stats.get("truncated"):
            dropped = trunc_stats.get("dropped_messages", 0)
            kept = trunc_stats.get("kept_messages", 0)
            if kept + dropped > 0 and dropped / (kept + dropped) > 0.85:
                notice = ("[System: Context severely truncated — "
                          f"{dropped} of {dropped + kept} messages dropped. "
                          "Consider using /compact or starting a new session "
                          "to maintain context quality.]")
                raw_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": notice}],
                })
                log(f"  -> High drop ratio notice injected ({dropped}/{dropped+kept} = "
                    f"{dropped/(kept+dropped)*100:.0f}%)")

        # Debug: compute hashes of first two messages
        if raw_messages:
            def _msg_hash(m):
                c = m.get("content", "")
                if isinstance(c, list):
                    c = "".join(b.get("text", "") for b in c if b.get("type") == "text")
                elif not isinstance(c, str):
                    c = str(c)
                return hashlib.md5((m.get("role", "") + ":" + c).encode()).hexdigest()[:8]
            h0 = _msg_hash(raw_messages[0]) if len(raw_messages) > 0 else "none"
            h1 = _msg_hash(raw_messages[1]) if len(raw_messages) > 1 else "none"
            log(f"  -> Msg hashes: msg0={h0}, msg1={h1}, total_msgs={len(raw_messages)}")

        # DEF-005: OOM safety — stage-gated iterative FIFO truncation to prevent Metal OOM.
        # Only enabled at OOM_DANGER and PRE_TRUNC stages.
        if stage_config["oom_safety"] and not IS_CLOUD and PROXY_CTX_TRUNCATE_STRATEGY != "rounds":
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
            _iteration = 0
            while True:
                est_chars = _estimate_message_chars(raw_messages) + static_chars
                est_tokens = _estimate_tokens_dynamic(raw_messages) + int(static_chars / max(PROXY_CTX_TOKEN_RATIO, 0.1))
                if (est_chars <= PROXY_CHARS_OOM_DANGER and est_tokens <= PROXY_OOM_SAFE_TOKENS) or len(raw_messages) <= 4:
                    break
                _iteration += 1
                keep = max(PROXY_CTX_KEEP_HEAD + PROXY_CTX_KEEP_TAIL, 4)
                if len(raw_messages) > keep:
                    dropped = len(raw_messages) - keep
                    raw_messages[:] = raw_messages[:PROXY_CTX_KEEP_HEAD] + raw_messages[-(keep - PROXY_CTX_KEEP_HEAD):]
                    log(f"  -> OOM safety (iter {_iteration}): est_chars={est_chars}, est_tokens={est_tokens}, "
                        f"dropped {dropped} msgs, kept {len(raw_messages)}")
                else:
                    break
            if _iteration > 0:
                _mc_put("oom_safety", {"triggered": True, "chars": est_chars,
                                       "est_tokens": est_tokens,
                                       "limit_chars": PROXY_CHARS_OOM_DANGER,
                                       "limit_tokens": PROXY_OOM_SAFE_TOKENS,
                                       "iterations": _iteration, "final_msgs": len(raw_messages)})

        # Phase 1: compute common prefix ratio against previous request in session.
        # This quantifies prefix KV cache stability. High ratio = stable prefix = better cache hits.
        previous_messages = _SESSION_LAST_MESSAGES.get(session_id) if session_id else None
        common_prefix_ratio = _compute_common_prefix_ratio(raw_messages, previous_messages or [])
        if PROXY_METRICS_ENABLED:
            _mc_put("common_prefix_ratio", {
                "ratio": common_prefix_ratio,
                "current_msgs": len(raw_messages),
                "previous_msgs": len(previous_messages) if previous_messages else 0,
            })
        log(f"  -> Common prefix ratio: {common_prefix_ratio:.2%} (current={len(raw_messages)} msgs, previous={len(previous_messages) if previous_messages else 0} msgs)")
        if session_id:
            with _state_lock:
                # Bound memory for the session message cache.
                if len(_SESSION_LAST_MESSAGES) > 1000:
                    _SESSION_LAST_MESSAGES.pop(next(iter(_SESSION_LAST_MESSAGES)), None)
                _SESSION_LAST_MESSAGES[session_id] = [dict(m) for m in raw_messages]

        # Final safety net: repair any orphaned tool_use/tool_result blocks
        # introduced by truncation, loop intervention, or compression.
        # This MUST run after all pipeline modifications, right before format conversion.
        raw_messages = _fix_tool_pairings(raw_messages)

        # Convert messages
        messages = convert_anthropic_messages_to_openai(raw_messages)

        # Handle system prompt
        system_msg = body.get("system")
        if system_msg:
            if isinstance(system_msg, list):
                system_text = "\n".join([b.get("text", "") for b in system_msg if b.get("type") == "text"])
            else:
                system_text = str(system_msg)
            if system_text.strip():
                messages = [{"role": "system", "content": system_text}] + messages

        openai_body = {
            "model": MODEL_NAME,
            "messages": messages,
            "max_tokens": body.get("max_tokens", 4096),
            "temperature": body.get("temperature", 0.7),
            "stream": is_stream,
        }
        if "top_p" in body:
            openai_body["top_p"] = body["top_p"]
        if "stop_sequences" in body:
            openai_body["stop"] = body["stop_sequences"]

        # DeepSeek flash model: disable thinking mode for direct content response
        if IS_CLOUD and "flash" in MODEL_NAME.lower():
            openai_body["thinking"] = {"type": "disabled"}

        # Handle tools
        raw_tools = body.get("tools")
        if raw_tools and PROXY_TOOL_FILTER_ENABLED:
            tc_raw = body.get("tool_choice")
            tc_name = None
            if isinstance(tc_raw, dict) and tc_raw.get("type") == "tool":
                tc_name = tc_raw.get("name", "")
            raw_tools, tf_stats = _filter_tools(
                raw_tools, raw_messages,
                recent_rounds=PROXY_TOOL_FILTER_RECENT,
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
                    f"scanned={tf_stats.get('scanned_assistant',0)}{recent_info}{filtered_info})")
                _mc_put("tool_filter", {**tf_stats, "applied": True})
            else:
                _mc_put("tool_filter", {**tf_stats, "applied": False})
        tools = convert_anthropic_tools_to_openai(body.get("tools"))
        if tools:
            openai_body["tools"] = tools
            log(f"  -> Tools: {[t['function']['name'] for t in tools]}")

        tool_choice = convert_anthropic_tool_choice_to_openai(body.get("tool_choice"))
        if tool_choice:
            openai_body["tool_choice"] = tool_choice

        log(f"  -> Forwarding to {LLAMA_BASE}/chat/completions")
        try:
            with _llama_lock:
                req = urllib.request.Request(
                    f"{LLAMA_BASE}/chat/completions",
                    data=json.dumps(openai_body).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLAMA_API_KEY}"},
                    method="POST"
                )
                resp = urllib.request.urlopen(req, timeout=PROXY_BACKEND_TIMEOUT)
                log(f"  <- llama-server status: {resp.status}")
                if is_stream:
                    self._handle_streaming_response(resp, body)
                else:
                    self._handle_non_streaming_response(resp, body)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8")
            log(f"  <- llama-server error: {e.code} - {err[:500]}")
            self._respond_json({"error": {"message": err}}, e.code)
            return

    def _handle_non_streaming_response(self, resp, anthropic_body):
        openai_resp = json.loads(resp.read().decode("utf-8"))
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
            log(f"  -> FORCE_STOPPED at {output_chars} chars (limit={output_chars_limit})")
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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        if not getattr(self, "_request_id", None):
            self._request_id = f"req_{os.urandom(8).hex()}"
        self.send_header("request-id", self._request_id)
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
                            log(f"  -> !! Output token limit on tool_call: est={int(est_tokens)}, limit={output_token_hard_limit}, tool={tool_name}, forcing stop")
                            output_force_stopped = True
                            break
                continue

            # Check text output token limit
            if output_char_count * 0.4 > output_token_hard_limit:
                log(f"  -> FORCE_STOPPED: output est={int(output_char_count * 0.4)} tokens, limit={output_token_hard_limit}")
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
        _jsonl_output_map[self._last_jsonl_token] = len(total_text)
        log(f"  <- Streamed text={len(total_text)} chars, tools={len(tool_calls_buffer)}")

    def _respond_json(self, data, status=200, extra_headers=None):
        raw = json.dumps(data, ensure_ascii=False)
        raw_bytes = raw.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if not getattr(self, "_request_id", None):
            self._request_id = f"req_{os.urandom(8).hex()}"
        self.send_header("request-id", self._request_id)
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
            self._respond_json({"schema": "v1", "total": 0})
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
        top_tools = sorted(tool_usage.items(), key=lambda x: -x[1])[:10]
        self._respond_json({
            "schema": "v1",
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
            "last_n": last_n,
        })


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
    ThreadingHTTPServer((host, port), Handler).serve_forever()

if __name__ == "__main__":
    main()
