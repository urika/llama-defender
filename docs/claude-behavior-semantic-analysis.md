# Claude Code 语义行为分析报告

> 分析时间：2026-06-04
> 数据来源：anthropic_proxy.py 日志、llama-server.log
> 代理端点：127.0.0.1:4000 → rapid-mlx + Qwen3.5-9B-MLX-4bit

---

## 1. 请求模式语义

### 1.1 流式 vs 非流式

```
  17  model=claude-opus-4-8, stream=False
  13  model=claude-opus-4-8, stream=True
```

**非流式请求（57%）多于流式请求（43%）**，这与典型聊天对话的预期相反。

**语义推断**：
- 非流式请求通常用于**工具调用结果处理**（stream=False + max_tokens=64/300）
- 流式请求用于**长文本生成**（stream=True + max_tokens=32000）
- Claude Code 将对话拆分为多个子请求：先非流式处理工具，再流式生成回复

### 1.2 模型选择语义

Claude Code 始终使用 claude-opus-4-8，映射到本地的 Qwen3.5-9B-MLX-4bit。

**行为模式**：
- 用户通过 /model 命令切换模型后，Claude Code 将该设置持久化为默认值
- 后续所有请求都携带相同的 model 字段
- 代理层通过 MODEL_ALIASES 将 claude-opus-4-8 映射到实际后端模型

---

## 2. 消息角色语义结构

### 2.1 角色序列模式

从后端日志提取的典型角色链：

```
['system', 'user', 'system', 'assistant', 'user', 'system', 'assistant', 'tool']
['system', 'user', 'system', 'assistant', 'user', 'system', 'assistant', 'user', 'tool', 'assistant', 'tool', 'system']
```

**语义解读**：

| 角色序列 | 含义 |
|----------|------|
| system (首个) | 系统提示（billing header、skills、instructions） |
| user | 用户输入（含 <system-reminder> 上下文） |
| system (穿插) | **Claude Code 注入的工具结果**（非标准 Anthropic 行为） |
| assistant | 模型生成的回复（文本或 tool_use） |
| tool | OpenAI 格式的工具结果（代理转换为 Anthropic tool_result） |

**关键发现**：Claude Code 大量使用 system 角色来注入中间状态（工具结果、命令输出、上下文），而不是使用标准的 tool_result 内容块。这导致消息历史中出现**多个 system 消息穿插**的非标准结构。

### 2.2 消息内容特征

**用户消息内容块结构**：

```json
[
  {"type": "text", "text": "<system-reminder>...上下文...</system-reminder>\n\n"},
  {"type": "text", "text": "@spec.md 请根据需求进行设计\n"},
  {"type": "text", "text": "<local-command-caveat>Caveat: ...</local-command-caveat>\n"},
  {"type": "text", "text": "<command-name>/model</command-name>\n..."},
  {"type": "text", "text": "<local-command-stdout>Set model to Opus 4.8...</local-command-stdout>\n"},
  {"type": "text", "text": "hi\n"},
  {"type": "text", "text": "hi"}
]
```

**语义标签分析**：

| 标签 | 出现次数 | 语义 |
|------|----------|------|
| <system-reminder> | 2,485 | 注入上下文（日期、技能列表、环境信息） |
| <local-command-caveat> | 高频 | 免责声明：不要响应这些本地命令消息 |
| <command-name> | 高频 | 用户执行的 CLI 命令（如 /model） |
| <local-command-stdout> | 高频 | 命令执行结果（如模型切换确认） |
| @filename | 中频 | 文件引用（如 @spec.md） |

---

## 3. 消息增长语义

### 3.1 增长曲线

```
msgs=7  → msgs=10 → msgs=12 → msgs=14
```

**每轮对话增加 2-3 条消息**：
- 1 条 assistant（模型回复，可能含 tool_use）
- 1 条 system（工具结果注入）
- 1 条 user（用户新输入，含多个内容块）

### 3.2 内容重复与膨胀

**重复注入问题**：

```
@spec.md 请根据需求进行设计     ← 第1次
@spec.md 请根据需求进行设计     ← 第2次（重复）
hi                               ← 第1次
hi                               ← 第2次（重复）
```

**语义推断**：
- Claude Code 在重试或重新构建请求时，会将之前的用户输入重复追加
- 或者用户在短时间内发送了相同内容（如快速按了两次回车）
- 这导致报文字符数非线性增长，加速触发上下文截断

**/model 命令的副作用**：
- Set model to Opus 4.8 (1M context) (default) 被注入消息历史 5 次
- 每次约 80 字符，累积 400+ 字符的冗余内容
- 带有 ANSI 颜色码，增加解析复杂度

---

## 4. 工具调用语义链

### 4.1 工具调用模式

```
  10  text=0 chars, tools=1     ← 纯工具调用（无文本内容）
   2  text=43 chars, tools=0    ← 纯文本回复
   2  text=2083 chars, tools=0  ← 长文本回复
   1  text=129 chars, tools=1   ← 文本+工具混合
```

**语义解读**：
- **纯工具调用（57%）**：模型直接输出 tool_use 块，没有前置文本说明
- **纯文本回复（17%）**：模型直接回答用户，不调用工具
- **长文本回复（11%）**：生成较长的解释性内容（2K+ 字符）

### 4.2 工具偏好

```
  10  Read tool   ← 绝对主导
```

**语义推断**：
- Claude Code 的编程工作流以**读取文件**为核心操作
- 在编辑前必须先 Read 确认内容
- Bash 和 Edit 是在 Read 获取上下文后的后续动作

---

## 5. 上下文管理语义

### 5.1 System-reminder 机制

出现 2,485 次的 <system-reminder> 块：

```
<system-reminder>
As you answer the user's questions, you can use the following context:
# currentDate
Today's date is 2026/06/04.

IMPORTANT: this context may or may not be relevant to your tasks.
You should not respond to this context unless it is highly relevant to your task.
</system-reminder>
```

**语义功能**：
1. **时间锚定**：提供当前日期，确保时间相关查询的准确性
2. **上下文注入**：动态插入 skills、环境变量、项目状态
3. **注意力引导**：通过 IMPORTANT 声明降低无关上下文的干扰

### 5.2 代理层的语义处理

代理对 Claude Code 报文进行的语义操作：

| 操作 | 语义目的 |
|------|----------|
| Error translation: 227 tool_result errors rewritten to natural language | 将工具错误转换为人类可读文本 |
| Tool clearing: 269 tool_results cleared, 159,533 chars freed | 丢弃旧工具结果，保留最近 N 对 |
| Context truncation (rounds): 558 messages dropped, 24 kept | 按对话轮数截断，保留最近 10 轮 |
| Tool-result compression: no consecutive cleared cycles to merge | 合并连续被清除的工具结果周期 |
| Normalized billing header in system prompt | 标准化计费头，避免重复计费信息 |
| Standardized date in msg0 block | 统一日期格式，减少 token 浪费 |

---

## 6. 超时与重试语义

### 6.1 超时模式

proxy_requests.jsonl 中的超时请求：

```
[55:18.17]  608.3s
[15:18.21]  899.5s
[25:18.22] 1198.4s
[35:18.25] 1496.4s
```

**语义推断**：
- 这些不是后端处理慢，而是**SSE 连接被保持到代理超时**
- 可能原因：
  1. Claude Code 的 SSE 客户端未正确关闭连接
  2. 用户在长生成过程中切换窗口，导致客户端挂起
  3. disconnect_guard 轮询机制与代理超时冲突

### 6.2 重试模式

代理日志中出现大量**重复请求**：

```
[14:45:04] REQ_SUMMARY session=a7806b18 chars=39058 tools=27 msgs=10
[14:45:04] REQ_SUMMARY session=a7806b18 chars=39058 tools=27 msgs=10  ← 同一毫秒重复
```

**语义推断**：
- Claude Code 的 HTTP 客户端可能存在**双重发送**机制
- 或代理层的日志记录有重复打印 bug
- 也可能是 Anthropic SDK 的重试逻辑导致同一请求被发送两次

---

## 7. 综合语义画像

### 7.1 Claude Code 的交互语义

Claude Code 不是简单的用户提问-模型回答对话，而是**多轮 agentic 循环**：

```
用户输入 → 模型思考 → 工具调用 → 执行工具 → 注入结果 → 模型再思考 → ... → 最终回复
```

每轮循环产生 **3 条消息**：
1. assistant：模型的 tool_use 或文本回复
2. system：代理/Claude Code 注入的工具结果
3. user：用户的新输入（含 system-reminder + 命令历史）

### 7.2 消息膨胀的语义根因

1. **system-reminder 重复注入**：每轮请求都携带完整的上下文块（2,485 次出现）
2. **命令历史累积**：/model、本地命令输出等被永久保留在历史中
3. **文件内容内联**：@spec.md 等引用将文件全文注入消息（而非只传引用）
4. **重复内容追加**：相同用户输入被多次追加到内容块数组中

### 7.3 代理的语义补偿策略

代理层通过以下语义转换来维持系统稳定：

1. **工具错误翻译**：将机器错误码转为自然语言，帮助模型理解失败原因
2. **上下文截断**：丢弃 96% 的历史消息，只保留最近 10 轮
3. **日期标准化**：统一日期格式，避免多种日期表示浪费 tokens
4. **计费头规范化**：去除重复计费信息

---

## 8. 结论

Claude Code 的语义行为特征：

| 维度 | 特征 |
|------|------|
| **对话模式** | Agentic 循环（用户→思考→工具→结果→再思考） |
| **消息结构** | 非标准多 system 角色穿插结构 |
| **内容膨胀** | system-reminder + 命令历史 + 文件内联导致指数增长 |
| **工具偏好** | Read 工具占绝对主导（编程工作流核心） |
| **重试行为** | 存在请求重复发送现象 |
| **超时模式** | SSE 连接保持过长，代理层 300s+ 超时 |

**优化建议**：
1. **system-reminder 去重**：如果内容未变化，不应每轮都重新注入
2. **命令历史清理**：/model 等元命令的输出不应长期保留在历史中
3. **文件引用优化**：@spec.md 应只传摘要或差异，而非全文
4. **重复请求修复**：调查并修复双重发送的根因
