# Test Suite

This directory holds all automated tests for the project. Every commit to `main` must pass
the **unit** tier (enforced by a pre-commit hook); the **integration** and **e2e** tiers
are run manually before merging feature branches.

## 状态矩阵（存储是系统状态的一等维度，2026-09-01）

被测系统的状态不止报文与配置——`logs/diag/` 下的存储同样是系统状态，测试设计中必须显式对待：

| 状态类别 | 存储 | 隔离手段 | 测试纪律 |
|---|---|---|---|
| 会话键控存储 | `manifest/index/archive/ledger/<sid>.*` | 按会话键天然隔离；**键 ≤8 字符**（R14 截断契约） | 用 `test/lib/state_fixture.py` 的 `new_sid()`（唯一+截断安全）或已知 sids；集成结束必须清理 |
| 全局追加流 | `sessions.jsonl`、`hbe.jsonl`、`experiments.jsonl` | 不可隔离，只能行过滤 | 集成测试经 `test/lib/diag_cleanup.sh` 过滤自身行；单测经 `isolated_diag()` 重定向 |
| 派生缓存 | `index/<sid>.db`（FTS） | 随 manifest 源失效 | 轮转/植入后须验证一致性（L3 先例） |
| 进程内存态 | MANIFEST/LEDGER 内存表、频次计数、信号量 | 随进程 | 需要时以"新会话键"语义测试（内存命中 vs 磁盘加载两条路径） |

**共享工具**（禁止再手写隔离/清理逻辑）：
- `test/lib/state_fixture.py` — `isolated_diag()` / `new_sid()` / `plant_session()`（已知答案植入）/ `scrub_sessions()`
- `test/lib/diag_cleanup.sh` — 集成脚本 trap 用的统一清理（收 SID 列表；`ITEST_KEEP=1` 调试保留）
- `test/lib/state_sentinel.sh` — 元测试：集成套件末尾断言 itest 会话状态零残留

**层级纪律**：
- **unit**：零生产状态写入（全部经 `isolated_diag()` 或显式 tmp 路径）
- **integration**：只允许 itest 前缀会话键；退出时 `diag_cleanup` 必须执行；套件末尾过 state sentinel
- **promptfoo / e2e**：生产观测层——打真实代理、读写生产状态。**实验批跑窗口禁跑**（pre-commit 自动跳过逻辑见 `.githooks/pre-commit`）

## Layout

```
test/
├── run_tests.sh                    # unified runner: --unit / --integration / --e2e / --all
├── unit/                           # pure logic, no I/O, <1s (25 files, ~980 tests)
│   ├── test_proxy_fallback.py      # content tools fallback, blocker, truncation, compression
│   ├── test_proxy_reload.py        # SIGHUP hot-reload regression
│   ├── test_proxy_state.py         # config invariants, __all__ coverage, RELOAD_SPEC consistency
│   ├── test_backend_strategy.py    # LocalStrategy / CloudStrategy defaults + flags (22 tests)
│   ├── test_lifecycle.py           # stage classification, dynamic max_tokens (20 tests)
│   ├── test_admin_server.py        # /status rendering, percentile, metrics finalization (17 tests)
│   ├── test_payload_limit.py       # P0: 413 payload rejection
│   ├── test_text_loop.py           # text output loop detection
│   ├── test_tool_parser_edge.py    # XML↔JSON tool argument parsing edge cases
│   ├── test_utils.py               # percentile, stable hash, cast_config, jsonl logging
│   ├── test_message_converter.py   # Anthropic↔OpenAI tool/tool_choice conversion, token estimation
│   ├── test_content_compressor.py  # semantic compression (json/code/log/text) + audit fallback
│   ├── test_tool_parser.py         # tool argument parsing, <tools> content extraction
│   ├── test_proxy_logging.py       # sensitive header masking, JSONL/structured logging
│   └── test_model_registry.py      # model catalog: synthesis equivalence, $env/$default refs,
│                                   # fallback chains, validation, hot-swap rejection, catalog_hash
├── integration/                    # boots a mock backend, no real LLM, ~60s (7 suites)
│   ├── test_blocker_integration.sh
│   ├── test_loop_integration.sh
│   ├── test_cache_align_integration.sh
│   ├── test_compress_integration.sh
│   ├── test_memory_reject_integration.sh
│   ├── test_status_integration.sh
│   ├── test_long_context_integration.sh
│   └── mock_backend.py             # shared OpenAI-compatible mock fixture
├── e2e/                            # requires running proxy + backend, ~30-60s
│   ├── test_proxy_integration.py
│   └── e2e_tools_fallback.sh
├── promptfoo/                      # Promptfoo fixed-prompt regression (5 core tests)
├── fixtures/                       # function signatures, behavior snapshots
└── README.md                       # this file
```

## Running

The unified runner picks a tier by flag:

```bash
bash test/run_tests.sh --unit          # pure logic — 548 tests in <1s
bash test/run_tests.sh --integration   # mock backend — 7 suites ~60s
bash test/run_tests.sh --e2e           # needs running proxy + backend
bash test/run_tests.sh --all           # unit + integration + e2e + trace
bash test/run_tests.sh --fast          # alias for --unit (pre-commit uses this)
bash test/run_tests.sh --trace         # requirement traceability (docs/requirements.yaml)
```

## Pre-commit gate

The `.githooks/pre-commit` hook runs `--unit` before every `git commit`.
Install with `git config core.hooksPath .githooks` on a fresh clone.
Skip with `SKIP_TESTS=1 git commit …` or `git commit --no-verify`.

## Adding a new test

| Tier          | When to use                                    | File pattern                  |
|---------------|------------------------------------------------|-------------------------------|
| `--unit`      | pure function, no I/O, no network              | `test/unit/test_*.py` (unittest) |
| `--integration` | needs a mock backend, no real LLM             | `test/integration/*.{sh,py}`  |
| `--e2e`       | needs a live proxy + real (or cloud) backend   | `test/e2e/*.{sh,py}`          |

After adding, run the new file directly to make sure it works in isolation, then
update `run_tests.sh` to include it in the right tier.

## Logs

Test logs are written to `logs/`:

- `logs/unit_test.log`      — verbose unittest output
- `logs/itest/`             — integration test logs (mock, proxy, metrics)
- `logs/e2e_test.log`       — combined e2e sub-suite output
