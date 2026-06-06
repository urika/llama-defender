# 配置修改记录：回滚并发上限

> 回滚时间：2026-06-04 13:00+  
> 回滚原因：内存压力过高，风险大于收益

---

## 回滚背景

2026-06-04 11:15 将 `--max-num-seqs` 从 1 提升到 2，`PROXY_MAX_CONCURRENT` 从 1 提升到 2。

经过 1.5 小时实际运行监控，发现内存压力过大，决定回滚。

---

## 回滚前数据（`--max-num-seqs 2` 运行 1.5h）

| 指标 | 数值 | 评估 |
|------|------|------|
| Metal memory peak | **26.7 GB** | 超过 20.1GB soft limit |
| Metal memory avg | **25.2 GB** | 持续高压运行 |
| forced cache clear | **44 次** / 1.5h | 频繁清除前缀缓存 |
| running=2 实际频率 | **3.0%** (7/230) | 并发场景极少 |
| 生成速度 | 12.7 tok/s (正常 30 tok/s) | 下降 58% |
| OOM / 崩溃 | 0 次 | 未崩溃，但处于危险边缘 |

### 关键问题

1. **内存压力翻倍**：单序列时 peak 13.5GB，但 rapid-mlx 在 running=2 场景下（即使实际只运行 1 个序列）内存膨胀到 26.7GB。推测是 `--max-num-seqs 2` 改变了内部内存分配策略，预留了更多空间。

2. **forced cache clear 频繁触发**：44 次 cache clear 意味着前缀缓存几乎无法稳定积累，严重影响了缓存命中率。

3. **收益极低**：running=2 实际发生频率仅 3%，Claude Code 的对话本质是串行的，修改对日常使用的加速效果微乎其微。

4. **生成速度下降**：内存压力下生成速度从 30 tok/s 降到 12.7 tok/s。

---

## 回滚内容

### 文件：`configs/rapid-mlx-9b.conf`

```diff
- RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.50 --cache-memory-percent 0.10 --cache-memory-mb 4096 --max-num-seqs 2"
+ RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.50 --cache-memory-percent 0.10 --cache-memory-mb 4096 --max-num-seqs 1"

  # 代理并发控制
- # 2026-06-04: 从 1 提升到 2，允许小请求在大请求 prefill 期间并行处理
- # 9B 模型内存压力小，48GB 统一内存下两个并发安全
- PROXY_MAX_CONCURRENT=2
+ # 2026-06-04: 曾尝试提升到 2，但 running=2 时内存 peak 达 26.7GB，
+ # 频繁触发 forced cache clear（44次/1.5h），生成速度下降 50%+。
+ # 回滚到 1，风险收益比更优。
+ PROXY_MAX_CONCURRENT=1
```

---

## 回滚后预期

| 指标 | 预期 |
|------|------|
| Metal memory peak | 回到 13-15GB |
| forced cache clear | 显著减少或消失 |
| 前缀缓存命中率 | 恢复 |
| 生成速度 | 恢复到 30 tok/s |
| bench_agent Edit TTFT | 回到 30s+（小请求阻塞问题复现） |

---

## 教训

1. `--max-num-seqs` 不仅影响并发能力，还影响 rapid-mlx 的**内部内存分配策略**
2. 即使 running=2 很少发生，参数本身就会导致内存预留增加
3. 对于单用户串行对话场景，`--max-num-seqs 1` 是更优选择
4. 未来如需并发，应优先考虑硬件升级（64GB+）而非参数调优
