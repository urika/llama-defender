# 上下文场景分档设计：本地模型的长度约定与性能边界

> **状态**：方案设计（interactive/coding 两档已在生产，batch-256K 档待验证）｜**日期**：2026-09-01
> **数据来源**：exp-2-v2 批跑 318 轮 archive×sessions 对齐实测（Ornith-1.5-35B-A3B-oQ4e, rapid-mlx, 48GB M 系列）
> **关联**：[PDC 设计](progressive-disclosure-context-serving-design-20260829.md)（折叠/召回协议）· [IFC](information-fidelity-control-design-20260829.md)（丢失度量）· [agent-output-budget](agent-output-budget-design-20260827.md)（输出侧预算）
> **一句话**：上下文长度不是单一数字而是场景档位——成本的决定变量是**前缀稳定性**（增量 prefill vs 全量冷算），而非总长；按场景约定「增量预算 + 冷启动容忍度」，并用配置档落地。

---

## 1. 需求场景定义

| 场景 | 交互模式 | 上下文目标 | 延迟容忍 | 典型任务 |
|---|---|---|---|---|
| **interactive** | 一问一答，等待响应 | ~20K tokens | **冷启动 <60s，暖轮 <10s** | 问答、小编辑、状态查询 |
| **coding**（现行生产） | agentic 多轮，长任务 | ~114K tokens（400K chars 上限） | 暖轮 10-30s，冷轮可到 ~100s | Claude Code / agent_go 日常 |
| **batch-256K**（本档新增） | 非交互，无人等待 | **~256K tokens（≈900K chars）** | **冷启动 8-10min 可容忍；暖轮 10-30s** | 长文档处理、整仓分析、夜间批处理、非交互文本/编程任务 |

核心诉求（2026-09-01 需求输入）：非交互场景支持 **256K 左右上下文、容忍较长时间**；交互场景维持 10s 级响应。

---

## 2. 实测性能数据（设计的经验基础）

### 2.1 TTFT vs 上下文规模（318 轮，冷前缀<30% 命中）

| 上下文（tokens） | 冷 TTFT p50 | 样本 |
|---|---|---|
| ~25K | 52s | 17 |
| ~35K | 85s | 64 |
| ~40K | 98s | 108（最大桶） |
| ~45K | 102s | 46 |
| ~55-60K | 92-97s | 32 |
| 30K / 50K 桶中的缓存命中轮 | **10-17s** | 混在桶内 |

**经验规律**：
- 冷 prefill 吞吐 ≈ **400-650 tok/s**（35B-A3B MoE，gpu-mem-util 0.70）
- 前缀缓存命中的轮次 TTFT 与**总长无关**，只与**本轮增量**（2-5K tokens）相关 → 50K tokens @ 10-17s 实测存在
- TTFT 双峰分布（批跑数据 85-102s 峰 / 10-17s 谷）的成因是 **fifo 头截断是否发生过**：头部丢消息 → 前缀整体作废 → 全量冷算

### 2.2 实际工作负载（exp-2-v2 批跑 archive）

- 发送 payload：p50=149K chars（≈43K tok），p90=174K（≈50K），max=222K（≈63K）
- 物理窗口利用率为 262K 原生能力的 **1/4**——余量被服务策略（延迟/OOM/截断）消耗，而非模型能力

### 2.3 当前配置链条与错位

| 层 | 数值 | 说明 |
|---|---|---|
| 模型原生 | 262,144 tokens | `max_position_embeddings` |
| catalog 声明 | 131,072 tokens | models.json `local-default`（保守，且 9B 误标同值） |
| 代理放行 | ≈114K tokens | `PROXY_CTX_CHARS_LIMIT=400K` |
| 客户端假设 | 200K | Claude Code 按 claude-sonnet 规格自行 compact |

**两处协议错位**（已排期修复）：catalog 与 env 双源漂移（待做：chars_limit 从 catalog 派生）；R10 `/v1/models` 未透传 `context_tokens`（管道现成，补字段即通）。

---

## 2.4 等效可访问容量（压缩+召回后的『上下文大小』，2026-09-01 补）

**三个必须区分的数**：

| 概念 | 数值 | 决定因素 |
|---|---|---|
| 物理窗口 | 262K tokens | 模型原生 `max_position_embeddings` |
| 有效工作集（驻留） | **40–60K tokens**（p50≈43K 实测） | fifo keep=24 滚动窗口 |
| 可寻址语料 | **无硬上限**（磁盘约束） | manifest 锚点 + archive 常驻 + 分页遍历 |

**分档等效容量预估**：

| 档位 | 物理窗口 | 压缩系数（实测） | 等效可访问容量 |
|---|---|---|---|
| interactive（9B） | ≈20K tok | ×0.15–0.4 | **≈40K tok**（窗口+折叠召回），暖轮 <10s |
| coding（现行） | ≈114K tok | ×0.15–0.4 | **≈150–200K tok**（43K 驻留 + 压缩省出空间 + 折叠可召回） |
| batch-256K | ≈256K tok | 禁头截断 | **≈256K tok 全量驻留 + archive 无限延伸** |

乘数逻辑：TS-4 实测 13769→2030 chars（×0.15）——同窗口装 6 倍信息骨架；被折叠的 57% 单元经锚点+orig/archive 恢复，语义上未丢，只是从『驻留』降级『可寻址』。

**三个比率才是真实约束**（容量本身已不是瓶颈）：

1. **召回命中率**（供给端）——修复前 0%（31 次全 miss：整句短语结构性 miss + 自引用洪泛），三级穿透修复后待 A/B v2 实测；
2. **拉取意愿率**（需求端）——指引后 0→31 次/任务已验证；「想到要查」的覆盖率待测（欠拉残余）；
3. **拉取后驻留时长**——召回结果仍会被后续折叠（churn），信息钉（驻留集）机制是下一个杠杆。

**诚实边界**：『可访问』≠『时刻记得』。实践上限由模型愿意花多少轮次拉取决定：约 20–50 次/任务 × 4K chars ≈ 100–200K tokens 的按需补给，叠加 40–60K 窗口驻留。

**结论**：coding 档等效可访问 ≈200K tokens（已超原生窗口的实用部分）；batch-256K 档等效可访问 = 会话累计全量（理论上无上限）。前提链条：命中率（A/B v2 验证中）→ dup 守卫（已实现 18019fb）→ 前缀稳定（禁头截断，已设计 §4.3）。

---

## 3. 影响因素（决定档位边界的物理量）

| # | 因素 | 机制 | 实测/推算 |
|---|---|---|---|
| F1 | **冷 prefill 吞吐** | TTFT ≈ tokens ÷ 吞吐 | 400-650 tok/s @ 35B-A3B |
| F2 | **前缀稳定性** | 头截断/改写 → 全前缀 KV 作废 | 85-102s 峰的唯一来源；死亡螺旋机理 |
| F3 | **KV cache 显存** | 线性于上下文长度；hybrid GDN 前缀不可修剪（rapid-mlx 门控） | 43K tok 在 8GB 稳定；256K 外推 12-16GB（4bit，**需实测校准**） |
| F4 | **统一内存预算** | 权重 20.1GB + KV + 激活 + macOS/Metal 保留 ≤ 48GB | 256K 档合计 40-44GB，紧但可行 |
| F5 | **gpu-mem-util 软限制** | 实际使用可超配额 20-40%（AGENTS.md §8.1） | 上探 cache-memory-mb 必须 `manage.sh monitor` 盯 Metal 峰值 |
| F6 | **请求体积上限** | `PROXY_MAX_REQUEST_BYTES=500KB` 会先于上下文上限卡住 | 256K tok ≈ 900KB 文本 → local 档需提至 1.2MB |
| F7 | **客户端期望错位** | Claude Code 按 200K 规格管理自身上下文 | 代理侧提前折叠的"静默失忆"根源之一（PDC 协议补齐中） |

**核心结论：F2（前缀稳定性）的权重高于 F1×F3 之和。** 256K 档的可行性完全建立在「会话内前缀永不破碎」之上——碎一次 = 重付 8-10 分钟。


---

## 3.5 KV cache 命中率与代理层管线的一等约束（2026-09-01 补）

rapid-mlx 前缀缓存对 hybrid GDN 架构 non-trimmable——前缀必须精确匹配。因此 TTFT 的两峰（暖轮 10–17s / 冷轮 85–102s）由一个变量决定：**本轮请求的前缀是否与缓存精确延续**。管理命中率 = 管理管线对前缀的破坏。

### 3.5.1 24 个管线 stage 的 KV 影响分类

| 类别 | 语义 | Stage |
|---|---|---|
| 纯读/元数据 | 零影响 | RequestParser、LifecycleClassifier、DynamicMaxTokens*、SmartRouter、RouteNotification、ErrorTranslator、ToolLoopDetector、TextLoopDetector、SessionLoopState、MessageHashDebug、PrefixRatioComputer、FormatConverter、BackendDispatcher |
| 尾部追加 | 缓存安全 | BlockerDetector、RereadDetector(HARD BLOCK)、HighDropRatioNotice、LoopIntervention |
| 头部冻结区 | 一次性稳定 | SystemNormalizer、CacheAligner |
| 就地改写(一次性) | 改后自愈 | DateNormalizer(同日期改出同值)、ToolPairingRepair(只修补破损对) |
| 就地改写(持续性) | **每轮破坏** | ContentCompressor(TS-4 压缩)——一次性 per 单元，但会持续产生新压缩 |
| 头截断 | **每轮破坏** | ContextTruncator(fifo 滑动)、OOMSafetyFIFO(紧急) |

*DynamicMaxTokens 改 body 的 max_tokens，不在 messages 前缀内。

**关键区分**：「一次性破坏」（改一次后前缀重新稳定）与「持续破坏」（每轮滑动每轮失配）差一个数量级——fifo 头截断是唯一的持续破坏者。

### 3.5.2 与既有基础设施的接线（四点，全部小改动）

1. **PrefixRatioComputer 升级为一等命中观测**：stage 21 已每请求计算公共前缀占比（日志可见），补两件事——写入 sessions.jsonl 结构化字段 + 修复后端 timings 管道（生产 hit_ratio 当前 100% null，R13 探测已就绪）。得到「前缀代理侧预估 + 引擎侧实测」双视图。
2. **CacheAligner 扩展为前缀契约执行者**：任何 stage 想改 system/tools 区必须经它审计；变化 = 一次显式「前缀失效事件」（计数+告警），不再静默。
3. **fifo 截断 = 显式前缀失效事件**：截断发生时递增 session 级「前缀破碎计数」并暴露 /api/status——让 85–102s 冷峰在指标上可见。batch 档 keep=512 使该计数恒 0。
4. **stage docstring 声明 KV 语义**：`kv: safe | append | one-shot | breaking` 四值标签——防止未来新 stage 无意破坏前缀。

### 3.5.3 与压缩/召回的衔接

- **可恢复压缩**（指针化）= 一次性破坏：每单元只指针化一次，其后前缀重新稳定——比 fifo 每轮滑动温和一个量级
- **折叠占位符/实体行/召回 follow-up** = 尾部追加或稳定文案 = 缓存安全
- **ctx_engine（append-only canonical）** = 这些纪律的终极形态：把追加变成数据结构性质，管线想破坏都破坏不了
  - **9B 机制验证已通过（2026-09-01，gate_test 80 轮，logs/gate-ctx-engine-9b.json）**：
    G1 非 epoch P90=1.36s（门禁 15s）✓；G2 epoch 重置轮 P90=1.47s（门禁 60s，9B 上 4 次重置均 <2s）✓；
    G3 零断连 ✓；G4 三次 epoch 后首轮 hit=0.916/0.917/0.927（门禁 >0.8）✓；
    非 epoch 命中率 p50=0.948——append-only 的『每轮只付增量』得到机制级实证
  - 35B 数字确认（G2 的 60s 门禁按 35B prefill 速率）待 A/B 完成后的窗口
  - 备注：S=60000 下 epoch 实际触发于 prompt≈32K——触发算式（est 口径）与 prompt tokens 的对应需单独核对
- **会话亲和**（PROXY_ROUTE_STICKY，已有）：同会话同引擎 → KV 池不跨引擎

### 3.5.4 命中率之外的 TTFT 手段

| 手段 | 适用 | 说明 |
|---|---|---|
| 9B interactive 档 | 小上下文高频交互 | prefill 快 3–5×，20K@<10s（影子实例已验证） |
| 分页拉取代替全文驻留 | 大文件 | `@offset` 每页 4K prefill，不驻留 37KB 全文 |
| epoch 折叠一次性重置 | 长会话必超窗 | 把「每轮滑动失配」变「低频一次性重置」（ctx_engine） |

**一句话**：命中率不靠缓存调优，靠上下文的「只追加、不回头」纪律——fifo 头截断是命中率的头号敌人，append-only（或足够大的窗口让它永不触发）是 TTFT 的根本解。

---

## 4. 三档配置方案

统一按仓库既有 conf 档位模式（`configs/*.conf` + `manage.sh switch`），并预留 `PROXY_CTX_PROFILE` 单变量映射（复用 `PROXY_COMPRESSION_PROFILE` 先例，Phase 2）。

### 4.1 interactive 档（待建，`ornith-oq4e-interactive.conf`）

```bash
PROXY_CTX_CHARS_LIMIT="70000"          # ≈20K tokens
PROXY_CTX_KEEP_MESSAGES="64"
PROXY_OOM_SAFE_CHARS="280000"
# 9B 引擎可选: prefill 更快, 20K@冷启动 ~30s 内
```
SLA：暖轮 <10s；冷轮 <60s。

### 4.2 coding 档（现行生产 `ornith-oq4e.conf`，不变）

```bash
PROXY_CTX_CHARS_LIMIT="400000"         # ≈114K tokens
PROXY_CTX_KEEP_MESSAGES="24"
PROXY_OOM_SAFE_CHARS="200000"
```
实测：暖轮 10-17s；头截断后冷轮 85-102s。L1-L3 召回指引 + PDC 微轮重派在此档补偿折叠损失。

### 4.3 batch-256K 档（本档新增，`ornith-oq4e-256k.conf`）

```bash
# ---- 上下文: 256K tokens ≈ 900K chars ----
PROXY_CTX_CHARS_LIMIT="900000"
PROXY_CTX_LIMIT_ENABLED="true"
PROXY_CTX_TRUNCATE_STRATEGY="fifo"
PROXY_CTX_KEEP_MESSAGES="512"          # 事实禁头截断(200 轮会话≈300 条, 永不触发)
PROXY_OOM_SAFE_CHARS="3200000"         # 关预截断
PROXY_MAX_REQUEST_BYTES="1200000"      # 900KB 文本 + 结构开销

# ---- 后端: KV 空间翻倍(从 8192 起步, 验证后上探) ----
RAPID_MLX_EXTRA_ARGS="--no-mllm --gpu-memory-utilization 0.80 \
  --cache-memory-mb 12800 --max-num-seqs 1 \
  --default-repetition-penalty 1.0 --hybrid-cache-entries 8 ..."

# ---- 超时: 覆盖 256K 冷启动 ----
PROXY_BACKEND_TIMEOUT="1200"           # 20min > 10min 冷 prefill + 生成
```

**设计要点**：
1. **keep=512 = 事实禁头截断**。会话内前缀永不碎 → 首轮 8-10min 后每轮只付增量（10-30s）。超 900K chars 时由 PDC 路径接管：ctx_recall 折叠 + 召回（L1 指引已就位），而非无感丢弃
2. **KV 从 12800MB 起步**（≈196K tok），实测校准 bytes/token 后再上探 16384。校准方法见 §6
3. **首轮 10 分钟的预期管理**：SSE 心跳已保活（#51-B1）；agent_go 侧 backend timeout 需 ≥1200s
4. **护栏**：`manage.sh monitor` 盯 Metal 峰值；memory_pressure 拒绝路径保持开启；优雅停止（§8.6 Metal 死锁纪律）

### 4.4 档位 ↔ 约定接口（模型与客户端的可感知性）

- **模型侧**：折叠占位符带 ctx_recall 指引（已落地 258d414）；压缩头标记 `[ctx:...]`（待做）；接近限额的分级 system-reminder（待做，HighDropRatioNotice 先例）
- **客户端侧**：R10 `/v1/models` 透传各档 `context_tokens`（管道现成）；agent_go 按档位调整自身 compact 策略与超时
- **运维侧**：`manage.sh switch <档位> && reload`；`/api/status` `ctx_config` 段已机读当前档参数

---

## 5. 已落地 vs 待做

| 项 | 状态 | 位置 |
|---|---|---|
| coding 档（400K chars + fifo keep24 + L1-L3 召回指引） | ✅ 生产 | `ornith-oq4e.conf` + 258d414 |
| batch-256K 档 conf | ⏳ 本文档 §4.3，待建文件 + 实测校准 | 新 `ornith-oq4e-256k.conf` |
| interactive 档 conf | ⏳ §4.1，需求确认后建 | 新 conf |
| KV bytes/token 实测校准 | ⏳ §6 验证路径第一步 | — |
| catalog 单一真相源（chars_limit 派生） | ⏳ ~30 行 | `proxy_config.py` + `models.json` |
| R10 透传 context_tokens | ⏳ ~10 行 | `anthropic_proxy.py` R10 段 |
| hit_ratio 可观测（后端 timings 管道修复） | ⏳ 半天（§3.5.2-1） | `diagnostics.probe_timings` 已就绪 |
| PrefixRatioComputer 值入结构化指标 + 破碎计数 | ⏳ ~40 行（§3.5.2-2/3） | `pipeline.py` stage 21 + truncation |
| stage KV 语义标签（safe/append/one-shot/breaking） | ⏳ 注释级 | `pipeline.py` 24 个 stage docstring |
| catalog 修正 262K（35B/9B 原生值） | ⏳ 数据修正 | `models.json` |
| 压缩头标记 `[ctx:...]` 协议 | ⏳ ~40 行 | `content_compressor.py` |
| 接近限额分级提示 | ⏳ 复用 HighDropRatioNotice 模式 | `pipeline.py` Stage 15 |
| `PROXY_CTX_PROFILE` 单变量映射 | Phase 2，三档稳定后 | `proxy_config.py` |

---

## 6. 验证路径（batch-256K 放行门禁）

1. **KV 校准**：196K tokens 真实会话（长文档任务）单跑；`manage.sh monitor` 采样 Metal 峰值；反推 bytes/token，确认 16384MB 是否安全
2. **prefill 曲线补点**：128K / 196K / 256K 各一次冷启动计时，校准吞吐回归线（现仅 25-63K 区间有数据，外推到 256K 有 ±30% 不确定度）
3. **前缀保温验证**：连续 20 轮增量会话，TTFT 应稳定在 10-30s（无 85s+ 峰）；任何一次头截断都会在曲线上一眼可见
4. **OOM 护栏演练**：双请求并发 + memory_pressure 注入，确认拒绝路径与优雅停止
5. **放行标准**：§6.1-6.4 全绿后 `switch` 上线；interactive 档同步用 20K 真实会话验收暖轮 <10s

---

## 7. 风险与边界

1. **KV 外推误差**：12-16GB 是线性外推，MoE 稀疏激活与 hybrid GDN 的 KV 构成可能非线性——§6.1 校准前不得直接上 256K
2. **10 分钟首轮的心理成本**：非交互场景可容忍，但客户端超时链（curl/SDK/agent_go）任一环 <1200s 都会断——上线前逐环核对
3. **前缀破碎回退**：若会话中途必须折叠（超 900K chars），一次破碎 = 8-10min 重算——PDC 折叠协议（锚点+召回）是把这次破碎变成"只折叠尾部、前缀仍稳"的关键（折叠追加在尾部，符合 append-only）
4. **与 9B 双引擎共存**：256K 档 KV 上探时注意与 9B 引擎（8084）的内存竞争——双引擎并发时 35B 档位需降配，`tools/engines.sh` 侧注明
5. **conservative 回退**：任何 Metal 异常按 §8.6 纪律 `stop-backend` 优雅停止，禁 kill -9

---

*实测数据与推演过程：exp-2-v2 批跑日志（`swe-eval/results/exp2-v2-resume.log`）、`logs/diag/archive/`（318 轮对齐样本）、`logs/diag/sessions.jsonl`。欠拉修复与召回协议见 commit 258d414 及 [PDC 设计 §2.2](progressive-disclosure-context-serving-design-20260829.md)。*
