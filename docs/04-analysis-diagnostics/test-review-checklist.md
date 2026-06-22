# 测试审查检查清单

> 目标：在 5 分钟内发现 80% 的测试设计问题
> 适用：Code Review 中的测试文件变更、新增 Test Class / TestCase
> 原则：审查测试**意图**而非测试**实现**，检查「代码写了什么」 vs 「代码应该做什么」

---

## 第一步：测试命名审查（30 秒）

测试方法名描述的是「应该的行为」还是「当前的行为」？

| ❌ 描述当前行为（危险） | ✅ 描述应该行为（安全） |
|------------------------|------------------------|
| `test_aliases_include_new_model` | `test_aliases_never_expose_model_name` |
| `test_returns_empty_list` | `test_returns_empty_list_when_no_data` |
| `test_uses_legacy_path` | `test_falls_back_to_legacy_path_when_new_unavailable` |

**检查方法**：把测试名中表示行为的动词部分取反，如果测试名仍然合理，说明测试名描述的是实现而非意图。

---

## 第二步：断言反转测试（1 分钟）

对每条 `assertIn` / `assertEqual` / `assertTrue`，假想将其反向（`assertNotIn` / `assertNotEqual` / `assertFalse`），测试是否仍然通过？

### 检查清单

```python
# ❌ 红绿反转（极端危险）
self.assertIn(MODEL_NAME, MODEL_ALIASES)
# 反转后 → assertNotIn(MODEL_NAME, MODEL_ALIASES) 也能通过吗？
# 如果能 → 测试不保护任何行为，必须修正

# ✅ 非反转（安全）
self.assertEqual(stage._backend_status, 200)
# 反转后 → self.assertNotEqual(stage._backend_status, 200) 会失败 → 有效

# ✅ 边界值（安全）
self.assertGreater(response_time, 0)
# 反转后 → assertLess 会失败 → 有效

# ⚠️ 模糊断言（低风险）
self.assertIn("error", response_text)
# 反转后 → 不一定失败（"error" 可能出现在其他位置）
# 建议改为更精确的断言：assertEqual(response["error"]["code"], 503)
```

### 常见危险模式速查

| 断言模式 | 反转风险 | 建议 |
|---------|---------|------|
| `assertIn(MODEL_NAME, list)` | ⚠️ 高 | 确认 list 真的应该包含 |
| `assertEqual(actual, "default")` | ✅ 低 | 字符串常量安全 |
| `assertTrue(result)` | ⚠️ 中 | 确认 result 是 True 而非 truthy 值 |
| `assertIsNotNone(x)` | ✅ 低 | 简单 null 检查安全 |
| `assertGreater(count, 0)` | ✅ 低 | 明确不等式 |

---

## 第三步：Mock 安全审查（1 分钟）

Mock 是否移除了被测系统的**安全保护层**？

### 危险 Mock 清单

```python
# ❌ 危险：Mock 了锁/信号量 → 并发问题不可检测
self._mock_lock = MagicMock()
# 保护层: threading.Semaphore 被替换为无行为 mock
# 后果: 即使不调用 with self._lock: 测试也通过 (P0#1)

# ✅ 安全：使用 real Semaphore(1)
self._cloud_lock = threading.Semaphore(1)
# 保护层真实存在 → 如果代码没获取锁，并发测试会卡死

# ⚠️ 值得警惕的 Mock 目标:
# - threading.Semaphore
# - threading.Lock
# - threading.Condition
# - threading.RLock
# - any __enter__ / __exit__ (context manager)

# ✅ 安全的 Mock 目标:
# - urllib.request.urlopen (外部 I/O)
# - time.time / time.monotonic (时间相关)
# - os.environ (配置)
# - open / os.listdir (文件系统)
# - random (不可预测)
```

### 替换标准

```python
# 安全模式：对安全层使用 real 对象
import threading

class TestBackendDispatcherSafe(unittest.TestCase):
    def setUp(self):
        # 改为 real Semaphore，不是 mock
        self._cloud_lock = threading.Semaphore(1)
        self._llama_lock = threading.Semaphore(1)
        self._handler = MagicMock()  # Handler 可以 mock（不是安全层）
    
    def test_cloud_concurrency_enforced(self):
        """验证 cloud 请求在锁保护下执行。"""
        self._cloud_lock.acquire()  # 预占锁
        stage = BackendDispatcher(
            cloud_lock=self._cloud_lock,
            llama_lock=self._llama_lock,
            handler=self._handler,
        )
        ctx = self._make_ctx(target="cloud")
        
        with patch("pipeline.urllib.request.urlopen") as mock_open:
            thread = threading.Thread(target=stage.process, args=(ctx,))
            thread.start()
            time.sleep(0.05)  # 给线程启动时间
            mock_open.assert_not_called()  # 锁被占，不应发出请求
            self._cloud_lock.release()
            thread.join(timeout=1)
            mock_open.assert_called_once()  # 锁释放后请求发出
```

---

## 第四步：生命周期完整性（1 分钟）

如果测试只验证「激活」不验证「清除」——这是**静态快照测试**：

```python
# ❌ 静态快照：只测激活
def test_cooldown_active_forces_local(self):
    _ps._cloud_cooldown_start["sess"] = time.monotonic()
    SmartRouter().process(ctx)
    self.assertEqual(ctx._route_target, "local")  # ✅ 激活对了
    # ❌ 但冷却期过期后状态是否清理？无验证

# ✅ 生命周期完整：激活 + 过期 + 清理
def test_cooldown_expired_cleans_all_state(self):
    # 阶段 1: 激活冷却期
    with patch("time.monotonic", return_value=100.0):
        _ps._cloud_cooldown_start["sess"] = time.monotonic()
        _ps._cloud_fail_count["sess"] = 3
        # 阶段 2: 模拟冷却期过期（时间推进）
    with patch("time.monotonic", return_value=2000.0):  # 1800s 后
        ctx = PipelineContext(session_id="sess", ...)
        SmartRouter().process(ctx)
    # 阶段 3: 验证清理
    self.assertNotIn("sess", _ps._cloud_cooldown_start)
    self.assertNotIn("sess", _ps._cloud_fail_count)
    self.assertNotIn("sess", _ps._SESSION_ROUTE_MAP)
```

### 生命周期检查模板

对每个修改状态的测试，画一条简单的时间线：

```
[开始] → [操作] → [中间状态] → [反向操作] → [结束状态]
                                              ↓
                                    测试必须覆盖此行
```

**标准检查**：
- 如果测试 `set` 了某值，是否有配套的 `clear` / `pop` / `reset` 测试？
- 如果测试注入了状态，是否有「状态自然过期/消失」的测试？
- 如果测试验证了「A 发生时 → B」，是否有「A 结束后 → 回到非 B」的测试？

---

## 第五步：覆盖门禁检查（30 秒）

### 函数 → 测试映射

```bash
# 扫描新增函数是否被测试覆盖
# 在 pre-commit 或 CI 中自动执行
git diff HEAD --name-only | while read modified; do
    grep -oP "(?<=^def )\w+" "$modified" | while read func; do
        if ! grep -q "$func" test/unit/*.py; then
            echo "⚠️  $modified:$func 缺少单元测试"
        fi
    done
done
```

### 特殊关注函数

| 函数类型 | 必须测试 | 原因 |
|---------|---------|------|
| 纯函数（无 I/O） | ✅ | 最容易测试，收益最高 |
| 状态修改函数 | ✅ | 易隐藏生命周期问题 |
| 成本/财务计算 | ✅ | 精度错误不易察觉 |
| 配置校验/构建 | ✅ | 常因修改不同步而失效 |
| 上下文管理器 | ✅ | 离开时的清理代码常被忽略 |

---

## 汇总表格：5 步检查清单

| 步骤 | 耗时 | 检测目标 | 自动化 |
|------|------|---------|--------|
| ① 测试命名 | 30s | 红绿反转（高危） | 命名模式扫描 |
| ② 断言反转 | 1min | 红绿反转（高危） | 半自动（需人工判断） |
| ③ Mock 安全 | 1min | 保护层移除（高危） | 检出 `MagicMock()` + lock/semaphore |
| ④ 生命周期 | 1min | 静态快照（中危） | 人工时间线检查 |
| ⑤ 覆盖门禁 | 30s | 白盒空隙（低危） | 函数名 vs 测试名扫描 |

> **执行时间**: ~4 分钟/PR。不单独开会，Code Review 中的 5 步快速检查。
> **拦截率**: 预计拦截 80% 以上的测试设计缺陷（基于本次 Code Review 的 12 项问题回溯验证）。
> **例外**: 正式 Bug Bash 前仍建议做一次深度测试审计（类似本次分析报告），发现系统性模式问题。
