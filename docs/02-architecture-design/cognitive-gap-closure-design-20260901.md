# 认知空白点收口设计：语义记忆 / 循环退火回滚 / 健康门 / 工具准入契约

> **状态**：设计提案（未实施）｜**日期**：2026-09-01
> **来源**：DeepSeek 对话《AI Agent 系统架构：长上下文、元认知与跳出思维循环》与本仓库七维映射对照分析的四个空白点——①语义层长期记忆 ②循环时硬重置/回滚/冷却 ③语义健康指标接入运行时（监控信号优先于数据信号）④工具输出准入契约运行时执行。
> **一句话**：信号层（IFC/H_BE/台账/manifest）已经"只测"但还没"有用"——本设计用四个最小增量把既有传感器接进既有执行器（循环干预、PDC 链路、escalate 决策表），全部复用现成链路、零新增进程、零第三方依赖、影子先行灰度放量；不引入真抢占，不破坏 append-only 与 prefix-cache 纪律。

---

## 0. 设计原则（约束前置）

继承 IFC/PDC 设计方法论，本设计受六条硬约束：

1. **观测先行**：每个方案的 P1 阶段一律为纯观测（log + metrics 记账，零行为干预），凭对照数据决定是否放量（同 IFC 效度关卡哲学）。
2. **tail-append 纪律**：一切注入只追加在发送视图尾部（`SessionLoopState` stage 10 先例）；任何改写只作用于**本批新增**消息（写入期压缩同槽位，`context-architecture-evolution-20260829.md` §挂载不重排已确认合法）。历史消息永不回溯改写。
3. **默认关**：所有新控制项 reloadable + 默认关闭，active 配置按需灰度开启。
4. **stdlib only**：不引入第三方包；检索复用 sqlite3/FTS5（选型文档已验证），执行复用既有 urllib 分发路径。
5. **fail-open**：信号缺失/计算异常时一律跳过干预，管线按默认路径运行（`signal_types.py` 全 Optional 字段同款纪律）。
6. **不新增干预机制**：优先"劫持既有执行器"（循环干预 L1-L3、fifo 截断、PDC 恢复链），不发明新的控制通道。

---

## 1. 背景与空白点盘点

### 1.1 现状锚点（2026-09-01 代码实测）

| 已有能力 | 代码锚点 | 状态 |
|---|---|---|
| 循环干预 L1 提示 / L2 移工具 / L3 全禁 | `loop_detection.py::_apply_loop_intervention`，管线 stage 8-11（`pipeline.py:1567`） | ✅ 闭环 |
| 循环状态跨轮持久（level + triggers） | `_ps._LOOP_SESSION_STATE`（`pipeline.py:1542`） | ✅ 但见 1.2 缺口 |
| IFC 信号 per-turn 产出 | `diagnostics.py:283`（view_summary）、`diagnostics.py:431`（build_ifc_section：retention / rationale_ratio / action_div / reread_pressure） | ✅ **只写不读** |
| escalate 决策表（reload/retry/reset/switch） | `escalate.py:161`（reread_pressure≥2→reload）、TaskState FSM | ⚠️ 未接线（唯一潜在消费方） |
| PDC 恢复链（manifest 索引 + archive 全文 + ctx_recall 三级穿透） | `memory_stores.py` / `ctx_recall.py::fts_search / recover_full_content` | ✅ 闭环 |
| 契约注册表（元数据 + 运行时 validate） | `contract_registry.py` | ⚠️ 零消费方 |
| 工具输出点状大小控制 | `ctx_recall.py::MICRO_TURN_RESULT_MAX_CHARS=2000`（仅微轮） | ⚠️ 无普适准入 |

### 1.2 四个缺口（对号入座）

| # | 缺口 | 具体表现 |
|---|---|---|
| ① | 语义层长期记忆 | 台账/archive/manifest 是**情景**记忆（词面可检索）；换一种说法即 miss 的抽象知识（项目约定、踩坑模式）无处沉淀 |
| ② | 循环硬重置/回滚/冷却 | `pipeline.py:1591`：max_run 回落即 level **立即归零** → 振荡（触发→解除→再触发）；triggers 累加但从不升级处置；循环毒轮次留在上下文每轮污染推理，无回滚 |
| ③ | 语义健康指标入运行时 | IFC 信号是上一轮的**事后**信号，max_run 是本轮的**事前**信号；后者独占控制权，违背"监控信号优先于数据信号" |
| ④ | 工具输出准入契约 | tool_result 下一轮回流时无 schema/大小/新鲜度准入，超限/坏输出直接进上下文 |

---

## 2. 方案 A：语义层长期记忆（SEM，语义卡蒸馏）

**定位**：情景记忆（已有）之上加一层**语义**记忆——LLM 自蒸馏的结构化知识卡，纯 stdlib。embedding/sqlite-vec 路径（选型文档预留）远期再议。

### 2.1 数据面

- **语义卡**（SemanticCard，TypedDict）：`{card_id, project_key, domain, pattern, evidence[], created_ts, hits, last_hit_ts}`。
- **存储**：`logs/diag/semantic/<project_key>.jsonl` 增量落盘；复用 `memory_stores.py` 骨架（MB 上限 → 删最老会话文件；条目上限 → TTL/hits LRU 驱逐）。
- **project_key**：从台账 session 键派生（cwd/repo 粒度），跨会话聚合。

### 2.2 写入路径（蒸馏）

- **触发器**：会话达到 `PROXY_SEM_DISTILL_MIN_TURNS` 轮，或台账 TTL 驱逐（`PROXY_DIAG_SESSION_TTL_MIN=180`）前 best-effort——挂 diagnostics per-turn 钩子，**事件触发**（非每轮探针，同 H_BE 采样纪律）。
- **执行**：一次后台请求，输入 = 台账 action 轨迹摘要 + sent_view 高频去重后材料清单；输出 = JSON 语义卡（≤3 张/次）。优先云端 flash 模型（便宜、不占本地推理窗口），不可用时跳过（fail-open）。
- **幂等**：写前经 `idempotency.py` 内容寻址去重；pattern 相似（FTS5 词面 + bigram Jaccard）则合并 evidence 而非新增卡。

### 2.3 读取路径（注入）

- **L0 语义卡检索**：`ctx_recall` 三级穿透检索之前，先对语义库做 FTS5 检索（整句短语 → token 聚合，同款三级可复用）；命中 `PROXY_SEM_INJECT_MAX` 张以内。
- **注入**：尾部 append 系统附录（`[Semantic memory: ...]`，含卡 ID 便于计量），每卡 `PROXY_SEM_INJECT_MAX_CHARS` 封顶。诊断记账新 injection kind `semantic_card`。

### 2.4 阶段

| 阶段 | 内容 | 放量门槛 |
|---|---|---|
| SEM-P1 | 蒸馏 + 落盘，只写不注入（影子） | 卡内容人工抽检有效率 ≥60% |
| SEM-P2 | L0 检索 + 注入 | 注入轮次的 ctx_recall 拉取率下降（语义卡预先回答 = 价值证据） |
| SEM-P3 | 跨会话合并 + hits 频率自校准（PDC revealed ground truth 同款） | — |

---

## 3. 方案 B：循环退火 / 回滚 / escalate 接线（LRC）

**定位**：给唯一已闭环的行为表面环补上"时间维度"——退火（冷却半衰）、升级（triggers 不再被浪费）、回滚（毒轮次出清），并把 escalate 决策表变成第一个真实消费点。

### 3.1 退火（LRC-P1，纯状态机，零前缀影响）

改造 `_LOOP_SESSION_STATE` 条目为 `{level, triggers, last_trigger_ts}`：

- **半衰降级**：max_run 回落时不再立即归零——距 `last_trigger_ts` < `PROXY_LOOP_COOLDOWN_S` 内 level 只降一级；冷却窗外才归零。（对应"时间退火"：给系统恢复期，防振荡。）
- **triggers 升级**：`triggers ≥ PROXY_LOOP_TRIGGER_ESCALATE`（默认 3）时本会话后续触发**直接 L3 起步**（跳过 L1/L2 渐进）——反复循环的会话已证明渐进无效。
- L1/L2 干预成功即写入 `last_trigger_ts`；归零保留 `triggers`（只按 `PROXY_LOOP_TRIGGERS_TTL_MIN` 滑窗过期，默认 60min）。

### 3.2 回滚（LRC-P2，发送视图尾部截除，灰度）

- **触发**：L2+ 或 L3 干预发生时，且 `PROXY_LOOP_ROLLBACK=true`。
- **动作**：`LoopIntervention`（stage 11）内，对本轮发送视图自尾部截除参与重复的 tool_use/tool_result **完整对**（复用 truncation TS-2 原子保护的反向遍历），K 由 `consecutive` 计数反推、上限 `PROXY_LOOP_ROLLBACK_MAX_TURNS`（默认 6）；截除后尾部 append `[System: rolled back K looping turns; do NOT re-attempt the removed actions.]`。
- **prefix cache 账**：尾部截除与 fifo 截断同构——截除点**之前**的前缀保持命中，只有被移除的 span 与其后的新增内容冷 prefill。对比死亡循环（每轮全量生成长输出），净赚。本地 hybrid 模型（`--hybrid-cache-entries 8`，trim-free 整条匹配）注意：截除发生在**请求构造期**，不触碰 rapid-mlx 缓存条目管理，无冲突。
- **安全阀**：截除后剩余轮数 < `PROXY_CTX_KEEP_MESSAGES/2` 时放弃回滚（防止把会话截穿）。

### 3.3 轮次级升级策略（LRC-P3）——**2026-09-02 改写**（原"escalate 接线"撤销）

> 演进轨迹：原设计拟将 `escalate.py` 决策表接入 `LoopIntervention`；经 [protocol-layer-ownership-review-20260902.md](protocol-layer-ownership-review-20260902.md)（C2）指出任务级/轮次级错位，v1.1 标注撤销；经 agent_go 反馈（`~/workspace/agent_go/docs/in/protocol-layer-ownership-review-feedback-20260902.md` §2.2-3）软化为**改写**——解除与 `escalate.py` 的模块耦合，保留决策表形式化为**轮次级独立升级策略**。

改写后设计：

- **自有输入信号**：`triggers`（`_LOOP_SESSION_STATE` 滑窗计数）、`last_trigger_ts`（冷却窗）、`max_run`、`reread_pressure`（可选，经 `GET /api/session/<key>/signals` 即 review LD-1；纯本地信号时无外部依赖）；**不消费 `EscalationDecision` 类型**（该契约保持任务级语义，归 agent_go）
- **自有输出语义**：`reset`（触发 §3.2 回滚）/ `retry`（尾部注入换路指令，既有 L1 模式）/ `switch`（仅 log 路由建议，人闭合，不自动切云防误计费）
- **吸收件**：escalate 的**决策表形式 + 幂等闸 + 熔断器**三件模式（非代码模块复用）——同一循环 30min 内重复升级 ≤1 次，防升级振荡
- 触发条件沿用：`loop_level == 3` 且 `triggers ≥ PROXY_LOOP_TRIGGER_ESCALATE`
- HG-P2 劫持循环干预路径不受影响（IFC 是代理原生轮次级信号）

### 3.4 阶段

| 阶段 | 内容 | 放量门槛 |
|---|---|---|
| LRC-P1 | 退火 + triggers 升级（纯内存状态机） | loop 触发率不升、会话中断率下降 |
| LRC-P2 | 回滚灰度（active 配置手动开） | 回滚后 3 轮内再触发率 < 不回滚对照 |
| LRC-P3 | 轮次级独立升级策略（2026-09-02 改写，原 escalate 接线撤销，见 §3.3） | 决策表形式化+幂等闸+熔断落地；同一循环 30min 内重复升级 ≤1 次 |

---

## 4. 方案 C：HealthGate 前置门（HG，监控信号优先）

**定位**：不做真抢占（ThreadingHTTPServer 顺序管线中打断上游 stage 违背现有结构、收益低），改做**前置门**——用上一轮的**事后**语义信号（IFC）对本轮获得控制权，途径是劫持既有循环干预路径，不新增执行器。

### 4.1 挂载点

新 stage `HealthGate`（编号 0.75，位于 RequestParser 之后、LifecycleClassifier 之前；亦满足"监控信号优先于数据信号"——它先于 stage 8-11 的数据信号看到控制结论）。

### 4.2 信号读取

- 来源：diagnostics 会话内存态中**上一轮** IFC section（`diagnostics.py:431` 已 per-turn 落 `record["ifc"]`）；内存缺失时从 `logs/diag/sessions.jsonl` 尾部兜底读取（fail-open：读不到即跳过）。
- 触发条件（满足其一）：
  - `action_div < PROXY_HEALTH_GATE_MIN_ACTION_DIV`（默认 0.3）**连续 ≥2 轮**（游走/坍缩双端检测的双端语义）；
  - `reread_pressure ≥ PROXY_HEALTH_GATE_MIN_REREAD`（默认 2）。

### 4.3 动作（分两级）

| 级别 | 动作 | 前缀影响 |
|---|---|---|
| HG-P1（观测） | 仅 log + metrics 标记 `health_gate_would_trigger` | 零 |
| HG-P2（干预） | `ctx.max_run = PROXY_LOOP_THRESHOLD`（**劫持 stage 11 既有 L2/L3 路径**，含退火/回滚联动）+ 尾部 append 信号摘要（`[System: contextual health degraded — action_diversity=…; change approach now.]`，记账 kind `health_gate`） | 仅尾部 append |

### 4.4 扩展信号（HG-P3）

- H_BE 信念熵骤降（模型对自身输出的置信坍缩）作为第三信号源接入同一门（`logs/diag/hbe.jsonl` 已 per-turn 落盘，只读回流，不改变探针"只测不动"的定位——动的只是门）。

---

## 5. 方案 D：工具输出准入契约（TAC）

**定位**：contract_registry 从"静态文档"变"运行时准入基线"；tool_result 回流时三查，超限/坏输出不直接进上下文，走 PDC 现成恢复链。

### 5.1 准入点

新薄 stage `ToolAdmission`（编号 0.8，RequestParser/HealthGate 之后）：对照 session_ledger 材料清单识别**本批新增**的 tool_result（历史消息一律不动，守住 tail-append 纪律）。

### 5.2 三查

| 查 | 条件 | 动作（按 `PROXY_TOOL_ADMISSION_MODE`） |
|---|---|---|
| ① 大小 | chars > `PROXY_TOOL_RESULT_MAX_CHARS`（默认 20000） | memory_stores 记 manifest + 原文经 archive 可恢复（`recover_full_content` 现成）+ 就地截断留 `ctx_recall` 指引（PDC-L1 摘要同款） |
| ② 空值/错误 | is_error 或内容为空 | 记账；连续同错误喂给既有 blocker 检测（stage 4）输入 |
| ③ 结构 | JSON 类工具输出不合 schema | `contract_registry.validate(...)`（fail-open：只记 violation，不改内容） |

### 5.3 记账与回填

- 每项 violation 记账新 injection kind `contract_violation`（含 tool 名、违反类型、原大小）；diagnostics per-turn 关联。
- violation 聚合（同工具同类型 ≥N 次）→ 回填 `contract_registry` 元数据（如为该工具登记 size 上限）——契约库从人工声明进化为运行时学习基线。
- **模式**：`observe`（P1，只记账不截断）/ `enforce`（P2，大小准入生效）/ `enforce+schema`（P3）。

---

## 6. 配置面汇总（CONFIG_REGISTRY 新增项）

全部 reloadable、默认关（观测记账类开关允许默认开）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `PROXY_SEM_ENABLED` | `false` | 语义卡总开关（含蒸馏与存储） |
| `PROXY_SEM_DISTILL_MIN_TURNS` | `12` | 蒸馏触发轮数 |
| `PROXY_SEM_INJECT_MAX` | `2` | 每请求注入语义卡上限 |
| `PROXY_SEM_INJECT_MAX_CHARS` | `1200` | 每卡注入字符封顶 |
| `PROXY_LOOP_ANNEAL_ENABLED` | `false` | 退火 + triggers 升级（LRC-P1） |
| `PROXY_LOOP_COOLDOWN_S` | `300` | 冷却窗口：窗内 level 只降一级 |
| `PROXY_LOOP_TRIGGER_ESCALATE` | `3` | triggers 达此数后直接 L3 起步 |
| `PROXY_LOOP_TRIGGERS_TTL_MIN` | `60` | triggers 计数滑窗过期 |
| `PROXY_LOOP_ROLLBACK` | `false` | 发送视图回滚（LRC-P2） |
| `PROXY_LOOP_ROLLBACK_MAX_TURNS` | `6` | 单次回滚最大轮数 |
| `PROXY_HEALTH_GATE_ENABLED` | `false` | HealthGate 总开关（P1 观测/P2 干预由级别参数区分） |
| `PROXY_HEALTH_GATE_MODE` | `observe` | `observe` / `enforce` |
| `PROXY_HEALTH_GATE_MIN_ACTION_DIV` | `0.3` | action_div 触发阈值 |
| `PROXY_HEALTH_GATE_MIN_REREAD` | `2` | reread_pressure 触发阈值 |
| `PROXY_TOOL_ADMISSION_ENABLED` | `false` | 工具准入总开关 |
| `PROXY_TOOL_ADMISSION_MODE` | `observe` | `observe` / `enforce` / `enforce+schema` |
| `PROXY_TOOL_RESULT_MAX_CHARS` | `20000` | tool_result 准入大小上限 |

同步动作：`manage.sh` 加默认值、`proxy_config.py::CONFIG_REGISTRY` 注册、CLAUDE.md / AGENTS.md §11 补表。

---

## 7. 实施优先级与阶段门

```
Wave 1（纯观测，零行为干预）   HG-P1 + TAC-P1(observe) + SEM-P1(影子蒸馏)
Wave 2（低风险状态机/准入）    LRC-P1(退火) + TAC-P2(enforce 大小)
Wave 3（干预灰度）             LRC-P2(回滚) + HG-P2(enforce)
Wave 4（接线与建设）             LRC-P3(轮次级升级策略·改写) + SEM-P2/L0 + TAC-P3(schema) + HG-P3(hbe)
```

**阶段门**（不达标止步于观测，同 IFC 效度关卡哲学）：

- HG P1→P2：观测期内 loop_l2/l3 实际触发轮中 HealthGate 条件命中率 ≥60%（说明信号有效度），且误报（触发后模型自行恢复）不劣化。
- LRC-P1 验收：触发振荡（30min 内 ≥2 次 L2+）频次下降 ≥50%。
- TAC P1→P2：violation 记账中"超限且后续被引用"占比 <20%（截错成本低）。
- SEM-P1 卡有效率 <60% → 停在影子（蒸馏质量不足以值得注入成本）。

**优先顺序理由**：③P1 零风险先行（信号已在产，只是没人读）> ②P1 纯内存状态机 > ④P1 观测 > ① 成本最高（每会话一次后台 LLM 调用）最后。

---

## 8. 测试锚点

| 层级 | 内容 |
|---|---|
| 单元 | `test/unit/test_loop_anneal.py`（半衰/升级/滑窗过期状态机）、`test_health_gate.py`（信号读取 fail-open、阈值判定、observe 不改 messages）、`test_tool_admission.py`（三查、manifest 记账、历史消息不动）、`test_semantic_store.py`（卡存储/去重合并/驱逐） |
| 既有回归 | `bash test/run_tests.sh --unit`（25 文件 979 用例零回归）；改 pipeline 挂载须 `--all`（流式/非流式、双协议端点、循环检测） |
| 签名/快照 | 新增公开函数后跑 `--signature` + `--snapshot` 重生成 |
| 集成 | mock_backend 场景：构造连续重复 tool_use 序列验证退火升级与回滚截除的原子性（tool_use/tool_result 对不拆散） |
| 观测验证 | HG-P1/TAC-P1 上线后 `GET /metrics` 应出现 `health_gate_would_trigger` / `contract_violation` 指标；diagnostics `injections` kind 可计量 |

---

## 9. 风险与不做清单

**风险与对策**：

| 风险 | 对策 |
|---|---|
| 回滚破坏 prefix cache（本地 hybrid 模型敏感） | 尾部截除与 fifo 同构，截除点前前缀保持命中；默认关，active 灰度；与 `PROXY_CTX_ENGINE_ENABLED=false` 的稳定前缀策略不冲突（不回溯 canonical） |
| HealthGate 误报（action_div 低≠循环，可能是有序重读） | P1 观测先行 + 阶段门；`enforce` 仅劫持既有 L2（移除单一工具），不直接 L3 |
| 语义卡蒸馏质量差、注入噪音 | 卡有效率阶段门；注入上限双封顶（张数+字符）；fail-open |
| 准入截断截掉有效内容 | PDC 恢复链保证**可恢复性**（manifest 索引 + archive 全文 + ctx_recall 三级穿透），截断是有损视图而非丢弃 |
| 信号读取竞态（diagnostics 会话态并发） | 读侧仅取上一轮已完结记录；`LOCK_WAIT_S` 有界等待先例（hbe_probe）；读失败 fail-open |

**明确不做**：

- **不做真抢占/运行时中断**：顺序管线中打断后端在途生成属 stream idle watchdog（已有）职责范畴；HealthGate 只在请求构造期决策。
- **不动 canonical / 客户端历史**：一切改写限发送视图本批新增或尾部；客户端零改动。
- **不自动切路由**：escalate `switch` 决策只出建议（人闭合），避免云端误计费。
- **不引入 embedding/向量库**：语义检索先用 FTS5 词面 + bigram；sqlite-vec 留待选型文档预留窗口。
- **不引入 protocol_orchestrator 全家**：任务级决策随 Protocol 层归 agent_go（2026-09-02 评审+反馈定论）；代理侧仅以**轮次级独立策略**形式吸收 escalate 的决策表/幂等闸/熔断三件模式（见 §3.3），编排闭环仍按三阶段规范另行推进。

---

## 10. 关联文档

- `information-fidelity-control-design-20260829.md`：IFC 信号定义与效度关卡方法论（本设计 HG 的传感器上游）
- `progressive-disclosure-context-serving-design-20260829.md`：PDC 恢复链（本设计 TAC 的执行器上游）
- `memory-storage-requirements-selection-20260829.md`：存储选型（SEM 卡存储复用其双轨形态）
- `rag-kv-storage-fit-survey-20260901.md`：RAG/KV 组件匹配度调研——SEM-P2（注入）前置的 embedding 生产方式决策（sidecar/蒸馏/云端）依据所在
- `skill-progressive-disclosure-insights-20260901.md`：Skill 渐进披露机制分析与压缩借鉴（索引行触发词工程与 §2 方案 A 的索引质量度量共用；可再生优先压缩为独立并行小增量）
- `three-layer-architecture-spec-20260830.md`：Protocol 层规范（escalate 接线的契约依据）
- `protocol-layer-ownership-review-20260902.md`（v1.2）+ agent_go 反馈（`~/workspace/agent_go/docs/in/protocol-layer-ownership-review-feedback-20260902.md`）：LRC-P3 由"接线 escalate"经"撤销"最终**改写为轮次级独立升级策略**（解除 escalate 耦合，吸收决策表/幂等闸/熔断三件模式）；任务工程归 agent_go；HG/LRC-P1/P2/TAC/SEM 不受影响
- `../01-requirements-product/llama-defender-integration-requirements.md`：injection kind 计量契约（D6 先例）
