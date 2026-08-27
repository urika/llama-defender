#!/usr/bin/env python3
"""Ornith-1.5-35B-A3B target-specific DFlash drafter distillation.

Adapted from audreyt/Ornith-1.5-9B-DFlash-GGUF `dflash_distill_mlx.py` for the
installed dflash-mlx 0.1.8 API (projected-context forward via DFlashDraftModel).

Pipeline: generate target responses -> cache frozen target hidden features at
the drafter's target_layer_ids -> fine-tune the Qwen3.6-35B DFlash drafter with
the DFlash position-weighted block cross-entropy objective -> export weights.

Uses a curated local prompt list (no `datasets` dependency / HF network).
"""

import argparse
import json
import math
import random
import shutil
import time
from pathlib import Path

import mlx.core as mx
from mlx import nn
import mlx.optimizers as optim
from mlx.utils import tree_flatten
from huggingface_hub import snapshot_download
from mlx_lm import generate, load as load_target
from mlx_lm.models.cache import make_prompt_cache
from mlx_lm.sample_utils import make_sampler

from dflash_mlx.runtime.loading import load_draft_bundle
from dflash_mlx.engine.target_ops import resolve_target_ops


PROMPTS = [
    "Write a Python function that fetches a URL with retries and a timeout.",
    "Explain how a transformer self-attention head computes attention weights.",
    "Refactor this loop into a list comprehension while keeping semantics.",
    "Write a SQL query joining orders and customers for the last 30 days.",
    "Implement a simple LRU cache in Python with O(1) get and put.",
    "Describe the tradeoffs of synchronous vs asynchronous I/O in web servers.",
    "Write a bash script that backs up a directory to a remote host.",
    "Explain the difference between a process and a thread with examples.",
    "Write a Python class for a bounded thread-safe queue.",
    "Show how to parse a CSV file streaming line by line in Python.",
    "Write a function to detect cycles in a directed graph.",
    "Explain how HTTP/2 multiplexing reduces head-of-line blocking.",
    "Write a Python snippet using asyncio to run three tasks concurrently.",
    "Describe how to profile and optimize a slow Python code path.",
    "Write a regex to match ISO 8601 timestamps.",
    "Implement binary search and explain its complexity.",
    "Write a small web server in Python using only the standard library.",
    "Explain the CAP theorem with a concrete distributed system example.",
    "Write a function that pretty-prints a nested dict with indentation.",
    "Describe how a database index speeds up lookups.",
    "Write a Python decorator that logs function call duration.",
    "Implement merge sort and analyze its worst-case behavior.",
    "Write a SQL query for the top 10 products by revenue this quarter.",
    "Explain how garbage collection works in Python (refcounting + generational).",
    "Write a function that finds the longest increasing subsequence.",
    "Describe the steps to debug a memory leak in a long-running service.",
]


def load_draft(draft_id):
    model, _info = load_draft_bundle(draft_id, lazy=False, draft_quant=None)
    return model


def target_features(ops, model, tokens, layer_ids):
    cache = make_prompt_cache(model)
    _, captured = ops.forward_with_hidden_capture(
        model,
        input_ids=mx.array(tokens, dtype=mx.int32)[None],
        cache=cache,
        capture_layer_ids={layer_id + 1 for layer_id in layer_ids},
        compute_logits=False,
    )
    hidden = ops.extract_context_feature(captured, layer_ids)
    hidden = mx.stop_gradient(hidden.astype(mx.bfloat16))[0]
    mx.eval(hidden)
    return hidden


def token_list(value):
    if isinstance(value, list):
        return [int(token) for token in value]
    raise TypeError(f"expected list of token ids, got {type(value)}")


def build_prompt_ids(tokenizer, instruction):
    return token_list(tokenizer.apply_chat_template(
        [{"role": "user", "content": instruction}],
        tokenize=True,
        add_generation_prompt=True,
    ))


def prepare_data(args):
    random.seed(args.seed)
    mx.random.seed(args.seed)
    args.output.mkdir(parents=True, exist_ok=True)
    samples_dir = args.output / "samples"
    samples_dir.mkdir(exist_ok=True)

    model, tokenizer = load_target(args.target)
    draft = load_draft(args.draft)
    ops = resolve_target_ops(model)
    target_layer_ids = draft.target_layer_ids
    model.eval()
    sampler = make_sampler(temp=0.6, top_p=0.95, top_k=20)

    total = args.samples + args.eval_samples
    manifest = []
    started = time.perf_counter()

    for index in range(total):
        instruction = PROMPTS[index % len(PROMPTS)]
        prompt_ids = build_prompt_ids(tokenizer, instruction)
        response = generate(
            model,
            tokenizer,
            prompt=prompt_ids,
            max_tokens=args.max_new_tokens,
            sampler=sampler,
            verbose=False,
        )
        response_ids = token_list(tokenizer.encode(response, add_special_tokens=False))
        tokens = (prompt_ids + response_ids)[: args.max_sequence_tokens]
        if len(tokens) < len(prompt_ids) + args.block_size:
            print("  (short response, skipped)", flush=True)
            continue
        hidden = target_features(ops, model, tokens, target_layer_ids)
        sample_name = f"{len(manifest):05d}.safetensors"
        mx.save_safetensors(str(samples_dir / sample_name), {
            "tokens": mx.array(tokens, dtype=mx.int32),
            "hidden": hidden,
            "prompt_length": mx.array([len(prompt_ids)], dtype=mx.int32),
        })
        split = "eval" if len(manifest) < args.eval_samples else "train"
        manifest.append({
            "file": sample_name,
            "split": split,
            "instruction": instruction,
            "response": response,
            "tokens": len(tokens),
            "prompt_tokens": len(prompt_ids),
        })
        elapsed = time.perf_counter() - started
        print(f"[{len(manifest):3d}/{total}] {split:5s} tokens={len(tokens):3d} "
              f"elapsed={elapsed / 60:.1f}m", flush=True)
        mx.clear_cache()
        if len(manifest) >= total:
            break

    if len(manifest) < total:
        raise RuntimeError(f"prepared {len(manifest)} samples, expected {total}")
    with (args.output / "manifest.jsonl").open("w", encoding="utf-8") as out:
        for row in manifest:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    (args.output / "config.json").write_text(
        json.dumps({
            "target": args.target,
            "draft": args.draft,
            "seed": args.seed,
            "samples": args.samples,
            "eval_samples": args.eval_samples,
            "block_size": args.block_size,
            "target_layer_ids": target_layer_ids,
        }, indent=2) + "\n"
    )


def read_manifest(data_dir, split):
    rows = []
    with (data_dir / "manifest.jsonl").open(encoding="utf-8") as src:
        for line in src:
            row = json.loads(line)
            if row["split"] == split:
                rows.append(row)
    if not rows:
        raise RuntimeError(f"no {split} samples in {data_dir}")
    return rows


def load_sample(data_dir, row):
    return mx.load(str(data_dir / "samples" / row["file"]))


def choose_anchor(sample, block_size, rng):
    tokens = sample["tokens"]
    prompt_length = int(sample["prompt_length"][0].item())
    last = int(tokens.shape[0]) - block_size
    if last < prompt_length:
        raise RuntimeError("sample response is shorter than the draft block")
    return rng.randint(prompt_length, last)


def bind_and_freeze(draft, target, ops, scope="all"):
    draft.bind_target_model(target, target_ops=ops)
    draft.freeze()
    draft.fc.unfreeze()
    draft.hidden_norm.unfreeze()
    if scope == "all":
        for layer in draft.layers:
            layer.unfreeze()
        draft.norm.unfreeze()
    draft.train()
    target.eval()


def loss_fn(draft, ops, target, sample, anchor, block_size, loss_gamma):
    tokens = sample["tokens"]
    target_hidden = sample["hidden"][None, :anchor]
    block_tokens = mx.concatenate((
        tokens[anchor:anchor + 1],
        mx.full((block_size - 1,), draft.mask_token_id, dtype=mx.int32),
    ))[None]
    noise_embedding = ops.embed_tokens(target)(block_tokens).astype(mx.bfloat16)
    draft_context = draft.project_target_hidden(target_hidden.astype(mx.bfloat16))
    draft_hidden = draft.forward_projected_context(
        noise_embedding=noise_embedding,
        draft_context=draft_context,
        cache=None,
    )
    logits = ops.logits_from_hidden(target, draft_hidden[:, 1:, :])
    labels = tokens[anchor + 1:anchor + block_size][None]
    losses = nn.losses.cross_entropy(logits, labels, reduction="none")
    positions = mx.arange(block_size - 1, dtype=mx.float32)
    weights = mx.exp(-positions / loss_gamma)
    return mx.sum(losses * weights[None]) / mx.sum(weights)


def acceptance_for_sample(draft, ops, target, sample, anchor, block_size):
    tokens = sample["tokens"]
    target_hidden = sample["hidden"][None, :anchor]
    block_tokens = mx.concatenate((
        tokens[anchor:anchor + 1],
        mx.full((block_size - 1,), draft.mask_token_id, dtype=mx.int32),
    ))[None]
    noise_embedding = ops.embed_tokens(target)(block_tokens).astype(mx.bfloat16)
    draft_context = draft.project_target_hidden(target_hidden.astype(mx.bfloat16))
    draft_hidden = draft.forward_projected_context(
        noise_embedding=noise_embedding,
        draft_context=draft_context,
        cache=None,
    )
    logits = ops.logits_from_hidden(target, draft_hidden[:, 1:, :])
    predictions = mx.argmax(logits, axis=-1)[0]
    expected = tokens[anchor + 1:anchor + block_size]
    accepted = 0
    for match in mx.equal(predictions, expected).tolist():
        if not match:
            break
        accepted += 1
    return accepted


def evaluate_draft(draft, ops, target, data_dir, rows, block_size, anchors, seed):
    rng = random.Random(seed)
    draft.eval()
    accepted = []
    exact = 0
    for index in range(anchors):
        row = rows[index % len(rows)]
        sample = load_sample(data_dir, row)
        anchor = choose_anchor(sample, block_size, rng)
        count = acceptance_for_sample(draft, ops, target, sample, anchor, block_size)
        accepted.append(count)
        exact += count == block_size - 1
        if index % 8 == 7:
            mx.clear_cache()
    draft.train()
    mean = sum(accepted) / len(accepted)
    return {
        "anchors": len(accepted),
        "mean_accepted_drafts": mean,
        "mean_cycle_tokens": mean + 1.0,
        "full_blocks": exact,
        "full_block_rate": exact / len(accepted),
    }


def draft_source_path(draft_id):
    candidate = Path(draft_id)
    if candidate.is_dir():
        return candidate
    return Path(snapshot_download(draft_id, allow_patterns=["*.safetensors", "*.json"]))


def save_draft(draft, draft_id, output, metrics, training):
    output.mkdir(parents=True, exist_ok=True)
    source = draft_source_path(draft_id)
    original = {
        key
        for file in source.glob("*.safetensors")
        for key in mx.load(str(file)).keys()
    }
    parameters = dict(tree_flatten(draft.parameters()))
    missing = sorted(original - parameters.keys())
    if missing:
        raise RuntimeError(f"trained model is missing original weights: {missing}")
    weights = {key: parameters[key] for key in sorted(original)}
    mx.eval(weights)
    mx.save_safetensors(str(output / "model.safetensors"), weights)
    shutil.copy2(source / "config.json", output / "config.json")
    (output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (output / "training.json").write_text(json.dumps(training, indent=2) + "\n")




def json_safe_args(args):
    values = vars(args).copy()
    for key, value in values.items():
        if isinstance(value, Path):
            values[key] = str(value)
    return values


def learning_rate(step, total, peak, warmup_ratio):
    warmup = max(1, round(total * warmup_ratio))
    if step < warmup:
        return peak * (step + 1) / warmup
    progress = (step - warmup) / max(1, total - warmup)
    return peak * 0.5 * (1.0 + math.cos(math.pi * progress))


def train_draft(args):
    rng = random.Random(args.seed)
    mx.random.seed(args.seed)
    train_rows = read_manifest(args.data, "train")
    eval_rows = read_manifest(args.data, "eval")
    target, _ = load_target(args.target)
    ops = resolve_target_ops(target)
    draft = load_draft(args.draft)
    bind_and_freeze(draft, target, ops, args.train_scope)

    optimizer = optim.AdamW(
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
    )
    loss_and_grad = nn.value_and_grad(draft, loss_fn)
    baseline = evaluate_draft(
        draft, ops, target, args.data, eval_rows, args.block_size,
        min(64, len(eval_rows) * 4), args.seed,
    )
    print(f"baseline {json.dumps(baseline, sort_keys=True)}", flush=True)
    best = baseline
    started = time.perf_counter()

    for step in range(args.steps):
        row = train_rows[step % len(train_rows)]
        if step and step % len(train_rows) == 0:
            rng.shuffle(train_rows)
        sample = load_sample(args.data, row)
        anchor = choose_anchor(sample, args.block_size, rng)
        rate = learning_rate(step, args.steps, args.learning_rate, args.warmup_ratio)
        optimizer.learning_rate = mx.array(rate)
        loss, grads = loss_and_grad(
            draft, ops, target, sample, anchor, args.block_size, args.loss_gamma
        )
        grads, grad_norm = optim.clip_grad_norm(grads, args.clip_grad)
        optimizer.update(draft, grads)
        mx.eval(loss, grad_norm, draft.parameters(), optimizer.state)

        if step == 0 or (step + 1) % 8 == 0:
            elapsed = time.perf_counter() - started
            print(f"step={step + 1}/{args.steps} loss={loss.item():.5f} "
                  f"grad={grad_norm.item():.3f} lr={rate:.3e} "
                  f"steps_s={(step + 1) / elapsed:.3f}", flush=True)
        if (step + 1) % args.eval_every == 0 or step + 1 == args.steps:
            metrics = evaluate_draft(
                draft, ops, target, args.data, eval_rows, args.block_size,
                min(64, len(eval_rows) * 4), args.seed,
            )
            print(f"eval step={step + 1} {json.dumps(metrics, sort_keys=True)}", flush=True)
            if metrics["mean_accepted_drafts"] > best["mean_accepted_drafts"]:
                best = metrics
                save_draft(draft, args.draft, args.output / "best", metrics, json_safe_args(args))
        if (step + 1) % args.save_every == 0:
            save_draft(
                draft, args.draft, args.output / f"step-{step + 1:06d}",
                metrics if "metrics" in locals() else baseline, json_safe_args(args),
            )
        mx.clear_cache()

    final_metrics = evaluate_draft(
        draft, ops, target, args.data, eval_rows, args.block_size,
        min(64, len(eval_rows) * 4), args.seed,
    )
    save_draft(draft, args.draft, args.output / "final", final_metrics, json_safe_args(args))
    summary = {"baseline": baseline, "best": best, "final": final_metrics}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)


def evaluate_command(args):
    rows = read_manifest(args.data, "eval")
    target, _ = load_target(args.target)
    ops = resolve_target_ops(target)
    draft = load_draft(args.draft)
    bind_and_freeze(draft, target, ops)
    metrics = evaluate_draft(
        draft, ops, target, args.data, rows, args.block_size, args.anchors, args.seed
    )
    print(json.dumps(metrics, indent=2))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--target", default="ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit")
    p.add_argument("--draft", default="z-lab/Qwen3.6-35B-A3B-DFlash")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--samples", type=int, default=128)
    p.add_argument("--eval-samples", type=int, default=16)
    p.add_argument("--max-new-tokens", type=int, default=192)
    p.add_argument("--max-sequence-tokens", type=int, default=384)
    p.add_argument("--block-size", type=int, default=16)
    p.add_argument("--seed", type=int, default=20260821)

    p = sub.add_parser("train")
    p.add_argument("--target", default="ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit")
    p.add_argument("--draft", default="z-lab/Qwen3.6-35B-A3B-DFlash")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--steps", type=int, default=768)
    p.add_argument("--block-size", type=int, default=16)
    p.add_argument("--learning-rate", type=float, default=2e-5)
    p.add_argument("--weight-decay", type=float, default=0.01)
    p.add_argument("--warmup-ratio", type=float, default=0.04)
    p.add_argument("--clip-grad", type=float, default=1.0)
    p.add_argument("--loss-gamma", type=float, default=4.0)
    p.add_argument("--eval-every", type=int, default=96)
    p.add_argument("--save-every", type=int, default=192)
    p.add_argument("--train-scope", choices=("all", "projection"), default="all")
    p.add_argument("--seed", type=int, default=20260821)

    p = sub.add_parser("evaluate")
    p.add_argument("--target", default="ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit")
    p.add_argument("--draft", required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--block-size", type=int, default=16)
    p.add_argument("--anchors", type=int, default=64)
    p.add_argument("--seed", type=int, default=20260821)

    args = parser.parse_args()
    if args.command == "prepare":
        prepare_data(args)
    elif args.command == "train":
        train_draft(args)
    else:
        evaluate_command(args)


if __name__ == "__main__":
    main()