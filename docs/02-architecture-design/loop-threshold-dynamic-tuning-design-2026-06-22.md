# 循环阈值随 Session 长度动态调优：设计提案（待分析）

> **文档版本**: v0.1 (Draft)
> **创建日期**: 2026-06-22
> **状态**: ⏳ **待分析**（Needs Analysis）— 未进入开发排期，仅作为决策输入
> **关联模块**: `loop_detection.py` / `pipeline.py:BlockerDetector` / `proxy_state.py:PROXY_LOOP_*` / `proxy_state.py:_SESSION_REQUEST_COUNT`
> **来源**: 智能路由优化建议集 第 4 项
> **前置依赖**: 建议1（非粘性 session）已交付；建议3（分段延迟度量）已交付；建议2（API Key 标识）已交付

---

## 目录

1. [背景与动机](#1-背景与动机)
2. [当前实现盘点](#2-当前实现盘点)
3. [问题分析](#3-问题分析)
4. [候选方案](#4-候选方案)
5. [推荐方案：分档动态阈值](#5-推荐方案分档动态阈值)
6. [配置项清单](#6-配置项清单)
7. [代码改造影响面](#7-代码改造影响面)
8. [风险与未决事项](#8-风险与未决事项)
9. [建议的测试覆盖](#9-建议的测试覆盖)
10. [下一步](#10-下一步)

---

## 1. 背景与动机

### 1.1 现状观察

智能路由在长会话（Agent 工作流 30+ 轮）中暴露两类问题：

| 现象 | 触发位置 | 当前阈值 | 影响 |
|------|---------|---------|------|
| **误判 Edit 后 Read 同一文件为循环** | `loop_detection.py`：精确匹配 tool_use | `PROXY_LOOP_THRESHOLD=3` | Edit→Read 循环验证是正常工作流，3 次命中即触发 Level 1 干预，注入打断通知 |
| **文本循环阈值在长 session 偏激进** | `loop_detection.py:_TEXT_LOOP_*` | `PROXY_TEXT_LOOP_THRESHOLD=3` & `min_chars=100` | 长会话的重复 citation/boilerplate 段落被误判 |
| **Blocker 对短 session 略松** | `pipeline.py:BlockerDetector` | `PROXY_BLOCKER_THRESHOLD=2` | 短 session 中 2 次 `file_not_found` 就触发 BLOCKER 注入，可能过早 |

### 1.2 核心 insight

> **循环阈值不应一刀切。** Session 越长，模型重复合法动作的概率越高（因为文件已被多次 Edit、上下文已被截断重读）。同时，短 session 中重复动作几乎一定是 bug。

因此应该按 session 长度分档使用不同阈值。

### 1.3 为什么现在做

- 建议1 已交付「按 session 长度回退 Local 路由」（`_SESSION_BELOW_THRESHOLD` 计数器），证明 session 长度信号可用。
- 建议3 已交付 `_LATENCY_BY_TARGET` 数据采集，可观察长 session 的 dispatch latency 分布，为阈值档位标定提供数据。
- 上游 `docs/04-analysis-diagnostics/dead-loop-analysis-report.md` 已有死循环分析数据，但未与 session 长度交叉。

---

## 2. 当前实现盘点

### 2.1 阈值相关常量

| 常量 | 默认值 | 来源 | 作用 |
|------|--------|------|------|
| `PROXY_LOOP_THRESHOLD` | `3` | `proxy_state.py:246` | 同一 tool_use 连续命中 N 次触发 Level 1 干预 |
| `PROXY_LOOP_LEVEL2` | `6` (2×threshold) | `proxy_state.py:247` | Level 2：移除循环 tool |
| `PROXY_LOOP_LEVEL3` | `9` (3×threshold) | `proxy_state.py:248` | Level 3：强制纯文本模式 |
| `PROXY_TEXT_LOOP_THRESHOLD` | `3` | `proxy_state.py:252` | 文本相似度循环触发阈值 |
| `PROXY_TEXT_LOOP_MIN_CHARS` | `100` | `proxy_state.py:253` | 文本最短判断长度 |
| `PROXY_TEXT_LOOP_SIMILARITY` | `0.85` | `proxy_state.py:254` | 相似度阈值 |
| `PROXY_BLOCKER_THRESHOLD` | `2` | `proxy_state.py:291` | 连续同类型错误触发 BLOCKER |

### 2.2 Session 长度信号

- `_SESSION_REQUEST_COUNT: dict[str, int]`（`proxy_state.py:125`）记录每个 session_id 的请求数。
- 由 `lifecycle.py:_classify_lifecycle_stage()` 在 `pipeline.py:LifecycleClassifier.process()` 中递增（`pipeline.py:369`）。
- 在 `stage_config["request_count"]` 中暴露，已写入 metrics。
- `_LOOP_SESSION_STATE`：记录循环历史，无 session 长度感知。

### 2.3 调用点

`loop_detection.py` 中的判定函数读取 `PROXY_LOOP_THRESHOLD` / `PROXY_TEXT_LOOP_THRESHOLD` 作为模块级常量。`pipeline.py:BlockerDetector` 读取 `PROXY_BLOCKER_THRESHOLD`。

---

## 3. 问题分析

### 3.1 关键问题

1. **阈值对 session 长度无感知** — 一个 50 轮的 Agent 工作流与一个 5 轮的快速查询用同一阈值，明显不合理。
2. **改动风险点** — `loop_detection.py` 是 P0 模块（DEFECT-LIST 多次记入），任何改动都要：
   - 不破坏现有 24+ 个 loop 测试（`test_loop_*.py` / `test_text_loop.py`）
   - 不丢失 Level 1→2→3 的递进语义
3. **配置爆炸风险** — 已经有 7 个阈值常量，若每个再分 3 档，会变成 21+ 个常量，增加运维负担。

### 3.2 备选思路

| 思路 | 描述 | 优点 | 缺点 |
|------|------|------|------|
| **A. 一刀切放宽** | 把 `PROXY_LOOP_THRESHOLD=5`、`BLOCKER_THRESHOLD=3` | 1 行改动 | 短 session 误判增多，回退 |
| **B. 分档动态** | 按 session 长度分 3 档（短/长/超长）选用不同阈值 | 精细化 | 需 6-9 个新常量 |
| **C. 连续函数** | `threshold = base + k × log(1 + session_count)` | 平滑 | 难调试、难文档化 |
| **D. 命中率加权** | 不看 session 长度，看「该 session 累计循环命中次数」自适应上调 | 反应真实风险 | 需维持新状态 `_SESSION_LOOP_HIT_COUNT`，与现有 `_LOOP_SESSION_STATE` 重叠 |

### 3.3 已否定方案

- **思路 A**：一刀切放宽会让短 session 的真实 bug 更晚被发现，违反「短 session 严、长 session 宽」的核心 insight。
- **思路 C**：连续函数不易在 /status 页面和配置文档中向用户展示当前生效阈值，运维难度高。

---

## 4. 候选方案

剩余两个思路（B 和 D）做对比：

| 维度 | B. 分档动态 | D. 命中率加权 |
|------|------------|-------------|
| 信号来源 | `_SESSION_REQUEST_COUNT` | `_LOOP_SESSION_STATE` 累计命中数 |
| 阈值变化 | 阶梯式（3→4→5） | 命中越多越宽 |
| 反映循环风险 | 间接（按 session 长度先验） | 直接（按实际命中） |
| 新增配置项 | 6-9 个 | 2-3 个（命中后增加量、上限） |
| 用户可解释性 | ✅ 强（"长 session 放宽到 5"） | ⚠️ 中（"已命中 2 次，放宽到 4"） |
| 与建议1 协同 | ✅ 共用 `_SESSION_REQUEST_COUNT` | 需新状态 |
| 对长 session 良性重复的敏感度 | ✅ 高（一开始就放宽） | ❌ 低（需先误触发一次才放宽） |

**倾向方案 B**。理由：
- 与建议1 同一信号源 (`_SESSION_REQUEST_COUNT`)，无需新增状态变量
- 阶梯式阈值易在 `/status` 上展示当前档位（"session 长度档=long, loop_threshold=4"）
- 长会话中的良性重复在 session 第 11 轮起即受益，不必先误触发

---

## 5. 推荐方案：分档动态阈值

### 5.1 Session 长度分档

| 档位 | 请求数范围 | 当前命名建议 | 含义 |
|------|-----------|-------------|------|
| `short` | 1 – 10 | `PROXY_LOOP_SESSION_SHORT_BOUND` | 短会话，严格阈值（沿用现有默认） |
| `long` | 11 – 25 | `PROXY_LOOP_SESSION_LONG_BOUND` | 长会话，放宽 1 档 |
| `very_long` | 26+ | — | 超长会话，放宽 2 档 |

> 边界值 10/25 取自建议1 中 `PROXY_ROUTE_STICKY_RETURN_ROUNDS=5` 的尺度数量级，可与建议1 信号校准。两个边界都做成可配置。

### 5.2 阈值矩阵

| Session 档位 | `loop_threshold` | `text_loop_threshold` | `blocker_threshold` |
|-------------|-----------------|---------------------|-------------------|
| `short` (1–10) | 3 (默认) | 3 (默认) | 2 (默认) |
| `long` (11–25) | 4 | 4 | 3 |
| `very_long` (26+) | 5 | 5 | 3 |

> `PROXY_LOOP_LEVEL2` / `LEVEL3` 保持 `2×` / `3×` threshold 的派生关系，不分别配置（避免配置爆炸）。

### 5.3 接入点

新增 helper（建议放在 `loop_detection.py` 顶层）：

```python
def _effective_session_tier(session_id: str) -> str:
    """Return 'short' | 'long' | 'very_long' based on _SESSION_REQUEST_COUNT."""
    cnt = _ps._SESSION_REQUEST_COUNT.get(session_id, 0)
    if cnt <= _ps.PROXY_LOOP_SESSION_SHORT_BOUND:
        return "short"
    if cnt <= _ps.PROXY_LOOP_SESSION_LONG_BOUND:
        return "long"
    return "very_long"


def _effective_loop_threshold(session_id: str) -> int:
    tier = _effective_session_tier(session_id)
    return {
        "short": _ps.PROXY_LOOP_THRESHOLD,
        "long": _ps.PROXY_LOOP_THRESHOLD_LONG,
        "very_long": _ps.PROXY_LOOP_THRESHOLD_VERY_LONG,
    }[tier]
```

各判定函数（如 `_detect_tool_loop()` / `_detect_text_loop()`）改为调用 `_effective_loop_threshold(session_id)` 而非直接读 `PROXY_LOOP_THRESHOLD`。

`pipeline.py:BlockerDetector` 同理引入 `_effective_blocker_threshold(session_id)`。

### 5.4 可观测性

`/status` 路由卡片新增一行 `Session Tier` 展示当前活跃 session 的档位分布（按 `_SESSION_REQUEST_COUNT` 桶计数）。

metrics JSONL 输出新增字段：
- `loop_threshold_tier`: "short" / "long" / "very_long"
- `loop_threshold_effective`: 实际生效阈值（int）

---

## 6. 配置项清单

### 6.1 新增常量（`proxy_state.py`）

| 名称 | 默认值 | 类型 | 说明 |
|------|--------|------|------|
| `PROXY_LOOP_SESSION_SHORT_BOUND` | 10 | int | ≤ 此值视为 short 档 |
| `PROXY_LOOP_SESSION_LONG_BOUND` | 25 | int | ≤ 此值视为 long 档；> 为 very_long |
| `PROXY_LOOP_THRESHOLD_LONG` | 4 | int | long 档 loop_threshold |
| `PROXY_LOOP_THRESHOLD_VERY_LONG` | 5 | int | very_long 档 loop_threshold |
| `PROXY_TEXT_LOOP_THRESHOLD_LONG` | 4 | int | long 档 text_loop_threshold |
| `PROXY_TEXT_LOOP_THRESHOLD_VERY_LONG` | 5 | int | very_long 档 text_loop_threshold |
| `PROXY_BLOCKER_THRESHOLD_LONG` | 3 | int | long 档 blocker_threshold |
| `PROXY_BLOCKER_THRESHOLD_VERY_LONG` | 3 | int | very_long 档 blocker_threshold |

> 8 个新常量。`PROXY_LOOP_LEVEL2` / `LEVEL3` 保持派生，不分别设置 long/very_long 版本，避免膨胀。

### 6.2 注册表同步

必须同时更新以下三处（否则 `test_proxy_state.py:TestReloadSpecDefaultsConsistency` 失败）：

1. `proxy_state.py:_RELOAD_SPEC` 加入 8 条新元组
2. `proxy_config.py:CONFIG_REGISTRY` 加入对应 metadata + validation
3. `reload_config.py` 通过现有 `_reload_config()` 自动覆盖（无需额外改动）

### 6.3 `__all__` 导出

`proxy_state.py:__all__` 加入 8 个新常量名。

---

## 7. 代码改造影响面

| 文件 | 改动 | 工作量 |
|------|------|--------|
| `proxy_state.py` | 新增 8 常量 + `_RELOAD_SPEC` + `__all__` | 0.5h |
| `proxy_config.py` | CONFIG_REGISTRY 8 条新条目 | 0.5h |
| `loop_detection.py` | 引入 `_effective_loop_threshold()` / `_effective_text_loop_threshold()`；判定函数替换常量读取 | 1.5h |
| `pipeline.py:BlockerDetector` | 引入 `_effective_blocker_threshold()`；判定替换 | 0.5h |
| `admin_server.py` | `/status` 卡片新增 Session Tier 行 | 0.5h |
| `test/unit/test_loop_detection.py` | 新增跨档位测试（short/long/very_long 触发边界） | 1h |
| `test/unit/test_text_loop.py` | 新增 session 长度档位测试 | 0.5h |
| `test/unit/test_pipeline_stages.py` | 新增 `TestBlockerThresholdBySessionTier` | 0.5h |
| `test/unit/test_proxy_state.py` | ReloadSpec 一致性测试同步 | 0.2h |
| `test/unit/test_admin_server.py` | Tier 行存在性测试 | 0.2h |
| `AGENTS.md` / `CLAUDE.md` | 文档更新新配置项 | 0.3h |

**总估计**: ~6h（含测试）。

---

## 8. 风险与未决事项

### 8.1 待你/PM 确认的点

- [ ] **档位边界 10/25 依据是否足够？** 现取自建议1 的 sticky return rounds 量级，但应做一次实测：分析 `logs/proxy_requests.jsonl` 中现有 session 长度分布，看 10/25 是否落在自然的密度洼地。
- [ ] **是否同时调整 `PROXY_TEXT_LOOP_MIN_CHARS`？** 长会话中长文本段更常见，是否应该把 min_chars 也分档？倾向不调（任何长度的循环都应被捕获），但需 PM 确认。
- [ ] **`PROXY_BLOCKER_THRESHOLD_LONG=3` 与 `_VERY_LONG=3` 相同是否有意义？** 现状是 long 和 very_long 都用 3，等于「long 起放宽到 3、不再变化」。可考虑 very_long 放到 4 或干脆让 `_VERY_LONG` 与 `_LONG` 取同值但保留语义。
- [ ] **是否允许单 session 跳过档位机制？** 例如 `X-Proxy-Loop-Tier: short` 头部强制档位（类比 `X-Proxy-Route-To`）。倾向不加，避免又一组覆盖机制。
- [ ] **是否要在 `_reload_config()` 中重新计算 sticky 的 below-threshold 计数器与 tier 边界？** 当前 `_SESSION_REQUEST_COUNT` 不在 reload 范围（thread-local session state），tier 也不会被 reload 重置——这是预期行为。

### 8.2 技术风险

| 风险 | 缓解 |
|------|------|
| 现有 24+ loop 测试因阈值变化而失败 | 测试默认在新 helper 中传入 `session_id="test_short"` 保持 short 档行为，不影响现有断言 |
| 在长 session 档边界抖动（第 10/11 轮、25/26 轮）阈值切换导致行为不一致 | 边界切换是单次事件，且阈值只放宽 1，影响有限；可在 metrics 中观察 |
| 配置文件未同步更新导致默认值漂移 | 与建议1 经验：增加 `_RELOAD_SPEC` 一致性测试强制三表同步 |

### 8.3 否定式结论

- ❌ **不做「思路 D：命中率加权」** — 需先误触发一次才放宽，长会话用户感受不到改进。
- ❌ **不做连续函数** — 难展示、难调试。
- ❌ **不修改 `PROXY_LOOP_LEVEL2/3` 的派生公式** — 阈值分档后 LEVEL2/3 跟着上浮即可，无需独立档位。

---

## 9. 建议的测试覆盖

### 9.1 新增测试类

| 测试类 | 测试方法 | 断言 |
|--------|---------|------|
| `TestSessionTierClassification` | `test_short_tier_1_to_10` | 1 和 10 → short |
| | `test_long_tier_11_to_25` | 11 和 25 → long |
| | `test_very_long_tier_26_plus` | 26 和 100 → very_long |
| | `test_boundaries_inclusive` | 10 → short, 11 → long, 25 → long, 26 → very_long |
| `TestEffectiveLoopThreshold` | `test_short_uses_default` | short → 3 |
| | `test_long_uses_long_config` | long → 4 |
| | `test_very_long_uses_very_long_config` | very_long → 5 |
| | `test_override_via_env` | 设 `PROXY_LOOP_THRESHOLD_LONG=6` → long → 6 |
| `TestBlockerThresholdBySessionTier` | `test_short_blocker_2` | short → 2 |
| | `test_long_blocker_3` | long → 3 |
| | `test_very_long_blocker_3` | very_long → 3 |

### 9.2 现有测试兼容性

- `test_loop_detection.py` 24+ 现有测试默认使用 `session_id="test_xxx"` —— `_SESSION_REQUEST_COUNT["test_xxx"]` 未设置时返回 0 ⇒ tier=short ⇒ 阈值=3（默认），与现有断言一致。
- `test_text_loop.py`：同上。
- `test_pipeline_stages.py:TestBlockerDetector`：同上。
- `test_proxy_state.py:TestReloadSpecDefaultsConsistency`：必须加入新 8 常量的 `_RELOAD_SPEC` 元组。

### 9.3 Integration 测试

新增 `test/integration/test_loop_tier_integration.sh`：
1. 启动 mock backend
2. 发送 12 次（进入 long 档）请求触发 4 次相同 tool_use
3. 断言前 3 次未触发 Level 1，第 4 次触发
4. 对比短 session（3 次即触发）

---

## 10. 下一步

1. **数据校准**（~1h）：运行 `python3 tools/analyze_recent_logs.py` 或自写脚本，对 `logs/proxy_requests.jsonl` 中 `session_id` 字段做长度分布统计，确认 10/25 边界是否落在自然密度洼地。如不是，调整边界。
2. **PM 确认** §8.1 中的 5 个问题。
3. **排期决定**：进入 Phase 4 开发 or 暂缓到下个迭代。
4. 若进入开发，按 §7 影响面逐文件改造，按 §9 测试覆盖逐项加测试，pre-commit hook 自动跑 `--unit` 保护。
5. 完成 1-4 后更新此文档为 `状态: 已排期` / `状态: 已交付`，归档到 `docs/05-operations-changelog/`。

---

## 附：参考文档

- `docs/02-architecture-design/intelligent-model-routing-design.md` v2.12（主设计）
- `docs/04-analysis-diagnostics/dead-loop-analysis-report.md`（死循环分析）
- `../04-analysis-diagnostics/DEFECT-LIST.md` P0 项 2/3/4（循环注入率 37%、re_read 公式错误等）
- `test/unit/test_loop_detection.py` / `test_text_loop.py` / `test_pipeline_stages.py:TestBlockerDetector`（现状测试）
- `proxy_state.py:125` `_SESSION_REQUEST_COUNT` / `:246-291` 阈值常量

---

**本文件目前为待分析状态。任何实施决定须以 PM / 系统设计 review 通过为前提。**