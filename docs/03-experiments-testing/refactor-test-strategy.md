# 重构测试策略与回归保障方案

> **版本**: v1.0 · 2026-06-18
> **目标读者**: 开发工程师 / 测试工程师 / 架构评审者
> **背景**: `anthropic_proxy.py` 从 v0.5.0 的 3,611 行增长至 4,557 行(+26%)，单文件包含所
>   有逻辑（配置、消息转换、工具解析、循环检测、压缩、blocker、metrics、HTTP handler）。
>   本次重构的核心目标：
>   (1) 模块化提取，降低认知负荷与耦合度
>   (2) 上下文管理优化（Cache Aligner、结构化压缩）
>   (3) 云端模式硬化
>   (4) 配置体系简化
> **策略目标**: 确保重构过程功能等价（回归安全），量化评估优化效果（可验证）。

---

## 目录

1. [重构范围与风险评估](#1-重构范围与风险评估)
2. [回归测试策略](#2-回归测试策略)
3. [优化效果评估框架](#3-优化效果评估框架)
4. [测试场景与案例补充](#4-测试场景与案例补充)
5. [度量指标与门禁标准](#5-度量指标与门禁标准)
6. [阶段执行路线图](#6-阶段执行路线图)

---

## 1. 重构范围与风险评估

### 1.1 重构目标与影响面

| 重构模块 | 预期目标 | 影响范围 | 风险等级 | 评估方法 |
|----------|----------|----------|----------|----------|
| **模块拆分** — 将 4,557 行单文件拆分为 `proxy/` 包 | 降低认知负载、解耦 | 所有 import 站 | **P0/高** — import 失败直接阻断所有请求 | 等价性校验 + import 测试 |
| **Cache Aligner** — 前缀对齐（UUID/时间戳/路径归一化） | 提升 prefix cache 命中率 | `_handle_messages` 早期处理、system prompt | **P1/中** — 仅影响对齐点，不影响功能等价性 | A/B 对比 + 命中率基准 |
| **结构化压缩** — 替代粗暴清除（JSON 瘦身+去重） | 减少语义损失，保留更多信息 | `clear_old_tool_results`、`_compress_content_pass` | **P1/中** — 压缩策略改变可能影响循环检测阈值 | token 数对比 + 语义保留率 |
| **云端模式硬化** — 自动检测增强、错误处理 | 云端场景稳定 | `BACKEND_TYPE` 判断、forwarding 路径 | **P2/中** — 云端路径目前无回归网 | 独立的云端 E2E 套件 |
| **配置体系简化** — 聚合默认值、减少冗余开关 | 降低配置迁移成本 | `configs/*.conf` 解析逻辑、PROXY_* 常量 | **P2/低** — 向后兼容旧配置即可 | 配置兼容性矩阵测试 |

### 1.2 各模块依赖关系

重构前函数调用关系（按重构后模块分组）：

```
anthropic_proxy.py (现状：单文件)
│
├─ core/config.py           ← 环境变量读取 + 默认值逻辑（130+ 行常量定义）
│   └─ 被所有模块引用
│
├─ core/metrics.py          ← log_metrics, _finalize_metrics, _mask_sensitive
│   └─ 被 _handle_messages、main 引用
│
├─ core/tool_parse.py       ← _extract_content_tool_calls, _StreamingToolsExtractor,
│   │                          parse_tool_arguments, _repair_truncated_json
│   └─ 被 convert_openai_response_to_anthropic、_handle_messages 引用
│
├─ core/format.py           ← convert_anthropic_messages_to_openai,
│   │                          convert_openai_response_to_anthropic,
│   │                          convert_anthropic_tools_to_openai,
│   │                          convert_anthropic_tool_choice_to_openai
│   └─ 被 _handle_messages 引用（核心转发路径）
│
├─ core/compression.py      ← _compress_content_pass, _incremental_compress,
│   │                          _compress_assistant_message, _apply_smart_truncation,
│   │                          _apply_rounds_truncation, truncate_messages_if_needed
│   └─ 被 _handle_messages 引用（上下文管理核心）
│
├─ core/clearing.py         ← clear_old_tool_results, strip_old_thinking_blocks,
│   │                          _classify_lifecycle_stage
│   └─ 被 truncation 和 loop detection 引用
│
├─ core/loop.py             ← _apply_loop_intervention, _detect_text_loop,
│   │                          _compute_text_similarity
│   └─ 被 _handle_messages 引用
│
├─ core/blocker.py          ← _detect_blocker_pattern, _build_blocker_message
│   └─ 被 _handle_messages 引用
│
├─ core/tool_filter.py      ← _filter_tools
│   └─ 被 _handle_messages 引用
│
├─ core/keyword_index.py    ← _extract_keywords, _inject_keyword_context,
│   │                          _translate_tool_result_errors
│   └─ 被 _handle_messages 引用
│
├─ core/dedup.py            ← _check_dedup
│   └─ 被 do_GET 引用
│
├─ core/cache_aligner.py    ← [新增] _align_system_prompt, _normalize_dynamic_values
│   └─ 被 _handle_messages 早期调用
│
└─ core/server.py           ← class Handler(BaseHTTPRequestHandler), do_POST, do_GET,
                               _handle_messages, _build_status_html, main()
    └─ 引用以上所有模块
```

**关键风险**: `_handle_messages`（~200+ 行）引用了几乎所有模块。重构时它的 import 变更是最容易出错的点——单一错位 import 会导致整个 /proxy 服务不可用。

---

## 2. 回归测试策略

### 2.1 分层回归框架

重构的每个变更都需要在 3 个层面验证等价性：

```
 Layer 1: 函数级等价性      输入→输出行为完全一致
     ↓
 Layer 2: 模块级等价性      模块组装后对外表现一致
     ↓
 Layer 3: 系统级等价性      proxy 端到端表现一致
```

### 2.2 等价性校验技术（模块拆分的核心保障）

#### 2.2.1 Import Smoke Test

每个模块拆分后，必须通过：

```python
# test/unit/test_module_imports.py — 新增
def test_import_config():
    """配置模块可独立导入"""
    import proxy.config
    assert hasattr(proxy.config, "IS_CLOUD")

def test_import_all_modules():
    """所有子模块可独立导入且无循环依赖"""
    for mod in ["config", "metrics", "tool_parse", "format",
                "compression", "clearing", "loop", "blocker",
                "tool_filter", "keyword_index", "dedup", "server"]:
        __import__(f"proxy.{mod}")

def test_no_circular_imports():
    """验证 import 图无环（全部导入不报 ImportError）"""
    import proxy
    assert hasattr(proxy, "Handler")
```

#### 2.2.2 函数签名快照

确保拆分前后函数签名完全一致：

```python
# test/unit/test_signature_preservation.py — 新增
import inspect, json

# 签名快照路径
_SNAPSHOT = "test/fixtures/func_signatures.json"

def test_all_func_signatures_preserved():
    """所有公共函数签名保持不变"""
    import anthropic_proxy as p
    snapshot = json.load(open(_SNAPSHOT))
    for name, expected in snapshot.items():
        fn = getattr(p, name, None)
        assert fn is not None, f"{name} 不存在"
        try:
            params = list(inspect.signature(fn).parameters.keys())
        except (ValueError, TypeError):
            continue  # C 扩展等无法 inspect
        assert params == expected["params"], \
            f"{name} 签名变更: {params} != {expected['params']}"
```

**生成快照命令**（重构前运行一次并提交）：

```bash
python3 -c "
import anthropic_proxy as p, inspect, json
snap = {}
for name in dir(p):
    fn = getattr(p, name)
    if not callable(fn) or name.startswith('_'): continue
    try:
        params = list(inspect.signature(fn).parameters.keys())
        snap[name] = {'name': name, 'params': params}
    except: pass
with open('test/fixtures/func_signatures.json', 'w') as f:
    json.dump(snap, f, indent=2)
"
```

#### 2.2.3 行为快照

对核心函数，记录结构化"输入→输出"快照：

```python
# test/unit/test_behavior_snapshot.py — 新增
_SNAPSHOTS = "test/fixtures/behavior_snapshots.json"

class TestBehaviorSnapshots(unittest.TestCase):
    def setUp(self):
        self.cases = json.load(open(_SNAPSHOTS))

    def test_classify_exception(self):
        for c in self.cases["_classify_exception"]:
            r = proxy._classify_exception(c["input"])
            assert r["status"] == c["expected"]["status"]
            assert r["retryable"] == c["expected"]["retryable"]

    def test_parse_tool_arguments(self):
        for c in self.cases["parse_tool_arguments"]:
            r = proxy.parse_tool_arguments(c["input"])
            assert r == c["expected"]

    def test_repair_truncated_json(self):
        for c in self.cases["_repair_truncated_json"]:
            r = proxy._repair_truncated_json(c["input"])
            assert r == c["expected"]

    def test_detect_blocker_pattern(self):
        for c in self.cases["_detect_blocker_pattern"]:
            r = proxy._detect_blocker_pattern(c["seq"], c.get("threshold", 3))
            assert r == c["expected"]

    def test_apply_smart_truncation(self):
        for c in self.cases["_apply_smart_truncation"]:
            r = proxy._apply_smart_truncation(c["messages"], c["max_chars"])
            assert len(r) == len(c["expected"])
```

快照覆盖函数及场景数：

| 函数 | 快照数 | 覆盖场景 |
|------|--------|----------|
| `_classify_exception` | 8 | OOM、ConnectionRefused、Timeout、RuntimeError(rapid-mlx)、ValueError、FileNotFound、KeyError、自定义错误 |
| `parse_tool_arguments` | 10 | 纯 JSON、嵌入 JSON、XML `<tool_call>`、`<function=...>`、heuristic、异常 JSON、空输入、None |
| `_extract_content_tool_calls` | 11 | 单/多 block、混杂文本、未闭合、JSON 解析失败、arguments 含 `</tools>`、arguments 为 string、数组形状、disabled |
| `_repair_truncated_json` | 8 | 纯截断、缺右括号、缺右引号、缺右大括号、嵌套截断、Unicode 截断、已完整、空对象 |
| `_detect_blocker_pattern` | 6 | N=3/5/7 相同错误、混合错误、single error |
| `clear_old_tool_results` | 6 | 全部保留、部分清除、全清除+preview、近期 Read 保护(+5)、frozen zone、清空后 |
| `_apply_loop_intervention` | 6 | Level 1/2/3、text loop、reset、disabled |

#### 2.2.4 模块级黑盒测试

```python
# test/unit/test_module_tool_parse.py — 新增
class TestParseModuleAPI(unittest.TestCase):
    """工具解析模块公共 API"""
    
    def test_streaming_end_to_end(self):
        """流式 chunk → 完整工具调用"""
        chunks = [
            'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"<tools>"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"{\\"name\\":\\"read\\""}}]}\n\n',
            'data: {"choices":[{"delta":{"content":",\\"arguments\\":{\\"path\\":\\"a.py\\"}}}"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"}</tools>"}}]}\n\n',
            'data: [DONE]\n\n',
        ]
        engine = proxy._StreamingToolsExtractor()
        result = {"tools": [], "text": ""}
        for c in chunks:
            result = engine.feed(c)
        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "read"
    
    def test_xml_to_json_4_levels(self):
        """4 级 XML→JSON 回退全部可触发"""
        for level, input_str in [
            (1, '{"name": "read", "arguments": {"path": "a.py"}}'),
            (2, 'Some text {"name": "read", "arguments": {}} more text'),
            (3, '<tool_call>read({"path": "a.py"})</tool_call>'),
            (4, '<function=read>{"path": "a.py"}</function>'),
        ]:
            result = proxy.parse_tool_arguments(input_str)
            assert result is not None, f"Level {level} fallback failed"
            assert result["name"] == "read"

    def test_7_json_repair_types(self):
        """7 种截断修复全覆盖"""
        for case in REPAIR_CASES:  # 来自 test/fixtures/
            result = proxy._repair_truncated_json(case["broken"])
            assert result == case["repaired"]
```

### 2.3 配置兼容性校验

```python
# test/unit/test_config_compatibility.py — 新增
class TestConfigBackwardCompat(unittest.TestCase):
    """旧配置文件在新代码上仍能工作"""
    
    def test_all_configs_parse(self):
        """configs/ 下所有 .conf 文件可被 sourcable"""
        import glob
        for conf in glob.glob("configs/*.conf"):
            with self.subTest(conf=os.path.basename(conf)):
                # 模拟 bash source
                import subprocess
                r = subprocess.run(["bash", "-c", f"source {conf} && env"], 
                                 capture_output=True, text=True)
                assert "LLAMA_MODEL" in r.stdout, f"{conf} 缺少 LLAMA_MODEL"
    
    def test_active_conf_symlink(self):
        """active.conf 是 symlink 的场景"""
        import os
        assert os.path.islink("configs/active.conf") or os.path.isfile("configs/active.conf")
    
    def test_env_override_precedence(self):
        """PROXY_* > LLAMA_* > 配置文件默认值"""
        from unittest.mock import patch
        import anthropic_proxy as proxy
        
        with patch.dict(os.environ, {"PROXY_CLEAR_ENABLED": "false"}, clear=True):
            reload(proxy)
            assert proxy.CLEAR_ENABLED is False
        
        with patch.dict(os.environ, {"PROXY_CLEAR_ENABLED": "true"}, clear=True):
            reload(proxy)
            assert proxy.CLEAR_ENABLED is True
    
    def test_partial_override(self):
        """只设 LLAMA_* 时使用默认 PROXY_* 值"""
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            reload(proxy)
            # 应使用代码中的默认值
            assert hasattr(proxy, "CLEAR_ENABLED")
```

### 2.4 协议合同测试

```python
# test/unit/test_protocol_contract.py — 新增
class TestProtocolContract(unittest.TestCase):
    """Anthropic ↔ OpenAI 协议翻译的回归网"""
    
    def test_message_schema_unchanged(self):
        """消息格式转换符合 OpenAI schema"""
        msg = {"role": "user", "content": "hello"}
        result = proxy.convert_anthropic_messages_to_openai([msg], [], None)
        assert isinstance(result, list)
        assert result[0]["role"] == "system"  # 自动插入
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "hello"
    
    def test_tool_def_schema_unchanged(self):
        """工具定义字段映射不变"""
        tools = [{"name": "read", "description": "Read a file",
                   "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]
        oai = proxy.convert_anthropic_tools_to_openai(tools)
        assert oai[0]["type"] == "function"
        fn = oai[0]["function"]
        assert fn["name"] == "read"
        assert "parameters" in fn
        assert fn["parameters"]["type"] == "object"
    
    def test_tool_choice_contract(self):
        """tool_choice 映射不变"""
        assert proxy.convert_anthropic_tool_choice_to_openai({"type": "auto"}) == "auto"
        assert proxy.convert_anthropic_tool_choice_to_openai({"type": "any"}) == "required"
        assert proxy.convert_anthropic_tool_choice_to_openai(
            {"type": "tool", "name": "read"}) == {"type": "function", "function": {"name": "read"}}
    
    def test_response_shape(self):
        """OpenAI → Anthropic 响应 json 顶层字段不变"""
        oai = json.dumps({
            "id": "chatcmpl-xxx", "object": "chat.completion",
            "created": 123, "model": "qwen",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        })
        anthro = proxy.convert_openai_response_to_anthropic(oai)
        assert anthro["id"].startswith("msg_")
        assert anthro["type"] == "message"
        assert anthro["content"][0]["type"] == "text"
        assert "input_tokens" in anthro["usage"]
        assert "output_tokens" in anthro["usage"]
```

---

## 3. 优化效果评估框架

### 3.1 Cache Aligner 效果评估

#### 3.1.1 单元测试

```python
# test/unit/test_cache_aligner.py — 新增
class TestCacheAligner(unittest.TestCase):
    """前缀对齐单元测试"""
    
    def test_uuid_replaced(self):
        """UUID 占位符化"""
        text = "session 550e8400-e29b-41d4-a716-446655440000"
        result = proxy._align_system_prompt(text)
        assert "{UUID_0}" in result
        assert "550e8400" not in result
    
    def test_iso_timestamp_replaced(self):
        """ISO 8601 时间戳归一化"""
        text = "Today is 2026-06-18T10:30:00+08:00"
        result = proxy._align_system_prompt(text)
        assert "{TS_0}" in result
    
    def test_user_path_replaced(self):
        """用户路径归一化"""
        text = "Working in /Users/bob/projects/main.py"
        result = proxy._align_system_prompt(text)
        assert "{PATH_0}" in result
        assert "/Users/bob" not in result
    
    def test_multiple_uuids_numbered(self):
        """同类多个动态值递增编号"""
        text = "a=550e8400-e29b-41d4-a716-446655440000 b=6ba7b810-9dad-11d1-80b4-00c04fd430c8"
        result = proxy._align_system_prompt(text)
        assert "{UUID_0}" in result
        assert "{UUID_1}" in result
    
    def test_no_false_positive(self):
        """普通文本不变"""
        text = "This is a normal sentence with no dynamic content."
        result = proxy._align_system_prompt(text)
        assert result == text
    
    def test_idempotent(self):
        """幂等性：相同输入产生相同输出"""
        text = "Session 550e8400-e29b-41d4-a716-446655440000"
        assert proxy._align_system_prompt(text) == proxy._align_system_prompt(text)
    
    def test_system_prompt_stable_across_requests(self):
        """连续 2 次请求的 system prompt 完全一致（有动态值被对齐）"""
        import time
        r1 = proxy._align_system_prompt(
            "System prompt. Date: 2026-06-18. Path: /Users/bob/project.")
        r2 = proxy._align_system_prompt(
            "System prompt. Date: 2026-06-19. Path: /Users/bob/project.")
        assert r1 == r2  # 时间戳和路径被对齐后一致
```

#### 3.1.2 集成测试

```bash
# test/integration/test_cache_alignment.sh — 新增
#!/usr/bin/env bash
# Cache Aligner 集成测试：验证 proxy 实际发送给后端的 system prompt

set -euo pipefail
cd "$(dirname "$0")/../.."

# 启动 mock backend（监听 mock 收到的实际请求）
python3 test/integration/mock_backend.py &
MOCK_PID=$!
sleep 1

# 启动 proxy，启用 Cache Aligner
LLAMA_BASE_URL=http://127.0.0.1:8089/v1 \
PROXY_CACHE_ALIGN_ENABLED=true \
python3 anthropic_proxy.py --port 4003 &
PROXY_PID=$!
sleep 2

# 发送含 UUID 的请求
curl -s -X POST http://127.0.0.1:4003/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-test" \
  -d '{
    "model": "test-model",
    "system": "Session: 550e8400-e29b-41d4-a716-446655440000, Time: 2026-06-18T10:30:00",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 10
  }'

# 检查 mock 收到的 system prompt 中 UUID 被对齐
if grep -q "{UUID_0}" /tmp/mock_captured_system.txt 2>/dev/null; then
  echo "PASS: cache_align_system_prompt_has_placeholders"
else
  echo "FAIL: system prompt lacks placeholders"
  exit 1
fi

# 发送相同 system prompt（不含动态值的情况）
curl -s -X POST http://127.0.0.1:4003/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-test" \
  -d '{
    "model": "test-model",
    "system": "Static system prompt only",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 10
  }'

if ! grep -q "{UUID_0}" /tmp/mock_captured_system_static.txt 2>/dev/null; then
  echo "PASS: no false positive on static text"
else
  echo "FAIL: false positive on static text"
  exit 1
fi

# 清理
kill $PROXY_PID $MOCK_PID 2>/dev/null || true
echo "All cache_alignment tests passed"
```

### 3.2 结构化压缩效果评估

```python
# test/unit/test_structured_compress.py — 新增
class TestStructuredCompression(unittest.TestCase):
    """TokenSieve 风格结构化压缩"""
    
    def test_null_removed(self):
        """JSON 中 null 被移除"""
        text = json.dumps({"a": 1, "b": None, "c": "hello"})
        result = proxy._compress_tool_result_json(text)
        parsed = json.loads(result)
        assert "b" not in parsed
        assert parsed["a"] == 1
    
    def test_empty_object_removed(self):
        """空 {} 和 [] 被移除"""
        text = json.dumps({"a": {}, "b": [], "c": [1]})
        result = proxy._compress_tool_result_json(text)
        parsed = json.loads(result)
        assert "a" not in parsed
        assert "b" not in parsed
        assert parsed["c"] == [1]
    
    def test_large_base64_replaced(self):
        """≥200 字符的 base64 被占位符替代"""
        b64 = "A" * 300
        text = json.dumps({"content": b64, "name": "test.png"})
        result = proxy._compress_tool_result_json(text)
        assert len(result) < len(text) * 0.5
        assert "<base64" in result or "base64" in result.lower()
    
    def test_duplicate_scalar_removed(self):
        """同文档重复标量值 first-seen-wins"""
        text = json.dumps({"items": [{"x": 1, "y": 1}, {"x": 2, "y": 1}]})
        result = proxy._compress_tool_result_json(text)
        assert len(result) <= len(text)  # 不应膨胀
        parsed = json.loads(result)
        assert len(parsed["items"]) == 2  # 结构保留
    
    def test_compression_ratio_reported(self):
        """压缩率在合理范围"""
        text = json.dumps({"a": 1, "b": None, "c": {}, "d": "A" * 500})
        result, ratio = proxy._compress_tool_result_json(text, return_ratio=True)
        assert 0.0 < ratio < 1.0
        assert len(result) < len(text) * 0.5
    
    def test_typical_read_result(self):
        """典型 Read 命令的 tool_result 压缩"""
        read_result = json.dumps({
            "path": "src/main.py",
            "content": "def hello():\n    print('hello world')\n" * 50,
            "size": 1250,
            "lines": 100
        })
        result, ratio = proxy._compress_tool_result_json(read_result, return_ratio=True)
        # content 不应被完全删除
        assert "hello" in result
        # 但整体应被压缩
        assert len(result) < len(read_result)
```

### 3.3 端到端基准测试

重构前后必须运行以下基准测试进行效果量化对比：

```bash
# tools/run_refactor_benchmarks.sh — 新增
#!/usr/bin/env bash
# 重构效果基准测试：同一环境下对比重构前后
# 用法: bash tools/run_refactor_benchmarks.sh [before_ref] [after_ref]

set -euo pipefail
BEFORE="${1:-HEAD~1}"
AFTER="${2:-HEAD}"
RESULTS="logs/refactor_bench_$(date +%Y%m%d_%H%M%S).json"

measure() {
    local label=$1; shift
    echo "=== [基准] $label ==="
    "$@" 2>&1 | tee -a "$RESULTS.tmp"
}

echo "{\"before\": \"$BEFORE\", \"after\": \"$AFTER\", \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$RESULTS"

# 基准组 A: 前缀对齐效果
measure "Prefix cache 命中率" python3 tools/cache_analyzer.py \
  --mode simulate --requests 30

# 基准组 B: 压缩效果
measure "上下文压缩率" python3 tools/bench_compress.py \
  --scenario typical_agentic_session --detail

# 基准组 C: 代理延迟
measure "代理 TTFT" python3 tools/bench_agent.py \
  --scenario quick_read --output /tmp/bench_agent_result.json

echo "=== 基准测试完成: $RESULTS ==="
```

### 3.4 A/B 对比实验

对算法替换型重构，设计轻量 A/B 实验：

| 实验 | 对照组 | 实验组 | 评估指标 | 最小样本 | 决策准则 |
|------|--------|--------|----------|----------|----------|
| Cache Aligner | `PROXY_CACHE_ALIGN=false` | `PROXY_CACHE_ALIGN=true` | prefix cache hit rate, TTFT p50 | 50 请求 | hit rate ≥ +15% |
| 结构化压缩 | `PROXY_STRUCT_COMPRESS=false` | `PROXY_STRUCT_COMPRESS=true` | compression ratio, loop rate | 100 请求 | ratio ≥ 40%, loop 不上升 |
| 截断策略 | `strategy=fifo` | `strategy=smart` | TTFT, 消息数/轮 | 30 请求 | TTFT p50 ≤ 120% baseline |

执行方式：

```bash
# Cache Aligner A/B
PROXY_CACHE_ALIGN=false python3 tools/run_experiment.sh \
  --output logs/ab_cache_align_control.jsonl --requests 50
PROXY_CACHE_ALIGN=true  python3 tools/run_experiment.sh \
  --output logs/ab_cache_align_treatment.jsonl --requests 50

# 分析
python3 tools/analyze_experiment.py \
  --control logs/ab_cache_align_control.jsonl \
  --treatment logs/ab_cache_align_treatment.jsonl \
  --metrics cache_hit_rate,ttft_p50,loop_rate
```

---

## 4. 测试场景与案例补充

### 4.1 新增测试文件总览

| 文件 | 层级 | Case 数 | 覆盖范围 |
|------|------|---------|----------|
| `test/unit/test_module_imports.py` | unit | 5 | 模块拆分后 import 完整性 |
| `test/unit/test_signature_preservation.py` | unit | 1 | 函数签名快照对比 |
| `test/unit/test_behavior_snapshot.py` | unit | 10+ | 输入→输出快照对比 |
| `test/unit/test_config_compatibility.py` | unit | 8 | 配置向后兼容性 |
| `test/unit/test_protocol_contract.py` | unit | 7 | API 协议合同 |
| `test/unit/test_module_tool_parse.py` | unit | 12 | 工具解析模块独立性 |
| `test/unit/test_cache_aligner.py` | unit | 8 | Cache Aligner 前缀对齐 |
| `test/unit/test_structured_compress.py` | unit | 8 | 结构化压缩 |
| `test/unit/test_concurrency.py` | unit | 6 | 并发安全 |
| `test/unit/test_cloud_mode.py` | unit | 10 | 云端模式检测+默认值+错误转发 |
| `test/unit/test_error_classification.py` | unit | 5 | 错误分类扩展 |
| `test/integration/test_cloud_mode.sh` | integration | 5 | 云端模式完整路径 |
| `test/integration/test_cache_alignment.sh` | integration | 2 | Cache Aligner 端到端 |
| `test/integration/test_config_loading.sh` | integration | 4 | 配置加载和优先级 |
| `test/e2e/test_cloud_e2e.py` | e2e | 3 | 云端真实 API 冒烟 |
| **合计** | | **94+** | |

### 4.2 现有测试用例增强

#### 4.2.1 增量压缩缓存（R1.3，当前零覆盖）

追加到 `test_proxy_fallback.py`：

```python
class TestIncrementalCompress(unittest.TestCase):
    """R1.3: 增量压缩 + 缓存"""
    
    def test_cache_hit_returns_cached(self):
        """相同内容缓存命中"""
        text = "content to compress"
        cache = {}
        r1 = proxy._incremental_compress(text, cache)
        r2 = proxy._incremental_compress(text, cache)
        assert r2["from_cache"] is True
        assert r2["result"] == r1["result"]
    
    def test_cache_miss_calls_compressor(self):
        """新内容触发压缩"""
        text = "new content"
        cache = {}
        result = proxy._incremental_compress(text, cache)
        assert result["from_cache"] is False
        assert "result" in result
    
    def test_cache_eviction(self):
        """缓存在 MAX_CACHE_ENTRIES 后淘汰"""
        cache = {}
        for i in range(proxy.INCREMENTAL_CACHE_MAX + 5):
            proxy._incremental_compress(f"text_{i}", cache)
        assert len(cache) <= proxy.INCREMENTAL_CACHE_MAX
    
    def test_compression_result_shape(self):
        """压缩结果包含必要字段"""
        result = proxy._incremental_compress("test", {})
        expected_keys = {"result", "from_cache", "original_chars", "compressed_chars"}
        assert expected_keys.issubset(result.keys())
```

#### 4.2.2 关键字索引（R1.4，当前零覆盖）

```python
class TestKeywordIndex(unittest.TestCase):
    """R1.4: 关键词按需检索"""
    
    def test_extract_filenames(self):
        """从消息中提取文件名"""
        msgs = [
            {"role": "user", "content": "please read main.py and utils.py"},
            {"role": "assistant", "content": "I'll check src/config.py"}
        ]
        keywords = proxy._extract_keywords(msgs)
        for f in ["main.py", "utils.py", "src/config.py"]:
            assert f in keywords, f"{f} 未提取"
    
    def test_extract_known_patterns(self):
        """提取已知配置模式"""
        msgs = [{"role": "user", "content": "look at config.yaml and .env"}]
        keywords = proxy._extract_keywords(msgs)
        assert len(keywords) > 0
    
    def test_disabled_via_env(self):
        """PROXY_HISTORY_ENABLED=false 时不提取"""
        with patch.object(proxy, "HISTORY_ENABLED", False):
            assert proxy._extract_keywords([{"role": "user", "content": "read file.py"}]) == set()
```

#### 4.2.3 工具定义过滤（R3.3，当前零覆盖）

```python
class TestToolFiltering(unittest.TestCase):
    """R3.3: 工具定义过滤"""
    
    def test_all_44_to_15(self):
        """44 个工具→约 15 个"""
        tools = [{"name": f"tool_{i}"} for i in range(44)]
        recent = ["tool_1"]
        filtered = proxy._filter_tools(tools, recent)
        assert 10 <= len(filtered) <= 20
    
    def test_always_keep_tools(self):
        """必定保留 read/write/glob"""
        tools = [{"name": n} for n in (["read","write","glob"] + [f"other_{i}" for i in range(41)])]
        filtered = proxy._filter_tools(tools, ["other_5"])
        names = [t["name"] for t in filtered]
        for keep in ["read", "write", "glob"]:
            assert keep in names, f"{keep} 被过滤了"
    
    def test_tool_choice_any_returns_all(self):
        """tool_choice=any 不过滤"""
        tools = [{"name": f"tool_{i}"} for i in range(44)]
        filtered = proxy._filter_tools(tools, [], tool_choice={"type": "any"})
        assert len(filtered) == 44
    
    def test_fallback_when_lt_minimum(self):
        """过滤后不足 5 个时返回全部"""
        tools = [{"name": "read"}, {"name": "write"}]
        filtered = proxy._filter_tools(tools, [])
        assert len(filtered) == 2
    
    def test_recent_tools_included(self):
        """近期使用的工具被保留"""
        tools = [{"name": f"tool_{i}"} for i in range(44)]
        recent = ["tool_30", "tool_35", "tool_40"]
        filtered = proxy._filter_tools(tools, recent)
        names = [t["name"] for t in filtered]
        for r in recent:
            assert r in names, f"近期工具 {r} 被过滤"
```

#### 4.2.4 截断策略（R1.1，当前仅测 fifo）

```python
class TestTruncationStrategies(unittest.TestCase):
    """R1.1: rounds/char 策略"""
    
    def _build_messages(self, n, rounds=1):
        msgs = [{"role": "system", "content": "system prompt"}]
        for i in range(n):
            msgs.append({"role": "user", "content": f"msg_{i}"})
            msgs.append({"role": "assistant", "content": f"resp_{i}"})
        return msgs
    
    def test_rounds_drops_oldest(self):
        """rounds 丢弃最早轮次"""
        msgs = self._build_messages(10, rounds=5)
        truncated = proxy.truncate_messages_if_needed(
            msgs, strategy="rounds", keep_rounds=2)
        assert len(truncated) < len(msgs)
        # 最早 3 轮应被丢弃
        content = json.dumps(truncated)
        assert "msg_0" not in content, "最早消息未被丢弃"
        assert "msg_8" in content, "最新消息被丢弃"
    
    def test_char_triggers(self):
        """char 策略在超过字符限制时触发"""
        msgs = self._build_messages(20)
        long_content = "x" * 200
        msgs.append({"role": "user", "content": long_content})
        truncated = proxy.truncate_messages_if_needed(
            msgs, strategy="char", char_limit=500)
        total = sum(len(json.dumps(m)) for m in truncated)
        assert total <= 600  # 允许 20% 偏差
```

### 4.3 云端模式测试

```python
# test/unit/test_cloud_mode.py — 新增
class TestCloudModeDetection(unittest.TestCase):
    """云端模式自动检测"""
    
    def setUp(self):
        # 保存原始 env 并在 tearDown 恢复
        self._orig = os.environ.copy()
    
    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig)
    
    def _reload_proxy(self):
        import importlib
        import anthropic_proxy as p
        importlib.reload(p)
        return p
    
    def test_detect_deepseek_cloud(self):
        """DeepSeek URL → cloud"""
        os.environ["LLAMA_BASE_URL"] = "https://api.deepseek.com/v1"
        p = self._reload_proxy()
        assert p.IS_CLOUD is True
    
    def test_detect_openai_cloud(self):
        """OpenAI URL → cloud"""
        os.environ["LLAMA_BASE_URL"] = "https://api.openai.com/v1"
        p = self._reload_proxy()
        assert p.IS_CLOUD is True
    
    def test_detect_local(self):
        """localhost:8081 → local"""
        os.environ["LLAMA_BASE_URL"] = "http://127.0.0.1:8081/v1"
        p = self._reload_proxy()
        assert p.IS_CLOUD is False
    
    def test_manual_override(self):
        """BACKEND_TYPE 覆盖自动检测"""
        os.environ["LLAMA_BASE_URL"] = "https://api.deepseek.com/v1"
        os.environ["BACKEND_TYPE"] = "local"
        p = self._reload_proxy()
        assert p.IS_CLOUD is False
    
    def test_cloud_disables_clearing(self):
        """云端下默认关闭 tool clearing"""
        os.environ["LLAMA_BASE_URL"] = "https://api.deepseek.com/v1"
        p = self._reload_proxy()
        assert p.CLEAR_ENABLED is False
    
    def test_cloud_sets_higher_concurrency(self):
        """云端下默认并发 4"""
        os.environ["LLAMA_BASE_URL"] = "https://api.deepseek.com/v1"
        p = self._reload_proxy()
        assert p.MAX_CONCURRENT == 4
    
    def test_cloud_401_handling(self):
        """云端 API key 错误返回 401"""
        from unittest.mock import patch
        import urllib.error
        with patch("urllib.request.urlopen") as mock:
            mock.side_effect = urllib.error.HTTPError(
                "https://api.deepseek.com/v1/chat/completions",
                401, "Unauthorized", {}, None)
            status, body = proxy._forward_cloud_request(...)
            assert status == 401
    
    def test_cloud_timeout_retryable(self):
        """云端超时归入 retryable 503"""
        os.environ["LLAMA_BASE_URL"] = "https://api.deepseek.com/v1"
        p = self._reload_proxy()
        with patch.object(p, "_forward_cloud_request", side_effect=TimeoutError):
            result = p._classify_exception(TimeoutError("timed out"))
            assert result["retryable"] is True
            assert result["status"] == 504
```

### 4.4 并发与线程安全

```python
# test/unit/test_concurrency.py — 新增
class TestConcurrencySafety(unittest.TestCase):
    """代理线程安全"""
    
    def test_semaphore_enforces_max(self):
        """Semaphore 控制并发不超过 PROXY_MAX_CONCURRENT"""
        sem = threading.Semaphore(2)
        active = []
        lock = threading.Lock()
        
        def task():
            with proxy._acquire_concurrent(sem):
                with lock:
                    active.append(1)
                    assert len(active) <= 2
                time.sleep(0.05)
                with lock:
                    active.pop()
        
        threads = [threading.Thread(target=task) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
    
    def test_loop_state_no_race(self):
        """_LOOP_SESSION_STATE 在并发下不损坏"""
        import random
        state = {}
        lock = threading.Lock()
        
        def update():
            for _ in range(500):
                with lock:
                    proxy._update_loop_state(state, random.choice(["read", "write", "glob"]))
        
        threads = [threading.Thread(target=update) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        # 状态应有效
        assert isinstance(state, dict)
    
    def test_lock_released_on_error(self):
        """_llama_lock 在异常路径释放"""
        lock = threading.Lock()
        try:
            with proxy._acquire_concurrent(lock):
                raise RuntimeError("模拟异常")
        except RuntimeError:
            pass
        # 锁应在异常后释放
        assert lock.acquire(blocking=False), "锁在异常后未释放"
        lock.release()
    
    def test_metrics_line_not_corrupted(self):
        """并发 metrics 写入行不交叠"""
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            log_file = f.name
        try:
            proxy.METRICS_FILE = log_file
            def writer(req_id):
                proxy.log_metrics({"status": 200, "req_id": req_id, "duration_ms": 100})
            threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
            for t in threads: t.start()
            for t in threads: t.join()
            
            lines = open(log_file).readlines()
            assert len(lines) == 20, f"预期 20 行，实际 {len(lines)}"
            for line in lines:
                obj = json.loads(line)
                assert "req_id" in obj
                assert isinstance(obj["req_id"], int)
        finally:
            os.unlink(log_file)
```

### 4.5 数据驱动 Fixture 文件

```json
# test/fixtures/loop_sequences.json
[
  {
    "name": "3-round-read-loop",
    "tools": [{"name": "read", "arguments": {"path": "a.py"}}],
    "iterations": 3,
    "expected_level": 2,
    "expected_intervention": true
  },
  {
    "name": "write-cognitive-loop",
    "tools": [
      {"name": "write", "arguments": {"path": "a.py", "content": "v1"}},
      {"name": "write", "arguments": {"path": "a.py", "content": "v2"}}
    ],
    "iterations": 6,
    "expected_level": 3,
    "expected_intervention": true,
    "expected_message_contains": "SWITCH"
  },
  {
    "name": "single-error-no-loop",
    "tools": [{"name": "read", "arguments": {"path": "missing.py"}}],
    "iterations": 1,
    "expected_level": 0,
    "expected_intervention": false
  }
]
```

```json
# test/fixtures/tool_filter_corpus.json
{
  "all_44_tools": [
    {"name": "read", "description": "Read a file"},
    {"name": "write", "description": "Write to a file"},
    {"name": "glob", "description": "List files"},
    {"name": "edit", "description": "Edit a file"},
    {"name": "bash", "description": "Run a shell command"},
    {"name": "web_search", "description": "Search the web"},
    {"name": "web_fetch", "description": "Fetch a URL"},
    {"name": "notebook_edit", "description": "Edit a notebook"},
    {"name": "str_replace_editor", "description": "Replace text in file"},
    {"name": "create", "description": "Create a file"},
    {"name": "think", "description": "Think about a problem"},
    {"name": "task", "description": "Create a sub task"},
    {"name": "question", "description": "Ask the user"},
    {"name": "computer", "description": "Control the computer"},
    {"name": "text_editor", "description": "Edit text"}
  ],
  "scenarios": {
    "fresh_session": {
      "tool_choice": "auto",
      "recent_used": [],
      "expected_tools_approx": 15
    },
    "recent_3": {
      "tool_choice": "auto",
      "recent_used": ["read", "read", "write"],
      "expected_tools_approx": 18
    },
    "tool_choice_any": {
      "tool_choice": {"type": "any"},
      "recent_used": [],
      "expected_tools_approx": 44
    },
    "tool_choice_specific": {
      "tool_choice": {"type": "tool", "name": "read"},
      "recent_used": [],
      "expected_tools_approx": 1
    }
  }
}
```

---

## 5. 度量指标与门禁标准

### 5.1 回归测试门禁

| 门禁 | 触发时机 | 标准 | 违规后果 |
|------|----------|------|----------|
| pre-commit (unit) | 每次 commit | ✅ unit 全部通过 | 拒绝 commit |
| 签名快照 | 每次重构变更 | ✅ 函数签名 100% 一致 | 阻塞变更 |
| 行为快照 | 每次重构变更 | ✅ 核心函数输出一致 | 阻塞变更（可更新快照） |
| pre-push (integration) | 每次 push | ✅ mock backend 测试通过 | 提醒 |
| 合并前 (e2e) | PR 合并前 | ✅ e2e 套件通过 | 阻塞合并 |
| 配置兼容性 | 配置变更 | ✅ 所有旧配置可 sourcable | 阻塞变更 |

### 5.2 效果评估门禁

| 指标 | 重构前基准 | 目标 | 测量工具 | 样本量 |
|------|-----------|------|----------|--------|
| 单元测试数量 | 213 | ≥ 280（+30%） | `grep -c "def test_"` | N/A |
| 需求覆盖率 | 78% (18/23) | ≥ 90% (21/23) | 需求矩阵 | N/A |
| **Prefix cache 命中率** | ~0% (local) | ≥ 30% | `tools/cache_analyzer.py` | 50 请求 |
| **TTFT p50** | ~28s (38K ctx) | ≤ 20s | `tools/bench_agent.py` | 30 请求 |
| **上下文压缩率** | ~30% (fifo) | ≥ 50% | `tools/bench_compress.py` | 10 场景 |
| **并发扩展性** | N=1 稳定 | N=2 稳定 | `tools/stress_concurrency.py` | 60s |
| **云端成功率** | 无基线 | ≥ 99% | E2E 套件 | 50 请求 |
| **死循环触发率** | 偶发 | 0% | 集成测试 mock | 10 场景 |

### 5.3 覆盖率增长曲线

| 阶段 | 单元测试 | 集成测试 | E2E | 总计 | pre-commit 耗时 |
|------|----------|----------|-----|------|-----------------|
| 当前 (v0.5.6) | 213 | 12 | 22 | **247** | ~1s |
| Phase 1 (回归网) | 264 | 16 | 22 | **302** | ≤ 3s |
| Phase 2 (模块提取) | 292 | 21 | 22 | **335** | ≤ 4s |
| Phase 3 (算法替换) | 318 | 25 | 25 | **368** | ≤ 5s |
| Phase 4 (云端) | 348 | 30 | 28 | **406** | ≤ 5s |
| 完成态 | 350+ | 30+ | 30+ | **410+** | ≤ 5s |

---

## 6. 阶段执行路线图

### Phase 1 — 回归安全网（第 1 周）

**目标**: 在改动代码前建立全量回归网

```
前置条件（重构前提交）
├── test/fixtures/func_signatures.json       ← 签名快照
├── test/fixtures/behavior_snapshots.json    ← 行为快照
├── test/fixtures/loop_sequences.json        ← 循环场景
└── test/fixtures/tool_filter_corpus.json    ← 过滤场景

新增测试
├── test/unit/test_module_imports.py          (5 cases)
├── test/unit/test_signature_preservation.py   (1 case)
├── test/unit/test_behavior_snapshot.py        (10+ cases)
├── test/unit/test_config_compatibility.py     (8 cases)
├── test/unit/test_protocol_contract.py        (7 cases)
├── 增强现有: TestIncrementalCompress          (+4 cases)
├── 增强现有: TestKeywordIndex                 (+4 cases)
├── 增强现有: TestToolFiltering                (+6 cases)
├── 增强现有: TestTruncationStrategies          (+6 cases)
└── test/integration/test_config_loading.sh    (4 cases)

门禁更新
├── .githooks/pre-commit 增加 --signature 检查
└── test/run_tests.sh 增加 --full 模式（unit + signature + snapshot）

小计: 51+ 新增测试
```

### Phase 2 — 模块提取（第 2 周）

**目标**: 提取首个模块并验证等价性

```
模块提取（按风险从低到高）
├── 第 1 步: proxy/dedup.py            ← 无外部依赖
├── 第 2 步: proxy/config.py           ← 被所有模块引用，先提取
├── 第 3 步: proxy/metrics.py          ← 独立日志
├── 第 4 步: proxy/tool_parse.py       ← 独立解析
├── 第 5 步: proxy/format.py           ← 协议转换
├── 第 6 步: proxy/tool_filter.py      ← 独立过滤
├── 第 7 步: proxy/compression.py      ← 压缩+截断+清除
├── 第 8 步: proxy/loop.py + blocker.py ← 循环检测
├── 第 9 步: proxy/server.py           ← HTTP handler（最后）

每步验证
├── import smoke test 通过
├── 签名快照 100% 一致
├── 行为快照 100% 通过
└── pre-commit pass

新增测试
├── test/unit/test_module_tool_parse.py  (12 cases)
├── test/unit/test_concurrency.py         (6 cases)
└── test/integration/test_cloud_mode.sh   (5 cases)

小计: 23+ 新增测试
```

### Phase 3 — 算法替换（第 3-4 周）

**目标**: Cache Aligner + 结构化压缩落地

```
Cache Aligner
├── proxy/cache_aligner.py
├── test/unit/test_cache_aligner.py          (8 cases)
├── test/integration/test_cache_alignment.sh (2 cases)
└── A/B 实验 → cache hit rate ≥ +15%

结构化压缩
├── proxy/compression.py（增强_crunch_tool_result）
├── test/unit/test_structured_compress.py    (8 cases)
└── A/B 实验 → compression ratio ≥ 40%

小计: 18+ 新增测试
```

### Phase 4 — 云端硬化 + 配置简化（第 5-6 周）

```
云端硬化
├── test/unit/test_cloud_mode.py              (10 cases)
├── test/integration/test_cloud_mode.sh       (+5 cases)
└── test/e2e/test_cloud_e2e.py                (3 cases, 需真实 API key)

配置简化
├── 消减冗余环境变量
├── 配置兼容性矩阵自动化（验证旧配置可用）
└── test/integration/test_config_loading.sh   (+4 cases)

小计: 22+ 新增测试
```

### Phase 5 — 持续维护

```
性能基准监控
├── tools/run_refactor_benchmarks.sh
└── 每个重大版本发布前运行

Fuzzing / Property-based
├── 对 _extract_keywords / _repair_truncated_json 做模糊测试
└── Phase D 评估 hypothesis 引入 ROI

在线监控驱动
├── proxy_metrics.jsonl 聚合命中率
└── 观察实际 compression ratio、loop rate 分布
```

---

## 附录 A: 重构分支工作流

```bash
# 1. 建立回归基准（重构前一次提交）
cd /Users/jinsongwang/APP/llama.cpp
python3 tools/gen_behavior_snapshots.py \
  --input test/fixtures/snapshot_sources.json \
  --output test/fixtures/behavior_snapshots.json
python3 tools/gen_func_signatures.py \
  --output test/fixtures/func_signatures.json
git add test/fixtures/
git commit -m "chore: 建立重构前行为/签名快照基线"

# 2. 创建重构分支
git checkout -b refactor/module-extraction

# 3. 每次提交前
bash test/run_tests.sh --fast          # unit + signature + snapshot

# 4. 推送前
bash test/run_tests.sh --all           # unit + integration + e2e

# 5. 算法替换时做 A/B
PROXY_CACHE_ALIGN_ENABLED=false python3 tools/bench_compress.py --scenario typical
PROXY_CACHE_ALIGN_ENABLED=true  python3 tools/bench_compress.py --scenario typical

# 6. 合并前
bash test/run_tests.sh --all
python3 tools/run_refactor_benchmarks.sh  # 对比重构前后基准
```

## 附录 B: 回归失败处理流程

```
┌─ unit test 失败 ──────→ 修复代码，不更新快照
│
├─ 签名快照不一致 ──────→ 确认是有意变更？→ 是：更新快照 / 否：修复
│
├─ 行为快照不一致 ──────→ 确认是有意变更？→ 是：`python3 tools/gen_behavior_snapshots.py --update`
│                            否：修复回归
│
├─ 配置兼容性失败 ──────→ 配置变更→必须提供 configs/ 迁移路径
│
├─ e2e 失败 ───────────→ 确认环境正常（proxy 是否在运行？端口冲突？）
│
└─ 基准测试降级 ────────→ 超过门禁阈值→暂缓合并，定位根因
```

## 附录 C: 测试 Fixture 文件规范

```python
# test/fixtures/ 下的 JSON fixture 格式

# behavior_snapshots.json:
# {
#   "<函数名>": [
#     {"input": ..., "expected": ...},
#     ...
#   ]
# }

# func_signatures.json:
# {
#   "<函数名>": {"name": "...", "params": ["arg1", "arg2", ...]},
#   ...
# }

# loop_sequences.json:
# [
#   {
#     "name": "场景描述",
#     "tools": [{"name": "...", "arguments": {...}}],
#     "iterations": 3,
#     "expected_level": 2,
#     "expected_intervention": true
#   }
# ]

# tool_filter_corpus.json:
# {
#   "all_44_tools": [{"name": "...", "description": "..."}],
#   "scenarios": {
#     "fresh_session": {
#       "tool_choice": "auto",
#       "recent_used": [],
#       "expected_tools_approx": 15
#     }
#   }
# }
```

---

> **文档变更记录**
>
> | 日期 | 变更说明 |
> |------|----------|
> | 2026-06-18 | 初版发布，覆盖模块拆分、Cache Aligner、结构化压缩、云端模式硬化、配置简化 |
