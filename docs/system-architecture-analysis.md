# System Architecture Analysis

> Generated: 2026-06-23
> Based on: /Users/jinsongwang/APP/llama.cpp

---

## 1. System Overview

This project is a **local LLM inference orchestration layer** that wraps external backend binaries (llama-server / rapid-mlx / vllm-mlx) and exposes an Anthropic-compatible API via a Python proxy. Primary use case: running Qwen series models on Apple Silicon (MacBook Pro M5 Pro, 48GB unified memory) for Claude Code / OpenCode AI coding workflows.

### Core Data Flow

```
Client (Anthropic SDK / Claude Code / OpenCode)
  |  Always connects to 127.0.0.1:4000
  |  POST /v1/messages
  v
+-----------------------------------------------------+
| anthropic_proxy.py (Python stdlib only)              |
|  +-- Handler (HTTP/1.1 ThreadingHTTPServer)          |
|  +-- 22-stage Pipeline (pipeline.py)                 |
|  |    RequestParser -> LifecycleClassifier           |
|  |    -> DynamicMaxTokens -> SmartRouter             |
|  |    -> RouteNotification -> ErrorTranslator        |
|  |    -> BlockerDetector -> SystemNormalizer         |
|  |    -> CacheAligner -> ContentCompressor           |
|  |    -> ToolLoopDetector -> TextLoopDetector        |
|  |    -> SessionLoopState -> LoopIntervention        |
|  |    -> RereadDetector -> DateNormalizer            |
|  |    -> ContextTruncator -> HighDropRatio           |
|  |    -> MessageHashDebug -> OOMSafetyFIFO           |
|  |    -> PrefixRatioComputer -> ToolPairingRepair    |
|  |    -> FormatConverter -> BackendDispatcher        |
|  +-- Config: proxy_state.py + proxy_config.py       |
|  Forward (OpenAI format)                             |
|  Backend (OpenAI-compatible API)                     |
|    +-- Local: llama-server/rapid-mlx/vllm-mlx        |
|    |         Running on :8081                         |
|    +-- Cloud: DeepSeek API / OpenAI API              |
|              Forwarded to external URL               |
+-----------------------------------------------------+
```

### Dual-mode Design

| Aspect | Local Mode | Cloud Mode |
|--------|-----------|------------|
| Detection | LLAMA_BASE_URL lacks cloud keywords | URL contains deepseek/openai/api. |
| Backend process | llama-server/rapid-mlx on :8081 | None (uses external API) |
| API Key | Dummy token (sk-1234) | Real key (secret.local.conf) |
| Concurrency default | 1 (prevents 48GB OOM) | 4 (cloud handles concurrency) |
| Context limit | Enabled (180K chars) | Disabled (1M+ token context) |

---

## 2. Module Dependency & Responsibilities

### Dependency Graph

```
proxy_state.py (933L)  -- Single source of truth
   All PROXY_*/LLAMA_* config constants + shared state
   _SESSION_COUNT, _DEDUP_CACHE, _LATENCY_WINDOW
      ^
      | from proxy_state import *
      |
proxy_config.py (798L) -- CONFIG_REGISTRY
   Config metadata, validation, drift detection
      ^
      |
pipeline.py (2225L)    -- 22-stage PipelineStage engine
      ^
      |
reload_config.py (105L) -- SIGHUP hot-reload (dual-setattr)
      ^
      |
      v
anthropic_proxy.py (1318L)
   HTTP Handler + pipeline composition + response handling
      ^
  | Referenced sub-modules:
  +-- message_converter.py (706L) -- format conversion
  +-- tool_parser.py (440L)       -- tool arg parsing
  +-- truncation.py (1224L)       -- context truncation
  +-- content_compressor.py (321L) -- semantic compression
  +-- loop_detection.py (404L)    -- loop detection
  +-- tool_filter.py (191L)       -- tool filtering
  +-- lifecycle.py (209L)         -- lifecycle stages
  +-- backend_strategy.py (134L)  -- strategy pattern
  +-- proxy_logging.py (114L)     -- structured logging
  +-- admin_server.py (2098L)     -- monitoring page
```

### Module Line Counts

| Module | Lines | Responsibility |
|--------|-------|----------------|
| pipeline.py | 2225 | 22-stage pipeline engine |
| admin_server.py | 2098 | /status page, session analysis, monitoring |
| anthropic_proxy.py | 1318 | HTTP Handler, pipeline assembly, response |
| truncation.py | 1224 | Context truncation (4 strategies) |
| proxy_state.py | 933 | Config constants + shared state |
| proxy_config.py | 798 | Config registry, validation, drift detection |
| message_converter.py | 706 | Anthropic <-> OpenAI format conversion |
| tool_parser.py | 440 | XML <-> JSON tool arg parsing |
| loop_detection.py | 404 | 3-level loop detection + intervention |
| content_compressor.py | 321 | Semantic compression |
| lifecycle.py | 209 | Session lifecycle stage classification |
| tool_filter.py | 191 | Dynamic tool definition filtering |
| backend_strategy.py | 134 | Strategy pattern (Local/Cloud) |
| proxy_logging.py | 114 | Structured JSONL logging |
| reload_config.py | 105 | SIGHUP hot-reload handler |

### Module Details

#### proxy_state.py (933 lines) -- Single Source of Truth
- All PROXY_* and LLAMA_* config constants with defaults
- Shared mutable state: _SESSION_REQUEST_COUNT, _DEDUP_CACHE
  _LATENCY_WINDOW, _METRICS_BUFFER, _SESSION_ROUTE_MAP
- Thread-local contexts: _log_ctx, _metrics_ctx
- Helpers: get_model_aliases(), _parse_conf_env(), _cast_config_value()
- __all__ whitelist for 'from proxy_state import *'

#### pipeline.py (2225 lines) -- Pipeline Engine
- PipelineContext: request body, session ID, route decision, stage outputs
- PipelineStage (ABC): process(ctx) interface
- ConditionalStage: conditionally activated stage
- Pipeline: sequential stage execution
- InstrumentedPipeline: Pipeline with timing/metrics instrumentation
- 22 concrete Stage classes (see Section 4)

#### anthropic_proxy.py (1318 lines) -- HTTP Handler
- class Handler(BaseHTTPRequestHandler) with ThreadingHTTPServer
- do_POST(): entry -> size check -> dedup -> route -> pipeline dispatch
- do_GET(): /v1/models, /status, /metrics, /session
- _handle_messages(): assemble Pipeline + 22 stages -> ctx.run()
- _handle_non_streaming_response(): full response -> OpenAI->Anthropic
- _handle_streaming_response(): SSE stream conversion
- _handle_openai_streaming_response(): OpenAI format passthrough

#### admin_server.py (2098 lines) -- Admin Interface
- _build_status_html(): HTML status page (PID, memory, cache, routing)
- Session analysis engine: /session endpoint
- Metrics aggregation from proxy_metrics.jsonl
- Error classification and display

---

## 3. HTTP Endpoints

| Method | Path | Function | Notes |
|--------|------|----------|-------|
| GET | /v1/models | Model alias list + route metadata | Cloud/Local/Auto flags |
| GET | /status | HTML status page | Real-time metrics, memory, alerts |
| GET | /metrics | JSON metrics summary | Reads proxy_metrics.jsonl |
| GET | /session | Session analysis | JSON or HTML |
| POST | /v1/messages | Anthropic Messages API | Core: 22-stage pipeline |
| POST | /v1/chat/completions | OpenAI Chat | Converted before pipeline |
| POST | /admin/route/force-local | Force session to local | Runtime override |
| POST | /admin/route/force-cloud | Force session to cloud | Runtime override |
| OPTIONS | * | CORS preflight | Returns 200 |

---

## 4. 22-Stage Pipeline Detail

All stages defined in pipeline.py, assembled in anthropic_proxy.py:731.

### Stage 0: RequestParser
- Parse request body JSON
- Extract model/stream/tools/session_id/chars fields
- Handle _x_proxy_route_to header override (force routing)
- Set PipelineContext immutable input fields

### Stage 1: LifecycleClassifier
- Call lifecycle._classify_lifecycle_stage()
- Classify session lifecycle by total char count
- Stages: init -> growth -> expansion -> saturation -> oom_danger
- Thresholds: PROXY_CHARS_GROWTH / EXPANSION / SATURATION / OOM_DANGER

### Stage 2: DynamicMaxTokens (ConditionalStage)
- Adjust max_tokens by lifecycle stage + memory pressure
- Condition: only active when PROXY_DYNAMIC_MAX_TOKENS_ENABLED=true
- Ceiling by stage: init=4096, growth/expansion=4096, saturation=2048
- rapid-mlx additional 0.8x multiplier

### Stage 2.5: SmartRouter
- Core routing decision engine, 10-level priority cascade
- Priority levels (0=lowest, 9=highest):
  0: Disabled (skip when only one backend available)
  0.5: Model preference threshold adjustment
  0.55: Model force bias
  0.6: _x_proxy_route_to header override
  1-9: Cloud cooldown -> session route state
       -> daily budget -> memory pressure
       -> context size -> lifecycle stage
       -> default local
- Output: _route_target (local/cloud), _route_reason

### Stage 2.6: RouteNotification
- Log route decision to context and metrics
- For debugging and observability

### Stage 3: ErrorTranslator
- Translate known backend error patterns to Chinese hints
- Known: Wasted call, File does not exist, InputValidationError
- Returns friendly errors with solution suggestions

### Stage 4: BlockerDetector
- Same tool + same error exceeding PROXY_BLOCKER_THRESHOLD
- Inject [BLOCKER] user message to message tail
- Condition: PROXY_BLOCKER_ENABLED=true (local=true, cloud=false)
- Threshold: PROXY_BLOCKER_THRESHOLD=2

### Stage 5: SystemNormalizer
- Merge multiple system messages, handle format differences

### Stage 6: CacheAligner
- Protect first PROXY_CACHE_ALIGN_HEAD (default 4) messages
  from compression/truncation
- Purpose: stabilize prefix cache for KV cache reuse
- Condition: PROXY_CACHE_ALIGN_ENABLED=true (local=true, cloud=false)

### Stage 7: ContentCompressor
- Semantic compression of long tool_result contents
- Content type detection: json/code/log/text
- Compression modes: lossless/semantic/aggressive
- Audit validates output, fallback to original on failure

### Stage 8: ToolLoopDetector
- Consecutive identical tool calls (same tool + same arg hash)
- Threshold: PROXY_LOOP_THRESHOLD=3

### Stage 9: TextLoopDetector
- Consecutive similar text output (cosine similarity)
- Threshold: PROXY_TEXT_LOOP_SIMILARITY=0.85
- Min chars: PROXY_TEXT_LOOP_MIN_CHARS=100

### Stage 10: SessionLoopState
- Track session-level loop state across requests
- Cumulative count, intervention level, history patterns

### Stage 11: LoopIntervention
- 3-level intervention:
  - Level 1: Inject soft hint in user message tail
  - Level 2: Remove looping tool from tools list
  - Level 3: Force plain-text mode (no tools) for one turn

### Stage 12: RereadDetector
- Detect repeated file read patterns
- Prevent Read -> Wasted call death loop

### Stage 13: DateNormalizer
- Normalize date references in messages
- Relative dates (yesterday/today/tomorrow) -> specific dates

### Stage 14: ContextTruncator
- Context truncation entry, dispatch by PROXY_CTX_TRUNCATE_STRATEGY
- rounds: keep head + N rounds + smart Read content preservation
- fifo: fixed message count, first-in-first-out
- char: threshold-based on PROXY_CTX_CHARS_LIMIT
- smart: role + content type aware

### Stage 15: HighDropRatioNotice
- When truncation drops >85% of messages
- Inject [System: Context severely truncated] notice

### Stage 16: MessageHashDebug
- Debug message hash computation
- For tracking message changes in logs

### Stage 17: OOMSafetyFIFO
- OOM safety FIFO truncation before backend send
- PROXY_OOM_SAFE_CHARS (default 200K) ceiling

### Stage 18: PrefixRatioComputer
- Compute prefix cache stability ratio
- For monitoring and cache optimization

### Stage 19: ToolPairingRepair
- Fix orphaned tool_use/tool_result pairs after truncation
- Prevent backend 400 errors from broken pairs

### Stage 20: FormatConverter
- Anthropic -> OpenAI format conversion
- Calls message_converter.convert_anthropic_messages_to_openai()
- Handles tool definition conversion, message role mapping

### Stage 21: BackendDispatcher (394 lines)
- Final stage: send POST to backend, handle streaming/non-streaming
- _do_dispatch(): urllib POST to LLAMA_BASE_URL + /chat/completions
- _record_cloud_failure(): cloud cooldown management
- _emergency_truncate(): keep last 3 rounds on emergency
- _accumulate_daily_cost(): cloud API cost tracking
- Calls handler._handle_streaming_response() or _handle_non_streaming_response()

---

## 5. Configuration System

### Priority Chain (high to low)

active.conf -> secret.local.conf -> manage.sh :${} defaults -> proxy_state.py DEFAULTS

active.conf is a symlink; manage.sh switch <name> atomically updates it.
secret.local.conf holds LLAMA_API_KEY (git-ignored).

### Config Categories

| Category | Key Variable | Local Default | Cloud Default |
|----------|-------------|--------------|--------------|
| Backend routing | LLAMA_BASE_URL | :8081/v1 | deepseek/v1 |
| Backend routing | LLAMA_API_KEY | sk-1234 | (real key) |
| Backend routing | BACKEND_TYPE | auto(local) | auto(cloud) |
| Backend routing | PROXY_MAX_CONCURRENT | 1 | 4 |
| Tool clearing | PROXY_CLEAR_ENABLED | false | false |
| Cache aligner | PROXY_CACHE_ALIGN_ENABLED | true | false |
| Compression | PROXY_COMPRESS_ENABLED | true | false |
| Context limit | PROXY_CTX_LIMIT_ENABLED | true | false |
| Context limit | PROXY_CTX_CHARS_LIMIT | 180000 | 500000 |
| Memory reject | PROXY_MEMORY_REJECT_THRESHOLD | 90% | 95% |
| Dynamic max_tokens | PROXY_DYNAMIC_MAX_TOKENS_ENABLED | true | false |
| Request size | PROXY_MAX_REQUEST_BYTES | 512000 | 512000 |
| Backend timeout | PROXY_BACKEND_TIMEOUT | 600s | 600s |
| Dedup window | PROXY_DEDUP_WINDOW | 2s | 2s |
| Blocker | PROXY_BLOCKER_ENABLED | true | false |
| Loop threshold | PROXY_LOOP_THRESHOLD | 3 | 3 |
| Tool filter | PROXY_TOOL_FILTER_ENABLED | true | false |
| Failure snapshots | PROXY_SNAPSHOT_ENABLED | true | true |
| Frozen head | PROXY_FROZEN_HEAD | 12 | 0 |
| Truncate strategy | PROXY_CTX_TRUNCATE_STRATEGY | rounds(default) | char |
| Keep rounds | PROXY_CTX_KEEP_ROUNDS | 10 | 10 |

---

## 6. Service Management (manage.sh, 1588 lines)

### Commands

| Command | Function | Scope |
|---------|----------|-------|
| start | Start backend + proxy | Both processes |
| start-cloud | Start proxy only (cloud mode) | Proxy only |
| stop | Stop proxy + backend | All processes |
| restart | Stop + 2s delay + start | All processes |
| reload | SIGHUP hot-reload config (~0.5s) | Proxy only, no restart |
| start-backend | Start local model only | Backend process |
| stop-backend | Stop local model only | Backend process |
| switch <name> | Switch active.conf symlink | Config file |
| status | Display running status | Read-only |
| logs [N] | View backend logs | Read-only |
| proxy-logs [N] | View proxy logs | Read-only |
| list | List available configs | Read-only |
| current | Show current config details | Read-only |
| watchdog | Monitoring daemon | Loop check + auto-restart |
| route-force-local | Force session to local | Runtime route override |
| route-force-cloud | Force session to cloud | Runtime route override |
| fix-template | Fix chat_template.jinja | File write |
| monitor | Metal memory real-time monitor | Read-only |
| help | Show usage info | Read-only |

### reload vs restart

| Aspect | restart | reload |
|--------|---------|--------|
| Proxy process | Killed + restarted | Stays alive (PID unchanged) |
| Local model | Stopped + restarted | Unaffected |
| Switch time | 8-60s (model reload) | ~0.5s |
| In-flight requests | Interrupted | Unaffected |
| Updates | Everything | All except PORT/HOST + thread-local state |

### Hot-switch Workflow

Local to Cloud:
  manage.sh switch deepseek-chat
  manage.sh reload
  manage.sh stop-backend  (optional: free GPU memory)

Cloud to Local:
  manage.sh switch rapid-mlx-35b
  manage.sh reload
  manage.sh start-backend

### Supported Backends

| Backend | Type | Startup function | Notes |
|---------|------|-----------------|-------|
| llama-server | Local GGUF | _start_llama_server() | Brew or source build |
| rapid-mlx | Local MLX | _start_rapid_mlx() | Apple-optimized |
| vllm-mlx | Local MLX | _start_rapid_mlx() (via LLAMA_SERVER_BIN) | BatchedEngine |
| mlx_vlm | Local VLM | _start_mlx_vlm() | Vision-language |
| DeepSeek API | Cloud | None (proxy only) | deepseek-v4-pro |
| OpenAI API | Cloud | None (proxy only) | Via LLAMA_BASE_URL |

### Health Check / Watchdog

- _wait_for_ready(): polls /v1/models, 60s timeout, supports download detection
- cmd_watchdog(): loop daemon, checks PID liveness + API health + tok/s every 60s
- Auto-restart on: process death, API unresponsive (MAX_FAIL=3), throughput < 15 tok/s
- Rate-limited to max 6 restarts/hour

---

## 7. Backend Strategy Pattern (backend_strategy.py)

| Feature | LocalStrategy | CloudStrategy |
|---------|--------------|--------------|
| Concurrency default | 1 | 4 |
| Context limit | Enabled (180K chars) | Disabled |
| OOM safety | Enabled | Disabled |
| Blocker detection | Enabled | Disabled |
| Tool filter | Enabled | Disabled |
| Semantic compression | Enabled | Disabled |
| Prefix cache | Enabled | Disabled |
| Dynamic max_tokens | Enabled | Disabled |
| MODEL_NAME | Local model name | deepseek-v4-pro |

BackendStrategy.create(is_cloud) factory returns the appropriate strategy.

---

## 8. Testing Infrastructure

| Tier | Location | Runtime | Dependencies |
|------|----------|---------|-------------|
| Unit | test/unit/ (18 files, 808 tests) | <2s | None (pure logic) |
| Integration | test/integration/ (7 suites) | ~5s | mock_backend.py |
| E2E | test/e2e/ | ~30-60s | Running proxy + backend |

### Runner

bash test/run_tests.sh --unit       (default if no flag)
bash test/run_tests.sh --integration
bash test/run_tests.sh --e2e
bash test/run_tests.sh --all        (unit + integration + e2e + trace)
bash test/run_tests.sh --trace      (requirement traceability)

### Pre-commit Hook (.githooks/pre-commit)
- Runs: unit tests + signature check + behavior snapshot
- Skip: SKIP_TESTS=1 git commit  or  git commit --no-verify
- Install: git config core.hooksPath .githooks

### Test Coverage
- message_converter: format conversion, tool mapping, token estimation
- tool_parser: 5-stage fallback, streaming extractor
- content_compressor: content detection, all compressors, audit
- loop_detection: identical calls, text similarity, dedup
- truncation: all 4 strategies, tool pairing repair
- proxy_state: config parse, reload, defaults
- proxy_logging: sensitive header masking, JSONL format
- lifecycle: stage classification, boundary conditions
- integration: 7 bash suites testing blocker, loop, cache, compress,
  memory reject, status, long-context scenarios

---

## 9. Key Gotchas & Known Issues

1. HF_HUB_OFFLINE=1 REQUIRED for vllm-mlx
   vllm-mlx v0.6.71 connects to huggingface.co on startup.
   Without HF_HUB_OFFLINE=1, it hangs indefinitely on ConnectTimeout.
   Fix: export in config file (see configs/qwen3-8b.conf).

2. GPU Memory Utilization >0.85 risks kernel panic
   On 48GB Macs, default 0.90 (36.2GB) can trigger Apple Silicon kernel panic.
   KV cache + activations can overshoot allocation by 20-40%.
   Recommendation: 0.80 (32.2GB) for 27B models.

3. Cross-request prefix cache unavailable (BatchedEngine limit)
   rapid-mlx v0.6.71 BatchedEngine lacks MemoryAwarePrefixCache.
   All requests do full prefill; cache hits are zero.

4. Metal device deadlock on rapid kill -9
   Multiple kill -9 in quick succession can cause Metal init hang.
   Requires machine reboot. Prefer manage.sh stop-backend.

5. Proxy always connects to 127.0.0.1:4000
   Never modify Claude Code config directly.
   Backend switching is done entirely at proxy layer.

6. Tool Clearing OFF recommended for local backends
   Clearing tool_results + rapid-mlx Wasted call = death loop.
   Keep PROXY_CLEAR_ENABLED=false for local backends.

7. Two concurrent large-context requests on rapid-mlx will OOM
   48GB unified memory cannot handle 2x >38K token prefill simultaneously.

8. Rapid-MLX ignores max_tokens (v0.6.30)
   PROXY_MAX_TOKENS_OVERRIDE enforces hard cap in proxy.

9. Dynamic max_tokens not applied to cloud (stage mismatch)
   DEF-014: Pipeline Stage 2 only fires for local backends.

10. Model alias to route mapping is static
    DEF-018: claude-sonnet-4-6 always maps to route preference,
    cannot dynamically match actual cloud model availability.

---

## 10. Key Design Patterns

### Dual-setattr Hot Reload
_reload_config() updates both proxy_state and anthropic_proxy modules
via setattr on SIGHUP. Ensures sub-modules reading proxy_state at call
time and local functions referencing module-level names both see new values.
Test guard: test_proxy_reload.py verifies sync after every reload.

### SmartRouter 10-level Priority Cascade
Per-request routing decision with escalating priorities.
Each level can override the previous. Allows fine-grained local/cloud split.

### PipelineStage Composition (Strategy Pattern)
Each stage is a class with process(ctx) interface. ConditionalStage allows
runtime activation. InstrumentedPipeline wraps with timing/metrics.
Easy to add/remove/reorder stages without changing the core handler.

### Strategy Pattern for Backend Defaults
BackendStrategy.create() returns LocalStrategy or CloudStrategy with
appropriate defaults for concurrency, context limits, safety features.
Adding new backends = new subclass.