# 本地执行栈与训练可行性：模型/后端/KV/微调（调研与实测综合）

> **状态**：设计研究记录（未实施）｜**日期**：2026-08-30
> **来源**：exp-1-amnesia 实验窗口期的第二批推演与本机实测验证（mlx_lm 栈/权重命名/后端二进制全部实查）。
> **关联**：[信息面度量推广与应用](information-metrics-applications-survey-20260830.md)（测量维度的推广）· [v0.7.0 看板](../07-project-board/v0.7.0-information-plane.md)（IFC-10 挂载本件工作）
> **一句话**：本机的完整执行栈决策图——9B 开箱可训且能力证据充分；35B 训练只差一行架构映射（30 分钟可定档）；KV 是幽灵损失通道需行为基线；后端选型=选生产引擎+选测量平台双目标。

---

## 1. wiki 小步快跑的本地训练管线（35B/9B 通用设计）

**核心洞察：小步协议把不可训练的"长轨迹 agent 问题"变成可训练的"海量短 episode 问题"**——绕开 FoldGRPO/Verlog 最难的轨迹长度问题。

三段管线（Tongyi 式课程）：

```
Stage 0 数据与奖励基建(全部现成)
  故障注入 = 无限可验证数据生成器(断链/陈旧引用/格式错乱,正确答案机械可知)
  本代理数据面 = rollout 免费仪器(ILE/H_BE/D_ledger 随产出)
Stage 1 SFT 冷启动
  云端 1M 模型在小步协议下示范操作级轨迹(输入集→patch+引用→验证通过)→蒸馏
Stage 2 RL(GRPO 式,自有 harness 为环境)
  奖励 = 机械验证通过(确定性) + 过程密集项(步数预算/reread=0/D_ledger 保真/
  [E2 成立时] H_BE 稳定) + 墙钟速度项(Forge 同款)
```

"快"的来源是结构不是模型：步小→prefill 小+前缀命中；patch 输出短；机械验证零模型成本；幂等闸防发散。每步 5-15s × 百级操作 = 1-3 小时批处理，与本地节奏匹配。

## 2. 本机训练可行性（实测验证于 2026-08-30）

### 2.1 栈与架构实查

| 项 | 实查结果 |
|---|---|
| 训练栈 | 系统 Python 已有 `mlx_lm 0.29.1`（lora 模块可用）；MLX 0.31/0.32 在两个 venv |
| 9B（Ornith-1.5-9B-MLX-4bit） | `model_type: qwen3_5`——**在 mlx_lm 注册表，开箱可训**；权重 4.7GB 已在本机 |
| 35B（Ornith-35B-A3B-oQ4e） | `model_type: qwen3_5_moe`——**注册表缺**；但 `qwen3_5.py` 已含 GDN 块（`linear_attn` 命名与 checkpoint 逐字对上）**且已含 `SparseMoeBlock`**；`qwen3_next.py`（461 行）为 GDN+MoE 完整实现 |

### 2.2 35B 移植成本三档（较此前"数天"判断大幅下修）

| 情景 | 工作量 | 内容 | 触发 |
|---|---|---|---|
| 最顺利 | **1-3 小时** | `MODEL_REMAPPING` 加 `"qwen3_5_moe": "qwen3_5"` + 剥 `language_model.` 前缀 + 解包 `text_config` → load/生成/LoRA 三连冒烟 | 命名全对齐（初查已对上大半） |
| 典型 | 1-2 天 | ~200-300 行薄适配类 + oQ4e 混合精度的 scales/biases 映射核对 | 部分对齐 |
| 不顺 | 3-5 天 | 权重重组 + 生成对拍调参 | 结构性差异（当前证据概率低） |

**30 分钟定档法**（必须排在实验窗口后，35B load 需 ~20GB 与后端互斥）：加 remap → `mlx_lm.load` 冒烟 → 生成对拍（vs rapid-mlx 同 prompt）→ LoRA 3-step。第一步的报错直接定档。

### 2.3 内存账（48GB M5 Pro，35B 假设架构解决后）

```
推理后端停止释放 +20GB；4-bit 权重 20GB；LoRA+Adam <1GB；
激活 2-6GB(小步协议→训练序列天然短；MoE A3B 按 3B 计激活)
合计 ~25-30GB < GPU 上限 ~40GB ✅  —— 卡架构不卡内存
```

## 3. 9B 能力评估（对位 wiki 维护任务）

**证据**：SWE-bench Verified **70.6** / Terminal-Bench 2.1 **47.0**（官方模型卡口径，超 Gemma-4-31B 的 52/42.1）——而 wiki 机械操作**严格易于** SWE-bench（输入小、输出结构化、验证确定）。

| wiki 任务类 | 9B 判定 |
|---|---|
| 机械可验证（断链/格式/索引/时效） | ✅✅ 绰绰有余 |
| 范围化对账（论断×源引文） | ✅ 预期通过率 70-85% |
| 局部综合 | 🟡 折扣大于 35B，但有验证兜底 |
| 原创跨源综合 | ❌（35B 亦然） |

风险：小模型校准陷阱更高发（低熵+高 D_ledger 象限——四象限准入硬升级兜底）；官方 bench 需打折 + thinking-off 格式遵从需实测。**实证 pilot（半天）**：故障注入 30 操作 → 9B 走小步协议 → 类别通过率/四象限落位/D_ledger 分布 → 机械类 >90% 且对账类 >70% 即承接立项。

## 4. KV 机制调研与行为基线

### 4.1 框架定位：幽灵损失通道

```
S → H → V_t (符号损失: ILE 可见) → KV(V_t) (数值损失: 视图不变, ILE=0 ✗) → B_t
```

KV 量化**符号层不可见、数值层退化条件**——Tier-0/ILE 结构性失明，只有双轴（H_BE×D_ledger）可见。预期形态：KV 4-bit 主要伤回忆精度 → **D_ledger 变化比 H_BE 更显著**（通道指纹）。

### 4.2 机制调研三层结论

- **本机栈（rapid-mlx）**：KV 量化（prefix 条目 + live `--kv-cache-dtype`）；hybrid GDN 层为固定尺寸递归态→不可修剪→prefix 边界 RNN 状态快照（恢复 ~0.1ms）；radix+快照组合的 prefix cache。
- **业界共识**：KIVI（ICML'24）确立非对称量化范式；**4-bit 基本精度安全、2-bit 长上下文退化**（Kitty MLSys'26）——但共识口径**不含本机负载形态**（hybrid GDN+长会话工具调用）。
- **缓存复用语义**（Anthropic prompt caching）：精确前缀匹配、断点前任何变化全失效、低于最小门槛**静默不缓存**。

### 4.3 为什么基线必须实测：三个静默失败先例

rapid-mlx [#1197 kv-dtype 曾不生效于 live 缓存]、Anthropic 门槛静默跳过、mlx-lm [#1162 hybrid 缓存静默失败]——同一模式：**KV/缓存层的声明行为与实际行为可能背离且不报错**。

### 4.4 行为基线三组（B1/B2/B3）

| # | 测量 | 产出 | 时机 |
|---|---|---|---|
| B1 重复探测稳定性 | 同上下文连续 3-5 次探针→H_BE 方差 | **测量噪声底**（E2 判定阈值 + 每栈噪声指纹） | 批跑恢复后立即，30min |
| B2 开关生效验证 | KV 开/关的 RSS 差+日志 | 防静默失败 | exp-2 前置 |
| B3 KV 压缩汇率 | 9B 平台 KV off vs 4-bit 各一小批 | KV 层率失真曲线 | exp-2 主体 |

**B1 双重价值**：既是 E2 的噪声底（ILE 后 ΔH 须超噪声才算信号），又是"选栈=选测量平台"的依据。指纹补 `kv_quantization/hybrid_cache_entries/prefix_cache` 三键（一行）。

## 5. 本地推理栈选型协议（冻结，排 E1-E4 后）

### 5.1 后端盘点（实查）

| 栈 | 就绪度 | 模型 | 先验 |
|---|---|---|---|
| rapid-mlx（active） | ✅ | Ornith-35B oQ4e / 9B(8084 辅助位) / Qwen3.8-27B / Gemma-4 | ~80 tok/s，hybrid cache+KV q4 成熟 |
| dflash-mlx | ✅ | Ornith-35B / Qwen3.6-35B | Qwen3.6 ~117（投机有效）；**Ornith ~91 vs 87 无增益** |
| llama-server | ✅ 二进制+GGUF 缓存 | 仅 GGUG 系 | hybrid GDN 不支持（9B 17 tok/s 事故）→ 兼容臂 |
| vllm-mlx | ⚠️ archived | — | 不测 |

### 5.2 协议要点

- **矩阵两轴**：A) 同模型跨引擎（Ornith-35B: rapid vs dflash）；B) 同引擎跨模型（rapid: 35B vs 9B——wiki 选型）
- **真实负载回放**：今日档案小/中/大三个真实会话逐轮回放 + 微基准（冷/热 TTFT、定长 decode、max_tokens 遵从——量化 §8.7 缺陷）
- **四维权重**：热 TTFT 与缓存 40% / decode 20% / **行为可靠性 25%（工具合规+max_tokens+探针稳定性 B1）** / 内存运维 15%
- **核心洞察：选栈 = 选生产引擎 + 选测量平台双目标**——探针方差最小的栈做实验平台，吞吐最高的栈做日常生产，可以不同栈
- 产出：`docs/06-reference-metrics/backend-selection-benchmark-20260830.md` + 台账 note

## 6. 执行时序与路线图挂载

```
批跑 end → 恢复三参数 → B1(30min) → E1-E4 判定(优先)
→ 后端选型窗口(~2-3h) → 9B 能力 pilot(半天, 可并行)
→ 35B remap 30min 定档 → (视结果) SFT v0 → exp-2 KV 审计(9B)
```

v0.7 看板挂 IFC-10（本件全部工作）；exp-2 候选与 v0.8 训练路线均以 E2 判定为分水岭。

## 7. 主要参考

rapid-mlx：[仓库](https://github.com/raullenchai/Rapid-MLX) · [PyPI 0.10.2](https://pypi.org/project/rapid-mlx/0.10.2/) · [#1197](https://github.com/raullenchai/Rapid-MLX/issues/1197) · [#1717](https://github.com/raullenchai/Rapid-MLX/issues/1717)；上游：[mlx-lm #1162](https://github.com/ml-explore/mlx-lm/issues/1162) · [#932](https://github.com/ml-explore/mlx-lm/issues/932)；KV 量化：[KIVI](https://arxiv.org/abs/2402.02750) · [Kitty MLSys'26](https://mlsys.org/virtual/2026/oral/3746)；缓存语义：[Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) · [机制解析](https://mager.co/blog/2026-04-29-claude-prompt-caching/)；后训练背景见 survey 文档 §6。
