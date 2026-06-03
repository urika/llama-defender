<!-- From: /Users/jinsongwang/APP/llama.cpp/AGENTS.md -->
# AGENTS.md — Local LLM Inference Stack

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
├── anthropic_proxy.py         # Anthropic→OpenAI proxy (python3)
├── configs/
│   ├── active.conf            # Symlink to the currently active config
│   ├── deepseek-chat.conf     # Cloud proxy → DeepSeek API (no local backend)
│   ├── qwen3.6-27b-mtp.conf   # llama-server + Qwen3.6-27B-MTP (GGUF)
│   └── rapid-mlx-35b.conf     # rapid-mlx + Qwen3.6-35B-A3B (MLX)
├── tools/
│   ├── bench_mtp.py           # MTP model performance benchmark
│   ├── test_proxy_fallback.py # Unit tests for proxy fallback logic
│   ├── e2e_tools_fallback.sh  # End-to-end proxy tool-call test
│   ├── logview.sh             # Unified log viewer
│   ├── sysmon.sh              # System monitoring (memory, CPU, disk)
│   ├── modelmon.sh            # Model service monitoring
│   └── memcheck.sh            # Detailed memory analysis
├── BENCHMARK.md               # Performance test report (Chinese)
└── CLAUDE.md                  # Legacy agent guide (keep in sync)
```

### Runtime artifacts (not in git)

- `llama-server.pid` — PID file written by `manage.sh`
- `anthropic_proxy.pid` — Proxy PID file written by `manage.sh`
- `logs/llama-server.log` — Combined stdout/stderr log of the backend process
- `logs/anthropic_proxy.log` — Proxy request/response log
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
- `OPTIONS` — CORS preflight

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
| `PROXY_CTX_TRUNCATE_STRATEGY` | `char` | Truncation strategy: `char` = threshold-based, `rounds` = keep last N assistant rounds with token budget |
| `PROXY_CTX_KEEP_ROUNDS` | `10` | Max number of recent assistant rounds to preserve (rounds strategy) |
| `PROXY_CTX_TOKEN_BUDGET` | `30000` | Prompt tokens budget上限 (rounds strategy), triggers dynamic round reduction |
| `PROXY_CTX_TOKEN_RATIO` | `1.3` | Chars-to-tokens estimation ratio for budget calculation |
| `PROXY_CTX_KEEP_ROUNDS_DYNAMIC` | `true` | Dynamically adjust keep_rounds based on total message count |

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

---

## Tools Directory (`tools/`)

| Script | Purpose | How to run |
|--------|---------|------------|
| `bench_mtp.py` | MTP model performance benchmark | `python3 tools/bench_mtp.py --quick` |
| `test_proxy_fallback.py` | Unit tests for proxy tool-call fallback | `python3 tools/test_proxy_fallback.py` |
| `e2e_tools_fallback.sh` | End-to-end proxy tool-call test | `bash tools/e2e_tools_fallback.sh` (needs running backend) |
| `logview.sh` | Unified log viewer | `./tools/logview.sh backend 100` |
| `sysmon.sh` | System monitoring | `./tools/sysmon.sh` |
| `modelmon.sh` | Model service monitoring | `./tools/modelmon.sh` |
| `memcheck.sh` | Detailed memory analysis | `./tools/memcheck.sh` |

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

### Unit tests

`tools/test_proxy_fallback.py` contains `unittest` tests for:
- `_extract_content_tool_calls` (non-streaming `<tools>` fallback)
- `_StreamingToolsExtractor` (streaming state machine)
- `convert_openai_response_to_anthropic` (full response conversion)

Run with:
```bash
python3 tools/test_proxy_fallback.py
# or
python3 -m unittest discover -s tools -p 'test_*.py' -v
```

### End-to-end tests

`tools/e2e_tools_fallback.sh` hits the live proxy (requires backend + proxy
running) and validates:
1. Non-streaming tool call returns correct `tool_use` block
2. Streaming tool call emits correct SSE event sequence
3. Plain chat without tools still works

Run with:
```bash
bash tools/e2e_tools_fallback.sh
```

### Manual validation

1. **Backend health**: `./manage.sh status` checks process + API endpoint.
2. **Proxy health**: `curl http://127.0.0.1:4000/v1/models`.
3. **End-to-end**: Send an Anthropic-format request through the proxy and verify
   response format and content.
4. **Benchmarking**: `python3 tools/bench_mtp.py --quick` measures MTP throughput.

When modifying `anthropic_proxy.py`, run **both** unit tests and e2e tests,
for both streaming and non-streaming paths, with and without tool calls.

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

Documented in `BENCHMARK.md` (Chinese) and briefly here for agent context:

1. **Rapid-MLX ignores `max_tokens`** (v0.6.30) — requests may generate far more
   tokens than requested. Workaround: use `llama-server` when token limits matter.
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

---

## Agent Checklist When Editing

- [ ] If you change backend startup flags in `manage.sh`, test `start`, `stop`,
      `restart`, and `status` for both `llama-server` and `rapid-mlx` backends.
      Also test `start-cloud` and cloud-mode `status`.
- [ ] If you modify `anthropic_proxy.py`, run `python3 tools/test_proxy_fallback.py`
      and `bash tools/e2e_tools_fallback.sh` (with proxy + backend running).
      Test both local and cloud modes.
- [ ] If you add a new config variable, add it to the defaults in `manage.sh` and
      document it in `CLAUDE.md` and this file.
- [ ] When adding a new backend type (cloud API provider), ensure `BACKEND_TYPE`
      auto-detection in `anthropic_proxy.py` covers its URL pattern.
- [ ] Keep `CLAUDE.md` and `AGENTS.md` in sync when architectural changes happen.
