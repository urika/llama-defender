# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/) and dates use ISO 8601.

---

## [0.5.0-baseline] - 2026-06-06

### Status: Pre-OSS-migration baseline

**Tag purpose**: 标记 Phase 1-5 完整实现快照,作为 llama_defender 库化 (路线图 B) 的起点。
**不推荐生产部署**: 7 个 P0 缺陷未修复 (详见 `docs/DEFECT-LIST.md`)。

### Added

#### 核心功能 (Phase 1-5)
- **双模式代理架构** (commit `16defd9`): Local (rapid-mlx/llama-server) + Cloud (DeepSeek/OpenAI) 路由
- **8 层处理管线** (Layer 1-8): 请求入口 → 语义预处理 → 循环/阻塞检测 → 缓存优化 → 上下文截断 → 格式转换 → 响应后处理 → 可观测性
- **23 个需求点全部实现** (R1.1-R7.2, 7 大领域 100% 覆盖)
- **2 种截断策略**: rounds (token 预算 + 保留轮数) + fifo + char
- **3 级压缩链**: LLM 压缩 → 规则压缩 → 静态折叠
- **3 级循环干预** (Level 1/2/3): 软提示 → 移除工具 → 强制纯文本
- **阻塞模式检测** (`_detect_blocker_pattern`): 连续 N 次相同错误类型干预
- **工具结果语义清除**: 工具优先级评分 + Read 200 字符预览 + 最近 Read 加分
- **工具定义动态过滤** (`_filter_tools`): 白名单 + 最近使用
- **XML→JSON 4 级回退** (`parse_tool_arguments`): JSON / embedded JSON / XML / heuristic
- **Content-text 工具提取** (`_StreamingToolsExtractor`): 流式状态机
- **JSON 修复** (`_repair_truncated_json`): 截断 tool_call arguments
- **MoE 特定处理**: Qwen chat template 修复 (5 个模型已覆盖)
- **结构化 Metrics** (`logs/proxy_metrics.jsonl`): 4 种 quality_flags
- **状态页** (`GET /status`): 实时指标展示
- **78 单元测试** (Phase A + B): 覆盖 18/23 需求
- **7 个集成测试** (含 blocker 矩阵)
- **19 个端到端测试** (含 12 case 集成矩阵)

#### 设计文档 (本次提交新增)
- `docs/PRD-anthropic-proxy.md` (688 行): 产品需求文档 v3.0
- `docs/DEFECT-LIST.md` (509 行): 30 项缺陷清单 (7 P0 + 8 P1 + 10 P2 + 5 P3)
- `docs/PM-ANALYSIS-FUTURE-ROADMAP.md` (377 行): 产品经理视角的核心功能取舍
- `docs/OSS-REPLACEMENT-EVALUATION.md` (688 行): OSS 替代品深度评估
- `docs/proxy-prefix-cache-design.md` (1001 行): 代理层 prefix cache 稳定化设计 v1.0
- `docs/README.md`: 文档目录导航

#### 文档重构
- 按 6 大类重组: 01-requirements-product / 02-architecture-design / 03-experiments-testing / 04-analysis-diagnostics / 05-operations-changelog / 06-reference-metrics
- 24 篇原始文档按主题归位

### Known Issues (P0, 7 项)

来自 `docs/DEFECT-LIST.md`:
- **DEF-001**: 22% 请求返回 500 错误 — **🟡 部分修复** (预截断 + 错误分类 + Retry-After)
- **DEF-002**: 37% 请求触发循环注入 (R2.1 未根治跨请求循环)
- **DEF-003**: re_read_rate 公式错误 (2,862%, 应 ≤ 100%)
- **DEF-004**: 工具过滤 recent 扫描 99% 失效
- **DEF-005**: Metal OOM 仍偶发
- **DEF-006**: Apple Silicon Kernel Panic 风险
- **DEF-007**: Chat template 修复未工具化

### Roadmap (路向 v1.0)

依据 `docs/PM-ANALYSIS-FUTURE-ROADMAP.md` 路线图 B:
- **Phase 1 (0-3 月)**: 稳态化 + 引入旁路观测 (Langfuse)
- **Phase 2 (3-6 月)**: OSS 替换 (LiteLLM 协议转换) + 提取 llama_defender 库
- **Phase 3 (6-12 月)**: 业务转型 (llama_defender OSS 库 + DSPy/GEPA 提示词优化模板)

### Metrics (snapshot at 0.5.0-baseline)

| 指标 | 数值 |
|------|------|
| 总 commits | 33 |
| 代码行数 (`anthropic_proxy.py`) | 3,611 |
| 函数数量 | 63 |
| 配置项 | 31 env vars |
| 需求点 | 23 (100% 实现) |
| 单元测试 | 78 cases (Phase A + B) |
| 集成测试 | 7 blocker scenarios |
| 端到端测试 | 19 integration cases |
| 文档总数 | 29 篇 (含本次新增 5 篇) |

### Known Compatibility

- Python 3.9+
- macOS (Apple Silicon M5 Pro tested, 48GB unified memory)
- Backends: rapid-mlx v0.6.71+ (recommended), rapid-mlx v0.6.30 (degraded), llama-server (partial)
- Clients: Claude Code (verified), Anthropic SDK, any OpenAI-compatible client

---

## [0.5.3-text-loop-detection] - 2026-06-08

### Status: 文本输出循环检测实现

**范围**: 代理层新增对 assistant 纯文本输出的循环检测，解决模型重复输出相同文本的问题。

### Added

- **文本输出循环检测** (`_detect_text_loop`): 扫描最近 N 条 assistant 消息的纯文本内容，使用字符级 bigram Jaccard 相似度检测重复模式
- **相似度计算函数** (`_compute_text_similarity`): 基于 bigram 的 Jaccard 相似度，对文本重复敏感且计算高效
- **3 级文本循环干预**: 与工具循环共享阈值，但生成针对性的干预消息
  - Level 1: 提示停止重复
  - Level 2: 强烈警告
  - Level 3: 剥夺所有工具，强制纯文本响应

### Configuration

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_TEXT_LOOP_ENABLED` | `true` | 文本循环检测开关 |
| `PROXY_TEXT_LOOP_THRESHOLD` | `3` | 连续相似文本触发阈值 |
| `PROXY_TEXT_LOOP_MIN_CHARS` | `100` | 最小文本长度（短于此的消息不参与检测） |
| `PROXY_TEXT_LOOP_SIMILARITY` | `0.85` | 相似度阈值 (0.0-1.0) |

### Verified

- 单元测试: 11 个新测试覆盖相似度计算、循环检测、干预消息生成
- 总测试数: 182 → 193

---

## [0.5.2-truncate-fix] - 2026-06-08

### Status: 死循环根因修复完成

**范围**: 代理层报文处理逻辑重构，彻底消除 `wasted` 错误死循环。

### Root Cause Analysis

**死循环完整因果链（已验证）:**
```
Claude Read file → tool_result 存入 context
        ↓
Tool Clearing 清理旧 tool_result → [cleared: ...] 占位符
        ↓
Claude 需要综合分析 → 重新 Read 被清理文件
        ↓
后端返回 "Wasted call"（文件未变化）
        ↓
Error translation 翻译为中文提示
        ↓
Tool Clearing 清理掉翻译结果（提示丢失）
        ↓
Truncate 丢弃更多消息 → Claude "失忆"
        ↓
Loop/Blocker/HARD BLOCK 干预注入
        ↓
Truncate 丢弃干预消息 → 干预无效
        ↓
循环加剧: context 250K+ / wasted 13+ / loop 4
```

**根因**: Tool Clearing 的 `[cleared: ...]` 对 Claude 语义模糊。Claude 无法区分 "内容被清理" 和 "读取失败"，必然尝试 re-read。后端返回 "Wasted call" 后，Error translation 的提示又被 Tool Clearing 清理，形成闭环。

### Fixed

#### 致命缺陷修复
- **Tool Clearing 关闭** (`PROXY_CLEAR_ENABLED=false`): 完全停止语义清理，所有 tool_result 完整保留
- **Truncate 智能保留 Read 结果** (`_apply_rounds_truncation`): 在 rounds 策略下，从 dropped 区域提取 Read 的 tool_result 完整保留，只压缩非 Read 消息
  - 丢弃率从 **80%** 降至 **20%**
  - 文件内容不再丢失，Claude 无需 re-read
- **Error translation 高优先级保留** (`clear_old_tool_results` scoring): 翻译后的 `[System: ...]` 提示获得 `score += 10`，不被清理
- **Re-read HARD BLOCK** (`_handle_messages`): 检测到最后 assistant 消息在 Read 已清理文件时，注入 `[System: HARD BLOCK...]` user 消息强制阻断
- **`kept_names` NameError** (`_filter_tools`): 修复 `kept_names` 在 `_sort_key` 闭包中引用前未定义的问题，消除 4 次 500 错误
- **`auto_keep` 累积失效移除** (`_filter_tools`): 删除 `_tool_freq` 全局计数器（永不重置、计数对象错误），改为 `TOOL_ALWAYS_KEEP | recent_tools | tool_choice_name` 静态策略

#### Metrics 完整性修复
- 所有 pipeline 步骤统一写入 `applied: True/False` 标志，消除字段缺失

### Verified

| 指标 | 旧会话 (`Tool Clearing ON`) | 新会话 (`Tool Clearing OFF + 智能保留`) |
|------|---------------------------|----------------------------------------|
| `wasted` | 7→9→11→13 持续增长 | **0** (全程) |
| `loop` | 3→4 | **1** (低于阈值 3) |
| `reread` | 1-2/轮 | **0** |
| `truncate` 丢弃 | 91-106 条 (80%) | **5-7 条 (20%)** |
| `qf` | `loop_injected, high_drop_ratio` | **[]** |
| 模型产出 | 0-500 chars (哑巴) | 10K+ chars (正常) |
| 会话消息数 | 120+ (循环膨胀) | 34 (正常增长) |
| 状态 | 死循环至断开 | **稳定 30+ 分钟** |

### Changed
- **配置** (`configs/rapid-mlx-35b.conf`): `PROXY_CLEAR_ENABLED=true` → `false`
- **代码行数** (`anthropic_proxy.py`): ~3,700 → ~4,050 (+~350)

### Known Issues
- **Context 增长速度**: Tool Clearing OFF 后，context 从 26K→66K 增长更快（全部保留）。但在 34 条消息时触发温和 truncate（丢弃 7 条），仍在可控范围
- **极端场景**: 若 Claude 读取 50+ 文件，context 可能难以控制。当前方案通过智能保留 Read 结果 + Truncate 压缩非 Read 内容来平衡

### Recommendation

**生产环境配置（当前）:**
```bash
PROXY_CLEAR_ENABLED=false           # 关闭 Tool Clearing
PROXY_CTX_TRUNCATE_STRATEGY=rounds  # 保留 rounds 策略
PROXY_TOOL_KEEP=8                   # 保持默认值
```

**Tool Clearing 不推荐在当前架构下启用**，除非后端支持 "文件未变化" 的显式语义（而非 "Wasted call" 错误）。

---

## [0.5.1-progress] - 2026-06-08

### Status: P0 修复冲刺中（v0.6.0 前置）

**范围**: 6 个 commit，修复/缓解 12 项缺陷（7 项完全修复 + 5 项部分修复）。

### Fixed

#### P0-Critical（7 项中 3 项完全修复，4 项部分修复）
- **DEF-001** 🟡 — 500 错误率缓解：预截断 (400K chars) + 503/504/499 错误分类 + Retry-After header + BrokenPipe→499 client_closed
- **DEF-002** 🟡 — 循环检测大修：移除 LOOP_CONSECUTIVE 双重计数（tail 扫描替代全量继承）+ 新增 Level 3（强制纯文本）+ Level 2 多工具移除 + `_LOOP_SESSION_STATE` 跨请求持久化
- **DEF-003** ✅ — re_read_rate 公式修正：`re_read_files / cleared_files * 100`，cap 100%。新增 `pipeline.re_read` 指标 + 5 个单测
- **DEF-004** ✅ — 工具过滤 recent 扫描验证非 bug：增强 observability（recent_tools 名称列表 + scanned_assistant 轮数）
- **DEF-005** 🟡 — OOM 缓解：`PROXY_OOM_SAFE_TOKENS=60000`，pipeline 后二次 token 检查（含 system prompt），超限强制 FIFO 截断
- **DEF-006** 🟡 — Kernel Panic 防御：`manage.sh` 启动前 sanity check，`--gpu-memory-utilization >0.85` 拒绝启动，`>0.80` 警告
- **DEF-007** ✅ — Chat template 工具化：`manage.sh fix-template <dir>` 一键修复 + 启动时自动检测 HuggingFace 缓存中的模板

#### P1-High（3 项完全修复 + 1 项部分修复）
- **DEF-101** ✅ — BrokenPipe/ConnectionResetError → 499 (client_closed)，不再返回 500
- **DEF-102** ✅ — fifo 策略确认为有意配置（利于 prefix cache 稳定，Plan 2D）
- **DEF-106** 🟡 — 非流式路径 JSON 修复：force_stopped 时回溯原始 tool_calls，调用 `_repair_truncated_json()`
- **DEF-108** ✅ — Blocker 触发修复：Pipeline 顺序修正（blocker detection 移到 tool-result clearing 之前），清除操作不再覆盖错误标记

#### P2-Medium（5 项修复 + 2 项缓解 + 1 项设计限制）
- **DEF-201** ✅ — re-read 检测改为仅扫描最后一次 assistant 消息，消除历史累积假阳性
- **DEF-202** ✅ — Bash dedup 跳过 `[cleared:...]` 内容，防止已清空结果反复触发 Jaccard 匹配
- **DEF-203** 🟡 — 工具过滤后按名字母排序，稳定 prefix cache 命中
- **DEF-204** ✅ — `/status` 端点不再产生日志噪音（GET /status 跳过 header logging）
- **DEF-205** ✅ — 请求去重：`_check_dedup()` 基于 body hash + `PROXY_DEDUP_WINDOW`(2s) 窗口，重复请求返回 429 + Retry-After
- **DEF-206** ⚪ — A/B 实验为运维决策工具，标记为设计限制
- **DEF-207** 🟡 — 新增 `./manage.sh watchdog` 命令，自动检测性能衰减并重启
- **DEF-104** 🟡 — 工具过滤日志增加 `filtered_out` 字段（被移除的工具名称排序列表），提升可观测性
- **DEF-107** 🟡 — 截断丢弃率 > 85% 时注入 `[System: Context severely truncated]` 通知

#### P3-Low（2 项修复）
- **DEF-302** ✅ — `_mask_sensitive()` 自动脱敏 `Authorization` / `X-Api-Key` 日志（首8+末4字符）
- **DEF-305** ✅ — `manage.sh start-cloud` 新增云 API `/models` 健康检查

### Added
- **Promptfoo 回归测试** (`promptfooconfig.yaml` + `tools/promptfoo_eval.sh`): 9 个固定 prompt 自动验证核心能力，集成到 `test/run_tests.sh --promptfoo`
- **pre-commit 分层触发**: 代理运行时自动跑 Promptfoo 快速模式（5 个核心测试），未运行时仅跑 unit tests
- **Langfuse sidecar** (`langfuse/docker-compose.yml`): 旁路观测基础设施就绪

### Changed
- **单元测试**: 78 → **124 cases**（+46），全部通过
- **DEFECT-LIST.md**: 更新修复状态统计（12 已修复 / 9 部分修复 / 8 未修复 / 1 设计限制）

### Metrics (snapshot at 0.5.1-progress)

| 指标 | 数值 |
|------|------|
| 总 commits | 39 (+6) |
| 代码行数 (`anthropic_proxy.py`) | ~3,700 (+~90) |
| 单元测试 | 124 cases (+46) |
| 缺陷修复 | 12 项（7 完全 + 5 部分） |
| Promptfoo 回归测试 | 9 cases |

---

## Future Releases

### [Unreleased] - target v0.6.0

**主题**: P0 遗留验证 + Langfuse 上线 + 剩余 P1/P2 修复

待办 (来自 DEFECT-LIST):
- ~~DEF-001~~ 🟡 部分修复 — 需生产验证 500 错误率 < 2%
- ~~DEF-002~~ 🟡 部分修复 — 需生产验证 loop_injected < 20%；Write 内容相似度检测未实施
- ~~DEF-003~~ ✅ 已修复
- ~~DEF-004~~ ✅ 已验证
- ~~DEF-005~~ 🟡 已缓解 — 需生产验证 OOM 减少
- ~~DEF-006~~ 🟡 已缓解 — 建议升级 rapid-mlx v0.6.71
- ~~DEF-007~~ ✅ 已修复
- 部署 Langfuse sidecar (3000 端口)
- 实施 `docs/proxy-prefix-cache-design.md` Phase 1-2
- 修复 DEF-107 (high_drop_ratio 干预)
- 修复 DEF-104 (白名单自动扩展)
- ~~DEF-204~~ ✅ 已修复
- ~~DEF-205~~ ✅ 已修复
- ~~DEF-302~~ ✅ 已修复
- ~~DEF-305~~ ✅ 已修复

### [Unreleased] - target v1.0.0

**主题**: llama_defender 库化 + 业务转型

- LiteLLM 替换协议转换 (~1200 LOC → ~100 LOC)
- 提取 `llama_defender` Python 库 (核心壁垒 6 模块)
- DSPy/GEPA 提示词优化模板
- 单元测试覆盖率 ≥ 80%
- 全部 30 项缺陷修复
- GitHub stars 1000+ 目标

---

[0.5.0-baseline]: #050-baseline---2026-06-06
