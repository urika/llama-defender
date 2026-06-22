# PRD：智能模型路由 (Intelligent Model Routing)

> **文档版本**: v1.5 (第四轮 Review 修订 — 基于 2026-06-22 生产日志分析)
> **创建日期**: 2026-06-21
> **最后修订**: 2026-06-22
> **状态**: 草案 (Proposed)  
> **作者**: PM 视角系统分析  
> **关联文档**: `PRD-anthropic-proxy.md` v3.0、`DEFECT-LIST.md`、`PM-ANALYSIS-FUTURE-ROADMAP.md`  
> **设计文档**: `docs/02-architecture-design/intelligent-model-routing-design.md`（系统架构、数据流、配置、路由引擎、可观测性、实施计划、指标、风险、附录）

---

## 目录

1. [产品概述与愿景](#1-产品概述与愿景)
2. [用户分析](#2-用户分析)
3. [问题定义](#3-问题定义)
4. [功能需求](#4-功能需求)
5. [非功能需求](#5-非功能需求)

---

## 1. 产品概述与愿景

### 1.1 一句话定位

> 在现有双模式 Proxy 架构基础上，新增**请求级智能路由层**，根据上下文大小、内存压力、会话状态自动决策将请求发送到本地模型或云端 API，在 Apple Silicon 48GB 物理限制下实现**兼顾性能、可靠性与成本**的最优权衡。

### 1.2 核心价值主张

> **数据基线说明**：以下「纯本地模式（当前）」数据来源于 v0.5.7 rounds 策略 + smart-preserving Read 修复后（2026-06-14 至 06-21，2,932 请求样本）。「SATURATION 阶段 54%」为修复前数据（06-05 至 06-14），保留作为历史对比。路由价值的**首要驱动力**是消除长上下文的 OOM 风险和 TTFT 退化，循环注入率下降是附带收益。详见 [§附录 A](#附录-a-数据基线)。

| 维度 | 纯本地模式（当前 v0.5.7） | 智能路由（目标） | 主要/附带 |
|------|--------------------------|----------------|----------|
| **OOM 崩溃风险** | 长上下文偶发 OOM | 路由到云端后 OOM 风险消除 | ⭐ 主要 |
| **长上下文 TTFT** | 28s+ (无 prefix cache) | 1-5s (云端 API) | ⭐ 主要 |
| **循环注入率** | 全量 9%；历史 SATURATION 阶段曾达 54%（修复前） | 云端处理的长上下文请求中 <2%；整体 Session 下降 50%+（目标 <5%） | 🟡 附带 |
| **上下文丢失** | 0%（v0.5.7 rounds 策略已消除 high_drop_ratio） | 保持 0% | 维持 |
| **月度成本** | ¥0 | **¥2-5**（flash 默认场景）；极端上限 ¥34（pro + 高频）[→成本模型](#附录-b-成本模型) | ⚠️ 权衡 |
| **数据隐私** | 全本地 | **87% 请求保留本地**，13% 传云端 [→数据基线](#附录-a-数据基线) | ⚠️ 权衡 |

### 1.3 产品边界

**做什么**：
- ✅ 基于上下文字符数的自动路由决策
- ✅ 跨 Session 路由状态管理
- ✅ 云端 API 不可用时的自动回退
- ✅ 路由事件的可观测性（日志、metrics、 /status）
- ✅ 用户可配置的路由阈值和云端模型选择

**不做什么**：
- ❌ 不在同一个 Session 内反复切换（切换到云端后保持）
- ❌ 不做基于任务语义的智能路由（如「写代码用 DeepSeek，写文档用 Qwen」）
- ❌ 不替代 LiteLLM 的全功能路由网关（详见 [§附录 C LiteLLM 替代方案评估](#附录-c-litellm-替代方案评估)）
- ❌ 不引入第三方路由服务依赖（保持纯 stdlib）

---

## 2. 用户分析

### 2.1 用户画像

**主要用户**: 在 Apple Silicon (M5 Pro, 48GB) 上使用 Claude Code 进行 Agentic 编程的开发者。

| 属性 | 描述 |
|------|------|
| **技术能力** | 熟练使用命令行、Git、Python；理解 LLM 基本概念 |
| **工作模式** | 单会话持续 30+ 分钟，20-100+ 轮请求 |
| **核心场景** | 代码读写、重构、调试、架构设计 |
| **痛点** | 长会话后期模型「失忆」、OOM 崩溃、循环死锁 |
| **约束** | 48GB 统一内存无法升级；对成本敏感但不排斥少量 API 费用 |

### 2.2 用户故事

#### US-1: 长会话自动保护（P0）

> **作为** 开发者在进行复杂的多文件重构任务，**我希望** 当会话上下文超过本地模型安全范围时，系统自动切换到云端模型，**以便** 我不需要手动感知和管理这一过程，任务能持续执行不被 OOM 或循环中断。

**验收标准**：
- 当 total_chars > PROXY_ROUTE_THRESHOLD_CHARS 时，请求自动路由到云端
- 切换后 Session 内后续请求保持在云端
- **切换后下一条 assistant 消息前自动注入通知**：`[System: Switched to cloud model — context {n} chars exceeds local {threshold} limit. Estimated cost ~¥{cost}/request. Use `./manage.sh route-force-local {session_id}` to return to local.]`
- 用户可在 /status 页面看到路由状态和估算成本
- OOM 事件从当前水平降至 0

#### US-2: 新会话回到本地（P0）

> **作为** 开启新会话的开发者，**我希望** 系统自动从云端切回本地模型，**以便** 新任务在短上下文阶段享受零成本、低延迟的本地推理。

**验收标准**：
- 新 Session（session_id 变更）的初始请求回到本地模型
- 切回过程对用户透明，无需任何操作
- 本地模型不会「继承」上一个 Session 的云端行为模式

#### US-3: 成本感知与预算告警（P1）

> **作为** 对 API 费用敏感的独立开发者，**我希望** 在 /status 页面看到当前 Session 使用了多少次云端请求、估算费用以及预算消耗比例，**以便** 我能根据预算调整路由阈值或选择更便宜的云端模型；当预算接近上限时，系统应主动告警，避免账单惊喜。

**验收标准**：
- /status 页面显示：cloud_requests_count、estimated_cost、daily_budget、budget_used_pct、cloud_model_name
- 当 `PROXY_ROUTE_DAILY_BUDGET > 0` 时，/status 按预算消耗比例显示分级告警：
  - 50%：🟡 黄色提示
  - 80%：🟠 橙色告警
  - 100%：🔴 红色告警，并可选自动禁用云端路由（`PROXY_ROUTE_DAILY_BUDGET_HARD_STOP=true`）
- /metrics JSON endpoint 包含路由统计：route_local_count / route_cloud_count
- 用户可通过 PROXY_CLOUD_MODEL 选择 flash (省钱) 或 pro (追求质量)

#### US-4: 云端不可用时回退（P1）

> **作为** 在网络不稳定环境工作的开发者，**我希望** 当云端 API 不可用时系统能自动回退到本地模型，**以便** 任务不会被网络问题阻塞。

**验收标准**：
- 云端返回 HTTPError 时，BackendDispatcher 自动 fallback 到本地
- 回退事件记录在 metrics 中 (route_fallback_count)
- 连续 3 次云端失败后，当前 Session 标记为 local_only

#### US-5: 强制本地模式（P2）

> **作为** 处理敏感代码的开发者，**我希望** 能临时关闭路由功能强制使用本地模型，**以便** 敏感数据不会传输到云端。

**验收标准**：
- PROXY_ROUTE_ENABLED=false 时所有请求走本地
- 切换模式通过 SIGHUP reload 即可生效，无需重启

### 2.3 用户旅程图

```
┌──────────────────────────────────────────────────────────────────────┐
│                    用户旅程：首次启用 → 触发 → 回退                     │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ① 启用路由                                                          │
│    用户编辑 config → PROXY_ROUTE_ENABLED=true                         │
│    执行 ./manage.sh reload                                            │
│    /status 显示 ⚠️ "路由已启用 — 长上下文请求将发送至 cloud API"        │
│    proxy 日志 WARN: [PRIVACY] Routing enabled, cloud: api.deepseek.com│
│                                                                      │
│  ② 正常使用 (87% 请求)                                                │
│    上下文 < 90K chars → 全部走本地                                    │
│    用户无感知                                                         │
│    /status: "当前目标: 🖥️ Local"                                      │
│                                                                      │
│  ③ 首次触发路由                                                       │
│    上下文增长至 127,843 chars > 90,000 阈值                            │
│    SmartRouter 决策 → cloud                                           │
│    日志: [smart_router] cloud (chars_exceed_threshold: 127843 > 90000)│
│    下一条 assistant 消息前注入:                                        │
│      "[System: Switched to cloud model — context 127,843 chars        │
│       exceeds local 90,000 limit. Estimated cost ~¥0.10/request.]"    │
│    /status 更新: "当前目标: ☁️ Cloud (deepseek-v4-flash)"              │
│                                                                      │
│  ④ 云端执行 (后续请求)                                                │
│    Session 内后续请求保持云端                                          │
│    无截断、无压缩、无 OOM 风险                                         │
│    /status 显示实时成本累计                                            │
│                                                                      │
│  ⑤ 云端故障回退                                                       │
│    云端 API 返回 503 / 网络超时                                         │
│    日志: "Cloud API failed, falling back to local"                      │
│    自动回退到本地 + 触发 EMERGENCY TRUNCATION：                          │
│      - 以 PROXY_OOM_SAFE_CHARS 的 50% 为截断目标（默认 100K chars）      │
│      - 或保留最近 3 轮 assistant 完整上下文（取两者较小值）               │
│      - 注入 high_drop_ratio 通知                                        │
│    用户收到: "[System: Cloud API unavailable, emergency fallback to       │
│              local. Context severely truncated to prevent OOM            │
│              (kept last 3 rounds / ~100K chars). Consider /compact or     │
│              retry when cloud recovers.]"                                │
│    /status: "当前目标: 🖥️ Local (回退，紧急截断)"                         │
│                                                                      │
│  ⑥ 新 Session 切回本地                                                │
│    用户开新 Claude Code 会话                                          │
│    session_id 变更 → _SESSION_ROUTE_MAP 新条目                        │
│    上下文短 → 自动回到本地                                            │
│    用户无感知                                                         │
│                                                                      │
│  ⑦ 手动强制本地（敏感代码场景）                                        │
│    用户执行: ./manage.sh route-force-local <session_id>               │
│    或: PROXY_ROUTE_ENABLED=false && ./manage.sh reload               │
│    /status 显示: "路由状态: ❌ 已禁用"                                  │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.4 用户控制机制

| 控制方式 | 优先级 | 说明 |
|---------|--------|------|
| **路由事件主动通知** | P0 | 首次路由到云端时，在 assistant 消息前注入 `[System: Switched to cloud model...]` 通知 |
| **手动强制本地 (Session 级)** | P1 | `./manage.sh route-force-local <session_id>` 将当前 Session 标记为 `local_forced` |
| **单次请求覆盖 Header** | P2 | `X-Proxy-Route-To: local` 或 `cloud`，允许 curl 测试或自定义客户端单次覆盖路由决策 |

---

## 3. 问题定义

### 3.1 核心问题：48GB 物理约束 vs Agentic 长会话

```
┌─────────────────────────────────────────────────────────┐
│                    Apple Silicon 48GB                    │
│  ┌──────────┐  ┌───────────┐  ┌──────────────────────┐ │
│  │ 模型权重  │  │ KV Cache  │  │ Prefix Cache + 系统  │ │
│  │ 14-18 GB │  │  6-10 GB  │  │ 6-8 GB              │ │
│  └──────────┘  └───────────┘  └──────────────────────┘ │
│                        剩余可用: ~12-18 GB               │
│                        (需容纳 Prefill Activations)     │
└─────────────────────────────────────────────────────────┘
```

当 Agentic 会话超过 90K chars (SATURATION 阶段) 时：
- TTFT 线性增长至 28s+
- 循环注入率升至 54%
- OOM 风险显著升高
- 代理层被迫截断 → 上下文丢失 → 模型「失忆」→ 重读循环

### 3.2 当前缓解手段的局限

| 缓解手段 | 效果 | 副作用 |
|---------|------|--------|
| Tool Clearing | 减少字符 | A/B 实验证明反而增加 19% 请求量 (模型失忆补偿性重读) |
| Context Truncation | 控制上下文 | 丢失历史信息，模型行为退化 |
| OOM Safety FIFO | 防崩溃 | 丢弃大量消息，上下文完整性受损 |
| 循环检测干预 | 打断死循环 | 只治标不治本 (21% 请求仍触发) |
| Dynamic max_tokens | 降低输出 | 限制模型表达能力 |

**核心矛盾**：所有缓解手段都在做「减法」——减少传递的信息。但 Agentic 工作流需要「完整」的上下文。

### 3.3 为什么路由是更好的解

```
当前路径:  请求 → 压缩/清除 → 截断/丢弃 → 模型(部分信息)→ 循环/失忆

路由路径:  请求 → 判断上下文 → 本地(完整) 或 云端(完整)
                ├─ 短上下文 → 本地模型 (完整信息 + 零成本 + 低延迟)
                └─ 长上下文 → 云端模型 (完整信息 + 无OOM + 无截断 + 低成本)
```

**路由消除了「减法」的必要性**：当请求不长时本地处理，当请求过长时直接交给不受 48GB 限制的云端，不需要做任何妥协。

---

## 4. 功能需求

### FR-1: 路由决策引擎

| ID | 需求 | 优先级 | 说明 |
|----|------|--------|------|
| FR-1.1 | 基于上下文字符数的路由 | P0 | 当 PipelineContext.total_chars > PROXY_ROUTE_THRESHOLD_CHARS 时路由到云端 |
| FR-1.2 | 基于生命周期阶段的路由 | P0 | 当 stage_config.stage ∈ {saturation, oom_danger, pre_trunc} 时触发路由 |
| FR-1.3 | Session 路由状态持久化 | P0 | 一旦 Session 路由到云端，后续请求保持云端；新 Session 重新从本地开始 |
| FR-1.4 | 内存压力触发路由 | P0 | 当系统 used_pct > 90% 且 available_gb < 5 时，即使 chars 未达阈值也路由到云端。内存压力是 OOM 的直接前兆，与上下文字符数是正交信号 |
| FR-1.5 | 路由决策可追溯 | P1 | 每次路由决策输出日志记录原因：chars/memory/session_state |

### FR-2: 双后端调度

| ID | 需求 | 优先级 | 说明 |
|----|------|--------|------|
| FR-2.1 | 双后端连接管理 | P0 | 本地和云端各维护独立的 base_url、api_key、model_name、并发锁 |
| FR-2.2 | Session 内后端一致性 | P0 | 同一 Session 内不切换后端（云端→云端，本地→本地） |
| FR-2.3 | 云端回退到本地 | P1 | 云端 HTTPError 时自动 fallback 到本地，最多重试 1 次。回退到本地时若上下文超过本地安全阈值（PROXY_OOM_SAFE_CHARS），触发 EMERGENCY TRUNCATION：以 PROXY_OOM_SAFE_CHARS 的 50% 为截断目标（默认 100K chars），或保留最近 3 轮 assistant 完整上下文（取较小值），并注入 `[System: emergency truncation...]` 通知 |
| FR-2.4 | 连续失败自动降级 | P1 | 连续 3 次云端失败后进入冷却期（默认 30 分钟），冷却期内强制本地。冷却期到期后自动恢复尝试云端 1 次 |
| FR-2.5 | 并发控制分离 | P1 | 本地 semaphore (PROXY_MAX_CONCURRENT) 和云端 semaphore (PROXY_ROUTE_CLOUD_CONCURRENT) 独立管理 |
| FR-2.6 | 双端不可用保护 | P1 | 当云端不可用且本地也不可用（后端未启动、OOM 中）时，返回 503 + Retry-After，不阻塞请求队列 |

### FR-3: 管道适配

| ID | 需求 | 优先级 | 说明 |
|----|------|--------|------|
| FR-3.1 | 云端模式跳过截断 | P0 | 路由到云端时，ContextTruncator 和 OOMSafetyFIFO 自动跳过 |
| FR-3.2 | 云端模式跳过压缩 | P0 | 路由到云端时，ContentCompressor 的 Tool Clearing 和语义压缩自动跳过 |
| FR-3.3 | 云端模式保留防御 | P1 | 即使路由到云端，循环检测、blocker 检测、错误翻译仍然执行 (云端模型也可能出错) |
| FR-3.4 | FormatConverter 适配 | P0 | 路由到云端时，openai_body.model 使用 PROXY_CLOUD_MODEL |

### FR-4: 可观测性

| ID | 需求 | 优先级 | 说明 |
|----|------|--------|------|
| FR-4.1 | 路由决策日志 | P0 | 每次路由决策记录: target、reason、chars、stage、session_id |
| FR-4.2 | /status 路由面板 | P1 | 显示: route_enabled、current_session_target、cloud_requests_total、estimated_cost、cloud_key_configured；当 cloud_key_configured=false 且 route_enabled=true 时显示红色告警条 |
| FR-4.3 | /metrics 路由统计 | P1 | 新增 metrics 字段: route_target、route_fallback、route_cloud_model |
| FR-4.4 | 成本估算 | P1 | 基于 deepseek 公开定价 × 估算的 input/output tokens 计算 per-request 成本。区分 pre-request 估算（input tokens + max_tokens 上限）和 post-request 真实成本（后端 response usage 字段）。成本单价通过 `PROXY_CLOUD_PRICE_INPUT`/`PROXY_CLOUD_PRICE_OUTPUT` 配置化 |
| FR-4.5 | 成本预算告警 | P1 | 当 `PROXY_ROUTE_DAILY_BUDGET > 0` 时，/status 页面按累计云端成本占预算比例显示分级告警；支持可选的硬停止（hard stop）：预算耗尽后该 Session 的新请求强制路由到 local，直至次日预算重置 |

### FR-5: 配置管理

| ID | 需求 | 优先级 | 说明 |
|----|------|--------|------|
| FR-5.1 | 可配置路由阈值 | P0 | PROXY_ROUTE_THRESHOLD_CHARS (默认 90000) |
| FR-5.2 | 路由开关 | P0 | PROXY_ROUTE_ENABLED (默认 false，向后兼容) |
| FR-5.3 | 云端模型选择 | P0 | PROXY_CLOUD_MODEL (默认 deepseek-v4-flash) |
| FR-5.4 | 云端 API 配置 | P0 | PROXY_CLOUD_BASE_URL (默认 https://api.deepseek.com/v1) |
| FR-5.5 | 云端并发控制 | P1 | PROXY_ROUTE_CLOUD_CONCURRENT (默认 2) |
| FR-5.6 | SIGHUP 热重载 | P1 | 所有 PROXY_ROUTE_* 配置支持热重载 |
| FR-5.7 | 路由配置 Profile（Phase 2） | P2 | 提供预设配置组合减少用户调参负担：`safe`（阈值 90K + flash + 冷却 30min）、`balanced`（阈值 60K + flash + 冷却 15min）、`cost-aware`（阈值 120K + flash + 冷却 60min）。通过 `PROXY_ROUTE_PROFILE` 一键切换，高级用户仍可逐参数覆盖 |
| FR-5.8 | 预算硬停止开关 | P1 | `PROXY_ROUTE_DAILY_BUDGET_HARD_STOP`（默认 false）：当单日成本达到 `PROXY_ROUTE_DAILY_BUDGET` 后，该 Session 的新请求强制路由到 local，次日 00:00 自动恢复 |

### FR-6: 用户控制

| ID | 需求 | 优先级 | 说明 |
|----|------|--------|------|
| FR-6.1 | 路由事件主动通知 | P0 | 首次路由到云端时，在下一条 assistant 消息前自动注入 `[System: Switched to cloud model...]` 通知，包含切换原因、上下文大小、估算成本 |
| FR-6.2 | Session 级强制本地 | P1 | 通过 `./manage.sh route-force-local <session_id>` 将当前 Session 标记为 `local_forced`，后续请求全部走本地 |
| FR-6.3 | 单次请求覆盖 Header | P2 | 支持 `X-Proxy-Route-To: local` 或 `cloud` header，允许 curl 测试或自定义客户端单次覆盖路由决策。**单次覆盖不写入 `_SESSION_ROUTE_MAP`**（不改变 Session 路由状态），后续请求仍按原有路由逻辑决策。注意：Claude Code 不会主动发送此 header，此功能主要面向调试和自定义客户端场景 |
| FR-6.4 | 路由状态可见 | P1 | /status 页面实时显示：路由状态、当前 Session 后端、切换原因、累计云端请求数和估算成本 |

---

## 5. 非功能需求

### NFR-1: 性能

| ID | 需求 | 指标 |
|----|------|------|
| NFR-1.1 | 路由决策延迟 | < 1ms（纯内存判断，无 I/O） |
| NFR-1.2 | 路由对管道的影响 | 不路由时（PROXY_ROUTE_ENABLED=false）零开销 |
| NFR-1.3 | 云端回退延迟 | 回退总延迟 = 云端超时 (PROXY_BACKEND_TIMEOUT) + 本地处理时间 |

### NFR-2: 可靠性

| ID | 需求 | 指标 |
|----|------|------|
| NFR-2.1 | 路由不会增加 500 错误率 | 验收: 500 错误率 ≤ 纯本地模式 |
| NFR-2.2 | 云端不可用不阻塞 | 回退到本地后正常完成请求 |
| NFR-2.3 | Session 路由状态一致性 | 同一 Session 内不出现 ping-pong 切换 |

### NFR-3: 安全性

| ID | 需求 | 指标 |
|----|------|------|
| NFR-3.1 | API Key 不泄露 | PROXY_CLOUD_API_KEY 在日志中脱敏 (复用 _mask_sensitive) |
| NFR-3.2 | 强制本地模式可用 | PROXY_ROUTE_ENABLED=false 时 100% 请求走本地 |

### NFR-4: 可维护性

| ID | 需求 | 指标 |
|----|------|------|
| NFR-4.1 | 路由逻辑独立 | 新增 SmartRouter Stage，不修改现有 22 个 Stage 的核心逻辑 |
| NFR-4.2 | 单元测试覆盖 | SmartRouter 测试覆盖所有决策路径 (≥15 cases) |
| NFR-4.3 | 向后兼容 | PROXY_ROUTE_ENABLED=false 时行为与当前版本完全一致 |

### NFR-5: 成本

| ID | 需求 | 指标 |
|----|------|------|
| NFR-5.1 | 默认低成本 | 默认使用 deepseek-v4-flash（输入¥0.5/M, 输出¥1.5/M tokens） |
| NFR-5.2 | 月度成本可控 | 预估月度成本 < ¥50（基于 13% 路由率 × 每日 40 次请求） |

### NFR-6: 隐私与合规

| ID | 需求 | 指标 |
|----|------|------|
| NFR-6.1 | 首次启用告警 | PROXY_ROUTE_ENABLED=true 首次生效时，/status 页面顶部显示红色提醒条：`⚠️ 智能路由已启用 — 长上下文请求将发送至云端 API: {cloud_base_url}`，proxy 日志输出 WARN 级别隐私提示 |
| NFR-6.2 | 数据上传日志 | 路由到云端时，日志记录 `[PRIVACY] Routing to cloud: {base_url} (model: {model}, chars: {n}, session: {id})` |
| NFR-6.3 | 敏感路径保护（尽力而为） | 通过 `PROXY_ROUTE_SENSITIVE_PATTERNS` 配置包含 `.env`/`.secret`/`credentials`/`id_rsa` 等关键词的文件路径正则列表。**仅检查 tool_use 参数中的 `file_path`/`path` 字段，不扫描自由文本内容**。命中时请求强制走本地。若命中敏感路径且上下文已超载，返回 403 + 提示用户手动处理，而非直接本地硬跑导致 OOM。此机制为**尽力而为**，不能保证识别所有敏感数据（如粘贴到对话中的密钥文本） |
| NFR-6.4 | 快速禁用路由 | `PROXY_ROUTE_ENABLED=false` 通过 `./manage.sh reload` 即时生效，所有后续请求回到本地 |

---

## 附录 A: 数据基线

> 以下数据基于 `logs/proxy_metrics.jsonl` 的 2,932 个真实请求。采样跨度两个时期，代理版本和策略不同，**直接对比需注意时效性**。

### A.0 基线时效性说明

| 时期 | 代理版本 | 截断策略 | 代表场景 |
|------|---------|---------|---------|
| **06-05 ~ 06-14（修复前）** | v0.5.0-baseline ~ v0.5.2 | FIFO | 历史基线，用于说明「为什么需要路由」 |
| **06-14 ~ 06-21（当前）** | v0.5.3+ rounds + smart-preserving Read | rounds | 当前基线，路由价值主张应基于此 |

> ⚠️ **重要**：SATURATION 阶段 54% 循环注入率来自修复前数据。v0.5.7 rounds 策略已将全量循环注入率降至 9%，且 high_drop_ratio 降为 0%。**路由的首要价值是消除长上下文 OOM 风险和 TTFT 退化**，循环注入率改善是附带收益。

### A.1 路由率估算依据

| 生命周期阶段 | chars 范围 | 请求占比 | 来源 |
|-------------|-----------|---------|------|
| INIT | < 15K | ~30% | proxy_metrics.jsonl lifecycle_stage 分布 |
| GROWTH | 15-40K | ~28% | 同上 |
| EXPANSION | 40-90K | ~29% | 同上 |
| **SATURATION** | **90-180K** | **~7%** | ← 路由候选区间 |
| **OOM_DANGER** | **180-350K** | **~5%** | ← 路由候选区间 |
| **PRE_TRUNC** | **> 350K** | **~1%** | ← 路由候选区间 |
| **路由触发率合计** | | **~13%** | SATURATION + OOM_DANGER + PRE_TRUNC |

### A.2 循环注入率

| 数据来源 | 时期 | 全量 | EXPANSION | SATURATION | 备注 |
|---------|------|------|-----------|------------|------|
| 长上下文验证报告 | 06-05~06-14 | 26% | 26% | **54%** | ⚠️ 修复前数据，FIFO 策略 |
| 长上下文验证报告 | 06-14~06-21 | **9%** | — | — | ✅ 当前基线，rounds 策略 |
| 路由目标（Phase 1） | — | **<5%** | — | **<2%（云端请求）** | 云端模型循环倾向远低于本地 |

### A.3 上下文丢失率 (high_drop_ratio)

| 时期 | 触发比例 | 备注 |
|------|---------|------|
| 06-05~06-14 | 8.7% (99/1,139) | 修复前，FIFO 策略 |
| 06-14~06-21 | **0%** | ✅ 当前基线，rounds + smart-preserving Read |

> 由于当前 high_drop_ratio 已为 0%，路由对此指标的边际改善为 0。路由的价值不在此维度。

### A.4 TTFT 基线

| 上下文大小 | 本地模型 (Qwen35B, rapid-mlx) | 云端 API (DeepSeek) |
|-----------|------------------------------|-------------------|
| < 5K chars | 0.29s (prefix cache hit) | 1-3s |
| 38K chars | ~28s (无 prefix cache) | 2-5s |
| > 90K chars | 30-60s (SATURATION+) | 3-8s |

> 数据来源：`tools/bench_perf.py` + `docs/03-experiments-testing/DEEPSEEK-AB-EXPERIMENT-GUIDE.md` 架构对比。TTFT 是路由的**首要价值驱动力**。

---

## 附录 B: 成本模型

### B.1 月度成本计算公式

```
月度成本 = Σ(每次云端请求成本)

单次成本 = (input_tokens × PRICE_INPUT + output_tokens × PRICE_OUTPUT) / 1,000,000

其中:
  input_tokens   = total_chars / token_ratio           ← pre-request 估算
  output_tokens  = actual usage.completion_tokens      ← post-request 真实值（来自后端 response.usage）
  token_ratio    = PROXY_CTX_TOKEN_RATIO (默认 2.0)

注意: pre-request 阶段 output_tokens 不可预知，仅估算 input cost。
       post-request 阶段从后端 response 中提取真实 usage，补算 output cost。
       /status 显示的 estimated_cost: pending 请求显示 input 估算，已完成请求显示真实总成本。

配置:
  PROXY_CLOUD_PRICE_INPUT   = 0.5    (¥/M tokens, deepseek-v4-flash)
  PROXY_CLOUD_PRICE_OUTPUT  = 1.5    (¥/M tokens, deepseek-v4-flash)
```

### B.2 典型请求成本估算

> 以下为 pre-request input 估算 + 假设 output 的场景。实际 output tokens 因任务而异（代码生成通常 < 2K tokens，长文本生成可能 > 8K）。

| 模型 | 输入 tokens | 假设输出 tokens | 单次成本 | 日成本 (40 请求/天 × 13%) | 月成本 (22 天) |
|------|------------|----------------|---------|--------------------------|---------------|
| **deepseek-v4-flash** (默认) | 15,000 | 2,000 | **¥0.01** | ¥0.05 | **¥1.16** |
| **deepseek-v4-flash** (保守) | 30,000 | 4,000 | **¥0.02** | ¥0.11 | **¥2.42** |
| **deepseek-v4-flash** (高估) | 60,000 | 8,000 | **¥0.04** | ¥0.22 | **¥4.84** |
| **deepseek-v4-pro** (对比) | 15,000 | 2,000 | ¥0.05 | ¥0.24 | **¥5.20** |

> **结论**: 默认 flash 模型下，月度成本即使保守高估也不超过 ¥5。PRD 正文 ¥2-5 对应 flash 默认到保守场景；¥34 为 pro 模型 + 高频使用（80 请求/天 × 20% 路由率）的**极端上限**，作为用户预算参考的 worst case。

### B.3 成本控制开关

| 机制 | 效果 |
|------|------|
| `PROXY_ROUTE_ENABLED=false` | 零成本（100% 本地） |
| `PROXY_CLOUD_MODEL=deepseek-v4-flash` | 最低单价 |
| `PROXY_ROUTE_THRESHOLD_CHARS` 调高 | 减少路由触发 |
| `/status` 实时成本可见 | 用户主动干预 |

---

## 附录 C: LiteLLM 替代方案评估

> 调研问题：如果直接使用 LiteLLM（成熟的开源 LLM 网关，9k+ GitHub Stars）进行智能路由，是否可以替代当前自建方案？
>
> 调研方法：阅读 LiteLLM 官方文档（Routing、Proxy、Call Hooks、Guardrails），对比 4 种架构方案。

### C.1 LiteLLM 功能画像

**LiteLLM 能做的（与我们场景相关）**：

| 能力 | 成熟度 | 说明 |
|------|--------|------|
| 路由策略 | ⭐⭐⭐⭐⭐ | 6 种内置：simple-shuffle、latency-based、cost-based、usage-based、least-busy、custom |
| 上下文窗口路由 | ⭐⭐⭐⭐ | 内置 `context_window_fallback` — 模型 context window 不够时自动切换 |
| Anthropic API 兼容 | ⭐⭐⭐⭐ | Proxy 模式原生支持 Anthropic↔OpenAI 双向转换 |
| 本地后端支持 | ⭐⭐⭐⭐ | `openai/` 前缀 + `api_base` 配置对接任何 OpenAI 兼容后端 |
| 请求拦截 Hook | ⭐⭐⭐⭐⭐ | `async_pre_call_hook` — 可修改 messages、切换 model、拒绝请求 |
| 回退/重试/冷却 | ⭐⭐⭐⭐⭐ | `num_retries`、`fallbacks`、`cooldown_time`、`allowed_fails` |
| 上下文管理 | ⭐⭐⭐ | 内置 `clear_tool_uses`（工具结果清理）和 `compact`（对话摘要压缩） |
| 成本追踪 | ⭐⭐⭐⭐ | 内置 cost map，按 token 计费，支持自定义价格 |

**LiteLLM 不能做的（我们需要但缺失的）**：

| 能力 | 缺失原因 | 对本项目的影响 |
|------|---------|--------------|
| Apple Silicon OOM 防护 | LiteLLM 是云 API 管理工具，不感知本地 Metal 内存压力 | **高** — 无基于内存压力的路由触发 |
| Qwen 工具调用修复 | 不原生支持模型特定的 workaround（XML→JSON fallback、boolean 强制转换、content-text 提取）。若要在 LiteLLM 中实现，需通过 CustomLogger/Hook 自行开发，其复杂度不亚于维护当前 Proxy | **高** — Qwen Write 缺 content、Edit 字符串化等随机失败 |
| 循环检测 + 3 级干预 | Guardrail 可单次拒绝，但无「检测历史→升级干预→跨请求持久化」机制 | **高** — 死亡循环无法打破 |
| Blocker 检测 | 无连续同错误类型的追踪 | **中** — 反复重试同一失败路径 |
| Session 级本地/云端路由一致性 | LiteLLM 的 `session_affinity` 主要保持同一云端部署的亲和性，不支持基于上下文长度在本地与云端之间做 Session 级 sticky 路由决策 | **中** — 同 Session 出现 ping-pong 切换 |
| Read 结果智能保护 | context_management 是通用清理，不区分 Read/Write/Bash 优先级 | **中** — Read 结果被错误清理 |
| 前缀缓存稳定化 | 不做 date normalization、system message 规范化 | **中** — 本地 prefix cache 命中率更低 |
| 错误翻译 | 不做后端错误→中文自然语言的提示翻译 | **低** — Qwen 可能不理解英文错误 |
| 纯 stdlib 约束 | 需 `pip install litellm`（依赖 20+ 第三方包），打破单文件部署 | **低** — 增加部署和审计复杂度 |

### C.2 四种架构方案对比

| 方案 | 架构 | 路由 | 防御 | 零依赖 | 复杂度 | 判定 |
|------|------|------|------|--------|--------|------|
| **A: LiteLLM 完全替代** | `Claude Code → LiteLLM → 本地/云端` | ✅✅ 成熟 | ❌❌ 全部丢失 | ❌ 20+ pip 包 | ✅ 简化 | **不可行** — 丢失核心壁垒 |
| **B: 双代理** | `Claude Code → LiteLLM → 当前 Proxy → 本地/云端` | ✅✅ | ✅✅ 保留 | ❌ | ❌ 双进程运维 | **过度工程** — 单用户场景无收益 |
| **C: 自建路由 (PRD)** | `Claude Code → 当前 Proxy → 本地/云端` | ✅ 够用 | ✅✅ 保留 | ✅ stdlib-only | ✅ 新增 ~500 行 | ⭐ **推荐** |
| **D: LiteLLM Sidecar** | Proxy 云端路径经 LiteLLM 转发 | ✅ 多云管理 | ✅✅ 保留 | ❌ | 🟡 | **未来可选** — 需要多云端 Provider 时有价值 |

### C.3 决策逻辑

```
                    路由能力  防御能力  零依赖  复杂度  总评
                    ────────  ────────  ──────  ──────  ────
方案 A: LiteLLM 替代   ✅✅      ❌❌      ❌      ✅      ❌
方案 B: 双代理        ✅✅      ✅✅      ❌      ❌      ❌
方案 C: 自建路由(PRD)   ✅       ✅✅      ✅      ✅      ✅  ← 当前最优
方案 D: LiteLLM Sidecar ✅      ✅✅      ❌      🟡      🟡
```

**核心结论**：LiteLLM 是为云 API 管理设计的通用网关。本项目是 Apple Silicon 本地 LLM 的专用防御层。两者的核心价值不重叠——**LiteLLM 缺失的 5 个关键能力（OOM 防护、Qwen 修复、循环检测、Blocker 检测、Session 路由一致性）恰好是本项目的核心壁垒**。自建路由（方案 C）是当前最优解。

### C.4 LiteLLM 值得借鉴的设计

| LiteLLM 设计 | PRD 的升级应用 |
|-------------|-------------|
| `context_window_fallback` — 异常驱动：请求先发本地，本地报 context window 错误后才切换。**缺点**：本地已尝试处理一次，可能产生失败请求或 OOM 风险 | SmartRouter 的 `chars_exceed_threshold` — **预测式路由**：在请求发出前就判断上下文是否超载，直接路由到云端，避免一次失败的本地调用和 OOM 风险 |
| `cooldown_time` + `allowed_fails` | §1.4 回退冷却期：连续失败 3 次后冷却 30 分钟 |
| `RetryPolicy` per exception type | §8.1 失败场景表：6 种错误类型的差异化处理 |
| `CustomGuardrail` pre-call 拦截 | NFR-6.3 敏感路径黑名单 — 请求前检查模式 |

### C.5 重新评估 LiteLLM 的触发条件

以下条件**全部满足**时，应重新评估 LiteLLM 集成：

| 条件 | 当前状态 | 说明 |
|------|---------|------|
| 需要对接 **3+ 个云端 API 提供商** | ❌ 当前仅 1 个（DeepSeek） | 多云管理是 LiteLLM 的核心价值 |
| 需要**多用户/多 API Key 管理** | ❌ 当前单人单 Key | LiteLLM 的预算/rate limit/团队管理 |
| 社区有成熟的 **loop/blocker 检测插件** | ❌ 当前无 | 降低自研防御层的维护成本 |
| **`pip install` 不再是部署约束** | ❌ 当前 stdlib-only | 单文件部署是当前核心优势 |

当前 4 个条件均不满足，方案 C（自建路由）是最优解。

### C.6 架构演进原则

为避免未来迁移 LiteLLM 时重写路由逻辑，当前架构设计应遵循后端抽象原则：

1. **SmartRouter 的输出是抽象的**：路由决策输出 `ctx._route_target`（`"local"` / `"cloud"` / `"local_forced"`），不包含具体的 HTTP 连接细节
2. **实际转发由 BackendDispatcher 完成**：所有 `base_url`、`api_key`、`lock` 的选择逻辑封装在 BackendDispatcher 中
3. **未来若迁移到方案 D（LiteLLM Sidecar）**：
   - SmartRouter 决策逻辑**无需改动** — 它只输出 target 标识
   - 仅需替换 BackendDispatcher 的实现，将云端路径的 HTTP 转发委托给 LiteLLM
   - 本地路径的 BackendDispatcher 保持不变（或也通过 LiteLLM 转发）

```
当前（方案 C）:                     未来（方案 D）:
SmartRouter → "cloud"              SmartRouter → "cloud"
    │                                    │
    ▼                                    ▼
BackendDispatcher                   BackendDispatcher
    │ (直接 HTTP)                       │ (委托)
    ▼                                    ▼
DeepSeek API                        LiteLLM Sidecar
                                         │
                                         ▼
                                    DeepSeek API
```

---

> **PRD 版本**: v1.5 (第四轮 Review 修订 — 基于 2026-06-22 生产日志分析)  
> **修订内容**: v1.0→v1.1 用户旅程、用户控制、隐私合规、数据基线、成本模型；v1.1→v1.2 LiteLLM 替代方案评估；v1.2→v1.3 区分修复前/当前基线、修正成本公式笔误、量化紧急截断标准、明确 Header 覆盖不写入 Session 状态、NFR-6.3 尽力而为声明、新增 FR-2.6 双端不可用保护、FR-5.7 路由 Profile 概念；v1.3→v1.4 修正 C.1 LiteLLM Session 一致性/Qwen 修复的表述、修正 C.4 context_window_fallback 从"吸收"改为"升级为预测式路由"、C.5 增加「当前状态」列、新增 C.6 架构演进原则明确 SmartRouter 与 BackendDispatcher 的抽象边界；**v1.4→v1.5 基于真实日志分析**：增强 US-3 成本感知为「成本感知与预算告警」、FR-4.2 /status 面板增加 `cloud_key_configured` 状态、新增 FR-4.5 成本预算告警、新增 FR-5.8 预算硬停止开关  
> **关联需求**: R8.1 智能模型路由 (新增需求域)  
> **设计文档**: 系统架构设计、数据流、配置体系、路由决策引擎、可观测性、实施计划、成功指标、风险与附录 — 见 `docs/02-architecture-design/intelligent-model-routing-design.md`
