# 上下文架构演进总览：概念架构与功能架构变更（IFC + PDC 系列）

> **状态**：设计提案（未实施）｜**日期**：2026-08-29
> **定位**：本系列三篇设计（[IFC](information-fidelity-control-design-20260829.md) 防御面 / [PDC](progressive-disclosure-context-serving-design-20260829.md) 建设面 / [存储需求与选型](memory-storage-requirements-selection-20260829.md) 支撑面）的**架构层收口**——以系统架构师视角说明概念架构与功能架构的变化，面向架构评审。
> **变更性质判断**：**演进而非重构**。不改请求路径拓扑、不新建进程/服务/端口、不破坏双协议端点语义与 append-only 不变式；零新增运行时依赖（sqlite3 stdlib 本机已验证）。

---

## 1. 概念架构变化

### 1.1 as-is：单向减法编辑器 + 开环信息环

现行概念模型：代理是客户端与后端之间的**纯变换管线**——每请求一次性猜测相关性（BM25/类型感知），压出固定视图 `V_t` 推送给后端；被丢弃的信息只能靠环境重观察恢复。控制论盘点结论：体量环（OOM/看门狗/熔断/预算）与行为表面环（循环干预/blocker/reread）已自动闭环，**信息环（压缩质量/截断质量/信念漂移）唯一开环**，靠人读诊断再 reload 闭合。

### 1.2 五个观念转变（as-is → to-be）

| # | 维度 | as-is | to-be |
|---|---|---|---|
| 1 | **代理角色** | 上下文编辑器（减法、一次性） | **上下文服务器**（减法编辑 + 交互式信息服务） |
| 2 | **信道形态** | 推式单向（代理猜测相关性） | 推式底座 + **拉式交互**（模型作为在环消费者裁决相关性） |
| 3 | **控制回路** | 信息环开环、人闭合 | **三级闭环**：被动度量 → 离线效度验证 → 事件门控；失效安全默认（信号缺失 = 精确回退现状） |
| 4 | **记忆地位** | 事后诊断档案（只写） | **运行时记忆系统**（被门控/拉取/预取/对账读写驱动；可寻址、可对账、可重建） |
| 5 | **损失观** | 损失尽量避免（不可度量） | **rate-distortion 分配 + 可恢复性**：损失预算显式化；manifest 保真 > 内容保真（索引换正文的选择权交给模型） |

### 1.3 目标概念架构：三面一体 + 分层控制

```
                    ┌────────────── L4 治理（人）：config / reload / 文档 ──────────────┐
                    │  ┌─────────── L3 监督：epoch 门 · 游走检测 · 策略级预案 ───────┐  │
   防御面 IFC  ←──→ │  │  ┌──────── L2 调节：压缩回退 · 信息钉 · 截断 · 路由 ────┐ │  │
   （不降级）        │  │  │  传感器(体量)：tokens/hit/drop        [既有·闭环]    │ │  │
                    │  │  │  传感器(信息)：Tier-0 / 探针 / 台账对账  [IFC·补环]   │ │  │
   建设面 PDC  ←──→ │  │  └───────────────────────────────────────────────────────┘ │  │
   （按需给）        │  └─────────────────────────────────────────────────────────────┘  │
                    └────────────────────────────────────────────────────────────────────┘
   支撑面 存储：Hot(V_t) / Warm(台账·manifest·pins·探针·拉取日志) / Cold(档案+FTS5 索引)
                    ▲ ctx_recall 拉取回路（模型 → manifest → 正文 → 追加回历史）
                    ▲ 预取回路（探针 Q3 需求信号 / 游走触发 → 主动披露）
                    ▲ 校准回路（拉取日志 ⋈ 损失日志 → 钉/权重频率晋升）
        L0 被控对象：backend 生成 × 任务环境
```

三面的分工：**IFC 守下界**（信息不降级：门控损失事件、钉驻留、双端失败检测），**PDC 扩上界**（信息可恢复可聚焦：manifest、拉取、预取），**存储提供寻址/对账/重建的地基**。三条新回路（拉取/预取/校准）全部以**追加**方式作用于历史，与 append-only 正交。

### 1.4 不变式（架构宪法级约束，本次演进中被显式保留）

1. **append-only canonical 不可侵犯**：canonical 历史永不回溯改写；一切披露、对账、探针均为追加或侧路。
2. **客户端零改动**：Claude Code 永远只连 `127.0.0.1:4000`；双协议端点语义不变。
3. **核心 stdlib only**：`anthropic_proxy.py`/`pipeline.py` 等零第三方依赖；sqlite3（本机已验证 FTS5/WAL）不破坏此约束。
4. **失效安全**：任何新控制信号缺失/过期/低置信时，行为精确回退到现状（门空闲 = hit_ratio 零回归为护栏）。
5. **确定性优先**：记忆构造确定性（"无 LLM"抽取）；LLM 只出现在显式标注的侧路（探针）与模型自决的拉取中。

---

## 2. 功能架构变化

### 2.1 模块变更地图

| 模块 | 变更 | 职责变化 |
|---|---|---|
| `belief_probe.py` | **新增** | 信念传感器：锚定问题、logprobs/自一致性熵、台账对账（D_ledger）；侧路分发，云端 only |
| `ctx_recall.py` | **新增** | 拉取服务：FTS5 检索 + manifest 解析 + 优雅缺页（not-present 语义） |
| `pipeline.py` | 扩展 | 两个新挂载点（见 2.2）；ILE 事件触发探针；微轮重派（PDC 方案 B） |
| `context_engine.py` | 扩展 | `maybe_epoch` 门控输入（halve/defer 分支）；pins 折叠存续 |
| `content_compressor.py` / `truncation.py` | 扩展 | 索引行生成（D0，非 epoch 路径补齐）；pin 豁免；回退触发信号接入 |
| `session_ledger.py` | 扩展 | unit ID 分配器；pins/探针/拉取日志并入增量落盘 |
| `diagnostics.py` | 扩展 | per-turn `ifc` 段（tier0 / ile / loss_by_stage / probe） |
| `tool_filter.py` | 扩展 | `ctx_recall` 工具定义注入（双协议） |
| `admin_server.py` | 扩展 | `GET /api/session/<key>/ifc`（健康/探针/拉取统计） |
| `proxy_config.py` | 扩展 | `PROXY_IFC_*` / `PROXY_PD_*` 注册（全部 reloadable，默认关，见 2.4） |
| 消息转换 / 路由主体 / 队列 / watchdog / 后端策略 | **不变** | — |

### 2.2 管线 stage 变化（不重排编号）

24 个 stage 编号保持不变，新增挂载按仓库小数惯例插入：

- **请求入口（stage 0 槽位）**：PDC 方案 A 的召回结果改写（客户端 error tool_result → 真实检索结果），与压缩改写同槽位同语义（提交点之前）。
- **17.5（17 与 21 之间，事件触发、默认关）**：探针执行与召回服务（方案 B 微轮重派）。管线中唯一的既有非推理 HTTP 先例为 kimi 配额查询；辅助推理分发是**新阶段类型**，受并发护栏约束（不占本地槽）。
- **7 / 14 / 17 各 stage 内部**：零行为变化的观测增强（损失记账、索引行生成）。

### 2.3 新增数据流（四条）

| 数据流 | 路径 | 触发 |
|---|---|---|
| 探针侧流 | ILE → belief_probe（云端侧路）→ 探针存储 → 门控输入 / 预取调度 | 信息损失事件，≤8 次/会话 |
| 召回流 | `ctx_recall` tool_use →（A 次请求改写 / B 微轮）→ FTS5 索引 → 档案正文 → tool_result 追加 | 模型自决 |
| 校准流 | 拉取日志 ⋈ 损失日志 → 拉后即弃率 → 钉来源/BM25 权重频率晋升 | 离线（D2） |
| 对账流 | 探针答案 ⋈ 台账 materials → D_ledger | 随探针 |

### 2.4 配置与接口面

- 新增 HTTP：`GET /api/session/<key>/ifc`；既有 `X-Proxy-Diag-*` / SSE 尾注机制不动（ifc 字段并入）。
- 新增注入工具：`ctx_recall`（~100 token 常量定义，双协议）。
- 新增配置组（全部 reloadable）：`PROXY_IFC_ENABLED`（true，仅度量）/ `PROBE_`、`GATE_`（+SHADOW）、`PINS_`、`WANDER_`（均 false）；`PROXY_PD_ENABLED`（true，仅索引）/ `RECALL_`、`PREFETCH_`（false）。
- 运行时依赖：**零新增**；新文件面仅 `logs/diag/index/<sid>.db`。

### 2.5 部署与运行视图

无新进程、新服务、新端口；并发纪律不变（`MAX_CONCURRENT=1` 本地不受探针/召回影响）；SIGHUP reload 语义不变，所有新开关热生效；存储布局按选型文档 §4。

---

## 3. 数据架构：核心数据概念与指标体系

> 从算法与数据流转视角提炼：领域实体是算法的公共词汇，指标按**派生深度**分层组织——分层规则本身就是数据架构的核心决策。

### 3.1 核心数据实体（领域模型）

| 实体 | 键 / 身份 | 关键属性 | 产生者 | 主要消费者 |
|---|---|---|---|---|
| **Unit（信息单元）** | `unit_id`（单调、absorb 赋值、绝不复用） | type（user/assistant/tool_use/tool_result）、turn、handles、size_chars、content_hash、state（in_view/deferred/dropped/evicted/**pinned**） | absorb（session_ledger 扩展） | manifest、损失日志、拉取、钉、档案 |
| **View（视图）** | `request_id` → 有序 unit_id 集 + 形态 | 形态：verbatim / compressed / folded / index-only | 视图构造（stage 7/14/17） | sent_view 档案、保真指标 |
| **Manifest（披露清单）** | `unit_id`（与单元 1:1） | 索引行（handle/turn/type/size），~100B/行 | D0 全路径生成 | ctx_recall、manifest_coverage |
| **ILE（信息损失事件）** | `request_id` + kinds | kinds：compress_drop / fifo_drop / epoch_collapse；affected unit_ids；loss_by_stage | stage 7/14/17 记账 | 探针触发、门控、拉后即弃 join |
| **Probe（探针记录）** | `request_id`（侧路，不进 canonical） | method、answers、d_ledger、h_consistency、age_turns | belief_probe | 门控新鲜度、预取调度 |
| **Pin（信息钉）** | `unit_id`（驻留关系） | source、size、last_referenced_turn、budget_share | 钉晋升/降级（含校准流） | 截断/压缩豁免、折叠存续 |
| **Pull（拉取记录）** | `request_id` + query | resolved unit_ids、tier_hit（warm/cold）、latency、previously_dropped | ctx_recall | 校准流、供给指标 |
| **LedgerFact（台账事实）** | 既有 R14 actions/materials | —— | 台账（既有） | D_ledger 对账的 ground truth 侧 |

实体关系骨架（数据血缘）：

```
Session ─< Request ─< View ──投影──> Unit ──1:1──> Manifest 行
                        │                │
                        │                ├──> Archive 正文（Cold 层）
                        │                └──> Pin（驻留子集，含生命周期）
                        ├──> ILE ──涉及──> Unit（损失关系）
                        ├──> Probe ──⋈──> LedgerFact（对账 → d_ledger）
                        └──> Pull ──⋈──> ILE/Unit（拉后即弃 → 校准）
```

### 3.2 数据契约：三级键树与五条不变量

- **三级键树**：`session_key`（会话）→ `request_id`（请求）→ `unit_id`（单元）。五流日志 + 三个新存储全部挂在这棵键树上，**任何跨流分析 = 键树 join**——这是拉后即弃率（Pull ⋈ ILE）与对账（Probe ⋈ LedgerFact）在结构上可能的前提。
- 五条数据不变量：① **manifest 完整性**（每个 deferred/dropped/evicted 单元恰有一条索引行）；② **unit 不可变**（absorb 后内容不改写，更正 = 新单元追加）；③ **优雅缺页**（内容已驱逐 → 结构化 not-present 响应，非 404 混淆）；④ **派生可重建**（Warm 全部存储可从 canonical/档案幂等重建，无孤本真相）；⑤ **预算存储层强制**（钉 ≤5%、探针 ≤8 次/会话、拉取 ≤16 次/会话由存储层计数，不靠生成端自律）。

### 3.3 核心指标体系：三级派生层次

指标按派生深度分层：**L1 决定成本与新鲜度，L2 是纯函数可单测，L3 是唯一允许触达执行器的层**。

| 层 | 定义 | 指标（口径） | 决策出口 |
|---|---|---|---|
| **L1 原始观测** | 传感器直接产出，每请求，零派生成本 | `loss_by_stage`（逐 stage 丢弃字符）、视图 unit 集、工具调用序列、探针原始答案、拉取事件、台账事实 | 仅落盘（sessions.jsonl `ifc` 段 / 台账） |
| **L2 派生指标** | L1 的纯函数，逐请求，可单测 | `retention = 1 − dropped_t/\|V_{t−1}\|`；`rationale_ratio`（动机存活率，注入摘要前测）；`action_div`（归一化 bigram 熵，双端检测）；`reread_pressure`；`d_ledger = 1 − recall(探针路径, 台账句柄)`；`h_consistency`（K=3 聚类熵）；`tier_hit`（warm/cold 命中） | diagnostics 字段；游走检测；门控新鲜度输入 |
| **L3 趋势与关联** | 跨轮聚合 / 跨流 join（离线或会话级） | h 分量**斜率**（趋势优先于绝对值）；`pull_after_drop`（拉取 ⋈ 损失，分类别）；`manifest_coverage`；存活轮数、saturation 无衰竭率；`hit_ratio` 零回归差 | **epoch 门（halve/defer）、压缩回退、钉晋升/降级、Phase 2 效度验证** |

设计规则：L1/L2 永远只写不读执行器（这是度量默认开的安全性前提）；L3 进执行器必须过效度关卡（IFC Phase 2）+ shadow 先行 + 趋势化触发。

### 3.4 数据流转总图：两条闭环共享一套键空间

```
            ┌──────────────────── 传感闭环 ────────────────────┐
client ──absorb──> Unit ──视图构造──> View/Manifest ──ILE──> Probe(侧路)
  ▲                 │                        │                  │ ⋈ LedgerFact
  │                 │                    loss journal           ▼
  │             Archive(Cold)               │          d_ledger / h_cons ──(斜率)──> L3 门控
  │                 ▲                       └────────┬─────────────────────────────┘
  │                 │                                │ 校准闭环: Pull ⋈ Loss
  │  追加(拉取即历史) │                                │   → 拉后即弃 → 钉/权重晋升
  └── tool_result ──┘          ctx_recall: FTS5 索引 ──> Archive 正文
```

§2.3 的四条数据流在此汇成两个闭环：**传感闭环**（ILE → 探针 → 对账 → L3 → 门控执行器）与**校准闭环**（Pull ⋈ Loss → 拉后即弃 → 钉晋升）。两条闭环共享 Unit 键空间——三级键树因此不是实现细节，而是两条闭环得以存在的结构前提。

---

## 4. 变更风险评估（架构师视角）

| 风险级 | 变更 | 性质 | 缓解（均为设计中既有护栏） |
|---|---|---|---|
| 低 | Tier-0 度量、索引行、loss journal | 纯观测，零行为变化 | reload 关闭即无痕 |
| 低 | 存储新增件（A3 模式） | 隔离文件面，容量 KB 级 | MB 上限 + 最老驱逐 |
| 中 | `ctx_recall` 注入与结果改写 | 回归面 = 工具列表与 tool_result 改写 | 复用 `compress_tool_result` 测试模式；双协议用例；体积/配额上限 |
| 中 | 探针侧路 | 云端成本/延迟/隐私 | 事件触发 + 配额；敏感会话（`ROUTE_SENSITIVE_PATTERNS` 命中）降级 Tier-0 或禁用 |
| 高 | epoch 门 / 压缩回退（控制类） | 可能引入新振荡模态 | 默认关 + shadow 先行 + 趋势化触发 + Phase 2 效度硬关卡（不达标止步于度量） |

与既有子系统的冲突检查：**队列**（探针不计入 bucket、不占本地槽）；**路由**（探针仅 logprobs 可用云端路径）；**prefix cache**（拉取即历史，追加保前缀稳定；门空闲时 hit_ratio 零回归护栏）；**OOM**（钉预算 ≤5% 计入既有预算口径）。

---

## 5. 实施路线（汇总三篇的轨道）与文档同步

```
并行起步 ── IFC Phase 0（Tier-0 度量）＋ PDC D0（manifest 完整性）     [纯观测，1-2 天]
传感器   ── IFC Phase 1（探针）＋ PDC D1（ctx_recall 方案 A）           [默认关，3-5 天]
数据期   ── IFC Phase 2（效度验证·硬关卡）∥ PDC D2（拉取日志校准）      [≥1 周]
控制     ── IFC Phase 3（门控 shadow→真实）＋ PDC D3（方案 B/预取合流）
品种     ── IFC Phase 4（钉/游走检测）→（效度通过后）策略级 Phase 5 预案
```

文档同步时点：**Phase 0 落地时**同步 `CLAUDE.md` / `AGENTS.md`（架构约定章）与 `proxy-pipeline-reference.md`（stage 挂载）；本系列四篇在此之前保持"提案"状态。顺带清理项：AGENTS.md §11.1 幽灵参数（`PROXY_COMPRESS_LLM_*`）。

---

## 6. 架构身份总结

演进前后的身份变化一句话：**从"上下文预算管理器"到"上下文信息系统"**——token 预算仍是硬约束（OOM/预算环一个不撤），但信息保真（IFC）与可寻址性（PDC）成为一等公民：代理不再只回答"这轮发多少 token"，而是同时回答"模型相信什么、缺什么、上哪取"。三篇设计共享同一验证纪律（观测先行、失效安全、事件触发、默认关），使整场演进在任一相位都可无损暂停。

---

*相关文档：[information-fidelity-control-design-20260829.md](information-fidelity-control-design-20260829.md)（IFC·防御面）、[progressive-disclosure-context-serving-design-20260829.md](progressive-disclosure-context-serving-design-20260829.md)（PDC·建设面）、[memory-storage-requirements-selection-20260829.md](memory-storage-requirements-selection-20260829.md)（存储·支撑面）、`proxy-pipeline-reference.md`（现行管线权威，实施时更新）。*
