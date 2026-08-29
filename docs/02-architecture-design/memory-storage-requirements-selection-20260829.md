# 记忆存储需求与技术选型（IFC + PDC 支撑层）

> **状态**：设计提案（未实施）｜**日期**：2026-08-29
> **定位**：[IFC](information-fidelity-control-design-20260829.md)（信息保真控制·防御面）与 [PDC](progressive-disclosure-context-serving-design-20260829.md)（渐进披露·建设面）的存储支撑层设计——从两份概念设计逐机制推导记忆（上下文存储）需求，并给出开源组件选型。
> **核心转变**：存储从"事后诊断档案"升格为"**运行时记忆系统**"——不再是只写的观测沉淀，而是被门控、拉取、预取、对账等运行时机制**读写驱动**的一等组件。
> **结论先行**：主流地基 R13-R16 已铺好（台账/档案/五流 join 已存在）；真正新增 = 统一寻址命名空间 + 五个小存储 + 冷层内容检索；选型核心件 = Python 自带 sqlite3 + FTS5（**本机已实测可用**），小存储自研（A3 模式），离线分析用 DuckDB，mem0/Letta/Graphiti 仅作概念参考。

---

## 1. 需求分析：六组需求

### A. 身份与寻址（PDC 地基，当前最大缺口）

- **稳定单元 ID 命名空间**：`ctx_recall` 按句柄/轮次/关键词检索、"拉后即弃率"要 join 拉取日志与损失日志——要求每个可丢弃单元（轮次/tool_result/span）有**跨视图稳定、单调分配、绝不复用**的 ID，在 canonical absorb 时一次性赋值。现状：`_msg_hash` 只是指纹；fifo 截断后位置漂移，无稳定寻址方案。
- **manifest 作为独立数据结构**：页表与内容分离（R1"索引永不丢"的存储形态）。每行 ~100 字节量级；即使内容被驱逐也保留索引行（见 C 组"优雅缺页"）。现状：仅 ctx_engine 折叠路径产生索引行，fifo/压缩路径不产生——即 PDC Phase D0 的内容。

### B. 一致性与事务性（append-only 的存储面延伸）

- **absorb = 事务提交点**：一轮的 canonical 吸收应原子更新 canonical + manifest + 台账 + 档案；部分失败至少可检测（单元校验和）。`_persist_delta_locked` 已是增量落盘先例。
- **全量可重建**：所有 Warm 存储必须能从 canonical/档案**幂等重建**——canonical_mismatch 触发全量重建的路径已存在（台账），manifest/pins/探针存储遵循同一重建语义，**不允许任何存储成为孤本真相**。
- **写入即不可变**：内容寻址、只追加不改写。唯一例外：PDC 方案 A 在 absorb 前改写召回 error result（提交点之前，与压缩改写同语义）。

### C. 分层、容量与驱逐

| 层 | 职责（新增后） | 容量/延迟预算 | 现状 |
|---|---|---|---|
| Hot（V_t，进程内构造） | 钉驻留 + manifest 行 + 工作集 + 损失记账出口 | 构造确定性；钉查 ≤1ms | 现有管线 |
| Warm（进程内 + 增量落盘） | 台账、manifest、pins、探针存储、拉取日志 | 每会话 KB~百 KB；查询 p95 ≤100ms（进程内字典） | 台账已有，其余待建 |
| Cold（磁盘档案） | 按 unit ID 取正文、内容级检索 | 单会话文件线性扫描 p95 ≤2s；超预算上 FTS5 索引 | 档案已有但**只有视图导出，无检索** |

- **驱逐必须分层**：先冷后暖；manifest 永不先于内容驱逐；内容已驱逐时 `ctx_recall` 返回结构化"可寻址但缺页"（页表项 not-present 语义），不返回垃圾或 404 混淆。
- **分析级留存**：Phase 2 需 ≥1 周生产数据做效度验证——当前台账内存 TTL 180 分钟 / 64 会话、档案 200MB 上限可能不够研究队列，需为研究队列做离线导出或扩额。

### D. 观测与关联（信用分配的存储约束）

- **五流可 join 是硬约束**：拉后即弃率 = 拉取日志 ⋈ 损失日志，两者必须共享 unit ID 命名空间——A 组 ID 方案因此是前提而非选项；session_key + request_id 关联已有（R16/trace），需补 unit_id 维度。
- **损失日志（loss journal）**：逐 stage、逐单元的丢弃记录（谁丢了什么、多少字符、何时）。`compression.ratio` 是聚合量，不满足按单元对账——这是 IFC §4.2 账本的存储化。
- **探针结果存储**：门控新鲜度（≤2 轮）要求按会话存探针历史（方法、D_ledger、一致性熵、时间戳），每会话 ≤8 条。

### E. 钉生命周期（驻留集的存储语义）

钉集合需要带元数据的存储：来源、体积、**最近被引用轮次**（anti-windup 降级判据）、预算占比；**持久化**（重启后驻留集必须恢复，否则每次重启等于"页表清空"）；上限 ≤5% 上下文由存储层强制，而非仅靠生成端自律。

### F. 安全与隐私（IFC 探针引入的新面）

探针请求把会话派生内容（任务陈述、材料清单）发往云端——**敏感会话（命中 `PROXY_ROUTE_SENSITIVE_PATTERNS`）的探针必须降级 Tier-0 或禁用**。召回结果重新进入上下文，不超出现有档案（本地明文）的暴露面。

## 2. 缺口汇总

新增 = **1 个命名空间算法 + 5 个小存储 + 1 个中型检索能力**，全部可复制 A3 的"按会话分文件 + 增量落盘 + MB 上限 + 最老驱逐"模式：

| 新增件 | 体量估算（每会话） | 形态 |
|---|---|---|
| 单元 ID 分配器 | 无存储（算法） | absorb 时赋值，单调不复用 |
| manifest | ≤200KB（~2000 单元 × 100B） | A3 JSONL 增量 |
| 损失日志 | 随 ILE 事件，KB 级 | 并入 sessions.jsonl `ifc` 段 |
| pins | ≤5% 上下文镜像，KB~十 KB 级 | 带引用轮次元数据，并入台账 |
| 探针存储 | ≤8 条 × 数百 B | 并入台账 |
| 拉取日志 | ≤16 条 × 数百 B | 并入台账 |
| **冷层内容检索** | 档案既有 200MB 内 | SQLite FTS5 只读索引（见 §3） |

需求的量级重心不在"存得多"，而在**"存得可寻址、可对账、可重建"**。

---

## 3. 技术选型

### 3.1 分层约束（选型的前提）

| 层 | 约束 | 依据 |
|---|---|---|
| 代理核心（热路径） | **stdlib only** | AGENTS.md 架构硬原则（`anthropic_proxy.py`/`pipeline.py` 等零第三方依赖） |
| tools/ 离线 | 三方包可选 | 仓库既有惯例（numpy/requests 各脚本自担） |
| 概念参考 | 只取设计不引入依赖 | append-only / 确定性 / 零依赖三原则 |

**没有可直接整体引入的开源组件**——需求过于领域特定（manifest 语义、五流对账、优雅缺页），且核心受 stdlib-only 硬约束；但分层看每层都有明确答案。

### 3.2 本机验证记录（2026-08-29，决定性事实）

```
Python 3.9.6（系统自带）
SQLite 3.51.0
FTS5：AVAILABLE（CREATE VIRTUAL TABLE ... USING fts5 实测成功）
WAL ：AVAILABLE（PRAGMA journal_mode=WAL 实测成功）
```

冷层检索的零依赖路径在本机成立，无需任何安装。

### 3.3 选型矩阵

| 需求 | 候选 | 推荐 | 理由 |
|---|---|---|---|
| 冷层内容检索 | sqlite3+FTS5；[sqlite-vec](https://github.com/asg017/sqlite-vec)；[bm25s](https://github.com/xhluca/bm25s)；Whoosh | **sqlite3 + FTS5（trigram）** | stdlib 零依赖且本机已验证；bm25s 依赖 numpy（核心不可用，tools 可用）；Whoosh 原项目弃维护 |
| 五个小存储 | 事件溯源库、内容寻址库 | **自研薄存储（A3 JSONL 模式）** | 寻址/对账/优雅缺页语义无现成件；hashlib + OrderedDict + json 全 stdlib |
| 五流离线 join | DuckDB（read_json_auto 直接 SQL 查 JSONL） | **DuckDB（仅 tools/ 侧）** | 单二进制 pip 包不碰核心；替代 trace_query 手写解析 |
| BM25 排序（Warm 层） | bm25s / rank-bm25 | **复用仓库自有 BM25**（content_compressor TS-4） | 零依赖原则下已有实现即最优选 |
| 语义/向量检索（远期） | sqlite-vec（FTS5+vec+RRF [混合范式](https://alexgarcia.xyz/blog/2024/sqlite-vec-hybrid-search/index.html)） | **预留可选加载** | C 扩展非 stdlib：`load_extension` 可选增强，缺失时降级 FTS5-only |
| 生成式长期记忆 | mem0 / Letta / Zep·Graphiti / Cognee | **不引入，只取概念** | 见 §3.5 |

### 3.4 三个关键工程判断

**① 双轨存储形态：热路径不上 SQLite。** 热路径维持"进程内 dict + JSONL 增量落盘"（复制 A3：零写路径风险、复用现有轮转/上限/驱逐设施）；SQLite 只作**检索侧只读索引**——档案落盘时同步 upsert 到 per-session FTS5 表，`ctx_recall` 查 SQLite、取正文走档案文件。核心热路径不引入事务/锁语义；多线程（`ThreadingHTTPServer`）下只需每线程独立连接 + WAL 只读并发，纪律简单。

**② FTS5 中文分词是必须提前处理的坑。** 默认 unicode61 分词器把连续汉字串切成单一大 token（无词切分），"上下文"查不到"管理上下文存储"。方案：trigram tokenizer（3 字滑窗、支持子串匹配、CJK 友好）+ 短查询（2 字词如"压缩""截断"）在候选集内 LIKE 兜底。列入 D1 设计任务。

**③ sqlite-vec 以可选增强形态预留。** 远期语义检索（embedding 向量 + RRF 融合）走 `load_extension` 动态加载，扩展缺失时自动降级 FTS5-only——核心零依赖不被破坏，升级路径不断。

### 3.5 记忆框架参考层（问题域相邻但不重合）

[mem0](https://mem0.ai/blog/zep-vs-mem0-which-ai-memory-layer-should-you-choose)（~55k stars、Apache 2.0、生态最大）、[Letta](https://forum.letta.com/t/agent-memory-letta-vs-mem0-vs-zep-vs-cognee/88)（MemGPT 系）、[Zep·Graphiti](https://particula.tech/blog/agent-memory-frameworks-tested-mem0-zep-letta-cognee-2026)（Apache 2.0，自称 LongMemEval 63.8% 对 mem0 49.0%）解决的是**跨会话长期记忆的生成与遗忘**，且全部 LLM-in-loop（记忆由模型抽取/改写）——与本系统三原则直接冲突：append-only 不可改写、确定性抽取（"确定性无 LLM"）、零依赖。不引入，但三个概念值得吸收进设计：

| 框架概念 | 本系统对应 |
|---|---|
| Graphiti 的 **bi-temporal 双时间轴**（事件时间 vs 摄入时间） | unit ID 与台账对账的时序设计（五流 join 的时间语义） |
| Letta 的 **memory blocks** | 信息钉 / 驻留集（IFC §4.3） |
| mem0 的记忆操作日志 | loss journal（逐单元损失记账） |

另：这些框架的[厂商基准互相矛盾](https://www.digitalapplied.com/blog/open-source-agent-memory-mem0-letta-zep-compared)——反过来印证 IFC Phase 2 **自建效度验证**而非引用外部基准的必要性。

---

## 4. 落地映射（D0/D1 直接输入）

一句话选型：**核心 = 进程内 dict + JSONL（A3 模式，热路径）+ SQLite FTS5 只读索引（检索路径，本机已验证）；tools = DuckDB（离线五流 SQL join）；语义升级预留 sqlite-vec 可选加载；mem0/Letta/Graphiti 进参考文献不进依赖树。**

建议存储布局（复用 `logs/diag/` 既有结构）：

```
logs/diag/
├── ledger/<sid>.jsonl      # 台账增量（pins/探针/拉取日志并入此文件扩展段）
├── archive/<sid>.jsonl     # sent_view 档案（正文权威，既有）
├── index/<sid>.db          # SQLite FTS5 只读检索索引（新增，per-session）
└── sessions.jsonl          # per-turn 深度记录（loss journal 并入 ifc 段）
```

---

*相关文档：[information-fidelity-control-design-20260829.md](information-fidelity-control-design-20260829.md)（IFC）、[progressive-disclosure-context-serving-design-20260829.md](progressive-disclosure-context-serving-design-20260829.md)（PDC）、`diagnostics-dataplane-design-20260819.md`（R13-R16 存储地基）、`logging-trajectory-improvement-design-20260820.md`（五流 join 既有设计）。*
