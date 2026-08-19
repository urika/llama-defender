<!-- AGENTS.md — Local LLM Inference Stack -->

> 本文件面向 AI 编程助手。首次接触本仓库时，请先阅读本节以建立全局认知；细节请参阅 [`CLAUDE.md`](CLAUDE.md) 与 [`docs/README.md`](docs/README.md)。

---

## 1. 项目概述

**这不是 llama.cpp 的 C++ 源码仓库。** 它是一个运行在 Python 与 Bash 之上的本地 LLM 推理编排层，核心职责是把下游的 `llama-server` 或 `rapid-mlx` 包装成一个 Anthropic 兼容的 API，供 Claude Code 等客户端使用。消费方 agent_go 项目把本服务称为 **llama-defender**（集成契约见 [`docs/llama-defender-integration-requirements.md`](docs/llama-defender-integration-requirements.md)：R1-R12 已全部交付）。

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

仓库根目录下一共有约 17 个核心 Python 文件和 1 个 Bash 脚本。所有业务逻辑均围绕 `anthropic_proxy.py` → `pipeline.py` 的请求管线展开。

### 3.1 核心文件

| 文件 | 用途 |
|------|------|
| [`manage.sh`](manage.sh) | 服务管理器：start/stop/restart/reload/switch/status/watchdog/wizard，以及本地/云端路由强制切换 |
| [`anthropic_proxy.py`](anthropic_proxy.py) | HTTP 代理入口：`ThreadingHTTPServer` + `Handler`（~1480 行）；流式/非流式响应处理、双协议端点；请求处理逻辑已全部下沉到 `pipeline.py` |
| [`pipeline.py`](pipeline.py) | 管线抽象：24 个可独立测试的 `PipelineStage`，把 `_handle_messages` 拆成 RequestParser、LifecycleClassifier、SmartRouter、ContentCompressor、ContextTruncator、BackendDispatcher 等阶段 |
| [`proxy_state.py`](proxy_state.py) | 单一真相源：所有 `PROXY_*` / `LLAMA_*` 常量、共享可变状态、线程本地上下文、`_RELOAD_SPEC`、模型别名、路由状态 |
| [`proxy_config.py`](proxy_config.py) | `CONFIG_REGISTRY`：每个环境变量的默认值（区分 local/cloud）、类型、作用域、文档说明；配置统一阶段一起为默认值唯一权威（`get_default()` / `validate_startup()` / `write_defaults_sh()`） |
| [`backend_strategy.py`](backend_strategy.py) | `BackendStrategy` / `LocalStrategy` / `CloudStrategy`：把 38+ 处 `if IS_CLOUD` 收敛为策略类 |
| [`reload_config.py`](reload_config.py) | SIGHUP 热重载实现：重新解析 `configs/active.conf` + `configs/secret.local.conf`，更新 `proxy_state` 与主模块；同时重载模型目录并重建 `MODEL_ROUTE_PREFERENCES` |
| [`model_registry.py`](model_registry.py) | 模型目录注册表：加载/校验 `configs/models.json`（providers/models/routes 三段，对齐 agent_go 三层设计），`$env`/`$default` 引用、fallback chain、坏文件拒绝热替换、`catalog_hash`；目录文件缺失时自动合成等价目录（与旧硬编码行为一致），`MODEL_ROUTE_PREFERENCES` 与 `get_model_aliases()` 均由其派生 |
| [`message_converter.py`](message_converter.py) | Anthropic ↔ OpenAI 消息与工具格式双向转换（含 `convert_openai_request_to_anthropic`，双协议端点入口）、token 估算 |
| [`tool_parser.py`](tool_parser.py) | XML ↔ JSON 工具参数解析、`<tools>` 内容块 fallback、流式工具提取器 |
| [`content_compressor.py`](content_compressor.py) | TokenSieve 语义压缩 + BM25 相关性驱动压缩（TS-1）：JSON / 代码 / 日志 / 文本的分层压缩。TS-4（2026-08-18 日志分析落地）：BM25 drop 分支改为类型感知结构化压缩（`_structured_compress`，json/code/log 保结构），结果超 `PROXY_BM25_DROP_TARGET_RATIO`（默认 0.45）再截断封顶；`PROXY_BM25_DROP_THRESHOLD` 默认 0.5→0.1；客户端断连（BrokenPipe）按 499 记账不再计 500 |
| [`compression_types.py`](compression_types.py) | TS-3 统一压缩结果类型契约：`CompressionResult` / `CompressionSubResult` TypedDict（JSON 可序列化，兼容 Py3.8+） |
| [`session_ledger.py`](session_ledger.py) | R14/R15 诊断存储：会话台账（action 轨迹 / dup / last_dup_turn / 材料清单，客户端原始历史增量扫描 + 前缀失配全量重建 → `canonical_mismatch`）+ sent_view 档案（`logs/diag/archive/<sid>.jsonl`，MB 上限，TTL 驱逐） |
| [`diagnostics.py`](diagnostics.py) | R13/R16 诊断记录器：per-request 累积（7 处注入登记 / timings 能力探测 / token 事实）、`X-Proxy-Diag-*` 响应头、SSE 尾注 `: x-proxy-diag {...}`、`logs/diag/sessions.jsonl` per-turn 深度记录（request_id 与 proxy_metrics 关联）、lifecycle 事件（R7 兑现） |
| [`truncation.py`](truncation.py) | 上下文截断：char / rounds / fifo / smart 策略、单遍合并压缩（L2 清除 + L4 thinking 剥离）、工具对原子保护（TS-2）、关键词索引、摘要缓存 |
| [`lifecycle.py`](lifecycle.py) | 生命周期阶段分类（init/growth/expansion/saturation/oom_danger/pre_trunc）与动态 token 预算 |
| [`loop_detection.py`](loop_detection.py) | 工具循环、文本输出循环、阻塞模式检测与干预 |
| [`tool_filter.py`](tool_filter.py) | 动态工具定义过滤，降低长工具列表的 token 开销 |
| [`admin_server.py`](admin_server.py) | `/status` 状态页、`/api/*` 结构化 JSON 端点、系统内存/进程信息、指标聚合与 `/metrics/history`、请求快照清理、并发统计 |
| [`proxy_logging.py`](proxy_logging.py) | 结构化 JSONL 日志、敏感头脱敏 |

### 3.2 数据流

```
Client POST /v1/messages（Anthropic）或 POST /v1/chat/completions（OpenAI，经 convert_openai_request_to_anthropic 转换）
  → anthropic_proxy.py:Handler.do_POST()
  → _llama_lock 获取并发许可
  → Handler._handle_messages()
  → InstrumentedPipeline 依次执行 24 个 stage（编号为代码注释中的历史层号）
       0. RequestParser
       1. LifecycleClassifier
       2. DynamicMaxTokens
       2.5. SmartRouter (local/cloud 路由决策)
       2.6. RouteNotification
       3. ErrorTranslator
       4. BlockerDetector
       5. SystemNormalizer
       6. CacheAligner
       7. ContentCompressor
       8. ToolLoopDetector
       9. TextLoopDetector
      10. SessionLoopState
      11. LoopIntervention
      12. RereadDetector
      13. DateNormalizer
      14. ContextTruncator
      15. HighDropRatioNotice
      16. MessageHashDebug
      17. OOMSafetyFIFO
      18. PrefixRatioComputer
      19. ToolPairingRepair
      20. FormatConverter
      21. BackendDispatcher
  → 转发到后端（local 后端走 OpenAI chat completions 格式；cloud 直接透传；OpenAI 端点请求 `_openai_mode` 原样返回 OpenAI 格式响应）
  → 流式或非流式响应返回 Client
```

### 3.3 配置文件

配置放在 `configs/*.conf`，是 Bash 可 source 的 `KEY="value"` 文件。

| 文件 | 说明 |
|------|------|
| `configs/active.conf` | 指向当前激活配置的符号链接（当前 → `rapid-mlx-35b-opt.conf`） |
| `configs/rapid-mlx-35b-opt.conf` | rapid-mlx + Qwen3.6-35B-A3B-UD-MLX-4bit（**当前激活**，GPU=70%，prefix cache + KV q4 开启） |
| `configs/qwen3.6-27b-4bit.conf` | rapid-mlx + Qwen3.6-27B dense 4bit（tool clearing 关闭） |
| `configs/gemma4-26b.conf` | rapid-mlx + Gemma-4-26B，并发 2，数据处理优化 |
| `configs/deepseek-chat.conf` | 云端 DeepSeek OpenAI 兼容端点（`deepseek-v4-flash`） |
| `configs/models.json` | 模型目录（providers/models/routes），`model_registry.py` 加载、SIGHUP 热重载；**删除后自动合成等价目录**，新增云端模型只需加条目（Phase A+） |
| `configs/secret.local.conf` | **git-ignored**，存放真实 API Key（含分提供商 `ZHIPU_API_KEY`/`KIMI_API_KEY`，由目录 `key_env` 引用） |
| `configs/archived/` | 归档的历史配置（qwen3-8b、rapid-mlx-9b、rapid-mlx-35b、thinkingcap-…-mtp 等） |

配置文件中必须包含元数据：`CONFIG_NAME`、`CONFIG_DESC`、`CONFIG_MEMORY`，供 `./manage.sh list` 读取。

### 3.4 工具与文档

- `tools/`：benchmark（`bench_*.py`）、分析（`analyze_*.py`）、监控（`monitor.py`、`sysmon.sh`）、需求追踪（`trace_requirements.py`）、模块提取（`extract_module.py`）等。
- `docs/`：按 7 类组织（需求产品、架构设计、实验测试、分析诊断、运维变更、参考指标、项目看板 `07-project-board/`）。入口 [`docs/README.md`](docs/README.md)；根级关键文档含 `llama-defender-integration-requirements.md`（agent_go 集成契约）、`DEFECT-LIST.md`、`requirement-matrix.md`。
- `test/`：自动化测试，见下节。
- `logs/`：运行时日志、测试日志、metrics、快照（git-ignored）。
- `assets/chat-templates/`：Qwen chat template 修复模板。

---

## 4. 运行命令

### 4.1 日常服务命令

```bash
./manage.sh start              # 按 active.conf 启动本地后端 + 代理
./manage.sh start --profile aggressive  # 压缩策略：balanced（默认）/ aggressive / conservative
./manage.sh start-cloud        # 仅启动代理，转发到云端 API
./manage.sh start-backend / stop-backend # 单独启停本地后端（优先用 stop-backend 优雅停止，避免 Metal 死锁）
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

**双协议端点**：`POST /v1/messages`（Anthropic 格式，主端点，流式 + 非流式）**和** `POST /v1/chat/completions`（OpenAI 格式，经 `convert_openai_request_to_anthropic` 转换入管线，`_openai_mode` 下原样返回 OpenAI 格式响应）。另有 `GET /v1/models`、`OPTIONS`。

**结构化 Admin API**（供 agent_go / 外部编排器使用，部分在 `admin_server.py` 实现）：

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/status` | 结构化健康与就绪状态：`proxy`、`backend`、`active_profile`、`state`、`ready`、`route_config`（R11 路由配置摘要） |
| GET | `/api/route/policies` | 脱敏路由策略 + 模型目录（R9）：providers（key 只回 `key_set` 布尔）、models、preferences、defaults、`catalog_hash`（agent_go 漂移检测） |
| GET | `/api/watchdog` | watchdog 状态：`enabled`、`running`、`pid`、`restart_count_1h`、`last_failure_reason` |
| GET | `/api/queue` | 请求优先级队列状态（Phase 1 默认关闭）：`enabled`、`workers`、`waiting`、`by_bucket`、`oldest_wait_ms` |
| GET | `/api/profiles` | 可用模型配置列表：`name`、`desc`、`memory_gb`、`active` |
| GET | `/metrics[?n=N]` | 最近请求指标（JSON） |
| GET | `/metrics/history[?session=K]` | 历史指标（JSON，R16 可选会话过滤） |
| GET | `/api/sessions` | 活跃诊断会话列表（R14 发现端点）：`key`/`key_source`/`turns`/`last_seen` |
| GET | `/api/session/<key>/ledger` | 会话台账（R14）：actions（dup/last_dup_turn）/ dup_queries / materials；404 未知、410 已驱逐 |
| GET | `/api/session/<key>/archive?view=sent` | sent_view 档案（R15）：默认索引模式，`include_payload=true` 拉正文；canonical 视图 Phase 1 前 501 |
| GET | `/api/session/<key>/metrics` | 会话诊断聚合（R16）：hit_ratio 分位、epoch/非 epoch 延迟分档、per-turn 时序 |
| GET | `/api/backend/props` / `/api/backend/slots` | llama-server 原生端点只读反代；后端不支持时 501 结构化降级 |
| POST | `/admin/route/force-local` / `force-cloud` | 会话级路由覆盖 |
| POST | `/admin/reload` | HTTP 热重载（R12，等效 `manage.sh reload`，含模型目录重载） |
| GET | `/status` | 人类可读 HTML 状态页 |

- `/api/status` 在 `state` 为 `healthy` 或 `starting` 时返回 200，否则 503（同 JSON body）。`state` 枚举：`healthy | starting | backend_down | proxy_down | model_drift | down`。
- `ready` 表示后端模型已加载、可接受推理请求（`starting` → `ready=false`），agent_go 的 `wait_ready` 以此字段为准。
- 路由响应头（R8 契约名）：每个路由响应带 `X-Proxy-Route-Target`（`cloud|local|local_forced`）、`X-Proxy-Route-Actual-Model`、`X-Proxy-Route-Reason`、`X-Proxy-Route-Cost`（预估费用，本地为 0）；OpenAI 协议非流式响应体另带 `proxy_route` 字段（实际 usage 计费）。单请求路由覆盖用请求头 `X-Proxy-Route-To: local|cloud`（无会话粘性）。
- 多提供商分发（Phase B）：按模型解析目录 provider 凭证/并发锁；路由 `fallback_chain` 跨商降级（跳过冷却中/无 key 的提供商）；分商熔断互不影响；按模型目录价格计费 + 全局/分商预算双上限。
- Anthropic 协议分发（Phase D）：provider 可声明 `protocol: anthropic`（双端点双 key：`anthropic_base_url` + `anthropic_key_env`）——该 provider 的模型走 `{anthropic_base_url}/v1/messages`（Anthropic 协议，如 Z.ai Coding Plan 订阅），SSE 原样透传、非流式直返 + `proxy_route` 归因；OpenAI 协议客户端自动跳过 anthropic 候选并沿链降级。当前 zhipu=protocol anthropic（订阅路径，glm 边际成本 0）。
- **R8-R12 已全部交付（2026-08-15）**：R8 归因头四件套 `X-Proxy-Route-*`（含 Cost 与 local_forced）+ OpenAI 协议非流式 `proxy_route` 体字段；R9 `GET /api/route/policies`（脱敏目录 + `catalog_hash`）；R10 `/v1/models` 能力元数据（`real_model`/`thinking_*`/`json_compliance`/`context_chars`/`price`/`direct_capable`）；R11 `/api/status` `route_config` 段；R12 `POST /admin/reload`。CLI 侧配套 `./manage.sh models` / `models-validate`。
- **R13-R16 诊断数据面已交付（2026-08-19，设计文档 [`docs/02-architecture-design/diagnostics-dataplane-design-20260819.md`](docs/02-architecture-design/diagnostics-dataplane-design-20260819.md)）**：R13 诊断归因双通道——非流式 HTTP 头 `X-Proxy-Diag-Request-Id` / `X-Proxy-Feedback-Injected`（csv）/ `X-Proxy-Prompt-Processed-N`（仅后端返回 timings 时，`hit_ratio = 1 − prompt_n/prompt_tokens`），流式经 SSE 注释行尾注 `: x-proxy-diag {...}`（message_stop / [DONE] 之前，规范保证被所有解析器忽略）；现有 7 处合成内容注入全部可计量。R14 台账 + `/api/sessions` 发现。R15 sent_view 每轮落盘（「模型实际所见」唯一权威）。R16 `logs/diag/sessions.jsonl` per-turn 深度记录 + `/api/status` `ctx_config` 段（bench 口径机读源）+ `lifecycle_events.jsonl` 激活（`canonical_mismatch`）。总开关 `PROXY_DIAG_ENABLED`。

### 4.4 后端类型自动检测

`BACKEND_TYPE` 从 `LLAMA_BASE_URL` 自动推断：

- URL 包含 `deepseek`、`openai`、`api.` → `cloud`
- 否则 → `local`

`MODEL_NAME` 也会自动设置（local 默认 `mlx-community/Qwen3.6-35B-A3B-4bit`，cloud 默认 `deepseek-v4-pro`；`deepseek-chat.conf` 显式设为 `deepseek-v4-flash`）。这些默认值由 `backend_strategy.py` 的 `LocalStrategy` / `CloudStrategy` 提供。

---

## 5. 测试策略

测试使用统一的 Bash 入口 `test/run_tests.sh`。

### 5.1 测试层级

| 层级 | 命令 | 依赖 | 说明 |
|------|------|------|------|
| 单元 | `bash test/run_tests.sh --unit` | 无 | `test/unit/test_*.py`，纯函数逻辑，25 个文件约 979 个用例，<1s |
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
- 日志同时输出到 stdout 和 `/tmp/anthropic_proxy.log`。

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
- 见 `configs/rapid-mlx-35b-opt.conf` 说明。
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
- 修复方式：替换模型目录中的 `chat_template.jinja`（rapid-mlx）或使用 `--chat-template` 参数（llama-server）。详情见 `TROUBLESHOOTING.md`。

---

## 9. 修改检查清单

在提交前，根据改动范围执行对应检查：

- [ ] **如果修改 `manage.sh`**：手动测试 `start`、`stop`、`restart`、`reload`、`start-backend`、`stop-backend`、`status`、`switch <name> && reload` 在本地与云端模式下是否都正常。
- [ ] **如果修改 `anthropic_proxy.py` / `pipeline.py`**：运行 `bash test/run_tests.sh --all`；重点检查流式/非流式工具调用、阻塞检测、云端模式、双协议端点（`/v1/chat/completions`）。
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
| agent_go 集成契约（R1-R12） | [`docs/llama-defender-integration-requirements.md`](docs/llama-defender-integration-requirements.md) |
| 已知缺陷列表 | [`docs/DEFECT-LIST.md`](docs/DEFECT-LIST.md) |
| 故障记录与 workaround | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) |
| 性能基线 | [`BENCHMARK.md`](BENCHMARK.md) |
| 测试布局 | [`test/README.md`](test/README.md) |
| 需求追踪 | [`docs/requirements.yaml`](docs/requirements.yaml) + [`tools/trace_requirements.py`](tools/trace_requirements.py) |
| Claude Code 专用指南 | [`CLAUDE.md`](CLAUDE.md) |

---

## 11. 关键配置参数与推荐组合

> 完整参数注册表见 [`proxy_config.py`](proxy_config.py) 的 `CONFIG_REGISTRY`（约 100+ 参数）。
> 以下仅列出最常用的参数及其推荐值。

### 11.1 压缩 Profile 推荐组合

`proxy_config.py` 定义了三种预设组合，通过 `PROXY_COMPRESSION_PROFILE` 切换：

| 参数 | balanced（默认） | aggressive | conservative |
|------|-----------------|------------|--------------|
| `PROXY_COMPRESS_MODE` | `smart` | `aggressive` | `conservative` |
| `PROXY_COMPRESS_MIN_CHARS` | 3000 | 1500 | 5000 |
| `PROXY_COMPRESS_TARGET_RATIO` | 0.40 | 0.25 | 0.55 |
| `PROXY_COMPRESS_LLM_CHUNK` | 4000 | 3000 | 6000 |
| `PROXY_COMPRESS_LLM_ENABLED` | `true` | `true` | `false` |
| `PROXY_COMPRESS_CLEAR_ENABLED` | `true` | `true` | `false` |
| `PROXY_CTX_TRUNCATE_STRATEGY` | `fifo` | `fifo` | `rounds` |
| `PROXY_CTX_KEEP_MESSAGES` | 40 | 30 | 50 |
| `PROXY_LOOP_THRESHOLD` | 5 | 4 | 6 |
| `PROXY_OOM_SAFE_TOKENS` | 60000 | 50000 | 70000 |

**选择建议**：
- **balanced**: 日常 coding 任务，兼顾质量与性能
- **aggressive**: 长上下文 agentic 场景，优先控制 token 消耗
- **conservative**: 质量敏感任务（代码审查、文档生成），优先保留上下文完整性

### 11.2 本地模式关键参数

| 参数 | 默认值 | 推荐值 | 说明 |
|------|--------|--------|------|
| `PROXY_MAX_CONCURRENT` | `1` | `1` | 48GB Mac 上推荐 1，OOM 风险 |
| `PROXY_CTX_TRUNCATE_STRATEGY` | `fifo` | `fifo` | 当前生产策略，prefix cache 友好 |
| `PROXY_CTX_KEEP_MESSAGES` | `40` | `40` | fifo 窗口大小 |
| `PROXY_OOM_SAFE_TOKENS` | `60000` | `60000` | OOM 安全阈值，约 120K chars |
| `PROXY_PRE_TRUNCATE_CHARS` | `400000` | `400000` | 请求体预截断阈值 |
| `PROXY_MAX_REQUEST_BYTES` | `512000` | `512000` | 请求体硬上限（500KB） |
| `PROXY_CLEAR_ENABLED` | `false` | `false` | 本地后端建议关闭，避免 Wasted call 循环 |
| `PROXY_TOOL_FILTER_ENABLED` | `true` | `true` | 本地模式默认开启 |
| `PROXY_TOOL_FILTER_MAX` | `20` | `20` | 超过此数量触发过滤 |
| `PROXY_TOOL_AUTO_PROMOTE_THRESHOLD` | `3` | `3` | 使用 ≥3 次自动加入 keep 集 |
| `PROXY_LOOP_THRESHOLD` | `5` | `5` | 循环检测阈值 |
| `PROXY_LOOP_LEVEL3` | `9` | `9` | Level 3 触发阈值（移除全部工具） |
| `PROXY_COMPRESSION_PROFILE` | `balanced` | `balanced` | 压缩策略预设组合 |

### 11.3 云端模式关键参数

| 参数 | 默认值 | 推荐值 | 说明 |
|------|--------|--------|------|
| `PROXY_MAX_CONCURRENT` | `5` | `5` | 云端并发高，API 无 OOM 风险 |
| `PROXY_TOOL_FILTER_ENABLED` | `false` | `false` | 云端不触发工具过滤 |
| `PROXY_CLEAR_ENABLED` | `true` | `true` | 云端可开启 tool result 清理 |
| `PROXY_COMPRESSION_PROFILE` | `balanced` | `aggressive` | 云端按 token 计费，建议激进压缩 |

### 11.4 路由参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_ROUTE_SENSITIVE_PATTERNS` | `""` | 匹配的请求强制走本地（逗号分隔关键字） |
| `PROXY_ROUTE_FORCE` | `""` | 强制路由模式：`local` 或 `cloud` |
| `PROXY_CLOUD_COOLDOWN` | `300` | 云端失败后的冷却时间（秒） |
| `PROXY_CLOUD_MAX_RETRY` | `2` | 云端重试次数 |

### 11.5 请求队列参数（Phase 1）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_QUEUE_ENABLED` | `false` | 启用优先级请求队列；默认关闭 = 原信号量行为（当前 `qwen3.8-27b-4bit` 灰度开启） |
| `PROXY_QUEUE_TIMEOUT_SECONDS` | `300` | 排队超时，超时返回 503 + `Retry-After` |
| `PROXY_QUEUE_LARGE_THRESHOLD_CHARS` | `80000` | large bucket 阈值（低优先级排队） |
| `PROXY_QUEUE_HUGE_THRESHOLD_CHARS` | `200000` | huge bucket 阈值：不入本地队列，按 `HUGE_ACTION` 处理 |
| `PROXY_QUEUE_HUGE_ACTION` | `cloud` | huge 处理：`cloud`（强制路由云端，需 `PROXY_ROUTE_ENABLED=true`）/ `reject`（413） |

### 11.6 诊断数据面参数（R13-R16，全部 reloadable）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_DIAG_ENABLED` | `true` | 诊断数据面总开关（头/尾注/台账/档案/落盘） |
| `PROXY_DIAG_SSE_TAIL` | `true` | 流式 SSE 尾注 `: x-proxy-diag`（关 = 纯头模式） |
| `PROXY_DIAG_SESSION_TTL_MIN` | `180` | 台账/档案会话 TTL（分钟） |
| `PROXY_DIAG_SESSION_MAX` | `64` | 内存台账会话数上限（FIFO 驱逐，驱逐后端点 410） |
| `PROXY_DIAG_ARCHIVE_ENABLED` | `true` | sent_view 常态每轮落盘（`logs/diag/archive/`） |
| `PROXY_DIAG_ARCHIVE_MAX_MB` | `200` | archive 磁盘总量上限（超限删最老会话文件） |
| `PROXY_DIAG_TIMINGS_SOURCE` | `auto` | prefill 数来源：`auto`（响应体 timings 探测，无则字段缺省）/ `off` |

队列分桶：interactive（<16K chars，最高优先级）→ standard → large → huge；同 bucket FIFO。响应头带 `X-Queue-Bucket/Position/Estimated-Wait-Ms/Wait-Ms`，状态见 `GET /api/queue`。

> 完整参数列表及详细文档见 [`proxy_config.py`](proxy_config.py) 的 `CONFIG_REGISTRY`。

---

## 12. 缺陷修复状态

> 完整缺陷清单见 [`docs/DEFECT-LIST.md`](docs/DEFECT-LIST.md)。以下为截至 2026-07-12 的汇总。

### 12.1 总体统计

| 类别 | 数量 | 占比 |
|------|------|------|
| ✅ 已修复 | 27 | 90% |
| 🟡 部分修复 | 1 | 3% |
| ⚪ 设计限制 | 2 | 7% |
| **合计** | **30** | **100%** |

### 12.2 按严重度

| 严重度 | 总数 | 已修复 |
|--------|------|--------|
| 🔴 P0-Critical | 7 | 7 |
| 🟠 P1-High | 8 | 8 |
| 🟡 P2-Medium | 10 | 8 |
| 🔵 P3-Low | 5 | 4 |

### 12.3 生产验证结果

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 500 错误率 | 2.3% | **0%** (0/20) |
| 503 错误率 | 4.5% | **0%** (0/20) |
| 成功率 | 93.2% | **100%** (20/20) |
| 单元测试 | 871 | **890** |
| 缺陷修复率 | 70% | **90%** |

### 12.4 关键修复清单

| 缺陷 | 描述 | 修复要点 |
|------|------|----------|
| DEF-001 | 500 错误率 2.3% | `do_GET` 异常处理、pipeline stage 日志、local fallback HTTPError 捕获 |
| DEF-002/109 | 循环检测假阳性 | `loop_injected` 统计口径从 `max_run≥3` 改为 `level≥1`；长上下文分层阈值 |
| DEF-003 | re_read_rate 公式 bug | 修复分母为 0 处理，新增 5 个单元测试 |
| DEF-101 | 503 错误率 4.5% | `_get_system_memory` 改用 `memory_pressure` 替代 `vm_stat` |
| DEF-104 | 白名单自动扩展 | `_SESSION_TOOL_FREQ` 跨请求频率计数，≥3 次自动晋升 |
| DEF-107 | high_drop_ratio 21.6% | fifo 截断时注入结构化摘要 |
| DEF-203 | prefix cache 断裂 | 规范填充工具列表，跨 session 工具序列一致 |
| DEF-207 | watchdog daemon | `--daemon` 参数 + PID 文件 + `disown` + `cmd_stop_watchdog` |
| DEF-208/209 | 测试覆盖 | 新增 `test_truncation_edge.py` 19 个测试 |
| DEF-210 | 文档同步 | fifo 策略文档、覆盖率表缺陷列、AGENTS.md 参数推荐 |
| DEF-303 | 日志分级 | 15 处关键 `log()` 添加 ERROR/WARN 级别 |
| DEF-304 | 可观测性仪表板 | Chart.js 趋势图、TTFT 追踪、metrics 轮转、`/metrics/history` 端点 |

---

> 本文件上一版本保存在 `AGENTS.md.bak`。
