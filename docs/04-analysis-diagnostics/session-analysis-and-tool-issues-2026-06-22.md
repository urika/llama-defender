# Session Analysis & Tooling Issues Log（2026-06-22）

## 背景

在实现 `/session?sid=<session_id>` 会话分析页面、优化 `/status` 布局以及提交代码的过程中，发现并修复/记录了以下问题。

---

## 问题 1：Session Trace 只显示最后一次请求，且时间戳缺少日期

### 现象
`/status` 页面的 **Session Trace** 卡片只展示 `/tmp/anthropic_request_body.json` 中的内容。该文件在每次请求时被覆盖，因此卡片里永远是“最近一次请求”。由于之前只显示 `%H:%M:%S`，用户容易把 `23:42:30` 误认为是“昨天 23 点”的数据。

### 原因
- `Session Trace` 的设计定位是“当前/最近请求体快照”，不是历史会话分析。
- `/tmp/anthropic_request_body.json` 在代理重启后仍然保留在 `/tmp`，若长时间没有新请求，就会一直显示旧数据。

### 修复
- `Captured At` 现在显示完整日期时间：`2026-06-22 23:42:30`。
- 增加 staleness 标识：
  - `● 实时`（5 分钟内）
  - `● 非实时 (>5min)`
  - `● 已过期 (>1h)`

### 建议
需要看历史完整会话应使用新页面 `/session?sid=<session_id>` 或 CLI 工具 `tools/analyze_session.py`。

---

## 问题 2：`/session?sid=<session_id>` 页面没有明显入口

### 现象
会话分析页面上线后，用户不知道从哪里进入。

### 原因
- `/status` 页面之前没有提供历史会话列表。
- 只有 `Intelligent Routing` 卡片里的 `Active Sessions` 表会在会话活跃时显示可点击的 session id，但会话结束后链接就消失了。

### 修复
在 `/status` 页面 `Session Trace` 下方新增 **📁 Recent Sessions** 卡片：
- 从 `logs/proxy_metrics.jsonl` 读取最近活跃的 12 个 `session_id`。
- 每个 id 都是 `/session?sid=...` 的链接。
- 显示请求数和最后活跃时间。

---

## 问题 3：一次 commit 混入了无关的 force-mode 路由改动

### 现象
使用 `git commit -am` 提交 UI 改动时，把 `pipeline.py`、`proxy_state.py`、`test/unit/test_smart_router.py`、`test/unit/test_pipeline_stages.py` 中已有的 force-mode 路由改动也一起提交了。

### 原因
`git commit -am` 会提交所有已跟踪文件的修改，包括工作区里未完成的其他功能代码。

### 修复
通过 `git reset --soft HEAD~1` 撤销该 commit，然后拆分为两个独立提交：
1. `feat(routing): add model force mode with no-fallback guard`
2. `ui(status): add Recent Sessions entry point and Session Trace staleness badge`

### 建议
- 提交前先用 `git status`/`git diff --stat` 确认改动范围。
- 不要把 `-am` 当成“提交当前功能”的快捷方式，特别是在工作区有多个并行改动时。

---

## 问题 4：`Write` 工具调用出现 JSON 解析错误

### 错误信息
```
⚙invalid [tool=write, error=Invalid input for tool write: JSON parsing failed: Text: {.
Error message: JSON Parse error: Expected '}']
```

### 排查结论：模型侧问题，非代理问题

1. **代理未参与序列化**：`anthropic_proxy.py` 只处理 `/v1/messages`、`/status`、`/session` 等运行时端点，不解析 Kimi Code CLI 与工具之间的内部 JSON。
2. **错误来源是工具调用层**：`Write` 工具要求 `content` 参数是一个 JSON **字符串**。如果生成的 tool-call JSON 中 `content` 被写成了对象（例如 `"content": {`），或者字符串内的 `{`/`}` 没有被正确转义，解析器就会报 `Expected '}'`。
3. **本次触发场景**：用户要求“把发现的问题写到一个 md 文档中”，模型可能在构造 `Write` 调用时把 Markdown 内容的开头 `{` 或模板占位符错误地解析为 JSON 对象边界，导致整个 payload 结构损坏。

### 修复/缓解
- 使用 `Write` 工具时，确保 `content` 是完整字符串，避免以 `{` 开头（可用 Markdown 标题 `# ` 开头）。
- 对于已有文件的小幅修改，优先使用 `Edit` 而非 `Write`，减少大段内容中特殊字符导致 JSON 损坏的概率。
- 如果必须写入包含大量 `{`/`}` 的内容（如代码、JSON 示例），先写纯文本说明部分，再分段写入代码块。

### 后续可改进（如平台支持）
- 在工具调用层增加 JSON schema 校验，失败时给出更明确的错误（例如指出是 `content` 类型错误）。
- 为 `Write`/`Edit` 提供二进制/原始内容模式，绕过字符串转义问题。

---

## 当前状态

- 上述问题 1/2/3 已修复并提交。
- 代理已重启加载最新代码（`/status` 可见 `Recent Sessions`）。
- 工作区干净，无未提交改动。
