# TS-1 设计: BM25 评分驱动压缩决策

> **文档版本**: v0.1 (Draft, 待 W3 评审)
> **创建日期**: 2026-07-05
> **状态**: ⏳ 待评审 — W3 (2026-08-03 ~ 08-07) 进入实施
> **关联 PRD**: [`PRD-litellm-borrow-2026-07-05`](../01-requirements-product/PRD-litellm-borrow-2026-07-05.md) §2 TS-1
> **关联模块**: `content_compressor.py` / `truncation.py:_compress_content_pass` / `pipeline.py:ContentCompressor` (Stage 7) / `proxy_state.py` / `proxy_config.py`
> **关联缺陷**: DEF-103 (Cleared Compression 触发率低) / DEF-107 (high_drop_ratio 21.6%)
> **前置依赖**: TS-2 (W1-W2 已交付, `_protected_pair_indices` 可复用)

---

## 目录

1. [背景与动机](#1-背景与动机)
2. [当前实现盘点](#2-当前实现盘点)
3. [范式转变: 从规则触发到相关性触发](#3-范式转变)
4. [API 设计](#4-api-设计)
5. [集成点](#5-集成点)
6. [边界与不变式](#6-边界与不变式)
7. [配置项](#7-配置项)
8. [测试覆盖](#8-测试覆盖)
9. [风险与未决事项](#9-风险与未决事项)
10. [实施次序](#10-实施次序)

---

## 1. 背景与动机

### 1.1 现状观察

v0.6.0 DEF-103 (Cleared Compression 触发率低) 与 DEF-107 (high_drop_ratio 21.6%) 共同根因:

> 当前 `compress_tool_result` 对**所有** tool_result 一视同仁触发压缩,无相关性判断。与最新用户意图无关的旧 tool_result 消耗了大量压缩预算,真正相关的 tool_result 反而被压得太狠;更糟的是,当 budget 紧张时所有 tool_result 被一同 drop (DEF-107 的 21.6% 丢失率)。

### 1.2 litellm 借鉴

litellm `litellm/compression/scoring/bm25.py:34` 的 `bm25_score_messages` 用 Okapi BM25 (k1=1.5, b=0.75) 对每条消息评分,query 取最后一条 user message。在此基础上还做 4-char 前缀展开模拟 stemming。纯 Python 零依赖。

### 1.3 为何现在做

- TS-2 (W1-W2 已交付) 提供了 `_protected_pair_indices`——可复用于"BM25 评分只跑在 dynamic 段"
- DEF-103/107 当前 M1 缓解率不达标 (压缩触发率 ~30% vs 验收线 ≥ 60%),M1.5 TS-1 是替代路径
- 纯标准库可写,BM25 还可跨请求复用 IDF (进程内字典),无运行时依赖

---

## 2. 当前实现盘点

### 2.1 现有 prior art

| 函数 | 位置 | 行为 | 性质 |
|---|---|---|---|
| `compress_tool_result` | `content_compressor.py:218` | 按 mime_hint / 启发式识别 json/code/log/text,选对应 `_sieve_*` 压缩函数;返回 `{original, compressed, content_type, strategy, audit_pass, ratio}` | 逐条 tool_result 处理 |
| `_compress_content_pass` | `truncation.py:17` | 遍历所有 tool_result,凡 `frozen_head` 之外的都调 `compress_tool_result` 压缩 | 一视同仁,无相关性 |
| `ContentCompressor` | `pipeline.py:967` (Stage 7) | 调 `_compress_content_pass(cache_dynamic, ...)`,`cache_dynamic` 已被 CacheAligner 切走 protected 段 | 入口 |
| `_detect_content_type` | `content_compressor.py:21` | 启发式判 json/code/log/text | 按内容类型,与 query 无关 |

### 2.2 现有阈值路径

```
compress_tool_result(content, threshold=PROXY_COMPRESS_THRESHOLD=4096)
  ├─ len(original) < threshold → 不压 (strategy="none")
  └─ len(original) >= threshold →  按 content_type 选策略
       ├─ json → _sieve_json
       ├─ code → _compress_code
       ├─ log  → _compress_log
       └─ text → _compress_text (head/tail truncate)
```

### 2.3 问题

1. **无相关性**: 旧 tool_result 与最新 user message 关系强弱不分,被一刀切压到相同 ratio
2. **触发率低**: 多数 tool_result < 4096 字符 (`PROXY_COMPRESS_THRESHOLD`),直接 return 未压 (DEF-103 数据: ~70% 跳过)
3. **预算刚性**: `_compress_content_pass` 对每条 tool_result 独立施加压缩预算,无"低分优先压"与"高分保留"的概念
4. **无 query 信号**: 完全没有抽取最新 user message 作为 query 做相关性匹配

---

## 3. 范式转变

### 3.1 从 "按内容类型一刀切" 到 "按相关性差异化"

```
现状 (v0.6.0):
  for each tool_result (dynamic 段):
      if len < threshold: skip
      else: compress by content_type (固定策略与压缩比)

TS-1 (v0.6.1):
  query = extract_last_user_message(messages)
  scores = {idx: bm25(msg, query) for idx in dynamic tool_results}
  for each tool_result in dynamic 段 (按 score 升序):
      if score < DROP_THRESHOLD: 压缩到原长 30%
      elif score < KEEP_THRESHOLD: 按 content_type 规则压 (现有路径)
      else: 不压 (即使长也保留,因为高相关)
```

### 3.2 与现有规则压缩的关系

- **保留** `_sieve_json` / `_compress_code` / `_compress_log` / `_compress_text` 作为**手段**
- **改变** 的是"何时压、压多狠": 由 BM25 分数决定
- **新增** "低分压狠 / 高分不压" 的差异化策略
- **不变** threshold 触发门 (`PROXY_COMPRESS_THRESHOLD=4096`) 仍作用——只是对低分项可适度下调 threshold (例如 2048) 以扩大触发面

---

## 4. API 设计

### 4.1 新增 —— `bm25_score_message`

```python
def bm25_score_message(msg: dict, query: str,
                       idf_map: Optional[Dict[str, float]] = None,
                       k1: float = 1.5, b: float = 0.75,
                       min_prefix: int = 4) -> float:
    """对单条消息内容计算 Okapi BM25 分数 (相对 query)。
    
    Args:
      msg: Anthropic 格式消息 (role + content list/text)
      query: 当前用户意图文本 (通常最后一条 user message)
      idf_map: 词 → IDF 值字典; None 时按 message 集合估算 (见 §4.3)
      k1, b: Okapi 参数 (默认 1.5/0.75, 与 litellm 一致)
      min_prefix: 前缀展开最小字符数 (默认 4, litellm 一致)
    
    Returns:
      float BM25 分数; 越高越相关。空 msg/query 返回 0.0。
    """
```

### 4.2 新增 —— query 提取

```python
def _extract_last_user_text(messages: List[dict]) -> str:
    """返回最后一条 user 消息的纯文本 (拼接所有 text block)。
    
    无 user 消息或 content 为空 → 返回空串。
    tool_result-only 的 user 消息 (无 text block) → 继续往前找。
    """
```

约束: 不取 assistant 输出 (避免受 rapid-mlx 输出抖动影响),与 litellm 一致。

### 4.3 新增 —— IDF 维护

```python
# 模块级进程内字典
_BM25_IDF_MAP: Dict[str, float] = {}
_BM25_IDF_DOC_FREQ: Dict[str, int] = {}   # 词 → 出现过的 message 数
_BM25_IDF_TOTAL_DOCS: int = 0

def _update_idf(messages: List[dict], min_prefix: int = 4) -> None:
    """增量更新 IDF: 这次请求所有 message 视为一个 doc 集合。
    
    LRU 上限: _BM25_IDF_MAP 超 10000 项时清低频项 (按 _BM25_IDF_DOC_FREQ 升序)。
    """

def _bm25_idf(token: str, min_prefix: int = 4) -> float:
    """返回 token 的 IDF; 未登录词用前缀展开查最相近的已登录词。"""
```

约束: IDF 字典模块级进程内,跨请求稳定,`./manage.sh reload` 保留 (因为是 Python 进程内字典,不落盘)。

### 4.4 新增 —— tokenize

```python
_BM25_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]")

def _bm25_tokenize(text: str, min_prefix: int = 4) -> List[str]:
    """分词: 英文走标识符规则,中文单字切分 (与 litellm 不同,处理中文)。
    
    小写归一化。
    前缀展开: 对每个长度 >= min_prefix 的英文 token,额外添加其前 min_prefix 字符
      作为变形词匹配键 (e.g. "compressing" → "comp" 也算一次匹配)。
    """
```

### 4.5 修改 —— `compress_tool_result` 集成

**不改变 `compress_tool_result` 自身签名**——它仍是逐条压缩函数。改为在 `_compress_content_pass` 调用前由调用方传入相关性参数:

```python
# content_compressor.py:218 新增可选参数
def compress_tool_result(content, mime_hint=None, threshold=None, mode=None,
                         bm25_score=None,            # 新增
                         bm25_drop_threshold=None,   # 新增
                         bm25_keep_threshold=None):  # 新增
    ...
    # 若 bm25_score < bm25_drop_threshold → 强制压到 30% 原长
    # 若 bm25_score >= bm25_keep_threshold → 不压 (即使 len >= threshold)
    # 否则走现有路径
```

### 4.6 修改 —— `_compress_content_pass` 集成

在 `truncation.py:17` 的 Phase 1b 之前加:

```python
# Phase 1a: BM25 query + 评分 (W3 新增)
if _ps.PROXY_BM25_ENABLED:
    query = cc._extract_last_user_text(messages)
    cc._update_idf(messages)
    scores = {}
    for msg_idx, block_idx in all_tool_result_indices:
        if frozen_head > 0 and msg_idx < frozen_head:
            continue
        # 受 TS-2 保护: protected_pair_indices 的索引不压 (整对保留,见 §6.1)
        if msg_idx in protected_pairs:
            continue
        block = messages[msg_idx]["content"][block_idx]
        # 对 tool_result 的 content 跑 BM25 (而非整个 msg)
        text = _block_text(block)
        scores[(msg_idx, block_idx)] = cc.bm25_score_message(
            {"role": "user", "content": [{"type": "text", "text": text}]},
            query=query)
else:
    scores = {}  # BM25 不启用时走原路径
```

然后 Phase 1b 改为按 scores 升序遍历,低分优先压:

```python
# 按 BM25 score 升序排 (低分先压)
if scores:
    ordered_indices = sorted(all_tool_result_indices, key=lambda i: scores.get(i, 0.0))
else:
    ordered_indices = all_tool_result_indices  # 原顺序

for msg_idx, block_idx in ordered_indices:
    if frozen_head > 0 and msg_idx < frozen_head:
        continue
    ...
    result = compress_tool_result(
        content, mime_hint=mime_hint,
        bm25_score=scores.get((msg_idx, block_idx)),
        bm25_drop_threshold=_ps.PROXY_BM25_DROP_THRESHOLD,
        bm25_keep_threshold=_ps.PROXY_BM25_KEEP_THRESHOLD,
    )
```

---

## 5. 集成点

### 5.1 与 CacheAligner (Stage 6) 的关系

- `ContentCompressor` 拿到的 `cache_dynamic` 已剔除 protected_prefix (前 `PROXY_CACHE_ALIGN_HEAD` 条)
- TS-1 BM25 评分**只对 dynamic 段内的 tool_result 跑**——protected 段不参与
- 跨段配对保护: 若某 tool_result 在 protected 段但其 tool_use 在 dynamic 段 (罕见),TS-2 的 `_protected_pair_indices` 会标它为不可压——TS-1 自动尊重

### 5.2 与 TS-2 `_protected_pair_indices` 的关系

- TS-2保护集 = CacheAligner 段 ∪ 配对索引
- TS-1 在保护集外的 dynamic tool_result 上做评分
- **不变式**: TS-1 永不在 protected_pair_indices 包含的索引上压

### 5.3 与 _compress_content_pass 的现有 clearing 逻辑

- `_compress_content_pass` 的 Phase 2a (tool-result clearing) 维持不变——它处理"全删"而非"压狠"
- TS-1 影响 Phase 1b (语义压缩),不影响 Phase 2a
- 但 Phase 2a 的 `clear_old_tool_results` 也应尊重 BM25 分数: 低分优先 clear (在 W3 d4 加,小改动)

### 5.4 与 cloud 模式的关系

- `ContentCompressor.should_run` 已 cloud 跳过 (line 977)。BM25 评分也只在 local 路径生效
- `PROXY_BM25_ENABLED` 默认 `true` (local) / `false` (cloud)

---

## 6. 边界与不变式

### 6.1 不变式

- **I-1**: `PROXY_BM25_ENABLED=false` 时,`_compress_content_pass` 行为与现状完全一致 (无回归)
- **I-2**: BM25 评分不修改 messages,只读;评分在 `_compress_content_pass` 内部一次性计算,不跨请求存储 score (跨请求存储的只有 IDF)
- **I-3**: protected_pair_indices 包含的索引不进入评分集合 (即不被压)
- **I-4**: query 取自最后一条 user text block,不计入 assistant 输出;无 user 时全走保底 (保所有原阈值)
- **I-5**: 若 query 为空 (无 user text block),BM25 评分全 0,所有 tool_result 走现有规则路径 (保底)
- **I-6**: IDF 字典 LRU 上限 10000 项,超出时清低频 (按 doc_freq 升序)

### 6.2 边界场景

| 场景 | 处理 |
|---|---|
| 空消息列表 | `bm25_score_message` 返回 0;`_extract_last_user_text` 返回 `""`;全走保底 |
| 全中文 query | 中文单字切分,IDF 用单字统计;前缀展开对中文跳过 (中文不需 stemming) |
| 中英文混合 query | 两套 token 同时进 IDF;分数加权对长度归一化后稳定 |
| tool_result 内只有图片 block | `_block_text` 抽不出文本 → score = 0 → 走保底 |
| 长 tool_result 但 BM25 高分 | 不压 (即使远超 threshold),保住相关上下文 |
| 短 tool_result 但 BM25 低分 | 不压 (因为 len < threshold,与现状一致;BM25 不强压短内容) |
| LRU 清理清掉了正在用的词 | 不可能——清理按 doc_freq 升序,某词在本次 query 中使用时其计数刚被增过,doc_freq ≥ 1,LRU 不清 |

---

## 7. 配置项

### 7.1 新增 (W3 d3 落地到 `proxy_state.py` + `proxy_config.py`)

| 变量 | 默认 (local / cloud) | 类型 | 作用 |
|---|---|---|---|
| `PROXY_BM25_ENABLED` | `true` / `false` | bool | BM25 评分总开关 |
| `PROXY_BM25_K1` | `1.5` / `1.5` | float | Okapi k1 (词频饱和) |
| `PROXY_BM25_B` | `0.75` / `0.75` | float | Okapi b (长度归一) |
| `PROXY_BM25_KEEP_THRESHOLD` | `3.5` / `3.5` | float | 分数 ≥ 此值不压 (即使长) |
| `PROXY_BM25_DROP_THRESHOLD` | `0.5` / `0.5` | float | 分数 < 此值压到 30% 原长 |
| `PROXY_BM25_MIN_PREFIX` | `4` / `4` | int | 前缀展开最小字符数 |
| `PROXY_BM25_IDF_LRU_MAX` | `10000` / `10000` | int | IDF 字典 LRU 上限 |

### 7.2 `manage.sh` 顶部

加同名 KEY="value" 默认值,与现有 PROXY_COMPRESS_* 并列。

### 7.3 `proxy_state.py:_RELOAD_SPEC` 与 `__all__`

7 项全部进 `_RELOAD_SPEC` (支持热重载) 与 `__all__`。`CONFIG_REGISTRY` 注册。

### 7.4 文档同步

按 AGENTS.md §9 检查清单:
- `CLAUDE.md` §6.2 / §7 同步新增 6 项 (LRU 上限可选)
- `AGENTS.md` §3.1 core files / §6.2 同步
- `docs/02-architecture-design/proxy-context-window-design.md` 更新压缩策略段

---

## 8. 测试覆盖

新增测试文件 `test/unit/test_bm25_scoring.py`,18 个用例 (W3 d5 由工程师实现填充)。

| # | 用例 | 验证 |
|---|---|---|
| 1 | `bm25_score_message` 空消息返回 0.0 | 边界 |
| 2 | `bm25_score_message` 空 query 返回 0.0 | I-5 |
| 3 | 完全匹配 (query == msg text) 高分 | 核心算法 |
| 4 | 完全不匹配 (无共同 token) 0 分 | 核心算法 |
| 5 | 部分匹配 (1/4 共同 token) 中分 | 核心算法 |
| 6 | 长 msg 但相同 query 比短 msg 分低 (b 归一) | b 参数 |
| 7 | 高频 token IDF 低 (饱和效应) | IDF |
| 8 | 低频 token IDF 高 | IDF |
| 9 | 未登录词返回默认 IDF (无膨胀) | 边界 |
| 10 | 前缀展开匹配 `compressing` ↔ `compressed` (共享 4-char 前缀) | 前缀展开 |
| 11 | 中文单字切分: "压缩算法" query 匹配 "压缩" 内容 | 中文 |
| 12 | 中英混合 query: "BM25 压缩" 匹配 "compression 压缩" | 混合 |
| 13 | `_extract_last_user_text` 无 user 返回 "" | 边界 |
| 14 | `_extract_last_user_text` 最后 user 只有 tool_result 时向前找 | F-4 |
| 15 | `_extract_last_user_text` 全 user 都只有 tool_result → "" | 边界 |
| 16 | `compress_tool_result` bm25_score=0.3 时强制压到 30% | 集成 |
| 17 | `compress_tool_result` bm25_score=4.0 时不压 (即使长 5000) | 集成 |
| 18 | `compress_tool_result` bm25_score=None → 现有路径不变 | I-1 |

### 8.1 TDD stub 文件 (待 W3 d1 生成)

类比 `test/unit/test_tool_pair_atomicity.py`,用 `inspect.signature` 检测 `compress_tool_result` 新增 kwarg 存在,未实现前全 skip。

---

## 9. 风险与未决事项

| # | 事项 | 状态 | 缓解 |
|---|---|---|---|
| R-TS1-1 | IDF 跨请求维护内存膨胀 | 已缓解 | LRU 上限 10000 项,清理低频 |
| R-TS1-2 | 跨语言 BM25 评分偏差 (中英混合) | 待 W3 验证 | 中文单字 + 英文标识符 + 前缀展开各算各的; W3 加 3 用例覆盖 |
| R-TS1-3 | 长 session (200+ tool_result) BM25 计算性能 | 应 ≤ 5ms / 100 条 | 字典查找; 真测后超时则加 `_BM25_SCORE_CACHE` by (msg_idx, query_hash) |
| R-TS1-4 | rapid-mlx 输出抖动不影响 (query 用 user) | 已缓解 | 设计上选 user,不选 assistant |
| R-TS1-5 | protected_pair_indices 与 BM25 的协作点 | 已缓解 | `_compress_content_pass` Phase 1a 调用 TS-2 helper |
| R-TS1-6 | `_update_idf` 每请求跑一次是否过频 | 待 W3 评议 | 增量更新,只算新出现的 token;可优化为每 N 次跑一次 |
| R-TS1-7 | 与 TS-3 的集成时机 | 待 W4 | TS-1 返回 dict 与 TS-3 `CompressionResult` 适配 (TS-3 W3末-W4 接管) |
| R-TS1-8 | 默认阈值 (KEEP 3.5, DROP 0.5) 是否合理 | 待 W3 现场 A/B | PRD §6.2 W3 末退出条件是"触发率 ≥ 60%",阈值不达标则调参 |

---

## 10. 实施次序

| 阶段 | 内容 | 验收 |
|---|---|---|
| W3 d1 | `_bm25_tokenizer` + `bm25_score_message` + `_bm25_idf` 实现 | 用例 1-5 通过 |
| W3 d2 | 前缀展开 + 中文切分 + IDF LRU | 用例 6-12 通过 |
| W3 d3 | `compress_tool_result` 新增 3 kwarg + 集成评分路径 | 用例 16-18 通过 |
| W3 d4 | `_extract_last_user_text` + `_compress_content_pass` Phase 1a 集成; `clear_old_tool_results` 低分优先 | 用例 13-15 通过 |
| W3 d5 | `CONFIG_REGISTRY` + `manage.sh` + `proxy_state._RELOAD_SPEC` + 18 单元测试全绿 | `./manage.sh reload` 热切换 `PROXY_BM25_ENABLED` 生效; `--unit` 通过 |
| W3 Review | design 校正 (与 TS-2 类似的偏差,记录到 review 文档) | 评审纪要 `docs/02-architecture-design/bm25-scoring-review-YYYYMMDD.md` |
| 留 W4 | TS-3 集成 (TS-1 返回 dict → `CompressionResult`) | TS-3 闭 |

---

## 11. 附录: litellm 参考实现要点

| 概念 | litellm 位置 | TS-1 借鉴 |
|---|---|---|
| `bm25_score_messages` (k1=1.5, b=0.75) | `litellm/compression/scoring/bm25.py:34` | 同参数 |
| 前缀展开 (min-prefix=4) | `litellm/compression/scoring/bm25.py` | 保留该启发式 |
| query 取最后一条 user message | `litellm/compression/compress.py:127` | 完全一致 |
| embedding scorer (cosine sim) | `litellm/compression/scoring/embedding_scorer.py:47` | **不借鉴** (stdlib only 约束 + 无外部计费) |
| IDF 维护跨请求 | litellm 用 `DualCache` (含 Redis) | 用进程内字典 + LRU (无 Redis) |
| 中文 tokenize | litellm 不特殊处理 | TS-1 增加单字切分 `[\u4e00-\u9fff]` |

---

## 12. 与 TS-2 / TS-3 的关系

### 12.1 与 TS-2 (W1-W2 已交付)

- TS-2 提供 `_protected_pair_indices`——TS-1 在 Phase 1a 调用它,protected 集内的 tool_result 不评分不压
- 实测无 TS-2 时,TS-1 也能独立运行 (会越过 protected 强压),但**前缀字节稳定**保证失效——故 TS-1 严格依赖 TS-2 先落地
- TS-2 的 `_find_tool_pairs` 返回 (a_idx, u_idx) 配对——TS-1 不需要配对信息,只需要 set 索引集合

### 12.2 与 TS-3 (W3末-W4 待启动)

- TS-1 返回 `{score, original, compressed, ...}` 字典字段
- TS-3 闭会把这些 dict 标准化为 `CompressionResult` TypedDict
- W3 d1-5 的 dict 返回结构尽量贴近 TS-3 字段,降低 W4 适配成本

---

> 本文件创建于 2026-07-05,依据 PRD-litellm-borrow §3.2 TS-1 落地设计。