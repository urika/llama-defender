"""Backend strategy pattern — encapsulates local vs cloud behavioral differences.

Replaces 38+ scattered `if IS_CLOUD else` branches with a single
BackendStrategy object. Adding a new backend (Ollama, vLLM, etc.)
only requires a new strategy class.
"""


class BackendStrategy:
    """Abstract strategy defining default config values and behavioral flags."""

    DEFAULTS = {}
    oom_safety_enabled = False
    prefix_cache_enabled = False

    @classmethod
    def get_default(cls, key, fallback=None):
        return cls.DEFAULTS.get(key, fallback)

    @staticmethod
    def create(is_cloud):
        return CloudStrategy if is_cloud else LocalStrategy


class LocalStrategy(BackendStrategy):
    """Local backend (llama-server, rapid-mlx) — 48GB Apple Silicon defaults."""

    DEFAULTS = {
        "PROXY_MAX_CONCURRENT": "1",
        "MODEL_NAME": "mlx-community/Qwen3.6-35B-A3B-4bit",
        "PROXY_CLEAR_ENABLED": "true", "PROXY_CLEAR_THRESHOLD": "15000",
        "PROXY_TOOL_KEEP": "2", "PROXY_FROZEN_HEAD": "12",
        "PROXY_CACHE_ALIGN_ENABLED": "true",
        "PROXY_COMPRESS_ENABLED": "true",
        "PROXY_CTX_LIMIT_ENABLED": "true", "PROXY_CTX_CHARS_LIMIT": "180000",
        "PROXY_CHARS_GROWTH": "40000", "PROXY_CHARS_EXPANSION": "90000",
        "PROXY_CHARS_SATURATION": "180000", "PROXY_CHARS_OOM_DANGER": "350000",
        "PROXY_MEMORY_REJECT_THRESHOLD": "90",
        "PROXY_DYNAMIC_MAX_TOKENS_ENABLED": "true",
        "PROXY_DYNAMIC_CONCURRENT_ENABLED": "true", "PROXY_DYNAMIC_CONCURRENT_MAX": "4",
        "PROXY_BLOCKER_ENABLED": "true", "PROXY_TOOL_FILTER_ENABLED": "true",
        "PROXY_TEXT_LOOP_ENABLED": "true", "PROXY_METRICS_ENABLED": "true",
        "PROXY_COMPRESS_THRESHOLD": "4096", "PROXY_COMPRESS_MODE": "semantic",
        "PROXY_SCRUB_ANSI": "true",
        "PROXY_SIEVE_JSON_MAX_ITEMS": "10", "PROXY_SIEVE_JSON_MAX_STR_LEN": "200",
        "PROXY_SIEVE_JSON_MAX_DEPTH": "4",
        "PROXY_LOG_DEDUPE": "true", "PROXY_DEDUPE_SCALARS": "false",
        "PROXY_COMPRESS_AUDIT": "true", "PROXY_CONTENT_TOOLS_FALLBACK": "true",
        "PROXY_SESSION_CONTINUATION_ENABLED": "true",
        "PROXY_SNAPSHOT_ENABLED": "true",
        "PROXY_BACKEND_TIMEOUT": "600", "PROXY_RETRY_AFTER_SECONDS": "30",
        "PROXY_DEDUP_WINDOW": "2", "PROXY_LOOP_THRESHOLD": "3",
        "PROXY_TEXT_LOOP_THRESHOLD": "3", "PROXY_TEXT_LOOP_MIN_CHARS": "100",
        "PROXY_TEXT_LOOP_SIMILARITY": "0.85", "PROXY_BLOCKER_THRESHOLD": "2",
        "PROXY_TOOL_FILTER_MAX": "20", "PROXY_TOOL_FILTER_RECENT": "5",
        "PROXY_HISTORY_INDEX": "rule", "PROXY_HISTORY_TOP_K": "5",
        "PROXY_HISTORY_MAX_CHARS": "500",
        "PROXY_OOM_SAFE_CHARS": "200000", "PROXY_OOM_SAFE_TOKENS": "60000",
        "PROXY_MAX_REQUEST_BYTES": "512000",
        "PROXY_DYNAMIC_MAX_TOKENS_INIT": "4096", "PROXY_DYNAMIC_MAX_TOKENS_GROWTH": "4096",
        "PROXY_DYNAMIC_MAX_TOKENS_SATURATION": "2048",
        "PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO": "0.8",
        "PROXY_DYNAMIC_CONCURRENT_MIN": "1",
        "PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS": "30000",
        "PROXY_DYNAMIC_CONCURRENT_ERROR_RATE": "0.2",
        "PROXY_TOKEN_RATIO_CHINESE": "1.5", "PROXY_TOKEN_RATIO_ENGLISH": "4.0",
        "PROXY_TOKEN_RATIO_CODE": "3.0", "PROXY_CTX_TOKEN_RATIO": "2.0",
        "PROXY_OUTPUT_TOKEN_LIMIT_RATIO": "2.0",
        "PROXY_CTX_KEEP_HEAD": "2", "PROXY_CTX_KEEP_TAIL": "4",
        "PROXY_CTX_TRUNCATE_STRATEGY": "char", "PROXY_CTX_KEEP_ROUNDS": "10",
        "PROXY_CTX_KEEP_MESSAGES": "40", "PROXY_CTX_TOKEN_BUDGET": "30000",
        "PROXY_CACHE_ALIGN_HEAD": "4", "PROXY_CLEAR_TAIL_FIRST": "true",
        "PROXY_SESSION_CONTINUATION_MIN_REQUESTS": "2",
        "PROXY_REREAD_PREVIEW_CHARS": "200", "PROXY_SNAPSHOT_MAX_FILES": "50",
    }

    oom_safety_enabled = True
    prefix_cache_enabled = True


class CloudStrategy(BackendStrategy):
    """Cloud API (DeepSeek, OpenAI) — 1M+ token context defaults."""

    DEFAULTS = {
        "PROXY_MAX_CONCURRENT": "4",
        "MODEL_NAME": "deepseek-v4-pro",
        "PROXY_CLEAR_ENABLED": "false", "PROXY_CLEAR_THRESHOLD": "30000",
        "PROXY_TOOL_KEEP": "10", "PROXY_FROZEN_HEAD": "0",
        "PROXY_CACHE_ALIGN_ENABLED": "false",
        "PROXY_COMPRESS_ENABLED": "false",
        "PROXY_CTX_LIMIT_ENABLED": "false", "PROXY_CTX_CHARS_LIMIT": "500000",
        "PROXY_CHARS_GROWTH": "80000", "PROXY_CHARS_EXPANSION": "200000",
        "PROXY_CHARS_SATURATION": "500000", "PROXY_CHARS_OOM_DANGER": "1000000",
        "PROXY_MEMORY_REJECT_THRESHOLD": "95",
        "PROXY_DYNAMIC_MAX_TOKENS_ENABLED": "false",
        "PROXY_DYNAMIC_CONCURRENT_ENABLED": "false", "PROXY_DYNAMIC_CONCURRENT_MAX": "8",
        "PROXY_BLOCKER_ENABLED": "false", "PROXY_TOOL_FILTER_ENABLED": "false",
        "PROXY_TEXT_LOOP_ENABLED": "true", "PROXY_METRICS_ENABLED": "true",
        "PROXY_COMPRESS_THRESHOLD": "4096", "PROXY_COMPRESS_MODE": "semantic",
        "PROXY_SCRUB_ANSI": "true",
        "PROXY_SIEVE_JSON_MAX_ITEMS": "10", "PROXY_SIEVE_JSON_MAX_STR_LEN": "200",
        "PROXY_SIEVE_JSON_MAX_DEPTH": "4",
        "PROXY_LOG_DEDUPE": "true", "PROXY_DEDUPE_SCALARS": "false",
        "PROXY_COMPRESS_AUDIT": "true", "PROXY_CONTENT_TOOLS_FALLBACK": "true",
        "PROXY_SESSION_CONTINUATION_ENABLED": "true",
        "PROXY_SNAPSHOT_ENABLED": "true",
        "PROXY_BACKEND_TIMEOUT": "600", "PROXY_RETRY_AFTER_SECONDS": "30",
        "PROXY_DEDUP_WINDOW": "2", "PROXY_LOOP_THRESHOLD": "3",
        "PROXY_TEXT_LOOP_THRESHOLD": "3", "PROXY_TEXT_LOOP_MIN_CHARS": "100",
        "PROXY_TEXT_LOOP_SIMILARITY": "0.85", "PROXY_BLOCKER_THRESHOLD": "2",
        "PROXY_TOOL_FILTER_MAX": "20", "PROXY_TOOL_FILTER_RECENT": "5",
        "PROXY_HISTORY_INDEX": "rule", "PROXY_HISTORY_TOP_K": "5",
        "PROXY_HISTORY_MAX_CHARS": "500",
        "PROXY_OOM_SAFE_CHARS": "200000", "PROXY_OOM_SAFE_TOKENS": "60000",
        "PROXY_MAX_REQUEST_BYTES": "512000",
        "PROXY_DYNAMIC_MAX_TOKENS_INIT": "4096", "PROXY_DYNAMIC_MAX_TOKENS_GROWTH": "4096",
        "PROXY_DYNAMIC_MAX_TOKENS_SATURATION": "2048",
        "PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO": "0.8",
        "PROXY_DYNAMIC_CONCURRENT_MIN": "1",
        "PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS": "30000",
        "PROXY_DYNAMIC_CONCURRENT_ERROR_RATE": "0.2",
        "PROXY_TOKEN_RATIO_CHINESE": "1.5", "PROXY_TOKEN_RATIO_ENGLISH": "4.0",
        "PROXY_TOKEN_RATIO_CODE": "3.0", "PROXY_CTX_TOKEN_RATIO": "2.0",
        "PROXY_OUTPUT_TOKEN_LIMIT_RATIO": "2.0",
        "PROXY_CTX_KEEP_HEAD": "2", "PROXY_CTX_KEEP_TAIL": "4",
        "PROXY_CTX_TRUNCATE_STRATEGY": "char", "PROXY_CTX_KEEP_ROUNDS": "10",
        "PROXY_CTX_KEEP_MESSAGES": "40", "PROXY_CTX_TOKEN_BUDGET": "30000",
        "PROXY_CACHE_ALIGN_HEAD": "4", "PROXY_CLEAR_TAIL_FIRST": "true",
        "PROXY_SESSION_CONTINUATION_MIN_REQUESTS": "2",
        "PROXY_REREAD_PREVIEW_CHARS": "200", "PROXY_SNAPSHOT_MAX_FILES": "50",
    }

    oom_safety_enabled = False
    prefix_cache_enabled = False
