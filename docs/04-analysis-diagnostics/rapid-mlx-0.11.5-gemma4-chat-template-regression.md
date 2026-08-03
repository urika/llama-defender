# rapid-mlx 0.11.5 Gemma 4 chat_template 回归

> 本文档记录 rapid-mlx 从 0.6.71 升级到 0.11.5 后，`mlx-community/gemma-4-26b-a4b-it-4bit`
> 因 `chat_template_type=gemma4` 触发 `ModuleNotFoundError` 导致启动崩溃的问题，以及本地修复方案。
> 按 GitHub Issue + PR 格式整理，可直接向 [raullenchai/Rapid-MLX](https://github.com/raullenchai/Rapid-MLX) 提交。

---

## 🐛 Issue: `ModuleNotFoundError: mlx_lm.chat_templates.gemma4` crashes Gemma 4 26B at startup (0.11.5 regression)

### Summary

After upgrading rapid-mlx from **0.6.71 → 0.11.5**, every Gemma 4 model whose
`tokenizer_config.json` declares `"chat_template_type": "gemma4"` fails to boot:

```
ModuleNotFoundError: No module named 'mlx_lm.chat_templates.gemma4'
ERROR:    Application startup failed. Exiting.
```

Affected alias: `mlx-community/gemma-4-26b-a4b-it-4bit` (HF id). Notably the
**local-path** checkpoint `lmstudio-community/gemma-4-31B-it-MLX-4bit` boots fine —
its `tokenizer_config.json` ships an inline `chat_template` string and leaves
`chat_template_type` unset, so it never hits the `importlib.import_module` branch.

### Environment

| Item | Value |
|------|-------|
| rapid-mlx | `0.11.5` (Homebrew tap `raullenchai/rapid-mlx`) |
| mlx-lm (bundled) | ships `mlx_lm/chat_templates/` with **only** `__init__.py` + `deepseek_v32.py` |
| OS | macOS 26.5.1 (Darwin 25.5.0), Apple Silicon (M-series, 48 GB) |
| Model | `mlx-community/gemma-4-26b-a4b-it-4bit` (revision `efbeee6e582ebfd06abc9d65e90839c4b5d2116b`) |
| Command | `rapid-mlx serve mlx-community/gemma-4-26b-a4b-it-4bit --tool-call-parser gemma4 --reasoning-parser gemma4 --no-mllm` |

### Reproduction

```bash
rapid-mlx serve mlx-community/gemma-4-26b-a4b-it-4bit \
  --host 127.0.0.1 --port 8081 \
  --enable-auto-tool-choice --tool-call-parser gemma4 \
  --reasoning-parser gemma4 --no-thinking --no-mllm \
  --gpu-memory-utilization 0.70
# → Application startup failed. Exiting.
```

### Expected behaviour

The server boots and serves the model. The upstream model directory **already
contains a `chat_template.jinja`** (Google Gemma 4 Canonical Template, 2026-07-09),
so a missing in-package `mlx_lm.chat_templates.gemma4` module should fall back to
that file rather than crash.

### Actual behaviour

```
INFO:rapid_mlx.utils.tokenizer:Gemma 4 native load failed
      (No module named 'mlx_lm.chat_templates.gemma4'),
      falling back to text-only wrapper (legacy mlx-lm)
...
  File ".../mlx_lm/tokenizer_utils.py", line 621, in load
    chat_template = importlib.import_module(
        f"mlx_lm.chat_templates.{chat_template_type}"
    ).apply_chat_template
ModuleNotFoundError: No module named 'mlx_lm.chat_templates.gemma4'
ERROR:    Application startup failed. Exiting.
```

### Root cause

`mlx_lm/tokenizer_utils.py:620-623` unconditionally imports a chat-template
module whenever `tokenizer_config.chat_template_type` is set:

```python
# mlx_lm/tokenizer_utils.py
if chat_template_type := tokenizer_config.get("chat_template_type", False):
    chat_template = importlib.import_module(
        f"mlx_lm.chat_templates.{chat_template_type}"
    ).apply_chat_template
```

But 0.11.5's bundled mlx-lm **removed** `mlx_lm/chat_templates/gemma4.py`
(the directory now contains only `__init__.py` and `deepseek_v32.py`). The
`mlx-community/gemma-4-26b-a4b-it-4bit` repo still declares
`"chat_template_type": "gemma4"`, so the import raises and there is **no
fallback to the on-disk `chat_template.jinja`** at this code site.

The same model booted under 0.6.71 because that release still shipped the
`gemma4` chat-template module — i.e. this is a **regression introduced by the
mlx-lm module removal** without a corresponding tolerance in the loader.

### Why the local-path 31B is unaffected

| Checkpoint | `chat_template_type` | inline `chat_template` | Result |
|------------|----------------------|------------------------|--------|
| `lmstudio-community/gemma-4-31B-it-MLX-4bit` (local) | `None` (unset) | ✅ 18 681-char string | boots — `chat_template_type` falsy, branch skipped |
| `mlx-community/gemma-4-26b-a4b-it-4bit` (HF id) | `"gemma4"` | ❌ absent | crashes — import attempted |

This asymmetry — two checkpoints of the *same* model family behaving
differently — is the tell-tale sign of a loader-level regression, not a model
defect.

### Related

- Rapid-MLX #1408 (closed, 2026-08-03) `fix(gemma4): unbreak bench, unlock live
  KV quantization` — fixes `gemma4_unified` *bench*/KV paths but **not** the
  `chat_template_type` import path described here.
- mlx-lm #1125 — Gemma 4 26B tool-call issues under the recommended sampling
  (separate concern; our tool-calls work once the template loads).
- `lmstudio-bug-tracker` #1741 — LM Studio's bundled `mlx_vlm` lacks gemma4 defs
  (same upstream "module removed" class of problem).

### Triage — 归属分析：为什么报给 Rapid-MLX（而非 mlx-lm 或模型仓库）

崩溃栈最底层在上游 `mlx_lm/tokenizer_utils.py`，因此归属有三个候选：
**Rapid-MLX**、**ml-explore/mlx-lm**、**mlx-community 模型仓库**。结论是
**Rapid-MLX**，理由如下：

| 候选 | 崩溃代码是否在此 | 能否自行修复 | 维护活跃度 | 修复路径 |
|------|------------------|--------------|-----------|---------|
| **Rapid-MLX** (`vllm_mlx`) | ❌（在 vendored mlx-lm 内） | ✅ 能——在 `mlx_lm.load` 前预处理 `tokenizer_config` | 高（issues 每日更新；Gemma 4 是其 README 主推家族） | **短**——可立即合并，用户下次 `brew upgrade` 即受益 |
| **mlx-lm** (`ml-explore`) | ✅ `tokenizer_utils.py:620` | ✅ 能——给 `importlib` 加容错 | 中（Apple 官方） | 长——上游发版 → Rapid-MLX 重新 vendor → 用户升级 |
| **模型仓库** (`mlx-community/...`) | ❌ | ✅ 能——删字段或补内嵌模板 | 低 | 治标——每个新 Gemma 4 仓库都要改一遍 |

**为什么崩溃行在 mlx-lm，却归 Rapid-MLX：**

1. **Rapid-MLX 控制版本组合。** 它 vendor 了特定版本的 mlx-lm，而该版本砍掉了
   `chat_templates/gemma4.py`。回归只存在于它的 bundle 里；回退版本或加防护是它能拉的杠杆。
2. **Rapid-MLX 已承接这份责任。** 它的 `vllm_mlx/utils/tokenizer.py` 已有 gemma4 专用
   fallback（`load_model_with_fallback` 里对 `mlx_lm.load` 的 try/except）——即它已知道 gemma4
   加载脆弱、主动 wrap。该 wrapper 只是没覆盖 chat-template 的 import 步骤。扩展它是顺理成章的修复（见下方 PR）。
3. **到用户路径最短。** Rapid-MLX 的 PR 数天即可合并；mlx-lm 的修复要先上游发版、再被
   Rapid-MLX 重新 vendor、才到用户。

欢迎在 `ml-explore/mlx-lm` 平行提交一个上游修复（让 import 本身容错——try/except 后用
`chat_template.jinja`），那会让每个下游消费者都健壮；但本报告与之独立。**模型仓库无责**：
声明 `chat_template_type=gemma4` 并提供 `chat_template.jinja` 是正当的，是 loader 没有兜住 fallback。

### Checklist before submitting upstream

- [ ] Search existing issues (none found for `chat_template_type gemma4`).
- [ ] Confirm reproducible on a clean brew install (it is — fresh 0.11.5 keg).
- [ ] Attach the full traceback (see PR section below).

---

## 🔧 PR: `fix(gemma4): strip unsupported chat_template_type before mlx_lm.load, fall back to chat_template.jinja`

### What does this PR do?

Adds a preprocess step in **rapid-mlx's own** `vllm_mlx/utils/tokenizer.py`:
before calling `mlx_lm.load`, if the model's `tokenizer_config` declares a
`chat_template_type` whose module is **not bundled** in the vendored mlx-lm,
strip the field so `AutoTokenizer.from_pretrained` falls back to the on-disk
`chat_template.jinja`. One small, surgical change — no default-behaviour change
for models whose module exists.

```python
# vllm_mlx/utils/tokenizer.py  (proposed — rapid-mlx's own code, directly mergeable)
import importlib.util

def _strip_unsupported_chat_template_type(tokenizer_config: dict) -> None:
    """The vendored mlx-lm (>= 0.32) dropped several bundled chat_templates.*
    modules (e.g. gemma4). When a model's tokenizer_config.json still declares
    one of those types, mlx_lm's unconditional importlib.import_module at
    tokenizer_utils.py:620 raises ModuleNotFoundError and the server fails to
    boot. Strip the field in that case so AutoTokenizer falls back to the
    chat_template.jinja it already loads from the model dir (present for every
    mlx-community Gemma 4 checkpoint)."""
    ct = tokenizer_config.get("chat_template_type")
    if ct and not importlib.util.find_spec(f"mlx_lm.chat_templates.{ct}"):
        tokenizer_config.pop("chat_template_type", None)
        logger.info(
            "stripped chat_template_type=%s (module not bundled in mlx-lm); "
            "AutoTokenizer will use chat_template.jinja from the model dir",
            ct,
        )

# --- wire-up: top of _load_model_with_fallback_impl, before `from mlx_lm import load` ---
def _load_model_with_fallback_impl(model_name, tokenizer_config=None):
    tokenizer_config = dict(tokenizer_config or {})           # don't mutate caller's dict
    _strip_unsupported_chat_template_type(tokenizer_config)   # <- new
    from mlx_lm import load
    ...
```

### Why is this needed?

Gemma 4 is a README-recommended alias family. Today on 0.11.5:

| | before | after |
|---|---|---|
| `rapid-mlx serve mlx-community/gemma-4-26b-a4b-it-4bit` | `ModuleNotFoundError: mlx_lm.chat_templates.gemma4` → startup failed | boots, answers correctly, tool-calls work |
| `serve --tool-call-parser gemma4` (26B/31B/e2b/e4b aliases with `chat_template_type=gemma4`) | crash | boots |

No default-behaviour change: when the module *does* exist, `find_spec` returns
it, the field is kept, and the path is byte-identical.

### Why fix it in `vllm_mlx`, not in bundled mlx-lm?

The crash originates in upstream `mlx_lm/tokenizer_utils.py`, but rapid-mlx
**vendors** mlx-lm as a dependency and cannot patch it in-tree. rapid-mlx's own
`vllm_mlx/utils/tokenizer.py` already carries a gemma4-specific fallback
(`load_model_with_fallback`'s try/except around `mlx_lm.load`) — that wrapper
just doesn't cover the chat-template import step. The preprocess hook above is
the natural extension of that existing fallback design: it lives entirely in
rapid-mlx's codebase and is directly mergeable without waiting for an upstream
mlx-lm release + re-vendoring. (A parallel upstream fix at `ml-explore/mlx-lm`
— making the import itself tolerant — is welcome, but this PR is independent of it.)

### How the output was verified

Workaround validated locally by deleting `chat_template_type` from the model's
`tokenizer_config.json` (equivalent to the loader treating the module as
absent), which forces the same `chat_template = None` branch and lets
`AutoTokenizer`'s already-loaded `chat_template.jinja` take over:

```bash
# 1. locate the real blob (HF cache stores snapshots as symlinks → blobs)
TC=$(readlink -f ~/.cache/huggingface/hub/models--mlx-community--gemma-4-26b-a4b-it-4bit/ \
  snapshots/efbeee6e582ebfd06abc9d65e90839c4b5d2116b/tokenizer_config.json)
cp "$TC" "$TC.bak"   # backup

# 2. drop the field that triggers the broken import
python3 -c "
import json, sys
d = json.load(open('$TC'))
d.pop('chat_template_type', None)
json.dump(d, open('$TC','w'), ensure_ascii=False, indent=2)
"

# 3. boot + smoke-test
rapid-mlx serve mlx-community/gemma-4-26b-a4b-it-4bit \
  --host 127.0.0.1 --port 8081 \
  --enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4 \
  --no-mllm --gpu-memory-utilization 0.70
# → ✅ Rapid-MLX 就绪; inference + tool-call both correct
```

**Smoke-test results** (post-fix):

```
GET /v1/models → mlx-community/gemma-4-26b-a4b-it-4bit

POST /v1/messages  "用一句话解释什么是内存泄漏"
→ "内存泄漏是指程序在运行过程中只管申请内存使用，却在不再需要时
    未能将其正确释放，导致系统可用内存逐渐减少……"      ✅ coherent

POST /v1/messages  tools=[get_weather(city)]  "北京天气怎么样？"
→ tool_use: get_weather  {"city": "北京"}        ✅ structured tool-call
```

The `tool_parsers.gemma4` module **is** still bundled (0.11.5 ships
`mlx_lm/tool_parsers/gemma4.py`), so `tool_parser_type=gemma4` continues to
work — only the *chat-template* module was removed.

### Why not `rapid-mlx pull` / re-download?

Re-pulling fetches the same repo revision, which still carries
`"chat_template_type": "gemma4"` in `tokenizer_config.json`. The crash is a
loader/mlx-lm mismatch, not a corrupt download, so a re-pull reproduces the
identical failure. The loader fix (or the local `tokenizer_config.json` edit)
is the actual resolution. An upstream `mlx-community/gemma-4-26b-a4b-it-4bit`
repo fix (drop `chat_template_type`, keep the `.jinja`) would also clear it,
but the loader should be tolerant regardless.

### Test plan

- New `tests/test_strip_unsupported_chat_template_type.py`:
  - `chat_template_type` set + module present → field kept (byte-identical).
  - `chat_template_type` set + module absent → field removed, logged once.
  - `chat_template_type` absent → no-op.
  - caller's `tokenizer_config` dict is not mutated (function copies internally).
- `ruff check && ruff format --check` clean.
- End-to-end on `mlx-community/gemma-4-26b-a4b-it-4bit`: serve boots, answers,
  tool-calls — as captured above.
- Regression on `lmstudio-community/gemma-4-31B-it-MLX-4bit` (local, no
  `chat_template_type`): byte-identical behaviour.

### Checklist

- [x] Reproduced on a clean 0.11.5 install.
- [x] Identified the exact failing line (`mlx_lm/tokenizer_utils.py:620`).
- [x] Verified the on-disk `chat_template.jinja` is a valid fallback.
- [x] Verified `tool_parsers.gemma4` is unaffected (still bundled).
- [x] Smoke-tested inference + tool-calling after the (equivalent) workaround.
- [x] Confirmed the fix lives in rapid-mlx's own `vllm_mlx` (no dependency patch).
- [ ] Submit upstream PR to `raullenchai/Rapid-MLX`.

---

## 本地处置记录（本项目）

| 时间 | 动作 |
|------|------|
| 2026-08-03 | rapid-mlx 升级 0.6.71 → 0.11.5；回归验证发现 gemma4-26b 启动崩溃 |
| 2026-08-03 | 定位根因：`chat_template_type=gemma4` 触发 `importlib` 加载已删除模块 |
| 2026-08-03 | 本地修复：从 HF 缓存 blob 中删除 `chat_template_type` 字段（已备份 `.bak-20260803`） |
| 2026-08-03 | 验证：推理 + 工具调用均正常；其余 3 个 rapid-mlx 模型回归通过 |
| 2026-08-03 | ✅ Issue 已提交：[#1420](https://github.com/raullenchai/Rapid-MLX/issues/1420) |
| 待办 | PR 修复（需先在 rapid-mlx 源码实现 `_strip_unsupported_chat_template_type` + 测试，再提交） |

> ⚠️ **本地补丁脆弱性提示**：删除字段是对 HF 缓存 blob 的就地修改。若将来
> `rapid-mlx pull --force` 或 `huggingface-cli download --force` 重下该模型，
> `tokenizer_config.json` 会被上游版本覆盖，`chat_template_type` 复现，崩溃回归。
> 升级 rapid-mlx 时需重新回归此项，或待上游 loader 修复后移除本地补丁。
