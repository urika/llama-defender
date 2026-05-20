# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

This directory is **not** the llama.cpp C++ source — it's an orchestration layer that wraps an LLM backend binary and exposes an Anthropic-compatible API. Target: running Qwen-family models on Apple Silicon (M-series, 48 GB unified memory) for agentic coding with Claude Code.

```
Client (Anthropic SDK) → anthropic_proxy.py:4000 → llama-server | rapid-mlx :8081 → model
```

Three core pieces:

- **`manage.sh`** — Bash service manager for the backend. Sources `configs/active.conf` (a symlink to the currently selected `configs/*.conf`), applies defaults for unset variables, and starts `llama-server` (GGUF) or `rapid-mlx` (MLX). Logs to `logs/llama-server.log`, PID in `llama-server.pid`.
- **`anthropic_proxy.py`** — Python 3 stdlib-only HTTP proxy (port 4000). Translates Anthropic Messages API ↔ OpenAI chat completions:
  - Bidirectional message/tool/tool_choice/SSE conversion
  - XML→JSON fallback for Qwen tool-calling quirks (llama.cpp issue #21495) via `parse_tool_arguments()`
  - Reasoning content extraction for Qwen thinking mode
  - Model aliases — clients can request `claude-3-5-sonnet-20241022`, `claude-sonnet-4-6`, `claude-opus-4-7`, etc.; all map to the active model
  - Optional `tool_result` truncation for long agentic sessions (env vars `PROXY_CLEAR_ENABLED`, `PROXY_CLEAR_THRESHOLD`, `PROXY_TOOL_KEEP`)
- **`qwen35-template.jinja`** — Strict Jinja template for Qwen3.5/3.6 (passed via `--jinja` when needed). Supports tool blocks, vision tokens, `<think>` reasoning, and Claude Code deferred-tool patterns. Raises on malformed message structure. Qwen2.5-Coder uses the GGUF's built-in template instead.

`AGENTS.md` contains the full reference (config variables, format conversion details, known issues, security notes); keep it in sync with this file when architectural changes happen.

## Service management

```bash
./manage.sh start                 # Start backend with active.conf
./manage.sh stop                  # Graceful, then kill -9
./manage.sh status                # PID, memory, API health, current model
./manage.sh restart               # Stop + start
./manage.sh logs [N]              # Tail last N lines (default 50)
./manage.sh list                  # All available configs
./manage.sh switch <name>         # Symlink active.conf → <name>.conf
./manage.sh current               # Current config details
```

Startup polls `http://host:port/v1/models` for up to 60 s to confirm readiness, then writes the PID file.

## Available configurations

Configs live in `configs/*.conf` as bash-sourcable files. `configs/active.conf` is a symlink to the currently active one.

| Config | Backend | Model | Context | Memory | Use case |
|--------|---------|-------|---------|--------|----------|
| `qwen3.6-35b` | llama-server | Qwen3.6-35B-A3B IQ4_XS | 131072 | ~22 GB | High-quality single-user coding (MoE) |
| `qwen3.5-9b` | llama-server | Qwen3.5-9B UD-Q4_K_XL | 65536 | ~5.6 GB | 2–3 concurrent Claude Code |
| `qwen3.5-9b-coding` | llama-server | Qwen3.5-9B Q5_K_M | 65536 | ~6.1 GB | Higher-precision algorithm work |
| `qwen2.5-coder-32b` | llama-server | Qwen2.5-Coder-32B-Instruct Q4_K_M | 32768 | ~20 GB | Dedicated coding model (Dense 32B) |
| `rapid-mlx-35b` | rapid-mlx | Qwen3.6-35B-A3B 4bit MLX | (model max) | ~16–17 GB | 36% faster than llama-server, better concurrency |

Each config sets `LLAMA_*` env vars (model, port, context, sampling, KV-cache type, thinking mode, extra args) plus optional `RAPID_MLX_*` vars. Metadata fields (`CONFIG_NAME`, `CONFIG_DESC`, `CONFIG_MEMORY`) are read by `./manage.sh list`. Defaults for any unset variable are applied in `manage.sh` itself.

## Proxy

```bash
python3 anthropic_proxy.py                                            # listens on 127.0.0.1:4000
LLAMA_BASE_URL=http://127.0.0.1:8081/v1 PORT=4000 python3 anthropic_proxy.py
```

Endpoints: `GET /v1/models`, `POST /v1/messages` (streaming + non-streaming), `OPTIONS`. Stateless, no third-party deps.

**Important when switching backends**: `MODEL_NAME` is hard-coded at the top of `anthropic_proxy.py` and must match the active backend's model identifier (e.g., `unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS` for the GGUF backend vs. `mlx-community/Qwen3.6-35B-A3B-4bit` for Rapid-MLX). Forgetting this breaks `/v1/models` discovery.

## Key implementation details

- **Model loading**: `LLAMA_MODEL` starting with `/` or `./` is treated as a local path (`-m`); otherwise it's a HuggingFace ID (`-hf`).
- **Thinking mode**: `LLAMA_THINKING=false|true` → `--chat-template-kwargs '{"enable_thinking":...}'`. Empty string skips the flag (use for models that don't support thinking, e.g., Qwen2.5).
- **KV cache**: Default `q8_0` for both K and V — Unsloth's recommendation for Qwen to avoid f16 accuracy degradation.
- **Concurrency caveat**: `llama-server` on Metal time-slices a single GPU; 2+ concurrent requests cause severe latency spikes. Rapid-MLX handles 2–4 concurrent requests much better (see `BENCHMARK.md`).
- **Rapid-MLX `max_tokens` bug** (v0.6.30): parameter is accepted but ignored — generations can run far past the limit. Use `llama-server` when token limits matter.
- **Tool-call fallback layers**: the proxy recognises tool calls in three increasing-cost layers — structured `tool_calls` JSON (preferred; Qwen3.x, Rapid-MLX), `parse_tool_arguments` XML→JSON salvage on the args string (`<tool_call>` / `<function=…>` quirks of llama.cpp issue #21495), and `_extract_content_tool_calls` content-text fallback for `<tools>...</tools>` blocks (Qwen2.5-Coder under Q4 quantisation emits these instead of populating `tool_calls`). Structured tool_calls always win when present. Gate: `PROXY_CONTENT_TOOLS_FALLBACK` (default `true`).
- **Testing**: unit tests at `tools/test_proxy_fallback.py` (`python3 tools/test_proxy_fallback.py`); end-to-end at `tools/e2e_tools_fallback.sh` (requires proxy + backend running). When modifying `anthropic_proxy.py`, run both — tool-call paths (streaming and non-streaming) are easy to break.

## Code style

- `manage.sh`: `set -euo pipefail`. Private helpers prefixed `_`, public commands prefixed `cmd_`. User-facing strings and comments are in **Chinese**.
- `anthropic_proxy.py`: **standard library only** — no third-party deps. Top-level constants, helpers as module-level functions, one `Handler` class. Logs to stdout *and* `/tmp/anthropic_proxy.log`.
- Config files: bash-sourcable `KEY="value"` syntax, Chinese section headers, self-contained (no includes), include the `CONFIG_NAME`/`CONFIG_DESC`/`CONFIG_MEMORY` metadata.
