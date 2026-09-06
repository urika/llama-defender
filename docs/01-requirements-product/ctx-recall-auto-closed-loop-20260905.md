# ctx_recall 自闭环（auto-recall）落地设计

> 日期：2026-09-05 · 状态：已评审修订（评审修正见各节标注），落地中 · 来源：swe-eval 方差验证 v4 + 架构决策分析（见 `docs/02-architecture-design/llama-defender-context-engineering-design.md` §14）
> 一句话：把"重复读取检测 → 召回决策 → 召回执行 → 结果注入 → 行为验证"五环收进代理内部闭环，不依赖模型主动调用 ctx_recall、不依赖 client 压缩功能。

## 1. 背景与证据（为什么做）

swe-eval v4 实测（本地 Ornith-1.5-35B × ansible psrp 实例 × 3 次独立会话）：

- 三轮全部 failed（f2p 0/11），全部命中 R-F08 探索循环；dup_max_count = 29/14/51
- ctx_engine 压缩全程生效（共 4 次 epoch），**ctx_recall 召回 0 次**——压缩主动、召回被动，信息净丢失
- epoch 负担与循环严重度同向（run 3 两次 epoch → dup 51、耗时最长 3.3h）

替代路径排除（实测）：模型主动召回 = 0 调用；client 层压缩（CLAUDE_CODE_AUTO_COMPACT_WINDOW=40000）在 headless `-p` 批跑下 prompt 涨到 2.6× 窗口仍零触发（证伪）；提示词引导降级为对照臂。

## 2. 目标与非目标

**目标**：检测探索循环形态时，代理自动召回折叠内容并注入上下文，打破"重复读取"循环；全程可观测、可开关、可证伪。

**非目标**：不承诺提升任务成功率（f2p）——自闭环解决过程病理；若 dup 下降但 f2p 无改善，按止损判据转向模型选型（§8）。

## 3. 落地清单（文件级）

| # | 落点 | 改动 | 说明 |
|---|---|---|---|
| 1 | `pipeline.py` | **新增 stage：`AutoRecallStage`**，插在 stage 12 `RereadDetector` 之后、stage 13 `DateNormalizer` 之前 | 五环的编排点；遵循 PipelineStage 契约（process / should_run / output_metrics），用 `ConditionalStage` 承载总开关。~~原文「loop 检测之后、smart_router 之前」~~ **评审修正**：实际装配顺序 SmartRouter=2.5 早于 ToolLoopDetector=8，该位置不存在；改定此处——消费 stage 8/11 输出、注入尾部赶在 14/17 截断之前、可读 SmartRouter 路由决策 |
| 2 | `session_ledger.py` | 只读消费 `dup_queries`（`(tool, target_hash)` 分组累计 + `last_dup_turn`，台账查询时派生），不动检测逻辑 | **评审修正**：①的「同一目标 Read ≥3 次」是跨轮累计口径，与 `dup_max_count` 同源；`ToolLoopDetector` 是 tail 15 条内连续 run-length，抓不到 A→B→A 交替，仅作兜底信号 |
| 3 | `ctx_recall.py` | 复用 `fts_search` / `recover_full_content`（注意后者签名需 `turn`，从 manifest 索引行带出）；新增 `auto_recall_for_target(session_key, target)` 包装（按文件路径/锚点取回折叠原文，带长度上限） | 召回执行器 |
| 4 | `memory_stores.py` | 只读消费：epoch 折叠 / fifo 截断时已写入的 manifest 索引行（`record_dropped_messages(…, "epoch_collapse"/"fifo", …)`），`MANIFEST.lines(session_key)` 过滤目标路径 | **评审修正**：判定「该目标内容是否已被折叠」直接查 manifest（RequestParser 已有同款读取先例 `pipeline.py:523`），顺带取得 ③ 需要的 anchor 与 turn；不新造索引，不改折叠逻辑（`context_engine.py` 零改动） |
| 5 | `anthropic_proxy.py:1470` 微轮重派（IFC-3） | 参考实现，不改动 | auto_recall 的注入与重派复用同机制 |
| 6 | `proxy_state.py` + `proxy_config.py` + `manage.sh` | 新增配置项（§5）：proxy_state 常量 + `_RELOAD_SPEC`（SIGHUP 热重载）、CONFIG_REGISTRY 注册、manage.sh 默认值 | **评审修正**：按仓库检查清单，新增配置三处同步，缺一热重载/文档口径不齐 |
| 7 | swe-eval 侧（消费方，不改代码） | EXP-2 实验定义 + 指标判读（dup_max_count / f2p / 召回采纳率） | D8 exp 命令族承载 |

**同构先例**：stage 12 `RereadDetector` 已实现「检测重读已丢内容 → 注入 `[System: …]` user 尾消息 + `diagnostics.record_injection` 记账」，只是针对 stage 7 清除路径且注入的是「让模型自调 ctx_recall」的 HARD BLOCK（即实测 0 调用的被动路径）。AutoRecallStage 是它在 epoch/fifo 折叠域的对应物，注入格式与记账方式直接照搬。

## 4. 五环设计

```
① 检测   AutoRecallStage.should_run/process: 台账 session_ledger.dup_queries
         发现 Read 类同一目标 (tool, target) count ≥ AUTO_RECALL_DUP_THRESHOLD(默认3)
         （跨轮累计口径；ToolLoopDetector 的 max_run 仅作兜底信号）
② 决策   查 memory_stores MANIFEST.lines(session_key): 该目标是否已有 dropped
         索引行 (epoch_collapse / fifo)? 是 → 取 anchor+turn 继续;
         否(内容仍在上下文里) → 跳过(防误注入)
③ 执行   ctx_recall.auto_recall_for_target(session_key, 文件路径)
         → manifest 行 anchor+turn → recover_full_content(上限
         AUTO_RECALL_MAX_CHARS, 默认 4000, 分页锚点保留); manifest 未命中
         回落 fts_search(target) 找锚点
④ 注入   以 [System: …] user 尾消息包装 (照搬 RereadDetector 格式), 附加在
         本次请求上下文尾部(append-only, prefix-cache 友好):
         "检测到你准备重复读取 <path>。该文件早前内容(已于 epoch N 折叠)如下,
          请直接使用, 无需重新读取:\n<召回内容>\n[锚点: r:call_xxx, 续读用 锚点@偏移]"
⑤ 验证   注入事件记账: diagnostics.record_injection("auto_recall") + 日志行
         [auto_recall] injected target=... anchor=... chars=... + 代理内存
         per-session 计数; 后续观测该目标 last_dup_turn 是否推进 →
         召回采纳率 = 注入后未再重读的注入/总注入 (swe-eval 侧差分台账)
```

关键不变式：**append-only**（不重写已有消息，与 ctx engine 的前缀缓存纪律一致）；**fail-open**（任一环失败 → 正常转发，不影响主链路，与现有 stage 风格一致）。

**session key 分域注意**：aux 请求的 session_id 带 `::aux-haiku` 后缀（cdf1df5 隔离）。全链路（检测/查 manifest/召回/计数）使用 `ctx.session_id` 原值即可与 manifest/FTS/台账的 key 自然对上；但 `::aux-haiku` 会话本身跳过注入（aux 是代理内部自答，注入即污染）。

## 5. 配置项（proxy_state，SIGHUP 热重载）

| 配置 | 默认 | 说明 |
|---|---|---|
| `PROXY_AUTO_RECALL_ENABLED` | false | 总开关。**默认关**，仅实验臂/灰度开 |
| `PROXY_AUTO_RECALL_DUP_THRESHOLD` | 3 | 触发重复读取阈值 |
| `PROXY_AUTO_RECALL_MAX_CHARS` | 4000 | 单次注入上限（防注入本身挤占窗口） |
| `PROXY_AUTO_RECALL_PER_SESSION` | 5 | 单会话注入次数上限（防注入循环） |

## 6. 观测与指标

- 代理侧：`diagnostics.record_injection("auto_recall")`（复用 R13 注入计量，`X-Proxy-Feedback-Injected` 头/SSE 尾注自动携带）+ 日志行 `[auto_recall] injected target=... anchor=... chars=...` + 内存 per-session 注入计数（限次用）
- swe-eval 侧（已具备）：`L1_convergence.ledger_dup_max_count`、epoch 次数、f2p/verdict；新增建议：召回采纳率进 L2_proxy_health 扩展段（随 metrics schema 下次升版）

## 7. EXP-2 实验设计（swe-eval D8 承载）

- 双臂：`arm_auto_recall`（PROXY_AUTO_RECALL_ENABLED=true）vs `arm_baseline`（false）
- 对照臂（可选第三臂）H-a 提示词臂：AGENT_PROMPT_TEMPLATE 加召回硬约束
- 实例：ansible psrp（v4 基线实例，三次 R-F08 基线已冻结）× repeats=3
- 判读：dup_max_count 组间差异 + 召回调用/注入率 + f2p + wall_time
- 口径冻结：同 v4（salt 会话隔离、同实例、同 target llama-defender-38）

### 7.1 执行手册（实验入口，已就绪 2026-09-05）

**已就绪的物料**：实验定义 `swe-eval/config/experiments/exp-20260905-auto-recall.yaml`；
开关行 `configs/ornith-oq4e.conf` 的 `PROXY_AUTO_RECALL_ENABLED`（默认 false）；
swe-eval 编排层两处缺口已闭合——`interleave=false` 臂主序分块调度（D8 §3.2 声明落地，
代理全局开关臂无法按 run 切换，分块使切换点收敛到臂间一次）与 repeats>1 时逐
(arm, repeat) 自动注入 `SWE_SESSION_SALT`（D7 §7.4 规则落地，防 2026-09-04 同会话
召回空 patch 事故复发）。

1. **前置**：`./manage.sh status` 确认 ornith-oq4e + 35B 独占窗口（无并发会话——gate
   教训：并发致命中塌陷）；确认 conf 里 `PROXY_CTX_ENGINE_ENABLED=true`、
   `PROXY_AUTO_RECALL_ENABLED="false"`（baseline 臂先跑，与 v4 同起点）。
2. **预检**：`cd APP/swe-eval && python3 scripts/preflight.py --target llama-defender-38`
   （exp run 门禁要求 15 分钟新鲜度）。
3. **注册实验**（先干跑看功效与口径冻结）：
   `python3 -m swe_test exp create --file config/experiments/exp-20260905-auto-recall.yaml --dry-run`
   → 去掉 `--dry-run` 正式落 manifest。
4. **跑 baseline 块**（seq 1-3，arm_auto_recall 之前）：
   `python3 -m swe_test exp run --exp-id exp-20260905-auto-recall --background`
   用 `python3 -m swe_test exp status --exp-id ...` 轮询。单 run 30min~3.3h（v4 节奏）。
5. **臂间切换**（自然窗口：seq 3 的 analyze 完成后、seq 4 首个请求前）：
   `configs/ornith-oq4e.conf` 改 `PROXY_AUTO_RECALL_ENABLED="true"` → `./manage.sh reload`。
   时序容忍度：auto_recall 触发需台账 dup≥3（会话深部），个别早期请求落在切换前
   不影响该 run 的注入机会；如错过窗口，停 worker → 修正 → 重跑 `exp run`
   （断点续跑跳过已完成 run）。
6. **批后判读**：
   - dup：`results/metrics/<run_id>.metrics.json` 的 `L1_convergence.ledger_dup_max_count`
     （台账派生，v4 基线 29/14/51），双臂各 3 值对比；
   - 机制证据：exp report §6 预期 pending（D7 mechanism_signals 未挂 auto_recall，
     与 EXP-1 mini 同状态）——权威证据在代理侧：
     `grep '\[auto_recall\] injected' logs/anthropic_proxy.log`（期望 arm_auto_recall
     3 会话共 1~5 条/会话、arm_baseline 0 条）+ `logs/diag/lifecycle_events.jsonl`
     确认两臂 epoch 均触发；
   - 采纳率：注入后该目标 `last_dup_turn` 是否推进（台账 `dup_queries` 差分）；
   - f2p/verdict/wall_time：`exp report` 产出 `report.md`，按 §8 Go/止损判读。
7. **收尾纪律**：conf 改回 `PROXY_AUTO_RECALL_ENABLED="false"` + reload（默认关，
   防实验干预污染日常测量）。

## 8. 验收标准与止损判据

**Go**：arm_auto_recall 的 dup_max_count 相对 baseline 显著下降（方向性即可，n=3 pilot）且无误注入事故（注入后模型行为正常推进）。

**止损**：
1. dup 不降 → 检测/注入设计错误，回炉（不修第二次）
2. dup 降但 f2p 无改善 → 主因是模型能力而非上下文管理，停止上下文工程投入，转向模型选型/混合路由

## 9. 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| 注入时机/内容错误致模型困惑 | 高 | system-reminder 包装 + 显式说明 + 注入日志可审计 |
| 检测误判（正常重读被拦） | 中 | 阈值≥3 + 折叠索引双重确认（②决策环） |
| 注入挤占小窗口 | 中 | MAX_CHARS 上限 + 会话次数上限 |
| 实验干预污染测量 | 中 | 默认关闭，仅实验臂开启 |

## 10. 工作量估算

代理侧（§3 项 1/3/5/6）：约 1~2 天（含单测）；EXP-2 实验设计与执行（swe-eval 侧）：约 0.5 天 + 批跑 2~4 小时（本地 35B 节奏）。

## 11. 后续扩展：墓碑召回（2026-09-06，已落地默认关）

EXP-2 进行中的取证发现（`swe-eval/scripts/analyze_client_tombstone.py` 可一键复现）：
客户端历史改写把旧 tool_result 丢成**悬空调用**（stage 20 配对补洞注入墓碑
`"Tool result was not provided..."`；425 件丢失、保留跨度中位 1 轮、transcript
级实证客户端持有真结果而请求里没有）。而代理写入期压缩**已被动寄存其中 ~40%**
（manifest `r:<call_id>` 锚 + orig/archive 原文，`recover_full_content` 实测可取回）。

据此新增 `PROXY_TOMBSTONE_RECALL_ENABLED`（默认关，与 dup 触发解耦）双机制：

1. **被动注记**（stage 20）：配对补洞后扫墓碑 → manifest `r:<call_id>` 有寄存 →
   墓碑 JSON 追加 `recall_hint` 字段（ctx_recall 取回提示）；无寄存保持裸墓碑
2. **主动代答**（stage 12.5 `_dangling_recall`）：悬空调用（末条 assistant 未决
   豁免）按 call_id 直查 manifest 寄存 → `recover_full_content` 取回 → `[System:
   AUTO-RECALL]` 尾消息回填（预算/限次/同调用去重沿用 auto-recall 护栏）

关键 join 键：悬空调用的 `call_id` 即寄存锚（`r:<call_id>`），比按文件路径匹配
精确——补上了 auto_recall_for_target 路径检索命不中寄存行的缺口。EXP-2 判读
注脚随之更新：dup 循环的信息丢失约四成在代理召回射程内，treatment 无效果时
先查射程外（客户端组装层）份额再谈止损。

**组装级显影（场景测试 `test/unit/test_tombstone_scenario.py`，2026-09-06）**：
stage 19（ToolPairingRepair）会先删除悬空 tool_use（防 400 的既有孤儿清理），
故 stage 20 在主链路上看不到墓碑注入条件——被动注记可达性受限（仅覆盖绕过
stage 19 的路径，保留为防御性），**主动代答（12.5，先于 19、只追加尾消息）是
主路径**——「单测绿≠组装对」的又一实证，场景测试按环节组装报文的价值所在。

### 11.1 L-11/DEF-307 数据面缺陷与 EXP-2 判读措辞（2026-09-06，已修复）

swe-eval 侧场景推演发现并经 s3851294 实证：**epoch 折叠路径原文未寄存**——
`_collapse` 只写 manifest 索引行（且行带折叠时刻轮号，archive 按 (anchor, turn)
精确匹配必 miss），auto-recall 主场景（epoch 折叠后重读）在数据面恒不可达；
EXP-2 唯一成功注入（seq 5，test_psrp.py，采纳 ✓）走的是**写入期压缩**路径。

**EXP-2 报告措辞纪律**：treatment 臂全程运行于「epoch 域召回不可达 + dup 路径
检索命中靠运气」的半残状态——2 resolved 属会话方差不可归因机制，唯一注入是
压缩路径样本。机制设计有效性由该样本（注入→采纳→重读停止）与三层测试背书；
**dup 组间差异不构成 H1 判读依据**。修复（epoch 寄存 + archive 全扫 + 候选
回退，DEF-307）随批后代理重启生效，机制完整版留待下一轮实验验证。

## 12. EXP-3 候选设计：云端上下文管理 A/B（2026-09-06 记入，待前置依赖）

**动机（经济账）**：云端按 token 计费，输入是复利式成本（60 轮 agent 会话后段单请求
输入 50-100K）。写入期压缩对肥 tool_result 压缩率 40-60% → 长会话任务级成本约打
5-7 折；`X-Proxy-Route-Cost` 已计量，节省可直接审计。典型口径（AGENTS §8.5）：
56K token × 20 请求 ≈ ¥1-3/任务 → 压缩后估算 ¥0.5-2。注意订阅制 GLM 路径
（zhipu，边际成本 0）无经济意义——实验只选计费臂（DeepSeek/Kimi）。

**四道关与现状**（当初「云端透传」决策的搁置理由，逐条复核）：

| 关卡 | 现状 |
|---|---|
| ① 前缀缓存兼容 | ✅ ctx_engine append-only + 写入期压缩本就 prefix-friendly；客户端墓碑化才是缓存杀手，本机制反而是修复 |
| ② 疗效未验证 | ⏳ 待 EXP-2 判读走完 §8.2（机制未证明保 f2p 前，不上付费路径） |
| ③ 客户端管理交叠 | ⏳ 云路径上客户端墓碑化已在「压缩」（粗暴全丢）；接管前需验证 `DISABLE_AUTOCOMPACT` 回退链（既有待办） |
| ④ 边际收益 | 视用法：仅 50 轮+ 长会话压得到；短会话与订阅臂不适用 |

**实验设计**（swe-eval D8 承载，与 EXP-2 同纪律）：

- 双臂：`arm_cloud_cm_on`（去除云端 stage 门控 6/7/14/15/17 + 引擎 + 墓碑召回新机制）
  vs `arm_cloud_cm_off`（现状透传）；target 用计费云端臂（llama-defender-cloud-k3 或 DeepSeek）
- 实例：长会话实例（≥50 轮才压得到），同实例 × repeats≥3，salt 逐 (arm,repeat)
- 主指标：**任务成本（¥/任务，route_cost 直接计量，确定性无方差判读难题）**；
  副指标：f2p 不劣化（H2 式护栏）、dup_max_count、缓存命中（云厂商账单口径）
- 判读：成本下降显著且 f2p 不劣 → Go；成本降但 f2f 劣化 → 压缩比降档重试一次；
  成本不降 → 关闭方向（缓存失效抵消了压缩，属机制性失败）

**工程前置清单**（按序，均不大）：

1. EXP-2 判读完成（本批次 + 墓碑召回新代码的下一轮实验）
2. `DISABLE_AUTOCOMPACT` 回退链验证（客户端让位，否则双层压缩互扰）
3. 云端 stage 门控改型：门控条件从「路由==cloud 硬编码」（`pipeline.py` 6/7/14/15/17
   与引擎的 `_route_target` 判断处）改为配置驱动（如 `PROXY_CLOUD_CM_ENABLED`），
   让云端按实验臂开合；auto-recall/墓碑召回的 aux 豁免与 fail-open 语义沿用
4. 成本计量核对：route_cost 对压缩后 token 的计费准确性抽查

**非目标**：不承诺 f2p 提升（成本实验）；订阅臂不适用；短会话不适用。

## 13. DEF-308（L-13）prefix cache 击穿与轨道①修复（2026-09-06）

EXP-2R 值守观测 + 日志取证（s38beef8）：46K tokens/轮 × TTFT p50 215s ≈ 每轮全额
冷 prefill（35B 本地臂墙钟 3-5 倍）。**三因**（详见 DEFECT-LIST DEF-308）：①主因
= 发送视图字节级回写——客户端 SDK 墓碑化改写制造孤儿，配对修复管线用代理墓碑
替换完整 tool 消息（turn4→5 实证：8685 字符消失/墓碑 4→5）；②Metal 压力驱逐
（5274 行 evict 日志）；③aux 13-token 垃圾条目。**架构约束假说被否定**：hybrid
`non_trimmable` 是整条复用（请求 ≥ 条目即命中），shared=45921 的条目匹配 97.7%
仍被弃用是因为视图回写，不是 Mamba 层不能复用。

**轨道①修复（已实现待上线，`PROXY_CTX_VIEW_STABLE_ENABLED` 默认关）**：
- engine absorb 新增改写判别（`_classify_rewrite` + `answered_tids` 增量账本）：
  user 消息的 tool_result 全部指向已应答 tid → 改写副本 skip（无新内容）/
  strip（混有新文本剥离后追加）——canonical 保留已发送完整版，视图只增不缩
- stage 19 `_fix_tool_pairings` 重复 tool_result **keep-first**（去破坏性兜底，
  无旗标始终生效）：首个完整版保留、后续副本摘除，替代原「全摘+注墓碑」
- 验收：`test/unit/test_view_stable.py` 6 场景（VS1 前缀字节稳定 = 改写轮视图
  与前轮视图字节级前缀兼容；VS2 完整版保留；VS3 混合剥离；VS4 新结果不误伤；
  VS5 旗标关回旧轨；VS6 keep-first）+ 全量 1611 绿
- **上线顺序**：EXP-2R 批后 → `tools/probe_prefix_cache.py` 三组受控实验
  （递增/收缩/恒定，直连后端）→ conf 开旗标 + restart → 观测 cache_fetch
  HIT 率与 TTFT（命中率权威口径 = llama-server.log，不依赖 usage 回传）
- 轨道②（cache-mem/gpu-mem 上调 A/B）随同一次重启生效；轨道③（aux 不
  cache_store）为 rapid-mlx 上游项，登记不实施

## 参考

- `docs/02-architecture-design/llama-defender-context-engineering-design.md` §14（召回缺位实测，证据全文）
- swe-eval `docs/domains/D7-evaluation-analysis.md` §7.4（指标面与关联指标）
- swe-eval `docs/domains/D8-experiment-management.md`（实验口径冻结与功效分析）
