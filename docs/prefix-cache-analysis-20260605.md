# Prefix Cache (KV Cache) 深度分析与 TurboQuant 测试记录

> 分析日期：2026-06-05
> 分析目标：理解 Rapid-MLX prefix cache 在当前 agentic 工作流下的命中情况，并测试 TurboQuant 压缩配置

---

## 1. 背景

后端：`rapid-mlx` v0.6.71 + Qwen3.5-9B-MLX-4bit  
配置：`PROXY_MAX_CONCURRENT=1`, prefix cache enabled, 8-bit KV quantization  
现象：`/status` 页面显示 prefix cache 命中率 0.0%

---

## 2. 分析过程

### 2.1 日志数据来源

- 后端日志：`logs/llama-server.log`
- 代理日志：`logs/anthropic_proxy.log`
- 请求报文：`/tmp/anthropic_request_body.json`
- 历史报文：`/tmp/anthropic_requests/*.json`

### 2.2 分析工具

编写了 `tools/cache_analyzer.py`，解析 `cache_fetch` / `schedule` 行，计算 HIT/MISS 统计、prefill 节省率、prompt 长度分布。

---

## 3. 关键发现

### 3.1 历史总命中率：49.3%

| 指标 | 数值 |
|------|------|
| HIT | 769 |
| MISS | 791 |
| Hit Rate | 49.3% |
| Prefill Savings (HIT时) | 96.1% |
| Avg cached/HIT | 27,226 tokens |
| Avg prefill/HIT | 1,109 tokens |

历史 HIT 高度集中在固定长度请求（31404/31579/31570 tokens，占 70%+），主要来自 benchmark/测试查询。

### 3.2 当前运行命中率：0.0%

当前启动后所有请求全部 MISS，schedule 行均为 `tokens_to_prefill=XXXXX`（无 cached 后缀）。

### 3.3 根因：代理截断策略破坏前缀稳定性

**直觉误区**：多轮对话应该是"前面固定 + 尾部追加"。

**实际验证**：
- 原始报文（代理保存的完整请求）：相邻请求共同前缀 **97.0%**（64/66 条消息相同）
- 截断后报文（后端实际接收）：相邻请求共同前缀仅 **19%-35%**（5-9/26 条消息相同）

**破坏机制**：

| 机制 | 影响 |
|------|------|
| `PROXY_CTX_TRUNCATE_STRATEGY=rounds` | 保留"最近 N 轮"对话，窗口随每轮请求整体右移 |
| `PROXY_CTX_KEEP_ROUNDS=10` | 当 token 预算（40K * 1.3 = 52K chars）超限时，动态削减轮次 |
| `PROXY_TOOL_KEEP=10` | Tool Clearing 删除旧工具结果内容，改变消息序列 |
| 动态 System Message | `The date has changed...`、`The task tools haven't been used recently...`、`Stop hook feedback` 等消息插入对话中间 |

**结论**：代理为保证上下文窗口不溢出，不断重构消息序列，恰好破坏了 prefix cache 所需的前缀稳定性。这是 **agentic 工作流与 prefix cache 机制的根本冲突**。

### 3.4 Rapid-MLX 缓存实现细节

- **磁盘持久化**：lifespan 级别（启动加载、退出保存），不支持运行时实时交换到磁盘
- **缓存键**：似乎基于完整消息序列匹配（非逐 token 前缀树），因为即使是部分共同前缀也无命中记录
- **Boundary Snapshot**：在消息边界保存快照，但仅用于同请求内重计算，不支持跨请求前缀匹配
- **LRU 淘汰**：`cache_mem` 超出 `--cache-memory-mb` 限制时触发

---

## 4. TurboQuant 配置测试

### 4.1 修改内容

文件：`configs/rapid-mlx-9b.conf`

```diff
-RAPID_MLX_KV_QUANTIZATION=true
-RAPID_MLX_KV_QUANT_BITS=8
+RAPID_MLX_KV_QUANTIZATION=false
+RAPID_MLX_KV_QUANT_BITS=8

-RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.50 --cache-memory-percent 0.10 --cache-memory-mb 4096 --max-num-seqs 1"
+RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.50 --cache-memory-mb 8192 --max-num-seqs 1 --kv-cache-turboquant --kv-cache-turboquant-bits 4 --pin-system-prompt"
```

### 4.2 生效验证

启动日志确认：
```
✅ MemoryAwarePrefixCache initialized: max_memory=8192.0MB
✅ TurboQuant V-cache: 4-bit, group_size=32 (K stays FP16)
✅ Features: ..., pin-system-prompt
```

### 4.3 内存压缩效果

| 指标 | 之前 (8-bit, 4096MB) | 现在 (TurboQuant 4-bit, 8192MB) |
|------|----------------------|--------------------------------|
| 新条目内存 (60K tokens) | ~2200MB (估算) | **327MB** (实测增量) |
| 每 token 内存 | ~32-37 KB | **5.4 KB** |
| 压缩倍数 | 基准 | **~6x** |

> 注：磁盘上的旧缓存文件（之前用 8-bit 保存）仍保持原大小，新保存的条目才应用 TurboQuant 压缩。

### 4.4 运行稳定性

| 指标 | 数值 | 状态 |
|------|------|------|
| Metal memory peak | 18.4GB | ✅ 正常（limit=20.1GB） |
| 生成速度 | 19.9 tok/s | ✅ 正常 |
| TTFT (58K tokens) | 69.5s | ⚠️ 预期内 |
| OOM / forced cache clear | 无 | ✅ 正常 |

### 4.5 命中率

当前运行仍保持 0% 命中率（与 TurboQuant 无关，由截断策略导致）。

---

## 5. 磁盘持久化机制结论

**问题**：缓存什么情况下存磁盘？是否可以配置多写入磁盘节省内存？

**答案**：
- Rapid-MLX prefix cache 的磁盘持久化是 **lifespan 级别**：进程启动时自动加载、进程退出时自动保存
- **不支持**运行时实时交换到磁盘，也没有配置参数控制持久化频率
- 运行时缓存完全驻留内存，受 `--cache-memory-mb` 限制
- 磁盘缓存路径：`~/.cache/vllm-mlx/prefix_cache/<model_hash>/`
- 格式：Safetensors，每个条目独立文件

---

## 6. 最终结论与建议

### 6.1 Agentic 工作流下的 Prefix Cache

在当前 Claude Code agentic 编程工作流中：
- **Prefix Cache 命中率天然受限**（当前 0%）
- 核心价值体现在：固定 prompt 的 benchmark/测试、同一请求重试、并发请求共享前缀
- 不是系统故障，而是**截断策略与缓存机制的根本冲突**

### 6.2 TurboQuant 建议

**建议保留当前 TurboQuant 配置**：
- 内存占用降低 ~6 倍，相同预算可容纳 6 倍缓存条目
- 运行稳定，无精度或速度退化（短期观察）
- `pin-system-prompt` 有助于在 LRU 淘汰时保护系统提示
- 8192MB cache memory 在 48GB 统一内存下安全（Metal peak 18.4GB + cache 8.4GB ≈ 27GB）

**风险**：TurboQuant 标注为 "Experimental"，长期稳定性需持续观察。

### 6.3 如需提升命中率

如需在 agentic 场景下提升 prefix cache 命中率，需从**代理层**调整：
- 增加 `PROXY_CTX_KEEP_ROUNDS`（减少截断频率，但增大上下文压力）
- 增大 `PROXY_CTX_CHARS_LIMIT` 或 `PROXY_CTX_TOKEN_BUDGET`
- 减少 `PROXY_TOOL_KEEP` 的清理频率
- 权衡：这些调整会增加上下文窗口压力，可能导致 OOM

---

## 7. 相关文件

- 分析脚本：`tools/cache_analyzer.py`
- 修改配置：`configs/rapid-mlx-9b.conf`
- 本文档：`docs/prefix-cache-analysis-20260605.md`
