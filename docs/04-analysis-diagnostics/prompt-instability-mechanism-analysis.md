# Agentic 截断策略导致 Prompt 不稳定的机制分析

> 分析日期: 2026-06-05
> 分析对象: rapid-mlx-35b 配置，PROXY_CTX_TRUNCATE_STRATEGY=rounds
>
> 目的: 解释为什么代理截断策略导致 prefix cache 命中率降为 0%，
> 并提供具体的改进方向。

---

## 一、当前配置参数

```bash
# configs/rapid-mlx-35b.conf 中的代理截断参数
PROXY_CLEAR_ENABLED=true
PROXY_CLEAR_THRESHOLD=30000
PROXY_TOOL_KEEP=8
PROXY_CTX_LIMIT_ENABLED=true
PROXY_CTX_KEEP_HEAD=2
PROXY_CTX_KEEP_TAIL=6
PROXY_CTX_TRUNCATE_STRATEGY=rounds
PROXY_CTX_KEEP_ROUNDS=8
PROXY_CTX_TOKEN_BUDGET=30000
PROXY_CTX_TOKEN_RATIO=2.0
```

---

## 二、核心问题：相邻请求的提示词重叠度仅 24%

### 2.1 实际数据

从后端日志提取相邻两个请求的 roles 序列：

```
Req 0: [system, user, system, user, assistant, user, assistant, tool, assistant, tool, assistant, user, assistant, tool, system, assistant, tool, assistant, tool, assistant, tool]
Req 1: [system, user, system, user, assistant, tool, assistant, tool, assistant, user, assistant, tool, system, assistant, tool, assistant, tool, assistant, tool, assistant, tool]

索引:    0      1     2      3      4           5     6           7     8           9     10          11    12          13    14     15          16    17          18    19          20
Req 0:   S      U     S      U      A           U     A           T     A           T     A           U     A           T     S      A           T     A           T     A           T
Req 1:   S      U     S      U      A           T     A           T     A           U     A           T     S           A     T      A           T     A           T     A           T
         ↑ 共同前缀到 [4]                           ↑ 分叉                                      ↑ 再次分叉                                    ↑ 共同后缀从 [15]
```

**关键指标：**
- 共同前缀长度：5 条消息（索引 0-4）
- 总消息数：21 条
- **共同前缀比例：5/21 = 23.8%**
- 分叉点：[5] user ≠ tool

### 2.2 多组相邻请求统计

| 请求对 | 消息数 | 共同前缀 | 比例 | 分叉点角色 |
|--------|--------|----------|------|------------|
| 0 → 1  | 21 → 21 | 5/21 | 23.8% | user ≠ tool |
| 1 → 2  | 21 → 21 | 7/21 | 33.3% | tool ≠ user |
| 2 → 3  | 21 → 22 | 5/21 | 23.8% | tool ≠ user |
| 3 → 4  | 22 → 22 | 5/22 | 22.7% | user ≠ tool |
| 4 → 5  | 22 → 21 | 6/22 | 27.3% | system ≠ assistant |

**平均共同前缀比例：约 26%**

---

## 三、为什么共同前缀这么短？—— 五个破坏机制

### 机制 1：轮次边界不固定（Rounds Strategy 的本质缺陷）

`rounds` 策略保留的是"最近 N 轮 assistant 对话"，但**每轮包含的消息数量是不固定的**：

```
简单轮次（无 tool 调用）: user → assistant                    = 2 条消息
普通轮次（1 个 tool）:     user → assistant → tool            = 3 条消息
复杂轮次（2 个 tools）:   user → assistant → tool → tool    = 4 条消息
```

当新轮次加入、旧轮次被挤出时：

```
保留窗口（8 轮）:
  轮次 1: 2 条消息  ← 被挤出
  轮次 2: 4 条消息
  轮次 3: 3 条消息
  轮次 4: 2 条消息
  轮次 5: 4 条消息
  轮次 6: 3 条消息
  轮次 7: 2 条消息
  轮次 8: 4 条消息
  轮次 9: 3 条消息  ← 新加入
```

**问题：轮次 1 被挤出（2 条），轮次 9 加入（3 条）**
- 为了保持总消息数在 21 条左右，还需要额外挤出 1 条消息
- 这导致轮次 2 的消息也可能被部分挤出
- 结果是**整个消息序列发生结构性重组**，不只是简单的"窗口滑动"

从 Req 0 → 1 的对比可以看到：
- [5] user → tool（角色变化）
- [6] assistant == assistant（又匹配了）
- [7] tool == tool（又匹配了）
- [8] assistant == assistant（又匹配了）
- [9] tool → user（再次分叉）

这说明消息序列不是简单地"右移"，而是**被打乱重组**了。

### 机制 2：Tool Clearing 改变消息内容

即使消息框架（roles）相同，tool_result 的内容也可能不同：

```python
# 旧的 tool_result（被清除前）:
{"role": "tool", "content": "```json\n{\"files\": [...]}\n```"}

# 清除后:
{"role": "tool", "content": "[cleared: original 12543 chars]"}
```

代理日志显示每次请求清除 **68-80 个 tool_results**，释放 **113K-163K 字符**。

即使 roles 序列看起来相同，token 序列（因为内容不同）也不同，
导致 Rapid-MLX 的哈希键不匹配。

### 机制 3：System 消息动态插入

从 roles 序列可以看到多个 `system` 消息：

```
索引 0: system   ← 头部系统提示（固定）
索引 2: system   ← 可能是 skills 或 dynamic reminder
索引 14: system  ← task tools 提醒或其他动态插入
```

这些 system 消息**不是都在头部**，而是分散在对话中间。
当新的 system 消息插入时（如 `The task tools haven't been used recently...`），
它可能出现在不同的索引位置，导致后续所有消息的索引偏移，破坏前缀匹配。

### 机制 4：Token Budget 动态削减轮次

```python
# 伪代码逻辑
estimated_tokens = total_chars * PROXY_CTX_TOKEN_RATIO  # ×2.0
if estimated_tokens > PROXY_CTX_TOKEN_BUDGET:            # >30000
    keep_rounds -= 1                                     # 减少保留轮次
```

当新增消息导致字符数超过 30,000 时：
- `keep_rounds` 从 8 减少到 7，甚至更少
- 这意味着**额外的一整轮对话被挤出**
- 导致前面消息序列发生更大变化

代理日志显示：`Context truncation (rounds): 168 messages dropped, 21 kept (rounds=8)`

说明当前 rounds=8 是**刚好卡在预算上限**的，任何新增消息都可能导致削减。

### 机制 5：新消息的"波纹效应"

Agentic 对话的每个新请求都会添加：
1. 上一轮 assistant 的回复（可能包含 tool_use）
2. tool 执行结果（tool_result）
3. 用户的新输入

这些新消息加入后：
- `keep_rounds` 窗口右移
- 旧轮次被挤出
- 但由于轮次内消息数不固定，挤出过程不均匀
- 加上 tool clearing 和 system 插入，最终形成**完全不同的消息序列**

---

## 四、数学解释：为什么 24% 的共同前缀无法命中缓存

### Rapid-MLX 缓存匹配机制

Rapid-MLX 的 prefix cache：
1. 基于**完整 prompt 的哈希键**查找缓存条目
2. 如果完整哈希不匹配，则**无法命中**
3. 即使支持部分前缀复用（如 `cached=32061/32807`），也需要**边界对齐**
   - 即消息边界（message boundary）需要与缓存中的 `boundary_snapshot` 对齐

### 为什么 24% 不够？

```
Req 0: [S, U, S, U, A, U, A, T, A, T, A, U, A, T, S, A, T, A, T, A, T]
Req 1: [S, U, S, U, A, T, A, T, A, U, A, T, S, A, T, A, T, A, T, A, T]
               ↑ 分叉点 [5]
```

虽然前 5 条消息相同，但第 5 条不同：
- Req 0[5] = user（用户的某条消息）
- Req 1[5] = tool（某个 tool_result）

这意味着：
1. **完整哈希完全不同**（因为第 5 条消息不同）
2. **边界快照无法对齐**（消息边界在第 5 条就分叉了）
3. 即使 Rapid-MLX 尝试部分匹配，也**找不到有效的缓存前缀**

**关键结论：**
- 要命中 prefix cache，需要**较长的稳定前缀**（ ideally >80%）
- 24% 的共同前缀意味着 76% 的消息都不同
- 这在 Rapid-MLX 的缓存机制下**不可能命中**

---

## 五、与"原始报文 97% 共同前缀"的对比

之前分析显示：**原始报文（代理截断前）有 97% 的共同前缀**。

这是因为：
- 原始报文包含完整的 160-170 条消息历史
- 相邻请求只新增 2-3 条消息
- 160/170 ≈ 94% 的消息完全相同

但代理截断后：
- 只保留 21 条消息（删除 85-90%）
- 21 条中又有 16 条不同
- **有效共同部分仅占约 5/21 ≈ 24%**

**这就是截断策略的"放大效应"：**
- 原始报文：新增 2 条 → 共同前缀 97%
- 截断后：新增 2 条 → 因为窗口滑动 + 重组 → 共同前缀 24%

---

## 六、五个破坏机制的叠加效应

```
原始报文 (170 条)
    │
    ├── [机制 1: 轮次边界不固定]
    │   新轮次 3 条消息加入，旧轮次 2 条消息被挤出
    │   → 消息序列重组，不是简单滑动
    │
    ├── [机制 2: Tool Clearing]
    │   68 个 tool_result 内容被替换为占位符
    │   → token 序列改变，即使角色相同也无法命中
    │
    ├── [机制 3: System 动态插入]
    │   system 消息插入到对话中间（索引 14）
    │   → 后续消息索引偏移
    │
    ├── [机制 4: Token Budget 削减]
    │   字符数超过 30K → keep_rounds 从 8 减到 7
    │   → 额外一轮被挤出
    │
    └── [机制 5: 新消息波纹效应]
        每次请求添加 assistant + tool_use + tool_result
        → 整个窗口被迫右移

    ↓
截断后报文 (21 条)
    │
    └── 相邻请求的共同前缀: 仅 24%
        → Prefix Cache 命中率: 0%
```

---

## 七、改进方向

### 方向 1：FIFO 截断策略（推荐实验）

改为"保留最近 N 条消息"（固定消息数），而不是"保留最近 N 轮"（动态消息数）：

```python
# 当前: rounds 策略
keep = head(2) + last_8_rounds(不定数量)

# 改进: FIFO 策略
keep = head(2) + last_N_messages(固定数量, 如 40 条)
```

**优点：**
- 每次新增固定数量的消息（如 2-3 条）
- 被挤出的也是固定数量的消息（最早的 2-3 条）
- 中间大部分消息保持稳定
- 共同前缀可能提升到 80-90%

**缺点：**
- 可能保留很多不相关的旧消息
- 无法智能保留"重要"的轮次

### 方向 2：保留最近 N 轮 + 固定轮次内消息数

限制每轮最多保留的消息数：

```python
# 每轮最多保留 3 条消息（user + assistant + 1 tool）
# 超出的 tool 结果被截断或合并
```

**优点：**
- 轮次边界更稳定
- 消息序列更可预测

**缺点：**
- 可能丢失重要 tool 结果
- 实现复杂

### 方向 3：禁用 Tool Clearing 或提高阈值

```bash
PROXY_CLEAR_ENABLED=false
# 或
PROXY_CLEAR_THRESHOLD=100000  # 大幅提高阈值
```

**优点：**
- 消息内容更稳定
- tool_result 不被替换

**缺点：**
- 上下文长度可能失控
- 可能导致后端 OOM

### 方向 4：使用摘要而不是占位符

对于被清除的 tool_result，使用**结构化摘要**而不是通用占位符：

```python
# 当前:
"[cleared: original 12543 chars]"

# 改进:
"[tool_result: list_files, 3 files returned, summary: src/main.py, tests/test.py, README.md]"
```

**优点：**
- 相同 tool 调用的摘要相同 → token 序列相同
- 保持语义信息的同时提高稳定性

**缺点：**
- 需要额外的摘要生成逻辑
- 摘要本身可能很长

### 方向 5：为 Prefix Cache 优化的专用截断策略

设计一个专门优化缓存命中的策略：

```python
def truncate_for_cache(messages, keep_head=2, min_stable_prefix=15):
    """
    确保保留足够多的稳定消息，使 prefix cache 能命中。
    """
    # 1. 保留头部（系统提示）
    stable = messages[:keep_head]
    
    # 2. 从尾部保留最近的消息
    #    但确保总消息数 > min_stable_prefix
    tail = messages[-min_stable_prefix:]
    
    # 3. 如果总长度超过限制，优先从"中间"删除
    #    而不是从"头部"删除
    ...
```

**核心思想：**
- 永远保留头部 + 最近 15 条消息
- 这样相邻请求至少有 head(2) + 中间(N-2) 条消息相同
- 共同前缀比例可提升到 70%+

---

## 八、实验建议

1. **修改配置，测试 FIFO 策略**：
   ```bash
   PROXY_CTX_TRUNCATE_STRATEGY=fifo
   PROXY_CTX_KEEP_MESSAGES=40  # 保留最近 40 条消息
   ```

2. **禁用 tool clearing，观察命中率变化**：
   ```bash
   PROXY_CLEAR_ENABLED=false
   ```

3. **使用 cache_analyzer.py 实时监控**：
   ```bash
   python3 tools/cache_analyzer.py --watch
   ```

4. **对比不同策略的代理日志**：
   - 观察相邻请求的 roles 序列重叠度
   - 观察 `cache_fetch` HIT/MISS 比例

---

## 九、总结

Agentic 截断策略导致 prompt 不稳定的**根本原因是设计目标冲突**：

| 目标 | 手段 | 副作用 |
|------|------|--------|
| 控制上下文长度 | 删除旧消息 | 窗口滑动，前缀不稳定 |
| 保留最近轮次 | 按轮次保留 | 轮次边界不固定，序列重组 |
| 减少 token 数 | Tool clearing | 内容改变，哈希不匹配 |
| 提醒 agent | System 动态插入 | 索引偏移，边界不对齐 |
| 控制预算 | 动态削减轮次 | 额外轮次被挤出 |

**五个机制叠加**，将原本 97% 的共同前缀压缩到 24%，
导致 Rapid-MLX 的 prefix cache **完全无法命中**（0% 命中率）。

要解决这个问题，需要**重新设计截断策略**，使其在控制长度的同时，
**最大化相邻请求的共同前缀比例**。FIFO 策略或"保留固定数量消息"的策略
可能是更优的选择。
