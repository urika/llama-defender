# llama-defender 上下文工程改造设计（append-only + epoch 压缩 + 动作台账）

> 状态：设计（v0.3，review 合理性复核后修正台账布局）
> 日期：2026-08-19
> 修订记录：v0.1 初稿；v0.2 并入外部 review（Kimi）反馈；**v0.3 取消独立台账块**——复核发现 v0.2「台账置 L2 之后」仍违反布局不变式（L2 每轮增长位于台账之前 → 台账整体每轮被推移 ~15K 重算）。根本约束：每轮 append 增长区域必须唯一。台账改为三载体：原生流（epoch 间）+ 压缩区台账格式（epoch 时）+ 代理侧 canonical（复述块呈现聚合）
> 输入依据：公开集本地臂批跑形态学观察（搜索兔子洞 / 代理每轮语义改写 / ~3min/轮）+ 业界调研（Manus / Anthropic / llama.cpp 官方文档与 tutorial）
> 关联：[llama-defender-integration-requirements.md](llama-defender-integration-requirements.md)（接口需求基线）、[harness-driving-architecture.md](harness-driving-architecture.md)（智能化能力盘点）
> 目标项目：`/Users/jinsongwang/APP/llama.cpp`（llama-defender 代理侧改造，不涉及 llama.cpp 内核修改）
> 背景讨论：本设计同时回应两个问题——① 每轮语义改写导致响应时间随轮数恶化；② 改写销毁「重复行为」证据，削弱模型元认知（搜索兔子洞的放大器之一）。

---

## 0. 问题定义

### 0.1 现状行为

llama-defender 对每个会话轮次执行**语义压缩**：收到客户端全量历史 → LLM 改写为摘要版 → 发给 llama-server。每轮都改写。

### 0.2 三个症状

| 症状 | 机制 | 实证 |
|------|------|------|
| 延迟恶化 | 改写历史 → KV 前缀缓存全量失效 → 每轮全量 re-prefill | 批跑 ~3min/轮（66 轮 / ~100K 上下文） |
| 成本恶化 | 会话总 prefill 成本 O(n³)（见 §1 成本模型） | 41 任务批跑预期 7-12 天 |
| 元认知证据销毁 | 「我已搜过 8 次同一查询」的重复模式被摘要抹掉，模型无法自检 | 748f534 搜索兔子洞 45+ 分钟无自愈 |

### 0.3 核心判断

「不压缩 → 长度膨胀 → 平方上涨」是**伪两难**。真正的元凶是「每轮改写」导致的缓存击穿，而非「不压缩」。正确解法：

```text
平时 append-only（缓存命中，增量 prefill）
压缩从「每轮」改为「按 epoch」（到阈值才压一次）
压缩只作用于 observation 正文，永不作用于 action 轨迹
```

---

## 1. 成本模型（为什么是 O(n³) → 线性）

Agent 会话 prefill 主导（Manus 实测 input:output ≈ 100:1）。设会话 n 轮、epoch 上限 S tokens：

| 策略 | 每轮 prefill | n 轮总成本 | 说明 |
|------|-------------|-----------|------|
| A. 每轮语义改写（现状） | 全量 r | **O(n³)**（Σr² ≈ n³/3） | 改 1 个 token，缓存从该点起全失效 |
| B. 纯 append-only | 增量 δ | O(n²)（Σδ·r ≈ n²/2） | 缓存全命中，但长度无上界 |
| **C. append-only + epoch 压缩（本设计）** | 增量 δ + 尾部复述块 | **O(n·S) ≈ 线性** | 每 epoch 一次 O(S) 重算，可摊销 |

**验证手段**：llama-server 日志 `slot update_slots ... prompt processing progress, n_past = X, n_tokens = Y`。`n_tokens/n_past` 即增量占比——现状应为 ≈1.0（全量），改造后应 <0.1。**Phase 0 先测这个数，坐实诊断再动工。**

---

## 2. 调研结论（业界基线）

| 来源 | 结论 | 对本设计的映射 |
|------|------|---------------|
| Manus（生产 agent 一手经验） | KV-cache 命中率是生产 agent 第一指标（缓存价差 10x）；上下文 **append-only**，序列化确定性；压缩必须**可恢复**（丢内容留句柄：URL/路径）；**保留失败记录**（擦除失败 = 模型无法更新信念）；上下文同质化会 few-shot 锁定行为 | §3 五层架构；§4.3 写入期压缩；§4.4 句柄规则；§4.2 action 轨迹不压缩 |
| Anthropic（官方） | Context editing：自动清除旧 tool results（保留最近 3 条），~30K 阈值；Compaction：阈值触发、保护近期上下文；**清 observation，不清 action 轨迹** | §4.6 epoch 触发条件与 L2 保护窗口 |
| llama.cpp / llama-server | `cache_prompt: true`（默认开）按最长公共前缀复用；`id_slot` 显式固定 slot（避免 `-sps` 相似度抖动换 slot）；`--slot-save-path` 可持久化 slot KV；KV 量化（`--cache-type-k/v q8_0`）省约一半 KV 内存 | §4.8 服务端配合 |

参考：
- [Context Engineering for AI Agents: Lessons from Building Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- [Tutorial: KV cache reuse with llama-server（llama.cpp #13606）](https://github.com/ggml-org/llama.cpp/discussions/13606)
- [Anthropic: Memory tool / Context editing](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- [Claude Platform Docs: Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction)

---

## 3. 总体架构：五层上下文

物理顺序（由布局不变式决定，见 §4.7）：
```text
┌─ L0 固定前缀     system prompt + 工具定义              永不改动（改它 = 全量失效）
├─ L3 压缩区       老轮次的台账格式（动作行+摘要+句柄）    epoch 间静态
├─ L2 原生流       本 epoch 内全部轮次（原生消息格式）      唯一的每轮 append 增长区
├─ 尾部复述块      聚合状态（去重计数/材料清单/轮次预算）    每轮重算（有界，≤600 tokens）
└─ L4 档案库       完整原始转录落盘                        不在 prompt 内（harness/人可查）
```

**分层编号（L0-L4）是概念职责，物理顺序由缓存不变式决定**：每轮 append 增长区域必须**唯一**（原生流），有界重写区（复述块）只能位于其后；中段任何插入/改写只能作为 epoch 事件。> 布局勘误史：v0.1 台账在前缀区（L0 后）——中段增长每轮推移其后全部内容；v0.2 台账在 L2 之后——L2 每轮增长位于台账之前，台账仍每轮被整体推移（~15K 重算）；v0.3 取消独立台账块，见 §4.2。

三层职责分离是本设计的核心不变式：

> **「台账永不压缩、正文滚动保留、档案落盘可召回、压缩按 epoch 不按轮。」**

| 层 | token 预算（默认） | 压缩策略 | 缓存影响 |
|----|------------------|---------|---------|
| L0 | 客户端决定（~5-10K） | 永不 | 稳定前缀，缓存根基 |
| L1 台账（概念层，三载体见 §4.2） | 压缩区行 ~30-50/调用；原生流内即原生消息 | 原生流部分永不压缩；压缩区部分超长时保计数聚合 | 随所属物理区（L2/L3） |
| L2 原生流 | epoch 间无上限增长；重切时保留 K=8 轮（~8-16K） | epoch 内逐字 | 唯一每轮 append 区，缓存增量命中 |
| L3 压缩区 | 每轮 ≤80 tokens 摘要 + 句柄 | epoch 时一次性收编 | epoch 间静态；每 epoch 一次缓存重建 |
| 尾部复述块 | ≤600 | 每轮确定性重算（无 LLM 调用） | 每轮重发（有界重算成本） |
| L4 | 无限（磁盘） | 不压缩 | 无（不在 prompt 内） |

---

## 4. 详细设计

### 4.1 L0 固定前缀纪律

代理对 L0 区域的**唯一责任是不碰它**：

- 不注入时间戳、随机 id、会话计数等任何变化内容（Manus：秒级时间戳即可杀死命中率）
- 工具定义列表全程不变（中途增删工具 = 其后全部缓存失效，且模型易 schema 混乱）
- 若需要注入代理侧元信息（如版本标记），一律放**尾部复述块**

### 4.2 L1 动作台账（元认知证据层）

**目的**：让「重复行为」在上下文中显式可见——这是搜索兔子洞的对症结构，同时是轮级看门狗的数据源。

**v0.3 重定义：台账不是一个 prompt 内独立增长的块，而是三种载体**：

| 载体 | 位置 | 职责 |
|------|------|------|
| ① 原生轮次流 | prompt 中部（L2） | epoch 之间「台账」就是原生流本身——append-only，动作轨迹逐字在场（Manus "keep the wrong stuff in"） |
| ② 压缩区台账格式 | prompt 前部（L3） | epoch 压缩时，老轮次收编为台账行（动作+摘要+句柄+计数）——只在 epoch 重写 |
| ③ 代理侧 canonical 台账 | 不进 prompt（代理内存/落盘） | dup/last_dup_turn/材料清单的计算基础 + 看门狗数据源；聚合结果经复述块呈现给模型 |

> v0.3 勘误（连续第二次位置修正，本次根治）：独立台账块这个构造**本身不成立**。v0.1 置前缀区——中段增长每轮推移其后内容；v0.2 置 L2 之后——L2 每轮增长位于台账之前，台账（~15K）仍每轮被整体推移重算，每轮成本 ≈ 18-20K 而非宣称的 2-4K。根本约束（§4.7 布局不变式）：**每轮 append 增长区域必须唯一**——两个增长块无论谁先谁后，后者都被前者推移。取消独立块后，该约束由原生流单独满足。

台账行格式（用于压缩区 ② 与代理侧 ③，确定性序列化、字段定序）：

```text
[#42] search("github ansible pull 80376") → serper | 654 chars | top: "fix incorrect dnf..." | dup=4 | last=#48
[#43] fetch("https://raw.githubusercontent.com/.../pkg_mgr.py@devel") → 4310 chars | saved: work/upstream_pkg_mgr.py
[#44] read("pkg_mgr.py") → 1204 lines | local bug version
```

字段：`轮次 | 工具 | 目标（截断） | 结果规模 | 一句话摘要 | 重复计数 dup | 末次重复轮次 last_dup_turn`

**规则**：
1. 原生流（载体①）append-only，epoch 内任何压缩不得触碰（擦除失败证据 = 模型无法更新信念）
2. 重复计数 `dup` 与 `last_dup_turn` 由代理在 canonical（载体③）上确定性计算（查询规范化后 hash 对比），不是 LLM 生成——`last_dup_turn` 让模型区分「陈年重复」与「刚刚重复」；呈现路径：压缩区行内 + 复述块聚合
3. 超长会话（>1000 调用）压缩区自身膨胀时的**降级聚合**：同目标条目合并、保留计数与首次/末次轮次——仍然不丢「重复可见性」
4. 看门狗消费载体③（HTTP 端点），与 prompt 内容无关——鸭子洞检测不依赖模型看得见

### 4.3 写入期压缩（write-time compression）——替代「每轮改写」的关键机制

**原则：observation 在写入那一刻就定型为它的保留形态，之后永不改写。**

工具结果返回时，代理按类型应用保留模板（确定性规则，无 LLM 调用）：

| 工具类型 | 保留模板 | 默认预算 |
|---------|---------|---------|
| 搜索结果（serper/WebSearch/searxng） | top-3 organic 摘要 + 查询串句柄 | ≤1.5K tokens |
| 文件读取 | head + tail + 完整路径句柄 | ≤2K tokens |
| curl/HTTP body | 关键段 + URL 句柄 | ≤2K tokens |
| 命令输出 | stdout/stderr head+tail + 退出码 | ≤1.5K tokens |
| 错误/异常 | **全文保留**（负反馈是稀缺信号，不截断） | ≤1K tokens |

**最小阈值规则**：原始结果 ≤ 该类预算时**逐字保留、不做模板包装**——小结果包装后反而比原文长（句柄 + 模板开销），反复累积成为膨胀源。

**句柄（handle）定义**（Manus 可恢复原则）：能够重新取得该内容的充分信息——URL / 文件路径+offset / 规范化查询串。内容可丢，句柄必留。

> **2026-08-21 修订（35B 实施追认）**：四类常规预算按 ~4 chars/token 折算与上表一致（实现值 6K/8K/8K/6K chars）。错误/异常实现为「全文保留至工具类预算×2 封顶（≈3K tokens）」——比上表 1K tokens 更宽：负反馈是稀缺信号，35B 上下文加宽（S=60K tokens）后有意放宽；超封顶截断并附 `[truncated-error]` 标记。

### 4.4 L2 近期正文（保护窗口）

- 本 epoch 内的全部轮次（原生消息格式，observation 已过写入期压缩）逐字保留——这是**唯一的每轮 append 增长区**（§4.7 布局不变式）
- 原生流内不做任何二次处理（Anthropic compaction "保护近期" 同款语义）
- K 的语义（v0.3 澄清）：K 不限制 epoch 间原生流的增长，只在 **epoch 重切**时生效——原生流保留最近 K 轮，更老轮次收编入压缩区。K 的取值依据：模型「一轮记忆窗口」的实证（轮 58 自省、轮 59 复发 → 近期窗口是元认知的主要作用区）

### 4.5 L3 压缩区（epoch 压缩目标）

epoch 重切时，原生流中 K 窗口外的轮次收编入压缩区，表示为台账格式（§4.2 载体②）：

```text
[#12-#15] search("ansible 80376") ×4 无新信息（dup）
[#16] fetch devel pkg_mgr.py 成功，存 work/upstream_pkg_mgr.py（句柄：URL）
[#17] read 上游测试文件 成功（句柄：路径）
```

- **动作行是压缩区的主体**（台账行含动作+结果摘要+句柄+计数，单一格式无双写）；assistant 的推理文本不保留（老轮次的价值在动作轨迹与可恢复性）
- 摘要生成默认用**确定性抽取模板**（首行 + 状态 + 句柄），不调 LLM——零成本、零延迟、可复现；LLM 摘要作为可选开关（质量档）

### 4.6 尾部复述块（recitation，Manus todo.md 模式）

每轮在**最新消息之后、生成点之前**（即 prompt 最末），确定性重发一个有界状态块：

```text
--- proxy state ---
turn: 66/150 | ctx: 74K/128K | epoch: 3
search dedup: "github ansible pull 80376" ×8 (no new info since #40)
materials in hand:
  - work/upstream_pkg_mgr.py  (upstream FIXED version, fetched #43)
  - pkg_mgr.py                (local BUG version)
  - tests/test_pkg_mgr_upstream.py (reference tests, #45)
next-step hint: materials sufficient for diff-based fix
---
```

内容三部分，全部由代理从 L1 台账**确定性推导**（无 LLM 调用）：
1. **去重计数**：重复查询及次数（兔子洞显性化）
2. **材料清单**：已获取的关键工件（即「材料清单检查点」，对「把拿 PR diff 误设为必需条件」的直接解）
3. **轮次/上下文预算**：进度感知

预算 ≤600 tokens。这是本设计中**唯一每轮变化的内容**，其重算成本有界且不击穿前缀缓存（位于尾部）。

**合成负反馈（可选开关，默认关）**：当同一规范化查询 `dup ≥ 3` 时，在下一条 observation 头部注入：

```text
[proxy] 注意：查询 "github ansible pull 80376" 已执行 8 次，结果无变化。请勿再次搜索同一查询。
```

属 append-only 流的一部分，缓存安全。**开启时必须在转录中标注**（bench verdict 口径：干预臂 vs 观察臂分开统计）。

**注入位置作为 Phase 2 A/B 变量**：A = observation 头部（模型视作环境反馈，心理模型更「硬」）；B = 尾部复述块内（零额外成本、近因注意力更强，但模型可能视作「代理提示」而非环境反馈）。两臂各测兔子洞率与任务通过率。

### 4.7 序列化与缓存纪律

1. **布局不变式（缓存根基，v0.3 强化）**：每轮 append 增长区域必须**唯一**（原生流）；有界重写区（复述块 ≤600）只能位于其后。推论：任何「独立的每轮增长块」都不能存在于原生流之前——两个增长区无论谁先谁后，后者都被前者整体推移（v0.1 前缀台账、v0.2 尾部台账两次错误同源）。中段插入/改写（压缩区重建、K 窗口重切）只允许作为 epoch 事件发生
2. **JSON key 定序**（`json.dumps(..., sort_keys=True)` 或固定 schema 顺序）——库不保证 key 顺序稳定时会静默击穿缓存
3. 代理维护**规范历史（canonical history）**：**客户端历史是 ground truth，canonical 是代理的派生视图**。客户端（claude CLI）每轮发全量历史（无状态 API），代理与之做公共前缀 diff → 识别新增轮次 → 写入期压缩 → append 到 canonical → 按物理顺序发送 `L0 + L3压缩区 + L2原生流 + 尾部复述` 给 llama-server；代理注入块（复述块/负反馈）只在发送时附加、不进 canonical（或带标记，diff 时忽略）
4. 客户端历史与 canonical 前缀不匹配时（客户端侧自身做了裁剪/微压缩）：降级为全量重建该会话视图（正确性优先），并记指标 `canonical_mismatch` 监控发生频率
5. epoch 压缩点尽量选在**轮边界/工具序列间隙**（避免拆散同一轮的 action/observation 对；推理链保护见 §4.9）

### 4.8 llama-server 服务端配合

| 项 | 配置 | 说明 |
|----|------|------|
| 前缀缓存 | 请求带 `cache_prompt: true`（默认开，显式声明） | slot 保留 KV，按最长公共前缀复用 |
| slot 固定 | 请求带 `id_slot`，按会话 key 分配 | 避免 `-sps` 相似度抖动导致换 slot 全量重算；批跑场景 `-np` 按**并发会话数**配置，每会话独占一 slot |
| KV 量化 | `--cache-type-k q8_0 --cache-type-v q8_0` | 省约一半 KV 内存 → 同显存可支撑更大 epoch 上限 S |
| slot 持久化（可选） | `--slots --slot-save-path` | 长会话跨 backend 重启恢复（Phase 3） |

**⚠️ 前置验证（Phase 0 一并做）**：确认所用模型（Qwen3.8-27B）非 SWA 混合架构，或 SWA 下前缀缓存语义实测正常——有社区报告 SWA 模型缓存行为异常（误失效/错误复用）。精确验证流程：
1. 第 1 次请求：前缀 P + 尾部 T1 → 应见 `n_tokens = |P|+|T1|`（全量）
2. 第 2 次请求：前缀 P + 尾部 T2 → 缓存正常应见 `n_tokens ≈ |T2|`（仅增量）
3. 异常信号：`n_tokens` 包含部分/全部 P 的重算（SWA 窗口外前缀不可复用）
重复 10 次排除偶发。

### 4.9 epoch 压缩状态机

```text
运行态（append-only）：
  每轮：diff 客户端历史 → 写入期压缩新 observation → append → 重算尾部复述块 → 发送

触发检查（每轮发送前）：
  total_tokens > epoch_trigger（默认 min(65% × ctx_max, ctx_max - 8K)）？

压缩态（每 epoch 一次）：
  原生流中 K 窗口外的轮次 → 收编入压缩区（台账格式：动作行 + 摘要 + 句柄）
  保留：L0 + 压缩区 + 原生流重切为最近 K 轮 + 尾部复述块
  缓存重建范围（精确）：公共前缀止于 L0 → 需重算 = 新压缩区 + 原生流 + 复述块
  （v0.2 勘误：v0.1 只说「缓存全量重建」，实际范围是 L0 之后的全部；量级见 §6.3）
  记指标：epoch_count、压缩前后 token 数

回退保护：
  压缩后若 total_tokens 仍 > trigger（极端：台账/窗口自身超限）
  → 收紧：K=4，L3 摘要再减半
  → 硬上限：仍超限则停止截断，向客户端返回 context 超限错误——
    代理无权把历史压缩到失真来「假装放得下」；超长会话该结束的是任务，
    暴露给 harness/人决策（proxy 无 UI，错误返回是它的「拒绝」通道）

推理链保护（epoch 触发时机启发式）：
  最近 2 轮呈连续 thought → tool_use → thought（无稳定 observation 间隙）时，
  延迟 epoch 到下一个完整轮边界，避免切断连贯推理链
```

### 4.10 可行性边界：什么能/不能在代理层完成

代理的位置：`claude CLI（客户端）→ llama-defender（代理）→ llama-server（后端）`。代理**可见**：客户端每轮发来的全量历史（含 tool_use/tool_result）、自己发给后端的请求、后端的响应流。代理**不可见**：工具的实际执行（发生在客户端）、客户端内部状态。按此边界分三类：

| 类别 | 内容 | 说明 |
|------|------|------|
| **① 代理进程内可完成** | L1 台账、写入期压缩（§4.3）、L2/L3、尾部复述块、合成负反馈、去重计数、epoch 状态机、canonical diff、`cache_prompt`/`id_slot` 请求参数、看门狗 HTTP 接口 | 全部是「请求内容变换 + 请求参数附加」，与现有语义压缩同机制，仅策略不同 |
| **② 项目管辖、非代理进程** | KV 量化、`-np` slot 数、`--slot-save-path` | 属 llama-server **启动参数**，走 manage.sh / profiles 配置层（本项目本就管理服务生命周期）。注意 `-c` 是总量均分到各 slot（`-c 1024 -np 2` → 每 slot 512）：slot 数与单会话上下文上限是 tradeoff，批跑串行场景 `np=1-2` 即可，agent_go `--parallel 3` 场景需相应加大 `-c` |
| **③ 代理层做不到（需降级）** | 见下两条边界 | — |

**边界 1：模型可主动调用的档案召回工具（L4）做不了（除非架构升级）**
工具定义来自客户端；代理私自注入的工具若被模型调用，tool_use 会返回给 claude CLI 执行 → 客户端报未知工具错误。绕过需做「隐藏内循环」（代理拦截自己注入的工具调用、自行执行、喂回 llama-server 继续生成，对客户端呈现单一响应流）——涉及多轮内部请求、计量、流式语义，属架构升级，**明确推迟**。降级方案：
- 复述块被动携带（代理按启发式决定注入什么，模型不主动召回）；
- 看门狗/档案查询做成**给 harness 的 HTTP 端点**（人/批跑器消费，非模型消费）。

**边界 2：客户端行为只能监控、不能约束**
L0 内容（system prompt + 工具定义）由客户端决定，代理只能「不动它」，不能「修它」。若 claude CLI 自身往 system 塞变化内容或做自带 microcompaction，代理只能通过 `canonical_mismatch` 指标发现，无法阻止——此时缓存收益打折属客户端侧问题，记录并单独反馈。

**大前提：代理从「逐请求无状态压缩」升级为「有会话状态」**
现状每轮改写很可能是无状态的（每请求全量重压）。本设计要求代理维护 canonical history（客户端原始历史 + 压缩视图双份）与会话生命周期（TTL/驱逐/内存上限/并发安全）——这是 Phase 1 最大的架构改动点，不是策略参数。

**两个实现级注意**：
1. 「写入期压缩」在代理层的实际时机是**下一请求到达时**（工具结果随客户端下一次请求到达，代理无法在结果产生的瞬间截获）——对后端视角语义不变（发送历史前已定型），但 canonical 与客户端历史是两套账，diff 逻辑须以客户端原始历史为基准、忽略代理自身注入块（复述块/负反馈需带注入标记）。
2. token 精确计数依赖 llama-server `/tokenize` 端点（可缓存），或按 chars/4 估算留余量；epoch 触发的 `ctx_max` 从 `/api/status`（R11）或配置同步获取。

---

## 5. 实施计划

### Phase 0：诊断确认（0.5 天，先行）

- llama-defender 记录每轮请求的 llama-server 日志指标：`n_past / n_tokens / prompt_eval_ms`
- 输出：每轮增量占比曲线。验收：现状 `n_tokens/n_past ≈ 1.0` 坐实缓存击穿诊断
- **隔离对照实验**：绕过代理改写，直接向 llama-server 发 append-only 序列（固定前缀 + 增量尾部），验证 `n_tokens/n_past < 0.1`——把「llama-server 缓存本身可用」与「代理改写是元凶」两个假设分开：若直发也不命中，问题在服务端/模型侧（SWA 等），设计需先退化
- 同时验证 SWA 缓存语义（§4.8 ⚠️ 项）

### Phase 1：核心改造（1-2 天）——append-only + 写入期压缩 + epoch

> **状态（2026-08-21）**：已实施——`context_engine.py`（写入期压缩模板 §4.3 / canonical 会话两套账 §4.10 / epoch 状态机 §4.9 含回退保护与 413 硬上限）+ 管线 stage 0.5（开启时 6/7/14/17 四 stage 联动跳过——6 CacheAligner 须一并跳过：其拆 prefix/dynamic 后由 7 重组，7 跳过则空 messages 送后端，gate 实测 400 拦截）+ 诊断接线（R16 `epoch_count/is_epoch_turn/epoch_triggered` 回填、R13 `epoch_count` 头/尾注、D6 新 kind `epoch_collapse`）+ `/api/status` `ctx_config.engine_enabled/epoch_S/window_K` 真值 + cached_tokens 透传（`cache_read_input_tokens`，Anthropic 原生字段）。§12.4 修正已纳入。单测 +19，全量 1156 绿。
>
> **验收门禁实测（2026-08-21，方案 A 合成会话 `tools/gate_test_ctx_engine.py`，35B 后端，S=60K/K=24）**：80 轮 agentic 会话——G1 非epoch轮 P90 **4.6s**（<15s ✅）/ G2 epoch轮 **2.4s**（<60s ✅，turn 54 触发，prompt 51.9K→12.1K）/ G3 **0** 断连错误 ✅ / G4 epoch后hit恢复 **0.921**（>0.8 ✅，爬回 0.97）/ 门禁1 增量prefill占比 P90 **2.3%**（<10% ✅）。诊断标记闭环：`is_epoch_turn(turn=54)`+`epoch_collapse`+`epoch_count=1` 全命中。**门禁 1-3 合成口径通过；门禁 3 句柄抽查与门禁 4 端到端双臂待真实任务（roadmap M3）。** 实测教训：① rapid-mlx 缓存要求"已存条目 ⊂ 新请求"（严格前缀扩展），客户端必须回显模型真实回复（claude CLI 天然满足；自建 harness 丢弃回复即全量 MISS）；② 会话 key 截断 8 字符下，相同前缀的 harness 会话共享引擎状态（内容无损但缓存互扰），harness 应保证 sid 前 8 字符唯一。生产 conf 已开启引擎（rapid-mlx-35b-opt `PROXY_CTX_ENGINE_ENABLED=true`）。
>
> **M2.1 语义修正（2026-08-22）**：8-22 真实任务重跑（303 轮）暴露
> canonical_mismatch 70%→每轮全量重建→TTFT_p90 131s 的 P0 缺陷，根因是
> diff 基准用了「客户端原始历史前缀」（M2 初始实现）——客户端任何扰动
> （microcompaction/头部改写/尾部裁剪，§4.10 边界 2 实锤）都触发重建。
> 修正：**新观测判定改为 sent_set（已发送消息 hash 集合）去重**，发送视图
> 纯 append-only——缓存键是「代理发送了什么」而非「客户端说了什么」。
> 客户端 compaction 摘要是新消息自然追加，被删除旧段保留（token 略涨，
> 由 epoch 收编）；mismatch 降级为纯诊断信号（仅当最近发送末尾消息不再
> 出现在客户端历史时置位，只上报不重建）。epoch 折叠后 sent_order 同步收
> 缩但 sent_set 不清（防重放）。单测 +2 调整（mid_rewrite/tail_mutation
> 场景按新语义断言）。

| 改动点 | 内容 |
|--------|------|
| llama-defender | canonical history + 前缀 diff + 写入期压缩模板（§4.3）+ epoch 状态机（§4.9）+ `cache_prompt`/`id_slot` 请求参数 |
| 不动 | llama.cpp 内核、客户端、模型配置 |

验收门禁：
1. 每轮增量 prefill 占比 >90%（`n_tokens/n_past < 0.1`）
2. 60+ 轮会话延迟分档（v0.2 修订，原 <15s 单目标不现实——epoch 轮重算 40-70K tokens 本身就需 20-70s）：非 epoch 轮 P90 <15s；epoch 轮 P90 <60s；整体 P90 <30s（对照现状 ~3min/轮，估算依据见 §6.3）
3. 压缩正确性：任意轮次可通过句柄重新取得原始内容（抽查）
4. 会话端到端时长对比报告（同任务、双臂）

### Phase 2：台账 + 复述块 + 看门狗数据源（1 天）

- L1 台账 + 尾部复述块（去重计数 + 材料清单）
- 看门狗接口：暴露台账查询（供批跑 harness 轮级 no-progress 检测消费——agent_go 任务级 `diff_stat_hash` 模式的轮级移植）
- 合成负反馈注入实现，**默认关**，开关进配置

验收门禁：
1. 探针任务集（含已知兔子洞场景）无效轮占比下降
2. 复述块 token 预算达标（≤600）
3. 注入开关状态在转录中可追溯

### Phase 3（可选）：档案召回 + slot 持久化

- L4 档案库访问：**先做降级形态**——`GET /api/session/<key>/archive` 查询端点（harness/人消费，§4.10 边界 1 的降级方案）；「模型可调用的召回工具」需隐藏内循环架构升级，明确不在本期
- `--slot-save-path` 持久化（跨 backend 重启）

---

## 6. 验证与指标

### 6.1 核心指标

| 指标 | 定义 | 目标 |
|------|------|------|
| **前缀命中率** | `1 - n_tokens/n_past`（llama-server 日志） | 运行态 >90% |
| 单轮 prefill 耗时 | prompt_eval_ms 分布（P50/P90，**epoch 轮与非 epoch 轮分档统计**） | 非 epoch 轮 P90 <15s；epoch 轮 <60s；整体 <30s |
| 会话端到端时长 | 任务开始到 done | 对照现状显著下降（预期量级见 §6.3） |
| epoch 数 | 每任务压缩次数 | 个位数（非每轮） |
| 无效轮占比 / 重复查询率 | 台账推导 | Phase 2 A/B 对比下降 |
| canonical_mismatch | 客户端历史与 canonical 前缀不匹配次数 | 监控项（频率高说明客户端自身在改历史） |

### 6.2 A/B 实验设计

```text
任务集：含已知兔子洞场景的探针集（10 个量级）+ 公开集抽样
臂 1（基线）：现状每轮语义改写
臂 2：Phase 1（append-only + epoch）
臂 3：Phase 2（+ 台账/复述块，注入关）
臂 4：Phase 2 + 合成负反馈（注入开）——单独口径，verdict 标注干预
双产出：延迟/成本指标 + 形态学指标（兔子洞率）
```

臂 2 vs 臂 1 分离**纯延迟收益**；臂 3 vs 臂 2 分离**元认知证据保留收益**（检验「压缩销毁元认知证据」假设）；臂 4 vs 臂 3 量化**主动干预收益**。

### 6.3 预期量级（估算，需实测校准）

- 修复前：~100K 全量 prefill × 每轮 ≈ 50-180s/轮（27B 本地 prefill ~500-2000 tok/s）+ 每轮压缩 LLM 调用 → 与 3min/轮观测吻合
- 修复后（非 epoch 轮）：增量 ~1-3K + 复述块 ≤600 ≈ 2-4s（**v0.3 起该估算才严格成立**——v0.2 的独立台账块每轮被原生流推移 ~15K，实际会是 8-20s）
- 修复后（epoch 轮）：重算范围 = 新压缩区 + 原生流 + 复述 ≈ 40-70K → 20-70s（按 1000-2000 tok/s）——这是验收分档的由来，也是 S 调优的动机（开放问题 1）
- 41 任务批跑：从 7-12 天量级压缩至 1-2 天量级（若兔子洞率同步下降还可能更短——无效轮本身是时长主贡献者）

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| SWA 混合架构缓存语义异常 | 前缀缓存收益不成立 | Phase 0 实测验证；异常则改用「窗口内 append-only」（等价退化，收益打折但仍优于每轮改写） |
| epoch 边界缓存重建 | 每 epoch 一次全量重算 | 固有代价；S 越大摊销越好，但受显存与长上下文性能退化约束——S 实测调优（开放问题 1） |
| 写入期压缩丢关键信息 | 模型缺材料 | 句柄可恢复（Manus 原则）+ L2 保护窗口 + 错误全文保留；A/B 观察通过率不劣化 |
| 台账膨胀（超长会话） | L1 自身成为大头 | §4.2 降级聚合（保计数合并）；台账预算上限告警 |
| 复述块扰动模型行为 | 个别任务变差 | 有界 ≤600 tokens；A/B 臂 3 单独验证；必要时按任务类型关闭 |
| 客户端自身微压缩 | canonical diff 失配 | §4.7 降级重建 + canonical_mismatch 监控 |
| 合成负反馈污染 bench 口径 | verdict 不可比 | 默认关；开时转录标注、单独臂统计 |
| L3 摘要信息丢失致任务失败 | 模型缺关键材料 | 句柄可恢复 + L2 保护 + 错误全文保留；A/B 加「摘要信息保留率」探针（随机抽查 L3 摘要，人工判定关键信息是否保留） |
| epoch 打断连贯推理链 | 压缩时机切断 thought 链 | §4.9 推理链保护启发式（连续推理中延迟到轮边界） |

---

## 8. 开放问题

1. **S（epoch 触发阈值）取值**：默认 `min(65% × ctx_max, ctx_max - 8K)`。灵敏度实验：固定任务，S=50%/65%/80% 三档，最小化「epoch 次数 × 单次重建耗时」乘积，同时观察通过率不劣化。
2. **K（L2 窗口）取值**：默认 8。4/8/12/16 网格；任务类型分层（代码修复 vs 查询类）最优 K 可能不同。
3. **复述块注入位置**：末尾（缓存友好 + 近因注意力）；L0 后固定区每轮击穿其后缓存，不推荐（仅记录）。负反馈的两种注入位置 A/B 见 §4.6。
4. **与 agent_go 侧 `/compact` 类机制的边界**：短期靠 `canonical_mismatch` 降级监控；长期方向是协商「客户端不做微压缩、压缩全权归代理」（单一责任方），需向 agent_go 侧提需求。
5. **LLM 摘要档**（L3 质量增强）：默认不开。确定性模板先行；每 epoch 一次 LLM 调用的延迟/成本很可能不抵质量收益。
6. **L3 与台账的冗余简化**：epoch 后的老轮次是否可仅由「台账 + 句柄」代表（L3 层取消）——台账已含动作 + 结果摘要 + 计数。若 A/B 显示通过率不劣化，可砍一层。

---

## 9. agent_go 侧落地清单

> 本设计 proxy-centric，agent_go 端**无核心改动**；以下为口径/协调/闭环三类触点。防过度落地：压缩、epoch、台账实现全在代理进程内（§4.10 ①）；TASK.md/agent_prompt 是子任务启动时一次性注入（首条消息，非每轮变化的 L0），不违反 §4.1 前缀纪律，**不需要改**。

### P0：口径与观测（不做则 A/B 口径污染、指标不可消费）

| # | 事项 | 文档依据 | 落地点 |
|---|------|---------|--------|
| 1 | **bench 口径标注**：代理干预状态进批次记录（压缩模式 / 负反馈注入开关 / S、K 值） | §4.6「开启时必须在转录中标注」、§6.2 四臂 | `batch_governance.py` manifest 运行配置摘要扩展（最小形态，不动 bench schema）；若需记录级区分再加 `bench_schema.py` 字段 |
| 2 | **metering 归因扩展**：解析代理新响应头（epoch 计数、feedback 注入标记），复用 R8 头模式 | §6.1 指标消费 | `api.py:156`（R8 解析模式）扩展 → metering.jsonl 字段 → `eval.py` analyze 可查。代理侧头定义需先向 llama-defender 提需求（integration-requirements 可记 R13） |
| 3 | **并发与 slot 协调**：`--parallel N` 超代理 slot 数时告警/降并发 | §4.10 类② | `profiles.py:319` health_check 扩展（读代理 `/api/status` 的 slot/ctx 配置，R11 先例）；pipeline pre-flight 就绪检查顺带 |

### P1：协调与闭环（增值）

| # | 事项 | 文档依据 | 落地点 |
|---|------|---------|--------|
| 4 | **轮级无进展看门狗**（agent_go 作为代理台账端点的第二消费方，第一是批跑 harness） | Phase 2「agent_go 任务级 diff_stat_hash 模式的轮级移植」 | `subtask.py:131/364`（MAX_GOAL_TURNS watchdog 模式）扩展：轮询 `GET /api/session/<key>/ledger`，dup ≥3 → 记 rabbit_hole 事件（metering/execution.log），开关控制 kill 或仅标注；同时成为「无效轮占比」一等指标的数据源 |
| 5 | **压缩归属分工声明**：「走代理时压缩全权归代理，agent_go 不做客户端压缩」 | 开放问题 4 | 文档动作：`production-model-config.md`/`config-schema.md` 写明分工；未来 client-side compact 功能须先与代理协调 |
| 6 | **模型切换 × 会话状态协调**：代理有状态后，switch/reload 时段的中断代价变为「epoch 重建 + KV slot 失效」 | §4.10 大前提（代理有会话状态） | local-model-management P1 切换原子序列加前置检查：代理活跃会话 >0 时延迟/告警（「活跃任务并发保护」从任务粒度扩到会话粒度） |

### P2：可选

7. runbook/config 文档：缓存命中率排查（`n_tokens/n_past` 读法）、代理上下文工程配置说明
8. web 配置中心：health_check 展示代理会话数/命中率/epoch 统计
9. **A/B 归属决策**：§6.2 四臂实验建议由批跑 harness 承载（公开集口径在那里），agent_go 只消费结果；若改由 agent_go bench 承载，`bench.py` 需支持按臂切代理配置

---

## 10. 诊断数据面完备性：缺口与接口需求

> 视角：agent_go 诊断分析（缓存验证 / 延迟归因 / 形态学复盘 / 会话观测）需要什么 vs 现在有什么。结论：**执行/成本/路由归因已齐；缓存、会话、行为复盘三域有缺口**——全部可通过「代理透传 + 少量新端点」补齐，需上游 llama.cpp 改动的仅一处（且需先验证）。

### 10.1 现状：能回答什么

| 域 | 数据 | 状态 |
|---|---|---|
| 执行归因 | meta.json / execution.log / verify_state / replay | ✅ 齐 |
| 成本/路由归因 | metering.jsonl + R8 头（`api.py:156` 解析） | ✅ 齐（R8 后） |
| 后端健康 | `/api/status` / `/metrics`（含历史、ttft） | ✅ 基本齐 |
| 客户端行为转录 | claude CLI session 日志（批跑 harness 已在用） | ⚠️ 客户端视角——**压缩后模型实际所见 ≠ 客户端所发**，作不了压缩后行为的依据 |

### 10.2 缺口与需求（按提供方分三层）

**① llama.cpp 原生已有，代理只需透传/采集（零上游改动）**

| 数据 | 原生位置 | 支撑诊断 |
|---|---|---|
| per-request `timings`（prompt_n / prompt_ms / predicted_*）+ `usage` | **响应体**（官方文档确认，版本需实测） | **每轮缓存命中率 = 1 − prompt_n / usage.prompt_tokens，无需解析日志**——Phase 0/1 核心指标的干净来源 |
| `/props`（n_ctx / total_slots / model_path） | GET /props | slot/并发协调（§9 P0-3）、epoch 触发阈值 ctx_max 同步、架构元数据（SWA 判定佐证） |
| `/slots` 实时状态 | GET /slots（需 `--slots` 启动） | 并发会话 vs slot 匹配监控 |
| verbose slot 日志（n_past / n_tokens / kv cache rm） | llama-server stdout | 兜底——文本日志格式不稳定，不作主路径 |

**② 需要代理新建（R13 系列，走 integration-requirements 需求流程）**

| 需求 | 内容 | 支撑 |
|---|---|---|
| **R13 响应头扩展** | `X-Proxy-Prompt-Processed-N`（本轮实算 prefill 数）、`X-Proxy-Epoch-Count`、`X-Proxy-Feedback-Injected`——复用 R8 头模式 | agent_go metering 采集（`api.py:156` R8 解析扩展）→ §6.1 指标消费 |
| **R14 会话台账端点** | `GET /api/session/<key>/ledger`（dup / last_dup_turn / 材料清单）——即 Phase 2 已设计项 | agent_go 轮级看门狗（§9 P1-4）+ 无效轮占比一等指标 |
| **R15 档案查询** | L4 只读访问 `GET /api/session/<key>/archive`——从 Phase 3 降级形态**提前**与 Phase 2 同期 | 压缩后行为复盘必须以代理档案为准（视角正确性）；批跑形态学分析（兔子洞）的权威数据源 |
| **R16 /metrics 会话维度扩展** | 每轮结构化落盘（session_key / turn / sent_tokens / processed_tokens / hit_ratio / epoch 触发 / 注入标记）jsonl + 按 session 聚合时序 | 时间线复盘、`canonical_mismatch` 监控、A/B 出数 |

**③ 可能涉及上游 llama.cpp（仅一处，需验证）**
- 当前部署版本的 `/v1/chat/completions` 响应是否实际包含 timings 对象（官方文档载明，本地版本需实测）。缺失退路：代理解析 llama-server 日志或走 `/completion` 端点取 timings；或向上游提 issue——不阻塞设计（日志兜底可用）

### 10.3 优先级（对齐实施 Phase）

| Phase | 必需项 | 理由 |
|---|---|---|
| Phase 0 | ① timings 透传 | 无它无法坐实 `n_tokens/n_past ≈ 1.0` 诊断——Phase 0 是闸门，数据是闸门的闸门 |
| Phase 1 验收 | R13 头 + R16 基础落盘 | 命中率/延迟分档曲线出数 |
| Phase 2 | R14 台账端点；R15 档案（若形态学复盘进 A/B） | 看门狗与兔子洞归因 |
| 常态 | ① /props、/slots 透传 | 并发协调与漂移监控 |

### 10.4 一条设计原则

> **诊断数据采集责任全部归代理，agent_go 只消费结构化接口**（响应头 / 端点 / jsonl）——不解析 llama-server 文本日志（格式不稳定），不把 claude CLI 侧转录当作压缩后行为的依据（视角错位：那是客户端发的，不是模型看的）。

---

## 11. 实测校准修订（2026-08-19，公开集批跑实证复核）

> 本节为外部 review 追加：以 2026-08-18/19 公开集本地臂批跑实测数据校准 §0-§8 的假设、默认参数与验收门禁。核心结论：**设计方向与结构正确，但 prefill 速度假设差 6-12 倍，且漏掉「客户端断连死线」与「pflash 96K 阈值」两个硬约束——默认参数与验收门禁须按本节重定，否则 Phase 1 验收在实测环境不可达**。原 §0-§10 文字不动，本节为修订增量。

### 11.1 三个硬约束（§0 补充，参数重定的事实基础）

**约束一：prefill 实测速度 ~130-300 tok/s，不是 1000-2000 tok/s。**

| 实测 | 数据 | 换算 |
|------|------|------|
| 42355d18 死锁轮（2026-08-19） | `backend_dispatcher completed in 466,091.9ms`，payload 338,956 chars | 466s / ~75K tokens ≈ **160 tok/s** |
| 冒烟任务 2 末轮（2026-08-18） | 344K chars 撞 600s 超时 exit 1 | 600s / ~78K tokens ≈ **130 tok/s** |
| click-06 干净计时冷启动 | 冷 TTFT 128s | 同量级 |
| click-06 热轮（缓存命中上界） | 6.9-9.9s/轮 | 缓存命中时增量 prefill 速度上限参照 |

§6.3「非 epoch 轮 2-4s / epoch 轮 20-70s」按此速度重推后不可达（见 11.2）。

**约束二：客户端 ~466s 断连死线。** claude CLI 客户端侧请求超时实测 ~466s（13:50:08 请求 → 13:54:24 `Client disconnected mid-response (broken pipe)`，backend 用时 466,091.9ms，客户端 13:55:00 立即重试同一 payload → 断-重试死循环）。payload 涨过 ~300K chars（≈70K tokens）后冷 prefill 必超该超时——**append-only 让上下文增长更顺滑，死线会来得更快**。客户端超时值不可配（claude CLI 2.1.233 bundle 中无 HTTP 超时 / 自动压缩阈值类环境变量，已实测验证）。推论：**任何单轮 prefill（含 epoch 重建）必须 ≤ ~45K tokens（@160 tok/s ≈ 280s，留 ~40% 余量）**。

**约束三：pflash 96K tokens 阈值（rapid-mlx 特有）。** 已实测定案：payload >96K tokens 时 rapid-mlx pflash 按设计绕过前缀缓存（keep_ratio 0.20 + recency 0.05 → 模型每轮仅 ~20% 上下文可见）——append-only 的全部缓存收益在此之上归零，且模型视野被静默截断。

**合并推导**：

```text
S（epoch 触发阈值）≤ min(96K tokens, ~70K tokens) ≈ 70K tokens（≈300K chars）
单轮 prefill（含 epoch 重建）≤ ~45K tokens（客户端断连死线留余量）
```

§4.9 默认 S = min(65% × ctx_max, ctx_max − 8K)：ctx_max=400K 时 S=260K tokens ≈ 1.1M chars——**远超死线 4 倍，作废**。K=8 原生流（8-16K）+ L3（每轮 ≤80 tokens × 轮数）+ L0（5-10K）在 70K 预算内可行，但 epoch 数受限；epoch 重建 45K tokens @160-300 tok/s ≈ 150-280s，两端均须在客户端断连前完成。

### 11.2 §6.3 数字重推与验收门禁修订

| 项 | 原设计 | 实测校准后 |
|----|--------|-----------|
| 非 epoch 轮 | 增量 ~1-3K + 复述 ≤600 ≈ 2-4s | 增量 1-3K @160-300 tok/s ≈ **4-20s** |
| epoch 轮 | 重算 40-70K ≈ 20-70s | 重建 ≤45K @160-300 tok/s ≈ **150-280s**（且 ≤ 断连死线是硬约束，45K 是上限不是目标） |
| 41 任务批跑 | 7-12 天 → 1-2 天 | → **2-3 天**（量级不变，系数按 1.5× 校准） |

Phase 1 验收门禁 2 修订：非 epoch 轮 P90 **<20s**；epoch 轮 **<300s 且 0 断连**；整体 P90 <30s。原「epoch 轮 P90 <60s」在 rapid-mlx 上不可达，按此验收会误判改造失败。

### 11.3 既有防御机制去留矩阵（新增，设计原未覆盖）

| 机制 | 现值 | 治理后状态 | 处置 |
|------|------|-----------|------|
| 413 墙（PROXY_QUEUE_HUGE_THRESHOLD_CHARS） | 200K chars | payload 恒 ≤S≈300K chars 时永不触发 | 保留作兜底 |
| **OOM 预截断（PROXY_OOM_SAFE_CHARS，keep_rounds=2 按轮砍）** | 450K chars（08-19 实测意外回退 200K，造成 2h 失忆污染） | **与 append-only 布局冲突**——按轮砍重排历史 → 缓存全量击穿，且模型静默失忆 | **退役或改造**（改造方向：超限时走 §4.9 回退保护「返回超限错误」而非静默砍轮）。优先级最高——本机制已实测造成批跑污染 |
| 队列 300s 超时 | 300s | prefill 从分钟级降到秒级后队列压力自然消退（08-19 a1569ea4 死因即队列超时风暴） | 保留，治理后应观察 0 触发 |
| 后端 900s 超时 | 900s | 单轮 prefill ≤280s，不触发 | 保留 |
| 客户端 ~466s 断连 | 不可配（已验证） | 靠 S 与单轮 prefill 预算回避 | 无 |

### 11.4 环境差异：rapid-mlx（§4.8 修订）

§4.8 全部为 llama-server 参数，实测后端为 rapid-mlx（127.0.0.1:8081）——对应能力须逐项验证：前缀缓存可用性已有实测（热轮 6.9-9.9s 即增量 prefill 命中），但 `id_slot` / `--slot-save-path` / KV 量化未必存在，Phase 0 一并核。SWA 验证项（§4.8 ⚠️）之外，**增加 pflash 96K 阈值行为验证**（96K 上下各 10 次往返，确认缓存命中/绕过切换点）。

### 11.5 dup 覆盖性边界（§4.2/§4.6 修订）

hash 规范化 dup 计数只抓「同查询重复」（748f534 的 PR 80376 形态）。实测另两种失败形态不在覆盖内：**换查询兔子洞**（同一意图换查询词/换引擎继续搜）与**训练先验幻觉**（click-06 引用不存在的 changelog 8.3.0 / 符号 `_expand_tabs`，把 bug 树里已有内容当「发现」）。台账是**必要非充分**——Phase 2 验收勿以 dup 计数替代无效轮占比的完整形态分类。

### 11.6 swe-eval harness 触点（§6.2/§9 补充）

§6.2 四臂 A/B 的承载点即 swe-eval harness（公开集口径在那里）。落地需：run_agent 在 runs.jsonl 记录代理干预状态（压缩模式 / S / K / 负反馈注入开关），report 按臂分桶——否则臂 2-4 的 verdict 与基线混在同一口径，A/B 结论不可信。批跑前与 llama-defender 的 R13 响应头对齐字段即可，改动量小。

### 11.7 开放问题 1 重写

S 的优化不再是一维（「epoch 次数 × 单次重建耗时」乘积），而是双约束下的可行域问题：

```text
S ≤ 70K tokens（客户端断连死线 + pflash 96K 取小）
epoch 重建 ≤ 45K tokens（断连死线留余量）
候选档：S = 50K / 60K / 68K 三档（原 50%/65%/80% 档作废——按 ctx_max 百分比定档会越过死线）
灵敏度目标不变：最小化 epoch 次数 × 重建耗时，通过率不劣化
```

---

## 12. Phase 0 诊断结果（2026-08-20 实测，Qwen3.6-35B-A3B）

> 生产后端已于 2026-08-20 切 35B（治理前置决策）。本诊断数据源与结论均以 35B 为准；
> 3.8 期行为证据（~3min/轮、600s 冷 prefill、466s 断连）按历史定案引用。

### 12.1 数据源定案（修正 §10.2 ①）

| 预期数据源 | 实测 | 定案 |
|---|---|---|
| 响应体 `timings`（prompt_n/prompt_ms） | **无**（rapid-mlx 确认不返回，L-5 第三次确认） | 弃用 |
| llama-server stdout n_past/n_tokens | rapid-mlx 日志不落盘（终端前台） | 不可用 |
| **响应体 `usage.prompt_tokens_details.cached_tokens`** | **有**（35B 原生返回，OpenAI 兼容） | **主路径**：命中率 = cached_tokens / prompt_tokens |
| 3.8 期代理日志 usage | 无 prompt_tokens_details（字段缺席） | 历史击穿证据只能用行为层定案 |

cached_tokens 比 n_past 更干净：直接给"本次请求复用自 KV 的 token 数"，无需解析日志。
Phase 1 验收门禁 1 与 R16 落盘字段建议统一改用此口径。

### 12.2 隔离对照实验（协议 §4.8 + §5，3200-token 固定前缀 P + 逐次变化尾部）

| 序列 | 条件 | 结果 | 判定 |
|---|---|---|---|
| A 初跑 ×10（直发，无 cache_prompt） | **与用户会话 cli_f456/cli_4aae 并发**（日志坐实） | cached=2，hit≈0.0006，7s/轮（全量 re-prefill） | miss 根因 = 并发，非配置 |
| V1-V4（cache_prompt / session 头 / 组合 / 每次新 session） | 无并发 | 全部 hit 0.994-0.995 | 见结论 2 |
| 干净 A ×5（直发，无 cache_prompt） | 无并发 | hit 0.995，热 0.4-0.75s | 缓存工作正常 |
| B 改写序列（中段 1/3 反转） | 无并发 | B1 正常 / B2 prompt 3217→4284 | 改写破坏前缀（预期） |
| C ×4（经代理，同 P 序列） | 无并发 | hit 0.987-0.993，0.75-0.95s | 代理不破坏固定前缀 |

### 12.3 五项结论

1. **35B 前缀缓存工作正常**：3200-token 前缀全量复用（hit 0.995），热请求 0.4-0.96s vs
   冷 7s（冷 prefill 实测 ~460 tok/s@3.2K——低于 model-assessment 1360 tok/s 口径，
   可能与 pflash/负载相关，Phase 1 校准样本再取）。
2. **`cache_prompt` 参数非必需**：无参数请求同样保留并复用 KV（干净 A 连续命中）。
   §4.8 表格"请求带 cache_prompt: true"修正为"**可选保险**，rapid-mlx 默认即复用"。
3. **SWA ⚠️ 项通过**：35B hybrid 架构下 3200-token 前缀全量复用，无"SWA 窗口外前缀
   不可复用"异常信号。35B 缓存语义与 dense 一致。
4. **击穿根因归代理改写，与后端无关**：代理链路对固定前缀零破坏（C 99%）；真实批跑
   会话的击穿来自多轮工具会话中的每轮改写（tool clearing/compress/fifo）——3.8 期
   ~3min/轮 + 600s 冷 prefill 的行为证据已坐实，35B 上缓存可用性由本实验证明。
   两者合成 = Phase 0 验收达成（"缓存本身可用"与"改写是元凶"两个假设分离）。
5. **新发现：max-num-seqs=1 下并发流量逐出 KV**——A 初跑与用户日常会话并发期间，
   单 slot 在请求间被其他会话占用，KV 全清、每轮全量 re-prefill；并发消失后同一
   序列立即恢复 0.4s/轮。**批跑期与用户日常使用共享后端 = 批跑会话缓存随时清零**
   （35B 冷 prefill 快，miss 成本比 3.8 低 ~4.6×，但仍应规避）。

### 12.4 对 Phase 1 的修正输入

1. `cache_prompt` 从"必需"降为"可选保险"（结论 2）。
2. **并发逐出是 §4.8"slot 固定"方案在 rapid-mlx 上的死穴**：max-num-seqs=1 只有
   一个 slot，id_slot 无法隔离生产与批跑流量。缓解选项（Phase 1 设计取舍）：
   a) 批跑窗口纪律（批跑期间不用代理做交互工作——不依赖代码，脆弱）；
   b) 批跑专用后端实例（端口隔离，35B 内存 ~12GB 可并跑第二实例，成本可接受）；
   c) 提高 max-num-seqs（与"每会话独占 slot"冲突，仅当并发会话数 ≤ seqs 才成立）。
   建议 b 为主（治理后 41 任务批跑时开专用 8081 实例）+ a 为辅。
3. S 预算重标定输入：本次无干净冷 prefill 样本（仅有的 7s@3.2K 样本取自并发窗口，
   被 slot 排队污染，只作量级参考）；35B 冷 prefill 以 model-assessment §3.1 三方
   实测 ~1360 tok/s 为准，epoch 重建 40-70K tokens 对应 ~30-50s。**§11 的 3.8
   实测校准（prefill 130-357 tok/s、P90<60s 门禁推导）在 35B 上全部重定**，门禁
   目标取"非 epoch 轮 P90<15s、epoch 轮 P90<60s"仍可达。客户端 466s 死线在 35B
    下允许单轮 prefill 至 ~200K tokens，不再是硬约束（E-3 在 35B 上自动解除）；
    35B 新硬约束 = pflash 阈值（当前 96K）与 epoch S 预算本身。

---

## 13. 里程碑溯源（M2 / M2.1 / M3，产品经理视角）

> 本节把 M2.1、M3 两个版本号的来历串成一条「问题 → 证据 → 假设 → 翻车
> → 修正 → 待验证」的证据链，便于评审与回溯。结论先行：**M2.1 是真实
> 任务翻车逼出的修正，M3 是真实任务才能做的毕业考**——两者都指向同一条
> 铁律：验收必须用「代理控制不了的客户端」，否则门禁通过 ≠ 生产可用。

### 13.1 根问题（改造立项依据）

每轮语义改写 → KV 前缀缓存全量失效 → 三个症状（§0.2）：延迟恶化（~3min/轮）、
成本 O(n³)、元认知证据销毁（搜索兔子洞无自愈）。核心判断（§0.3）：元凶是
「每轮改写」而非「不压缩」。

### 13.2 Phase 0 诊断（先坐实再动工，§12）

2026-08-20 在 35B 后端做隔离对照，分离两个假设：
- 后端缓存本身可用？→ 固定前缀直发命中率 **0.995**（结论 1/3）
- 击穿是代理造成？→ 代理对固定前缀零破坏（结论 4），真实击穿来自每轮改写

**结论 4**：后端无病，病在代理「每轮改写」——整个改造的立项依据。

### 13.3 M2 = Phase 1 核心改造（append-only + epoch）

- **内容**：每轮改写 → 平时 append-only（增量 prefill）+ 压缩按 epoch。落地
  `context_engine.py` + 管线 stage 0.5 + 诊断接线，2026-08-21 提交（8ecc424 / 6d54be4）。
- **验证（合成门禁）**：`tools/gate_test_ctx_engine.py` 构造**完美 append-only
  客户端**（回显模型真实回复），80 轮门禁 G1–G4 全过（§5）：G1 非 epoch 轮
  P90 4.6s / G2 epoch 轮 2.4s / G3 0 断连 / G4 命中恢复 0.921 / 增量占比 P90 2.3%。
- **伏笔**：合成客户端是「完美回显」理想客户端，通过门禁 ≠ 真实 claude CLI 通过。

### 13.4 M2.1 怎么来的（真实任务翻车 → 根因，2026-08-22）

- **翻车**：真实 claude CLI 跑真实任务（42355d18，**303 轮**），M2 在真实链路崩——
  `canonical_mismatch` **70%** → 每轮全量重建 → 每轮前缀全变 → 缓存必断 →
  `TTFT_p90 131.7s` / `latency_p90 413s` / epoch×26 / loop×135。
- **根因**（§4.10 边界 2 实锤）：M2 的 `absorb()` 用「客户端原始历史前缀」做 diff
  基准；真实 claude CLI 会做自带 microcompaction / 头部改写 / 尾部裁剪，任何扰动
  都让「客户端前缀 ≠ 代理存的 canonical 前缀」→ 触发全量重建。合成客户端不扰动
  历史故门禁通过，真实客户端扰动历史故翻车——同一 bug 两面。
- **修正**（已提交 `2d6d16c`，2026-08-23）：缓存键应是「**代理发送了什么**」而非
  「客户端说了什么」——`absorb()` 新观测判定改 `sent_set`（已发送消息 hash 集合）
  去重，发送视图纯 append-only；客户端 compaction 摘要作新消息追加、被删旧段保留；
  `mismatch` 降级为纯诊断信号（只上报不重建）。单测 29 绿 + 全量回归无漂移。
- **PM 教训**：M2 验收覆盖「代理能控制的客户端」，漏了「代理控制不了的客户端行为」
  （边界 2 早已写明但未被门禁覆盖）。M2.1 把该边界从「监控项」变为「代码不变量」。

### 13.5 M3 是什么、为什么还没做

- **来源**：§5 Phase 1 验收原文——「门禁 3 句柄抽查与门禁 4 端到端双臂待真实任务
  （roadmap M3）」。即门禁 1–2 由合成客户端验证（M2 已完成），门禁 3（句柄可恢复
  原始内容）+ **门禁 4（同任务双臂端到端时长对比）**须真实客户端。
- **M3 = 真实任务端到端验收门禁**，是 M2/M2.1 在「生产形态客户端」上的最终确认，
  无法用合成脚本替代（合成脚本复现不了客户端 microcompaction 扰动，验证不了 M2.1）。
- **当前卡点**（截至 2026-08-23）：① 当前 `active.conf=ornith-oq4e` 且引擎关
  （`PROXY_CTX_ENGINE_ENABLED=false`），M2.1 代码路径休眠；② 运行代理虽已加载
  M2.1 但引擎未开；③ 工作区有并行会话未提交改动，重启会混入半途代码；④ M3 本身
  需真实跑 303 轮级任务，由批跑 harness / 用户触发。

### 13.6 状态看板

| 节点 | 内容 | 状态 | 证据 |
|------|------|------|------|
| Phase 0 | 缓存可用、改写是元凶 | ✅ 定案 | §12 隔离实验 |
| M2 (Phase 1) | append-only + epoch | ✅ 已交付 | 合成门禁 G1–G4 |
| M2.1 | sent_set 语义修正 | ✅ 已提交 2d6d16c，待重启生效 | 29 单测 + 全量回归绿 |
| M3 | 真实任务端到端验收 | ⏳ 未做（引擎关 + 需重启 + 需真实任务） | 门禁 3/4 待真实客户端 |

**下一步**：等并行 dflash 工作提交 → 切引擎开启配置 + 重启 → 重跑 M3（42355d18
同任务双臂对比）；或 SIGHUP reload 临时开引擎跑合成冒烟，M3 真实任务由用户触发。

### 13.7 M3 必要性与可行性分析（产品经理视角）

> 事实锚点：① M3 已写入 PRD（`docs/01-requirements-product/PRD-anthropic-proxy.md:664`）
> 「门禁 = 42355d18 单任务重跑：0 截断/0 超时/0 断连 + 轮时长曲线达标」，且是全量
> 41 任务批跑前置；② A/B harness 已存在（`tools/run_experiment.sh` prepare/collect/report，
> 支持 `--group A|B`）。

**必要性（P0，不可跳过）**
1. M2.1 是真实任务翻车逼出的，只有真实任务能证明修好：其根因（§4.10 边界 2）
   是客户端自带 microcompaction/头部改写/尾部裁剪，合成客户端（完美回显）复现不了
   该扰动——M2.1 价值恰在「对不完美客户端稳定」，合成门禁验证不了。
2. 历史已证「合成通过 ≠ 生产可用」：M2 合成 G1–G4 全过 → 真实 303 轮翻车
   （mismatch 70% / TTFT 131s）。M3 是这条铁律的强制执行点。
3. 门禁 3（句柄可恢复性）/ 4（双臂端到端时长对比）是改造终极 KPI，合成不可替代：
   门禁 3 覆盖真实多样工具类型（search/read/fetch/curl）的压缩正确性；门禁 4 回答
   「延迟/成本/元认知收益到底值不值」。
4. M2.1 tradeoff（保留被删旧段→token 涨）须真实长会话确认 epoch 收编能压回预算，
   否则越过 pflash 96K（缓存收益归零 + 模型视野截断）或客户端 466s 断连死线——
   合成短会话测不出。
5. 阻塞后续：M3 是 PRD 中全量 41 任务批跑与治理 Phase 2（台账/复述块）的前置闸门。

**可行性（中，5 项约束均可缓解）**
- 有利：A/B harness 就绪、任务明确（42355d18）、指标可采集（35B `cached_tokens`
  命中率干净 §12.1 + R16 每轮落盘 + R13 头）、本地无云成本可重跑。
- 约束与缓解：

| # | 约束 | 影响 | 缓解 |
|---|------|------|------|
| F1 | 当前引擎关 + active=ornith-oq4e（并行 dflash 配置）；重启加载并行未提交改动污染数据 | 高 | 先收尾并行 dflash 提交，干净工作树再重启 |
| F2 | 真实任务非确定性（采样），双臂对比有方差 | 中 | 硬判据（0 截断/0 超时/0 断连）不受方差；方向性结论即可，统计加跑 3× 取中位 |
| F3 | 耗时：baseline 长会话撞断连/pflash 可能跑不全 | 低（恰是证据） | baseline 跑崩 = 旧方案不行铁证；M2.1 臂跑满 303 轮确认稳定，单臂数小时本地可接受 |
| F4 | 并发逐出（§12.3 结论 5）：批跑共享后端被日常流量清 KV | 中 | 开专用 8081 实例（35B ~12GB 可并跑）或无人窗口 |
| F5 | epoch S 未定档（开放问题 1）；M2.1 保留旧段使 context 涨更快 | 高 | M3 前定 S 档（建议 60K ≤ 断连/pflash 取小 70K）；35B 冷 ~460 tok/s，epoch 重建 ≤45K≈98s < 断连余量 |

**最小可行 M3 序列**
1. 收尾并行 dflash 提交 → 干净工作树（F1）
2. 定 S 档（建议 60K）+ 切引擎开启配置（rapid-mlx-35b-opt 已开，或 ornith 加 `PROXY_CTX_ENGINE_ENABLED=true`）
3. 开专用 8081 实例或占无人窗口（F4）
4. `run_experiment.sh prepare --group A` 跑 baseline（引擎关，现每轮改写）/ `--group B` 跑 M2.1（引擎开），任务均 42355d18
5. 判据：硬布尔 0 截断/0 超时/0 断连 + 轮时长曲线（B 轮 P90 ≪ A）+ 运行态 hit_ratio >90% + 门禁 3 句柄抽查
6. 每臂 ≥1 次（硬判据够），方向性结论；统计加跑 3×

**判定**：必要性 P0（不做的話 M2.1 等于没验收）；可行性中（五道坎均有现成缓解，
唯一硬依赖是先收尾并行在地代码并把引擎打开）。
