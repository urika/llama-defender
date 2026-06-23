# Tool-Call 输出 Token 预算调整

**日期**: 2026-06-23  
**配置**: `configs/rapid-mlx-35b-opt.conf`  
**相关模块**: `proxy_state.py`、`lifecycle.py`、`manage.sh`

## 问题现象

OpenCode 在调用 `write` / `edit` 等长参数工具时出现大量报错：

```text
Invalid input for tool write: JSON parsing failed: Expected '}'
```

代理日志中观察到模型陷入循环：

```text
LOOP LEVEL 1: tool=write max_run=3
LOOP LEVEL 1: tool=invalid max_run=3
```

## 根因分析

1. OpenCode 请求自带 `max_tokens=32000`，但代理的 `DynamicMaxTokens` 阶段会按 lifecycle 阶段和内存压力动态压低上限。
2. 默认 `init` 阶段上限仅 **4096**，rapid-mlx 后端再乘以折扣 **0.8**，内存紧张时继续乘以 **0.7**，最终生效约 **2867 tokens**。
3. `write` / `edit` 工具需要一次性输出完整 JSON 参数，其中 `content` 字段可能包含数百行代码。
4. 2867 tokens 不足以装下较长文件内容，导致模型生成的 JSON 参数被截断，OpenCode 解析失败；失败 → 重试 → 截断 → 循环。

日志示例：

```text
max_tokens dynamic: 32000 -> 2867 (stage=init,low_memory)
```

## 调整方案

在 `configs/rapid-mlx-35b-opt.conf` 中调整动态 token 上限：

```bash
# 动态 max_tokens 上限：OpenCode 的 write/edit 等工具调用可能生成较长的 JSON
# 参数/文件内容，init/growth 阶段提高到 32K，避免模型因输出 token 不足而截断 JSON。
export PROXY_DYNAMIC_MAX_TOKENS_INIT=32768
export PROXY_DYNAMIC_MAX_TOKENS_GROWTH=32768
export PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO=1.0
```

同时在 `manage.sh` 的 `_start_proxy()` 中显式传递这些变量，确保代理进程启动时拿到正确值：

```bash
PROXY_DYNAMIC_MAX_TOKENS_ENABLED="${PROXY_DYNAMIC_MAX_TOKENS_ENABLED:-true}" \
PROXY_DYNAMIC_MAX_TOKENS_INIT="${PROXY_DYNAMIC_MAX_TOKENS_INIT:-4096}" \
PROXY_DYNAMIC_MAX_TOKENS_GROWTH="${PROXY_DYNAMIC_MAX_TOKENS_GROWTH:-4096}" \
PROXY_DYNAMIC_MAX_TOKENS_SATURATION="${PROXY_DYNAMIC_MAX_TOKENS_SATURATION:-2048}" \
PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO="${PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO:-0.8}" \
```

## 参数说明

| 参数 | 含义 | 调整前 | 调整后 | 备注 |
|------|------|--------|--------|------|
| `PROXY_DYNAMIC_MAX_TOKENS_INIT` | init 阶段输出上限 | 4096 | 32768 | 匹配 OpenCode 请求的 32K |
| `PROXY_DYNAMIC_MAX_TOKENS_GROWTH` | growth 阶段输出上限 | 4096 | 32768 | 同上 |
| `PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO` | rapid-mlx 折扣 | 0.8 | 1.0 | 不再额外打折 |

由于 `DynamicMaxTokens` 最终取 `min(max_tokens_orig, cap)`，当 OpenCode 请求 `max_tokens=32000` 时，实际生效为 **32000 tokens**。

## 16K 是否足够？

**不够。** 实测中：

- 简单脚本（几十行）约 1K–3K tokens。
- 中等复杂度脚本（如带 UI 的贪吃蛇）约 5K–10K tokens。
- 大型重构/多文件 P0/P1/P2 改动脚本可能超过 15K tokens。

因此选择 **32K**，与客户端请求保持一致，留出充足余量。

## 验证

调整后重启代理，日志显示：

```text
max_tokens dynamic: 32000 -> 32768 (stage=init)
```

`write` 工具测试返回完整有效 JSON：

```json
{"filePath": "/tmp/test_dynamic3.py", "content": "print(\"hello\")\n"}
```

## 监控建议

1. 关注日志中 `max_tokens dynamic` 是否被意外压低。
2. 关注 `LOOP LEVEL 1: tool=write max_run=N` 和 `tool=invalid` 是否复发。
3. 对超长代码生成任务，建议用户直接切到云端模型：
   ```bash
   /model claude-opus-4-7
   ```

## 相关改动

- `configs/rapid-mlx-35b-opt.conf`
- `manage.sh`
- `proxy_state.py`（修复 `_parse_conf_env` 不识 `export` 前缀，避免热重载后配置丢失）
