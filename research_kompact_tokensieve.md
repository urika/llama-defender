# Kompact + TokenSieve 源码研究笔记

## 仓库位置

已 clone 到本地：

- `/Users/jinsongwang/APP/research/kompact` （Python，FastAPI 透明代理）
- `/Users/jinsongwang/APP/research/tokensieve` （Rust，CLI 输出压缩）

---

## 一、Kompact：8 层变换管线

### 1.1 项目定位
Kompact 是一个位于 Agent 与 LLM Provider 之间的透明 HTTP 代理，监听默认端口 `7878`。它拦截 Anthropic / OpenAI 兼容请求，对上下文做多层压缩后再转发，声称可节省 40–70% 的 token，同时在 BFCL 等基准上保持质量损失 < 4%。

### 1.2 核心入口
- CLI：`src/kompact/__main__.py`
- 代理服务器：`src/kompact/proxy/server.py`
- 变换编排：`src/kompact/transforms/pipeline.py`
- 配置：`src/kompact/config.py`
- 类型系统：`src/kompact/types.py`
- 协议转换：`src/kompact/parser/messages.py`

### 1.3 8 个 Transform 与执行顺序

| 层 | Transform | 文件 | 作用 | 典型节省 |
|---|---|---|---|---|
| L1 | Schema Optimizer | `schema_optimizer.py` | 基于 TF-IDF 动态选择相关 tool 定义 | 50–90% tool defs |
| L2 | TOON | `toon.py` | 将 JSON 数组/对象转为紧凑表格/函数签名 | 30–60% JSON |
| L2 | JSON Crusher | `json_crusher.py` | 常量字段提取、低基数枚举、异常保留 | 40–80% 结构化 JSON |
| L2 | Code Compressor | `code_compressor.py` | 保留 Python 代码骨架，删除函数体 | ~70% 代码 |
| L2 | Log Compressor | `log_compressor.py` | 重复日志行折叠为 `[repeated N times]` | 60–90% 日志 |
| L2b | HTML Stripper | `html_stripper.py` | 去除 WebFetch 导航/页脚/链接列表 | ~88% nav overhead |
| L2c | Content Compressor | `content_compressor.py` | 基于 TF-IDF 的抽取式文本压缩 | 可调 target_ratio |
| L3 | Observation Masker | `observation_masker.py` | 旧 tool_result 替换为占位符，保留最近 N 条 | ~50% 历史 |
| L4 | Cache Aligner | `cache_aligner.py` | 将 UUID/时间戳/路径替换为占位符以提升 prefix cache 命中 | 间接节省 |

> 注：README 称 “8 transforms”，但 `pipeline.py` 实际编排了 9 个独立模块（含 `html_stripper`）。

### 1.4 自适应参数 (`_adapt_params`)
```python
tokens < 500          → 跳过 content_compressor
tokens < 20K          → target_ratio 0.75, keep_last_n = max(5, n//3)
tokens < 100K         → target_ratio 0.60, keep_last_n = max(4, n//4)
tokens >= 100K        → target_ratio 0.45, keep_last_n = max(3, n//5)
```

### 1.5 Cache Aligner 关键源码

文件：`src/kompact/transforms/cache_aligner.py`

**策略**：把系统 prompt 和前 2 条 user 消息中的动态值抽取成稳定占位符，使 prefix cache 在多次请求间保持相同前缀。

支持的正则模式：
- UUID：`[0-9a-f]{8}-...`
- 时间戳：`YYYY-MM-DDTHH:MM:SS...`
- Unix 时间戳：`1[6-9]\d{8}`
- 用户路径：`(?:/[\w.-]+){3,}` 且包含 `/Users/`、`/home/`、`/tmp/`

```python
def transform(messages, config, system_prompt=""):
    aligned_system, dynamic_values = _extract_dynamic(system_prompt, config)
    for i, msg in enumerate(messages):
        if msg.role == Role.SYSTEM or (msg.role == Role.USER and i < 2):
            # 替换动态值为 {UUID_0}, {TS_0}, {PATH_0} 等占位符
```

**价值**：Anthropic cached input 折扣 90%，OpenAI 50%。通过把变化量推到尾部，稳定前缀可被重复命中。

### 1.6 Schema Optimizer 关键源码

文件：`src/kompact/transforms/schema_optimizer.py`

**策略**：当 tool 数量超过 `max_tools`（默认 10，但默认 `enabled=False`）时，用 TF-IDF 余弦相似度从最近 5 条消息中提取 query，对所有 tool 做相关性打分，保留 top-K，并强制保留最近使用过的 tool。

核心算法：
1. `_extract_query(messages[-5:])` 提取最近对话文本。
2. `_compute_idf(tools)` 在 tool 集合上计算 IDF。
3. `_tfidf_cosine(query_tf, doc_tf, idf)` 计算余弦相似度。
4. `_recent_usage_boost(tool, messages)` 最近 3 条消息里用过的 tool +0.5。
5. `_get_recently_used_tools` 强制保留最后一条 assistant 消息中出现的 tool。

**注意**：默认 `SchemaOptimizerConfig.enabled = False`，需要显式开启。

### 1.7 与当前项目的可借鉴点
当前 `anthropic_proxy.py` 已有自己的 tool filter（基于固定白名单 + 最近使用），Kompact 的 TF-IDF 方案可作为替代/补充，动态判断保留哪些 tool，尤其在 tool 数量 > 20 时减少 token。

---

## 二、TokenSieve：去重策略源码

### 2.1 项目定位
TokenSieve 通过 PATH shadowing（`~/.tokensieve/bin/aws` 等 symlink）拦截 `aws`、`kubectl`、`databricks` 等 CLI 调用，对其 JSON stdout 进行 6 阶段压缩，再返回给 Agent。

### 2.2 6 阶段管线

```text
[Scrubber] → [JSON Gate] → [Sieve] → [Deduper] → [Router] → [Handoff]
```

### 2.3 关键源码文件
- `src/deduper.rs` — 去重核心
- `src/sieve.rs` — 剪枝 + base64 占位
- `src/router.rs` — Schema-YAML / PVFN 格式选择
- `src/pvfn.rs` — Path-Value Flattened Notation
- `src/auditor.rs` — tiktoken cl100k_base 统计
- `src/main.rs` — 代理入口与管线编排

### 2.4 “file-block 指纹去重” 实际实现

**重要澄清**：TokenSieve 仓库中并未实现传统意义上的“file-block 指纹去重”（如 Rabin 分块 + MinHash/SimHash/SHA）。它的去重策略是 **First-Seen-Wins Scalar Deduplication**，可视为一种“值指纹”策略：用 JSON 标量的字符串化结果作为 key，首次出现保留，后续重复删除。

#### Deduper 两阶段算法（`src/deduper.rs`）

**Pass 1：Epoch Timestamp Stripping**
```rust
const EPOCH_MS_THRESHOLD: i64 = 1_000_000_000_000;
// 任何大于 10^12 的整数视为 Unix 毫秒时间戳，直接剔除
```

**Pass 2：First-Seen-Wins Deduplication**
```rust
fn strip_seen(value: Value, seen: &mut HashSet<String>) -> Option<Value> {
    match value {
        Value::Object(map) => {
            // 1. 键按字母序排序（确定性优先级）
            // 2. 先处理非数组字段，建立 seen-set
            // 3. 再处理数组字段，每个元素拿到父级 seen-set 的 snapshot
        }
        Value::Array(arr) => {
            // 嵌套数组：每个元素基于 snapshot 构建独立 seen-set
            // 兄弟元素间互不抑制，但父级已出现的值仍会被过滤
        }
        // 标量：用 v.to_string() 作为指纹，已存在则丢弃
    }
}
```

#### 作用域规则

| 上下文 | 作用域 | 说明 |
|---|---|---|
| 根数组 | 每个元素独立 seen-set | 防止跨资源抑制（如两个 cluster 的 region 相同，第二个仍保留） |
| 嵌套数组 | 每个元素继承父级 snapshot | `NI[0].SubnetId` 与 `NI[1].SubnetId` 都保留；但父级 `VpcId` 仍会被过滤 |
| 根对象 | 全局共享 seen-set | 单文档内重复标量只保留第一次 |

#### 双遍对象遍历的原因
```rust
let (non_arrays, arrays): (Vec<_>, Vec<_>) = entries
    .into_iter()
    .partition(|(_, v)| !matches!(v, Value::Array(_)));
// 先处理 scalar/object，让数组 snapshot 时包含全部父级标量
```
这样即使 `NetworkInterfaces` (N) 按字母序排在 `vpc_id` (v) 之前，数组元素也能看到 `vpc_id` 已存在。

### 2.5 Sieve 阶段（`src/sieve.rs`）
- 删除 `null`、空字符串、空数组、空对象（自底向上折叠）。
- base64 blob 检测：长度 ≥ 200、字符全在 base64 字母表、≥92% 字母数字 → 替换为 `<base64 N chars>`。

### 2.6 Router / PVFN 阶段（`src/router.rs` + `src/pvfn.rs`）
- **Schema-YAML**：根为同质对象数组且 fill ratio ≥ 55% 时使用，key 只发一次。
- **PVFN**：其余情况，用 `path=value` 扁平化，配合 `@map` 头部对高频长键做缩写（如 `NetworkInterfaces → NI`）。
- 密集子数组可在 PVFN 中内联 Schema-YAML 块。

### 2.7 与当前项目的可借鉴点
当前 `anthropic_proxy.py` 的 context truncator 更偏向“消息级”截断。TokenSieve 展示了 **JSON 结构化去重** 对 CLI tool_result 的巨大收益：
- 删除 null / 空值 / 时间戳 / base64  blobs。
- 同文档内重复标量值去重。
- 将 JSON 转为更紧凑的 Schema-YAML 或 PVFN。

如果 Agent 频繁调用 `Read`/`Bash` 读取 JSON/日志/代码，可在 proxy 中增加类似 TokenSieve 的 JSON 压缩阶段，进一步降低上下文增长。

---

## 三、关键对比

| 维度 | Kompact | TokenSieve |
|---|---|---|
| 拦截位置 | HTTP API 代理（Provider 侧） | CLI PATH shadowing（Tool 侧） |
| 输入格式 | Anthropic / OpenAI 消息 + tool defs | CLI JSON stdout |
| 核心压缩 | 8+ transforms（按内容类型） | 6-stage pipeline（结构化剪枝 + 去重 + 格式选择） |
| 去重/过滤 | Schema Optimizer（TF-IDF 选 tool）、Observation Masker（历史 masking） | First-seen-wins scalar dedup（值指纹） |
| Cache 优化 | Cache Aligner（UUID/时间戳/路径占位） | 无（一次性 CLI 输出，无会话缓存） |
| Token 统计 | 估算（chars/4） | tiktoken cl100k_base 精确计算 |
| 目标场景 | 长会话、多 tool、多轮对话 | 单次 CLI 调用的大 JSON 输出 |

---

## 四、结论

1. **Kompact** 的 8 层变换管线是可组合、可配置、自适应的，其中 **Cache Aligner** 对 prefix cache 命中率优化直接对应 provider 折扣，**Schema Optimizer** 提供了比当前固定白名单更智能的 tool 选择方案。

2. **TokenSieve** 的“file-block 指纹去重”应理解为 **JSON 标量级的 first-seen-wins 去重**，而非文件分块哈希指纹。其设计重点在于：时间戳剥离、空值折叠、base64 占位、重复标量抑制、结构扁平化。

3. 两个项目均可为当前 `anthropic_proxy.py` 的 context 优化提供参考：
   - 引入 Kompact 式 Cache Aligner 以提升 Anthropic prefix cache 命中。
   - 引入 TokenSieve 式 JSON 结构化压缩，用于处理 `Bash`/`Read` 返回的大型 JSON/日志。


---

## 五、实测结果

### 5.1 Kompact 测试与基准

```bash
cd /Users/jinsongwang/APP/research/kompact
uv sync --extra dev
.venv/bin/python -m pytest -q
# 74 passed in 0.59s

.venv/bin/python benchmarks/compression_ratio.py
```

输出摘要：

| Fixture | Transform | 压缩后占比 | 节省 token |
|---|---|---|---|
| error_responses (5 items) | TOON | 63.25% | 155 |
| error_responses (5 items) | JSON Crusher | 77.87% | 93 |
| search_results (10 items) | TOON | 73.36% | 531 |
| log_outputs (8799 chars) | Pipeline | 75.63% | 536 |
| code_files (16226 chars) | Pipeline | **27.28%** | **2950** |
| synthetic 100-item user array | TOON | 42.13% | 2239 |
| synthetic 100-item user array | JSON Crusher | **22.33%** | **3005** |

**结论**：Kompact 对**代码**和**结构化 JSON 数组**压缩效果最显著，对日志文本提升中等。

### 5.2 TokenSieve 测试与压测

```bash
cd /Users/jinsongwang/APP/research/tokensieve
cargo test --release
# 60 unit tests + 1 doc-test, all passed
```

17 次真实 AWS 调用汇总：

| 指标 | 数值 |
|---|---|
| 原始 token | 40,483 |
| 压缩后 token | 21,487 |
| **整体节省** | **46.9%** |
| 最高单次节省 | EKS describe-cluster 66.4% |
| 主要驱动 | base64 证书占位、结构扁平化、重复标量去重、key 缩写 |

---

## 六、与当前 `anthropic_proxy.py` 的对比

### 6.1 当前 proxy 已有的能力

当前代理在 `anthropic_proxy.py` 中已经实现了相当丰富的上下文管理：

| 能力 | 实现位置 | 说明 |
|---|---|---|
| Tool 定义过滤 | `_filter_tools` (~3164) | 固定白名单 + 最近使用，>20 个 tool 时触发 |
| Tool result 清理 | `_compress_content_pass` (~1082) | 按语义优先级保留高价值 Read/Bash 结果，其余替换为 `[cleared: ...]` |
| 消息截断 | `truncate_messages_if_needed` (~1724) | `char`/`rounds`/`fifo`/`smart` 四种策略 |
| Smart 截断 | `_apply_smart_truncation` (~1620) | 保留 system + tool_result，优先保留最近 user/assistant |
| 思考块剥离 | `_compress_content_pass` Phase 2b | 只保留最近 N 条 assistant thinking |
| Blocker 检测 | `_detect_blocker_pattern` (~1907) | 检测连续相同错误类型并注入 `[BLOCKER]` |
| 关键词索引 | `_extract_keywords` / `_inject_keyword_context` (~3219) | 从丢弃消息提取文件名/错误类型/函数名注入尾部 |
| 请求去重 | `_check_dedup` (~174) | body hash + 时间窗防重复 POST |
| 错误分类 | `_classify_exception` (~252) | 503/504 加 Retry-After |

### 6.2 当前 proxy 尚未覆盖、可借鉴的能力

| 可借鉴点 | 来源 | 当前缺失 | 预期收益 |
|---|---|---|---|
| **Cache Aligner** | Kompact | 没有把 UUID/时间戳/用户路径从 system prompt 中提取为占位符 | 提升 Anthropic prefix cache 命中率，直接降低 90% cached input 成本 |
| **JSON/代码结构化压缩** | TokenSieve / Kompact | tool_result 中的 JSON/代码要么全清，要么原样保留，没有中间压缩 | 对 Read 返回的 JSON、Bash 返回的日志/JSON 可再省 30–70% |
| **TF-IDF Tool 选择** | Kompact Schema Optimizer | 当前是固定白名单 + 最近使用，不够动态 | tool 定义很多时，按当前 query 动态挑选更相关 tool |
| **标量去重** | TokenSieve Deduper | 同一条 tool_result 内部重复 ID/ARN/时间戳未去重 | 减少 AWS/云 API 输出中的冗余标量 |
| **base64 blob 占位** | TokenSieve Sieve | 当前未识别长 base64 字符串并替换 | 证书、kubeconfig、embeddings 等可大幅压缩 |
| **Schema-YAML / PVFN 输出** | TokenSieve Router | 当前保持原始 JSON，未做格式转换 | 同质对象数组可省 20–50% key 重复 token |

### 6.3 当前 proxy 更优的地方

| 当前能力 | 说明 |
|---|---|
| 双语错误翻译 | 把 `Wasted call` / `File does not exist` 等转写为中文提示 |
| Loop/Blocker 干预 | 比 Kompact/TokenSieve 更主动的循环检测 |
| Smart truncation | 保留 Read tool_result 避免 re-read 死循环 |
| 生命周期阶段感知 | `_classify_lifecycle_stage` 按会话阶段调整策略 |
| 请求去重 | 防止客户端重试导致重复 LLM 调用 |

---

## 七、可落地的集成建议

### 7.1 短期可尝试（改动小、风险低）

#### A. 引入 Cache Aligner（Kompact 模式）
在 `anthropic_proxy.py` 的 `do_POST` 早期对 `request.system` 和前 1–2 条 user 消息做动态值占位符化：
- UUID → `{UUID_0}`
- 时间戳 → `{TS_0}`
- `/Users/...`、`/home/...`、`/tmp/...` 路径 → `{PATH_0}`

**收益**：前缀稳定后 Anthropic cached input 折扣生效，长期会话成本明显下降。

#### B. 增强 Tool Result 内容压缩（TokenSieve 模式）
在 `_compress_content_pass` 中，对**未被清除**的 tool_result 增加一层 JSON 结构化压缩：
1. 删除 `null` / `""` / `[]` / `{}`。
2. 将 ≥200 字符的 base64 字符串替换为 `<base64 N chars>`。
3. 同文档内重复标量去重（first-seen-wins）。
4. 对同质对象数组尝试 Schema-YAML 或 JSON minify。

**注意**：只对非 Read 的 tool_result 或 Read 返回的非代码 JSON 启用，避免破坏代码文件内容。

#### C. base64 Blob 检测
直接复用 TokenSieve 的 content-only 检测逻辑：
```python
def is_base64_blob(s: str) -> bool:
    if len(s) < 200: return False
    alnum = sum(c.isalnum() for c in s)
    all_b64 = all(c in BASE64_ALPHABET for c in s)
    return all_b64 and alnum / len(s) >= 0.92
```
对检测到的 blob 替换为占位符，可显著降低证书、图片 base64、embedding 等 token 占用。

### 7.2 中期可评估（改动中等）

#### D. 替换/增强 `_filter_tools`
当前 `_filter_tools` 使用固定白名单。可引入 Kompact 的 TF-IDF 相关性打分：
- 从最近 N 条消息提取 query。
- 对每个 tool 的 name/description/parameters 做 TF-IDF。
- 保留 top-K + 最近使用 + 强制白名单。

**适用条件**：当 tool 数量 > 20 且当前任务明显只需要子集时（如用户只问代码问题，可隐藏财务/邮件类 tool）。

#### E. JSON 输出格式转换
对 `Bash` 返回的大型 JSON 数组（如 `aws ec2 describe-*`）在 proxy 层转换为 Schema-YAML 或 PVFN：
- key 只发一次。
- 值按行排列。
- 路径/缩写映射头部 `@map` 可进一步缩短长键名。

**风险**：模型需要适应新格式。建议先在非核心会话 A/B 测试。

### 7.3 不建议直接照搬的点

| 不建议 | 原因 |
|---|---|
| 完全照搬 Kompact 的 Observation Masker | 当前 proxy 的 smart truncation + tool_result 保留已能避免 re-read 死循环，盲目 masking 可能回退到旧问题 |
| 完全照搬 TokenSieve 的 PATH shadowing | 当前架构是 HTTP 代理，CLI 拦截模式不适用 |
| 启用 Kompact 的 Code Compressor 于 Read 结果 | 当前策略是保留 Read 文件完整内容，压缩代码骨架会丢失实现细节 |
| TokenSieve 的 PVFN 作为默认格式 | 需要模型对 `path=value` 格式有足够理解力，否则可能降低质量 |

---

## 八、下一步可执行动作

1. **最小可复现 PoC**：在 `anthropic_proxy.py` 中新增一个 `PROXY_CACHE_ALIGN_ENABLED` 开关，实现 Cache Aligner，对本地 backend 跑 10 轮相同 system prompt 的请求，观察 prefix cache 命中变化。

2. **JSON 压缩 PoC**：新增 `PROXY_JSON_COMPRESS_ENABLED` 开关，对 `tool_result` 中 JSON 字符串应用 TokenSieve 的 sieve + deduper 逻辑，用真实 AWS CLI 输出测量 token 节省。

3. **A/B 质量评估**：选取 3–5 个长会话日志，分别用当前 proxy、+Cache Aligner、+JSON 压缩三种配置重放，比较：
   - token 节省率
   - 任务完成率
   - re-read / wasted 错误数量

4. **文档同步**：若引入新开关，更新 `AGENTS.md` 和 `docs/02-architecture-design/proxy-pipeline-reference.md`。

---

*研究完成时间：2026-06-18*  
*源码路径：/Users/jinsongwang/APP/research/{kompact,tokensieve}*  
*报告路径：/Users/jinsongwang/APP/llama.cpp/research_kompact_tokensieve.md*
