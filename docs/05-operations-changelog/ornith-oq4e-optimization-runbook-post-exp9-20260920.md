# Ornith-oQ4e 优化验证 Runbook（EXP-9 收官后执行）

> 状态：**计划（未执行）** ｜ 撰写日期：2026-09-20 ｜ 预计执行：EXP-9 收官 + swe-eval 取证完成后
> 对象配置：`configs/ornith-oq4e.conf`（当前 `active.conf`）
> 分析依据：[`docs/04-analysis-diagnostics/ornith-oq4e-performance-bottleneck-analysis-20260920.md`](../04-analysis-diagnostics/ornith-oq4e-performance-bottleneck-analysis-20260920.md)（§5 / §10 / §11）
> 排程约束：[`exp9-closeout-runbook-20260920.md`](exp9-closeout-runbook-20260920.md)、跨仓联合协调文档 J 系决议

---

## 0. 目的与执行摘要

**目的**：用三组「小步、高信号」改动，验证 Ornith-oQ4e 长上下文性能瓶颈（Metal 压力导致每请求 prefix cache 驱逐 + decode 衰减）的根因判定是否成立，并量测每步的真实收益。每步独立可回滚、可归因。

| # | 动作 | 目的 | 预期信号 | 回滚 |
|---|---|---|---|---|
| 1 | `--pin-system-prompt` + `--gpu-memory-utilization 0.75→0.80` | 消除每请求驱逐 | `[prefix-pressure-evict]` **计数→0** | 删参数 / 回 0.75 |
| 2 | `PROXY_HBE_ENABLED=false` + `PROXY_CTX_EPOCH_TRIGGER_TOKENS 70000→50000` | 去附加开销 + 降 live KV | 纯 decode / TTFT 回升 | 改回原值 |
| 3 | rapid-mlx `0.12.13 → 0.14.3`（`.venv-rapidmlx`） | 启用 hybrid recurrent-state checkpoint + GDN prefill 优化 | 长会话 `cache_fetch HIT` 恢复、decode 升 | 装回 0.12.13 |

**均不改模型权重、不改输入内容语义、不改客户端配置。**

---

## 1. 前置条件与硬约束

1. **顺序硬约束**：EXP-9 驱动退出 → **swe-eval 收官取证（含代理侧数据拉取）完成** → 才可重启代理/引擎。
   代理重启会清内存台账（R14 ledger / R16 聚合），**取证必须在前**。
2. **引擎重启协调协议**照走：多会话通知、重启窗口内 404 为预期（见
   `engine-restart-coordination-20260906.md`）。
3. **代理代码激活**需重启代理进程（SIGHUP reload 不加载新代码）；本 Runbook 三步骤均为
   **配置变更**（runbook 内已注明 reload vs restart），不涉及代码变更。
4. 执行前 `git log` 复核无新协调决议（联合文档为唯一权威）。

---

## 2. 基线采集（每步之前都先做一次）

```bash
# ① 状态与内存
./manage.sh status
curl -s http://127.0.0.1:4000/api/status | python3 -m json.tool > /tmp/ornith-status-<step>.json

# ② 近 30 请求指标
curl -s "http://127.0.0.1:4000/metrics?n=30" > /tmp/ornith-metrics-<step>.json

# ③ 后端日志切片（解码/分块/驱逐）
tail -c 3000000 logs/llama-server.log \
  | grep -oE 'finished normally, [0-9]+ tokens in [0-9.]+s|first token after [0-9.]+s|cache_entries=[0-9]+ cache_mem=[0-9]+MB|\[Metal memory\][^"]*|prefix-pressure-evict[^"]*' \
  > /tmp/ornith-backend-<step>.log
```

**关键指标**（口径见分析文档 §2）：纯 decode（`finished normally`）、TTFT（`first token after`）、
`prefix-pressure-evict` 计数、Metal `active/peak/cap`、cache_entries/cache_mem。

---

## 3. Step 1 — 消除驱逐（`restart-backend`）

**改动**：`configs/ornith-oq4e.conf` 的 `RAPID_MLX_EXTRA_ARGS`

```diff
-RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.75 --cache-memory-mb 12288 --max-num-seqs 1 --default-repetition-penalty 1.0 --hybrid-cache-entries 32"
+RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.80 --cache-memory-mb 12288 --max-num-seqs 1 --default-repetition-penalty 1.0 --hybrid-cache-entries 32 --pin-system-prompt"
```

**执行**：

```bash
./manage.sh stop-backend
./manage.sh start-backend      # RAPID_MLX_EXTRA_ARGS 为启动参数，必须重启后端
```

**判定（对照分析文档 §6）**：
- ✅ 通过：`prefix-pressure-evict` **计数归零**；Metal `peak < cap`；长上下文纯 decode 从 ~20 → **≥25 tok/s**
- ⚠️ 注意：`--gpu-memory-utilization > 0.85` 被 `manage.sh:542`（DEF-006）拒绝；0.80 会打印警告但放行
- ⛔ 回滚：删 `--pin-system-prompt`、`0.80 → 0.75`，重启后端

---

## 4. Step 2 — 去附加开销 + 降 live KV（`reload`，不重启引擎）

**改动**：`configs/ornith-oq4e.conf`

```diff
-export PROXY_HBE_ENABLED=true
-export PROXY_HBE_SAMPLE_EVERY=2
+export PROXY_HBE_ENABLED=false
+export PROXY_HBE_SAMPLE_EVERY=2
...
-export PROXY_CTX_EPOCH_TRIGGER_TOKENS=70000
+export PROXY_CTX_EPOCH_TRIGGER_TOKENS=50000
```

**执行**：

```bash
./manage.sh reload            # 以上均为 reloadable，无需重启
curl -s http://127.0.0.1:4000/api/status | python3 -c \
  "import json,sys;c=json.load(sys.stdin)['ctx_config'];print('epoch_S=',c['epoch_S'])"
```

**判定**：
- ✅ 通过：40K 会话下纯 decode **回升 10–20%**；TTFT p50 下降；负重启（大请求）下无明显退化
- ⚠️ 副作用：HBE 探针停采 → `logs/diag/hbe.jsonl` 断流（标定/实验用途；如需可改 `SAMPLE_EVERY=8` 折中）
- ⚠️ 副作用：S=50000 使 epoch 折叠更早触发（更早收编），影响长会话窗口
- ⛔ 回滚：改回 `PROXY_HBE_ENABLED=true`、`PROXY_CTX_EPOCH_TRIGGER_TOKENS=70000`，`./manage.sh reload`

---

## 5. Step 3 — 升级引擎 rapid-mlx 0.14.3（`stop-backend` + `start-backend`）

**背景**：Ornith 与 Qwen3.8 同属 Qwen3.5-class GDN hybrid；0.14.2 的 **hybrid recurrent-state
checkpoint 恢复** 与 0.13.0 的 **自调 recurrent prefill / fused GDN kernel** 对 Ornith 同样适用
（见分析文档 §10/§11）。

**执行**：

```bash
# ① 升级 config 指定的引擎 venv（不动 brew；config 用 LLAMA_SERVER_BIN 指向 .venv-rapidmlx）
.venv-rapidmlx/bin/pip install -U "rapid-mlx==0.14.3"
.venv-rapidmlx/bin/rapid-mlx --version        # 应显示 0.14.3

# ② 换引擎
./manage.sh stop-backend
./manage.sh start-backend

# ③ 冒烟
curl -s http://127.0.0.1:4000/api/status | python3 -c "import json,sys;print(json.load(sys.stdin)['state'])"
```

**判定**：
- ✅ 通过：长会话出现 `cache_fetch HIT cached=<较大值>` / recurrent-state 恢复迹象；40K decode 提升；
  `cache_fetch` 不再整链 MISS
- ⚠️ 风险：0.14.x 变更了解析器/默认值（Ornith 为非别名模型，走架构自动检测）；升级后须回归
  **工具调用 / 流式 / 冷 prefill** 三件套
- ⚠️ 风险：MTP/hybrid 交互（rapid-mlx #477）——本步**不启用** `--force-spec-decode`，仅验证缓存/前缀改善；
  投机解码单独立项（分析文档 §11.4）
- ⛔ 回滚：`.venv-rapidmlx/bin/pip install "rapid-mlx==0.12.13"`，重启后端

---

## 6. 统一验收标准

| 指标（口径） | 现状 | 目标线 |
|---|---|---|
| 长上下文（~40K）纯 decode | ~20 tok/s | **≥25 tok/s** |
| TTFT p95 | 17.7s | **<12s** |
| `prefix-pressure-evict` | 每请求 | **0** |
| 短上下文（5–10K）纯 decode | — | 70–90 tok/s（回归基线） |
| 工具调用 / 流式 / 冷 prefill 回归 | — | 无退化 |

任一指标未达，按 §3–§5 的「回滚」回退对应步骤，并记录观测数据。

---

## 7. 风险与回滚矩阵

| 风险 | 出现条件 | 处置 |
|---|---|---|
| Metal 贴上限触发 `watchdogd` panic | `gpu-memory-utilization 0.80` + 长 prefill | 回 0.75；必要时 `sudo sysctl iogpu.wired_limit_mb=43008`（评估后） |
| HBE 停采影响实验 | Step 2 | 改 `SAMPLE_EVERY=8` 折中或本步延后 |
| S=50000 折叠过早、窗口变短 | Step 2 | 回 70000；或仅调 `KEEP_TOKEN_BUDGET` |
| 0.14.3 行为回归（解析器/默认值） | Step 3 | 回 0.12.13；影子环境先预跑 |
| 重启窗口 404 / 会话中断 | Step 1/3 | 执行前走引擎重启协调协议、避开在途批跑 |
| 归因混淆（多步叠加） | 连续执行 | **每步之间必须完成 §2 采集与判定**，再进下一步 |

---

## 8. 影子环境预跑（推荐）

三步骤可先在影子代理验证，避免扰动生产状态：

```bash
# 见 test/lib/promptfoo_env.sh 头注：从工作树起独立代理(:4021，引擎关、状态隔离)
bash test/run_tests.sh --promptfoo
```

若时间有限，至少对 **Step 3（升级）** 做影子冒烟，其余两步为纯配置、可直接在生产小窗口执行。

---

## 9. 需要的配合（转发相关方）

| 相关方 | 需要配合 |
|---|---|
| swe-eval | EXP-9 收官取证完成后通知；批跑窗口避开 Step 1/3 的重启窗口 |
| 值守 | Step 1/3 执行期间关注 404 与会话中断；按协调协议广播 |
| 本仓（llama.cpp） | 执行三步、采集数据、回写本 Runbook 与联合文档 |

---

## 附录：涉及文件

```
配置          configs/ornith-oq4e.conf（RAPID_MLX_EXTRA_ARGS / PROXY_HBE_* / PROXY_CTX_EPOCH_TRIGGER_TOKENS）
引擎 venv     .venv-rapidmlx（config LLAMA_SERVER_BIN 指向）
安全门        manage.sh:534-547（DEF-006，gpu-mem >0.85 拒绝）
后端日志      logs/llama-server.log
状态/指标     /api/status ｜ /metrics?n=30
分析依据      docs/04-analysis-diagnostics/ornith-oq4e-performance-bottleneck-analysis-20260920.md
排程约束      docs/05-operations-changelog/exp9-closeout-runbook-20260920.md
```

---

## 附录 B：评审记录（LCP snap-down 线，2026-09-20）

> 评审方：本仓并行会话（P1a/P1a.2/P1a.3 本地补丁线 + Phase 0 诊断 owner）。
> 结论：三步方向合理；**Step 3 有两个会使升级无效 / 机制替换未决的事实问题，执行前须处理**。

### B1（阻塞）`LLAMA_SERVER_BIN` 未设置——Step 3 按写执行不会生效

实测：`configs/ornith-oq4e.conf` 无 `LLAMA_SERVER_BIN`；`manage.sh:623` 回退 PATH 的
`rapid-mlx`（= brew Cellar 0.12.12）。只 `pip install -U` 到 `.venv-rapidmlx`
再 `start-backend`，跑起来的仍是 brew 0.12.12——**升级静默无效**。
修：Step 3 增加配置 diff（`LLAMA_SERVER_BIN=.venv-rapidmlx/bin/rapid-mlx`），
③ 冒烟加版本断言（`ps -E -p <pid>` 环境或启动日志版本行）。

### B2（阻塞）升级 = 用上游机制替换本地 P1a 补丁——替换决策与验收口径需先定

实测 0.14.3 wheel：包已改名 `vllm_mlx → rapid_mlx`（vllm_mlx 仅 deprecation shim），
本地 P1a 补丁（`patches/vllm-mlx/0.12.12/`，5 文件）**无法沿用**。但上游**已内置同族机制**：
`rapid_mlx/hybrid_state_checkpoints.py`（env `RAPID_MLX_HYBRID_CHECKPOINT_MAX`
默认 4 / `RAPID_MLX_HYBRID_CHECKPOINT_STRIDE` 默认 2048），fetch 侧支持两条路径
（LCP + **supersequence**，日志 `[cache_fetch] LCP snapped to checkpoint:
shared=... resumed_at=...`）。

| 维度 | 本地 P1a（0.12.12 补丁） | 上游 0.14.3 |
|---|---|---|
| 检查点位置 | **消息边界**（chat 路由截断渲染计算） | **prefill chunk 边界**（stride 2048，自动） |
| 覆盖窗口 | M=8 消息边界，**覆盖全上下文** | 默认 4×2048 ≈ **最近 ~8K** |
| 路径 | 仅 LCP | LCP + supersequence |
| 依赖 | 需 batched/request/scheduler 配合 | 零 API 配合 |
| 日志 / 开关 | `LCP snapdown` / `PROXY_CACHE_LCP_SNAPDOWN` | `snapped to checkpoint` / `RAPID_MLX_HYBRID_CHECKPOINT_MAX=0` 关闭 |

即：上游**覆盖路径更广、更自治**；但默认窗口仅最近 ~8K，而 E1 实测中位发散点在
请求长度 81% 处（距尾 ~19%）——40K 请求时发散点可能**落在窗口外 → 回退全冷**。
升级执行须：① 实测窗口够不够（不够则调大 MAX/STRIDE，注意检查点内存）；
② 验收日志从 `LCP snapdown` 改为 `snapped to checkpoint`；③ **EXP-8 的 LCP off 臂
开关从 `PROXY_CACHE_LCP_SNAPDOWN=0` 改为 `RAPID_MLX_HYBRID_CHECKPOINT_MAX=0`**
（否则 off 臂失去含义、对照失效）。

### B3（增强）Step 1/2 与本线的协同与张力

- **Step 1**：前科条件已消失（现单引擎，8084 无监听——kvcache 文档 §8.2 的
  gpu 0.80 变差是双引擎 Metal 竞争所致），值得重试；且条目存活↑ → 检查点存活↑ →
  snap 收益同向放大。建议 §2 基线采集**加 `snapdown`/`snapped` 与
  `LCP unavailable` 计数**，判定时分清收益来自哪条路径。
- **Step 2**：S 70000→50000 与 kvcache 文档 §8.4 校准（S=110000 建议）方向相反，
  目标不同（decode 优先）可以理解；但折叠频率↑ → 全冷重建轮↑，其代价现被 snap
  部分对冲（重建期正是 LCP 发散场景）——判定时**同时记全冷轮计数**，否则
  收益/代价会算错。
- **§8 影子环境**："避免扰动生产状态"仅在代理状态层面成立：promptfoo 推理仍打
  共享后端 8081（`--max-num-seqs 1`）——**批期禁跑**（并发排队干扰批跑与验收窗口）。
