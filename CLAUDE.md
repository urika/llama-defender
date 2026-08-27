# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

This directory is **not** the llama.cpp C++ source — it's an orchestration layer that wraps an LLM backend and exposes an Anthropic-compatible API. Target: running Qwen-family models locally on Apple Silicon (M-series, 48 GB unified memory) **or** forwarding to cloud APIs (DeepSeek) for agentic coding with Claude Code. The agent_go project calls this service "llama-defender" (see `docs/llama-defender-integration-requirements.md`).

```
Local:  Client (Anthropic SDK) → anthropic_proxy.py:4000 → llama-server | rapid-mlx | dflash-mlx :8081 → model
Cloud:  Client (Anthropic SDK) → anthropic_proxy.py:4000 → DeepSeek/OpenAI API → cloud model
```

**Core principle**: Claude Code always connects to `http://127.0.0.1:4000`.
Backend switching is done entirely at the proxy layer — **never modify Claude Code
configuration directly** (no `~/.claude/settings.local.json` changes, no
`ANTHROPIC_BASE_URL` env vars in Claude Code).

Core pieces:

- **`manage.sh`** — Bash service manager. Sources `configs/active.conf`, applies defaults, starts local backend or cloud-only proxy.
- **`anthropic_proxy.py`** — Python 3 stdlib-only HTTP proxy (port 4000). Entry point: `Handler` class (HTTP), `main()`. ~1480 lines after Phase 0-4 refactoring; all message-processing logic now lives in `pipeline.py`.
- **`pipeline.py`** — **The heart of the system**: ~24 composable, independently testable `PipelineStage` components. `Handler._handle_messages()` builds an `InstrumentedPipeline` that runs each request through: `RequestParser → LifecycleClassifier → DynamicMaxTokens → SmartRouter → RouteNotification → ErrorTranslator → BlockerDetector → SystemNormalizer → CacheAligner → ContentCompressor → ToolLoopDetector → TextLoopDetector → SessionLoopState → LoopIntervention → RereadDetector → DateNormalizer → ContextTruncator → HighDropRatioNotice → MessageHashDebug → OOMSafetyFIFO → PrefixRatioComputer → ToolPairingRepair → FormatConverter → BackendDispatcher`. Stages are thin wrappers around the extracted modules below; deferred imports avoid circular-import issues with `proxy_state` globals.
- **`proxy_state.py`** — Single source of truth for all PROXY_* config, shared mutable state, SIGHUP reload spec (~1000 lines).
- **`proxy_config.py`** — Canonical CONFIG_REGISTRY with per-variable defaults, types, scopes, validation.
- **`backend_strategy.py`** — `LocalStrategy` / `CloudStrategy` classes (strategy pattern) encapsulating local vs cloud defaults and behavioral flags; replaced 38+ scattered `if IS_CLOUD` branches. Adding a new backend (Ollama, vLLM, …) only requires a new strategy class.

Extracted modules:

| Module | Lines | Purpose |
|--------|------|---------|
| `admin_server.py` | ~2700 | Status HTML dashboard, metrics, memory checks, concurrency, `/api/*` JSON endpoints |
| `truncation.py` | ~1680 | Context truncation: single-pass content compression (L2 clearing + L4 thinking strip), smart truncation with tool-pair atomic protection (TS-2) |
| `message_converter.py` | ~900 | Anthropic↔OpenAI bidirectional format conversion (incl. `convert_openai_request_to_anthropic` for the dual-protocol endpoint) |
| `content_compressor.py` | ~680 | TokenSieve semantic compression + BM25 relevance-driven compression (TS-1) + TS-4 结构化 drop 分支 (`_structured_compress`, BM25 低分先保结构压缩再按 `PROXY_BM25_DROP_TARGET_RATIO` 封顶) |
| `session_ledger.py` | ~430 | R14/R15 diagnostics stores: per-session action ledger (dup/last_dup_turn/materials, incremental client-history scan with prefix-diff rebuild) + sent_view archive (`logs/diag/archive/<sid>.jsonl`, MB-capped) |
| `diagnostics.py` | ~330 | R13/R16 diagnostics recorder: per-request accumulation (injections/timings probe/token facts), `X-Proxy-Diag-*` headers, SSE tail `: x-proxy-diag {...}`, `logs/diag/sessions.jsonl` per-turn records, lifecycle events |
| `tool_parser.py` | ~470 | XML→JSON fallback, content-tools extraction, streaming extractor |
| `loop_detection.py` | ~410 | Loop/blocker detection, text similarity, intervention |
| `tool_filter.py` | ~210 | Tool definition filtering, keyword extraction, error translation |
| `lifecycle.py` | ~210 | Lifecycle stage classification, dynamic token budget |
| `reload_config.py` | ~105 | SIGHUP hot-reload: re-read active.conf, update `proxy_state` + caller module |
| `compression_types.py` | ~70 | TS-3 `CompressionResult` / `CompressionSubResult` TypedDicts (JSON-serializable, Py3.8+) |
| `model_registry.py` | ~500 | Model catalog registry: loads/validates `configs/models.json` (providers/models/routes), `$env`/`$default` refs, fallback chains, hot-swap rejection, `catalog_hash`. Synthesizes a legacy-equivalent catalog when the file is absent — `MODEL_ROUTE_PREFERENCES` and `get_model_aliases()` derive from it |
| `proxy_logging.py` | ~130 | Structured logging, JSONL requests/metrics |

**`tools/extract_module.py`** — AST-based extraction tool with dual-patch test migration.

`AGENTS.md` contains the full reference (config variables, format conversion details, known issues, security notes); keep it in sync with this file when architectural changes happen.

## Service management

```bash
./manage.sh start                 # Start local backend + proxy with active.conf
./manage.sh start-cloud           # Start proxy only, forwarding to cloud API
./manage.sh start --profile aggressive  # Compression strategy: balanced (default) | aggressive | conservative
./manage.sh stop                  # Graceful, then kill -9
./manage.sh status                # PID, memory, API health, current model
./manage.sh restart               # Stop + start
./manage.sh reload                # SIGHUP hot-reload of active.conf into the running proxy (~0.5s, idempotent)
./manage.sh switch <name>         # Symlink active.conf → <name>.conf (non-interactive)
./manage.sh logs [N] / proxy-logs [N]  # Tail backend / proxy log
./manage.sh list / current        # All available configs / current config details
./manage.sh wizard                # Interactive quick-start wizard
```

Backend-only variants: `start-backend`, `stop-backend` (use graceful `stop-backend` — repeated `kill -9` can deadlock Metal; see below). Startup polls `http://host:port/v1/models` for up to 60 s to confirm readiness, then writes the PID file.

Additional commands (see `./manage.sh help`):

```bash
./manage.sh watchdog [--daemon]             # Monitor backend health, auto-restart on degradation
./manage.sh watchdog-status                 # Structured JSON status of the watchdog
./manage.sh route-force-local <session_id>  # Force session to local (sensitive code, privacy)
./manage.sh route-force-cloud <session_id>  # Force session to cloud (override throttle)
./manage.sh models                          # Model catalog overview (providers/models/routes, key readiness, hash)
./manage.sh models-validate                 # Validate configs/models.json (non-zero exit on bad file)
./manage.sh monitor [N]               # Metal memory live monitor (refresh every N sec, default 5)
./manage.sh fix-template <dir>        # Repair Qwen chat_template (DEF-007: prevents system message crashes)
```

## Available configurations

Configs live in `configs/*.conf` as bash-sourcable files. `configs/active.conf` is a symlink to the currently active one; retired configs live in `configs/archived/`; `configs/secret.local.conf` holds real API keys (gitignored, sourced by cloud configs).

| Config | Backend | Model | Memory | Use case |
|--------|---------|-------|--------|----------|
| `ornith-oq4e` (active) | rapid-mlx | pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp | ~16–20 GB | **Ornith-1.5 自改进 MoE，oQ4e imatrix 混合精度（20.1GB）**，质量优于均匀 4-bit，thinking off，decode ~80 tok/s，`--hybrid-cache-entries 8` 必配（无投机解码） |
| `ornith-35b` | rapid-mlx | ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit | ~16–20 GB | 均匀 4-bit（19.5GB），decode ~87 tok/s 更快，质量略降 |
| `ornith-dflash-35b` | dflash-mlx | ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit + z-lab/Qwen3.6-35B-A3B-DFlash | ~20 GB | Ornith + Qwen3.6 DFlash 头，decode ~91 tok/s（acceptance 63.3%），与纯 rapid-mlx 持平 |
| `dflash-35b` | dflash-mlx | mlx-community/Qwen3.6-35B-A3B-4bit + z-lab/Qwen3.6-35B-A3B-DFlash | ~20 GB | **DFlash 投机解码（~117 tok/s）**，thinking off，prefix cache 有效 |
| `rapid-mlx-35b-opt` | rapid-mlx | mlx-community/Qwen3.6-35B-A3B-4bit | ~14–18 GB | 35B MoE 标准 4-bit，GPU=70%（必须），prefix cache + KV q4 on，decode ~51 tok/s |
| `qwen3.8-27b-4bit` | rapid-mlx | Qwen3.8-27B 4bit | ~15–18 GB | Qwen3.8 hybrid，`--hybrid-cache-entries 8` + gpu-mem 0.80 |
| `qwen3.6-27b-4bit` | rapid-mlx | Qwen3.6-27B dense 4bit | ~13–16 GB | Dense alternative，tool clearing off |
| `gemma4-26b` | rapid-mlx | gemma-4-26b-it | ~14–16 GB | Gemma 4 26B, concurrency=2, temp 0.2 |
| `deepseek-chat` | cloud (DeepSeek) | `deepseek-v4-flash` | N/A | Cloud API, no local backend |

Each config sets `LLAMA_*` env vars (backend, model, port, context, sampling, KV-cache type, thinking mode) plus backend-specific vars: `RAPID_MLX_*` (rapid-mlx: tool/reasoning parsers, prefix cache, KV quantization, `--hybrid-cache-entries 8` for hybrid models) or `DFLASH_*` (dflash-mlx: `DFLASH_DRAFT_MODEL`, `DFLASH_ENABLE_THINKING=false`, `DFLASH_EXTRA_ARGS`). Metadata fields (`CONFIG_NAME`, `CONFIG_DESC`, `CONFIG_MEMORY`) are read by `./manage.sh list`. Defaults for any unset variable are applied in `manage.sh` itself.

**Model catalog** (`configs/models.json`, loaded by `model_registry.py`, hot-reloaded on SIGHUP): declarative providers (endpoints + `key_env` + concurrency, keys live in `secret.local.conf`), models (price/capabilities/quirks — glm-5.x, k3, deepseek-v4-*), and routes (alias → cloud-model binding with optional fallback chains). Deleting the file synthesizes a legacy-equivalent catalog — behavior identical to the pre-catalog hardcode. Route preferences rebuild on reload, so `$env` refs track the live `PROXY_CLOUD_MODEL`.

> **⚠️ rapid-mlx OOM 防范** (48GB unified memory):
> - `PROXY_MAX_CONCURRENT=1`（gemma4 配置为 2）— 两个 38K+ token 请求并发必然 OOM
> - `--gpu-memory-utilization 0.70` + `--max-num-seqs 1` + `--cache-memory-mb 4096` — rapid-mlx 的 `allocation_limit` 是**软限制**，实际使用会超出 20-40%。降低 limit 让引擎更早节流
> - 前缀缓存堆积后，再叠加一个大请求 prefill 极易触发 `[METAL] Insufficient Memory`；Phase 3 memory-aware guardrails（`_should_reject_for_memory`、dynamic `max_tokens`、dynamic concurrency）是必须的
> - 实测：并发=1 + GPU=70% 后，单 38K 请求稳定运行（TTFT ~28s），无 OOM

## Proxy

```bash
python3 anthropic_proxy.py                                            # listens on 127.0.0.1:4000
LLAMA_BASE_URL=http://127.0.0.1:8081/v1 PORT=4000 python3 anthropic_proxy.py
```

**Dual-protocol endpoints**: `POST /v1/messages` (Anthropic format, primary) **and** `POST /v1/chat/completions` (OpenAI format, converted via `convert_openai_request_to_anthropic` then passed through). Also `GET /v1/models`, `OPTIONS`. Stateless, no third-party deps.

**Structured admin APIs** (for `agent_go` / external orchestrators):

| Method | Path | Purpose | Response |
|--------|------|---------|----------|
| GET | `/api/status` | Structured service health & readiness | JSON: `proxy`, `backend`, `active_profile`, `state`, `ready`, `route_config` |
| GET | `/api/route/policies` | Sanitized routing policies + model catalog (R9) | JSON: `providers` (keys as `key_set` bool only), `models`, `preferences`, `defaults`, `catalog_hash`, `api_version` |
| GET | `/api/watchdog` | Watchdog state | JSON: `enabled`, `running`, `pid`, `last_restart_at`, `restart_count_1h`, `last_failure_reason` |
| GET | `/api/queue` | Priority request queue state (Phase 1, default off) | JSON: `enabled`, `workers`, `waiting`, `by_bucket`, `oldest_wait_ms` |
| GET | `/api/profiles` | Available model configs | JSON: `profiles[]` with `name`, `desc`, `memory_gb`, `active` |
| GET | `/metrics[?n=N]` | Recent request metrics | JSON |
| GET | `/metrics/history[?session=K]` | Historical metrics (R16: optional per-session filter) | JSON |
| GET | `/api/sessions` | Active diagnostics sessions (R14 discovery) | JSON: `sessions[]` with `key`/`key_source`/`turns`/`last_seen` |
| GET | `/api/session/<key>/ledger` | Session action ledger (R14) | JSON: `actions[]` (dup/last_dup_turn), `dup_queries[]`, `materials[]`; 404 unknown / 410 evicted |
| GET | `/api/session/<key>/archive?view=sent` | Per-turn sent_view archive (R15) | JSON: turn index (payload only with `include_payload=true`); 501 for `canonical` until Phase 1 |
| GET | `/api/session/<key>/metrics` | Per-session diag aggregates (R16) | JSON: `hit_ratio_p50/p90`, `latency_by_kind` (epoch/normal split), `series[]` |
| GET | `/api/backend/props` / `/api/backend/slots` | llama-server native endpoints, read-only reverse proxy | JSON or 501 `{"supported": false}` when backend lacks them |
| POST | `/admin/route/force-local` / `force-cloud` | Session-level route override | JSON |
| POST | `/admin/reload` | HTTP hot-reload, equivalent to `manage.sh reload` (R12) | JSON: `reloaded`, `active_profile`, `api_version` |
| GET | `/status` | Human-readable HTML status page | HTML |

- `/api/status` returns `200` when `state` is `healthy` or `starting`, otherwise `503` with the same JSON body. `state` enum: `healthy | starting | backend_down | proxy_down | model_drift | down`.
- `ready` means the backend model is loaded and can accept inference requests (`starting` → `ready=false`). `agent_go`'s `wait_ready` polls this field.
- Watchdog auto-restart events are logged to `logs/watchdog_state.json`; lifecycle events (`service_start`, `config_reload`, `profile_switch`, `watchdog_auto_restart`, …) to `logs/lifecycle_events.jsonl`.

**llama-defender integration** (`docs/llama-defender-integration-requirements.md`): R1-R12 **all delivered**. R1-R7: structured status, readiness semantics, manage.sh call contract, profiles/watchdog APIs. R8: `X-Proxy-Route-Target/Actual-Model/Reason/Cost` attribution headers + OpenAI-mode `proxy_route` body field. R9: `GET /api/route/policies` (sanitized catalog + `catalog_hash` for agent_go drift detection). R10: `/v1/models` capability metadata (`real_model`/`thinking_*`/`json_compliance`/`context_chars`/`price`/`direct_capable`). R11: `/api/status` `route_config` digest. R12: `POST /admin/reload`.

**Diagnostics data plane** (R13-R16, `docs/02-architecture-design/diagnostics-dataplane-design-20260819.md`, 2026-08-19 delivered): R13 diag attribution — non-streaming HTTP headers `X-Proxy-Diag-Request-Id` / `X-Proxy-Feedback-Injected` (csv) / `X-Proxy-Prompt-Processed-N` (only when backend returns `timings`), streaming via SSE comment tail `: x-proxy-diag {...}` before `message_stop` / `[DONE]`; all 7 existing synthetic-content injections instrumented (`loop_l1/l2/l3`, `text_loop`, `blocker`, `reread_hard`, `route_notice`, `high_drop_notice`, `truncation_summary`, `session_loop_warning`). R14 session ledger (`/api/session/<key>/ledger` + `/api/sessions`). R15 sent_view archive (per-turn final backend payload — the authoritative "what the model actually saw"). R16 per-turn `logs/diag/sessions.jsonl` (request_id-correlated with `proxy_metrics.jsonl`; `hit_ratio = 1 − prompt_n/prompt_tokens`; `is_epoch_turn` reserved null until context-engineering Phase 1) + `/api/status` `ctx_config` digest + `lifecycle_events.jsonl` activated (`canonical_mismatch`). Master switch `PROXY_DIAG_ENABLED` (SIGHUP-reloadable family `PROXY_DIAG_*`).

**Dual-mode auto-detection**: `BACKEND_TYPE` is automatically inferred from `LLAMA_BASE_URL`: contains `deepseek` / `openai` / `api.` → `cloud`, otherwise → `local`. `MODEL_NAME` auto-set accordingly; manual override via env var is rarely needed.

| Mode | `LLAMA_BASE_URL` | `MODEL_NAME` (auto) | `LLAMA_API_KEY` | `PROXY_MAX_CONCURRENT` |
|------|------------------|---------------------|-----------------|------------------------|
| Local | `http://127.0.0.1:8081/v1` | `mlx-community/Qwen3.6-35B-A3B-4bit` | Dummy (`sk-1234`) | `1` |
| Cloud (DeepSeek) | `https://api.deepseek.com/v1` | `deepseek-v4-pro` (auto default; `deepseek-chat.conf` sets `deepseek-v4-flash`) | **Real key** | `4` |

DeepSeek model mapping: `deepseek-v4-pro[1m]` ↔ `deepseek-v4-pro` (thinking), `deepseek-v4-flash` ↔ `deepseek-v4-flash`. Legacy `deepseek-chat`/`deepseek-reasoner` names were deprecated 2026-07-24.

**Intelligent Model Routing** (SmartRouter stage): auto-routes requests between local and cloud backends based on context size, memory pressure, and session state.
- **Enable**: `PROXY_ROUTE_ENABLED=true` (default `false`, backward-compatible)
- **Threshold**: `PROXY_ROUTE_THRESHOLD_CHARS=90000` — requests above this route to cloud
- **Cloud model**: `PROXY_CLOUD_MODEL=deepseek-v4-flash` (default), `deepseek-v4-pro` for quality
- **API Key**: `PROXY_CLOUD_API_KEY` must be set in `secret.local.conf` for cloud routing
- **Fallback**: `PROXY_ROUTE_FALLBACK_ENABLED=true` — cloud failure walks the catalog `fallback_chain` (cross-provider, skipping providers in cooldown or without a key) → emergency truncation → local retry; per-provider circuit breakers (fail counter + cooldown) are independent per provider
- **Protocol dispatch (Phase D)**: a provider may declare `"protocol": "anthropic"` — its models dispatch to `{anthropic_base_url}/v1/messages` (Anthropic protocol, e.g. the Z.ai Coding Plan subscription) with SSE passthrough; OpenAI-protocol clients (`/v1/chat/completions`) skip anthropic candidates and fall through the chain
- **Daily budget**: `PROXY_ROUTE_DAILY_BUDGET=5.0` caps global daily cloud cost (0 = unlimited); catalog `defaults.per_provider_budget` adds per-provider caps. Costs use per-model catalog pricing (models without a price fall back to `PROXY_CLOUD_PRICE_*`)
- **Session control**: `./manage.sh route-force-local <sid>` / `route-force-cloud <sid>`; **per-request override**: `X-Proxy-Route-To: local|cloud` request header (no session stickiness)
- **Response headers (R8 contract)**: `X-Proxy-Route-Target` (`cloud|local|local_forced`), `X-Proxy-Route-Actual-Model`, `X-Proxy-Route-Reason`, `X-Proxy-Route-Cost` on every routed response; OpenAI-protocol non-streaming responses also carry a `proxy_route` body field with actual usage-based cost

Model ID → route preference mapping (preference only, safety always overrides):
| Agent Model ID | Route Bias | Threshold | Cloud Model |
|---------------|-----------|-----------|-------------|
| `claude-sonnet-4-6` | auto | 90K | flash |
| `claude-opus-4-7` | prefer_cloud | 72K | pro |
| `claude-haiku-4-5` | prefer_local | 120K | flash |

## Key implementation details

- **Model loading**: `LLAMA_MODEL` starting with `/` or `./` is treated as a local path (`-m`); otherwise it's a HuggingFace ID (`-hf`). Local model artifacts live in `models/` (GGUF + MLX).
- **Thinking mode**: `LLAMA_THINKING=false|true` → `--chat-template-kwargs '{"enable_thinking":...}'`. Empty string skips the flag (use for models that don't support thinking, e.g., Qwen2.5).
- **KV cache**: Default `q8_0` for both K and V on llama-server; rapid-mlx configs use `RAPID_MLX_KV_QUANTIZATION=true` with 4 bits.
- **Context management defaults tied to backend type** (`backend_strategy.py` is the source of these defaults): cloud = clearing disabled (1M+ context), ctx-limit disabled; local = ctx-limit enabled (180K chars), clearing configurable per config (`rapid-mlx-35b-opt` enables clearing, `qwen3.6-27b-4bit` disables it — rapid-mlx returns `Wasted call` for unchanged re-reads, which interacts with clearing to cause file-re-read death loops). See `docs/research-context-optimization/06-context-compression-strategy.md` for the full strategy.
- **Concurrency caveat**: `llama-server` on Metal time-slices a single GPU; 2+ concurrent requests cause severe latency spikes. The proxy controls this via `PROXY_MAX_CONCURRENT` (default `1` local, `4` cloud/rapid-mlx) using a `threading.Semaphore`, plus `PROXY_DYNAMIC_CONCURRENT_*` guardrails.
- **Rapid-MLX `max_tokens` bug** (observed on v0.6.30): parameter is accepted but ignored — generations can run far past the limit. `DynamicMaxTokens` + `PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO=1.0` mitigate; use `llama-server` when hard token limits matter. Since 2026-08-27 the budget is context-aware: saturation tier uses `PROXY_DYNAMIC_MAX_TOKENS_SATURATION` (default 2048, ornith 8192), oom_danger/pre_trunc use `PROXY_DYNAMIC_MAX_TOKENS_OOM` (default 4096). `PROXY_MAX_TOKENS_OVERRIDE` is a final *ceiling* only (never raises a tighter dynamic cap). Timeout hardening (same date): non-streaming requests proactively return 504 at `client_timeout − PROXY_TIMEOUT_MARGIN_S` (30) so the error reaches the client before it disconnects; streaming uses an inter-chunk idle watchdog `PROXY_STREAM_IDLE_TIMEOUT_S` (30s after first token) that aborts the relay and cancels in-flight generation. See `docs/02-architecture-design/agent-output-budget-design-20260827.md`.
- **Rapid-MLX OOM on Apple Silicon** (48GB): `allocation_limit` is a soft target, not a hard wall — prefill activations + KV cache + prefix cache can overshoot 20-40%. Crash signature: `[METAL] Command buffer execution failed: Insufficient Memory`. Mitigation: `PROXY_MAX_CONCURRENT=1`, `--gpu-memory-utilization 0.70`, Phase 3 memory-aware guardrails (`_should_reject_for_memory`, dynamic `max_tokens`, dynamic concurrency). Avoid >40K token contexts when cache is already >6GB.
- **Prefix cache**: re-enabled on rapid-mlx 0.11.5 (`RAPID_MLX_ENABLE_PREFIX_CACHE=true` in active config). Older 0.6.71 BatchedEngine lacked cross-request prefix cache (PagedCache was within-request only).
- **HF_HUB_OFFLINE=1**: vllm-mlx tries to reach huggingface.co at startup; network failure causes a silent `ConnectTimeout` retry loop. Add `export HF_HUB_OFFLINE=1` to configs where relevant.
- **Error classification and retry (DEF-001)**: `_classify_exception(e)` classifies unhandled `do_POST` exceptions as 503 (OOM/connection refused, retryable), 504 (timeout, retryable), or 500 (programming error, not retryable). Retryable errors include a `Retry-After` header (default 30s via `PROXY_RETRY_AFTER_SECONDS`) and `"retryable": true` in the JSON body. Rapid-mlx raises generic `RuntimeError`, so detection also matches message substrings.
- **Tool-call fallback layers**: three increasing-cost layers — structured `tool_calls` JSON (preferred; Qwen3.x), `parse_tool_arguments` XML→JSON salvage on the args string (`<tool_call>` / `<function=…>` quirks of llama.cpp issue #21495), and `_extract_content_tool_calls` content-text fallback for `<tools>...</tools>` blocks. Structured tool_calls always win when present. Gate: `PROXY_CONTENT_TOOLS_FALLBACK` (default `true`).
- **Cloud API forwarding**: in cloud mode the proxy skips the local backend lock and forwards directly with the real `LLAMA_API_KEY`. Token counting uses `usage.prompt_tokens` / `completion_tokens` from the cloud response instead of `timings.*`.
- **Cloud cost**: DeepSeek `deepseek-v4-pro` costs ~¥2–8 per million tokens; a typical agentic coding task costs ~¥1–3. Monitor via `REQ_SUMMARY` lines in proxy logs.
- **MTP (Multi-Token Prediction)**: Qwen3.6 supports MTP for ~1.15–1.4× faster generation. Requires MTP-specific GGUF models and a llama-server built with `--spec-type draft-mtp` support (Brew version lacks this). Benchmark: `python3 tools/bench_mtp.py --quick`.
- **Metal device deadlock**: after repeated `kill -9` on the backend process, Metal initialization can hang at `MLX step thread initialized`. Requires reboot to clear. Use `./manage.sh stop-backend` for graceful shutdown.
- **Testing**: all tests live under `test/` — `test/unit/` (pure logic, no I/O, <1s; 24 files, ~900 tests), `test/integration/` (boots a mock backend, no LLM, ~60s; 9 suites), `test/e2e/` (requires a running proxy + backend). Unified runner `test/run_tests.sh` with `--unit`/`--integration`/`--e2e`/`--all`/`--fast` flags. Single file: `python3 -m unittest discover -s test/unit -p 'test_tool_parser.py' -v`. Pre-commit hook at `.githooks/pre-commit` runs `--unit` on every commit (`git config core.hooksPath .githooks`); skip with `SKIP_TESTS=1 git commit …`. When modifying `anthropic_proxy.py` or `pipeline.py`, run all three tiers — tool-call paths (streaming and non-streaming), blocker detection, and cloud mode are all easy to break.

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
| `tools/trace_requirements.py` | Trace which requirements (R1-R12) are exercised by live traffic |
| `tools/monitor_proxy_live.sh` | Live HTTP traffic monitor for the proxy |
| `tools/analyze_claude_semantics.py` | Claude Code semantic behavior analysis from logged requests |
| `tools/analyze_experiment.py` | A/B experiment result analyzer |
| `tools/promptfoo_eval.sh` | Promptfoo-based regression test runner |
| `tools/logview.sh` | Unified log viewer for backend and proxy logs |
| `tools/sysmon.sh` | System monitoring (memory, CPU, disk, processes) |
| `tools/modelmon.sh` | Model service monitoring (process, download, API health) |
| `tools/memcheck.sh` | Detailed memory analysis (`vm_stat` breakdown) |
| `tools/run_experiment.sh` | A/B experiment orchestration script |

## Documentation

Documents are organized under `docs/` in 7 categories (see `docs/README.md` for full index):

| Category | Subdirectory | What it contains |
|----------|-------------|------------------|
| Requirements | `01-requirements-product/` | PRD, system requirements analysis |
| Architecture | `02-architecture-design/` | Pipeline design (`proxy-pipeline-reference.md`), context window design, design reviews |
| Testing | `03-experiments-testing/` | A/B experiment guides, test strategy, benchmark methodology |
| Analysis | `04-analysis-diagnostics/` | Dead-loop analysis, cache analysis, prompt instability, message analysis |
| Operations | `05-operations-changelog/` | Optimization logs, config change records, monitoring reports |
| Metrics | `06-reference-metrics/` | KPI definitions, structured summary evaluation |
| Project board | `07-project-board/` | Task tracking boards |

Key reference files outside `docs/`:
- `AGENTS.md` — Full reference for config variables, format conversion details, known issues, security notes
- `docs/llama-defender-integration-requirements.md` — agent_go integration contract (R1-R7 delivered, R8-R12 pending)
- `TROUBLESHOOTING.md` — Known issues and workarounds (chat template, tool calling, OOM diagnostics)
- `BENCHMARK.md` — Performance baseline measurements (M5 Pro 48GB)
- `CHANGELOG.md` — Release history with P0-P3 defect tracking
- `docs/DEFECT-LIST.md` — Defect registry (DEF-001…)
- `promptfooconfig.yaml` — Promptfoo regression test suite configuration

## Performance monitoring

The proxy logs structured metrics to `logs/proxy_metrics.jsonl` (one JSON line per request):
```json
{"status":200, "duration_ms":12345, "input_chars":56000, "output_chars":1200, "pipeline":{...}, "quality_flags":[...]}
```

Use `tools/monitor.py` to generate summary reports with p50/p90/p99 latency, truncation rates, blocker triggers, and quality flag distributions. Request payloads are logged to `logs/proxy_requests.jsonl` for post-hoc analysis (enabled via `PROXY_SAVE_REQUESTS`).

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
- Python modules: **standard library only** — no third-party deps. `proxy_state.py` is the single source of truth for config constants and shared state (imported via `from proxy_state import *`). `anthropic_proxy.py` holds the `Handler` class and `main()`; request processing lives in `pipeline.py` stages. Logs to stdout *and* `/tmp/anthropic_proxy.log`.
- Config files: bash-sourcable `KEY="value"` syntax, Chinese section headers, self-contained (no includes), include the `CONFIG_NAME`/`CONFIG_DESC`/`CONFIG_MEMORY` metadata.
- New pipeline stages: subclass `PipelineStage`, keep them thin wrappers over the extracted modules, and add a corresponding `test/unit/test_pipeline_stages.py` case.
