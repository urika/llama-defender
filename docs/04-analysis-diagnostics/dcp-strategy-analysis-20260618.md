# DCP 策略分析与代理层上下文压缩对比（2026-06-18 更新版）

> 数据来源：[Opencode-DCP/opencode-dynamic-context-pruning](https://github.com/Opencode-DCP/opencode-dynamic-context-pruning) 官方 README（master 分支，SHA `d2c068c`）
> 对比对象：本项目 `anthropic_proxy.py` 8 层代理管线（Layer 2 内容压缩 + Layer 5 上下文截断）
> 参考文档：`docs/04-analysis-diagnostics/dcp-vs-proxy-context-compression.md`（2026-06-17 首版，含实现代码）
> 本文定位：在首版基础上补充官方 README 最新信息，修正配置默认值，并新增 **Sleev 迁移** 这一战略维度

---

## 0. 本次更新相对首版的关键差异

| 维度 | 首版（2026-06-17） | 本次更新（2026-06-18） | 来源 |
|------|-------------------|----------------------|------|
| `compress.maxContextLimit` 默认值 | 150000 | **100000** | README 官方默认配置 |
| `compress.summaryBuffer` | 未记录 | **新增**：让 active summary tokens 扩展有效 maxContextLimit | README |
| `compress.protectTags` | 未记录 | **新增**：`<protect>...</protect>` 标签保护 | README |
| `compress.showCompression` | 未记录 | **新增**：在 chat 通知中展示压缩内容 | README |
| `turnProtection` | 未记录 | **新增**：工具调用后 N 轮内不被剪枝 | README |
| `experimental.customPrompts` | 仅提"支持自定义 prompt" | **6 类可编辑 prompt 明确列出** | README |
| `experimental.allowSubAgents` | 未记录 | **新增**：是否允许在子代理会话中处理 DCP | README |
| **Sleev 迁移公告** | 未记录 | **重大**：DCP 开发放缓，新功能转移到 Sleev（本地代理） | README 项目状态段 |
| 许可证 | 未记录 | **AGPL-3.0-or-later** | README 末尾 |

> ⚠️ **Sleev 是本次更新最重要的战略信息**：DCP 作者已将新功能开发转移到 [Sleev](https://sleev.ai)，定位为"面向 Claude Code、Codex、OpenCode 的本地代理"。这与本项目 `anthropic_proxy.py` 的定位**高度重叠**——详见 §6。

---

## 1. DCP 策略总览（基于官方 README 校准）

DCP（Dynamic Context Pruning）是 OpenCode 的一款插件，目标是在**不修改会话原始历史**的前提下，动态减少发往 LLM 的上下文 token 量。核心思路：把上下文管理建模成**模型可主动调用的 `compress` 工具** + 若干**自动清理策略**。

### 1.1 核心组件

| 组件 | 作用 | 触发方式 |
|------|------|----------|
| **Compress 工具** | 将已完成、陈旧的对话区间替换为高保真技术摘要 | 模型主动调用（基于任务完成度），或由上下文压力 nudge 触发 |
| **Deduplication** | 相同工具 + 相同参数的重复调用只保留最新输出 | 自动，每次 compress 工具运行时重算 |
| **Purge Errors** | N 轮前的错误工具输入被剪枝，仅保留错误消息本身 | 自动，默认 4 轮，compress 工具运行时重算 |
| **Nudge 机制** | 在上下文接近阈值时向模型注入压缩提示 | 自动，基于 min/maxContextLimit 和 nudgeFrequency |

### 1.2 Compress 工具的两种模式

- **range 模式（默认）**：压缩一段连续的消息区间，生成一个或多个摘要块。新压缩若与旧摘要重叠，旧摘要被**嵌套**进新摘要，避免信息随多次压缩被稀释。
- **message 模式（实验性）**：逐条压缩单条原始消息，粒度更细，让模型像做手术一样管理上下文。

压缩时保护以下内容：
- 指定工具的输出（默认保护 `task`, `skill`, `todowrite`, `todoread`, `compress`, `batch`, `plan_enter`, `plan_exit`, `write`, `edit`）
- 子代理和 skill 的输出
- 用户通过 `protectedFilePatterns`（glob）指定的文件操作
- 用户消息（可选 `protectUserMessages`，但大段粘贴内容将永不压缩）
- `<protect>...</protect>` 标签包裹的文本（可选 `protectTags`）

### 1.3 非破坏性转换（关键设计）

**原始会话历史永远不被修改**。所有剪枝/压缩只作用于**即将发往 LLM 的出站消息副本**：
- 本地历史保持完整，可审计、可回退。
- 摘要块引用稳定的 message ID（如 `m0001`）和 compression block ID（如 `b1`），模型可明确知道哪些内容被折叠。

### 1.4 官方默认配置（校准后）

```jsonc
{
  "$schema": "https://raw.githubusercontent.com/Opencode-DCP/opencode-dynamic-context-pruning/master/dcp.schema.json",
  "enabled": true,
  "autoUpdate": true,
  "debug": false,
  "pruneNotification": "detailed",
  "pruneNotificationType": "chat",
  "commands": { "enabled": true, "protectedTools": [] },
  "manualMode": { "enabled": false, "automaticStrategies": true },
  "turnProtection": { "enabled": false, "turns": 4 },
  "experimental": { "allowSubAgents": false, "customPrompts": false },
  "protectedFilePatterns": [],
  "compress": {
    "mode": "range",
    "permission": "allow",
    "showCompression": false,
    "summaryBuffer": true,
    "maxContextLimit": 100000,
    "minContextLimit": 50000,
    "nudgeFrequency": 5,
    "iterationNudgeThreshold": 15,
    "nudgeForce": "soft",
    "protectedTools": [],
    "protectTags": false,
    "protectUserMessages": false
  },
  "strategies": {
    "deduplication": { "enabled": true, "protectedTools": [] },
    "purgeErrors": { "enabled": true, "turns": 4, "protectedTools": [] }
  }
}
```

**阈值含义**：
- `minContextLimit`（50K）：低于此值不提醒压缩；到达/超过后开启 turn/iteration nudge。
- `maxContextLimit`（100K）：超过后强力注入压缩提示，大幅提高压缩概率。
- 阈值支持绝对字符数或模型上下文窗口百分比（`"X%"`）。
- `summaryBuffer: true`：让 active summary 的 tokens 扩展有效的 maxContextLimit，避免摘要本身挤占可用空间。

### 1.5 Per-model 阈值覆盖

```jsonc
"modelMaxLimits": {
  "openai/gpt-5.3-codex": 120000,
  "anthropic/claude-sonnet-4.6": "80%"
},
"modelMinLimits": {
  "openai/gpt-5.3-codex": 50000,
  "anthropic/claude-sonnet-4.6": "25%"
}
```

按 `providerID/modelID` 覆盖全局阈值，适配不同模型的上下文窗口。

### 1.6 六类可编辑 Prompt（customPrompts 开启后）

| Prompt 名称 | 作用 |
|-------------|------|
| `system` | DCP 系统提示 |
| `compress-range` | range 模式压缩指令 |
| `compress-message` | message 模式压缩指令 |
| `context-limit-nudge` | 上下文限制 nudging |
| `turn-nudge` | 轮次 nudging |
| `iteration-nudge` | 迭代 nudging |

默认写入 `~/.config/opencode/dcp-prompts/defaults/`，用户可在 overrides 目录下放同名文件覆盖。

### 1.7 命令与交互

- `/dcp` — 打开 DCP TUI 面板（上下文、统计、手动模式控制）
- `/dcp-compress [focus]` — 请求模型执行一次压缩，可选 focus 文本引导压缩方向

### 1.8 Prompt Cache 影响（官方口径）

LLM 提供商基于**精确前缀匹配**缓存 prompt。DCP 剪枝内容会改变消息，从而使该点之后的缓存前缀失效。

- **Trade-off**：损失部分 cache 命中，换取 token 节省和更少的 stale context 幻觉。
- **实测**：cache hit rate 约 **85%（DCP）vs 90%（无 DCP）**。
- **无影响场景**：
  - **请求级计费**（如 GitHub Copilot）——按请求而非 token 计费
  - **统一 token 定价**（如 Cerebras）——缓存与未缓存 token 同价

### 1.9 项目状态与许可证

- **开发放缓**：README 明确公告"Development on DCP has slowed because most new context-management work has moved to Sleev"。
- **Sleev 定位**：本地代理，面向 Claude Code、Codex、OpenCode，构建于 DCP 核心思想之上，支持任意 harness/client。
- **许可证**：AGPL-3.0-or-later（⚠️ 对商业集成有传染性约束）。

---

## 2. 当前项目（anthropic_proxy.py）策略总览

本项目的代理层**没有模型可主动调用的上下文管理工具**，而是采用**被动、自动、基于字符阈值**的 8 层管线，在请求进入后端前统一处理。与 DCP 对应的是 **Layer 2 内容压缩** 和 **Layer 5 上下文截断**。

### 2.1 生命周期阶段驱动的统一阈值

所有压缩/截断力度由 `_classify_lifecycle_stage()` 根据总字符数单调递增决定：

```
chars →    15K       40K        90K        180K       350K     400K
           │         │          │           │          │        │
Stage:   INIT     GROWTH    EXPANSION   SATURATION  OOM_DANGER  PRE_TRUNC
L2+L4:   跳过    尾40%清除   尾60%+     全dynamic+   全量+     全量+
                 (无think)  think=5    think=3     think=1   think=1
L5截断:   关       关       预算触发    rounds=8   rounds=3  rounds=2
Frozen:   12       12         12          6          0         0
```

### 2.2 Layer 2：内容压缩（_compress_content_pass）

单次遍历完成：
- **错误翻译**：把后端英文错误改写为中文自然语言提示（`Wasted call` → 不要反复读取）。
- **工具内容清除**：用 `[cleared: ...]` 占位符替换旧 `tool_result` 内容。
  - 按语义评分保留高价值结果（Read/Agent 优先级高、代码/错误加分、近期 Read 额外加分）。
  - Read 结果保留前 200 字符预览，降低重读循环。
  - Frozen Zone（默认前 12 条）保护 prefix cache 稳定。
- **Thinking 剥离**：清除旧 `reasoning_content` / `thinking` blocks。
- **Bash 去重**：Jaccard ≥ 0.7 的连续 Bash 输出合并。

### 2.3 Layer 5：上下文截断（truncate_messages_if_needed）

三种策略：

| 策略 | 说明 |
|------|------|
| **rounds** | 保留最近 N 轮 assistant 对话 + HEAD，MIDDLE 压缩摘要（默认） |
| **fifo** | 保留 HEAD + TAIL 固定条数，中间直接丢弃 |
| **char** | 丢弃最旧中间消息直到总字符低于阈值 |

截断后的 MIDDLE 通过**四级压缩链**处理：
1. **增量压缩**：会话级 `_summary_cache`，只压缩新增 dropped 消息。
2. **LLM 压缩**：调用本地模型生成结构化摘要（30s 超时，失败降级）。
3. **规则压缩**：提取错误、代码状态、文件变更、决策等结构化信息。
4. **静态折叠**：`[Context folded: N messages dropped]`。

并支持：
- **Read 结果智能保留**：`rounds` 策略下，从 dropped 区间提取所有 Read tool_result 并完整保留，避免 DCP 式"清除后重读"问题。
- **关键词索引注入**：从 dropped 消息提取文件名/错误类型/函数名，匹配当前 tail 后注入相关历史。
- **严重截断通知**：丢弃比例 > 85% 时注入 `[System: Context severely truncated]`。

### 2.4 Layer 3：循环与阻塞检测（DCP 没有的主动干预层）

- **精确/模式/文本循环检测**：连续相同工具调用或相似文本触发三级干预（提示 → 移除工具 → 强制纯文本）。
- **阻塞检测**：连续 N 次相同错误类型注入 `[BLOCKER]` 提示。
- **Re-read 检测**：检测模型是否尝试读取已被清除的文件，注入 HARD BLOCK。

### 2.5 工具定义过滤（Layer 6）

44 个工具 → 通过白名单 + 最近使用扫描压缩到约 15 个，节省 5-8K tokens。

---

## 3. 维度对比

### 3.1 触发机制

| 维度 | DCP | 当前项目代理层 |
|------|-----|----------------|
| **触发主体** | 模型主动调用 `compress` 工具 + 自动策略 | 代理层被动扫描，按字符阈值自动触发 |
| **触发时机** | 基于任务完成度 + 上下文压力 nudge | 基于总字符数生命周期阶段 |
| **可控性** | 高，模型决定压缩什么、何时压缩 | 低，完全由代理层规则决定 |
| **用户干预** | 支持 `/dcp-compress [focus]` 手动触发 | 无手动触发入口 |

### 3.2 压缩粒度与摘要质量

| 维度 | DCP | 当前项目代理层 |
|------|-----|----------------|
| **粒度** | range（连续区间）或 message（单条） | rounds（最近 N 轮）/ fifo / char |
| **摘要生成** | 模型调用 compress 时自生成摘要 | 四级压缩链：增量 → LLM → 规则 → 折叠 |
| **信息保留** | 嵌套摘要 + protected tools/files + `<protect>` 标签 | Read 结果智能保留 + 关键词索引注入 |
| **用户消息** | 可选 `protectUserMessages` 完整保留 | 无特殊保护，按 Frozen Zone 和策略处理 |
| **摘要稳定性** | 压缩块 ID 稳定，可嵌套 | 摘要内容可能随每次请求重新生成 |
| **摘要空间** | `summaryBuffer` 让摘要 tokens 扩展上限 | 无类似机制，摘要在预算内竞争 |

### 3.3 历史完整性与可审计性

| 维度 | DCP | 当前项目代理层 |
|------|-----|----------------|
| **原始历史** | **永不修改**，只转换出站副本 | 代理层内部修改后转发，原始历史不由代理保存 |
| **回退能力** | 强，稳定 message/block 引用 | 弱，摘要一旦生成无法自动展开 |
| **可观测性** | 压缩通知可显示在 chat 或 toast；debug 日志 | 仅日志 + metrics.jsonl + status 页面 |

### 3.4 工具相关处理

| 维度 | DCP | 当前项目代理层 |
|------|-----|----------------|
| **重复工具去重** | 自动 deduplication（same tool + same args），compress 时重算 | Bash 输出 Jaccard 合并（有限） |
| **错误输入清理** | Purge Errors（N 轮后只保留错误消息） | 错误翻译为中文 + 无专门 purge |
| **Write 输入清理** | 无专门策略（但 protectedFilePatterns 可保护） | 无专门策略 |
| **Protected tools** | 默认保护 10 个核心工具，可配置 | Frozen Zone 保护前 N 条消息中的 tool_result |
| **重读循环处理** | 依赖 protected file patterns + turnProtection | Re-read 检测 + HARD BLOCK + 200 字符 Read 预览 |
| **文件模式保护** | `protectedFilePatterns`（glob 匹配 filePath） | 无 |

### 3.5 循环/阻塞检测

| 维度 | DCP | 当前项目代理层 |
|------|-----|----------------|
| **循环检测** | 无专门机制 | 精确 + 模式 + 文本三级检测，支持 escalating intervention |
| **阻塞检测** | 无专门机制 | 连续相同错误类型注入 `[BLOCKER]` |
| **干预强度** | 无 | Level 1 提示 → Level 2 移除工具 → Level 3 强制纯文本 |

### 3.6 Prompt Cache 影响

| 维度 | DCP | 当前项目代理层 |
|------|-----|----------------|
| **Cache 策略** | 承认 cache 会失效，以 token 节省换 cache hit；压缩时一起重算 dedup/purge | 专门设计 Frozen Zone + 日期标准化保持 prefix cache 稳定 |
| **实测影响** | 约 85% cache hit（无 DCP 时 90%） | 未给出绝对值，但强调 Frozen Zone 和日期占位对 cache 的保持作用 |
| **权衡** | 牺牲部分 cache，换取更智能的压缩 | 牺牲压缩激进程度，换取 cache 稳定性和可预测性 |
| **计费模型适配** | 明确区分请求级计费/统一定价无影响 | 未涉及（本地后端无计费） |

### 3.7 配置灵活度

| 维度 | DCP | 当前项目代理层 |
|------|-----|----------------|
| **配置层级** | 全局 + 项目级 + 环境变量覆盖 | 单一 `configs/active.conf` 环境变量 |
| **模型特定阈值** | 支持 `modelMaxLimits` / `modelMinLimits`（按 providerID/modelID） | 仅区分 local/cloud 两套默认值 |
| **Prompt 覆盖** | 支持 `experimental.customPrompts` 自定义 6 类提示 | 无提示自定义能力 |
| **手动模式** | `manualMode.enabled` 可关闭自动策略，`automaticStrategies` 控制自动清理 | 无手动模式 |
| **工具保护** | `compress.protectedTools` + `strategies.*.protectedTools` + `protectedFilePatterns` | `PROXY_FROZEN_HEAD` + `PROXY_TOOL_KEEP` |
| **通知可配置** | `pruneNotification`（off/minimal/detailed）+ `pruneNotificationType`（chat/toast） | 无通知机制（对客户端透明） |

### 3.8 适用后端差异

| 维度 | DCP | 当前项目代理层 |
|------|-----|----------------|
| **目标场景** | 云 API（OpenAI/Anthropic/OpenCode 云端） | 本地后端（rapid-mlx / llama-server）为主，支持云转发 |
| **上下文压力** | 大上下文窗口（1M+ tokens），需要智能瘦身 | 小上下文窗口（~128K tokens），需要防止 OOM |
| **并发** | 由云服务商处理 | 本地默认 `PROXY_MAX_CONCURRENT=1` 防止 Metal OOM |
| **计费敏感性** | 高（token 计费场景收益显著） | 无（本地推理无 token 成本） |

---

## 4. 策略矩阵总览

```
                        DCP          Proxy
                        ───          ─────
模型驱动压缩             ✅            ❌ (规则驱动)
LLM 生成摘要             ✅ (大模型)    ✅ (小模型/规则 fallback)
规则生成摘要             ❌            ✅
Tool-result 清除         ❌            ✅ (语义评分)
Thinking 块剥离          ❌            ✅
消息截断                 ❌            ✅ (3 策略)
Frozen Zone (前缀保护)   ❌            ✅
去重 (工具调用级)        ✅            ⚠️ (仅 Bash Jaccard + 请求级 MD5)
错误输入清理             ✅ (purgeErrors) ✅ (错误翻译，无 purge)
Blocker 主动干预         ❌            ✅ (中文提示 + 解决方案)
生命周期阶段感知         ❌            ✅ (5 阶段)
工具定义过滤             ❌            ✅
文本循环检测             ❌            ✅
OOM 安全防护             ❌            ✅ (硬上限)
会话续接检测             ❌            ✅
Nudge/提示注入           ✅            ❌
Per-model 阈值覆盖       ✅            ❌
Protected tag (<protect>) ✅           ❌
Protected file patterns  ✅ (glob)     ❌
summaryBuffer (摘要扩展) ✅            ❌
嵌套摘要                 ✅            ❌
手动模式                 ✅            ❌
自定义 Prompt            ✅ (6 类)     ❌
TUI 面板/命令            ✅ (/dcp)     ❌
通知系统                 ✅ (chat/toast) ❌ (仅日志)
非破坏性历史             ✅            ❌ (in-place mutation)
许可证                   AGPL-3.0      N/A (内部项目)
```

---

## 5. 优劣势总结

### 5.1 DCP 优势

1. **模型驱动的压缩更智能**：模型知道哪些任务已完成、哪些内容不再需要，摘要针对性更强。
2. **非破坏性历史**：原始会话永远完整，便于审计、调试和回退。
3. **嵌套摘要避免信息稀释**：多次压缩不会把旧摘要压成无意义碎片。
4. **自动策略覆盖常见浪费**：deduplication、purge errors 都是零成本收益。
5. **可配置程度高**：支持模型特定阈值、自定义 prompt（6 类）、手动模式、文件模式保护、`<protect>` 标签、`summaryBuffer`。
6. **用户可见性**：通知系统（chat/toast）让用户感知压缩发生。

### 5.2 DCP 劣势

1. **依赖模型配合**：如果模型不主动调用 compress，效果会打折（nudge 可缓解但非根治）。
2. **破坏 prefix cache**：每次压缩都改变后续前缀，本地后端对 cache 敏感时代价高（85% vs 90%）。
3. **不解决本地后端 OOM**：它优化的是 token 量，不是并发或峰值内存。
4. **无循环/阻塞干预**：重复错误和工具循环依赖模型自身或上游客户端处理。
5. **开发放缓**：新功能转移到 Sleev，DCP 维护模式。
6. **AGPL-3.0 许可证**：对商业集成有传染性约束。

### 5.3 当前项目代理层优势

1. **本地后端适配强**：生命周期阶段、Frozen Zone、并发控制、输出截断都是为了 rapid-mlx/llama-server 的稳定性设计。
2. **积极的循环/阻塞干预**：三层循环干预 + Blocker 检测 + Re-read HARD BLOCK，能打断模型自陷。
3. **Read 结果智能保留**：`rounds` 策略下完整保留 dropped 区间的 Read 输出，避免"清除 → 重读"死循环。
4. **Cache 友好**：日期标准化、Frozen Zone 保护前缀，适合本地 prefix cache。
5. **无需模型配合**：所有压缩/截断在代理层静默完成，对客户端透明。
6. **OOM 安全**：多层硬上限（`PROXY_OOM_SAFE_CHARS`/`PROXY_OOM_SAFE_TOKENS`），云 API 不需要但本地必需。

### 5.4 当前项目代理层劣势

1. **压缩被动粗糙**：按字符阈值一刀切，容易在不必要的时候压缩，或该压缩时不够精细。
2. **摘要质量依赖 LLM/规则**：LLM 压缩有 30s 超时和失败降级，规则压缩信息密度有限。
3. **历史不可回退**：代理层修改后的消息直接发给后端，没有稳定的 block ID 供引用。
4. **无自动 dedup/purge errors/supersede writes**：重复工具输出和旧错误输入不会自动清理。
5. **用户无法手动干预**：没有 `/compress` 或类似入口。
6. **无 per-model 阈值**：9B vs 35B vs 云模型用同一套阈值（仅区分 local/cloud）。
7. **无通知机制**：用户对压缩无感知。

---

## 6. 战略维度：Sleev 迁移的影响

### 6.1 Sleev 是什么（闭源商业产品）

DCP README 公告：新功能开发已转移到 [Sleev](https://sleev.ai)。经查证 Sleev 官网与文档，**Sleev 是闭源商业产品，非开源**：

| 证据 | 说明 |
|------|------|
| **强制账号登录** | 首次运行 `sleev` 必须登录 Sleev 账户，关联本地 gateway 到账户体系 |
| **定价页** | Free（100 req/day）+ Pro（$20/月，beta 期免费），有明确的商业化路径 |
| **网关二进制分发** | CLI 下载后首次运行才拉取匹配版本的 gateway 二进制，**不提供源码** |
| **Offline Licensing** | 受限网络环境需"审批"获取离线许可证，典型商业 SaaS 模式 |
| **Dashboard 账户体系** | 用量/指标上报到 Sleev 账户（对话历史声称留本地，但元数据上报） |
| **许可证** | 未公开开源许可证，© 2026 Sleev. All rights reserved. |

**对比 DCP**：DCP 是 AGPL-3.0-or-later 开源插件；Sleev 是闭源商业代理。"新功能转移到 Sleev"实际意味着**从开源转向闭源商业**。

定位：
- **本地代理**（local proxy，gateway 在用户机器上运行）
- 面向 **Claude Code、Codex、OpenCode、ChatGPT、GitHub Copilot、Kimi、Z.AI**
- 构建于 DCP 核心思想之上，新增 prompt cache 保留、上下文压缩
- 安装：`curl -fsSL https://sleev.ai/install.sh | bash` 或 `npm install -g sleev`

### 6.2 与本项目的关系

| 维度 | 本项目 anthropic_proxy.py | Sleev |
|------|--------------------------|-------|
| 定位 | 本地代理，Anthropic→OpenAI 翻译 | 本地代理，context management |
| 目标客户端 | Claude Code（通过 Anthropic SDK） | Claude Code、Codex、OpenCode 等多客户端 |
| 核心能力 | 协议翻译 + 上下文压缩 + 循环检测 + OOM 防护 | DCP 式模型驱动压缩 + 自动清理 + prompt cache 保留 |
| 后端 | rapid-mlx / llama-server / 云 API | 任意（harness 无关，支持 Anthropic/OpenAI/Codex/Kimi/Moonshot/opencode-go） |
| 成熟度 | 内部项目，v0.5.x | 商业产品，v1.4.x（已发布） |
| **开源** | 内部项目（未开源） | **闭源商业**（© All rights reserved） |
| **隐私** | 全本地，无账户 | gateway 本地，但需登录账户 + 元数据上报 Dashboard |
| **成本** | 免费（本地推理） | beta 免费，后续 $20/月（Pro） |

**关键判断**：
- Sleev 与本项目在"本地代理"这一定位上**直接重叠**，但商业模式、隐私模型、后端适配深度不同。
- Sleev 的优势是继承了 DCP 的模型驱动压缩思路 + 商业化维护 + 多客户端支持；本项目的优势是**全本地无账户、深度适配 Apple Silicon 本地后端**（OOM 防护、Frozen Zone、Metal 并发控制）、无外部依赖。
- Sleev 不开源意味着**无法复用其代码**，只能参考其公开文档描述的思路。

### 6.3 可选路线（更新）

1. **观望（推荐）**：Sleev 闭源 + 需账户 + 未来收费，与本项目"全本地、无外部依赖"的定位冲突。继续关注其公开文档描述的策略思路，但**不集成、不依赖**。
2. **吸收思路（可行）**：将 DCP（开源 AGPL-3.0）的模型驱动压缩、deduplication、purgeErrors、嵌套摘要等思路**独立实现**到 `anthropic_proxy.py`，保持本地后端适配优势。⚠️ 注意：**不可直接复制 DCP 代码**（AGPL 传染性），只能参考思路独立实现。
3. **分层协作（不推荐）**：让 Sleev 作为上游 context-management 层。**问题**：Sleev 需账户登录 + 元数据上报 + 未来收费，违背本项目"全本地、无外部依赖"原则；且 Sleev 网关是闭源二进制，无法审计或定制。
4. **替代评估（可选）**：若未来本项目维护成本过高，可评估 Sleev Pro 是否覆盖本地后端场景——但当前 Sleev 文档未提及 Apple Silicon / Metal / OOM 适配，主要面向云 API 节省 token 账单，与本项目"本地推理无 token 成本"的场景**不匹配**。

> ⚠️ **结论修正**：首版文档建议"评估 Sleev 集成"，经查证 Sleev 为闭源商业产品后，**不建议集成 Sleev**。应坚持吸收 DCP 开源思路独立实现，保持本项目的全本地、无依赖、Apple Silicon 深度适配优势。

---

## 7. 竞品格局：类似产品调研

基于 GitHub 搜索（2026-06-18），LLM 上下文压缩/管理代理领域已出现多款产品。按定位和成熟度分类如下。

### 7.1 产品矩阵

| 产品 | 仓库 | 许可证 | 语言 | Stars | 定位 | 压缩方式 |
|------|------|--------|------|-------|------|----------|
| **DCP** | Opencode-DCP/opencode-dynamic-context-pruning | AGPL-3.0 | TS | — | OpenCode 插件 | 模型驱动 compress 工具 + 自动 dedup/purge |
| **Sleev** | sleev.ai（闭源） | 闭源商业 | — | — | 本地代理（多客户端） | DCP 思路 + prompt cache 保留 |
| **Kompact** | npow/kompact | MIT | Python | 3 | 透明 HTTP 代理 | 8 变换管线（TF-IDF/JSON/code/observation mask/cache aligner） |
| **Headroom** | PyPI `headroom-ai`（仓库未公开/404） | Apache-2.0 | Python | — | 透明代理 + GUI/gateway 生态 | SmartCrusher（prose 专用）+ code-aware + memory |
| **TokenSieve** | david-spies/tokensieve | MIT | Go | 0 | 企业级有状态缓存代理 | SHA-256 指纹 dedup（file/trace/blob/system prompt） |
| **LLMLingua-2** | microsoft/LLMLingua | MIT | Python | 6.3k | 学术 prompt 压缩库 | 小模型（BERT/GPT2）token 分类剪枝 |
| **claude-code-memory** | AbdoKnbGit/claude-code-memory | — | Python | 5 | Claude Code 跨会话记忆 | 持久存储 + 上下文注入（非实时压缩） |
| **ZeroWeight** | josetrejor-cmyk/ZeroWeight | — | — | 0 | prompt 端压缩 + 输出精简 | Headroom 风格 + 输出端约束 |

### 7.2 重点产品详评

#### 7.2.1 Kompact（npow/kompact）— 最接近的可借鉴开源产品

- **发布**：PyPI `kompact`，v0.4.0（2026-05-14），`pip install kompact`，30 秒启动
- **压缩率**：40-70% token 节省（BFCL 工具 schema 55.3%，Glaive 工具调用 56.6%）
- **质量影响**：在 BFCL 基准上 Claude Haiku/Sonnet/Opus 质量下降仅 -2.6%/-3.9%/-0.5%（优于 LLMLingua-2 的 -23%/-20%/-27%）
- **8 变换管线**（与本项目 8 层管线高度对标）：
  1. Schema Optimizer（TF-IDF 选择）
  2. Content Compressors（TOON、JSON、code）
  3. Extractive Compress（TF-IDF 句子）
  4. Observation Masker（历史管理）
  5. Cache Aligner（**prefix caching 保留**——与本项目 Frozen Zone 同一目标）
- **可配置**：`X-Kompact-Disable` 头按请求禁用特定变换
- **可观测**：OpenTelemetry + Prometheus + Grafana 内置
- **关键启示**：Kompact 的 **Cache Aligner** 与本项目 **Frozen Zone** 解决同一问题（保留 prefix cache），值得对比实现思路。Kompact 的 **Schema Optimizer（TF-IDF）** 与本项目 **_filter_tools（白名单 + 最近使用）** 也是同一目标的两条路径——TF-IDF 更通用，白名单更可控。

#### 7.2.2 Headroom-ai（chopratejas/headroom）— 生态最丰富，★31,630

> **2026-06-18 更新**：经 P2 深度研究确认，Headroom-ai 主仓库为 `chopratejas/headroom`（非 `headroom-ai/headroom`），
> 已公开，★31,630，Apache-2.0。详见 §12.1 深度分析。下文为初版调研内容，部分信息已过时。

- **分发**：PyPI `headroom-ai[code,memory]` v0.26.0，Apache-2.0，[chopratejas/headroom](https://github.com/chopratejas/headroom)
- **特性**：`--learn --code-aware --memory`，tree-sitter 代码感知（ast-grep-cli），hnswlib + sentence-transformers 语义记忆
- **压缩率**：60-95%（工具输出/日志/RAG/代码场景）；BFCL 97% 精度 + 32% 压缩（工具调用不破坏）
- **生态**：已有第三方 GUI（aminechraibi/headroom-gui）、K8s 网关（yossiovadia/headroom-gateway）、9router 组合（magicpro97/headroom-9router-combo）
- **模式**：Library (`compress(messages)`) / Proxy (`headroom proxy --port 8787`) / Agent wrap (`headroom wrap claude`) / MCP server
- **核心算法**：CacheAligner（前缀稳定）+ ContentRouter（内容路由）+ SmartCrusher（JSON）+ CodeCompressor（AST）+ Kompress-base（ML 文本）+ CCR（可逆压缩）
- **关键启示**：Headroom 的 **CCR 可逆压缩**是本项目最值得借鉴的方向——存储原始内容，模型可按需检索，避免死亡循环。**ContentRouter 内容路由**让压缩策略按内容类型智能选择。两者均可在 stdlib 约束下实现。详见 §12.1。

#### 7.2.3 TokenSieve（david-spies/tokensieve）— Go 实现的企业级去重代理

- **定位**：与本项目最相似的"企业级有状态缓存代理"，但用 Go 写
- **压缩策略**：纯 **SHA-256 指纹去重**（whole-message / file-block / stack-trace / large-blob / system-prompt），不做摘要压缩
- **性能**：<1ms 延迟，>10k req/s，~50MB 内存
- **关键启示**：TokenSieve 的 **file-block 指纹去重**（识别 `--- BEGIN FILE`、`File:`、`<file path=` 标记）是本项目**没有的**——本项目按 tool_result 整体清除，TokenSieve 按**文件块**去重，粒度更细。但 TokenSieve **不做摘要**，只去重，无法应对"内容演化"场景（同一文件多次修改后内容不同）。

#### 7.2.4 LLMLingua-2（microsoft/LLMLingua）— 学术方案，质量风险高

- **定位**：学术界最知名的 prompt 压缩库，6.3k stars，EMNLP'23/ACL'24
- **方法**：用小模型（GPT2-small / BERT）做 token 分类，剪掉"非必要"token，最高 20x 压缩
- **质量风险**：Kompact 基准显示 LLMLingua-2 在工具调用场景**破坏 schema**（-20% 到 -27% 质量），因为它不理解 JSON 结构
- **关键启示**：LLMLingua-2 的**小模型压缩**思路在理论上是本项目 LLM 压缩的替代方案（用更小的模型做压缩，避免占用主后端），但**质量风险太高**，尤其对工具调用密集的 agentic 场景。不适合本项目。

### 7.3 与本项目的定位对比

| 维度 | 本项目 | DCP | Sleev | Kompact | Headroom-ai | TokenSieve | LLMLingua-2 |
|------|--------|-----|-------|---------|-------------|------------|-------------|
| **目标后端** | 本地（rapid-mlx/llama-server） | 云 | 云 | 云 | 云 | 云 | 任意（库） |
| **协议翻译** | ✅ Anthropic→OpenAI | ❌ | ✅ | ✅ | ✅ | ✅ | ❌（库） |
| **OOM 防护** | ✅（Apple Silicon） | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Frozen Zone** | ✅ | ❌ | ✅ | ✅(Cache Aligner) | ✅(CacheAligner) | ❌ | ❌ |
| **循环/阻塞检测** | ✅ | ❌ | ❌ | ❌ | ✅(headroom learn) | ❌ | ❌ |
| **模型驱动压缩** | ❌ | ✅ | ✅ | ❌ | ✅(Kompress-base) | ❌ | ✅(小模型) |
| **自动去重** | ⚠️(Bash Jaccard) | ✅ | ✅ | ✅ | ✅(cross-agent) | ✅(SHA-256) | ❌ |
| **摘要压缩** | ✅(LLM/规则) | ✅(模型生成) | ✅ | ✅(TF-IDF/JSON) | ✅(SmartCrusher+CCR) | ❌(仅去重) | ✅(小模型) |
| **可逆压缩** | ❌([cleared]占位符) | ❌ | ? | ❌ | ✅(CCR) | ❌ | ❌ |
| **内容路由** | ❌(固定8层) | ❌ | ? | ⚠️(按变换) | ✅(ContentRouter) | ❌ | ❌ |
| **AST 代码压缩** | ❌ | ❌ | ? | ❌ | ✅(CodeCompressor) | ❌ | ❌ |
| **依赖** | stdlib only | npm | npm | pip | pip+torch+ts | Go | pip+torch |
| **开源** | 内部 | ✅ AGPL | ❌闭源 | ✅ MIT | ✅ Apache-2.0 (★31K) | ✅ MIT | ✅ MIT |

### 7.4 对本项目的启示

1. **本项目是唯一面向本地后端的代理**：所有竞品都面向云 API 节省 token 账单，**没有一个**解决 Apple Silicon 本地后端的 OOM、Metal 并发、prefix cache 持久化问题。这是本项目的**独特价值**，不应被竞品的存在否定。

2. **可借鉴的具体技术**（注意许可证）：
   - **Headroom-ai 的 CCR 可逆压缩**：存储原始 tool_result，模型可按需检索——**解决死亡循环的关键**（Apache-2.0，可在 stdlib 下实现）
   - **Headroom-ai 的 ContentRouter**：按内容类型选最优压缩策略（Apache-2.0，可在 stdlib 下实现）
   - **Kompact 的 Cache Aligner**：对比本项目 Frozen Zone，可能有更优的 prefix cache 对齐算法（MIT 许可，可参考思路）
   - **Kompact 的 Schema Optimizer（TF-IDF）**：作为本项目 `_filter_tools` 白名单的替代/补充思路（MIT）
   - **TokenSieve 的 file-block 指纹去重**：比本项目整块 tool_result 清除更细粒度（MIT）
   - **Headroom-ai/Skim 的 tree-sitter code-aware**：按 AST 压缩代码内容（Apache-2.0/MIT，但 tree-sitter 依赖重，可用 Python `ast` 模块替代）
   - **⚠️ 不可借鉴**：DCP 的 compress 工具实现（AGPL-3.0 传染性）、Sleev（闭源）

3. **竞品成熟度分化**：Headroom-ai（★31,630）和 LLMLingua-2（★6,308）已成熟，DCP 有完整实现但 AGPL。Kompact/TokenSieve/Skim/Token Reducer 是早期项目（★27-28），但源码可读、策略可参考。本项目在本地后端适配维度**无直接竞品**。

4. **市场趋势**：2026 年 5-6 月集中出现 Kompact、Headroom 生态、TokenSieve、Sleev 等多款产品，说明"LLM 上下文压缩代理"正在成为独立赛道。本项目应明确**本地后端深度适配**的差异化定位，而非与云 API 节账单的竞品正面竞争。

---

## 8. 可借鉴的改进点

> 完整实现代码见 `docs/04-analysis-diagnostics/dcp-vs-proxy-context-compression.md` §5，此处仅列要点与本次新增项。

### 7.1 短期可落地（无需架构大改）

| 改进点 | 来源 | 预期收益 | 实现难度 |
|--------|------|----------|----------|
| **自动 Deduplication** | DCP strategies.deduplication | 减少 5-20% 上下文（反复 Read/Bash 场景） | 低（在 `_compress_content_pass` Phase 2a 前加预处理） |
| **Purge Errors** | DCP strategies.purgeErrors | 清理旧错误输入的大段堆栈 | 低（同上插入位置） |
| **Supersede Writes** | DCP 思路扩展 | 文件被后续 Read 后，旧 Write 输入可剪枝 | 中（需两遍扫描） |
| **Per-Config 阈值覆盖** | DCP modelMaxLimits | 9B vs 35B vs 云模型用不同阈值 | 低（configs 中加 JSON + 解析函数） |
| **`<protect>` 标签支持** | DCP compress.protectTags | 用户精确控制不可压缩内容 | 低（`_compress_content_pass` 检测标签跳过） |

### 7.2 中期改进（需要新增能力）

| 改进点 | 来源 | 预期收益 | 实现难度 |
|--------|------|----------|----------|
| **模型可调用的 compress 工具** | DCP compress 工具 | 模型主动决定何时压缩，摘要更精准 | 高（需在工具列表注册 + 代理层拦截处理） |
| **压缩块 ID 与占位** | DCP block ID | 摘要可引用、可审计 | 中（在 `_apply_rounds_truncation` 包装） |
| **嵌套摘要** | DCP range 嵌套 | 多次压缩不稀释信息 | 中（修改 `_compress_middle_with_llm` 传入历史摘要） |
| **summaryBuffer 机制** | DCP compress.summaryBuffer | 让摘要 tokens 不挤占可用预算 | 中（调整 token budget 计算） |
| **阶段感知 Nudge** | DCP nudge | SATURATION 阶段引导模型收尾 | 中（注入提示，风险：干扰 Qwen 推理） |

### 7.3 需要谨慎评估的改进

| 改进点 | 风险 | 建议 |
|--------|------|------|
| **完全转向 DCP 式模型驱动压缩** | 本地模型（Qwen 3.6 4bit）可能不善于判断何时压缩，且破坏 prefix cache | 先作为可选策略，保留现有阈值触发作为 fallback |
| **关闭 Frozen Zone 追求更高压缩率** | 显著降低 prefix cache 命中率，增加本地后端负载 | 保持 Frozen Zone，仅在高阶段动态缩小 |
| **引入 protectUserMessages** | 本地模型上下文紧张，大段用户粘贴内容不压缩可能导致 OOM | 默认关闭，仅在小上下文模型配置中开启 |
| **照搬 DCP nudge 到本地后端** | nudge 增加请求文本量，对 128K 上下文模型可能负收益 | 仅在云模式或模型驱动 compress 工具开启时启用 |
| **集成 Sleev** | **闭源商业**（需账户、未来收费、网关二进制不可审计）+ AGPL-3.0 传染性（DCP 部分）+ 项目成熟度未知 | **不推荐集成**。坚持吸收 DCP 开源思路独立实现 |

---

## 9. 结论

- **DCP 更适合大上下文、云 API 场景**：它把上下文管理权部分交给模型，通过非破坏性出站转换实现智能压缩，牺牲部分 prompt cache（85% vs 90%）以换取 token 效率。
- **当前项目代理层更适合本地小上下文后端**：以稳定性为第一目标，采用被动阈值触发、强循环/阻塞干预、cache 友好的 Frozen Zone，但压缩粒度较粗、缺乏自动 dedup/purge 等精细化策略。
- **Sleev 是新的战略变量**：DCP 作者已转向构建本地代理 Sleev，与本项目的定位重叠。但 Sleev **闭源商业**（需账户、未来收费、网关二进制分发），与本项目"全本地、无外部依赖"定位冲突，**不建议集成**，应坚持吸收 DCP 开源思路独立实现。
- **最佳实践不是二选一**：当前项目可以吸收 DCP 的 `deduplication`、`purgeErrors`、`supersedeWrites`、`<protect>` 标签、`summaryBuffer`、per-model 阈值等自动策略作为短期改进，以及模型可调用的 `compress` 工具、嵌套摘要作为中期补充，同时保留现有的生命周期阶段、Frozen Zone 和循环检测作为安全网。这样既能提升长会话的 token 效率，又能维持本地后端的稳定性和 cache 命中率。

---

## 10. P1 竞品源码深度分析（2026-06-18 补充）

> 本节基于对 Kompact 和 TokenSieve **完整源码**的逐行阅读，验证 §7/§8 中基于 README 推断的策略细节。
> 两项目均为 **MIT 许可**，思路可参考，但本项目坚持标准库实现，不引入其代码依赖。

### 10.1 Kompact（MIT）— 8 变换管线源码分析

> 仓库：`npow/kompact`（Python，~3 stars，早期阶段）
> 核心目录：`src/kompact/transforms/`（11 个文件）、`src/kompact/cache/`、`src/kompact/proxy/`

#### 10.1.1 管线架构（`pipeline.py`）

Kompact 的核心是一个 **4 层、8 变换**的有序管线，每个变换独立可开关：

```
Layer 1: Schema Optimizer    — 工具定义 TF-IDF 选择（对标 _filter_tools）
Layer 2: Content Compressors — 4 个内容压缩器顺序执行：
         2a. TOON             — 最大变换（16KB），核心创新
         2b. JSON Crusher      — JSON 结构压缩
         2c. Code Compressor   — 代码块压缩
         2d. Log Compressor    — 日志压缩
Layer 2b: HTML Stripper       — WebFetch 导航/chrome 剥离
Layer 2c: Content Compressor  — 抽取式文本压缩（prose/长文本）
Layer 3: Observation Masker   — 历史工具输出遮蔽（对标 tool_result clearing）
Layer 4: Cache Aligner        — 前缀缓存对齐（对标 Frozen Zone + date normalization）
```

**自适应参数调节**（`_adapt_params`）：根据总 token 估算动态调整压缩强度：

| 上下文大小 | content_compressor.target_ratio | observation_masker.keep_last_n |
|------------|--------------------------------|-------------------------------|
| <500 tokens | 禁用内容压缩 | — |
| 2K-20K | 0.75（保守） | max(5, n_messages//3) |
| 20K-100K | 0.60（平衡） | max(4, n_messages//4) |
| 100K+ | 0.45（激进） | max(3, n_messages//5) |

> **对比本项目**：本项目有类似的 5 阶段生命周期（INIT/GROWTH/EXPANSION/SATURATION/OOM_DANGER），但阶段切换是**离散阈值**，Kompact 是**连续线性插值**。Kompact 的自适应更平滑，但本项目的阶段感知能触发更多联动行为（如 SATURATION 阶段启用 Loop Guard、OOM_DANGER 启用 Truncate）。

#### 10.1.2 Schema Optimizer（`schema_optimizer.py`）— 对标 `_filter_tools`

**核心算法**：TF-IDF 余弦相似度工具选择。

```python
# 简化伪代码
query = " ".join(msg.text for msg in messages[-5:])  # 最近 5 条消息作为查询
idf = _compute_idf(tools)  # 每个工具作为一个"文档"计算 IDF

for tool in tools:
    tfidf_score = _tfidf_cosine(query_terms, tool_terms, idf)
    recent_boost = 0.5 if tool in recently_used else 0.0
    score = tfidf_score + recent_boost

# 保留 top-K + 最近使用的工具
selected = sorted(scored, reverse=True)[:max_tools]
selected += [t for t in tools if t.name in recently_used and t not in selected]
```

**关键设计细节**：
- 工具文本化：`tool.name + tool.description + 所有参数名 + 参数描述` 拼成一个"文档"
- IDF 计算：`math.log(total_docs / (1 + doc_count))`，标准 IDF 公式
- 最近使用加成：最近 3 条消息中用过的工具 +0.5 分数 boost
- 最后一条 assistant 消息中用过的工具**强制保留**（确保工具调用链不断裂）
- token 估算：`len(json.dumps(tool.raw)) // 4`（4 字符≈1 token）

> **对比本项目 `_filter_tools`**：
> | 维度 | 本项目 `_filter_tools` | Kompact Schema Optimizer |
> |------|----------------------|--------------------------|
> | 选择策略 | **硬编码白名单**（16 个核心工具）+ 最近 N 轮使用工具 | **TF-IDF 语义相关性** + 最近使用 boost |
> | 智能程度 | 低（固定列表，不随对话内容变化） | 中（根据当前对话主题动态选择） |
> | 强制保留 | 白名单 + 最近使用 | 最后一条 assistant 使用的工具 |
> | 计算开销 | O(1) 查表 | O(n_tools × n_terms) TF-IDF 计算 |
> | 适用场景 | 工具数量固定、对话主题变化小 | 工具数量多、对话主题跨度大 |
>
> **可借鉴点**：本项目当前用硬编码白名单（`Read/Write/Edit/Bash/Glob/Grep/...` 16 个），对于 agentic coding 场景足够。但如果未来工具数量增长到 50+，TF-IDF 语义选择比固定白名单更灵活。**短期不需要引入**，但可作为 `_filter_tools` 的可选增强策略（`PROXY_TOOL_FILTER_STRATEGY=whitelist|tfidf`）。

#### 10.1.3 Cache Aligner（`cache_aligner.py`）— 对标 Frozen Zone + date normalization

**核心策略**：将系统提示和早期消息中的**动态内容**替换为稳定占位符，最大化前缀缓存命中率。

```python
# 四类动态内容正则模式
UUID_PATTERN     = r"[0-9a-f]{8}-[0-9a-f]{4}-..."      # UUID
TIMESTAMP_PATTERN = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}..."  # ISO 时间戳
UNIX_TS_PATTERN  = r"\b1[6-9]\d{8}\b"                   # Unix 时间戳 (2020-2033)
ABS_PATH_PATTERN = r"(?:/[\w.-]+){3,}"                  # /foo/bar/baz 路径

# 替换策略
text = text.replace(uuid_val, "{{UUID_0}}")
text = text.replace(ts_val, "{{TS_0}}")
# 路径仅替换包含 /Users/ /home/ /tmp/ 的用户特定路径
text = text.replace(path_val, "{{PATH_0}}")
```

**作用范围**：仅处理 `system` 消息 + 前 2 条 `user` 消息（系统注入的上下文），不处理后续对话。

**缓存收益声明**：Anthropic 90% discount on cached input tokens，OpenAI 50% discount。

> **对比本项目**：
> | 维度 | 本项目 | Kompact Cache Aligner |
> |------|--------|----------------------|
> | 动态内容归一化 | **日期归一化**（`_normalize_dates`：将 `2026-06-18` 替换为 `{{DATE}}`） | **4 类模式**：UUID/时间戳/Unix TS/用户路径 |
> | 作用范围 | 整个消息列表 | 仅 system + 前 2 条 user |
> | 占位符还原 | 不还原（直接发归一化后的文本） | 不还原（`dynamic_values` 存入 details 但不回填） |
> | Frozen Zone | **有**（前 N 条消息不压缩不截断，保证前缀稳定） | **无显式 Frozen Zone**，靠归一化保持前缀稳定 |
>
> **可借鉴点**：
> 1. **UUID/路径归一化**：本项目目前只做日期归一化。Claude Code 的 system prompt 中包含 session UUID 和 `/Users/.../workspace` 路径，这些每次会话都变，严重影响前缀缓存。**值得直接借鉴**，在 `_normalize_dates` 旁加 `_normalize_uuids` 和 `_normalize_paths`。
> 2. **作用范围限制**：Kompact 只处理前 2 条消息，避免误改对话中的用户输入。本项目 Frozen Zone 已有类似保护，但归一化目前可能作用于全部消息——应确认归一化也限制在 Frozen Zone 内。
> 3. **⚠️ 路径归一化的风险**：对于 agentic coding，文件路径是关键信息。将 `/Users/jinsongwang/APP/llama.cpp/anthropic_proxy.py` 归一化为 `{{PATH_0}}` 可能导致模型无法引用文件路径。**不能照搬**，应仅归一化 system prompt 中的工作目录路径，不归一化对话中 Read/Edit 的文件路径。

#### 10.1.4 Observation Masker（`observation_masker.py`）— 对标 tool_result clearing

**核心策略**：将旧工具输出替换为紧凑占位符，仅保留最近 N 条完整输出。

```python
# 保留最近 N 条 tool_result，其余替换为占位符
positions_to_mask = tool_result_positions[:-keep_last_n]

# 占位符格式
"[Output omitted — 1500 tokens. Starts with: <first 100 chars>]"
# 如果 include_summary=True，包含原文本第一行前 100 字符作为简要上下文

# 原始内容存入 store（可检索回填）
store.put(key=tool_use_id, content=block.text, metadata={"tool_name": ...})
```

**设计细节**：
- 基于 JetBrains Mellum 研究：~50% cost reduction with minimal accuracy impact
- 占位符包含 token 数量信息（`1500 tokens`），让模型知道省略了多少
- 可选包含首行摘要（`include_summary`），提供极简上下文
- 原始内容存入 `cache/store.py`，支持按 `tool_use_id` 检索回填

> **对比本项目 tool_result clearing**：
> | 维度 | 本项目 | Kompact Observation Masker |
> |------|--------|---------------------------|
> | 触发方式 | 字符阈值（`PROXY_CLEAR_THRESHOLD=15000`） | keep_last_n 固定保留最近 N 条 |
> | 占位符格式 | `[cleared: <tool_name> <hash>]` | `[Output omitted — N tokens. Starts with: ...]` |
> | 上下文提示 | 无 | **有**（首行 100 字符 + token 数量） |
> | 原始内容存储 | 不存储（不可回填） | **存入 store**（可按 ID 检索） |
> | 死亡循环风险 | **高**（cleared → re-read → "Wasted call" → 死循环，已在 AGENTS.md 记录） | **低**（占位符包含首行摘要，模型知道内容已存在） |
>
> **关键发现**：Kompact 的占位符设计**直接解决了本项目的死亡循环问题**。本项目 `[cleared: ...]` 占位符不提供任何上下文，模型无法区分"内容被清除"和"读取失败"，导致 re-read 死循环。Kompact 的 `[Output omitted — N tokens. Starts with: ...]` 让模型明确知道"内容已存在但被省略"，不会触发 re-read。
>
> **可借鉴点**：
> 1. **增强占位符**：在 `[cleared: ...]` 中加入 token 数量和首行摘要，改为 `[cleared: <tool> <hash> — N tokens. Starts with: <first 100 chars>]`。**这是最直接的改进**，无需架构变更。
> 2. **原始内容存储**：本项目目前不存储被清除的原始内容。如果未来要支持"模型主动请求展开"（类似 DCP 的 compress 工具），需要先建立 store 机制。Kompact 的 `cache/store.py`（6.8KB）提供了参考实现。
> 3. **⚠️ 本项目 AGENTS.md 已明确 tool clearing 不推荐用于本地后端**，改用 Truncate rounds 策略智能保留 Read 结果。Kompact 的占位符增强思路可以应用到 Truncate 的 summary 消息中——在摘要中为每个被压缩的 tool_result 加入首行上下文。

#### 10.1.5 Kompact 源码分析小结

| 变换 | 对标本项目 | 可借鉴度 | 行动建议 |
|------|-----------|----------|----------|
| Schema Optimizer (TF-IDF) | `_filter_tools` 白名单 | ★★☆ | 中期可选增强（工具数 50+ 时启用） |
| Cache Aligner (UUID/路径归一化) | `_normalize_dates` | ★★★ | **短期直接借鉴**：扩展归一化到 UUID 和 system prompt 路径 |
| Observation Masker (占位符增强) | tool_result clearing | ★★★ | **短期直接借鉴**：占位符加 token 数 + 首行摘要 |
| Content Compressors (TOON/JSON/Code/Log) | `_compress_content_pass` | ★☆☆ | 本项目已有等价实现，TOON 细节未获取（16KB 超时） |
| HTML Stripper | 无对标 | ★★☆ | WebFetch 场景有用，中期可加 |
| Adaptive Scaling | 5 阶段生命周期 | ★★☆ | 连续插值 vs 离散阶段，各有优劣 |

---

### 10.2 TokenSieve（MIT）— 文件块指纹去重源码分析

> 仓库：`david-spies/tokensieve`（Go，~0 stars，极早期）
> 核心文件：`pkg/diff/compression.go`（6.6KB）、`pkg/cache/sliding_window.go`（4KB）

#### 10.2.1 多通道压缩策略（`compression.go`）

TokenSieve 的核心是 **4 通道顺序去重管线**：

```
Pass 1: 整消息精确去重     — role+content 哈希，完全相同的消息只保留首条
Pass 2: 文件块去重         — 正则识别文件转储头，按块哈希去重
Pass 3: 堆栈跟踪去重       — 正则识别错误/panic 块，按块哈希去重
Pass 4: 大段文本去重       — >200 字符的连续段落按哈希去重
```

**Pass 1：整消息精确去重**
```go
seen := make(map[string]bool)
for _, msg := range messages {
    h := c.ComputeHash(msg.Role + "::" + msg.Content)  // SHA-256
    if seen[h] { continue }  // 跳过完全相同的消息
    seen[h] = true
}
```

**Pass 2：文件块去重（核心创新）**
```go
// 匹配 Claude Code/Cursor/Copilot 的文件转储头格式
var fileBlockRe = regexp.MustCompile(
    `(?m)^(---\s*BEGIN FILE|File:|<file path=|// FILE:|#\s*FILE:)\s*(.+)`,
)

// 按文件头分割，每块独立哈希
for i, header := range headers {
    block := header + body
    hash := c.ComputeHash(block)
    if c.Get(hash) {  // 缓存命中
        // 替换为紧凑引用标注
        out.WriteString(fmt.Sprintf(
            "\n[TS:FILE_CACHE ref=%s path=%q saved≈%dtok]\n",
            hash[:12], extractPath(header), entry.TokenEst,
        ))
    } else {
        c.Put(hash, block, sessionID)  // 首次出现，存入缓存
        out.WriteString(block)
    }
}
```

**Pass 3：堆栈跟踪去重**
```go
var stackTraceRe = regexp.MustCompile(
    `(?s)((?:Error:|Traceback \(most recent call last\)|panic:|goroutine \d+).+?(?:\n\n|\z))`,
)
// 相同的堆栈跟踪只保留首次，后续替换为 [TS:TRACE_CACHE ref=...]
```

**Pass 4：大段文本去重**
```go
// >200 字符的 \n\n 分隔段落
for _, para := range strings.Split(content, "\n\n") {
    if utf8.RuneCountInString(para) < 200 { continue }
    hash := c.ComputeHash(para)
    if c.Get(hash) {
        out = append(out, fmt.Sprintf("[TS:BLOB_CACHE ref=%s saved≈%dtok]", ...))
    } else {
        c.Put(hash, para, sessionID)
        out = append(out, para)
    }
}
```

#### 10.2.2 滑动窗口缓存（`sliding_window.go`）

TokenSieve 使用 `SlidingWindowCache` 存储内容指纹：
- **哈希算法**：SHA-256（`ComputeHash` 方法）
- **缓存键**：内容哈希前 12 字符（`hash[:12]`）
- **缓存值**：`{Content, TokenEst, SessionID}`
- **淘汰策略**：滑动窗口（LRU 变体，具体实现未获取）
- **去重标注格式**：`[TS:FILE_CACHE ref=<12char_hash> path="<path>" saved≈<N>tok]`

#### 10.2.3 TokenSieve 源码分析小结

> **对比本项目**：
> | 维度 | 本项目 | TokenSieve |
> |------|--------|------------|
> | 去重粒度 | **整消息级**（tool_result 完整清除或保留） | **块级**（文件块/堆栈/段落独立去重） |
> | 去重策略 | 无主动去重（依赖 Truncate rounds 策略丢弃旧消息） | **4 通道顺序去重**（整消息→文件块→堆栈→大段文本） |
> | 哈希算法 | 无 | SHA-256 |
> | 缓存引用 | 无 | `[TS:FILE_CACHE ref=... path=... saved≈Ntok]` 紧凑标注 |
> | 文件块识别 | 无 | 正则匹配 5 种文件转储头格式（Claude Code/Cursor/Copilot 兼容） |
> | 堆栈跟踪识别 | 无（有 error_translation 但不压缩） | 正则匹配 Error/Traceback/panic/goroutine |
> | 跨请求缓存 | 无 | **有**（SlidingWindowCache 跨请求持久化） |
>
> **关键发现**：
> 1. **文件块去重是本项目缺失的能力**。在 agentic coding 中，模型经常反复 Read 同一个大文件（如 `anthropic_proxy.py` 3600 行），每次 Read 结果都完整注入上下文。TokenSieve 的文件块去重可以在第二次 Read 时将整个文件内容替换为 `[TS:FILE_CACHE ref=... path="anthropic_proxy.py" saved≈3600tok]`，**节省 100% 重复文件内容 token**。
> 2. **堆栈跟踪去重对本项目有价值**。Bash 命令失败时，错误堆栈经常重复出现（如 Metal OOM 堆栈）。本项目目前有 `error_translation` 重写错误消息，但不压缩重复堆栈。
> 3. **跨请求缓存是关键基础设施**。TokenSieve 的 `SlidingWindowCache` 让去重不仅限于单次请求内部，而是跨请求持久化。本项目目前每次请求独立处理，无跨请求去重能力。
>
> **可借鉴点**：
> 1. **短期：文件块指纹去重**。在 `_compress_content_pass` Phase 2a 前加入文件块去重预处理：
>    - 用正则识别 tool_result 中的文件转储头（`File:`、`<file path=`等）
>    - 对每个文件块计算 SHA-256 指纹
>    - 进程内缓存（`dict[hash, {content, token_est}]`），同会话内重复文件块替换为 `[FILE_CACHE ref=... saved≈Ntok]`
>    - **预期收益**：反复 Read 同一文件场景节省 20-40% 上下文
> 2. **中期：堆栈跟踪去重**。在 `error_translation` 旁加堆栈去重，相同堆栈只保留首次完整内容。
> 3. **⚠️ 跨请求缓存的隐私风险**：TokenSieve 的 `SlidingWindowCache` 持久化文件内容到磁盘。本项目定位为单用户本地代理，进程内缓存（`dict`）即可，不需要磁盘持久化，避免 prompt 数据泄漏风险。

---

### 10.3 P1 研究结论与行动项汇总

| 优先级 | 改进点 | 来源 | 预期收益 | 实现难度 | 许可证风险 |
|--------|--------|------|----------|----------|------------|
| **P1-短期** | UUID + system 路径归一化 | Kompact Cache Aligner | 提高 prefix cache 命中率 5-15% | 低（扩展 `_normalize_dates`） | 无（MIT） |
| **P1-短期** | tool_result 占位符增强 | Kompact Observation Masker | 减少死亡循环，改善模型理解 | 低（改占位符模板） | 无（MIT） |
| **P1-短期** | 文件块指纹去重（进程内） | TokenSieve file-block dedup | 反复 Read 场景节省 20-40% | 中（正则+SHA-256+缓存 dict） | 无（MIT） |
| **P1-中期** | 堆栈跟踪去重 | TokenSieve stack-trace dedup | 重复错误场景节省 10-20% | 中（正则+缓存） | 无（MIT） |
| **P1-中期** | TF-IDF 工具选择（可选策略） | Kompact Schema Optimizer | 工具数 50+ 时更灵活 | 中（TF-IDF 计算） | 无（MIT） |
| **P1-中期** | HTML 导航剥离 | Kompact HTML Stripper | WebFetch 场景节省 30-50% | 中（HTML 解析） | 无（MIT） |

> **关键结论**：P1 研究验证了 §7/§8 中基于 README 的推断。Kompact 和 TokenSieve 的源码提供了三个可直接落地的短期改进（UUID 归一化、占位符增强、文件块去重），均无许可证风险（MIT）。**下一步**：进入 P2 研究（DCP 源码只读分析 + Headroom tree-sitter），为中期改进（模型可调用 compress 工具、嵌套摘要）收集实现细节。

---

## 11. P2 深度研究：DCP 源码实现细节（只读分析）

> ⚠️ **许可证约束**：DCP 采用 AGPL-3.0-or-later 许可证。**代码不可直接复用**。
> 本节内容为只读分析，仅用于理解策略思路，实现时必须独立编写（Python stdlib only）。
> 12 个源码文件已下载到 `/tmp/dcp-research/` 供参考。

### 11.1 核心创新：嵌套摘要机制（Nested Summary）

DCP 最核心的创新是**压缩块嵌套树**——允许新的压缩摘要引用之前已压缩的块，形成多层摘要嵌套。

#### 11.1.1 数据结构（`state/types.ts`）

```typescript
interface CompressionBlock {
    blockId: number              // 压缩块 ID（b1, b2, ...）
    runId: number                // 压缩运行 ID（同一次 compress 调用可含多块）
    active: boolean              // 是否活跃（被新块消耗后变 false）
    consumedBlockIds: number[]   // 此块消耗的旧块 ID（形成嵌套）
    parentBlockIds: number[]     // 消耗此块的新块 ID（反向链）
    effectiveMessageIds: string[] // 有效消息 = 直接消息 + 所有被消耗块的有效消息
    effectiveToolIds: string[]
    directMessageIds: string[]   // 直接压缩的消息
    directToolIds: string[]
    summary: string              // 摘要文本
    anchorMessageId: string      // 摘要注入点（作为合成 user 消息插入）
    mode: "auto" | "manual"      // 触发模式
    topic?: string               // 主题标签
}
```

**嵌套树语义**：
- `consumedBlockIds`：新块吸收了哪些旧块（向下引用）
- `parentBlockIds`：旧块被哪个新块吸收（向上引用）
- `effectiveMessageIds`：递归累积——新块"看见"的消息包括自己的直接消息 + 所有被消耗块的间接消息
- `active`：被消耗后置 false，不再注入出站消息

#### 11.1.2 `(bN)` 占位符机制（`range-utils.ts`）

模型在生成摘要时使用 `(bN)` 占位符引用之前已压缩的块。这是嵌套的关键接口。

**占位符解析**：
```typescript
function parseBlockPlaceholders(summary: string): Placeholder[] {
    return [...summary.matchAll(/\(b(\d+)\)|\{block_(\d+)\}/gi)]
        .map(m => ({ id: parseInt(m[1] || m[2]), startIndex: m.index, endIndex: m.index + m[0].length }))
}
```

**占位符验证**（`validateSummaryPlaceholders`）：
- 必须精确匹配 `requiredSet`（被消耗的旧块集合）
- 不允许重复
- 不允许引用不存在的块
- **preflight 检查**：在应用前验证，失败则拒绝压缩

**占位符注入**（`injectBlockPlaceholders`，`range-utils.ts:170`）：
```typescript
function injectBlockPlaceholders(summary, requiredSet, blocks): string {
    let expanded = "", cursor = 0
    for (const ph of parseBlockPlaceholders(summary)) {
        expanded += summary.slice(cursor, ph.startIndex)
        const target = blocks.get(ph.id)
        expanded += restoreSummary(target.summary)  // 旧块的实际摘要内容
        consumedBlockIds.push(ph.id)
        cursor = ph.endIndex
    }
    expanded += summary.slice(cursor)
    return expanded
}
```

> **关键设计**：占位符不是引用指针，而是**内容内联展开**。摘要文本本身被替换为旧块的完整摘要。这意味着摘要会随嵌套层数增长，但出站消息始终是平面的——无需递归解析。

**边界处理**（`injectBoundarySummary`）：
- 如果 range 的 start/end 边界本身落在已压缩块内，自动注入该块的摘要
- 处理模型可能遗漏的边界块占位符

**缺失块补全**（`appendMissingBlockSummaries`）：
- 如果模型遗漏了某些 required 块占位符，在摘要末尾附加：
  ```
  The following previously compressed summaries were also part of this conversation section:
  ### (b3)
  <旧块 b3 的摘要内容>
  ```

#### 11.1.3 压缩状态应用（`state.ts:62` `applyCompressionState`）

```typescript
function applyCompressionState(state, newBlock, consumedBlockIds) {
    for (const oldId of consumedBlockIds) {
        const old = state.blocks.get(oldId)
        old.active = false
        old.deactivatedByBlockId = newBlock.blockId
        old.parentBlockIds.push(newBlock.blockId)
        // 新块继承旧块的所有有效消息
        newBlock.effectiveMessageIds.push(...old.effectiveMessageIds)
    }
    // 从活跃块列表中移除被消耗的旧块
    state.activeBlockIds = state.activeBlockIds.filter(id => !consumedBlockIds.includes(id))
    state.blocks.set(newBlock.blockId, newBlock)
    state.activeBlockIds.push(newBlock.blockId)
}
```

**对比本项目**：本项目的 `_compress_middle_with_llm` 是**单层扁平摘要**——每次截断后生成一个 summary 消息替换中间消息，无嵌套引用机制。下一次截断时，旧的 summary 会被当作普通消息处理（可能被再次压缩或保留）。

**可借鉴策略**（独立实现）：
- 用 `(sN)` 占位符让新摘要引用旧摘要
- 维护 `dict[summaryId, {content, consumedIds, active}]` 状态
- 注入时内联展开旧摘要内容
- 挑战：本项目无持久化会话状态（stateless proxy），需要新增 in-memory session state

---

### 11.2 非破坏性出站消息构建（`messages/prune.ts`）

DCP 的出站转换是**非破坏性**的——原始消息保留在会话状态中，只在发送给模型时替换。

#### 11.2.1 `filterCompressedRanges()` — 核心出站转换

```typescript
function filterCompressedRanges(messages, state): Message[] {
    const result = []
    for (const msg of messages) {
        // 在 anchorMessageId 处注入压缩摘要（合成 user 消息）
        const anchor = state.blocks.find(b => b.active && b.anchorMessageId === msg.id)
        if (anchor) {
            result.push(createSyntheticUserMessage(anchor.summary))
        }
        // 跳过已被压缩的消息（activeBlockIds.length > 0）
        if (msg.activeBlockIds && msg.activeBlockIds.length > 0) continue
        result.push(msg)
    }
    return result
}
```

**关键**：摘要作为**合成 user 消息**注入，不是 system 消息。这让模型将摘要视为用户提供的上下文，而非系统指令。

#### 11.2.2 工具输出/输入剪枝

| 函数 | 作用 | 替换文本 |
|------|------|----------|
| `pruneToolOutputs()` | 替换被剪枝工具的输出 | `[Output removed to save context - information superseded or no longer needed]` |
| `pruneToolErrors()` | 替换失败工具的字符串输入 | `[input removed due to failed tool call]` |
| `pruneToolInputs()` | 替换 AskUserQuestion 工具输入 | `[questions removed - see output for user's answers]` |
| `pruneFullTool()` | **已注释禁用** — 完全移除工具调用消息 | （未启用） |

> **对比本项目**：本项目的 `PROXY_CLEAR_ENABLED` 用 `[cleared: ...]` 替换 tool_result，**无任何上下文信息**导致死亡循环（§3 已分析）。DCP 的替换文本包含"information superseded"说明，且**只对明确标记为 prune 的工具生效**（通过 dedup/purge 策略标记），不是阈值触发。

**可借鉴策略**：
- 替换文本增加语义说明（"information superseded"）
- 不基于字符阈值，而基于明确标记（dedup 标记重复工具、purge 标记过期错误工具）
- 本项目已在 v0.5.2 的 rounds 策略中实现"smart-preserving Read results"，方向一致

---

### 11.3 Deduplication 策略（`strategies/deduplication.ts`）

#### 11.3.1 签名算法

```typescript
function createToolSignature(toolName: string, parameters: any): string {
    const normalized = normalizeParameters(parameters) // 去除 undefined/null 值
    const sorted = sortObjectKeys(normalized)           // 递归排序对象键
    return `${toolName}::${JSON.stringify(sorted)}      // 稳定字符串签名
}
```

**归一化**：
- `normalizeParameters`：递归移除 `undefined` 和 `null` 值（让 `{a: 1, b: undefined}` 等同 `{a: 1}`）
- `sortObjectKeys`：递归排序对象键（让 `{b: 2, a: 1}` 签名等同 `{a: 1, b: 2}`）

**分组保留**：
- 按签名分组，**只保留最后一条**（最新调用）
- 其余标记为 prune（`state.prune.tools.set(id, tokenCount)`）

#### 11.3.2 保护机制

```typescript
function isToolNameProtected(name: string): boolean {
    return PROTECTED_TOOLS.includes(name) // 受保护工具列表
}
function isFilePathProtected(path: string): boolean {
    return PROTECTED_PATH_PATTERNS.some(p => minimatch(path, p)) // glob 模式
}
```

**对比本项目**：本项目无工具调用级去重。`_compress_content_pass` 的 Phase 1 做的是消息内容级 SHA-256 去重（整条消息），不是工具参数级。

**可借鉴策略**（独立实现）：
- 在 `_compress_content_pass` 前加工具调用去重预处理
- 签名 = `tool_name + hashlib.sha256(json.dumps(sorted_params).encode()).hexdigest()`
- 进程内 `dict[signature, tool_use_id]`，重复时替换旧 tool_result 为 `[superseded by later call]`
- 保护：`Read`/`Write`/`Edit` 等关键工具 + glob 路径白名单

---

### 11.4 Purge Errors 策略（`strategies/purge-errors.ts`）

```typescript
function purgeErrors(state, turnThreshold = 4) {
    for (const [id, metadata] of state.tools) {
        if (metadata.status !== "error") continue  // 只处理错误工具
        const turnAge = state.currentTurn - metadata.turn
        if (turnAge >= turnThreshold) {            // 默认 4 轮后清除
            state.prune.tools.set(id, metadata.tokenCount)
        }
    }
}
```

**对比本项目**：本项目的 `_translate_tool_result_errors` 只**重写错误消息文本**（如 "Wasted call" → 中文提示），不剪枝。`_detect_blocker_pattern` 检测连续相同错误并注入 `[BLOCKER]` 消息，但也不剪枝。

**可借鉴策略**：
- 在 `_detect_blocker_pattern` 旁加过期错误剪枝
- 跟踪每个 tool_result 的轮次年龄
- `turnAge >= 4` 且 `is_error == true` 的工具，替换输入为 `[expired error call - see BLOCKER notice]`
- 与现有 blocker 检测协同：blocker 注入提示 + 旧错误输入剪枝 = 双重清理

---

### 11.5 System Prompt 与 Compress Range Prompt（`prompts/`）

#### 11.5.1 System Prompt 核心理念（`prompts/system.ts`）

DCP 注入的系统提示强调**压缩哲学**：

| 核心理念 | 原文摘录 |
|----------|----------|
| 环境约束 | "context-constrained environment" |
| 持续管理 | "Manage context continuously" |
| 结晶而非清理 | "compress transforms conversation content into dense, high-fidelity summaries. This is not cleanup - it is crystallization" |
| 相变隐喻 | "Think of compression as phase transitions: raw exploration becomes refined understanding" |
| 压缩时机 | COMPRESS WHEN: section is genuinely closed |
| 不压缩条件 | DO NOT COMPRESS IF: raw context still relevant/needed |
| 自问判断 | "Is this section closed enough to become summary-only right now?" |

> **注意**：本文件的系统提示与 opencode 的 `compress` 工具描述高度一致——证实 DCP 是 opencode 内置 compress 工具的策略文档来源。

#### 11.5.2 Compress Range Prompt 要求（`prompts/compress-range.ts`）

| 要求 | 说明 |
|------|------|
| **EXHAUSTIVE** | "Capture file paths, function signatures, decisions made, constraints discovered, key findings... EVERYTHING" |
| **USER INTENT FIDELITY** | "preserve the user's intent with extra care. Do not change scope, constraints, priorities, acceptance criteria" |
| **Direct quotes** | "Directly quote user messages when they are short enough" |
| **LEAN** | "Strip away the noise: failed attempts that led nowhere, verbose tool outputs, back-and-forth exploration" |
| **(bN) placeholders** | 精确匹配 required set，无重复，preflight check |
| **FLOW PRESERVATION** | 占位符展开后文本仍连贯 |
| **BOUNDARY IDS** | mNNNN（消息）+ bN（压缩块）|
| **BATCHING** | 多个独立 range 可在一次调用中批量处理 |

> **对比本项目**：本项目的 `_compress_middle_with_llm` prompt 结构为 `Root cause:`/`Fix:`/`Avoidance:` 三段式（见 §2 Layer 5）。DCP 的要求更全面——特别强调 **USER INTENT FIDELITY**（保留用户意图）和 **EXHAUSTIVE**（穷尽关键信息）。

**可借鉴策略**：
- 本项目 LLM 摘要 prompt 可增加"保留用户原始意图"和"穷尽关键路径/签名/约束"要求
- 加入"直接引用短用户消息"指令
- 加入"剥离失败尝试和探索噪音"指令

---

### 11.6 管线流程（`compress/pipeline.ts`）

```
prepareSession():
  1. refreshManualMode() — 检查手动/自动模式
  2. ask(permission: "compress") — 请求用户权限（手动模式下）
  3. fetchSessionMessages() — 获取会话所有消息
  4. ensureSessionInitialized() — 初始化会话状态（加载/创建）
  5. assignMessageRefs() — 分配消息引用 ID（mNNNN 格式）
  6. deduplicate() — ★ 自动去重（每次 compress 调用都执行）
  7. purgeErrors() — ★ 自动清除过期错误（每次 compress 调用都执行）
  8. buildSearchContext() — 构建搜索上下文

[执行压缩 — 模型调用 compress 工具时]
  9. resolveRanges() — 解析 range 边界（mNNNN → 消息 ID）
  10. validateNonOverlapping() — 验证多个 range 不重叠
  11. 对每个 compression plan:
      - parseBlockPlaceholders() — 解析 (bN) 占位符
      - validateSummaryPlaceholders() — 验证占位符完整性
      - injectBlockPlaceholders() — 注入嵌套内容（内联展开旧摘要）
      - appendProtectedUserMessages() — 附加受保护用户消息
      - appendProtectedPromptInfo() — 附加 <protect> 标签内容
      - appendProtectedTools() — 附加受保护工具输出
      - appendMissingBlockSummaries() — 补全模型遗漏的块
  12. allocateRunId() + allocateBlockId() — 分配新块 ID
  13. wrapCompressedSummary() — 包装为 [Compressed conversation section] 格式
  14. applyCompressionState() — ★ 应用压缩状态（更新嵌套树）

finalizeSession():
  15. applyPendingCompressionDurations()
  16. saveSessionState() — 持久化会话状态
  17. sendCompressNotification() — 发送通知
```

**关键流程特点**：
1. **deduplicate + purgeErrors 是自动前置步骤**——每次 compress 调用都自动执行，不需要模型显式请求
2. **compress 是模型可调用工具**——不是代理自动触发，而是模型主动判断何时压缩
3. **权限控制**——手动模式下需用户确认；自动模式下模型可自主调用
4. **状态持久化**——会话状态（压缩块树）保存到磁盘，跨请求恢复

> **对比本项目**：本项目的压缩是**代理自动触发**的（基于字符阈值/轮次阈值/5 阶段生命周期），模型无控制权。DCP 让模型**自主决定**压缩时机和范围，更灵活但需要模型具备判断力。

---

### 11.7 DCP 策略对比汇总

| 维度 | DCP | 本项目（anthropic_proxy.py） | 差异分析 |
|------|-----|------------------------------|----------|
| **触发方式** | 模型调用 compress 工具（主动） | 代理基于阈值自动触发（被动） | DCP 更灵活，本项目更可控 |
| **摘要嵌套** | `(bN)` 占位符 + 嵌套树（多层） | 单层扁平 summary 消息 | DCP 支持多层累积，本项目每次独立 |
| **状态管理** | 持久化会话状态（磁盘） | 无状态（stateless proxy） | DCP 支持跨请求，本项目每次请求独立 |
| **去重粒度** | 工具参数级（normalized + sorted 签名） | 消息内容级（SHA-256 整条） | DCP 更精细，能识别"同工具不同参数" |
| **错误清理** | turnAge >= 4 自动剪枝错误工具输入 | blocker 注入 + error_translation 重写 | DCP 直接剪枝，本项目只提示 |
| **保护机制** | 工具名 + glob 路径模式 | rounds 策略 smart-preserve Read results | 两者方向一致，实现不同 |
| **摘要质量** | EXHAUSTIVE + USER INTENT FIDELITY + LEAN | Root cause/Fix/Avoidance 三段式 | DCP 要求更全面 |
| **出站转换** | 非破坏性（合成 user 消息注入摘要） | 破坏性（直接修改消息数组） | DCP 保留原始数据，本项目就地修改 |
| **摘要类型** | 合成 user 消息 | 合成 user 消息（`_compress_middle_with_llm`） | 两者一致——user 消息更自然 |
| **权限控制** | 手动/自动模式 + 用户确认 | 无（代理全自动） | DCP 有人工兜底，本项目全自动 |

---

### 11.8 P2 DCP 研究结论与行动项

| 优先级 | 改进点 | DCP 来源 | 预期收益 | 实现难度 | 许可证风险 |
|--------|--------|----------|----------|----------|------------|
| **P2-中期** | 嵌套摘要占位符 `(sN)` | DCP `(bN)` 机制 | 支持多层累积摘要，减少信息丢失 | **高**（需新增 session state） | 无（策略参考，独立实现） |
| **P2-中期** | 工具参数级去重 | DCP deduplication | 精确识别重复工具调用，比消息级去重更准 | 中（签名 + 进程内缓存） | 无（策略参考） |
| **P2-中期** | 过期错误输入剪枝 | DCP purge-errors | 清理 4+ 轮前的错误工具输入 | 低（轮次跟踪 + 替换） | 无（策略参考） |
| **P2-中期** | LLM 摘要 prompt 增强 | DCP compress-range prompt | 提升摘要质量（穷尽性 + 意图保留） | 低（改 prompt 模板） | 无（策略参考） |
| **P2-长期** | 模型可调用 compress 工具 | DCP compress 工具设计 | 让模型自主决定压缩时机 | **极高**（需改 API 接口） | 无（策略参考） |
| **P2-长期** | 会话状态持久化 | DCP session state | 支持嵌套摘要 + 跨请求去重 | **极高**（需改 stateless 架构） | 无（策略参考） |

> **关键结论**：
> 1. **DCP 的嵌套摘要机制是最值得借鉴的长期方向**，但需要引入会话状态管理——这与本项目"stateless proxy"架构冲突，需要架构级决策。
> 2. **短期可落地的有三个**：工具参数级去重、过期错误剪枝、LLM 摘要 prompt 增强。均无需架构改动。
> 3. **模型可调用 compress 工具是长期愿景**——让模型自主管理上下文，而非代理被动截断。这需要暴露新的 API 接口（如 `POST /v1/compress`）和修改客户端集成方式。
> 4. **AGPL-3.0 约束确认**：所有实现必须独立编写，仅参考策略思路。本项目 Python stdlib only 的约束进一步排除了直接移植 TypeScript 代码。

---

## 12. P2 深度研究：Headroom-ai + tree-sitter 代码感知工具（Skim + Token Reducer）

> **重要更正的更正**：本节初稿曾误称"§7 中的 Headroom 系误识"——该判断本身是错误的。
> 实际情况：PyPI 上有两个不同包：
> - `headroom` = "Max Headroom" CLI AI 助手（MIT, James Bridges, GitHub 404）——**不相关**
> - **`headroom-ai`** = "The Context Optimization Layer for LLM Applications"（**Apache-2.0, chopratejas/headroom, ★31,630**）——**§7 正确识别的真正 Headroom**
>
> Headroom-ai 是目前 GitHub 上 **stars 最多的上下文压缩工具**（31K+），提供 library/proxy/MCP 三种模式，
> 与本项目的 HTTP 代理架构**直接可比**。以下 §12.1 为其深度分析。
> Skim 和 Token Reducer 作为 tree-sitter 代码压缩的补充研究保留在 §12.2 和 §12.3。

### 12.1 Headroom-ai（chopratejas/headroom）—— 最成熟的上下文压缩代理

| 属性 | 值 |
|------|-----|
| 仓库 | [chopratejas/headroom](https://github.com/chopratejas/headroom) |
| PyPI | [`headroom-ai`](https://pypi.org/project/headroom-ai/) v0.26.0 |
| 语言 | Python + TypeScript |
| 许可证 | **Apache-2.0** |
| Stars | **31,630**（所有上下文压缩工具中最高） |
| 创建 | 2026-01-07 |
| 最近更新 | 2026-06-18（今天，活跃维护） |
| 版本 | v0.26.0（成熟） |
| 安装 | `pip install "headroom-ai[all]"` / `npm install headroom-ai` / Docker |
| 文档 | [headroom-docs.vercel.app](https://headroom-docs.vercel.app/docs) |

#### 12.1.1 核心定位：上下文压缩层

> **"60–95% fewer tokens · library · proxy · MCP · 6 algorithms · local-first · reversible"**

Headroom 压缩 AI agent 读取的一切——工具输出、日志、RAG chunks、文件、会话历史——在到达 LLM 之前。
**相同答案，fraction of tokens**。

**四种集成模式**：
| 模式 | 命令 | 对应本项目 |
|------|------|------------|
| Library | `compress(messages)` in Python/TS | — |
| **Proxy** | **`headroom proxy --port 8787`** | **`anthropic_proxy.py:4000`**（直接对应） |
| Agent wrap | `headroom wrap claude\|codex\|cursor\|aider\|copilot` | — |
| MCP server | `headroom mcp install` | — |

#### 12.1.2 架构：CacheAligner → ContentRouter → CCR

```
Agent/App → Headroom (local) → LLM Provider
             │
             ├─ CacheAligner  → 稳定前缀，让 KV cache 命中
             ├─ ContentRouter → 检测内容类型，选择压缩器
             │    ├─ SmartCrusher   (JSON 压缩)
             │    ├─ CodeCompressor (AST/tree-sitter 压缩)
             │    └─ Kompress-base  (文本 ML 模型压缩)
             ├─ CCR (reversible)   → 原始内容缓存，LLM 按需检索
             ├─ Cross-agent memory → 跨 agent 共享存储，自动去重
             └─ headroom learn     → 挖掘失败会话，写修正到 CLAUDE.md/AGENTS.md
```

**六大压缩算法**：
| 算法 | 作用 | 技术基础 |
|------|------|----------|
| **SmartCrusher** | 通用 JSON 压缩（数组/嵌套对象/混合类型） | 结构化 JSON 变换 |
| **CodeCompressor** | AST 感知代码压缩（Python/JS/Go/Rust/Java/C++） | tree-sitter (ast-grep-cli) |
| **Kompress-base** | 文本/散文压缩 | 自研 HuggingFace 模型 ([kompress-v2-base](https://huggingface.co/chopratejas/kompress-v2-base)) |
| **CacheAligner** | 前缀稳定化，提高 KV cache 命中率 | 前缀归一化 |
| **IntelligentContext** | 评分式上下文拟合，学习重要性 | 学习型重要性评分 |
| **CCR** | 可逆压缩——原始内容缓存，LLM 按需检索 | 本地缓存 + retrieval 工具 |

#### 12.1.3 CCR（可逆压缩）—— 核心创新

> **本项目最大的差距**：Headroom 的 CCR 让压缩**可逆**——原始内容存储在本地，
> LLM 可通过 `headroom_retrieve` 工具按需检索。这解决了本项目 `[cleared: ...]` 占位符
> 导致的**死亡循环**问题（§3 已分析）。

CCR 流程：
1. 压缩时：原始内容存储到本地缓存（TTL 可配）
2. 压缩后：上下文中用压缩版本替换原始内容
3. LLM 需要时：调用 `headroom_retrieve` 工具获取原始内容
4. 自动过期：TTL 到期后自动清理

**对比本项目**：
- 本项目 `[cleared: ...]` → **不可逆**，模型无法获取原始内容 → 重新执行工具 → 死亡循环
- Headroom CCR → **可逆**，模型可按需检索原始内容 → 无需重新执行工具 → 避免死亡循环

#### 12.1.4 Pipeline 生命周期

Headroom 定义了一个稳定的请求生命周期：

```
Setup → Pre-Start → Post-Start → Input Received → Input Cached →
Input Routed → Input Compressed → Input Remembered → Pre-Send → Post-Send → Response Received
```

- **Transforms** 做实际工作：CacheAligner, ContentRouter, SmartCrusher, CodeCompressor, Kompress-base, IntelligentContext/RollingWindow
- **Pipeline extensions** 通过 `on_pipeline_event(...)` 观察或自定义生命周期阶段
- **Compression hooks** 作为额外扩展点
- **Proxy extensions** 是 ASGI 中间件/路由/启动策略的集成点

> **对比本项目**：本项目的 8 层管线（§2）是类似的分层架构，但无正式的生命周期钩子机制。
> Headroom 的 `on_pipeline_event(...)` 模式更灵活，允许第三方扩展。

#### 12.1.5 基准测试结果

**实际 agent 工作负载节省**：
| 工作负载 | 压缩前 | 压缩后 | 节省 |
|----------|--------|--------|------|
| 代码搜索（100 结果） | 17,765 | 1,408 | **92%** |
| SRE 事故调试 | 65,694 | 5,118 | **92%** |
| GitHub issue 分拣 | 54,174 | 14,761 | **73%** |
| 代码库探索 | 78,502 | 41,254 | **47%** |

**标准基准精度保持**：
| 基准 | 类别 | 基线 | Headroom | Delta |
|------|------|------|----------|-------|
| GSM8K | 数学 | 0.870 | 0.870 | **±0.000** |
| TruthfulQA | 事实 | 0.530 | 0.560 | **+0.030** |
| SQuAD v2 | QA | — | **97%** | 19% 压缩 |
| **BFCL** | **工具** | — | **97%** | **32% 压缩** |

> **关键**：BFCL（Berkeley Function-Calling Leaderboard）97% 精度 + 32% 压缩——
> 证明 Headroom 的压缩**不破坏工具调用能力**。这是本项目最关注的指标。

#### 12.1.6 Agent 兼容性

| Agent | `headroom wrap` | 特性 |
|-------|:---------------:|------|
| **Claude Code** | ✅ | `--memory` · `--code-graph` |
| Codex | ✅ | 与 Claude 共享 memory |
| Cursor | ✅ | 打印配置，粘贴一次 |
| Aider | ✅ | 启动 proxy + 启动 agent |
| Copilot CLI | ✅ | 启动 proxy + 订阅模式 |
| OpenClaw | ✅ | 安装为 ContextEngine 插件 |

**集成生态**：Anthropic SDK, OpenAI SDK, Vercel AI SDK, LiteLLM, LangChain, Agno, Strands, ASGI, MCP

#### 12.1.7 依赖分析

**核心依赖（轻量）**：`tiktoken`, `pydantic`, `litellm`, `click`, `rich`, `opentelemetry-api`, `ast-grep-cli`

**可选 extras**：
| Extra | 用途 | 依赖 | 本项目兼容性 |
|-------|------|------|-------------|
| `[code]` | AST 代码压缩 | `tree-sitter-language-pack` | ❌ 非 stdlib |
| `[memory]` | 跨 agent 记忆 | `hnswlib`, `sqlite-vec`, `sentence-transformers` | ❌ 非 stdlib |
| `[ml]` | Kompress-base 模型 | `torch`, `transformers`, `huggingface-hub` | ❌ 非 stdlib（~2-3GB） |
| `[proxy]` | HTTP 代理模式 | `fastapi`, `uvicorn`, `httpx`, `magika`, `zstandard` | ❌ 非 stdlib |
| `[relevance]` | 相关性评分 | `fastembed`, `numpy` | ❌ 非 stdlib |
| `[image]` | 图片压缩 | `pillow`, `rapidocr-onnxruntime` | ❌ 非 stdlib |
| **`[pytorch-mps]`** | **Apple-GPU 嵌入器 offload** | `torch`, `sentence-transformers` | ❌ 非 stdlib，但**针对 Apple Silicon** |
| `[voice]` | 语音压缩 | `onnxruntime`, `transformers`, `torch` | ❌ 非 stdlib |

> **关键发现**：Headroom 有 **`[pytorch-mps]`** extra 专门用于 **Apple Silicon GPU offload**
> （设置 `HEADROOM_EMBEDDER_RUNTIME=pytorch_mps`）。这说明 Headroom **已经适配 Apple Silicon**，
> 与本项目的运行环境（M5 Pro, 48GB）重合。

#### 12.1.8 Headroom-ai 与本项目深度对比

| 维度 | Headroom-ai | 本项目 | 差异分析 |
|------|-------------|--------|----------|
| **定位** | 通用上下文压缩层（library/proxy/MCP） | Anthropic→OpenAI 代理 + 本地后端管理 | Headroom 更通用，本项目更专一 |
| **代理模式** | `headroom proxy --port 8787` | `anthropic_proxy.py:4000` | **架构直接对应** |
| **压缩可逆性** | **CCR 可逆**（原始内容可检索） | **不可逆**（`[cleared: ...]` 占位符） | **Headroom 优势巨大**——避免死亡循环 |
| **内容路由** | ContentRouter（检测类型→选压缩器） | 8 层管线（固定顺序处理） | Headroom 更灵活，按内容类型选策略 |
| **JSON 压缩** | SmartCrusher（专用 JSON 变换） | 无专用 JSON 压缩 | Headroom 优势——工具输出多为 JSON |
| **代码压缩** | CodeCompressor（tree-sitter AST） | 无（纯文本处理） | Headroom 优势——AST 感知 |
| **文本压缩** | Kompress-base（自研 ML 模型） | LLM 摘要（`_compress_middle_with_llm`） | 不同路径——Headroom 用小模型，本项目用 LLM 自摘要 |
| **前缀稳定** | CacheAligner | `_normalize_dates` + Frozen Zone | **方向一致**，Headroom 更系统化 |
| **跨 agent 记忆** | 有（共享存储 + 自动去重） | 无（stateless） | Headroom 优势——跨会话/跨 agent |
| **失败学习** | `headroom learn`（挖掘失败→写 CLAUDE.md） | `_detect_blocker_pattern` + error_translation | **方向一致**，Headroom 更自动化 |
| **Apple Silicon** | `[pytorch-mps]` extra | Metal OOM 管理（`PROXY_MAX_CONCURRENT=1`） | 两者都关注 Apple Silicon，角度不同 |
| **依赖** | 重（torch/transformers/tree-sitter/hnswlib） | **Python stdlib only** | **本项目优势**——零依赖，易部署 |
| **Stars** | 31,630 | 内部项目 | Headroom 有社区验证 |
| **目标** | 云端 API token 费用 | 本地后端 OOM + 上下文管理 | **目标不同**——互补非完全竞争 |
| **BFCL 精度** | 97%（32% 压缩） | 未测 | Headroom 有基准验证 |

#### 12.1.9 可借鉴策略

| 优先级 | 改进点 | Headroom 来源 | 预期收益 | 实现难度 | 约束兼容 |
|--------|--------|---------------|----------|----------|----------|
| **★★★ 短期** | **可逆压缩（CCR 思路）** | CCR (reversible compression) | **解决死亡循环**——存储原始 tool_result，模型可按需检索 | 中（进程内缓存 + retrieval 提示） | ✅ 可用 stdlib 实现 |
| **★★★ 短期** | **ContentRouter 思路** | ContentRouter (检测类型→选压缩器) | 按内容类型选最优压缩策略 | 中（内容类型检测 + 分支压缩） | ✅ 可用 stdlib 实现 |
| **★★☆ 中期** | **SmartCrusher JSON 压缩** | SmartCrusher (JSON 变换) | 工具输出 JSON 压缩 30-50% | 中（JSON 结构压缩） | ✅ 可用 stdlib `json` 实现 |
| **★★☆ 中期** | **headroom learn 思路** | headroom learn (失败挖掘→写修正) | 自动从失败会话学习，写 AGENTS.md | 中（失败模式检测 + 文件写入） | ✅ 可用 stdlib 实现 |
| **★☆☆ 长期** | **CodeCompressor AST** | CodeCompressor (tree-sitter) | 代码文件压缩 60-80% | 高（需 tree-sitter 或 `ast` 模块） | ⚠️ Python `ast` 仅支持 .py |
| **★☆☆ 长期** | **Kompress-base 文本模型** | Kompress-base (HuggingFace) | 散文压缩 40-60% | 极高（需 ML 模型） | ❌ 非 stdlib |
| **★☆☆ 长期** | **跨 agent 记忆** | Cross-agent memory (共享存储) | 跨会话/跨 agent 上下文复用 | 极高（需持久化 + 嵌入） | ❌ 非 stdlib |

> **最重要的借鉴**：**CCR 可逆压缩**是 Headroom 最核心的创新，也是本项目最大的差距。
> 当前本项目的 `[cleared: ...]` 占位符导致死亡循环（§3 已详细分析）。
> CCR 的思路——"压缩但保留原始，模型可按需检索"——可以在 **stdlib 约束下**通过
> 进程内缓存（`dict[tool_use_id, original_content]`）+ 在占位符中嵌入检索提示实现。
> 这是**性价比最高的改进方向**。

### 12.2 Skim（dean0x/skim）—— 代码感知 AST 压缩引擎（补充研究）

| 属性 | 值 |
|------|-----|
| 仓库 | [dean0x/skim](https://github.com/dean0x/skim) |
| 语言 | Rust |
| 许可证 | MIT |
| Stars | 27 |
| 创建 | 2025-10-06 |
| 最近更新 | 2026-06-17（活跃维护） |
| 版本 | v2.10.0 |
| 安装 | `brew install dean0x/tap/skim` / `npm install -g rskim` / `cargo install rskim` |
| 集成 | Claude Code, Cursor, Codex, Gemini, Copilot, Crush; MCP server mode |

#### 12.2.1 核心理念：注意力稀释而非容量瓶颈

> **"Context capacity is not the bottleneck. Attention is."**
> 每个发送给 LLM 的 token 都在稀释其注意力。研究表明长上下文中模型会丢失关键细节——超过某个阈值后，
> 添加更多上下文反而让输出更差。

**典型数据**：80 文件 TypeScript 项目 = 63,198 tokens，其中只有约 5,000 tokens 是实际信号，其余是实现噪音。

#### 12.2.2 六种变换模式（Tree-sitter AST 解析）

| 模式 | 压缩率 | 保留内容 | 适用场景 |
|------|--------|----------|----------|
| Full | 0% | 原始源代码 | 测试/对比 |
| Minimal | 15-30% | 全部代码 + 文档注释 | 轻度清理 |
| Pseudo | 30-50% | 逻辑流、名称、值 | 需要逻辑的 LLM 上下文 |
| **Structure** | **70-80%** | 签名、类型、类、导入 | **理解架构（默认模式）** |
| Signatures | 85-92% | 仅可调用签名 | API 文档 |
| Types | 90-95% | 仅类型定义 | 类型系统分析 |

**变换示例**（Structure 模式）：
```typescript
// 输入 (100 tokens)
export function processUser(user: User): Result {
    const validated = validateUser(user);
    if (!validated) throw new Error("Invalid");
    const normalized = normalizeData(user);
    return await saveToDatabase(normalized);
}

// 输出 (12 tokens, 88% 压缩)
export function processUser(user: User): Result { /* ... */ }
```

**Token budget cascading**：`--tokens N` 参数自动选择能适配预算的最激进模式。

**支持 17 种语言**：TypeScript, JavaScript, Python, Rust, Go, Java, C, C++, C#, Ruby, SQL, Kotlin, Swift, Markdown, JSON, YAML, TOML。

#### 12.2.3 命令重写（PreToolUse Hook）

`skim init` 安装 PreToolUse hook，自动将以下命令重写为 skim 等价物：
- `cat`/`head`/`tail` → `skim`（AST 变换）
- `cargo test`/`pytest`/`vitest`/`jest`/`go test` → `skim <test-runner>`（输出压缩）
- `git diff` → `skim git diff`（AST 感知 diff）
- `cargo build`/`cargo clippy`/`make`/`tsc` → `skim <build-tool>`（错误提取）

**两层规则系统**：声明式前缀替换 + 自定义参数处理器。管道/换行/heredoc/命令替换的命令不会被重写（安全保护）。

#### 12.2.4 三级降级输出压缩

测试/构建/lint/git 输出压缩采用**三级降级策略**：
1. **Structured parse** — 精确解析输出格式（如 cargo test JSON）
2. **Regex fallback** — 正则提取关键信息（如失败/断言/计数）
3. **Passthrough** — 原样输出（guardrail 确保不比原始大）

**AST 感知 git diff**：显示变更函数的完整边界 + `+`/`-` 标记，剥离 diff 噪音。`--mode structure` 附加未变更函数的签名作为上下文。

#### 12.2.5 代码库热力图（Codebase Heatmap）

| 分析维度 | 说明 |
|----------|------|
| Churn hotspots | 最频繁变更的文件（按提交数排名） |
| Blast radius | 文件耦合检测——总是同时变更的文件 |
| Fix risk | 高修复提交密度或 fix-after-touch 模式的文件 |
| Bus factor | 单一主导作者（>80% 提交）的文件 |
| Module health | 目录封装评分（跨边界耦合违规） |

#### 12.2.6 代码搜索

- **n-gram 索引**：`skim search index` 构建感知 AST 的搜索索引（增量 SHA-256 缓存，50K 文件上限）
- **时间排序**：`--hot`（热点）/`--cold`（最少变更）/`--risky`（修复密度）
- **Blast-radius 过滤**：`--blast-radius FILE` 预过滤为历史上与 FILE 共变的文件
- **AST 结构搜索**：`--ast "try-catch"` / `--ast "for_statement > await_expression"`

#### 12.2.7 性能

| 文件大小 | 行数 | 耗时 | 速度 |
|----------|------|------|------|
| 小 | 300 | 1.3ms | 4.3µs/line |
| 中 | 1500 | 6.4ms | 4.3µs/line |
| 大 | 3000 | 14.6ms | 4.9µs/line |

缓存命中时 48x 加速（244ms → 5ms）。缓存键：`SHA256(file path + mtime + mode)`。

#### 12.2.8 Skim 与本项目对比

| 维度 | Skim | 本项目 |
|------|------|--------|
| **定位** | 代码感知工具（agent 的 PreToolUse hook） | 代理层（Anthropic→OpenAI 转换 + 上下文管理） |
| **作用层** | 命令执行前（PreToolUse hook 重写命令） | 请求转发前（代理截断/压缩消息） |
| **AST 解析** | tree-sitter，17 种语言 | 无（纯文本处理） |
| **变换模式** | 6 级（Full→Types，15-95% 压缩） | 无代码变换（只有消息级截断/摘要） |
| **输出压缩** | 测试/构建/lint/git 三级降级 | 无（error_translation 只重写消息） |
| **git diff** | AST 感知（函数边界 + 变更标记） | 无 |
| **代码搜索** | n-gram + 时间排序 + blast-radius | 无（只有 BM25 MVP 关键词索引） |
| **集成方式** | PreToolUse hook / MCP server | HTTP 代理（127.0.0.1:4000） |
| **语言** | Rust（编译为本地二进制） | Python stdlib only |
| **目标** | 云端 API token 费用 + 注意力稀释 | 本地后端 OOM + 上下文窗口管理 |

> **关键差异**：Skim 作用于**代码内容**（在 agent 读取文件时压缩代码），本项目作用于**消息流**（在代理转发请求时截断历史）。两者作用层不同，**互补而非竞争**。Skim 解决"代码太大"问题，本项目解决"会话太长"问题。

**可借鉴策略**：
1. **短期：AST 感知文件压缩（Python 实现）**。本项目的 Read tool_result 经常包含完整文件（如 `anthropic_proxy.py` 3600 行）。可在代理层对 Read 结果做 AST 级压缩——保留函数签名 + 类定义 + 导入，剥离函数体。Python 有 `ast` 模块（stdlib），无需 tree-sitter。
2. **中期：三级降级输出压缩**。Bash tool_result 中的测试/构建输出可采用 structured → regex → passthrough 三级策略。比当前的 `[cleared: ...]` 更智能。
3. **⚠️ 注意**：Skim 的 PreToolUse hook 模式需要 agent 支持 hook 机制。本项目作为 HTTP 代理，无法直接安装 hook——但可以在代理层对 tool_result 做 AST 压缩，效果类似。

---

### 12.3 Token Reducer（Madhan230205/token-reducer）—— 混合 RAG 上下文压缩（补充研究）

| 属性 | 值 |
|------|-----|
| 仓库 | [Madhan230205/token-reducer](https://github.com/Madhan230205/token-reducer) |
| 语言 | Python |
| 许可证 | MIT |
| Stars | 28 |
| 最近更新 | 2026-06-15（活跃维护） |
| 集成 | Claude Code 插件（`/plugin install`） |
| 依赖 | SQLite FTS5 + tree-sitter（可选）+ sentence-transformers（可选） |

#### 12.3.1 核心架构：混合 RAG 管线

```
PREPROCESS → INDEX → RETRIEVE → RE-RANK → COMPRESS → CONTEXT PACKET
```

查询流程：`Query → FTS(BM25) → (Vector fallback if needed) → Merge → Top 5 → Compress`

**关键技术**：
- **BM25 + ONNX 向量搜索**：混合检索，BM25 为主，向量搜索为 fallback
- **Tree-sitter AST 分块**：Python/TS/Go/Rust/Java/C/C++/Ruby（可选，无依赖时用 regex 分块）
- **TextRank 压缩**：图基句子评分，智能摘要
- **Import Graph**：无需 LSP 自动映射文件依赖
- **2-Hop Symbol Expansion**：自动"go-to-definition"扩展引用的函数
- **Semantic Clustering**：语义聚类避免冗余

#### 12.3.2 零依赖模式

无 ML 库时仍可运行：
- `--embedding-backend hash`：哈希嵌入替代神经网络
- Regex 分块替代 tree-sitter AST
- SQLite FTS5 仍可用于 BM25 检索

#### 12.3.3 Claude Code 插件集成

```bash
# 一行安装
/plugin marketplace add Madhan230205/token-reducer
/plugin install token-reducer@Madhan230205-token-reducer
```

插件结构包含 `.claude-plugin/plugin.json`、`.mcp.json`、`hooks/`、`commands/`、`agents/`、`skills/`。

#### 12.3.4 配置参数（40+）

| 关键参数 | 默认值 | 说明 |
|----------|--------|------|
| `chunkSizeWords` | 220 | 每块目标词数 |
| `embeddingBackend` | "ml" | "ml"（神经网络）或 "hash"（零依赖） |
| `hybridMode` | "fallback" | "fallback"（向量补充）或 "always"（总是向量） |
| `astChunkingEnabled` | true | 使用 tree-sitter AST 分块 |
| `textRankEnabled` | true | 图基句子评分 |
| `importGraphEnabled` | true | 文件依赖追踪 |
| `twoHopExpansionEnabled` | true | 自动扩展引用符号 |
| `compressionWordBudget` | 350 | 压缩输出最大词数 |

#### 12.3.5 Token Reducer 与本项目对比

| 维度 | Token Reducer | 本项目 |
|------|---------------|--------|
| **定位** | 代码库检索 + 压缩（RAG 管线） | 代理层（消息流截断/压缩） |
| **作用层** | 查询前（RAG 检索相关代码片段） | 请求转发前（截断历史消息） |
| **检索** | BM25 + ONNX 向量（混合 RAG） | BM25 MVP（关键词索引注入） |
| **分块** | Tree-sitter AST 分块 | 无（消息级处理） |
| **压缩** | TextRank 图基句子评分 | LLM 摘要（`_compress_middle_with_llm`） |
| **依赖图** | Import Graph + 2-Hop Expansion | 无 |
| **存储** | SQLite FTS5 + HNSW | 无持久化（stateless） |
| **集成** | Claude Code 插件 | HTTP 代理 |
| **依赖** | tree-sitter + sentence-transformers（可选） | Python stdlib only |

> **关键差异**：Token Reducer 是**代码库 RAG 工具**——在查询前检索相关代码片段，减少需要发送的代码量。本项目是**会话历史管理工具**——在请求转发时截断/压缩历史消息。两者解决不同问题。

**可借鉴策略**：
1. **短期：BM25 检索增强**。本项目已有 `PROXY_HISTORY_INDEX=rule`（BM25 MVP）。可参考 Token Reducer 的 SQLite FTS5 方案，将关键词索引从进程内 dict 升级为持久化 FTS5 索引。
2. **中期：AST 分块用于 Read 结果**。与 Skim 的策略一致——对 Read tool_result 做 AST 分块，只保留相关块。
3. **⚠️ 依赖约束**：Token Reducer 使用 tree-sitter + sentence-transformers（非 stdlib）。本项目 stdlib only 约束下，AST 分块可用 Python `ast` 模块（仅限 Python 文件），向量搜索不可用（需保留 hash 嵌入或跳过）。

---

### 12.4 P2 研究结论与行动项（Headroom-ai + Skim + Token Reducer）

| 优先级 | 改进点 | 来源 | 预期收益 | 实现难度 | 许可证风险 |
|--------|--------|------|----------|----------|------------|
| **★★★ 短期** | **可逆压缩（CCR 思路）** | Headroom-ai CCR | **解决死亡循环**——存储原始 tool_result，模型可按需检索 | 中（进程内缓存 + retrieval 提示） | 无（Apache-2.0 策略参考） |
| **★★★ 短期** | **ContentRouter 内容路由** | Headroom-ai ContentRouter | 按内容类型选最优压缩策略 | 中（类型检测 + 分支压缩） | 无（Apache-2.0 策略参考） |
| **★★☆ 短期** | Read 结果 AST 压缩（Python `ast`） | Skim Structure 模式 | Read 大文件场景节省 60-80% | 中（Python `ast` 模块，仅 .py 文件） | 无（MIT 策略参考） |
| **★★☆ 短期** | 三级降级 Bash 输出压缩 | Skim 三级降级 | 测试/构建输出场景节省 50-70% | 中（structured → regex → passthrough） | 无（MIT 策略参考） |
| **★★☆ 中期** | SmartCrusher JSON 压缩 | Headroom-ai SmartCrusher | 工具输出 JSON 压缩 30-50% | 中（JSON 结构压缩，stdlib `json`） | 无（Apache-2.0 策略参考） |
| **★★☆ 中期** | headroom learn 失败学习 | Headroom-ai learn | 自动从失败会话学习，写 AGENTS.md | 中（失败模式检测 + 文件写入） | 无（Apache-2.0 策略参考） |
| **★☆☆ 中期** | BM25 升级为 SQLite FTS5 | Token Reducer 混合 RAG | 持久化关键词索引，跨请求复用 | 中（SQLite FTS5 是 stdlib） | 无（MIT 策略参考） |
| **★☆☆ 中期** | AST 感知 git diff 压缩 | Skim git diff | diff 场景节省 40-60% | 高（需 tree-sitter 或 `ast` 模块） | 无（MIT 策略参考） |
| **★☆☆ 长期** | CodeCompressor AST（多语言） | Headroom-ai CodeCompressor | 代码文件压缩 60-80%（多语言） | 高（需 tree-sitter） | 无（Apache-2.0 策略参考） |
| **★☆☆ 长期** | 混合 RAG（BM25 + 向量） | Token Reducer | 语义检索能力 | **极高**（需非 stdlib 依赖） | 无（MIT 策略参考） |
| **★☆☆ 长期** | 跨 agent 记忆 | Headroom-ai Cross-agent memory | 跨会话/跨 agent 上下文复用 | **极高**（需持久化 + 嵌入） | 无（Apache-2.0 策略参考） |

> **关键结论**：
> 1. **Headroom-ai（★31,630）是本研究中最重要发现**——它是目前 GitHub 上 stars 最多的上下文压缩工具，Apache-2.0，提供 library/proxy/MCP 三种模式，与本项目架构**直接可比**。其 CCR 可逆压缩、ContentRouter 内容路由、SmartCrusher JSON 压缩是本项目最值得借鉴的方向。
> 2. **CCR 可逆压缩是解决死亡循环的关键**。本项目的 `[cleared: ...]` 占位符导致死亡循环（§3 已分析），Headroom 的 CCR 思路——"压缩但保留原始，模型可按需检索"——可在 stdlib 约束下通过进程内缓存实现，是**性价比最高的改进方向**。
> 3. **Skim 和 Token Reducer 作用于代码内容/代码库检索，本项目作用于会话历史消息流——互补**。Skim 的 AST 压缩策略可通过 Python `ast` 模块在代理层对 Read 结果实现（仅 .py 文件）。Token Reducer 的 BM25 思路可升级本项目的关键词索引。
> 4. **本项目 vs Headroom-ai 的核心差异是依赖约束**：Headroom 使用 torch/transformers/tree-sitter/hnswlib 等重依赖，本项目 Python stdlib only。这意味着 Headroom 的 ML 模型压缩（Kompress-base）和语义记忆无法直接采用，但其**架构思路**（CCR、ContentRouter、CacheAligner）可在 stdlib 约束下独立实现。
> 5. **"Headroom"误识已二次更正**：§7 正确识别了 Headroom-ai（chopratejas/headroom, Apache-2.0, ★31,630），本节初稿错误地将其判为误识（因搜索了 `headroom` 而非 `headroom-ai`）。现已确认 Headroom-ai 是真实、成熟、直接可比的竞品。

---

## 13. P3 研究：LLMLingua-2 质量风险分析（确认不采用）

### 13.1 项目概况

| 属性 | 值 |
|------|-----|
| 仓库 | [microsoft/LLMLingua](https://github.com/microsoft/LLMLingua) |
| 语言 | Python |
| 许可证 | MIT |
| Stars | 6,308 |
| 最近更新 | 2026-06-17（活跃维护） |
| 论文 | EMNLP'23 (LLMLingua), ACL'24 (LongLLMLingua), ACL'24 Findings (LLMLingua-2) |
| 集成 | LangChain, LlamaIndex, Prompt flow |

### 13.2 技术方案

LLMLingua 系列采用**ML 模型驱动的 token 级压缩**：

| 版本 | 方法 | 模型 | 压缩率 | 速度 |
|------|------|------|--------|------|
| LLMLingua (EMNLP'23) | 基于 perplexity 的 token 丢弃 | GPT2-small / LLaMA-7B | 最多 20x | 基准 |
| LongLLMLingua (ACL'24) | 缓解"lost in the middle" | 同上 | 1/4 tokens, RAG +21.4% | 基准 |
| **LLMLingua-2** (ACL'24 Findings) | **GPT-4 数据蒸馏 + BERT token 分类** | **BERT-level encoder** | **最多 20x** | **3-6x 更快** |

**LLMLingua-2 核心方法**：
1. 用 GPT-4 生成压缩训练数据（数据蒸馏）
2. 训练 BERT-level encoder 做二分类：每个 token 保留/丢弃
3. 推理时用小模型对 prompt 做 token 级压缩

### 13.3 依赖分析

```python
# setup.py INSTALL_REQUIRES
INSTALL_REQUIRES = [
    "transformers>=4.26.0",  # HuggingFace transformers（非 stdlib）
    "accelerate",            # ML 加速库（非 stdlib）
    "torch",                 # PyTorch（非 stdlib，~2GB）
    "tiktoken",              # OpenAI tokenizer（非 stdlib）
    "nltk",                  # NLP 工具包（非 stdlib）
    "numpy",                 # 数值计算（非 stdlib）
]
```

### 13.4 质量风险分析

#### 13.4.1 Token 级压缩破坏结构化数据

LLMLingua-2 在 **token 级别**删除"非必要" token。这对结构化数据是灾难性的：

| 数据类型 | 压缩前 | 压缩后（token 丢弃） | 问题 |
|----------|--------|----------------------|------|
| **Tool schema (JSON)** | `{"name": "Read", "input_schema": {"type": "object"}}` | `{"name "Read" input_schema" "type" "object"}` | JSON 语法破坏，无法解析 |
| **Code** | `def foo(x: int) -> str: return str(x)` | `def foo(x int) str return str(x)` | 语法破坏，无法执行 |
| **Tool result** | `{"path": "/foo/bar", "content": "..."}` | `{"path" "/foo/bar" content" "..."}` | JSON 破坏 |
| **XML/HTML** | `<tool_use id="abc">...</tool_use>` | `<tool_use id="abc">...</tool_use>` | 标签可能被截断 |

> **关键问题**：agentic coding 上下文中 60-80% 是结构化数据（tool_use/tool_result/JSON/code）。
> token 级压缩会破坏这些数据的语法完整性，导致模型无法理解工具定义和结果。

#### 13.4.2 学术评估 vs. agentic 场景差距

LLMLingua-2 的学术评估场景：
- **RAG 文档压缩**：自然语言文档，token 丢弃后仍可理解
- **Meeting 摘要**：自然语言对话，冗余度高
- **CoT 推理**：推理链中的冗余词

本项目场景：
- **Tool schema**：JSON 结构，无冗余 token
- **Code 文件**：语法严格，token 丢弃破坏可执行性
- **Tool result**：结构化输出，token 丢弃破坏数据完整性
- **System prompt**：精确指令，token 丢弃改变语义

#### 13.4.3 性能与资源开销

| 维度 | LLMLingua-2 | 本项目约束 |
|------|-------------|------------|
| **模型大小** | BERT encoder ~110MB-440MB | 无模型（纯规则） |
| **推理延迟** | 每次 prompt 需 BERT 前向传播 | 代理需 <100ms 处理 |
| **内存占用** | PyTorch + BERT ~500MB-2GB | 48GB Mac 已面临 OOM |
| **依赖体积** | torch + transformers ~2-3GB | Python stdlib only |

> **致命冲突**：本项目运行在 48GB 统一内存的 Apple Silicon 上，**已经面临 Metal OOM 风险**
> （见 §4 第 5 点）。加载 PyTorch + BERT 模型（额外 500MB-2GB）会加剧内存压力，
> 与本项目的核心目标（防止 OOM）完全矛盾。

### 13.5 不采用确认

| 否决理由 | 严重程度 | 说明 |
|----------|----------|------|
| **Python stdlib only 约束** | **致命** | torch/transformers/numpy 均非 stdlib |
| **结构化数据破坏** | **致命** | token 级压缩破坏 JSON/code/tool schema 语法 |
| **内存冲突** | **致命** | 48GB Mac 已 OOM，加载 ML 模型加剧问题 |
| **延迟开销** | 高 | BERT 前向传播增加每次请求延迟 |
| **场景不匹配** | 高 | 设计用于 RAG 文档，非 agentic 消息流 |
| **模型下载** | 中 | 需下载 BERT 权重（非离线友好） |

> **结论：确认不采用 LLMLingua-2**。
> 理由：三个致命冲突（stdlib 约束、结构化数据破坏、内存冲突）无法解决。
> LLMLingua-2 的 token 级压缩方法适用于自然语言文档压缩（RAG 场景），
> **不适用于包含大量结构化数据（tool schema/code/JSON）的 agentic coding 上下文**。

---

## 14. 研究汇总：全景对比与改进路线图

### 14.1 全景对比矩阵

| 工具 | 语言 | 许可证 | Stars | 压缩粒度 | 作用层 | 目标场景 | 本项目可借鉴度 |
|------|------|--------|-------|----------|--------|----------|----------------|
| **Headroom-ai** | Python+TS | Apache-2.0 | **31,630** | 内容路由（6 算法） | Library/Proxy/MCP | 云端 API token 费用 | **★★★★★**（CCR+ContentRouter+CacheAligner） |
| **DCP** | TypeScript | AGPL-3.0 | — | 消息段（range） | 模型调用 compress 工具 | 通用会话管理 | ★★★☆☆（策略参考，代码不可用） |
| **Kompact** | Python | MIT | — | 消息内容（8 变换） | 代理管线 | 云端 API token 费用 | ★★★★☆（3 短期 + 3 中期） |
| **TokenSieve** | Go | MIT | — | 文件块/堆栈（指纹去重） | 代理管线 | 云端 API token 费用 | ★★★★☆（文件块去重） |
| **Skim** | Rust | MIT | 27 | 代码 AST（6 模式） | PreToolUse hook | 代码内容压缩 | ★★★☆☆（AST 压缩策略） |
| **Token Reducer** | Python | MIT | 28 | 代码块（RAG 检索） | Claude Code 插件 | 代码库检索 | ★★☆☆☆（BM25 升级策略） |
| **LLMLingua-2** | Python | MIT | 6,308 | Token 级（ML 模型） | 代理管线 | RAG 文档压缩 | ★☆☆☆☆（**不采用**，3 致命冲突） |
| **本项目** | Python | — | — | 消息级（5 阶段 + 8 层管线） | HTTP 代理 | 本地后端 OOM 管理 | — |

### 14.2 改进路线图（汇总）

#### 短期（P1+P2 可落地，无需架构改动）

| # | 改进点 | 来源 | 预期收益 | 难度 | 约束兼容 |
|---|--------|------|----------|------|----------|
| **0** | **可逆压缩（CCR 思路）** | **Headroom-ai CCR** | **解决死亡循环**——存储原始 tool_result，模型可按需检索 | 中 | ✅ stdlib（进程内缓存） |
| **0b** | **ContentRouter 内容路由** | **Headroom-ai ContentRouter** | 按内容类型选最优压缩策略 | 中 | ✅ stdlib |
| 1 | UUID + system 路径归一化 | Kompact Cache Aligner | prefix cache 命中率 +5-15% | 低 | ✅ stdlib |
| 2 | tool_result 占位符增强 | Kompact Observation Masker | 减少死亡循环 | 低 | ✅ stdlib |
| 3 | 文件块指纹去重（进程内） | TokenSieve file-block dedup | 反复 Read 节省 20-40% | 中 | ✅ stdlib |
| 4 | 工具参数级去重 | DCP deduplication | 精确识别重复工具调用 | 中 | ✅ stdlib |
| 5 | 过期错误输入剪枝 | DCP purge-errors | 清理 4+ 轮前错误工具 | 低 | ✅ stdlib |
| 6 | LLM 摘要 prompt 增强 | DCP compress-range prompt | 提升摘要质量 | 低 | ✅ stdlib |
| 7 | Read 结果 AST 压缩（Python `ast`） | Skim Structure 模式 | Read 大文件节省 60-80% | 中 | ✅ stdlib |

#### 中期（需适度开发）

| # | 改进点 | 来源 | 预期收益 | 难度 | 约束兼容 |
|---|--------|------|----------|------|----------|
| 8 | SmartCrusher JSON 压缩 | Headroom-ai SmartCrusher | 工具输出 JSON 压缩 30-50% | 中 | ✅ stdlib (`json`) |
| 9 | headroom learn 失败学习 | Headroom-ai learn | 自动从失败会话学习 | 中 | ✅ stdlib |
| 10 | 堆栈跟踪去重 | TokenSieve stack-trace dedup | 重复错误节省 10-20% | 中 | ✅ stdlib |
| 11 | TF-IDF 工具选择 | Kompact Schema Optimizer | 工具 50+ 时更灵活 | 中 | ✅ stdlib |
| 12 | HTML 导航剥离 | Kompact HTML Stripper | WebFetch 节省 30-50% | 中 | ✅ stdlib |
| 13 | 三级降级 Bash 输出压缩 | Skim 三级降级 | 测试/构建输出节省 50-70% | 中 | ✅ stdlib |
| 14 | BM25 升级为 SQLite FTS5 | Token Reducer 混合 RAG | 持久化关键词索引 | 中 | ✅ stdlib (`sqlite3`) |
| 15 | 嵌套摘要占位符 `(sN)` | DCP `(bN)` 机制 | 多层累积摘要 | 高 | ⚠️ 需 session state |
| 16 | AST 感知 git diff 压缩 | Skim git diff | diff 场景节省 40-60% | 高 | ✅ stdlib (`ast`) |

#### 长期（需架构级决策）

| # | 改进点 | 来源 | 预期收益 | 难度 | 约束兼容 |
|---|--------|------|----------|------|----------|
| 17 | 模型可调用 compress 工具 | DCP compress 工具 | 模型自主管理上下文 | 极高 | ⚠️ 需 API 接口改造 |
| 18 | 会话状态持久化 | DCP session state | 嵌套摘要 + 跨请求去重 | 极高 | ⚠️ 需改 stateless 架构 |
| 19 | 跨 agent 记忆 | Headroom-ai Cross-agent memory | 跨会话/跨 agent 上下文复用 | 极高 | ❌ 需持久化 + 嵌入 |
| 20 | 混合 RAG（BM25 + 向量） | Token Reducer | 语义检索能力 | 极高 | ❌ 需非 stdlib 依赖 |
| 21 | Import Graph + 2-Hop | Token Reducer LSP-Killer | 自动扩展相关定义 | 极高 | ❌ 需代码分析引擎 |

### 14.3 优先级排序建议

**第零优先（短期 #0-0b，立即启动——最高价值）**：
- **0. 可逆压缩（CCR 思路）** — 进程内缓存 `dict[tool_use_id, original_content]`，占位符中嵌入检索提示，让模型知道可按需检索原始内容。**这是解决死亡循环的关键改进**。
- **0b. ContentRouter 内容路由** — 检测 tool_result 内容类型（JSON/code/text/log），按类型选最优压缩策略（JSON 结构压缩 / AST 压缩 / 文本摘要 / 日志三级降级）。

**第一优先（短期 #1-3，即可落地）**：
1. UUID/路径归一化 — 扩展 `_normalize_dates`，加 UUID/`/Users/`/`/tmp/` 归一化
2. 占位符增强 — 将 `[cleared: ...]` 改为 `[Output omitted — N tokens. Starts with: <preview>]`（与 CCR 协同）
3. 文件块去重 — 在 `_compress_content_pass` 前加 SHA-256 文件块去重

**第二优先（短期 #4-7，1-2 周内）**：
4. 工具参数去重 — 签名 = `tool_name + hash(sorted_json(params))`，保留最新
5. 过期错误剪枝 — 轮次年龄 >= 4 的错误工具替换输入
6. LLM 摘要 prompt — 增加 EXHAUSTIVE + USER INTENT FIDELITY + LEAN 要求
7. Read AST 压缩 — Python `ast` 模块对 .py 文件做 Structure 模式

**第三优先（中期 #8-16，1-3 个月）**：
- SmartCrusher JSON 压缩、headroom learn 失败学习
- 逐步引入堆栈去重、TF-IDF 工具选择、HTML 剥离、三级降级输出压缩
- BM25 升级 SQLite FTS5
- 嵌套摘要占位符（需评估 session state 架构）

**长期探索（#17-21）**：
- 模型可调用 compress 工具（需 API 接口改造）
- 会话状态持久化 + 跨 agent 记忆（需改 stateless 架构 + 持久化）
- 混合 RAG / Import Graph（需评估是否放宽 stdlib 约束）

### 14.4 最终结论

1. **Headroom-ai（★31,630, Apache-2.0）是本研究最重要的发现**——它是目前 GitHub 上 stars 最多的上下文压缩工具，提供 library/proxy/MCP 三种模式，与本项目的 HTTP 代理架构**直接可比**。其 **CCR 可逆压缩**和 **ContentRouter 内容路由**是本项目最值得借鉴的方向，均可在 Python stdlib 约束下独立实现。

2. **本项目在本地后端上下文管理领域仍无直接竞争者**。Headroom-ai 和其他工具都 targeting 云端 API token 费用，无一解决 Apple Silicon 本地后端的 OOM/Metal/prefix-cache 问题。但 Headroom-ai 的 `[pytorch-mps]` extra 表明它已开始适配 Apple Silicon，是**潜在的未来竞争者**。

3. **最值得借鉴的短期改进有 9 项**（#0-7），全部兼容 Python stdlib only 约束。其中 **CCR 可逆压缩**（#0）是**最高价值改进**——解决死亡循环问题；ContentRouter（#0b）让压缩策略更智能。其余 7 项（UUID 归一化、占位符增强、文件块去重等）可立即落地。

4. **DCP 的嵌套摘要机制是最值得关注的长期方向**，但需要引入 session state（与 stateless 架构冲突）。AGPL-3.0 许可证排除了代码复用，只能策略参考。

5. **Skim 的 AST 压缩策略可通过 Python `ast` 模块在代理层实现**——对 Read tool_result 中的 .py 文件做 Structure 模式压缩（保留签名/类/导入，剥离函数体），是性价比最高的中期改进。

6. **LLMLingua-2 确认不采用**——三个致命冲突（stdlib 约束、结构化数据破坏、内存冲突）无法解决。token 级 ML 压缩适用于自然语言文档，不适用于 agentic coding 的结构化上下文。

7. **所有可借鉴策略均为宽松许可证**（Headroom-ai Apache-2.0、Kompact/TokenSieve/Skim/Token Reducer MIT），无许可证风险。DCP 为 AGPL-3.0，仅策略参考，代码不可复用。

---

## 15. 硬件约束影响性分析：MacBook Pro M5 Pro 48GB 统一内存

> 分析日期：2026-06-18
> 硬件：MacBook Pro M5 Pro，48GB 统一内存（Apple Silicon Metal）
> 主用模型：Qwen3.6-35B-A3B-4bit（MoE，256 experts / 3 active）
> 数据来源：`configs/rapid-mlx-35b.conf`、`rapid-mlx-cache-analysis.md`、`prefix-cache-analysis-20260605.md`、生产日志实测

本节回答：**48GB 统一内存这一硬约束，对 KV cache 容量、上下文长度期望、以及前述竞品策略（§10-§14）的可采纳性有何影响？**

### 15.1 内存预算分解（实测口径）

#### 15.1.1 48GB 统一内存的归属

| 归属 | 占用 | 说明 |
|------|------|------|
| macOS 系统基线 | ~6-8 GB | WindowServer、内核、常驻进程 |
| **可用于 ML 的总量** | **~40-42 GB** | 48 - 系统基线 |
| 模型权重（Qwen3.6-35B-A3B-4bit） | ~14-18 GB | 4-bit 量化 MoE，CONFIG_MEMORY 标注 ~14-18GB |
| Prefix cache（`--cache-memory-mb 4096`） | 4 GB | 硬上限，LRU 淘汰 |
| **KV cache + prefill 激活 剩余预算** | **~18-24 GB** | 40 - 18 - 4（下限口径） |
| `--gpu-memory-utilization 0.75` 软上限 | ~36 GB | Metal `allocation_limit`，含模型+KV+cache+激活 |
| 实测峰值（limit=28GB/0.60 时） | 33-39 GB | **超限 20-40%**——allocation_limit 是软目标非硬墙 |

**关键事实**：`allocation_limit` 是**软目标**。Prefill 阶段的激活内存可超限 20-40%，这是 OOM 的直接来源（非 KV 存储本身）。OOM 签名：`[METAL] Command buffer execution failed: Insufficient Memory`。

#### 15.1.2 KV cache 每 token 成本（三种量化档位实测）

| 量化档位 | 每 token 成本 | 60K tokens 实占 | 压缩倍数 | 来源 |
|----------|-------------|----------------|---------|------|
| FP16（无量化） | ~46.3 KB/tok | ~2.8 GB | 1× | `rapid-mlx-35b.conf:43`（84K=3.8GB 反算） |
| 8-bit | ~37 KB/tok | ~2.2 GB | 1.25× | `prefix-cache-analysis-20260605.md` §4.3 |
| **4-bit TurboQuant** | **~5.4 KB/tok** | **~327 MB** | **~8.5×** | 同上，实测增量 |

当前生产配置（`rapid-mlx-35b.conf`）启用 `RAPID_MLX_KV_QUANTIZATION=true` + `KV_QUANT_BITS=4`，即采用 4-bit 档位。

> ⚠️ **TurboQuant 与 prefix cache 持久化冲突**：`--kv-cache-turboquant` 启用的 `TurboQuantKVCache` 缺少 `state` 属性，导致 cache 保存失败（见 AGENTS.md 警告）。当前 35b 配置移除了 turboquant CLI flag，改用标准 4-bit KV（PolarQuant），每 token 成本略高于 5.4 KB/tok 但支持持久化。

### 15.2 KV cache 容量 vs 上下文长度期望

#### 15.2.1 理论 KV 容量（4-bit 档位，无 prefix cache 预留）

| KV 预算 | 可容纳 tokens | 对比 `LLAMA_CTX=131072` |
|---------|--------------|------------------------|
| 4 GB | ~740 K | 5.6× 上下文上限 |
| 10 GB | ~1.85 M | 14× |
| 14 GB | ~2.6 M | 20× |
| 18 GB | ~3.3 M | 25× |

**结论：在 4-bit KV 量化下，KV cache 存储已不是上下文长度的瓶颈。** 48GB 机器理论上可支撑 50万+ tokens 的 KV 存储。

#### 15.2.2 实际可达上下文长度——三个真实瓶颈

理论 KV 容量充裕，但**实际可用上下文远低于此**，受三个瓶颈制约：

| 瓶颈 | 机制 | 实测数据 | 对上下文长度的影响 |
|------|------|---------|------------------|
| **① Prefill 时间** | 线性增长，无 prefix cache 命中 | 60-80K tokens = **55-75s**；131K ≈ 120-150s；180K ≈ 165-210s | 单轮交互延迟超 60s 即影响 agentic 体验；超 120s 接近不可用 |
| **② Prefill 激活内存** | 超限 20-40%，软目标非硬墙 | limit=28GB → 峰值 33-39GB；**2 个 >38K 并发请求必 OOM** | 激活随 prompt 长度增长，是 OOM 主因，非 KV 存储 |
| **③ Prefix cache 0% 命中** | MoE `ArraysCache` 不可修剪 | LCP 找到 29K 公共前缀但被 `non_trimmable=True` 跳过 | **每轮都全量 prefill**，无加速，且 cache 占 4GB 纯浪费 |

#### 15.2.3 上下文长度期望校准

| 配置层 | 参数 | 设定值 | 实际可达 | 差距原因 |
|--------|------|--------|---------|---------|
| 后端 | `LLAMA_CTX` | 131,072 | ~80-100K 可用（prefill <75s） | prefill 时间限制 |
| 代理 | `PROXY_CTX_CHARS_LIMIT` | 150,000 chars（35b） | 生效 | ≈ 60-75K tokens |
| 代理 | `PROXY_PRE_TRUNCATE_CHARS` | 400,000 chars | 生效 | ≈ 160-200K tokens 硬截断 |
| 云端 | `deepseek-chat.conf` | 500,000 chars | 无本地限制 | 云端无 prefill/OOM |

**校准结论**：48GB 硬件下，**单请求实际可用上下文 ≈ 60-100K tokens**（受 prefill 时间约束），远低于 `LLAMA_CTX=131072` 的标称值和 KV 存储的理论上限。代理层 `PROXY_CTX_CHARS_LIMIT=150000`（≈60-75K tokens）是符合硬件实际的安全设定。

### 15.3 硬件约束对各竞品策略的过滤

将 §10-§14 的改进路线图按"48GB 约束兼容性"重新过滤：

#### 15.3.1 硬件约束下的三档分类

| 档位 | 判据 | 策略 | 说明 |
|------|------|------|------|
| 🟢 **直接受益** | 减少 prompt tokens → 线性减少 prefill 时间 + 激活内存 + OOM 风险 | CCR(#0)、ContentRouter(#0b)、UUID归一化(#1)、占位符增强(#2)、文件块去重(#3)、工具参数去重(#4)、过期错误剪枝(#5)、AST压缩(#7)、SmartCrusher(#8) | **token 减少是 48GB 机器的唯一高杠杆** |
| 🟡 **中性/低价值** | 不影响 token 数或依赖 prefix cache 命中 | TF-IDF工具选择(#11)、HTML剥离(#12)、BM25升级(#14) | 工具选择/HTML 剥离减少的是工具定义 tokens，收益小；BM25 是索引层，与 prefill 无关 |
| 🔴 **被硬件阻塞** | 需要更多内存或依赖 prefix cache 机制 | prefix cache 稳定化、并发提升、LLMLingua-2(#P3)、Headroom-ai `[pytorch-mps]` embedder | 见下详述 |

#### 15.3.2 被硬件阻塞的策略详述

**（a）Prefix cache 稳定化策略——低价值**
- Kompact Cache Aligner、DCP 非破坏性历史等策略旨在提升 prefix cache 命中率
- 但本机 Qwen3.6-35B-A3B 的 MoE `ArraysCache` 不可修剪，**LCP 匹配被强制跳过，命中率 0%**（`rapid-mlx-cache-analysis.md` §4-§5）
- 即使代理层完美稳定前缀，后端也无法命中 → **此类策略在当前模型上收益为零**
- 唯一例外：若切回纯 Transformer 模型（如 Qwen3.5-27B 非 MoE），cache trim 可能正常，此时策略恢复价值

**（b）并发提升——硬件锁死**
- `PROXY_MAX_CONCURRENT` 已从 4 降至 1（`rapid-mlx-35b.conf:49`）
- 实测：2 个大上下文（>38K tokens）并发请求**必然 OOM**（prefill 激活叠加）
- 9B 模型曾试 `MAX_CONCURRENT=2`：内存 peak 26.7GB，forced cache clear 44次/1.5h，生成速度降 50%+
- **48GB 统一内存 + Metal 单 GPU 时间切片 = 并发被硬件锁死在 1**，任何依赖并发的策略（如 Headroom-ai Proxy 多请求流水线）无法采纳

**（c）LLMLingua-2——双重阻塞**
- 内存冲突：加载 PyTorch + BERT encoder ~500MB-2GB，48GB 已近极限（模型 18GB + KV + cache + 激活），**embedder 与推理模型无法共存**
- 与 §13 结论一致：stdlib 约束 + 结构化数据破坏 + 内存冲突，三重致命

**（d）Headroom-ai `[pytorch-mps]` embedder——内存冲突**
- Headroom-ai 的 memory 模式需要 `sentence-transformers` + `hnswlib`（embedder 常驻 GPU）
- `[pytorch-mps]` extra 让 embedder 用 Apple Silicon GPU——**与本地推理模型争抢同一块 GPU**
- 48GB 下模型已占 18GB，embedder 再占 1-2GB 会进一步挤压 prefill 激活预算
- **结论**：可借鉴 Headroom-ai 的 CCR/ContentRouter 算法思路（纯逻辑，无 embedder），但**不可引入其 memory 模式的 embedder 依赖**

### 15.4 硬件约束对 KV cache 策略的直接影响

#### 15.4.1 KV 量化档位选择权衡

| 档位 | 内存 | 精度风险 | prefix cache 持久化 | 适用场景 |
|------|------|---------|-------------------|---------|
| FP16 | 最高（46 KB/tok） | 无 | ✅ | 小上下文 + 高质量要求 |
| 8-bit | 中（37 KB/tok） | 低 | ✅ | 平衡 |
| **4-bit PolarQuant** | **最低（~6-8 KB/tok）** | **中**（长上下文末端可能退化） | ✅ | **当前生产**（35b 配置） |
| 4-bit TurboQuant | 最低（5.4 KB/tok） | 中 | ❌（`state` 缺失） | 已弃用（见 AGENTS.md 警告） |

**结论**：当前 4-bit PolarQuant 是 48GB 下的最优档位——KV 存储不再是瓶颈，且支持 cache 持久化。精度风险在 agentic coding 场景可接受（工具调用结构比长程推理更鲁棒）。

#### 15.4.2 Prefix cache 内存——纯浪费 but 保留

- 当前 `--cache-memory-mb 4096` 预留 4GB prefix cache
- MoE 不可修剪导致 agentic 场景 0% 命中 → **这 4GB 实际被浪费**
- 但保留理由：exact match（重复请求/benchmark）仍可命中；且移除 cache 释放的 4GB 转给 prefill 激活，**不改变 OOM 风险**（激活超限是动态的，多 4GB 预算会被更大 prompt 消耗）
- **可调方向**：若确认不做 benchmark，可将 `cache-memory-mb` 降至 1024-2048，释放 2-3GB 给激活预算，**降低单请求 OOM 概率但无 token 收益**

#### 15.4.3 上下文长度 vs prefill 时间——不可调和的矛盾

| 目标 | 要求 | 48GB 是否满足 |
|------|------|--------------|
| 131K 上下文（`LLAMA_CTX` 标称） | KV 4-bit: ~0.7GB（够）；prefill: ~120-150s | ⚠️ KV 够，**时间不可接受** |
| 200K 上下文 | KV: ~1.1GB（够）；prefill: ~165-210s | ❌ 时间完全不可用 |
| 500K 上下文（云端 deepseek 等价） | KV: ~2.7GB（够）；prefill: ~400s+ | ❌ 本地不可行 |
| 1M 上下文（Headroom-ai/云端长程） | KV: ~5.4GB（够）；prefill: ~800s+ | ❌ 本地完全不可行 |

**核心矛盾**：4-bit KV 让"存得下"不再是问题，但"算得完"（prefill 时间）成为硬约束。**上下文长度的实际天花板由 prefill 时间决定，而非 KV 容量**。

### 15.5 硬件约束对改进路线图的重新排序

综合 15.1-15.4，对 §14.3 的优先级按"48GB 约束下的实际收益"重新排序：

#### 15.5.1 收益公式

在 48GB + 4-bit KV + 0% prefix cache 命中下，单请求总延迟近似：

```
TTFT ≈ k × prompt_tokens    （k ≈ 0.9-1.1 ms/tok，prefill 线性）
OOM风险 ∝ prompt_tokens²     （激活内存随长度超线性增长）
```

因此 **每减少 1K prompt tokens ≈ 节省 0.9-1.1s TTFT + 降低 OOM 概率**。这是量化所有策略收益的统一口径。

#### 15.5.2 重新排序（按 token 减少量 × 落地难度）

| 优先级 | 策略 | 预计 token 减少 | TTFT 收益 | 硬件兼容 | 说明 |
|--------|------|----------------|----------|---------|------|
| **P0** | **CCR 可逆压缩(#0)** | 间接（避免 re-read → 避免 re-prefill） | **避免 60K+ 全量重算** | 🟢 | 死亡循环每次 re-read = 55-75s 白费，CCR 消除之 |
| **P0** | **ContentRouter(#0b)** | 20-50%（按内容类型选压缩器） | 12-38s | 🟢 | JSON/代码/日志分别压缩，token 直降 |
| **P1** | 文件块去重(#3) | 20-40%（重复 Read） | 12-30s | 🟢 | agentic 反复 Read 同文件常见 |
| **P1** | AST 压缩(#7) | 60-80%（Read .py 文件） | 大文件 40-60s | 🟢 | Read 大文件占 prompt 大头 |
| **P1** | 占位符增强(#2) | 10-30%（tool_result 压缩） | 6-22s | 🟢 | 与 CCR 协同 |
| **P2** | SmartCrusher JSON(#8) | 30-50%（工具输出 JSON） | 18-38s | 🟢 | 工具输出多为 JSON |
| **P2** | 三级降级 Bash 输出(#13) | 50-70%（测试/构建日志） | 大日志 30-50s | 🟢 | 日志占 prompt 比例高 |
| **P2** | 工具参数去重(#4) | 5-15% | 3-11s | 🟢 | 精确去重 |
| **P3** | UUID/路径归一化(#1) | 0%（不减少 token） | **0s** | 🟡 | **prefix cache 0% 命中 → 此策略当前无收益**，仅未来切非 MoE 模型时有效 |
| **P3** | 过期错误剪枝(#5) | 5-10% | 3-7s | 🟢 | 清理噪声 |
| **P3** | HTML 剥离(#12) | 30-50%（WebFetch 场景） | 场景依赖 | 🟡 | 仅 WebFetch 频繁时有效 |
| ❌ | prefix cache 稳定化类 | 0% | 0s | 🔴 | MoE 不可修剪，0% 命中，当前无收益 |
| ❌ | 并发提升类 | — | — | 🔴 | 硬件锁死 MAX_CONCURRENT=1 |
| ❌ | LLMLingua-2 | — | — | 🔴 | 内存 + stdlib 双重阻塞 |
| ❌ | Headroom-ai embedder memory | — | — | 🔴 | GPU 争抢 |

#### 15.5.3 关键调整 vs §14.3 原排序

| 调整项 | §14.3 原排 | §15.5 新排 | 原因 |
|--------|-----------|-----------|------|
| **UUID/路径归一化(#1)** | 第一优先 | **降为 P3** | prefix cache 0% 命中，归一化无用武之地 |
| **CCR(#0)** | 第零优先 | **维持 P0** | 48GB 下 re-prefill 成本极高（55-75s），CCR 避免重算收益放大 |
| **ContentRouter(#0b)** | 第零优先 | **维持 P0** | token 直降 20-50%，直接缓解 prefill 时间 + OOM |
| **AST 压缩(#7)** | 第二优先 | **升至 P1** | Read 大文件是 agentic prompt 大头，60-80% 压缩直接降 prefill |
| **文件块去重(#3)** | 第一优先 | **维持 P1** | 反复 Read 场景常见 |
| **TF-IDF 工具选择(#11)** | 第三优先 | **降为 P3/暂缓** | 工具定义 tokens 占比小（5-8K），收益有限 |

### 15.6 结论

1. **48GB 统一内存 + 4-bit KV 量化下，KV cache 存储已不是上下文长度瓶颈。** 理论可存 50万+ tokens，但 prefill 时间（线性，~1ms/tok）将实际可用上下文限制在 **60-100K tokens**。

2. **三个真实瓶颈**：① prefill 时间（60-80K = 55-75s）；② prefill 激活内存（超限 20-40%，OOM 主因）；③ prefix cache 0% 命中（MoE `ArraysCache` 不可修剪，每轮全量重算）。

3. **并发被硬件锁死在 1**。48GB + Metal 单 GPU 时间切片下，2 个 >38K tokens 并发请求必 OOM。`PROXY_MAX_CONCURRENT=1` 是硬件强制的下限，任何依赖并发的策略无法采纳。

4. **token 减少是 48GB 机器的唯一高杠杆**。每减少 1K prompt tokens ≈ 节省 0.9-1.1s TTFT + 降低 OOM 概率。这使得 **CCR、ContentRouter、AST 压缩、文件块去重**成为最高价值改进——它们直接减少 prompt tokens，同时缓解时间与内存两个瓶颈。

5. **prefix cache 稳定化类策略在当前 MoE 模型上收益为零**。UUID/路径归一化（Kompact Cache Aligner 思路）从第一优先降为 P3——除非切回纯 Transformer 非 MoE 模型，否则此类策略无落地价值。

6. **不可采纳的策略**：LLMLingua-2（内存 + stdlib 双阻塞）、Headroom-ai embedder memory 模式（GPU 争抢）、任何并发提升方案（硬件锁死）。可借鉴的是纯逻辑算法（CCR/ContentRouter/SmartCrusher），不可引入其 ML/embedder 依赖。

7. **上下文长度期望校准**：本地 48GB 实际天花板 ≈ 60-100K tokens（prefill 时间约束），云端（deepseek）500K+ 无此限制。这一差距是本地 vs 云端的**根本能力差**，压缩策略只能优化本地体验，不能弥合此差距。若需 200K+ 上下文 agentic 工作流，云端是唯一选项。

---

## 16. 优化后长上下文处理时间预期（量化估算）

> 估算日期：2026-06-18
> 基线数据：§15 实测（prefill ~1ms/tok，prefix cache 0% 命中，MAX_CONCURRENT=1）
> 目的：将 §14 改进路线图落地后，给出长上下文 TTFT 的量化预期

### 16.1 估算口径与假设

**基线参数（实测口径，来自 `rapid-mlx-cache-analysis.md` §2.2 + §15.2.2）**：

| 参数 | 值 | 来源 |
|------|----|----|
| Prefill 速率 `k` | 0.9-1.1 ms/tok | 60-80K tokens → 55-75s 反算 |
| TTFT 公式 | `TTFT ≈ k × prompt_tokens` | prefix cache 0% 命中，每轮全量 prefill |
| OOM 风险 | `∝ prompt_tokens²` | 激活内存随长度超线性增长 |
| 典型 agentic prompt | 60K tokens（中位）/ 80K tokens（高位） | 生产日志 |
| 基线 TTFT | **60s（60K）/ 75s（80K）** | 实测 |
| 并发 | 1（硬件锁死） | §15.3.2 |

**叠加假设**：
- 各策略作用于不同内容类型（ContentRouter 路由后互斥），叠加按"互补系数 0.7"折算（非简单相加，因部分内容被多策略覆盖）
- CCR 不减少首轮 prompt，但消除死亡循环的"浪费轮"——按会话级统计
- 所有估算假设策略在代理层（`anthropic_proxy.py`）用 Python stdlib 独立实现，不引入 ML/embedder 依赖（§15.3 约束）

### 16.2 分阶段预期表

#### 16.2.1 单轮 TTFT 改善（首轮/非循环场景）

| 阶段 | 落地策略 | Prompt tokens（60K 基线） | TTFT | 较基线改善 | 较基线降幅 |
|------|---------|------------------------|------|-----------|-----------|
| **基线** | 无 | 60K | 60s | — | — |
| **短期（P0+P1）** | CCR + ContentRouter + 文件块去重 + AST 压缩 + 占位符增强 | 35-42K | **35-42s** | 18-25s | **30-42%** |
| **中期（+P2）** | + SmartCrusher JSON + 三级降级 Bash + 工具参数去重 | 25-32K | **25-32s** | 28-35s | **47-58%** |
| **全部落地（+P3）** | + 过期错误剪枝 + HTML 剥离 + BM25 索引 | 22-28K | **22-28s** | 32-38s | **53-63%** |

#### 16.2.2 高位场景（80K tokens 长会话）

| 阶段 | Prompt tokens | TTFT | 较基线改善 |
|------|--------------|------|-----------|
| 基线 | 80K | 75s | — |
| 短期（P0+P1） | 47-56K | **47-56s** | 19-28s |
| 中期（+P2） | 33-42K | **33-42s** | 33-42s |
| 全部落地 | 29-37K | **29-37s** | 38-46s |

### 16.3 CCR 的会话级特殊价值

CCR 的收益无法用单轮 TTFT 衡量——它消除的是**死亡循环的浪费轮**。

**死亡循环成本（实测，§3 + dead-loop-analysis-report.md）**：
- 每次循环 = 1 次 re-read tool_result 清空 + 1 次重算
- 每次 re-read 触发 60-80K tokens 全量 prefill = **55-75s 纯浪费**
- 实测循环可连续 7→9→11→13 轮 escalating，累计浪费 **6-16 分钟**

**CCR 落地后会话级改善**：

| 指标 | 基线 | CCR 后 | 改善 |
|------|------|--------|------|
| 单次循环浪费 | 55-75s | **0s**（可逆检索，无 re-prefill） | -100% |
| 会话累计循环浪费（20 轮会话） | 5-15 分钟 | **0** | 消除 |
| 会话总时长（20 轮） | 25-40 分钟 | **15-25 分钟** | **40-50%** |
| 上下文膨胀（death loop 触发） | 250K+ 崩溃 | 稳定 60-80K | 避免 OOM |

**CCR 是唯一能将会话级时间降低 40-50% 的策略**——其他策略优化单轮 TTFT，CCR 消除整类浪费。

### 16.4 不可逾越的硬件下限

即使全部策略落地，仍存在硬件强制的下限：

| 下限 | 原因 | 数值 |
|------|------|------|
| **Prefill 线性下限** | 4-bit KV 缓解存储，但 prefill 计算量 ∝ tokens，无 prefix cache 命中 | ~1ms/tok 不可降 |
| **最小可工作 prompt** | system + skills + 当前任务 + 最近 2-3 轮 + 工具定义 | ~12-18K tokens |
| **对应 TTFT 下限** | 12-18K × 1ms | **12-18s** |
| **并发下限** | Metal 单 GPU 时间切片 | MAX_CONCURRENT=1 不可升 |

**绝对下限**：在当前 MoE 模型 + 0% prefix cache 命中下，**单轮 TTFT 硬下限 ≈ 12-18s**（即最小工作集的 prefill 时间）。全部优化落地后 22-28s 已接近此下限。

### 16.5 突破硬件下限的唯二路径

| 路径 | 效果 | 代价 | 可行性 |
|------|------|------|--------|
| **① 切非 MoE 纯 Transformer 模型** | prefix cache 恢复 LCP 命中，50% 前缀命中 → TTFT 再减半至 **11-14s** | 放弃 35B-A3B MoE 性能（切 Qwen3.5-27B 等），生成质量可能下降 | 中（需模型切换 + 重新验证） |
| **② 切云端** | prefill 由云端集群承担，本地无 TTFT | 按量付费（¥1-3/任务），数据出域 | 高（已有 deepseek-chat.conf） |

**本地 48GB 单机的优化天花板 ≈ 22-28s（全部策略落地）/ 11-14s（切非 MoE 模型）**。要进一步突破需切云端。

### 16.6 预期汇总

| 场景 | 基线 | 短期（P0+P1） | 中期（+P2） | 全部落地 | 理论下限 |
|------|------|--------------|------------|---------|---------|
| **单轮 TTFT（60K）** | 60s | 35-42s | 25-32s | **22-28s** | 12-18s |
| **单轮 TTFT（80K）** | 75s | 47-56s | 33-42s | **29-37s** | 12-18s |
| **20 轮会话总时长** | 25-40 min | — | — | **15-25 min** | — |
| **死亡循环浪费** | 5-15 min | **0**（CCR） | 0 | 0 | 0 |
| **OOM 风险** | 高（80K 接近极限） | 中 | 低 | **低** | — |
| **可用上下文上限** | 60-100K | 80-130K* | 100-160K* | 120-180K* | 60-100K** |

> *上下文上限提升：压缩后相同 TTFT 预算可容纳更多原始信息（压缩比 2-3×）
> **理论下限行：切非 MoE 模型后的 prefix cache 命中场景，上下文上限不变（仍受 prefill 时间约束）

### 16.7 结论

1. **短期优化（P0+P1，1-2 周内）即可将单轮 TTFT 从 60s 降至 35-42s**（降 30-42%），这是性价比最高的阶段。

2. **全部策略落地后单轮 TTFT 可达 22-28s**（降 53-63%），接近 48GB 硬件的绝对下限（12-18s）。继续优化的边际收益递减。

3. **CCR 是会话级时间的最大杠杆**——消除死亡循环可将 20 轮会话总时长从 25-40 分钟降至 15-25 分钟（降 40-50%），这是单轮 TTFT 优化无法达到的。

4. **突破 22-28s 下限只有两条路**：切非 MoE 模型（恢复 prefix cache，降至 11-14s，但牺牲 MoE 性能）或切云端（无本地 prefill，但付费 + 数据出域）。两者都是架构级决策，非压缩策略可及。

5. **压缩策略的隐性收益——可用上下文上限提升**：相同 TTFT 预算下，压缩比 2-3× 意味着可容纳 120-180K 原始信息（vs 基线 60-100K），**间接弥合本地与云端的上下文能力差**。这是比 TTFT 改善更重要的战略价值。

---

## 参考

### 研究对象
- **[Headroom-ai](https://github.com/chopratejas/headroom)** — Apache-2.0, ★31,630, 6 算法 + CCR 可逆压缩 + Proxy/MCP（§12.1，**最高借鉴价值**）
- [DCP GitHub](https://github.com/Opencode-DCP/opencode-dynamic-context-pruning) — AGPL-3.0，嵌套摘要 + compress 工具（§1, §11）
- [Kompact](https://github.com/npow/kompact) — MIT，8 变换管线 + Cache Aligner + Schema Optimizer（§10.1）
- [TokenSieve](https://github.com/david-spies/tokensieve) — MIT，4-pass 指纹去重（§10.2）
- [Skim](https://github.com/dean0x/skim) — MIT，★27，tree-sitter AST 代码压缩引擎，17 语言（§12.2）
- [Token Reducer](https://github.com/Madhan230205/token-reducer) — MIT，★28，混合 RAG (BM25 + ONNX) 上下文压缩（§12.3）
- [LLMLingua](https://github.com/microsoft/LLMLingua) — MIT，★6,308，ML 模型 token 级压缩（§13，确认不采用）
- [Sleev](https://sleev.ai) — DCP 作者的下一代 context-management 本地代理（闭源，§6）

### 本项目文档
- `docs/04-analysis-diagnostics/dcp-vs-proxy-context-compression.md` — 首版对比（含完整实现代码，2026-06-17，02-architecture-design 版已合并于此）
- `docs/04-analysis-diagnostics/rapid-mlx-cache-analysis.md` — Prefix cache 命中问题分析（MoE non-trimmable 根因，§15 数据源）
- `docs/04-analysis-diagnostics/prefix-cache-analysis-20260605.md` — TurboQuant 配置测试 + KV 量化内存实测（§15.1.2 数据源）
- `anthropic_proxy.py` — 当前项目的核心代理实现
- `docs/02-architecture-design/proxy-pipeline-reference.md` — 8 层管线参考
- `docs/DEFECT-LIST.md` — 30 缺陷记录（7 P0 + 8 P1 + 10 P2 + 5 P3）

### 硬件约束分析数据源（§15）
- `configs/rapid-mlx-35b.conf` — 生产配置（gpu-memory-utilization 0.75, 4-bit KV, cache-memory-mb 4096, MAX_CONCURRENT=1）
- `configs/rapid-mlx-9b.conf` — 9B 对照（MAX_CONCURRENT=2 实测失败记录）
- 生产日志实测：prefill 55-75s/60-80K tokens、OOM 峰值 33-39GB、prefix cache 0% 命中

### DCP 源码（只读分析，/tmp/dcp-research/）
- `compress/pipeline.ts`, `compress/types.ts`, `compress/range.ts`, `compress/state.ts`, `compress/range-utils.ts`
- `strategies/deduplication.ts`, `strategies/purge-errors.ts`
- `prompts/compress-range.ts`, `prompts/context-limit-nudge.ts`, `prompts/system.ts`
- `messages/prune.ts`, `state/types.ts`
