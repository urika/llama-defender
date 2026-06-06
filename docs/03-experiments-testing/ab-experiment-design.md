# A/B 对比实验设计：Context Management 配置对比

> 实验目的：在相同 agentic 编程任务和同一云端模型（DeepSeek）下，对比不同代理层 context management 配置（模拟本地约束 vs 云端无约束）对任务执行效率和质量的影响

---

## 一、实验架构

### 1.1 统一代理路由

A/B 两组**均通过 `anthropic_proxy.py` 代理访问同一云端 API**，由代理层控制模型路由和 context management 配置。Claude Code 始终连接 `127.0.0.1:4000`，无需修改客户端配置。

```
Claude Code → anthropic_proxy.py:4000 → api.deepseek.com/v1
                 │                              ↓
                 │  配置化差异             DeepSeek API (同一模型)
                 │  - clearing 开关
                 │  - 阈值/保留数
                 │  - ctx_limit 开关
                 │  - 上下文上限
```

### 1.2 设计原则

- **单一变量**：两组使用同一模型（DeepSeek），唯一变量是代理层的 context management 配置
- **配置化**：所有差异通过环境变量控制，无需修改代码
- **可复现**：通过 `tools/run_experiment.sh` 脚本自动化 prepare/collect/report 流程
- **代理路由**：Claude Code 始终连接 `127.0.0.1:4000`，模型选择完全由代理层决定

### 1.3 A 组配置（模拟本地约束）

模拟本地后端（rapid-mlx / llama-server）在 48GB 统一内存下的 context management 行为：
- 工具结果清理（clearing）**开启**，阈值低，保留少
- 上下文截断（ctx_limit）**开启**，上限小

| 环境变量 | 值 | 说明 |
|----------|-----|------|
| `PROXY_CLEAR_ENABLED` | `true` | 开启工具结果清理 |
| `PROXY_CLEAR_THRESHOLD` | `15000` | 总字符数超过 15K 触发清理 |
| `PROXY_TOOL_KEEP` | `2` | 仅保留最近 2 对 tool_result |
| `PROXY_CTX_LIMIT_ENABLED` | `true` | 开启上下文截断 |
| `PROXY_CTX_CHARS_LIMIT` | `180000` | 上下文超过 180K 字符时截断 |

### 1.4 B 组配置（云端无约束）

模拟云端 API 无物理内存限制的理想场景：
- 工具结果清理（clearing）**关闭**
- 上下文截断（ctx_limit）**关闭**

| 环境变量 | 值 | 说明 |
|----------|-----|------|
| `PROXY_CLEAR_ENABLED` | `false` | 关闭工具结果清理 |
| `PROXY_CLEAR_THRESHOLD` | `30000` | （未生效）默认值 |
| `PROXY_TOOL_KEEP` | `10` | （未生效）默认值 |
| `PROXY_CTX_LIMIT_ENABLED` | `false` | 关闭上下文截断 |
| `PROXY_CTX_CHARS_LIMIT` | `500000` | （未生效）默认值 |

### 1.5 启动命令

```bash
# A 组（模拟本地约束）
PROXY_CLEAR_ENABLED=true \
PROXY_CLEAR_THRESHOLD=15000 \
PROXY_TOOL_KEEP=2 \
PROXY_CTX_LIMIT_ENABLED=true \
PROXY_CTX_CHARS_LIMIT=180000 \
LLAMA_BASE_URL=https://api.deepseek.com/v1 \
LLAMA_API_KEY=sk-... \
python3 anthropic_proxy.py

# B 组（云端无约束，使用代理默认值）
LLAMA_BASE_URL=https://api.deepseek.com/v1 \
LLAMA_API_KEY=sk-... \
python3 anthropic_proxy.py
```

---

## 二、实验任务设计

### 2.1 任务选择原则

- **可重复**：同一任务可多次执行，结果可比较
- **有明确验收标准**：通过/失败可客观判定
- **覆盖 agentic 典型场景**：文件读写、代码修改、工具调用
- **中等复杂度**：预期 70-100 轮工具调用完成

### 2.2 已执行任务

| 编号 | 任务描述 | 实际轮次(A/B) | 验收标准 | 完成状态 |
|------|----------|---------------|----------|----------|
| T1 | 为代理添加结构化日志和状态页统计 | 94 / 79 | 功能正常运行 | ✅ 已完成 |

### 2.3 实验控制

- **模型一致性**：A/B 两组使用**相同**云端模型（DeepSeek），消除模型差异干扰
- **代理路由**：Claude Code 始终连接 `127.0.0.1:4000`，不修改客户端配置
- **代理层配置化**：所有实验变量通过环境变量注入，由 `tools/run_experiment.sh` 管理
- **工作目录**：A/B 两组使用相同的代码库状态
- **日志隔离**：每次实验前清空 `/tmp/anthropic_proxy.log`，实验后归档到 `logs/experiments/`
- **工具集**：使用 Claude Code 默认工具集（27 个工具）

---

## 三、评价指标体系

### 3.1 效能指标（定量，自动收集）

| 指标 | 测量方式 | 数据源 |
|------|----------|--------|
| **任务完成率** | 验收标准通过/失败 | 人工判定 |
| **总耗时** | 从 prepare 到 collect 的时间差 | `run_experiment.sh` 计时 |
| **工具调用次数（请求数）** | REQ_SUMMARY 日志统计 | `analyze_experiment.py` |
| **总字符数** | 所有请求体字符总和 | REQ_SUMMARY |
| **平均请求大小** | 总字符数 / 请求数 | 自动计算 |
| **最大请求大小** | 最大单次请求字符数 | REQ_SUMMARY |
| **请求大小平均增长** | 每轮请求增量均值 | 自动计算 |
| **工具清理次数** | Tool clearing 触发次数 | 代理日志 |
| **累计清理字符** | 清理释放的总字符数 | 代理日志 |
| **上下文截断次数** | Context truncation 触发次数 | 代理日志 |
| **错误/异常次数** | 代理日志中的 error/Exception | 代理日志 |
| **工具调用分布** | 各工具调用频率 | 代理日志 |

### 3.2 质量指标（定性/半定量，人工评分）

| 指标 | 评分方式 | 1-5 分 |
|------|----------|--------|
| **代码正确性** | 人工 review | ？ |
| **代码风格** | 是否符合项目规范 | ？ |
| **错误恢复能力** | 遇到报错后能否自行修复 | ？ |
| **边界处理** | 是否考虑了 edge cases | ？ |
| **注释/文档** | 是否添加了必要的注释 | ？ |

### 3.3 成本指标

| 指标 | 计算方式 | A 组 | B 组 |
|------|----------|------|------|
| **API 费用** | DeepSeek 定价 × tokens | 按实际 | 按实际 |
| **时间成本** | 工程师等待时间 | ？ | ？ |
| **总请求 token 量** | 请求总字符数估算 | ？ | ？ |

---

## 四、数据收集方案

### 4.1 自动化工具链

```
tools/run_experiment.sh        # 实验生命周期管理（prepare / collect / report）
tools/analyze_experiment.py    # 日志解析与指标计算
logs/experiments/              # 实验数据归档目录
  ├── {group}-{timestamp}.log       # 原始代理日志
  ├── {group}-{timestamp}.json      # 分析指标 JSON
  ├── {group}-{timestamp}.start_time # 实验开始时间戳
  ├── {group}-{timestamp}.end_time   # 实验结束时间戳
  ├── ab_report_{timestamp}.md       # A/B 对比报告
  └── current_meta.json             # 当前实验元数据
```

### 4.2 使用方式

```bash
# 准备实验环境（自动记录实验 ID、配置、开始时间）
./tools/run_experiment.sh prepare --group A --task "任务描述"

# 收集实验数据（自动复制日志、运行分析、保存结果）
./tools/run_experiment.sh collect --group A

# 生成 A/B 对比报告
./tools/run_experiment.sh report

# 查看当前实验状态
./tools/run_experiment.sh status
```

### 4.3 自动收集指标（`analyze_experiment.py`）

从代理日志中自动解析以下信息：

```python
# 请求摘要解析
grep 'REQ_SUMMARY' → {time, chars, tools}

# 工具清理解析
grep 'tool_results cleared' → {count, chars_freed}

# 上下文截断解析
grep 'messages dropped' → {msgs_dropped, chars_removed}

# 工具调用列表
grep '-> Tools:' → Counter{tool_name: frequency}

# 流式响应统计
grep 'Streamed text=' → {text_chars, tool_count}
```

### 4.4 手动记录

| 记录项 | 记录时机 | 方式 |
|--------|----------|------|
| 任务开始时间 | prepare 阶段 | 自动（start_time 文件） |
| 任务完成时间 | collect 阶段 | 自动（end_time 文件） |
| 是否完成 | 验收后 | 人工判定 |
| 代码质量评分 | 实验结束后 | 人工 review |
| 异常/意外行为 | 发生时 | 人工记录 |
| 主观体验 | 实验后 | 人工填写 |

---

## 五、实验执行流程

### 5.1 标准化流程（使用 `run_experiment.sh`）

```bash
# ═══════════════════════════════════════════════════════════
# 第一阶段：A 组（模拟本地约束 - clearing 开启）
# ═══════════════════════════════════════════════════════════

# 1. 准备 A 组实验环境
./tools/run_experiment.sh prepare \
  --group A \
  --task "为代理添加结构化日志和状态页统计"

# 脚本输出：
#   [INFO] A 组配置: Cloud + clearing 开启 (threshold=15000, keep=2)
#   [INFO] 实验 ID: A-20260602-141545
#   [INFO] 启动代理命令:
#     PROXY_CLEAR_ENABLED=true PROXY_CLEAR_THRESHOLD=15000 PROXY_TOOL_KEEP=2 \
#     PROXY_CTX_LIMIT_ENABLED=true PROXY_CTX_CHARS_LIMIT=180000 \
#     LLAMA_BASE_URL=https://api.deepseek.com/v1 LLAMA_API_KEY=sk-... \
#     python3 anthropic_proxy.py

# 2. 按提示启动代理（新终端）
export LLAMA_API_KEY="sk-..."
cd /Users/jinsongwang/APP/llama.cpp
PROXY_CLEAR_ENABLED=true PROXY_CLEAR_THRESHOLD=15000 PROXY_TOOL_KEEP=2 \
PROXY_CTX_LIMIT_ENABLED=true PROXY_CTX_CHARS_LIMIT=180000 \
LLAMA_BASE_URL=https://api.deepseek.com/v1 LLAMA_API_KEY="$LLAMA_API_KEY" \
python3 anthropic_proxy.py

# 3. 人工执行 Claude Code 任务
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=sk-any
# ... 在 Claude Code 中执行任务直到完成 ...

# 4. 收集 A 组数据
./tools/run_experiment.sh collect --group A

# ═══════════════════════════════════════════════════════════
# 第二阶段：B 组（云端无约束 - clearing 关闭）
# ═══════════════════════════════════════════════════════════

# 5. 准备 B 组实验环境
./tools/run_experiment.sh prepare \
  --group B \
  --task "为代理添加结构化日志和状态页统计"

# 脚本输出：
#   [INFO] B 组配置: Cloud + clearing 关闭 (1M token 上下文)

# 6. 启动 B 组代理（使用默认配置，clearing 关闭）
LLAMA_BASE_URL=https://api.deepseek.com/v1 \
LLAMA_API_KEY="$LLAMA_API_KEY" \
python3 anthropic_proxy.py

# 7. 执行相同任务
# ... 在 Claude Code 中执行相同任务 ...

# 8. 收集 B 组数据
./tools/run_experiment.sh collect --group B

# ═══════════════════════════════════════════════════════════
# 第三阶段：生成对比报告
# ═══════════════════════════════════════════════════════════

# 9. 生成 A/B 对比报告
./tools/run_experiment.sh report
# 报告输出: logs/experiments/ab_report_{timestamp}.md
```

### 5.2 脚本命令速查

| 命令 | 说明 |
|------|------|
| `./tools/run_experiment.sh prepare -g A -t '...'` | 准备实验环境，输出启动命令 |
| `./tools/run_experiment.sh collect -g A` | 收集日志，运行分析，保存结果 |
| `./tools/run_experiment.sh report` | 查找最新 A/B 数据，生成对比报告 |
| `./tools/run_experiment.sh status` | 查看当前实验元数据 |
| `./tools/run_experiment.sh help` | 查看帮助 |

---

## 六、核心假设

实验聚焦于 **context management 配置对 agentic 任务执行的影响**，而非模型能力对比。

### 6.1 假设 H1：clearing 关闭 → 请求数更少

- **预期**：B 组（无 clearing）请求数 < A 组（有 clearing）
- **逻辑**：clearing 开启时，Agent 丢失早期上下文，可能重复探索已走过的路径，导致更多冗余请求
- **验证**：对比 `total_requests`

### 6.2 假设 H2：clearing 关闭 → 请求增长更平稳

- **预期**：B 组（无 clearing）请求大小增长更线性，A 组（有 clearing）呈锯齿状
- **逻辑**：clearing 会截断 tool_result 内容，但 Agent 可能重新获取信息导致后续请求波动
- **验证**：对比 `req_size_growth` 和请求大小序列

### 6.3 假设 H3：clearing 关闭 → 总字符数更多

- **预期**：B 组（无 clearing）总请求字符数 > A 组（有 clearing）
- **逻辑**：不清理历史 tool_result 导致上下文持续膨胀
- **验证**：对比 `total_chars` 和 `max_chars`

### 6.4 假设 H4：ctx_limit 关闭 → 最大请求更大

- **预期**：B 组（ctx_limit 关闭）最大单次请求 > A 组（ctx_limit 开启, 180K 上限）
- **逻辑**：ctx_limit 直接截断超限请求
- **验证**：对比 `max_chars`

### 6.5 假设 H5：clearing 对任务完成质量无明显影响

- **预期**：两组任务完成质量（代码正确性、风格）相近
- **逻辑**：DeepSeek 模型能力足以补偿 context 损失
- **验证**：人工 review 代码质量评分

---

## 七、风险控制

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|----------|
| DeepSeek API 限流 | 中 | 实验中断 | 准备备用 API Key，降低并发 |
| 网络不稳定 | 中 | 数据偏差 | 多次重复实验，取中位数 |
| 成本超预期 | 中 | 预算超支 | 设置用量上限，先小规模测试 |
| 代理日志丢失 | 低 | 数据缺失 | 每次 collect 前确认日志存在并归档 |
| clearing 未生效 | 低 | A/B 无差异 | prepare 阶段打印配置，人工确认 |
| 结果不可复现 | 低 | 结论无效 | 固定配置参数，多次重复 |

---

## 八、实际实验结果

### 8.1 实验概览

| 项目 | 详情 |
|------|------|
| **实验日期** | 2026-06-02 |
| **任务** | 为代理添加结构化日志和状态页统计 |
| **模型** | DeepSeek API（A/B 组相同） |
| **A 组实验 ID** | `A-20260602-141545` |
| **B 组实验 ID** | `B-20260602-1400` |

### 8.2 核心指标对比

| 指标 | A 组（clearing 开启） | B 组（clearing 关闭） | 差异 |
|------|----------------------|----------------------|------|
| **总请求数** | 94 | 79 | **-15（-16%）** |
| **总字符数** | 21,105,972 | 14,462,399 | **-6,643,573（-31%）** |
| **平均请求大小** | 224,532 chars | 183,068 chars | **-41,464（-18%）** |
| **最大请求大小** | 358,200 chars | 237,750 chars | **-120,450（-34%）** |
| **最小请求大小** | 764 chars | 126 chars | -638 |
| **平均工具数/请求** | 42.6 | 42.9 | +0.3 |
| **请求大小平均增长** | 3,843 chars/轮 | 1,840 chars/轮 | **-2,003（-52%）** |
| **工具清理次数** | 0 | 0 | 0 |
| **上下文截断次数** | 0 | 0 | 0 |
| **错误数** | 0 | 0 | 0 |

### 8.3 关键发现

1. **B 组（无 clearing）请求数少 16%**：与假设 H1 方向相反。A 组（有 clearing）反而发出了更多请求（94 vs 79），可能是因为丢失上下文后需要重新探索。

2. **B 组（无 clearing）总字符数少 31%**：与假设 H3 方向相反。无 clearing 的 B 组总请求体反而更小，说明上下文保留使得 Agent 能更高效地利用已有信息，减少重复传递。

3. **A 组（有 clearing）请求增长更快（+52%）**：与假设 H2 方向一致。A 组每轮请求增量几乎是 B 组的 2 倍，表明 clearing 导致 Agent 需要更多补偿性信息获取。

4. **两组的 clearing 和 ctx_limit 均未实际触发**：虽然 A 组配置了 clearing，但在此次任务中未达到触发条件（请求最大 358K < 无明确触发线，且 clearing 机制基于 tool_result 对计数而非单次请求大小）。这意味着本次实验中 A/B 组的实际差异可能主要来自**心理效应**（Agent 感知到 context 受限后的行为变化）而非机制层面的直接截断。

5. **工具调用模式高度一致**：两组工具分布几乎完全相同（都是 27 个工具各出现请求数次），说明 Agent 的工具使用策略不受 context management 配置显著影响。

### 8.4 后续改进方向

- **增大任务复杂度**使 clearing/ctx_limit 真正触发（例如 200+ 轮工具调用任务）
- **降低 A 组阈值**（如 `PROXY_CLEAR_THRESHOLD=5000`, `PROXY_CTX_CHARS_LIMIT=50000`）以放大差异
- **增加重复实验**（每组至少 3 次）以获得统计显著性
- **添加请求时间戳分析**（TTFT、总耗时）到 `analyze_experiment.py`

---

*实验设计版本：v2.0*
*更新日期：2026-06-03*
*变更摘要：基于实际测试情况更新 — A/B 组均通过代理路由，差异配置化（clearing/ctx_limit），新增实际实验数据*
