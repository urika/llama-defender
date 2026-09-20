# Ornith-oQ4e 优化验证 Runbook（EXP-9 收官后执行）

> 状态：**计划（未执行）** ｜ 撰写日期：2026-09-20 ｜ 预计执行：EXP-9 收官 + swe-eval 取证完成后
> 对象配置：`configs/ornith-oq4e.conf`（当前 `active.conf`）
> 分析依据：[`docs/04-analysis-diagnostics/ornith-oq4e-performance-bottleneck-analysis-20260920.md`](../04-analysis-diagnostics/ornith-oq4e-performance-bottleneck-analysis-20260920.md)（§5 / §10 / §11）
> 排程约束：[`exp9-closeout-runbook-20260920.md`](exp9-closeout-runbook-20260920.md)、跨仓联合协调文档 J 系决议
> 评审记录：附录 B（2026-09-20，commit `6de2218`，LCP snap-down 补丁线并行会话）；**B1/B2/B3 已折入正文 §2–§8**
> 隔离与回退：**§10 零影响验证（T0–T3，并行 venv + 影子端口 + 状态隔离）｜ §11 回退设计（分层制品 + 原子切换 + 决策闸）**

---

## 0. 目的与执行摘要

**目的**：用三组「小步、高信号」改动，验证 Ornith-oQ4e 长上下文性能瓶颈（Metal 压力导致每请求 prefix cache 驱逐 + decode 衰减）的根因判定是否成立，并量测每步的真实收益。每步独立可回滚、可归因。

| # | 动作 | 目的 | 预期信号 | 回滚 |
|---|---|---|---|---|
| 1 | `--pin-system-prompt` + `--gpu-memory-utilization 0.75→0.80` | 消除每请求驱逐 | `[prefix-pressure-evict]` **计数→0** | 删参数 / 回 0.75 |
| 2 | `PROXY_HBE_ENABLED=false` + `PROXY_CTX_EPOCH_TRIGGER_TOKENS 70000→50000` | 去附加开销 + 降 live KV | 纯 decode / TTFT 回升 | 改回原值 |
| 3 | rapid-mlx `0.12.13 → 0.14.3`（`.venv-rapidmlx`，**须加 `LLAMA_SERVER_BIN`**） | **用上游 hybrid checkpoint 机制替换本地 P1a 补丁** + GDN prefill 优化 | `snapped to checkpoint` 触发、decode 升 | 装回 0.12.13 / 删 bin 行 |

**均不改模型权重、不改输入内容语义、不改客户端配置。**

---

## 1. 前置条件与硬约束

1. **顺序硬约束（可执行判据，替代人通知）**：EXP-9 驱动退出 → **swe-eval 收官取证完成** → 才可重启代理/引擎。
   代理重启会清内存台账（R14 ledger / R16 聚合），**取证必须在前**。
   - **取证完成门 = `~/APP/swe-eval/results/experiments/<exp>/finale-*.md` 存在**（`scripts/batch_finale.py` 物化产出）——
     「文件存在即达标」，不再依赖会话间口头确认。
   - **L-31 约束**：判读侧的 `analyze --force` 必须在**代理重启之前**完成；重启后再跑会因内存台账被清而失真。
   - 以上两条未满足，**不得进入 §3（Step 1 需重启后端）**。
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

# ④ snap 路径计数（B3：区分本地 P1a 补丁 vs 上游 checkpoint，并计回退全冷）
tail -c 3000000 logs/llama-server.log | grep -cE 'LCP snapdown|snapped to checkpoint'  # snap 命中
tail -c 3000000 logs/llama-server.log | grep -c 'LCP unavailable'                      # 回退全冷信号
```

**关键指标**（口径见分析文档 §2）：纯 decode（`finished normally`）、TTFT（`first token after`）、
`prefix-pressure-evict` 计数、Metal `active/peak/cap`、cache_entries/cache_mem、
**`snapdown`/`snapped` 计数与 `LCP unavailable`（回退全冷）计数**、**epoch 全冷重建轮计数**。

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
- 🔴 **关键前提（2026-09-20 实测修正）**：**必须先抬高 `iogpu.wired_limit_mb`，否则本步无效**——
  `cap = max_recommended_working_set_size × util`（`vllm_mlx/scheduler.py:4417`），默认 base 仅
  **37.44 GiB** → 0.80 只得 30.2GB，**仍低于实际 peak 31.0GB** → 驱逐照旧。详见 §12.7。
- 📌 **B3**：此前 0.80 变差的「前科」已消失——现为单引擎（辅助端口 8084 无监听），
  `rapidmlx-kvcache-mechanism` §8.2 的退化是**双引擎 Metal 竞争**所致，本步可放心重试。
  且条目存活↑ → 检查点存活↑ → snap 收益同向放大（判定时按 §2④ 区分收益来自驱逐消除还是 snap）。
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
- 📌 **B3**：S 70000→50000 与 `rapidmlx-kvcache-mechanism` §8.4 的 S=110000 校准**方向相反**（目标不同：
  本步 decode 优先）。折叠频率↑ → **全冷重建轮↑**（重建期正是 LCP 发散场景），其代价现被 snap 部分对冲
  ——判定时必须**同时记全冷轮计数**（见 §2④），否则收益/代价会算错。
- ⛔ 回滚：改回 `PROXY_HBE_ENABLED=true`、`PROXY_CTX_EPOCH_TRIGGER_TOKENS=70000`，`./manage.sh reload`

---

## 5. Step 3 — 升级引擎 rapid-mlx 0.14.3（`stop-backend` + `start-backend`）

> **评审修订（附录 B / B1+B2）**：本步不是普通升级，而是「**用上游内置机制替换本地 P1a 补丁**」，
> 且按原写法会**静默无效**。执行前必须完成下列修正。

### 5.1 版本基线（先澄清，勿沿用旧假设）

| 项 | 实际值 | 说明 |
|---|---|---|
| **运行中引擎** | `/opt/homebrew/bin/rapid-mlx` = **brew 0.12.12** | 后端 PID 实测；`configs/ornith-oq4e.conf` **未设** `LLAMA_SERVER_BIN` |
| `manage.sh` 解析 | `manage.sh:623` `${LLAMA_SERVER_BIN:-rapid-mlx}` → PATH 的 Brew | 未设时回退 brew |
| `.venv-rapidmlx` | **0.12.13**（当前**未被** ornith 配置使用） | 仅 `qwen3.8-27b-4bit.conf` 用 `LLAMA_SERVER_BIN` 指向它 |

→ 故真实升级路径是 **brew 0.12.12 →`.venv-rapidmlx` 0.14.3**（不是 0.12.13→0.14.3）。

### 5.2 性质：用上游机制替换本地 P1a 补丁

- 0.14.3 wheel **包已改名** `vllm_mlx → rapid_mlx`（`vllm_mlx` 仅 deprecation shim），本地 P1a 补丁
  （`patches/vllm-mlx/0.12.12/`，5 文件）**无法沿用**；但上游**已内置同族机制**
  `rapid_mlx/hybrid_state_checkpoints.py`（LCP + supersequence 双路径，零 API 配合）。

| 维度 | 本地 P1a（0.12.12 补丁） | 上游 0.14.3 |
|---|---|---|
| 检查点位置 | 消息边界（chat 路由计算） | prefill chunk 边界（stride 2048，自动） |
| 覆盖窗口 | M=8 消息边界，覆盖全上下文 | 默认 4×2048 ≈ **最近 ~8K** |
| 路径 | 仅 LCP | LCP + supersequence |
| 依赖 | 需 batched/request/scheduler 配合 | 零 API 配合 |
| 日志 / 开关 | `LCP snapdown` / `PROXY_CACHE_LCP_SNAPDOWN` | `snapped to checkpoint` / `RAPID_MLX_HYBRID_CHECKPOINT_MAX=0` |

### 5.3 执行

```bash
# ⓪ 前置：确认 active 引擎二进制版本（评审建议；区分 brew 0.12.12 vs venv）
PID=$(pgrep -f 'rapid-mlx serve' | head -1)
ps -p "$PID" -o command= | cut -c1-160          # 期望看到 /opt/homebrew/... (brew 0.12.12)
/opt/homebrew/bin/rapid-mlx --version            # 0.12.12
.venv-rapidmlx/bin/rapid-mlx --version           # 0.12.13（当前未启用）

# ① 升级 venv
.venv-rapidmlx/bin/pip install -U "rapid-mlx==0.14.3"
.venv-rapidmlx/bin/rapid-mlx --version           # 应显示 0.14.3

# ② B1：给 ornith 配置加 LLAMA_SERVER_BIN（否则仍回退 brew 0.12.12）
#    configs/ornith-oq4e.conf 增行：
#    LLAMA_SERVER_BIN="/Users/jinsongwang/APP/llama.cpp/.venv-rapidmlx/bin/rapid-mlx"

# ③ 换引擎
./manage.sh stop-backend
./manage.sh start-backend

# ④ 版本断言（确认真的走 venv 引擎，而非 brew）
PID=$(pgrep -f 'rapid-mlx serve' | head -1)
ps -p "$PID" -o command= | grep -q '.venv-rapidmlx' && echo "OK: venv 引擎" || echo "FAIL: 仍走 brew"
curl -s http://127.0.0.1:4000/api/status | python3 -c "import json,sys;print(json.load(sys.stdin)['state'])"
```

### 5.4 B2 修正（窗口 adequacy / 验收口径 / off 臂开关）

0. **观测面前置（联合待办 #9）**：按 §10.3 应用 `patches/vllm-mlx/0.14.3/cachestats-counters.diff`，
   并断言 `/metrics` 出现 `snapdown_*` 字段——否则升级后**实时归因倒退**（上游无检查点计数）。
   `tools/lcp_trace_analyzer.py` 的新日志口径适配仍待办，过渡期以 `/metrics` 为准。

1. **实测窗口够不够**：E1 实测中位发散点在请求长度 **81% 处**（距尾 ~19%）；上游默认窗口 ~8K，
   40K 请求时发散点可能**落在窗口外 → 回退全冷**。**先在 ~40K 请求上测 `snapped to checkpoint` 是否触发**；
   不够则调大 `RAPID_MLX_HYBRID_CHECKPOINT_MAX` / `STRIDE`（注意检查点内存占用）。
2. **验收日志改口径**：从 `LCP snapdown` 改为
   `[cache_fetch] LCP snapped to checkpoint: shared=... resumed_at=...`；`LCP unavailable` 仍作回退全冷信号。
3. **EXP-8 LCP off 臂开关改写**：`PROXY_CACHE_LCP_SNAPDOWN=0` → `RAPID_MLX_HYBRID_CHECKPOINT_MAX=0`
   （**不改则 off 臂失去含义、对照失效**）。

### 5.5 判定 / 风险 / 回滚

- ✅ 通过：~40K 请求出现 `snapped to checkpoint`；`cache_fetch` 不再整链 MISS；长会话 decode 提升
- ⚠️ 风险：0.14.x 变更了包名/解析器/默认值（Ornith 为非别名模型，走架构自动检测）；升级后回归
  **工具调用 / 流式 / 冷 prefill** 三件套
- ⚠️ 风险：MTP/hybrid 交互（rapid-mlx #477）——本步**不启用** `--force-spec-decode`，仅验证缓存/前缀改善；
  投机解码单独立项（分析文档 §11.4）
- ⚠️ **世代边界**：Step 2（epoch S 变更）+ Step 3（引擎 0.14.3）落地后即**新引擎世代**——
  EXP-10 基线复用须核对、跨实验对照须标注（见 §9 与联合文档世代登记）
- ⛔ 回滚：删 `LLAMA_SERVER_BIN` 行（回 brew 0.12.12）或 `.venv-rapidmlx/bin/pip install "rapid-mlx==0.12.13"`，重启后端

---

## 6. 统一验收标准

| 指标（口径） | 现状 | 目标线 |
|---|---|---|
| 长上下文（~40K）纯 decode | ~20 tok/s | **≥25 tok/s** |
| TTFT p95 | 17.7s | **<12s** |
| `prefix-pressure-evict` | 每请求 | **0** |
| 短上下文（5–10K）纯 decode | — | 70–90 tok/s（回归基线） |
| Step 3：~40K `snapped to checkpoint` | — | 触发（非回退全冷） |
| snap 命中 / `LCP unavailable`（回退全冷）计数 | — | 按 §2④ 记录并区分来源 |
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
| **上游窗口不足 → 回退全冷** | Step 3，~40K 发散点落在默认 ~8K 窗口外 | 调大 `RAPID_MLX_HYBRID_CHECKPOINT_MAX`/`STRIDE`；或回退 P1a 补丁线 |
| **本地 P1a 补丁与新包不兼容** | Step 3，`vllm_mlx`→`rapid_mlx` 改名 | 确认走上游 `hybrid_state_checkpoints.py`；删除/停用旧补丁 |
| **EXP-8 off 臂开关未改** | Step 3 后 | 改 `RAPID_MLX_HYBRID_CHECKPOINT_MAX=0`，否则对照失效 |
| 重启窗口 404 / 会话中断 | Step 1/3 | 执行前走引擎重启协调协议、避开在途批跑 |
| 归因混淆（多步叠加） | 连续执行 | **每步之间必须完成 §2 采集与判定**，再进下一步 |

---

## 8. 影子环境预跑（推荐）

> **更完整的「零影响分层验证 + 回退」设计见 §10 / §11**——本节仅为快捷入口。

三步骤可先在影子代理验证，避免扰动生产状态：

```bash
# 见 test/lib/promptfoo_env.sh 头注：从工作树起独立代理(:4021，引擎关、状态隔离)
bash test/run_tests.sh --promptfoo
```

若时间有限，至少对 **Step 3（升级）** 做影子冒烟，其余两步为纯配置、可直接在生产小窗口执行。

> ⚠️ **批期禁跑（评审 B3）**：「不扰动生产」仅在**代理状态**层面成立——promptfoo 推理仍打**共享后端 :8081**
> （`--max-num-seqs 1`），会并发排队干扰批跑与验收窗口。**批期（EXP-9/EXP-8 在跑）禁跑**。

---

## 9. 需要的配合（转发相关方）

| 相关方 | 需要配合 |
|---|---|
| swe-eval | 出 `finale-*.md`（取证完成门）；批跑窗口避开 Step 1/3 的重启窗口；`analyze --force` 须在代理重启前完成（L-31） |
| 值守 | Step 1/3 执行期间关注 404 与会话中断；按协调协议广播 |
| 本仓（llama.cpp） | 执行三步、采集数据、回写本 Runbook 与联合文档；**世代边界登记**（Step 2+3 后为新引擎世代，EXP-10 基线复用须核对） |

---

## 10. 升级前隔离验证设计（零影响）

**目标**：在**不触碰生产**（不改 `configs/active.conf`、不重启生产后端、不写共享 HF/状态缓存）的前提下，
把引擎升级（Step 3）与引擎参数（Step 1）验证到「可切换」状态；生产切换只在最后一步、原子完成（§11.2）。

### 10.1 零影响三不变量

| # | 不变量 | 落实 |
|---|---|---|
| I1 | 不重启/不改生产后端（:8081） | 影子引擎走**独立端口 :8090**、独立进程 |
| I2 | 不改生产配置 | **不改** `configs/active.conf` 与 `configs/ornith-oq4e.conf`；影子用命令行参数 |
| I3 | 不污染共享缓存/状态 | `HF_HUB_OFFLINE=1` + `RAPID_MLX_STATE_DIR=/tmp/ornith-staging/state`；日志落 `/tmp` |

**并行 venv（关键）**：**不升级** `.venv-rapidmlx`（生产在用，且被 `qwen3.8-27b-4bit.conf` 引用），
而是新建 `.venv-rapidmlx-0143` 装 0.14.3；brew 0.12.12 原样保留。**任何影子步骤都不写生产 venv/brew**。

### 10.2 分层验证（按资源代价递增）

| 层 | 载体 | 资源 | 生产在跑时可并行 | 验证内容 |
|---|---|---|---|---|
| **T0 静态** | 无 | ~0 | ✅ 随时 | 包改名/`rapid_mlx` 导入/flag 存在性/`hybrid_state_checkpoints.py`/P1a 补丁覆盖对照/**snapdown 计数补丁已应用**（§10.3） |
| **T1 小模型** | `ornith-ai/Ornith-1.5-9B-MLX-4bit`（**同族 GDN hybrid**，~5GB，已缓存） | ~5GB | ✅ 通常可（内存紧张则等空闲） | boot/工具/流式/推理/prefix 复用/**checkpoint 机制是否生效 + `/metrics` snapdown 字段** |
| **T2 真模型影子** | 同 oQ4e 35B | ~27GB | ❌ **需生产后端让位**（48GB 装不下两台 35B） | ~40K A/B：decode/TTFT/`snapped to checkpoint`/**窗口 adequacy（B2 核心未知）** |
| **T3 生产切换** | 生产 | — | — | 原子切换 + 回退（§11） |

**T0（任意时刻，零内存）**
```bash
python3 -m venv .venv-rapidmlx-0143
.venv-rapidmlx-0143/bin/pip install "rapid-mlx==0.14.3"
.venv-rapidmlx-0143/bin/rapid-mlx --version                 # 应 0.14.3
.venv-rapidmlx-0143/bin/python -c "import rapid_mlx,vllm_mlx;print(rapid_mlx.__version__)"  # 改名 + shim
.venv-rapidmlx-0143/bin/rapid-mlx serve --help | grep -iE 'hybrid|checkpoint|pin-system|pflash'
ls .venv-rapidmlx-0143/lib/python*/site-packages/rapid_mlx/hybrid_state_checkpoints.py
# B2 观测面：#9(a) snapdown 计数补丁已应用（随 Step 3 落地）——见 §10.3
```

### 10.3 B2 观测面缺口（联合待办 #9）——补丁已应用 + 运行时验证通过

**缺口**：上游 0.14.3 `CacheStats` **无检查点计数**（`_snap_hybrid_trim` 命中只记通用 `hits`/`tokens_saved`），
且日志口径从 `LCP snapdown` 变为 `snapped to checkpoint`，导致 `tools/lcp_trace_analyzer.py` **失效**。

**现状**（`a15f9b8` 编写 → 2026-09-20 Step 3 窗应用，已闭合）：补丁已应用到 `.venv-rapidmlx-0143`，
运行时验证通过（`/metrics` 出 `snapdown_*`，长会话 `snapdown_hits=1`）。详见 §15。

| 项 | 状态 |
|---|---|
| 补丁 | `patches/vllm-mlx/0.14.3/cachestats-counters.diff`（+ `memory_cache.py` / `routes_metrics.py` / `README.md`） |
| 内容 | `CacheStats` +`snapdown_hits`/`snapdown_tokens_saved`（随 hits reset 契约）；`to_dict` 输出；LCP + supersequence 两命中点递增；`routes/metrics` counter +2（`rapid_mlx_prefix_cache_snapdown_*_total`） |
| 离线验证 | ✅ `py_compile` + 语义自检 5/5（基准 = PyPI 0.14.3 wheel） |
| **应用 / 运行验证** | ✅ **已完成**（2026-09-20，随 Step 3；`/metrics` 实测 snapdown 计数） |

**Step 3 前置断言（加在 §5.3 ⓪ 后）**
```bash
# 应用补丁（照 patches/vllm-mlx/0.14.3/README.md，勿盲目套 diff：版本漂移注意）
ls .venv-rapidmlx-0143/lib/python*/site-packages/rapid_mlx/memory_cache.py
grep -n 'snapdown_hits' .venv-rapidmlx-0143/lib/python*/site-packages/rapid_mlx/memory_cache.py

# 起影子后断言 /metrics 出现新字段
curl -s localhost:8090/metrics | grep -c 'snapdown'     # 期望 ≥1（或用 /metrics 全量键检查）
```

**仍待处理（不在本次补丁内）**：`tools/lcp_trace_analyzer.py` 对**新日志口径**（`snapped to checkpoint`）的适配——
未适配前该工具的实时归因不可用，Step 3 后须以 `/metrics` 新字段为准（并找回该工具）。

**触发**：EXP-9 收官 + Step 3 升级窗（联合协调 0920-12 / 待办 #9）。

**T1（小模型，隔离端口 8090）**
```bash
STG=/tmp/ornith-staging; mkdir -p $STG/state $STG/logs
HF_HUB_OFFLINE=1 RAPID_MLX_STATE_DIR=$STG/state \
  .venv-rapidmlx-0143/bin/rapid-mlx serve ornith-ai/Ornith-1.5-9B-MLX-4bit \
  --no-mllm --port 8090 --gpu-memory-utilization 0.20 --cache-memory-mb 2048 \
  --max-num-seqs 1 --hybrid-cache-entries 8 > $STG/logs/engine-9b.log 2>&1 &
curl -s localhost:8090/v1/models | head -c 200              # 影子就绪
# 冒烟：工具调用 / 流式 / 两轮前缀复用；grep 日志 checkpoint 行
kill %1                                                      # 仅退出影子进程
```

**T2（真模型影子，生产让位窗口 —— 预验证阶段唯一的生产中断点）**
```bash
./manage.sh stop-backend                                     # 生产中断开始
HF_HUB_OFFLINE=1 RAPID_MLX_STATE_DIR=$STG/state \
  .venv-rapidmlx-0143/bin/rapid-mlx serve pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp \
  --no-mllm --port 8090 --gpu-memory-utilization 0.80 --cache-memory-mb 12288 \
  --max-num-seqs 1 --default-repetition-penalty 1.0 --hybrid-cache-entries 32 \
  --pin-system-prompt > $STG/logs/engine-35b.log 2>&1 &
# A/B：基线臂 = brew 0.12.12 同命令（无 --pin-system-prompt）；对照臂 = 0.14.3（含之）
# 直连 :8090，各跑 ~40K 请求；采集 §2 指标 + grep 'snapped to checkpoint' / 'LCP unavailable'
kill %1
./manage.sh start-backend                                    # 生产中断结束（回现状）
```

> **Step 2（HBE off + S）为纯代理、reloadable**，无需影子：生产上 `./manage.sh reload` 即生效，
> 秒级回退（§11.1）。其「全冷轮代价」用 §2④ 计数在判定时一并量。
### 10.4 回退演练（T2 窗口顺带）

改配置 → 起影子 → 测 → **回退** → 再起，实测回退耗时（目标 ≤5 min），确认回退后 §2 指标回到基线。

### 10.5 T2 执行要点（实测得出，勿踩）

- **分叉深度必须精确控制**：B2 问的是「上游 checkpoint 窗口 vs 真实发散点」。首次测试把分叉点放在
  **token 4**（head 处）→ `LCP unavailable` + `snapdown_hits=0` → 得到**假阴性**。
  正确做法：构造 **共享 HEAD（~81%）+ 分叉 MID + 共享 TAIL**，分叉点落在 E1 实测的中位深度。
  ```python
  content_A = f"{HEAD} {MID_A} {TAIL}"   # HEAD 占 ~81%
  content_B = f"{HEAD} {MID_B} {TAIL}"   # 仅 MID 不同 → LCP shared ≈ len(HEAD)
  ```
- **并发观察 `/metrics`**：`rapid_mlx_prefix_cache_snapdown_hits_total` 与日志
  `LCP snapped to checkpoint: shared=... resumed_at=... remaining=...` 应同步出现。
- **venv 建站需 Python ≥3.10**：`python3` 在本机是 **3.9**，必须用
  `/opt/homebrew/bin/python3.14 -m venv`（否则 `pip install rapid-mlx` 报「no matching distribution」）。

---

## 11. 回退设计（可回退保证）

### 11.1 分层与不可变制品

| 层 | 变更 | 不可变制品 / 备份 | 回退动作 | 生效时间 |
|---|---|---|---|---|
| 引擎 | 0.14.3 | **并行 venv** `.venv-rapidmlx-0143`（生产 venv/brew 不动） | 删 `LLAMA_SERVER_BIN` 行（回 brew 0.12.12）或指回旧 venv | 重启后端 |
| 配置 | conf 行 | `cp configs/ornith-oq4e.conf /tmp/ornith-oq4e.conf.bak-<ts>` | `cp` 回 + `restart`/`reload` | 秒–分钟 |
| 代理行为 | `PROXY_HBE_ENABLED true→false`、`PROXY_CTX_EPOCH_TRIGGER_TOKENS 70000→50000` | §3/§4 diff 记录原值 | 改回原值 + `./manage.sh reload` | **秒级** |
| 模型 | 无（复用缓存） | — | — | — |
| 文档/代码 | git | commit / tag | `git revert <sha>` | — |

### 11.2 原子切换
生产侧所有变更集中为**一次**操作：`configs/ornith-oq4e.conf` 加 `LLAMA_SERVER_BIN` +
（可选）`--pin-system-prompt` / `gpu 0.80` → `./manage.sh stop-backend && ./manage.sh start-backend`。
失败即：

```bash
cp /tmp/ornith-oq4e.conf.bak-<ts> configs/ornith-oq4e.conf
./manage.sh stop-backend && ./manage.sh start-backend
```

### 11.3 回退决策闸（任一触发即回退）
- 冷 prefill 回归 **>20%**，或 ~40K decode **低于现状**；
- 工具调用失败率↑ / `L3` 循环↑ / 解析器异常；
- `snapped to checkpoint` **从未触发**（= B2 窗口不足）；
- TTFT p95 恶化；Metal `watchdogd` panic。

### 11.4 回退后验证
跑 §2 采集，确认 decode/TTFT/驱逐数回到基线；`/api/status` `state=healthy`；工具调用冒烟通过。

### 11.5 制品清单（可追溯）
- venv：`.venv-rapidmlx-0143`（`pip freeze > /tmp/ornith-staging/manifest.txt`）
- 配置备份：`/tmp/ornith-oq4e.conf.bak-<ts>`
- 影子日志/状态：`/tmp/ornith-staging/logs/*.log`、`/tmp/ornith-staging/state`（可随时删除，不影响生产）

---

## 12. T2 实测结果（2026-09-20 执行完毕）
> 状态：**已完成**。生产已恢复（新后端 PID 94938，`state=healthy`）。原始数据
> `/tmp/ornith-staging/logs/{T2-RESULTS.md,t2c-*.log,t2c-*-r*.json}`；执行器 `tools/t2_gate_watch.sh`。
> 放行依据：**`exp-20260919-select-gate/finale-20260920-055914`（全部步骤 rc=0）** + 用户确认取证完成。

### 12.1 结论（B2 已解答）

**上游 0.14.3 checkpoint 窗口在 81% 分叉处成功命中——无需调大 `CHECKPOINT_MAX/STRIDE`。**

### 12.2 A/B（同一 ~31K prompt，81% 深度分叉）

| 指标 | brew 0.12.12（现状） | 0.14.3 + pin | 提升 |
|---|---|---|---|
| A 冷 prefill | 77.5s | **30.7s** | **2.5×** |
| B 81% 分叉 | **78.5s（`LCP unavailable` 全冷）** | **8.8s（snap 命中）** | **8.9×** |
| `snapdown_hits` | 0（机制不存在） | **1** | — |
| `tokens_saved` | 0 | **24576** | — |
| 复用率 | 0% | **79%**（24576/30976） | — |

关键日志：`[cache_fetch] LCP snapped to checkpoint: shared=25069 entry_len=30752 resumed_at=24576 remaining=6400`

### 12.3 判读

1. **升级是数量级收益**：现状在长会话**每次前缀分叉都全冷**（78.5s）；0.14.3 仅 8.8s。直接命中
   本 Runbook 要治的瓶颈（§0 目的达成）。
2. **B2 窗口 adequacy ✅**：81% 分叉命中 → 默认窗口够用，不必调参（此项此前是最大未知）。
3. **B2 计数补丁运行时验证 ✅**：`snapdown_*` 已实时出现在 `/metrics`；观测面缺口 #9(a) 闭合。
4. **B1 补充**：brew 0.12.12 **自身也有 `--pin-system-prompt`** —— pin 不是升级独有，差异在 checkpoint 机制。
5. **disk-stream**：0.14.3 registry 仍**无 `qwen3_5_moe`** → Ornith 依旧不可用（维持 §5.D 观察项结论）。

### 12.4 落地到生产的清单（待用户决策，§5/§11）

```bash
# ① 生产环境引入 0.14.3 引擎（B1：必须显式指定二进制，否则回退 brew）
#    configs/ornith-oq4e.conf 增行：
LLAMA_SERVER_BIN="/Users/jinsongwang/APP/llama.cpp/.venv-rapidmlx-0143/bin/rapid-mlx"

# ② 应用 B2 计数补丁到生产 venv（照 patches/vllm-mlx/0.14.3/README.md）
cp patches/vllm-mlx/0.14.3/memory_cache.py   .venv-rapidmlx-0143/lib/python3.14/site-packages/rapid_mlx/memory_cache.py
cp patches/vllm-mlx/0.14.3/routes_metrics.py .venv-rapidmlx-0143/lib/python3.14/site-packages/rapid_mlx/routes/metrics.py

# ③ 原子切换（§11.2）
cp configs/ornith-oq4e.conf /tmp/ornith-oq4e.conf.bak-$(date +%s)
./manage.sh stop-backend && ./manage.sh start-backend
# 断言：ps 命令含 .venv-rapidmlx-0143；/metrics 有 snapdown_* 字段

# ④ EXP-8 off 臂开关改写（B2 第 3 项）
#    PROXY_CACHE_LCP_SNAPDOWN=0  →  RAPID_MLX_HYBRID_CHECKPOINT_MAX=0
```

### 12.5 遗留

| 项 | 状态 |
|---|---|
| `tools/lcp_trace_analyzer.py` 新日志口径适配 | ✅ **已完成**（`RE_SNAP_V143`/`RE_SUPER_SNAP_V143` 已在库；T2 日志实测解析正确：snapdown=1、复用 24576、节省 79.3%、粒度损失 p50 493） |
| `tools/t2_gate_watch.sh` 门控 bug | ✅ **已修**（G1 用驱动特征名 + 排除自身 PID；G2 改扫全部 exp 目录且「新鲜」改为可选 `T2_REQUIRE_FRESH`；G3 以 watcher **进程存活**为准；G2b 拒绝 rc≠0 的取证件）。三分支单测通过（OPEN/CLOSED/rc=1→CLOSED） |
| 生产切换决策 | ✅ 已完成（§15，0.14.3 + A1 落地） |
| T1 影子 / 门控 watcher | 已全部停止；生产后端已恢复（PID 94938） |

### 12.6 门控脚本修复说明（复盘）

本次 T2 为**人工绕过**放行，原因三条，均已在脚本中修正：

1. **G1 假阳性**：`pgrep -f 'exp9'` 匹配到 watcher 自身命令行（含 `t2_gate`+日志路径）。
   → 改为匹配驱动特征名（`batch_finale\.py` 等）并 `grep -v "^$$$"` 排除自身；另支持 `T2_DRIVER_PID`。
2. **G2 过期目录**：硬编码 `exp-20260907-claimtest`，该目录在收官后**已被清理** → 永远找不到。
   → 改为 `find "$EXP_ROOT"` 扫全部 exp 取最新；「文件比 watcher 新」的时序要求改为
   **可选** `T2_REQUIRE_FRESH=1`（否则 watcher 晚于取证启动时会永久卡住）。
3. **G3 僵尸尾行**：finale-watch 进程已死，但日志尾行遗留 `state=running` → 永久卡门。
   → 改为**先查 `.finale-watch.pid` 进程是否存活**，仅存活时才判 `state`。

---

## 13. Step 1 小窗口实测结果（2026-09-20 执行完毕）

> 状态：**已执行**（配置当前生效中）。原始数据 `/tmp/ornith-staging/logs/{STEP1-RESULTS.md,step1*.log}`。

### 13.1 结论：✅ 驱逐归零 —— 但决定性杠杆是 `iogpu.wired_limit_mb`，不是 `--gpu-memory-utilization`

| 指标 | 基线 | Step 1（旧 cap 30.2GB） | Step 1（**新 cap 36.1GB**） |
|---|---|---|---|
| `prefix-pressure-evict` | 974 | 464（未消除） | **0** ✅ |
| `adaptive_prefill →256` | 598 | 301（未消除） | **0** ✅ |
| Metal cap | 30.2GB | **30.2GB（完全未变）** | **36.1GB** |
| Metal active / peak | 27.1 / 31.0GB | 27.1 / 31.0GB | 23.8 / **26.7GB** |
| append-only 会话耗时 | 常有 10–265s | — | **1.27–1.28s（连续 HIT）** |

### 13.2 机制（源码级，重要认知修正）

`vllm_mlx/scheduler.py:4417 _resolve_metal_cap_bytes()`：

```python
base = mx.device_info()["max_recommended_working_set_size"]
cap  = int(base * gpu_memory_utilization)
```

本机实测与推算：

| 条件 | base | util 0.80 → cap | 结果 |
|---|---|---|---|
| 默认（`iogpu.wired_limit_mb=0`） | **37.44 GiB** | **30.2GB** | **< peak 31.0GB → 每请求驱逐** |
| `iogpu.wired_limit_mb=43008` 后 | **45.1 GB** | **36.1GB** | **> peak → 驱逐归零** ✅ |

启动日志证据：`Metal memory limits set: allocation_limit=36.1GB (80% of 45.1GB)`

**教训**：
1. **`--gpu-memory-utilization` 的基数是 macOS 的 `max_recommended_working_set_size`（默认仅 37.44GiB，不是物理 48GiB）** —— 单纯调 util 无法突破该封顶，Step 1 单独执行**无效**。
2. **`--pin-system-prompt` 保护的是条目优先级，不是容量** —— 容量驱逐下无济于事。
3. **决定性手段 = §5.C `sudo sysctl iogpu.wired_limit_mb=43008`（抬高 base）**，须在重启后端**前**设置。
4. **`wired_limit` 是运行时参数，重启失效** → 需持久化（§13.4）。

### 13.3 测试设计教训（避免误判回归）

首次用「每轮内容全不同、递增长度」的压力序列，观测到耗时线性劣化（18.9s→183.4s）且
`HIT` 为空 —— 这是**测试设计问题**（内容无前缀关系 → 每轮全冷），**不是回归**。
改用**真实 agent 形态（固定 system 前缀 + append-only 逐轮增长）**后，连续 HIT、耗时稳定 ~1.3s：
```
HIT prompt_tokens=1125 cached=819  remaining=306
HIT prompt_tokens=1445 cached=1139 remaining=306
HIT prompt_tokens=1765 cached=1459 remaining=306
```
→ **验证性能必须用 append-only 会话形态**（Runbook §2 采集口径补充）。

### 13.4 待办：`wired_limit` 持久化（§5.C 落地）

运行时 `sysctl` 重启丢失，需 LaunchDaemon plist：
```bash
sudo tee /Library/LaunchDaemons/com.local.iogpu-wired-limit.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.local.iogpu-wired-limit</string>
  <key>ProgramArguments</key>
  <array><string>/usr/sbin/sysctl</string><string>iogpu.wired_limit_mb=43008</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
sudo launchctl load /Library/LaunchDaemons/com.local.iogpu-wired-limit.plist
```
⚠️ 48GB 档上限 43008（留 ~6GB 给系统）；**贴近上限有 `watchdogd` panic 风险**（参见
`turboquant-mlx` 实测），需持续观察。

### 13.5 当前生产状态
- 后端 **PID 7757**，配置 = Step 1（`gpu 0.80` + `--pin-system-prompt`）+ `wired_limit=43008`
- 回退：`/tmp/ornith-staging/ornith-oq4e.conf.prestep1`（删 pin + 回 0.75）；`wired_limit` 回退 `=0`

---

---

## 14. Step 2 小窗口实测结果（2026-09-20 执行完毕）

> 状态：**已执行**（配置当前生效中）。回退点 `/tmp/ornith-staging/ornith-oq4e.conf.prestep2`。
> 手段：纯 `./manage.sh reload`（引擎未重启，秒级回退）。

### 14.1 改动与验证

```diff
-export PROXY_HBE_ENABLED=true
+export PROXY_HBE_ENABLED=false
-export PROXY_CTX_EPOCH_TRIGGER_TOKENS=70000
+export PROXY_CTX_EPOCH_TRIGGER_TOKENS=50000
```

`reload` 后 `/api/status` 断言：`epoch_S=50000`、`window_K=24`、`state=healthy` ✅

### 14.2 实测（Step 1 之上叠加）

| 指标 | Step 2 结果 |
|---|---|
| HBE 停采 | ✅ `logs/diag/hbe.jsonl` 2630 → 2630（不再增长） |
| `prefix-pressure-evict` | **0**（Step 1 已归零，Step 2 保持） |
| append-only 会话耗时 | 2.66s → 1.50s → 1.52s → **1.55s**（稳定） |
| prefix HIT | `cached=916 → 1366 → 1816`（增量稳定 437）✅ |
| epoch 折叠 | **0**（60 轮未达 S=50K） |

### 14.3 判读与遗留

- ✅ **pass**：HBE 探针开销消除；会话延迟稳定在 ~1.5s；驱逐保持为 0。
- ⚠️ **未观测到 S 下调的代价**：本次会话规模小（60 轮 ≈ 2.2K tokens），**远未触发 S=50K 折叠**，
  故 B3 提出的「折叠频率↑ → 全冷重建轮↑」风险**本次无法验证**。需在**真实长会话**（接近 50K 视图）
  上补测，判定时按 §2④ 同时记 `snapdown`/`LCP unavailable`/全冷轮计数。
- ⚠️ HBE 停采的代价：`logs/diag/hbe.jsonl` 断流（标定/实验用途）。如需保留，可改
  `PROXY_HBE_SAMPLE_EVERY=8` 折中（GLM-4.5 无此选项，仅本地路径）。

### 14.4 当前生产状态（Step 1 + Step 2 叠加）
- 后端 PID 7757：`gpu 0.80` + `--pin-system-prompt` + `cap 36.1GB`
- 系统：`iogpu.wired_limit_mb=43008`
- 代理：`HBE_ENABLED=false`、`PROXY_CTX_EPOCH_TRIGGER_TOKENS=50000`（**注：后经 0920-15 影响三定案回 70000**，见 §18）

---

## 15. Step 3 落地记录：0.14.3 + 方案 A1（2026-09-20 执行完毕）

> 状态：**已落地并生产运行**。回退点 `/tmp/ornith-staging/ornith-oq4e.conf.prestep3a1-20260920-214910`。

### 15.1 执行中发现的阻塞：0.14.3 拒绝 hybrid 的 KV 量化

按 §12.4 首次尝试（加 `LLAMA_SERVER_BIN`、保留 `RAPID_MLX_KV_QUANTIZATION=true`）**启动失败**：

```
rapid_mlx.kv_cache_dtype.KVCacheQuantizationUnsupportedError:
--kv-cache-dtype int4 cannot be honored ...: the loaded model is incompatible: ArraysCache.
ERROR: Application startup failed. Exiting.
```

**根因（源码级）**：
- `cli.py:5030` `_kv_quant_explicit = bool(args.kv_cache_quantization) or (args.kv_cache_dtype != "bf16")`
- `scheduler.py:4709` 运行时探测到 `ArraysCache`（GatedDeltaNet recurrent state）→ **显式量化请求 fail closed 抛异常**；
  非显式请求才静默降级 bf16
- `_supports_hybrid_partial_quantization()` 的白名单**仅 gemma4**（`model_type in {gemma4, gemma4_assistant, gemma4_unified}`）——
  Ornith/Qwen3.5/3.8 的 `qwen3_5_moe` **未获豁免**，尽管它们有 10/40 层是全注意力、理论上可部分量化

**0.14.3 KV 量化支持面**：

| 类型 | 支持 | 说明 |
|---|---|---|
| `bf16` | ✅ 全机型默认 | — |
| `int8`/`int4` | ✅ 仅全注意力/兼容机型 | `QuantizedKVCache` + fused attention |
| hybrid GDN（Ornith/Qwen3.5-3.8） | ❌ 被拒 | 运行时 `ArraysCache` 探测 → fail closed |
| hybrid 部分量化 | ⚠️ 仅 gemma4 白名单 | `qwen3_5_moe` 不在内 |
| `--kv-cache-turboquant k8v4` | ✅ 存在（独立路径） | 可能绕过 safelist，未验证；AGENTS.md §8.3 有告诫 |

### 15.2 方案 A1：关闭 KV 量化（bf16）

```diff
+LLAMA_SERVER_BIN="/Users/jinsongwang/APP/llama.cpp/.venv-rapidmlx-0143/bin/rapid-mlx"
-RAPID_MLX_KV_QUANTIZATION=true
+RAPID_MLX_KV_QUANTIZATION=false
```

落地断言：
- `ps` 命令含 `.venv-rapidmlx-0143` ✅（B1 版本断言）
- `KV cache dtype: bf16 (...) — bf16 selected (no QuantizedKVCache wrap)` ✅
- `Metal memory limits set (manual): allocation_limit=36.1GB (80% of 45.1GB)` ✅
- `state=healthy / ready=true` ✅

### 15.3 内存评估（A1 是否够用）

| 配置 | active | peak | cap |
|---|---|---|---|
| 旧 0.12.12（int4 KV） | 21.2–21.9GB | 24.3–25.0GB | 36.1GB |
| **0.14.3（bf16 KV）** | 24.2–26.4GB | **24.3–26.5GB** | 36.1GB |

- 差异 **+0.2 ~ +2.2GB**；**余量 ~10GB**
- **代价小的原因**：仅 **10/40 层有 KV cache**（30 层 GDN 是固定 recurrent state，KV dtype 不影响）；
  prefix 复用靠 recurrent-state checkpoint，与 KV dtype 解耦
- 长上下文阶梯：**60K 冷（69,273 tok）172.3s**；**104K（104,473 tok）129.5s（HIT 复用 69,248）**；
  **OOM=0、驱逐=1（干净段）** ✅

### 15.4 同口径 A/B（~30K，512 输出，81% 分叉）

| 指标 | 旧 0.12.12（int4 KV） | **0.14.3 + A1（bf16）** | 提升 |
|---|---|---|---|
| A 冷 | 99.6s | **43.7s** | **2.28×** |
| B 81% 分叉 | 101.7s（`LCP unavailable` 全冷） | **28.3s（snap 命中）** | **3.60×** |
| cached_tokens | 0 | **16384** | — |
| Metal peak | 31.0GB | **26.5GB** | -4.5GB |

> 注：3.60× 低于 T2 的 8.9×，因本次输出 512 token 摊薄了 prefill 占比；**纯 prefill 维度仍 ~2.3×**。

### 15.5 回归测试（0.14.3）
基础生成 / 流式 SSE / 工具调用（`get_weather{"city":"Tokyo"}`）/ 代理 :4000 Claude 形态
（`stop_reason=end_turn`, `REGRESSION_OK`）—— **全部通过，无行为回归**。

### 15.6 当前生产配置（Step 1+2+C+3+A1）
```
引擎   .venv-rapidmlx-0143 (0.14.3)
KV     bf16 (A1)
GPU    0.80 → cap 36.1GB
flags  --pin-system-prompt --hybrid-cache-entries 32
系统   iogpu.wired_limit_mb=43008（LaunchDaemon 持久化）
代理   HBE off, PROXY_CTX_EPOCH_TRIGGER_TOKENS=50000（后回 70000，见 §18）
```

---

## 16. MAX/STRIDE 调研与不确定性（2026-09-20）

> 结论：**不采纳调参（保守，保持默认 4/2048）**。MAX/STRIDE 非决定性杠杆，其收益被「LCP 条目选择」掩盖。

### 16.1 参数语义

`rapid_mlx/hybrid_state_checkpoints.py`：
- `RAPID_MLX_HYBRID_CHECKPOINT_MAX`（默认 **4**）——每层保留的检查点数；超出时**丢弃与前一检查点间隔最小者**，最新者永不丢
- `RAPID_MLX_HYBRID_CHECKPOINT_STRIDE`（默认 **2048**）——两检查点最小 token 间隔
- 检查点在 **prefill chunk 边界**按 stride 自动记录（`scheduler.py:5107`），零 API 配合

### 16.2 扫描结果（同口径 A冷 + B 81%分叉）

| MAX | STRIDE | A 冷 | B 分叉 | `resumed_at` | **粒度损失** |
|---|---|---|---|---|---|
| 4 | 2048 | 29.5s | 15.7s | 16384 | 7797 |
| **16** | **512** | 28.3s | **8.8s** | 22528 | **1653** |
| 8 | 2048 | 30.7s | 30.7s | — | 未触发 |
| 8 | 1024 | 30.6s | 30.7s | — | 未触发 |
| 16 | 1024 | 28.8s | 28.2s | — | 未触发 |

### 16.3 稳定性复测 —— 结论反转

`MAX=16/STRIDE=512` 连跑 **3 次**：**3/3 未触发**（`shared=NA`）。

**根因：不是参数，是「LCP 候选条目选择」不稳定**：
- 失败时日志：`LCP unavailable: shared=1 entry_len=13 requested_len=29888` → 匹配到**小条目**（warmup 残留）→ 拒绝
- 成功时日志：`LCP snapped to checkpoint: shared=24181 entry_len=29664 resumed_at=20480` → 匹配到 **A 的大条目** → 成功
- 两轮的 `entries` 数不同（6/7 vs 10），说明 **LRU 排序/条目集合**决定了候选

**结论**：`MAX/STRIDE` 的收益被条目选择掩盖；**调参不可靠**。

### 16.4 决策与后续

- ❌ **不调 MAX/STRIDE**（保持默认 4/2048）：避免引入不可复现的参数
- ✅ 已落地的 Step 1+2+C+3+A1 收益明确（**2.28× 冷 / 3.60× 分叉**），无需依赖调参
- 🔬 **待研究（低优先）**：
  - 为何小条目（`entry_len=13`）会抢占 LCP 候选 → 条目选择/LRU 策略
  - `--prefix-cache-index radix` 是否改善候选选择
  - `--cache-memory-mb` 扩大是否让大条目更易存活

### 16.5 测试方法教训（重要）

首版 A/B 构造有缺陷，导致全部 `shared=NA`：
1. **A/B 必须共享相同 system 消息** —— 否则 token 序列在头部就错位（`shared=23`），
   即使字符层面 81% 相同也不构成 token 前缀
2. 分叉点须精确落在 **81% 深度**（E1 实测中位发散点），否则落在 checkpoint 窗口外
3. 每臂须 **清空 `RAPID_MLX_STATE_DIR`**，否则上轮 cache 状态污染
4. 检查点落在 **stride × prefill-chunk** 边界，需与分叉位置对齐才可能命中

---

## 17. 当前版本性能基线（2026-09-20）

> 配置：rapid-mlx **0.14.3** + **bf16 KV（A1）** + `--pin-system-prompt` + cap 36.1GB + M5 Pro/48GB。
> 原始数据：`logs/bench-ctx-ladder-20260920-224859.json`、`/tmp/ornith-staging/logs/{matrix-results.txt,bench-ctx-ladder-current.log}`。

### 17.1 上下文长度 × 性能阶梯（`tools/bench_ctx_ladder.py`，256 tok/档）

| 上下文 | 实际 prompt | 冷 TTFT | prefill | 冷 decode | 热 TTFT | 热 decode | RSS 峰值 |
|---|---|---|---|---|---|---|---|
| 512 | 552 | 74.6s* | 7/s* | 55.1/s | 0.49s | 60.9/s | 0.24GB |
| 4K | 4,092 | 4.0s | 1,020/s | 54.3/s | 0.55s | 59.6/s | 0.25GB |
| 16K | 16,324 | 14.8s | 1,106/s | 52.1/s | 0.68s | 54.9/s | 0.28GB |
| 32K | 32,828 | 46.7s | 703/s | 48.7/s | 0.87s | 50.2/s | 0.33GB |
| 64K | 65,521 | 128.9s | 508/s | 36.1/s | 1.26s | 37.0/s | 0.38GB |
| 100K | 102,419 | 276.0s | 371/s | 29.6/s | 1.78s | 27.6/s | 0.48GB |
| 160K | 164,724 | 572.6s | 288/s | 23.4/s | 6.33s | 23.6/s | 0.60GB |

\* 首档 512 的 74.6s 是**首请求冷启动**（kernel 未预热），4K 档即恢复 4.0s。

**特征**：冷 prefill 峰值在 4K–16K（**1020–1106 tok/s**）；热 TTFT 全档 ≤100K 均 **<2s**；
decode 从短上下文 ~55 衰减至 160K 的 23.4（-58%）。

### 17.2 稳态 decode（关键：必须预热）

**三臂矩阵**（影子 :8090，各 3 次，同 prompt/500 tok）：

| 引擎 | KV | run1 | run2 | run3 | 均值 |
|---|---|---|---|---|---|
| brew 0.12.12 | int4 | 34.3 | 44.1 | 74.1 | 50.8（**波动大**） |
| brew 0.12.12 | bf16 | 72.9 | 74.5 | 74.7 | 74.0 |
| **venv 0.14.3** | **bf16** | 79.4 | 85.7 | 87.1 | **84.1** ✅ |

**结论**：
- **当前配置（0.14.3 + bf16）是三者最优（84.1 tok/s）**，**优于旧基线 79.8**
- **int4 KV 反而更慢且不稳**（34→74），印证 dequant-on-read 的 O(context) 代价 → **A1（关量化）是正确决策**
- **bf16 方差小**（72.9–87.1）；int4 方差大

### 17.3 测量方法教训（重要，避免重蹈）

1. **必须预热**：首请求冷启动（kernel/缓存未热）可低 **2×**。本次曾一度误判
   「0.14.3 比 0.12.12 慢 35%（54 vs 80）」，实为冷启动 + 长 bench 后状态污染
2. **单次采样不可靠**：int4 臂波动 34→74 tok/s；**必须 ≥3 次取稳定值**
3. **端到端 vs 纯 decode 要分清**：`Chat completion` 行含 prefill；输出越长 prefill 占比越低
4. **对比必须同口径**：同 prompt、同 max_tokens、同预热轮次、同上下文长度

### 17.4 与历史基线对照

| 时期 | 短上下文 decode | 备注 |
|---|---|---|
| 旧基线（2026-08-26，0.12.12 + int4） | 79.8 tok/s | `logs/baselines/ornith-oq4e.json`（单次 500tok） |
| **当前（2026-09-20，0.14.3 + bf16）** | **84.1 tok/s** | 三臂矩阵均值（预热后） |
| 优化前长上下文（0.12.12 + int4，cap 30.2） | ~20 tok/s | 受每请求驱逐拖累 |

> 注：长上下文「~20」与短上下文「~84」口径不同；驱逐修复（Step 1+C）后长上下文不再每请求全冷。

### 17.5 checkpoint 开销（隔离实验）

`CHECKPOINT_MAX=0`（关）vs `=4`（默认）同口径：**47.4 vs 39.9 tok/s** → 检查点约 **-8 tok/s（~17%）**。
但该开销被 bf16 的增益覆盖（净结果仍 84 > 74）；且 checkpoint 换来 **snap 复用（分叉场景 3.6×）**，
收益远大于代价。**保留默认**。

---

## 18. 对联合文档 0920-15 的响应（2026-09-20）

> 来源：`~/APP/swe-eval/docs/joint-coordination-llamacpp.md` §五 0920-15(swe-eval)（提交 `6f5feb4`）。

### 18.1 已核实事实（接受）

| 项 | 状态 |
|---|---|
| 引擎换代 `.venv-rapidmlx-0143`（PID 25033，`gpu 0.80` + `--pin-system-prompt`） | ✅ 即本 runbook §15 落地 |
| `iogpu.wired_limit_mb=43008` LaunchDaemon 持久化 | ✅ |
| 代理 `S=50000` 生效、HBE 停采 | ✅（后 S 回 70K，见 18.3） |
| **#9(a) 已应用且运行时可见** | ✅ **文档漂移已修**（`patches/vllm-mlx/0.14.3/README.md` + 本文 §10.3 原写"未应用"） |
| L-15 dup 口径 9-19 19:36:59 生效 | ✅（接受，以 AGENTS.md 为准） |

### 18.2 影响一：世代边界 —— 接受，无本仓动作

13 run 双世代混合（9 个重启前 / 4 个后）；优化为**双变世代**（S 变更 + 引擎换代）→
EXP-9 的 A0 作废、重跑范围（72 vs 108）由 swe-eval 侧认领定案。本仓已提供世代戳补件建议（18.4）。

### 18.3 影响三：S/keep 比例 —— **已定案：回 S=70000** ✅

**决策依据**：`S=50000` + `KEEP_TOKEN_BUDGET=40000` 余量仅 10K（原设计 40K 是"S=70K 的余量分配"），
折叠频率约 3×，且落在 §14.3 自认"未验证"的 regime。

**执行**（2026-09-20）：

```diff
-export PROXY_CTX_EPOCH_TRIGGER_TOKENS=50000
+export PROXY_CTX_EPOCH_TRIGGER_TOKENS=70000
```

`./manage.sh reload` 后 `/api/status` 断言：**`epoch_S=70000`、`window_K=24`** ✅
（`KEEP_TOKEN_BUDGET=40000` 保留 → 恢复 40K/70K 原余量比）
回退点：`/tmp/ornith-staging/ornith-oq4e.conf.prepilot-s70k`

### 18.4 影响二：操作点未复验 —— **进行中**（本仓认领）

0920-15 指出：0.14.3 进程段仍有 **11 次 `[prefix-pressure-evict]`**（`metal_cap=36.1GB, cache_max=11.6GB`），
同段 prompt 达 32,828 / 65,521 / 104,473 tokens；§13「驱逐归零」系**小窗**（峰值 31.0GB）实测，
EXP-9 重跑操作点（64K 长会话）恰落在未复验区。

**本仓动作**：跑**长会话操作点试点**（≥50K 视图），采集四件套——
`epoch_collapse` / `[prefix-pressure-evict]` / 引擎 `/metrics` 驱逐+snapdown 计数 / 每轮墙钟。
**不通过则先回滚再重跑**（避免 38–49h 批次二次作废）。结果见 §19（待补）。

### 18.5 提请 (c)：引擎世代戳进证据链 —— 接受

现 manifest.frozen 仅含 swe-eval `code_commit`，run 记录无引擎版本/KV/S/`wired_limit`。
**建议批首或 finale 增**：引擎 `:8081/metrics` 快照 + 版本 + 世代戳，使"跨世代标注"可机读自证。
（本仓可提供采集脚本；落盘位置由 swe-eval 侧定。）

### 18.6 未核实声明的本仓立场

- **11 次驱逐归属**：0920-15 称发生在"引擎团队自身长 prompt 测试窗（非 swe-eval 批）"——
  从时间戳看主体落在本会话 §12/§17 的 T2/基线测试期，**与 swe-eval 批无关**，本仓确认
- **post-fold token 数**：同意"折叠倍率应以试点 `epoch_collapse` 实测为准"，不采信算术推算
- **A5×4 与 POST 世代 run 是否同集**：属 swe-eval 工件，本仓无映射数据

---

## 附录：涉及文件

```
配置          configs/ornith-oq4e.conf（RAPID_MLX_EXTRA_ARGS / PROXY_HBE_* / PROXY_CTX_EPOCH_TRIGGER_TOKENS；
               Step 3 须新增 LLAMA_SERVER_BIN，未设时 manage.sh:623 回退 brew）
引擎二进制    当前 = /opt/homebrew/bin/rapid-mlx（brew 0.12.12）｜ 目标 = .venv-rapidmlx（0.14.3）
影子制品      .venv-rapidmlx-0143（并行 venv）｜ /tmp/ornith-staging/{state,logs}（§10）
配置备份      /tmp/ornith-oq4e.conf.bak-<ts>（§11）
本地补丁      patches/vllm-mlx/0.12.12/（P1a，5 文件；0.14.3 改名后不可沿用，被上游取代）
              patches/vllm-mlx/0.14.3/cachestats-counters.diff（#9(a) snapdown 计数；已随 Step 3 应用）
上游机制      rapid_mlx/hybrid_state_checkpoints.py（RAPID_MLX_HYBRID_CHECKPOINT_MAX=4 / _STRIDE=2048）
安全门        manage.sh:534-547（DEF-006，gpu-mem >0.85 拒绝）；manage.sh:623（引擎二进制解析）
后端日志      logs/llama-server.log（`snapped to checkpoint` / `LCP unavailable` / `prefix-pressure-evict`）
状态/指标     /api/status ｜ /metrics?n=30
取证完成门    ~/APP/swe-eval/results/experiments/<exp>/finale-*.md（§1）
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

---
