# A/B 测试任务：为代理添加结构化日志系统

## 任务概述

为 `anthropic_proxy.py` 添加**结构化请求日志**和**状态页统计**两个功能模块。任务聚焦、边界清晰，预计产生 15-25 轮有效交互。

这是一个**需要跨步骤回顾代码结构**的任务：Agent 需要在第 5 轮理解 `Handler.do_POST`，在第 15 轮修改它，在第 20 轮再次查看以确认修改位置。如果 tool_result 被清除，Agent 会忘记之前看过的代码结构。

---

## 任务目标（精简为 2 个模块）

### M1: 结构化请求日志（核心）

在 `anthropic_proxy.py` 中，每次收到 `POST /v1/messages` 请求时，在现有日志之外**追加**一行 JSON Lines 记录到 `logs/proxy_requests.jsonl`：

```json
{"timestamp":"2026-06-01T13:45:30","method":"POST","path":"/v1/messages","model":"deepseek-v4-pro","input_chars":240547,"output_chars":1024,"status":200,"duration_ms":14500}
```

要求：
- 使用 Python 标准库的 `json` + `open(..., 'a')`
- 写入操作不能阻塞请求处理（若文件写入可能耗时，用 `threading.Lock` 保护）
- 在 `Handler.do_POST` 的适当位置调用日志写入函数

### M2: 状态页统计卡片（集成）

在现有 `/status` HTML 页面中新增一个 "Request Stats" 卡片，显示：
- 今日请求数（从 `proxy_requests.jsonl` 按日期统计）
- 平均响应时间（最近 20 条记录的平均 `duration_ms`）
- 总输入/输出字符数（今日累计）

要求：
- 修改 `_build_status_html()` 函数
- 在生成 HTML 时读取并解析 `proxy_requests.jsonl`
- 如果日志文件不存在，显示 "No data yet"

---

## 关键约束

1. **标准库 only** — 不使用 pandas、numpy 等第三方库
2. **向后兼容** — 不能破坏现有代理功能
3. **线程安全** — 并发请求时的日志写入需加锁
4. **不修改 manage.sh** — 本次任务只修改 `anthropic_proxy.py`

---

## 评估标准

### 功能正确性（50%）— 可自动验证

| 检查项 | 验证方法 |
|--------|----------|
| `logs/proxy_requests.jsonl` 文件生成 | `test -f logs/proxy_requests.jsonl` |
| 每条记录为有效 JSON | `python3 -c "import json; [json.loads(l) for l in open('logs/proxy_requests.jsonl')]"` |
| 包含必需字段（timestamp, model, input_chars, output_chars, duration_ms） | 同上 |
| 状态页显示统计卡片 | `curl -s http://127.0.0.1:4000/status \| grep -q "Request Stats"` |
| 状态页显示今日请求数 | 同上 |

### 代码质量（30%）— 人工评估

- 遵循现有代码风格（模块级函数、顶层常量）
- 异常处理完善（文件不存在、JSON 解析失败等）
- 注释清晰

### 长上下文利用能力（20%）— A/B 测试核心指标

**本项不是主观评分，而是日志自动提取的量化指标：**

| 指标 | 说明 | 预期差异 |
|------|------|----------|
| `total_requests` | 完成本任务的总 POST 请求数 | B 组 ≤ A 组（无需重复读取） |
| `req_size_growth` | 请求大小平均每轮增长量 | B 组 < A 组（无清除后膨胀） |
| `clearing_events` | Tool clearing 触发次数 | A 组 > 0，B 组 = 0 |
| `truncation_events` | Context truncation 触发次数 | A 组 ≥ 0，B 组 = 0 |

> **注意**：当前日志系统**不记录具体工具调用的文件路径**，因此无法直接计算"文件重复读取率"。上述指标作为替代 proxy。

---

## 为什么这个任务适合 A/B 测试

### 1. 需要跨步骤回顾代码

```
第 3-5 轮:  Read(anthropic_proxy.py) → 理解 Handler 结构和日志输出位置
第 8-12 轮: Write(_write_request_log) → 实现日志写入函数
第 13-17 轮: Edit(Handler.do_POST) → 在请求处理中调用日志函数
             （需要记得 Handler.do_POST 的结构和现有日志调用位置）
第 18-22 轮: Read(_build_status_html) → 理解状态页 HTML 生成逻辑
第 23-25 轮: Edit(_build_status_html) → 添加统计卡片
             （需要记得之前写的日志文件路径和字段格式）
```

如果 tool_result 被清除（A 组）：
- 第 13 轮可能忘记 Handler 结构 → **重新 Read**
- 第 23 轮可能忘记日志字段 → **重新查看之前的实现**

### 2. 任务范围可控

- 只修改 **1 个文件**（`anthropic_proxy.py`）
- 只添加 **2 个功能点**（日志写入 + 状态页卡片）
- 预计 **15-25 轮**完成，不会因为任务过大而失控

### 3. 明确的完成边界

任务完成的标准是**可自动验证**的：
```bash
# 验证 M1
test -f logs/proxy_requests.jsonl
python3 -c "import json; [json.loads(l) for l in open('logs/proxy_requests.jsonl')]"

# 验证 M2
curl -s http://127.0.0.1:4000/status | grep -q "Request Stats"
```

---

## 实验设计：控制变量

### 唯一变量：`PROXY_CLEAR_ENABLED`

| 组 | 代理配置 | 模型 | Clearing | 其他条件 |
|---|---------|------|----------|----------|
| **A（对照组）** | Cloud 模式 | DeepSeek v4-pro | **强制开启** (`PROXY_CLEAR_ENABLED=true, TOOL_KEEP=2, THRESHOLD=15000`) | 与 B 组完全相同 |
| **B（实验组）** | Cloud 模式 | DeepSeek v4-pro | **默认关闭** (`PROXY_CLEAR_ENABLED=false`) | 与 A 组完全相同 |

> **关键控制**：两组使用**同一模型**（DeepSeek v4-pro），通过**同一代理**运行，唯一区别是代理的 clearing 策略。这排除了模型能力差异对实验结果的干扰。

### 启动命令

```bash
# A 组（强制开启 clearing）
PROXY_CLEAR_ENABLED=true \
PROXY_CLEAR_THRESHOLD=15000 \
PROXY_TOOL_KEEP=2 \
PROXY_CTX_LIMIT_ENABLED=true \
PROXY_CTX_CHARS_LIMIT=180000 \
LLAMA_BASE_URL=https://api.deepseek.com/v1 \
LLAMA_API_KEY=sk-... \
python3 anthropic_proxy.py

# B 组（默认关闭 clearing）
LLAMA_BASE_URL=https://api.deepseek.com/v1 \
LLAMA_API_KEY=sk-... \
python3 anthropic_proxy.py
# clearing 自动关闭（云模式默认值）
```

---

## 中途检查点

为了防止 Agent 陷入无限循环或偏离任务，定义以下检查点：

| 检查点 | 轮次 | 标准 | 未达标干预 |
|--------|------|------|-----------|
| C1 | 第 8 轮 | Agent 已读取过 `anthropic_proxy.py` 并提到 `Handler` 类 | 若未读取，人工提示"请先查看 anthropic_proxy.py 的 Handler 类" |
| C2 | 第 15 轮 | Agent 已开始修改代码（出现 `Write` 或 `Edit` 工具调用） | 若未开始修改，人工提示"请开始实现日志写入函数" |
| C3 | 第 25 轮 | 无论完成度如何，停止任务并收集数据 | 记录当前完成度（0-100%） |

---

## 执行流程

```bash
# 步骤 1: 准备 A 组环境（开启 clearing）
./tools/run_experiment.sh prepare --group A --task '为代理添加结构化日志和状态页统计'
# 按提示启动代理（A 组配置：强制开启 clearing）
# 输入任务 prompt 给 Claude Code，执行到完成或第 25 轮
./tools/run_experiment.sh collect --group A

# 步骤 2: 准备 B 组环境（关闭 clearing）
./tools/run_experiment.sh prepare --group B --task '为代理添加结构化日志和状态页统计'
# 按提示启动代理（B 组配置：默认关闭 clearing）
# 输入**相同的**任务 prompt 给 Claude Code
./tools/run_experiment.sh collect --group B

# 步骤 3: 生成对比报告
./tools/run_experiment.sh report
```

---

## 数据收集清单

实验结束后，`tools/analyze_experiment.py` 自动提取以下指标：

| 指标 | 类型 | 说明 |
|------|------|------|
| `total_requests` | 整数 | 完成任务的 POST 请求总数 |
| `avg_chars_per_req` | 整数 | 平均请求大小（chars） |
| `max_chars` | 整数 | 最大请求大小 |
| `req_size_growth` | 整数 | 平均每轮增长量（chars） |
| `total_streams` | 整数 | 流式响应次数 |
| `avg_stream_text` | 整数 | 平均响应文本长度 |
| `clearing_events` | 整数 | Tool clearing 触发次数 |
| `truncation_events` | 整数 | Context truncation 触发次数 |
| `cleared_chars_total` | 整数 | 累计清除字符数 |
| `errors` | 整数 | 错误/异常次数 |
| `tool_freq` | 字典 | 各工具调用频率 TOP10 |

---

## 预期 Agent 行为对比

### A 组（开启 clearing，kept=2）

```
第 5 轮: Read(anthropic_proxy.py) → 理解 Handler 结构
第 10 轮: Write(_write_request_log) → 实现日志函数
第 15 轮: Read(anthropic_proxy.py) → ❌ 忘记 Handler 在哪，重新读取
第 18 轮: Edit(Handler.do_POST) → 添加日志调用
第 22 轮: Read(_build_status_html) → 理解状态页
第 25 轮: Edit(_build_status_html) → 添加统计卡片
         （可能又忘记日志字段格式，需要重新查看）
```

### B 组（关闭 clearing）

```
第 5 轮: Read(anthropic_proxy.py) → 理解 Handler 结构
第 10 轮: Write(_write_request_log) → 实现日志函数
第 14 轮: Edit(Handler.do_POST) → ✅ 记得 Handler 结构，直接修改
第 18 轮: Read(_build_status_html) → 理解状态页
第 22 轮: Edit(_build_status_html) → ✅ 记得日志字段，直接引用
```

**核心假设**：B 组的 `total_requests` 应显著低于 A 组（少 2-4 轮重复读取）。

---

## 风险提示

1. **模型随机性**：即使 prompt 完全相同，LLM 的响应仍有随机性。建议每组运行 2-3 次取平均。
2. **任务熟悉度**：如果 Agent 之前做过类似任务，第二次执行可能更快（学习效应）。建议 A/B 顺序随机化。
3. **日志粒度限制**：当前代理日志不记录具体工具参数（如 Read 的文件路径），因此无法直接计算"文件重复读取率"。`total_requests` 和 `req_size_growth` 作为替代指标。

---

*任务设计版本: 2.0*
*适用代理: Claude Code + anthropic_proxy.py*
*预期执行时间: 15-30 分钟/组*
*预期轮次: 15-25 轮/组*
