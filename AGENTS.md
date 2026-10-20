<!-- AGENTS.md — Local LLM Inference Stack -->

> 本文件面向 AI 编程助手。首次接触本仓库时，请先阅读本节以建立全局认知；细节请参阅 [`CLAUDE.md`](CLAUDE.md) 与 [`docs/README.md`](docs/README.md)。

---

## 1. 项目概述

**这不是 llama.cpp 的 C++ 源码仓库。** 它是一个运行在 Python 与 Bash 之上的本地 LLM 推理编排层，核心职责是把下游的 `llama-server` 或 `rapid-mlx` 包装成一个 Anthropic 兼容的 API，供 Claude Code 等客户端使用。消费方 agent_go 项目把本服务称为 **llama-defender**（集成契约见 [`docs/01-requirements-product/llama-defender-integration-requirements.md`](docs/01-requirements-product/llama-defender-integration-requirements.md)：R1-R12 已全部交付）。

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
| 外部后端 | `llama-server`、`rapid-mlx`、`vllm-mlx`、`dflash-mlx` | 本地模型服务二进制，不在仓库内；`dflash-mlx`（`.venv-dflash`）提供 DFlash 投机解码，35B 实测 ~117 tok/s |
| 监控/分析 | 额外 Python 脚本 | `tools/` 下的 benchmark 与分析工具（部分依赖 `numpy`、`requests` 等，见各脚本头部） |

### 2.2 构建与打包

本仓库**没有** `pyproject.toml`、`setup.py`、`setup.cfg`、`requirements.txt`、`Makefile`、`Dockerfile` 或 `docker-compose.yml`（`langfuse/docker-compose.yml` 仅用于可选的 Langfuse 监控）。

- 直接运行：`python3 anthropic_proxy.py`
- 服务管理：`./manage.sh start`
- Python 依赖：代理核心零依赖；`tools/` 与测试脚本各自按需 import，不强制统一安装。
- Node 依赖：进入仓库根目录执行 `npm install` 即可安装 `promptfoo`。

---

## 3. 代码组织与模块划分

仓库根目录下的核心 Python 模块约 46 个 + `manage.sh`。所有业务逻辑均围绕 `anthropic_proxy.py` → `pipeline.py` 的请求管线展开。

> **明细已拆分** → [`docs/02-architecture-design/codebase-reference-20261017.md`](docs/02-architecture-design/codebase-reference-20261017.md)
> （核心文件清单 / 管线 25 stage 全序 / 旁路系统详述 / 配置文件明细 / 工具清单 / Admin API 端点表）。

**入口三件套**（改代码前必知）：
- [`anthropic_proxy.py`](anthropic_proxy.py)：HTTP 入口（ThreadingHTTPServer + Handler，双协议端点）
- [`pipeline.py`](pipeline.py)：25 个可独立测试的 `PipelineStage`，请求处理全部在此
- [`proxy_state.py`](proxy_state.py)：单一真相源——所有 `PROXY_*` 常量、共享状态、热重载规范

**新增管线阶段**：继承 `PipelineStage` 保持薄封装（deferred import 规避循环依赖），并在 `test/unit/test_pipeline_stages.py` 补测试。

## 4. 运行命令

### 4.1 日常服务命令

```bash
./manage.sh start              # 按 active.conf 启动本地后端 + 代理
./manage.sh start --profile aggressive  # 压缩策略：balanced（默认）/ aggressive / conservative
./manage.sh start-cloud        # 仅启动代理，转发到云端 API
./manage.sh start-backend / stop-backend # 单独启停本地后端（优先用 stop-backend 优雅停止，避免 Metal 死锁）
./manage.sh start-aux <name> / stop-aux <name> # 独立辅助引擎启停（configs/<name>.conf，独立端口/进程，与主后端共存；如 start-aux minicpm5-2b）
./manage.sh stop               # 优雅停止后端和代理
./manage.sh restart            # stop + start
./manage.sh reload             # SIGHUP 热重载代理配置（不重启 proxy 进程，约 0.5s）
./manage.sh status             # PID、内存、API 健康、当前模型
./manage.sh logs [N]           # 查看后端日志（默认 50 行）
./manage.sh proxy-logs [N]     # 查看代理日志（默认 50 行）
./manage.sh list               # 列出所有可用配置
./manage.sh switch <name>      # 切换 active.conf 软链（非交互）
./manage.sh current            # 显示当前配置详情
./manage.sh wizard             # 交互式快速启动向导
./manage.sh watchdog [--daemon] # 后端健康监控，性能衰减时自动重启
./manage.sh watchdog-status    # watchdog 结构化 JSON 状态
./manage.sh route-force-local <session_id>   # 强制会话走本地
./manage.sh route-force-cloud <session_id>   # 强制会话走云端
./manage.sh monitor [N]        # Metal 内存实时监控（每 N 秒刷新，默认 5）
./manage.sh models             # 模型目录总览（providers/models/routes、key 就绪状态、hash）
./manage.sh models-validate    # 校验 configs/models.json（坏文件非零退出）
./manage.sh config-lint [file] # 校验配置文件与 CONFIG_REGISTRY 一致性（默认 active.conf）
./manage.sh autotune help      # BVR 自调优闭环 CLI（generate/list/apply/rollback/status，设计文档 bvr-autotune-loop-design-20260916）
./manage.sh fix-template <dir> # 修复 Qwen chat_template
```

#### 辅助引擎：`tools/engines.sh`

`manage.sh` 只管理 `active.conf`（主后端）；**其余引擎用 `tools/engines.sh` 独立管理**，
各自端口/进程，与主后端共存：

```bash
tools/engines.sh list                  # 列出 configs/*.conf 中的辅助引擎
tools/engines.sh start   <name>        # 启动（读 configs/<name>.conf）
tools/engines.sh stop    <name>        # 优雅停止（SIGTERM → 20s → SIGKILL）
tools/engines.sh restart <name>
tools/engines.sh status  [name]
tools/engines.sh supervise <name> --daemon   # 常驻守护（被误杀自动重启）
```

> ⚠️ **参数对齐约束（DEF-211）**：`engines.sh` 与 `manage.sh` 各自拼装 `rapid-mlx serve`
> 命令行。**任何一侧新增启动参数都必须同步另一侧**，否则会出现静默降级——典型症状是
> `--tool-call-parser` 已设但漏了 `--enable-auto-tool-choice`，`/v1/models` 的
> `capabilities` 少掉 `tools`，tool calling 永久失效且不报错。
> 验收辅助引擎时务必检查 `capabilities`，不要只看 `HTTP 200`。
> 详见 [`docs/05-operations-changelog/engines-sh-param-alignment-20261020.md`](docs/05-operations-changelog/engines-sh-param-alignment-20261020.md)。

> ⚠️ **内存约束**：48 GB 机器上主后端（Splash 27B 约 15–19 GB）与辅助引擎（9B 约 5.7 GB）
> **不可同时常驻**——会触发全局换页，把两者一起拖慢（实测 900-token 生成从 55.8 掉到 10.6 tok/s）。
> 详见 [`docs/04-analysis-diagnostics/ornith-9b-memory-pressure-20261020.md`](docs/04-analysis-diagnostics/ornith-9b-memory-pressure-20261020.md)。

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

**双协议端点**：`POST /v1/messages`（Anthropic 格式，主端点，流式 + 非流式）**和** `POST /v1/chat/completions`（OpenAI 格式，经 `convert_openai_request_to_anthropic` 转换入管线，`_openai_mode` 下原样返回 OpenAI 格式响应）。另有 `GET /v1/models`、`OPTIONS`。

**结构化 Admin API**（供 agent_go / 外部编排器使用，部分在 `admin_server.py` 实现）：

> **Admin API 全表**（`/api/status`、`/api/route/policies`、`/api/session/*`、`/admin/inject` 等 18 端点）
> → [`docs/02-architecture-design/codebase-reference-20261017.md`](docs/02-architecture-design/codebase-reference-20261017.md) §五。
### 4.4 后端类型自动检测

`BACKEND_TYPE` 从 `LLAMA_BASE_URL` 自动推断：

- URL 包含 `deepseek`、`openai`、`api.` → `cloud`
- 否则 → `local`（含 rapid-mlx / dflash-mlx / llama-server，均走 `http://127.0.0.1:8081/v1`）

`MODEL_NAME` 也会自动设置（local 默认 `mlx-community/Qwen3.6-35B-A3B-4bit`，cloud 默认 `deepseek-v4-pro`；`deepseek-chat.conf` 显式设为 `deepseek-v4-flash`）。这些默认值由 `backend_strategy.py` 的 `LocalStrategy` / `CloudStrategy` 提供。

### 4.5 Splash 引擎（2026-09-23 接入，runbook §29）

`LLAMA_BACKEND=splash`（`configs/splash-qwen36-35b.conf` / `splash-qwen38-27b.conf`）：Inco AI 开源引擎，**DFlash 2 投机解码内置于 decode 路径**，`splash serve` 启动。

- **进程模型（与 rapid-mlx 关键差异）**：splash CLI spawn `server.py`（前端，持 :8081 LISTEN）+ `serve-native`（引擎子进程）后**自身退出** → manage.sh 按端口感知前端 PID；杀前端→native 连带退出，stop 兜底 `pkill serve-native`
- **互斥单引擎**：与 Ornith（rapid-mlx）不可同跑，切换 = `switch <name> && restart`
- **诊断面（2026-10-17 实测更正）**：Splash 自带完整 Prometheus 指标面 `GET /metrics`（`splash_*` 族：TTFT/ITL 分位、state 命中/未中、KV 页水位、draft 接受率、内存压力等），**可观测性不弱于 rapid-mlx**；仍无对应物的是 rapid-mlx 特有计数（snapdown/驱逐计数、响应 timings 字段、KV dtype gauge）与 `/props`。`/api/status` 的 backend 进程匹配已加 `splash`
- **性能参照**（27B/35B-A3B，M5 Pro 48GB）：代码任务 78–264 tok/s（DFlash2 接受率红利）、32K 热 TTFT 0.10–0.36s、32K 冷 prefill 611–3139 tok/s；散文受带宽极限（27B 38.8 = 上限）
- **模型格式封闭**：仅 `incoai/*-Splash` 专有包（`splash-packed-q4[-moe]`），普通 MLX/HF 权重不加载
- **切换纪律**：批跑期禁切（红线 1）；admin_server.py 的 splash 匹配需代理重启生效

> **hybrid 前缀缓存必知**：Qwen3.8-27B / Qwen3.6-35B-A3B / Ornith-1.5-35B-A3B（均 Qwen3.5/3.6/3.8 系 hybrid GatedDeltaNet）在 rapid-mlx 中对 prefix cache 条目标 `non_trimmable`（只整条精确匹配）。必须 `--hybrid-cache-entries 8`（启用 trim-free 前缀复用；0=禁用→每轮全量冷 prefill）；关 `PROXY_CTX_ENGINE_ENABLED` 配合稳定前缀可保命中。**0.14.3 起上游内置 hybrid recurrent-state checkpoint**（`RAPID_MLX_HYBRID_CHECKPOINT_MAX`=4/`_STRIDE`=2048，prefill chunk 级快照 + fetch 侧 LCP/supersequence snap），使部分前缀复用成为可能（实测冷 prefill 2.3–3.6×）；**cache 上限勿盲目调大**：`--cache-memory-mb` 12GB→16GB 实测反引入 Metal pressure（cache 上限与 Metal cap 打架），已回退；**降 `--hybrid-cache-entries` 反而三赢**（见 §11.2 表）。另：hybrid 骨干被 rapid-mlx 门控关闭投机解码（MTP/DFlash/DSpark 均不可用，见 issue #1941）。详见 `docs/05-operations-changelog/dflash-mlx-integration-20260826.md` 与 `docs/05-operations-changelog/ornith-15-integration-20260826.md`。

---

## 5. 测试策略

测试使用统一的 Bash 入口 `test/run_tests.sh`。

### 5.1 测试层级

| 层级 | 命令 | 依赖 | 说明 |
|------|------|------|------|
| 单元 | `bash test/run_tests.sh --unit` | 无 | `test/unit/test_*.py`，纯函数逻辑，51 个文件 1516 个用例（2026-09-02 实测），<15s |
| 集成 | `bash test/run_tests.sh --integration` | 启动 mock backend | `test/integration/*.sh` + `mock_backend.py`，约 60s |
| Promptfoo | `bash test/run_tests.sh --promptfoo` | 真实后端（:8081） | 固定 prompt 回归测试（9 个用例）；**影子代理环境**——从工作树启动独立代理（:4021，引擎关、状态隔离，测的是待提交代码），不打生产代理（2026-09-06，此前直打生产有三重缺陷：代码滞后/engine canonical 污染/生产状态扰动，见 `test/lib/promptfoo_env.sh` 头注） |
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
4. 如果真实后端正在运行，额外运行 `--promptfoo` 快速模式（只跑 5 个核心用例；影子代理环境，批跑实验占用后端时会因排队变慢）

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

- 修改 `anthropic_proxy.py` / `pipeline.py`：必须跑 `bash test/run_tests.sh --all`（特别容易破坏流式/非流式工具调用、阻塞检测、云端模式、双协议端点）。
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
- 新增管线阶段：继承 `PipelineStage`，保持为已抽取模块之上的薄封装（deferred import 规避 `proxy_state` 循环依赖），并在 `test/unit/test_pipeline_stages.py` 补测试。
- 日志同时输出到 stdout 和 `/tmp/anthropic_proxy.log`（manage.sh 启动时为 `logs/anthropic_proxy.log`）。主日志按大小 copytruncate 轮转（`PROXY_LOG_ROTATE_MB`，默认 50MB × `PROXY_LOG_ROTATE_KEEP`=3 份备份，A2 2026-08-20）；manage.sh wrapper 将 stdout 指向 /dev/null、仅 stderr 落文件——`log()` 是唯一按路径写入方，无双写。

### 6.3 配置文件

- Bash-sourcable 语法：`KEY="value"`。
- 注释与章节标题使用 **中文**。
- 自包含，不 include 其他文件。
- 必须包含 `CONFIG_NAME`、`CONFIG_DESC`、`CONFIG_MEMORY`。
- API Key 只放在 `configs/secret.local.conf`（已被 `.gitignore` 排除）。

### 6.4 文档

- `docs/` 下 7 类目录结构固定；新增文档按命名规范放入对应目录（见 `docs/README.md`）。
- 日期后缀使用 `YYYYMMDD`。
- 修改架构约定后，必须同步更新 `CLAUDE.md` 与本文件。

### 6.4.1 共享技能（`skills/`）

- Agent 可复用技能放**顶层 `skills/<name>/`**（`SKILL.md` + 可选 `references/`），
  不放 `docs/`（那里是固定 7 类的人类文档），也不放 `.agents/`（工具本地状态、已 ignore）。
- 各工具的技能搜索路径（`.agents/skills/`、`.claude/skills/`）用**符号链接**指向
  `skills/<name>`，保持单一真相源，禁止复制两份。
- 约定与检查清单见 [`skills/README.md`](skills/README.md)。

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

- `vllm-mlx` v0.6.71 若无法连接 HuggingFace 会在启动时挂起（无报错输出的 `ConnectTimeout` 重试循环）。
- 相关配置中必须加 `export HF_HUB_OFFLINE=1`，见 `configs/archived/qwen3-8b.conf`。

### 8.3 KV-cache / prefix-cache turboquant

- Rapid-MLX 如果需要跨重启保留 prefix cache，**不要**使用 `--kv-cache-turboquant`。
- Prefix cache 现状：rapid-mlx **0.11.5** 起重新启用跨请求 prefix cache（active 配置 `RAPID_MLX_ENABLE_PREFIX_CACHE=true`）；旧的 0.6.71 BatchedEngine 不支持（PagedCache 仅提供请求内 KV 管理）。KV 量化用 `RAPID_MLX_KV_QUANTIZATION=true` + 4 bits。

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
- 修复方式：替换模型目录中的 `chat_template.jinja`（rapid-mlx）或使用 `--chat-template` 参数（llama-server）。详情见 `docs/06-reference-metrics/TROUBLESHOOTING.md`。

---

## 9. 修改检查清单

在提交前，根据改动范围执行对应检查：

- [ ] **如果修改 `manage.sh`**：手动测试 `start`、`stop`、`restart`、`reload`、`start-backend`、`stop-backend`、`status`、`switch <name> && reload` 在本地与云端模式下是否都正常。
- [ ] **如果修改 `anthropic_proxy.py` / `pipeline.py`**：运行 `bash test/run_tests.sh --all`；重点检查流式/非流式工具调用、阻塞检测、云端模式、双协议端点（`/v1/chat/completions`）。
- [ ] **如果新增配置变量**：在 `manage.sh` 加默认值，在 `proxy_config.py` 的 `CONFIG_REGISTRY` 注册，并同步更新 `CLAUDE.md` 与本文件。
- [ ] **如果新增后端/云服务商**：更新 `anthropic_proxy.py` 中的 `BACKEND_TYPE` 自动检测逻辑与 URL 模式文档。
- [ ] **如果修改截断、循环检测、阻塞逻辑**：对照 `docs/04-analysis-diagnostics/DEFECT-LIST.md` 检查是否重新引入已知 P0 问题。
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
| agent_go 集成契约（R1-R12） | [`docs/01-requirements-product/llama-defender-integration-requirements.md`](docs/01-requirements-product/llama-defender-integration-requirements.md) |
| 已知缺陷列表 | [`docs/04-analysis-diagnostics/DEFECT-LIST.md`](docs/04-analysis-diagnostics/DEFECT-LIST.md) |
| 故障记录与 workaround | [`docs/06-reference-metrics/TROUBLESHOOTING.md`](docs/06-reference-metrics/TROUBLESHOOTING.md) |
| 性能基线 | [`docs/06-reference-metrics/BENCHMARK.md`](docs/06-reference-metrics/BENCHMARK.md) |
| 测试布局 | [`test/README.md`](test/README.md) |
| 需求追踪 | [`docs/requirements.yaml`](docs/requirements.yaml) + [`tools/trace_requirements.py`](tools/trace_requirements.py) |
| Claude Code 专用指南 | [`CLAUDE.md`](CLAUDE.md) |
| swe-eval ↔ llama.cpp 联合协调（跨仓） | `~/APP/swe-eval/docs/joint-coordination-llamacpp.md`（共识/决议 J-系/待办对账唯一权威，改前先读最新版） |
| 代码库参考（模块/管线/工具/Admin API） | [`docs/02-architecture-design/codebase-reference-20261017.md`](docs/02-architecture-design/codebase-reference-20261017.md) |
| 配置参数指南（推荐组合全表） | [`docs/06-reference-metrics/config-parameters-guide-20261017.md`](docs/06-reference-metrics/config-parameters-guide-20261017.md) |
| 9B 能力调研/质量门/haiku 路由裁决 | [`docs/06-reference-metrics/ornith-9b-capability-research-20261015.md`](docs/06-reference-metrics/ornith-9b-capability-research-20261015.md) |
| 引擎重启协调协议（硬规则） | [`docs/05-operations-changelog/engine-restart-coordination-20260906.md`](docs/05-operations-changelog/engine-restart-coordination-20260906.md) |

---

## 11. 关键配置参数与推荐组合

> **全表已拆分** → [`docs/06-reference-metrics/config-parameters-guide-20261017.md`](docs/06-reference-metrics/config-parameters-guide-20261017.md)
> （压缩 Profile / 本地模式 / 云端模式 / 路由 / 队列 / 诊断数据面 / admin 注入 / BVR 自调优 / 跨会话记忆 九张参数表）。
> 权威默认值 = [`proxy_config.py`](proxy_config.py) 的 `CONFIG_REGISTRY`；新增参数必须同时登记 CONFIG_REGISTRY + manage.sh + 该指南。

最常用速查：压缩档 `PROXY_COMPRESSION_PROFILE=balanced`（默认）| `aggressive`（长上下文）| `conservative`（质量敏感）；
本地并发恒 `PROXY_MAX_CONCURRENT=1`（48GB OOM 红线）；批期红线见 §8 与 `docs/05-operations-changelog/` runbook。

---

## 12. 缺陷修复状态

> 缺陷唯一权威 = [`docs/04-analysis-diagnostics/DEFECT-LIST.md`](docs/04-analysis-diagnostics/DEFECT-LIST.md)（DEF-001…，含状态与修复要点）。
> 历史 2026-07 快照统计已随拆分移出（git 历史可查）；新缺陷一律登记 DEFECT-LIST，不在本文件维护。

---

> 历史版本：git 历史即存档（`git log -- AGENTS.md`）。大拆分前全量版本见 2026-10-17 之前提交。
