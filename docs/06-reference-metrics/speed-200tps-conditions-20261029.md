# 「200+ tok/s」条件定量结论（2026-10-29 本机复测）

> **一句话**：200+ tok/s 不是「更强的引擎/更好的模型」，而是**「可预测输出 + 短上下文 +
> 投机解码」这个工作点**。本机（M5 Pro 20c / 48GB，Splash 1.4.0 + OptiQ-4bit + DFlash2）
> 今天实测上限 **215.4 tok/s**，与官方 M5 Pro 标称 210 一致。
>
> 由来：用户提出「社区跑的最高性能数据达到 200+ t/s，请调研确认具体条件」→ 先做外部调研
> （官方 HF 模型卡 / unsloth / Reddit / llmcheck），再做本机 12 样本复测，把条件**定量化**。

## 一、核心定量关系（本次复测的最大收获）

```
tok/s ≈ 36 × tok/cycle
```

- **步频恒定**：12 个样本的解码步频落在 **33.9–37.2 steps/s**（thinking-on 约 36；关思考路由约 40），
  贯穿 86–215 tok/s 全程不随输出内容变化。
- **tok/cycle ＝ 每个解码步产出的 token 数 ＝ 1 + 草稿被接受数**，上限由 draft 长度 k 决定。
- 所以**唯一能动的量是 tok/cycle**；它由「输出可预测性 + 上下文长度」决定，不由引擎参数决定。

**历史 335 tok/s 的解释**：335 ÷ 36 ≈ **9.3 tok/cycle** → k=8 且**全部被接受**（10-20 记录
DFlash2 短 prompt 接受率 99%）。即 335 是该配置的**理论上限**（k=8 全接受的稳态），
不是生产可达值。

## 二、本机实测矩阵（2026-10-29 03:40–03:43，同一进程、thinking on 除注明外）

| # | 条件 | 输入 tok | 输出 tok | TTFT | **tok/s** | **tok/cycle** | 步频 |
|---|------|---------:|---------:|-----:|----------:|--------------:|-----:|
| I | 短 prompt + **数字 1..150 枚举** | 48 | 1,006 | 0.1s | **215.4** | **5.9** | 36.5 |
| J | 短 prompt + **代码生成** | 52 | 725 | 0.1s | **213.8** | **6.0** | 35.6 |
| K | 短 prompt + **重复文本 ×40** | 58 | 943 | 0.1s | **203.9** | 5.7 | 35.8 |
| G | 短 prompt + 自由摘要 | 35 | 537 | 0.1s | 185.0 | 5.1 | 36.3 |
| F | 短 prompt + 自由摘要 | 35 | 508 | 0.1s | 181.1 | 5.0 | 36.2 |
| A | 短 prompt + 自由摘要 | 34 | 430 | 0.1s | 156.0 | 4.6 | 33.9 |
| H | 1.5K prompt + 摘要 | 1,524 | 576 | 0.5s | 148.6 | 4.0 | 37.2 |
| C | 1.5K prompt + 摘要（冷） | 1,475 | 559 | 0.5s | 145.1 | 4.0 | 36.3 |
| B | 短 prompt + 摘要（1024 输出上限） | 34 | 537 | 0.1s | 140.3 | 4.0 | 35.1 |
| E | **10.8K prompt** + 摘要 | 10,835 | 573 | 4.0s | **120.1** | 3.3 | 36.4 |
| L | **关思考**（haiku 路由 `thinking off`）+ 列表 40 条 | 63 | 891 | 0.1s | **86.0** | **2.1** | 41.0 |
| — | 生产 agentic 长会话混流（10-27/10-28 历史） | 30–96K | — | 2–5s | 116–132 | — | — |

> 所有请求均带 nonce 会话头（`X-Claude-Code-Session-Id`）规避代理幂等去重；
> `cached` 均为 0（冷前缀），故 TTFT 为冷启口径。

## 三、条件清单（按决定性排序）

| # | 条件 | 阈值 | 反证（本机或社区实测） |
|---|------|------|----------------------|
| ① | **输出可预测**（代码／枚举／模板／重复） | tok/cycle ≥ 5.6 | 自由摘要 4.0–5.1 → 140–185；列表（关思考）2.1 → 86 |
| ② | **上下文短** | ≤2K | 10.8K → tok/cycle 3.3 → **120 tok/s**；社区 4K 输入 → 110 |
| ③ | **4-bit 权重 + 投机解码开**（DFlash2/MTP） | — | llmcheck 的 M5 Pro 7 次**无投机**实测 68.7–95.6 tok/s（tok/cycle≈1） |
| ④ | 硬件解码步频 | M5 Pro ≈36 steps/s | M3 Max 40c 209 / M5 Max ≈220 / M1 Max 136（本仓 10-20 同基准口径） |
| ⑤ | 度量口径 | 纯 decode、单请求 | 官方「4 路并发短 prompt **357**」是**聚合**值，不是单请求速度 |

## 四、与外部来源逐条对齐（全部可由 `tok/s ≈ 36 × tok/cycle` 解释）

| 来源 | 数值 | 口径与条件 | 我们的复现 |
|------|------|-----------|-----------|
| **Splash 官方**（HF 模型卡：M5 Pro 16c / 48GB） | **210** | SPEED-Bench coding prompts over HTTP、**1,024 输出**、reasoning on、4-bit Splash 包 + DFlash2 | **J = 213.8（代码）** 完全对上 |
| Splash 官方同表 | 2,011 prefill / 123ms 热 TTFT / 357 聚合(4 路短) / 236 聚合(4×32K) | 长上下文与并发是**聚合**口径 | 长上下文单请求掉到 120（E） |
| unsloth 文档 | **220** | llama.cpp + **MTP** 投机解码 | 同机制：草稿 + 可预测输出 |
| 社区视频/LinkedIn（M5 Max） | ~220 | 混合注意力（30×GDN + 10×full）+ M5 Max 40c | 步频更高 → 同比放大 |
| llmcheck（M5 Pro 社区 7 测） | 68.7–95.6 | **无投机**：llama.cpp 81.96 / Ollama 79.48(MTP off) / mlx-lm 77.8 / Rapid-MLX 75.66 / LM Studio 95.6 | tok/cycle≈1 → 36×(1.x) ≈ 70–95 ✔ |
| Reddit（M5 Pro Mini，**4K 输入**） | Splash 35B 4-bit **110** / MLX 8-bit 66.8 | 4K 上下文 + 自由文本 | 1.5K 已 145、10.8K 120 → 4K 约 130，同趋势 ✔ |
| 本仓历史（10-20，splash 1.3.2） | **335** 短 prompt | DFlash2 接受率 **99%** | 335÷36 ≈ 9.3 = k8 全接受（理论上限）✔ |
| 本仓历史（10-27 生产） | 116–117 | 长会话混流（热/冷轮+并发） | 同 tok/cycle 3.2–3.3 区间 ✔ |

## 五、两个反直觉但已实测的结论（重要，避免踩坑）

1. **关思考不会更快**：haiku 路由（`effort none · thinking off`）+ 列表输出实测
   **86.0 tok/s / 2.1 tok/cycle**，比 thinking-on 的自由摘要（140–185）**更慢**。
   原因：推理文本（套路化、重复度高）反而更容易被草稿命中；列表/枚举式短句则不然。
   → 「thinking 烧 token 所以慢」是伪因果，真正变量仍是接受率。
2. **`reasoning_effort: "none"` 写在请求体里无效**：引擎仍打印
   `effort absent → low · thinking on`。该字段需在**路由层** `thinking_disabled`
   （如 haiku 档）才生效；sonnet 路由当前未配。

## 六、生产要逼近 200 的杠杆（按性价比）

| 杠杆 | 预期 | 成本/归属 |
|------|------|----------|
| **让输出更可预测**（代码/结构化优先、模板化回答、少开放式散文） | tok/cycle 4→5.5 ⇒ 145→200 | 任务侧，零成本，立即可用 |
| 压缩档 `aggressive` / epoch 折叠（降上下文） | 3.3→4+ ⇒ 120→145+ | **行为类**（J6：预注册判据 + 空窗授权） |
| 更大/更匹配 draft（k↑） | 上限随 k 线性 | 引擎侧（Splash 仅支持官方 DFlash2，无调参口） |
| 换 M5 Max（40c GPU） | 步频↑ | 硬件 |

## 七、复现方法（本机）

```bash
# 条件：thinking on（默认）、短 prompt、可预测输出、1024 输出上限
curl -s -X POST http://127.0.0.1:4000/v1/messages \
  -H 'Content-Type: application/json' \
  -H "X-Claude-Code-Session-Id: bench-$(date +%s%N)" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":700,"stream":false,
       "messages":[{"role":"user","content":"只输出数字 1 到 150，每行一个，不要解释。"}]}'
# 读数：logs/llama-server.log 的 Done 行（input/cached/TTFT · tok/s · tok/cycle）
#   tok/s ÷ tok/cycle = 步频（应 ≈36）；tok/cycle ≥5.6 ⇒ 200+
```

**注意**：每次必须换 `X-Claude-Code-Session-Id`（代理有幂等去重，同文连发会 429）；
读数看引擎 `Done` 行而不要用端到端时间（后者含 TTFT 与代理开销）。

## 八、与其他文档的关系

- `docs/06-reference-metrics/BENCHMARK.md`（§质量门 v3、335 tok/s 记录）——本文件为
  **同一批测量数据的条件化解释**，不推翻其数值，只补「为什么」与「什么条件下可达」。
- `docs/06-reference-metrics/splash-model-capability-research-20261020.md`（官方基准与
  llama.cpp 对照）——本文件把其中的「短 prompt 335 / @30K 123–140」统一进 `36 × tok/cycle`。
- `docs/06-reference-metrics/memory-ledger-arbitration-20261029.md`——本复测前必须先把内存
  闸打开（当天因 kapitsa 与引擎并驻、内存 96% 导致代理 `backend_oom` 全拒，无法测量）。
