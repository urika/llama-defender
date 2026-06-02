# DeepSeek 代理中转与 A/B 实验完整指南

> 版本：v1.0
> 日期：2026-06-02
> 作者：AI Agent
> 适用：macOS + Claude Code + DeepSeek API

---

## 目录

1. [架构概览](#一架构概览)
2. [代理修改说明](#二代理修改说明)
3. [DeepSeek 启动指南](#三deepseek-启动指南)
4. [A/B 对比实验方案](#四ab-对比实验方案)
5. [实验执行脚本说明](#五实验执行脚本说明)
6. [状态页面适配](#六状态页面适配)
7. [成本控制与风险](#七成本控制与风险)
8. [故障排查](#八故障排查)
9. [快速命令索引](#九快速命令索引)

---

## 一、架构概览

### 1.1 两种工作模式

```
┌─────────────────────────────────────────────────────────────────┐
│                     模式 A：本地后端                            │
│  Claude Code → anthropic_proxy.py:4000 → rapid-mlx:8081       │
│                                               ↓                 │
│                              Qwen3.6-35B-A3B-4bit (本地 48GB)   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                     模式 B：DeepSeek 云端                       │
│  Claude Code → anthropic_proxy.py:4000 → api.deepseek.com/v1  │
│                                               ↓                 │
│                              DeepSeek Chat (云端 671B MoE)      │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 核心差异

| 维度 | 本地（A组） | DeepSeek（B组） |
|------|------------|----------------|
| 模型 | Qwen3.6-35B-4bit | DeepSeek Chat (671B MoE, 37B 激活) |
| 部署 | MacBook Pro M5, 48GB 统一内存 | DeepSeek 云端 GPU 集群 |
| 费用 | 几乎为零（电费） | 按 token 计费（约 ¥2-8/百万 tokens） |
| TTFT | 50-60s（56K tokens） | < 5s（云端专用 GPU） |
| Context | 48GB 物理极限，强制 cache clear | 64K tokens，无内存限制 |
| 并发 | PROXY_MAX_CONCURRENT=1 | PROXY_MAX_CONCURRENT=4 |
| 稳定性 | 有 OOM 风险 | 云 SLA 保障 |

---

## 二、代理修改说明

### 2.1 修改清单（anthropic_proxy.py）

| 编号 | 修改点 | 本地模式 | 云模式 | 代码行 |
|------|--------|---------|--------|--------|
| 1 | `LLAMA_API_KEY` 环境变量 | `sk-1234`（占位） | 实际 DeepSeek Key | ~17 |
| 2 | `BACKEND_TYPE` 自动检测 | `local` | `cloud`（URL 匹配） | ~19-26 |
| 3 | `MODEL_NAME` 默认值 | `mlx-community/Qwen3.6-35B-A3B-4bit` | `deepseek-chat` | ~37 |
| 4 | `PROXY_MAX_CONCURRENT` | `1` | `4` | ~35 |
| 5 | Authorization 头 | `Bearer sk-1234` | `Bearer $LLAMA_API_KEY` | ~1144 |
| 6 | 后端锁 `_llama_lock` | `with _llama_lock` | 直接请求 | ~1140 |
| 7 | Streaming token 计数 | `timings.prompt_n` | `usage.prompt_tokens` | ~1245 |
| 8 | 状态页面 Backend 卡片 | PID/Memory/CPU/Uptime | Endpoint/Model/API Key 掩码 | ~824 |
| 9 | 日志统计 OOM/CacheClear | 从后端日志读取 | 隐藏（不适用） | ~775 |
| 10 | main() 启动日志 | 普通信息 | 附加 "Cloud API mode" | ~1375 |

### 2.2 自动检测逻辑

```python
if "deepseek" in LLAMA_BASE.lower() or \
   "openai" in LLAMA_BASE.lower() or \
   "api." in LLAMA_BASE.lower():
    BACKEND_TYPE = "cloud"
else:
    BACKEND_TYPE = "local"
```

### 2.3 环境变量全集

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLAMA_BASE_URL` | `http://127.0.0.1:8081/v1` | 后端 API 地址 |
| `LLAMA_API_KEY` | `sk-1234` | API Key（DeepSeek 需替换） |
| `MODEL_NAME` | 自动判断 | 模型名称 |
| `BACKEND_TYPE` | 自动检测 | `local` 或 `cloud` |
| `PROXY_MAX_CONCURRENT` | `1`(本地)/`4`(云) | 代理并发数 |
| `PROXY_CLEAR_ENABLED` | `true` | 工具结果清理 |
| `PROXY_CLEAR_THRESHOLD` | `15000` | 清理触发阈值（字符） |
| `PROXY_TOOL_KEEP` | `2` | 保留最近 N 对 tool_result |
| `PROXY_CTX_LIMIT_ENABLED` | `true` | 上下文截断 |
| `PROXY_CTX_CHARS_LIMIT` | `180000` | 上下文字符上限 |
| `PROXY_LOG_PATH` | `/tmp/anthropic_proxy.log` | 代理日志路径 |

---

## 三、DeepSeek 启动指南

### 3.1 获取 API Key

1. 访问 https://platform.deepseek.com/
2. 注册/登录账号
3. 进入「API Keys」页面
4. 点击「创建 API Key」
5. 复制生成的 Key（格式：`sk-...`）

### 3.2 方式一：使用 manage.sh（推荐）

```bash
# 1. 设置 API Key
export LLAMA_API_KEY="sk-你的DeepSeekKey"

# 2. 一键启动云端代理
./manage.sh start-cloud

# 输出示例：
# [INFO] 启动云端代理模式...
# [INFO]   后端 URL:   https://api.deepseek.com/v1
# [INFO]   模型:       deepseek-chat
# [INFO]   API Key:    sk-xxxx****
# [INFO]   并发:       4
# [INFO] ✅ 云端代理已启动
# [INFO]
# [INFO] Claude Code 配置命令:
# [INFO]   export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
# [INFO]   export ANTHROPIC_AUTH_TOKEN=sk-any
# [INFO]
# [INFO] 状态页面: http://127.0.0.1:4000/status

# 3. 配置 Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=sk-any
cd /your/project && claude

# 4. 查看状态
./manage.sh status

# 5. 停止
./manage.sh stop
```

### 3.3 方式二：手动启动

```bash
export LLAMA_BASE_URL=https://api.deepseek.com/v1
export LLAMA_API_KEY="sk-你的Key"
export MODEL_NAME=deepseek-chat
export PROXY_MAX_CONCURRENT=4
python3 anthropic_proxy.py
```

### 3.4 方式三：使用配置文件

```bash
# 编辑配置文件
vim configs/deepseek-chat.conf
# 填入 LLAMA_API_KEY="sk-你的Key"

# 手动加载配置
source configs/deepseek-chat.conf
python3 anthropic_proxy.py
```

---

## 四、A/B 对比实验方案

### 4.1 实验目的

在相同 agentic 编程任务下，定量对比：
- **本地 Qwen3.6-35B-4bit** vs **DeepSeek Chat**
- 任务完成率、效率、代码质量、成本

### 4.2 实验架构

```
A 组（对照组）：Claude Code → 代理 → rapid-mlx → Qwen3.6-35B-4bit
B 组（实验组）：Claude Code → 代理 → api.deepseek.com → DeepSeek Chat
```

### 4.3 评价指标体系

#### 效能指标（10 项，自动收集）

| 指标 | 测量方式 | 数据源 |
|------|----------|--------|
| 任务完成率 | 验收通过/失败 | 人工判定 |
| 总耗时 | 实验开始到验收通过 | `run_experiment.sh` 计时 |
| 工具调用次数 | REQ_SUMMARY 统计 | 代理日志 |
| 平均 TTFT | 首 token 返回时间 | 代理日志时间戳差 |
| 平均 TBT | Token 间间隔（流式） | 代理日志 |
| 纯文本思考比例 | Assistant 消息中不含 tool_use 的比例 | 报文分析 |
| 重复工具调用率 | 同一工具连续调用占比 | 报文分析 |
| Compact 次数 | 上下文被迫重置次数 | 代理日志 |
| 请求总字符数 | 最终请求体大小 | REQ_SUMMARY |
| 错误/超时次数 | 异常记录 | 代理日志 |

#### 质量指标（5 项，人工评分）

| 指标 | 评分方式 |
|------|----------|
| 代码正确性 | 1-5 分，人工 review |
| 代码风格 | 1-5 分，是否符合项目规范 |
| 错误恢复能力 | 遇到报错后能否自行修复 |
| 边界处理 | 是否考虑了 edge cases |
| 注释/文档 | 是否添加了必要的注释 |

#### 成本指标（4 项）

| 指标 | 计算方式 |
|------|----------|
| API 费用 | DeepSeek 定价 × tokens |
| 电力成本 | ~100W × 时间 |
| 时间成本 | 工程师等待时间 × 时薪 |
| 总成本 | API + 电力 + 时间 |

### 4.4 实验任务列表

| 编号 | 任务描述 | 预期轮次 | 验收标准 |
|------|----------|----------|----------|
| T1 | 添加 `/health` REST API 端点 | 15-25 | `curl` 测试通过 |
| T2 | 修复已知 bug（有 issue 描述） | 10-20 | 单元测试通过 |
| T3 | 重构模块，提取公共函数 | 15-30 | Code review 通过 |
| T4 | 添加完整测试覆盖 | 20-35 | pytest 覆盖率 >90% |
| T5 | 添加日志和错误处理 | 10-20 | 运行时无异常 |

### 4.5 核心假设

| 假设 | 预期 | 验证方式 |
|------|------|----------|
| H1 模型能力 | B 完成率更高 | 对比 T1-T5 完成率 |
| H2 TTFT | B < 5s vs A 50-60s | 日志时间戳分析 |
| H3 无膨胀 | B 无 cache clear | 日志统计 |
| H4 成本差异 | B ¥0.5-2.0/任务 | DeepSeek 账单 |
| H5 行为模式 | B 思考比例 >10% | 消息结构分析 |

---

## 五、实验执行脚本说明

### 5.1 脚本架构

```
tools/run_experiment.sh     # 实验执行（prepare / collect / report）
tools/analyze_experiment.py # 日志分析（单日志 / A/B 对比）
logs/experiments/           # 实验数据目录
  ├── A-20260602-103000.log     # A 组原始日志
  ├── A-20260602-103000.json    # A 组分析数据
  ├── B-20260602-110000.log     # B 组原始日志
  ├── B-20260602-110000.json    # B 组分析数据
  ├── ab_report_20260602-120000.md  # 对比报告
  └── current_meta.json         # 当前实验元数据
```

### 5.2 实验完整流程

```bash
# ═══════════════════════════════════════════════════════════
# 第一阶段：A 组（本地后端）
# ═══════════════════════════════════════════════════════════

# 1. 启动本地后端
./manage.sh start

# 2. 准备实验环境
./tools/run_experiment.sh prepare \
  --group A \
  --task "添加 /health REST API 端点，返回 {status: ok}"

# 3. 人工执行任务（在另一个终端）
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=sk-any
cd /your/project
claude "添加 /health REST API 端点，返回 {status: ok}"
# ... 人工交互直到任务完成 ...

# 4. 收集 A 组数据
./tools/run_experiment.sh collect --group A

# ═══════════════════════════════════════════════════════════
# 第二阶段：B 组（DeepSeek 云端）
# ═══════════════════════════════════════════════════════════

# 5. 停止本地后端，切换到云端
./manage.sh stop
export LLAMA_API_KEY="sk-你的DeepSeekKey"
./manage.sh start-cloud

# 6. 重置工作区到与 A 组相同的初始状态
cd /your/project
git checkout experiment/ab-test-$(date +%Y%m%d)
git reset --hard HEAD

# 7. 准备 B 组实验环境（使用相同任务描述）
./tools/run_experiment.sh prepare \
  --group B \
  --task "添加 /health REST API 端点，返回 {status: ok}"

# 8. 人工执行任务（在另一个终端）
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=sk-any
cd /your/project
claude "添加 /health REST API 端点，返回 {status: ok}"
# ... 人工交互直到任务完成 ...

# 9. 收集 B 组数据
./tools/run_experiment.sh collect --group B

# ═══════════════════════════════════════════════════════════
# 第三阶段：生成对比报告
# ═══════════════════════════════════════════════════════════

# 10. 生成报告
./tools/run_experiment.sh report

# 输出：logs/experiments/ab_report_20260602-120000.md
```

### 5.3 脚本命令参考

```bash
# 准备实验环境
./tools/run_experiment.sh prepare --group A|B --task "任务描述"

# 收集实验数据（任务完成后执行）
./tools/run_experiment.sh collect --group A|B

# 生成 A/B 对比报告（两组都收集后执行）
./tools/run_experiment.sh report

# 查看当前实验状态
./tools/run_experiment.sh status

# 查看帮助
./tools/run_experiment.sh help
```

### 5.4 分析报告输出示例

```markdown
# A/B 对比实验报告

## A 组（本地后端）
```json
{
  "total_requests": 25,
  "total_chars": 2456789,
  "avg_chars": 98272,
  "max_chars": 197170,
  "tool_clears": 8,
  "errors": 3,
  "tool_freq": {
    "Bash": 42,
    "Read": 38,
    "Skill": 12
  }
}
```

## B 组（DeepSeek 云端）
```json
{
  "total_requests": 18,
  "total_chars": 856432,
  "avg_chars": 47579,
  "max_chars": 89234,
  "tool_clears": 0,
  "errors": 0,
  "tool_freq": {
    "Bash": 28,
    "Read": 22,
    "Skill": 8
  }
}
```

## 对比摘要

| 指标 | A 组 | B 组 | 差异 |
|------|------|------|------|
| total_requests | 25 | 18 | -7 |
| total_chars | 2456789 | 856432 | -1600357 |
| avg_chars | 98272 | 47579 | -50693 |
| max_chars | 197170 | 89234 | -107936 |
| tool_clears | 8 | 0 | -8 |
| errors | 3 | 0 | -3 |
```

---

## 六、状态页面适配

### 6.1 本地模式状态页

```
🖥️ Local LLM Stack Status

Backend (llama-server / rapid-mlx)
  Status:     ● Running
  PID:        68163
  Memory:     18.3 GB
  CPU:        45%
  Uptime:     02:15:30

Proxy (anthropic_proxy.py)
  Status:     ● Running
  PID:        68231
  Listen:     127.0.0.1:4000
  Backend:    http://127.0.0.1:8081/v1

Log Stats
  OOM Crashes:        0  [点击查看详情]
  Forced Cache Clear: 8  [点击查看详情]
  Requests:           25 [点击查看详情]
```

### 6.2 云模式状态页

```
🖥️ Local LLM Stack Status

Backend
  Type:       Cloud API (cloud)
  Endpoint:   https://api.deepseek.com/v1
  Model:      deepseek-chat
  API Key:    sk-xxxx****

Proxy (anthropic_proxy.py)
  Status:     ● Running
  PID:        68345
  Listen:     127.0.0.1:4000

Log Stats
  Requests:   18 [点击查看详情]
  Config:     TOOL_KEEP=2, MAX_CONCURRENT=4
  Model:      deepseek-chat
```

---

## 七、成本控制与风险

### 7.1 DeepSeek 定价参考

| 模型 | Input (百万 tokens) | Output (百万 tokens) |
|------|---------------------|----------------------|
| deepseek-chat | ¥2 | ¥8 |
| deepseek-reasoner | ¥4 | ¥16 |

### 7.2 成本估算

| 场景 | Tokens | 费用估算 |
|------|--------|----------|
| 单次小任务（10K input + 5K output） | 15K | ¥0.06 |
| 单次中任务（50K input + 20K output） | 70K | ¥0.26 |
| 单次大任务（200K input + 80K output） | 280K | ¥1.04 |
| 完整 A/B 实验（2×中任务） | 140K | ¥0.52 |

### 7.3 风险控制

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|----------|
| API Key 泄露 | 低 | 高 | 日志只显示前 8 位，定期轮换 Key |
| 费用超预期 | 中 | 中 | 设置用量上限，先小规模测试 |
| 网络不稳定 | 中 | 中 | 多次重复实验，取中位数 |
| DeepSeek 限流 | 中 | 中 | 降低并发到 2，准备备用 Key |
| 工具调用不兼容 | 低 | 高 | 提前用 curl 测试 function calling |

---

## 八、故障排查

### 8.1 代理启动失败

```bash
# 检查语法
python3 -m py_compile anthropic_proxy.py

# 检查端口占用
lsof -i :4000

# 查看启动日志
tail -n 50 logs/anthropic_proxy.log
```

### 8.2 DeepSeek API 返回错误

```bash
# 测试连通性
curl -s https://api.deepseek.com/v1/models \
  -H "Authorization: Bearer $LLAMA_API_KEY"

# 常见错误码
# 401: API Key 无效
# 429: 请求过快（降低 PROXY_MAX_CONCURRENT）
# 500: DeepSeek 服务端错误（稍后重试）
```

### 8.3 Claude Code 无法连接代理

```bash
# 检查代理是否运行
curl http://127.0.0.1:4000/v1/models

# 检查环境变量
echo $ANTHROPIC_BASE_URL  # 应为 http://127.0.0.1:4000
echo $ANTHROPIC_AUTH_TOKEN  # 任意值
```

### 8.4 日志无 REQ_SUMMARY

```bash
# 确认代理日志路径
echo $PROXY_LOG_PATH  # 默认 /tmp/anthropic_proxy.log

# 检查代理是否写入日志
tail -f /tmp/anthropic_proxy.log
```

---

## 九、快速命令索引

### 9.1 启动命令

```bash
# 本地模式
./manage.sh start

# DeepSeek 云模式
export LLAMA_API_KEY="sk-..."
./manage.sh start-cloud
```

### 9.2 状态查询

```bash
./manage.sh status           # 命令行状态
curl http://127.0.0.1:4000/status  # Web 状态页
```

### 9.3 停止命令

```bash
./manage.sh stop             # 停止代理+本地后端
```

### 9.4 实验命令

```bash
# 准备
./tools/run_experiment.sh prepare --group A --task "任务描述"

# 收集
./tools/run_experiment.sh collect --group A

# 报告
./tools/run_experiment.sh report
```

### 9.5 日志分析

```bash
# 单日志分析
python3 tools/analyze_experiment.py --log /tmp/anthropic_proxy.log

# A/B 对比
python3 tools/analyze_experiment.py \
  --a logs/experiments/A-xxx.log \
  --b logs/experiments/B-xxx.log \
  --report logs/experiments/report.md
```

### 9.6 环境变量速查

```bash
# DeepSeek 模式
export LLAMA_BASE_URL=https://api.deepseek.com/v1
export LLAMA_API_KEY="sk-你的Key"
export MODEL_NAME=deepseek-chat
export PROXY_MAX_CONCURRENT=4

# Claude Code 配置
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=sk-any
```

---

## 附录：文件清单

| 文件 | 类型 | 修改/新增 |
|------|------|----------|
| `anthropic_proxy.py` | 修改 | 10 处改动，支持云 API |
| `manage.sh` | 修改 | `start-cloud`、`status` 适配 |
| `configs/deepseek-chat.conf` | 新增 | DeepSeek 配置文件 |
| `docs/ab-experiment-design.md` | 新增 | 实验方案（11K 字） |
| `docs/DEEPSEEK-AB-EXPERIMENT-GUIDE.md` | 新增 | 本指南 |
| `tools/run_experiment.sh` | 新增 | 实验执行脚本 |
| `tools/analyze_experiment.py` | 修改 | 支持单日志 + A/B 对比 |

---

*文档版本：v1.0*
*生成日期：2026-06-02*
