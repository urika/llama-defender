# Ornith-oQ4e 长上下文性能瓶颈分析与优化方案

> 日期：2026-09-20 ｜ 状态：**诊断 + 方案（未实施）**
> 硬件：Apple M5 Pro / 48 GB 统一内存 ｜ 后端：rapid-mlx **0.12.12**
> 配置：`configs/ornith-oq4e.conf`（`active.conf` 指向）｜ 模型：`pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp`
> 数据源：`./manage.sh status`、`GET /api/status`、`GET /metrics`、`logs/llama-server.log`、
>          `logs/diag/sessions.jsonl`、rapid-mlx 0.12.12 源码与官方文档、社区公开测评

---

## 1. 摘要

| 结论 | 说明 |
|---|---|
| 服务健康、缓存机制正常 | `state=healthy` / `ready=true`；prefix cache 命中率 99%+ |
| **长上下文吞吐显著低于基线** | 40K+ 上下文纯解码 ~20 tok/s，约为仓库短上下文基线（~80）的 1/4 |
| **主因 = Metal 内存压力导致的缓存驱逐** | `active 27.1GB / peak 31.0GB > cap 30.2GB`，每个请求触发 `prefix-pressure-evict` + 分块缩到 256 |
| **次因 = 长上下文本征开销 + KV 量化 dequant** | 10 层全注意力的 O(S) KV 读取；`int4` KV 的 O(context) dequant 惩罚 |
| 额外开销 | HBE 探针（每 2 请求一次 16K-token 探针，串行占锁）、PD 微轮重派 |
| 无投机解码可用 | hybrid 骨干被 rapid-mlx 门控关闭 spec-decode（AGENTS.md §8.3） |

**判定**：短上下文达标、缓存有效；长上下文偏慢属**已知行为**（`docs/05-operations-changelog/ornith-15-integration-20260826.md:209` 已记录长会话 decode <27 tok/s 引发超时风暴），非新故障。

---

## 2. 现状实测指标

### 2.1 服务与内存

| 指标 | 实测值 | 来源 |
|---|---|---|
| 状态 / ready | `healthy` / `true` | `/api/status` |
| 后端 PID / 运行时长 | 3979 / ~4.7h | `make.sh status` |
| 内存占用（RSS） | ~5 GB（不含 Metal 分配） | `ps` |
| **Metal active / peak / cap** | **27.1 GB / 31.0 GB / 30.2 GB** | `logs/llama-server.log` |
| prefix cache entries / mem | 20–24 / 7.8–8.5 GB（上限 11.6 GB） | 同上 |
| 系统内存空闲 | 25%；swap 0 | `memory_pressure` / `sysctl vm.swapusage` |

### 2.2 速度

| 指标 | 实测区间 | 典型值 | 备注 |
|---|---|---|---|
| 纯解码（generation only）@40K | 15–30 tok/s | ~20 | `finished normally, N tokens in Xs` |
| 端到端（含 TTFT）@40K | 8.8–21.3 tok/s | 12–15 | `Chat completion: N tokens in Xs` |
| 冷 prefill（MISS，31–33K tok） | 270–350 tok/s | ~300 | 首 token 93.6s / 122.1s |
| 增量 prefill（命中，20–1242 tok） | — | 首 token 0.7–3.0s | `tokens_to_prefill` |
| **TTFT** | p50 **7.2s** / p95 17.7s / p99 18.7s | — | `/metrics` |
| prefix 命中率 | `cached/prompt` ≈ 99%+ | — | 如 `cached=43730/43750` |
| sent_view prefix_ratio | 0.989–0.997 | — | `sessions.jsonl` |

### 2.3 基线对照

| 来源 | 解码 | prefill（冷） | 说明 |
|---|---|---|---|
| 仓库基线（短上下文 ~5K） | ~80 tok/s（70–96） | ~713 tok/s | `ornith-15-integration-20260826.md` |
| 本机实测（长上下文 40K+） | ~20 tok/s | ~300 tok/s | 本文 |
| 外部 DGX Spark NVFP4 | ~75 tok/s | 4083 tok/s @43K 未缓存 | vLLM/SGLang |
| 外部 M5 Max MLX-8bit | 97 tok/s（短） | — | llm-bench.io |
| 外部 CrucibleMark | 54.22 tok/s | — | 综合评分 75.65 |

> 口径不可直接比较：仓库/外部基线为**短上下文**；本机为 40K+ 长上下文 agentic 会话。

---

## 3. 瓶颈根因分析

### 3.1 内存账 → 每请求驱逐

```
权重 (4-bit)        20.1 GB
prefix cache        8.0–11.6 GB   (--cache-memory-mb 12288)
live KV / 激活      余量
--------------------------------
Metal active        27.1 GB
Metal peak          31.0 GB   >   cap 30.2 GB   ← 0.75 × 基数 ~40.3 GB
```

`peak > cap` 使 rapid-mlx 每请求触发：
- `[prefix-pressure-evict] evicted N entries under Metal pressure (metal_cap=30.2GB, cache_max=11.6GB)`
- `[adaptive_prefill] chunk 2048 -> 256`（Metal 压力下缩减分块 → prefill 吞吐下降）

### 3.2 模型结构：KV 只作用于 10/40 层

由模型 `config.json` 的 quantization map 反推层类型：

| 层类型 | 层号 | 数量 |
|---|---|---|
| 全注意力（`self_attn`） | 3, 7, 11, 15, 19, 23, 27, 31, 35, 39 | **10** |
| GatedDeltaNet 线性注意力（`linear_attn`） | 其余 | **30** |

推论：
- 线性层 carry **固定大小 recurrent state**（零 per-token KV 增长），但作为 `ArraysCache` **non-trimmable**（见 `rapidmlx-kvcache-mechanism-20260903.md`）
- KV 量化（`--kv-cache-quantization`）仅影响 10 层 → **省内存有限，但 dequant 惩罚照付**

### 3.3 KV 量化惩罚

- 当前配置经 `manage.sh:611` 映射为 `--kv-cache-quantization --kv-cache-quantization-bits 4`
- 该路径 `--kv-cache-quantization` 是已废弃别名（等价 `--kv-cache-dtype int8`）
- rapid-mlx 官方实测（#1853）：**int4 KV 的 dequant-on-read 为 O(context)/step，16K 处即 -27% decode**（int8 -36%），上下文越长惩罚越大
- 由于本模型仅 10/40 层受影响，实际惩罚小于 dense 模型，但仍非零

### 3.4 附加运行开销

| 项 | 配置 | 影响 |
|---|---|---|
| HBE 探针 | `PROXY_HBE_ENABLED=true`、`PROXY_HBE_SAMPLE_EVERY=2` | 每 2 成功本地响应搭便车一次 16K-token 探针，串行占引擎锁（`--max-num-seqs 1`），抬高延迟/排队 |
| PD 微轮重派 | `PROXY_PD_MICRO_TURN_ENABLED=true` | 额外一次模型 dispatch |
| 并发 | `--max-num-seqs 1`、`PROXY_MAX_CONCURRENT=1` | 无并行摊销，单请求串行 |

---

## 4. 外部测评调研（模型质量侧）

模型身份：官方 `ornith-ai/Ornith-1.5-35B-A3B`（Qwen3.5-MoE 架构，35.8B 总参 / ~3B 激活 / 256 专家 top-8 / 40 层 hybrid / 262K 原生 / MIT）的 oMLX oQ4e 4-bit 量化；原始 1.5 版 **MTP 头损坏**，本量化由 Shisha-AI 重训（"fixed-mtp"）。

### 4.1 厂商自报（5 次平均）

Terminal-Bench 2.1 (Claude Code) 68.5 ｜ SWE-bench Verified 79–80.1 ｜ SWE-bench Pro 59.6 ｜ GPQA Diamond 89.2 ｜ 声称全面超 Qwen3.6-35B-A3B（52.5）。

### 4.2 独立复现存疑（explainx.ai 汇总 HN）

| 基准 | Qwen3.8-27B（社区） | Ornith-1.5-35B（社区） |
|---|---:|---:|
| Terminal-Bench 2.1 | **73.0** | 67.8 |
| SWE-bench Pro | **61.7** | 59.6 |
| DeepSWE | **42.2** | 22.0 |
| HLE | **30.8** | 25.6 |

→ 官方表称超 Qwen3.6，独立跑却**输给 Qwen3.8-27B**（DeepSWE 差近一倍）；另有模型常自称 Claude（训练数据污染迹象）。

### 4.3 量化 / MTP 水位

| 来源 | MTP acceptance | decode |
|---|---|---|
| bartowski IQ4_XS + Q4_0 MTP | 32.5% | 32.6 tok/s |
| mudler APEX + Q8_0 MTP | 26.2% | 25.6 tok/s |
| 1.5-9B 基线（专用 drafter） | 82.7% | 65.4 tok/s |

→ 35B 档 MTP 头与量化目标分布错配，接受率普遍差；本量化 "fixed-mtp" 是否真正修复，公开独立验证尚缺。

**结论**：速度侧的「短上下文 75–97 tok/s」与本机「长上下文 ~20 tok/s」不矛盾——瓶颈在长上下文与 Metal 压力，而非模型本身；质量侧官方宣称强但独立复现存疑。

---

## 5. 优化方案（未实施，按风险/收益分档）

### A. 代理层（零风险，`./manage.sh reload` 即生效）

| 参数 | 现值 → 建议 | 作用 |
|---|---|---|
| `PROXY_HBE_ENABLED` | `true` → **`false`**（或 `SAMPLE_EVERY` 2→8） | 去掉每 2 请求的探针锁占用/延迟 |
| `PROXY_CTX_EPOCH_TRIGGER_TOKENS` | `70000` → **`50000`** | 降 live KV，直接削 O(ctx) decode |
| `PROXY_CTX_KEEP_TOKEN_BUDGET` | `40000` → **`28000`** | 同上 |
| `PROXY_DYNAMIC_MAX_TOKENS_SATURATION` | `8192` → **`4096`** | 限制解码期 KV 增长，降超时风险 |
| `PROXY_PD_MICRO_TURN_ENABLED` | `true` → `false`（需评估召回收益） | 省一次额外重派 |

### B. rapid-mlx 启动参数（改 `RAPID_MLX_EXTRA_ARGS`，需 `restart-backend`）

| 参数 | 作用 / 风险 |
|---|---|
| **`--pin-system-prompt`** | 钉住 system prompt 防驱逐——**直击每请求 evict**，优先级最高 |
| `--relocate-mid-conversation-system` | 保 Claude Code 中途注入 system 的 prefix cache（默认 hoist 会改写前缀）；文档警告「被搬移文本获 user 权限」 |
| `--kv-cache-dtype int4` | 替换已废弃 `--kv-cache-quantization` 别名 |
| `--kv-cache-turboquant k8v4` | 替代方案（~1.6× prefill，代价 ~12% decode）；与 quantization 互斥，实验性；AGENTS.md §8.3 建议若需跨重启 prefix cache 则勿用 |
| `--metal-cap-kv-bytes-per-token` | 量化 KV 被自动 fp16 估算高估 → 可能误拒长 prompt；按实测下调（int4 ≈ fp16 的 1/4） |
| `--pflash auto --pflash-include-tools` | 冷 prefill TTFT **3.87×–8.5×**（PR#287/#649）；默认跳过带 tools 请求；丢中段 ~80% token，仅治冷启 |
| `--hybrid-cache-entries 32→16` / `--cache-memory-mb 12288→8192` | 降 cache 内存换更少驱逐；可能增冷 prefill |

### C. 系统级（需 sudo，重启失效）

| 项 | 命令/值 | 说明 |
|---|---|---|
| 提升 Metal wired 上限 | `sudo sysctl iogpu.wired_limit_mb=43008` | 48GB 档安全值 42GB（留 6GB 给系统）；**风险**：贴近上限触发 `watchdogd`/`AppleARMWatchdogTimer` panic |
| GPU 配额 | `--gpu-memory-utilization 0.75 → 0.80` | 仓库 DEF-006 允许上限（`manage.sh:542`） |

### D. 结构性（收益最大，代价/验证成本高）

| 方向 | 代价 |
|---|---|
| 降量化档（oQ3，权重 ~15GB，腾 5GB 给 KV/cache） | 质量损失 |
| MoE expert disk-stream（`--disk-stream`） | 释放 resident 权重；需 `vllm_mlx.registry` 支持 `qwen3_5_moe`，命中率决定成败 |
| 升级到 64GB 机器 | 成本 |

---

## 6. 预期性能指标

基准口径：**长上下文** = 当前实测 ~40K prompt / 60+ 消息会话；**短上下文** = 5–10K 回归基线。

| 指标 | 当前实测 | P0（代理层 + pin/gpu） | P0+P1（+wired limit/pflash） | 依据 |
|---|---|---|---|---|
| 纯解码 @40K | 15–30（~20） | 25–35 | 30–45（乐观） | 消驱逐重算 + 减少 KV dequant O(ctx) 惩罚；上下文项仍在 |
| 端到端 @40K | 8.8–21（12–15） | 20–30 | 28–40 | 主要来自 TTFT 改善 |
| TTFT p50 | 7.2s | 3–5s | 2–4s | HBE 关闭 + 命中稳定 |
| TTFT p95 | 17.7s | 8–12s | 5–8s | 冷/驱逐轮减少；pflash 治冷启 |
| 冷 prefill | 270–350 tok/s | 400–550 | 550–700（pflash 等效 1000–2500） | 分块不再缩到 256；pflash 3.87–8.5× |
| prefix 驱逐 | **每请求** | 0（目标） | 0 | `--pin-system-prompt` + cap 余量 |
| 短上下文 decode | — | 70–85 | 75–95 | 回归仓库基线 ~80 / 外部 M5 类 ~97 |

### 关键判断（避免过高预期）

1. **纯解码硬上限由上下文决定**：hybrid 门控关闭投机解码，10 层全注意力的 O(40K) KV 读取无法绕过。P0/P1 主要回收 **TTFT 与有效吞吐**，不是把 20 直接拉回 80。40K 下 ~30–45 已是合理上限；接近 80 需把上下文压到 10K 量级。
2. **短上下文可回归基线**：优化后 5–10K prompt 应稳定 70–90 tok/s、TTFT <1s，这是验证优化生效的最干净口径。
3. **若 pin + headroom 仍止不住驱逐**（即 `cache-memory-mb 12G` 结构性过大），则解码不会改善，需降到 8G 或进一步降 S——以「驱逐次数 = 0」为验收线。
4. **质量不受影响**：均为运行时/缓存调参，不改权重；pflash 是唯一有召回代价的选项（丢中段 ~80%，默认跳过 tools）。

---

## 7. 验收方法

- 用影子代理（`test/lib/promptfoo_env.sh`，:4021，状态隔离）跑固定短/长 prompt 各一轮，避免污染生产状态
- 读 `logs/llama-server.log`：
  - `first token after Xs` → TTFT
  - `finished normally, N tokens in Xs` → 纯解码
  - `[prefix-pressure-evict]` 计数 → 断言归零
- **短期目标线**：长上下文纯解码 ≥25 tok/s、TTFT p95 <12s、驱逐=0；达标再进 P1

---

## 8. 风险与未决

| 项 | 风险 | 缓解 |
|---|---|---|
| `iogpu.wired_limit_mb` 提升 | 贴近上限触发 kernel watchdog panic | 48GB 档不超过 43008；监控 powerd/watchdog |
| `--pflash` | 丢中段 ~80% token，工具场景可靠性未知 | 保留默认跳过 tools；先影子 A/B |
| `--kv-cache-turboquant` | 实验性；可能影响跨重启 prefix cache | 先 A/B，必要时回退 |
| 预期值偏差 | 「删掉驱逐后解码恢复多少」最不确定 | 先做 P0 一轮量测再定 P1 |
| 质量宣称存疑 | 独立复现落后 Qwen3.8-27B | 以本仓实测任务为准，勿仅凭厂商表 |

---

## 9. 备选配置对比：切换到 Qwen3.8-27B

问题：切换到已有的 `configs/qwen3.8-27b-4bit.conf`（`mlx-community/Qwen3.8-27B-4bit`），性能预期如何？

> 说明：Qwen3.8-27B 是 **dense hybrid**（64 层 = 16 全注意力 + 48 GatedDeltaNet，全参数激活），与 Ornith 的
> **MoE 35B-A3B（~3B 激活）** 性质不同。该配置依赖 `.venv-rapidmlx` 的 rapid-mlx **0.12.13**（brew 仅 0.12.12），
> 显式 `--hybrid-cache-entries 8`、`--pflash auto --pflash-threshold 98304 --pflash-include-tools`、
> `--gpu-memory-utilization 0.80`、`PROXY_CTX_ENGINE_ENABLED=false`（fifo）。

### 9.1 同方法实测基线（`bench_ctx_ladder.py`，M5 Pro 48GB，rapid-mlx 0.12.13，PFlash）

| 上下文 | 冷 TTFT | prefill | 冷 decode | 热 TTFT |
|---|---|---|---|---|
| 512 | 4.3s | 128 tok/s | 21.2 tok/s | 4.3s |
| 4K | 13.9s | 294 | 20.5 | 14.4s |
| 16K | 55.4s | 294 | 17.3 | 59.8s |
| **32K** | **24.4s** | **1344** ⚡ | 18.3 | 24.5s |
| 64K | 48.0s | 1365 | 17.2 | 48.1s |
| 100K | 77.4s | 1323 | 15.9 | 78.3s |
| 160K | 134.4s | 1226 | 14.0 | 134.9s |

- RSS 峰值 **11.0–14.2 GB 全窗口平坦**，250K tok 实测稳定
- PFlash 拐点 32K：阈值下 294 tok/s → 之上 ~1350 tok/s（**4.6×**）
- decode 衰减平缓（21→14，-34%）

> **口径分歧（待复核）**：运维记录报 decode **~39.5 tok/s**（`dflash-mlx-integration-20260826.md:46`），
> 而 z-lab 冒烟（`qwen3.8-dflash2-smoke-20260829.md`）纯 AR 仅 17.94、DFlash2 36.6。按带宽估算
> （dense 27B 4-bit ≈ 16GB/step）**~20 tok/s 更可信**，39.5 存疑。

### 9.2 与当前 Ornith-oQ4e 对比（~32–40K 上下文）

| 维度 | Ornith-oQ4e（当前） | 当前负载下实测 | Qwen3.8-27B |
|---|---|---|---|
| 架构 | MoE 35B-A3B（3B 激活） | — | **dense hybrid 27B（全激活）** |
| 权重 | 20.1 GB | — | ~16.1 GB |
| **内存压力** | **27/30.2GB，每请求驱逐** | 同上 | **11–14GB 平坦，无驱逐** ✅ |
| decode | ~74（clean bench） | ~20（受驱逐拖累） | ~18（bench）/ 39.5（存疑） |
| cold prefill | ~300 tok/s（**无 pflash**） | ~300 | **~1344 tok/s（≥32K，pflash）** ✅ |
| 冷 TTFT @100K | ~330s（推算） | — | **77.4s** ✅ |
| prefix cache | **99% 命中、增量轮亚秒** | 同 | **弱（GDN 48/64 线性层，热≈冷）** ⚠️ |
| 稳定最大上下文 | 262K | — | 250K 已验证 |

### 9.3 结论：净效果 = 换掉「内存瓶颈」换「decode 速度」

**切到 3.8 更优的方面**
- **彻底消除 Metal 驱逐**（RSS 11–14GB）→ 直接解决当前 `prefix-pressure-evict` + `adaptive_prefill` 缩块顽疾
- **长上下文（≥64K）prefill/TTFT 大幅领先**（pflash：100K 冷 TTFT 77s vs Ornith ~330s）
- **质量大概率更好**：外部独立测评 Qwen3.8-27B 在 Terminal-Bench 2.1（73.0 vs 67.8）、DeepSWE（42.2 vs 22.0）领先 Ornith-1.5-35B

**切到 3.8 更差的方面**
- **decode 约 2–4× 慢**（dense 27B vs 3B 激活 MoE）→ 短/中上下文 agentic 逐轮明显变慢
- **prefix cache 收益弱且证据冲突**：clean bench 显示热≈冷（每轮付全额 prefill）；运维记录称 entries=8 后 80% 命中、增量轮 0.6s。**这是最大不确定项**，也是 3.8 早期 broken-pipe 风暴的根因
- 依赖 `.venv-rapidmlx`（0.12.13）；ctx engine 为 OFF（fifo）；采样/tool parser 不同（hermes）

### 9.4 选型建议

| 场景 | 选择 |
|---|---|
| Ornith 驱逐问题**能**通过 §5.B `--pin-system-prompt` + §5.C wired limit 修好 | **留在 Ornith**（decode 优势大） |
| 修不好，或负载以 **≥64K 长上下文**为主 | **切 Qwen3.8-27B**（内存稳、长 prefill 快、质量更好） |
| 追求短上下文极致速度 | 两者均不如 Qwen3.6-35B-A3B（短 decode 82 tok/s） |

> 一句话：切换是把「内存抖动 + ~20 tok/s」换成「无抖动 + ~18 tok/s（长上下文 prefill 快 4×）」，
> 属**质量/内存优先的横向置换，非纯提速**。

### 9.5 切换操作与验证

```
./manage.sh switch qwen3.8-27b-4bit
./manage.sh stop-backend      # 换模型 + 换引擎 venv，需重启后端
./manage.sh reload
./manage.sh start-backend
```

验证同 §7：影子代理跑固定短/长 prompt，读 `logs/llama-server.log` 的 `first token after` /
`finished normally, N tokens in Xs`；重点核实 **prefix cache 是否真命中**（`cache_fetch HIT cached=...`）
与 decode 实测值（消除 §9.1 的 21 vs 39.5 口径分歧）。

---

## 10. Qwen3.8-27B 上游优化进展（rapid-mlx 0.13 / 0.14，调研 2026-09-20）

针对 §9 识别的三个不足（decode 慢 / prefix cache 弱 / 无投机解码），上游已有对应能力，**主要杠杆是升级 rapid-mlx 大版本**。

本机现状：`.venv-rapidmlx` = **0.12.13**（brew 0.12.12）；检查点 `mlx-community/Qwen3.8-27B-4bit`
（alias `supports_spec_decode: false`、`tool_call_parser: hermes`）。pip 最新 **0.14.3**、brew stable **0.14.2**。

### 10.1 版本代差（关键条目）

| 版本 | 与 Qwen3.8-27B 相关的实测改进 |
|---|---|
| **0.13.4** | 合格 Qwen 模型自动启用 MTP 预设：Qwen3.8-27B 单请求 **1.43× @128 tok**、**2.34× @32K**；多请求 cohort 聚合 **+14.1%–30.8%**。`--no-spec-decode` 可退回 |
| **0.14.0** | Qwen3.8-27B **MTP GDN verification 45.87 → 52.28 tok/s（+14% decode）**；**Copy-draft**：Qwen3.8 turn 接受 **71–99%** 复制 token、decode **1.6–2×** |
| **0.14.2** | **hybrid 会话从 recurrent-state checkpoint 恢复**；工具轮不再把临时指引挪到 prompt 前端 → 长会话保住 prefix cache（省 ~20s/轮） |
| **0.13.0** | 递归模型 **prefill 自调 chunk**（按 profile）；GDN prefill blocked-seq Metal kernel；GDN 输入投影融合单量化 matmul |
| **0.12.16** | 官方推荐 32GB+ Mac 跑 Qwen3.8-27B：**"~40 tok/s with multi-token prediction on"** |

> **口径澄清**：这解释了 §9.1 的「21 vs 39.5 tok/s」分歧——**21 ≈ 纯 AR**（我们的 0.12.13，spec 被 hybrid
> 门控关闭）；**~40 ≈ MTP 开启**（0.13.4+ 自动预设）。

### 10.2 对三个不足的对应

| §9 不足 | 新方案 | 来源 |
|---|---|---|
| decode 慢（dense 27B 全激活） | MTP 自动预设（2.34× @32K）+ MTP GDN verification +14% + **Copy-draft 1.6–2×** | 0.13.4 / 0.14.0 |
| prefix cache 弱（GDN 48/64 线性层） | **hybrid recurrent-state checkpoint 恢复** + 工具轮前缀保护 + 自调 prefill | 0.14.2 / 0.13.0 |
| 无投机解码（#1941 hybrid 门控） | 0.13.4 起对合格 Qwen3.8-27B 产物**默认开启 MTP**；社区 `junainfinity/mlx-mtp` 亦在 repo 内实现 GDN/SSM speculative rollback | 0.13.4 |

### 10.3 落地步骤（本地栈）

```bash
# ① 升级引擎 venv（config 的 LLAMA_SERVER_BIN 指向它，不动 brew）
.venv-rapidmlx/bin/pip install -U "rapid-mlx>=0.14.3"     # 附带 MLX 0.32.x

# ② 换带 MTP 头的检查点（当前 mlx-community 版丢头）
#    0.14.x 别名：rapid-mlx/Qwen3.8-27B-4bit-MTP-MLX（MTP 内置）
#    或混合精度 rapid-mlx/Qwen3.8-27B-mixed-3.5bpw-MLX（13GB，更省内存）

# ③ 更新 configs/qwen3.8-27b-4bit.conf
#    - MODEL_NAME/LLAMA_MODEL → 新检查点
#    - RAPID_MLX_TOOL_PARSER: hermes → qwen3_coder_xml（0.14 别名变更）
#    - 可去掉显式 --speculative-config（0.13.4 起自动）
```

> 注：本机 HF 缓存仅有 `mlx-community/Qwen3.8-27B-4bit`，**MTP 检查点需另行下载（~13–15GB）**。

### 10.4 预期

| 指标 | 0.12.13（现状） | 0.14.3 + MTP 检查点 |
|---|---|---|
| decode 短 | ~21 tok/s | ~40+ tok/s |
| decode @32K | ~18 | ~35–42（2.34×） |
| 代码编辑轮 decode | — | +1.6–2×（copy-draft） |
| prefix cache | 弱/整链冷 | recurrent-state 恢复，长会话保命中 |
| prefill | 294（<32K）/1350（pflash） | 自调 chunk + GDN kernel，进一步升 |

### 10.5 风险与备选

- **MTP + GDN prefix cache 仍是要点**：vLLM 同类问题（#53670）显示 EAGLE/MTP 在 hybrid GDN 布局上
  会掉 6–10 点命中率、单次重算 1648 token；rapid-mlx 0.14.2 声称已用 recurrent-state checkpoint 解决，
  **需实测验证**。
- 需重新下载检查点；工具解析器更换需回归测试。
- 备选引擎（非 rapid-mlx）：`ddalcu/mlx-serve`（原生 Qwen MTP / PLD）、`junainfinity/mlx-mtp`
  （in-repo GDN rollback）、`z-lab DFlash2`（本地冒烟 2.0×，与生产持平）。
- **建议动作**：先影子环境升 0.14.3 + MTP 检查点跑 §7 验证（decode / TTFT / `cache_fetch HIT`），
  confirm 后再切生产；EXP-9 / EXP-8 收官窗口期间尤其要隔离。

---

## 11. Ornith-1.5 上游与引擎侧优化进展（调研 2026-09-20）

针对 §3 识别的不足（Metal 压力 / decode 慢 / 无投机解码），Ornith 侧同样有可用更新。与 §10 不同，
**Ornith 本体无新版本**（仍 1.5），杠杆在 **MTP 头 / 量化 / 引擎**三侧。

### 11.1 上游更新盘点

| 项 | 状态 |
|---|---|
| Ornith-1.6 / 2.0 | **无**（仍 1.5；base 仓近 6–12 天有小更新，非新版本） |
| 官方 MLX 量化 | **新增** `ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit`（此前仅 bf16 `-MLX` 69.3GB） |
| 官方 MTP 层 | 1.5 **自带** `mtp_num_hidden_layers: 1` 真实权重（1.0 无）——**但官方头质量差**：独立实测 `mtp.*` std 恰为 0.0200（= `initializer_range`，随机），acceptance **~13%**（ornith-ai discussion #10），offmonreal 实测 **34–41%** |
| 社区修复头 | `shisa-ai/Ornith-1.5-35B-A3B-MTP-ONLY`（warm-start Qwen3.6 头 + KL 蒸馏）、Qwen3.6 donor 头 → **57–61% 接受率，~2.1× over AR**（skinnyctax 实测 74.3%） |
| 我们的量化 | `pyros-vault/...-oQ4e-fixed-mtp` **即 Shisha-AI 重训头版**；同类新增 `mlx-works/...-oQ4e-mtp`、`-oQ3e-mtp`、`scottlowry/...-oQ4e-mtp` |
| oMLX 侧 | 原生支持 Ornith：**Lightning MTP / DFlash / SpecPrefill / TurboQuant KV / Qwen35 ANE prefill**（`qwen35_ane_prefill_*`，把 GDN prefill 卸载到神经引擎） |

### 11.2 社区实测（同族模型，oMLX + Lightning MTP）

| 硬件 · 工具 | 上下文 | PP tok/s | **TG tok/s** | 峰值 |
|---|---|---|---|---|
| **M5 Pro 48GB · rapid-mlx 0.12.13（本机）** | 40K | ~300 | **~20** | 27/30.2GB |
| M5 Pro 48GB · oQ4e-mtp | 1K | 1591 | **81.6** | — |
| M5 Pro 48GB · oQ4e-mtp | 4K | 1967 | **72.1** | — |
| M1 Max 64GB · oQ4e-fixed-mtp（**同量化**） | 16K | 516 | **59.9** | 25.0GB |
| M5 Max 128GB · oQ4e-mtp | 16K | 3963 | **109.8** | 23.2GB |
| M5 Max 128GB · oQ4e-mtp | 64K | 2265 | **74.6** | 27.3GB |
| llm-bench.io（M5 Max, oMLX） | — | — | 峰值 **113.4**／均值 111.8 | 20.8GB |

→ **同一量化在 M1 Max 16K 即 ~60 tok/s（MTP 开）**，本机 M5 Pro 40K 仅 ~20：差距主要来自
**MTP 未生效 + 48GB 内存压力**，而非模型本身。

### 11.3 针对 §3 三个不足的对应

| 不足 | Ornith 侧更新 | 可行路径 |
|---|---|---|
| **decode 慢**（MTP 未生效） | oMLX "Lightning MTP" 实测 60–115 tok/s；社区修复头 ~2.1× | ① oMLX 引擎（MTP 可开）；② rapid-mlx `--force-spec-decode` + DFlash |
| **无 drafter / broken MTP** | 官方头坏、社区头已修；**z-lab/Qwen3.5-35B-A3B-DFlash 可作 Ornith drafter**（NVIDIA 论坛实测成立） | rapid-mlx **#477**：hybrid MTP 仍被拦截（`BatchGenerator._step` 缺失，MTP install 短路）；**但 `--force-spec-decode` 对 DFlash/suffix 路径仍有效** → 走 DFlash |
| **Metal 压力/驱逐** | 模型更新不解；0.13.4 起 per-model Metal 上限按可用内存推导、0.14.2 hybrid recurrent-state checkpoint | §5 手段 + **升 0.14.3**（hybrid 优化同适用于 Ornith，同属 Qwen3.5-class GDN） |
| prefill ~300 | oMLX **ANE prefill**；rapid-mlx 0.13.0 自调 recurrent prefill + fused GDN Metal kernel | 升 rapid-mlx；或评估 oMLX |

### 11.4 建议动作（按性价比）

1. **升 rapid-mlx 0.12.13 → 0.14.3**：hybrid recurrent-state checkpoint + 自调 GDN prefill +
   fused kernel 对 Ornith 直接适用（同走 `hybrid-cache` 路径），**零改模型**。
2. **试 `--force-spec-decode` + DFlash drafter**（`z-lab/Qwen3.5-35B-A3B-DFlash`）：绕过 #477 的
   hybrid-MTP 拦截，走 DFlash/suffix 路径。本仓 2026-08-26 已测 dflash-mlx 于 Ornith ~91 tok/s
   （短线无净增益），但那是**独立引擎**；并入 rapid-mlx 后能否叠加 prefix cache 需重测。
3. **评估 oMLX 作为备选引擎**：原生 Ornith 支持、Lightning MTP 实测高、ANE prefill；OpenAI 兼容，
   代理层改动小。成本 = 换栈 + 回归。
4. **Metal 压力**：先 §5 的 `--pin-system-prompt` + `iogpu.wired_limit_mb` + 降 cache；若仍驱逐，
   说明 48GB 装 20GB 权重 + 12GB cache 结构性吃紧，需降量化档（`mlx-works/...-oQ3e-mtp`）。
5. 可换 `mlx-works/...-oQ4e-mtp` 或官方 `-MLX-4bit` 做对照（注意 mlx-works 卡**建议 MTP 默认关**）。

### 11.5 风险

- **MoE A3B 的 MTP ROI 存疑**：mlx-lm #990 复现显示 35B-A3B MoE 加 MTP **−5%~−8%**（baseline 已快，
  MTP 头本身是全 MoE 层，固定成本高）；dense 27B 才有 +31%。与 oMLX 高 TG 数字矛盾，**须自测**。
- MTP 头质量参差（官方 13% vs 社区 57–61%），换量化需核实头来源。
- oMLX 是不同栈，集成/回归成本；EXP-9 / EXP-8 窗口期不宜大改。

---

## 附录：关键文件与命令

```
配置        configs/ornith-oq4e.conf ｜ 备选 configs/qwen3.8-27b-4bit.conf
相关行动手册 docs/05-operations-changelog/exp9-closeout-runbook-20260920.md（EXP-8 换模至 3.8）
KV 量化映射  manage.sh:611-614（--kv-cache-quantization --kv-cache-quantization-bits）
GPU 安全门   manage.sh:534-547（DEF-006，>0.85 拒绝，0.80-0.85 警告）
后端日志     logs/llama-server.log
会话诊断     logs/diag/sessions.jsonl
进程命令     ps -o pid,rss,command -p <backend_pid>
内存压力     memory_pressure / sysctl iogpu.wired_limit_mb / sysctl vm.swapusage
状态 API     curl http://127.0.0.1:4000/api/status
指标 API     curl "http://127.0.0.1:4000/metrics?n=30"
```

### 参考

- rapid-mlx 0.12.12 `serve --help`（`--pflash*`、`--pin-system-prompt`、`--kv-cache-dtype`、`--metal-cap-kv-bytes-per-token` 等）
- rapid-mlx 源码：`vllm_mlx/pflash.py`、`vllm_mlx/kv_estimation.py`、`vllm_mlx/kv_quant_gate.py`
- rapidmlx.com/docs/perf-flags（PFlash / MTP / KV 压缩）、rapidmlx.com/docs/cli
- `docs/05-operations-changelog/ornith-15-integration-20260826.md`（集成与基线）
- `docs/06-reference-metrics/BENCHMARK.md` §Qwen3.8-27B 基线与三方同档对比（2026-08-15）
- `docs/05-operations-changelog/dflash-mlx-integration-20260826.md`（hybrid 前缀缓存 + 3.8/35B 优化）
- `docs/05-operations-changelog/qwen3.8-dflash2-smoke-20260829.md`（3.8 decode 口径）
- `docs/04-analysis-diagnostics/rapidmlx-kvcache-mechanism-20260903.md`（hybrid non-trimmable 机制）
- rapid-mlx 官方 changelog（0.13.x/0.14.x 发布说明）：`https://rapidmlx.com/docs/changelog`
- rapid-mlx Qwen 家族别名页（MTP/混合精度/解析器变更）：`https://rapidmlx.com/docs/models/families/qwen`
- 上游同型问题：vLLM #53670（EAGLE/MTP prefix-cache last-block drop on hybrid Qwen3.8 GDN）
- rapid-mlx Ornith 家族页（0.13.0 起别名，仅 bf16）：`https://rapidmlx.com/docs/models/families/ornith`
- rapid-mlx #477（hybrid GDN MTP 拦截与 `--force-spec-decode` DFlash 路径）
- Ornith MTP 头质量实测：`hipfire-models/ornith-1.5-35b-a3b`、`offmonreal/Ornith-1.5-35B-MaxQuality-MTP-GGUF`、ornith-ai discussion #10
- MTP 效果参考：mlx-lm PR #990（dense +31%、MoE A3B −5~−8%）
- oMLX 社区跑分：`https://omlx.ai/benchmarks/`（Ornith-oQ4e-mtp / -fixed-mtp 多机型）、llm-bench.io Ornith-1.5-35B-A3B-oQ4e-mtp
- 外部：explainx.ai Ornith-1.5 评测综述、DGX Spark NVFP4 实测、llm-bench.io、CrucibleMark、srmiles/local-llm-benchmarks（MTP 接受率）
