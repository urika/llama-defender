# DeepSeek 成本追溯与修复（2026-08-31）

## 背景
deepseek 官网账单 08-31 出现 **¥64** 消费，而代理 `route_cost` 显示 ¥0、`PROXY_ROUTE_DAILY_BUDGET`（¥5）从未触发。成本统计与真实账单严重背离，追溯确认。

## 追溯结论（两条流量）

### 1. `deepseek-v4-pro`：53 次（opus 链兜底）
- 客户端：OpenAI 协议测试/探针会话，请求 `claude-opus-4-7`。
- 链路：`smart_router cloud (model_forced_fallback_cloud(opus->glm-5.3))` → **`Skip glm-5.3 — anthropic-protocol backend cannot serve an OpenAI-protocol client`** → 直接落 `deepseek-v4-pro`。
- 根因：glm 订阅（zhipu/zhipu-cn）是 **anthropic 协议**，OpenAI 协议客户端被代理判定"响应形态不匹配"而**整链跳过**（pipeline.py:2535-2544 设计行为）→ 零边际候选被废弃 → 落到付费 deepseek-v4-pro（¥9/27 每 M）。

### 2. `deepseek-v4-flash`：cli_1a0f 324 次（haiku force-local 本地失败 fallback）
- 客户端：`claude-haiku-4-5`（`behavior: force` + `prefer_local`，本应恒本地保密）。
- 链路：本地后端失败 → `PROXY_ROUTE_FALLBACK_ENABLED=true` 的 local→cloud fallback → glm 链失败 → `deepseek-v4-flash`。
- 根因：**force_local 模型的本地失败 fallback 未尊重 force_local 语义**——本应"永不云端"，却经 fallback 上云（324 次）。数据保密被破坏。

## 成本统计失效（两处）
1. `pipeline.py:3124`：`if route_target=='cloud' and not self._route_fallback` —— **fallback 到 deepseek 的流量不计成本** → route_cost ¥0 → ¥5 预算/告警全部失效。
2. deepseek 模型在目录中为字符串条目（无 price）→ 成本核算用通用 `PROXY_CLOUD_PRICE_INPUT/OUTPUT`（0.5/1.5），非真实价。

## 修复
1. **opencode 切 Anthropic 协议**（`~/.config/opencode/opencode.json`）：默认模型 `volcengine-agent-plan/deepseek-v4-flash-260425` → **`local-llama/claude-sonnet-4-6`**（`@ai-sdk/anthropic`，baseURL `http://localhost:4000`）→ 请求走 `POST /v1/messages` → `_openai_mode=False` → **glm 订阅（anthropic 协议）不再被跳过**，coding plan 优先。
2. **opus 链兜底降成本**（`configs/models.json`）：`deepseek-v4-pro` → **`deepseek-v4-flash`**（官方定价 pro=3x flash）。

## 定价确认（官方 api-docs.deepseek.com，CNY/每百万 token，2026-08-18 峰谷生效）
| 档位 | v4-flash | v4-pro |
|---|---|---|
| 输入（缓存未命中）高峰 | ¥3.00 | ¥9.00 |
| 输出 高峰 | ¥9.00 | ¥27.00 |
| 输入/输出 空闲 | 半价 | 半价 |

## 验证（修复后）
- opencode/local-llama 请求 → `POST /v1/messages`（Anthropic）✅
- 强制云端 Anthropic 请求（`X-Proxy-Route-To: cloud` + sonnet）→ `Forwarding to open.bigmodel.cn/api/anthropic/v1/messages`（zhipu-cn）→ `api.z.ai`（zhipu）——**glm 订阅被真实尝试，无 Skip** ✅
- 各链 deepseek 兜底全部为 `deepseek-v4-flash` ✅

## 待办（未修）
1. **force_local 禁止云 fallback**：haiku 本地失败应重试/报错，不得经 fallback 上云（数据保密）。
2. **deepseek 目录补价格**：flash ¥3/9、pro ¥9/27（字符串条目 → dict + price）。

## 补充（同日）：fallback 成本累计修复（已生效）

### 改动
`pipeline.py` 两处 `_accumulate_daily_cost` 调用点移除 `not self._route_fallback` 条件：
- `_do_dispatch`（约 3124 行）
- `_do_dispatch_anthropic`（约 3258 行）

效果：glm/kimi 订阅（0 价）失败后降级到付费 deepseek 的流量也如实计入成本，恢复 ¥5 预算护栏。

### 验证（代理重启后，2026-09-01 16:08）
cli_948e 强制云端 sonnet 请求：
```
Forwarding to open.bigmodel.cn (zhipu-cn, glm-5.3-flash-cn)
Cloud API failed (400), checking fallback...
Forwarding to api.z.ai (zhipu, glm-5.3-flash)   ← 链内 fallback
backend status: 200 (anthropic)
[route_cost] daily cost now ¥0.0000             ← 成本行出现（旧代码 fallback 会跳过）
```
- 链内 fallback 的成本行**出现**（旧代码 `not self._route_fallback` 会跳过）✅
- 金额 ¥0 正确（服务商 glm 订阅 0 价）
- 若降到 deepseek 将按 3/9 计入（同代码路径）

### 单价一致性复查
目录价格与官网一致：deepseek-v4-flash 3/9（缓存 0.1）、deepseek-v4-pro 9/27（缓存 0.3）——高峰未命中保守口径；glm/kimi 订阅 0 价正确。

## 补充（同日）：api_model 解耦未生效（bigmodel.cn 国内站 400）修复

### 现象
bigmodel.cn（zhipu-cn 国内订阅）每次返回 400 `[1214] modelCode 不存在` → 链内 fallback 到 Z.ai，偶尔继续降到 deepseek。

### 根因
- 目录 `glm-5.3-flash-cn` 有 `api_model: glm-5.3-flash`（正确智谱请求码）。
- 但 `_resolve_cloud_target`（pipeline.py）用 `creds.get("api_model")` 读——`get_model_credentials` 返回 **provider 凭据**（无 api_model）→ 恒 None → 发送目录 id `glm-5.3-flash-cn`。
- bigmodel.cn 不识别 `-cn` 后缀码 → 1214 400。

### 修复
`_resolve_cloud_target`：api_model 改从 **模型目录条目** 读取：
```python
_m_entry = model_registry.get_model(model_name) or {}
_api_model = (_m_entry.get("api_model") or "").strip() or model_name
```

### 验证（代理重启后 2026-09-01 16:17）
- 修复前：`Forwarding to open.bigmodel.cn → Cloud API failed (400)` → 落 Z.ai。
- 修复后：`Forwarding to open.bigmodel.cn → backend status: 200` ✅（国内订阅直接成功，无 fallback）。
- 三个 `-cn` 模型（glm-5.3-cn / glm-5.3-flash-cn / glm-5.2-cn）api_model 均正确解析。
- route_cost ¥0（订阅零边际）。

### 影响
国内站订阅可用 → 减少 fallback → 降低 deepseek 使用概率。

## 相关
- 路由策略：coding plan（glm 订阅 + kimi 会员）优先于 deepseek；deepseek 仅兜底。
- 变更文件：`~/.config/opencode/opencode.json`、`configs/models.json`（opus 链）、`pipeline.py`（成本累计 + api_model 解析）。