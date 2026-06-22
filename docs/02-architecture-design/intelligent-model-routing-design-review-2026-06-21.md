# 设计文档 Review：智能模型路由 (v2.0)

> **被评审文档**: `docs/02-architecture-design/intelligent-model-routing-design.md` v2.0  
> **关联 PRD**: `docs/01-requirements-product/PRD-intelligent-model-routing.md` v1.3  
> **评审日期**: 2026-06-21  
> **评审视角**: 产品经理 + 轻技术视角  
> **评审结论**: 基本通过，需修正关键实现细节后进入开发

---

## 一、总体判断

设计文档 v2.0 整体质量高，架构清晰、考虑周全，具备进入开发阶段的基本条件。

**主要优点**:
- SmartRouter 插入位置合理（LifecycleClassifier 之后、内容处理之前）
- Session 路由状态管理清晰
- 云端回退流程设计避免了 BackendDispatcher 内部嵌套锁的死锁风险
- Cloud Stage Skip 机制明确
- 配置 Profile 系统为 Phase 2 预留了扩展空间

**待解决的关键问题**:
- BackendDispatcher fallback 实现方式存在歧义
- Emergency truncation 触发时机与 ContextTruncator 执行顺序有矛盾
- `PROXY_CLOUD_API_KEY` 默认值错误
- 决策矩阵与伪代码不完全一致
- PipelineContext 存在重复字段

---

## 二、架构设计层面的问题

### 2.1 Stage 编号不一致

文档中 Stage 列表出现：
- Stage 0: RequestParser
- Stage 1: LifecycleClassifier
- Stage 2: DynamicMaxTokens
- Stage 1.5: SmartRouter

**问题**: 编号方式不一致，容易造成实现时的混乱。

**建议**: 在代码实现中不依赖数字编号，而是使用 Stage 名称列表。文档中可保留"插入在 LifecycleClassifier 之后、ErrorTranslator 之前"的描述，不强制使用 Stage 1.5 这个编号。

---

### 2.2 HighDropRatioNotice 改为 ConditionalStage 的影响

§1.5 提到 `HighDropRatioNotice` 当前继承 `PipelineStage`，需要改为 `ConditionalStage`。

**问题**: 需要验证该 Stage 是否依赖 `ctx.trunc_stats` 字段，以及本地路径中 `trunc_stats` 为 None 时是否会正确跳过。

**建议**: 在 `HighDropRatioNotice.should_run` 中明确实现：
```python
def should_run(self, ctx):
    if ctx._route_target == "cloud":
        return False
    return ctx.trunc_stats is not None
```

---

## 三、数据流设计层面的问题

### 3.1 回退流程与 InstrumentedPipeline 的协作存在歧义

§2.3 描述的回退流程：
> "BackendDispatcher 检测到 cloud HTTPError → 修改 ctx._route_target = 'local_forced' → return ctx → InstrumentedPipeline 检测到当前 Stage 是 BackendDispatcher 且 ctx._route_target == 'local_forced' → 重新执行 BackendDispatcher.process(ctx)"

**问题**: `InstrumentedPipeline.run()` 的常规实现是顺序遍历 stages，一个 stage 只执行一次。要在同一个请求中重新执行 BackendDispatcher，需要修改 Pipeline 框架的通用逻辑。

**建议明确实现方式**:

**方案 A（推荐）**: BackendDispatcher 内部处理 fallback
```python
def process(self, ctx):
    if ctx._route_target == 'cloud':
        try:
            self._dispatch_cloud(ctx)
        except urllib.error.HTTPError:
            self._record_cloud_failure(ctx)
            ctx._route_target = 'local_forced'
            ctx._emergency_fallback = True
            self._dispatch_local(ctx)
    elif ctx._route_target in ('local', 'local_forced'):
        self._dispatch_local(ctx)
```

**方案 B**: 修改 InstrumentedPipeline 支持 stage 重试
如果坚持方案 B，需要明确 `InstrumentedPipeline.run()` 的修改方式，并给出伪代码。

**PM 判断**: 建议采用方案 A，修改面更小，metrics 也更连续。

---

### 3.2 回退时的锁顺序

§2.2 提到避免嵌套锁的死锁风险，但无论是内部重试还是重新提交，都需要注意：

- Cloud lock 和 local lock 是独立的 semaphore
- 如果当前线程持有 cloud lock，然后要获取 local lock，必须先释放 cloud lock

**建议**: 在 BackendDispatcher 中明确实现：
```python
with self._cloud_lock:
    try cloud dispatch
if cloud failed and need fallback:
    # cloud lock already released
    with self._llama_lock:
        local dispatch
```

---

### 3.3 Emergency truncation 触发时机矛盾

`_emergency_fallback` 在 BackendDispatcher 中设置，但 ContextTruncator 在 BackendDispatcher 之前执行。

**关键矛盾**:
- 如果采用 BackendDispatcher 内部 fallback 方案，ContextTruncator 已经执行过，无法读取 `_emergency_fallback`
- 如果采用 Pipeline 重新提交方案，可以解决这个矛盾，但需要付出 metrics 分裂或 pipeline 重试的代价

**建议**:
- 如果采用 Pipeline 重新提交：明确 new request_id 的处理方式、metrics 如何关联、如何避免无限循环
- 如果采用 BackendDispatcher 内部 fallback：需要在 BackendDispatcher 内部调用截断函数，或在 SmartRouter 之前增加"预截断"Stage

---

## 四、PipelineContext 字段设计

### 4.1 `_route_notified` 字段位置不当

§2.4 把 `_route_notified` 放在 PipelineContext 中，但 §1.6 的实现又使用了 `_ps._route_notified_{session_id}` 来标记每个 Session 的通知状态，两个机制重复。

**建议**:
- 保留 `_ps._route_notified_{session_id}` 作为跨请求状态（正确）
- 从 PipelineContext 中删除 `_route_notified` 字段，避免混淆
- RouteNotification Stage 直接读写 `_ps` 中的标记

---

### 4.2 `_route_cloud_model` 字段

§2.4 新增 `_route_cloud_model` 用于 metrics。

**问题**: §3.1 配置中 `PROXY_CLOUD_MODEL` 是单个模型，为什么需要 `_route_cloud_model` 字段？

**建议**: 保留该字段，但说明"当前等于 `PROXY_CLOUD_MODEL`，为未来多云端模型预留"。

---

## 五、路由决策引擎

### 5.1 决策矩阵的优先级存在逻辑问题

§4.1 决策矩阵：
- 优先级 3 和 4 有重叠
- 优先级 5/6 永远不会触发（已被优先级 4 覆盖）
- 优先级 7 的冷却期逻辑在 §4.4 伪代码中已提前处理，矩阵中未体现

**建议修正决策矩阵**:

| 优先级 | 条件 | 决策 | 原因标签 |
|---|---|---|---|
| 0 | `PROXY_ROUTE_ENABLED == false` | local | disabled |
| 1 | session_id 在 cloud_fail_count 冷却期内 | local | cloud_cooldown_active |
| 2 | `_SESSION_ROUTE_MAP[session_id] == "cloud"` | cloud | session_already_cloud |
| 3 | `_SESSION_ROUTE_MAP[session_id] == "local_forced"` | local | session_force_local |
| 4 | 命中敏感路径且上下文超载 | reject | sensitive_path_over_limit |
| 5 | 命中敏感路径 | local | sensitive_path |
| 6 | `used_pct > ROUTE_MEMORY_PCT AND available_gb < 5` | cloud | memory_pressure |
| 7 | `total_chars > THRESHOLD` | cloud | chars_exceed_threshold |
| 8 | `stage ∈ {saturation, oom_danger, pre_trunc}` | cloud | lifecycle_stage |
| 9 | 默认 | local | under_threshold |

---

### 5.2 冷却期逻辑与 `_SESSION_ROUTE_MAP` 的交互

§4.4 伪代码中冷却期结束后 `_SESSION_ROUTE_MAP.pop` 会清除 session 状态。但如果 `local_forced` 是由用户手动 `./manage.sh route-force-local` 导致的，冷却期逻辑不会处理它。

**建议**: 区分 `local_forced` 的来源：
- 由 cloud failures 导致：受冷却期管理
- 由用户手动导致：不受冷却期管理，永久本地直到用户解除

---

## 六、可观测性

### 6.1 `/status` 页面成本显示

§5.2 的 `/status` 示例显示累计成本，但未说明是 pre-request 估算还是 post-request 真实成本。

**建议明确**:
- 每次请求完成后更新真实成本
- pending 请求显示 input 估算
- `/status` 显示累计真实成本 + 当前 pending 估算

---

### 6.2 Metrics 中的 `actual_cost_total: null`

§5.3 的 metrics 示例中 `actual_cost_total: null`。非流式响应可从 usage 字段获取，流式响应通常在最后一个 chunk 才有 usage。

**建议**: 说明流式响应的 actual_cost 在 message_stop 事件后补写，或在 metrics 中分阶段记录。

---

## 七、配置体系

### 7.1 `PROXY_CLOUD_API_KEY` 默认值错误

§3.1 中：
> `PROXY_CLOUD_API_KEY` 默认值 `$LLAMA_API_KEY`

**问题**: `LLAMA_API_KEY` 在本地模式下是 dummy token `sk-1234`，不能用于云端。

**建议修正**:
- `PROXY_CLOUD_API_KEY` 无默认值
- 必须用户显式配置或通过 `secret.local.conf` 设置
- 如果未设置且 `PROXY_ROUTE_ENABLED=true`，SmartRouter 应记录 error 并强制 local

---

### 7.2 Profile 系统的加载顺序

§3.4 的 Profile 加载逻辑在 `proxy_state.py` 顶层执行时可能覆盖用户显式配置。

**建议**: Profile 加载应在读取所有环境变量之后、使用这些值之前执行。或明确 profile 只是定义一组默认值，优先级低于显式环境变量。

---

## 八、实施计划

### 8.1 Phase 1 工作量估算偏乐观

§6 中 Phase 1 合计 ~16-17 小时。考虑到 BackendDispatcher 改造和测试复杂度，单人项目可能不够。

**建议**: Phase 1 估算调整为 20-24 小时，或拆分一个 Phase 0（spike）验证 BackendDispatcher fallback 方案。

---

### 8.2 缺少与现有测试的回归计划

文档没有说明如何确保新增路由不破坏现有 548 个单元测试。

**建议补充**:
- Phase 1 验收标准增加：所有现有单元测试在 `PROXY_ROUTE_ENABLED=false` 和 `PROXY_ROUTE_ENABLED=true` 两种配置下均通过
- 明确是否需要为路由功能新增 mock backend

---

## 九、风险与缓解

### 9.1 R2 提到的 daily budget cap 配置缺失

R2 缓解措施提到 daily budget cap (Phase 3)，但 §3.1 配置表中没有 `PROXY_ROUTE_DAILY_BUDGET`。

**建议**: 要么删除该缓解措施，要么在配置表中增加 `PROXY_ROUTE_DAILY_BUDGET` 并在 Phase 3 实现。

---

### 9.2 R7 概率/影响评估不当

R7 "用户忘记开启路由功能"被标为"高概率/低影响"。实际上如果用户不知道路由功能，长会话问题不会解决，影响不应为"低"。

**建议**: 影响改为"中"或"高"，缓解措施增加首次启动 `./manage.sh start` 时的提示。

---

## 十、最终结论

| 维度 | 评分 | 说明 |
|---|---|---|
| 架构清晰度 | ★★★★★ | 模块划分、Stage 插入位置合理 |
| 实现可行性 | ★★★★☆ | 回退流程与 Pipeline 协作需明确 |
| 数据流完整性 | ★★★★☆ | PipelineContext 字段有一处重复 |
| 并发安全 | ★★★★☆ | 锁顺序需明确，冷却期状态需加锁 |
| 可观测性 | ★★★★★ | logs/status/metrics 三层覆盖 |
| 测试计划 | ★★★☆☆ | 缺少回归测试和 mock backend 说明 |
| 配置体系 | ★★★★☆ | Profile 加载顺序和 API key 默认值需修正 |

**综合判断**: 设计文档 v2.0 整体优秀，但需要解决以下关键问题后才能进入开发：

1. **BackendDispatcher fallback 实现方式**：内部重试 vs Pipeline 重新提交
2. **Emergency truncation 触发时机**：与 ContextTruncator 执行顺序的关系
3. **`PROXY_CLOUD_API_KEY` 默认值**：不能是 `LLAMA_API_KEY`
4. **决策矩阵与伪代码一致性**：修正优先级
5. **删除 PipelineContext 中重复的 `_route_notified` 字段**
6. **并发安全**：`_SESSION_ROUTE_MAP`、`_cloud_fail_count`、`_cloud_cooldown_start` 的读写加锁

---

## 十一、建议下一步

1. 召开 30 分钟设计评审，重点讨论 BackendDispatcher fallback 方案
2. 根据评审结论更新 §2.2/§2.3
3. 修正 §3.1、§4.1、§4.4 的配置和决策逻辑
4. 补充并发安全说明（哪些 dict 需要 `_state_lock`）
5. 更新 §6 测试计划，明确 mock backend 需求
6. 重新 review 后进入开发

---

*文档路径*: `docs/02-architecture-design/intelligent-model-routing-design-review-2026-06-21.md`  
*评审日期*: 2026-06-21
