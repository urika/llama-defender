# RAG 组件与 KV 存储方案调研 · 匹配度评估

> **状态**：调研结论（选型文档 §3.4③ 已据此修订）｜**日期**：2026-09-01
> **来源**：对当前（2025-2026）RAG 实现组件（向量存储/编排框架/记忆框架）与 KV 存储方案的盘点，对照本系统六大纪律（stdlib-only 核心、单进程、零新增端口、append-only、客户端零改动、fail-open）与四大空白点（`cognitive-gap-closure-design-20260901.md`）逐项评估匹配度。
> **一句话**：**存储引擎不是本系统的瓶颈，语义生产才是。** 「JSONL + SQLite FTS5 trigram」在当前规模（64 会话 / MB 级文件）下仍是最优解，绝大多数 RAG 组件与 KV 方案不匹配核心纪律；唯一需修正的是选型文档对 sqlite-vec 的单点预留（上游已实质停滞，改为三级降级链）；语义召回缺口（SEM 动机）的真实瓶颈是 **embedding 生产方式**，不是向量库选型。

---

## 1. 被评估对象：系统现状锚点

| 层 | 现状 | 规模 |
|---|---|---|
| 热路径存储 | 进程内 dict + JSONL 增量落盘（manifest/ledger/archive/semantic） | 单会话 ≤2000 行（`memory_stores.py::MAX_LINES_PER_SESSION`），全库 ≤100-200MB |
| 检索 | SQLite FTS5 trigram（`ctx_recall.py` 三级穿透）+ 内存子串 L1 | 本机 SQLite 3.51.0 已实测可用（选型文档） |
| 幂等/缓存 | 进程内 LRU+TTL（`idempotency.py`） | — |
| 向量 | **无**（选型文档原 :106 预留 sqlite-vec 可选加载） | — |
| 纪律 | stdlib-only / 单进程 / 零新端口 / append-only / fail-open | 不可协商（`context-architecture-evolution-20260829.md` 不变式） |

## 2. 组件盘点与匹配度

### 2.1 向量检索（RAG 存储侧）

| 组件 | 形态 | 匹配度 | 关键事实 |
|---|---|---|---|
| **sqlite-vec** | SQLite 扩展（纯 C，MIT/Apache-2.0） | ⚠️ **预留路径需修正** | 上游 asg017 仓库 2025 年中起实质停滞（[issue #226](https://github.com/asg017/sqlite-vec/issues/226)，2025-06："关键 issue 无人处理"）；社区 fork 已接棒（v0.2.0-alpha）。vec0 虚表，float/int8/binary 向量，纯暴力扫描（无 ANN，作者已言明 pre-v1） |
| LanceDB | 嵌入式列存（Rust wheel） | ❌ 核心 / 🔶 tools | 零服务、Python 原生，但重依赖；违反核心 stdlib 纪律 |
| Chroma | 嵌入式/服务双态 | ❌ | 依赖链重（含 ONNX 默认 embedding），架构错位 |
| FAISS / hnswlib / usearch | 原生索引库 | ❌ 核心 / 🔶 tools 离线 | ANN 在会话级规模（千级向量）属过度设计——纯 Python 暴力余弦（≤2K×768d ≈ 0.1-0.3s）已够召回路径 |
| Qdrant / Milvus / pgvector | 独立服务 / 需 Postgres | ❌ | 新增服务/端口，直接违反演进不变式 |

### 2.2 RAG 编排框架

| 组件 | 匹配度 | 理由 |
|---|---|---|
| LangChain / LlamaIndex / Haystack | ❌ | 抽象层重、依赖树大；本系统管线 24 stage 自有编排，只缺一个检索函数而不是一个框架 |
| LightRAG / GraphRAG / nano-graphrag | ❌ | 图谱构建需 LLM 抽取管线 + 图存储（Neo4j/FalkorDB），与台账/manifest 已有的结构化血缘重复建设 |
| txtai | 🔶 tools | 最接近"轻"的纯 Python 系，但仍带 embedding 模型依赖，仅适合 tools/ 离线分析 |

### 2.3 Agent 记忆框架（mem0 / Letta / Zep·Graphiti / Cognee）

**全部不引入**（与选型文档 §3.5 既定结论一致，2026 年格局反证自建路线）：

- **Zep/Graphiti**：需图数据库 + 独立服务；2026 年对比文献实测其记忆足迹可达 600K tokens/会话量级（对照 Mem0 宣称 1,764）。
- **Mem0**：向量库 + LLM 抽取管线；核心思想（LLM 抽取事实→事实级存储）已被 SEM 语义卡设计吸收。
- **Letta（MemGPT）**：自带 agent server + PostgreSQL，是完整运行时而非库——与"代理是编排层、Claude Code 是客户端"的架构正面冲突。
- 共同问题：**都要求把自有存储栈和进程带入本系统**；本系统已有等价轻量对应物（bi-temporal→unit_id 时序、memory blocks→pins、抽取→SEM 蒸馏）。2026 年多份对比结论"无人全胜"（厂商基准互相矛盾），印证多租户 SaaS 场景的设计在单机单用户下自建成本低一个数量级。

### 2.4 KV 存储（记忆层）

| 组件 | 匹配度 | 理由 |
|---|---|---|
| **SQLite（WAL）** | ✅ **保持** | 单写者 + per-session 库 + MB 级数据 = SQLite 甜点区；`MAX_CONCURRENT=1` 纪律天然规避写竞争 |
| LMDB / RocksDB(plyvel) | ❌ | 十亿级记录/写放大优化场景；本量级下只增加原生依赖不增加价值 |
| Redis | ❌ | 常驻服务 + 新端口；进程内 LRU（`idempotency.py`）已覆盖同类需求 |
| diskcache / shelve | ❌ | 与 SQLite 功能重叠，多一个形态不如少一个 |
| DuckDB | 🔶 tools（既定） | 分析型列存，离线工具已定其位（`trace_query` 替代手写解析） |

### 2.5 KV Cache（serving 层，同名异义）

LMCache / Mooncake（KV cache 卸载/跨实例复用/PD 分离）是 vLLM/SGLang 生态件，针对多实例集群——**不匹配**本系统单机 Mac 栈。本系统的前缀缓存问题（hybrid trim-free 复用、append-only 纪律）是**行为问题**而非基础设施问题：`--hybrid-cache-entries 8` + 代理层 tail-append 纪律已闭环，无需引入。记录于此以消歧"KV 存储"两种含义。

## 3. 关键发现

1. **sqlite-vec 单点预留必须修订**（唯一需动选型文档的点）：上游停滞是硬事实。三级降级链（已落入选型文档 §3.4③）：`FTS5-only（默认）→ 纯 Python 暴力余弦（会话级规模，零依赖，float blob 存 SQLite + array 计算）→ 社区 fork load_extension（量级超阈值时，缺失自动回落）`。
2. **语义召回的真瓶颈是 embedding 生产方式**，三类路径各有代价：

| 路径 | 代价 | 判定 |
|---|---|---|
| llama-server sidecar（`--embedding --pooling` + Qwen3-Embedding-0.6B，llama.cpp 已验证支持 /v1/embeddings） | 新增进程/端口，违反演进不变式 | 需显式决策豁免才可启用 |
| 云端 embedding API | 成本 + 隐私（本地敏感代码外发） | 与路由敏感模式纪律冲突 |
| **LLM 蒸馏触发词**（skill 借鉴，SEM-P1 路线） | 每会话一次后台调用 | **零新增基础设施，优先**；本调研强化其优先地位 |

3. **四缺口中三个与检索引擎无关**：循环退火（状态机）、HealthGate（信号接线）、工具准入（入口检查）不吃任何 RAG 组件——不为"上 RAG"而引入组件。
4. **记忆框架 2026 格局反向验证自建路线**：台账 + manifest + 拉取的轻量形态在单机单用户下成本低一个数量级；SEM 蒸馏 = mem0 思想的 stdlib 化。

## 4. 结论

| 判定 | 内容 |
|---|---|
| **匹配，保持** | JSONL 热路径 + SQLite FTS5 trigram + LRU 幂等 + 「索引永不丢」manifest 模式 |
| **匹配，按修订后预留** | sqlite-vec 三级降级链（选型文档 §3.4③ 已更新）；DuckDB 限 tools |
| **不匹配，排除** | RAG 编排框架（LangChain/LlamaIndex/LightRAG 系）、记忆框架（mem0/Letta/Zep）、服务型向量库/KV（Redis/Qdrant/LMDB/RocksDB/pgvector）、KV cache 卸载件（LMCache/Mooncake） |
| **真实缺口（SEM-P2 前置决策）** | embedding 生产路径：sidecar vs 蒸馏 vs 云端——蒸馏路线与 `cognitive-gap-closure-design` §2（SEM）天然衔接，向量检索仅在蒸馏后召回率仍不达标时按三级降级链第 2/3 级启用 |

## 5. 关联文档

- `memory-storage-requirements-selection-20260829.md`：被修订对象（§3.3 表行 + §3.4③ 三级降级链）
- `cognitive-gap-closure-design-20260901.md`：SEM 方案（embedding 决策的宿主）与 TAC/HG/LRC（与检索引擎无关的佐证）
- `skill-progressive-disclosure-insights-20260901.md`：蒸馏触发词路线的机制依据（§3.1 索引行 description 工程）
- `context-architecture-evolution-20260829.md`：不变式来源（零新增进程/服务/端口）
