<!-- AGENTS.md — Local LLM Inference Stack -->

> This file provides guidance to AI coding agents working with this repository.
> The codebase contains a mix of English and Chinese documentation; this file is
> written in English to align with the existing `CLAUDE.md` agent guide.

---

## Project Overview

This repository is **not** the llama.cpp C++ source code. It is a local LLM
inference orchestration layer that wraps external backend binaries
(`llama-server` or `rapid-mlx`) and exposes an Anthropic-compatible API via a
Python proxy.

Primary use-case: running **Qwen3.6-35B-A3B via Rapid-MLX** (and occasionally
Qwen3.6-27B-MTP via llama-server) on Apple Silicon (MacBook Pro M5 Pro,
48 GB unified memory) for agentic coding workflows, specifically with Claude Code.

**Proxy dual-mode**: the same proxy can also forward requests to cloud APIs
(DeepSeek, OpenAI) without any local backend, enabling A/B comparison between
local and cloud models. Claude Code always connects to `127.0.0.1:4000`;
backend switching is done entirely at the proxy layer — **never modify Claude Code
configuration directly**.

### High-level data flow

```
Local mode:  Client (Anthropic SDK) → anthropic_proxy.py:4000 → llama-server/rapid-mlx:8081 → GGUF/MLX model
Cloud mode:  Client (Anthropic SDK) → anthropic_proxy.py:4000 → DeepSeek/OpenAI API → cloud model
```

The proxy translates Anthropic Messages API requests into OpenAI chat-completion
requests, then converts the backend's OpenAI-compatible responses back into
Anthropic format (including streaming SSE events).

---

## Technology Stack

| Component | Technology |
|-----------|------------|
| Service manager | Bash 4+ (`manage.sh`) |
| API proxy | Python 3 (stdlib only: `http.server`, `urllib.request`, `json`, `re`) |
| Backend option 1 (local) | `llama-server` binary from upstream llama.cpp (GGUF) |
| Backend option 2 (local) | `rapid-mlx` binary (MLX framework, Apple-optimized) |
| Backend option 3 (cloud) | DeepSeek API (`deepseek-v4-pro`) or OpenAI API |
| OS target | macOS with Metal (Apple Silicon) |

**No build tools** (no `pyproject.toml`, `package.json`, `Cargo.toml`, `Makefile`,
etc.). The project is a collection of runnable scripts and configuration files.

---

## File Organization

```
.
├── manage.sh                  # Main service manager (bash)
├── anthropic_proxy.py         # Anthropic→OpenAI proxy (python3, ~3,600 lines)
├── configs/
│   ├── active.conf            # Symlink to the currently active config
│   ├── deepseek-chat.conf     # Cloud proxy → DeepSeek API (no local backend)
│   ├── qwen3.6-27b-mtp.conf   # llama-server + Qwen3.6-27B-MTP (GGUF)
│   ├── rapid-mlx-35b.conf     # rapid-mlx + Qwen3.6-35B-A3B (MLX)
│   └── rapid-mlx-9b.conf      # rapid-mlx + Qwen3.6-9B (lightweight)
├── tools/
│   ├── bench_mtp.py           # MTP model performance benchmark
│   ├── bench_rapidmlx.py      # Rapid-MLX throughput benchmark
│   ├── bench_agent.py         # Agentic end-to-end benchmark
│   ├── bench_compress.py      # Context compression benchmark
│   ├── cache_analyzer.py      # Prefix-cache hit-rate analyzer
│   ├── context_stress_test.py # Long-context stress test
│   ├── stress_test.py         # Load stress test
│   ├── analyze_claude_semantics.py  # Semantic behavior analysis
│   ├── analyze_experiment.py  # A/B experiment result analyzer
│   ├── trace_requirements.py  # Requirement traceability checker
│   ├── logview.sh             # Unified log viewer
│   ├── sysmon.sh              # System monitoring (memory, CPU, disk)
│   ├── modelmon.sh            # Model service monitoring
│   ├── memcheck.sh            # Detailed memory analysis
│   └── run_experiment.sh      # A/B experiment runner
├── test/                      # Automated tests (see test/README.md)
│   ├── run_tests.sh           # Unified tier-based runner
│   ├── unit/                  # Pure logic, no I/O (<1s)
│   ├── integration/           # Mock backend, no LLM (~5s)
│   ├── e2e/                   # Requires running proxy + backend
│   └── fixtures/              # Shared test fixtures
├── docs/                      # Project documentation (24+ files)
│   ├── 01-requirements-product/
│   ├── 02-architecture-design/
│   ├── 03-experiments-testing/
│   ├── 04-analysis-diagnostics/
│   ├── 05-operations-changelog/
│   ├── 06-reference-metrics/
│   ├── DEFECT-LIST.md         # 30 defects (7 P0 + 8 P1 + 10 P2 + 5 P3)
│   ├── OSS-REPLACEMENT-EVALUATION.md
│   ├── PM-ANALYSIS-FUTURE-ROADMAP.md
│   └── README.md              # Documentation navigation
├── assets/
│   └── chat-templates/        # Fixed Qwen Jinja templates
├── .githooks/
│   └── pre-commit             # Pre-commit gate: runs --unit on every commit
├── BENCHMARK.md               # Performance test report (Chinese)
├── CHANGELOG.md               # Release changelog (v0.5.0-baseline)
├── CLAUDE.md                  # Legacy agent guide (keep in sync)
└── TROUBLESHOOTING.md         # Incident records and fixes (Chinese)
```

### Runtime artifacts (not in git)

- `llama-server.pid` — PID file written by `manage.sh`
- `anthropic_proxy.pid` — Proxy PID file written by `manage.sh`
- `logs/llama-server.log` — Combined stdout/stderr log of the backend process
- `logs/anthropic_proxy.log` — Proxy request/response log
- `logs/proxy_metrics.jsonl` — Structured per-request pipeline metrics
- `logs/proxy_requests.jsonl` — Structured request/response summary log
- `/tmp/anthropic_request_body.json` — Last proxy request body (debug)

---

## Service Management (`manage.sh`)

### Commands

```bash
./manage.sh start              # Start backend + proxy with current active config (local)
./manage.sh start-cloud        # Start proxy only, forwarding to cloud API (DeepSeek/OpenAI)
./manage.sh stop               # Graceful stop (fallback to kill -9)
./manage.sh status             # PID, memory, API health, current model, proxy status
./manage.sh restart            # Stop + start
./manage.sh logs [N]           # Tail last N lines of backend log (default 50)
./manage.sh proxy-logs [N]     # Tail last N lines of proxy log (default 50)
./manage.sh list               # List all available configs
./manage.sh switch <name>      # Symlink active.conf to <name>.conf
./manage.sh current            # Show current config details
```

### Configuration system

Configs are bash-sourcable files under `configs/*.conf`. `configs/active.conf` is
a symlink to the currently selected config. `manage.sh` sources `active.conf` on
startup (if it exists), then applies default values for any unset variables.

Key environment variables (with defaults):

| Variable | Default | Description |
|----------|---------|-------------|
| `LLAMA_BACKEND` | `llama-server` | Backend: `llama-server` or `rapid-mlx` |
| `LLAMA_MODEL` | `mlx-community/Qwen3.6-35B-A3B-4bit` | Model path or HuggingFace ID |
| `LLAMA_PORT` | `8081` | Backend listen port |
| `LLAMA_HOST` | `127.0.0.1` | Backend bind address |
| `LLAMA_CTX` | `131072` | Context length (llama-server only) |
| `LLAMA_BATCH` | `2048` | Batch size (llama-server only) |
| `LLAMA_UBATCH` | `512` | Micro-batch size (llama-server only) |
| `LLAMA_N_PREDICT` | `-1` | Max tokens to predict (llama-server only) |
| `LLAMA_THREADS` | `8` | CPU threads |
| `LLAMA_KV_K` | `q8_0` | K-cache quantization type |
| `LLAMA_KV_V` | `q8_0` | V-cache quantization type |
| `LLAMA_TEMP` | `0.6` | Sampling temperature |
| `LLAMA_TOP_P` | `0.95` | Top-p sampling |
| `LLAMA_TOP_K` | `20` | Top-k sampling |
| `LLAMA_PRESENCE_PENALTY` | `0.0` | Presence penalty (llama-server only) |
| `LLAMA_MIN_P` | `0.0` | Min-p sampling (llama-server only) |
| `LLAMA_THINKING` | `false` | Enable Qwen thinking mode (`false`/`true`/``) |
| `LLAMA_EXTRA_ARGS` | `--jinja --flash-attn on --fit on` | Extra CLI flags |

Cloud API specific variables (used when `BACKEND_TYPE=cloud`):

| Variable | Default | Description |
|----------|---------|-------------|
| `LLAMA_BASE_URL` | `https://api.deepseek.com/v1` | Cloud API base URL |
| `LLAMA_API_KEY` | (none) | **Real** API key for cloud service (not a dummy token) |
| `MODEL_NAME` | `deepseek-v4-pro` | Cloud model identifier |
| `BACKEND_TYPE` | (auto-detected) | `local` or `cloud`; auto-detected from `LLAMA_BASE_URL` |
| `PROXY_MAX_CONCURRENT` | `4` (cloud) / `1` (local) | Max concurrent requests |

Rapid-MLX specific variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `RAPID_MLX_TOOL_PARSER` | `qwen3_coder` | Tool-call parser |
| `RAPID_MLX_REASONING_PARSER` | `qwen3` | Reasoning parser |
| `RAPID_MLX_ENABLE_PREFIX_CACHE` | `true` | Enable prefix cache |
| `RAPID_MLX_KV_QUANTIZATION` | `false` | Enable KV quantization |
| `RAPID_MLX_KV_QUANT_BITS` | `8` | KV quant bits |
| `RAPID_MLX_EXTRA_ARGS` | `` | Extra Rapid-MLX CLI flags |

Watchdog variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHDOG_INTERVAL` | `60` | Health check interval (seconds) |
| `WATCHDOG_TOK_THRESHOLD` | `15` | Min tok/s before auto-restart |
| `WATCHDOG_MAX_FAIL` | `3` | Consecutive health check failures before restart |

Config files also contain metadata fields (`CONFIG_NAME`, `CONFIG_DESC`,
`CONFIG_MEMORY`) used by `manage.sh list` for human-readable display.

### Startup behavior

1. Checks if service is already running (reads PID file, falls back to `pgrep`).
2. Checks port availability with `lsof`.
3. Launches backend via `nohup … >> llama-server.log 2>&1 &`.
4. Polls `http://host:port/v1/models` for up to 60 seconds to confirm readiness
   (with download-progress detection for HuggingFace models).
5. Writes backend PID to `llama-server.pid`.
6. Starts `anthropic_proxy.py` if not already running, writes PID to
   `anthropic_proxy.pid`.

---

## Proxy (`anthropic_proxy.py`)

### Startup

```bash
python3 anthropic_proxy.py
# or with env vars:
LLAMA_BASE_URL=http://127.0.0.1:8081/v1 PORT=4000 python3 anthropic_proxy.py
```

The proxy listens on `HOST:PORT` (default `127.0.0.1:4000`) and forwards to
`LLAMA_BASE_URL` (default `http://127.0.0.1:8081/v1`).

### Dual-mode design (local vs cloud)

The proxy automatically detects whether it is running in **local** or **cloud**
mode based on `LLAMA_BASE_URL`:

| Aspect | Local mode | Cloud mode |
|--------|-----------|------------|
| Detection | `LLAMA_BASE_URL` lacks `deepseek`, `openai`, or `api.` | `LLAMA_BASE_URL` contains `deepseek`, `openai`, or `api.` |
| `BACKEND_TYPE` | `local` | `cloud` |
| Backend process | `llama-server` or `rapid-mlx` on `:8081` | None (external API) |
| `LLAMA_API_KEY` | Dummy token (`sk-1234`) for backend compatibility | **Real API key** (required) |
| `MODEL_NAME` | Auto-set to local model (e.g., `mlx-community/Qwen3.6-35B-A3B-4bit`) | Auto-set to cloud model (e.g., `deepseek-v4-pro`) |
| `PROXY_MAX_CONCURRENT` | Default `1` (prevents OOM on 48GB) | Default `4` (cloud handles concurrency) |
| Concurrency control | `threading.Semaphore` around local backend | `threading.Semaphore` around cloud API |
| Token counting | Uses `timings.prompt_n` / `predicted_n` (llama-server) or `usage.*` | Uses `usage.prompt_tokens` / `completion_tokens` (OpenAI/DeepSeek) |
| Tool clearing | **Enabled** by default (threshold=15K, keep=2) | **Disabled** by default (1M+ token context) |
| Context limit | **Enabled** by default (limit=180K chars) | **Disabled** by default |
| Status page | Shows PID, memory, cache stats | Shows endpoint, model, masked API key |

**Key principle**: Claude Code always connects to `http://127.0.0.1:4000`.
Switching between local and cloud models is done by:
1. `./manage.sh switch <config>` (change `active.conf` symlink)
2. `./manage.sh restart` (or `./manage.sh start-cloud` for cloud)

**Never modify Claude Code configuration** (`~/.claude/settings.local.json`,
`ANTHROPIC_BASE_URL`, etc.) directly. The proxy is the single point of control.

### Supported endpoints

- `GET /v1/models` — Returns model aliases (Claude model IDs mapped to local model)
- `POST /v1/messages` — Anthropic Messages API (streaming and non-streaming)
- `GET /status` — HTML status page with real-time metrics, memory bars, and alerts
- `OPTIONS` — CORS preflight

### 8-layer request pipeline

The proxy processes every request through an 8-layer pipeline (documented in
`docs/02-architecture-design/proxy-pipeline-reference.md`):

1. **Request Entry** (`Handler.do_POST`) — routing, header masking, request dedup, JSON parse, session tracking, metrics init
2. **Semantic Preprocessing** — error translation, tool-result clearing, placeholder preservation
3. **Loop & Blocker Guard** — exact/pattern loop detection, escalating intervention, re-read detection
4. **Cache Optimizer** — date normalization, thinking clearing, cleared-content compression
5. **Context Truncator** — rounds/fifo/char strategies, three-tier compression, incremental summary
6. **Format & Forward** — Anthropic→OpenAI conversion, tool filtering, backend forwarding
7. **Response Control** — streaming/non-streaming SSE reconstruction, output truncation, JSON repair
8. **Observability** — metrics JSONL logging, request/response JSONL logging

### Format conversions

The proxy performs bidirectional translation between Anthropic and OpenAI formats:

1. **Messages** — Anthropic `user`/`assistant` with complex content blocks
   (`text`, `tool_use`, `tool_result`) → OpenAI `user`/`assistant`/`tool`.
2. **Tools** — Anthropic `custom` tool type → OpenAI `function` tool type.
3. **Tool choice** — Anthropic `auto`/`any`/`none`/`tool` → OpenAI equivalents.
4. **Streaming** — Reconstructs Anthropic SSE events
   (`message_start`, `content_block_start/delta/stop`, `message_delta/stop`)
   from OpenAI streaming chunks.
5. **Tool calls** — Extracts `tool_calls` from OpenAI assistant messages and
   emits Anthropic `tool_use` blocks.

### Proxy-side tool-result clearing

To prevent long agentic sessions from exhausting context window, the proxy
can truncate old `tool_result` contents while keeping the most recent
`PROXY_TOOL_KEEP` pairs intact. This mimics Anthropic's context management
without native API support.

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_CLEAR_ENABLED` | `false` (cloud) / `true` (local) | Enable tool-result clearing. **Auto-disabled for cloud backends** (1M+ token context) |
| `PROXY_CLEAR_THRESHOLD` | `30000` (cloud) / `15000` (local) | Character threshold to trigger clearing |
| `PROXY_TOOL_KEEP` | `10` (cloud) / `2` (local) | Number of recent tool_result pairs to preserve |
| `PROXY_CONTENT_TOOLS_FALLBACK` | `true` | Enable `<tools>` content-text extraction |
| `PROXY_MAX_CONCURRENT` | `4` (cloud) / `1` (local) | Max concurrent requests forwarded to backend |
| `PROXY_CTX_LIMIT_ENABLED` | `false` (cloud) / `true` (local) | Enable message truncation when context exceeds limit |
| `PROXY_CTX_CHARS_LIMIT` | `500000` (cloud) / `180000` (local) | Character limit for context truncation (char strategy) |
| `PROXY_CTX_TRUNCATE_STRATEGY` | `char` | Truncation strategy: `char` = threshold-based, `rounds` = keep last N assistant rounds with token budget, `fifo` = fixed message count |
| `PROXY_CTX_KEEP_ROUNDS` | `10` | Max number of recent assistant rounds to preserve (rounds strategy) |
| `PROXY_CTX_KEEP_MESSAGES` | `40` | Total messages to keep (fifo strategy) |
| `PROXY_CTX_KEEP_HEAD` | `2` | Keep first N messages (system context + skills) |
| `PROXY_CTX_KEEP_TAIL` | `4` | Keep last N messages |
| `PROXY_CTX_TOKEN_BUDGET` | `30000` | Prompt tokens budget上限 (rounds strategy), triggers dynamic round reduction |
| `PROXY_CTX_TOKEN_RATIO` | `2.0` | Chars-to-tokens estimation ratio for budget calculation |
| `PROXY_MAX_TOKENS_OVERRIDE` | `0` | Hard cap on `max_tokens` (0 = disabled); works around rapid-mlx ignoring max_tokens |
| `PROXY_OUTPUT_TOKEN_LIMIT_RATIO` | `2.0` | Multiplier applied to max_tokens for output safety margin |
| `PROXY_BACKEND_TIMEOUT` | `300` | Backend request timeout in seconds |
| `PROXY_PRE_TRUNCATE_CHARS` | `400000` | Pre-truncate very large payloads to prevent OOM/timeout |
| `PROXY_RETRY_AFTER_SECONDS` | `30` | Retry-After header value (seconds) for 503/504 responses |
| `PROXY_DEDUP_WINDOW` | `2` | Deduplication window (seconds) for detecting duplicate POST requests via body hash |

### Proxy-side error classification and retry (DEF-001)

The proxy classifies unhandled exceptions in `do_POST` via `_classify_exception(e)`
and returns the appropriate HTTP status code with structured error JSON:

| Error class | HTTP status | `error.type` | Retryable | Example |
|-------------|-------------|--------------|-----------|---------|
| Backend OOM / resource exhaustion | 503 | `backend_oom` | Yes | `[METAL] Insufficient Memory`, `out of memory` |
| Backend timeout | 504 | `timeout_error` | Yes | `urllib.error.URLError: timed out` |
| Backend unavailable / connection refused | 503 | `backend_unavailable` | Yes | `ConnectionRefusedError` |
| Programming error (KeyError, TypeError, etc.) | 500 | `internal_error` | No | — |
| Unknown | 500 | `unknown_error` | No | — |

For **retryable** errors (503/504), the proxy adds a `Retry-After` response header
(RFC 7231 §7.1.3) with the value of `PROXY_RETRY_AFTER_SECONDS`, and includes a
`"retryable": true` field in the error JSON body. Well-behaved clients (Anthropic
SDK, Claude Code) can use this to back off automatically.

Detection uses both Python exception class names **and** message-substring matching,
since rapid-mlx raises generic `RuntimeError` for OOM/timeout conditions.

`_respond_json()` accepts an optional `extra_headers` dict for sending additional
headers before `end_headers()`.

### Proxy-side tool definition filtering

When enabled, the proxy reduces the number of tool definitions sent to the
backend by keeping only high-frequency core tools and recently-used tools.
This can save 5-8K tokens per request when 44 tools are defined.

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_TOOL_FILTER_ENABLED` | `true` (local) / `false` (cloud) | Enable tool definition filtering |
| `PROXY_TOOL_FILTER_MAX` | `20` | Only trigger filtering when tools exceed this count |
| `PROXY_TOOL_FILTER_RECENT` | `5` | Scan last N assistant rounds for used tools |

Always-kept tools: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `LS`,
`Task`, `WebFetch`, `WebSearch`, `TodoRead`, `TodoWrite`, `Skill`, `Agent`,
`NotebookEdit`, `EnterPlanMode`, `ExitPlanMode`, `AskUserQuestion`.

### Proxy-side structured metrics logging

Per-request pipeline metrics written to `logs/proxy_metrics.jsonl`. Each line
is a JSON object with input stats, per-step pipeline data, compression quality
flags, and timing.

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_METRICS_ENABLED` | `true` | Enable metrics JSONL logging |
| `PROXY_METRICS_DIR` | `logs` | Directory for `proxy_metrics.jsonl` |

### Proxy-side keyword index (BM25 MVP)

When context truncation drops messages, keywords (filenames, error types,
function names) are extracted from dropped messages and injected into the
tail if relevant to current context.

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_HISTORY_INDEX` | `rule` | Keyword index mode: `off` or `rule` (TF matching) |
| `PROXY_HISTORY_TOP_K` | `5` | Max keyword entries to inject |
| `PROXY_HISTORY_MAX_CHARS` | `500` | Max chars for injected keyword context |

### Proxy-side blocker detection

When the same tool fails the same way `PROXY_BLOCKER_THRESHOLD` times in a
row (e.g. `Read` keeps returning "file not found", or `Bash` keeps getting
parameter validation errors), the proxy injects a `[BLOCKER]` user message
into the tail. The message tells the model to stop retrying and either
switch tools or report the blocker to the user. This is a **stronger
escalation** than the loop-detection break notice (which only fires on
identical-arg loops) and addresses the case where the model keeps trying
slightly different args against a fundamentally broken path.

Disabled by default for cloud backends (1M+ context, low marginal value).

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_BLOCKER_ENABLED` | `true` (local) / `false` (cloud) | Enable blocker detection |
| `PROXY_BLOCKER_THRESHOLD` | `2` | Consecutive same-error results before injecting `[BLOCKER]` |

### Loop detection and intervention

The proxy tracks consecutive identical tool_use calls and escalating patterns.
When a loop is detected, it applies 3 levels of intervention:

- **Level 1**: Soft hint injected into the user message tail
- **Level 2**: Remove the looping tool from the tools list
- **Level 3**: Force plain-text mode (no tools) for one turn

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_LOOP_THRESHOLD` | `3` | Consecutive identical calls before Level 1 intervention |
| `PROXY_LOOP_LEVEL2` | `6` | Threshold for Level 2 (defaults to `PROXY_LOOP_THRESHOLD * 2`) |

### Special handling

- **XML→JSON fallback** (`parse_tool_arguments`): Qwen models occasionally emit
  XML-style tool calls instead of JSON (llama.cpp issue #21495). The proxy tries
  JSON → embedded JSON → XML extraction → heuristic fallback.
- **Content-text tool extraction** (`_extract_content_tool_calls` and
  `_StreamingToolsExtractor`): some Qwen models under Q4_K_M quantization
  emit `<tools>\n{"name":..., "arguments":{...}}\n</tools>` as plain content
  text instead of populating the `tool_calls` array. The proxy scans content
  text in both the non-streaming converter and a streaming state machine,
  parses the JSON body, and synthesises Anthropic `tool_use` blocks. Structured
  `tool_calls` always take precedence.
- **Reasoning content**: Qwen3.6's `reasoning_content` field is extracted; if
  regular `content` is empty, reasoning text is used as the response body.
- **Model aliases**: Clients can request `claude-3-5-sonnet-20241022`,
  `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5`, etc.; all map to
  the active model (local or cloud). When using DeepSeek's Anthropic-compatible
  endpoint, `claude-opus` maps to `deepseek-v4-pro` and `claude-haiku`/`sonnet`
  map to `deepseek-v4-flash`.
- **Tool IDs**: Some backends omit `tool_call_id` in streaming; the proxy
  generates synthetic IDs (`call_<hex>`) to satisfy Anthropic SDK requirements.
- **Error translation**: Known backend error patterns (`Wasted call`, `File does not exist`, `InputValidationError`) are rewritten into natural-language Chinese hints with solution suggestions.
- **Request deduplication** (`_check_dedup`, DEF-205): Hash-based dedup with `PROXY_DEDUP_WINDOW` (default 2s) window. Duplicate POSTs receive 429 + Retry-After. Prevents double-forwarding from client retries.
- **Sensitive header masking** (`_mask_sensitive`, DEF-302): `Authorization` and `X-Api-Key` headers are automatically masked (first 8 + last 4 chars) in all log output. Prevents API key leakage to log files.
- **Context-loss notice** (DEF-107): When truncation drops > 85% of messages, a `[System: Context severely truncated]` user message is injected to warn the model that earlier context is lost.
- **Tool filter observability** (DEF-104): `_filter_tools()` logs the `filtered_out` field (sorted list of removed tool names) for debugging whitelist effectiveness.

---

## Tools Directory (`tools/`)

| Script | Purpose | How to run |
|--------|---------|------------|
| `bench_mtp.py` | MTP model performance benchmark | `python3 tools/bench_mtp.py --quick` |
| `bench_rapidmlx.py` | Rapid-MLX throughput benchmark | `python3 tools/bench_rapidmlx.py` |
| `bench_agent.py` | Agentic end-to-end benchmark | `python3 tools/bench_agent.py` |
| `bench_compress.py` | Context compression benchmark | `python3 tools/bench_compress.py` |
| `cache_analyzer.py` | Prefix-cache hit-rate analyzer | `python3 tools/cache_analyzer.py` |
| `context_stress_test.py` | Long-context stress test | `python3 tools/context_stress_test.py` |
| `stress_test.py` | Load stress test | `python3 tools/stress_test.py` |
| `analyze_claude_semantics.py` | Semantic behavior analysis | `python3 tools/analyze_claude_semantics.py` |
| `analyze_experiment.py` | A/B experiment result analyzer | `python3 tools/analyze_experiment.py <log>` |
| `trace_requirements.py` | Requirement traceability checker | `python3 tools/trace_requirements.py --strict` |
| `logview.sh` | Unified log viewer | `./tools/logview.sh backend 100` |
| `sysmon.sh` | System monitoring | `./tools/sysmon.sh` |
| `modelmon.sh` | Model service monitoring | `./tools/modelmon.sh` |
| `memcheck.sh` | Detailed memory analysis | `./tools/memcheck.sh` |
| `run_experiment.sh` | A/B experiment runner | `./tools/run_experiment.sh` |

All automated tests live under `test/` (see `test/README.md`); the pre-commit hook
at `.githooks/pre-commit` runs the fast `--unit` tier on every commit.

---

## Code Style Guidelines

### Bash (`manage.sh`, `tools/*.sh`)

- `set -euo pipefail` at the top of every script.
- Functions prefixed with underscore are private/internal (e.g., `_load_config`).
- Public commands use `cmd_` prefix (e.g., `cmd_start`).
- Use `[[ ]]` for all conditionals.
- Use `local` for function-scoped variables.
- Color-coded output: `info`, `warn`, `error` helper functions.
- Comments and user-facing output are in **Chinese**.

### Python (`anthropic_proxy.py`, `tools/*.py`)

- Standard library **only** — do not add third-party dependencies.
- Top-level constants (`LLAMA_BASE`, `MODEL_NAME`, `MODEL_ALIASES`).
- Helper functions at module level, no classes except `Handler`.
- `log()` writes to stdout and `/tmp/anthropic_proxy.log` (or `PROXY_LOG_PATH`).
- Keep the proxy stateless; all request state lives in `Handler` instances.

### Config files (`configs/*.conf`)

- Bash-sourcable syntax (`KEY="value"`).
- Chinese comments for section headers.
- Metadata fields (`CONFIG_NAME`, `CONFIG_DESC`, `CONFIG_MEMORY`) for `list` display.
- Each config is self-contained; no inheritance or includes.

---

## Testing Strategy

All automated tests live under `test/` (see `test/README.md` for the full layout
and pre-commit instructions). Three tiers, in order of speed/cost:

| Tier          | Location                  | Runtime | What it needs                              |
|---------------|---------------------------|---------|--------------------------------------------|
| **unit**      | `test/unit/`              | <1s     | nothing (no I/O)                           |
| **integration** | `test/integration/`     | ~5s     | nothing (boots its own mock backend)       |
| **e2e**       | `test/e2e/`               | ~30-60s | a running proxy (port 4000) + backend      |

A pre-commit hook at `.githooks/pre-commit` runs the **unit** tier on every
`git commit`. Install with `git config core.hooksPath .githooks` on a fresh
clone. Skip with `SKIP_TESTS=1 git commit …` or `git commit --no-verify`.

### Unified runner

```bash
bash test/run_tests.sh --unit          # default if no flag
bash test/run_tests.sh --integration
bash test/run_tests.sh --e2e
bash test/run_tests.sh --all           # unit + integration + e2e + trace
bash test/run_tests.sh --fast          # alias for --unit (pre-commit uses this)
bash test/run_tests.sh --trace         # requirement traceability (docs/requirements.yaml)
```

### Unit tests

`test/unit/test_proxy_fallback.py` contains `unittest` tests for:
- `_extract_content_tool_calls` (non-streaming `<tools>` fallback)
- `_StreamingToolsExtractor` (streaming state machine)
- `convert_openai_response_to_anthropic` (full response conversion)
- `_detect_blocker_pattern` (blocker detection — same-type run, mixed types, threshold, breaks)
- `_build_blocker_message` (cache-stability, tool/error metadata)
- `_compress_middle_with_llm` prompt structure (`Root cause:`/`Fix:`/`Avoidance:`)
- `truncate_messages_if_needed` (FIFO placeholder cache-stability)
- `_filter_tools` (tool definition filtering)
- `_translate_tool_result_errors` (error translation patterns)

Run directly:
```bash
python3 test/unit/test_proxy_fallback.py
# or
python3 -m unittest discover -s test/unit -p 'test_*.py' -v
```

### Integration tests

`test/integration/test_blocker_integration.sh` boots
`test/integration/mock_backend.py` (a tiny OpenAI-compatible mock) and the
proxy once, then runs 7 test cases against the running proxy, asserting that
the `[BLOCKER]` user message is (or is not) injected into the body forwarded
to the backend:

| TC | Scenario | Expected |
|----|----------|----------|
| 1 | 2× `file_not_found` (Read) | trigger, Read/file_not_found, run=2 |
| 2 | 2× `Wasted call` (Read) | trigger, Read/wasted |
| 3 | 2× `InputValidationError` (Bash) | trigger, Bash/input_validation |
| 4 | 3× `file_not_found` (Read) | trigger, message says "3 times" |
| 5 | 1× `file_not_found` only | no trigger (below threshold) |
| 6 | mixed types (wasted + file_not_found) | no trigger (type change breaks run) |
| 7 | 2 errors → 1 success → 1 error | no trigger (success breaks run) |

The script also dumps a per-request metrics summary from
`logs/itest/proxy_metrics.jsonl` showing which requests triggered the
blocker and why. No real LLM is required.

### End-to-end tests

`test/e2e/e2e_tools_fallback.sh` hits the live proxy (requires backend + proxy
running) and validates:
1. Non-streaming tool call returns correct `tool_use` block
2. Streaming tool call emits correct SSE event sequence
3. Plain chat without tools still works

`test/e2e/test_proxy_integration.py` is a 12-case matrix covering route
discovery, simple chat, Chinese, tool use, multi-turn tool flows, streaming,
session continuity, concurrency, count_tokens, long context, special chars,
and Anthropic SDK headers. Both sub-suites run under `test/run_tests.sh --e2e`.

### Requirement traceability

`tools/trace_requirements.py` audits `docs/requirements.yaml` against code
anchors and test coverage. Run via `bash test/run_tests.sh --trace`.

### Manual validation

1. **Backend health**: `./manage.sh status` checks process + API endpoint.
2. **Proxy health**: `curl http://127.0.0.1:4000/v1/models`.
3. **Status page**: Open `http://127.0.0.1:4000/status` in a browser.
4. **End-to-end**: Send an Anthropic-format request through the proxy and verify
   response format and content.
5. **Benchmarking**: `python3 tools/bench_mtp.py --quick` measures MTP throughput.

When modifying `anthropic_proxy.py`, run **all three tiers** (`bash test/run_tests.sh --all`),
covering both streaming and non-streaming paths, with and without tool calls,
in both local and cloud modes.

When modifying `manage.sh`, test `start`, `stop`, `restart`, `switch`, and
`status` for both backends.

---

## Deployment Process

This is a **single-machine, single-user** setup. Deployment steps:

1. Ensure `llama-server` (from upstream llama.cpp) or `rapid-mlx` is installed
   and on `$PATH`.
2. Select config: `./manage.sh switch <config_name>`.
3. Start backend + proxy: `./manage.sh start`.
4. Point client SDK to `http://127.0.0.1:4000`.

No containerization, no CI/CD, no package management. Both backend and proxy
run as detached processes managed by `manage.sh`.

### Building llama-server from source (for MTP support)

Brew's `llama-server` lacks MTP support. Build from source:

```bash
git clone https://github.com/ggml-org/llama.cpp /tmp/llama.cpp
cmake /tmp/llama.cpp -B /tmp/llama.cpp/build -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=OFF
cmake --build /tmp/llama.cpp/build --config Release -j --target llama-server
# Binary: /tmp/llama.cpp/build/bin/llama-server
```

Set `LLAMA_SERVER_BIN` env var or update `tools/bench_mtp.py`'s constant.

---

## Known Issues & Limitations

Documented in `BENCHMARK.md` (Chinese), `docs/DEFECT-LIST.md`, and briefly here
for agent context:

1. **Rapid-MLX ignores `max_tokens`** (v0.6.30) — requests may generate far more
   tokens than requested. Workaround: `PROXY_MAX_TOKENS_OVERRIDE` enforces a
   hard cap in the proxy.
2. **llama.cpp poor Qwen3.5-9B performance** — Gated DeltaNet architecture has
   incomplete Metal support; only ~17 tok/s. This model config has been removed;
   use Rapid-MLX or Qwen3.6-27B-MTP instead.
3. **KV cache restore errors** — `state_seq_set_data` errors appear in
   `llama-server.log`; non-fatal but indicate compatibility quirks with Qwen3.x.
4. **Concurrency limits on Apple Silicon** — Metal single-GPU time-slicing is
   inefficient; 2+ concurrent requests cause severe latency spikes on llama-server.
   Rapid-MLX handles multiple concurrent small requests well, but **two concurrent
   large-context (>38K tokens) requests on Rapid-MLX will reliably OOM** on 48GB
   unified memory. The proxy uses `PROXY_MAX_CONCURRENT` via a `threading.Semaphore`
   to control forwarding: `1` for llama-server configs, and **`1` for rapid-mlx-35b**
   (was `4`, reduced after repeated `[METAL] Insufficient Memory` crashes).
5. **Rapid-MLX OOM on 48GB unified memory** — `allocation_limit` (set via
   `--gpu-memory-utilization`) is a **soft target**, not a hard wall. Prefill-phase
   activations + KV cache + prefix cache can overshoot by 20–40% (e.g. limit=28GB,
   actual peaks at 33–39GB). Known crash signature: `[METAL] Command buffer
   execution failed: Insufficient Memory`. Prefix cache accumulating to 6GB+
   drastically increases risk. Mitigation: `PROXY_MAX_CONCURRENT=1`,
   `--gpu-memory-utilization 0.60` (allocation_limit ≈24GB), keep
   `--cache-memory-percent 0.30` with memory-aware cache enabled. The
   `forced cache clear` triggered at 30GB does not prevent the crash.
6. **Proxy `MODEL_NAME` auto-detection** — `MODEL_NAME` is now automatically
   set based on `BACKEND_TYPE` (local: `mlx-community/Qwen3.6-35B-A3B-4bit`,
   cloud: `deepseek-v4-pro`). Manual override via `MODEL_NAME` env var is still
   supported for edge cases.
7. **MTP requires source-built llama-server** — Brew version lacks
   `--spec-type draft-mtp`. Use config `qwen3.6-27b-mtp` with a manually built
   binary (the `qwen3.6-35b-mtp` config has been removed).
8. **Cloud API cost risk** — DeepSeek `deepseek-v4-pro` charges per token.
   A typical agentic coding task with 56K tokens/request × 20 requests costs
   approximately ¥1–3. Monitor usage via proxy logs (`REQ_SUMMARY` lines).
9. **Cloud mode observability loss** — When using cloud APIs, the proxy loses
   visibility into: exact TTFT (network latency overlay), memory pressure,
   prefix cache effectiveness, and forced cache clears. Only request size,
   tool-call frequency, and message structure remain observable.
10. **`deepseek-chat` deprecation** — DeepSeek's `deepseek-chat` and
    `deepseek-reasoner` model names will be deprecated on 2026-07-24. Use
    `deepseek-v4-pro` and `deepseek-v4-flash` instead.
11. **P0 defects at v0.5.0-baseline** — `docs/DEFECT-LIST.md` tracks 30 defects
    including 7 P0 (22% 500 error rate, 37% loop injection rate, re_read_rate
    formula error, tool-filter recent scan failure, Metal OOM, kernel panic risk,
    chat-template fix not toolized). Review this file before attempting fixes.

---

## Security Considerations

- **No authentication** on either the backend (`:8081`) or the proxy (`:4000`).
  Both bind to `127.0.0.1` by default, but any local process can access them.
- **No input validation** beyond JSON parsing in the proxy. Maliciously crafted
  Anthropic requests may propagate to the backend unchecked.
- **Log files** (`llama-server.log`, `anthropic_proxy.log`, `/tmp/anthropic_proxy.log`)
  may contain prompt data. They are world-readable on typical `/tmp` setups.
- **No HTTPS** — all traffic is plain HTTP on localhost.
- **Do not expose ports to the public internet** without an authentication layer.
- The proxy uses a dummy bearer token (`sk-1234`) when forwarding to a **local**
  backend; this is for backend compatibility, not security.
- In **cloud mode**, the proxy forwards the **real `LLAMA_API_KEY`** to the cloud
  API provider. Ensure `LLAMA_API_KEY` is properly protected (e.g., via env vars,
  not hard-coded in config files checked into git).
- **Sensitive header masking** (DEF-302): `_mask_sensitive()` automatically redacts
  `Authorization` and `X-Api-Key` headers in all log output, displaying them as
  `sk-123456****wxyz` (first 8 + last 4 chars).

---

## Agent Checklist When Editing

- [ ] If you change backend startup flags in `manage.sh`, test `start`, `stop`,
      `restart`, and `status` for both `llama-server` and `rapid-mlx` backends.
      Also test `start-cloud` and cloud-mode `status`.
- [ ] If you modify `anthropic_proxy.py`, run `bash test/run_tests.sh --all`
      (covers unit, integration, e2e, and trace tiers — the e2e tier needs a running
      proxy + backend). Test both local and cloud modes. The pre-commit hook
      runs only `--unit` for speed; manually run the other tiers before push.
- [ ] If you add a new config variable, add it to the defaults in `manage.sh` and
      document it in `CLAUDE.md` and this file.
- [ ] When adding a new backend type (cloud API provider), ensure `BACKEND_TYPE`
      auto-detection in `anthropic_proxy.py` covers its URL pattern.
- [ ] When modifying context truncation, loop detection, or blocker logic,
      verify against `docs/DEFECT-LIST.md` to avoid re-introducing known P0 issues.
- [ ] Keep `CLAUDE.md` and `AGENTS.md` in sync when architectural changes happen.
