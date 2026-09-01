# Skill 渐进式信息披露机制分析与上下文压缩借鉴

> **状态**：调研与借鉴提案（§3.2/§3.4/§3.5末行 已实施；§3.1 部分；§3.3 待 batch 档）｜**日期**：2026-09-01
> **实施对账**（2026-09-01 晚更新）:
> - §3.2 replayable 分档 → ✅ 18019fb（PROXY_TRUNCATE_REPLAYABLE_DROP 默认关; Read 结果绕过 BM25 keep 指针化, 原文寄存 orig/ + manifest 登记; dup 守卫同路径第二次保留原文）
> - §3.4 二跳地址 → ✅ 大半（258d414 占位符指引 + b7a22fa sample anchors + 锚点直查; 摘要行已带 r: 锚点可直达）
> - §3.5 末行"显式告知 N 单元可查" → ✅（258d414 占位符指引 + f10d83c S1 空结果存储概况）
> - §3.3-lite → ✅（S1 空结果高频句柄 = 目录思想轻量版; 完整 list 模式仍待 P3）
> - 另: 分页续读协议（借鉴 skill 按任务粒度披露, 4b5503c）——文档未列, 已实施: 大单元恢复从 4000 字节硬截断升级为 锚点@offset 分页
> - §3.1 触发词工程 → 🟡 部分（head 240 扩容 + 端到端修复; 触发语义模板待做）
> **来源**：对 Claude Code Agent Skills 等 skill 机制的机制级分析，与本仓库 PDC（渐进披露上下文服务）设计的对照。skill 是"渐进披露"的**业界已验证形态**——它解决的问题与本仓库压缩/截断/召回面对的是同一个：如何让有限上下文容纳超出其容量的知识与数据。
> **一句话**：skill 的本质是**用廉价常驻的语义索引（触发词 description）+ 按需页入的内容 + 代码化再生，把"上下文里有什么"变成"上下文里能寻址到什么"**；对本仓库最直接的一条借鉴是压缩优先级重排——第一选择不是"压得更狠"，而是标注可再生性：可再生的丢掉留指针，不可再生的进 manifest 索引。

---

## 1. 机制：四级披露栈（寻址与内容分离）

Skill（以 Claude Code Agent Skills 为原型）把知识分成四层，每层 token 成本与驻留策略完全不同：

```
L1 元数据（常驻）      name + description → 系统提示词，~50-100 tokens/skill
L2 主体（按触发加载）  SKILL.md 正文 → 指令/工作流/决策树，~1-5K tokens
L3 资源（按需引用）    SKILL.md 链接的 reference/forms/examples → Read 工具二跳加载
L4 可执行脚本（零加载） scripts/* → 经 bash 执行，代码本体永不进上下文，只回传结果
```

**L1 — 元数据常驻**：只有 `name`（≤64 字符）与 `description`（≤1024 字符）进系统提示词。关键工程细节：description 不是"功能简介"而是**路由键**——写的是触发条件（"当用户要处理 X / 给出 Y 类 URL 时使用"），本质是给下一轮模型看的检索索引。

**L2 — 主体页入**：模型判断 description 与任务匹配后用 Read 加载正文。显式 page fault；正文此后随对话历史常驻会话。

**L3 — 二跳引用**：SKILL.md 不要求穷尽，可写"做 Y 时读 reference.md 的 Z 节"。披露**链式递进**：元数据→正文→引用文件，每跳有明确地址（路径），模型按需决定走多深。

**L4 — 代码即行动**：最激进一级。正文指示"运行 validate.py 而不是阅读它"；代码在外部执行，只有输出回到上下文。**内容的 token 成本归零，转化为计算成本**。

### 1.1 token 经济学

| 层 | 空闲成本 | 激活成本 | 失真 |
|---|---|---|---|
| L1 元数据 | 常驻（每 skill 数十 token） | — | 无（但路由可能失灵） |
| L2 主体 | 0 | 1-5K tokens | 无 |
| L3 资源 | 0 | 按节引用 | 无 |
| L4 脚本 | 0 | 仅输出回传 | **可再生（严格零失真）** |

## 2. 三条设计原理

1. **驻留成本与激活成本解耦**：空闲付 O(技能数 × 元数据)，激活才付 O(元数据+正文+引用资源)。虚拟内存同构：系统提示词=常驻集，skill 调用=缺页中断，对话历史=已页入页面的页表。
2. **模型即路由器**：披露决策由 LLM 基于廉价元数据自主做出，零检索基础设施（无 embedding/FTS）。前提是 description 触发词工程到位——写糊即路由失灵，这是 skill 系统最主要的失败模式。
3. **可再生优于已持有**：L4 哲学——内容不必"持有"，只要能按需"再生"（重跑/重读）。持有只剩延迟优化价值，正确性由再生能力保证。

---

## 3. 对本仓库上下文压缩的五条借鉴

### 3.1 索引行 = description 工程（改进 manifest）

PDC 的 manifest 索引行（「索引永不丢」）目前锚点偏**物理特征**（type/hash/出处，经 `ifc_metrics.unit_anchors`）。skill 启示：索引行的核心字段应是**触发语义**——该单元能回答什么问题、什么任务会需要它。丢弃历史时 L1 摘要不写成"内容概要"，而写成可判定条目：

```
[dropped: 包含 auth.py 的 3 次报错现场与最终修复方案；含用户"不要改测试文件"的约束]
```

模型下一轮拉回靠**触发词匹配**而非内容相似。落点：`memory_stores.py::record_dropped_messages` 索引行模板 + `ctx_recall.py` 三级穿透检索的命中率——**拉取命中率上限由索引行触发词密度决定，不是检索算法**。配套度量：拉后即弃率（PDC 已定义）兼作索引质量指标。

### 3.2 「可再生」作为压缩最高优先级（改进 truncation）

Skill L4 给压缩的启示——**压缩策略优先序重排**：

| 优先级 | 内容类别 | 动作 | 失真 |
|---|---|---|---|
| 1 | 可再生（文件读取/可重跑命令的输出） | 丢弃，留再生命令指针（`来源：Read src/x.py`） | 严格零（可重取，本地后端重读成本极低） |
| 2 | 可再生性差（不可精确重算的长文本） | 结构化压缩（现有 `_structured_compress` 定位） | 有损有界 |
| 3 | 不可再生（一次性推理产物/用户约束） | 保留 + manifest 索引（PDC 现有路径） | 无丢弃 |

当前 fifo/压缩不区分可再生与不可再生。落地小增量：

- `unit_model.py` Handle 词汇表加 `replayable` 属性（文件读/可重跑命令 = 可回放）；
- truncation 对可回放单元直接降到激进丢弃档（保留再生命令行）。

**与历史教训的冲突必须处理**：2026-08-29 TokenSieve 重读死循环（压太狠致模型反复重读）说明"丢弃+引导重读"会被滥用。防御：再生命令配 dup 计数（`session_ledger` 已有 `last_dup_turn`）——同一目标第二次重读时升级为**保留原文**而非再丢弃；与 `loop_detection` 既有 dup 检测共用状态。

### 3.3 目录暴露给模型：从「代理猜」到「模型分页」（改进 ctx_recall）

Skill 的披露由模型自主决定；当前 PDC 微轮自答是**代理侧**判定要不要拉取。借鉴：manifest 升级为一等可浏览对象——`ctx_recall` 工具加 `list` 模式（返回被丢弃单元索引页，支持过滤/分页），模型自己"翻目录"再决定取哪条。一次 list 数百 token，换来路由决策交给对这个任务最了解的一方。与 context-tier-profiles 分工吻合：batch 档（256K 宽裕）用模型自主深挖，interactive 档维持代理代答省轮次。

### 3.4 二跳链式披露（改进 PDC-L1 摘要）

SKILL.md 引用资源是链式的；PDC-L1 注入摘要目前是"平面"的。借鉴：给摘要行加**二跳地址**（manifest unit_id），模型拉回摘要后可再按 unit_id 取全量（`ctx_recall.recover_full_content` 已支持按 tool_use_id 恢复）。摘要层与全量层之间从此有正式"引用协议"，而非仅一层兜底。

### 3.5 失败模式的镜像教训

| Skill 失败模式 | 压缩侧对应风险 | 既有/应加防御 |
|---|---|---|
| description 写糊 → 路由失灵 | 索引行触发词密度不足 → 拉取 miss | §3.1 索引行工程；拉后即弃率作质量指标 |
| 元数据过期 → 误路由 | 陈旧 manifest 行 → 拉取失败/内容失效 | 会话 TTL/跨批轮转已有；可加"拉取 404 计数"降权 |
| 正文过重 → 触发即爆 | 摘要注入过长挤占工作集 | 注入双封顶（张数+字符，`cognitive-gap-closure-design` SEM 同款） |
| 模型不知道 skill 存在 → 永不触发 | 模型不知道有被丢内容 → 永不召回 | 「索引永不丢」+ 注入时显式告知"还有 N 单元可查" |

---

## 4. 落地形态与优先级

| 借鉴 | 落点 | 规模 | 优先级 |
|---|---|---|---|
| §3.2 replayable 分档 | `unit_model.py` + `truncation.py` + dup 守卫 | 小（词汇表一字段 + 截断分档一档） | **P1**（独立小增量，与 SEM/TAC 不冲突） |
| §3.1 索引行触发词工程 | `memory_stores.py` 索引行模板 | 小（模板字段） | P2（随 SEM-P1 蒸馏一起验收索引质量） |
| §3.4 摘要二跳地址 | PDC-L1 摘要模板 + `ctx_recall` | 小 | P2 |
| §3.3 ctx_recall list 模式 | `ctx_recall.py` TOOL_SCHEMA 扩展 | 中 | P3（batch 档灰度） |

**验收信号**：ctx_recall 拉取命中率上升且拉后即弃率下降（索引质量）；replayable 单元丢弃后 dup 重读率不升（守卫有效）。

**约束沿用**：tail-append 纪律、默认关 reloadable、fail-open、stdlib only、CONFIG_REGISTRY 注册（如 `PROXY_TRUNCATE_REPLAYABLE_DROP=true`、`PROXY_MANIFEST_TRIGGER_WORDS=true`）。

---

## 5. 关联文档

- `progressive-disclosure-context-serving-design-20260829.md`：PDC 四层需求/三级存储/拉取通道（本分析 §3.1/3.3/3.4 的宿主）
- `information-fidelity-control-design-20260829.md`：rate-distortion 分配与可恢复性（§3.2「可再生」的理论位置）
- `cognitive-gap-closure-design-20260901.md`：SEM 语义卡与 TAC 准入（索引质量度量共用；§3.2 的 dup 守卫与 LRC 共用 session_ledger 状态）
- `memory-storage-requirements-selection-20260829.md`：存储选型（manifest 模板扩展的约束来源）
