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
        "defaults": {"local": "mlx-community/Qwen3.6-35B-A3B-4bit", "cloud": "deepseek-v4-pro"},
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

    # ---- Pre-trunc / OOM safety ----
    "PROXY_OOM_SAFE_CHARS": {
        "defaults": {"all": "200000"},
        "type": "int", "scope": "reloadable",
        "doc": "Pre-truncate payloads exceeding this char count to keep_rounds=2. Legacy name: PROXY_PRE_TRUNCATE_CHARS.",
    },
    "PROXY_MAX_REQUEST_BYTES": {
        "defaults": {"all": str(500 * 1024)},
        "type": "int", "scope": "reloadable",
        "doc": "Hard limit on request body size. Returns 413 Payload Too Large before pipeline processing.",
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
        "doc": "max_tokens ceiling for saturation/oom_danger/pre_trunc stages.",
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

    # ---- Logging ----
    "PROXY_LOG_PATH": {
        "defaults": {"all": "/tmp/anthropic_proxy.log"},
        "type": "str", "scope": "module",
        "doc": "Log file path. Written alongside stdout.",
    },
}

# ---------------------------------------------------------------------------
# Thread lock and shared state: imported from proxy_state (single source of
# truth for all module-level config constants and mutable shared state).
# Previously defined here to avoid circular imports — now resolved by
# extracting state into its own dependency-free module.
# ---------------------------------------------------------------------------

from proxy_state import (
    _state_lock,
    _SESSION_REQUEST_COUNT,
    _SESSION_LAST_MESSAGES,
    _DEDUP_CACHE,
    _LATENCY_WINDOW,
    _ERROR_WINDOW,
)

__all__ = [
    "CONFIG_REGISTRY",
    "resolve_default",
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

def resolve_default(key, is_cloud):
    """Return the canonical default value for a config key given backend mode.

    Looks up the key in CONFIG_REGISTRY and returns:
      - mode-specific default if present (local/cloud)
      - 'all' default as fallback
      - None if key not found
    """
    entry = CONFIG_REGISTRY.get(key)
    if not entry:
        return None
    defaults = entry.get("defaults", {})
    mode = "cloud" if is_cloud else "local"
    return defaults.get(mode, defaults.get("all"))

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
