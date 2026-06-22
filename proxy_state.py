#!/usr/bin/env python3
"""
proxy_state.py — single source of truth for all configuration constants,
mutable shared state, thread-local contexts, and config helper functions.

All PROXY_* / LLAMA_* constants, IS_CLOUD, MODEL_NAME, MODEL_ALIASES,
shared mutable dicts, thread-locals, and the _RELOAD_SPEC live here.
anthropic_proxy.py and proxy_config.py both import from this module.
"""

import collections
import os
import re
import subprocess
import threading
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _run(cmd, timeout=3):
    """Run a shell command and return stripped stdout, or empty string on error."""
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=timeout).strip()
    except Exception:
        return ""


def _get_system_memory():
    """Return macOS system memory stats used by routing and status page.

    Keys: total_gb, used_gb, available_gb, used_pct (string like "85.3").
    """
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
    true_used = data.get("wired_gb", 0) + data.get("active_gb", 0)
    available = data.get("free_gb", 0) + data.get("inactive_gb", 0)
    data["total_gb"] = total
    data["used_gb"] = true_used
    data["available_gb"] = available
    data["used_pct"] = f"{true_used/total*100:.1f}"
    return data

# ---------------------------------------------------------------------------
# Backend routing
# ---------------------------------------------------------------------------
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

from backend_strategy import BackendStrategy
_strategy = BackendStrategy.create(IS_CLOUD)

# ---------------------------------------------------------------------------
# Concurrency control: backend-aware request serialization
# ---------------------------------------------------------------------------

# Config default resolution: reads canonical defaults from proxy_config
def _default(env_key, cloud_val, local_val):
    """Return the canonical default for env_key, falling back to hardcoded.
    Uses backend_strategy for IS_CLOUD-dependent defaults."""
    try:
        from proxy_config import resolve_default
        resolved = resolve_default(env_key, IS_CLOUD)
        if resolved is not None:
            return str(resolved)
    except (ImportError, Exception):
        pass
    return str(_strategy.get_default(env_key, local_val if not IS_CLOUD else cloud_val))

PROXY_MAX_CONCURRENT = int(os.environ.get("PROXY_MAX_CONCURRENT", _default("PROXY_MAX_CONCURRENT", "4", "1")))
_llama_lock = threading.Semaphore(PROXY_MAX_CONCURRENT)
MODEL_NAME = os.environ.get("MODEL_NAME", _default("MODEL_NAME", "deepseek-v4-pro", "mlx-community/Qwen3.6-35B-A3B-4bit"))

# ---------------------------------------------------------------------------
# Tool-result clearing: proxy-side context management
# ---------------------------------------------------------------------------
PROXY_CLEAR_ENABLED = os.environ.get("PROXY_CLEAR_ENABLED", _default("PROXY_CLEAR_ENABLED", "false", "true")).lower() in ("1", "true", "yes")
PROXY_CLEAR_THRESHOLD = int(os.environ.get("PROXY_CLEAR_THRESHOLD", _default("PROXY_CLEAR_THRESHOLD", "30000", "15000")))
PROXY_TOOL_KEEP = int(os.environ.get("PROXY_TOOL_KEEP", _default("PROXY_TOOL_KEEP", "10", "2")))

# ---------------------------------------------------------------------------
# Frozen Zone
# ---------------------------------------------------------------------------
PROXY_FROZEN_HEAD = int(os.environ.get("PROXY_FROZEN_HEAD", _default("PROXY_FROZEN_HEAD", "0", "12")))

# ---------------------------------------------------------------------------
# Tail-first clearing
# ---------------------------------------------------------------------------
PROXY_CLEAR_TAIL_FIRST = os.environ.get("PROXY_CLEAR_TAIL_FIRST", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Cache Aligner (Phase 1)
# ---------------------------------------------------------------------------
PROXY_CACHE_ALIGN_ENABLED = os.environ.get("PROXY_CACHE_ALIGN_ENABLED", _default("PROXY_CACHE_ALIGN_ENABLED", "false", "true")).lower() in ("1", "true", "yes")
PROXY_CACHE_ALIGN_HEAD = int(os.environ.get("PROXY_CACHE_ALIGN_HEAD", "4"))

# Per-session last messages for computing common_prefix_ratio.
_SESSION_LAST_MESSAGES = {}
_LOOP_SESSION_STATE = {}

# Shared state for session tracking, dedup, and dynamic concurrency.
# Read-modify-write sequences on these dicts must acquire _state_lock.
_SESSION_REQUEST_COUNT = {}

# ---------------------------------------------------------------------------
# Semantic content compression (Phase 2)
# ---------------------------------------------------------------------------
PROXY_COMPRESS_ENABLED = os.environ.get("PROXY_COMPRESS_ENABLED", _default("PROXY_COMPRESS_ENABLED", "false", "true")).lower() in ("1", "true", "yes")
PROXY_COMPRESS_THRESHOLD = int(os.environ.get("PROXY_COMPRESS_THRESHOLD", "4096"))
PROXY_COMPRESS_MODE = os.environ.get("PROXY_COMPRESS_MODE", "semantic")
PROXY_SCRUB_ANSI = os.environ.get("PROXY_SCRUB_ANSI", "true").lower() in ("1", "true", "yes")
PROXY_SIEVE_JSON_MAX_ITEMS = int(os.environ.get("PROXY_SIEVE_JSON_MAX_ITEMS", "10"))
PROXY_SIEVE_JSON_MAX_STR_LEN = int(os.environ.get("PROXY_SIEVE_JSON_MAX_STR_LEN", "200"))
PROXY_SIEVE_JSON_MAX_DEPTH = int(os.environ.get("PROXY_SIEVE_JSON_MAX_DEPTH", "4"))
PROXY_LOG_DEDUPE = os.environ.get("PROXY_LOG_DEDUPE", "true").lower() in ("1", "true", "yes")
PROXY_DEDUPE_SCALARS = os.environ.get("PROXY_DEDUPE_SCALARS", "false").lower() in ("1", "true", "yes")
PROXY_COMPRESS_AUDIT = os.environ.get("PROXY_COMPRESS_AUDIT", "true").lower() in ("1", "true", "yes")

CONTENT_TOOLS_FALLBACK_ENABLED = os.environ.get("PROXY_CONTENT_TOOLS_FALLBACK", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Context-limit truncation
# ---------------------------------------------------------------------------
PROXY_CTX_LIMIT_ENABLED = os.environ.get("PROXY_CTX_LIMIT_ENABLED", _default("PROXY_CTX_LIMIT_ENABLED", "false", "true")).lower() in ("1", "true", "yes")
PROXY_CTX_CHARS_LIMIT = int(os.environ.get("PROXY_CTX_CHARS_LIMIT", _default("PROXY_CTX_CHARS_LIMIT", "500000", "180000")))
PROXY_CTX_KEEP_HEAD = int(os.environ.get("PROXY_CTX_KEEP_HEAD", "2"))
PROXY_CTX_KEEP_TAIL = int(os.environ.get("PROXY_CTX_KEEP_TAIL", "4"))
PROXY_CTX_TRUNCATE_STRATEGY = os.environ.get("PROXY_CTX_TRUNCATE_STRATEGY", "char")
PROXY_CTX_KEEP_ROUNDS = int(os.environ.get("PROXY_CTX_KEEP_ROUNDS", "10"))
PROXY_CTX_KEEP_MESSAGES = int(os.environ.get("PROXY_CTX_KEEP_MESSAGES", "40"))
PROXY_CTX_TOKEN_BUDGET = int(os.environ.get("PROXY_CTX_TOKEN_BUDGET", "30000"))
PROXY_CTX_TOKEN_RATIO = float(os.environ.get("PROXY_CTX_TOKEN_RATIO", "2.0"))

# ---------------------------------------------------------------------------
# Unified char-based lifecycle stage thresholds
# ---------------------------------------------------------------------------
PROXY_CHARS_GROWTH = int(os.environ.get(
    "PROXY_CHARS_GROWTH", "80000" if IS_CLOUD else "40000"))
PROXY_CHARS_EXPANSION = int(os.environ.get(
    "PROXY_CHARS_EXPANSION", "200000" if IS_CLOUD else "90000"))
PROXY_CHARS_SATURATION = int(os.environ.get(
    "PROXY_CHARS_SATURATION",
    os.environ.get("PROXY_CTX_CHARS_LIMIT", _default("PROXY_CTX_CHARS_LIMIT", "500000", "180000"))))
PROXY_CHARS_OOM_DANGER = int(os.environ.get(
    "PROXY_CHARS_OOM_DANGER", "1000000" if IS_CLOUD else "350000"))

# ---------------------------------------------------------------------------
# Output token control
# ---------------------------------------------------------------------------
PROXY_MAX_TOKENS_OVERRIDE = int(os.environ.get("PROXY_MAX_TOKENS_OVERRIDE", "0"))
PROXY_OUTPUT_TOKEN_LIMIT_RATIO = float(os.environ.get("PROXY_OUTPUT_TOKEN_LIMIT_RATIO", "2.0"))
PROXY_BACKEND_TIMEOUT = int(os.environ.get("PROXY_BACKEND_TIMEOUT", "600"))

# DEF-001: hard ceiling for total payload size.
# Cloud backends (DeepSeek/OpenAI) support 1M+ tokens, so pre_truncate is
# effectively disabled (10M chars threshold). Local backends cap at 200K
# to prevent Metal OOM.
_default_oom = _strategy.get_default("PROXY_OOM_SAFE_CHARS", "200000")
# Treat empty string like unset (manage.sh may pass PROXY_OOM_SAFE_CHARS=""
# when neither PROXY_OOM_SAFE_CHARS nor PROXY_PRE_TRUNCATE_CHARS is defined).
# `int("")` raises ValueError, so use `or` to fall through to the default.
PROXY_OOM_SAFE_CHARS = int(
    os.environ.get("PROXY_OOM_SAFE_CHARS")
    or os.environ.get("PROXY_PRE_TRUNCATE_CHARS")
    or _default_oom
)
PROXY_PRE_TRUNCATE_CHARS = PROXY_OOM_SAFE_CHARS  # Legacy alias

# P0: Hard limit on request body size
PROXY_MAX_REQUEST_BYTES = int(os.environ.get("PROXY_MAX_REQUEST_BYTES", str(500 * 1024)))

# DEF-005: estimated prompt token limit
PROXY_OOM_SAFE_TOKENS = int(os.environ.get("PROXY_OOM_SAFE_TOKENS", "60000"))

# DEF-001 retry
PROXY_RETRY_AFTER_SECONDS = int(os.environ.get("PROXY_RETRY_AFTER_SECONDS", "30"))

# ---------------------------------------------------------------------------
# Phase 3: dynamic token estimation by content type
# ---------------------------------------------------------------------------
PROXY_TOKEN_RATIO_CHINESE = float(os.environ.get("PROXY_TOKEN_RATIO_CHINESE", "1.5"))
PROXY_TOKEN_RATIO_ENGLISH = float(os.environ.get("PROXY_TOKEN_RATIO_ENGLISH", "4.0"))
PROXY_TOKEN_RATIO_CODE = float(os.environ.get("PROXY_TOKEN_RATIO_CODE", "3.0"))

# ---------------------------------------------------------------------------
# Phase 3: memory pressure active rejection
# ---------------------------------------------------------------------------
PROXY_MEMORY_REJECT_THRESHOLD = float(os.environ.get(
    "PROXY_MEMORY_REJECT_THRESHOLD", "95" if IS_CLOUD else "90"))

# ---------------------------------------------------------------------------
# Phase 3: dynamic max_tokens
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
# Loop detection
# ---------------------------------------------------------------------------
PROXY_LOOP_THRESHOLD = int(os.environ.get("PROXY_LOOP_THRESHOLD", "3"))
PROXY_LOOP_LEVEL2 = int(os.environ.get("PROXY_LOOP_LEVEL2", str(PROXY_LOOP_THRESHOLD * 2)))
PROXY_LOOP_LEVEL3 = int(os.environ.get("PROXY_LOOP_LEVEL3", str(PROXY_LOOP_THRESHOLD * 3)))

# Text output loop detection
PROXY_TEXT_LOOP_ENABLED = os.environ.get("PROXY_TEXT_LOOP_ENABLED", "true").lower() in ("true", "1", "yes")
PROXY_TEXT_LOOP_THRESHOLD = int(os.environ.get("PROXY_TEXT_LOOP_THRESHOLD", "3"))
PROXY_TEXT_LOOP_MIN_CHARS = int(os.environ.get("PROXY_TEXT_LOOP_MIN_CHARS", "100"))
PROXY_TEXT_LOOP_SIMILARITY = float(os.environ.get("PROXY_TEXT_LOOP_SIMILARITY", "0.85"))

# Session continuation
PROXY_SESSION_CONTINUATION_ENABLED = os.environ.get(
    "PROXY_SESSION_CONTINUATION_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_SESSION_CONTINUATION_MIN_REQUESTS = int(os.environ.get(
    "PROXY_SESSION_CONTINUATION_MIN_REQUESTS", "2"))

# Dedup
PROXY_DEDUP_WINDOW = int(os.environ.get("PROXY_DEDUP_WINDOW", "2"))
_DEDUP_CACHE = {}

# Phase 3: sliding windows for dynamic concurrency control
_LATENCY_WINDOW = collections.deque(maxlen=50)
_ERROR_WINDOW = collections.deque(maxlen=50)
# Phase 3+ (建议3): per-backend latency deques — segmented p95 by route_target
_LATENCY_BY_TARGET = {}  # {'local': deque(maxlen=100), 'cloud': deque(maxlen=100)}

# Phase 3: metrics schema v1 fixed field set
_METRICS_V1_FIELDS = {
    "schema_version", "ts", "session_id", "input_msgs", "input_chars",
    "input_tools", "output_chars", "duration_ms", "status", "error_type",
    "error", "pipeline", "quality_flags", "compression_ratio", "token_ratio",
    "est_input_tokens", "est_output_tokens", "memory_rejected", "used_pct",
    "max_tokens_original", "max_tokens_dynamic", "snapshot_written",
    "dynamic_concurrent", "tools",
}

# ---------------------------------------------------------------------------
# Re-read prevention
# ---------------------------------------------------------------------------
PROXY_REREAD_PREVIEW_CHARS = int(os.environ.get("PROXY_REREAD_PREVIEW_CHARS", "200"))

# ---------------------------------------------------------------------------
# Blocker detection
# ---------------------------------------------------------------------------
PROXY_BLOCKER_ENABLED = os.environ.get("PROXY_BLOCKER_ENABLED", "true" if not IS_CLOUD else "false").lower() in ("1", "true", "yes")
PROXY_BLOCKER_THRESHOLD = int(os.environ.get("PROXY_BLOCKER_THRESHOLD", "2"))

_BLOCKER_ERROR_MARKERS = (
    ("wasted",            ["该文件自上次读取后未发生变化", "wasted call"]),
    ("file_not_found",    ["文件不存在", "file does not exist", "no such file"]),
    ("input_validation",  ["工具调用参数错误", "inputvalidationerror"]),
)

# ---------------------------------------------------------------------------
# Dynamic tool definition filtering
# ---------------------------------------------------------------------------
PROXY_TOOL_FILTER_ENABLED = os.environ.get("PROXY_TOOL_FILTER_ENABLED", "true" if not IS_CLOUD else "false").lower() in ("1", "true", "yes")
PROXY_TOOL_FILTER_MAX = int(os.environ.get("PROXY_TOOL_FILTER_MAX", "20"))
PROXY_TOOL_FILTER_RECENT = int(os.environ.get("PROXY_TOOL_FILTER_RECENT", "5"))
TOOL_ALWAYS_KEEP = (
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "LS", "Task", "WebFetch", "WebSearch",
    "TodoRead", "TodoWrite",
    "Skill", "Agent", "NotebookEdit",
    "EnterPlanMode", "ExitPlanMode",
    "AskUserQuestion",
    "mcp__searxng__search",
    "mcp__serper__google_search",
    "mcp__wechat-search__search_wechat",
)

# ---------------------------------------------------------------------------
# Keyword index (BM25 MVP)
# ---------------------------------------------------------------------------
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
# LLM compression cache (used by truncation module)
_summary_cache = {}
_summary_cache_lock = threading.Lock()
_SUMMARY_CACHE_MAX_SESSIONS = 10
_SUMMARY_CACHE_MAX_CHARS = 3000

TOOL_RESULT_HIGH_VALUE_PATTERNS = [
    (re.compile(r'(function |class |def |import |from |\{\s*"[a-z]|\#include)', re.IGNORECASE), 3),
    (re.compile(r'(total \d+|drwx|\.py$|\.js$|\.ts$)', re.IGNORECASE), 1),
    (re.compile(r'(error|traceback|exception)', re.IGNORECASE), 2),
    (re.compile(r'Wasted call', re.IGNORECASE), 0),
]

# ---------------------------------------------------------------------------
# Incremental summary cache (used by truncation.py)
# ---------------------------------------------------------------------------
_summary_cache = {}
_summary_cache_lock = threading.Lock()
_SUMMARY_CACHE_MAX_SESSIONS = 10
_SUMMARY_CACHE_MAX_CHARS = 3000

# ---------------------------------------------------------------------------
# Structured request logging
# ---------------------------------------------------------------------------
_LOG_DIR = os.path.join(_SCRIPT_DIR, "logs")
_LOG_PATH = os.path.join(_LOG_DIR, "llama-server.log")
_JSONL_PATH = os.path.join(_LOG_DIR, "proxy_requests.jsonl")
_jsonl_lock = threading.Lock()
_jsonl_output_map = {}
_jsonl_counter = 0

# ---------------------------------------------------------------------------
# Intelligent model routing
# ---------------------------------------------------------------------------
PROXY_ROUTE_ENABLED = os.environ.get("PROXY_ROUTE_ENABLED", "false").lower() in ("1", "true", "yes")
PROXY_ROUTE_THRESHOLD_CHARS = int(os.environ.get("PROXY_ROUTE_THRESHOLD_CHARS", "90000"))
PROXY_CLOUD_BASE_URL = os.environ.get("PROXY_CLOUD_BASE_URL", "https://api.deepseek.com/v1")
PROXY_CLOUD_API_KEY = os.environ.get("PROXY_CLOUD_API_KEY", "")
PROXY_CLOUD_MODEL = os.environ.get("PROXY_CLOUD_MODEL", "deepseek-v4-flash")
PROXY_ROUTE_CLOUD_CONCURRENT = int(os.environ.get("PROXY_ROUTE_CLOUD_CONCURRENT", "2"))
PROXY_ROUTE_MEMORY_PCT = int(os.environ.get("PROXY_ROUTE_MEMORY_PCT", "90"))
PROXY_ROUTE_FALLBACK_ENABLED = os.environ.get("PROXY_ROUTE_FALLBACK_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_ROUTE_MAX_CLOUD_FAILS = int(os.environ.get("PROXY_ROUTE_MAX_CLOUD_FAILS", "3"))
PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS = int(os.environ.get("PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", "1800"))
PROXY_CLOUD_PRICE_INPUT = float(os.environ.get("PROXY_CLOUD_PRICE_INPUT", "0.5"))
PROXY_CLOUD_PRICE_OUTPUT = float(os.environ.get("PROXY_CLOUD_PRICE_OUTPUT", "1.5"))
PROXY_ROUTE_SENSITIVE_PATTERNS = os.environ.get("PROXY_ROUTE_SENSITIVE_PATTERNS", "")
PROXY_ROUTE_PROFILE = os.environ.get("PROXY_ROUTE_PROFILE", "")
PROXY_ROUTE_DAILY_BUDGET = float(os.environ.get("PROXY_ROUTE_DAILY_BUDGET", "0"))
# Optional hard-stop: when true, block new cloud requests once daily budget is reached.
# When false, cloud routing continues and /status still shows tiered budget alerts.
PROXY_ROUTE_DAILY_BUDGET_HARD_STOP = os.environ.get(
    "PROXY_ROUTE_DAILY_BUDGET_HARD_STOP", "true"
).lower() in ("1", "true", "yes")
# Tiered budget alert thresholds (percentage) shown on /status. Comma-separated.
PROXY_ROUTE_BUDGET_ALERT_TIERS = os.environ.get("PROXY_ROUTE_BUDGET_ALERT_TIERS", "50,80,100")
# --- Sticky session routing (建议1) ---
# When sticky=true (default), a session routed to cloud stays cloud forever.
# When sticky=false, allow a cloud session to return to local if context drops
# below threshold for PROXY_ROUTE_STICKY_RETURN_ROUNDS consecutive requests.
PROXY_ROUTE_STICKY = os.environ.get("PROXY_ROUTE_STICKY", "true").lower() in ("1", "true", "yes")
PROXY_ROUTE_STICKY_RETURN_ROUNDS = int(os.environ.get("PROXY_ROUTE_STICKY_RETURN_ROUNDS", "5"))
PROXY_ROUTE_STICKY_RETURN_RATIO = float(os.environ.get("PROXY_ROUTE_STICKY_RETURN_RATIO", "0.7"))
# Per-session counter of consecutive cloud requests that fell below the
# threshold (cleared when session returns to local or session_route changes).
_SESSION_BELOW_THRESHOLD: dict = {}  # {session_id: int count}

# Cached compiled regex for sensitive path detection.
# Rebuilt when PROXY_ROUTE_SENSITIVE_PATTERNS changes (via reload or init).
_SENSITIVE_PATTERNS_RE = None
_SENSITIVE_PATTERNS_SOURCE = ""


def _compile_sensitive_patterns():
    """Compile and cache sensitive path patterns regex.

    Returns a compiled regex object, or None if no patterns configured.
    Invalid patterns are logged and ignored.
    """
    global _SENSITIVE_PATTERNS_RE, _SENSITIVE_PATTERNS_SOURCE
    patterns_str = PROXY_ROUTE_SENSITIVE_PATTERNS
    if patterns_str == _SENSITIVE_PATTERNS_SOURCE and _SENSITIVE_PATTERNS_RE is not None:
        return _SENSITIVE_PATTERNS_RE

    _SENSITIVE_PATTERNS_SOURCE = patterns_str
    if not patterns_str:
        _SENSITIVE_PATTERNS_RE = None
        return None

    raw_patterns = [p.strip() for p in patterns_str.split(",") if p.strip()]
    if not raw_patterns:
        _SENSITIVE_PATTERNS_RE = None
        return None

    # Treat each configured fragment as a literal substring by default.
    # This avoids regex-injection surprises from user config.
    escaped = [re.escape(p) for p in raw_patterns]
    try:
        _SENSITIVE_PATTERNS_RE = re.compile("|".join(escaped), re.IGNORECASE)
    except re.error as e:
        # Should not happen after escaping, but keep fallback.
        print(f"[proxy_state] Invalid sensitive patterns: {e}")
        _SENSITIVE_PATTERNS_RE = None
    return _SENSITIVE_PATTERNS_RE


def invalidate_sensitive_patterns_cache():
    """Force recompilation of sensitive path regex (called on SIGHUP reload)."""
    global _SENSITIVE_PATTERNS_RE, _SENSITIVE_PATTERNS_SOURCE
    _SENSITIVE_PATTERNS_RE = None
    _SENSITIVE_PATTERNS_SOURCE = ""

# Model ID → route preference mapping (preference only, safety always overrides)
MODEL_ROUTE_PREFERENCES = {
    "claude-sonnet-4-6": {
        "route_bias": "auto",
        "threshold_factor": 1.0,
        "memory_bias": 0,
        "cloud_model": PROXY_CLOUD_MODEL,
    },
    "claude-opus-4-7": {
        "route_bias": "prefer_cloud",
        "threshold_factor": 0.8,
        "memory_bias": -5,
        "cloud_model": "deepseek-v4-pro",
    },
    "claude-haiku-4-5": {
        "route_bias": "prefer_local",
        "threshold_factor": 1.33,
        "memory_bias": 0,
        "cloud_model": PROXY_CLOUD_MODEL,
    },
}

# ---------------------------------------------------------------------------
# Structured metrics logging
# ---------------------------------------------------------------------------
PROXY_METRICS_ENABLED = os.environ.get("PROXY_METRICS_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_METRICS_DIR = os.environ.get("PROXY_METRICS_DIR", "logs")
_METRICS_PATH = os.path.join(_SCRIPT_DIR, PROXY_METRICS_DIR, "proxy_metrics.jsonl")
_metrics_lock = threading.Lock()
_state_lock = threading.Lock()

# Cloud concurrency lock (rebuilt on SIGHUP if PROXY_ROUTE_CLOUD_CONCURRENT changes)
_cloud_lock = threading.Semaphore(PROXY_ROUTE_CLOUD_CONCURRENT)

# Session-level routing state (all access under _state_lock)
_SESSION_ROUTE_MAP: dict[str, str] = {}             # session_id → "local"|"cloud"|"local_forced"
_SESSION_ROUTE_FORCE_SOURCE: dict[str, str] = {}     # session_id → "cloud_failures"|"user_manual"
_cloud_fail_count: dict[str, int] = {}               # session_id → int
_cloud_cooldown_start: dict[str, float] = {}          # session_id → monotonic timestamp
_ROUTE_NOTIFIED_SESSIONS: set[str] = set()           # sessions already shown route switch notice

# Daily cloud cost tracking (all access under _state_lock)
_route_daily_cost: float = 0.0
_route_daily_date: str = ""   # YYYY-MM-DD, cross-day auto-reset


def _accumulate_route_daily_cost(input_tokens: int = 0, output_tokens: int = 0) -> float:
    """Atomically add estimated cloud API cost and return new daily total.

    Tokens are estimated; output_tokens may be max_tokens upper-bound for streaming.
    Cost is in CNY. Cross-day reset is handled automatically.
    """
    global _route_daily_cost, _route_daily_date
    today = time.strftime("%Y-%m-%d")
    with _state_lock:
        if _route_daily_date != today:
            _route_daily_date = today
            _route_daily_cost = 0.0
        input_cost = input_tokens * PROXY_CLOUD_PRICE_INPUT / 1_000_000
        output_cost = output_tokens * PROXY_CLOUD_PRICE_OUTPUT / 1_000_000
        _route_daily_cost += input_cost + output_cost
        return _route_daily_cost


def _parse_budget_alert_tiers() -> tuple:
    """Parse PROXY_ROUTE_BUDGET_ALERT_TIERS into sorted integer thresholds.

    Invalid values are ignored; default returns (50, 80, 100).
    """
    raw = PROXY_ROUTE_BUDGET_ALERT_TIERS
    if not raw:
        return (50, 80, 100)
    tiers = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            val = int(part)
            if 0 < val <= 10000:
                tiers.append(val)
        except ValueError:
            continue
    return tuple(sorted(set(tiers))) if tiers else (50, 80, 100)


def _get_budget_alert_level(used_pct: float) -> str:
    """Return alert level based on configured budget tiers.

    Levels: "" (none), "warning" (crossed first tier), "danger" (second tier),
    "critical" (third/highest tier).
    """
    tiers = _parse_budget_alert_tiers()
    if not tiers or used_pct <= 0:
        return ""
    if used_pct >= tiers[-1]:
        return "critical"
    if len(tiers) >= 2 and used_pct >= tiers[-2]:
        return "danger"
    if used_pct >= tiers[0]:
        return "warning"
    return ""


# Model aliases cache (rebuilt on SIGHUP via invalidate_model_aliases_cache())
_MODEL_ALIASES_CACHE = None


def get_model_aliases():
    """Return stable Agent-facing model aliases. Never exposes MODEL_NAME.
    Thread-safe: uses double-checked locking under _state_lock.
    """
    global _MODEL_ALIASES_CACHE
    if _MODEL_ALIASES_CACHE is not None:
        return _MODEL_ALIASES_CACHE
    with _state_lock:
        if _MODEL_ALIASES_CACHE is not None:
            return _MODEL_ALIASES_CACHE
        aliases = [
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "default",
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
            "claude-3-5-haiku-20241022",
        ]
        if IS_CLOUD or PROXY_ROUTE_ENABLED:
            aliases.append("claude-opus-4-7")
        _MODEL_ALIASES_CACHE = aliases
        return aliases


def invalidate_model_aliases_cache():
    """Invalidate the model aliases cache (called on SIGHUP reload)."""
    global _MODEL_ALIASES_CACHE
    _MODEL_ALIASES_CACHE = None


# Legacy static list — kept for backward compat in reload_config.py / tests.
# New code should call get_model_aliases().
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

# Thread-local context for per-request logging
_log_ctx = threading.local()

# Thread-local context for per-request metrics collection
_metrics_ctx = threading.local()

# ---------------------------------------------------------------------------
# SIGHUP hot-reload infrastructure
# ---------------------------------------------------------------------------
_RELOAD_LOCK = threading.Lock()
RELOAD_CONFIG_PATH = os.environ.get(
    "PROXY_RELOAD_CONFIG",
    os.path.join(_SCRIPT_DIR, "configs", "active.conf"),
)
RELOAD_SECRET_PATH = os.environ.get(
    "PROXY_RELOAD_SECRET",
    os.path.join(_SCRIPT_DIR, "configs", "secret.local.conf"),
)

# Tier 1 scalars reloaded from conf. Format:
#   (env_key, python_attr_name, cast, cloud_default, local_default)
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
    # Semantic compression (Phase 2)
    ("PROXY_COMPRESS_ENABLED", "PROXY_COMPRESS_ENABLED", "bool", "false", "true"),
    ("PROXY_COMPRESS_THRESHOLD", "PROXY_COMPRESS_THRESHOLD", "int", "4096", "4096"),
    ("PROXY_COMPRESS_MODE", "PROXY_COMPRESS_MODE", "str", "semantic", "semantic"),
    ("PROXY_SCRUB_ANSI", "PROXY_SCRUB_ANSI", "bool", "true", "true"),
    ("PROXY_SIEVE_JSON_MAX_ITEMS", "PROXY_SIEVE_JSON_MAX_ITEMS", "int", "10", "10"),
    ("PROXY_SIEVE_JSON_MAX_STR_LEN", "PROXY_SIEVE_JSON_MAX_STR_LEN", "int", "200", "200"),
    ("PROXY_SIEVE_JSON_MAX_DEPTH", "PROXY_SIEVE_JSON_MAX_DEPTH", "int", "4", "4"),
    ("PROXY_DEDUPE_SCALARS", "PROXY_DEDUPE_SCALARS", "bool", "false", "false"),
    ("PROXY_LOG_DEDUPE", "PROXY_LOG_DEDUPE", "bool", "true", "true"),
    ("PROXY_COMPRESS_AUDIT", "PROXY_COMPRESS_AUDIT", "bool", "true", "true"),
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
    ("PROXY_BACKEND_TIMEOUT", "PROXY_BACKEND_TIMEOUT", "int", "600", "600"),
    ("PROXY_OOM_SAFE_TOKENS", "PROXY_OOM_SAFE_TOKENS", "int", "60000", "60000"),
    ("PROXY_RETRY_AFTER_SECONDS", "PROXY_RETRY_AFTER_SECONDS", "int", "30", "30"),
    ("PROXY_MAX_REQUEST_BYTES", "PROXY_MAX_REQUEST_BYTES", "int", str(500 * 1024), str(500 * 1024)),
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
    # Route
    ("PROXY_ROUTE_ENABLED", "PROXY_ROUTE_ENABLED", "bool", "false", "false"),
    ("PROXY_ROUTE_THRESHOLD_CHARS", "PROXY_ROUTE_THRESHOLD_CHARS", "int", "90000", "90000"),
    ("PROXY_CLOUD_BASE_URL", "PROXY_CLOUD_BASE_URL", "str", "https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
    ("PROXY_CLOUD_MODEL", "PROXY_CLOUD_MODEL", "str", "deepseek-v4-flash", "deepseek-v4-flash"),
    ("PROXY_ROUTE_CLOUD_CONCURRENT", "PROXY_ROUTE_CLOUD_CONCURRENT", "int", "2", "2"),
    ("PROXY_ROUTE_MEMORY_PCT", "PROXY_ROUTE_MEMORY_PCT", "int", "90", "90"),
    ("PROXY_ROUTE_FALLBACK_ENABLED", "PROXY_ROUTE_FALLBACK_ENABLED", "bool", "true", "true"),
    ("PROXY_ROUTE_MAX_CLOUD_FAILS", "PROXY_ROUTE_MAX_CLOUD_FAILS", "int", "3", "3"),
    ("PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", "int", "1800", "1800"),
    ("PROXY_CLOUD_PRICE_INPUT", "PROXY_CLOUD_PRICE_INPUT", "float", "0.5", "0.5"),
    ("PROXY_CLOUD_PRICE_OUTPUT", "PROXY_CLOUD_PRICE_OUTPUT", "float", "1.5", "1.5"),
    ("PROXY_ROUTE_SENSITIVE_PATTERNS", "PROXY_ROUTE_SENSITIVE_PATTERNS", "str", "", ""),
    ("PROXY_ROUTE_DAILY_BUDGET", "PROXY_ROUTE_DAILY_BUDGET", "float", "0", "0"),
    ("PROXY_ROUTE_DAILY_BUDGET_HARD_STOP", "PROXY_ROUTE_DAILY_BUDGET_HARD_STOP", "bool", "true", "true"),
    ("PROXY_ROUTE_BUDGET_ALERT_TIERS", "PROXY_ROUTE_BUDGET_ALERT_TIERS", "str", "50,80,100", "50,80,100"),
    ("PROXY_ROUTE_PROFILE", "PROXY_ROUTE_PROFILE", "str", "", ""),
    ("PROXY_ROUTE_STICKY", "PROXY_ROUTE_STICKY", "bool", "true", "true"),
    ("PROXY_ROUTE_STICKY_RETURN_ROUNDS", "PROXY_ROUTE_STICKY_RETURN_ROUNDS", "int", "5", "5"),
    ("PROXY_ROUTE_STICKY_RETURN_RATIO", "PROXY_ROUTE_STICKY_RETURN_RATIO", "float", "0.7", "0.7"),
]


# ---------------------------------------------------------------------------
# Config helper functions
# ---------------------------------------------------------------------------

def _parse_conf_env(path):
    """Parse a bash-style KEY="value" config file into a dict.

    Handles double/single quotes, comments (#), and blank lines.
    Strips inline comments (space + #) after quoted values.
    Does NOT evaluate shell expansions.
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
                if len(val) >= 2 and val[0] in ('"', "'"):
                    quote = val[0]
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


# ---------------------------------------------------------------------------
# All public names exportable via `from proxy_state import *`
# ---------------------------------------------------------------------------
__all__ = [
    # Backend
    "LLAMA_BASE", "LLAMA_API_KEY", "BACKEND_TYPE", "IS_CLOUD", "_strategy", "_SCRIPT_DIR",
    # Concurrency
    "PROXY_MAX_CONCURRENT", "_llama_lock", "MODEL_NAME",
    # Tool-result clearing
    "PROXY_CLEAR_ENABLED", "PROXY_CLEAR_THRESHOLD", "PROXY_TOOL_KEEP",
    "PROXY_FROZEN_HEAD", "PROXY_CLEAR_TAIL_FIRST",
    # Cache aligner
    "PROXY_CACHE_ALIGN_ENABLED", "PROXY_CACHE_ALIGN_HEAD",
    # Shared state
    "_SESSION_LAST_MESSAGES", "_LOOP_SESSION_STATE", "_SESSION_REQUEST_COUNT",
    # Compression
    "PROXY_COMPRESS_ENABLED", "PROXY_COMPRESS_THRESHOLD", "PROXY_COMPRESS_MODE",
    "PROXY_SCRUB_ANSI", "PROXY_SIEVE_JSON_MAX_ITEMS", "PROXY_SIEVE_JSON_MAX_STR_LEN",
    "PROXY_SIEVE_JSON_MAX_DEPTH", "PROXY_LOG_DEDUPE", "PROXY_DEDUPE_SCALARS",
    "PROXY_COMPRESS_AUDIT", "CONTENT_TOOLS_FALLBACK_ENABLED",
    # Context truncation
    "PROXY_CTX_LIMIT_ENABLED", "PROXY_CTX_CHARS_LIMIT", "PROXY_CTX_KEEP_HEAD",
    "PROXY_CTX_KEEP_TAIL", "PROXY_CTX_TRUNCATE_STRATEGY", "PROXY_CTX_KEEP_ROUNDS",
    "PROXY_CTX_KEEP_MESSAGES", "PROXY_CTX_TOKEN_BUDGET", "PROXY_CTX_TOKEN_RATIO",
    # Lifecycle thresholds
    "PROXY_CHARS_GROWTH", "PROXY_CHARS_EXPANSION", "PROXY_CHARS_SATURATION",
    "PROXY_CHARS_OOM_DANGER",
    # Output control
    "PROXY_MAX_TOKENS_OVERRIDE", "PROXY_OUTPUT_TOKEN_LIMIT_RATIO",
    "PROXY_BACKEND_TIMEOUT", "PROXY_OOM_SAFE_CHARS", "PROXY_PRE_TRUNCATE_CHARS",
    "PROXY_MAX_REQUEST_BYTES", "PROXY_OOM_SAFE_TOKENS", "PROXY_RETRY_AFTER_SECONDS",
    # Token ratios
    "PROXY_TOKEN_RATIO_CHINESE", "PROXY_TOKEN_RATIO_ENGLISH", "PROXY_TOKEN_RATIO_CODE",
    # Memory
    "PROXY_MEMORY_REJECT_THRESHOLD",
    # Dynamic max_tokens
    "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", "PROXY_DYNAMIC_MAX_TOKENS_INIT",
    "PROXY_DYNAMIC_MAX_TOKENS_GROWTH", "PROXY_DYNAMIC_MAX_TOKENS_SATURATION",
    "PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO",
    # Snapshots
    "PROXY_SNAPSHOT_ENABLED", "PROXY_SNAPSHOT_MAX_FILES",
    # Dynamic concurrency
    "PROXY_DYNAMIC_CONCURRENT_ENABLED", "PROXY_DYNAMIC_CONCURRENT_MIN",
    "PROXY_DYNAMIC_CONCURRENT_MAX", "PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS",
    "PROXY_DYNAMIC_CONCURRENT_ERROR_RATE",
    # Loop detection
    "PROXY_LOOP_THRESHOLD", "PROXY_LOOP_LEVEL2", "PROXY_LOOP_LEVEL3",
    "PROXY_TEXT_LOOP_ENABLED", "PROXY_TEXT_LOOP_THRESHOLD", "PROXY_TEXT_LOOP_MIN_CHARS",
    "PROXY_TEXT_LOOP_SIMILARITY",
    # Session continuation
    "PROXY_SESSION_CONTINUATION_ENABLED", "PROXY_SESSION_CONTINUATION_MIN_REQUESTS",
    # Dedup
    "PROXY_DEDUP_WINDOW", "_DEDUP_CACHE",
    # Sliding windows
    "_LATENCY_WINDOW", "_ERROR_WINDOW", "_LATENCY_BY_TARGET", "_METRICS_V1_FIELDS",
    # Re-read
    "PROXY_REREAD_PREVIEW_CHARS",
    # Blocker
    "PROXY_BLOCKER_ENABLED", "PROXY_BLOCKER_THRESHOLD", "_BLOCKER_ERROR_MARKERS",
    # Tool filter
    "PROXY_TOOL_FILTER_ENABLED", "PROXY_TOOL_FILTER_MAX", "PROXY_TOOL_FILTER_RECENT",
    "TOOL_ALWAYS_KEEP",
    # Keyword index
    "PROXY_HISTORY_INDEX", "PROXY_HISTORY_TOP_K", "PROXY_HISTORY_MAX_CHARS",
    # Semantic priority
    "TOOL_SEMANTIC_PRIORITY", "TOOL_RESULT_HIGH_VALUE_PATTERNS",
    "_summary_cache", "_summary_cache_lock", "_SUMMARY_CACHE_MAX_SESSIONS", "_SUMMARY_CACHE_MAX_CHARS",
    # Logging
    "_LOG_DIR", "_LOG_PATH", "_JSONL_PATH", "_jsonl_lock", "_jsonl_output_map", "_jsonl_counter",
    "PROXY_METRICS_ENABLED", "PROXY_METRICS_DIR", "_METRICS_PATH", "_metrics_lock",
    "_state_lock", "MODEL_ALIASES", "_log_ctx", "_metrics_ctx",
    # Summary cache
    "_summary_cache", "_summary_cache_lock", "_SUMMARY_CACHE_MAX_SESSIONS", "_SUMMARY_CACHE_MAX_CHARS",
    # Reload
    "_RELOAD_LOCK", "RELOAD_CONFIG_PATH", "RELOAD_SECRET_PATH", "_RELOAD_SPEC",
    # Config helpers
    "_parse_conf_env", "_cast_config_value",
    # Intelligent model routing
    "PROXY_ROUTE_ENABLED", "PROXY_ROUTE_THRESHOLD_CHARS",
    "PROXY_CLOUD_BASE_URL", "PROXY_CLOUD_API_KEY", "PROXY_CLOUD_MODEL",
    "PROXY_ROUTE_CLOUD_CONCURRENT", "PROXY_ROUTE_MEMORY_PCT",
    "PROXY_ROUTE_FALLBACK_ENABLED", "PROXY_ROUTE_MAX_CLOUD_FAILS",
    "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", "PROXY_CLOUD_PRICE_INPUT",
    "PROXY_CLOUD_PRICE_OUTPUT", "PROXY_ROUTE_SENSITIVE_PATTERNS",
    "PROXY_ROUTE_PROFILE", "PROXY_ROUTE_DAILY_BUDGET",
    "PROXY_ROUTE_DAILY_BUDGET_HARD_STOP", "PROXY_ROUTE_BUDGET_ALERT_TIERS",
    "PROXY_ROUTE_STICKY", "PROXY_ROUTE_STICKY_RETURN_ROUNDS",
    "PROXY_ROUTE_STICKY_RETURN_RATIO",
    "MODEL_ROUTE_PREFERENCES",
    "_cloud_lock", "_SESSION_ROUTE_MAP", "_SESSION_ROUTE_FORCE_SOURCE",
    "_cloud_fail_count", "_cloud_cooldown_start", "_ROUTE_NOTIFIED_SESSIONS",
    "_route_daily_cost", "_route_daily_date", "_SESSION_BELOW_THRESHOLD",
    "_MODEL_ALIASES_CACHE", "_SENSITIVE_PATTERNS_RE", "_SENSITIVE_PATTERNS_SOURCE",
    "get_model_aliases", "invalidate_model_aliases_cache",
    "_compile_sensitive_patterns", "invalidate_sensitive_patterns_cache",
    "_accumulate_route_daily_cost",
    "_parse_budget_alert_tiers", "_get_budget_alert_level",
]
