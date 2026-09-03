#!/usr/bin/env python3
"""
Canonical config variable registry for anthropic_proxy.py.

Serves as the single source of truth for all PROXY_* / LLAMA_* / RAPID_MLX_*
environment variables. CLAUDE.md, AGENTS.md, and docs/ should reference this
file rather than duplicating default values.

Usage:
    import proxy_config
    # Access constants:
    proxy_config.IS_CLOUD
    # Validate current state:
    proxy_config.validate()
"""

import os
import sys
import threading
import collections

# ---------------------------------------------------------------------------
# CONFIG_REGISTRY: every configurable env var with its canonical default(s)
# ---------------------------------------------------------------------------
# Each entry:
#   key: env var name (UPPERCASE)
#   value: {
#     "defaults": {                     # default values keyed by scenario
#       "all": "...",                   #   shared default for all modes
#       "local": "...",                 #   local-only override
#       "cloud": "...",                 #   cloud-only override
#     },
#     "type": "str"|"int"|"float"|"bool",
#     "scope": "module"|"reloadable",   # module = needs restart; reloadable = SIGHUP ok
#     "doc": "Human-readable description",
#     "alias_of": "PROXY_FOO",          # if this var is a deprecated alias
#   }
#
# Notes:
#   - "all" default applies when neither "local" nor "cloud" is specified
#     for the current mode (BACKEND_TYPE).
#   - "type" controls _cast_config_value() during reload.
#   - "scope=module" means the var is read at startup and NOT updated by
#     _reload_config (PORT, HOST, etc.).

CONFIG_REGISTRY = {
    # ---- Backend routing ----
    "LLAMA_BASE_URL": {
        "defaults": {"all": "http://127.0.0.1:8081/v1"},
        "type": "str", "scope": "reloadable",
        "doc": "Backend API base URL. Auto-detected: 'deepseek'/'openai'/'api.' → cloud mode.",
    },
    "LLAMA_API_KEY": {
        "defaults": {"all": "sk-1234"},
        "type": "str", "scope": "reloadable",
        "doc": "API key for backend. Local: dummy. Cloud: real key (set in secret.local.conf).",
    },
    "BACKEND_TYPE": {
        "defaults": {"all": ""},
        "type": "str", "scope": "reloadable",
        "doc": "Override backend type: 'local' or 'cloud'. Auto-detected from LLAMA_BASE_URL when empty.",
    },
    "MODEL_NAME": {
        "defaults": {"local": "unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit", "cloud": "deepseek-v4-pro"},
        "type": "str", "scope": "reloadable",
        "doc": "Model identifier sent to backend. Auto-set by BACKEND_TYPE.",
    },
    "PORT": {
        "defaults": {"all": "4000"},
        "type": "int", "scope": "module",
        "doc": "Proxy listen port.",
    },
    "HOST": {
        "defaults": {"all": "127.0.0.1"},
        "type": "str", "scope": "module",
        "doc": "Proxy listen address.",
    },

    # ---- Server info ----
    "LLAMA_PORT": {
        "defaults": {"all": "8081"},
        "type": "int", "scope": "reloadable",
        "doc": "Backend listen port.",
    },
    "LLAMA_HOST": {
        "defaults": {"all": "127.0.0.1"},
        "type": "str", "scope": "reloadable",
        "doc": "Backend bind address.",
    },

    # ---- Concurrency ----
    "PROXY_MAX_CONCURRENT": {
        "defaults": {"local": "1", "cloud": "4"},
        "type": "int", "scope": "reloadable",
        "doc": "Max concurrent requests forwarded to backend. Local: 1 (prevents Metal OOM). Cloud: 4.",
    },

    # ---- Tool-result clearing ----
    "PROXY_CLEAR_ENABLED": {
        "defaults": {"local": "true", "cloud": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable tool-result content clearing. Recommended false for local backends (rapid-mlx Wasted call death loop).",
    },
    "PROXY_CLEAR_THRESHOLD": {
        "defaults": {"local": "15000", "cloud": "30000"},
        "type": "int", "scope": "reloadable",
        "doc": "Character threshold to trigger tool-result clearing.",
    },
    "PROXY_TOOL_KEEP": {
        "defaults": {"local": "2", "cloud": "10"},
        "type": "int", "scope": "reloadable",
        "doc": "Number of recent tool_result pairs to preserve during clearing.",
    },
    "PROXY_CLEAR_TAIL_FIRST": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "When enabled, clear newest tool_results first (protects prefix cache stability).",
    },
    "PROXY_FROZEN_HEAD": {
        "defaults": {"local": "12", "cloud": "0"},
        "type": "int", "scope": "reloadable",
        "doc": "Protect first N messages from clearing/compression. Local: 12 (~5-8K tokens). Cloud: 0 (disabled).",
    },

    # ---- Tool-call fallback ----
    "PROXY_CONTENT_TOOLS_FALLBACK": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable <tools> content-text extraction fallback for Qwen models.",
    },

    # ---- Context truncation ----
    "PROXY_CTX_LIMIT_ENABLED": {
        "defaults": {"local": "true", "cloud": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable message truncation when context exceeds limit.",
    },
    "PROXY_CTX_CHARS_LIMIT": {
        "defaults": {"local": "180000", "cloud": "500000"},
        "type": "int", "scope": "reloadable",
        "doc": "Character limit for context truncation (char strategy). Deprecated alias for PROXY_CHARS_SATURATION.",
        "alias_of": "PROXY_CHARS_SATURATION",
    },
    "PROXY_CTX_KEEP_HEAD": {
        "defaults": {"all": "2"},
        "type": "int", "scope": "reloadable",
        "doc": "Keep first N messages during truncation (system context + skills).",
    },
    "PROXY_CTX_KEEP_TAIL": {
        "defaults": {"all": "4"},
        "type": "int", "scope": "reloadable",
        "doc": "Keep last N messages during truncation.",
    },
    "PROXY_CTX_TRUNCATE_STRATEGY": {
        "defaults": {"all": "char"},
        "type": "str", "scope": "reloadable",
        "doc": "Truncation strategy: char (threshold-based), rounds (keep last N assistant rounds), fifo (fixed count).",
    },
    "PROXY_CTX_KEEP_ROUNDS": {
        "defaults": {"all": "10"},
        "type": "int", "scope": "reloadable",
        "doc": "Max recent assistant rounds to preserve (rounds strategy).",
    },
    "PROXY_CTX_KEEP_MESSAGES": {
        "defaults": {"all": "40"},
        "type": "int", "scope": "reloadable",
        "doc": "Total messages to keep (fifo strategy).",
    },
    "PROXY_CTX_TOKEN_BUDGET": {
        "defaults": {"all": "30000"},
        "type": "int", "scope": "reloadable",
        "doc": "Prompt token budget ceiling (rounds strategy).",
    },
    "PROXY_CTX_TOKEN_RATIO": {
        "defaults": {"all": "2.0"},
        "type": "float", "scope": "reloadable",
        "doc": "Chars-to-tokens estimation ratio for budget calculation.",
    },

    # ---- Lifecycle stage thresholds (chars) ----
    "PROXY_CHARS_GROWTH": {
        "defaults": {"local": "40000", "cloud": "80000"},
        "type": "int", "scope": "reloadable",
        "doc": "Char threshold for GROWTH lifecycle stage (tail-40% clearing).",
    },
    "PROXY_CHARS_EXPANSION": {
        "defaults": {"local": "90000", "cloud": "200000"},
        "type": "int", "scope": "reloadable",
        "doc": "Char threshold for EXPANSION lifecycle stage (tail-60% clearing + think strip).",
    },
    "PROXY_CHARS_SATURATION": {
        "defaults": {"local": "180000", "cloud": "500000"},
        "type": "int", "scope": "reloadable",
        "doc": "Char threshold for SATURATION lifecycle stage (full-dynamic clear + merge + trunc).",
    },
    "PROXY_CHARS_OOM_DANGER": {
        "defaults": {"local": "350000", "cloud": "1000000"},
        "type": "int", "scope": "reloadable",
        "doc": "Char threshold for OOM_DANGER lifecycle stage (no frozen + hard truncation).",
    },

    # ---- Output token control ----
    "PROXY_MAX_TOKENS_OVERRIDE": {
        "defaults": {"all": "0"},
        "type": "int", "scope": "reloadable",
        "doc": "Hard cap on max_tokens. 0 = disabled. Works around rapid-mlx ignoring max_tokens.",
    },
    "PROXY_OUTPUT_TOKEN_LIMIT_RATIO": {
        "defaults": {"all": "2.0"},
        "type": "float", "scope": "reloadable",
        "doc": "Multiplier applied to max_tokens for output safety margin.",
    },
    "PROXY_BACKEND_TIMEOUT": {
        "defaults": {"all": "600"},
        "type": "int", "scope": "reloadable",
        "doc": "Backend request timeout in seconds. Increase for long-context (100K+ prefill ~5 min).",
    },
    "PROXY_TIMEOUT_MARGIN_S": {
        "defaults": {"all": "30"},
        "type": "int", "scope": "reloadable",
        "doc": "Proactive timeout margin (s): non-streaming requests return 504 at client_timeout - margin so the error reaches the client before it disconnects.",
    },
    "PROXY_STREAM_IDLE_TIMEOUT_S": {
        "defaults": {"all": "30"},
        "type": "int", "scope": "reloadable",
        "doc": "Streaming inter-chunk idle watchdog (s), counted after the first token. A mid-stream stall beyond this aborts the relay and cancels in-flight generation.",
    },
    "PROXY_SSE_HEARTBEAT_BYTES": {
        "defaults": {"all": "100000"},
        "type": "int", "scope": "reloadable",
        "doc": "#51-B1 v2: streaming requests with body >= this many bytes run an SSE ': keepalive' heartbeat (every PROXY_SSE_HEARTBEAT_S) while the relay waits for the backend's first chunk during long cold prefills (epoch spikes / new-peak turns) — feeds the client's idle timer so it doesn't disconnect at ~180s.",
    },
    "PROXY_SSE_HEARTBEAT_S": {
        "defaults": {"all": "15"},
        "type": "float", "scope": "reloadable",
        "doc": "#51-B1: SSE keepalive comment interval (s) during backend preflight wait.",
    },

    # ---- Pre-trunc / OOM safety ----
    "PROXY_OOM_SAFE_CHARS": {
        "defaults": {"all": "200000"},
        "type": "int", "scope": "reloadable",
        "doc": "Pre-truncate payloads exceeding this char count to keep_rounds=2. Legacy name: PROXY_PRE_TRUNCATE_CHARS.",
    },
    "PROXY_MAX_REQUEST_BYTES": {
        "defaults": {"all": str(500 * 1024)},
        "type": "int", "scope": "reloadable",
        "doc": "Hard limit on request body size for local backend. Returns 413 Payload Too Large before forwarding to local.",
    },
    "PROXY_CLOUD_MAX_REQUEST_BYTES": {
        "defaults": {"all": str(2 * 1024 * 1024)},
        "type": "int", "scope": "reloadable",
        "doc": "Hard limit on request body size for cloud backend. Larger than local because cloud providers handle their own OOM scheduling.",
    },
    "PROXY_OOM_SAFE_TOKENS": {
        "defaults": {"all": "60000"},
        "type": "int", "scope": "reloadable",
        "doc": "Estimated prompt token limit. Force aggressive FIFO if exceeded. 0 = disabled.",
    },
    "PROXY_RETRY_AFTER_SECONDS": {
        "defaults": {"all": "30"},
        "type": "int", "scope": "reloadable",
        "doc": "Retry-After header value (seconds) for 503/504 responses.",
    },

    # ---- Token estimation ratios ----
    "PROXY_TOKEN_RATIO_CHINESE": {
        "defaults": {"all": "1.5"},
        "type": "float", "scope": "reloadable",
        "doc": "Chars-per-token ratio for Chinese-dominated content.",
    },
    "PROXY_TOKEN_RATIO_ENGLISH": {
        "defaults": {"all": "4.0"},
        "type": "float", "scope": "reloadable",
        "doc": "Chars-per-token ratio for English-dominated content.",
    },
    "PROXY_TOKEN_RATIO_CODE": {
        "defaults": {"all": "3.0"},
        "type": "float", "scope": "reloadable",
        "doc": "Chars-per-token ratio for code-dominated content.",
    },

    # ---- Memory rejection ----
    "PROXY_MEMORY_REJECT_THRESHOLD": {
        "defaults": {"local": "90", "cloud": "95"},
        "type": "float", "scope": "reloadable",
        "doc": "System memory % threshold. New requests rejected with 503 above this.",
    },

    # ---- Dynamic max_tokens ----
    "PROXY_DYNAMIC_MAX_TOKENS_ENABLED": {
        "defaults": {"local": "true", "cloud": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Adjust max_tokens by lifecycle stage and memory pressure.",
    },
    "PROXY_DYNAMIC_MAX_TOKENS_INIT": {
        "defaults": {"all": "4096"},
        "type": "int", "scope": "reloadable",
        "doc": "max_tokens ceiling for init lifecycle stage.",
    },
    "PROXY_DYNAMIC_MAX_TOKENS_GROWTH": {
        "defaults": {"all": "4096"},
        "type": "int", "scope": "reloadable",
        "doc": "max_tokens ceiling for growth/expansion stages.",
    },
    "PROXY_DYNAMIC_MAX_TOKENS_SATURATION": {
        "defaults": {"all": "2048"},
        "type": "int", "scope": "reloadable",
        "doc": "max_tokens ceiling for saturation stage.",
    },
    "PROXY_DYNAMIC_MAX_TOKENS_OOM": {
        "defaults": {"all": "4096"},
        "type": "int", "scope": "reloadable",
        "doc": "max_tokens ceiling for oom_danger/pre_trunc stages.",
    },
    "PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO": {
        "defaults": {"all": "0.8"},
        "type": "float", "scope": "reloadable",
        "doc": "Additional multiplier for rapid-mlx backend on dynamic max_tokens.",
    },

    # ---- Dynamic concurrency ----
    "PROXY_DYNAMIC_CONCURRENT_ENABLED": {
        "defaults": {"local": "true", "cloud": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Auto-adjust backend concurrency by latency/error rate.",
    },
    "PROXY_DYNAMIC_CONCURRENT_MIN": {
        "defaults": {"all": "1"},
        "type": "int", "scope": "reloadable",
        "doc": "Minimum concurrent requests.",
    },
    "PROXY_DYNAMIC_CONCURRENT_MAX": {
        "defaults": {"local": "4", "cloud": "8"},
        "type": "int", "scope": "reloadable",
        "doc": "Maximum concurrent requests.",
    },
    "PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS": {
        "defaults": {"all": "30000"},
        "type": "float", "scope": "reloadable",
        "doc": "P95 latency threshold; above this concurrency is reduced.",
    },
    "PROXY_DYNAMIC_CONCURRENT_ERROR_RATE": {
        "defaults": {"all": "0.2"},
        "type": "float", "scope": "reloadable",
        "doc": "Error-rate threshold; above this concurrency is reduced.",
    },

    # ---- Loop detection ----
    "PROXY_LOOP_THRESHOLD": {
        "defaults": {"all": "3"},
        "type": "int", "scope": "reloadable",
        "doc": "Consecutive identical tool calls before Level 1 intervention.",
    },
    "PROXY_LOOP_LEVEL2": {
        "defaults": {"all": "6"},
        "type": "int", "scope": "reloadable",
        "doc": "Consecutive identical calls before Level 2 (tool removal). Defaults to PROXY_LOOP_THRESHOLD * 2.",
    },
    "PROXY_LOOP_LEVEL3": {
        "defaults": {"all": "9"},
        "type": "int", "scope": "reloadable",
        "doc": "Consecutive identical calls before Level 3 (force plain-text). Defaults to PROXY_LOOP_THRESHOLD * 3.",
    },

    # ---- Context-size-based loop tiers (DEF-109) ----
    "PROXY_LOOP_CHARS_LONG": {
        "defaults": {"all": "50000"},
        "type": "int", "scope": "reloadable",
        "doc": "Total chars ≥ this → long tier (stricter loop thresholds).",
    },
    "PROXY_LOOP_CHARS_VERY_LONG": {
        "defaults": {"all": "100000"},
        "type": "int", "scope": "reloadable",
        "doc": "Total chars ≥ this → very_long tier (strictest loop thresholds).",
    },

    # ---- Dynamic loop thresholds (Phase 4 / 建议4) ----
    "PROXY_LOOP_SESSION_SHORT_BOUND": {
        "defaults": {"all": "10"},
        "type": "int", "scope": "reloadable",
        "doc": "Session request count ≤ this value → short tier (strict thresholds).",
    },
    "PROXY_LOOP_SESSION_LONG_BOUND": {
        "defaults": {"all": "25"},
        "type": "int", "scope": "reloadable",
        "doc": "Session request count ≤ this value → long tier; > this → very_long tier.",
    },
    "PROXY_LOOP_THRESHOLD_LONG": {
        "defaults": {"all": "4"},
        "type": "int", "scope": "reloadable",
        "doc": "Loop threshold for long-tier sessions (11-25 requests).",
    },
    "PROXY_LOOP_THRESHOLD_VERY_LONG": {
        "defaults": {"all": "5"},
        "type": "int", "scope": "reloadable",
        "doc": "Loop threshold for very_long-tier sessions (26+ requests).",
    },
    "PROXY_TEXT_LOOP_THRESHOLD_LONG": {
        "defaults": {"all": "4"},
        "type": "int", "scope": "reloadable",
        "doc": "Text loop threshold for long-tier sessions.",
    },
    "PROXY_TEXT_LOOP_THRESHOLD_VERY_LONG": {
        "defaults": {"all": "5"},
        "type": "int", "scope": "reloadable",
        "doc": "Text loop threshold for very_long-tier sessions.",
    },
    "PROXY_BLOCKER_THRESHOLD_LONG": {
        "defaults": {"all": "3"},
        "type": "int", "scope": "reloadable",
        "doc": "Blocker threshold for long-tier sessions.",
    },
    "PROXY_BLOCKER_THRESHOLD_VERY_LONG": {
        "defaults": {"all": "3"},
        "type": "int", "scope": "reloadable",
        "doc": "Blocker threshold for very_long-tier sessions.",
    },

    # ---- Text loop detection ----
    "PROXY_TEXT_LOOP_ENABLED": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable text output loop detection.",
    },
    "PROXY_TEXT_LOOP_THRESHOLD": {
        "defaults": {"all": "3"},
        "type": "int", "scope": "reloadable",
        "doc": "Consecutive similar text messages before intervention.",
    },
    "PROXY_TEXT_LOOP_MIN_CHARS": {
        "defaults": {"all": "100"},
        "type": "int", "scope": "reloadable",
        "doc": "Minimum text length to consider for loop detection.",
    },
    "PROXY_TEXT_LOOP_SIMILARITY": {
        "defaults": {"all": "0.85"},
        "type": "float", "scope": "reloadable",
        "doc": "Text similarity threshold (0.0-1.0) for loop detection.",
    },

    # ---- Blocker detection ----
    "PROXY_BLOCKER_ENABLED": {
        "defaults": {"local": "true", "cloud": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Consecutive same-error results trigger [BLOCKER] user message.",
    },
    "PROXY_BLOCKER_THRESHOLD": {
        "defaults": {"all": "2"},
        "type": "int", "scope": "reloadable",
        "doc": "Consecutive same-error threshold before blocker injection.",
    },

    # ---- Semantic compression ----
    "PROXY_COMPRESS_ENABLED": {
        "defaults": {"local": "true", "cloud": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable semantic content compression for long tool_result contents.",
    },
    "PROXY_COMPRESS_THRESHOLD": {
        "defaults": {"all": "4096"},
        "type": "int", "scope": "reloadable",
        "doc": "Minimum char length of tool_result to trigger semantic compression.",
    },
    "PROXY_TRUNCATE_REPLAYABLE_DROP": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Skill-L4 borrowing: file-read (replayable) tool_results bypass BM25 "
               "keep and drop to a regen pointer (source + orig registered for "
               "ctx_recall). Guard: per-session per-path count — second drop of "
               "the same path keeps the original (anti re-read death spiral).",
    },
    "PROXY_COMPRESS_MODE": {
        "defaults": {"all": "semantic"},
        "type": "str", "scope": "reloadable",
        "doc": "Compression mode: lossless, semantic, or aggressive.",
    },
    "PROXY_SCRUB_ANSI": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Remove ANSI color/control codes from tool_result contents before compression.",
    },
    "PROXY_COMPRESS_AUDIT": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Validate compressed output; fallback to original on failure.",
    },
    "PROXY_DEDUPE_SCALARS": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Deduplicate repeated long scalar strings within a tool_result (only in aggressive mode).",
    },
    # TS-1: BM25 relevance-driven compression
    "PROXY_BM25_ENABLED": {
        "defaults": {"local": "true", "cloud": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable BM25 relevance scoring for tool_result compression decisions.",
    },
    "PROXY_BM25_K1": {
        "defaults": {"all": "1.5"},
        "type": "float", "scope": "reloadable",
        "doc": "Okapi BM25 k1 parameter (term frequency saturation).",
    },
    "PROXY_BM25_B": {
        "defaults": {"all": "0.75"},
        "type": "float", "scope": "reloadable",
        "doc": "Okapi BM25 b parameter (length normalization).",
    },
    "PROXY_BM25_KEEP_THRESHOLD": {
        "defaults": {"all": "3.5"},
        "type": "float", "scope": "reloadable",
        "doc": "BM25 score >= this value: skip compression entirely (keep verbatim).",
    },
    "PROXY_BM25_DROP_THRESHOLD": {
        "defaults": {"all": "0.1"},
        "type": "float", "scope": "reloadable",
        "doc": "BM25 score < this value: force aggressive compression. 2026-08-18 lowered 0.5->0.1 (measured score median 0.00/p90 0.18; old default put nearly all tool_results in the drop path).",
    },
    "PROXY_BM25_DROP_TARGET_RATIO": {
        "defaults": {"all": "0.45"},
        "type": "float", "scope": "reloadable",
        "doc": "TS-4: BM25 drop branch cap ratio (relative to original). Structured compression runs first; head/tail truncation is applied on top only if the structured result still exceeds this ratio.",
    },
    "PROXY_BM25_MIN_PREFIX": {
        "defaults": {"all": "4"},
        "type": "int", "scope": "reloadable",
        "doc": "Minimum prefix length for BM25 prefix expansion (stemming heuristic).",
    },
    "PROXY_BM25_IDF_LRU_MAX": {
        "defaults": {"all": "10000"},
        "type": "int", "scope": "reloadable",
        "doc": "Maximum entries in the BM25 IDF map before LRU pruning.",
    },
    "PROXY_SIEVE_JSON_MAX_ITEMS": {
        "defaults": {"all": "10"},
        "type": "int", "scope": "reloadable",
        "doc": "Max array items to keep during JSON sieve compression.",
    },
    "PROXY_SIEVE_JSON_MAX_STR_LEN": {
        "defaults": {"all": "200"},
        "type": "int", "scope": "reloadable",
        "doc": "Max string length to keep during JSON sieve compression.",
    },
    "PROXY_SIEVE_JSON_MAX_DEPTH": {
        "defaults": {"all": "4"},
        "type": "int", "scope": "reloadable",
        "doc": "Max recursion depth during JSON sieve compression.",
    },

    # ---- Cache aligner ----
    "PROXY_CACHE_ALIGN_ENABLED": {
        "defaults": {"local": "true", "cloud": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Protect first N messages from truncation/reordering for prefix cache stability.",
    },
    "PROXY_CACHE_ALIGN_HEAD": {
        "defaults": {"all": "4"},
        "type": "int", "scope": "reloadable",
        "doc": "Number of prefix messages to protect (system + skills + first user + first assistant).",
    },

    # ---- Tool filtering ----
    "PROXY_TOOL_FILTER_ENABLED": {
        "defaults": {"local": "true", "cloud": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Reduce tool definitions sent to backend by keeping only high-frequency + recent tools.",
    },
    "PROXY_TOOL_FILTER_MAX": {
        "defaults": {"all": "20"},
        "type": "int", "scope": "reloadable",
        "doc": "Only trigger filtering when tools exceed this count.",
    },
    "PROXY_TOOL_FILTER_RECENT": {
        "defaults": {"all": "5"},
        "type": "int", "scope": "reloadable",
        "doc": "Scan last N assistant rounds for recently used tools.",
    },
    "PROXY_TOOL_AUTO_PROMOTE_THRESHOLD": {
        "defaults": {"all": "3"},
        "type": "int", "scope": "reloadable",
        "doc": "Tools used ≥ this many times in a session auto-promote to keep set (DEF-104). 0 disables.",
    },

    # ---- History index ----
    "PROXY_HISTORY_INDEX": {
        "defaults": {"all": "rule"},
        "type": "str", "scope": "reloadable",
        "doc": "Keyword index mode: off or rule (TF matching).",
    },
    "PROXY_HISTORY_TOP_K": {
        "defaults": {"all": "5"},
        "type": "int", "scope": "reloadable",
        "doc": "Max keyword entries to inject into truncated tail.",
    },
    "PROXY_HISTORY_MAX_CHARS": {
        "defaults": {"all": "500"},
        "type": "int", "scope": "reloadable",
        "doc": "Max chars for injected keyword context.",
    },

    # ---- Observability ----
    "PROXY_METRICS_ENABLED": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable per-request metrics JSONL logging.",
    },
    "PROXY_METRICS_DIR": {
        "defaults": {"all": "logs"},
        "type": "str", "scope": "reloadable",
        "doc": "Directory for proxy_metrics.jsonl.",
    },
    "PROXY_SAVE_REQUESTS": {
        "defaults": {"all": ""},
        "type": "str", "scope": "reloadable",
        "doc": "Enable request/response JSONL logging. Set to '1' or 'true' to enable.",
    },
    "PROXY_SAVE_REQUESTS_DIR": {
        "defaults": {"all": "/tmp/anthropic_requests"},
        "type": "str", "scope": "reloadable",
        "doc": "Directory for request/response JSONL logs.",
    },
    "PROXY_SAVE_REQUESTS_MAX": {
        "defaults": {"all": "10"},
        "type": "int", "scope": "reloadable",
        "doc": "Max request/response records to retain.",
    },
    "PROXY_SNAPSHOT_ENABLED": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Write before/after JSON snapshots on request failures.",
    },
    "PROXY_SNAPSHOT_MAX_FILES": {
        "defaults": {"all": "50"},
        "type": "int", "scope": "reloadable",
        "doc": "Maximum snapshot files to retain.",
    },

    # ---- Re-read prevention ----
    "PROXY_REREAD_PREVIEW_CHARS": {
        "defaults": {"all": "200"},
        "type": "int", "scope": "reloadable",
        "doc": "Number of chars to preserve as preview when clearing Read tool_results.",
    },

    # ---- Session continuation ----
    "PROXY_SESSION_CONTINUATION_ENABLED": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable aggressive lifecycle classification for long-running agent sessions.",
    },
    "PROXY_SESSION_CONTINUATION_MIN_REQUESTS": {
        "defaults": {"all": "2"},
        "type": "int", "scope": "reloadable",
        "doc": "Min prior requests before session is classified as continuation.",
    },

    # ---- Dedup ----
    "PROXY_DEDUP_WINDOW": {
        "defaults": {"all": "2"},
        "type": "int", "scope": "reloadable",
        "doc": "Deduplication window in seconds for detecting duplicate POST requests.",
    },

    # ---- Log dedup (compression) ----
    "PROXY_LOG_DEDUPE": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Collapse repeated log lines during log-type compression.",
    },

    # ---- Compression profile ----
    "PROXY_COMPRESSION_PROFILE": {
        "defaults": {"all": "balanced"},
        "type": "str", "scope": "reloadable",
        "doc": "Preset profile: balanced (daily coding), aggressive (large logs/data), conservative (quality-critical). Individual PROXY_* vars still override profile values.",
    },

    # ---- Logging ----
    "PROXY_LOG_PATH": {
        "defaults": {"all": "/tmp/anthropic_proxy.log"},
        "type": "str", "scope": "module",
        "doc": "Log file path. Written alongside stdout.",
    },

    # ---- Intelligent model routing ----
    "PROXY_ROUTE_ENABLED": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable intelligent model routing: auto-switch between local and cloud backends.",
    },
    "PROXY_ROUTE_THRESHOLD_CHARS": {
        "defaults": {"all": "90000"},
        "type": "int", "scope": "reloadable",
        "doc": "Context character threshold above which requests route to cloud.",
    },
    "PROXY_CLOUD_BASE_URL": {
        "defaults": {"all": "https://api.deepseek.com/v1"},
        "type": "str", "scope": "reloadable",
        "doc": "Cloud API endpoint URL for routed requests.",
    },
    "PROXY_CLOUD_MODEL": {
        "defaults": {"all": "deepseek-v4-flash"},
        "type": "str", "scope": "reloadable",
        "doc": "Cloud model identifier used for routed requests.",
    },
    "PROXY_CLOUD_API_KEY": {
        "defaults": {"all": ""},
        "type": "str", "scope": "reloadable",
        "doc": "Cloud API key for routed requests. Must be set in configs/secret.local.conf for cloud routing to work.",
    },
    "PROXY_ROUTE_CLOUD_CONCURRENT": {
        "defaults": {"all": "2"},
        "type": "int", "scope": "reloadable",
        "doc": "Max concurrent requests to the cloud backend.",
    },
    "PROXY_ROUTE_MEMORY_PCT": {
        "defaults": {"all": "90"},
        "type": "int", "scope": "reloadable",
        "doc": "Memory pressure threshold (% used) that triggers cloud routing.",
    },
    "PROXY_ROUTE_FALLBACK_ENABLED": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Whether to fall back to local backend when cloud is unavailable.",
    },
    "PROXY_ROUTE_MAX_CLOUD_FAILS": {
        "defaults": {"all": "3"},
        "type": "int", "scope": "reloadable",
        "doc": "Max consecutive cloud failures before entering cooldown.",
    },
    "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS": {
        "defaults": {"all": "300"},
        "type": "int", "scope": "reloadable",
        "doc": "Cooldown duration (seconds) after max cloud failures reached.",
    },
    "PROXY_CLOUD_PRICE_INPUT": {
        "defaults": {"all": "0.5"},
        "type": "float", "scope": "reloadable",
        "doc": "Cloud API input price (CNY per million tokens).",
    },
    "PROXY_CLOUD_PRICE_OUTPUT": {
        "defaults": {"all": "1.5"},
        "type": "float", "scope": "reloadable",
        "doc": "Cloud API output price (CNY per million tokens).",
    },
    "PROXY_ROUTE_SENSITIVE_PATTERNS": {
        "defaults": {"all": ""},
        "type": "str", "scope": "reloadable",
        "doc": "Comma-separated literal substring patterns for sensitive file paths (force local routing).",
    },
    "PROXY_ROUTE_DAILY_BUDGET": {
        "defaults": {"all": "0"},
        "type": "float", "scope": "reloadable",
        "doc": "Daily cloud API cost cap (CNY). 0 = unlimited.",
    },
    "PROXY_ROUTE_DAILY_BUDGET_HARD_STOP": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "When true, block new cloud requests once daily_budget is reached. When false, only emit tiered alerts.",
    },
    "PROXY_ROUTE_BUDGET_ALERT_TIERS": {
        "defaults": {"all": "50,80,100"},
        "type": "str", "scope": "reloadable",
        "doc": "Comma-separated daily budget usage percentages that trigger warning/danger/critical alerts on /status.",
    },
    "PROXY_ROUTE_PROFILE": {
        "defaults": {"all": ""},
        "type": "str", "scope": "reloadable",
        "doc": "Route config profile name: safe / balanced / cost-aware.",
    },
    "PROXY_ROUTE_STICKY": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Sticky sessions: once cloud, always cloud (recommended true for cost-predictable workflows; false allows session to return to local after N rounds below threshold).",
    },
    "PROXY_ROUTE_STICKY_RETURN_ROUNDS": {
        "defaults": {"all": "5"},
        "type": "int", "scope": "reloadable",
        "doc": "Non-sticky: consecutive below-threshold rounds before a cloud session returns to local.",
    },
    "PROXY_ROUTE_STICKY_RETURN_RATIO": {
        "defaults": {"all": "0.7"},
        "type": "float", "scope": "reloadable",
        "doc": "Non-sticky: ratio of effective_threshold below which a cloud request counts as 'below' for return counter (0.7 = below 70% of threshold).",
    },

    # ---- 请求优先级队列（Phase 1：默认关闭，向后兼容）----
    "PROXY_QUEUE_ENABLED": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Enable priority request queue. Default false = legacy semaphore behavior.",
    },
    "PROXY_QUEUE_TIMEOUT_SECONDS": {
        "defaults": {"all": "300"},
        "type": "int", "scope": "reloadable",
        "doc": "Max seconds a request may wait in the priority queue before 503.",
    },
    "PROXY_QUEUE_LARGE_THRESHOLD_CHARS": {
        "defaults": {"all": "80000"},
        "type": "int", "scope": "reloadable",
        "doc": "Char threshold for 'large' queue bucket.",
    },
    "PROXY_QUEUE_HUGE_THRESHOLD_CHARS": {
        "defaults": {"all": "350000"},
        "type": "int", "scope": "reloadable",
        "doc": "Char threshold for 'huge' bucket: routed to cloud or rejected instead of queueing locally. "
        "2026-08-18: 200000 -> 350000, 对齐后端 pflash 96K token 阈值 (350K chars ~ 80-100K tokens): "
        "墙内走前缀缓存, 过 96K tokens 由 pflash 兜底; auto-route 90K chars 阈值不受影响, "
        "非强制路由的大会话仍在 huge 之前送云。",
    },
    "PROXY_QUEUE_HUGE_ACTION": {
        "defaults": {"all": "cloud"},
        "type": "str", "scope": "reloadable",
        "doc": "Action for huge bucket: cloud (route via SmartRouter if enabled) | reject (413).",
    },

    # ---- R13-R16 诊断数据面（diagnostics dataplane，2026-08-19）----
    "PROXY_DIAG_ENABLED": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Master switch for the diagnostics data plane (R13-R16): diag response "
               "headers, SSE tail notes, session ledger, sent_view archive, sessions.jsonl.",
    },
    "PROXY_DIAG_SSE_TAIL": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Emit the ': x-proxy-diag {...}' SSE comment line at stream tail (R13 "
               "streaming channel). Comment lines are ignored by all SSE parsers per spec.",
    },
    "PROXY_DIAG_SESSION_TTL_MIN": {
        "defaults": {"all": "180"},
        "type": "int", "scope": "reloadable",
        "doc": "Minutes a session's ledger/archive stays resident after last activity.",
    },
    "PROXY_DIAG_SESSION_MAX": {
        "defaults": {"all": "64"},
        "type": "int", "scope": "reloadable",
        "doc": "Max sessions kept in the in-memory ledger (FIFO eviction beyond this).",
    },
    "PROXY_DIAG_ARCHIVE_ENABLED": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Persist per-turn sent_view (final backend payload + injection marks) to "
               "logs/diag/archive/<sid>.jsonl (R15).",
    },
    "PROXY_DIAG_ARCHIVE_MAX_MB": {
        "defaults": {"all": "200"},
        "type": "int", "scope": "reloadable",
        "doc": "Total archive size cap in MB; oldest session files are deleted beyond it.",
    },
    "PROXY_DIAG_TIMINGS_SOURCE": {
        "defaults": {"all": "auto"},
        "type": "str", "scope": "reloadable",
        "doc": "Where prompt-processed counts come from: auto (probe backend response "
               "timings; fields stay null when unsupported) | off (never emit).",
    },
    "PROXY_DIAG_LEDGER_ENABLED": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Persist R14 session ledger increments to logs/diag/ledger/<sid>.jsonl; "
               "/api/session/<key>/ledger serves from memory first, file fallback.",
    },
    "PROXY_DIAG_LEDGER_MAX_MB": {
        "defaults": {"all": "100"},
        "type": "int", "scope": "reloadable",
        "doc": "Total ledger dir size cap in MB; oldest session files are deleted beyond it.",
    },

    # ---- PDC 渐进披露（IFC-3 方案 B，2026-08-30）----
    # PROXY_PD_ENABLED 自 MVP 起以 getattr 默认运行，此处正式注册为唯一权威。
    # 模型流出 ctx_recall 调用时，代理在同一请求内自答并追加结果后重新分发
    # （子代理模式内置化），客户端全透明。关闭时回落 MVP 路径 A（次请求
    # content_compressor 改写）。仅当响应未发出任何内容块且全部工具调用均为
    # ctx_recall 时触发；结果截断 2000 chars（PDC §5 护栏）。
    "PROXY_PD_ENABLED": {
        "defaults": {"all": "true"},
        "type": "bool", "scope": "reloadable",
        "doc": "Progressive disclosure master switch: ctx_recall tool injection + "
               "manifest indexing.",
    },
    "PROXY_PD_MICRO_TURN_ENABLED": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Same-request micro-turn re-dispatch for ctx_recall calls (PDC option B). "
               "Off = MVP path A (next-request tool_result rewrite via content_compressor).",
    },
    "PROXY_PD_MICRO_TURN_MAX": {
        "defaults": {"all": "2"},
        "type": "int", "scope": "reloadable",
        "doc": "Max micro-turn re-dispatches per request (bounds recursion; each costs "
               "one incremental prefill).",
    },

    # ---- H_BE shadow 探针（belief-entropy shadow probe，2026-08-29）----
    # 只测不动：成功的本地响应完成后，搭 prefix cache 便车追加一次双探针锚定
    # 提问，用 top_logprobs 截断熵估计 MMPO 式信念熵 H_BE，落盘 logs/diag/hbe.jsonl。
    # 永不改变任何路由/截断/压缩决策；任何失败静默跳过（fail-open）。
    "PROXY_HBE_ENABLED": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Shadow H_BE probe master switch. Off by default; measures only, never acts.",
    },
    "PROXY_HBE_MIN_CHARS": {
        "defaults": {"all": "20000"},
        "type": "int", "scope": "reloadable",
        "doc": "Skip probe when the sent payload is smaller than this (chars) — small "
               "contexts have no truncation risk and don't need belief tracking.",
    },
    "PROXY_HBE_SAMPLE_EVERY": {
        "defaults": {"all": "4"},
        "type": "int", "scope": "reloadable",
        "doc": "Probe cadence: run on every Nth turn of a session (1 = every turn).",
    },
    "PROXY_HBE_TOP_LOGPROBS": {
        "defaults": {"all": "20"},
        "type": "int", "scope": "reloadable",
        "doc": "top_logprobs for the probe call; entropy is computed over this truncated "
               "distribution (measured top-20 mass coverage ~92-100%).",
    },
    "PROXY_HBE_MAX_TOKENS": {
        "defaults": {"all": "160"},
        "type": "int", "scope": "reloadable",
        "doc": "Probe completion budget (tokens). 160 lets the two-sentence anchor answer "
               "finish; 48 truncated 97.6% of answers mid-way (measured 2026-08-30). "
               "Cost is ~1s decode — prefill dominates probe latency.",
    },
    "PROXY_HBE_LOCK_WAIT_S": {
        "defaults": {"all": "5.0"},
        "type": "float", "scope": "reloadable",
        "doc": "Max seconds the probe waits for the engine concurrency lock; skipped "
               "beyond this so shadow probing never delays user requests.",
    },
    "PROXY_HBE_TIMEOUT_S": {
        "defaults": {"all": "120"},
        "type": "int", "scope": "reloadable",
        "doc": "Probe backend socket timeout in seconds (cache-warm prefill + short gen).",
    },
    "PROXY_PIN_ENABLED": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "R19 X-Proxy-Pin-Context master switch: pinned anchors skip "
               "compression/truncation stages. Off = header ignored "
               "(+X-Proxy-Pin-Disabled: true).",
    },
    "PROXY_PIN_BUDGET_RATIO": {
        "defaults": {"all": "0.05"},
        "type": "float", "scope": "reloadable",
        "doc": "Pin budget as ratio of request chars (contract: <=5%). Excess "
               "pins demoted from list tail with X-Proxy-Pin-Demoted header.",
    },
    "PROXY_FOLD_DENSE_ENABLED": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Dense fold directory for fifo truncation (2026-09-03): replace the "
               "one-line fold placeholder with a per-file/action index "
               "(<=MAX_CHARS). Off = legacy one-line version. Shadow logging "
               "always on regardless of this switch.",
    },
    "PROXY_FOLD_DENSE_MAX_FILES": {
        "defaults": {"all": "10"},
        "type": "int", "scope": "reloadable",
        "doc": "Max file rows in the dense fold directory (first-seen turn order).",
    },
    "PROXY_FOLD_DENSE_MAX_CHARS": {
        "defaults": {"all": "800"},
        "type": "int", "scope": "reloadable",
        "doc": "Hard cap for the dense fold Files segment (chars); tail "
               "RECALL_CUE sentence is never truncated.",
    },
    "PROXY_CTX_ENGINE_ENABLED": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "reloadable",
        "doc": "Context-engineering engine (R8.1-R8.3): append-only canonical + "
               "write-time compression + epoch state machine; replaces retro "
               "ContentCompressor/ContextTruncator/OOMSafetyFIFO when on.",
    },
    "PROXY_CTX_EPOCH_TRIGGER_TOKENS": {
        "defaults": {"all": "0"},
        "type": "int", "scope": "reloadable",
        "doc": "Epoch budget S in tokens; 0 = auto → min(65% × ctx_chars/4, 70000).",
    },
    "PROXY_CTX_WINDOW_K": {
        "defaults": {"all": "0"},
        "type": "int", "scope": "reloadable",
        "doc": "Rounds kept verbatim at epoch re-cut (K); 0 = auto → 24.",
    },

    # ---- 后端启动参数（由 manage.sh 消费，代理运行时不读取，scope=module）----
    "LLAMA_BACKEND": {
        "defaults": {"all": "llama-server"},
        "type": "str", "scope": "module",
        "doc": "Backend binary kind: llama-server | rapid-mlx | vllm-mlx. Consumed by manage.sh.",
    },
    "LLAMA_MODEL": {
        "defaults": {"all": ""},
        "type": "str", "scope": "module",
        "doc": "Model id/path passed to the backend binary (-hf or -m). Consumed by manage.sh.",
    },
    "LLAMA_SERVER_BIN": {
        "defaults": {"all": ""},
        "type": "str", "scope": "module",
        "doc": "Explicit path to the backend binary (e.g. .venv-rapidmlx/bin/rapid-mlx). Empty = PATH lookup.",
    },
    "LLAMA_THREADS": {
        "defaults": {"all": "8"},
        "type": "int", "scope": "module",
        "doc": "CPU threads for the backend binary.",
    },
    "LLAMA_TEMP": {
        "defaults": {"all": "0.7"},
        "type": "float", "scope": "module",
        "doc": "Sampling temperature passed to the backend.",
    },
    "LLAMA_TOP_P": {
        "defaults": {"all": "0.8"},
        "type": "float", "scope": "module",
        "doc": "Top-p sampling passed to the backend.",
    },
    "LLAMA_TOP_K": {
        "defaults": {"all": "20"},
        "type": "int", "scope": "module",
        "doc": "Top-k sampling passed to the backend.",
    },
    "LLAMA_MIN_P": {
        "defaults": {"all": "0.0"},
        "type": "float", "scope": "module",
        "doc": "Min-p sampling passed to the backend.",
    },
    "LLAMA_PRESENCE_PENALTY": {
        "defaults": {"all": "0.0"},
        "type": "float", "scope": "module",
        "doc": "Presence penalty passed to the backend.",
    },
    "LLAMA_THINKING": {
        "defaults": {"all": "false"},
        "type": "str", "scope": "module",
        "doc": "Thinking mode flag: true|false|'' (empty = omit flag, for models without thinking support).",
    },
    "RAPID_MLX_TOOL_PARSER": {
        "defaults": {"all": ""},
        "type": "str", "scope": "module",
        "doc": "rapid-mlx tool parser name (e.g. hermes, qwen3_coder_xml). Consumed by manage.sh.",
    },
    "RAPID_MLX_REASONING_PARSER": {
        "defaults": {"all": ""},
        "type": "str", "scope": "module",
        "doc": "rapid-mlx reasoning parser name (e.g. qwen3). Consumed by manage.sh.",
    },
    "RAPID_MLX_ENABLE_PREFIX_CACHE": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "module",
        "doc": "Enable rapid-mlx cross-request prefix cache (>= 0.11.5). Consumed by manage.sh.",
    },
    "RAPID_MLX_KV_QUANTIZATION": {
        "defaults": {"all": "false"},
        "type": "bool", "scope": "module",
        "doc": "Enable rapid-mlx KV cache quantization. Consumed by manage.sh.",
    },
    "RAPID_MLX_KV_QUANT_BITS": {
        "defaults": {"all": "4"},
        "type": "int", "scope": "module",
        "doc": "rapid-mlx KV cache quantization bits. Consumed by manage.sh.",
    },
    "RAPID_MLX_EXTRA_ARGS": {
        "defaults": {"all": ""},
        "type": "str", "scope": "module",
        "doc": "Extra CLI args appended to the rapid-mlx command line. Consumed by manage.sh.",
    },
}

# 非代理配置变量白名单：允许出现在 conf/env 中，但不要求注册进 CONFIG_REGISTRY。
# - CONFIG_*：configs/*.conf 的元数据，由 manage.sh list 消费。
# - HF_HUB_OFFLINE：环境开关，由后端进程读取。
# - *_API_KEY：models.json 中 provider key_env 引用的密钥变量，存放在 secret.local.conf。
NON_PROXY_VARS = frozenset({
    "CONFIG_NAME",
    "CONFIG_DESC",
    "CONFIG_MEMORY",
    "HF_HUB_OFFLINE",
})
NON_PROXY_SUFFIXES = ("_API_KEY",)

# ---------------------------------------------------------------------------
# Thread lock and shared state: lazily re-exported from proxy_state (single
# source of truth for all module-level config constants and mutable shared
# state).
#
# 配置统一阶段一：proxy_state 顶部需要 `from proxy_config import get_default`，
# 若此处仍保留模块级 `from proxy_state import ...` 会形成循环 import。改为
# PEP 562 惰性 __getattr__ 转发，属性访问行为不变（proxy_config._state_lock
# 等仍可正常使用，且与 proxy_state 中是同一对象）。
# ---------------------------------------------------------------------------

_LAZY_STATE_NAMES = frozenset({
    "_state_lock",
    "_SESSION_REQUEST_COUNT",
    "_SESSION_LAST_MESSAGES",
    "_DEDUP_CACHE",
    "_LATENCY_WINDOW",
    "_ERROR_WINDOW",
})


def __getattr__(name):
    # PEP 562：访问时才 import proxy_state，打破模块加载期的循环依赖。
    if name in _LAZY_STATE_NAMES:
        import proxy_state
        return getattr(proxy_state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "CONFIG_REGISTRY",
    "resolve_default",
    "get_default",
    "get_registry_entry",
    "is_reloadable",
    "list_unregistered_env_vars",
    "validate_startup",
    "write_defaults_sh",
    "diff_from_defaults",
    "validate",
    "_state_lock",
    "_SESSION_REQUEST_COUNT",
    "_SESSION_LAST_MESSAGES",
    "_DEDUP_CACHE",
    "_LATENCY_WINDOW",
    "_ERROR_WINDOW",
]

# ---------------------------------------------------------------------------
# Utility: resolve canonical default for a config key
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Compression profiles — preset combinations for common scenarios.
# Individual PROXY_* env vars still take precedence over profile values.
# ---------------------------------------------------------------------------

PROFILE_MAP = {
    "balanced": {
        "doc": "日常编码 — 平衡压缩与质量",
        "PROXY_COMPRESS_ENABLED": "true",
        "PROXY_COMPRESS_MODE": "semantic",
        "PROXY_COMPRESS_THRESHOLD": "4096",
        "PROXY_CLEAR_ENABLED": "false",
        "PROXY_CTX_LIMIT_ENABLED": "true",
        "PROXY_CTX_TRUNCATE_STRATEGY": "rounds",
        "PROXY_CTX_KEEP_ROUNDS": "10",
        "PROXY_CTX_TOKEN_BUDGET": "30000",
        "PROXY_TOOL_FILTER_ENABLED": "true",
        "PROXY_TOOL_FILTER_MAX": "20",
        "PROXY_CACHE_ALIGN_ENABLED": "true",
        "PROXY_CACHE_ALIGN_HEAD": "4",
        "PROXY_BM25_ENABLED": "true",
        "PROXY_SCRUB_ANSI": "true",
        "PROXY_COMPRESS_AUDIT": "true",
        "PROXY_HISTORY_INDEX": "rule",
        "PROXY_OOM_SAFE_CHARS": "200000",
    },
    "aggressive": {
        "doc": "超大日志/数据分析 — 激进压缩省上下文",
        "PROXY_COMPRESS_ENABLED": "true",
        "PROXY_COMPRESS_MODE": "aggressive",
        "PROXY_COMPRESS_THRESHOLD": "2048",
        "PROXY_CLEAR_ENABLED": "true",
        "PROXY_CLEAR_THRESHOLD": "10000",
        "PROXY_CTX_LIMIT_ENABLED": "true",
        "PROXY_CTX_TRUNCATE_STRATEGY": "fifo",
        "PROXY_CTX_KEEP_MESSAGES": "30",
        "PROXY_TOOL_FILTER_ENABLED": "true",
        "PROXY_TOOL_FILTER_MAX": "15",
        "PROXY_CACHE_ALIGN_ENABLED": "true",
        "PROXY_CACHE_ALIGN_HEAD": "2",
        "PROXY_BM25_ENABLED": "true",
        "PROXY_SCRUB_ANSI": "true",
        "PROXY_COMPRESS_AUDIT": "false",
        "PROXY_DEDUPE_SCALARS": "true",
        "PROXY_HISTORY_INDEX": "rule",
        "PROXY_OOM_SAFE_CHARS": "150000",
    },
    "conservative": {
        "doc": "质量关键任务 — 零语义损失风险",
        "PROXY_COMPRESS_ENABLED": "false",
        "PROXY_CLEAR_ENABLED": "false",
        "PROXY_CTX_LIMIT_ENABLED": "true",
        "PROXY_CTX_TRUNCATE_STRATEGY": "char",
        "PROXY_CTX_CHARS_LIMIT": "180000",
        "PROXY_TOOL_FILTER_ENABLED": "false",
        "PROXY_CACHE_ALIGN_ENABLED": "true",
        "PROXY_CACHE_ALIGN_HEAD": "6",
        "PROXY_BM25_ENABLED": "false",
        "PROXY_SCRUB_ANSI": "false",
        "PROXY_COMPRESS_AUDIT": "true",
        "PROXY_HISTORY_INDEX": "off",
        "PROXY_OOM_SAFE_CHARS": "250000",
    },
}


def resolve_default(key, is_cloud):
    """Return the canonical default value for a config key given backend mode.

    Looks up the key in CONFIG_REGISTRY and returns:
      - mode-specific default if present (local/cloud)
      - 'all' default as fallback
      - None if key not found

    If PROXY_COMPRESSION_PROFILE is set (and not the default "balanced"),
    profile values override the registry defaults.  Individual env vars
    (set in config files or shell) still take precedence — this function
    is only consulted when no env var is set.
    """
    entry = CONFIG_REGISTRY.get(key)
    if not entry:
        return None
    defaults = entry.get("defaults", {})
    mode = "cloud" if is_cloud else "local"
    base = defaults.get(mode, defaults.get("all"))

    # Apply profile override if the key has a profile value.
    profile_name = os.environ.get("PROXY_COMPRESSION_PROFILE", "balanced")
    if profile_name != "balanced":
        profile = PROFILE_MAP.get(profile_name)
        if profile and key in profile:
            return profile[key]

    return base

# ---------------------------------------------------------------------------
# Utility: list all vars that differ from their defaults for health/debug
# ---------------------------------------------------------------------------

def diff_from_defaults(module_vars, is_cloud):
    """Compare module-level config values against CONFIG_REGISTRY defaults.

    module_vars: dict of current module globals (e.g. vars(proxy_module)).
    Returns list of (key, current_value, canonical_default) for each mismatch.
    """
    diffs = []
    for key, entry in CONFIG_REGISTRY.items():
        canonical = resolve_default(key, is_cloud)
        if canonical is None:
            continue
        current = module_vars.get(key)
        if current is not None:
            entry_type = entry.get("type", "str")
            try:
                if entry_type == "int":
                    current_s = str(current)
                    canonical_s = canonical
                elif entry_type == "float":
                    current_s = str(current)
                    canonical_s = canonical
                elif entry_type == "bool":
                    current_s = "true" if current else "false"
                    canonical_s = canonical
                else:
                    current_s = str(current)
                    canonical_s = canonical
                if current_s != canonical_s:
                    diffs.append((key, current_s, canonical_s))
            except (ValueError, TypeError):
                diffs.append((key, str(current), canonical))
    return diffs

# ---------------------------------------------------------------------------
# Utility: validate current module state against registry
# ---------------------------------------------------------------------------

def validate(module=None):
    """Log warnings for config vars that differ from CONFIG_REGISTRY defaults.
    module: the anthropic_proxy module (optional, uses caller's module).
    """
    if module is None:
        # Introspect the caller
        import inspect
        frame = inspect.currentframe()
        if frame and frame.f_back:
            module = sys.modules.get(frame.f_back.f_globals.get("__name__", ""))
        if module is None:
            module = sys.modules.get("__main__")

    is_cloud = getattr(module, "IS_CLOUD", False)
    diffs = diff_from_defaults(vars(module), is_cloud)
    if diffs:
        msg = "[CONFIG] Vars different from CONFIG_REGISTRY defaults:\n"
        for k, cur, can in diffs:
            msg += f"  {k}: current={cur}, canonical_default={can}\n"
        # We can't call proxy's log() here (circular import), so we print
        print(f"\033[33m[WARN] {msg.strip()}\033[0m", file=sys.stderr)
    return diffs

# ---------------------------------------------------------------------------
# Initialization check on import
# ---------------------------------------------------------------------------

# Boot-time: AUTO_DETECT_CLOUD will be set after proxy loads its module-level
# vars. Callers should invoke validate() after the proxy's module-level code
# has run, not here (circular dependency).


# ---------------------------------------------------------------------------
# 配置统一阶段一：CONFIG_REGISTRY 作为唯一默认值权威
# ---------------------------------------------------------------------------

def get_default(key, backend_type=None):
    """Return the canonical default for key, resolved for backend_type ('local'/'cloud').

    backend_type: if None, inferred from LLAMA_BASE_URL.
    Returns the raw default string from CONFIG_REGISTRY.
    """
    if backend_type is None:
        base_url = os.environ.get("LLAMA_BASE_URL", "")
        backend_type = "cloud" if any(x in base_url.lower() for x in ("deepseek", "openai", "api.")) else "local"
    is_cloud = (backend_type == "cloud")
    resolved = resolve_default(key, is_cloud)
    return resolved if resolved is not None else ""


def get_registry_entry(key):
    """Return the full registry entry dict for key, or None."""
    return CONFIG_REGISTRY.get(key)


def is_reloadable(key):
    """Return True if key has scope == 'reloadable'."""
    entry = CONFIG_REGISTRY.get(key)
    return entry and entry.get("scope") == "reloadable"


def _is_non_proxy_var(key):
    """白名单判定：conf 元数据 / 环境开关 / provider 密钥变量不算未注册。"""
    if key in NON_PROXY_VARS:
        return True
    return any(key.endswith(suffix) for suffix in NON_PROXY_SUFFIXES)


def list_unregistered_env_vars(env=None):
    """Return sorted list of PROXY_*/LLAMA_*/RAPID_MLX_* env vars not in CONFIG_REGISTRY."""
    if env is None:
        env = os.environ
    prefixes = ("PROXY_", "LLAMA_", "RAPID_MLX_")
    return sorted(
        k for k in env
        if k.startswith(prefixes) and k not in CONFIG_REGISTRY and not _is_non_proxy_var(k)
    )


def validate_startup(env=None, active_conf_path=None, backend_type=None, strict=False):
    """Validate startup configuration. Returns list of error strings.

    strict=False: returns errors but does not exit.
    strict=True: raises SystemExit with error summary.

    Checks:
    1. All PROXY_*/LLAMA_*/RAPID_MLX_* env vars are registered in CONFIG_REGISTRY.
    2. Values match declared types (int/float/bool).
    3. backend_type is 'local' or 'cloud'.
    4. If active_conf_path is given, all KEY=value vars in the file are registered.
    """
    if env is None:
        env = os.environ
    errors = []

    # 1. 未注册的环境变量
    unregistered = list_unregistered_env_vars(env)
    for k in unregistered:
        errors.append(f"Unregistered env var: {k} (add to CONFIG_REGISTRY or remove)")

    # 2. 类型校验
    for key, entry in CONFIG_REGISTRY.items():
        raw = env.get(key)
        if raw is None or raw == "":
            continue
        entry_type = entry.get("type", "str")
        if entry_type == "int":
            try:
                int(raw)
            except ValueError:
                errors.append(f"{key} should be int, got '{raw}'")
        elif entry_type == "float":
            try:
                float(raw)
            except ValueError:
                errors.append(f"{key} should be float, got '{raw}'")
        elif entry_type == "bool":
            if raw.lower() not in ("1", "true", "yes", "0", "false", "no"):
                errors.append(f"{key} should be bool, got '{raw}'")

    # 3. backend_type 合法性
    bt = env.get("BACKEND_TYPE", backend_type or "")
    if bt and bt not in ("local", "cloud"):
        errors.append(f"BACKEND_TYPE should be 'local' or 'cloud', got '{bt}'")

    # 4. active.conf 中的未注册变量
    if active_conf_path and os.path.exists(active_conf_path):
        with open(active_conf_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key = line.split("=", 1)[0].strip()
                # conf 文件里允许 export KEY=... 写法
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                if (
                    key.startswith(("PROXY_", "LLAMA_", "RAPID_MLX_"))
                    and key not in CONFIG_REGISTRY
                    and not _is_non_proxy_var(key)
                ):
                    errors.append(f"Unregistered config var in {active_conf_path}: {key}")

    if strict and errors:
        summary = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise SystemExit(summary)
    return errors


def write_defaults_sh(backend_type, path):
    """Write unset CONFIG_REGISTRY variables to a Bash file at path.

    Only writes variables that are NOT already set in os.environ.
    Format: export KEY="value"
    """
    lines = []
    for key, entry in sorted(CONFIG_REGISTRY.items()):
        if key in os.environ:
            continue
        val = get_default(key, backend_type)
        if val == "":
            continue
        lines.append(f'export {key}="{val}"')
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
