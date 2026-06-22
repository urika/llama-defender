# 单元测试覆盖分析报告

> **日期**: 2026-06-22
> **范围**: `test/unit/` — 17 文件, 705 tests, 5.84s
> **审查触发**: Code Review 发现 12 项问题，仅 3 项被测试捕获（且其中 2 项断言的是错误行为）

---

## 1. 覆盖现状

### 1.1 测试分布

| 模块 | 测试文件 | 用例数 | 覆盖内容 |
|------|---------|--------|---------|
| SmartRouter / RouteNotification | `test_smart_router.py` | 42 | 决策矩阵全 10 级、RouteNotification 注入、FormatConverter 路由 |
| BackendDispatcher | `test_pipeline_stages.py` | 15 | local/cloud 分发、失败回退、敏感路径阻断、cooldown 激活 |
| Proxy Reload | `test_proxy_reload.py` | 27 | config 解析、reload 同步、Semaphore 重建、ModelAliases |
| Proxy State | `test_proxy_state.py` | 30 | 导入、config 校验、正则 mock、`__all__` 覆盖 |
| Pipeline 集成 | `test_pipeline_stages.py` | 5 | 0→7 stage 链、truncation 链 |
| 其他 | 12 文件 | 586 | converter、compressor、loop、tool_parser、logger 等 |

### 1.2 新增代码覆盖率估算

智能路由新增代码约 512 行（pipeline.py）+ 112 行（proxy_state.py）+ 72 行（admin_server.py）≈ **696 行新增**。

| 区域 | 总行数 | 被测试 | 覆盖率(估) |
|------|--------|--------|-----------|
| `SmartRouter._routing_decision` | 85 | 10 级矩阵 + 边界 | ~90% |
| `SmartRouter.output_metrics` | 16 | agent_tier/route_bias **未测** | ~60% |
| `RouteNotification` | 60 | first/emergency/once | ~85% |
| `BackendDispatcher` (cloud) | 75 | 成功/回退/阻断/cooldown | ~80% |
| `BackendDispatcher._do_dispatch` | 35 | **锁获取未测 / cost 准确性未测** | ~40% |
| `proxy_state.get_model_aliases` | 18 | 6 用例 | ~95% |
| `_accumulate_route_daily_cost` | 14 | **0 用例** | 0% |
| `_compile_sensitive_patterns` | 25 | 1 用例 | ~50% |

---

## 2. 未发现问题的根因分析

### 2.1 模式一：红绿反转测试（Tests inverted）

最严重的模式。测试 **断言了 bug 是正确的行为**：

```python
# test_proxy_reload.py:279 — 旧代码
def test_aliases_include_new_model(self):
    """MODEL_ALIASES is rebuilt to include the new MODEL_NAME."""
    ...
    self.assertIn("test-model-v2", proxy.MODEL_ALIASES)
    #        ^^^^^^^^^^^^^^^^^^^^^^^^
    # 测试断言 MODEL_NAME 泄露给 Agent 是「正确」行为
```

受影响测试：

| 测试 | 断言内容 | 对应 Bug |
|------|---------|---------|
| `test_aliases_include_new_model` | MODEL_NAME ∈ ModelAliases | P0#3 MODEL_NAME 泄露 |
| `test_proxy_state_aliases_rebuilt` | MODEL_NAME ∈ ModelAliases | P0#3 MODEL_NAME 泄露 |
| `test_model_aliases` (proxy_state) | MODEL_NAME ∈ ModelAliases | P0#3 MODEL_NAME 泄露 |

**根因**: 测试基于「当前实现」而非「设计需求」编写。测试文档没有反映设计文档 §9.3.1 的规范（ModelAliases 不应包含 MODEL_NAME）。

---

### 2.2 模式二：快乐路径综合症（Happy-path-only）

测试使用 **Mock 锁而非真实锁**，从不验证锁的获取行为：

```python
# test_pipeline_stages.py — BackendDispatcher 测试
self._mock_lock = MagicMock()
self._mock_lock.__enter__ = MagicMock(return_value=None)
self._mock_lock.__exit__ = MagicMock(return_value=None)
```

这导致：
1. **P0#1（并发控制丢失）不可检测** — mock lock 即使不调用 `with lock:` 也不会报错
2. 测试验证了「发送了 HTTP 请求」，但没验证「请求是否在锁保护下发送」

**对比测试 → 生产代码的关键路径**：

```
测试流程:
  stage.process(ctx)
    → _do_dispatch()          ← 直接调用，不检查锁
    → urlopen(mock)           ← mock 返回成功
    → assert urlopen called   ← ✅ 测试通过

生产流程:
  Thread A: _do_dispatch()    ← 无锁，直接进入
  Thread B: _do_dispatch()    ← 同时进入
    → 两个 concurrent urlopen → Metal OOM / API 不限流
```

**根因**: 测试替身（mock）移除了被测对象的保护层。测试验证的是「mock 世界」而非「生产世界」。

---

### 2.3 模式三：静态快照测试（Stateless snapshot）

测试验证**单次操作**的状态，不验证**完整生命周期**：

```python
# test_smart_router.py — Cooldown 测试
def test_cooldown_active_forces_local(self):
    _ps._cloud_cooldown_start["sess_cool"] = time.monotonic()
    #    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ 直接注入状态
    ctx = SmartRouter().process(ctx)
    self.assertEqual(ctx._route_target, "local")
    self.assertIn("cloud_cooldown_active", ctx._route_reason)
```

缺失的测试：

| 测试 | 应验证 | 未验证 |
|------|--------|--------|
| Cooldown 激活 | 激活后 → local | P0#4 cooldown 过期后 session 状态是否清理 |
| Header override | 单次覆盖有效 | Header 完成后 `_route_header_override` 是否被清除 |
| Session lifecycle | new→cloud→stay cloud→new→local | **已覆盖** ✅ |

**P0#4 在现有测试中不可检测** — 测试直接注入 `_cloud_cooldown_start[session]`，但不测试冷却期过期后的状态清理。这需要时间模拟（`time.monotonic()` + `sleep` 或 mock time）。

---

### 2.4 模式四：白盒覆盖空隙（Coverage gaps）

以下关键函数和路径完全没有单元测试：

| 函数/路径 | 位置 | 发现的问题 | 原因 |
|-----------|------|-----------|------|
| `_accumulate_route_daily_cost` | proxy_state.py:470 | P0#2 两个版本并存 | 写了测试但没加 |
| `BackendDispatcher._accumulate_daily_cost` | pipeline.py:1895 | 调用 `_accumulate_route_daily_cost` → 未验证 | 无 |
| `SmartRouter.output_metrics` | pipeline.py:547 | 缺少 `agent_tier` / `route_bias` → 未发现 | metrics 字段验证不严格 |
| `get_model_aliases()` 线程安全 | proxy_state.py:491 | 无并发测试 | 单线程测试环境 |
| `__all__` 完整性 | proxy_state.py:690 | 32 个新名称不在 `__all__` | 已有 `test_all_covers_*` 但只检查了旧名称 |

---

### 2.5 模式五：集成边界未测试（Integration boundary gap）

不同模块之间的数据流未经交叉验证：

```python
# reload_config.py — 「自己的」ModelAliases 构建
aliases = ["claude-3-5-sonnet-20241022", ..., "default", model]

# proxy_state.py — get_model_aliases() 构建
aliases = ["claude-sonnet-4-6", ..., "claude-3-5-haiku-20241022"]

# anthropic_proxy.py — /v1/models handler
aliases = _ps.get_model_aliases()  # ← 用了 get_model_aliases
# 但 reload_config.py 用内联列表，两个不同源
```

缺失的测试：「reload 后，`/v1/models` 的响应是否等于 `get_model_aliases()`」。这需要集成测试（启动 mock proxy + 发送 HTTP 请求）。

---

## 3. 具体测试缺口清单

### 3.1 P0 级别缺口（导致已发现的 P0 问题）

| 缺口 | 问题 | 应写的测试 |
|------|------|-----------|
| 锁获取验证 | P0#1 并发丢失 | `test_concurrency_lock_acquired` — 验证 `_cloud_lock.acquire()` 被调用 |
| 成本函数覆盖 | P0#2 两个版本并存 | `test_accumulate_daily_cost_uses_both_input_output` |
| 配置与代码一致性 | P0#3 MODEL_NAME 泄露 | `test_reload_sync_with_get_model_aliases` — reload 后对比两个源 |
| 冷却期生命周期 | P0#4 状态未清理 | `test_cooldown_expired_cleans_all_state` — mock time |

### 3.2 P1 级别缺口

| 缺口 | 问题 | 应写的测试 |
|------|------|-----------|
| metrics 字段完整 | P1#7 缺少 agent_tier | `test_output_metrics_contains_agent_tier` |
| SIGHUP 缓存同步 | P1#5 敏感路径缓存 | `test_reload_invalidates_sensitive_cache` |
| `/status` 实时状态 | P1#6/#8 Active Sessions | `test_status_shows_active_sessions` |

### 3.3 P2 级别缺口

| 缺口 | 问题 | 应写的测试 |
|------|------|-----------|
| 线程安全 | P2#9 缓存竞态 | `test_get_model_aliases_concurrent` — 10 线程并发调用 |
| `__all__` 完整性 | P2#10 缺少新名称 | `test_all_covers_route_vars` — 正则扫描 `PROXY_ROUTE_*` |
| 成本动态计算 | P2#11 硬编码 | `test_route_notice_cost_format` — 验证消息中包含动态计算的成本 |

---

## 4. 建议的测试改进

### 4.1 测试基础设施改进

```python
# 问题: mock 锁不检测获取行为
self._mock_lock = MagicMock()
self._mock_lock.__enter__ = MagicMock(return_value=None)

# 改进: 使用 real Semaphore(1) 并在测试中同步验证
import threading
self._real_lock = threading.Semaphore(1)
ctx = ...  # 用 real lock 构造 BackendDispatcher
stage = BackendDispatcher(cloud_lock=self._real_lock, ...)

# 然后在另一个线程尝试 acquire(blocking=False)
other = self._real_lock.acquire(blocking=False)
self.assertFalse(other, "lock should be held during dispatch")
```

### 4.2 新增测试清单

按照 Priority 排序：

```python
# P0-urgent: 锁获取验证（防止回归）
def test_local_dispatch_acquires_llama_lock(self):
    """_do_dispatch 必须在 _llama_lock 保护下执行。"""
    real_lock = threading.Semaphore(1)
    real_lock.acquire()  # 预占锁
    ctx = self._make_ctx(target="local")
    stage = BackendDispatcher(llama_lock=real_lock, ...)
    with patch("pipeline.urllib.request.urlopen") as mock_open:
        thread = threading.Thread(target=stage.process, args=(ctx,))
        thread.start()
        time.sleep(0.05)
        mock_open.assert_not_called()  # 锁被占，不应发出请求
        real_lock.release()
        thread.join(timeout=1)
        mock_open.assert_called_once()  # 锁释放后请求发出

# P0-urgent: 冷却期过期清理
@patch("time.monotonic")
def test_cooldown_expired_cleans_all_state(self, mock_time):
    mock_time.side_effect = [100.0, 100.0, 2000.0]  # now, then future
    _ps._cloud_cooldown_start["sess"] = 100.0
    _ps._cloud_fail_count["sess"] = 3
    _ps._SESSION_ROUTE_MAP["sess"] = "local_forced"
    
    ctx = PipelineContext(session_id="sess", ...)
    SmartRouter().process(ctx)
    
    self.assertNotIn("sess", _ps._cloud_cooldown_start)
    self.assertNotIn("sess", _ps._cloud_fail_count)
    self.assertNotIn("sess", _ps._SESSION_ROUTE_MAP)

# P1: metrics 字段完整性
def test_smart_router_metrics_has_agent_tier(self):
    metrics = SmartRouter().output_metrics(ctx)
    self.assertIn("agent_tier", metrics)
    self.assertIn("route_bias", metrics)

# P1: 成本函数覆盖
def test_accumulate_route_daily_cost_input_output(self):
    total = _ps._accumulate_route_daily_cost(1000, 500)
    expected = 1000 * 0.5 / 1_000_000 + 500 * 1.5 / 1_000_000
    self.assertAlmostEqual(total, expected, places=6)
```

### 4.3 预提交钩子增强

当前 `.githooks/pre-commit` 运行 `--unit` 但不检查测试名称命名规范。建议添加检查：

```bash
# 预提交: 检查新模式是否缺少测试
git diff --cached --name-only | grep 'proxy_state.py$' && \
  grep -q "PROXY_ROUTE_" proxy_state.py && \
  ! grep -q "test_proxy_route\|test_smart_router\|test_model_aliases" test/unit/test_proxy_state.py && \
  echo "⚠️ 新增 PROXY_ROUTE_* 配置但未添加单元测试" && exit 1
```

---

## 5. 总结

### 五大测试失败模式

```
模式                              | 影响 | 对应 P0/P1 | 修复方向
───────────────────────────────────┼──────┼───────────┼──────────────────────
① 红绿反转（测试断言 bug 正确）     | 致命 | P0#3      | 测试基于设计规范而非当前代码
② 快乐路径（mock 移除保护层）       | 致命 | P0#1      | 使用 real Semaphore + 并发验证
③ 静态快照（不测完整生命周期）       | 严重 | P0#4      | 时间模拟 + 状态清理验证
④ 白盒空隙（未覆盖函数/路径）        | 严重 | P0#2, P1#7 | 补全单元测试
⑤ 集成边界（跨模块一致性未验证）     | 中等 | —         | 集成测试 + 交叉断言
```

### 当前测试有效性评分

| 维度 | 评分 | 说明 |
|------|------|------|
| 功能覆盖 | ⭐⭐⭐⭐ | 决策矩阵 10 级覆盖 90% |
| 边界条件 | ⭐⭐⭐ | 临界值、空值、异常路径覆盖好 |
| **回归防护** | **⭐⭐** | P0#1/P0#3 回归测试失效 |
| 并发安全 | ⭐ | 无并发测试，mock 锁不可检测 |
| 生命周期 | ⭐⭐⭐ | 缺少冷却期过期、Session 清理 |
| 测试自检 | ⭐ | 红绿反转问题显示测试缺乏「测试测试」机制 |

> **底线**: 705 个测试覆盖率数字好看（5.84s 跑完），但 P0#1（并发丢失）仍可通过测试流水线 — 因为测试替身移除了被测系统的安全层。这不是 705/705 的问题，是 705/705 的**幻觉**。
