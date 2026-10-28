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

## 二、🟠 P1-High (12 项)

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

## 三、🟡 P2-Medium (15 项)

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

### DEF-211: `engines.sh` 启动的辅助引擎静默丢失 tool calling — ✅ 已修复

| 项 | 内容 |
|------|------|
| **数据源** | 2026-10-20 重启 `ornith-9b` 时实测；`/v1/models` 的 `capabilities` 从 `['text','tools']` 退化为 `['text']` |
| **现象** | 引擎健康、无报错，但带 `tools` 参数请求时 `finish_reason` 不再返回 `tool_calls`，退化为普通文本补全 |
| **根因** | `tools/engines.sh` 的 `_cmd_start()` 与 `manage.sh` 参数拼装分叉：装了 `--tool-call-parser` 却未加 `--enable-auto-tool-choice`，解析器形同虚设。同时漏掉 `--no-thinking`、`--log-level INFO`、`--kv-cache-quantization`（导致 conf 里 `RAPID_MLX_KV_QUANTIZATION=true` 被静默忽略） |
| **修复** | `engines.sh` 补齐四项参数，与 `manage.sh` 语义对齐（+14/-1 行）；同时读取 `RAPID_MLX_KV_QUANTIZATION` / `_KV_QUANT_BITS` / `LLAMA_THINKING` |
| **验证** | `capabilities` 恢复 `['text','tools']`；`finish_reason=tool_calls`、参数 JSON 正确解析 |
| **影响面** | 所有 `engines.sh` 启动的辅助引擎（`configs/*.conf` 除 `active.conf`）；主后端走 `manage.sh` 不受影响 |
| **剩余** | 两个启动入口仍各自维护参数列表，建议后续抽公共构造函数，避免再次腐化 |

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

---

### DEF-313: 工具列表非平稳——tools 块位于 prompt 头部，工具集抖动击穿整条前缀缓存（splash TTFT 长尾主因，P1） — ✅ 已修复（2026-10-20，P0+P1）

| 项 | 内容 |
|------|------|
| **数据源** | 生产值守（splash-qwen36-35b，2026-10-20 14:xx）+ 直连 :8081 最小探针（`tools/probe_tool_prefix_cache.py`）+ `logs/anthropic_proxy.log` + `logs/llama-server.log` + `chat_template.jinja` |
| **现象** | TTFT 长尾：命中态 0.4–0.9s，未命中态 30–90s（`splash_ttft_p95` 39.5s）。后端 prefill 786 tok/s，50K 冷启 ≈ 64s，与实测吻合。工具数在 15↔19 间反复横跳（`28->19` 21 次 / `22->15` 44 次） |
| **根因（新机制）** | Qwen3.6 chat template 把 **tools 块渲染在 prompt 最前**：`<im_start>system\n# Tools\n...<tools>{tool json}...</tools>` 之后才接真实 system + messages。tools 块是**前缀头部**，工具集/顺序/数量任一变化 → 整条前缀失效 → 全额冷 prefill。稳定视图 `PROXY_CTX_VIEW_STABLE_ENABLED` 只处理**消息视图**（SDK tombstone 改写副本），**不触碰 `body["tools"]`**，无法覆盖此段 |
| **探针实证** | 直连 :8081，同一 prompt + 15 工具（tools 块 ≈ 1191 token）：同集重发 **0.22s**（命中）；换名 / 多一个工具 **1.73–2.01s**（全冷）。tools 块确在缓存键内 |
| **工具集非平稳四因** | a) 客户端送来的工具数就在变（`tools=28` 1317× / 26 / 22 / 15 / 11 / 0）<br>b) `keep_set = TOOL_ALWAYS_KEEP ∩ 客户端工具 ∪ recent ∪ auto_promoted`——客户端未送全 always-keep（22/15/11）时数量下降<br>c) `auto_promoted` 随会话工具频率增长（阈值 3）——同会话内也增长<br>d) `PROXY_TOOL_FILTER_MAX=14` 仅**下限**（`if len(kept) < MAX: pad`），**从不裁剪** → 实际发出 15~20 个 |
| **DEF-203 修复为何不足** | DEF-203 只做了 ①按稳定序排序 ②向下填充到 MAX；二者都只稳定**顺序**，不恒定**集合**；且 `MAX` 是下限非上限 |
| **稳定视图排查结论** | `PROXY_CTX_VIEW_STABLE_ENABLED=true` 已加载，但 `view-stable: skipped` 日志 **0 次**；消息视图靠 `absorb()` 的 `sent_set` 去重已稳（prefix ratio 99%+），view-stable 属冗余且**不覆盖 tools** |
| **与 DEF-308 关系** | DEF-308 三因（hybrid `non_trimmable` / 发送视图回写 / Metal 驱逐）针对 rapid-mlx；本条是 splash 模板 **tools 头部**这一**独立**击穿源，机制不同、可单独治理 |
| **修复方向** | 在 `_filter_tools` 层把工具集做成**常量**：① 固定 `TOOL_ALWAYS_KEEP` 白名单 + 关 `auto_promote`/`recent` 动态扩充；或 ② 把 `PROXY_TOOL_FILTER_MAX` 改为**硬上限**（裁剪而非仅填充）；或 ③ tools 块与消息块分离缓存键（需后端配合） |
| **修复落地（2026-10-20）** | **P0（配置）**：`PROXY_TOOL_AUTO_PROMOTE_THRESHOLD=0` + `PROXY_TOOL_FILTER_RECENT=0`，关闭动态扩充，使首次快照确定。<br>**P1（代码）**：新增 `PROXY_TOOLS_STABLE_SESSION`（CONFIG_REGISTRY + proxy_state + 可热重载），`tool_filter.py` 增加 `_SESSION_TOOL_FREEZE`，按 **`(session_id, 客户端工具名集合)`** 冻结过滤输出：同 profile 后续请求逐字节复用（定义从当前请求刷新、客户端缺失时回退快照），不同 profile（受限子 agent）互不污染；`ctx_recall` 不进快照、replay 时重注入。容量上限 512，FIFO 淘汰。<br>**为何不做全局 union**：同 session 内客户端会在 28↔22 工具间切换（实测 `always=9` vs `always=5`，子任务/只读子 agent），union 会把父 agent 的 Write/Edit 塞给受限子任务。 |
| **验收（2026-10-20 22:40）** | 探针（经代理 :4000）：同会话 15 工具连发 + 中途切 22 工具再切回，`cached` 除首条外**恒为 1632/1651 = 98.8%**；生产会话 27B：`cached 38,784/39,718`，后端工具 hash 恒为 `15·0fff7128`，TTFT 从 30–90s 降到 **2–7s**。剩余冷启动仅来自后端重启清缓存 / 新会话 / EPOCH。 |
| **附带观察（同批值守）** | ① **EPOCH 全量重建**：本轮 50 分钟触发 4 次（14:19:51 / 14:24:44 / 14:29:35 / 14:31:15），`Common prefix ratio 0.00%`，#3→#4 仅隔 1.5 分钟（抖动）<br>② **Metal 压力**：`splash_memory_pressure=warning`，headroom 8.77→6.20GB，`state_evictions` 139→175，命中率 99.7%→24.2%<br>③ aux `embedgemma2` 为 `-ngl 0`（纯 CPU），**非** Metal 竞争者 |
| **影响** | 周期性 30–90s 冷 prefill 尖峰，agentic 吞吐较缓存有效时下降 3–5×；每次工具集跳变即触发 |
| **剩余** | 修复方向 ①/② 实现 + 批后探针复测（tools 块恒定 → 冷 prefill 尖峰消失） |

### DEF-314: 云端兜底不认 anthropic 协议 provider，且只试候选链首个（P1） — ✅ 已修复（2026-10-21）

| 项 | 内容 |
|----|------|
| **现象** | `POST /v1/messages` 请求 `claude-haiku-4-5` 返回 503：`Local failed: <urlopen error [Errno 61] Connection refused>; cloud fallback failed: 'choices'` |
| **数据源** | 2026-10-20 23:53 生产值守（`logs/anthropic_proxy.log`）+ `configs/models.json` 路由表 |
| **根因 A（协议）** | local 失败后兜底路径（`pipeline.py` 原 4553-4577）一律走 `_do_dispatch`（OpenAI 解析器）。`zhipu-cn` 是 **anthropic 协议** provider，返回 `{"content":[...]}`，`message_converter.py:810` 读 `openai_resp["choices"][0]` → `KeyError: 'choices'`。正常云端分派（`pipeline.py:4333`）有协议分支，**兜底路径漏了** |
| **根因 B（不遍历）** | 兜底只取**第一个**可用候选即返回，候选 #1 失败（如 zhipu-cn 401）后不会继续尝试链上其余 provider |
| **触发条件** | ① `claude-haiku-4-5` 路由声明 `local_model: ornith-9b`，而 9B 引擎 (:8084) 未常驻 → local 必失败；② 候选链首位为 anthropic-protocol provider |
| **修复（2026-10-21）** | 兜底改为遍历 `_usable()` 过滤后的**完整候选链**（与正常云端分派同口径：跳过冷却/无 key/provider 协议与客户端不兼容）；按 `cand["protocol"] == "anthropic"` 分支走 `_do_dispatch_anthropic`；单个候选 HTTPError/异常记录后 `continue`，全链失败才返回 503（错误含最后失败原因） |
| **验收（2026-10-21 00:00）** | 不带 header 请求 haiku → **HTTP 200**（日志：`zhipu-cn` 401 → `trying next...` → `zhipu/glm-5.3-flash` 200），耗时 19.6s；`test_proxy_fallback` **275 passed**；`run_tests.sh --unit` **2066 passed** |
| **关联变更（同一修复窗口）** | `configs/models.json` 的 `claude-haiku-4-5.local_model` 由 `ornith-9b` 置空 → 9B 未常驻时回落**主引擎**（当时 35B:8081），实测 0.09s / HTTP 200。需要 9B 分流时显式使用 `ornith-9b` 别名 |
| **影响** | 任何 anthropic-protocol provider（zhipu / zhipu-cn / z.ai / kimi）在 local 失败时都不可用，等于本地引擎一挂即整条请求失败 |

### DEF-315: `/api/status` 的 backend.pid 归属错误——pgrep 命中辅助引擎（P2） — ✅ 已修复（2026-10-21）

| 项 | 内容 |
|----|------|
| **现象** | `GET /api/status` 的 `backend.pid = 90927`、`uptime_sec ≈ 178716`（2 天），而真实主后端是 PID 97610 / uptime 19 分钟。`backend.name` 显示 `splash`、`model_name` 正确，仅 **pid/uptime 错位** |
| **数据源** | 2026-10-21 00:1x 生产（`splash-qwen38-27b-mlx`）+ `ps`/`lsof` 交叉校验 |
| **根因** | `admin_server._build_status_json()`（及 `_build_status_html()`）调用 `_get_process_info("rapid-mlx\|llama-server\|dflash\|splash", "Backend")` **未传 `fallback_port`**，于是走 `pgrep -f ... \| head -1`。该正则同时匹配辅助引擎 `tools/bin/llama-server-eg2`（EmbeddingGemma，PID 90927，常驻 2 天）与其它杂项进程，先命中者被当成主后端 |
| **为何危险** | 消费方（agent_go / 告警 / 值守脚本）依赖 `backend.pid` + `uptime_sec` 判断主后端存活；aux 常驻时**主后端挂了也会显示 alive**，且 uptime 完全失真 |
| **修复（2026-10-21）** | 新增 `admin_server._local_backend_port()`：从 `_ps.LLAMA_BASE` 解析端口（`IS_CLOUD` 时返回 `None`）。两处 `_get_process_info(... "Backend")` 均传 `fallback_port=_local_backend_port()`，使 **端口持有者成为唯一权威**——与 `_get_process_info` 自身 2026-09-27 注释（"Port holder first: it is the unique truth"）一致；同款做法此前只用在 proxy（`:4000`）上 |
| **验收（2026-10-21 00:3x）** | 重启代理后 `/api/status` → `backend.pid = 4697`，与 `manage.sh status` 的 PID、`lsof -iTCP:8081` 的持有者**三者一致**；aux `90927` 不再被误报；`state: healthy`、`ready: true`、`name: splash`；`run_tests.sh --unit` **2066 passed**；`--signature` preserved |
| **覆盖范围** | 所有本地后端（rapid-mlx / llama-server / dflash-mlx / splash）统一受益；云模式不受影响（无本地 PID） |

### DEF-316: 动态并发开关标 reloadable 但 `_RELOAD_SPEC` 漏登记（P2） — ✅ 已修复（2026-10-21）

| 项 | 内容 |
|----|------|
| **现象** | 配置写入 `PROXY_DYNAMIC_CONCURRENT_ENABLED=false` 后执行 `./manage.sh reload`，`/status` 仍显示 `Max Concurrent 1 (dynamic)`，`_adjust_concurrency()` 继续把本地并发从 1 抬到 4 |
| **根因 A** | `proxy_config.CONFIG_REGISTRY` 声明这 5 个 `PROXY_DYNAMIC_CONCURRENT_*` 项 `scope=reloadable`，但 `proxy_state._RELOAD_SPEC`（`reload_config` 唯一应用的清单）**未登记**。reload 只遍历 `_RELOAD_SPEC`，未登记 = 不应用 |
| **根因 B（易误判）** | `manage.sh reload` 只重载**配置常量**，不重载**模块代码**。即使补登记 `_RELOAD_SPEC`，运行中的进程仍持有旧模块 → 必须重启进程才生效 |
| **危害** | 48GB 机器上动态并发按 p95<30s 放行，可绕过 AGENTS.md §8.1「本地并发恒 1」红线；实测 prefill 与 decode 抢 GPU，decode 52.9→23.2 tok/s、冷 48K TTFT 259s |
| **修复（2026-10-21）** | `_RELOAD_SPEC` 补登记 `PROXY_DYNAMIC_CONCURRENT_{ENABLED,MIN,MAX,LATENCY_P95_MS,ERROR_RATE}`，默认值与 CONFIG_REGISTRY 对齐（local ENABLED=true / MAX=4；cloud ENABLED=false / MAX=8） |
| **关联配置** | `configs/splash-qwen38-27b-mlx.conf` 与 `splash-qwen36-35b-mlx.conf` 均加 `PROXY_MAX_CONCURRENT=1` + `PROXY_DYNAMIC_CONCURRENT_ENABLED=false` + `PROXY_DYNAMIC_CONCURRENT_MAX=1` |
| **验收（2026-10-21 00:48）** | 重启后启动日志 `Dynamic concurrency: disabled (min=1, max=1)`；`/status` → `Max Concurrent 1`（无 `(dynamic)` 后缀）；`run_tests.sh --unit` **2066 passed** |
| **附带观察** | 同一窗口重启后 ANE 重新校准，`share` 从 0.47 掉到 **0.03**、`minimum_rows` 512→1759、数值偏差 1.5%→**8.2%**、ANE 78.6ms vs GPU 83.9ms（GPU 基线从 48.6ms 升到 83.9ms）。推测为系统级内存压力（swap 3.46GB / 空闲页 ~300MB）拖慢 GPU 基线，ANE 优势随之消失——说明 ANE 收益**依赖机器状态**，需在干净状态下重新校准 |

### DEF-317: `manage.sh` 把 splash 后端误判为 rapid-mlx（P2） — ✅ 已修复（2026-10-21）

| 项 | 内容 |
|----|------|
| **现象** | `./manage.sh status` 显示 `后端: rapid-mlx`（实际 Splash 1.4.0 + 35B OptiQ），且 `内存(Splash)` 行消失、`进程 RSS` 显示的是无关进程 |
| **数据源** | 2026-10-21 21:1x（`splash-qwen36-35b-optiq`，PID 49192） |
| **根因 A（正则）** | `_current_backend()` 对 splash 的判别是 `grep -q 'splash.*server.py'`，而 1.3.x 起的实际启动形态是 `python3 -m server.server ...`（**不是** `server.py`）→ 0 命中，掉进回退 |
| **根因 B（回退过宽）** | 回退第一条是 `pgrep -f "rapid-mlx"`，会命中**任何命令行里含该字符串**的进程。实测命中 PID 56541——一个正在 `cat > configs/archived/README-20261021.md` 的 zsh（heredoc 正文里含 "rapid-mlx" 字样），且该分支排在 `splash serve` 之前 |
| **修复（2026-10-21）** | ① 抽出 `_classify_backend_pid()`，PID 文件路径与端口路径共用；splash 判别改为 `splash.*(server\.py\|-m server\.server)`，两种形态都认。<br>② 回退顺序改为「**先取 `$LLAMA_PORT` 的 lsof 端口持有者**」（唯一权威，与 `admin_server._local_backend_port` 同口径），再退到收紧后的 pattern（`rapid-mlx serve` / `splash serve-native\|splash serve `），**去掉裸 `rapid-mlx` 与裸 `mlx_vlm`** |
| **验收（2026-10-21 21:15）** | `status` → `后端: splash`、`内存(Splash): 22.4 GB` 行恢复；`current` → `后端: splash`；`start` → `后端已在运行 (PID: 49192, backend: splash)`（不再重启后端）；`bash -n manage.sh` 通过 |
| **影响** | 仅观测面。但会误导运维/告警（误以为在跑 rapid-mlx），并让 `内存(Splash)` 等条件分支失效 |

### DEF-318: reasoning_content 未回传为 thinking 块——截断时半截思考冒充正文（P2） — ✅ 已修复（2026-10-21）

| 项 | 内容 |
|----|------|
| **现象** | ① 非流式 Anthropic 响应里**没有** `thinking` 块，思考过程被丢弃（`reasoning_tokens` 计入 usage 但内容不可见）<br>② `max_tokens` 小于思考开销时（如 32），`stop_reason: max_tokens` 且 `text` 块内容是**半截思考**（"Here's a thinking process…"），客户端误以为这是答案<br>③ 流式把 `reasoning_content` 直接并入 `text_delta`，客户端无法区分思考与答案 |
| **数据源** | 2026-10-21 22:xx–23:xx（`splash-qwen36-35b-optiq`，thinking on + effort low） |
| **根因 A（非流式）** | `message_converter.convert_openai_response_to_anthropic` 只在 `content` 为空时把 `reasoning_content` 顶替成 text，**从不生成 thinking 块** |
| **根因 B（流式）** | `anthropic_proxy.py` 流式主循环 `text = delta.get("content","") or delta.get("reasoning_content","")`——思考被当作正文增量发出 |
| **根因 C（引擎侧）** | Splash 1.4.0 **无视** `thinking:{"type":"disabled"}`（后端日志仍是 `effort absent → low · thinking on`），只认 `reasoning_effort:"none"`（→ `effort none · thinking off`） |
| **修复（2026-10-21）** | ① 新增 `PROXY_EMIT_THINKING_BLOCKS`（CONFIG_REGISTRY + proxy_state + `_RELOAD_SPEC`，reloadable，默认 **true**）。<br>② 非流式：`reasoning_content` → `{"type":"thinking","thinking":...,"signature":"proxy-thinking"}`，不再顶替正文；关闭开关恢复旧行为。<br>③ 流式：reasoning 走独立 thinking 块（`content_block_start` + `thinking_delta` + `signature_delta` + `content_block_stop`），块索引改为动态连续 `thinking(0) → text → tool_use`；同时修复旧实现 tool_use 固定从 index 1 起、无文本时 index 0 空缺的问题。<br>④ 流式微轮门槛增加 `_thinking_idx is None` 条件，避免发过思考后被 ctx_recall follow-up 截断。<br>⑤ 路由级 `thinking_disabled`（`models.json` → `get_route` → `SmartRouter` → `FormatConverter`）：同时发 `thinking:{"type":"disabled"}` 与 `reasoning_effort:"none"`，兼顾 deepseek 类与 splash 类后端 |
| **关联配置** | ① `claude-haiku-4-5` 路由加 `"thinking_disabled": true`；`--thinking-token-budget` 2048 → **512**。<br>② `claude-sonnet-4-6` 路由加 `"min_max_tokens": 3000`（2026-10-22，初值 1600）——thinking 与 tool_use 共享同一预算，实测 `max_tokens` ≤512 时思考吃光预算、**拿不到 tool_use**，768 起稳定；单轮委派峰值 839，1600 够用，但**多轮回传后终答会重新思考整段上下文**（实测 think 1952 字符 + 长答案把 1600 打满，`stop=max_tokens` 截断），故提到 3000。实现为**路由级**下限（`get_route` → `SmartRouter._route_min_max_tokens` → `FormatConverter`），同时改写 `openai_body` 与 Anthropic `body`——否则输出截断阈值仍按客户端原值 ×2 计算，会把刚放宽的 tool_call 又截掉。haiku 档不设下限（thinking off，100 足够）。 |
| **验收（2026-10-21 23:15）** | sonnet 非流式 → `["thinking","text"]`（thinking 内容 + 答案 391）；sonnet 流式 → index 0 `thinking_delta` 序列；sonnet + 工具 → 块索引 **0(thinking) → 1(tool_use)** 连续；haiku → 仅 text 块、`output_tokens 4`、后端日志 `effort none · thinking off`；`--unit` **2066 passed**、`--signature` 185 preserved、`--snapshot` 57 cases 全绿 |
| **影响** | 思考对客户端可见且可计量；截断不再把半截思考当答案；haiku 档恢复"快速直答" |

### DEF-319: OpenAI 端点遇 `content: null` 整端点 500（thinking 模型触发，P1） — ✅ 已修复（2026-10-21）

| 项 | 内容 |
|----|------|
| **现象** | `POST /v1/chat/completions`（OpenAI 协议）返回 500：`{"error":{"message":"Proxy error: RuntimeError: Pipeline stage 'backend_dispatcher' failed: TypeError: 'NoneType' object is not subscriptable"}}`，**所有请求必现**（含无 tools 的最小请求） |
| **数据源** | 2026-10-21 23:39–23:41（`splash-qwen36-35b-optiq`，thinking on） |
| **根因** | `anthropic_proxy._handle_non_streaming_response`（OpenAI 分支）`content_summary = message.get("content", "")[:100]`。thinking 模型的 `content` 是 JSON **null**（全部输出在 `reasoning_content` 里），而 `dict.get(k, default)` 在"键存在但值为 null"时返回 **None**，`None[:100]` → TypeError |
| **为何现在才暴露** | 之前生产配置 `LLAMA_THINKING=false`，`content` 恒为字符串；切到 `--default-reasoning-effort low` 后 `content` 变成 null。属**潜伏缺陷被 thinking 模式激活** |
| **修复（2026-10-21）** | ① `(message.get("content") or "")[:100]`。<br>② `pipeline.py` 的 `PIPELINE FAILURE` 处补一条压缩 traceback（`PIPELINE TRACEBACK (stage)`，WARN 级，截断 1200 字符）——此前只有异常消息，`'NoneType' object is not subscriptable` 无法定位，本次靠临时插桩才找到行号 |
| **验收（2026-10-21 23:42）** | OpenAI 端点无 tools → **200**（`content: null` + `reasoning_content`）；带 `task` tool → **200** + `tool_calls[0].function.name="task"` + 正确参数 + `finish_reason: tool_calls`；Anthropic 端点 `Task` 工具 → `stop_reason: tool_use` + `tool_use` 块 |
| **影响** | OpenAI 协议客户端（含任何走 `/v1/chat/completions` 的 agent）在 thinking 模型下完全不可用 |

### DEF-320: 重启留下孤儿引擎 → 内存准入失败 → 服务停摆 5 分钟（P1） — ✅ 已修复（2026-10-22）

| 项 | 内容 |
|----|------|
| **现象** | 01:08–01:13 服务完全不可用（`:8081`/`:4000` 均无监听）。Splash 连续 3 次 bootstrap 失败：`error: runtime bootstrap failed [resource_assembly/model_loading]: not enough free memory to start (1500 MiB reclaimable, 2048 MiB protected for macOS, system pressure warning)`，每次以 `Error · native protocol reached EOF` 收尾。 |
| **影响** | **3 个生产请求被 Cancelled**（`input 59,609` TTFT 252.5s / `input 59,981` TTFT 255.9s / `input 108,706 · cached 0 · tools 54`），其中 108K 那次是重上下文 agent 会话。停服前已有 250s+ 排队积压。 |
| **根因 1（孤儿）** | 01:07:23 `Stopping · releasing engine resources` 后，旧的 `server.server`(97817) 与 `serve-native`(97850，RSS 1.6GB、Metal ~21GB) **没有退出**。新实例启动后把 PID 文件从 97817 改写成 98880 → 旧进程成孤儿。`manage.sh stop` 只按 PID 文件杀一个进程；`_check_port` 也因旧前端已不监听而放行。 |
| **根因 2（内存）** | 两个引擎各需 ~21GB，合计 42GB 逼近 44GB limit，Splash 内存准入直接拒绝（reclaimable 仅 1500 MiB）。 |
| **放大因素** | 系统 swap 已用 7.5GB / 8GB，加大失败概率。 |
| **修复（2026-10-22）** | 新增 `manage.sh:_reap_splash_orphans()`：扫描**不在 PID 文件里**的 Splash 进程（`splash serve-native` + `splash/.*-m server\.server`），SIGTERM → 10s 等待 → SIGKILL 兜底。两处调用：① `_start_splash()` 启动前（先清孤儿再查端口）；② `cmd_stop_backend()` 正常停止后的兜底分支。 |
| **验收（2026-10-22 01:15）** | `stop` → `start` 一轮：发现并清理孤儿 461/482，最终仅剩 1063(前端)/1138(引擎) 单实例；`:8081` 正常监听；`memory_pressure=normal`、headroom 19.46GB、`denied=0`；持久缓存恢复 462 个 restore point；健康请求 `stop=end_turn` / `OK`。 |
| **残留风险** | swap 仍 7.0GB/8GB；`PROXY_MAX_CONCURRENT=1` 下 59–108K 长上下文会排队数分钟。建议择机重启系统释放 swap。 |

### DEF-321: `tools/engines.sh` 与 `manage.sh` 两套 PID 文件互不可见 → 停止辅助引擎误报「未在运行」（P2） — ✅ 已修复（2026-10-27）

| 项 | 内容 |
|----|------|
| **现象** | `tools/engines.sh stop embedgemma2` 报「引擎 embedgemma2 未在运行」，但 8083 端口仍在监听、进程 49371 健在（`llama-server-eg2`，启动于 10-23 18:16）。 |
| **根因** | 两套管理器各写各的 PID 文件：`manage.sh start-aux` 写 `logs/backend-<name>.pid`，`tools/engines.sh` 写 `.engines/<name>.pid`。embedgemma2 由 `manage.sh` 自动拉起（`CONFIG_AUTOSTART_AUX`），PID 只落在 `logs/` 下；`engines.sh` 只认 `.engines/` → 读不到 PID → 判定「未运行」且**不执行停止**。`.engines/` 目录实测为空。 |
| **影响** | 运维误导（以为引擎已停，实际仍在跑，占 CPU/内存）；`engines.sh start` 还会因「未在运行」判定而尝试二次启动（端口冲突）。 |
| **修复（2026-10-27）** | `tools/engines.sh` 新增 `_resolve_pid()`：先查 `.engines/<name>.pid`，再回落 `logs/backend-<name>.pid`（谁活着用谁）；`_pid_owner()` 标注归属方。`stop` 停完清理两处 PID 文件；`status` 显示 `[由 manage.sh 管理]`；`start` 改为「已运行即跳过」（不再二次拉起）。`manage.sh:cmd_stop_aux` 本身逻辑正确（读同一 PID 文件），无需改动。 |
| **验收（2026-10-27）** | `tools/engines.sh status embedgemma2` → `✅ 运行中 PID=49371 :8083 [由 manage.sh 管理]`；`bash -n` 通过。 |
| **残留风险** | 两套管理入口并存仍是设计债（建议长期收敛为单一入口）；`supervise` 守护仍只认 `.engines/`。 |

### DEF-322: 监控外推失真——相邻窗差分算 ETA + 告警等级用字符串 `max()` 比较（P2） — ✅ 已修复（2026-10-27）

| 项 | 内容 |
|----|------|
| **现象 A（ETA 误报 ×2）** | 10-27 前后两次报「按当前速率 headroom 约 0.3h / 0.4h 耗尽」，均未发生（headroom 实际稳定在 17–18GB）。根因：ETA 用**相邻两次快照差分**外推 5 分钟窗，headroom 因 Splash 周期性回收呈锯齿 → 单窗斜率非单调、噪声放大。 |
| **现象 B（等级错标）** | 日志出现 `CRIT | 内存压力 warning` —— 判级用 `max(level, "WARN")`，字符串字典序下 `"CRIT" < "WARN"` 为真 → **CRIT 被 WARN 覆盖**，告警行级别与实际不符（等级取严重度更高者的语义被破坏）。 |
| **现象 C（速率荒谬）** | 改滚动窗后首轮出现「denied 增速 79710/h（0min 窗口）」——denied 是突发计数（单分钟可 +350），1–2 个样本的差分算出上万/h。 |
| **修复（2026-10-27）** | ① 新增 `tools/monitor_window.py`（纯函数层，可单测）：`slope()` 最小二乘、`update_history()` 窗口裁剪、`headroom_eta()`/`denied_rate()`（**最小跨度闸 15min，跨度不足不外推**）、`sev_max()` 严重度比较；② `tools/monitor_service.sh` 改用该模块，状态文件新增 `hist` 滚动历史（30min 窗）；③ 告警文本标注窗口跨度，摘要行增列 `eta X(Ymin 窗)`。 |
| **验收（2026-10-27）** | 新增 `test/unit/test_monitor_window.py`（13 例：斜率精确性/跨度闸/窗口裁剪/等级序/退化样本）；实跑 `once` 输出 `eta —(0.0min 窗)` 且告警级别正确（WARN 而非错标 CRIT）；监控守护已重启加载新逻辑（PID 7686）。 |
| **残留风险** | 滚动窗需 ≥15min 真实样本才有外推（守护 5min 周期 ⇒ 约 3 轮后可用）；合成/污染样本会污染窗口（本次已清空重建）。 |

### DEF-323: 重启后自启链三处断裂——launchd PATH 缺 Homebrew / 作业组连坐杀守护 / 守护单周期脆弱（P1） — ✅ 已修复（2026-10-27）

| 项 | 内容 |
|----|------|
| **现象** | 2026-10-27 05:52 系统重启后：① `logs/startup-stack.log` 报「未找到 splash 命令。安装: brew install incoai/tap/splash」，`manage.sh start` rc=1 → 引擎没被自启拉起（实测栈最终由登录后的其他会话于 05:53:56 拉起）；② 监控守护在 05:53:00 被拉起（PID 2726），但 05:54:24 验收时已死。 |
| **根因 1（PATH）** | launchd 登录作业的默认 PATH 不含 `/opt/homebrew/bin` → `manage.sh` 找不到 splash CLI（brew 安装路径）。 |
| **根因 2（连坐）** | 守护由 `startup_stack.sh` 用 `nohup … &` 拉起；**launchd 在主作业退出时连坐杀掉作业组内的全部子进程**（nohup 只挡 SIGHUP，挡不住作业组终止）→ 守护随 wrapper 退出被 SIGTERM。 |
| **根因 3（脆弱）** | 守护循环内单周期异常（服务不可达时快照字段缺失 → `report()` 内 `float(None)` 抛错）可终止整个守护循环——而守护是服务唯一的眼睛，不能因一次采集失败就消失。 |
| **修复（2026-10-27）** | ① 两个 launchd 作业拆开：`com.llama-defender.stack`（RunAtLoad、KeepAlive=false，起栈）＋ **新增 `com.llama-defender.monitor`（RunAtLoad、KeepAlive=true，托管监控守护）**；两者均注入 `EnvironmentVariables.PATH`（含 `/opt/homebrew/bin`）；② `startup_stack.sh` 显式 export PATH 并**不再**自行拉起守护（交由 launchd 托管）；③ `monitor_service.sh` 守护循环加 `set +e` 包裹 + `report/emit/append_log` 单步失败不退出（`|| snap="$snap"` / `|| true`）。 |
| **验收（2026-10-27 05:56）** | `launchctl kickstart com.llama-defender.stack` → `manage.sh start rc=0`（PATH 修复生效）；守护 PID 6947 由 launchd 托管且首轮快照 `mem 22.13G head 18.13G denied 0`；`tools/post_reboot_check.sh` **8 项全绿**（swap 0.00M / 代理 5196 healthy / 引擎 :8081 / embedgemma2 :8083 / fnf 四旗标 / br4 / 守护 / capability_audit exit 0）。 |
| **残留风险** | `KeepAlive=true` 的守护若被用户手动 kill 会被 launchd 重拉（预期）；守护日志 `logs/service-monitor.daemon.log` 与业务日志分离。 |

### DEF-324: launchd 守栈作业退出即连坐杀掉整个栈——重启后服务「起来又死」（P1） — ✅ 已修复（2026-10-28）

| 项 | 内容 |
|----|------|
| **现象** | 10-28 22:55 重启后：launchd 自启作业 `com.llama-defender.stack` **确实把栈拉起来了**（`logs/startup-stack.log`：引擎 1645 / 代理 1743 / embedgemma2 1772，22:57:08 健康检查 Done·TTFT 0.0s），但 wrapper 22:57:18 退出后，23:17 复查时 `:4000`/`:8081`/`:8083` **全部无监听**（栈已死）。23:17:06 另有进程把栈重新拉起（ppid=1）才恢复服务。 |
| **根因** | launchd 在作业主进程退出时回收**整个作业进程组**；引擎/代理/辅助引擎都是 wrapper（`tools/startup_stack.sh` → `manage.sh start`）的子进程 → wrapper 一退出即被连坐 SIGTERM。这与 DEF-323 根因②**同源**（当时受害者是监控守护，这次是整个栈）。 |
| **为什么昨天没暴露** | 昨天重启时 `manage.sh start` 因 PATH 缺 Homebrew 直接失败（rc=1），栈没被自启拉起 → 后续由登录后的其他会话手工拉起（父进程非 launchd）→ 躲过了连坐。 |
| **修复（2026-10-28）** | ① `tools/startup_stack.sh` 改为**常驻前台**：起栈后进入守栈循环（每 30s 校验 `:8081` 在听 且 代理 PID 活着），wrapper 不再退出 → 作业进程组不被回收；② 栈亡时区分「人工停机」与「异常退出」——检测到新的 `service_stop` 生命周期事件 → exit 0（不拉起，尊重人工停机），否则 exit 1；③ plist 改 `KeepAlive={"SuccessfulExit":false}`（仅异常退出时由 launchd 重启本作业）；④ `tools/post_reboot_check.sh` 增第 6b 项「守栈 wrapper 在跑」检查。 |
| **验收（2026-10-28 23:22）** | 守栈作业 PID 5063 常驻；90s 后复核：代理 4761 / 引擎 4693 / embedgemma2 4806 **俱在**（此前 wrapper 退出即死）；`post_reboot_check.sh` **9 项全绿**（swap 0.56M / 代理 healthy / 引擎 / 辅助引擎 / fnf 四旗标 / br4 / 守护 / 守栈作业 / capability_audit exit 0）。批跑已自动恢复（swef12d2，93–96K 上下文请求在飞）。 |
| **残留风险** | 守栈循环用「端口 + PID 文件」判活，若代理被单独 kill 而 PID 文件未清，会误判为栈亡 → exit 1 → launchd 重启整栈（可接受，但会打断在飞请求）；`manage.sh stop` 走 `service_stop` 事件路径，不会误触发拉起。 |

## 五、缺陷分布与统计

### 5.1 按严重度（截至 2026-09-06）

| 严重度 | 总数 | ✅ 已修复 | 🟡 部分修复 | 🔴 未修复 |
|--------|------|----------|------------|----------|
| 🔴 P0-Critical | 7 | 7 | 0 | 0 |
| 🟠 P1-High | 12 | 12 | 0 | 0 |
| 🟡 P2-Medium | 15 | 12 | 1 | 2 |
| 🔵 P3-Low | 5 | 4 | 0 | 0 |
| **合计** | **39** | **35** | **1** | **2** |（DEF-312 治理三侧已落地待量测）

### 5.1a 按修复状态汇总

| 状态 | 数量 | 占比 | 缺陷编号 |
|------|------|------|----------|
| ✅ 已修复/已验证 | 34 | 87% | DEF-001~DEF-004, DEF-007, DEF-101~DEF-102, DEF-104~DEF-109, DEF-201~DEF-205, DEF-207~DEF-210, DEF-301~DEF-305, DEF-313~DEF-320 |
| 🟡 部分修复/已缓解 | 1 | 3% | DEF-005 (OOM 缓解, 代码审计确认完整) |
| 🔴 未修复/待治理 | 2 | 6% | DEF-306 (fail-closed——两处裸int已补防, 主修复完成), DEF-312 (模型层幻觉, 治理三侧已落地待量测) |
| ⚪ 设计限制 | 2 | 6% | DEF-103 (fifo 与 compression 功能重叠), DEF-206 (A/B 实验为一次性决策工具) |

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
