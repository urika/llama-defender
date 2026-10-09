# 跨会话记忆 + 自动交接（R-CM-1/R-CM-2）P0 落地（2026-10-08）

> 需求：[`../01-requirements-product/context-engineering-enhancement-requirements-20261008.md`](../01-requirements-product/context-engineering-enhancement-requirements-20261008.md)
> 范围：P0 两项（R-CM-1 跨会话持久记忆、R-CM-2 自动交接摘要）。R-CM-3~R-CM-7（门控折叠/符号检索/预取/输出预算/召回度量）未实施。

## 1. 背景

复盘「小窗口时代 vibe coding 如何实现」并调研开源方案后确认：本仓已具备**取数（ctx_recall）/ 压缩（fifo·epoch·BM25）/ 缓存（append-only + prefix cache）/ 注入（admin/inject）**四条主干，但缺**跨会话记忆**与**自动交接**。本次补齐这两块（对应需求 P0）。

## 2. 交付内容

| 需求 | 模块 | 说明 |
|------|------|------|
| R-CM-1 | `memory_store.py` | 项目级持久记忆库，stdlib `sqlite3` + FTS5（不可用则 LIKE 回退）；`add/search/list/forget/stats/export_markdown`；`MEMORY` 单例；全链路 fail-open |
| R-CM-2 | `handoff.py` | 规则化从台账（`session_ledger`）抽取结构化 handoff（goal/files_touched/tools_used/recent_actions/repeated_targets）；`store`（JSONL + markdown）/`latest`/`render_block`/`distill_memory` |

## 3. 管线与端点

- 注入 stage：`MemoryInjectStage`（0.61）/ `HandoffInjectStage`（0.62），紧跟 `AdminInjectStage`（0.6）。
- 注入纪律：**会话首请求注入一次**（进程内守卫 `_SESSION_MEMORY_SEEN`/`_SESSION_HANDOFF_SEEN`，防每轮重复破坏前缀）；append-only 尾部；engine-on 走 `context_engine.append_injected`（跨轮存活，AdminInject 同款），engine-off view-only；`::aux` 会话跳过。
- 端点：
  - `GET /api/memory?project=&q=&mode=stats`
  - `POST /admin/memory`（body: text/kind/project/source/session_key/confidence）
  - `DELETE /admin/memory/<id>`
  - `GET /api/handoff?session=KEY[&render=1]`
  - `POST /admin/handoff`（body: session_key/goal/decisions/open_questions/next_steps/project/distill）
- CLI：`./manage.sh handoff <session_key> [--goal/--decision/--next/--question/--distill]`
- 分域：请求头 `X-Proxy-Project`（缺省 `PROXY_MEMORY_DEFAULT_PROJECT`）。

## 4. 配置（均 reloadable，默认关）

新增 10 项，登记于 `proxy_config.py` `CONFIG_REGISTRY` + `proxy_state.py` `_RELOAD_SPEC`（详见 [AGENTS.md §11.9](../../AGENTS.md)）：
`PROXY_MEMORY_ENABLED / _DB_PATH / _DEFAULT_PROJECT / _INJECT_MAX_CHARS / _TOP_K / _TTL_DAYS`、
`PROXY_HANDOFF_ENABLED / _TRIGGER_CHARS / _MAX_CHARS / _DIR`。

## 5. 启用（灰度）

```bash
# conf 追加
export PROXY_MEMORY_ENABLED=true
export PROXY_HANDOFF_ENABLED=true
./manage.sh restart     # 首次：加载新代码（SIGHUP 不重载 Python 代码）
# 之后改开关仅需 ./manage.sh reload
```

## 6. 测试

- `test/unit/test_memory_store.py`（8）、`test/unit/test_handoff.py`（8）、`test_pipeline_stages.py::TestMemoryHandoffInjectStages`（5）新增。
- `test/integration/test_rcm_memory_handoff.sh`（9 例）：隔离影子代理 + mock 后端端到端——记忆/交接注入到后端请求体、首请求守卫、GET 端点。已接入 `run_tests.sh --integration`。
- 实测结论（影子代理，未动生产）：**engine-off** 注入 view-only 单轮（req2 无块）；**engine-on**（生产配置）注入持久进 canonical、req2 仍在但**计数不增**（守卫防重复）。两模式各 9/9。运行 `RCM_ENGINE=true bash test/integration/test_rcm_memory_handoff.sh` 可复跑 engine-on 变体。
- `bash test/run_tests.sh --unit` → 1917 passed；`--snapshot` 57 全过；`--signature` 重生成 `test/fixtures/func_signatures.json`（185 签名）后 PASS。
- `./manage.sh config-lint` / `bash -n manage.sh` / `import anthropic_proxy` 均 OK。

## 7. 回退

- 运行时：置 `PROXY_MEMORY_ENABLED=false`、`PROXY_HANDOFF_ENABLED=false` + reload（零注入）。
- 代码：删除两模块 + 撤销 `pipeline.py` 两 stage 与 `anthropic_proxy.py` 端点/装配；无 schema 迁移（记忆 DB 为独立文件，删 `logs/memory/` 即净）。

## 8. 已知限制（后续）

- P0 交接为**规则化**生成（确定性、零后端依赖）；需求所述 LLM 富化（喂后端模型补 decisions/open_questions）接口已预留 `handoff.build(..., enrich=...)`，未接线。
- `PROXY_MEMORY_TTL_DAYS` 仅登记，驱逐策略未实现（`MAX_ROWS_PER_PROJECT` 软上限生效）。
- engine-off 时首轮注入不跨轮存活（依赖 engine-on canonical）。

> 追溯：R-CM-1/R-CM-2 已登记 `docs/requirements.yaml`（domain `R-CM`）并在 `tools/trace_requirements.py` 支持 `R-CM-<n>`；`--trace` 通过（30 requirements）。

## 9. 附：R-CM-6 输出预算自适应（同日补，2026-10-08）

- **现状**：R-CM-6 目标（消除写文件轮次被 `saturation` 档 8192 截断）**已由既有 L-73（2026-10-07）实现**——`lifecycle._compute_dynamic_max_tokens`：长载荷工具（`Write/Edit/MultiEdit/NotebookEdit/Bash`，`pipeline._LONG_PAYLOAD_TOOLS`）轮次**完全不降压**；其余带工具轮次下限 `PROXY_DYNAMIC_MAX_TOKENS_FLOOR`（默认 32768）；纯文本轮次保留原降压。测试 7 例（`test/unit/test_lifecycle.py::TestComputeDynamicMaxTokens`）。
- **本次补齐**：`PROXY_DYNAMIC_MAX_TOKENS_FLOOR` / `_WRITE_NO_REDUCE` 此前**声明 reloadable 却未进 `_RELOAD_SPEC`**（reload 不生效）——现补入；文档/`requirements.yaml` R-CM-6 锚点对齐。
- **未重复实现**：需求初稿拟用的 `..._WRITE_BYPASS`/`..._WRITE_FLOOR` 名称作废，统一用既有 `..._WRITE_NO_REDUCE`/`..._FLOOR`。

## 10. 附：R-CM-7 召回采纳率度量（同日补，2026-10-08）

- **实现**：
  - `diagnostics._rcm_record(ctx)` → per-turn `sessions.jsonl` 增 `rcm` 段 `{memory,handoff,recall}`（0/1，取自 `injection_details`；决策 `finalize_request` 写入）。
  - `admin_server._build_session_metrics_json` → `/api/session/<key>/metrics` 增 `rcm` 聚合段：
    - `memory_injected_turns` / `handoff_injected_turns` / `recall_injected_turns`；
    - `first_injection_turn` / `turns_after_first_injection`（会话在注入后延续的 lead-lag 代理）；
    - `recall_adoption{adopted,total,rate}`：**台账口径**——`auto_recall` 注入目标在注入轮之后未再被读取（`dup_queries[target].last_turn <= 注入轮`）计采纳；
    - `recall_hit_rate` = `ctx_recall.hits/(hits+misses)`。
- **口径诚实**：台账不在此进程（重启后内存态缺失）→ `recall_adoption.total=0 / rate=null`，不编造。采纳定义与 `ctx-recall-auto-closed-loop` 设计"由 last_dup_turn 差分"一致，代理侧先行提供数。
- **测试**：`test/unit/test_rcm_metrics.py`（3 例：计数/lead-lag/采纳率、台账缺失降级、无注入）全绿。
- **配置**：无新增开关（沿用 `PROXY_DIAG_ENABLED`）。

## 11. 附：R-CM-3 门控式 epoch 折叠（同日补，2026-10-08）

- **实现**：`context_engine.build_epoch_rubric(sess, session_key)` 三判据——`closed`（末轮非悬空 tool_use，`_last_round_closed`）/ `progress`（`user_msgs > last_epoch_user_msgs`）/ `not_stuck`（台账 `dup_queries.count < PROXY_CTX_EPOCH_RUBRIC_STUCK_MAX`）。`CanonicalSession.maybe_epoch(..., rubric=None)` 新增 `rubric` 形参：非 None 且非全 True → **不折叠**（保持 canonical，返回未触发），日志 `epoch gated by rubric`。`pipeline.ContextEngineStage` 在开关开启时构造并传入 rubric。
- **默认关**：`PROXY_CTX_EPOCH_RUBRIC_ENABLED=false` → `rubric=None` → 纯 token 阈值旧行为不变（零回归）。`PROXY_CTX_EPOCH_RUBRIC_STUCK_MAX=5`。
- **取舍**：卡住时不折叠可能使视图继续增长至 `ContextOverflowError`（宁可不压也不丢中间态）；换路线提示由 auto-recall 卡死升级路径另行注入（未在本项合并）。
- **测试**：`test/unit/test_epoch_rubric.py`（5 例：闭单元/进展/卡住/门控跳过/rubric=None）全绿。

## 12. 附：R-CM-4 符号级检索增强 + R-CM-5 预取推送（同日补，2026-10-08）

### R-CM-4 符号级检索增强（`ctx_recall.py`，默认关）
- `format_recall_result` 改为薄包装：调用 `_format_recall_result`（原实现）+ 追加 `format_symbol_section(query, session_key)`。禁用/无命中/fail-open → 无追加。
- 符号提取 `_symbol_ident(query)`：优先含 `_` 或大小写混合（PascalCase/camelCase）的标识符，否则首个 ≥4 字符标识符；`_candidate_files(session_key)` 取本会话 manifest 句柄中磁盘存在、且在 `PROXY_RECALL_SYMBOL_ROOT` 内的文件；`symbol_refs(...)` 经 subprocess 调 `grep -nw`（或 `PROXY_RECALL_SYMBOL_CMD` 覆盖），解析 `path:line:text`。
- 配置（reloadable，默认关）：`PROXY_RECALL_SYMBOL_ENABLED=false`、`_TOOL=grep`、`_CMD=""`、`_MAX_REFS=8`、`_TIMEOUT_S=3`、`_ROOT=""`。
- 核心 stdlib-only；外部工具经 subprocess，异常/超时 fail-open。

### R-CM-5 预取/推送披露（`pipeline.PrefetchStage` 12.6，默认关）
- 需求信号 = 最近 user 文本；`_bm25_rank_lines`（复用 `content_compressor._bm25_tokenize` + 本地 BM25）对 manifest 已折叠 `tool_result` 单元排序取 top-k → `recover_full_content` 取回 → `[System: PREFETCH]` 尾部注入（append-only）。
- 去重/限额：与 `AutoRecallStage` **同轮去重**（本轮 auto-recall 已注入则跳过）；锚点级 `_SESSION_PREFETCH_STATE` 去重 + 每会话 `PROXY_PD_PREFETCH_PER_SESSION=5` 上限。
- 配置（reloadable，默认关）：`PROXY_PD_PREFETCH_ENABLED=false`、`_TOP_K=3`、`_MAX_CHARS=2000`、`_PER_SESSION=5`。
- 测试：`test/unit/test_recall_symbol.py`（12）、`test/unit/test_prefetch_stage.py`（8）全绿。
