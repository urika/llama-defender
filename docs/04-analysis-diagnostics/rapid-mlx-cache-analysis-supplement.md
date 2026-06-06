# Rapid-MLX Prefix Cache 命中问题 — 补充分析

> 日期：2026-06-03
> 基于：rapid-mlx v0.6.30 源码 + GitHub 仓库分析

---

## 核心结论（修正版）

**原报告结论"rapid-mlx 只支持整句完全匹配"与源码不符。**

Rapid-MLX 实际上实现了 4 种匹配策略（exact / prefix / supersequence / LCP），但由于 **MoE 架构的 non-trimmable cache layers 问题**，前缀匹配可能被跳过，导致看似"只支持完全匹配"的现象。

---

## Rapid-MLX Prefix Cache 匹配策略（源码证据）

`memory_cache.py` 的 `_fetch_locked()` 方法（约 line 743）实现了 4 种匹配：

| 匹配类型 | 触发条件 | 预期收益 |
|---------|---------|---------|
| `exact` | 完整 prompt 完全相同 | 全量命中，0 prefill |
| `prefix` | 缓存比请求短 | 只计算剩余 tokens |
| `supersequence` | 缓存比请求长且可修剪 | 修剪后命中，计算差量 |
| `lcp` | 最长公共前缀（divergent 序列） | 相同前缀部分命中 |

代码注释明确说明 LCP 的设计目标：
> "This handles the agentic pattern: same system+context prefix but different final user message."

---

## 为什么你的场景不命中？

### 关键代码（`memory_cache.py`）

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

### 问题根因

**Qwen3.6-35B-A3B 是 MoE 架构（256 experts, 3 active）**，其 cache entry 可能包含不可修剪的层。当 supersequence 或 LCP 匹配找到候选 entry 时，如果存在 non-trimmable 层，匹配会被跳过，最终返回 MISS。

### 这解释了

- `entries=4` 表示缓存确实存在于内存中
- 但 `cache_fetch` 仍返回 MISS
- 完全相同的请求（exact match）能命中，因为不涉及 trim

---

## 验证方法

### 1. 开启 Debug 日志

```bash
rapid-mlx serve mlx-community/Qwen3.6-35B-A3B-4bit \
  --log-level debug \
  --gpu-memory-utilization 0.68
```

如果看到以下日志，说明是 non-trimmable 层问题：
- `[cache_fetch] supersequence match skipped: non-trimmable cache layers`
- `[cache_fetch] LCP candidate: lcp=X entry_len=Y excess=Z`

### 2. 对照实验验证 exact match

```bash
# 完全相同请求两次
curl .../chat/completions -d '{"messages":[{"role":"user","content":"Say hello"}]}'
# 第二次应该 HIT: cached=N remaining=0
```

---

## 实际验证结果（2026-06-03）

### 验证环境

| 项目 | 值 |
|------|-----|
| rapid-mlx 版本 | v0.6.30 |
| 模型 | mlx-community/Qwen3.6-35B-A3B-4bit |
| 日志级别 | `--log-level DEBUG` |
| 验证方法 | 发送 curl 测试请求 + 触发代理层真实请求 |

### 关键日志摘录

#### 1. 长对话请求（prompt ≈ 80K tokens）

```log
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP scan: cached_len=79812 req_len=79863 lcp=29424
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP candidate: lcp=29424 entry_len=79812 excess=50388 non_trimmable=True cache_layers=40 layer_types=['ArraysCache', 'ArraysCache', 'ArraysCache']
INFO:vllm_mlx.scheduler:[cache_fetch] request=5d99e1c5-749 MISS prompt_tokens=79863 time=0.003s entries=5
```

#### 2. Tiny 请求（prompt = 11 tokens）

```log
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP scan: cached_len=11 req_len=13 lcp=3
DEBUG:vllm_mlx.memory_cache:[cache_fetch] LCP candidate: lcp=3 entry_len=11 excess=8 non_trimmable=True cache_layers=40 layer_types=['ArraysCache', 'ArraysCache', 'ArraysCache']
INFO:vllm_mlx.scheduler:[cache_fetch] request=f1fb1b47-d78 MISS prompt_tokens=13 time=0.000s entries=5
```

### 日志解读

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

### 性能影响估算

如果 `non_trimmable=False`：

- 长对话场景下，**29,424 tokens（约 37%）的前缀可以复用**
- 只需计算剩余的 **50,388 tokens**
- 按当前 prefill 速度估算：
  - 完整 80K tokens ≈ 60-70 秒
  - 50K tokens ≈ 35-45 秒
  - **潜在节省：约 20-25 秒/轮**

### 关键结论

1. **rapid-mlx 的 LCP 算法本身是正确的**：成功找到了 29K tokens 的公共前缀
2. **所有 40 个 cache layer 都是 non-trimmable**：`layer_types=['ArraysCache', ...]`
3. **与 prompt 长度无关**：即使是 11 tokens 的小请求，同样 `non_trimmable=True`
4. **这是一个上游实现限制**：Qwen3.6-35B-A3B 的 `ArraysCache` 层没有实现 `trim()` 方法

---

## 什么情况下前缀缓存会带来性能收益？

### 有效场景

| 场景 | 匹配类型 | 收益 |
|------|---------|------|
| 批量重复请求（完全相同 prompt） | exact | 最佳，0 prefill |
| 多轮对话中 system prompt 不变 | prefix/supersequence | 只需计算新增部分 |
| Agentic 场景（相同 context，不同 final message） | lcp | 前缀部分复用 |

### 无效场景

| 场景 | 原因 |
|------|------|
| 每轮都新增 assistant + user 消息 | prompt 永远与上一轮不完全相同 |
| MoE 架构（Qwen3.6-35B-A3B） | non-trimmable layers 导致前缀匹配被跳过 |
| Transformer 纯序列（支持 trim） | 可能正常命中 prefix/supersequence |

---

## 配置建议

### 当前配置（已优化）

```bash
RAPID_MLX_EXTRA_ARGS="--gpu-memory-utilization 0.60 --cache-memory-percent 0.20 --cache-memory-mb 5120 --prefix-cache-size 2000"
```

### 说明

- `--enable-prefix-cache` 默认已启用，无需额外配置
- `--gpu-memory-utilization 0.68` 减少 forced cache clear
- `--prefix-cache-size` 仅在 legacy mode 生效，当前 memory-aware 模式不使用

### 可尝试方向

| 方向 | 说明 | 风险 |
|------|------|------|
| `--no-memory-aware-cache` | 切换到 legacy 缓存模式 | 行为可能不同，未验证 |
| 使用纯 Transformer 模型（非 MoE） | 如 Qwen3.5-27B，cache trim 可能正常工作 | 性能可能低于 35B-A3B |

---

## 上游修复可能性

GitHub 上有多项 prefix cache 相关工作（2026 年 5 月下旬）：

- `fix(scheduler): port boundary-snapshot save path to mlx-lm 0.31+` (#435, #439)
- `feat(usage): surface prefix-cache hits in OpenAI + Anthropic` (#478)

建议关注上游是否有关于 MoE hybrid 模型 non-trimmable layers 的修复。

---

## 附录：关键源码文件

| 文件 | 作用 |
|------|------|
| `vllm_mlx/memory_cache.py` | MemoryAwarePrefixCache 实现（4 种匹配策略） |
| `vllm_mlx/prefix_cache.py` | PrefixCacheManager / BlockAwarePrefixCache |
| `vllm_mlx/paged_cache.py` | Block 级别分页缓存管理 |
| `vllm_mlx/scheduler.py` | 调度器，调用 cache_fetch/cache_store |

---

## 附录：相关日志格式

```
# Exact match（HIT）
INFO:vllm_mlx.scheduler:[cache_fetch] request=xxx HIT prompt_tokens=14 cached=14 remaining=0 time=0.000s

# Prefix match（HIT，remaining > 0）
INFO:vllm_mlx.scheduler:[cache_fetch] request=xxx HIT prompt_tokens=100 cached=50 remaining=50

# Non-trimmable skip（MISS，但 entries 存在）
DEBUG:vllm_mlx.scheduler:[cache_fetch] supersequence match skipped: non-trimmable cache layers

# Miss（完全未命中）
INFO:vllm_mlx.scheduler:[cache_fetch] request=xxx MISS prompt_tokens=61398 time=0.002s entries=4
```
