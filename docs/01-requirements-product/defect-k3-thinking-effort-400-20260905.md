# 缺陷移交：kimi k3 通道 thinking+effort 叠加 400（L-8）

> 来源：swe-eval 2026-09-04 k3 臂前置验证失败 → 2026-09-05 复现 + 二分定位。
> 状态：待修复（阻塞 swe-eval cloud-k3 臂 / 后续云端实验）。原始请求 body 已留档 `tests/fixtures/k3-thinking-effort-400-request.json`。
> 登记：swe-eval `docs/roadmap-issues.md` L-8。

## 1. 症状

claude CLI（2.1.259）真实请求经代理 4000 转发 kimi k3 端点，**必现 400**：

```
Cloud API failed (400: {"error":{"message":"Invalid request Error","type":"invalid_request_error"}})
```

- kimi 返回的 message 是无信息量的通用文案，不指明字段。
- 代理侧 fallback：400 判 non-retryable 无冷却；应急截断不触发（10K chars < 250K）；本地兜底亦 400（该请求 135KB 超本地 413 上限），最终向客户端返回 `cloud_unavailable`。
- 简单请求（无 tools、无 thinking/effort）正常 200——9/4 前置验证"简单请求 OK、真实请求 400"的现象即此。

## 2. 根因（二分定位，2026-09-05 实测）

**最小复现**：仅两个字段叠加即触发——

```json
{
  "model": "claude-sonnet-4-6-k3",
  "max_tokens": 32000, "stream": true,
  "messages": [{"role": "user", "content": "say ok"}],
  "thinking": {"type": "adaptive"},
  "output_config": {"effort": "max"}
}
```

二分矩阵（全部经代理实测）：

| 载荷 | 结果 |
|---|---|
| 完整 claude CLI body 原样回放（129KB / 32 工具） | **400** |
| 去掉 `output_config` | 200 |
| 去掉 `thinking` | 200 |
| 去掉 `metadata` / 工具 schema `$schema` 键 / 全部 tools | 仍 400 |
| 最小载荷 thinking+output_config 双在 | **400** |
| 最小载荷仅 `thinking:{type:adaptive}` | 200 |
| 最小载荷仅 `output_config:{effort:max}` | 200 |

结论：**`thinking` 与 `output_config.effort` 各自单独可通过，叠加即被 kimi 拒**。

## 3. 机制（代码定位）

两条独立逻辑无协调地叠加进同一 OpenAI body：

1. `pipeline.py:2501-2502`：客户端 `thinking` 块**原样透传** → kimi 收到 `thinking: {"type": "adaptive"}`。
2. `pipeline.py:2541-2550`（reasoning-effort passthrough）：`output_config.effort=max` 经 `_effective_effort` → `_map_effort_to_levels`（catalog `reasoning_effort_levels: ["low","high","max"]`）→ 附加 `reasoning_effort: "max"`。

kimi k3 对 `thinking` + `reasoning_effort` 同现拒收（kimi 对二者单独均容忍，组合语义冲突：thinking 已是其推理开关，reasoning_effort 重复声明档位）。claude Code ≥2.1 的 effort beta（`anthropic-beta: ...,effort-2025-11-24`）使真实请求恒带这两个字段，故必现。

## 4. 修复建议（llama.cpp 侧决策）

按侵入度排序，任选其一：

1. **provider 级互斥**（推荐）：kimi 系模型当 `reasoning_effort` 被设置时丢弃透传的 `thinking`（或反之），在 `pipeline.py` 2550 行附近加 4-6 行归一化；quirk 化（如 `request_quirks.effort_excludes_thinking: true`）保持 catalog 驱动。
2. **thinking 归一化**：透传前把 `adaptive` 归一为 kimi 接受值（enabled），若归一化后仍与 reasoning_effort 冲突则回到方案 1。
3. 临时规避（不等修复）：swe-eval 侧 claude CLI 关掉 effort beta——但 effort 是被测真实流量特征，**实验臂不应改客户端行为**，仅作冒烟备选。

## 5. 验收

- 最小复现载荷（§2）经代理 → 200。
- claude CLI headless 真实请求（`ANTHROPIC_MODEL=claude-sonnet-4-6-k3`，129KB/32 工具形态）→ 200，且 usage 正常回填。
- 回归：deepseek 臂（force_thinking_disabled 路径）与本地臂行为不变；`pytest`（llama.cpp 侧契约/转换测试）全绿。

## 6. 证据索引

- 复现会话：proxy log `[sess=7a037b91]` 2026-09-05 21:34:41（完整 claude CLI 请求 135KB → 400 全链路日志）；二分会话 sess 前缀 `swe-k3bisect3/4/5-*`。
- 捕获的原始 Anthropic body（129KB，含 32 工具）：复现当日 `/tmp/k3-fail-body.json`（易失，需要留档请移入 llama.cpp `tests/fixtures/`）。
- swe-eval 侧配置：`config/targets.yaml` `llama-defender-cloud-k3`（代理别名 `claude-sonnet-4-6-k3`，链仅 [k3]）。
