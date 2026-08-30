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
| `PRD-anthropic-proxy.md` | 产品需求文档 (PRD v3.1，2026-08-29 修订)，需求体系 R1-R10（v3.1 新增 R9 信息保真控制 IFC / R10 渐进披露上下文服务 PDC，其中 R9.0 统一词汇表已落地）、8 层处理管线、关键配置参数和信息面分批次迭代路线图 |
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
| `multi-cloud-model-catalog-design-20260815.md` | 多云端模型目录（Model Catalog）设计与整体架构 Review（v2）：模型管理现状梳理（8 处硬编码触点）、`configs/models.json` 声明式目录（providers/models/routes 对齐 agent_go 三层设计）、直连 vs 走代理双源真相边界（catalog_hash 漂移检测）、R8 响应头命名契约对齐（`X-Proxy-Route-*`）、多提供商分发与 fallback chain、按模型成本核算，Phase A-C 实施计划（顺带交付 llama-defender R8-R12） |
| `config-unification-and-request-queue-design-20260815.md` | 配置统一与请求队列设计方案：解决配置默认值分散四处导致的漂移风险（如 qwen3.8 MODEL_NAME 缺失），以及单信号量下大请求阻塞小请求的公平性问题；含 CONFIG_REGISTRY 单一事实源、启动校验、请求四级分桶、准入控制、优先级队列、超时取消、与 SmartRouter 集成、迁移与测试计划 |
| `diagnostics-dataplane-design-20260819.md` | 诊断数据面设计（R13-R16，**已实施 2026-08-19**）：上下文工程改造的观测先行层——R13 诊断归因双通道（非流式 HTTP 头 + 流式 SSE 尾注，修正「复用 R8 头模式」的流式时序矛盾）、R14 会话台账端点（dup/last_dup_turn/材料清单，每请求派生、与 Phase 1 解耦）、R15 L4 档案三视图（sent_view 为压缩后行为复盘唯一权威）、R16 会话维度 metrics（含 is_epoch_turn 分档前提、request_id 关联）；后端能力矩阵（llama-server/rapid-mlx/cloud 降级链）、D-Phase 0-2 实施计划（约 2 天，先诊断后改造，四臂 A/B 统一口径） |
| `logging-trajectory-improvement-design-20260820.md` | 日志体系评估与改进设计（对标 DSH 轨迹视图四层模型）：现状 7 类日志/档案对标事件/动作/语义/性能层——R13-R16 已是正确的"最小轨迹系统"；差距 G1-G6（台账不持久、requests.jsonl 缺 session_id/request_id、主日志 510MB 无轮转、无统一事件契约、无离线投影工具、导出空白）；Phase A 地基三项（P0，1-2 天）+ Phase B 投影工具（trace_query/trace_replay 含 A/B diff）+ Phase C 按需；明确不做清单（请求级重放/事件溯源重构/进程内 SDK） |
| `information-fidelity-control-design-20260829.md` | 信息保真控制（IFC）设计提案（**未实施**）：控制论×信息论框架重读——回路盘点（基础设施/行为表面环已闭环、信息环唯一开环）+ 信道损失链（DPI/Good Regulator/Ashby 品种缺口）；核心算法：三层传感器（Tier-0 结构指标 / logprobs / 自一致性探针）+ 台账对账式信念审计（D_ledger，R14 台账作 ground truth 解决锚定效度盲区）+ epoch 门控/压缩回退/信息钉（第一个保真方向执行器，含 anti-windup）+ 双端失败检测（补高熵游走端）；Phase 0-4 落地计划（效度关卡：≥200 ILE 上 \|ρ\|≥0.4 或 AUC≥0.7，不达标止步于度量）+ 四层指标与六条护栏预算；§9 动态决策能力边界（调节级 Phase 3/4 达到、策略级 Phase 5 预案）；源于 MMPO/信念熵综述分析 |
| `progressive-disclosure-context-serving-design-20260829.md` | 渐进披露上下文服务（PDC）设计提案（**未实施**，IFC 姊妹篇·建设性补全）：虚拟内存隐喻——代理从"一次性减法编辑器"升级为"交互式信息服务器"；模型信息输入需求四层（任务框架零容忍/可寻址索引零容忍/新鲜工作集/尾部高容忍）、三层存储（V_t/台账 Warm/档案 Cold 全部已存在）+ `ctx_recall` 拉取通道（协议两案：次请求改写先行有 compress_tool_result 先例，微轮重派升级）；三条披露规则（索引永不丢/内容按需给/拉取即历史 append-only 兼容）；与 IFC 合流（探针 Q3=需求信号、游走获得建设性执行器、**拉取日志=压缩策略 revealed ground truth** 驱动钉/权重频率计数自校准）；D0-D3 轨道与 IFC 并行（不依赖 Phase 2 效度关卡）+ 度量（拉后即弃率等）+ 护栏 |
| `memory-storage-requirements-selection-20260829.md` | 记忆存储需求与技术选型（IFC+PDC 支撑层，**未实施**）：存储从"事后诊断档案"升格为"运行时记忆系统"；六组需求（稳定单元 ID 寻址/absorb 事务性与全量可重建/Hot-Warm-Cold 分层驱逐含优雅缺页/五流 unit_id join/钉生命周期/敏感会话探针降级）+ 缺口汇总（1 命名空间算法+5 小存储+1 中型检索）；选型分层——核心 stdlib only（**sqlite3+FTS5 本机实测可用**：SQLite 3.51.0 FTS5/WAL 验证记录）、双轨形态（热路径 dict+JSONL 沿 A3 模式、检索路径 SQLite 只读索引）、FTS5 中文分词 trigram+LIKE 兜底、DuckDB 限 tools 离线、sqlite-vec 可选加载预留、mem0/Letta/Graphiti 仅概念参考（bi-temporal→unit ID 时序、memory blocks→pins、操作日志→loss journal）+ 存储布局建议 |
| `information-metrics-applications-survey-20260830.md` | 信息面度量的推广与应用（调研与设计综合，**未实施**）：框架解释力——wiki+opencode 即路线 D（交互式披露）的现实实现（链接图=manifest/sources=Cold 层）+云端 1M vs 本地错误循环的四机制（证据存活性/有损迭代发散/验证经济学/能力乘性放大）；本地 harness 可行性设计（无损小步+结构性全局、四机制拆解、任务四级路由）；度量驱动算法（分解双停止判据-容量+熵、双轴准入四象限、Tier-0 触发器→恢复动作表）；harness 算法审计（损失空间坐标系、通用审计协议、**压缩汇率**、客户端 compact 免费审计-via view_reset）；业界调研两篇——七家上下文工程定位（全行业聚路线 C、阈值之争=无度量症状、pi 唯一 D 雏形）+ harness+RL 后训练五层方法论（Context-Folding/MemAgent/Cursor/Anthropic context editing）；本框架三定位：训练奖励组件空位/rollout 免费仪器/本地 35B 后训练组合；v0.8 候选路线 |
| `local-stack-and-training-feasibility-20260830.md` | 本地执行栈与训练可行性（实测综合，**未实施，IFC-10**）：wiki 小步快跑训练管线（故障注入=无限可验证数据生成器/SFT 蒸馏/GRPO 短-episode RL，小步协议绕开长轨迹训练难点）；本机可训性实查——9B(qwen3_5) 开箱可训 4.7GB、35B(qwen3_5_moe) 缺注册但 GDN+MoE 组件齐备、**移植成本三档下修至 1-3 小时(remap 一行)~2 天**+30 分钟定档法+内存账 25-30GB；9B 能力评估（SWE-bench 70.6 证据+任务分级判定+半天实证 pilot）；KV 机制（幽灵损失通道定位/rapid-mlx 三机制/三个静默失败先例/KIVI 共识与口径缺口/B1-B3 行为基线，B1 兼作 E2 噪声底）；后端选型协议（rapid/dflash/llama-server 盘点、真实负载回放、四维权重、**选栈=选生产引擎+选测量平台双目标**） |
| `cognitive-strategies-orchestrator-design-20260830.md` | 认知策略编排器（**宏大主题开篇**，概念框架未实施）：人类 17 种认知策略→Agent 系统设计完整映射——五个核心(分类/模式/尝试/分治/抽象) + 12 个额外(类比推理/间隔重复/精细编码/双重编码/元认知/程序化/认知卸载/孵化/手段-目的/反事实/自我解释/交叉练习)；认知负荷理论为统一底层(LLM 有与人类工作记忆类似的性能悬崖)；实现矩阵(已有/前沿/空白——最大空白：双重编码与类比推理)；统一架构"认知策略编排器"(分类器+分解器+模式库+案例库+元认知+回顾器+抽象管理器+假设管理器 构成闭环)；与经典认知架构(ACT-R/SOAR/GWT/LIDA)对照；4 项优先行动(案例库MVP/间隔重复精确化/认知负荷仪表盘/假设树) |
| `layered-theory-framework-20260830.md` | 分层理论框架（**认识论收口**）：解决"系统太复杂难以理论化"的标准方法——五层架构(信息→控制→认知→工程→实践)，每层有独立词汇/定律/度量/验证方法；层间接口(抽象屏障)与单向依赖；17 种策略的分层归属(认知 8/工程 6/控制 2/跨层 1)；5 条最小原理归属(4 条在工程层→瓶颈是代码不是理论)；问题定位方法("这是哪层的问题")；与经典分层对照(Marr 三层/OSI/计算机栈/VSM)；理论提炼路径(聚焦 Level 0→1 经验原理，不做 Level 3 大统一)；认识论定位为 Simon 人工科学(设计+验证，非发现自然规律) |
| `context-architecture-evolution-20260829.md` | 上下文架构演进总览（**系列收口·架构评审入口**，未实施）：概念架构五转变（上下文编辑器→服务器/推式单向→推+拉交互/信息环开环→三级闭环/诊断档案→运行时记忆/损失避免→rate-distortion 分配+可恢复性）+ 三面一体目标架构图（IFC 防御/PDC 建设/存储支撑 + 拉取/预取/校准三回路）+ 五条架构不变式（append-only/客户端零改/stdlib/失效安全/确定性优先）；功能架构——模块变更地图（新增 belief_probe/ctx_recall，扩展 8 模块）、管线挂载不重排（入口改写槽位+17.5 事件阶段）、四条新数据流、配置面与部署零变化；风险分级表（低观测/中注入/高门控）与既有子系统冲突检查；实施路线汇总与文档同步时点；定性为"演进而非重构"；§3 数据架构（**8 核心实体** Unit/View/Manifest/ILE/Probe/Pin/Pull/LedgerFact + 血缘骨架、**三级键树** session_key→request_id→unit_id 与五条数据不变量、**指标三级派生体系** L1 原始观测→L2 纯函数派生→L3 趋势关联·唯一可触执行器层、传感/校准双闭环共享 Unit 键空间） |
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
| `diag-dataplane-verification-20260819.md` | R13-R16 诊断数据面验证方案与实测记录：五层验证金字塔（L0 自动化回归 / L1 冒烟命令组 / L2 场景化验收——注入可观测性、dup 计数、双账本 request_id 关联实测全过 / L3 agent_go 消费方清单 / L4 运行期观察）；rapid-mlx 无 timings 定案与离线兜底路径；已知边界（字段缺省语义、8 字符 key 截断） |
| `swe-bench-pro-eval.md` | SWE-bench Pro 测评接口层说明：独立项目 `~/APP/swe-eval` 的用法、代理接口约定（路由强制/会话隔离）、两阶段计划与口径声明 |

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
| `dflash-mlx-integration-20260826.md` | dflash-mlx 新后端集成（35B ~117 tok/s）：hybrid 前缀缓存调研、rapid-mlx 优化、MTP 无效结论、manage.sh/admin_server 改动、drafter 补丁 |
| `model-catalog-multi-cloud-20260815.md` | 多云模型目录 Phase A-D 全量落地变更记录（registry/多提供商分发/fallback chain/Anthropic 协议通道/R8-R12 交付/路由与成本拓扑/新增模型 SOP/遗留事项） |
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
| `api-and-operations-guide.md` | **对外操作与 API 手册**：面向 `agent_go` / 运维人员的命令、结构化 API、配置文件、生命周期事件速查 |

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
| 对外集成 / 运维操作 | `06-reference-metrics/api-and-operations-guide.md`（命令、API、配置文件、生命周期事件） |
| 了解使用场景 | `02-architecture-design/use-cases.md`（7 个场景：日常编码 / 云端切换 / 多模型分工 / 长上下文 / 评测 / 压测 / 故障恢复） |
| 排查线上问题 | `05-operations-changelog/` 查看近期变更 → `04-analysis-diagnostics/` 查找同类问题 |
| 设计新功能 | `01-requirements-product/` 确认需求边界 → `02-architecture-design/` 参考现有设计模式 |
| 运行 A/B 测试 | `03-experiments-testing/DEEPSEEK-AB-EXPERIMENT-GUIDE.md` |
| 补充测试用例 | `03-experiments-testing/test-strategy.md` 查看覆盖缺口 |
| 重构回归保障 | `03-experiments-testing/refactor-test-strategy.md` 查看等价性校验、效果评估框架和 84+ 新增测试案例 |
| 优化性能指标 | `06-reference-metrics/` 查看 KPI 定义 → `04-analysis-diagnostics/` 查看历史分析 |
