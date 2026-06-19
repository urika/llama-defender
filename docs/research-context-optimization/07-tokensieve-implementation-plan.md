# 07 TokenSieve 借鉴点落地开发计划

## 7.1 背景与目标

TokenSieve 是针对 AWS CLI 等大型 JSON 输出进行结构化压缩的 Rust 工具，其管线设计（scrubber → JSON gate → sieve → deduper → router/pvfn → auditor）与本系统痛点高度重合：

- **痛点 2**：tool result 粗暴 clearing 导致 re-read 死循环
- **痛点 4**：后端资源约束需要降低单轮 token 量
- **痛点 7**：压缩后缺乏语义校验，可观测性不足

本计划将 TokenSieve 的核心思想转化为 `anthropic_proxy.py` 中可执行、可测试、可回滚的开发任务。

## 7.2 设计原则

1. **标准库实现**：不引入第三方依赖，符合项目现有约束。
2. **仅压缩 tool_result**：不碰 system prompt、skills、用户消息。
3. **语义可逆**：压缩后的内容仍能让模型判断文件是否存在、内容大意。
4. **失败回退**：auditor 校验不通过时返回原始内容。
5. **开关化**：每个子功能独立 env var，默认保守开启。

## 7.3 TokenSieve 管线与本系统映射

```
TokenSieve                本系统落地能力                    接入模块
─────────────────────────────────────────────────────────────────────────
scrubber        →    ANSI/不可见字符清洗                 _compress_tool_result
JSON gate       →    内容类型识别（json/code/log/text）   _detect_content_type
sieve           →    JSON 结构化摘要                     _sieve_json
deduper         →    标量值首次出现后去重                _dedupe_scalars
router/pvfn     →    按类型选择压缩策略                  _COMPRESS_STRATEGIES
auditor         →    压缩后语义校验                      _audit_compression
```

## 7.4 开发任务清单

### Phase 1：基础清洗与识别（可与第一阶段并行）

| 任务 | 负责人 | 验收标准 | 预计工时 | 依赖 |
|------|--------|----------|----------|------|
| 7.4.1 实现 `ANSIScrubber` | 开发 | `\x1b\[[0-9;]*m` 等 ANSI 转义序列被去除；`test/unit/` 覆盖纯文本、带颜色 Bash 输出 | 4h | 无 |
| 7.4.2 实现 `ContentTypeDetector` | 开发 | 能正确区分 json/code/log/text；单元测试覆盖 4 种类型边界 case | 4h | 无 |
| 7.4.3 注册压缩入口 `compress_tool_result` | 开发 | 统一入口接受 `(content, mime_hint=None)`，返回 `(compressed, meta)`；meta 包含 `content_type`/`strategy`/`original_len`/`compressed_len`/`audit_pass` | 4h | 7.4.1、7.4.2 |

### Phase 2：核心压缩器（第二阶段主体）

| 任务 | 负责人 | 验收标准 | 预计工时 | 依赖 |
|------|--------|----------|----------|------|
| 7.4.4 实现 `JSONSieveCompressor` | 开发 | 对 JSON 数组保留 key、截断长字符串、保留前 N 项 + 总数；对嵌套对象限制深度；失败回退原样 | 1.5d | 7.4.3 |
| 7.4.5 实现 `LogCompressor` | 开发 | 去除时间戳、聚合相邻重复行、保留含 error/exception/warning 的行；单元测试覆盖 | 1d | 7.4.3 |
| 7.4.6 增强 `CodeCompressor` | 开发 | 删除注释与不影响语法的空白；保留缩进结构；不压缩短代码（<阈值） | 1d | 7.4.3 |
| 7.4.7 实现 `PlainTextCompressor` | 开发 | 仅对超长段落做截断，保留前 N 行 + 行数提示 | 4h | 7.4.3 |

### Phase 3：策略路由与校验

| 任务 | 负责人 | 验收标准 | 预计工时 | 依赖 |
|------|--------|----------|----------|------|
| 7.4.8 实现 `ContentRouter` | 开发 | 根据 `content_type` 选择对应压缩器；允许 `mime_hint` 覆盖；默认对未知类型使用 plain text | 1d | 7.4.4–7.4.7 |
| 7.4.9 实现 `CompressionAuditor` | 开发 | JSON 压缩后 `json.loads` 通过；code 压缩后括号/引号平衡；失败时 `audit_pass=false` 并回退 | 4h | 7.4.4–7.4.8 |
| 7.4.10 实现标量 `Deduper` | 开发 | 仅对长度 > 20 的字符串缓存，首次出现后替换为 `###(repeated: N)`；默认关闭；非代码类型启用 | 4h | 7.4.4 |

### Phase 4：接入代理与可观测性

| 任务 | 负责人 | 验收标准 | 预计工时 | 依赖 |
|------|--------|----------|----------|------|
| 7.4.11 接入 `_compress_content_pass` | 开发 | 替换粗暴 clearing 逻辑，改为调用 `compress_tool_result`；保留 `PROXY_CLEAR_ENABLED=false` 时完全关闭压缩的行为 | 1d | 7.4.3–7.4.9 |
| 7.4.12 新增压缩相关 metrics 字段 | 开发 | 每条请求记录 `compress_enabled`/`compress_strategy`/`compress_original_len`/`compress_compressed_len`/`compress_ratio`/`compress_audit_pass` | 4h | 7.4.11 |
| 7.4.13 `/status` 增加压缩统计 | 开发 | 显示本轮压缩次数、平均压缩率、audit 失败次数 | 4h | 7.4.12 |

### Phase 5：测试与验证

| 任务 | 负责人 | 验收标准 | 预计工时 | 依赖 |
|------|--------|----------|----------|------|
| 7.4.14 单元测试覆盖所有压缩器 | 开发 | `test/unit/test_tokensieve_compressors.py` 覆盖 scrubber、detector、json sieve、log、code、router、auditor、deduper | 1d | 7.4.1–7.4.10 |
| 7.4.15 集成测试：压缩后 re-read 不增加 | 开发/QA | 构造包含大 JSON tool_result 的会话，验证 `wasted` 计数不增加 | 4h | 7.4.11 |
| 7.4.16 A/B benchmark | 开发/QA | 运行 `bench_agent.py` 对比压缩开启/关闭的 token 量、session 稳定性 | 1d | 7.4.11–7.4.15 |

## 7.5 接口设计（草案）

```python
# 新增模块：可放在 anthropic_proxy.py 内或单独文件 tokensieve_adapters.py

import json
import re

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

class CompressResult:
    def __init__(self, original, compressed, content_type, strategy,
                 audit_pass=True, meta=None):
        self.original = original
        self.compressed = compressed
        self.content_type = content_type
        self.strategy = strategy
        self.audit_pass = audit_pass
        self.meta = meta or {}

    @property
    def ratio(self):
        if not self.original:
            return 1.0
        return len(self.compressed) / len(self.original)

def compress_tool_result(content: str, mime_hint: str = None,
                         threshold: int = 4096,
                         enable_dedupe: bool = False) -> CompressResult:
    if len(content) < threshold:
        return CompressResult(content, content, "short", "none", audit_pass=True)

    scrubbed = _scrub_ansi(content)
    content_type = mime_hint or _detect_content_type(scrubbed)
    strategy = _COMPRESS_STRATEGIES.get(content_type, _compress_plain)
    compressed = strategy(scrubbed, enable_dedupe=enable_dedupe)
    audit_pass = _audit_compression(scrubbed, compressed, content_type)

    if not audit_pass:
        compressed = scrubbed

    return CompressResult(
        original=content,
        compressed=compressed,
        content_type=content_type,
        strategy=strategy.__name__,
        audit_pass=audit_pass,
    )
```

## 7.6 环境变量与开关

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_COMPRESS_ENABLED` | `true`（local）/ `false`（cloud） | 总开关 |
| `PROXY_COMPRESS_THRESHOLD` | `4096` | 触发压缩的字符阈值 |
| `PROXY_COMPRESS_MODE` | `semantic` | `lossless` / `semantic` / `aggressive` |
| `PROXY_SCRUB_ANSI` | `true` | 是否去除 ANSI 颜色码 |
| `PROXY_SIEVE_JSON_MAX_ITEMS` | `10` | JSON 数组保留最大项数 |
| `PROXY_SIEVE_JSON_MAX_STR_LEN` | `200` | JSON 字符串最大保留长度 |
| `PROXY_SIEVE_JSON_MAX_DEPTH` | `4` | JSON 嵌套最大深度 |
| `PROXY_LOG_DEDUPE` | `true` | 是否聚合重复日志行 |
| `PROXY_DEDUPE_SCALARS` | `false` | 是否启用标量去重（默认关闭） |
| `PROXY_COMPRESS_AUDIT` | `true` | 是否启用压缩后校验 |

## 7.7 接入 `anthropic_proxy.py` 的关键位置

1. **`_compress_content_pass`**：将现有粗暴 clearing 替换为 `compress_tool_result` 调用。
2. **`_respond_json` / metrics**：在请求处理末尾写入压缩统计字段。
3. **`/status`**：读取最近 N 条请求的压缩统计并展示。
4. **`do_POST` 前置处理**：为 `tool_result` 内容标记 `mime_hint`（如已知文件扩展名或 content-type）。

## 7.8 测试策略

| 测试层级 | 覆盖内容 | 工具 |
|----------|----------|------|
| 单元测试 | 每个压缩器、router、auditor 的输入输出 | `python3 -m unittest test/unit/test_tokensieve_compressors.py` |
| 集成测试 | 端到端请求中 tool_result 被压缩且语义保留 | `test/integration/test_compress_integration.sh` |
| A/B benchmark | 真实/模拟长会话中的 token 量与稳定性 | `python3 tools/bench_agent.py --compress` |

## 7.9 风险与应对

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| JSON 摘要破坏语法 | 中 | 高 | auditor 校验，失败回退 |
| 代码压缩误删语义 | 低 | 高 | 保守策略（只删注释/空行），auditor 校验平衡 |
| ANSI 清洗误伤合法内容 | 低 | 低 | 仅匹配 ANSI 转义序列 |
| 标量去重误伤代码 | 中 | 中 | 默认关闭，非代码类型启用 |
| 压缩增加 CPU 开销 | 中 | 低 | 阈值触发 + 小请求跳过 |

## 7.10 里程碑

| 里程碑 | 时间 | 验收标准 |
|--------|------|----------|
| M2.1 清洗与识别完成 | Phase 1 结束 | ANSI scrubber + content type detector 单元测试通过 |
| M2.2 核心压缩器完成 | Phase 2 结束 | JSON/Log/Code/Plain 压缩器 + 单元测试通过 |
| M2.3 路由与校验接入 | Phase 3 结束 | router + auditor 接入代理，失败回退工作 |
| M2.4 完整验证 | Phase 4–5 结束 | A/B benchmark 显示 tool_result token 下降 ≥ 20%，wasted 不增加 |

## 7.11 与主计划的对应关系

本计划是 `05-plan.md` 第二阶段「语义压缩与防御增强」的细化子计划，具体对应：

- 7.4.1 → `05-plan.md` 2.4
- 7.4.2、7.4.3、7.4.8、7.4.9 → `05-plan.md` 2.5、2.6、2.7
- 7.4.4 → `05-plan.md` 2.1
- 7.4.5 → `05-plan.md` 2.3
- 7.4.6 → `05-plan.md` 2.2
- 7.4.10 → `05-plan.md` 2.8
- 7.4.11–7.4.16 → `05-plan.md` 2.9–2.13 的细化
