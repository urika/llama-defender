# 代理层语义优化：量化指标体系

> 日期: 2026-06-04
> 版本: v3（新增埋点代码实现）
> 目的: 将语义优化从"观察日志"提升为可量化、可追踪、可回归的指标体系

---

## 1. 优化回顾

代理层已实施的 5 项语义优化：

| 编号 | 优化 | 核心思想 | 代码位置 |
|------|------|---------|---------|
| O1 | 语义分级清除 | 按工具名+内容类型分配保留优先级 | `clear_old_tool_results()` L545 |
| O2 | 错误信息翻译 | 将结构化错误改写为自然语言指导 | `_handle_messages()` L1953 |
| O3 | 增强循环检测 | 可配置阈值 + 相同参数匹配 | `_handle_messages()` L1993 |
| O4 | 动态 KEEP 策略 | 主进程/子代理自动识别，动态调整保留量 | `_handle_messages()` L1939 |
| O5 | 文件路径元数据 | 清除时保留 file_path/command 关键信息 | `clear_old_tool_results()` L602 |

---

## 2. 关键量化指标（KPI）

### 2.1 循环健康度

| 指标 | 定义 | 计算方式 | 健康阈值 | 数据源 |
|------|------|---------|---------|--------|
| **循环检测率** `loop_rate` | 被检测到的循环次数 / 总请求数 | `grep -c "Loop detected"` / `grep -c "REQ_SUMMARY"` | < 5% | proxy log |
| **单会话最大连续调用** `max_consecutive` | 同一 tool_use 的最大连续重复次数 | 需新增日志: 连续调用计数 | ≤ 3 | 待实现 |
| **循环恢复时间** `loop_recovery_ms` | 从注入打断消息到模型发出不同工具调用的耗时 | 需新增日志: 打断→变更的时间差 | < 30s | 待实现 |
| **错误翻译率** `error_translation_rate` | 被翻译的错误 / 含错误的 tool_result 总数 | `grep -oP 'Error translation: \K\d+'` | 目标: 100% | proxy log |

### 2.2 上下文管理效率

| 指标 | 定义 | 计算方式 | 健康阈值 | 数据源 |
|------|------|---------|---------|--------|
| **上下文膨胀率** `context_inflation` | 每轮请求 chars 增量 | `chars[n] - chars[n-1]`，从 REQ_SUMMARY 提取 | < 2000/轮 | proxy log |
| **清除节省率** `clear_savings_rate` | cleared_chars / total_chars_before | 从 Tool clearing 日志提取 | > 30% | proxy log |
| **语义保留命中率** `keep_high_prio_rate` | kept_high_prio / kept | 从 Tool clearing 日志提取 | > 80% | proxy log |
| **清除后重复读取率** `re_read_rate` | 清除某文件后模型再次 Read 同一文件的次数 / 清除次数 | 需新增: 清除文件路径追踪 + 后续 Read 匹配 | < 10% | 待实现 |
| **动态 KEEP 激活率** `dynamic_keep_rate` | 子代理请求占比 | `grep -c "Sub-agent detected"` / `grep -c "REQ_SUMMARY"` | 观测值 | proxy log |

### 2.3 工具调用质量

| 指标 | 定义 | 计算方式 | 健康阈值 | 数据源 |
|------|------|---------|---------|--------|
| **工具有效率** `tool_effectiveness` | 有效 tool_result / 总 tool_result | 1 - (Wasted + Error) / Total | > 80% | 请求报文分析 |
| **Read 重复率** `read_repeat_rate` | 对同一 file_path 的重复 Read 次数 / 总 Read 次数 | 需解析 tool_use 序列 | < 15% | 待实现 |
| **工具多样性** `tool_diversity` | 单会话使用的不同工具种类数 | 统计 tool_use name 的 unique 数 | ≥ 3 | 请求报文分析 |
| **路径修正速度** `path_correction_rounds` | 子代理从路径幻觉到正确路径的尝试次数 | 手动标注（从保存的报文） | ≤ 2 | 人工分析 |

### 2.4 端到端效率

| 指标 | 定义 | 计算方式 | 健康阈值 | 数据源 |
|------|------|---------|---------|--------|
| **Token 效率** `token_efficiency` | 有效产出 tokens / 总 prompt tokens | 需后端返回 completion_tokens | 观测值 | proxy log |
| **TTFT 恶化度** `ttft_increase` | 有清除 vs 无清除的 TTFT 差异 | 有清除时的 TTFT / 基线 TTFT | < 1.5x | 后端 log |
| **请求成功率** `request_success_rate` | 200 响应 / 总请求 | `grep -c "status=200"` / `grep -c "REQ_SUMMARY"` | > 95% | proxy log |
| **会话完成率** `session_completion_rate` | 最终产生 text 输出（非 tool_use 循环）的会话 / 总会话 | 需新增: 会话终止标记 | > 90% | 待实现 |

---

## 3. 当前可从日志直接提取的指标

以下指标可立即通过现有 proxy log 提取，无需代码修改：

### 3.1 提取脚本

```bash
#!/bin/bash
# proxy_metrics.sh — 从代理日志提取量化指标
LOG=${1:-/Users/jinsongwang/APP/llama.cpp/logs/anthropic_proxy.log}

echo "=== 代理层语义优化量化指标 ==="
echo "日志文件: $LOG"
echo ""

# 基础请求统计
total_req=$(grep -c "REQ_SUMMARY" "$LOG")
echo "[基础] 总请求数: $total_req"

# 会话统计（去重 session_id）
sessions=$(grep -oP 'session=\K[a-f0-9]+' "$LOG" | sort -u | wc -l | tr -d ' ')
echo "[基础] 独立会话数: $sessions"

# 上下文膨胀（最近20个请求的平均 chars）
echo ""
echo "[上下文膨胀] 最近 20 个请求的 chars:"
grep "REQ_SUMMARY" "$LOG" | tail -40 | grep -oP 'chars=\K\d+' | \
  awk '{sum+=$1; n++} END {printf "  平均 chars: %.0f\n  最大 chars: (见日志)\n", sum/n}'

# 消息数统计
echo ""
echo "[消息数] 最近 20 个请求的 msgs:"
grep "REQ_SUMMARY" "$LOG" | tail -40 | grep -oP 'msgs=\K\d+' | \
  awk '{sum+=$1; n++; if($1>max)max=$1} END {printf "  平均 msgs: %.0f\n  最大 msgs: %d\n", sum/n, max}'

# O2: 错误翻译（含类型细分）
error_translated=$(grep -oP 'Error translation: \K\d+' "$LOG" | paste -sd+ - | bc 2>/dev/null || echo 0)
error_events=$(grep -c "Error translation" "$LOG")
wasted=$(grep -oP 'wasted=\K\d+' "$LOG" | paste -sd+ - | bc 2>/dev/null || echo 0)
fnf=$(grep -oP 'file_not_found=\K\d+' "$LOG" | paste -sd+ - | bc 2>/dev/null || echo 0)
iv=$(grep -oP 'input_validation=\K\d+' "$LOG" | paste -sd+ - | bc 2>/dev/null || echo 0)
echo ""
echo "[O2 错误翻译] 翻译事件数: $error_events, 总翻译条数: $error_translated"
echo "  类型细分: wasted=$wasted file_not_found=$fnf input_validation=$iv"

# O3: 循环检测
loops=$(grep -c "Loop detected" "$LOG")
consec=$(grep -c "Consecutive calls" "$LOG")
max_run_max=$(grep -oP 'max_run=\K\d+' "$LOG" | sort -n | tail -1)
echo ""
echo "[O3 循环检测] 检测次数: $loops, 连续调用事件: $consec, 历史最高连续: ${max_run_max:-0}"
echo "  循环检测率: $(echo "scale=4; $loops * 100 / $total_req" | bc 2>/dev/null || echo 'N/A')%"

# O4: 动态 KEEP
subagent=$(grep -c "Sub-agent detected" "$LOG")
echo ""
echo "[O4 动态KEEP] 子代理请求数: $subagent"
echo "  子代理占比: $(echo "scale=4; $subagent * 100 / $total_req" | bc 2>/dev/null || echo 'N/A')%"

# O1/O5: 清除统计
clearing=$(grep -c "Tool clearing.*cleared" "$LOG")
total_cleared=$(grep -oP '(\d+) tool_results cleared' "$LOG" | grep -oP '^\d+' | paste -sd+ - | bc 2>/dev/null || echo 0)
total_freed=$(grep -oP '([\d,]+) chars freed' "$LOG" | tr -d ',' | paste -sd+ - | bc 2>/dev/null || echo 0)
avg_kept=$(grep -oP 'kept \K\d+' "$LOG" | tail -100 | awk '{sum+=$1; n++} END {printf "%.1f", sum/n}')
avg_high_prio=$(grep -oP 'high_prio=\K\d+' "$LOG" | tail -100 | awk '{sum+=$1; n++} END {printf "%.1f", sum/n}')
echo ""
echo "[O1 语义清除] 清除事件数: $clearing"
echo "  总清除条数: $total_cleared"
echo "  总释放字符: $total_freed"
echo "  平均保留数: $avg_kept"
echo "  平均高优先级保留: $avg_high_prio"
if [ "$avg_kept" != "0" ] && [ -n "$avg_kept" ]; then
  echo "  语义保留命中率: $(echo "scale=2; $avg_high_prio * 100 / $avg_kept" | bc 2>/dev/null || echo 'N/A')%"
fi

# Re-read 检测
re_reads=$(grep -c "Re-read after clear" "$LOG")
re_read_rates=$(grep -oP 're_read_rate=\K\d+' "$LOG" | sort -n)
if [ -n "$re_read_rates" ]; then
  re_read_avg=$(echo "$re_read_rates" | awk '{sum+=$1; n++} END {printf "%.0f%%", sum/n}')
  re_read_max=$(echo "$re_read_rates" | tail -1)
  echo ""
  echo "[O5 重读检测] 重读事件数: $re_reads, 平均重读率: $re_read_avg, 最高重读率: ${re_read_max}%"
fi
```

### 3.2 当前实测数据（2026-06-04 10:42 截取）

从 proxy log 中提取的关键数字：

| 指标 | 实测值 |
|------|--------|
| 总请求数 | 2556 |
| 循环检测次数 | 10 |
| 错误翻译事件 | 20 |
| 子代理请求 | 2 |
| 清除事件 | 2156 |
| 平均 KEEP | 10 |
| 平均高优先级保留 | 10 |
| 语义保留命中率 | ~100% |

---

## 4. 已实现的埋点代码

以下埋点已实现在 `anthropic_proxy.py` 中，通过 proxy log 输出结构化数据。

### 4.1 会话标识与请求概况

**位置**: `do_POST()` ~L1897  
**日志格式**:
```
[REQ_SUMMARY] session=<8位hex> chars=<N> tools=<N> msgs=<N>
```

**实现逻辑**:
- `session_id`: 对 system prompt 前 500 字符做 MD5 取前 8 位，同一会话的所有请求共享同一 session_id
- `msgs`: 当前请求的 messages 数量（跟踪上下文增长）
- `chars`: messages JSON 序列化后的字符数
- `tools`: 工具定义数量（用于区分主进程 27 / 子代理 17,20）

**关联指标**: `context_inflation`（chars 差值），`dynamic_keep_rate`（tools 聚合），会话级聚合

**提取方式**:
```bash
grep "REQ_SUMMARY" proxy.log | grep "session=abc12345"  # 按会话过滤
```

---

### 4.2 连续调用追踪（max_consecutive）

**位置**: `_handle_messages()` ~L2015（循环检测代码之前）  
**日志格式**:
```
  -> Consecutive calls: max_run=<N> tool=<name> total_calls=<N>
```

**实现逻辑**:
- 遍历 `tool_call_history`，计算同一 (tool_name, args) 的最大连续出现次数
- 仅当 `max_run >= 2` 时输出日志（避免正常单次调用产生噪音）
- `total_calls` 为本请求中所有 tool_use 调用总数

**触发条件**: max_run ≥ 2（即至少有 2 次连续相同调用）

**关联指标**: `max_consecutive`（直接从日志提取），`loop_rate`（max_run ≥ threshold 的比例）

**提取方式**:
```bash
grep "Consecutive calls" proxy.log | grep -oP 'max_run=\K\d+' | sort -n | uniq -c | sort -rn
```

**注意**: 此埋点在 O3 循环检测之前执行，因此即使循环被 O3 打断，连续调用计数仍会被记录。

---

### 4.3 清除文件路径追踪（cleared_files）

**位置**: `clear_old_tool_results()` ~L593（清除循环内）  
**返回值扩展**: `clear_stats["cleared_files"]` = `set()` 包含所有被清除的文件路径

**实现逻辑**:
- 在清除循环中，从配对的 tool_use 提取 `file_path` 或 `path` 参数
- 将路径加入 `cleared_files` 集合
- 通过 `clear_stats` 返回给 `_handle_messages()` 供后续分析使用

**关联指标**: 为 4.4（re_read_rate）提供数据基础

---

### 4.4 清除后重读检测（re_read_rate）

**位置**: `_handle_messages()` ~L2048（清除日志之后）  
**日志格式**:
```
  -> Re-read after clear: <N> reads target <M> cleared files (re_read_rate=<P>%)
```

**实现逻辑**:
- 从 `clear_stats["cleared_files"]` 获取本次被清除的文件路径集合
- 遍历 `raw_messages` 中所有 assistant 消息的 Read tool_use
- 检查 file_path 是否在 cleared_files 中
- 输出重读次数、命中的已清除文件数、重读率百分比

**触发条件**: 清除发生（clear_stats.cleared=True）且存在 cleared_files，且检测到重读

**关联指标**: `re_read_rate`（直接从日志提取），衡量 O5（路径元数据）和 O1（语义保留）的有效性

**提取方式**:
```bash
grep "Re-read after clear" proxy.log | grep -oP 're_read_rate=\K\d+'
```

**注意**: 此检测基于当前请求的历史消息，即"过去已发生的重读"。模型在收到翻译后的错误消息或打断消息后的行为变化，需通过下一个请求的 tool_effectiveness 间接观测。

---

### 4.5 错误翻译类型细分

**位置**: `_handle_messages()` ~L1985（O2 错误翻译）  
**日志格式**:
```
  -> Error translation: <N> errors rewritten (wasted=<N> + file_not_found=<N> + input_validation=<N>)
```

**实现逻辑**:
- `error_types` dict 分类计数：`wasted`（Wasted call）、`file_not_found`（File does not exist）、`input_validation`（InputValidationError）
- 仅在 error_translated > 0 时输出，且只显示有值的类型

**关联指标**: `error_translation_rate`（按类型细分），识别主要错误来源

**提取方式**:
```bash
grep "Error translation" proxy.log | grep -oP 'wasted=\K\d+' | paste -sd+ - | bc  # Wasted call 总数
grep "Error translation" proxy.log | grep -oP 'file_not_found=\K\d+' | paste -sd+ - | bc  # FileNotFound 总数
grep "Error translation" proxy.log | grep -oP 'input_validation=\K\d+' | paste -sd+ - | bc  # InputValidation 总数
```

---

### 4.6 埋点输出汇总

完整的一个请求在 proxy log 中的埋点输出示例：

```
[10:54:51] POST /v1/messages from 127.0.0.1
[10:54:51]   [REQ_SUMMARY] session=a3f7b2c1 chars=269928 tools=27 msgs=485
[10:54:51]   -> Handling model=claude-sonnet-4-6, stream=True
[10:54:51]   -> Sub-agent detected (20 tools, no Agent/Plan), dynamic KEEP=15
[10:54:51]   -> Error translation: 4 errors rewritten (file_not_found=2 + input_validation=2)
[10:54:51]   -> Consecutive calls: max_run=2 tool=Read total_calls=11
[10:54:51]   -> Loop detected: Read called 3 times with same args, injected break message
[10:54:51]   -> Tool clearing: 5 tool_results cleared, 15,230 chars freed (kept 15, high_prio=15)
[10:54:51]   -> Re-read after clear: 2 reads target 3 cleared files (re_read_rate=66%)
```

---

### 4.7 埋点与指标对应关系

| 埋点日志关键词 | 关联指标 | 数据源状态 |
|---|---|---|
| `[REQ_SUMMARY] session=` | `context_inflation`, 会话聚合 | ✅ 已实现 |
| `Consecutive calls: max_run=` | `max_consecutive`, `loop_rate` | ✅ 已实现 |
| `Loop detected:` | `loop_rate`, `loop_recovery_ms`（时间戳差值） | ✅ 已实现 |
| `Error translation:` | `error_translation_rate`（含类型细分） | ✅ 已实现 |
| `Sub-agent detected:` | `dynamic_keep_rate` | ✅ 已实现 |
| `Tool clearing:` | `clear_savings_rate`, `keep_high_prio_rate` | ✅ 已实现 |
| `Re-read after clear:` | `re_read_rate` | ✅ 已实现 |

---

## 5. 观测方法

### 5.1 实时监控（每次 Claude Code 会话后）

```bash
# 提取最近一次会话的指标
tail -500 /path/to/anthropic_proxy.log | bash proxy_metrics.sh /dev/stdin
```

### 5.2 周期对比（每日/每周）

```bash
# 对比不同时间段的指标趋势
# 需要日志轮转支持（当前单文件）
```

### 5.3 A/B 测试

切换优化开关（通过环境变量），对比同一任务在不同配置下的指标：

| 配置项 | 对照组 | 实验组 |
|--------|--------|--------|
| `PROXY_TOOL_KEEP` | 5 | 10 |
| `PROXY_LOOP_THRESHOLD` | 0（禁用） | 3 |
| `TOOL_SEMANTIC_PRIORITY` | 均等 | 分级 |
| 错误翻译 | 关闭 | 开启 |

---

## 6. 指标分级

### L0 — 必须监控（异常告警）

- **循环检测率** > 10%：模型频繁进入死循环
- **请求成功率** < 90%：代理或后端故障
- **上下文膨胀率** > 5000/轮：可能存在上下文泄漏

### L1 — 建议监控（趋势分析）

- **错误翻译率**：衡量 O2 有效性
- **语义保留命中率**：衡量 O1 有效性
- **工具有效率**：衡量模型行为质量
- **动态 KEEP 激活率**：衡量子代理调度频率

### L2 — 深度分析（按需提取）

- **循环恢复时间**：评估打断消息的引导效果
- **清除后重复读取率**：评估预览保留的信息量是否足够
- **路径修正速度**：评估子代理的自纠正能力
- **工具多样性**：评估模型是否过度依赖单一工具

---

## 7. 设计-指标关联分析

### 7.1 因果链模型

代理层的语义优化不是孤立生效的，它们之间存在**级联依赖关系**。以下是核心因果链：

```
对话轮次增长
    │
    ├─→ tool_result 累积 → 总 chars 超过 PROXY_CLEAR_THRESHOLD
    │       │
    │       ├─→ [O4] 动态 KEEP 策略决定保留量（主进程 10 / 子代理 15）
    │       │       │
    │       │       └─→ [O1] 语义分级清除决定"保留谁"（按 tool_name + content_pattern 打分）
    │       │               │
    │       │               ├─→ 影响: keep_high_prio_rate, clear_savings_rate
    │       │               │
    │       │               ├─→ [O5] 被清除的 tool_result 保留 file_path 元数据
    │       │               │       │
    │       │               │       └─→ 影响: re_read_rate（模型是否因丢失路径而重复读取）
    │       │               │
    │       │               └─→ 清除后模型可能丢失上下文 → 尝试 Read 已清除的文件
    │       │                       │
    │       │                       ├─→ [O2] 错误信息翻译：Wasted call → 自然语言指导
    │       │                       │       │
    │       │                       │       └─→ 影响: error_translation_rate, tool_effectiveness
    │       │                       │
    │       │                       └─→ 模型继续重复 Read → 形成循环
    │       │                               │
    │       │                               └─→ [O3] 循环检测：连续 N 次相同调用 → 注入打断消息
    │       │                                       │
    │       │                                       └─→ 影响: loop_rate, loop_recovery_ms, max_consecutive
    │       │
    │       └─→ 清除释放 chars → 降低 prompt_tokens → 降低 TTFT
    │               │
    │               └─→ 影响: context_inflation, token_efficiency, ttft_increase
    │
    └─→ 未被清除的 tool_result 持续累积
            │
            └─→ 影响: request_success_rate, session_completion_rate
```

### 7.2 优化-指标影响矩阵

每项优化对所有指标的**直接影响**（★）和**间接影响**（☆）：

| 指标 \ 优化 | O1 语义分级 | O2 错误翻译 | O3 循环检测 | O4 动态 KEEP | O5 文件路径元数据 |
|---|---|---|---|---|---|
| `loop_rate` | ☆（保留高价值内容减少重新读取需求） | ☆（自然语言指导减少循环诱发） | ★（直接检测并打断循环） | ☆（更多保留=更少信息丢失=更少循环） | ☆（路径提示减少盲目重读） |
| `max_consecutive` | ☆ | ☆ | ★（阈值=N，限制最大连续数） | ☆ | ☆ |
| `loop_recovery_ms` | — | ★（翻译后的指导提供替代方案，加速恢复） | ★（打断消息含"换用其他方式"指导） | — | — |
| `error_translation_rate` | — | ★（直接衡量 O2 的覆盖率） | — | — | — |
| `context_inflation` | ★（清除降低每轮增量） | ☆（翻译替代原始错误，可能略增 chars） | ☆（打断消息注入增加少量 chars） | ★（保留量直接影响膨胀斜率） | ☆（元数据附加少量 chars） |
| `clear_savings_rate` | ★（优先级决定清除量） | — | — | ★（保留数决定节省空间） | — |
| `keep_high_prio_rate` | ★（直接衡量 O1 的分级准确性） | — | — | ☆（保留数影响高优先级占比） | — |
| `re_read_rate` | ★（保留高价值内容减少重读需求） | ☆（翻译告知"不要再读"） | ☆（打断阻止继续读） | ★（更多保留=更少重读） | ★（路径元数据提醒"已读过"） |
| `dynamic_keep_rate` | — | — | — | ★（直接衡量 O4 的触发频率） | — |
| `tool_effectiveness` | ★（保留有价值内容提高后续决策质量） | ★（翻译提高模型对错误的理解） | ☆（打断无效调用提高有效率） | ☆（更多保留提高信息完备性） | ☆（路径信息提高后续调用准确性） |
| `read_repeat_rate` | ★（保留 Read 结果减少重复） | ☆（翻译"不要再读"降低重复意愿） | ★（打断连续 Read） | ★（更多保留减少信息丢失） | ★（路径元数据提供"已读"标记） |
| `tool_diversity` | ☆（保留更多上下文让模型能选择多样工具） | ★（翻译建议"用 Bash cat"引入多样性） | ★（打断单一工具循环强制探索） | ☆ | — |
| `token_efficiency` | ★（清除低价值内容减少无效 token） | ☆（翻译略增 token 但提高有效性） | ☆（打断减少无效调用轮次） | ★（保留量直接影响 prompt 大小） | ☆（元数据略增 token） |
| `ttft_increase` | ★（清除降低 prompt_tokens → 降低 TTFT） | ☆（翻译可能略增 chars） | ☆（打断消息略增 chars） | ★（保留量直接影响 prompt 大小） | ☆（元数据略增 chars） |
| `request_success_rate` | ☆（保留关键内容提高任务完成率） | ☆（翻译提高错误恢复率） | ☆（打断阻止无限循环） | ☆（更多保留提高信息完备性） | ☆ |
| `session_completion_rate` | ☆ | ☆ | ★（打断死循环是会话完成的必要条件） | ☆ | ☆ |

**图例**: ★ 直接影响（因果链第一跳），☆ 间接影响（因果链第二跳），— 无显著关联

### 7.3 优化间的依赖关系

```
O4 (动态 KEEP)
 ├── 决定 O1 的保留预算（effective_keep 参数）
 ├── 决定 O5 的元数据数量（清除量 = total - effective_keep）
 └── 影响 O3 的触发概率（保留越多 → 信息丢失越少 → 循环概率越低）

O1 (语义分级清除)
 ├── 依赖 O4 提供的保留预算
 ├── 为 O5 提供清除列表（哪些被清除、保留哪些）
 └── 影响 O2 和 O3 的上游（清除质量决定下游循环/错误的频率）

O5 (文件路径元数据)
 ├── 依赖 O1 决定哪些被清除
 └── 是 O2 的前馈（元数据越完整 → O2 翻译时路径提示越精准）

O2 (错误信息翻译)
 ├── 是 O1 清除的下游（清除导致 Wasted call → O2 翻译）
 ├── 是 O3 的前馈（翻译可能直接解决循环，无需 O3 介入）
 └── 依赖 O5 的路径信息（翻译 "该文件 xxx 未变化" 需要 file_path）

O3 (循环检测)
 ├── 是所有其他优化的最终防线
 ├── 只有 O2 翻译后模型仍未改变行为时才触发
 └── 独立于 O1/O4/O5（即使清除策略变化，循环检测逻辑不变）
```

### 7.4 优化生效的时序分析

在一次 Claude Code 会话中，优化的触发时序：

```
请求 1-5     O4 判断角色 → O1 阈值未到，跳过清除
请求 6-10    O1 首次触发清除 → O5 记录清除文件路径
请求 11-15   模型可能重读已清除文件 → O2 翻译 Wasted call
请求 16+     模型持续重复 → O3 检测循环并打断
```

**关键洞察**：O4→O1 是预防性优化（阻止问题发生），O2→O3 是反应性优化（问题发生后修复）。预防性优化在每轮请求都生效，反应性优化只在特定条件下触发。因此：

- **预防性指标**（context_inflation, clear_savings_rate, keep_high_prio_rate）应在每个会话中持续观测
- **反应性指标**（loop_rate, error_translation_rate）只在异常场景中出现，低频不代表无效

### 7.5 指标异常的根因定位

当某个指标偏离健康阈值时，按以下路径定位根因：

```
loop_rate 升高
 ├── 检查 clear_savings_rate 是否下降 → O1 清除不足导致信息丢失
 │       ├── 检查 keep_high_prio_rate → O1 分级是否准确
 │       └── 检查 dynamic_keep_rate → O4 是否正确识别角色
 ├── 检查 error_translation_rate → O2 是否覆盖了所有错误类型
 ├── 检查 max_consecutive → O3 阈值是否过大
 └── 检查 context_inflation → 对话是否过长超出 KEEP 补偿能力

tool_effectiveness 下降
 ├── 检查 keep_high_prio_rate → O1 是否误删了高价值内容
 ├── 检查 re_read_rate → 模型是否因信息丢失而重复读取
 │       ├── 检查 O5 是否保留了路径元数据
 │       └── 检查 O2 是否翻译了 Wasted call
 └── 检查 tool_diversity → 模型是否过度依赖单一工具

context_inflation 持续升高
 ├── 检查 clear_savings_rate → O1 是否充分清除
 │       └── 检查 PROXY_CLEAR_THRESHOLD 是否过高
 ├── 检查 dynamic_keep_rate → O4 是否对子代理给了过多保留
 └── 检查 ttft_increase → TTFT 是否因此恶化
```

### 7.6 配置参数对指标的敏感度分析

| 参数 | 调大 | 对指标的影响 |
|------|------|-------------|
| `PROXY_TOOL_KEEP` (当前=10) | → 15 | clear_savings_rate↓, re_read_rate↓, context_inflation↑, ttft_increase↑ |
| `PROXY_TOOL_KEEP` | → 5 | clear_savings_rate↑, re_read_rate↑, loop_rate↑, tool_effectiveness↓ |
| `PROXY_CLEAR_THRESHOLD` (当前=15000) | → 30000 | 清除延迟触发，前期 context_inflation↑，后期清除更激进 |
| `PROXY_LOOP_THRESHOLD` (当前=3) | → 5 | loop_rate↓（检测变少），max_consecutive↑（允许更多重复），loop_recovery_ms↑ |
| `PROXY_LOOP_THRESHOLD` | → 2 | loop_rate↑（更灵敏），误检率可能↑（正常重试被误判） |
| `Read` 优先级 (当前=3) | → 1 | keep_high_prio_rate↓（Read 内容被更多清除），re_read_rate↑ |
| `Wasted call` 优先级 (当前=0) | → 1 | Wasted call 保留更多 → clear_savings_rate↓, context_inflation↑ |

**最优平衡点**：当前配置（KEEP=10, THRESHOLD=3, Read=3, Wasted=0）在死循环场景中验证通过：
- O1 保留了 10 个高优先级 tool_result（keep_high_prio_rate=100%）
- O3 在第 3 次循环时打断（loop_recovery ~3min vs 219次无检测）
- O2 翻译了 222 条 Wasted call（error_translation_rate ~100%）

### 7.7 跨优化协同效应

某些优化组合产生的效果**大于单独效果之和**：

| 组合 | 协同效应 | 量化验证方式 |
|------|---------|-------------|
| O1 + O5 | O1 保留高价值内容，O5 对被清除内容保留路径 → 模型既能利用保留的完整内容，又知道"哪些文件已读过" | 对比有/无 O5 时的 re_read_rate |
| O2 + O3 | O2 翻译错误为自然语言（第一次引导），O3 在模型不响应时注入打断（第二次引导）→ 双层防御 | 对比有/无 O2 时的 loop_rate（O3 触发频率是否下降） |
| O4 + O1 | O4 为子代理提供更大保留预算（15），O1 在此预算内优先保留高价值内容 → 子代理获得比主进程更完整的上下文 | 对比子代理 vs 主进程的 tool_effectiveness |
| O1 + O2 | O1 保留 Read 内容（优先级3），O2 翻译 Wasted call（优先级0会被清除）→ 高价值内容保留，低价值错误被翻译后再清除 | 对比清除后 Wasted call 的 tool_result 是否被翻译 |

### 7.8 反向指标（需要关注的风险）

优化可能引入的负面效应：

| 优化 | 风险指标 | 阈值 | 说明 |
|------|---------|------|------|
| O1 | O1 处理耗时 | < 50ms | 语义打分遍历所有 tool_result，大量 tool_result 时可能成为瓶颈 |
| O2 | 翻译后 chars 变化 | 监控 | 自然语言翻译通常比原始错误消息长（如 "Wasted call" → 60+ 字的中文指导），可能增加 prompt |
| O3 | 误检率 | < 5% | 正常的重试（如网络超时后重试 Read）可能被误判为循环 |
| O4 | 主进程信息不足 | 监控 | 如果子代理判断逻辑有误，主进程可能被错误分配较小的 KEEP |
| O5 | 元数据 chars 开销 | < 总 chars 的 1% | 每个被清除的 tool_result 附加 `file=xxx` 元数据，累积可能可观 |

---

## 8. 后续优化方向与关联指标

| 优化方向 | 预期影响 | 关联指标 | 验证方法 |
|---------|---------|---------|---------|
| 智能预览（代码保留 import/签名） | 降低 `re_read_rate` | re_read_rate, tool_effectiveness | A/B 测试 |
| 交替循环检测（A→B→A→B） | 捕获更多循环模式 | loop_rate, max_consecutive | 对比检测率 |
| 主动缓存标记（清除时标注 Wasted） | 减少重复读取 | re_read_rate | A/B 测试 |
| 内容感知 KEEP（代码>日志>ls） | 提升 `keep_high_prio_rate` | keep_high_prio_rate, token_efficiency | A/B 测试 |
| 跨会话学习（记住历史循环模式） | 降低 `loop_rate` | loop_rate 趋势 | 长期观测 |
