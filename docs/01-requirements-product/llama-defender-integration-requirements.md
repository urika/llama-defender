# llama-defender 集成需求（agent_go → 服务方）

> 状态：需求稿 v2（2026-08-15 更新：并入模型实体三层设计的接口契约，补充 R8-R12；2026-08-19 补充 R13-R16 上下文工程诊断数据面，R8-R12 已全部交付）
> 关联：`local-model-management-design.md`（agent_go 侧设计）、`model-entity-config-design.md`（agent_go 侧设计）（模型实体三层配置设计）
> 目标项目：`/Users/jinsongwang/APP/llama.cpp`（llama-defender）
> 背景：agent_go 将本地模型纳入管理（启停/切换/状态监控/保活分工）。按「谁拥有进程谁保活」原则，agent_go 作为消费方只负责就绪检查与一次性修复触发；本文档列出需要 llama-defender（服务方）提供或增强的接口与功能契约。
> v2 增补：按模型实体三层设计（① 模型固有 / ② 角色绑定 / ③ 部署拓扑），③ 部署拓扑归代理侧，需代理提供**部署可视**与**路由归因**接口（R8-R12），支撑 agent_go 侧 ① registry 数据采集与计量归因。

> **定位澄清（2026-08-15，与 `model-entity-config-design.md §3.1`（agent_go 侧设计）互引）**：本地模型经代理有**三重价值**——托管（进程/GPU/生命周期）+ 智能路由（本地↔云端分流、超长转云）+ 报文压缩（上下文压缩到本地可用范围）。**非所有流量必须走代理**：Anthropic 兼容 + 上下文充足 + 云端模型（如 glm-5.3）可**直连**，仅本地模型/非 Anthropic 协议/需压缩或智能分流时走代理。走代理 vs 直连是 agent_go 侧路由决策（消费方），不影响本文档接口需求；R8-R12 仅在"走代理"路径上需要。

## 0. 待办清单速览（实施视图：已完成 / 待做 / 顺序）

### 0.1 已完成（保留，勿回退）

| 能力 | 说明 |
|------|------|
| 双协议端点 | `POST /v1/messages`（Anthropic）+ `POST /v1/chat/completions`（OpenAI） |
| thinking/response_format 透传 | `FormatConverter` + `convert_openai_request_to_anthropic` 已修复透传（v4-pro 推理必需，b14 修复） |
| SmartRouter 智能路由 | 模型偏好（MODEL_ROUTE_PREFERENCES 三模式）+ 阈值/内存/会话/sticky + 云端熔断冷却回退 |
| manage.sh 生命周期 | `start/stop/status/restart/reload/switch/watchdog`（pidfile + 日志 + 热重载 SIGHUP） |
| 基础接口 | `GET /v1/models`、`GET /status`(HTML)、`GET /api/status`(JSON)、`GET /metrics`、`GET /api/profiles`、`GET /api/watchdog`、`POST /admin/route/force-local`、`POST /admin/route/force-cloud` |
| **R8-R12 路由归因与部署可视** | **2026-08-15 全部交付**（模型目录 Phase B/C，commit b35b608/后续）。**R8** ✅ 四头 `X-Proxy-Route-Target(cloud\|local\|local_forced)/Actual-Model/Reason/Cost`（Cost 为预估，OpenAI 协议非流式响应体另带 `proxy_route` 实际 usage 计费字段）；**R9** ✅ `GET /api/route/policies`：providers（key 只回 `key_set` 布尔）+ models（tier/price/capabilities/direct_capable）+ preferences + defaults + `catalog_hash`（漂移检测）+ `api_version`；**R10** ✅ `/v1/models` metadata 增 `real_model/thinking_supported/thinking_required/json_compliance/context_chars/price/direct_capable/fallback_models`；**R11** ✅ `/api/status` 增 `route_config{route_enabled,cloud_model,cloud_key_set,cloud_concurrent}`；**R12** ✅ `POST /admin/reload`（幂等，等效 SIGHUP，含 configs/models.json 目录重载）；CLI 配套：`./manage.sh models` / `models-validate` |
| **R13-R16 上下文工程诊断数据面** | **2026-08-19 全部交付**（设计文档 [../02-architecture-design/diagnostics-dataplane-design-20260819.md](../02-architecture-design/diagnostics-dataplane-design-20260819.md)）。**R13** ✅ 诊断归因双通道：非流式头 `X-Proxy-Diag-Request-Id` / `X-Proxy-Feedback-Injected`（7 处既有注入全部可计量）/ `X-Proxy-Prompt-Processed-N`（后端返回 timings 时），流式 SSE 尾注 `: x-proxy-diag {...}`；**R14** ✅ 台账 `GET /api/session/<key>/ledger`（dup/last_dup_turn/材料清单，增量扫描 + canonical_mismatch 重建）+ `GET /api/sessions` 发现端点；**R15** ✅ sent_view 档案 `GET /api/session/<key>/archive?view=sent`（每轮最终 payload 落盘，MB 上限/TTL）；**R16** ✅ `logs/diag/sessions.jsonl` per-turn 深度记录（request_id 关联、hit_ratio、is_epoch_turn 预留）+ `GET /api/session/<key>/metrics` 聚合 + `/metrics/history?session=` + `/api/status` `ctx_config` 段 + `/api/backend/props\|slots` 反代 + `lifecycle_events.jsonl` 激活（R7 兑现）；总开关 `PROXY_DIAG_ENABLED`（`PROXY_DIAG_*` 家族 SIGHUP 可热更） |

### 0.2 待做（当前无）

R1-R16 已全部交付。上下文工程本体（canonical history / epoch 状态机 / 写入期压缩 / 复述块 / 合成负反馈）见 [../02-architecture-design/llama-defender-context-engineering-design.md](../02-architecture-design/llama-defender-context-engineering-design.md)——落地后点亮 `X-Proxy-Epoch-Count` 头、`is_epoch_turn` 分档字段与 archive `canonical` 视图（接口字段名均已预留）。

### 0.3 实施顺序

R13-R16 已于 2026-08-19 按 `timings 透传 → R13 → R16 → R14 → R15` 顺序全部完成（D-Phase 0-2 合并交付，见设计文档 §11）。

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
| `GET /metrics[?n=N]` | 监控指标（S7） | ✅ JSON |
| `manage.sh start / start-backend` | 启动（S4） | ✅ 幂等 |
| `manage.sh reload`（SIGHUP） | 热重载（S4/S5） | ✅ 幂等，~0.5s |
| `manage.sh switch <name>` | 切换软链（S5） | ✅ 非交互自动跳过确认 |
| `manage.sh stop / stop-backend / restart` | 停止/重启（S4/S5） | ✅ 可用，有副作用 |
| `configs/active.conf` 软链 | 当前 profile ground truth（S2/S5） | ✅ 文件级可信 |
| `*.pid` + pgrep 自愈 | 进程 ground truth（S3） | ✅ |
| `GET /api/status` | 结构化状态（R1，**已实现**） | ✅ JSON：proxy/backend/active_profile/state |
| `GET /api/profiles` | profile 列表（R6，**已实现**） | ✅ JSON |
| `GET /api/watchdog` | watchdog 状态（R5，**已实现**） | ✅ JSON |
| `GET /metrics/history` | 历史指标（S7） | ✅ JSON |
| `POST /admin/route/force-local` / `force-cloud` | 手动路由覆盖（会话级） | ✅ |

## 3. 需新增/增强的接口（gap）

### R1（P0）：结构化状态 JSON 端点

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

### R2（P0）：明确的就绪（readiness）语义

**现状缺口**：`/v1/models` 返回 200 不代表模型加载完成（35B 加载需数十秒）。

**需求**：R1 的 `ready` 字段语义固定为「模型加载完成且可接受推理请求」；`starting` 状态下 `ready=false`。agent_go 的 `wait_ready` 轮询以该字段为准，不再自行猜测。

### R3（P0）：manage.sh 调用契约

**需求**（多为现状确认+固化）：
1. **非交互保证**：stdin 非 tty 时，所有命令（含 `switch`/`restart`/`stop`）不得阻塞等待输入。
2. **退出码**：成功=0，失败=非 0；失败原因写 stderr。
3. **幂等**：`start`/`start-backend`/`reload`/`switch` 重复调用结果一致。
4. **超时上限**：命令自身应有硬超时（参考现有 `WAIT_HARD_LIMIT=1800`），不得无限挂起。

### R3.1 manage.sh CLI 命令参考（服务启停主路径）

manage.sh 是**服务启停的主路径**，尤其在 HTTP API 生效前或代理不可用时（proxy_down 场景下 `/api/*` 全部不可达，CLI 是唯一控制面）。工作目录：`/Users/jinsongwang/APP/llama.cpp`。

**只读命令（任意频率，零副作用）**：

| 命令 | 说明 |
|------|------|
| `status` | 文本状态：后端/代理存活、PID、内存、当前配置（人类可读） |
| `current` | 当前激活配置详情 |
| `list` | 列出所有可用 profile |
| `watchdog-status` | watchdog 结构化状态 JSON（R5） |
| `logs [N]` | 后端日志 tail |
| `proxy-logs [N]` | 代理日志 tail |

**幂等启动/恢复命令（重复调用安全）**：

| 命令 | 说明 | 场景 |
|------|------|------|
| `start` | 启动后端+代理；已运行则跳过/补启动 | 服务全停后拉起（S4） |
| `start-backend` | 仅启动本地后端 | backend_down 修复（S4 阶梯 level 1） |
| `reload` | SIGHUP 热重载代理配置（~0.5s，不断连） | 配置漂移修复（S4 阶梯 level 0） |
| `switch <name>` | 改 active.conf 软链（非交互自动跳过确认） | 切换序列第一步（S5） |

**变更/停止命令（有副作用，需并发保护）**：

| 命令 | 说明 | 注意 |
|------|------|------|
| `stop-backend` | 停本地后端，释放 GPU 内存 | 在途请求失败；执行前查活跃任务 |
| `stop` | 停 watchdog+代理+后端 | 同上 |
| `restart` | stop + start（含模型重新加载，35B 需数十秒） | S4 阶梯 level 2，重操作 |
| `start-cloud` | 仅启云端代理 | **会先停本地后端**，勿当纯加云端用 |
| `watchdog [--daemon]` / `stop-watchdog` | 启停 watchdog | 保活归服务方，agent_go 不调用 |

**调用约定**（与 R3 契约一致）：工作目录必须为 llama-defender 仓库根目录；`subprocess.run` 收集退出码（0=成功）；非 tty stdin 下不得等待输入；变更命令自动获取 `.manage.lock`（R4），持锁冲突时快速失败。

### R4（P1）：变更操作互斥锁

**现状缺口**：agent_go pre-flight repair 与 llama-defender watchdog 自动重启可能并发触发（S6）。

**需求**：manage.sh 的变更类命令（`start/stop/restart/start-backend/stop-backend/switch`）进入时获取仓库级文件锁（如 `.manage.lock`，flock），已持锁则快速失败（非 0 + stderr 说明锁持有者）。watchdog 内部操作同样走该锁。

### R5（P1）：watchdog 状态查询

**需求**：`manage.sh watchdog-status`（或 HTTP 端点）输出结构化信息：`enabled / last_restart_at / restart_count_1h / last_failure_reason`。agent_go 诊断时只读该状态，用于判断「后端是否正被 watchdog 恢复中」（避免重复修复）。

### R6（P1）：profile 列表端点（可选增强）

**现状**：agent_go 解析 `configs/*.conf` + active.conf 软链（可用但耦合目录结构）。

**需求（可选）**：`GET /api/profiles` 返回 `[{"name": "...", "desc": "...", "memory_gb": ..., "active": true}]`。若实现，agent_go 改走 HTTP；不实现则维持文件解析（可接受）。

### R7（P2）：事件通知（可选）

**需求（可选）**：模型切换完成 / 自动重启发生时写 `logs/lifecycle_events.jsonl`（每行 `{ts, event, detail}`），agent_go 监控页可消费。轮询 status 已可满足，此条非必需。

---

## 3.1 模型实体三层设计增补需求（R8-R12，2026-08-15）

以下需求来自 `model-entity-config-design.md`（agent_go 侧设计） 的三层设计：③ 部署拓扑归代理侧，需代理提供**部署可视**与**路由归因**接口，支撑 agent_go 侧 ① 模型 registry 数据采集与计量归因。

### R8（P0）：路由归因返回（响应头/字段）

**现状缺口（G2）**：agent_go metering 按 URL（localhost）标 `is_local=True`，但代理 force_fallback 时 opus-4-7 有 ~36% 概率回退本地——**代理知道实际路由（cloud/local）与真实模型，但不回传**，导致计量归因全错（云端调用被记为本地、成本错算）。

**需求**：每个推理响应（`/v1/messages`、`/v1/chat/completions`）带路由归因：

| 方式 | 字段 | 说明 |
|---|---|---|
| 响应头（推荐，流式兼容） | `X-Proxy-Route-Target: cloud\|local\|local_forced` | 实际路由 |
| | `X-Proxy-Route-Actual-Model: deepseek-v4-pro` | 真实后端模型名 |
| | `X-Proxy-Route-Reason: model_forced_fallback_cloud` | 路由原因 |
| | `X-Proxy-Route-Cost: 0.0002` | 本次云端费用（本地为 0） |
| 或响应体扩展字段（非流式） | `"proxy_route": {"target","actual_model","reason","cost"}` | 同上 |

流式响应在 `message_start` / 首帧携带。agent_go metering 据此标 `is_local`（target=local 才是本地）与实际模型，修正成本归因。

### R9（P0）：路由策略可视端点 `GET /api/route/policies`

**现状缺口（G3）**：`MODEL_ROUTE_PREFERENCES`（模型→本地/混合/云端三模式路由表）对 agent_go 不可见——agent_go 无法预知某模型会走哪、cloud_model 是什么、云端 key 是否就绪。

**需求**：返回脱敏后的路由配置：

```json
{
  "route_enabled": true,
  "cloud_model": "deepseek-v4-pro",
  "cloud_key_set": true,
  "threshold_chars": 80000,
  "preferences": {
    "claude-haiku-4-5": {"route_bias": "prefer_local", "behavior": "prefer", "cloud_model": "deepseek-v4-flash"},
    "claude-sonnet-4-6": {"route_bias": "auto", "behavior": "prefer", "cloud_model": "deepseek-v4-flash"},
    "claude-opus-4-7": {"route_bias": "prefer_cloud", "behavior": "force_fallback", "cloud_model": "deepseek-v4-pro"}
  }
}
```

`cloud_key_set` 仅布尔（**不返回 key 明文**）。agent_go 健康检查/配置中心据此展示「该模型实际会走本地还是云端」，替代当前盲猜。

### R10（P1）：`/v1/models` 能力元数据增强

**现状缺口（G1）**：`/v1/models` 只回别名 + route，无能力元数据——agent_go 的 ① 模型 registry 需手工录入 thinking/json_compliance/context 等固有属性，无法自动采集。

**需求**：每个模型条目增强 metadata：

```json
{"id": "claude-opus-4-7", "object": "model",
 "metadata": {"route": "cloud", "real_model": "deepseek-v4-pro",
              "thinking_supported": true, "thinking_required": true,
              "json_compliance": "loose", "context_chars": 200000}}
```

字段：`route`（已有）、`real_model`、`thinking_supported/required`、`json_compliance`（strict/loose/poor）、`context_chars`。agent_go registry 可定时同步，替代手工维护。

### R11（P1）：`/api/status` 路由配置段增强

**现状缺口（G4）**：健康检查的模型名探测依据不一致（/status HTML vs /v1/models 别名），且不知云端配置状态。

**需求**：`/api/status` 增加 `route_config` 段：`{"cloud_model": "...", "route_enabled": bool, "cloud_key_set": bool, "cloud_concurrent": n}`。与 R9 互补（R9 全量策略，R11 当前状态摘要）。

### R12（P2）：HTTP 热重载 `POST /admin/reload`

**现状**：热重载仅 `manage.sh reload`（CLI，需到仓库目录执行）。proxy_down 以外的远程/容器场景无 HTTP 路径。

**需求（可选）**：`POST /admin/reload` 等效 SIGHUP 热重载（读 active.conf 应用变更），返回 `{reloaded: true, active_profile: "..."}`。幂等。

### 边界（不需代理提供，agent_go 侧职责）

- ① 逻辑模型 registry：`quality_tags`、pricing 表、角色绑定（router.roles）——agent_go `models.json`/pricing.py
- ② 角色场景参数：temperature/max_tokens/thinking 开关/goal/min_difficulty——agent_go config
- Plan 生成/拆解/e2e 判定——agent_go 核心流程

## 3.2 上下文工程诊断数据面增补需求（R13-R16，2026-08-19）

以下需求来自 [../02-architecture-design/llama-defender-context-engineering-design.md §10](../02-architecture-design/llama-defender-context-engineering-design.md)：上下文工程（压缩/epoch/feedback 注入）落地后，缓存命中率、延迟分档、会话观测、压缩后行为复盘均无数据源。**诊断数据采集责任全部归代理，agent_go 只消费结构化接口**（响应头/端点/jsonl）。完备设计与接口契约见 [../02-architecture-design/diagnostics-dataplane-design-20260819.md](../02-architecture-design/diagnostics-dataplane-design-20260819.md)。

### R13（P1）：诊断归因返回（非流式 HTTP 头 + 流式 SSE 尾注双通道）

> 2026-08-19 修订：原「复用 R8 头模式」表述仅在**非流式**成立——流式响应头先于任何后端数据发出，而 `Prompt-Processed-N` 要等 prefill 完成后的终块 `timings` 才可知，物理上不可能作为流式 HTTP 头携带。流式经 SSE 尾注携带，见下方格式。

| 字段 | 说明 | 通道 |
|---|---|---|
| `X-Proxy-Prompt-Processed-N` | 本轮实算 prefill 数（缓存命中率分子）；后端返回 timings 时才有，**否则缺省，不发假值** | 非流式头；流式尾注 |
| `X-Proxy-Epoch-Count` | 会话累计 epoch（截断/重构）触发次数（上下文工程 Phase 1 落地后出现，此前不发送） | 头 + 尾注 |
| `X-Proxy-Feedback-Injected` | 本请求代理注入的全部合成内容 kind（`loop_l1/loop_l2/loop_l3/text_loop/blocker/reread_hard/route_notice/high_drop_notice/truncation_summary`；Phase 2 后增 `negative_feedback`/`recitation`） | 头 + 尾注 |
| `X-Proxy-Diag-Request-Id` | 诊断关联键，与 R16 jsonl 及 `proxy_metrics.jsonl` 的 `request_id` 字段对齐（metering ↔ 会话指标互查） | 头 + 尾注 |

**流式尾注格式**（SSE 注释行，规范保证所有解析器忽略；在 `message_stop`（Anthropic）/ `data: [DONE]`（OpenAI）之前插入）：

```text
: x-proxy-diag {"request_id":"req_...","session_key":"a1b2c3d4","prompt_processed_n":412,"prompt_sent_n":98347,"hit_ratio":0.9958,"epoch_count":3,"feedback_injected":["loop_l1"]}
```

**L3 接入补全（2026-08-19 G 系列补丁）**：
- 尾注与载荷**恒含 `session_key`**（代理侧实际归并的 8 字符 key）——metering 归因落会话无需自行实现截断。
- **OpenAI 协议非流式**响应体带 `proxy_diag` 字段（与 `proxy_route` 并列，本地/云端路由均注入）。
- `api_version` 升为 **`"2"`**（`/api/status`、`/admin/reload`、`/api/route/policies`）——agent_go 存在性探测以版本区分新旧代理（本节 §4 fail-open 的前提）。

agent_go metering 采集（`api.py:156` R8 解析模式扩展，见本节边界）→ metering.jsonl 字段 → `eval.py` analyze 可查。

### R14（P1）：会话台账端点

`GET /api/session/<key>/ledger`：dup / last_dup_turn / 材料清单（Phase 2 已设计项）。支撑 agent_go 轮级看门狗（§9 P1-4）+ 无效轮占比一等指标。

**会话发现与 turn 语义**（2026-08-19 补）：
- `GET /api/sessions` 返回活跃会话列表（`key / key_source / turns / last_seen / route / hit_ratio_p90 / evict_in_min`），供 agent_go/harness 枚举 `<key>`。
- **turn = 代理所见该会话的请求序号**（一次请求内的多工具调用同 turn）——轮级看门狗与台账轮次以此对齐。
- 会话 key 契约：优先请求头 `X-Claude-Code-Session-Id`（内部截断 8 字符）；无头时回退 `md5(ip:ua:date)` 会**按天合并所有无头会话**——批跑 harness 必须显式发送该头。
- **端点接受完整会话头值**（G-D）：`<key>` 传完整 id 时，精确未命中且其 8 字符截断形式有台账/档案则自动归并——harness 无需自行截断。

### R15（P2）：L4 档案查询

L4 只读访问 `GET /api/session/<key>/archive`——从 Phase 3 降级形态**提前**与 Phase 2 同期。压缩后行为复盘必须以代理档案为准（视角正确性：模型实际所见 ≠ 客户端所发）；批跑形态学分析（兔子洞）的权威数据源。支持 `?view=sent|client|canonical`（默认 `sent`，即实际发给后端的最终 payload，含注入块标注；`canonical` 视图 Phase 1 前返回 501）。

### R16（P1）：/metrics 会话维度扩展

每轮结构化落盘 jsonl（`session_key / turn / request_id / sent_tokens / processed_tokens / hit_ratio / epoch 触发 / is_epoch_turn（延迟分档前提）/ 注入标记 / canonical_mismatch`）+ 按 session 聚合时序（`GET /api/session/<key>/metrics`、`GET /metrics/history?session=`）。支撑时间线复盘、`canonical_mismatch` 监控、A/B 出数；与 `proxy_metrics.jsonl` 经 `request_id` 关联（per-request 全端点记录与 per-turn 深度记录并行，不合并 schema）。

**`/api/status` 增 `ctx_config` 段**（2026-08-19 补，仿 R11 `route_config` 先例）：`{compression_mode, feedback_injection_enabled, epoch_S, window_K, diag_enabled}`——bench manifest 口径标注（上下文工程设计 §9 P0-1）的机读数据源，S/K 在 Phase 1 前为 null。

### 附：llama-server 原生数据透传（零上游改动）

| 数据 | 原生位置 | 用途 |
|---|---|---|
| per-request `timings`（prompt_n / prompt_ms / predicted_*）+ `usage` | 响应体（版本需实测） | 每轮缓存命中率 = 1 − prompt_n / usage.prompt_tokens——Phase 0/1 核心指标来源 |
| `/props`（n_ctx / total_slots / model_path） | GET /props | slot/并发协调、epoch 触发阈值 ctx_max 同步 |
| `/slots` 实时状态 | GET /slots（需 `--slots` 启动） | 并发会话 vs slot 匹配监控 |

透传形态：`GET /api/backend/props` / `GET /api/backend/slots` 只读反代；后端不支持（如 rapid-mlx）时返回结构化 501 + `{"supported": false}`，消费方 fail-open。

### 边界（R13-R16 的 agent_go 侧职责）

- **metering 双来源解析**：`api.py:156` 的 R8 头解析扩展需同时支持 HTTP 头（非流式）与 SSE 注释行 `: x-proxy-diag {...}`（流式）——两个通道字段同名同义。
- **批跑 harness 必须显式发送 `X-Claude-Code-Session-Id`**（会话 key 契约，见 R14；无头回退 key 会按天合并会话，污染台账与档案）。
- 形态学复盘以 `archive?view=sent`（代理 sent_view）为准，不以 claude CLI 客户端转录为准（视角错位）。

## 3.3 信号/召回服务面增补需求（R17-R19，2026-09-02 **已冻结 FROZEN**）

> **状态与流程（contract-first，2026-09-02 双端约定，见 agent_go 反馈 §六）**：~~草案~~ → **agent_go 评审确认无异议（2026-09-02），契约冻结**——LD-1/LD-2/LD-3 据此动工；后续字段变更走 `CONTRACT_VERSION` 递增 + 双端漂移检测测试同步，不做静默变更。依据：`protocol-layer-ownership-review-20260902.md`（v1.2）§3.5 交互面 ⑤⑥、M3/M4/M5。

### R17（P1，草案）：会话信号快照端点 `GET /api/session/<key>/signals`

- **生产方**：代理（`ifc_metrics` / `hbe_probe` / `diagnostics` 聚合）；**消费方**：agent_go（P5 升级决策、AG-3 replan 决策表软依赖）、HealthGate 观测出口
- **响应体**：`signal_types.SignalSnapshot`（`contract_version=1`，全字段 Optional，fail-open——信号计算失败对应字段 null，不 500）：

| 字段 | 类型 | 来源 | 语义 |
|---|---|---|---|
| `contract_version` | int | `signal_types.CONTRACT_VERSION` | 契约版本，双端漂移检测键 |
| `h_be` / `h_be_trend` | float? | `hbe_probe`（top_logprobs 截断熵） | 信念熵及趋势；未采样轮 null |
| `d_ledger` | float? | `ifc_metrics.reconcile` | 探针答案 vs 台账 ground truth 偏差 |
| `retention` | float? | `ifc_metrics` 锚点差分 | 相邻轮发送视图保留率；首见会话 null |
| `rationale_ratio` | float? | `ifc_metrics` | 理由内容占比 |
| `action_diversity` | float? | `ifc_metrics`（bigram 熵） | 工具序列多样性；持续低位=坍缩/游走双端告警 |
| `reread_pressure` | int | `ifc_metrics` | 重读压力 |
| `ile` / `ile_kinds` | bool / list[str] | `ifc_metrics.infer_ile_kinds` | 信息损失事件及类别 |
| `view_reset` | bool | `ifc_metrics` | 视图重置标记 |
| `cognitive_load` | float | 预留（默认 0.0） | 认知负荷 |
| `config_fingerprint` | str | proxy_state | 配置指纹（双端对齐） |
| `session_key` / `turn` | str / int | 诊断上下文 | 关联键（与 R14/R16 同源） |

- **降级语义**：会话未知 → 404；已驱逐 → 410（对齐 R14/R15 先例）。
- **验收**：全字段 JSON 可得；数值与 `logs/diag/sessions.jsonl` 末轮 `ifc` 段抽样一致。

### R18（P1，草案）：任务上下文证据包 `POST /api/task-context`

- **方向**：agent_go → 代理；**语义封装原则**（review §3.6）：对外暴露能力（"给任务描述，拿上下文证据"），`recall`/`manifest`/`orig` 机制面降级 admin/debug（经 agent_go 核对 `diag.py` 不消费，无影响）
- **请求**：

```json
{
  "task_descriptor": {
    "description": "修复 auth 模块登录超时",
    "input_files": ["src/auth.py"],
    "keywords": ["登录", "timeout"]
  },
  "session_key": "k8chars",
  "budget_chars": 6000
}
```

  `session_key` 可选（缺省=仅做跨会话检索，不做本会话台账/archive 关联）；`budget_chars` 可选（默认 6000，上限 20000）。
- **响应**：

```json
{
  "bundle_id": "b-xxxx",
  "items": [
    {
      "unit_id": "u-...",
      "source": "manifest|archive|semantic",
      "trigger_why": "命中关键词: 登录/timeout",
      "preview_chars": 800,
      "content": "...",
      "full_available": true
    }
  ],
  "total_chars": 4200,
  "budget_remaining": 1800
}
```

  `unit_id` 为 manifest 单元锚点（二跳地址，取全量走既有 R15 admin 面 `?include_payload=true`，不新增端点）；`trigger_why` 为索引行触发语义（description 工程，skill 借鉴 §3.1）；`full_available=false` = 优雅缺页（仅索引）；`source: "semantic"` 为 SEM 语义卡预留扩展点。
- **降级语义**：无命中 → 200 + 空 `items`（不 404）；存储故障 → 503 + `Retry-After`。
- **验收**：AG-4 对冻结契约开发可直连 mock；`budget_chars` 裁剪生效。

### R19（P2，草案）：上下文钉扎请求头 `X-Proxy-Pin-Context`

- **方向**：agent_go → 代理（reload 重试请求携带）；**语义**：pinned 锚点在压缩/截断 stage 跳过（review M5）
- **格式**：`X-Proxy-Pin-Context: <unit_id>[,<unit_id>...]`（UTF-8，总长 ≤2KB）
- **三约束**（v1.1 M5，2026-09-02 措辞修正：存储层记账 + 注入点 stage 强制）：
  1. **预算**：pin 总量 ≤5% 上下文（`memory-storage-requirements-selection` §E），超限按 LRU 降级最旧 pin，回响应头 `X-Proxy-Pin-Demoted: <unit_id>`；
  2. **OOM 语义**：`OOMSafetyFIFO`（stage 17）默认**不豁免** pinned 锚点——oom_danger 档 pin 挂起，回 `X-Proxy-Pin-Suspended: oom_danger`（豁免与否为 Phase 0 显式决策项，此为草案默认）；
  3. **append-only**：禁注历史区，仅工作集/尾部。
- **总开关**：`PROXY_PIN_ENABLED`（默认 false，reloadable）——关闭时请求头被忽略并回 `X-Proxy-Pin-Disabled: true`。
- **验收**：pin 后多轮对话目标内容不被 fifo 驱逐；超预算降级可观测；开关关闭时零行为。

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
| R1 结构化状态 | P0 | `agent_go model status/diagnose` 全部走 JSON，不再解析 HTML |
| R2 readiness 语义 | P0 | 模型加载期 `ready=false` 时 pre-flight 等待而非误判失败 |
| R3 manage.sh 契约 | P0 | 非交互调用全部命令无阻塞；失败退出码非 0 |
| R4 互斥锁 | P1 | watchdog 恢复中 agent_go repair 快速失败并提示，不双重重启 |
| R5 watchdog 状态 | P1 | diagnose 输出含「watchdog 恢复中」判定 |
| R6 profile 端点 | P1（可选） | `model list` 走 HTTP（若实现） |
| R7 事件通知 | P2（可选） | 监控页展示生命周期事件 |
| R8 路由归因返回 | **P0** | metering 按 route_target 标 is_local（不再按 URL），成本归因正确 |
| R9 路由策略可视 | **P0** | 配置中心展示模型实际路由（本地/云端），替代盲猜 |
| R10 模型能力元数据 | P1 | ① registry 自动同步能力属性，免手工录入 |
| R11 status 路由配置段 | P1 | 健康检查探测依据统一（cloud_model/key_set） |
| R12 HTTP 热重载 | P2（可选） | 远程/容器场景可 reload |
| R13 诊断响应头扩展 | P1 | metering 采集 Prompt-Processed-N / Epoch-Count / Feedback-Injected（R8 解析模式扩展） |
| R14 会话台账端点 | P1 | 轮级看门狗消费 dup / last_dup_turn / 材料清单 |
| R15 L4 档案查询 | P2 | 压缩后行为复盘以代理档案为准（L4 只读） |
| R17 信号快照端点 | P1（**已冻结** 2026-09-02） | AG-3 replan 决策表消费 reread_pressure；HealthGate 观测出口 |
| R18 task-context 证据包 | P1（**已冻结** 2026-09-02） | AG-4 reload 重试路径消费；语义封装、机制面降 admin/debug |
| R19 pin 请求头 | P2（**已冻结** 2026-09-02） | AG-5 reload 重试防二次压缩；三约束（预算/ OOM 语义/append-only）落地 |
| R16 /metrics 会话维度 | P1 | 每轮 jsonl 落盘 + session 聚合时序，A/B 出数 |

## 6. 兼容策略

- R1/R2 未实现前，agent_go 维持现有 HTML 解析 + pidfile 兜底，功能可用但脆弱。
- 全部需求按「新增不破坏」原则：llama-defender 新增端点/字段，不改变现有 `/v1/models`、`/metrics`、manage.sh 既有行为。
- agent_go 侧对每个需求做存在性探测，缺失则降级（fail-open），保证旧版 llama-defender 可继续工作。
