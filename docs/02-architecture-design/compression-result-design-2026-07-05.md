# TS-3 设计: 压缩结果结构化 (`CompressionResult`)

> **文档版本**: v0.1 (Draft, 待 W3末-W4 评审)
> **创建日期**: 2026-07-05
> **状态**: ⏳ 待评审 — W3末-W4 (2026-08-07 ~ 08-14) 进入实施
> **关联 PRD**: [`PRD-litellm-borrow-2026-07-05`](../01-requirements-product/PRD-litellm-borrow-2026-07-05.md) §2 TS-3
> **关联模块**: `proxy_state.py` / 新建 `compression_types.py` 或并入 `proxy_state` / `truncation.py` / `content_compressor.py` / `pipeline.py` / `admin_server.py`
> **关联缺陷**: DEF-003 (`re_read_rate` 公式错) / DEF-304 (可观测性仪表板缺)
> **前置依赖**: TS-2 (已交付,新增 `skipped_reason` 字段) / TS-1 (可在 W3末前已部分完成,提供 `bm25_scores` 字段来源)

---

## 目录

1. [背景与动机](#1-背景与动机)
2. [当前实现盘点](#2-当前实现盘点)
3. [范式转变: 从散落 dict 到结构化契约](#3-范式转变)
4. [API 设计](#4-api-设计)
5. [集成点](#5-集成点)
6. [边界与不变式](#6-边界与不变式)
7. [测试覆盖](#7-测试覆盖)
8. [风险与未决事项](#8-风险与未决事项)
9. [实施次序](#9-实施次序)

---

## 1. 背景与动机

### 1.1 现状观察

v0.6.0 PRD 接管了 8 个管线 stage (ContentCompressor / ContextTruncator / OOMSafetyFIFO 等) 的输出统计,但当前所有 stage 输出 stats 都是**散落 dict**,字段命名/语义/枚举值没有跨 stage 一致约束:

- `compress_tool_result` 返回 `{original, compressed, content_type, strategy, audit_pass, ratio}` (content_compressor.py:284)
- `_compress_content_pass` 返回 `(messages, {clear, think, compress})` (truncation.py:288)
- `truncate_messages_if_needed` 返回 `(messages, {enabled, strategy, skipped, ...})` (truncation.py:832+),不同 strategy (rounds/fifo/smart) 字段集合不重合
- `_apply_smart_truncation` 返回 stats 还有 TS-2 新增的 `skipped_reason` 字段
- Phase 1b 压缩 stats 是 list of dict (truncation.py:95),与 truncate 的顶层 dict 又不同结构

### 1.2 litellm 借鉴

litellm `litellm/types/compression.py:15` 的 `CompressedResult` TypedDict 统一字段:
```python
{
  "messages", "original_tokens", "compressed_tokens", "compression_ratio",
  "cache": Dict[str, str], "tools": List[dict],
  "compression_skipped_reason": NotRequired[str]
}
```

### 1.3 为何现在做

- DEFECT-LIST DEF-003 `re_read_rate` 公式错 (2862%) 根因之一是 stats 字段不清,字段命名 (`compression_ratio` vs `compression.ratio` vs `compress.ratio`) 三处不同含义
- DEFECT-LIST DEF-304 可观测仪表板缺齐,统一字段命名后,`admin_server.py` 一次聚合即可展示
- TS-2 已新增 `skipped_reason:` 字段(粒度不等),需正式登记到类型契约
- TS-1 W3末若已部分落地,BM25 分数 (`bm25_scores`) 需有承载处

### 1.4 TS-3 的本质

不是新功能,而是**把已有零散 stats 标准化为一个 TypedDict 契约**,让管线输出成为**可观测数据源**,而非工程师看代码才知道字段的隐式约定。

---

## 2. 当前实现盘点

### 2.1 现有返回类型汇总

| 函数 | 位置 | 返回签名 | 字段集合 |
|---|---|---|---|
| `compress_tool_result` | content_compressor.py:218 | `dict` | original, compressed, content_type, strategy, audit_pass, ratio |
| `_compress_content_pass` | truncation.py:17 | `(messages, dict)` | clear, think, compress (各为 dict) |
| `clear_old_tool_results` | truncation.py:290 | `(messages, dict)` | enabled, cleared, cleared_tool_results, kept, cleared_chars, total_chars_before |
| `_apply_smart_truncation` | truncation.py:602 | `(messages, dict)` | enabled, strategy="smart", truncated, dropped_messages, kept_messages, compressed_assistants, kept_chars, budget_chars, (TS-2) reason, skipped_reason, chars, unprotected_chars |
| `_apply_rounds_truncation` | truncation.py:1350 | `(messages, dict)` | enabled, strategy="rounds", skipped, (when truncated) dropped_messages, kept_messages, chars_after, compression="folded"/"rules" |
| `truncate_messages_if_needed` | truncation.py:807 | `(messages, dict)` | enabled, strategy, skipped, truncated, ... (按 strategy 字段各异) |
| Phase 1b 压缩 list | truncation.py:95 | `list[dict]` (内部) | msg_idx, block_idx, content_type, strategy, ratio, audit_pass, original_len, compressed_len |

### 2.2 字段语义冲突点

| 名字 | 在哪里 | 含义 | 冲突 |
|---|---|---|---|
| `strategy` | compress_tool_result / truncate / _apply_smart | "json_sieve"/"code_compress"/... vs "smart"/"rounds"/"fifo" | 同名异义,需用 namespace 区分 |
| `ratio` | compress_tool_result | compressed/original (越低压得越狠) | 与 metrics 里的 `compression_ratio` 同义 (前者小,后者也是 est_after/input_est) |
| `compression_ratio` | admin_server / anthropic_proxy metrics | est_after/input_est (整轮管线) | 与 `ratio` 同义但命名不同 |
| `reason` | _apply_smart | "below_budget" / "must_keep_exceeds_budget" / "invalid_anthropic_tool_sequence" | 有 `skipped_reason` 同义字段 (TS-2 引入),需统一 |
| `truncated` | truncate_messages_if_needed | bool | 与 `enabled` / `skipped` 三字段冗余表达三状态 |

### 2.3 现有 metrics 出口

- `pipeline.py:ContentCompressor.output_metrics` (line 1042) → 拼 `semantic_compress` + `tool_clear` + `think_strip`
- `pipeline.py:ContextTruncator.output_metrics` (line 1488) → 拼 `applied` + `triggered` + `strategy` + `dropped` + `kept` + `compression` + `chars_after`
- `admin_server._get_context_optimization_stats` (line 400) → 解析 `pipeline.semantic_compress` + `common_prefix_ratio`
- 没有跨请求的"今日压缩手段分布"统计 (BM25 vs rule_based vs smart vs rounds vs fifo)

---

## 3. 范式转变

### 3.1 从 "散落 dict 字段约定" 到 "结构化契约"

```
现状 (v0.6.0):
  truncate_messages_if_needed → (messages, {strategy:"smart", truncated:True, dropped_messages:3, ...})
  _compress_content_pass      → (messages, {clear:{...}, think:{...}, compress:{...}})
  compress_tool_result        → {original, compressed, ratio:0.4, ...}
                                    ↓
              各自被 ctx.compress_stats / ctx.trunc_stats / metrics JSONL 各拼一套字段名
              (admin_server 凑出 avg_compression_ratio; metrics 凑出 compression_ratio; 各人各写)

TS-3 (v0.6.1):
  compress_tool_result        → CompressionResult(...)
  truncate_messages_if_needed → CompressionResult(...)
  clear_old_tool_results       → CompressionResult(...)
  _compress_content_pass      → CompressionResult(..., sub={"clear":..., "think":..., "compress":list[...]})
                                    ↓
              统一 CompressionResult → ctx.compression_result (PipelineContext 单一字段)
              → output_metrics 一键序列化
              → admin_server 简单聚合 (今日 strategy 计数,压缩比直方图)
```

### 3.2 与 TS-1 / TS-2 的字段牵手

| TS-2/TS-1 引入字段 | TS-3 落点 |
|---|---|
| `skipped_reason` (TS-2) | `CompressionResult.skipped_reason` 顶级字段,枚举化 |
| `protected_indices` (TS-2 `_protected_pair_indices`) | `CompressionResult.protected_indices` (debug 用,序列化可选) |
| `dropped_indices` (TS-2 drop 决策) | `CompressionResult.dropped_indices` |
| `bm25_scores` (TS-1 Phase 1a) | `CompressionResult.bm25_scores: Dict[int, float]` |
| TS-1 返回 dict (含 score, original, compressed) | 作 `CompressionResult.sub.compress[i]` 数组元素 |

---

## 4. API 设计

### 4.1 新建 —— `compression_types.py`

```python
# compression_types.py
"""TS-3: 统一的压缩结果类型契约.

设计原则:
  - 兼容 Python 3.9 (TypedDict 来自 typing)
  - 不依赖任何第三方包
  - 字段均可 dict 化 (JSON 序列化),除可能含 bytes 的 audit payload 不参与 metrics

参考: litellm/types/compression.py:15 CompressedResult
"""
import sys
if sys.version_info >= (3, 8):
    from typing import TypedDict, List, Dict, Optional
else:
    from typing_extensions import TypedDict, List, Dict, Optional


class CompressionSubResult(TypedDict, total=False):
    """单条 tool_result 压缩子结果 (compress_tool_result 升级版)."""
    original: str
    compressed: str
    content_type: str          # "json" | "code" | "log" | "text" | "short"
    strategy: str              # "none" | "json_sieve" | "code_compress"
                               # | "log_compress" | "text_truncate"
                               # | "audit_fallback" | "bm25_drop" | "bm25_keep" (TS-1)
    audit_pass: bool
    ratio: float
    original_len: int
    compressed_len: int
    bm25_score: Optional[float]           # TS-1
    msg_idx: int                          # 来自 truncation Phase 1b
    block_idx: int


class CompressionResult(TypedDict, total=False):
    """统一压缩/截断/清除操作返回类型 (PRD-litellm-borrow §2 TS-3).

    所有 compress_tool_result / _compress_content_pass / truncate_messages_if_needed /
    clear_old_tool_results / _apply_smart_truncation / _apply_rounds_truncation 统一返回此类型。
    顶级字段全是可选 (total=False),让各调用点按需填字段而不强求全填。
    """
    # --- 主体 ---
    messages: List[dict]                   # 处理后消息 (顶层)
    original_tokens: int                   # 估算输入 tokens (可选)
    compressed_tokens: int                 # 估算输出 tokens (可选)
    compression_ratio: float               # 1 - compressed/original (0=完全压缩, 1=未动)
                                            #  与 litellm 一致: 越高代表保留越多

    # --- 策略 metadata ---
    strategy: str                          # "smart" | "rounds" | "fifo" | "char"
                                           #  | "bm25" | "rule_based" | "no_op"
    enabled: bool                           # 总开关是否启用
    skipped: bool                          # 是否跳过 (未触发)
    truncated: bool                         # 是否实际截断 (替代旧 truncated 字段)
    skipped_reason: Optional[str]          # "below_trigger" | "below_budget"
                                           #  | "invalid_anthropic_tool_sequence" (TS-2)
                                           #  | "all_protected" | "no_reduction"
                                           #  | "stage_skip" | "oom_emergency" (TS-2)
                                           #  | "below_limit"

    # --- 索引/配对 ---
    protected_indices: List[int]           # 受 CacheAligner / TS-2 保护的 msg idx
    dropped_indices: List[int]             # 被 drop 的 msg idx
    bm25_scores: Dict[int, float]          # TS-1: idx → BM25 score (空时为非 BM25 策略)
    cache_keys: Dict[str, str]             # 留空 (信息降级方案无 retrieval,与 litellm 不同)

    # --- 计数/字符预算 ---
    dropped_messages: int
    kept_messages: int
    compressed_assistants: int
    kept_chars: int
    budget_chars: int

    # --- 子结果 ---
    sub: Dict[str, object]                 # {"compress": [CompressionSubResult, ...],
                                            #  "clear": dict, "think": dict}
                                            #  (来自 _compress_content_pass 兼容旧字段)
```

### 4.2 修改 —— `compress_tool_result` 返回 `CompressionSubResult`

签名不变,返回 dict 字段集合对齐 `CompressionSubResult`:
- 新增 `original_len`, `compressed_len` (从 `len(original)` / `len(compressed)`)
- 新增 `bm25_score` (TS-1 接入后填,空则 omit)
- 保留 `original` / `compressed` / `content_type` / `strategy` / `audit_pass` / `ratio`

### 4.3 修改 —— `truncate_messages_if_needed` 返回 `CompressionResult`

```python
def truncate_messages_if_needed(messages, session_id=None, keep_rounds=None,
                                strategy=None, budget_chars=None):
    ...
    return result_messages, {
        "messages": result_messages,
        "enabled": True,
        "strategy": effective_strategy,
        "skipped": <bool>,
        "truncated": <bool>,
        "skipped_reason": <reason or None>,
        "protected_indices": list(_protected_pair_indices(messages, _ps.PROXY_CACHE_ALIGN_HEAD)),
        "dropped_indices": [...],
        "dropped_messages": ...,
        "kept_messages": ...,
        # strategy 各异字段保留向前兼容 ...
    }
```

### 4.4 修改 —— `_compress_content_pass` 返回 CompressionResult 顶层 + `sub`

```python
return messages, {
    "messages": messages,
    "strategy": "rule_based",  # 或 "bm25" 当 TS-1 启用
    "enabled": True,
    "skipped": not compressed_any,
    "compression_ratio": <1 - post/pre>,
    "sub": {
        "compress": compress_stats_list,  # list[CompressionSubResult]
        "clear": clear_stats,
        "think": think_stats,
    },
    "protected_indices": <from CacheAligner/TS-2>,
    "bm25_scores": <Dict[idx, float]> if PROXY_BM25_ENABLED else {},
}
```

### 4.5 修改 —— `clear_old_tool_results` 返回 CompressionResult

类似 `_compress_content_pass`,字段精简 (无 sub.compress,只有 sub.clear+think)

### 4.6 兼容性 —— `PipelineContext` 新增 `compression_result` 字段

`pipeline.py:PipelineContext` (around line 174) 新增:
```python
compression_result: Optional[dict] = None   # CompressionResult (统一)
# 保留旧字段兼容:
compress_stats: Optional[dict] = None       # legacy, deprecated in W4
trunc_stats: Optional[dict] = None          # legacy, deprecated in W4
```

Stage 在 `process()` 同时填新和旧字段 (双写)。`output_metrics` 读新字段,fallback 旧字段。W4 末尾删除旧字段 (一次性切换)。

### 4.7 admin_server 聚合 —— `_get_compression_stats()`

```python
def _get_compression_stats():
    """聚合当日 CompressionResult: 各 strategy 计数 + 平均 compression_ratio + skipped_reason 分布.
    
    输出格式 (供 /status JSON `compression` 段):
    {
      "today": {
        "strategy_counts": {"smart": 12, "rounds": 5, "bm25": 8, "fifo": 0},
        "avg_compression_ratio": 0.57,
        "skipped_reason_counts": {"invalid_anthropic_tool_sequence": 3, "below_budget": 22},
        "truncated_total": 18,
        "protected_pair_avg": 4.2,
      },
      "last_10m": {...}
    }
    """
```

吞并 `_get_context_optimization_stats` 中已有的 `avg_compression_ratio`,旧字段保留兼容期 4 周。

### 4.8 `/status` 段输出

```html
<div class="section">
  <h3>Compression (TS-3)</h3>
  <div class="row"><span class="label">Today / Strategies</span>
    <span class="value">smart=12, bm25=8, rounds=5, fifo=0</span></div>
  <div class="row"><span class="label">Avg Ratio</span>
    <span class="value">0.57</span></div>
  <div class="row"><span class="label">Skipped Reasons (10m)</span>
    <span class="value">below_budget=22, invalid_anthropic_tool_sequence=3</span></div>
  <div class="row"><span class="label">Avg Protected Pairs</span>
    <span class="value">4.2</span></div>
</div>
```

---

## 5. 集成点

### 5.1 与 ContentCompressor Stage 7 的关系

```python
class ContentCompressor(ConditionalStage):
    def process(self, ctx):
        cache_dynamic, compress_stats = truncation._compress_content_pass(...)
        # TS-3: 同时写 ctx.compression_result
        ctx.compression_result = compress_stats   # 已是 CompressionResult 类型
        ctx.compress_stats = compress_stats        # legacy (W4 末删除)
        ...
```

### 5.2 与 ContextTruncator Stage 17 的关系

```python
class ContextTruncator(ConditionalStage):
    def process(self, ctx):
        messages, trunc_stats = truncation.truncate_messages_if_needed(...)
        ctx.messages = messages
        ctx.compression_result = trunc_stats
        ctx.trunc_stats = trunc_stats            # legacy (W4 末删除)
        ...
```

### 5.3 与 OOMSafetyFIFO Stage 20 的关系

`_oom_safety_fifo` (TS-2 W2 创建) 返回的 dict 已含 `skipped_reason="oom_emergency"` 与 `strategy="oom_safety_fifo"`,直接符合 `CompressionResult.skipped_reason` 枚举值与 `strategy` 顶级字段。TS-3 仅在 CompressionResult 类型上形式化这两个字段 (无需改代码)。

### 5.4 与 metrics JSONL 的关系

`output_metrics` 各 stage 改为输出 CompressionResult 的 strip 版 (只保留顶级可聚合字段,移除 `messages` / `sub.compress[].original` 大字段)。Metrics 段命名统一:

```
pipeline.content_compressor.compression = {strategy, ratio, dropped, protected_n, bm25_scores_avg}
pipeline.context_truncator.compression  = {strategy, ratio, skipped_reason, dropped, kept, budget_chars}
pipeline.oom_safety.compression          = {strategy:"oom_safety_fifo", dropped}
```

### 5.5 与 TS-1 的协同 (W3末可用)

- TS-1 Phase 1a 计算的 `bm25_scores: Dict[(msg_idx, block_idx), float]`
- TS-3 在 `_compress_content_pass` 把它整理为 `CompressionResult.bm25_scores: Dict[int, float]` (key 改为 msg_idx,因为 metrics 关心 message 级聚合而非 block 级)

### 5.6 与 TS-2 的协同 (已交付)

- TS-2 在 `_apply_smart_truncation` 返回 `reason` 与 `skipped_reason` 两字段同义。TS-3 在 CompressionResult 顶层**只保留 `skipped_reason`**;`reason` 作为 sub-field 移到 `sub.smart_reason` 兼容旧 metrics 解析 (4 周兼容期后下线)。

---

## 6. 边界与不变式

### 6.1 不变式

- **I-1**: 现有调用点 (`pipeline.py:993`, `pipeline.py:1438`, `admin_server._get_context_optimization_stats`) 在 W4 d1-d3 必须无感知改 CompressionResult —— `ctx.compress_stats` / `ctx.trunc_stats` 仍可读字段,即使它们已是 CompressionResult 实例
- **I-2**: `total=False` TypedDict 允许任意子集,各调用点**不强制**填全字段;但 strategy 顶级必填
- **I-3**: `skipped_reason` 枚举值封闭集合,新增需在文档同步登记
- **I-4**: `compression_ratio` 与 litellm 一致: `1 - compressed/original`,越接近 1 越未压;与现有 metrics `compression_ratio` (= est_after/input_est) 同方向,可直接对接
- **I-5**: `messages` 顶级字段可 `None` (e.g. `clear_old_tool_results` 不改 messages 时),不强制

### 6.2 边界场景

| 场景 | 处理 |
|---|---|
| `truncate_messages_if_needed` returns dict of len-0 stats (disabled) | `CompressionResult.enabled=False`,其余字段 omit;`output_metrics` 返回 `{"applied": False}` |
| `compression_ratio` 分母为 0 (空 messages) | `ratio = 1.0`(保守: 未压 = 全保留) |
| TS-1 未启用 (`PROXY_BM25_ENABLED=false`) | `bm25_scores = {}`,不写入 |
| TS-2 `_protected_pair_indices` 返回空集 | `protected_indices = []` (不要 None) |
| 某次 `sub.compress` 是空 list | 仍写 `"sub": {"compress": []}`,不要 omit |
| 旧字段 `compression` (rounds strategy 的 "folded"/"rules") | 作为 `CompressionResult.sub.rounds_compression` 子字段,不进顶级 |

---

## 7. 测试覆盖

新增测试文件 `test/unit/test_compression_result.py`,14 个用例 (TDD stub,W3末-W4 由工程师实现填充)。

| # | 用例 | 验证 |
|---|---|---|
| 1 | `CompressionResult` TypedDict 在 Python 3.9 import 无异常 | 兼容性 |
| 2 | `compress_tool_result` 返回 dict 满足 `CompressionSubResult` 字段集 | 4.2 |
| 3 | `compress_tool_result` 含 `original_len` / `compressed_len` | 4.2 |
| 4 | `truncate_messages_if_needed` 返回 dict 满足 `CompressionResult` 必填项 | 4.3 |
| 5 | rounds 路径返回 `strategy="rounds"`, sub 字段含 `rounds_compression` | 4.3 |
| 6 | smart 路径返回 `strategy="smart"`, skipped_reason 枚举正确 (TS-2 引入值) | 4.3 |
| 7 | smart 路径成功截断: dropped_indices, kept_messages 非空 | 4.3 |
| 8 | `_compress_content_pass` 返回 `sub.compress` list 项为 `CompressionSubResult` | 4.4 |
| 9 | `_compress_content_pass` 返回 `compression_ratio` 范围 [0, 1] | I-4 |
| 10 | `clear_old_tool_results` 不改 messages 时 `messages` 字段可为 None 或等于输入 | I-5 |
| 11 | `_oom_safety_fifo` 返回 `strategy="oom_safety_fifo"`, `skipped_reason="oom_emergency"` | 5.3 |
| 12 | `admin_server._get_compression_stats` 返回 dict 有 strategy_counts + avg_compression_ratio | 4.7 |
| 13 | `admin_server._get_compression_stats` 当 metrics 文件不存在时返回 _empty_compression_stats | 4.7 |
| 14 | 兼容性: `ctx.compress_stats['clear']` 旧字段仍可访问 (双写期) | I-1 |

### 7.1 TDD stub (W3末生成)

类比 TS-1/TS-2,用 `inspect.signature` + `hasattr` 检测 `compression_types` 模块导出。

---

## 8. 风险与未决事项

| # | 事项 | 状态 | 缓解 |
|---|---|---|---|
| R-TS3-1 | 改 6 个函数返回类型波及太多调用点 | 中影响 | TypedDict total=False + 字段 >= 现有集合,纯加字段不删字段;signature 快照刷新 |
| R-TS3-2 | `output_metrics` 与 `_get_context_optimization_stats` 旧字段依赖 | 中影响 | 兼容期 4 周,双写新旧字段;W4 d3 后切换 |
| R-TS3-3 | `_protected_pair_indices` 每次返回 list 浪费,但仅 debug 时用 | 低影响 | 仅在 `PROXY_METRICS_ENABLED=true` 时填充 `protected_indices` |
| R-TS3-4 | TS-1 `bm25_scores` 与 TS-3 `bm25_scores` 的 key 类型 (msg_idx vs (msg,block)) | 待 W3末评议 | TS-3 顶层用 msg_idx 聚合,block level 进 sub.compress[i].bm25_score |
| R-TS3-5 | 行为快照 (`gen_behavior_snapshots.py`) 因字段新增需重生成 | 低影响 | 跑通即刷新 |
| R-TS3-6 | `CompressionResult.messages` 含原始 lista 可能耗 metrics 大小 | 低影响 | metrics JSONL 不序列化 messages 字段,output_metrics strip 移除 |
| R-TS3-7 | Python 3.9 TypedDict total=False 在某些 IDE 不提示字段 | 低影响 | 不影响运行;subprocess 多版本兼容性已验证 (3.8+ 支持) |

---

## 9. 实施次序

| 阶段 | 内容 | 验收 |
|---|---|---|
| W3末 d1 | 新建 `compression_types.py`: `CompressionResult` + `CompressionSubResult` 定义 | 用例 1 通过;纯类型可 import 无副作用 |
| W3末 d2 | `compress_tool_result` 加 `original_len`/`compressed_len`/`bm25_score` 字段 | 用例 2-3 通过;signature 快照刷新 |
| W4 d1 | `truncate_messages_if_needed` / `_apply_smart_truncation` / `_apply_rounds_truncation` 三路统一返回类型 | 用例 4-7 通过 |
| W4 d2 | `_compress_content_pass` / `clear_old_tool_results` 适配 | 用例 8-10 通过 |
| W4 d3 | admin `_get_compression_stats` 新函数 + `_oom_safety_fifo` 字段形式化 (无需改代码) + 兼容期双写 | 用例 11-13 通过 |
| W4 d4 | `/status` HTML 段加入 + 文档同步 + 行为快照刷新 | 用例 14 通过;`./manage.sh status` 展示 compression 段;CLAUDE.md / AGENTS.md §6.2/§7 同步 |
| W4 d5 | 全量回归 + signature + snapshot + integration | `bash test/run_tests.sh --all` (unit + integration) 全绿 |
| 后续 4 周兼容期 | 监控 `ctx.compress_stats` / `ctx.trunc_stats` 旧字段是否仍被读,无则删除 | changelog `2026-08-15-v0.6.1-litellm-borrow.md` 列入"deprecated but not removed"列表 |

---

## 10. 与 litellm 的字段映射

| litellm CompressedResult | 本设计 CompressionResult | 差异说明 |
|---|---|---|
| `messages` | `messages` | 一致 |
| `original_tokens` | `original_tokens` | 一致 |
| `compressed_tokens` | `compressed_tokens` | 一致 |
| `compression_ratio` (1 - compressed/original) | `compression_ratio` | 完全一致 (I-4) |
| `cache: Dict[str, str]` | `cache_keys: Dict[str, str]` | 本设计信息降级方案留空,改名表达"键集合"而非"cache 表" |
| `tools: List[dict]` (retrieval tool) | **省略** | 信息降级方案无 retrieval tool (ADR-LB-01) |
| `compression_skipped_reason` (NotRequired) | `skipped_reason: Optional[str]` | 一致;扩展枚举集合适配 TS-2 |
| — (无) | `protected_indices`, `dropped_indices` | 本设计独有,适配 TS-2 工具配对 |
| — (无) | `bm25_scores` | 本设计独有,适配 TS-1 BM25 评分 |
| — (无) | `strategy`, `enabled`, `skipped`, `truncated` | 本设计独有,适配本管线多 strategy 多 stage 模型 |
| — (无) | `sub` | 本设计独有,适配 `_compress_content_pass` 三段 (compress/clear/think) 结构 |

---

## 11. 与 TS-1 / TS-2 的关系

### 11.1 与 TS-2 (已交付)

TS-3 是 TS-2 的"形式化封口":
- TS-2 在 `_apply_smart_truncation` / `_oom_safety_fifo` 末尾返回 dict 时,`skipped_reason` 字段已就位 (W1-W2 已加)
- TS-3 把这些散点 dict 提升为正式类型契约,旧字段全部保留 (兼容期 4 周)
- 不影响 TS-2 已通过的 12 个 tool pair 测试

### 11.2 与 TS-1 (W3 同期推进)

TS-1 与 TS-3 是同一 sprint 的两条工作流,可在 W3末做接口对齐:
- TS-1 在 `compress_tool_result` 加 `bm25_score` kwarg,W3 d3
- TS-3 在 `compress_tool_result` 返回 dict 加 `bm25_score` 字段,W3末 d2
- 两者调用次序: TS-1 先算 score 传进函数,TS-3 在返回值里把 score 回显出来供 metrics
- 联调点: W3末 d2 实现 `bm25_score` 字段时,需 TS-1 W3 d3 已落地的调用点

---

## 12. 后续可清理项 (W4 末保留 backlog)

- W4 末保留 `ctx.compress_stats` / `ctx.trunc_stats` 双写兼容期到至少 v0.7.0;v0.7.0 prefix cache 引擎化时再清理 (避免 v0.6.1 升级用户因依赖旧字段升级失败)
- `sub.smart_reason` (TS-2 reason 字段) 兼容期同上
- `rounds_compression` 子字段 litellm 无对应,作为本设计独有遗留

---

> 本文件创建于 2026-07-05,依据 PRD-litellm-borrow §3.2 TS-3 落地设计。