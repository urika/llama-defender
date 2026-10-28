# Flint 与 Kapitsa 引擎评估记录（2026-10-29）

> **结论先行**：Flint（Recursive 40B-A3B MoE）在本机可跑但**不具备生产替代条件**；Kapitsa 作为引擎在**同族 35B** 上与现产 splash 打平（115.2 vs 116-132 tok/s），KFlash（树验证策略）相对链式验证增益实测仅 **+11.5%**（官方对照 339 tok/s 为别的高端机口径）——**现产 splash + DFlash2 保持不动**。
> 本记录留存四形态的同口径数据与踩坑清单，供后续再评估时免重复劳动。

## 一、背景与目标

- 动机：现产主引擎为 Splash 1.4.0 + Qwen3.6-35B-A3B-OptiQ-4bit（decode 116-132 tok/s，质量门 PASS 93.5%）；考察①新一代模型（Flint）②新引擎（Kapitsa）能否带来实质提升。
- 约束：48GB 统一内存 → **本地引擎与 Kapitsa 二选一**（内存互斥，见 `docs/06-reference-metrics/memory-ledger-arbitration-20261029.md`）；批期禁起第二模型服务器。

## 二、四形态同口径对比（质量门 v3 = 62 条 / 7 类，确定性评分）

| 形态 | decode | prefill | 内存 | 质量门 |
|------|--------|---------|------|--------|
| **现产 splash + Qwen3.6-35B-A3B-OptiQ-4bit** | 116-132 tok/s | 1101-1154 tok/s | 24.4GB | **PASS 93.5%** |
| **Kapitsa + Qwen3.8-27B-4bit + KFlash** | **23-24 tok/s**（plain 17.8） | ~1500 tok/s | 21.1GB（权重 15 + 草稿器 1.6 + KV room 22.2GB） | **PASS 95.2%（最高）**（summary 7/7 全过） |
| Kapitsa + Qwen3.6-35B-A3B-4bit + KFlash | 115.2 tok/s（纯 decode） | ~1500+ tok/s | ~22GB | FAIL 91.9%（summary 4/7 未达线） |
| Kapitsa + Flint native 4bit | 144.0-146.8 tok/s | ~1400 tok/s | 22.6GB | PASS 88.7% |
| llama.cpp master + Flint UD-Q4_K_XL | 69.2 tok/s | ~970 tok/s | 23.7GB | PASS 85.5% |
| Kapitsa + Flint UD-IQ3_XXS（3bit 导入） | 139.5 tok/s | 1644 tok/s | 14.1GB | PASS 82.3% |
| （参照）llama.cpp + Qwen3.6-35B-A3B-4bit | — | — | — | — |

关键读数：
- **Flint 天花板 = 147 tok/s（plain decoding）**：Kapitsa 官方文档明言「Dory and Flint have none: they decode without one」——Flint **无任何草稿器（无 DFlash2/DFlash 草稿、无 MTP head）** → KFlash 作为验证策略没有草稿可验、MTP 亦不可用 → 无投机加成空间（术语层次见 §三·五 注）。
- **Flint 推理质量弱于现产**：专项探针（8 题确定性答案 + 开思考 + 足额预算）现产 8/8（思考 990-1583 字有界、均 2.8s），Flint IQ3 7/8 且 **R5 自指逻辑题思考死循环**（8192 预算下思考 9211 字、答案 0 字、63s——加预算仍不收敛）。
- **Kapitsa + KFlash 对同族 35B 只 +11.5%**：纯 decode 差分 115.2（KFlash on）vs 103.3（`NI_KFLASH=0` 链式）；官方对照「Kapitsa+KFlash 339 > Splash+DFlash2 281」为别的高端机口径，本机 M5 Pro 上不成立。

## 三、踩坑清单（复用价值最高的部分）

1. **Kapitsa 自带 pull 对大文件必卡死**：进程存活、进度条永远「0 KB / 0.0 MB/s」、`.part` 逻辑尺寸满但 `du` 仅 80K（**稀疏预分配假象**）。**根因**：Kapitsa 走 `HF_ENDPOINT` 环境变量——本机全局设为 `HF_ENDPOINT=hf-mirror.com`（实测仅 9KB/s，且 HF 元数据 API 路径超时）→ pull 拉 `huggingface.co` 直连（1.8MB/s）或改用 ModelScope 镜像自研并行下载。
2. **HF 元数据 API 在本环境不可用**（`resolve`/CDN 通、`api` 超时）→ `kapitsa serve --model <hf-repo>` 会报「Can't download the file list」；解法是喂本地目录（HF 快照含完整布局：分片 + `model.safetensors.index.json` + `chat_template.jinja`）。
3. **ModelScope 镜像可用且快**：`mlx-community/Qwen3.6-35B-A3B-4bit`、`unsloth/flint-GGUF`、`RecursiveSI/flint-int4` 均已镜像；并行断点续传可跑到 ~16MB/s（单连接 4MB/s）。**注意 `curl -sIL` 的 content-length 有时取空** → 改用 ModelScope 文件 API 取精确尺寸。
4. **思考开关是质量门的必要条件**：Flint 默认 thinking-on 会烧完 768 max_tokens 预算致正文空 → 门首跑 83.9% FAIL（6 条空响应假象）→ llama-server `-rea off` 后 85.5% PASS。与 splash 当年同源教训。
5. **llama.cpp 构建缺 UI 资产会挂**：`tools/ui` gzip 步骤（缺 CSS 文件）→ 配置加 `-DLLAMA_UI_GZIP=OFF` 解决。
6. **Kapitsa 导入需支持的专家量化格式**：仅接受 IQ1_S/IQ2_XXS/IQ2_S/IQ3_XXS/IQ3_S 或全 Q8_0/Q6_K；unsloth **UD-Q4_K_XL（IQ3_S/Q5_K 混合）不可导入** → 只能走 IQ3_XXS（3bit，质量 -3.2pp）或官方原生 4bit。

## 三·五、Qwen3.8-27B 专项（2026-10-29 03:3x-04:0x，Kapitsa）

**社区对照**（r/LocalLLM 同机帖，M5 Pro 64GB，Qwen3.8-27B 4-bit + 草稿器，400 输出 token、reasoning 关）：

| 运行时 | decode | 备注 |
|--------|--------|------|
| TensorFold beta.8 | 36.0 tok/s | 14.98GiB + 3.6GB 草稿器 |
| **Kapitsa 0.2.3** | **34.3 tok/s** | **含 MTP head + DFlash2（~20.2GB 包）** |
| LM Studio 0.4.26+4 | 33.5 tok/s | Splash 引擎 |
| oMLX 0.7.2 → Splash | 33.2 tok/s | |
| Splash 1.3.2 | 30.3 tok/s | unsloth Q4_K_M |
| MLX-Serve | 30.1 tok/s | |
| Ollama 0.40.6 | 27.0 tok/s | |

Kapitsa 官方 `docs/benchmarks.md`（口径：4K prompt + 512 token 答案、T 0.6、rate 取流中段）M5 Pro 48GB 行：27B decode 与 mlx-serve 相当（77 vs 78）。

**本机实测**（M5 Pro 48GB，`mlx-community/Qwen3.8-27B-4bit` + 官方 `incoai/Qwen3.8-27B-DFlash2` 草稿器）：

| 配置 | decode | 判读 |
|------|--------|------|
| plain（`--dflash none`） | 17.8 tok/s | 稠密 27B（64 层 × 5120，无专家）4bit 的带宽天花板 ≈ 13.5GB/token ÷ ~273GB/s ≈ 20 tok/s → **已触顶 91%** |
| 链式 DFlash2（`NI_KFLASH=0`） | 21.5 tok/s | 投机有效但增益有限 |
| KFlash 树验证 | 23.2-24.1 tok/s | T=0 / T=0.6 无差异 |
| 质量门 v3 | **PASS 59/62 = 95.2%** | **所有受测模型最高**；summary 7/7 全过（optiq 5/7、Flint 4-5/7、9B 2/7） |

**与社区差距的已知项**：我们的包**缺 MTP head**（社区包 20.2GB 含 MTP 239MB；Kapitsa 日志明示 "no MTP head … speculative decoding off"）→ MTP 快路径不可用，只剩 DFlash2；草稿器本身与模型词表一致（均 248320），"reduced draft head 98304 ids" 是 Kapitsa 的设计而非错配。即便追平社区 34.3 tok/s，也仅为现产 35B 的 1/4。

### MTP head 补齐与实测（2026-10-29 03:5x，已闭环）

**取源头**：MTP 张量不在 mlx-community 的 4bit/8bit 权重仓（2180 张量、0 个 `mtp.*`），而在**官方 base 仓 `Qwen/Qwen3.8-27B`**——15 个 `mtp.*` 张量（`mtp.fc`、`mtp.layers.0.*`、`mtp.norm`、`mtp.pre_fc_norm_*`）全部集中在最后一个分片 `model-00018-of-00018.safetensors`（3.39GB）。只下该分片即可（ModelScope 镜像并行 ~3.5 分钟）。

**构建侧车**（Kapitsa 内置子命令，从 safetensors 抽取 mtp 张量）：
```bash
kapitsa mtp-sidecar <本地目录含 index+分片+config> /path/mtp.safetensors
# → wrote ... 15 tensors, 849.4 MB, base_hidden_variant post_norm
cp mtp.safetensors ~/.cache/kapitsa/models/qwen3.8-27b-4bit/
```

**验证**（serve 日志）：
```
MTP head loaded in 659 ms (dense, trunk hidden post-norm, drafts over the full vocabulary)
DFlash2: reduced draft head, 98304 of 248320 ids … 1.61 GB resident
KFlash: tree drafting on the DFlash2 lattice
engine ready: … KFlash speculative decoding (DFlash2 lattice, tree verify), KFlash (DFlash2)
```

**性能（纯 decode 差分，64/1024 双跑剥离 TTFT）**：

| 配置 | decode | 相对 plain |
|------|--------|-----------|
| plain（`--dflash none`，无 MTP） | 17.8 tok/s | — |
| DFlash2 草稿 + KFlash 树验证（无 MTP） | 23.2 tok/s | +30% |
| DFlash2 草稿 + KFlash 树验证 + MTP head（本次） | **32.1 tok/s** | **+80%** |
| 社区同机（M5 Pro **64GB**，含 MTP） | 34.3 tok/s | — |

**结论**：MTP 侧车补齐后达到社区水平的 ~94%（我们 48GB 机、社区 64GB 机）；27B 的瓶颈是稠密 27B 的带宽（13.5GB/token ÷ ~273GB/s ≈ 20 tok/s 理论 plain 上限），投机解码把有效吞吐推到 32 tok/s 属超线性收益。**定位不变**：质量最高（95.2%）但吞吐仅为现产 35B 的 ~1/4 → 保持「质量优先 / 内存友好」降级档，不作吞吐主力。

**注意事项**：`--hidden post_norm|pre_norm` 必须与模型布局匹配；本次 `post_norm` 生效且加速明显（若布局选错，表现是接受率≈0、速度回到 plain，而非答案错误——投机解码的输出仍由目标模型逐 token 验证）。

**产品定位结论**：27B 是**质量最高**（95.2%）但**吞吐最低**（23 vs 116-132 tok/s）→ 定位为**内存优先的降级档**（16GB 权重 + 22GB KV room，为 IDE/agent 会话腾空间），不作吞吐型主力；生产主力维持 splash + 35B OptiQ。

> **术语层次注（2026-10-29 订正）**：**KFlash 是验证策略（树形投机验证），不是草稿器**。
> 草稿器来源 = DFlash2 / DFlash / MTP / DSpark / LiLiCorr（`--dflash` 选源、`--spec` 控 MTP 深度）；
> KFlash 拿草稿器的候选做一次树形验证。两者可叠加：本机 27B 实测阶梯
> plain 17.8 → DFlash2+链式 21.5 → DFlash2+KFlash 23.2 → DFlash2+KFlash+MTP 32.1 tok/s。
> Kapitsa README 原文：「KFlash, tree speculative decoding: one verify pass checks a tree of the
> drafter's guesses, with a DFlash2, DFlash, MTP, DSpark or LiLiCorr drafter behind every model that ships one」。

## 四、后续可选动作（未做）
- **同权重 1:1 对照**（Kapitsa+KFlash vs splash+DFlash2，同为 OptiQ-4bit 权重）：HF 缓存已有 `mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit` 与 DFlash2 草稿器，可喂本地目录启动。**本次未做**：需独占引擎 ~12 分钟，与并行会话批期窗口相撞概率高且预期增量有限（±5%）→ 按窗口纪律不抢占。
- **Qwen3.8-27B（Kapitsa 支持）**：weights 16.08GB + MTP head 239MB + DFlash2 草稿器 3.85GB → MTP 与 KFlash 双可用；本仓 conf 为 Qwen3.8-27B（无 28B 档）。
- **代理层思考预算护栏已落地**（`91f7550`）：G1 分账（本地后端注入 llama.cpp 原生 `reasoning_budget_tokens` + 答案保留预算）、G2 降级重试（有 reasoning 但正文空且 `finish_reason=length` → 关思考重试一次，fail-open）——对任何后端通用，Flint 场景同样受益。

## 五、资产与生产状态

- 测试资产：`~/models/flint`（UD-Q4_K_XL 23GB，llama.cpp 用）、`~/.cache/kapitsa/models/`（flint-4bit 22.6GB + qwen3.6-35b 19.5GB + dflash2 1.0GB）。
- 生产未受影响：主引擎仍是 splash + OptiQ-4bit（`configs/splash-qwen36-35b-optiq.conf`），代理 :4000 healthy，embedgemma2 :8083 常驻（CONFIG_AUTOSTART_AUX 自动拉起）。

## 三·六、27B MTP 就绪后复测（2026-10-29 04:00–04:25）

> 背景：§三·五 的 27B 测量时我们的包**缺 MTP head**（kapitsa 日志 `no MTP head … speculative decoding off`），
> 只有 DFlash2 一条草稿路。2026-10-29 04:00 包更新为**含 MTP head**（`weights 16.08 GB + MTP head 239 MB + DFlash2 drafter 3.85 GB`，共 20.2GB），
> 故按原计划重做实验。测量口径：`tools/bench_kapitsa.py`（流式，TTFT 与 decode 分列），
> **权威读数取引擎自身的 `chat: prompt … , N tokens in M ms (X tok/s), Y tok/cycle` 行**（避免端到端时间混入 prefill）。

### 1) 三配置同 prompt 对照（kapitsa + Qwen3.8-27B-4bit，M5 Pro 48GB）

| 配置 | 4K prompt + 512 输出 | short prompt + 512 输出 | 强制长输出（600 tok 数字枚举） |
|------|----------------------|--------------------------|-------------------------------|
| **A. KFlash + DFlash2**（默认） | **41.2 / 42.6 tok/s**（tok/cycle 4.12–4.29） | 21.1 / 21.4（1.97–2.04） | **122.7 / 113.3**（**10.15 / 9.51**） |
| **B. KFlash + MTP**（`--dflash none`，depth 4） | 29.3 / 29.3（2.55） | **32.4 / 30.8**（2.84） | — |
| **C. plain**（`--dflash none --spec 0`） | 17.6 / 17.1 | 17.6 / 17.8 | — |

判读：
- **plain 17.5 与 §三·五 的 17.8 吻合** → 测量方法一致、数据可信。
- **DFlash2 与 MTP 是互补而非替代**：DFlash2 强在长/可预测输出（4K 41.2 vs MTP 29.3；强制长输出 122.7），
  MTP 强在短自由文本（32.4 vs 21.1）。**27B 应默认 DFlash2，MTP 作兜底**（DFlash2 缺失/不可用时）。
- 短自由文本仅 21 tok/s 是 think-off 27B 的「短答 + 低接受率（1.97 tok/cycle）」所致，不是引擎故障。

### 2) 同权重 1:1 对照（本次唯一干净的引擎对照）

同一份 `Qwen3.8-27B-4bit` 权重、同一 prompt（600-token 数字枚举，稳态口径）：

| 引擎 | decode | tok/cycle | 步频（cycles/s） | 4K+512 附带读数 |
|------|--------|-----------|------------------|-----------------|
| **kapitsa + KFlash + DFlash2** | **122.7 / 113.3 tok/s** | **10.15 / 9.51** | 12.1 | 41.2–42.6（tok/cycle 4.1–4.3） |
| **splash + DFlash2（链式）** | **104.6 / 104.8 tok/s** | 7.9 | 13.2 | 53.9\*（该次输出仅 157 tok，口径不齐） |

\* splash 27B 在「裸段落」prompt 上只答 157 tok（think-off），故 4K 列不可直接比；**强制长输出列才是同口径对照**。

结论：
- **KFlash 的树验证在 27B 上确有增益：+10~15%**（122.7/113.3 vs 104.6/104.8），与官方 M5 Pro 27B
  口径 +7% 同向、本机略高。机制：树验证把 tok/cycle 从 7.9 抬到 9.5–10.2（+20~28%），
  代价是步频略降（13.2→12.1 cycles/s），净得 +10~15%。
- **增益随「可预测性」放大**：强制长输出（枚举）tok/cycle 10+；裸段落/自由答仅 2–4。
  与 `speed-200tps-conditions-20261029.md` 的 `tok/s ≈ 步频 × tok/cycle` 完全一致。
- **但绝对吞吐差仍是 3 倍**：同机 35B OptiQ（生产主力）在 4K+512 下 **130–136 tok/s**
  （tok/cycle 3.5–3.7、步频 ~36），27B+KFlash 最好 122.7（仅枚举口径）——**生产主力维持 35B 不动**。

### 3) 内存与包构成（本次实测）

| 项 | 数值 |
|----|------|
| 包总盘占 | 20.2GB（weights 16.08 + MTP 0.239 + DFlash2 3.85） |
| 运行时 | DFlash2 resident **1.63GB**（+0.23GB 槽环），MTP head 24ms 载入 |
| KFlash 计划 | `~/.cache/kapitsa/kflash/qwen3.8-27b-4bit-dflash2-b4-Apple_M5_Pro.json`（按机型预调） |
| 互斥 | 与 35B splash（22GB）二选一，实际执行了 `stop-backend` → 起 kapitsa → 测完切回 |

### 4) 复现命令

```bash
cd ~/APP/llama.cpp
./manage.sh stop-backend                       # 让出引擎（内存互斥）
kapitsa serve --model 27b --private --port 8000 --context 32k --slots 1   # A: KFlash+DFlash2
# B: 加 --dflash none ；C: 加 --dflash none --spec 0
python3 tools/bench_kapitsa.py http://127.0.0.1:8000/v1 "A" 2
# 权威读数：grep 'chat: prompt' <日志> | tail
pkill -f "kapitsa serve"; ./manage.sh switch splash-qwen36-35b-optiq && ./manage.sh start
```
