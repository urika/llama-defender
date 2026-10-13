# 决策级联流程设计：规则 → Jev → 分层升级（20261013）

> 状态：**提案**（设计文档，未评审、未冻结；跨仓落地须经 swe-eval 联合协调治理
> `~/APP/swe-eval/docs/joint-coordination-llamacpp.md` 核号，不得当已定方案使用）。
>
> 来源：Jev 使用策略调研（`docs/06-reference-metrics/jev-mcp-tool.md` §使用策略与校准核验）
> ＋ 业界实践调研（2026-10-13）。关联：swe-eval `jev-paradigm` / `jev-cchannel-design`、
> agent_go 成本控制三层与 eval gate。
>
> 一句话：**规则管「必须对的」，Jev 管「需要理解的」，升级通道管「都没把握的」，
> 抽审计「Jev 太有把握的」。次序固定：确定性规则 → Jev 概率判定 → 分层升级。**

## 1. 流程定义（本仓讨论沉淀的四层结构）

```
输入
 │
 ├─ Tier 0  确定性规则（~0ms，免费，fail-closed）
 │          密钥/危险命令/算术/日期/金额核验、可枚举匹配
 │          命中即拦截或放行，不进模型；高精度低召回（漏的交 Tier 1）
 │
 ├─ Tier 1  Jev 概率判定（~0.1s，state 已过滤无关项）
 │          双阈值分带（源自 cascade 双阈值思想，arXiv:2502.09054）：
 │            p ≥ φ_accept            → 直接执行（按比例 ρ 抽审，见 §3.1）
 │            ξ_abstain ≤ p < φ_accept → 灰区，↓ Tier 2
 │            p < ξ_abstain           → 拒答，直接 ↓ Tier 2（不浪费 Tier 1 复判）
 │
 └─ Tier 2  分层升级（贵档，逐级抬升成本，任一级可终结）
            ① 补数据重判：把缺失证据加进 state 重调 Jev（~1s，最便宜，先做）
            ② 规则/计算核验：查库、算数、确定性代码核对（ms–s）
            ③ 小 LLM 单点判断（~$0.001）
            ④ 大模型 / 人工（最贵，兜底）
```

**分工判据（能否交给上一层）**：能写成确定性规则的**必须**是规则；写不成规则、
但答案空间能在代码里枚举的语义判断才是 Jev 的活；答案空间本身不确定的进 Tier 2。

## 2. 业界实践综述（2026-10-13 检索）

| 出处 | 结论 | 对本设计的输入 |
|------|------|---------------|
| Flowable 2026.1 Guardrails | 分层护栏成熟能力：确定性规则 → policy agent（LLM 判官）→ 服务检查；「block the cheap and obvious cases without a model call」 | 规则前置是产品级共识，非个别做法 |
| FutureAGI《LLM Guardrails 2026》 | **「scanners before classifiers」**：8 个 <10ms 确定性扫描器先行（Jailbreak/Secrets/CodeInjection/…）；小快分类器在大模型前；**护栏自身有延迟预算**：输入轨 p50<100ms、工具调用轨 p50<100ms（阻断外发副作用） | Tier 0 清单化 + 全链延迟预算写入验收 |
| arXiv:2502.09054《Cost-Saving LLM Cascades with Early Abstention》 | cascade 双阈值：**deferral 阈值 φ**（低置信升级下一档）+ **abstention 阈值 ξ**（更低置信直接拒答）；early abstention 让便宜模型提前弃权，省掉贵模型调用 | Tier 1 三带（执行/灰区/拒答）的理论依据 |
| Openlayer / Maxim / DigitalApplied（OWASP LLM Top 10 对齐） | 输入/输出双轨护栏；**每次工具调用都是执法点**（schema 校验是最小可行执行轨） | agent 类工具链落地位置 |
| Morphllm | guardrail = 检查 + **动作**（allow/block/rewrite/alert/route），不是日志 | 升级通道的出口必须是动作（route 到下一档），不是仅记录 |
| Jev 生态实证（toolgate / jev-engineering / JevLoop / wakegate） | toolgate：静态规则先跑，Jev 七问只过规则漏掉的；jev-engineering：注入攻防 300 次，直白注入推不动 0/30（规则前置），权威措辞可移动 3/30（纯 Jev 会被注入） | 安全类判定规则必须在 Jev 之前，硬安全永不单靠概率模型 |

**业界共同形态**：这不是新发明，是把已收敛的 guardrail/cascade 经验应用到
`jev_decide` 这个具体仪器上。

## 3. 三个成立前提（讨论沉淀，缺一能效反噬）

### 3.1 「无法判断」是代码定义的灰区，不是 Jev 的诚实自查——confident wrong 是级联死穴

Jev 不原生输出「我不知道」，拒答带由阈值切出；**捞得回低置信样本，捞不回
高置信但错的样本**（实测：扑克实验 16/16 全押、dice 实验 400 次全押面 1）。
对策两件，必须都做：

- **随机抽审**：对 p ≥ φ_accept 的放行样本按比例 ρ（起步 5%）抽送 Tier 2，
  持续测**假通过率**——不抽审就无法知道阈值外躺着多少自信错误；
- **硬规则前置**：确定性可查的（算术/日期/金额/密钥/权限）一律不进模型。

### 3.2 升级路径分层，不一刀跳最贵档

「无法判断」的常见根因是 **state 不完整**（Jev 封闭：信息不在输入里只能猜），
所以 Tier 2 的第一动作是**补数据重判**，而非直接上 LLM——这一档往往便宜两个
数量级。

### 3.3 阈值在**自己的**分布上校准，盯三个率

TypeSafe 的 RLCD 校准只在自有分布成立。落地必须：固定判定集 + 重复多次 +
洗牌选项（Vals AI / Convex 方法），画覆盖-准确率曲线选 φ/ξ，并持续盯：

| 指标 | 恶化含义 |
|------|---------|
| 覆盖率（自动化率） | 太低 = 没省钱，退化成全走贵档 |
| 假通过率（false accept，抽审测得） | 上升 = 分布漂移，事故前兆 |
| 假拒答率（false reject） | 升级通道被琐碎样本淹没，Tier 2 成本反弹 |

## 4. 跨仓落地路径（提案，须经各自治理评审）

### 4.1 swe-eval：C 通道归因的三级化（最成熟场景，已有 91% 战场）

现状：`jev-paradigm-20260926.md`（结构化判断仪器）、`jev-cchannel-design-20260926.md`
（失败归因：部分可解实例 513 failed runs 占 91%，jev 是候选武器）、
`jev-call-card` / `jev-verification-requirements` / `jev-efficacy-preregistration`
（调用契约与效能预注册已成体系）。本流程的接入点：

- **Tier 0（规则归因）**：粒度判定已是规则可算的（实例级 = 该实例 0 resolved；
  其余归 run 级——C 通道设计 §0 的硬结论 1「粒度不需要人定义，可由数据算出」）。
  把这类**数据可算的归因**明确划为 Tier 0，不消耗 jev 判定；
- **Tier 1（jev 语义归因）**：对规则漏掉的 run 级失败判归因类目——保持现有
  「读分布不读标签」纪律与三型调用契约不变，新增的只是**双阈值分带**：
  高置信归因直接入档，灰区/拒答进 Tier 2；
- **Tier 2（升级）**：灰区归因升级为「补证据重判 → 规则复核 → 人工审计」；
  对应既有装备 `swe-instance-audit` 与 `jev-residual-register`（残差登记）
  ——**复用优先，不新建装备**；
- **抽审计**：与 `jev-bias-ablation-preregistration`（偏置消融）和
  `jev-slots-preregistration` 的抽样设计合并考虑，避免双份抽样成本。
- **治理**：以上任何采纳经 `joint-coordination-llamacpp.md` 登记与核号；
  防线 10（外发只审计不设禁令）与 CON-3 仪器可信度前置继续适用。

### 4.2 agent_go：评审/门禁链路的便宜初筛（提案）

现状：`Plan → Decompose → Execute` 编排；成本控制已是规则级三层（L1 单调用封顶 /
L2 子任务累计 / L3 任务熔断）；`eval gate` 以 Accepted Delivery 为产品 KPI；
`review --task` 支持 `--deep` 独立模型分析；`eval judge` N 模型互审。接入点
（按 ROI 排序）：

1. **review 初筛**：`review --task <id>` 的 approve/reject 前加 Jev 灰区初筛
   ——对 diff + 验证命令结果做 choice/noul 判定，高置信 approve/明显 reject
   直接走，灰区才升 `--deep`（独立模型分析）。**预期省下 `--deep` 的大头调用**，
   因为多数子任务是平凡的；
2. **命令安全轨**：worktree 内子任务执行的 shell 命令先过确定性危险模式规则
   （Tier 0），规则漏掉的语义风险（如编码混淆的外发）交 Jev noul 七问式检查
   （参考 toolgate 形态），灰区升级为「挂起问人」而非阻断——对齐 agent_go
   `--budget-mode` 的 strict/degrade/ignore 语义；
3. **成本控制升级决策**：L3 熔断后的恢复策略选择（等待/降级模型/放弃）可
   用 Jev choice 做初判，但**熔断本身保持纯规则**——硬安全与钱袋子不押概率模型。
- **落地纪律**：agent_go 为独立仓（`~/APP/agent_go`，stdlib-only），接入须
  自带校准数据与抽审机制（§3.3），且遵守其 `eval metric-freeze` 不可变 KPI 基线
  ——先以 shadow 模式跑（只记录不生效），对照后再启用。

### 4.3 llama.cpp 代理本体（远期参考，不在当前计划）

本仓 P3 验证链（L1 机械 → L2 语义 → L3 台账）与 P5 升级协议（`escalate.py`：
决策表 + 熔断器 + TaskState）已与本流程同构；若将来把 `jev_decide` 接进闭环，
缺的不是骨架而是 §3.3 的校准数据与 §3.1 的抽审机制。**当前不动运行路径**
（jev 仪器继续只读旁路，对齐 jev-mcp-tool.md 的定位）。

## 5. 验收与红线

- **延迟预算**（借 FutureAGI 口径）：Tier 0 p50<10ms；Tier 1 p50<150ms；
  全链（未升级样本）p50<200ms——超预算先砍 state 再考虑换仪器；
- **能效账**：目标形态 = Tier 0 拦一部分 + Tier 1 高置信放行大部分 →
  进入 Tier 2 的 ≤ 个位数百分比；灰区率是核心观测值，持续 >30% 说明
  criteria/state 设计有问题（回 L0 判据工程），不是继续调阈值；
- **红线**：① 硬安全（密钥/危险命令/钱）永不单靠 Jev；② 阈值不在自己数据
  校准过不得用于生产拦截；③ 抽审计 ρ 不得为省钱关闭；④ 任何 swe-eval/
  agent_go 落地先 shadow 后生效，且经各自治理评审（批期禁扰动红线同样适用）。

## 6. 参考

- 本仓：`docs/06-reference-metrics/jev-mcp-tool.md`（使用策略与校准核验）、
  `docs/06-reference-metrics/jev-quota.md`（额度）
- swe-eval：`docs/jev-paradigm-20260926.md`、`docs/jev-cchannel-design-20260926.md`、
  `docs/jev-residual-register-20261012.md`、`docs/joint-coordination-llamacpp.md`
- 业界：Flowable 2026.1 Guardrails；FutureAGI《Ultimate Guide to LLM Guardrails 2026》；
  arXiv:2502.09054（cascade 双阈值与 early abstention）；Openlayer/Maxim/
  DigitalApplied（OWASP LLM Top 10 对齐）；toolgate / jev-engineering / JevLoop（Jev 生态实证）
