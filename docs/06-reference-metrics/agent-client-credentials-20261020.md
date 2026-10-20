# 本机 Agent 客户端凭证与 OpenCode 配置（2026-10-20）

> **范围**：本机（macOS / M5 Pro）上 **Agent 客户端侧**的凭证管理——
> OpenCode、Claude Code 的配置与 shell 环境变量。
> 服务端（llama-defender 代理）自身的 `LLAMA_API_KEY` 见
> [`api-and-operations-guide.md`](./api-and-operations-guide.md) 与 AGENTS.md §7.1，本文不重复。
>
> **一句话**：所有密钥只存 shell 环境变量（`~/.zshenv`），配置文件一律用 `{env:VAR}` 引用，
> 任何被 git 跟踪的文件里不得出现明文密钥。

---

## 一、凭证映射表

| 变量 | 消费者 | 定义位置 | 引用方式 |
|------|--------|----------|----------|
| `SERPER_API_KEY` | OpenCode MCP `serper` | `~/.zshenv` | `mcp.serper.environment.SERPER_API_KEY = "{env:SERPER_API_KEY}"` |
| `NVIDIA_API_KEY` | OpenCode provider `nvidia` | `~/.zshenv` | `provider.nvidia.options.apiKey = "{env:NVIDIA_API_KEY}"` |
| `HF_TOKEN` | HuggingFace 下载（模型/数据集） | `~/.zshenv` | 由工具直接读环境变量 |
| `CMD_API_KEY` | OpenCode MCP `jev`（项目级） | `~/.zshrc` | `mcp.jev.environment.CMD_API_KEY = "{env:CMD_API_KEY}"` |
| `DEEPSEEK_API_KEY` | 代理云端 provider | `~/.zshrc` | 由代理读取 |
| `ZAI_API_KEY` | 代理云端 provider（Z.ai） | `~/.zshrc` | 由代理读取 |
| `LLAMA_API_KEY` | llama-defender 本地后端鉴权 | `configs/secret.local.conf`（git-ignored） | 代理与探针读取 |

> 占位符（非真实凭证，无需处理）：`provider.olama-local.options.apiKey = "sk-1234"`、
> `provider.local-llama.options.apiKey = "local-proxy"`、
> `mcp.mcp-server-askecho-search-infinity.environment.ASK_ECHO_SEARCH_INFINITY_API_KEY = "<SEARCH_API_KEY>"`。

## 二、Shell 变量的放置约定

**优先放 `~/.zshenv`，而不是 `~/.zshrc`。**

| 文件 | 加载时机 | 适用 |
|------|----------|------|
| `~/.zshenv` | **所有** zsh 调用（交互 + 非交互 + 脚本） | ✅ 需要被工具/子进程读取的密钥 |
| `~/.zshrc` | 仅**交互式** shell | 别名、提示符、`PATH` 等交互专属设置 |

原因（实测）：`~/.zshrc` 里的变量在 `zsh -c '...'`（非交互）中**不可见**，
而 Agent 工具、`nohup` 子进程、launchd 服务大多以非交互方式启动。把密钥只放 `.zshrc`
会出现「终端里 `echo $VAR` 有值、工具里却是空」的隐蔽故障。

```bash
# 验证（应当两个都为 ok）
zsh -c 'echo "SERPER=${SERPER_API_KEY:+ok} NVIDIA=${NVIDIA_API_KEY:+ok}"'
```

> `PATH` 一类交互无关但需要继承的设置仍可留在 `.zshrc`；密钥类统一走 `.zshenv`。

## 三、OpenCode 配置：`{env:VAR}` 约定

OpenCode 支持在配置值中写 `{env:VAR}`，启动时从环境变量解析。

```jsonc
{
  "provider": {
    "nvidia": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "apiKey": "{env:NVIDIA_API_KEY}",        // ✅ 不落盘明文
        "baseURL": "https://integrate.api.nvidia.com/v1"
      }
    }
  },
  "mcp": {
    "serper": {
      "type": "local",
      "command": ["npx", "-y", "serper-search-scrape-mcp-server"],
      "environment": {
        "SERPER_API_KEY": "{env:SERPER_API_KEY}" // ✅ 不落盘明文
      }
    }
  }
}
```

### ⚠️ 前置条件：变量必须在「启动环境」里可见

`{env:VAR}` 是在 OpenCode **进程启动时**解析的：

- 从终端启动 → 继承 shell 环境，`~/.zshenv` 生效 ✅
- 从 Finder / Dock / 图形启动器启动 → **不继承** shell 环境，解析结果为空 ❌
  （症状：provider 鉴权 401、MCP 工具报 key 缺失）

若必须支持 GUI 启动，可改用 macOS `launchctl setenv VAR value`，或退回到
`configs/secret.local.conf` 这类 git-ignored 文件。

## 四、当前 OpenCode 配置清单（2026-10-20 清理后）

### Providers

| provider | 状态 | 说明 |
|----------|------|------|
| `local-llama` | ✅ 启用 | 指向本地代理 `http://localhost:4000`（Anthropic 协议） |
| `nvidia` | ⏸ disabled | `nvapi-*` key 已改为 `{env:NVIDIA_API_KEY}` |
| `olama-local` | ⏸ disabled | 本地 Ollama，`sk-1234` 占位符 |

> **已移除**：`volcengine-agent-plan` —— Agent Plan 套餐到期，整块 provider（11 个模型）
> 与 `disabled_providers` 条目一并删除。

`model`：`local-llama/claude-sonnet-4-6`（未受影响）。

### MCP Servers

| server | 状态 | 备注 |
|--------|------|------|
| `knowledge` | ✅ | 本地知识库 |
| `searxng` | ✅ | 元搜索 |
| `serper` | ✅ | key 走 `{env:SERPER_API_KEY}` |
| `wechat-search` | ✅ | 需绝对路径（见下） |
| `mcp-server-askecho-search-infinity` | ✅ | ⚠️ key 仍为占位符 `<SEARCH_API_KEY>` |
| `dataPro-search` | ⏸ disabled | 套餐停订；过期 `X-Agent-Plan-Key` header 已移除，保留 `url`/`type` 便于日后重开 |

> `wechat-search` 的 `command` 必须用**绝对路径**：
> `"/Users/<user>/APP/wechat_search/mcp_server.py"`。
> 相对路径会按 OpenCode 的 cwd（项目目录）解析，导致 `can't open file '.../mcp_server.py'`。

## 五、Claude Code（`.claude/settings.local.json`）

该文件记录 Bash 权限白名单，历史上因「批准含密钥的整条命令」而把明文 key 写进了
`permissions.allow`。2026-10-20 已全部改为变量引用：

```jsonc
// 改前（泄露）
"Bash(SERPER_API_KEY=<key> uvx serper-mcp-server)"
"Bash(curl ... -H 'X-API-KEY: <key>' ...)"
"Bash(export HF_TOKEN=\"hf_***\")"

// 改后（引用）
"Bash(SERPER_API_KEY=$SERPER_API_KEY uvx serper-mcp-server)"
"Bash(curl ... -H 'X-API-KEY: $SERPER_API_KEY' ...)"
"Bash(export HF_TOKEN=\"$HF_TOKEN\")"
```

`.claude/` 已在 `.gitignore` 中忽略（**该目录可能含本地凭证，严禁入库**）。

## 六、安全事件记录与后续

**2026-10-20 发现**：`SERPER_API_KEY` 与 `HF_TOKEN` 以明文存在于
`.claude/settings.local.json` 的权限条目中；`volcengine-agent-plan` 的 `ark-*` key
与 `nvidia` 的 `nvapi-*` key 以明文存在于 OpenCode 全局配置中。

**已处理**：

1. 全部改为环境变量引用（`$VAR` / `{env:VAR}`）
2. 密钥迁至 `~/.zshenv`
3. 移除过期的 `volcengine-agent-plan` provider 与其 key
4. 清理含明文的临时备份文件

**仍须执行**：**轮换这些密钥**。改写只能防止未来泄露，已以明文形式出现过的凭证
应视为已泄露，须在对应控制台重新签发：

- Serper → 重新生成 API Key
- HuggingFace → 吊销并重建 Access Token
- NVIDIA → 重新生成 `nvapi-*`

## 七、验收检查清单

```bash
# 1) 环境变量非交互可见
zsh -c 'echo "SERPER=${SERPER_API_KEY:+ok} NVIDIA=${NVIDIA_API_KEY:+ok} HF=${HF_TOKEN:+ok}"'

# 2) OpenCode 配置无明文密钥（按形态匹配，避免把真实前缀写进文档）
grep -nE '(nvapi-[A-Za-z0-9]{20,}|ark-[0-9a-f]{8,}|hf_[A-Za-z0-9]{20,}|[a-f0-9]{32,})' ~/.config/opencode/opencode.json \
  && echo "⚠️ 有明文" || echo "✅ 无明文"

# 3) MCP 全部连接
cd ~ && opencode mcp list

# 4) Claude Code 权限文件无明文
grep -E 'e6c83eff|hf_' ~/.claude/settings.local.json && echo "⚠️ 有明文" || echo "✅ 无明文"

# 5) 仓库内无明文密钥
cd ~/APP/llama.cpp && grep -rlE 'nvapi-|e6c83eff|hf_' \
  --include='*.json' --include='*.conf' --include='*.local*' . 2>/dev/null \
  | grep -vE 'node_modules|\.venv|logs/' 
```

## 八、相关

- [`api-and-operations-guide.md`](./api-and-operations-guide.md)——llama-defender 对外操作手册
- [`jev-mcp-tool.md`](./jev-mcp-tool.md)——jev MCP（`CMD_API_KEY`）与本地服务生命周期
- [`../02-architecture-design/opencode-anthropic-routing-decision-20260829.md`](../02-architecture-design/opencode-anthropic-routing-decision-20260829.md)——OpenCode 走 Anthropic 协议的决策
- [`../../skills/README.md`](../../skills/README.md)——共享技能目录（技能中亦禁止明文密钥）
- AGENTS.md §7.1——API Key 管理（服务端侧）
