# Spark-X2.5-4B + llama-server 本地测试报告

**测试日期**: 2026-09-03  
**测试人**: OpenCode agent  
**仓库**: /Users/jinsongwang/APP/llama.cpp  
**目标**: 评估星火 Spark-X2.5-4B 官方 GGUF 在当前代理栈的可行性, 重点验证 llama-server 路径的 prefix cache 复用情况。

---

## 1. 模型与数据源

| 来源 | 仓库 | 格式 | 大小 |
|---|---|---|---|
| 官方 Chat | `XHToken/Spark-X2.5-4B` | BF16 / transformers | — |
| 官方 GGUF | `XHToken/Spark-X2.5-4B-GGUF` | GGUF | 7.66 GiB (BF16) |
| 社区 MLX | `abenzerps/Spark-X2.5-4B-MLX-4bit` | MLX 4-bit (custom code) | 2.31 GiB |

本次测试使用官方 GGUF: `models/Spark-X2.5-4B.gguf`。

模型关键参数:

- 架构: 自定义 `Spark2_5ForCausalLM`, `model_type=spark2_5`
- 参数量: 4B
- 原生上下文: 1,048,576 tokens
- 注意力: sliding_window(512) + full_attention 混合, 每 4 层 sliding 后 1 层 full
- 思考模型: 默认输出 `<think>` reasoning content
- 工具调用: chat template 内置 tools/`<tool_call>` 支持

---

## 2. 后端构建

当前 Homebrew 版 `llama-server` (build 1 / d4c8e2c) **无法加载** 该 GGUF:

```text
E llama_model_load: error loading model: unknown model architecture: 'spark2_5'
```

因为 llama.cpp 主线尚未合并 Spark2_5 支持。本次测试使用 PR:

- **ggml-org/llama.cpp #27868** — "Add Spark2_5 Model" (KnightYao)
- 构建命令:

```bash
git clone https://github.com/ggml-org/llama.cpp.git /tmp/llama.cpp-spark
cd /tmp/llama.cpp-spark
git fetch origin pull/27868/head:pr-spark
git checkout pr-spark
cmake -B build -DLLAMA_METAL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --target llama-server -j8
```

构建产物: `/tmp/llama.cpp-spark/build/bin/llama-server`。

---

## 3. 启动参数

```bash
llama-server \
  --model /Users/jinsongwang/APP/llama.cpp/models/Spark-X2.5-4B.gguf \
  --host 127.0.0.1 --port 8082 \
  --ctx-size 65536 --n-gpu-layers 999 \
  --verbose
```

GPU 层全部 offload 到 Apple Silicon Metal。

---

## 4. 功能验证

### 4.1 基础对话

- prompt: `"53乘以42等于多少？"`
- 输出: 完整 reasoning chain + 最终答案 `53 × 42 = 2226`
- 结论: 模型可正常推理并给出最终答案。

### 4.2 工具调用 (原生 `/v1/chat/completions`)

- prompt: `"北京天气怎么样？"`
- tools: `get_weather(city)`
- 输出:

```json
{
  "tool_calls": [{
    "type": "function",
    "function": {"name": "get_weather", "arguments": "{\"city\":\"北京\"}"},
    "id": "..."
  }]
}
```

结论: **llama-server 自带 tool parser 可直接解析 Spark 的 `<tool_call>` 输出**, 无需代理侧 XML parser。

### 4.3 Prefix cache 复用

用 8K 词系统提示构造 8.9K prompt, 第二请求仅改最后 user 消息:

| 指标 | Turn 1 (cold) | Turn 2 (warm) |
|---|---|---|
| prompt tokens | 8,909 | 8,911 |
| cached tokens | 0 | 8,904 |
| 耗时 | 7.10 s | 0.61 s |
| 加速比 | — | **~11.6×** |

结论: **llama-server 的 trimmable prefix cache 对 Spark2_5 有效**, 增量对话几乎完全复用前文。

---

## 5. 性能基准

测试环境: Apple M5 Pro, 48 GB 统一内存, llama-server Metal offload, ctx-size=65536。

| 上下文 | prompt tokens | cold TTFT | warm TTFT | cached/prompt | decode tok/s (128 tok) |
|---|---:|---:|---:|---:|---:|
| 1K 词 | 829 | 0.584 s | 0.078 s | 824/831 | 31.1 |
| 4K 词 | 3,509 | 1.816 s | 0.090 s | 3,504/3,511 | 30.8 |
| 16K 词 | 16,909 | 12.379 s | 0.117 s | 16,904/16,911 | 27.1 |
| 31K 词 | 33,909 | 26.546 s | 0.185 s | 33,904/33,911 | 23.7 |

观察:

- warm TTFT 与 prompt 长度基本无关 (仅处理新增 user 消息), 证明 prefix cache 命中极高。
- decode 速度随上下文增大略有下降, 从 31 → 24 tok/s, 属于正常 KV cache 增长开销。
- 4B 模型在 48GB Mac 上非常轻量, 可轻松开到 64K+ 上下文。

---

## 6. 与 rapid-mlx 路径的对比

| 维度 | rapid-mlx + Ornith-35B | llama-server + Spark-4B |
|---|---|---|
| 架构支持 | 仅内置标准架构 | PR #27868 支持 custom `spark2_5` |
| Prefix cache | non_trimmable, 增量对话 miss | trimmable, 近 100% 复用 |
| 推理速度 | prefill 主导, ~0.8 tok/s e2e | 轻量, decode ~30 tok/s |
| 工具调用 | 依赖代理侧 Qwen XML parser | llama-server 原生 JSON tool_calls |
| 显存占用 | ~20 GB | ~8 GB |
| 模型质量 | 35B MoE, SOTA bench | 4B, 适合轻量/长上下文任务 |

---

## 7. 已知问题

1. **当前 brew 版 llama.cpp 不支持 spark2_5**, 必须自行构建 PR #27868。
2. **Spark-X2.5 是 thinking 模型**, 默认输出 reasoning_content。尝试通过 chat template 关闭 thinking (`enable_thinking=false`) 会导致输出乱码, 说明 thinking 对该模型是强约束。
3. **代理集成未完整测试**: 本次仅在 8082 直接测试 llama-server; 在 4001 起第二代理实例时因进程扫描冲突导致状态混乱, 建议通过 `./manage.sh switch spark-x2.5-llama-server && ./manage.sh start` 单实例验证。

---

## 8. 配置与启动方式

已新增配置文件:

- `configs/spark-x2.5-llama-server.conf`

使用方式:

```bash
# 1. 确保已下载 GGUF 到 models/Spark-X2.5-4B.gguf
# 2. 确保 PATH 指向支持 spark2_5 的 llama-server (PR #27868 构建产物)
./manage.sh switch spark-x2.5-llama-server
./manage.sh start
```

---

## 9. 结论

- **Spark-X2.5-4B 可以接入当前代理栈, 推荐路径是 llama-server + 官方 GGUF**。
- rapid-mlx / mlx-lm 因自定义架构和缺少 `trust_remote_code` 支持, **不适合**。
- 该模型最大的亮点是 **1M 上下文 + llama-server 的 prefix cache 友好**, 在 48GB Mac 上跑长上下文增量对话成本极低。
- 作为 4B 模型, 它无法替代 35B 主模型做复杂 coding, 但可作为 **轻量长上下文副模型 / prefix-cache 基准测试参考**。
