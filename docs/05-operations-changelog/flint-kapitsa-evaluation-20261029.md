# Flint 与 Kapitsa 引擎评估记录（2026-10-29）

> **结论先行**：Flint（Recursive 40B-A3B MoE）在本机可跑但**不具备生产替代条件**；Kapitsa 作为引擎在**同族 35B** 上与现产 splash 打平（115.2 vs 116-132 tok/s），KFlash 增益实测仅 **+11.5%**（官方对照 339 tok/s 为别的高端机口径）——**现产 splash + DFlash2 保持不动**。
> 本记录留存四形态的同口径数据与踩坑清单，供后续再评估时免重复劳动。

## 一、背景与目标

- 动机：现产主引擎为 Splash 1.4.0 + Qwen3.6-35B-A3B-OptiQ-4bit（decode 116-132 tok/s，质量门 PASS 93.5%）；考察①新一代模型（Flint）②新引擎（Kapitsa）能否带来实质提升。
- 约束：48GB 统一内存 → **本地引擎与 Kapitsa 二选一**（内存互斥，见 `docs/06-reference-metrics/memory-ledger-arbitration-20261029.md`）；批期禁起第二模型服务器。

## 二、四形态同口径对比（质量门 v3 = 62 条 / 7 类，确定性评分）

| 形态 | decode | prefill | 内存 | 质量门 |
|------|--------|---------|------|--------|
| **现产 splash + Qwen3.6-35B-A3B-OptiQ-4bit** | 116-132 tok/s | 1101-1154 tok/s | 24.4GB | **PASS 93.5%** |
| Kapitsa + Qwen3.6-35B-A3B-4bit + KFlash | 115.2 tok/s（纯 decode） | ~1500+ tok/s | ~22GB | FAIL 91.9%（summary 4/7 未达线） |
| Kapitsa + Flint native 4bit | 144.0-146.8 tok/s | ~1400 tok/s | 22.6GB | PASS 88.7% |
| llama.cpp master + Flint UD-Q4_K_XL | 69.2 tok/s | ~970 tok/s | 23.7GB | PASS 85.5% |
| Kapitsa + Flint UD-IQ3_XXS（3bit 导入） | 139.5 tok/s | 1644 tok/s | 14.1GB | PASS 82.3% |
| （参照）llama.cpp + Qwen3.6-35B-A3B-4bit | — | — | — | — |

关键读数：
- **Flint 天花板 = 147 tok/s（plain decoding）**：Kapitsa 官方文档明言「Dory and Flint have none: they decode without one」——Flint **无 DFlash 草稿器、无 MTP head** → KFlash/MTP 均不可用，无投机加成空间。
- **Flint 推理质量弱于现产**：专项探针（8 题确定性答案 + 开思考 + 足额预算）现产 8/8（思考 990-1583 字有界、均 2.8s），Flint IQ3 7/8 且 **R5 自指逻辑题思考死循环**（8192 预算下思考 9211 字、答案 0 字、63s——加预算仍不收敛）。
- **Kapitsa + KFlash 对同族 35B 只 +11.5%**：纯 decode 差分 115.2（KFlash on）vs 103.3（`NI_KFLASH=0` 链式）；官方对照「Kapitsa+KFlash 339 > Splash+DFlash2 281」为别的高端机口径，本机 M5 Pro 上不成立。

## 三、踩坑清单（复用价值最高的部分）

1. **Kapitsa 自带 pull 对大文件必卡死**：进程存活、进度条永远「0 KB / 0.0 MB/s」、`.part` 逻辑尺寸满但 `du` 仅 80K（**稀疏预分配假象**）。**根因**：Kapitsa 走 `HF_ENDPOINT` 环境变量——本机全局设为 `HF_ENDPOINT=hf-mirror.com`（实测仅 9KB/s，且 HF 元数据 API 路径超时）→ pull 拉 `huggingface.co` 直连（1.8MB/s）或改用 ModelScope 镜像自研并行下载。
2. **HF 元数据 API 在本环境不可用**（`resolve`/CDN 通、`api` 超时）→ `kapitsa serve --model <hf-repo>` 会报「Can't download the file list」；解法是喂本地目录（HF 快照含完整布局：分片 + `model.safetensors.index.json` + `chat_template.jinja`）。
3. **ModelScope 镜像可用且快**：`mlx-community/Qwen3.6-35B-A3B-4bit`、`unsloth/flint-GGUF`、`RecursiveSI/flint-int4` 均已镜像；并行断点续传可跑到 ~16MB/s（单连接 4MB/s）。**注意 `curl -sIL` 的 content-length 有时取空** → 改用 ModelScope 文件 API 取精确尺寸。
4. **思考开关是质量门的必要条件**：Flint 默认 thinking-on 会烧完 768 max_tokens 预算致正文空 → 门首跑 83.9% FAIL（6 条空响应假象）→ llama-server `-rea off` 后 85.5% PASS。与 splash 当年同源教训。
5. **llama.cpp 构建缺 UI 资产会挂**：`tools/ui` gzip 步骤（缺 CSS 文件）→ 配置加 `-DLLAMA_UI_GZIP=OFF` 解决。
6. **Kapitsa 导入需支持的专家量化格式**：仅接受 IQ1_S/IQ2_XXS/IQ2_S/IQ3_XXS/IQ3_S 或全 Q8_0/Q6_K；unsloth **UD-Q4_K_XL（IQ3_S/Q5_K 混合）不可导入** → 只能走 IQ3_XXS（3bit，质量 -3.2pp）或官方原生 4bit。

## 四、后续可选动作（未做）

- **同权重 1:1 对照**（Kapitsa+KFlash vs splash+DFlash2，同为 OptiQ-4bit 权重）：HF 缓存已有 `mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit` 与 DFlash2 草稿器，可喂本地目录启动。**本次未做**：需独占引擎 ~12 分钟，与并行会话批期窗口相撞概率高且预期增量有限（±5%）→ 按窗口纪律不抢占。
- **Qwen3.8-27B（Kapitsa 支持）**：weights 16.08GB + MTP head 239MB + DFlash2 草稿器 3.85GB → MTP 与 KFlash 双可用；本仓 conf 为 Qwen3.8-27B（无 28B 档）。
- **代理层思考预算护栏已落地**（`91f7550`）：G1 分账（本地后端注入 llama.cpp 原生 `reasoning_budget_tokens` + 答案保留预算）、G2 降级重试（有 reasoning 但正文空且 `finish_reason=length` → 关思考重试一次，fail-open）——对任何后端通用，Flint 场景同样受益。

## 五、资产与生产状态

- 测试资产：`~/models/flint`（UD-Q4_K_XL 23GB，llama.cpp 用）、`~/.cache/kapitsa/models/`（flint-4bit 22.6GB + qwen3.6-35b 19.5GB + dflash2 1.0GB）。
- 生产未受影响：主引擎仍是 splash + OptiQ-4bit（`configs/splash-qwen36-35b-optiq.conf`），代理 :4000 healthy，embedgemma2 :8083 常驻（CONFIG_AUTOSTART_AUX 自动拉起）。
