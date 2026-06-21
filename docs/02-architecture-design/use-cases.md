# 目标场景使用案例 (Use Cases)

> **日期**: 2026-06-21  
> **适用版本**: v0.5.7+ (Phase 0 模块拆分后)  
> **硬件**: MacBook Pro M5 Pro, 48GB unified memory, 15-core CPU  
> **核心定位**: 本地 LLM 推理编排层，为 Claude Code agentic coding 工作流提供 Anthropic 兼容 API

---

## 场景总览

```
┌─────────────────────────────────────────────────────────────┐
│                     使用案例矩阵                              │
├──────────┬──────────────────┬──────────┬─────────────────────┤
│          │  低复杂度         │  中复杂度 │  高复杂度           │
├──────────┼──────────────────┼──────────┼─────────────────────┤
│  高频    │ A: 日常编码       │ D: 长上下文 │ B: 本地↔云端热切换  │
│  中频    │ E: 模型选型评测   │          │ C: 多模型分工        │
│  低频    │ F: 并发压测验证   │          │ G: 故障恢复与自愈    │
└──────────┴──────────────────┴──────────┴─────────────────────┘
```

---

## 案例 A: 日常 Agentic 编码工作流（主场景）

### A.1 场景描述

开发者使用 Claude Code 进行日常软件工程任务（修 bug、加功能、重构），由本地 Qwen3.6-35B-A3B 模型提供推理能力。代理层自动管理上下文膨胀、工具循环检测和 OOM 防护。

### A.2 前置条件

```bash
# 1. 确认 35B 配置已激活
./manage.sh current          # 应显示 rapid-mlx-35b-opt

# 2. 启动服务（后端 + 代理）
./manage.sh start

# 3. 验证就绪（等待 "API: 正常" 出现，可能需要 30-60s 模型加载）
./manage.sh status
# 预期输出:
#   后端: rapid-mlx  PID: xxxx  内存: ~16 GB  API: 正常
#   代理: 运行中     地址: http://127.0.0.1:4000

# 4. Claude Code 已指向 127.0.0.1:4000（无需修改客户端配置）
```

### A.3 典型工作流

```
开发者 → Claude Code → "修复 auth.py 中的 SQL 注入漏洞"
                          │
                          ├─ [轮 1-5] 探索阶段
                          │   Read(auth.py), Grep("query"), Glob("test/*")
                          │   代理层: 工具过滤 44→15 tools, 节省 ~8K tokens/请求
                          │   上下文: ~15K chars → INIT 阶段，无压缩
                          │
                          ├─ [轮 6-15] 实施阶段
                          │   Edit(auth.py), Bash(pytest), Read(result)
                          │   代理层: 上下文 ~50K chars → GROWTH 阶段
                          │   自动压缩: tail-40% tool_result 清除 + thinking 剥离
                          │   每轮节省 ~3K chars
                          │
                          ├─ [轮 16-30] 验证阶段
                          │   多轮 Bash 测试 + Read 日志
                          │   代理层: 上下文 ~90K chars → EXPANSION 阶段
                          │   Rounds 截断: 保留最近 10 轮 + Read 结果智能保留
                          │   增量摘要: 旧消息压缩为 summary，不是简单丢弃
                          │
                          └─ [轮 31+] 如果循环
                              模型重复 Read 同一文件 3+ 次
                              代理层: Level 1 提示 → Level 2 移除工具 → Level 3 纯文本
```

### A.4 关键代理层行为（开发者无感知）

| 行为 | 触发条件 | 效果 | 对应配置 |
|------|---------|------|---------|
| 工具定义过滤 | tools > 20 | 44→15 tools，省 ~8K tokens | `PROXY_TOOL_FILTER_ENABLED=true` |
| Tool-result 清除 | chars > 40K (GROWTH) | 旧 tool_result 替换为 200 字预览 | `PROXY_CLEAR_ENABLED=true` |
| Thinking 剥离 | chars > 40K | 移除旧 thinking blocks | `_compress_content_pass()` |
| Rounds 截断 | chars > 90K (EXPANSION) | 保留最近 10 轮，中间消息压缩摘要 | `PROXY_CTX_KEEP_ROUNDS=10` |
| 循环检测 | 连续 3 次相同 tool_use | Level 1 提示注入 | `PROXY_LOOP_THRESHOLD=3` |
| Blocker 检测 | 连续 2 次同类型错误 | `[BLOCKER]` 消息注入 | `PROXY_BLOCKER_THRESHOLD=2` |
| OOM 预截断 | chars > 200K | 强制 keep_rounds=2 | `PROXY_OOM_SAFE_CHARS=200000` |
| 请求体硬限制 | Content-Length > 500KB | 413 拒绝（防 Metal OOM） | `PROXY_MAX_REQUEST_BYTES=512000` |

### A.5 预期性能（基于实测基线）

| 指标 | 值 | 说明 |
|------|-----|------|
| TTFT (15K context) | ~8-12s | INIT 阶段，冷启动 |
| TTFT (90K context) | ~30-50s | EXPANSION 阶段 |
| 生成速度 | ~25-35 tok/s | 35B MoE 4-bit |
| 单轮耗时 | ~10-60s | 取决于上下文长度 + 输出长度 |
| 30 轮会话总耗时 | ~15-30 min | 含工具调用往返 |
| 内存占用 | ~14-18 GB | 模型 + KV cache + prefix cache |

### A.6 监控与诊断

```bash
# 实时查看代理处理日志
./manage.sh proxy-logs 50

# 查看后端性能日志
./manage.sh logs 50

# 查看状态页（浏览器）
open http://127.0.0.1:4000/status

# 查看结构化 metrics
curl -s http://127.0.0.1:4000/metrics?n=20 | python3 -m json.tool

# Metal 内存实时监控
./manage.sh monitor 5    # 每 5 秒刷新

# 关键指标含义:
#   REQ_SUMMARY chars=XXXXX tools=XX  → 每请求的输入规模
#   Tool clearing: N cleared, M chars freed  → 压缩效果
#   Context truncation: N dropped, M kept     → 截断效果
#   quality_flags=[loop_injected, ...]        → 质量告警
```

### A.7 何时应切换到其他案例

| 信号 | 建议切换到 |
|------|-----------|
| TTFT > 60s，上下文持续膨胀 | 案例 D（长上下文优化）或案例 B（切云端） |
| 模型输出质量下降（代码不完整、逻辑错误） | 案例 E（质量评测）或案例 B（切云端对比） |
| 需要 JSON/CSV 数据处理（非编码任务） | 案例 C（切换到 Gemma-26B） |
| 需要快速原型验证（不在乎质量） | 案例 C（切换到 Qwen3-8B） |

---

## 案例 B: 本地↔云端热切换（成本优化）

### B.1 场景描述

本地模型免费但速度慢、上下文有限；云端 API（DeepSeek）快但按 token 付费。利用代理的热重载能力（~0.5s 切换，不中断 proxy 进程），根据任务复杂度动态切换后端。

### B.2 成本模型

```
本地 (rapid-mlx-35b):
  金钱成本: ¥0/请求
  时间成本: ~30s/轮（含 TTFT + 生成）
  上下文限制: ~180K chars（OOM 风险）
  适用: 日常编码、探索、迭代

云端 (deepseek-chat / deepseek-v4-pro):
  金钱成本: ~¥0.01-0.05/请求（56K tokens 请求）
  时间成本: ~3-8s/轮（网络 + 推理）
  上下文限制: ~1M+ tokens（无 OOM 风险）
  适用: 复杂推理、长上下文、质量关键任务
```

### B.3 切换工作流

#### 本地 → 云端（当本地遇到瓶颈时）

```bash
# 场景: 本地会话已 30+ 轮，TTFT > 60s，需要快速完成
# 或: 需要 > 180K chars 的超长上下文分析

# 步骤 1: 切换配置（仅改 symlink，~0s)
./manage.sh switch deepseek-chat

# 步骤 2: 热重载（SIGHUP，proxy 进程不重启，~0.5s)
./manage.sh reload
# proxy 输出: [RELOAD] OK: backend=cloud base=https://api.deepseek.com/v1 ...

# 步骤 3 (可选): 释放本地 GPU 内存
./manage.sh stop-backend
# 内存从 ~16 GB 降到 ~0.5 GB

# Claude Code 无需任何操作——下一轮请求自动走云端
```

#### 云端 → 本地（任务完成后回到免费模式）

```bash
# 步骤 1: 预启动本地后端（需要 30-60s 模型加载，可提前做）
./manage.sh start-backend &
# 等待 "API: 正常" 出现

# 步骤 2: 切换配置 + 热重载
./manage.sh switch rapid-mlx-35b-opt && ./manage.sh reload

# 完成。Claude Code 下一轮请求自动走本地
```

### B.4 混合策略（推荐）

```
会话开始
  │
  ├─ [探索阶段] 本地 35B
  │   低成本迭代，不介意等待
  │   代理自动管理上下文膨胀
  │
  ├─ 触发切换信号:
  │   - TTFT > 60s（上下文过长）
  │   - 代理日志出现 "OOM_DANGER" 或 "PRE_TRUNC"
  │   - 需要高质量输出（如生成最终报告）
  │   - 连续 3+ 次工具循环无法恢复
  │
  ├─ [关键阶段] 云端 DeepSeek
  │   快速完成复杂推理
  │   生成高质量代码或分析报告
  │   成本: ~¥1-3（20 请求 × 56K tokens）
  │
  └─ 完成 → 切回本地
```

### B.5 关键注意事项

| 注意点 | 说明 |
|--------|------|
| **API Key** | 云端模式需要真实 API Key，存于 `configs/secret.local.conf`（git-ignored） |
| **上下文不丢失** | 切换后 Claude Code 的下一次请求仍携带完整会话历史，代理的截断/压缩对云端配置更宽松 |
| **并发提升** | 云端默认 `PROXY_MAX_CONCURRENT=4`（vs 本地 1），可并行处理 |
| **Tool Clearing** | 云端默认关闭（1M+ 上下文，无需压缩） |
| **成本监控** | `./manage.sh proxy-logs` 中的 `REQ_SUMMARY` 行显示每请求的 chars/tools 数 |

---

## 案例 C: 多模型分工（任务路由）

### C.1 场景描述

不同模型擅长不同任务。利用代理的热切换能力，在单个工作日内根据当前任务类型选择最优模型，而非一个模型干所有事。

### C.2 模型能力矩阵

| 配置名 | 模型 | 内存 | 擅长 | 不擅长 | 适用场景 |
|--------|------|------|------|--------|---------|
| `rapid-mlx-35b-opt` | Qwen3.6-35B-A3B (MoE) | 14-18 GB | 通用编码、多语言、推理 | 纯数学、超长上下文 | **默认选择**，平衡质量和速度 |
| `mlx_vlm-27b` | Qwen3.6-27B-OptiQ (Dense) | 12-16 GB | 密集推理、SWE-bench | 速度稍慢 | 复杂 bug 修复、架构设计 |
| `qwen2.5-coder-14b` | Qwen2.5-Coder-14B | 8-10 GB | 代码补全、语法修复 | 复杂推理、非代码任务 | 快速代码编辑、简单重构 |
| `gemma4-26b` | Gemma-4-26B-A4B | 14-16 GB | 数据处理、JSON/CSV、格式化 | 复杂编码 | 日志分析、数据转换 |
| `qwen3-8b` | Qwen3-8B | 4-5 GB | 快速原型、简单问答 | 复杂任务 | 测试代理功能、快速验证 |

### C.3 典型工作日时间线

```bash
# === 上午: 编码任务（默认 35B） ===
./manage.sh switch rapid-mlx-35b-opt && ./manage.sh reload
# Claude Code: 修复 3 个 bug，加 1 个 feature
# 预期: 每轮 10-30s，30 轮会话 ~20 min

# === 下午: 日志分析（切 Gemma-26B） ===
./manage.sh switch gemma4-26b && ./manage.sh reload
# Claude Code: "分析 logs/proxy_metrics.jsonl，统计 500 错误率趋势"
# Gemma 擅长 JSON/CSV 处理，Temp=0.2 确保格式准确
# 注意: Gemma 的 reasoning-parser 是 "gemma4"，已正确配置

# === 下午: 快速原型验证（切 8B 省时间） ===
./manage.sh switch qwen3-8b && ./manage.sh reload
# Claude Code: "写一个 hello world 的 FastAPI 服务"
# 8B 模型 4-5 GB 内存，TTFT ~2-3s，适合简单任务快速迭代
# 并发=4，可同时处理多个小请求

# === 傍晚: 复杂重构（切 27B Dense） ===
./manage.sh switch mlx_vlm-27b && ./manage.sh reload
# Claude Code: "重构整个认证模块，从 session 改为 JWT"
# 27B Dense 模型推理更深，适合架构级变更
# 4-bit KV cache + PagedCache，支持更长上下文

# === 收工前: 回到默认 ===
./manage.sh switch rapid-mlx-35b-opt && ./manage.sh reload
```

### C.4 切换决策树

```
当前任务是什么?
  │
  ├─ 编码/修 bug/加功能
  │   ├─ 简单（单文件、< 50 行改动）→ qwen2.5-coder-14b (快)
  │   ├─ 复杂（多文件、架构变更）  → mlx_vlm-27b (深)
  │   └─ 一般                      → rapid-mlx-35b-opt (默认)
  │
  ├─ 数据处理/日志分析
  │   └─ gemma4-26b (JSON/CSV 专长)
  │
  ├─ 快速验证/测试
  │   └─ qwen3-8b (最快最省)
  │
  └─ 复杂推理/长报告
      └─ deepseek-chat (云端，见案例 B)
```

### C.5 注意事项

- **每次切换需 reload**：`switch` 只改 symlink，`reload` 才让 proxy 生效（~0.5s）
- **后端需重启**：`switch` + `reload` 只切换代理路由；如果本地后端模型不同，需要 `stop-backend && start-backend`（30-60s 模型加载）
- **内存不可共存**：48GB 机器同一时刻只能加载一个本地模型（35B 占 16GB，无法同时跑 27B）
- **并发差异**：35B/27B 并发=1；14B 并发=2；8B 并发=4

---

## 案例 D: 长上下文代码库分析

### D.1 场景描述

需要让模型理解整个代码库（或大文件），上下文远超常规编码会话。代理层的生命周期阶段管理和压缩策略是关键。

### D.2 上下文容量预估

| 模型 | 理论上限 | 实际安全上限 | OOM 阈值 | 说明 |
|------|---------|-------------|---------|------|
| 35B MoE | 131K tokens | ~60K tokens (120K chars) | ~180K chars | MoE 激活值大 |
| 27B Dense | 131K tokens | ~80K tokens (160K chars) | ~120K chars | 4-bit KV cache 更省 |
| 云端 | 1M+ tokens | 无限制 | N/A | 无 OOM 风险 |

### D.3 代理层处理阶段（开发者应理解）

```
输入上下文增长:
  0 ──────────────────────────────────────────────→ 200K+ chars
  │←─── INIT ───→│                      │
  │   无压缩      │←── GROWTH ──→│       │
  │  (< 15K)      │  tail-40%清除 │       │
  │               │  (< 40K)     │       │
  │               │              │←─ EXPANSION ─→│
  │               │              │  tail-60%清除  │
  │               │              │  + rounds截断  │
  │               │              │  (< 90K)      │
  │               │              │               │←─ SATURATION ─→
  │               │              │               │  全量清除+合并+截断
  │               │              │               │  (< 180K)
  │               │              │               │
  │               │              │               │           ←─ OOM_DANGER ──
  │               │              │               │              no frozen + 硬截断
```

### D.4 推荐策略

#### 策略 1: 分批阅读（推荐，适用于本地模型）

```bash
# 不一次性 Read 整个代码库，而是让 Claude Code 分批探索
# 代理层的工具过滤 + 循环检测会辅助管理

# Claude Code 提示示例:
# "先读目录结构 (LS), 然后读核心文件的函数签名 (Grep), 
#  不要读完整文件内容，只读关键部分"
# → 代理层工具过滤保留 Read/Grep/Glob，移除不相关工具
```

#### 策略 2: 切换到 27B Dense（更长有效上下文）

```bash
# 27B 的 4-bit KV cache 使得有效上下文更长
./manage.sh switch mlx_vlm-27b && ./manage.sh reload && ./manage.sh restart
# 注意: 需 restart 因为换了后端模型
# 27B 的 PROXY_CTX_CHARS_LIMIT=120K (vs 35B 的 150K，但 KV cache 更高效)
```

#### 策略 3: 切换到云端（无上下文限制）

```bash
# 当本地无法承载时
./manage.sh switch deepseek-chat && ./manage.sh reload
./manage.sh stop-backend   # 释放 GPU 内存
# 云端 1M+ token 上下文，无 OOM 风险
# PROXY_CLEAR_ENABLED=false, PROXY_CTX_LIMIT_ENABLED=false
# → 代理不做任何压缩，完整传递上下文
```

### D.5 监控关键信号

```bash
# 当出现以下日志时，说明上下文即将溢出:
./manage.sh proxy-logs 100 | grep -E "OOM_DANGER|PRE_TRUNC|SATURATION"

# 关键指标:
# "Lifecycle stage: SATURATION"  → 接近上限，即将全量压缩
# "Pre-truncation triggered"     → 已超过 200K，强制截断
# "Memory pressure rejection"    → 系统内存不足，拒绝请求 (503)
```

---

## 案例 E: 模型选型评测

### E.1 场景描述

在切换到新模型前，用量化基准测试评估其质量和性能，避免"凭感觉选模型"。

### E.2 质量评测

```bash
# 1. 确保待评测模型已启动
./manage.sh status   # 后端 + 代理正常

# 2. 运行质量评测（14 项测试: 代码/数学/指令/格式/常识）
python3 tools/bench_quality.py

# 输出示例:
# ============ 质量评测结果 ============
# 模型: mlx-community/Qwen3.6-35B-A3B-4bit
# ─────────────────────────────────────
# 代码生成:   4/4 通过 (100%)
# 数学推理:   2/3 通过 (67%)   ← 17 是否质数答错
# 指令遵循:   2/3 通过 (67%)   ← "不要包含'是'"失败
# 格式正确性: 2/2 通过 (100%)
# 常识推理:   2/2 通过 (100%)
# ─────────────────────────────────────
# 总分: 12/14 (85.7%)
```

### E.3 性能评测

```bash
# 快速性能测试（TTFT + tok/s + 并发 + 长上下文）
python3 tools/bench_perf.py --quick

# 或分项测试:
python3 tools/bench_perf.py --ttft-only          # 首 token 延迟
python3 tools/bench_perf.py --speed-only          # 生成速度
python3 tools/bench_perf.py --long-ctx-only       # 长上下文 TTFT 趋势

# 输出示例:
# ─── TTFT 基线 ───
#   Context 1K:   TTFT = 2.1s
#   Context 10K:  TTFT = 8.3s
#   Context 50K:  TTFT = 31.2s
#   Context 100K: TTFT = 68.5s
#
# ─── 生成速度 ───
#   max_tokens=256:  32.1 tok/s
#   max_tokens=1024: 28.7 tok/s
#
# ─── 并发 ───
#   Concurrent=1: 28.5 tok/s, TTFT=8.2s
#   Concurrent=2: 15.3 tok/s per stream (Metal 时间片)
```

### E.4 对比评测流程

```bash
# 对比 35B vs 27B vs 云端

# Step 1: 评测 35B
./manage.sh switch rapid-mlx-35b-opt && ./manage.sh restart
sleep 60  # 等待模型加载
python3 tools/bench_quality.py > /tmp/bench_35b_quality.txt
python3 tools/bench_perf.py --quick > /tmp/bench_35b_perf.txt

# Step 2: 评测 27B
./manage.sh switch mlx_vlm-27b && ./manage.sh restart
sleep 60
python3 tools/bench_quality.py > /tmp/bench_27b_quality.txt
python3 tools/bench_perf.py --quick > /tmp/bench_27b_perf.txt

# Step 3: 评测云端
./manage.sh switch deepseek-chat && ./manage.sh reload && ./manage.sh stop-backend
python3 tools/bench_quality.py > /tmp/bench_cloud_quality.txt
python3 tools/bench_perf.py --quick > /tmp/bench_cloud_perf.txt

# Step 4: 对比
diff /tmp/bench_35b_quality.txt /tmp/bench_27b_quality.txt
```

### E.5 评测维度权重建议

| 维度 | 编码场景权重 | 数据处理权重 | 快速原型权重 |
|------|------------|------------|------------|
| 代码生成正确率 | 40% | 10% | 20% |
| 指令遵循 | 20% | 30% | 30% |
| 格式正确性 | 10% | 40% | 10% |
| TTFT | 10% | 10% | 30% |
| tok/s | 10% | 10% | 10% |
| 数学/常识推理 | 10% | 0% | 0% |

---

## 案例 F: 并发压测与稳定性验证

### F.1 场景描述

验证系统在压力下的稳定性，确认 OOM 防护、错误恢复和 watchdog 自愈是否正常工作。

### F.2 渐进式压测

```bash
# Step 1: 基线单请求
python3 tools/stress_test.py --concurrency 1 --duration 60
# 预期: 全部 200, TTFT < 15s

# Step 2: 超并发测试（绕过代理限制）
python3 tools/bench_perf.py --concurrency-only --override-concurrency=4
# 注意: 35B/27B 在并发=4 时极可能 OOM
# 观察代理是否正确返回 503 (backend_oom) + Retry-After

# Step 3: 长上下文压力测试
python3 tools/context_stress_test.py
# 模拟 agentic 会话的上下文增长，验证截断/压缩是否生效

# Step 4: 内存监控
./manage.sh monitor 2 &
python3 tools/stress_test.py --concurrency 2 --duration 300
# 观察 Metal 内存峰值是否 < 32 GB (80% of 36.2 GB)
```

### F.3 Watchdog 自愈验证

```bash
# 启动 watchdog（后台守护模式）
./manage.sh watchdog --daemon &

# 模拟后端异常:
kill -9 $(cat llama-server.pid)   # 强杀后端

# 预期 watchdog 行为:
# 1. 60s 内检测到后端无响应
# 2. 自动执行 restart
# 3. 每小时重启不超过 6 次（防止死循环）

# 查看 watchdog 日志:
cat logs/watchdog.log | tail -20
```

### F.4 检查清单

| 检查项 | 验证方法 | 预期结果 |
|--------|---------|---------|
| 413 请求体限制 | 发送 > 500KB 请求 | 返回 413 + `payload_too_large` |
| 503 OOM 拒绝 | 内存 > 90% 时发请求 | 返回 503 + `backend_oom` + `Retry-After` |
| 504 超时分类 | 后端响应 > 600s | 返回 504 + `timeout_error` |
| 429 去重 | 2s 内发相同请求 | 第二次返回 429 + `Retry-After` |
| 循环检测 | 连续 3 次相同 Read | Level 1 提示注入 |
| Blocker 检测 | 连续 2 次 file_not_found | `[BLOCKER]` 消息注入 |
| SIGHUP 热重载 | `./manage.sh reload` | proxy PID 不变，~0.5s 完成 |

---

## 案例 G: 故障恢复与自愈

### G.1 常见故障与恢复

| 故障 | 症状 | 恢复操作 | 预计时间 |
|------|------|---------|---------|
| 后端 OOM 崩溃 | `500 Internal Error` / `[METAL] Insufficient Memory` | watchdog 自动 restart；或手动 `./manage.sh restart` | 60-90s |
| Metal 设备死锁 | 后端启动卡在 `MLX step thread initialized` | **需重启机器**（无法通过 kill 恢复） | 5-10 min |
| 代理配置错误 | 启动后立即退出 | `./manage.sh proxy-logs 20` 查看错误；修复 conf 后 `reload` | 1 min |
| 前缀缓存膨胀 | 内存持续增长不释放 | `./manage.sh stop-backend && start-backend`（Deep reset 清空缓存） | 60-90s |
| 会话死循环 | 代理日志频繁出现 `Loop detected` | 代理自动 Level 1-3 干预；或 `/compact` 清理会话 | 自动恢复 |

### G.2 紧急降级流程

```bash
# 当本地模型反复崩溃且无法快速恢复时:

# Step 1: 立即切到云端（保证服务可用）
./manage.sh switch deepseek-chat && ./manage.sh reload
# proxy 不重启，Claude Code 下一轮请求即走云端

# Step 2: 停止本地后端（释放 GPU）
./manage.sh stop-backend

# Step 3: 排查本地问题
./manage.sh logs 100    # 查看崩溃日志
./manage.sh status      # 确认云端代理正常

# Step 4: 修复后切回本地
./manage.sh start-backend   # 预加载模型
./manage.sh switch rapid-mlx-35b-opt && ./manage.sh reload
```

### G.3 日志诊断速查

```bash
# 500 错误根因分析
grep '"status": 500' logs/proxy_metrics.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    print(f\"  {m['ts']} error_type={m.get('error_type','?')} error={m.get('error','')[:80]}\")
"

# 循环注入统计
grep 'loop_injected' logs/proxy_metrics.jsonl | wc -l

# OOM 事件时间线
grep -E "OOM|memory_rejected|Insufficient Memory" logs/anthropic_proxy.log logs/llama-server.log

# 请求体大小分布（识别超大请求）
grep 'REQ_SUMMARY' logs/anthropic_proxy.log | awk -F'chars=' '{print $2}' | awk '{print $1}' | sort -n | tail -10
```

---

## 附录: 快速参考卡

### 日常命令速查

```bash
./manage.sh start                    # 启动一切
./manage.sh status                   # 检查健康
./manage.sh switch <name> && reload  # 热切换（0.5s）
./manage.sh restart                  # 冷重启（60s，换模型时用）
./manage.sh proxy-logs 50            # 查看代理日志
./manage.sh logs 50                  # 查看后端日志
open http://127.0.0.1:4000/status    # 状态页
```

### 模型选择决策

```
要做什么?
  ├─ 日常编码 → rapid-mlx-35b-opt (默认)
  ├─ 快速验证 → qwen3-8b (最快)
  ├─ 数据处理 → gemma4-26b (JSON/CSV)
  ├─ 深度推理 → mlx_vlm-27b (Dense)
  ├─ 纯代码   → qwen2.5-coder-14b
  └─ 高质量/长上下文 → deepseek-chat (云端)
```

### 成本意识

```
本地: ¥0/请求, 但 ~30s/轮, 上下文 < 180K
云端: ~¥0.01-0.05/请求, ~5s/轮, 上下文 1M+
混合: 日常本地, 关键时刻云端, 日均 ¥1-3
```
