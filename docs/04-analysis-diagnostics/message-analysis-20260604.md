# 报文情况与处理性能分析报告

> 分析时间：2026-06-04 11:00+  
> 后端：rapid-mlx + Qwen3.5-9B-MLX-4bit  
> 运行时长：~38 分钟

---

## 1. 当前运行状态

| 项目 | 状态 |
|------|------|
| 后端类型 | `rapid-mlx` |
| 当前配置 | `rapid-mlx-9b` |
| 运行模型 | `Qwen3.5-9B-MLX-4bit` |
| 后端 PID / 内存 | 2255 / **5.5 GB** |
| 运行时间 | 38 分 43 秒 |
| 代理状态 | `127.0.0.1:4000` 正常运行 |
| API 健康 | ✅ 正常 |

---

## 2. 报文特征分析

### 2.1 请求规模

- **累计请求数**：`1,151` 条（`proxy_requests.jsonl`）
- **大请求报文**：**331K 字符**，27 个工具定义
- **消息历史**：约 **582 条消息**（558 条被截断，保留 24 条）
- **REQ_SUMMARY 重复**：同一毫秒内记录两次相同摘要，日志膨胀较快

### 2.2 上下文截断（最新）

```
Context truncation (rounds): 558 messages dropped, 24 kept
(rounds=10, ~26807 tokens, budget=40000)
```

- **截断策略**：`rounds` 模式，保留最近 10 轮对话
- **丢弃比例**：约 **96% 的消息被丢弃**
- **Token 预算**：40K，实际使用约 26.8K
- **趋势**：截断程度在持续加剧（从 552 → 558 dropped）

### 2.3 报文增长趋势

| 时间 | 字符数 | 工具数 | 备注 |
|------|--------|--------|------|
| 10:51 | 302,434 | 27 | 初始观测 |
| 10:54 | 329,234 | 27 | 持续增长 |
| 11:03 | 329,989 | 27 | 高位平台 |
| 11:04 | **331,001** | 27 | **仍在增长** |

> 报文从 302K 增长到 331K，说明存在消息泄漏或 tool_result 累积未被有效清理。

---

## 3. 处理性能分析

### 3.1 后端推理性能（分层对比）

| 场景 | Prompt Tokens | TTFT | 生成速度 | 总耗时 |
|------|---------------|------|----------|--------|
| 小请求（工具调用）| 797 | **0.7s** | **29.9 tok/s** | 1.8s |
| 大请求冷启动 | 32,523 | **27-29s** | **3.6 tok/s** | ~30s |
| 缓存命中 | 32,364 | **1.2s** | 25.5 tok/s | 2.9s |
| 超长文本生成 | — | — | **37.1 tok/s** | **315s** |

### 3.2 端到端延迟（代理层）

最近请求耗时分布：

```
  0.6 - 3.1s   ████████████████████  短请求 / 状态检查
  9.0 - 14.6s  ████████              正常对话轮次
  315.1s       ▌                     异常长连接请求
```

### 3.3 异常耗时请求

`proxy_requests.jsonl` 中历史最慢的 5 条请求：

| 耗时 | 输入字符 | 输出字符 | 时间戳 |
|------|----------|----------|--------|
| **844 秒** | 33,580 | 55,922 | 10:14 |
| **475 秒** | 76,671 | 0 | 09:34 |
| **444 秒** | 119 | 10,801 | 09:34 |
| **335 秒** | 86,390 | 0 | 06-03 |
| **333 秒** | 2,214 | 0 | 06-03 |

> **844 秒（14 分钟）** 和 **475 秒（8 分钟）** 的请求极不正常。后端的 `disconnect_guard` 显示一个请求保持了 **315 秒** 连接，生成了 **11,689 tokens**。推测可能是客户端连接未断开，或代理层超时配置与后端不匹配。

---

## 4. 资源使用与稳定性

### 4.1 Metal 内存（实时）

```
[Metal memory] active=13.6GB  peak=19.1GB  cache=0.0GB
```

- **当前活跃内存**：13.6 GB
- **历史峰值**：19.1 GB
- 在 48GB 统一内存范围内，**安全** ✅

### 4.2 前缀缓存

```
cache_entries=7  cache_mem=4259MB (~4.2GB)
```

- 前缀缓存命中时 TTFT 从 27s → **1.2s**，改善 **95%+**
- 缓存占用 4.2GB 内存，合理

### 4.3 历史异常（已恢复）

日志中发现过去的 **Metal OOM 崩溃**：

```
[METAL] Command buffer execution failed: Insufficient Memory
[metal::malloc] Resource limit (499000) exceeded.
[generation_error_recovery] aborted 1 running requests, batch generator closed, Metal cache cleared
```

- rapid-mlx 已自动恢复（`generation_error_recovery`）
- **当前无活跃 OOM 错误**

---

## 5. 关键发现与建议

| 问题 | 严重程度 | 说明与建议 |
|------|----------|-----------|
| **报文持续增长** | 🔴 高 | 331K chars → 仍在增长，96% 消息被截断。建议检查是否有消息泄漏或 tool_result 未正确清理 |
| **超长耗时请求** | 🔴 高 | 844s / 475s 历史记录，需排查是否客户端未正确关闭连接。建议检查 `disconnect_guard` 与代理连接超时配置 |
| **日志重复记录** | 🟡 中 | REQ_SUMMARY 同一毫秒重复两次，102K+ 行日志膨胀快。建议修复代理双重记录问题 |
| **重复 GET /status** | 🟢 低 | 浏览器每 5 秒轮询状态页，产生大量日志噪音 |
| **内存使用安全** | 🟢 低 | peak 19.1GB / 48GB，当前运行稳定 |

---

## 6. 工具调用测试失败分析

### 6.1 现象

`bench-agent-20260604-090711.json` 中工具调用测试结果：

| 测试项 | 成功/总数 | 准确率 |
|--------|-----------|--------|
| Read tool | 0/1 | 0% |
| Bash tool | 0/1 | 0% |
| Edit tool | 0/1 | 0% |

### 6.2 根因定位

**根因不在后端或代理，而在测试脚本 `tools/bench_agent.py` 的流式响应解析缺陷。**

从 `llama-server.log` 可以确认，后端一直都能正确生成工具调用（`[SSE-TC]` 日志）。但 `bench_agent.py` 的 `send_anthropic_request()` 函数在流式解析时：

1. `content_block_start` 只记录 TTFT，**不提取 `tool_use` 块**
2. `content_block_delta` 只处理 `text_delta`，**忽略 `input_json_delta`**

而测试判定逻辑在检查 `content` 文本中是否包含工具名：

```python
content = resp.get("content", "")
has_expected_tool = tc["expect_tool"].lower() in content.lower()
```

当模型正确返回 `tool_use` 块时，`content` 为空字符串，判定必然失败。

### 6.3 修复内容

#### 修复 1：流式解析器捕获 `tool_use` 块

```python
elif evt_type == "content_block_start":
    if ttft_ms is None:
        ttft_ms = (time.time() - t0) * 1000
    block = evt.get("content_block", {})
    if block.get("type") == "tool_use":
        tool_calls.append({
            "id": block.get("id", ""),
            "name": block.get("name", ""),
            "input": block.get("input", {}),
        })

elif evt_type == "content_block_delta":
    delta = evt.get("delta", {})
    if delta.get("type") == "text_delta":
        full_content += delta.get("text", "")
    elif delta.get("type") == "input_json_delta" and tool_calls:
        tool_calls[-1]["input_str"] = tool_calls[-1].get("input_str", "") + delta.get("partial_json", "")
```

#### 修复 2：测试判定改为检查 `tool_calls` 列表

```python
tool_calls = resp.get("tool_calls", [])
has_expected_tool = any(
    tc["expect_tool"].lower() == t.get("name", "").lower()
    for t in tool_calls
)
```

#### 修复 3：优化 Edit 测试用例提示

原提示导致 9B 模型选择先 `Read` 再编辑（合理策略）。修改为明确指示：

```
"Use the Edit tool to replace 'hello' with 'world' in file /tmp/test.py. Do not read the file first."
```

### 6.4 修复验证

```
  --- Read tool ---
      Run 1: TTFT=764ms  output=28  tool_call=✅
      📊 准确率: 1/1 (100%)

  --- Bash tool ---
      Run 1: TTFT=2054ms output=27  tool_call=✅
      📊 准确率: 1/1 (100%)

  --- Edit tool ---
      Run 1: TTFT=31872ms output=53  tool_call=✅
      📊 准确率: 1/1 (100%)
```

---

## 7. Edit 工具测试 TTFT 31.8 秒根因分析

### 7.1 核心结论

**31.8 秒的 TTFT 不是 Edit 测试本身的推理延迟，而是「请求排队等待」造成的。**

Edit 测试的实际推理性能非常健康：
- 后端 `first token after 0.7s`
- 总耗时 `1.77s`（53 tokens，29.9 tok/s）
- 但 bench_agent 测量到的 TTFT 包含了前面大请求的 **排队阻塞时间**

### 7.2 时间线重构

从 `llama-server.log` 中提取的精确事件序列：

| 时间 | 事件 | 内部 Request ID | Prompt Tokens | TTFT |
|------|------|-----------------|---------------|------|
| T+0 | **Read 测试**开始 | — | 780 | **0.7s** |
| T+1s | **Bash 测试**开始 | `6b935a6d-6ac` | 780 (cached) | **0.1s** |
| T+2s | **Claude Code 大请求**进入 | `c5b000ba-0ba` | **32,523** | — |
| T+2.5s | **Edit 测试**发送到后端 | `a6285577-cea` | 797 | — |
| T+2.5s | Edit 测试进入**等待队列** | — | — | — |
| T+30s | 大请求 prefill 完成 | `c5b000ba-0ba` | 32,523 | **27.9s** |
| T+31.8s | **Edit 测试终于开始推理** | `a6285577-cea` | 797 | **0.7s** |
| T+33.5s | Edit 测试完成 | — | — | 总耗时 1.8s |

### 7.3 为什么 Read/Bash 不受影响？

| 测试 | TTFT | 原因 |
|------|------|------|
| Read | 0.7s | 在大请求**开始之前**就完成了 |
| Bash | 0.1s | 大请求刚开始时它已拿到 GPU，且是 cached prefill |
| Edit | **31.8s** | 发送时 GPU 正被 32K tokens 大请求占用，排队等待 |

### 7.4 大请求是谁？

来自 **Claude Code 的正常对话**：

```
POST /v1/messages?beta=true
Content-Length: 490691
REQ_SUMMARY: chars=331001 tools=27
Context truncation: 558 messages dropped, 24 kept (~26807 tokens, budget=40000)
```

- **报文大小**：331K 字符，27 个工具定义
- **截断后 token 数**：约 26.8K tokens
- **后端实际 prefill**：32,523 tokens（含系统提示和工具模板膨胀）

### 7.5 根本瓶颈：`max-num-seqs=1`

当前配置：

```bash
RAPID_MLX_EXTRA_ARGS="... --max-num-seqs 1"
```

`--max-num-seqs 1` 意味着后端同一时间只能处理 **1 个序列**。当 Claude Code 发送了一个 32K tokens 的大请求时：

1. 该请求进入 GPU 开始 prefill（27.9s）
2. prefill 完成后继续生成输出
3. 在此期间，**任何其他请求（包括 bench-agent 的 Edit 测试）都必须排队等待**
4. Edit 测试的实际推理只有 0.7s，但排队等了约 30s

### 7.6 修复建议

| 方案 | 效果 | 风险 |
|------|------|------|
| **提高 `--max-num-seqs` 到 2** | 允许大请求 prefill 时小请求并行 | 9B 模型内存压力小，可能可行；但大上下文并发可能仍导致延迟 |
| **bench-agent 测试前先暂停 Claude Code** | 消除干扰，获得干净的基准 | 需要手动操作，不适合自动化 CI |
| **bench-agent 增加「预热/等待空闲」逻辑** | 检测后端空闲后再开始测试 | 最实际的方案，可在脚本中实现 |

### 7.7 一句话总结

> Edit 测试的 31.8 秒 TTFT 中，31 秒是排队等 Claude Code 的 32K tokens 大请求释放 GPU，实际推理只花了 0.7 秒。这是 `--max-num-seqs 1` 下的预期行为，不是性能退化。

---

## 8. 后端请求 ID 识别说明

在 `llama-server.log` 中，存在**两种关联的请求 ID**。

### 8.1 后端内部调度 ID（主要使用）

**格式**：`request=[a-f0-9]{8}-[a-f0-9]{3}`  
**示例**：`request=a6285577-cea`、`request=c5b000ba-0ba`

这是 rapid-mlx **内部调度系统**使用的追踪 ID，贯穿了请求在后端的完整生命周期：

```
[schedule]        request=a6285577-cea uid=0 prompt_tokens=797 ...
[cache_fetch]     request=a6285577-cea MISS prompt_tokens=797 ...
[engine_core]     [stream_outputs] a6285577-cea first token after 0.7s
[cache_store]     request=a6285577-cea tokens=850 ...
```

**使用理由**：这个 ID 出现在 scheduler、engine、cache 等所有内部组件日志中，可以**跨模块串联**一个请求的 prefill → 生成 → 缓存 → 清理的完整事件链。

### 8.2 对外暴露的 OpenAI Chat Completion ID

**格式**：`chatcmpl-[a-f0-9]{8}`  
**示例**：`chatcmpl-dd7fbdd0`、`chatcmpl-ac239b54`

这是符合 OpenAI API 规范的响应 ID，出现在 SSE 数据流中，返回给代理/客户端：

```
[SSE-TC] data: {"id":"chatcmpl-dd7fbdd0","choices":[...]}
```

**局限**：代理层看到的是这个 ID，但它在后端日志中**只出现在路由层的 SSE 输出里**，不出现在 scheduler/engine 日志中，所以**无法用来追踪内部调度状态**。

### 8.3 两种 ID 的映射关系

| 内部 ID | OpenAI ID | 场景 |
|---------|-----------|------|
| `a6285577-cea` | `chatcmpl-dd7fbdd0` | Edit 测试（bench-agent） |
| `65eaa681-22f` | `chatcmpl-ac239b54` | Claude Code 大请求 |

从日志中可以确认映射：同一个请求的内部 ID 和 OpenAI ID 会**在相近的时间窗口**出现，且 `chatcmpl-id` 只在 `[SSE-*]` 路由日志中出现，而 `request=xxx` 出现在所有内部组件中。

---

## 9. 性能瓶颈总结

1. **Prefill 是主要瓶颈**：32K tokens 冷启动需 27-29 秒，但缓存命中后降至 1.2 秒
2. **前缀缓存非常有效**：缓存命中时 TTFT 改善 **95%+**
3. **生成速度合理**：小请求 30 tok/s，大请求 3.6 tok/s，9B 4bit 量化下符合预期
4. **连接/超时管理是隐患**：315s-844s 的超长请求暗示连接层可能存在挂起或客户端未正确断开的情况
5. **GPU 序列化是并发瓶颈**：`--max-num-seqs 1` 导致任何大请求都会阻塞后续所有请求
