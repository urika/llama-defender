# Test Suite

This directory holds all automated tests for the project. Every commit to `main` must pass
the **unit** tier (enforced by a pre-commit hook); the **integration** and **e2e** tiers
are run manually before merging feature branches.

## Layout

```
test/
├── run_tests.sh                    # unified runner: --unit / --integration / --e2e / --all
├── unit/                           # pure logic, no I/O, <1s (10 files, 462 tests)
│   ├── test_proxy_fallback.py      # content tools fallback, blocker, truncation, compression
│   ├── test_proxy_reload.py        # SIGHUP hot-reload regression
│   ├── test_proxy_state.py         # config invariants, __all__ coverage, RELOAD_SPEC consistency
│   ├── test_backend_strategy.py    # LocalStrategy / CloudStrategy defaults + flags (22 tests)
│   ├── test_lifecycle.py           # stage classification, dynamic max_tokens (20 tests)
│   ├── test_admin_server.py        # /status rendering, percentile, metrics finalization (17 tests)
│   ├── test_payload_limit.py       # P0: 413 payload rejection
│   ├── test_text_loop.py           # text output loop detection
│   ├── test_tool_parser_edge.py    # XML↔JSON tool argument parsing edge cases
│   └── test_utils.py               # percentile, stable hash, cast_config, jsonl logging
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
bash test/run_tests.sh --unit          # pure logic — 462 tests in <1s
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
