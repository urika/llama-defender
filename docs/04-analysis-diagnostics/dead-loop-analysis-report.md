# Claude Code 死循环分析与代理层优化报告

> 日期: 2026-06-04
> 模型: Qwen3.5-9B-MLX-4bit / claude-opus-4-8
> 后端: rapid-mlx v0.6.71
> 项目: 围棋 AI 对战系统 (`/Users/jinsongwang/workspace/test1/`)
> 更新: 新增 §6 Write 工具认知循环实例分析

---

## 1. 现象

### 1.1 会话概览

在一次 Claude Code agentic 编程会话中，通过代理层保存的 20 个请求报文，完整记录了从死循环到恢复的全过程。

| 时间段 | 请求 | 消息数 | 工具数 | 角色 | 行为特征 |
|--------|------|--------|--------|------|---------|
| 09:47-09:50 | 0003-0010 | 465→477 | 27 | 主进程 | Read 死循环（219 次 Wasted call） |
| **09:47:58** | — | — | — | **代理重启** | **KEEP=5→10, 循环检测, 语义保留** |
| 09:51-09:53 | 0011 | 479→481 | 27 | 主进程 | 循环停止，Bash cat 自救 |
| 09:57 | 0012-0015 | 1→5 | 17 | 子代理（只读） | 高效完成代码分析 |
| 09:59 | 0017-0022 | 1→11 | 20 | 子代理（可写） | 路径幻觉，渐进修正 |

### 1.2 核心数据

| 指标 | 数值 |
|------|------|
| 总请求 | 20 个 |
| 总消息 | 465→481 条（主进程） |
| tool_use 调用 | 233 次 |
| Read 调用 | 226 次（97%） |
| "Wasted call" | 219 次 |
| 浪费率 | **94.0%** |
| 死循环持续时间 | ~20 分钟（09:47:00 - 09:47:58 旧代理，09:47:58-09:51:37 新代理惯性期） |
| 代理重启后恢复时间 | ~3 分钟（循环停止） + ~4 分钟（完成任务） |
| 实际产出 | 代理优化前为零；优化后 7 分钟内完成分析报告 + 规划 |

---

## 2. 完整时间线

```
09:47:00  死循环持续中，旧代理（KEEP=5, 无循环检测）
          每 2 秒一次 Read → Wasted call → Read → ...
          kept last 5, 198-222 tool_results cleared

09:47:49  旧代理最后一轮: 222 cleared, kept last 5

════════════ 09:47:50~09:47:58 代理重启 ══════════
变更内容:
  1. PROXY_TOOL_KEEP: 5 → 10
  2. 循环检测: 连续 3 次相同 tool_use → 注入打断消息
  3. 语义保留: 清除 tool_result 时保留前 200 字符预览

09:47:58  新代理首次生效
          Loop detected: Read called 3 times with same args
          kept last 10, 217 cleared
          同秒内两个请求均触发循环检测

09:49-09:50  新代理处理旧会话（惯性期）
             模型仍尝试 Read，但每次被循环检测打断
             共触发 10 次 Loop detected

09:51:37  循环完全停止（无 Loop detected）
          语义保留让模型看到 "Preview: # 围棋游戏..."
          模型不再需要重新 Read

09:54  用户重新提问: "请分析目前哪些功能已经实现了"

09:56:03  模型主动选择 Bash cat（而非 Read）获取 spec.md
          → 成功读取 spec.md + index.html
          → 生成完整的已实现/缺失功能分析表

09:57  Claude Code 派出子代理（只读，17 tools）
       "You are a file search specialist... READ-ONLY MODE"
       Bash find → Read index.html → 高效完成

09:58  主进程拿到子代理结果（481 msgs）
       模型输出: "请规划设计需求，列出todo，分阶段实施"

09:59  Claude Code 派出第二个子代理（可写，20 tools）
       "You are an agent... complete the task fully"
       路径幻觉（go_app/go_app/app/go_app/...）→ 渐进修正
       Bash find → ls → Read spec.md → 成功
```

---

## 3. 原因分析

### 3.1 死循环的三层根因

```
            ┌──────────────────────────────────┐
            │       用户任务: 分析围棋项目       │
            └──────────────┬───────────────────┘
                           ↓
            ┌──────────────────────────────────┐
            │  模型首次 Read spec.md (21K chars)  │
            └──────────────┬───────────────────┘
                           ↓
            ┌──────────────────────────────────┐
            │  多轮后 clear_old_tool_results     │
            │  KEEP=5: 只保留最后 5 个            │
            │  spec.md 内容被清除                 │  ← 语义层根因
            └──────────────┬───────────────────┘
                           ↓
            ┌──────────────────────────────────┐
            │  模型丢失关键上下文                 │
            │  尝试重新 Read spec.md             │
            └──────────────┬───────────────────┘
                           ↓
            ┌──────────────────────────────────┐
            │  Claude Code 返回:                  │
            │  "Wasted call — file unchanged     │
            │   since your last Read..."         │
            └──────────────┬───────────────────┘
                           ↓
            ┌──────────────────────────────────┐
            │  模型不理解 "Wasted call" 含义      │  ← 理解层根因
            │  继续尝试 Read → 再次 Wasted call   │
            └──────────────┬───────────────────┘
                           ↓
            ┌──────────────────────────────────┐
            │  无循环检测机制                     │  ← 防御层根因
            │  无限重复: Read → Wasted → Read    │
            │  219 次后模型放弃: "Ready to help"  │
            └──────────────────────────────────┘
```

| 层级 | 根因 | 影响 |
|------|------|------|
| **语义层** | `KEEP=5` 只保留最后 5 个 tool_result，spec.md 内容被清除为 `[cleared to save context: 21000 chars]` | 模型丢失文件内容，被迫重新读取 |
| **理解层** | 模型无法理解 Claude Code 的 "Wasted call" 错误信息的语义（文件缓存机制） | 不知道应该换用其他方式获取文件 |
| **防御层** | 代理层无循环检测机制，无法在检测到重复行为时主动干预 | 模型可无限重复同一操作 |

### 3.2 Claude Code 三层 Agent 架构

通过分析 system prompt 和工具集差异，发现 Claude Code 使用三层架构：

| 层级 | 角色 | Tools | System Prompt | 权限 |
|------|------|-------|---------------|------|
| **主进程** | 交互式代理 | 27（全套） | "interactive agent" + 26K 字符完整指令 | 全部权限，含 Agent 派发、Plan mode |
| **子代理（只读）** | 文件搜索专家 | 17 | "file search specialist... READ-ONLY MODE" | 无 Edit/Write/Agent/Plan |
| **子代理（执行）** | 任务执行代理 | 20 | "agent... complete the task fully" | 有 Edit/Write 但无 Agent/Plan |

工具集差异：

```
27→17 剥离: Agent, Edit, Write, EnterPlanMode, ExitPlanMode,
           AskUserQuestion, ScheduleWakeup, Workflow, NotebookEdit, TaskOutput

17→20 增加: Edit, Write, NotebookEdit（执行型子代理可写）
```

### 3.3 路径幻觉问题

执行型子代理（会话 3）首次调用 Read 时猜测了三条不存在的路径：

```
/Users/jinsongwang/workspace/test1/go_app/go_app/app/go_app/engine...
/Users/jinsongwang/workspace/test1/go_app/go_app/app/go_app/engine...
/Users/jinsongwang/workspace/test1/go_app/go_app/app/go_app/go_ap...
```

这是模型对 Web 应用项目结构的**训练数据偏差**——模型假设围棋系统是 Django/Flask 应用，但实际项目只有 `spec.md` + `index.html` 两个文件。

---

## 4. 修复方案与验证

### 4.1 三层组合修复

| 层级 | 修复措施 | 实现 |
|------|---------|------|
| **防御层** | 循环检测：连续 3 次相同 tool_use → 注入用户打断消息 | `_handle_messages()` 中检测 tool_call_history |
| **语义层** | 语义保留：清除时保留前 200 字符预览 | `clear_old_tool_results()` 中 original_len > 300 时追加 Preview |
| **配置层** | PROXY_TOOL_KEEP: 5 → 10 | `configs/rapid-mlx-9b.conf` |

### 4.2 验证结果

代理日志确认修复生效：

```
[09:47:58] Loop detected: Read called 3 times with same args, injected break message
[09:47:58] Tool clearing: 217 tool_results cleared, 81,263 chars freed (kept last 10)
```

| 验证项 | 结果 | 证据 |
|--------|------|------|
| 循环检测 | 通过 | 10 次 `Loop detected` 日志 |
| 语义保留 | 通过 | 超 300 字符的 tool_result 带 `Preview:` 前缀 |
| KEEP=10 | 通过 | `kept last 10` 替代旧的 `kept last 5` |
| 单元测试 | 通过 | 28 tests OK |
| 死循环恢复 | 通过 | 09:51:37 后无 Loop detected |

### 4.3 修复前后对比

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| tool_result 清除 | `[cleared: 21000 chars]` | `[cleared: 21000 chars. Preview: # 围棋游戏...]` |
| 重复 Read | 219 次死循环 | 第 3 次注入打断消息 |
| 近期上下文保留 | 最后 5 个 tool_result | 最后 10 个 |
| 恢复时间 | 不可恢复（用户需手动重启） | ~3 分钟自动停止 + ~4 分钟完成任务 |

---

## 5. Insight

### 5.1 LLM 对工具错误信息的理解极弱

模型能理解自然语言任务，但对结构化工具错误信息（"Wasted call"、"File does not exist"、"InputValidationError"）的理解远不如自然语言反馈。在 219 次循环中，模型始终无法将 "Wasted call — file unchanged since your last Read. Refer to that earlier tool_result instead." 理解为"不要再用 Read 了"。

**启示**：代理层应将工具错误信息转化为更明确的自然语言指令。我们的循环检测注入的打断消息是自然语言格式，模型能立即理解并改变行为。

### 5.2 tool_result 清除是语义损失，不只是 token 节省

清除 tool_result 时不保留任何内容摘要，等于让模型"失忆"。对于 agentic 场景，模型依赖 tool_result 维持对项目结构的理解——清除 spec.md 的内容后，模型不知道项目是什么、有什么文件、代码在哪里。

**启示**：tool_result 清除应该是一种**有损压缩**而非**删除**。保留前 200 字符的预览足以让模型判断"我已经读过这个文件，内容大致是..."，避免重新读取。

### 5.3 代理层是防御 LLM 异常行为的最佳位置

循环检测、语义保留等修复在代理层实现，无需修改 LLM 或客户端。这提供了三个优势：

- **模型无关**：无论后端是 Qwen、DeepSeek 还是 Claude，代理层防御都有效
- **客户端无关**：无论使用 Claude Code、opencode 还是自定义客户端，代理层防御都有效
- **热部署**：代理重启仅需 ~8 秒，不影响正在运行的模型推理服务

### 5.4 Claude Code 的子代理架构已内建防循环策略

子代理的 system prompt 明确声明 `READ-ONLY MODE` 或 `complete the task fully`，工具集按角色精简（17/20 vs 27）。这种架构层面的约束比模型层面的"理解错误信息"更可靠——子代理即使遇到 Wasted call 也无法无限循环，因为它的工具集和指令限制了行为空间。

**启示**：代理层优化应配合客户端架构。我们的循环检测是通用防御，但 Claude Code 的子代理架构提供了更根本的解决方案——通过权限最小化限制异常行为的爆炸半径。

### 5.5 死循环是渐进发生的，不是突然出现的

从代理日志可以看到清除数量的渐进增长：

```
09:47:00  198 cleared
09:47:45  220 cleared
09:47:47  221 cleared
09:47:49  222 cleared  ← 每轮 +1（一个 Read + 一个 Wasted call）
```

每轮对话增加一对 Read/Wasted call，tool_result 被清除后模型再次丢失上下文。这不是突发故障，而是**渐进式语义耗散**——随着对话进行，模型逐渐丢失早期积累的上下文，最终回到"什么都不知道"的状态。

**启示**：KEEP 值的选择直接影响语义耗散的临界点。KEEP=5 在 10+ 轮工具调用后就会开始清除关键文件内容；KEEP=10 将这个临界点推迟到 20+ 轮。结合语义保留，模型能在清除后仍保持对关键文件的记忆。

### 5.6 模型具有延迟的工具学习能力

模型在 219 次 Read 失败后，最终学会了用 `Bash cat` 替代 `Read`：

```
[471] assistant: tool_use:Read → Wasted call（最后一次尝试 Read）
[473] assistant: tool_use:Bash("cat spec.md") → 成功（首次尝试 Bash cat）
```

这意味着模型**能够**从失败中学习替代策略，但学习速度极慢（需要 219 次失败）。循环检测在第 3 次就打断循环，等于强制模型立即寻找替代方案，将学习延迟从 219 轮缩短到 3 轮。

**启示**：不要依赖模型的自我纠正能力。在代理层主动检测异常模式并注入指导信息，比等待模型自行发现替代方案高效两个数量级。

---

## 6. 实例补充：Write 工具认知循环（16:02-16:23）

### 6.1 现象

在 Read 死循环修复后（15:30 代理重启），Claude Code 开始了新的围棋游戏编程会话。前 12 轮工作正常，但从第 15 轮开始出现新型循环。

| 阶段 | 时间 | Msgs | Chars | max_run | 状态 |
|------|------|------|-------|---------|------|
| 正常工作 | 15:30-15:59 | 23→50 | 72K→211K | ≤2 | ✅ 无异常 |
| Write 循环 | 16:02-16:22 | 53→66 | 225K→297K | 3→8 | ⚠️ Loop detected ×7 |
| 停止 | 16:23 | 66 | — | — | text=102, 结束 |

### 6.2 循环根因：认知不一致

与 Read 死循环（无法理解 "Wasted call" 错误信息）不同，Write 循环的根因是**模型认知不一致**：

```
模型生成 board.js (37,733 chars) → Write 成功
    ↓
模型评估: "文件内容太长，需要精简"
    ↓
重写 board.js (13,384 chars) → Write 成功
    ↓
tool_result: "has been updated successfully"
    ↓
模型再次评估: "文件内容太长，需要精简"     ← 认知循环
    ↓
重写 board.js (13,384 chars) → Write 成功  ← 内容几乎相同
    ↓
... 重复 7 次
```

**关键证据**（来自保存的请求报文 `160244_0015_m53_t27.json`）：

```
[50] assistant: "文件内容太长，需要精简。让我重新编写更简洁的 board.js："
[50] assistant: tool_use: Write file=board.js content=13384 chars
[51] tool_result: "The file has been updated successfully."
[52] assistant: "文件内容太长，需要精简。让我重新编写更简洁的 board.js："  ← 完全相同的文本
[52] assistant: tool_use: Write file=board.js content=13384 chars             ← 几乎相同的内容
[53] tool_result: "The file has been updated successfully."
```

### 6.3 与 Read 死循环的对比

| 维度 | Read 死循环（09:47） | Write 认知循环（16:02） |
|------|---------------------|----------------------|
| **工具** | Read | Write |
| **根因** | 无法理解错误信息（Wasted call） | 认知不一致（成功后仍要精简） |
| **tool_result** | 错误信息（"Wasted call"） | 成功信息（"updated successfully"） |
| **内容变化** | 每次相同（读同一文件） | 每次几乎相同（13384→13384→13384） |
| **循环频率** | ~2s/次（219 次） | ~3min/次（7 次） |
| **模型自知** | 无（不理解为何失败） | 有（知道文件已更新，但认知矛盾） |
| **Loop detection** | 未部署 | 已部署，正确触发 |
| **break 消息效果** | 立即生效（3 次后停止） | 当次生效但跨请求无效 |

### 6.4 Loop Detection 的局限性

Loop detection 正确检测到并注入了 break 消息，但出现了**跨请求循环**问题：

1. **单次请求内**：模型收到 break 后确实改变了行为（输出 "文件内容太长，无法继续编写"）
2. **下一次请求**：Claude Code 客户端将上一轮的结果作为新请求的历史，模型在新上下文中又回到 Write 循环
3. **max_run 持续增长**：3→4→5→6→7→8，每次请求都在历史中累积 Write 调用

代理日志证据：
```
[16:02:44] Consecutive calls: max_run=3 tool=Write total_calls=22
[16:02:44] Loop detected: Write called 3 times with same args, injected break message
[16:06:00] Consecutive calls: max_run=4 tool=Write total_calls=23
[16:06:00] Loop detected: Write called 3 times with same args, injected break message
[16:09:11] Consecutive calls: max_run=5 tool=Write total_calls=24
...
[16:22:55] Consecutive calls: max_run=8 tool=Write total_calls=27
[16:22:55] Loop detected: Write called 3 times with same args, injected break message
```

**每 3 分钟重复一次**：模型花费 ~3 分钟生成 13K chars 的 Write 内容，提交成功后立即在下一轮重复。

### 6.5 最终停止

16:19 和 16:23 模型输出纯文本（192 chars 和 102 chars），之后无新请求。可能原因：
- Claude Code 的客户端重试限制
- 用户手动停止
- 模型在累积足够多 break 消息后终于改变策略（输出 "将文件拆分成多个文件"）

### 6.6 Insight

#### 6.6.1 循环有两种类型：错误驱动 vs 认知驱动

- **错误驱动循环**（Read）：模型不理解错误信息，机械重复。break 消息对这类循环**高度有效**。
- **认知驱动循环**（Write）：模型理解成功信息，但内部认知矛盾（"需要精简"vs"已经精简了"）。break 消息对这类循环**仅在当次有效**，跨请求后模型恢复相同认知。

**启示**：Loop detection 需要增加**跨请求循环检测**能力。当 session 的 max_run 持续增长且 loop_detected > N 时，应注入更强的终止信号（如直接修改 tool_result 内容告知"此文件已重复写入多次，请更换策略"）。

#### 6.6.2 Write 去重可作为补充防御

当前 loop detection 基于工具名+参数的精确匹配。可以增加**语义级去重**：
- 检测到同一文件路径的连续 Write
- 比较内容相似度（如 Jaccard 或 edit distance）
- 相似度 > 80% 时，在 tool_result 中附加："注意：此文件内容与上次写入高度相似（相似度 99%），请确认是否需要继续修改"

#### 6.6.3 长文本生成是循环的触发条件

board.js 的 Write 内容为 13,384 chars（~4K tokens），模型需要 ~3 分钟生成。在这 3 分钟内：
- 模型"忘记"了之前的 Write 结果（因为 tool_result 是在生成完成后才注入的）
- 生成结束时，模型看到的上下文中只有 "updated successfully" 而非实际内容
- 模型再次基于 "文件太长" 的初始认知开始新一轮精简

**启示**：对于长文本 Write，可以在 tool_result 中保留内容摘要（如前 500 字符 + 总行数 + 关键函数列表），帮助模型维持对已写入内容的记忆。

---

## 附录 A：请求文件命名规则

```
/tmp/anthropic_requests/HHMMSS_NNNN_m{msg_count}_t{tool_count}.json

示例: 095957_0022_m11_t20.json
  时间: 09:59:57
  序号: 0022（本次代理启动后的第 22 个请求）
  消息数: 11
  工具数: 20
```

## 附录 B：代理层修复代码位置

| 修复 | 文件 | 函数/位置 |
|------|------|----------|
| 循环检测 | `anthropic_proxy.py` | `_handle_messages()` ~L1870 |
| 语义保留 | `anthropic_proxy.py` | `clear_old_tool_results()` ~L553 |
| KEEP 配置 | `configs/rapid-mlx-9b.conf` | `PROXY_TOOL_KEEP=10` |

## 附录 C：相关文档

- `docs/proxy-context-window-design.md` v5 — 代理层上下文窗口设计文档（含本次修复的技术细节）
- `docs/rapid-mlx-cache-analysis.md` — Prefix cache 原始分析
- `docs/rapid-mlx-cache-analysis-supplement.md` — Prefix cache 修正分析
