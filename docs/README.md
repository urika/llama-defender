# 文档目录说明

本文档库采用软件工程标准文档分类体系，按文档性质和用途分为 **7 大类**。所有文档按编号前缀排序，便于快速定位。

---

## 目录结构

```
docs/
├── 01-requirements-product/          # 需求与产品文档
├── 02-architecture-design/           # 架构与设计文档
├── 03-experiments-testing/           # 实验与测试文档
├── 04-analysis-diagnostics/          # 问题分析与诊断报告
├── 05-operations-changelog/          # 运维与变更记录
├── 06-reference-metrics/             # 技术参考与指标文档
├── 07-project-board/                 # 项目看板与版本规划
├── research-context-optimization/    # 上下文压缩策略研究系列（早期探索）
└── README.md                         # 本文件
```

---

## 分类说明

### 01-requirements-product — 需求与产品文档

定义系统"做什么"，面向产品经理、项目经理和开发者。

| 文档 | 说明 |
|------|------|
| `PRD-anthropic-proxy.md` | 产品需求文档 (PRD v3.1，2026-08-29 修订)，需求体系 R1-R10（v3.1 新增 R9 信息保真控制 IFC / R10 渐进披露上下文服务 PDC，其中 R9.0 统一词汇表已落地）、8 层处理管线、关键配置参数和信息面分批次迭代路线图 |
| `llama-defender-integration-requirements.md` | llama-defender 集成需求（agent_go → 服务方）：R1-R12 已交付，R13-R16 上下文工程诊断数据面，R17-R19 数据契约 |
| `system-requirements-analysis.md` | 系统需求分析与验证，基于实测数据（Qwen3.6-35B-A3B + Claude Code + M5 Pro 48GB）提炼性能基线与优化目标 |
| `business-cases-validation-20260830.md` | 业务案例与系统解决方案推演（**产品验证**）：五个业务案例（防死循环/wiki 综述更新/会话健康监控/多文件代码修复/压缩成本评估）逐一推演三层架构+五协议的端到端方案 |
| `prd-cognitive-orchestrator-v4.md` | PRD v4.0：认知编排器（**开发输入**）——R11-R15 五大需求；核心指标；三期架构图+五协议调用流+数据流；五批次交付计划 |
| `PM-ANALYSIS-FUTURE-ROADMAP.md` | 产品经理视角分析：核心功能取舍与未来路线图 |
| `OSS-REPLACEMENT-EVALUATION.md` | OSS 替代可能性深度评估（LiteLLM / Langfuse / Promptfoo / GEPA / DSPy / LangChain 等） |
| `PRD-intelligent-model-routing.md` | 智能模型路由 PRD |
| `PRD-intelligent-model-routing-review-2026-06-21.md` | 智能路由 PRD Review |
| `PRD-litellm-borrow-2026-07-05.md` | LiteLLM 借鉴增量包 PRD |
| `PRD-opencode-proxy-compatibility-2026-06-25.md` | OpenCode 代理兼容性 PRD |
| `pm-pain-points-and-solution-prospect-2026-06-21.md` | PM 痛点与解决方案前景 |
| `requirement-matrix.md` | 需求追踪矩阵（由 `tools/trace_requirements.py` 自动生成） |

**入口文档**：新成员请先阅读 `PRD-anthropic-proxy.md` 了解系统全貌。

---

### 02-architecture-design — 架构与设计文档

定义系统"怎么做"，面向架构师和核心开发者。

| 文档 | 说明 |
|------|------|
| `proxy-pipeline-reference.md` | 代理层请求处理管线参考文档，逐层说明 8 层处理逻辑、Phase 0 模块架构、dual-setattr 热重载 |
| `proxy-context-window-design.md` | 代理层上下文窗口替换设计文档（v9），Phase 1-3 已实施 + Phase 0 模块拆分 |
| `proxy-prefix-cache-design.md` | 代理层 Prefix Cache 稳定化设计文档 v1 |
| `llama-defender-context-engineering-design.md` | llama-defender 上下文工程改造设计（append-only + epoch 压缩 + 动作台账） |
| `system-architecture-analysis.md` | 系统架构分析 |
| `architecture-review-2026-06-21.md` | 系统架构审查报告（2026-06-21） |
| `pipeline-abstraction-plan.md` | Pipeline 抽象设计文档 |
| `use-cases.md` | 目标场景使用案例（7 个场景 A-G） |
| `design-review-20260608.md` | 设计审阅意见 |
| `proxy-context-window-design-review.md` | 上下文窗口设计审阅意见 |
| `proxy-context-window-design-review-merged.md` | 审阅意见合并记录 |
| `multi-cloud-model-catalog-design-20260815.md` | 多云端模型目录设计与整体架构 Review |
| `config-unification-and-request-queue-design-20260815.md` | 配置统一与请求队列设计方案 |
| `diagnostics-dataplane-design-20260819.md` | 诊断数据面设计（R13-R16，已实施） |
| `logging-trajectory-improvement-design-20260820.md` | 日志体系评估与改进设计 |
| `information-fidelity-control-design-20260829.md` | 信息保真控制（IFC）设计提案（未实施） |
| `progressive-disclosure-context-serving-design-20260829.md` | 渐进披露上下文服务（PDC）设计提案（未实施） |
| `memory-storage-requirements-selection-20260829.md` | 记忆存储需求与技术选型（IFC+PDC 支撑层，未实施） |
| `information-metrics-applications-survey-20260830.md` | 信息面度量的推广与应用（未实施） |
| `local-stack-and-training-feasibility-20260830.md` | 本地执行栈与训练可行性（未实施） |
| `cognitive-strategies-orchestrator-design-20260830.md` | 认知策略编排器（概念框架，未实施） |
| `layered-theory-framework-20260830.md` | 分层理论框架（认识论收口） |
| `three-layer-architecture-spec-20260830.md` | 三层架构正式规范（落地蓝图 v1.0） |
| `context-architecture-evolution-20260829.md` | 上下文架构演进总览（系列收口·架构评审入口） |
| `cognitive-gap-closure-design-20260901.md` | 认知空白点收口设计（未实施） |
| `skill-progressive-disclosure-insights-20260901.md` | Skill 渐进式信息披露机制分析与压缩借鉴（调研提案） |
| `protocol-layer-ownership-review-20260902.md` | Protocol Layer 归属与边界设计 Review（v1.2 定版） |
| `rag-kv-storage-fit-survey-20260901.md` | RAG 组件与 KV 存储匹配度调研 |
| `bm25-scoring-design-2026-07-05.md` | BM25 相关性评分设计 |
| `compression-result-design-2026-07-05.md` | 压缩结果类型设计 |
| `tool-pair-atomicity-design-2026-07-05.md` | 工具对原子性设计 |
| `loop-threshold-dynamic-tuning-design-2026-06-22.md` | 循环阈值动态调优设计 |
| `agent-output-budget-design-20260827.md` | Agent 输出预算设计 |
| `intelligent-model-routing-design.md` | 智能模型路由设计 |
| `intelligent-model-routing-design-review-2026-06-21.md` | 智能路由设计 Review |
| `intelligent-model-routing-design-review-2026-06-22.md` | 智能路由设计 Review（续） |
| `intelligent-model-routing-design-review-2026-06-22-model-id.md` | 智能路由 model-id Review |
| `intelligent-model-routing-force-prefer-design.md` | 路由强制偏好设计 |
| `intelligent-model-routing-force-prefer-design-review.md` | 路由强制偏好设计 Review |
| `opencode-anthropic-routing-decision-20260829.md` | OpenCode Anthropic 路由决策 |
| `proxy-truncation-agent-scenario.md` | 代理截断 Agent 场景设计 |
| `context-tier-profiles-design-20260901.md` | 上下文分层 Profile 设计 |
| `reading-list-systems-knowledge-20260830.md` | 系统知识阅读清单 |
| `proxy_solutions_mapping.md` | 代理层可解痛点与 Kompact/TokenSieve 改进方案映射 |

---

### 03-experiments-testing — 实验与测试文档

定义"如何验证"，面向测试工程师、实验设计者和维护者。

| 文档 | 说明 |
|------|------|
| `DEEPSEEK-AB-EXPERIMENT-GUIDE.md` | DeepSeek 代理中转与 A/B 实验完整指南 |
| `ab-experiment-design.md` | A/B 对比实验设计：Context Management 配置对比 |
| `ab-test-task-log-system.md` | A/B 测试任务：为代理添加结构化日志系统 |
| `test-strategy.md` | 测试策略与覆盖矩阵 |
| `refactor-test-strategy.md` | 重构测试策略与回归保障方案 |
| `diag-dataplane-verification-20260819.md` | R13-R16 诊断数据面验证方案与实测记录 |
| `swe-bench-pro-eval.md` | SWE-bench Pro 测评接口层说明 |
| `long-context-verification-tests.md` | 长上下文验证测试 |
| `local-model-hard-tasks-prompts-20260812.md` | 本地模型困难任务 prompts |
| `local-model-hard-tasks-test-20260812.md` | 本地模型困难任务测试 |
| `promptfoo-migration.md` | promptfoo 迁移文档 |
| `amnesia-experiment-protocol-20260830.md` | Amnesia 实验协议 |
| `ctx-single-step-case-debug-design-20260905.md` | ctx-case 单步报文调试框架设计（影子环境/mock 后端/四出口断言） |
| `ctx-single-step-case-set-20260905.md` | ctx-case 案例集规格 TC01-TC21（六字段/案例 + 日志问题追溯附录 + 首轮实测红绿表） |
| `ctx-case-behavioral-extension-design-20260905.md` | ctx-case 行为层扩展（B-suite：模型在环测试 + 闭环问答探针 + 定量评估设计） |
| `system-behavior-testing-methodology-20260905.md` | 系统行为类测试与评估方法论（分层金字塔/断环矩阵/金标准探针/小样本统计） |

---

### 04-analysis-diagnostics — 问题分析与诊断报告

记录"发现了什么"，面向问题排查、性能优化和研究者。

| 文档 | 说明 |
|------|------|
| `DEFECT-LIST.md` | 功能缺陷清单（DEF-001…，30 项缺陷含根因、修复、遗留问题） |
| `dead-loop-analysis-report.md` | Claude Code 死循环分析与代理层优化报告 |
| `message-analysis-20260602.md` | 报文深度分析报告（197K chars / 56K tokens 膨胀晚期诊断） |
| `swe-empty-patch-rootcause-20260903.md` | swe 实例空 patch 根因调查与窗口深度验证（fifo 24→80 判定实验） |
| `ctx-swe-correlation-analysis-20260905.md` | 上下文管理条件与 swe 测试结果相关性分析（单实例 24 run 纵向 + 定性结论） |
| `message-analysis-20260604.md` | 报文情况与处理性能分析报告 |
| `claude-behavior-semantic-analysis-v2.md` | Claude Code 语义行为深度分析（v2） |
| `rapid-mlx-cache-analysis.md` | Rapid-MLX Prefix Cache 命中问题分析报告 |
| `rapid-mlx-cache-analysis-supplement.md` | 补充分析：源码级验证 4 种匹配策略 |
| `rapidmlx-kvcache-mechanism-20260903.md` | rapid-mlx KV Cache 机制分析 |
| `rapid-mlx-0.11.5-gemma4-chat-template-regression.md` | rapid-mlx 0.11.5 Gemma4 chat template 回归分析 |
| `prefix-cache-analysis-20260605.md` | Prefix Cache 深度分析与 TurboQuant 测试记录 |
| `prompt-instability-mechanism-analysis.md` | Agentic 截断策略导致 Prompt 不稳定的机制分析 |
| `proxy-truncation-as-forgetting-mechanism.md` | 代理截断作为遗忘机制 |
| `model-tool-issues.md` | 本地模型 Tool Calling 质量问题记录 |
| `session-analysis-and-tool-issues-2026-06-22.md` | 会话分析与工具问题 |
| `test-coverage-analysis-2026-06-22.md` | 测试覆盖分析 |
| `test-review-checklist.md` | 测试审阅检查清单 |
| `dcp-strategy-analysis-20260618.md` | DCP 策略分析 |
| `dcp-vs-proxy-context-compression.md` | DCP vs 代理上下文压缩 |
| `code-review-2026-06-22.md` | 代码审阅（2026-06-22） |
| `code-review-anthropic-proxy-20250607.md` | anthropic_proxy 代码审阅（2025-06-07） |
| `amnesia-experiment-report-20260830.md` | Amnesia 实验报告 |
| `exp2-v2-wrapup-20260901.md` | exp2 v2 收尾报告 |
| `proxy_pain_points_analysis.md` | 代理层痛点分析（产品经理视角） |

---

### 05-operations-changelog — 运维与变更记录

记录"做了什么变更"，面向运维工程师和值班人员。

| 文档 | 说明 |
|------|------|
| `dflash-mlx-integration-20260826.md` | dflash-mlx 新后端集成（35B ~117 tok/s） |
| `ornith-15-integration-20260826.md` | Ornith-1.5-35B 集成变更 |
| `model-catalog-multi-cloud-20260815.md` | 多云模型目录 Phase A-D 全量落地变更记录 |
| `optimization-log-20260603.md` | 代理层优化工作日志 |
| `config-change-20260604-max-num-seqs.md` | 配置修改：`--max-num-seqs` 从 1 提升到 2 |
| `config-change-20260604-rollback.md` | 配置修改：回滚并发上限 |
| `monitor-report-20260604-post-change.md` | 配置修改后监控报告 |
| `tool-call-token-budget-2026-06-23.md` | 工具调用 token 预算调整 |
| `cloud-cooldown-revision-2026-06-23.md` | 云端冷却时间修订 |
| `refactoring-completion-report-2026-06-21.md` | 重构完成报告（anthropic_proxy.py 模块化拆分） |
| `haiku-force-local-20260829.md` | Haiku 强制本地路由变更 |
| `deepseek-cost-trace-fix-20260831.md` | DeepSeek 成本追踪修复 |
| `qwen25-coder-7b-completion-20260827.md` | Qwen2.5-Coder-7B completion 测试 |
| `qwen3.8-dflash2-smoke-20260829.md` | Qwen3.8 DFlash2 冒烟测试 |
| `model-performance-snapshot-20260902.md` | 模型性能快照 |
| `spark-x2.5-llama-server-test-20260903.md` | Spark-X2.5 llama-server 测试 |

---

### 06-reference-metrics — 技术参考与指标文档

提供"如何度量"的标准和参考，面向数据分析师、优化工程师和外部集成方。

| 文档 | 说明 |
|------|------|
| `api-and-operations-guide.md` | **对外操作与 API 手册**：面向 `agent_go` / 运维人员的命令、结构化 API、配置文件、生命周期事件速查 |
| `BENCHMARK.md` | 本地 LLM 性能测试报告与基线 |
| `TROUBLESHOOTING.md` | 故障排查记录（chat template、tool calling、OOM 等） |
| `proxy-semantic-metrics.md` | 代理层语义优化：量化指标体系（v3） |
| `structured-summary-impl-evaluation.md` | 结构化摘要替代占位符：代码实现评估 |

---

### 07-project-board — 项目看板与版本规划

记录版本目标、里程碑和任务拆分，面向项目管理者和开发者。

| 文档 | 说明 |
|------|------|
| `v0.6.0.md` | v0.6.0 Project Board — P0 修复 + Langfuse 集成 |
| `v0.6.1.md` | v0.6.1 Project Board — litellm 借鉴增量包 |
| `v0.7.0-information-plane.md` | v0.7.0 Project Board — 信息面（IFC/PDC）+ 认知编排器 MVP |

---

### research-context-optimization — 上下文压缩策略研究系列

早期上下文优化研究系列（2026-06），是后续 `02-architecture-design/` 中压缩/截断设计的先导探索。保留为历史参考，新增设计请优先查阅 `02-architecture-design/`。

| 文档 | 说明 |
|------|------|
| `README.md` | 系列说明 |
| `01-requirements.md` | 上下文优化需求 |
| `02-problems.md` | 问题定义 |
| `03-analysis.md` | 分析 |
| `04-solutions.md` | 解决方案 |
| `05-plan.md` | 实施计划 |
| `06-context-compression-strategy.md` | 上下文压缩策略总览（Phase 1-3 整合版） |
| `06-feasibility.md` | 可行性分析 |
| `07-tokensieve-implementation-plan.md` | TokenSieve 实现计划 |
| `research_kompact_tokensieve.md` | Kompact + TokenSieve 源码研究笔记 |
| `research_kompact_tokensieve_pm.md` | Kompact / TokenSieve 集成评估：产品经理视角 |

---

## 文档命名规范

- **产品/需求文档**：`PRD-*.md`、`system-requirements-*.md`
- **设计文档**：`*-design.md`、`*-pipeline-*.md`、`*-review*.md`
- **实验/测试文档**：`*-experiment-*.md`、`*-test-*.md`、`test-strategy.md`
- **分析报告**：`*-analysis-*.md`、`*-report-*.md`
- **运维记录**：`*-log-*.md`、`*-change-*.md`、`*-integration-*.md`
- **参考文档**：`*-metrics.md`、`*-evaluation.md`、`*-guide.md`
- **项目看板**：`v*.md`

日期后缀格式：`YYYYMMDD`，便于按时间排序和追溯。

---

## 使用建议

| 场景 | 推荐路径 |
|------|----------|
| 新成员 onboarding | `01-requirements-product/PRD-anthropic-proxy.md` → `02-architecture-design/proxy-pipeline-reference.md` → `02-architecture-design/use-cases.md` |
| 对外集成 / 运维操作 | `06-reference-metrics/api-and-operations-guide.md` |
| 了解使用场景 | `02-architecture-design/use-cases.md` |
| 排查线上问题 | `04-analysis-diagnostics/DEFECT-LIST.md` → `05-operations-changelog/` 查看近期变更 → `04-analysis-diagnostics/` 查找同类问题 |
| 设计新功能 | `01-requirements-product/` 确认需求边界 → `02-architecture-design/` 参考现有设计模式 |
| 运行 A/B 测试 | `03-experiments-testing/DEEPSEEK-AB-EXPERIMENT-GUIDE.md` |
| 补充测试用例 | `03-experiments-testing/test-strategy.md` 查看覆盖缺口 |
| 重构回归保障 | `03-experiments-testing/refactor-test-strategy.md` |
| 优化性能指标 | `06-reference-metrics/` 查看 KPI 定义 → `04-analysis-diagnostics/` 查看历史分析 |
| 查看版本规划 | `07-project-board/` |
