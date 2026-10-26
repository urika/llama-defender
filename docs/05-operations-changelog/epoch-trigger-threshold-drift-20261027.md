# epoch 触发阈值决策依据漂移复核（S=150000，2026-10-27）

> 状态：**结论 = 不改运行参数**。仅补注文档与 conf 注释。
> 本次未改任何 `PROXY_*` 值、未 reload、未重启后端、未触碰批跑产物。

## 1. 起因

上下文健康巡检时发现 `PROXY_CTX_EPOCH_TRIGGER_TOKENS=150000` 与
`PROXY_CTX_CHARS_LIMIT=180000` 存在量级落差，一度被判定为"配置隐患"并提出
修正方案（改为 `0` 走 auto 推导）。深挖后**该判断被证伪**，本文记录订正过程
与证据，避免同样的误判再次发生。

## 2. 原决策内容（2026-10-20）

出处：`configs/splash-qwen38-27b-mlx.conf` L94-102，`configs/splash-qwen36-35b-optiq.conf`
L106-108 复刻同一决策。

```
# 2026-10-20: S 70000→150000。
# 历史依据(docs/02-architecture-design/llama-defender-context-engineering-design.md §11):
#   S=70K 由三约束推导 = min(rapid-mlx pflash 96K 悬崖, 客户端 466s 断连死线@160tok/s≈70K)。
# 现状变化: ① splash 无 pflash 96K 悬崖；② prefill 450-488 tok/s（旧 160-460）；
#   ③ 单轮 prefill 硬上限 466s → 466×0.6×470 ≈ 131K 安全线；取 150K 时最坏
#   冷 prefill ≈ 150K/470 ≈ 319s（留 32% 余量）。KV 150K ≈ 5.0G，headroom 足够。
# 目的: 任务上下文 40-67K 时不再触发 epoch → 消除 epoch_collapse（rerun gate 阈值 0）
#   与 epoch 全量重建的 30-90s 冷 prefill。
```

设计文档 §11（`llama-defender-context-engineering-design.md` L484-522）给出三硬约束：

| 约束 | 内容 | 对 S=70K 的作用 |
|---|---|---|
| 一 | prefill 实测 ~130-300 tok/s（非设计假设的 1000-2000） | 死线反推 |
| 二 | claude CLI 客户端 **~466s 断连死线**（不可配，已实测） | 466×0.6÷160 ≈ 70K |
| 三 | rapid-mlx **pflash 96K tokens 阈值**（>96K 时前缀缓存收益归零） | 直接取 96K |

> `S ≤ min(96K, ~70K) ≈ 70K`；单轮 prefill（含 epoch 重建）必须 ≤~45K tokens

原决策的三个目标：① splash 无 pflash 悬崖 → 约束三解除；② prefill 提升 →
约束二放宽到 ~131K 安全线；③ **消除 rerun gate 的 `epoch_collapse` 告警**
（`tools/rerun_gate.py` `--max-epoch-collapse` 默认 **0**，即一次折叠即失败）
与 epoch 全量重建的 30-90s 冷 prefill。

## 3. 误判订正（两处）

### 3.1 把 `PROXY_CTX_CHARS_LIMIT` 当成了硬拒绝上限

**错**：以为 180000 chars 是上下文硬上限，超出即截断，因此"S=150000 token 永远
到不了，epoch 永不触发"。

**实**：引擎接管后，`ContextTruncator`（stage 14，`pipeline.py` L3309）与
`OOMSafetyFIFO`（stage 17，`pipeline.py` L3498）均直接 `return False`：

```python
# ContextTruncator.should_run
if _ctx_engine_on():
    return False  # 引擎的 epoch 状态机接管预算控制
# OOMSafetyFIFO.should_run
if _ctx_engine_on():
    return False  # 引擎的 epoch 回退保护接管超限路径
```

`PROXY_CTX_CHARS_LIMIT` 的实际用途只有三处：auto 推导公式的输入、
`queue_manager.decide_huge_action` 的入参、启动日志打印。真正的巨大请求拦截是
`PROXY_QUEUE_HUGE_THRESHOLD_CHARS=350000`，且 `PROXY_ROUTE_THRESHOLD_CHARS=80000`
已把 >80K chars 的请求路由云端。

### 3.2 误以为 auto 公式表达的是"当前后端最优阈值"

`context_engine.effective_trigger_tokens()`：

```python
raw = getattr(_ps, "PROXY_CTX_EPOCH_TRIGGER_TOKENS", 0) or 0
if raw and raw > 0:
    return int(raw)                       # 显式值直接采用
ctx_chars = getattr(_ps, "PROXY_CTX_CHARS_LIMIT", 400000) or 400000
return min(int(ctx_chars * 0.65 // TOKEN_CHAR_RATIO), DEFAULT_EPOCH_TRIGGER_TOKENS)
```

auto 分支 `min(65% × ctx_chars/4, 70000)` 直接继承 §11 的 70K 推导与
`ctx_max=400K` 的**旧假设**，公式里**没有 prefill 能力这一输入**——它从不打算表达
"当前 splash 后端的最优阈值"。在 `ctx_chars=180000` 下推出 **29250**，比 150000
低 **5.1×**，与原决策意图**完全相反**。

## 4. 实测证据（决策方向正确，且余量更大）

采样：`GET http://127.0.0.1:8081/metrics`（splash 1.4.0 + Qwen3.6-35B-A3B-OptiQ-4bit，
2026-10-27 会话累计口径）。

| 指标 | 决策时假设（27B dense） | 本机实测（splash 1.4.0 / 35B-A3B） |
|---|---|---|
| prefill | 470 tok/s | **1101–1154 tok/s** |
| decode | 65–71 tok/s（27B） | **116–117 tok/s** |
| S=150000 冷 prefill | ≈319s | **≈130s**（vs 466s 死线 → **3.5× 余量**） |
| DFlash2 接受率 | — | 0.534–0.578 |

反向校验：按实测 1154 tok/s，466s 死线留 40% 余量 → **S 安全上限 ≈ 322,736
tokens**，150000 远在其内。

### 目标达成复核

| 目标 | 状态 | 证据 |
|---|---|---|
| 消除 epoch_collapse | ✅ | 真实 `EPOCH #` 触发 = **1 次**（整个代理日志）；10 个真实会话 `epoch_count` **全为 0** |
| 消除 epoch 重建冷 prefill | ✅ | 上述 1 次在 16:13，之后无复发 |
| rerun gate 门禁（阈值 0） | ✅ PASS | `logs/diag/sessions.jsonl` → 真实会话 `epoch_count>0` 数量 = **0** |

> **易误读点澄清**：`logs/diag/manifest/*.jsonl` 里每天有 ~1500–1900 条
> `epoch_collapse` 行（2026-10-22 至 10-27 累计 7631 行 / 40 会话），但这**不是
> 折叠事件数**——它记的是 `_collapse()` 收编的 message 行数。
> `tools/rerun_gate.py` L161-168 已明确修正过此口径（"原实现数条目数对任何长会话
> 恒真、不具判别力"），改用 `sessions.jsonl` 的 `epoch_count`（会话级折叠次数）。
> 当前 rerun gate 门禁**是干净的**，勿据 manifest 行数误判。

### 为什么"改 0"会破坏原决策

实测任务上下文分布（近 200 次请求的 `input` token）：
**p50=37,336 / p90=60,621 / max=70,795**。改 0 后阈值降至 29250 → 绝大多数会话
立刻开始触发 epoch 折叠，**正向制造**原决策要消除的 `epoch_collapse` 与 30-90s
冷 prefill 重建，且 rerun gate（阈值 0）随之失败。

## 5. 附带发现：引擎版本漂移

| 项 | 文档/注释声明 | 实机 |
|---|---|---|
| Splash 版本 | "1.3.2"（conf 头部 + `CONFIG_DESC`） | **1.4.0**（`/opt/homebrew/Cellar/splash/1.4.0/`） |
| `--idle-release` | `120m`（conf 声明） | 实际 `7200.0`（秒）= 120m ✅ 一致 |
| `--max-context` | `256K` | 实际 `--max-context=262144` ✅ 一致 |
| `--thinking-token-budget` | `512`（conf 声明） | 实际 `512` ✅ 一致 |
| `--default-reasoning-effort` | `low` | 实际 `low` ✅ 一致 |

仅版本号注释滞后，不影响任何参数。已在本轮一并订正 `CONFIG_DESC` 与 conf 头部。
（`AGENTS.md` §4.5 早已记为 "Splash 1.4.0"，是 conf 注释未跟上。）

## 6. 本轮实际改动（全部为注释/文档，零运行参数）

| 文件 | 改动 |
|---|---|
| `configs/splash-qwen36-35b-optiq.conf` | 头部新增「2026-10-27 决策依据漂移复核」段；`PROXY_CTX_EPOCH_TRIGGER_TOKENS` 上方补注实测与"不要改 0"警示；`CONFIG_DESC` 版本号 1.3.2→1.4.0 且 prefill 数值改实测口径 |
| `docs/05-operations-changelog/epoch-trigger-threshold-drift-20261027.md` | 本文（新建） |

**未改动**：`PROXY_CTX_EPOCH_TRIGGER_TOKENS`（仍 150000）、任何其他 `PROXY_*`、
后端进程、批跑产物。

## 7. 后续维护提示

换模型/换引擎后若要重估 S，判据顺序：

1. 先量 prefill：`splash_prefill_tokens_per_second`（或对应引擎指标）
2. 反推安全上限：`S_max ≈ 466s × 0.6 × prefill_tok_per_s`（留 40% 余量）
3. 再看**实际任务上下文分布**（p50/p90/max）——S 必须显著高于 p90，
   否则会重演 epoch_collapse 与 rerun gate 失败
4. 注意 `PROXY_CTX_CHARS_LIMIT` 只影响 auto 分支，**不是**硬拒绝上限
   （引擎接管后截断/OOM 两个 stage 均让位给 epoch 状态机）

## 8. 参考

- 设计文档 §11 实测校准：`docs/02-architecture-design/llama-defender-context-engineering-design.md` L484-522
- 原决策出处：`configs/splash-qwen38-27b-mlx.conf` L94-102
- rerun gate 口径修正：`tools/rerun_gate.py` L161-168
- 引擎能力调研：`docs/06-reference-metrics/splash-model-capability-research-20261020.md`
- 引擎重启协调协议：`docs/05-operations-changelog/engine-restart-coordination-20260906.md`
