# 文档目录说明

本文档库采用软件工程标准文档分类体系，按文档性质和用途分为 6 大类。所有文档按编号前缀排序，便于快速定位。

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
└── README.md                         # 本文件
```

---

## 分类说明

### 01-requirements-product — 需求与产品文档

定义系统"做什么"，面向产品经理、项目经理和开发者。

| 文档 | 说明 |
|------|------|
| `PRD-anthropic-proxy.md` | 产品需求文档 (PRD v3.0)，涵盖 7 大领域 / 23 个需求点、8 层处理管线、关键配置参数和迭代路线图 |
| `system-requirements-analysis.md` | 系统需求分析与验证，基于实测数据（Qwen3.6-35B-A3B + Claude Code + M5 Pro 48GB）提炼性能基线与优化目标 |

**入口文档**：新成员请先阅读 `PRD-anthropic-proxy.md` 了解系统全貌。

---

### 02-architecture-design — 架构与设计文档

定义系统"怎么做"，面向架构师和核心开发者。

| 文档 | 说明 |
|------|------|
| `proxy-context-window-design.md` | 代理层上下文窗口替换设计文档（v9），Phase 1-3 已实施 + Phase 0 模块拆分，含问题诊断、方案设计、资源护栏、风险矩阵 |
| `proxy-pipeline-reference.md` | 代理层请求处理管线参考文档，与 `anthropic_proxy.py` (5529 行) + `proxy_state.py` (518 行) + `proxy_config.py` (659 行) 同步，逐层说明 8 层处理逻辑、Phase 0 模块架构、dual-setattr 热重载 |
| `use-cases.md` | 目标场景使用案例（7 个场景 A-G）：日常编码、本地↔云端热切换、多模型分工、长上下文分析、模型选型评测、并发压测、故障恢复 |
| `proxy-context-window-design-review.md` | 设计审阅意见（P1-P3 需修正 + S1-S5 建议改进） |
| `proxy-context-window-design-review-merged.md` | 审阅意见合并记录，8/8 全部采纳 |
| `../research-context-optimization/06-context-compression-strategy.md` | 上下文压缩管理策略总览（Phase 1-3 整合版），含决策矩阵、指标体系与配置建议 |

---

### 03-experiments-testing — 实验与测试文档

定义"如何验证"，面向测试工程师、实验设计者和维护者。

| 文档 | 说明 |
|------|------|
| `DEEPSEEK-AB-EXPERIMENT-GUIDE.md` | DeepSeek 代理中转与 A/B 实验完整指南，含架构概览、启动指南、实验方案、故障排查 |
| `ab-experiment-design.md` | A/B 对比实验设计：Context Management 配置对比（模拟本地约束 vs 云端无约束） |
| `ab-test-task-log-system.md` | A/B 测试任务：为代理添加结构化日志系统（M1 结构化日志 + M2 状态页统计） |
| `test-strategy.md` | 测试策略与覆盖矩阵，审计发现自动化测试覆盖 7/23 = 30%，列出补齐优先级 |
| `refactor-test-strategy.md` | 重构测试策略与回归保障方案，覆盖模块拆分等价性校验、Cache Aligner/结构化压缩效果评估、云端模式硬化、84+ 新增测试案例 |

---

### 04-analysis-diagnostics — 问题分析与诊断报告

记录"发现了什么"，面向问题排查、性能优化和研究者。

| 文档 | 说明 |
|------|------|
| `dead-loop-analysis-report.md` | Claude Code 死循环分析与代理层优化报告，含 20 个请求报文的完整循环到恢复过程 |
| `message-analysis-20260602.md` | 报文深度分析报告（197K chars / 56K tokens 膨胀晚期诊断） |
| `message-analysis-20260604.md` | 报文情况与处理性能分析报告（331K 字符、582 条消息、1151 条请求） |
| `claude-behavior-semantic-analysis-v2.md` | Claude Code 语义行为深度分析（v2），1220 条记录基础上的交互语义研究 |
| `rapid-mlx-cache-analysis.md` | Rapid-MLX Prefix Cache 命中问题分析报告（v0.6.30，100% MISS 根因） |
| `rapid-mlx-cache-analysis-supplement.md` | 补充分析：源码级验证 4 种匹配策略（exact/prefix/supersequence/LCP） |
| `prefix-cache-analysis-20260605.md` | Prefix Cache 深度分析与 TurboQuant 测试记录 |
| `prompt-instability-mechanism-analysis.md` | Agentic 截断策略导致 Prompt 不稳定的机制分析（相邻请求重叠度仅 24%） |
| `proxy-truncation-as-forgetting-mechanism.md` | 代理截断作为遗忘机制：Claude Code 的认知生存策略 |
| `model-tool-issues.md` | 本地模型 Tool Calling 质量问题记录（Write 工具缺少 content 参数等） |

---

### 05-operations-changelog — 运维与变更记录

记录"做了什么变更"，面向运维工程师和值班人员。

| 文档 | 说明 |
|------|------|
| `optimization-log-20260603.md` | 代理层优化工作日志（Context Bloat 治理 + 性能优化 Phase 1-3） |
| `config-change-20260604-max-num-seqs.md` | 配置修改记录：将 `--max-num-seqs` 从 1 提升到 2 |
| `config-change-20260604-rollback.md` | 配置修改记录：回滚并发上限（内存压力过高，风险大于收益） |
| `monitor-report-20260604-post-change.md` | 配置修改后监控报告（10 分钟稳定性监控） |

---

### 06-reference-metrics — 技术参考与指标文档

提供"如何度量"的标准和参考，面向数据分析师和优化工程师。

| 文档 | 说明 |
|------|------|
| `proxy-semantic-metrics.md` | 代理层语义优化：量化指标体系（v3），含 5 项优化回顾、循环健康度 KPI、埋点代码实现 |
| `structured-summary-impl-evaluation.md` | 结构化摘要替代占位符：代码实现评估，分析 prefix cache 命中率提升可行性 |

---

## 文档命名规范

- **产品/需求文档**：`PRD-*.md`、`system-requirements-*.md`
- **设计文档**：`*-design.md`、`*-pipeline-*.md`、`*-review*.md`
- **实验/测试文档**：`*-experiment-*.md`、`test-strategy.md`
- **分析报告**：`*-analysis-*.md`、`*.md`（以问题域命名）
- **运维记录**：`*-log-*.md`、`*-change-*.md`、`*-report-*.md`
- **参考文档**：`*-metrics.md`、`*-evaluation.md`

日期后缀格式：`YYYYMMDD`，便于按时间排序和追溯。

---

## 使用建议

| 场景 | 推荐路径 |
|------|----------|
| 新成员 onboarding | `01-requirements-product/PRD-anthropic-proxy.md` → `02-architecture-design/proxy-pipeline-reference.md` → `02-architecture-design/use-cases.md` |
| 了解使用场景 | `02-architecture-design/use-cases.md`（7 个场景：日常编码 / 云端切换 / 多模型分工 / 长上下文 / 评测 / 压测 / 故障恢复） |
| 排查线上问题 | `05-operations-changelog/` 查看近期变更 → `04-analysis-diagnostics/` 查找同类问题 |
| 设计新功能 | `01-requirements-product/` 确认需求边界 → `02-architecture-design/` 参考现有设计模式 |
| 运行 A/B 测试 | `03-experiments-testing/DEEPSEEK-AB-EXPERIMENT-GUIDE.md` |
| 补充测试用例 | `03-experiments-testing/test-strategy.md` 查看覆盖缺口 |
| 重构回归保障 | `03-experiments-testing/refactor-test-strategy.md` 查看等价性校验、效果评估框架和 84+ 新增测试案例 |
| 优化性能指标 | `06-reference-metrics/` 查看 KPI 定义 → `04-analysis-diagnostics/` 查看历史分析 |
