# 智能路由设计文档 Review（模型 ID 暴露契约专项）

> **Review 日期**: 2026-06-22
> **Review 对象**: `intelligent-model-routing-design.md` v2.8 → v2.10（重点关注 §9 模型 ID 暴露契约）
> **上轮 Review**: 
> - `intelligent-model-routing-design-review-2026-06-21.md`（PM 视角，架构清晰度、决策矩阵、并发安全）
> - `intelligent-model-routing-design-review-2026-06-22.md`（FR 逐条覆盖、技术深度）
> **本轮定位**: 第三轮 Review，聚焦 **§9 模型 ID 暴露契约**，基于 cc-switch / cc-router 生态调研和 Agent 兼容性分析
> **最新状态**: v2.9 采纳了本轮 Review 的所有 P0/P1 建议。v2.10 修正了跨章节不一致（§4.4 伪代码、PipelineContext 字段表、措辞统一）。所有问题已关闭。

---

## 目录

1. [总体评价](#1-总体评价)
2. [P0：必须修正](#2-p0必须修正)
3. [P1：设计层面改进](#3-p1设计层面改进)
4. [P2：优化建议](#4-p2优化建议)
5. [修正后 §9 推荐方案](#5-修正后-9-推荐方案)
6. [总结评分](#6-总结评分)

---

## 1. 总体评价

设计文档 v2.8 新增的 §9 直接回应了「暴露给 Agent 的模型 ID 和名称如何处理」这一核心问题，方向正确。但三个关键细节需要修正：

| 亮点 | 问题 |
|------|------|
| 动态 `/v1/models` 构思好 | Response model 真实性（方案 A）兼容风险高 |
| 模型 ID 编码路由语义的思路对 | `MODEL_NAME` 仍暴露给 Agent |
| Agent 配置指南（§9.6）实用 | Tier 强制路由存在矛盾场景 |

**核心原则确认（赞同）**：「Proxy 是模型发现入口」和「模型 ID 编码路由语义」是正确的设计方向。问题在于实现细节和边界处理。

---

## 2. P0：必须修正

### 2.1 Response `model` 字段真实性（方案 A）存在兼容风险

**当前设计**（§9.5）:

> Response 中的 `"model"` 字段**替换**为实际处理请求的模型名，而非 Client 请求的 ID。

```
Agent 请求: model="claude-sonnet-4-6"
  ↓
Proxy 处理: openai_body.model = "deepseek-v4-flash"（发给 DeepSeek）
  ↓
响应: model="deepseek-v4-flash"  ← Agent 看到陌生模型名
```

**风险评估**:

| 风险 | 概率 | 影响 | 说明 |
|------|------|------|------|
| Claude Code SDK 校验失败 | 中 | 高 | Anthropic SDK 可能校验 `response.model ∈ availableModels`。返回 `deepseek-v4-flash`（不在 `/v1/models` 返回的 Anthropic 别名中）可能触发 SDK 异常或静默重试 |
| cc-router 生态惯例违背 | — | 中 | 所有主流 cc-router 工具（`ccproxy`、`claude-code-router`、`Free-Claude-Code`）都**回显原始 model 名**。这是经过大规模验证的安全模式 |
| Agent 行为退化 | 低 | 中 | Claude Code 根据 response model 名做 tier 判断（opus/haiku 有不同行为模式）。收到 `deepseek-v4-flash` 可能误判 backend 能力 |
| OpenCode 兼容性 | 低 | 中 | OpenCode 的 `opencode.json` 中 `"model"` 字段与 response model 不匹配时可能报错 |

**业界证据**:

各主流 cc-router 工具均采用**回显策略**：

| 工具 | Response model 处理 |
|------|-------------------|
| `@ssbun/cc-router` | 原样回显 Client 请求的 model ID |
| `claude-code-router` (musistudio) | 原样回显，通过配置映射表内部转换 |
| `Free-Claude-Code` | Gateway Model ID 系统「伪装」Claude 模型 ID |
| `ccproxy` (QAA-Tools) | 原样回显，Web UI 切换 provider 但 response 不变 |

**建议：改为方案 B — 回显原始 model + 可选 Header 透传**

```python
# === 方案 B：兼容优先 ===

# Response body: 始终回显 Agent 请求的 model ID
anthropic_resp["model"] = anthropic_body.get("model", "claude-3-5-sonnet-20241022")

# 新增 optional response headers（Agent 可选读取，不破坏现有契约）
# X-Actual-Model: deepseek-v4-flash        ← 实际后端模型名
# X-Route-Target: cloud                     ← 路由目标
# X-Route-Reason: chars_exceed_threshold    ← 决策原因
```

**理由**:

1. **RouteNotification**（§1.6）已通过消息流通知 Agent 和用户
2. **`/status` 页面**（§5.2）已显示实际路由状态
3. **Response header** 是增量信息，不破坏任何现有契约
4. 与 cc-router 生态的**事实标准**保持一致

**如果必须保留方案 A**，至少做成可配置的：

```bash
# proxy_state.py 新增
PROXY_RESPONSE_MODEL_POLICY=echo     # "echo"：回显 Agent 请求值（默认，兼容优先）
                                     # "actual"：返回实际处理模型名（高级用户，需自行验证 Agent 兼容性）
```

实现：

```python
# FormatConverter / Response 处理中
if _ps.PROXY_RESPONSE_MODEL_POLICY == "actual":
    response_model = _ps.PROXY_CLOUD_MODEL if ctx._route_target == "cloud" else _ps.MODEL_NAME
else:
    response_model = anthropic_body.get("model", "claude-3-5-sonnet-20241022")
```

---

### 2.2 `/v1/models` 仍暴露 `MODEL_NAME` — 应移除

**当前代码**（§9.3.1 第 1596 行）:

```python
def get_model_aliases():
    aliases = []
    aliases.append("claude-sonnet-4-6")
    if IS_CLOUD or PROXY_ROUTE_ENABLED:
        aliases.append("claude-opus-4-7")
    if not IS_CLOUD or PROXY_ROUTE_FALLBACK_ENABLED:
        aliases.append("claude-haiku-4-5")
    # ⚠️ 问题：暴露内部模型名
    aliases.append(MODEL_NAME)  # "mlx-community/Qwen3.6-35B-A3B-4bit" 或 "deepseek-v4-flash"
    return aliases
```

**问题分析**:

`MODEL_NAME` 是内部实现细节。暴露给 Agent 后有连锁问题：

| 场景 | 后果 |
|------|------|
| Claude Code `/model` 选择器 | 显示 `mlx-community/Qwen3.6-35B-A3B-4bit` 这个奇怪的「模型名」 |
| `./manage.sh reload` 切换配置 | `MODEL_NAME` 从 `Qwen...` 变为 `deepseek-v4-flash`，Agent 的 `availableModels` 列表突变 |
| Agent 选择了 `MODEL_NAME` | 等同于绕过了路由体系，直接用裸模型名 |
| **这正是用户最初的问题** | 「后端模型切换后，Agent 需要手工进行切换」 |

**建议**:

```python
def get_model_aliases():
    """返回 Agent 可见的稳定模型列表。不包含内部 MODEL_NAME。"""
    aliases = [
        "claude-sonnet-4-6",    # auto 路由（SmartRouter 决策）
        "claude-haiku-4-5",     # 偏好本地
        "default",              # 兼容别名
    ]
    if IS_CLOUD or PROXY_ROUTE_ENABLED:
        aliases.append("claude-opus-4-7")  # 偏好云端（仅路由启用时暴露）
    return aliases
```

Agent 看到的模型列表永远只有 3-4 个 Anthropic 别名，**不随 reload、不随路由决策变化**。这就是「Agent 不需要手工切换」的保证。

**如果确实需要暴露实际后端名供调试**，通过独立机制而非混入 `/v1/models`：

```python
# /admin/status 或 /admin/models（调试/管理用，不面向 Agent）
GET /admin/backend-info → {
    "local_model": "mlx-community/Qwen3.6-35B-A3B-4bit",
    "cloud_model": "deepseek-v4-flash",
    "route_enabled": true
}
```

---

## 3. P1：设计层面改进

### 3.1 Tier → 路由强制映射存在安全矛盾

**当前设计**（§9.4.2）:

| 模型 ID | 路由策略 | 行为 |
|---------|---------|------|
| `claude-sonnet-4-6` | `auto` | SmartRouter 决策 |
| `claude-opus-4-7` | `cloud_only` | **强制**云端 |
| `claude-haiku-4-5` | `local_only` | **强制**本地 |

**矛盾场景**:

```
场景 A：用户选了 claude-opus-4-7（想要最强质量）
  → 上下文只有 5K chars → 决策矩阵 Priority 0.75 强制 cloud
  → 结果：白花钱（本地完全能处理，且零延迟）

场景 B：用户选了 claude-haiku-4-5（想要省钱快速）
  → 上下文 200K chars，内存 92% → 决策矩阵 Priority 0.75 强制 local
  → 结果：OOM 崩溃（智能路由本应保护用户）
```

**核心矛盾**: Agent 的模型 tier 选择表达的是**质量/成本偏好**，但当前设计把它当作**路由强制指令**。在 OOM 风险和成本之间，安全应该永远优先。

**建议：改为「路由偏好 + 云端模型选择」，不做强制路由**:

```python
# proxy_state.py — 替代 MODEL_ROUTE_HINTS
MODEL_ROUTE_PREFERENCES = {
    "claude-sonnet-4-6": {
        "cloud_model": PROXY_CLOUD_MODEL,      # 云端用默认（flash）
        "route_bias": "auto",                   # SmartRouter 完全自主
    },
    "claude-opus-4-7": {
        "cloud_model": "deepseek-v4-pro",       # 云端用 pro（高质量）
        "route_bias": "prefer_cloud",           # 偏好云端但不强制
        # 效果：降低路由阈值 20%（90K→72K），但仍受安全约束
    },
    "claude-haiku-4-5": {
        "cloud_model": PROXY_CLOUD_MODEL,       # 云端用 flash（省钱）
        "route_bias": "prefer_local",           # 偏好本地但不强制
        # 效果：提高路由阈值 33%（90K→120K），但内存压力仍可触发
    },
}
```

**决策矩阵调整**:

```python
# SmartRouter._routing_decision() 中
# Priority 0.75 改为：根据 route_bias 调整阈值，而非强制路由

pref = _ps.MODEL_ROUTE_PREFERENCES.get(requested_model, {})
route_bias = pref.get("route_bias", "auto")

# 调整阈值（在 Priority 6/7/8 中生效，而非独立优先）
if route_bias == "prefer_cloud":
    effective_threshold = int(PROXY_ROUTE_THRESHOLD_CHARS * 0.8)  # 降低 20%
    effective_memory_pct = PROXY_ROUTE_MEMORY_PCT - 5              # 更早触发
elif route_bias == "prefer_local":
    effective_threshold = int(PROXY_ROUTE_THRESHOLD_CHARS * 1.33) # 提高 33%
    effective_memory_pct = PROXY_ROUTE_MEMORY_PCT                   # 不变
else:  # auto
    effective_threshold = PROXY_ROUTE_THRESHOLD_CHARS
    effective_memory_pct = PROXY_ROUTE_MEMORY_PCT

# 内存压力（Priority 6）和上下文大小（Priority 7）始终生效
# 不做 Priority 0.75 的强制云/本地判断
```

**效果对比**:

| 场景 | 当前设计（强制） | 建议设计（偏好） |
|------|----------------|----------------|
| haiku + 短上下文 | local（正确） | local（正确） |
| haiku + 长上下文+OOM风险 | **local → OOM** ❌ | **cloud**（内存安全优先）✅ |
| opus + 短上下文 | **cloud → 白花钱** ❌ | **local**（经济合理）✅ |
| opus + 长上下文 | cloud（正确） | **cloud + pro**（质量+安全）✅ |

---

### 3.2 `capabilities` 非标准字段 — 不能依赖 Agent 理解

**当前设计**（§9.3）在 `/v1/models` 响应中添加 `capabilities` 自定义字段：

```json
{
  "id": "claude-sonnet-4-6",
  "capabilities": {
    "target": "auto",
    "actual_model": "mlx-community/Qwen3.6-35B-A3B-4bit",
    "description": "Auto-routed: local (short ctx) or cloud (long ctx)"
  }
}
```

**问题**:

| 方面 | 分析 |
|------|------|
| Anthropic 标准格式 | `/v1/models` 标准字段是 `id`, `object`, `created`, `owned_by`。没有 `capabilities` |
| 前向兼容 | Agent 会忽略未知 JSON 字段（没问题），但**不能假设 Agent 会读取** |
| `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` | 期望的是标准 Anthropic 格式，自定义字段可能被忽略或导致解析问题 |
| 实际受众 | `capabilities` 对**人类运维者**有价值，但人类不直接读 `/v1/models` JSON |

**建议**: 保留 `capabilities` 字段（前向兼容，无破坏性），但做三件事：

1. **文档明确声明**：`capabilities` 是辅助元数据，Agent 可能完全忽略
2. **不依赖它作为路由感知的主机制**：主要路由感知通过 RouteNotification（§1.6）+ Response Header（§2.1 建议）+ `/status` 页面（§5.2）
3. **增加 `owned_by` 区分模型来源**（Anthropic 标准字段）：

```python
def _build_models_response():
    models = []
    
    # 标准字段 + 可选 capabilities
    models.append({
        "id": "claude-sonnet-4-6",
        "object": "model",
        "created": 1677610602,
        "owned_by": "proxy-router",              # 标识为代理层模型（Agent 可选读取）
        "capabilities": {                         # 自定义元数据（辅助）
            "route_target": "auto",
            "cloud_model": PROXY_CLOUD_MODEL,
            "local_model": MODEL_NAME,
            "description": "Auto-routed: local (short ctx) or cloud (long ctx)"
        }
    })
    
    # ... 其他模型同理
    
    return {"object": "list", "data": models}
```

---

### 3.3 RouteNotification 通知内容应区分首次/回退

**当前设计**（§1.6）只有一种通知模板。但实际有两种场景：

| 场景 | 触发条件 | 通知内容应有差异 |
|------|---------|----------------|
| **首次主动路由** | chars > threshold | 强调「保护本地模型」+ 成本估算 |
| **回退路由** | 云端失败 → local | 强调「云端不可用，紧急回退」+ 上下文被截断警告 |

**建议**:

```python
class RouteNotification(ConditionalStage):
    def process(self, ctx):
        if ctx._emergency_fallback:
            notice = self._build_emergency_notice(ctx)
        else:
            notice = self._build_first_route_notice(ctx)
        # ...

    def _build_first_route_notice(self, ctx):
        """首次主动路由到云端的通知。"""
        return (
            f"[System: Switched to cloud model — context {ctx.total_chars:,} chars "
            f"exceeds local {_ps.PROXY_ROUTE_THRESHOLD_CHARS:,} limit. "
            f"Using {_ps.PROXY_CLOUD_MODEL}. "
            f"Estimated cost ~¥0.01-0.04/request. "
            f"Session will stay on cloud. New sessions return to local. "
            f"To force local: `./manage.sh route-force-local {ctx.session_id}`.]"
        )

    def _build_emergency_notice(self, ctx):
        """云端回退到本地的紧急通知。"""
        return (
            f"[System: Cloud API unavailable, emergency fallback to local. "
            f"Context severely truncated to prevent OOM "
            f"(kept last 3 rounds). "
            f"Consider /compact or retry when cloud recovers. "
            f"To force cloud retry: `./manage.sh route-force-cloud {ctx.session_id}`.]"
        )
```

---

### 3.4 决策矩阵 Priority 0.5/0.75 顺序问题

**当前设计**（§4.1）:

| 优先级 | 条件 | 
|--------|------|
| 0.5 | Header 覆盖（单次） |
| 0.75 | 模型 ID 路由提示 |

**问题**: Header 覆盖是**调试/测试工具**，模型 ID 选择是**Agent 配置层面的决策**。如果 Agent 配置了 `claude-haiku-4-5`（偏好本地），同时请求带了 `X-Proxy-Route-To: cloud` header，哪个该赢？

**建议**: **模型 ID 偏好优先于 Header 覆盖**。Header 覆盖应该是调试工具，用于临时探索，不应覆盖 Agent 配置的意图。

```diff
  | 优先级 | 条件 | 决策 |
  |--------|------|------|
- | 0.5 | Header 覆盖 | local/cloud |
- | 0.75 | 模型 ID 路由偏好 | read preference |
+ | 0.5 | 模型 ID 路由偏好 | read preference |
+ | 0.6 | Header 覆盖（单次调试） | local/cloud |
  | 1 | 冷却期 | local |
```

---

## 4. P2：优化建议

### 4.1 补充 OpenCode 的配置指南

§9.6 只有 Claude Code 的配置说明。OpenCode 的模型配置方式不同，且使用代理层的场景越来越多。

**建议在 §9.6 增加**:

```markdown
#### 9.6.4 OpenCode 配置

**方式 1：全局模型**

~/.config/opencode/opencode.json:
{
  "model": "claude-sonnet-4-6"       // auto 路由
}

**方式 2：Per-Agent 模型绑定**

{
  "agent": {
    "plan": {
      "model": "claude-opus-4-7"     // plan agent 偏好云端 + pro
    },
    "build": {
      "model": "claude-sonnet-4-6"   // build agent 自动路由
    },
    "explore": {
      "model": "claude-haiku-4-5"    // explore agent 偏好本地
    }
  }
}

**方式 3：API Base URL 指向 Proxy**

export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
# 或通过 OpenCode 原生 Anthropic provider 配置
```

### 4.2 `/v1/models` 支持分页参数

当前实现直接返回全量列表。Anthropic API 支持 `limit` 和 `before` 参数进行分页。

```python
# GET /v1/models?limit=20&before=claude-haiku-4-5
def _handle_models_request(self):
    limit = int(self._get_query_param("limit", 20))
    before = self._get_query_param("before", None)
    
    models = _build_models_response()["data"]
    if before:
        # 找到 before 的位置，返回之后的模型
        for i, m in enumerate(models):
            if m["id"] == before:
                models = models[i+1:]
                break
    models = models[:limit]
    
    self._respond_json({"object": "list", "data": models})
```

### 4.3 Model Alias 加载时机优化

`get_model_aliases()` 每次 `/v1/models` 请求都重新计算，但模型列表只在 reload 时变化。建议加缓存：

```python
# proxy_state.py
_MODEL_ALIASES_CACHE = None
_MODEL_ALIASES_CACHE_VERSION = 0

def get_model_aliases():
    global _MODEL_ALIASES_CACHE, _MODEL_ALIASES_CACHE_VERSION
    if _MODEL_ALIASES_CACHE is not None and _MODEL_ALIASES_CACHE_VERSION == _RELOAD_VERSION:
        return _MODEL_ALIASES_CACHE
    # ... 重建 ...
    _MODEL_ALIASES_CACHE = aliases
    _MODEL_ALIASES_CACHE_VERSION = _RELOAD_VERSION
    return aliases
```

---

## 5. 修正后 §9 推荐方案

综合以上修正建议，§9 的核心实现应该是：

### 5.1 三层模型标识体系

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: Agent-Facing（稳定，不随路由/配置变化）              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ /v1/models 返回固定 Anthropic 别名:                     │  │
│  │   claude-sonnet-4-6  → auto 路由（默认）               │  │
│  │   claude-opus-4-7    → 偏好云端 + pro 模型             │  │
│  │   claude-haiku-4-5   → 偏好本地                       │  │
│  │   default            → 兼容别名                       │  │
│  │                                                        │  │
│  │ ❌ 不暴露 MODEL_NAME（内部模型名）                       │  │
│  │ ❌ 不随 reload/路由变化                                 │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  Layer 2: Response（兼容优先）                                │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ response.model        → 回显 Agent 请求值（兼容）       │  │
│  │ X-Actual-Model header → 实际后端模型名（可选读取）      │  │
│  │ X-Route-Target header → cloud/local（可选读取）        │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  Layer 3: Human-Facing（消息 + 页面）                         │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ RouteNotification  → 首次路由通知（消息流）              │  │
│  │ /status 页面       → 路由状态面板（浏览器）              │  │
│  │ /admin/backend-info → 实际后端详情（调试 API）           │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 PipelineContext 精简字段

```python
@dataclass
class PipelineContext:
    # === 路由相关（无需新增 _actual_response_model）===
    _route_target: str = "local"
    _route_reason: str = ""
    _route_header_override: str = ""
    _route_actual_cost: float = 0.0
    _route_cloud_model: str = ""         # 实际使用的云端模型（受 tier 偏好影响）
    _emergency_fallback: bool = False
    _agent_model_tier: str = "sonnet"    # 新增：Agent 选择的 tier（opus/sonnet/haiku）
```

### 5.3 FormatConverter 核心逻辑

```python
# FormatConverter.process()
if ctx._route_target == "cloud":
    # 根据 Agent tier 选择云端模型
    ctx._route_cloud_model = _resolve_cloud_model(ctx._agent_model_tier)
    openai_body["model"] = ctx._route_cloud_model
else:
    openai_body["model"] = _ps.MODEL_NAME

# Response model 回显 Agent 请求值（兼容优先）
# ctx._actual_response_model 不再需要
# 响应处理函数继续使用 anthropic_body.get("model") 原样回显
```

### 5.4 Response Header 注入

```python
# BackendDispatcher 成功返回后
if hasattr(self._handler, 'send_header'):
    self._handler.send_header("X-Actual-Model", ctx._route_cloud_model or _ps.MODEL_NAME)
    self._handler.send_header("X-Route-Target", ctx._route_target)
    self._handler.send_header("X-Route-Reason", ctx._route_reason)
```

### 5.5 修正前后的关键差异

| 方面 | 当前 v2.8 设计 | 修正建议 |
|------|--------------|---------|
| Response model | 替换为实际后端名 | 回显 Agent 请求值 + Header 透传 |
| `/v1/models` 包含 MODEL_NAME | ✅ 包含 | ❌ 移除 |
| Tier 路由 | 强制（cloud_only / local_only） | 偏好（prefer_cloud / prefer_local），安全仍优先 |
| `capabilities` 字段 | 依赖 Agent 理解 | 保留但声明为辅助元数据 |
| 新字段 `_actual_response_model` | 需要 | 不需要（方案 B 无需新字段） |
| `_agent_model_tier` | 无 | 新增，承载 Agent tier 选择 |
| Decision Priority 0.5/0.75 | Header > Model | Model > Header |
| OpenCode 配置 | 缺失 | 补充 |
| RouteNotification | 单一模板 | 区分首次/回退两种场景 |

---

## 6. 总结评分

| 维度 | 评分 | 说明 |
|------|------|------|
| 架构设计 | ⭐⭐⭐⭐⭐ | SmartRouter + 双后端 + Stage Skip 方案成熟，方向正确 |
| 模型 ID 契约 | ⭐⭐⭐ | §9 方向正确，但 Response 方案 A 有兼容风险、MODEL_NAME 仍暴露 |
| Tier 映射 | ⭐⭐⭐ | 强制路由存在安全隐患，建议改为偏好提示 |
| Agent 兼容性 | ⭐⭐⭐⭐ | Claude Code 考虑充分，OpenCode 缺失 |
| 可实施性 | ⭐⭐⭐⭐⭐ | 分 Phase 合理，工作量估算准确。修正后无新增字段，实施更简单 |
| **综合** | **⭐⭐⭐⭐** | 方向正确，3 项 P0 修正后可进入 Phase 1 开发 |

### 本轮问题统计

| 级别 | 数量 | 条目 |
|------|------|------|
| 🔴 P0 | 2 | #2.1 Response model 方案 A 兼容风险, #2.2 MODEL_NAME 暴露 |
| 🟡 P1 | 4 | #3.1 Tier 强制路由安全矛盾, #3.2 capabilities 非标准字段, #3.3 RouteNotification 模板单一, #3.4 Priority 顺序 |
| 🟢 P2 | 3 | #4.1 OpenCode 配置缺失, #4.2 分页参数, #4.3 别名缓存 |

### 与前三轮 Review 的合并

综合三轮 Review，设计文档待解决问题共 **18 项**：

| 来源 | P0 | P1 | P2 |
|------|----|----|-----|
| 第一轮 (06-21) | 5 项 | 3 项 | 2 项 |
| 第二轮 (06-22) | 3 项 | 3 项 | 3 项 |
| **本轮 (06-22 模型 ID)** | **2 项** | **4 项** | **3 项** |
| **合计** | **10 项** | **10 项** | **8 项** |

> 本轮与上两轮**无重复**。第一轮侧重架构和并发安全，第二轮侧重 FR 覆盖和配置，本轮侧重模型 ID 暴露契约。

---

> **Review 人**: Claude Code (claude.ai/code)  
> **后续行动**: 建议综合三轮 Review，产出设计文档 v2.9。推荐优先修正本轮的 2 项 P0（Response model 回显 + MODEL_NAME 移除），这两项修正会减少实现复杂度（移除 `_actual_response_model` 字段，减少 Handler 改造）。
