#!/usr/bin/env python3
"""Generate behavior snapshot for regression testing.

Usage:
    python3 tools/gen_behavior_snapshots.py                                           # gen from default source
    python3 tools/gen_behavior_snapshots.py --input path/to/sources --output path/to/snapshot
    python3 tools/gen_behavior_snapshots.py --verify                                    # exit 1 on mismatch

This must be run BEFORE refactoring starts, to capture the baseline input->output
behavior of core functions. The snapshot is read by test/unit/test_behavior_snapshot.py
on every commit to detect unintended behavioral drift.

Source file format:
    {
        "<func_name>": {
            "description": "what this tests",
            "cases": [{"note": "...", "input": ..., ...}, ...]
        }
    }

Output format:
    {
        "<func_name>": [
            {"input": ..., "expected": ..., "note": "..."},
            ...
        ]
    }
"""
import argparse
import importlib
import json
import os
import sys
from contextlib import contextmanager

# Add repo root to path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _build_exception(exc_name, msg):
    exc_map = {
        "TimeoutError": TimeoutError,
        "ConnectionRefusedError": ConnectionRefusedError,
        "ConnectionError": ConnectionError,
        "BrokenPipeError": BrokenPipeError,
        "ValueError": ValueError,
        "KeyError": KeyError,
        "RuntimeError": RuntimeError,
    }
    cls = exc_map.get(exc_name, RuntimeError)
    try:
        return cls(msg)
    except TypeError:
        return cls(msg)


@contextmanager
def _env(key, value):
    old = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if old is None:
            del os.environ[key]
        else:
            os.environ[key] = old


def _import_proxy():
    """Import anthropic_proxy with controlled env."""
    import anthropic_proxy as p
    return p


def _run_case(p, func_name, case):
    fn = getattr(p, func_name, None)
    if fn is None:
        raise ValueError(f"Function {func_name} not found")

    if func_name == "_classify_exception":
        exc = _build_exception(case["exc"], case["msg"])
        status, err_type, retry = fn(exc)
        return {"status": status, "error_type": err_type, "retryable": retry}

    if func_name == "parse_tool_arguments":
        return fn(case["input"], case.get("hint", ""))

    if func_name == "_compute_text_similarity":
        return fn(case["text1"], case["text2"])

    if func_name == "_detect_blocker_pattern":
        with _env("PROXY_BLOCKER_ENABLED", "true"):
            importlib.reload(p)
            fn = getattr(p, func_name)
            result = fn(case["input"])
            if isinstance(result, dict):
                result.pop("reason", None)
            return result

    # Default: direct call with 'input' field
    return fn(case["input"])


def generate(source_path, output_path):
    p = _import_proxy()
    with open(source_path) as f:
        sources = json.load(f)

    output = {}
    for func_name, config in sources.items():
        if not isinstance(config, dict) or "cases" not in config:
            continue
        results = []
        for case in config["cases"]:
            try:
                expected = _run_case(p, func_name, case)
                # Store the full case context for accurate verification
                results.append({
                    "input": case,
                    "expected": expected,
                    "note": case.get("note", ""),
                })
            except Exception as exc:
                print(f"  [SKIP] {func_name} / {case.get('note', '?')}: {exc}")
        if results:
            output[func_name] = results
        print(f"  {func_name}: {len(results)}/{len(config['cases'])} cases")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nOutput: {output_path}")


def verify(output_path):
    p = _import_proxy()
    with open(output_path) as f:
        snapshot = json.load(f)

    errors = 0
    total = 0
    for func_name, cases in snapshot.items():
        if not isinstance(cases, list):
            continue
        for entry in cases:
            total += 1
            case = entry.get("input", entry)
            if not isinstance(case, dict):
                continue
            try:
                if func_name == "_classify_exception":
                    exc = _build_exception(case.get("exc", ""), case.get("msg", ""))
                    result = getattr(p, func_name)(exc)
                    actual = {"status": result[0], "error_type": result[1], "retryable": result[2]}
                elif func_name == "parse_tool_arguments":
                    actual = getattr(p, func_name)(case.get("input", ""), case.get("hint", ""))
                elif func_name == "_compute_text_similarity":
                    actual = getattr(p, func_name)(case.get("text1", ""), case.get("text2", ""))
                elif func_name == "_detect_blocker_pattern":
                    with _env("PROXY_BLOCKER_ENABLED", "true"):
                        importlib.reload(p)
                        fn = getattr(p, func_name)
                        result = fn(case.get("input", []))
                        actual = {k: v for k, v in result.items() if k != "reason"}
                elif func_name == "convert_anthropic_tool_choice_to_openai":
                    actual = getattr(p, func_name)(case.get("input", {}))
                elif func_name == "convert_anthropic_tools_to_openai":
                    actual = getattr(p, func_name)(case.get("input", []))
                elif func_name == "clear_old_tool_results":
                    actual = getattr(p, func_name)(case.get("input", []), case.get("tools", None))
                else:
                    actual = getattr(p, func_name)(case.get("input", ""))

                # Compare via JSON serialization
                actual_s = json.dumps(actual, sort_keys=True, default=str)
                expected_s = json.dumps(entry.get("expected", {}), sort_keys=True, default=str)
                if actual_s != expected_s:
                    # Tolerance for float similarity
                    if func_name == "_compute_text_similarity":
                        a = actual if isinstance(actual, (int, float)) else 0
                        e_val = entry.get("expected", 0)
                        e = e_val if isinstance(e_val, (int, float)) else 0
                        if abs(a - e) < 0.01:
                            continue
                    note = entry.get("note", "")
                    print(f"  [FAIL] {func_name} / {note}: expected={expected_s[:80]}, actual={actual_s[:80]}")
                    errors += 1
            except Exception as exc:
                print(f"  [ERROR] {func_name} / {entry.get('note', '?')}: {exc}")
                errors += 1

    print(f"\nVerified {total} snapshot cases: {errors} failure(s)")
    return errors == 0


def main():
    parser = argparse.ArgumentParser(description="Generate/verify behavior snapshots")
    parser.add_argument("--input", default="test/fixtures/snapshot_sources.json")
    parser.add_argument("--output", default="test/fixtures/behavior_snapshots.json")
    parser.add_argument("--verify", action="store_true", help="Verify against snapshot (exit 1 on mismatch)")
    args = parser.parse_args()

    if not os.path.isabs(args.input):
        args.input = os.path.join(_REPO_ROOT, args.input)
    if not os.path.isabs(args.output):
        args.output = os.path.join(_REPO_ROOT, args.output)

    if args.verify:
        ok = verify(args.output)
        sys.exit(0 if ok else 1)
    else:
        if not os.path.exists(args.input):
            print(f"[ERROR] Source not found: {args.input}")
            sys.exit(1)
        generate(args.input, args.output)


if __name__ == "__main__":
    main()
