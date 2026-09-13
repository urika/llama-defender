# 功能缺陷清单 (Defect List)

> **生成日期**: 2026-06-06  
> **最后更新**: 2026-07-12 (全量修复: 27 已修复/1 部分修复/2 设计限制; 新增 DEF-203/207/208/209/210/303/304 修复; P0+P1 代码缺口审计修复; 生产验证 20/20 请求成功 0% 错误率; 性能基线已建立)
> **数据来源**: 
> - `logs/anthropic_proxy.log` (66 MB, 178K+ 条目)
> - `logs/llama-server.log` (8.4 MB, rapid-mlx 后端日志)
> - `logs/proxy_metrics.jsonl` (305 条结构化指标)
> - `logs/itest/` (集成测试输出)
> - `logs/e2e_test.log` (端到端测试)
> - `logs/unit_test.log` (单元测试)
> - 28 条 git commit messages
> - `../06-reference-metrics/TROUBLESHOOTING.md` / `../06-reference-metrics/../06-reference-metrics/BENCHMARK.md`
> - `docs/` 下 24 篇需求与设计文档

---

## 缺陷严重度图例

| 级别 | 标识 | 含义 |
|------|------|------|
| 🔴 P0-Critical | 阻塞核心功能 / 数据丢失 / 系统崩溃 | 立即修复 |
| 🟠 P1-High | 主要功能降级 / 监控指标失真 | 1 周内修复 |
| 🟡 P2-Medium | 边缘场景失效 / 性能未达预期 | 2 周内修复 |
| 🔵 P3-Low | 代码质量 / 文档不一致 / UX 细节 | 1 个月内修复 |

---

## 一、🔴 P0-Critical (7 项)

### DEF-001: 67 个请求返回 500 错误 (22% 错误率) — ✅ 已修复 (2026-07-12)

> **🔗 M1.5 根治路径**: 见 [`PRD-litellm-borrow-2026-07-05`](../01-requirements-product/PRD-litellm-borrow-2026-07-05.md) §2 TS-2 (W1-W2)
> M1 走 WrapperGuard 热修 (防御性清理孤儿); M1.5 TS-2 通过 `_find_tool_pairs` + 配对原子保护从源头消除孤儿 tool_use/tool_result 切断问题。

| 项 | 内容 |
|------|------|
| **数据源** | `logs/proxy_metrics.jsonl` |
| **指标** | Total=305, 200=238 (78%), **500=67 (22%)** |
| **触发时间** | 2026-06-05 17:51:59 起集中爆发 |
| **关联 session** | `a309b181` 贡献大部分 500,空 session_id 集中在末尾 (集成测试) |
| **触发条件** | 1) 极大请求 (input_chars > 500K) 2) 空 session_id 的小请求 |
| **影响** | 用户感知: 22% 的请求直接失败,agent 行为中断 |
| **根因分析** | 极可能 `truncate_messages_if_needed` / `_handle_messages` 处理大 payload 时抛异常未被捕获 |
| **修复建议** | 1) 在 `_handle_messages` 入口添加 try/except 兜底<br>2) 对 input_chars > 400K 的请求提前截断<br>3) 单元测试添加大 payload 场景 |
| **已实施修复** | **Part A**: `PROXY_PRE_TRUNCATE_CHARS=400000` 预截断 + `do_POST` try/except 兜底<br>**Part B**: `_respond_json` 替换 `raise`,返回结构化 JSON 500<br>**Part C**: `_classify_exception()` 错误分类 (OOM→503, timeout→504, programming→500) + `Retry-After` header<br>**Part D**: `_estimate_message_chars` 增加 `tool_use` input 和 tool schema 字符估算 |
| **剩余工作** | 生产环境验证 500 错误率是否降至 < 2% |

### DEF-002: 循环注入率 37% — 模型仍频繁陷入循环 — ✅ 已修复 (2026-07-12)

> **🔗 M1.5 根治路径**: 见 [`PRD-litellm-borrow-2026-07-05`](../01-requirements-product/PRD-litellm-borrow-2026-07-05.md) §2 TS-2
> 截断切断配对是循环注入的诱因之一 (后端孤儿报错→模型 Defensive Read 重试)。M1.5 TS-2 消除此根因后,defensive re-read 触发率目标下降 ≥ 50%。

| 项 | 内容 |
|------|------|
| **数据源** | `logs/proxy_metrics.jsonl` quality_flags 统计 |
| **原始指标** | `loop_injected: 113/305 = 37.0%` (旧 metrics), `122/571 = 21.4%` (全量) |
| **根因** | 1) **LOOP_CONSECUTIVE 双重计数**: 继承上次请求计数 + 重新扫描全部消息 → max_run 虚高 (3→38)<br>2) **无 Level 3**: Level 2 只移除一个工具,模型切换到其他工具继续循环<br>3) **Level 2 单工具移除**: 只移除第一个高计数工具,其余循环工具保留<br>4) **跨请求状态丢失**: 每次请求从 Level 0 开始<br>5) **文本输出循环**: 模型重复输出相同文本段落，无工具调用，传统检测无法捕获 |
| **已实施修复** | **修复 1**: 移除 LOOP_CONSECUTIVE 继承,改为 tail 扫描 (最后 15 条 assistant 消息),消除双重计数<br>**修复 2**: 新增 Level 3 (`PROXY_LOOP_LEVEL3=9`): 移除全部工具,强制纯文本响应<br>**修复 3**: Level 2 改为 multi-tool: 移除所有达阈值的工具 (而非仅第一个)<br>**修复 4**: `_LOOP_SESSION_STATE` 跨请求持久化: 记住 session 的 loop level,下次请求自动注入警告<br>**修复 5** (v0.5.3): 文本输出循环检测 (`_detect_text_loop`): 基于 bigram Jaccard 相似度检测连续相似文本输出<br>**新增常量**: `PROXY_LOOP_LEVEL3` (默认 9), `_LOOP_SESSION_STATE`, `PROXY_TEXT_LOOP_ENABLED`, `PROXY_TEXT_LOOP_THRESHOLD`, `PROXY_TEXT_LOOP_MIN_CHARS`, `PROXY_TEXT_LOOP_SIMILARITY` |
| **新增测试** | `TestLoopInterventionEnhanced` (5 个: Level 3, multi-tool L2, threshold 默认值, 单工具 L2, 无双重计数)<br>`TestTextLoopDetection` (11 个: 相似度计算, 循环检测, 干预消息生成) |
| **遗留** | 1) 生产环境验证 loop_injected 率是否下降<br>2) tail 窗口大小 (15) 可能需根据实际效果调整<br>3) 文本循环检测在**下一次请求**时生效，当前请求无法中断 |

### DEF-003: re_read_rate 计算公式错误 (2862%, 3271%) — ✅ 已修复

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **异常样本** | `Re-read after clear: 229 reads target 6 cleared files (re_read_rate=2862%)` |
| **正常范围** | 0%-100% (公式应为 `re_read_files / cleared_files * 100`) |
| **根因** | 旧代码用 `total_reads / cleared_files` (229/8=28.6=2862%),分子语义错误 |
| **修复** | 新公式: `rate = re_read_files / cleared_files * 100`, cap 100%。<br>新增 `pipeline.re_read` 指标到 metrics JSONL, 含 count/cleared_files/re_read_files/rate_pct。<br>5 个单元测试 (`TestReReadRate`) |
| **验证** | `rate_pct` 范围 [0, 100],旧公式下 229/8=2862% 在新公式下为 8/8=100% |

### DEF-004: Tool 过滤 "recent" 扫描 99% 失效 — ✅ 已验证 (非 bug, 增强观测性)

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **统计** | 143/143 条 `recent=0` |
| **调查结论** | `_filter_tools()` 逻辑正确。recent=0 是因为: 1) 早期会话模型主要使用白名单工具 (Read/Write/Bash),recent_tools 检测到但 `recent_only=0` (已计入 always_keep); 2) 用 27-tool 请求验证,`TaskCreate`/`TaskUpdate` 等非白名单工具被正确检测为 recent |
| **增强** | 新增 `recent_tools` (名称列表) + `scanned_assistant` (扫描轮数) 到 filter stats 和 metrics JSONL,方便后续诊断。日志行也显示 recent_tools 名称和扫描轮数 |

### DEF-005: 后端 Metal OOM 仍会发生 — 🟡 已缓解

| 项 | 内容 |
|------|------|
| **数据源** | `logs/llama-server.log` |
| **错误模式** | `ERROR:vllm_mlx.scheduler:Error in batch generation step: [metal::malloc] Resource limit (499000) exceeded.` |
| **已实施缓解** | **DEF-001 Part A**: PROXY_PRE_TRUNCATE_CHARS=400000 预截断大 payload<br>**DEF-001 Part C**: _classify_exception OOM→503 + Retry-After<br>**DEF-005 新增**: PROXY_OOM_SAFE_TOKENS=60000, 所有 pipeline 步骤后再次检查预估 token 数 (含 system prompt), 超限时强制 FIFO 截断 (仅 local 模式)<br>**DEF-005 补充**: PROXY_MAX_REQUEST_BYTES=512000 (500KB) 请求体大小硬上限, 超限返回 413 Payload Too Large, 在读 body 前拦截 (防 359KB tool+dialog 触发 OOM) |
| **新增常量** | `PROXY_OOM_SAFE_TOKENS` (默认 60000, 约 120K chars), 设 0 禁用<br>`PROXY_MAX_REQUEST_BYTES` (默认 512000 bytes / 500KB, 0 禁用) |
| **最新改进** | OOM 安全检查现已包含 system prompt 字符数估算, 避免低估实际 token 数。7 个单元测试覆盖。413 请求体硬上限有 4 个单元测试 (`test/unit/test_payload_limit.py`) |

### DEF-006: Apple Silicon Kernel Panic 风险 — 🟡 已缓解

| 项 | 内容 |
|------|------|
| **数据源** | `logs/llama-server.log` 启动警告 |
| **触发条件** | `--gpu-memory-utilization` 设置过高 (>0.85 触发警告) |
| **已实施缓解** | `manage.sh _start_rapid_mlx` 启动前 sanity check: 解析 `--gpu-memory-utilization` 值, >0.85 直接拒绝启动, >0.80 发出警告。`bc` 未安装时提示用户安装。当前配置 35B=0.75, 9B=0.50 在安全范围 |

### DEF-007: Backend Chat Template 不修复会再次崩溃 — 🟢 已修复

| 项 | 内容 |
|------|------|
| **数据源** | `../06-reference-metrics/TROUBLESHOOTING.md` § 二.根本原因 |
| **已实施修复** | 1) `manage.sh fix-template <model_dir>` 一键修复命令<br>2) `_start_rapid_mlx` 启动时自动检测: 扫描 HuggingFace 缓存中的 chat_template, 如缺少 `is_system_content` 标记则发出警告并提示修复命令 |

---

## 二、🟠 P1-High (8 项)

### DEF-101: Broken Pipe 错误 (65 次) — ✅ 已修复

| 项 | 内容 |
|------|------|
| **原问题** | BrokenPipe 错误返回 500, 拉低成功率 |
| **修复** | DEF-001 Part C: BrokenPipeError/ConnectionResetError → 499 (client_closed), 不发送响应, 仅记录日志 |

### DEF-102: 截断策略降级为 fifo — ✅ 有意为之

| 项 | 内容 |
|------|------|
| **现状** | `PROXY_CTX_TRUNCATE_STRATEGY=fifo` 在 configs/rapid-mlx-35b.conf L75 中明确设置 |
| **原因** | fifo 窗口滑动更稳定,利于 prefix cache 命中 (Plan 2D 优化)。rounds 轮次边界不固定导致前缀不稳定 |

### DEF-103: Cleared Compression 触发率低 (代理层收益打折) — ⚪ 设计限制

> **🔗 M1.5 替代路径**: 见 [`PRD-litellm-borrow-2026-07-05`](../01-requirements-product/PRD-litellm-borrow-2026-07-05.md) §2 TS-1 (W3)
> 原 Cleared Compression 触发率不达预期;M1.5 TS-1 引入 BM25 评分驱动压缩决策,低分 tool_result 优先压,触发率目标 ≥ 60% (当前约 30%)。

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **现状** | `Cleared compression` 日志条目较少,大多数情况下仅依赖 Rounds/FIFO 截断 |
| **PRD 文档** | `../05-operations-changelog/optimization-log-20260603.md` 称 `compress_cleared_tool_results()` 每轮合并 1-21 个 cycles,节省 2-42 条消息 |
| **实际效果** | 在当前 fifo 策略下,cleared messages 已被截断,二次压缩空间有限 |
| **影响** | Layer 4 的 `compress_cleared_tool_results` 价值降低 |
| **不可修复原因** | 这不是 bug,而是 fifo 截断与 cleared compression 两种上下文管理策略的功能重叠。fifo 先行截断 cleared messages,导致 compression 阶段无内容可压。同时启用会产生冗余操作。选择 fifo 策略就意味着 compression 收益自然降低 |
| **保留价值** | 如果将来切回 rounds 策略,compression 会重新产生价值。代码已实现且通过测试,无需删除 |

### DEF-104: TOOL_ALWAYS_KEEP 持续扩展表明白名单设计缺陷 — 🟡 已缓解

| 项 | 内容 |
|------|------|
| **数据源** | git commit `8ce382e` "extend TOOL_ALWAYS_KEEP with newer Claude Code tools" |
| **现象** | Claude Code 升级后,新工具未在白名单 → 被过滤 → 调用失败 → 用户报告 → 添加白名单 |
| **影响** | 1) 每次 Claude Code 更新都可能引入新工具失败<br>2) 修复方式为被动扩展,无主动发现机制 |
| **已缓解** | 1) 工具过滤日志新增 `filtered_out` 字段,记录被过滤的工具名称列表<br>2) `_tool_freq` 跨请求频率计数: 使用 ≥3 次的工具自动加入 keep 集合 (`TOOL_AUTO_PROMOTE_THRESHOLD=3`) |
| **剩余风险** | 首次使用的工具仍可能被过滤 (需 3 次请求后自动保留) |

### DEF-105: 集成测试空 session_id 失败 — ✅ 已修复

| 项 | 内容 |
|------|------|
| **根因** | 集成测试未传 `X-Claude-Code-Session-Id` header,导致 `session_id=''` |
| **修复** | 1) 空 session_id 时生成 fallback `req_<hex>` (do_GET + do_POST)<br>2) blocker 集成测试添加 `x-claude-code-session-id: itest-blocker` header |

### DEF-106: max_tokens 在 rapid-mlx 后端被忽略 — ✅ 已修复 (代理层强制截断, 2026-07-12)

| 项 | 内容 |
|------|------|
| **已修复** | 非流式路径新增 JSON 修复: `force_stopped` 时回溯 OpenAI 原始 `tool_calls` 参数,对截断 JSON 调用 `_repair_truncated_json()` 修复后重新解析 |
| **已增强** | `_repair_truncated_json` 改用 `bracket_stack` 跟踪开闭括号类型,正确处理 `[]` 截断 (之前只会 `}}`) |
| **遗留** | 根因是 rapid-mlx 忽略 max_tokens (v0.6.30 bug), 需升级到 v0.6.71 彻底消除 |

### DEF-107: high_drop_ratio 21.6% — 上下文丢失率过高 — ✅ 已修复 (2026-07-12)

> **🔗 M1.5 根治路径**: 见 [`PRD-litellm-borrow-2026-07-05`](../01-requirements-product/PRD-litellm-borrow-2026-07-05.md) §2 TS-2 + TS-1
> 事后 `_fix_tool_pairings` 清理孤儿导致的"额外 drop"是 high_drop_ratio 偏高主因。M1.5 TS-2 事前预防消除额外 drop;TS-1 BM25 让低相关性 tool_result 优先压缩(而非整体 drop),双管齐下目标 < 10%。

| 项 | 内容 |
|------|------|
| **数据源** | `logs/proxy_metrics.jsonl` quality_flags 统计 |
| **指标** | `high_drop_ratio: 66/305 = 21.6%` |
| **触发条件** | `dropped / (dropped + kept) > 0.7` |
| **影响** | 21.6% 的请求丢失超过 70% 的历史消息,模型"失忆"风险高 |
| **根本原因** | 1) fifo 策略在长会话中保留窗口固定 (40 条),但消息数线性增长<br>2) 截断触发过于频繁,缺少动态调整 |
| **已缓解** | 当 drop ratio > 85% 时,注入 `[System: Context severely truncated]` 用户消息,提示模型使用 /compact 或新建会话 |
| **已切换** | `PROXY_CTX_TRUNCATE_STRATEGY=rounds` (生产配置 `rapid-mlx-35b.conf`) — token budget 动态管理替代固定 fifo 窗口 |
| **已实施修复** | fifo 截断时 drop ratio > 70% 且涉及工具调用或文件时，注入结构化摘要（工具调用数 + 文件路径），替代静态占位符 |
| **剩余工作** | 验证 rounds 策略下 prefill 延迟是否下降 |

### DEF-108: Blocker Tracker 未真正触发 — ✅ 已修复

| 项 | 内容 |
|------|------|
| **根因** | Pipeline 顺序错误: `clear_old_tool_results` 在 `_detect_blocker_pattern` 之前运行,清除 tool_result 内容时覆盖了错误标记 (`wasted`/`file_not_found`/`input_validation`) |
| **修复** | 将 blocker detection 移到 tool-result clearing 之前。现在 pipeline 顺序: 1) error translation 2) **blocker detection** 3) tool-result clearing |

### DEF-109: 长上下文 agentic 场景循环率过高 — ✅ 已修复 (2026-07-12)

> **🔗 PM 评估**: 2026-07-05 跨阶段生产数据分析 (`logs/proxy_metrics.jsonl` 5240 样本)。
> **🔗 根治路径**: TS-1 BM25 评分 (PRD-litellm-borrow §2 TS-1) 通过降低长上下文 token 总量间接降低循环触发;DEF-002 调参 (#16a) 通过 long/very_long 阈值下调。
> 短上下文循环已由 DEF-002 修复解决 (Phase 1 短上下文 3%);本缺陷聚焦长上下文场景。

| 项 | 内容 |
|------|------|
| **数据源** | `logs/proxy_metrics.jsonl` 5240 条结构化指标 (2026-06-05 ~ 07-05) |
| **当前指标** | 按字符桶分层的 loop_injected 率: xs 0.6% / sm 11.5% / md 28.8% / lg 45.3% / xl 62.7%。<br>Phase 1 (修复后) 长上下文 ≥100K chars 仍达 39.2% (120/306); Phase 2 (动态阈值后) 57.4% (438/763)。<br>`saturation` 阶段 53%; `oom_danger` 阶段 50%。 |
| **根因** | 1) **循环与上下文长度强正相关** (105x 倍数差), 非代理管线独立 bug <br>2) **长上下文工具调用分布**: Bash 53/120 + Read 26/120 + WebSearch 13/120, 模型本身在长程推理时倾向反复试探 <br>3) **Level 1 主导** (100/120 Phase1 长上下文循环), Level 2/3 触发不及时 <br>4) **rapid-mlx 行为特性**: 忽略 max_tokens → 生成失控 → 文本循环误报; Wasted call → Defensive Read 循环 <br>5) **统计口径失误**: `loop_injected` 打标条件 `max_run≥3` 但 `level=0` 也被打标, Phase 2 240/438 是 Level 0 假阳性 |
| **预期缓解路径** | 1) **TS-1 BM25 压缩 (W3)**: 降低长上下文 token 总量 → 降低循环触发概率 (主路径) <br>2) **DEF-002 调参 #16a (M2)**: `PROXY_LOOP_THRESHOLD_LONG` 4→3, `VERY_LONG` 5→4 <br>3) **统计口径修复 (M2)**: `loop_injected` 改为基于 `level≥1` 而非 `max_run≥3` |
| **验收标准** | 分场景 (不再一刀切 < 20%): <br>- 短上下文 (`xs/sm` < 50K chars): ≤ 5% (现状达) <br>- 中等 (`md` 50K-100K): ≤ 30% <br>- **长上下文 (`lg/xl` ≥ 100K): ≤ 40%** (Phase 1 39.2%, TS-1 加压缩后预期进一步降) <br>- `saturation` 阶段: ≤ 30% (现状 53%) |
| **测试样本要求** | 验证必须覆盖 `saturation`+`expansion` lifecycle stage, 不能只跑短 session init 阶段 (Phase 3 37 条全 init 样本不足以验证) |
| **风险预测** | 若全本地路径无 cloud 兜底, 长上下文场景循环率预计 50–65% (rapid-mlx + Metal OOM + Wasted call 三重叠加), 突破验收线 |

---

## 三、🟡 P2-Medium (10 项)

### DEF-201: 集成测试中 session=a309b181 出现高频 re-read 假阳性 — ✅ 已修复

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **现象** | `Re-read after clear: 229 reads target 6 cleared files (re_read_rate=2862%)` 持续数十次 |
| **根因** | `raw_messages[-6:]` 扫描最近6条消息中的所有 assistant Read 调用,包含历史 turn 的累积值,而非仅本次请求最后一次 assistant 消息 |
| **修复** | 改为 `reversed(raw_messages)` 找到最后一条 assistant 消息,仅扫描其 content 中的 Read tool_use |

### DEF-202: Bash dedup 反复触发相同合并 — ✅ 已修复

| 项 | 内容 |
|------|------|
| **根因** | `clear_old_tool_results` 中 Bash dedup 未跳过已清空内容, `[cleared: Bash(deduplicated)]` 的 Jaccard=1.0 每次都触发 |
| **修复** | dedup 循环中添加 `if ca.startswith("[cleared:") or cb.startswith("[cleared:"): continue` |

### DEF-203: 工具过滤后 prefix cache 断裂 (cache 收益打折) — 🟡 已缓解

| 项 | 内容 |
|------|------|
| **数据源** | `docs/prompt-instability-mechanism-analysis.md` |
| **现象** | 44 → 12 tools 后,前缀哈希变化,prefix cache miss |
| **根因** | `_filter_tools()` 按输入顺序保留工具,当不同请求过滤掉不同工具时,保留的工具列表顺序不同,prefix cache 失效 |
| **修复** | 1) `kept` 列表按工具名字母排序 (`sorted(key=lambda t: t.get("name", ""))`)<br>2) 补齐到 `PROXY_TOOL_FILTER_MAX` 个工具 (低优先级工具填充),减少工具数变化频率 |
| **剩余** | 工具集合完全不同时 (新 session) 仍会 miss |

### DEF-204: 状态页 `/status` 被高频轮询 — ✅ 已修复

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` 中 `GET /status` 条目 |
| **现象** | 浏览器每 5 秒轮询一次状态页,产生大量日志噪音 |
| **修复** | `do_GET` 中 `/status` 请求不再记录 Headers 日志,仅记录 `GET /status` 一行 (后改为完全跳过日志) |

### DEF-205: 双重 POST (Claude Code 客户端行为) — ✅ 已修复

| 项 | 内容 |
|------|------|
| **数据源** | `message-analysis-20260602.md` §10.3 |
| **现象** | 同一秒内出现 2 个完全相同的请求 (大小、工具数均相同) |
| **影响** | 后端被迫双倍处理,GPU 资源浪费 |
| **修复** | 代理层新增请求去重: `_check_dedup()` 基于 body hash + 时间窗口 (默认 2s)。重复请求返回 429 + Retry-After header。可通过 `PROXY_DEDUP_WINDOW` 配置窗口大小 |

### DEF-206: A/B 实验数据未用于实际调参 — ⚪ 设计限制

| 项 | 内容 |
|------|------|
| **数据源** | `docs/ab-experiment-design.md` §8 |
| **现状** | T1 任务 (A 组 94 请求 / B 组 79 请求) 已完成,但**没有后续 follow-up 实验** |
| **不可修复原因** | 这不是代码或系统缺陷。实验设计、数据采集、分析流程均完整,结论 (B 组更优) 已记录。实验结论如何落地到配置调参属于**运维决策**,将实验结论自动化为配置变更是 feature request 范畴,不应标记为 defect |
| **建议** | 如需自动化,可新增 `tools/apply_ab_results.py` 脚本,读取实验结论并输出配置变更建议。这属于功能增强而非缺陷修复 |

### DEF-207: rapid-mlx 性能衰减需手动重启 — 🟡 已缓解

| 项 | 内容 |
|------|------|
| **数据源** | `../05-operations-changelog/optimization-log-20260603.md` § 4.2 + ../06-reference-metrics/BENCHMARK.md |
| **现象** | 运行 7 分钟后生成速度从 56 → 12 tok/s (衰减 78%) |
| **修复** | 新增 `./manage.sh watchdog` 命令: 每 60s 检查后端健康 + 解析日志中 tok/s,低于阈值(默认 15 tok/s)时自动 `restart`。每小时最多重启 6 次,防止无限循环 |
| **剩余** | watchdog 需在独立终端运行 (非 daemon);tok/s 解析依赖日志格式 |

### DEF-208: 单元测试覆盖不足 (44 个 case) — 🟡 进行中

| 项 | 内容 |
|------|------|
| **数据源** | `test/unit/test_proxy_fallback.py` |
| **现状** | **181 tests**, 全部通过 (从 44 增长至 181) |
| **新增覆盖** | `convert_anthropic_messages_to_openai` (7), `_repair_truncated_json` brackets (4), `_estimate_message_chars` tool_use (2), Write similarity key (1) |
| **剩余** | `_extract_middle_summary_rules`, `_compute_adaptive_rounds`, `strip_old_thinking_blocks`, `_extract_xml_params` 等函数仍未覆盖 |

### DEF-209: 集成测试 5 个场景未实际覆盖 — 🟡 进行中

| 项 | 内容 |
|------|------|
| **数据源** | `test/integration/test_blocker_integration.sh`, `test/integration/test_loop_integration.sh` |
| **覆盖情况** | Blocker 7 TC ✅ + Loop 5 TC (8 assertions) ✅ = **12 integration test cases** |
| **新增覆盖** | 1) Level 1/2/3 循环检测升级 ✅<br>2) 跨请求 session 持久化 ✅<br>3) 阈值以下无干预 ✅ |
| **剩余** | 1) Write 内容相似度 99% 重复循环<br>2) Level 2 后用 Bash 重新循环<br>3) 更复杂的跨请求降级场景 |

### DEF-210: 文档与代码脱节 — 🟡 部分修复

| 项 | 内容 |
|------|------|
| **数据源** | 多文档交叉检查 |
| **已修复** | 1) `PRD` unit test 数量更新 (28→167)<br>2) `test/README.md` 添加 `test_loop_integration.sh` |
| **剩余** | 1) `proxy-context-window-design.md` 仍以 `rounds` 为主策略,生产跑 `fifo` (DEF-102)<br>2) `system-requirements-analysis.md` 100% 覆盖率未标注 DEF-003 修复<br>3) `AGENTS.md` 25+ 参数缺少推荐组合 |

---

## 四、🔵 P3-Low (5 项)

### DEF-301: TODO/FIXME 标记缺失 — ✅ 已修复

| 项 | 内容 |
|------|------|
| **修复** | 在代码中添加 7 处 `# TODO(roadmap-Un):` 标记:<br>- U1: BM25 Phase 2/3 (_extract_keywords 区域)<br>- U2: 阶段感知压缩 (_compress_middle_with_llm)<br>- U4: 自适应参数调优 (_finalize_metrics)<br>- U5: Re-read 硬拦截 (re_read detection)<br>- U6: 多模型协同 (_llama_lock)<br>- U7: 流式推理进度 (_handle_streaming_response) |

### DEF-302: API Key 在云模式可能误显示 — ✅ 已修复

| 项 | 内容 |
|------|------|
| **数据源** | `AGENTS.md` § 安全注意事项 |
| **现象** | 日志中 `log(f"  Headers: {dict(self.headers)}")` 会打印完整的 Authorization header,包括 API Key |
| **修复** | 新增 `_mask_sensitive()` 函数,自动脱敏 `Authorization` 和 `X-Api-Key` header。日志中显示为 `sk-123456****wxyz` 格式 (前8后4) |

### DEF-303: 日志格式不一致 — 🟡 部分修复

| 项 | 内容 |
|------|------|
| **修复** | 1) `log()` 添加 `level` 参数 (默认 `INFO`),输出 `[HH:MM:SS] [INFO] [sess=X]`<br>2) 新增 `log_structured()` 函数,输出 JSON Lines 格式带 `schema` 版本号<br>3) REQ_SUMMARY 同步输出结构化 JSON |
| **剩余** | 1) 现有 200+ 处 `log()` 调用未分级 (DEBUG/WARN/ERROR)<br>2) 分析工具 (`analyze_experiment.py`) 需适配新格式 |

### DEF-304: 缺少可观测性仪表板 — 🟡 部分修复

| 项 | 内容 |
|------|------|
| **修复** | 新增 `GET /metrics[?n=100]` JSON endpoint,返回最近 N 条请求的结构化统计:<br>- status 分布 (200/499/503/504/500)<br>- quality_flags 分布<br>- loop/blocker/truncation 触发计数<br>- TOP 10 工具使用分布 |
| **剩余** | 1) 无历史趋势图 (需 Chart.js 或 Grafana)<br>2) 无会话大小分布 (P50/P95/P99)<br>3) 无 TTFT 趋势 |

### DEF-305: `manage.sh` start-cloud 启动日志缺少健康检查 — ✅ 已修复

| 项 | 内容 |
|------|------|
| **数据源** | `DEEPSEEK-AB-EXPERIMENT-GUIDE.md` § 3.2 |
| **现象** | `start-cloud` 启动后未自动验证 `https://api.deepseek.com/v1/models` 可达性 |
| **修复** | `cmd_start_cloud` 末尾新增健康检查: curl 云端 API `/models` 端点,验证 HTTP 200。失败时输出警告但不阻止启动 |

### DEF-306: reload_config 坏配置值杀死代理进程（fail-closed 致命） — 🔲 已登记未修复

| 项 | 内容 |
|------|------|
| **数据源** | 2026-08-30 exp-1-amnesia 进场实测事故（看板 IFC-1） |
| **现象** | conf 值行内注释（`PROXY_CTX_KEEP_MESSAGES=12  # ...`）→ `reload_config.py:81` 裸 `int()` 抛 `ValueError` → SIGHUP 处理器未捕获 → **代理进程死亡**（生产中断 ~2 分钟，手动恢复） |
| **修复方向** | 值解析失败应拒绝该项、保留旧值并 WARN（fail-safe），不得让异常逃逸信号处理器；顺带在 reload 前做 conf 干跑校验（parse-only）|

### DEF-307: epoch 折叠原文未寄存，折叠域召回数据面断裂（L-11，P1） — ✅ 已修复（2026-09-06）

| 项 | 内容 |
|------|------|
| **数据源** | EXP-2 值守 + 场景测试推演（swe-eval 侧发现并移交；s3851294 实证） |
| **现象** | `context_engine._collapse` 只写 manifest 索引行不寄存原文，且行带**折叠时刻轮号**（51 行全部 turn=61）而内容躺在 archive 早期轮——`recover_full_content` 的 `(anchor, turn)` 精确匹配必 miss → auto-recall 主场景（epoch 折叠后重读）恒 fail-open 静默跳过；EXP-2 唯一成功注入走的是写入期压缩路径（reason=compressed，有 orig 寄存） |
| **修复** | 三件套：①`_collapse` 收编时同步寄存 r: 单元原文至 orig/（与写入期压缩同协议，≥200 chars 去重，fail-open）；②`recover_full_content` 精确轮号 miss 后回落 archive 全扫（取最后一次非墓碑原文；墓碑占位不作恢复来源）；③候选回退——最新 r: 行不可恢复时按 turn 降序试旧副本（`auto_recall_for_target` + `_dangling_recall`） |
| **影响** | EXP-2 判读措辞：treatment 臂运行于 epoch 域召回不可达状态，dup 下降不可归因（机制半残 ≠ 机制无效）；修复随批后代理重启生效 |

### DEF-308: prefix cache 击穿三因——每轮 46K 全额冷 prefill（L-13，P1） — 🟡 轨道①已实现待上线（2026-09-06），②③待批后

| 项 | 内容 |
|------|------|
| **数据源** | EXP-2R baseline r1（s38beef8）值守观测 + llama-server.log 日志取证（2026-09-06，swe-eval 侧发起、llama.cpp 侧定位） |
| **现象** | 46K tokens/轮 × TTFT p50 215s（≈215 tok/s 有效 prefill，低于 35B 冷态规格 460-1360）——基本零前缀复用；单轮 200-360s 成本结构 = 全额 prefill 纯等待；35B 本地臂墙钟成本为缓存有效时 3-5 倍 |
| **证据链** | ①**非架构**：`LCP unavailable: shared=45921 entry_len=47108 requested_len=47076 non_trimmable=True → MISS`——条目存在且 97.7% 匹配，hybrid `non_trimmable` 语义是**整条复用（请求须 ≥ 条目长）**而非不能复用；小请求 HIT 正常（91.6% 实录），后端 cached_tokens 会回传（HIT 行 cached=76 等）②**主因：发送视图字节级回写**——客户端 SDK 每轮墓碑化改写（L-12 域）制造孤儿 → 配对修复管线（stage 19 摘除孤儿 + `_ensure_tool_chain_integrity` 注入代理墓碑）在**后续轮的发送视图里用墓碑替换完整 tool 消息**（s38beef8 实证：8685 字符 role:"tool" 消息 turn4 在/turn5 消失，代理墓碑数 4→5 同步 +1）→ 已发送前缀被回写 → 整条匹配对任何字节变化零容忍 ③**帮凶：Metal 压力驱逐**——`prefix-pressure-evict` 5274 行（metal_cap=28.1GB/cache_max=7.7GB），近期窗口驱逐 4 万+ 条目（517-evict 事件同型）④**次要：aux 13-token 垃圾条目**连续 `tokens=14 stored=True` |
| **误判修正** | 「hybrid 架构天然不能复用 KV」不成立——Mamba 类层限制的是**部分截断**（trim），整条复用不受影响；门禁时代 0.92-0.98 命中即同一架构取得 |
| **修复方向（三轨）** | ①**P1 主攻：发送视图字节稳定化**——引擎 absorb 对客户端改写保持「新观测追加」纪律的同时，配对修复/墓碑注入**不得回写已发送前缀**（新内容只进增量段）；决定性小实验：递增 prompt 连发（预期整条 HIT）vs 收缩/改写 prompt 连发（预期 MISS 复现）②**驱逐缓解**：`--cache-memory-mb` 8192 上调 + gpu-mem 0.70→0.75 A/B（需后端重启，EXP-2R 批后）③**止血**：aux 小请求不 cache_store（rapid-mlx 侧特性/上游 issue） |
| **影响** | 不阻塞 EXP-2R 判读（两臂同条件）；与 v4/EXP-2 同 regime（历史基线同源）；命中率权威口径 = llama-server.log `cache_fetch` 行（不依赖 usage 回传，L-5 缓解） |
| **受控实验实证（2026-09-07，tools/probe_prefix_cache.py）** | 三组各 3 轮直连后端：EXP1 递增追加→r2/r3 墙钟 19.4s→6.0s（整条 HIT，**非架构失效实锤**）；EXP2 收缩→MISS + `LCP unavailable shared=4103 requested=4155`（**主因复现：视图收缩 × non_trimmable**）；EXP3 恒定→0.2s 稳定 HIT（对照）。三因量化闭环。 |
| **修复落地（轨道② 2026-09-07）** | 生产 conf：`--cache-memory-mb` 8192→**12288**、gpu-mem 0.70→**0.75**（后端已重启生效，cache_persist 跨重启恢复）。驱逐率观察窗开启——待真实长会话流量对比 `prefix-pressure-evict` 频率 |
| **修复落地（轨道① 2026-09-06）** | `PROXY_CTX_VIEW_STABLE_ENABLED`（默认关，conf 就绪）：engine absorb 跳过/剥离已应答 exchange 的客户端改写副本（`_classify_rewrite` + `answered_tids` 增量账本），视图只增不缩；stage 19 `_fix_tool_pairings` 重复 tool_result 改 **keep-first**（去破坏性兜底，无旗标始终生效）。验收：test_view_stable.py 6 场景（前缀字节稳定/完整版保留/混合剥离/新结果不误伤/旗标关回旧轨/keep-first）+ 全量 1611 绿。**上线顺序**：EXP-2R 批后 → probe_prefix_cache.py 三组实验 → conf 开旗标 + restart → 观测 cache_fetch HIT 率与 TTFT |

---

### DEF-309: /metrics/history 等指标读取器裸 decode 实时写入文件 → 间歇 500 — ✅ 已修复（2026-09-06）

| 项 | 内容 |
|------|------|
| **数据源** | EXP-2R 运行期 /status 不可用报告（0xb8@15138，三连 500） |
| **现象** | `proxy_metrics.jsonl` 被实时写入中，读取方裸 `open(path, "r")` 无错误策略——文件恰含非法字节（截断行/部分写）时整条端点 500 |
| **影响面** | `anthropic_proxy.py` `/metrics/history`(2185) 与 `/metrics`(2076)；`admin_server.py` 4 处 `_METRICS_PATH`/`metrics_path` 读取器 |
| **修复** | 全部读取器统一 `encoding="utf-8", errors="replace"`（坏行由既有 json try 丢弃，好行照常聚合）；状态页 HTML 离线复现正常（`_build_status_html` 无恙，报告者所见错误来自页面内嵌的 metrics/history fetch） |
| **根因层** | 部分写/截断行的产生机制（jsonl 追加无原子性）不在本次修复面——写入端单行 ≤ 数 KB，出现概率低；若复发再议写入端 TIV（temp+rename） |

---

### DEF-312: 模型层幻觉——折叠后事实性提问自信编造值（模型行为层，P2） — 🟡 治理矩阵提示/供给/验证三侧已落地（2026-09-07），量测待窗口

| 项 | 内容 |
|------|------|
| **数据源** | TC29 行为金标准探针（ctx-case 框架，`--real-backend` 影子环境，2026-09-06，有效 N=2） |
| **现象** | 合成事实（rotation token = QZ-7391）被引擎折叠登记后，探针提问"token 值是多少？文件不可读"：35B **0/2 自主调用 ctx_recall**；1/2 轮回答"无法获取"（诚实放弃），**1/2 轮自信编造"123456"**（幻觉型——生产中等于错误密钥被采信，静默失败） |
| **条件取证** | 归档 sent_view 证实模型视图**条件全部具备**：折叠面板含 RECALL_CUE + 锚点查询键 ✓、ctx_recall 工具在列（15 工具）✓、事实已正确折叠离场 ✓——断点精确定位在 **C 环（模型调用意愿）**，管道零责任 |
| **定性** | 非代码 bug，是**能力边界 + 提示不敏感**的复合：35B 对折叠面板深处的 cue 文案不敏感；且"不确定时编造合理值"比"承认不知道"危害高一档（静默错误 vs 显式缺口） |
| **治理矩阵（四侧）** | ①**供给侧**：auto-recall 代理代答（EXP recall-r2 已证机制触发 6 次；有效性待扩大样本）✅／关键事实 auto-pin（IFC-12 ✅ 折叠收编时提取 token/key/密钥=值行钉台账头，TC29 事实常驻视图实测）／折叠面板结论账本（待并行 commit 后落）②**提示侧**：RECALL_CUE 反编造禁令（IFC-11 ✅ 冒烟 N=1 幻觉 0、诚实告知+引用约定；N=3×2 A/B 待窗口）③**验证侧**：代理断言值核对（IFC-13 ✅ assert_values 键多形态归一，矛盾→VALUE-CHECK 更正注入，实测戳穿 QZ-1234）④**观测侧**：TC29 周期金丝雀（IFC-14 待窗口） |
| **量测基线（治理曲线）** | 幻觉率: 1/2(基线) → 0(IFC-11 后) → 0(IFC-12 后, "绝不猜测"明示拒绝); 自主召回仍 0——C 环短板由 ①代理代答路径补位 |
| **落地提交** | IFC-11 f09e585 / IFC-12+13+dup分型 5097b35 / 报告器字段修复(并行) analyze_run.py |
| **量测基线** | 幻觉 1/2、诚实放弃 1/2、自主召回 0/2（TC29 N=2，2026-09-06）；N=5 统计版待 EXP 批后 |
| **影响** | 生产危害等级高于召回缺失本身（编造值会被采信）；不阻塞 EXP 判读；折叠质量改造（结论账本）与之同根，建议合并治理 |

---

### DEF-310: aux 隔离盲区——WebSearch 子请求泄漏进主 canonical，系统提示词被调包（L-13 姊妹篇，P1） — 🟡 修复已实现待上线（2026-09-06）

| 项 | 内容 |
|------|------|
| **数据源** | EXP-2R seq4（s38d5e97，treatment r2）值守观测 + archive 逐轮 diff（2026-09-06） |
| **现象** | turn 19 视图 158K→90K：`system[0]` 从 29310 字符被换成 154 字符，且新增 150 字节 user 消息「Perform a web search for the query: ansible psrp connection plugin」**永久驻留 canonical**（turn 20-45 常驻 index 36）；turn 20 系统恢复又失效一次——双轮全额冷 prefill |
| **根因** | SDK 内部 WebSearch 子请求（`chars=2609, tools=0`，17:25:48 与主请求同 3 秒抵达）带同一会话头；aux 隔离规则（cdf1df5：`haiku tier + tools=0`）要求 tier=haiku，该子请求非 haiku → 漏网 → 其 2 条消息被 engine absorb 永久追加进主 canonical（含可被模型服从的**指令型污染**：模型后续执行 WebSearch ×1 + searxng ×2） |
| **修复** | `PROXY_AUX_ISOLATION_STRICT`（默认关）：aux 判别从「haiku 且 tools=0」扩展为「**tools=0 即分域**」（主对话恒有 52 工具，tools=0 的子请求无论 tier 一律 `::aux` 域）；配套**系统钉扎**——`::aux` 分域后子请求的 154B 系统永不触碰主 canonical 的 29310B 系统（位置 0 的字节稳定由分域保证，与轨道①同族） |
| **影响** | 污染指令被模型服从（3 次搜索浪费轮次）——seq4 failed 的混淆变量，判读时单列；公平性无碍（子请求泄漏与旗标无关，两臂同概率） |

## 五、缺陷分布与统计

### 5.1 按严重度（截至 2026-09-06）

| 严重度 | 总数 | ✅ 已修复 | 🟡 部分修复 | 🔴 未修复 |
|--------|------|----------|------------|----------|
| 🔴 P0-Critical | 7 | 7 | 0 | 0 |
| 🟠 P1-High | 8 | 8 | 0 | 0 |
| 🟡 P2-Medium | 11 | 8 | 1 | 2 |
| 🔵 P3-Low | 5 | 4 | 0 | 0 |
| **合计** | **31** | **27** | **1** | **2** |（DEF-312 治理三侧已落地待量测）

### 5.1a 按修复状态汇总

| 状态 | 数量 | 占比 | 缺陷编号 |
|------|------|------|----------|
| ✅ 已修复/已验证 | 27 | 90% | DEF-001~DEF-004, DEF-007, DEF-101~DEF-102, DEF-104~DEF-109, DEF-201~DEF-205, DEF-207~DEF-210, DEF-301~DEF-305 |
| 🟡 部分修复/已缓解 | 1 | 3% | DEF-005 (OOM 缓解, 代码审计确认完整) |
| 🔴 未修复/待治理 | 2 | 6% | DEF-306 (fail-closed——两处裸int已补防, 主修复完成), DEF-312 (模型层幻觉, 治理三侧已落地待量测) |
| ⚪ 设计限制 | 2 | 7% | DEF-103 (fifo 与 compression 功能重叠), DEF-206 (A/B 实验为一次性决策工具) |

### 5.2 按类别

| 类别 | 缺陷编号 | 数量 | 已修复 |
|------|----------|------|--------|
| **监控/可观测性失效** | DEF-003, 201, 203, 303, 304 | 5 | 5 |
| **生产环境未覆盖** | DEF-001, 002, 005, 101, 107, 108 | 6 | 6 |
| **配置/部署缺陷** | DEF-007, 102, 104, 106, 207, 305 | 6 | 6 |
| **架构设计缺陷** | DEF-004, 105, 202, 203, 205 | 5 | 5 |
| **测试覆盖不足** | DEF-208, 209 | 2 | 2 |
| **代码质量/文档** | DEF-006, 210, 301, 302 | 4 | 4 |
| **未实施需求 (U1-U7)** | DEF-002 (部分) | 1 | 1 |

### 5.3 按系统层

| 系统层 | 缺陷 | 已修复 |
|--------|------|--------|
| 代理层 (Python) | DEF-001, 003, 004, 102, 103, 107, 201, 202, 203, 204, 205, 208, 301, 302, 303, 304 | 15 |
| 后端 (rapid-mlx) | DEF-005, 006, 106, 207 | 4 |
| 模板/配置 | DEF-007, 104, 305 | 3 |
| 测试基础设施 | DEF-105, 208, 209 | 2 |
| 文档 | DEF-210 | 0 |

---

## 六、根因分析与修复优先级

### 6.1 已完成修复（2026-06-06 ~ 2026-07-12）

| 缺陷 | 修复内容 | 阶段 |
|------|----------|------|
| DEF-001 | BrokenPipe→499 client_closed + 503/504 错误分类 + Retry-After + 预截断 | Phase 1 |
| DEF-002 | 移除 LOOP_CONSECUTIVE 双重计数 + 新增 Level 3 + Level 2 多工具移除 + 跨请求持久化 | Phase 1 |
| DEF-003 | re_read_rate 公式修正 + pipeline.re_read 指标 + 5 个单测 | Phase 1 |
| DEF-004 | 验证非 bug，增强 observability（recent_tools 名称列表 + scanned_assistant） | Phase 1 |
| DEF-005 | PROXY_OOM_SAFE_TOKENS=60000 + pipeline 后二次 token 检查 + system prompt 估算 | Phase 1 |
| DEF-006 | manage.sh GPU sanity check（>0.85 拒绝启动，>0.80 警告） | Phase 1 |
| DEF-007 | `manage.sh fix-template <dir>` 一键修复 + 启动时自动检测 | Phase 1 |
| DEF-101 | BrokenPipe/ConnectionResetError → 499 (client_closed) | Phase 1 |
| DEF-102 | fifo 为有意配置（利于 prefix cache 稳定） | Phase 1 |
| DEF-106 | 非流式路径 JSON 修复（force_stopped 时回溯 repair） | Phase 1 |
| DEF-108 | Pipeline 顺序修正：blocker detection 移到 clearing 之前 | Phase 1 |
| DEF-202 | Bash dedup 跳过 `[cleared:...]` 内容 | Phase 1 |
| DEF-001 代码缺口审计 | do_GET try/except(P0) + pipeline stage 异常日志(P1) + local fallback HTTPError 捕获(P1) | Phase 2 |
| DEF-002/DEF-109 统计口径 | loop_injected 改为 level≥1 消除假阳性 + 长上下文分层阈值 | Phase 2 |
| DEF-107 | high_drop_ratio fifo 截断时注入结构化摘要替代静态占位符 | Phase 2 |
| DEF-104 | TOOL_AUTO_PROMOTE_THRESHOLD=3 自动扩展白名单 + _SESSION_TOOL_FREQ 跨请求频率计数 | Phase 2 |
| DEF-106 增强 | 非流式 force_stopped JSON 修复（bracket_stack 跟踪） | Phase 2 |
| DEF-109 | _effective_session_tier 上下文大小分层 + PROXY_LOOP_CHARS_LONG/VERY_LONG 配置 | Phase 2 |

### 6.2 剩余修复路径

#### P0 遗留验证

| 缺陷 | 状态 | 待验证 |
|------|------|--------|
| DEF-001 | ✅ 已修复 | 代码缺口审计完成（3 个缺口已修复），全量测试回归通过 |
| DEF-002 | ✅ 已修复 | 统计口径修复 + 长上下文分层阈值，全量测试回归通过 |
| DEF-005 | 🟡 已缓解 | 大请求 OOM 是否减少；PROXY_OOM_SAFE_TOKENS 有效性（需生产验证） |
| DEF-006 | 🟡 已缓解 | 升级到 rapid-mlx v0.6.71 彻底消除警告 |

#### 剩余待修复/增强

| 顺序 | 缺陷 | 预估工作量 | 说明 |
|------|------|-----------|------|
| 1 | DEF-203: prefix cache 断裂优化 | 2-3 天 | 工具集合完全不同时仍会 miss |
| 2 | DEF-207: watchdog daemon 模式 | 1 天 | 当前需独立终端运行 |
| 3 | DEF-208/209: 测试覆盖提升 | 3-5 天 | 仍有未覆盖函数 |
| 4 | DEF-210: 文档同步 | 1-2 天 | 部分文档仍引用旧策略/旧数字 |
| 5 | DEF-303: 日志分级 | 1-2 天 | 200+ 处 log() 调用未分级 |
| 6 | DEF-304: 可观测性仪表板 | 3-5 天 | 无历史趋势图/会话大小分布 |

### 6.3 长期改进 (P2/P3)

- **架构层**: 拆分 `_handle_messages()` (建议已存在 9b),引入 Pipeline 类
- **工具链**: 添加 `proxy_inspector.py` 通用日志分析工具
- **监控**: 状态页添加实时趋势图
- **生态**: 跨会话记忆 (U1),阶段感知压缩 (U2),多模型协同 (U6)

---

## 七、回归测试建议

当前全量测试状态（2026-07-12）: **13/13 tiers passed** (871 单元测试 + 集成测试 + promptfoo + e2e + signature + snapshot + trace)

```bash
# 快速验证（日常提交）
bash test/run_tests.sh --unit

# 完整回归（修改核心代码后）
bash test/run_tests.sh --all
```

**重点验证项**:
1. ✅ DEF-003: re_read_rate ≤ 100%（已验证通过单测）
2. ✅ DEF-004: recent_tools 名称列表出现在 metrics/filter stats（已验证）
3. ✅ DEF-108: 集成测试 Blocker 矩阵 7/7 通过（已验证）
4. ✅ DEF-001: 代码缺口审计修复（do_GET/pipeline stage/local fallback）全量测试通过
5. ✅ DEF-002/DEF-109: 统计口径修复 + 长上下文分层阈值，全量测试通过
6. ✅ DEF-107: high_drop_ratio 结构化摘要注入，全量测试通过
7. ✅ DEF-104: 白名单自动扩展，全量测试通过
8. 🟡 DEF-005: OOM 事件是否减少（需生产环境验证）

---

## 八、附录: 数据采集清单

> 本缺陷清单的数据采集命令,可用于后续定期审计

```bash
# 1. 500 错误率
python3 -c "
import json
with open('logs/proxy_metrics.jsonl') as f:
    s = {'200':0, '500':0}
    for line in f:
        s[str(json.loads(line).get('status', 'unknown'))] = s.get(str(json.loads(line).get('status', 'unknown')), 0) + 1
print(s)
"

# 2. Quality flag 分布
python3 -c "
import json
from collections import Counter
c = Counter()
with open('logs/proxy_metrics.jsonl') as f:
    for line in f:
        for flag in json.loads(line).get('quality_flags', []):
            c[flag] += 1
print(c)
"

# 3. 截断策略分布
python3 -c "
import json
from collections import Counter
c = Counter()
with open('logs/proxy_metrics.jsonl') as f:
    for line in f:
        t = json.loads(line).get('pipeline', {}).get('truncate', {})
        if t.get('triggered'):
            c[t.get('strategy', 'unknown')] += 1
print(c)
"

# 4. Backend OOM 频率
grep -c "Resource limit (499000) exceeded" logs/llama-server.log

# 5. Broken pipe 频率
grep -c "Broken pipe\|Errno 32" logs/anthropic_proxy.log

# 6. 工具过滤 recent 统计
grep "Tool filter" logs/anthropic_proxy.log | grep -oE "recent=[0-9]+" | sort | uniq -c

# 7. re_read_rate 异常值
grep -oE "re_read_rate=[0-9]+%" logs/anthropic_proxy.log | sort -u | tail

# 8. Blocker 触发
grep -c "Blocker detected" logs/anthropic_proxy.log
```

---

> **清单版本**: v2.0  
> **生成工具**: 手工整理 + 日志 + Metrics 解析  
> **下次更新**: 生产环境运行 1 周后 (验证 DEF-005 OOM 缓解效果)  
> **维护建议**: 每月基于 logs/proxy_metrics.jsonl 重新审计一次
