# 功能缺陷清单 (Defect List)

> **生成日期**: 2026-06-06  
> **数据来源**: 
> - `logs/anthropic_proxy.log` (66 MB, 178K+ 条目)
> - `logs/llama-server.log` (8.4 MB, rapid-mlx 后端日志)
> - `logs/proxy_metrics.jsonl` (305 条结构化指标)
> - `logs/itest/` (集成测试输出)
> - `logs/e2e_test.log` (端到端测试)
> - `logs/unit_test.log` (单元测试)
> - 28 条 git commit messages
> - `TROUBLESHOOTING.md` / `BENCHMARK.md`
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

### DEF-001: 67 个请求返回 500 错误 (22% 错误率) — 🟡 部分修复

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
| **剩余工作** | 1) 生产环境验证 500 错误率是否降至 < 2%<br>2) 根因仍可能是 `_handle_messages` 内部逻辑错误,预截断仅为缓解措施 |

### DEF-002: 循环注入率 37% — 模型仍频繁陷入循环 — 🟡 部分修复

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
| **数据源** | `TROUBLESHOOTING.md` § 二.根本原因 |
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

### DEF-103: Cleared Compression 触发率低 (代理层收益打折) — ✅ 设计限制 (已记录)

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **现状** | `Cleared compression` 日志条目较少,大多数情况下仅依赖 Rounds/FIFO 截断 |
| **PRD 文档** | `optimization-log-20260603.md` 称 `compress_cleared_tool_results()` 每轮合并 1-21 个 cycles,节省 2-42 条消息 |
| **实际效果** | 在当前 fifo 策略下,cleared messages 已被截断,二次压缩空间有限 |
| **影响** | Layer 4 的 `compress_cleared_tool_results` 价值降低 |
| **结论** | 设计限制: fifo 策略已截断大部分 cleared messages, compression 在 fifo 模式下收益有限。保留作为 rounds 策略的补充优化 |

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

### DEF-106: max_tokens 在 rapid-mlx 后端被忽略 — 🟡 部分修复

| 项 | 内容 |
|------|------|
| **已修复** | 非流式路径新增 JSON 修复: `force_stopped` 时回溯 OpenAI 原始 `tool_calls` 参数,对截断 JSON 调用 `_repair_truncated_json()` 修复后重新解析 |
| **已增强** | `_repair_truncated_json` 改用 `bracket_stack` 跟踪开闭括号类型,正确处理 `[]` 截断 (之前只会 `}}`) |
| **遗留** | 根因是 rapid-mlx 忽略 max_tokens (v0.6.30 bug), 需升级到 v0.6.71 |

### DEF-107: high_drop_ratio 21.6% — 上下文丢失率过高 — 🟡 已缓解

| 项 | 内容 |
|------|------|
| **数据源** | `logs/proxy_metrics.jsonl` quality_flags 统计 |
| **指标** | `high_drop_ratio: 66/305 = 21.6%` |
| **触发条件** | `dropped / (dropped + kept) > 0.7` |
| **影响** | 21.6% 的请求丢失超过 70% 的历史消息,模型"失忆"风险高 |
| **根本原因** | 1) fifo 策略在长会话中保留窗口固定 (40 条),但消息数线性增长<br>2) 截断触发过于频繁,缺少动态调整 |
| **已缓解** | 当 drop ratio > 85% 时,注入 `[System: Context severely truncated]` 用户消息,提示模型使用 /compact 或新建会话 |
| **已切换** | `PROXY_CTX_TRUNCATE_STRATEGY=rounds` (生产配置 `rapid-mlx-35b.conf`) — token budget 动态管理替代固定 fifo 窗口 |
| **剩余工作** | 1) 截断时注入压缩摘要替代简单截断<br>2) 验证 rounds 策略下 prefill 延迟是否下降 |

### DEF-108: Blocker Tracker 未真正触发 — ✅ 已修复

| 项 | 内容 |
|------|------|
| **根因** | Pipeline 顺序错误: `clear_old_tool_results` 在 `_detect_blocker_pattern` 之前运行,清除 tool_result 内容时覆盖了错误标记 (`wasted`/`file_not_found`/`input_validation`) |
| **修复** | 将 blocker detection 移到 tool-result clearing 之前。现在 pipeline 顺序: 1) error translation 2) **blocker detection** 3) tool-result clearing |

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
| **评估** | A/B 实验为一次性决策工具,非持续集成项。实验结论 (B 组更优) 已记录,配置调优属于运维决策而非代码缺陷 |
| **状态** | 标记为设计限制,不需要代码修复 |

### DEF-207: rapid-mlx 性能衰减需手动重启 — 🟡 已缓解

| 项 | 内容 |
|------|------|
| **数据源** | `optimization-log-20260603.md` § 4.2 + BENCHMARK.md |
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

---

## 五、缺陷分布与统计

### 5.1 按严重度（截至 2026-06-08）

| 严重度 | 总数 | ✅ 已修复 | 🟡 部分修复 | 🔴 未修复 |
|--------|------|----------|------------|----------|
| 🔴 P0-Critical | 7 | 3 | 4 | 0 |
| 🟠 P1-High | 8 | 4 | 1 | 3 |
| 🟡 P2-Medium | 10 | 5 | 5 | 0 |
| 🔵 P3-Low | 5 | 3 | 2 | 0 |
| **合计** | **30** | **15** | **12** | **3** |

### 5.1a 按修复状态汇总

| 状态 | 数量 | 占比 | 缺陷编号 |
|------|------|------|----------|
| ✅ 已修复/已验证 | 15 | 50% | DEF-003, DEF-004, DEF-007, DEF-101, DEF-102, DEF-105, DEF-108, DEF-201, DEF-202, DEF-204, DEF-205, DEF-301, DEF-302, DEF-305 |
| 🟡 部分修复/已缓解 | 12 | 40% | DEF-001, DEF-002, DEF-005, DEF-006, DEF-104, DEF-106, DEF-107, DEF-203, DEF-207, DEF-208, DEF-209, DEF-210, DEF-303, DEF-304 |
| 🔴 未修复 | 2 | 7% | DEF-103, DEF-303/304 剩余部分 |
| ⚪ 设计限制 | 2 | 7% | DEF-103, DEF-206 |

### 5.2 按类别

| 类别 | 缺陷编号 | 数量 | 已修复 |
|------|----------|------|--------|
| **监控/可观测性失效** | DEF-003, 201, 203 | 3 | 1 |
| **生产环境未覆盖** | DEF-001, 002, 005, 101, 107, 108 | 6 | 3 |
| **配置/部署缺陷** | DEF-007, 102, 104, 106, 207, 305 | 6 | 3 |
| **架构设计缺陷** | DEF-004, 105, 202, 203, 205 | 5 | 2 |
| **测试覆盖不足** | DEF-208, 209 | 2 | 0 |
| **代码质量/文档** | DEF-006, 210, 301, 302, 303, 304 | 6 | 1 |
| **未实施需求 (U1-U7)** | DEF-002 (部分) | 1 | 0 |

### 5.3 按系统层

| 系统层 | 缺陷 | 已修复 |
|--------|------|--------|
| 代理层 (Python) | DEF-001, 003, 004, 102, 103, 107, 201, 202, 203, 204, 205, 208, 301, 302, 303 | 6 |
| 后端 (rapid-mlx) | DEF-005, 006, 106, 207 | 1 |
| 模板/配置 | DEF-007, 104, 305 | 2 |
| 测试基础设施 | DEF-105, 208, 209 | 0 |
| 文档 | DEF-210 | 0 |

---

## 六、根因分析与修复优先级

### 6.1 已完成修复（2026-06-06 ~ 2026-06-08）

| 缺陷 | 修复内容 | Commit |
|------|----------|--------|
| DEF-001 | BrokenPipe→499 client_closed + 503/504 错误分类 + Retry-After + 预截断 | 5cecef6, 9758a6c, 86934ae |
| DEF-002 | 移除 LOOP_CONSECUTIVE 双重计数 + 新增 Level 3 + Level 2 多工具移除 + 跨请求持久化 | f6a222b |
| DEF-003 | re_read_rate 公式修正 + pipeline.re_read 指标 + 5 个单测 | 5cecef6 |
| DEF-004 | 验证非 bug，增强 observability（recent_tools 名称列表 + scanned_assistant） | f419e6b |
| DEF-005 | PROXY_OOM_SAFE_TOKENS=60000 + pipeline 后二次 token 检查 + system prompt 估算 | f277e10, 3a517ea |
| DEF-006 | manage.sh GPU sanity check（>0.85 拒绝启动，>0.80 警告） | f277e10 |
| DEF-007 | `manage.sh fix-template <dir>` 一键修复 + 启动时自动检测 | f277e10, 3a517ea |
| DEF-101 | BrokenPipe/ConnectionResetError → 499 (client_closed) | f277e10 |
| DEF-102 | fifo 为有意配置（利于 prefix cache 稳定） | f277e10 |
| DEF-106 | 非流式路径 JSON 修复（force_stopped 时回溯 repair） | f277e10 |
| DEF-108 | Pipeline 顺序修正：blocker detection 移到 clearing 之前 | f277e10 |
| DEF-202 | Bash dedup 跳过 `[cleared:...]` 内容 | f277e10 |

### 6.2 剩余修复路径

#### P0 遗留验证

| 缺陷 | 状态 | 待验证 |
|------|------|--------|
| DEF-001 | 🟡 部分修复 | 生产环境 500 错误率是否 < 2% |
| DEF-002 | 🟡 部分修复 | loop_injected 率是否降至 < 20%；Write 内容相似度检测未实施 |
| DEF-005 | 🟡 已缓解 | 大请求 OOM 是否减少；PROXY_OOM_SAFE_TOKENS 有效性 |
| DEF-006 | 🟡 已缓解 | 升级到 rapid-mlx v0.6.71 彻底消除警告 |

#### P1/P2 待修复

| 顺序 | 缺陷 | 预估工作量 |
|------|------|-----------|
| 1 | DEF-107: high_drop_ratio 21.6% 干预 | 1-2 天 |
| 2 | DEF-104: TOOL_ALWAYS_KEEP 白名单自动扩展 | 2-3 天 |
| 3 | DEF-105: 集成测试空 session_id | 0.5 天 |
| 4 | DEF-103: Cleared Compression 触发率低评估 | 0.5 天 |
| 5 | DEF-201: re-read 假阳性统计粒度 | 1 天 |
| 6 | DEF-203: prefix cache 断裂优化 | 2-3 天 |
| 7 | DEF-205: 双重 POST 代理层去重 | 1-2 天 |
| 8 | DEF-208/209: 测试覆盖提升至 ≥200 case | 3-5 天 |
| 9 | DEF-301~305: P3 代码质量项 | 2-3 天 |

### 6.3 长期改进 (P2/P3)

- **架构层**: 拆分 `_handle_messages()` (建议已存在 9b),引入 Pipeline 类
- **工具链**: 添加 `proxy_inspector.py` 通用日志分析工具
- **监控**: 状态页添加实时趋势图
- **生态**: 跨会话记忆 (U1),阶段感知压缩 (U2),多模型协同 (U6)

---

## 七、回归测试建议

修复 P0/P1 后,必须重跑:

```bash
# 单元测试 (44 tests, 应保持全部通过)
python3 test/unit/test_proxy_fallback.py

# 集成测试 (含 blocker 矩阵)
bash test/integration/test_blocker_integration.sh

# 端到端测试 (需要后端运行)
bash test/e2e/e2e_tools_fallback.sh
python3 test/e2e/test_proxy_integration.py

# 完整三件套
bash test/run_tests.sh --all
```

**重点验证项**:
1. ✅ DEF-003: re_read_rate ≤ 100%（已验证通过单测）
2. ✅ DEF-004: recent_tools 名称列表出现在 metrics/filter stats（已验证）
3. ✅ DEF-108: 集成测试 Blocker 矩阵 7/7 通过（已验证）
4. 🟡 DEF-001: 500 错误率是否 < 2%（需生产环境验证）
5. 🟡 DEF-002: loop_injected 率是否降至 < 20%（需生产环境验证）
6. 🟡 DEF-005: OOM 事件是否减少（需生产环境验证）
7. 🟡 DEF-106: 非流式 force_stopped JSON 修复是否生效（需端到端验证）

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

> **清单版本**: v1.0  
> **生成工具**: 手工整理 + 日志 + Metrics 解析  
> **下次更新**: P0 修复完成后 (预计 1-2 周)  
> **维护建议**: 每月基于 logs/proxy_metrics.jsonl 重新审计一次
