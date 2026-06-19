#!/usr/bin/env python3
"""Generate function signature snapshot for regression testing.

Usage:
    python3 tools/gen_func_signatures.py                         # default output
    python3 tools/gen_func_signatures.py --output path/to/file   # custom path
    python3 tools/gen_func_signatures.py --verify                 # exit 1 on mismatch

This must be run BEFORE refactoring starts, to capture the baseline signatures.
The snapshot is read by test/unit/test_signature_preservation.py on every commit
to detect unintended signature drift.

Output format (JSON):
    {
        "<func_name>": {
            "name": "<func_name>",
            "params": ["arg1", "arg2", ...],
            "defaults": {"arg1": "default_value"}
        },
        ...
    }
"""
import argparse
import inspect
import json
import os
import sys

# Add repo root to path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anthropic_proxy as p


def _build_signature(fn):
    """Extract name, params, and defaults from a callable."""
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        defaults = {}
        for k, v in sig.parameters.items():
            if v.default is not inspect.Parameter.empty:
                if isinstance(v.default, (str, int, float, bool)):
                    defaults[k] = v.default
                elif v.default is None:
                    defaults[k] = None
                else:
                    defaults[k] = repr(v.default)
        return {"name": fn.__name__, "params": params, "defaults": defaults}
    except (ValueError, TypeError):
        return None


def generate(output_path):
    """Scrape all callable functions from anthropic_proxy and write snapshot."""
    snap = {}
    for name in dir(p):
        fn = getattr(p, name)
        if not callable(fn):
            continue
        sig = _build_signature(fn)
        if sig is not None:
            snap[name] = sig

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(snap, f, indent=2, default=str)

    pub = sorted(k for k in snap if not k.startswith("_"))
    priv = sorted(k for k in snap if k.startswith("_") and not k.startswith("__"))
    print(f"Generated {len(snap)} function signatures:")
    print(f"  Public:  {len(pub)}")
    print(f"  Private: {len(priv)}")
    print(f"  Output:  {output_path}")
    return snap


def verify(output_path):
    """Verify that current signatures match the snapshot on disk."""
    with open(output_path) as f:
        snapshot = json.load(f)
    current = {}
    for name in dir(p):
        fn = getattr(p, name)
        if not callable(fn):
            continue
        sig = _build_signature(fn)
        if sig is not None:
            current[name] = sig

    errors = []
    for name, expected in snapshot.items():
        current_sig = current.get(name)
        if current_sig is None:
            errors.append(f"MISSING: function '{name}' was in snapshot but no longer exists")
            continue
        if current_sig["params"] != expected["params"]:
            errors.append(
                f"SIGNATURE CHANGED: {name}\n"
                f"  expected: {name}({', '.join(expected['params'])})\n"
                f"  actual:   {name}({', '.join(current_sig['params'])})"
            )

    added = set(current.keys()) - set(snapshot.keys())
    if added:
        print(f"[INFO] New functions not in snapshot: {sorted(added)}")

    if errors:
        print(f"[FAIL] {len(errors)} signature change(s) detected:")
        for e in errors:
            print(f"  {e}")
        return False
    print(f"[PASS] All {len(snapshot)} function signatures preserved")
    return True


def main():
    parser = argparse.ArgumentParser(description="Generate/verify function signature snapshot")
    parser.add_argument("--output", default="test/fixtures/func_signatures.json",
                        help="Output path for the snapshot JSON")
    parser.add_argument("--verify", action="store_true",
                        help="Verify current signatures match snapshot (exit 1 on mismatch)")
    args = parser.parse_args()

    if not os.path.isabs(args.output):
        args.output = os.path.join(_REPO_ROOT, args.output)

    if args.verify:
        ok = verify(args.output)
        sys.exit(0 if ok else 1)
    else:
        generate(args.output)


if __name__ == "__main__":
    main()
