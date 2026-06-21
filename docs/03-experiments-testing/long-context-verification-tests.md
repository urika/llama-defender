# 长上下文场景验证测试设计

> **日期**: 2026-06-21  
> **数据来源**: `logs/proxy_metrics.jsonl` (2932 条, 2026-06-05 ~ 2026-06-21)  
> **分析目标**: 基于 Claude Code agentic coding 真实日志，设计针对性验证测试  
> **实施状态**: TC-1/TC-2/TC-4 已实现自动化测试 (`test/integration/test_long_context_integration.sh`)，10/10 通过

---

## 1. 日志分析发现

### 1.1 核心数据对比（修复前 vs 修复后）

| 指标 | 修复前 (06-05 ~ 06-14) | 修复后 (06-14 ~ 06-21) | 变化 |
|------|----------------------|----------------------|------|
| 总请求数 | 1131 | 261 | — |
| 200 成功率 | 87% (992/1131) | 84% (221/261) | -3% |
| **500 错误** | **63 (5.6%)** | **0 (0%)** | **✅ 消除** |
| 503 错误 | 32 (2.8%) | 33 (12.6%) | ⚠️ 增加（全是后端未运行） |
| loop_injected 率 | **26% (299)** | **9% (24)** | **✅ 下降 65%** |
| high_drop_ratio | 99 (8.7%) | **0** | **✅ 消除** |
| >90K chars 请求占比 | 32% (367) | 6% (16) | ✅ 大幅下降 |
| >180K chars 请求占比 | 16% (181) | 0.4% (1) | ✅ 几乎消除 |

**关键发现**：
1. **DEF-001 (500 错误率 22%) 已从代码层面消除**——修复后 0 个 500 错误
2. **DEF-002 (循环注入率 37%) 显著改善**——从 26% 降至 9%
3. **high_drop_ratio 完全消除**——截断策略改进生效
4. **503 错误全部是 "Connection refused"**（后端未运行时的请求），非 OOM 导致

### 1.2 上下文长度与错误率关系（全量数据）

| 阶段 | chars 范围 | 请求数 | 500 错误 | 503/504 | loop_injected | high_drop | p50 延迟 |
|------|-----------|--------|---------|---------|--------------|-----------|---------|
| INIT | < 15K | 525 | 7 | 5 | 3 (0.6%) | 0 | 3.3s |
| GROWTH | 15-40K | 196 | 0 | 27 | 24 (12%) | 0 | 17.3s |
| EXPANSION | 40-90K | 288 | 2 | 22 | 75 (**26%**) | 0 | 27.0s |
| SATURATION | 90-180K | 201 | 4 | 11 | 109 (**54%**) | 2 | 33.3s |
| OOM_DANGER | 180-350K | 75 | 0 | 0 | 63 (**84%**) | 33 (44%) | 26.2s |
| CRITICAL | 350K+ | 107 | **50 (47%)** | 0 | 49 (46%) | 64 (60%) | 18.7s |

**关键发现**：
1. **350K+ 是死亡线**：47% 的 500 错误、60% 的 high_drop_ratio 集中在此区间
2. **循环注入在 EXPANSION 阶段开始爆发**（26%），SATURATION 阶段达 54%
3. **延迟在 SATURATION 达到峰值**（p50=33s），但 OOM_DANGER 反而下降（因为请求被快速拒绝）
4. **Pipeline 防线触发率为 0%**：trunc/compress/clear 在所有区间均未触发——这是一个严重问题

### 1.3 Pipeline 触发率分析

```
所有 2932 条请求中（实际 metric key 名称）:
  truncate.applied=True       = 189 次 (6.4%)
  tool_clear.applied=True     = 53 次 (1.8%)   ← 多为旧配置记录
  semantic_compress.enabled   = 18 次 (0.6%)
  pre_truncate.triggered      = 92 次 (3.1%)
```

**说明**：
- `PROXY_CLEAR_ENABLED=false`（活跃配置显式关闭）——这是 v0.5.2 刻意设计，
  防止 rapid-mlx 的 "Wasted call" 死循环（见 AGENTS.md ⚠️ WARNING）
- 63% 的请求是测试/bench 流量（<5K chars），无法触发任何 L2-L5 防线
- `semantic_compress` 仅在成功压缩时记录 metric（无 "运行但无操作" 记录）
- `fifo` 截断策略仅在消息数 > 30 时触发，短会话不达到阈值

### 1.4 典型长上下文会话模式

从 session `a309b181`（168 请求，最大 645K chars）看到的典型模式：

```
轮次 1-30:  正常增长 35K → 90K (EXPANSION)
轮次 30-60: 失控增长 90K → 260K (SATURATION→OOM_DANGER)
轮次 60-100: 爆炸增长 260K → 540K (CRITICAL, loop_injected 每轮触发)
轮次 100+:  全部 500 错误，会话死亡
```

修复后的 session `0b38e096`（109 请求，最大 199K chars，100% 200）：
- 上下文始终控制在 200K 以内
- 多次回到 INIT 阶段（说明 Claude Code 的 `/compact` 或新子会话在起作用）
- `pre_truncate` 在 5 个请求中触发，成功防止了失控增长

---

## 2. 验证测试设计

基于上述发现，设计 8 个验证测试场景，覆盖从正常到极端的所有情况。

### 测试矩阵

| 编号 | 场景 | 验证目标 | 数据依据 |
|------|------|---------|---------|
| TC-1 | 渐进式上下文增长 | 生命周期阶段正确切换 | §1.2 阶段阈值表 |
| TC-2 | 350K+ 超大请求 | 413/503 正确拦截 | §1.2 CRITICAL 区间 47% 500 |
| TC-3 | 循环检测+截断联动 | EXPANSION 阶段循环不失控 | §1.2 EXPANSION loop=26% |
| TC-4 | Pipeline 防线触发 | trunc/compress/clear 实际生效 | §1.3 触发率 0% 异常 |
| TC-5 | 长会话稳定性 | 100+ 请求不崩溃 | §1.4 session 0b38e096 |
| TC-6 | 后端不可用恢复 | 503 → 重启 → 200 | §1.1 503 全是 Connection refused |
| TC-7 | 热重载不丢上下文 | reload 后配置立即生效 | Phase 0 dual-setattr |
| TC-8 | 内存压力拒绝 | OOM 阈值正确触发 503 | DEF-005 |

---

### TC-1: 渐进式上下文增长 — 生命周期阶段验证

**验证目标**: 确认 `_classify_lifecycle_stage()` 在正确的字符阈值切换阶段

**前置条件**: 本地后端运行，`PROXY_CTX_LIMIT_ENABLED=true`

**测试步骤**:

```python
# 模拟 Claude Code 的渐进式上下文增长
# 每轮增加 ~5000 chars（典型的 Read + Edit 往返）
test_prompts = [
    # Round 1: INIT (< 15K)
    {"msgs": 2, "target_chars": 5000},
    # Round 2-3: GROWTH (15K-40K)
    {"msgs": 5, "target_chars": 20000},
    {"msgs": 8, "target_chars": 35000},
    # Round 4-5: EXPANSION (40K-90K)
    {"msgs": 15, "target_chars": 55000},
    {"msgs": 20, "target_chars": 80000},
    # Round 6-7: SATURATION (90K-180K)
    {"msgs": 30, "target_chars": 120000},
    {"msgs": 45, "target_chars": 160000},
]
```

**验证点**:

| 轮次 | 输入 chars | 预期阶段 | 预期 clear_zone_pct | 预期 truncate_rounds |
|------|-----------|---------|-------------------|---------------------|
| 1 | 5K | init | None | None |
| 2 | 20K | growth | 0.4 | None |
| 3 | 35K | growth | 0.4 | None |
| 4 | 55K | expansion | 0.6 | None |
| 5 | 80K | expansion | 0.6 | None |
| 6 | 120K | saturation | 1.0 | 10 |
| 7 | 160K | saturation | 1.0 | 10 |

**自动化脚本设计**:

```bash
#!/bin/bash
# test_long_ctx_stages.sh — 通过代理发送渐进式增长的请求
# 断言 metrics.jsonl 中的 lifecycle_stage 字段

PROXY=http://127.0.0.1:4000
LOG=logs/test_long_ctx_stages.jsonl

for chars in 5000 20000 35000 55000 80000 120000 160000; do
    # 构造指定大小的消息（填充 tool_result）
    python3 -c "
import json, sys
chars = $chars
filler = 'x' * (chars // 3)
body = {
    'model': 'claude-sonnet-4-6',
    'max_tokens': 100,
    'stream': False,
    'messages': [
        {'role': 'user', 'content': 'Read this file'},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {'file_path': 'test.py'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': filler}]},
        {'role': 'user', 'content': 'Summarize what you read'}
    ]
}
print(json.dumps(body))
" | curl -s -X POST $PROXY/v1/messages \
        -H "Content-Type: application/json" \
        -H "x-api-key: test" \
        -H "anthropic-version: 2023-06-01" \
        -d @- > /dev/null
    
    echo "Sent request with ~$chars chars"
done

# 验证 metrics 中的 stage
python3 -c "
import json
with open('$LOG') as f:
    for line in f:
        m = json.loads(line)
        stage = m.get('pipeline', {}).get('lifecycle_stage', {})
        if isinstance(stage, dict):
            print(f'  chars={m.get(\"input_chars\",0):>8,} stage={stage.get(\"stage\",\"?\")} clear_zone={stage.get(\"clear_zone_pct\")} trunc_rounds={stage.get(\"truncate_rounds\")}')
"
```

---

### TC-2: 超大请求拦截 — 413 + pre_truncate 验证

**验证目标**: 确认 350K+ 的请求被正确拦截（413 或 pre_truncate），不会导致 500 错误

**数据依据**: §1.2 显示 350K+ 区间 500 错误率 47%

**测试步骤**:

```bash
#!/bin/bash
# test_oversized_reject.sh

PROXY=http://127.0.0.1:4000

# Case A: > 500KB 请求体 → 应返回 413
echo "=== Case A: 600KB 请求 (Content-Length > PROXY_MAX_REQUEST_BYTES) ==="
python3 -c "
import json
filler = 'A' * 600000
body = {'model': 'claude-sonnet-4-6', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': filler}]}
print(json.dumps(body))
" | curl -s -o /dev/null -w "%{http_code}" -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" -H "x-api-key: test" -d @-
echo " (expected: 413)"

# Case B: 400K chars 请求 (Content-Length < 500KB but chars > OOM_SAFE_CHARS)
echo "=== Case B: 400K chars 请求 (触发 pre_truncate) ==="
python3 -c "
import json
filler = 'B' * 400000
body = {'model': 'claude-sonnet-4-6', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': filler}]}
print(json.dumps(body))
" | curl -s -o /dev/null -w "%{http_code}" -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" -H "x-api-key: test" -d @-
echo " (expected: 200 with pre_truncate triggered)"

# Case C: 200K chars 请求 (正常 SATURATION 上限)
echo "=== Case C: 200K chars 请求 (正常处理) ==="
python3 -c "
import json
filler = 'C' * 200000
body = {'model': 'claude-sonnet-4-6', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': filler}]}
print(json.dumps(body))
" | curl -s -o /dev/null -w "%{http_code}" -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" -H "x-api-key: test" -d @-
echo " (expected: 200)"

# 验证 metrics
echo "=== Metrics 验证 ==="
tail -3 logs/proxy_metrics.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    pre = m.get('pipeline', {}).get('pre_truncate', {})
    print(f'  chars={m.get(\"input_chars\",0):>8,} status={m.get(\"status\")} pre_trunc={pre.get(\"triggered\",False)} dropped={pre.get(\"dropped_msgs\",\"-\")}')
"
```

**预期结果**:

| Case | 请求大小 | 预期 HTTP 状态 | 预期 pipeline 行为 |
|------|---------|---------------|-------------------|
| A | 600KB body | **413** | 无 pipeline 处理 |
| B | 400K chars | **200** | `pre_truncate.triggered=true` |
| C | 200K chars | **200** | 正常处理，无 pre_truncate |

---

### TC-3: 循环检测与截断联动

**验证目标**: 确认 EXPANSION 阶段（40-90K）的循环注入率从 26% 下降到 < 10%

**数据依据**: §1.2 显示 EXPANSION 阶段 loop_injected=26%，修复后降至 9%

**测试步骤**:

```bash
#!/bin/bash
# test_loop_with_context.sh — 模拟 Claude Code 在中等上下文下的工具循环

PROXY=http://127.0.0.1:4000

# 构造一个 50K chars 的会话历史 + 连续 4 次相同 Read 调用
python3 -c "
import json

# 构造 50K chars 的上下文（EXPANSION 阶段）
context_msgs = []
filler = 'def example_function():\n    pass\n' * 200  # ~6K chars per file

# 模拟 8 轮 Read + tool_result
for i in range(8):
    context_msgs.append({'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': f't{i}', 'name': 'Read', 'input': {'file_path': f'file_{i}.py'}}
    ]})
    context_msgs.append({'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': f't{i}', 'content': filler}
    ]})

# 添加 3 次连续相同 Read（触发循环检测）
for i in range(3):
    context_msgs.append({'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': f'loop{i}', 'name': 'Read', 'input': {'file_path': 'same_file.py'}}
    ]})
    context_msgs.append({'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': f'loop{i}', 'content': 'file content here'}
    ]})

body = {
    'model': 'claude-sonnet-4-6',
    'max_tokens': 100,
    'stream': False,
    'messages': [{'role': 'user', 'content': 'continue'}] + context_msgs
}
print(json.dumps(body))
" | curl -s -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" \
    -H "x-api-key: test" \
    -H "anthropic-version: 2023-06-01" \
    -d @- > /dev/null

# 验证 metrics 中的循环检测
tail -1 logs/proxy_metrics.jsonl | python3 -c "
import json, sys
m = json.loads(sys.stdin.read())
qf = m.get('quality_flags', [])
loop = m.get('pipeline', {}).get('loop_detect', {})
stage = m.get('pipeline', {}).get('lifecycle_stage', {})
if isinstance(stage, dict):
    stage_name = stage.get('stage', '?')
else:
    stage_name = '?'
print(f'  chars={m.get(\"input_chars\",0):,}')
print(f'  stage={stage_name}')
print(f'  quality_flags={qf}')
print(f'  loop_detect={loop}')
print(f'  PASS: loop_injected={\"loop_injected\" in qf}')
print(f'  PASS: stage in [expansion, saturation]={stage_name in [\"expansion\", \"saturation\"]}')
"
```

**预期结果**:
- `quality_flags` 包含 `loop_injected`（检测到 3 次连续相同 Read）
- `lifecycle_stage.stage` = `expansion` 或 `saturation`
- 请求返回 200（不是 500）

---

### TC-4: Pipeline 防线触发验证（关键！）

**验证目标**: 确认 trunc/compress/clear 在配置启用时**实际触发**并记录到 metrics

**数据依据**: §1.3 显示全量数据中 pipeline 触发率为 0%，这可能是配置未启用或 metrics 未记录

**测试步骤**:

```bash
#!/bin/bash
# test_pipeline_triggers.sh

PROXY=http://127.0.0.1:4000

# Step 1: 确认当前配置
echo "=== Current config ==="
grep "PROXY_CLEAR_ENABLED\|PROXY_CTX_LIMIT_ENABLED\|PROXY_COMPRESS_ENABLED\|PROXY_CTX_TRUNCATE_STRATEGY" configs/active.conf

# Step 2: 构造 SATURATION 阶段的请求（120K chars）
echo "=== Sending 120K chars request ==="
python3 -c "
import json

# 构造大量 tool_result（模拟 Claude Code 的文件读取历史）
msgs = []
for i in range(30):
    msgs.append({'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': f't{i}', 'name': 'Read', 'input': {'file_path': f'file_{i}.py'}}
    ]})
    # 每个 tool_result ~3000 chars
    content = f'# File {i}\\n' + 'code_line = 1\\n' * 300
    msgs.append({'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': f't{i}', 'content': content}
    ]})

body = {
    'model': 'claude-sonnet-4-6',
    'max_tokens': 10,
    'stream': False,
    'messages': [{'role': 'user', 'content': 'summarize'}] + msgs
}
print(json.dumps(body))
" | curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" -H "x-api-key: test" -d @-

# Step 3: 验证 pipeline 是否触发
echo "=== Pipeline metrics ==="
tail -1 logs/proxy_metrics.jsonl | python3 -c "
import json, sys
m = json.loads(sys.stdin.read())
p = m.get('pipeline', {})
trunc = p.get('truncate', {})
comp = p.get('semantic_compress', {})
clear = p.get('tool_clear', {})
stage = p.get('lifecycle_stage', {})
if isinstance(stage, dict):
    stage_name = stage.get('stage', '?')
else:
    stage_name = str(stage)

print(f'  input_chars={m.get(\"input_chars\", 0):,}')
print(f'  stage={stage_name}')
print(f'  truncate: {trunc}')
print(f'  semantic_compress: {comp}')
print(f'  tool_clear: {clear}')

# 断言
if stage_name in ['saturation', 'expansion']:
    print()
    print('  EXPECT: At least one pipeline action should trigger')
    triggered = trunc.get('applied') or comp.get('enabled') or clear.get('applied')
    if triggered:
        print('  ✅ PASS: Pipeline action triggered')
    else:
        print('  ❌ FAIL: No pipeline action triggered despite SATURATION stage')
        print('     → Check PROXY_CLEAR_ENABLED / PROXY_CTX_LIMIT_ENABLED / PROXY_COMPRESS_ENABLED')
"
```

**预期结果**（取决于配置）:
- `stage` = `saturation`
- `truncate.applied = true`（消息被截断，fifo 策略下需 >30 条消息）
- 或 `tool_clear.applied = true`（需要 `PROXY_CLEAR_ENABLED=true`，默认 false）
- 或 `semantic_compress.enabled = true`（内容被压缩，需 >4096 chars 的 tool_result）

**如果全部未触发**: 检查以下配置：
1. `PROXY_CTX_LIMIT_ENABLED` 是否为 `true`
2. `PROXY_CTX_TRUNCATE_STRATEGY` 是否为 `fifo`（需 >30 消息）或 `rounds`（需 >90K chars）
3. `PROXY_CLEAR_ENABLED` 是否为 `true`（默认 false，防止死循环）
4. `PROXY_COMPRESS_ENABLED` 是否为 `true`（默认 true for local）

---

### TC-5: 长会话稳定性（100+ 请求）

**验证目标**: 确认 100+ 请求的长会话不出现 500 错误，上下文被有效控制

**数据依据**: §1.4 session `0b38e096`（109 请求，100% 200，max 199K chars）

**测试步骤**:

```python
#!/usr/bin/env python3
"""test_long_session_stability.py — 模拟 100+ 轮 agentic 会话"""

import json
import time
import urllib.request

PROXY = "http://127.0.0.1:4000"
SESSION_ID = "test-long-stability"

messages = [{"role": "user", "content": "Let's work on a Python project together."}]

stats = {"total": 0, "ok": 0, "err": 0, "max_chars": 0, "stages": []}

for i in range(100):
    # 模拟 assistant 回复 + tool_use
    messages.append({"role": "assistant", "content": [
        {"type": "text", "text": f"Round {i}: Let me read a file and make changes."},
        {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": f"src/module_{i % 10}.py"}}
    ]})
    
    # 模拟 tool_result（文件内容，~2000 chars）
    file_content = f"# Module {i % 10}\n" + "def func():\n    pass\n" * 100
    messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": file_content}
    ]})
    
    # 发送请求
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 50,
        "stream": False,
        "messages": messages,
    }).encode()
    
    req = urllib.request.Request(f"{PROXY}/v1/messages", data=body, headers={
        "Content-Type": "application/json",
        "x-api-key": "test",
        "anthropic-version": "2023-06-01",
        "X-Claude-Code-Session-Id": SESSION_ID,
    })
    
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            stats["ok"] += 1
    except Exception as e:
        stats["err"] += 1
        print(f"  Round {i}: ERROR {e}")
    
    stats["total"] += 1
    
    # 每轮检查 metrics
    # (read from proxy_metrics.jsonl)
    
    if i % 20 == 0:
        body_size = len(body)
        stats["max_chars"] = max(stats["max_chars"], body_size)
        print(f"  Round {i}: body={body_size:,} bytes, ok={stats['ok']}, err={stats['err']}")

print(f"\n=== Results ===")
print(f"  Total: {stats['total']}")
print(f"  OK: {stats['ok']} ({stats['ok']*100//stats['total']}%)")
print(f"  Error: {stats['err']}")
print(f"  Max body: {stats['max_chars']:,} bytes")
print(f"  PASS: 0 errors = {stats['err'] == 0}")
print(f"  PASS: success rate > 95% = {stats['ok']*100/stats['total'] > 95}")
```

**预期结果**:
- 100 轮请求全部成功（0 个 500 错误）
- 请求体大小被控制（代理层截断/压缩生效）
- 成功率 > 95%

---

### TC-6: 后端不可用恢复

**验证目标**: 确认后端宕机时返回 503 + Retry-After，恢复后自动回到 200

**数据依据**: §1.1 修复后 33 个 503 全是 "Connection refused"（后端未运行）

**测试步骤**:

```bash
#!/bin/bash
# test_backend_recovery.sh

PROXY=http://127.0.0.1:4000

# Step 1: 确认后端正常
echo "=== Step 1: Backend healthy ==="
curl -s -o /dev/null -w "%{http_code}" -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" -H "x-api-key: test" \
    -d '{"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
echo " (expected: 200)"

# Step 2: 停止后端（模拟崩溃）
echo "=== Step 2: Stop backend ==="
./manage.sh stop-backend
sleep 2

# Step 3: 发送请求 → 应返回 503
echo "=== Step 3: Request while backend down ==="
RESP=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" -H "x-api-key: test" \
    -d '{"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}')
echo "  HTTP $RESP (expected: 503)"
echo "  Retry-After header:"
curl -s -I -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" -H "x-api-key: test" \
    -d '{"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}' \
    2>/dev/null | grep -i retry-after
echo "  Error type:"
cat /tmp/resp.json | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('error',{}).get('type','?'))"

# Step 4: 重启后端
echo "=== Step 4: Restart backend ==="
./manage.sh start-backend
# 等待就绪
for i in $(seq 1 60); do
    if curl -sf --max-time 2 http://127.0.0.1:8081/v1/models >/dev/null 2>&1; then
        echo "  Backend ready after ${i}s"
        break
    fi
    sleep 1
done

# Step 5: 发送请求 → 应返回 200
echo "=== Step 5: Request after recovery ==="
curl -s -o /dev/null -w "%{http_code}" -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" -H "x-api-key: test" \
    -d '{"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
echo " (expected: 200)"
```

**预期结果**:

| 步骤 | 预期状态码 | 预期 error.type | 预期 Retry-After |
|------|----------|----------------|-----------------|
| 正常 | 200 | — | — |
| 后端宕机 | **503** | `backend_unavailable` | `30` |
| 恢复后 | 200 | — | — |

---

### TC-7: 热重载配置同步验证

**验证目标**: SIGHUP reload 后 `proxy_state` 和 `anthropic_proxy` 的配置同步更新

**数据依据**: Phase 0 重构的 dual-setattr 修复

**测试步骤**:

```python
#!/usr/bin/env python3
"""test_reload_sync.py — 验证热重载后 proxy_state 与 anthropic_proxy 同步"""

import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import anthropic_proxy as proxy
import proxy_state

# 创建临时配置文件
tmpdir = tempfile.mkdtemp()
confpath = os.path.join(tmpdir, "test_reload.conf")

with open(confpath, "w") as f:
    f.write('LLAMA_BASE_URL="http://127.0.0.1:8081/v1"\n')
    f.write('PROXY_BACKEND_TIMEOUT="999"\n')
    f.write('PROXY_CLEAR_ENABLED="false"\n')
    f.write('PROXY_FROZEN_HEAD="42"\n')

# 记录 reload 前的值
before = {
    "timeout_proxy": proxy.PROXY_BACKEND_TIMEOUT,
    "timeout_state": proxy_state.PROXY_BACKEND_TIMEOUT,
    "clear_proxy": proxy.PROXY_CLEAR_ENABLED,
    "clear_state": proxy_state.PROXY_CLEAR_ENABLED,
    "frozen_proxy": proxy.PROXY_FROZEN_HEAD,
    "frozen_state": proxy_state.PROXY_FROZEN_HEAD,
}

print(f"Before reload:")
for k, v in before.items():
    print(f"  {k} = {v}")

# 执行 reload
from unittest.mock import patch
with patch.object(proxy, "RELOAD_CONFIG_PATH", confpath):
    proxy._reload_config()

# 记录 reload 后的值
after = {
    "timeout_proxy": proxy.PROXY_BACKEND_TIMEOUT,
    "timeout_state": proxy_state.PROXY_BACKEND_TIMEOUT,
    "clear_proxy": proxy.PROXY_CLEAR_ENABLED,
    "clear_state": proxy_state.PROXY_CLEAR_ENABLED,
    "frozen_proxy": proxy.PROXY_FROZEN_HEAD,
    "frozen_state": proxy_state.PROXY_FROZEN_HEAD,
}

print(f"\nAfter reload:")
for k, v in after.items():
    print(f"  {k} = {v}")

# 验证同步
print(f"\n=== Sync verification ===")
all_synced = True
for key in ["timeout", "clear", "frozen"]:
    proxy_val = after[f"{key}_proxy"]
    state_val = after[f"{key}_state"]
    synced = proxy_val == state_val
    if not synced:
        all_synced = False
    print(f"  {key}: proxy={proxy_val} state={state_val} {'✅' if synced else '❌'}")

# 验证值确实改变了
changed = before["timeout_proxy"] != after["timeout_proxy"]
print(f"\n  Values changed: {'✅' if changed else '❌'}")
print(f"  All synced: {'✅' if all_synced else '❌'}")

# 清理
os.unlink(confpath)
os.rmdir(tmpdir)
```

---

### TC-8: 内存压力拒绝验证

**验证目标**: 系统内存超过阈值时，新请求被正确拒绝（503 + backend_oom）

**测试步骤**:

```bash
#!/bin/bash
# test_memory_reject.sh

PROXY=http://127.0.0.1:4000

# 方法: 用 stress_test 模拟内存压力，或手动检查 /status 页面的 used_pct
# PROXY_MEMORY_REJECT_THRESHOLD 默认 90% (local)

echo "=== Current memory status ==="
./manage.sh status 2>&1 | grep -i "内存\|memory\|used"

echo ""
echo "=== Memory reject threshold ==="
grep "PROXY_MEMORY_REJECT_THRESHOLD" configs/active.conf || echo "  (using default 90%)"

echo ""
echo "=== Note: This test requires actual memory pressure to trigger ==="
echo "    To simulate: run multiple large-context requests concurrently"
echo "    Or: lower PROXY_MEMORY_REJECT_THRESHOLD to 50% for testing"
echo ""
echo "    Verification:"
echo "    1. Set PROXY_MEMORY_REJECT_THRESHOLD=50 in active.conf"
echo "    2. ./manage.sh reload"
echo "    3. Send a request → should get 503 + backend_oom"
echo "    4. Restore threshold to 90"
```

**半自动化验证**（降低阈值触发）:

```bash
# 临时降低阈值测试
ORIGINAL=$(grep "PROXY_MEMORY_REJECT_THRESHOLD" configs/active.conf || echo "90")
sed -i.bak 's/PROXY_MEMORY_REJECT_THRESHOLD=.*/PROXY_MEMORY_REJECT_THRESHOLD=20/' configs/active.conf 2>/dev/null
echo 'PROXY_MEMORY_REJECT_THRESHOLD=20' >> configs/active.conf
./manage.sh reload

# 发送请求
RESP=$(curl -s -o /tmp/mem_test.json -w "%{http_code}" -X POST $PROXY/v1/messages \
    -H "Content-Type: application/json" -H "x-api-key: test" \
    -d '{"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}')
echo "HTTP $RESP (expected: 503 if used_pct > 20%)"

cat /tmp/mem_test.json | python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
err = d.get('error', {})
print(f'  type={err.get(\"type\")} retryable={err.get(\"retryable\")}')
print(f'  message={err.get(\"message\",\"\")[:80]}')
"

# 恢复
rm -f configs/active.conf.bak
./manage.sh reload
```

---

## 3. 测试执行优先级

| 优先级 | 测试 | 理由 |
|--------|------|------|
| **P0** | TC-4 (Pipeline 触发率) | 日志显示触发率 0%，可能是严重 bug |
| **P0** | TC-2 (超大请求拦截) | 350K+ 是 500 错误高发区，需确认已修复 |
| **P1** | TC-5 (长会话稳定性) | 验证核心使用场景的端到端可靠性 |
| **P1** | TC-1 (生命周期阶段) | 验证压缩/截断的触发基础是否正确 |
| **P2** | TC-3 (循环检测) | 修复后 loop 率已从 26%→9%，需确认可复现 |
| **P2** | TC-6 (后端恢复) | 503 占修复后错误的 100%，需确认恢复机制 |
| **P3** | TC-7 (热重载同步) | 已有单元测试覆盖，e2e 为补充验证 |
| **P3** | TC-8 (内存拒绝) | 需要实际内存压力，难以自动化 |

---

## 4. 关键发现与建议

### 4.1 Pipeline 触发率分析（已修正）

经调查，"触发率 0%" 是 **metric key 名称不匹配导致的测量偏差**。实际 metric key 是 `truncate` / `tool_clear` / `semantic_compress`（不是 `context_truncate` / `tool_clearing`）。

修正后的实际触发率：
| Pipeline 层 | Metric Key | 触发次数 | 比率 | 说明 |
|------------|-----------|---------|------|------|
| L1 pre_truncate | `pre_truncate.triggered` | 92 | 3.1% | chars > 200K 时触发 |
| L2 tool_clear | `tool_clear.applied` | 53 | 1.8% | 多为旧配置记录（当前 `PROXY_CLEAR_ENABLED=false`） |
| L4 semantic_compress | `semantic_compress.enabled` | 18 | 0.6% | 仅成功压缩时记录 |
| L5 truncate | `truncate.applied` | 189 | 6.4% | fifo 策略下需 >30 条消息 |

**结论**：Pipeline 功能正常，触发率低主要因为：
1. 63% 的请求是测试流量（<5K chars），不可能触发任何 L2-L5
2. `PROXY_CLEAR_ENABLED=false` 是刻意设计（防止 rapid-mlx 死循环）
3. `semantic_compress` 仅在成功时记录（无 "运行但无操作" metric）

### 4.2 350K+ 请求仍可达后端

日志显示 107 个 350K+ chars 的请求中有 57 个返回 200（成功到达后端）。这说明：
- `PROXY_MAX_REQUEST_BYTES` (500KB) 只拦截了 body > 500KB 的请求
- 350K chars 的 JSON body 约为 400-450KB，低于 500KB 限制
- `PROXY_OOM_SAFE_CHARS` (200K) 应该触发 pre_truncate，但日志显示该区间 pre_truncate 触发率为 0%

**建议**: 降低 `PROXY_MAX_REQUEST_BYTES` 到 350KB，或确保 `PROXY_OOM_SAFE_CHARS` pre_truncate 确实生效。

### 4.3 503 全是后端未运行

修复后 33 个 503 错误全是 "Connection refused"（后端进程未启动）。这说明：
- 代理的 503 分类正确（`backend_unavailable`）
- 但用户在 后端未启动时仍尝试请求，说明 `./manage.sh start` 的就绪检测可能不够明显

**建议**: 在 Claude Code 连接前增加预检脚本，确认后端已就绪。
