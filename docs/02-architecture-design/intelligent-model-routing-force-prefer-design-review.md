# 对 `intelligent-model-routing-force-prefer-design.md` 的评审意见

> **评审对象**：`docs/02-architecture-design/intelligent-model-routing-force-prefer-design.md`  
> **评审日期**：2026-06-22  
> **结论**：设计方向正确、改动轻量，可直接作为实现蓝图，但需在实施前处理几项安全与体验问题。

---

## 1. 总体评价

该设计很好地利用了现有 `MODEL_ROUTE_PREFERENCES` 和 `SmartRouter` 框架，通过新增 `behavior` 字段把模型 ID 从“偏好提示”升级为“路由意图”，满足用户在 Claude 端通过 `/model` 切换模型即可切换本地/云端的需求。改动范围小、语义清晰、向后兼容。

---

## 2. 认可点

| 序号 | 说明 |
|------|------|
| 1 | **字段设计简洁**：`behavior: "force" \| "prefer"` 与现有 `route_bias` 组合，不用推翻已有配置结构。 |
| 2 | **优先级插入合理**：Priority 0.5 直接短路，force 模式跳过阈值、内存、生命周期等自动决策，符合“用户明确意图优先”。 |
| 3 | **失败策略区分**：force 不 fallback，prefer 保持现有 fallback，透明且不会让用户在明确选 opus 时 unexpectedly 用到本地模型。 |
| 4 | **测试矩阵完整**：覆盖了 force cloud、prefer 智能路由、force 不 fallback 等关键路径。 |
| 5 | **向后兼容**：未知模型默认 `prefer`，现有行为不变。 |

---

## 3. 建议与风险

### 3.1 force cloud 时无 API key 的处理需要明确（P0）

设计提到：

> Cloud 无 API key → 自动 return `local`（现有逻辑，pipeline.py:1812-1820）

但又在失败处理里说 force 模式不 fallback、返回 503。这两处存在矛盾：

- 如果 SmartRouter 已经决定 `cloud`，BackendDispatcher 发现没有 key 就回退到 `local`，那等于 force 还是 fallback 了。
- **建议**：force 模式下若缺少 cloud key，应直接返回 503 或 400，并附带明确错误信息（如 `"cloud API key not configured for forced model claude-opus-4-7"`），避免用户以为自己在用云端实际上走了本地。

### 3.2 建议 force 模式仍受敏感内容策略约束（P1）

设计让 force 跳过 Priority 4/5 的 `sensitive_path` 检查。如果某类文件/内容被配置为禁止上云，用户仅通过换模型就能绕过，存在合规风险。

**建议**：

- 把 `sensitive_path` 检查提前到 Priority 0.5 之前，或在 force 分支内单独检查。
- 命中敏感规则时，对 force cloud 返回 `403` 或 `local_forced`，而不是直接上云。

### 3.3 预算硬上限是否应覆盖 force 模式？（P1）

设计让 force 跳过 `daily_budget_exceeded`。如果用户日常把模型切到 opus 后忘记切回，可能在一夜之间把预算打满。

**建议**：

- 增加配置项 `PROXY_ROUTE_FORCE_BYPASS_BUDGET`（默认 `false`）。
- 默认行为：即使 force，也要先检查预算；只有显式开启 bypass 时才跳过。

### 3.4 模型别名应只在 cloud 可用时暴露（P0）

`claude-opus-4-7` 是一个只能走云端的模型。如果当前配置没有 `PROXY_CLOUD_API_KEY`，`/v1/models` 仍返回它会导致用户选中后所有请求报错。

**建议**：

- 在 `get_model_aliases()` 中，对纯云端别名（如 `claude-opus-4-7`）增加判断：仅当 `PROXY_ROUTE_ENABLED` 且 cloud key 存在时才加入列表。
- 或者在 `/v1/models` 里给云端别名加 `description`/`owned_by` 提示，但 Claude Code CLI 不一定展示。

### 3.5 Header override 与 force 的优先级可再斟酌（P2）

设计把 `X-Proxy-Route-To` header 放在 Priority 0.6，低于 force 的 0.5。这意味着用户选 opus 后，即使显式 header `X-Proxy-Route-To: local` 也无效。

**建议**：

- 如果 header 是“单次请求显式覆盖”，放在 force 之后是合理的（模型选择更强）。
- 但如果 header 用于管理员/脚本强制回退，建议让 header 优先级高于 force，或增加一个 `X-Proxy-Route-Force` header。
- 文档里应明确说明：在 `behavior=force` 的模型下，header override 不生效。

### 3.6 建议补充会话级观察指标（P2）

`/status` 和 `/session` 页面当前已有 `route_target` 和 `route_reason`。force 模式下建议把 reason 写成：

```
model_forced_cloud(claude-opus-4-7 -> deepseek-v4-pro)
```

方便一眼看出是哪个模型触发的云端路由。

### 3.7 Claude Code CLI 的模型缓存问题（P2）

Claude Code CLI 可能在启动时缓存 `/v1/models` 列表。修改 `get_model_aliases()` 后，用户可能需要重启 Claude Code 才能看到 `claude-opus-4-7`。

**建议**：

- 在文档或配置注释里说明。
- 考虑提供 `/model claude-opus-4-7` 失败的降级提示。

### 3.8 建议给 haiku 也保留一个“强制本地”选项（P3）

当前 haiku 是 `prefer` + `prefer_local`。如果未来用户想明确“我这个小任务绝对不走云”，可以再增加一个 `claude-haiku-local-4-5` 或类似别名，配置 `behavior=force, route_bias=prefer_local`。当前设计已支持，只需在 `MODEL_ROUTE_PREFERENCES` 里加一行即可。

---

## 4. 修改建议清单

| 优先级 | 项 | 文件 |
|---|---|---|
| P0 | force 模式缺少 cloud key 时返回错误，不回退 local | `pipeline.py` BackendDispatcher |
| P0 | 云端-only 模型别名仅在 cloud key 存在时暴露 | `proxy_state.py` `get_model_aliases()` |
| P1 | force 模式仍受敏感路径策略约束 | `pipeline.py` SmartRouter |
| P1 | 增加 `PROXY_ROUTE_FORCE_BYPASS_BUDGET` 配置，默认 force 也检查预算 | `proxy_state.py`, `pipeline.py` |
| P2 | header override 与 force 优先级明确化并文档化 | `pipeline.py`, 设计文档 |
| P2 | reason 字符串包含目标云模型名 | `pipeline.py` SmartRouter |
| P2 | 更新 `/status`、`/session` 展示 force 来源 | `admin_server.py` |
| P3 | 增加 `claude-haiku-local-4-5` 等强制本地别名（可选） | `proxy_state.py` |

---

## 5. 结论

该设计方向正确、改动轻量，可以直接作为实现蓝图。但在实施前建议先处理 **P0 的 API key 回退矛盾和别名暴露条件**，否则会出现“用户选了 opus 却 silently 走本地”或“没有 cloud key 时列表里还有 opus”的糟糕体验。
