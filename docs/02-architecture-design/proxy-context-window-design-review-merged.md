# 代理层上下文窗口替换设计 — Review 合并记录

> 审阅对象: `docs/proxy-context-window-design.md`
> Review 文档: `docs/proxy-context-window-design-review.md`
> 合并日期: 2026-06-03
> 合并人: Kimi Code CLI
> 状态: **已完成**

---

## 合并摘要

| Review 编号 | 类型 | 结论 | 修改位置 |
|-------------|------|------|----------|
| P1 | 需修正 | ✅ **采纳** | 设计文档 3.2 节、3.3 节、6 节 |
| P2 | 需修正 | ✅ **采纳** | 设计文档 1.2 节、4.1/4.3 节、附录 9 |
| P3 | 需修正 | ✅ **采纳** | 设计文档 3.2 节算法流程 |
| S1 | 建议改进 | ✅ **采纳** | 设计文档 3.3 节、6 节 |
| S2 | 建议改进 | ✅ **采纳** | 设计文档 3.4 节 |
| S3 | 建议改进 | ✅ **采纳** | 设计文档 5.1 节风险矩阵 |
| S4 | 建议改进 | ✅ **采纳** | `AGENTS.md` 环境变量表 |
| S5 | 建议改进 | ✅ **采纳** | 设计文档 4.1/4.3 节 |

**未采纳意见**: 无（8/8 全部采纳）

---

## 逐项说明

### P1: 与现有 `truncate_messages_if_needed` 功能重叠

**Review 意见**: 不新增独立函数，增强现有 `truncate_messages_if_needed`，添加 `char|rounds` 策略选择。

**修改内容**:
- 删除原设计中的 `replace_with_recent_window()` 独立函数
- 将 `rounds` 策略内嵌到 `truncate_messages_if_needed()` 中作为分支
- 保留 `char` 策略作为默认行为，确保向后兼容
- 新增配置参数 `PROXY_CTX_TRUNCATE_STRATEGY` 控制策略选择

**设计文档变更**: 3.2 节算法流程重写，3.3 节执行顺序调整，6 节配置参数表更新。

---

### P2: 核心数据假设不一致

**Review 意见**: `~80 tokens` 与 `~487 tokens` 相差 6 倍；目标 `15K-20K` 与附录 `21,200` 不一致。

**修改内容**:
- 1.2 节重新分析 prompt 构成，引入 **Tool definitions 固定开销**（~8K-12K tokens）
- 删除"80 tokens/条"和"487 tokens/条"两个矛盾数字
- 调整核心目标为 **25K-35K tokens**（更保守、更可信）
- 附录 9 重写为 Prompt Token 构成分析，明确拆解 tool defs / system / messages content / messages structure
- 4.1/4.3/4.4 节同步调整收益预估（20K → 25K 作为基准）

---

### P3: 算法边界条件缺失

**Review 意见**: 缺少 tail 与 head 重叠检查，缺少最小消息数检查。

**修改内容**:
- 添加 `min_msgs = PROXY_CTX_KEEP_HEAD + keep_rounds * 3` 提前返回
- 添加 `tail_start` 计算和 `tail_start <= PROXY_CTX_KEEP_HEAD` 重叠检查
- 重叠时返回 `"reason": "overlap"` 而非静默处理

**设计文档变更**: 3.2 节算法流程 Step 2 后新增边界检查代码块。

---

### S1: 明确 window 与 truncate 的互斥关系

**Review 意见**: `rounds` 启用时 `truncate` 基本不触发，建议互斥。

**修改内容**:
- 3.3 节新增"策略互斥原则"说明
- 6 节配置参数表明确：`PROXY_CTX_TRUNCATE_STRATEGY=rounds` 时跳过 `char` 逻辑
- 执行顺序简化为 `clear → think_strip → compress → truncate(rounds|char)`

---

### S2: 占位消息的连续 user role 风险

**Review 意见**: Anthropic API 中连续两条 `user` 消息可能被合并或拒绝。

**修改内容**:
- 3.4 节新增连续 user role 处理逻辑
- 算法中检查 `tail[0].get("role") == "user"`，将占位文本合并到 tail 首条 user 消息前面
- 仅在 tail 首条非 user 时才单独插入占位消息

---

### S3: streaming 场景下的 tool_use_id 一致性风险

**Review 意见**: 模型可能在 streaming 生成中引用已被丢弃消息中的旧 `tool_use_id`。

**修改内容**:
- 5.1 节风险矩阵新增一行："Streaming 中引用已丢弃的 tool_use_id"
- 缓解措施：依赖模型自然避免引用窗口外 ID；Claude Code SDK 报错后会触发重试

---

### S4: 配置参数需登记到 AGENTS.md

**Review 意见**: 新增配置参数未在 AGENTS.md 中登记，优先级关系不明。

**修改内容**:
- `AGENTS.md:248-249` 后新增 3 行配置参数：
  - `PROXY_CTX_TRUNCATE_STRATEGY`
  - `PROXY_CTX_KEEP_ROUNDS`
  - `PROXY_CTX_KEEP_ROUNDS_DYNAMIC`
- 描述中明确 `char` 与 `rounds` 策略的互斥关系

---

### S5: Prefill 速度和生成速度数据需标注来源

**Review 意见**: 量化数据未区分"实测"与"预估"。

**修改内容**:
- 4.1 节新增"数据来源标注"段落，列出每项数据的精确来源（后端日志 request ID、bench 脚本输出）
- 4.3 节标注生成速度数据来源（`request=1119ebe6-523` 实测 vs `bench_rapidmlx.py` 基准）
- 表格中 25K/15K 等中间值明确标注为"插值预估"

---

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `docs/proxy-context-window-design.md` | 大幅修订 | 451 行，8 处标注 review 意见 |
| `AGENTS.md` | 小幅增补 | 新增 3 个环境变量到配置表 |
| `docs/proxy-context-window-design-review-merged.md` | 新建 | 本合并记录文档 |

---

## 遗留事项

以下事项未在本次合并中实施，留待 Phase 1 编码阶段处理：

1. **tiktoken 精确测量**: 附录中的 token 估算仍为粗略值，实施时可用 tiktoken 对典型 Anthropic 消息做精确测量
2. **动态窗口策略代码**: 5.3 节的 `compute_keep_rounds()` 为伪代码，实施时需完整实现
3. **单元测试覆盖**: `truncate_messages_if_needed` 的 `rounds` 分支需补充测试用例
