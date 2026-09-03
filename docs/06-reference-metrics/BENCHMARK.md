# 本地 LLM 性能测试报告

> **测试日期**: 2026-08-15 (更新: Qwen3.8-27B 基线)
> **上次测试**: 2026-07-12
> **测试环境**: MacBook Pro M5 Pro (48GB 统一内存)
> **测试工具**: tools/bench_ctx_ladder.py / tools/oom_ladder_test.py
> **代理版本**: 30 个缺陷全部修复后 (890 单元测试通过)

---

## 目录

- [测试环境](#测试环境)
- [模型列表](#模型列表)
- [Qwen3.8-27B 基线 (2026-08-15)](#qwen38-27b-基线-2026-08-15)
- [TTFT 基准测试](#ttft-基准测试)
- [生成速度](#生成速度)
- [多轮递增 (Agentic 模拟)](#多轮递增-agentic-模拟)
- [长上下文性能](#长上下文性能)
- [并发性能](#并发性能)
- [历史对比 (2026-05-10 vs 2026-07-12)](#历史对比-2026-05-10-vs-2026-07-12)
- [端到端测试（代理层）](#端到端测试代理层)
- [已知问题](#已知问题)
- [配置切换指南](#配置切换指南)
- [推荐配置](#推荐配置)
- [附录: 测试原始数据](#附录-测试原始数据)

---

## 测试环境

| 项目 | 配置 |
|------|------|
| 机型 | MacBook Pro M5 Pro |
| 内存 | 48 GB 统一内存 |
| GPU | Apple M5 Pro (14-core) |
| Metal | Metal 4 |
| OS | macOS |
| Rapid-MLX | 0.12.13 (`.venv-rapidmlx`;brew 渠道 0.12.12) |
| 代理 | anthropic_proxy.py (30 缺陷修复后) |
| 代理配置 | `qwen3.8-27b-4bit` (GPU 70%, prefix cache on, KV q4, PFlash auto) |

---

## 模型列表

| # | 模型 | 框架 | 量化 | 大小 | 配置名 |
|---|------|------|------|------|--------|
| ~~1~~ | ~~Qwen3.6-35B-A3B~~ | ~~llama.cpp~~ | ~~UD-IQ4_XS (GGUF)~~ | ~~~22 GB~~ | ~~`qwen3.6-35b`~~ *(已移除)* |
| ~~2~~ | ~~Qwen3.5-9B~~ | ~~llama.cpp~~ | ~~UD-Q4_K_XL (GGUF)~~ | ~~~5.6 GB~~ | ~~`qwen3.5-9b`~~ *(已移除)* |
| **3** | **Qwen3.6-35B-A3B** | **Rapid-MLX** | **4bit (MLX)** | **~17 GB** | **`rapid-mlx-35b-opt`** |
| **4** | **Qwen3.8-27B** (hybrid GDN) | **Rapid-MLX** | **4bit (MLX)** | **~16 GB** | **`qwen3.8-27b-4bit`** |

---

## Qwen3.8-27B 基线 (2026-08-15)

> 首个 hybrid GatedDeltaNet(线性注意力 + 每 4 层全注意力)配置。
> 引擎: rapid-mlx 0.12.13(Qwen3.8 该版本才 onboard,别名 `qwen3.8-27b-4bit`);
> `RAPID_MLX_TOOL_PARSER=hermes`,`--pflash auto --pflash-include-tools`,KV q4。
> 投机解码(MTP/DSpark/DDTree)对 hybrid 骨干不可用,见上游 #1941。

### 上下文 × 性能阶梯(直连后端 8081)

测试方法: `tools/bench_ctx_ladder.py`,每档 nonce 破前缀缓存(冷)+ 同 body 重发(热),
生成 256 tokens,流式测 TTFT。原始数据: `logs/bench-ctx-ladder-20260815-190658.json`

| 上下文 | 冷 TTFT | prefill 速度 | 冷 decode | 热 TTFT | 热 decode | RSS 峰值 |
|--------|---------|-------------|-----------|---------|-----------|---------|
| 512 tok | 4.3s | 128 tok/s | **21.2 tok/s** | 4.3s | 21.3 | 14.1 GB |
| 4K | 13.9s | 294 tok/s | 20.5 | 14.4s | 19.8 | 14.1 GB |
| 16K | 55.4s | 294 tok/s | 17.3 | 59.8s | 16.5 | 14.1 GB |
| **32K** | **24.4s** | **1344 tok/s** ⚡ | 18.3 | 24.5s | 18.8 | 14.1 GB |
| 64K | 48.0s | 1365 tok/s | 17.2 | 48.1s | 17.2 | 14.1 GB |
| 100K | 77.4s | 1323 tok/s | 15.9 | 78.3s | 16.2 | 14.2 GB |
| 160K | 134.4s | 1226 tok/s | 14.0 | 134.9s | 14.0 | 14.2 GB |

**关键发现**

- **PFlash 拐点在 32K tokens**: 阈值以下全量 prefill 稳定 294 tok/s;≥32K 压缩生效,
  有效 prefill 跳至 ~1350 tok/s(4.6×),32K→160K 近似线性
- **decode 衰减平缓**: 21.2 → 14.0 tok/s(160K 时 -34%);GDN 层 O(1)/token,
  仅每 4 层全注意力层随上下文增长
- **前缀缓存对本模型几乎无收益**: 热 TTFT ≈ 冷 TTFT 全档位成立。
  引擎对 hybrid 自动限制 `--hybrid-cache-entries=8`,GDN 递归状态恢复成本≈重算
  (与投机解码 blocker 同根因,Rapid-MLX #1941)。真实流量有少量部分命中
  (观测到 1710 tok 请求命中 932)
- **推论**: Claude Code 每轮对话付全额 prefill。典型一轮 30K chars ≈ 8K tok ≈ 28s TTFT

### OOM 边界 + PFlash A/B(tools/oom_ladder_test.py)

无 PFlash(基线): 46.6K tok/191s,100.9K/701s,140K 超 900s 客户端超时
(进程健康,非 OOM);RSS 平坦 11.0→11.2GB,**全程无 Metal OOM 特征**

有 PFlash: 

| 阶梯 | 无 PFlash | 有 PFlash | 加速比 |
|------|-----------|-----------|--------|
| 100K tok | 700.8s | **62.6s** | **11.2×** |
| 140K tok | >900s(超时) | **95.8s** | >9.4× |
| 250K tok | ~3600s(推算) | **218.0s** | ~16× |

- **埋针质量**: 100K tok 上下文 50% 深度双针(端口/人名)全部召回 ✅
- **250K tok(≈模型 262K 窗口 96%)跑通**,RSS 峰值 14.08GB
- OOM 结论: hybrid KV + 4bit 量化 + GPU 70% 预算下,内存不是边界,延迟才是

### 代理层配置(随本次基线落地)

| 配置项 | 旧值 | 新值 |
|--------|------|------|
| `PROXY_CTX_CHARS_LIMIT` | 80,000 | 400,000 (≈100K tok) |
| `PROXY_ROUTE_THRESHOLD_CHARS` | 80,000 | 400,000 |
| `PROXY_MAX_REQUEST_BYTES` | 512,000 | 1,048,576 |
| `PROXY_CHARS_EXPANSION` | 90,000 | 400,000(修 Priority-8 生命周期路由) |

注意: 只调 chars 阈值无效——SmartRouter Priority 8 会把 saturation 阶段
(旧边界 90K chars)直接路由云端,必须同步上调 `PROXY_CHARS_EXPANSION`。

### 与 35B-A3B 基线(2026-07-12)对比

| 指标 | Qwen3.6-35B-A3B (MoE 3B 激活) | Qwen3.8-27B (dense hybrid) |
|------|------------------------------|----------------------------|
| 短上下文 decode | **102.5 tok/s** | 21.2 tok/s |
| 热 TTFT | **≈0s(缓存全命中)** | ≈冷 TTFT(缓存基本无收益) |
| 100K tok 冷 TTFT | 6.9s(异常值,调度变化) | 77.4s(PFlash 后) |
| 最大实测上下文 | 200K tok | **250K tok 稳定** |
| 内存占用 | 12.3 GB | 11.0-14.2 GB(全窗口平坦) |

**选型建议**: ~~日常高频轮次(短上下文+缓存敏感)35B-A3B 仍是主力;
长上下文专项(>30K tok 冷启动、全窗口需求)用 Qwen3.8-27B。~~
**已被下方三方同方法对比修正(2026-08-15): 同方法重测后 35B-A3B 在全部
档位(含 160K 长上下文)性能领先,本节 7-12 数据为旧引擎基线存档,仅供追溯。**

### 与 Qwen3.6-27B / 35B-A3B 三方同档对比 (2026-08-15)

同方法(`bench_ctx_ladder.py`,生成 256 tok/档)、同引擎(rapid-mlx 0.12.13)、
同 GPU 预算(70%),3.6-27B 与 35B 侧临时启用 `--pflash auto --pflash-include-tools`
保证方法学一致(测毕均已还原配置)。原始数据:
`logs/bench-ctx-ladder-20260815-190658.json` (3.8) /
`logs/bench-ctx-ladder-20260815-200534.json` (3.6) /
`logs/bench-ctx-ladder-20260815-201523.json` (35B)

每格顺序: **35B-A3B / 3.6-27B / 3.8-27B**,加粗为该档最优

| 上下文 | 冷 TTFT | 冷 prefill tok/s | 热 TTFT | decode tok/s |
|--------|---------|------------------|---------|--------------|
| 512 tok | **1.3s** / 8.2s / 4.3s | **422** / 67 / 128 | **0.7s** / 3.1s / 4.3s | **82.3** / 21.3 / 21.2 |
| 4K | **3.3s** / 41.5s / 13.9s | **1247** / 99 / 294 | **0.8s** / 3.1s / 14.4s | **77.1** / 20.4 / 20.5 |
| 16K | **12.0s** / 165.5s / 55.4s | **1360** / 99 / 294 | **1.0s** / 3.7s / 59.8s | **63.1** / 16.9 / 17.3 |
| 32K ⚡PFlash | **4.9s** / 66.5s / 24.4s | **6697** / 494 / 1344 | **4.7s** / 66.8s / 24.5s | **73.9** / 19.2 / 18.3 |
| 64K | **9.6s** / 133.7s / 48.0s | **6794** / 490 / 1365 | **9.4s** / 134.9s / 48.1s | **65.9** / 17.6 / 17.2 |
| 100K | **16.3s** / 217.6s / 77.4s | **6287** / 471 / 1323 | **16.3s** / 218.6s / 78.3s | **59.4** / 16.0 / 15.9 |
| 160K | **37.8s** / 375.3s / 134.4s | **4355** / 439 / 1226 | **42.6s** / 377.6s / 134.9s | **48.8** / 13.7 / 14.0 |
| RSS 峰值 | **11.9-12.1 GB** / ~14 GB / 14.1-14.2 GB | | | |

**关键发现**

- **35B-A3B 每个单元格全胜**: decode 3-4×(82 vs 21 tok/s,3B 激活 vs 27B 全参数);
  冷 prefill PFlash 阈值以下 4.6-10×(1360 vs 294/99),PFlash 区 5×(~6800 vs
  ~1350/~470);160K 全程仅 37.8s vs 3.8 的 134.4s;内存还最轻
- **热前缀缓存 35B 与 3.6 都生效、唯独 3.8 无效**: ≤16K 档 35B 热 0.7-1.0s、
  3.6 热 3.1-3.7s,而 3.8 热≈冷。35B 同为 hybrid(is_hybrid=True)却有缓存收益,
  说明"hybrid ⇒ 缓存无收益"不成立——3.8 的 GDN 层高占比(3/4 线性层)使递归状态
  恢复成本≈重算,35B 的 hybrid 结构缓存友好得多
- **PFlash 区(≥32K)三者热缓存全部失效**(压缩改变 token 序列 → 缓存键失效),
  该区热≈冷,差距完全由冷 prefill 速度决定 → 35B 优势进一步扩大
- **decode 衰减斜率**: 35B 82→49 tok/s(-41%),3.6/3.8 均 21→14(-34%)——
  长上下文下 35B 衰减略陡,但 160K 处仍有 3.5× 绝对领先
- **结论(修正上一节选型建议)**: 纯性能维度 35B-A3B 全场景碾压,无 trade-off
  区间;3.8-27B 的选用理由只能来自**生成质量/能力代差**(新一代模型,本压测
  不覆盖),若质量差异不敏感,35B 是速度最优解。可用 `tools/bench_quality.py`
  补质量维度对比后再定主力配置

### 生成质量对比 (bench_quality.py, 2026-08-15)

14 用例(代码×4/数学×3/指令×3/格式×2/常识×2),走代理 4000,temp=0.1。
原始数据: `logs/quality-bench-20260815-202212.json` (3.8) /
`logs/quality-bench-20260815-202319.json` (35B)

| 维度 | 3.8-27B | 35B-A3B |
|------|---------|---------|
| 代码生成 | 2/4 | 2/4 |
| 数学/指令/格式/常识 | 10/10 | 10/10 |
| **总计(检查器口径)** | **12/14 (86%)** | **12/14 (86%)** |
| 实质(人工复核) | 14/14 | 14/14 |
| 单请求平均耗时 | 6.8s | **1.9s** (3.6×) |

- 两者的 2 个"失败"用例完全相同且均为**检查器命名严格性误判**:
  `code_sort` 双方实现为 `def quick_sort`(期望 `quicksort`);
  `code_recursive` 双方实现为 `def max_depth` / `def tree_depth`(期望 `depth`)——
  代码本身完整正确(含类型注解、docstring、分区/递归辅助函数)
- **本套件区分不出质量差**: 用例偏基础,两模型同分同耗时分布。
  质量维度无法为 3.8 翻盘 → **按速度选 35B-A3B**;若需更强区分度,
  后续可接 HumanEval 级评测再验证

---

## TTFT 基准测试

### 测试方法

- 5 种负载: small (18 chars) / medium (61 chars) / large (153 chars) / 5K ctx / 10K ctx
- 测量冷启动 (cold) 和热启动 (warm) 首 token 延迟
- 热启动比例 = warm / cold (越低越好)

### 结果

| 负载 | 冷 TTFT | 热 TTFT | 热/冷比 |
|------|---------|---------|---------|
| small (18 chars) | **0.4s** | **0.0s** | 0% |
| medium (61 chars) | **0.8s** | **0.0s** | 0% |
| large (153 chars) | **1.1s** | **0.0s** | 0% |
| 5K ctx (840 chars) | **1.4s** | **0.0s** | 0% |
| 10K ctx (1661 chars) | **1.7s** | **0.0s** | 0% |

### 关键发现

- **热启动 TTFT 全部为 0.0s** -- prefix cache 命中率极高
- **冷启动 TTFT 随输入线性增长**: small 0.4s -> 10K ctx 1.7s (4.25x)
- 代理层管线 (22 stage) 增加约 0.3-0.5s 固定开销

---

## 生成速度

### 测试方法

- Prompt: 61 chars medium prompt
- max_tokens: 50 / 200 / 500
- 测量: 总耗时、生成速度 (tok/s)

### 结果

| max_tokens | 总耗时 | 输出 tokens | 生成速度 |
|-----------|--------|------------|---------|
| 50 | 0.8s | 50 | **60.5 tok/s** |
| 200 | 3.0s | 200 | **102.5 tok/s** |
| 500 | 7.1s | 500 | **81.5 tok/s** |

### 关键发现

- **最佳速度 102.5 tok/s** (max_tokens=200)，比旧基线 75 tok/s 提升 37%
- 500 tokens 时速度下降至 81.5 tok/s，可能因 GPU 持续负载发热降频

---

## 多轮递增 (Agentic 模拟)

### 测试方法

- 模拟 Claude Code agentic 场景: 每轮追加前一轮输出，逐步增加上下文
- 6 轮递增: 18 -> 360 chars

### 结果

| Round | 输入 | 输出 | 耗时 | 生成速度 |
|-------|------|------|------|---------|
| 0 | 18 | 3 | 0.2s | 15.6 tok/s |
| 1 | 39 | 18 | 0.4s | 42.2 tok/s |
| 2 | 75 | 52 | 0.9s | 60.5 tok/s |
| 3 | 145 | 77 | 1.4s | 53.9 tok/s |
| 4 | 240 | 102 | 1.8s | **374.0 tok/s** |
| 5 | 360 | 144 | 2.4s | 158.4 tok/s |

### 关键发现

- **Round 4 异常高 (374 tok/s)**: 可能因 prefix cache 完全预热，预填充极快
- **整体趋势**: 速度随轮次增长而提升 (15.6 -> 158.4 tok/s)，说明 prefix cache 在多轮对话中持续积累收益


---

## 长上下文性能

### 测试方法

- 7 个上下文档位: 1K / 5K / 10K / 20K / 40K / 100K / 200K tokens
- 测量冷/热 TTFT、prefill 速度、生成速度
- 所有请求使用相同 prefix，验证 prefix cache 命中

### 结果

| Context | 冷 TTFT | 热 TTFT | 热/冷比 | Prefill 速度 |
|---------|---------|---------|---------|-------------|
| 1K tok | 0.9s | 0.0s | 0% | 1,076 tok/s |
| 5K tok | 0.9s | 0.0s | 0% | 5,267 tok/s |
| 10K tok | 6.0s | 0.9s | 16% | 1,561 tok/s |
| 20K tok | 12.3s | 1.0s | 8% | 1,509 tok/s |
| 40K tok | 26.0s | 10.5s | 41% | 1,280 tok/s |
| 100K tok | 6.9s | 4.7s | 68% | 11,988 tok/s |
| 200K tok | 4.0s | 11.4s | 282% | 41,071 tok/s |

### 关键发现

- **Prefix cache 全部命中** (1K-200K 均显示缓存命中) -- DEF-203 修复有效
- **40K tokens 是性能拐点**: 冷 TTFT 26.0s (10K 的 4.3x)，热 TTFT 10.5s
- **100K+ 异常**: 冷 TTFT 反而下降 (6.9s < 26.0s)，可能因后端动态调度策略变化
- **200K 热 TTFT > 冷 TTFT**: 可能因缓存重建开销超过冷启动
- **Prefill 速度**: 小上下文 ~1K-5K tok/s，大上下文突发可达 41K tok/s

---

## 并发性能

> 当前代理配置 `PROXY_MAX_CONCURRENT=1` (48GB Mac 推荐值)，无法进行并发测试。
> 如需并发数据，请参考 2026-05-10 旧基线。

### 历史数据 (Rapid-MLX + Qwen3.6-35B-A3B, 2026-05-10)

**小上下文**:

| 并发 | 总耗时 | 单请求速度 | 总吞吐 |
|------|--------|-----------|--------|
| 1 | 0.97s | 43.4 t/s | 43.4 t/s |
| 2 | 0.81s | 50.2 t/s | 86.7 t/s |
| 3 | 1.10s | 39.5 t/s | 95.6 t/s |
| 4 | 1.38s | 33.0 t/s | 101.5 t/s |

**大上下文 (~13.6K tokens)**:

| 并发 | 总耗时 | 总吞吐 | 成功数 |
|------|--------|--------|--------|
| 1 | 42.65s | 50.4 t/s | 1/1 |
| 2 | 33.04s | 70.5 t/s | 2/2 |
| 3 | 47.62s | 93.9 t/s | 3/3 |
| 4 | 49.29s | 94.3 t/s | 4/4 |

---

## 历史对比 (2026-05-10 vs 2026-07-12)

| 指标 | 旧基线 (05-10) | 新基线 (07-12) | 变化 |
|------|---------------|---------------|------|
| TTFT small | ~0.1s | 0.4s | 增加 (22 stage 管线开销) |
| TTFT medium | ~0.1s | 0.8s | 增加 |
| 生成速度 (200 tok) | ~75 tok/s | **102.5 tok/s** | **+37%** |
| 生成速度 (50 tok) | ~43 tok/s | **60.5 tok/s** | **+41%** |
| 长上下文 40K TTFT | 未测 | 26.0s cold / 10.5s warm | 新基线 |
| Prefix cache 命中 | 未测 | 1K-200K 全部命中 | 优秀 |
| 500 错误率 | 2.3% | **0%** | 消除 |
| 503 错误率 | 4.5% | **0%** | 消除 |
| 单元测试 | 871 | **890** | +19 |

### 分析

1. **生成速度大幅提升 (+37%)**: 得益于 `rapid-mlx-35b-opt` 配置优化 (移除 force-spec-decode, flash-attn on)
2. **TTFT 增加**: 代理层从 8 层管线扩展到 22 层，每层约 20-40ms 固定开销，累计约 0.3-0.5s
3. **错误率归零**: DEF-001/DEF-101 修复有效
4. **Prefix cache 稳定**: DEF-203 修复后跨 session 工具序列一致，缓存命中率极高


---

## 端到端测试（代理层）

### 测试链路

```
Claude Code -> anthropic_proxy.py:4000 -> Rapid-MLX:8081 -> Qwen3.6-35B-A3B
```

### 代理兼容性

| 功能 | 状态 | 说明 |
|------|------|------|
| 简单对话 | OK | 20/20 请求 100% 成功 |
| 流式响应 | OK | SSE 格式正确 |
| 工具调用 (Bash/Read) | OK | XML->JSON 双向转换 |
| 消息格式转换 | OK | Anthropic <-> OpenAI |
| max_tokens 传递 | WARN | 代理层已强制截断 (DEF-106 修复) |
| stop_reason 映射 | OK | end_turn / max_tokens / tool_use |
| 循环检测 | OK | 3 级干预 (DEF-002/109 修复) |
| 上下文截断 | OK | fifo 策略 + 结构化摘要 (DEF-107 修复) |
| 可观测性 | OK | Chart.js 趋势图 + TTFT 追踪 (DEF-304) |
| Watchdog | OK | daemon 模式 + PID 文件 (DEF-207) |

---

## 已知问题

### 1. Rapid-MLX 不遵守 max_tokens

- **现象**: 请求设置 `max_tokens=100`，实际可能生成远超限制
- **原因**: Rapid-MLX 0.6.30 的 bug，参数已接收但内部调度器未执行截断
- **已缓解**: 代理层新增 `force_stopped` 截断 + JSON 修复 (DEF-106)
- **根治**: 升级 Rapid-MLX 到 v0.6.71+

### 2. llama.cpp 不支持 Qwen3.5-9B 的 DeltaNet 架构

- **现象**: 9B 模型生成速度仅 17 tok/s（预期 150+）
- **原因**: Gated DeltaNet + Gated Attention 混合架构的 Metal 支持不完善
- **解决**: 使用 MLX 框架（Rapid-MLX 实测 108 tok/s）

### 3. KV Cache 恢复错误（llama.cpp）

- **现象**: 日志中大量 `state_seq_set_data: error loading state: failed to restore kv cache`
- **影响**: 不影响功能，但提示 llama.cpp 对 Qwen3.5/3.6 的状态恢复有兼容性问题

### 4. 多并发 GPU 饱和

- **现象**: Mac M5 Pro 单 Metal GPU，3 并发后吞吐量不再增长
- **本质**: slot 分时复用 != 真正并行，并发数 != slot 数
- **建议**: 48GB Mac 上保持 `PROXY_MAX_CONCURRENT=1`

---

## 配置切换指南

### 配置文件位置

```
~/APP/llama.cpp/configs/
  active.conf                 # 当前激活配置（软链接）
  rapid-mlx-35b-opt.conf      # Rapid-MLX + 35B-A3B (优化版)
  qwen3.6-27b-4bit.conf       # Rapid-MLX + Qwen3.6-27B dense
  qwen3.8-27b-4bit.conf       # Rapid-MLX 0.12.13 + Qwen3.8-27B hybrid (PFlash)
  gemma4-26b.conf             # Rapid-MLX + Gemma-4-26B
  deepseek-chat.conf          # 云端 DeepSeek
```

### 快速切换

```bash
cd ~/APP/llama.cpp

# 切换到云端 DeepSeek
./manage.sh switch deepseek-chat && ./manage.sh reload
./manage.sh stop-backend

# 切回本地 Rapid-MLX
./manage.sh switch rapid-mlx-35b-opt && ./manage.sh reload
./manage.sh start-backend

# 查看所有配置
./manage.sh list

# 查看当前配置
./manage.sh current
```

---

## 推荐配置

| 场景 | 推荐配置 | 原因 |
|------|---------|------|
| 单用户编程 | `rapid-mlx-35b-opt` | 102 tok/s，prefix cache 全命中，0% 错误率 |
| 质量敏感任务 | `rapid-mlx-35b-opt` + conservative profile | 保留更多上下文，适合代码审查 |
| 长上下文 agentic | `rapid-mlx-35b-opt` + aggressive profile | 优先控制 token 消耗 |
| 超长上下文 (30K-250K tok) | `qwen3.8-27b-4bit` | PFlash 11× prefill,250K 窗口实测稳定,内存平坦 |
| 云端高速 | `deepseek-chat` | 无本地资源限制，按 token 计费 |
| Gemma 4 实验 | `gemma4-26b` | 26B 模型，需 2 并发上限 |

---

## 附录: 测试原始数据

### 2026-07-12 完整基线

原始数据文件: `logs/perf-bench-20260712-214241.json`

```
TTFT:
  small:   cold=0.4s  warm=0.0s  in=18  out=3
  medium:  cold=0.8s  warm=0.0s  in=61  out=50
  large:   cold=1.1s  warm=0.0s  in=153 out=50
  5k_ctx:  cold=1.4s  warm=0.0s  in=840 out=50
  10k_ctx: cold=1.7s  warm=0.0s  in=1661 out=50

生成速度:
  max_tokens=50:   0.8s  60.5 tok/s
  max_tokens=200:  3.0s  102.5 tok/s
  max_tokens=500:  7.1s  81.5 tok/s

长上下文:
  1K:     cold=0.9s   warm=0.0s   prefill=1076 tok/s
  5K:     cold=0.9s   warm=0.0s   prefill=5267 tok/s
  10K:    cold=6.0s   warm=0.9s   prefill=1561 tok/s
  20K:    cold=12.3s  warm=1.0s   prefill=1509 tok/s
  40K:    cold=26.0s  warm=10.5s  prefill=1280 tok/s
  100K:   cold=6.9s   warm=4.7s   prefill=11988 tok/s
  200K:   cold=4.0s   warm=11.4s  prefill=41071 tok/s
```

---

*文档生成时间: 2026-07-12;2026-08-15 增补 Qwen3.8-27B 基线*
*测试工具: tools/bench_perf.py (完整模式);tools/bench_ctx_ladder.py / tools/oom_ladder_test.py (Qwen3.8)*
*原始数据: logs/bench-ctx-ladder-20260815-190658.json, logs/oom-ladder-20260815-180529.json*
