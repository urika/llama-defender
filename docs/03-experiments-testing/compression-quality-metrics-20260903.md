# Compression Quality Metrics

## Purpose

Compression quality is evaluated in four layers: efficiency, fidelity, agent behavior, and runtime safety. A lower retained ratio is not automatically better; fidelity and task behavior are hard constraints.

## Raw data collection

`pipeline.ContentCompressor.output_metrics()` records per-request facts in `proxy_metrics.jsonl`:

- `context_before_chars` / `context_after_chars`
- `context_savings_ratio`
- `semantic_original_chars` / `semantic_compressed_chars` / `semantic_saved_chars`
- `semantic_audit_failures`
- `semantic_compressed`, `cleared`, `cleared_chars`, `think_stripped`
- `strategy`, `bm25_scores_avg`, `protected_n`

Existing request facts are also consumed:

- `status` and `quality_flags`
- `reread_pressure` or `ifc.reread_pressure`
- truncation and OOM stage results

The online path records facts only. It does not calculate expensive semantic metrics.

## Derived analysis

Run:

```bash
python3 tools/analyze_compression_quality.py --input logs/proxy_metrics.jsonl --json
```

For an A/B comparison:

```bash
python3 tools/analyze_compression_quality.py \
  --baseline logs/experiments/baseline.jsonl \
  --input logs/experiments/treatment.jsonl --json
```

The analyzer reports:

- P50/P90/average context and semantic savings;
- strategy distribution and actual compression trigger count;
- semantic audit pass rate;
- reread pressure, loop injection, blocker, and high-drop rates;
- raw field coverage;
- A/B deltas for success rate, savings, audit pass rate, reread pressure, loops, and blockers.

Metrics requiring original request or sent-view payloads are reported as unavailable rather than inferred: `entity_recall`, `anchor_retention`, `manifest_coverage`, `recall_success_rate`, `task_success_delta`, and `prefix_cache_delta`.

## Quality gates

Default gates are:

- audit pass rate is 100% when semantic compression events exist;
- loop injection rate is 0;
- blocker rate is 0;
- average context savings is at least 15% when compression data exists.

For release decisions, also require a paired task evaluation: compressed versus uncompressed runs on the same tasks, model, sampling parameters, and session seed. Task completion regression should be no more than 2 percentage points; critical coding tasks must not regress.

## Interpretation

The current `ratio` fields represent retained size (`after / before`). The analyzer reports `context_savings_ratio` as `1 - after / before`, which is the more intuitive efficiency metric. Efficiency must be reviewed together with reread pressure, loop/blocker rates, and task success.
