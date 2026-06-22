# 智能路由设计文档 Review（第二轮：FR 逐条覆盖 + 技术深度）

> **Review 日期**: 2026-06-22
> **Review 对象**: `docs/02-architecture-design/intelligent-model-routing-design.md` v2.0
> **参考 PRD**: `docs/01-requirements-product/PRD-intelligent-model-routing.md` v1.4
> **上轮 Review**: `intelligent-model-routing-design-review-2026-06-21.md`（PM 视角，已覆盖架构清晰度、决策矩阵优先级、`_route_notified` 重复字段、并发安全等）
> **本轮定位**: 第二轮 Review，侧重 **FR 逐条覆盖度 + 技术实现细节**，与上轮互补，避免重复

---

## 与上轮 Review 的分工

上轮 Review (06-21) 已覆盖以下问题，本轮**不再重复**：

| 上轮已覆盖 | 本轮对应 |
|-----------|---------|
| Stage 编号不一致 | 仅补充 §1.3 遗漏 + RouteNotification 编号 |
| HighDropRatioNotice → ConditionalStage | — |
| 回退流程与 Pipeline 协作歧义 | — |
| Emergency truncation 触发时机矛盾 | — |
| PipelineContext `_route_notified` 重复字段 | — |
| `_route_cloud_model` 字段必要性 | — |
| 决策矩阵优先级逻辑问题 | 仅补充 Header 优先级行 |
| 冷却期与 `local_forced` 交互 | — |
| /status 成本显示（估算 vs 真实） | 扩展为 #7 实际成本计算流程 |
| `PROXY_CLOUD_API_KEY` 默认值 | 扩展为 #9 |
| Profile 加载顺序 | — |
| Phase 1 工作量估算 | — |
| 缺少回归测试计划 | 扩展为 #6 测试数量 |
| R2 daily budget cap 配置缺失 | — |
| R7 概率/影响评估 | — |

---

## 本轮新发现的问题

### 🔴 P0：必须修复

#### #1 FR-6.3 `X-Proxy-Route-To` Header 在设计中完全缺失

**严重程度**: 🔴 P0（PRD 功能需求未覆盖）

PRD FR-6.3 要求：
> 支持 `X-Proxy-Route-To: local` 或 `cloud` header，单次覆盖不写入 `_SESSION_ROUTE_MAP`

设计文档中的遗漏：
- §4.4 `_routing_decision()` 伪代码中**没有任何 Header 检查逻辑**
- §2.4 PipelineContext 字段表中没有 `_route_header_override` 字段
- §1.2 模块表中 `RequestParser` 没有「解析 X-Proxy-Route-To header」的改造描述
- 整个 SmartRouter 未读取 HTTP request headers

**建议修复**:

1. PipelineContext 新增字段：
```python
_route_header_override: str = ""  # "local" | "cloud" | ""
```

2. RequestParser 从 HTTP header 解析并填充此字段。

3. `_routing_decision()` 在优先级 0 之后增加：
```python
# Priority 0.5: X-Proxy-Route-To header (single-request, no session sticky)
if ctx._route_header_override in ("local", "cloud"):
    return ctx._route_header_override, "header_override"
```

4. §4.1 决策矩阵新增一行（优先级 0.5）。

---

#### #2 `PROXY_ROUTE_FALLBACK_ENABLED=false` 的行为未定义

**严重程度**: 🔴 P0（配置存在但行为未定义）

§3.1 配置表定义了 `PROXY_ROUTE_FALLBACK_ENABLED`（默认 true），但以下位置均未引用此开关：

- §2.2 BackendDispatcher 流程图：云端 HTTPError → 直接 fallback，无开关判断
- §2.3 云端回退流程：假设回退始终启用
- §4.4 决策伪代码：无相关检查
- §8.1 所有失败场景：假设回退始终启用

当用户设置 `PROXY_ROUTE_FALLBACK_ENABLED=false`（如处理敏感数据时），云端失败后应**返回 503 而非静默回退**。

**建议修复**:

在 BackendDispatcher 回退逻辑中增加：
```python
if route_target == "cloud" and not PROXY_ROUTE_FALLBACK_ENABLED:
    handler._respond_json({
        "error": {
            "type": "cloud_unavailable",
            "message": "Cloud API failed and fallback disabled"
        }
    }, 503)
    return
```

§8.1 失败场景表新增对应行。

---

#### #3 RouteNotification Stage 未出现在 §1.3 流程图中

**严重程度**: 🔴 P0（§1.6 独立定义但流程图不可见）

§1.6 定义了完整的 `RouteNotification` Stage（含 ~40 行实现代码），但 §1.3 的 Stage 执行流程图中完全看不到这个 Stage。读者看到 §1.6 时会困惑其执行时机。

**建议修复**:

在 §1.3 流程图 SmartRouter 之后增加：
```
Stage 2.6: RouteNotification ★    ← 始终执行（首次路由到云端时注入通知）
```

（注：SmartRouter 实际位于 Stage 2 之后，上轮 Review 已指出编号问题。此处沿用当前文档的 `Stage 1.5/1.6` 编号以保持一致，待编号修正后同步更新。）

---

### 🟡 P1：建议修复

#### #4 敏感路径 + 紧急回退的交互路径未定义

**严重程度**: 🟡 P1

考虑场景：Session 已路由到云端 → 请求包含敏感文件路径 → 云端失败 → 触发紧急回退（`_emergency_fallback=True`）→ 但敏感路径检测要求强制本地 → **矛盾**。

此时应急回退不应触发（截断后仍然要把敏感请求发到本地，敏感性问题未解决），应返回 403。

**建议修复**:

在 §2.3 紧急回退流程中增加：
```python
if getattr(ctx, '_emergency_fallback', False) and _is_sensitive_request(ctx):
    handler._respond_json({
        "error": {
            "type": "sensitive_fallback_blocked",
            "message": "Cloud API failed but request contains sensitive file paths."
        }
    }, 403)
    return
```

---

#### #5 NFR-4.2 测试数量与 Phase 1 计划不一致

**严重程度**: 🟡 P1

| 来源 | SmartRouter 测试 | BackendDispatcher 测试 |
|------|-----------------|----------------------|
| PRD §NFR-4.2 | ≥15 cases | — |
| 设计 §6 Phase 1 | ≥12 cases | ≥8 cases |

PRD 要求 SmartRouter ≥15 cases（覆盖所有决策路径），设计 Phase 1 只承诺 ≥12。

**建议修复**: 将 Phase 1「单元测试 SmartRouter (12 cases)」改为「**(15 cases)**」。决策矩阵 §4.1 有 7 行 + §4.2 有 8 个场景示例 = 15 个 case 正好覆盖。

---

#### #6 成本估算 post-request 实际值计算流程缺失

**严重程度**: 🟡 P1

PRD FR-4.4 区分：
- pre-request 估算（input tokens + max_tokens 上限）
- post-request 真实成本（后端 response `usage` 字段）

设计 §5.3 metrics 有 `actual_cost_total: null`，但**没有说明何时、在哪里、如何从后端 response 提取 `usage` 并计算**。

§2.2 BackendDispatcher 流程中也没有「提取 response.usage → 计算实际成本 → 写入 ctx」的步骤。

**建议修复**:

在 BackendDispatcher 成功路径中增加：
```
├─ on success:
│   ├─ streaming: after stream ends → extract usage.completion_tokens
│   │   → actual_cost = (input_tokens × PRICE_INPUT + completion_tokens × PRICE_OUTPUT) / 1M
│   │   → write to ctx._route_actual_cost
│   └─ non-streaming: extract response.usage → same calculation
```

PipelineContext 新增：`_route_actual_cost: float = 0.0`

---

### 🟢 P2：建议优化

#### #7 `manage.sh` 路由管理命令实现方案缺失

PRD 多处引用：
- `./manage.sh route-force-local <session_id>` — FR-6.2, US-1
- `./manage.sh route-disable` — §5.4
- `./manage.sh route-force-cloud <session_id>` — §8.1

但设计文档未说明这些命令如何与 proxy 内部状态（`_SESSION_ROUTE_MAP`）交互。

**建议修复**: 在 §1.2 模块表或附录中补充：

| 模块 | 改动描述 |
|------|---------|
| **admin_server.py** | +15 行，新增 `POST /admin/route/force-local` 和 `POST /admin/route/force-cloud` |
| **manage.sh** | +20 行，新增 `cmd_route_force_local` / `cmd_route_force_cloud`，通过 curl 调用 admin endpoint |

---

#### #8 云端 API Response 格式差异未讨论

DeepSeek API 和本地 llama-server/rapid-mlx 返回的 response 格式可能存在差异（`usage` 字段结构、`finish_reason` 枚举值、streaming chunk 结构）。设计文档未讨论 FormatConverter 和 response 处理如何应对。

**建议修复**: 在 §8 风险表新增一条，并在 BackendDispatcher 中统一 response 后处理。

---

#### #9 Session 路由状态的全量清理策略

§1.6 的 `_route_notified_{session_id}` 动态属性标记、§4.3 的 `_cloud_fail_count` 和 `_cloud_cooldown_start` 没有统一的清理策略。

**建议修复**: 所有路由相关的 session 状态 dict 使用同一个 LRU 上限（如 1000 条），在同一清理函数中处理。§4.3 的清理逻辑应扩展覆盖全部 4 个数据结构。

---

## 总结评分

| 维度 | 评分 | 说明 |
|------|------|------|
| FR 覆盖度 | **90%** | FR-6.3 Header 缺失；manage.sh 命令方案缺失 |
| NFR 覆盖度 | **95%** | 测试数量与 PRD 不一致（12 vs 15） |
| 设计质量 | **85%** | 核心架构清晰，但交互路径有遗漏（敏感+回退、回退禁用） |
| 一致性 | **80%** | §1.3 流程图与 §1.6 文字不同步；测试数量偏差 |
| 可实施性 | **90%** | 工作量估算合理，边界 case 处理方案需补充 |
| **综合** | **88%** | — |

### 本轮问题统计

| 级别 | 数量 | 条目 |
|------|------|------|
| 🔴 P0 | 3 | #1 Header 缺失, #2 FALLBACK_ENABLED 未定义, #3 RouteNotification 未入图 |
| 🟡 P1 | 3 | #4 敏感+回退交互, #5 测试数量, #6 实际成本计算 |
| 🟢 P2 | 3 | #7 manage.sh 方案, #8 Response 格式差异, #9 清理策略 |

### 与上轮 Review 合并后的完整问题清单

综合两轮 Review，设计文档待解决问题共 **15 项**：

| 来源 | P0 | P1 | P2 |
|------|----|----|-----|
| 上轮 (06-21) | 5 项 | 3 项 | 2 项 |
| 本轮 (06-22) | 3 项 | 3 项 | 3 项 |
| **合计** | **8 项** | **6 项** | **5 项** |

> 上轮 P0：Stage 编号、回退方案选择、Emergency truncation 时机、API_KEY 默认值、决策矩阵优先级
> 上轮 P1：`_route_notified` 字段重复、并发安全加锁、测试回归计划
> 上轮 P2：daily budget cap 配置、R7 风险评估
>
> 详见 `intelligent-model-routing-design-review-2026-06-21.md`。

**总体评价**：设计文档 v2.0 核心架构扎实，方向正确。两轮 Review 合计 15 项问题中，P0 的 8 项建议在进入 Phase 1 开发前解决，P1 的 6 项建议在 Phase 1 过程中修正，P2 的 5 项可在后续迭代处理。

---

> **Review 人**: Claude Code (claude.ai/code)
> **后续行动**: 建议作者综合两轮 Review，产出设计文档 v2.1。
