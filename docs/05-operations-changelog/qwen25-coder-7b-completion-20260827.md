# Qwen2.5-Coder-7B-8bit IDE 补全服务接入（2026-08-27）

> **背景**: Claude Code agentic 会话占用生产栈(8081 Ornith / 4000 llama-defender)时, 编辑器内联补全无法获得低延迟通道。引入独立 8083 端口的 Qwen2.5-Coder-7B 补全服务, 直连不经代理。

## 一、选型与验证

| 候选 | FIM 结果 | 结论 |
|---|---|---|
| **Qwen2.5-Coder-7B-Instruct-8bit** | ✅ FIM 三连(单行填充/中段编辑/裸前缀)全过; 中段题与 Qwen3-Coder-30B 逐字符一致; 热 TTFT **0.07s**, 冷(440tok) 1.97s | **采用** |
| Qwen2.5-Coder-7B-4bit | ❌ checkpoint 损坏(FIM 出 "lo lo lo"、chat 答非所问) | 已删缓存, 勿再下 |
| Qwen3-Coder 家族 | 无 8B 档; 本机 Qwen3-8B-4bit FIM 不合格(instruct 对齐压掉填充能力) | 不采用 |

## 二、架构定位

```
编辑器 FIM 请求 → POST 127.0.0.1:8083/v1/completions → rapid-mlx (Qwen2.5-Coder-7B-8bit)
                 (直连, 不经 llama-defender)
```

- **独立端口 8083**: 与生产(8081 Ornith / 4000 代理)隔离。
- **为何不经代理**: 代理 `MAX_CONCURRENT=1` 会把补全排队到 agentic 请求之后; 且代理的截断/压缩管线对 FIM 请求无意义。
- **与 Ornith 共存**: 合计 ~30GB, 48GB 机器宽裕; 但 **Ornith 批跑前必须停本服务**(批跑 KV 可涨至 27GB+, 叠加贴物理上限, 有 swap 挤兑事故形态, 见 2026-08-22 记录)。

## 三、配置与文件

| 文件 | 说明 |
|---|---|
| `configs/qwen25-coder-7b-8bit.conf` | 8083 端口、gpu-mem 0.22(权重 8.5GB→cap ~10.6GB)、cache 1536MB、max-num-seqs 1、prefix cache 开、KV 量化关 |
| `tools/coder7b.sh` | start/stop/status/probe 启停脚本(不动 active.conf, stdlib 即可运行) |
| `tools/bench_ctx_ladder_8083.py` | 上下文长度×性能阶梯压测(直连后端, 测 TTFT/prefill/decode) |

**关键配置决策**:
- `RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.22 --cache-memory-mb 1536 --max-num-seqs 1"` — D-METAL-CAP 准入检查要求 cap 必须大于权重, 否则全部 503。
- `RAPID_MLX_ENABLE_PREFIX_CACHE=true` — 编辑器同文件反复补全, 热 TTFT 0.07s 靠它。
- `RAPID_MLX_KV_QUANTIZATION=false` — 8bit 模型 KV 用 FP16 即可, 少一个变量。

## 四、FIM 调用契约

```
prompt = "<|fim_prefix|>{光标前文}<|fim_suffix|>{光标后文}<|fim_middle|>"
stop   = ["<|fim_end|>", "<|endoftext|>"]
端点   = POST http://127.0.0.1:8083/v1/completions
```

## 五、操作注意

1. 服务启动: `tools/coder7b.sh start`; 停止: `tools/coder7b.sh stop`(Ornith 批跑前必做)。
2. 独立 PID 文件 `.coder7b.pid`, 与生产进程完全隔离。
3. 该实验曾遗留一个运行 5h 的 8083 进程, 被 `manage.sh restart` 的进程探测误判为"后端已在运行"导致 Ornith 后端未启动——已停掉; 复现此类问题优先检查 `lsof -Pi :8083`。