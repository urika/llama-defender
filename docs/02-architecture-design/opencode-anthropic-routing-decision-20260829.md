# opencode 走 Anthropic 协议接入决策（2026-08-29）

> **背景**: opencode 通过 `local-llama` provider 直连代理 `:4000`，但用的是 `@ai-sdk/openai-compatible`（OpenAI 协议 `/v1/chat/completions`）。这导致订阅零边际提供商（Z.ai/bigmodel/Kimi 的 glm-5.3-flash 等）被代理的"anthropic 协议跳过过滤"排除，长上下文会话只能落 deepseek 按量计费。
> **决策**: **方案 B**——opencode 改用 `@ai-sdk/anthropic`（Anthropic 协议 `/v1/messages`），使零边际路由可达。方案 D（协议桥）评估后暂缓（成本/风险见 §四）。

## 一、根因链

```
opencode → POST /v1/chat/completions → _openai_mode=True
  ↓
pipeline.py:2447 _usable() 过滤: not (_oai_client and protocol=="anthropic")
  → zhipu(glm-5.3/glm-5.3-flash) 与 kimi 全部被跳过
  ↓
长上下文(>106K chars, haiku 档)路由云端 → 只剩 deepseek 候选 → 按量付费
```

**本质**: 订阅零成本提供商仅开放 Anthropic 端点（OpenAI `paas/v4` 不覆盖订阅 token），而 opencode 此前只讲 OpenAI 协议 → 二者不可交汇。

## 二、方案对比

| 方案 | 做法 | 成本/风险 | 结论 |
|---|---|---|---|
| **B. opencode 切 Anthropic 协议** | `local-llama` npm `@ai-sdk/openai-compatible` → `@ai-sdk/anthropic`，baseURL `:4000/v1` → `:4000` | 零代码、零风险、改单侧配置 | ✅ **采用** |
| D. OpenAI→Anthropic 协议桥 | 代理层双向转换（含流式 SSE 映射） | 中大（1-2 会话开发）；流式工具调用/thinking 映射高风险；需持续维护双协议演进 | ⏸ 暂缓 |
| C. bigmodel OpenAI 端点 | 订阅 token 打 `paas/v4` | 不可行（订阅不覆盖 OpenAI 端点） | ✗ 弃 |

## 三、实施（opencode 配置）

`~/.config/opencode/opencode.json` → `provider.local-llama`（改动前已备份 `.bak.<ts>`）：

```json
"local-llama": {
  "name": "Local MLX Proxy (Anthropic)",
  "npm": "@ai-sdk/anthropic",
  "options": { "apiKey": "local-proxy", "baseURL": "http://localhost:4000" },
  "models": { "claude-haiku-4-5": {}, "claude-sonnet-4-6": {}, "claude-opus-4-7": {} }
}
```

- Anthropic SDK 自动拼接 `/v1/messages`；代理接受任意 `x-api-key`。
- 生效时机：**下一个 opencode 会话**（当前会话沿用已加载配置）。

## 四、预期行为（haiku 档，已实测验证）

> **2026-08-29 后续决策：haiku 改为 `behavior: force` + `prefer_local` → 恒走本地（数据保密），不再按阈值切云端。** 该决策优先级高于此前的阈值调整（1.33→2.0）。大上下文强制本地由 Ornith 承受（343K chars 实测 24.7GB < cap 28.1GB；decode 随上下文衰减，250K+ 变慢），confidentiality 优先于成本/延迟。

| 上下文 | 决策 | 路由 |
|---|---|---|
| 任意长度 | `model_forced_local(claude-haiku-4-5)` | **恒本地 Ornith**（实测 180K chars 请求 → `Forwarding to http://127.0.0.1:8081` ✅） |

- 仅 `X-Proxy-Route-To: cloud` 请求头可覆盖（客户端默认不发送，即安全）。
- sonnet/opus 仍按阈值路由云端（零边际链 glm-5.3-flash-cn → kimi → deepseek 兜底）。
- 阈值路由与协议无关，切协议只改变"云端候选可达性"。

## 五、验证

- 代理 `/v1/messages` 接受 Anthropic 格式（短 haiku 请求实测 200、路由本地 ✅）。
- `opencode models` 成功解析 `@ai-sdk/anthropic` 与 `local-llama/*` 模型（无解析错误）。
- 待新会话确认：长上下文 opencode 请求走 `glm-5.3-flash`（代理日志 `Forwarding ... api.z.ai/api/anthropic` 或 bigmodel，视配额/冷却）。

## 六、后续（方案 D 触发条件）

协议桥仅在以下情况值得投入：
1. 出现 ≥2 个 OpenAI 协议客户端且各自配置负担变真实；或
2. 需要把 OpenAI 协议客户端也纳入统一零边际路由且无法逐个改客户端。

实施时务必：`PROXY_BRIDGE_ANTHROPIC_OPENAI` 开关默认关 + 仅桥接标记 provider + 先非流式后流式 + 转换失败强降级 deepseek。

## 七、相关

- 同日：glm-5.3-flash 入库并接入路由（`configs/models.json`，国内站 `zhipu-cn` provider 实测可用）；方案 A 配额感知（1308/403 → 冷却到重置，含 Kimi `/usages` 查询）已上线。
- 路由链（2026-08-29 终态，零边际优先、deepseek 兜底）：
  - sonnet → `[glm-5.3-flash-cn, glm-5.3-flash, k3, glm-5.3, deepseek-v4-flash]`
  - opus → `[glm-5.3-cn, glm-5.3, kimi-for-coding, glm-5.3-flash, deepseek-v4-pro]`
  - **haiku → 恒本地（`behavior: force`，数据保密，永不云端）**