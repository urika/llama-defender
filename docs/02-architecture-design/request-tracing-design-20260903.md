# Request Tracing Design

## Scope

The proxy uses lightweight tracing without third-party dependencies. Existing
`request_id` remains the identifier for one proxy HTTP request. `trace_id`
groups one logical request chain, and `span_id` identifies one operation in
that chain.

```text
trace_id
└── request_id / root_span_id
    ├── request_parser
    ├── content_compressor
    ├── context_truncator
    └── backend_dispatcher
```

## ID semantics

| Field | Meaning | Lifetime |
|---|---|---|
| `trace_id` | Logical request chain | Shared by retries/fallbacks when implemented in one chain |
| `request_id` | One proxy HTTP request | One incoming request |
| `root_span_id` | Root operation for the proxy request | One incoming request |
| `span_id` | One executed pipeline operation | One operation |
| `parent_span_id` | Parent operation ID | One span |

Generated IDs are random and contain no prompt data. A valid W3C
`traceparent` supplies the incoming trace ID. `X-Proxy-Trace-Id` is accepted
for local integration, but invalid values are ignored.

## Recording

The request entry creates the trace context. `InstrumentedPipeline` creates
one span around every executed stage and records status, duration, and stage
name. Conditional stages that do not execute are counted in the existing
pipeline summary but do not get a span.

The trace summary is written to `proxy_metrics.jsonl` under `trace` and to the
R16 diagnostic record under the same fields. Responses expose:

- `X-Proxy-Trace-Id`
- `X-Proxy-Span-Id` (the request root span)

The current `request_id` response header remains unchanged.

## Context unit references in spans

Context-management spans (`cache_aligner`, `content_compressor`,
`context_truncator`, `oom_safety`) carry a bounded `context` summary of unit
changes instead of message bodies:

```json
{
  "trace_id": "tr_x",
  "span_id": "sp_x",
  "name": "content_compressor",
  "context": {
    "before_units": 12,
    "after_units": 12,
    "dropped_units": 0,
    "compressed_units": 1,
    "pair_dropped": 0,
    "pair_integrity": true,
    "changed_total": 1,
    "changed_units": [
      {
        "anchor": "r:call_123",
        "kind": "tool_result",
        "action": "compressed",
        "before_chars": 12000,
        "after_chars": 1800
      }
    ],
    "changed_truncated": false
  }
}
```

Design rules:

- Unit anchors reuse `ifc_metrics.unit_anchors` (`u:<id>` / `r:<id>` /
  `h:<msg_hash>`), so there is only one unit vocabulary. `light=True` skips
  `head`/`triggers` to keep per-stage extraction cheap.
- A `u:X`/`r:X` pair dropped together counts as an atomic drop
  (`pair_dropped`) and keeps `pair_integrity: true`; a one-sided drop flips
  `pair_integrity: false`.
- `changed_units` is capped at 20; overflow is reported via `changed_total`
  and `changed_truncated`.
- Spans never contain body text, API keys, or unbounded handles. Bodies stay
  in `archive/`, `manifest/`, `orig/`, and `snapshots/`.
- The full trace (including `context`) is written under `metrics.trace`;
  R16 diag records keep only summary IDs to avoid duplication.

## Query tool

`tools/context_query.py` queries context unit references by request, by
session, or by span id:

```bash
python3 tools/context_query.py request <request_id>
python3 tools/context_query.py session <session_key> [--limit N] [--turn N]
python3 tools/context_query.py span <span_id>
python3 tools/context_query.py --json request <request_id>   # machine readable
```

It joins `proxy_metrics.jsonl` spans, `diag/sessions.jsonl` per-turn trace
records, and `diag/manifest/<sid>.jsonl` index rows. Outputs per-stage
compressed/dropped unit anchors, char deltas, pair integrity, and manifest
reason/tool/handle detail.

## Failure and retry semantics

An exception marks the current stage span as `error`. The request trace stays
available for normal metrics and snapshot correlation. Future backend retry or
fallback attempts should create sibling spans under `root_span_id` with
`kind="client"` and attributes such as `provider`, `model`, and `attempt`.

`trace_context.py` is thread-local and is cleared at the end of every HTTP
request, so IDs cannot leak between requests.

## Queries

Existing request correlation remains valid:

```bash
python3 tools/trace_query.py --json request <request_id>
```

The returned metrics record contains `trace_id`, `root_span_id`, and the
serialized `spans` list. The trace can then be joined with snapshots and R16
diagnostics using `request_id`.

## Privacy and limits

Trace records contain identifiers, stage metadata, and timing only. They do
not add prompt or tool-result payloads to logs. Span attributes must remain
bounded and must not contain API keys or raw message content.