# PRD：OpenCode 与本地代理兼容性跟踪

> **文档版本**: v1.0
> **创建日期**: 2026-06-25
> **最后修订**: 2026-06-25
> **状态**: 进行中 (In Progress)
> **作者**: 代理层问题排查
> **关联文档**: `PRD-anthropic-proxy.md`、`DEFECT-LIST.md`、`PRD-intelligent-model-routing.md`
> **关联代码**: `message_converter.py`、`pipeline.py`、`configs/gemma4-26b.conf`

---

## 目录

1. [背景与问题概述](#1-背景与问题概述)
2. [根因分析](#2-根因分析)
3. [已实施的修复](#3-已实施的修复)
4. [当前影响评估](#4-当前影响评估)
5. [遗留问题与风险](#5-遗留问题与风险)
6. [后续跟踪项](#6-后续跟踪项)
7. [建议](#7-建议)
8. [附录 A：关键日志证据](#附录-a关键日志证据)
9. [附录 B：相关配置](#附录-b相关配置)

---

## 1. 背景与问题概述

### 1.1 使用场景

OpenCode 作为 Claude Code 的替代客户端，通过 Anthropic SDK 连接本地代理 (`anthropic_proxy.py:4000`)，由代理根据上下文大小智能路由到：
- **本地后端**: `rapid-mlx` + `mlx-community/gemma-4-26b-a4b-it-4bit`
- **云端后端**: DeepSeek API (`deepseek-v4-flash`)

### 1.2 发现的问题

| 问题 | 现象 | 严重程度 |
|---|---|---|
| **云端 400 错误** | OpenCode 会话上下文超过 50K 字符后，代理将请求路由到 DeepSeek，DeepSeek 返回 `400 Bad Request`，触发 fallback 到本地，导致请求延迟 60-80 秒 | P0 |
| **工具消息链断裂** | OpenCode 使用纯文本 `[Calling tool...` 表示工具调用，代理在 Anthropic→OpenAI 转换时无法构造合法的 `tool_calls`/`role="tool"` 配对 | P0 |
| **本地大上下文延迟高** | 大上下文 fallback 到本地后，`rapid-mlx` 处理 70K+ 字符需要 60 秒以上 | P1 |
| **循环/截断潜在影响** | OpenCode 多轮工具调用场景下，代理的循环检测和上下文截断可能误伤正常流程 | P2 |

### 1.3 问题时间线

- **2026-06-25 09:49**: OpenCode 会话 `cli_e646` 启动，连接本地代理。
- **09:50:17**: 首次上下文超过 50K，路由到 DeepSeek，成功（7.3s）。
- **09:50:25 / 09:51:33**: 后续两个大请求均因 DeepSeek 400 fallback 到本地，耗时 67.7s / 80.2s。
- **10:05**: 定位根因为 Anthropic→OpenAI 工具消息转换丢失 `tool_call_id` 和 `tool_calls` 配对。
- **10:15**: 完成 `message_converter.py` 修复并热重载代理。
- **10:16**: 验证通过代理到 DeepSeek 的请求恢复 200。

---

## 2. 根因分析

### 2.1 OpenCode 的两种访问模式

| 模式 | 协议 | 目标 | 工具调用格式 |
|---|---|---|---|
| **直接访问 DeepSeek** | OpenAI chat completions | `https://api.deepseek.com/v1` | OpenCode 使用 `opencode-go` provider，工具调用为原生 OpenAI 格式，工具链完整 |
| **通过本地代理** | Anthropic Messages API | `http://127.0.0.1:4000/v1/messages` | OpenCode 使用 Anthropic SDK，model=`claude-sonnet-4-6`，工具调用以纯文本 `[Calling tool...` 呈现 |

### 2.2 代理转换缺陷

OpenCode 通过代理时发送的 Anthropic 格式消息存在以下特征：

1. **Assistant 消息无结构化 tool_use**：
   - 内容仅为 `"[Calling tool"` 文本，没有 `{"type": "tool_use", "id": "...", "name": "...", "input": {...}}` 块。
   - 代理无法从中提取 `tool_calls`。

2. **Tool 结果消息为 OpenAI 兼容格式**：
   - `{"role": "tool", "tool_call_id": "call_xxx", "content": "..."}`
   - 代理原转换逻辑会丢失 `tool_call_id`。

3. **OpenAI/DeepSeek 校验失败**：
   - 错误 1：`messages[N]: missing field tool_call_id`
   - 错误 2：`Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`

### 2.3 为什么本地模型不受影响

`rapid-mlx` 对 OpenAI 消息格式的校验较宽松，能够容忍缺少 `tool_calls` 的 `role="tool"` 消息；而 DeepSeek 执行严格校验，直接返回 400。

### 2.4 GitHub 相关 Issue 调研

在 GitHub 上检索 OpenCode / Vercel AI SDK / Claude Code 生态，发现**工具消息链断裂（orphan `tool_use` / `tool_result`）是已知且长期存在的问题**，但**我们当前观察到的具体变体**（OpenCode 发送纯文本 `[Calling tool...` 的 assistant 消息，而非结构化 `tool_use` blocks）没有精确匹配的公开 issue。

#### 2.4.1 与 OpenCode 内部 orphan tool 相关的问题

OpenCode 主仓库存在大量用户报告 `tool_use ids were found without tool_result blocks` 的 issue，说明其消息持久化或 pre-request 层在多种场景下会破坏 tool 配对：

| Issue | 标题 | 关键结论 |
|---|---|---|
| [anomalyco/opencode#17065](https://github.com/anomalyco/opencode/issues/17065) | Session compaction produces orphaned `tool_use` blocks | OpenCode 的 compaction 会丢弃 `tool_result` 但保留对应的 `tool_use`，导致 Anthropic API 400。已通过隔离代理变量确认根因在 OpenCode 侧。 |
| [anomalyco/opencode#10616](https://github.com/anomalyco/opencode/issues/10616) | `messages.87: tool_use ids were found without tool_result blocks immediately` | 无插件场景下的 orphan tool_use 报错。 |
| [anomalyco/opencode#8377](https://github.com/anomalyco/opencode/issues/8377) | Most sessions eventually gets an tool_use error | 长会话普遍出现 tool_use 配对错误，必须 fork 才能恢复。 |
| [anomalyco/opencode#5750](https://github.com/anomalyco/opencode/issues/5750) | Tool use id bug |  |
| [anomalyco/opencode#4802](https://github.com/anomalyco/opencode/issues/4802) | `tool_use ids were found without tool_result` blocks | 中断响应时产生。 |
| [anomalyco/opencode#3230](https://github.com/anomalyco/opencode/issues/3230) | Each `tool_use` block must have a corresponding `tool_result` block when interrupting | 工具调用期间中断会话会破坏配对。 |
| [anomalyco/opencode#2720](https://github.com/anomalyco/opencode/issues/2720) | AI_APICallError: tool_use blocks found without corresponding tool_result blocks |  |
| [anomalyco/opencode#2214](https://github.com/anomalyco/opencode/issues/2214) | `AI_APICallError: messages.3: tool_use ids were found without tool_result blocks` |  |
| [anomalyco/opencode#1662](https://github.com/anomalyco/opencode/issues/1662) | `tool_use ids were found without tool_result` blocks | 用户通过插件扫描 orphan tool_use 自救。 |
| [anomalyco/opencode#22808](https://github.com/anomalyco/opencode/issues/22808) | 400 Error: `InternalError.Algo.InvalidParameter` due to unclosed tool_calls after interruption | 中断后产生 dangling `tool_calls`，建议 OpenCode 实现 "Tombstone" 模式自动注入占位 tool 消息。这与本代理 `_normalize_orphan_tool_messages` 的防御性思路一致。 |

#### 2.4.2 Vercel AI SDK 层的 orphan tool 问题

OpenCode 的 Anthropic provider 底层依赖 `@ai-sdk/anthropic`。该 SDK 也存在 adapter 产生 orphan `tool_use` 的问题：

- [vercel/ai#14259](https://github.com/vercel/ai/issues/14259)：`@ai-sdk/anthropic` adapter 在 tool call 处于 `output-error` 状态时，会生成缺少对应 `tool_result` 的 `tool_use` block。诊断证据显示 `ProviderTransform.message()`（OpenCode 的 normalization 层）保留了所有 tool 消息，但最终 Anthropic API payload 仍然丢失配对，说明问题出在 Vercel AI SDK adapter 内部。

这进一步说明：即使 OpenCode 的 internal message 层面配对正确，最终发往代理/上游的 Anthropic payload 仍可能断裂。

#### 2.4.3 OpenCode Go / DeepSeek 桥接层问题

OpenCode Go（`opencode.ai/zen/go`）作为 Anthropic 兼容代理时，已有多个关于 DeepSeek 格式转换的 issue：

- [anomalyco/opencode#24224](https://github.com/anomalyco/opencode/issues/24224)：Claude Code 经 OpenCode Go 访问 `deepseek-v4-pro` 时，Anthropic 格式的 tool 定义被转换为 OpenAI 格式时丢失 `tools[0].function.name`，导致 DeepSeek 400。
- [NousResearch/hermes-agent#51381](https://github.com/NousResearch/hermes-agent/issues/51381)：Hermes Agent 经 OpenCode Go `/zen/go/v1` 访问 DeepSeek 时同样遇到 `tools[0].function: missing field name`。

这说明 **OpenCode Go 的 Anthropic→OpenAI 工具格式桥接存在系统性缺陷**；我们的本地代理虽然与 OpenCode Go 不是同一组件，但承担着类似的协议转换职责，因此需要同样防御性地处理工具格式。

#### 2.4.4 社区桥接方案

已有社区项目专门解决 Claude Code + OpenCode Go + DeepSeek 的协议转换：

- [superheroYu/deepseek-v4-opencode-claude-code-bridge](https://github.com/superheroYu/deepseek-v4-opencode-claude-code-bridge)：本地 Anthropic→OpenAI 协议桥，处理 `tool_use`/`tool_result`、`reasoning_content`、工具 schema 等转换。

该项目的存在说明此场景足够普遍，且 OpenCode 原生桥接尚未完全解决。

#### 2.4.5 调研结论

1. **orphan tool 是 OpenCode / Vercel AI SDK 的已知问题**：大量 issue 覆盖 compaction、中断、error-state tool 等场景。
2. **OpenCode Go 的 DeepSeek 桥接有类似格式缺陷**：tool name 丢失、reasoning_content 处理等问题已被报告。
3. **未发现精确匹配我们观察的 issue**：即 OpenCode 通过 Anthropic SDK 向本地代理发送纯文本 `[Calling tool...` assistant 消息，而不是结构化 `tool_use` blocks。这可能是因为：
   - 该行为是 OpenCode 在特定 provider/model 组合下的降级/表示方式；
   - 或问题发生在 OpenCode 内部 message 到 API payload 的转换阶段，尚未被单独报告。
4. **代理侧防御性修复合理**：在 OpenCode 侧彻底解决之前，本地代理继续执行 orphan tool 归一化是必要的兼容性措施。

---

## 3. 已实施的修复

### 3.1 代码修改

#### 3.1.1 `message_converter.py` — 防御性工具链修复

1. **保留 `role="tool"` 消息的 `tool_call_id` 和 `name`**
   - 在 `convert_anthropic_messages_to_openai()` 的字符串内容分支中，当输入已经是 OpenAI 兼容格式时，保留 `tool_call_id` 和 `name`。

2. **增强 `_normalize_orphan_tool_messages()`**
   - 覆盖更多边界情况：
     - assistant 包含部分 `tool_calls` 时，额外 orphan tool 仍被归一化；
     - 连续多个 orphan tool 序列被正确分隔；
     - 从 assistant 文本中提取可能的 `tool_call_id`（OpenAI / Anthropic 两种格式），并在转换后的 user 消息前缀中标注 "referenced in previous assistant message"。
   - 使用 tool-response zone 判断，确保只有紧跟 assistant 的 orphan tool 才被转换。

3. **新增 `_ensure_tool_chain_integrity()` + tombstone 注入**
   - 在 orphan 归一化之后运行，为仍然缺少响应的 assistant `tool_calls` 自动注入占位 `role="tool"` 消息。
   - 避免 DeepSeek / OpenAI 因 "tool_calls must be followed by tool messages" 返回 400。
   - tombstone 内容包含缺失的 `tool_call_id` 和错误说明。

#### 3.1.2 `pipeline.py` — Cloud 400 诊断与修复重试

1. **Cloud API 错误日志增强 (`_log_cloud_error`)**
   - 当 Cloud API 返回 400/500 时，将完整请求体（脱敏后）和响应体写入 `logs/cloud_errors_YYYYMMDD.jsonl`。
   - 记录 session_id、request_id、route_target、status、model 等字段，便于事后诊断。
   - 修复了原代码中 `e.read()` 被调用两次导致第二次为空的 bug。

2. **Cloud 400 本地修复重试 (`_is_repairable_format_error` + `_repair_openai_messages`)**
   - 当 Cloud 返回 400 且响应体包含 `tool_call_id` / `tool_calls` / `tool_use` / `missing field` 等关键词时，判定为可修复格式错误。
   - 对 `ctx.openai_body["messages"]` 重新运行 `_normalize_orphan_tool_messages()` 和 `_ensure_tool_chain_integrity()`。
   - 用修复后的消息体重试云端一次；成功则避免 fallback 到本地（节省 60-80s）。
   - 重试失败则继续原有 fallback 逻辑，并记录第二次错误。

3. **OpenCode 客户端识别增强**
   - `PipelineContext` 新增 `client_type` 字段。
   - `_handle_messages()` 从 `User-Agent` 提取 client_type 并写入 ctx。
   - `RequestParser` 在 `REQ_SUMMARY` 日志和 metrics 中输出 `client_type`。
   - `BackendDispatcher` 在 metrics 中输出 `client_type`，便于按客户端分析成功率和 fallback 率。

#### 3.1.3 路由配置调优

**文件**: `configs/gemma4-26b.conf` + `manage.sh`

- `PROXY_ROUTE_THRESHOLD_CHARS`: 50,000 → **80,000**（减少短请求上云）
- `PROXY_ROUTE_STICKY`: 默认 true → **false**（上下文下降后允许回到本地）
- `PROXY_ROUTE_STICKY_RETURN_ROUNDS`: 默认 5 → **3**（如未来启用 sticky，更快返回本地）
- `PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS`: 300 → **120**（缩短 cloud 失败后的冷却时间）
- `manage.sh` 新增传递 `PROXY_ROUTE_STICKY`、`PROXY_ROUTE_STICKY_RETURN_ROUNDS`、`PROXY_ROUTE_STICKY_RETURN_RATIO` 三个环境变量。

### 3.2 测试补充

| 文件 | 新增/更新内容 |
|---|---|
| `test/unit/test_message_converter.py` | 新增 `TestNormalizeOrphanToolMessages`：部分 tool_calls + orphan、文本 id 提取、多 orphan runs 等；新增 `TestEnsureToolChainIntegrity`：tombstone 注入、user 前注入、完整配对不注入、组合 orphan+dangling。 |
| `test/unit/test_proxy_fallback.py` | 更新 `test_tool_use_message` / `test_mixed_text_and_tool_use`，验证 dangling tool_call 会生成 tombstone。 |
| `test/unit/test_cloud_error_logging.py` | 新增：验证 `_log_cloud_error` 写入每日轮换 JSONL、脱敏 `api_key`、截断长响应体。 |
| `test/unit/test_cloud_format_repair.py` | 新增：验证 Cloud 400 修复重试成功、修复失败 fallback、非 400 不触发修复。 |

### 3.3 验证结果

- **单元测试**: `test/unit/` 全部 **826 passed**（新增 14 个测试）。
- **直接重放 OpenCode 请求体到 DeepSeek**: 从 400 变为 **200 OK**。
- **代理实时日志**: 修复后通过代理到 DeepSeek 的请求均返回 `backend status: 200`。
- **热重载**: 已通过 `./manage.sh reload` 生效，无需重启后端。
- **配置语法**: `bash -n manage.sh` 通过。

---

## 4. 当前影响评估

### 4.1 对 OpenCode 云端路由的影响

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 大上下文云端 400 率 | 高（连续两次 400） | 0（已验证 200） |
| Fallback 延迟 | 67-80 秒 | 不再需要 fallback |
| 云端请求可用性 | 不可用 | 可用 |
| 工具结果语义 | 可能丢失 tool_call_id | 保留在 user 消息内容中 |

### 4.2 对 OpenCode 本地路由的影响

本地路径不经过 DeepSeek 格式校验，修复前后基本无变化。但需注意以下代理层操作对 OpenCode 的潜在影响：

| 代理操作 | 影响 |
|---|---|
| **Dynamic max_tokens** | expansion 阶段 32K → 4K，saturation 阶段更低，限制 OpenCode 单次输出长度 |
| **Context truncation (rounds)** | 长会话可能丢弃旧消息，导致 OpenCode "失忆" 或重复读取 |
| **Semantic compression** | >4K 的 tool_result 被压缩，可能丢失细节 |
| **Loop detection** | 多轮同类工具调用（如连续 read）可能被误判为循环，移除工具 |
| **Tool filtering** | 若开启，动态减少可用工具数量 |
| **Cache aligner** | 保护前 4 条消息，稳定前缀缓存，但减少可压缩空间 |

### 4.3 对代理可观测性的影响

修复后，metrics 中 `backend_dispatcher.route_target=cloud` 且 `route_fallback=false` 的记录会增加，云端成本会真实产生。需要加强成本监控。

---

## 5. 遗留问题与风险

### 5.1 语义降级风险

将 orphan tool 结果从 `role="tool"` 转成 `role="user"` 是一种**兼容性格式降级**。虽然避免了 400，但：
- 云端模型（DeepSeek）看到的结果不再是标准 tool result 格式；
- 模型需要依赖 `[tool result for call_xxx]:` 文本前缀理解这是工具输出；
- 对于依赖严格 tool 调用的复杂任务，可能影响准确性。

**风险等级**: 中

### 5.2 OpenCode 文本工具调用的根本问题

修复是代理侧的**防御性适配**，并未改变 OpenCode 发送非标准 Anthropic 消息的事实。如果 OpenCode 未来改变文本格式（例如 `[Calling tool` 不再出现），当前的修复逻辑可能失效或产生意外转换。

**风险等级**: 中

### 5.3 长上下文本地 fallback 延迟

虽然云端 400 已修复，但当云端不可用、预算耗尽或用户强制本地时，大上下文请求仍需要本地处理 60-80 秒。这不是格式问题，而是本地模型吞吐能力问题。

**风险等级**: 中

### 5.4 循环检测误伤

OpenCode 高频多轮工具调用，`tool_loop_detector` 的 `max_run` 容易接近阈值 3。当前会话已观察到 `max_run=2`，一旦达到 3 会触发工具移除。

**风险等级**: 低-中

### 5.5 配置耦合

当前修复对所有使用 Anthropic SDK 且发送 orphan tool 消息的客户端生效，不局限于 OpenCode。需要确保不会误伤其他正常客户端。

**风险等级**: 低

---

## 6. 后续跟踪项

### 6.1 短期（1-3 天）

| ID | 任务 | 状态 | 负责人 | 验收标准 |
|---|---|---|---|---|
| OC-1 | **监控云端 OpenCode 请求成功率** | 待执行 | 运维 | 连续 24 小时无 `Cloud API failed (400)`，cloud 请求成功率 >99% |
| OC-2 | **观察 orphan tool 转换后的响应质量** | 待执行 | 开发/PM | 对比修复前后 OpenCode 大上下文任务的完成率、工具调用正确率 |
| OC-3 | **补充端到端测试** | 部分完成 | 开发 | 单元测试已覆盖；待补充集成测试：模拟 OpenCode 文本工具调用 + orphan tool 消息，验证代理→DeepSeek 200 |
| OC-4 | **检查其他客户端兼容性** | 待执行 | 开发 | 验证 Claude Code、其他 Anthropic SDK 客户端不受 _normalize_orphan_tool_messages 影响 |

### 6.2 中期（1-2 周）

| ID | 任务 | 状态 | 负责人 | 验收标准 |
|---|---|---|---|---|
| OC-5 | **评估本地模型对 OpenCode 的支持边界** | 待执行 | 开发/PM | 输出报告：上下文大小、任务类型、工具调用频率的推荐本地/云端策略 |
| OC-6 | **优化 OpenCode 大上下文本地性能** | 待执行 | 开发 | 测试 `rapid-mlx-35b-opt` 或其他配置在大上下文下的表现，给出配置建议 |
| OC-7 | **Review loop_threshold 对 OpenCode 的适用性** | 待执行 | 开发 | 根据实际 `max_run` 分布，决定是否调整 `PROXY_LOOP_THRESHOLD` |
| OC-8 | **完善云端错误日志** | 已完成 | 开发 | Cloud API 400/500 时记录完整请求/响应体到 `logs/cloud_errors_YYYYMMDD.jsonl` |

### 6.3 长期（可选）

| ID | 任务 | 状态 | 负责人 | 验收标准 |
|---|---|---|---|---|
| OC-9 | **与 OpenCode 社区/文档确认标准工具调用格式** | 待执行 | PM/开发 | 确认 OpenCode 是否支持输出标准 Anthropic `tool_use` 块，或在代理侧增加更稳健的反解析；参考已有 issue：#17065、#22808、vercel/ai#14259、#24224 |
| OC-10 | **评估专用 OpenCode provider 模式** | 待执行 | 架构 | 是否让 OpenCode 直接以 OpenAI 格式连接代理（绕过 Anthropic→OpenAI 转换），降低复杂度 |

### 6.4 已完成的代理端增强（对应原建议清单 1-6）

| 原建议 | 实施文件 | 状态 |
|---|---|---|
| 1. 增强 orphan tool 归一化 | `message_converter.py` | ✅ 完成 |
| 2. 工具链完整性校验 + tombstone 注入 | `message_converter.py` | ✅ 完成 |
| 3. Cloud API 错误日志增强 | `pipeline.py` | ✅ 完成 |
| 4. OpenCode 客户端识别和专用 metrics | `pipeline.py`, `anthropic_proxy.py` | ✅ 完成 |
| 5. Cloud 400 后的本地修复重试机制 | `pipeline.py` | ✅ 完成 |
| 6. 路由策略调优（阈值、sticky） | `configs/gemma4-26b.conf`, `manage.sh` | ✅ 完成 |

---

## 7. 建议

### 7.1 对当前 OpenCode 会话

- 云端路由已恢复，可继续使用智能路由模式；
- 监控 `/status` 页面的 **Daily Cost** 和 **Cloud Ratio**，避免预算超支；
- 若任务对延迟敏感且上下文较小，可强制本地路由（`X-Proxy-Route-To: local`）。

### 7.2 对代理配置

- 保持 `PROXY_ROUTE_ENABLED=true`，但建议关闭 sticky 或缩短 sticky 返回周期，避免一旦上云就长期锁定；
- 根据 OpenCode 使用频率，评估是否需要独立的 `PROXY_ROUTE_THRESHOLD_CHARS` 调低或调高；
- 考虑为 OpenCode 会话单独设置路由策略（如基于 `User-Agent` 或 session 前缀）。

### 7.3 对代码演进

- `_normalize_orphan_tool_messages` 是当前防御性修复，长期应考虑：
  - 在 OpenCode 侧推动标准 `tool_use` 块输出；或
  - 在代理侧实现更智能的文本工具调用反解析（从 `[Calling tool` 文本中提取工具名和参数）。

---

## 附录 A：关键日志证据

### A.1 修复前：DeepSeek 400 + Fallback

```
[09:50:25] [INFO] [sess=cli_e646]   -> Forwarding to https://api.deepseek.com/v1/chat/completions (cloud, model=deepseek-v4-flash)
[09:50:26] [INFO] [sess=cli_e646]   <- Cloud API failed (400), checking fallback...
[09:50:26] [INFO] [sess=cli_e646]   -> Cloud failure non-retryable (400), no cooldown
[09:50:26] [INFO] [sess=cli_e646]   -> Fallback to local backend
[09:50:26] [INFO] [sess=cli_e646]   <- backend status: 200
[09:51:32] [INFO] [sess=cli_e646]   -> [backend_dispatcher] completed in 67698.2ms
```

### A.2 修复后：DeepSeek 200

```
[10:16:16] [INFO] [sess=cli_e646]   -> Forwarding to https://api.deepseek.com/v1/chat/completions (cloud, model=deepseek-v4-flash)
[10:16:17] [INFO] [sess=cli_e646]   <- backend status: 200
[10:16:19] [INFO] [sess=cli_e646]   -> [route_cost] daily cost now ¥11.9422
[10:16:19] [INFO] [sess=cli_e646]   -> [backend_dispatcher] completed in 2871.2ms
```

### A.3 转换前的问题示例

Anthropic 输入：
```json
{"role": "assistant", "content": "[Calling tool"}
{"role": "tool", "tool_call_id": "call_xxx", "content": "file contents"}
```

转换前 OpenAI 输出：
```json
{"role": "assistant", "content": "[Calling tool"}
{"role": "tool", "content": "file contents"}   // 缺少 tool_call_id
```

转换后 OpenAI 输出：
```json
{"role": "assistant", "content": "[Calling tool"}
{"role": "user", "content": "[tool result for call_xxx]:\nfile contents"}
```

---

## 附录 B：相关配置

当前活跃配置 `configs/gemma4-26b.conf` 中与 OpenCode 相关的关键项：

```bash
# 智能路由
PROXY_ROUTE_ENABLED=true
PROXY_ROUTE_THRESHOLD_CHARS=50000
PROXY_CLOUD_MODEL=deepseek-v4-flash

# 上下文管理
PROXY_CTX_LIMIT_ENABLED=true
PROXY_CTX_CHARS_LIMIT=180000
PROXY_CTX_TRUNCATE_STRATEGY=rounds
PROXY_CTX_KEEP_ROUNDS=15
PROXY_CACHE_ALIGN_HEAD=4

# 压缩
PROXY_COMPRESS_ENABLED=true
PROXY_COMPRESS_THRESHOLD=4096

# 输出限制
PROXY_MAX_TOKENS_OVERRIDE=32768
PROXY_DYNAMIC_MAX_TOKENS_ENABLED=true

# 循环/拦截
PROXY_LOOP_THRESHOLD=3
PROXY_BLOCKER_ENABLED=true
PROXY_BLOCKER_THRESHOLD=2
```

---

*本文档为 OpenCode 与代理兼容性问题的跟踪 PRD，将根据后续监控数据和修复进展持续更新。*
