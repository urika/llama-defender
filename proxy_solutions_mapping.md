# 代理层可解痛点与 Kompact/TokenSieve 改进方案映射

> 承接 `proxy_pain_points_analysis.md`，进一步回答：哪些痛点可以通过代理层解决？参考 Kompact 和 TokenSieve 的源码，可以推导出哪些具体可落地的改进方案？

---

## 一、判断原则：什么是"代理层可解"？

代理层位于 **Client（Claude Code）** 与 **Backend（rapid-mlx/llama-server/Cloud API）** 之间，能够：

| 能力 | 说明 |
|---|---|
| ✅ 修改请求体 | 改写 messages、tools、system prompt、参数 |
| ✅ 修改响应体 | 修复/转换后端返回的异常内容 |
| ✅ 做本地计算 | 文本分析、压缩、去重、哈希、评分 |
| ✅ 维护跨请求状态 | session 级别的循环计数、缓存元数据 |
| ✅ 路由与降级 | 错误分类、重试、 fallback |
| ❌ 改变后端内部行为 | 如 KV cache layout、max_tokens 实现、chat template 渲染 |
| ❌ 突破硬件物理限制 | 48GB 内存上限、Metal 显存池 |

**核心判断标准**：凡是不依赖后端内部改造、不突破硬件限制、仅通过"改写/过滤/压缩/状态跟踪"就能缓解的问题，都属于代理层可解。

---

## 二、八大痛点 vs 代理层可解性

| 痛点 | 代理层可解性 | 可解程度 | 备注 |
|---|---|---|---|
| 痛点 1：上下文长度 vs prefix cache 命中率冲突 | **✅ 可解** | 高 | 通过稳定前缀、减少动态内容、对齐 system prompt 可显著改善 |
| 痛点 2：Tool result 清除导致语义损失与 re-read 死循环 | **✅ 可解** | 高 | 放弃删除，改用结构化压缩；保留 Read 结果 |
| 痛点 3：循环行为多样性与防御军备竞赛 | **✅ 可解** | 中-高 | 代理层已是主要防御位置；可减少诱因而非只加规则 |
| 痛点 4：后端资源约束（OOM / 性能衰减） | **⚠️ 部分可解** | 中 | 代理可减少输入规模、预截断、精确预算，但无法根治后端内存管理 |
| 痛点 5：工具过滤白名单困境 | **✅ 可解** | 高 | 代理直接控制发送哪些 tool definitions |
| 痛点 6：客户端与后端兼容性摩擦 | **✅ 可解** | 高 | 代理承担协议翻译和格式归一化 |
| 痛点 7：可观测性不足 | **✅ 可解** | 高 | 代理是最佳观测点，可输出结构化指标 |
| 痛点 8：配置复杂度与运维负担 | **✅ 可解** | 中 | 通过自适应默认值、配置分层、减少开关 |

**结论**：8 个痛点中，**6 个完全可解、1 个部分可解、0 个完全不可解**。只有痛点 4（后端资源约束）需要代理与后端配置协同解决。

---

## 三、代理层可解方案详述

### 3.1 痛点 1：上下文长度 vs prefix cache 命中率冲突

#### 代理层可做什么？
代理是唯一能"在请求到达后端前稳定前缀"的层级。

#### 从 Kompact 学到的方案：Cache Aligner

**Kompact 实现**（`src/kompact/transforms/cache_aligner.py`）：
- 识别 UUID、时间戳、用户路径等动态值。
- 用 `{UUID_0}`、`{TS_0}`、`{PATH_0}` 等占位符替换。
- 作用于 system prompt 和前 1–2 条 user 消息。

**落地到当前 proxy**：

```python
# 新增配置
PROXY_CACHE_ALIGN_ENABLED = os.environ.get("PROXY_CACHE_ALIGN_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_CACHE_ALIGN_PATTERNS = {
    "uuid": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I),
    "timestamp": re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
    "unix_ts": re.compile(r"\b1[6-9]\d{8}\b"),
    "user_path": re.compile(r"(?:/Users/|/home/|/tmp/)[\w./-]+"),
}

# 在 _handle_messages 早期对 request.system 和前 2 条 user 消息应用
```

**预期收益**：
- system prompt 中动态值（如 session ID、当前时间、工作目录）被占位符化。
- 相同 system prompt 的多次请求前缀完全一致，prefix cache 命中率提升。
- 对本地后端，减少重复 prefill 计算。

**额外建议**：
- 将 `The date has changed...` 等动态 system reminder 改为尾部追加，而非插入中间。
- 对 fifo/rounds 策略，保持头部 system 消息绝对稳定。

---

### 3.2 痛点 2：Tool Result 清除导致语义损失与 re-read 死循环

#### 代理层可做什么？
代理可以决定如何压缩/保留 tool_result 内容，而不必直接删除。

#### 从 TokenSieve 学到的方案：结构化压缩替代删除

**TokenSieve 实现**（`src/sieve.rs` + `src/deduper.rs`）：
1. **Sieve**：删除 null、空字符串、空数组、空对象；将 ≥200 字符 base64 替换为 `<base64 N chars>`。
2. **Deduper**：删除大于 10¹² 的整数（epoch 时间戳）；同文档内重复标量值 first-seen-wins 去重。
3. **Router**：同质对象数组转为 Schema-YAML，消除 key 重复。

**落地到当前 proxy**：

```python
def _compress_tool_result_json(text: str) -> str:
    """对 tool_result 中的 JSON 进行 TokenSieve 式结构化压缩。"""
    try:
        data = json.loads(text)
        pruned = _sieve_prune(data)      # 删除空值，base64 占位
        deduped = _dedupe_scalars(pruned) # 时间戳剥离，重复标量去重
        # 对同质对象数组尝试 Schema-YAML，否则 minify
        return _route_format(deduped)
    except json.JSONDecodeError:
        return text
```

**与当前 `_compress_content_pass` 的关系**：
- 当前逻辑：对超出保留数量的 tool_result，替换为 `[cleared: ...]` 或保留 200 字符 preview。
- 改进逻辑：**不删除**，而是对内容做结构化压缩；只删除真正无信息的字段。

**关键约束**：
- **Read 返回的代码文件**：保持完整，不做压缩（当前 smart truncation 已保证）。
- **Bash 返回的 JSON/日志**：启用 sieve + deduper，可大幅压缩。
- **WebFetch 返回的 HTML**：可引入 Kompact HTML Stripper 去除 nav/chrome。

**预期收益**：
- 保留完整语义，避免 re-read 死循环。
- 对 AWS/CLI 类 JSON 输出可节省 30–50% token。
- base64 证书/图片数据从 ~800 token 降到 4 token。

---

### 3.3 痛点 3：循环行为多样性与防御军备竞赛

#### 代理层可做什么？
代理可以检测异常模式并注入干预消息，这是当前已经做的。

#### 改进方向：从"检测循环"转向"减少诱因"

**当前做法**：
- Loop detection（L1/L2/L3）
- Blocker detection
- Text loop detection

**参考 Kompact/TokenSieve 的优化**：

| 循环类型 | 根因 | 代理层可减少诱因的方案 |
|---|---|---|
| Read 死循环 | 文件内容被清除 | 不再清除，结构化压缩替代 |
| Write 认知循环 | 模型看不到已写入内容摘要 | 在 tool_result 中附加内容摘要（前 500 字符 + 关键结构） |
| Bash 重复命令 | 已清空内容触发相似度 | 压缩时不使用 `[cleared:...]` 占位符 |
| 文本输出循环 | 模型重复生成 | 继续 text loop detection，可结合 TokenSieve 的摘要思想 |

**新增建议**：
- **Write 结果摘要化**：当 Write 返回 `updated successfully` 时，代理可在 tool_result 中追加写入文件的前 500 字符摘要，帮助模型维持记忆。
- **跨请求循环追踪强化**：当 session 累计 loop_detected > N 次且针对同一文件/工具时，直接修改该 tool 的可用性（短期屏蔽）。

---

### 3.4 痛点 4：后端资源约束（OOM / 性能衰减）

#### 代理层可做什么？（部分）

| 可解部分 | 不可解部分 |
|---|---|
| 预截断超大请求 | 后端 KV cache 内部内存管理 |
| 更精确的 token 估算 | Metal 显存池大小 |
| 压缩输入减少 prefill 量 | rapid-mlx 本身的性能衰减 bug |
| 503 + Retry-After 优雅降级 | GPU 硬件限制 |

#### 从 Kompact/TokenSieve 学到的方案

**Kompact**：
- 8 层 transforms 综合压缩（TOON、JSON Crusher、Code Compressor、Log Compressor 等）。
- 自适应参数：根据总 token 数调整压缩强度。

**TokenSieve**：
- 6-stage pipeline 将 CLI JSON 输出压缩 47%。

**落地到当前 proxy**：

```python
# 在 PROXY_PRE_TRUNCATE_CHARS 之前增加一层结构化压缩
# 如果压缩后仍超过阈值，再执行 FIFO 截断
```

**预期收益**：
- 减少进入后端 prefill 的 token 数。
- 降低 KV cache 峰值占用。
- 但无法完全避免 OOM，仍需 `PROXY_OOM_SAFE_TOKENS` 兜底。

---

### 3.5 痛点 5：工具过滤白名单困境

#### 代理层可做什么？
代理直接控制发送给后端的 tool definitions 列表。

#### 从 Kompact 学到的方案：TF-IDF Schema Optimizer

**Kompact 实现**（`src/kompact/transforms/schema_optimizer.py`）：
- 从最近 5 条消息提取 query。
- 对每个 tool 的 name/description/parameters 做 TF-IDF 评分。
- 保留 top-K 工具，并强制保留最近使用过的工具。

**落地到当前 proxy**：

```python
def _filter_tools_dynamic(tools, messages, recent_rounds=5, max_tools=20):
    # 1. 保留白名单 + 最近使用 + tool_choice_name（兼容当前逻辑）
    keep_set = TOOL_ALWAYS_KEEP | _get_recent_tools(messages, recent_rounds)
    if tool_choice_name:
        keep_set.add(tool_choice_name)
    
    # 2. 如果仍超过 max_tools，用 TF-IDF 对剩余工具排序
    if len(keep_set) < max_tools:
        query = _extract_query_text(messages[-5:])
        scored = _tfidf_score_tools(tools, query)
        remaining_slots = max_tools - len(keep_set)
        extra = [t for t, score in scored if t.name not in keep_set][:remaining_slots]
        keep_set.update(t.name for t in extra)
    
    return [t for t in tools if t.name in keep_set]
```

**与当前逻辑的差异**：
- 当前：固定白名单 + 最近使用 + 补齐到 20。
- 改进：在白名单基础上，对剩余工具按当前 query 动态排序。

**预期收益**：
- 新工具不需要手动加白名单，首次使用即可能入选。
- 长会话中工具集随任务焦点动态变化。
- 保持 prefix cache 稳定（结果按名字母排序）。

**风险与缓解**：
- 风险：误删关键工具。
- 缓解：保留白名单兜底；对 TF-IDF 选中的工具记录到 metrics；默认关闭，灰度验证。

---

### 3.6 痛点 6：客户端与后端兼容性摩擦

#### 代理层可做什么？
代理是协议翻译层，可以做大量归一化工作。

#### 可落地的代理层改进

| 兼容性问题 | 当前状态 | 代理层可加强方案 |
|---|---|---|
| `mid-conversation-system` 导致 chat template 报错 | 已修复（替换 template） | 代理层增加 system message 重排序：提取所有 system 消息前置 |
| `developer` role 不被后端识别 | 部分处理 | 代理层统一将 `developer` 映射为 `system` |
| `max_tokens` 被 rapid-mlx 忽略 | 已部分缓解（非流式 JSON 修复） | 流式路径也增加截断后 JSON 修复；或向后端升级施压 |
| 空 SSE 流 / 后端异常 | 已分类错误 | 增加更细粒度的错误翻译与客户端提示 |

**参考**：TokenSieve 的 scrubber 在 JSON 解析前先去除 ANSI 转义码，避免解析失败。代理层可以借鉴这种"预处理归一化"思想：

```python
# 在 Anthropic → OpenAI 转换前，对 message content 做归一化
def _normalize_content(content):
    # 1. 去除 ANSI 转义码（TokenSieve 思想）
    # 2. 合并连续的 system 消息
    # 3. 将 developer role 转为 system
    # 4. 对超长单个 text block 做软截断提示
    pass
```

---

### 3.7 痛点 7：可观测性不足

#### 代理层可做什么？
代理是每个请求的必经之地，天然是最佳观测点。

#### 从 Kompact/TokenSieve 学到的方案

**Kompact**：
- `proxy_metrics.jsonl` 记录每个 transform 的 tokens_saved。
- Dashboard 展示 per-transform breakdown、recent requests、compression ratio。

**TokenSieve**：
- 每条响应 stderr 输出 `[TokenSieve] Original: X tok | Compressed: Y tok | Saved: Z (P%) | Shape: S`。
- 明确告诉用户/运维发生了什么。

**落地到当前 proxy**：

```python
# 在 proxy_metrics.jsonl 中增加 compression 字段
{
    "compression": {
        "transforms": [
            {"name": "cache_aligner", "tokens_saved": 120},
            {"name": "json_compress", "tokens_saved": 3400},
            {"name": "base64_scrub", "tokens_saved": 800},
        ],
        "total_saved": 4320,
        "compression_ratio": 0.18
    }
}

# stderr receipt（类似 TokenSieve）
# [Proxy] Original: 24000 tok | Compressed: 19680 tok | Saved: 4320 (18.0%) | Transforms: cache_aligner,json_compress
```

**新增指标建议**：
- `prefix_stable_chars`：system prompt + 头部稳定消息字符数。
- `dynamic_value_count`：Cache Aligner 替换的动态值数量。
- `tool_result_compressed_bytes`：JSON 压缩前后字节数。
- `schema_optimizer_filtered`：被过滤的工具名列表。

---

### 3.8 痛点 8：配置复杂度与运维负担

#### 代理层可做什么？
通过自适应默认值和配置分层减少人工调参。

#### 改进方案

| 当前问题 | 改进方案 |
|---|---|
| 31+ env vars | 合并相关参数为配置 profile（如 `aggressive`/`balanced`/`conservative`） |
| `char`/`rounds`/`fifo`/`smart` 选择困难 | 默认 `smart`，根据后端模式自动调整参数 |
| 参数隐性耦合 | 文档化参数依赖矩阵；代码中增加配置校验 |
| 新功能默认行为不确定 | 所有压缩类功能默认关闭，通过 `PROXY_EXPERIMENTAL_*` 显式开启 |

**参考 Kompact**：
- Kompact 使用 `KompactConfig` dataclass，有清晰的默认值和层级。
- 每个 transform 独立 `enabled` 开关，可单独禁用。

**落地建议**：

```python
# 引入配置 profile
PROXY_COMPRESSION_PROFILE = os.environ.get("PROXY_COMPRESSION_PROFILE", "balanced")
# balanced: cache_aligner + json_compress (safe)
# aggressive: + base64_scrub + dedupe + schema_optimizer
# conservative: 仅 cache_aligner
```

---

## 四、不可解或仅部分可解的问题边界

### 4.1 后端内部行为（代理无法直接解决）

| 问题 | 说明 | 代理层能做的 |
|---|---|---|
| rapid-mlx 忽略 max_tokens | 后端 bug | 流式/非流式路径做 JSON 修复；推动升级 v0.6.71+ |
| TurboQuant 破坏 cache persist | `TurboQuantKVCache` 无 `state` 属性 | AGENTS.md 明确警告；代理无法修复后端类 |
| KV cache layout 内存占用 | 后端实现细节 | 通过压缩输入间接减少 KV；无法优化 layout |
| Metal 显存池大小 | macOS 硬件/驱动限制 | 监控 + 预截断 + 降配；无法突破物理上限 |

### 4.2 客户端行为（代理只能适应，不能改变）

| 问题 | 说明 | 代理层能做的 |
|---|---|---|
| Claude Code 注入 mid-conversation system | 客户端 beta 特性 | 归一化 system 消息位置 |
| Claude Code 发送重复 POST | 客户端重试机制 | 请求去重（已实现 `_check_dedup`） |
| Claude Code 子代理工具集差异 | 客户端 agent 架构 | 后端看到的 tool 列表由客户端决定，代理只能过滤 |

---

## 五、推荐落地路线图

基于"可解性高、风险低、收益明显"的原则，建议按以下顺序实施：

### Phase 1：前缀稳定化（1–2 周）
- **目标**：痛点 1
- **方案**：引入 Kompact Cache Aligner
- **关键配置**：`PROXY_CACHE_ALIGN_ENABLED=true`
- **验证指标**：prefix cache 命中率、相同 system prompt 的 TTFT

### Phase 2：Tool Result 结构化压缩（2–3 周）
- **目标**：痛点 2、痛点 4（部分）
- **方案**：TokenSieve 式 sieve + deduper + Schema-YAML/PVFN
- **关键配置**：`PROXY_TOOL_RESULT_COMPRESS_ENABLED=true`
- **验证指标**：tool_result token 数、re_read_rate、wasted 错误数

### Phase 3：工具动态选择（3–4 周）
- **目标**：痛点 5
- **方案**：Kompact TF-IDF Schema Optimizer 增强当前 `_filter_tools`
- **关键配置**：`PROXY_TOOL_FILTER_DYNAMIC=true`
- **验证指标**：filtered_out 列表、工具调用失败率、任务完成率

### Phase 4：可观测性增强（持续）
- **目标**：痛点 7
- **方案**：per-transform metrics + TokenSieve 式 receipt
- **验证指标**：metrics 完整性、问题定位时间

### Phase 5：配置简化（长期）
- **目标**：痛点 8
- **方案**：配置 profile、自适应默认值、参数依赖校验
- **验证指标**：配置项数量、新用户上手时间

---

## 六、方案与源码对应关系

| 方案 | 参考源码 | 当前 proxy 插入位置 |
|---|---|---|
| Cache Aligner | `kompact/src/kompact/transforms/cache_aligner.py` | `_handle_messages` 入口， Anthropic → OpenAI 转换前 |
| JSON Sieve/Dedupe | `tokensieve/src/deduper.rs`, `tokensieve/src/sieve.rs` | `_compress_content_pass` 中，对非 Read tool_result 启用 |
| Schema-YAML/PVFN | `tokensieve/src/router.rs`, `tokensieve/src/pvfn.rs` | `_compress_content_pass` 后端输出格式化阶段 |
| HTML Stripper | `kompact/src/kompact/transforms/html_stripper.py` | WebFetch tool_result 处理分支 |
| TF-IDF Tool Filter | `kompact/src/kompact/transforms/schema_optimizer.py` | 替换/增强 `_filter_tools` |
| Per-Transform Metrics | `kompact/src/kompact/metrics/tracker.py` | `proxy_metrics.jsonl` 写入点 |
| Token Receipt | `tokensieve/src/auditor.rs` | stderr 或 metrics 中输出 |

---

## 七、风险与缓解汇总

| 方案 | 主要风险 | 缓解措施 |
|---|---|---|
| Cache Aligner | 模型不理解 `{UUID_0}` 占位符 | 仅替换 system/早期 user 消息；保留映射表用于 debug |
| JSON 压缩 | 误删代码文件中的关键字段 | 对 Read 代码文件结果禁用；保留字段类型元数据 |
| Schema-YAML | 模型不适应 path=value 格式 | 默认对同质数组启用；A/B 测试验证 |
| TF-IDF Tool Filter | 误删关键工具 | 白名单兜底；默认关闭；记录 filtered_out |
| HTML Stripper | 误删正文内容 | 仅对 WebFetch 结果启用；配置 nav_link_ratio 阈值 |
| 配置 Profile | 隐藏参数导致调试困难 | 保留所有底层开关；profile 只是预设组合 |

---

## 八、结论

1. **代理层可以解决当前 8 个痛点中的 7 个**，只有后端资源约束中的"后端内部内存管理"部分需要后端升级配合。

2. **Kompact 和 TokenSieve 提供了经过验证的具体实现**：
   - Kompact 的 **Cache Aligner** 直接对应痛点 1。
   - TokenSieve 的 **Sieve + Deduper + Router** 直接对应痛点 2 和痛点 4。
   - Kompact 的 **Schema Optimizer** 对应痛点 5。
   - 两者的 **metrics/receipt 机制** 对应痛点 7。

3. **推荐的最小可行路径**：
   - 先做 **Cache Aligner**（低风险、长期收益）。
   - 再做 **Tool Result JSON 结构化压缩**（直接避免 re-read 死循环）。
   - 最后视情况引入 **TF-IDF Tool Filter** 和可观测性增强。

4. **核心原则**：
   > 不要删除信息，要压缩信息；不要增加新规则，要减少诱因；不要追求最大压缩率，要在质量、稳定、cache 命中之间找到平衡。

---

*文档路径：/Users/jinsongwang/APP/llama.cpp/proxy_solutions_mapping.md*
