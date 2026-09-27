# Jev MCP 工具

`tools/jev_mcp_server.py` 将 Command Code 的 `typesafe/jev` 决策 API 暴露为本地 stdio MCP 工具 `jev_decide`。它接受 `state` 和命名的 `questions`，支持 `noul`、`choice`、`score`，返回原始概率结构，不生成自由文本结论。

## 安全设置 API key

先在当前用户的 shell 环境中设置 Command Code API key；不要把真实 key 写进仓库文件、`.mcp.json`、命令行参数或 MCP server 源码：

```bash
export CMD_API_KEY='从 Command Code Studio 创建的 API key'
```

MCP server 启动时从 `CMD_API_KEY` 读取密钥，并以 HTTPS Bearer token 调用：

```text
https://api.commandcode.ai/provider/v1/systemone
```

API key 对应的账号/计划必须有 Provider API 访问权限（GOAT 或以上的支持计划，或 Provider plan）。

## 注册到 Command Code

在安装/启动 Command Code 的同一 shell 中运行：

```bash
cmd mcp add jev -- python3 /Users/<用户名>/APP/llama.cpp/tools/jev_mcp_server.py
```

将路径替换为该文件的实际绝对路径。确认该 shell 环境已导出 `CMD_API_KEY`，然后在 Command Code 中打开 `/mcp`，检查 `jev` 已连接且工具 `jev_decide` 可用。

如果 Command Code 从 GUI 启动、未继承 shell 环境变量，请从已导出变量的 shell 启动它；不要把 key 写到被版本控制的 MCP 配置。仓库 stdio server 不读取 `configs/secret.local.conf`，以免将 llama-defender 云端凭证与 Jev API 凭证混用。

## 示例

在会话中请求：“用 Jev 判断这段状态是否紧急，并返回概率：支付连续失败三天。”工具输入等价于：

```json
{
  "state": "Payments failed for three days.",
  "questions": {
    "urgent": {
      "type": "noul",
      "instructions": "Does this need urgent attention?"
    }
  }
}
```

该决策调用需 GOAT 或以上计划；输入按 Provider API 费率计费，输出 token 免费。模型结论是概率信号，调用方应自行设定阈值并保留业务校验。

## 额度与限流（2026-09-27 落地）

jev 与运营 CLI 会话**共享同一账户的两个滚动窗口**（5 小时／周，按 credit 计）：窗口被吃满时
决策调用会被 `HTTP 429` 拒绝。官方 provider 端点没有额度接口，故本仓自建两件：

**① MCP 工具 `jev_usage`**（只读元数据：不发内容、不消耗判定、实测不计次不计费）——
返回两窗 `used`/`cap`/`resetAt`、剩余额度与计划，用于「调用前先看能不能调」。
**② 429 错误体白名单提取**——官方语义里拒绝消息会**点名窗口并给出重置时刻**；旧实现
把响应体整个丢弃，现改为只取 `error.message`（截断＋脱敏，绝不放行整包、绝不回显密钥），
错误串因此变成可行动的「窗口 + 重置时刻」。**超额时不要盲退避**：先读 `resetAt` 再等，
或 `/extra` 买额度、`/upgrade` 升档。

命令行等价物：`python3 tools/jev_quota.py [--json] [--plan]`，退出码分级
`0` 充足 ／ `3` 接近（≥80%）／ `4` 超额 ／ `5` **不可探测**——4 与 5 严格区分，
**探测失败不得被误读成额度耗尽**。

> 端点、实测台账（账户快照、单次调用 0.0000366 credit、元数据 GET 不计费）与落地细节见
> `docs/06-reference-metrics/jev-quota.md`。`/alpha/*` 属 CLI 内部端点、**未文档化**，
> 故实现按「不可探测」降级而非硬依赖。
