# Cloud Cooldown 机制重新评估与调整

**日期**: 2026-06-23  
**相关模块**: `pipeline.py`、`proxy_state.py`、`proxy_config.py`、`configs/rapid-mlx-35b-opt.conf`  
**背景**: 本地后端因大上下文请求触发 Metal OOM 崩溃，诱因是会话被 cloud cooldown 锁定在本地 30 分钟。

## 原机制问题

1. **Cooldown 是硬锁**：一旦云 API 连续失败 3 次，会话在 30 分钟内被强制路由到本地，优先级高于上下文大小、内存压力、生命周期阶段等所有安全条件。
2. **本地失败无法突围**：如果本地后端此时也 OOM/不可用，代理不会自动再切回云端，而是直接返回 503。
3. **Cooldown 时间过长**：默认 1800 秒（30 分钟），对于本地后端偶发 OOM 的恢复场景过长。
4. **错误类型不区分**：401/403 等认证错误也会触发 cooldown，导致配置错误时直接锁死本地。

实际后果：

```text
[smart_router] local (cloud_cooldown_active)
...
[METAL] Command buffer execution failed: Insufficient Memory
```

一个本应按阈值（>90K 字符）走云端的 145K 字符请求，被硬锁压到本地，导致后端崩溃。

## 调整方案

### 1. Cooldown 改为「本地偏好」而非硬锁

`SmartRouter` 先按原有优先级（会话状态、内存压力、上下文阈值、生命周期阶段）计算自然目标；仅在自然目标是本地时才因 cooldown 选择本地。安全条件（大上下文、高内存、oom_danger 阶段）仍可覆盖 cooldown，将请求路由到云端。

调整后的决策顺序：

```text
自然目标 = 按会话/预算/内存/阈值/生命周期计算
if 自然目标 == cloud:
    if cooldown 激活:
        return cloud, reason + "_cooldown_override"
    return cloud, reason
if 自然目标 == local 且 cooldown 激活且非强制本地/预算超限:
    return local, cloud_cooldown_active
return local, reason
```

### 2. 本地后端失败时自动清除 cooldown 并回退云端

`BackendDispatcher` 捕获本地 `HTTPError` 和 `URLError`：

- 若用户未手动强制本地，且 fallback 开启，且请求不包含敏感路径，则：
  - 清除该会话的 cloud cooldown；
  - 移除 `local_forced(cloud_failures)` 标记；
  - 将请求重试到云端。

这样即使会话处于 cooldown，本地 OOM 也不会锁死请求。

### 3. 仅可重试错误触发 cooldown

_cloud failure 触发 cooldown 的条件收紧为：

- `URLError`（连接/超时等网络问题）
- HTTP `408/429/500/502/503/504`

401/403 等认证/授权错误不再触发 cooldown。

### 4. 默认 cooldown 时长缩短

| 参数 | 旧默认值 | 新默认值 | 说明 |
|------|----------|----------|------|
| `PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS` | 1800 | 300 | 从 30 分钟缩短到 5 分钟 |

`configs/rapid-mlx-35b-opt.conf` 同步改为 `300`。

### 5. 强制本地仍保留硬锁

通过 `/admin/route/force-local` 或模型偏好 `prefer_local` 强制本地时，即使本地失败也不会 fallback 到云端；这是用户显式选择，避免绕过安全策略。

## 验证日志示例

大上下文 + cooldown 激活时，现在会走云端：

```text
[smart_router] cloud (chars_exceed_threshold(145788>90000)_cooldown_override)
```

本地 OOM 后，下一个请求自动清除 cooldown 并回云：

```text
<- Local backend failed (Connection refused), checking fallback...
-> Fallback to cloud backend (deepseek-v4-flash)
```

## 相关改动

- `pipeline.py`
  - `SmartRouter._routing_decision` 拆分出 `_natural_routing_decision`
  - cooldown 作为最终偏好应用
  - `BackendDispatcher` 本地失败时 fallback 到云端并清除 cooldown
  - `_record_cloud_failure` 仅对可重试错误计数
- `proxy_state.py`
  - `PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS` 默认值改为 `300`
  - `_RELOAD_SPEC` 同步
- `proxy_config.py`
  - `CONFIG_REGISTRY` 默认值同步
- `configs/rapid-mlx-35b-opt.conf`
  - `PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS=300`
- `test/unit/test_smart_router.py`
  - 更新 cooldown 测试：本地偏好、阈值覆盖、生命周期覆盖、手动本地硬锁
- `test/unit/test_pipeline_stages.py`
  - 新增：401 不触发 cooldown、本地失败回退云端、手动本地不 fallback

## 监控建议

1. 关注日志中 `cloud_cooldown_active` 是否仍在大上下文请求中出现——应基本消失。
2. 关注 `local_failure_fallback` 频率，若频繁出现说明本地后端负载/内存需要进一步调优。
3. 关注云端成本：缩短 cooldown 和大上下文走云会增加 cloud API 调用，建议配合 `PROXY_ROUTE_DAILY_BUDGET` 使用。
