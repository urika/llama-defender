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

Primary use-case: running **Qwen3.6-35B-A3B** (and occasionally Qwen3.5-9B) on
Apple Silicon (MacBook Pro M5 Pro, 48 GB unified memory) for agentic coding
workflows, specifically with Claude Code.

### High-level data flow

```
Client (Anthropic SDK) → anthropic_proxy.py:4000 → llama-server/rapid-mlx:8081 → GGUF/MLX model
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
| Backend option 1 | `llama-server` binary from upstream llama.cpp (GGUF) |
| Backend option 2 | `rapid-mlx` binary (MLX framework, Apple-optimized) |
| Chat template | Custom Jinja2 template (`qwen35-template.jinja`) |
| OS target | macOS with Metal (Apple Silicon) |

**No build tools** (no `pyproject.toml`, `package.json`, `Cargo.toml`, `Makefile`,
etc.). The project is a collection of runnable scripts and configuration files.

---

## File Organization

```
.
├── manage.sh                  # Main service manager (bash)
├── anthropic_proxy.py         # Anthropic→OpenAI proxy (python3)
├── qwen35-template.jinja      # Chat template for Qwen3.5/3.6
├── configs/
│   ├── active.conf            # Symlink to the currently active config
│   ├── qwen3.6-35b.conf       # llama-server + Qwen3.6-35B-A3B (GGUF)
│   ├── qwen3.5-9b.conf        # llama-server + Qwen3.5-9B (GGUF)
│   ├── qwen3.5-9b-coding.conf # llama-server + Qwen3.5-9B higher precision
│   └── rapid-mlx-35b.conf     # rapid-mlx + Qwen3.6-35B-A3B (MLX)
├── BENCHMARK.md               # Performance test report (Chinese)
└── CLAUDE.md                  # Legacy agent guide (keep in sync)
```

### Runtime artifacts (not in git)

- `llama-server.pid` — PID file written by `manage.sh`
- `logs/llama-server.log` — Combined stdout/stderr log of the backend process
- `logs/anthropic_proxy.log` — Proxy request/response log
- `/tmp/anthropic_request_body.json` — Last proxy request body (debug)

---

## Service Management (`manage.sh`)

### Commands

```bash
./manage.sh start              # Start backend with current active config
./manage.sh stop               # Graceful stop (fallback to kill -9)
./manage.sh status             # PID, memory, API health, current model
./manage.sh restart            # Stop + start
./manage.sh logs [N]           # Tail last N lines (default 50)
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
| `LLAMA_MODEL` | `unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS` | Model path or HuggingFace ID |
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
| `LLAMA_THINKING` | `false` | Enable Qwen thinking mode |
| `LLAMA_EXTRA_ARGS` | `--jinja --flash-attn on --fit on` | Extra CLI flags |

Rapid-MLX specific variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `RAPID_MLX_TOOL_PARSER` | `qwen3_coder` | Tool-call parser |
| `RAPID_MLX_REASONING_PARSER` | `qwen3` | Reasoning parser |
| `RAPID_MLX_ENABLE_PREFIX_CACHE` | `true` | Enable prefix cache |
| `RAPID_MLX_KV_QUANTIZATION` | `false` | Enable KV quantization |
| `RAPID_MLX_KV_QUANT_BITS` | `8` | KV quant bits |

Config files also contain metadata fields (`CONFIG_NAME`, `CONFIG_DESC`,
`CONFIG_MEMORY`) used by `manage.sh list` for human-readable display.

### Startup behavior

1. Checks if service is already running (reads PID file, falls back to `pgrep`).
2. Checks port availability with `lsof`.
3. Launches backend via `nohup … >> llama-server.log 2>&1 &`.
4. Polls `http://host:port/v1/models` for up to 60 seconds to confirm readiness.
5. Writes PID to `llama-server.pid`.

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
| `PROXY_CLEAR_ENABLED` | `true` | Enable tool-result clearing |
| `PROXY_CLEAR_THRESHOLD` | `30000` | Character threshold to trigger clearing |
| `PROXY_TOOL_KEEP` | `5` | Number of recent tool_result pairs to preserve |

### Special handling

- **XML→JSON fallback** (`parse_tool_arguments`): Qwen models occasionally emit
  XML-style tool calls instead of JSON (llama.cpp issue #21495). The proxy tries
  JSON → embedded JSON → XML extraction → heuristic fallback.
- **Content-text tool extraction** (`_extract_content_tool_calls` and
  `_StreamingToolsExtractor`): Qwen2.5-Coder-32B under Q4_K_M quantization
  emits `<tools>\n{"name":..., "arguments":{...}}\n</tools>` as plain content
  text instead of populating the `tool_calls` array. The proxy scans content
  text in both the non-streaming converter and a streaming state machine,
  parses the JSON body, and synthesises Anthropic `tool_use` blocks. Structured
  `tool_calls` always take precedence. Env-var gate:
  `PROXY_CONTENT_TOOLS_FALLBACK` (default `true`).
- **Reasoning content**: Qwen3.6's `reasoning_content` field is extracted; if
  regular `content` is empty, reasoning text is used as the response body.
- **Model aliases**: Clients can request `claude-3-5-sonnet-20241022`,
  `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5`, etc.; all map to
  the local model.
- **Tool IDs**: Some backends omit `tool_call_id` in streaming; the proxy
  generates synthetic IDs (`call_<hex>`) to satisfy Anthropic SDK requirements.

---

## Chat Template (`qwen35-template.jinja`)

A custom Jinja2 chat template used by `llama-server --jinja`. Supports:

- Multi-modal content (image, video) with `vision_start`/`vision_end` tokens.
- Tool definition injection in system prompt.
- XML-style tool call formatting (`<tool_call>`, `<function=…>`, `<parameter=…>`).
- Tool response wrapping (`<tool_response>`).
- Reasoning tag extraction (`<think>` / `</think>`).
- Deferred tool patterns (Claude Code multi-step tool workflows).
- `enable_thinking` flag controlled via `LLAMA_THINKING`.

The template is **strict** — it raises Jinja exceptions for invalid message
structures (system message not first, system message containing images, missing
user query, unexpected roles, etc.).

---

## Code Style Guidelines

### Bash (`manage.sh`)

- `set -euo pipefail` at the top of the script.
- Functions prefixed with underscore are private/internal (e.g., `_load_config`).
- Public commands use `cmd_` prefix (e.g., `cmd_start`).
- Use `[[ ]]` for all conditionals.
- Use `local` for function-scoped variables.
- Color-coded output: `info`, `warn`, `error` helper functions.
- Comments and user-facing output are in **Chinese**.

### Python (`anthropic_proxy.py`)

- Standard library **only** — do not add third-party dependencies.
- Top-level constants (`LLAMA_BASE`, `MODEL_NAME`, `MODEL_ALIASES`).
- Helper functions at module level, no classes except `Handler`.
- `log()` writes to stdout and `/tmp/anthropic_proxy.log`.
- Keep the proxy stateless; all request state lives in `Handler` instances.

### Config files (`configs/*.conf`)

- Bash-sourcable syntax (`KEY="value"`).
- Chinese comments for section headers.
- Metadata fields (`CONFIG_NAME`, `CONFIG_DESC`, `CONFIG_MEMORY`) for `list` display.
- Each config is self-contained; no inheritance or includes.

---

## Testing Strategy

There is **no automated test suite** in this repository. Validation is manual:

1. **Backend health**: `./manage.sh status` checks process + API endpoint.
2. **Proxy health**: `curl http://127.0.0.1:4000/v1/models`.
3. **End-to-end**: Send an Anthropic-format request through the proxy and verify
   response format and content.
4. **Benchmarking**: Custom scripts (historically placed in `/tmp/`) measure
   throughput, TTFT, and concurrency behavior. See `BENCHMARK.md` for results.

When modifying `anthropic_proxy.py`, test both streaming and non-streaming
paths, with and without tool calls.

When modifying `manage.sh`, test `start`, `stop`, `restart`, `switch`, and
`status` for both backends.

---

## Deployment Process

This is a **single-machine, single-user** setup. Deployment steps:

1. Ensure `llama-server` (from upstream llama.cpp) or `rapid-mlx` is installed
   and on `$PATH`.
2. Select config: `./manage.sh switch <config_name>`.
3. Start backend: `./manage.sh start`.
4. Start proxy (in another terminal or via `screen`/`tmux`):
   `python3 anthropic_proxy.py`.
5. Point client SDK to `http://127.0.0.1:4000`.

No containerization, no CI/CD, no package management. Both backend and proxy
run as foreground/detached processes managed manually.

---

## Known Issues & Limitations

Documented in `BENCHMARK.md` (Chinese) and briefly here for agent context:

1. **Rapid-MLX ignores `max_tokens`** (v0.6.30) — requests may generate far more
   tokens than requested. Workaround: use `llama-server` when token limits matter.
2. **llama.cpp poor Qwen3.5-9B performance** — Gated DeltaNet architecture has
   incomplete Metal support; only ~17 tok/s. Use Rapid-MLX for this model.
3. **KV cache restore errors** — `state_seq_set_data` errors appear in
   `llama-server.log`; non-fatal but indicate compatibility quirks with Qwen3.x.
4. **llama-server poor concurrency** — Metal single-GPU time-slicing is
   inefficient; 2+ concurrent requests cause severe latency spikes. Rapid-MLX
   handles 2–4 concurrent requests much better.
5. **Proxy `MODEL_NAME` hard-coding** — When switching backend, you must also
   update `MODEL_NAME` in `anthropic_proxy.py` to match the backend's model
   identifier (see `BENCHMARK.md` § "代理层 MODEL_NAME 切换").

---

## Security Considerations

- **No authentication** on either the backend (`:8081`) or the proxy (`:4000`).
  Both bind to `127.0.0.1` by default, but any local process can access them.
- **No input validation** beyond JSON parsing in the proxy. Maliciously crafted
  Anthropic requests may propagate to the backend unchecked.
- **Log files** (`llama-server.log`, `/tmp/anthropic_proxy.log`) may contain
  prompt data. They are world-readable on typical `/tmp` setups.
- **No HTTPS** — all traffic is plain HTTP on localhost.
- **Do not expose ports to the public internet** without an authentication layer.
- The proxy uses a dummy bearer token (`sk-1234`) when forwarding to the backend;
  this is for backend compatibility, not security.

---

## Agent Checklist When Editing

- [ ] If you change backend startup flags in `manage.sh`, test `start` and `status`.
- [ ] If you modify `anthropic_proxy.py`, test both streaming and non-streaming
      requests, and verify tool-call XML fallback still works.
- [ ] If you add a new config variable, add it to the defaults in `manage.sh` and
      document it in `CLAUDE.md` and this file.
- [ ] If you modify `qwen35-template.jinja`, test with `llama-server --jinja` and
      verify tool/reasoning/vision paths.
- [ ] Keep `CLAUDE.md` and `AGENTS.md` in sync when architectural changes happen.
