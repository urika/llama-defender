# 代理层上下文优化研究：文档索引

> 本目录按 **需求 / 问题 / 分析 / 解决方案 / 计划** 五段式结构整理 Kompact、TokenSieve 源码调研结论，以及当前 `anthropic_proxy.py` 的痛点与可落地优化方向。

## 快速导航

| 序号 | 文档 | 阅读场景 | 关键结论 |
|------|------|----------|----------|
| `01-requirements.md` | 需求 | 明确本次研究的北极星目标与约束 | 在保持/提升任务完成率的前提下降低上下文压力 |
| `02-problems.md` | 问题 | 了解当前代理层 8 大核心痛点，以及本地部署/机器性能如何约束它们 | 上下文 vs cache 冲突、tool result 清除死循环、循环多样性、资源约束、工具过滤、兼容性、可观测性、配置复杂度，以及三类约束关联分析 |
| `03-analysis.md` | 分析 | 查看 Kompact / TokenSieve 的适用性判断 | Cache Aligner 优先级最高，语义压缩次之，自然语言改写不推荐 |
| `04-solutions.md` | 解决方案 | 查看代理层可落地的 6 大方案 | 方案 A 为 Cache Aligner，方案 B 为语义保留压缩，方案 C–F 为防御/资源/观测/配置 |
| `05-plan.md` | 计划 | 了解分阶段落地路线图 | 4 阶段，M1–M4 里程碑，优先完成基线与 Cache Aligner MVP |
| `06-context-compression-strategy.md` | 策略总览 | 已实施能力的整体梳理 | Phase 1-3 三层防御体系、决策矩阵、指标体系、配置建议 |
| `06-feasibility.md` | 可行性 | 评估当前机器+模型能否支撑编程 Agent | 可行但有边界，优化后可支撑轻量到中度场景，不可行时需升级硬件/模型/架构 |
| `07-tokensieve-implementation-plan.md` | 开发计划 | TokenSieve 借鉴点的详细落地任务 | 5 个 Phase、16 项任务、接口草案、metrics 字段、里程碑 |

## 参考文档

以下原始调研文档保留在项目根目录，作为本目录的详细技术依据：

| 文档 | 内容 |
|------|------|
| `/Users/jinsongwang/APP/llama.cpp/research_kompact_tokensieve.md` | Kompact 与 TokenSieve 源码级技术分析、benchmark 数据、关键类与函数说明 |
| `/Users/jinsongwang/APP/llama.cpp/research_kompact_tokensieve_pm.md` | 产品经理视角的目标、判断维度、投入产出评估 |
| `/Users/jinsongwang/APP/llama.cpp/proxy_pain_points_analysis.md` | 当前代理层 8 大痛点详细分析与根因拆解 |
| `/Users/jinsongwang/APP/llama.cpp/proxy_solutions_mapping.md` | 痛点与调研产品的解决方案映射、优先级矩阵 |

## 关键结论速览

1. **最高优先级**：将 `anthropic_proxy.py` 的上下文截断策略向 Kompact 的 `cache_aligner` 靠拢，稳定 system/skills/工具定义顺序，隔离动态 system 插入。
2. **次高优先级**：用 Kompact 的 `json_crusher` / `code_compressor` 与 TokenSieve 的结构化摘要替代粗暴的 tool result clearing，降低 re-read 死循环风险。
3. **谨慎引入**：标量去重、LLM-based 摘要；需要小范围验证后再决定是否启用。
4. **不推荐**：会改变 token 序列的自然语言改写（如 Kompact `toon`），因为 local 模式下 prefix cache 稳定性比绝对 token 量更重要。
5. **配套工作**：修复 `re_read_rate` 等错误指标、增强 `/status` 看板、建立 A/B 实验流程，让每次优化都可测量、可回滚。
6. **可行性边界**：当前 48GB + Qwen3.6-35B-A3B 可支撑轻量到中度编程 Agent 场景，优化后可将稳定会话从 10–30 分钟延长到 30–60 分钟、支持 1–5 万行代码库；但无法支撑大型 monorepo 或 2 小时以上复杂会话，除非升级硬件/模型/架构。

## 使用建议

- **技术同学**从 `01-requirements.md` → `02-problems.md` → `03-analysis.md` → `04-solutions.md` 顺序阅读。
- **产品/项目管理者**重点看 `01-requirements.md`、`03-analysis.md` 中的判断矩阵、`05-plan.md` 的路线图。
- 如需深入源码实现细节，可回到根目录的 `research_kompact_tokensieve.md` 查阅类图、函数签名与 benchmark 命令。
