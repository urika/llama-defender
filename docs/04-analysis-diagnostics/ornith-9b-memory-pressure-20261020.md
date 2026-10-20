# Ornith-1.5-9B 速度劣化诊断：系统内存压力导致的换页（2026-10-20）

> **一句话**：9B 引擎本身没有故障，是**系统内存被耗尽**——模型权重（4.7 GB）被换到
> swap，每次解码都要从磁盘读回，900-token 生成从应达的 ~55 tok/s 掉到 **10.6 tok/s**。
> 释放内存并重启引擎后恢复 **55.8 tok/s（5.3×）**，`swapped_out` 归零。

---

## 一、问题现象

`ornith-ai/Ornith-1.5-9B-MLX-4bit`（`:8084`，rapid-mlx，262K ctx）在看似「正常」的状态下
严重劣化：引擎不崩溃、不报错、`/v1/models` 健康、CPU 占用极低，但生成速度与**生成长度强相关**。

| 测试 | 修复前 | 说明 |
|------|--------|------|
| 16 tok（短请求） | 0.29–0.37s | 正常 |
| 250 tok | 42–50 tok/s | 已低于基线 |
| 500 tok | 54.9 → **14.5 tok/s** | 同一次会话内衰减 |
| 900 tok | **10.6 / 12.3 tok/s** | 劣化最严重 |
| 质量门 62 条总耗时 | **421s** | p50 4.41s / p90 15.35s / max 62.1s |
| 单条长文档提取（A13） | **62.1s** | 慢到不可用 |

**关键特征**：同一进程内，速度随测试进行**持续下降**（250 tok 时 43 tok/s → 后续 300 tok 只剩 6.1 tok/s）。
这不是模型问题，而是外部资源在恶化。

## 二、诊断过程

### 2.1 先排除「引擎自身故障」

```bash
# 引擎存活、模型正确、上下文正常
ps -p 27023 -o etime=     # 19:27:42 —— 已稳定运行 19.5 小时
curl :8084/v1/models      # context_window=262144, capabilities=['text','tools']
# 无 OOM / 无 backpressure / 无崩溃
```

调度器日志显示 `running=1 waiting=0`——**没有排队**，是单请求本身就慢。

### 2.2 排除 CPU 回退

解码期间采样引擎 CPU 占用：

```
CPU% 采样: 0.1, 82.4, 49.4, 14.0, 7.2, 4.9, 4.4, 4.9
```

稳态仅 4–7%。若是 CPU 回退，会持续占满单核（~100%）。**这是 GPU 在等内存，不是 CPU 在算。**

### 2.3 定位到内存换页

```bash
vmmap -summary 27023
# Physical footprint:  5.7G
# Writable regions: resident=5.3G (80%)   swapped_out=4.8G (71%)
```

**引擎自身有 4.8 GB 被换出**——而模型权重正好 4.7 GB。每次解码都要把权重从 swap 读回。

系统侧同时满足换页条件：

| 指标 | 值 |
|------|-----|
| 物理内存 | 48 GB |
| Pages free | **0.2 GB** |
| Compressor | **15.4 GB** |
| Swap 已用 | **16.1 GB / 16.4 GB**（几乎满） |
| Pages wired down | 23.5 GB |

单次 300-token 解码期间：`swapins +780`、`pageins +3,181`。
前缀缓存也在被反复驱逐：

```
[prefix-pressure-evict] evicted 1 entries under cache-self pressure
(metal_cap=14.4GB, cache_max=1.9GB)
```

### 2.4 谁在占内存

| 进程 | footprint | 状态 |
|------|-----------|------|
| Splash 27B（`:8081`） | 15–19 GB | **生产后端**，30 请求/分钟 |
| **`com.agentgo.rsi-jev`（`:8022`）** | **8.27 GB** | **空转**，仅响应 `/health` |
| 9B 引擎（`:8084`） | 5.7 GB | 本次诊断对象 |
| 系统压缩器 | 12–15 GB | 内存压力的结果而非原因 |

根因明确：`configs/active.conf` 已写明 **「与 ornith-oq4e 互斥切换」「单引擎，与 Ornith 不可同跑」**，
但实际三个模型服务同时在跑。

## 三、处置

```bash
cd ~/APP/llama.cpp

# 1) 停掉 9B（本次先释放内存再重启，避免带着碎片状态恢复）
kill 27023            # SIGTERM，引擎会保存 prefix cache 后退出（约 11s）
rm -f .engines/ornith-9b.pid   # 清理陈旧 pidfile

# 2) 清理 9B 前缀缓存（约 310 MB，避免带着被反复驱逐的碎片启动）
rm -rf ~/.cache/rapid-mlx/prefix_cache/ornith-ai--Ornith-1.5-9B-MLX-4bit--*

# 3) 重启
tools/engines.sh start ornith-9b
```

> ⚠️ 重启时发现 `engines.sh` 有独立缺陷（缺 `--enable-auto-tool-choice` 导致 tool calling 静默失效），
> 已一并修复，详见 [`engines-sh-param-alignment-20261020.md`](../05-operations-changelog/engines-sh-param-alignment-20261020.md)。

## 四、修复效果

### 4.1 速度

| 长度 | 修复前 | 修复后 | 提升 |
|------|--------|--------|------|
| 100 tok | — | 50.6 tok/s | — |
| 250 tok | 42–50 | 54.2 | ↑ |
| 500 tok | 54.9 → 14.5（衰减） | **55.5** | 3.8× |
| 900 tok | **10.6** | **55.8** | **5.3×** |

**长度衰减消失**：900 tok 与 100 tok 速率一致（55.8 vs 50.6）。
连续 6 次 300 tok：`min=53.7 / p50=55.7 / max=55.7`，方差趋近于零
（修复前是 6–55 tok/s 的剧烈抖动）。

### 4.2 换页

```
修复前: swapped_out = 4.8 G (71%)
修复后: swapped_out = 0 K  (0%)
```

### 4.3 质量门（62 条金标）

| | 修复前 | 修复后 |
|---|---|---|
| overall | 88.7% | 87.1% |
| **总耗时** | **421s** | **92s（4.6×）** |
| p50 | 4.41s | **0.93s** |
| p90 | 15.35s | **2.56s** |
| max | 62.1s | **13.5s** |

质量持平（差值落在 `summary` 硬字数这一已知短板的波动区间内），速度大幅改善。

## 五、重要更正：`~100 tok/s` 口径不成立

此前多处文档（`BENCHMARK.md`、`ornith-9b-capability-research-20261015.md`）记录 9B 稳态 `~100 tok/s`。
本次实测**该数字不可达**，原因如下：

- 模型是 **dense 9B**（日志：`pure attention, throttle OFF, spec decode OFF`），4-bit 权重 **4.7 GB**。
- 解码是内存带宽受限：`tok/s ≈ 有效带宽 / 权重体积`。
- M5 Pro 标称内存带宽上限约 **307 GB/s** → 理论上限约 **65 tok/s**。
- 实测 55.8 tok/s ⇒ 有效带宽 ≈ 262 GB/s ≈ **理论上限的 85%**，已属良好。

因此 **55.8 tok/s 基本就是该硬件上该模型的物理上限**。`~100 tok/s` 可能是与其他模型
（如 35B-A3B MoE，仅 3B 激活参数）或不同量化/投机解码配置混淆所致。

> 已在 `BENCHMARK.md` 与 `ornith-9b-capability-research-20261015.md` 中同步更正。

## 六、测量方法学教训

1. **必须先预热**。冷启动首个请求（prefix cache miss + kernel 编译）曾出现
   `17.66s / 31 tokens`（≈2 tok/s）的假象；预热后同一请求 2.12s。
2. **不要在代理繁忙时测本地引擎**。本次曾用 `curl` 直接压 `:8081` 得到 11 tok/s，
   而同一时刻生产日志显示 **74.6–79.9 tok/s**——测试请求在与 5–6 万字符的真实请求抢同一引擎。
   **生产吞吐应读引擎自报日志/指标，不要用旁路压测代替。**
3. **优先看 `vmmap` 的 `swapped_out`**，比 `RSS` 更能反映「模型是否在内存里」。
4. **CPU% 是区分 CPU 回退与 GPU 等活动的高性价比信号**（低 CPU + 低吞吐 ⇒ 等内存/等带宽）。

## 七、运维建议

> **当前状态（2026-10-20）**：9B 引擎已**停止**（`tools/engines.sh status ornith-9b` → ⚪ 停止），
> 资源让给生产后端 Splash 27B；需要时按下列命令临时启动。

1. **9B 与 27B 不要同时常驻**（48 GB 机器）。二者互斥已在 `active.conf` 注明，应按需切换。
2. 本次诊断后已**持久禁用**空闲的 `com.agentgo.rsi-jev`（`:8022`，8.27 GB），
   释放约 8 GB；`jev` MCP（纯云端）不受影响。详见
   [`jev-mcp-tool.md`](../06-reference-metrics/jev-mcp-tool.md) §生命周期管理。
3. 若需临时启用 9B：
   ```bash
   cd ~/APP/llama.cpp && tools/engines.sh start ornith-9b
   tools/engines.sh status ornith-9b
   ```
   用完即停，避免与生产后端争内存。
4. 长生成（>500 tok）在内存紧张时最先劣化；若必须长输出，优先走云端 fallback
   （`configs/models.json` 的 `claude-haiku-4-5` 云端链：`glm-5.3-flash-cn` → `glm-5.3-flash`
   → `kimi-for-coding-highspeed` → `deepseek-flash`）。

## 八、相关

- [`engines-sh-param-alignment-20261020.md`](../05-operations-changelog/engines-sh-param-alignment-20261020.md)
- [`ornith-9b-capability-research-20261015.md`](../06-reference-metrics/ornith-9b-capability-research-20261015.md)
- [`BENCHMARK.md`](../06-reference-metrics/BENCHMARK.md)
- [`ornith-oq4e-performance-bottleneck-analysis-20260920.md`](./ornith-oq4e-performance-bottleneck-analysis-20260920.md)（同类长上下文瓶颈分析）
- [`TROUBLESHOOTING.md`](../06-reference-metrics/TROUBLESHOOTING.md)
