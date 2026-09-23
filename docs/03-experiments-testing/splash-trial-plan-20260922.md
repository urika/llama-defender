# Splash 试装与同机对照：计划与设计

> 状态：**计划（待 r2 批跑收官后执行）** ｜ 撰写日期：2026-09-22
> 前置：r2 批跑（`exp-20260921-select-gate-r2`）收官或熔断停批 + GPU 空闲
> 目标：**同机验证「硬件潜力」**——Splash（DFlash 2）vs 我方 rapid-mlx，**同模型可比**

---

## 0. 为什么现在不能跑（硬约束）

| 约束 | 说明 |
|---|---|
| **红线 1（批期不动引擎）** | 0922-3/0922-5 与 swe-eval 确认；试装 Splash = 加重型 GPU 进程，比"动引擎"更严重 |
| **GPU 争抢** | Ornith ~28GB（peak 33.26GB，已越早警 33.0）+ Splash 27B（15GB 权重 + 1.2GB draft + KV）→ **合计 >48GB → OOM** |
| **熔断误判** | `metal_peak` 距停批线（34.0）仅 0.74GB；**任何额外 GPU 负载都可能触发停批** |
| **批次污染** | GPU 竞争 → 墙钟劣化 → **r2 数据失真** |

**已做（无害）**：Splash 1.0.2 已 `brew install`（**不启动则不占 GPU**）。

---

## 1. 目标与假设

### 1.1 核心问题

> **同硬件（M5 Pro 48GB）上，Splash 的「engine built around the model + DFlash 2」能比我们的通用 rapid-mlx 栈快多少？**

### 1.2 关键方法论：**必须同模型对照**

| 引擎 | 官方基准模型 | 我方对应配置 |
|---|---|---|
| **Splash** | `incoai/Qwen3.8-27B-Splash` | 无（Splash 独占格式） |
| **我方 rapid-mlx** | — | `configs/qwen3.8-27b-4bit.conf`（**同模型 Qwen3.8-27B**）|

**这就是对照设计的基础**：
- **臂 A**：Splash + Qwen3.8-27B-Splash（DFlash 2 开）
- **臂 B**：rapid-mlx 0.14.3 + `mlx-community/Qwen3.8-27B-4bit`（我方现有配置）

**⚠️ 注意差异（需在报告中声明）**：
| 维度 | Splash | 我方 |
|---|---|---|
| 权重格式 | `splash-packed-q4`（专用） | MLX 4bit |
| draft | **DFlash 2**（内置） | 无 |
| KV | int8（默认） | int4（配置）或 bf16 |
| 引擎 | Inco 特化 kernel | 通用 MLX |

→ **不是纯引擎对照，而是「整套栈」对照**（与 Splash 官方博客同口径："end-to-end comparisons"）。

---

## 2. 前置准备（批跑期间可做，不占 GPU）

### 2.1 模型包下载（网络 + 磁盘，不占 GPU）

```bash
# HF 镜像已配（HF_ENDPOINT=https://hf-mirror.com）
export HF_ENDPOINT=https://hf-mirror.com

# Splash 包 17.4GB
huggingface-cli download incoai/Qwen3.8-27B-Splash \
  --local-dir ~/models/splash/Qwen3.8-27B-Splash

# 我方 27B（若未下载）
# mlx-community/Qwen3.8-27B-4bit
```

**磁盘需求**：17.4GB（Splash）+ ~15GB（我方 27B 若未下）→ **334GB 可用，充足**。

### 2.2 环境核对（已做）

| 项 | 要求 | 实测 | 判定 |
|---|---|---|---|
| macOS | 26.4+ | **26.5.1** | ✅ |
| 芯片 | M3+ | **M5 Pro** | ✅ |
| 内存 | ≥36GB | **48GB** | ✅ |
| Homebrew | 需 | 6.0.20 | ✅ |
| Splash | 1.0.2 | **已装** | ✅ |
| Xcode/Metal | 仅源码编译需 | 无（bottle 免） | ✅ 不影响 |

---

## 3. 测试设计（批跑后执行）

### 3.1 隔离原则

| 原则 | 做法 |
|---|---|
| **单一引擎在线** | 跑 A 前停 B，反之亦然（**避免 GPU 争抢**） |
| **清内存** | 每臂切换前 `stop-backend` + 确认 Metal 释放 |
| **同端口** | 都绑一个固定端口（Splash :8000 / rapid-mlx :8081）—— **分别测，不并发** |
| **同 prompt 集** | 用 `tools/bench_ctx_ladder.py` 的阶梯 + 固定 decode prompt |

### 3.2 测试项（对齐 Splash 官方四维）

| # | 维度 | 方法 | 指标 |
|---|---|---|---|
| **1** | **decode（短）** | 512 tok × 3（预热后） | tok/s |
| **2** | **decode（长上下文阶梯）** | `bench_ctx_ladder.py` | 4K/16K/32K tok/s |
| **3** | **冷 prefill** | 32K prompt 冷 | TTFT s / prefill tok/s |
| **4** | **缓存命中 TTFT** | 32K replay（前缀不变） | ms |
| **5** | **并发** | 4 路短 prompt（Splash 支持并发；我方 `max-num-seqs 1`） | 聚合 tok/s |

**注**：第 5 项**我方配置是 `--max-num-seqs 1`** → **并发对照不公平**，需声明或调高（另测）。

### 3.2.1 ⭐ 公平性升级：同权重确认（2026-09-22 参数调研补充）

**manifest `upstream` 段实测**：

```
target:    mlx-community/Qwen3.8-27B-4bit @ 3e6447f0
draft:     incoai/Qwen3.8-27B-DFlash2  @ dedf8df6
tokenizer: 同 target
```

→ **Splash 的 27B 权重与我方 `configs/qwen3.8-27b-4bit.conf` 是同一上游**（`mlx-community/Qwen3.8-27B-4bit`）
→ **同模型同权重同 tokenizer，纯引擎/栈对照**（比原计划预期更干净）。

### 3.2.2 新增测试项：KV 格式双测

Splash `--kv-format`（README 有、`--help` 未列的隐藏参数）：

| 值 | 行为 |
|---|---|
| **`int8`**（默认） | 8-bit KV，**kernel 直读**（dequant 融合在 kernel 内） |
| `bf16` | 避免量化；**~2× KV 内存**；官方明示**长上下文可能更慢** |

**对照我方结论**：Ornith 上 **bf16 快于 int4**（dequant-on-read O(context) 代价，§17 三臂）。
但 **Splash 的 int8 是 kernel 融合直读**（不同实现），结论可能相反 → **值得双测**：

```bash
# 臂 A1：默认 int8（官方推荐）
splash serve --model incoai/Qwen3.8-27B-Splash --port 8000 \
  --max-context 100K --default-reasoning-effort none

# 臂 A2：bf16 KV
splash serve --model incoai/Qwen3.8-27B-Splash --port 8000 \
  --max-context 100K --kv-format bf16 --default-reasoning-effort none
```

### 3.2.3 参数全解（`serve --help` 实测 12 参 + 3 环境变量）

**网络与服务**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | `0.0.0.0` 开 LAN |
| `--port` | `SPLASH_PORT`/8000 | 端口 |
| `--allowed-host` | — | 额外 HTTP Host 名（不改 bind；可重复） |
| `--api-key` | `SPLASH_API_KEY` | Bearer/x-api-key；**健康探针保持公开** |
| `--no-webui` | — | 关浏览器聊天页 |

**资源上限（唯二「调优」入口，无配置文件）**：

| 参数 | 默认 | 说明 |
|---|---|---|
| **`--max-memory`** | **auto** | **Metal 分配上限**（如 28G）——非进程 RSS |
| **`--max-context`** | **auto** | 上限最高 **256K**——**容量上限非首 token 保证** |

**生成行为**：

| 参数 | 说明 |
|---|---|
| `--default-reasoning-effort` | none/minimal/low/medium/high/xhigh/max；显式请求值优先；CLI 旗标优先于环境 |
| `--served-model-name` | API 别名（可重复；响应仍报真实包 ID） |

**请求体/图片**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--max-request-size` | 128M | 并发输入共享预算 = max(512M, 2×limit) |
| `--max-image-pixels` | 4,194,304 | 单图最大像素 |

**环境变量仅 3 个**：`SPLASH_API_KEY` / `SPLASH_PORT` / `SPLASH_DEFAULT_REASONING_EFFORT`
（另有 `HF_HUB_CACHE`/`HF_HOME`/`HF_TOKEN`/`SPLASH_CRASH_TRACE=1`）。

### 3.2.4 manifest 内嵌几何（启动即定，不可配）⭐

「engine built around the model」的实体——`execution_geometry`：

```json
{
  "draft_proposal_tokens": 7,        // DFlash 2 每块提案 7 token
  "draft_sliding_window": 2048,      // draft 滑窗（长上下文 draft 内存有界之因）
  "target_verify_rows": 8,           // 一次验证 8 行
  "target_kv_block_tokens": 32,      // paged KV 每块 32 token
  "prefill_token_budget": 2048,      // prefill chunk 固定 2048
  "maximum_batch_width": 4,          // 批宽 4
  "attention_history_group": 128,
  "allocation_extent_target_bytes": 134217728
}
```

**与我方可调参数对照**：

| 项 | Splash（写死） | 我方（可调） |
|---|---|---|
| prefill chunk | 2048 固定 | `adaptive_prefill` 2048→256 动态 |
| 批宽 | 4 | `--max-num-seqs 1` |
| KV 块 | **32 tok/page（分页）** | 非分页（整条目） |
| draft 块 | 7 tok/提案 | — |

**format 段**：`splash-packed-q4`（schema 3），q4 group_size 64 / storage_n 256，段对齐 16KB。

### 3.2.5 试装参数决策（更新）

| 项 | 决策 | 理由 |
|---|---|---|
| `--max-memory` | **不设**（auto） | 与生产 cap 哲学一致，用满 Metal 推荐值 |
| `--max-context` | **100K** | 与我方阶梯匹配（27B 原生 256K 内存不够） |
| `--kv-format` | **int8 + bf16 双测** | 见 3.2.2 |
| `--default-reasoning-effort none` | 设 | 对齐我方 `LLAMA_THINKING=false` |
| `--max-request-size` | 不设 | 默认 128M 已远超我方 500KB |
| 认证 | 不设 | 本机环回 |

### 3.3 对照表模板

| 指标 | **Splash + 27B** | **rapid-mlx + 27B** | 比值 |
|---|---|---|---|
| decode 短（tok/s） | ? | ? | ? |
| decode 16K | ? | ? | ? |
| decode 32K | ? | ? | ? |
| prefill 32K（tok/s） | ? | ? | ? |
| 冷 TTFT 32K（s） | ? | ? | ? |
| **缓存 TTFT 32K（ms）** | ? | ? | ? |
| 并发 4 路（tok/s） | ? | —（seqs=1） | — |
| Metal peak（GB） | ? | ? | — |

**预期**（基于 Splash 官方 vs oMLX）：Splash 或领先 1.2–2×（oMLX 与我方同族通用引擎）。

---

## 4. 执行序列（批跑后）

```
[阶段 0] 批跑收官/熔断 → GPU 空闲确认
   └─ ps 确认无 run_agent；metal_active 回落

[阶段 1] 下载模型包（若批期未下）
   └─ HF_ENDPOINT=hf-mirror.com

[阶段 2] 臂 B：我方 rapid-mlx 27B（先测现有基线）
   ├─ ./manage.sh switch qwen3.8-27b-4bit
   ├─ ./manage.sh start-backend
   ├─ bench_ctx_ladder.py（阶梯）
   └─ decode ×3 + 32K TTFT

[阶段 3] 切换到臂 A：Splash
   ├─ ./manage.sh stop-backend          # 释放 GPU
   ├─ 确认 metal_active 回落
   ├─ splash serve --model incoai/Qwen3.8-27B-Splash --max-context 100K
   └─ 等待 "Ready"

[阶段 4] 臂 A 测量（同口径）
   ├─ 同上 bench（改 :8000）
   └─ 记录 /status 的 memory_actual + metrics

[阶段 5] 对照 + 结论
   └─ 落盘 logs/baselines/splash-vs-rapidmlx-27b-<date>.json

[阶段 6] 恢复
   └─ 停 Splash → 按需切回生产配置
```

---

## 5. 风险与回退

| 风险 | 缓解 |
|---|---|
| **Splash 加载失败**（内存预算不足） | `--max-memory 28G` / `--max-context 32K` 降配 |
| **Metal 争抢**（切换未清干净） | 每臂切换后 `ps` + `/status` 确认；必要时重启 |
| **Ornith 配置被改动** | 试装用 **`switch qwen3.8-27b-4bit`**（非 active 配置）→ **不动 ornith-oq4e.conf** |
| **试装影响生产** | 全程在**非批期**执行；完成后切回 active.conf |
| **20GB+ 下载失败** | hf-mirror 已验证可达；断点续传 |

**回退点**：
- 配置：`configs/active.conf` 软链（切回 `ornith-oq4e`）
- Splash：`brew uninstall splash`

---

## 6. 预期结论与用途

| 可能的结论 | 后续动作 |
|---|---|
| **Splash 显著更快**（>1.5×） | 评估 **Qwen3.8-27B 迁到 Splash** 的可行性（我方 27B 是备选配置） |
| **Splash 仅为 DFlash 增益** | 评估 **DFlash 2 draft 移植**到 rapid-mlx（需上游支持） |
| **Splash 无显著优势** | 确认我方栈在通用引擎中已接近最优 |
| **Splash 不可用**（格式/内存） | 记录限制，仅作技术参照 |

**对 Ornith 的意义**：本对照**不能直接推及 Ornith**（不同模型）；但可回答「**同硬件下特化引擎 vs 通用引擎的差距量级**」，为是否联系 Inco 做 Ornith 支持提供依据。

---

## 7. 待办清单

- [ ] **等 r2 收官**（前置门）
- [ ] 批期下载 Splash 包（17.4GB，不占 GPU）← **可先行**
- [ ] 确认我方 27B 权重已下载
- [ ] 阶段 2：rapid-mlx 27B 基线
- [ ] 阶段 3-4：Splash 试跑 + 测量
- [ ] 阶段 5：对照报告
- [ ] 阶段 6：恢复生产配置

---

**附录：Splash 关键参数**

```bash
splash serve --model incoai/Qwen3.8-27B-Splash \
  --port 8000 \
  --max-context 100K \      # 上下文上限（默认 auto）
  --max-memory 28G \        # Metal 分配上限（默认 auto）
  --kv-format int8 \        # 默认 int8；bf16 避免量化
  --default-reasoning-effort none   # 关闭思考
```

**诊断端点**：`GET /status`（admission/memory_actual/latency）、`GET /metrics`（Prometheus）
