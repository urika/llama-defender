# Splash 1.0.2 → 1.3.2 迁移 Runbook（2026-10-20）

> 记录引擎从 **Splash 1.0.2（含专有包）** 迁移到 **1.3.2（MLX 上游权重 + DFlash2 草稿）** 的完整操作，
> 以及 1.3.0/1.3.1/1.3.2 三版新特性的逐项对照。
>
> 能力/速度选型结论另见
> [`docs/06-reference-metrics/splash-model-capability-research-20261020.md`](../06-reference-metrics/splash-model-capability-research-20261020.md)。

---

## 一、摘要

| 项 | 变更前 | 变更后 |
|----|--------|--------|
| 引擎 | Splash 1.0.2 | **Splash 1.3.2** |
| 模型 | `incoai/Qwen3.6-35B-A3B-Splash` / `incoai/Qwen3.8-27B-Splash`（专有包） | `mlx-community/Qwen3.6-35B-A3B-4bit` / `mlx-community/Qwen3.8-27B-4bit` |
| 草稿 | 包内置 `draft/layer-N.bin` | `incoai/Qwen3.6-35B-A3B-DFlash2` / `incoai/Qwen3.8-27B-DFlash2` |
| 配置 | `splash-qwen36-35b.conf` / `splash-qwen38-27b.conf` | `splash-qwen36-35b-mlx.conf` / `splash-qwen38-27b-mlx.conf` |

**触发原因**：`--state-format bf16`（1.3.2 新增）可把 GDN recurrent state 减半，解掉 1.0.2 时代
state 触顶（17.4 GB / 95 条，Metal 41.0/43.2 GB，21 次内存预留被拒，138 s 冷 prefill）的问题。

---

## 二、版本特性对照

### 2.1 Splash 1.3.0 —— ANE 预填充

| 特性 | 说明 |
|------|------|
| **GPU + Neural Engine 切分 FFN** | 仅 dense 目标（27B）默认启用；按机器+模型自动校准并记忆；MoE(35B) 与 decode 仍走 GPU |
| Hadamard 旋转 W8A8 | ANE 侧计算，从现有模型暂存权重，不额外保留 int8 全模型 |
| 自动回退 | 评估失败/超时/非有限结果 → 该 chunk 重跑 GPU；`--disable-ane` 强制纯 GPU |
| 可观测 | `/status` 的 `ane_ffn` 报告 share / 最小 chunk / 评估次数 / 时序 / 重跑 |

> 本机实测：share **0.47**，FFN 27.7 ms（ANE）vs 48.3 ms（GPU）；空闲重校准结果仍为 0.47（无增益，原校准已最优）。
> 短 prompt、35B MoE、decode 不走 ANE。

### 2.2 Splash 1.3.1 —— ⚠️ Breaking Change

| 类别 | 内容 |
|------|------|
| **Breaking** | **Splash 包（`incoai/Qwen3.8-27B-Splash`、`incoai/Qwen3.6-35B-A3B-Splash` 及社区包）不再加载**（#335）。报错会给出 Hub 缓存目录，可删 |
| 模型格式 | MLX affine 2/3/4/5/6/8 bit、group 32/64/128、混精度、mxfp4；F16 vision projector；`processor_config.json` 新版图像处理器；OptiQ + `--language-only` |
| **性能** | MLX target/draft 统一到 GGUF 的 **block kernel 库**：2–4 并发 decode GPU 时间 **−4~26%**；`UD-Q4_K_M` GGUF decode **−6~12%**；prefill **−3~16%**；M3 Max 35B decode **−8~18%** |
| 内存 | 会话 state 27B 187→**153 MiB**，35B 109→**64 MiB**（同预算存更多会话） |
| 其他 | `/status`/`/metrics` 报告**热状态**并在日志记录变化；支持 Claude Code tool search 的 `tool_reference`；恢复权重改为等待内存而非停引擎 |

### 2.3 Splash 1.3.2 —— 本次目标版本

#### 模型格式

- 加载 MLX `nvfp4`、NVIDIA Model Optimizer、compressed-tensors NVFP4/FP8、llama.cpp NVFP4 GGUF（如 `nvidia/Qwen3.8-27B-NVFP4`）
- 更多 GGUF 变体：`UD-Q8_K_XL`、Q5_0/Q5_1、IQ1_M、IQ2_S token 表、融合 MoE gate/up 专家、混合类型 GDN alpha/beta
- 量化 checkpoint 可含 BF16/F16 权重（整包 BF16/F16/F32 仍不支持）

#### 内存与缓存

| 特性 | 收益 / 用法 |
|------|------------|
| **`--state-format bf16`** | GDN state 减半：27B 153→**81 MiB**，35B 64→**34 MiB**；计算仍 FP32，存储时 round |
| **SSD 前缀缓存默认 `auto`** | 配额最高 32 GiB，随卷剩余空间自适应；闲置 14 天清理；`--max-cache-disk 0` 关闭 |
| **`--persistent-cache` 免配额** | 跨重启保留；`--max-cache-disk-writes SIZE` 限制每日可选写入 |
| **`POST /v1/memory/release`** | 按需释放空闲权重（保留前缀缓存），下次请求恢复；忙时 409 `engine_busy` |
| **`cache_salt`** | 不同 salt 的 KV/state 在磁盘上也隔离 |
| **`deduplicated_tokens`** | Done 日志/metrics 报告超出初始前缀的复用 token（含 suspension 后复用） |

#### 思考与 Agent 兼容

| 特性 | 说明 |
|------|------|
| **`thinking_token_budget`** | Chat Completions/Responses 可限推理长度；服务端默认 `--thinking-token-budget N`，`-1` 关闭；Messages 遵守 `budget_tokens ≥ 1024` |
| **`--thinking-end-text TEXT`** | 预算耗尽时拉回答案；须以 `` 结尾、≤64 token；结构化输出时会等到完整字符再闭合 |
| **mid-session tool 增删** | Messages 按对话顺序应用 `tool_addition`/`tool_removal`，同名后者覆盖前者 |
| **工具名放宽** | 支持点号/空格/标点/Unicode，最长 256 字符 |
| **请求宽容度** | `max_tokens` 超窗口不再 400、可选 null 字段、`seed: -1`、stop 与 tools 并存、历史 malformed tool args 均可处理 |

#### 延迟与解码（40 核 M5 Max 官方实测）

| 优化 | 收益 |
|------|------|
| 首 token 提前发出 | 短 prompt TTFT **−37 ms**（27B）/ **−13 ms**（35B） |
| prefill 内捕获缓存状态 | 追加 100–300 token 到 8K 前缀 **−11~14%**（27B）/ **−20~23%**（35B） |
| 视觉编码 | 16,384-patch 图 **−20%**（1.419→1.141 s） |
| MoE decode 去冗余 gather | 35B 单 lane 周期 **−1.4~2.5%** |
| GDN decode 折叠 | 多数场景提升 **至 ~5.5%** |

#### 可观测与可靠性

- `/status.active_requests`：phase、优先级、prompt 进度、已生成 token、年龄
- `timings` / `metrics.speculation`：逐请求投机解码计数、tokens per cycle
- 取消/超时 prefill 保留上一检查点，重试可用
- 非有限 prefill 不入缓存；NaN/-inf logits 不可被 penalty 选中
- 引擎重启保留首引擎上下文（防 ANE 校准变更导致永久不可用）
- 孤立代理字符 tokenize 为 U+FFFD

---

## 三、迁移步骤（可复现）

### 3.1 前置

```bash
# HF 直连不可达时走镜像
export HF_ENDPOINT=https://hf-mirror.com
export HF_HUB_OFFLINE=0
```

### 3.2 升级引擎

```bash
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1
brew upgrade splash          # 1.0.2 → 1.3.2
splash --version             # 应输出 Splash 1.3.2
```

### 3.3 下载 DFlash2 草稿

`splash serve` 会按 `install/families.py` 的 `FAMILIES` 自动抓取，但若离线需预先下载：

```bash
P=/opt/homebrew/Cellar/splash/1.3.2/libexec/python/bin/python3
"$P" -c "
from huggingface_hub import snapshot_download
print(snapshot_download('incoai/Qwen3.8-27B-DFlash2'))
print(snapshot_download('incoai/Qwen3.6-35B-A3B-DFlash2'))
"
```

| draft | 体积 | 备注 |
|-------|------|------|
| `incoai/Qwen3.8-27B-DFlash2` | 3.85 GB | 与旧包内置 draft 不兼容（旧格式为 `draft/layer-N.bin + model.bin`） |
| `incoai/Qwen3.6-35B-A3B-DFlash2` | 1.0 GB | 同上 |

### 3.4 切换配置

```bash
./manage.sh switch splash-qwen36-35b-mlx && ./manage.sh restart   # MoE 快档
# 或
./manage.sh switch splash-qwen38-27b-mlx && ./manage.sh restart   # 稠密能力档
```

启动日志应出现：

```
Installing mlx-community/Qwen3.6-35B-A3B-4bit as Qwen3.6-35B-A3B (mlx-affine);
draft incoai/Qwen3.6-35B-A3B-DFlash2; vision disabled.
...
Ready · mlx-community/Qwen3.6-35B-A3B-4bit · context 256K · language only · http://127.0.0.1:8081
```

### 3.5 关键启动参数

```bash
SPLASH_EXTRA_ARGS="--max-context 256K --state-format bf16 --language-only \
                   --persistent-cache --default-reasoning-effort none"
```

| 参数 | 作用 | 生效方式 |
|------|------|---------|
| `--state-format bf16` | state 减半 | 需 restart |
| `--language-only` | 跳过 vision tower，省内存/启动时间 | 需 restart（独立 installation，首次重新准备） |
| `--persistent-cache` | SSD 前缀缓存跨重启 | 需 restart |
| `--default-reasoning-effort none` | 关闭默认推理 | 需 restart |

---

## 四、验收

### 4.1 功能性

```bash
curl -s http://127.0.0.1:8081/v1/models | python3 -m json.tool | head -20
```

期望：`vision: false`、`input_modalities: ["text"]`（`--language-only` 生效）。

### 4.2 指标面

```bash
curl -s http://127.0.0.1:8081/metrics | grep -E \
  "memory_pressure|memory_headroom|state_entries|state_bytes|ttft_p50|draft_acceptance"
```

### 4.3 实测基线（本机 M5 Pro 48GB）

| 指标 | 27B 稠密 | 35B-A3B MoE |
|------|---------:|------------:|
| 冷 prefill 30.4K | 450–488 tok/s | **2122 tok/s** |
| 热 TTFT 30.4K | 0.53–0.56 s | **0.27 s** |
| Decode @30K | 65–71 tok/s | **123–140 tok/s** |
| Decode 短 prompt | — | **335 tok/s** |
| DFlash2 接受率 | 65.7% | 55.9%（短 prompt 99%） |
| Metal 占用 | 19.0 GB | 21.9 GB |
| state/条（bf16） | 80.6 MiB | 34 MiB |
| 内存压力 / headroom | normal / 24.2 GB | normal / 21.3 GB |

复现脚本模式见 `docs/06-reference-metrics/BENCHMARK.md` §Splash 1.3.2 双档基线。

---

## 五、回退

```bash
# 1) 退引擎
brew install incoai/tap/splash@1.0.2   # 若 tap 仍保留旧版
# 或从 Time Machine / 旧 Cellar 恢复

# 2) 切回旧配置（需先确认 Splash 包仍在 HF 缓存）
./manage.sh switch splash-qwen36-35b && ./manage.sh restart
```

**注意**：

- 1.3.2 的 persistent cache 与 1.0.2 不兼容（state 格式变更），回退后首次请求为冷 prefill
- BF16 state 使用**独立命名空间**；从 FP32 切到 BF16 会重新填充一次
- 回退路径**未在本次变更中实测**（旧 Cellar 已被 brew 清理），视为理论路径

---

## 六、已知限制

| 限制 | 说明 |
|------|------|
| `FAMILIES` 仅 2 族 | 1.3.2 只注册 `Qwen3.8-27B` 与 `Qwen3.6-35B-A3B`；Flash-Next / GLM-5.3 / Muse-Glimmer 的 DFlash2 草稿虽已发布但未接入 |
| 单引擎互斥 | 27B 与 35B 不可同时常驻（40.9 GB vs limit 44 GB，且内存紧张会换页），切换 = `switch + restart`（约 60–90 s） |
| 不支持 MTP | 官方明确 "MTP modules are not read"；DFlash2 是唯一投机路径 |
| 不支持整包 BF16/F16/F32 | 安装器会拒绝全 BF16/F16/F32 的 GGUF |
| 协议版本 | 1.3.2 的 native/server 协议为 **10**（1.0.2 为 8）；升级后必须重启 |

---

## 七、可选参数（尚未启用，按需）

| 参数 / 端点 | 场景 |
|-------------|------|
| `--thinking-token-budget N` | 防止思考吃光 `max_tokens`（实测小 `max_tokens` 会得到 `stop_reason: max_tokens` 且只有 thinking 块） |
| `--max-cache-disk 0` | 内存紧张时换回更大 context（SSD 缓存默认预留 state staging 缓冲） |
| `--max-cache-disk-writes 2G` | 限制每日 SSD 写入，保护磁盘寿命 |
| `POST /v1/memory/release` | 临时让出内存给辅助引擎，不必停服务 |
| `cache_salt` | 多租户 / 多会话 KV 隔离 |

---

## 八、相关文档

- 能力/速度选型：[`docs/06-reference-metrics/splash-model-capability-research-20261020.md`](../06-reference-metrics/splash-model-capability-research-20261020.md)
- 性能基线：[`docs/06-reference-metrics/BENCHMARK.md`](../06-reference-metrics/BENCHMARK.md)
- 引擎总览：[`AGENTS.md` §4.5](../../AGENTS.md)
- 配置明细：[`docs/02-architecture-design/codebase-reference-20261017.md`](../02-architecture-design/codebase-reference-20261017.md)
