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
