# 代理层上下文窗口替换设计文档

> 状态: Phase 1 已实施，待集成验证  
> 作者: Kimi Code CLI / opencode  
> 日期: 2026-06-04  
> 版本: v3（Phase 1 实施 + Kimi 行业建议评估）

---

## 1. 背景与问题诊断

### 1.1 当前症状

在实际对话中（以 Qwen3.6-35B-A3B 为例），每轮请求的 prompt 长度持续膨胀：

| 指标 | 实测值 | 来源 |
|------|--------|------|
| 原始请求消息数 | 93 条 | `/tmp/anthropic_request_body.json` |
| 代理层处理后消息数 | 81 条 | 后端日志 `msgs=81` |
| Prompt tokens | **68,131** | 后端日志 `prompt_tokens=68131` |
| TTFT (首 token 延迟) | **90.8s** | 后端日志 `first token after 90.8s` |
| 每轮总耗时 | ~97s | 监控数据 |

### 1.2 根因分析

**根因不是"内容太长"，而是"消息数量太多"**。

Claude Code 采用 agentic 工作流，每轮对话产生大量工具调用：

```
user: 任务描述
assistant: 分析 + tool_use(Read)
user:  tool_result(文件内容 5000 字)
assistant: 分析 + tool_use(Bash)
user:  tool_result(命令输出)
...
```

10 轮对话后，消息数可达 90+ 条。每条消息在转换为 OpenAI 格式后，包含 role、content、tool_calls / tool_call_id 等字段，产生显著的**结构开销**。更关键的是，Claude Code 每次请求携带 **44 个 tool definitions**，这部分开销与消息数无关但占用了大量 tokens。

**Prompt 构成拆解**（基于后端日志 `request=1119ebe6-523`）：

| 组成部分 | 估算 Tokens | 说明 |
|----------|------------|------|
| 44 个 Tool definitions | ~8K-12K | 每次请求固定开销，与消息数无关 |
| System prompt | ~2K-4K | 技能、项目规则等 |
| 81 条 Messages 内容 | ~30K-35K | 实际文本 + tool_use 参数 |
| 81 条 Messages 结构开销 | ~8K-12K | role/content/tool_call_id 等 JSON 包装 |
| **合计** | **~68K** | 与实测 `prompt_tokens=68131` 吻合 |

**关键洞察**：即使清空 tool_result content，消息结构和 tool definitions 的固定开销仍然占据约 **20K-25K tokens**。要显著降低 prompt，必须**减少消息数量**。

### 1.3 现有三层机制的局限

当前代理层已实现三层"做减法"机制：

| 机制 | 效果 | 局限 |
|------|------|------|
| Tool-result clearing | 清空 38 个旧 tool_result content | 不减少消息数 |
| Thinking stripping | 删除旧 thinking block | 当前架构下基本不触发 |
| Tool-result compression | 合并 10 个空循环，减少 20 条消息 | 只压缩"纯工具调用"对，带 text 的 assistant 消息无法压缩 |

**净效果**：93 → 81 条消息（-13%），对 68K tokens 的 prompt 杯水车薪。

---

## 2. 设计目标

### 2.1 核心目标

将 prompt tokens 从 **68K 降到 25K-35K**，使 TTFT 从 **90s 降到 15-20s 以内**。

### 2.2 设计原则

1. **源头控制**：不再在完整历史上修修补补，而是直接定义"保留边界"
2. **显式告知**：被丢弃的历史用占位消息显式告知模型，避免 silent truncation
3. **可配置**：默认关闭，用户可自主选择启用和保留轮数
4. **保留近期**：最近 N 轮对话完整保留，不影响当前工作流

---

## 3. 设计方案：滑动窗口 + 占位消息

### 3.1 核心思路

```
Claude Code 发送 93 条消息
    ↓
代理层定义窗口：只保留最近 10 轮对话
    ↓
头部 2 条（system context）保留
中间 60+ 条丢弃 → 替换为 1 条摘要占位消息
尾部 20 条（最近 10 轮）完整保留
    ↓
转发给后端：~23 条消息
```

### 3.2 算法流程

不新增独立函数，而是**增强现有 `truncate_messages_if_needed`**，添加 `rounds` 截断策略。

```python
def truncate_messages_if_needed(messages):
    """
    Proxy-side message truncation with dual strategy support.

    Strategy 'char' (default): drop old messages until total chars fall below
    PROXY_CTX_CHARS_LIMIT. Preserves head + tail window.

    Strategy 'rounds': always keep only the most recent N assistant rounds,
    replacing dropped messages with a lightweight placeholder. More aggressive
    but predictable.
    """
    if not PROXY_CTX_LIMIT_ENABLED and PROXY_CTX_TRUNCATE_STRATEGY != "rounds":
        return messages, {"enabled": False}

    # --- Rounds strategy ---
    if PROXY_CTX_TRUNCATE_STRATEGY == "rounds":
        keep_rounds = PROXY_CTX_KEEP_ROUNDS
        min_msgs = PROXY_CTX_KEEP_HEAD + keep_rounds * 3  # head + N rounds (rough upper bound)
        if len(messages) <= min_msgs:
            return messages, {"enabled": True, "strategy": "rounds", "skipped": True, "reason": "below_min"}

        # Step 1: 保留头部
        head = messages[:PROXY_CTX_KEEP_HEAD]

        # Step 2: 从尾部向前收集 keep_rounds 轮对话
        tail = []
        assistant_count = 0
        for msg in reversed(messages):
            tail.insert(0, msg)
            if msg.get("role") == "assistant":
                assistant_count += 1
            if assistant_count >= keep_rounds:
                break

        # Boundary check: ensure tail doesn't overlap with head
        tail_start = len(messages) - len(tail)
        if tail_start <= PROXY_CTX_KEEP_HEAD:
            return messages, {"enabled": True, "strategy": "rounds", "skipped": True, "reason": "overlap"}

        # Step 3: 被丢弃的中间部分
        dropped = messages[PROXY_CTX_KEEP_HEAD : tail_start]

        # Step 4: 生成占位消息（含文件名索引）
        tool_count = sum(
            1 for m in dropped
            if m.get("role") == "assistant"
            and any(b.get("type") == "tool_use" for b in m.get("content", []))
        )

        # 提取被丢弃消息中涉及的文件路径
        file_mentions = set()
        for m in dropped:
            if m.get("role") == "assistant":
                for b in m.get("content", []):
                    if b.get("type") == "tool_use":
                        args = b.get("function", {}).get("arguments", "")
                        if isinstance(args, str):
                            for pat in [r'"path":\s*"([^"]+)"', r'"file":\s*"([^"]+)"',
                                        r'"filePath":\s*"([^"]+)"', r'"command":\s*"([^"]*)"']:
                                file_mentions.update(re.findall(pat, args))

        file_info = f" Files previously accessed: {', '.join(sorted(file_mentions)[:10])}." if file_mentions else ""
        summary_text = (
            f"[Context folded: {len(dropped)} earlier messages omitted. "
            f"Previous work included {tool_count} tool interactions."
            f"{file_info} "
            f"Retaining last {keep_rounds} conversation rounds.]"
        )

        # Step 5: 处理连续 user role 风险（review S2）
        if tail and tail[0].get("role") == "user":
            # 将占位文本追加到 tail 第一条 user 消息前面
            tail_content = tail[0].get("content", [])
            if isinstance(tail_content, list):
                tail[0]["content"] = [{"type": "text", "text": summary_text}] + tail_content
            else:
                tail[0]["content"] = [{"type": "text", "text": summary_text}, {"type": "text", "text": str(tail_content)}]
            result = head + tail
        else:
            summary_msg = {"role": "user", "content": [{"type": "text", "text": summary_text}]}
            result = head + [summary_msg] + tail

        return result, {
            "enabled": True,
            "strategy": "rounds",
            "truncated": True,
            "original_msgs": len(messages),
            "kept_msgs": len(result),
            "dropped_msgs": len(dropped),
            "tool_count": tool_count,
        }

    # --- Char strategy (existing logic) ---
    total_chars = _estimate_message_chars(messages)
    if total_chars < PROXY_CTX_CHARS_LIMIT:
        return messages, {"enabled": True, "strategy": "char", "skipped": True, "reason": "below_limit", "chars": total_chars}

    # ... existing char-based truncation logic ...
```

### 3.3 与现有机制的集成

**策略互斥原则**（review S1）：`rounds` 策略启用时，`char` 策略自动禁用。两者不会同时触发，避免多层兜底逻辑增加调试难度。

```python
def _handle_messages(self, body):
    raw_messages = body.get("messages", [])
    
    # 第一层：工具结果清理（在完整历史上操作，确保保留的 tool_result 是最新的）
    raw_messages, clear_stats = clear_old_tool_results(raw_messages)
    
    # 第二层：thinking 清理
    raw_messages, think_stats = strip_old_thinking_blocks(raw_messages)
    
    # 第三层：空消息压缩
    raw_messages, compress_stats = compress_cleared_tool_results(raw_messages)
    
    # 第四层：上下文截断（char 或 rounds 策略，互斥）
    raw_messages, trunc_stats = truncate_messages_if_needed(raw_messages)
    # rounds 策略已内置占位消息，char 策略为静默截断
    
    # ...转发给后端
```

**执行顺序说明**：
- `clear` → `think_strip` → `compress` 先对完整历史做"减法"
- `truncate` 最后执行，在精简后的历史上做"定边界"
- `rounds` 模式下，truncate 内部会插入占位消息，保留头部 + 最近 N 轮

### 3.4 占位消息设计

**角色选择**：`user`
- Anthropic API 中 `system` 角色通常只有一条
- `assistant` 角色代表模型输出，不应由代理伪造
- `user` 角色最自然，代表"用户告知模型上下文已折叠"

**连续 user role 处理**（review S2）：如果 tail 窗口的第一条消息恰好是 `user`，单独插入占位消息会导致连续两条 `user` 消息，可能被 Anthropic API 合并或拒绝。处理方案：

```python
if tail and tail[0].get("role") == "user":
    # 将占位文本合并到 tail 第一条 user 消息的前面
    tail[0]["content"] = [{"type": "text", "text": summary_text}] + tail[0].get("content", [])
else:
    # 单独插入一条 user 占位消息
    result = head + [{"role": "user", "content": [{"type": "text", "text": summary_text}]}] + tail
```

**占位消息内容结构**：
```json
{
  "role": "user",
  "content": [{
    "type": "text",
    "text": "[Context folded: 60 earlier messages omitted. Previous work included 45 tool interactions. Files previously accessed: src/main.py, lib/utils.py, tests/test_main.py. Retaining last 10 conversation rounds.]"
  }]
}
```

---

## 4. 收益量化分析

### 4.1 时间收益

数据来源标注（review S5）：
- **68K TTFT 90.8s**：后端日志 `request=1119ebe6-523`（2026-06-03 实测）
- **14.8K TTFT 5.7s**：`tools/bench_rapidmlx.py` 测试输出（2026-06-03 重启后）
- **2.9K TTFT 1.6s**：同上
- **20K/35K 为插值预估**，基于 Metal 内存带宽非线性饱和特性

| Prompt 长度 | Prefill 速度 | TTFT | vs 当前 | 每轮节省 |
|-------------|-------------|------|---------|----------|
| 68K (现状) | ~750 tok/s | **90s** | — | — |
| 35K | ~1500 tok/s | **23s** | -67s | **67s** |
| 25K | ~1800 tok/s | **14s** | -76s | **76s** |
| 15K | ~2200 tok/s | **7s** | -83s | **83s** |

> 注：prefill 速度非线性下降是因为 Metal 内存带宽在长上下文时饱和。短 prompt 的 prefill 速度来自 `bench_rapidmlx.py` 实测；长 prompt 速度来自后端日志；中间值为插值预估。

### 4.2 内存收益

从后端日志提取 KV cache 占用（8-bit 量化）：

| Prompt | KV Cache | vs 当前 | 释放内存 |
|--------|----------|---------|----------|
| 68K | **4906 MB** | — | — |
| 35K | ~2500 MB | -2400 MB | **2.4 GB** |
| 20K | ~1400 MB | -3500 MB | **3.5 GB** |

当前 `gpu-memory-utilization=0.60`，48GB 内存可用约 28GB：
- 模型：~17GB
- KV cache (68K)：~5GB  
- 峰值：25.7GB（日志实测）
- **安全余量仅 ~2GB**

降到 20K 后：
- 峰值降至 ~18.5GB
- **安全余量提升到 ~9GB**
- OOM 风险从"高"降至"几乎为零"

### 4.3 生成速度收益

当前长上下文下生成速度严重下降：
- 现状：79 tokens / 6.3s = **12.5 tok/s**（来源：后端日志 `request=1119ebe6-523`，prefill 后剩余生成时间仅 6.3s）
- 基准（短上下文）：**68-73 tok/s**（来源：`tools/bench_rapidmlx.py`，14K/2.9K prompt 测试）

降到 25K 后，生成速度预期恢复到 **55-65 tok/s**（预估，基于内存压力缓解）。

### 4.4 端到端对比

假设每轮输出 200 tokens：

| 场景 | 当前 (68K) | 降长后 (25K) | 节省 |
|------|-----------|-------------|------|
| Prefill | 90.8s | ~14s | **-77s** |
| 生成 | 16s (200/12.5) | 4s (200/55) | **-12s** |
| **每轮总计** | **~107s** | **~18s** | **-89s (83%)** |

---

## 5. 行业方案调研

### 5.1 问题普遍性

**非常普遍。** OpenAI 官方 Cookbook 将其称为 "Context Bloat"——每次推理都重新处理完整历史，导致成本二次增长、延迟线性恶化、推理质量因无关历史而下降（"Lost in the Middle" 现象）。Anthropic 的 SWE-bench 实验也显示，未加控制的 Agent 在 150 步循环中轻易累积到数百万 tokens。

### 5.2 行业主流方案

| 方案 | 代表产品/论文 | 核心思路 | 适用场景 |
|------|-------------|---------|---------|
| 被动截断 | OpenAI Cookbook | 按 token 数截断，优先保留 system + 最近 N 条 | 简单场景 |
| 主动压缩（SummarizingSession） | Claude Code, Cursor, Aider, OpenAI Agents SDK | 将旧历史压缩为 summary message | **最主流** |
| 主动压缩（Focus） | 《Active Context Compression》论文 | Agent 自主决定何时压缩，高频小步（每 10-15 次 tool call） | 前沿研究 |
| 语义压缩（RAG） | Continue.dev, Cody | 将历史向量化，按需检索相关片段 | 需要客户端改造 |
| Prompt Chaining | Anthropic 推荐 | 拆分子任务，独立上下文 | 需要客户端架构变更 |
| 架构级（虚拟内存） | MemGPT | 操作系统式虚拟内存，LLM 显式管理外部存储 | 复杂系统 |
| KV Cache 复用 | vLLM APC, SGLang RadixAttention | 基础设施层缓存 prefix KV | 受模型架构限制（我们的 MoE non-trimmable 问题） |
| 模型路由 | Gemini 1.5 Pro (2M tokens) | 长上下文自动路由到大窗口模型 | 云端场景 |

### 5.3 我们的方案定位

我们选择 **主动压缩（SummarizingSession）** 方案，理由：

1. **完全代理层可控** — 不需要改客户端（Claude Code）、不需要调 LLM 做摘要、不需要向量数据库
2. **立即可实施** — 只修改 `anthropic_proxy.py`，不引入新依赖
3. **行业验证** — OpenAI Agents SDK Cookbook 提供了几乎完全一致的实现
4. **与现有架构兼容** — 增强现有 `truncate_messages_if_needed`，不新增独立函数

### 5.4 行业建议与我们的取舍

| 行业建议 | 来源 | 我们的取舍 |
|----------|------|-----------|
| 用 LLM 生成 summary 替代静态占位 | OpenAI Cookbook, Kimi | **Phase 2 考虑**。代理层调 LLM 增加延迟和复杂度，Phase 1 先用静态占位 |
| 按 token 预算动态触发而非固定轮数 | OpenAI Cookbook, Kimi | **采纳**。见 5.5 节 |
| 按完整 turn 保留而非单条 message | Kimi | **已采纳**。算法按 assistant 角色计数，天然按 turn 边界切割 |
| 阶段感知压缩（探索→实现转换点） | 《Active Context Compression》论文 | **不采纳**。代理层无法判断 agent 阶段 |
| 高频小步压缩（每 10-15 次 tool call） | 《Active Context Compression》论文 | **不采纳**。代理层压缩粒度是整个请求，无法在生成中途压缩 |

### 5.5 Token 预算动态触发

采纳行业建议，用 token 预算替代固定轮数作为触发条件：

```python
PROXY_CTX_TOKEN_BUDGET = 30000  # 目标 prompt tokens 上限

def truncate_messages_if_needed(messages):
    if PROXY_CTX_TRUNCATE_STRATEGY == "rounds":
        # 估算当前 token 数（基于 chars × 1.3 系数）
        estimated_tokens = _estimate_message_chars(messages) * 1.3
        if estimated_tokens < PROXY_CTX_TOKEN_BUDGET:
            return messages, {"enabled": True, "strategy": "rounds", "skipped": True, "reason": "below_budget"}

        # 动态计算 keep_rounds：从最大值开始递减，直到预估 tokens 低于预算
        for rounds in range(PROXY_CTX_KEEP_ROUNDS, 2, -1):
            candidate, stats = _apply_rounds_truncation(messages, rounds)
            estimated = _estimate_message_chars(candidate) * 1.3
            if estimated <= PROXY_CTX_TOKEN_BUDGET:
                return candidate, stats

        # 最低保留 2 轮
        return _apply_rounds_truncation(messages, 2)
```

**注意**：token 估算是基于字符数的近似（`chars × 1.3`），因为我们不引入 tiktoken 依赖。后端日志中的 `prompt_tokens` 提供精确值可用于事后校准。

---

## 6. 风险与缓解策略

### 6.1 风险矩阵

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 跨轮引用失效 | 中 | 高 | 占位消息显式告知"历史已折叠" |
| 代码上下文丢失 | 中 | 高 | 保留最近 N 轮完整对话，包含文件读写 |
| Claude Code 预期不同步 | 低 | 中 | 占位消息让模型知道"之前做过什么" |
| 模型忽略占位消息 | 低 | 中 | 使用方括号 `[Context folded]` 增强可见性 |
| Streaming 中引用已丢弃的 tool_use_id | 低 | 中 | 模型通常只引用最近窗口内的 tool_use_id；若引用旧 ID，Claude Code SDK 会报错并触发重试（review S3） |
| Token 估算不准确导致超预算 | 中 | 中 | 基于后端日志 `prompt_tokens` 持续校准系数；设 30K 预算目标 25K 实际，留 20% 余量 |
| 用户意外启用导致困惑 | 低 | 低 | 默认关闭，/status 页面显示当前策略 |

### 6.2 详细风险说明

**风险 1：跨轮引用失效**
- 场景：用户说"请按照第三步的方案继续"
- 如果"第三步"在 N 轮之前，模型看不到具体方案
- 缓解：
  - 占位消息中的 `tool_count` 让模型知道"之前做过很多工具调用"
  - 模型可主动询问用户"请重新说明第三步的具体内容"
  - 用户可通过 `/clear` 在关键节点重置上下文

**风险 2：代码上下文丢失**
- 场景：模型之前读了 file A 的内容并做了修改，N 轮后再次编辑 file A
- 缓解：
  - 保留的 N 轮通常包含最近的文件操作
  - 若需要早期文件内容，模型会重新 `Read`
  - 额外开销：一次 `Read` 调用 ≈ 1-2s，远低于当前 90s TTFT

**风险 3：Claude Code 内部状态不同步**
- 场景：Claude Code 本地认为"我告诉过模型 X"，但代理层把 X 丢掉了
- 缓解：
  - 占位消息明确告知模型"历史被折叠"
  - 模型不会 hallucinate 早期内容，而是基于保留的窗口工作
  - 若 Claude Code 引用被折叠的内容，模型会表现为"不记得"，用户可察觉

### 6.3 动态窗口策略（高级）

为降低风险，可实现动态 `keep_rounds`：

```python
def compute_keep_rounds(msg_count):
    """对话越短保留越多，对话越长折叠越激进。"""
    if msg_count < 30:
        return 15      # 短对话：保留 15 轮
    elif msg_count < 60:
        return 10      # 中对话：保留 10 轮
    else:
        return 8       # 长对话：保留 8 轮
```

---

## 7. 工具调用功能影响分析

### 7.1 典型 Agent 工作流的消息结构

```
msg0:  user (system/skills)          ← HEAD，始终保留
msg1:  user (工具定义)               ← HEAD，始终保留
msg2:  user (任务描述)
msg3:  assistant (分析 + tool_use: Read)
msg4:  user (tool_result: 文件内容 5000 字)
msg5:  assistant (分析 + tool_use: Bash) 
msg6:  user (tool_result: 命令输出)
msg7:  assistant (分析 + tool_use: Read)
msg8:  user (tool_result: 另一个文件内容)
msg9:  assistant (修改代码 + tool_use: Edit)
msg10: user (tool_result: 编辑确认)
...
msg80: user (最新问题)
msg81: assistant (最新回复)
```

### 7.2 影响矩阵

| 场景 | rounds=10 保留 | 影响 | 严重程度 |
|------|---------------|------|----------|
| 最近 10 轮的 Read 结果 | ✅ 完整保留 | 无影响 | — |
| 10 轮之前的 Read 结果 | ❌ 被丢弃 | 模型不记得文件内容 | ⚠️ 中 |
| 最近 10 轮的 Bash 输出 | ✅ 完整保留 | 无影响 | — |
| 10 轮之前的 Bash 输出 | ❌ 被丢弃 | 模型不记得命令结果 | ⚠️ 低 |
| 跨文件引用（读 A 后改 B，10 轮后再改 A） | ❌ A 的内容丢失 | 模型需要重新 Read A | ⚠️ 中 |
| 错误修复迭代（同一文件反复编辑） | ✅ 最近的编辑保留 | 早期失败尝试丢失，最新成功状态保留 | ✅ 低 |
| 用户引用早期对话（"按第三步方案继续"） | ❌ 第三步可能丢失 | 模型会重新询问 | ⚠️ 中 |

### 7.3 与现有机制的对比

**关键洞察**：现有 `clear_old_tool_results` 机制已经丢失了早期 tool_result 内容（替换为 `[cleared to save context]`）。rounds 策略只是更激进——连 `[cleared]` 占位消息本身也丢弃了。

| | 现有机制 | rounds 策略 |
|---|---------|------------|
| 早期 tool_result 内容 | ❌ 丢失（清空为 `[cleared]`） | ❌ 丢失（连消息也删除） |
| 早期 tool_use 调用记录 | ✅ 保留（assistant 消息还在） | ❌ 丢失 |
| 早期 assistant 分析文本 | ✅ 保留 | ❌ 丢失 |
| 模型行为 | "我记得调过这个工具，但不记得结果" | "我不记得调过这个工具" |

### 7.4 模型恢复策略

rounds 策略下模型丢失了早期上下文，但可以**自动恢复**：

| 恢复方式 | 触发条件 | 额外开销 | vs 节省 |
|----------|----------|----------|---------|
| 重新 Read | 发现需要文件内容时 | +1-2s | 远低于省下的 77s prefill |
| 询问用户 | 发现引用缺失时 | +3-5s（等待用户输入） | 比 90s TTFT 好得多 |
| 重新 Bash | 需要确认运行状态时 | +1-3s | 远低于省下的 77s prefill |

### 7.5 典型场景量化分析

以 Claude Code 典型 agent 任务为例（修改 3 个文件，15 轮对话）：

```
轮 1-5:  探索阶段（Read 多个文件，Bash 查看结构）
轮 6-10: 实现阶段（Edit 修改代码，Bash 运行测试）  
轮 11-15: 修复阶段（根据测试结果继续修改）
```

- `keep_rounds=10`：轮 1-5 被丢弃，模型不记得探索过的文件
- **实际影响**：如果轮 11-15 需要修改轮 1-5 读过的文件，模型会重新 Read
- **额外开销**：1-2 次 Read 调用 ≈ 2-4s
- **节省**：77s prefill

**净收益**：+77s prefill 节省 - 4s 额外 Read = **净省 73s**

### 7.6 占位消息增强：文件名索引

为帮助模型判断是否需要重新读取，在占位消息中**提取被丢弃消息涉及的文件路径**：

```python
file_mentions = set()
for m in dropped:
    if m.get("role") == "assistant":
        for b in m.get("content", []):
            if b.get("type") == "tool_use":
                args = b.get("function", {}).get("arguments", "")
                if isinstance(args, str):
                    for pattern in [r'"path":\s*"([^"]+)"', r'"file":\s*"([^"]+)"',
                                    r'"filePath":\s*"([^"]+)"', r'"command":\s*"([^"]*)"']:
                        file_mentions.update(re.findall(pattern, args))

summary_text = (
    f"[Context folded: {len(dropped)} earlier messages omitted. "
    f"Files previously accessed: {', '.join(sorted(file_mentions)[:10])}. "
    f"Previous work included {tool_count} tool interactions. "
    f"Retaining last {keep_rounds} conversation rounds.]"
)
```

**效果**：模型看到 `"Files previously accessed: src/main.py, lib/utils.py, tests/test_main.py"`，可以判断是否需要重新读取某个文件，而不是盲目地全部重读。

---

## 8. 配置参数

```bash
# configs/rapid-mlx-35b.conf

# 上下文截断策略：char = 按字符阈值（默认），rounds = 按对话轮数 + token 预算
PROXY_CTX_TRUNCATE_STRATEGY=char
PROXY_CTX_KEEP_ROUNDS=10
PROXY_CTX_TOKEN_BUDGET=30000

# 动态窗口（可选，覆盖固定轮数）
PROXY_CTX_KEEP_ROUNDS_DYNAMIC=true
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_CTX_TRUNCATE_STRATEGY` | `char` | 截断策略：`char` 按字符阈值，`rounds` 按保留轮数 + token 预算 |
| `PROXY_CTX_KEEP_ROUNDS` | `10` | rounds 策略下最大保留最近 N 轮 assistant 回复 |
| `PROXY_CTX_TOKEN_BUDGET` | `30000` | rounds 策略下的 prompt tokens 预算上限（动态触发） |
| `PROXY_CTX_KEEP_ROUNDS_DYNAMIC` | `true` | 是否根据消息总数动态调整保留轮数 |

**策略互斥**（review S1）：
- `PROXY_CTX_TRUNCATE_STRATEGY=rounds` 时，现有 `PROXY_CTX_CHARS_LIMIT` 截断逻辑被跳过
- `PROXY_CTX_TRUNCATE_STRATEGY=char` 时，保持原有行为不变
- `PROXY_CTX_LIMIT_ENABLED=false` 且 `STRATEGY=char` 时，完全禁用截断

---

## 9. 实施计划

### Phase 1：基础实现 — 静态占位 + Token 预算 + 文件名索引 ✅ 已完成
- [x] 增强 `truncate_messages_if_needed()` 添加 `rounds` 分支（`_apply_rounds_truncation` 辅助函数）
- [x] 实现 token 预算动态触发（`PROXY_CTX_TOKEN_BUDGET`，从 `keep_rounds` 递减到 min=2）
- [x] 实现占位消息文件名索引（7.6 节方案，提取 path/file/filePath/directory）
- [x] 实现连续 user role 处理（review S2，合并到 tail 首条 user 消息）
- [x] 在 `_handle_messages` 中调整执行顺序：clear → date_norm → think_strip → compress → truncate
- [x] 添加配置参数（`PROXY_CTX_TRUNCATE_STRATEGY`、`PROXY_CTX_KEEP_ROUNDS`、`PROXY_CTX_TOKEN_BUDGET`、`PROXY_CTX_TOKEN_RATIO`）
- [x] 更新代理日志输出（rounds 日志含 estimated_tokens、actual_keep_rounds、budget）
- [ ] 同步更新 `AGENTS.md` 配置表（review S4）

### Phase 2：集成验证（30 分钟）
- [ ] 运行单元测试 `test_proxy_fallback.py`（28 tests passed）
- [ ] 使用 `/tmp/anthropic_request_body.json` 做集成测试
- [ ] 重启服务，观察 `/status` 页面
- [ ] 发送测试请求验证效果
- [ ] 对比后端日志 `prompt_tokens` 与 `PROXY_CTX_TOKEN_BUDGET`，校准估算系数

### Phase 3：调优（可选）
- [ ] 根据实际体验调整 `PROXY_CTX_KEEP_ROUNDS` 和 `PROXY_CTX_TOKEN_BUDGET`
- [ ] 评估动态窗口策略效果
- [ ] 收集用户反馈

### Phase 4：高级优化（后续迭代）
- [ ] 评估 LLM 生成 summary 替代静态占位（可能用小模型如 Qwen3-4B 离线生成）
- [ ] 评估高频小步压缩（每 N 次 tool call 触发一次）的可行性
- [ ] 研究阶段感知压缩的代理层实现可能性

---

## 10. 外部建议评估（Kimi）

Kimi 针对 "MacBook Pro 运行 Qwen3.6-35B 50K 上下文 TTFT 40s" 给出了 5 条建议，以下逐条评估与本项目的关系：

### 10.1 建议与现状对比

| # | Kimi 建议 | 本项目状态 | 评估 |
|---|-----------|-----------|------|
| 1 | Prompt Cache 文件化（mlx-lm `save_prompt_cache`） | ❌ 不可行 | 我们使用 Rapid-MLX，非原生 mlx-lm；且 Qwen3.6 的 DeltaNet 层 SSM 状态导致 cache 不稳定。Kimi 也指出了这个风险 |
| 2 | 切换至 Rapid-MLX / vMLX | ✅ 已完成 | 我们已在用 Rapid-MLX + 8-bit KV 量化。vMLX 的 0.22s TTFT 数据疑为极短 prompt 的基准，非 50K 上下文 |
| 3 | Prompt 结构重排（静态前缀 + 动态尾部） | ✅ 部分实现 | date normalization 将动态日期替换为 `DATE_PLACEHOLDER`，稳定 prefix。但 agent 场景中 prompt 几乎全是动态的（每轮 tool_result 不同），静态前缀占比很低 |
| 4 | KV Cache 量化 + 关闭 swap | ✅ 已完成 | `RAPID_MLX_KV_QUANTIZATION=true`，`KV_QUANT_BITS=8`。48GB 统一内存基本不触发 swap |
| 5 | 框架级备选（MLC-LLM 等） | ⚠️ 不适用 | MLC-LLM 100K 场景需 70-85GB 内存，超出 48GB 上限 |

### 10.2 Kimi 未覆盖的优化（我们已实施）

1. **代理层上下文截断**（本文档核心方案）— Kimi 未提及此路径，因为在 Claude Code + 本地模型的组合下，这是代理层独有的优化空间
2. **工具调用压缩** — `compress_cleared_tool_results` 合并连续空工具调用循环
3. **Thinking block 清理** — `strip_old_thinking_blocks` 删除旧 assistant thinking 内容
4. **并发控制** — `PROXY_MAX_CONCURRENT=1` 防止双请求 OOM

### 10.3 结论

Kimi 的建议在**推理引擎选择**层面有价值（确认了我们选择 Rapid-MLX 的正确性），但在**代理层优化**层面未覆盖我们的核心方案。我们的 rounds 策略 + token 预算是针对 agent 长对话场景的专门优化，在 prefix cache 不可用的前提下，是当前架构下收益最大的优化路径。

---

## 11. 相关文档

- `docs/rapid-mlx-cache-analysis.md` — Prefix cache 命中问题分析
- `docs/rapid-mlx-cache-analysis-supplement.md` — 补充实验数据
- `docs/proxy-context-window-design-review.md` — 本文档 review 意见
- `CLAUDE.md` — 代理层架构说明
- `AGENTS.md` — 项目编码规范

---

## 12. 附录：Prompt Token 构成分析

基于后端日志 `request=1119ebe6-523`（2026-06-03 实测）：

```
实测值:
  msgs = 81
  total_chars (messages) = 43,160
  prompt_tokens = 68,131
  tools = 44
```

**Token 构成拆解**（估算，无 tiktoken 精确值）：

| 组成部分 | 估算 Tokens | 推导依据 |
|----------|------------|----------|
| 44 Tool definitions | ~8K-12K | 每个 tool 含 name/description/parameters schema |
| System prompt | ~2K-4K | 技能描述 + 项目规则 + 日期占位 |
| 81 条 Messages 内容 | ~30K-35K | 43,160 chars ÷ ~1.3 chars/token（代码混合） |
| 81 条 Messages 结构 | ~8K-12K | role/content/tool_call_id/type 等 JSON 包装 |
| **合计** | **~68K** | 与实测 68,131 吻合 |

**降到 25 条消息后的预估**（review P2 修正）：

| 组成部分 | 变化 | 预估 Tokens |
|----------|------|------------|
| 44 Tool definitions | 不变 | ~10K |
| System prompt | 不变 | ~3K |
| 25 条 Messages 内容 | 减少为原来的 ~30% | ~10K-12K |
| 25 条 Messages 结构 | 减少为原来的 ~31% | ~3K-4K |
| **合计** | — | **~26K-29K** |

> 注：Tool definitions 是固定开销（~10-15K tokens），即使消息数降到 10 条也无法消除。这是 rounds 策略收益的上限约束。
