# Force/Prefer 双模式路由设计

> **版本**: v1.0  
> **日期**: 2026-06-23  
> **关联文档**: `intelligent-model-routing-design.md` (§4.1 决策矩阵、§9.4 模型 ID 路由偏好)

---

## 1. 需求背景

当前智能路由系统中，模型 ID 对路由行为仅起**偏好提示**效果（调整阈值 `threshold_factor` / `memory_bias`），不直接决定路由目标。用户希望在 Claude Code / OpenCode 中选择不同模型 ID 时获得不同的路由行为：

| 模型 | 期望 | 原因 |
|------|------|------|
| `claude-opus-4-7` | **直接走云端** (force) | 用户明确要最强模型，`deepseek-v4-pro` |
| `claude-sonnet-4-6` | **智能路由** (prefer) | SmartRouter 根据上下文/内存自动决策 |
| `claude-haiku-4-5` | **智能路由** (prefer) | SmartRouter 决策，加本地偏好 |

**核心需求**：用户通过 `/model` 命令切换模型 ID 时，Proxy 应尊重其意图，同时保留 prefer 模式的智能决策能力。

---

## 2. 设计

### 2.1 `behavior` 字段

在 `MODEL_ROUTE_PREFERENCES` 每项中增加 `behavior` 字段，取值为 `"force"` 或 `"prefer"`：

```python
MODEL_ROUTE_PREFERENCES = {
    "claude-opus-4-7": {
        "cloud_model": "deepseek-v4-pro",
        "route_bias": "prefer_cloud",
        "behavior": "force",              # ← 新增：直接走云端
        "threshold_factor": 0.8,          # 保留，用于 metrics 记录
        "memory_bias": -5,                # 保留
    },
    "claude-sonnet-4-6": {
        "cloud_model": PROXY_CLOUD_MODEL,
        "route_bias": "auto",
        "behavior": "prefer",             # ← 新增：SmartRouter 决策
        "threshold_factor": 1.0,
        "memory_bias": 0,
    },
    "claude-haiku-4-5": {
        "cloud_model": PROXY_CLOUD_MODEL,
        "route_bias": "prefer_local",
        "behavior": "prefer",             # ← 新增：SmartRouter 决策 + 本地偏好
        "threshold_factor": 1.33,
        "memory_bias": 0,
    },
}
```

### 2.2 决策矩阵扩展

**修改后**的 `_routing_decision()` Priority 0.5 逻辑：

```
Priority 0.5: 模型 ID 路由偏好

  ┌─ behavior="force" ─────────────────────┐
  │  route_bias="prefer_cloud" → return cloud  │
  │  route_bias="prefer_local" → return local   │
  │  route_bias="auto"        → 继续后续优先级   │
  └─────────────────────────────────────────────┘

  ┌─ behavior="prefer" ─────────────────────┐
  │  调整 effective_threshold / effective_memory_pct  │
  │  继续 Priority 0.6+ (当前行为)                    │
  └─────────────────────────────────────────────────────┘
```

### 2.3 Priority 顺序

| Priority | 条件 | 决策 |
|----------|------|------|
| 0 | routing disabled | local |
| **0.5** | **behavior=force, route_bias=prefer_cloud** | **→ cloud**（直接返回） |
| **0.5** | **behavior=force, route_bias=prefer_local** | **→ local**（直接返回） |
| **0.5** | behavior=prefer | 调整阈值，继续后续优先级 |
| 0.6 | Header override | local/cloud |
| 1 | cooldown | local |
| 2 | session_already_cloud | cloud |
| 3 | session_force_local | local |
| 3.5 | budget exceeded | local |
| 4 | sensitive_path + over limit | reject |
| 5 | sensitive_path | local |
| 6 | memory pressure | cloud |
| 7 | chars exceed threshold | cloud |
| 8 | lifecycle stage | cloud |
| 9 | default | local |

> force 模式在 Priority 0.5 直接 return，跳过 0.6-9 所有后续优先级。BackendDispatcher 在 HTTP 层处理实际错误（cloud 失败 → force 模式不 fallback → 返回 503）。

### 2.4 故障处理

| 模式 | cloud 失败时的行为 | 原因 |
|------|------------------|------|
| **force** | **返回 503**，不自动回退 | 用户可手工 `/model claude-sonnet-4-6` 切换到 prefer 模式。保留透明性——如果 force 静默 fallback，用户不知道 cloud 不可用 |
| **prefer** | **自动 fallback 到 local**（现有行为） | SmartRouter 已有 fallback 机制 |

### 2.5 安全考量

force 模式跳过所有 SmartRouter 安全保护（冷却期、Session sticky、Budget、内存压力、上下文大小、生命周期）。安全由 BackendDispatcher 在 HTTP 层兜底：

- **Cloud 无 API key** → 自动 return `local`（现有逻辑，pipeline.py:1812-1820）
- **Cloud HTTP 4xx/5xx** → force 模式返回 503（新增逻辑）
- **Metal OOM** → force 模式不走 local，所以 OOM 风险不适用于 force cloud

---

## 3. 交互流程

```
用户: /model claude-opus-4-7
  ↓
Claude Code: POST {"model": "claude-opus-4-7", ...}
  ↓
SmartRouter Priority 0.5:
  behavior="force", route_bias="prefer_cloud"
  → return "cloud", "model_forced_cloud(claude-opus-4-7)"
  ↓
BackendDispatcher:
  尝试 cloud API (deepseek-v4-pro)

  ┌─ 成功 ────────────────────────────────┐
  │ 响应 model = "claude-opus-4-7" (回显)   │
  │ Headers: X-Route-Target=cloud           │
  │          X-Route-Reason=model_forced_...│
  │          X-Actual-Model=deepseek-v4-pro │
  │ 用户获得最强模型                          │
  └─────────────────────────────────────────┘

  ┌─ 失败 (503) ──────────────────────────┐
  │ ctx._route_reason.startswith("forced_")│
  │ → 跳过 fallback                        │
  │ → 返回 503 给客户端                     │
  │ Claude Code 显示错误                    │
  │ 用户: /model claude-sonnet-4-6         │
  │ → prefer 模式 → 智能路由 → local       │
  └─────────────────────────────────────────┘
```

---

## 4. 改动文件清单

| 文件 | 改动 | 行数估算 |
|------|------|---------|
| `proxy_state.py` | `MODEL_ROUTE_PREFERENCES` 增加 `behavior` 字段 | +3 行 |
| `pipeline.py` | `SmartRouter._routing_decision()` Priority 0.5 增加 force 短路 | +8 行 |
| `pipeline.py` | `BackendDispatcher.process()` cloud 失败处增加 force 检查 | +10 行 |
| `test/unit/test_smart_router.py` | 修改 `test_opus_short_context_still_local` + 新增 3 个 force/prefer 测试 | ±15 行 |
| `test/unit/test_pipeline_stages.py` | 新增 `test_force_cloud_does_not_fallback` | +20 行 |

---

## 5. 测试矩阵

| 测试 | 输入 | 预期输出 |
|------|------|---------|
| `test_opus_force_cloud_short_context` | opus + 5K chars | → cloud, reason="model_forced_cloud(opus...)" |
| `test_opus_force_cloud_reason_tag` | opus + 0 chars | → cloud, reason 包含模型名 |
| `test_sonnet_prefer_still_uses_smart_router` | sonnet + 5K chars | → local (under_threshold) |
| `test_haiku_prefer_still_uses_bias` | haiku + 5K chars | → local (under_threshold) |
| `test_force_cloud_does_not_fallback_on_503` | opus + cloud 503 | → 503 response, no local fallback |

---

## 6. 与现有设计的兼容性

| 方面 | 变更前 | 变更后 |
|------|--------|--------|
| `MODEL_ROUTE_PREFERENCES` 结构 | 4 字段 | **5 字段** (+behavior) |
| opus 路由行为 | prefer (阈值降低 20%) | **force (直接 cloud)** |
| sonnet 路由行为 | prefer (auto) | prefer (auto) — **不变** |
| haiku 路由行为 | prefer (阈值提高 33%) | prefer (阈值提高 33%) — **不变** |
| 未知模型 ID 行为 | behavior 默认为空 → 走 prefer 路径 | behavior 默认 "prefer" → 走 prefer 路径 — **不变** |
| Cloud failure | 自动 fallback 到 local | prefer 同左；**force → 503，不 fallback** |

---

## 7. 待讨论事项

以下问题在评审 (`intelligent-model-routing-force-prefer-design-review.md`) 中被提出，方向待确认后再实施。

### 7.1 🔴 敏感路径策略是否应约束 force 模式

| 项目 | 内容 |
|------|------|
| **问题** | force 模式在 Priority 0.5 短路返回，跳过 Priority 4/5 的 `sensitive_path` 检查。如果某类文件/内容被配置为禁止上云，用户仅通过选 `claude-opus-4-7` 就能绕过，存在合规风险 |
| **当前行为** | ✅ SmartRouter 的 `sensitive_path` 仍会在 **prefer** 模式中生效，但 **force** 模式完全跳过 |
| **建议方案 A** | force 模式也受敏感路径约束，命中时返回 403（合规优先） |
| **建议方案 B** | 增加 `PROXY_ROUTE_FORCE_BYPASS_SENSITIVE` 配置（默认 `false`），默认 force 也走敏感检查 |
| **建议方案 C** | 跳过，因为 force 模式是用户主动选择，不应受策略限制。敏感路径策略属于 prefer 模式的安全兜底 |
| **推荐** | **方案 B**，平衡合规与灵活性。默认安全，高级用户可显式绕过 |

**影响范围**：SmartRouter 的 Priority 顺序调整（将 sensitive_path 检查移至 Priority 0.5 之前），或 force 分支内单独调用 `_is_sensitive_request()`。

### 7.2 🟡 预算上限是否应覆盖 force 模式

| 项目 | 内容 |
|------|------|
| **问题** | force 模式在 Priority 0.5 短路返回，跳过 Priority 3.5 的 `daily_budget_exceeded` 检查。如果用户日常选 opus 后忘记切回，可能一夜超预算 |
| **当前行为** | ✅ SmartRouter 的每日预算检查在 **prefer** 模式中生效，但 **force** 模式完全跳过。不过 BackendDispatcher 的成本记账函数 `_accumulate_route_daily_cost` 在 cloud 请求完成后仍会执行，因此 `/status` 页面上的成本数据始终准确 |
| **建议方案 A** | force 模式也检查每日预算，超预算时拒绝（预算保护优先） |
| **建议方案 B** | 增加 `PROXY_ROUTE_FORCE_BYPASS_BUDGET` 配置（默认 `false`），默认 force 也受预算约束 |
| **建议方案 C** | 跳过，因为 force 是用户主动选择，预算管理应在外部（如 Cloud API 自身的消费告警）完成 |
| **推荐** | **方案 B**，对齐敏感路径的模式。保持统一的「默认安全，可显式绕过」策略 |

**影响范围**：SmartRouter 的 Priority 顺序调整，或 force 分支内检查 `_ps._route_daily_cost >= PROXY_ROUTE_DAILY_BUDGET`。

### 7.3 🟢 评审已采纳项汇总

| 问题 | 等级 | 状态 | 实施文件 |
|------|------|------|---------|
| force + 无 API key → 错误而非 fallback | 🔴 P0 | ✅ **已采纳实施** | `pipeline.py` |
| 云端别名在无 cloud key 时不暴露 | 🔴 P0 | ✅ **已采纳实施** | `proxy_state.py` |
| reason 字符串包含目标云端模型名 | 🟢 P2 | ✅ **已采纳实施** | `pipeline.py` |
| Header override 与 force 优先级明确化 | 🟡 P1 | 📝 已记录 — 当前设计意图（用户模型选择 > 调试 header）正确 |
| Claude Code CLI 模型缓存 | 🟢 P2 | 📝 已记录 — `/v1/models` 变化需重启 Claude Code 才能感知 |
