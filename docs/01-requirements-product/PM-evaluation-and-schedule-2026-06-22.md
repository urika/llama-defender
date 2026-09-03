# 智能模型路由：PM 需求评估与排期

> **评估日期**: 2026-06-22
> **评估人**: PM 视角
> **评估对象**: PRD v1.4 + 设计文档 v2.10 + 三轮 Review（06-21/06-22/06-22-model-id）
> **评估结论**: ✅ 需求与方案通过评审，建议按三阶段排期进入开发

---

## 目录

1. [需求评估](#1-需求评估)
2. [方案评估](#2-方案评估)
3. [需求-方案覆盖矩阵](#3-需求-方案覆盖矩阵)
4. [Review 问题收敛分析](#4-review-问题收敛分析)
5. [工作量评估与排期](#5-工作量评估与排期)
6. [风险登记册](#6-风险登记册)
7. [里程碑与交付物](#7-里程碑与交付物)
8. [决策与建议](#8-决策与建议)

---

## 1. 需求评估

### 1.1 需求完整性：⭐⭐⭐⭐⭐（优秀）

PRD v1.4 经过三轮修订，需求定义成熟度高：

| 维度 | 覆盖情况 | 评价 |
|------|---------|------|
| **功能需求** | 6 个域、25 项 FR，P0-P2 三级优先级 | 完整，无遗漏 |
| **非功能需求** | 6 个域、17 项 NFR，含量化指标 | 完整，可度量 |
| **用户故事** | 5 个 US，含验收标准 | 覆盖核心场景 |
| **用户旅程** | 7 步旅程图，含异常路径 | 覆盖 happy + sad path |
| **数据基线** | 2,932 真实样本，区分修复前后 | 决策有据 |
| **成本模型** | 公式 + 场景估算 + 控制开关 | 清晰可算 |
| **竞品分析** | LiteLLM 4 方案对比 + 决策矩阵 | 有理有据 |
| **架构演进** | C.6 预留 LiteLLM Sidecar 扩展路径 | 前瞻性好 |

### 1.2 需求优先级合理性

PRD 的优先级分配符合「核心价值优先、体验渐进」原则：

| 优先级 | 数量 | 代表性需求 | 判断 |
|------|------|-----------|------|
| **P0** | 12 项 | FR-1.1~1.4（路由决策）、FR-2.1~2.2（双后端）、FR-3.1~3.2/3.4（管道适配）、FR-4.1（日志）、FR-5.1~5.4（配置）、FR-6.1（通知） | ✅ 合理 — 覆盖 MVP 最小闭环 |
| **P1** | 9 项 | FR-2.3~2.6（回退/降级）、FR-4.2~4.4（可观测性）、FR-5.5~5.6（并发/热重载）、FR-6.2/6.4（用户控制） | ✅ 合理 — 容错 + 可观测是上线前提 |
| **P2** | 4 项 | FR-5.7（Profile）、FR-6.3（Header 覆盖） | ✅ 合理 — 体验优化，可延后 |

### 1.3 需求风险点

| 风险 | 严重程度 | 说明 |
|------|---------|------|
| **隐私合规（NFR-6）** | 🟡 中 | NFR-6.3 敏感路径保护是「尽力而为」——仅扫描 tool_use 参数，不扫描自由文本。用户可能误以为「所有敏感数据都被保护」。需在产品文档/通知中明确此限制 |
| **成本模型偏差** | 🟡 中 | 月度 ¥2-5 基于 13% 路由率 × 40 请求/天。若用户使用频率显著更高（如全天候 Agent 循环），成本可能翻倍。`PROXY_ROUTE_DAILY_BUDGET` 安排在 Phase 3 实现，Phase 1-2 期间用户需自行监控 |
| **OOM 风险未完全消除** | 🟢 低 | 回退场景（云端失败→本地紧急截断）仍可能触发 OOM，但概率极低（需云端故障 + 本地上下文超载同时发生） |

---

## 2. 方案评估

### 2.1 方案成熟度：⭐⭐⭐⭐☆（良好→优秀）

设计文档 v2.10 经过三轮 Review 共 28 项问题的修正，已高度成熟：

| 维度 | 评分 | 说明 |
|------|------|------|
| **架构设计** | ⭐⭐⭐⭐⭐ | SmartRouter + BackendDispatcher 双后端 + Stage Skip 方案成熟 |
| **数据流设计** | ⭐⭐⭐⭐⭐ | 四层数据架构（配置/会话/请求/观测）+ 完整生命周期图 |
| **路由决策引擎** | ⭐⭐⭐⭐⭐ | 10 级决策矩阵、偏好非强制、安全优先 |
| **模型 ID 契约** | ⭐⭐⭐⭐⭐ | 三层标识体系（Agent-Facing/Response/Human-Facing）设计完备 |
| **可观测性** | ⭐⭐⭐⭐⭐ | 日志/status/metrics 三层 + 完整指标字典 |
| **配置体系** | ⭐⭐⭐⭐☆ | 15 个配置变量 + 3 个 Profile + SIGHUP 热重载 |
| **实施计划** | ⭐⭐⭐⭐☆ | 三阶段 42h 估算，任务分解到文件级 |
| **风险缓解** | ⭐⭐⭐⭐⭐ | 9 项风险 + 6 种失败场景 + 恢复路径 |

### 2.2 关键技术决策评估

| 决策 | 选择 | PM 判断 |
|------|------|---------|
| 回退在 BackendDispatcher 内部处理（方案 A） | ✅ 正确 | 修改面更小，避免 InstrumentedPipeline 框架改造风险 |
| Response model 回显 Agent 请求值（方案 B） | ✅ 正确 | 与 cc-router 生态惯例一致，避免 Anthropic SDK 校验失败 |
| Tier 路由从「强制」改为「偏好」 | ✅ 正确 | 安全（OOM 保护）永远优先于偏好 |
| 默认禁用路由（`PROXY_ROUTE_ENABLED=false`） | ✅ 正确 | 向后兼容，用户主动开启 |
| 默认使用 flash 模型 | ✅ 正确 | 成本优先，pro 模型作为 opus tier 的可选项 |

### 2.3 实现复杂度评估

| 模块 | 复杂度 | 新增/改动行数 | 风险 |
|------|--------|-------------|------|
| SmartRouter | 🟡 中 | ~150 行新增 | 决策矩阵逻辑需充分测试 |
| BackendDispatcher | 🔴 高 | ~80 行改动 | 双后端 + 回退 + 紧急截断，最容易出 bug |
| RouteNotification | 🟢 低 | ~40 行新增 | 独立 Stage，影响面小 |
| 4 个 Stage Skip | 🟢 低 | ~20 行改动 | 仅改 should_run，风险低 |
| FormatConverter | 🟢 低 | ~10 行改动 | 仅 model 字段选择逻辑 |
| /v1/models 改造 | 🟡 中 | ~60 行改动 | 需要确保不破坏现有 Agent 兼容性 |
| proxy_state.py | 🟡 中 | ~80 行新增 | 14 常量 + 4 dict + 锁边界需正确 |
| /status + admin endpoint | 🟢 低 | ~45 行新增 | 纯增量，不影响现有逻辑 |

---

## 3. 需求-方案覆盖矩阵

### 3.1 FR 覆盖度：100%（25/25）

| FR ID | 需求 | 设计方案 | 覆盖状态 |
|-------|------|---------|---------|
| FR-1.1 | 基于 chars 路由 | §4.1 Priority 7 | ✅ |
| FR-1.2 | 基于 stage 路由 | §4.1 Priority 8 | ✅ |
| FR-1.3 | Session 状态持久化 | §4.3 `_SESSION_ROUTE_MAP` | ✅ |
| FR-1.4 | 内存压力路由 | §4.1 Priority 6 | ✅ |
| FR-1.5 | 决策可追溯 | §5.1 日志 + §5.3 metrics | ✅ |
| FR-2.1 | 双后端连接管理 | §2.2 BackendDispatcher | ✅ |
| FR-2.2 | Session 内一致性 | §4.3 `_SESSION_ROUTE_MAP` + Priority 2/3 | ✅ |
| FR-2.3 | 云端回退 | §2.3 完整回退流程 | ✅ |
| FR-2.4 | 连续失败降级 | §4.3 冷却期 + `_record_cloud_failure` | ✅ |
| FR-2.5 | 并发控制分离 | §3.1 `_cloud_lock` + `_llama_lock` | ✅ |
| FR-2.6 | 双端不可用保护 | §8.1 Cloud+Local 双故障场景 | ✅ |
| FR-3.1 | 云端跳过截断 | §1.5 ContextTruncator/OOMSafetyFIFO skip | ✅ |
| FR-3.2 | 云端跳过压缩 | §1.5 ContentCompressor/CacheAligner skip | ✅ |
| FR-3.3 | 云端保留防御 | §1.3 循环/blocker 检测在云端继续执行 | ✅ |
| FR-3.4 | FormatConverter 适配 | §9.5.3 cloud model 选择 | ✅ |
| FR-4.1 | 路由决策日志 | §5.1 日志格式 | ✅ |
| FR-4.2 | /status 路由面板 | §5.2 完整面板设计 | ✅ |
| FR-4.3 | /metrics 路由统计 | §5.3 Metrics JSONL Schema + §2.5.4 指标字典 | ✅ |
| FR-4.4 | 成本估算 | §2.2 `_dispatch` + §2.5.4 actual_cost_total | ✅ |
| FR-5.1~5.7 | 配置管理 | §3 全部覆盖（含 Profile 系统） | ✅ |
| FR-6.1 | 路由通知 | §1.6 RouteNotification Stage | ✅ |
| FR-6.2 | 强制本地 | §4.3 `local_forced` + admin endpoint | ✅ |
| FR-6.3 | Header 覆盖 | §4.1 Priority 0.6 | ✅ |
| FR-6.4 | 路由状态可见 | §5.2 /status + §9.7.3 | ✅ |

### 3.2 NFR 覆盖度：100%（17/17）

| NFR ID | 需求 | 设计方案 | 覆盖状态 |
|--------|------|---------|---------|
| NFR-1.1 | 决策延迟 < 1ms | §4 纯内存判断，无 I/O | ✅ |
| NFR-1.2 | 禁用时零开销 | §4.1 Priority 0 直接返回 local | ✅ |
| NFR-1.3 | 回退延迟 | §2.3 回退流程 | ✅ |
| NFR-2.1 | 不增加 500 错误 | §8.1 失败场景表 | ✅ |
| NFR-2.2 | 云端不可用不阻塞 | §2.3 fallback + §8.1 | ✅ |
| NFR-2.3 | 无 ping-pong 切换 | §4.1 Priority 2/3 session 状态锁定 | ✅ |
| NFR-3.1 | API Key 脱敏 | `_mask_sensitive()` 复用 | ✅ |
| NFR-3.2 | 强制本地可用 | Priority 0 + `PROXY_ROUTE_ENABLED=false` | ✅ |
| NFR-4.1 | 路由逻辑独立 | SmartRouter 独立 Stage | ✅ |
| NFR-4.2 | 单元测试 ≥15 cases | §6 Phase 1: 15 cases + §9.8.3: 23 cases | ✅ |
| NFR-4.3 | 向后兼容 | §9.8.1 兼容性矩阵 | ✅ |
| NFR-5.1 | 默认低成本 | flash 模型 + §3.1 定价配置 | ✅ |
| NFR-5.2 | 月度 < ¥50 | §7.3 成本指标 | ✅ |
| NFR-6.1 | 首次启用告警 | §1.6 通知 + /status 红色提醒 | ✅ |
| NFR-6.2 | 数据上传日志 | §5.1 `[PRIVACY]` 日志 | ✅ |
| NFR-6.3 | 敏感路径保护 | §5.4 `_is_sensitive_request()` | ✅ |
| NFR-6.4 | 快速禁用 | SIGHUP reload | ✅ |

---

## 4. Review 问题收敛分析

### 4.1 三轮 Review 问题统计

| 轮次 | 日期 | P0 | P1 | P2 | 合计 | 当前状态 |
|------|------|----|----|-----|------|---------|
| 第一轮 | 06-21 | 5 | 3 | 2 | 10 | ✅ 已关闭（v2.3-v2.7 修正） |
| 第二轮 | 06-22 | 3 | 3 | 3 | 9 | ✅ 已关闭（v2.5-v2.7 修正） |
| 第三轮 | 06-22 model-id | 2 | 4 | 3 | 9 | ✅ 已关闭（v2.9-v2.10 修正） |
| **合计** | | **10** | **10** | **8** | **28** | ✅ **全部关闭** |

### 4.2 关键问题修正确认

| 原始问题 | 严重程度 | 修正版本 | 确认 |
|---------|---------|---------|------|
| BackendDispatcher fallback 方案歧义 | 🔴 P0 | v2.3 → 方案 A（内部重试） | ✅ |
| Emergency truncation 时机矛盾 | 🔴 P0 | v2.3 → 在 BackendDispatcher 内部调用 | ✅ |
| `PROXY_CLOUD_API_KEY` 默认值错误 | 🔴 P0 | v2.4 → 无默认值，必须显式配置 | ✅ |
| 决策矩阵优先级逻辑问题 | 🔴 P0 | v2.5 → 10 级优先级 | ✅ |
| Response model 方案 A 兼容风险 | 🔴 P0 | v2.9 → 方案 B（回显 + Header） | ✅ |
| `/v1/models` 暴露 MODEL_NAME | 🔴 P0 | v2.9 → 移除，改为稳定别名 | ✅ |
| Tier 强制路由安全矛盾 | 🟡 P1 | v2.9 → 改为偏好（阈值调整） | ✅ |
| FR-6.3 Header 覆盖缺失 | 🔴 P0 | v2.8 → Priority 0.6 | ✅ |
| FALLBACK_ENABLED 行为未定义 | 🔴 P0 | v2.7 → BackendDispatcher 内判断 | ✅ |

> **结论**: 设计文档 v2.10 已解决所有已知 Review 问题，具备进入开发的条件。

---

## 5. 工作量评估与排期

### 5.1 总工作量

| Phase | 开发 | 测试 | 文档 | 合计 | 累计 |
|-------|------|------|------|------|------|
| Phase 1: MVP | 12h | 8h | 2h | **22h** | 22h |
| Phase 2: 容错增强 | 8h | 2h | 1h | **11h** | 33h |
| Phase 3: 体验优化 | 3h | 4h | 2h | **9h** | 42h |
| **总计** | **23h** | **14h** | **5h** | **42h** | |

> 以上为纯编码时间。按单人开发、每日有效 4-5h 计算，总日历时间约 **9-11 个工作日**。

### 5.2 三阶段排期

```
Week 1 (6/23 - 6/27)                    Week 2 (6/30 - 7/4)                    Week 3 (7/7 - 7/11)
┌─────────────────────────┐    ┌─────────────────────────┐    ┌─────────────────────────┐
│     Phase 1: MVP        │    │   Phase 2: 容错增强      │    │   Phase 3: 体验优化      │
│                         │    │                         │    │                         │
│ Mon: config + state     │    │ Mon: fallback logic     │    │ Mon: cost calculation   │
│ Tue: SmartRouter        │    │ Tue: cooldown + memory  │    │ Tue: RouteNotification  │
│ Wed: BackendDispatcher  │    │ Wed: /status panel      │    │ Wed: /status trends     │
│ Thu: Stage skip + 集成  │    │ Thu: /metrics + 集成    │    │ Thu: A/B testing        │
│ Fri: 单元测试 + buffer  │    │ Fri: 集成测试 + buffer  │    │ Fri: 文档 + 上线准备     │
│                         │    │                         │    │                         │
│ ▼ Gate 1: MVP 验收      │    │ ▼ Gate 2: 容错验收      │    │ ▼ Gate 3: 全功能验收     │
└─────────────────────────┘    └─────────────────────────┘    └─────────────────────────┘
```

### 5.3 各阶段详细任务分解

#### Phase 1: MVP（Week 1, ~22h）— 可演示的最小闭环

| 序号 | 任务 | 文件 | 预估 | 优先级 |
|------|------|------|------|--------|
| 1.1 | 新增 `PROXY_ROUTE_*` 14 个常量 + `_cloud_lock` + 4 个 dict + `_RELOAD_SPEC` 条目 | `proxy_state.py` | 2h | P0 |
| 1.2 | 新增 `SmartRouter` Stage（150 行，含 10 级决策矩阵 + 偏好阈值调整） | `pipeline.py` | 3h | P0 |
| 1.3 | 改造 `BackendDispatcher` 双后端支持（base_url/api_key/lock 选择） | `pipeline.py` | 2h | P0 |
| 1.4 | 4 个 Stage 添加 `should_run` cloud skip（CacheAligner/ContentCompressor/ContextTruncator/OOMSafetyFIFO） | `pipeline.py` | 1h | P0 |
| 1.5 | `HighDropRatioNotice` 改为 `ConditionalStage` | `pipeline.py` | 0.5h | P0 |
| 1.6 | `_handle_messages` 集成 SmartRouter + RouteNotification + 双锁传递 | `anthropic_proxy.py` | 0.5h | P0 |
| 1.7 | 改造 `/v1/models`（稳定别名 + `_build_models_response` + 移除 MODEL_NAME） | `anthropic_proxy.py` | 1h | P0 |
| 1.8 | `RequestParser` 解析 `X-Proxy-Route-To` header + `agent_model_tier` | `pipeline.py` | 0.5h | P1 |
| 1.9 | `FormatConverter` model 字段适配（cloud 用 `_route_cloud_model`，local 用 `MODEL_NAME`） | `pipeline.py` | 0.5h | P0 |
| 1.10 | 配置示例追加到 `rapid-mlx-35b-opt.conf` | `configs/` | 0.5h | P0 |
| 1.11 | 单元测试 SmartRouter（15 cases：10 级优先级 + 5 个边界） | `test/unit/` | 3h | P0 |
| 1.12 | 单元测试 BackendDispatcher 双模式（8 cases） | `test/unit/` | 2h | P0 |
| 1.13 | 单元测试 `/v1/models` 改造（5 cases） | `test/unit/` | 1h | P0 |
| 1.14 | 单元测试 `MODEL_ROUTE_PREFERENCES` 阈值调整（4 cases） | `test/unit/` | 1h | P1 |
| 1.15 | 集成测试路由链（3 cases：正常路由/禁用路由/mock 云端 503） | `test/integration/` | 2h | P0 |
| 1.16 | 回归测试：所有现有测试在 `PROXY_ROUTE_ENABLED=false` 和 `true` 下通过 | `test/` | 1h | P0 |

#### Phase 2: 容错增强（Week 2, ~11h）

| 序号 | 任务 | 文件 | 预估 | 优先级 |
|------|------|------|------|--------|
| 2.1 | 云端失败回退逻辑（BackendDispatcher 内部 try/except + 紧急截断） | `pipeline.py` | 3h | P0 |
| 2.2 | 连续失败 Session 降级 + 冷却期定时器 | `pipeline.py` + `proxy_state.py` | 1h | P0 |
| 2.3 | 内存压力触发路由（`_get_system_memory` 集成） | `pipeline.py` | 1h | P0 |
| 2.4 | `/status` 路由面板（HTML 模板 + 统计数据） | `admin_server.py` | 3h | P1 |
| 2.5 | `/metrics` 路由统计（JSON endpoint） | `admin_server.py` | 1h | P1 |
| 2.6 | `manage.sh` 路由管理命令（`route-force-local`/`route-force-cloud`） | `manage.sh` | 0.5h | P1 |
| 2.7 | `admin_server.py` 新增 `POST /admin/route/force-local` 和 `/force-cloud` | `admin_server.py` | 0.5h | P1 |
| 2.8 | 集成测试回退链（3 cases：回退成功/回退禁用/连续失败冷却） | `test/integration/` | 2h | P1 |

#### Phase 3: 体验优化（Week 3, ~9h）

| 序号 | 任务 | 文件 | 预估 | 优先级 |
|------|------|------|------|--------|
| 3.1 | 成本估算（pre-request input 估算 + post-request 真实 usage 计算） | `pipeline.py` | 2h | P1 |
| 3.2 | RouteNotification Stage（首次路由 + 紧急回退两种模板） | `pipeline.py` | 1h | P0* |
| 3.3 | X-* Response Header 注入（X-Actual-Model/X-Route-Target/X-Route-Reason） | `pipeline.py` | 0.5h | P1 |
| 3.4 | `/status` 成本趋势展示 + daily budget 显示 | `admin_server.py` | 2h | P2 |
| 3.5 | `PROXY_ROUTE_DAILY_BUDGET` 实现（Phase 3） | `proxy_state.py` + `pipeline.py` | 1h | P2 |
| 3.6 | A/B 测试：路由 vs 纯本地（5 个场景） | `tools/` | 4h | P2 |
| 3.7 | 用户文档更新（CLAUDE.md / AGENTS.md / ../06-reference-metrics/TROUBLESHOOTING.md） | `docs/` | 2h | P2 |

> \* RouteNotification 是 P0 需求（FR-6.1），但可在 Phase 1 用简化版（仅日志），Phase 3 升级为完整消息注入。此处排入 Phase 3 是基于依赖关系（需要成本估算就绪后才能在通知中显示估算成本）。

### 5.4 各阶段验收门禁（Gate）

#### Gate 1: MVP 验收（Phase 1 结束）

| 验收项 | 标准 | 测量方式 |
|--------|------|---------|
| 基本路由功能 | `PROXY_ROUTE_ENABLED=true` + chars > 90K → 请求路由到云端 | 手动 E2E 测试 |
| 向后兼容 | `PROXY_ROUTE_ENABLED=false` → 行为与当前版本完全一致 | 回归测试套件全通过 |
| 路由决策准确 | 10 级决策矩阵全部命中正确 target | 单元测试 15 cases 通过 |
| 双后端调度 | local 走 rapid-mlx，cloud 走 DeepSeek API | 集成测试验证 |
| Stage Skip | 云端路径 4 个 Stage 正确跳过 | 单元测试 + pipeline_summary metrics |
| 无新增 500 错误 | 路由不引入新错误类别 | 回归测试 + 手动压测 |
| 模型列表稳定 | `/v1/models` 返回固定 Anthropic 别名，不含 MODEL_NAME | 单元测试验证 |

#### Gate 2: 容错验收（Phase 2 结束）

| 验收项 | 标准 | 测量方式 |
|--------|------|---------|
| 云端回退 | 云端 503 → 自动回退本地 + 紧急截断 | 集成测试（mock 后端） |
| 连续失败冷却 | 3 次失败 → 冷却 30min → 自动恢复 | 集成测试 |
| 回退禁用 | `FALLBACK_ENABLED=false` + 云端故障 → 返回 503 | 集成测试 |
| 内存压力路由 | used_pct > 90% + avail < 5GB → 云端 | 单元测试 |
| /status 面板 | 显示路由状态、统计、成本 | 手动验证 |
| 用户强制本地 | `route-force-local <session_id>` → 后续请求走本地 | 集成测试 |

#### Gate 3: 全功能验收（Phase 3 结束）

| 验收项 | 标准 | 测量方式 |
|--------|------|---------|
| 成本追踪 | /status 显示 pre-request 估算 + post-request 真实成本 | 手动 + metrics 验证 |
| 路由通知 | 首次云端切换时消息流注入通知 | E2E 测试 |
| A/B 对比 | 路由模式 vs 纯本地模式在 5 个场景下的指标对比 | `tools/bench_agent.py` |
| 文档完整 | 用户文档覆盖配置、使用、故障排查 | Review |

---

## 6. 风险登记册

### 6.1 开发风险

| ID | 风险 | 概率 | 影响 | 缓解措施 | 
|----|------|------|------|---------|
| DEV-1 | BackendDispatcher 回退实现引入死锁 | 低 | 高 | cloud_lock 和 llama_lock 不嵌套持有；充分单元测试锁顺序 |
| DEV-2 | 云端 API 响应格式与本地不一致导致解析错误 | 中 | 中 | FormatConverter 已做 Anthropic↔OpenAI 双向转换；DeepSeek API 此前已验证兼容 |
| DEV-3 | 现有 500+ 单元测试在路由启用后出现回归 | 低 | 中 | Phase 1 验收包含全量回归测试；先跑 `PROXY_ROUTE_ENABLED=false` 确认基线 |
| DEV-4 | rapid-mlx `max_tokens` bug 导致云端回退后本地 OOM | 中 | 中 | 紧急截断以 `PROXY_OOM_SAFE_CHARS` 的 50% 为目标；Phase 2 内存压力路由作为前置防护 |

### 6.2 上线风险

| ID | 风险 | 概率 | 影响 | 缓解措施 | 
|----|------|------|------|---------|
| OPS-1 | 用户未配置 `PROXY_CLOUD_API_KEY` 导致路由静默失败 | 中 | 中 | SmartRouter 检测到未配置时记录 ERROR + 强制 local + /status 显示警告 |
| OPS-2 | DeepSeek API 临时不可用导致大量回退 | 低 | 中 | 冷却期机制 + `FALLBACK_ENABLED` 开关 |
| OPS-3 | 月度费用超出用户预期 | 低 | 中 | /status 实时成本可见 + Phase 3 daily budget cap |
| OPS-4 | 用户不知道路由功能存在 | 中 | 中 | `/status` 提示 + 首次启动提示 + 文档引导 |

### 6.3 技术债风险

| ID | 风险 | 说明 | 偿还计划 |
|----|------|------|---------|
| TD-1 | `_cloud_lock` Semaphore 热重载 | SIGHUP 时需重建 Semaphore，与现有 `PROXY_MAX_CONCURRENT` 逻辑一致 | Phase 1 实现时同步处理 |
| TD-2 | Session 状态 dict 清理 | 4 个 dict 需统一 LRU 淘汰，在 `_classify_lifecycle_stage` 中附带清理 | Phase 2 |
| TD-3 | `get_model_aliases()` 缓存 | 每次 `/v1/models` 请求都重建列表 | Phase 1 实现即带缓存（§9.3.3） |

---

## 7. 里程碑与交付物

### 7.1 里程碑

| 里程碑 | 目标日期 | 入口条件 | 出口条件 |
|--------|---------|---------|---------|
| **M1: 开发启动** | 6/23（周一） | PRD + 设计文档通过评审 | — |
| **M2: MVP 完成** | 6/27（周五） | Phase 1 任务完成 | Gate 1 验收通过 |
| **M3: 容错完成** | 7/4（周五） | Phase 2 任务完成 | Gate 2 验收通过 |
| **M4: 全功能上线** | 7/11（周五） | Phase 3 任务完成 | Gate 3 验收通过 |

### 7.2 交付物清单

| 交付物 | 类型 | 里程碑 | 说明 |
|--------|------|--------|------|
| `proxy_state.py` 路由配置 | 代码 | M2 | 14 常量 + 锁 + 4 dict |
| `SmartRouter` Stage | 代码 | M2 | 150 行，10 级决策矩阵 |
| `BackendDispatcher` 双后端 | 代码 | M2 | 80 行改动 + 回退逻辑 |
| Cloud Stage Skip | 代码 | M2 | 4 个 Stage should_run |
| `/v1/models` 稳定别名 | 代码 | M2 | 移除 MODEL_NAME |
| 单元测试（≥32 cases） | 测试 | M2 | SmartRouter + BackendDispatcher + models + preferences |
| 集成测试（≥6 cases） | 测试 | M2-M3 | 路由链 + 回退链 |
| `/status` 路由面板 | 功能 | M3 | HTML 面板 |
| `/metrics` 路由统计 | 功能 | M3 | JSON endpoint |
| `manage.sh` 路由命令 | 工具 | M3 | route-force-local/cloud |
| 成本估算系统 | 功能 | M4 | pre + post request |
| RouteNotification | 功能 | M4 | 消息流通知 |
| A/B 测试报告 | 文档 | M4 | 5 场景对比 |
| 用户文档更新 | 文档 | M4 | CLAUDE.md/AGENTS.md/../06-reference-metrics/TROUBLESHOOTING.md |
| 配置示例文件 | 配置 | M2 | rapid-mlx-35b-opt.conf |

### 7.3 甘特图

```
                     Week 1 (6/23)    Week 2 (6/30)    Week 3 (7/7)
                     M  T  W  T  F    M  T  W  T  F    M  T  W  T  F
                     
Phase 1: MVP         ████████████████
  config + state     ████
  SmartRouter           ██████
  BackendDispatcher          ████
  Stage skip + 集成               ████
  单元测试                              ██
  Gate 1: MVP 验收                      ██

Phase 2: 容错增强                     ████████████████████
  fallback logic                      ██████
  cooldown + memory                        ██
  /status panel                               ██████
  /metrics + 集成                                  ████
  Gate 2: 容错验收                                  ██

Phase 3: 体验优化                                        ████████████████████
  cost calculation                                       ████
  RouteNotification                                          ██
  /status trends                                                ████
  A/B testing                                                        ████████
  Gate 3: 全功能验收                                                      ██
```

---

## 8. 决策与建议

### 8.1 Go/No-Go 决策

| 决策维度 | 状态 | 判断 |
|---------|------|------|
| 需求完整性 | PRD v1.4，25 FR + 17 NFR，三轮修订 | ✅ GO |
| 方案成熟度 | 设计 v2.10，三轮 Review 28 项问题全部关闭 | ✅ GO |
| 实现可行性 | 42h 工作量，stdlib-only，无外部依赖 | ✅ GO |
| 风险评估 | 9 项风险均有缓解措施 | ✅ GO |
| 资源可用性 | 单人开发，3 周日历时间 | ✅ GO |

**决策：✅ GO — 批准进入 Phase 1 开发**

### 8.2 对开发的建议

1. **Phase 1 优先实现 BackendDispatcher 双后端基础功能，回退逻辑推迟到 Phase 2。** Phase 1 目标是最小闭环 —— 能路由到云端并正确返回。回退逻辑复杂度高（紧急截断 + 锁顺序 + 冷却期），应在基础功能稳定后再加。

2. **RouteNotification 在 Phase 1 用简化版（仅日志），Phase 3 升级为消息流注入。** 消息流注入涉及 `ctx.messages.append()`，需要验证对 downstream stages（尤其是循环检测和 FormatConverter）的影响。

3. **所有 Session 状态 dict 的读写必须持有 `_state_lock`。** 当前 `_state_lock` 已保护 `_SESSION_REQUEST_COUNT` 等，路由新增的 4 个 dict 应纳入同一锁保护范围。

4. **Phase 1 验收必须包含 `PROXY_ROUTE_ENABLED=false` 下的全量回归测试。** 这是「不破坏现有功能」的唯一保证。

### 8.3 对产品的建议

1. **NFR-6.3 敏感路径保护的「尽力而为」性质需在用户文档中显著声明。** 避免用户误以为所有敏感数据（如粘贴到对话中的密钥文本）都被保护。

2. **建议在 Phase 2 上线后收集 1-2 周的真实路由数据，再决定是否需要调整默认阈值（90K）。** 13% 的路由率是基于 2,932 个样本的估算，实际使用模式可能有偏差。

3. **`PROXY_ROUTE_DAILY_BUDGET` 建议从 Phase 3 提前到 Phase 2。** 这是成本控制的关键开关，如果用户担心费用，应该在路由功能可用时就有预算保护。

4. **考虑在后续版本增加「路由决策预览」模式（`PROXY_ROUTE_DRY_RUN=true`）。** 用户可以先用 dry-run 模式观察「如果启用路由，哪些请求会被路由到云端」，再决定是否真正启用。降低启用门槛。

### 8.4 对运维的建议

1. **首次启用路由的用户应执行 `./manage.sh start-cloud` 确认云端 API Key 有效。** 避免路由触发时才发现 Key 未配置或无效。

2. **建议监控 `route_fallback_count` 指标，设置告警阈值（如单日 > 5 次回退）。** 回退意味着云端不可用，需要排查网络或 API Key 问题。

3. **定期 review `proxy_metrics.jsonl` 中的 cost 字段，与实际 DeepSeek 账单对账。** 确保成本估算准确。

---

> **评估版本**: v1.1
> **下次评审**: Phase 1 结束后进行中期评审（预计 6/27）
> **关联文档**:
> - PRD: `docs/01-requirements-product/PRD-intelligent-model-routing.md` v1.4
> - 设计: `docs/02-architecture-design/intelligent-model-routing-design.md` v2.11（已根据本评估建议更新 Phase 1-3 排期）
> - Review 1: `docs/02-architecture-design/intelligent-model-routing-design-review-2026-06-21.md`
> - Review 2: `docs/02-architecture-design/intelligent-model-routing-design-review-2026-06-22.md`
> - Review 3: `docs/02-architecture-design/intelligent-model-routing-design-review-2026-06-22-model-id.md`
>
> **v1.1 更新**: 设计文档 v2.10→v2.11 已同步更新：PROXY_ROUTE_DAILY_BUDGET Phase 3→Phase 2、Phase 1 补充缺失任务、Phase 2 新增 RouteNotification 完整实现 + manage.sh 命令、R2 风险引用修正、§1.6 分阶段实现说明
