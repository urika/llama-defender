# 代理截断设计：Agent 长上下文场景分析

> **日期**: 2026-06-10
> **背景**: 4-bit KV 量化 + 推测解码测试完成后，对代理截断逻辑在 Agent 多轮场景下的适用性进行分析

---

## 1. 问题场景

### 典型 Agent Workflow

```
用户任务 → 系统提示 → 多轮工具调用累积 → 最终分析大文本/代码库
                      ↑ 这里开始累积上下文
                      ↓
              50-200K chars，50-200 条消息
                      ↓
              此时用户让 agent 分析一个 80K 的代码库
                      ↓
              最终请求 = 80K 历史 + 80K 新内容 = 160K chars
```

### 单次大请求测试 vs 真实 Agent 场景

| 维度 | 单次大请求测试 | 真实 Agent 多轮场景 |
|------|--------------|-------------------|
| 消息数量 | 2-3 条 | 50-200 条 |
| 总字符数 | 102K | 160K+ |
| tool_result 比例 | 低 | 高（文件读取结果） |
| 历史价值 | 无 | 高（决定后续操作） |
| 当前截断策略 | 不触发（正确） | 可能误触发或不触发（错误） |

---

## 2. 当前截断逻辑分析

### 三层截断机制

```
请求 → _classify_lifecycle_stage() → stage (init/growth/expansion/saturation/oom_danger/pre_trunc)
                              ↓
                    truncate_rounds = stage_config["truncate_rounds"]
                              ↓
         truncate_messages_if_needed(messages, keep_rounds=N)
                              ↓
         策略路由：rounds / fifo / char
```

### 当前策略路由

```python
# anthropic_proxy.py:1541-1682
if PROXY_CTX_TRUNCATE_STRATEGY == "rounds":
    # token 预算检查（PROXY_CHARS_EXPANSION=90K）
    # 按 rounds 截断
elif PROXY_CTX_TRUNCATE_STRATEGY == "fifo":
    # 按消息数量截断（PROXY_CTX_KEEP_MESSAGES=30）
    # ⚠️ 完全不看字符数！n <= 30 就直接返回
elif ...:  # char 及其他
    # no-op fallback
```

### 100KB 单次请求为什么不触发截断

```
100KB 请求（2条消息）进入：
  → _classify_lifecycle_stage(total_chars=102K) → stage="saturation"
  → truncate_rounds=10
  → truncate_messages_if_needed(messages, keep_rounds=10)
  → PROXY_CTX_TRUNCATE_STRATEGY=fifo
  → n=2 <= 30 → 直接返回，不截断 ✅ 正确
```

### 真实 Agent 场景的三个盲区

#### 盲区 1：多轮累积 + 大请求 = 双重处理缺失

```
场景：会话已有 80K chars / 50 条消息（工具调用历史）
      此时用户让 agent 分析一个 80K 的代码库
      最终请求 = 80K 历史 + 80K 新内容 = 160K chars
```

- `fifo` 只看消息数量：`n=52 <= 30`，跳过
- `_classify_lifecycle_stage` 进入 `saturation` 或 `oom_danger`
- 但 `truncate_rounds=10` 只在 `rounds` 策略下生效，`fifo` 下完全不读这个值
- **结果**：160K chars 的请求直接送后端

#### 盲区 2：工具调用历史的"有效信息密度"极低

```
典型 Claude Code 会话消息结构：
- user: "任务描述"                      200 chars  →  高价值
- assistant: tool_use(Read)             500 chars  →  低价值
- user: tool_result(文件内容 20KB)     20,000 chars → 高价值，保留
- assistant: tool_use(Grep)             300 chars  →  低价值
- user: tool_result(results 5KB)       5,000 chars → 高价值，保留
... 重复 50 次 ...
```

`fifo` 策略按"消息条数"截断，不区分：
- `tool_result` 包含实际文件内容（高价值）
- `assistant` 的决策推理文本（低价值，可压缩）
- `user` 消息的任务描述（高价值，通常在 head）

#### 盲区 3：Pre-truncate 阈值设置方向错误

```
PROXY_PRE_TRUNCATE_CHARS=400K 是为了防 OOM
但真正危险的是"多轮累积 + 大新请求"叠加后的总 context
单次 100K 请求不是问题，多轮累积后才是
```

---

## 3. 设计原则

| 原则 | 说明 |
|------|------|
| **Token 预算优先于消息数量** | `fifo` 按条数截断在多轮场景下无效 |
| **Tool-result 内容优先保留** | 文件读取结果是高价值信息，丢弃后必须重读 |
| **Head 保护** | 系统提示和技能定义每次都要，不计入截断预算 |
| **区分续接和大请求** | 新请求用宽松策略，多轮累积用严格预算 |
| **4-bit KV 后预算更宽裕** | 量化后同样内存能装更多 token，预算可适当放大 |

---

## 4. 具体改进建议

### 改进 1：`rounds` 策略增加 token 预算检查（P0）

**文件**: `anthropic_proxy.py`

**问题**：`rounds` 策略只看 `keep_rounds=N`，不考虑 token 总量。大会话（每轮 100 条消息）可能远超 token 预算。

**改进**：在 `rounds` 策略中，截断后增加 token 预算二次检查，超预算则继续降 rounds 压缩。

```python
# anthropic_proxy.py, truncate_messages_if_needed() rounds 分支，约 line 1564

# ① 先做 rounds 截断
result, stats = _apply_rounds_truncation(messages, keep_rounds, session_id=session_id)
result_chars = _estimate_message_chars(result)
result_tokens = _estimate_message_tokens(result)  # 新增

# ② rounds 后检查 token 预算
budget_tokens = PROXY_TOKEN_BUDGET  # 默认 30000
if result_tokens <= budget_tokens:
    stats["chars"] = result_chars
    stats["tokens"] = result_tokens
    stats["budget_tokens"] = budget_tokens
    return result, stats

# ③ 超预算：降 rounds 继续压缩
min_rounds = 2
while result_tokens > budget_tokens and keep_rounds > min_rounds:
    keep_rounds -= 1
    result, stats = _apply_rounds_truncation(messages, keep_rounds, session_id=session_id)
    result_tokens = _estimate_message_tokens(result)

return result, stats
```

---

### 改进 2：新增 `smart` 截断策略（P2）

**文件**: `anthropic_proxy.py`

**问题**：`fifo` 和 `rounds` 不区分消息内容价值。

**改进**：新增 `smart` 策略，按角色+内容类型决定保留优先级。

```python
# anthropic_proxy.py

PROXY_CTX_TRUNCATE_STRATEGY = os.environ.get(
    "PROXY_CTX_TRUNCATE_STRATEGY", "rounds"  # 改默认
)

elif PROXY_CTX_TRUNCATE_STRATEGY == "smart":
    """
    Smart truncate: 优先保留高价值内容。
    优先级（高→低）：
      1. system / head 消息     — 不截断
      2. tool_result 内容        — 精确保留
      3. user 消息（不含工具结果）— 保留最近 N 条
      4. assistant 推理文本      — 可压缩/替换为摘要
    """
    return _apply_smart_truncation(messages, session_id=session_id)


def _apply_smart_truncation(messages, session_id=None):
    budget = PROXY_TOKEN_BUDGET

    # 分类
    system = [m for m in messages if m.get("role") == "system"]
    tool_results = [m for m in messages if _is_tool_result(m)]
    others = [m for m in messages if m not in system + tool_results]

    current_tokens = sum_tokens(system + tool_results)
    kept = []

    # 从新到旧保留 others
    for msg in reversed(others):
        msg_tokens = estimate_tokens(msg)
        if current_tokens + msg_tokens <= budget:
            kept.insert(0, msg)
            current_tokens += msg_tokens
        else:
            # 尝试压缩 assistant 内容
            compressed = _compress_assistant_message(msg) if msg.get("role") == "assistant" else msg
            compressed_tokens = estimate_tokens(compressed)
            if current_tokens + compressed_tokens <= budget:
                kept.insert(0, compressed)
                current_tokens += compressed_tokens

    return system + tool_results + kept, {
        "strategy": "smart",
        "truncated": current_tokens > budget,
        "kept_tokens": current_tokens,
    }


def _is_tool_result(msg):
    if msg.get("role") != "user":
        return False
    content = msg.get("content", [])
    if isinstance(content, list):
        return any(b.get("type") == "tool_result" for b in content)
    return False


def _compress_assistant_message(msg):
    """将 assistant 消息的推理内容替换为占位符"""
    content = msg.get("content", [])
    if isinstance(content, list):
        compressed_blocks = []
        for b in content:
            if b.get("type") == "tool_use":
                compressed_blocks.append(b)  # 保留 tool_use
            else:
                compressed_blocks.append({"type": "text", "text": "[reasoning omitted]"})
        return {**msg, "content": compressed_blocks}
    return {**msg, "content": "[reasoning omitted]"}


def sum_tokens(messages):
    return sum(estimate_tokens(m) for m in messages)
```

---

### 改进 3：会话续接检测（P1）

**文件**: `anthropic_proxy.py`

**问题**：单次大请求和多轮累积后的续接应使用不同策略。

**改进**：通过 `session_id` 连续请求计数判断续接深度，使用严格截断。

```python
# anthropic_proxy.py

# 模块级会话请求计数
_session_request_count = {}  # session_id → 请求计数


def _classify_lifecycle_stage(messages, session_id=None):
    total_chars = _estimate_message_chars(messages)

    # 判断是否是多轮续接
    is_continuation = False
    if session_id:
        count = _session_request_count.get(session_id, 0)
        is_continuation = count >= 2  # ≥2 次请求视为续接
        _session_request_count[session_id] = count + 1

    # 以下为新增的续接判断分支
    if is_continuation and total_chars > PROXY_CHARS_EXPANSION:
        # 续接 + 大请求：使用更激进的截断配置
        return {
            "stage": "saturation",
            "total_chars": total_chars,
            "frozen_head": 2,
            "clear_zone_pct": 1.0,
            "thinking_keep": 3,
            "truncate_rounds": max(3, PROXY_CTX_KEEP_ROUNDS // 2),  # 激进截断
            "oom_safety": True,
        }

    # 原有阶段判断逻辑...
    if total_chars < PROXY_CLEAR_THRESHOLD:
        return {"stage": "init", ...}
    elif total_chars < PROXY_CHARS_GROWTH:
        return {"stage": "growth", ...}
    # ... 以此类推
```

---

### 改进 4：重命名 `PRE_TRUNCATE` 为 `OOM_SAFE`，降低阈值（P1）

**文件**: `anthropic_proxy.py`

**问题**：`PROXY_PRE_TRUNCATE_CHARS=400K` 阈值过高，多轮累积后仍可能超过后端处理能力。

**改进**：重命名为 `PROXY_OOM_SAFE_CHARS`，阈值降至 200K。

```python
# anthropic_proxy.py

# 旧名称保留兼容，新名称更清晰
PROXY_OOM_SAFE_CHARS = int(os.environ.get(
    "PROXY_OOM_SAFE_CHARS",
    os.environ.get("PROXY_PRE_TRUNCATE_CHARS", "200000")  # 从 400K 降到 200K
))

# 调用位置约 line 3313
if total_chars > PROXY_OOM_SAFE_CHARS and msgs:
    log(f"  -> OOM safety truncation: {total_chars:,} > {PROXY_OOM_SAFE_CHARS:,}")
    msgs_truncated, pre_stats = _apply_rounds_truncation(
        msgs, keep_rounds=2, session_id=pre_session_id
    )
    # ...
```

---

### 改进 5：`manage.sh` 配置传递清理（P2）

**文件**: `manage.sh`

**问题**：`manage.sh` 默认 `PROXY_CTX_TRUNCATE_STRATEGY=char`，但配置文件中设置了 `fifo`/`rounds`，优先级不清晰。

**改进**：改为如果配置文件未设置才用默认值。

```bash
# manage.sh, _start_proxy() 函数，约 line 505

# 旧：
PROXY_CTX_TRUNCATE_STRATEGY="${PROXY_CTX_TRUNCATE_STRATEGY:-char}"

# 新：配置文件先设置，manage.sh 最后兜底
PROXY_CTX_TRUNCATE_STRATEGY="${PROXY_CTX_TRUNCATE_STRATEGY:-${LLAMA_CTX_STRATEGY:-char}}"
```

---

## 5. 优先级与工作量

| 优先级 | 改进 | 工作量 | 理由 |
|--------|------|--------|------|
| **P0** | 改进1: rounds + token 预算 | 小 | 直接修复核心问题 |
| **P1** | 改进4: OOM_SAFE 重命名 + 降阈值 | 小 | 防 OOM 崩溃 |
| **P1** | 改进3: 会话续接检测 | 中 | 区分场景精准截断 |
| **P2** | 改进2: 新增 smart 策略 | 大 | 完整实现 + 测试 |
| **P2** | 改进5: manage.sh 配置传递 | 小 | 减少配置混淆 |

---

## 6. 实施路径

### Phase 1: 紧急修复（P0 + P1）
1. 修改 `rounds` 策略，增加 token 预算二次检查
2. 将 `PROXY_PRE_TRUNCATE_CHARS` 重命名为 `PROXY_OOM_SAFE_CHARS`，阈值降至 200K
3. 添加会话续接检测（简单版本：按 session 请求计数）

### Phase 2: 策略增强（P2）
4. 新增 `smart` 截断策略
5. 清理 `manage.sh` 配置传递
6. 补充单元测试覆盖

### Phase 3: 长期优化
7. 分层配置：大小会话分别优化
8. 动态预算调整（根据历史 OOM 情况自动调参）

### Phase 3 详细设计

> Phase 1+2 已修复核心问题（rounds 预算迭代 / 200K 阈值 / 续接检测 /
> smart 策略）。Phase 3 解决两类长期遗留问题：(a) 不同规模会话共用
> 一组截断参数导致「小会话过度压缩 / 大会话不够压缩」；(b) 配置
> 静态化，无法从历史 OOM 事件中学习。本节给出 4 项具体设计。

#### 设计 P3-1：会话规模分档（small/medium/large）

**目标**：根据 `len(messages) + total_chars` 自动判定会话规模，对小会话放宽
压缩、对大会话收紧压缩，避免一刀切。

**判定阈值**（默认 local 模式，cloud 模式可独立配置）：

| 规模 | 消息数 | 字符数 | 典型场景 |
|------|--------|--------|----------|
| `small` | < 5 | < 10K | 简单问答、首次工具调用 |
| `medium` | 5-30 | 10K-90K | 多轮对话、中等工具链 |
| `large` | ≥ 30 | ≥ 90K | agent 长任务、200+ 轮 |

**每档独立配置**（新增 6 个环境变量，可选覆盖默认）：

| 变量 | 默认 | 含义 |
|------|------|------|
| `PROXY_PROFILE_SMALL_KEEP_ROUNDS` | `20` | small 档保留轮数（宽松） |
| `PROXY_PROFILE_MEDIUM_KEEP_ROUNDS` | `10` | medium 档保留轮数（当前默认） |
| `PROXY_PROFILE_LARGE_KEEP_ROUNDS` | `5` | large 档保留轮数（收紧） |
| `PROXY_PROFILE_LARGE_CLEAR_ZONE_PCT` | `1.0` | large 档清空 tail 比例 |
| `PROXY_PROFILE_LARGE_FROZEN_HEAD` | `2` | large 档 frozen head 数量 |
| `PROXY_PROFILE_LARGE_OM_SAFE` | `true` | large 档启用 OOM safety |

**API**：

```python
# anthropic_proxy.py
def _classify_session_profile(messages, total_chars):
    """Return 'small' | 'medium' | 'large' based on message count and chars."""
    n = len(messages)
    if n < 5 and total_chars < 10_000:
        return "small"
    if n < 30 and total_chars < 90_000:
        return "medium"
    return "large"


def _resolve_profile_overrides(profile):
    """Map a profile name to its config dict. Reads PROXY_PROFILE_* env vars."""
    if profile == "small":
        return {
            "truncate_rounds": int(os.environ.get("PROXY_PROFILE_SMALL_KEEP_ROUNDS", "20")),
            "frozen_head": PROXY_FROZEN_HEAD,
            "clear_zone_pct": 0.0,
            "thinking_keep": 10,
            "oom_safety": False,
        }
    if profile == "medium":
        return {  # current defaults
            "truncate_rounds": PROXY_CTX_KEEP_ROUNDS,
            "frozen_head": max(2, PROXY_FROZEN_HEAD // 2),
            "clear_zone_pct": 1.0,
            "thinking_keep": 3,
            "oom_safety": False,
        }
    # large
    return {
        "truncate_rounds": int(os.environ.get("PROXY_PROFILE_LARGE_KEEP_ROUNDS", "5")),
        "frozen_head": int(os.environ.get("PROXY_PROFILE_LARGE_FROZEN_HEAD", "2")),
        "clear_zone_pct": float(os.environ.get("PROXY_PROFILE_LARGE_CLEAR_ZONE_PCT", "1.0")),
        "thinking_keep": 1,
        "oom_safety": os.environ.get("PROXY_PROFILE_LARGE_OM_SAFE", "true").lower() in ("1", "true", "yes"),
    }
```

**集成点**：
- `_classify_lifecycle_stage()` 在计算完 `stage` 之后，调用 `_classify_session_profile()`
  得到 `profile`，再用 `_resolve_profile_overrides(profile)` 覆盖 stage 默认值。
- 新增 `stage_config["profile"]` 字段，写入 `proxy_metrics.jsonl`。
- 与 Phase 1 「会话续接检测」叠加：续接 + large 档使用最小 `truncate_rounds`（min 2）。

**默认行为**（不开 env var）：
- small 档：保留 20 轮（≈ 60 消息），几乎不压缩
- medium 档：当前默认（10 轮 / 90K 阈值）
- large 档：5 轮 / 2 frozen head / 100% clear

**测试用例**：
- `len=3, chars=5000` → `profile=small`, `truncate_rounds=20`
- `len=15, chars=40000` → `profile=medium`, `truncate_rounds=10`
- `len=100, chars=200000` → `profile=large`, `truncate_rounds=5`
- 续接 + `len=100, chars=200000` → `profile=large`, `truncate_rounds=min(2, 5)=2`

---

#### 设计 P3-2：OOM 率滑动窗口

**目标**：跟踪最近 N 个请求的 OOM 率，为「动态预算调整」提供信号源。

**数据结构**（模块级，per-config 隔离）：

```python
# anthropic_proxy.py
from collections import deque

_OOM_HISTORY = {}  # config_name -> deque of bool (True=oom, False=ok)
_OOM_HISTORY_WINDOW = int(os.environ.get("PROXY_OOM_HISTORY_WINDOW", "50"))


def _record_outcome(config_name, oom):
    """Append a single outcome to the rolling window. Trims to maxlen."""
    if config_name not in _OOM_HISTORY:
        _OOM_HISTORY[config_name] = deque(maxlen=_OOM_HISTORY_WINDOW)
    _OOM_HISTORY[config_name].append(oom)


def _get_oom_rate(config_name):
    """Return (oom_count, total_count, rate) for the current window."""
    dq = _OOM_HISTORY.get(config_name)
    if not dq:
        return 0, 0, 0.0
    oom = sum(dq)
    total = len(dq)
    return oom, total, oom / total if total else 0.0
```

**触发点**：
- **OOM 触发**：`_classify_exception` 返回 `(503, "backend_oom", True)` 时调用 `_record_outcome(config_name, True)`。
- **成功触发**：do_POST 末尾（response 200）调用 `_record_outcome(config_name, False)`。
- **当前 config 名**：通过 `MODEL_NAME` 或 `LLAMA_MODEL` 派生。

**为什么需要 OOM 率**：
- 单次 OOM 可能是偶发，不应立即调参
- 滑动窗口（默认 50 请求）反映最近的趋势
- 高 OOM 率（> 50%）触发降预算；低 OOM 率（< 10%）允许逐步恢复

---

#### 设计 P3-3：动态预算调整

**目标**：根据 OOM 率自动调整 `PROXY_CHARS_EXPANSION` 等关键参数，无需
人工干预。

**算法**：

```python
# anthropic_proxy.py

# Module-level
_PROXY_BUDGET_OVERRIDE = {}  # config_name -> dict of overrides
_BUDGET_MIN_RATIO = float(os.environ.get("PROXY_BUDGET_MIN_RATIO", "0.5"))
_BUDGET_RECOVERY_RATIO = float(os.environ.get("PROXY_BUDGET_RECOVERY_RATIO", "1.0"))


def _compute_budget_multiplier(config_name):
    """Return a multiplier in [BUDGET_MIN_RATIO, BUDGET_RECOVERY_RATIO]
    based on the rolling OOM rate."""
    oom_count, total, rate = _get_oom_rate(config_name)
    if total < 10:  # not enough samples, no adjustment
        return 1.0
    if rate > 0.5:  # heavy OOM, halve the budget
        return _BUDGET_MIN_RATIO
    if rate > 0.3:  # mild OOM, scale down proportionally
        return max(_BUDGET_MIN_RATIO, 1.0 - (rate - 0.3) * 2.0)
    if rate < 0.1 and total >= 30:  # safe for a while, recover
        return _BUDGET_RECOVERY_RATIO
    return 1.0


def get_effective_budget(config_name):
    """Return the effective PROXY_CHARS_EXPANSION after dynamic adjustment."""
    mult = _compute_budget_multiplier(config_name)
    override = _PROXY_BUDGET_OVERRIDE.get(config_name, {})
    return int(override.get("chars_expansion", PROXY_CHARS_EXPANSION) * mult)
```

**应用点**：
- 在 `_classify_lifecycle_stage()` 内部：
  ```python
  eff_budget = get_effective_budget(current_config_name)
  if total_chars > eff_budget:
      # triggers truncation earlier than the static threshold
  ```
- 在 `truncate_messages_if_needed` 的 `rounds` 分支和 `_apply_smart_truncation`
  中使用 `eff_budget` 替代 `PROXY_CHARS_EXPANSION`。

**保护机制**：
- **下限保护**：`_BUDGET_MIN_RATIO=0.5`，预算不会低于原始 50%（防止
  反复 OOM → 降预算 → 又 OOM 的正反馈）。
- **快速恢复**：低 OOM 率（< 10%）持续 30+ 请求后，逐步回到原预算。
- **样本不足**：< 10 个样本时不做调整（避免冷启动误判）。
- **手动覆盖**：env `PROXY_BUDGET_OVERRIDE_CHARS=60000` 直接锁定预算，
  跳过动态逻辑（用于调试）。

---

#### 设计 P3-4：OOM 历史持久化

**目标**：让动态预算跨进程重启学习，避免每次新进程都从空历史开始。

**机制**：

```python
# anthropic_proxy.py
import atexit

_OOM_HISTORY_PATH = os.environ.get(
    "PROXY_OOM_HISTORY_PATH", "logs/oom_history.jsonl")


def _persist_oom_history():
    """Flush _OOM_HISTORY to disk on shutdown."""
    try:
        os.makedirs(os.path.dirname(_OOM_HISTORY_PATH), exist_ok=True)
        with open(_OOM_HISTORY_PATH, "w") as f:
            for config_name, dq in _OOM_HISTORY.items():
                # Each config is a single JSON line for forward compatibility
                f.write(json.dumps({
                    "config": config_name,
                    "history": list(dq),
                    "saved_at": datetime.now().isoformat(),
                }) + "\n")
    except OSError as e:
        log(f"OOM history persist failed: {e}")


def _load_oom_history():
    """Restore _OOM_HISTORY from disk on startup."""
    if not os.path.exists(_OOM_HISTORY_PATH):
        return
    try:
        with open(_OOM_HISTORY_PATH) as f:
            for line in f:
                rec = json.loads(line)
                cfg = rec.get("config")
                hist = rec.get("history", [])
                if cfg and hist:
                    dq = deque(hist, maxlen=_OOM_HISTORY_WINDOW)
                    _OOM_HISTORY[cfg] = dq
    except (OSError, json.JSONDecodeError) as e:
        log(f"OOM history load failed (ignoring): {e}")


# At module import time
_load_oom_history()
# At process shutdown
atexit.register(_persist_oom_history)
```

**写入策略**：
- 每次 do_POST 末尾更新内存中的 deque（高频，不写盘）
- `atexit` 钩子在 proxy 正常退出时一次性持久化（低频）
- 不在每次写盘（避免 I/O 抖动）

**读取策略**：
- 进程启动时一次性加载
- 文件不存在 / 解析失败 → 静默忽略（不阻断启动）
- 旧版本的 `oom_history.jsonl` 格式变化时，通过 `try/except` 兼容

**清理策略**：
- `logs/oom_history.jsonl` 滚动：超过 1MB 时按时间截断最近 50 行
- 不影响 in-memory 状态（磁盘只用于冷启动恢复）

---

#### 设计 P3-5：可观测性扩展

**目标**：让 OOM 动态调整对用户透明，便于诊断误判。

**新增 metrics 字段**（写入 `proxy_metrics.jsonl`）：

| 字段 | 含义 |
|------|------|
| `session.profile` | small / medium / large |
| `oom_history.count` | 当前窗口中 OOM 次数 |
| `oom_history.total` | 当前窗口总请求数 |
| `oom_history.rate` | 0.0-1.0 浮点 |
| `effective_budget_chars` | 动态调整后的实际预算 |
| `budget_multiplier` | 当前应用的乘数 |

**日志格式**（`do_POST` 末尾）：

```
-> Session profile=large (100 msgs, 200K chars), oom_rate=0.42 (21/50), budget=45000 (mult=0.5)
```

**调试环境变量**：
- `PROXY_OOM_DIAGNOSTICS=true` → 每 10 个请求打印 OOM 率 + 当前乘数
- `PROXY_BUDGET_OVERRIDE_CHARS=60000` → 锁定预算，跳过动态逻辑

**测试用例**：
- 50 个 OOM 请求后，`get_effective_budget` 返回 `PROXY_CHARS_EXPANSION * 0.5`
- 50 个成功请求后，恢复到 `PROXY_CHARS_EXPANSION * 1.0`
- 进程重启后历史恢复，budget 仍然使用降级值

---

### Phase 3 优先级与工作量

| 优先级 | 设计 | 工作量 | 理由 |
|--------|------|--------|------|
| **P3-1** | 会话规模分档 | 中 | 解锁「小会话过度压缩」 |
| **P3-2** | OOM 率滑动窗口 | 小 | 信号源，单纯计数器 |
| **P3-3** | 动态预算调整 | 中 | 核心价值，但需要 P3-2 支撑 |
| **P3-4** | OOM 历史持久化 | 小 | atexit + JSONL，简单 |
| **P3-5** | 可观测性扩展 | 小 | 纯指标 + 日志 |

**推荐实施顺序**：P3-2 → P3-4 → P3-3 → P3-1 → P3-5
（P3-2/4 是基础设施，先做；P3-3 是核心价值；P3-1 独立可选；P3-5 最后做可观测性）

---

## 7. 测试验证计划

### 单元测试
- `rounds` 策略 + token 超预算场景
- `smart` 策略消息分类正确性
- 会话续接检测计数正确性

### 集成测试
- 多轮累积（50+ 消息）后的截断效果
- 工具调用历史中的 tool_result 保留验证
- OOM_SAFE 触发后请求仍有效

### 压力测试
- 80K 历史 + 80K 新内容的会话续接场景
- 200+ 消息的连续工具调用场景
- 与 4-bit KV 量化配置联合测试

---

## 8. 相关文档

- `proxy-pipeline-reference.md` — 8 层代理 pipeline 详细设计
- `proxy-context-window-design.md` — 上下文窗口设计原始文档
- `proxy-context-window-design-review-merged.md` — 设计评审合并版
- `DEFECT-LIST.md` — 已知缺陷清单（DEF-001 DEF-005 等）
