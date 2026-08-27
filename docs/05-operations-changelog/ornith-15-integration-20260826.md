# Ornith-1.5-35B-A3B 集成记录（2026-08-26）

## 一、背景与目标

DeepReinforce 于 2026-08-19 发布 Ornith-1.5（MIT 协议，三尺寸：397B MoE / **35B-A3B MoE** / 9B dense）。模型卡称 35B-A3B 在编码/agentic bench 全面超 Qwen3.6-35B-A3B：

| Benchmark | Ornith-1.5-35B-A3B | Qwen3.6-35B-A3B |
|---|---|---|
| Terminal-Bench 2.1 (Claude Code) | **68.5** | 49.2 |
| SWE-bench Verified | **79.0** | 73.4 |
| SWE-bench Pro | **59.6** | 49.5 |
| GPQA Diamond | **89.2** | 86.0 |
| MCP-Atlas | **70.2** | 62.8 |

目标：将官方 MLX 版接入本栈（rapid-mlx 后端 + anthropic_proxy.py 代理），验证可行性与性能。

## 一·一、模型家族评分与特点（官方 + 第三方）

### 1. Ornith-1.5 家族特点
- **MIT 协议**，DeepReinforce 出品，2026-08-19 发布（blog: ornith.ai/ornith_1_5.html）。
- **自改进训练循环**：不再依赖人工任务集，联合优化三件事——任务生成、scaffold 构建、solution rollout（GRPO，reward 朝 0.2 成功率调）→ 持续自我扩展课程。
- 三尺寸：**397B MoE** / **35B-A3B MoE**（3B active）/ **9B dense**（另有 9B-Mobile 量化版可跑手机）。
- 架构：基于 **Qwen3.5**（hybrid GatedDeltaNet 线性注意力 + 全注意力 3:1 + 稀疏 MoE 256 专家/top-8）；**262K 上下文**；推理模型（thinking 默认开）+ qwen3_xml 工具调用。
- 多格式发布：BF16 / FP8 / NVFP4 / GGUF / **MLX**。

### 2. 官方评分（35B-A3B vs Qwen3.6-35B，5 次均值）
| Benchmark | Ornith-1.5-35B | Qwen3.6-35B | 备注 |
|---|---|---|---|
| Terminal-Bench 2.1 (Terminus-2) | **67.8** | 52.5 | |
| Terminal-Bench 2.1 (Claude Code) | **68.5** | 49.2 | |
| SWE-bench Verified | **79.0** | 73.4 | 甚至超 Qwen3.5-397B (76.4) |
| SWE-bench Pro | **59.6** | 49.5 | |
| SWE-bench Multilingual | **71.4** | 67.2 | 追平 397B (69.3) |
| DeepSWE | **22.0** | 0 | 同级全 0，最大相对差距 |
| Frontier-Bench v0.1 | **5.1** | 1.4 | 同级/397B 均 1.4 |
| NL2Repo | **46.2** | 29.4 | |
| SWE Atlas QnA | **39.8** | 15.5 | 2.5× |
| HLE (no tools) | 25.6 | 21.4 | 同尺寸最优，但落后 397B (28.7) |
| HLE (with tools) | 33.4 | 28.9 | 落后 397B (48.3) 明显 |
| GPQA Diamond | **89.2** | 86.0 | 略超 397B (88.4) |
| MCP-Atlas | 70.2 | 62.8 | 落后 Muse-Glimmer-30B (75.5) |
| Toolathlon-Verified | **48.7** | 41.7 | |
| WideSearch | **67.8** | 60.1 | |
| BrowseComp | **67.6** | 62.0 | |
| ClawEval | **72.5** | 68.7 | |

### 3. 家族其他尺寸
- **397B**：Terminal-Bench 2.1 **86.1** / DeepSWE **56.0** —— 对齐 Claude Opus 4.8 (85.0/59.0)，超 GLM-5.2 (82.7/46.2)、DeepSeek-V4-Flash-0731 (82.7/54.4)。
- **9B**：Terminal-Bench 2.1 **47.0** / SWE-bench Verified **70.6** —— 超 Gemma 4-31B (42.1/52) 及部分 Qwen3.6-35B 项。

### 4. 第三方评测（MindStudio 2026-08-20，交叉验证）
- 官方数字独立复述一致；SWE-bench Verified 79 是该表中唯一破 79 的模型。
- 社区本地实测（单 A100 80GB + vLLM，~74GB 含 KV）：端到端自主完成"构建实时价格告警功能"——自主规划→写码→自测→迭代→验收，无需步骤指引（无 checklist）。
- 社区标题：本地测试称"比 Qwen3.8-27B 更好更快"。
- **定位结论**：编码/agentic 全面领先同级，纯推理（HLE w/ tools）仍落后大模型——**是编码 agent 的强候选，非纯推理替代**。

### 5. 本机实测（§五/§六 + 基线 2026-08-26）
- 质量 14/14 (100%)、TTFT 0.27/0.81/1.01s、gen **79.8 tok/s**（本地模型全项最优，见 `logs/baselines/ornith-oq4e.json`）。

### 6. 推理评分对比（vs Qwen3.8-27B，2026-08-26）
| 推理基准 | Qwen3.8-27B（官方） | Ornith-1.5-35B-A3B（官方） |
|---|---|---|
| GPQA Diamond | **89.2** | 89.2（打平） |
| HLE | **30.8** | 25.6（无工具）/ 33.4（带工具） |
| IFBench（指令遵循） | 79.5 | 无公开 |
| Artificial Analysis 智能指数 | **52（GPT-5.6 级，开放权重最高）** | 未入榜 |

- **纯推理口径**：Qwen3.8-27B **不弱于甚至略优**——GPQA 打平（89.2=89.2）、HLE 无工具更高（30.8 vs 25.6）、AA 指数 52（GPT-5.6 级）而 Ornith 未入榜；Ornith 仅"带工具 HLE"（33.4）反超。
- **编码/agentic（本栈用途）Ornith 更强**：Terminal-Bench 2.1 67.8/68.5 vs Qwen3.8-27B 61.7，SWE-bench 系全面领先。
- **本机速度差 2 倍**：Qwen3.8-27B 本机 rapid-mlx ~39.5 tok/s（hybrid dense 全参数激活），Ornith oQ4e 79.8 tok/s。
- **结论**：纯推理 Qwen3.8-27B 更值；但综合「编码能力 + 本机速度 + 质量 14/14」，**Ornith oQ4e 仍是本机最佳**。需纯推理可留 `qwen3.8-27b-4bit` 为备选（半速）。

## 二、模型与架构核实

- 官方 MLX 版本（`ornith-ai/`）：**`Ornith-1.5-35B-A3B-MLX-4bit`（19.5GB）**、`-MLX-8bit`（36.8GB）、`Ornith-1.5-9B-MLX-8bit`；另有 BF16/FP8/NVFP4/GGUF。
- 架构：**`Qwen3_5MoeForConditionalGeneration`**（`model_type: qwen3_5_moe`，text 骨干 `qwen3_5_moe_text`）→ 基于 Qwen3.5 的 **hybrid GatedDeltaNet + 全注意力 3:1**：
  - 40 层 = **30 线性注意力（GatedDeltaNet）+ 10 全注意力**（层 [3,7,11,...,39]）。
  - **256 专家 / top-8**，`moe_intermediate_size=512`，`hidden_size=2048`，`max_position_embeddings=262144`。
- **推理模型**：默认 thinking 开；工具调用 qwen3_xml；官方推荐采样 temp=0.6 / top_p=0.95 / top_k=20。
- 架构推论（决定能否进本栈）：
  1. **hybrid 必配 `--hybrid-cache-entries 8`**（同 Qwen3.8/3.6，否则 prefix cache 全 MISS）。
  2. **投机解码全部不可用**：rapid-mlx 对 hybrid 骨干门控关闭 spec-decode（issue #1941，recurrent-state 无法 rollback）；且 Ornith 无任何 DFlash/MTP drafter。

## 三、下载过程（HF 网络抖动处理）

- 本机系统 Python 3.9 用 LibreSSL 2.8.3，**无法连 HF**（`NotOpenSSLWarning` + TLS 失败）→ 改用 `.venv-dflash`（Python 3.12 + OpenSSL 3.6）下载。
- `snapshot_download` 反复在 xet 路径网络抖动中断（HEAD 失败，10G/18G 处各断一次），缓存可续传但同一小文件反复失败。
- **最终方案**：`curl` 直接拉齐 4 个 safetensors shard（SHA256 与 HF 列出大小逐一核对）+ 全部小文件，并**手工写回 hub cache**（`blobs/<sha256>` + `snapshots/<rev>/<file>` 软链，`REV=19504d9`）→ repo-id `ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit` 可离线解析，served `/v1/models` id 与代理 `MODEL_NAME` 精确一致。
- 完整副本备份：`/tmp/ornith-model/`（38GB）。

## 四、配置 `configs/ornith-35b.conf`

基于 `rapid-mlx-35b-opt.conf`，关键项：

- `LLAMA_MODEL=MODEL_NAME="ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit"`
- `RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.70 --cache-memory-mb 8192 --max-num-seqs 1 --default-repetition-penalty 1.05 --hybrid-cache-entries 8"`（**hybrid-cache-entries 8 必配**）
- `RAPID_MLX_TOOL_PARSER="qwen3_coder_xml"` + `RAPID_MLX_REASONING_PARSER="qwen3"`
- `LLAMA_THINKING=false`（manage.sh 自动加 `--no-thinking`；推理模型必关，否则吞输出降速）
- `LLAMA_TEMP=0.6 / TOP_P=0.95 / TOP_K=20`（Ornith 官方通用推荐）
- `PROXY_CTX_ENGINE_ENABLED=false`（保前缀缓存命中，同 dflash-35b）

## 五、实测结果（rapid-mlx 后端）

| 项 | 数值 |
|---|---|
| decode | **~70–96 tok/s**（300-token 批次 96.5；短批 70.8，随负载波动） |
| prefill（冷） | ~713 tok/s（5272 tokens / 7.4s） |
| prefix cache（append-only） | **14.4x**：req2 `cache_fetch HIT cached=5296/5318 remaining=22`，7.39s → **0.51s** |
| thinking | ✅ 关闭（输出无思维块，`7` 直答） |
| 工具调用 | ✅ 端到端 `tool_use`（get_weather/Beijing → `{"city":"Beijing"}`） |
| 内存 | model on disk 18.2GB / est. working set 27.3GB（48GB 余量充足） |
| 正确性 | 全链路 healthy，`/api/status` 正常 |

> 注意：prefill 增量命中依赖**稳定前缀**——新请求必须包含存储条目的完整 token 前缀（agent append-only 模式天然满足；两个独立同前缀请求因存储条目含首轮输出而无法裁剪复用，属预期）。

## 六、性能定位

| 配置 | 引擎 | decode tok/s | 备注 |
|---|---|---|---|
| Ornith-1.5-35B-A3B-4bit | rapid-mlx | **~86–88** | 无投机解码 |
| Ornith-1.5-35B-A3B-4bit | dflash-mlx + Qwen3.6-DFlash | **~91**（engine）/ 86.5（proxy） | DFlash acceptance 63.3%，与纯 rapid-mlx 基本持平 |
| Qwen3.6-35B-A3B-4bit | rapid-mlx | ~51 | gpu-mem 0.70 必须 |
| Qwen3.6-35B-A3B-4bit | dflash-mlx | ~117 | DFlash 投机解码（可选生产） |
| Qwen3.8-27B-4bit | rapid-mlx | ~39.5 | hybrid dense |

**结论**：Ornith-1.5 虽无投机解码，但 19.5GB 更轻 + 3B active MoE，decode 实测逼近 dflash-35b，且编码 bench 全面胜出——**当前最优综合候选**。

### 6.1 MTP / DFlash2 适配结论

- **MTP**：Ornith 架构支持（`mtp_num_hidden_layers:1`）但官方 `mtp.*` 头**是随机初始化占位**（std=0.0200 恰为 initializer_range，acceptance ~13% = 纯碰运气）。社区训练头 `shisa-ai/Ornith-1.5-35B-A3B-MTP-ONLY`（~60% accept）只有 BF16/FP8 合并版（vLLM/llama.cpp 目标），**无 MLX 版**；且 rapid-mlx 对 hybrid 骨干门控关闭投机解码（issue #1941），MTP 不可用。
- **DFlash2**：无 Ornith DFlash2 drafter；dflash-mlx 引擎只支持 DFlash1。
- **DFlash1（实测可行）**：社区验证 Qwen3.6 的 `z-lab/Qwen3.6-35B-A3B-DFlash` 头可直接当 Ornith 草稿。配置 `configs/ornith-dflash-35b.conf` 实测：
  - 加载正常（`DFlash speculative decoding active`），thinking off，prefix cache + L2 生效。
  - **decode ~91 tok/s，acceptance 63.3%**（Qwen3.6 原生 73–90%，跨模型下降）。
  - L2 prefix restore ~125–132 tok/s（增量轮复用良好）。
  - **与纯 rapid-mlx（86–88 tok/s）基本持平** → DFlash 对 Ornith 无显著增益（drafter 未对齐），不如 Qwen3.6 上 117 tok/s 的幅度。

## 七、当前状态与回退

- active.conf → `ornith-35b.conf`（rapid-mlx，**持平且更简单**），全栈 healthy，端到端（代理/工具/缓存/状态页）全部验证通过。
- 单元测试 1168/1168 通过（仅新增配置文件，无代码改动）。

```bash
./manage.sh switch dflash-35b && ./manage.sh stop && ./manage.sh start   # 回退 dflash
```

## 七·五、Ornith 专用 DFlash drafter 蒸馏实验（2026-08-26）

### 背景
官方 MTP 头随机初始化（~13% accept）不可用；社区 Qwen3.6-35B-DFlash 头可直接当 Ornith 草稿（线上 63.3% accept，decode ~91 tok/s），但为跨模型未对齐。

### 方案
audreyt 发布 `dflash_distill_mlx.py`（MLX 原生 DFlash 蒸馏，原目标 Ornith-9B）。其依赖私有 `dflash.model_mlx` 模块（旧 API）与公开 dflash-mlx 0.1.8（`dflash_mlx`，projected-context 新 API）不兼容 → **改写为 0.1.8 API 适配版**，已存档于 **`tools/dflash_distill_mlx.py`**（可复用）：
- 特征缓存用 `target_ops.forward_with_hidden_capture` + `extract_context_feature`。
- loss 用 `draft.project_target_hidden` + `forward_projected_context` + `logits_from_hidden`（draft_context 必须先过 fc/hidden_norm 投影，否则形状错误）。
- 采样集用本地精选 coding prompt（无 `datasets` 依赖）。
- `--train-scope projection`（fc+hidden_norm）→ warm-start `--train-scope all`（整个 6 层 drafter）。

### 实测
| 项 | 数值 |
|---|---|
| prepare | 144 样本 / 5.7 min（~2.4s/样本：~192 tok 生成 + ~384 tok 特征前向） |
| train（projection） | ~24 step/s，4096 步 ≈ 3 min；acceptance 2.34→2.42 |
| train（all，warm-start） | ~5 step/s；acceptance 2.42→**2.66**（best @step1536） |
| 蒸馏评价指标 | 原 Qwen3.6 drafter 2.34 → 蒸馏 best **2.66**（+13.7%，同一 eval 集） |
| **线上（dflash-mlx 实跑）** | **decode 仍 ~91 tok/s，acceptance 仍 63.3%，无增益** |

> 实验产物在 `/tmp/ornith-distill/`（full_data 样本、full_train_all 检查点，**临时目录，重启即失**）；如需复现请用 `tools/dflash_distill_mlx.py` 重新准备/训练。

### 结论
- **蒸馏管线端到端跑通**（本机 M5 Pro 全流程 ~30 min，硬件无压力）。
- **训练指标真实提升，但未转化为线上 decode 加速**：dflash-mlx 自适应 DFlash 调度对 2.3→2.7 量级的 drafter 提升不敏感，decode 稳定在 ~91 tok/s。
- **最终判定**：Ornith 的投机解码收益上限即为 ~91 tok/s（dflash），与纯 rapid-mlx（~87）基本持平；**当前最优 = rapid-mlx ornith-35b（~87 tok/s，无 drafter 复杂度）**。DFlash 加速真正有效的是 Qwen3.6-35B 原生（117 tok/s，dflash-35b 配置）。

## 七·六、oQ4e 混合精度量化切换（2026-08-26，**当前生产**）

### 背景
均匀 4-bit（`ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit`，19.5GB）decode ~87 tok/s 但为逐张量均匀量化。`pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp` 用 oMLX v0.6.2 **oQ4e（imatrix 增强混合精度）**：20.1GB，**314 处 per-tensor 精度提升**（group size 64/128 混合），校准集 `oqe_code_multilingual`（128 样本 × 512 token）——路由/lm_head/attention 等敏感张量保真度更高。

### 下载（HF 网络抖动处理，同 §三）
- 21.6GB（5 shard + 12 小文件），`.venv-dflash` Python 下载，途中一次中断自动续传；最后 shard4 HEAD 反复失败 → **kill 重试循环 + 拷贝部分字节 + `curl -C -` 续传**（4.36GB → 5,133,842,500B 精确完整，sha256 校验通过）+ 手工回填 `model.safetensors.index.json`。

### 配置 `configs/ornith-oq4e.conf`（从 ornith-35b.conf 派生）
- `LLAMA_MODEL=MODEL_NAME="pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp"`
- 其余同 ornith-35b.conf：`--no-mllm` + `--hybrid-cache-entries 8` + gpu-mem 0.70 + qwen3_coder_xml + thinking off。

### 实测对比
| 指标 | 均匀 4-bit（ornith-35b） | **oQ4e（ornith-oq4e，当前激活）** |
|---|---|---|
| decode | ~87 tok/s | **~80 tok/s**（-8%） |
| prefill（冷） | ~713 tok/s | ~680 tok/s |
| prefix cache | ✅ 秒级 | ✅ `HIT cached=2272/2293`，6.4x |
| 质量 | 标准 | **imatrix 混合精度，路由/lm_head 更保真** |
| 正确性 | ✅ | ✅ 17×24=408、fizzbuzz、排序、Paris 全对 |

### 结论
- **保留 oQ4e 为当前生产**：以 8% 速度换质量。选 Ornith 的目的就是编码/agentic 质量，混合精度对路由敏感张量的保真更契合。
- 速度优先可回退 `ornith-35b`（均匀 4-bit，~87 tok/s）。

## 八、备忘

1. **Ornith = hybrid（Qwen3.5 系）**：`--hybrid-cache-entries 8` 必配；无任何投机解码。
2. **推理模型 thinking 默认开** → 必须 `--no-thinking`（manage.sh 由 `LLAMA_THINKING=false` 自动加）。
3. **下载用 `.venv-dflash` Python**（系统 Python 3.9 LibreSSL 过旧无法连 HF）；HF 网络抖动时用 curl + 手工回填 hub cache。
4. 工具/推理 parser 复用 qwen3 系，与现栈完全兼容。

## 九、上下文感知输出预算（2026-08-27，修复 115K 超时循环）

- **事件**：会话 s38a6b67（343K chars / 115K tokens，非流式 max_tokens=16384）在 saturation 档拿 16K 预算，decode <27 tok/s → >600s 代理超时 → 客户端 300/600s 超时后静默重发 → 5×504 + 8×499 重试风暴。prefix cache 全程 HIT，非缓存问题。
- **根因**：`_compute_dynamic_max_tokens` 三档合一，saturation/oom_danger/pre_trunc 共用 `PROXY_DYNAMIC_MAX_TOKENS_SATURATION=16384`；且 lifecycle continuation 短路使 agent 连续会话 >250K chars 一律归 saturation（oom/pre_trunc 对 agent 不可达）。
- **修复**（设计见 `docs/02-architecture-design/agent-output-budget-design-20260827.md`）：
  1. `lifecycle.py` 拆出 oom 档 → 新增 `PROXY_DYNAMIC_MAX_TOKENS_OOM`（默认 4096，`_RELOAD_SPEC`/`__all__`/`CONFIG_REGISTRY` 同步注册）。
  2. ornith-*.conf ×3：`SATURATION=16384→8192`、新增 `OOM=4096`、`PROXY_MAX_TOKENS_OVERRIDE=32768→0`（仅向下钳制的天花板，置 0 彻底排除干扰）。
  3. 单测补四档分支 + override 不反向用例（unit 1172 全过，signature/config-lint 通过）。
- **预算语义**：预算=天花板非目标值；触顶返回 `stop_reason=max_tokens` 由 Claude Code 下一轮续写，长代码/文档走 Write 工具不受限 → 对 agent 实质产出无影响。

## 十、超时硬化：主动 504 + 流式空闲看门狗（2026-08-27，P1 补强）

- **背景**：§九 预算修复解决了"过大输出"，但预算之外仍存在 **600s 后端 vs 300s 客户端竞速**——非流式请求超时错误在客户端断连后才送达（`CRITICAL: failed to send error`），且流中 stall 会拖满唯一 sequence。
- **服务端主动 504**：
  1. do_POST 解析 `X-Stainless-Timeout` → `PipelineContext.client_timeout_s`（缺省 0）。
  2. BackendDispatcher `_effective_backend_timeout(ctx, proactive=not ctx.is_stream)`：非流式 urlopen 超时钳为 `min(600, 客户端超时−PROXY_TIMEOUT_MARGIN_S=30)` → 300s 客户端在 270s 收到 504+Retry-After（客户端未断、错误可送达）；流式保持宽松超时（prefill 不受限）。
- **流式空闲看门狗**：
  1. `_timed_stream_lines`（anthropic_proxy.py）包住三处流式循环；首 token 后把 socket 读超时收紧到 `PROXY_STREAM_IDLE_TIMEOUT_S=30`（已实测 `resp.fp.raw._sock` 路径可用）。
  2. stall 抛 `StreamIdleTimeout` → BackendDispatcher 捕获后 `resp.close()` 取消后端在途生成（headers 已提交，客户端见截断流自行重试）。
- **参数**：`PROXY_TIMEOUT_MARGIN_S` / `PROXY_STREAM_IDLE_TIMEOUT_S`（均默认 30，reloadable，`_RELOAD_SPEC`/`__all__`/`CONFIG_REGISTRY` 注册）。
- **验证**：unit 1182 全过（新增 13 用例：proactive 钳制 5 + 看门狗 2 + client_timeout 解析 3 + RequestParser 续）+ signature 快照重生成 + snapshot + config-lint 通过。
- **生效**：配置值 reload 已应用；新代码需 `./manage.sh restart` 重启代理进程后生效。