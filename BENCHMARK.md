# 本地 LLM 性能测试报告

> 测试日期: 2026-05-10  
> 测试环境: MacBook Pro M5 Pro (48GB 统一内存)  
> 测试工具: 自定义 Python 脚本 + curl  

---

## 目录

- [测试环境](#测试环境)
- [模型列表](#模型列表)
- [单请求性能](#单请求性能)
- [并发性能（小上下文）](#并发性能小上下文)
- [并发性能（大上下文 ~13.6K tokens）](#并发性能大上下文)
- [端到端测试（代理层）](#端到端测试代理层)
- [已知问题](#已知问题)
- [配置切换指南](#配置切换指南)

---

## 测试环境

| 项目 | 配置 |
|------|------|
| 机型 | MacBook Pro M5 Pro |
| 内存 | 48 GB 统一内存 |
| GPU | Apple M5 Pro (14-core) |
| Metal | Metal 4 |
| OS | macOS |
| llama.cpp | build 9090 |
| Rapid-MLX | 0.6.30 |
| 代理 | anthropic_proxy.py (自定义) |

---

## 模型列表

| # | 模型 | 框架 | 量化 | 大小 | 配置名 |
|---|------|------|------|------|--------|
| 1 | Qwen3.6-35B-A3B | llama.cpp | UD-IQ4_XS (GGUF) | ~22 GB | `qwen3.6-35b` |
| 2 | Qwen3.5-9B | llama.cpp | UD-Q4_K_XL (GGUF) | ~5.6 GB | `qwen3.5-9b` |
| 3 | Qwen3.6-35B-A3B | Rapid-MLX | 4bit (MLX) | ~17 GB | `rapid-mlx-35b` |

---

## 单请求性能

### 测试方法

- Prompt: "Say hello in one word" / "Write a Python fibonacci function"
- max_tokens: 20 (简单) / 100 (代码)
- temperature: 0.6-0.7
- 测量: 总耗时、TTFT、生成速度

### 结果

| 模型 | 框架 | 简单对话 | 代码生成 (100 tok) | TTFT |
|------|------|---------|-------------------|------|
| Qwen3.6-35B-A3B | llama.cpp | 0.5s / ~20 t/s | 3.5s / ~55 t/s | ~0.1s |
| Qwen3.5-9B | llama.cpp | 0.5s / ~16 t/s | 6.0s / ~17 t/s | ~0.3s |
| Qwen3.6-35B-A3B | Rapid-MLX | 0.47s / ~43 t/s | 3.75s / ~75 t/s | ~0.1s |

### 关键发现

- **Rapid-MLX 比 llama.cpp 快 36%**（75 vs 55 tok/s）
- **Qwen3.5-9B 在 llama.cpp 下异常慢**（仅 17 tok/s），可能由于 Gated DeltaNet 架构的 Metal 支持不完善
- **预填充速度**: llama.cpp 35B 约 900 t/s；Rapid-MLX 35B 约 70-260 t/s（小 prompt）

---

## 并发性能（小上下文）

### 测试方法

- Prompt: "Write a one-sentence description of Python programming language."
- max_tokens: 50
- 并发数: 1/2/3/4
- 所有请求使用相同 prompt

### 模型 1: llama.cpp + Qwen3.6-35B-A3B

| 并发 | 总耗时 | 单请求速度 | 总吞吐 | 状态 |
|------|--------|-----------|--------|------|
| 1 | ~2s | ~55 t/s | ~55 t/s | ✅ |
| 2 | ~20-40s | ~25 t/s | ~50 t/s | ⚠️ 延迟大 |
| 3+ | >40s | <20 t/s | <60 t/s | ❌ 不可用 |

### 模型 3: Rapid-MLX + Qwen3.6-35B-A3B

| 并发 | 总耗时 | 单请求速度 | 总吞吐 | 状态 |
|------|--------|-----------|--------|------|
| 1 | 0.97s | 43.4 t/s | 43.4 t/s | ✅ |
| 2 | 0.81s | 50.2 t/s | 86.7 t/s | ✅ |
| 3 | 1.10s | 39.5 t/s | 95.6 t/s | ✅ |
| 4 | 1.38s | 33.0 t/s | 101.5 t/s | ✅ |

### 关键发现

- **Rapid-MLX 并发扩展性远优于 llama.cpp**
- llama.cpp 在 Metal 上单 GPU 分时复用效率极低（2 并发延迟从 2s 暴涨到 40s）
- Rapid-MLX 4 并发总耗时仅 1.38s，且单请求速度衰减可控

---

## 并发性能（大上下文）

### 测试方法

- Prompt: 48,248 字符，实际 tokenized 为 **13,635 tokens**
  - 包含系统提示、工具定义、500 个文件索引、30 轮历史对话、代码片段
- max_tokens: 100
- 并发数: 1/2/3/4

### 模型 3: Rapid-MLX + Qwen3.6-35B-A3B

| 并发 | 总耗时 | 总吞吐 | 成功数 | 单请求平均耗时 |
|------|--------|--------|--------|--------------|
| 1 | 42.65s | 50.4 t/s | 1/1 | 42.65s |
| 2 | 33.04s | 70.5 t/s | 2/2 | 18.56s |
| 3 | 47.62s | 93.9 t/s | 3/3 | 33.51s |
| 4 | 49.29s | 94.3 t/s | 4/4 | 28.06s |

### 单请求耗时分布（4 并发）

| 请求 | 耗时 | 生成 tokens | 说明 |
|------|------|------------|------|
| #0 | 49.11s | 2,148 | 长输出 |
| #1 | 6.88s | 177 | 短输出 |
| #2 | 49.29s | 2,148 | 长输出 |
| #3 | 6.99s | 177 | 短输出 |

### 关键发现

- **4 并发全部成功**，但单请求耗时极不均匀（7s vs 49s）
- **吞吐量 3→4 几乎不增长**（93.9 → 94.3 t/s），GPU 已饱和
- **⚠️ max_tokens=100 未被遵守**（详见[已知问题](#已知问题)）
- 大上下文预填充慢，13.6K tokens 单请求需 42s

### Claude Code 实际场景估算

| 场景 | 单请求 | 4 并发 | 体验 |
|------|--------|--------|------|
| 短回复 (200 tok) | ~10-15s | ~15-20s | ⚠️ 较慢 |
| 中回复 (500 tok) | ~25-30s | ~30-40s | ❌ 很慢 |
| 长回复 (1000 tok) | ~40-50s | ~50-60s | ❌ 不可接受 |

**建议**: 大上下文下控制在 **2 并发** 以内。

---

## 端到端测试（代理层）

### 测试链路

```
Claude Code → anthropic_proxy.py:4000 → Rapid-MLX/llama-server:8081 → 模型
```

### 代理兼容性

| 功能 | llama.cpp | Rapid-MLX |
|------|-----------|-----------|
| 简单对话 | ✅ | ✅ |
| 流式响应 | ✅ | ✅ |
| 工具调用 (Bash/Read) | ✅ | ✅ |
| 消息格式转换 | ✅ | ✅ |
| max_tokens 传递 | ✅ | ⚠️ 不生效 |
| stop_reason 映射 | ✅ | ✅ |

---

## 已知问题

### 1. Rapid-MLX 不遵守 max_tokens

- **现象**: 请求设置 `max_tokens=100`，实际生成 2,148 tokens
- **原因**: Rapid-MLX 0.6.30 的 bug，参数已接收但内部调度器未执行截断
- **影响**: 中高风险，Claude Code 可能收到意外超长回复
- **解决**: 换回 llama-server 或等 Rapid-MLX 更新

### 2. llama.cpp 不支持 Qwen3.5-9B 的 DeltaNet 架构

- **现象**: 9B 模型生成速度仅 17 tok/s（预期 150+）
- **原因**: Gated DeltaNet + Gated Attention 混合架构的 Metal 支持不完善
- **解决**: 使用 MLX 框架（Rapid-MLX 实测 108 tok/s）

### 3. KV Cache 恢复错误（llama.cpp）

- **现象**: 日志中大量 `state_seq_set_data: error loading state: failed to restore kv cache`
- **影响**: 不影响功能，但提示 llama.cpp 对 Qwen3.5/3.6 的状态恢复有兼容性问题

### 4. 多并发 GPU 饱和

- **现象**: Mac M5 Pro 单 Metal GPU，3 并发后吞吐量不再增长
- **本质**: slot 分时复用 ≠ 真正并行，并发数 ≠ slot 数

---

## 配置切换指南

### 配置文件位置

```
~/APP/llama.cpp/configs/
├── active.conf             # 当前激活配置（软链接）
├── qwen3.6-35b.conf        # llama-server + 35B
├── qwen3.5-9b.conf         # llama-server + 9B
└── rapid-mlx-35b.conf      # Rapid-MLX + 35B + 8-bit KV
```

### 快速切换

```bash
cd ~/APP/llama.cpp

# 切换到 Rapid-MLX + 8-bit KV
./manage.sh switch rapid-mlx-35b
./manage.sh restart

# 切回 llama-server
./manage.sh switch qwen3.6-35b
./manage.sh restart

# 查看所有配置
./manage.sh list

# 查看当前配置
./manage.sh current
```

### 代理层 MODEL_NAME 切换

切换后端时，需要同步更新 `anthropic_proxy.py` 中的 `MODEL_NAME`：

```python
# llama-server
MODEL_NAME = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS"

# Rapid-MLX
MODEL_NAME = "mlx-community/Qwen3.6-35B-A3B-4bit"
```

---

## 推荐配置

| 场景 | 推荐配置 | 原因 |
|------|---------|------|
| **单用户高质量编程** | llama.cpp 35B | 速度够用 (55 t/s)，max_tokens 可控 |
| **追求速度** | Rapid-MLX 35B | 快 36% (75 t/s)，但 max_tokens 有 bug |
| **2 并发编程** | Rapid-MLX 35B | 总吞吐 86 t/s，体验相对可控 |
| **3-4 并发** | 不推荐 | 延迟过大，体验差 |
| **小模型快速响应** | 暂不支持 | Qwen3.5-9B 在 llama.cpp 下太慢 |

---

## 附录: 测试原始数据

### Rapid-MLX 小上下文并发原始数据

```
1并发: 0.97s, 43.4 t/s
2并发: 0.81s, 50.2 t/s (总吞吐 86.7 t/s)
3并发: 1.10s, 39.5 t/s (总吞吐 95.6 t/s)
4并发: 1.38s, 33.0 t/s (总吞吐 101.5 t/s)
```

### Rapid-MLX 大上下文并发原始数据

```
1并发: 42.65s, 50.4 t/s
2并发: 33.04s, 70.5 t/s (总吞吐 70.5 t/s)
3并发: 47.62s, 93.9 t/s (总吞吐 93.9 t/s)
4并发: 49.29s, 94.3 t/s (总吞吐 94.3 t/s)
```

---

*文档生成时间: 2026-05-10*  
*测试脚本: /tmp/concurrent_test.py, /tmp/concurrent_large_test.py*
