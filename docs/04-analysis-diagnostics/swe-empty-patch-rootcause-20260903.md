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

## 8. 追踪：engine-on 致死根因与透传基线（2026-09-04/05 补充）

### 8.1 engine-on "1 轮 end_turn" 真相：aux/主 session key 污染

复现实锤（09-04 22:21 run）：主任务请求（32K/40 tools）模型仅回 22 chars 即
end_turn。链路：
- claude SDK 内部 aux 调用（title 生成，haiku tier + tools=0）与主对话**共用
  X-Claude-Code-Session-Id**（ANTHROPIC_CUSTOM_HEADERS 对 SDK 全部请求统一
  注入，harness 无法区分——run_agent.py:405 单 sid 设计）
- 代理按 sid[:8] 归会话 → aux 历史吸进主任务 canonical → `prefix mismatch —
  canonical rebuilt` → 主对话形态错乱 → 模型 1 轮 end_turn
- 09-03 的 400 死循环是同根源另一形态（aux 当日直连官方 API，被孤儿历史卡）
- **9/3 engine-on 7 连败全部由此致**——engine-on 的 run 从未真正跑长过

**修复（pipeline.py ContextEngineStage.should_run）**：haiku tier + tools=0
请求的会话 key 追加 `::aux-haiku` 域后缀，aux 与主 canonical 互不可见。
单测覆盖（aux 分域 + 主 canonical 不被污染），1544 tests OK。

### 8.2 SDK 轮间历史改写（性能噪音，非致死）

engine-on 修复后复现轮：archive 逐轮 diff 发现 claude SDK headless 每轮把
**上一轮工具结果替换为占位符** `{"error": "Tool result was not provided..."}`——
工具结果单轮生命周期。后果：engine 每轮必然 prefix mismatch → canonical
rebuild → 前缀缓存命中反复重建（性能损失，正确性无损——canonical 跟随客户端
历史）。透传组（8.3）该现象未出现，疑似与 resume/引擎形态叠加相关，待后续
自然会话再证。

### 8.3 纯透传基线 + 轮数预算：0B 的另一半真相

对照组（engine off + 压缩/过滤/清除/HBE 全关 + KEEP 80 + 干净
SWE_SESSION_SALT）：

| run | max_turns | 结果 |
|---|---|---|
| passthrough3 | 25 | 0B——25 轮全在探索（13R+12B），max_turns 截停 |
| **passthrough-100t** | 100 | **5822B patch ✓**——51 轮自然收敛（end_turn） |

- 51 轮构成：探索 ~30（Read/Bash/Grep）→ Edit×13 → Bash 验证 → end_turn
- 速度 ~15s/轮（后端 fetch 143 HIT/0 MISS、命中 98.5%——增量 prefill 仅
  几百 tokens；decode 20-25 tok/s）
- **max_turns=25 对全栈任务偏紧**（转折点在 20-50 轮间波动）——9/3 的
  4054B 与本次 5822B 都是 ~50 轮预算下的产出
- 行为方差教训：旧会话 key 复用（历史包袱）曾诱发 23 连败路径循环 +
  BLOCKER——同任务重跑必须换 SWE_SESSION_SALT（run_agent docstring 已记）

### 8.4 结论修订（对 §3 根因的补充）

fifo 窗口浅（24 条）仍是重要约束（§3 成立），但同实例完整判定需要三条件
同时满足：**窗口够深（80）+ 轮数预算够（≥50）+ aux 隔离（engine on 时）**。
三者缺一即 0B。纯透传 + 80 窗口 + 100 轮预算是最稳健的当前基线
（5822B，27 分钟，$0.15）。
