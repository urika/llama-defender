# 04 解决方案：代理层可落地的优化方向

## 4.1 总体思路

> **以 Cache Aligner 为核心，语义保留压缩为补充，循环/资源/观测为护栏，逐步迭代而非一次性重构。**

| 层级 | 目标 | 手段 |
|------|------|------|
| 稳定层 | 提升 prefix cache 命中率 | 固定 system/skills/工具定义顺序，隔离动态内容 |
| 压缩层 | 降低单轮 token 量 | 结构化压缩（JSON/代码/日志），保留语义 |
| 防御层 | 减少无效循环 | 增强 loop/blocker 检测，语义化错误提示 |
| 资源层 | 延缓 OOM/衰减 | 内存预测、动态并发控制、输出限制 |
| 观测层 | 支持调参与验证 | 统一指标、快照、回归测试 |

## 4.2 方案 A：Cache Aligner 化（优先级 P0）

### 4.2.1 目标

将相邻请求的共同前缀稳定在 **80% 以上**，prefix cache 命中率从当前 0% 提升到可观测水平。

### 4.2.2 具体措施

| 措施 | 当前状态 | 建议改动 | 负责模块 |
|------|----------|----------|----------|
| 固定消息顺序 | `rounds` 策略会重排消息 | 将 system、skills、工具定义始终放在前 N 位，不参与 truncation | `truncate_messages_if_needed` |
| 隔离动态 system 插入 | Claude Code 的 `mid-conversation-system` 插入中间 | 将非首条 system 消息合并为单独 user 提示或移到固定前缀末尾 | `do_POST` 前置处理 |
| 工具定义顺序稳定 | `_filter_tools` 按输入顺序保留 | 过滤后按固定白名单顺序排序，缺失的留空占位 | `_filter_tools` |
| 工具调用结果位置稳定 | `smart preserve` 插入位置随内容变化 | 约定 Read 结果统一追加到固定前缀之后，而非散列 | `truncate_messages_if_needed` |
| Token budget 不再动态减少 keep_rounds | 字符超限时 `keep_rounds` 递减 | 固定 `keep_rounds`，超限改用摘要或 FIFO，不压缩固定前缀 | `truncate_messages_if_needed` |

### 4.2.3 风险与缓解

| 风险 | 缓解 |
|------|------|
| 固定前缀过长导致可用窗口减少 | 通过语义压缩释放空间 |
| 工具定义顺序改变破坏现有逻辑 | 保留 `filtered_out` 记录，先在 cloud 模式测试 |
| 动态 system 信息丢失 | 将其转为 user 提示追加到末尾，信息不丢 |

### 4.2.4 预期收益

- 相邻请求共同前缀从 35% 提升到 80%+。
- Rapid-MLX prefix cache 命中率从 0% 提升到可测量正值。
- 单轮 prefill 延迟下降。

## 4.3 方案 B：语义保留压缩（优先级 P1）

### 4.3.1 目标

替代当前粗暴的 tool result clearing，降低 token 量同时保留模型可理解的语义。

### 4.3.2 具体措施

| 措施 | 参考来源 | 实现要点 |
|------|----------|----------|
| JSON 输出结构化摘要 | TokenSieve `sieve.rs` | 对数组保留前 N 项+摘要，截断长字符串但保留 key 结构 |
| 代码文件空白/注释消除 | Kompact `code_compressor` | 仅删除不影响语法的空白和注释，不删除代码 |
| 日志输出聚合 | Kompact `log_compressor` | 去除时间戳、聚合重复行、保留错误/异常行 |
| HTML/XML 输出剥离 | Kompact `html_stripper` | 对 WebFetch 结果去除标签，保留文本 |
| Schema 字段精简 | Kompact `schema_optimizer` | 删除工具定义中的冗余字段（default/example 等） |

### 4.3.3 实现原则

- **可逆性**：压缩后的内容仍能让模型判断"文件是否存在、内容是什么"。
- **选择性**：只压缩过长的 tool_result（>阈值），不压缩短内容。
- **开关化**：`PROXY_COMPRESS_MODE=lossless|semantic|aggressive`，默认 `lossless`。
- **白名单**：压缩器支持 `application/json`、`text/plain code`、日志等 MIME 类型。

### 4.3.4 风险与缓解

| 风险 | 缓解 |
|------|------|
| 压缩导致 JSON 语法错误 | 压缩后解析验证，失败则回退原样 |
| 模型误解摘要内容 | 对摘要添加前缀说明（如 `[summarized: 20 items, 3 unique]`） |
| 压缩本身消耗 CPU | 仅对超长内容启用，小请求跳过 |

## 4.4 方案 C：循环与异常行为防御（优先级 P1）

### 4.4.1 目标

在语义保留压缩基础上，进一步降低 `wasted`、`re-read`、文本循环的发生概率。

### 4.4.2 具体措施

| 措施 | 当前状态 | 增强方向 |
|------|----------|----------|
| 错误语义翻译 | 已翻译 `Wasted call` 等 | 增加 Kompact 风格的关键字段提取，让模型知道"上次读了什么" |
| 工具结果指纹 | 无 | 对 Read 结果计算内容指纹，跨请求识别"已读过且未变化" |
| 文本循环检测 | 已有 Jaccard 相似度 | 增加 token 级别 n-gram 检测 |
| Blocker 升级 | 已有 L1/L2/L3 | 增加"强制换工具"建议（如 Read 失败 → 用 Bash） |
| 循环历史继承 | 每次请求重置 | 在 session 上下文摘要中携带"近期已失败操作" |

### 4.4.3 关键改动

```python
# 伪代码：Read 结果指纹缓存
_read_fingerprints = {}

def _fingerprint_read_result(path: str, content: str) -> str:
    fp = hashlib.sha256(content.encode()).hexdigest()[:16]
    _read_fingerprints[path] = fp
    return fp

def _is_re_read(path: str, content: str) -> bool:
    fp = hashlib.sha256(content.encode()).hexdigest()[:16]
    return _read_fingerprints.get(path) == fp
```

## 4.5 方案 D：资源与并发护栏（优先级 P2）

### 4.5.1 目标

降低 OOM 概率，延缓性能衰减。

### 4.5.2 具体措施

| 措施 | 当前状态 | 建议改动 |
|------|----------|----------|
| 请求 token 预估 | `chars/4` | 引入更精确的字符-token 比，按语言动态调整 |
| 动态并发控制 | 固定 semaphore | 根据后端响应时间/错误率调整 `PROXY_MAX_CONCURRENT` |
| 输出截断兜底 | `PROXY_MAX_TOKENS_OVERRIDE` | 改为按后端实际支持情况动态设置 |
| 内存预警 | `/status` 显示内存 | 在请求高峰前主动拒绝或等待 |

## 4.6 方案 E：可观测性增强（优先级 P2）

### 4.6.1 目标

让每次优化都可被测量、可验证、可回滚。

### 4.6.2 具体措施

| 措施 | 当前状态 | 建议改动 |
|------|----------|----------|
| 修复错误指标 | `re_read_rate` 公式错误 | 修正为 `re_read_files / unique_read_files` |
| 增加 cache 指标 | 无 | 在 metrics 中记录 `common_prefix_ratio`、`estimated_cache_hit` |
| 请求快照 | `/tmp/anthropic_request_body.json` | 对大请求失败时写入前后对比快照 |
| 工具过滤看板 | 日志记录 `filtered_out` | 增加每工具被过滤频次统计 |
| 配置效果 A/B | 手动切换 | 通过 `run_experiment.sh` 自动化对比 |

## 4.7 方案 F：配置治理（优先级 P3）

### 4.7.1 目标

降低配置复杂度，提供推荐组合。

### 4.7.2 具体措施

| 措施 | 说明 |
|------|------|
| 配置模板 | `configs/*.conf` 提供 3 套模板：`local-aggressive`、`local-balanced`、`cloud` |
| 参数分层 | 全局默认 → 后端默认 → 场景覆盖 |
| 配置校验 | 启动时校验参数组合，警告冲突（如 `CLEAR_ENABLED=true` + `TRUNCATE_STRATEGY=rounds`） |
| 文档同步 | 每次新增参数同步更新 `AGENTS.md` 和 `docs/` |

## 4.8 各方案与痛点的对应关系

```
痛点 1: 上下文 vs cache      ← 方案 A（Cache Aligner）+ 方案 B（语义压缩）
痛点 2: tool result 清除      ← 方案 B（语义保留压缩）
痛点 3: 循环行为多样性        ← 方案 C（循环防御）
痛点 4: 后端资源约束          ← 方案 D（资源护栏）+ 方案 B（降 token）
痛点 5: 工具过滤白名单        ← 方案 A（稳定工具顺序）+ 方案 F（配置治理）
痛点 6: 兼容性摩擦           ← 方案 A（隔离动态 system）+ 方案 F（模板配置）
痛点 7: 可观测性不足          ← 方案 E（metrics 增强）
痛点 8: 配置复杂度            ← 方案 F（配置治理）
```

## 4.9 实施原则

1. **小步快跑**：每个方案拆分为独立 PR，可单独开关。
2. **先测后开**：默认关闭，通过集成测试和 A/B 实验验证后再默认开启。
3. **保持可逆**：新增功能必须支持 `PROXY_*_ENABLED=false` 回退。
4. **不引入外部依赖**：核心逻辑在 `anthropic_proxy.py` 中用标准库实现。
5. **metrics 先行**：每次改动前确保基线指标可采集。
6. **环境感知**：区分 local 与 cloud 模式，避免把硬件代偿策略强加给资源充裕的环境。
7. **约束内优化**：承认 48GB + 35B 的硬边界，不追求根除硬件症状，而是最大化代理层可控收益。

## 4.10 约束分析对方案选择的影响

`02-problems.md` 中已将 8 大痛点按约束关联分为三类：

| 分类 | 痛点 | 代理层应对策略 |
|------|------|----------------|
| 直接由约束导致 | 1、4、5 | **缓解为主**，在资源边界内寻找最大收益，同时保留升级硬件的退出路径 |
| 被约束显著放大 | 2、3、6 | **代偿为主**，用代理层防御逻辑弥补模型/后端能力不足 |
| 相对独立但治理受约束影响 | 7、8 | **根治为主**，与硬件无关，必须工程化解决 |

这一分类对方案选择产生以下 5 个具体影响：

### 影响 1：优先选择不依赖硬件升级的优化

在 48GB + 35B 的硬约束下，任何依赖"更多内存"或"更强模型"的方案都不现实。因此：

- **优先做**：Cache Aligner（方案 A）、指标修复（方案 E 子项）、配置治理（方案 F）。
- **谨慎做**：LLM-based 内容摘要、自然语言改写——它们增加计算开销或破坏 prefix cache。
- **不做**：要求扩大上下文窗口的方案。

### 影响 2：同一策略需要区分 local / cloud 默认行为

约束分析揭示：同一功能在两种模式下的必要性可能相反。

| 策略 | local 模式 | cloud 模式 | 原因 |
|------|------------|------------|------|
| aggressive tool filtering | 可能需要 | 不需要 | cloud 上下文充裕 |
| context truncation | 必需 | 大幅放宽 | cloud 支持 1M token |
| blocker/loop 多层防御 | 多层 | 轻量 | 本地模型能力弱 |
| prefix cache 稳定化 | 核心收益 | 边际收益低 | cloud prefill 本身快且便宜 |

因此所有新功能都应支持：**总开关 + 模式感知默认值 + 配置模板覆盖**。

### 影响 3：成功标准必须现实化

不能按"理想环境"设定 KPI：

| 指标 | 不切实际的期望 | 约束下的现实目标 |
|------|----------------|------------------|
| prefix cache 命中率 | 90%+ | 从 0% 提升到可测量正值（如 30–50%） |
| OOM 频率 | 零 OOM | 显著降低， graceful degradation |
| wasted 错误 | 零 wasted | 不进入死循环，可控增长 |
| 单轮 token 量 | 压缩 80% | 在语义可逆前提下压缩 20–40% |

### 影响 4：避免用代理层解决"硬件问题"

代理层不能替代硬件升级。以下问题若主要由硬件导致，应标记为"受限优化"：

- 35B 模型在 48GB 上的绝对 prefill 速度上限
- rapid-mlx 后端本身的 OOM 阈值
- 模型对复杂指令的自我纠正能力

代理层的目标是"在约束内做到最好"，而不是"消除约束"。

### 影响 5：保留云模式作为效果对照

`./manage.sh start-cloud` 提供了一个天然 A/B 组：

- 若某痛点在云模式下显著减弱 → 说明该痛点主要由本地部署约束导致，代理层只需缓解。
- 若某痛点在云模式下依然存在 → 说明该痛点是代理层自身问题，应优先根治。

## 4.11 基于约束分析的方案重新评估

结合 `02-problems.md` 的约束分类，对 4.2–4.7 的 6 个方案重新评估如下：

### 方案 A：Cache Aligner 化 —— 保持 P0，最高优先级

| 维度 | 评估 |
|------|------|
| 约束关联 | 主要解决痛点 1（直接由约束导致） |
| 为何不依赖硬件 | 通过稳定消息顺序提升 prefix cache 命中，不增加内存，只改变排列方式 |
| 本地收益 | 最高：当前 prefix cache 命中率为 0%，任何提升都直接减少 prefill 计算 |
| cloud 收益 | 边际：cloud prefill 成本低，但可保持开启作为一致性策略 |
| 风险 | 低；若实现错误可立即关闭 |
| 结论 | **优先落地**，作为第一阶段核心任务 |

### 方案 B：语义保留压缩 —— 保持 P1，但拆分子项

| 子项 | 优先级 | 约束影响说明 |
|------|--------|--------------|
| JSON 结构化摘要 | P1 | 降低 token，保留语义，适合本地 |
| 代码空白/注释消除 | P1 | 低风险，直接减少 tool_result token |
| 日志聚合 | P1 | 对 Bash 返回的大型日志效果显著 |
| HTML 剥离 | P2 | 收益场景有限 |
| Schema 字段精简 | P1 | 直接减少 40+ 工具定义的 token |
| LLM-based 内容摘要 | **不推荐** | 增加本地计算负担，破坏 prefix cache 稳定性 |

**调整**：将"LLM-based 内容摘要"从方案 B 中移除，明确列为不推荐。

### 方案 C：循环与异常行为防御 —— 保持 P1，但定位为"代偿"

| 维度 | 评估 |
|------|------|
| 约束关联 | 主要解决痛点 2、3（被约束显著放大） |
| 为何不根治 | 根本原因是本地模型理解力不足，代理层只能检测和干预 |
| 实施原则 | 多层防御 + 强制换工具建议，但不过度干预正常多步操作 |
| cloud 模式 | 可大幅降低阈值或关闭部分层 |
| 结论 | **必须做**，但要明确这是"代偿"而非"根治" |

### 方案 D：资源与并发护栏 —— 从 P2 调整为"与 P1 并行的小项"

| 维度 | 评估 |
|------|------|
| 约束关联 | 直接应对痛点 4 |
| 边界 | 只能在 48GB + 35B 的硬边界内做缓冲，无法突破 |
| 关键改动 | 动态并发、内存预警、输出限制 |
| 优先级 | 不如 A 收益高，但实现成本低，可与 A 并行 |
| 结论 | **保持 P2 但提前到第一阶段后半段实施** |

### 方案 E：可观测性增强 —— 从 P2 提升为 P1

| 维度 | 评估 |
|------|------|
| 约束关联 | 痛点 7 与硬件无关，但本地调试成本高使其更难发现 |
| 为何提升 | 没有正确指标，无法验证 A/B/C/D 的效果 |
| 关键改动 | 修复 `re_read_rate`、新增 `common_prefix_ratio`、请求快照 |
| 结论 | **必须先做**，作为所有优化的测量基础 |

### 方案 F：配置治理 —— 保持 P3，但前置部分工作

| 维度 | 评估 |
|------|------|
| 约束关联 | 痛点 8 因资源紧张而参数爆炸 |
| 前置工作 | 第一阶段即定义 `local-balanced` / `cloud` 两套基础模板 |
| 完整治理 | 等 A/B/E 落地后再做参数分层和自动校验 |
| 结论 | **先轻量模板，后完整治理** |

## 4.12 调整后的优先级与阶段映射

| 优先级 | 方案 | 约束定位 | 落地阶段 |
|--------|------|----------|----------|
| **P0** | E（可观测性修复） | 无关/基础 | 第一阶段前半段 |
| **P0** | A（Cache Aligner） | 缓解约束症状 | 第一阶段核心 |
| **P1** | B（语义保留压缩） | 缓解约束症状 + 代偿模型不足 | 第二阶段 |
| **P1** | C（循环防御） | 代偿模型不足 | 第二阶段 |
| **P1** | D（资源护栏） | 缓冲约束边界 | 第一阶段后半段到第二阶段 |
| **P2** | F 模板部分（local/cloud 模板） | 治理复杂度 | 第一阶段 |
| **P3** | F 完整治理（参数分层/校验） | 治理复杂度 | 第四阶段 |

## 4.13 更新后的实施 checklist

每新增一个优化功能时，必须回答：

1. 该功能主要解决哪类约束关联问题？（直接约束 / 被放大 / 独立）
2. 该功能在 local 和 cloud 模式下是否都需要？默认是否应不同？
3. 该功能是否增加额外计算/内存开销？是否在 48GB 边界内可承受？
4. 该功能是否破坏 prefix cache 稳定性？
5. 该功能是否有 `PROXY_*_ENABLED` 开关和可逆回退路径？
6. 该功能是否已在 metrics 中定义基线和验收指标？
7. 该功能是否已在 cloud 模式下作为对照验证过必要性？

## 4.14 TokenSieve 借鉴点与本系统痛点的落地映射

TokenSieve 针对 AWS CLI 大型 JSON 输出的压缩管线（scrubber → JSON gate → sieve → deduper → router/pvfn → auditor）与本系统痛点高度重合，尤其是 **痛点 2（tool result 清除导致语义丢失）**、**痛点 4（后端资源约束）** 和 **痛点 7（可观测性）**。以下是可直接落地的借鉴点。

### 4.14.1 落地借鉴点一览

| TokenSieve 模块 | 本系统可落地能力 | 解决的痛点 | 落地难度 | 优先级 |
|-----------------|------------------|------------|----------|--------|
| `scrubber` | 去除 Bash/Read 输出中的 ANSI 颜色码、不可见字符 | 2、4、6 | 低 | P1 |
| `JSON gate` | 自动识别 tool_result 是否为 JSON | 2、4 | 低 | P1 |
| `sieve` | JSON 数组结构化摘要（保留 key、截断 value、前 N 项 + 计数） | 2、4 | 中 | P1 |
| `deduper` | 标量值首次出现后去重（first-seen-wins） | 2、4 | 中 | P2 |
| `router` / `pvfn` | 按输出类型选择压缩策略（JSON/代码/日志/纯文本） | 2、4、8 | 中 | P1 |
| `auditor` | 压缩后语义校验，失败则回退原样 | 2、4、7 | 低 | P1 |

### 4.14.2 具体落地设计

#### 1) `scrubber`：ANSI 与不可见字符清洗

**场景**：Bash 命令常返回带颜色码的输出（如 `ls --color=auto`、`npm test`），这些 ANSI 转义序列在 tool_result 中占用 token 且对模型无意义。

**落地实现**：

```python
import re

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

def _scrub_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)
```

**接入位置**：`anthropic_proxy.py` 中处理 tool_result 内容时，或作为 `_compress_content_pass` 的第一步。

**开关**：`PROXY_SCRUB_ANSI=true`（默认开启，风险极低）。

**收益**：降低 token 量，减少兼容性摩擦（痛点 6）。

---

#### 2) `JSON gate`：JSON 类型自动识别

**场景**：`Read`/`Bash` 返回的内容有时是 JSON，有时是代码、日志或自然语言。需要先识别类型，再选择压缩策略。

**落地实现**：

```python
def _detect_content_type(text: str) -> str:
    stripped = text.strip()
    if (stripped.startswith('{') and stripped.endswith('}')) or \
       (stripped.startswith('[') and stripped.endswith(']')):
        try:
            json.loads(stripped)
            return 'json'
        except Exception:
            pass
    if '\n' in text and any(line.startswith('20') for line in text.split('\n')[:3]):
        return 'log'
    if text.count('def ') + text.count('class ') > 2:
        return 'code'
    return 'text'
```

**接入位置**：新增 `_compress_tool_result(content, mime_hint=None)` 统一入口。

**收益**：为 router 提供决策输入，避免对非 JSON 内容错误应用 JSON 摘要。

---

#### 3) `sieve`：JSON 结构化摘要

**场景**：AWS CLI 式的大型 JSON 数组、API 返回、数据库查询结果、日志聚合等。直接塞进上下文会瞬间占满窗口。

**落地实现**（标准库实现）：

```python
def _sieve_json(obj, max_items=10, max_str_len=200, max_depth=4):
    """保留结构，截断重复/过长内容。"""
    if isinstance(obj, str):
        if len(obj) > max_str_len:
            return obj[:max_str_len] + f'...[truncated {len(obj)-max_str_len} chars]'
        return obj
    if isinstance(obj, list):
        if len(obj) > max_items:
            summarized = [_sieve_json(item) for item in obj[:max_items]]
            return summarized + [f'...({len(obj)-max_items} more items)']
        return [_sieve_json(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _sieve_json(v) for k, v in obj.items()}
    return obj
```

**关键原则**：
- 保留所有 key（模型需要知道有什么字段）。
- 截断过长的字符串值，但保留前 N 个字符。
- 数组保留前 N 项 + 总数提示。
- 嵌套层级过深时截断。

**接入位置**：`_compress_content_pass` 中，仅对 `tool_result` 类型内容启用。

**开关**：`PROXY_SIEVE_JSON_ENABLED=true`，阈值 `PROXY_SIEVE_JSON_THRESHOLD=4096`。

**收益**：替代粗暴 clearing，降低 re-read 风险（痛点 2），减少 token 量（痛点 4）。

---

#### 4) `deduper`：标量值首次出现后去重

**场景**：JSON 数组中大量重复值（如状态码全部为 `"available"`、区域名重复出现），首次出现后可用占位符替代。

**落地实现**：

```python
def _dedupe_scalars(obj, seen=None):
    if seen is None:
        seen = {}
    if isinstance(obj, str):
        if obj in seen:
            return f'###(repeated: {seen[obj]})'
        if len(obj) > 20:  # 只缓存长值
            seen[obj] = len(seen) + 1
        return obj
    if isinstance(obj, list):
        return [_dedupe_scalars(item, seen) for item in obj]
    if isinstance(obj, dict):
        return {k: _dedupe_scalars(v, seen) for k, v in obj.items()}
    return obj
```

**注意**：代码场景中需谨慎使用，避免将合法重复字符串（如 `"return"`）替换为占位符。建议：
- 仅对值长度 > 20 的字符串去重。
- 不对代码类型内容启用。
- 默认关闭，验证后再开启。

**开关**：`PROXY_DEDUPE_SCALARS=false`（默认关闭）。

---

#### 5) `router` / `pvfn`：按输出类型选择策略

**场景**：不同 tool_result 需要不同压缩强度：
- JSON 数组 → sieve 结构化摘要
- 代码文件 → 删除空白/注释
- 日志 → 聚合重复行、保留错误行
- 自然语言 → 仅截断过长段落

**落地实现**：

```python
_COMPRESS_STRATEGIES = {
    'json': _sieve_json,
    'code': _compress_code,
    'log': _compress_log,
    'text': _compress_text,
}

def _compress_tool_result(content: str, mime_hint: str = None) -> str:
    content_type = mime_hint or _detect_content_type(content)
    strategy = _COMPRESS_STRATEGIES.get(content_type, _compress_text)
    return strategy(content)
```

**接入位置**：替换当前 `_compress_content_pass` 中简单粗暴的占位符逻辑。

**开关**：`PROXY_COMPRESS_MODE=lossless|semantic|aggressive`。

**收益**：避免一刀切清除，保留关键语义，降低循环风险（痛点 2、3）。

---

#### 6) `auditor`：压缩后语义校验

**场景**：压缩 JSON 后可能意外破坏语法，导致模型后续解析失败；或代码压缩后丢失关键符号。

**落地实现**：

```python
def _audit_compression(original: str, compressed: str, content_type: str) -> bool:
    if content_type == 'json':
        try:
            json.loads(compressed)
            return True
        except Exception:
            return False
    if content_type == 'code':
        # 简单校验：括号平衡、引号闭合
        return _check_code_balance(compressed)
    # text/log：默认通过，或检查关键错误关键词是否保留
    return True
```

**接入位置**：压缩后立即调用，失败则回退到原内容，并记录 metrics。

**收益**：保证压缩不会引入新错误（痛点 2、4、7）。

### 4.14.3 与现有代理能力的衔接

| 现有功能 | 衔接方式 |
|----------|----------|
| `_compress_content_pass` | 将粗暴 clearing 替换为 router + sieve/scrubber/auditor |
| `_filter_tools` | 对 JSON Schema 做 TokenSieve 式字段精简（类似 schema_optimizer） |
| `truncate_messages_if_needed` | 压缩后单轮 token 下降，可减少截断频率，间接保护 prefix cache |
| `logs/proxy_metrics.jsonl` | 记录 `compress_ratio`、`content_type`、`audit_pass` 等指标 |
| `_detect_blocker_pattern` | 对压缩后的 re-read 行为继续监控，防止新策略引入循环 |

### 4.14.4 落地阶段建议

将 TokenSieve 借鉴点拆入现有阶段：

| 任务 | 所属阶段 | 优先级 |
|------|----------|--------|
| ANSI scrubber + JSON gate | 第一阶段（与 Cache Aligner 并行） | P1 |
| JSON sieve + auditor | 第二阶段（语义保留压缩核心） | P1 |
| Router / 输出类型策略 | 第二阶段 | P1 |
| 标量 deduper | 第二阶段后半段或第三阶段 | P2 |
| metrics 字段 `compress_ratio`/`content_type`/`audit_pass` | 第一阶段（可观测性） | P1 |

### 4.14.5 风险与缓解

| 风险 | 缓解 |
|------|------|
| JSON 摘要破坏语义 | auditor 校验，失败回退 |
| 去重误伤代码 | 仅对长字符串、非代码类型启用 |
| 类型识别错误 | 允许 MIME hint 覆盖，默认保守策略 |
| 压缩引入延迟 | 仅对超长内容启用，小请求跳过 |
| 模型不理解摘要格式 | 添加前缀说明（如 `[JSON summarized: 10/100 items]`） |
