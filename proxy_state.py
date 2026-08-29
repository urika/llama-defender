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

import model_registry

# 配置统一阶段一：默认值唯一权威是 proxy_config.CONFIG_REGISTRY。
# proxy_config 对共享状态采用惰性 __getattr__ 转发，此处 import 不会形成循环依赖。
from proxy_config import get_default

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _run(cmd, timeout=3):
    """Run a shell command and return stripped stdout, or empty string on error."""
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=timeout).strip()
    except Exception:
        return ""


def _get_system_memory():
    """Return macOS system memory stats used by routing and status page.

    Uses `memory_pressure` (macOS native) instead of `vm_stat` for a more
    accurate system-wide memory pressure reading. The "free percentage" from
    memory_pressure accounts for page cache, purgeable pages, and compressor
    — not just wired+active pages — giving a better signal for Metal OOM risk.

    Keys: total_gb, used_gb, available_gb, used_pct (string like "85.3").
    """
    out = _run("memory_pressure")
    data = {}
    for line in out.splitlines():
        if "System-wide memory free percentage:" in line:
            free_pct = float(line.split(":")[1].strip().rstrip("%"))
            used_pct = 100.0 - free_pct
            total = 48.0
            data["total_gb"] = total
            data["used_gb"] = round(total * used_pct / 100, 1)
            data["available_gb"] = round(total * free_pct / 100, 1)
            data["used_pct"] = f"{used_pct:.1f}"
            return data
    data["total_gb"] = 48.0
    data["used_gb"] = 0.0
    data["available_gb"] = 48.0
    data["used_pct"] = "0.0"
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
# 注：保留用于向后兼容；新代码请直接使用顶部导入的 get_default()。
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
MODEL_NAME = os.environ.get("MODEL_NAME", _default("MODEL_NAME", "deepseek-v4-pro", "unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit"))

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

# DEF-104: Per-session tool frequency counter for auto-promote.
# Maps session_id → {tool_name: count}. Updated by _filter_tools after each request.
_SESSION_TOOL_FREQ: dict[str, dict[str, int]] = {}

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
PROXY_COMPRESSION_PROFILE = os.environ.get("PROXY_COMPRESSION_PROFILE", "balanced")

# TS-1: BM25 relevance-driven compression (W3 d3-d5)
PROXY_BM25_ENABLED = os.environ.get("PROXY_BM25_ENABLED", _default("PROXY_BM25_ENABLED", "false", "true")).lower() in ("1", "true", "yes")
PROXY_BM25_K1 = float(os.environ.get("PROXY_BM25_K1", "1.5"))
PROXY_BM25_B = float(os.environ.get("PROXY_BM25_B", "0.75"))
PROXY_BM25_KEEP_THRESHOLD = float(os.environ.get("PROXY_BM25_KEEP_THRESHOLD", "3.5"))
# TS-4 (2026-08-18 日志分析): 全史 bm25_scores 中位数 0.00/p90 0.18,旧默认 0.5
# 使几乎所有 tool_result 落入 drop 分支被 30% 截断;降至 0.1 收窄误伤面
# (drop 分支同时已改为类型感知结构化压缩,见 content_compressor._structured_compress)。
PROXY_BM25_DROP_THRESHOLD = float(os.environ.get("PROXY_BM25_DROP_THRESHOLD", "0.1"))
# TS-4: BM25 drop 分支结构化压缩后的封顶比例 (相对原文长度)。
PROXY_BM25_DROP_TARGET_RATIO = float(os.environ.get("PROXY_BM25_DROP_TARGET_RATIO", "0.45"))
PROXY_BM25_MIN_PREFIX = int(os.environ.get("PROXY_BM25_MIN_PREFIX", "4"))
PROXY_BM25_IDF_LRU_MAX = int(os.environ.get("PROXY_BM25_IDF_LRU_MAX", "10000"))

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
PROXY_CHARS_GROWTH = int(os.environ.get("PROXY_CHARS_GROWTH", get_default("PROXY_CHARS_GROWTH")))
PROXY_CHARS_EXPANSION = int(os.environ.get("PROXY_CHARS_EXPANSION", get_default("PROXY_CHARS_EXPANSION")))
PROXY_CHARS_SATURATION = int(os.environ.get("PROXY_CHARS_SATURATION", get_default("PROXY_CHARS_SATURATION")))
PROXY_CHARS_OOM_DANGER = int(os.environ.get("PROXY_CHARS_OOM_DANGER", get_default("PROXY_CHARS_OOM_DANGER")))

# ---------------------------------------------------------------------------
# Output token control
# ---------------------------------------------------------------------------
PROXY_MAX_TOKENS_OVERRIDE = int(os.environ.get("PROXY_MAX_TOKENS_OVERRIDE", "0"))
PROXY_OUTPUT_TOKEN_LIMIT_RATIO = float(os.environ.get("PROXY_OUTPUT_TOKEN_LIMIT_RATIO", "2.0"))
PROXY_BACKEND_TIMEOUT = int(os.environ.get("PROXY_BACKEND_TIMEOUT", "600"))
# 2026-08-27: 主动超时余量——非流式请求在"客户端超时−余量"处主动返回 504,
# 保证错误送达客户端(而非 CRITICAL: failed to send error)。取客户端
# X-Stainless-Timeout 头,缺省(非 stainless 客户端)时退回 PROXY_BACKEND_TIMEOUT。
PROXY_TIMEOUT_MARGIN_S = int(os.environ.get("PROXY_TIMEOUT_MARGIN_S", "30"))
# 2026-08-27: 流式 chunk 空闲看门狗——首 token 后若无 chunk 超过该秒数即中止
# 中继并取消后端在途生成(prefill/首 token 不受限,仅限流中 stall)。
PROXY_STREAM_IDLE_TIMEOUT_S = int(os.environ.get("PROXY_STREAM_IDLE_TIMEOUT_S", "30"))
# 2026-08-29 #51-B1: 大 payload 流式请求预发 SSE 头 + 冷 prefill 期间心跳注释行。
PROXY_SSE_HEARTBEAT_BYTES = int(os.environ.get("PROXY_SSE_HEARTBEAT_BYTES", "100000"))
PROXY_SSE_HEARTBEAT_S = float(os.environ.get("PROXY_SSE_HEARTBEAT_S", "15"))


class StreamIdleTimeout(Exception):
    """流式后端 chunk 空闲超时(首 token 后无 chunk 超过 PROXY_STREAM_IDLE_TIMEOUT_S)。

    由 _timed_stream_lines 抛出,BackendDispatcher 捕获后中止中继并关闭到后端的
    连接以取消在途生成(防流中 stall 拖满后端唯一 sequence)。
    """

# DEF-001: hard ceiling for total payload size.
# Cloud backends (DeepSeek/OpenAI) support 1M+ tokens, so pre_truncate is
# effectively disabled (10M chars threshold). Local backends cap at 200K
# to prevent Metal OOM.
_default_oom = get_default("PROXY_OOM_SAFE_CHARS")
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
# Cloud path can accept larger payloads because cloud providers (DeepSeek/OpenAI)
# handle their own OOM scheduling; this avoids rejecting requests that SmartRouter
# would otherwise route to cloud.
PROXY_CLOUD_MAX_REQUEST_BYTES = int(os.environ.get("PROXY_CLOUD_MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

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
    "PROXY_MEMORY_REJECT_THRESHOLD", get_default("PROXY_MEMORY_REJECT_THRESHOLD")))

# ---------------------------------------------------------------------------
# Phase 3: dynamic max_tokens
# ---------------------------------------------------------------------------
PROXY_DYNAMIC_MAX_TOKENS_ENABLED = os.environ.get(
    "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", "true" if not IS_CLOUD else "false").lower() in ("1", "true", "yes")
PROXY_DYNAMIC_MAX_TOKENS_INIT = int(os.environ.get("PROXY_DYNAMIC_MAX_TOKENS_INIT", "4096"))
PROXY_DYNAMIC_MAX_TOKENS_GROWTH = int(os.environ.get("PROXY_DYNAMIC_MAX_TOKENS_GROWTH", "4096"))
PROXY_DYNAMIC_MAX_TOKENS_SATURATION = int(os.environ.get("PROXY_DYNAMIC_MAX_TOKENS_SATURATION", "2048"))
PROXY_DYNAMIC_MAX_TOKENS_OOM = int(os.environ.get("PROXY_DYNAMIC_MAX_TOKENS_OOM", "4096"))
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
    "PROXY_DYNAMIC_CONCURRENT_ENABLED", get_default("PROXY_DYNAMIC_CONCURRENT_ENABLED")).lower() in ("1", "true", "yes")
PROXY_DYNAMIC_CONCURRENT_MIN = int(os.environ.get("PROXY_DYNAMIC_CONCURRENT_MIN", "1"))
PROXY_DYNAMIC_CONCURRENT_MAX = int(os.environ.get(
    "PROXY_DYNAMIC_CONCURRENT_MAX", get_default("PROXY_DYNAMIC_CONCURRENT_MAX")))
PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS = float(os.environ.get(
    "PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS", "30000"))
PROXY_DYNAMIC_CONCURRENT_ERROR_RATE = float(os.environ.get(
    "PROXY_DYNAMIC_CONCURRENT_ERROR_RATE", "0.2"))

# ---------------------------------------------------------------------------
# 请求优先级队列（Phase 1：默认关闭，叠加在 _llama_lock 之上，不替代它）
# ---------------------------------------------------------------------------
PROXY_QUEUE_ENABLED = os.environ.get(
    "PROXY_QUEUE_ENABLED", get_default("PROXY_QUEUE_ENABLED")).lower() in ("1", "true", "yes")
PROXY_QUEUE_TIMEOUT_SECONDS = int(os.environ.get(
    "PROXY_QUEUE_TIMEOUT_SECONDS", get_default("PROXY_QUEUE_TIMEOUT_SECONDS")))
PROXY_QUEUE_LARGE_THRESHOLD_CHARS = int(os.environ.get(
    "PROXY_QUEUE_LARGE_THRESHOLD_CHARS", get_default("PROXY_QUEUE_LARGE_THRESHOLD_CHARS")))
PROXY_QUEUE_HUGE_THRESHOLD_CHARS = int(os.environ.get(
    "PROXY_QUEUE_HUGE_THRESHOLD_CHARS", get_default("PROXY_QUEUE_HUGE_THRESHOLD_CHARS")))
PROXY_QUEUE_HUGE_ACTION = os.environ.get(
    "PROXY_QUEUE_HUGE_ACTION", get_default("PROXY_QUEUE_HUGE_ACTION"))

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

# DEF-109: Context-size-based loop tiers
PROXY_LOOP_CHARS_LONG = int(os.environ.get("PROXY_LOOP_CHARS_LONG", "50000"))
PROXY_LOOP_CHARS_VERY_LONG = int(os.environ.get("PROXY_LOOP_CHARS_VERY_LONG", "100000"))

# Phase 4 (建议4): Session-tier dynamic loop thresholds
PROXY_LOOP_SESSION_SHORT_BOUND = int(os.environ.get("PROXY_LOOP_SESSION_SHORT_BOUND", "10"))
PROXY_LOOP_SESSION_LONG_BOUND = int(os.environ.get("PROXY_LOOP_SESSION_LONG_BOUND", "25"))
PROXY_LOOP_THRESHOLD_LONG = int(os.environ.get("PROXY_LOOP_THRESHOLD_LONG", "4"))
PROXY_LOOP_THRESHOLD_VERY_LONG = int(os.environ.get("PROXY_LOOP_THRESHOLD_VERY_LONG", "5"))
PROXY_TEXT_LOOP_THRESHOLD_LONG = int(os.environ.get("PROXY_TEXT_LOOP_THRESHOLD_LONG", "4"))
PROXY_TEXT_LOOP_THRESHOLD_VERY_LONG = int(os.environ.get("PROXY_TEXT_LOOP_THRESHOLD_VERY_LONG", "5"))
PROXY_BLOCKER_THRESHOLD_LONG = int(os.environ.get("PROXY_BLOCKER_THRESHOLD_LONG", "3"))
PROXY_BLOCKER_THRESHOLD_VERY_LONG = int(os.environ.get("PROXY_BLOCKER_THRESHOLD_VERY_LONG", "3"))

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
PROXY_TOOL_AUTO_PROMOTE_THRESHOLD = int(os.environ.get("PROXY_TOOL_AUTO_PROMOTE_THRESHOLD", "3"))
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
# Status API / agent_go integration
# ---------------------------------------------------------------------------
# G-C (2026-08-19): "2" = R13-R16 诊断数据面已就绪(/api/sessions、
# /api/session/<key>/*、/api/backend/*、/api/status ctx_config 段、proxy_diag
# 体字段)。agent_go 存在性探测以版本区分新旧代理(集成契约 §4 fail-open 前提)。
PROXY_STATUS_API_VERSION = "2"
_ACTIVE_CONF_PATH = os.path.join(_SCRIPT_DIR, "configs", "active.conf")
_WATCHDOG_STATE_PATH = os.path.join(_LOG_DIR, "watchdog_state.json")
_LIFECYCLE_EVENTS_PATH = os.path.join(_LOG_DIR, "lifecycle_events.jsonl")

# ---------------------------------------------------------------------------
# R13-R16 diagnostics data plane (see docs/02-architecture-design/
# diagnostics-dataplane-design-20260819.md)
# ---------------------------------------------------------------------------
# Runtime backend name (startup axis, from LLAMA_BACKEND env set by configs/*.conf
# via manage.sh). "unknown" when not provided — runtime code must not rely on it
# for correctness, only for capability hints / display.
PROXY_BACKEND_NAME = os.environ.get("LLAMA_BACKEND", "") or "unknown"

PROXY_DIAG_ENABLED = os.environ.get(
    "PROXY_DIAG_ENABLED", get_default("PROXY_DIAG_ENABLED")).lower() in ("1", "true", "yes")
PROXY_DIAG_SSE_TAIL = os.environ.get(
    "PROXY_DIAG_SSE_TAIL", get_default("PROXY_DIAG_SSE_TAIL")).lower() in ("1", "true", "yes")
PROXY_DIAG_SESSION_TTL_MIN = int(os.environ.get(
    "PROXY_DIAG_SESSION_TTL_MIN", get_default("PROXY_DIAG_SESSION_TTL_MIN")))
PROXY_DIAG_SESSION_MAX = int(os.environ.get(
    "PROXY_DIAG_SESSION_MAX", get_default("PROXY_DIAG_SESSION_MAX")))
PROXY_DIAG_ARCHIVE_ENABLED = os.environ.get(
    "PROXY_DIAG_ARCHIVE_ENABLED", get_default("PROXY_DIAG_ARCHIVE_ENABLED")).lower() in ("1", "true", "yes")
PROXY_DIAG_ARCHIVE_MAX_MB = int(os.environ.get(
    "PROXY_DIAG_ARCHIVE_MAX_MB", get_default("PROXY_DIAG_ARCHIVE_MAX_MB")))
PROXY_DIAG_TIMINGS_SOURCE = os.environ.get(
    "PROXY_DIAG_TIMINGS_SOURCE", get_default("PROXY_DIAG_TIMINGS_SOURCE"))
# A3 台账持久化(logging-trajectory-improvement-design-20260820 Phase A)：
# R14 台账增量落盘 logs/diag/ledger/<sid>.jsonl，端点内存优先、档案兜底——
# agent_go 轮级看门狗(集成契约 §3.2 / 上下文工程 §9 P1-4)跨重启不失忆。
PROXY_DIAG_LEDGER_ENABLED = os.environ.get(
    "PROXY_DIAG_LEDGER_ENABLED", get_default("PROXY_DIAG_LEDGER_ENABLED")).lower() in ("1", "true", "yes")
PROXY_DIAG_LEDGER_MAX_MB = int(os.environ.get(
    "PROXY_DIAG_LEDGER_MAX_MB", get_default("PROXY_DIAG_LEDGER_MAX_MB")))

# ---------------------------------------------------------------------------
# 上下文工程引擎（R8.1-R8.3，context_engine.py；设计 llama-defender-context-
# engineering-design §4.3/§4.9，Phase 0 §12 结论已坐实击穿根因为每轮回溯改写）
# ---------------------------------------------------------------------------
PROXY_CTX_ENGINE_ENABLED = os.environ.get(
    "PROXY_CTX_ENGINE_ENABLED", get_default("PROXY_CTX_ENGINE_ENABLED")).lower() in ("1", "true", "yes")
# S: epoch 触发 token 预算; 0 = auto → min(65%×ctx_chars/4, 70K)（§12.4 35B 校准）
PROXY_CTX_EPOCH_TRIGGER_TOKENS = int(os.environ.get(
    "PROXY_CTX_EPOCH_TRIGGER_TOKENS", get_default("PROXY_CTX_EPOCH_TRIGGER_TOKENS")))
# K: epoch 重切保留最近轮数; 0 = auto → 24（§4.4）
PROXY_CTX_WINDOW_K = int(os.environ.get(
    "PROXY_CTX_WINDOW_K", get_default("PROXY_CTX_WINDOW_K")))

_DIAG_DIR = os.path.join(_LOG_DIR, "diag")
_DIAG_SESSIONS_PATH = os.path.join(_DIAG_DIR, "sessions.jsonl")
_DIAG_ARCHIVE_DIR = os.path.join(_DIAG_DIR, "archive")
_DIAG_LEDGER_DIR = os.path.join(_DIAG_DIR, "ledger")
_diag_lock = threading.Lock()
_diag_ctx = threading.local()  # per-request diagnostics accumulation (see diagnostics.py)

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
PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS = int(os.environ.get("PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", "300"))
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


def _detect_client_type(user_agent: str) -> str:
    """Normalize User-Agent header into a short client type label.

    Recognized clients:
      - opencode
      - claude-code / Claude
      - kimi
      - curl / wget / httpie
      - browser (Mozilla/AppleWebKit)
    Returns 'unknown' for empty/unrecognized agents.
    """
    if not user_agent:
        return "unknown"
    ua = user_agent.lower()
    if "opencode" in ua:
        return "opencode"
    if "claude-code" in ua or ua.startswith("claude/"):
        return "claude-code"
    if "kimi" in ua:
        return "kimi"
    if "curl" in ua:
        return "curl"
    if "wget" in ua:
        return "wget"
    if "httpie" in ua:
        return "httpie"
    if "mozilla" in ua or "applewebkit" in ua:
        return "browser"
    # Fallback to first token if it looks like a product name
    first = user_agent.split("/")[0].strip()
    if first and " " not in first and len(first) <= 30:
        return first
    return "unknown"


# Model ID → route preference mapping (preference only, safety always overrides).
# Derived from the model catalog via model_registry (configs/models.json); when
# that file is absent the registry synthesizes an equivalent catalog from the
# env defaults above, so behavior is identical to the former hardcode.
# Rebuilt on SIGHUP (reload_config.py) — "$env" references resolve against the
# live PROXY_CLOUD_MODEL, fixing the legacy import-time capture staleness.
_CATALOG_FROM_FILE = model_registry.load(
    env_cloud_model_getter=lambda: PROXY_CLOUD_MODEL,
    cloud_base_url=PROXY_CLOUD_BASE_URL,
    cloud_concurrent=PROXY_ROUTE_CLOUD_CONCURRENT,
)
MODEL_ROUTE_PREFERENCES = model_registry.build_route_preferences()


# ---------------------------------------------------------------------------
# Multi-provider cloud state (Phase B model catalog)
# ---------------------------------------------------------------------------
def _env_lookup(key, default=None):
    """Provider env resolver: hot-reloaded proxy_state attrs win over os.environ.

    Provider keys (ZHIPU_API_KEY / KIMI_API_KEY / ...) live either as proxy_state
    attrs (applied by reload_config from secret.local.conf on SIGHUP) or in the
    process environment (exported by manage.sh at startup).
    """
    v = globals().get(key)
    if isinstance(v, str) and v:
        return v
    v = os.environ.get(key)
    return v if v else default


def rebuild_provider_locks():
    """(Re)build per-provider semaphores from the catalog's `concurrent` fields.

    Rebuilt at startup and on SIGHUP. Dispatch looks up the selected model's
    provider; providers without a lock (or unknown models) fall back to the
    global _cloud_lock, preserving pre-catalog behavior.
    """
    global _provider_locks
    new_locks = {}
    for pname in model_registry.list_providers():
        if pname == "local":
            continue
        creds = model_registry.get_provider_credentials(pname, env_lookup=_env_lookup)
        if creds:
            new_locks[pname] = threading.Semaphore(max(1, creds["concurrent"]))
    _provider_locks = new_locks


_provider_locks = {}
rebuild_provider_locks()

# Per-provider circuit breaker + cost (all access under _state_lock)
_PROVIDER_FAIL_COUNT = {}       # provider → consecutive failure count
_PROVIDER_COOLDOWN_START = {}   # provider → monotonic timestamp
_PROVIDER_QUOTA_RESET = {}      # provider → epoch (wall-clock) when subscription quota resets
_route_provider_cost = {}       # provider → daily cost (¥, same date key as _route_daily_date)


def _provider_cooldown_active(pname):
    """True while provider cooldown window is open; clears it once expired.

    Also honors a subscription quota reset deadline (方案 A): while the provider's
    quota reset epoch is in the future, the provider stays cold even if the
    fixed cooldown window (PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS) has elapsed. When
    the reset deadline passes, the quota state clears and the provider returns
    to service automatically.
    """
    if not pname:
        return False
    # Quota reset deadline (wall-clock) takes precedence — skip until reset.
    with _state_lock:
        qr = _PROVIDER_QUOTA_RESET.get(pname)
    if qr:
        if time.time() < qr:
            return True
        with _state_lock:
            _PROVIDER_QUOTA_RESET.pop(pname, None)
    with _state_lock:
        ts = _PROVIDER_COOLDOWN_START.get(pname)
    if not ts:
        return False
    if time.monotonic() - ts < PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS:
        return True
    with _state_lock:
        _PROVIDER_COOLDOWN_START.pop(pname, None)
    return False


def _record_quota_exhausted(pname, reset_epoch):
    """Record a subscription quota-exhausted event with its reset deadline.

    The provider is held cold until `reset_epoch` (wall-clock). Parse the reset
    time from the provider's rate-limit error message (e.g. Z.ai 1308 "Your limit
    will reset at YYYY-MM-DD HH:MM:SS"); 0 disables the deadline (fixed-window
    cooldown only). Safe to call repeatedly — later deadlines win.
    """
    if not pname or not reset_epoch:
        return
    with _state_lock:
        cur = _PROVIDER_QUOTA_RESET.get(pname, 0)
        if reset_epoch > cur:
            _PROVIDER_QUOTA_RESET[pname] = reset_epoch
            _PROVIDER_COOLDOWN_START[pname] = time.monotonic()
            _PROVIDER_FAIL_COUNT[pname] = 0


def _provider_quota_state(pname):
    """Visibility: {exhausted, resets_at_epoch, resets_at_iso} for a provider."""
    with _state_lock:
        qr = _PROVIDER_QUOTA_RESET.get(pname)
    if not qr:
        return {"exhausted": False, "resets_at_epoch": None, "resets_at_iso": None}
    import datetime
    iso = datetime.datetime.fromtimestamp(qr).strftime("%Y-%m-%d %H:%M:%S")
    return {"exhausted": time.time() < qr, "resets_at_epoch": qr, "resets_at_iso": iso}


def _record_provider_failure(pname, retryable=True):
    """Count a provider failure; trip the cooldown after MAX_CLOUD_FAILS."""
    if not pname or not retryable:
        return
    with _state_lock:
        _PROVIDER_FAIL_COUNT[pname] = _PROVIDER_FAIL_COUNT.get(pname, 0) + 1
        n = _PROVIDER_FAIL_COUNT[pname]
        if n >= PROXY_ROUTE_MAX_CLOUD_FAILS:
            _PROVIDER_COOLDOWN_START[pname] = time.monotonic()
            _PROVIDER_FAIL_COUNT[pname] = 0


def _record_provider_success(pname):
    if not pname:
        return
    with _state_lock:
        _PROVIDER_FAIL_COUNT.pop(pname, None)


def _provider_budget_exceeded(pname):
    """True when the provider's per-provider daily budget cap is reached."""
    if not pname:
        return False
    budget = model_registry.get_provider_budget(pname)
    if budget is None or budget <= 0:
        return False
    today = time.strftime("%Y-%m-%d")
    with _state_lock:
        if _route_daily_date != today:
            return False
        spent = _route_provider_cost.get(pname, 0.0)
    return spent >= budget

# ---------------------------------------------------------------------------
# Structured metrics logging
# ---------------------------------------------------------------------------
PROXY_METRICS_ENABLED = os.environ.get("PROXY_METRICS_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_METRICS_DIR = os.environ.get("PROXY_METRICS_DIR", "logs")
_METRICS_PATH = os.path.join(_SCRIPT_DIR, PROXY_METRICS_DIR, "proxy_metrics.jsonl")
_metrics_lock = threading.Lock()
_state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 请求优先级队列：全局单例（惰性构建，worker 数 = 构建时的 PROXY_MAX_CONCURRENT）
# 注意：SIGHUP 热重载 PROXY_MAX_CONCURRENT / 阈值不会重建已存在的管理器实例；
# 分桶阈值在调用方（Handler）按当前值实时计算，不受此限制。
# ---------------------------------------------------------------------------
_QUEUE_MANAGER = None


def get_queue_manager():
    """返回全局 RequestQueueManager（双重检查锁惰性构建）。"""
    global _QUEUE_MANAGER
    if _QUEUE_MANAGER is None:
        with _state_lock:
            if _QUEUE_MANAGER is None:
                import queue_manager
                _QUEUE_MANAGER = queue_manager.RequestQueueManager(
                    max_workers=PROXY_MAX_CONCURRENT,
                    large_threshold=PROXY_QUEUE_LARGE_THRESHOLD_CHARS,
                    huge_threshold=PROXY_QUEUE_HUGE_THRESHOLD_CHARS,
                )
    return _QUEUE_MANAGER

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


def _accumulate_route_daily_cost(input_tokens: int = 0, output_tokens: int = 0,
                                 model: str = "", provider: str = "") -> float:
    """Atomically add estimated cloud API cost and return new daily total.

    Tokens are estimated; output_tokens may be max_tokens upper-bound for streaming.
    Cost is in CNY. Cross-day reset is handled automatically.

    Phase B: per-model pricing from the catalog (models.<name>.price); models
    without a catalog price fall back to the global PROXY_CLOUD_PRICE_* pair.
    Per-provider totals accumulate alongside the global total for
    per_provider_budget caps (defaults.per_provider_budget).
    """
    global _route_daily_cost, _route_daily_date
    price_in, price_out = PROXY_CLOUD_PRICE_INPUT, PROXY_CLOUD_PRICE_OUTPUT
    if model:
        entry = model_registry.get_model(model)
        price = (entry or {}).get("price") or {}
        pi = price.get("input")
        po = price.get("output")
        if isinstance(pi, (int, float)) and not isinstance(pi, bool) and pi >= 0:
            price_in = pi
        if isinstance(po, (int, float)) and not isinstance(po, bool) and po >= 0:
            price_out = po
    today = time.strftime("%Y-%m-%d")
    with _state_lock:
        if _route_daily_date != today:
            _route_daily_date = today
            _route_daily_cost = 0.0
            _route_provider_cost.clear()
        input_cost = input_tokens * price_in / 1_000_000
        output_cost = output_tokens * price_out / 1_000_000
        _route_daily_cost += input_cost + output_cost
        if provider:
            _route_provider_cost[provider] = _route_provider_cost.get(provider, 0.0) + input_cost + output_cost
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
        # Derived from the model catalog (model_registry.get_alias_list):
        # legacy surface (incl. always-exposed claude-opus-4-7 so existing
        # sessions never 404; without a cloud key the proxy falls back to
        # local gracefully) + extra route keys declared in configs/models.json.
        # Never exposes MODEL_NAME.
        aliases = model_registry.get_alias_list()
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
    # TS-1 BM25 relevance-driven compression
    ("PROXY_BM25_ENABLED", "PROXY_BM25_ENABLED", "bool", "false", "true"),
    ("PROXY_BM25_K1", "PROXY_BM25_K1", "float", "1.5", "1.5"),
    ("PROXY_BM25_B", "PROXY_BM25_B", "float", "0.75", "0.75"),
    ("PROXY_BM25_KEEP_THRESHOLD", "PROXY_BM25_KEEP_THRESHOLD", "float", "3.5", "3.5"),
    ("PROXY_BM25_DROP_THRESHOLD", "PROXY_BM25_DROP_THRESHOLD", "float", "0.1", "0.1"),
    ("PROXY_BM25_DROP_TARGET_RATIO", "PROXY_BM25_DROP_TARGET_RATIO", "float", "0.45", "0.45"),
    ("PROXY_BM25_MIN_PREFIX", "PROXY_BM25_MIN_PREFIX", "int", "4", "4"),
    ("PROXY_BM25_IDF_LRU_MAX", "PROXY_BM25_IDF_LRU_MAX", "int", "10000", "10000"),
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
    ("PROXY_TIMEOUT_MARGIN_S", "PROXY_TIMEOUT_MARGIN_S", "int", "30", "30"),
    ("PROXY_STREAM_IDLE_TIMEOUT_S", "PROXY_STREAM_IDLE_TIMEOUT_S", "int", "30", "30"),
    ("PROXY_SSE_HEARTBEAT_BYTES", "PROXY_SSE_HEARTBEAT_BYTES", "int", "100000", "100000"),
    ("PROXY_SSE_HEARTBEAT_S", "PROXY_SSE_HEARTBEAT_S", "float", "15", "15"),
    ("PROXY_OOM_SAFE_TOKENS", "PROXY_OOM_SAFE_TOKENS", "int", "60000", "60000"),
    ("PROXY_RETRY_AFTER_SECONDS", "PROXY_RETRY_AFTER_SECONDS", "int", "30", "30"),
    ("PROXY_MAX_REQUEST_BYTES", "PROXY_MAX_REQUEST_BYTES", "int", str(500 * 1024), str(500 * 1024)),
    ("PROXY_CLOUD_MAX_REQUEST_BYTES", "PROXY_CLOUD_MAX_REQUEST_BYTES", "int", str(2 * 1024 * 1024), str(2 * 1024 * 1024)),
    # Loop detection
    ("PROXY_TEXT_LOOP_ENABLED", "PROXY_TEXT_LOOP_ENABLED", "bool", "true", "true"),
    ("PROXY_TEXT_LOOP_THRESHOLD", "PROXY_TEXT_LOOP_THRESHOLD", "int", "3", "3"),
    ("PROXY_TEXT_LOOP_MIN_CHARS", "PROXY_TEXT_LOOP_MIN_CHARS", "int", "100", "100"),
    ("PROXY_TEXT_LOOP_SIMILARITY", "PROXY_TEXT_LOOP_SIMILARITY", "float", "0.85", "0.85"),
    # DEF-109 context-size-based loop tiers
    ("PROXY_LOOP_CHARS_LONG", "PROXY_LOOP_CHARS_LONG", "int", "50000", "50000"),
    ("PROXY_LOOP_CHARS_VERY_LONG", "PROXY_LOOP_CHARS_VERY_LONG", "int", "100000", "100000"),
    # Phase 4 dynamic loop thresholds
    ("PROXY_LOOP_SESSION_SHORT_BOUND", "PROXY_LOOP_SESSION_SHORT_BOUND", "int", "10", "10"),
    ("PROXY_LOOP_SESSION_LONG_BOUND", "PROXY_LOOP_SESSION_LONG_BOUND", "int", "25", "25"),
    ("PROXY_LOOP_THRESHOLD_LONG", "PROXY_LOOP_THRESHOLD_LONG", "int", "4", "4"),
    ("PROXY_LOOP_THRESHOLD_VERY_LONG", "PROXY_LOOP_THRESHOLD_VERY_LONG", "int", "5", "5"),
    ("PROXY_TEXT_LOOP_THRESHOLD_LONG", "PROXY_TEXT_LOOP_THRESHOLD_LONG", "int", "4", "4"),
    ("PROXY_TEXT_LOOP_THRESHOLD_VERY_LONG", "PROXY_TEXT_LOOP_THRESHOLD_VERY_LONG", "int", "5", "5"),
    ("PROXY_BLOCKER_THRESHOLD_LONG", "PROXY_BLOCKER_THRESHOLD_LONG", "int", "3", "3"),
    ("PROXY_BLOCKER_THRESHOLD_VERY_LONG", "PROXY_BLOCKER_THRESHOLD_VERY_LONG", "int", "3", "3"),
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
    ("PROXY_TOOL_AUTO_PROMOTE_THRESHOLD", "PROXY_TOOL_AUTO_PROMOTE_THRESHOLD", "int", "3", "3"),
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
    ("PROXY_CLOUD_API_KEY", "PROXY_CLOUD_API_KEY", "str", "", ""),
    ("PROXY_ROUTE_CLOUD_CONCURRENT", "PROXY_ROUTE_CLOUD_CONCURRENT", "int", "2", "2"),
    ("PROXY_ROUTE_MEMORY_PCT", "PROXY_ROUTE_MEMORY_PCT", "int", "90", "90"),
    ("PROXY_ROUTE_FALLBACK_ENABLED", "PROXY_ROUTE_FALLBACK_ENABLED", "bool", "true", "true"),
    ("PROXY_ROUTE_MAX_CLOUD_FAILS", "PROXY_ROUTE_MAX_CLOUD_FAILS", "int", "3", "3"),
    ("PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", "int", "300", "300"),
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
    # Dynamic max_tokens
    ("PROXY_DYNAMIC_MAX_TOKENS_ENABLED", "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", "bool", "false", "true"),
    ("PROXY_DYNAMIC_MAX_TOKENS_INIT", "PROXY_DYNAMIC_MAX_TOKENS_INIT", "int", "4096", "4096"),
    ("PROXY_DYNAMIC_MAX_TOKENS_GROWTH", "PROXY_DYNAMIC_MAX_TOKENS_GROWTH", "int", "4096", "4096"),
    ("PROXY_DYNAMIC_MAX_TOKENS_SATURATION", "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", "int", "2048", "2048"),
    ("PROXY_DYNAMIC_MAX_TOKENS_OOM", "PROXY_DYNAMIC_MAX_TOKENS_OOM", "int", "4096", "4096"),
    ("PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO", "PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO", "float", "0.8", "0.8"),
    # 请求优先级队列（Phase 1，默认关闭）
    ("PROXY_QUEUE_ENABLED", "PROXY_QUEUE_ENABLED", "bool", "false", "false"),
    ("PROXY_QUEUE_TIMEOUT_SECONDS", "PROXY_QUEUE_TIMEOUT_SECONDS", "int", "300", "300"),
    ("PROXY_QUEUE_LARGE_THRESHOLD_CHARS", "PROXY_QUEUE_LARGE_THRESHOLD_CHARS", "int", "80000", "80000"),
    ("PROXY_QUEUE_HUGE_THRESHOLD_CHARS", "PROXY_QUEUE_HUGE_THRESHOLD_CHARS", "int", "350000", "350000"),
    ("PROXY_QUEUE_HUGE_ACTION", "PROXY_QUEUE_HUGE_ACTION", "str", "cloud", "cloud"),
    # R13-R16 诊断数据面（reloadable 开关；路径类为 module 常量不热更）
    ("PROXY_DIAG_ENABLED", "PROXY_DIAG_ENABLED", "bool", "true", "true"),
    ("PROXY_DIAG_SSE_TAIL", "PROXY_DIAG_SSE_TAIL", "bool", "true", "true"),
    ("PROXY_DIAG_SESSION_TTL_MIN", "PROXY_DIAG_SESSION_TTL_MIN", "int", "180", "180"),
    ("PROXY_DIAG_SESSION_MAX", "PROXY_DIAG_SESSION_MAX", "int", "64", "64"),
    ("PROXY_DIAG_ARCHIVE_ENABLED", "PROXY_DIAG_ARCHIVE_ENABLED", "bool", "true", "true"),
    ("PROXY_DIAG_ARCHIVE_MAX_MB", "PROXY_DIAG_ARCHIVE_MAX_MB", "int", "200", "200"),
    ("PROXY_DIAG_LEDGER_ENABLED", "PROXY_DIAG_LEDGER_ENABLED", "bool", "true", "true"),
    ("PROXY_DIAG_LEDGER_MAX_MB", "PROXY_DIAG_LEDGER_MAX_MB", "int", "100", "100"),
    ("PROXY_CTX_ENGINE_ENABLED", "PROXY_CTX_ENGINE_ENABLED", "bool", "false", "false"),
    ("PROXY_CTX_EPOCH_TRIGGER_TOKENS", "PROXY_CTX_EPOCH_TRIGGER_TOKENS", "int", "0", "0"),
    ("PROXY_CTX_WINDOW_K", "PROXY_CTX_WINDOW_K", "int", "0", "0"),
    ("PROXY_DIAG_TIMINGS_SOURCE", "PROXY_DIAG_TIMINGS_SOURCE", "str", "auto", "auto"),
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
                # Strip bash export/declare prefixes so both
                #   export VAR="value"
                #   declare -x VAR="value"
                # are parsed correctly.
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                elif key.startswith("declare -x "):
                    key = key[len("declare -x "):].strip()
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

# Fallback: read PROXY_CLOUD_API_KEY from secret.local.conf if env var not set.
# This ensures direct `python3 anthropic_proxy.py` also works (not just via
# `./manage.sh` which sources the file before spawning the subprocess).
if not PROXY_CLOUD_API_KEY:
    _secret_path = os.path.join(_SCRIPT_DIR, "configs", "secret.local.conf")
    _secret_env = _parse_conf_env(_secret_path)
    if "PROXY_CLOUD_API_KEY" in _secret_env:
        PROXY_CLOUD_API_KEY = _secret_env["PROXY_CLOUD_API_KEY"]

    # Also update anthropic_proxy module if already imported
    import sys as _sys
    if "anthropic_proxy" in _sys.modules:
        _sys.modules["anthropic_proxy"].PROXY_CLOUD_API_KEY = PROXY_CLOUD_API_KEY


# ---------------------------------------------------------------------------
# All public names exportable via `from proxy_state import *`
# ---------------------------------------------------------------------------
__all__ = [
    # Backend
    "LLAMA_BASE", "LLAMA_API_KEY", "BACKEND_TYPE", "IS_CLOUD", "_strategy", "_SCRIPT_DIR",
    # Status API / agent_go integration
    "PROXY_STATUS_API_VERSION", "_ACTIVE_CONF_PATH", "_WATCHDOG_STATE_PATH", "_LIFECYCLE_EVENTS_PATH",
    # R13-R16 diagnostics data plane
    "PROXY_BACKEND_NAME", "PROXY_DIAG_ENABLED", "PROXY_DIAG_SSE_TAIL",
    "PROXY_DIAG_SESSION_TTL_MIN", "PROXY_DIAG_SESSION_MAX", "PROXY_DIAG_ARCHIVE_ENABLED",
    "PROXY_DIAG_ARCHIVE_MAX_MB", "PROXY_DIAG_TIMINGS_SOURCE",
    "PROXY_DIAG_LEDGER_ENABLED", "PROXY_DIAG_LEDGER_MAX_MB",
    "PROXY_CTX_ENGINE_ENABLED", "PROXY_CTX_EPOCH_TRIGGER_TOKENS", "PROXY_CTX_WINDOW_K",
    "_DIAG_DIR", "_DIAG_SESSIONS_PATH", "_DIAG_ARCHIVE_DIR", "_DIAG_LEDGER_DIR", "_diag_lock", "_diag_ctx",
    # Concurrency
    "PROXY_MAX_CONCURRENT", "_llama_lock", "MODEL_NAME",
    # Tool-result clearing
    "PROXY_CLEAR_ENABLED", "PROXY_CLEAR_THRESHOLD", "PROXY_TOOL_KEEP",
    "PROXY_FROZEN_HEAD", "PROXY_CLEAR_TAIL_FIRST",
    # Cache aligner
    "PROXY_CACHE_ALIGN_ENABLED", "PROXY_CACHE_ALIGN_HEAD",
    # Shared state
    "_SESSION_LAST_MESSAGES", "_LOOP_SESSION_STATE", "_SESSION_REQUEST_COUNT",
    "_SESSION_TOOL_FREQ",
    # Compression
    "PROXY_COMPRESS_ENABLED", "PROXY_COMPRESS_THRESHOLD", "PROXY_COMPRESS_MODE",
    "PROXY_SCRUB_ANSI", "PROXY_SIEVE_JSON_MAX_ITEMS", "PROXY_SIEVE_JSON_MAX_STR_LEN",
    "PROXY_SIEVE_JSON_MAX_DEPTH", "PROXY_LOG_DEDUPE", "PROXY_DEDUPE_SCALARS",
    "PROXY_COMPRESS_AUDIT", "PROXY_COMPRESSION_PROFILE", "CONTENT_TOOLS_FALLBACK_ENABLED",
    # TS-1 BM25
    "PROXY_BM25_ENABLED", "PROXY_BM25_K1", "PROXY_BM25_B",
    "PROXY_BM25_KEEP_THRESHOLD", "PROXY_BM25_DROP_THRESHOLD", "PROXY_BM25_DROP_TARGET_RATIO",
    "PROXY_BM25_MIN_PREFIX", "PROXY_BM25_IDF_LRU_MAX",
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
    "PROXY_MAX_REQUEST_BYTES", "PROXY_OOM_SAFE_TOKENS", "PROXY_RETRY_AFTER_SECONDS", "PROXY_CLOUD_MAX_REQUEST_BYTES",
    "PROXY_TIMEOUT_MARGIN_S", "PROXY_STREAM_IDLE_TIMEOUT_S",
    "StreamIdleTimeout",
    # Token ratios
    "PROXY_TOKEN_RATIO_CHINESE", "PROXY_TOKEN_RATIO_ENGLISH", "PROXY_TOKEN_RATIO_CODE",
    # Memory
    "PROXY_MEMORY_REJECT_THRESHOLD",
    # Dynamic max_tokens
    "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", "PROXY_DYNAMIC_MAX_TOKENS_INIT",
    "PROXY_DYNAMIC_MAX_TOKENS_GROWTH", "PROXY_DYNAMIC_MAX_TOKENS_SATURATION",
    "PROXY_DYNAMIC_MAX_TOKENS_OOM", "PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO",
    # Snapshots
    "PROXY_SNAPSHOT_ENABLED", "PROXY_SNAPSHOT_MAX_FILES",
    # Dynamic concurrency
    "PROXY_DYNAMIC_CONCURRENT_ENABLED", "PROXY_DYNAMIC_CONCURRENT_MIN",
    "PROXY_DYNAMIC_CONCURRENT_MAX", "PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS",
    "PROXY_DYNAMIC_CONCURRENT_ERROR_RATE",
    # Loop detection
    "PROXY_LOOP_THRESHOLD", "PROXY_LOOP_LEVEL2", "PROXY_LOOP_LEVEL3",
    "PROXY_LOOP_CHARS_LONG", "PROXY_LOOP_CHARS_VERY_LONG",
    "PROXY_LOOP_SESSION_SHORT_BOUND", "PROXY_LOOP_SESSION_LONG_BOUND",
    "PROXY_LOOP_THRESHOLD_LONG", "PROXY_LOOP_THRESHOLD_VERY_LONG",
    "PROXY_TEXT_LOOP_ENABLED", "PROXY_TEXT_LOOP_THRESHOLD", "PROXY_TEXT_LOOP_MIN_CHARS",
    "PROXY_TEXT_LOOP_SIMILARITY",
    "PROXY_TEXT_LOOP_THRESHOLD_LONG", "PROXY_TEXT_LOOP_THRESHOLD_VERY_LONG",
    # Session continuation
    "PROXY_SESSION_CONTINUATION_ENABLED", "PROXY_SESSION_CONTINUATION_MIN_REQUESTS",
    # Dedup
    "PROXY_DEDUP_WINDOW", "_DEDUP_CACHE",
    # Sliding windows
    "_LATENCY_WINDOW", "_ERROR_WINDOW", "_LATENCY_BY_TARGET", "_METRICS_V1_FIELDS",
    # Re-read
    "PROXY_REREAD_PREVIEW_CHARS",
    # Blocker
    "PROXY_BLOCKER_ENABLED", "PROXY_BLOCKER_THRESHOLD",
    "PROXY_BLOCKER_THRESHOLD_LONG", "PROXY_BLOCKER_THRESHOLD_VERY_LONG",
    "_BLOCKER_ERROR_MARKERS",
    # Tool filter
    "PROXY_TOOL_FILTER_ENABLED", "PROXY_TOOL_FILTER_MAX", "PROXY_TOOL_FILTER_RECENT",
    "PROXY_TOOL_AUTO_PROMOTE_THRESHOLD",
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
    "_detect_client_type",
    "_accumulate_route_daily_cost",
    "_parse_budget_alert_tiers", "_get_budget_alert_level",
    # 请求优先级队列
    "PROXY_QUEUE_ENABLED", "PROXY_QUEUE_TIMEOUT_SECONDS",
    "PROXY_QUEUE_LARGE_THRESHOLD_CHARS", "PROXY_QUEUE_HUGE_THRESHOLD_CHARS",
    "PROXY_QUEUE_HUGE_ACTION", "get_queue_manager",
]
