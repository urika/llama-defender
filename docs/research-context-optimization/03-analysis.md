# 03 分析：外部产品调研与适用性判断

## 3.1 调研对象

| 产品 | 定位 | 核心能力 | 源码状态 | 与本项目关联 |
|------|------|----------|----------|--------------|
| **Kompact** | Prompt optimization / context compression | 8 层变换管线（JSON/代码/日志/HTML/图像/掩码等） | Python，已 clone | 可直接参考其 `cache_aligner` 和 `schema_optimizer` |
| **TokenSieve** | Token-cost reduction for AWS CLI outputs | 6 阶段管线（scrubber → sieve → deduper → router/pvfn → auditor） | Rust，已 clone | 可参考其去重、摘要、结构化压缩思路 |

## 3.2 Kompact 技术解构

### 3.2.1 8 层变换管线

```
schema_optimizer  →  cache_aligner  →  toon  →  json_crusher
                                                          ↓
content_compressor ← log_compressor ← code_compressor ←┘
                                                          ↓
                                              observation_masker
                                                          ↓
                                                 html_stripper
```

| 层 | 功能 | 本场景相关度 |
|----|------|--------------|
| `schema_optimizer` | JSON Schema 字段去重、默认字段裁剪 | 中 |
| `cache_aligner` | 通过对齐消息顺序、删除动态 system 插入来提高 prefix cache 命中 | **高** |
| `toon` | 将自然语言压缩为 token 最优表达（数字、日期等） | 中 |
| `json_crusher` | JSON 字段排序、空白消除、键名替换 | 高 |
| `code_compressor` | 删除注释/空白，保留语义 | **高** |
| `log_compressor` | 日志去时间戳、聚合相似行 | 中 |
| `content_compressor` | 长文本分块摘要 | 中 |
| `observation_masker` | 敏感字段掩码 | 低 |
| `html_stripper` | 去除 HTML 标签 | 中 |

### 3.2.2 实测效果

基于本地 clone 运行 benchmark：

```
input_tokens=118050
Kompact code pipeline output_tokens=32210
ratio=27.28%
```

代码压缩管线在工具结果、日志、JSON 混合场景下降幅显著，验证了**结构化压缩优于通用截断**。

### 3.2.3 可直接借鉴的实现

1. **`cache_aligner` 的固定前缀策略**
   - 识别并稳定前 N 条消息（system、skills、模型别名）。
   - 移除中间动态插入的 system 消息。
   - 相同工具调用顺序按约定排列。

2. **`schema_optimizer` 的 JSON Schema 精简**
   - Claude Code 每次请求携带 40+ 工具定义，每个工具都有 JSON Schema。
   - Kompact 通过删除 `default`、`example`、`description` 中冗余字段，可降低 schema token。

3. **`code_compressor` 与 `json_crusher` 的语义保留压缩**
   - 去除注释、空行、重复空白。
   - 对 JSON 输出按字典序排列键名，保证相同内容产生相同 token 序列。

### 3.2.4 不适用之处

- Kompact 是离线/批处理优化工具，不是代理运行时；不能直接接入请求链路。
- 部分压缩（如 `content_compressor` 摘要）需要 LLM 本身参与，与本项目"不增加外部调用"的约束冲突。
- `toon` 的自然语言改写可能改变 token 序列，反而破坏 prefix cache。

## 3.3 TokenSieve 技术解构

### 3.3.1 6 阶段管线

```
raw AWS CLI output
      ↓
  scrubber  → 去除 ANSI 颜色、不可见字符
      ↓
 JSON gate  → 判断是否为 JSON 输出
      ↓
    sieve    → 结构化摘要（保留 key/value 长度约束）
      ↓
   deduper   → 重复 value 去重（first-seen-wins 标量模式）
      ↓
router/pvfn → 选择摘要策略（保留原样 / 列表摘要 / KV 摘要）
      ↓
  auditor   → 校验语义是否丢失
```

### 3.3.2 实测效果

```
input tokens=18036
output tokens=9586
savings=46.9%
```

### 3.3.3 可直接借鉴的实现

1. **JSON 结构化摘要**
   - 对 `aws ec2 describe-*` 类型的大型数组输出，保留字段名和值长度，但截断过长字符串。
   - 对 agent 场景：可应用于 `Bash`/`Read` 返回的 JSON 数组、日志输出。

2. **去重策略**
   - TokenSieve 的 `deduper` 使用 **first-seen-wins** 的标量去重，即重复值替换为 `"###"`。
   - 在 agent 场景下，可避免同一文件内容在多个 tool_result 中重复出现。

3. **保留语义的分级策略**
   - `router` 根据输出类型选择策略：原样、列表摘要、KV 摘要。
   - 对应到代理层：可区分代码、JSON、日志、自然语言，使用不同压缩器。

### 3.3.4 不适用之处

- TokenSieve 主要针对 AWS CLI 的 JSON 输出，对代码文件和自然语言对话历史压缩有限。
- 其去重是标量值级别的，不是文件块指纹；无法处理"同一文件不同片段"的重复。
- Rust 实现不能直接嵌入 Python 代理，需要重写核心逻辑。

## 3.4 本地代理层可借鉴的改进点

| 痛点 | Kompact 启发 | TokenSieve 启发 | 当前代理已有基础 |
|------|--------------|-----------------|------------------|
| 痛点 1 cache 冲突 | `cache_aligner` 固定前缀、移除动态 system | 相同请求结构稳定 | `rounds` + `smart preserve` 已有雏形 |
| 痛点 2 tool result 清除 | `json_crusher` + `code_compressor` 语义保留压缩 | 结构化摘要替代删除 | `_compress_content_pass` 已有压缩逻辑 |
| 痛点 3 循环行为 | `observation_masker` 记录关键字段 | `auditor` 校验语义不丢失 | blocker/loop detection 已存在 |
| 痛点 4 后端资源 | `toon` 减少冗余 token | JSON 摘要降低输出 token | `PROXY_MAX_TOKENS_OVERRIDE` 等 |
| 痛点 5 工具过滤 | `schema_optimizer` 精简 schema | - | `_filter_tools` 已有白名单 |
| 痛点 6 兼容性 | - | - | chat template 修复、格式转换 |
| 痛点 7 可观测性 | - | `auditor` 提供验证 | metrics JSONL、/status 已存在 |
| 痛点 8 配置复杂度 | - | - | 环境变量过多 |

## 3.5 适用性判断矩阵

| 能力 | 推荐度 | 风险 | 备注 |
|------|--------|------|------|
| Cache Aligner（固定前缀） | ★★★★★ | 低 | 直接命中痛点 1，改动可控 |
| Schema 精简 | ★★★★☆ | 中 | 需确保不会删掉工具调用必需的字段 |
| 代码/JSON 语义压缩 | ★★★★☆ | 中 | 替代粗暴删除，降低 re-read 风险 |
| 标量去重 | ★★★☆☆ | 中 | 对代码场景收益有限，需验证 |
| 内容摘要（LLM-based） | ★★☆☆☆ | 高 | 增加延迟，破坏 cache 稳定性 |
| 自然语言改写（toon） | ★☆☆☆☆ | 高 | 改变 token 序列，不适合 local 模式 |

## 3.6 关键结论

1. **最高优先级：Cache Aligner 化**。当前代理的 `rounds` 策略+`smart preserve` 已接近 Kompact 的思路，但缺少"固定前缀锚定"和"动态 system 消息隔离"。这一步风险最低、收益最确定。
2. **次高优先级：语义保留压缩**。用 Kompact 的 `json_crusher` + `code_compressor` 替代或增强当前粗暴的 tool result clearing，避免 re-read 死循环。
3. **谨慎引入：标量去重与 LLM 摘要**。需要在小范围测试后决定是否启用。
4. **不推荐：toon 类自然语言改写**。local 模式下 prefix cache 是稀缺资源，改写会破坏前缀稳定。
