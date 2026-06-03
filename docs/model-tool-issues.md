# 本地模型 Tool Calling 质量问题记录

> 记录 Qwen3.6-35B-A3B-4bit（rapid-mlx）在 Claude Code 代理模式下产生的 tool calling 参数错误。

---

## Issue #1: Write 工具缺少 `content` 参数

### 时间
- **日期**: 2026-06-03
- **首次出现**: 11:27:55
- **后端**: rapid-mlx PID 48844
- **代理**: anthropic_proxy.py PID 48997
- **配置**: rapid-mlx-35b.conf（C组超宽松参数: threshold=100K, keep=5）

### 用户任务
在 `/Users/jinsongwang/workspace/test1` 目录下编写一个贪吃蛇游戏。

### 报文完整内容

**请求模型**: `claude-sonnet-4-6`（本地模型别名）
**Max tokens**: 32000
**Stream**: True

#### Msg 0 (user)
```
[0] text: "<system-reminder>...Today's date is 2026/06/03...</system-reminder>"
[1] text: "编写一个贪吃蛇游戏"
```

#### Msg 1 (assistant)
```
[0] text: "<|mask_start|><think>...Let me build a complete, playable Snake game using HTML/CSS/JavaScript in a single file...</think>"
[1] tool_use: Bash
    name: Bash
    id: call_90892147...
    input: {"command": "ls /Users/jinsongwang/workspace/test1/", "description": "List current working directory"}
```

#### Msg 2 (user) — Bash 返回
```
[0] tool_result:
    tool_use_id: call_90892147...
    content: '(Bash completed with no output)'
```

#### Msg 3 (assistant)
```
[0] text: "目录为空，我来创建一个贪吃蛇游戏。"
[1] tool_use: TaskCreate
    name: TaskCreate
    id: call_f7b6049b...
    input: {"subject": "创建贪吃蛇游戏", "description": "使用HTML+CSS+JavaScript创建完整的贪吃蛇游戏..."}
```

#### Msg 4 (user) — TaskCreate 返回
```
[0] tool_result:
    tool_use_id: call_f7b6049b...
    content: 'Task #1 created successfully: 创建贪吃蛇游戏'
```

#### Msg 5 (assistant) — ❌ 问题所在
```
[0] tool_use: Write
    name: Write
    id: call_84e49a44...
    input: {"file_path": "/Users/jinsongwang/workspace/test1/index.html"}
    
    ⚠️ MISSING: "content" parameter
```

#### Msg 6 (user) — Write 失败
```
[0] tool_result:
    tool_use_id: call_84e49a44...
    content: '<tool_use_error>InputValidationError: Write failed due to the following issue:\nThe required parameter `content` is missing</tool_use_error>'
```

### 根因分析

**这不是代理转换的问题。**

代理的 `parse_tool_arguments()` 忠实地解析了后端模型生成的 JSON：
```json
{"file_path": "/Users/jinsongwang/workspace/test1/index.html"}
```

模型**明确知道**要写 `index.html`，并且在 thinking 内容中已规划了完整的游戏实现（Canvas、键盘控制、计分、碰撞检测等），但在生成 tool_call arguments 时**遗漏了 `content` 参数**。

**可能原因**:
1. 模型分步思维与 tool_call 生成脱节：thinking 中完成了设计，但 tool_call JSON 只捕获了文件路径
2. Qwen3.6-35B 的 tool calling 在复杂多参数场景（Write 需要 file_path + content）下不稳定
3. Chat template 对 Write 工具的参数描述可能在某些情况下被模型忽略

### 影响
- Write 工具无法执行，文件未创建
- 任务进度受阻
- 需要模型重新尝试或用户干预

---

## Issue #2: Edit 工具 `replace_all` 类型错误

### 时间
- **日期**: 2026-06-03
- **首次出现**: C组实验期间（约 10:11–10:53）
- **后端**: rapid-mlx
- **任务**: 为 anthropic_proxy.py 添加 M1/M2 功能

### 现象
Edit 工具调用时，`replace_all` 参数有时为 JSON boolean `false`，有时为字符串 `"False"`：

| 调用 | `replace_all` 值 | 类型 | 结果 |
|------|-----------------|------|------|
| #1 | `false` | bool | ✅ 成功 |
| #2 | `"False"` | str | ❌ 失败 |
| #3 | `false` | bool | ✅ 成功 |
| #4 | `"False"` | str | ❌ 失败 |
| #5 | `"False"` | str | ❌ 失败 |
| #6 | `false` | bool | ✅ 成功 |

### 根因分析
后端模型生成 tool_call arguments 时不稳定，将 boolean 值输出为字符串。

### 修复状态
✅ **已在代理层修复**（commit: 待记录）
- 新增 `_coerce_booleans()` 函数
- 在 `parse_tool_arguments()` 的所有返回路径上包裹后处理
- 将 `"True"`/`"False"`/`"true"`/`"false"` 自动转为 Python bool

---

## 共性问题总结

| 问题 | 责任方 | 修复层面 | 状态 |
|------|--------|---------|------|
| `replace_all` 字符串化 | 后端模型 | 代理层后处理 | ✅ 已修复 |
| Write 缺少 `content` | 后端模型 | **无法代理层修复** | ❌ 待观察 |

### 代理层能做什么
- boolean 字符串化 → 可以后处理转换 ✅
- 参数完全缺失 → 代理无法凭空补全 ❌

### 建议
1. **Issue #1（Write 缺 content）**: 需在 chat template 或模型层面改善。若频繁出现，可考虑在代理层对 Write 工具做参数补全提示（如检测到缺少 content 时返回特定错误信息引导模型）。
2. **Issue #2（boolean 字符串化）**: 已修复，持续观察是否覆盖所有变体。
3. **持续记录**: 后续出现新的 tool calling 质量问题时追加到此文档。

---

*最后更新: 2026-06-03 11:28*
