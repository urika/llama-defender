# PRD — litellm 借鉴: 压缩评分 / 工具原子单元 / 压缩结果结构化

> **PRD 版本**: v1.1 (2026-07-05 PM 评估后调整: TS-1 主路径接管 DEF-109 长上下文循环根治; 验收分场景分段; TS-2 真实贡献下调)
> **生成日期**: 2026-07-05
> **来源**: litellm 项目 (`/Users/jinsongwang/worklogs/litellm`) 横向对比分析 + 2026-07-05 跨阶段生产数据 (5240 metrics) PM 评估
> **目标版本**: v0.6.1 (M1 增量包,挂在 v0.6.0 P0 修复之后,v0.7.0 prefix cache 引擎化之前)
> **关联缺陷**: DEF-001 / DEF-002 / DEF-003 / DEF-103 / DEF-107 / DEF-109 (新) / DEF-203 / DEF-208
> **关联项目板**: [`v0.6.0.md`](../07-project-board/v0.6.0.md) #1 / #2 / #10 / #14, [`v0.6.1.md`](../07-project-board/v0.6.1.md)

---

## 目录

1. [背景与动机](#1-背景与动机)
2. [需求清单 (3 项)](#2-需求清单)
3. [方案设计](#3-方案设计)
4. [需求-缺陷-验收矩阵](#4-需求-缺陷-验收矩阵)
5. [工作量评估](#5-工作量评估)
6. [排期](#6-排期)
7. [风险登记册](#7-风险登记册)
8. [里程碑与交付物](#8-里程碑与交付物)
9. [决策记录](#9-决策记录)

---

## 1. 背景与动机

### 1.1 为何借鉴 litellm

横向对比 `/Users/jinsongwang/worklogs/litellm` 后发现,本项目盲点集中在**压缩决策的可量化性**与**工具调用语义边界**两处。litellm 用 BM25 评分 + tool-exchange 原子单元 + 结构化 `CompressedResult` 三套机制覆盖了这两点,虽然 litellm 整体不能直接搬过来用 (方向正交,详见 §9 决策记录),但作为方法论借鉴性价比高。

### 1.2 为何不借鉴 litellm 的其它功能

| litellm 特性 | 不借鉴原因 |
|---|---|
| Retrieval tool + agentic loop | 与"信息降级"哲学冲突;rapid-mlx 忽略 max_tokens 会让 retrieval 轮失控 |
| Embedding scorer | 违反标准库约束;BM25 已够用 |
| Redis/S3/DualCache 后端 | 单机本地部署不需要 |
| Virtual key / 多租户 | 单人使用 |
| 多 provider Router | 只有 local/cloud 两路 |
| Prompt-caching deployment affinity | 只有一个本地 deployment,无亲和可言 |

### 1.3 选这三项的判据 (v1.1 修订)

1. **TS-2 (工具原子单元)** —— 直接修复 DEF-001 (22% 500 错误) 与 DEF-107 (high_drop_ratio),不需要新依赖,改动局部
   - **(v1.1 修订)**: 对 DEF-002 循环注入的真实贡献被高估。Phase 1 长上下文 120 个 loop_injected 中,工具 Bash/Read/Text_loop/WebSearch 主因 78+19+13=110/120=92% 是**模型本身的循环决策**而非"截断切断配对→孤儿→Defensive Read"次级循环源。TS-2 仅覆盖少数孤儿诱发的循环,< 30% 贡献。
2. **TS-1 (BM25 评分)** —— 提升压缩决策质量 (DEF-103 / DEF-107) + **(v1.1 新)** **DEF-109 长上下文循环主路径**
   - PM 评估结论: 循环率与上下文长度强正相关 (calibration: `xs` 0.6% vs `xl` 62.7%, 105x 倍数差)
   - TS-1 通过降低长上下文 token 总量,**间接降低循环触发概率** (主路径),而非直接面对循环检测本身
   - 此条乃 v1.1 关键修订: 把长上下文循环根治责任主要落在 TS-1 而非 TS-2
3. **TS-3 (压缩结果结构化)** —— 为可观测性留接口 (DEF-003 / DEF-304),改动最小

三者形成"决策 → 执行 → 观测"闭环,故作为一组打包到 v0.6.1。

---

## 2. 需求清单

### TS-2 — Anthropic 工具配对原子单元 (P0)

**一句话**: 截断、压缩、清除任一动作必须把 assistant `tool_use` 与匹配的 user `tool_result` 视为不可分割的整体。

| 字段 | 值 |
|---|---|
| 优先级 | 🔴 P0 |
| 关联缺陷 | DEF-001 (22% 500 错误), DEF-002 (37% 循环注入), DEF-107 (high_drop_ratio 21.6%) |
| 关联 issue | v0.6.0 #1 #2 #14 |
| Component | Truncation (R1) |
| Effort | M (3-5d) |

**FR-TS-2-1**: 在 `truncation.py` 新增 `_find_tool_pairs(messages) -> List[Tuple[int, int]]`,返回每对 `(assistant_idx, user_idx)` 的起止区间。

**FR-TS-2-2**: `truncate_messages_if_needed` 在删除/替换任一消息前,先扫描 `_find_tool_pairs` 结果,若删除会切断配对,则同对一起删除或一起保留。

**FR-TS-2-3**: `clear_old_tool_results` (truncation.py:290) 同步引入配对约束 —— 删除 tool_result 时,对应的 tool_use assistant 必须同时移除,或两者都保留。

**FR-TS-2-4**: 当出现 "孤儿 tool_use" (配对的 tool_result 缺失) 或反之,跳过当前截断/清除操作并记录 `compression_skipped_reason = "invalid_anthropic_tool_sequence"`,直接走 fallback 策略。

**NFR-TS-2-1**: 纯标准库实现,不引入 jinja / jsonschema 等解析依赖。

**NFR-TS-2-2**: 单元测试新增 ≥ 12 个用例 (配对识别、孤儿处理、嵌套工具调用、跨多轮 tool_use、配对越界、stable order 验证)。

---

### TS-1 — BM25 评分驱动压缩决策 (P1)

**一句话**: `content_compressor.py` 引入 Okapi BM25 评分,作为"该不该压、压到多大"的量化决策依据。

| 字段 | 值 |
|---|---|
| 优先级 | 🟠 P1 |
| 关联缺陷 | DEF-103 (Cleared Compression 触发率低), DEF-107 (high_drop_ratio 21.6%) |
| 关联 issue | v0.6.0 #10 #14 |
| Component | Truncation (R1) |
| Effort | M (3-5d) |

**FR-TS-1-1**: 在 `content_compressor.py` 顶部新增 `bm25_score_message(msg, query, idf_map) -> float`,实现 Okapi BM25 (k1=1.5, b=0.75),tokenize 用纯 Python (`re.findall(r"[A-Za-z_][A-Za-z0-9_]*"|"[\u4e00-\u9fff]")`,小写归一化)。

**FR-TS-1-2**: query 提取取 `_extract_last_user_message(messages)`,与 litellm 一致;若有 4-char 前缀展开 (粗略匹配形态学变体),按 litellm `scoring/bm25.py` 的 min-prefix=4 策略实现。

**FR-TS-1-3**: `compress_tool_result` (content_compressor.py:218) 在多 tool_result 场景,**额外返回每条 tool_result 的 BM25 分数**,低分优先压缩;当前规则 (JSON/code/log/text 分类) 作为压缩手段保留,但**用 BM25 决定压缩顺序与最终保留比例**。

**FR-TS-1-4**: 单条 tool_result score 高于 `PROXY_BM25_KEEP_THRESHOLD` (新配置项,默认 3.5) 时,跳过压缩。低于 `PROXY_BM25_DROP_THRESHOLD` (默认 0.5) 时,可压缩到原长 30%。

**FR-TS-1-5**: 受 TS-2 配对约束保护,被保护对的 assistant + user 之间不可根据 BM25 单边压缩。

**NFR-TS-1-1**: 标准库 only,不引入 numpy / sklearn / rank-bm25 等包。

**NFR-TS-1-2**: BM25 计算延迟需 < 5ms / 100 条消息 (基于字典查找,无 IO)。

**NFR-TS-1-3**: 单元测试 ≥ 18 个用例 (空 query、纯标点、跨语言、IDF 未登录词、阈值边界、长 tool_result 衰减)。

---

### TS-3 — 压缩结果结构化 (P1)

**一句话**: 所有压缩/截断/清除操作的产物统一为结构化结果对象,带有 `compression_skipped_reason` 枚举,可被 `/status` 与日志聚合。

| 字段 | 值 |
|---|---|
| 优先级 | 🟠 P1 |
| 关联缺陷 | DEF-003 (re_read_rate 公式错), DEF-304 (可观测性仪表板缺) |
| 关联 issue | v0.6.0 #3 #29 |
| Component | Observability (R6) + Truncation (R1) |
| Effort | S (1-2d) |

**FR-TS-3-1**: 在 `proxy_state.py` 或新建 `compression_types.py` 定义 TypedDict (Python 3.9 兼容,使用 `typing.TypedDict`):

```python
class CompressionResult(TypedDict):
    messages: List[dict]
    original_tokens: int
    compressed_tokens: int
    compression_ratio: float           # 1 - compressed/original
    strategy: str                      # "bm25" | "rule_based" | "smart" | "rounds" | "fifo" | "no_op"
    skipped_reason: Optional[str]      # "below_trigger" | "invalid_anthropic_tool_sequence"
                                       #  | "all_protected" | "no_reduction" | "stage_skip"
    protected_indices: List[int]       # 受 TS-2 / CacheAligner 保护的索引
    dropped_indices: List[int]
    cache_keys: Dict[str, str]         # 留空,信息降级方案无 retrieval key (留接口)
    bm25_scores: Dict[int, float]      # 当 strategy="bm25" 时填入,否则空
```

**FR-TS-3-2**: `compress_tool_result` / `truncate_messages_if_needed` / `clear_old_tool_results` 改为返回 `CompressionResult`。调用方 (`pipeline.py` Stage 10 ContentCompressor / Stage 17 ContextTruncator) 适配调用契约。

**FR-TS-3-3**: `pipeline.py` 把 `CompressionResult.stripped()` 挂到 `ctx.metrics` 末尾,替换原有零散字段。

**FR-TS-3-4**: `admin_server.py` `_get_proxy_metrics` 增加 `compression_stats` 段,聚合当日所有请求的 `strategy` 计数与平均 `compression_ratio`,输出到 `/status` JSON 段 `"compression"`。

**NFR-TS-3-1**: 标准库 only;`TypedDict` 在 Python 3.9 兼容 (使用 `from typing import TypedDict`)。

**NFR-TS-3-2**: 既有单元测试中调用 `compress_tool_result` / `truncate_messages_if_needed` 的用例需要适配新返回类型,签名快照 (`tools/gen_func_signatures.py`) 同步刷新。

**NFR-TS-3-3**: 行为快照 (`tools/gen_behavior_snapshots.py`) 同步刷新。

---

## 3. 方案设计

### 3.1 整体关系

```
TS-2 (工具原子单元)         ← 必须先做,提供边界保护
   ▼
TS-1 (BM25 评分)            ← 在边界内打分
   ▼
TS-3 (结果结构化)            ← 包装 TS-1 / TS-2 的输出
```

### 3.2 落地位置

| 需求 | 主要修改文件 | 次要修改文件 |
|---|---|---|
| TS-2 | `truncation.py` (新增 `_find_tool_pairs` + 改 `truncate_messages_if_needed` / `clear_old_tool_results`) | `pipeline.py` (CacheAligner 调用点), `test/unit/test_truncation.py` (新增用例) |
| TS-1 | `content_compressor.py` (新增 `bm25_*` + 改 `compress_tool_result`) | `proxy_config.py` (`CONFIG_REGISTRY` 注册 `PROXY_BM25_*`), `test/unit/test_content_compressor.py` |
| TS-3 | 新建 `compression_types.py` 或在 `proxy_state.py` 加 TypedDict | `truncation.py` / `content_compressor.py` / `pipeline.py` / `admin_server.py` 适配返回类型 |

### 3.3 配置项新增 (TS-1)

在 `proxy_config.py` 的 `CONFIG_REGISTRY` 注册:

| 变量 | 默认 | 类型 | 作用域 |
|---|---|---|---|
| `PROXY_BM25_ENABLED` | `true` (local) / `false` (cloud) | bool | 通用 |
| `PROXY_BM25_K1` | `1.5` | float | 通用 |
| `PROXY_BM25_B` | `0.75` | float | 通用 |
| `PROXY_BM25_KEEP_THRESHOLD` | `3.5` | float | 通用 |
| `PROXY_BM25_DROP_THRESHOLD` | `0.5` | float | 通用 |
| `PROXY_BM25_MIN_PREFIX` | `4` | int | 通用 |

在 `manage.sh` 顶部加同名默认值。

文档同步:必须在 `CLAUDE.md` 与 `AGENTS.md` 第 6.2 节、第 7 节同步新增条目 (按 AGENTS.md §9 检查清单要求)。

### 3.4 风险降低 —— 不动 prefix-cache 稳定路径

TS-1 / TS-2 的所有改动必须**在 `CacheAligner` 划出的 protected 段之外**生效。protected 段在前 N 条 (`PROXY_CACHE_ALIGN_PREFIX_N`),BM25 评分只对 dynamic 段 (import 命令,后续会话) 跑;tool pair 保护只对 protected 段之后的配对生效 (因为 protected 段本身的 tool 配对本来就不会动)。

---

## 4. 需求-缺陷-验收矩阵 (v1.1 修订: 分场景分段验收)

> **v1.1 PM 评估驱动调整**:
> 1. 验收不再一刀切 < 20%,改为按上下文长度场景分段
> 2. TS-2 对 DEF-002 循环注入的真实贡献预期从 ≥ 50% 下调为 < 30% (实测样本分析)
> 3. 新增 DEF-109 长上下文循环根治,主路径由 TS-1 承担
> 4. 统计口径修复 (loop_injected 打标改为基于 level≥1) 由 M2 #16a 承担

| 需求 | 关联 DEFECT | 现有验收标准 (v0.6.0) | 本 PRD 补充验收 (v1.1 分场景分段) |
|---|---|---|---|
| TS-2 | DEF-001 | 500 错误率 < 2% | 验收日志 5 天内不再出现 `TemplateError: tool_use without tool_result` 类错误 |
| TS-2 | DEF-002 (部分) | 循环注入率 < 20% (短上下文) | **下调贡献预期**: defensive re-read 触发率下降 < 30% (而非 ≥ 50%)。<br>主因分析: Phase 1 长上下文 120 loop 中 Bash/Read/Text_loop 主因占 92%, TS-2 仅覆盖少数孤儿诱发循环 |
| TS-2 | DEF-107 | high_drop_ratio < 10% | 工具完整配对时 high_drop_ratio 自然下降 |
| TS-1 | DEF-103 | 重新评估触发条件 | `PROXY_BM25_ENABLED=true` 后压缩触发率 ≥ 60% (相比当前约 30%) |
| TS-1 | DEF-107 | < 10%, 主动 compact 建议 | 低 BM25 分数的 tool_result 优先被压缩,不再一刀切 |
| **TS-1 (主路径)** | **DEF-109 (新)** | — (新缺陷) | **分场景验收**: <br>- 短上下文 (`xs/sm` <50K): ≤ 5% <br>- 中等 (`md` 50K-100K): ≤ 30% <br>- **长上下文 (`lg/xl` ≥100K): ≤ 40%** (Phase 1 39.2%, TS-1 加压缩后期进一步降) <br>- **`saturation` 阶段: ≤ 30%** (现状 53%) <br>- 验证样本必须覆盖 `saturation` + `expansion` 阶段, 不能只跑 `init` |
| TS-3 | DEF-003 | re_read_rate ≤ 100% | `/status` JSON 新增 `compression` 段后,re_read 公式替换为基于 `bm25_scores` 派生 |
| TS-3 | DEF-304 | (可观测性仪表板) | 压缩段作为仪表板首批字段,后续接入仪表板时直接读 |
| 通用 | DEF-208 | 单元测试 167 cases | 三项合计新增 ≥ 36 用例 (12 + 18 + 6 适配),幼航到 ≥ 203 cases |

---

## 5. 工作量评估

### 5.1 工时拆解

| 任务 | 估时 | 备注 |
|---|---|---|
| **TS-2 工具原子单元** | 3d | |
| └ `_find_tool_pairs` 实现 | 0.5d | 含边界覆盖测试 |
| └ 改 `truncate_messages_if_needed` 三路 (smart/rounds/fifo) | 1d | 三种策略各自加配对约束 |
| └ 改 `clear_old_tool_results` | 0.5d | |
| └ 12 个单元测试 | 1d | |
| **TS-1 BM25 评分** | 5d | 难点在 IDF 与 prefix expansion |
| └ `bm25_score_message` + IDF 维护 | 1d | 黑名 IDF 用进程内字典累积,跨请求稳定 |
| └ prefix expansion 实现 | 1d | min 4-char 前缀,纯字符串切片 |
| └ `compress_tool_result` 集成 | 1d | 受 TS-2 配对约束的双向接口 |
| └ `CONFIG_REGISTRY` + `manage.sh` 配置 | 0.5d | |
| └ 18 个单元测试 | 1.5d | 含 IDF 在线累积、跨语言、阈值边界 |
| **TS-3 结果结构化** | 2d | |
| └ `CompressionResult` 定义 | 0.25d | |
| └ 改 `compress_tool_result` / `truncate_messages_if_needed` / `clear_old_tool_results` 返回 | 0.75d | |
| └ `pipeline.py` 调用契约适配 | 0.5d | |
| └ `admin_server.py` 加 `compression_stats` | 0.5d | |
| **签名/行为快照刷新** | 0.5d | Signature + Snapshot |
| **集成测试更新与回归** | 1d | run_tests.sh --all 跑通 |
| **文档同步** (CLAUDE.md / AGENTS.md / proxy_config.py doc) | 0.5d | |
| **合计** | **12d ≈ 2.5 周净工时** | (预计 3.5 周含日程缓冲) |

### 5.2 并行度

- TS-2 必须先动 (TS-1 配对约束的边界由 TS-2 定)。
- TS-3 可与 TS-2 后半段并行 (TS-3 不依赖 BM25 算法本身,只依赖统一返回类型)。
- 严格次序: **TS-2 → (TS-1 ‖ TS-3 前置)** → TS-3 后置。

---

## 6. 排期

### 6.1 固定到既有 milestone

- **M1 (v0.6.0 P0 全部关闭)**: 现目标 2026-07-15 (5 周)。**不动。** TS-2 修 DEF-001 / DEF-002 虽然落在 M1 范围内,但由于实现方式不同,需要在 M1 之外另开窗口。
- **M1.5 (v0.6.1 litellm 借鉴增量包)**: **本 PRD 落点** — 2026-07-20 ~ 2026-08-15,4 周窗口。
- **M2 (v0.6.0 P1 + Langfuse 集成)**: 现目标 2026-08-31。本 PRD 完成后,P1 中的 DEF-103 / DEF-107 直接 🟩 Done,无需再走旧路径。
- **M3 (v0.7.0 prefix cache 引擎化)**: 2026 Q4。本 PRD 为其铺路 —— TS-2 的工具配对保护让 prefix 字节更稳定。

### 6.2 周计划

| 周次 | 日期 | 内容 | 退出条件 |
|---|---|---|---|
| W1 | 2026-07-20 ~ 07-24 | TS-2 设计评审 + 实现主路径 (`_find_tool_pairs` + `truncate_messages_if_needed` smart 路径) | smart 路径配对保护测试通过 (6 用例) |
| W2 | 2026-07-27 ~ 07-31 | TS-2 适配 rounds / fifo + `clear_old_tool_results` + 单元测试补齐 (12 用例) | `bash test/run_tests.sh --unit` 全绿; DEF-001 复测无新错 |
| W3 | 2026-08-03 ~ 08-07 | TS-1 BM25 核心实现 + IDF 维护 + prefix expansion | BM25 单元测试 12 用例通过;受 TS-2 边界保护的 `compress_tool_result` 集成 |
| W3 末 | 2026-08-07 | TS-3 `CompressionResult` 定义 + 全返回类型改造 | 签名快照刷新;既有测试链全绿 |
| W4 | 2026-08-10 ~ 08-14 | TS-3 `admin_server.py` 聚合 + `/status` 段上线 + 文档同步 + 全量回归 | `./manage.sh status` 显示 compression 段; CLAUDE.md / AGENTS.md 更新; `--all` 通过 |

### 6.3 时间线视图

```
M1 (v0.6.0 P0)   ▓▓▓▓▓░░░░░░░░░░░░░░  2026-07-15 关闭
M1.5 (v0.6.1)    ░░░░░▓▓▓▓▓▓▓▓▓▓░░  2026-07-20 → 2026-08-15
M2 (v0.6.0 P1)   ░░░░░░░░░░░░░▓▓▓▓  2026-08-31 (本 PRD 提前关闭 #10 #14)
M3 (v0.7.0 PC)   ░░░░░░░░░░░░░░░░░░  2026 Q4
```

### 6.4 与 v0.6.0 项目板的修订

v0.6.0.md #3.1 (P0) #1 #2 在 M1 仍按原计划推进 (WrapperGuard / 跨请求循环检测)。M1 关闭后,P0 中的 DEF-001 / DEF-002 走 **双路径**:

1. 原路径: WrapperGuard 防御性修复 (M1 内完成,作为 hotfix)
2. 本 PRD 路径: TS-2 根治 (M1.5 内完成,作为根治性替代)

DEF-107 (#14) 直接归到本 PRD W2 退出条件内,M1 不再单独闭门 (从 🟡 Mitigated → 🟩 Done 在 M1.5)。

### 6.5 持续性要求

- 每周一次进度同步到 `docs/07-project-board/v0.6.0.md`,在 #1 / #2 / #10 / #14 行尾追加 `(M1.5 替代路径; PRD-litellm-borrow v1.0 §6.2 W{X})`。
- 本 PRD 完结时,M1.5 关闭并在 `docs/05-operations-changelog/` 增加 changelog `2026-08-15-v0.6.1-litellm-borrow.md`。

---

## 7. 风险登记册

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| R-1 | TS-2 配对识别实现错误,误删可保留的 tool_use,反而打破 prefix-cache | M | H | 单元测试 ≥ 12 用例覆盖跨多轮 / 嵌套 / 无配对 / 索引越界;signature 快照刷新;先在 dev 配置启用 |
| R-2 | BM25 IDF 跨请求维护导致内存膨胀 | L | M | IDF 字典加 LRU 上限 (10000 tokens),超过清理低频项 |
| R-3 | 跨语言 (中英文混合) BM25 评分偏差大 | M | M | tokenize 对中文走 `[\u4e00-\u9fff]` 单字切分,与 litellm 不同 (litellm 不处理中文);W3 内加 ≥ 3 用例验证 |
| R-4 | `CompressionResult` 类型变化波及过多调用点 | M | M | 先用 union 类型 (返回 dict 兼容旧调用) 再迭代;signature 快照保证路由层兼容 |
| R-5 | 与 v0.6.0 同时排期,资源冲突 | M | H | 本 PRD 严格挂在 M1.5 (M1 冻结后启动);每周对齐进度,发现冲突则压缩 TS-1 范围 (只保留 KEEP/DROP threshold,不做前缀展开) |
| R-6 | rapid-mlx 输出不稳定造成 BM25 评分抖动 | L | L | query 用最后一条 user message 而非 assistant 输出,与 litellm 一致,未受输出波动影响 |
| R-7 | TS-3 行为快照刷新遗漏某一调用点 | L | M | `tools/gen_behavior_snapshots.py` 跑通即说明覆盖到位;signature + snapshot 双闸 |
| **R-9 (新)** | **TS-2 长上下文实战样本不足** | M | H | Phase 3 (07-05, TS-2 上线) 37 条样本**全部 `init` 阶段**, 最长 9.5K chars, 0 长上下文样本。M-TS-2 5 日观察必须强制覆盖 `saturation`+`expansion` 阶段, 否则 0% 数据不代表 TS-2 在长上下文下有效 |
| **R-10 (新)** | **全部走本地路径会恶化循环** | M | H | 长上下文 ≥100K 当前主要由 cloud 路径承担。若 future 无 cloud 兜底, rapid-mlx + Metal OOM + Wasted call 三重叠加, 长上下文循环率预计 50-65%, 突破验收 ≤ 40%。v0.7.0 prefix cache 引擎化前不建议全切本地 |

---

## 8. 里程碑与交付物

### M-TS-2 (W2 末,2026-07-31)

- [ ] `truncation.py:_find_tool_pairs` 实现 + 单元测试 12 用例
- [ ] `truncate_messages_if_needed` / `clear_old_tool_results` 走配对约束
- [ ] `bash test/run_tests.sh --unit` 全绿
- [ ] DEF-001 / DEF-002 现场复测,日志 5 天内无 `TemplateError: tool_use without tool_result`

### M-TS-1 (W3 末,2026-08-07)

- [ ] `content_compressor.py` BM25 实现 + 单元测试 18 用例
- [ ] `CONFIG_REGISTRY` 新增 6 项配置
- [ ] `manage.sh` 顶部加默认值
- [ ] `./manage.sh reload` 可热切换 `PROXY_BM25_ENABLED` 真实生效
- [ ] **(v1.1 新)** DEF-109 长上下文循环验收: 至少 5 个 session 跑到 `saturation` 阶段 (≥100K chars), 统计 BM25 启用前后 loop_injected 率, 验收线见 §4 分场景分段
- [ ] **(v1.1 新)** 证明 TS-1 主路径有效: BM25 启用时长上下文 `lg/xl` loop 率 ≤ 40% (对照 Phase 1 39.2% 基线)

### M-TS-3 + 总收尾 (W4 末,2026-08-15)

- [ ] `CompressionResult` TypedDict 定义
- [ ] 所有 compress/truncate/clear 返回新类型
- [ ] `admin_server.py` `/status` JSON 新增 `compression` 段
- [ ] 签名快照 + 行为快照刷新
- [ ] `CLAUDE.md` / `AGENTS.md` 同步更新 §6.2 / §7
- [ ] `bash test/run_tests.sh --all` (单元 + 集成) 全绿
- [ ] changelog `docs/05-operations-changelog/2026-08-15-v0.6.1-litellm-borrow.md`

---

## 9. 决策记录

### 9.1 ADR-LB-01: 不引入 litellm 的 retrieval tool + agentic loop

**决策**: TS-1 / TS-2 / TS-3 只借鉴算法与结构,不引入 litellm 的"压缩 + 注入 retrieval tool + agentic loop 回填"机制。

**理由**: rapid-mlx 忽略 `max_tokens` (DEF-106),retrieval 回填那一轮生成长度可能远超预算;且与本项目"信息降级"哲学冲突 —— 本项目的 KV-prefix-cache 优化建立在"前缀字节稳定"前提上,临时注入 retrieval tool 会破坏 prefix。

### 9.2 ADR-LB-02: 不借助 litellm 的 embedding scorer

**决策**: TS-1 只实现 BM25,不做 embedding。

**理由**: (1) 违反 `AGENTS.md` §6.2 "stdlib only" 约束;(2) BM25 在 agentic coding 场景已能识别"这条 tool_result 跟当前问题相关性";(3) embedding 调用本身造价 (DeepSeek embedding 计费) 与"本地省 token"目标矛盾。

### 9.3 ADR-LB-03: 不借鉴 litellm 的 DualCache / Redis 等外部缓存

**决策**: 本 PRD 不引入 DualCache 类。

**理由**: 单机本地部署场景下,进程内字典 + 落 `logs/cache.pkl` 已能覆盖 summary / loop state 等需求。引入 Redis 等外部缓存会给"开机自启"路径增加运行时依赖,偏离"48GB Mac 单人开发"的真实负载。

### 9.4 ADR-LB-04: 排期挂在 M1.5 而非并入 M1

**决策**: 不挤 M1 (P0 修复窗口),新开 M1.5。

**理由**: M1 已是 5 周满负荷 (P0 7 项);TS-2 虽然修 DEF-001 / DEF-002,但是另一种实现路径 (WrapperGuard 是防御性热修,TS-2 是根治性结构改造),两条路径不能同窗并行做,会互相覆盖。M1 完成后再上 TS-2,可作 v0.6.0 验证基线对照。

### 9.5 后续可能借鉴 (本 PRD 不包含,登记到 backlog)

- TS-4: Cooldown 模型 — 后端连续失败自动切云端 (落地到 `backend_strategy.py`)
- TS-5: Cache key 设计 — 为将来响应缓存预留接口
- TS-6: CacheAligner 增强 — 受保护集合增加"最后一条 user + 最后一条 assistant"

### 9.6 ADR-LB-05: 循环验收改为分场景分段 (v1.1 PM 评估之后修订)

**决策** (2026-07-05): 放弃 v1.0 "loop_injected 综合 < 20%" 的一刀切验收,改为按字符桶 + lifecycle stage 分场景验收。

**理由**: 跨阶段生产数据 (5240 metrics) 表明循环与上下文长度强正相关 (xs 0.6% → xl 62.7%, 105x 倍数差),一刀切综合验收会掩盖长上下文场景的真实问题。Phase 1 综合 6.6% 已达 v1.0 验收,但长上下文 39.2% 实际未解决。分场景:
- 短上下文 (xs/sm): ≤ 5% — 已达标, 基线
- 中等 (md): ≤ 30%
- 长上下文 (lg/xl): ≤ 40% — PRD 主目标
- saturation 阶段: ≤ 30%

### 9.7 ADR-LB-06: DEF-109 主路径由 TS-1 而非 TS-2 承担

**决策** (2026-07-05): 新缺陷 DEF-109 (长上下文 agentic 循环率过高) 的主缓解路径由 TS-1 (BM25 压缩降低长上下文 token 总量) 承担,而非 TS-2 (工具配对原子保护)。

**理由**: PM 评估 Phase 1 长上下文 120 个 loop_injected 工具分布: Bash 53 + Read 26 + text_loop 19 + WebSearch 13 = 111/120 = 92.5% 是**模型本身的工具调用决策循环**,非"截断切断配对→Defensive Read"次级循环源。TS-2 形式化保护对孤儿诱发循环有效 < 30%。TS-1 通过降低上下文长度间接降低循环触发概率,是更根本的缓解路径。DEF-002 调参 (#16a 在 M2) 作为辅助路径 (long/very_long 阈值下调触发更早干预)。

---

## 10. 附录

### 10.1 litellm 参考实现索引

| 概念 | litellm 文件:行 | 本 PRD 借鉴方式 |
|---|---|---|
| `_extract_anthropic_tool_exchange_spans` | `litellm/compression/compress.py:165-205` | TS-2 用相同思想实现 `_find_tool_pairs`,但不依赖 OpenAI↔Anthropic 适配器 |
| `bm25_score_messages` (k1=1.5, b=0.75) | `litellm/compression/scoring/bm25.py:34` | TS-1 同参数实现,中文 tokenize 改为单字切分 |
| 前缀展开 (min-prefix=4) | `litellm/compression/scoring/bm25.py` 内 | TS-1 保留该启发式 |
| `_get_protected_indices` (system + last user + last assistant) | `litellm/compression/compress.py:208-234` | 仅作 CacheAligner 增强 (TS-6/backlog) 的参考,不引入本 PRD |
| `CompressedResult` TypedDict | `litellm/types/compression.py:15` | TS-3 借鉴字段集合,但 `cache_keys` 留空 (信息降级方案无 retrieval) |

### 10.2 关联文档

- [`docs/01-requirements-product/PRD-anthropic-proxy.md`](PRD-anthropic-proxy.md) 主 PRD
- [`docs/01-requirements-product/PM-evaluation-and-schedule-2026-06-22.md`](PM-evaluation-and-schedule-2026-06-22.md) 路由模块排期参考
- [`docs/07-project-board/v0.6.0.md`](../07-project-board/v0.6.0.md) v0.6.0 项目板
- [`docs/DEFECT-LIST.md`](../DEFECT-LIST.md) 缺陷全表
- [`AGENTS.md`](../../AGENTS.md) §6.2 / §7 / §9 修改检查清单 (本 PRD 完结时必须同步)
- [`CLAUDE.md`](../../CLAUDE.md) 同步更新

---

> 本文件创建于 2026-07-05,依据 litellm 项目 (`/Users/jinsongwang/worklogs/litellm`) 对照分析。