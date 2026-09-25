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
