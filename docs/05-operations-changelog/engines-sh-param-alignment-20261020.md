# engines.sh 参数对齐修复（tool calling 丢失）— 2026-10-20

> **一句话**：`tools/engines.sh start <name>` 启动的辅助引擎**永久丢失 tool calling 能力**，
> 因为它与 `manage.sh` 的参数拼装逻辑不一致——装了 tool parser 却没开
> `--enable-auto-tool-choice`。本次修复让两者对齐。

---

## 一、现象

用 `tools/engines.sh restart ornith-9b` 重启后，引擎健康、`/v1/models` 正常返回，
但工具调用静默失效：

```bash
# 重启前（manage.sh 启动的实例）
caps: ['text', 'tools']        # tool_call_parser=qwen3_coder_xml

# 重启后（engines.sh 启动的实例）
caps: ['text']                 # ⚠️ tools 消失
```

用 `tools` 参数实测：`finish_reason` 不再返回 `tool_calls`，而是退化为普通文本补全。
**没有报错、没有日志告警**——属于静默降级，最容易被漏掉。

## 二、根因

两个启动入口各自拼装 `rapid-mlx serve` 参数，长期分叉：

| 参数 | `manage.sh` | `engines.sh`（修复前） | 后果 |
|------|-------------|------------------------|------|
| `--enable-auto-tool-choice` | ✅ 总是加 | ❌ **缺失** | **tool parser 不生效** |
| `--tool-call-parser` | ✅ | ✅ | — |
| `--reasoning-parser` | ✅ | ✅ | — |
| `--no-thinking` | ✅（按 `LLAMA_THINKING`） | ❌ 缺失 | thinking 块行为不一致 |
| `--log-level INFO` | ✅ | ❌ 缺失 | 日志级别漂移 |
| `--kv-cache-quantization` | ✅（按 `RAPID_MLX_KV_QUANTIZATION`） | ❌ 缺失 | `configs/ornith-9b.conf` 里 `RAPID_MLX_KV_QUANTIZATION=true` **被静默忽略** |

关键点：`--tool-call-parser` 只是**声明用哪个解析器**，真正启用 tool-call 通路的是
`--enable-auto-tool-choice`。缺了后者，解析器形同虚设。

> 历史背景：`configs/ornith-9b.conf` 头部注释写的是「工具/推理解析器同 35B(qwen3 系)」，
> 但该配置此前一直由 `manage.sh` 生效（那时 8084 走的是 manage 链路），
> 一旦改用 `engines.sh` 就暴露出分叉。

## 三、修复

`tools/engines.sh` 的 `_cmd_start()` 补齐四项，与 `manage.sh` 语义对齐：

```diff
-    local model port extra toolparser reasoning
+    local model port extra toolparser reasoning kvquant kvbits thinking
     reasoning="$(_get_conf "$name" RAPID_MLX_REASONING_PARSER)"
+    kvquant="$(_get_conf "$name" RAPID_MLX_KV_QUANTIZATION)"
+    kvbits="$(_get_conf "$name" RAPID_MLX_KV_QUANT_BITS)"
+    thinking="$(_get_conf "$name" LLAMA_THINKING)"
 ...
     local args=()
     [[ -n "$extra" ]] && args+=( $extra )
+    # 与 manage.sh 对齐: tool choice 必须显式开启, 否则 /v1/models 的
+    # capabilities 不含 tools, tool_calls 永远不触发。
+    args+=( --enable-auto-tool-choice )
     [[ -n "$toolparser" ]] && args+=( --tool-call-parser "$toolparser" )
     [[ -n "$reasoning" ]] && args+=( --reasoning-parser "$reasoning" )
+    # thinking 默认关闭(与 manage.sh 的 LLAMA_THINKING 语义一致)
+    [[ "$thinking" != "true" ]] && args+=( --no-thinking )
+    args+=( --log-level INFO )
+    # KV 量化(与 manage.sh 对齐; conf 里 RAPID_MLX_KV_QUANTIZATION=true 时启用)
+    if [[ "$kvquant" == "true" ]]; then
+        args+=( --kv-cache-quantization --kv-cache-quantization-bits "${kvbits:-4}" )
+    fi
```

改动量：`1 file changed, 14 insertions(+), 1 deletion(-)`。

## 四、验证

```bash
cd ~/APP/llama.cpp
tools/engines.sh restart ornith-9b

# 1) capabilities 恢复
curl -s http://127.0.0.1:8084/v1/models | python3 -c \
  "import sys,json;m=json.load(sys.stdin)['data'][0];print(m['capabilities'])"
# → ['text', 'tools']

# 2) 实际 tool_calls 通路
# finish_reason: tool_calls
# tool_calls: [{"function":{"name":"get_weather",
#               "arguments":"{\"city\": \"Beijing\", \"unit\": \"c\"}"}}]
```

两项均通过。同时确认 KV 量化与 `--no-thinking` 已透传（`ps` 查看完整参数可见）。

## 五、影响范围

- **只影响 `engines.sh` 启动的辅助引擎**（`configs/*.conf` 中除 `active.conf` 外的全部）。
- 主后端（`manage.sh` → `active.conf`）**不受影响**，其参数路径本来就是完整的。
- 已在 `ornith-9b` 上验证；其他引擎若配置了 `RAPID_MLX_TOOL_PARSER`，重启后同样会从「静默失效」变为「正常生效」。

## 六、经验

1. **两个启动入口 = 参数腐化温床**。`manage.sh` 与 `engines.sh` 拼装同一后端命令行，
   任何一侧新增参数都必须同步另一侧。建议后续抽公共参数构造函数（当前仍是两处维护）。
2. **静默降级比崩溃更危险**。tool calling 失效不报错，只体现在 `capabilities` 少一项。
   运维验收应该把 `/v1/models` 的 `capabilities` 纳入健康检查，而不只看 `HTTP 200`。
3. 排查此类问题的第一步：用 `ps -p <pid> -o command=` 对比**预期参数**与**实际参数**，
   而不是只看配置文件。

## 七、相关

- [`docs/04-analysis-diagnostics/ornith-9b-memory-pressure-20261020.md`](../04-analysis-diagnostics/ornith-9b-memory-pressure-20261020.md)（同批次诊断）
- [`docs/06-reference-metrics/ornith-9b-capability-research-20261015.md`](../06-reference-metrics/ornith-9b-capability-research-20261015.md)
- `configs/ornith-9b.conf`
- `tools/engines.sh` / `manage.sh`
