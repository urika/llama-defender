# 智能模型路由：系统设计文档

> **文档版本**: v2.12
> **创建日期**: 2026-06-21
> **状态**: 设计阶段  
> **关联 PRD**: `docs/01-requirements-product/PRD-intelligent-model-routing.md`（产品概述、用户分析、问题定义、功能需求、非功能需求）  
> **关联架构**: `docs/02-architecture-design/proxy-pipeline-reference.md`

---

## 目录

1. [系统架构设计](#1-系统架构设计)
   - 1.4 [关键设计决策](#14-关键设计决策)
   - 1.5 [Cloud Stage Skip 实现](#15-cloud-stage-skip-实现)
   - 1.6 [路由通知注入 Stage](#16-路由通知注入-stage)
2. [数据流设计](#2-数据流设计)
   - 2.3 [云端回退完整流程](#23-云端回退完整流程)
   - 2.4 [PipelineContext 完整字段表](#24-pipelinecontext-完整字段表)
   - 2.5 [数据架构设计](#25-数据架构设计)
3. [配置体系设计](#3-配置体系设计)
   - 3.4 [配置 Profile 系统](#34-配置-profile-系统phase-2)
4. [路由决策引擎设计](#4-路由决策引擎设计)
   - 4.4 [决策伪代码（含冷却期定时器）](#44-决策伪代码含冷却期定时器)
5. [可观测性设计](#5-可观测性设计)
   - 5.4 [敏感路径匹配实现](#54-敏感路径匹配实现)
6. [实施阶段计划](#6-实施阶段计划)
7. [成功指标](#7-成功指标)
8. [风险与缓解](#8-风险与缓解)
9. [模型 ID 暴露契约](#9-模型-id-暴露契约)
10. [附录](#10-附录)

---

## 1. 系统架构设计

### 1.1 整体架构

```
                    Claude Code (Anthropic SDK)
                           │
                           ▼
              ┌─────────────────────────┐
              │   anthropic_proxy:4000  │
              │                        │
              │  do_POST → _handle_messages()
              │                        │
              │  InstrumentedPipeline   │
              │  ┌────────────────────┐ │
              │  │ 0. RequestParser   │ │
              │  │ 1. Lifecycle       │ │
              │  │ 2. DynamicMaxTokens│ │
              │  │                    │ │
              │  │ ★ 2.5 SmartRouter  │ │  ← 新增
              │  │                    │ │
              │  │ 3-20. 条件执行    │ │  ← 云端跳过截断/压缩
              │  │                    │ │
              │  │ ★ 21. BackendDispatcher │ ← 改造: 双后端
              │  └────────────────────┘ │
              │           │             │
              │     ┌─────┴─────┐       │
              │     ▼           ▼       │
              │  ┌──────┐  ┌────────┐   │
              │  │本地锁│  │ 云端锁 │   │
              │  └──┬───┘  └───┬────┘   │
              └─────┼──────────┼────────┘
                    │          │
         ┌──────────▼──┐  ┌───▼──────────┐
         │ rapid-mlx   │  │ DeepSeek API │
         │ :8081       │  │ api.deepseek │
         │ 48GB 本地   │  │ .com/v1      │
         └─────────────┘  └──────────────┘
```

### 1.2 新增/改造模块

| 模块 | 当前状态 | 目标状态 | 改动描述 |
|------|---------|---------|---------|
| **RequestParser** (pipeline.py) | 已有 | **+8 行** | 从 HTTP header `X-Proxy-Route-To` 解析单次覆盖值写入 `ctx._route_header_override`；从 `body.model` 提取 Agent tier 写入 `ctx._agent_model_tier` |
| **SmartRouter** (pipeline.py) | 不存在 | **新增 Stage** | 150 行，位于 LifecycleClassifier 之后，根据 total_chars + stage + session 状态 + 内存压力 + Header 覆盖决策路由目标 |
| **RouteNotification** (pipeline.py) | 不存在 | **新增 Stage** | 40 行，SmartRouter 之后，首次路由到云端时注入通知 |
| **BackendDispatcher** (pipeline.py) | 单后端 | **双后端 + 回退** | 40 行改动，根据 ctx._route_target 选择 base_url/api_key/model/lock；cloud HTTPError 时内部 fallback + Emergency truncation + FALLBACK_ENABLED 检查 |
| **PipelineContext** (pipeline.py) | 已有 | **+7 字段** | `_route_target` / `_route_reason` / `_route_header_override` / `_route_actual_cost` / `_route_cloud_model` / `_emergency_fallback` / `_agent_model_tier` |
| **proxy_state.py** | 已有 | **+14 常量 + 1 锁 + 4 dicts** | 路由配置常量 + `_cloud_lock` + `_SESSION_ROUTE_MAP` / `_SESSION_ROUTE_FORCE_SOURCE` / `_cloud_fail_count` / `_cloud_cooldown_start` |
| **_handle_messages** (anthropic_proxy.py) | 已有 | **+4 行** | 在 Stage 列表添加 SmartRouter(2.5) + RouteNotification(2.6) + 传递 cloud_lock |
| **ConditionalStage 条件** (pipeline.py) | 已有 | **4 个 Stage 改 should_run** | ContextTruncator/OOMSafetyFIFO/ContentCompressor/CacheAligner 添加 cloud skip |
| **/status 页面** (admin_server.py) | 已有 | **+30 行** | 路由状态面板 |
| **admin_server.py** | 已有 | **+15 行** | 新增 `POST /admin/route/force-local` 和 `POST /admin/route/force-cloud`（供 manage.sh 调用） |
| **manage.sh** | 已有 | **+20 行** | 新增 `cmd_route_force_local` / `cmd_route_force_cloud`，通过 curl 调用 admin endpoint |

### 1.3 Stage 执行流程图

```
Stage 0:  RequestParser        ← 始终执行
Stage 1:  LifecycleClassifier  ← 始终执行 (路由需要 stage + chars 信息)
Stage 2:  DynamicMaxTokens     ← 始终执行
Stage 2.5: SmartRouter ★       ← 始终执行 (PROXY_ROUTE_ENABLED=false 时返回 local)
Stage 2.6: RouteNotification ★ ← 条件（首次路由到云端时注入通知，仅执行一次 per session）
                                    │
                    ┌───────────────┼───────────────┐
                    ▼                               ▼
              local 路径                      cloud 路径
                    │                               │
Stage 3:  ErrorTranslator     ← 始终   Stage 3:  ErrorTranslator     ← 始终
Stage 4:  BlockerDetector     ← 始终   Stage 4:  BlockerDetector     ← 始终
Stage 5:  SystemNormalizer    ← 始终   Stage 5:  SystemNormalizer    ← 始终
Stage 6:  CacheAligner        ← 始终   Stage 6:  CacheAligner        ← SKIP ★
Stage 7:  ContentCompressor   ← 始终   Stage 7:  ContentCompressor   ← SKIP ★
Stage 8:  ToolLoopDetector    ← 始终   Stage 8:  ToolLoopDetector    ← 始终
Stage 9:  TextLoopDetector    ← 始终   Stage 9:  TextLoopDetector    ← 始终
Stage 10: SessionLoopState    ← 始终   Stage 10: SessionLoopState    ← 始终
Stage 11: LoopIntervention    ← 始终   Stage 11: LoopIntervention    ← 始终
Stage 12: RereadDetector      ← 始终   Stage 12: RereadDetector      ← 始终
Stage 13: DateNormalizer      ← 始终   Stage 13: DateNormalizer      ← 始终
Stage 14: ContextTruncator    ← 始终   Stage 14: ContextTruncator    ← SKIP ★
Stage 15: HighDropRatioNotice ← 始终   Stage 15: HighDropRatioNotice ← SKIP ★
Stage 16: MessageHashDebug    ← 始终   Stage 16: MessageHashDebug    ← 始终
Stage 17: OOMSafetyFIFO       ← 条件   Stage 17: OOMSafetyFIFO       ← SKIP ★
Stage 18: PrefixRatioComputer ← 始终   Stage 18: PrefixRatioComputer ← 始终
Stage 19: ToolPairingRepair   ← 始终   Stage 19: ToolPairingRepair   ← 始终
Stage 20: FormatConverter     ← 始终   Stage 20: FormatConverter     ← 始终 (model 改为 PROXY_CLOUD_MODEL)
Stage 21: BackendDispatcher   ← 本地   Stage 21: BackendDispatcher   ← 云端 ★
                    │                               │
                    ▼                               ▼
              rapid-mlx :8081              DeepSeek / OpenAI API
```

> ★ = 路由到云端时行为变化

### 1.4 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 路由决策位置 | Stage 2.5 (Lifecycle 之后, 内容处理之前) | Lifecycle 提供了路由决策所需的 total_chars 和 stage 信息；在内容处理之前决策可以避免无意义的压缩/截断 |
| Session 内不切换 | 一旦云端就保持云端 | 避免 prefix cache 断裂、输出风格不一致、ping-pong 延迟 |
| 云端保持防御层 | 循环/blocker 检测在云端也执行 | 云端模型也可能出错，防御层开销极小 (<1ms) |
| 默认使用 flash | deepseek-v4-flash | ¥0.5/M input tokens, 是 pro 的 1/4 成本 |
| 默认禁用路由 | PROXY_ROUTE_ENABLED=false | 向后兼容，用户主动开启 |
| 回退冷却期 | 连续失败后冷却 30 分钟再尝试 | 避免永久 `local_forced`，给云端恢复的机会；可通过 `PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS` 配置 |

### 1.5 Cloud Stage Skip 实现

路由到云端时，以下 Stage 通过 `ConditionalStage.should_run()` 跳过。每个 Skip 的理由不同：

```python
# === Stage 6: CacheAligner ===
# 云端无本地 KV cache，不需要 prefix/dynamic 拆分来稳定前缀缓存。
# 且云端 ContextTruncator 和 ContentCompressor 均 SKIP，
# 没有截断/压缩需要 prefix 保护，故 alignment 本身无意义。
# 安全分析验证：下游 Stage（8-20）均不依赖 _cache_prefix/_cache_dynamic 私有字段
class CacheAligner(ConditionalStage):
    def should_run(self, ctx):
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            return False
        return _ps.PROXY_CACHE_ALIGN_ENABLED

# === Stage 7: ContentCompressor ===
# 云端 128K/1M token 上下文，无需 Tool Clearing + Thinking Strip + 语义压缩
# 注意：云端 SKIP 后 ctx.cleared_files 为 None/[]，RereadDetector 无 re-read 可检测
class ContentCompressor(ConditionalStage):
    def should_run(self, ctx):
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            return False
        return True  # 本地时始终执行（内部根据 PROXY_CLEAR_ENABLED 进一步判断）

# === Stage 14: ContextTruncator ===
# 云端 128K/1M token 上下文，不需要截断
class ContextTruncator(ConditionalStage):
    def should_run(self, ctx):
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            return False
        return _ps.PROXY_CTX_LIMIT_ENABLED

# === Stage 15: HighDropRatioNotice ===
# 依赖 ContextTruncator 的 trunc_stats，云端无截断故无条件触发
class HighDropRatioNotice(ConditionalStage):
    def should_run(self, ctx):
        if getattr(ctx, '_route_target', 'local') == 'cloud':
            return False
        return ctx.trunc_stats is not None  # 无截断数据时跳过
```

**注意事项**：
- `HighDropRatioNotice` 当前继承 `PipelineStage`（非条件），需改为继承 `ConditionalStage` 以支持 `should_run`
- Stage 17 `OOMSafetyFIFO` 已是 `ConditionalStage`，其 `should_run` 中已有 `not IS_CLOUD` 检查 — 添加 `_route_target != "cloud"` 即可
- 循环/blocker 检测 Stage（3-5, 8-12）在云端**继续执行** — 云端模型也可能出错

### 1.6 路由通知注入 Stage

PRD FR-6.1 要求首次路由到云端时注入 `[System: Switched to cloud model...]` 通知。独立 Stage 置于 SmartRouter 之后。

> **分阶段实现**: Phase 1 使用简化版（仅日志输出路由切换事件），Phase 2 升级为完整的消息流注入（含首次路由和紧急回退两种模板）。

```python
class RouteNotification(ConditionalStage):
    """Stage 2.6: Inject route-switch notification when target changes to cloud.

    Conditional: only runs when _route_target == 'cloud' AND
    this session hasn't been notified yet.
    """

    name = "route_notification"

    def should_run(self, ctx):
        if getattr(ctx, '_route_target', 'local') != 'cloud':
            return False
        session_id = ctx.session_id
        if session_id and getattr(_ps, f"_route_notified_{session_id}", False):
            return False
        return True

    def process(self, ctx: PipelineContext) -> PipelineContext:
        session_id = ctx.session_id

        if ctx._emergency_fallback:
            notice = self._build_emergency_notice(ctx)
        else:
            notice = self._build_first_route_notice(ctx)

        ctx.messages.append({
            "role": "user",
            "content": [{"type": "text", "text": notice}],
        })

        notified_key = f"_route_notified_{session_id}" if session_id else None
        if notified_key:
            setattr(_ps, notified_key, True)

        log(f"  -> Route notification injected (session={session_id}, model={_ps.PROXY_CLOUD_MODEL})")
        return ctx

    def _build_first_route_notice(self, ctx):
        """首次主动路由到云端的通知。"""
        total_chars = ctx.stage_config.get("total_chars", 0) if ctx.stage_config else 0
        threshold = _ps.PROXY_ROUTE_THRESHOLD_CHARS
        model = _ps.PROXY_CLOUD_MODEL
        return (
            f"[System: Switched to cloud model — context {total_chars:,} chars "
            f"exceeds local {threshold:,} limit. Using {model}. "
            f"Estimated cost ~¥0.01-0.04/request. "
            f"Session will stay on cloud. New sessions return to local. "
            f"To force local: `./manage.sh route-force-local {ctx.session_id or 'SESSION_ID'}`.]"
        )

    def _build_emergency_notice(self, ctx):
        """云端回退到本地的紧急通知。"""
        total_chars = ctx.stage_config.get("total_chars", 0) if ctx.stage_config else 0
        target = min(_ps.PROXY_OOM_SAFE_CHARS // 2, _ps.PROXY_CHARS_EXPANSION)
        return (
            f"[System: Cloud API unavailable, emergency fallback to local. "
            f"Context severely truncated from {total_chars:,} to ~{target:,} chars "
            f"(kept last 3 rounds). "
            f"Consider /compact or retry when cloud recovers. "
            f"To force cloud retry: `./manage.sh route-force-cloud {ctx.session_id or 'SESSION_ID'}`.]"
        )
```

**设计要点**：
- 通过 `_ps._route_notified_{session_id}` 标记确保每个 Session 只通知一次
- 通知内容包含切换原因、上下文大小、成本估算、如何切回本地
- 通知作为 user message 追加，确保模型能看到并据此调整行为

---

## 2. 数据流设计

### 2.1 路由决策数据流

```
┌──────────────────┐
│ PipelineContext  │
│ .total_chars     │ ← RequestParser 填充
│ .stage_config    │ ← LifecycleClassifier 填充
│ .session_id      │ ← RequestParser 填充
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────────────────────┐
│              SmartRouter.process()                │
│                                                  │
│  1. if not PROXY_ROUTE_ENABLED:                  │
│       → route_target = "local"                   │
│       → return                                   │
│                                                  │
│  2. check _SESSION_ROUTE_MAP[session_id]:         │
│       if "cloud": → route_target = "cloud"       │
│       (maintain session consistency)             │
│                                                  │
│  3. check total_chars vs threshold:              │
│       if > PROXY_ROUTE_THRESHOLD_CHARS:          │
│         → route_target = "cloud"                 │
│         → _SESSION_ROUTE_MAP[id] = "cloud"       │
│                                                  │
│  4. check memory pressure:                       │
│       if used_pct > 90 and available_gb < 5:     │
│         → route_target = "cloud"                 │
│         → _SESSION_ROUTE_MAP[id] = "cloud"       │
│                                                  │
│  5. check cloud health:                          │
│       if cloud_consecutive_failures >= 3:         │
│         → route_target = "local"                 │
│         → force local for this session           │
│                                                  │
│  6. log decision with reason                     │
│  7. set ctx._route_target, ctx._route_reason     │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│           Downstream Stages                      │
│                                                  │
│  ContentCompressor.should_run:                   │
│    → return False if ctx._route_target=="cloud"  │
│                                                  │
│  ContextTruncator.should_run:                    │
│    → return False if ctx._route_target=="cloud"  │
│                                                  │
│  OOMSafetyFIFO.should_run:                       │
│    → return False if ctx._route_target=="cloud"  │
│                                                  │
│  FormatConverter.process:                        │
│    → openai_body["model"] =                      │
│        PROXY_CLOUD_MODEL if ctx._route_target    │
│        == "cloud" else MODEL_NAME                │
│                                                  │
│  BackendDispatcher.process:                      │
│    → select base_url/api_key/lock based on       │
│      ctx._route_target                           │
│    → on cloud HTTPError: fallback to local       │
└──────────────────────────────────────────────────┘
```

### 2.2 BackendDispatcher 双后端调度

```
BackendDispatcher.process(ctx)
│
├─ route_target == "local"
│   ├─ base_url = LLAMA_BASE
│   ├─ api_key  = LLAMA_API_KEY
│   ├─ lock     = _llama_lock
│   └─ model    = MODEL_NAME
│
├─ route_target == "cloud"
│   ├─ base_url = PROXY_CLOUD_BASE_URL
│   ├─ api_key  = PROXY_CLOUD_API_KEY
│   ├─ lock     = _cloud_lock
│   └─ model    = PROXY_CLOUD_MODEL
│
├─ with selected_lock:
│   ├─ send HTTP POST to {base_url}/chat/completions
│   ├─ on success:
│   │   ├─ handler._handle_streaming_response   (streaming) 
│   │   │   └─ after stream ends: extract usage from last chunk
│   │   │      → actual_cost = (prompt_tokens × PRICE_INPUT + completion_tokens × PRICE_OUTPUT) / 1M
│   │   │      → write ctx._route_actual_cost
│   │   └─ handler._handle_non_streaming_response (non-streaming)
│   │       └─ extract response["usage"] → same calculation
│   │       → write ctx._route_actual_cost
│   └─ on HTTPError:
│       ├─ if route_target == "cloud":
│       │   ├─ log("Cloud API failed, falling back to local")
│       │   ├─ _cloud_fail_count[session_id] += 1
│       │   └─ retry with local backend ⚠️
│       └─ else:
│           └─ handler._respond_json(error, code)
│
└─ output_metrics:
    └─ { backend_status, stream, route_target, route_cloud_model }
```

> ⚠️ **回退注意事项**: 云端回退到本地时需要关注死锁风险。本地锁和云端锁是独立的 semaphore。如果当前线程持有 cloud lock，需要获取 local lock，必须先退出 `with self._cloud_lock` 块，再进入 `with self._llama_lock` 块。

### 2.3 云端回退完整流程（采用 BackendDispatcher 内部重试）

**决策**：采用 BackendDispatcher 内部处理 fallback（方案 A），而非修改 Pipeline 框架支持 stage 重试。理由：修改面更小，metrics 连续，避免 InstrumentedPipeline 改造风险。

**核心流程**：

```python
class BackendDispatcher(PipelineStage):
    def process(self, ctx):
        if ctx._route_target == 'cloud':
            # === Cloud path ===
            try:
                with self._cloud_lock:
                    resp = self._dispatch(ctx, base_url=PROXY_CLOUD_BASE_URL,
                                          api_key=PROXY_CLOUD_API_KEY, model=PROXY_CLOUD_MODEL)
                self._backend_status = resp.status if resp else 0
            except urllib.error.HTTPError as e:
                # === Cloud failed → fallback to local ===
                if not _ps.PROXY_ROUTE_FALLBACK_ENABLED:
                    # Fallback disabled: return 503, do NOT silently route to local
                    log(f"  -> Cloud API failed ({e.code}), fallback disabled — returning 503")
                    self._handler._respond_json({
                        "error": {
                            "type": "cloud_unavailable",
                            "message": f"Cloud API failed with status {e.code} and fallback is disabled. Enable with PROXY_ROUTE_FALLBACK_ENABLED=true."
                        }
                    }, 503)
                    self._backend_status = 503
                    return ctx

                self._record_cloud_failure(ctx)
                ctx._route_target = 'local_forced'
                ctx._route_reason = 'cloud_fallback'
                log(f"  -> Cloud API failed ({e.code}), falling back to local")

                # Emergency truncation: happens HERE, not in ContextTruncator
                # (ContextTruncator already executed in the Pipeline order)
                # Before truncating: check if the request touches sensitive paths
                if _is_sensitive_request(ctx):
                    log(f"  -> Cloud API failed but request contains sensitive paths — blocking fallback")
                    self._handler._respond_json({
                        "error": {
                            "type": "sensitive_fallback_blocked",
                            "message": "Cloud API failed and request contains sensitive file paths. Cannot fallback to local."
                        }
                    }, 403)
                    self._backend_status = 403
                    return ctx

                self._emergency_truncate(ctx)

                # cloud_lock already released (exited the `with` block)
                with self._llama_lock:
                    resp = self._dispatch(ctx, base_url=LLAMA_BASE,
                                          api_key=LLAMA_API_KEY, model=MODEL_NAME)
                self._backend_status = resp.status if resp else 0

        elif ctx._route_target in ('local', 'local_forced'):
            # === Local path ===
            with self._llama_lock:
                resp = self._dispatch(ctx, base_url=LLAMA_BASE,
                                      api_key=LLAMA_API_KEY, model=MODEL_NAME)
            self._backend_status = resp.status if resp else 0
```

**`_dispatch` 函数签名**：

```python
def _dispatch(self, ctx, base_url, api_key, model) -> http.client.HTTPResponse:
    """Send HTTP POST to backend. Returns the HTTP response object.
    
    Calls handler._handle_streaming_response or handler._handle_non_streaming_response
    internally.  After the handler returns, extracts usage from the stream/response
    for cost calculation.
    """
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(ctx.openai_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    if "include_usage" not in ctx.openai_body.get("stream_options", {}):
        ctx.openai_body.setdefault("stream_options", {})["include_usage"] = True
    resp = urllib.request.urlopen(req, timeout=PROXY_BACKEND_TIMEOUT)
    if ctx.is_stream:
        self._handler._handle_streaming_response(resp, ctx)
        # After stream ends: extract usage.completion_tokens from last chunk
        # _last_stream_usage is set by _handle_streaming_response from the
        # final stream chunk's usage field (requires stream_options.include_usage=true)
        if hasattr(self._handler, '_last_stream_usage'):
            ctx._route_actual_cost = (
                (ctx.total_chars / PROXY_CTX_TOKEN_RATIO) * PROXY_CLOUD_PRICE_INPUT
                + self._handler._last_stream_usage.get("completion_tokens", 0) * PROXY_CLOUD_PRICE_OUTPUT
            ) / 1_000_000
    else:
        resp_body = json.loads(resp.read().decode("utf-8"))
        self._handler._handle_non_streaming_response(resp_body, ctx)
        usage = resp_body.get("usage", {})
        ctx._route_actual_cost = (
            usage.get("prompt_tokens", 0) * PROXY_CLOUD_PRICE_INPUT
            + usage.get("completion_tokens", 0) * PROXY_CLOUD_PRICE_OUTPUT
        ) / 1_000_000
    return resp
```

> **include_usage 说明**: 云端 streaming 请求必须在 `stream_options` 中设置 `include_usage: true`，最后一个 chunk 才会包含 `usage` 字段。当前 proxy 的 FormatConverter 需确保转换后的 OpenAI body 包含此字段。

**`_emergency_truncate` 实现**：

```python
def _emergency_truncate(self, ctx):
    """Emergency context reduction when cloud fallback to overloaded local.

    Called inside BackendDispatcher when cloud fails and local context
    exceeds PROXY_OOM_SAFE_CHARS.  Must run after ContextTruncator has
    already executed (we are truncating FURTHER on top of normal truncation).
    """
    import truncation

    total_chars = ctx.stage_config.get('total_chars', 0) if ctx.stage_config else 0
    target = min(_ps.PROXY_OOM_SAFE_CHARS // 2, _ps.PROXY_CHARS_EXPANSION)

    if total_chars <= target:
        log(f"  -> Emergency truncation skipped: {total_chars} chars within {target} limit")
        return

    # Use the same truncation function but with emergency parameters
    emergency_messages, emergency_stats = truncation.truncate_messages_if_needed(
        ctx.messages,
        session_id=ctx.session_id,
        force_strategy='rounds',
        force_keep_rounds=3,
        force_max_chars=target,
    )

    dropped = emergency_stats.get('dropped_messages', 0)
    kept = emergency_stats.get('kept_messages', 0)
    log(f"  -> EMERGENCY TRUNCATION: {dropped} messages dropped, "
        f"{kept} kept (target={target:,} chars, kept last 3 rounds)")

    # Inject emergency notice
    notice = (
        f"[System: Cloud API unavailable, emergency fallback to local. "
        f"Context severely truncated from {total_chars:,} to ~{target:,} chars "
        f"(kept last 3 rounds). Consider /compact or retry when cloud recovers.]"
    )
    emergency_messages.append({
        "role": "user",
        "content": [{"type": "text", "text": notice}],
    })

    ctx.messages = emergency_messages
    ctx._emergency_fallback = True
```

**设计要点**：
- 回退在 BackendDispatcher 内部完成，不重新提交到 Pipeline 入口 — 避免 InstrumentedPipeline 改造
- Emergency truncation 在 BackendDispatcher 内部调用，解决了「ContextTruncator 已执行过」的时序矛盾
- cloud_lock 和 llama_lock 不会同时持有 — 先退出 `with self._cloud_lock` 再进入 `with self._llama_lock`
- Emergency truncation 复用 `truncation.truncate_messages_if_needed` 函数，不重复实现

**`_record_cloud_failure` 辅助函数**：

```python
def _record_cloud_failure(self, ctx):
    """Record cloud failure and manage cooldown state."""
    session_id = ctx.session_id
    if not session_id:
        return

    with _ps._state_lock:
        _ps._cloud_fail_count[session_id] = _ps._cloud_fail_count.get(session_id, 0) + 1
        fail_count = _ps._cloud_fail_count[session_id]

        if fail_count >= PROXY_ROUTE_MAX_CLOUD_FAILS:
            _ps._cloud_cooldown_start[session_id] = time.monotonic()
            _ps._SESSION_ROUTE_MAP[session_id] = "local_forced"
            _ps._SESSION_ROUTE_FORCE_SOURCE[session_id] = "cloud_failures"
            log(f"  -> Cloud cooldown activated for session {session_id} "
                f"({fail_count} failures, {PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS}s)")
```

### 2.4 PipelineContext 完整字段表

以下列出路由功能新增/修改的 PipelineContext 字段：

```python
@dataclass
class PipelineContext:
    # === 现有字段（不变） ===
    request_id: str = ""
    model: str = "unknown"
    is_stream: bool = False
    max_tokens_orig: int = 4096
    raw_tools_orig: list = field(default_factory=list)
    session_id: str = ""
    total_chars: int = 0
    tools_list: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    body: dict = field(default_factory=dict)
    stage_config: Optional[dict] = None
    # ... (其余现有字段不变)

    # === 路由新增字段 ===
    _route_target: str = "local"          # "local" | "cloud" | "local_forced"
    _route_reason: str = ""               # 决策原因标签（如 "chars_exceed_threshold"）
    _route_header_override: str = ""      # 单次请求 Header 覆盖值（"local"|"cloud"|""），不写入 Session 状态
    _route_actual_cost: float = 0.0       # post-request 真实成本（从 backend response.usage 计算）
    _emergency_fallback: bool = False     # 是否处于紧急回退模式
    _route_cloud_model: str = ""          # 实际使用的云端模型。Phase 1 固定 = PROXY_CLOUD_MODEL。Phase 2/3 若支持多云端 provider（如 LiteLLM Sidecar），此字段承载动态路由结果
    _agent_model_tier: str = "sonnet"     # Agent 选择的模型 tier（"opus"|"sonnet"|"haiku"），由 RequestParser 从 body.model 提取，SmartRouter 和 FormatConverter 使用
```

**字段分组**：
- `_route_target` / `_route_reason`：SmartRouter 写入，下游 Stage 读取
- `_route_notified`：跨请求状态，通过 `_ps._route_notified_{session_id}` 存储在 proxy_state 中（不在 PipelineContext 中，避免与 RouteNotification Stage 内的标记机制重复）
- `_emergency_fallback`：BackendDispatcher 在回退时写入，用于日志/metrics 标记
- `_route_cloud_model`：SmartRouter 根据 tier 偏好写入，FormatConverter 和 BackendDispatcher 使用
- `_agent_model_tier`：RequestParser 从 `body.model` 提取，SmartRouter 据此调整阈值 + 选择云端模型

---

### 2.5 数据架构设计

#### 2.5.1 数据实体模型

智能路由涉及三类数据实体，按生命周期和作用域分层：

```
┌─────────────────────────────────────────────────────────────────┐
│                      数据实体分层架构                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Layer 1: 配置层（进程级，热重载）                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ PROXY_ROUTE_ENABLED         bool   路由总开关             │   │
│  │ PROXY_ROUTE_THRESHOLD_CHARS int    上下文阈值             │   │
│  │ PROXY_CLOUD_BASE_URL        str    云端 API 端点          │   │
│  │ PROXY_CLOUD_API_KEY         str    云端 API Key           │   │
│  │ PROXY_CLOUD_MODEL           str    云端模型名             │   │
│  │ PROXY_ROUTE_CLOUD_CONCURRENT int   云端并发数             │   │
│  │ PROXY_ROUTE_MEMORY_PCT      int    内存压力阈值            │   │
│  │ PROXY_ROUTE_FALLBACK_ENABLED bool  回退开关               │   │
│  │ PROXY_ROUTE_MAX_CLOUD_FAILS  int   连续失败上限            │   │
│  │ PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS int 冷却时间            │   │
│  │ PROXY_CLOUD_PRICE_INPUT     float  输入单价 (¥/M tokens)  │   │
│  │ PROXY_CLOUD_PRICE_OUTPUT    float  输出单价 (¥/M tokens)  │   │
│  │ PROXY_ROUTE_SENSITIVE_PATTERNS str 敏感路径正则           │   │
│  │ PROXY_ROUTE_PROFILE         str    Profile 名称           │   │
│  │ PROXY_ROUTE_DAILY_BUDGET    float  日费用上限             │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Layer 2: Session 状态层（跨请求，_state_lock 保护）              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ _SESSION_ROUTE_MAP          dict   session_id → target   │   │
│  │ _SESSION_ROUTE_FORCE_SOURCE  dict   session_id → source  │   │
│  │ _cloud_fail_count           dict   session_id → int      │   │
│  │ _cloud_cooldown_start       dict   session_id → float    │   │
│  │ _route_notified_{id}        attr   session_id → True     │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Layer 3: 请求级数据（单请求生命周期，PipelineContext 字段）       │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ _route_target               str    本次请求后端目标       │   │
│  │ _route_reason               str    决策原因标签           │   │
│  │ _route_header_override      str    Header 单次覆盖        │   │
│  │ _route_actual_cost          float  实际云 API 成本        │   │
│  │ _route_cloud_model          str    实际使用的云端模型      │   │
│  │ _emergency_fallback         bool   是否紧急回退模式        │   │
│  │ stage_config.{total_chars, stage}  ← 上游 Stage 供给      │   │
│  │ messages                    list   ← 上游 Stage 供给      │   │
│  │ body                        dict   ← 上游 Stage 供给      │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Layer 4: 观测层（持久化，JSONL 写入）                            │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ pipeline.smart_router       obj    路由决策记录           │   │
│  │ pipeline.backend_dispatcher obj    后端调度记录           │   │
│  │ pipeline.route_notification obj    通知注入记录           │   │
│  │ pipeline_summary            obj    Stage 级耗时           │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**实体关系**：

```
PROXY_ROUTE_ENABLED ──触发──▶ SmartRouter.process()
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
            Layer 1 Config    Layer 2 Session    Layer 3 Request
            (只读引用)        (读写, 带锁)       (读写, 无锁)
                    │               │               │
                    └───────────────┼───────────────┘
                                    ▼
                            Layer 4 Observability
                            (写入 JSONL, 无锁)
```

#### 2.5.2 数据生命周期

```
┌──────────────────────────────────────────────────────────────────┐
│                    单请求数据生命周期                               │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ① RequestParser                                                 │
│     ctx._route_header_override ← HTTP header "X-Proxy-Route-To" │
│     ctx._route_target = "local"  (默认)                           │
│     ctx._route_actual_cost = 0.0                                  │
│                                                                  │
│  ② LifecycleClassifier                                           │
│     ctx.stage_config = {total_chars, stage, ...}                 │
│     （SmartRouter 依赖此数据做决策）                                │
│                                                                  │
│  ③ SmartRouter.process()                                         │
│     ┌─ 读取: Layer 1 Config (proxy_state.PROXY_ROUTE_*)           │
│     │         Layer 2 Session (_SESSION_ROUTE_MAP 等, 需锁)       │
│     │         ctx._route_header_override                          │
│     │         ctx.stage_config.total_chars, stage                 │
│     │         _get_system_memory().used_pct, available_gb         │
│     │                                                             │
│     ├─ 决策: 按优先级 0-9                                         │
│     │                                                             │
│     └─ 写入: ctx._route_target = "local"|"cloud"|"local_forced"  │
│              ctx._route_reason = "chars_exceed_threshold"|...     │
│              _SESSION_ROUTE_MAP[session_id] = target (需锁)       │
│                                                                  │
│  ④ RouteNotification.process()                                   │
│     ┌─ 读取: ctx._route_target                                    │
│     │         _route_notified_{session_id}                        │
│     │                                                             │
│     └─ 写入: ctx.messages.append(notification)                    │
│              _route_notified_{session_id} = True                  │
│                                                                  │
│  ⑤ 下游 Stage (根据 _route_target 条件执行)                       │
│     ┌─ ContextTruncator.should_run:                               │
│     │     ctx._route_target == "cloud" → False (SKIP)             │
│     │     （emergency truncation 在 BackendDispatcher 内部处理，    │
│     │      不经过 ContextTruncator）                               │
│     │                                                             │
│     └─ ContentCompressor.should_run:                              │
│           ctx._route_target == "cloud" → False (SKIP)             │
│                                                                  │
│  ⑥ BackendDispatcher.process()                                   │
│     ┌─ 路径: cloud                                                │
│     │   ├─ 发送 HTTP → 成功: write ctx._route_actual_cost         │
│     │   └─ 失败:                                                 │
│     │       ├─ not FALLBACK_ENABLED → 503, ctx._route_target 不变 │
│     │       └─ FALLBACK_ENABLED:                                  │
│     │           ├─ _is_sensitive_request → 403                    │
│     │           ├─ _record_cloud_failure:                         │
│     │           │   _cloud_fail_count[session_id] += 1 (需锁)    │
│     │           │   if >= MAX: _cloud_cooldown_start = now (需锁) │
│     │           ├─ _emergency_truncate(ctx)                       │
│     │           └─ 重试本地                                       │
│     │                                                             │
│     └─ 路径: local                                                │
│         └─ 发送 HTTP → 成功/失败                                   │
│                                                                  │
│  ⑦ Pipeline 结束 → InstrumentedPipeline 汇总 metrics             │
│     ┌─ 读取: 各 Stage 的 output_metrics()                         │
│     └─ 写入: proxy_metrics.jsonl                                  │
│                                                                  │
│  ⑧ 请求结束 → PipelineContext 销毁                                │
│     Layer 3 数据随 ctx 对象回收                                    │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘

Session 数据生命周期:

  创建: SmartRouter 首次写入 _SESSION_ROUTE_MAP[session_id]
  更新: 后续请求读取/更新 _SESSION_ROUTE_MAP / _cloud_fail_count / _cloud_cooldown_start
  清理: _classify_lifecycle_stage 中，当 dict 超过 1000 条时 LRU 淘汰
        （与 _SESSION_REQUEST_COUNT 共用清理触发点）
```

#### 2.5.3 数据分布与锁边界

```
┌─────────────────────────────────────────────────────────────────┐
│                        数据分布图                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  proxy_state.py (进程级单例)                                     │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │  Layer 1: PROXY_ROUTE_* 常量 (无锁，读共享)                 │ │
│  │  Layer 2: _SESSION_ROUTE_MAP        ┐                     │ │
│  │           _SESSION_ROUTE_FORCE_SOURCE │                    │ │
│  │           _cloud_fail_count          ├ _state_lock 保护    │ │
│  │           _cloud_cooldown_start      │                     │ │
│  │           _route_notified_{id}       ┘                     │ │
│  │                                                           │ │
│  │  _cloud_lock    Semaphore (云端并发控制，与数据无关)         │ │
│  │  _llama_lock    Semaphore (已有，本地并发控制)              │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                                 │
│  pipeline.py (每次请求创建)                                       │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │  PipelineContext (单请求，无锁)                             │ │
│  │  ┌─────────────────────────────────────────────────────┐  │ │
│  │  │ Layer 3: _route_target, _route_reason,              │  │ │
│  │  │          _route_header_override,                    │  │ │
│  │  │          _route_actual_cost,                        │  │ │
│  │  │          _route_cloud_model,                        │  │ │
│  │  │          _emergency_fallback                        │  │ │
│  │  │                                                    │  │ │
│  │  │ 从上游继承: stage_config, messages, body, ...       │  │ │
│  │  └─────────────────────────────────────────────────────┘  │ │
│  │                                                           │ │
│  │  SmartRouter: 读取 Layer 1+2，写入 Layer 3+2               │ │
│  │  RouteNotification: 读取 Layer 2+3，写入 Layer 2+3         │ │
│  │  BackendDispatcher: 读取 Layer 1+3，写入 Layer 2+3         │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                                 │
│  proxy_metrics.jsonl (磁盘持久化)                                │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │ Layer 4: 每请求追加一行 JSON，无并发写入冲突（追加模式）      │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

锁边界:

  _state_lock 保护范围:
    - _SESSION_ROUTE_MAP 的读、写、清理
    - _SESSION_ROUTE_FORCE_SOURCE 的读、写、清理
    - _cloud_fail_count 的读、写、清理
    - _cloud_cooldown_start 的读、写、清理
    - _route_notified_{session_id} 动态属性的设置和清理
    - 清理时上述 4 个 dict 的同步 LRU 淘汰

  _state_lock 不保护:
    - PipelineContext 字段（单请求，无竞争）
    - proxy_metrics.jsonl 写入（追加模式 + 文件锁由 OS 处理）
    - PROXY_ROUTE_* 配置常量（热重载时通过 setattr 原子替换）
```

#### 2.5.4 观测指标模型

**完整 Metrics JSONL Schema（单次请求）**：

```json
{
  "schema_version": "v1",
  "request_id": "req_a1b2c3d4",
  "session_id": "abc12345",
  "timestamp": "2026-06-22T10:30:00Z",
  
  "pipeline": {
    "smart_router": {
      "elapsed_ms": 0.2,
      "target": "cloud",
      "reason": "chars_exceed_threshold",
      "chars": 127843,
      "threshold": 90000,
      "stage": "saturation",
      "memory_used_pct": 62.0,
      "memory_available_gb": 18.0,
      "session_route_state": "local",
      "header_override": "",
      "decision_priority": 7
    },
    "route_notification": {
      "elapsed_ms": 0.1,
      "injected": true,
      "reason": "first_cloud_switch"
    },
    "backend_dispatcher": {
      "elapsed_ms": 2341.5,
      "backend_status": 200,
      "stream": 1,
      "route_target": "cloud",
      "route_cloud_model": "deepseek-v4-flash",
      "route_fallback": false,
      "emergency_fallback": false,
      "fallback_reason": "",
      "sensitive_blocked": false,
      "estimated_cost_input": 0.008,
      "estimated_cost_output": 0.003,
      "estimated_cost_total": 0.011,
      "actual_cost_total": 0.012
    },
    "pipeline_summary": {
      "total_stages": 22,
      "executed": 18,
      "skipped": 4,
      "skipped_stages": ["cache_aligner", "content_compressor", "context_truncator", "oom_safety_fifo"],
      "slowest_stage": "backend_dispatcher",
      "slowest_elapsed_ms": 2341.5,
      "total_elapsed_ms": 2567.3
    }
  },
  
  "quality_flags": {
    "loop_injected": false,
    "blocker_injected": false,
    "high_drop_ratio": false
  },
  
  "cost": {
    "currency": "CNY",
    "model": "deepseek-v4-flash",
    "price_input_per_mtok": 0.5,
    "price_output_per_mtok": 1.5,
    "estimated": 0.011,
    "actual": 0.012
  }
}
```

**指标字典**：

| 指标路径 | 类型 | 写入 Stage | 说明 |
|---------|------|-----------|------|
| `pipeline.smart_router.target` | string | SmartRouter | "local" / "cloud" / "local_forced" |
| `pipeline.smart_router.reason` | string | SmartRouter | 决策原因标签 |
| `pipeline.smart_router.decision_priority` | int | SmartRouter | 命中的决策矩阵优先级 (0-9) |
| `pipeline.smart_router.memory_used_pct` | float | SmartRouter | 系统内存使用率 |
| `pipeline.smart_router.session_route_state` | string | SmartRouter | 决策前的 session 状态 |
| `pipeline.route_notification.injected` | bool | RouteNotification | 是否注入了通知 |
| `pipeline.backend_dispatcher.route_target` | string | BackendDispatcher | 实际使用的后端 |
| `pipeline.backend_dispatcher.route_fallback` | bool | BackendDispatcher | 是否触发了回退 |
| `pipeline.backend_dispatcher.emergency_fallback` | bool | BackendDispatcher | 是否触发紧急截断 |
| `pipeline.backend_dispatcher.fallback_reason` | string | BackendDispatcher | 回退原因（cloud HTTP status code） |
| `pipeline.backend_dispatcher.sensitive_blocked` | bool | BackendDispatcher | 是否因敏感路径阻断 |
| `pipeline.backend_dispatcher.estimated_cost_total` | float | BackendDispatcher | pre-request 估算成本 |
| `pipeline.backend_dispatcher.actual_cost_total` | float | BackendDispatcher | post-request 真实成本 |
| `pipeline.pipeline_summary.skipped_stages` | []string | InstrumentedPipeline | 被跳过的 Stage 名称列表 |
| `cost.actual` | float | BackendDispatcher | 从 response.usage 计算，可为 null |
| `quality_flags.loop_injected` | bool | LoopIntervention | 是否触发循环干预 |
| `quality_flags.high_drop_ratio` | bool | HighDropRatioNotice | 是否触发高丢率通知 |

**聚合指标（可在 /status 页面展示）**：

| 聚合指标 | 计算方式 | 数据源 |
|---------|---------|--------|
| `cloud_requests_total` | COUNT(route_target="cloud") | metrics JSONL |
| `cloud_fallbacks_total` | COUNT(route_fallback=true) | metrics JSONL |
| `cloud_failure_rate` | fallbacks / cloud_requests | 派生计算 |
| `estimated_cost_total` | SUM(estimated_cost_total where route_target="cloud") | metrics JSONL |
| `actual_cost_total` | SUM(actual_cost_total where actual_cost_total IS NOT NULL) | metrics JSONL |
| `avg_cloud_latency_ms` | AVG(elapsed_ms where route_target="cloud") | metrics JSONL |
| `loop_rate_cloud` | COUNT(loop_injected=true AND route_target="cloud") / COUNT(route_target="cloud") | metrics JSONL |

---

## 3. 配置体系设计

### 3.1 新增环境变量

| 变量 | 默认值 | 类型 | 热重载 | 说明 |
|------|--------|------|--------|------|
| `PROXY_ROUTE_ENABLED` | `false` | bool | ✅ | 路由总开关。false 时 100% 走本地 |
| `PROXY_ROUTE_THRESHOLD_CHARS` | `90000` | int | ✅ | 上下文字符数阈值，超过时路由到云端 |
| `PROXY_CLOUD_BASE_URL` | `https://api.deepseek.com/v1` | str | ✅ | 云端 API 端点 |
| `PROXY_CLOUD_API_KEY` | **无默认值（必须显式配置）** | str | ❌ | 云端 API Key。本地模式 `LLAMA_API_KEY=sk-1234` 是 dummy token，不能用于云端。必须通过 `secret.local.conf` 或环境变量显式设置。未设置且路由触发时，SmartRouter 记录 ERROR 并强制 local |
| `PROXY_CLOUD_MODEL` | `deepseek-v4-flash` | str | ✅ | 云端模型标识符 |
| `PROXY_ROUTE_CLOUD_CONCURRENT` | `2` | int | ✅ | 云端请求并发数 |
| `PROXY_ROUTE_MEMORY_PCT` | `90` | int | ✅ | 内存压力触发路由的 used_pct 阈值 |
| `PROXY_ROUTE_FALLBACK_ENABLED` | `true` | bool | ✅ | 云端失败时是否回退到本地 |
| `PROXY_ROUTE_MAX_CLOUD_FAILS` | `3` | int | ✅ | 连续云端失败次数上限，超过后进入冷却期 |
| `PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS` | `1800` | int | ✅ | 连续失败后冷却时间（秒），冷却期内强制本地；到期后自动恢复尝试云端 |
| `PROXY_CLOUD_PRICE_INPUT` | `0.5` | float | ✅ | 云端 API 输入价格（¥/M tokens），用于成本估算 |
| `PROXY_CLOUD_PRICE_OUTPUT` | `1.5` | float | ✅ | 云端 API 输出价格（¥/M tokens），用于成本估算 |
| `PROXY_ROUTE_SENSITIVE_PATTERNS` | `""` | str | ✅ | 敏感文件路径正则列表（逗号分隔），命中时请求强制本地。例：`.env,.secret,credentials,id_rsa` |
| `PROXY_ROUTE_DAILY_BUDGET` | `0` (无限制) | float | ✅ | 每日云端 API 费用上限（¥）。超过后当日所有请求强制本地。0 = 不限制。Phase 2 实现 |
| `PROXY_ROUTE_PROFILE` | `""` (不使用) | str | ❌ | 路由配置 Profile 名称：`safe` / `balanced` / `cost-aware`。设置后自动应用预设默认值（仅填充未显式设置的环境变量）。注意：Profile 在读取所有环境变量后一次性应用，SIGHUP 后需重新加载 |

### 3.2 配置示例

```bash
# configs/rapid-mlx-35b-opt.conf — 追加路由配置

# === 智能路由（可选，默认关闭） ===
# 当上下文超过阈值时自动路由到云端 API
PROXY_ROUTE_ENABLED=true
PROXY_ROUTE_THRESHOLD_CHARS=90000

# 云端 API 配置（需配合 configs/secret.local.conf 中的 LLAMA_API_KEY）
PROXY_CLOUD_BASE_URL=https://api.deepseek.com/v1
PROXY_CLOUD_MODEL=deepseek-v4-flash

# 云端并发控制（建议 2，避免触发 API rate limit）
PROXY_ROUTE_CLOUD_CONCURRENT=2

# 回退策略
PROXY_ROUTE_FALLBACK_ENABLED=true
PROXY_ROUTE_MAX_CLOUD_FAILS=3
PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS=1800

# 成本估算参数
PROXY_CLOUD_PRICE_INPUT=0.5
PROXY_CLOUD_PRICE_OUTPUT=1.5

# 敏感路径保护（可选，逗号分隔的正则模式）
# PROXY_ROUTE_SENSITIVE_PATTERNS=".env,.secret,credentials,id_rsa"
```

### 3.3 SIGHUP 热重载支持

所有 `PROXY_ROUTE_*` 变量加入 `_RELOAD_SPEC`：

```python
# proxy_state.py _RELOAD_SPEC 追加
("PROXY_ROUTE_ENABLED", "PROXY_ROUTE_ENABLED", "bool", "false", "false"),
("PROXY_ROUTE_THRESHOLD_CHARS", "PROXY_ROUTE_THRESHOLD_CHARS", "int", "90000", "90000"),
("PROXY_CLOUD_BASE_URL", "PROXY_CLOUD_BASE_URL", "str", "https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
("PROXY_CLOUD_MODEL", "PROXY_CLOUD_MODEL", "str", "deepseek-v4-flash", "deepseek-v4-flash"),
("PROXY_ROUTE_CLOUD_CONCURRENT", "PROXY_ROUTE_CLOUD_CONCURRENT", "int", "2", "2"),
("PROXY_ROUTE_MEMORY_PCT", "PROXY_ROUTE_MEMORY_PCT", "int", "90", "90"),
("PROXY_ROUTE_FALLBACK_ENABLED", "PROXY_ROUTE_FALLBACK_ENABLED", "bool", "true", "true"),
("PROXY_ROUTE_MAX_CLOUD_FAILS", "PROXY_ROUTE_MAX_CLOUD_FAILS", "int", "3", "3"),
("PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", "int", "1800", "1800"),
("PROXY_CLOUD_PRICE_INPUT", "PROXY_CLOUD_PRICE_INPUT", "float", "0.5", "0.5"),
("PROXY_CLOUD_PRICE_OUTPUT", "PROXY_CLOUD_PRICE_OUTPUT", "float", "1.5", "1.5"),
("PROXY_ROUTE_SENSITIVE_PATTERNS", "PROXY_ROUTE_SENSITIVE_PATTERNS", "str", "", ""),
("PROXY_ROUTE_DAILY_BUDGET", "PROXY_ROUTE_DAILY_BUDGET", "float", "0", "0"),
("PROXY_ROUTE_PROFILE", "PROXY_ROUTE_PROFILE", "str", "", ""),
```

注意：`PROXY_ROUTE_CLOUD_CONCURRENT` 热重载时需要重建 `_cloud_lock` Semaphore（与当前 `PROXY_MAX_CONCURRENT` 的 Semaphore 重建逻辑一致）。

### 3.4 配置 Profile 系统（Phase 2）

为减少用户逐参数调优负担，提供 3 个预设 Profile。用户设置 `PROXY_ROUTE_PROFILE` 即可一键切换，高级用户仍可逐参数覆盖：

```python
# proxy_state.py
_ROUTE_PROFILES = {
    "safe": {
        # 保守：只在本地明显吃力时路由
        "PROXY_ROUTE_THRESHOLD_CHARS": "90000",
        "PROXY_CLOUD_MODEL": "deepseek-v4-flash",
        "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS": "1800",
        "PROXY_ROUTE_MEMORY_PCT": "90",
        "description": "保守模式：SATURATION 阶段触发，flash 模型，30min 冷却"
    },
    "balanced": {
        # 均衡：更早路由，降低本地压力
        "PROXY_ROUTE_THRESHOLD_CHARS": "60000",
        "PROXY_CLOUD_MODEL": "deepseek-v4-flash",
        "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS": "900",
        "PROXY_ROUTE_MEMORY_PCT": "85",
        "description": "均衡模式：EXPANSION 晚期触发，flash 模型，15min 冷却"
    },
    "cost-aware": {
        # 节省：尽量本地，成本优先
        "PROXY_ROUTE_THRESHOLD_CHARS": "120000",
        "PROXY_CLOUD_MODEL": "deepseek-v4-flash",
        "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS": "3600",
        "PROXY_ROUTE_MEMORY_PCT": "95",
        "description": "成本优先：OOM_DANGER 附近才路由，flash 模型，60min 冷却"
    },
}
```

**加载逻辑**（在读取所有环境变量之后、使用这些值之前执行；Profile 仅定义默认值，优先级低于显式环境变量）：

```python
PROXY_ROUTE_PROFILE = os.environ.get("PROXY_ROUTE_PROFILE", "")

if PROXY_ROUTE_PROFILE and PROXY_ROUTE_PROFILE in _ROUTE_PROFILES:
    profile = _ROUTE_PROFILES[PROXY_ROUTE_PROFILE]
    for key, val in profile.items():
        if key != "description" and key not in os.environ:
            # 仅当用户未显式设置环境变量时才使用 profile 默认值
            os.environ[key] = val
```

**配置示例**：

```bash
# 一键安全模式
PROXY_ROUTE_PROFILE=safe

# 或手动覆盖 profile 中的单项
PROXY_ROUTE_PROFILE=safe
PROXY_ROUTE_THRESHOLD_CHARS=70000  # 覆盖 safe profile 的 90000
```

---

## 4. 路由决策引擎设计

### 4.1 决策矩阵

| 优先级 | 条件 | 决策 | 原因标签 |
|--------|------|------|---------|
| 0 | `PROXY_ROUTE_ENABLED == false` | **local** | disabled |
| 0.5 | 模型 ID 路由偏好（调整阈值参数） | **偏好影响** | model_preference (调整 threshold/memory 后继续) |
| 0.6 | `ctx._route_header_override ∈ {"local", "cloud"}` | **local/cloud** | header_override（单次调试，不写入 Session 状态） |
| 1 | session_id 在 `_cloud_fail_count` 冷却期内 | **local** | cloud_cooldown_active |
| 2 | `_SESSION_ROUTE_MAP[session_id] == "cloud"` | **cloud** | session_already_cloud |
| 3 | `_SESSION_ROUTE_MAP[session_id] == "local_forced"` | **local** | session_force_local |
| 4 | 命中敏感路径且上下文超载 | **reject** | sensitive_path_over_limit |
| 5 | 命中敏感路径 | **local** | sensitive_path |
| 6 | `used_pct > effective_memory_pct AND available_gb < 5` | **cloud** | memory_pressure (阈值受偏好影响) |
| 7 | `total_chars > effective_threshold` | **cloud** | chars_exceed_threshold (阈值受偏好影响) |
| 8 | `stage ∈ {saturation, oom_danger, pre_trunc}` | **cloud** | lifecycle_stage |
| 9 | 默认 | **local** | under_threshold |

> **修正说明**（v2.8）：优先级 0.5 从「Header 覆盖」改为「模型 ID 路由偏好」——偏好**调整阈值参数**而非强制路由。Header 覆盖降至优先级 0.6（调试工具）。阈值调整（effective_threshold / effective_memory_pct）在 Priority 6/7 中生效，但 Priority 1-5（冷却期、Session 状态、敏感路径）不受影响。

### 4.2 路由决策示例

以下用具体 chars / stage / memory 组合说明最终路由结果：

| # | total_chars | stage | used_pct | avail_gb | session 已路由? | cloud 连续失败? | 决策 | 原因 |
|---|------------|-------|----------|----------|----------------|----------------|------|------|
| 1 | 5,000 | init | 45% | 25 | 否 | 0 | **local** | under_threshold |
| 2 | 127,843 | saturation | 60% | 18 | 否 | 0 | **cloud** | chars_exceed_threshold |
| 3 | 45,000 | expansion | 70% | 14 | 否 | 0 | **local** | under_threshold（未达 90K 阈值） |
| 4 | 45,000 | expansion | 94% | 3 | 否 | 0 | **cloud** | memory_pressure（内存优先于 chars） |
| 5 | 200,000 | oom_danger | 75% | 10 | 是 (cloud) | 0 | **cloud** | session_already_cloud（优先级 2） |
| 6 | 200,000 | oom_danger | 75% | 10 | 是 (local_forced) | 3 | **local** | session_force_local（优先级 3，冷却期内） |
| 7 | 15,000 | init | 50% | 22 | 否 | 0 | **local** | under_threshold（优先级 9 默认） |
| 8 | 15,000 | init | 50% | 22 | 是 (cloud) | 0 | **cloud** | session_already_cloud（优先级 2） |
| 9 | 50,000 | expansion | 50% | 20 | 否 | 3 (冷却中) | **local** | cloud_cooldown_active（优先级 1） |

> **关键规则**：Session 一致性 > 内存安全 > 上下文大小 > 默认本地。

### 4.3 状态管理

```python
# proxy_state.py 新增共享状态（以下 dict 的读写均需持有 _state_lock）

# Session 路由状态: session_id → "local" | "cloud" | "local_forced"
_SESSION_ROUTE_MAP = {}

# local_forced 来源: session_id → "cloud_failures" | "user_manual"
# - cloud_failures: 受冷却期管理，到期自动恢复
# - user_manual: 不受冷却期管理，永久本地直到 ./manage.sh route-force-cloud
_SESSION_ROUTE_FORCE_SOURCE = {}

# 云端失败计数: session_id → int
_cloud_fail_count = {}

# 冷却期开始时间: session_id → float (time.monotonic() timestamp)
_cloud_cooldown_start = {}

# 路由通知标记: f"_route_notified_{session_id}" → True
# （动态属性，无需预定义）

# 清理策略: 所有路由相关 session 状态 dict 使用统一 LRU 上限（1000 条）
# 在 _classify_lifecycle_stage 中附带清理（与 _SESSION_REQUEST_COUNT 同位置）：
#   if len(_SESSION_ROUTE_MAP) > 1000: 清理最旧的条目
# 清理时同时从 _SESSION_ROUTE_MAP / _SESSION_ROUTE_FORCE_SOURCE /
# _cloud_fail_count / _cloud_cooldown_start 中删除对应 session_id
# 动态属性 _route_notified_{session_id} 一并清理
```

**并发安全**：以上 4 个 dict 的读写均需持有 `_state_lock`（现有锁，已保护 `_SESSION_REQUEST_COUNT`、`_LOOP_SESSION_STATE` 等）。在 `_routing_decision()` 和 `_record_cloud_failure()` 中通过 `with _ps._state_lock:` 保护。

### 4.4 决策伪代码（含冷却期定时器 + 模型偏好阈值调整）

```python
def _routing_decision(ctx):
    """Determine route target for this request. Priority 0-9 per §4.1.
    
    Priority 0.5 (model preference) adjusts effective thresholds rather than
    forcing a route direction — safety (OOM protection) always overrides preference.
    """
    
    session_id = ctx.session_id
    
    # Priority 0: routing disabled
    if not PROXY_ROUTE_ENABLED:
        return "local", "disabled"
    
    # === Priority 0.5: Model ID route preference (adjust thresholds, do NOT force route) ===
    requested_model = ctx.body.get("model", "")
    pref = _ps.MODEL_ROUTE_PREFERENCES.get(requested_model, {})
    
    # Apply preference-based threshold adjustments
    effective_threshold = int(PROXY_ROUTE_THRESHOLD_CHARS * pref.get("threshold_factor", 1.0))
    effective_memory_pct = PROXY_ROUTE_MEMORY_PCT + pref.get("memory_bias", 0)
    
    # Record cloud model selection for FormatConverter
    ctx._route_cloud_model = pref.get("cloud_model", PROXY_CLOUD_MODEL)
    
    # Classify agent tier (for _resolve_cloud_model downstream)
    ctx._agent_model_tier = _classify_tier(requested_model)
    route_bias = pref.get("route_bias", "auto")
    
    # NOTE: route_bias is informational only — does NOT force a route direction.
    # effective_threshold / effective_memory_pct are used in Priority 6/7 below.
    
    # === Priority 0.6: X-Proxy-Route-To header (single-request debug override) ===
    if ctx._route_header_override in ("local", "cloud"):
        return ctx._route_header_override, "header_override"
    
    # Priority 1: cloud cooldown active
    if session_id and session_id in _cloud_cooldown_start:
        elapsed = time.monotonic() - _cloud_cooldown_start[session_id]
        if elapsed < PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS:
            remaining = int(PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS - elapsed)
            _SESSION_ROUTE_MAP[session_id] = "local_forced"
            return "local", f"cloud_cooldown_active({remaining}s remaining)"
        else:
            # Cooldown expired — reset
            _cloud_fail_count.pop(session_id, None)
            _cloud_cooldown_start.pop(session_id, None)
            _SESSION_ROUTE_MAP.pop(session_id, None)
            _SESSION_ROUTE_FORCE_SOURCE.pop(session_id, None)
            log(f"  -> Cloud cooldown expired for session {session_id}")
    
    # Priority 2/3: Session-level state
    if session_id:
        session_route = _SESSION_ROUTE_MAP.get(session_id)
        if session_route == "cloud":
            return "cloud", "session_already_cloud"
        if session_route == "local_forced":
            return "local", "session_force_local"
    
    # Priority 4/5: Sensitive path check
    if _is_sensitive_request(ctx):
        total_chars = ctx.stage_config.get("total_chars", 0) if ctx.stage_config else 0
        if total_chars > PROXY_OOM_SAFE_CHARS:
            raise _RouteRejectException("Sensitive path over context limit")
        return "local", "sensitive_path"
    
    # === Priority 6: Memory pressure (uses effective_memory_pct from model preference) ===
    mem = _get_system_memory() if _get_system_memory else {}
    used_pct = float(mem.get("used_pct", 0))
    available_gb = float(mem.get("available_gb", 48))
    if used_pct > effective_memory_pct and available_gb < 5:
        if session_id:
            _SESSION_ROUTE_MAP[session_id] = "cloud"
        return "cloud", f"memory_pressure(used={used_pct}% > {effective_memory_pct}%, avail={available_gb}GB)"
    
    # === Priority 7: Context size (uses effective_threshold from model preference) ===
    total_chars = ctx.stage_config.get("total_chars", 0) if ctx.stage_config else 0
    if total_chars > effective_threshold:
        if session_id:
            _SESSION_ROUTE_MAP[session_id] = "cloud"
        return "cloud", f"chars_exceed_threshold({total_chars} > {effective_threshold})"
    
    # Priority 8: Lifecycle stage (belt-and-suspenders)
    stage = ctx.stage_config.get("stage", "init") if ctx.stage_config else "init"
    if stage in ("saturation", "oom_danger", "pre_trunc"):
        if session_id:
            _SESSION_ROUTE_MAP[session_id] = "cloud"
        return "cloud", f"lifecycle_stage({stage})"
    
    # Priority 9: Default local
    if session_id and _SESSION_ROUTE_MAP.get(session_id) != "cloud":
        _SESSION_ROUTE_MAP[session_id] = "local"
    return "local", "under_threshold"


# === Helper: classify agent model tier from model ID ===
def _classify_tier(model_id: str) -> str:
    """Extract tier from model ID: 'opus', 'sonnet', 'haiku'.
    
    Default: 'sonnet' (for unknown/unrecognized model IDs).
    """
    model_lower = model_id.lower()
    if "opus" in model_lower:
        return "opus"
    elif "haiku" in model_lower:
        return "haiku"
    return "sonnet"


# === Helper: resolve cloud model from agent tier ===
def _resolve_cloud_model(tier: str) -> str:
    """Select cloud model based on agent tier.
    
    opus → deepseek-v4-pro (high quality)
    sonnet / haiku / unknown → PROXY_CLOUD_MODEL (default: deepseek-v4-flash)
    """
    if tier == "opus":
        return "deepseek-v4-pro"
    return PROXY_CLOUD_MODEL
```

---

## 5. 可观测性设计

### 5.1 日志输出

```
# 路由决策
-> [smart_router] cloud (chars_exceed_threshold: 127843 > 90000)
-> [smart_router] local (under_threshold: 45231 chars, stage=growth)
-> [smart_router] cloud (session_already_cloud)
-> [smart_router] local (cloud_failures_exhausted: 3 consecutive failures)

# BackendDispatcher 双后端
-> Forwarding to https://api.deepseek.com/v1/chat/completions (cloud)
-> Forwarding to http://127.0.0.1:8081/v1/chat/completions (local)
<- backend status: 200 (cloud, deepseek-v4-flash)

# 云端回退
-> Cloud API failed (503), falling back to local
-> Fallback to local backend (fallback_count=1)
<- backend status: 200 (local, fallback)

# Pipeline metrics
-> [smart_router] completed in 0.2ms
-> [backend_dispatcher] completed in 2341.5ms
-> Pipeline summary: 20 executed, 3 skipped, slowest=backend_dispatcher(2341.5ms)
```

### 5.2 /status 页面扩展

**P1#6 新增路由状态字段**：

```
┌─────────────────────────────────────────┐
│ 🔀 智能路由                              │
├─────────────────────────────────────────┤
│ 路由状态:     ✅ 已启用                   │
│ 当前 Session: abc12345                   │
│ Session 路由:  ☁️ Cloud（P1#6 新增）      │
│ 当前目标:     ☁️ Cloud (deepseek-v4-flash) │
│ 实际后端:     deepseek-v4-flash（P1#6）    │
│ 云端模型:     deepseek-v4-flash（P1#6）    │
│ 切换原因:     chars_exceed_threshold      │
│ 估算成本:     ¥0.34 / ¥1.27 累计（P1#6）  │
│                                         │
│ 📊 统计 (自启动以来)                      │
│ 本地请求:     187 (87.4%)                │
│ 云端请求:     27  (12.6%)                │
│ 云端回退:     0                          │
│ 连续失败:     0                          │
│                                         │
│ 💰 估算成本                              │
│ 本次会话:     ¥0.34 (6 次 cloud, 已结算)   │
│ 累计:        ¥1.27 (27 次 cloud, 含 1 次 pending 估算) │
│ 云端模型:     deepseek-v4-flash          │
│                                         │
│ ⚙️ 配置                                  │
│ 阈值:        90,000 chars               │
│ 内存触发:    90% used_pct               │
│ Profile:     safe (默认)                 │
│                                         │
│ 🔍 Active Sessions（P1#6 新增）          │
│ ┌──────────┬────────┬─────────┬───────┐ │
│ │ Session   │ Target │ Reason  │ Cost  │ │
│ ├──────────┼────────┼─────────┼───────┤ │
│ │ abc12345  │ cloud  │ chars   │ ¥0.34 │ │
│ │ def67890  │ local  │ under   │ ¥0.00 │ │
│ └──────────┴────────┴─────────┴───────┘ │
│                                         │
└─────────────────────────────────────────┘

> 成本说明: 每次请求完成后更新真实成本（从 backend response.usage.completion_tokens 计算）；pending 请求仅显示 input 估算。累计 = 真实成本（已完成）+ 估算（pending）。
```

**Active Sessions 表字段**:

| 字段 | 来源 | 说明 |
|------|------|------|
| Session | `_SESSION_ROUTE_MAP` key | 活跃 session ID |
| Target | `_SESSION_ROUTE_MAP[session_id]` | 当前路由目标 |
| Reason | `_SESSION_ROUTE_REASON.get(session_id)` | 切换到当前目标的原因 |
| Cost | 累计 `_route_actual_cost` | 该 session 所有 cloud 请求的成本总和 |

### 5.3 Metrics JSONL 扩展

**P1#7 新增字段**（`proxy_metrics.jsonl` 每行记录新增路由相关字段）：

```json
{
  "schema_version": "v2",
  "request_id": "req_a1b2c3d4",
  "session_id": "abc12345",
  "requested_model": "claude-sonnet-4-6",
  "pipeline": {
    "smart_router": {
      "target": "cloud",
      "reason": "chars_exceed_threshold",
      "chars": 127843,
      "threshold": 90000,
      "stage": "saturation",
      "agent_tier": "sonnet",
      "route_bias": "auto",
      "prefer_local": false
    },
    "format_converter": {
      "actual_model": "deepseek-v4-flash",
      "thinking_disabled": false
    },
    "backend_dispatcher": {
      "backend_status": 200,
      "stream": 1,
      "route_target": "cloud",
      "route_cloud_model": "deepseek-v4-flash",
      "route_fallback": false,
      "route_reason": "chars_exceed_threshold",
      "route_cost_estimate": 0.011,
      "estimated_cost_input": 0.008,
      "estimated_cost_output": 0.003,
      "estimated_cost_total": 0.011,
      "actual_cost_total": 0.011
    }
  }
}
```

**新增字段说明**:

| 字段路径 | 类型 | 来源 | 说明 |
|---------|------|------|------|
| `requested_model` | `string` | Agent 请求的 `body.model` | `claude-sonnet-4-6` / `claude-opus-4-7` / 其他 |
| `pipeline.smart_router.agent_tier` | `string` | `ctx._agent_model_tier` | `opus` / `sonnet` / `haiku` |
| `pipeline.smart_router.route_bias` | `string` | `MODEL_ROUTE_PREFERENCES[model].route_bias` | `auto` / `prefer_cloud` / `prefer_local` |
| `pipeline.format_converter.actual_model` | `string` | 实际发送到后端的 `openai_body.model` | `deepseek-v4-flash` / `mlx-community/Qwen3.6-35B-A3B-4bit` |
| `pipeline.backend_dispatcher.route_reason` | `string` | `ctx._route_reason` | 与 smart_router.reason 一致（冗余但便于单行分析） |
| `pipeline.backend_dispatcher.route_cost_estimate` | `float` | 请求前估算的成本 | 用于 pending 请求的成本预览 |

### 5.4 敏感路径匹配实现

PRD NFR-6.3 要求命中敏感路径时强制走本地。此功能为**尽力而为**，不保证识别所有敏感数据：

```python
def _is_sensitive_request(ctx: PipelineContext) -> bool:
    """Check if request touches sensitive file paths (best-effort)."""
    patterns_str = _ps.PROXY_ROUTE_SENSITIVE_PATTERNS
    if not patterns_str:
        return False

    import re
    patterns = [p.strip() for p in patterns_str.split(",") if p.strip()]
    if not patterns:
        return False

    sensitive_re = re.compile("|".join(patterns), re.IGNORECASE)

    # Only scan tool_use parameters (file_path / path fields)
    # Does NOT scan free-text user/assistant message content
    for msg in ctx.messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_use":
                continue
            inp = block.get("input", {})
            if not isinstance(inp, dict):
                continue
            file_path = inp.get("file_path") or inp.get("path") or ""
            if file_path and sensitive_re.search(file_path):
                return True

    return False
```

**SmartRouter 集成**：

```python
# SmartRouter.process() 中的敏感路径检查（在 chars 和 memory 检查之前）
if _is_sensitive_request(ctx):
    if ctx.stage_config and ctx.stage_config.get("total_chars", 0) > _ps.PROXY_OOM_SAFE_CHARS:
        # Sensitive + over limit: reject with guidance
        log(f"  -> [PRIVACY] Sensitive path detected, context over limit — rejecting")
        raise _RouteRejectException(
            "Sensitive file path detected and context exceeds local limit. "
            "Use `./manage.sh route-force-local <session_id>` to force local, then /compact."
        )
    # Sensitive + under limit: force local (OK)
    log(f"  -> [PRIVACY] Sensitive path detected — forcing local")
    return "local", "sensitive_path"
```

**设计约束**（已在 PRD NFR-6.3 中声明）：
- 仅检查 `tool_use` 参数中的 `file_path`/`path` 字段
- 不扫描自由文本内容（无法识别粘贴到对话中的密钥）
- 命中敏感路径且上下文超载时，返回结构化错误而非直接硬跑导致 OOM

---

## 6. 实施阶段计划

### Phase 1: MVP（第 1-2 周）

**目标**: 基于上下文字符数的基本路由，可手动开启。RouteNotification 仅日志输出，完整消息注入推迟到 Phase 2。

| 任务 | 预估 | 文件 |
|------|------|------|
| 新增 `PROXY_ROUTE_*` 14 个常量 + `_cloud_lock` + 4 个 dict + `_RELOAD_SPEC` 条目 | 2h | proxy_state.py |
| 新增 `SmartRouter` Stage（150 行，含 10 级决策矩阵 + 偏好阈值调整） | 3h | pipeline.py |
| 改造 `BackendDispatcher` 双后端支持（base_url/api_key/lock 选择，不含回退） | 2h | pipeline.py |
| 改造 `/v1/models`（稳定 Anthropic 别名 + `_build_models_response` + 移除 MODEL_NAME） | 1h | anthropic_proxy.py |
| 4 个 Stage 添加 `should_run` cloud skip（CacheAligner/ContentCompressor/ContextTruncator/OOMSafetyFIFO） | 1h | pipeline.py |
| `HighDropRatioNotice` 改为 `ConditionalStage` | 0.5h | pipeline.py |
| `_handle_messages` 集成 SmartRouter + RouteNotification 简化版（仅日志） + 双锁传递 | 0.5h | anthropic_proxy.py |
| `RequestParser` 解析 `X-Proxy-Route-To` header + `agent_model_tier` 提取 | 0.5h | pipeline.py |
| `FormatConverter` model 字段适配（cloud 用 `_route_cloud_model`，local 用 `MODEL_NAME`） | 0.5h | pipeline.py |
| 配置示例追加到 rapid-mlx-35b-opt.conf | 0.5h | configs/ |
| 单元测试 SmartRouter（15 cases：10 级优先级 + 场景边界） | 3h | test/unit/ |
| 单元测试 BackendDispatcher 双模式（8 cases） | 2h | test/unit/ |
| 单元测试 `/v1/models` 改造（5 cases） | 1h | test/unit/ |
| 单元测试 `MODEL_ROUTE_PREFERENCES` 阈值调整（4 cases） | 1h | test/unit/ |
| 集成测试路由链（3 cases：正常路由/禁用路由/mock 云端 503） | 2h | test/integration/ |
| 回归测试：所有现有测试在 `PROXY_ROUTE_ENABLED=false` 和 `true` 下通过 | 1h | test/ |
| **合计** | **~22h** | |

**Phase 1 验收清单**：
- `PROXY_ROUTE_ENABLED=true` + context > 90K chars → 请求路由到云端
- `PROXY_ROUTE_ENABLED=false` → 行为与当前版本一致
- 所有现有单元测试在**两种配置下**均通过（`PROXY_ROUTE_ENABLED=false` 和 `true`）
- 新增 SmartRouter 15 cases + BackendDispatcher 8 cases
- 新增集成测试 mock backend（模拟云端 503 回退场景）
- HighDropRatioNotice 回归测试（验证改为 ConditionalStage 后本地路径行为不变）

**Phase 1 量化验收指标**：

| 指标 | 目标 | 测量方式 |
|------|------|---------|
| 路由触发准确率 | ≥ 95%（不过度路由、不漏路由） | `proxy_metrics.jsonl` smart_router 统计 |
| 云端请求占比 | 10–20%（在预期范围内） | metrics route_target=cloud 占比 |
| 新增 500 错误 | 0（路由不引入新错误） | metrics status=500 计数 |
| 单元测试 | SmartRouter ≥ 15 cases（覆盖决策矩阵 10 级 + 8 个场景示例）, BackendDispatcher ≥ 8 cases | `test/unit/` |
| 集成测试 | 路由链 ≥ 3 cases | `test/integration/` |

### Phase 2: 容错增强（第 3-4 周）

**目标**: 云端回退、内存触发、Session 一致性、成本控制、用户通知。

| 任务 | 预估 |
|------|------|
| 云端失败回退逻辑 (BackendDispatcher 内) | 3h |
| 连续失败 Session 降级 + 冷却期定时器 | 1h |
| 内存压力触发路由 | 1h |
| RouteNotification 完整实现（首次路由 + 紧急回退两种模板，消息流注入） | 1h |
| /status 路由面板 | 3h |
| /metrics 路由统计 | 1h |
| `PROXY_ROUTE_DAILY_BUDGET` 实现（超预算当日强制本地） | 1h |
| `manage.sh` 路由管理命令（`route-force-local`/`route-force-cloud`） | 0.5h |
| `admin_server.py` 新增 `POST /admin/route/force-local` 和 `/force-cloud` | 0.5h |
| 集成测试回退链 (3 cases) | 2h |
| **合计** | **~14h** |

### Phase 3: 体验优化（第 5-6 周）

**目标**: 成本追踪完善、A/B 验证、体验打磨。

| 任务 | 预估 |
|------|------|
| 成本估算完善 (pre-request input 估算 + post-request 真实 usage 计算，流式/非流式) | 2h |
| X-* Response Header 注入（`X-Actual-Model`/`X-Route-Target`/`X-Route-Reason`，含 SSE 时序处理） | 0.5h |
| /status 成本趋势展示 + daily budget 状态 | 2h |
| A/B 测试：路由 vs 纯本地 (5 cases) | 4h |
| 用户文档更新（CLAUDE.md / AGENTS.md / ../06-reference-metrics/TROUBLESHOOTING.md） | 2h |
| **合计** | **~10.5h** |

### 总工作量

| Phase | 开发 | 测试 | 文档 | 合计 |
|-------|------|------|------|------|
| Phase 1 | 12h | 10h | 1h | **~23h** |
| Phase 2 | 11h | 2h | 1h | **~14h** |
| Phase 3 | 4h | 4h | 2h | **~10h** |
| **总计** | **27h** | **16h** | **4h** | **~47h** |

---

## 7. 成功指标

### 7.1 功能指标

| 指标 | 当前值 | 目标值 | 测量方式 |
|------|--------|--------|---------|
| 路由触发率 | N/A | 10-15% (覆盖 SATURATION+ 区间) | metrics.JSONL route_target=cloud 占比 |
| OOM 事件 | 偶发 (DEF-005) | **0** | llama-server.log grep "Insufficient Memory" |
| 云端回退率 | N/A | < 5% | metrics.JSONL route_fallback=true 占比 |
| Session 切换 ping-pong | N/A | **0** | 日志分析：同一 session_id 出现 local→cloud→local |

### 7.2 体验指标

| 指标 | 当前值 | 目标值 | 测量方式 |
|------|--------|--------|---------|
| 长上下文 TTFT (P95) | 28s+ | **< 5s** (云端) | metrics.JSONL backend_dispatcher elapsed |
| 循环注入率 (长上下文) | 54% (SATURATION) | **< 2%** | metrics.JSONL quality_flags loop_injected |
| high_drop_ratio | 8.7% (修复前) / 0% (修复后) | **0%** (维持) | metrics.JSONL quality_flags |

### 7.3 成本指标

| 指标 | 目标值 | 测量方式 |
|------|--------|---------|
| 月度云端请求量 | < 500 (基于 40 请求/天 × 22 天 × 13% 路由率) | /status 累计计数 |
| 月度 API 费用 | < ¥50 | /status estimated_cost |
| 单次云端请求成本 | < ¥0.15 (flash 模型) | metrics estimated_cost |

---

## 8. 风险与缓解

| # | 风险 | 概率 | 影响 | 缓解 |
|---|------|------|------|------|
| R1 | Cloud API 不可用导致请求失败 | 中 | 高 | Phase 2 实现自动回退 + Session 连续失败降级 |
| R2 | API 费用超出预期 | 中 | 中 | 默认 flash 模型 (¥0.5/M in)；/status 显示实时成本；可配置 daily budget cap (Phase 2) |
| R3 | 云端模型输出风格与本地差异大 | 低 | 低 | 只在 Session 边界切换 (新 Session 回本地)；注入切换 notice |
| R4 | BackendDispatcher 回退死锁 | 低 | 中 | BackendDispatcher 内部处理 fallback；cloud_lock 和 llama_lock 不会同时持有（先退出 with cloud_lock 再进入 with llama_lock）。Emergency truncation 在 BackendDispatcher 内部调用，不经过 ContextTruncator |
| R5 | 并发控制不当导致 cloud rate limit | 低 | 中 | PROXY_ROUTE_CLOUD_CONCURRENT 独立控制，默认 2 |
| R6 | API Key 泄露到日志 | 低 | 高 | 复用 _mask_sensitive()；secret.local.conf 已是 git-ignored |
| R7 | 用户不知道路由功能存在 | 中 | 中 | /status 页面提示 "路由未启用"；首次 `./manage.sh start` 时输出提示；文档引导开启。若用户不知道此功能，长会话问题不会被解决，影响应为「中」 |
| R8 | 内存压力误判触发不必要的路由 | 低 | 中 | 双重条件 (used_pct > 90% AND available_gb < 5)；可配置阈值 |
| R9 | Cloud API Response 格式差异 | 低 | 中 | DeepSeek API 和本地 llama-server/rapid-mlx 的 `usage` 字段结构、`finish_reason` 枚举值、streaming chunk 结构可能存在差异。FormatConverter 和 BackendDispatcher 的 response 处理使用同一个接口（`handler._handle_streaming_response` / `_handle_non_streaming_response`），差异由现有 Anthropic↔OpenAI 转换层统一处理 |

### 8.1 关键失败场景与恢复路径

| 场景 | 触发条件 | 系统行为 | 用户体验 | 恢复方式 |
|------|---------|---------|---------|---------|
| **Cloud API 不可用** | HTTP 5xx / 网络超时 | 自动回退到本地 + 触发激进 OOM 截断；注入 `[System: Cloud API unavailable, fallback to local...]` 通知 | 请求延迟增加（云端超时 + 本地处理），可能截断上下文 | 连续失败 3 次后进入冷却期（30 分钟），到期自动重试云端 |
| **大上下文回退到本地** | 云端失败 + 本地上下文超 PROXY_OOM_SAFE_CHARS | **EMERGENCY TRUNCATION**：以 PROXY_OOM_SAFE_CHARS 的 50% 为截断目标（默认 ~100K chars），或保留最近 3 轮 assistant 完整上下文（取较小值）。注入 `[System: Cloud API unavailable, emergency fallback to local. Context severely truncated (~{n} chars). Consider /compact or retry when cloud recovers.]` 通知 | 上下文大幅丢失（可能 >80%），模型可能「失忆」。/status 显示紧急截断状态 | 用户执行 `/compact` 或等待云端恢复后新开请求自动重试 |
| **Cloud + Local 双故障** | 两边都不可用（云端 down + 本地后端未启动或 OOM 中） | 返回 503 + Retry-After header + 结构化错误 JSON `{"error":{"type":"backend_unavailable","message":"Both local and cloud backends are unavailable"}}`。不阻塞请求队列。日志 CRITICAL | 请求失败，需用户手动检查 | 检查本地后端进程 (`./manage.sh status`) + 网络连接后重试 |
| **API Key 失效** | 云端返回 401 | 视为云端失败，回退到本地 + 日志 WARN：`[PRIVACY] Cloud API key invalid` | 切换到本地（可能 OOM） | 更新 `secret.local.conf` 中的 `LLAMA_API_KEY` 后 `./manage.sh reload` |
| **Cloud Rate Limit** | 云端返回 429 | 自动回退到本地；云端锁降级 | 请求走本地 | 降低 `PROXY_ROUTE_CLOUD_CONCURRENT` 或等待 rate limit 窗口过期 |
| **冷却期后首次请求** | 冷却期满 + 新请求到来 | 恢复尝试云端 1 次；成功则清除 `local_forced` 标记，失败则重新进入冷却期 | 可能再次失败回退 | 自动恢复或手动 `./manage.sh route-force-cloud <session_id>` 提前解除冷却 |

---

## 9. 模型 ID 暴露契约

> 本章节定义代理层向 Agent（Claude Code、OpenCode 等）暴露模型信息的方式，包括模型发现 (`GET /v1/models`)、模型 ID 选择、Response 模型名真实性、以及路由切换时的 Agent 感知机制。
> 
> **设计参考**: Claude Code 的 Gateway Model Discovery (`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`) 和 `modelOverrides` 机制。
> 
> **核心原则**: 代理层是模型发现的单一入口，模型 ID 编码路由偏好，Response 兼容优先。

### 9.1 设计原则

| 原则 | 说明 |
|------|------|
| **Proxy 是模型发现入口** | `GET /v1/models` 返回稳定的 Anthropic 别名列表，Agent 通过此接口感知可用模型 |
| **模型 ID 编码路由偏好** | 不同 ID 暗示不同路由偏好（auto / prefer_cloud / prefer_local），Agent 选模型即选路由策略偏好 |
| **Response 兼容优先** | response 中的 `model` 字段**回显** Agent 请求的 ID（兼容现有生态），通过 `X-Actual-Model` / `X-Route-Target` Header 透传实际信息 |
| **安全优先于偏好** | 模型 ID 偏好是**提示**而非指令，当内存压力或 OOM 风险出现时，SmartRouter 仍可覆盖偏好 |
| **切换可感知** | Session 内路由切换时，Agent 通过 RouteNotification + Response Header 双重感知 |
| **Client 无关性** | `/model` 是 Client-side 机制，代理层无需理解，只需正确响应 `model` 字段变化 |

### 9.2 数据流

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Model ID 暴露数据流（修正后）                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ① Agent 启动 → GET /v1/models                                           │
│     └─ Proxy 返回稳定模型列表（3-4 个 Anthropic 别名，不暴露内部模型名）       │
│                                                                         │
│  ② Agent 选择模型                                                        │
│     └─ 用户配置 / settings.json / 默认                                    │
│     └─ 请求 body.model = "claude-sonnet-4-6"                             │
│                                                                         │
│  ③ RequestParser 提取 tier                                               │
│     └─ ctx._agent_model_tier = "sonnet"  (sonnet/opus/haiku)            │
│                                                                         │
│  ④ SmartRouter 决策                                                      │
│     └─ 根据 route_bias (auto/prefer_cloud/prefer_local) + 安全指标决策     │
│     └─ bias 调整阈值但**不强制**路由方向（安全优先）                        │
│     └─ ctx._route_target = "cloud"|"local"                              │
│                                                                         │
│  ⑤ FormatConverter 映射模型名                                             │
│     └─ openai_body.model = _resolve_cloud_model(tier)  (cloud)          │
│     └─ openai_body.model = MODEL_NAME           (local)                 │
│                                                                         │
│  ⑥ BackendDispatcher 发送响应                                            │
│     └─ response.model 回显 Agent 请求值（兼容优先）                         │
│     └─ X-Actual-Model header → 实际后端模型名                              │
│     └─ X-Route-Target header → cloud/local                                │
│     └─ X-Route-Reason  header → 路由决策原因                                │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

#### 9.2.1 P0#3: 修正 Flash Thinking 判断

当前 `pipeline.py:1526-1528`（FormatConverter）使用 `_ps.MODEL_NAME` 判断是否需要禁用 DeepSeek flash 模型的 thinking：

```python
# ❌ 当前: 用 _ps.MODEL_NAME 判断，路由到 cloud 时 MODEL_NAME 仍是本地模型名
# pipeline.py:1527
if _ps.IS_CLOUD and "flash" in _ps.MODEL_NAME.lower():
    openai_body["thinking"] = {"type": "disabled"}
```

**问题**：路由到 cloud 时，`_ps.MODEL_NAME` 仍然是本地模型名（如 `mlx-community/Qwen3.6-35B-A3B-4bit`），不包含 "flash"，导致 thinking 不会被禁用。DeepSeek flash 模型产生不必要的额外 token 消耗。

**修正**：改用实际发送的 model 名判断：

```python
# ✅ 修正: 用实际发送的 model 名判断
# pipeline.py, FormatConverter 中
actual_model = openai_body.get("model", _ps.MODEL_NAME)
# 或在设置 openai_body["model"] 后:
if (ctx._route_target == "cloud" or _ps.IS_CLOUD) and "flash" in openai_body["model"].lower():
    openai_body["thinking"] = {"type": "disabled"}
```

### 9.3 `/v1/models` 动态响应（稳定别名，不暴露内部模型名）

当前 `MODEL_ALIASES` 是静态列表。改造为**稳定别名列表**，Agent 始终看到固定的 3-4 个 Anthropic 别名。**不暴露 `MODEL_NAME`（内部实现细节）**，不随 reload、不随路由决策变化。

```python
def _build_models_response():
    """构建 Agent 可见的稳定模型列表。
    
    返回固定的 Anthropic 别名，不包含内部 MODEL_NAME。
    "claude-opus-4-7" 仅在路由启用时显示（避免用户选择不可用选项）。
    """
    models = []
    
    # 1. 默认自动路由模型（始终可见）
    models.append({
        "id": "claude-sonnet-4-6",
        "object": "model",
        "created": 1677610602,
        "owned_by": "proxy-router",     # 标识为代理层模型
        "capabilities": {                # 辅助元数据（Agent 可能忽略）
            "route_bias": "auto",
            "description": "Auto-routed: SmartRouter decides local or cloud"
        }
    })
    
    # 2. 偏好云端模型（仅路由启用或 cloud 模式时显示）
    if IS_CLOUD or PROXY_ROUTE_ENABLED:
        models.append({
            "id": "claude-opus-4-7",
            "object": "model",
            "created": 1677610602,
            "owned_by": "proxy-router",
            "capabilities": {
                "route_bias": "prefer_cloud",
                "description": "Prefers cloud (DeepSeek Pro) for quality"
            }
        })
    
    # 3. 偏好本地模型（始终可见）
    models.append({
        "id": "claude-haiku-4-5",
        "object": "model",
        "created": 1677610602,
        "owned_by": "proxy-router",
        "capabilities": {
            "route_bias": "prefer_local",
            "description": "Prefers local model for speed and privacy"
        }
    })
    
    # ❌ 不暴露 MODEL_NAME（内部实现细节）
    # 调试用独立接口: GET /admin/backend-info
    
    return {"object": "list", "data": models}
```

> **关键决策**: Agent 看到的模型列表**不随 reload、不随路由决策变化**。这就是「Agent 不需要手工切换」的保证。
> 后端模型信息通过独立管理接口暴露：`GET /admin/backend-info` → `{"local_model": ..., "cloud_model": ..., "route_enabled": true}`

#### 9.3.1 `MODEL_ALIASES` 改造（含遗留静态列表清理）

当前 `MODEL_ALIASES` 在 `proxy_state.py` 中有两处：

**a) 函数版本（推荐）** — `get_model_aliases()`：

```python
# proxy_state.py — 推荐使用此函数

def get_model_aliases():
    """返回 Agent 可见的稳定模型别名列表。
    
    仅包含 Anthropic 兼容别名，不暴露内部 MODEL_NAME。
    列表内容不随 reload / 路由决策变化。
    """
    aliases = [
        "claude-sonnet-4-6",    # auto 路由（SmartRouter 决策）
        "claude-haiku-4-5",     # 偏好本地
        "default",               # 兼容别名
    ]
    if IS_CLOUD or PROXY_ROUTE_ENABLED:
        aliases.append("claude-opus-4-7")  # 偏好云端（仅路由启用时显示）
    return aliases
```

**b) 遗留静态列表（需清理）** — `proxy_state.py:407-416`：

```python
# === P0: 需移除 MODEL_NAME ===
# proxy_state.py:407-416 — 当前代码保持 MODEL_NAME 在列表中

MODEL_ALIASES = [
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-7",
    "default",
    MODEL_NAME,              # ← P0#1: 移除此行，防止真实后端模型泄露给 Agent
]
```

**迁移步骤**：
1. 从 `MODEL_ALIASES` 静态列表中移除 `MODEL_NAME`（第 415 行）
2. `reload_config.py` 中更新对 `MODEL_ALIASES` 的引用，改为调用 `get_model_aliases()`
3. `anthropic_proxy.py` 中 `/v1/models` handler 改用 `get_model_aliases()`（当前已正确使用）
4. 测试验证：`test/unit/test_proxy_reload.py` 同步更新

#### 9.3.2 SIGHUP 兼容性

```python
# _reload_config() 中
proxy_state.MODEL_ALIASES = get_model_aliases()
anthropic_proxy.MODEL_ALIASES = get_model_aliases()
```

#### 9.3.3 `get_model_aliases()` 缓存优化

为了避免每次 `/v1/models` 请求都重新计算，加入简单缓存，仅在 reload 时刷新：

```python
# proxy_state.py
_MODEL_ALIASES_CACHE = None
_MODEL_ALIASES_CACHE_VERSION = 0

def get_model_aliases():
    global _MODEL_ALIASES_CACHE, _MODEL_ALIASES_CACHE_VERSION
    if _MODEL_ALIASES_CACHE is not None and _MODEL_ALIASES_CACHE_VERSION == _RELOAD_VERSION:
        return _MODEL_ALIASES_CACHE
    # ... 重建逻辑 ...
    _MODEL_ALIASES_CACHE = aliases
    _MODEL_ALIASES_CACHE_VERSION = _RELOAD_VERSION
    return aliases
```

#### 9.3.4 `/v1/models` 稳定性测试要点（P0#2）

当前主路径已正确返回稳定别名（`anthropic_proxy.py:383-386`）。需要追加以下测试锁定行为：

```python
# test/unit/test_model_aliases.py（新增）

class TestModelAliasesStability(unittest.TestCase):
    """验证 /v1/models 始终返回稳定 Anthropic 别名，不泄露内部模型名。"""
    
    def test_never_exposes_internal_model_name(self):
        """MODEL_NAME 不应出现在 Agent 可见的模型列表中。"""
        aliases = get_model_aliases()
        assert "mlx-community" not in aliases
        assert "deepseek" not in aliases
        assert MODEL_NAME not in aliases      # P0#1 保证
    
    def test_stable_across_reload(self):
        """reload 后模型列表不变。"""
        before = get_model_aliases()
        _reload_config()  # 模拟 SIGHUP
        after = get_model_aliases()
        assert before == after
    
    def test_only_anthropic_aliases(self):
        """只返回 Anthropic 兼容别名。"""
        aliases = get_model_aliases()
        assert all(alias in (
            "claude-sonnet-4-6", "claude-haiku-4-5",
            "claude-opus-4-7", "default"
        ) for alias in aliases)
    
    def test_opus_hidden_when_routing_disabled(self):
        """路由禁用时 claude-opus-4-7 不出现。"""
        with patch('proxy_state.PROXY_ROUTE_ENABLED', False):
            aliases = get_model_aliases()
            assert "claude-opus-4-7" not in aliases
```

### 9.4 模型 ID → 路由偏好映射（偏好而非强制）

Agent 选择的 model ID 编码**路由偏好**，SmartRouter 据此**调整阈值**而非强制路由。安全（OOM 保护）始终优先于偏好。

#### 9.4.1 偏好映射表

```
Client 选择的 model ID    路由偏好      SmartRouter 行为
──────────────────────────────────────────────────────────────────────
claude-sonnet-4-6      →  auto           默认阈值（90K chars, 90% mem）
claude-opus-4-7        →  prefer_cloud   降低阈值 20%（72K chars, 85% mem）+ 使用 pro 模型
claude-haiku-4-5       →  prefer_local   提高阈值 33%（120K chars, 90% mem）
```

**核心逻辑变更**: 从「强制路由」改为「阈值调整」。SmartRouter 的 Priority 6/7/8（内存/上下文/stage）**始终生效**，不因偏好而跳过。

#### 9.4.2 效果对比

| 场景 | 当前设计（强制） | Review 建议（偏好） |
|------|----------------|-------------------|
| haiku + 短上下文 | local（正确） | local（正确） |
| haiku + 长上下文 + OOM 风险 | **local → OOM** ❌ | **cloud**（安全优先）✅ |
| opus + 短上下文 | **cloud → 白花钱** ❌ | **local**（经济合理）✅ |
| opus + 长上下文 | cloud（正确） | **cloud + pro 模型**（质量+安全）✅ |

#### 9.4.3 实现

```python
# proxy_state.py — 替代 MODEL_ROUTE_HINTS（强制映射）
# 模型 ID → 路由偏好配置
MODEL_ROUTE_PREFERENCES = {
    "claude-sonnet-4-6": {
        "cloud_model": PROXY_CLOUD_MODEL,        # 云端用默认（flash）
        "route_bias": "auto",                     # SmartRouter 完全自主
        "threshold_factor": 1.0,                  # 阈值系数
        "memory_bias": 0,                         # 内存偏置
    },
    "claude-opus-4-7": {
        "cloud_model": "deepseek-v4-pro",        # 云端用 pro（高质量）
        "route_bias": "prefer_cloud",             # 偏好云端但不强制
        "threshold_factor": 0.8,                  # 阈值降低 20%（72K 触发）
        "memory_bias": -5,                        # 内存触发降低 5%（85% 触发）
    },
    "claude-haiku-4-5": {
        "cloud_model": PROXY_CLOUD_MODEL,        # 云端用 flash（省钱）
        "route_bias": "prefer_local",             # 偏好本地但不强制
        "threshold_factor": 1.33,                 # 阈值提高 33%（120K 触发）
        "memory_bias": 0,                         # 内存触发不变（90%）
    },
}
```

SmartRouter 决策逻辑变更（详见 §4.4 完整伪代码）：

```python
# SmartRouter._routing_decision() — Priority 0.5 核心片段
# 从「直接返回 cloud/local」改为「调整阈值参数」
# 完整实现见 §4.4

requested_model = ctx.body.get("model", "")
pref = _ps.MODEL_ROUTE_PREFERENCES.get(requested_model, {})

# 根据偏好调整阈值（用于后续 Priority 6/7/8）
effective_threshold = int(PROXY_ROUTE_THRESHOLD_CHARS * pref.get("threshold_factor", 1.0))
effective_memory_pct = PROXY_ROUTE_MEMORY_PCT + pref.get("memory_bias", 0)

# 记录选择的云端模型（供 FormatConverter 使用）
ctx._route_cloud_model = pref.get("cloud_model", PROXY_CLOUD_MODEL)
ctx._agent_model_tier = _classify_tier(requested_model)  # "opus" / "sonnet" / "haiku"

# ❌ 不再直接返回 cloud/local
# 继续 Priority 0.6-9，使用 effective_* 替代原始阈值
```

**`_classify_tier` 和 `_resolve_cloud_model` 函数定义见 §4.4。**

### 9.5 Response 模型名真实性（方案 B：兼容优先 + Header 透传）

**根据 Review 修正：从方案 A 改为方案 B。**

**理由**：主流 cc-router 生态（`ccproxy`、`claude-code-router`、`Free-Claude-Code`）均采用**回显策略**——response 中的 `model` 字段原样返回 Agent 请求的值。Anthropic SDK 可能校验 `response.model ∈ availableModels`，返回 `deepseek-v4-flash` 可能触发 SDK 异常。

#### 9.5.1 核心行为

```
Agent 请求: model="claude-sonnet-4-6"
  ↓
Proxy 处理: openai_body.model = "deepseek-v4-flash"（发给 DeepSeek）
  ↓
响应 body:      model="claude-sonnet-4-6"         ← 回显 Agent 请求值（兼容）
响应 Header:    X-Actual-Model: deepseek-v4-flash  ← 实际后端名（可选读取）
                X-Route-Target: cloud              ← 路由目标
                X-Route-Reason: chars_exceed_threshold  ← 决策原因
```

#### 9.5.2 改动点

| 位置 | 行为 | 说明 |
|------|------|------|
| `response.body.model` | **回显** `anthropic_body.get("model", ...)` | **当前已正确**，无需改。确认代码：`anthropic_proxy.py:698`（non-streaming）、`:761`（streaming） |
| `response.headers` | 新增 3 个 X-* Header | BackendDispatcher 写入 |
| `openai_body["model"]` | 根据 `_route_target` + `_route_cloud_model` 选择 | FormatConverter 已有逻辑 |
| `message_converter` | 不变 | 无需修改 |
| PipelineContext | 无需新增 `_actual_response_model` | 节省改动 |

**P0#4 确认 — Response model 回显（当前已正确）：**

```python
# anthropic_proxy.py:698 — 非流式响应
anthropic_resp = convert_openai_response_to_anthropic(
    openai_resp,
    anthropic_body.get("model", "claude-3-5-sonnet-20241022")  # ← 回显 Agent 请求值
)

# anthropic_proxy.py:761 — 流式响应
model_name = anthropic_body.get("model", "claude-3-5-sonnet-20241022")  # ← 回显 Agent 请求值
# ... SSE 事件中使用 model_name
```

这两处**已经正确**回显 Agent 请求值，智能路由不需要修改此处。只需确保 BackendDispatcher 注入 Header 后不影响现有逻辑。

#### 9.5.3 FormatConverter 核心逻辑

```python
# FormatConverter.process() 中
if ctx._route_target == "cloud":
    # 根据 Agent tier 选择云端模型（opus→pro, sonnet/haiku→flash）
    ctx._route_cloud_model = _resolve_cloud_model(ctx._agent_model_tier)
    openai_body["model"] = ctx._route_cloud_model
else:
    openai_body["model"] = _ps.MODEL_NAME
```

#### 9.5.4 Response Header 注入

> **SSE 注意事项**: `X-*` 自定义 Header 必须在 HTTP 响应头中发送（`send_header()` 在 `send_response()` 之前调用），而非在 SSE event stream 中。无论流式 (`text/event-stream`) 还是非流式 (`application/json`) 响应，自定义 Header 均为 HTTP 层元数据，由 BackendDispatcher 在调用 handler 的 response 处理方法之前注入。

```python
# BackendDispatcher.process() — 在 _dispatch 成功返回后、构造 response 之前
# 通过 Handler._current_ctx 传递 PipelineContext

# 1. 暂存 ctx 到 handler（Handler 本身不接收 ctx 参数）
self._handler._current_ctx = ctx

# 2. 注入 HTTP response headers（在 send_response() 之前）
if hasattr(self._handler, 'send_header'):
    actual_model = ctx._route_cloud_model or _ps.MODEL_NAME
    self._handler.send_header("X-Actual-Model", actual_model)
    self._handler.send_header("X-Route-Target", ctx._route_target)
    self._handler.send_header("X-Route-Reason", ctx._route_reason)

# 3. 然后调用正常的 streaming / non-streaming 处理
# （response body 中的 model 字段始终回显 Agent 请求值）

**响应 Headers 完整规格（P1#5）**:

| Header | 类型 | 说明 | 示例值 |
|--------|------|------|--------|
| `X-Route-Target` | `string` | 路由目标 | `local` / `cloud` / `local_forced` |
| `X-Route-Reason` | `string` | 决策原因标签（完整格式） | `chars_exceed_threshold(127843 > 90000)` / `session_already_cloud` / `under_threshold` / `header_override` |
| `X-Actual-Model` | `string` | 实际后端模型名 | `deepseek-v4-flash` / `mlx-community/Qwen3.6-35B-A3B-4bit` |

**注入时机**：BackendDispatcher 在 `_dispatch` 成功返回后、`send_response()` 之前注入。无论流式（`text/event-stream`）还是非流式（`application/json`），Header 均为 HTTP 层元数据。

**Agent 读取方式**：通过 HTTP response headers 读取（可选）。Claude Code / OpenCode 的 SDK 层通常忽略自定义 Header，但中间件（如日志收集、成本分析工具）应读取。
```
HTTP/1.1 200 OK
Content-Type: text/event-stream
X-Actual-Model: deepseek-v4-flash
X-Route-Target: cloud
X-Route-Reason: chars_exceed_threshold(127843 > 90000)

{"type": "message_start", "message": {"model": "claude-sonnet-4-6", ...}}
```

#### 9.5.5 与 cc-router 生态的一致性

| 工具 | Response model | 额外机制 |
|------|---------------|---------|
| `@ssbun/cc-router` | 原样回显 | — |
| `claude-code-router` | 原样回显 | 配置映射表内部转换 |
| `Free-Claude-Code` | Gateway ID「伪装」Claude 别名 | — |
| `ccproxy` (QAA-Tools) | 原样回显 | Web UI 切换 provider |
| **本项目（v2.8 修正）** | **原样回显** | **+ X-Actual-Model / X-Route-Target Header** |

### 9.6 Agent 配置指南

用户需要配置 Agent 以启用 Gateway 模型发现或显式指定可用模型。

#### 9.6.1 Claude Code Gateway 发现（推荐）

```bash
# 让 Claude Code 从 Proxy 的 /v1/models 动态发现模型
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1

# 或持久化到 settings.json
{
  "env": {
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"
  }
}
```

设置后，Claude Code 的 `/model` 选择器会显示 Proxy 返回的所有模型 ID 及其 capabilities，用户可直接选择不同路由策略。

#### 9.6.2 Settings.json 显式配置

```json
{
  "model": "claude-sonnet-4-6",       // 默认 auto 路由
  "availableModels": [
    "claude-sonnet-4-6",              // auto 路由（SmartRouter 决策）
    "claude-opus-4-7",               // 偏好云端 + pro 模型
    "claude-haiku-4-5"               // 偏好本地
  ]
}
```

#### 9.6.3 ANTHROPIC_MODEL 环境变量

```bash
# Session 级别覆盖（偏好而非强制，安全约束始终优先）
export ANTHROPIC_MODEL=claude-opus-4-7      # 偏好云端（72K 阈值 + pro 模型）
export ANTHROPIC_MODEL=claude-haiku-4-5     # 偏好本地（120K 阈值 + flash 模型）
export ANTHROPIC_MODEL=claude-sonnet-4-6    # 自动路由（默认 90K 阈值）
```

#### 9.6.4 OpenCode 配置

OpenCode 使用方式不同，代理层同样适用：

**方式 1：全局模型配置**

```json
~/.config/opencode/opencode.json:
{
  "model": "claude-sonnet-4-6"       // auto 路由
}
```

**方式 2：Per-Agent 策略模型绑定（P2#8）**

不同 Agent 可绑定不同模型 ID，表达不同路由偏好。OpenCode 原生支持 Per-Agent 模型绑定，Claude Code 需通过 `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` 或 `availableModels` 配置实现：

```json
// OpenCode:
{
  "agent": {
    "plan": {
      "model": "claude-opus-4-7"     // plan agent: 偏好云端 + pro（最高质量）
    },
    "build": {
      "model": "claude-sonnet-4-6"   // build agent: auto 路由（SmartRouter 决策）
    },
    "explore": {
      "model": "claude-haiku-4-5"    // explore agent: 偏好本地（快速+隐私）
    }
  }
}
```

各策略绑定到 Proxy 后的实际行为：

| Agent 类型 | 发送的 model ID | Proxy tier 解析 | 路由偏好 | 云端模型 | 本地模型 |
|-----------|----------------|----------------|---------|---------|---------|
| Plan | `claude-opus-4-7` | `opus` | `prefer_cloud`（阈值降低 20%） | `deepseek-v4-pro` | `MODEL_NAME` |
| Build | `claude-sonnet-4-6` | `sonnet` | `auto`（SmartRouter 自主决策） | `PROXY_CLOUD_MODEL` | `MODEL_NAME` |
| Explore | `claude-haiku-4-5` | `haiku` | `prefer_local`（阈值提高 33%） | `PROXY_CLOUD_MODEL` | `MODEL_NAME` |

> **关键**：Per-Agent 绑定对 Agent 完全透明。Agent 始终发送 Anthropic 别名，Proxy 内部根据 tier 决定路由偏好和云端模型选择。Agent 不需要知道本地用 Qwen 还是云端用 DeepSeek。

**方式 3：API Base URL 指向 Proxy**

```bash
export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
# 或通过 OpenCode 原生 Anthropic provider 配置
```

### 9.7 路由切换的 Agent 感知

当 SmartRouter 在 Session 内切换后端时，Agent 通过三个渠道感知：

#### 9.7.1 RouteNotification（已设计，见 §1.6）

首次切换到云端时注入 `[System: Switched to cloud model...]` 通知（区分首次主动路由和回退紧急通知），包含切换原因、上下文大小、成本估算。

#### 9.7.2 Response Header 透传（§9.5）

切换后的第一条 response 中包含 `X-Actual-Model`、`X-Route-Target`、`X-Route-Reason` Header。Agent 或客户端可主动读取这些 Header 感知路由变化。Response body 中的 `model` 字段**不变**（兼容优先）。

```
切换前响应 Header:  X-Actual-Model: mlx-community/Qwen3.6-35B-A3B-4bit
切换后响应 Header:  X-Actual-Model: deepseek-v4-flash
                    X-Route-Target: cloud
                    X-Route-Reason: chars_exceed_threshold(127843 > 90000)
```

#### 9.7.3 `/status` 页面（已设计，见 §5.2）

页面显示当前路由目标和实际模型名，用户可手动确认。

### 9.8 安全与兼容性

#### 9.8.1 向后兼容

| 场景 | 行为 | 兼容性 |
|------|------|--------|
| `PROXY_ROUTE_ENABLED=false`（默认） | `/v1/models` 返回精简列表，response model 回显 | ✅ 完全兼容 |
| `PROXY_ROUTE_ENABLED=true` | `/v1/models` 增加 `claude-opus-4-7`，新增 X-* Header | ✅ 前向兼容（新增字段/Header） |
| Client 不读取 X-* Header | 忽略额外 Header，行为不变 | ✅ 兼容 |
| Client 使用 `claude-sonnet-4-6` | SmartRouter 自动决策（同无路由时行为） | ✅ 兼容 |
| Anthropic SDK 校验 response.model | 始终回显 Agent 请求值，不触发校验失败 | ✅ 兼容 |
| cc-router 工具 | 回显策略与生态惯例一致 | ✅ 兼容 |

#### 9.8.2 敏感信息保护

- `MODEL_NAME` 不再暴露给 Agent（已从 `/v1/models` 移除）
- X-* Header 中的模型名不含 API Key 等凭据。云端模型名（如 `deepseek-v4-flash`）为公开信息，无泄露风险
- `MODEL_ROUTE_PREFERENCES` 不包含任何凭据信息
- 管理接口 `GET /admin/backend-info` 应绑定 localhost 或加访问控制（Phase 2）

#### 9.8.3 单元测试覆盖

| 测试场景 | 数量 | 说明 |
|---------|------|------|
| `_build_models_response` 稳定列表 | 3 cases | 路由启用/禁用/cloud 模式 |
| `get_model_aliases` 不包含 MODEL_NAME | 2 cases | 验证无内部名泄露 |
| `MODEL_ROUTE_PREFERENCES` 阈值调整 | 4 cases | auto/prefer_cloud/prefer_local/未知ID 默认 |
| `_routing_decision` 偏好不强制 | 6 cases | 短+长上下文各 3 种偏好（验证安全优先） |
| X-* Header 注入 | 3 cases | 非流式/流式/回退场景 |
| `get_model_aliases` 缓存 | 2 cases | 命中/未命中 |
| `_resolve_cloud_model` tier 映射 | 3 cases | opus→pro/sonnet→flash/haiku→flash |

### 9.10 最终契约：Agent ↔ Proxy 模型 ID 约定

#### 9.10.1 核心承诺

```
┌──────────────────────────────────────────────────────────────────────┐
│                    最终契约 (Final Contract)                          │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Agent 配置（永远不改）:                                               │
│    model = "claude-sonnet-4-6"    ← 默认 auto 路由                     │
│    model = "claude-opus-4-7"      ← 偏好云端 + pro（plan agent）        │
│    model = "claude-haiku-4-5"     ← 偏好本地（explore agent）           │
│                                                                      │
│  Proxy 内部动态决定:                                                   │
│    short context → local  → MODEL_NAME                               │
│    long context  → cloud  → PROXY_CLOUD_MODEL                        │
│    opus strategy → cloud  → deepseek-v4-pro                          │
│    haiku strategy→ local preferred / flash fallback                   │
│                                                                      │
│  Response body（Agent 看到的）:                                        │
│    model = "claude-sonnet-4-6"    ← 永远回显 Agent 请求值               │
│                                                                      │
│  Debug/Status（人类运维看到的）:                                         │
│    actual_model = "mlx-community/Qwen3.6-35B-A3B-4bit"               │
│    或 actual_model = "deepseek-v4-flash"                              │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

#### 9.10.2 契约解决的问题

| 问题 | 解决方案 |
|------|---------|
| 「后端模型切换后 Agent 需要手工切换」 | Agent 永远用固定 Anthropic 别名，切换完全由 Proxy 透明处理 |
| PRD 智能路由 | SmartRouter 根据上下文大小/内存压力/生命周期动态决定 local↔cloud |
| Session Sticky | 首次切换后 Session 级 sticky，不回退（避免 ping-pong） |
| 成本可见 | `/status` 页面展示实时成本 + X-* Header 透传实际模型 |
| 兼容性 | `/v1/models` 返回 Anthropic 标准格式 + response model 回显 + 不暴露内部名 |

#### 9.10.3 三个「永远不变」

1. **`/v1/models` 响应永远不变**：只返回 3-4 个 Anthropic 别名，不包含内部 MODEL_NAME
2. **Response body `model` 永远回显 Agent 请求值**：不随路由决策变化
3. **Agent 配置永远不需要修改**：切换后端、启用路由、调整阈值均无需 Agent 配合

#### 9.10.4 三个「可选感知」

1. **Response Headers**（`X-Route-Target`, `X-Route-Reason`, `X-Actual-Model`）：中间件可读取
2. **RouteNotification**（消息流中的 `[System: Switched...]`）：Agent 和用户可见
3. **`/status` 页面**：人类运维可查看路由状态和成本

---

## 10. 附录

### A. 路由阈值选择指南

| 场景 | 推荐阈值 | 路由率 (预估) | 说明 |
|------|---------|-------------|------|
| **保守 (默认)** | 90,000 (SATURATION 起点) | ~13% | 只在本地模型明显吃力时路由 |
| **激进** | 40,000 (EXPANSION 起点) | ~42% | 更早路由，更保守地保护本地模型 |
| **冒险** | 180,000 (OOM_DANGER 起点) | ~6% | 尽量少路由，但 SATURATION 区间 (90-180K) 循环率高 |

### B. 云端模型对比

| 模型 | 输入价格 | 输出价格 | 上下文窗口 | 推荐场景 |
|------|---------|---------|-----------|---------|
| `deepseek-v4-flash` ⭐ | ¥0.5/M | ¥1.5/M | 128K | 默认选择，性价比最高 |
| `deepseek-v4-pro` | ¥2.0/M | ¥8.0/M | 1M | 追求最高质量 |
| `gpt-4o-mini` (OpenAI) | ¥1.0/M | ¥4.0/M | 128K | OpenAI 生态用户 |

### C. 参考资料

- LiteLLM Router: https://docs.litellm.ai/docs/routing
- OpenRouter: https://openrouter.ai/
- DeepSeek API Pricing: https://api-docs.deepseek.com/quick_start/pricing
- DCP (Dynamic Context Pruning): https://github.com/isaacbmiller/dynamic-context-pruning
- 本项目 DEFECT-LIST.md: 30 项缺陷的完整分析
- 本项目 PM-ANALYSIS-FUTURE-ROADMAP.md: 产品路线图

### D. 术语表

| 术语 | 定义 |
|------|------|
| **路由 (Routing)** | 根据请求特征将请求分发到不同后端的决策过程 |
| **回退 (Fallback)** | 当首选后端不可用时切换到备用后端的机制 |
| **Session 路由状态** | 记录每个 Session 当前使用的后端，确保 Session 内一致性 |
| **SATURATION 阶段** | 生命周期分类中的第 4 阶段 (90K-180K chars)，本地模型开始明显吃力 |
| **ping-pong 切换** | 同一 Session 内在本地/云端之间反复切换（设计上禁止） |

---

> **设计文档版本**: v2.12  
> **修订内容**: v2.3 新增 §2.5 数据架构设计；v2.4 7 项文档一致性修正；v2.5 12 项第三轮 Review 修正；v2.6 5 项第四轮 Review 修正（notified_key 未定义 bug / 数据生命周期 / 版本号 / _last_stream_usage / 措辞）；v2.7 回退路径+本地路径 resp= 补全 / 总工作量表求和修正；v2.8 新增 §9 模型 ID 暴露契约（动态 /v1/models、模型 ID 路由提示、Response model 真实性方案 A、Agent 配置指南）；v2.9 §9 Review 修正：方案 A→B（回显+Header）、移除 MODEL_NAME 暴露、强制路由→偏好路由（安全优先）、RouteNotification 区分首次/回退、决策矩阵 Priority 0.5/0.6 顺序修正、补充 OpenCode 配置；v2.10 跨章节一致性修正：§4.4 伪代码同步更新（含 _classify_tier / _resolve_cloud_model helper）、§1.2 模块表补充 RequestParser tier 提取 + PipelineContext +7 字段、§2.4 新增 _agent_model_tier 字段、§9.5.4 Header 注入 SSE 时机说明、§9.6.2 措辞修正「强制」→「偏好」；v2.11 PM 排期修订：PROXY_ROUTE_DAILY_BUDGET 从 Phase 3 提前到 Phase 2、Phase 1 补充缺失任务、Phase 2 新增 RouteNotification + manage.sh 命令 + admin endpoint、Phase 3 缩减为成本追踪完善 + A/B + 文档；v2.12 补齐 P0/P1/P2 实现点：P0#1 MODEL_ALIASES 移除 MODEL_NAME + 清理指南（§9.3.1）、P0#2 /v1/models 稳定性测试用例（§9.3.4）、P0#3 flash thinking 改用实际 cloud model（§9.2.1）、P0#4 response model 回显确认 (anthropic_proxy.py:698/761)（§9.5.2）、P1#5 Response Headers 完整规格（§9.5.4）、P1#6 /status Active Sessions 表（§5.2）、P1#7 metrics v2 schema 新增 6 字段（§5.3）、P2#8 Per-Agent 策略绑定 plan/build/explore（§9.6.4）、新增 §9.10 最终契约
> **关联 PRD**: `docs/01-requirements-product/PRD-intelligent-model-routing.md`
