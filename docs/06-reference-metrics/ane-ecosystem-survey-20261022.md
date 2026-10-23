# ANE 目标场景与应用生态 · 调研综述

> 调研日期：2026-10-22
> 范围：ANE 设计目标、能力边界、开源项目生态、学术研究、对本项目（llama.cpp 本地推理栈）的借鉴点
> 数据来源：GitHub API 实查（star 数）、HF 实查（下载量）、Splash 1.4.0 `DEVELOPMENT.md`、Orion 论文（arXiv 2603.06728）、ANE 逆向工程综述（alphaXiv 2606.22283）、llama.cpp Discussion #336

---

## 目录

1. [ANE 的设计目标场景](#一ane-的设计目标场景)
2. [能力边界](#二能力边界)
3. [开源项目生态](#三开源项目生态)（含 3.5 决策模型品类 JEV 类的 ANE 部署实例）
4. [学术研究](#四学术研究)
5. [社区关键结论](#五社区关键结论)
6. [对本项目的借鉴点](#六对本项目的借鉴点)
7. [总结](#七总结)

---

## 一、ANE 的设计目标场景

Apple 从 A11（2017）到 M5，ANE 的定位始终未变——**高频调用、固定 shape、低功耗的端侧推理**。

### 1.1 Apple 官方场景

| 官方场景 | ANE 发挥作用的特征 |
|----------|--------------------|
| FaceID | 固定输入，每秒多次，不能唤醒 GPU |
| Siri / 语音识别 | 常驻监听，功耗敏感 |
| 照片语义搜索 | 端侧 embedding |
| 视频人像模式 / 实时字幕 | 每帧推理，与 GPU 渲染并行 |
| Apple Intelligence 端侧模型（~3B） | 苹果自己的 LLM 也只把**部分层**放 ANE |

### 1.2 设计哲学

ANE 不是"更快的 GPU"，而是"**不打扰别人的专用协处理器**"：

> zero idle power — the ANE is hard power-gated when unused… **dedicated silicon — ANE inference leaves the GPU and CPU entirely free for other workloads**（Orion 论文，arXiv 2603.06728）

两个关键属性：

- **零空闲功耗**：不用时硬断电，适合常驻小模型
- **专用硅片**：ANE 工作时 GPU/CPU 完全空闲，可并行干别的

---

## 二、能力边界

社区共识的能力矩阵：

| 能力 | 结论 | 说明 |
|------|------|------|
| **大 GEMM（prefill FFN）** | ✅ 最强项 | INT8 W8A8，算力密度高 |
| **固定 shape 小模型** | ✅ 主场 | embedding / classifier / VAD |
| **decode（1 token/步）** | ❌ | IOSurface 派发开销压死收益 |
| **attention / concat / 动态 KV** | ❌ | 编译失败或不支持 |
| **训练** | ⚠️ | 官方不支持；有逆向实现（`maderix/ANE`） |
| **自定义算子** | ❌ | 完全黑盒 |

> "Kind of a very limited GPU with an extremely stubborn bouncer at the door."
> —— llama.cpp Discussion #336

### 2.1 精度与算子限制

| 项 | 说明 |
|----|------|
| 数据类型 | 仅 FP16 / INT8（W8A8），无 FP32 |
| GELU | 只认 tanh 近似式 |
| Concat | **直接编译失败**，多张量操作必须拆成独立 program |
| 动态 shape | ❌ 静态编译，每个 shape 一个私有程序产物 |
| 控制流 | ❌ 无 if/loop |
| 数值偏差 | INT8 相对 GPU 留 2.5–2.8%（Splash 实测，Qwen3.8-27B） |

### 2.2 内存带宽对 decode 的致命影响

Orion 论文实测（M4 Max，GPT-2 124M）：

```
ANE decode: 170 tok/s   ← 被 IOSurface 派发开销拖死
CPU:        283 tok/s   ← CPU 都比 ANE 快
GPU:        更快
```

**每步 1 token 的派发成本 > 计算收益**——这与 Splash 的 `minimum_rows=512` 设计完全吻合。

---

## 三、开源项目生态

### 3.1 第 1 层：Apple 官方（`apple-aiml-research` / `apple` org）

| 项目 | ★ | 定位 |
|------|--:|------|
| `ml-stable-diffusion` | **17,968** | SD 官方 Core ML 版，ANE + GPU 混合 |
| `coremltools` | 5,437 | 转换 / 量化 / 校验工具链 |
| `ml-ane-transformers` | 2,747 | **Transformer 上 ANE 的参考实现**（reshape 技巧的源头） |
| `ml-vision-transformers-ane` | 94 | ViT 变体 |
| `coreml-depth-anything-v2-small` | — | 深度估计（HF 37.7K 下载） |

### 3.2 第 2 层：生产级应用（真实用户规模）

| 项目 | ★ / 下载 | 说明 |
|------|--:|------|
| **`argmaxinc/whisperkit-coreml`** | **11,962,225 下载** | **ANE 语音识别的事实标准** |
| `FluidInference/parakeet-tdt-0.6b-v3-coreml` | 296,382 | NVIDIA Parakeet 移植到 ANE |
| `FluidInference/silero-vad-coreml` | 46,105 | VAD |
| `FluidInference/speaker-diarization-coreml` | 23,896 | 说话人分离 |
| `MochiDiffusion/MochiDiffusion` | 7,971 | Mac 原生 SD 客户端 |
| `FluidAudio` | 3,159 | CoreML 音频全家桶（TTS/ASR/VAD） |
| `ggml-org/whisper.cpp` | 54,544 | `--coreml-encode`：**只有 encoder 走 ANE**，decoder 留 CPU/GPU |

**模式清晰可见**：生产级 ANE 应用全部是「**固定 shape 的 encoder + 低功耗常驻**」——语音、VAD、分离、embedding。没有一个是把 LLM decode 放上 ANE 的。

### 3.3 第 3 层：逆向 / 底层研究（最活跃的方向）

| 项目 | ★ | 说明 |
|------|--:|------|
| **`maderix/ANE`** | **7,277** | **用逆向出的私有 API 在 ANE 上训练**——打破"ANE 不能训练"的铁律 |
| `hollance/neural-engine` | 2,546 | 《Everything we actually know about the ANE》社区知识库 |
| `eiln/ane` | 541 | **Linux 上的 ANE 驱动**（逆向固件协议） |
| `mizorewww/laya-coreml` | 1,595 | 经校验的 CoreML/ANE 移植集 |

学术论文：

- **`arXiv 2603.06728`《Orion》**：在 ANE 上做 LLM 训练与推理的系统化表征
- **`alphaXiv 2606.22283`**：ANE 架构、编程与性能的逆向工程（roofline：M1 ANE 的 ridge point ≈ 141 FLOP/byte）

### 3.4 第 4 层：社区运行时（尝试绕过 CoreML）

| 项目 | ★ | 思路 |
|------|--:|------|
| `christopherkarani/Espresso` | 173 | **Swift 直接写 ANE，绕过 CoreML** |
| `mechramc/Orion` | 130 | ANE 上训练 + 跑小 LLM 的本地 runtime |
| `john-rocky/CoreML-LLM` | 201 | LLM on ANE + GPU 混合 |
| `johnmai-dev/ANE-LM` | 145 | ANE LLM 推理 |
| `smpanaro/more-ane-transformers` | 62 | ml-ane-transformers 的 LLM 扩展 |

### 3.5 决策模型品类（JEV 类）—— 已有 ANE 部署实例 ⭐

「固定 shape 小模型」里与我们最相关的品类是 **JEV 类 System-1 决策模型**（三原语
`noul`/`score`/`choice` + 逐选项校准概率、单次前向、不生成文本）。它**已经有 ANE/CoreML 部署版**：

**`FluidInference/jev-9b-coreml`**（2026-10-10 更新，253 下载）

```
base_model : autotrust/JEV-9B（= Qwen3.5-9B + System 1 LoRA + 决策头）
pipeline   : image-text-to-text
library    : coreml
tags       : [coreml, apple, decision-model, jev, system-one,
              typed-decisions, calibrated-probabilities, qwen3.5]
```

#### 工程手法（全部命中本文总结的 ANE 模式）

| 手法 | 具体做法 |
|------|---------|
| **静态序列分桶** | 8 个 decoder part（每 part 4 层），每个只有 `L256` / `L512` 两个 function |
| **权重精度** | 8-bit per-channel 权重 + fp16 compute |
| **决策头缩减** | `lm_head` 只保留 **24 个 verbalizer 行**（false/true、0–5、A–P）→ 头仅 4.3 MB |
| **校准内置** | per-slot bias + **per-kind temperature**（对应 jev 原则「校准比准确率重要」） |
| **host 侧预处理** | embedding gather、RoPE 的 cos/sin 表、vision patch 重采样都在 host 做 |
| **vision 分桶** | VisionTower 只有 P256 / P576 两个 patch 档；官方明说 fp32（fp16 对该 ViT 不够准） |

运行时：FluidUse 仓库的 `JevManager`（Swift，macOS 15+），附 SwiftUI demo——模型自己玩超级马里奥 1-1 关。

#### JEV 家族规模（需求真实存在）

```
autotrust/JEV-27B-VL      2,110,389 下载   ← 最高
autotrust/GEV-26B-Decide  1,146,465
autotrust/JEV-35B           648,100
autotrust/JEV-9B            519,717
autotrust/JEV-27B           300,270
```

但 **ANE/CoreML 版目前只有 9B 这一个**（FluidInference 出品，与其 parakeet/silero 同一套路）。

#### 更轻的替代

`eightman999/open-jev-coreml`（596 MB `jevlite.pt`）——名字带 coreml 但实为 PyTorch 文件，需自行转换且无文档。

#### 四个限制

| 限制 | 说明 |
|------|------|
| **采用量极小** | 253 下载 vs WhisperKit 的 11.96M——刚起步 |
| **并不小** | 9B 带 vision 全家桶 ≈ **12 GB**（decoder 6.5 + vision 3.4 + embedding 2.0）；是 ANE 上限场景而非轻量场景 |
| **运行时是 Swift** | `JevManager` 在 FluidUse（macOS 15+），**无官方 Python 路径**；接入 Python 栈需 Swift HTTP 桥或子进程 |
| **fp32 vision tower** | 官方明说「fp16 对这个 ViT 不够准」→ vision 部分不能压缩 |

#### 通用参考（更小的决策头）

不带 vision、只做文本三分类时，CoreML 生态有成套的小模型模板（转换手法可抄）：
`all-MiniLM-L6-v2-coreml`（237 下载）、`bert-base-multilingual-ner-coreml`（198）、
`image-safety-classifier-coreml`（38）——均为「固定 shape + 小头 + 分类」标准形态。

---

## 四、学术研究

### 4.1 Orion（arXiv 2603.06728）

首个系统化表征 ANE 用于 LLM 训练与推理的工作：

- 给出 ANE 的 IOSurface 派发开销是 decode 不可行的定量根因
- 指出 ANE 的三大优势：零空闲功耗、专用硅片（不抢 CPU/GPU）、固定 shape 高效

### 4.2 ANE 逆向工程综述（alphaXiv 2606.22283）

基于私有 runtime / 编译器 / 驱动 / 固件的静态分析与实测：

- ANE 是 **fixed-function 数据流阵列**，MAC 阵列在出厂时按拓扑连线
- roofline：算术强度低于 ridge point（M1 ≈ 141 FLOP/byte）的操作受**内存带宽**限制，高于则受 **MAC 阵列**限制
- decode 属于前者（强度低），dense conv 属于后者

### 4.3 Splash 的工程实践（我们正在用的引擎）

Splash 1.4.0 `DEVELOPMENT.md` 记录了把 ANE 用于 **dense LLM prefill FFN split** 的完整工程：

- 按 intermediate channel 切，34 units（512 通道/unit）
- 校准公式 `T(s) = max(A(s), G(s) + u·A(s))`，收益门槛 5%
- 校准结果按 `(Mac, 模型, macOS, 引擎构建, 内存上限)` 缓存到 `remembered-*`
- 启动时逐 function 做数值校验（偏差 ≤ ANE 增量的 10%，实测 2.5–2.8%）

---

## 五、社区关键结论

### 5.1 decode 上 ANE 是死路

Orion 论文定量证明了根因（IOSurface 派发开销），Splash 用 `minimum_rows=512` 从设计上规避，社区用"CPU 都比 ANE 快"的实测反复验证。**三方结论一致。**

### 5.2 matmul 可以赢，但要看对比对象

> 100 个 matmul 在 M2 上 ANE 比 GPU 快 4×（217ms vs 1316ms）

但对比对象是 llama.cpp 的手写 GPU kernel，不是 CoreML 自己的 GPU 后端。**ANE 赢的是"CoreML-GPU"，不是"最优 GPU kernel"**。

### 5.3 ANE 的带宽会拖累 GPU

Splash `DEVELOPMENT.md`：

> the GPU's part runs a tenth to a third longer beside the ANE's

即校准公式里的 `u` 系数——**ANE 跑的时候 GPU 会慢 10–33%**。这也是为什么 share 不能给满。

### 5.4 训练已被逆向出来，但不可靠

`maderix/ANE` 用私有 API 在 ANE 上做反向传播（7,277★ 说明需求真实存在），但：

- 依赖未公开的私有框架
- Apple 每次系统更新都可能破坏
- **不适合生产栈**

---

## 六、对本项目的借鉴点

### 6.1 【已验证，保持】ANE 只做 prefill 大 GEMM

我们当前的用法（dense 27B prefill FFN、`share 0.53`、`min_rows 512`）与全社区最佳实践一致。**不要尝试**：

- decode 上 ANE
- attention / concat 上 ANE
- MoE 上 ANE

### 6.2 【新机会】把 `embedgemma2` 迁到 ANE

当前 `embedgemma2`（295MB embedding 模型）跑在 **CPU**（:8083）。

按 FluidInference 的模式（silero-vad 46K 下载、parakeet 296K 下载），**embedding 模型是 ANE 的完美目标**：

```
现状: embedgemma2 → CPU（~0.3GB，抢 CPU 核）
可试: embedgemma2 → CoreML → ANE（零 CPU 占用，零空闲功耗）
```

**收益**：embedding 调用不与 LLM 抢 CPU；ANE 本来就为「固定 shape + 小模型 + 高频」设计。

### 6.3 【借鉴模式】`remembered-*` 校准缓存

Splash 的五元组缓存值得我们在 **BVR 自调优**里复用：

```
key = (硬件型号, 模型, 引擎版本, 配置档, 内存上限)
val = 校准出的最优参数
失效条件: 任一元组变化 → 重新校准
```

### 6.4 【借鉴模式】可观测性指标设计

Splash 的 `ane_ffn` 指标族是很好的模板：

| 指标 | 作用 |
|------|------|
| `state`（split/off/stopped） | 一眼看出是否在用 |
| `share` | 实际分担比例 |
| `split_commands` / `ane_ms` | 真实工作量 |
| `reruns` | **失败率**（0 = 健康） |
| `reason` | 为什么没启用 |

我们给代理/引擎加任何硬件加速时，都应该有这五件套。

### 6.5 【借鉴模式】必须有一键降级

```
--disable-ane    → 纯 GPU，一条命令，日志里明确说明
```

任何硬件相关优化都应遵循：**功能可用性 > 性能**，且降级路径要写进启动日志。

### 6.6 【风险提示】不要碰私有 API

`maderix/ANE` 的训练虽然 star 高，但依赖逆向的私有接口。我们的栈是生产服务，**不该引入会随 macOS 更新而碎掉的东西**。

### 6.8 【远期】决策级联的本地 ANE 档（JEV 类）

决策级联（`decision-cascade-rules-jev-escalation-20261013`）目前 Tier 0 走确定性规则、
高阶走 API jev。`FluidInference/jev-9b-coreml` 证明了「JEV 类模型可以整体落在 ANE 上」，
其「静态分桶 + 24 行决策头 + per-kind temperature 校准」就是现成模板：

- **可行**，但当前唯一版本是 9B+vision（≈12 GB）且需 Swift 集成——**暂不落地**
- **触发条件**：出现 4B 级、纯文本、标准 `.mlpackage` 的 JEV CoreML 版（或自转）再评估
- 届时定位：Tier 0.5 本地档（API jej 不可用/超时的兜底），而非替换

### 6.7 【可试】把「静态 shape 分档」用于 FIM 补全

`qwen25-coder-7b-8bit`（:8085，IDE 补全）的输入长度相对固定。按 Splash 的做法，可以预编译几个固定 shape 档位（如 256/512/1024 token），减少动态编译开销。

---

## 七、总结

| 问题 | 答案 |
|------|------|
| ANE 的目标场景 | **固定 shape + 高频 + 低功耗的端侧推理**（语音 / VAD / embedding / vision encoder） |
| LLM 上 ANE 可行吗 | 只适合**小模型的 prefill 部分**；decode 不可行 |
| 最活跃的开源方向 | ① 语音生产栈（WhisperKit 12M 下载）② 逆向训练 / 驱动 ③ 小模型推理 runtime |
| 最大空白 | 没有任何项目把 LLM decode 放上 ANE |
| 决策模型（JEV 类） | **已有 ANE/CoreML 版**（`FluidInference/jev-9b-coreml`，9B+vision≈12GB，Swift runtime）——但 4B 级纯文本版尚缺 |
| 对我们的借鉴 | 保持 prefill 用法；**可试把 embedgemma2 迁到 ANE**；复用校准缓存与可观测性模式 |

---

## 附录 A：本机 ANE 实测（M5 Pro 48GB，Qwen3.8-27B）

| 项 | 值 |
|----|-----|
| FFN 结构 | `intermediate_size = 17,408`（34 units × 512 通道） |
| `share` | **0.5294**（= 18 / 34 units = 9,216 通道给 ANE） |
| `minimum_rows` | 512 |
| 每单位耗时 | ANE 1.7 ms vs GPU 1.1 ms（ANE 慢 1.55×） |
| 2048 行 FFN 层 | 纯 GPU 48.6 ms → split 后 **30.2 ms**（**1.61×**） |
| 数值偏差 | **1.3%**（INT8 相对纯 GPU，官方允许 ≤10%） |
| `split_commands` / `ane_ms` / `reruns` | 311 / 504,529 ms / **0** |

## 附录 B：ANE vs GPU vs NVIDIA

| | ANE（16 核） | M5 Pro GPU（20 核） | RTX 5090 |
|----|--------------|---------------------|----------|
| 架构 | 固定功能数据流阵列 | SIMT + 每核 Neural Accelerator | SIMT + Tensor Core |
| FP16 峰值 | 未公布 | ~30–40 TFLOPS | ~380 TFLOPS |
| 带宽 | 307 GB/s（共享） | 307 GB/s | 1,792 GB/s |
| 数据类型 | FP16 / INT8 | FP16/BF16/FP32/INT8 | FP16/BF16/FP8/FP4 |
| 可编程性 | ❌（Core ML 图） | ✅ Metal/MLX | ✅ CUDA/Triton |
| 动态 shape | ❌ | ✅ | ✅ |
| 训练 | ❌ | 部分（MLX） | ✅ 完整 |
| 空闲功耗 | 0（硬断电） | 低 | 高 |

---

*本文档由 llama.cpp 本地推理栈项目组维护；调研方法与数据源见文首。*
