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

**D. 引擎侧上报候选**（long-term，2026-09-03 已源码定论 + 日志量化）
原疑点表述（候选选择偏向完整 store）**已被源码否定**：`memory_cache.py::_fetch_locked`
路径顺序 = exact → prefix（radix 最长严格前缀，命中即返回）→ supersequence →
LCP——prefix 严格优先，LCP 仅在无前缀匹配时执行，不存在"候选偏向"逻辑。
non-trimmable 的 supersequence/LCP 拒绝是设计行为（#1025/#1075/#1103）。

**日志量化**（42 万行历史）：LCP unavailable 5459 次中 **3356 次 shared≥10000**
（61%，每次浪费 1.2-1.6 万 tokens prefill）；窗口证据（每轮 boundary_snapshot
saved 成功 + 下轮 MISS entries=8 恒定）→ 疑点收敛为**「boundary 快照每轮都存、
下轮 fetch 时却不可见」**。嫌疑机制：store() 的 hybrid 专用 LRU（
`hybrid_reuse_max_entries`）+ `evict_prefixes` 驱逐链（存更长条目驱逐更短前缀），
长会话每轮 1 boundary + N 完整 store 超上限 → 旧 boundary 快照被逐 → 下轮只能
撞到含输出的完整 store → 分叉 U。entries=8 时期此问题最重；32 已缓解（§8.1）。

**上报前置条件**：entries=32 配置下取 1 个干净折叠窗口，逐轮对照
`boundary_snapshot(saved N)` 与下轮 `cache_fetch(HIT/MISS + entries 数)`——
若快照 saved 成功且前缀一致仍连续 U≥3 → 分支 B（真 bug）值得上报；若快照被
LRU 挤掉 → 设计行为，issue 建议价值仅"boundary 快照保底不逐"（低优先）。

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


## 8. 实验验证记录（2026-09-03）

### 8.1 A: hybrid-cache-entries 8→32 ✅（保留）
gate 80 轮对照(35B): 全冷轮 11→9/80, 重建期最长段 6连→5连,
首次折叠后恢复 3连→1连(turn35→36 hit=0.92), 命中率 0.949→0.952。
已入生产 conf。

### 8.2 gpu-mem 0.80 实验 ❌（回退）
假设: 提高 Metal cap 28.1→32GB 减少驱逐。
结果(双引擎下): 非epoch P90 10.3→18.7s, 命中 0.952→0.929 —— 变差。
根因: 9B 引擎(8084)共享 Metal, 0.80 更大分配在物理竞争下引发更频繁驱逐。
**驱逐根因确认**: `[prefix-pressure-evict] under Metal pressure
(metal_cap=28.1GB, cache_max=7.7GB)` —— 35B 权重 20GB + cache 7.7GB
逼近 cap, 快照被逐导致重建期拉长。单引擎独占时才可上探 gpu-mem。

### 8.3 B(折叠后预热轮) 重新评估
驱逐环境下预热轮无效——快照存了也会被逐。治本依赖:
① 单引擎独占 + gpu-mem 上探(9B 停跑时) ② 或减小快照尺寸(K 更小/更频繁折叠)
暂缓, 标记依赖单引擎环境。


### 8.4 C: S 触发算式校准 ✅（影子验证，S=110000 建议值）

**偏差确认**: est×1.6 系数源自 2026-08-22 旧形态(82346/51248)。当前 Qwen 模板
+ tool 结构下 JSON 开销使 est 高估真实 ~17%, 叠加 ×1.6 放大 → 实际触发点 =
标称 S×0.537(实测 S=60000 → 真实 32K 触发, 窗口仅填 1/4)。

**校准验证** (S=110000, gate 80 轮):
| 指标 | S=60000 | S=110000 |
|---|---|---|
| 全冷轮 | 8-9/80 | **2/80** (启动+1次折叠) |
| 折叠间隔 | ~22 轮 | **62 轮** |
| 命中率 p50 | 0.952 | **0.959** |
| 窗口峰值 | ~33K | **60,175 tokens**(设计目标) |

**校准公式**: S_conf = S_desired_real_tokens / 0.537
生产 ctx_engine 启用建议: PROXY_CTX_EPOCH_TRIGGER_TOKENS=110000(真实 59K 触发)。

**注**: EST_REAL_RATIO=1.6 在代码中仍为旧值——精确重测需 est 落盘,
暂以 S 校准公式替代(影响面更小)。

### 9. 官方 mlx_lm 对照实测（2026-09-03，回答「官方 35B 4bit + mlx_lm 是否可作对照后端」）

**背景**：评估三条理由判官方 mlx_lm 切换无价值（#980 hybrid prefix cache broken /
#1178 server 无 prompt-cache-file / #1293 工具调用空壳）。实测直接推翻前两条、
部分推翻第三条——作为 rapid-mlx 的缓存命中机制 baseline，官方 mlx_lm 可用。

**环境**：rapid-mlx 0.12.12 捆绑 mlx-lm 0.31.3（独立 python -m mlx_lm.server，
本地快照 ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit，model_type=qwen3_5_moe，无 MTP）。
注意其 /v1/models 注册了 hub 全部缓存 repo 并按请求 model id 按需加载——之前
404 的 `mlx-community/Qwen3.6-35B-A3B-MTP-4bit` 是缓存里真实存在的 repo（其
config model_type=qwen3_5_mtp，0.31.3 无该模块 → not supported），与默认模型无关。

**实测 1 — 增量轮缓存命中（回应 #980）**：
| 轮 | 内容 | wall | cached_tokens |
|---|---|---|---|
| r0 | 5,197 tok（4K 固定前缀首轮） | 7.5s | — |
| r1 | 与 r0 完全相同 | **0.3s** | — |
| r2 | 前缀 +47 tok 历史追加 | 0.4s | — |
| r3 | 再 +43 tok | 0.4s | — |
| A1 | 1,687 tok（同前缀二次） | 0.61s | **1,677**（99.4%） |
| A2 | 与 A1 相同 | 0.36s | **1,686**（99.9%） |

→ mlx_lm 0.31.3 对 qwen3_5_moe（hybrid GDN）的跨请求前缀缓存**有效**，
`usage.prompt_tokens_details.cached_tokens` 可观测。与 rapid-mlx 无代差（同为近
全命中）。#980「hybrid silent 全量重算」至少在本版本/本模型不成立。

**实测 2 — 工具调用（回应 #1293）**：带 get_weather tools 的请求正常返回
`tool_calls: [{function:{name:"get_weather", arguments:{"city":"北京"}}, id 完整}]`，
77 completion tokens。0.31.3 对 Qwen3.5/3.6 模板的工具解析已工作（#1293 已修复）。
→ #1293 推断不成立。

**实测 3 — reasoning 处理（未被评估覆盖、实测暴露的真实差异）**：该模型 thinking
默认开（generation_config），mlx_lm 把 reasoning 输出到非标准 `message.reasoning`
字段，`message.content` 只剩空白——无 thinking off 开关、无 reasoning parser。
对照：rapid-mlx 魔改层有 `RAPID_MLX_REASONING_PARSER=qwen3` + `--no-thinking`。

**结论**：官方 mlx_lm 在**纯缓存命中指标上可作对照后端**（cached_tokens 直接可比），
但两条真实限制使其无法跑代理生产链路：① 无 thinking off / reasoning parser →
content 空壳，代理拿不到工具语义所需文本；② tools 走裸 OpenAI 格式与 rapid-mlx
qwen3_coder_xml 路径不同。对照价值限于「前缀命中率/TTFT」单一维度；工具轮序列
对照不可行。rapid-mlx 的差异价值在 reasoning/tool parser 补丁与 Metal 内存管理，
不在前缀缓存机制。

### 8.5 ctx_engine 生产验证 gate 80 轮 ✅（2026-09-03 白天，ornith-oq4e + entries 32）

**结论：§8.4 影子验证的生产复现，G1-G4 全通过，S 校准公式实锤。**

| 门禁 | 指标 | 结果 |
|---|---|---|
| G1 | 非 epoch 轮 dur P90 | **10.9s** < 15s ✅ |
| G2 | epoch 轮 dur max | **23.6s** < 60s ✅ |
| G3 | 断连/错误 | 0/80 ✅ |
| G4 | 折叠后首非 epoch 轮 hit | **0.930** > 0.8 ✅ |
| 命中 | 非 epoch p50 | **0.953**（影子 0.959） |
| 折叠 | 触发点 | **turn 63，真实 59,910 tokens**（S=110000 est → ×0.537 公式预测 59K ✓） |
| 窗口 | 峰值 prompt | **59,910 tokens**（设计 60,175 的 99.6%） |
| 全冷轮 | — | 仅 [1, 63]（启动首轮 + 折叠轮本身，设计预期） |

**踩坑记录（同轮验证的方法论教训）**：
1. **会话 key = X-Claude-Code-Session-Id[:8]**（session_ledger D5）——gate 默认
   session `gate-ce-<ts>` 前 8 字符恒为 `gate-ce-` → 跨 run 共享 ctx_engine
   canonical → 首轮继承上轮状态（4815→18678 tokens）。工具脚本需 8 字符唯一
   session。
2. **智能路由会切走本地验证流量**：gate model=claude-sonnet-4-6 → 目录 routes
   cloud_model 首位 glm-5.3-flash-cn（zhipu 订阅零边际）+ `session_already_cloud`
   粘性 → 会话某轮判 cloud 后整会话锁云端（usage 转 Anthropic 语义：
   input=增量 vs 本地全量 → hit>1 假象 + 数据作废）。**本地引擎验证必须带
   `X-Proxy-Route-To: local` 头**（tools/gate_test_ctx_engine.py 已加）。
3. hbe 探针（SAMPLE_EVERY=2）在 gate 期间给后端加 50% 请求流量，验证窗口临时
   关闭（PROXY_HBE_ENABLED=false → reload），验证后已恢复 true。
