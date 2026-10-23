# Splash 引擎与模型能力调研（2026-10-20）

> 回答一个常见误解：**Inco AI 的 Splash 是否训练了「评分更高」的模型？**
> 结论：**没有**。Inco 不训练目标模型，只训练投机解码草稿；Splash 包的权重与 Qwen 原版逐字节一致，能力完全相同，提升的只有速度。

---

## 一、结论摘要

| 问题 | 答案 | 依据 |
|------|------|------|
| Inco 训练了目标模型吗？ | **没有** | `incoai` 组织全部 12 个仓库中无原创目标模型 |
| Splash 包与 Qwen 原版权重不同吗？ | **逐字节一致**（27B 仅 48 个 GDN decay 向量有 float ULP 级差异） | `docs/performance.md`（incoai/splash） |
| 有 Splash 版的 SWE-bench / Terminal-Bench 分数吗？ | **没有**，官方只公布 tok/s | 官方 launch post / 模型卡 |
| 质量有提升吗？ | **无**；官方自述 perplexity **高于** llama.cpp 0.1–0.9% | `docs/performance.md` |
| Splash 的实际价值 | **速度**（decode 2.5–5.3× llama.cpp，4 路并发最高 3.9×） | 同上 |

**一句话**：Splash 是「让 Qwen 跑得更快」的引擎，不是「让 Qwen 变得更聪明」的模型。

---

## 二、`incoai` 仓库的真实构成（2026-10-20 全量）

| 类型 | 仓库 | 说明 |
|------|------|------|
| **Splash 包** | `Qwen3.8-27B-Splash`、`Qwen3.6-35B-A3B-Splash` | Qwen 权重**打包**（4-bit target + DFlash2 draft + vision + tokenizer），不可在 Transformers/MLX 加载 |
| 量化 | `GLM-5.3-NVFP4` | 第三方模型量化 |
| **DFlash2 草稿**（Inco 唯一训练产物） | `Qwen3.8-27B-DFlash2`(346k dl)、`Qwen3.6-35B-A3B-DFlash2`、`Qwen3.8-Flash-Next-DFlash2`、`GLM-5.3-DFlash2`、`GLM-5.3-Flash-DFlash2`、`Muse-Glimmer-30B-DFlash2`（含 GGUF 变体） | 1.0–5.5 GB 的块扩散草稿，仅提议 token |

> ⚠️ **2026-10-20 复核**：splash 1.3.2 的 `install/families.py` 中 `FAMILIES` **仅注册 2 个家族**（Qwen3.8-27B、Qwen3.6-35B-A3B）。Flash-Next / GLM-5.3 / Muse-Glimmer 的 DFlash2 草稿虽已发布，但**尚未接入 1.3.2**，属于前瞻性产物。

---

## 三、DFlash2 = 唯一的「训练」

DFlash2 是一个 **5 层、block size 16 的块扩散草稿网络**：

- 职责：一次前向预测 16 个 token 的候选块
- 目标模型并行验证，接受正确的、纠正错误的
- **不是独立模型**，不能单独服务
- 不改变能力，只改变速度（验证阶段保证分布）

因此「Splash 训练了更强的模型」这一说法从机制上就不成立。

---

## 四、官方速度基准（M5 Pro 48GB，SPEED-Bench 编码题，1024 输出）

| 指标 | Qwen3.6-35B-A3B | Qwen3.8-27B |
|------|-----------------|-------------|
| Decode 短 prompt | 210 tok/s（1.7×） | 74 tok/s（2.0×） |
| Prefill 32K | 2011 tok/s（1.3×） | 363 tok/s（1.2×） |
| 缓存 TTFT 32K 重放 | 123 ms（6.6×） | 282 ms（7.3×） |
| 4 路并发短 prompt | 357 tok/s（2.0×） | 170 tok/s（3.9×） |
| 4 路并发 32K | 236 tok/s（3.8×） | — |

对比 llama.cpp（同一 Unsloth UD-Q4_K_M，Metal）：

| Decode tok/s | M5 Pro 20c | M3 Max 40c |
|--------------|-----------:|-----------:|
| 35B-A3B · Splash | 175 | 209 |
| 35B-A3B · llama.cpp | 69 | 66 |
| 27B · Splash | 74 | 92 |
| 27B · llama.cpp | 16 | 17 |
| 27B · llama.cpp + MTP | 27 | 20 |

即 **35B 快 2.5–3.2×，27B 快 4.5–5.3×**（对 MTP 为 2.7–4.6×）。

---

## 五、质量证据（官方自述，非第三方）

| 指标 | 27B | 35B-A3B |
|------|-----|---------|
| Next-token 一致率（vs llama.cpp） | 99.30–99.45% | 97.83–98.14% |
| Perplexity（vs llama.cpp） | **+0.1–0.4%** | **+0.1–0.9%** |
| llama.cpp CPU vs Metal（对照） | 97.8% | 96.5–96.9% |

- KV 用 BF16 + `--disable-ane` 时取上表；默认 INT8 缓存为 99.23–99.25% / 97.92–97.94%
- 差异位置均为「近平局」（前两名候选仅差 0.03–0.10 nats）
- **没有任何证据表明 Splash 包能力高于 Qwen 原版**

---

## 六、本机实测（M5 Pro 48GB，splash 1.3.2，MLX 上游权重 + DFlash2）

2026-10-20 实测，两档均已启用 `--state-format bf16 --language-only --persistent-cache`：

| 指标 | Qwen3.8-27B（稠密） | Qwen3.6-35B-A3B（MoE 3B 激活） |
|------|--------------------|-------------------------------|
| 冷 prefill 30.4K | 450–488 tok/s | **2122 tok/s（4.4×）** |
| 热 TTFT 30.4K | 0.53–0.56 s | **0.27 s** |
| Decode @30K | 65–71 tok/s | **123–140 tok/s** |
| Decode 短 prompt | — | **335 tok/s** |
| DFlash2 接受率 | 65.7% | 55.9%（短 prompt 99%） |
| Metal 占用 | 19.0 GB | 21.9 GB |
| state/条（bf16） | 80.6 MiB | **34 MiB** |
| 内存压力 | normal | normal |

> 注：27B 的热 TTFT 0.53s / 冷 prefill 450–488 已是 DFlash2 + persistent-cache + ANE split 后的上限；再压只能降量化，收益微弱（4bit 16.1GB / oQ4 16.7 / mxfp4 15.2，带宽瓶颈不变）。

---

## 七、48GB 上的「能力 vs 速度」两难

| 模型 | 架构 | Agentic | SWE-Pro | LiveCodeBench | 速度 | 48GB | splash 1.3.2 |
|------|------|--------:|--------:|--------------:|------|------|--------------|
| **Qwen3.8-27B** | 稠密 27B | **63.3** | 61.7 | 90.3 | 65–71 | ✅ 15GB | ✅ |
| Qwen3.6-35B-A3B | MoE 35B/3B | 30.2 | 49.5 | 80.4 | **175–210** | ✅ 20.4GB | ✅ |
| Qwen3-Coder-30B-A3B | MoE 30B/3B | 强（社区 95.8%） | — | — | ~130–180 | ✅ | ❌ `qwen3_moe` |
| Gemma 4 26B A4B | MoE 26B/4B | 中 | — | — | ~79 | ✅ | ❌ |
| Muse Glimmer-30B | 稠密 29.6B | SWE-Pro 51.2 | — | — | 22–50 | ✅ | ❌ |
| GLM-5.3-Flash | MoE 320B/18B | **84.3**（TB2.1） | — | — | ~50（云端） | ❌ 328GB | ❌ |
| Qwen3.8-Flash-Next | MoE 125B+51B/6B | — | — | — | — | ❌ 80–111GB | ❌ |

**结论**：48GB 上不存在「agentic 强于 Qwen3.8-27B 且 ≥80 tok/s」的单一模型。

- 稠密 27–30B 受带宽墙限制，DFlash2 后上限 65–71
- ≥80 tok/s 必须是 3–6B 激活的 MoE，能力天花板在 Qwen3.6-35B-A3B 档
- 真正「又快又强」的 GLM-5.3-Flash（320B/18B）与 Qwen3.8-Flash-Next（125B/6B）都需要 80GB+ 统一内存

---

## 八、建议

| 场景 | 方案 |
|------|------|
| 日常/快速迭代 | `splash-qwen36-35b-mlx`（MoE，335 tok/s 短 prompt，热 TTFT 0.27s） |
| 复杂重构/多文件调试 | `splash-qwen38-27b-mlx`（agentic 强一档） |
| 想要「又快又强」 | 本地无解；走代理已有 cloud 路由（`PROXY_ROUTE_ENABLED` + `PROXY_ROUTE_THRESHOLD_CHARS`） |
| 能力真正升级 | 等 Qwen4 small（2026-10-10 @QwenDevs 预告「big or small?」→「let's do both!」，未确认） |

---

## 九、参考

- Inco AI 博客 `Splash: A Local Engine Built Around the Model`（2026-09-17）
- 迁移操作与版本特性对照：[`docs/05-operations-changelog/splash-132-migration-20261020.md`](../05-operations-changelog/splash-132-migration-20261020.md)
- `incoai/splash` → `docs/performance.md`、`dev/benchmarks/upstream-loading.md`
- HF 模型卡：`incoai/Qwen3.8-27B-Splash`、`incoai/Qwen3.6-35B-A3B-Splash`
- Qwen 官方模型卡：`Qwen/Qwen3.8-27B`（Coding / Agent 评测表）
- 第三方对比：benchlm.ai `qwen3-6-35b-a3b-vs-qwen3-8-27b`、agent12 writeup（2026-08-18，M5 实测 8/8 同分但 16.9× 时差）
- Z.ai `GLM-5.3-Flash` 模型卡与博客（2026-08-26）

---

**维护备注**：若 Inco 后续把 GLM-5.3-Flash / Qwen3.8-Flash-Next 接入 `FAMILIES`，需重新评估第 7 节的可行性（当前受 48GB 内存限制仍然不可行，除非换机）。
