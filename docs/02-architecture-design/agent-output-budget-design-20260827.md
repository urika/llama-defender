# 编程 Agent 场景：上下文感知输出预算设计

> **日期**: 2026-08-27
> **背景**: 2026-08-26 夜间会话 s38a6b67 在 115K tokens 上下文下出现 600s 超时重试循环（5×504 + 8×499，最近 40 请求失败率 32.5%）。本设计针对编程 Agent 场景给出本地模型的输出预算方案。
> **状态**: 待实现
> **修订**: v2（2026-08-27）——按代码与 ornith-oq4e.conf 实测对齐生命周期阈值、修正 OVERRIDE 语义、补充 continuation 短路可达性、强制本地路由约束、耗时模型与可观测性。

---

## 1. 问题回顾

### 1.1 事件证据链

```
客户端（claude-cli/2.1.238, X-Proxy-Route-To: local）
  → 115,329 tokens 上下文（chars=343K, 非流式, max_tokens=16384）
  → rapid-mlx + Ornith-1.5-35B-A3B-oQ4e（本地 :8081）
  → prefix cache HIT（cached=115314 / remaining=15）—— prefill 毫秒级，KV 缓存无问题
  → 生成阶段 decode <27 tok/s（实证上界；10 个全注意力层每步扫全上下文）
  → 16K 输出 > 600s 代理超时 → abort_request → 客户端已断开
  → CRITICAL: failed to send error（错误送不出去）
  → 客户端 SDK 静默重发同一请求（重发廉价：缓存命中使 prefill 几乎免费）
  → 条件不变 → 再次超时 → 循环
```

### 1.2 根因

| 层面 | 结论 |
|------|------|
| 前缀缓存 | ✅ 正常（115K HIT） |
| 内存 | ✅ 安全（metal 24.7GB / cap 28.1GB） |
| **输出预算** | ❌ 事故档位（saturation）允许 16384 max_tokens，而 decode <27 tok/s → 超出超时窗口 |
| **超时协调** | ⚠️ 客户端 300/600s ≠ 代理 600s，错误响应无法送达客户端 |
| 机器性能 | ✅ 未降频（短上下文 65.7-79.8 tok/s） |

**结论**：瓶颈是"长上下文下的 decode 生成速度 × 过大输出预算"超出超时窗口。前缀缓存解决了 prefill，但生成阶段的上下文线性成本不受缓存影响。

---

## 2. 编程 Agent 场景特征

| 特征 | 含义 |
|------|------|
| 轮次短促 | 思考 + 工具调用 + 短回复，典型 0.2-2K tokens/轮 |
| 长内容走工具 | 代码/文档经 Write/Edit 落盘，单次参数通常 <4K tokens |
| 上下文累积快 | 多轮 10-50K 常见，极端可达 115K |
| decode 随上下文衰减 | 短 80 → 30K ~50 → 60K ~35 → 115K <27 tok/s（实证上界） |
| prefix cache 命中率高 | 顺序工具调用高度复用前缀，增量极小 |
| **continuation 为主** | 绝大多数请求 request_count≥续传阈值 → lifecycle 走 continuation 短路分支 |

**推论**：编程 Agent 的输出预算应当"前松后紧"——正常轮次给足预算，极端上下文收紧，且收紧不损失实际产出（长内容走工具）。

---

## 3. 设计目标

1. **消灭超时重试循环**：任何请求的生成耗时 < 客户端超时（300/600s）。
2. **不损失 Agent 产出**：正常轮次（<250K chars）预算不变；长内容经 Write 工具不受限。
3. **与既有机制兼容**：复用 lifecycle 六档分类 + continuation 短路，不新增状态机。
4. **可配置、可回退、可测试**：新增参数进 `CONFIG_REGISTRY` 与 `_RELOAD_SPEC`，热重载生效。

---

## 4. 现状分析（以 ornith-oq4e.conf 实配为准）

### 4.1 生命周期六档（chars 阈值，**当前激活配置**）

```
init      <  80K   PROXY_CHARS_GROWTH     （默认 40K，ornith 重标定）
growth    <  80K
expansion < 250K   PROXY_CHARS_EXPANSION  （默认 90K）
saturation< 450K   PROXY_CHARS_SATURATION （默认 180K）
oom_danger<   2M   PROXY_CHARS_OOM_DANGER （默认 350K）
pre_trunc ≥   2M   PROXY_OOM_SAFE_CHARS / TOKENS 同步
```

> **2026-08-26 重标定依据**（configs/ornith-oq4e.conf:55-66 注释）：旧阈值按 Qwen3.8-27B（82K tokens 即 28.8GB OOM）标定，对 Ornith 过紧，曾致前缀缓存击穿（ttft 2-15s → 93-113s/轮）+ 模型失忆（一次丢 53 条消息）。新阈值以 fifo 400K chars 上限为实际约束，OOM 墙退为纯兜底（2M chars 在 400K fifo 前不可达）。**本设计不回退阈值，只重映射预算**。

### 4.2 continuation 短路（可达性关键）

`lifecycle.py:102`：`if is_continuation and total_chars > PROXY_CHARS_EXPANSION: return stage="saturation"`

- agent 连续会话只要 chars >250K，**一律短路为 saturation**，早于 oom_danger/pre_trunc 判定。
- **oom_danger / pre_trunc 对 continuation 会话不可达**，仅首轮（is_continuation=False）超大请求可触达。
- **推论**：对编程 Agent，saturation 档就是"极端档"，预算映射必须以其为准。

### 4.3 现有输出预算机制

`lifecycle.py:_compute_dynamic_max_tokens` 仅三档，saturation/oom_danger/pre_trunc 共用 `PROXY_DYNAMIC_MAX_TOKENS_SATURATION`：

```python
if   stage == "init":                 cap = PROXY_DYNAMIC_MAX_TOKENS_INIT        # 32768（ornith）
elif stage in ("growth","expansion"): cap = PROXY_DYNAMIC_MAX_TOKENS_GROWTH      # 32768（ornith）
else:  # saturation/oom_danger/pre_trunc 共用
                                      cap = PROXY_DYNAMIC_MAX_TOKENS_SATURATION # 16384（ornith）
```

### 4.4 OVERRIDE 语义（pipeline.py:610）

`PROXY_MAX_TOKENS_OVERRIDE=32768` 仅在 `body > OVERRIDE` 时**向下钳制**（最终天花板），**不会**把动态预算拉回。动态预算 ≤12288 < 32768 时不受影响。**推论**：预算方案与 OVERRIDE=32768 兼容；只要各档 cap ≤ OVERRIDE 即不被破坏。

### 4.5 其他前置事实

| 项 | 值 | 影响 |
|---|---|---|
| `PROXY_DYNAMIC_MAX_TOKENS_ENABLED` | 本地默认 `true`，云端默认 `false`（proxy_state.py:241） | **云端不生成本预算**（云端 decode 快无需收紧），本文仅约束本地 |
| `PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO` | ornith=1.0（默认 0.8） | 已无二次折扣，预算与表一致 |
| `_RELOAD_SPEC` | 已含 ENABLED/INIT/GROWTH/SATURATION/RAPID_MLX_RATIO（proxy_state.py:1001-1005） | 新参数必须同步注册 |
| 路由强制头 | `X-Proxy-Route-To: local` 优先级 0.6，**高于**阈值/内存路由（pipeline.py:716） | 事故自带该头 → 云端安全阀（80000 chars）被绕过，**预算是唯一防线** |

### 4.6 缺陷

**saturation/oom_danger/pre_trunc 共用 16384**，且 continuation 短路把 agent 极端会话全部归入 saturation（16384）→ s38a6b67（343K chars）拿 16K → 超时循环。阈值随会话增长"越极端越慢 decode"，预算却一成不变。

---

## 5. 方案设计：上下文感知输出预算

### 5.1 阈值映射表（以 ornith 实配 + continuation 短路为准）

| 生命周期档 | 上下文 chars | 约 tokens | decode 估 | 预算 | 预算耗时（上限） | 是否落窗口 |
|---|---|---|---|---|---|---|
| init/growth | <80K | <27K | ~65 | 32768（不变） | — | ✅ |
| expansion | 80-250K | 27-83K | ~35-50 | 32768（不变） | — | ✅ |
| **saturation**（含 continuation 短路） | **≥250K** | **83-150K+** | **<27** | **8192** | 304-546s | ✅ <600s |
| oom_danger | 450K-2M（仅首轮可达） | >150K | <22 | **4096** | 186-410s | ✅ |
| pre_trunc | ≥2M（仅首轮可达） | >660K | <22 | **4096** | 186-410s | ✅ |

> **saturation=8192 取值依据**：事故档 16384 在 600s 内失败（decode <27 tok/s）。8192 = ½×16384，按 decode 15-27 tok/s 估算耗时 304-546s，**任何速率均 <600s 代理超时**；对 300s 客户端在 ≥27 tok/s 时落窗，低速率时由 6.2 流式看门狗/6.3 主动 504 兜底。oom/pre_trunc 合并为 4096（continuation 不可达，仅为首轮兜底）。
> **灵敏度**：若实测事故档 decode 稳定 ≥27 tok/s，saturation 可放宽至 12288（见 §9 校准流程）。

### 5.2 设计原则与替代公式

**主原则**：`预算 ≤ T_client × f_est(chars)`，其中 `T_client` 为目标完成时间（取客户端超时，300/600s），`f_est` 为经验 decode 速率曲线。

**推荐实现（优于固定四档）**：

```python
# lifecycle.py:_compute_dynamic_max_tokens 可选替代
# 分段线性插值 f_est：80@0K / 65@30K / 50@60K / 35@90K / 22@150K+（tok/s）
# cap = min(requested, int(T_target * f_est(est_chars)))
```

- 连续平滑，避免固定档在阈值边界抖动；仍映射 lifecycle stage 取 `min`，与现有语义兼容。
- 便于后续按后端 `timings.cached_tokens` / 实际生成时长自适应标定。
- **本文落地先按 §5.1 固定四档**（改动最小、可立即验证），连续公式作为迭代方向。

### 5.3 行为语义：截断不丢产出

- 预算触顶时响应 `stop_reason = "max_tokens"`，Claude Code **下一轮自动续写**，内容不丢失，仅拆分。
- 长代码/文档经 Write/Edit 工具落盘，单次 <4K tokens，不触及上限。
- 因此收紧预算对编程 Agent 的实质产出影响可忽略。

---

## 6. 超时策略

### 6.1 后端超时

| 项 | 值 | 说明 |
|---|---|---|
| `PROXY_BACKEND_TIMEOUT` | 600（保持） | 客户端超时无法修改（300/600 均有），不做超时竞速 |
| 完成保障 | 由预算保证 | 请求生成耗时经 §5.1 控制在超时前 |

### 6.2 流式生成看门狗（补强）

- 非流式请求在客户端断连前无法感知，只能靠预算。
- **流式路径**：补后端 chunk 级 idle 看门狗（如 30s 无 chunk 即断连 + 499 记账），区分"首 token 慢"与"生成卡死"，避免等满 600s。
- 验证 streaming 路径 BrokenPipe 走 499 而非 500（anthropic_proxy.py:850 非流式已按 `_client_disconnected` 计 499，流式需确认）。

### 6.3 服务端主动 504（补强）

- 当后端生成逼近 `客户端超时 − 30s` 时，代理主动返回 504 + Retry-After，使客户端在自身超时前收到明确"可重试"信号，避免 `CRITICAL: failed to send error`（错误送不出去）与孤儿请求白烧算力。

---

## 7. 路由策略

| 决策 | 说明 |
|---|---|
| 本地为 Agent 默认 | 隐私 / 零成本 / prefix cache 命中率高 |
| 强制本地路由约束 | `X-Proxy-Route-To: local` 优先级 0.6 绕过云端安全阀，**强制本地时仍受输出预算约束**——预算在 SmartRouter 之后无条件执行 |
| 强制本地 + 超大上下文告警 | 当 `header_override==local && est_chars > PROXY_OOM_SAFE_CHARS` 时输出 WARN 日志 + `X-Proxy-Route-Warn` 头，提示该请求已越过云端兜底阈值、仅靠预算保护 |
| 个别超大单次输出 | 临时去掉 force-local 路由云端（decode 快，无需收紧） |

---

## 8. 配置变更清单

### 8.1 参数变更

| 参数 | 现值 | 目标值 | 位置 |
|---|---|---|---|
| `PROXY_DYNAMIC_MAX_TOKENS_SATURATION` | 16384 | **8192** | ornith-*.conf ×3 |
| `PROXY_DYNAMIC_MAX_TOKENS_OOM`（**新增**） | — | **4096** | proxy_state / proxy_config / _RELOAD_SPEC / __all__ / ornith-*.conf ×3 |
| `PROXY_MAX_TOKENS_OVERRIDE` | 32768 | **0**（或保持 32768，见 4.4） | ornith-*.conf ×3（置 0 彻底消除干扰） |

### 8.2 代码改动

| 位置 | 变更 |
|---|---|
| `lifecycle.py:163` `_compute_dynamic_max_tokens` | 拆分 else 分支：`saturation → SATURATION`；`oom_danger/pre_trunc → PROXY_DYNAMIC_MAX_TOKENS_OOM` |
| `proxy_state.py` | 新增 `PROXY_DYNAMIC_MAX_TOKENS_OOM`（env 读取，默认 4096）+ `_RELOAD_SPEC` 元组（~line 1004 处）+ `__all__` |
| `proxy_config.py` | `CONFIG_REGISTRY` 新增条目（`defaults: {all: "4096"}`，scope reloadable，doc：oom_danger/pre_trunc 档 max_tokens 上限） |
| `pipeline.py:579` `DynamicMaxTokens` | 无需改动（复用返回 cap）；如做 §5.2 连续公式则在此调用处替换 |
| `pipeline.py:716` SmartRouter | 可选：强制本地 + est_chars>OOM_SAFE_CHARS 时打 WARN（§7） |
| `reload_config.py` | 经 `_RELOAD_SPEC` 自动覆盖，无需直接改动 |

### 8.3 同步文件（全量）

- `configs/ornith-oq4e.conf` / `configs/ornith-35b.conf` / `configs/ornith-dflash-35b.conf`（×3 同步）
- `CLAUDE.md` / `AGENTS.md` §11.2 本地模式参数表各加一行
- 本设计文档落地后更新 changelog

---

## 9. 验证计划

### 9.1 单元

- `bash test/run_tests.sh --unit`：为 `_compute_dynamic_max_tokens` 补用例——saturation→8192、oom_danger/pre_trunc→4096、init/growth/expansion 不变、override 不反向拉高。

### 9.2 配置

- `./manage.sh reload` + `./manage.sh config-lint` 通过；确认新参数进 `/api/route/policies`（reloadable 生效）。

### 9.3 行为复测（度量矩阵，替代"构造 100K+ 一例"）

| 维度 | 取值 |
|---|---|
| 上下文 chars | 90K / 250K / 450K 三档 |
| 协议 | stream=true / stream=false |
| override | on(32768) / off(0) |

每格至少一例长会话复测，记录：实际生成耗时、decode 速率、是否 504/499、`stop_reason`、loop 误报数。**重点复现事故档（≈343K chars, non-stream, max_tokens=16384）确认预算生效不再超时**。

### 9.4 校准流程（§5.1 灵敏度）

1. 用固定输出长度（如 2K tokens）在 250K/450K/2M chars 上下文各测一次，得实测 `f_est`。
2. 若事故档 decode ≥27 tok/s，saturation 放宽至 12288；若 <20，保持 8192 或降 6144。
3. 校准结果回填 §5.1 表与 changelog。

### 9.5 可观测性（补强）

- `_compute_dynamic_max_tokens` 已回 reason（`stage=…,rapid-mlx_discount,low_memory`）→ 接入 pipeline.py:561 metrics / `X-Proxy-Diag-*` 头（diagnostics.py）/ `logs/diag/sessions.jsonl`。
- 对 `stop_reason=="max_tokens"` 计数并告警，区分"预算触顶"与"模型早停"，避免误判。

---

## 10. 风险与权衡

| 风险 | 权衡 |
|---|---|
| saturation 收紧至 8192 | 单次大内联输出拆两次（stop_reason=max_tokens 续写），实质损失可忽略 |
| oom/pre_trunc 档 continuation 不可达 | 仅首轮兜底；agent 极端会话由 saturation=8192 覆盖 |
| 事故档 decode 实测低于 15 tok/s | 8192 仍 <600s 代理超时；若需同时满足 300s 客户端，走 6.2 流式看门狗 / 6.3 主动 504 |
| 600s 后端 vs 300s 客户端竞速 | 已由 6.3 主动 504 兜底（替代单纯依赖预算） |
| 强制本地绕过云端安全阀 | 预算无条件执行 + §7 告警，双保险 |
| rapid-mlx 历史"忽略 max_tokens"缺陷 | 0.12.x 已实测尊重（事件中 abort 可见预算生效）；复测发现异动即回退配置 |
| 新增参数漏同步 _RELOAD_SPEC/__all__ | §8.2 逐项核对；config-lint 兜底 |

---

## 11. 最小可落地改动（DoD）

1. `lifecycle.py` 拆分 oom 档 → `PROXY_DYNAMIC_MAX_TOKENS_OOM`。
2. `proxy_state.py` + `proxy_config.py` 注册新参数（reloadable）+ `_RELOAD_SPEC` + `__all__`。
3. ornith-*.conf ×3：`SATURATION=8192`、新增 `OOM=4096`、`OVERRIDE=0`。
4. 单元测试补四档分支 + override 不反向用例。
5. 文档同步 CLAUDE.md / AGENTS.md / changelog。
6. §9.3 复测事故档确认预算生效、无 504/499 循环。