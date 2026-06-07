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
| **已实施修复** | **Part A** (commit 9758a6c): `PROXY_PRE_TRUNCATE_CHARS=400000` 预截断 + `do_POST` try/except 兜底<br>**Part B**: `_respond_json` 替换 `raise`,返回结构化 JSON 500<br>**Part C**: `_classify_exception()` 错误分类 (OOM→503, timeout→504, programming→500) + `Retry-After` header + `retryable` 字段<br>**测试**: 10 个新单元测试 (`TestClassifyException`) + 3 个预截断测试 (`TestDef001PreTruncation`) |
| **剩余工作** | 1) 生产环境验证 500 错误率是否降至 < 2%<br>2) 根因仍可能是 `_handle_messages` 内部逻辑错误,预截断仅为缓解措施 |

### DEF-002: 循环注入率 37% — 模型仍频繁陷入循环

| 项 | 内容 |
|------|------|
| **数据源** | `logs/proxy_metrics.jsonl` quality_flags 统计 |
| **指标** | `loop_injected: 113/305 = 37.0%` |
| **同期 high_drop_ratio** | 66/305 = 21.6% |
| **关联 commit** | `4e4e3e6` (模式循环检测)、`4bdf309` (阻塞检测) |
| **现象** | 37% 的请求触发了循环注入。`logs/anthropic_proxy.log` 中 `LOOP LEVEL 3: Bash called 9 times` 反复出现,且**同一 session 内连续触发 5+ 次** |
| **未解决场景** | 1) **跨请求循环**: `LOOP LEVEL 3` 注入后,下一轮模型又回到相同循环 (16:02 → 16:06 → 16:09 → 16:12 持续 4 轮)<br>2) **认知驱动循环**: Write 工具循环 (16:02-16:23) 持续 21 分钟,Level 3 仅当次有效,跨请求失效 |
| **修复建议** | 1) 实现跨请求循环追踪 (累计 max_run 趋势)<br>2) 实现 Write 内容相似度检测 (Jaccard > 0.8 视为重复)<br>3) 文档已知 (`docs/claude-behavior-semantic-analysis-v2.md` §7.6),但未实施 |

### DEF-003: re_read_rate 计算公式错误 (2862%, 3271%) — ✅ 已修复

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **异常样本** | `Re-read after clear: 229 reads target 6 cleared files (re_read_rate=2862%)` |
| **正常范围** | 0%-100% (公式应为 `re_read_files / cleared_files * 100`) |
| **根因** | 旧代码用 `total_reads / cleared_files` (229/8=28.6=2862%),分子语义错误 |
| **修复** | 新公式: `rate = re_read_files / cleared_files * 100`, cap 100%。<br>新增 `pipeline.re_read` 指标到 metrics JSONL, 含 count/cleared_files/re_read_files/rate_pct。<br>5 个单元测试 (`TestReReadRate`) |
| **验证** | `rate_pct` 范围 [0, 100],旧公式下 229/8=2862% 在新公式下为 8/8=100% |

### DEF-004: Tool 过滤 "recent" 扫描 99% 失效

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **统计** | 99 条 `Tool filter: 44 -> 12 (always=12, recent=0)` / 130 总条数 = **76% recent=0** |
| **预期行为** | "recent" 应扫描最近 N=5 轮 assistant tool_use,收集实际用过的工具 |
| **实际行为** | 永远 0 — 工具过滤仅依赖 TOOL_ALWAYS_KEEP 白名单 (12 个),完全忽略"最近使用"维度 |
| **影响** | R3.3 工具过滤只起到白名单作用,失去"按需扩展"能力。当用户调用非白名单工具 (如 `Skill`) 时,工具被剔除 → 模型调用失败 |
| **根因** | 可能 `_filter_tools()` 扫描 assistant 消息时,role 字段名不匹配 (Anthropic 用 `assistant`,转换后可能丢失) |
| **修复建议** | 1) 在 `_filter_tools()` 中添加调试日志,记录 `recent_tools` 集合<br>2) 检查消息转换后 `role="assistant"` 是否完整保留<br>3) 单元测试: 模拟最近 5 轮使用 `Skill` 工具,验证是否在过滤后保留 |

### DEF-005: 后端 Metal OOM 仍会发生 (未根除)

| 项 | 内容 |
|------|------|
| **数据源** | `logs/llama-server.log` |
| **错误模式** | `ERROR:vllm_mlx.scheduler:Error in batch generation step: [metal::malloc] Resource limit (499000) exceeded.` |
| **出现频次** | 至少 3 次明确 OOM 事件 |
| **现象** | rapid-mlx 自动 `generation_error_recovery` 恢复,但**当前请求被中止** (running=1 的请求也被 abort) |
| **已尝试的缓解** | commit `d8b650d` (降低 GPU 内存限制)、`PROXY_MAX_CONCURRENT=1` |
| **遗留** | 即便 PROXY_MAX_CONCURRENT=1,单请求 prefill 仍可能 peak 超 20GB 触发 OOM (`allocation_limit` 是软目标) |
| **影响** | 大请求 (60K+ tokens) 偶发崩溃,代理侧表现为 500 错误 |
| **修复建议** | 1) 在代理层预估 prompt_tokens,>50K 时主动分片或降级到 rounds 截断更激进<br>2) 监控 `cache_mem` 接近 `cache-memory-mb` 上限时主动清理<br>3) 添加 `disconnect_guard` 行为: 检测到 OOM 时主动断开后端并通知用户 |

### DEF-006: Apple Silicon Kernel Panic 风险

| 项 | 内容 |
|------|------|
| **数据源** | `logs/llama-server.log` 启动警告 |
| **警告原文** | `Continuing may trigger a macOS kernel panic (see issue #324). Apple Silicon firmware can panic the whole system rather than...` |
| **出现频次** | 至少 6 次启动警告 |
| **影响** | 一旦触发,系统整体崩溃 (不仅仅是进程),数据可能丢失 |
| **触发条件** | `--gpu-memory-utilization` 设置过高 (>0.85 触发警告) |
| **当前配置** | 35B: 0.75, 9B: 0.50 — 在安全范围,但启动警告仍出现 (rapid-mlx v0.6.30 总是显示) |
| **修复建议** | 1) 验证当前值是否低于阈值 (推荐 < 0.80)<br>2) `manage.sh` 添加启动前 sanity check<br>3) 升级 rapid-mlx 到 v0.6.71 (修复 MoE non-trimmable + 多个稳定性) |

### DEF-007: Backend Chat Template 不修复会再次崩溃 (隐性 P0)

| 项 | 内容 |
|------|------|
| **数据源** | `TROUBLESHOOTING.md` § 二.根本原因 |
| **错误模式** | `jinja2.exceptions.TemplateError: System message must be at the beginning.` |
| **触发条件** | Claude Code 启用 `mid-conversation-system-2026-04-07` beta,在对话中段注入 system 消息 |
| **已修复** | commit `e221965` (Qwen chat template 兼容性) + 替换 5 个模型的 chat_template.jinja |
| **遗留风险** | 1) 新下载的模型 (`ollama pull`, `huggingface-cli download` 等) 默认带原始 Qwen template,会再次崩溃<br>2) 修复覆盖仅 5 个模型 (Qwen3.6-35B-A3B / 27B-AEON × 2 / Qwen3.5-27B / Qwen3-Coder-30B),其他模型未覆盖 |
| **修复建议** | 1) `manage.sh` 启动前检查模型目录的 chat_template 是否需要修复<br>2) 提供 `manage.sh fix-template <model_dir>` 一键修复命令<br>3) README 警告新模型需先运行修复 |

---

## 二、🟠 P1-High (8 项)

### DEF-101: Broken Pipe 错误 (65 次)

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **错误模式** | `ERROR: [Errno 32] Broken pipe` |
| **出现频次** | 65 次 |
| **原因** | 客户端 (Claude Code) 在代理完成前断开连接 (超时或主动取消) |
| **当前处理** | 静默捕获 (commit 隐含修复) |
| **遗留** | 1) 仍有 65 条未处理 (日志保留但可能误导)<br>2) 客户端重试机制可能放大 (同一请求重发 2 次) |
| **修复建议** | 1) 区分"客户端取消" vs "服务端异常",后者应触发 retry 计数<br>2) 客户端应禁用主动取消,改用更长 timeout |

### DEF-102: 截断策略降级为 fifo (与文档不符)

| 项 | 内容 |
|------|------|
| **数据源** | `logs/proxy_metrics.jsonl` truncate 统计 |
| **统计数据** | Truncations=134, 100% 走 `fifo` 策略 |
| **PRD 文档** | `proxy-context-window-design.md` 称 `rounds` 是主策略,`fifo` 是备选 |
| **实际现状** | `rounds` 策略未生效,仅 `fifo` 在运行 |
| **可能原因** | 1) 配置文件未启用 `PROXY_CTX_TRUNCATE_STRATEGY=rounds`<br>2) 实际配置从 `rounds` 降级为 `fifo` 但未记录原因 |
| **影响** | 失去自适应保留轮数、增量压缩、关键词索引等 `rounds` 专属优化 |
| **修复建议** | 1) 检查 configs/*.conf 中实际配置值<br>2) 若有意降级需记录 commit,否则回退到 rounds |

### DEF-103: Cleared Compression 触发率低 (代理层收益打折)

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **现状** | `Cleared compression` 日志条目较少,大多数情况下仅依赖 Rounds/FIFO 截断 |
| **PRD 文档** | `optimization-log-20260603.md` 称 `compress_cleared_tool_results()` 每轮合并 1-21 个 cycles,节省 2-42 条消息 |
| **实际效果** | 在当前 fifo 策略下,cleared messages 已被截断,二次压缩空间有限 |
| **影响** | Layer 4 的 `compress_cleared_tool_results` 价值降低 |
| **修复建议** | 1) 重新评估 cleared compression 的触发条件<br>2) 文档同步更新,避免误判 |

### DEF-104: TOOL_ALWAYS_KEEP 持续扩展表明白名单设计缺陷

| 项 | 内容 |
|------|------|
| **数据源** | git commit `8ce382e` "extend TOOL_ALWAYS_KEEP with newer Claude Code tools" |
| **现象** | Claude Code 升级后,新工具未在白名单 → 被过滤 → 调用失败 → 用户报告 → 添加白名单 |
| **影响** | 1) 每次 Claude Code 更新都可能引入新工具失败<br>2) 修复方式为被动扩展,无主动发现机制 |
| **修复建议** | 1) 在状态页添加"被过滤但被调用的工具"统计<br>2) 自动将"最近 N 轮高频使用但不在白名单"的工具临时加入 |

### DEF-105: 集成测试空 session_id 失败

| 项 | 内容 |
|------|------|
| **数据源** | `logs/proxy_metrics.jsonl` 末尾 5 条 500 错误 |
| **模式** | `session_id=''`, `input_chars=42-49` (极小请求) |
| **时间** | 2026-06-06 09:24-09:50 集中出现 |
| **可能原因** | 集成测试 (`test/integration/`) 中部分用例未正确传递 `X-Claude-Code-Session-Id` header |
| **影响** | 集成测试可能存在 false-negative,某些场景未真正覆盖 |
| **修复建议** | 1) 集成测试套件添加 session_id 必填校验<br>2) 空 session_id 时使用 fallback (如 `req_<timestamp>`)<br>3) 现有 19/19 pass 可能不反映真实问题 |

### DEF-106: max_tokens 在 rapid-mlx 后端被忽略

| 项 | 内容 |
|------|------|
| **数据源** | `BENCHMARK.md` § 推荐配置 + `message-analysis-20260602.md` § 8.2 |
| **现状** | Rapid-MLX 0.6.30 已知 bug,完全忽略 `max_tokens`,可能输出 64K tokens |
| **代理层缓解** | `PROXY_OUTPUT_TOKEN_LIMIT_RATIO=2.0` + FORCE_STOPPED |
| **遗留** | 1) 文本被截断时产生 `[Output truncated]` 标记,但工具调用的 input JSON 也会被截断,需 `_repair_truncated_json()` 修复 (line 284)<br>2) JSON 修复仅在流式场景下生效,非流式场景修复不完整 |
| **修复建议** | 1) 升级到 rapid-mlx 0.6.71 (修复 max_tokens 行为)<br>2) 非流式场景添加同样的 JSON 修复 |

### DEF-107: high_drop_ratio 21.6% — 上下文丢失率过高

| 项 | 内容 |
|------|------|
| **数据源** | `logs/proxy_metrics.jsonl` quality_flags 统计 |
| **指标** | `high_drop_ratio: 66/305 = 21.6%` |
| **触发条件** | `dropped / (dropped + kept) > 0.7` |
| **影响** | 21.6% 的请求丢失超过 70% 的历史消息,模型"失忆"风险高 |
| **根本原因** | 1) fifo 策略在长会话中保留窗口固定 (40 条),但消息数线性增长<br>2) 截断触发过于频繁,缺少动态调整 |
| **修复建议** | 1) 改回 rounds 策略 (DEF-102)<br>2) 触发 high_drop_ratio 时主动注入压缩摘要,而非简单截断<br>3) 当 ratio > 0.9 时,主动通知用户"会话过长,建议 /compact" |

### DEF-108: Blocker Tracker 未真正触发 (`Blocker detected: 0`)

| 项 | 内容 |
|------|------|
| **数据源** | `grep "Blocker detected" logs/anthropic_proxy.log \| wc -l` = **0** |
| **现状** | 1) `Blocker tracker: enabled` 在启动时出现 10+ 次<br>2) 但 178K+ 日志行中**从未出现** `Blocker detected` 注入事件 |
| **可能原因** | 1) 触发条件太严 (PROXY_BLOCKER_THRESHOLD=2 看似简单,可能要求更复杂的错误模式)<br>2) 日志关键字不匹配 (实际打的是 `BLOCKER message injected` 而非 `Blocker detected`) |
| **影响** | R2.4 阻塞模式检测的"生产验证"缺失,集成测试 (`test_blocker_integration.sh`) 通过但生产日志未见真实触发 |
| **修复建议** | 1) 验证日志关键字名一致性<br>2) 主动注入模拟 blocker 场景到生产环境做 sanity check<br>3) 如果确实未触发,需调低阈值 (THRESHOLD=1) 或扩大错误识别范围 |

---

## 三、🟡 P2-Medium (10 项)

### DEF-201: 集成测试中 session=a309b181 出现高频 re-read 假阳性

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **现象** | `Re-read after clear: 229 reads target 6 cleared files (re_read_rate=2862%)` 持续数十次 |
| **分析** | 每次 re-read 数量 (229) 与 cleared files (6) 比例完全相同 → 表明这是**长会话累积值**而非本次请求值,统计粒度有误 |
| **影响** | 监控告警无意义,可能掩盖真实问题 |
| **修复建议** | re-read 检测应仅统计"本次请求新增的 Read 数",而非整个 session 累积 |

### DEF-202: Bash dedup 反复触发相同合并

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **现象** | `Bash dedup: 3 similar results merged, 858 chars saved` 在 09:14-09:15 之间触发 7 次,内容完全相同 |
| **可能原因** | 1) 每次请求都重新扫描整个历史<br>2) 同样的 Bash 输出被反复"去重" (但已经清空,无需去重) |
| **影响** | 1) 浪费 CPU 时间<br>2) 日志噪音 |
| **修复建议** | 1) dedup 应跳过已被清空为 `[cleared: ...]` 的内容<br>2) 单元测试: 验证连续 dedup 不会无限触发 |

### DEF-203: 工具过滤后 prefix cache 断裂 (cache 收益打折)

| 项 | 内容 |
|------|------|
| **数据源** | `docs/prompt-instability-mechanism-analysis.md` |
| **现象** | 44 → 12 tools 后,前缀哈希变化,prefix cache miss |
| **当前数据** | `logs/proxy_metrics.jsonl` 中 `Tool filter: 44 -> 12` 130 次 / 305 = **42.6%** 的请求走过滤 |
| **影响** | 42.6% 的请求即使 rounds 稳定,prefix cache 仍 miss (因为工具列表变了) |
| **修复建议** | 1) 缓存过滤后的工具列表 (按"最近使用工具"分组)<br>2) 只过滤不变的尾部,保留稳定的 tools schema 前缀 |

### DEF-204: 状态页 `/status` 被高频轮询

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` 中 `GET /status` 条目 |
| **现象** | 浏览器每 5 秒轮询一次状态页,产生大量日志噪音 |
| **数据支撑** | `message-analysis-20260604.md` §5 列为"🟢 低"问题 |
| **修复建议** | 1) `/status` 不写入 `proxy_requests.jsonl` (只写 `proxy_metrics.jsonl`)<br>2) 状态页添加 ETag/Last-Modified 减少 304 响应 |

### DEF-205: 双重 POST (Claude Code 客户端行为)

| 项 | 内容 |
|------|------|
| **数据源** | `message-analysis-20260602.md` §10.3 |
| **现象** | 同一秒内出现 2 个完全相同的请求 (大小、工具数均相同) |
| **影响** | 后端被迫双倍处理,GPU 资源浪费 |
| **代理层处理** | 无 — 文档明确"客户端行为,代理层无法修复" |
| **修复建议** | 1) 在代理层做请求去重 (基于 body hash + 时间窗口 1s)<br>2) 减少并发压力测试影响 |

### DEF-206: A/B 实验数据未用于实际调参

| 项 | 内容 |
|------|------|
| **数据源** | `docs/ab-experiment-design.md` §8 |
| **现状** | T1 任务 (A 组 94 请求 / B 组 79 请求) 已完成,但**没有后续 follow-up 实验** |
| **影响** | 1) 投入产出比低<br>2) 实验结论 (B 组更优) 未指导实际配置选择 |
| **修复建议** | 1) 完成实验 follow-up: 用 B 组配置 (clearing 关闭) 在生产中跑 1 周,对比实际效果<br>2) 至少 3 次重复实验以获得统计显著性 |

### DEF-207: rapid-mlx 性能衰减需手动重启

| 项 | 内容 |
|------|------|
| **数据源** | `optimization-log-20260603.md` § 4.2 + BENCHMARK.md |
| **现象** | 运行 7 分钟后生成速度从 56 → 12 tok/s (衰减 78%) |
| **当前状态** | 无自动重启机制,需用户手动 `./manage.sh restart` |
| **修复建议** | 1) 添加 watchdog: 检测生成速度 < 20 tok/s 持续 5 分钟时自动重启<br>2) 集成到 `manage.sh` |

### DEF-208: 单元测试覆盖不足 (44 个 case)

| 项 | 内容 |
|------|------|
| **数据源** | `logs/unit_test.log` |
| **现状** | 44 tests, 全部通过, 0.003s |
| **缺失覆盖** | 1) 截断策略 (rounds/fifo/char) 各场景<br>2) 三级压缩链 (LLM/Rules/Static) 各场景<br>3) 循环检测 (Level 1/2/3) 边界条件<br>4) 工具过滤的"最近使用"扫描<br>5) 大 payload (>500K chars) 不抛异常<br>6) 跨请求循环追踪 (R2.1 文档要求) |
| **修复建议** | 单元测试目标 ≥ 200 case,覆盖率 > 80% |

### DEF-209: 集成测试 5 个场景未实际覆盖

| 项 | 内容 |
|------|------|
| **数据源** | `test/integration/test_blocker_integration.sh` |
| **覆盖情况** | 1) 2× file_not_found (Read) ✅<br>2) 2× Wasted call (Read) ✅<br>3) 2× InputValidationError (Bash) ✅<br>4) 3× file_not_found (Read) ✅<br>5) 1× file_not_found (no trigger) ✅<br>6) mixed types (no trigger) ✅<br>7) 2 errors → 1 success → 1 error (no trigger) ✅ |
| **缺失** | 1) 写操作认知循环 (Write 内容相似度 99% 重复)<br>2) Level 2 移除工具后用 Bash 重新循环<br>3) 跨请求循环追踪 |
| **修复建议** | 扩展集成测试矩阵覆盖认知循环和跨请求场景 |

### DEF-210: 文档与代码脱节

| 项 | 内容 |
|------|------|
| **数据源** | 多文档交叉检查 |
| **具体脱节** | 1) `proxy-context-window-design.md` 称 `rounds` 为主策略,但生产跑的是 `fifo` (DEF-102)<br>2) `system-requirements-analysis.md` 100% 覆盖率,实际 re-read 监控失效 (DEF-003)<br>3) `AGENTS.md` 列出 25+ 参数,但 docs 中未给出推荐组合<br>4) `PRD-anthropic-proxy.md` v3.0 中"Phase 5 测试"声称三层测试体系完成,但 unit test 仅 44 case,实际覆盖率不足 (DEF-208) |
| **修复建议** | 1) 文档与代码双重维护机制<br>2) CI 中添加文档/代码一致性检查 |

---

## 四、🔵 P3-Low (5 项)

### DEF-301: TODO/FIXME 标记缺失

| 项 | 内容 |
|------|------|
| **数据源** | `grep "TODO\|FIXME" anthropic_proxy.py` |
| **结果** | 仅 1 处 (line 919,关键词扫描用,非真实 TODO) |
| **影响** | 已知的"未实现"功能 (U1-U7) 无代码内标记,新开发者难以快速识别 |
| **修复建议** | 在 U1-U7 位置添加 `# TODO(roadmap): ...` 注释 |

### DEF-302: API Key 在云模式可能误显示

| 项 | 内容 |
|------|------|
| **数据源** | `AGENTS.md` § 安全注意事项 |
| **现状** | 状态页显示 `API Key: sk-xxxx****` (前 8 位 + 掩码) |
| **遗留** | 1) 日志中可能完整打印 API Key (`log_request` 调试信息)<br>2) 错误堆栈中可能包含 Authorization header |
| **修复建议** | 1) 在所有日志输出前过滤 `Bearer sk-...` 模式<br>2) 错误堆栈中脱敏 |

### DEF-303: 日志格式不一致

| 项 | 内容 |
|------|------|
| **数据源** | `logs/anthropic_proxy.log` |
| **现象** | 日志条目包含: `GET /v1/models` / `Headers: {...}` / `Body: {...}` / `<- Response body: {...}` 多种格式混合 |
| **影响** | 难以统一解析,影响 `analyze_experiment.py` 等分析工具的准确性 |
| **修复建议** | 1) 统一日志格式 (JSON Lines)<br>2) 添加日志 schema 版本号 |

### DEF-304: 缺少可观测性仪表板

| 项 | 内容 |
|------|------|
| **现状** | 只有 `/status` HTML 页面,无历史趋势图 |
| **缺失** | 1) TTFT 趋势 (按小时/天)<br>2) 循环触发率 (loop_injected 比例)<br>3) 截断频率 (truncation/小时)<br>4) 工具使用分布 (TOP 10)<br>5) 会话大小分布 (P50/P95/P99) |
| **修复建议** | 1) 在 `/status` 页面添加迷你趋势图 (Chart.js)<br>2) 集成 Grafana / Prometheus (如增加 prometheus_client 依赖,违反 zero-dep 原则) |

### DEF-305: `manage.sh` start-cloud 启动日志缺少健康检查

| 项 | 内容 |
|------|------|
| **数据源** | `DEEPSEEK-AB-EXPERIMENT-GUIDE.md` § 3.2 |
| **现象** | `start-cloud` 启动后未自动验证 `https://api.deepseek.com/v1/models` 可达性 |
| **影响** | 用户需手动 `curl` 验证,出错时排查路径长 |
| **修复建议** | 在 `start-cloud` 末尾添加 health check 步骤 |

---

## 五、缺陷分布与统计

### 5.1 按严重度

| 严重度 | 数量 | 占比 |
|--------|------|------|
| 🔴 P0-Critical | 7 | 23% |
| 🟠 P1-High | 8 | 27% |
| 🟡 P2-Medium | 10 | 33% |
| 🔵 P3-Low | 5 | 17% |
| **合计** | **30** | **100%** |

### 5.2 按类别

| 类别 | 缺陷编号 | 数量 |
|------|----------|------|
| **监控/可观测性失效** | DEF-003, 201, 203 | 3 |
| **生产环境未覆盖** | DEF-001, 002, 005, 101, 107, 108 | 6 |
| **配置/部署缺陷** | DEF-007, 102, 104, 106, 207, 305 | 6 |
| **架构设计缺陷** | DEF-004, 105, 202, 203, 205 | 5 |
| **测试覆盖不足** | DEF-208, 209 | 2 |
| **代码质量/文档** | DEF-006, 210, 301, 302, 303, 304 | 6 |
| **未实施需求 (U1-U7)** | DEF-002 (部分) | 1 |

### 5.3 按系统层

| 系统层 | 缺陷 |
|--------|------|
| 代理层 (Python) | DEF-001, 003, 004, 102, 103, 107, 201, 202, 203, 204, 205, 208, 301, 302, 303 |
| 后端 (rapid-mlx) | DEF-005, 006, 106, 207 |
| 模板/配置 | DEF-007, 104, 305 |
| 测试基础设施 | DEF-105, 208, 209 |
| 文档 | DEF-210 |

---

## 六、根因分析与修复优先级

### 6.1 紧急修复路径 (P0)

| 顺序 | 缺陷 | 预估工作量 | 依赖 |
|------|------|-----------|------|
| 1 | DEF-001: 500 错误率 22% | 2-3 天 | 无 |
| 2 | DEF-002: 循环注入率 37% | 3-5 天 | 跨请求追踪设计 |
| 3 | DEF-005: Metal OOM | 1-2 天 | DEF-007 (后端升级) |
| 4 | DEF-007: Chat template 修复工具化 | 1 天 | 资产文件 |
| 5 | DEF-006: Kernel panic 防御 | 0.5 天 | 配置审计 |

### 6.2 改进路径 (P1)

| 顺序 | 缺陷 | 预估工作量 | 关联 |
|------|------|-----------|------|
| 1 | DEF-102: 恢复 rounds 策略 | 1 天 | 配置 + 监控 |
| 2 | DEF-004: 工具过滤 recent 扫描 | 1-2 天 | 单元测试 |
| 3 | DEF-003: re_read_rate 公式 | 0.5 天 | 监控定义 |
| 4 | DEF-108: Blocker 触发验证 | 0.5 天 | 日志关键字统一 |
| 5 | DEF-104: 白名单自动扩展 | 2-3 天 | 监控数据 |
| 6 | DEF-107: high_drop_ratio 干预 | 1 天 | 行为优化 |

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
1. 修复 DEF-001 后,500 错误率 < 2%
2. 修复 DEF-003 后,re_read_rate ≤ 100%
3. 修复 DEF-004 后,最近使用的工具出现在 filtered list
4. 修复 DEF-102 后,truncation strategy 显示 `rounds`
5. 修复 DEF-108 后,生产日志中 `Blocker detected` > 0

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
