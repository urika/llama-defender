# 代理层上下文窗口替换设计文档

> 状态: Phase 1-3 已实施 + Phase 0 模块拆分完成  
> 作者: Kimi Code CLI / opencode  
> 日期: 2026-06-21  
> 版本: v9（Phase 0 模块拆分：proxy_state.py 作为配置/共享状态单一事实源，dual-setattr reload）

---

## 0. 模块架构 (Phase 0 重构)

本设计文档描述的上下文管理逻辑全部实现在 `anthropic_proxy.py` 中，但配置常量、共享状态和热重载基础设施已提取到独立模块，确保单一事实源和可维护性。

### 0.1 模块依赖关系

```
proxy_state.py (518 行)              # 单一事实源，无外部依赖
  ├── 所有 PROXY_*/LLAMA_* 配置常量（本文档涉及的所有阈值）
  ├── 共享可变状态 (_SESSION_REQUEST_COUNT, _DEDUP_CACHE, _LATENCY_WINDOW, ...)
  ├── Thread-local 上下文 (_log_ctx, _metrics_ctx)
  ├── _RELOAD_SPEC (热重载标量列表)
  ├── _parse_conf_env() / _cast_config_value()  (配置解析辅助)
  └── __all__ 白名单 (from proxy_state import *)
        ▲
        │ from proxy_state import *
        │ import proxy_state  (用于 _reload_config 的 dual-setattr)
        │
anthropic_proxy.py (5529 行)         # 8 层管线 + Handler
  ├── _reload_config()               # SIGHUP dual-setattr (proxy_state + self)
  ├── 上下文管理函数:
  │     _classify_lifecycle_stage()  → 阶段判定 (§0.2)
  │     _compress_content_pass()     → L2 内容压缩
  │     truncate_messages_if_needed()→ L5 截断入口
  │     _apply_rounds_truncation()   → Rounds 策略
  │     _compress_middle_with_llm()  → LLM 压缩
  │     _incremental_compress()      → 增量缓存
  │     clear_old_tool_results()     → 语义清除 (legacy)
  │     strip_old_thinking_blocks()  → Thinking 剥离
  ├── 8 层管线其余函数
  ├── class Handler(BaseHTTPRequestHandler)
  └── main()
        ▲
        │ from proxy_state import (_state_lock, _SESSION_*, ...)
        │
proxy_config.py (659 行)             # CONFIG_REGISTRY
  ├── 配置元数据 + 校验
  └── validate() / get_config_summary()
```

### 0.2 配置热重载 (SIGHUP dual-setattr)

`_reload_config()` 在收到 SIGHUP 信号时触发（`./manage.sh reload`），同时更新两个模块的属性：

```python
# anthropic_proxy.py:35
def _reload_config(signum=None, frame=None):
    with _RELOAD_LOCK:
        self_mod = sys.modules[__name__]
        env = _parse_conf_env(RELOAD_CONFIG_PATH)
        # ... parse env ...
        for env_key, py_name, cast, cloud_def, local_def in _RELOAD_SPEC:
            val = _cast_config_value(env.get(env_key, default), cast)
            setattr(proxy_state, py_name, val)   # ← 更新 proxy_state
            setattr(self_mod, py_name, val)       # ← 更新 anthropic_proxy
```

**为什么需要 dual-setattr**:
- `proxy_config.py` 等 sub-module 通过 `proxy_state.PROXY_*` 读取配置 → 需要更新 `proxy_state`
- `anthropic_proxy.py` 内的 local function 引用 module-level name（通过 `from proxy_state import *` 导入）→ 需要更新 `anthropic_proxy` 自身

**测试守护**: `test/unit/test_proxy_reload.py` 的 `TestProxyStateSync` 类验证每次 reload 后 `proxy_state.PROXY_*` 与 `anthropic_proxy.PROXY_*` 保持一致。

### 0.3 生命周期阶段阈值 (本文档核心)

所有阈值定义在 `proxy_state.py`，可通过环境变量或 `configs/active.conf` 覆盖：

| 阶段 | 环境变量 | local 默认 | cloud 默认 | 触发动作 |
|------|----------|-----------|-----------|---------|
| INIT | `PROXY_CLEAR_THRESHOLD` | 15000 | 30000 | 无压缩 |
| GROWTH | `PROXY_CHARS_GROWTH` | 40000 | 80000 | tail-40% 清除 |
| EXPANSION | `PROXY_CHARS_EXPANSION` | 90000 | 200000 | tail-60% 清除 + think strip |
| SATURATION | `PROXY_CHARS_SATURATION` | 180000 | 500000 | full-dynamic clear + merge + trunc |
| OOM_DANGER | `PROXY_CHARS_OOM_DANGER` | 350000 | 1000000 | no frozen + hard truncation |
| PRE_TRUNC | `PROXY_OOM_SAFE_CHARS` | 200000 | 200000 | keep_rounds=2 强制截断 |

> 注：阈值严格单调递增。`_classify_lifecycle_stage()` (anthropic_proxy.py:1427) 根据当前消息总字符数判定阶段，返回 stage_config 驱动 L2-L5 的压缩/截断强度。

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

> **当前生产配置**: `PROXY_CTX_TRUNCATE_STRATEGY=fifo` (DEF-102)。`rounds` 策略已完整实现，
> 但因 turn boundary 不稳定导致 prefix cache 命中率下降，暂时未启用。下文以 `rounds` 为
> 主进行设计说明，`fifo` 策略见 § 3.4。

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

        # Step 4: 生成固定占位消息（Prefix Cache 友好）
        summary_text = "[Context folded: earlier messages omitted. Retaining last N conversation rounds.]"

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

### 3.4 占位消息设计（稳定版，Prefix Cache 友好）

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

**占位消息内容**（固定文本，确保 Prefix Cache 命中）：

```json
{
  "role": "user",
  "content": [{
    "type": "text",
    "text": "[Context folded: earlier messages omitted. Retaining last N conversation rounds.]"
  }]
}
```

**为什么使用固定文本而非动态内容**：

v0.6.71 修复了 MoE non-trimmable 问题后，prefix cache 可以正常工作。此时占位消息成为 prompt 的组成部分：

```
system (4K tokens) + tools (12K tokens) + head (2 msgs) + 占位消息 (固定) = ~16K+ 稳定前缀
```

如果占位消息每轮变化（包含 dropped_count、tool_count、file_mentions 等动态信息），prefix cache 会在占位消息处断裂，导致约 16K tokens 的缓存无法复用。使用固定文本后：

| 占位文本 | Prefix Cache 命中 | 效果 |
|----------|-------------------|------|
| 动态（含 dropped_count 等） | ❌ 每轮断裂 | 16K tokens 全部重新计算 |
| 固定 `[Context folded: ...]` | ✅ 稳定前缀 | 16K tokens 命中，TTFT 大幅下降 |

实测数据（见 Section 13）：
- 35B 模型：rounds 策略下 2705 tokens prompt，97% 缓存命中，TTFT 2.4s→1.1s
- 9B 模型：rounds 策略下 4661 tokens prompt，90% 缓存命中（system 前缀）

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
| KV Cache 复用 | vLLM APC, SGLang RadixAttention | 基础设施层缓存 prefix KV | ✅ Rapid-MLX v0.6.71 已修复 MoE non-trimmable 问题，prefix cache 正常工作 |
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

### 7.6 占位消息设计：固定文本 + Prefix Cache 优化

占位消息使用**固定文本**而非动态内容（含 dropped_count、tool_count、file_mentions），原因：

**Prefix Cache 稳定性**：Rapid-MLX v0.6.71 修复了 MoE non-trimmable 问题后，prefix cache 可以正常命中。如果占位消息每轮变化，prefix cache 在占位消息处断裂，system + tools + head 的 ~16K tokens 缓存无法复用。固定文本使整个前缀保持稳定。

**动态信息的替代方案**：被丢弃的文件名等信息虽然有用，但模型通过重新 Read 即可恢复（开销仅 1-2s），远低于 prefix cache 未命中导致的 TTFT 增加。

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

### Phase 1：基础实现 — 稳定占位 + Token 预算 ✅ 已完成
- [x] 增强 `truncate_messages_if_needed()` 添加 `rounds` 分支（`_apply_rounds_truncation` 辅助函数）
- [x] 实现 token 预算动态触发（`PROXY_CTX_TOKEN_BUDGET`，从 `keep_rounds` 递减到 min=2）
- [x] 实现固定占位消息（Prefix Cache 友好，替代动态内容）
- [x] 实现连续 user role 处理（review S2，合并到 tail 首条 user 消息）
- [x] 在 `_handle_messages` 中调整执行顺序：clear → date_norm → think_strip → compress → truncate
- [x] 提取 `_char_strategy_truncation()` 为独立函数，修复 char 策略 fallback
- [x] 添加配置参数（`PROXY_CTX_TRUNCATE_STRATEGY`、`PROXY_CTX_KEEP_ROUNDS`、`PROXY_CTX_TOKEN_BUDGET`、`PROXY_CTX_TOKEN_RATIO`）
- [x] 更新代理日志输出（rounds 日志含 estimated_tokens、actual_keep_rounds、budget）
- [x] 28 单元测试通过

### Phase 2：Prefix Cache 验证 ✅ 已完成
- [x] Rapid-MLX 升级到 v0.6.71，确认 MoE non-trimmable 问题已修复
- [x] 35B 模型 prefix cache 验证：精确匹配 100%、长静态前缀 99.6%、rounds 策略 97%
- [x] 9B 模型 prefix cache 验证：精确匹配 100%、大 system 99.3%、rounds 策略 90%
- [x] 固定占位消息验证：system + tools + head + 固定占位 ≈ 16K+ 稳定前缀可跨轮次缓存

### Phase 3：调优（可选）
- [ ] 根据实际体验调整 `PROXY_CTX_KEEP_ROUNDS` 和 `PROXY_CTX_TOKEN_BUDGET`
- [ ] 评估动态窗口策略效果
- [ ] 收集用户反馈

### Phase 4：高级优化 ✅ 已完成（提升至 Phase 1）
- [x] ~~评估 LLM 生成 summary 替代静态占位~~ → 三级压缩链已实现：LLM → 规则 → 静态折叠
- [x] 高频小步压缩 → P1-2 增量压缩已实现（`_incremental_compress`）
- [ ] 研究阶段感知压缩的代理层实现可能性

### P0-1：结构化 Metrics 日志 ✅ 已完成
- [x] 新增 `_metrics_ctx` thread-local + `log_metrics()` 函数
- [x] 每请求输出 `logs/proxy_metrics.jsonl`（结构化 JSON）
- [x] 管线各步骤 metrics 收集：error_translation、tool_clear、loop_detect、think_strip、compress、truncate、tool_filter
- [x] 质量标记自动生成：`high_drop_ratio`、`llm_compress_failed`、`budget_overflow`、`loop_injected`
- [x] 压缩比计算（`compression_ratio`）
- [x] 配置：`PROXY_METRICS_ENABLED=true`、`PROXY_METRICS_DIR=logs`

### P0-2：动态工具定义过滤 ✅ 已完成
- [x] 新增 `_filter_tools()` 函数 + `TOOL_ALWAYS_KEEP` 白名单（12 个核心工具）
- [x] 扫描最近 N 轮 assistant 消息收集已使用工具名
- [x] `tool_choice` 兼容：指定工具强制保留
- [x] 过滤后过少时回退到原始列表
- [x] 预期节省：44→15 tools ≈ 5-8K tokens
- [x] 配置：`PROXY_TOOL_FILTER_ENABLED`、`PROXY_TOOL_FILTER_MAX=20`、`PROXY_TOOL_FILTER_RECENT=5`

### P1-1：BM25 MVP（关键词索引）✅ 已完成
- [x] `_extract_keywords()` 从 dropped 消息提取关键词（文件名、错误类型、函数名）
- [x] `_inject_keyword_context()` 在 tail 消息中子串匹配并注入
- [x] 集成到 `_apply_rounds_truncation()`，追加到压缩摘要后
- [x] 纯内存，无持久化，~1-2ms 开销
- [x] 配置：`PROXY_HISTORY_INDEX=rule`、`PROXY_HISTORY_TOP_K=5`、`PROXY_HISTORY_MAX_CHARS=500`

### P1-2：增量压缩 ✅ 已完成
- [x] 会话级 `_summary_cache`（LRU，最多 10 个会话，3000 chars/摘要）
- [x] `_incremental_compress()` 只压缩新增 dropped 消息，合并已有缓存
- [x] `_merge_summaries_with_llm()` LLM 合并两个摘要（总长 > 2000 chars 时触发）
- [x] `truncate_messages_if_needed()` 和 `_apply_rounds_truncation()` 新增 `session_id` 参数
- [x] 收益：LLM 调用从 40-60 条 → 5-10 条新消息，prompt 从 8K→1-2K chars

### BM25 Phase 2-3（增强版/完整版）未开始
- [ ] Bigram 分词器 + 内存倒排索引 + TF 评分
- [ ] BM25 评分 + 代码感知分词 + JSONL 持久化 + 跨会话加载

---

## 10. 外部建议评估（Kimi）

Kimi 针对 "MacBook Pro 运行 Qwen3.6-35B 50K 上下文 TTFT 40s" 给出了 5 条建议，以下逐条评估与本项目的关系：

### 10.1 建议与现状对比

| # | Kimi 建议 | 本项目状态 | 评估 |
|---|-----------|-----------|------|
| 1 | Prompt Cache 文件化（mlx-lm `save_prompt_cache`） | ❌ 不可行 | 我们使用 Rapid-MLX，非原生 mlx-lm。但 Rapid-MLX v0.6.71 的内置 prefix cache 已足够好 |
| 2 | 切换至 Rapid-MLX / vMLX | ✅ 已完成 | 我们已在用 Rapid-MLX + 8-bit KV 量化。vMLX 的 0.22s TTFT 数据疑为极短 prompt 的基准，非 50K 上下文 |
| 3 | Prompt 结构重排（静态前缀 + 动态尾部） | ✅ 已实现 | date normalization + 固定占位消息，v0.6.71 prefix cache 正常工作后效果显著 |
| 4 | KV Cache 量化 + 关闭 swap | ✅ 已完成 | `RAPID_MLX_KV_QUANTIZATION=true`，`KV_QUANT_BITS=8`。48GB 统一内存基本不触发 swap |
| 5 | 框架级备选（MLC-LLM 等） | ⚠️ 不适用 | MLC-LLM 100K 场景需 70-85GB 内存，超出 48GB 上限 |

### 10.2 Kimi 未覆盖的优化（我们已实施）

1. **代理层上下文截断**（本文档核心方案）— Kimi 未提及此路径，因为在 Claude Code + 本地模型的组合下，这是代理层独有的优化空间
2. **工具调用压缩** — `compress_cleared_tool_results` 合并连续空工具调用循环
3. **Thinking block 清理** — `strip_old_thinking_blocks` 删除旧 assistant thinking 内容
4. **并发控制** — `PROXY_MAX_CONCURRENT=1` 防止双请求 OOM

### 10.3 结论

Kimi 的建议在**推理引擎选择**层面有价值（确认了我们选择 Rapid-MLX 的正确性），但在**代理层优化**层面未覆盖我们的核心方案。随着 Rapid-MLX v0.6.71 修复了 MoE prefix cache 问题，我们的 rounds 策略 + 固定占位消息 + prefix cache 形成了**双层优化**：代理层减少 prompt 长度，引擎层缓存稳定前缀，两者协同降低 TTFT。

---

## 11. 相关文档

- `docs/research-context-optimization/06-context-compression-strategy.md` — **上下文压缩管理策略总览**（Phase 1-3 整合版）
- `docs/research-context-optimization/05-plan.md` — 分阶段落地路线图
- `docs/research-context-optimization/04-solutions.md` — 代理层可落地的优化方向
- `docs/04-analysis-diagnostics/dcp-strategy-analysis-20260618.md` — 竞品上下文压缩策略分析
- `docs/02-architecture-design/proxy-pipeline-reference.md` — 8 层代理管线参考
- `docs/rapid-mlx-cache-analysis.md` — Prefix cache 命中问题分析（v0.6.30，non-trimmable）
- `docs/rapid-mlx-cache-analysis-supplement.md` — 补充实验数据
- `docs/proxy-context-window-design-review.md` — 本文档 review 意见
- `CLAUDE.md` — 代理层架构说明
- `AGENTS.md` — 环境变量与编码规范

> 注：v0.6.71 已修复 MoE non-trimmable 问题，`rapid-mlx-cache-analysis.md` 中记录的问题已解决。

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

---

## 13. Prefix Cache 验证结果（Rapid-MLX v0.6.71）

### 13.1 背景

Rapid-MLX v0.6.30 中，MoE 模型（Qwen3.6-35B-A3B、Qwen3.5-9B）的 ArraysCache 被标记为 `non_trimmable=True`，导致 LCP（Longest Common Prefix）匹配策略被跳过，prefix cache 全部 MISS。v0.6.71 修复了此问题。

### 13.2 直接后端测试

通过 OpenAI API 直接请求后端，验证基础 cache 功能：

#### 35B 模型（Qwen3.6-35B-A3B-4bit）

| 测试 | prompt_tokens | cached | remaining | 命中率 | TTFT |
|------|---------------|--------|-----------|--------|------|
| 精确匹配（短） | 33 | 33 | 0 | **100%** | 即时 |
| 前缀匹配（短） | 51 | 35 | 16 | **68%** | 即时 |
| LCP 匹配（短） | 51 | 14 | 37 | **27%** | 即时 |
| 长静态前缀 | 3863 | 3847 | 16 | **99.6%** | 3.0s→0.5s |

#### 9B 模型（Qwen3.5-9B-MLX-4bit）

| 测试 | prompt_tokens | cached | remaining | 命中率 | TTFT |
|------|---------------|--------|-----------|--------|------|
| 精确匹配（短） | 30 | 30 | 0 | **100%** | 即时 |
| 前缀匹配（短） | 48 | 32 | 16 | **67%** | 即时 |
| 大 system 直接 | 4219 | 4208 | 30 | **99.3%** | 3.6s→0.5s |

### 13.3 代理层 Rounds 策略测试

通过 anthropic_proxy.py（Anthropic Messages API → OpenAI 转换）发送 agent 风格请求：

#### 35B 模型 Rounds 策略

| 请求 | prompt_tokens | cached | remaining | 命中率 | TTFT |
|------|---------------|--------|-----------|--------|------|
| req1 (MISS→store) | 2705 | — | — | — | 2.4s |
| req2 (HIT prefix) | 2770 | 2689 | 81 | **97.0%** | 1.1s |

#### 9B 模型 Rounds 策略

| 请求 | prompt_tokens | cached | remaining | 命中率 | TTFT |
|------|---------------|--------|-----------|--------|------|
| req1 (HIT system prefix) | 4661 | 4208 | 453 | **90.2%** | — |
| req2 (HIT system prefix) | 4659 | 4208 | 451 | **90.5%** | 1.2s |

### 13.4 关键结论

1. **v0.6.71 修复了 MoE prefix cache**：9B 和 35B 模型的 prefix cache 均正常工作
2. **固定占位消息有效**：system + tools + head + 固定占位形成稳定前缀，跨轮次可被缓存
3. **缓存命中率 90-99%**：稳定前缀（system prompt 部分）几乎完全命中
4. **TTFT 显著下降**：35B 从 2.4s 降到 1.1s（2.2x 加速），9B 大 system 从 3.6s 降到 0.5s（7x 加速）

### 13.5 优化机制协同

```
代理层（rounds 策略）           引擎层（prefix cache）
  ↓                              ↓
68K → 4K-25K tokens           稳定前缀 16K+ tokens 命中
  ↓                              ↓
TTFT 90s → 2-15s              + 90-99% 缓存命中
  ↓                              ↓
  └────────── 协同效果 ──────────┘
              ↓
         实际 TTFT：1-5s（vs 原始 90s）
 ```

---

## 14. 死循环问题分析与修复（v5）

> 日期: 2026-06-04  
> 问题: 模型在 agentic 场景下反复 Read 同一文件，形成死循环

### 14.1 问题现象

在真实 Claude Code 会话中（围棋游戏 AI 对战系统项目，477 条消息），模型产生了 **219 次无效的 Read 调用**，占总 tool_use 调用（233 次）的 **94%**：

```
[437] assistant: tool_use:Read → spec.md
[438] user: tool_result: "Wasted call — file unchanged since your last Read..."
[439] assistant: tool_use:Read → spec.md    (重复)
[440] user: tool_result: "Wasted call..."
... (重复 219 次) ...
[463] assistant: "Hi! I'm ready to help."   (模型放弃)
```

**会话统计**：

| 指标 | 数值 |
|------|------|
| 总消息数 | 477 条 |
| tool_use 调用 | 233 次 |
| Read 调用 | 226 次（97%） |
| Bash 调用 | 7 次（3%） |
| "Wasted call" 响应 | 219 次 |
| 浪费率 | **94.0%** |
| 实际产出 | 零（未完成任何分析任务） |

### 14.2 根因分析

死循环的根本原因是 **tool_result 清除导致的语义丢失**：

```
1. 模型首次 Read spec.md → 获取完整内容（~21K 字符）
2. 多轮对话后，clear_old_tool_results 清除该 tool_result
   → 内容变为 "[cleared to save context: 21000 chars]"
3. 模型丢失 spec.md 的关键上下文
4. 模型尝试重新 Read spec.md
5. Claude Code 检测到文件未变 → 返回 "Wasted call — file unchanged..."
6. 模型不理解 "Wasted call" 的含义（这是 Claude Code 的缓存机制）
7. 模型再次 Read → 再次 "Wasted call" → 死循环
```

**三层原因**：

| 层级 | 原因 | 影响 |
|------|------|------|
| **语义层** | 清除 tool_result 丢失文件内容，模型无法回忆 | 模型被迫重新读取 |
| **理解层** | 模型不理解 "Wasted call" 错误信息 | 不知应换用其他方式获取 |
| **防御层** | 无循环检测机制 | 可无限重复同一操作 |

### 14.3 修复方案

采用三层组合防御：

#### 14.3.1 死循环检测（防御层）

在 `_handle_messages` 中检测最近 3 次 tool_use 是否为同一调用（名称+参数相同），若检测到则注入用户打断消息：

```python
# anthropic_proxy.py _handle_messages()
# 检测连续 3 次相同 tool_use
if len(tool_call_history) >= 3:
    last_calls = tool_call_history[-3:]
    if last_calls[0] == last_calls[1] == last_calls[2]:
        loop_msg = {
            "role": "user",
            "content": [{"type": "text", "text": (
                "[System notice: The last 3 assistant messages all called "
                f"{name} with identical arguments. This appears to be a loop. "
                f"Please stop calling {name} and either use a different approach "
                f"or inform the user that you cannot complete this task.]"
            )}]
        }
        raw_messages.append(loop_msg)
```

#### 14.3.2 语义保留（语义层）

清除 tool_result 时保留前 200 字符预览，让模型知道文件大致内容：

```python
# anthropic_proxy.py clear_old_tool_results()
if isinstance(original, str) and original_len > 300:
    snippet = original[:200].replace("\n", " ").strip()
    block["content"] = f"[cleared to save context: {original_len} chars. Preview: {snippet}]"
else:
    block["content"] = f"[cleared to save context: {original_len} chars]"
```

效果示例：
```
清除前: tool_result 内容为 spec.md 全文（21,000 字符）
清除后: "[cleared to save context: 21000 chars. Preview: # 围棋游戏 AI 对战系统产品规格文档 ## 1. 系统概述 ### 1.1 产品定位 一个基于 Web 的围棋游戏系统...]"
```

#### 14.3.3 保留更多近期 tool_result（配置层）

将 `PROXY_TOOL_KEEP` 从 5 提升至 10：

```bash
# configs/rapid-mlx-9b.conf
PROXY_TOOL_KEEP=10    # 原值: 5
```

### 14.4 集成测试验证

#### 14.4.1 测试环境

| 项目 | 值 |
|------|---|
| 模型 | Qwen3.5-9B-MLX-4bit |
| 后端 | rapid-mlx v0.6.71 |
| 代理 | anthropic_proxy.py (含 v5 修复) |
| 配置 | PROXY_TOOL_KEEP=10, THRESHOLD=20000 |

#### 14.4.2 构造测试

模拟死循环场景：14 个 tool_result（各 3000 字符）+ 3 次相同 Read 调用：

```python
# 构造 14 个 tool_result + 3 次相同 Read(spec.md)
messages = [...14 pairs of tool_use/tool_result + 3 loop Read calls...]
resp = requests.post('http://127.0.0.1:4000/v1/messages', json=body)
```

#### 14.4.3 验证结果

代理日志确认三项修复全部生效：

```
[09:49:43]   -> Tool clearing: 5 tool_results cleared, 25,000 chars freed (kept last 10)
[09:49:56]   -> Loop detected: Read called 3 times with same args, injected break message
[09:49:56]   -> Tool clearing: 217 tool_results cleared, 81,263 chars freed (kept last 10)
```

**真实 Claude Code 会话也触发了检测**（该会话本身是旧代理版本产生的 219 次死循环，重启后首次请求被新代理捕获）。

| 验证项 | 结果 | 证据 |
|--------|------|------|
| 循环检测 | ✅ 通过 | `Loop detected: Read called 3 times with same args, injected break message` |
| 语义保留 | ✅ 通过 | 超过 300 字符的 tool_result 带 `Preview:` 前缀 |
| KEEP=10 | ✅ 通过 | `kept last 10` 而非旧的 `kept last 2/5` |
| 单元测试 | ✅ 通过 | 28 tests OK |

### 14.5 修复前后对比

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| tool_result 清除 | `[cleared to save context: 21000 chars]` | `[cleared to save context: 21000 chars. Preview: # 围棋游戏...]` |
| 重复 Read 循环 | 无限循环（219+ 次） | 第 3 次注入打断消息 |
| 近期上下文保留 | 仅保留最后 5 个 tool_result | 保留最后 10 个 |
| 模型自恢复 | 需改用 Bash cat（第 465 次尝试才发现） | 直接在打断消息引导下换用其他方式 |

### 14.6 已知限制

1. ~~**循环检测仅限同一 tool_use**~~：已在 v6 中修复，现在支持模式检测（相同文本+工具名集合）
2. **预览可能截断关键信息**：200 字符对大文件可能不够，但受 token 预算约束
3. **打断消息可能被模型忽略**：模型可能继续尝试其他工具获取同一信息
4. ~~**阈值硬编码**~~：已实现 `PROXY_LOOP_THRESHOLD` 环境变量

### 14.7 v6 优化（已实施）

参见 §15-§19。

---

## 15. Lost-in-the-Middle 问题与缓解策略

### 15.1 问题描述

LLM 在长上下文中，**中间位置**的信息准确率显著低于**开头**（首因偏差）和**结尾**（近因偏差）。

对编码智能体（可能运行数十轮甚至数百轮）的影响：
- **开头**：原始任务描述 — 可能被保留但语义遗忘
- **结尾**：最近行动 — 自然保留
- **中间**：错误消息、解决方案、架构决策、代码演进 — **压缩时丢失风险最高**

### 15.2 参考：kimix 四项缓解措施

| # | 措施 | 代理层实现 | 状态 |
|---|------|-----------|------|
| 1 | HEAD 独立保留（首条消息永远不被压缩） | `PROXY_CTX_KEEP_HEAD=2` | ✅ 已有 |
| 2 | 自适应保留深度（根据信号动态调整） | `_compute_adaptive_rounds()` | ✅ v6 新增 |
| 3 | 结构化压缩（LLM/规则替代简单删除） | 三级降级链 | ✅ v6 新增 |
| 4 | BM25 历史索引 | 未实现 | 🔜 未来 |

---

## 16. 自适应保留深度（措施 2）

### 16.1 设计思路

固定 `rounds=8` 无法适应不同复杂度的对话。当对话包含错误、多文件编辑等复杂状态时，需要保留更多近期上下文。

### 16.2 信号与增量

| 信号 | 增量 | 检测方式 | 理由 |
|------|------|---------|------|
| tool_result 含 error/exception/failed | +1 轮 | 扫描 user 消息中 tool_result 内容 | 错误状态需要更多上下文解决 |
| assistant 中 Write/Edit > 2 个 | +1 轮 | 扫描 assistant 消息中 tool_use | 多文件变更需要连续性 |

**上限**：`base_rounds × 2`，防止无限膨胀。

### 16.3 实现

函数 `_compute_adaptive_rounds(messages, base_rounds)` 在截断前计算自适应轮数，替代固定 `PROXY_CTX_KEEP_ROUNDS`。

```
实际保留轮数 = min(base_rounds + 额外轮数, base_rounds × 2)
```

---

## 17. 三级压缩降级链（措施 3）

### 17.1 降级架构

```
截断触发
  ├─ dropped >= 10 消息 → 尝试 LLM 压缩（30s 超时）
  │   ├─ 成功 → 使用 LLM 结构化摘要
  │   └─ 失败 → 降级到规则压缩
  ├─ dropped < 10 消息 → 直接规则压缩
  │   ├─ 有提取内容 → 使用规则化摘要
  │   └─ 无提取内容 → 降级到简单折叠
  └─ 简单折叠（原始行为）："[Context folded: N messages omitted...]"
```

### 17.2 LLM 压缩（`_compress_middle_with_llm`）

**触发条件**：被截断的中间消息 >= 10 条

**流程**：
1. 将中间消息转为文本格式（每条截断到 300 chars）
2. 总量限制 8000 chars（防止压缩本身消耗过多 token）
3. 调用本地 LLM（`http://127.0.0.1:8081/v1/chat/completions`）
4. 使用结构化提示词，输出 XML 格式

**压缩提示词结构**：
```
<current_focus>当前正在做什么</current_focus>
<errors_solutions>所有错误及其解决方式</errors_solutions>
<code_state>当前文件状态、关键代码签名</code_state>
<decisions>架构/设计决策</decisions>
<pending>未完成的任务</pending>
```

**优先级顺序**（参考 kimix）：
1. 当前任务状态
2. 错误与解决方案
3. 代码演进（仅保留最终工作版本）
4. 系统上下文
5. 设计决策
6. 待办事项

**约束**：
- `max_tokens=1024`（压缩输出不超过 1K tokens）
- `temperature=0.3`（低创造性，高保真）
- `timeout=30s`（不阻塞主请求太久）
- `stream=False`（非流式，快速获取完整输出）

### 17.3 规则化压缩（`_extract_middle_summary_rules`）

**触发条件**：LLM 压缩失败，或被截断消息 < 10 条

**提取逻辑**：

| 提取目标 | 来源 | 格式 |
|---------|------|------|
| 错误信息 | user/tool_result 含 error/exception/failed | 原文保留（前 500 chars） |
| 已解决信息 | tool_result 含 successfully/updated/created | `[resolved]` 前缀 |
| 代码变更 | assistant/Write/Edit tool_use | `Write(file_path)` 列表 |
| 文件状态 | 所有 tool_use 的 file_path | 文件→最后操作映射 |
| 架构决策 | assistant/text 含 DECISION/TODO/FIXME/IMPORTANT | 原文保留（前 200 chars） |

**输出格式**：
```xml
[Compressed context from N earlier messages (rule-based):]
<errors_solutions>
- Error: Cannot read property 'x' of undefined
- [resolved] File created successfully
</errors_solutions>
<code_changes>
- Write(/path/to/board.js)
- Edit(/path/to/ai.js)
</code_changes>
<file_states>
- /path/to/board.js: last Write
- /path/to/ai.js: last Edit
</file_states>
```

### 17.4 简单折叠（原始行为）

**触发条件**：规则压缩也无法提取有价值内容

**输出**：
```
[Context folded: N earlier messages omitted. Previous work included M tool interactions. Files previously accessed: a.js, b.js. Retaining last K conversation rounds.]
```

---

## 18. 模式检测循环检测（v6 增强）

### 18.1 问题

v5 的循环检测只匹配"相同工具+相同参数"的精确循环。当模型交替调用 `Read(board.js)` → `Read(ai.js)` → `Read(board.js)` 时，参数不同，检测不触发。

### 18.2 解决方案

新增**模式匹配**：检测"相同文本输出 + 相同工具名集合"的语义循环。

```
pattern = (text前200 chars, 工具名sorted集合)
连续 >= 3 次相同 pattern → 触发循环检测
```

**示例**：模型输出 "Based on the conversation history..." + Read(board.js)，下次 "Based on the conversation history..." + Read(ai.js) —— 文本相同，工具集合 {Read} 相同，触发检测。

---

## 19. 35B 模型配置优化

### 19.1 GPU 内存配置演进

| 参数 | v5 (0.60) | v6 (0.75) | 效果 |
|------|-----------|-----------|------|
| `--gpu-memory-utilization` | 0.60 | **0.75** | allocation_limit 28.8→30.2 GB |
| `--cache-memory-mb` | 5120 | **4096** | 适配 35B 内存占用 |
| `--kv-cache-turboquant` | 无 | **启用 (4-bit)** | 替代 8-bit 普通量化 |
| `--pin-system-prompt` | 无 | **启用** | 固定 system prompt 在 KV cache |
| `--max-num-seqs` | 默认 | **1** | 单请求独占 |

### 19.2 效果对比

| 指标 | 0.60 + 8bit KV | 0.75 + turboquant | 改善 |
|------|---------------|-------------------|------|
| Metal active | 28-30 GB | **23-25 GB** | -17% |
| Forced cache clear | 1660 次 | **0 次** | 消除 |
| Cache HIT rate | < 50% | 逐步积累 | 改善 |
| 39K chars 响应 | 121s | **20-21s** | **6x** |

### 19.3 配置文件

```bash
# configs/rapid-mlx-35b.conf 关键参数
RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.75 --cache-memory-mb 4096 --max-num-seqs 1 --kv-cache-turboquant --kv-cache-turboquant-bits 4 --pin-system-prompt"
PROXY_MAX_TOKENS_OVERRIDE=16384
PROXY_OUTPUT_TOKEN_LIMIT_RATIO=2.0
PROXY_BACKEND_TIMEOUT=600
PROXY_TOOL_KEEP=8
PROXY_CTX_TOKEN_BUDGET=30000
```

### 19.4 System Prompt 稳定性分析

| 组件 | 大小 | 变化频率 |
|------|------|---------|
| msg[0] user (system-reminder + claudeMd) | 1908 chars | ✅ 同日内不变 |
| msg[1] system (skills 列表) | 5735 chars | ✅ 稳定 |
| Tools schema (27 tools) | 81920 chars | ✅ 稳定 |

`--pin-system-prompt` 配合日期标准化逻辑，确保前缀完全稳定，prefix cache 可持续命中。

---

## 20. 完整上下文管理流程（v8）

```
请求进入 anthropic_proxy.py
  │
  ├─ 1. 解析 messages, tools；初始化 _metrics_ctx
  ├─ 2. 内存压力检查（Phase 3）
  │     └─ _should_reject_for_memory() → 503 + Retry-After（超限）
  ├─ 3. 并发窗口记录 → _record_request_for_concurrency()
  ├─ 4. 日期标准化（固定 currentDate）
  ├─ 5. Error translation（Wasted/FileNotFound → 自然语言）
  ├─ 6. Tool clearing（语义优先级清除，默认关闭）
  │     ├─ 语义评分：Read=3, Agent=3, WebFetch=2, Bash=1, Edit/Write=1
  │     ├─ 动态 KEEP：子代理 auto KEEP=15
  │     └─ Bash dedup（Jaccard >= 0.7 合并）
  ├─ 7. 循环检测（精确 + 模式匹配）
  │     ├─ 精确：相同工具名+相同参数 >= 3 次
  │     └─ 模式：相同文本+相同工具集合 >= 3 次
  ├─ 8. Re-read 检测（Read 清除后的文件）
  ├─ 9. Thinking block 清理
  ├─ 10. Phase 2 语义压缩（Cache Aligner dynamic zone）
  │     ├─ _detect_content_type() → json/code/log/text
  │     ├─ ContentRouter 选择压缩器
  │     └─ CompressionAuditor 校验，失败回退
  ├─ 11. Cache Aligner 拆分：prefix zone + dynamic zone
  ├─ 12. Context truncation（核心）
  │     ├─ 生命周期阶段：_classify_lifecycle_stage(chars)
  │     ├─ 自适应保留深度：_compute_adaptive_rounds()
  │     ├─ 分离：HEAD + TAIL(自适应N轮) + MIDDLE
  │     ├─ MIDDLE 压缩（增量优先）：
  │     │   ├─ 增量压缩：_incremental_compress() 检查 _summary_cache
  │     │   ├─ >= 10 msgs → LLM 压缩（30s timeout）
  │     │   ├─ 失败/<10 → 规则压缩
  │     │   └─ 无内容 → 简单折叠
  │     ├─ Read 结果智能保留（rounds 策略）
  │     ├─ 关键词索引（P1-1）：
  │     │   ├─ _extract_keywords(dropped)
  │     │   └─ _inject_keyword_context(keywords, tail)
  │     └─ 重组：HEAD + 压缩摘要 + 保留 Read + TAIL
  ├─ 13. 动态 max_tokens（Phase 3）：_compute_dynamic_max_tokens()
  ├─ 14. 工具过滤（P0-2）：_filter_tools()
  │     ├─ TOOL_ALWAYS_KEEP 白名单
  │     ├─ 最近 N 轮已使用工具保留
  │     └─ tool_choice 指定工具强制保留
  ├─ 15. 转发到后端
  ├─ 16. 输出控制
  │     ├─ Streaming: FORCE_STOPPED (text + tool_call args)
  │     ├─ Non-streaming: text truncation
  │     └─ _repair_truncated_json() 修复截断 JSON
  ├─ 17. 动态并发调整（Phase 3）：_adjust_concurrency()
  ├─ 18. 失败快照（Phase 3）：_write_request_snapshot()
  └─ 19. Metrics 记录（schema v1）
        ├─ _finalize_metrics()：动态 token 估算 + 固定字段
        ├─ quality_flags + compression_ratio
        └─ log_metrics() → logs/proxy_metrics.jsonl
```

---

## 21. BM25 历史索引设计（措施 4）

### 21.1 问题分析

即使经过自适应保留深度和结构化压缩，中间部分的关键信息仍可能被归档。典型的遗忘场景：

| 场景 | 丢失内容 | 后果 |
|------|---------|------|
| 长会话中修复过 bug | 错误信息和解决方案被截断 | 模型可能重犯同一错误 |
| 做过架构决策 | 设计讨论被截断 | 模型可能推翻已定方案 |
| 读过的关键文件 | 文件内容被清除 | 模型重新读取（Wasted call） |
| 用户早期要求 | 原始需求描述被压缩 | 模型偏离原始目标 |

**核心矛盾**：保留所有历史 → token 超限；截断历史 → 关键信息丢失。

**BM25 索引的解法**：不保留原始消息，但保留**可搜索的索引**。当模型需要时，按需检索并注入。

### 21.2 架构设计

```
┌─────────────────────────────────────────────────┐
│                  请求处理流程                      │
│                                                   │
│  请求进入 → 解析 messages                          │
│              │                                    │
│              ├─→ [索引] 新消息写入 BM25 索引         │
│              │    - user 消息全文索引               │
│              │    - assistant 文本索引              │
│              │    - tool_use: 工具名+参数索引        │
│              │    - tool_result: 前 500 chars 索引  │
│              │                                    │
│              ├─→ [截断] 自适应保留 + 压缩            │
│              │                                    │
│              ├─→ [检索] 用最新 user 消息搜索索引      │
│              │    - BM25 评分                       │
│              │    - 取 top-K 相关片段               │
│              │    - 注入为 user 消息（在末尾）        │
│              │                                    │
│              └─→ [转发] 压缩后 + 检索增强的请求       │
└─────────────────────────────────────────────────┘

索引持久化:
  ┌──────────┐     ┌──────────┐
  │ 内存索引  │ ←→  │ 磁盘文件  │
  │ (dict)   │     │ JSONL    │
  └──────────┘     └──────────┘
```

### 21.3 数据结构

#### 21.3.1 索引条目

```python
class IndexEntry:
    session_id: str       # 会话 ID
    msg_index: int        # 消息在原始会话中的序号
    role: str             # user / assistant
    tokens: list[str]     # 分词结果 (bigram)
    content_hash: str     # 内容哈希（去重用）
    summary: str          # 压缩后的摘要（≤200 chars）
    timestamp: str        # ISO 时间戳
    metadata: dict        # 工具名、文件路径等
```

#### 21.3.2 倒排索引

```python
inverted_index: dict[str, list[tuple[int, int]]]
# token → [(entry_id, term_frequency), ...]

doc_lengths: list[int]    # 每个 entry 的 token 数
avg_doc_length: float     # 平均文档长度
N: int                    # 总文档数
```

### 21.4 分词策略

#### Bigram 分词器（基础版）

```python
def tokenize(text: str) -> list[str]:
    # 1. 清洗：去除标点、统一小写
    cleaned = re.sub(r'[^\w\s]', ' ', text.lower())
    words = cleaned.split()
    
    # 2. 停用词过滤
    words = [w for w in words if w not in STOP_WORDS and len(w) > 1]
    
    # 3. Bigram 生成
    bigrams = []
    for i in range(len(words) - 1):
        bigrams.append(f"{words[i]}_{words[i+1]}")
    
    # 4. 保留 unigram（单字词也有价值）
    return words + bigrams
```

**示例**：
```
输入: "board.js 的 countLiberties 方法报错 TypeError"
分词: ["boardjs", "countliberties", "方法", "报错", "typeerror",
       "boardjs_countliberties", "countliberties_方法", "方法_报错", "报错_typeerror"]
```

#### 代码感知分词（增强版）

```python
def tokenize_code_aware(text: str) -> list[str]:
    tokens = []
    
    # 提取文件路径
    for path in re.findall(r'[/\w]+\.\w+', text):
        tokens.append(f"path:{path}")
    
    # 提取函数/方法名
    for name in re.findall(r'\b([a-z][a-zA-Z0-9_]*)\s*\(', text):
        tokens.append(f"func:{name}")
    
    # 提取错误类型
    for err in re.findall(r'\b([A-Z]\w+Error)\b', text):
        tokens.append(f"error:{err}")
    
    # 常规 bigram
    tokens.extend(tokenize(text))
    
    return tokens
```

### 21.5 BM25 评分算法

```python
def bm25_score(query_tokens: list[str], entry_id: int,
               k1: float = 1.5, b: float = 0.75) -> float:
    score = 0.0
    dl = doc_lengths[entry_id]
    
    for qt in query_tokens:
        if qt not in inverted_index:
            continue
        
        # IDF = log((N - df + 0.5) / (df + 0.5) + 1)
        df = len(inverted_index[qt])
        idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
        
        # TF in document
        tf = 0
        for eid, freq in inverted_index[qt]:
            if eid == entry_id:
                tf = freq
                break
        
        # BM25 formula
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * dl / avg_doc_length)
        score += idf * numerator / denominator
    
    return score
```

### 21.6 检索与注入流程

```
1. 提取最新 user 消息作为 query
2. 对 query 分词
3. BM25 检索 top-K（K=5）相关条目
4. 过滤：
   - 已在当前 context 中的条目 → 跳过（去重）
   - 相似度低于阈值 → 跳过
   - 条目过多 → 只保留 top-K
5. 格式化为检索结果消息
6. 注入到 messages 末尾（在最新 user 消息之前）
```

**注入格式**：
```json
{
  "role": "user",
  "content": [{
    "type": "text",
    "text": "[History context retrieved for current task:]\n- [msg#23] Fixed TypeError in countLiberties by adding null check\n- [msg#45] Decision: Use BFS for group detection instead of DFS\n- [msg#67] board.js evaluated: countTerritory returns {black: 12, white: 8}\n..."
  }]
}
```

**关键设计决策**：
- 注入为 **user 消息**（不是 system），避免破坏 system prompt 的 cache
- 放在 **最新 user 消息之前**，确保模型优先处理真实用户输入
- 带有 `[History context]` 前缀，模型可区分历史检索和当前上下文

### 21.7 索引持久化

#### 存储结构

```
data/
├── index/
│   ├── {session_id}.jsonl      # 索引条目（追加写入）
│   └── {session_id}.meta.json   # 元数据（倒排索引快照）
```

#### 写入策略

```
每 N 个请求（N=5）刷新一次索引到磁盘
代理重启时从磁盘加载 {session_id}.jsonl 重建倒排索引
```

#### JSONL 格式

```jsonl
{"idx":0,"role":"user","tokens":["hi","围棋","游戏"],"summary":"用户发起围棋游戏项目","hash":"abc123","ts":"2026-06-05T14:00:00","meta":{"tool":null,"files":[]}}
{"idx":1,"role":"assistant","tokens":["read","boardjs","countliberties"],"summary":"读取 board.js 分析 countLiberties","hash":"def456","ts":"2026-06-05T14:00:20","meta":{"tool":"Read","files":["/path/board.js"]}}
```

### 21.8 与现有系统的集成点

```
_handle_messages() 中的位置：

1. 解析 messages ──→ 索引新消息
2. Error translation ──→ 标记错误类型到索引 metadata
3. Tool clearing ──→ 更新索引（标记已清除的消息）
4. 循环检测 ──→ 记录循环模式到索引
5. Context truncation ──→ 截断前的消息已在索引中
6. ★ BM25 检索 ──→ 注入相关历史（新增）
7. 转发
```

---

## 22. 基础半设计（MVP）

### 22.1 设计原则

完整版 BM25 索引需要持久化存储、倒排索引维护、磁盘 I/O。**基础半设计**用最简方式实现核心价值：

> **"在截断时保留关键词索引，按需注入，无需持久化"**

### 22.2 核心简化

| 特性 | 完整版 | 基础半设计 |
|------|--------|-----------|
| 索引存储 | 磁盘 JSONL + 内存倒排索引 | **纯内存，随请求重建** |
| 分词 | Bigram + 代码感知 | **简单关键词提取** |
| 检索算法 | BM25 | **TF 匹配（词频排序）** |
| 检索触发 | 每次请求 | **仅截断时** |
| 持久化 | session 级磁盘存储 | **无持久化** |
| 注入位置 | 最新 user 消息前 | **压缩摘要中嵌入** |

### 22.3 基础版实现

#### 22.3.1 关键词提取

```python
def _extract_keywords(messages):
    """从被截断的消息中提取关键词索引"""
    keywords = {}  # keyword -> [summary, ...]
    
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        text = ""
        files = []
        
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        text += b.get("text", "") + " "
                    elif b.get("type") == "tool_use":
                        name = b.get("name", "")
                        inp = b.get("input", {})
                        if isinstance(inp, dict):
                            fp = inp.get("file_path", inp.get("path", ""))
                            if fp:
                                files.append(fp)
                        text += f"{name} "
                    elif b.get("type") == "tool_result":
                        tc = b.get("content", "")
                        if isinstance(tc, str):
                            text += tc[:200] + " "
        
        # 提取关键词：文件名 + 函数名 + 错误类型
        for path in files:
            fname = path.split("/")[-1]
            keywords.setdefault(fname, []).append(f"{role}: {text[:100]}")
        
        for err in re.findall(r'\b([A-Z]\w*(?:Error|Exception))\b', text):
            keywords.setdefault(err, []).append(f"{role}: {text[:100]}")
        
        for func in re.findall(r'\b([a-z][a-zA-Z0-9_]{3,})\s*\(', text):
            keywords.setdefault(func, []).append(f"{role}: {text[:100]}")
    
    return keywords
```

#### 22.3.2 检索与注入

```python
def _inject_keyword_context(keywords, current_messages):
    """检查当前消息是否引用了被截断的关键词"""
    # 从最近 3 条消息中提取查询词
    query_text = ""
    for msg in current_messages[-3:]:
        content = msg.get("content", "")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    query_text += b.get("text", "") + " "
        elif isinstance(content, str):
            query_text += content + " "
    
    # 匹配关键词
    matches = []
    for kw, summaries in keywords.items():
        if kw.lower() in query_text.lower():
            matches.append(f"[{kw}]: {summaries[-1]}")  # 取最新一条
    
    if not matches:
        return None
    
    return "[Relevant history context:]\n" + "\n".join(matches[:5])
```

#### 22.3.3 集成方式

在 `_apply_rounds_truncation()` 中：

```python
# 1. 从 dropped 消息提取关键词
keywords = _extract_keywords(dropped)

# 2. 检查 tail 消息是否引用了这些关键词
keyword_context = _inject_keyword_context(keywords, tail)

# 3. 追加到压缩摘要后面
if keyword_context:
    compressed_text += "\n\n" + keyword_context
```

### 22.4 基础版 vs 完整版效果对比

| 场景 | 基础版 | 完整版 |
|------|--------|--------|
| 模型提到 "之前那个 TypeError" | ✅ 能找到错误摘要 | ✅ 能找到完整错误+解决方案 |
| 模型提到 "board.js" | ✅ 能找到文件操作记录 | ✅ 能找到完整代码变更历史 |
| 模型提到 "之前讨论的架构" | ❌ 无匹配（"架构"太泛） | ✅ BM25 语义匹配 |
| 跨会话恢复 | ❌ 无持久化 | ✅ 从磁盘加载索引 |
| 性能开销 | ~1ms | ~10ms + 磁盘 I/O |

### 22.5 实施路线

```
Phase 1 (基础半设计) ✅ 已完成:
  - _extract_keywords() 从被截断消息中提取关键词
  - _inject_keyword_context() 在 tail 中匹配并注入
  - 零持久化，零额外 I/O
  - 与现有 _apply_rounds_truncation() 集成
  - 配置: PROXY_HISTORY_INDEX=rule, PROXY_HISTORY_TOP_K=5, PROXY_HISTORY_MAX_CHARS=500

Phase 2 (增强版) 未开始:
  - Bigram 分词器
  - 内存倒排索引
  - TF 评分检索
  - 会话级关键词缓存（避免重复提取）

Phase 3 (完整版) 未开始:
  - BM25 评分
  - 代码感知分词
  - JSONL 持久化
  - 跨会话索引加载
  - 自适应 top-K
```

### 22.6 配置参数

```bash
# 历史索引开关
PROXY_HISTORY_INDEX=rule       # off / rule / bm25

# 检索参数
PROXY_HISTORY_TOP_K=5          # 最多注入 K 条检索结果
PROXY_HISTORY_MAX_CHARS=500    # 检索结果最大字符数

# BM25 参数（Phase 3）
PROXY_HISTORY_BM25_K1=1.5      # BM25 k1 参数
PROXY_HISTORY_BM25_B=0.75      # BM25 b 参数

# 持久化（Phase 3）
PROXY_HISTORY_INDEX_DIR=data/index  # 索引存储目录
```

---

## 23. Phase 3：资源与观测护栏

> 本节补充 2026-06-18 实施的 Phase 3 能力。完整环境变量清单见 `AGENTS.md`「Resource & observation guardrails (Phase 3)」。

### 23.1 设计目标

在 Phase 1（prefix 稳定）和 Phase 2（语义压缩）基础上，增加第三层防御：**资源护栏 + 可观测性**，解决以下问题：

| 问题 | Phase 3 对策 |
|------|--------------|
| 后端突发 OOM | 内存压力主动拒绝 |
| `max_tokens` 被后端忽略导致长输出 OOM | 动态 `max_tokens` 上限 |
| 固定并发无法适应负载 | 动态并发控制 |
| 大请求失败后无现场 | 失败快照 |
| 调参缺乏量化依据 | `/status` 看板 + metrics schema v1 |

### 23.2 生命周期阶段驱动的统一阈值

所有压缩/截断/输出限制力度由 `_classify_lifecycle_stage()` 根据总字符数单调递增决定：

```
chars →    15K       40K        90K        180K       350K     400K
           │         │          │           │          │        │
Stage:   INIT     GROWTH    EXPANSION   SATURATION  OOM_DANGER  PRE_TRUNC
L2+L4:   跳过    尾40%清除   尾60%+     全dynamic+   全量+     全量+
                 (无think)  think=5    think=3     think=1   think=1
L5截断:   关       关       预算触发    rounds=8   rounds=3  rounds=2
Frozen:   12       12         12          6          0         0
max_tok: 4096     4096       4096        2048       2048      2048
```

### 23.3 动态 Token 估算（`_estimate_tokens_dynamic`）

替代单一 `chars / ratio`，按内容语言自动选择 chars-per-token：

| 内容类型 | 默认 ratio | 识别方式 |
|----------|-----------|----------|
| Chinese | 1.5 | 中文字符占比 > 30% |
| English | 4.0 | 英文单词占比高 |
| Code    | 3.0 | 标识符/括号/关键字密度高 |
| Mixed   | 2.0 | 其他 |

该估算同时用于：token budget 触发、OOM safety 截断、`/status` 显示、`metrics` 的 `est_input_tokens`。

### 23.4 内存压力主动拒绝（`_should_reject_for_memory`）

- 读取系统内存 `used_pct`。
- 若 `used_pct > PROXY_MEMORY_REJECT_THRESHOLD`（local 默认 90，cloud 默认 95），在 `do_POST` 入口直接返回：
  - HTTP 503
  - `error.type = backend_oom`
  - `Retry-After: PROXY_RETRY_AFTER_SECONDS`
  - `"retryable": true`
- 被拒绝的请求不消耗后端资源，为内存释放争取时间。

### 23.5 动态 `max_tokens`（`_compute_dynamic_max_tokens`）

根据生命周期阶段、后端类型、当前内存压力动态降低 `max_tokens`：

| Stage | local 上限 | cloud 上限 | rapid-mlx 额外系数 |
|-------|-----------|-----------|-------------------|
| INIT / GROWTH / EXPANSION | 4096 | 8192 | ×0.8 |
| SATURATION / OOM_DANGER / PRE_TRUNC | 2048 | 4096 | ×0.8 |

同时记录 `max_tokens_original`、`max_tokens_dynamic`、`used_pct` 到 metrics。

### 23.6 动态并发控制（`_adjust_concurrency`）

维护最近 50 个请求的滑动窗口：

| 信号 | 动作 |
|------|------|
| P95 延迟 > 30s 或错误率 > 20% | semaphore 减 1（最低 1） |
| P95 延迟 < 15s 且错误率为 0 | semaphore 加 1（最高 local=4 / cloud=8） |

local 模式默认最大并发仍受硬件限制，动态调整主要用于从临时高负载快速降级。

### 23.7 请求失败快照（`_write_request_snapshot`）

请求失败（HTTP >= 500 或异常）时，写入：

```
logs/snapshots/<request_id>_before.json   # 原始请求体
logs/snapshots/<request_id>_after.json    # 经过 pipeline 后的请求体
```

最多保留 `PROXY_SNAPSHOT_MAX_FILES` 个文件，旧文件自动清理。

### 23.8 `/status` 与 metrics schema v1

`/status` 新增「Context Optimization」卡片，展示：

| 字段 | 说明 |
|------|------|
| `avg_common_prefix_ratio` | 最近 N 请求平均公共前缀比例 |
| `avg_compression_ratio` | 平均压缩比 |
| `loop_events` | 循环检测触发次数 |
| `blocker_events` | blocker 触发次数 |
| `last_blocker` | 最近 blocker 详情 |
| `dynamic_concurrent` | 当前动态并发值 |

metrics schema v1 强制固定字段集合，新增 `schema_version: "v1"`、`token_ratio`、`est_input_tokens`、`est_output_tokens`、`memory_rejected`、`used_pct`、`max_tokens_*`、`snapshot_written`、`dynamic_concurrent` 等字段。

### 23.9 与 Phase 1/2 的关系

```
Phase 1 (稳定层) → Phase 2 (压缩层) → Phase 3 (护栏层)
     │                   │                   │
     │ 保护 prefix        │ 减少 token        │ 防止 OOM
     │ 稳定 cache         │ 保留语义          │ 可观测
     └───────────────────┴───────────────────┘
                         ↓
              完整上下文压缩管理体系
```

### 23.10 验证结果

| 验证项 | 结果 |
|--------|------|
| 单元测试 | 281 个通过 |
| 集成测试 | 6 套通过（blocker/loop/cache-align/compress/memory-reject/status） |
| `/status` Context Optimization 卡片 | 字段正常显示 |
| metrics schema v1 | 所有记录固定字段完整 |
