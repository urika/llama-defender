# dflash-mlx 集成与本地后端性能优化记录（2026-08-26）

## 一、背景与目标

Claude Code 客户端经 `anthropic_proxy.py:4000` → 本地后端。此前 rapid-mlx 后端存在两类问题：

1. **hybrid 模型前缀缓存失效**：Qwen3.8-27B（GatedDeltaNet 混合架构）每轮 agent 追加都被迫全量冷 prefill（46K token ≈ 740s），远超客户端 300s 超时 → broken-pipe 断连风暴（118+ 次）。
2. **decode 速度上限低**：rapid-mlx 对 35B-A3B 实测 45–51 tok/s，未达用户记忆中的 80+ tok/s。

本文记录：prefix cache 根因调研、rapid-mlx 优化、dflash-mlx 新引擎集成、以及各模型适配结论。

---

## 二、hybrid 前缀缓存调研结论（关键认知）

### 2.1 为什么 hybrid 缓存"不可用"

- Qwen3.8-27B / Qwen3.6-35B-A3B 是 hybrid：**GatedDeltaNet 线性注意力**（递归状态）+ **Gated Attention**（KV cache）。
- 递归状态（`ArraysCache` conv_state/recurrent_state）**物理上不可裁剪**（RNN 状态无法删尾）→ rapid-mlx 对含此层的缓存条目标记 `non_trimmable=True`，只接受**整条精确匹配**。
- agent 循环每轮追加 → 前缀长度变化 → 永远不精确匹配 → 永远 MISS → 每轮全量冷 prefill。实测大请求 `entries=0`、`non_trimmable=True` 遍布日志。

### 2.2 正确解法：trim-free 前缀复用（rapid-mlx 已内置）

- rapid-mlx PR #1111/#1163 + v0.10.12 推出 **"trim-free prefix reuse"**：不裁剪递归状态，而是在**稳定边界存整条 non-trimmable 条目**，对**精确匹配 / 前缀扩展匹配**复用（无需裁剪）。
- 关键 flag：**`--hybrid-cache-entries N`**（默认 0=禁用；`--enable-prefix-cache` + hybrid 时**自动设为 8**，commit 0147c9a/#1163）。
- **`--hybrid-cache-entries 0`（之前误配）会关闭该特征** → 全 MISS。**应设 8**。
- 验证：Qwen3.8-27B 设 8 后，`cache_fetch HIT cached=3625 remaining=25`（共享前缀命中，仅 prefill 增量），增量轮 740s → 0.6s。

### 2.3 结论

- **`--hybrid-cache-entries 8` 对 hybrid 模型（Qwen3.8-27B / 35B-A3B）必要且有效**；rapid-mlx ≥0.10.12 会自动设 8，显式写 8 仅为明确意图、防 auto-enable 逻辑变动，**保留无害**。
- 关 context_engine（`PROXY_CTX_ENGINE_ENABLED=false`）配合稳定前缀可进一步保命中率（引擎 epoch 重写会破坏前缀缓存）。

---

## 三、rapid-mlx 后端优化

### 3.1 Qwen3.8-27B（hybrid，dense）

`configs/qwen3.8-27b-4bit.conf`：

- `--hybrid-cache-entries 8`（启用 trim-free 前缀复用）
- `--gpu-memory-utilization 0.80`
- `PROXY_CTX_ENGINE_ENABLED=false`

效果：decode ~39.5 tok/s；KV 缓存有效（80% 命中，增量轮秒级）；broken-pipe 风暴消除。

### 3.2 Qwen3.6-35B-A3B（hybrid，MoE）

`configs/rapid-mlx-35b-opt.conf`：

- 模型从 `unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit`（动态量化）切到 **`mlx-community/Qwen3.6-35B-A3B-4bit`**（标准 4-bit）：decode 37.9 → **51 tok/s**（+34%）。
- `--gpu-memory-utilization` 必须 **0.70**：0.80 会挤占系统内存（free 27%→ decode 崩到 5-7 tok/s），0.70 恢复 72% free + 51 tok/s。
- `--cache-memory-mb 8192` + `--hybrid-cache-entries 8`：缓存有效（80% 命中，冷请求 24.8s → 7s）。

### 3.3 MTP 尝试（35B）— 无效

- 35B config 含 `mtp_num_hidden_layers:1` 但 4-bit 权重**不含 MTP 头**（已拆到 `mlx-community/Qwen3.6-35B-A3B-MTP-4bit`）。
- 下载侧车 + `--speculative-config '{"method":"mtp",...}' --force-spec-decode` 可加载（`Loaded 46/46 MTP tensors`），但 **`max_k 被 clamp 到 1`**（SSM-hybrid 目标缺 per-position 快照）→ **decode 无提升**（51.3 = 关闭时）。
- **结论：rapid-mlx 内对 hybrid 的 MTP 无效，已回退**。

---

## 四、dflash-mlx 引擎集成（新后端，35B 实测 ~117 tok/s）

### 4.1 为什么换引擎

`ywchiu/mlx_benchmark_lab`（M5 Max 64GB，同模型 Qwen3.6-35B-A3B-4bit）跨框架实测：
dflash-mlx **128–155 tok/s**（DFlash 投机解码）> omlx（长上下文稳）> rapid-mlx（中游）> mlx-vlm（多模态但慢 25-30%）。

rapid-mlx 对 hybrid 的 DFlash/SuffixDecoding 均被门禁排除（`supports_spec_decode=False`），内部无更多提速杠杆 → 换 dflash-mlx。

### 4.2 安装与依赖

```bash
python3.12 -m venv .venv-dflash
.venv-dflash/bin/pip install dflash-mlx        # 0.1.8, mlx 0.32.2, mlx-lm 0.31.3
# 模型已本地; drafter 需下载
```

### 4.3 drafter 配置补丁（重要）

z-lab 2026-06 起的 drafter 把 `block_size` / `rope_theta` 移入嵌套结构（`dflash_config.block_size`、`rope_parameters.rope_theta`），而 dflash-mlx 0.1.8 只在顶层读取 → `DFlashDraftModelArgs missing 'rope_theta'/'block_size'`。

**修复**：编辑 drafter `config.json`，在顶层补 `block_size`（取 `dflash_config.block_size`）与 `rope_theta`（取 `rope_parameters.rope_theta`，35B/27B 均为 10000000）。
- 35B drafter `z-lab/Qwen3.6-35B-A3B-DFlash`：需补丁（嵌套格式）。
- 27B drafter `z-lab/Qwen3.6-27B-DFlash`：旧格式，顶层已含，无需补丁。

### 4.4 配置 `configs/dflash-35b.conf`

关键项：
- `LLAMA_BACKEND=dflash-mlx`、`LLAMA_MODEL=mlx-community/Qwen3.6-35B-A3B-4bit`
- `DFLASH_DRAFT_MODEL=z-lab/Qwen3.6-35B-A3B-DFlash`
- `DFLASH_ENABLE_THINKING=false`（dflash 无 `--no-thinking`，需 `--chat-template-args '{"enable_thinking": false}'`；**开 thinking 会吞输出 + 降 decode**）
- `DFLASH_EXTRA_ARGS="--temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.0"`（**dflash 不支持 `--repetition-penalty`**，会报 unrecognized arguments）
- `PROXY_CTX_ENGINE_ENABLED=false`（保前缀缓存命中）

### 4.5 manage.sh / admin_server.py 改动

- `manage.sh`：新增 `_start_dflash_mlx()`、两个后端 dispatch case（`dflash-mlx|dflash`）、`_current_backend`/`_get_pid` 的 dflash 进程检测（stop/status 可识别）。
- `admin_server.py`：
  - 后端进程匹配 `rapid-mlx|llama-server` → 加 `dflash`（否则 `backend_alive=False` → backend_down）。
  - `_probe_backend_model_name()` 改为**先精确匹配 `MODEL_NAME`、再家族兼容**（dflash 的 /v1/models 列出全部本地 MLX 模型，否则探错模型）。
  - `/status` 后端卡片条件 `_strategy.oom_safety_enabled` → `_ps.IS_CLOUD`（**修复隐藏 bug**：本地后端一直误显示 "Cloud API" 卡片）；本地卡片加 `Type` 行（`PROXY_BACKEND_NAME` + `BACKEND_TYPE`）。

### 4.6 实测（35B-A3B-4bit @ dflash-mlx）

| 项 | 数值 |
|---|---|
| decode（thinking off） | **~117 tok/s**（rapid-mlx 的 2.3×） |
| DFlash acceptance | 73–90% |
| prefill | ~900–2400 tok/s |
| prefix cache | 冷 15.7s → 同前缀 5.7s（`prefill restored` 生效） |
| 工具调用 | OpenAI `tool_calls` 格式正常（get_weather/Beijing 验证） |
| 正确性 | 17×24=408、437、56 等均正确 |
| 内存 | active ~20GB（35B MoE + drafter） |

---

## 五、Qwen3.6-27B 适配 dflash — 不推荐

- **可运行**（DFlash 87.5% acceptance，prefix cache 生效），但：
  - **decode 仅 ~15–17 tok/s**（dense 27B 全参数激活，本机慢）。
  - **thinking 无法关闭**：该 27B MLX 转换 tokenizer `chat_template` 为空（无 thinking 开关），`enable_thinking:false` 与 system 提示均无效，模型总输出 "Thinking Process:" → 输出污染。
- **结论**：dflash 的推荐模型是 **35B-A3B**（117 tok/s + thinking 可关）。27B 若坚持用，走 rapid-mlx（qwen3.6-27b-4bit，thinking 可关、~36 tok/s）。

---

## 六、性能汇总对比

| 配置 | 引擎 | decode tok/s | KV/prefix 缓存 | 备注 |
|---|---|---|---|---|
| Qwen3.8-27B-4bit | rapid-mlx | ~39.5 | ✅（hybrid-cache-entries 8） | gpu-mem 0.80 |
| Qwen3.6-35B-A3B-4bit | rapid-mlx | ~51 | ✅ | gpu-mem 0.70 必须 |
| **Qwen3.6-35B-A3B-4bit** | **dflash-mlx** | **~117** | ✅ | **当前生产**，thinking off |
| Qwen3.6-27B-4bit | dflash-mlx | ~16 | ✅ | thinking 无法关，不推荐 |

---

## 七、当前状态与回退

- active.conf → `dflash-35b.conf`，全栈 healthy，端到端正常（7×8=56 等）。
- 单元测试 1168/1168 通过（manage.sh/admin_server 改动无回归）。

回退到 rapid-mlx：
```bash
./manage.sh switch rapid-mlx-35b-opt && ./manage.sh stop && ./manage.sh start
```
配置备份：`/tmp/35b-conf-backup.conf`、`/tmp/35b-conf-pre-mtp.conf`。

---

## 八、关键结论备忘

1. **hybrid 前缀缓存**：`--hybrid-cache-entries 8` 是必配（0 会禁用 trim-free 复用）；关 context_engine 配合稳定前缀。
2. **rapid-mlx 对 35B 的 gpu-mem 必须 0.70**（0.80 内存压力崩 decode）。
3. **标准 4-bit > unsloth UD 动态量化**（decode +34%，质量略降）。
4. **MTP 对 hybrid（K=1 clamp）无效**。
5. **dflash-mlx 是 35B 最优引擎**（~117 tok/s），但 drafter 需 config 补丁、thinking 必须 off。
6. **Qwen3.6-27B 不适合 dflash**（慢 + thinking 不可关）。