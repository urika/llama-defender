# A/B 对比实验设计：本地 Qwen3.6-35B vs DeepSeek API

> 实验目的：在相同 agentic 编程任务下，对比本地部署（Qwen3.6-35B-4bit + rapid-mlx）与云端 API（DeepSeek Chat）的工作效果

---

## 一、实验架构

### 1.1 对照组 A：本地后端（当前配置）

```
Claude Code → anthropic_proxy.py:4000 → rapid-mlx:8081
                                              ↓
                                    Qwen3.6-35B-A3B-4bit (本地)
```

**配置**：
- 模型：`mlx-community/Qwen3.6-35B-A3B-4bit`
- 后端：`rapid-mlx v0.6.30`
- 内存：48GB 统一内存
- 并发：`PROXY_MAX_CONCURRENT=1`
- 成本：几乎为零（电费）

### 1.2 实验组 B：DeepSeek 云端 API

```
Claude Code → anthropic_proxy.py:4000 → api.deepseek.com/v1
                                              ↓
                                    DeepSeek Chat (云端)
```

**配置**：
- 模型：`deepseek-chat`
- 后端：DeepSeek OpenAI 兼容 API
- 并发：`PROXY_MAX_CONCURRENT=4`
- 成本：按 token 计费（约 ¥2/百万 input tokens，¥8/百万 output tokens）

### 1.3 切换方式

```bash
# A 组（本地）
export LLAMA_BASE_URL=http://127.0.0.1:8081/v1
export LLAMA_API_KEY=sk-1234
python3 anthropic_proxy.py

# B 组（DeepSeek）
export LLAMA_BASE_URL=https://api.deepseek.com/v1
export LLAMA_API_KEY=<你的 DeepSeek API Key>
export MODEL_NAME=deepseek-chat
python3 anthropic_proxy.py
```

---

## 二、实验任务设计

### 2.1 任务选择原则

- **可重复**：同一任务可多次执行，结果可比较
- **有明确验收标准**：通过/失败可客观判定
- **覆盖 agentic 典型场景**：文件读写、代码修改、工具调用
- **中等复杂度**：预期 10-30 轮工具调用完成

### 2.2 推荐任务列表

| 编号 | 任务描述 | 预期轮次 | 验收标准 |
|------|----------|----------|----------|
| T1 | 在现有代码库中添加一个 REST API 端点 | 15-25 | curl 测试通过 |
| T2 | 修复一个已知的 bug（有 issue 描述） | 10-20 | 单元测试通过 |
| T3 | 重构一个模块，提取公共函数 | 15-30 | 代码 review 通过 |
| T4 | 为现有函数添加完整测试覆盖 | 20-35 | pytest 覆盖率 >90% |
| T5 | 添加日志和错误处理到关键路径 | 10-20 | 运行时无异常 |

> 建议：首次实验选 **T1 或 T2**，因为验收标准最客观。

### 2.3 实验控制

- **工作目录**：A/B 两组使用不同的 git branch 或不同目录
- **初始状态**：每次实验前 reset 到相同的 git commit
- **系统提示**：使用相同的 system prompt（代理层不做修改）
- **工具集**：使用相同的 27 个工具配置
- **温度**：temperature=0.7（默认）

---

## 三、评价指标体系

### 3.1 效能指标（定量）

| 指标 | 测量方式 | A 组（本地） | B 组（DeepSeek） |
|------|----------|-------------|-----------------|
| **任务完成率** | 验收标准通过/失败 | ？ | ？ |
| **总耗时** | 从发起到验收通过的时间 | ？ | ？ |
| **工具调用次数** | REQ_SUMMARY 日志统计 | ？ | ？ |
| **平均 TTFT** | 首 token 返回时间 | ？ | ？ |
| **平均 TBT** | token 间间隔（流式） | ？ | ？ |
| **纯文本思考比例** | Assistant 消息中不含 tool_use 的比例 | ？ | ？ |
| **重复工具调用率** | 同一工具连续调用占比 | ？ | ？ |
| **Compact 次数** | 上下文被迫重置次数 | ？ | ？ |
| **请求总字符数** | 最终请求体大小 | ？ | ？ |

### 3.2 质量指标（定性/半定量）

| 指标 | 评分方式 | A 组 | B 组 |
|------|----------|------|------|
| **代码正确性** | 1-5 分，人工 review | ？ | ？ |
| **代码风格** | 1-5 分，是否符合项目规范 | ？ | ？ |
| **错误恢复能力** | 遇到报错后能否自行修复 | ？ | ？ |
| **边界处理** | 是否考虑了 edge cases | ？ | ？ |
| **注释/文档** | 是否添加了必要的注释 | ？ | ？ |

### 3.3 成本指标

| 指标 | 计算方式 | A 组 | B 组 |
|------|----------|------|------|
| **API 费用** | 按 DeepSeek 定价 × tokens | ¥0 | ？ |
| **电力成本** | 估算（~100W × 时间） | ~¥0.1/h | ¥0 |
| **时间成本** | 工程师等待时间 × 时薪 | ？ | ？ |
| **总成本** | API + 电力 + 时间 | ？ | ？ |

---

## 四、数据收集方案

### 4.1 自动收集（代理日志）

代理已支持以下自动记录：

```bash
# 1. 请求摘要（每条请求）
grep 'REQ_SUMMARY' /tmp/anthropic_proxy.log
# 输出: [HH:MM:SS] [REQ_SUMMARY] chars=XXXXX tools=YY

# 2. 工具清理记录
grep 'Tool clearing' /tmp/anthropic_proxy.log

# 3. 上下文截断记录
grep 'Context truncation' /tmp/anthropic_proxy.log

# 4. 响应摘要（流式）
grep 'Streamed text=' /tmp/anthropic_proxy.log

# 5. 错误记录
grep 'llama-server error\|Exception\|Traceback' /tmp/anthropic_proxy.log
```

### 4.2 实验后分析脚本

```bash
# 收集实验数据
python3 << 'PYEOF'
import json, re
from collections import Counter

with open('/tmp/anthropic_proxy.log') as f:
    lines = f.readlines()

# 解析 REQ_SUMMARY
reqs = []
for line in lines:
    m = re.search(r'\[(\d{2}:\d{2}:\d{2})\].*\[REQ_SUMMARY\] chars=(\d+) tools=(\d+)', line)
    if m:
        reqs.append({'time': m.group(1), 'chars': int(m.group(2)), 'tools': int(m.group(3))})

# 统计
print(f"总请求数: {len(reqs)}")
print(f"总字符数: {sum(r['chars'] for r in reqs):,}")
print(f"平均请求: {sum(r['chars'] for r in reqs)/len(reqs):.0f} chars")
print(f"最大请求: {max(r['chars'] for r in reqs):,}")
print(f"工具清理: {sum(1 for l in lines if 'Tool clearing' in l)}")
print(f"上下文截断: {sum(1 for l in lines if 'Context truncation' in l)}")

# 工具调用频率
tool_lines = [l for l in lines if "-> Tools:" in l]
tool_counter = Counter()
for line in tool_lines:
    m = re.search(r"-> Tools: \[(.*?)\]", line)
    if m:
        for name in m.group(1).replace("'", "").split(", "):
            tool_counter[name.strip()] += 1
print(f"\n工具调用 TOP 10:")
for name, count in tool_counter.most_common(10):
    print(f"  {name}: {count}")
PYEOF
```

### 4.3 手动记录

| 记录项 | 记录时机 | 记录人 |
|--------|----------|--------|
| 任务开始时间 | 发起第一个请求 | 自动 |
| 任务完成时间 | 验收通过 | 人工 |
| 是否完成 | 验收后 | 人工 |
| 代码质量评分 | 实验结束后 | 人工 review |
| 异常/意外行为 | 发生时 | 人工记录 |
| 主观体验 | 实验后 | 人工填写 |

---

## 五、实验执行流程

### 5.1 实验前准备

```bash
# 1. 准备实验分支
git checkout -b experiment/ab-test-$(date +%Y%m%d)
git commit --allow-empty -m "experiment: A/B baseline"

# 2. 准备 A 组环境（保持当前）
./manage.sh status  # 确认 rapid-mlx 运行正常

# 3. 准备 B 组环境
# 获取 DeepSeek API Key（从 deepseek.com 注册）
export DEEPSEEK_API_KEY="sk-..."
```

### 5.2 执行 A 组（本地）

```bash
# 确保使用本地配置
export LLAMA_BASE_URL=http://127.0.0.1:8081/v1
export LLAMA_API_KEY=sk-1234

# 启动代理
python3 anthropic_proxy.py &
PROXY_PID=$!

# 清空日志
> /tmp/anthropic_proxy.log

# 执行 Task T1
cd /path/to/project
claude "请为这个项目添加一个 /health REST API 端点，返回 {status: ok}"

# 记录完成时间、验收结果
# ...

# 停止代理
kill $PROXY_PID

# 保存日志
cp /tmp/anthropic_proxy.log logs/experiment-a-$(date +%Y%m%d-%H%M%S).log
```

### 5.3 执行 B 组（DeepSeek）

```bash
# 切换到 DeepSeek 配置
export LLAMA_BASE_URL=https://api.deepseek.com/v1
export LLAMA_API_KEY=$DEEPSEEK_API_KEY
export MODEL_NAME=deepseek-chat

# 启动代理（不需要本地后端）
python3 anthropic_proxy.py &
PROXY_PID=$!

# 清空日志
> /tmp/anthropic_proxy.log

# 重置工作区到相同状态
git checkout experiment/ab-test-$(date +%Y%m%d)
git reset --hard HEAD

# 执行相同的 Task T1
cd /path/to/project
claude "请为这个项目添加一个 /health REST API 端点，返回 {status: ok}"

# 记录完成时间、验收结果
# ...

# 停止代理
kill $PROXY_PID

# 保存日志
cp /tmp/anthropic_proxy.log logs/experiment-b-$(date +%Y%m%d-%H%M%S).log
```

### 5.4 实验后分析

```bash
# 运行分析脚本
python3 tools/analyze_experiment.py \
  --a logs/experiment-a-*.log \
  --b logs/experiment-b-*.log \
  --output docs/experiment-results-$(date +%Y%m%d).md
```

---

## 六、预期结果与假设

### 6.1 假设 H1：DeepSeek 模型能力更强

- **预期**：B 组任务完成率更高，代码质量评分更高
- **依据**：DeepSeek Chat 是 671B MoE 模型（激活 37B），远超本地 35B 量化模型
- **验证**：对比 T1-T5 的完成率和质量评分

### 6.2 假设 H2：DeepSeek TTFT 更快但网络延迟存在

- **预期**：B 组 TTFT < 5s（A 组 50-60s），但总响应时间受网络影响
- **依据**：云 API 有专用 GPU 集群，无本地内存瓶颈
- **验证**：对比 REQ_SUMMARY 时间戳和流式响应间隔

### 6.3 假设 H3：本地无 context 膨胀问题

- **预期**：B 组无 forced cache clear，无 compact 需求，context 可持续增长
- **依据**：云 API 有 64K context，无内存限制
- **验证**：对比日志中的 cache clear 和 compact 频率

### 6.4 假设 H4：成本差异显著

- **预期**：B 组单次任务成本 ¥0.5-2.0，A 组几乎为零
- **依据**：56K tokens/请求 × 20 请求 ≈ 1.1M tokens
- **验证**：对比 DeepSeek 账单和本地电费

### 6.5 假设 H5：Agent 行为模式不同

- **预期**：B 组纯文本思考比例更高（>10%），重复调用率更低（<30%）
- **依据**：更强的模型可能有更好的规划和批量处理能力
- **验证**：对比代理日志中的消息结构和工具序列

---

## 七、风险控制

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|----------|
| DeepSeek API 限流 | 中 | 实验中断 | 准备备用 API Key，降低并发 |
| DeepSeek 不支持 tools | 低 | 实验失败 | 提前用 curl 测试 tool calling |
| 网络不稳定 | 中 | 数据偏差 | 多次重复实验，取中位数 |
| 成本超预期 | 中 | 预算超支 | 设置用量上限，先小规模测试 |
| 结果不可复现 | 低 | 结论无效 | 固定 seed，多次重复 |

---

## 八、最小可行实验（MVP）

如果资源有限，可先执行 **最小可行实验**：

1. **单任务**：只执行 T1（添加 /health API）
2. **单次重复**：A/B 各执行 1 次
3. **核心指标**：只记录完成率、总耗时、工具调用次数
4. **时间**：约 30-60 分钟

```bash
# 快速启动 DeepSeek 实验
LLAMA_BASE_URL=https://api.deepseek.com/v1 \
  LLAMA_API_KEY=$DEEPSEEK_API_KEY \
  python3 anthropic_proxy.py &

# 执行任务
cd project && claude "添加 /health 端点"

# 对比结果
grep -c 'REQ_SUMMARY' /tmp/anthropic_proxy.log
```

---

*实验设计版本：v1.0*
*设计日期：2026-06-02*
