# swe 实例空 patch 根因调查与窗口深度验证（2026-09-03）

> 状态：结论归档｜日期：2026-09-03
> 实例：`internetarchive/openlibrary-d109cc7e`（annotate list seeds 全栈任务）
> 一句话：**空 patch 根因 = fifo 消息窗口（24 条 ≈ 11 轮工作记忆）截断探索积累期**；
> 窗口加深至 80 条后同一 35B 模型产出 4054B 实现 patch。与 ctx_engine、驱逐、
> 代码 diff、模型能力均无关（逐项排除）。

## 1. 问题与时间线

| 时间 | 条件 | 结果 |
|---|---|---|
| 08-27 | 35B oQ4e（旧窗口配置） | patch 5171B（未 resolved） |
| 09-02 | 35B oQ4e + engine off，51 轮慢跑 | patch 2872B（未 resolved） |
| 09-03 全天 | 35B（engine on ×7 次 / off 对照 1 次） | **全部 0B 空 patch** |
| 09-03 17:03 | 35B + **KEEP_MESSAGES 24→80** | **patch 4054B（3×Edit）** |
| 09-03 18:2x | 27B + 窗口 80（+aggressive 压缩） | 0B 空 patch（压缩变量未隔离） |

## 2. 排除链（每项均有日志/代码证据）

| 嫌疑 | 排除证据 |
|---|---|
| ctx_engine 干扰 | engine off 对照同样空 patch；engine on/off 均与产出无关 |
| 代码 diff（9/2→今天） | BASE(8cbea80)→运行代码实质等价（R18/R19 端点 + engine 接线）；truncation/ctx_recall 新逻辑 12:17 才写入未载入 |
| 驱逐死循环慢死 | 9/2 同样分钟级慢轮（194s/轮）却产出；干净后端正常速度仍不产出 |
| 模型单轮能力 | 冒烟 3.4s 正确 edit_file（`a-b→a+b`） |
| 上下文"长度"限制 | 模型 262K 上限；实际瓶颈是消息数（24 条）非 token |
| swe-eval 脚本 | run_agent 8/30 后未变（9/2 与今天同版） |

## 3. 根因机制：消息数窗口 vs 探索积累期

- fifo 按**消息数**（24 条）截断，不感知 token——工具会话每条消息 2K+ tokens，
  24 条 ≈ **11 轮工作记忆**（52K tokens 中 ~40% 还是 SDK 固定税：
  system 28.8K + system-reminder 31.5K）
- 35B 完成该任务需 ~22 轮/44 分钟探索积累才进入写阶段（验证 run 实测：
  前 24 动作全只读，尾段 Grep→Read→Edit×3）
- 24 条窗口在积累完成前反复抹掉早期发现 → 模型永远"重新探索"→ 永不产出
- 9/2 的成功 = 24 条窗口下的长尾运气（51 轮中某轮恰好完成积累）

## 4. 窗口深度验证（判定实验）

`PROXY_CTX_KEEP_MESSAGES 24→80`（25 轮 × 3 条/轮 ≈ 75 条 < 80 → 全程无截断、
全历史可见）：

- 台账：14 Read + 10 Bash + 2 Grep + **3 Edit**（Edit 集中在尾段）
- patch 4054B：`lists/model.py` 加 `SeedNoteDict` TypedDict +
  `seed_key_to_seed_type()`——任务正确实现代码（非幻觉总结）
- 结论：**窗口深度是产出与否的决定变量**；35B 能力完好

**生产建议**：KEEP_MESSAGES=80 保留（已验证）。代价：每轮 prefill 增大
（~52K 峰值接近驱逐临界——长会话需观察，必要时 gpu-mem 上探配合）。

## 5. 附带发现

### 5.1 27B 对照（未隔离压缩变量）
27B（窗口 80 + 其 conf aggressive 压缩——text/模板压 78%）25 轮空 patch。
压缩变量使结果无法归因于模型；若需干净 27B 对照须关压缩重跑。

### 5.2 ctx_recall 四次零触发（场景分析）
s3864951 / 35B 各 run / 27B run：工具注入 ✓、manifest+orig 可查 ✓（27B 199 行）、
压缩标记提示 ✓——模型**从不发起调用**，面对压缩文件的应对是 Bash 重读。
解释：文件是**可再生内容**（路径在手重读零成本且确定）——ctx_recall 对可再生
内容无吸引力；其价值场景 = 折叠收编的不可再生轮次（engine on 长会话），
该场景在这些 run 中未出现。召回链真实验证仍需 engine on 折叠会话。

### 5.3 PDC 片段级寄存补丁（engine 写入期压缩）
`context_engine._transform_message` 补寄存（对齐 truncation 协议）：engine 压缩
丢弃原文 → orig + manifest（anchor=r:tool_use_id），ctx_recall 可兑现。
truncation 路径本有寄存（27B orig 199 行实证）；engine 路径曾是漏网。
2 新单测 + 全量 1543 OK。engine on 实际运行验证待真实会话。

### 5.4 驱逐死循环（性能次级因素）
长上下文（52K+）+ entries 32 大 KV 超 metal_cap 28.1GB → prefix-pressure-evict
循环（尾部 30% 日志 517 次）→ 轮次 4-14 分钟。35B 验证 run 在干净后端
（重启后）未触发。与空 patch 无因果（9/2 慢而产出），但影响实验吞吐。

## 6. 配置变更清单（本次实验后状态）

| 项 | 值 | 说明 |
|---|---|---|
| ornith-oq4e.conf engine | true（恢复） | 生产原配置 |
| ornith-oq4e.conf KEEP_MESSAGES | 80（保留） | 窗口深度验证结论 |
| qwen3.8-27b-4bit.conf KEEP_MESSAGES | 80 | 同验证条件 |
| qwen3.8-27b-4bit.conf LLAMA_MODEL | 完整 id（修复） | 短名不可解析 |
| tools/gate_test_ctx_engine.py | +X-Proxy-Route-To: local | 本地验证防路由切云 |
| swe-eval targets.yaml | +llama-defender-27b | 27B 对照 target |

## 7. 关联

- `rapidmlx-kvcache-mechanism-20260903.md`：KV/驱逐机制（§8.5 gate 验证）
- `gate_test_ctx_engine.py`（tools/）：ctx_engine 门禁（force-local 版）
- swe-eval results/runs.jsonl：本调查的 run 记录（verify-window80 / verify-27b）
