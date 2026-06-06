# Test Suite

This directory holds all automated tests for the project. Every commit to `main` must pass
the **unit** tier (enforced by a pre-commit hook); the **integration** and **e2e** tiers
are run manually before merging feature branches.

## Layout

```
test/
├── run_tests.sh                # unified runner with --unit / --integration / --e2e / --all
├── unit/                       # pure logic, no network, <1s
│   └── test_proxy_fallback.py
├── integration/                # boots a mock backend, no real LLM, ~5s
│   ├── test_blocker_integration.sh
│   └── mock_backend.py         # shared OpenAI-compatible mock fixture
├── e2e/                        # requires a running proxy + backend, ~30-60s
│   ├── test_proxy_integration.py
│   └── e2e_tools_fallback.sh
├── fixtures/                   # (reserved for future shared fixtures)
└── README.md                   # this file
```

## Running

The unified runner picks a tier by flag:

```bash
bash test/run_tests.sh --unit          # pure logic — runs in <1s, no I/O
bash test/run_tests.sh --integration   # boots mock backend on :8089 + proxy on :4001
bash test/run_tests.sh --e2e           # hits the live proxy on :4000 + backend on :8081
bash test/run_tests.sh --all           # unit + integration + e2e (in that order)
bash test/run_tests.sh --fast          # alias for --unit (used by pre-commit)
```

Override URLs for the e2e tier when running against cloud mode or a non-default port:

```bash
PROXY_BASE=http://127.0.0.1:4000 BACKEND_URL=http://127.0.0.1:8081 \
  bash test/run_tests.sh --e2e
```

## Pre-commit gate

`.githooks/pre-commit` is wired up automatically — it runs the **unit** tier on every
`git commit`. If the unit tests fail, the commit is rejected.

To install on a fresh clone (the file is committed; `core.hooksPath` is per-machine):

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit test/run_tests.sh
```

Skip mechanisms (use sparingly — both are recorded in commit history by the user's choice):

| Mechanism                       | Scope                  | Recommended? |
|---------------------------------|------------------------|--------------|
| `SKIP_TESTS=1 git commit -m …`  | skips this hook only   | emergency    |
| `git commit --no-verify`        | skips **all** git hooks| emergency    |

The hook is also bypassed automatically during `git rebase` and `git merge` (when
`MERGE_HEAD` exists) so rebasing past commits doesn't re-run the entire test history.

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
- `logs/itest/`             — blocker-integration raw logs (`proxy.log`, `mock.log`, `mock_capture.jsonl`, `proxy_metrics.jsonl`)
- `logs/e2e_test.log`       — combined e2e sub-suite output
