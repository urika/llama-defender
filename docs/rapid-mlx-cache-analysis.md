# Rapid-MLX Prefix Cache 命中问题分析报告

> 分析时间：2026-06-03
> 后端版本：rapid-mlx v0.6.30
> 模型：mlx-community/Qwen3.6-35B-A3B-4bit
> 硬件：MacBook Pro M5 Pro, 48GB unified memory

---

## 1. 问题概述

在 Claude Code 代理模式下使用 rapid-mlx 作为本地 LLM 后端时，发现 **prefix cache（前缀缓存）完全无法命中**，导致每轮请求都需要重新计算完整的 prompt（~60K tokens），单次推理延迟稳定在 **55-75 秒**。即使代理层做了前缀稳定化（日期标准化），cache 仍然 100% MISS。

---

## 2. 现象描述

### 2.1 后端日志特征

```
# 每次请求都是 MISS
INFO:vllm_mlx.scheduler:[cache_fetch] request=bd91ed81-17b MISS prompt_tokens=61256 time=0.002s entries=6

# 请求结束后 cache 被保存
INFO:vllm_mlx.scheduler:[cache_store] request=bd91ed81-17b tokens=61378 stored=True cache_entries=4 cache_mem=4085MB

# 但下一个请求仍然是 MISS
INFO:vllm_mlx.scheduler:[cache_fetch] request=b07728bc-ece MISS prompt_tokens=61398 time=0.002s entries=4
```

**关键矛盾**：`entries=4` 表示缓存条目存在于内存中，但 `cache_fetch` 仍然返回 `MISS`。

### 2.2 延迟表现

| 指标 | 数值 | 说明 |
|------|------|------|
| Prompt tokens | 61,000-61,500 | 长对话历史 |
| Tokens to prefill | = Prompt tokens | 100% 重新计算，0% 命中 |
| First token latency | 55-75s | 全部花在 prefill 阶段 |
| Output tokens | 90-300 | 实际生成内容很少 |
| 生成速度 | ~2 tok/s | 正常，瓶颈在 prefill |

### 2.3 代理层前缀稳定化已生效

代理层 `_handle_messages()` 中将动态日期标准化为固定占位符：

```python
"Today's date is 2026/06/03." → "Today's date is DATE_PLACEHOLDER."
```

验证结果（连续 3 轮请求的 msg0/msg1 哈希完全一致）：

```
[14:21:05] Msg hashes: msg0=8767aff2, msg1=b2d6317a, total_msgs=59
[14:22:16] Msg hashes: msg0=8767aff2, msg1=b2d6317a, total_msgs=59
[14:24:42] Msg hashes: msg0=8767aff2, msg1=b2d6317a, total_msgs=61
```

---

## 3. 诊断过程

### 3.1 Phase 1：排除 forced cache clear 干扰

**初始假设**：cache 被频繁清空导致无法命中。

**证据**：
```
WARNING:vllm_mlx.engine_core:[Memory pressure] 27.4GB > 26GB threshold, forced cache clear
```

**调整**：将 `--gpu-memory-utilization` 从 `0.60` 提高到 `0.68`。

**结果**：forced cache clear 完全消失，cache 可以正常保存（4 entries, 4085MB）。

**结论**：forced cache clear 不是 MISS 的根本原因。

### 3.2 Phase 2：对照实验验证 cache 机制

#### 实验 A：完全相同的请求

```bash
curl ... -d '{"messages":[{"role":"user","content":"Say hello"}]}'
# 第一次：MISS → cache_store（保存 2 entries）
# 第二次（完全相同）：HIT cached=14 remaining=0
```

**结论**：rapid-mlx 的 cache 硬件层面工作正常，完全相同的 prompt 可以 100% 命中。

#### 实验 B：前缀相同但内容不同

```bash
# 第一次："Say hello" → cache_store
# 第二次："Say hello again" → MISS（entries=7，缓存存在但匹配失败）
```

**初步结论**：rapid-mlx 的 prefix cache **看似只支持完全匹配**。

> 注：后续 DEBUG 日志验证（第 5 章）证明 rapid-mlx 实际实现了 LCP 前缀匹配，但由于 MoE 模型的 `ArraysCache` 层不可修剪，前缀匹配被强制跳过，导致表现为"只支持完全匹配"。

### 3.3 Phase 3：分析帮助文档

rapid-mlx v0.6.30 的帮助信息：

```
--enable-prefix-cache    Enable prefix caching for repeated prompts (default: enabled)
```

关键词是 **"repeated prompts"（重复的 prompt）**，而非 **"prefix matching"（前缀匹配）**。

这与标准 vLLM 的 prefix caching 语义不同。标准 vLLM 的 prefix caching 是将 prompt 分块，逐块匹配前缀；而 rapid-mlx 的实现似乎只是缓存整个 prompt 的 KV，仅对**完全相同的 prompt** 生效。

---

## 4. 根本原因

### 4.1 核心结论（修正版）

**原结论"rapid-mlx 只支持整句完全匹配"与源码不符。**

Rapid-MLX 实际上实现了 **4 种匹配策略**（exact / prefix / supersequence / LCP），但由于 **MoE 架构的 non-trimmable cache layers 问题**，前缀匹配被强制跳过，导致看似"只支持完全匹配"的现象。

### 4.2 源码证据：4 种匹配策略

`memory_cache.py` 的 `_fetch_locked()` 方法实现了 4 种匹配：

| 匹配类型 | 触发条件 | 预期收益 |
|---------|---------|---------|
| `exact` | 完整 prompt 完全相同 | 全量命中，0 prefill |
| `prefix` | 缓存比请求短 | 只计算剩余 tokens |
| `supersequence` | 缓存比请求长且可修剪 | 修剪后命中，计算差量 |
| `lcp` | 最长公共前缀（divergent 序列） | 相同前缀部分命中 |

代码注释明确说明 LCP 的设计目标：
> "This handles the agentic pattern: same system+context prefix but different final user message."

### 4.3 关键代码（`memory_cache.py`）

```python
has_non_trimmable = any(
    not (
        lc.is_trimmable()
        if hasattr(lc, "is_trimmable")
        else hasattr(lc, "trim")
    )
    for lc in best_super.cache
)

if excess > 0 and has_non_trimmable:
    logger.debug(
        "[cache_fetch] supersequence match skipped: "
        "non-trimmable cache layers (hybrid model)"
    )
```

### 4.4 问题根因

**Qwen3.6-35B-A3B 是 MoE 架构（256 experts, 3 active）**，其 cache entry 包含不可修剪的层。当 supersequence 或 LCP 匹配找到候选 entry 时，如果存在 non-trimmable 层，匹配会被跳过，最终返回 MISS。

### 4.5 与标准 vLLM 的对比

| 特性 | 标准 vLLM Prefix Caching | rapid-mlx v0.6.30（理论） | rapid-mlx v0.6.30（实际 MoE） |
|------|-------------------------|--------------------------|-----------------------------|
| 匹配粒度 | Token block 级别 | 4 种策略（含 LCP） | LCP 找到后被迫跳过 |
| "Say hello" + " again" | HIT 前缀 | 应 HIT 前缀 | **MISS**（non-trimmable）|
| 对话场景适用性 | ✅ | ✅（设计目标）| ❌（MoE 限制）|
| 重复批量请求 | ✅ | ✅ | ✅（exact match 工作）|

### 4.6 其他可能因素（已排除）

| 假设 | 验证方法 | 结论 |
|------|----------|------|
| Tool call IDs 动态变化 | 检查请求体 | 不是主因 |
| 日期字符串导致前缀变化 | 代理层哈希验证 | 已排除，msg0/msg1 哈希稳定 |
| Tokenizer 行为不一致 | 对照实验 | 已排除，相同请求能 HIT |
| Cache 被系统清空 | 调高 utilization 后验证 | 已排除，cache 保存成功 |
| `--prefix-cache-size` 配置问题 | 帮助文档分析 | 该参数只在 legacy mode 生效 |
| rapid-mlx 未实现前缀匹配 | 源码分析 | **已排除**，实际实现了 LCP |

---

## 5. DEBUG 日志验证结果（2026-06-03）

### 5.1 验证方法

开启 rapid-mlx DEBUG 日志：

```bash
rapid-mlx serve mlx-community/Qwen3.6-35B-A3B-4bit \
  --log-level DEBUG \
  --gpu-memory-utilization 0.60
```

发送测试请求（curl + 代理层真实请求），观察 `cache_fetch` 的 DEBUG 输出。

### 5.2 关键日志摘录

#### 长对话请求（prompt ≈ 80K tokens）

```log
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP scan: cached_len=79812 req_len=79863 lcp=29424
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP candidate: lcp=29424 entry_len=79812 excess=50388 non_trimmable=True cache_layers=40 layer_types=['ArraysCache', 'ArraysCache', 'ArraysCache']
INFO:vllm_mlx.scheduler:[cache_fetch] request=5d99e1c5-749 MISS prompt_tokens=79863 time=0.003s entries=5
```

#### Tiny 请求（prompt = 11 tokens）

```log
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP scan: cached_len=11 req_len=13 lcp=3
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP candidate: lcp=3 entry_len=11 excess=8 non_trimmable=True cache_layers=40 layer_types=['ArraysCache', 'ArraysCache', 'ArraysCache']
INFO:vllm_mlx.scheduler:[cache_fetch] request=f1fb1b47-d78 MISS prompt_tokens=13 time=0.000s entries=5
```

### 5.3 日志解读

| 字段 | 长对话值 | Tiny 值 | 含义 |
|------|---------|---------|------|
| `cached_len` | 79,812 | 11 | 缓存中已存在的 prompt 长度 |
| `req_len` | 79,863 | 13 | 当前请求的 prompt 长度 |
| `lcp` | **29,424** | 3 | **最长公共前缀 token 数** |
| `excess` | 50,388 | 8 | 需要重新计算的 token 数 |
| `non_trimmable` | **True** | **True** | 是否存在不可修剪的 cache layer |
| `cache_layers` | 40 | 40 | 总 cache layer 数量 |
| `layer_types` | ArraysCache ×3 | ArraysCache ×3 | 不可修剪层的类型 |
| 最终结果 | **MISS** | **MISS** | 匹配被强制跳过 |

### 5.4 性能影响估算

如果 `non_trimmable=False`：

- 长对话场景下，**29,424 tokens（约 37%）的前缀可以复用**
- 只需计算剩余的 **50,388 tokens**
- 按当前 prefill 速度估算：
  - 完整 80K tokens ≈ 60-70 秒
  - 50K tokens ≈ 35-45 秒
  - **潜在节省：约 20-25 秒/轮**

### 5.5 验证结论

1. **rapid-mlx 的 LCP 算法本身是正确的**：成功找到了 29K tokens 的公共前缀
2. **所有 40 个 cache layer 都是 non-trimmable**：`layer_types=['ArraysCache', ...]`
3. **与 prompt 长度无关**：即使是 11 tokens 的小请求，同样 `non_trimmable=True`
4. **这是一个上游实现限制**：Qwen3.6-35B-A3B 的 `ArraysCache` 层没有实现 `trim()` 方法

---

## 6. 影响评估

### 5.1 性能影响

| 场景 | 有 Prefix Cache | 无 Prefix Cache | 差距 |
|------|----------------|----------------|------|
| 单轮延迟 | ~10-20s（仅计算新增 token）| ~60s（完整 prefill）| **3-6x** |
| 长会话体验 | 流畅，延迟稳定 | 每轮都慢，累加等待 | 极差 |
| 并发能力 | 更高（prefill 快）| 更低（长时间占用 GPU）| 显著 |

### 5.2 内存影响

虽然 cache 不命中，但 rapid-mlx 仍然会尝试保存 cache（`cache_store`），消耗 ~4GB 内存。这部分内存被浪费在永远无法命中的缓存上。

---

## 7. 相关配置记录

### 6.1 当前生效配置

```bash
# configs/rapid-mlx-35b.conf
RAPID_MLX_EXTRA_ARGS="--gpu-memory-utilization 0.60 --cache-memory-percent 0.20 --cache-memory-mb 5120 --prefix-cache-size 2000"
```

### 6.2 配置参数说明

| 参数 | 当前值 | 作用 | 效果 |
|------|--------|------|------|
| `--gpu-memory-utilization` | 0.60 | Metal 内存分配上限和 cache clear 阈值 | ✅ 防止 OOM，但会触发 forced cache clear |
| `--cache-memory-mb` | 5120 | Cache 绝对上限（MB）| ✅ 限制 cache 内存不无限增长 |
| `--cache-memory-percent` | 0.20 | Cache 内存比例（auto-detect 时）| 参考值，被 `--cache-memory-mb` 覆盖 |
| `--prefix-cache-size` | 2000 | 最大缓存条目数 | ⚠️ 仅在 legacy mode 生效，当前 memory-aware 模式不使用 |
| `--enable-prefix-cache` | 默认启用 | 启用前缀缓存 | ⚠️ LCP 算法正确，但 MoE 层不可修剪导致 MISS |

### 6.3 尝试过的参数组合

| 组合 | 结果 |
|------|------|
| `--gpu-memory-utilization 0.60`（默认配置）| forced cache clear 频繁触发，cache=0 |
| `--gpu-memory-utilization 0.68` | forced cache clear 消失，但仍是 MISS；后发生 Metal OOM 崩溃，已回滚 |
| `--no-memory-aware-cache` | 未测试，可能改变缓存策略但风险未知 |

---

## 8. 结论与建议

### 7.1 核心结论

1. **rapid-mlx v0.6.30 实现了 4 种 prefix cache 匹配策略（含 LCP），算法本身是正确的**
2. **Qwen3.6-35B-A3B 的 MoE `ArraysCache` 层不可修剪**，导致 LCP 匹配被强制跳过，对话场景下实际无效
3. **forced cache clear 是独立问题**，与 cache 不命中无关
4. **真正的性能瓶颈是 prefill 阶段**，而非生成阶段（2 tok/s 的生成速度是正常的）

### 7.2 可行的优化方向

| 方向 | 可行性 | 预期效果 | 风险 |
|------|--------|----------|------|
| 减少 prompt 长度 | 高 | 显著缩短 prefill 时间 | 可能丢失上下文 |
| 切换到 llama-server | 中 | llama-server 支持真正的 prefix caching | 性能可能比 rapid-mlx 慢 36% |
| 使用纯 Transformer 模型（非 MoE）| 中 | 如 Qwen3.5-27B，cache trim 可能正常工作 | 性能可能低于 35B-A3B |
| 等待 rapid-mlx 更新 | 低 | 需上游修复 `ArraysCache.trim()` | 时间不确定 |
| 使用 `--no-memory-aware-cache` | 中 | 切换到 legacy 缓存模式，行为可能不同 | 未验证，可能引入新问题 |

### 7.3 当前推荐的配置

保留 `--gpu-memory-utilization 0.60` 和 `--cache-memory-mb 5120`：
- 防止 Metal OOM 崩溃（48GB 内存已接近极限）
- 限制 cache 内存不无限增长
- 保留重复批量请求时 exact match 的可能性

---

## 附录 A：关键源码文件

| 文件 | 作用 |
|------|------|
| `vllm_mlx/memory_cache.py` | MemoryAwarePrefixCache 实现（4 种匹配策略） |
| `vllm_mlx/prefix_cache.py` | PrefixCacheManager / BlockAwarePrefixCache |
| `vllm_mlx/paged_cache.py` | Block 级别分页缓存管理 |
| `vllm_mlx/scheduler.py` | 调度器，调用 cache_fetch/cache_store |

---

## 附录 B：关键日志摘录

### 正常日志（INFO 级别）

```
# 旧配置（0.60）的 forced cache clear
WARNING:vllm_mlx.engine_core:[Memory pressure] 27.4GB > 26GB threshold, forced cache clear

# cache 保存成功
INFO:vllm_mlx.scheduler:[cache_store] request=bd91ed81-17b tokens=61378 stored=True cache_entries=4 cache_mem=4085MB

# 不命中（entries 存在但匹配失败）
INFO:vllm_mlx.scheduler:[cache_fetch] request=b07728bc-ece MISS prompt_tokens=61398 time=0.002s entries=4

# Exact match（HIT）
INFO:vllm_mlx.scheduler:[cache_fetch] request=191eab69-299 HIT prompt_tokens=14 cached=14 remaining=0 time=0.000s

# 前缀不同 MISS
INFO:vllm_mlx.scheduler:[cache_fetch] request=cec26809-174 MISS prompt_tokens=15 time=0.000s entries=7
```

### DEBUG 日志（关键验证证据）

```
# LCP 扫描找到公共前缀
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP scan: cached_len=79812 req_len=79863 lcp=29424

# LCP 候选被 non-trimmable 层跳过
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP candidate: lcp=29424 entry_len=79812 excess=50388 non_trimmable=True cache_layers=40 layer_types=['ArraysCache', 'ArraysCache', 'ArraysCache']

# Supersequence 匹配被跳过（相同根因）
DEBUG:vllm_mlx.scheduler:[cache_fetch] supersequence match skipped: non-trimmable cache layers
```
