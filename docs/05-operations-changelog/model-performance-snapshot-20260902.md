# 模型服务性能快照：Ornith-1.5-35B-A3B-oQ4e（2026-09-02）

> 记录时间：2026-09-02 23:55
> 后端：Rapid-MLX + `pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp`
> 数据来源：zcode `ctx_engine` 80-turn 门控验证（session `gatece35`）+ 后端日志 + `/api/session/*/metrics` + `logs/proxy_requests.jsonl` / `logs/diag/sessions.jsonl`

---

## 1. 运行配置

```text
命令行关键参数：
  rapid-mlx serve pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp \
    --host 127.0.0.1 --port 8081 \
    --enable-prefix-cache \
    --kv-cache-quantization --kv-cache-quantization-bits 4 \
    --hybrid-cache-entries 8 \
    --gpu-memory-utilization 0.70 \
    --cache-memory-mb 8192 \
    --max-num-seqs 1
```

代理侧：`PROXY_PIN_ENABLED=true`（临时开启验证），其余保持 active.conf 默认。

---

## 2. 延迟与吞吐

| 指标 | 数值 |
|---|---|
| 总轮次 | 80 |
| 端到端延迟 p50 | **2.69 s** |
| 端到端延迟 p90 | **19.0 s** |
| 端到端延迟 max | **28.8 s** |
| 平均输入字符数 | **181,000 chars** |
| 平均 prompt tokens | **19,500 tokens** |
| 最大 prompt tokens | **32,764 tokens** |
| 平均生成长度 | **15 tokens** |
| 后端自报吞吐（含 prefill） | **0.7–0.8 tok/s** |
| 估算 prefill 吞吐 | p50 约 **7K tok/s**；p90 约 **1K tok/s** |

说明：
- 生成长度极短（门控脚本有意为之），因此端到端时间几乎由 prefill 决定。
- rapid-mlx 未在响应体返回 `timings`，代理 `ttft_ms` 字段为 null；TTFT 只能通过总时长反推。

---

## 3. KV Cache / Prefix Cache

### 3.1 观测到的缓存状态

后端日志片段：

```text
[cache_fetch] LCP unavailable: shared=14017 entry_len=14094 requested_len=15060 non_trimmable=True
[cache_fetch] request=d8c960d8-b93 MISS prompt_tokens=15060
[cache_store] tokens=14094 ... cache_entries=8 cache_mem=1362MB
[cache_store] tokens=15076 ... cache_entries=8 cache_mem=1170MB
```

### 3.2 结论

- KV cache 已启用 4-bit 量化，后端 reported `cache_mem` 在 **1.17–1.36 GB** 之间。
- `hybrid-cache-entries=8` 生效，但条目是 **non_trimmable**（只整条精确匹配）。
- 上下文从 14,094 涨到 15,060 时，旧 snapshot 无法命中，导致 **全量冷 prefill**。
- 对“逐轮微量增长”的长会话，prefix cache 命中率极低，是当前最大性能瓶颈。

---

## 4. 上下文长度与压缩

| 指标 | 数值 |
|---|---|
| epoch_collapse 次数 | **3** |
| canonical_mismatch 次数 | **0** |
| 循环干预 `loop_l1` 注入次数 | **25** |
| `ctx_recall` 调用次数 | **0** |
| ContentCompressor 触发 | 未触发（`compress_enabled=false`） |

说明：
- 长度控制主要靠 `context_engine` 的 epoch 折叠，而非 stage-7 语义压缩。
- epoch 折叠后实际入模视图很小（最后一轮 `total_chars=2027`），但模型尚未主动调用 `ctx_recall` 召回被折叠内容。
- `canonical_mismatch=0` 说明 append-only canonical 会话视图稳定，无前缀缓存击穿风险。

---

## 5. 系统资源

| 指标 | 数值 |
|---|---|
| 后端 PID | 21997 |
| 后端 RSS | **5.6 GB**（%MEM 11.2） |
| 系统内存 free（memory_pressure） | **75%** |
| OOM / Metal 错误 | 无 |

---

## 6. 已知限制

1. rapid-mlx 不是 llama-server，`/api/backend/props` 与 `/api/backend/slots` 返回 `supported=false`，无法读取原生 KV slot 占用。
2. 后端不返回 `timings`，代理无法记录精确 TTFT / prompt_eval_ms。
3. hybrid prefix cache 的 `non_trimmable` 语义导致增量轮次几乎全 miss，需权衡 `--hybrid-cache-entries` 数量或等待 rapid-mlx 改进 trimmable 支持。

---

## 7. 后续可关注

- 评估 `--hybrid-cache-entries 16/32` 是否能提高 snapshot 命中率（代价是显存）。
- 观察模型是否会随着上下文增长开始调用 `ctx_recall`；若仍不调用，需检查 PDC-L1 指引文案或工具可见性。
- 若长上下文 prefill 持续占主导，可考虑切换至云端或启用更激进的 epoch 折叠阈值。

---

*本快照由代理自动采集并写入，原始数据保留在 `logs/proxy_requests.jsonl`、`logs/diag/sessions.jsonl` 与后端 stdout 日志中。*
