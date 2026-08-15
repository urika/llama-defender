# 配置统一与请求队列设计方案

> **状态**: 已实施（配置统一 Phase 1-2 + 请求队列 Phase 1，2026-08-15 灰度开启于 `qwen3.8-27b-4bit`）  
> **日期**: 2026-08-15  
> **版本**: v1.1  
> **关联文档**: `proxy-context-window-design.md`, `multi-cloud-model-catalog-design-20260815.md`  
> **背景**: 针对 qwen3.8-27b 引入过程中发现的 `MODEL_NAME` 漂移问题，以及 240K–724K chars 超大请求直接访问后端导致小请求 TTFT 60–190s 的阻塞问题

---

## 0. 设计范围

本文档包含两个相对独立但互补的设计：

1. **配置统一（Configuration Unification）**  
   解决配置默认值分散在 `backend_strategy.py`、`proxy_state.py`、`proxy_config.py`、`manage.sh` 四处导致的漂移风险。

2. **请求队列设计（Request Queue Design）**  
   解决 `PROXY_MAX_CONCURRENT=1` 信号量下大请求阻塞小请求、缺乏优先级/超时/背压的问题。

---

# 第一部分：配置统一

## 1. 现状分析

### 1.1 当前默认值分布

| 位置 | 作用 | 问题 |
|------|------|------|
| `proxy_config.py` | `CONFIG_REGISTRY`，声明式配置元数据 | 名义上的权威，但未被其他模块强制消费 |
| `backend_strategy.py` | `LocalStrategy.DEFAULTS` / `CloudStrategy.DEFAULTS` | 与 `CONFIG_REGISTRY` 重复，且不参与 reload |
| `proxy_state.py` | `_default()` 函数做本地/云端 fallback | 第三处硬编码默认值 |
| `manage.sh` | Bash 启动时的默认值注入 | 第四处硬编码，bash 与 Python 默认值可能不一致 |

### 1.2 已发生的事故

`configs/qwen3.8-27b-4bit.conf` 只设置了 `LLAMA_MODEL`，未设置 `MODEL_NAME`：

- `proxy_state.py` 回退到 `LocalStrategy.DEFAULTS` 的 `mlx-community/Qwen3.6-35B-A3B-4bit`
- 后端实际加载 `mlx-community/Qwen3.8-27B-4bit`
- `/api/status` 报 `model_drift`
- metrics 归因错误，路由策略元数据错误

**根因**：新增配置时，作者需要知道在 `proxy_config.py`、`backend_strategy.py`、`proxy_state.py`、`manage.sh` 中至少一处设置默认值，且它们之间没有启动时校验。

### 1.3 当前 CONFIG_REGISTRY 的局限

`CONFIG_REGISTRY` 虽然声明了每个变量的默认值、类型、作用域，但：

- `proxy_state.py` 没有强制从 `CONFIG_REGISTRY` 读取
- `backend_strategy.py` 的 `DEFAULTS` 与 `CONFIG_REGISTRY` 无强制一致性校验
- `manage.sh` 的 Bash 默认值与 Python 层无同步机制
- 新增变量时不会自动检查是否已注册

---

## 2. 设计目标

1. **单一事实源**：`proxy_config.py` 的 `CONFIG_REGISTRY` 是所有配置默认值、类型、作用域、文档的唯一权威。
2. **强制一致性**：`proxy_state.py`、`backend_strategy.py`、`manage.sh` 必须从 `CONFIG_REGISTRY` 派生默认值，不再独立定义。
3. **启动即校验**：代理启动时校验配置完整性和一致性，发现漂移立即报错。
4. **配置漂移检测**：新增配置变量时必须注册到 `CONFIG_REGISTRY`，否则启动失败。
5. **热重载安全**：`scope=reloadable` 的变量在 SIGHUP 时安全更新，`scope=module` 的变量禁止热重载。

### 2.1 非目标

- 不引入 YAML/TOML 等新的配置格式，继续用 Bash-sourcable `KEY="value"`。
- 不改变现有环境变量命名（`PROXY_*` / `LLAMA_*` / `RAPID_MLX_*`）。
- 不引入配置版本迁移机制（本次仅做统一，不做 schema 演进）。

---

## 3. 设计方案

### 3.1 总体架构

```
configs/*.conf (用户配置，Bash KEY="value")
        │
        ▼
manage.sh (启动时读取 active.conf + secret.local.conf)
        │  export 环境变量
        ▼
proxy_state.py (只读取环境变量，从 CONFIG_REGISTRY 获取默认值)
        │  from proxy_config import get_default, get_registry
        ▼
backend_strategy.py (仅保留行为标志，不再定义 DEFAULTS)
        ▼
proxy_config.py CONFIG_REGISTRY (唯一权威)
        │
        ├─ validate_startup()   → 启动时校验
        ├─ validate_reload()    → SIGHUP 时校验
        └─ lint_config_file()   → 检查 conf 文件变量是否已注册
```

### 3.2 核心改动

#### 3.2.1 `proxy_config.py`：增强为运行时配置工厂

新增函数：

```python
def get_default(key: str, backend_type: str) -> Any:
    """从 CONFIG_REGISTRY 返回解析后的默认值（含类型转换）。"""

def get_registry_entry(key: str) -> dict:
    """返回 CONFIG_REGISTRY 中某条目的完整元数据。"""

def is_reloadable(key: str) -> bool:
    """返回 scope == 'reloadable'。"""

def list_unregistered_env_vars(env: dict) -> list:
    """返回 env 中未在 CONFIG_REGISTRY 注册的 PROXY_*/LLAMA_*/RAPID_MLX_* 变量。"""
```

保留 `CONFIG_REGISTRY` 字典结构不变，但所有其他模块必须通过上述函数访问，不再直接硬编码默认值。

#### 3.2.2 `proxy_state.py`：废除 `_default()`，统一从 `CONFIG_REGISTRY` 取值

当前代码：

```python
def _default(env_key, local_val, cloud_val):
    _strategy = LocalStrategy if not IS_CLOUD else CloudStrategy
    return str(_strategy.get_default(env_key, local_val if not IS_CLOUD else cloud_val))

PROXY_MAX_CONCURRENT = int(os.environ.get("PROXY_MAX_CONCURRENT", _default("PROXY_MAX_CONCURRENT", "4", "1")))
```

改为：

```python
from proxy_config import get_default

PROXY_MAX_CONCURRENT = int(
    os.environ.get("PROXY_MAX_CONCURRENT", get_default("PROXY_MAX_CONCURRENT", BACKEND_TYPE))
)
```

**原则**：`proxy_state.py` 不再 import `backend_strategy` 来获取默认值。

#### 3.2.3 `backend_strategy.py`：仅保留行为标志

当前：

```python
class LocalStrategy(BackendStrategy):
    DEFAULTS = { ... }  # 删除整个字典
    oom_safety_enabled = True
    prefix_cache_enabled = True
```

改为：

```python
class LocalStrategy(BackendStrategy):
    # DEFAULTS 删除，所有默认值来自 proxy_config.CONFIG_REGISTRY
    oom_safety_enabled = True
    prefix_cache_enabled = True
```

新增一个过渡方法用于向后兼容（标记 deprecated）：

```python
class BackendStrategy:
    @classmethod
    def get_default(cls, key, fallback=None):
        """DEPRECATED: use proxy_config.get_default()."""
        from proxy_config import get_default
        return get_default(key, "local" if cls is LocalStrategy else "cloud") or fallback
```

#### 3.2.4 `manage.sh`：从 `CONFIG_REGISTRY` 动态生成默认值

在 `manage.sh` 的 `_apply_defaults()` 中，不再硬编码默认值，而是调用一个轻量 CLI：

```bash
# tools/config_default.py
python3 -c "
import proxy_config
import os
backend_type = os.environ.get('BACKEND_TYPE', 'local')
print(proxy_config.get_default('$1', backend_type))
"
```

或者更简单：启动代理前，由 `proxy_config.py` 提供一个 `export_defaults.sh` 生成函数，写入临时文件后 source：

```python
# proxy_config.py
def write_defaults_sh(backend_type: str, path: str):
    """把 CONFIG_REGISTRY 中所有未显式设置的变量写入 Bash 文件。"""
```

`manage.sh` 改为：

```bash
source configs/active.conf
source configs/secret.local.conf 2>/dev/null || true
python3 -c "
import proxy_config
proxy_config.write_defaults_sh('$BACKEND_TYPE', '.config_defaults.tmp')
" && source .config_defaults.tmp && rm .config_defaults.tmp
```

#### 3.2.5 启动校验（Startup Validation）

`anthropic_proxy.py` 的 `main()` 启动前调用：

```python
from proxy_config import validate_startup

errors = validate_startup(
    env=os.environ,
    active_conf_path=proxy_config.CONFIG_PATH,
    backend_type=proxy_config.BACKEND_TYPE,
)
if errors:
    for e in errors:
        log(f"[CONFIG ERROR] {e}")
    sys.exit(1)
```

`validate_startup()` 检查：

1. **所有 `PROXY_*` / `LLAMA_*` / `RAPID_MLX_*` 环境变量已注册**  
   未注册变量提示：`"PROXY_FOO is not in CONFIG_REGISTRY. Add it or fix the typo."`

2. **类型一致性**  
   `PROXY_MAX_CONCURRENT` 必须是正整数；`PROXY_ROUTE_ENABLED` 必须是布尔字符串。

3. **local/cloud 模式匹配**  
   `MODEL_NAME` 必须与 `BACKEND_TYPE` 匹配；cloud 模式必须设置 `PROXY_CLOUD_API_KEY`（或 `LLAMA_API_KEY` 非 dummy）。

4. **配置文件变量注册**  
   解析 `active.conf` 中的所有 `KEY=value`，检查是否都在 `CONFIG_REGISTRY` 中。新增配置时漏注册会在启动时报错。

5. **默认值一致性**  
   如果 `backend_strategy.py` 还残留 `DEFAULTS`，启动时警告并提示删除。

#### 3.2.6 配置文件 Lint（Config Linting）

`manage.sh` 新增命令：

```bash
./manage.sh config-lint qwen3.8-27b-4bit.conf
```

检查：
- 变量名是否已注册
- 值类型是否正确
- 必填元数据（`CONFIG_NAME`、`CONFIG_DESC`、`CONFIG_MEMORY`）是否存在
- `MODEL_NAME` 是否与后端预期模型一致（可选，需联网或本地检查）

---

## 4. 迁移计划

### 4.1 阶段一：软迁移（1 个 PR）

1. 增强 `proxy_config.py`：新增 `get_default()`、`validate_startup()`、`write_defaults_sh()`。
2. `proxy_state.py` 逐步替换 `_default()` 调用，保留 `_default()` 作为向后兼容 wrapper（标记 deprecated）。
3. `backend_strategy.py` 保留 `DEFAULTS`，但 `proxy_state.py` 不再使用。
4. `manage.sh` 在 `start` 时先调用 `validate_startup`，发现错误时警告但不退出。
5. 所有现有配置跑一遍 lint，修复问题。

### 4.2 阶段二：硬迁移（1-2 个 PR）

1. 删除 `backend_strategy.py` 的 `DEFAULTS`。
2. 删除 `proxy_state.py` 的 `_default()`。
3. `manage.sh` 的 `_apply_defaults()` 完全切换到 `write_defaults_sh()`。
4. `validate_startup()` 从警告改为硬失败。
5. 预提交钩子加入 `config-lint`。
6. 更新 `AGENTS.md`、`CLAUDE.md` 的配置开发指南。

### 4.3 阶段三：长期治理

1. 新增配置变量必须先在 `CONFIG_REGISTRY` 注册，PR 模板加 checklist。
2. 文档（AGENTS.md / CLAUDE.md / docs/）引用 `CONFIG_REGISTRY` 的默认值，不再硬编码。
3. 每次发版前运行 `config-lint` 对所有 `configs/*.conf`。

---

# 第二部分：请求队列设计

## 5. 现状分析

### 5.1 当前并发模型

```python
# proxy_state.py
_llama_lock = threading.Semaphore(PROXY_MAX_CONCURRENT)

# anthropic_proxy.py Handler.do_POST()
with _llama_lock:
    _handle_messages(body)
```

- **local 模式**：`PROXY_MAX_CONCURRENT=1`，严格串行。
- **cloud 模式**：`PROXY_MAX_CONCURRENT=4`（或动态调整），简单并发。

### 5.2 已观察到的阻塞场景

1. 客户端直接 POST 到 rapid-mlx:8081（绕过代理），发送 240K–724K chars 的超大 prompt。
2. rapid-mlx `adaptive_prefill` 将 chunk size 降到 512 tokens，46K tokens prefill 耗时 184s。
3. 期间其他正常小请求（16–60 tokens）被排队，TTFT 60–190s。
4. 代理侧出现 504（`PROXY_BACKEND_TIMEOUT=600s` 不够时）或流式连接超长保活（752s）。

### 5.3 当前缺失的能力

| 能力 | 现状 | 影响 |
|------|------|------|
| 请求优先级 | 无 | 小请求与大请求同等排队 |
| 预估等待时间 | 无 | 客户端不知道要等多久 |
| 大请求拒绝/重定向 | 无 | 超大 prompt 直接阻塞后端 |
| 队列深度可观测 | 无 | 无法监控排队情况 |
| 请求取消/超时主动断连 | 无 | 客户端挂死等待 |
| 流式/非流式分离 | 无 | 都走同一个信号量 |

---

## 6. 设计目标

1. **公平性**：小请求不应被大请求无限阻塞。
2. **可预期性**：请求进入队列时返回预估等待时间和队列位置。
3. **自我保护**：超过容量或阈值的大请求被拒绝、截断或自动路由到云端。
4. **可观测性**：队列深度、等待时间、拒绝原因写入 metrics。
5. **向后兼容**：默认配置下行为与当前一致（`PROXY_MAX_CONCURRENT=1`），队列特性可通过配置开启。

### 6.1 非目标

- 不实现多后端负载均衡（仍是一个本地后端）。
- 不实现请求持久化队列（重启后队列清空）。
- 不修改后端（rapid-mlx/llama-server）的调度逻辑，只在代理层做排队。

---

## 7. 设计方案

### 7.1 总体架构

```
Client
  │ POST /v1/messages
  ▼
┌─────────────────────────────────────────┐
│  Request Classifier                      │
│  - 按 chars/token 估算分桶               │
│  - 提取 stream / model / priority hint   │
└────────────────┬────────────────────────┘
                 │
        ┌────────▼────────┐
        │  Admission Gate │  ← 是否拒绝/重定向
        └────────┬────────┘
                 │
        ┌────────▼────────┐
        │  Priority Queue │  ← 按 bucket 分队列
        └────────┬────────┘
                 │
        ┌────────▼────────┐
        │  Worker Pool    │  ← Semaphore(PROXY_MAX_CONCURRENT)
        │  (1 or N)       │
        └────────┬────────┘
                 │
        ┌────────▼────────┐
        │  Backend        │
        │  (rapid-mlx)    │
        └─────────────────┘
```

### 7.2 请求分桶（Request Bucketing）

按预估 prompt tokens 将请求分为四级：

| Bucket | Token 范围 | 策略 | 目标场景 |
|--------|-----------|------|----------|
| `interactive` | < 8K | 最高优先级，允许抢占 | 日常对话、小工具调用 |
| `standard` | 8K–32K | 正常优先级 | 中等上下文 coding |
| `large` | 32K–100K | 低优先级，可排队 | 长文档分析 |
| `huge` | > 100K | 拒绝或强制路由云端 | 超长上下文 |

**分桶依据**：`total_chars / 2`（当前项目使用的粗略 token 估算），或直接从 backend `prompt_tokens` 反推（后端有精确值，但需请求完成后才知道，所以只能用于事后统计，不能用于事前分桶）。

**优先级规则**：
1. `interactive` 请求插入 `standard`/`large`/`huge` 队列前面。
2. 同 bucket 内 FIFO。
3. `huge` bucket 默认不进入本地队列，直接触发 `SmartRouter` 的云端路由或返回 413。

### 7.3 准入控制（Admission Gate）

在请求进入队列前执行以下检查：

```python
def admission_check(ctx: PipelineContext) -> AdmissionResult:
    """
    返回: allow | reject | route_to_cloud | truncate_and_allow
    """
    chars = ctx.total_chars
    backend_type = ctx.backend_type
    
    # 1. 请求体硬上限（已有）
    if chars > PROXY_MAX_REQUEST_BYTES:
        return reject(status=413, reason="payload_too_large")
    
    # 2. huge bucket 处理
    if chars > PROXY_QUEUE_HUGE_THRESHOLD_CHARS:  # 默认 200K
        if PROXY_ROUTE_ENABLED and not ctx.force_local:
            return route_to_cloud(reason="huge_context")
        return reject(status=413, reason="huge_context_not_supported_locally")
    
    # 3. 大请求预警：进入 large bucket 时提示用户
    if chars > PROXY_QUEUE_LARGE_THRESHOLD_CHARS:  # 默认 80K
        ctx.add_warning("large_request_queueing", f"prompt ~{chars//2}K tokens may wait behind large requests")
    
    return allow
```

### 7.4 优先级队列实现

使用 `heapq` 实现带优先级的队列，替代裸 `threading.Semaphore`：

```python
import heapq
import itertools

@dataclass(order=True)
class QueuedRequest:
    priority: int               # bucket 权重 + 时间惩罚
    seq: int = field(compare=False)  # FIFO tie-breaker
    ctx: PipelineContext = field(compare=False)
    enqueue_ts: float = field(compare=False)

class PriorityRequestQueue:
    def __init__(self, max_workers: int):
        self._heap = []
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._workers = threading.Semaphore(max_workers)
        self._waiting_count = 0
    
    def enqueue(self, ctx) -> QueueTicket:
        bucket = classify_bucket(ctx.total_chars)
        priority = bucket_priority(bucket)
        ticket = QueueTicket(
            id=ctx.request_id,
            bucket=bucket,
            enqueue_ts=time.time(),
        )
        item = QueuedRequest(
            priority=priority,
            seq=next(self._seq),
            ctx=ctx,
            enqueue_ts=time.time(),
        )
        with self._lock:
            heapq.heappush(self._heap, item)
            self._waiting_count += 1
        return ticket
    
    def dequeue(self) -> Optional[PipelineContext]:
        with self._lock:
            if not self._heap:
                return None
            item = heapq.heappop(self._heap)
            self._waiting_count -= 1
            item.ctx.queue_wait_ms = (time.time() - item.enqueue_ts) * 1000
            return item.ctx
    
    def estimated_wait_ms(self, bucket: str) -> int:
        """根据当前队列深度和 bucket 返回预估等待时间。"""
        # 简化实现：队列中比该 bucket 优先级高或同级的请求数 × 平均处理时间
        ...
```

**关键**：在 `Handler.do_POST()` 中，不再直接 `with _llama_lock:`，而是：

```python
# 伪代码
def do_POST(self):
    ctx = RequestParser().process(...)
    
    # 准入控制
    admission = admission_gate(ctx)
    if admission.action == "reject":
        return send_error(admission.status, admission.reason)
    if admission.action == "route_to_cloud":
        return SmartRouter().route_to_cloud(ctx)
    
    # 入队
    ticket = priority_queue.enqueue(ctx)
    ctx.set_header("X-Queue-Bucket", ticket.bucket)
    ctx.set_header("X-Queue-Position", str(ticket.position))
    ctx.set_header("X-Queue-Estimated-Wait-Ms", str(ticket.estimated_wait_ms))
    
    # 等待 worker
    worker_acquired = priority_queue.wait_for_worker(ticket, timeout=PROXY_QUEUE_TIMEOUT_SECONDS)
    if not worker_acquired:
        priority_queue.cancel(ticket)
        return send_error(503, "queue_timeout", retryable=True)
    
    try:
        pipeline.run(ctx)
    finally:
        priority_queue.release_worker(ticket)
```

### 7.5 超时与取消

- **排队超时**：`PROXY_QUEUE_TIMEOUT_SECONDS`（默认 300s）。超过后返回 503 + `Retry-After`，并从队列移除。
- **执行超时**：`PROXY_BACKEND_TIMEOUT` 不变，但大请求在入队前会被准入控制拦截。
- **主动取消**：客户端断开连接时，如果请求还在队列中，从队列移除；如果已在执行，通知后端（如果后端支持 abort）。

### 7.6 流式请求处理

流式请求和非流式请求在同一个队列，但：
- 流式请求一旦获得 worker，会长时间占用连接。
- 建议为流式请求单独分配 `PROXY_STREAM_MAX_CONCURRENT`（默认 1），避免流式大请求阻塞所有交互式请求。

### 7.7 与 SmartRouter 的集成

`huge` bucket 默认触发云端路由，而不是进入本地队列。这复用了现有的 `SmartRouter` 逻辑：

```python
if bucket == "huge":
    ctx.route_hint = "cloud"
    ctx.route_reason = "huge_context_auto_route"
```

### 7.8 直接访问后端的防护

请求队列无法阻止直接访问 `:8081` 的流量，但可以降低其影响：

1. **启动时绑定随机端口**：`manage.sh` 启动 rapid-mlx 时不固定 `8081`，而是随机端口，代理通过环境变量获得。外部客户端不知道端口。
2. **代理到后端的内部校验头**：代理转发请求时加 `X-Proxy-Internal: <token>`，后端校验该头，拒绝直接请求。
3. **后端侧最大 prompt 限制**：通过 rapid-mlx 启动参数限制 `--max-prompt-tokens`（如果支持）或 `--max-model-len`。

**本次设计只做代理层队列，直接访问后端的封堵作为独立 P0 任务处理。**

---

## 8. 接口变更

### 8.1 新增配置变量（需注册到 CONFIG_REGISTRY）

| 变量 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `PROXY_QUEUE_ENABLED` | bool | `false` | 是否启用优先级队列（默认关闭，向后兼容） |
| `PROXY_QUEUE_TIMEOUT_SECONDS` | int | `300` | 排队超时时间 |
| `PROXY_QUEUE_LARGE_THRESHOLD_CHARS` | int | `80000` | large bucket 阈值 |
| `PROXY_QUEUE_HUGE_THRESHOLD_CHARS` | int | `200000` | huge bucket 阈值，超过则路由云端或拒绝 |
| `PROXY_QUEUE_HUGE_ACTION` | str | `cloud` | huge bucket 处理：`cloud` / `reject` / `truncate` |
| `PROXY_STREAM_MAX_CONCURRENT` | int | `1` | 流式请求专用并发数 |

### 8.2 新增响应头

| 头 | 说明 |
|----|------|
| `X-Queue-Bucket` | 请求分桶：interactive / standard / large / huge |
| `X-Queue-Position` | 入队时的队列位置 |
| `X-Queue-Estimated-Wait-Ms` | 预估等待毫秒数 |
| `X-Queue-Wait-Ms` | 实际等待毫秒数（响应时） |

### 8.3 新增 metrics 字段

```json
{
  "queue": {
    "enabled": true,
    "bucket": "standard",
    "position_at_enqueue": 3,
    "estimated_wait_ms": 15000,
    "actual_wait_ms": 12500,
    "queue_depth": 2,
    "rejected_reason": null
  }
}
```

### 8.4 新增 API 端点

`GET /api/queue`：

```json
{
  "enabled": true,
  "workers": 1,
  "waiting": 3,
  "by_bucket": {
    "interactive": 0,
    "standard": 2,
    "large": 1,
    "huge": 0
  },
  "oldest_wait_ms": 45000
}
```

---

## 9. 数据结构

```python
# pipeline.py 或新模块 queue_manager.py

@dataclass
class QueueTicket:
    id: str
    bucket: str
    position: int
    enqueue_ts: float
    estimated_wait_ms: int

@dataclass
class AdmissionResult:
    action: str  # "allow" | "reject" | "route_to_cloud" | "truncate_and_allow"
    status: int = 200
    reason: str = ""
    truncated_chars: int = 0

class RequestQueueManager:
    def __init__(self, config: QueueConfig):
        self._heap: list[QueuedRequest] = []
        self._worker_sem = threading.Semaphore(config.max_workers)
        self._stream_sem = threading.Semaphore(config.stream_max_workers)
    
    def classify_bucket(self, total_chars: int) -> str:
        ...
    
    def enqueue(self, ctx) -> QueueTicket:
        ...
    
    def cancel(self, ticket: QueueTicket) -> bool:
        ...
    
    def acquire_worker(self, ticket, timeout) -> bool:
        ...
    
    def release_worker(self, ticket):
        ...
    
    def stats(self) -> dict:
        ...
```

---

## 10. 测试计划

### 10.1 单元测试

| 测试用例 | 验证点 |
|----------|--------|
| `test_queue_bucket_classification` | 不同 chars 分入正确 bucket |
| `test_queue_priority_ordering` | interactive 先于 large 出队 |
| `test_queue_timeout` | 超时后返回 503 且从队列移除 |
| `test_admission_reject_huge` | huge bucket 被正确拒绝或路由 |
| `test_queue_stats_accuracy` | `/api/queue` 返回正确队列深度 |
| `test_queue_backward_compat` | `PROXY_QUEUE_ENABLED=false` 时行为与当前一致 |

### 10.2 集成测试

| 场景 | 步骤 | 预期 |
|------|------|------|
| 大请求阻塞小请求 | 1. 发 100K chars 请求<br>2. 立即发 1K chars 请求 | 小请求进入 interactive bucket，不被阻塞或提示等待时间 |
| huge 自动路由 | 1. 发 250K chars 请求<br>2. `PROXY_ROUTE_ENABLED=true` | 请求被路由到云端，不进入本地队列 |
| 队列超时 | 1. 设置 `PROXY_QUEUE_TIMEOUT=1`<br>2. 发大请求占满 worker<br>3. 发第二个请求 | 第二个请求 503 + Retry-After |
| 流式请求隔离 | 1. 发流式大请求<br>2. 发非流式小请求 | 小请求不被流式请求阻塞（如果流式独立 worker） |

### 10.3 压力测试

- 并发 10 个小请求 + 2 个大请求，验证平均等待时间。
- 连续发 5 个 huge 请求，验证都被路由/拒绝，本地队列不爆炸。

---

## 11. 上线计划

### Phase 1：队列基础设施（默认关闭）

1. 实现 `RequestQueueManager`。
2. `PROXY_QUEUE_ENABLED=false` 时走原信号量逻辑。
3. 加单元测试和集成测试。
4. 文档更新。

### Phase 2：灰度开启

1. 在 `qwen3.8-27b-4bit.conf` 中开启 `PROXY_QUEUE_ENABLED=true`。
2. 监控 24–48 小时，观察 queue wait、TTFT、504 率。
3. 调优 bucket 阈值。

### Phase 3：默认开启

1. 所有 local 配置默认 `PROXY_QUEUE_ENABLED=true`。
2. 文档声明为推荐配置。
3. 移除旧信号量路径（或保留一个版本作为紧急回退）。

---

## 12. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 队列实现引入 bug 导致死锁 | 中 | 高 | 全面单元测试；保留 `PROXY_QUEUE_ENABLED=false` 回退 |
| 分桶阈值不合理 | 高 | 中 | 灰度期间根据 metrics 调整阈值 |
| 大请求被路由云端导致成本上升 | 中 | 中 | 设置 `PROXY_ROUTE_DAILY_BUDGET`；`huge` 可配置为 `reject` 而非 `cloud` |
| 流式请求仍阻塞队列 | 中 | 中 | 独立 `PROXY_STREAM_MAX_CONCURRENT`；或流式请求单独排队 |
| 配置统一破坏现有配置 | 低 | 高 | 阶段一软迁移，先警告不退出 |

---

## 13. 总结

### 13.1 配置统一

- **问题**：默认值分散四处，新增配置易遗漏。
- **方案**：`CONFIG_REGISTRY` 为唯一权威，`proxy_state.py` / `backend_strategy.py` / `manage.sh` 强制消费。
- **交付**：`validate_startup()` + `config-lint` + 两阶段迁移。

### 13.2 请求队列

- **问题**：单信号量无优先级，大请求阻塞小请求。
- **方案**：四级 bucket + 准入控制 + 优先级队列 + 超时取消。
- **交付**：默认关闭，灰度后开启，与 SmartRouter 集成。

### 13.3 两个设计的协同

- 配置统一确保 `PROXY_QUEUE_*` 等新变量有一致的注册和校验。
- 请求队列的 huge bucket 处理依赖配置统一中的路由参数解析。
- 两者共同提升代理层的健壮性和可维护性。

---

## 14. 待办清单

### 配置统一

- [ ] `proxy_config.py`: 新增 `get_default()` / `validate_startup()` / `write_defaults_sh()` / `list_unregistered_env_vars()`
- [ ] `proxy_state.py`: 替换所有 `_default()` 调用为 `get_default()`
- [ ] `backend_strategy.py`: 删除 `DEFAULTS`，保留行为标志
- [ ] `manage.sh`: `_apply_defaults()` 改为消费 `write_defaults_sh()`
- [ ] `manage.sh`: 新增 `config-lint` 命令
- [ ] `anthropic_proxy.py`: `main()` 中调用 `validate_startup()`
- [ ] `test/unit/test_proxy_config.py`: 新增配置统一测试
- [ ] 更新 `AGENTS.md` / `CLAUDE.md`

### 请求队列

- [ ] 新建 `queue_manager.py`（或并入 `pipeline.py`）
- [ ] `proxy_config.py`: 注册 `PROXY_QUEUE_*` / `PROXY_STREAM_MAX_CONCURRENT`
- [ ] `anthropic_proxy.py`: `Handler.do_POST()` 集成 `admission_gate` + `priority_queue`
- [ ] `admin_server.py`: 新增 `/api/queue` 端点
- [ ] `test/unit/test_queue_manager.py`
- [ ] `test/integration/test_queue_integration.sh`
- [ ] `configs/qwen3.8-27b-4bit.conf`: 灰度开启队列
- [ ] 更新 `AGENTS.md` / `CLAUDE.md`

---

> 评审意见请在此文档下回复，或创建 `config-unification-and-request-queue-design-review-20260815.md`。
