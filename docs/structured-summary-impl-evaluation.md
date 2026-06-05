# 结构化摘要替代占位符：代码实现评估

> 评估日期: 2026-06-05
>
> 目标: 将 `[cleared: 12345 chars]` 占位符替换为结构化摘要，
> 使相同 tool 调用生成相同的 token 序列，提升 prefix cache 命中率。

---

## 一、当前实现分析

### 1.1 占位符生成逻辑（第 694 行）

```python
# anthropic_proxy.py:694
block["content"] = f"[cleared{meta_info}: {original_len} chars.{preview}]"
```

其中：
- `meta_info`: ` file=src/main.py` 或 ` cmd=git status`
- `original_len`: 原始内容字符数（每次调用都不同）
- `preview`: 如果内容 >300 字符，取前 200 字符作为预览

**示例输出：**
```
[cleared file=src/main.py: 12543 chars. Preview: import os\ndef main():...]
```

### 1.2 问题：为什么当前占位符无法命中缓存？

| 因素 | 变化性 | 影响 |
|------|--------|------|
| `original_len` | 每次不同 | 占位符文本中包含具体数字，token 序列不同 |
| `preview` | 每次不同 | 前 200 字符预览因内容变化而不同 |
| `meta_info` | 相对稳定 | file_path 或 cmd 相对稳定 |

**结论：即使是同一文件的不同版本读取，占位符也不同 → 哈希不匹配。**

---

## 二、结构化摘要方案设计

### 2.1 核心原则

**对于相同的 `(tool_name, input_args)` 组合，始终生成完全相同的摘要文本。**

这样：
- 第 N 轮请求中 `Read(file="src/main.py")` 的摘要 = `X`
- 第 N+1 轮请求中 `Read(file="src/main.py")` 的摘要（如果该 tool_result 被保留在窗口中）= `X`
- token 序列相同 → 前缀匹配 → cache HIT

### 2.2 按 Tool 类型的摘要策略

#### Read / Write / Edit（文件操作）

```python
# 当前:
"[cleared file=src/main.py: 12543 chars. Preview: import os...]"

# 改进方案 A: 仅保留文件路径
"[cleared: Read file=src/main.py]"

# 改进方案 B: 保留文件路径 + 行数（更稳定）
"[cleared: Read file=src/main.py (12543 chars)]"  # 但字符数会变...

# 改进方案 C: 仅保留工具类型和路径（最稳定）
"[cleared: Read(src/main.py)]"
```

**推荐方案 C**：完全去掉长度和预览，只保留 `tool_name(file_path)`。

#### Bash

```python
# 当前:
"[cleared cmd=git status: 342 chars. Preview: On branch main...]"

# 改进:
"[cleared: Bash(\"git status\")]"
```

#### WebSearch / WebFetch

```python
# 当前:
"[cleared: 5678 chars. Preview: {'results': [...]}]"

# 改进:
"[cleared: WebSearch(query=\"...\")]"  # 保留查询关键词
```

#### Agent / EnterPlanMode

```python
# 改进:
"[cleared: Agent(task=\"...\")]"
```

---

## 三、代码修改点评估

### 修改点 1：`_clear_tool_results` 占位符生成（第 690-694 行）

**当前代码：**
```python
# Line 690-694
preview = ""
orig_str = str(original) if original else ""
if orig_str and original_len > 300:
    preview = f" Preview: {orig_str[:200]}..."
block["content"] = f"[cleared{meta_info}: {original_len} chars.{preview}]"
```

**修改后：**
```python
# 结构化摘要生成
summary = _generate_tool_summary(tool_name, meta_info)
block["content"] = f"[cleared: {summary}]"
```

**新增函数：**
```python
def _generate_tool_summary(tool_name, meta_info):
    """Generate deterministic summary for a cleared tool result.
    Same (tool_name, meta_info) always produces the same output.
    """
    if not meta_info:
        return tool_name or "tool"
    # meta_info format: " file=path" or " cmd=command"
    if meta_info.startswith(" file="):
        return f"{tool_name}({meta_info[6:]})"
    elif meta_info.startswith(" cmd="):
        return f'{tool_name}("{meta_info[5:].strip()}")'
    return f"{tool_name}{meta_info}"
```

**影响范围：** 仅第 690-694 行，约 10 行代码变更。

---

### 修改点 2：`clear_old_tool_results` 中的 Bash dedup（第 698-722 行）

**当前代码：**
```python
# Line 717-720
messages[msg_idx_b]["content"][block_idx_b]["content"] = (
    f"[deduplicated Bash output (sim={int(jaccard*100)}%): "
    f"{len(cb)} chars. Preview: {cb[:200]}...]"
)
```

**问题：** 包含 `sim=73%` 和 `len(cb)`，每次都不同。

**修改后：**
```python
messages[msg_idx_b]["content"][block_idx_b]["content"] = (
    "[cleared: Bash(deduplicated)]"
)
```

**影响范围：** 第 717-720 行，约 3 行代码变更。

---

### 修改点 3：`_is_cleared_tool_result_msg` 检测（第 1200-1219 行）

**当前代码：**
```python
# Line 1212
if "[cleared to save context:" not in block_content and "[Result of tool call hidden]" not in block_content:
    return False
# Line 1219
return "[cleared to save context:" in block_content or "[Result of tool call hidden]" in block_content
```

**问题：** 硬编码检测旧格式字符串。

**修改后：**
```python
# 更通用的检测：只要包含 "[cleared:" 前缀即可
CLEARED_PREFIXES = ("[cleared:", "[cleared to save context:", "[Result of tool call hidden]")

if not any(p in block_content for p in CLEARED_PREFIXES):
    return False
return any(p in block_content for p in CLEARED_PREFIXES)
```

**影响范围：** 第 1212 行和第 1219 行，约 4 行代码变更。

---

### 修改点 4：`compress_cleared_tool_results` 合并（第 1268-1275 行）

**当前代码：**
```python
# Line 1268-1275
compressed.append({
    "role": "assistant",
    "content": [{"type": "text", "text": f"[Previous {len(cycles)} tool calls: {', '.join(tool_names)}]"}],
})
compressed.append({
    "role": "user",
    "content": [{"type": "text", "text": f"[{len(cycles)} tool results cleared]"}],
})
```

**问题：**
- `len(cycles)` 每次请求都不同（因为窗口滑动导致保留的 cycles 数量不同）
- `tool_names` 列表可能因顺序不同而不同

**修改后：**
```python
# 按工具名称排序，确保顺序稳定
tool_names_sorted = sorted(set(tool_names))
compressed.append({
    "role": "assistant",
    "content": [{"type": "text", "text": f"[Previous tool calls: {', '.join(tool_names_sorted)}]"}],
})
compressed.append({
    "role": "user",
    "content": [{"type": "text", "text": "[tool results cleared]"}],
})
```

**注意：** 即使这样，`tool_names_sorted` 仍然会因为窗口滑动而不同（不同请求保留的 cycles 不同）。

**更好的方案：** 不要合并 cycles，保持每个 cycle 独立，但每个 tool_result 使用结构化摘要。

**影响范围：** 第 1268-1275 行，约 6 行代码变更。

---

### 修改点 5：`_is_cleared_tool_result_msg` 的辅助函数（第 1200 行）

**可能需要新增：** 一个工具函数来标准化摘要格式，确保所有地方使用统一的 `[cleared: ...]` 前缀。

```python
def _make_cleared_placeholder(summary):
    """Generate standardized cleared placeholder."""
    return f"[cleared: {summary}]"

def _is_cleared_content(text):
    """Check if text is a cleared placeholder (any format)."""
    return isinstance(text, str) and text.startswith("[cleared")
```

---

## 四、修改工作量评估

| 修改点 | 文件 | 行数 | 复杂度 | 风险 |
|--------|------|------|--------|------|
| 占位符生成逻辑 | `anthropic_proxy.py:690-694` | ~10 行 | 低 | 低 |
| Bash dedup 占位符 | `anthropic_proxy.py:717-720` | ~3 行 | 低 | 低 |
| 清除状态检测 | `anthropic_proxy.py:1212,1219` | ~4 行 | 低 | 中（需兼容旧格式）|
| 合并 cycles 文本 | `anthropic_proxy.py:1268-1275` | ~6 行 | 中 | 中 |
| 新增辅助函数 | `anthropic_proxy.py` | ~10 行 | 低 | 低 |
| **合计** | | **~33 行** | **低** | **低-中** |

**预计开发时间：15-30 分钟**
**测试时间：30-60 分钟**（需验证 unit tests + e2e + cache 命中率）

---

## 五、预期效果评估

### 5.1 收益分析

**能解决的问题：**
- ✅ 相同 tool 调用的 cleared 占位符 token 序列相同
- ✅ 如果同一个 `Read(file="src/main.py")` 在多轮中都被保留，其摘要相同 → 前缀匹配

**不能解决的问题：**
- ❌ rounds 策略导致的窗口滑动（轮次边界不固定）
- ❌ 新消息加入导致旧轮次被挤出
- ❌ system 消息动态插入
- ❌ token budget 动态削减轮次

### 5.2 实际命中率提升预测

从数据看，当前每轮保留 21 条消息，其中约 5-9 条是 tool_result。
如果 tool clearing 清除了其中 6-8 个，这些被清除的 tool_result 的摘要如果稳定：

**场景：假设某轮次有 3 个 tool_result 被清除，下一轮这 3 个仍在窗口中**
- 当前：3 个不同的 `[cleared: ...12345 chars]` → 3 处 token 序列不同
- 改进后：3 个相同的 `[cleared: Read(src/main.py)]` → 3 处 token 序列相同

**但这只有在"相同的 tool_result 在多轮中同时被保留"时才有效。**

实际上，由于 rounds 策略每轮都会挤出旧轮次，**大多数 tool_result 只会在窗口中存在 1-2 轮**。
所以结构化摘要只能影响"最近 keep 轮次内重复出现的 tool 调用"，这在 agentic 场景中很少见。

**保守估计：命中率提升 0-5%（因为 rounds 滑动是主要问题，tool clearing 只是次要因素）**

---

## 六、关键限制：为什么结构化摘要收益有限

### 6.1 根本问题：Rounds 滑动 > Tool Clearing

从数据看，共同前缀 24% 的主要原因是：
- 轮次边界不固定（机制 1）：贡献 ~50% 的不稳定性
- System 动态插入（机制 3）：贡献 ~20%
- Token budget 削减（机制 4）：贡献 ~15%
- **Tool Clearing（机制 2）：贡献 ~15%**

即使 tool clearing 完全稳定，共同前缀也只能从 24% 提升到 ~35%，
仍然远低于缓存命中所需的 80%+。

### 6.2 更好的投资方向

如果目标是提升 prefix cache 命中率，**优先修改 rounds 策略**（改为 FIFO 或固定消息数）
的收益远高于优化 tool clearing。

| 改进方向 | 实现复杂度 | 预期命中率提升 | 推荐优先级 |
|----------|-----------|----------------|------------|
| 结构化摘要（本方案） | 低（30 分钟） | 0-5% | P2 |
| FIFO 截断策略 | 中（1-2 小时） | 40-60% | **P1** |
| 禁用 Tool Clearing | 低（5 分钟改配置） | 5-10% | P2 |
| 固定轮次内消息数 | 中（1 小时） | 20-30% | P1 |

---

## 七、结论

### 技术可行性：✅ 容易实现

- 仅需修改约 30 行代码
- 风险低（只改变占位符文本格式，不改变逻辑）
- 向后兼容（检测函数支持旧格式）

### 实际收益：⚠️ 有限

- 只能解决 tool clearing 导致的 token 序列变化
- 无法解决 rounds 滑动、system 插入、token budget 等更主要的问题
- 预期命中率提升 0-5%

### 建议

1. **如果只做这一改动**：可以实施，但预期效果有限，不要抱太高期望。
2. **如果配合 FIFO 策略**：结构性摘要 + FIFO 截断可以协同工作，
   将共同前缀从 24% 提升到 80%+，命中率可能有质的飞跃。
3. **实施顺序**：先改 FIFO 截断（高回报），再改结构化摘要（锦上添花）。
