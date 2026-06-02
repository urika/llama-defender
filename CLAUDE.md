# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

This directory is **not** the llama.cpp C++ source — it's an orchestration layer that wraps an LLM backend and exposes an Anthropic-compatible API. Target: running Qwen-family models locally on Apple Silicon (M-series, 48 GB unified memory) **or** forwarding to cloud APIs (DeepSeek) for agentic coding with Claude Code.

```
Local:  Client (Anthropic SDK) → anthropic_proxy.py:4000 → llama-server | rapid-mlx :8081 → model
Cloud:  Client (Anthropic SDK) → anthropic_proxy.py:4000 → DeepSeek/OpenAI API → cloud model
```

**Core principle**: Claude Code always connects to `http://127.0.0.1:4000`.
Backend switching is done entirely at the proxy layer — **never modify Claude Code
configuration directly** (no `~/.claude/settings.local.json` changes, no
`ANTHROPIC_BASE_URL` env vars in Claude Code).

Three core pieces:

- **`manage.sh`** — Bash service manager. Sources `configs/active.conf` (a symlink to the currently selected `configs/*.conf`), applies defaults, and starts either a local backend (`llama-server` or `rapid-mlx`) or a cloud-only proxy (`start-cloud`).
- **`anthropic_proxy.py`** — Python 3 stdlib-only HTTP proxy (port 4000). Dual-mode:
  - **Local mode**: Translates Anthropic ↔ OpenAI for `llama-server`/`rapid-mlx`
  - **Cloud mode**: Forwards to DeepSeek/OpenAI APIs with real API key, using `BACKEND_TYPE` auto-detection
  - Bidirectional message/tool/tool_choice/SSE conversion
  - XML→JSON fallback for Qwen tool-calling quirks (llama.cpp issue #21495)
  - Reasoning content extraction for Qwen thinking mode
  - Model aliases — all Claude model IDs map to the active model (local or cloud)
  - Optional `tool_result` truncation for long agentic sessions
- **`qwen35-template.jinja`** — Strict Jinja template for Qwen3.5/3.6 (local mode only).

`AGENTS.md` contains the full reference (config variables, format conversion details, known issues, security notes); keep it in sync with this file when architectural changes happen.

## Service management

```bash
./manage.sh start                 # Start local backend + proxy with active.conf
./manage.sh start-cloud           # Start proxy only, forwarding to cloud API
./manage.sh stop                  # Graceful, then kill -9
./manage.sh status                # PID, memory, API health, current model
./manage.sh restart               # Stop + start
./manage.sh logs [N]              # Tail last N lines (default 50)
./manage.sh proxy-logs [N]        # Tail last N lines of proxy log
./manage.sh list                  # All available configs
./manage.sh switch <name>         # Symlink active.conf → <name>.conf
./manage.sh current               # Current config details
```

Startup polls `http://host:port/v1/models` for up to 60 s to confirm readiness, then writes the PID file.

## Available configurations

Configs live in `configs/*.conf` as bash-sourcable files. `configs/active.conf` is a symlink to the currently active one.

| Config | Backend | Model | Context | Memory | Use case |
|--------|---------|-------|---------|--------|----------|
| `deepseek-chat` | cloud (DeepSeek) | `deepseek-v4-pro` | (API limit) | N/A | Cloud API, no local backend |
| `qwen3.6-27b-mtp` | llama-server | Qwen3.6-27B-MTP UD-Q4_K_XL | 131072 | ~19–20 GB | MTP Dense 27B, ~1.4× speedup |
| `rapid-mlx-35b` | rapid-mlx | Qwen3.6-35B-A3B 4bit MLX | (model max) | ~16–17 GB | 36% faster than llama-server, **concurrency=1 required** |

> **⚠️ rapid-mlx OOM 防范** (48GB unified memory):
> - `PROXY_MAX_CONCURRENT=1` — 两个 38K+ token 请求并发必然 OOM
> - `--gpu-memory-utilization 0.60` — rapid-mlx 的 `allocation_limit` 是**软限制**，实际使用会超出 20-40%（如 limit=28GB 实际冲到 37GB）。降低 limit 让引擎更早节流
> - 前缀缓存堆积到 6GB+ 后，再叠加一个大请求 prefill 极易触发 `[METAL] Insufficient Memory`。`--cache-memory-percent 0.30` + memory-aware cache 动态回收是必须的
> - 实测：并发=1 + utilization=60% 后，单 38K 请求稳定运行（TTFT ~28s），无 OOM

Each config sets `LLAMA_*` env vars (model, port, context, sampling, KV-cache type, thinking mode, extra args) plus optional `RAPID_MLX_*` vars. Metadata fields (`CONFIG_NAME`, `CONFIG_DESC`, `CONFIG_MEMORY`) are read by `./manage.sh list`. Defaults for any unset variable are applied in `manage.sh` itself.

## Proxy

```bash
python3 anthropic_proxy.py                                            # listens on 127.0.0.1:4000
LLAMA_BASE_URL=http://127.0.0.1:8081/v1 PORT=4000 python3 anthropic_proxy.py
```

Endpoints: `GET /v1/models`, `POST /v1/messages` (streaming + non-streaming), `OPTIONS`. Stateless, no third-party deps.

**Dual-mode auto-detection**: `BACKEND_TYPE` is automatically inferred from
`LLAMA_BASE_URL`:
- Contains `deepseek` / `openai` / `api.` → `cloud`
- Otherwise → `local`

`MODEL_NAME` is auto-set accordingly (local: `mlx-community/Qwen3.6-35B-A3B-4bit`,
cloud: `deepseek-v4-pro`). Manual override via `MODEL_NAME` env var is supported
but rarely needed.

| Mode | `LLAMA_BASE_URL` | `MODEL_NAME` (auto) | `LLAMA_API_KEY` | `PROXY_MAX_CONCURRENT` |
|------|------------------|---------------------|-----------------|------------------------|
| Local | `http://127.0.0.1:8081/v1` | `mlx-community/Qwen3.6-35B-A3B-4bit` | Dummy (`sk-1234`) | `1` |
| Cloud (DeepSeek) | `https://api.deepseek.com/v1` | `deepseek-v4-pro` | **Real key** | `4` |

When using DeepSeek's Anthropic-compatible endpoint, `claude-opus` maps to
`deepseek-v4-pro` and `claude-haiku`/`sonnet` map to `deepseek-v4-flash`.

## Key implementation details

- **Model loading**: `LLAMA_MODEL` starting with `/` or `./` is treated as a local path (`-m`); otherwise it's a HuggingFace ID (`-hf`).
- **Thinking mode**: `LLAMA_THINKING=false|true` → `--chat-template-kwargs '{"enable_thinking":...}'`. Empty string skips the flag (use for models that don't support thinking, e.g., Qwen2.5).
- **KV cache**: Default `q8_0` for both K and V — Unsloth's recommendation for Qwen to avoid f16 accuracy degradation.
- **Context management defaults tied to backend type**: `PROXY_CLEAR_ENABLED`, `PROXY_TOOL_KEEP`, `PROXY_CTX_LIMIT_ENABLED`, and `PROXY_CTX_CHARS_LIMIT` all have **backend-type-aware defaults**:
  - **Cloud** (DeepSeek/OpenAI): clearing **disabled** (1M+ token context), ctx-limit **disabled**
  - **Local** (llama-server/rapid-mlx): clearing **enabled** (threshold=15K, keep=2), ctx-limit **enabled** (limit=180K chars)
  - Override via env vars if needed, but the defaults handle the common case.
- **Concurrency caveat**: `llama-server` on Metal time-slices a single GPU; 2+ concurrent requests cause severe latency spikes. Rapid-MLX handles 2–4 concurrent requests much better. The proxy controls this via `PROXY_MAX_CONCURRENT` (default `1` for llama-server configs, `4` for Rapid-MLX configs) using a `threading.Semaphore`.
- **Rapid-MLX `max_tokens` bug** (v0.6.30): parameter is accepted but ignored — generations can run far past the limit. Use `llama-server` when token limits matter.
- **Rapid-MLX OOM on Apple Silicon** (48GB): `allocation_limit` is a soft target, not a hard wall. Prefill-phase activations + KV cache + prefix cache can overshoot by 20-40%. Known crash signature: `[METAL] Command buffer execution failed: Insufficient Memory`. Mitigation: `PROXY_MAX_CONCURRENT=1`, `--gpu-memory-utilization 0.60`, and avoid >40K token contexts when cache is already >6GB. `forced cache clear` at 30GB threshold does not prevent the crash.
- **Tool-call fallback layers**: the proxy recognises tool calls in three increasing-cost layers — structured `tool_calls` JSON (preferred; Qwen3.x, Rapid-MLX), `parse_tool_arguments` XML→JSON salvage on the args string (`<tool_call>` / `<function=…>` quirks of llama.cpp issue #21495), and `_extract_content_tool_calls` content-text fallback for `<tools>...</tools>` blocks (some Qwen models under Q4 quantisation emit these instead of populating `tool_calls`). Structured tool_calls always win when present. Gate: `PROXY_CONTENT_TOOLS_FALLBACK` (default `true`).
- **Cloud API forwarding**: In cloud mode, the proxy skips the local backend lock
  (`_llama_lock`) and forwards directly to the cloud API with the real
  `LLAMA_API_KEY`. Token counting uses `usage.prompt_tokens` / `completion_tokens`
  from the cloud response instead of `timings.*`.
- **Cloud cost**: DeepSeek `deepseek-v4-pro` costs ~¥2–8 per million tokens.
  A typical agentic coding task (56K tokens × 20 requests) costs approximately
  ¥1–3. Monitor via `REQ_SUMMARY` lines in proxy logs.
- **MTP (Multi-Token Prediction)**: Qwen3.6 supports MTP for ~1.15–1.4× faster generation. Requires MTP-specific GGUF models and a llama-server built from source with `--spec-type draft-mtp` support (Brew version lacks this). Use `--spec-type draft-mtp --spec-draft-n-max 2` in `LLAMA_EXTRA_ARGS`. Config: `qwen3.6-27b-mtp.conf`. Performance benchmark: `python3 tools/bench_mtp.py --quick`.
- **Testing**: unit tests at `tools/test_proxy_fallback.py` (`python3 tools/test_proxy_fallback.py`); end-to-end at `tools/e2e_tools_fallback.sh` (requires proxy + backend running). When modifying `anthropic_proxy.py`, run both — tool-call paths (streaming and non-streaming) are easy to break. Also test cloud mode (e.g., `./manage.sh start-cloud`).

## Tools

| Script | Purpose |
|--------|---------|
| `tools/bench_mtp.py` | MTP model performance benchmark (local + HF models, draft-n sweep) |
| `tools/test_proxy_fallback.py` | Unit tests for proxy tool-call fallback logic |
| `tools/e2e_tools_fallback.sh` | End-to-end proxy tool-call test (needs running backend) |
| `tools/logview.sh` | Unified log viewer for backend and proxy logs |
| `tools/sysmon.sh` | System monitoring (memory, CPU, disk, processes) |
| `tools/modelmon.sh` | Model service monitoring (process, download, API health) |
| `tools/memcheck.sh` | Detailed memory analysis (`vm_stat` breakdown) |

## Building llama-server from source

Brew's `llama-server` lags behind GitHub. For MTP support, build from source:

```bash
git clone https://github.com/ggml-org/llama.cpp /tmp/llama.cpp
cmake /tmp/llama.cpp -B /tmp/llama.cpp/build -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=OFF
cmake --build /tmp/llama.cpp/build --config Release -j --target llama-server
# Binary: /tmp/llama.cpp/build/bin/llama-server
```

Set `LLAMA_SERVER_BIN` env var or update `tools/bench_mtp.py`'s `LLAMA_SERVER_BIN` constant to use the built binary.

## Code style

- `manage.sh`: `set -euo pipefail`. Private helpers prefixed `_`, public commands prefixed `cmd_`. User-facing strings and comments are in **Chinese**.
- `anthropic_proxy.py`: **standard library only** — no third-party deps. Top-level constants, helpers as module-level functions, one `Handler` class. Logs to stdout *and* `/tmp/anthropic_proxy.log`.
- Config files: bash-sourcable `KEY="value"` syntax, Chinese section headers, self-contained (no includes), include the `CONFIG_NAME`/`CONFIG_DESC`/`CONFIG_MEMORY` metadata.
