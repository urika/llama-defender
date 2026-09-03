# Context Management Trace Unit References (2026-09-03)

## Motivation

`span_id` previously told us *which* context-management stage ran and how long
it took, but not *which* context units it changed. This document records the
analysis and the chosen design: spans carry bounded **unit references**, not
message bodies.

## Decisions

1. **Bodies stay out of spans.** Full payloads already live in:
   - `logs/snapshots/<request_id>_before/after.json` — original request and
     error payloads
   - `logs/diag/archive/<sid>.jsonl` — final payload sent to the backend
   - `logs/diag/manifest/<sid>.jsonl` — index lines for dropped units
   - `logs/diag/orig/<sid>.jsonl` — originals for recoverable compression
   Duplicating them into metrics would bloat logs, duplicate PII/source, and
   turn spans from a lightweight index into a second archive.

2. **Unit anchors are the single vocabulary.** Reuse
   `ifc_metrics.unit_anchors()` (`u:<tool_use_id>`, `r:<tool_use_id>`,
   `h:<msg_hash>`) rather than inventing a second unit schema. A new
   `light=True` mode skips `head`/`triggers` so per-stage extraction stays
   cheap.

3. **Do not use `msg_idx`/`block_idx` as the durable reference.** These are
   stage-local positions that shift after reordering/truncation. Final logs
   reference anchors/unit ids.

4. **Pair atomicity is observable.** A `u:X`/`r:X` pair dropped together is an
   atomic drop (`pair_dropped`, `pair_integrity=true`). A one-sided drop is a
   protocol hazard and sets `pair_integrity=false`.

5. **Only changed units are listed, and the list is capped.** Default cap 20;
   overflow surfaces as `changed_total` + `changed_truncated`.

6. **Text anchors are weaker.** `h:<msg_hash>` identifies a content version,
   not a durable cross-version entity; text units rely on `session_id + turn`.

## Implementation

- `trace_context.py`
  - `finish_span(..., context=None)` persists a bounded `context` object on the
    span record.
  - `context_delta(before_units, after_units, limit=20)` is a pure function
    producing `{before_units, after_units, dropped_units, compressed_units,
    pair_dropped, pair_integrity, changed_total, changed_units,
    changed_truncated}`.
- `ifc_metrics.py`
  - `unit_anchors(msg, light=False)`; `light=True` omits `head`/`triggers`.
- `pipeline.py`
  - `_CONTEXT_TRACE_STAGES = {cache_aligner, content_compressor,
    context_truncator, oom_safety}`.
  - `_trace_unit_map(messages)` builds `anchor -> unit` via light anchors
    (fail-open -> `None`).
  - `InstrumentedPipeline.run` captures a before/after unit map for those
    stages and attaches `context_delta` to the stage span.

## Example span

```json
{
  "name": "context_truncator",
  "span_id": "sp_x",
  "context": {
    "before_units": 80,
    "after_units": 54,
    "dropped_units": 26,
    "compressed_units": 0,
    "pair_dropped": 3,
    "pair_integrity": true,
    "changed_total": 26,
    "changed_units": [
      {"anchor": "h:8f31a2c4", "kind": "text", "action": "dropped",
       "before_chars": 3200, "after_chars": 0, "recoverable": false}
    ],
    "changed_truncated": true
  }
}
```

## Join model

```text
trace_id / request_id
  -> span_id (which stage)
      -> anchor / unit_id (which unit)
          -> archive / manifest / orig (the actual content)
```

## Query tool

`tools/context_query.py` provides three lookups (stdlib only):

```bash
python3 tools/context_query.py request <request_id>    # 单请求上下文 + manifest
python3 tools/context_query.py session <session_key>   # 逐轮上下文摘要
python3 tools/context_query.py span <span_id>          # 定位单个 span
python3 tools/context_query.py --json <subcmd> ...     # 机器可读
```

Data joined: `proxy_metrics.jsonl` spans, `diag/sessions.jsonl` per-turn trace
records, and `diag/manifest/<sid>.jsonl` rows. Unit tests in
`test/unit/test_context_query.py` cover request/session/span lookups against a
synthetic logs fixture.

## Out of scope (future)

- Backend retry/fallback client spans and `ctx_recall` micro-turn spans still
  need their own spans (`kind="client"`).
- Durable per-version `unit_id` for plain-text messages (beyond `h:<hash>`).
- Enriching per-unit `strategy`/`recovery_key` in the span requires joining by
  anchor with manifest/orig at analysis time.
