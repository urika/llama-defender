# 设计 Review：Protocol Layer 归属与边界

> **版本**: v1.2 ｜ **日期**: 2026-09-02（v1.1 复核修订：C1 证据更正、M1 表述更新、M2/M6 补代码锚点、M5 补信任边界与 OOM 语义、新增盲点 B1-B5 与 Phase 0 前置验证项；v1.2 按 agent_go 反馈定版修订：Phase 2 改"组件拆解吸收"、B1 关闭、C2 软化为轮次级独立策略改写、测试基线 41→124 更正、§6 措辞限定、新端点纳入 R17-R19 契约版本化）
> **Review 对象**: Protocol Layer（`decompose.py` / `escalate.py` / `verification_chain.py` / `idempotency.py` / `protocol_orchestrator.py` / `post_governance.py` / `protocol_types.py` / `signal_types.py` / `contract_registry.py`）+ 代理侧压缩/召回/信号模块的边界
> **关联文档**: [three-layer-architecture-spec-20260830.md](three-layer-architecture-spec-20260830.md) ｜ [cognitive-orchestrator-design-doc-20260830.md](cognitive-orchestrator-design-doc-20260830.md) ｜ [cognitive-gap-closure-design-20260901.md](cognitive-gap-closure-design-20260901.md) ｜ [memory-storage-requirements-selection-20260829.md](memory-storage-requirements-selection-20260829.md)（§E pins 预算与 M5 同源）｜ [llama-defender-integration-requirements.md](../01-requirements-product/llama-defender-integration-requirements.md)（新端点按 R17-R19 纳入，§3.3 草案态）｜ agent_go 反馈：`~/workspace/agent_go/docs/in/protocol-layer-ownership-review-feedback-20260902.md`
> **结论**: **有条件通过**——实现质量（状态机/幂等/防御/契约）合格，但**架构归属存在根本性错位**，需按「任务工程归 agent_go、输入工程归代理」的边界重构后再定版。

---

## 1. 背景与评审范围

### 1.1 现状

本仓库（llama-defender）是本地 LLM 推理编排代理，核心职责是把 `llama-server` / `rapid-mlx` 包装成 Anthropic 兼容 API。仓库内实现了三层架构（Signal → Protocol → Application）中的 **Signal Layer（已生产挂载）** 与 **Protocol Layer（P1-P5 五协议编排闭环，Phase 1 独立模块 + 单元测试，未接入运行时 pipeline）**。

Protocol Layer 由以下模块组成：

| 模块 | 职责 | 运行时接线 |
|---|---|---|
| `decompose.py` | P1 分解器（双判据递归） | ❌ 未接线 |
| `escalate.py` | P5 升级器（决策表/状态机/熔断） | ❌ 未接线 |
| `verification_chain.py` | P3 三级验证链 | ❌ 未接线 |
| `idempotency.py` | decompose/verify 幂等缓存 | ❌ 未接线 |
| `protocol_orchestrator.py` | 五协议编排闭环（唯一入口 `solve()`） | ❌ 未接线 |
| `post_governance.py` | 入场层校准 / 模式编译 / 数据质量 SLA | ❌ 未接线 |
| `protocol_types.py` / `signal_types.py` | 数据契约（TypedDict） | 契约层 |
| `contract_registry.py` | 契约血缘元数据 + 运行时验证 | 元数据 |

验证现状：Protocol 域 characterization 基线实为 **124 个用例全绿**（2026-09-02 实测——v1.2 更正：原引"41 个"只计 `test_decompose` 20 + `test_protocol_orchestrator` 21，漏计 `test_escalate` 21 / `test_idempotency` 14 / `test_post_governance` 31 / `test_verification_chain` 17），纯函数级验证完整。

### 1.2 评审主线

1. 任务分解的具体算法与粒度拆解（流程/结构两维度）是否合理；
2. 拆分是否有落地场景、能否验证；
3. 新系统（无存量文件）如何拆分；
4. 从架构设计文档拆分的常识做法；
5. **升维：任务工程是否应该发生在代理运行时**；
6. 哪些模块应剥离到 agent_go 侧；
7. 上下文压缩与管理的归属；
8. agent_go 与本项目的交互面；
9. 召回服务面的必要性与语义封装。

---

## 2. Review 意见（按严重度分级）

### 2.1 🔴 Critical（架构错位，必须改）

| # | 问题 | 现状 | 证据 | 建议 |
|---|---|---|---|---|
| C1 | **任务工程被实现在代理内，但归属在 agent_go** | P1-P5 五协议闭环写在代理仓库，却不会被代理运行时调用 | `pipeline.py`/`anthropic_proxy.py` 零引用；轮次协议（Anthropic/OpenAI）里没有 `Task` 产生点；**契约层已预设归属**——`contract_registry` 中 `Task` 的 producer 是 `application_layer`（v1.1 更正：原引述"P1_decompose 自己"有误，SubTask 的 producer 才是 P1_decompose；真实数据反而强化结论）；且代理运行时**无 P2 执行器**，escalate 的 reload/retry 动作在代理内无可作用对象（无功能论证，强于错位论证）；代理客户端多数为交互式（Claude Code），不存在任务级分解的发生时机 | 整包迁移到 agent_go（见 §4 Phase 2）；本仓库保留为**契约基准 + 参考实现** |
| C2 | **LRC-P3 的 escalate 临时接线方向错误**（条件成立——C1 的推论；2026-09-02 处置按 agent_go 反馈 §2.2-3 软化为**改写**） | `cognitive-gap-closure-design` 计划把 escalate 决策表接入 `LoopIntervention`（stage 11） | escalate 是任务级决策（验证失败/升级），loop_detection 是轮次级安全；两者是不同层 | **解除与 `escalate.py` 的模块耦合，改写为轮次级独立升级策略**（自有输入信号 triggers/cooldown/max_run、自有输出语义 reset/retry/switch，不共享 `EscalationDecision` 类型；吸收决策表形式+幂等闸+熔断三件模式）；任务级升级随吸收归 agent_go。**边界澄清**：HealthGate 与 LRC-P1/P2 不受影响 |

### 2.2 🟠 Major（能力缺陷/契约缺口，应改）

| # | 问题 | 现状 | 建议 |
|---|---|---|---|
| M1 | **CapacityCriterion 拆分维度单一（只按读侧输入体积装箱）** | greenfield 任务（无 `input_files`）退化单原子任务（`split()` 对空 input_files 产出空 batches）；超大规格文档也拆不动（`split()` 只装箱 input_files）。注：读侧字节度量本身已于 2026-09-01 wiki 匹配修正（`decompose.py:34` docstring，真实 stat 字节优先） | 新增 **`ScopeCriterion`**：按 `constraints.modules`/feature 边界切分 + 每个子任务携带文档章节与交付目标 |
| M2 | **`_infer_dependencies` 是占位** | 返回 `[]`（`decompose.py:171`，docstring 自注"简化版"），依赖 DAG 不满足规范不变量②（无环） | 由模块 `deps` 生成真实依赖，拓扑排序决定执行顺序 |
| M3 | **信号面无契约化 API** | IFC 四指标/H_BE 只落文件（`hbe.jsonl`/`sessions.jsonl`），无稳定 JSON 端点 | 🆕 `GET /api/session/<key>/signals`（IFC/H_BE/压缩统计快照），`signal_types.SignalSnapshot` 为契约；兼作 HealthGate-P1 观测数据出口 |
| M4 | **召回服务无语义面** | 只有模型工具 `ctx_recall`（会话内自愈）；任务级 reload 无 agent 可调接口 | 🆕 `POST /api/task-context`（任务描述 → 上下文证据包），内部组合 manifest+recall+orig；裸接口降级 admin/debug；**留 SEM 语义卡扩展点**（跨会话命中丰富证据包，避免 agent 消费面定型后 SEM 注入无出口） |
| M5 | **reload 无防二次压缩机制** | 召回内容跨请求注入会被 truncation/compression 再压 → reload 死循环 | 🆕 `X-Proxy-Pin-Context: <anchor>` 头 + 各压缩 stage 跳过 pinned 锚点。**v1.1 补三约束**：① pin 预算由存储层强制（≤5% 上下文，见关联文档 §E），防客户端无限 pin 架空 OOM 防护；② 显式决策 `OOMSafetyFIFO`（stage 17）是否豁免 pinned 锚点——豁免 = Metal OOM 风险回归，不豁免 = 须写明"pin 在 oom_danger 档失效"的降级语义；③ pin 只落工作集/尾部（append-only 与前缀缓存约束，禁止注入历史区） |
| M6 | **测试覆盖缺口（真实文件字节装箱）** | 单测用不存在的伪路径（`test_decompose.py::_big_task` 以 `"x"*file_len` 后缀填充伪路径 → `os.path.getsize` 必 OSError → 走路径串长降级，恰好 ≈ file_len 伪造真实感），真实 `os.path.getsize` 装箱路径从未被验证 | 补 tmpfile 真实文件测试 |

### 2.3 🟡 Minor（设计味道/文档漂移）

| # | 问题 | 建议 |
|---|---|---|
| N1 | `Recaller(query, session_key)` 以 `sid` 当 query | 语义化：query 由任务描述符（input_files/描述/失败原因）构造，绑定 `POST /api/task-context` |
| N2 | 文档状态漂移：`three-layer-architecture-spec` 标 `ProtocolOrchestrator` "🔴 未实现（核心待建件）"；AGENTS.md 声称"Phase 2 接 pipeline" | 更新为"已实现、归属 agent_go、代理内为契约基准" |
| N3 | `PROTOCOL_BUDGETS` 仍是模块常量 | 按注释迁入 `CONFIG_REGISTRY`（键名 `PROXY_PROTOCOL_*`） |
| N4 | 单测里 `_big_task()` 等 fixture 无真实文件 | 随 M6 一并修 |

### 2.4 🔵 v1.1 复核新增盲点（本文自身的修订项）

| # | 盲点 | 处置 |
|---|---|---|
| B1 | **agent_go 实现语言未验证**——全文假设"整包迁移 7 模块（3-4 天）"。若 agent_go 非 Python（名称暗示 Go），Phase 2 是**重写**而非迁移：工期失效，"契约基准+参考实现"从过渡态变**永久态** | **✅ 已关闭（2026-09-02 agent_go 反馈实证）**：纯 Python（stdlib-only 运行时、pyproject.toml + pytest，名称与语言无关）；Phase 2 为迁移路径，共享契约收敛为"共享契约包"形态 |
| B2 | **M5 pin 的信任边界**——`X-Proxy-Pin-Context` 是客户端可控的上下文管理旁路，可无限 pin 架空 OOM 防护 | 已并入 M5 建议三约束（预算强制 / OOMSafetyFIFO 豁免决策 / append-only 约束） |
| B3 | **pin 与前缀缓存交互未讨论**——pinned 锚点注入历史区会破坏 append-only 不变式 | 已并入 M5 约束 ③ |
| B4 | **task-context（⑥面）与 SEM 语义卡关系未提**——SEM 跨会话命中应丰富证据包 | 已并入 M4（留扩展点） |
| B5 | **post_governance 拆分论证不完整**——DataQualityMonitor 留代理的理由未给出，其 SLA 数据半数产自 agent 侧验证结果 | §5.1 该行标注"待论证"，Phase 2 复核观测闭环完整性 |

---

## 3. 关键分析摘要（支撑结论的依据）

### 3.1 职责边界：代理 = 读侧，agent_go = 写侧

```
代理 (llama-defender)  管「模型读到什么」  → 输入工程: 压缩/截断/召回/路由/Signal 观测  ✅ 生产已挂载
客户端 (agent_go)      管「模型要做什么」  → 任务工程: 分解/执行/验证/升级 (P1-P5)          ← 真正归属
```

- **Signal 层挂代理自洽**：观察是被动的、只读的、fail-open 的（IFC/H_BE/PDC 都是"只测不动"）。
- **Protocol 层是主动控制**：主动控制必须有一个**任务所有者**——在当前架构里是 agent_go，不是代理。

### 3.2 任务级分解不会在代理运行时发生的三个硬矛盾

1. **协议层错位**：代理说"轮次协议"（Anthropic/OpenAI），P1 需要 `Task` 对象——运行时没有任何环节产生 Task。
2. **双脑问题**：agent_go 自身是 agent（有规划/子代理），代理再做任务分解 = 两个大脑控制同一段对话。
3. **职责边界已划**：R1-R12 集成契约里 agent_go 是任务拥有者，llama-defender 是服务方/基础设施。

### 3.3 上下文压缩与管理必然归代理

- **绑定后端物理事实**：context 窗口、KV/prefix cache、Metal 显存、max_tokens bug——只有代理持有。
- **每请求高频**：压缩/截断每轮都发生（stage 7/14/17）；任务分解一个任务一次。
- **多客户端共享**：上下文工程是公共设施，不能被某个客户端独占。
- **决策闭环同侧**：压缩需要的信号（H_BE/drop ratio/hit_ratio）都由代理生产。
- **连带所有权**：压缩归代理 ⇒ 压缩造成的"客户端-模型视图分歧"的对账（ledger/archive/canonical）也归代理——R14/R15 已完整落地。

### 3.4 召回的三层边界（决策与执行分离）

| 层 | 内容 | 决策方 | 执行方 |
|---|---|---|---|
| ① 内容恢复执行（检索/FTS/archive 读回） | "内容在哪、怎么取" | — | **代理**（唯一持有数据） |
| ② 会话内自愈召回 | "模型此刻缺上下文" | **模型**（ctx_recall 工具） | 代理（已闭环 ✅） |
| ③ 任务级 reload | "子任务失败、值得带恢复上下文重试" | **agent**（P5 升级决策） | 代理执行召回 + agent 重派子任务 |

**核心论证**：代理无法成为"自己压缩决策的请求方"——自己丢的自己捡回来是无意义循环；捡回的触发信号必须来自外部（模型或 agent）。reload 不是召回决策，是**失败升级决策**，触发信号（"子任务验证失败"）只在任务工程侧存在。

### 3.5 交互面：六类

| 面 | 内容 | 状态 |
|---|---|---|
| ① 控制面 | 启停/切换/热重载/watchdog/路由强制 | ✅ R1-R7/R12 |
| ② 路由/模型面 | `/api/route/policies`、`/v1/models` 能力元数据 | ✅ R8-R11 |
| ③ 归因/响应面 | `X-Proxy-Route-*` 头、`X-Proxy-Diag-*` 头、SSE 尾注 | ✅ R8/R13 |
| ④ 诊断数据面 | sent_view/ledger/sessions/metrics | ✅ R14-R16 |
| ⑤ 信号面 | IFC/H_BE/压缩统计 → `SignalSnapshot` 契约 | 🆕 需正式化 |
| ⑥ 召回服务面 | `POST /api/task-context`（语义封装） | 🆕 需新增 |

### 3.6 语义封装原则

**对外永远是能力，不是机制**。agent_go 的契约面只暴露任务语义（`POST /api/task-context`：给任务描述，拿上下文证据）；`recall`/`manifest`/`orig` 是 PDC 内部机制，降级为 admin/debug 面（供 trace/对账/人工审计）。理由：机制会变而语义稳定、知识归属代理、安全脱敏、与 R9/R13-R16 先例一致。

---

## 4. 修改清单（按执行顺序分四期）

> **2026-09-02 双端分工对齐**：agent_go 反馈 §三/§四给出任务分工——本端 LD-1..LD-8（LD-1/2/3 即本清单 Phase 1 三端点；LD-4 分解补完；LD-5 即 C2 改写；LD-6 编排器冻结；LD-7 文档同步；LD-8 R17-R19 契约版本化），对端 AG-1..AG-8（AG-2/3 吸收验证前置层与决策层、AG-4/5 消费 task-context 与 pin、AG-6/7 门控评估、AG-8 双端 E2E）。本清单与之对齐执行。

### Phase 0 — 契约固化与定位澄清（代理 + agent 双端，约 1 天）

- [x] ~~前置验证（B1）~~ **已关闭**：agent_go 反馈实证纯 Python（stdlib-only、pyproject.toml + pytest）——Phase 2 为迁移路径，共享契约按"共享包"形态
- [x] **前置决策（M5/B2）：pin 预算与 OOM 语义**——≤5% 预算记账在 `RequestParser` stage-0 强制（`PROXY_PIN_BUDGET_RATIO=0.05`）；`OOMSafetyFIFO` 不豁免 pinned 锚点，OOM 危险档设置 `ctx.pin_suspended="oom_danger"` 降级语义；注入点 append-only（尾部工作集，不污染历史区）。实现见 `pipeline.py`/`truncation.py`/`proxy_state.py`。
- [x] **抽取 `signal_types.py` + `protocol_types.py` 为共享契约包**——契约 R17-R19 已冻结并写入 `../01-requirements-product/llama-defender-integration-requirements.md` §3.3；物理拆包延至 Phase 2 与 agent_go 共建「共享包」形态，当前以文件级 `CONTRACT_VERSION` 对齐。
- [x] 更新 `AGENTS.md` / `CLAUDE.md` / `three-layer-architecture-spec` 状态表：Protocol Layer 定位改为"任务工程，归属 agent_go；本仓库为契约基准+参考实现（冻结）"，删除"Phase 2 接 pipeline"表述（反馈 LD-7）
- [x] ~~撤销 LRC-P3~~ → **改写完成**：`cognitive-gap-closure-design` §3.3 已改写为轮次级独立升级策略（解除 escalate 耦合，2026-09-02）
- [x] **契约草案先行（contract-first，2026-09-02 双端约定，见反馈 §六）**：R17/R18/R19 草案已入 `../01-requirements-product/llama-defender-integration-requirements.md` §3.3 及 §5 优先级表；**agent_go 评审确认无异议（2026-09-02），契约已冻结**——LD-1/LD-2/LD-3 开工授权生效；字段变更走 `CONTRACT_VERSION` 递增
- [x] **开工前置检查（2026-09-02）**：工作区变更盘点——本设计相关改动全部为文档（零运行时影响）；既有运维变更（models.json 路由偏好、ornith-9b gpu-mem 修正、proxy 空行）经 `models-validate`（hash=42b6d3218401c3dc）+ `config-lint` 全部通过；在跑 proxy（启动时快照）不受影响。**判定：可开工，首项即契约评审等待期内的 Phase 0 文档同步（LD-7）**

### Phase 1 — 代理侧补齐数据底座（为 agent 消费铺路，约 2-3 天）

> **执行进度（2026-09-02 后续）**：LD-1 已实现——`GET /api/session/<key>/signals`（`diagnostics.build_session_signals` 聚合 + 端点分支，契约 v1 冻结版全字段映射，404/410 对齐 R14/R15）；新增 `test_signals_endpoint.py` 5 用例。LD-2 已实现——`POST /api/task-context`（`ctx_recall.build_task_context_bundle` + `memory_stores.known_sessions` + `anthropic_proxy.py` 端点），新增 `test_task_context.py` 6 用例。LD-3 已实现——`X-Proxy-Pin-Context`（≤5% 预算强制、`truncation.py` fifo 中段跳过 + TS-2 原子对扩展、`OOMSafetyFIFO` oom_danger 挂起、`BackendDispatcher`/响应头传播），新增 `test_pin_truncate.py` 4 用例。LD-4 M6 真实文件字节装箱单测已补（`test_decompose.py::TestRealFilePacking` 2 用例）。单测套件 **1516 全绿**（2026-09-02 实测），signature/behavior 快照 PASS。**待 proxy 重启后 live smoke**。

- [x] 🆕 `GET /api/session/<key>/signals`：IFC 四指标 + H_BE + 压缩统计（复用 `ifc_metrics`/`hbe_probe`/诊断）
- [x] 🆕 `POST /api/task-context`：任务描述 → 上下文证据包（内部组合 `MANIFEST.lines` → `lookup` → `recover_full_content`，预算裁剪 `6000/20000`）；`recall`/`manifest`/`orig` 端点仅作 admin/debug 面
- [x] 🆕 `X-Proxy-Pin-Context` 头：≤5% 预算 stage-0 强制；`ContextTruncator` 对 pinned 锚点跳过截断（复用 TS-2 原子保护机制推广到原子对友邻）；`OOMSafetyFIFO` 不豁免；`BackendDispatcher` 传播 `X-Proxy-Pin-*` 响应头
- [x] 补真实文件字节装箱单测（`tmpfile` 写真实内容 → 验证 `os.path.getsize` 装箱路径）

### Phase 2 — 任务工程吸收 agent_go（2026-09-02 按 agent_go 反馈 §2.2-1 修订：~~整包迁移~~ → **组件拆解吸收**）

> 原方案否决理由：agent_go 已有完整任务工程栈（`generate_plan`/`plan_to_subtasks` ≈P1、验证循环+`evaluator.py` ≈P3、`replan.py` ≈P5、wave scheduler ≈编排）——整包迁移将在 agent_go 内复现 C1 的双脑问题；且 `protocol_orchestrator` 的 P2 执行器仅为 callable stub，迁移即执行面回退。

- [x] `protocol_orchestrator` **冻结为可执行规范（参考实现）**（LD-6）：模块 docstring 已加冻结声明，明确不再主动扩展、不接入运行时 pipeline，待 agent_go 组件拆解吸收。
- [ ] agent_go 按消费方拉力逐组件吸收（对端 AG-1..AG-8）：AG-2 验证循环机械前置层（verification_chain L1 → `evaluator.EvalStrategy` 前置，编译错/测试红/空 diff 在 LLM 语义评估前拦截）、AG-3 replan 确定性决策层（escalate 决策表+幂等闸+熔断 → `replan.py`，输出 `EscalationDecision`；**软依赖 LD-1**——决策表消费 reread_pressure 等 IFC 信号）、AG-4 task-context 消费端、AG-5 pin 注入支持、AG-6 decompose 判据吸收评估、AG-7 post_governance 吸收评估（依赖 B5 论证完成）
- [ ] **验收标准改写**：验证循环具备机械前置层、replan 具备确定性决策表、双端共享 `EscalationDecision` 契约——而非"7 模块出现在 agent_go"
- [ ] `contract_registry.py` 血缘标注**双态约定**（冻结期）：`P1_decompose` / `P5_escalate` producer 标注"参考实现（冻结）；生产归 agent_go"——避免与仓库内仍在运行的单测产生归属歧义，保持可审计
- [ ] 代理侧 LD-4 decompose 补完**拆两步投入**：契约澄清 + M6 真实文件装箱测试先行；`ScopeCriterion` 实现等 AG-6 拉力再动（避免给待归档代码超前投资）

### Phase 3 — 验证闭环（约 2 天）

- [ ] 代理侧：信号/召回/pin 三端点的契约测试 + 回归（`bash test/run_tests.sh --all`）
- [ ] agent 侧：E2E——任务 → Scope 分解 → 执行 → 验证失败 → escalate(reload) → task-context 召回 → pin 注入重执行 → 通过
- [ ] 双端契约一致性测试（`CONTRACT_VERSION` 漂移检测）

---

## 5. 迁移模块清单（剥离 vs 保留 vs 共享）

### 5.1 剥离到 agent_go（任务工程，7 模块）

| 模块 | 迁移要点 |
|---|---|
| `decompose.py` | 连同演进方向（ScopeCriterion）一起迁 |
| `escalate.py` | 与代理侧 `loop_detection` 是两层概念 |
| `verification_chain.py` | 与 `protocol_types` 契约强绑定 |
| `protocol_orchestrator.py` | 连同 `IllegalTransitionError`/`PROTOCOL_BUDGETS` |
| `idempotency.py` | **拆分**：decompose/verify 缓存迁 agent；recall 缓存留代理 |
| `post_governance.py` | **拆分**：EntryLayerCalibrator + PatternCompiler 迁 agent；DataQualityMonitor 留代理观测（**B5 待论证**：SLA 数据半数产自 agent 侧验证结果，留代理的观测闭环是否完整需 Phase 2 复核） |
| `protocol_types.py` | 主副本迁 agent |

### 5.2 共享契约（双端必须一致）

> **B1 已关闭（2026-09-02 agent_go 反馈）**：agent_go 为纯 Python——本表按**共享契约包**形态执行，双端各持 `CONTRACT_VERSION` 漂移检测测试（先例：`catalog_hash`）。

| 契约 | 生产方 | 消费方 |
|---|---|---|
| `signal_types.py`（SignalSnapshot） | 代理（ifc/hbe/诊断） | agent_go（决策输入） |
| `protocol_types.py` 的 `EscalationDecision` | 双端 | 双端 |
| `contract_registry.py` | 双端各自半边 | 血缘审计 |
| `unit_model.py` 的 `msg_hash` | 代理 | 双端观测对齐 |

### 5.3 保留在代理侧（输入工程/观测/数据底座）

| 类别 | 模块 |
|---|---|
| 内容压缩 | `content_compressor.py` |
| 上下文截断 | `truncation.py` |
| 上下文工程引擎 | `context_engine.py` |
| 生命周期/预算 | `lifecycle.py`、`queue_manager.py` |
| 召回执行体 | `ctx_recall.py`（检索/恢复）、`memory_stores.py`（manifest/orig） |
| 信号生产 | `ifc_metrics.py`、`hbe_probe.py`、`diagnostics.py`、`session_ledger.py` |
| 会话安全 | `loop_detection.py`（轮次级） |
| 协议/路由/配置 | `anthropic_proxy.py`、`pipeline.py`、`backend_strategy.py`、`model_registry.py`、`admin_server.py` 等 |

---

## 6. 结论

> **实现正确，归属错误**。编排闭环质量合格（状态机/幂等/死循环防御/性能预算达标；**分解器能力待建**——M1/M2 实证），但它是一套"写在代理里的客户端能力"。按所有权边界重构：**任务工程（P1-P5）以"组件拆解吸收"方式归 agent_go，代理保留输入工程（压缩/截断/召回执行/信号/诊断）并提供 signals + task-context + pin 三个契约化出口（R17-R19）**——迁移完成后，当前"编排器独立成模块、不进 pipeline"的状态将从"未完成"变为"架构正确的克制"。

**一句话边界**：

> 代理管「模型读到什么、装不装得下、丢了怎么找回」（输入工程），agent_go 管「模型要做什么、做对没有、失败怎么办」（任务工程）。
