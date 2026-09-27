# jev 额度探测与管理（2026-09-27 实测分析与落地计划）

> 对象：`typesafe/jev`（System One）决策 API，即 `tools/jev_mcp_server.py` 的后端
> `https://api.commandcode.ai/provider/v1/systemone`。落地点：**本仓（llama.cpp）**——
> jev 仪器在此仓，swe-eval 侧只登记前置条件与指针。
> 起因：2026-09-27 swe-eval 的 ex04 批量排序被 `HTTP 429` 全量挡住，需要能**看得见额度**。
>
> **落地状态（2026-09-27）：已落地。** `tools/jev_quota.py`（`--check`/`--json`/`--plan`，
> 退出码 0/3/4/5）＋ `tools/jev_mcp_server.py`（MCP 工具 `jev_usage`、429 错误体白名单提取）
> ＋ `test/unit/test_jev_quota.py`（15 例）与既有 MCP 测试更新；全量 unit 层 1772 passed /
> 11 skipped，并已用真实 stdio 会话端到端验证（`tools/list` → `jev_usage` 真读额度）。
> 下文 §4 保留为设计记录（与实现一致）。

## 0. 结论速览（每条都有实测支撑）

1. **官方文档没有额度 API**（provider 页只有 5 个端点：chat/completions、responses、
   messages、models、systemone），响应头里也**没有任何** rate/limit/quota 字段。
   但 **CLI（`cmd`）自己用的 `/alpha/*` 端点在服务端存在且可用同一把 `CMD_API_KEY` 调用**——
   端点路径从 CLI bundle 提取（`/opt/homebrew/lib/node_modules/command-code/dist/cli.mjs`：
   `"/alpha/whoami"`、`"/alpha/billing/credits"`、`"/alpha/billing/subscriptions"`、
   `"/alpha/usage/summary"`）。⇒ **额度探测可行、可脚本化**。
2. **实测账户状态**（2026-09-27 04:51 UTC）：计划 `individual-pro-v1`（Pro：5 小时窗
   cap **16**、周窗 cap **40**，单位＝credit≈USD）；5 小时窗 `0.0034/16`、周窗 `24.82/40`
   （**62%**）、剩余月额 `55.18/80`。⇒ **额度没有耗尽**。
3. **那次 429 的成因定案：5 小时窗超额**（文档语义：超额请求会被拒，且消息点名窗口与
   重置时刻）。窗口于本地 12:50 重置后，调用立即恢复（首个成功响应 200）。
   **更正**：swe-eval 侧先前记的「并发确定触发」**不成立**（并发只是可能加速触顶）。
4. **单次 jev 调用实测 0.0000366 credit**（327 in / 24 out）——折合输入侧 ≈$0.112/M，
   约为文档名义单价（$0.04/M）的 **2.7 倍**。按实测单价：ex03 的 77 次 ≈0.0028 credits；
   513 次批量 ≈**0.019 credits**（占 5 小时窗 **0.1%**）。⇒ **jev 自身花费可忽略**。
5. **元数据 GET 不计次不计费**：连读 `credits`/`summary`/`models` 多次，`totalCount`
   （1108）与 `windowLimits.*.used` 前后**逐位不变**。⇒ 探测本身免费，可放心前置调用。
6. **真问题是共享窗口**：账户同时被 **CLI 会话**消耗——一次 `cmd -p` 调用实测 ≈**0.0033
   credits**（≈**90 次 jev 调用**）。⇒ 额度管理的主体是运营会话；jev 是**受害者**：
   窗口被会话吃满时，jev 一起 429。

## 1. 通道盘点（全部实测）

| 通道 | 可用 | 返回内容 |
|---|---|---|
| `GET /provider/v1/models` | ✅ 200 | 仅模型表（零判定、零计费） |
| 响应头（任意端点） | ❌ | 无 rate/limit/quota/reset 字段 |
| `GET /alpha/billing/credits` | ✅ 200 | **两窗 used/cap/exceeded/resetAt ＋ 剩余 credits**（关键通道） |
| `GET /alpha/billing/subscriptions` | ✅ 200 | `planId`/`status`/计费周期 |
| `GET /alpha/usage/summary` | ✅ 200 | 期累计：请求数/成本/输入输出 token/成功率 |
| `GET /alpha/whoami` | ✅ 200 | 身份（`user`） |
| CLI `/usage` | ❌ 非交互 | 客户端渲染的交互浮层；`cmd -p "/usage"` 只会得到一段说明 |
| web `commandcode.ai/usage`、`/billing` | 需登录 | 人读 |

`/alpha/*` **未文档化**（属 CLI 内部 API）⇒ 落地必须容错降级，不得作为硬依赖。

## 2. 响应字段语义（实测）

`GET /alpha/billing/credits`：

```json
{"credits": {"belowThreshold": false, "creditThreshold": 0,
             "monthlyCredits": 55.1843708057, "purchasedCredits": 0, "freeCredits": 0},
 "windowLimits": {"limited": true, "exceeded": null,
   "fiveHour": {"used": 0.003389104, "cap": 16, "exceeded": false, "resetAt": 1790502607690},
   "weekly":   {"used": 24.8156291943, "cap": 40, "exceeded": false, "resetAt": 1791041816259}},
 "sandboxAccess": false, "sandboxMinutes": null}
```

- `credits.monthlyCredits` ＝ **剩余**月额（验证：80 − 55.1844 = 24.8156 ＝ `weekly.used`，两处自洽）。
- `resetAt` ＝ epoch **毫秒**（`fiveHour` 为滚动的 5 小时窗；`weekly` 为滚动 7 天窗）。
- 文档口径：**caps 按 credit 价值计**（非请求数）；窗口由首个请求开启，到期自动重置；
  额外购买额度不受窗口限制。

`GET /alpha/usage/summary`（期累计，billing-period）：本次实测 1108 请求 / $41.16 /
输入 357.8M token / 成功率 99.0% ——**主体不是 jev**（jev 全项目累计 ≈96 次）。

## 3. 429 的语义与正确处置

1. 文档：超额请求被拒时，消息会**点名窗口并给出重置时刻**。当前 MCP server 把响应体
   丢掉（只报 `HTTP 429 <reason>`），等于把最有用的信息扔了 → **必须改**（见 §4.2）。
2. **不盲退避**：429 时先读窗口——`exceeded` 为真则**等重置**（或提示 `/extra` 买额度、
   `/upgrade` 升档），短退避重试无意义；未超额（网关瞬时抖动）才短退避重试一次。
3. 并发不是机制但会加速触顶：批量一律**串行**（同一时刻一个在飞）。

## 4. 落地计划（本仓）

### 4.1 新增 `tools/jev_quota.py`（标准库，零判定、零内容外发）

- `--check`：人读输出两窗 ％、重置倒计时、剩余额度、计划名与周期。
- `--json`：机器可读（给批前置检查用）。
- 退出码分级：`0` 充足 ／ `3` 5 小时窗 ≥80%（接近）／ `4` 任一窗 `exceeded`（超限）
  ／ `5` **不可探测**（`/alpha/*` 404/401 或网络错 → 端点疑似变更，不崩溃）。
- 凭证：沿用 `~/.zshrc` 单一源读取（同 `jev_mcp_server` 模式），**绝不回显**。
- 审计口径：**只读元数据、无内容外发**（内容类别＝无）；探测不计次不计费（§0.5 实测）。

### 4.2 改造 `tools/jev_mcp_server.py`

- **429 抓体**：白名单提取 `error.message`（截断 + 脱敏：绝不回显 key、不回显整包），
  错误串变为可行动的「窗口 + 重置时刻」。
- **新增 MCP 工具 `jev_usage`**：复用 4.1 逻辑返回窗口快照，使会话与脚本都能问
  「现在能不能调、还能调多少」。
- **串行化**：server 内置单飞锁（并发请求排队），从结构上消除并发触顶。
- **429 处置**：读窗口 → 超额则直接返回带重置时刻的错误（不重试）；未超额则退避 5s 重试一次。
- （可选）本地 ledger：把每次响应的 `usage` 追加落盘，与官方账对账（自估 vs 官方）。

### 4.3 测试与文档

- `test/unit/test_jev_quota.py`：窗口解析、百分比与倒计时、退出码分级、降级路径
  （mock HTTP：200/404/401/超时）、**不得回显 key** 断言。
- `test/unit/test_jev_mcp_server.py` 增：429 体提取与脱敏、`jev_usage` 工具契约、
  串行化（并发调用不并发发出）。
- 本文与 `docs/06-reference-metrics/jev-mcp-tool.md` 增「额度与限流」节并互相链接；
  文档须标注 `/alpha/*` 为**未文档化端点**及其降级行为。

## 5. 与 swe-eval 的接口

- **C 通道立项件的「可用性前置」具体化**为可执行判据：开批前跑 `jev_quota.py --json`，
  以实测单价对照预算（513 次 ≈ 0.019 credits ≪ 周窗余量），把判定写进批前置检查记录。
- **预算口径更正**：记账从名义单价改为**实测单价 0.0000366 credit/次**（327 in/24 out；
  随 state 长度线性变化），并以 `/alpha/usage/summary` 的官方累计做外部锚。
- **共享窗口告警**：jev 与运营 `cmd` 会话同账户 ⇒ 探测的意义在于「现在能不能跑、
  什么时候能跑」；建议把探测放进开批前置（与 swe-eval 的 EXP 启动七查并轨）。

## 6. 风险与边界

1. `/alpha/*` 未文档化 → 可能变更/关闭。故**降级即「不可探测」**，不阻断主流程；退出码 5
   与「超限」严格区分（否则会把探测失败误读成额度耗尽）。
2. 探测虽不计费，也不应被滥用为心跳（未文档化端点的调用频率无公开约束，保持克制）。
3. 本文的账户快照属**时点数据**，引用须带时刻；额度状态一律**实时读**、不得引用本件数值。
