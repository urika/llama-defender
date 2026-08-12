# llama-defender 集成需求（agent_go → 服务方）

> 状态：已实现（Phase 1–3，2026-08-12）。R1–R7 全部交付，agent_go 可依赖以下接口。
> 变更任何端点字段、退出码或文件路径前需通知 agent_go 侧并做兼容性处理。
> 关联：[local-model-management-design.md](local-model-management-design.md)（agent_go 侧设计）
> 目标项目：`/Users/jinsongwang/APP/llama.cpp`（llama-defender）
> 背景：agent_go 将本地模型纳入管理（启停/切换/状态监控/保活分工）。按「谁拥有进程谁保活」原则，agent_go 作为消费方只负责就绪检查与一次性修复触发；本文档列出需要 llama-defender（服务方）提供或增强的接口与功能契约。

## 1. 需求场景

| # | 场景 | agent_go 行为 | 需要的 llama-defender 能力 |
|---|---|---|---|
| S1 | 任务启动前就绪检查 | pipeline pre-flight 探测后端是否可用 | 快速健康探针 + 结构化状态 |
| S2 | Plan 阶段模型感知 | generate_plan 前取当前模型名/能力档注入 planner 上下文 | 结构化输出当前激活模型与后端类型 |
| S3 | 故障诊断分级 | 任务失败后区分 proxy 死/backend 死/模型漂移/加载中 | 分级状态字段（而非 HTML 解析） |
| S4 | 一次性修复触发 | pre-flight 发现问题时触发 reload/start-backend/restart | 幂等命令 + 可靠退出码 + 可轮询的就绪信号 |
| S5 | 模型切换 | 编排 switch→stop-backend→reload→start-backend 原子序列 | 非交互保证 + 失败可回滚的 ground truth |
| S6 | 并发保护 | agent_go 修复与 llama-defender watchdog 恢复可能并发 | 变更操作的互斥锁 / 单入口序列化 |
| S7 | 状态监控展示 | status 面板 / web 页面展示后端健康与指标 | 稳定的 JSON metrics 接口 |

## 2. 已有接口（可直接使用，需保持稳定）

以下能力已验证存在，agent_go 侧直接使用；**请保持行为与输出稳定，变更需通知**：

| 接口 | 用途 | 现状 |
|---|---|---|
| `GET /v1/models` | 健康探针（S1） | ✅ 200 + 别名/路由 metadata |
| `GET /api/status` | 结构化健康/就绪状态（S1/S2/S3） | ✅ JSON；`state`/`ready` 字段；非健康状态返回 503 |
| `GET /api/watchdog` | watchdog 运行状态（S3/S6） | ✅ JSON：enabled/running/restart_count_1h/... |
| `GET /api/profiles` | 可用配置列表（S2/S5） | ✅ JSON：`name`/`desc`/`memory_gb`/`active` |
| `GET /metrics[?n=N]` | 监控指标（S7） | ✅ JSON |
| `manage.sh start / start-backend` | 启动（S4） | ✅ 幂等；触发 `service_start` 生命周期事件 |
| `manage.sh reload`（SIGHUP） | 热重载（S4/S5） | ✅ 幂等，~0.5s；触发 `config_reload` 事件 |
| `manage.sh switch <name>` | 切换软链（S5） | ✅ 非交互自动跳过确认；触发 `profile_switch` 事件 |
| `manage.sh stop / stop-backend / restart` | 停止/重启（S4/S5） | ✅ 可用，有副作用；触发 `service_stop`/`service_restart` 事件 |
| `manage.sh watchdog-status` | watchdog 结构化状态（S3/S6） | ✅ JSON 输出，等价于 `GET /api/watchdog` |
| `configs/active.conf` 软链 | 当前 profile ground truth（S2/S5） | ✅ 文件级可信 |
| `logs/watchdog_state.json` | watchdog 持久化状态 | ✅ 由 watchdog 启动/停止/自动重启时更新 |
| `logs/lifecycle_events.jsonl` | 生命周期事件流（S7） | ✅ 每行 `{ts, event, detail}` |
| `*.pid` + pgrep 自愈 | 进程 ground truth（S3） | ✅ |

## 3. 需新增/增强的接口（gap）

> 实现状态标记：`✅ 已完成` | `🚧 进行中` | `⏳ 未开始`

### R1（P0）：结构化状态 JSON 端点 ✅

**现状缺口**：`GET /status` 返回 HTML，agent_go 需解析 HTML 取 `MODEL_NAME`，脆弱易碎。

**需求**：新增 `GET /api/status`（或 `GET /status?format=json`），返回：

```json
{
  "proxy":   {"pid": 12345, "uptime_sec": 3600, "alive": true},
  "backend": {"pid": 23456, "alive": true, "model_name": "Qwen3.6-35B-A3B-UD-MLX-4bit",
              "backend_type": "rapid-mlx", "base_url": "http://127.0.0.1:8081/v1"},
  "active_profile": "rapid-mlx-35b-opt",
  "state": "healthy",
  "ready": true
}
```

字段要求：
- `state` 枚举：`healthy | starting | backend_down | proxy_down | model_drift | down`（对应 S3 诊断分级）。
- `model_name` 为后端**真实加载**的模型名（S2 感知与计量映射依赖）。
- `active_profile` 为 active.conf 软链目标名（S5 漂移检测依赖：`model_name` 对应配置 ≠ `active_profile` → `model_drift`）。
- 响应 < 1s，失败返回非 200 + JSON error body。

### R2（P0）：明确的就绪（readiness）语义 ✅

**现状缺口**：`/v1/models` 返回 200 不代表模型加载完成（35B 加载需数十秒）。

**需求**：R1 的 `ready` 字段语义固定为「模型加载完成且可接受推理请求」；`starting` 状态下 `ready=false`。agent_go 的 `wait_ready` 轮询以该字段为准，不再自行猜测。

### R3（P0）：manage.sh 调用契约 ✅

**需求**（多为现状确认+固化）：
1. **非交互保证**：stdin 非 tty 时，所有命令（含 `switch`/`restart`/`stop`）不得阻塞等待输入。
2. **退出码**：成功=0，失败=非 0；失败原因写 stderr。
3. **幂等**：`start`/`start-backend`/`reload`/`switch` 重复调用结果一致。
4. **超时上限**：命令自身应有硬超时（参考现有 `WAIT_HARD_LIMIT=1800`），不得无限挂起。

### R4（P1）：变更操作互斥锁 ✅

**现状缺口**：agent_go pre-flight repair 与 llama-defender watchdog 自动重启可能并发触发（S6）。

**需求**：manage.sh 的变更类命令（`start/stop/restart/start-backend/stop-backend/switch`）进入时获取仓库级文件锁（如 `.manage.lock`，flock），已持锁则快速失败（非 0 + stderr 说明锁持有者）。watchdog 内部操作同样走该锁。

### R5（P1）：watchdog 状态查询 ✅

**需求**：`manage.sh watchdog-status`（或 HTTP 端点）输出结构化信息：`enabled / last_restart_at / restart_count_1h / last_failure_reason`。agent_go 诊断时只读该状态，用于判断「后端是否正被 watchdog 恢复中」（避免重复修复）。

**实现**：`manage.sh watchdog-status` 输出合法 JSON；代理提供 `GET /api/watchdog`；状态持久化到 `logs/watchdog_state.json`。

### R6（P1）：profile 列表端点（可选增强） ✅

**现状**：agent_go 解析 `configs/*.conf` + active.conf 软链（可用但耦合目录结构）。

**需求（可选）**：`GET /api/profiles` 返回 `[{"name": "...", "desc": "...", "memory_gb": ..., "active": true}]`。若实现，agent_go 改走 HTTP；不实现则维持文件解析（可接受）。

**实现**：代理提供 `GET /api/profiles`，从 `configs/*.conf` 解析 `CONFIG_NAME` / `CONFIG_DESC` / `CONFIG_MEMORY`，`active=true` 标记与 `configs/active.conf` 软链目标一致；`memory_gb` 对无法解析的云端配置返回 `null`。

### R7（P2）：事件通知（可选） ✅

**需求（可选）**：模型切换完成 / 自动重启发生时写 `logs/lifecycle_events.jsonl`（每行 `{ts, event, detail}`），agent_go 监控页可消费。轮询 status 已可满足，此条非必需。

**实现**：`manage.sh` 在 `service_start` / `service_stop` / `service_restart` / `config_reload` / `profile_switch` / `watchdog_auto_restart` 等事件发生时追加 JSONL 到 `logs/lifecycle_events.jsonl`；`LIFECYCLE_EVENTS_PATH` 可被环境变量覆盖以便测试。

## 4. 接口协议要求汇总

| 维度 | 要求 |
|---|---|
| 传输 | HTTP on `127.0.0.1:4000`，无鉴权（本机回环） |
| 格式 | 程序化端点返回 `application/json`；字段名为 snake_case 且**稳定不更名**（更名=breaking change 需通知 agent_go） |
| 错误 | 非 2xx + JSON `{"error": "...", "state": "..."}`；agent_go 按 state 归因 |
| 超时 | 探测类端点响应 < 2s；status 类 < 1s；manage.sh 变更命令有硬上限（≤1800s） |
| 幂等 | start/start-backend/reload/switch 幂等；stop 类幂等收尾 |
| 并发 | 变更操作必须经文件锁互斥（R4）；只读端点无锁 |
| 版本 | 建议响应含 `api_version`（如 `"1"`），便于后续演进 |
| 降级 | 端点不存在/字段缺失时 agent_go 侧 fail-open（回退 HTML 解析或标记 unknown），不阻断任务 |

## 5. 优先级与验收

| 需求 | 优先级 | agent_go 侧验收 |
|---|---|---|
| R1 结构化状态 | P0 | ✅ `GET /api/status` 可用；`agent_go model status/diagnose` 全部走 JSON，不再解析 HTML |
| R2 readiness 语义 | P0 | ✅ `/api/status` 的 `ready` 字段语义固定为模型加载完成 |
| R3 manage.sh 契约 | P0 | ✅ 非交互调用全部命令无阻塞；失败退出码非 0 |
| R4 互斥锁 | P1 | ✅ `.manage.lock` 文件锁；watchdog 恢复中 agent_go repair 快速失败并提示，不双重重启 |
| R5 watchdog 状态 | P1 | ✅ `GET /api/watchdog` + `manage.sh watchdog-status` 输出结构化 JSON |
| R6 profile 端点 | P1（可选） | ✅ `GET /api/profiles` 可用；`model list` 改走 HTTP |
| R7 事件通知 | P2（可选） | ✅ `logs/lifecycle_events.jsonl` 记录切换/重启/自动恢复事件 |

## 6. 兼容策略

- R1/R2 未实现前，agent_go 维持现有 HTML 解析 + pidfile 兜底，功能可用但脆弱。
- 全部需求按「新增不破坏」原则：llama-defender 新增端点/字段，不改变现有 `/v1/models`、`/metrics`、manage.sh 既有行为。
- agent_go 侧对每个需求做存在性探测，缺失则降级（fail-open），保证旧版 llama-defender 可继续工作。
