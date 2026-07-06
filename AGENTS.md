<!-- AGENTS.md — Local LLM Inference Stack -->

> 本文件面向 AI 编程助手。首次接触本仓库时，请先阅读本节以建立全局认知；细节请参阅 [`CLAUDE.md`](CLAUDE.md) 与 [`docs/README.md`](docs/README.md)。

---

## 1. 项目概述

**这不是 llama.cpp 的 C++ 源码仓库。** 它是一个运行在 Python 与 Bash 之上的本地 LLM 推理编排层，核心职责是把下游的 `llama-server` 或 `rapid-mlx` 包装成一个 Anthropic 兼容的 API，供 Claude Code 等客户端使用。

运行模式：

```
Local:  Client (Anthropic SDK) → anthropic_proxy.py:4000 → llama-server | rapid-mlx :8081 → GGUF/MLX 模型
Cloud:  Client (Anthropic SDK) → anthropic_proxy.py:4000 → DeepSeek / OpenAI API → cloud 模型
```

**核心原则**：Claude Code 永远只连接 `http://127.0.0.1:4000`。后端切换、模型切换、本地/云端切换全部在代理层完成，**不要修改 Claude Code 自身的配置**（不要改 `~/.claude/settings.local.json`，不要设 `ANTHROPIC_BASE_URL` 等环境变量）。

目标硬件环境：Apple Silicon（M 系列，48 GB 统一内存），主要运行 Qwen 家族模型，也支持 Gemma 4 等 MLX 模型。

---

## 2. 技术栈与构建方式

### 2.1 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 服务管理 | Bash (`manage.sh`) | 启动/停止/热重载/配置切换 |
| 代理核心 | Python 3.9.6 | 代理主程序、管线、工具解析、状态管理 |
| 代理依赖 | Python 标准库 **only** | `anthropic_proxy.py`、`proxy_state.py`、`proxy_config.py`、`pipeline.py` 等均不依赖第三方包 |
| 回归测试 | Node.js / npm (`promptfoo`) | `package.json` 仅用于安装 `promptfoo` 与 `@libsql/darwin-arm64` |
| 外部后端 | `llama-server`、`rapid-mlx`、`vllm-mlx` | 本地模型服务二进制，不在仓库内 |
| 监控/分析 | 额外 Python 脚本 | `tools/` 下的 benchmark 与分析工具（部分依赖 `numpy`、`requests` 等，见各脚本头部） |

### 2.2 构建与打包

本仓库**没有** `pyproject.toml`、`setup.py`、`setup.cfg`、`requirements.txt`、`Makefile`、`Dockerfile` 或 `docker-compose.yml`（`langfuse/docker-compose.yml` 仅用于可选的 Langfuse 监控）。

- 直接运行：`python3 anthropic_proxy.py`
- 服务管理：`./manage.sh start`
- Python 依赖：代理核心零依赖；`tools/` 与测试脚本各自按需 import，不强制统一安装。
- Node 依赖：进入仓库根目录执行 `npm install` 即可安装 `promptfoo`。

---

## 3. 代码组织与模块划分

仓库根目录下一共有约 15 个核心 Python 文件和 1 个 Bash 脚本。所有业务逻辑均围绕 `anthropic_proxy.py` 的请求管线展开。

### 3.1 核心文件

| 文件 | 用途 |
|------|------|
| [`manage.sh`](manage.sh) | 服务管理器：start/stop/restart/reload/switch/status/watchdog，以及本地/云端路由强制切换 |
| [`anthropic_proxy.py`](anthropic_proxy.py) | HTTP 代理入口：`ThreadingHTTPServer` + `Handler`；8 层请求管线；工具调用 fallback；流式/非流式响应处理 |
| [`pipeline.py`](pipeline.py) | 管线抽象：22 个可独立测试的 `PipelineStage`，把 `_handle_messages` 拆成 RequestParser、LifecycleClassifier、SmartRouter、ContentCompressor、ContextTruncator、BackendDispatcher 等阶段 |
| [`proxy_state.py`](proxy_state.py) | 单一真相源：所有 `PROXY_*` / `LLAMA_*` 常量、共享可变状态、线程本地上下文、`_RELOAD_SPEC`、模型别名、路由状态 |
| [`proxy_config.py`](proxy_config.py) | `CONFIG_REGISTRY`：每个环境变量的默认值（区分 local/cloud）、类型、作用域、文档说明 |
| [`backend_strategy.py`](backend_strategy.py) | `BackendStrategy` / `LocalStrategy` / `CloudStrategy`：把 38+ 处 `if IS_CLOUD` 收敛为策略类 |
| [`reload_config.py`](reload_config.py) | SIGHUP 热重载实现：重新解析 `configs/active.conf` + `configs/secret.local.conf`，更新 `proxy_state` 与主模块 |
| [`message_converter.py`](message_converter.py) | Anthropic ↔ OpenAI 消息与工具格式双向转换，含 token 估算 |
| [`tool_parser.py`](tool_parser.py) | XML ↔ JSON 工具参数解析、`<tools>` 内容块 fallback、流式工具提取器 |
| [`content_compressor.py`](content_compressor.py) | TokenSieve 语义压缩：JSON / 代码 / 日志 / 文本的分层压缩 |
| [`truncation.py`](truncation.py) | 上下文截断：char / rounds / fifo / smart 策略、关键词索引、摘要缓存 |
| [`lifecycle.py`](lifecycle.py) | 生命周期阶段分类（init/growth/expansion/saturation/oom_danger/pre_trunc）与动态 token 预算 |
| [`loop_detection.py`](loop_detection.py) | 工具循环、文本输出循环、阻塞模式检测与干预 |
| [`tool_filter.py`](tool_filter.py) | 动态工具定义过滤，降低长工具列表的 token 开销 |
| [`admin_server.py`](admin_server.py) | `/status` 状态页、系统内存/进程信息、指标聚合、请求快照清理、并发统计 |
| [`proxy_logging.py`](proxy_logging.py) | 结构化 JSONL 日志、敏感头脱敏 |

### 3.2 数据流

```
Client POST /v1/messages
  → anthropic_proxy.py:Handler.do_POST()
  → _llama_lock 获取并发许可
  → Handler._handle_messages()
  → InstrumentedPipeline 依次执行 22 个 stage
       1. RequestParser
       2. LifecycleClassifier
       3. DynamicMaxTokens
       4. SmartRouter (local/cloud 路由)
       5. RouteNotification
       6. ErrorTranslator
       7. BlockerDetector
       8. SystemNormalizer
       9. CacheAligner
      10. ContentCompressor
      11. ToolLoopDetector
      12. TextLoopDetector
      13. SessionLoopState
      14. LoopIntervention
      15. RereadDetector
      16. DateNormalizer
      17. ContextTruncator
      18. HighDropRatioNotice
      19. MessageHashDebug
      20. OOMSafetyFIFO
      21. PrefixRatioComputer
      22. ToolPairingRepair / FormatConverter / BackendDispatcher
  → 转发到后端（local 后端走 OpenAI chat completions 格式；cloud 直接透传）
  → 流式或非流式 Anthropic 格式响应返回 Client
```

### 3.3 配置文件

配置放在 `configs/*.conf`，是 Bash 可 source 的 `KEY="value"` 文件。

| 文件 | 说明 |
|------|------|
| `configs/active.conf` | 指向当前激活配置的符号链接 |
| `configs/gemma4-26b.conf` | rapid-mlx + Gemma-4-26B，并发 2，数据处理优化 |
| `configs/rapid-mlx-35b-opt.conf` | rapid-mlx + Qwen3.6-35B-A3B 4bit |
| `configs/qwen3-8b.conf` | vllm-mlx + Qwen3-8B（需 `HF_HUB_OFFLINE=1`） |
| `configs/deepseek-chat.conf` | 云端 DeepSeek OpenAI 兼容端点 |
| `configs/secret.local.conf` | **git-ignored**，存放真实 API Key |
| `configs/archived/` | 归档的历史配置 |

配置文件中必须包含元数据：`CONFIG_NAME`、`CONFIG_DESC`、`CONFIG_MEMORY`，供 `./manage.sh list` 读取。

### 3.4 工具与文档

- `tools/`：benchmark（`bench_*.py`）、分析（`analyze_*.py`）、监控（`monitor.py`、`sysmon.sh`）、需求追踪（`trace_requirements.py`）、模块提取（`extract_module.py`）等。
- `docs/`：按 6 类组织（需求产品、架构设计、实验测试、分析诊断、运维变更、参考指标）。入口 [`docs/README.md`](docs/README.md)。
- `test/`：自动化测试，见下节。
- `logs/`：运行时日志、测试日志、metrics、快照（git-ignored）。
- `assets/chat-templates/`：Qwen chat template 修复模板。

---

## 4. 运行命令

### 4.1 日常服务命令

```bash
./manage.sh start              # 按 active.conf 启动本地后端 + 代理
./manage.sh start-cloud        # 仅启动代理，转发到云端 API
./manage.sh stop               # 优雅停止后端和代理
./manage.sh restart            # stop + start
./manage.sh reload             # SIGHUP 热重载代理配置（不重启 proxy 进程，约 0.5s）
./manage.sh status             # PID、内存、API 健康、当前模型
./manage.sh logs [N]           # 查看后端日志（默认 50 行）
./manage.sh proxy-logs [N]     # 查看代理日志（默认 50 行）
./manage.sh list               # 列出所有可用配置
./manage.sh switch <name>      # 切换 active.conf 软链
./manage.sh current            # 显示当前配置详情
./manage.sh watchdog [--daemon] # 后端健康监控，性能衰减时自动重启
./manage.sh route-force-local <session_id>   # 强制会话走本地
./manage.sh route-force-cloud <session_id>   # 强制会话走云端
./manage.sh fix-template <dir> # 修复 Qwen chat_template
```

### 4.2 本地/云端热切换示例

```bash
# 本地 → 云端（不重启代理）
./manage.sh switch deepseek-chat && ./manage.sh reload
./manage.sh stop-backend          # 释放本地模型内存

# 云端 → 本地
./manage.sh switch gemma4-26b && ./manage.sh reload
./manage.sh start-backend
```

### 4.3 直接运行代理

```bash
python3 anthropic_proxy.py                              # 监听 127.0.0.1:4000
LLAMA_BASE_URL=http://127.0.0.1:8081/v1 PORT=4000 python3 anthropic_proxy.py
```

代理支持的端点：`GET /v1/models`、`POST /v1/messages`（流式 + 非流式）、`OPTIONS`、`GET /status` 等（部分在 `admin_server.py` 实现）。

### 4.4 后端类型自动检测

`BACKEND_TYPE` 从 `LLAMA_BASE_URL` 自动推断：

- URL 包含 `deepseek`、`openai`、`api.` → `cloud`
- 否则 → `local`

`MODEL_NAME` 也会自动设置（local 默认 `mlx-community/Qwen3.6-35B-A3B-4bit`，cloud 默认 `deepseek-v4-pro`）。

---

## 5. 测试策略

测试使用统一的 Bash 入口 `test/run_tests.sh`。

### 5.1 测试层级

| 层级 | 命令 | 依赖 | 说明 |
|------|------|------|------|
| 单元 | `bash test/run_tests.sh --unit` | 无 | `test/unit/test_*.py`，纯函数逻辑，约 826 个用例，<1s |
| 集成 | `bash test/run_tests.sh --integration` | 启动 mock backend | `test/integration/*.sh` + `mock_backend.py`，约 60s |
| Promptfoo | `bash test/run_tests.sh --promptfoo` | 运行中的代理 | 固定 prompt 回归测试（9 个用例） |
| E2E | `bash test/run_tests.sh --e2e` | 运行中的代理 + 后端 | `test/e2e/*` |
| 签名 | `bash test/run_tests.sh --signature` | 无 | 校验函数签名快照是否漂移（`tools/gen_func_signatures.py`） |
| 行为快照 | `bash test/run_tests.sh --snapshot` | 无 | 校验行为快照是否漂移（`tools/gen_behavior_snapshots.py`） |
| 需求追踪 | `bash test/run_tests.sh --trace` | 无 | 解析 `docs/requirements.yaml`，检查实现与测试锚点 |
| 全部 | `bash test/run_tests.sh --all` | 以上全部 | 按顺序执行 unit → integration → promptfoo → e2e → signature → snapshot → trace |

快捷方式：`--fast` 是 `--unit` 的别名。

环境变量：

- `PROXY_BASE`：覆盖代理地址（默认 `http://127.0.0.1:4000`）
- `BACKEND_URL`：覆盖后端地址（默认 `http://127.0.0.1:8081`）
- `SKIP_E2E=1` / `SKIP_PROMPTFOO=1`：使用 `--all` 时跳过对应层级

### 5.2 预提交钩子

`.githooks/pre-commit` 默认运行：

1. `--unit`
2. `--signature`
3. `--snapshot`
4. 如果代理正在运行，额外运行 `--promptfoo` 快速模式（只跑 5 个核心用例）

安装：

```bash
git config core.hooksPath .githooks
```

跳过：

```bash
SKIP_TESTS=1 git commit -m "..."    # 跳过测试门
# 或
git commit --no-verify               # 绕过所有钩子
```

### 5.3 何时运行什么测试

- 修改 `anthropic_proxy.py`：必须跑 `bash test/run_tests.sh --all`（特别容易破坏流式/非流式工具调用、阻塞检测、云端模式）。
- 修改 `manage.sh`：必须测试 `start`、`stop`、`restart`、`reload`、`switch <name> && reload`、本地/云端两种模式。
- 修改 `proxy_state.py` / `proxy_config.py`：跑 `--unit` + `--trace`。
- 修改任何模块的函数签名或行为契约：跑 `--signature` + `--snapshot`。
- 日常提交：预提交钩子跑 `--unit` 即可。

---

## 6. 代码风格与开发约定

### 6.1 `manage.sh`

- 开头必须 `set -euo pipefail`。
- 私有辅助函数前缀 `_`，对外命令前缀 `cmd_`。
- 面向用户的字符串与注释使用 **中文**。
- 颜色输出函数：`info()`、`warn()`、`error()`。

### 6.2 Python 代理核心

- **标准库 only**：`anthropic_proxy.py`、`proxy_state.py`、`proxy_config.py` 不得引入第三方包。
- `proxy_state.py` 是单一真相源：所有 `PROXY_*` 配置常量、共享状态、`__all__`、热重载规范都在这里定义。
- `proxy_config.py` 的 `CONFIG_REGISTRY` 是配置元数据的权威来源，CLAUDE.md / AGENTS.md / docs 应引用它而不是重复写死默认值。
- `anthropic_proxy.py` 顶部使用 `from proxy_state import *` 导入所有常量。
- 辅助函数多为模块级函数；只有一个 `Handler` 类处理 HTTP。
- 日志同时输出到 stdout 和 `/tmp/anthropic_proxy.log`。

### 6.3 配置文件

- Bash-sourcable 语法：`KEY="value"`。
- 注释与章节标题使用 **中文**。
- 自包含，不 include 其他文件。
- 必须包含 `CONFIG_NAME`、`CONFIG_DESC`、`CONFIG_MEMORY`。
- API Key 只放在 `configs/secret.local.conf`（已被 `.gitignore` 排除）。

### 6.4 文档

- `docs/` 下 6 类目录结构固定；新增文档按命名规范放入对应目录（见 `docs/README.md`）。
- 日期后缀使用 `YYYYMMDD`。
- 修改架构约定后，必须同步更新 `CLAUDE.md` 与本文件。

### 6.5 无统一 linter/format 配置

仓库没有 `pyproject.toml`、`.ruff.toml`、`.flake8` 或 `pytest.ini`。提交前以测试通过为准，不强制代码格式化工具。

---

## 7. 安全注意事项

### 7.1 API Key 管理

- 云端模式需要真实的 `LLAMA_API_KEY`。
- 必须存放在 `configs/secret.local.conf`，该文件已被 `.gitignore` 排除。
- 永远不要把 key 写入任何被 git 跟踪的文件（包括配置示例、测试文件、日志）。

### 7.2 日志脱敏

- `proxy_logging.py` 会对 `Authorization`、`X-Api-Key` 等敏感请求头进行掩码处理。
- 云 API 错误日志 `logs/cloud_errors_YYYYMMDD.jsonl` 在写入前会清理请求体中的 `api_key` 字段。

### 7.3 路由敏感内容

- `PROXY_ROUTE_SENSITIVE_PATTERNS` 用于智能路由：匹配到的请求会被强制留在本地后端。
- 配置值按字面量子串处理（已做 `re.escape`），避免正则注入。

### 7.4 请求体大小限制

- `PROXY_MAX_REQUEST_BYTES` 默认 500 KB，超大请求在管线前直接返回 `413 Payload Too Large`。

### 7.5 快照文件

- 请求失败时会写入 `logs/snapshots/<request_id>_{before,after}.json`，可能包含原始请求体；这些文件仅用于本地调试，不会被 git 跟踪（`logs/` 已忽略）。

---

## 8. 关键风险与重要警告

以下事项来自实测与运维记录，修改相关代码前务必回顾，避免复现 P0 问题。

### 8.1 本地后端并发

- **48 GB Mac 上 `PROXY_MAX_CONCURRENT=1`** 是多个本地配置（rapid-mlx / llama-server）的默认且推荐值。
- 两个大上下文请求并行极易触发 OOM，症状为 `[METAL] Command buffer execution failed: Insufficient Memory`。
- Rapid-MLX 的 `--gpu-memory-utilization` 是软限制，实际使用可能超出 20–40%。建议 ≤ 0.80。

### 8.2 vllm-mlx 启动

- `vllm-mlx` v0.6.71 若无法连接 HuggingFace 会在启动时挂起。
- 配置中必须加 `export HF_HUB_OFFLINE=1`，见 `configs/qwen3-8b.conf`。

### 8.3 KV-cache / prefix-cache turboquant

- Rapid-MLX 如果需要跨重启保留 prefix cache，**不要**使用 `--kv-cache-turboquant`。
- 见 `configs/rapid-mlx-35b-opt.conf` 说明。

### 8.4 工具结果清除死亡循环

- 本地后端建议 `PROXY_CLEAR_ENABLED=false`。
- Rapid-MLX 对未变化的文件重读会返回 `Wasted call`，与清除叠加后容易导致模型反复读取文件的死亡循环。
- 上下文增长由截断（truncation）和压缩（compression）控制，而不是靠清除。

### 8.5 云端模式真实计费

- Cloud 模式会转发真实 `LLAMA_API_KEY` 到 DeepSeek / OpenAI。
- DeepSeek `deepseek-v4-pro` 约 ¥2–8 / 百万 token；典型 agentic coding 任务（56K token × 20 请求）约 ¥1–3。
- 可通过 `/status` 与 `REQ_SUMMARY` 日志行监控云成本。

### 8.6 Metal 死锁/内核恐慌

- 反复 `kill -9` 后端可能让 Metal 陷入挂起状态，需要重启系统才能恢复。
- 优先使用 `./manage.sh stop-backend` 进行优雅停止。
- 保持 `--gpu-memory-utilization` ≤ 0.80 可降低内核恐慌风险。

### 8.7 Rapid-MLX 忽略 max_tokens

- Rapid-MLX v0.6.30 接受 `max_tokens` 但会忽略它，生成长度可能远超限制。
- 如需严格控制输出长度，使用 `llama-server` 后端。

### 8.8 聊天模板兼容性

- Claude Code 的 `mid-conversation-system` beta 会在对话中间插入 `system` 消息，Qwen 官方 chat template 要求所有 `system` 消息必须在最开头，否则会触发 `TemplateError: System message must be at the beginning`。
- 修复方式：替换模型目录中的 `chat_template.jinja`（rapid-mlx）或使用 `--chat-template` 参数（llama-server）。详情见 `TROUBLESHOOTING.md`。

---

## 9. 修改检查清单

在提交前，根据改动范围执行对应检查：

- [ ] **如果修改 `manage.sh`**：手动测试 `start`、`stop`、`restart`、`reload`、`start-backend`、`stop-backend`、`status`、`switch <name> && reload` 在本地与云端模式下是否都正常。
- [ ] **如果修改 `anthropic_proxy.py`**：运行 `bash test/run_tests.sh --all`；重点检查流式/非流式工具调用、阻塞检测、云端模式。
- [ ] **如果新增配置变量**：在 `manage.sh` 加默认值，在 `proxy_config.py` 的 `CONFIG_REGISTRY` 注册，并同步更新 `CLAUDE.md` 与本文件。
- [ ] **如果新增后端/云服务商**：更新 `anthropic_proxy.py` 中的 `BACKEND_TYPE` 自动检测逻辑与 URL 模式文档。
- [ ] **如果修改截断、循环检测、阻塞逻辑**：对照 `docs/DEFECT-LIST.md` 检查是否重新引入已知 P0 问题。
- [ ] **如果修改架构约定**：同步更新 `CLAUDE.md` 与 `AGENTS.md`。
- [ ] **所有提交**：确保 `.githooks/pre-commit` 的 `--unit` 测试通过。

---

## 10. 参考入口

| 主题 | 文档 |
|------|------|
| 完整代理管线 | [`docs/02-architecture-design/proxy-pipeline-reference.md`](docs/02-architecture-design/proxy-pipeline-reference.md) |
| 上下文压缩策略 | [`docs/research-context-optimization/06-context-compression-strategy.md`](docs/research-context-optimization/06-context-compression-strategy.md) |
| 上下文窗口/截断设计 | [`docs/02-architecture-design/proxy-context-window-design.md`](docs/02-architecture-design/proxy-context-window-design.md) |
| 智能模型路由 | [`docs/02-architecture-design/intelligent-model-routing-design.md`](docs/02-architecture-design/intelligent-model-routing-design.md) |
| 已知缺陷列表 | [`docs/DEFECT-LIST.md`](docs/DEFECT-LIST.md) |
| 故障记录与 workaround | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) |
| 性能基线 | [`BENCHMARK.md`](BENCHMARK.md) |
| 测试布局 | [`test/README.md`](test/README.md) |
| 需求追踪 | [`docs/requirements.yaml`](docs/requirements.yaml) + [`tools/trace_requirements.py`](tools/trace_requirements.py) |
| Claude Code 专用指南 | [`CLAUDE.md`](CLAUDE.md) |

---

> 本文件上一版本保存在 `AGENTS.md.bak`。
