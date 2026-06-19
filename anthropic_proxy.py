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

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LLAMA_BASE = os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8081/v1")
LLAMA_API_KEY = os.environ.get("LLAMA_API_KEY", "sk-1234")
# Backend type: "local" (llama-server/rapid-mlx) or "cloud" (DeepSeek/OpenAI)
BACKEND_TYPE = os.environ.get("BACKEND_TYPE", "")
if not BACKEND_TYPE:
    # Auto-detect from URL
    if "deepseek" in LLAMA_BASE.lower() or "openai" in LLAMA_BASE.lower() or "api." in LLAMA_BASE.lower():
        BACKEND_TYPE = "cloud"
    else:
        BACKEND_TYPE = "local"
IS_CLOUD = BACKEND_TYPE == "cloud"

# ---------------------------------------------------------------------------
# Concurrency control: backend-aware request serialization
# llama-server on Metal single-GPU suffers from time-slicing with -np > 1,
# so it defaults to 1 (serialized). rapid-mlx handles 2-4 concurrent requests
# well, so its config sets this higher.
# Cloud APIs handle concurrency natively, so we allow more.
# ---------------------------------------------------------------------------
PROXY_MAX_CONCURRENT = int(os.environ.get("PROXY_MAX_CONCURRENT", "4" if IS_CLOUD else "1"))
_llama_lock = threading.Semaphore(PROXY_MAX_CONCURRENT)
# TODO(roadmap-U6): Multi-model collaboration — small model for compression, large for main inference
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-v4-pro" if IS_CLOUD else "mlx-community/Qwen3.6-35B-A3B-4bit")

# ---------------------------------------------------------------------------
# Tool-result clearing: proxy-side context management
# Defaults are tied to BACKEND_TYPE:
#   - Cloud APIs (DeepSeek/OpenAI): disabled by default (1M+ token context)
#   - Local backends (llama-server/rapid-mlx): enabled by default (limited VRAM)
# Override via env vars: PROXY_CLEAR_ENABLED, PROXY_CLEAR_THRESHOLD, PROXY_TOOL_KEEP
# ---------------------------------------------------------------------------
PROXY_CLEAR_ENABLED = os.environ.get("PROXY_CLEAR_ENABLED", "false" if IS_CLOUD else "true").lower() in ("1", "true", "yes")
PROXY_CLEAR_THRESHOLD = int(os.environ.get("PROXY_CLEAR_THRESHOLD", "30000" if IS_CLOUD else "15000"))  # chars, not tokens
PROXY_TOOL_KEEP = int(os.environ.get("PROXY_TOOL_KEEP", "10" if IS_CLOUD else "2"))  # keep last N tool_use/tool_result pairs

# ---------------------------------------------------------------------------
# Frozen Zone: protect the first N messages from L2/L4 modification to
# preserve prefix KV cache stability across consecutive requests.
# - Local: 12 messages (~5-8K tokens of stable prefix)
# - Cloud: 0 (disabled, 1M+ context means low marginal cache value)
# Override via env vars:
# ---------------------------------------------------------------------------
PROXY_FROZEN_HEAD = int(os.environ.get("PROXY_FROZEN_HEAD", "0" if IS_CLOUD else "12"))

# ---------------------------------------------------------------------------
# Tail-first clearing: when enabled, L2 clear_old_tool_results() operates
# from tail to head in the dynamic zone. The newest tool_results get
# cleared first, protecting the prefix's cache stability.
# ---------------------------------------------------------------------------
PROXY_CLEAR_TAIL_FIRST = os.environ.get("PROXY_CLEAR_TAIL_FIRST", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Cache Aligner (Phase 1): protect the first N messages from truncation and
# reordering to stabilize the prefix KV cache across consecutive requests.
# - Local: enabled by default, head=4 (system + skills + first user + first assistant)
# - Cloud: disabled by default (1M context, low marginal cache value)
# ---------------------------------------------------------------------------
PROXY_CACHE_ALIGN_ENABLED = os.environ.get("PROXY_CACHE_ALIGN_ENABLED", "false" if IS_CLOUD else "true").lower() in ("1", "true", "yes")
PROXY_CACHE_ALIGN_HEAD = int(os.environ.get("PROXY_CACHE_ALIGN_HEAD", "4"))

# Per-session last messages for computing common_prefix_ratio.
# Key: session_id, Value: list of message dicts (after cache aligner / before truncation).
_SESSION_LAST_MESSAGES = {}

# ---------------------------------------------------------------------------
# Semantic content compression (Phase 2): TokenSieve-inspired structured
# compression for tool_result contents. Reduces token pressure while keeping
# semantics, as an alternative/supplement to aggressive tool-result clearing.
# ---------------------------------------------------------------------------
PROXY_COMPRESS_ENABLED = os.environ.get("PROXY_COMPRESS_ENABLED", "false" if IS_CLOUD else "true").lower() in ("1", "true", "yes")
PROXY_COMPRESS_THRESHOLD = int(os.environ.get("PROXY_COMPRESS_THRESHOLD", "4096"))
PROXY_COMPRESS_MODE = os.environ.get("PROXY_COMPRESS_MODE", "semantic")  # lossless | semantic | aggressive
PROXY_SCRUB_ANSI = os.environ.get("PROXY_SCRUB_ANSI", "true").lower() in ("1", "true", "yes")
PROXY_SIEVE_JSON_MAX_ITEMS = int(os.environ.get("PROXY_SIEVE_JSON_MAX_ITEMS", "10"))
PROXY_SIEVE_JSON_MAX_STR_LEN = int(os.environ.get("PROXY_SIEVE_JSON_MAX_STR_LEN", "200"))
PROXY_SIEVE_JSON_MAX_DEPTH = int(os.environ.get("PROXY_SIEVE_JSON_MAX_DEPTH", "4"))
PROXY_LOG_DEDUPE = os.environ.get("PROXY_LOG_DEDUPE", "true").lower() in ("1", "true", "yes")
PROXY_DEDUPE_SCALARS = os.environ.get("PROXY_DEDUPE_SCALARS", "false").lower() in ("1", "true", "yes")
PROXY_COMPRESS_AUDIT = os.environ.get("PROXY_COMPRESS_AUDIT", "true").lower() in ("1", "true", "yes")

CONTENT_TOOLS_FALLBACK_ENABLED = os.environ.get("PROXY_CONTENT_TOOLS_FALLBACK", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Context-limit truncation: proxy-side message dropping when total context
# exceeds backend capacity. Complements tool-result clearing by dropping
# entire old messages (not just tool payloads).
# Defaults tied to BACKEND_TYPE (disabled for cloud, enabled for local).
# ---------------------------------------------------------------------------
PROXY_CTX_LIMIT_ENABLED = os.environ.get("PROXY_CTX_LIMIT_ENABLED", "false" if IS_CLOUD else "true").lower() in ("1", "true", "yes")
PROXY_CTX_CHARS_LIMIT = int(os.environ.get("PROXY_CTX_CHARS_LIMIT", "500000" if IS_CLOUD else "180000"))  # chars heuristic
PROXY_CTX_KEEP_HEAD = int(os.environ.get("PROXY_CTX_KEEP_HEAD", "2"))  # keep first N messages (system context + skills)
PROXY_CTX_KEEP_TAIL = int(os.environ.get("PROXY_CTX_KEEP_TAIL", "4"))  # keep last N messages
PROXY_CTX_TRUNCATE_STRATEGY = os.environ.get("PROXY_CTX_TRUNCATE_STRATEGY", "char")
PROXY_CTX_KEEP_ROUNDS = int(os.environ.get("PROXY_CTX_KEEP_ROUNDS", "10"))
PROXY_CTX_KEEP_MESSAGES = int(os.environ.get("PROXY_CTX_KEEP_MESSAGES", "40"))  # fifo strategy: total messages to keep
PROXY_CTX_TOKEN_BUDGET = int(os.environ.get("PROXY_CTX_TOKEN_BUDGET", "30000"))
PROXY_CTX_TOKEN_RATIO = float(os.environ.get("PROXY_CTX_TOKEN_RATIO", "2.0"))

# ---------------------------------------------------------------------------
# Unified char-based lifecycle stage thresholds.
# All size thresholds use _estimate_message_chars() as the single metric.
# Stages are strictly monotonic by char count — lighter compression at
# lower thresholds, heavier at higher thresholds.
#
#   INIT       < PROXY_CLEAR_THRESHOLD           (15K)  — no compression
#   GROWTH     < PROXY_CHARS_GROWTH              (40K)  — tail-40% clearing
#   EXPANSION  < PROXY_CHARS_EXPANSION           (90K)  — tail-60% clearing + think strip
#   SATURATION < PROXY_CHARS_SATURATION          (180K) — full-dynamic clear + merge + trunc
#   OOM_DANGER < PROXY_CHARS_OOM_DANGER          (350K) — no frozen + hard truncation
#   PRE_TRUNC  ≥ PROXY_OOM_SAFE_CHARS            (200K) — keep_rounds=2
#
# Deprecated aliases (auto-fallback if new var not set):
#   PROXY_CTX_CHARS_LIMIT → PROXY_CHARS_SATURATION
# ---------------------------------------------------------------------------
PROXY_CHARS_GROWTH = int(os.environ.get(
    "PROXY_CHARS_GROWTH", "80000" if IS_CLOUD else "40000"))
PROXY_CHARS_EXPANSION = int(os.environ.get(
    "PROXY_CHARS_EXPANSION", "200000" if IS_CLOUD else "90000"))
PROXY_CHARS_SATURATION = int(os.environ.get(
    "PROXY_CHARS_SATURATION",
    os.environ.get("PROXY_CTX_CHARS_LIMIT", "500000" if IS_CLOUD else "180000")))
PROXY_CHARS_OOM_DANGER = int(os.environ.get(
    "PROXY_CHARS_OOM_DANGER", "1000000" if IS_CLOUD else "350000"))

# ---------------------------------------------------------------------------
# Output token control: prevent rapid-mlx from generating unbounded output
# Known Issue #1: rapid-mlx ignores max_tokens
# ---------------------------------------------------------------------------
PROXY_MAX_TOKENS_OVERRIDE = int(os.environ.get("PROXY_MAX_TOKENS_OVERRIDE", "0"))
PROXY_OUTPUT_TOKEN_LIMIT_RATIO = float(os.environ.get("PROXY_OUTPUT_TOKEN_LIMIT_RATIO", "2.0"))
PROXY_BACKEND_TIMEOUT = int(os.environ.get("PROXY_BACKEND_TIMEOUT", "300"))

# DEF-001: hard ceiling for total payload size to prevent rapid-mlx OOM/timeout.
# When a request exceeds this char count, the proxy force-truncates to
# keep_rounds=2 BEFORE any other processing. Default lowered 400K → 200K
# (Phase 1 of proxy-truncation-agent-scenario.md) because multi-round agent
# sessions can compose 80K history + 80K new content = 160K; the old 400K
# ceiling left no headroom for the actual LLM call after pipeline overhead.
# Tune via PROXY_OOM_SAFE_CHARS env var; set very high (e.g. 10000000) to
# disable. PROXY_PRE_TRUNCATE_CHARS is the legacy name, kept for compat.
PROXY_OOM_SAFE_CHARS = int(os.environ.get(
    "PROXY_OOM_SAFE_CHARS",
    os.environ.get("PROXY_PRE_TRUNCATE_CHARS", "200000"),
))
# Legacy alias for any external consumers (status page, metrics, tests).
PROXY_PRE_TRUNCATE_CHARS = PROXY_OOM_SAFE_CHARS

# DEF-005: estimated prompt token limit to prevent Metal OOM.
# After all pipeline steps, if estimated tokens exceed this, force aggressive FIFO.
# Set to 0 to disable. Default 60000 (~120K chars at ratio 2.0) for 48GB Apple Silicon.
PROXY_OOM_SAFE_TOKENS = int(os.environ.get("PROXY_OOM_SAFE_TOKENS", "60000"))

# DEF-001 retry: seconds to ask clients to wait before retrying on 503/504.
PROXY_RETRY_AFTER_SECONDS = int(os.environ.get("PROXY_RETRY_AFTER_SECONDS", "30"))

# ---------------------------------------------------------------------------
# Phase 3: dynamic token estimation by content type
# Replaces the single PROXY_CTX_TOKEN_RATIO for more accurate token counts
# when the prompt is dominated by Chinese, English, or code.
# ---------------------------------------------------------------------------
PROXY_TOKEN_RATIO_CHINESE = float(os.environ.get("PROXY_TOKEN_RATIO_CHINESE", "1.5"))
PROXY_TOKEN_RATIO_ENGLISH = float(os.environ.get("PROXY_TOKEN_RATIO_ENGLISH", "4.0"))
PROXY_TOKEN_RATIO_CODE = float(os.environ.get("PROXY_TOKEN_RATIO_CODE", "3.0"))

# ---------------------------------------------------------------------------
# Phase 3: memory pressure active rejection
# If system used_pct exceeds this threshold, reject new requests with 503.
# ---------------------------------------------------------------------------
PROXY_MEMORY_REJECT_THRESHOLD = float(os.environ.get(
    "PROXY_MEMORY_REJECT_THRESHOLD", "95" if IS_CLOUD else "90"))

# ---------------------------------------------------------------------------
# Phase 3: dynamic max_tokens based on lifecycle stage and backend type
# ---------------------------------------------------------------------------
PROXY_DYNAMIC_MAX_TOKENS_ENABLED = os.environ.get(
    "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", "true" if not IS_CLOUD else "false").lower() in ("1", "true", "yes")
PROXY_DYNAMIC_MAX_TOKENS_INIT = int(os.environ.get("PROXY_DYNAMIC_MAX_TOKENS_INIT", "4096"))
PROXY_DYNAMIC_MAX_TOKENS_GROWTH = int(os.environ.get("PROXY_DYNAMIC_MAX_TOKENS_GROWTH", "4096"))
PROXY_DYNAMIC_MAX_TOKENS_SATURATION = int(os.environ.get("PROXY_DYNAMIC_MAX_TOKENS_SATURATION", "2048"))
PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO = float(os.environ.get(
    "PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO", "0.8"))

# ---------------------------------------------------------------------------
# Phase 3: request failure snapshots
# ---------------------------------------------------------------------------
PROXY_SNAPSHOT_ENABLED = os.environ.get("PROXY_SNAPSHOT_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_SNAPSHOT_MAX_FILES = int(os.environ.get("PROXY_SNAPSHOT_MAX_FILES", "50"))

# ---------------------------------------------------------------------------
# Phase 3: dynamic concurrency control
# ---------------------------------------------------------------------------
PROXY_DYNAMIC_CONCURRENT_ENABLED = os.environ.get(
    "PROXY_DYNAMIC_CONCURRENT_ENABLED", "false" if IS_CLOUD else "true").lower() in ("1", "true", "yes")
PROXY_DYNAMIC_CONCURRENT_MIN = int(os.environ.get("PROXY_DYNAMIC_CONCURRENT_MIN", "1"))
PROXY_DYNAMIC_CONCURRENT_MAX = int(os.environ.get(
    "PROXY_DYNAMIC_CONCURRENT_MAX", "8" if IS_CLOUD else "4"))
PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS = float(os.environ.get(
    "PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS", "30000"))
PROXY_DYNAMIC_CONCURRENT_ERROR_RATE = float(os.environ.get(
    "PROXY_DYNAMIC_CONCURRENT_ERROR_RATE", "0.2"))

# ---------------------------------------------------------------------------
# Loop detection: detect consecutive identical tool_use calls
# ---------------------------------------------------------------------------
PROXY_LOOP_THRESHOLD = int(os.environ.get("PROXY_LOOP_THRESHOLD", "3"))
PROXY_LOOP_LEVEL2 = int(os.environ.get("PROXY_LOOP_LEVEL2", str(PROXY_LOOP_THRESHOLD * 2)))
PROXY_LOOP_LEVEL3 = int(os.environ.get("PROXY_LOOP_LEVEL3", str(PROXY_LOOP_THRESHOLD * 3)))

# Text output loop detection: detect repeated similar text in assistant messages
PROXY_TEXT_LOOP_ENABLED = os.environ.get("PROXY_TEXT_LOOP_ENABLED", "true").lower() in ("true", "1", "yes")
PROXY_TEXT_LOOP_THRESHOLD = int(os.environ.get("PROXY_TEXT_LOOP_THRESHOLD", "3"))
PROXY_TEXT_LOOP_MIN_CHARS = int(os.environ.get("PROXY_TEXT_LOOP_MIN_CHARS", "100"))
PROXY_TEXT_LOOP_SIMILARITY = float(os.environ.get("PROXY_TEXT_LOOP_SIMILARITY", "0.85"))

_LOOP_SESSION_STATE = {}

# Phase 1 (改进3): session request counter for continuation detection.
# When a session has ≥ PROXY_SESSION_CONTINUATION_MIN_REQUESTS prior
# requests AND the current payload is in saturation/expansion territory,
# _classify_lifecycle_stage returns an aggressive config (frozen_head=2,
# truncate_rounds=max(3, base//2)) to prevent OOM in long agent sessions.
_SESSION_REQUEST_COUNT = {}
PROXY_SESSION_CONTINUATION_ENABLED = os.environ.get(
    "PROXY_SESSION_CONTINUATION_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_SESSION_CONTINUATION_MIN_REQUESTS = int(os.environ.get(
    "PROXY_SESSION_CONTINUATION_MIN_REQUESTS", "2"))

PROXY_DEDUP_WINDOW = int(os.environ.get("PROXY_DEDUP_WINDOW", "2"))
_DEDUP_CACHE = {}

# Phase 3: sliding windows for dynamic concurrency control.
_LATENCY_WINDOW = collections.deque(maxlen=50)
_ERROR_WINDOW = collections.deque(maxlen=50)

# Phase 3: metrics schema v1 fixed field set.
# Any metric record logged by log_metrics() is guaranteed to contain these keys.
_METRICS_V1_FIELDS = {
    "schema_version", "ts", "session_id", "input_msgs", "input_chars",
    "input_tools", "output_chars", "duration_ms", "status", "error_type",
    "error", "pipeline", "quality_flags", "compression_ratio", "token_ratio",
    "est_input_tokens", "est_output_tokens", "memory_rejected", "used_pct",
    "max_tokens_original", "max_tokens_dynamic", "snapshot_written",
    "dynamic_concurrent", "tools",
}

def _check_dedup(body_str):
    """Hash-based request dedup with TTL.
    Uses MD5 instead of Python hash() for cross-process stability and collision resistance."""
    h = hashlib.md5(body_str.encode("utf-8")).hexdigest()
    now = datetime.now().timestamp()
    for k in list(_DEDUP_CACHE):
        if now - _DEDUP_CACHE[k] > PROXY_DEDUP_WINDOW:
            del _DEDUP_CACHE[k]
    if h in _DEDUP_CACHE:
        return True
    _DEDUP_CACHE[h] = now
    return False


def _compute_text_similarity(text1, text2):
    """Compute similarity between two texts using character-level Jaccard index.
    Returns float 0.0 (completely different) to 1.0 (identical)."""
    if not text1 or not text2:
        return 0.0
    # Use bigrams for better granularity
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1)) if len(s) >= 2 else set(s)
    b1 = bigrams(text1)
    b2 = bigrams(text2)
    if not b1 or not b2:
        return 0.0
    intersection = len(b1 & b2)
    union = len(b1 | b2)
    return intersection / union if union > 0 else 0.0


def _detect_text_loop(tail_assistant, threshold=PROXY_TEXT_LOOP_THRESHOLD,
                      min_chars=PROXY_TEXT_LOOP_MIN_CHARS,
                      similarity_threshold=PROXY_TEXT_LOOP_SIMILARITY):
    """Detect repeated similar text output in assistant messages.
    Returns (max_run, is_text_loop) tuple."""
    if not PROXY_TEXT_LOOP_ENABLED or len(tail_assistant) < threshold:
        return 0, False

    # Extract text content from assistant messages
    text_history = []
    for msg in tail_assistant:
        content = msg.get("content", "")
        text = ""
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    text += block.get("text", "")
        elif isinstance(content, str):
            text = content
        # Only consider substantial text (ignore short tool-only messages)
        if len(text) >= min_chars:
            text_history.append(text[:500])  # Cap at 500 chars for comparison
        else:
            text_history.append("")  # Short messages break the chain

    if len(text_history) < threshold:
        return 0, False

    # Check for consecutive similar texts from the end
    max_run = 1
    current_run = 1
    for i in range(len(text_history) - 1, 0, -1):
        curr = text_history[i]
        prev = text_history[i - 1]
        if curr and prev:
            sim = _compute_text_similarity(curr, prev)
            if sim >= similarity_threshold:
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                current_run = 1
        else:
            current_run = 1

    return max_run, max_run >= threshold


def _classify_exception(e):
    import socket as _socket
    exc_name = type(e).__name__
    msg = str(e).lower()
    if isinstance(e, (TimeoutError, _socket.timeout)) or "timeout" in exc_name.lower():
        return 504, "timeout_error", True
    if "timed out" in msg or "timeout" in msg or "read timed out" in msg:
        return 504, "timeout_error", True
    if "memory" in exc_name.lower() or "memory" in msg or \
       "resource limit" in msg or "oom" in msg or "out of memory" in msg:
        return 503, "backend_oom", True
    if isinstance(e, (BrokenPipeError, ConnectionResetError)):
        return 499, "client_closed", False
    if isinstance(e, (ConnectionError, ConnectionRefusedError)) or \
       "connection refused" in msg or \
       "failed to connect" in msg:
        return 503, "backend_unavailable", True
    if isinstance(e, (KeyError, TypeError, AttributeError, NameError, ValueError)):
        return 500, "internal_error", False
    return 500, "unknown_error", False


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
PROXY_METRICS_ENABLED = os.environ.get("PROXY_METRICS_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_METRICS_DIR = os.environ.get("PROXY_METRICS_DIR", "logs")
_METRICS_PATH = os.path.join(_SCRIPT_DIR, PROXY_METRICS_DIR, "proxy_metrics.jsonl")
_metrics_lock = threading.Lock()


def _next_jsonl_token():
    """Generate a unique request token for correlating request log entries."""
    global _jsonl_counter
    _jsonl_counter += 1
    return f"req_{_jsonl_counter}_{os.urandom(4).hex()}"


def _ensure_jsonl_dir():
    """Create logs/ directory if it doesn't exist."""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
    except OSError:
        pass


def log_request(model: str, input_chars: int, output_chars: int,
                status: int, duration_ms: float, start_time: str = ""):
    """Append one JSON Lines record to proxy_requests.jsonl (thread-safe)."""
    _ensure_jsonl_dir()
    now_iso = datetime.now().isoformat()
    record = {
        "start_time": start_time or now_iso,
        "end_time": now_iso,
        "method": "POST",
        "path": "/v1/messages",
        "model": model,
        "input_chars": input_chars,
        "output_chars": output_chars,
        "status": status,
        "duration_ms": round(duration_ms, 1),
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _jsonl_lock:
            with open(_JSONL_PATH, "a") as f:
                f.write(line)
    except OSError:
        pass


MODEL_ALIASES = [
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-7",
    "default",
    MODEL_NAME,
]

# Thread-local context for per-request logging (session_id prefix)
_log_ctx = threading.local()

# Thread-local context for per-request metrics collection
_metrics_ctx = threading.local()


def log_metrics(metrics: dict):
    _ensure_jsonl_dir()
    line = json.dumps(metrics, ensure_ascii=False) + "\n"
    try:
        with _metrics_lock:
            with open(_METRICS_PATH, "a") as f:
                f.write(line)
    except OSError:
        pass


def _mask_sensitive(headers_dict):
    if not isinstance(headers_dict, dict):
        return headers_dict
    masked = {}
    for k, v in headers_dict.items():
        kl = k.lower()
        if kl in ("authorization", "x-api-key") and isinstance(v, str):
            if len(v) > 12:
                masked[k] = v[:8] + "****" + v[-4:]
            else:
                masked[k] = v[:4] + "****"
        else:
            masked[k] = v
    return masked


LOG_SCHEMA_VERSION = "v1"


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    sess = getattr(_log_ctx, 'session_id', None)
    if sess:
        line = f"[{ts}] [{level}] [sess={sess}] {msg}"
    else:
        line = f"[{ts}] [{level}] {msg}"
    print(line)
    log_path = os.environ.get("PROXY_LOG_PATH", "/tmp/anthropic_proxy.log")
    try:
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log_structured(event, **kwargs):
    ts = datetime.now().strftime("%H:%M:%S")
    sess = getattr(_log_ctx, 'session_id', None)
    entry = {"schema": LOG_SCHEMA_VERSION, "ts": ts, "event": event}
    if sess:
        entry["session_id"] = sess
    entry.update(kwargs)
    line = json.dumps(entry, ensure_ascii=False)
    print(line)
    log_path = os.environ.get("PROXY_LOG_PATH", "/tmp/anthropic_proxy.log")
    try:
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# SIGHUP hot-reload: re-read active.conf and update module-level config
# without restarting the proxy process. manage.sh `reload` command sends
# SIGHUP after switching the active.conf symlink.
#
# Design:
#   - Tier 1 (simple scalars): updated via setattr from parsed conf
#   - Tier 2 (derived objects): Semaphore rebuilt if MAX_CONCURRENT changed;
#     MODEL_ALIASES rebuilt to pick up new MODEL_NAME
#   - Tier 3 (not reloaded): PORT/HOST (socket bound), log paths, thread-local
#     session state (_SESSION_REQUEST_COUNT, _DEDUP_CACHE, etc.), constant
#     tuples (TOOL_ALWAYS_KEEP, _BLOCKER_ERROR_MARKERS)
#
# Safety:
#   - _RELOAD_LOCK serializes reload against concurrent signal delivery
#   - Semaphore replacement: threads holding the old lock release it
#     normally; new `with _llama_lock:` lookups hit the new Semaphore.
#     Brief over-subscription possible during the swap window — acceptable
#     since reload is triggered at request boundaries, not mid-request.
# ---------------------------------------------------------------------------
_RELOAD_LOCK = threading.Lock()
RELOAD_CONFIG_PATH = os.environ.get(
    "PROXY_RELOAD_CONFIG",
    os.path.join(_SCRIPT_DIR, "configs", "active.conf"),
)
# Local secrets file (git-ignored) — sourced by manage.sh at startup.
# _reload_config parses this for LLAMA_API_KEY so hot-switch to cloud
# mode picks up the real key without restarting the proxy.
RELOAD_SECRET_PATH = os.environ.get(
    "PROXY_RELOAD_SECRET",
    os.path.join(_SCRIPT_DIR, "configs", "secret.local.conf"),
)

# Tier 1 scalars reloaded from conf. Format:
#   (env_key, python_attr_name, cast, cloud_default, local_default)
# cast: "int" | "float" | "bool" | "str"
# Defaults mirror the module-level os.environ.get defaults (lines 19-368).
# Variables with None defaults are handled specially in _reload_config.
_RELOAD_SPEC = [
    # Clearing
    ("PROXY_CLEAR_ENABLED", "PROXY_CLEAR_ENABLED", "bool", "false", "true"),
    ("PROXY_CLEAR_THRESHOLD", "PROXY_CLEAR_THRESHOLD", "int", "30000", "15000"),
    ("PROXY_TOOL_KEEP", "PROXY_TOOL_KEEP", "int", "10", "2"),
    ("PROXY_FROZEN_HEAD", "PROXY_FROZEN_HEAD", "int", "0", "12"),
    ("PROXY_CLEAR_TAIL_FIRST", "PROXY_CLEAR_TAIL_FIRST", "bool", "true", "true"),
    ("PROXY_REREAD_PREVIEW_CHARS", "PROXY_REREAD_PREVIEW_CHARS", "int", "200", "200"),
    # Cache aligner
    ("PROXY_CACHE_ALIGN_ENABLED", "PROXY_CACHE_ALIGN_ENABLED", "bool", "false", "true"),
    ("PROXY_CACHE_ALIGN_HEAD", "PROXY_CACHE_ALIGN_HEAD", "int", "4", "4"),
    # Content tools fallback
    ("PROXY_CONTENT_TOOLS_FALLBACK", "CONTENT_TOOLS_FALLBACK_ENABLED", "bool", "true", "true"),
    # Context truncation
    ("PROXY_CTX_LIMIT_ENABLED", "PROXY_CTX_LIMIT_ENABLED", "bool", "false", "true"),
    ("PROXY_CTX_CHARS_LIMIT", "PROXY_CTX_CHARS_LIMIT", "int", "500000", "180000"),
    ("PROXY_CTX_KEEP_HEAD", "PROXY_CTX_KEEP_HEAD", "int", "2", "2"),
    ("PROXY_CTX_KEEP_TAIL", "PROXY_CTX_KEEP_TAIL", "int", "4", "4"),
    ("PROXY_CTX_TRUNCATE_STRATEGY", "PROXY_CTX_TRUNCATE_STRATEGY", "str", "char", "char"),
    ("PROXY_CTX_KEEP_ROUNDS", "PROXY_CTX_KEEP_ROUNDS", "int", "10", "10"),
    ("PROXY_CTX_KEEP_MESSAGES", "PROXY_CTX_KEEP_MESSAGES", "int", "40", "40"),
    ("PROXY_CTX_TOKEN_BUDGET", "PROXY_CTX_TOKEN_BUDGET", "int", "30000", "30000"),
    ("PROXY_CTX_TOKEN_RATIO", "PROXY_CTX_TOKEN_RATIO", "float", "2.0", "2.0"),
    # Lifecycle thresholds
    ("PROXY_CHARS_GROWTH", "PROXY_CHARS_GROWTH", "int", "80000", "40000"),
    ("PROXY_CHARS_EXPANSION", "PROXY_CHARS_EXPANSION", "int", "200000", "90000"),
    ("PROXY_CHARS_OOM_DANGER", "PROXY_CHARS_OOM_DANGER", "int", "1000000", "350000"),
    # Output control
    ("PROXY_MAX_TOKENS_OVERRIDE", "PROXY_MAX_TOKENS_OVERRIDE", "int", "0", "0"),
    ("PROXY_OUTPUT_TOKEN_LIMIT_RATIO", "PROXY_OUTPUT_TOKEN_LIMIT_RATIO", "float", "2.0", "2.0"),
    ("PROXY_BACKEND_TIMEOUT", "PROXY_BACKEND_TIMEOUT", "int", "300", "300"),
    ("PROXY_OOM_SAFE_TOKENS", "PROXY_OOM_SAFE_TOKENS", "int", "60000", "60000"),
    ("PROXY_RETRY_AFTER_SECONDS", "PROXY_RETRY_AFTER_SECONDS", "int", "30", "30"),
    # Loop detection
    ("PROXY_TEXT_LOOP_ENABLED", "PROXY_TEXT_LOOP_ENABLED", "bool", "true", "true"),
    ("PROXY_TEXT_LOOP_THRESHOLD", "PROXY_TEXT_LOOP_THRESHOLD", "int", "3", "3"),
    ("PROXY_TEXT_LOOP_MIN_CHARS", "PROXY_TEXT_LOOP_MIN_CHARS", "int", "100", "100"),
    ("PROXY_TEXT_LOOP_SIMILARITY", "PROXY_TEXT_LOOP_SIMILARITY", "float", "0.85", "0.85"),
    # Session continuation
    ("PROXY_SESSION_CONTINUATION_ENABLED", "PROXY_SESSION_CONTINUATION_ENABLED", "bool", "true", "true"),
    ("PROXY_SESSION_CONTINUATION_MIN_REQUESTS", "PROXY_SESSION_CONTINUATION_MIN_REQUESTS", "int", "2", "2"),
    # Dedup
    ("PROXY_DEDUP_WINDOW", "PROXY_DEDUP_WINDOW", "int", "2", "2"),
    # Blocker
    ("PROXY_BLOCKER_ENABLED", "PROXY_BLOCKER_ENABLED", "bool", "false", "true"),
    ("PROXY_BLOCKER_THRESHOLD", "PROXY_BLOCKER_THRESHOLD", "int", "2", "2"),
    # Tool filter
    ("PROXY_TOOL_FILTER_ENABLED", "PROXY_TOOL_FILTER_ENABLED", "bool", "false", "true"),
    ("PROXY_TOOL_FILTER_MAX", "PROXY_TOOL_FILTER_MAX", "int", "20", "20"),
    ("PROXY_TOOL_FILTER_RECENT", "PROXY_TOOL_FILTER_RECENT", "int", "5", "5"),
    # History index
    ("PROXY_HISTORY_INDEX", "PROXY_HISTORY_INDEX", "str", "rule", "rule"),
    ("PROXY_HISTORY_TOP_K", "PROXY_HISTORY_TOP_K", "int", "5", "5"),
    ("PROXY_HISTORY_MAX_CHARS", "PROXY_HISTORY_MAX_CHARS", "int", "500", "500"),
    # Metrics
    ("PROXY_METRICS_ENABLED", "PROXY_METRICS_ENABLED", "bool", "true", "true"),
]


def _parse_conf_env(path):
    """Parse a bash-style KEY="value" config file into a dict.

    Handles double/single quotes, comments (#), and blank lines.
    Strips inline comments (space + #) after quoted values.
    Does NOT evaluate shell expansions — active.conf files are simple
    KEY=value assignments with no command substitution.
    """
    result = {}
    if not path or not os.path.isfile(path):
        return result
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Strip inline comment after the value: only when the value
                # is quoted (so # inside quotes is preserved) and a space-#
                # sequence follows the closing quote.
                if len(val) >= 2 and val[0] in ('"', "'"):
                    quote = val[0]
                    # Find the matching closing quote
                    end = val.find(quote, 1)
                    if end != -1:
                        trailing = val[end + 1:].strip()
                        if trailing.startswith("#"):
                            val = val[:end + 1]
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                if key:
                    result[key] = val
    except OSError:
        pass
    return result


def _cast_config_value(value, cast):
    if cast == "int":
        return int(value)
    if cast == "float":
        return float(value)
    if cast == "bool":
        return str(value).lower() in ("1", "true", "yes")
    return value


def _reload_config(signum=None, frame=None):
    """SIGHUP handler: re-read active.conf and update module-level config.

    Triggered by `manage.sh reload` after switching the active.conf symlink.
    Updates Tier 1 scalars via setattr, rebuilds Semaphore (Tier 2) if
    PROXY_MAX_CONCURRENT changed, and rebuilds MODEL_ALIASES to pick up
    the new MODEL_NAME. PORT/HOST and session state are left untouched.
    """
    with _RELOAD_LOCK:
        mod = sys.modules[__name__]
        env = _parse_conf_env(RELOAD_CONFIG_PATH)
        # Merge secrets (LLAMA_API_KEY) from secret.local.conf
        secret_env = _parse_conf_env(RELOAD_SECRET_PATH)
        if secret_env:
            env.update({k: v for k, v in secret_env.items() if k not in env})
        if not env:
            log("[RELOAD] no config parsed from %s — keeping current values"
                % RELOAD_CONFIG_PATH, level="WARN")
            return

        # --- Backend routing (special: drives IS_CLOUD defaults) ---
        # Local configs may not set LLAMA_BASE_URL — reconstruct from
        # LLAMA_HOST + LLAMA_PORT (mirrors manage.sh _start_proxy logic).
        if "LLAMA_BASE_URL" in env:
            base = env["LLAMA_BASE_URL"]
        else:
            host = env.get("LLAMA_HOST", "127.0.0.1")
            port = env.get("LLAMA_PORT", "8081")
            base = "http://%s:%s/v1" % (host, port)
        setattr(mod, "LLAMA_BASE", base)
        setattr(mod, "LLAMA_API_KEY",
                env.get("LLAMA_API_KEY", getattr(mod, "LLAMA_API_KEY")))

        bt = env.get("BACKEND_TYPE", "")
        if not bt:
            low = base.lower()
            if "deepseek" in low or "openai" in low or "api." in low:
                bt = "cloud"
            else:
                bt = "local"
        setattr(mod, "BACKEND_TYPE", bt)
        is_cloud = bt == "cloud"
        setattr(mod, "IS_CLOUD", is_cloud)

        # MODEL_NAME: conf may use MODEL_NAME or LLAMA_MODEL (manage.sh
        # passes MODEL_NAME=${MODEL_NAME:-$LLAMA_MODEL} to the proxy).
        model = env.get("MODEL_NAME") or env.get("LLAMA_MODEL",
                                                  getattr(mod, "MODEL_NAME"))
        setattr(mod, "MODEL_NAME", model)

        # --- Concurrency + Semaphore rebuild (Tier 2) ---
        new_max = int(env.get("PROXY_MAX_CONCURRENT", "4" if is_cloud else "1"))
        old_max = getattr(mod, "PROXY_MAX_CONCURRENT")
        setattr(mod, "PROXY_MAX_CONCURRENT", new_max)
        if new_max != old_max:
            setattr(mod, "_llama_lock", threading.Semaphore(new_max))
            log("[RELOAD] Semaphore rebuilt: %d -> %d" % (old_max, new_max))

        # --- MODEL_ALIASES rebuild (Tier 2) ---
        setattr(mod, "MODEL_ALIASES", [
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
            "claude-3-5-haiku-20241022",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-opus-4-7",
            "default",
            model,
        ])

        # --- Tier 1 scalars ---
        for env_key, py_name, cast, cloud_def, local_def in _RELOAD_SPEC:
            default = cloud_def if is_cloud else local_def
            raw = env.get(env_key, default)
            setattr(mod, py_name, _cast_config_value(raw, cast))

        # --- Dependent defaults (special handling) ---
        # PROXY_LOOP_LEVEL2/3 default to LOOP_THRESHOLD * 2 / * 3
        loop_thr = int(env.get("PROXY_LOOP_THRESHOLD",
                               getattr(mod, "PROXY_LOOP_THRESHOLD")))
        setattr(mod, "PROXY_LOOP_THRESHOLD", loop_thr)
        setattr(mod, "PROXY_LOOP_LEVEL2",
                int(env.get("PROXY_LOOP_LEVEL2", str(loop_thr * 2))))
        setattr(mod, "PROXY_LOOP_LEVEL3",
                int(env.get("PROXY_LOOP_LEVEL3", str(loop_thr * 3))))

        # PROXY_CHARS_SATURATION: falls back to PROXY_CTX_CHARS_LIMIT
        sat = (env.get("PROXY_CHARS_SATURATION")
               or env.get("PROXY_CTX_CHARS_LIMIT",
                          "500000" if is_cloud else "180000"))
        setattr(mod, "PROXY_CHARS_SATURATION", int(sat))

        # PROXY_OOM_SAFE_CHARS: falls back to PROXY_PRE_TRUNCATE_CHARS
        oom = (env.get("PROXY_OOM_SAFE_CHARS")
               or env.get("PROXY_PRE_TRUNCATE_CHARS", "200000"))
        setattr(mod, "PROXY_OOM_SAFE_CHARS", int(oom))
        setattr(mod, "PROXY_PRE_TRUNCATE_CHARS", int(oom))

        log("[RELOAD] OK: backend=%s base=%s model=%s concurrent=%d "
            "clear=%s ctx_limit=%s frozen=%d truncate=%s"
            % (bt, base[:60], model, new_max,
               getattr(mod, "PROXY_CLEAR_ENABLED"),
               getattr(mod, "PROXY_CTX_LIMIT_ENABLED"),
               getattr(mod, "PROXY_FROZEN_HEAD"),
               getattr(mod, "PROXY_CTX_TRUNCATE_STRATEGY")))


# Register SIGHUP handler (must be in main thread; module-level code runs
# in main thread at import time).
signal.signal(signal.SIGHUP, _reload_config)


# ---------------------------------------------------------------------------
# XML -> JSON fallback for Qwen3.5/3.6 tool calling
# llama.cpp issue #21495: model occasionally emits XML instead of JSON
# ---------------------------------------------------------------------------

def _extract_xml_params(raw: str) -> dict:
    """Extract parameters from Qwen XML-style tool calls."""
    result = {}
    # Pattern 1: <parameter=key>value</parameter>
    for m in re.finditer(r'<parameter=(\w+)>([^<]*)</parameter>', raw, re.DOTALL):
        result[m.group(1)] = m.group(2).strip()
    # Pattern 2: <param name="key">value</param>
    for m in re.finditer(r'<param\s+name="(\w+)">([^<]*)</param>', raw, re.DOTALL):
        result[m.group(1)] = m.group(2).strip()
    # Pattern 3: <key>value</key> inside a tool block
    for m in re.finditer(r'<(\w+)>([^<]+)</\1>', raw, re.DOTALL):
        k = m.group(1)
        if k in ("function", "tool_call", "name", "arguments", "parameter"):
            continue
        result[k] = m.group(2).strip()
    return result


def _extract_xml_tool_name(raw: str) -> str:
    """Try to extract function name from XML-style output."""
    # <function=func_name>...
    m = re.search(r'<function=(\w+)>', raw)
    if m:
        return m.group(1)
    # <tool_call><name>func_name</name>
    m = re.search(r'<(?:tool_call|function)[^>]*>.*?<name>(\w+)</name>', raw, re.DOTALL)
    if m:
        return m.group(1)
    return ""


def _repair_truncated_json(raw: str) -> str:
    """Attempt to repair truncated JSON by closing open strings/braces/brackets."""
    if not raw or not raw.strip():
        return "{}"
    s = raw.strip()
    in_string = False
    escape_next = False
    bracket_stack = []
    i = 0
    while i < len(s):
        c = s[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if c == '\\':
            if in_string:
                escape_next = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if c == '{':
            bracket_stack.append('}')
        elif c == '[':
            bracket_stack.append(']')
        elif c in ('}', ']'):
            if bracket_stack:
                bracket_stack.pop()
        i += 1
    if in_string:
        s += '"'
    while bracket_stack:
        s += bracket_stack.pop()
    return s


def _is_truncated_json(raw: str) -> bool:
    """Check if raw string is an *incomplete* JSON (truncated) vs. a malformed one."""
    s = raw.strip() if raw else ""
    if not s:
        return False
    # Single-char openers – clear truncation
    if s in ("{", "[", "{"):
        return True
    # Unclosed string
    if s.count('"') % 2 == 1:
        return True
    # Unmatched braces / brackets
    open_braces = s.count('{') - s.count('}')
    open_brackets = s.count('[') - s.count(']')
    if open_braces > 0 or open_brackets > 0:
        return True
    # Ends mid-value / mid-key (common truncation patterns)
    s_end = s.rstrip()
    if s_end.endswith('":') or s_end.endswith('",') or s_end.endswith(':'):
        return True
    if s_end.endswith(',') or s_end.endswith('"'):
        return True
    return False


def _coerce_booleans(obj):
    """Recursively coerce stringified booleans ('True'/'False'/'true'/'false') to bool."""
    if isinstance(obj, dict):
        return {k: _coerce_booleans(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_booleans(v) for v in obj]
    if isinstance(obj, str):
        if obj.lower() == "true":
            return True
        if obj.lower() == "false":
            return False
    return obj


def _unescape_double_escaped_json(obj):
    """Recursively unescape double-escaped JSON strings.
    Qwen3.6 with qwen3_coder_xml parser occasionally emits arguments where
    array/object values are wrapped as strings, e.g.:
        {"questions": "[{\\\"question\\\": \\\"...\\\"}]"}
    After outer json.loads, the inner value becomes a string like
    '[{"question": "..."}]'.  This helper detects such string-wrapped
    JSON and turns it back into real nested structures.
    """
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            unescaped = _unescape_double_escaped_json(v)
            result[k] = unescaped
        return result
    if isinstance(obj, list):
        return [_unescape_double_escaped_json(v) for v in obj]
    if isinstance(obj, str):
        stripped = obj.strip()
        if (stripped.startswith('[') and stripped.endswith(']')) or \
           (stripped.startswith('{') and stripped.endswith('}')):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                # Try repair for truncated inner JSON
                repaired = _repair_truncated_json(stripped)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass
    return obj


def _finalize_parsed_args(parsed):
    """Apply post-parse fixes: unescape double-escaped JSON, then coerce booleans."""
    if not isinstance(parsed, dict):
        return {}
    parsed = _unescape_double_escaped_json(parsed)
    return _coerce_booleans(parsed)


def parse_tool_arguments(raw: str, tool_name_hint: str = "") -> dict:
    """
    Parse tool arguments from backend response.
    Falls back from JSON -> XML extraction -> empty dict.
    Stringified booleans are coerced to real bools to satisfy client validation.
    Detects truncated JSON vs. malformed JSON and logs explicitly.
    Also unescapes double-escaped JSON arrays/objects (Qwen3.6 qwen3_coder_xml bug).
    """
    raw = raw.strip() if raw else ""
    if not raw:
        return {}

    # 1. Try standard JSON
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return _finalize_parsed_args(parsed)
        return {}
    except json.JSONDecodeError:
        pass

    # 2. Attempt repair unconditionally — _is_truncated_json misses cases where
    # the JSON ends with } but inner structures are truncated (rapid-mlx
    # qwen3_coder_xml output: {"questions": "[{\\\"q\\\": \\\"...\\\"}]")
    repaired = _repair_truncated_json(raw)
    try:
        parsed = json.loads(repaired)
        if isinstance(parsed, dict):
            if repaired != raw:
                log(f"  [JSON_REPAIRED] tool={tool_name_hint}, {len(raw)} -> {len(repaired)} chars")
            return _finalize_parsed_args(parsed)
    except json.JSONDecodeError:
        if repaired != raw:
            log(f"  [JSON_TRUNCATED_REPAIR_FAILED] tool={tool_name_hint}, repaired={repaired[:200]!r}")

    # 3. Try to find a JSON object embedded inside the text
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        try:
            parsed = json.loads(raw[brace_start:brace_end + 1])
            if isinstance(parsed, dict):
                return _finalize_parsed_args(parsed)
        except json.JSONDecodeError:
            pass

    # 4. XML fallback
    xml_params = _extract_xml_params(raw)
    if xml_params:
        log(f"  [XML_FALLBACK] extracted {len(xml_params)} params from XML for tool={tool_name_hint}")
        return _finalize_parsed_args(xml_params)

    # 5. Last resort: treat the whole string as a single "command" or "query" param
    # based on common tool patterns
    if tool_name_hint in ("exec", "bash", "shell"):
        return {"command": raw.strip("`\n ")}
    if tool_name_hint in ("read", "view", "file"):
        return {"file_path": raw.strip("`\n ")}

    log(f"  [JSON_MALFORMED] failed to parse args for tool={tool_name_hint}, raw={raw[:200]!r}")
    return {}


# ---------------------------------------------------------------------------
# Content-text tool-call fallback for Qwen2.5-Coder
# The model emits <tools>\n{"name":"X","arguments":{...}}\n</tools> as plain
# content text instead of populating the OpenAI tool_calls array. This helper
# (and the streaming state-machine class below) extract those blocks and
# synthesize standard Anthropic tool_use blocks. Gated by env var so it can
# be disabled for models where <tools> might be legitimate prose.
# ---------------------------------------------------------------------------
TOOLS_TRIGGER = "<tools>"
TOOLS_END_TAG = "</tools>"


def _parse_tools_block_body(body):
    """Parse the JSON body inside a <tools>...</tools> block.
    Returns {"name": str, "arguments": dict} on success, None on failure."""
    try:
        obj = json.loads(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            return None
        args = obj.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        return {"name": obj["name"], "arguments": args}
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _extract_content_tool_calls(text):
    """Scan plain content text for <tools>...</tools> blocks (Qwen2.5-Coder).
    Returns {"text": <cleaned text>, "tools": [{"name", "arguments"}, ...]}.
    Failed/malformed blocks are left verbatim in the cleaned text so the
    user doesn't lose visible output.
    Tries find() first (handles adjacent blocks); on parse failure retries
    with rfind() so literal </tools> substrings inside JSON args also work."""
    if not CONTENT_TOOLS_FALLBACK_ENABLED or not text:
        return {"text": text or "", "tools": []}
    parts = []
    tools = []
    i = 0
    while i < len(text):
        open_i = text.find(TOOLS_TRIGGER, i)
        if open_i < 0:
            parts.append(text[i:])
            break
        parts.append(text[i:open_i])
        body_start = open_i + len(TOOLS_TRIGGER)
        first_close = text.find(TOOLS_END_TAG, body_start)
        if first_close < 0:
            # No closing tag at all — preserve raw from here on
            parts.append(text[open_i:])
            break
        # Try first occurrence; this is correct for adjacent blocks.
        body = text[body_start:first_close].strip()
        parsed = _parse_tools_block_body(body)
        close_i = first_close
        if parsed is None:
            # Retry with rfind in case arguments JSON contained "</tools>".
            last_close = text.rfind(TOOLS_END_TAG, body_start)
            if last_close > first_close:
                body2 = text[body_start:last_close].strip()
                parsed2 = _parse_tools_block_body(body2)
                if parsed2 is not None:
                    parsed = parsed2
                    body = body2
                    close_i = last_close
        if parsed is None:
            log(f"  [CONTENT_TOOLS_FALLBACK] parse failed for body[:80]={body[:80]!r}")
            parts.append(text[open_i:close_i + len(TOOLS_END_TAG)])
        else:
            tools.append(parsed)
            log(f"  [CONTENT_TOOLS_FALLBACK] extracted tool={parsed['name']}")
        i = close_i + len(TOOLS_END_TAG)
    return {"text": "".join(parts).strip(), "tools": tools}


class _StreamingToolsExtractor:
    """State machine for stripping <tools>...</tools> blocks from a token stream.

    Usage:
        ext = _StreamingToolsExtractor()
        for chunk in incoming_deltas:
            for kind, value in ext.feed(chunk):
                ...  # kind in {"text", "tool"}; value is str or {"name","arguments"}
        for kind, value in ext.finalize():
            ...

    Holds back the last few characters of each delta only if they could still
    extend to the opening trigger (max len(TRIGGER)-1 = 6 chars).
    Disabled (passthrough) when CONTENT_TOOLS_FALLBACK_ENABLED is false.
    """

    def __init__(self):
        self.pending_text = ""        # buffered text that may include partial <tools> prefix
        self.in_tools_block = False   # True between <tools> and </tools>
        self.tools_content_buf = ""   # accumulated body of current <tools> block
        self._enabled = CONTENT_TOOLS_FALLBACK_ENABLED

    def feed(self, incoming):
        if not incoming:
            return []
        if not self._enabled:
            return [("text", incoming)]
        events = []
        text = self.pending_text + incoming
        self.pending_text = ""

        while text:
            if self.in_tools_block:
                self.tools_content_buf += text
                text = ""
                # Prefer first occurrence; if parse fails we retry with rfind below.
                end_idx = self.tools_content_buf.find(TOOLS_END_TAG)
                if end_idx < 0:
                    break  # need more deltas
                body = self.tools_content_buf[:end_idx].strip()
                remainder = self.tools_content_buf[end_idx + len(TOOLS_END_TAG):]
                parsed = _parse_tools_block_body(body)
                if parsed is None:
                    # Retry with rfind in case arguments JSON contained "</tools>"
                    last_end = self.tools_content_buf.rfind(TOOLS_END_TAG)
                    if last_end > end_idx:
                        body2 = self.tools_content_buf[:last_end].strip()
                        remainder2 = self.tools_content_buf[last_end + len(TOOLS_END_TAG):]
                        parsed2 = _parse_tools_block_body(body2)
                        if parsed2 is not None:
                            parsed = parsed2
                            body = body2
                            remainder = remainder2
                if parsed is not None:
                    events.append(("tool", parsed))
                    log(f"  [CONTENT_TOOLS_FALLBACK] streamed tool={parsed['name']}")
                else:
                    log(f"  [CONTENT_TOOLS_FALLBACK] streamed parse failed for body[:80]={body[:80]!r}")
                    events.append(("text", TOOLS_TRIGGER + body + TOOLS_END_TAG))
                self.in_tools_block = False
                self.tools_content_buf = ""
                text = remainder
                continue

            trigger_idx = text.find(TOOLS_TRIGGER)
            if trigger_idx >= 0:
                if trigger_idx > 0:
                    events.append(("text", text[:trigger_idx]))
                self.in_tools_block = True
                text = text[trigger_idx + len(TOOLS_TRIGGER):]
                continue

            # No trigger in this batch — hold back any suffix that could still
            # extend to <tools>, emit the rest.
            holdback = 0
            for h in range(1, min(len(TOOLS_TRIGGER), len(text)) + 1):
                if text[-h:] == TOOLS_TRIGGER[:h]:
                    holdback = h
            if holdback > 0 and len(text) > holdback:
                events.append(("text", text[:-holdback]))
                self.pending_text = text[-holdback:]
            elif holdback > 0:
                self.pending_text = text
            else:
                events.append(("text", text))
            break

        return events

    def finalize(self):
        """Emit any unflushed state. Call once after the stream ends."""
        events = []
        if not self._enabled:
            return events
        if self.pending_text:
            events.append(("text", self.pending_text))
            self.pending_text = ""
        if self.in_tools_block:
            # No closing </tools> ever arrived — preserve buffered content as text.
            log("  [CONTENT_TOOLS_FALLBACK] EOF inside <tools>, emitting raw")
            events.append(("text", TOOLS_TRIGGER + self.tools_content_buf))
            self.in_tools_block = False
            self.tools_content_buf = ""
        return events


def convert_anthropic_tools_to_openai(tools):
    """Convert Anthropic tool format to OpenAI tool format.

    Handles three tool types:
    - Anthropic custom tools (type="custom") → OpenAI function tools
    - Simple tools (name only, no type) → OpenAI function tools
    - Anthropic server-side web_search_20250305 → mapped to a function tool
      with a `query` parameter so local/cloud OpenAI-compatible backends can
      execute it. The model extracts the query from the user message and
      emits a tool_call; proxy returns it as a tool_use block to Claude Code.
      See AGENTS.md "Server-side web_search mapping" for details.
    """
    if not tools:
        return None
    openai_tools = []
    for tool in tools:
        tool_type = tool.get("type", "")
        # Server-side web_search tool → function with query parameter
        if tool_type == "web_search_20250305":
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", "web_search"),
                    "description": (
                        "Search the web for up-to-date information. "
                        "Extract the search query from the user's message "
                        "(the text after 'Perform a web search for the query: ') "
                        "and pass it as the `query` parameter. Returns search "
                        "results with titles, URLs, and snippets."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "description": "The search query string",
                                "type": "string",
                            },
                        },
                        "required": ["query"],
                    },
                }
            })
        elif tool_type == "custom":
            # Anthropic custom tool
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                }
            })
        elif "name" in tool:
            # Simple tool definition
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", tool.get("parameters", {})),
                }
            })
    return openai_tools if openai_tools else None


def convert_anthropic_tool_choice_to_openai(tool_choice):
    """Convert Anthropic tool_choice to OpenAI tool_choice."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return "auto"
        elif tool_choice == "any":
            return {"type": "function"}
        elif tool_choice == "none":
            return "none"
    elif isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type", "")
        if tc_type == "tool":
            return {
                "type": "function",
                "function": {"name": tool_choice.get("name", "")}
            }
        elif tc_type == "auto":
            return "auto"
        elif tc_type == "any":
            return {"type": "function"}
        elif tc_type == "none":
            return "none"
    return None


def _estimate_message_chars(messages):
    """Rough character count for threshold checking (no tokenizer)."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    total += len(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    total += len(str(block.get("content", "")))
                elif block.get("type") == "tool_use":
                    total += len(json.dumps(block.get("input", {}), ensure_ascii=False))
        else:
            total += len(str(content))
    return total


def _extract_text_from_messages(messages):
    """Concatenate all text content from messages for content-type analysis."""
    parts = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    parts.append(str(block.get("content", "")))
                elif block.get("type") == "tool_use":
                    parts.append(json.dumps(block.get("input", {}), ensure_ascii=False))
        else:
            parts.append(str(content))
    return "\n".join(parts)


def _classify_content_for_ratio(text):
    """Return dominant content type for dynamic token ratio selection.

    Heuristics:
      - cjk: ratio of CJK characters > 0.4
      - code: high density of code tokens (brackets, semicolons, keywords)
      - english: fallback
    """
    if not text:
        return "english"
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    total = len(text)
    if total == 0:
        return "english"
    if cjk / total > 0.4:
        return "chinese"
    code_tokens = len(re.findall(r"[{}\[\];()=]", text))
    keywords = len(re.findall(
        r"\b(def|class|function|const|let|var|import|from|return|if|else|for|while|try|except|async|await)\b",
        text, re.IGNORECASE))
    if total > 200 and (code_tokens / total > 0.08 or keywords >= 5):
        return "code"
    return "english"


def _estimate_tokens_dynamic(messages, ratio_override=None):
    """Estimate token count using content-type-aware ratios.

    Falls back to PROXY_CTX_TOKEN_RATIO when ratio_override is provided or
    content classification is inconclusive.
    """
    if ratio_override:
        return int(_estimate_message_chars(messages) / max(ratio_override, 0.1))
    text = _extract_text_from_messages(messages)
    content_type = _classify_content_for_ratio(text)
    ratio_map = {
        "chinese": PROXY_TOKEN_RATIO_CHINESE,
        "english": PROXY_TOKEN_RATIO_ENGLISH,
        "code": PROXY_TOKEN_RATIO_CODE,
    }
    ratio = ratio_map.get(content_type, PROXY_CTX_TOKEN_RATIO)
    # For mixed content, weight by detected type but blend with the default ratio
    # to avoid over-correction on short or ambiguous inputs.
    if len(text) < 500:
        ratio = (ratio + PROXY_CTX_TOKEN_RATIO) / 2.0
    return int(_estimate_message_chars(messages) / max(ratio, 0.1))


def _compute_re_read_rate(re_read_files, cleared_files):
    """Compute re-read rate as a percentage capped at 100.

    DEF-003: rate must be re_read_files / cleared_files, not raw call count.
    Returns 0.0 when there are no cleared files.
    """
    if not cleared_files:
        return 0.0
    return min(float(re_read_files) / float(cleared_files) * 100.0, 100.0)


def _message_stable_hash(msg):
    """Return a stable hash for a message dict used in prefix comparison."""
    try:
        return hashlib.sha256(json.dumps(msg, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return hashlib.sha256(str(msg).encode("utf-8")).hexdigest()


def _compute_common_prefix_ratio(current, previous):
    """Compute the ratio of chars in the common prefix of two message lists.

    Walks from the first message until messages differ, then returns
    common_chars / total_chars. Used to quantify prefix cache stability.
    """
    if not current or not previous:
        return 0.0
    common_chars = 0
    min_len = min(len(current), len(previous))
    for i in range(min_len):
        if _message_stable_hash(current[i]) != _message_stable_hash(previous[i]):
            break
        common_chars += _estimate_message_chars([current[i]])
    total_chars = _estimate_message_chars(current)
    if total_chars <= 0:
        return 0.0
    return round(common_chars / total_chars, 4)


def _normalize_system_messages(messages):
    """Convert mid-conversation system messages to user messages.

    Qwen chat templates require the system message to be at the beginning.
    Claude Code's mid-conversation-system beta inserts system messages later,
    which breaks rapid-mlx/Qwen. We keep the first system message (if any) and
    convert subsequent ones to user messages prefixed with [System update].
    """
    if not messages:
        return messages
    result = []
    seen_system = False
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            if not seen_system:
                result.append(msg)
                seen_system = True
            else:
                content = msg.get("content", "")
                text = content
                if isinstance(content, list):
                    text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
                new_msg = {
                    "role": "user",
                    "content": [{"type": "text", "text": f"[System update]: {text}"}]
                }
                result.append(new_msg)
        else:
            result.append(msg)
    return result


def _apply_cache_aligner(messages):
    """Cache Aligner MVP: protect the first N messages from truncation.

    Returns (prefix_messages, dynamic_messages). The caller should run
    truncation only on dynamic_messages, then reassemble prefix + dynamic.
    """
    if not PROXY_CACHE_ALIGN_ENABLED:
        return [], messages
    head = min(PROXY_CACHE_ALIGN_HEAD, len(messages))
    prefix = messages[:head]
    dynamic = messages[head:]
    return prefix, dynamic


# ---------------------------------------------------------------------------
# Phase 2: TokenSieve-inspired content compression for tool_result payloads.
# ---------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*m')


def _scrub_ansi(text):
    """Remove ANSI color/control escape sequences from terminal output."""
    if not isinstance(text, str):
        text = str(text)
    return _ANSI_ESCAPE_RE.sub('', text)


def _detect_content_type(text, mime_hint=None):
    """Detect whether a tool_result payload is json, code, log, or plain text."""
    if mime_hint:
        hint = mime_hint.lower()
        if "json" in hint:
            return "json"
        if any(k in hint for k in ("html", "xml", "markdown")):
            return "text"
        if any(k in hint for k in ("python", "javascript", "typescript", "rust", "go", "c++", "c", "java")):
            return "code"
    if not isinstance(text, str):
        return "text"
    stripped = text.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or \
       (stripped.startswith("[") and stripped.endswith("]")):
        try:
            json.loads(stripped)
            return "json"
        except Exception:
            pass
    # Log heuristic: lines starting with timestamps or level keywords.
    lines = stripped.splitlines()
    if len(lines) >= 2:
        log_markers = 0
        for line in lines[:10]:
            if re.search(r"^\d{4}[-/]\d{2}[-/]\d{2}|^\d{2}:\d{2}:\d{2}|\b(ERROR|WARN|INFO|DEBUG|FATAL)\b", line):
                log_markers += 1
        if log_markers >= 1:
            return "log"
    # Code heuristic: significant syntax markers or indentation pattern.
    code_markers = sum(1 for kw in ("def ", "class ", "import ", "function ", "const ", "let ", "var ", "#include", "return ")
                       if kw in text)
    indented_lines = sum(1 for line in lines if line.startswith(("    ", "\t")))
    if code_markers >= 1 or (len(lines) >= 3 and indented_lines >= 1):
        return "code"
    return "text"


def _sieve_json(obj, max_items=None, max_str_len=None, max_depth=None,
                seen_strings=None, enable_dedupe=False, _depth=0):
    """Summarize JSON while preserving structure.

    - Arrays keep first max_items entries plus a count note.
    - Strings longer than max_str_len are truncated.
    - Nesting deeper than max_depth is stringified.
    - Optional scalar dedupe (first-seen-wins) for long repeated strings.
    """
    if max_items is None:
        max_items = PROXY_SIEVE_JSON_MAX_ITEMS
    if max_str_len is None:
        max_str_len = PROXY_SIEVE_JSON_MAX_STR_LEN
    if max_depth is None:
        max_depth = PROXY_SIEVE_JSON_MAX_DEPTH
    if seen_strings is None:
        seen_strings = {}

    if _depth > max_depth:
        return str(obj)[:max_str_len]

    if isinstance(obj, str):
        if enable_dedupe and len(obj) > 20:
            if obj in seen_strings:
                return f"###(repeated: {seen_strings[obj]})"
            seen_strings[obj] = len(seen_strings) + 1
        if len(obj) > max_str_len:
            return obj[:max_str_len] + f"...[truncated {len(obj) - max_str_len} chars]"
        return obj

    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if obj is None:
        return None

    if isinstance(obj, list):
        if len(obj) > max_items:
            summarized = [_sieve_json(item, max_items, max_str_len, max_depth,
                                      seen_strings, enable_dedupe, _depth + 1)
                          for item in obj[:max_items]]
            return summarized + [f"...({len(obj) - max_items} more items)"]
        return [_sieve_json(item, max_items, max_str_len, max_depth,
                            seen_strings, enable_dedupe, _depth + 1)
                for item in obj]

    if isinstance(obj, dict):
        return {
            k: _sieve_json(v, max_items, max_str_len, max_depth,
                           seen_strings, enable_dedupe, _depth + 1)
            for k, v in obj.items()
        }

    return str(obj)[:max_str_len]


def _compress_code(text):
    """Remove comments and collapse excessive blank lines while keeping code."""
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    result = []
    prev_blank = False
    for line in lines:
        stripped = line.strip()
        # Drop full-line comments (common languages)
        if stripped.startswith(("#", "//", "/*", "*", "--", ";")):
            continue
        # Drop trailing comments
        for marker in ("//", "#", "--"):
            idx = line.find(marker)
            if idx >= 0:
                line = line[:idx]
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        result.append(line.rstrip())
        prev_blank = is_blank
    return "\n".join(result).strip()


def _compress_log(text, dedupe=True):
    """Deduplicate adjacent log lines and strip common timestamps.
    Keep lines containing error/exception/warning keywords."""
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    out = []
    last_line = None
    dup_count = 0
    for line in lines:
        # Strip common timestamp prefixes
        cleaned = re.sub(r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?\s*", "", line)
        cleaned = re.sub(r"^\d{2}:\d{2}:\d{2}\s*", "", cleaned)
        if dedupe and cleaned == last_line:
            dup_count += 1
            continue
        if dup_count > 0:
            out.append(f"...({dup_count} identical lines omitted)")
            dup_count = 0
        last_line = cleaned
        # Prioritize error lines by keeping them verbatim; compress benign lines
        if re.search(r"\b(ERROR|Exception|Traceback|FATAL|CRITICAL)\b", cleaned, re.IGNORECASE):
            out.append(line)  # keep original with timestamp for errors
        else:
            out.append(cleaned)
    if dup_count > 0:
        out.append(f"...({dup_count} identical lines omitted)")
    return "\n".join(out).strip()


def _compress_text(text, max_len=2000):
    """Truncate very long plain text while keeping first/last context."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_len:
        return text
    head = text[:max_len // 2]
    tail = text[-max_len // 2:]
    return f"{head}\n\n...[truncated {len(text) - max_len} chars]\n\n{tail}"


def _dedupe_scalars(obj, min_len=20, seen=None):
    """First-seen-wins scalar deduplication for JSON/string values."""
    if seen is None:
        seen = {}
    if isinstance(obj, str):
        if len(obj) >= min_len:
            if obj in seen:
                return f"###(repeated: {seen[obj]})"
            seen[obj] = len(seen) + 1
        return obj
    if isinstance(obj, list):
        return [_dedupe_scalars(item, min_len, seen) for item in obj]
    if isinstance(obj, dict):
        return {k: _dedupe_scalars(v, min_len, seen) for k, v in obj.items()}
    return obj


def _audit_compression(original, compressed, content_type):
    """Validate that compression did not destroy syntax/semantics."""
    if not PROXY_COMPRESS_AUDIT:
        return True
    if content_type == "json":
        try:
            json.loads(compressed)
            return True
        except Exception:
            return False
    if content_type == "code":
        # Simple balance check: brackets and quotes should still be roughly paired.
        open_br = compressed.count("(") + compressed.count("[") + compressed.count("{")
        close_br = compressed.count(")") + compressed.count("]") + compressed.count("}")
        return abs(open_br - close_br) <= 2
    # text/log: always pass the audit
    return True


def compress_tool_result(content, mime_hint=None, threshold=None, mode=None):
    """Compress a single tool_result content payload.

    Returns a dict:
        {
            "original": str,
            "compressed": str,
            "content_type": str,
            "strategy": str,
            "audit_pass": bool,
            "ratio": float,
        }
    """
    if threshold is None:
        threshold = PROXY_COMPRESS_THRESHOLD
    if mode is None:
        mode = PROXY_COMPRESS_MODE

    original = content if isinstance(content, str) else str(content)

    if mode == "lossless" or len(original) < threshold:
        return {
            "original": original,
            "compressed": original,
            "content_type": "short",
            "strategy": "none",
            "audit_pass": True,
            "ratio": 1.0,
        }

    # Stage 1: scrub ANSI
    scrubbed = _scrub_ansi(original) if PROXY_SCRUB_ANSI else original

    # Stage 2: detect content type
    content_type = _detect_content_type(scrubbed, mime_hint=mime_hint)

    # Stage 3: route to compressor
    if content_type == "json":
        try:
            parsed = json.loads(scrubbed)
            enable_dedupe = PROXY_DEDUPE_SCALARS and mode == "aggressive"
            compressed_obj = _sieve_json(parsed, enable_dedupe=enable_dedupe)
            if PROXY_DEDUPE_SCALARS and mode == "aggressive":
                compressed_obj = _dedupe_scalars(compressed_obj)
            compressed = json.dumps(compressed_obj, ensure_ascii=False, separators=(',', ':'))
            strategy = "json_sieve"
        except Exception:
            compressed = scrubbed
            strategy = "json_passthrough"
    elif content_type == "code":
        compressed = _compress_code(scrubbed)
        strategy = "code_compress"
    elif content_type == "log":
        compressed = _compress_log(scrubbed, dedupe=PROXY_LOG_DEDUPE)
        strategy = "log_compress"
    else:
        compressed = _compress_text(scrubbed)
        strategy = "text_truncate"

    # Stage 4: audit
    audit_pass = _audit_compression(scrubbed, compressed, content_type)
    if not audit_pass:
        compressed = scrubbed
        strategy = "audit_fallback"

    ratio = len(compressed) / len(original) if original else 1.0
    return {
        "original": original,
        "compressed": compressed,
        "content_type": content_type,
        "strategy": strategy,
        "audit_pass": audit_pass,
        "ratio": round(ratio, 4),
    }


def _generate_tool_summary(tool_name, meta_info):
    """Generate deterministic summary for a cleared tool result.
    Same (tool_name, meta_info) always produces the same output,
    enabling prefix cache hits when the same tool call appears across requests.
    """
    if not tool_name:
        return "tool"
    if meta_info.startswith(" file="):
        return f'{tool_name}("{meta_info[6:]}")'
    elif meta_info.startswith(" cmd="):
        cmd = meta_info[5:].strip()
        return f'{tool_name}("{cmd}")'
    return tool_name


def _classify_lifecycle_stage(messages, session_id=None):
    """
    Classify the current request into a lifecycle stage based on total chars.
    All thresholds are in chars (from _estimate_message_chars), guaranteeing
    monotonic escalation: lighter compression at lower stages, heavier at
    higher stages.

    Phase 1 改进3: When session_id is provided AND the session has already
    accumulated >= PROXY_SESSION_CONTINUATION_MIN_REQUESTS prior requests
    (i.e. this is a continuation, not the first call) AND the payload is
    past PROXY_CHARS_EXPANSION, the function returns an aggressive config
    even if the raw char count would map to a milder stage. This addresses
    the "agent 累积后大请求" 盲区 in proxy-truncation-agent-scenario.md.

    Returns a dict:
      {
        "stage": "init"|"growth"|"expansion"|"saturation"|"oom_danger"|"pre_trunc",
        "total_chars": int,
        "frozen_head": int,          # Frozen Zone protection
        "clear_zone_pct": float|None, # tail-clear zone (None=skip)
        "thinking_keep": int,         # thinking keep_recent (0=skip)
        "truncate_rounds": int|None,  # L5 rounds (None=skip truncation)
        "oom_safety": bool,           # Enable OOM safety iterative FIFO
        "is_continuation": bool,      # True when session_id is a known continuation
        "request_count": int,         # # of prior requests in this session
      }
    """
    total_chars = _estimate_message_chars(messages)
    cloud = IS_CLOUD

    # Phase 1: detect session continuation. The increment happens here so the
    # counter advances exactly once per request, atomically w.r.t. the
    # classification decision.
    is_continuation = False
    request_count = 0
    if PROXY_SESSION_CONTINUATION_ENABLED and session_id:
        request_count = _SESSION_REQUEST_COUNT.get(session_id, 0)
        is_continuation = request_count >= PROXY_SESSION_CONTINUATION_MIN_REQUESTS
        _SESSION_REQUEST_COUNT[session_id] = request_count + 1

    # Aggressive branch: continuation + above-EXPANSION payload. Return a
    # saturation-grade config regardless of the raw stage mapping. This
    # catches the "agent 多轮累积后大请求" case where the in-memory history
    # alone is > 90K chars but hasn't yet tripped the OOM_DANGER threshold.
    if is_continuation and total_chars > PROXY_CHARS_EXPANSION:
        aggressive_rounds = max(3, PROXY_CTX_KEEP_ROUNDS // 2)
        return {
            "stage": "saturation",
            "total_chars": total_chars,
            "frozen_head": 2,
            "clear_zone_pct": 1.0,
            "thinking_keep": 3,
            "truncate_rounds": aggressive_rounds,
            "oom_safety": not cloud,
            "is_continuation": True,
            "request_count": request_count,
        }

    # Defaults per stage — ordered by increasing severity
    if total_chars < PROXY_CLEAR_THRESHOLD:
        return {
            "stage": "init", "total_chars": total_chars,
            "frozen_head": PROXY_FROZEN_HEAD if not cloud else 0,
            "clear_zone_pct": None, "thinking_keep": 0,
            "truncate_rounds": None, "oom_safety": False,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    elif total_chars < PROXY_CHARS_GROWTH:
        return {
            "stage": "growth", "total_chars": total_chars,
            "frozen_head": PROXY_FROZEN_HEAD if not cloud else 0,
            "clear_zone_pct": 0.4, "thinking_keep": 0,
            "truncate_rounds": None, "oom_safety": False,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    elif total_chars < PROXY_CHARS_EXPANSION:
        return {
            "stage": "expansion", "total_chars": total_chars,
            "frozen_head": PROXY_FROZEN_HEAD if not cloud else 0,
            "clear_zone_pct": 0.6, "thinking_keep": 5,
            "truncate_rounds": PROXY_CTX_KEEP_ROUNDS, "oom_safety": False,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    elif total_chars < PROXY_CHARS_SATURATION:
        return {
            "stage": "saturation", "total_chars": total_chars,
            "frozen_head": max(2, (PROXY_FROZEN_HEAD if not cloud else 0) // 2),
            "clear_zone_pct": 1.0, "thinking_keep": 3,
            "truncate_rounds": PROXY_CTX_KEEP_ROUNDS, "oom_safety": False,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    elif total_chars < PROXY_CHARS_OOM_DANGER:
        return {
            "stage": "oom_danger", "total_chars": total_chars,
            "frozen_head": 0, "clear_zone_pct": 1.0, "thinking_keep": 1,
            "truncate_rounds": 3, "oom_safety": not cloud,
            "is_continuation": is_continuation, "request_count": request_count,
        }
    else:
        return {
            "stage": "pre_trunc", "total_chars": total_chars,
            "frozen_head": 0, "clear_zone_pct": 1.0, "thinking_keep": 1,
            "truncate_rounds": 2, "oom_safety": not cloud,
            "is_continuation": is_continuation, "request_count": request_count,
        }


def _compute_dynamic_max_tokens(max_tokens_orig, stage_config, mem=None):
    """Compute a context-aware max_tokens ceiling.

    - Heavy lifecycle stages get a lower ceiling.
    - rapid-mlx backend gets an additional discount (known to ignore max_tokens).
    - Low available memory lowers the ceiling one more notch.
    Returns (adjusted_max_tokens, reason_string).
    """
    if not PROXY_DYNAMIC_MAX_TOKENS_ENABLED:
        return max_tokens_orig, "dynamic_disabled"

    stage = stage_config.get("stage", "init")
    if stage == "init":
        cap = PROXY_DYNAMIC_MAX_TOKENS_INIT
    elif stage in ("growth", "expansion"):
        cap = PROXY_DYNAMIC_MAX_TOKENS_GROWTH
    else:  # saturation, oom_danger, pre_trunc
        cap = PROXY_DYNAMIC_MAX_TOKENS_SATURATION

    adjusted = min(max_tokens_orig, cap)
    reasons = [f"stage={stage}"]

    if not IS_CLOUD and "rapid-mlx" in (MODEL_NAME or ""):
        adjusted = int(adjusted * PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO)
        reasons.append("rapid-mlx_discount")

    try:
        if mem is None:
            mem = _get_system_memory()
        available_gb = float(mem.get("available_gb", 48))
        total_gb = float(mem.get("total_gb", 48))
        if total_gb > 0 and available_gb / total_gb < 0.20:
            adjusted = int(adjusted * 0.7)
            reasons.append("low_memory")
    except Exception:
        pass

    adjusted = max(1, adjusted)
    return adjusted, ",".join(reasons)


def _compress_content_pass(messages, tools_list=None, stage_config=None):
    """
    Single-pass content compression: combines L2 tool-result clearing and
    L4 thinking block stripping into one traversal.

    Scans messages once to locate all tool_results and thinking blocks,
    then applies semantic clearing and thinking stripping in a second pass.
    Both operations respect Frozen Zone protection — messages before
    frozen_head are never modified.

    Returns (messages, combined_stats_dict).
    """
    if stage_config is None:
        stage_config = _classify_lifecycle_stage(messages)

    frozen_head = stage_config.get("frozen_head", PROXY_FROZEN_HEAD)
    clear_zone_pct = stage_config.get("clear_zone_pct")
    thinking_keep = stage_config.get("thinking_keep", 3)
    total_chars = stage_config.get("total_chars", _estimate_message_chars(messages))

    # ---- Phase 1: collect indices (single scan) ----
    all_tool_result_indices = []
    thinking_indices = []

    for msg_idx, msg in enumerate(messages):
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block_idx, block in enumerate(content):
            bt = block.get("type", "")
            if bt == "tool_result":
                all_tool_result_indices.append((msg_idx, block_idx))
            elif bt == "thinking" and msg.get("role") == "assistant":
                if msg_idx not in thinking_indices:
                    thinking_indices.append(msg_idx)
        # Also check for inline <thinking> tags in text blocks
        if msg.get("role") == "assistant":
            for block in content:
                if block.get("type") == "text":
                    txt = block.get("text", "")
                    if "<thinking>" in txt or "</thinking>" in txt:
                        if msg_idx not in thinking_indices:
                            thinking_indices.append(msg_idx)
                        break

    # ---- Phase 1b: semantic compression of tool_result contents (Phase 2) ----
    compress_stats_list = []
    if PROXY_COMPRESS_ENABLED:
        for msg_idx, block_idx in all_tool_result_indices:
            if frozen_head > 0 and msg_idx < frozen_head:
                continue
            block = messages[msg_idx]["content"][block_idx]
            content = block.get("content", "")
            if not content:
                continue
            # Guess mime hint from tool name when possible.
            mime_hint = None
            tool_use_id = block.get("tool_use_id", "")
            for m_idx in range(msg_idx - 1, -1, -1):
                m = messages[m_idx]
                if m.get("role") == "assistant":
                    c = m.get("content", "")
                    if isinstance(c, list):
                        for b in c:
                            if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                                tool_name = b.get("name", "")
                                if tool_name == "Read":
                                    inp = b.get("input", {})
                                    if isinstance(inp, dict):
                                        fp = inp.get("file_path", inp.get("path", ""))
                                        if fp:
                                            mime_hint = fp.lower().split(".")[-1] if "." in fp else None
                                break
                    break

            result = compress_tool_result(content, mime_hint=mime_hint)
            if result["ratio"] < 1.0:
                block["content"] = result["compressed"]
                compress_stats_list.append({
                    "msg_idx": msg_idx,
                    "block_idx": block_idx,
                    "content_type": result["content_type"],
                    "strategy": result["strategy"],
                    "ratio": result["ratio"],
                    "audit_pass": result["audit_pass"],
                    "original_len": len(result["original"]),
                    "compressed_len": len(result["compressed"]),
                })

    # ---- Phase 2a: tool-result clearing logic ----
    clear_stats = {"enabled": False, "skipped": True, "reason": "disabled"}
    cleared_files = []
    if PROXY_CLEAR_ENABLED and total_chars >= PROXY_CLEAR_THRESHOLD:
        # Filter to dynamic zone
        if frozen_head > 0:
            tool_result_indices = [(mi, bi) for mi, bi in all_tool_result_indices if mi >= frozen_head]
        else:
            tool_result_indices = list(all_tool_result_indices)

        # Apply zone-pct filter
        if clear_zone_pct is not None and clear_zone_pct < 1.0 and tool_result_indices:
            eligible_count = max(1, int(len(tool_result_indices) * clear_zone_pct))
            tool_result_indices = tool_result_indices[:eligible_count]
            # log handled by caller

        # Reduce frozen if too few
        _frozen = frozen_head
        if len(tool_result_indices) <= PROXY_TOOL_KEEP and _frozen > 0:
            _frozen = max(0, _frozen // 2)
            tool_result_indices = [(mi, bi) for mi, bi in all_tool_result_indices if mi >= _frozen]

        if len(tool_result_indices) > PROXY_TOOL_KEEP:
            keep = PROXY_TOOL_KEEP
            if tools_list is not None:
                has_agent = any(t == "Agent" or t == "EnterPlanMode" for t in tools_list)
                if not has_agent and len(tools_list) > 0:
                    keep = max(PROXY_TOOL_KEEP, 15)

            total_tr = len(tool_result_indices)
            recent_cutoff = max(0, total_tr - 6)
            keep_positions = set()
            cleared_files_set = set()

            # Score and keep
            scored = []
            for idx_pos, (msg_idx, block_idx) in enumerate(tool_result_indices):
                block = messages[msg_idx]["content"][block_idx]
                tool_use_id = block.get("tool_use_id", "")
                content_str = str(block.get("content", ""))
                score = 0
                tool_name = ""
                for m_idx in range(msg_idx - 1, -1, -1):
                    m = messages[m_idx]
                    if m.get("role") == "assistant":
                        c = m.get("content", "")
                        if isinstance(c, list):
                            for b in c:
                                if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                                    tool_name = b.get("name", "")
                                    break
                        if tool_name:
                            break
                score += TOOL_SEMANTIC_PRIORITY.get(tool_name, 1)
                for pat, pts in TOOL_RESULT_HIGH_VALUE_PATTERNS:
                    if pat.search(content_str[:500]):
                        score += pts
                if tool_name == "Read" and idx_pos >= recent_cutoff:
                    score += 5
                if "[System:" in content_str and any(kw in content_str for kw in ("未发生变化", "文件不存在", "参数错误")):
                    score += 10
                scored.append((score, idx_pos, msg_idx, block_idx, tool_name, content_str))

            scored.sort(key=lambda x: (-x[0], -x[1]))
            keep_positions = set(x[1] for x in scored[:keep])

            # Apply clearing
            cleared_count = 0
            cleared_chars = 0
            for idx_pos, (msg_idx, block_idx) in enumerate(tool_result_indices):
                if idx_pos in keep_positions:
                    continue
                block = messages[msg_idx]["content"][block_idx]
                original = block.get("content", "")
                original_len = len(str(original)) if original else 0
                tool_use_id = block.get("tool_use_id", "")
                # Extract meta_info
                meta_info = ""
                for m_idx in range(msg_idx - 1, -1, -1):
                    m = messages[m_idx]
                    if m.get("role") == "assistant":
                        c = m.get("content", "")
                        if isinstance(c, list):
                            for b in c:
                                if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                                    inp = b.get("input", {})
                                    if isinstance(inp, dict):
                                        fp = inp.get("file_path", inp.get("path", ""))
                                        cmd = inp.get("command", "")
                                        if fp:
                                            meta_info = f" file={fp}"
                                            cleared_files_set.add(fp)
                                        elif cmd:
                                            meta_info = f" cmd={cmd[:60]}"
                                    break
                        if meta_info:
                            break
                # Tool name for summary
                tool_name = ""
                for m_idx in range(msg_idx - 1, -1, -1):
                    m = messages[m_idx]
                    if m.get("role") == "assistant":
                        c = m.get("content", "")
                        if isinstance(c, list):
                            for b in c:
                                if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                                    tool_name = b.get("name", "")
                                    break
                        if tool_name:
                            break
                summary = _generate_tool_summary(tool_name, meta_info)
                if tool_name == "Read" and PROXY_REREAD_PREVIEW_CHARS > 0:
                    preview = str(original)[:PROXY_REREAD_PREVIEW_CHARS]
                    if len(str(original)) > PROXY_REREAD_PREVIEW_CHARS:
                        preview += "..."
                    block["content"] = f"[cleared: {summary}]\n{preview}"
                else:
                    block["content"] = f"[cleared: {summary}]"
                cleared_count += 1
                cleared_chars += original_len

            cleared_files = list(cleared_files_set)
            clear_stats = {
                "enabled": True, "cleared": True,
                "cleared_tool_results": cleared_count,
                "cleared_chars": cleared_chars, "kept": keep,
                "cleared_files": cleared_files,
                "total_chars_before": total_chars,
                "frozen_used": _frozen,
            }
        else:
            clear_stats = {
                "enabled": True, "skipped": True,
                "reason": "few_tool_results",
                "count": len(tool_result_indices),
                "frozen_used": _frozen,
            }
    elif not PROXY_CLEAR_ENABLED:
        clear_stats = {"enabled": False}
    else:
        clear_stats = {"enabled": True, "skipped": True, "reason": "below_threshold", "chars": total_chars}

    # ---- Phase 2b: thinking block stripping ----
    think_stats = {"enabled": True, "skipped": True, "reason": "stage_skip"}
    if thinking_keep > 0 and thinking_indices:
        dynamic_thinking = [idx for idx in thinking_indices if idx >= frozen_head]
        if len(dynamic_thinking) > thinking_keep:
            keep_set = set(dynamic_thinking[-thinking_keep:])
            stripped = 0
            for idx in dynamic_thinking:
                if idx not in keep_set:
                    _strip_thinking_from_msg(messages[idx])
                    stripped += 1
            think_stats = {
                "enabled": True, "stripped": True,
                "stripped_count": stripped, "kept": thinking_keep,
                "total_thinking": len(thinking_indices),
                "frozen_thinking_count": len(thinking_indices) - len(dynamic_thinking),
            }
        elif dynamic_thinking:
            think_stats = {"enabled": True, "skipped": True, "reason": "few_dynamic_thinking",
                           "count": len(dynamic_thinking)}

    # Aggregate compression stats
    aggregated_compress_stats = {"enabled": False, "compressed_count": 0, "saved_chars": 0}
    if compress_stats_list:
        original_total = sum(s["original_len"] for s in compress_stats_list)
        compressed_total = sum(s["compressed_len"] for s in compress_stats_list)
        strategies = {}
        for s in compress_stats_list:
            strategies[s["strategy"]] = strategies.get(s["strategy"], 0) + 1
        aggregated_compress_stats = {
            "enabled": True,
            "compressed_count": len(compress_stats_list),
            "original_chars": original_total,
            "compressed_chars": compressed_total,
            "saved_chars": original_total - compressed_total,
            "ratio": round(compressed_total / original_total, 4) if original_total else 1.0,
            "strategies": strategies,
            "audit_failures": sum(1 for s in compress_stats_list if not s["audit_pass"]),
        }

    return messages, {"clear": clear_stats, "think": think_stats, "compress": aggregated_compress_stats}


def clear_old_tool_results(messages, tools_list=None, clear_zone_pct=None):
    """
    Legacy wrapper around _compress_content_pass.

    Originally a standalone 250-line implementation, now delegates to the
    unified single-pass compressor to eliminate duplication.  Preserves the
    original return signature (messages, flat_stats_dict) so existing unit
    tests and documentation continue to work.
    """
    total_chars = _estimate_message_chars(messages)
    stage_config = {
        "stage": "legacy",
        "total_chars": total_chars,
        "frozen_head": PROXY_FROZEN_HEAD,
        "clear_zone_pct": clear_zone_pct,
        "thinking_keep": 0,
        "truncate_rounds": None,
        "oom_safety": False,
    }
    messages, combined = _compress_content_pass(
        messages, tools_list=tools_list, stage_config=stage_config
    )
    clear_stats = combined.get("clear", {})
    # Map nested format back to the flat dict expected by legacy callers/tests
    stats = dict(clear_stats)
    stats.setdefault("high_prio", 0)
    stats.setdefault("dedup_bash", 0)
    stats.setdefault("dedup_chars_saved", 0)
    stats.setdefault("frozen_head", stage_config["frozen_head"])
    return messages, stats


def _compute_adaptive_rounds(messages, base_rounds):
    extra = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tc = b.get("content", "")
                        if isinstance(tc, str):
                            low = tc.lower()
                            if any(kw in low for kw in ["error", "exception", "failed", "traceback"]):
                                extra += 1
                                break
            elif isinstance(content, str):
                low = content.lower()
                if any(kw in low for kw in ["error", "exception", "failed", "traceback"]):
                    extra += 1
        elif role == "assistant":
            if isinstance(content, list):
                write_count = 0
                edit_count = 0
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        name = b.get("name", "")
                        if name in ("Write", "NotebookEdit"):
                            write_count += 1
                        elif name == "Edit":
                            edit_count += 1
                if write_count + edit_count > 2:
                    extra += 1
    adaptive = min(base_rounds + extra, base_rounds * 2)
    return adaptive


def _extract_middle_summary_rules(messages):
    errors_solutions = []
    code_changes = []
    decisions = []
    file_states = {}

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tc = b.get("content", "")
                        if isinstance(tc, str):
                            low = tc.lower()
                            if any(kw in low for kw in ["error", "exception", "failed", "traceback"]):
                                errors_solutions.append(tc[:500])
                            if "successfully" in low or "updated" in low or "created" in low:
                                errors_solutions.append(f"[resolved] {tc[:200]}")
        elif role == "assistant":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "tool_use":
                            name = b.get("name", "")
                            inp = b.get("input", {})
                            if isinstance(inp, dict):
                                fp = inp.get("file_path", inp.get("path", ""))
                                if fp:
                                    file_states[fp] = name
                                if name in ("Write", "Edit"):
                                    code_changes.append(f"{name}({fp})")
                        elif b.get("type") == "text":
                            txt = b.get("text", "")
                            if any(kw in txt for kw in ["DECISION", "TODO", "FIXME", "IMPORTANT", "NOTE"]):
                                decisions.append(txt[:200])

    parts = []
    if errors_solutions:
        parts.append("<errors_solutions>")
        for e in errors_solutions[:5]:
            parts.append(f"- {e}")
        parts.append("</errors_solutions>")
    if code_changes:
        parts.append("<code_changes>")
        for c in code_changes[:10]:
            parts.append(f"- {c}")
        parts.append("</code_changes>")
    if file_states:
        parts.append("<file_states>")
        for fp, op in sorted(file_states.items())[-10:]:
            parts.append(f"- {fp}: last {op}")
        parts.append("</file_states>")
    if decisions:
        parts.append("<decisions>")
        for d in decisions[:5]:
            parts.append(f"- {d}")
        parts.append("</decisions>")

    if not parts:
        return None
    header = f"[Compressed context from {len(messages)} earlier messages (rule-based):]"
    return header + "\n".join(parts)


# TODO(roadmap-U2): Phase-aware compression — detect exploration/implementation/debug stages
def _compress_middle_with_llm(messages, timeout=30):
    try:
        conversation_text = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            parts.append(b.get("text", "")[:300])
                        elif b.get("type") == "tool_use":
                            name = b.get("name", "")
                            inp = b.get("input", {})
                            parts.append(f"[tool:{name}({json.dumps(inp, ensure_ascii=False)[:200]})]")
                        elif b.get("type") == "tool_result":
                            tc = b.get("content", "")
                            if isinstance(tc, str):
                                parts.append(f"[result:{tc[:200]}]")
                text = " ".join(parts)
            elif isinstance(content, str):
                text = content[:300]
            else:
                continue
            conversation_text.append(f"{role}: {text}")

        conv_str = "\n".join(conversation_text)
        if len(conv_str) > 8000:
            conv_str = conv_str[:8000] + "...[truncated]"

        prompt = (
            "Summarize the following coding session into these XML sections. "
            "Be concise. Keep error messages verbatim. Keep file paths. Remove narration.\n\n"
            "<current_focus>What is being worked on (1-2 sentences)</current_focus>\n"
            "<errors_solutions>\n"
            "For each non-trivial error encountered, output ONE entry in this EXACT format:\n"
            "  - Error: <short verbatim error message or symptom>\n"
            "    Root cause: <why it happened — 1 sentence>\n"
            "    Fix: <what was done to resolve it — 1 sentence>\n"
            "    Avoidance: <what to verify next time to prevent recurrence — 1 sentence or 'N/A'>\n"
            "If no errors: output 'none'.\n"
            "</errors_solutions>\n"
            "<code_state>Current file states, key code signatures (function names, important constants)</code_state>\n"
            "<decisions>Architecture/design decisions and the reason behind each</decisions>\n"
            "<pending>Unfinished tasks, blockers, and what is needed to unblock each</pending>\n\n"
            f"Session log ({len(messages)} messages):\n{conv_str}"
        )

        payload = {
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
            "temperature": 0.3,
            "stream": False,
        }
        req_data = json.dumps(payload).encode("utf-8")
        with _llama_lock:
            req = urllib.request.Request(
                f"{LLAMA_BASE}/chat/completions",
                data=req_data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        text = ""
        for choice in result.get("choices", []):
            msg = choice.get("message", {})
            text += msg.get("content", "")
        if text.strip():
            return f"[Compressed context from {len(messages)} earlier messages (LLM):]\n{text.strip()}"
        return None
    except Exception as e:
        log(f"  -> LLM compression failed: {e}, falling back to rules")
        return None


_summary_cache = {}
_summary_cache_lock = threading.Lock()
_SUMMARY_CACHE_MAX_SESSIONS = 10
_SUMMARY_CACHE_MAX_CHARS = 3000


def _merge_summaries_with_llm(old_summary, new_summary, timeout=15):
    try:
        prompt = (
            "Merge these two session summaries into one concise summary. "
            "Keep all errors, file states, and decisions. Remove redundancy.\n\n"
            f"<previous_summary>\n{old_summary[:3000]}\n</previous_summary>\n\n"
            f"<new_summary>\n{new_summary[:3000]}\n</new_summary>"
        )
        payload = {
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 800,
            "temperature": 0.3,
            "stream": False,
        }
        req_data = json.dumps(payload).encode("utf-8")
        with _llama_lock:
            req = urllib.request.Request(
                f"{LLAMA_BASE}/chat/completions",
                data=req_data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        text = ""
        for choice in result.get("choices", []):
            msg = choice.get("message", {})
            text += msg.get("content", "")
        return text.strip() if text.strip() else old_summary + "\n\n" + new_summary
    except Exception as e:
        log(f"  -> Summary merge failed: {e}, concatenating")
        return old_summary + "\n\n" + new_summary


def _incremental_compress(dropped, session_id):
    with _summary_cache_lock:
        cache = _summary_cache.get(session_id)

    if cache and cache.get("last_compressed_msg_index", 0) > 0:
        new_start = min(cache["last_compressed_msg_index"], len(dropped))
        new_dropped = dropped[new_start:]
        if len(new_dropped) >= 5:
            new_summary = _compress_middle_with_llm(new_dropped, timeout=30)
            if not new_summary:
                new_summary = _extract_middle_summary_rules(new_dropped)
        else:
            new_summary = _extract_middle_summary_rules(new_dropped) if new_dropped else None

        if new_summary:
            combined = cache["summary"] + "\n\n" + new_summary
            if len(combined) > _SUMMARY_CACHE_MAX_CHARS:
                combined = _merge_summaries_with_llm(cache["summary"], new_summary)
            compressed_text = combined
        else:
            compressed_text = cache["summary"]
    else:
        if len(dropped) >= 10:
            compressed_text = _compress_middle_with_llm(dropped, timeout=30)
        else:
            compressed_text = None
        if not compressed_text:
            compressed_text = _extract_middle_summary_rules(dropped)
        if not compressed_text:
            return None, None

    with _summary_cache_lock:
        if len(_summary_cache) >= _SUMMARY_CACHE_MAX_SESSIONS:
            oldest_key = next(iter(_summary_cache))
            del _summary_cache[oldest_key]
        _summary_cache[session_id] = {
            "last_compressed_msg_index": len(dropped),
            "summary": compressed_text[:_SUMMARY_CACHE_MAX_CHARS],
        }

    return compressed_text, cache is not None


def _is_tool_result_message(msg):
    """Return True iff `msg` is a user message whose content contains at
    least one `tool_result` block. Used by the smart strategy to classify
    messages that should never be dropped/compressed (their content is the
    high-value file/exec output the model will re-read if lost)."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content", [])
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def _compress_assistant_message(msg):
    """Return a copy of an assistant message with non-tool_use text blocks
    replaced by a fixed placeholder. Tool_use blocks are kept verbatim
    because the model's subsequent tool_result depends on the tool name +
    args. The fixed placeholder text preserves prefix-cache stability."""
    content = msg.get("content", [])
    if isinstance(content, list):
        compressed_blocks = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                compressed_blocks.append(b)
            else:
                compressed_blocks.append({"type": "text", "text": "[reasoning omitted]"})
        return {**msg, "content": compressed_blocks}
    if isinstance(content, str):
        return {**msg, "content": "[reasoning omitted]"}
    return msg


def _apply_smart_truncation(messages, budget_chars=None, session_id=None):
    """Phase 2 改进2 (proxy-truncation-agent-scenario.md): role+content-aware
    truncation. Preserves high-value content (system, tool_result, recent
    user turns) verbatim; compresses assistant reasoning text into a fixed
    placeholder. Iterates from newest to oldest, fitting each message into
    the char budget and falling back to a compressed form for assistant
    messages when the original would overflow.

    Priority (high → low):
      1. system messages           — always kept
      2. tool_result messages      — always kept (file contents survive)
      3. user messages (no tool)   — newest first, then older
      4. assistant messages        — newest first; reasoning compressed
                                     if original doesn't fit, dropped if
                                     compressed form still doesn't fit

    Returns (result_messages, stats_dict). The stats dict has:
      strategy='smart', truncated, dropped_messages, kept_messages,
      compressed_assistants, kept_chars, budget_chars
    """
    if budget_chars is None:
        budget_chars = PROXY_CHARS_EXPANSION

    total_input_chars = _estimate_message_chars(messages)
    # Below budget: nothing to do.
    if total_input_chars <= budget_chars:
        return messages, {
            "enabled": True,
            "strategy": "smart",
            "skipped": True,
            "reason": "below_budget",
            "chars": total_input_chars,
            "budget_chars": budget_chars,
        }

    # Step 1: classify. Use id-based sets to avoid relying on dict equality
    # (Anthropic SDK message dicts are not hashable, so `m in system`
    # would also be O(n²) and unreliable for nested mutations).
    system_msgs = [m for m in messages if m.get("role") == "system"]
    system_ids = {id(m) for m in system_msgs}
    tool_result_msgs = [m for m in messages if id(m) not in system_ids and _is_tool_result_message(m)]
    tool_result_ids = {id(m) for m in tool_result_msgs}
    other_msgs = [m for m in messages
                  if id(m) not in system_ids and id(m) not in tool_result_ids]

    # Step 2: always keep system + tool_result; measure their cost.
    kept = list(system_msgs) + list(tool_result_msgs)
    kept_chars = _estimate_message_chars(kept)

    # If the must-keep set alone exceeds budget, we still have to keep
    # them (otherwise the model would lose file contents and trigger a
    # re-read loop). The caller is expected to catch this with the
    # OOM_SAFE hard ceiling before reaching here.
    if kept_chars > budget_chars:
        return kept, {
            "enabled": True,
            "strategy": "smart",
            "truncated": True,
            "dropped_messages": len(other_msgs),
            "kept_messages": len(kept),
            "compressed_assistants": 0,
            "kept_chars": kept_chars,
            "budget_chars": budget_chars,
            "reason": "must_keep_exceeds_budget",
        }

    # Step 3: walk other_msgs newest-first. For each message, try to keep
    # it as-is. If it doesn't fit and is an assistant, try the compressed
    # form. Otherwise drop.
    compressed_count = 0
    dropped_count = 0
    # Insert in chronological order, so we prepend and reverse at the end.
    chosen = []
    for msg in reversed(other_msgs):
        msg_chars = _estimate_message_chars([msg])
        if kept_chars + msg_chars <= budget_chars:
            chosen.append(msg)
            kept_chars += msg_chars
            continue
        if msg.get("role") == "assistant":
            compressed = _compress_assistant_message(msg)
            comp_chars = _estimate_message_chars([compressed])
            if kept_chars + comp_chars <= budget_chars:
                chosen.append(compressed)
                kept_chars += comp_chars
                compressed_count += 1
                continue
        dropped_count += 1

    # Reverse `chosen` to restore chronological order, then assemble.
    chosen.reverse()
    result = kept + chosen
    return result, {
        "enabled": True,
        "strategy": "smart",
        "truncated": dropped_count > 0 or compressed_count > 0,
        "dropped_messages": dropped_count,
        "kept_messages": len(result),
        "compressed_assistants": compressed_count,
        "kept_chars": kept_chars,
        "budget_chars": budget_chars,
    }


def truncate_messages_if_needed(messages, session_id=None, keep_rounds=None):
    """
    Proxy-side message truncation with dual strategy support.

    Strategy 'char' (default): drop old messages until total chars fall below
    PROXY_CTX_CHARS_LIMIT. Preserves head + tail window.

    Strategy 'rounds': keep only the most recent N assistant rounds,
    replacing dropped messages with a lightweight placeholder.
    When keep_rounds is provided (from lifecycle stage config), it overrides
    the default adaptive_rounds computation.

    Char-based budget: uses PROXY_CHARS_EXPANSION (chars) as the unified
    trigger threshold, replacing the old token-budget PROXY_CTX_TOKEN_BUDGET.
    Operates on Anthropic-format messages in-place.
    Returns (messages, stats_dict).
    """
    if not PROXY_CTX_LIMIT_ENABLED and PROXY_CTX_TRUNCATE_STRATEGY != "rounds":
        return messages, {"enabled": False}

    # ---------- rounds strategy ----------
    if PROXY_CTX_TRUNCATE_STRATEGY == "rounds":
        # keep_rounds=None: stage says skip truncation entirely
        if keep_rounds is None:
            return messages, {"enabled": True, "strategy": "rounds", "skipped": True, "reason": "stage_skip"}
        total_chars = _estimate_message_chars(messages)
        # Char-based budget check: skip if within PROXY_CHARS_EXPANSION
        if total_chars <= PROXY_CHARS_EXPANSION:
            return messages, {
                "enabled": True, "strategy": "rounds", "skipped": True,
                "reason": "below_budget",
                "chars": total_chars,
                "budget_chars": PROXY_CHARS_EXPANSION,
            }

        # Use stage-config keep_rounds if provided, else adaptive.
        # P0 fix: both branches iterate down from the initial rounds to fit
        # the char budget. Previously the stage-specified branch did a single
        # pass and returned even if the result was still over budget — which
        # let oversized agent sessions (50+ msgs/round, 200+ rounds) leak
        # through with hundreds of thousands of chars.
        min_rounds = 2
        if keep_rounds is not None:
            adaptive_rounds = keep_rounds
            for rounds in range(keep_rounds, min_rounds - 1, -1):
                result, stats = _apply_rounds_truncation(messages, rounds, session_id=session_id)
                if not stats.get("truncated"):
                    return result, stats
                result_chars = _estimate_message_chars(result)
                if result_chars <= PROXY_CHARS_EXPANSION or rounds == min_rounds:
                    stats["chars"] = result_chars
                    stats["budget_chars"] = PROXY_CHARS_EXPANSION
                    stats["actual_keep_rounds"] = rounds
                    stats["stage_keep_rounds"] = keep_rounds
                    stats["adaptive_rounds"] = adaptive_rounds
                    stats["budget_iterations"] = keep_rounds - rounds
                    return result, stats
        else:
            # Backward-compatible: adaptive rounds + LLM/rule compression
            adaptive_rounds = _compute_adaptive_rounds(messages, PROXY_CTX_KEEP_ROUNDS)
            for rounds in range(adaptive_rounds, min_rounds - 1, -1):
                result, stats = _apply_rounds_truncation(messages, rounds, session_id=session_id)
                if not stats.get("truncated"):
                    return result, stats
                result_chars = _estimate_message_chars(result)
                if result_chars <= PROXY_CHARS_EXPANSION or rounds == min_rounds:
                    stats["chars"] = result_chars
                    stats["budget_chars"] = PROXY_CHARS_EXPANSION
                    stats["actual_keep_rounds"] = rounds
                    stats["adaptive_rounds"] = adaptive_rounds
                    return result, stats

        return messages, {"enabled": True, "strategy": "rounds", "skipped": True, "reason": "no_reduction"}

    # ---------- fifo strategy ----------
    if PROXY_CTX_TRUNCATE_STRATEGY == "fifo":
        n = len(messages)
        keep_total = PROXY_CTX_KEEP_MESSAGES
        if n <= keep_total:
            return messages, {
                "enabled": True, "strategy": "fifo", "skipped": True,
                "reason": "below_limit", "count": n, "limit": keep_total,
            }

        head = messages[:PROXY_CTX_KEEP_HEAD]
        tail_count = keep_total - PROXY_CTX_KEEP_HEAD
        tail = messages[-tail_count:]
        dropped = messages[PROXY_CTX_KEEP_HEAD : n - tail_count]
        dropped_count = len(dropped)

        # Count tools in dropped messages
        tool_count = 0
        for m in dropped:
            if m.get("role") == "assistant":
                content = m.get("content", [])
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tool_count += 1
                elif isinstance(content, dict) and content.get("type") == "tool_use":
                    tool_count += 1

        # Extract file mentions from dropped messages
        file_mentions = set()
        for m in dropped:
            if m.get("role") == "assistant":
                content = m.get("content", [])
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            args = ""
                            fn = b.get("function") or {}
                            if isinstance(fn, dict):
                                args = fn.get("arguments", "")
                            if not args:
                                args = b.get("input", "")
                            if isinstance(args, dict):
                                args = json.dumps(args)
                            if isinstance(args, str):
                                for pat in [r'"path":\s*"([^"]+)"', r'"file":\s*"([^"]+)"',
                                            r'"filePath":\s*"([^"]+)"', r'"directory":\s*"([^"]+)"']:
                                    file_mentions.update(re.findall(pat, args))

        file_info = f" Files: {', '.join(sorted(file_mentions)[:10])}." if file_mentions else ""

        # Plan 1 (prefix-cache fix): the placeholder text MUST be byte-for-byte
        # identical across requests. Including dropped_count / tool_count /
        # file_mentions here changes the text every request, breaking the
        # cache at the placeholder boundary and dropping hit rate to 0%.
        # Dynamic info is still kept in the stats dict below (used for
        # proxy_metrics.jsonl) — it just doesn't leak into the prompt.
        compressed_text = "[Context folded: earlier messages omitted.]"

        if tail and tail[0].get("role") == "user":
            tail_content = tail[0].get("content", [])
            summary_block = {"type": "text", "text": compressed_text}
            if isinstance(tail_content, list):
                tail[0]["content"] = [summary_block] + tail_content
            else:
                tail[0]["content"] = [summary_block, {"type": "text", "text": str(tail_content)}]
            result = head + tail
        else:
            summary = {"role": "user", "content": [{"type": "text", "text": compressed_text}]}
            result = head + [summary] + tail

        return result, {
            "enabled": True,
            "strategy": "fifo",
            "truncated": True,
            "dropped_messages": dropped_count,
            "kept_messages": len(result),
            "tool_count": tool_count,
            "file_mentions": len(file_mentions),
        }

    # ---------- smart strategy (Phase 2 改进2) ----------
    # Role+content-aware truncation. Keeps system + tool_result verbatim
    # (preserves file contents to avoid re-read loops), then keeps newer
    # user/assistant messages in reverse-chronological order until the
    # PROXY_CHARS_EXPANSION budget is filled. Assistant messages that
    # don't fit are first attempted in compressed form (tool_use blocks
    # kept, reasoning text replaced by a stable placeholder) before being
    # dropped entirely.
    if PROXY_CTX_TRUNCATE_STRATEGY == "smart":
        return _apply_smart_truncation(
            messages, budget_chars=PROXY_CHARS_EXPANSION, session_id=session_id,
        )

    # ---------- char strategy (and any other unhandled strategy) ----------
    # Falls back to no-op truncation when strategy is "char" or anything other
    # than rounds/fifo/smart. The actual char-window implementation lives
    # (misnamed) inside _apply_rounds_truncation above and is currently only
    # invoked through that path. Returning a no-op stats dict here prevents
    # the caller from hitting `TypeError: cannot unpack non-iterable
    # NoneType object`.
    return messages, {
        "enabled": True,
        "strategy": PROXY_CTX_TRUNCATE_STRATEGY,
        "skipped": True,
        "reason": "char_strategy_uses_noop_fallback",
    }


def _detect_blocker_pattern(messages):
    """
    Walk messages backward and detect a tail of consecutive same-error-type
    tool_result rejections. Stops at the first non-error tool_result, the first
    plain user text message, or when the run length drops to zero.

    Returns a dict:
        {
          "triggered": bool,
          "tool_name": str,            # last assistant tool_use name in the run
          "error_type": str,           # wasted | file_not_found | input_validation
          "run_length": int,           # count of consecutive errors at the tail
        }
    or {"triggered": False} when no run meets the threshold.

    Detection is based on the rewritten content produced by the error-
    translation pass (which substitutes the original tool_result with a fixed
    Chinese system message). This means the detector is best-effort: it relies
    on the upstream pass having run, and on the marker substrings matching.
    """
    if not PROXY_BLOCKER_ENABLED:
        return {"triggered": False, "reason": "disabled"}

    last_tool_name = None
    run = []  # list of (tool_name, error_type), index 0 = oldest in run

    for msg in reversed(messages):
        role = msg.get("role")
        if role == "assistant":
            content = msg.get("content", "")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        last_tool_name = b.get("name", "unknown")
                        break
            continue
        if role != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            # Plain user text breaks the consecutive-error tail.
            break
        tool_result_block = None
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tool_result_block = b
                break
        if tool_result_block is None:
            # User message with no tool_result breaks the tail (e.g. a text turn).
            break
        tc = str(tool_result_block.get("content", ""))
        tc_lower = tc.lower()
        matched = None
        for err_type, markers in _BLOCKER_ERROR_MARKERS:
            for m in markers:
                if m in tc_lower:
                    matched = err_type
                    break
            if matched:
                break
        if matched is None:
            # Non-error tool_result breaks the run.
            break
        if run and run[-1][1] != matched:
            # Different error type from the most recent in the run: treat
            # the run as broken (the model is hitting two different
            # problems, not a single recurring one).
            break
        run.append((last_tool_name or "unknown", matched))

    run_length = len(run)
    if run_length < PROXY_BLOCKER_THRESHOLD:
        return {"triggered": False, "run_length": run_length, "threshold": PROXY_BLOCKER_THRESHOLD}

    tool_name, error_type = run[-1]  # most recent
    return {
        "triggered": True,
        "tool_name": tool_name,
        "error_type": error_type,
        "run_length": run_length,
    }


def _build_blocker_message(tool_name, error_type, run_length):
    """
    Construct a [BLOCKER] user message to inject into the tail when a tool
    has been failing the same way repeatedly. Text is intentionally short and
    identical for the same (tool_name, error_type) pair, so prefix cache hits
    are preserved across requests.
    """
    return {
        "role": "user",
        "content": [{
            "type": "text",
            "text": (
                f"[BLOCKER] The tool '{tool_name}' has failed with the same "
                f"error ({error_type}) {run_length} times in a row. "
                f"Do NOT call '{tool_name}' again with similar arguments. "
                f"Either switch to a different tool, change your approach, "
                f"or report this blocker to the user."
            ),
        }],
    }


def _build_tool_use_map(messages):
    """Build a mapping from tool_use_id to tool name across all messages."""
    tool_map = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tool_map[b.get("id", "")] = b.get("name", "")
    return tool_map


def _apply_rounds_truncation(messages, keep_rounds, session_id=None):
    head = messages[:PROXY_CTX_KEEP_HEAD]

    tail = []
    assistant_count = 0
    for msg in reversed(messages):
        tail.insert(0, msg)
        if msg.get("role") == "assistant":
            assistant_count += 1
        if assistant_count >= keep_rounds:
            break

    dropped_count = len(messages) - len(head) - len(tail)
    if dropped_count <= 0:
        return messages, {"enabled": True, "strategy": "rounds", "skipped": True}

    dropped = messages[PROXY_CTX_KEEP_HEAD : len(messages) - len(tail)]

    # NEW: Preserve Read tool_results from the dropped zone to prevent re-read loops.
    # Read tool_results contain file contents that LLM summaries cannot replace.
    tool_map = _build_tool_use_map(messages)
    read_results = []
    remaining_dropped = []
    for m in dropped:
        is_read_result = False
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                for b in content:
                    if b.get("type") == "tool_result":
                        if tool_map.get(b.get("tool_use_id", "")) == "Read":
                            is_read_result = True
                            break
        if is_read_result:
            read_results.append(m)
        else:
            remaining_dropped.append(m)

    dropped = remaining_dropped
    dropped_count = len(dropped)

    tool_count = 0
    for m in dropped:
        if m.get("role") == "assistant":
            content = m.get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_count += 1
            elif isinstance(content, dict) and content.get("type") == "tool_use":
                tool_count += 1

    file_mentions = set()
    for m in dropped:
        if m.get("role") == "assistant":
            content = m.get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        args = ""
                        fn = b.get("function") or {}
                        if isinstance(fn, dict):
                            args = fn.get("arguments", "")
                        if not args:
                            args = b.get("input", "")
                        if isinstance(args, dict):
                            args = json.dumps(args)
                        if isinstance(args, str):
                            for pat in [r'"path":\s*"([^"]+)"', r'"file":\s*"([^"]+)"',
                                        r'"filePath":\s*"([^"]+)"', r'"directory":\s*"([^"]+)"']:
                                file_mentions.update(re.findall(pat, args))

    file_info = f" Files previously accessed: {', '.join(sorted(file_mentions)[:10])}." if file_mentions else ""

    compressed_text = None
    cache_hit = False
    if session_id:
        compressed_text, cache_hit = _incremental_compress(dropped, session_id)
    if not compressed_text:
        if dropped_count >= 10:
            compressed_text = _compress_middle_with_llm(dropped, timeout=30)
        if not compressed_text:
            compressed_text = _extract_middle_summary_rules(dropped)
    if not compressed_text:
        compressed_text = (
            f"[Context folded: {dropped_count} earlier messages omitted. "
            f"Previous work included {tool_count} tool interactions."
            f"{file_info} "
            f"Retaining last {keep_rounds} conversation rounds.]"
        )

    if PROXY_HISTORY_INDEX == "rule" and dropped_count >= 5:
        keywords = _extract_keywords(dropped)
        keyword_ctx = _inject_keyword_context(
            keywords, tail,
            top_k=PROXY_HISTORY_TOP_K,
            max_chars=PROXY_HISTORY_MAX_CHARS,
        )
        if keyword_ctx:
            compressed_text += "\n\n" + keyword_ctx

    # Assemble result with preserved Read results inserted between summary and tail
    result = list(head)

    if compressed_text:
        if tail and tail[0].get("role") == "user":
            # Copy first tail msg to avoid mutating original messages list
            modified_tail0 = dict(tail[0])
            tail_content = modified_tail0.get("content", [])
            summary_block = {"type": "text", "text": compressed_text}
            if isinstance(tail_content, list):
                modified_tail0["content"] = [summary_block] + list(tail_content)
            else:
                modified_tail0["content"] = [summary_block, {"type": "text", "text": str(tail_content)}]
            result.append(modified_tail0)
            result.extend(tail[1:])
        else:
            summary = {"role": "user", "content": [{"type": "text", "text": compressed_text}]}
            result.append(summary)
            result.extend(tail)
    else:
        result.extend(tail)

    # Insert preserved Read results between head/summary and tail
    tail_start_in_result = len(head)
    if not (tail and tail[0].get("role") == "user") and compressed_text:
        tail_start_in_result += 1
    if read_results:
        result = result[:tail_start_in_result] + read_results + result[tail_start_in_result:]

    return result, {
        "enabled": True,
         "strategy": "rounds",
         "truncated": True,
         "dropped_messages": dropped_count,
         "kept_messages": len(result),
         "tool_count": tool_count,
         "file_mentions": len(file_mentions),
         "compression": "llm" if "LLM" in compressed_text else ("rules" if "rule-based" in compressed_text else "folded"),
     }


# ---------------------------------------------------------------------------
# Thinking/reasoning block stripping: remove old assistant thinking content
# to reduce context size. Operates defensively since current clients rarely
# send explicit thinking blocks (reasoning is usually inline text).
# ---------------------------------------------------------------------------
def _has_thinking_content(msg):
    """Check if an assistant message contains thinking/reasoning content."""
    content = msg.get("content", "")
    if isinstance(content, list):
        for block in content:
            if block.get("type") == "thinking":
                return True
            if block.get("type") == "text":
                text = block.get("text", "")
                if "<thinking>" in text or "</thinking>" in text:
                    return True
    elif isinstance(content, str):
        if "<thinking>" in content or "</thinking>" in content:
            return True
    return False


def _strip_thinking_from_msg(msg):
    """Remove thinking content from a message (in-place)."""
    content = msg.get("content", "")
    if isinstance(content, list):
        new_content = []
        for block in content:
            if block.get("type") == "thinking":
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
                text = text.strip()
                if text:
                    new_content.append({"type": "text", "text": text})
            else:
                new_content.append(block)
        msg["content"] = new_content
    elif isinstance(content, str):
        content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL).strip()
        msg["content"] = content


def strip_old_thinking_blocks(messages, keep_recent=3, frozen_head=None):
    """
    Remove thinking/reasoning content from old assistant messages.
    Keeps the most recent keep_recent assistant messages with thinking intact.
    Set keep_recent=0 to skip thinking stripping entirely (INIT/GROWTH stages).
    When frozen_head > 0, the first N messages are scanned but their thinking
    blocks are NEVER stripped — this preserves prefix KV cache stability.
    Only thinking blocks in the dynamic zone (messages frozen_head+) are
    eligible for stripping (oldest dynamic thinking stripped first).
    Returns (messages, stats_dict).
    """
    if not messages:
        return messages, {"enabled": False}
    if frozen_head is None:
        frozen_head = PROXY_FROZEN_HEAD

    # keep_recent=0 means skip entirely (lightweight stages)
    if keep_recent <= 0:
        return messages, {"enabled": True, "skipped": True, "reason": "stage_skip", "keep_recent": 0}

    # Find all assistant messages with thinking, but only strip in dynamic zone
    thinking_indices = []
    for idx, msg in enumerate(messages):
        if msg.get("role") == "assistant" and _has_thinking_content(msg):
            thinking_indices.append(idx)

    if not thinking_indices:
        return messages, {"enabled": True, "skipped": True, "reason": "no_thinking_found"}

    # Filter to dynamic zone indices (those in frozen zone are protected)
    dynamic_thinking = [idx for idx in thinking_indices if idx >= frozen_head]
    frozen_thinking = [idx for idx in thinking_indices if idx < frozen_head]

    if len(dynamic_thinking) <= keep_recent:
        return messages, {
            "enabled": True,
            "skipped": True,
            "reason": "few_dynamic_thinking",
            "count": len(dynamic_thinking),
            "frozen_thinking_count": len(frozen_thinking),
            "frozen_head": frozen_head,
        }

    # Keep the most recent `keep_recent` dynamic thinking messages
    keep_set = set(dynamic_thinking[-keep_recent:])
    stripped_count = 0
    for idx in dynamic_thinking:
        if idx in keep_set:
            continue
        _strip_thinking_from_msg(messages[idx])
        stripped_count += 1

    return messages, {
        "enabled": True,
        "stripped": True,
        "stripped_count": stripped_count,
        "kept": keep_recent,
        "total_thinking": len(thinking_indices),
        "frozen_thinking_count": len(frozen_thinking),
        "frozen_head": frozen_head,
    }


# ---------------------------------------------------------------------------
def convert_anthropic_messages_to_openai(messages):
    """Convert Anthropic message format to OpenAI message format."""
    openai_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            # Complex content with tool_use / tool_result
            text_parts = []
            tool_calls = []
            tool_results = []

            for block in content:
                block_type = block.get("type", "")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    tool_input = block.get("input", {})
                    if tool_input is None:
                        tool_input = {}
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(tool_input) if isinstance(tool_input, dict) else (tool_input if isinstance(tool_input, str) else "{}"),
                        }
                    })
                elif block_type == "tool_result":
                    tr_content = block.get("content", "")
                    if tr_content is None:
                        tr_content = ""
                    tool_results.append({
                        "tool_call_id": block.get("tool_use_id", ""),
                        "role": "tool",
                        "content": tr_content,
                    })

            if role == "assistant" and tool_calls:
                openai_msg = {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else None,
                    "tool_calls": tool_calls,
                }
                if not openai_msg["content"]:
                    del openai_msg["content"]
                openai_messages.append(openai_msg)
            elif role == "user" and tool_results:
                # Tool results MUST come immediately after the assistant
                # tool_calls that triggered them. OpenAI/DeepSeek strictly
                # validate that every tool_calls message is followed by tool
                # messages (one per tool_call_id) before any other role.
                # Inserting a user text message here would break that pairing
                # ("insufficient tool messages following tool_calls").
                # So: emit tool results first, then any trailing text.
                for tr in tool_results:
                    tr_content = tr["content"]
                    if tr_content is None:
                        tr_content = ""
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": str(tr_content),
                    })
                if text_parts:
                    openai_messages.append({
                        "role": "user",
                        "content": "\n".join(text_parts),
                    })
            else:
                openai_messages.append({
                    "role": role,
                    "content": "\n".join(text_parts) if text_parts else json.dumps(content),
                })
        else:
            openai_messages.append({
                "role": role,
                "content": str(content) if content else "",
            })

    return openai_messages


def convert_openai_response_to_anthropic(openai_resp, anthropic_model):
    """Convert OpenAI response to Anthropic response format."""
    choice = openai_resp["choices"][0]
    msg = choice["message"]
    content_text = msg.get("content", "") or ""
    reasoning = msg.get("reasoning_content", "")

    # Qwen3.6 fix: if content is empty but reasoning exists, use reasoning as content
    if not content_text.strip() and reasoning:
        content_text = reasoning.strip()

    content = []
    existing_tool_calls = msg.get("tool_calls") or []
    synthesized = False

    # Content-text fallback for Qwen2.5-Coder: <tools>{...}</tools> in plain text.
    # Only fires when no structured tool_calls were returned (structured wins).
    extracted = _extract_content_tool_calls(content_text)
    if extracted["tools"] and not existing_tool_calls:
        if extracted["text"]:
            content.append({"type": "text", "text": extracted["text"]})
        for t in extracted["tools"]:
            content.append({
                "type": "tool_use",
                "id": f"call_{os.urandom(8).hex()}",
                "name": t["name"],
                "input": t["arguments"],
            })
        synthesized = True
    elif content_text:
        content.append({"type": "text", "text": content_text})

    # Handle structured tool_calls -> tool_use
    for tc in existing_tool_calls:
        if tc.get("type") == "function":
            func = tc["function"]
            tool_name = func.get("name", "")
            raw_args = func.get("arguments", "{}")
            input_data = parse_tool_arguments(raw_args, tool_name)
            # Ensure tool_call id is present (some backends omit it)
            tc_id = tc.get("id", "") or f"call_{os.urandom(8).hex()}"
            content.append({
                "type": "tool_use",
                "id": tc_id,
                "name": tool_name,
                "input": input_data,
            })

    stop_reason = choice.get("finish_reason", "stop")
    anthropic_stop_reason = "end_turn"
    if stop_reason == "tool_calls":
        anthropic_stop_reason = "tool_use"
    elif stop_reason == "length":
        anthropic_stop_reason = "max_tokens"
    elif stop_reason == "stop":
        anthropic_stop_reason = "end_turn"
    # Override when we synthesized tool_use from content fallback
    if synthesized and anthropic_stop_reason != "max_tokens":
        anthropic_stop_reason = "tool_use"

    return {
        "id": f"msg_{openai_resp['id'][:16]}",
        "type": "message",
        "role": "assistant",
        "model": anthropic_model,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_resp.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_resp.get("usage", {}).get("completion_tokens", 0),
        },
        "content": content,
        "stop_reason": anthropic_stop_reason,
    }


# ---------------------------------------------------------------------------
# Status page: system monitoring dashboard
# ---------------------------------------------------------------------------
import subprocess
import time

_LOG_PATH = os.path.join(_SCRIPT_DIR, "logs", "llama-server.log")
_PID_PATH = os.path.join(_SCRIPT_DIR, "llama-server.pid")


def _run(cmd, timeout=3):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=timeout).strip()
    except Exception:
        return ""


def _get_process_info(pattern, name, fallback_port=None):
    """Return dict with pid, rss_mb, cpu, elapsed for a process matching pattern."""
    # Try pgrep first
    pid = _run(f"pgrep -f '{pattern}' | head -1")
    # Fallback: detect by listening port (for proxy itself)
    # Use -sTCP:LISTEN to only match the listening process, not client connections
    if not pid and fallback_port:
        pid = _run(f"lsof -i :{fallback_port} -sTCP:LISTEN -t | head -1")
    if not pid:
        return {"running": False, "name": name}
    info = _run(f"ps -o pid=,rss=,pcpu=,etime= -p {pid}")
    parts = info.split()
    if len(parts) >= 4:
        rss_kb = int(parts[1])
        return {
            "running": True,
            "name": name,
            "pid": parts[0],
            "rss_mb": f"{rss_kb / 1024:.1f}",
            "cpu": parts[2],
            "elapsed": parts[3],
        }
    return {"running": False, "name": name}


def _get_system_memory():
    out = _run("vm_stat")
    data = {}
    page_size = 16384
    for line in out.splitlines():
        if "Pages free:" in line:
            data["free_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages wired down:" in line:
            data["wired_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages active:" in line:
            data["active_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages inactive:" in line:
            data["inactive_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages stored in compressor:" in line:
            data["compress_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
    total = 48.0
    # macOS: Free is always tiny; Inactive is reclaimable cache.
    # Show meaningful metrics: true used (wired+active) vs available (free+inactive).
    true_used = data.get("wired_gb", 0) + data.get("active_gb", 0)
    available = data.get("free_gb", 0) + data.get("inactive_gb", 0)
    data["total_gb"] = total
    data["used_gb"] = true_used          # Wired + Active (truly in use)
    data["available_gb"] = available     # Free + Inactive (reclaimable)
    data["used_pct"] = f"{true_used/total*100:.1f}"
    return data


def _should_reject_for_memory(mem=None):
    """Return (rejected: bool, used_pct: float) based on memory pressure threshold."""
    try:
        if mem is None:
            mem = _get_system_memory()
        used_pct = float(mem.get("used_pct", 0))
        return used_pct > PROXY_MEMORY_REJECT_THRESHOLD, used_pct
    except Exception:
        return False, 0.0


def _cleanup_snapshots(snapshot_dir, max_files):
    """Keep only the most recent max_files snapshot pairs."""
    try:
        files = [
            (f, os.path.getmtime(os.path.join(snapshot_dir, f)))
            for f in os.listdir(snapshot_dir)
            if f.endswith(".json")
        ]
        files.sort(key=lambda x: x[1], reverse=True)
        for old_file, _ in files[max_files:]:
            try:
                os.remove(os.path.join(snapshot_dir, old_file))
            except OSError:
                pass
    except OSError:
        pass


def _write_request_snapshot(request_id, before_body, after_body=None, error=None):
    """Write before/after request snapshots for debugging failures.

    Returns True if a snapshot was written.
    """
    if not PROXY_SNAPSHOT_ENABLED:
        return False
    try:
        snapshot_dir = os.path.join(_SCRIPT_DIR, "logs", "snapshots")
        os.makedirs(snapshot_dir, exist_ok=True)
        before_path = os.path.join(snapshot_dir, f"{request_id}_before.json")
        with open(before_path, "w", encoding="utf-8") as f:
            json.dump({"request_id": request_id, "body": before_body}, f,
                      ensure_ascii=False, indent=2)
        if after_body is not None or error is not None:
            after_path = os.path.join(snapshot_dir, f"{request_id}_after.json")
            payload = {"request_id": request_id}
            if after_body is not None:
                payload["body_after_pipeline"] = after_body
            if error is not None:
                payload["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
            with open(after_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        _cleanup_snapshots(snapshot_dir, PROXY_SNAPSHOT_MAX_FILES)
        return True
    except Exception:
        return False


def _read_log_tail(path, max_bytes=200000):
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", errors="ignore") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes), 0)
            return f.read()
    except OSError:
        return ""


def _record_request_for_concurrency(duration_ms, status):
    """Append a sample to the latency/error sliding windows."""
    try:
        _LATENCY_WINDOW.append(float(duration_ms))
        _ERROR_WINDOW.append(0 if int(status) == 200 else 1)
    except Exception:
        pass


def _percentile(values, p):
    """Return the p-th percentile of a list of numbers (0 <= p <= 1)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return s[f] + (s[c] - s[f]) * (k - f)


def _adjust_concurrency():
    """Dynamically adjust backend semaphore size based on recent latency/error window.

    Returns a dict describing the decision (or None if disabled).
    """
    global _llama_lock, PROXY_MAX_CONCURRENT
    if not PROXY_DYNAMIC_CONCURRENT_ENABLED:
        return None
    try:
        latencies = list(_LATENCY_WINDOW)
        errors = list(_ERROR_WINDOW)
        if len(latencies) < 5:
            return None
        p95 = _percentile(latencies, 0.95)
        error_rate = sum(errors) / len(errors) if errors else 0.0
        current = PROXY_MAX_CONCURRENT
        new_max = current
        if p95 > PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS or error_rate > PROXY_DYNAMIC_CONCURRENT_ERROR_RATE:
            new_max = max(PROXY_DYNAMIC_CONCURRENT_MIN, current - 1)
        elif p95 < PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS / 2 and error_rate == 0.0:
            new_max = min(PROXY_DYNAMIC_CONCURRENT_MAX, current + 1)
        if new_max != current:
            PROXY_MAX_CONCURRENT = new_max
            _llama_lock = threading.Semaphore(new_max)
            log(f"[DYNAMIC_CONCURRENT] adjusted {current} -> {new_max} (p95={p95:.0f}ms, error_rate={error_rate:.2f})")
            return {"adjusted": True, "previous": current, "current": new_max, "p95": p95, "error_rate": error_rate}
        return {"adjusted": False, "current": current, "p95": p95, "error_rate": error_rate}
    except Exception:
        return None


def _get_log_stats():
    """Count recent OOMs, forced cache clears, and requests from log tail.
    Requests get accurate timestamps from proxy logs [REQ_SUMMARY].
    OOM/CacheClear have no timestamp (backend logs don't include wall-clock time).
    For cloud backends, OOM/cache-clear metrics are not available."""
    backend_tail = _read_log_tail(_LOG_PATH, 200000) if not IS_CLOUD else ""
    proxy_log_path = os.environ.get("PROXY_LOG_PATH", "/tmp/anthropic_proxy.log")
    proxy_tail = _read_log_tail(proxy_log_path, 100000)

    # --- Extract request events from proxy logs ([HH:MM:SS] [REQ_SUMMARY] chars=X tools=Y) ---
    proxy_req_events = []
    for line in proxy_tail.splitlines()[-40:]:
        m = re.search(r'\[(\d{2}:\d{2}:\d{2})\].*\[REQ_SUMMARY\].*chars=(\d+).*tools=(\d+)', line)
        if m:
            proxy_req_events.append((m.group(1), m.group(2), m.group(3)))

    # --- Build recent events list ---
    events = []
    req_idx = 0
    if not IS_CLOUD:
        for line in backend_tail.splitlines()[-40:]:
            if "Insufficient Memory" in line:
                events.append(("—", "🔴 OOM", line.split(":")[-1].strip()[-80:]))
            elif "forced cache clear" in line:
                events.append(("—", "🟡 CacheClear", line.split(":")[-1].strip()[-80:]))
            elif "[REQUEST]" in line and "total_chars=" in line:
                m = re.search(r"total_chars=(\d+).*?tools=(\d+)", line)
                if m:
                    ts = proxy_req_events[req_idx][0] if req_idx < len(proxy_req_events) else "—"
                    events.append((ts, "📨 Request", f"{m.group(1)} chars, {m.group(2)} tools"))
                    req_idx += 1
    events = events[-12:]

    # --- Detailed lists for modal popup ---
    if IS_CLOUD:
        oom_details = []
        clear_details = []
    else:
        oom_details = [("—", line.split(":")[-1].strip()[-120:])
                       for line in backend_tail.splitlines() if "Insufficient Memory" in line]
        clear_details = [("—", line.split(":")[-1].strip()[-120:])
                         for line in backend_tail.splitlines() if "forced cache clear" in line]
    req_details = [(ts, f"{chars} chars, {tools} tools") for ts, chars, tools in proxy_req_events]

    return {
        "ooms": len(oom_details),
        "clears": len(clear_details),
        "requests": len(req_details),
        "last_events": events,
        "oom_details": oom_details[-20:],
        "clear_details": clear_details[-20:],
        "req_details": req_details[-20:],
    }


def _get_cache_stats():
    """Parse backend log for prefix cache HIT/MISS since current startup.
    Returns {"hit": N, "miss": N, "total": N, "rate_str": "X.X%", "since": "description"}.
    Cloud backends return zeros."""
    if IS_CLOUD:
        return {"hit": 0, "miss": 0, "total": 0, "rate_str": "N/A", "since": "N/A (cloud)"}
    try:
        with open(_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except (OSError, IOError):
        return {"hit": 0, "miss": 0, "total": 0, "rate_str": "N/A", "since": "log unavailable"}

    # Find the most recent startup (MemoryAwarePrefixCache initialized)
    start_idx = 0
    startup_line = ""
    for i, line in enumerate(lines):
        if "MemoryAwarePrefixCache initialized" in line:
            start_idx = i
            startup_line = line.strip()

    hit = miss = 0
    for line in lines[start_idx:]:
        if "cache_fetch" in line:
            if "HIT" in line:
                hit += 1
            elif "MISS" in line:
                miss += 1
    total = hit + miss
    rate = (hit / total * 100) if total > 0 else 0

    # Extract session label from startup line
    if startup_line:
        # e.g. "INFO:vllm_mlx.memory_cache:MemoryAwarePrefixCache initialized: max_memory=4096.0 MB"
        since = "last cache restart"
    else:
        since = "backend start"

    return {"hit": hit, "miss": miss, "total": total, "rate_str": f"{rate:.1f}%", "since": since}


def _get_traffic_stats():
    """Read proxy_requests.jsonl and compute traffic metrics + anomaly detection."""
    try:
        with open(_JSONL_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, IOError):
        return _empty_traffic_stats()

    if not lines:
        return _empty_traffic_stats()

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts_str = rec.get("start_time", "") or rec.get("timestamp", "") or rec.get("end_time", "")
            if ts_str:
                try:
                    rec["_ts"] = datetime.fromisoformat(ts_str)
                except ValueError:
                    continue
            else:
                continue
            records.append(rec)
        except json.JSONDecodeError:
            continue

    if not records:
        return _empty_traffic_stats()

    now = datetime.now()
    records_1h = [r for r in records if (now - r["_ts"]).total_seconds() <= 3600]
    records_10m = [r for r in records_1h if (now - r["_ts"]).total_seconds() <= 600]

    if not records_1h:
        return _empty_traffic_stats()

    def _stats(recs):
        if not recs:
            return {}
        durations = [r.get("duration_ms", 0) for r in recs]
        durations.sort()
        inputs = [r.get("input_chars", 0) for r in recs]
        outputs = [r.get("output_chars", 0) for r in recs]
        statuses = [r.get("status", 200) for r in recs]
        n = len(recs)
        return {
            "count": n,
            "avg_latency_ms": round(sum(durations) / n, 1) if n else 0,
            "p50_latency_ms": durations[n // 2] if n else 0,
            "p95_latency_ms": durations[int(n * 0.95)] if n else 0,
            "max_latency_ms": round(max(durations), 1) if durations else 0,
            "avg_input_chars": round(sum(inputs) / n, 0) if n else 0,
            "avg_output_chars": round(sum(outputs) / n, 0) if n else 0,
            "max_input_chars": max(inputs) if inputs else 0,
            "max_output_chars": max(outputs) if outputs else 0,
            "success_rate": round(sum(1 for s in statuses if s == 200) / n * 100, 1) if n else 100.0,
        }

    stats_1h = _stats(records_1h)
    stats_10m = _stats(records_10m)

    # --- Anomaly detection ---
    alerts = []
    # Duplicate requests: same input_chars within same second
    sec_to_inputs = {}
    for r in records_10m:
        sec_key = r["_ts"].strftime("%H:%M:%S")
        sec_to_inputs.setdefault(sec_key, []).append(r.get("input_chars", 0))
    for sec_key, inputs in sec_to_inputs.items():
        from collections import Counter
        c = Counter(inputs)
        for inp_chars, cnt in c.items():
            if cnt >= 2:
                alerts.append(("warn", f"重复请求: {sec_key} 内 {cnt} 个请求 input_chars={inp_chars:,}"))
    # Oversized requests
    for r in records_10m:
        inp = r.get("input_chars", 0)
        if inp > 100000:
            alerts.append(("warn", f"超大报文: {r['_ts'].strftime('%H:%M:%S')} input_chars={inp:,}"))
    # Slow requests
    for r in records_10m:
        dur = r.get("duration_ms", 0)
        if dur > 60000:
            et = r.get("end_time", "")
            et_short = datetime.fromisoformat(et).strftime('%H:%M:%S') if et else "?"
            alerts.append(("warn", f"超长耗时: {r['_ts'].strftime('%H:%M:%S')}→{et_short} {dur/1000:.1f}s"))
    # Very slow requests (critical)
    for r in records_10m:
        dur = r.get("duration_ms", 0)
        if dur > 120000:
            et = r.get("end_time", "")
            et_short = datetime.fromisoformat(et).strftime('%H:%M:%S') if et else "?"
            alerts.append(("critical", f"严重超时: {r['_ts'].strftime('%H:%M:%S')}→{et_short} {dur/1000:.1f}s"))

    # Latency distribution buckets for visualization
    all_durations = [r.get("duration_ms", 0) for r in records_1h]
    buckets = [
        ("<5s", 0), ("5-15s", 0), ("15-30s", 0),
        ("30-60s", 0), ("60-120s", 0), (">120s", 0),
    ]
    for d in all_durations:
        if d < 5000:
            buckets[0] = (buckets[0][0], buckets[0][1] + 1)
        elif d < 15000:
            buckets[1] = (buckets[1][0], buckets[1][1] + 1)
        elif d < 30000:
            buckets[2] = (buckets[2][0], buckets[2][1] + 1)
        elif d < 60000:
            buckets[3] = (buckets[3][0], buckets[3][1] + 1)
        elif d < 120000:
            buckets[4] = (buckets[4][0], buckets[4][1] + 1)
        else:
            buckets[5] = (buckets[5][0], buckets[5][1] + 1)

    return {
        "stats_1h": stats_1h,
        "stats_10m": stats_10m,
        "alerts": alerts,
        "latency_buckets": buckets,
        "last_record_time": records[-1]["_ts"].strftime("%H:%M:%S") if records else "—",
    }


def _empty_traffic_stats():
    return {
        "stats_1h": {},
        "stats_10m": {},
        "alerts": [],
        "latency_buckets": [("<5s", 0), ("5-15s", 0), ("15-30s", 0), ("30-60s", 0), ("60-120s", 0), (">120s", 0)],
        "last_record_time": "—",
    }


def _get_context_optimization_stats():
    """Aggregate recent proxy_metrics.jsonl for context optimization dashboard.

    Returns dict with avg common_prefix_ratio, avg compression_ratio,
    loop/blocker counts, and the most recent blocker event.
    """
    try:
        with open(_METRICS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, IOError):
        return _empty_context_optimization_stats()

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts_str = rec.get("ts", "")
            if ts_str:
                try:
                    rec["_ts"] = datetime.fromisoformat(ts_str)
                    records.append(rec)
                except ValueError:
                    pass
        except json.JSONDecodeError:
            continue

    if not records:
        return _empty_context_optimization_stats()

    now = datetime.now()
    recent = [r for r in records if (now - r["_ts"]).total_seconds() <= 600]
    if not recent:
        recent = records[-50:]

    ratios = [r.get("pipeline", {}).get("common_prefix_ratio", {}).get("ratio", 0) for r in recent]
    ratios = [r for r in ratios if isinstance(r, (int, float))]
    compressions = [r.get("compression_ratio", 1.0) for r in recent]
    compressions = [c for c in compressions if isinstance(c, (int, float))]

    loop_count = 0
    blocker_count = 0
    recent_blocker = None
    for r in recent:
        pipeline = r.get("pipeline", {})
        if pipeline.get("loop_detect", {}).get("max_run", 0) >= PROXY_LOOP_THRESHOLD:
            loop_count += 1
        blocker = pipeline.get("blocker_detect", {})
        if blocker.get("triggered"):
            blocker_count += 1
            recent_blocker = {
                "ts": r.get("ts", ""),
                "tool": blocker.get("tool_name", "?"),
                "error": blocker.get("error_type", "?"),
                "run": blocker.get("run_length", 0),
            }

    return {
        "avg_common_prefix_ratio": round(sum(ratios) / len(ratios), 3) if ratios else 0.0,
        "avg_compression_ratio": round(sum(compressions) / len(compressions), 3) if compressions else 1.0,
        "loop_triggered_10m": loop_count,
        "blocker_triggered_10m": blocker_count,
        "recent_blocker": recent_blocker,
        "max_concurrent": PROXY_MAX_CONCURRENT,
        "dynamic_concurrent_enabled": PROXY_DYNAMIC_CONCURRENT_ENABLED,
    }


def _empty_context_optimization_stats():
    return {
        "avg_common_prefix_ratio": 0.0,
        "avg_compression_ratio": 1.0,
        "loop_triggered_10m": 0,
        "blocker_triggered_10m": 0,
        "recent_blocker": None,
        "max_concurrent": PROXY_MAX_CONCURRENT,
        "dynamic_concurrent_enabled": PROXY_DYNAMIC_CONCURRENT_ENABLED,
    }


def _get_session_trace():
    """Parse /tmp/anthropic_request_body.json and build an HTML snippet showing
    the semantic message timeline (roles, tool calls, text previews, errors).
    Returns (html_str, tools_list) where tools_list is [(msg_idx, name, params), ...]
    for modal popup display."""
    try:
        with open("/tmp/anthropic_request_body.json", "r", encoding="utf-8") as f:
            body = json.load(f)
    except (OSError, json.JSONDecodeError):
        return '<div class="evt">No active request body</div>', [], []

    try:
        mtime = os.path.getmtime("/tmp/anthropic_request_body.json")
        saved_at = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
    except OSError:
        saved_at = None

    msgs = body.get("messages", [])
    model = body.get("model", "unknown")
    max_tokens = body.get("max_tokens", "?")
    total_chars = len(json.dumps(msgs, ensure_ascii=False)) if msgs else 0

    # Count roles and tool actions
    user_count = sum(1 for m in msgs if m.get("role") == "user")
    assistant_count = sum(1 for m in msgs if m.get("role") == "assistant")
    tool_uses = 0
    tool_results = 0
    errors = 0
    # Collect detailed tool use and error info for modal
    tools_detail = []
    errors_detail = []
    for idx, m in enumerate(msgs):
        content = m.get("content", [])
        if isinstance(content, list):
            for c in content:
                if c.get("type") == "tool_use":
                    tool_uses += 1
                    name = c.get("name", "?")
                    inp = c.get("input", {})
                    params = ", ".join(f"{k}={v!r}" for k, v in list(inp.items())[:4])
                    if len(inp) > 4:
                        params += ", ..."
                    tools_detail.append((saved_at or "—", f"Msg {idx}: {name}({params})"))
                elif c.get("type") == "tool_result":
                    tool_results += 1
                    tr = c.get("content", "")
                    err_text = ""
                    if isinstance(tr, str) and "tool_use_error" in tr:
                        errors += 1
                        err_text = tr[:120]
                    elif isinstance(tr, list) and tr:
                        t = tr[0].get("text", "")
                        if "tool_use_error" in str(t):
                            errors += 1
                            err_text = str(t)[:120]
                    if err_text:
                        err_summary = err_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        errors_detail.append((saved_at or "—", f"Msg {idx}: {err_summary}"))

    # Build timeline HTML (last 8 messages)
    timeline = []
    ts_html = f'<span class="evt-ts">{saved_at}</span> ' if saved_at else ''
    for idx, m in enumerate(msgs):
        if idx < len(msgs) - 8:
            continue
        role = m.get("role", "?")
        content = m.get("content", [])
        prefix = f"Msg {idx}"
        line = ""
        if isinstance(content, list):
            texts = []
            tools = []
            has_error = False
            for c in content:
                ctype = c.get("type", "")
                if ctype == "text":
                    t = c.get("text", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    texts.append(t[:60] + ("..." if len(t) > 60 else ""))
                elif ctype == "tool_use":
                    name = c.get("name", "?")
                    inp = c.get("input", {})
                    # Show a key param preview
                    preview = ""
                    if isinstance(inp, dict):
                        for k in ("command", "file_path", "subject", "description", "old_string"):
                            if k in inp:
                                v = str(inp[k])[:40]
                                preview = f" {k}={v}"
                                break
                    tools.append(f"{name}{preview}")
                elif ctype == "tool_result":
                    tr = c.get("content", "")
                    if isinstance(tr, str) and "tool_use_error" in tr:
                        has_error = True
                    elif isinstance(tr, list) and tr:
                        t = tr[0].get("text", "")
                        if "tool_use_error" in str(t):
                            has_error = True
            parts = []
            if texts:
                parts.append(texts[0])
            if tools:
                parts.append(" | ".join(tools))
            if has_error:
                parts.append("❌ ERROR")
            line = " | ".join(parts) if parts else "[empty]"
        else:
            t = str(content).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:60]
            line = t + ("..." if len(str(content)) > 60 else "")

        role_color = "#3498db" if role == "user" else ("#2ecc71" if role == "assistant" else "#888")
        timeline.append(
            f'<div class="evt">{ts_html}<span style="color:{role_color};font-weight:600;">{prefix} ({role})</span> {line}</div>'
        )

    if not timeline:
        timeline.append('<div class="evt">No messages</div>')

    summary = (
        f'<div class="row"><span class="label">Messages</span>'
        f'<span class="value">{len(msgs)}</span></div>'
        f'<div class="row"><span class="label">Model</span>'
        f'<span class="value">{model}</span></div>'
        f'<div class="row"><span class="label">Max Tokens</span>'
        f'<span class="value">{max_tokens}</span></div>'
        f'<div class="row"><span class="label">Total Chars</span>'
        f'<span class="value">{total_chars:,}</span></div>'
        f'<div class="row"><span class="label">User / Assistant</span>'
        f'<span class="value">{user_count} / {assistant_count}</span></div>'
        f'<div class="row"><span class="label">Tool Uses</span>'
        f'<span class="value clickable" onclick="showModal(\'tools\', \'🔧 Tool Calls Detail\')">{tool_uses}</span></div>'
        f'<div class="row"><span class="label">Errors</span>'
        f'<span class="value clickable" style="color:{"#e74c3c" if errors else "#2ecc71"}" onclick="showModal(' + "'errors', '❌ Errors Detail')" + f'">{errors}</span></div>'
    )
    if saved_at:
        summary += f'<div class="row"><span class="label">Captured At</span><span class="value">{saved_at}</span></div>'

    return summary + "\n".join(timeline), tools_detail, errors_detail


def _build_status_html():
    backend_info = _get_process_info("rapid-mlx|llama-server", "Backend")
    proxy_info = _get_process_info("anthropic_proxy.py", "Proxy", fallback_port=4000)
    mem = _get_system_memory()
    log = _get_log_stats()
    traffic = _get_traffic_stats()
    session_trace, tools_detail, errors_detail = _get_session_trace()
    cache_stats = _get_cache_stats()
    ctx_opt = _get_context_optimization_stats()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    backend_color = "#2ecc71" if backend_info.get("running") else "#e74c3c"
    cache_rate_color = "#888"
    if cache_stats["total"] > 0:
        rate = cache_stats["hit"] / cache_stats["total"] * 100
        cache_rate_color = "#2ecc71" if rate >= 50 else "#f39c12" if rate >= 20 else "#e74c3c"
    proxy_color = "#2ecc71" if proxy_info.get("running") else "#e74c3c"
    mem_used_pct = float(mem.get("used_pct", 0))
    mem_warn = mem_used_pct > PROXY_MEMORY_REJECT_THRESHOLD
    mem_alert = mem_used_pct > 75
    mem_color = "#e74c3c" if mem_warn or mem_alert else "#2ecc71"

    # --- Traffic Stats card ---
    s1h = traffic.get("stats_1h", {})
    s10m = traffic.get("stats_10m", {})
    qps = round(s10m.get("count", 0) / 600, 3) if s10m.get("count") else 0
    traffic_card = f"""<div class="card">
    <h2>📊 Traffic Stats</h2>
    <div class="row"><span class="label">Requests (1h / 10m)</span><span class="value">{s1h.get("count", 0)} / {s10m.get("count", 0)}</span></div>
    <div class="row"><span class="label">Avg Latency</span><span class="value">{s1h.get("avg_latency_ms", 0)/1000:.1f}s</span></div>
    <div class="row"><span class="label">P95 Latency</span><span class="value">{s1h.get("p95_latency_ms", 0)/1000:.1f}s</span></div>
    <div class="row"><span class="label">Max Latency</span><span class="value">{s1h.get("max_latency_ms", 0)/1000:.1f}s</span></div>
    <div class="row"><span class="label">Avg In / Out</span><span class="value">{s1h.get("avg_input_chars", 0):.0f} / {s1h.get("avg_output_chars", 0):.0f} chars</span></div>
    <div class="row"><span class="label">Max In / Out</span><span class="value">{s1h.get("max_input_chars", 0):,.0f} / {s1h.get("max_output_chars", 0):,.0f}</span></div>
    <div class="row"><span class="label">Success Rate</span><span class="value" style="color:{"#2ecc71" if s1h.get("success_rate", 100) >= 95 else "#f39c12" if s1h.get("success_rate", 100) >= 80 else "#e74c3c"}">{s1h.get("success_rate", 100):.1f}%</span></div>
    <div class="row"><span class="label">Est. QPS (10m)</span><span class="value">{qps:.3f}</span></div>
    <div class="row"><span class="label">Last Record</span><span class="value">{traffic.get("last_record_time", "—")}</span></div>
  </div>"""

    # --- Context Optimization card (Phase 3) ---
    recent_blocker_html = ""
    rb = ctx_opt.get("recent_blocker")
    if rb:
        recent_blocker_html = (
            f'<div class="row"><span class="label">Recent Blocker</span>'
            f'<span class="value" style="color:#f39c12">{rb.get("tool", "?")} / {rb.get("error", "?")} (run={rb.get("run", 0)})</span></div>'
        )
    ctx_opt_card = f"""<div class="card">
    <h2>🧠 Context Optimization</h2>
    <div class="row"><span class="label">Avg Prefix Ratio</span><span class="value">{ctx_opt.get("avg_common_prefix_ratio", 0):.1%}</span></div>
    <div class="row"><span class="label">Avg Compression</span><span class="value">{ctx_opt.get("avg_compression_ratio", 1.0):.2f}x</span></div>
    <div class="row"><span class="label">Loop Triggered (10m)</span><span class="value">{ctx_opt.get("loop_triggered_10m", 0)}</span></div>
    <div class="row"><span class="label">Blocker Triggered (10m)</span><span class="value">{ctx_opt.get("blocker_triggered_10m", 0)}</span></div>
    {recent_blocker_html}
    <div class="row"><span class="label">Max Concurrent</span><span class="value">{ctx_opt.get("max_concurrent", PROXY_MAX_CONCURRENT)}{" (dynamic)" if ctx_opt.get("dynamic_concurrent_enabled") else ""}</span></div>
  </div>"""

    # --- Alerts card ---
    alerts = traffic.get("alerts", [])
    if alerts:
        alerts_html = ""
        for severity, msg in alerts:
            color = "#e74c3c" if severity == "critical" else "#f39c12"
            icon = "🔴" if severity == "critical" else "⚠️"
            alerts_html += f'<div class="evt"><span style="color:{color};font-weight:600;">{icon} {msg}</span></div>'
    else:
        alerts_html = '<div class="evt" style="color:#2ecc71;">✅ No anomalies detected (last 10m)</div>'

    # Cloud-backend status card (no PID/memory/uptime)
    if IS_CLOUD:
        backend_card = f"""<div class="card">
    <h2>Backend</h2>
    <div class="row"><span class="label">Type</span><span class="value">Cloud API ({BACKEND_TYPE})</span></div>
    <div class="row"><span class="label">Endpoint</span><span class="value">{LLAMA_BASE}</span></div>
    <div class="row"><span class="label">Model</span><span class="value">{MODEL_NAME}</span></div>
    <div class="row"><span class="label">API Key</span><span class="value">{LLAMA_API_KEY[:8]}****</span></div>
  </div>"""
    else:
        backend_card = f"""<div class="card">
    <h2>Backend</h2>
    <div class="row"><span class="label">Status</span><span class="value"><span class="status-dot" style="background:{backend_color}"></span>{"Running" if backend_info.get("running") else "Stopped"}</span></div>
    <div class="row"><span class="label">Name</span><span class="value">{backend_info.get("name", "N/A")}</span></div>
    <div class="row"><span class="label">PID</span><span class="value">{backend_info.get("pid", "N/A")}</span></div>
    <div class="row"><span class="label">Memory</span><span class="value">{backend_info.get("rss_mb", "N/A")} MB</span></div>
    <div class="row"><span class="label">CPU</span><span class="value">{backend_info.get("cpu", "N/A")}%</span></div>
    <div class="row"><span class="label">Uptime</span><span class="value">{backend_info.get("elapsed", "N/A")}</span></div>
  </div>"""

    # Conditional log-stat rows (avoid backslashes inside f-strings)
    oom_row = ""
    cache_row = ""
    if not IS_CLOUD:
        oom_row = '<div class="row"><span class="label">OOM Crashes</span><span class="value oom clickable" onclick="showModal(' + "'oom', '🔴 OOM Crashes Detail')" + f'">{log["ooms"]}</span></div>'
        cache_row = '<div class="row"><span class="label">Forced Cache Clear</span><span class="value clear clickable" onclick="showModal(' + "'clear', '🟡 Forced Cache Clear Detail')" + f'">{log["clears"]}</span></div>'

    events_html = ""
    for ts, evt_type, evt_msg in log["last_events"]:
        ts_display = f'<span class="evt-ts">{ts}</span>' if ts != "—" else '<span class="evt-ts" style="color:#666">—</span>'
        events_html += f'<div class="evt">{ts_display} <span class="evt-tag">{evt_type}</span> {evt_msg}</div>'
    if not events_html:
        events_html = '<div class="evt">No recent events</div>'

    # JSON data for modal popups
    import json as _json
    modal_data = _json.dumps({
        "oom": log.get("oom_details", []),
        "clear": log.get("clear_details", []),
        "request": log.get("req_details", []),
        "tools": tools_detail,
        "errors": errors_detail,
    })
    # Escape </script> inside <script> to prevent premature tag closure
    # when eventData contains nested HTML/JS (e.g. Write tool content).
    modal_data = modal_data.replace("</script>", "<\\/script>")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<!-- auto-refresh disabled when modal is open -->
<title>Local LLM Stack Status</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .ts {{ color: #888; font-size: 12px; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
  .card {{ background: #16213e; border-radius: 10px; padding: 16px; }}
  .card h2 {{ font-size: 14px; margin: 0 0 12px 0; color: #a0a0c0; text-transform: uppercase; letter-spacing: 1px; }}
  .row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #2a2a4a; font-size: 13px; }}
  .row:last-child {{ border-bottom: none; }}
  .label {{ color: #888; }}
  .value {{ font-weight: 600; }}
  .status-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
  .mem-bar {{ height: 10px; background: #2a2a4a; border-radius: 5px; margin-top: 8px; overflow: hidden; }}
  .mem-fill {{ height: 100%; border-radius: 5px; transition: width 0.5s; }}
  .evt {{ font-size: 12px; padding: 4px 0; border-bottom: 1px solid #2a2a4a; color: #ccc; }}
  .evt:last-child {{ border-bottom: none; }}
  .evt-tag {{ display: inline-block; min-width: 80px; font-weight: 600; font-size: 11px; }}
  .evt-ts {{ display: inline-block; min-width: 60px; font-family: monospace; font-size: 11px; color: #888; margin-right: 4px; }}
  .oom {{ color: #e74c3c; }}
  .clear {{ color: #f39c12; }}
  .req {{ color: #3498db; }}
  .clickable {{ cursor: pointer; text-decoration: underline; }}
  .clickable:hover {{ opacity: 0.8; }}
  .footer {{ margin-top: 20px; font-size: 11px; color: #666; text-align: center; }}
  .modal {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.75); z-index: 100; justify-content: center; align-items: center; }}
  .modal-content {{ background: #16213e; border-radius: 10px; padding: 20px; max-width: 800px; width: 90%; max-height: 80vh; overflow-y: auto; border: 1px solid #2a2a4a; }}
  .close-btn {{ float: right; font-size: 24px; cursor: pointer; color: #888; line-height: 1; }}
  .close-btn:hover {{ color: #fff; }}
  .modal-row {{ padding: 8px 0; border-bottom: 1px solid #2a2a4a; font-size: 12px; color: #ccc; display: flex; gap: 12px; }}
  .modal-row:last-child {{ border-bottom: none; }}
  .modal-time {{ color: #888; font-family: monospace; min-width: 60px; flex-shrink: 0; }}
  .modal-msg {{ word-break: break-word; }}
</style>
</head>
<body>
<h1>🖥️ Local LLM Stack Status</h1>
<div class="ts">Updated: {now} &nbsp;•&nbsp; Auto-refresh every 5s</div>

<div class="grid">
  {backend_card}

  <div class="card">
    <h2>Proxy</h2>
    <div class="row"><span class="label">Status</span><span class="value"><span class="status-dot" style="background:{proxy_color}"></span>{"Running" if proxy_info.get("running") else "Stopped"}</span></div>
    <div class="row"><span class="label">Name</span><span class="value">{proxy_info.get("name", "N/A")}</span></div>
    <div class="row"><span class="label">PID</span><span class="value">{proxy_info.get("pid", "N/A")}</span></div>
    <div class="row"><span class="label">Memory</span><span class="value">{proxy_info.get("rss_mb", "N/A")} MB</span></div>
    <div class="row"><span class="label">Listen</span><span class="value">127.0.0.1:4000</span></div>
    <div class="row"><span class="label">Backend</span><span class="value">{LLAMA_BASE}</span></div>
  </div>

  <div class="card">
    <h2>System Memory</h2>
    <div class="row"><span class="label">Total</span><span class="value">{mem.get("total_gb", 48):.0f} GB</span></div>
    <div class="row"><span class="label">Used (Wired+Active)</span><span class="value" style="color:{mem_color}">{mem.get("used_gb", 0):.1f} GB ({mem.get("used_pct", "0")}%)</span></div>
    <div class="row"><span class="label">Available</span><span class="value">{mem.get("available_gb", 0):.1f} GB (Free+Inactive)</span></div>
    <div class="row"><span class="label">Wired</span><span class="value">{mem.get("wired_gb", 0):.1f} GB</span></div>
    <div class="row"><span class="label">Active</span><span class="value">{mem.get("active_gb", 0):.1f} GB</span></div>
    <div class="row"><span class="label">Inactive</span><span class="value">{mem.get("inactive_gb", 0):.1f} GB</span></div>
    <div class="row"><span class="label">Compressed</span><span class="value">{mem.get("compress_gb", 0):.1f} GB</span></div>
    <div class="mem-bar"><div class="mem-fill" style="width:{mem.get("used_pct", 0)}%;background:{mem_color}"></div></div>
  </div>

  <div class="card">
    <h2>Log Stats (recent tail)</h2>
    {oom_row}
    {cache_row}
    <div class="row"><span class="label">Requests</span><span class="value req clickable" onclick="showModal('request', '📨 Requests Detail')">{log["requests"]}</span></div>
    <div class="row"><span class="label">Prefix Cache</span><span class="value" style="color:{cache_rate_color}" title="统计范围: {cache_stats['since']} (跨session累计请查看 /status 页面历史)">{cache_stats["hit"]}/{cache_stats["total"]} ({cache_stats["rate_str"]})</span></div>
    <div class="row"><span class="label">Config</span><span class="value">CLEAR={'on' if PROXY_CLEAR_ENABLED else 'off'}, LIMIT={'on' if PROXY_CTX_LIMIT_ENABLED else 'off'}, MAX_CONCURRENT={PROXY_MAX_CONCURRENT}</span></div>
    <div class="row"><span class="label">Model</span><span class="value">{MODEL_NAME}</span></div>
    {'<div class="row"><span class="label">Memory Alert</span><span class="value" style="color:#e74c3c">⚠️ Used ' + str(mem_used_pct) + '% (reject threshold ' + str(PROXY_MEMORY_REJECT_THRESHOLD) + '%)</span></div>' if mem_warn else ''}
  </div>

  {traffic_card}

  {ctx_opt_card}

  <div class="card" style="grid-column: 1 / -1;">
    <h2>🚨 Alerts (last 10m)</h2>
    {alerts_html}
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>Session Trace</h2>
    {session_trace}
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>Recent Events</h2>
    {events_html}
  </div>
</div>

<div class="footer">Open http://127.0.0.1:4000/status in your browser</div>

<!-- Modal -->
<div id="modal" class="modal" onclick="closeModal(event)">
  <div class="modal-content" onclick="event.stopPropagation()">
    <span class="close-btn" onclick="closeModal()">&times;</span>
    <h3 id="modal-title" style="margin-top:0;color:#a0a0c0;font-size:14px;text-transform:uppercase;letter-spacing:1px;">Detail</h3>
    <div id="modal-body"></div>
  </div>
</div>

<script>
var eventData = {modal_data};
function showModal(type, title) {{
  document.getElementById('modal-title').innerText = title;
  var body = document.getElementById('modal-body');
  body.innerHTML = '';
  var items = eventData[type] || [];
  if (items.length === 0) {{
    body.innerHTML = '<div class="modal-row">No events found</div>';
  }} else {{
    items.forEach(function(item) {{
      var row = document.createElement('div');
      row.className = 'modal-row';
      var ts = item[0] || '—';
      var msg = item[1] || '';
      row.innerHTML = '<span class="modal-time">' + ts + '</span><span class="modal-msg">' + msg + '</span>';
      body.appendChild(row);
    }});
  }}
  document.getElementById('modal').style.display = 'flex';
}}
function closeModal(e) {{
  if (!e || e.target.id === 'modal') {{
    document.getElementById('modal').style.display = 'none';
  }}
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') closeModal();
}});
setInterval(function() {{
  if (document.getElementById('modal').style.display !== 'flex') {{
    location.reload();
  }}
}}, 5000);
</script>
</body>
</html>"""
    return html


# TODO(roadmap-U4): Auto-tune thresholds/budget from quality_flags history
def _finalize_metrics(mc):
    pipeline = mc.get("pipeline", {})
    quality_flags = []
    trunc = pipeline.get("truncate", {})
    if trunc.get("triggered"):
        dropped = trunc.get("dropped", 0)
        kept = trunc.get("kept", 0)
        if kept + dropped > 0 and dropped / (dropped + kept) > 0.7:
            quality_flags.append("high_drop_ratio")
        if trunc.get("compression") in ("rules", "folded") and dropped >= 10:
            quality_flags.append("llm_compress_failed")
        est_after = trunc.get("est_tokens_after", 0)
        budget = trunc.get("budget", 0)
        if budget > 0 and est_after > budget * 1.1:
            quality_flags.append("budget_overflow")
    loop = pipeline.get("loop_detect", {})
    if loop.get("max_run", 0) >= PROXY_LOOP_THRESHOLD:
        quality_flags.append("loop_injected")
    blocker = pipeline.get("blocker_detect", {})
    if blocker.get("triggered"):
        quality_flags.append("blocker_injected")
    mc["quality_flags"] = quality_flags

    # Phase 3: dynamic token estimation
    input_chars = mc.get("input_chars", 0)
    token_ratio = PROXY_CTX_TOKEN_RATIO
    try:
        # Reconstruct a minimal message list for ratio detection. The original
        # body is no longer available here, so we fall back to classifying the
        # input_chars text as a single English block for ratio selection.
        content_type = _classify_content_for_ratio("x" * min(input_chars, 1000))
        ratio_map = {
            "chinese": PROXY_TOKEN_RATIO_CHINESE,
            "english": PROXY_TOKEN_RATIO_ENGLISH,
            "code": PROXY_TOKEN_RATIO_CODE,
        }
        token_ratio = ratio_map.get(content_type, PROXY_CTX_TOKEN_RATIO)
    except Exception:
        token_ratio = PROXY_CTX_TOKEN_RATIO
    input_est = int(input_chars / max(token_ratio, 0.1))
    est_after = trunc.get("est_tokens_after", input_est) if trunc.get("triggered") else input_est
    if input_est > 0:
        mc["compression_ratio"] = round(est_after / input_est, 2)
    else:
        mc["compression_ratio"] = 1.0
    mc["token_ratio"] = round(token_ratio, 2)
    mc["est_input_tokens"] = input_est
    output_chars = mc.get("output_chars", 0)
    mc["est_output_tokens"] = int(output_chars / max(token_ratio, 0.1))

    # Phase 3: schema v1 — guarantee a fixed set of keys
    mc["schema_version"] = "v1"
    for field in _METRICS_V1_FIELDS:
        mc.setdefault(field, None)
    mc["dynamic_concurrent"] = {
        "enabled": PROXY_DYNAMIC_CONCURRENT_ENABLED,
        "current": PROXY_MAX_CONCURRENT,
        "min": PROXY_DYNAMIC_CONCURRENT_MIN,
        "max": PROXY_DYNAMIC_CONCURRENT_MAX,
    }


def _mc_put(step_key, data):
    mc = getattr(_metrics_ctx, 'mc', None)
    if mc and PROXY_METRICS_ENABLED:
        mc["pipeline"][step_key] = data


def _filter_tools(tools, messages, recent_rounds=5, tool_choice_name=None):
    if not tools or len(tools) <= PROXY_TOOL_FILTER_MAX:
        return tools, {"filtered": False, "reason": "below_max"}

    recent_tools = set()
    assistant_count = 0
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            assistant_count += 1
            content = msg.get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        recent_tools.add(b.get("name", ""))
            if assistant_count >= recent_rounds:
                break

    always_keep_set = set(TOOL_ALWAYS_KEEP)
    keep_set = always_keep_set | recent_tools
    if tool_choice_name:
        keep_set.add(tool_choice_name)

    # Phase 1: sort kept tools by a stable order (always-keep first, then recent,
    # then alphabetical) so the prefix token sequence is identical across requests
    # when the same tools are available.
    def _tool_sort_key(t):
        name = t.get("name", "")
        if name in always_keep_set:
            # preserve the order defined in TOOL_ALWAYS_KEEP
            return (0, TOOL_ALWAYS_KEEP.index(name))
        if name in recent_tools:
            return (1, name)
        return (2, name)

    kept = sorted(
        [t for t in tools if isinstance(t, dict) and t.get("name", "") in keep_set],
        key=_tool_sort_key
    )

    if len(kept) < 5:
        return tools, {"filtered": False, "reason": "too_few_after_filter"}

    kept_names = {t.get("name", "") for t in kept if isinstance(t, dict)}
    if len(kept) < PROXY_TOOL_FILTER_MAX:
        remaining = sorted(
            [t for t in tools if isinstance(t, dict) and t.get("name", "") not in kept_names],
            key=lambda t: t.get("name", "")
        )
        kept.extend(remaining[:PROXY_TOOL_FILTER_MAX - len(kept)])
        kept.sort(key=_tool_sort_key)
        kept_names = {t.get("name", "") for t in kept if isinstance(t, dict)}

    all_names = {t.get("name", "") for t in tools if isinstance(t, dict)}
    filtered_out = sorted(all_names - kept_names)

    return kept, {
        "filtered": True,
        "original": len(tools),
        "kept": len(kept),
        "always_keep": len(always_keep_set & kept_names),
        "recent_only": len(recent_tools - always_keep_set),
        "recent_tools": sorted(recent_tools),
        "scanned_assistant": assistant_count,
        "filtered_out": filtered_out,
    }


def _extract_keywords(messages):
    keywords = {}
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        text = ""
        files = []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text += b.get("text", "") + " "
                elif b.get("type") == "tool_use":
                    name = b.get("name", "")
                    inp = b.get("input", {})
                    if isinstance(inp, dict):
                        for k in ("file_path", "path", "directory"):
                            fp = inp.get(k, "")
                            if fp:
                                files.append(fp)
                    text += f"{name} "
                elif b.get("type") == "tool_result":
                    tc = b.get("content", "")
                    if isinstance(tc, str):
                        text += tc[:200] + " "
                    elif isinstance(tc, list):
                        for tb in tc:
                            if isinstance(tb, dict):
                                text += str(tb.get("text", ""))[:200] + " "
        summary = f"{role}: {text[:100].strip()}"
        for path in files:
            fname = path.split("/")[-1]
            keywords.setdefault(fname, []).append(summary)
        for err in re.findall(r'\b([A-Z]\w*(?:Error|Exception))\b', text):
            keywords.setdefault(err, []).append(summary)
        for func in re.findall(r'\b([a-z][a-zA-Z0-9_]{3,})\s*\(', text):
            keywords.setdefault(func, []).append(summary)
    return keywords


def _inject_keyword_context(keywords, current_messages, top_k=5, max_chars=500):
    query_text = ""
    for msg in list(reversed(current_messages))[:3]:
        content = msg.get("content", "")
        if isinstance(content, str):
            query_text += content + " "
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    query_text += b.get("text", "") + " "
    if not query_text.strip():
        return None
    matches = []
    seen = set()
    for kw, entries in keywords.items():
        if kw.lower() in query_text.lower():
            for entry in entries:
                if entry not in seen:
                    seen.add(entry)
                    matches.append(f"[{kw}]: {entry}")
    if not matches:
        return None
    matches = matches[:top_k]
    result = "[Relevant history context:]\n" + "\n".join(f"- {m}" for m in matches)
    if len(result) > max_chars:
        result = result[:max_chars] + "..."
    return result


# ---------------------------------------------------------------------------
# Error translation (R5.1 / R5.2) — extracted from _handle_messages for testability
# ---------------------------------------------------------------------------
def _translate_tool_result_errors(messages):
    """Walk the user-side tool_result blocks and rewrite known backend
    error patterns into natural-language Chinese hints. Returns
    (messages, counts_dict) and mutates `messages` in place.

    Three patterns are recognised:
      - "Wasted call"     → "文件自上次读取后未发生变化"  (R5.1 wasted)
      - "File does not exist" / "No such file" → "文件不存在..."  (R5.1 file_not_found)
      - "InputValidationError" / "invalid x" → "工具调用参数错误..."  (R5.1 input_validation)

    Each replacement includes a solution hint (R5.2):
      - wasted: 用 Bash cat 代替
      - file_not_found: 用 Bash ls 或 find 确认项目结构
      - input_validation: 检查工具参数格式
    """
    error_count = {"wasted": 0, "file_not_found": 0, "input_validation": 0}
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_result":
                continue
            bc = str(block.get("content", ""))
            if "Wasted call" in bc:
                block["content"] = (
                    "[System: 该文件自上次读取后未发生变化，不要再使用 Read 工具反复读取。"
                    "如果需要查看文件内容，用 Bash cat 命令代替。]"
                )
                error_count["wasted"] += 1
            elif "File does not exist" in bc or "No such file" in bc:
                block["content"] = (
                    "[System: 文件不存在。请先用 Bash ls 或 find 命令确认项目结构，"
                    "然后使用正确的文件路径。]"
                )
                error_count["file_not_found"] += 1
            elif "InputValidationError" in bc or "invalid x" in bc.lower():
                block["content"] = (
                    "[System: 工具调用参数错误。请检查工具参数格式，"
                    "确保所有必填参数正确提供。]"
                )
                error_count["input_validation"] += 1
    return messages, error_count


# ---------------------------------------------------------------------------
# Loop intervention (R2.1) — extracted from _handle_messages for testability
# ---------------------------------------------------------------------------
def _apply_loop_intervention(
    raw_messages, raw_tools, max_run, consecutive,
    threshold=PROXY_LOOP_THRESHOLD, level2_threshold=PROXY_LOOP_LEVEL2,
    level3_threshold=PROXY_LOOP_LEVEL3, pattern_tool_name=None,
    is_text_loop=False, text_loop_run=0,
):
    """Escalating loop intervention (R2.1). Returns (messages, tools, level, tool_name).

    Pure-ish: given the conversation state, returns the (possibly extended) message
    list and (possibly filtered) tools list. Caller is responsible for assigning
    them back to body["messages"]/body["tools"] and emitting the metrics step.

    - max_run < threshold          → no-op, returns (raw_messages, raw_tools, 0, "")
    - threshold <= max_run < L2    → Level 1: append hint user message
    - L2 <= max_run < L3           → Level 2: remove ALL high-count tools from raw_tools
    - max_run >= L3                → Level 3: strip ALL tools (force plain text)
    """
    if max_run < threshold:
        return raw_messages, raw_tools, 0, ""

    # Text loop detection: different intervention for repeated similar text
    if is_text_loop and text_loop_run >= threshold:
        if text_loop_run >= level3_threshold:
            new_messages = list(raw_messages) + [{
                "role": "user",
                "content": [{"type": "text", "text":
                    f"[CRITICAL: You have been repeating the same text output {text_loop_run} times. "
                    f"This is a text output loop. ALL tools have been DISABLED for this turn. "
                    f"You MUST stop repeating yourself. "
                    f"If you are stuck, say 'I am stuck' and explain what is wrong. "
                    f"Do NOT repeat the same explanation again.]"
                }]
            }]
            return new_messages, [], 3, "text_loop"
        elif text_loop_run >= level2_threshold:
            new_messages = list(raw_messages) + [{
                "role": "user",
                "content": [{"type": "text", "text":
                    f"[IMPORTANT: You have repeated similar text {text_loop_run} times. "
                    f"This indicates you are stuck in a loop. "
                    f"STOP repeating the same explanation. "
                    f"If you cannot solve the problem, say so clearly and ask for help. "
                    f"Do NOT repeat your previous analysis.]"
                }]
            }]
            return new_messages, raw_tools, 2, "text_loop"
        else:
            new_messages = list(raw_messages) + [{
                "role": "user",
                "content": [{"type": "text", "text":
                    f"[System notice: You have repeated similar text output {text_loop_run} times. "
                    f"This appears to be a loop. "
                    f"STOP repeating yourself and either provide a NEW response or acknowledge you are stuck. "
                    f"Do NOT repeat the same explanation.]"
                }]
            }]
            return new_messages, raw_tools, 1, "text_loop"

    # Tool loop detection (original logic)
    loop_keys = [k for k, v in consecutive.items() if v >= threshold]
    high_count_tools = sorted(set(k.split(":")[0] for k in loop_keys))
    tool_name = high_count_tools[0] if high_count_tools else (pattern_tool_name or "unknown")

    if max_run >= level3_threshold:
        restricted = sorted(set(t.get("name", "") for t in (raw_tools or []) if isinstance(t, dict)))
        new_messages = list(raw_messages) + [{
            "role": "user",
            "content": [{"type": "text", "text":
                f"[CRITICAL: You have been looping for {max_run} repetitions across tools "
                f"({', '.join(restricted[:5])}). ALL tools have been DISABLED for this turn. "
                f"You MUST respond with plain text only. "
                f"Describe the problem you are stuck on and what you have tried. "
                f"Do NOT attempt to call any tools.]"
            }]
        }]
        return new_messages, [], 3, tool_name

    if max_run >= level2_threshold:
        remove_set = set(high_count_tools)
        new_tools = [t for t in (raw_tools or [])
                     if not (isinstance(t, dict) and t.get("name", "") in remove_set)]
        removed_names = ", ".join(sorted(remove_set))
        new_messages = list(raw_messages) + [{
            "role": "user",
            "content": [{"type": "text", "text":
                f"[IMPORTANT: You have called tools {max_run} times with no progress. "
                f"The following tools have been REMOVED: {removed_names}. "
                f"You MUST use a completely different approach. "
                f"If you are stuck, describe the problem and ask for guidance.]"
            }]
        }]
        return new_messages, new_tools, 2, tool_name

    new_messages = list(raw_messages) + [{
        "role": "user",
        "content": [{"type": "text", "text":
            f"[System notice: You have repeated the same action {max_run} times "
            f"(tool: {tool_name}). This is likely a loop. "
            f"STOP using {tool_name} immediately and try a completely different approach. "
            f"If file content was cleared, assume the file is unchanged and work from memory.]"
        }]
    }]
    return new_messages, raw_tools, 1, tool_name


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
            # Bound memory for the session message cache.
            if len(_SESSION_LAST_MESSAGES) > 1000:
                _SESSION_LAST_MESSAGES.pop(next(iter(_SESSION_LAST_MESSAGES)), None)
            _SESSION_LAST_MESSAGES[session_id] = [dict(m) for m in raw_messages]

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
