# 测试策略与覆盖矩阵

> **版本**: v1.0 · 2026-06-06
> **数据来源**: `docs/PRD-anthropic-proxy.md` (PRD v3.0) + `test/` 目录盘点 + `anthropic_proxy.py` 源码
> **目标读者**: 测试工程师 / 维护者 / 任何对代理层做修改的开发者
> **TL;DR**: PRD 自报 "23/23 需求 = 100% 实现"；本审计发现 **代码实现 100%，但自动化测试仅覆盖 7/23 = 30%**。优先补齐 R2 循环三级干预、R1 增量压缩/关键词、R3 工具过滤、R4 JSON 修复与 reasoning 提取。

---

## 1. 现状盘点

### 1.1 测试资产 (test/ 目录)

| 文件 | 形态 | Case 数 | 运行时 | 触发方式 |
|------|------|---------|--------|----------|
| `test/unit/test_proxy_fallback.py` | Python unittest | **44** | <1s | pre-commit |
| `test/integration/test_blocker_integration.sh` | bash + mock backend | **7** | ~5s | 手动 / pre-push |
| `test/integration/mock_backend.py` | 共享 fixture | — | — | 由上调用 |
| `test/e2e/test_proxy_integration.py` | Python urllib | **12 函数 / 19+ 断言** | ~10s | 手动 |
| `test/e2e/e2e_tools_fallback.sh` | bash + curl | **3** | ~30s | 手动 |
| `test/run_tests.sh` | 统一 runner | — | — | 入口 |
| `.githooks/pre-commit` | git hook | — | — | 每次 commit |
| **合计** | | **66 cases / 78 assertions** | | |

### 1.2 现有测试覆盖的具体函数

| 测试类 / 文件 | 覆盖的源码函数 | 需求点 |
|---------------|----------------|--------|
| `TestExtractContentToolCalls` (11) | `_extract_content_tool_calls` (line 419) | R4.2 |
| `TestStreamingStateMachine` (12) | `_StreamingToolsExtractor` (line 465) | R4.2 |
| `TestNonStreamingConversion` (6) | `convert_openai_response_to_anthropic` (line 1818) | R4.1/R4.2/R4.3 |
| `TestBlockerDetection` (12) | `_detect_blocker_pattern` + `_build_blocker_message` (line 1248/1331) | R2.4 |
| `TestCompressPromptStructure` (1) | `_compress_middle_with_llm` (line 950) | R1.2 |
| `TestFifoPlaceholderStability` (3) | `truncate_messages_if_needed` (line 1109) | R1.1/R3.2 |
| `test_blocker_integration.sh` (7) | 端到端：[BLOCKER] 注入 + 元数据 | R2.4 + R5.* (间接) |
| `test_proxy_integration.py` (12) | 路由 / 中文 / 工具 / 流式 / 会话 / 并发 | R7.1 + 通用冒烟 |
| `e2e_tools_fallback.sh` (3) | 端到端：非流/流/回归 | R4.2 端到端 |

---

## 2. 需求 × 覆盖矩阵

**图例**: ✅ 已覆盖（≥3 个 case）｜🟡 部分覆盖（1-2 个 case 或仅端到端）｜❌ 未覆盖 ｜🚫 不可单元测试（依赖外部组件）

| 需求 | 描述 | 实现位置 | 单测 | 集成 | e2e | 等级 | 缺口 |
|------|------|----------|------|------|-----|------|------|
| **R1.1** | 主动上下文截断 (rounds/fifo/char) | line 1106 | 3 (fifo only) | 0 | 0 | 🟡 | rounds/char 策略未测 |
| **R1.2** | LLM 压缩摘要 | line 950 | 1 (prompt only) | 0 | 0 | 🟡 | 仅校验 prompt 结构，未校验实际压缩行为 |
| **R1.3** | 增量压缩 + 缓存 | line 1066 | 0 | 0 | 0 | ❌ | 整模块零测试 |
| **R1.4** | 关键词按需检索 | line 2626/2701 | 0 | 0 | 0 | ❌ | 整模块零测试 |
| **R2.1** | 递进式循环干预 (Level 1/2/3) | line 2956-3034 | 0 | 0 | 0 | ❌ | **关键风险点**：死循环是 PRD §2.1 头号根因 |
| **R2.2** | 智能清除 (200 字符预览) | line 789 | 0 | 0 | 0 | ❌ | |
| **R2.3** | 近期 Read 保护 (+5 分) | line 725-727 | 0 | 0 | 0 | ❌ | |
| **R2.4** | 阻塞模式检测 | line 1229 | 12 | 7 | 0 | ✅ | **亮点**：3 个独立角度（逻辑/端到端/元数据） |
| **R3.1** | KV Cache 复用 | backend | 0 | 0 | 0 | 🚫 | 需 backend 端 metrics，需手动 |
| **R3.2** | 日期标准化 (前缀稳定) | line 3071 | 0 | 0 | 0 | ❌ | |
| **R3.3** | 工具定义过滤 (44→15) | line 2591/2625 | 0 | 0 | 0 | ❌ | 估算节省 5-8K tokens 无回归网 |
| **R4.1** | XML→JSON 回退 | line 340 | 0 (无专用) | 0 | 0 | ❌ | `parse_tool_arguments` 4 级回退无测试 |
| **R4.2** | Content-text 工具提取 | line 419/465 | 29 | 0 | 3 | ✅ | **亮点**：流式+非流式+边界完整 |
| **R4.3** | 输出截断 (force-stopped) | line 3224/3265 | 1 (max_tokens) | 0 | 0 | 🟡 | 仅 `max_tokens_not_overridden`，force-stopped 分支未测 |
| **R4.4** | JSON 修复 (截断) | line 286 | 0 | 0 | 0 | ❌ | `_repair_truncated_json` 整函数零测试 |
| **R4.5** | Reasoning 提取 | line 3388 | 0 | 0 | 0 | ❌ | |
| **R5.1** | 错误翻译 (3 种) | line 2852 | 0 | 0 | 0 | ❌ | 仅在集成测试中间接触发 |
| **R5.2** | 错误上下文增强 | line 2852 | 0 | 0 | 0 | ❌ | |
| **R6.1** | 结构化 Metrics 输出 | line 221 | 0 | 0 | 0 | ❌ | 监控整个系统的"黑匣子"无测试 |
| **R6.2** | 4 种质量标记 | line 2555 | 0 | 0 | 0 | ❌ | `loop_injected` 等标志无验证 |
| **R6.3** | 压缩比追踪 | — | 0 | 0 | 0 | ❌ | |
| **R7.1** | 并发控制 (Semaphore) | line 39 | 0 | 0 | 1 | 🟡 | 1 case 验证基本序列化；N=4/race condition 未测 |
| **R7.2** | 云端切换 | 双模式 | 0 | 0 | 0 | ❌ | BACKEND_TYPE 自动检测无测试 |

### 2.1 覆盖率统计

| 维度 | 数字 | 比例 |
|------|------|------|
| 已实现需求点 (PRD) | 23 | 100% |
| 有≥1 个测试的需求 | 7 | **30%** |
| 充分覆盖 (≥3 case) | 2 (R2.4, R4.2) | 9% |
| 零测试 | 15 | **65%** |
| 不可单元测试 | 1 (R3.1) | 4% |

> ⚠️ **结论**: PRD 自评"100% 实现"是事实，但 "100% 验证" 远未达到。任何对 R1.3 / R2.1 / R3.3 / R4.1 / R4.4 / R4.5 / R5.* / R6.* / R7.2 的修改，目前都没有回归网。

---

## 3. 整体测试策略

### 3.1 测试金字塔与分层目标

```
                    ╔══════════════════╗
                    ║  Manual / 生产  ║   ← 真实使用、性能基准、Cloud 模式
                    ║   Exploration   ║
                    ╚══════════════════╝
                  ╔════════════════════════╗
                  ║     E2E (e2e/)         ║   真实 proxy + backend
                  ║   目标: ≥25 cases      ║   CI 手动触发 / 合并前
                  ╚════════════════════════╝
              ╔════════════════════════════════╗
              ║   Integration (integration/)   ║   mock backend，无 LLM
              ║   目标: ≥10 cases              ║   pre-push gate
              ╚════════════════════════════════╝
          ╔════════════════════════════════════════╗
          ║        Unit (unit/)                    ║   纯函数，无 I/O
          ║   目标: ≥80 cases                       ║   **pre-commit 必跑**
          ║   覆盖率: 函数 ≥70%, 分支 ≥50%         ║
          ╚════════════════════════════════════════╝
```

### 3.2 分层职责

| 层级 | 职责 | 不应做 | 当前规模 |
|------|------|--------|----------|
| **Unit** | 纯逻辑：解析、状态机、转换、检测、压缩算法 | 网络 I/O、文件系统、状态泄漏 | 44 → **目标 80+** |
| **Integration** | 多个组件协同：mock 后端 + 真实代理进程 | 真实 LLM 推理、长时间运行 | 7 → **目标 10+** |
| **E2E** | 真实栈：proxy + backend | 模拟大负载（用 stress_test.py） | 22 → **目标 25+** |
| **Manual** | 性能、Cloud 模式、生产场景 | 取代自动化 | 持续 |

### 3.3 测试设计原则

1. **测试金字塔顺序执行**: pre-commit (unit) → pre-push (integration) → 合并前 (e2e) → 发布前 (manual)
2. **Fixture 隔离**: 每个测试构造独立的 messages / 请求体；禁止依赖前序测试的状态泄漏
3. **可重复性**: mock 后端是 golden（无 LLM），保证跨机器结果一致
4. **失败信息丰富**: 失败时打印 traceback、相关输入、期望/实际差异（已部分做到）
5. **无第三方依赖**: 沿用 stdlib-only 风格，pytest/nose 暂不引入
6. **回归优先于新功能**: 修改 R2.1 死循环治理时，先补 R2.1 单元测试，再写新逻辑

### 3.4 配置参数的测试覆盖

PRD §6 列了 25+ 环境变量。当前测试**只隐式覆盖少数**（blocker 阈值、fifo 策略、content-tools 开关）。

| 类别 | 参数 | 现状 | 建议 |
|------|------|------|------|
| 并发 | `PROXY_MAX_CONCURRENT` | 1 case (e2e) | unit 用 mock Semaphore 测 N=1/4 |
| 工具清除 | `PROXY_CLEAR_*` (4 个) | ❌ | unit 测 clear_old_tool_results 边界 |
| 循环/阻塞 | `PROXY_LOOP_LEVEL1/2/3` | ❌ | unit 测三档阈值 + 干预消息生成 |
| 截断 | `PROXY_CTX_TRUNCATE_STRATEGY` (3) | 🟡 fifo only | unit 补 rounds / char 策略 |
| 工具过滤 | `PROXY_TOOL_FILTER_*` (3) | ❌ | unit 测白名单 + recent N + 边界 (保留<5) |
| 关键词索引 | `PROXY_HISTORY_*` (3) | ❌ | unit 测 extract_keywords / inject_keyword_context |

### 3.5 数据驱动 / 参数化

当前 e2e/test_proxy_integration.py 已经按功能分块；下一步对 R2.1 / R2.2 / R3.3 / R4.1 等多个相同骨架的需求点，建议建一个 `test/fixtures/` 目录存放典型 message 模板：

```
test/fixtures/
├── loop_sequences.json         # 3/6/9 轮 Read 循环、A→B 交替、Write 循环...
├── blocker_scenarios.json      # 7 类错误类型 + 触发模式
├── truncation_samples.json     # 50/100/200 消息长度的典型 session
├── tool_filter_corpus.json     # 44 tool 列表 + 各种使用模式
└── xml_json_corpus.json        # 各种 Qwen 工具调用怪格式样本
```

---

## 4. 缺口补充路线图

按 **风险 × 实施成本** 排序，4 阶段递进。

### Phase A — 紧急（1 周内）堵核心盲区

| 缺口 | 风险 | 建议测试 |
|------|------|----------|
| R2.1 递进循环干预 | **高** — 这是 PRD §2.1 头号根因，但仅 R2.4 测了 blocker 模式 | unit: `test_loop_level1_injects_text_message`, `test_loop_level2_removes_tool`, `test_loop_level3_replaces_tool_use`；integration 模拟 3/6/9 轮 Read |
| R4.4 `_repair_truncated_json` | **高** — Qwen 长输出必触发 | unit: 7 个 case：纯截断 / 缺右括号 / 缺右引号 / 缺右大括号 / 嵌套截断 / Unicode 截断 / 已完整 |
| R6.1/6.2 Metrics | **中** — 失去可观测性后无法定位线上问题 | unit: `test_log_metrics_emits_all_fields`, `test_quality_flag_loop_injected`, `test_compression_ratio_in_stats` |
| R1.1 rounds/char 策略 | **中** — 当前只测了 fifo | unit: `test_truncate_rounds_drops_oldest_rounds`, `test_truncate_char_triggers_at_limit` |

### Phase B — 短期（2 周）覆盖格式兼容

| 缺口 | 测试 |
|------|------|
| R4.1 XML→JSON 回退 (4 级) | unit: `test_parse_tool_arguments_*`：纯 JSON / 嵌入 JSON / XML 格式 / heuristic fallback |
| R4.5 Reasoning 提取 | unit: `test_extract_reasoning_from_openai_resp`, `test_reasoning_appears_as_thinking_block` |
| R5.1/5.2 错误翻译 | unit: `test_translate_wasted_call`, `test_translate_file_not_found`, `test_translate_input_validation` (断言中文提示 + 建议) |
| R2.2 智能清除 (200 字符预览) | unit: `test_clear_read_keeps_200_chars`, `test_clear_other_replaces_with_placeholder` |
| R2.3 近期 Read 保护 | unit: `test_recent_reads_get_5_point_bonus`, `test_old_reads_dropped_first` |

### Phase C — 中期（1 个月）覆盖配置矩阵

| 缺口 | 测试 |
|------|------|
| R3.3 工具过滤 | unit: `test_filter_keeps_always_keep_tools`, `test_filter_includes_recent_n`, `test_filter_respects_tool_choice`, `test_filter_fallback_when_lt_5` |
| R1.3 增量压缩 | unit: `test_incremental_cache_hit_skips_llm`, `test_cache_miss_calls_llm` |
| R1.4 关键词索引 | unit: `test_extract_keywords_filenames`, `test_inject_keyword_context_top_k` |
| R3.2 日期标准化 | unit: `test_date_normalization_is_idempotent`, `test_placeholder_text_stable_across_dates` |
| R7.2 云端切换 | unit: `test_backend_type_detected_from_url`, `test_cloud_mode_disables_clearing`；integration 跑 `start-cloud` 模式 |

### Phase D — 持续（季度）性能与回归

- 性能回归：用 `tools/stress_test.py` + `tools/bench_mtp.py` 守护 TTFT / tokens / 消息数
- Prefix cache 命中率监控：从 `proxy_metrics.jsonl` 聚合加入 `test/fixtures/expected_metrics.json`，CI 检查漂移
- 长期：探索 property-based testing（hypothesis），对 `_extract_keywords` / `_repair_truncated_json` 模糊测试

---

## 5. 度量指标

### 5.1 内部质量 (CI 看)

| 指标 | 目标 | 测量方式 |
|------|------|----------|
| Unit case 数 | ≥ 80 | `grep -c "    def test_" test/unit/*.py` |
| 需求覆盖率 | ≥ 70% (16/23) | 需求矩阵 ✅+🟡 之和 |
| pre-commit 耗时 | ≤ 2s | `time bash test/run_tests.sh --fast` |
| e2e 套件耗时 | ≤ 60s | `time bash test/run_tests.sh --e2e` |
| Flaky 率 | ≤ 1% | 连续 10 次运行中失败次数 |

### 5.2 外部效果 (线上看)

PRD §1.2 已定义 6 项核心指标。落到测试守护上：

| PRD 指标 | 守护方式 |
|----------|----------|
| Prompt tokens ~27K | `e2e/test_proxy_integration.py::test_long_context` 校验 5K 输入后代理仍工作 |
| TTFT 1-5s (cache) | 性能测试，不进单元 |
| 消息数/轮 ~28 | unit: `test_truncate_*` 校验 100+ 消息被截到 ≤40 |
| 工具数/请求 15 | unit: `test_filter_44_to_15` |
| 死循环 3 次打断 | integration: 模拟 10 轮 Read，断言 Level 2 触发 |
| OOM 0 次 | manual，不进自动化 |

### 5.3 监控告警 (线上)

- **proxy_metrics.jsonl 聚合**: 实时计算 `loop_injected` 比例、compression_ratio 分布
- **请求成功率**: `_failed_request` 计数（需要在代理加埋点）
- **错误翻译命中率**: 统计 `error_translation.count > 0` 的请求占比

---

## 6. 风险与权衡

| 决策 | 风险 | 缓解 |
|------|------|------|
| 坚持 stdlib-only（不引 pytest） | unit 用例组织不够灵活（无 parametrize、fixture 弱） | 短期可接受；Phase D 评估 pytest 引入 ROI |
| pre-commit 只跑 unit（<1s） | integration 失败可能漏到 main | 配套 pre-push hook 跑 `--integration`；`manage.sh` 文档强调合并前 `--all` |
| 真实 LLM 不在测试栈 | 模型行为变化时某些 case 可能失效 | blocker 模式测试已用 mock；模型差异通过 manifest 锁版本 |
| Fixture 数据从 docs/ 反推 | 可能漏掉真实场景的边角 | Phase D 收集线上 proxy_metrics 反哺 fixture |
| R7.2 云端无 CI 覆盖 | `start-cloud` 路径可能回归 | 添加 `test/integration/test_cloud_mode.sh`，用 DeepSeek 的 1 个便宜请求 |

---

## 7. 立即行动清单 (本 PR)

1. ✅ 完成 test/ 目录梳理（已落 bbddd1d）
2. ✅ 配置 pre-commit 单元测试门禁（已落 bbddd1d）
3. ✅ 修复 R2 循环检测的 NoneType bug（已落 bbddd1d）
4. ⏭ **下一个 PR**: 实现 Phase A 至少 3 项：
   - R2.1 递进干预的 3 个 unit test
   - R4.4 `_repair_truncated_json` 的 7 个 unit test
   - R6.1/R6.2 metrics 的 2 个 unit test

---

## 附录 A: 测试模板库（建议复用）

**Unit test 骨架**（已示范于 `test_proxy_fallback.py`）：
```python
class TestXxxFeature(unittest.TestCase):
    def setUp(self): ...                    # patch env vars / 模块常量
    def test_happy_path(self): ...
    def test_boundary_low(self): ...
    def test_boundary_high(self): ...
    def test_disabled_via_env(self): ...    # 验证 PROXY_*_ENABLED=false 短路
    def test_cache_stability(self): ...     # 对 R3.2 类需求：相同输入产相同字节
```

**Integration test 骨架**（已示范于 `test_blocker_integration.sh`）：
```bash
# 1) start mock backend
# 2) start proxy with the env vars under test
# 3) curl the proxy with the target body
# 4) assert on the mock's captured body (what the backend actually received)
```

**E2E test 骨架**（已示范于 `test_proxy_integration.py`）：
```python
def test_X():
    body = { ... }                         # realistic Anthropic-format request
    code, headers, resp, elapsed = _post("/v1/messages", body)
    assert code == 200
    data = json.loads(resp.read())
    assert <semantic assertion>
```

## 附录 B: 测试反模式（避免）

- ❌ 用真实 LLM 校验语义（不可重复、成本高、CI 不友好）
- ❌ 在 unit test 里启动 HTTP server（应留给 integration）
- ❌ 依赖前序测试的全局状态（应每个 test_* 自己构造输入）
- ❌ 断言具体错误信息字符串（脆弱；用 error_code 或正则更稳）
- ❌ 把 stress test / bench 当成 pass/fail gate（变异性大；用 baseline + 容差）
