# 代理层优化工作日志

> 日期: 2026-06-03  
> 作者: Kimi Code CLI  
> 主题: 长对话 Context Bloat 治理 + 代理层性能优化  

---

## 1. 背景与问题

### 症状
- 每轮对话 prompt tokens 高达 **68K-93K**
- TTFT（首 token 延迟）**90-109 秒**
- 消息数膨胀至 **128+ 条/轮**
- Metal OOM 崩溃（48GB 内存耗尽）
- 生成速度从 56 tok/s 衰减到 12 tok/s

### 根因
1. **Claude Code 历史无限累积**：每轮追加新消息，不清理旧上下文
2. **Tool definitions 固定开销**：45 个 tools 占 ~11K tokens
3. **rapid-mlx Metal 性能衰减**：长运行后生成速度暴跌 78%
4. **重复并发 POST**：每轮 2 个相同请求叠加负载

---

## 2. 优化措施与实施记录

### Phase 1：调整清理参数（配置层）

**文件**: `configs/rapid-mlx-35b.conf`

| 参数 | 原值 | 新值 | 效果 |
|------|------|------|------|
| `PROXY_CLEAR_THRESHOLD` | 100000 | **30000** | 更早触发 tool-result 清理 |
| `PROXY_TOOL_KEEP` | 5 | **3** | 保留最近 3 对完整 tool-result |
| `PROXY_CTX_CHARS_LIMIT` | 350000 | **150000** | 更早触发消息截断 |
| `PROXY_CTX_KEEP_TAIL` | 4 | **6** | 保留更多最近上下文 |

**结果**: 每轮清理 50+ tool_results，释放 ~350K chars

---

### Phase 2：增强代理层逻辑（代码层）

**文件**: `anthropic_proxy.py`

#### 2.1 新增 `strip_old_thinking_blocks()`
- 删除旧 assistant 消息中的 `thinking` 类型 block 和 `<thinking>` XML 标签
- 保留最近 3 条，删除旧的
- 当前为**防御性代码**（实际请求中暂无独立 thinking block）

#### 2.2 新增 `compress_cleared_tool_results()`
- 识别 `assistant(PURE TOOL_USE) + user(cleared tool_result)` 连续对
- 将多个连续对合并为轻量摘要：
  ```
  assistant: "[Previous 5 tool calls: Read, Bash, Read, Edit, Read]"
  user:      "[5 tool results cleared]"
  ```
- 效果：每轮合并 1-21 个 cycles，减少 2-42 条消息，节省 ~200-2100 tokens 结构开销

---

### Phase 3：滑动窗口截断（架构层）

**核心思路**: 从"做减法"转向"定边界"——不再修修补补，而是直接丢弃旧消息。

**文件**: `anthropic_proxy.py` + `configs/rapid-mlx-35b.conf` + `manage.sh`

#### 3.1 实现 `truncate_messages_if_needed()` 的 `rounds` 策略

```python
# 保留头部 system context + 尾部最近 N 轮，中间用占位消息替代
PROXY_CTX_TRUNCATE_STRATEGY = "rounds"  # char | rounds
PROXY_CTX_KEEP_ROUNDS = 8
```

**算法**:
1. 保留前 `PROXY_CTX_KEEP_HEAD=2` 条（system/skills）
2. 从尾部向前收集最近 8 轮 assistant 对话
3. 中间丢弃的消息替换为占位消息：
   ```json
   {"role": "user", "content": "[Context folded: 141 earlier messages omitted.]"}
   ```
4. 处理连续 user role 风险（将占位文本合并到 tail 首条 user 消息）

#### 3.2 修复 manage.sh 环境变量传递

**文件**: `manage.sh`

问题：manage.sh 启动代理时未传递 `PROXY_CTX_TRUNCATE_STRATEGY` 和 `PROXY_CTX_KEEP_ROUNDS`。

修复：在 `nohup python3 anthropic_proxy.py` 前添加环境变量导出：
```bash
PROXY_CTX_TRUNCATE_STRATEGY="${PROXY_CTX_TRUNCATE_STRATEGY:-char}" \
PROXY_CTX_KEEP_ROUNDS="${PROXY_CTX_KEEP_ROUNDS:-10}" \
```

#### 3.3 修复日志兼容性问题

原 `_handle_messages` 日志代码假设 `trunc_stats` 包含 `dropped_chars`/`chars_before` 等字段（char 策略特有）。rounds 策略缺少这些字段，导致 `KeyError`。

修复：按策略类型分支输出日志：
```python
if strategy == "rounds":
    log(f"Context truncation (rounds): {dropped} dropped, {kept} kept")
else:
    log(f"Context truncation (char): {dropped} dropped, {chars} removed")
```

---

### Phase 4：Broken Pipe 修复（稳定性）

**文件**: `anthropic_proxy.py`

**问题**: 客户端超时断开后，代理层写入已关闭的响应流，抛出 `BrokenPipeError`。

**修复位置**:
1. `_emit_text_delta()` 内部：捕获 `BrokenPipeError` + `ConnectionResetError`
2. `message_stop` / `message_delta` 发送时：同上捕获
3. 静默处理，不抛出错误日志

**效果**: 修复前 30 个 Broken pipe 错误 → 修复后 0 个

---

### Phase 5：设计文档与 Review

**文件**: `docs/proxy-context-window-design.md` + `docs/proxy-context-window-design-review-merged.md`

- 编写完整设计文档（451 行）
- 接收并合并 8 条 review 意见（全部采纳）
- 更新 `AGENTS.md` 配置表

---

## 3. 效果对比

### 核心指标

| 指标 | 优化前 | 优化后 | 改善 |
|------|--------|--------|------|
| **消息数** | 128+ | **19** | -85% |
| **Prompt tokens** | 68K-93K | **33K** | **-52%** |
| **TTFT** | 90-109s | **27-32s** | **-70%** |
| **每轮总耗时** | 97-133s | **36-42s** | **-62%** |
| **生成速度（刚重启）** | 12 tok/s | **56 tok/s** | **+367%** |
| **生成速度（运行后）** | 12 tok/s | **12-14 tok/s** | 无改善 |
| **Tool definitions** | 45 | **30** | -33% |
| **Broken pipe 错误** | 30 | **0** | 修复 |

### 代理层处理链日志（典型一轮）

```
Tool clearing:           68 tool_results cleared, 362,404 chars freed
Context truncation:      141 messages dropped, 19 kept (rounds strategy)
Tool-result compression: 2 cycles merged, 4 msgs removed
```

---

## 4. 关键发现

### 4.1 Tool definitions 是硬瓶颈

Prompt 33K tokens 的构成：
- 30 个 Tool definitions: ~7,500 tokens (29.7%) ← **固定开销**
- System prompt: ~4,000 tokens (10.6%) ← **固定开销**
- Messages 内容: ~21,500 tokens (56.9%)
- Messages 结构: ~1,000 tokens (2.7%)

**结论**: 即使消息数降到 1 条，prompt 仍有 ~15K tokens。要进一步优化需减少 tool 数量（需改 Claude Code 配置）。

### 4.2 rapid-mlx Metal 性能衰减

| 运行时长 | 生成速度 | Prefill 速度 |
|----------|----------|-------------|
| 刚重启 | 56 tok/s | 1,391 tok/s |
| 7 分钟后 | 13.5 tok/s | 1,011 tok/s |
| 14 分钟后 | 12 tok/s | 1,044 tok/s |

**结论**: 运行时间越长，Metal 内存碎片化越严重，生成速度衰减 78%。唯一有效解决方案是**定期重启**。

### 4.3 重复并发 POST

Claude Code 每轮发送 **2 个完全相同的 POST 请求**，后端被迫双倍处理。这是客户端行为，代理层无法修复。

---

## 5. 遗留问题

| 问题 | 状态 | 建议 |
|------|------|------|
| rapid-mlx Metal 性能衰减 | ❌ 未解决 | 实施自动重启机制（每 30-60 分钟） |
| 生成速度 12→56 tok/s 差距 | ❌ 未解决 | 同上，重启后可恢复 |
| 重复并发 POST | ❌ 客户端行为 | 需 Claude Code 团队修复 |
| Tool definitions 仍占 30% | ⚠️ 部分解决 | 已从 45→30，可继续精简到 15 |
| Prefix cache 100% MISS | ❌ 上游限制 | rapid-mlx v0.6.30 的 MoE ArraysCache 不支持 trim，需等上游修复 |

---

## 6. 修改文件清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `anthropic_proxy.py` | 大幅修改 | 新增 3 个函数 + rounds 策略 + Broken pipe 修复 |
| `configs/rapid-mlx-35b.conf` | 多次修改 | 清理参数、截断策略、keep_rounds |
| `manage.sh` | 小幅修改 | 添加 2 个环境变量传递 |
| `AGENTS.md` | 小幅修改 | 新增 3 个配置参数到环境变量表 |
| `docs/proxy-context-window-design.md` | 新建 | 设计文档 451 行 |
| `docs/proxy-context-window-design-review-merged.md` | 新建 | Review 合并记录 |
| `tools/bench_rapidmlx.py` | 修正 | 端口 4000→8081 |

---

## 7. 下一步建议

1. **P0 - 自动重启机制**: 当检测到生成速度 < 20 tok/s 或运行时间 > 30 分钟时，自动重启 rapid-mlx
2. **P1 - 继续精简 tools**: 从 30 → 15，预计 prompt 再降 ~4K tokens
3. **P2 - 监控增强**: 在 `/status` 页面添加生成速度、TTFT 趋势图
4. **P3 - 占位消息语义化**: 基于被丢弃消息中的 tool 名和文件路径生成结构化摘要
