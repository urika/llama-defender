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

Additional commands (see `./manage.sh help`):

```bash
./manage.sh watchdog              # Monitor backend health, auto-restart on degradation
./manage.sh fix-template <dir>    # Repair Qwen chat_template (DEF-007: prevents system message crashes)
```

## Available configurations

Configs live in `configs/*.conf` as bash-sourcable files. `configs/active.conf` is a symlink to the currently active one.

| Config | Backend | Model | Context | Memory | Use case |
|--------|---------|-------|---------|--------|----------|
| `deepseek-chat` | cloud (DeepSeek) | `deepseek-v4-flash` | (API limit) | N/A | Cloud API, no local backend |
| `rapid-mlx-35b` | rapid-mlx | Qwen3.6-35B-A3B 4bit MLX | (model max) | ~14–18 GB | Programming, 36% faster than llama-server, **concurrency=1 required** |
| `rapid-mlx-9b` | rapid-mlx | Qwen3.6-9B-A3B 4bit MLX | (model max) | ~8–10 GB | Lighter alternative for constrained memory |
| `gemma4-26b` | rapid-mlx | gemma-4-26b-it | (model max) | ~26 GB | Gemma 4 26B via rapid-mlx |

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
- **Context management defaults tied to backend type**: `PROXY_CLEAR_ENABLED`, `PROXY_TOOL_KEEP`, `PROXY_CTX_LIMIT_ENABLED`, and `PROXY_CTX_CHARS_LIMIT` all have **backend-type-aware defaults** (see `AGENTS.md` and `docs/research-context-optimization/06-context-compression-strategy.md` for the full strategy):
  - **Cloud** (DeepSeek/OpenAI): clearing **disabled** (1M+ token context), ctx-limit **disabled**
  - **Local** (llama-server/rapid-mlx): clearing **disabled by default** (rapid-mlx returns `Wasted call` for unchanged re-reads, causing death loops), ctx-limit **enabled** (limit=180K chars)
  - Override via env vars if needed, but the defaults handle the common case.
- **Concurrency caveat**: `llama-server` on Metal time-slices a single GPU; 2+ concurrent requests cause severe latency spikes. Rapid-MLX handles 2–4 concurrent requests much better. The proxy controls this via `PROXY_MAX_CONCURRENT` (default `1` for llama-server configs, `4` for Rapid-MLX configs) using a `threading.Semaphore`.
- **Rapid-MLX `max_tokens` bug** (v0.6.30): parameter is accepted but ignored — generations can run far past the limit. Use `llama-server` when token limits matter.
- **Rapid-MLX OOM on Apple Silicon** (48GB): `allocation_limit` is a soft target, not a hard wall. Prefill-phase activations + KV cache + prefix cache can overshoot by 20-40%. Known crash signature: `[METAL] Command buffer execution failed: Insufficient Memory`. Mitigation: `PROXY_MAX_CONCURRENT=1`, `--gpu-memory-utilization 0.75`, Phase 3 memory-aware guardrails (`_should_reject_for_memory`, dynamic `max_tokens`, dynamic concurrency), and avoid >40K token contexts when cache is already >6GB. `forced cache clear` at 30GB threshold does not prevent the crash.
- **Error classification and retry (DEF-001)**: `_classify_exception(e)` classifies unhandled `do_POST` exceptions as 503 (OOM/connection refused, retryable), 504 (timeout, retryable), or 500 (programming error, not retryable). Retryable errors include a `Retry-After` header (default 30s via `PROXY_RETRY_AFTER_SECONDS`) and `"retryable": true` in the JSON body, allowing well-behaved clients to back off automatically. Detection uses both exception class and message-substring matching (rapid-mlx raises generic `RuntimeError`).
- **Tool-call fallback layers**: the proxy recognises tool calls in three increasing-cost layers — structured `tool_calls` JSON (preferred; Qwen3.x, Rapid-MLX), `parse_tool_arguments` XML→JSON salvage on the args string (`<tool_call>` / `<function=…>` quirks of llama.cpp issue #21495), and `_extract_content_tool_calls` content-text fallback for `<tools>...</tools>` blocks (some Qwen models under Q4 quantisation emit these instead of populating `tool_calls`). Structured tool_calls always win when present. Gate: `PROXY_CONTENT_TOOLS_FALLBACK` (default `true`).
- **Cloud API forwarding**: In cloud mode, the proxy skips the local backend lock
  (`_llama_lock`) and forwards directly to the cloud API with the real
  `LLAMA_API_KEY`. Token counting uses `usage.prompt_tokens` / `completion_tokens`
  from the cloud response instead of `timings.*`.
- **Cloud cost**: DeepSeek `deepseek-v4-pro` costs ~¥2–8 per million tokens.
  A typical agentic coding task (56K tokens × 20 requests) costs approximately
  ¥1–3. Monitor via `REQ_SUMMARY` lines in proxy logs.
- **MTP (Multi-Token Prediction)**: Qwen3.6 supports MTP for ~1.15–1.4× faster generation. Requires MTP-specific GGUF models and a llama-server built from source with `--spec-type draft-mtp` support (Brew version lacks this). Use `--spec-type draft-mtp --spec-draft-n-max 2` in `LLAMA_EXTRA_ARGS`. Config: `qwen3.6-27b-mtp.conf`. Performance benchmark: `python3 tools/bench_mtp.py --quick`.
- **Testing**: all tests live under `test/` with three tiers — `test/unit/` (pure logic, no I/O, runs in <1s), `test/integration/` (boots a mock backend, no LLM needed, ~5s), `test/e2e/` (requires a running proxy + backend, ~30-60s). Unified runner at `test/run_tests.sh` with `--unit`/`--integration`/`--e2e`/`--all` flags. A pre-commit hook at `.githooks/pre-commit` runs `--unit` on every commit; install via `git config core.hooksPath .githooks`. Skip with `SKIP_TESTS=1 git commit …` (or `git commit --no-verify` to bypass all hooks). When modifying `anthropic_proxy.py`, run all three tiers — tool-call paths (streaming and non-streaming), blocker detection, and cloud mode (e.g., `./manage.sh start-cloud`) are all easy to break.
- **vllm-mlx startup requires `HF_HUB_OFFLINE=1`** (v0.6.71): backend tries to reach `huggingface.co` at startup; network failure causes `ConnectTimeout` retry loop with no error output. Add `export HF_HUB_OFFLINE=1` to the config file.
- **Cross-request prefix cache unavailable in BatchedEngine**: rapid-mlx v0.6.71 BatchedEngine does not integrate MemoryAwarePrefixCache. Old non-batched engine (colocated with v0.6.71 binaries) had it working at 98-99% hit rate. PagedCache (block-level) only provides within-request KV management.
- **Metal device deadlock**: after repeated `kill -9` on the backend process, Metal initialization can hang at `MLX step thread initialized`. Requires reboot to clear. Use `./manage.sh stop-backend` for graceful shutdown.
- **`--gpu-memory-utilization 0.80` recommended** for 48GB machines. Default 0.90 leaves only ~3.6GB headroom; production peaks at 87.6%. >0.85 triggers kernel panic risk documentation in AGENTS.md.

## Tools

| Script | Purpose |
|--------|---------|
| `tools/bench_mtp.py` | MTP model performance benchmark (local + HF models, draft-n sweep) |
| `tools/bench_agent.py` | Agentic workload performance benchmark (tool-call round-trip latency) |
| `tools/bench_rapidmlx.py` | Rapid-MLX specific throughput/latency benchmark |
| `tools/bench_quality.py` | Model quality evaluation (code generation, math reasoning, instruction following) |
| `tools/bench_compress.py` | Compression strategy benchmark (LLM compression vs rule-based vs static) |
| `tools/stress_test.py` | Stress test: sustained concurrent requests against the proxy |
| `tools/context_stress_test.py` | Context-stress test: escalating payload sizes to test OOM boundaries |
| `tools/cache_analyzer.py` | Prefix cache efficiency analysis (hit rate, miss patterns) |
| `tools/monitor.py` | Periodic performance monitoring + Claude semantic action analysis |
| `tools/trace_requirements.py` | Trace which requirements (R1-R7) are exercised by live traffic |
| `tools/monitor_proxy_live.sh` | Live HTTP traffic monitor for the proxy |
| `tools/analyze_claude_semantics.py` | Claude Code semantic behavior analysis from logged requests |
| `tools/analyze_experiment.py` | A/B experiment result analyzer |
| `tools/promptfoo_eval.sh` | Promptfoo-based regression test runner |
| `tools/promptfoo_report_merge.py` | Merge multiple promptfoo report JSONs |
| `tools/logview.sh` | Unified log viewer for backend and proxy logs |
| `tools/sysmon.sh` | System monitoring (memory, CPU, disk, processes) |
| `tools/modelmon.sh` | Model service monitoring (process, download, API health) |
| `tools/memcheck.sh` | Detailed memory analysis (`vm_stat` breakdown) |
| `tools/run_experiment.sh` | A/B experiment orchestration script |

## Documentation

Documents are organized under `docs/` in 6 categories (see `docs/README.md` for full index):

| Category | Subdirectory | What it contains |
|----------|-------------|------------------|
| Requirements | `01-requirements-product/` | PRD, system requirements analysis |
| Architecture | `02-architecture-design/` | Pipeline design, context window design, design reviews |
| Testing | `03-experiments-testing/` | A/B experiment guides, test strategy, benchmark methodology |
| Analysis | `04-analysis-diagnostics/` | Dead-loop analysis, cache analysis, prompt instability, message analysis |
| Operations | `05-operations-changelog/` | Optimization logs, config change records, monitoring reports |
| Metrics | `06-reference-metrics/` | KPI definitions, structured summary evaluation |

Key reference files outside `docs/`:
- `AGENTS.md` — Full reference for config variables, format conversion details, known issues, security notes
- `TROUBLESHOOTING.md` — Known issues and workarounds (chat template, tool calling, OOM diagnostics)
- `BENCHMARK.md` — Performance baseline measurements (M5 Pro 48GB)
- `CHANGELOG.md` — Release history with P0-P3 defect tracking
- `promptfooconfig.yaml` — Promptfoo regression test suite configuration

## Performance monitoring

The proxy logs structured metrics to `logs/proxy_metrics.jsonl` (one JSON line per request):
```json
{"status":200, "duration_ms":12345, "input_chars":56000, "output_chars":1200, "pipeline":{...}, "quality_flags":[...]}
```

Use `tools/monitor.py` to generate summary reports with p50/p90/p99 latency, truncation rates, blocker triggers, and quality flag distributions. Request payloads are logged to `logs/proxy_requests.jsonl` for post-hoc analysis (enabled via `PROXY_SAVE_REQUESTS`).

All automated tests live under `test/` (see `test/README.md`); the pre-commit hook
at `.githooks/pre-commit` runs the fast `--unit` tier on every commit.

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
