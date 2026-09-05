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


def _warn_diag(site, exc):
    """诊断层异常落 WARN(前 N 次每挂点)——fail-open 但故障不可静默(评审 P2)。"""
    try:
        import diagnostics
        diagnostics.warn_suppressed(site, exc)
    except Exception:
        pass


import loop_detection
from loop_detection import *

import queue_manager

import model_registry

# ---------------------------------------------------------------------------
# Pipeline abstraction: refactored _handle_messages processing stages.
# ---------------------------------------------------------------------------
from pipeline import (
    PipelineContext,
    InstrumentedPipeline,
    RequestParser,
    ContextEngineStage,
    _ctx_engine_on,
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
    AutoRecallStage,
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


def _timed_stream_lines(resp):
    """Wrap a backend SSE stream with an inter-chunk idle watchdog (select 版).

    首 token 后, 若连续 PROXY_STREAM_IDLE_TIMEOUT_S 无后端数据 → 抛
    StreamIdleTimeout(BackendDispatcher 捕获后中止中继并 close() 取消
    在途生成)。prefill/首 token 不受限(计时从首个 yield 之后起)。

    2026-08-29 select 重写(原 settimeout 版): 实测 sock.settimeout 在
    受控环境(socketpair)生效、但在 urlopen 真实后端链上不生效(v3 探针
    铁证: settimeout(3) 后 readline 阻塞 225s 无超时; 生产 194s 静默
    亦未触发 30s 看门狗)——两环境行为分叉, 根因在 urlopen 链某层。select
    为纯 OS 层等待, 与 #51-B1 v4 心跳同原语, 两环境实测可靠。
    sock 不可提取时退化为裸迭代(无看门狗, 与原版降级语义一致)。
    """
    import socket
    import select as _select
    sock = None
    try:
        sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    except Exception:
        sock = None
    first = True
    it = iter(resp)
    while True:
        if not first and sock is not None:
            try:
                r, _, _ = _select.select([sock], [], [], PROXY_STREAM_IDLE_TIMEOUT_S)
            except (OSError, ValueError, TypeError):
                r = True  # select 不可用(fake sock 无 fileno 等)则裸迭代
            if not r:
                # 已等满 idle 时限且 fd 无数据。极小概率误杀: fp 缓冲尚有
                # 未消费行而 fd 静默(SSE 场景消费方逐行高速消费, 窗口极小,
                # 接受此权衡——原 settimeout 版无此窗口但真实链不生效)。
                raise StreamIdleTimeout(
                    f"backend stream idle > {PROXY_STREAM_IDLE_TIMEOUT_S}s after first token")
        try:
            bline = next(it)
        except StopIteration:
            return
        except socket.timeout:
            # 兜底: sock 不可提取走裸迭代时, settimeout 若在该环境生效,
            # timeout 归类为 StreamIdleTimeout(向后兼容原语义)
            raise StreamIdleTimeout(
                f"backend stream idle > {PROXY_STREAM_IDLE_TIMEOUT_S}s after first token")
        first = False
        yield bline



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
                        # R10: capability metadata from the model catalog —
                        # agent_go's registry syncs from this instead of manual entry.
                        meta = {"route": meta_route}
                        real = pref.get("cloud_model")
                        if real:
                            meta["real_model"] = real
                            entry = model_registry.get_model(real)
                            if entry:
                                caps = entry.get("capabilities") or {}
                                thinking = caps.get("thinking")
                                meta["thinking_supported"] = (
                                    thinking in ("supported", "required", "only")
                                    if thinking is not None else None
                                )
                                meta["thinking_required"] = (
                                    thinking in ("required", "only")
                                    if thinking is not None else None
                                )
                                meta["json_compliance"] = caps.get("json")
                                meta["context_chars"] = caps.get("context_tokens")
                                meta["price"] = entry.get("price")
                                prov = model_registry.get_provider(entry.get("provider", "")) or {}
                                meta["direct_capable"] = bool(prov.get("anthropic_compatible", False))
                            if pref.get("fallback_models"):
                                meta["fallback_models"] = pref["fallback_models"]
                        models.append({
                            "id": name,
                            "object": "model",
                            "created": 1677610602,
                            "owned_by": "proxy-router",
                            "metadata": meta,
                        })
                    # 本地引擎直列: 目录中 provider=local/local-9b 的模型
                    # (ornith-9b → 9B 引擎 :8084, local-default → 35B :8081),
                    # 供客户端直选/探测双引擎
                    for _lm_name in model_registry.list_models():
                        _lm = model_registry.get_model(_lm_name) or {}
                        _lm_prov = _lm.get("provider", "")
                        if _lm_prov not in ("local", "local-9b"):
                            continue
                        _lcaps = _lm.get("capabilities") or {}
                        _creds = model_registry.get_provider_credentials(_lm_prov, env_lookup=_ps._env_lookup) or {}
                        models.append({
                            "id": _lm_name,
                            "object": "model",
                            "created": 1677610602,
                            "owned_by": "proxy-router",
                            "metadata": {
                                "route": "local",
                                "provider": _lm_prov,
                                "model_name": _lm.get("model_name"),
                                "real_model": _lm_name,
                                "thinking_supported": (_lcaps.get("thinking") in ("supported", "required", "only")
                                                       if _lcaps.get("thinking") is not None else None),
                                "thinking_required": (_lcaps.get("thinking") in ("required", "only")
                                                      if _lcaps.get("thinking") is not None else None),
                                "json_compliance": _lcaps.get("json"),
                                "context_chars": _lcaps.get("context_tokens"),
                                "price": _lm.get("price"),
                                "direct_capable": False,
                                "base_url": _creds.get("base_url") or "",
                            },
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
                elif self.path == "/api/status":
                    status = _build_status_json()
                    http_code = 200 if status["state"] in ("healthy", "starting") else 503
                    self._respond_json(status, http_code)
                elif self.path == "/api/watchdog":
                    self._respond_json(_build_watchdog_json())
                elif self.path == "/api/queue":
                    self._respond_json(_build_queue_json())
                elif self.path == "/api/profiles":
                    self._respond_json({"profiles": _build_profiles_json()})
                elif self.path == "/api/route/policies":
                    # R9: sanitized routing policies + catalog (no key material)
                    self._respond_json(_build_route_policies_json())
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
                # ---- R13-R16 诊断数据面端点（见 diagnostics-dataplane-design）----
                elif self.path == "/api/sessions":
                    # R14: 会话发现（key 契约见设计 D5）
                    import session_ledger
                    self._respond_json(session_ledger.LEDGER.list_sessions())
                elif self.path.startswith("/api/session/"):
                    if not self._handle_diag_session_endpoint():
                        self._respond_json(
                            {"error": {"type": "not_found", "message": "unknown session endpoint"}}, 404)
                elif self.path == "/api/backend/props":
                    self._handle_backend_props_endpoint("props")
                elif self.path == "/api/backend/slots":
                    self._handle_backend_props_endpoint("slots")
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
            self._post_body = body  # admin 端点复用, 避免二次读取 rfile 为空
            log(f"POST {self.path}")
            log(f"  Headers: {_mask_sensitive(dict(self.headers))}")
            # Admin: HTTP hot-reload (R12) — checked before JSON parsing so an
            # empty-body POST is valid. Idempotent, serialized by _RELOAD_LOCK.
            if self.path == "/admin/reload":
                self._handle_admin_reload()
                return
            # R18: 任务上下文证据包（契约冻结版; 语义封装面, 机制面走 admin/debug）
            if self.path == "/api/task-context":
                self._handle_task_context()
                return
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
                # per-route 上下文管理豁免的 per-request 头（§10.1，TC25/27）
                parsed["_x_proxy_context_managed_by"] = self.headers.get("X-Proxy-Context-Managed-By", "")
                # 客户端超时（stainless SDK 的 X-Stainless-Timeout）——用于非流式
                # 主动 504 与流式空闲看门狗的上限推导；缺省则退回后端超时。
                parsed["_x_client_timeout_s"] = self.headers.get("X-Stainless-Timeout", "")
                # R19: X-Proxy-Pin-Context —— pinned 锚点列表(stage 14 跳过,
                # stage 17 不豁免; 预算/降级语义见集成契约 §3.3 冻结版)
                parsed["_x_proxy_pin_context"] = self.headers.get("X-Proxy-Pin-Context", "")
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
               self.path == "/messages" or self.path.startswith("/messages?") or \
               self.path == "/v1/chat/completions" or self.path.startswith("/v1/chat/completions?"):
                # /messages 别名: ai-sdk(如 opencode @ai-sdk/anthropic 4.x)对
                # baseURL=http://localhost:4000 构造 POST /messages(无 /v1),
                # 与 /v1/messages 同语义(Anthropic 协议)。
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

                # 请求优先级队列（Phase 1，PROXY_QUEUE_ENABLED=false 时完全跳过）：
                # 1) 准入控制：huge bucket 强制路由云端或 413 拒绝
                # 2) 非 huge：入队并在 _handle_messages 前等待 worker 名额
                _queue_ticket = None       # 非 None 表示需要在 finally 中 release
                _queue_bucket = ""
                if PROXY_QUEUE_ENABLED:
                    _qm = _ps.get_queue_manager()
                    _queue_bucket = queue_manager.classify_bucket(
                        total_chars,
                        _ps.PROXY_QUEUE_LARGE_THRESHOLD_CHARS,
                        _ps.PROXY_QUEUE_HUGE_THRESHOLD_CHARS,
                    )
                    if _queue_bucket == "huge":
                        # huge 不入本地队列。准入决策由 decide_huge_action 决定：
                        # - local（显式 X-Proxy-Route-To: local 且未超本地上限）→ 放行本地，
                        #   由管线 OOM 保护 / ContextTruncator 兜底；
                        # - cloud（无 local 头 + 路由开启）→ 强制云端；
                        # - reject → 413 拒绝（本地确实无法承载 / 路由关闭）。
                        _client_route = parsed.get("_x_proxy_route_to", "")
                        # 模型级强制本地(如 haiku behavior=force+prefer_local, 数据保密)
                        # → 巨请求不得路由云端
                        _pref = _ps.MODEL_ROUTE_PREFERENCES.get(parsed.get("model", ""), {})
                        _force_local = bool(
                            _pref.get("behavior") == "force"
                            and _pref.get("route_bias") == "prefer_local")
                        _huge_action = queue_manager.decide_huge_action(
                            total_chars,
                            _client_route,
                            _ps.PROXY_QUEUE_HUGE_ACTION,
                            PROXY_ROUTE_ENABLED,
                            _ps.PROXY_CTX_CHARS_LIMIT,
                            force_local=_force_local,
                        )
                        if _huge_action["action"] == "local":
                            # 放行本地：保持 local 标记，SmartRouter / _early_route_decision 均走本地；
                            # 不入队（huge 语义），由 _llama_lock 串行化。
                            self._queue_response_headers = {"X-Queue-Bucket": "huge"}
                            log(f"  -> [queue] huge bucket ({total_chars:,} chars): explicit "
                                f"X-Proxy-Route-To: local, forwarding locally "
                                f"(ctx_limit={_ps.PROXY_CTX_CHARS_LIMIT:,})", level="WARN")
                        elif _huge_action["action"] == "cloud":
                            # 复用 X-Proxy-Route-To 内部标记，SmartRouter 会强制走云端；
                            # _early_route_decision 也会因此跳过 OOM 预截断。
                            parsed["_x_proxy_route_to"] = "cloud"
                            self._queue_response_headers = {"X-Queue-Bucket": "huge"}
                            log(f"  -> [queue] huge bucket ({total_chars:,} chars >= "
                                f"{_ps.PROXY_QUEUE_HUGE_THRESHOLD_CHARS:,}): force route to cloud", level="WARN")
                        else:
                            log(f"  -> [queue] huge bucket rejected ({total_chars:,} chars, "
                                f"action={PROXY_QUEUE_HUGE_ACTION}, route_enabled={PROXY_ROUTE_ENABLED})", level="WARN")
                            if PROXY_METRICS_ENABLED:
                                mc = getattr(_metrics_ctx, 'mc', None)
                                if mc:
                                    mc["queue_bucket"] = "huge"
                                    mc["queue_rejected"] = "huge_context_not_supported_locally"
                                    _finalize_metrics(mc)
                                    log_metrics(mc)
                            self._respond_json(
                                {
                                    "error": {
                                        "type": "huge_context_not_supported_locally",
                                        "message": (f"Prompt of {total_chars:,} chars exceeds the local queue "
                                                    f"huge threshold ({_ps.PROXY_QUEUE_HUGE_THRESHOLD_CHARS:,}). "
                                                    "Enable routing (PROXY_ROUTE_ENABLED) to auto-route such requests to cloud."),
                                        "chars": total_chars,
                                        "threshold": _ps.PROXY_QUEUE_HUGE_THRESHOLD_CHARS,
                                        "retryable": False,
                                    }
                                },
                                413,
                            )
                            return
                    else:
                        _queue_ticket = _qm.enqueue({
                            "request_id": getattr(self, "_request_id", "") or "",
                            "total_chars": total_chars,
                            "stream": bool(parsed.get("stream")),
                            "bucket": _queue_bucket,
                        })
                        self._queue_response_headers = {
                            "X-Queue-Bucket": _queue_bucket,
                            "X-Queue-Position": str(_qm.position(_queue_ticket)),
                            "X-Queue-Estimated-Wait-Ms": str(_qm.estimated_wait_ms(_queue_bucket)),
                        }
                        log(f"  -> [queue] enqueued bucket={_queue_bucket} "
                            f"pos={self._queue_response_headers['X-Queue-Position']} "
                            f"est_wait={self._queue_response_headers['X-Queue-Estimated-Wait-Ms']}ms")
                        if PROXY_METRICS_ENABLED:
                            mc = getattr(_metrics_ctx, 'mc', None)
                            if mc:
                                mc["queue_bucket"] = _queue_bucket

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
                elif _ctx_engine_on() and total_chars > PROXY_OOM_SAFE_CHARS and msgs:
                    # §12.4「优先级最高」: 引擎开启时退役预截断——按轮砍重排历史与
                    # append-only 布局冲突(缓存全量击穿 + 模型静默失忆, 42355d18
                    # 死循环成因)。引擎的 canonical 视图已被写入期压缩兜底, 超限
                    # 走 §4.9 回退保护(ContextOverflowError → 413 拒绝)而非静默砍轮。
                    log(f"  -> OOM safety pre-truncation skipped: ctx engine on — "
                        f"overflow handled by epoch fallback 413 ({total_chars:,} chars)",
                        level="WARN")
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
                import trace_context
                _trace = trace_context.begin(
                    _req_id,
                    traceparent=self.headers.get("traceparent"),
                    trace_id=self.headers.get("X-Proxy-Trace-Id"),
                )
                self._last_jsonl_token = _next_jsonl_token()
                _jsonl_output_map[self._last_jsonl_token] = 0
                # R13-R16: 本请求诊断累积态初始化(key_source 记录会话 key 来源,
                # 供 /api/sessions 暴露无头回退 key 的合并风险,设计 D5)
                if PROXY_DIAG_ENABLED:
                    try:
                        import diagnostics
                        diagnostics.begin_request(
                            _req_id, raw_sid[:8],
                            "header" if self.headers.get("X-Claude-Code-Session-Id") else "fallback")
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["request_id"] = _req_id
                            mc["trace_id"] = _trace["trace_id"]
                            mc["root_span_id"] = _trace["root_span_id"]
                    except Exception as _e:
                        _warn_diag("begin_request", _e)
                # Phase 3: request failure snapshot — save original body before processing
                _write_request_snapshot(_req_id, parsed)
                _snapshot_written = False
                # 请求队列：等待 worker 名额（超时返回 False 并已自动出队）
                _queue_acquired = False
                if _queue_ticket is not None:
                    _queue_acquired = _qm.acquire(_queue_ticket, timeout=PROXY_QUEUE_TIMEOUT_SECONDS)
                    _queue_wait_ms = _queue_ticket.elapsed_wait_ms()
                    if not _queue_acquired:
                        log(f"  -> [queue] timeout after {_queue_wait_ms}ms "
                            f"(bucket={_queue_bucket}, limit={PROXY_QUEUE_TIMEOUT_SECONDS}s)", level="WARN")
                        if PROXY_METRICS_ENABLED:
                            mc = getattr(_metrics_ctx, 'mc', None)
                            if mc:
                                mc["queue_wait_ms"] = _queue_wait_ms
                                mc["queue_rejected"] = "queue_timeout"
                                _finalize_metrics(mc)
                                log_metrics(mc)
                        self._respond_json(
                            {
                                "error": {
                                    "type": "queue_timeout",
                                    "message": (f"Request waited {_queue_wait_ms}ms in queue "
                                                f"(bucket={_queue_bucket}), exceeding the "
                                                f"{PROXY_QUEUE_TIMEOUT_SECONDS}s limit. Retry later."),
                                    "request_id": _req_id,
                                    "retryable": True,
                                }
                            },
                            503,
                            extra_headers={"Retry-After": str(PROXY_RETRY_AFTER_SECONDS)},
                        )
                        return
                    if self._queue_response_headers is not None:
                        self._queue_response_headers["X-Queue-Wait-Ms"] = str(_queue_wait_ms)
                    if PROXY_METRICS_ENABLED:
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["queue_wait_ms"] = _queue_wait_ms
                    log(f"  -> [queue] acquired worker after {_queue_wait_ms}ms (bucket={_queue_bucket})")
                try:
                    self._handle_messages(parsed)
                    _dur = (_time.monotonic() - _t0) * 1000
                    _out_chars = _jsonl_output_map.pop(self._last_jsonl_token, 0)
                    # TS-4: 客户端中途断连 (BrokenPipe 已在 backend_dispatcher 捕获)
                    # 按 499 (client closed request) 记账,不计入 5xx 错误率。
                    _status = 499 if getattr(self, "_client_disconnected", False) else 200
                    log_request(
                        model=parsed.get("model", "unknown"),
                        input_chars=total_chars,
                        output_chars=_out_chars,
                        status=_status,
                        duration_ms=_dur,
                        start_time=_req_start_time,
                        session_id=raw_sid,
                        request_id=_req_id,
                        trace_id=_trace["trace_id"],
                    )
                    _record_request_for_concurrency(_dur, _status)
                    if PROXY_METRICS_ENABLED:
                        mc = getattr(_metrics_ctx, 'mc', None)
                        if mc:
                            mc["output_chars"] = _out_chars
                            mc["duration_ms"] = round(_dur, 1)
                            mc["status"] = _status
                            if not mc.get("request_id"):
                                mc["request_id"] = _req_id
                            _finalize_metrics(mc)
                            log_metrics(mc)
                    # R16: per-turn 深度记录落盘(sessions.jsonl,经 request_id 与
                    # proxy_metrics.jsonl 关联;canonical_mismatch → lifecycle 事件)
                    if PROXY_DIAG_ENABLED:
                        try:
                            import diagnostics
                            diagnostics.finalize_request(getattr(_metrics_ctx, 'mc', None) or {})
                        except Exception as _e:
                            _warn_diag("finalize_request", _e)
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
                        session_id=raw_sid,
                        request_id=_req_id,
                        trace_id=_trace["trace_id"],
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
                    # 请求队列：释放 worker 名额并记录占用耗时（供等待时间估计）
                    if _queue_acquired:
                        try:
                            _qm.release(_queue_ticket,
                                        occupied_ms=(_time.monotonic() - _t0) * 1000)
                        except Exception:
                            pass
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
            try:
                import trace_context
                trace_context.clear()
            except Exception:
                pass
            if PROXY_METRICS_ENABLED:
                _metrics_ctx.mc = None

    def _handle_messages(self, body):
        """Pipeline-based message processing — 22 stages."""
        import trace_context
        # Emergency rollback: set PROXY_PIPELINE_DISABLED=1 to use the old path
        # (requires reverting to a prior commit that still has the legacy code).
        ctx = PipelineContext(
            body=body,
            request_id=getattr(self, '_request_id', ''),
            trace_id=(trace_context.current() or {}).get("trace_id", ""),
            root_span_id=(trace_context.current() or {}).get("root_span_id", ""),
            client_type=_detect_client_type(self.headers.get('User-Agent', '')),
        )
        InstrumentedPipeline([
            RequestParser(),              # 0
            ContextEngineStage(),         # 0.5 — 上下文工程引擎(默认关;开启时 7/14/17 跳过)
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
            AutoRecallStage(),            # 12.5 — ctx_recall 自闭环(默认关;台账 dup 检测→manifest 确认→召回尾部注入)
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

            _choices = openai_resp.get("choices") or []
            choice = _choices[0] if _choices else {}
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
                    _choices = openai_resp.get("choices") or []
                    _msg0 = _choices[0].get("message", {}) if _choices else {}
                    for tc in (_msg0.get("tool_calls") or []):
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

        # IFC-3 方案B: 微轮续流——重派后的递归调用复用已发出的 SSE 头与
        # message_start, 客户端整轮只见一条消息。标志在入口消费(置回 False),
        # 嵌套微轮会在重派前重新置 True。
        _micro_continue = getattr(self, '_micro_continuation', False)
        if _micro_continue:
            self._micro_continuation = False

        # #51-B1: 头段(v2 心跳挂 _heartbeat_lines 首行等待, 与此处解耦)
        if not _micro_continue:
            self._send_sse_stream_headers()

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
                # 2026-08-27 修复: 原为 `pass`——吞掉后读循环继续消费后端流,
                # 客户端已消失仍让 rapid-mlx 烧完整段生成(MAX_CONCURRENT=1 下
                # 把唯一 sequence 占满, 后续请求排队放大成分钟级积压; 08-27
                # probe 排队 25 分钟事故根因)。改为向上抛给 backend_dispatcher
                # 的断连处理: 立即停止中继并 close() 到后端的连接取消在途生成。
                raise

        # Send message_start (usage will be updated from llama-server timings)
        if not _micro_continue:
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
        for line in self._heartbeat_lines(resp):
            line = line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            if output_force_stopped:
                break

            if _first_token_time is None:
                _first_token_time = time.monotonic()

            # include_usage 尾块 choices 为空列表(rapid-mlx 实测)——.get 默认值
            # 只在 key 缺席时生效, 空列表须显式兜底, 否则 [][0] IndexError
            # (2026-08-21 #39 门禁实测: 每个流式请求在生成末必崩, claude CLI
            # 全部降级非流式重试, 每轮双倍请求)。
            choices = chunk.get("choices") or []
            choice = choices[0] if choices else {}
            delta = choice.get("delta", {})

            # Track finish_reason from the stream
            if choice.get("finish_reason"):
                stream_finish_reason = choice["finish_reason"]

            # Extract usage from llama-server timings or OpenAI/DeepSeek usage field
            timings = chunk.get("timings")
            if timings:
                input_tokens = timings.get("prompt_n", input_tokens)
                output_tokens = timings.get("predicted_n", output_tokens)
                # R13/R16: timings 扩展采集(prompt_ms/predicted_ms + 能力探测)
                if PROXY_DIAG_ENABLED:
                    try:
                        import diagnostics
                        if diagnostics.probe_timings(timings):
                            diagnostics.set_prompt_tokens(
                                processed_n=timings.get("prompt_n"),
                                prompt_eval_ms=timings.get("prompt_ms"),
                                gen_ms=timings.get("predicted_ms"),
                                generation_n=timings.get("predicted_n"))
                    except Exception as _e:
                        _warn_diag("stream_timings", _e)
            usage = chunk.get("usage")
            if usage:
                input_tokens = usage.get("prompt_tokens", input_tokens)
                output_tokens = usage.get("completion_tokens", output_tokens)
                # 验收门禁 1: cached_tokens 回填引擎会话(流式路径; 需要
                # FormatConverter 的 stream_options.include_usage 使后端发本块)
                if _ctx_engine_on():
                    _sid = getattr(_log_ctx, 'session_id', None)
                    if _sid:
                        try:
                            import context_engine
                            _details = usage.get("prompt_tokens_details") or {}
                            _hit = context_engine.ENGINE.get_or_create(_sid).record_usage(
                                usage.get("prompt_tokens", 0),
                                _details.get("cached_tokens"))
                            if _hit is not None:
                                log(f"  -> [context_engine] usage: "
                                    f"prompt={usage.get('prompt_tokens')} "
                                    f"cached={_details.get('cached_tokens')} "
                                    f"hit={_hit:.1%}")
                        except Exception as _e:
                            log(f"  -> [context_engine] usage record failed: {_e}",
                                level="WARN")
                # R13/R16: sent/usage 是 hit_ratio 分母(1 − prompt_n/prompt_tokens)
                if PROXY_DIAG_ENABLED:
                    try:
                        import diagnostics
                        diagnostics.set_prompt_tokens(
                            sent_n=usage.get("prompt_tokens"),
                            generation_n=usage.get("completion_tokens"))
                    except Exception as _e:
                        _warn_diag("stream_usage", _e)

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

        # IFC-3 方案B(2026-08-30): 微轮重派——本响应尚未向客户端发出任何内容
        # 块(无文本、无内联工具)且工具调用全部为 ctx_recall 时, 代理在同一请求
        # 内自答并重新分发(append-only 尾部追加, prefix cache 增量友好), 客户端
        # 全透明。开关关闭/预算耗尽/构造失败 → 正常发射回落路径 A(次请求改写)。
        # 注: thinking 模型的 reasoning_content 会作为文本增量先发 →
        # text_block_started=True → 自动回落路径 A(生产配置 thinking off 不受影响)。
        if (not text_block_started and not content_tools_pending
                and tool_calls_buffer):
            _dispatch = getattr(self, '_micro_recall_dispatch', None)
            if _dispatch is not None:
                _follow = None
                try:
                    import ctx_recall as _cr
                    _tcs = [tool_calls_buffer[i] for i in sorted(tool_calls_buffer)
                            if tool_calls_buffer[i].get("function", {}).get("name")]
                    _follow = _cr.build_follow_up_messages(
                        getattr(_log_ctx, 'session_id', None) or '', _tcs)
                except Exception as _e:
                    _warn_diag("micro_turn_build", _e)
                if _follow is not None:
                    # 标志先置 True 供递归入口消费; finally 兜底清除防重派异常
                    # 时泄漏到同连接的下一请求。
                    self._micro_continuation = True
                    try:
                        if _dispatch(_follow):
                            return
                    finally:
                        self._micro_continuation = False

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

            # R13 流式通道: SSE 注释行尾注——message_stop 之前(设计 D1)。
            # 注释行(: 前缀)被所有 SSE 解析器忽略(claude CLI/Anthropic SDK/OpenAI SDK)。
            if PROXY_DIAG_ENABLED and PROXY_DIAG_SSE_TAIL:
                try:
                    import diagnostics
                    self.wfile.write(diagnostics.sse_tail_line(
                        diagnostics.build_diag_payload()).encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client disconnected — response pointless
                except Exception as _e:
                    _warn_diag("sse_tail_anthropic", _e)

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
        # REQ_USAGE: 记录流式响应 usage 信息（归因=实际响应引擎模型码，TC04 契约）
        if input_tokens > 0 or output_tokens > 0:
            _attribution = getattr(_log_ctx, "model", "")
            try:
                _attribution = ((_log_ctx.openai_body or {}).get("model")
                                or _attribution)
            except AttributeError:
                pass
            log(f"  [REQ_USAGE] input={input_tokens} output={output_tokens} "
                f"model={_attribution}")


    def _send_sse_stream_headers(self):
        """流式响应头段(200 + SSE + request-id + route/queue/diag 头)。
        B1 抽出: 预发路径(_begin_sse_stream)与常规路径共用, 保证头集合一致。"""
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
        # 请求队列响应头（X-Queue-*），与 route headers 同机制
        queue_headers = getattr(self, '_queue_response_headers', None) or {}
        for hk, hv in queue_headers.items():
            self.send_header(hk, str(hv))
        self._queue_response_headers = None
        # R19 pin 响应头（X-Proxy-Pin-*），与 queue headers 同机制
        pin_headers = getattr(self, '_pin_response_headers', None) or {}
        for hk, hv in pin_headers.items():
            self.send_header(hk, str(hv))
        self._pin_response_headers = None
        # R13 诊断归因头
        self._send_diag_headers()
        self.end_headers()
        self._sse_head_sent = True


    def _heartbeat_lines(self, resp):
        """#51-B1(v4, select 心跳): 大 payload 流式中继期间, 用 select 等待
        后端数据、超时即向客户端发 SSE 心跳注释行——覆盖冷 prefill 的整个
        静默窗口(后端首 chunk 立即到、真 token 要等 prefill 完成, 实测
        300KB→225s), 防 CLI idle 断连(实测 184.6s)。

        迭代教训(2026-08-29, 两项实测):
        · v2 线程版: 主线程阻塞在 resp.readline() 期间心跳线程 216s 不被
          调度(GIL 饥饿), 并发线程方案在该形态下不可用;
        · v3 settimeout 版: sock.settimeout(3) 后 readline 仍阻塞 225s
          ——settimeout 对 urlopen 响应的 BufferedReader 读取链不生效
          (同因疑使 _timed_stream_lines 的 idle 看门狗在 prefill 场景
          从未生效)。select 为纯 OS 层等待, 无上述两层依赖。

        心跳写失败(BrokenPipe)=客户端已断: 关闭 resp 取消后端在途生成
        (联动 2026-08-27 修复#1)。
        idle 看门狗语义保留: 首行之后连续 PROXY_STREAM_IDLE_TIMEOUT_S 无
        后端数据 → StreamIdleTimeout(与心跳并存: 心跳只维持字节流, 不
        影响 stall 判定)。
        dispatcher 按 payload(≥PROXY_SSE_HEARTBEAT_BYTES) 置
        _sse_heartbeat_wanted; 未置/False/sock 不可提取时零行为差异。"""
        wanted = bool(getattr(self, '_sse_heartbeat_wanted', False))
        if not wanted:
            for line in _timed_stream_lines(resp):
                yield line
            return
        sock = None
        try:
            sock = getattr(getattr(getattr(resp, "fp", None), "raw", None),
                           "_sock", None)
        except Exception:
            sock = None
        if sock is None:
            for line in _timed_stream_lines(resp):
                yield line
            return
        import select as _select
        interval = max(0.01, float(
            getattr(_ps, "PROXY_SSE_HEARTBEAT_S", 15)))
        idle_limit = max(interval, float(
            getattr(_ps, "PROXY_STREAM_IDLE_TIMEOUT_S", 30)))
        beats = 0
        got_first = False
        idle_started = None
        while True:
            try:
                r, _, _ = _select.select([sock], [], [], interval)
            except (OSError, ValueError):
                for line in _timed_stream_lines(resp):
                    yield line
                return
            if not r:
                if got_first and idle_started is None:
                    idle_started = time.monotonic()
                elif (got_first and idle_started is not None
                        and time.monotonic() - idle_started >= idle_limit):
                    raise StreamIdleTimeout(
                        f"backend stream idle > {idle_limit}s after first line "
                        f"(heartbeat kept client alive: {beats} beats)")
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    beats += 1
                except (BrokenPipeError, ConnectionResetError, OSError):
                    log("  <- [sse-heartbeat] client gone (%d beats) — "
                        "backend request cancelled" % beats, level="WARN")
                    self._client_disconnected = True
                    try:
                        resp.close()
                    except Exception:
                        pass
                    return
                continue
            idle_started = None
            line = resp.readline()
            if not line:
                if beats:
                    log(f"  -> [sse-heartbeat] kept client alive: "
                        f"{beats} beats during prefill/relay")
                return
            got_first = True
            yield line

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
        # 请求队列响应头（X-Queue-*），与 route headers 同机制
        queue_headers = getattr(self, '_queue_response_headers', None) or {}
        for hk, hv in queue_headers.items():
            self.send_header(hk, str(hv))
        self._queue_response_headers = None
        # R19 pin 响应头（X-Proxy-Pin-*），与 queue headers 同机制
        pin_headers = getattr(self, '_pin_response_headers', None) or {}
        for hk, hv in pin_headers.items():
            self.send_header(hk, str(hv))
        self._pin_response_headers = None
        # R13 诊断归因头
        self._send_diag_headers()
        self.end_headers()

        total_text = ""
        try:
            for line in self._heartbeat_lines(resp):
                # R13 流式通道: [DONE] 前插入诊断尾注(设计 D1,注释行规范保证被忽略)
                try:
                    _dec = line.decode("utf-8").strip() if isinstance(line, bytes) else str(line).strip()
                except Exception:
                    _dec = ""
                if _dec == "data: [DONE]" and PROXY_DIAG_ENABLED and PROXY_DIAG_SSE_TAIL:
                    try:
                        import diagnostics
                        self.wfile.write(diagnostics.sse_tail_line(
                            diagnostics.build_diag_payload()).encode("utf-8"))
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # client disconnected
                    except Exception as _e:
                        _warn_diag("sse_tail_done", _e)
                self.wfile.write(line)
                try:
                    decoded = _dec
                    if decoded.startswith("data: "):
                        data_str = decoded[6:].strip()
                        if data_str and data_str != "[DONE]":
                            chunk = json.loads(data_str)
                            _choices = chunk.get("choices") or []
                            delta = _choices[0].get("delta", {}) if _choices else {}
                            total_text += delta.get("content", "") or ""
                            # R13/R16: 该透传路径此前完全不解析 usage/timings——
                            # 补齐采集(OpenAI 协议流式的诊断数据来源)
                            if PROXY_DIAG_ENABLED:
                                try:
                                    import diagnostics
                                    _t = chunk.get("timings")
                                    if diagnostics.probe_timings(_t):
                                        diagnostics.set_prompt_tokens(
                                            processed_n=_t.get("prompt_n"),
                                            prompt_eval_ms=_t.get("prompt_ms"),
                                            gen_ms=_t.get("predicted_ms"),
                                            generation_n=_t.get("predicted_n"))
                                    _u = chunk.get("usage")
                                    if _u:
                                        diagnostics.set_prompt_tokens(
                                            sent_n=_u.get("prompt_tokens"),
                                            generation_n=_u.get("completion_tokens"))
                                except Exception as _e:
                                    _warn_diag("passthrough_diag", _e)
                except Exception:
                    pass
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 2026-08-27 修复: 原为 `pass`——断连后读循环继续消费后端流, 改为
            # 上抛给 backend_dispatcher 取消在途生成(同 _emit_text_delta 注释)。
            raise
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

    def _handle_task_context(self):
        """R18: POST /api/task-context — 任务描述 → 上下文证据包（契约 §3.3 冻结版）。

        语义封装原则（review §3.6）: 暴露能力不暴露机制——recall/manifest/orig
        降 admin/debug 面。无命中 → 200 空 items（契约: 不 404）；存储故障 →
        503 + Retry-After（fail-open）。
        """
        try:
            import ctx_recall as _cr
            raw = getattr(self, "_post_body", "") or "{}"
            body = json.loads(raw) if raw.strip() else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            descriptor = body.get("task_descriptor")
            session_key = body.get("session_key")
            session_key = session_key[:64] if isinstance(session_key, str) and session_key else None
            bundle = _cr.build_task_context_bundle(
                descriptor if isinstance(descriptor, dict) else {},
                session_key=session_key,
                budget_chars=body.get("budget_chars") or 6000)
            self._respond_json(bundle)
        except (ValueError, json.JSONDecodeError) as e:
            self._respond_json(
                {"error": {"type": "bad_request", "message": str(e)[:200]}}, 400)
        except Exception as e:
            log(f"  <- [task-context] failed: {e}", level="ERROR")
            self._respond_json(
                {"error": {"type": "storage_fault", "message": str(e)[:200]}},
                503, extra_headers={"Retry-After": "5"})

    def _handle_admin_reload(self):
        """R12: POST /admin/reload — HTTP equivalent of `manage.sh reload` (SIGHUP).

        Idempotent; serialized against SIGHUP by the same _RELOAD_LOCK inside
        reload_config. Also reloads the model catalog (configs/models.json).
        """
        log("  -> [admin/reload] HTTP hot-reload triggered")
        ok, err = True, ""
        try:
            _reload_config()
        except Exception as e:
            ok, err = False, str(e)
            log(f"  <- [admin/reload] failed: {e}", level="ERROR")
        payload = {
            "api_version": PROXY_STATUS_API_VERSION,
            "reloaded": ok,
            "active_profile": _current_active_profile(),
        }
        if err:
            payload["error"] = err
        self._respond_json(payload, 200 if ok else 500)

    def _send_common_headers(self, content_type):
        """Shared response header block: CORS + request-id + R8 route headers."""
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        if not getattr(self, "_request_id", None):
            self._request_id = f"req_{os.urandom(8).hex()}"
        self.send_header("request-id", self._request_id)
        route_headers = getattr(self, '_route_response_headers', None) or {}
        for hk, hv in route_headers.items():
            self.send_header(hk, str(hv))
        self._route_response_headers = None
        # 请求队列响应头（X-Queue-*），与 route headers 同机制
        queue_headers = getattr(self, '_queue_response_headers', None) or {}
        for hk, hv in queue_headers.items():
            self.send_header(hk, str(hv))
        self._queue_response_headers = None
        # R19 pin 响应头（X-Proxy-Pin-*），与 queue headers 同机制
        pin_headers = getattr(self, '_pin_response_headers', None) or {}
        for hk, hv in pin_headers.items():
            self.send_header(hk, str(hv))
        self._pin_response_headers = None
        # R13 诊断归因头（X-Proxy-Diag-*），与 route headers 同机制
        self._send_diag_headers()

    def _send_diag_headers(self):
        """R13: 发送暂存的 X-Proxy-Diag-* 响应头（发后清空，仿 _route_response_headers）。

        值未知（如后端无 timings）的字段从不出现——时序诚实原则（设计 P1）。
        """
        diag_headers = getattr(self, '_diag_response_headers', None) or {}
        for hk, hv in diag_headers.items():
            self.send_header(hk, str(hv))
        try:
            import trace_context
            trace = trace_context.current()
            if trace:
                self.send_header("X-Proxy-Trace-Id", trace["trace_id"])
                self.send_header("X-Proxy-Span-Id", trace["root_span_id"])
        except Exception:
            pass
        self._diag_response_headers = None

    def _handle_anthropic_stream_passthrough(self, resp, anthropic_body):
        """Relay an Anthropic-protocol SSE stream to the client unchanged (Phase D).

        The upstream (anthropic-protocol cloud backend) already emits the exact
        event protocol the client speaks — no conversion, just relay + TTFT.
        """
        self.send_response(200)
        self.send_header("Cache-Control", "no-cache")
        self._send_common_headers("text/event-stream")
        self.end_headers()
        _first_token_time = None
        _tail_written = False
        try:
            for raw in self._heartbeat_lines(resp):
                if not isinstance(raw, bytes):
                    raw = raw.encode("utf-8")
                if _first_token_time is None and raw.strip() and not raw.startswith(b":"):
                    _first_token_time = time.monotonic()
                # R13 流式通道: 尾注必须在 message_stop 之前(设计 D1——解析器
                # 收到终止事件后停止读取,流尾追加会被静默丢弃)。该路径不解析
                # usage(云端 anthropic 协议无 timings),token 字段按 P3 保持 null。
                if (PROXY_DIAG_ENABLED and PROXY_DIAG_SSE_TAIL
                        and not _tail_written and b"message_stop" in raw):
                    _tail_written = self._write_diag_sse_tail()
                self.wfile.write(raw)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 2026-08-27 修复: 原为 `pass`——断连后读循环继续消费后端流, 改为
            # 上抛给 backend_dispatcher 取消在途生成(同 _emit_text_delta 注释)。
            raise
        # 兜底: 流异常截断(未收到 message_stop)时在结尾追加,保证诊断仍可达
        if PROXY_DIAG_ENABLED and PROXY_DIAG_SSE_TAIL and not _tail_written:
            self._write_diag_sse_tail()
        if _first_token_time is not None:
            mc = getattr(_metrics_ctx, 'mc', None)
            if mc:
                mc["ttft_ms"] = round((time.monotonic() - _first_token_time) * 1000, 1)

    def _write_diag_sse_tail(self):
        """Write the R13 SSE tail line; returns True if written. Never raises."""
        try:
            import diagnostics
            self.wfile.write(diagnostics.sse_tail_line(
                diagnostics.build_diag_payload()).encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False  # client disconnected
        except Exception as _e:
            _warn_diag("sse_tail_passthrough", _e)
            return False

    def _handle_anthropic_response(self, status, body_bytes, ctx):
        """Return a non-streaming Anthropic-protocol response unchanged (Phase D)."""
        self.send_response(status)
        self._send_common_headers("application/json")
        self.end_headers()
        try:
            self.wfile.write(body_bytes if isinstance(body_bytes, bytes)
                             else body_bytes.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

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
        # 请求队列响应头（X-Queue-*），与 route headers 同机制
        queue_headers = getattr(self, '_queue_response_headers', None) or {}
        for hk, hv in queue_headers.items():
            self.send_header(hk, str(hv))
        self._queue_response_headers = None
        # R19 pin 响应头（X-Proxy-Pin-*），与 queue headers 同机制
        pin_headers = getattr(self, '_pin_response_headers', None) or {}
        for hk, hv in pin_headers.items():
            self.send_header(hk, str(hv))
        self._pin_response_headers = None
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        # R13 诊断归因头（非流式路径含响应期回填的 Processed-N）
        self._send_diag_headers()
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
        from urllib.parse import parse_qs, urlparse
        metrics_dir = os.environ.get("PROXY_METRICS_DIR", "logs")
        metrics_path = os.path.join(metrics_dir, "proxy_metrics.jsonl")
        # R16: ?session=<key> 过滤(会话维度历史,与 sessions.jsonl 互补)
        sess_filter = (parse_qs(urlparse(self.path).query).get("session", [""])[0] or "").strip()
        records = []
        try:
            with open(metrics_path, "r") as f:
                for line in f:
                    try:
                        rec = json.loads(line.strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if sess_filter and rec.get("session_id") != sess_filter:
                        continue
                    records.append(rec)
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

    # ------------------------------------------------------------------
    # R13-R16 诊断数据面端点（docs/02-architecture-design/
    # diagnostics-dataplane-design-20260819.md §4）
    # ------------------------------------------------------------------
    def _handle_diag_session_endpoint(self):
        """R14/R15/R16/R17: /api/session/<key>/{ledger,archive,metrics,hbe,signals} 分发。

        错误语义（集成契约 §4 fail-open）：未知 key → 404 JSON；已驱逐 → 410 +
        evicted_at；canonical 视图 Phase 1 前未启用 → 501。
        """
        from urllib.parse import parse_qs, urlparse, unquote
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) < 4 or parts[0] != "api" or parts[1] != "session":
            return False
        key = unquote(parts[2])[:64]
        tail = parts[3]
        params = parse_qs(urlparse(self.path).query)

        def _q(name, default=None, cast=str):
            vals = params.get(name, [])
            if not vals:
                return default
            try:
                return cast(vals[0])
            except (TypeError, ValueError):
                return default

        import session_ledger
        import diagnostics

        # G-D: 允许消费方持完整会话头值查询——精确 key 未命中且其 8 字符
        # 截断形式有台账/档案时按截断 key 归并（代理内部路由本就如此归并，
        # 语义一致；harness 无需自行实现截断）。
        if len(key) > 8:
            _short = key[:8]
            if (not session_ledger.LEDGER.session_alive(key)
                    and not session_ledger.ARCHIVE.has_archive(key)
                    and (session_ledger.LEDGER.session_alive(_short)
                         or session_ledger.ARCHIVE.has_archive(_short))):
                key = _short

        if tail == "ledger":
            ledger = session_ledger.LEDGER.build_ledger_json(
                key, limit_turns=_q("limit_turns", None, int))
            if ledger is None:
                evicted = session_ledger.LEDGER.evicted_at(key)
                if evicted:
                    self._respond_json(
                        {"error": {"type": "session_evicted", "evicted_at": evicted}}, 410)
                else:
                    self._respond_json({"error": {"type": "session_not_found"}}, 404)
                return True
            self._respond_json(ledger)
            return True

        if tail == "archive":
            view = _q("view", "sent")
            if view != "sent":
                self._respond_json({
                    "error": {
                        "type": "view_not_enabled",
                        "supported": False,
                        "message": "only view=sent is available; canonical lands with "
                                   "context-engineering Phase 1; client transcript is the "
                                   "client's own record (perspective mismatch, design D7).",
                    }}, 501)
                return True
            result, err = session_ledger.ARCHIVE.read(
                key, turn=_q("turn", None, int),
                limit=_q("limit", 50, int), offset=_q("offset", 0, int),
                include_payload=_q("include_payload", "false") in ("true", "1"))
            if err == "not_found":
                self._respond_json({"error": {"type": "session_not_found"}}, 404)
                return True
            self._respond_json({"session_key": key, "view": "sent", **result})
            return True

        if tail == "metrics":
            records = diagnostics.read_session_metrics(key)
            # #60(2026-08-29): ?since=ISO 前缀过滤。sid 设计为重跑同任务同 key
            # 稳定(断点续跑 key 稳定), sessions.jsonl 按 key 聚合会跨批次串扰——
            # 实测 s38a5c10 聚合了 8/21-26 六天 490 条记录, 8/22 引擎开启时代的
            # 40 个 epoch_turn 混入 8/26 引擎关闭批次的读数(误判"幽灵注入")。
            # since 传 ISO 时间戳/日期前缀, 只保留 ts >= since 的记录。
            since = _q("since", "")
            if since:
                records = [r for r in records
                           if str(r.get("ts", "")) >= since]
            if not records and not session_ledger.LEDGER.session_alive(key):
                self._respond_json({"error": {"type": "session_not_found"}}, 404)
                return True
            self._respond_json(_build_session_metrics_json(key, records))
            return True

        if tail == "hbe":
            # R17: H_BE shadow 探针记录（hbe_probe.py 落盘，schema v2 起含
            # completion_budget/answer_truncated/h_max_token_idx）。消费方
            # agent_go diag.py 只读不算——熵/D_ledger 计算永不跨侧重写。
            records = diagnostics.read_session_hbe(key)
            since = _q("since", "")
            if since:
                records = [r for r in records
                           if str(r.get("ts", "")) >= since]
            if not records and not session_ledger.LEDGER.session_alive(key):
                self._respond_json({"error": {"type": "session_not_found"}}, 404)
                return True
            self._respond_json({
                "session_key": key,
                "count": len(records),
                "records": records,
            })
            return True

        if tail == "signals":
            # R17: 会话信号快照（SignalSnapshot 契约 v1，集成契约 §3.3 冻结版）。
            # 全字段 Optional fail-open；会话已知但无诊断记录 → 全 null 快照。
            records = diagnostics.read_session_metrics(key)
            hbe_records = diagnostics.read_session_hbe(key)
            if not records and not hbe_records:
                evicted = session_ledger.LEDGER.evicted_at(key)
                if evicted:
                    self._respond_json(
                        {"error": {"type": "session_evicted", "evicted_at": evicted}}, 410)
                    return True
                if not session_ledger.LEDGER.session_alive(key):
                    self._respond_json({"error": {"type": "session_not_found"}}, 404)
                    return True
            self._respond_json(
                diagnostics.build_session_signals(key, records, hbe_records))
            return True

        return False

    def _handle_backend_props_endpoint(self, which):
        """llama-server 原生 /props、/slots 只读反代（R16 附表透传）。

        后端不支持（rapid-mlx / cloud）→ 501 + {"supported": false}，
        agent_go fail-open（设计 D9）。LLAMA_BASE 形如 http://host:port/v1，
        原生端点挂在根路径。
        """
        if _ps.IS_CLOUD:
            self._respond_json(
                {"supported": False, "error": "cloud backend has no local slots/props"}, 501)
            return
        base = _ps.LLAMA_BASE.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        url = f"{base}/{which}"
        try:
            import urllib.request as _ur
            req = _ur.Request(url, method="GET")
            with _ur.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._respond_json({"supported": True, "source": url, "data": data})
        except Exception as e:
            self._respond_json(
                {"supported": False, "error": str(e)[:200], "source": url}, 501)


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

    # 配置启动校验（配置统一阶段一：仅警告，不退出）
    config_errors = proxy_config.validate_startup(
        env=os.environ,
        active_conf_path=getattr(_ps, "RELOAD_CONFIG_PATH", "configs/active.conf"),
        backend_type="cloud" if IS_CLOUD else "local",
        strict=False,
    )
    if config_errors:
        for err in config_errors:
            log(f"[CONFIG WARNING] {err}")

    class ReusableThreadingHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True

    ReusableThreadingHTTPServer((host, port), Handler).serve_forever()

if __name__ == "__main__":
    main()
