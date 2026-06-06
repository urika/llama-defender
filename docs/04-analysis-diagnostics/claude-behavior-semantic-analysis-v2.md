# Claude Code 语义行为深度分析（v2）

> 分析时间：2026-06-04 15:30
> 数据基础：anthropic_proxy.py 日志（修复后，PID 92303）、proxy_requests.jsonl（1220 条记录）
> 代理端点：127.0.0.1:4000 → rapid-mlx + Qwen3.5-9B-MLX-4bit

---

## 核心发现前置

**修复后的关键确认**：
- `proxy_requests.jsonl` 1220 条记录中，真正"chars 相同且间隔 < 2s"的相邻对仅 **3 对**（0.25%）
- 日志中同一秒内出现多个 POST，绝大多数是**不同请求的日志交错**（`ThreadingHTTPServer` 多线程并发处理）
- 代理层已消除双重日志写入（15:21 后连续重复行数为 0）

---

## 第一层：交互语义 — Claude Code 不是聊天工具

### 1.1 从"对话"到"任务委托"

传统聊天系统的交互语义：
```
用户提问 → 模型理解 → 模型回答
```

Claude Code 的交互语义：
```
用户委托任务 → 模型制定计划 → 模型执行工具 → 注入执行结果 → 模型再评估 → ... → 交付成果
```

**语义差异**：
- 用户输入 `"@spec.md 请根据需求进行设计"` 不是"请回答我关于设计的问题"，而是"**请你代替我完成设计工作**"
- 模型的回复往往不是直接答案，而是 `tool_use`（Read/Edit/Bash）— 即"**我需要的不是答案，而是行动**"

### 1.2 Agentic 循环的语义结构

每轮交互产生 3 条消息（而非传统对话的 1 条）：

| 消息角色 | 语义功能 | 类比 |
|---------|---------|------|
| `assistant` | 模型的"行动计划"（tool_use 或文本回复） | 项目经理下达指令 |
| `system`（穿插） | "执行结果反馈"（工具输出、命令结果） | 执行团队汇报进度 |
| `user` | "任务委托 + 上下文更新"（含 system-reminder） | 客户确认需求变更 |

**这解释了为什么消息数增长这么快**：不是"对话轮数"多，而是"任务执行步骤"多。

---

## 第二层：请求语义 — 多路复用的任务调度

### 2.1 同一秒内多个 POST 的语义真相

修复后数据（15:21 之后）：

```
[15:23:04] POST① → chars=945881, stream=True, Timeout=600
[15:23:04] POST② → chars=945881, stream=True, Timeout=600
```

**这不是"重复请求"，而是 Claude Code 的"多路复用调度"**：

Claude Code 内部可能同时运行多个"思维线程"：
1. **主生成线程**：处理当前用户的显式请求（stream=True）
2. **后台评估线程**：评估上下文状态、准备下一步工具调用（也可能 stream=True）
3. **标题生成线程**：独立为会话生成标题（stream=True, chars=810）

证据：16:03:18 同时出现了 6 个 POST：
- 2 个 `chars=810`（标题生成，tools=0）
- 4 个 `chars=18433`（其他任务，tools=44）

这些请求携带不同的 `session` hash，说明它们**属于不同的内部任务流**。

### 2.2 Stream 模式的语义分工

```
stream=True:  116 次 (81%)  — 长文本生成、主任务流
stream=False:  28 次 (19%)  — 工具决策、短文本、状态查询
```

**语义解读**：
- `stream=True`：需要"渐进式交付"的场景（代码编写、文档生成），用户需要看到实时输出
- `stream=False`：需要"原子性决策"的场景（选择哪个工具、生成 3-7 词的标题），结果必须完整才能解析

---

## 第三层：消息语义 — system 角色不是系统提示

### 3.1 system 角色的语义挪用

Anthropic Messages API 的标准语义：
- `system`：对话开始时的系统提示（一次性）
- `user`：用户输入
- `assistant`：模型回复
- `tool`：工具结果

Claude Code 的实际语义：
- `system`：**状态通道**（每轮都注入工具结果、命令输出、上下文更新）
- `user`：任务委托（含 system-reminder 上下文包裹）
- `assistant`：行动计划
- `tool`：OpenAI 兼容格式的工具结果（代理转换）

**关键洞察**：Claude Code 把 `system` 角色当作了一个**可写的状态寄存器**，而不是只读的系统提示。这违反了标准 API 语义，但实现了更灵活的状态传递。

### 3.2 内容块重复的语义解释

```json
[
  {"type": "text", "text": "@spec.md 请根据需求进行设计\n"},
  {"type": "text", "text": "@spec.md 请根据需求进行设计\n"},
  {"type": "text", "text": "hi\n"},
  {"type": "text", "text": "hi"}
]
```

**这不是用户输入错误，而是"上下文重放"**：
- Claude Code 在重建请求时，将历史用户输入**按内容块粒度**重新组装
- 如果历史中有重复的内容块（如用户快速按了两次回车），它们会被**忠实保留**
- 这种"忠实重放"语义确保了模型不会丢失任何用户输入，代价是冗余

### 3.3 文件引用的内联语义

`@spec.md` 不是"请读取这个文件"的引用，而是"**这个文件的内容就是上下文的一部分**"的内联嵌入。

语义对比：

| 符号 | 传统语义 | Claude Code 语义 |
|------|---------|-----------------|
| `@filename` | 引用/链接 | 内容内联（全文嵌入） |
| `#currentDate` | 环境变量 | 时间锚定（每轮更新） |
| `/model` | 配置命令 | 状态变更（结果注入历史） |

**后果**：一个 `@spec.md` 可能引入 50K+ 字符，且每轮都重复携带。

---

## 第四层：工具语义 — 编程即编排

### 4.1 工具调用的工作流语义

Claude Code 的工具使用遵循**严格的编程工作流**：

```
Read(文件A) → 理解上下文 → Edit(文件A) → Bash(验证) → Read(文件B) → Edit(文件B) → ...
     ↑                                              ↓
   信息收集                                     状态确认
```

**语义约束**：
- **必须先 Read 再 Edit**：模型不会"盲改"文件，必须先获取内容
- **Edit 后必须 Bash 验证**：修改后需要运行测试或检查语法
- **复杂任务拆分为 Agent**：超过一定复杂度的任务会创建子 Agent 并行处理

### 4.2 MCP 工具的语义扩展

44 个工具中包含大量 MCP（Model Context Protocol）工具：

```
mcp__serper__google_search         → "我需要搜索互联网"
mcp__wechat-search__search_wechat  → "我需要搜索微信公众号"
mcp__serper__webpage_scrape        → "我需要抓取网页内容"
```

**语义解读**：MCP 将"外部世界交互"封装为"工具调用"，使模型能够：
1. **语义化地表达意图**："搜索"而不是"生成搜索 URL"
2. **结构化地接收结果**：返回 JSON 而不是原始 HTML
3. **可组合地编排动作**：搜索 → 抓取 → 总结 → 写入文件

---

## 第五层：上下文语义 — 不可避免的膨胀

### 5.1 上下文增长的三重机制

当前会话规模：`chars=945881, tools=44, msgs=83`

**增长不是线性的，而是层叠的**：

| 层级 | 增长源 | 每轮增量 | 累积效应 |
|------|--------|---------|---------|
| L1: 用户输入 | 新任务描述 | 100-5K chars | 线性 |
| L2: 工具结果 | Read/Edit/Bash 输出 | 1K-50K chars | 超线性 |
| L3: 系统注入 | system-reminder + 命令历史 | 5K-20K chars | 线性但基数大 |
| L4: 工具签名 | 44 个工具的 JSON Schema | ~20K chars | **恒定但沉重** |

**关键瓶颈**：L4（工具签名）每轮都完整传递 20K 字符，占总上下文约 2%。这看似不多，但当总上下文达到 1M 时，工具签名本身就占用了 20K 的固定开销。

### 5.2 system-reminder 的锚定语义

```xml
<system-reminder>
As you answer the user's questions, you can use the following context:
# currentDate
Today's date is 2026/06/04.

IMPORTANT: this context may or may not be relevant to your tasks.
You should not respond to this context unless it is highly relevant to your task.
</system-reminder>
```

**语义功能分解**：
1. **时间锚定**：确保模型知道"现在"，避免时间幻觉
2. **注意力引导**：`IMPORTANT` 声明使用**否定式注意力机制**（"除非相关，否则不要响应"）
3. **上下文边界**：明确区分"系统注入的上下文"和"用户真正的输入"

---

## 第六层：会话语义 — 长会话的生存策略

### 6.1 会话标题的自动生成

16:03:18 的 `chars=810` 请求：
```
"Generate a concise, sentence-case title (3-7 words) 
that captures the main topic or goal of this coding session."
```

**语义解读**：Claude Code 将会话视为**可管理的实体**，需要：
- **可识别的身份**（标题用于会话列表）
- **可归档的结构**（标题用于历史检索）
- **可分享的摘要**（标题用于会话导出）

### 6.2 模型切换的语义降级

```
claude-opus-4-8 → claude-sonnet-4-6
```

**语义解释**：
- **Opus**："我需要深度推理"（复杂架构设计、算法实现）
- **Sonnet**："我需要快速响应"（代码补全、简单编辑、上下文浏览）
- 切换是**任务适配行为**，不是用户偏好表达

### 6.3 代理截断的语义补偿

当上下文达到 945K chars 时，代理执行：
```
Context truncation (rounds): 558 messages dropped, 24 kept
```

**语义影响**：
- 模型丢失了 96% 的历史上下文
- 但保留了最近 10 轮的"工作记忆"
- 早期文件读取结果（Read tool 输出）被丢弃，可能导致模型"忘记"已读取的文件内容

**这解释了为什么长会话中模型会重复读取同一个文件**：不是模型"健忘"，而是代理截断丢弃了早期的 Read 结果。

---

## 综合语义画像

| 维度 | 语义特征 | 对本地后端的影响 |
|------|---------|-----------------|
| **交互模式** | 任务委托 → Agentic 循环（非对话） | 请求频率高于预期 |
| **请求调度** | 多路复用（同一秒内多请求并发） | `ThreadingHTTPServer` 线程竞争 |
| **消息结构** | system 角色作为状态通道 | 非标准 API 格式，代理需转换 |
| **内容膨胀** | 文件内联 + 工具签名 + 系统注入 | 上下文快速逼近 1M 上限 |
| **工具编排** | Read → Edit → Bash 编程循环 | Read 工具占绝对主导 |
| **会话管理** | 自动标题 + 模型适配 + 截断生存 | 长会话需要反复重新加载上下文 |

---

## 关键洞察：Claude Code 的"认知架构"

Claude Code 可以被理解为一个**具备外化工作记忆的认知系统**：

1. **工作记忆** = 最近 10 轮消息（代理截断后保留的部分）
2. **长期记忆** = 文件系统（通过 Read 工具按需加载）
3. **感知通道** = system-reminder（每轮更新环境状态）
4. **行动通道** = 44 个工具（与外部世界交互）
5. **元认知** = 会话标题生成、模型选择、任务分解

**代理层的角色**：不是简单的协议转换器，而是这个认知系统的**记忆管理层**—负责压缩、截断、翻译和补偿，确保本地模型在有限上下文窗口内仍能运行这个复杂的 Agentic 工作流。

---

## 第七层：循环模式分类 — 错误驱动 vs 认知驱动

### 7.1 两种循环模式

通过对代理日志中多次循环事件的语义分析，发现 LLM 循环行为存在两种根本不同的驱动模式：

| 维度 | 错误驱动循环 | 认知驱动循环 |
|------|------------|------------|
| **典型工具** | Read | Write |
| **根因** | 无法理解错误信息（Wasted call） | 内部认知矛盾（成功后仍重复） |
| **tool_result** | 错误信息 | 成功信息 |
| **内容变化** | 完全相同 | 几乎相同（>99%） |
| **循环频率** | 极快（~2s/次） | 慢速（~3min/次） |
| **模型自知** | 无 | 有（但无法自纠正） |
| **break 效果** | 立即停止 | 仅当次有效 |

### 7.2 实例：Read 死循环（09:47，错误驱动）

详见 `docs/dead-loop-analysis-report.md` §1-5。

核心路径：`Read spec.md → Wasted call → 不理解错误 → 再次 Read → ...`（219 次）

Loop detection 在第 3 次注入 break 消息后立即生效。

### 7.3 实例：Write 认知循环（16:02-16:23，认知驱动）

**背景**：修复 Read 循环后（15:30 代理重启），Claude Code 使用 `claude-opus-4-8`（44 tools, max_tokens=64K）开始围棋游戏编程。

**完整时间线**：

```
15:30-15:59  正常工作阶段（12 轮工具调用）
             Read spec.md → Write plan.md (20K chars) → Write board.js (37K chars)
             每轮 chars 增长 ~14K，max_run=2

15:42-15:59  渐进式精简
             board.js: 37,733 → 13,384 chars（模型主动精简）
             模型输出: "文件内容太长，需要精简"

16:02        首次 Loop detected
             board.js Write 内容: 13,384 chars（与上轮几乎相同）
             代理注入 break → 模型输出 "无法继续编写"

16:06        第 2 次 Loop detected（跨请求循环开始）
             模型在新请求中再次 Write board.js (13,384 chars)

16:09-16:22  持续循环（max_run 3→8）
             每 ~3 分钟一次，内容几乎不变

16:19        模型输出: "文件内容太长，无法继续编写。让我用不同的方式处理：
             将文件拆分成多个文件。"

16:23        最终输出 102 chars 文本，会话结束
```

**关键报文证据**（`160244_0015_m53_t27.json`）：

```
msg[50] assistant text: "文件内容太长，需要精简。让我重新编写更简洁的 board.js："
msg[50] assistant tool_use: Write file=board.js content=13,384 chars
msg[51] tool_result: "The file has been updated successfully."
msg[52] assistant text: "文件内容太长，需要精简。让我重新编写更简洁的 board.js："
msg[52] assistant tool_use: Write file=board.js content=13,384 chars
msg[53] tool_result: "The file has been updated successfully."
```

**代理层追踪指标**：

| 时间 | max_run | total_calls | chars | clearing |
|------|---------|-------------|-------|----------|
| 15:42 | 2 | 15 | 113K | 5 cleared, 883 freed |
| 15:59 | 2 | 21 | 211K | 11 cleared, 1,268 freed |
| 16:02 | **3** | 22 | 225K | 12 cleared, 1,421 freed |
| 16:06 | 4 | 23 | 239K | 13 cleared, 1,574 freed |
| 16:09 | 5 | 24 | 254K | 14 cleared, 1,727 freed |
| 16:12 | 6 | 25 | 268K | 15 cleared, 1,880 freed |
| 16:15 | 7 | 26 | 282K | 16 cleared, 2,033 freed |
| 16:18 | 8 | 27 | 297K | 17 cleared, 2,186 freed |

### 7.4 认知循环的心理学类比

Write 认知循环类似于人类的"强迫性检查"行为：
- 知道门已经锁了（tool_result: "updated successfully"）
- 但仍然觉得需要再检查一次（"文件内容太长，需要精简"）
- 每次检查后获得短暂安心，但随即重新产生焦虑

**LLM 的"焦虑"来源**：上下文窗口中同时存在 "board.js 已写入成功" 和 "文件内容太长" 的记忆。模型无法调和这两个信息，导致决策摇摆。

### 7.5 防御策略对比

| 策略 | 对错误驱动循环 | 对认知驱动循环 |
|------|--------------|--------------|
| Loop detection（3次阈值） | ✅ 高效 | ⚠️ 当次有效，跨请求失效 |
| Error translation（自然语言改写） | ✅ 有效 | ❌ 无错误可改写 |
| 语义保留（Preview） | ✅ 有效 | ❌ 文件已成功写入 |
| **跨请求循环检测**（max_run 趋势） | 不需要 | ✅ **需要** |
| **Write 去重**（内容相似度检测） | 不适用 | ✅ **需要** |
| **tool_result 内容摘要**（Write 结果保留前 500 字符） | 不需要 | ✅ **需要** |

### 7.6 关键指标监控

通过 `max_run` 和 `total_calls` 的趋势可以提前识别循环：

```
正常工作:  max_run=2, total_calls 线性增长
循环早期:  max_run=3, Loop detected 首次触发
循环中期:  max_run=4-6, Loop detected 持续触发
循环晚期:  max_run≥7, chars 快速膨胀（+14K/次）
```

**预警阈值建议**：
- `max_run >= 3`：黄色预警（Loop detected 已触发）
- `max_run >= 5` 且 `loop_detected >= 3`：红色预警（跨请求循环确认）
- `max_run >= 7`：建议注入强终止信号或建议用户重启会话
