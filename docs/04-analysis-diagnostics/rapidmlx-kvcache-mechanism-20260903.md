# rapid-mlx KV Cache 机制调研与匹配方式

> 日期：2026-09-03 ｜ 源码：rapid-mlx 0.12.12（homebrew），`vllm_mlx/` 包
> 模块：`memory_cache.py`（MemoryAwarePrefixCache）/ `prefix_cache.py`（BlockAware，备用）/
>        `engine/batched.py`（boundary snapshot）/ `scheduler.py`（快照触发）

## 1. 架构：三层缓存 + 四条复用路径

生产用的是 `MemoryAwarePrefixCache`（`--cache-memory-mb` 上限，LRU）。每次请求的 KV 在消息边界保存为条目（entry），fetch 时按 token 序列匹配。**四条路径**：

```
请求 tokens T 到达
├─ ① exact 匹配   T == entry        → 全复用（但 non-trimmable 条目不可用——
│                                     scheduler 会重发最后一个 prompt token，
│                                     需要 N-1 边界快照）
├─ ② prefix 匹配  entry ⊂ T（严格前缀）→ 复用 entry，只 prefill 增量 remaining
│                                     ← 这是 agent 逐轮增长想走的路
├─ ③ supersequence entry ⊃ T        → 需 trim 掉 excess → non-trimmable 拒绝
└─ ④ LCP          entry 与 T 共享前缀后分叉 → 需 trim → non-trimmable 拒绝
```

## 2. hybrid non-trimmable 的确切语义（#427/#1075/#1103）

Ornith-35B（GatedDeltaNet）与 9B（dense GatedDeltaNet）的缓存层都是 `ArraysCache` —— **无 `trim` 能力**（`is_trimmable()=False`）。因此：

- **③④ 两条路径结构性拒绝**——存储条目比请求长、或与请求分叉时无法裁剪状态续算，只能全量 prefill
- **唯一可行的复用 = ② prefix 匹配**（entry 恰好是请求的严格前缀，续算不需 trim）
- 源码注释原话（`batched.py`）：“the prefix cache finds the LCP match but can't crop the Mamba state at the cut point, so a stored full-prompt+output entry is unusable for a turn-2 prompt that shares only the prefix”

## 3. 边界快照机制（让 ② 成立的关键，PR #435/#427）

引擎每轮在 `prefix_boundary` 保存快照——**渲染"不含 assistant generation marker"的位置**（含最新 user 消息）。下一轮请求 = 该边界 + 新内容，才能 ② 命中。

```
[boundary_snapshot] request=xxx saved 28816 tokens at message boundary  ← 每轮结束
[cache_fetch] HIT prompt=29843 cached=28847 remaining=996 time=0.002s  ← 下轮 prefix 命中
```

**模板的隐患**（源码明示）：若模板的 generation 渲染（`add_generation_prompt=True`，尾部如 `<think>\n`）**不是** no-generation 渲染的严格前缀——上轮的 `<think>\n` 在下一轮被真实 assistant 内容替换 → 前缀分叉 → 只能走 ④ LCP → non-trimmable 拒绝 → 全冷。

## 4. 日志现象与源码机制的对应

| 日志 | 源码路径 | 含义 |
|---|---|---|
| `HIT cached=28847 remaining=957` | ② prefix | 上一轮 boundary 快照是本轮严格前缀 → 只 prefill 增量 |
| `[boundary_snapshot] saved N` | 每轮结束保存 | 为下一轮备料 |
| `LCP unavailable: shared=14017 entry_len=14094 requested_len=15060 non_trimmable=True` | ④ LCP 拒绝 | 候选 entry（完整 store，含上轮 assistant 回复）与请求分叉；trim 不允许 → 全冷 |
| `cached 停滞 28847 多轮` | ② 但快照滞后 | 命中边界停在一个较旧的 boundary，增量每轮增长（remaining 1953→3917） |

**折叠后的重建期**（epoch 后 5-6 轮连续 unavailable）：折叠使请求变短（32780→13096）→ 所有旧 entry 变 supersequence（③拒绝）；新快照逐轮重建中，若候选选中"含 assistant 回复的完整 store"而非 boundary 快照 → 分叉 → ④拒绝。**这正是用户实测 0.7-1K tok/s 的轮次**。

## 5. 匹配本框架的正确使用方式

| 层 | 匹配方式 | 结论 |
|---|---|---|
| **请求形态** | 逐轮纯 append（永不缩短、前缀永不重排） | ✅ 唯一能稳定走 ② 的形态。代理 append-only 已做 |
| **请求缩短**（折叠/compact/截断） | 触发 ③ supersequence 全冷 | ⚠️ epoch 折叠每 ~22 轮一次可接受；**重建期滞后 5-6 轮是额外浪费（疑为候选选择偏向完整 store）** |
| **前缀分叉**（模板 suffix/工具变化/中间插入） | 触发 ④ LCP 全冷 | ⚠️ 代理应避免一切对历史前缀的插入/改写（占位符注入在尾部已做） |
| **KV 量化** | store 时压缩/fetch 解压，不影响匹配 | ✅ 4bit 已在用 |
| **响应缓存**（`--response-cache-entries`） | 完全重复请求短路整条管线 | 与逐轮增量无关（agent 不重发相同请求） |

## 6. 可行的优化（按杠杆排序）

**A. `--hybrid-cache-entries 8 → 32`（一行配置，建议立即实验）**
`hybrid_reuse_max_entries` 决定保留多少 non-trimmable 条目（默认 0=全丢，auto 8）。**更多条目 = 不同边界的快照共存**——折叠后旧 boundary 快照不立即被 LRU 挤掉，重建期命中概率提高。8→32 只增内存（条目受 cache_memory_mb 限制），零风险，值得先试。

**B. 折叠后"预热"首轮**（代理侧，~20 行）
epoch 折叠后第一轮已知必全冷——可在折叠执行后立刻向引擎发一次**零生成空请求**（同前缀 + `max_tokens=1`）让 boundary 快照落盘，之后真实轮直接 ② 命中。消除 5-6 轮重建滞后的浪费（每轮省 13-17K prefill）。

**C. 请求前缀零漂移纪律**（已是代理纪律，强化）
- 折叠占位符/实体行/召回 follow-up 一律**尾部追加**（已做）
- 禁止任何对 system/tools 区的轮间变化（CacheAligner 契约，已声明）
- **S 触发算式校准**（S=60000 实际 ~32K 触发）→ 折叠更可预期、间隔更长（每 22 轮 → 每 35-40 轮）

**D. 引擎侧上报候选**（long-term）
重建期连续多轮 unavailable 疑为 fetch 候选选择偏向"完整 store"而非 boundary 快照（boundary 快照 13081 是 14078 的严格前缀，理论上应 ② 命中却走了 ④）——值得向 rapid-mlx 报 issue（若复现路径稳定）。

**E. 模型/引擎选型（已有方向）**
9B dense GatedDeltaNet 源码确认同样 non-trimmable，但 64K 测试平坦 1.3-2.5s 证明其快照机制工作正常（且永不缩短的请求形态 + 快 prefill）；35B 的问题是折叠使请求周期缩短。**dense 纯 Transformer（如 Qwen3.6-27B？）无此约束，可走 ③④ trim 路径**——若 27B dense 质量可接受，缓存行为可能更优（待验证）。

## 7. 结论

KV cache 优化 = **让请求形态匹配 ② prefix 路径**：
1. 永不缩短、永不重排（append-only 已做，epoch 折叠是唯一例外）
2. 折叠后预热消除重建期（B）
3. 更多快照共存提高命中窗口（A）
4. 前缀零漂移纪律（C）
5. 长期：dense 模型规避 non-trimmable 约束（E）

**A+B 是本机立刻能做的两项**，预计把 gate 35B 的折叠重建期从 5-6 轮全冷压缩到 1 轮，非 epoch 命中率从 0.949 进一步向 0.99 靠。
