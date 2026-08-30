# PRD v4.0：认知编排器（Cognitive Orchestrator）

> **版本**: v4.0 ｜ **日期**: 2026-08-30 ｜ **状态**: 待评审
> **上游**: PRD v3.1 (R1-R10) + 业务案例验证 + 三层架构规范 v1.0
> **范围**: 三层架构（Signal/Protocol/Application）+ 五协议 + 数据契约 + 契约管理

---

## 1. 概述

### 1.1 一句话定位
在本地 LLM 推理代理层之上，构建一个认知编排器——通过五协议（分解/执行/验证/召回/升级）管理模型的思考过程，使 9B/35B 本地模型能够可靠地完成 wiki 维护、代码修复、研究综合等长程任务。

### 1.2 解决的問題

| 痛点 | 解决方案 | 验证案例 |
|---|---|---|
| 任务死循环（734x 重读） | P4 Recall + P5 Escalate | 防死循环案例 |
| 截断丢信息找不回 | P4 Recall + Manifest | 代码修复案例 |
| 会话退化无预警 | Signal 趋势 + P5 路由 | 会话监控案例 |
| 模型"自信地说错" | P3 Verify + D_ledger | Wiki 更新案例 |
| 不知道何时升级 | P1 分解 + 双轴准入 | 全部案例 |

### 1.3 核心指标

| 指标 | 基线 | 目标 | 测量方法 |
|---|---|---|---|
| 死循环检测时间 | 45 分钟（无检测） | ≤ 3 次重读（~15s） | reread_pressure 触发 |
| Wiki 更新完成率 | 人工 100% | 9B 自动 ≥80% | 15 条论断核对通过率 |
| 会话退化预警 | 无 | 失效前 3-5 轮 | hbe_slope + retention 双条件 |
| 代码修复原子性 | 无保障 | 100%（全改或全不改） | git 变更集完整性 |
| 压缩汇率 | 未知 | 有经验值（R²≥0.4） | 管理实验 + trace_query |

---

## 2. 需求体系（R11-R15）

### R11: 认知编排器核心 [P0]

| ID | 需求 | 优先级 | 依赖 |
|----|------|--------|------|
| R11.1 | ProtocolOrchestrator 类——五协议串联入口 | P0 | signal_types, protocol_types |
| R11.2 | P1 Decompose——双判据递归分解 | P0 | SignalSnapshot |
| R11.3 | P2 Execute——小步协议（V=H） | P0 | SubTask, working_set |
| R11.4 | P3 Verify——三级机械/语义/台账 | P0 | Patch, Citation |
| R11.5 | P4 Recall——manifest 检索 + ctx_recall 挂载 | P0 | ManifestStore |
| R11.6 | P5 Escalate——决策表 + 幂等闸 | P0 | SignalSnapshot, Verdict |

### R12: 数据契约体系 [P0]

| ID | 需求 | 优先级 |
|----|------|--------|
| R12.1 | 14 个 TypedDict 显式定义 | P0 |
| R12.2 | contract_registry.py——契约注册+元数据+验证 | P0 |
| R12.3 | test_contract_alignment.py——对齐自动化 | P0 |
| R12.4 | CONTRACT_VERSION 版本管理 | P1 |
| R12.5 | IdempotencyManager——三缓存(分解/验证/召回) | P1 |

### R13: 设计模式落地 [P1]

| ID | 需求 | 模式 | 优先级 |
|----|------|------|--------|
| R13.1 | SessionState 黑板——协议不互相引用 | Blackboard | P0 |
| R13.2 | 依赖注入——Generator 可替换 | DI | P0 |
| R13.3 | 验证责任链——L1→L2→L3 | Chain of Resp. | P1 |
| R13.4 | TaskState 状态机——任务生命周期 | State | P1 |
| R13.5 | TaskCircuitBreaker——同类任务熔断 | Circuit Breaker | P1 |
| R13.6 | SignalBus——信号发布/订阅 | Observer | P2 |

### R14: 并发与性能 [P1]

| ID | 需求 | 优先级 |
|----|------|--------|
| R14.1 | 管线级并行 + 模型级串行 | P1 |
| R14.2 | 逐协议性能预算 + 超时降级 | P1 |
| R14.3 | PROXY_PIPELINE_CONCURRENT 配置 | P2 |

### R15: 运维与治理 [P2]

| ID | 需求 | 优先级 |
|----|------|--------|
| R15.1 | PostGovernance 基础版——入场层校准 | P2 |
| R15.2 | 数据质量监控——SLA 报表 | P2 |
| R15.3 | 熔断器——同类任务失败统计 | P2 |

---

## 3. 系统架构

### 3.1 三层架构

```
┌─────────────────────────────────────────────┐
│ Application Layer (wiki/code/research)     │
│ 消费 Protocol Layer 的保证                  │
├─────────────────────────────────────────────┤
│ Protocol Layer                              │
│  P1 Decompose → P2 Execute → P3 Verify    │
│       ↑              ↑           ↓         │
│  P4 Recall      P5 Escalate               │
│  + ProtocolOrchestrator + SessionState    │
├─────────────────────────────────────────────┤
│ Signal Layer (IFC) — 已建成 ✅              │
│  H_BE / ILE / D_ledger / retention        │
│  → SignalSnapshot → 阈值 → 触发            │
└─────────────────────────────────────────────┘
```

### 3.2 五协议调用流

```
Application → Task
  → P1 Decompose(如果需要) → List[SubTask]
  → 逐个: P2 Execute → P3 Verify
      → 通过: git commit
      → 失败: P5 Escalate
          → retry: P2 重试
          → reload: P4 Recall → P2 重试
          → split: P1 重新分解
          → route_cloud/human: 外部处理
```

### 3.3 数据流

```
SignalSnapshot ← Signal Layer 生产
    ↓
Task → SubTask → Patch → Citation → Verdict → EscalationDecision
    ↑                                              ↓
    └── RecallResult ← ManifestLine ← Memory Layer ←┘
```

---

## 4. 技术约束

| 约束 | 值 | 来源 |
|---|---|---|
| Python 版本 | 3.9 | 系统 |
| 第三方依赖 | 零（stdlib only） | 仓库惯例 |
| 文件组织 | 扁平模块（无包结构） | 仓库惯例 |
| 线程安全 | ThreadingHTTPServer 兼容 | 现有架构 |
| 配置 | reloadable + CONFIG_REGISTRY 注册 | IFC-2 |
| 测试 | 每协议独立可测 + 端到端 | 三层规范 §6 |
| 部署 | 与现有 proxy/pipeline 共存 | 零侵入 |

---

## 5. 分期交付计划

| 批次 | 内容 | 工作量 | 前置 |
|---|---|---|---|
| **Batch A** | 数据契约 + 契约管理 | 2 天 | 无 |
| **Batch B** | P4 Recall 挂载 + P5 Escalate | 3 天 | Batch A |
| **Batch C** | P1 Decompose + ProtocolOrchestrator | 3 天 | Batch A+B |
| **Batch D** | 设计模式 + 并发 + 性能预算 | 2 天 | Batch C |
| **Batch E** | 后治理 + 运维 | 2 天 | Batch D |

总计：约 12 天（可与现有 IFC-3 挂载包并行部分工作）

---

## 6. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| pipeline.py 被并行工作占用 | 高 | P4 挂载延迟 | 方案 A（次请求改写）不依赖 pipeline |
| 9B 能力不足（对账 <70%） | 中 | wiki 案例受阻 | SFT v0（故障注入→LoRA） |
| 锚充分性不成立 | 低 | PDC 核心假设失效 | E1 已验证 manifest 100%覆盖 |
| DEF-306 持续触发 | 高 | 实验中断 | 加急修复（IFC-1 最高优先） |
