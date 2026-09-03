# Pipeline 抽象设计文档

**状态**：待实施  
**依赖**：Phase 0-3 重构完成（14 模块，462 测试，1598 行主文件）  
**预计规模**：~500 行新代码 + ~500 行重构  
**风险**：高（核心编排器变更）

---

## 一、背景

`anthropic_proxy.py` 的 `Handler._handle_messages()` 方法包含 **20+ 个顺序处理阶段**，全部内联在一个 ~530 行的方法中。每个阶段手工调用 `log()` 和 `_mc_put()` 进行日志和 metrics 采集。

```
当前 _handle_messages 阶段顺序：
  L1: 请求入口（解析、去重、会话跟踪）
  L2: 错误翻译 (_translate_tool_result_errors)
  L3: 阻塞检测 (_detect_blocker_pattern)
  L4: 系统消息规范化 (_normalize_system_messages)
  L5: 缓存对齐 (_apply_cache_aligner)
  L6: 生命周期分类 (_classify_lifecycle_stage)
  L7: 内容压缩 (_compress_content_pass)
  L8: 循环检测 (_detect_text_loop)
  L9: 循环干预 (_apply_loop_intervention)
  L10: 重读检测
  L11: 日期标准化
  L12: 上下文截断 (truncate_messages_if_needed)
  L13: OOM 安全截断
  L14: 前缀比率 (_compute_common_prefix_ratio)
  L15: 格式转换 (convert_anthropic_messages_to_openai)
  L16: 系统提示处理
  L17: 工具过滤 (_filter_tools)
  L18: 后端转发 (urllib.request.urlopen)
  L19: 响应处理（流式/非流式）
  L20: Metrics 记录
```

**核心问题**：无可组合性、不可独立测试、横切关注点手动注入、顺序依赖隐式。

---

## 二、目标设计

### 2.1 核心接口

```python
class PipelineContext:
    """请求级状态容器。贯穿整个管线的唯一数据结构。"""
    request_id: str
    body: dict          # 原始请求体
    messages: list      # 当前消息列表
    tools: list         # 当前工具定义
    model: str
    stream: bool
    max_tokens: int
    stage_config: dict  # 生命周期阶段配置
    metrics: dict       # 累计 metrics（替代 _mc_put）
    errors: list        # 错误收集
    timings: dict       # 各阶段耗时
    flags: set          # 质量标志


class PipelineStage:
    """单个处理阶段。"""
    name: str
    
    def process(self, ctx: PipelineContext) -> PipelineContext: ...


class Pipeline:
    """有序 Stage 列表。顺序执行，输出=下一输入。"""
    def run(self, ctx: PipelineContext) -> PipelineContext: ...


class InstrumentedPipeline(Pipeline):
    """自动日志 + metrics + 计时。"""
    def run(self, ctx): ...
```

### 2.2 Stage 清单

| # | Stage 名称 | 当前实现 | 所在模块 |
|---|-----------|---------|---------|
| 1 | `RequestParser` | 内联 | Handler |
| 2 | `ErrorTranslator` | `_translate_tool_result_errors` | `tool_filter` |
| 3 | `BlockerDetector` | `_detect_blocker_pattern` | `loop_detection` |
| 4 | `SystemNormalizer` | `_normalize_system_messages` | `lifecycle` |
| 5 | `CacheAligner` | `_apply_cache_aligner` | `lifecycle` |
| 6 | `LifecycleClassifier` | `_classify_lifecycle_stage` | `lifecycle` |
| 7 | `ContentCompressor` | `_compress_content_pass` | `truncation` |
| 8 | `LoopDetector` | `_detect_text_loop` | `loop_detection` |
| 9 | `LoopIntervention` | `_apply_loop_intervention` | `loop_detection` |
| 10 | `RereadDetector` | 内联 | Handler |
| 11 | `DateNormalizer` | 内联 | Handler |
| 12 | `ContextTruncator` | `truncate_messages_if_needed` | `truncation` |
| 13 | `PrefixRatioComputer` | `_compute_common_prefix_ratio` | `message_converter` |
| 14 | `FormatConverter` | `convert_anthropic_messages_to_openai` | `message_converter` |
| 15 | `ToolFilter` | `_filter_tools` | `tool_filter` |
| 16 | `BackendDispatcher` | 内联 | Handler |
| 17 | `ResponseHandler` | 内联 | Handler |

### 2.3 目标 `_handle_messages`

```python
def _handle_messages(self, body):
    ctx = PipelineContext(body=body, model=..., stream=...)
    pipeline = InstrumentedPipeline([
        RequestParser(),
        ErrorTranslator(),
        SystemNormalizer(),
        CacheAligner(),
        LifecycleClassifier(),
        BlockerDetector(),
        ContentCompressor(),
        LoopDetector(),
        LoopIntervention(),
        RereadDetector(),
        DateNormalizer(),
        ContextTruncator(),
        PrefixRatioComputer(),
        FormatConverter(),
        ToolFilter(),
        BackendDispatcher(self._llama_lock),
        ResponseHandler(),
    ])
    ctx = pipeline.run(ctx)
    self._respond(ctx)
```

从 ~530 行缩减至 ~30 行。

---

## 三、实施计划

### 步骤 1：定义接口（创建 `pipeline.py`）

- `PipelineContext` 数据类
- `PipelineStage` 抽象基类
- `Pipeline` 编排器
- `InstrumentedPipeline`

### 步骤 2：分批迁移 Stage

| 批次 | Stage | 风险 |
|------|-------|------|
| A | `PipelineContext` + `RequestParser` + `ErrorTranslator` + `SystemNormalizer` | 低 |
| B | `CacheAligner` + `LifecycleClassifier` | 低 |
| C | `BlockerDetector` + `LoopDetector` + `LoopIntervention` | 中 |
| D | `ContentCompressor` + `DateNormalizer` + `RereadDetector` | 中 |
| E | `ContextTruncator` + `PrefixRatioComputer` | 高 |
| F | `FormatConverter` + `ToolFilter` | 中 |
| G | `BackendDispatcher` + `ResponseHandler` | 高 |

### 步骤 3：替换 `_handle_messages`

所有 Stage 就位后，用 Pipeline 替换内联代码。

### 步骤 4：测试验证

- 每个 Stage 独立单元测试
- Pipeline 集成测试
- 回归：462 单元 + 47 集成 + 5 Promptfoo

---

## 四、风险与缓解

| 风险 | 缓解 |
|------|------|
| Stage 顺序依赖破坏 | 每批次提交后运行全部测试 |
| PipelineContext 设计不当 | 先试点 2-3 个 Stage |
| 性能退化 | ~0.1ms/stage 开销，462 tests 安全网 |
| 测试迁移工作量 | 现有测试保持通过，增量添加 Stage 测试 |

---

## 五、验收标准

1. `_handle_messages` 从 ~530 行缩减至 ~30 行
2. 全部现有测试通过
3. 每个 Stage 可独立实例化和测试
4. 新增 Stage 只需实现 `PipelineStage` 接口
5. 日志/metrics 自动采集

---

## 六、备选方案

**不做 Pipeline 抽象**：当前 `_handle_messages` 虽然冗长但稳定。小团队低频变更可保持现状。

**部分抽象**：仅迁移低风险的 A-D 批次（8 个 Stage），高风险 Stage 保留原处。
