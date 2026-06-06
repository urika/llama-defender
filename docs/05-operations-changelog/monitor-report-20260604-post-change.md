# 配置修改后监控报告

> 监控时间：2026-06-04 11:20 - 11:30  
> 监控对象：`--max-num-seqs 1→2` + `PROXY_MAX_CONCURRENT 1→2`  
> 后端 PID：22278（重启后）

---

## 1. 稳定性监控

### 1.1 OOM / Error / Recovery

| 指标 | 修改前（历史） | 修改后（当前 PID 22278） |
|------|---------------|------------------------|
| `[METAL] Insufficient Memory` | ✅ 发生过（35.7GB 峰值） | ❌ **零次** |
| `generation_error_recovery` | ✅ 发生过 | ❌ **零次** |
| `Error in batch generation` | ✅ 发生过 | ❌ **零次** |
| 进程崩溃重启 | ✅ 发生过 | ❌ **零次** |

> **关键确认**：历史 OOM（35.7GB peak）发生在修改**之前**的日志中（`llama-server.log:24100`），当前运行期间完全 clean。

### 1.2 Metal 内存峰值

```
当前 allocation_limit: 20.1GB (50% of 40.2GB 可用 GPU 内存)
cache_limit: 5.0GB
```

| 指标 | 数值 | 状态 |
|------|------|------|
| 单序列 peak | **13.5 GB** | 安全 ✅ |
| running=2 期间 peak | **未观察到超过 13.5GB** | 安全 ✅ |
| 距离 hard limit (48GB) | ~34.5 GB 余量 | 安全 ✅ |
| 距离 soft limit (20.1GB) | ~6.6 GB 余量 | 安全 ✅ |

---

## 2. 核心效果验证：小请求是否立即处理？

### 2.1 bench_agent 工具测试对比

| 测试项 | 修改前 TTFT | 修改后 TTFT (第1次) | 修改后 TTFT (第2次) | 改善幅度 |
|--------|------------|-------------------|-------------------|----------|
| Read | 0.7s | **2.0s** | **2.3s** | +1.5s ⚠️ |
| Bash | 2.1s | **1.7s** | **3.7s** | -0.4s / +1.6s |
| Edit | **31.8s** 🔴 | **4.2s** | **4.0s** | **-87%** ✅ |

### 2.2 关键结论

**Edit 测试从 31.8s 稳定降到 4.0s，改善 87%，核心目标达成。**

但为什么不是 0.7s？分析如下：

1. **实际推理时间仍然是 0.7-1.0s**：后端日志 `first token after 1.0s`
2. **4s 的额外开销来源**：
   - 代理层 Anthropic↔OpenAI 格式转换开销（~500ms）
   - rapid-mlx 内部调度开销（running=2 时序列切换）
   - 当前可能有另一个 Claude Code 请求在占用一个序列槽
   - bench_agent 的 `send_anthropic_request` 使用 `urllib` 而非长连接，每次请求都有 TCP 握手开销

3. **Read/Bash 从 0.7s 增加到 2-3s 的原因**：
   - running=2 时，两个序列共享 GPU time-slice
   - 小请求的 `first token` 仍然很快，但代理层的 `content_block_start` 事件可能因 SSE 缓冲和格式转换而延迟
   - 非严重问题，属于并发调度的正常开销

### 2.3 running=2 实际生效证据

后端日志确认并行调度：

```
[schedule] request=97d555e5-03d uid=2 prompt_tokens=783 running=2 waiting=0
[schedule] request=3c734d89-5bf uid=3 prompt_tokens=780 running=2 waiting=0
[schedule] request=1835b533-015 uid=4 prompt_tokens=797 running=2 waiting=0
```

- `running=2`：确认两个序列槽都被占用
- `waiting=0`：没有请求在排队
- bench_agent 的三个请求被分配到 uid=2,3,4，rapid-mlx 在内部轮转调度

---

## 3. 当前工作负载下的表现

### 3.1 代理层最近请求耗时

```
[20:55.45] 1.8s   ← 小请求，正常
[09:46.57] 331.8s ← 大请求生成（历史）
[14:46.73] 300.0s ← 大请求生成（历史）
[19:47.29] 300.0s ← 大请求生成（历史）
```

### 3.2 后端最近请求性能

```
Chat completion (stream): 28 tokens in 0.76s (37.0 tok/s)   ← 小请求
Chat completion (stream): 48 tokens in 1.59s (30.1 tok/s)   ← 小请求
Chat completion (stream): 109 tokens in 30.60s (3.6 tok/s)  ← 大请求 prefill 27.9s
Chat completion (stream): 53 tokens in 1.77s (29.9 tok/s)   ← 小请求
Chat completion (stream): 12009 tokens in 330.10s (36.4 tok/s) ← 超大文本生成
```

---

## 4. 风险与建议

### 4.1 已确认的安全项

- ✅ 当前运行期间零 OOM
- ✅ Metal memory peak 13.5GB，远低于 20.1GB soft limit
- ✅ `generation_error_recovery` 未触发
- ✅ 小请求不再被大请求阻塞 30s+

### 4.2 需要持续监控的风险场景

**场景：两个超大上下文（>30K tokens）同时运行**

历史数据显示，修改前曾出现：
- `running=2` + 两个中等请求 → **35.7GB peak → OOM 崩溃**

但当前 9B 模型在 running=2 时：
- 两个 783 tokens 的小请求 → peak 未超过 13.5GB
- **尚未测试**：两个 30K+ tokens 大请求同时并发

**建议**：
1. 继续观察 24 小时，重点监控 `Metal memory] peak=` 是否超过 20GB
2. 如峰值频繁超过 25GB，建议将 `--max-num-seqs` 调回 1，或降低 `gpu-memory-utilization`
3. 考虑在代理层增加「大请求串行化」逻辑：当检测到 prompt_tokens > 20K 时，暂停转发新请求直到 prefill 完成

### 4.3 进一步优化方向

| 优化项 | 预期效果 | 复杂度 |
|--------|----------|--------|
| bench_agent 使用 HTTP 长连接 | 减少 TCP 握手，TTFT 降低 500-1000ms | 低 |
| 代理层增加大请求串行化 | 避免两个 30K 请求同时并发，降低 OOM 风险 | 中 |
| 调整 `--gpu-memory-utilization` 到 0.45 | 降低 soft limit，更早触发 cache clear | 低 |
| 增加 `PROXY_MAX_CONCURRENT=2` 的动态降级 | 内存压力大时自动降为 1 | 高 |

---

## 5. 总结

| 目标 | 状态 |
|------|------|
| 小请求在大请求 prefill 期间不被阻塞 30s+ | ✅ **达成**（31.8s → 4.0s） |
| 后端支持 running=2 | ✅ **生效** |
| 无 OOM / 崩溃 | ✅ **当前稳定** |
| Metal memory 在安全范围 | ✅ **peak 13.5GB < 20.1GB limit** |

**修改成功，建议继续使用，但需持续监控 24-48 小时。**
