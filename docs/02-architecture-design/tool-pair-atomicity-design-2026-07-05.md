# TS-2 设计: Anthropic 工具配对原子单元

> **文档版本**: v0.1 (Draft, 待 W1 评审)
> **创建日期**: 2026-07-05
> **状态**: ⏳ 待评审 — W1 (2026-07-20 ~ 07-24) 进入实施
> **关联 PRD**: [`PRD-litellm-borrow-2026-07-05`](../01-requirements-product/PRD-litellm-borrow-2026-07-05.md) §2 TS-2
> **关联模块**: `truncation.py` / `content_compressor.py` / `pipeline.py:CacheAligner` (Stage 6)
> **关联缺陷**: DEF-001 / DEF-002 / DEF-107
> **前置依赖**: M1 (v0.6.0 P0 关闭,2026-07-15)

---

## 目录

1. [背景与动机](#1-背景与动机)
2. [当前实现盘点](#2-当前实现盘点)
3. [范式转变: 从事后修复到事前预防](#3-范式转变)
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

v0.6.0 项目板 P0 中,DEF-001 (22% 500 错误) 与 DEF-002 (37% 循环注入) 已分别由 WrapperGuard 与跨请求循环追踪走 M1 热修路径。但日志复盘显示根因之一是:

> **截断/清除时切断 assistant `tool_use` 与 user `tool_result` 配对**,后端收到孤儿消息触发 OpenAI 兼容性 400 错误,模型再 Defensive Read 重试→循环注入率上升。

### 1.2 litellm 借鉴

litellm `litellm/compression/compress.py:165-205` 的 `_extract_anthropic_tool_exchange_spans` 把 assistant `tool_use` + 配对的 user `tool_result` 作为**不可分割的 keep/drop 单元**,破坏即跳过整个压缩 (`invalid_anthropic_tool_sequence`)。

### 1.3 为何现在做

- M1 WrapperGuard 是**防御性热修** (允许孤儿出现再清理)
- 本设计是**根治性替代** (不让孤儿出现)
- 两条路径不冲突,M1 关闭后 TS-2 上线,WrapperGuard 退化成纯兜底

---

## 2. 当前实现盘点

### 2.1 现有 prior art

| 函数 | 位置 | 行为 | 性质 |
|---|---|---|---|
| `_is_tool_result_message` | `truncation.py:572` | 判定消息是否含 tool_result block | 辅助 |
| `_fix_tool_pairings` | `truncation.py:890` | 收集 `tool_use.id`,删孤儿 tool_result;收集 `tool_result.tool_use_id`,删孤儿 tool_use,再 reorder | **事后修复** |
| `_reorder_tool_results` | `truncation.py:977` | 确保 tool_result 紧跟 tool_use | **事后修复** |

### 2.2 现有的截断/清除调用链

```
truncate_messages_if_needed (line 706)
  ├─ _apply_rounds_truncation     (line 1066)
  ├─ fifo 路径                    (line 780-870,内联)
  ├─ _apply_smart_truncation      (line 602)
  └─ 最后调用 _fix_tool_pairings  (事后修复)

clear_old_tool_results (line 290)
  └─ 不调用 _fix_tool_pairings (依赖外层 ContentCompressor stage 触发)
```

### 2.3 问题

- 事后修复虽能清理孤儿,但**仍存在中间态**:截断后到 `_fix_tool_pairings` 调用之间,messages 已损坏;若中间被并发观察或日志读取,容易看出"切断了的配对"作为基线。
- 事后修复会**删除已经截断的相邻消息**,产生非预期的额外 drop,`high_drop_ratio` 偏高 (DEF-107 现 21.6%);本应在第一个截断决策时就把整对一起 drop。
- 事后修复**无 bail-out**:无论配对切断多严重,都强行清理,触发 `compression_skipped_reason` 机制缺失 (TS-3 要补)。

---

## 3. 范式转变

### 3.1 从 "事后修复" 到 "事前预防"

```
现状 (v0.6.0):
  truncate → 损坏 → _fix_tool_pairings → 清理孤儿 → 额外 drop
  
TS-2 (v0.6.1):
  _find_tool_pairs → 配对索引 → truncate 在保护集内 → 无孤儿 → 无额外 drop
                                              ↓ 若无任何可删
                                          skipped_reason="invalid_anthropic_tool_sequence"
```

### 3.2 与 `_fix_tool_pairings` 共存

- TS-2 不删除 `_fix_tool_pairings` —— 它仍作为 WrapperGuard 兜底,处理 rapid-mlx 等后端异常返回的孤儿 (而非代理本身造成)
- TS-2 在 `truncate_messages_if_needed` / `clear_old_tool_results` 入口处生效,目标是不让 `_fix_tool_pairings` 出现"代理自造的孤儿"
- 日志区分:`_fix_tool_pairings` 打 `Tool pairing fix:代理自造` vs `Tool pairing fix:后端异常`;本次只新增前者统计,后者沿用现有路径

---

## 4. API 设计

### 4.1 新增 —— `_find_tool_pairs`

```python
def _find_tool_pairs(messages: List[dict]) -> List[Tuple[int, int]]:
    """识别 Assistant tool_use → User tool_result 的配对区间。
    
    返回: [(assistant_msg_idx, user_msg_idx), ...],按 assistant_idx 升序
    
    规则:
      - 仅匹配 block["id"] == block["tool_use_id"]
      - 一个 assistant 消息中多个 tool_use 分别匹配各自的 user tool_result
      - 同一 tool_use_id 多次出现 (违反协议) —— 取第一个匹配,其余标孤儿 (返回 (-1, idx) 表示)
      - 找不到配对的孤儿 tool_result/user (含 tool_result 但无 sender tool_use) → 不出现在结果中
    """
```

返回值约定:
- `(a_idx, u_idx)`:正常配对
- `(-1, u_idx)`:孤儿 tool_result (后端异常输入,不归本设计处理)
- 配对结果按 `a_idx` 升序;`a_idx == -1` 的孤儿排在最后

### 4.2 新增 —— 配对保护集

```python
def _protected_pair_indices(messages: List[dict],
                            protected_prefix_n: int) -> set:
    """返回所有不可单边截断的消息索引集合。
    
    逻辑:
      1. CacheAligner 已保护的前 protected_prefix_n 条 → 全部入集
      2. _find_tool_pairs 返回的所有 (a_idx, u_idx) 配对索引 → 全部入集
      3. 若任一索引在 protected 段内,其配对索引强制入集 (跨段配对保护)
    """
```

### 4.3 修改 —— `truncate_messages_if_needed`

```python
def truncate_messages_if_needed(messages, session_id=None, keep_rounds=None):
    protected = _protected_pair_indices(messages, PROXY_CACHE_ALIGN_PREFIX_N)
    # ...原有策略逻辑...
    # 在每个删除决策点:
    if drop_idx in protected:
        # 尝试整对 drop (a_idx 与 u_idx 都 drop)
        pair = _find_pair_for(drop_idx, pairs)
        if pair_can_drop(pair):
            drop both
        else:
            skip → skipped_reason = "invalid_anthropic_tool_sequence"
            break
```

### 4.4 修改 —— `clear_old_tool_results`

在 `clear_old_tool_results` (line 290) 内部,删除任一 tool_result 前调用 `_protected_pair_indices`,被保护的 tool_result 不允许单独删除,删除时与其 tool_use 一起删,或两者都保留。

---

## 5. 集成点

### 5.1 与 CacheAligner (Stage 6) 的关系

- CacheAligner 划出 "前 N 条 protected 段" + "其余 dynamic 段"
- TS-2 保护集 = CacheAligner protected 段 ∪ 所有配对索引 (不论位于哪段)
- 即:dynamic 段内的 tool 配对也被保护,不被单边截断
- 跨段配对 (tool_use 在 protected 段,tool_result 在 dynamic 段,或反之):两者都入保护集

### 5.2 与 ContentCompressor (Stage 10) 的关系

- `compress_tool_result` 在压缩单条 tool_result 前,先查 `_protected_pair_indices`
- 被保护的 tool_result 仍可压缩内容 (因为压缩不删除消息本身),但**不得删除** —— 这与现状一致,只是显式化

### 5.3 与 OOMSafetyFIFO (Stage 20) 的兜底关系

- OOMSafetyFIFO 是紧急 FIFO,可以打破保护集 (避免 OOM 优先一切)
- TS-2 在 OOMSafetyFIFO 之前生效;OOMSafetyFIFO 触发时记录 `skipped_reason="oom_emergency"` 强行截断
- 触发后 `_fix_tool_pairings` 仍兜底清理

---

## 6. 边界与不变式

### 6.1 不变式 (实施后必须成立)

- **I-1**: 经过 TS-2 处理后,若 `skipped_reason is None`,则 `_find_tool_pairs(messages)` 返回的所有配对中,无任一索引被单边删除
- **I-2**: 被保护集包含的索引,不被 `_apply_smart_truncation` / `_apply_rounds_truncation` / fifo 路径 / `clear_old_tool_results` 单边删除
- **I-3**: OOMSafetyFIFO 触发时,I-1/I-2 不保证 (紧急路径)

### 6.2 边界场景

| 场景 | 处理 |
|---|---|
| 同一 tool_use_id 在两条 user 消息中出现 (后端异常输入) | `_find_tool_pairs` 取第一个为配对,第二个标 `(-1, u_idx)` 孤儿 |
| 同 tool_use_id 被两个 assistant 携带 (异常) | 取第一个,孤儿 assistant tool_use 由 `_fix_tool_pairings` 兜底 |
| 跨段配对 (a 在 prefix, u 在 dynamic) | 两者都入 `_protected_pair_indices` |
| 连续多对配对 (assistant A → user U1, A → U2 同批发起) | 两对都识别,各自成对 |
| protected 段内 tool_use 没有 user tool_result (历史孤儿) | `_find_tool_pairs` 不返回此项,把它交给 WrapperGuard 兜底 |
| 整对长度超过截断预算 | 整对 drop 或整对保留;无中间态 |

---

## 7. 配置项

本设计**不新增**配置项。CacheAligner 的 `PROXY_CACHE_ALIGN_PREFIX_N` 复用;开关 `PROXY_TOOL_PAIR_ATOMIC` 默认 `true` (v0.6.1 后硬开启,无开关意义),登记到 `CONFIG_REGISTRY` 仅作为可观测标记:

| 变量 | 默认 | 类型 | 作用 |
|---|---|---|---|
| `PROXY_TOOL_PAIR_ATOMIC` | `true` | bool | 仅日志用,标识 TS-2 是否启用;M1.5 后不可关闭 |

---

## 8. 测试覆盖

新增测试文件 `test/unit/test_tool_pair_atomicity.py`,12 个用例 (TDD stub,详见同目录)。

| # | 用例 | 验证不变式 |
|---|---|---|
| 1 | 单配对单 tool_use | I-1 |
| 2 | 同 assistant 多 tool_use 配多 tool_result | I-1 |
| 3 | 孤儿 user tool_result (无 sender) | `_find_tool_pairs` 不返回 |
| 4 | 孤儿 assistant tool_use (无 result) | `_find_tool_pairs` 不返回 |
| 5 | 重复 tool_use_id (异常输入) | 取首配对,余者 (-1) |
| 6 | 跨 protected/dynamic 段配对 | I-2 |
| 7 | protected 段内孤儿 | 不归本设计处理 |
| 8 | truncate smart 路径遇配对,整对 drop | I-1 |
| 9 | truncate smart 路径遇配对,整对保留并 skipped | I-1 + skipped_reason |
| 10 | captured rounds 截断保前 N 对 | I-2 |
| 11 | clear_old_tool_results 单边删除被拒 | I-2 |
| 12 | OOMSafetyFIFO 触发打破保护集 | I-3 |

---

## 9. 风险与未决事项

| # | 事项 | 状态 |
|---|---|---|
| R-TS2-1 | `_find_tool_pairs` 对超长 session (200+ msg) 性能 | 应 ≤ 5ms,基于字符串比对;若超出需加缓存 |
| R-TS2-2 | 与 litellm `_extract_anthropic_tool_exchange_spans` 思想类似但 API 不同 (litellm 用 spans 范围,本设计用 idx 对集合) | 设计选择,不需对齐 |
| R-TS2-3 | W1 评审是否同意保留 `_fix_tool_pairings` 作为兜底 | 待评审 |
| R-TS2-4 | `skipped_reason` 字段正式接入 TS-3 `CompressionResult` 的时机 | TS-3 W3 末,W1-W2 用临时 dict 字段 |

---

## 10. 实施次序

| 阶段 | 内容 | 验收 |
|---|---|---|
| W1 d1-2 | `_find_tool_pairs` + `_protected_pair_indices` 实现 | 用例 1-5 通过 |
| W1 d3-4 | `truncate_messages_if_needed` smart 路径集成 | 用例 6-9 通过 |
| W1 d5 | 评审 | 评审纪要进 `docs/02-architecture-design/tool-pair-atomicity-review-YYYYMMDD.md` |
| W2 d1-2 | rounds / fifo 路径 + `clear_old_tool_results` | 用例 10-11 |
| W2 d3 | OOMSafetyFIFO 协调 | 用例 12 |
| W2 d4-5 | 单元测试补齐 + `--unit` 全绿;DEF-001/DEF-002 现场 5 日观察 | 公开 |

---

## 11. 附录: litellm 参考实现

```python
# litellm/compression/compress.py:165-205 (摘要)
def _extract_anthropic_tool_exchange_spans(messages):
    """Assistant tool_use + matching user tool_result 视为原子 keep/drop 单元。
    若工具序列本身已损坏,跳过整个压缩并返回 invalid_anthropic_tool_sequence。"""
    ...
```

本设计的差异:
- litellm 返回 `spans` (一对范围),本设计返回 `idx pairs` (一对索引集合) —— 因为本项目后续截断动作以单消息为单位而非范围
- litellm 跳过整个压缩,本设计只跳过当前截断决策并尝试下一可删项 —— 因为截断预算压力比压缩更刚性

---

> 本文件创建于 2026-07-05,依据 PRD-litellm-borrow §3.2 落地设计。