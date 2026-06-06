# 代理层上下文窗口替换设计 — Review 意见

> 审阅对象: `docs/proxy-context-window-design.md`
> 审阅日期: 2026-06-03
> 状态: 已修订（设计文档 v2 已整合全部 P1-P3 + S1-S5 意见及行业方案调研）

---

## 总体评价

文档结构清晰，量化分析扎实，风险识别全面。但存在**与现有实现冲突/重叠**的问题，以及核心数据假设不一致，需修订后再进入实施。

---

## 需修正问题

### P1: 与现有 `truncate_messages_if_needed` 功能重叠

**严重程度**: 高
**位置**: 设计文档 3.2 节、3.3 节

**现状**:

`anthropic_proxy.py:552-620` 已实现基于字符阈值的截断：

```python
truncate_messages_if_needed(messages)
# PROXY_CTX_CHARS_LIMIT=180000, PROXY_CTX_KEEP_HEAD=2, PROXY_CTX_KEEP_TAIL=4
```

设计方案 `replace_with_recent_window` 本质上也是"保留头尾、丢弃中间"。

**对比**:

| 维度 | 现有 truncate | 设计方案 window |
|------|-------------|----------------|
| 触发条件 | 超过字符阈值 (180K) | 按轮数定义窗口 |
| 丢弃粒度 | 逐条删到低于阈值 | 按轮数批量替换 |
| 占位消息 | 无（静默丢弃） | 有（`[Context folded]`） |
| 保留尾部 | 固定 4 条 | 按轮数（8-15轮） |

**建议**: 不新增独立函数，**增强 `truncate_messages_if_needed`**：

- 添加 `PROXY_CTX_TRUNCATE_STRATEGY=char|rounds` 选择模式
- `rounds` 模式触发时插入占位消息
- 复用现有 `PROXY_CTX_KEEP_HEAD` 配置

---

### P2: 核心数据假设不一致

**严重程度**: 高
**位置**: 设计文档 L38 vs 附录 L372

- L38 声称每条消息结构开销 **~80 tokens**
- 附录 L372 推导出每条消息结构开销 **~487 tokens**

两者相差 6 倍，直接影响收益估算的可信度。

同样，L69 声称目标是将 prompt 从 68K 降到 **15K-20K**，但附录 L378 估算为 **21,200 tokens**，超出目标范围。

**建议**:

1. 统一结构开销数字，明确区分"纯 JSON 括号开销"和"含 role/ID/type 的总开销"
2. 用 tiktoken 对一条典型 Anthropic 消息做精确测量，替换估算值
3. 调整目标范围或在附录中修正预估

---

### P3: 算法边界条件缺失

**严重程度**: 中
**位置**: 设计文档 3.2 节算法流程

```python
for msg in reversed(messages):
    tail.insert(0, msg)
    if msg.get("role") == "assistant":
        assistant_count += 1
    if assistant_count >= keep_rounds:
        break
```

问题：

1. **没有跳过 head 区域** — 如果消息总数少于 `head + tail`，tail 会包含 head 中的消息，导致 head 消息被重复
2. **尾部第一轮可能不完整** — reversed 后遇到的第一个 assistant 不一定是完整对话轮的开始（可能是 tool_result 后的 assistant）
3. **`dropped` 索引假设** — `messages[HEAD : len(messages) - len(tail)]` 需确保 `len(messages) - len(tail) >= HEAD`

**建议**: 添加边界检查：

```python
tail_start = len(messages) - len(tail)
if tail_start <= PROXY_CTX_KEEP_HEAD:
    return messages, {"enabled": False}
```

并在函数开头增加：

```python
if len(messages) <= PROXY_CTX_KEEP_HEAD + keep_rounds * 3:
    return messages, {"enabled": False}
```

---

## 建议改进

### S1: 明确 window 与 truncate 的关系

**位置**: 设计文档 3.3 节

当前设计的执行顺序：

```
window(大刀) → clear(中刀) → truncate(小刀) → think_strip → compress
```

window 之后消息量已大幅减少，`truncate_messages_if_needed` 基本不会再触发。两层兜底逻辑共存会增加调试难度。

**建议**: 在文档中明确两者关系为**互斥**：window 启用时自动禁用 truncate。配置层面：

```
PROXY_WINDOW_ENABLED=true  →  强制 PROXY_CTX_LIMIT_ENABLED=false
```

---

### S2: 占位消息的连续 user role 风险

**位置**: 设计文档 3.4 节

占位消息角色选为 `user`，但 Anthropic API 中连续两条 `user` 消息可能被合并或拒绝。如果 window 尾部的第一条恰好也是 `user`，会出现连续 user。

**建议**: 检查 tail 首条消息的 role：

```python
if tail and tail[0].get("role") == "user":
    # 将占位文本追加到 tail[0] 的 content 前面
    tail[0]["content"] = summary_blocks + tail[0].get("content", [])
else:
    # 单独放一条 user 消息
    result = head + [summary_msg] + tail
```

---

### S3: 补充 streaming 场景下的 tool_use_id 一致性风险

**位置**: 设计文档 5.1 节风险矩阵

设计只考虑了请求体处理，未提及 streaming 响应下模型可能引用被丢弃消息中的 `tool_use_id`。如果模型在生成过程中输出了旧 `tool_use_id`，Claude Code SDK 可能报错。

**建议**: 在风险矩阵中新增：

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| streaming 中引用已丢弃的 tool_use_id | 低 | 中 | 保留被丢弃消息中的 tool_use_id 集合，注入占位消息供模型参考；或依赖模型自然避免引用 |

---

### S4: 配置参数需登记到 AGENTS.md

**位置**: 设计文档第 6 节

新增的配置参数（`PROXY_WINDOW_ENABLED`、`PROXY_WINDOW_ROUNDS`、`PROXY_WINDOW_DYNAMIC`）未在 AGENTS.md 的配置表中登记，也未说明与现有 `PROXY_CTX_LIMIT_*` 的优先级关系。

**建议**: 实施时同步更新 AGENTS.md 的环境变量表，并明确优先级：

```
PROXY_WINDOW_ENABLED=true  →  忽略 PROXY_CTX_LIMIT_*
PROXY_WINDOW_ENABLED=false →  使用 PROXY_CTX_LIMIT_* 控制截断
```

---

### S5: Prefill 速度和生成速度数据需标注来源

**位置**: 设计文档 4.1 节、4.3 节

| 数据 | 文档值 | 缺失信息 |
|------|--------|----------|
| Prefill 速度 @ 68K | ~750 tok/s | 未引用 bench 脚本或日志 |
| Prefill 速度 @ 20K | ~2200 tok/s | 未引用 bench 脚本或日志 |
| 生成速度 @ 68K | 12.5 tok/s | 未引用 bench 脚本或日志 |
| 生成速度 @ 20K | 65-70 tok/s | 预期值，非实测 |

**建议**: 标注数据来源（如 `bench_mtp.py --quick` 输出、后端日志 `timings` 字段等），或明确标注"实测"与"预估"。

---

## 修订状态

| 编号 | 类型 | 描述 | 优先级 | 状态 |
|------|------|------|--------|------|
| P1 | 需修正 | 与 truncate 功能重叠，建议增强而非新建 | 高 | ✅ 已整合（3.2 节改为增强 `truncate_messages_if_needed`） |
| P2 | 需修正 | 结构开销数据不一致（80 vs 487 tokens） | 高 | ✅ 已修正（附录拆解为 Tool definitions + System + Messages 内容 + Messages 结构） |
| P3 | 需修正 | 算法边界条件缺失 | 中 | ✅ 已修正（添加 overlap 检查和 min_msgs 边界） |
| S1 | 改进 | window 与 truncate 互斥关系 | 中 | ✅ 已整合（3.3 节策略互斥原则） |
| S2 | 改进 | 连续 user role 处理 | 中 | ✅ 已整合（3.4 节连续 user role 处理逻辑） |
| S3 | 改进 | streaming tool_use_id 一致性 | 低 | ✅ 已整合（6.1 节风险矩阵新增条目） |
| S4 | 改进 | 配置参数登记到 AGENTS.md | 低 | 📋 实施时执行（Phase 1 checklist） |
| S5 | 改进 | 量化数据标注来源 | 低 | ✅ 已修正（4.1 节、4.3 节标注数据来源） |

---

## 行业调研补充

基于 OpenAI Cookbook、Anthropic 文档、MemGPT、Cursor/Aider 等产品的调研，设计文档 v2 新增了以下内容：

1. **第 5 节：行业方案调研** — 8 种主流方案对比表 + 我们方案定位 + 行业建议取舍
2. **Token 预算动态触发**（5.5 节） — 采纳 Kimi 建议和 OpenAI Cookbook 实践，用 `PROXY_CTX_TOKEN_BUDGET` 替代纯固定轮数
3. **Phase 4 高级优化路线图**（8 节） — LLM summary、高频小步压缩、阶段感知压缩

**关键取舍**：Phase 1 不引入 LLM 调用做 summary（增加延迟和复杂度），先验证静态占位 + token 预算的基本效果。

- `docs/proxy-context-window-design.md` — 被审阅的设计文档
- `anthropic_proxy.py:552-620` — 现有 `truncate_messages_if_needed` 实现
- `anthropic_proxy.py:1717-1789` — 现有 `_handle_messages` 执行链
- `AGENTS.md` — 项目配置文档（需同步更新）
