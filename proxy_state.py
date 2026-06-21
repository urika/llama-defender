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
import threading

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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
PROXY_OOM_SAFE_CHARS = int(os.environ.get(
    "PROXY_OOM_SAFE_CHARS",
    os.environ.get("PROXY_PRE_TRUNCATE_CHARS", _default_oom),
))
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
TOOL_RESULT_HIGH_VALUE_PATTERNS = [
    (re.compile(r'(function |class |def |import |from |\{\s*"[a-z]|\#include)', re.IGNORECASE), 3),
    (re.compile(r'(total \d+|drwx|\.py$|\.js$|\.ts$)', re.IGNORECASE), 1),
    (re.compile(r'(error|traceback|exception)', re.IGNORECASE), 2),
    (re.compile(r'Wasted call', re.IGNORECASE), 0),
]

# ---------------------------------------------------------------------------
# Structured request logging
# ---------------------------------------------------------------------------
_LOG_DIR = os.path.join(_SCRIPT_DIR, "logs")
_JSONL_PATH = os.path.join(_LOG_DIR, "proxy_requests.jsonl")
_jsonl_lock = threading.Lock()
_jsonl_output_map = {}
_jsonl_counter = 0

# ---------------------------------------------------------------------------
# Structured metrics logging
# ---------------------------------------------------------------------------
PROXY_METRICS_ENABLED = os.environ.get("PROXY_METRICS_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_METRICS_DIR = os.environ.get("PROXY_METRICS_DIR", "logs")
_METRICS_PATH = os.path.join(_SCRIPT_DIR, PROXY_METRICS_DIR, "proxy_metrics.jsonl")
_metrics_lock = threading.Lock()
_state_lock = threading.Lock()

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
    "LLAMA_BASE", "LLAMA_API_KEY", "BACKEND_TYPE", "IS_CLOUD", "_strategy",
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
    "_LATENCY_WINDOW", "_ERROR_WINDOW", "_METRICS_V1_FIELDS",
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
    # Logging
    "_LOG_DIR", "_JSONL_PATH", "_jsonl_lock", "_jsonl_output_map", "_jsonl_counter",
    "PROXY_METRICS_ENABLED", "PROXY_METRICS_DIR", "_METRICS_PATH", "_metrics_lock",
    "_state_lock", "MODEL_ALIASES", "_log_ctx", "_metrics_ctx",
    # Reload
    "_RELOAD_LOCK", "RELOAD_CONFIG_PATH", "RELOAD_SECRET_PATH", "_RELOAD_SPEC",
    # Config helpers
    "_parse_conf_env", "_cast_config_value",
]
