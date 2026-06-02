# A/B 测试任务：为代理添加结构化日志系统

## 任务概述

为 `anthropic_proxy.py` 添加一个完整的请求/响应结构化日志系统。该系统需要持久化存储每一次 API 交互的详细信息，并提供查询接口和统计视图。

这是一个**长链条、多文件、需要回顾**的任务，非常适合检验 Agent 在完整上下文记忆 vs 工具历史清除两种模式下的表现差异。

---

## 任务目标

实现以下 4 个功能模块：

### M1: 结构化日志持久化
- 每条请求记录为 JSON Lines 格式，存储到 `logs/proxy_requests.jsonl`
- 记录字段：`timestamp`, `method`, `path`, `model`, `input_tokens`, `output_tokens`, `duration_ms`, `status`, `error`
- 在现有日志之外**增量写入**，不影响现有 `anthropic_proxy.log`

### M2: 日志查询 API
- `GET /logs?start=2026-06-01T00:00:00&end=2026-06-01T23:59:59` — 按时间范围查询
- `GET /logs/stats` — 返回统计摘要（总请求数、平均响应时间、错误率、按模型分组）
- 支持 `limit` 和 `offset` 分页

### M3: 日志轮转
- 当 `proxy_requests.jsonl` 超过 10MB 时自动轮转
- 旧日志压缩为 `.jsonl.gz`
- 保留最近 7 天的日志

### M4: 状态页集成
- 在现有 `/status` 页面新增 "Request Stats" 卡片
- 显示：今日请求数、平均 TTFT、总 tokens 消耗、错误率趋势图

---

## 关键约束

1. **标准库 only** — 不使用任何第三方依赖（如 pandas、numpy）
2. **向后兼容** — 不能破坏现有代理功能
3. **性能优先** — 日志写入不能阻塞请求处理（使用后台线程或缓冲）
4. **线程安全** — 考虑并发请求时的日志写入竞争

---

## 评估标准

### 功能正确性（40%）
- [ ] 结构化日志文件正确生成
- [ ] 查询 API 返回正确数据
- [ ] 统计计算准确
- [ ] 日志轮转按规则执行

### 代码质量（30%）
- [ ] 遵循现有代码风格（模块级函数、顶层的常量）
- [ ] 无第三方依赖
- [ ] 异常处理完善
- [ ] 注释清晰

### 长上下文利用能力（30%）— A/B 测试核心指标
- [ ] **是否重复读取已分析过的代码段落？**
- [ ] **是否记得早期对某模块的设计决策并正确应用？**
- [ ] **修改某处后，是否记得同步修改相关联的另一处？**
- [ ] **任务后期是否还需要重新理解前期的架构？**

---

## 为什么这个任务适合 A/B 测试

### 1. 需要跨步骤记忆
```
第 5 轮: 读取 anthropic_proxy.py 的 Handler 类 → 理解请求处理流程
第 15 轮: 修改 Handler.do_POST → 需要记得第 5 轮看到的代码结构
第 25 轮: 添加日志查询 API → 需要知道 Handler 的路由方式
第 35 轮: 状态页集成 → 需要回顾之前写的统计逻辑
```
如果 tool_result 被清除，Agent 会**忘记**第 5 轮读取的内容，导致第 15/25/35 轮需要**重新读取**同一段代码。

### 2. 多文件关联
- `anthropic_proxy.py` — 主文件，需要多处修改
- `manage.sh` — 可能需要添加日志清理的 cron 逻辑
- `AGENTS.md` — 需要更新文档
- 文件间存在依赖关系，修改一处需要记得同步另一处

### 3. 有明确的"重复工作"信号
通过对比两组日志中 `Read` 工具调用的重复率，可以量化 clearing 的影响：
```
组 A (Local + clearing):  读取 anthropic_proxy.py 段落 X 的次数 = ?
组 B (Cloud + no clearing): 读取 anthropic_proxy.py 段落 X 的次数 = ?
```

### 4. 任务长度可控
预计产生 **30-50 轮** 有效对话，足够让 clearing 的影响充分显现。

---

## 执行脚本

```bash
# 准备阶段：备份当前代码
cp anthropic_proxy.py anthropic_proxy.py.bak
cp manage.sh manage.sh.bak

# 启动代理（根据 A/B 组切换配置）
# 组 A: ./manage.sh switch rapid-mlx-35b && ./manage.sh start
# 组 B: ./manage.sh switch deepseek-chat && ./manage.sh start-cloud

# 将本文件的"任务目标"部分作为 prompt 输入 Claude Code

# 收集阶段：记录完整日志
cp logs/anthropic_proxy.log logs/ab-experiment-<group>-<timestamp>.log

# 评估阶段：运行分析脚本
python3 tools/analyze_experiment.py --log logs/ab-experiment-*.log
```

---

## 预期 Agent 行为模式

### 理想行为（完整记忆）
```
1. Read(anthropic_proxy.py) → 理解整体结构
2. Read(Handler.do_POST) → 理解请求入口
3. Write(_write_structured_log) → 实现日志写入
4. Edit(Handler.do_POST) → 在请求处理中调用日志写入
5. Read(Handler._build_status_html) → 理解状态页结构
6. Edit(Handler._build_status_html) → 添加统计卡片
7. ...（后续不需要重新读取已看过的代码）
```

### 失忆行为（工具历史被清除）
```
1. Read(anthropic_proxy.py) → 理解整体结构
2. Write(_write_structured_log) → 实现日志写入
3. Edit(Handler.do_POST) → 修改请求入口
4. Read(anthropic_proxy.py) → ❌ 忘记 Handler 在哪，重新读取
5. Read(Handler._build_status_html) → 理解状态页
6. Edit(Handler._build_status_html) → 添加统计卡片
7. Read(anthropic_proxy.py) → ❌ 忘记日志写入函数放在哪，重新读取
```

通过对比两组日志中 `Read` 调用的**文件重复率**，可以量化 clearing 对效率的影响。

---

## 数据收集清单

实验结束后，需要从日志中提取以下指标：

| 指标 | 说明 | 数据来源 |
|------|------|----------|
| `total_rounds` | 总对话轮数 | 日志中的 POST 请求数 |
| `unique_reads` | 不重复的文件读取次数 | 工具调用日志 |
| `total_reads` | 总文件读取次数 | 工具调用日志 |
| `read_redundancy` | 重复读取率 = (total - unique) / total | 计算 |
| `total_edits` | 总编辑次数 | 工具调用日志 |
| `task_completion` | 任务完成度（0-100%） | 人工评估 |
| `total_duration` | 任务总耗时 | 日志时间戳 |
| `avg_round_time` | 平均每轮耗时 | 计算 |
| `api_cost` | API 总成本（¥） | DeepSeek 账单或估算 |
| `error_count` | 错误/重试次数 | 日志中的 ERROR |

---

*任务设计版本: 1.0*
*适用代理: Claude Code + anthropic_proxy.py*
*预期执行时间: 20-40 分钟*
