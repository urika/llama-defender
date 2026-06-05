# 配置修改记录：提升并发上限

> 修改时间：2026-06-04 11:15+  
> 修改人：AI Agent (Kimi Code CLI)  
> 关联分析：`docs/message-analysis-20260604.md`

---

## 修改背景

在分析 `bench-agent` Edit 工具测试时，发现其 **31.8 秒 TTFT** 并非推理延迟，而是被 Claude Code 的 32K tokens 大请求阻塞排队所致。根本瓶颈是：

- 后端 `--max-num-seqs 1`：GPU 同一时间只能处理 1 个序列
- 代理 `PROXY_MAX_CONCURRENT=1`：同一时间只能转发 1 个请求

当大请求占用 GPU 进行 27.9 秒 prefill 时，后续所有请求（包括小工具调用）必须排队等待。

---

## 可行性分析

| 对比项 | 35B 模型 | 9B 模型（当前） |
|--------|----------|----------------|
| 基础内存 | ~16-17 GB | **~6-8 GB** |
| KV 量化 | 8-bit | **8-bit** |
| 单序列 peak | ~33-39 GB | **19.1 GB** |
| `allocation_limit` | 0.60×48GB = 28.8GB | **0.50×48GB = 24GB** |
| 并发风险 | 两个大上下文 >38K 必 OOM | **余量更充裕** |

**结论**：9B 模型比 35B 小约 3 倍，单序列峰值仅 19.1GB，距离 24GB 软限制还有 **~5GB 余量**。两个序列共享模型权重（~4.5GB），额外开销主要来自 KV cache 和激活值。在 8-bit KV 量化下，第二个 32K 上下文序列的增量内存预计 **3-6GB**，总峰值约 **22-25GB**，处于安全边界。

**风险**：两个同时的超大上下文（>38K tokens）请求可能触及 `allocation_limit`，但 rapid-mlx 的 limit 是软限制，允许少量超发。如遇到 OOM 可回滚。

---

## 修改内容

### 文件：`configs/rapid-mlx-9b.conf`

#### 修改 1：后端并发序列数

```diff
- RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.50 --cache-memory-percent 0.10 --cache-memory-mb 4096 --max-num-seqs 1"
+ RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.50 --cache-memory-percent 0.10 --cache-memory-mb 4096 --max-num-seqs 2"
```

**作用**：允许 rapid-mlx 后端同时调度 **2 个序列**。当一个大请求在进行长 prefill 时，小请求（如工具调用、状态检查）可以并行进入 GPU，无需排队等待。

#### 修改 2：代理并发转发数

```diff
  # 代理并发控制
- PROXY_MAX_CONCURRENT=1
+ # 2026-06-04: 从 1 提升到 2，允许小请求在大请求 prefill 期间并行处理
+ # 9B 模型内存压力小，48GB 统一内存下两个并发安全
+ PROXY_MAX_CONCURRENT=2
```

**作用**：代理层的 `threading.Semaphore` 从 1 提升到 2，与后端并发能力保持一致。避免代理成为瓶颈。

---

## 预期效果

| 场景 | 修改前 | 修改后 |
|------|--------|--------|
| 大请求 prefill 期间发送小请求 | **排队 20-30s** | **立即处理（0.7s TTFT）** |
| 两个短请求同时到达 | 串行执行 | **并行执行** |
| 大请求 + 大请求同时到达 | 串行执行 | 并行执行（有 OOM 风险，需监控） |
| 代理吞吐能力 | 1 req/s（理论） | 2 req/s（理论） |

---

## 验证步骤

1. `./manage.sh restart` 重启后端 + 代理
2. `./manage.sh status` 确认状态正常
3. 发送两个并发小请求验证并行能力
4. 监控 `llama-server.log` 中的 `[Metal memory]` 峰值

---

## 回滚方案

如观察到以下现象，立即回滚：
- `[METAL] Insufficient Memory` 错误
- `generation_error_recovery` 频繁触发
- Metal 内存峰值持续超过 28GB

回滚命令：
```bash
# 恢复配置
git checkout configs/rapid-mlx-9b.conf
# 或手动改回 --max-num-seqs 1 和 PROXY_MAX_CONCURRENT=1
./manage.sh restart
```
