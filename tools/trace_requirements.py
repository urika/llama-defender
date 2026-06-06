#!/usr/bin/env python3
"""Trace PRD requirements to code and test anchors.

Reads docs/requirements.yaml (the authoritative source for 23 functional
requirements) and verifies each requirement's `impl` and `tests` anchors
still resolve. Also scans code + tests for any R<id> tags that are NOT
declared in the YAML ("orphans") and reports them.

Anchor formats (newline = OR):

  impl  —  "path/to/file.py:FUNC_NAME"    top-level `def FUNC_NAME`
           "path/to/file.py:ClassName.method"
           "path/to/file.py:CONST_NAME"   top-level `CONST_NAME = ...`

  tests —  "test/.../test_*.py::TestClass"             (whole class)
           "test/.../test_*.py::TestClass::test_method" (single method)
           "test/.../test_*.py::test_function"          (module-level function)
           "test/.../test_*.sh"                         (whole script)
           "test/.../test_*.sh::TC1"                    (bash test name)

Symbol anchors (file:FUNC / file:CONST) are resolved by grepping for the
matching `^def <name>` / `^class <name>` / `^NAME = ` pattern. Unlike
file:LINE anchors, they are stable across unrelated code changes.

Run:
    python3 tools/trace_requirements.py
    python3 tools/trace_requirements.py --markdown docs/requirement-matrix.md
    python3 tools/trace_requirements.py --strict      # exit 1 on any issue
"""
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Set, Tuple

# Paths are anchored at the repo root so the script can be run from anywhere.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAML_PATH = os.path.join(REPO_ROOT, "docs/requirements.yaml")
PROXY_PATH = os.path.join(REPO_ROOT, "anthropic_proxy.py")
TEST_DIRS = [
    os.path.join(REPO_ROOT, "test/unit"),
    os.path.join(REPO_ROOT, "test/integration"),
    os.path.join(REPO_ROOT, "test/e2e"),
]

# ---------------------------------------------------------------------------
# Minimal stdlib-only YAML loader (handles our specific schema)
# ---------------------------------------------------------------------------
def _coerce(s: str):
    s = s.strip()
    if s == "" or s == "~" or s.lower() == "null":
        return None
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [] if not inner else [_coerce(p) for p in inner.split(",")]
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def load_yaml(path: str) -> List[dict]:
    """Parse the small subset of YAML we use: list-of-mappings with 2-space
    indent and scalar values. Comments (#) are stripped; quoted/unquoted
    strings and inline lists ([a, b]) are supported."""
    with open(path) as f:
        raw = f.read()
    lines = []
    for line in raw.splitlines():
        s = line.split("#", 1)[0].rstrip()
        lines.append(s)
    out: List[dict] = []
    current: dict = {}
    item_indent = -1
    in_list = False
    list_key = ""
    list_indent = -1
    for line in lines:
        if not line.strip():
            continue
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        # 1) "- KEY: VALUE" — new list item.
        m = re.match(r"^-\s+(\w+):\s*(.*)$", stripped)
        if m and (item_indent == -1 or indent == item_indent):
            if current:
                out.append(current)
            current = {}
            current[m.group(1)] = _coerce(m.group(2))
            item_indent = indent
            in_list = False
            continue
        # 2) "- VALUE" — list item under a list-typed key.
        m = re.match(r"^-\s+(.*)$", stripped)
        if m and in_list and list_key and indent > list_indent:
            current[list_key].append(_coerce(m.group(1).strip()))
            continue
        # 3) "KEY: VALUE" — nested key.
        m = re.match(r"^(\w+):\s*(.*)$", stripped)
        if m:
            if indent == 0 and not current:
                continue  # top-level wrapper like "requirements:"
            if indent == item_indent:
                continue
            if indent > item_indent:
                key = m.group(1)
                v = m.group(2).strip()
                if v == "":
                    current[key] = []
                    in_list = True
                    list_indent = indent
                    list_key = key
                else:
                    current[key] = _coerce(v)
                    in_list = False
            continue
    if current:
        out.append(current)
    return out


# ---------------------------------------------------------------------------
# Anchor resolution
# ---------------------------------------------------------------------------
def _read(path: str) -> str:
    with open(os.path.join(REPO_ROOT, path) if not os.path.isabs(path) else path) as f:
        return f.read()


def check_impl_anchor(anchor: str) -> Tuple[bool, str]:
    """Resolve a `file:symbol` impl anchor.

    `symbol` may be:
      - a top-level function name → looks for `^def <name>\(`
      - a top-level class name    → looks for `^class <name>[:(]`
      - `ClassName.method`       → looks for `^class ClassName` then
                                    `^\s+def method\(` (any indent)
      - a top-level constant     → looks for `^<NAME>\s*=` at column 0
    """
    if ":" not in anchor:
        return False, f"impl anchor must be 'file:symbol', got: {anchor!r}"
    path_part, symbol = anchor.rsplit(":", 1)
    path = os.path.join(REPO_ROOT, path_part)
    if not os.path.exists(path):
        return False, f"impl file not found: {path_part}"
    if not symbol:
        return False, f"empty symbol in impl anchor: {anchor!r}"

    content = _read(path)

    # ClassName.method form.
    if "." in symbol:
        cls_name, method = symbol.split(".", 1)
        cls_re = re.compile(rf"^class\s+{re.escape(cls_name)}\b", re.MULTILINE)
        m = cls_re.search(content)
        if not m:
            return False, f"{path_part}: class {cls_name!r} not found"
        m_re = re.compile(rf"^\s+def\s+{re.escape(method)}\s*\(", re.MULTILINE)
        if not m_re.search(content):
            return False, f"{path_part}: method {method!r} not in class {cls_name}"
        return True, ""

    # Try top-level def, then class, then constant.
    def_re = re.compile(rf"^def\s+{re.escape(symbol)}\s*\(", re.MULTILINE)
    if def_re.search(content):
        return True, ""
    class_re = re.compile(rf"^class\s+{re.escape(symbol)}\b", re.MULTILINE)
    if class_re.search(content):
        return True, ""
    const_re = re.compile(rf"^{re.escape(symbol)}\s*=", re.MULTILINE)
    if const_re.search(content):
        return True, ""
    return False, (
        f"{path_part}: no `def {symbol}`, `class {symbol}`, or "
        f"`{symbol} = ...` at module top level"
    )


def check_test_anchor(anchor: str) -> Tuple[bool, str]:
    """Resolve a test anchor (see header for accepted forms)."""
    parts = anchor.split("::")
    path_part = parts[0]
    path = os.path.join(REPO_ROOT, path_part)
    if not os.path.exists(path):
        return False, f"file not found: {path_part}"
    if len(parts) == 1:
        return True, ""
    if path_part.endswith(".py"):
        class_name = parts[1] if len(parts) >= 2 else None
        method_name = parts[2] if len(parts) >= 3 else None
        if not class_name:
            return False, f"missing class/function name in {anchor!r}"
        content = _read(path)
        # Class member.
        cls_re = re.compile(rf"^class\s+{re.escape(class_name)}\b", re.MULTILINE)
        if cls_re.search(content):
            if method_name:
                m_re = re.compile(rf"^\s+def\s+{re.escape(method_name)}\b", re.MULTILINE)
                if not m_re.search(content):
                    return False, f"method {method_name!r} not found in class {class_name}"
            return True, ""
        # Module-level function. class_name is actually the function name.
        if not method_name:
            fn_re = re.compile(rf"^def\s+{re.escape(class_name)}\b", re.MULTILINE)
            if fn_re.search(content):
                return True, ""
        return False, f"{class_name!r} not found (neither class nor module-level function) in {path_part}"
    if path_part.endswith(".sh"):
        tc_name = parts[1] if len(parts) >= 2 else None
        if not tc_name:
            return True, ""
        content = _read(path)
        fn_re = re.compile(rf"^(?:function\s+)?{re.escape(tc_name)}\s*\(\)", re.MULTILINE)
        if not fn_re.search(content):
            return False, f"bash test {tc_name!r} not found in {path_part}"
        return True, ""
    return False, f"unrecognized test file extension: {path_part}"


# ---------------------------------------------------------------------------
# Orphan detection: R<id> tags in code/tests not in the YAML
# ---------------------------------------------------------------------------
ID_PATTERN = re.compile(r"R[1-7]\.\d+")

def scan_code_tags() -> Dict[str, List[str]]:
    """Grep anthropic_proxy.py for `# Implements: R<id>` patterns."""
    found: Dict[str, List[str]] = defaultdict(list)
    if not os.path.exists(PROXY_PATH):
        return found
    with open(PROXY_PATH) as f:
        for i, line in enumerate(f, 1):
            m = re.search(r"#\s*Implements:\s*([R\d.,\s]+)", line)
            if m:
                for rid in ID_PATTERN.findall(m.group(1)):
                    found[rid].append(f"anthropic_proxy.py:{i}")
    return found


def scan_test_tags() -> Dict[str, List[str]]:
    """Grep test files for R<id> in class/method docstrings + bash comments."""
    found: Dict[str, List[str]] = defaultdict(list)
    for test_dir in TEST_DIRS:
        if not os.path.isdir(test_dir):
            continue
        for root, _, files in os.walk(test_dir):
            for name in files:
                path = os.path.join(root, name)
                rel = os.path.relpath(path, REPO_ROOT)
                with open(path) as f:
                    for i, line in enumerate(f, 1):
                        if name.endswith(".py"):
                            for rid in ID_PATTERN.findall(line):
                                found[rid].append(f"{rel}:{i}")
                        elif name.endswith(".sh"):
                            if line.lstrip().startswith("#"):
                                for rid in ID_PATTERN.findall(line):
                                    found[rid].append(f"{rel}:{i}")
    return found


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--markdown", help="also write the matrix to this path")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 if any anchor missing or orphan found")
    p.add_argument("--quiet", action="store_true",
                   help="only print summary line + non-zero exit on issues")
    args = p.parse_args()

    if not os.path.exists(YAML_PATH):
        print(f"ERROR: {YAML_PATH} not found", file=sys.stderr)
        return 2

    reqs = load_yaml(YAML_PATH)
    by_id = {r["id"]: r for r in reqs if r.get("id")}

    errors: List[str] = []
    rows = []
    for rid in sorted(by_id.keys(), key=lambda x: (x.split(".")[0], int(x.split(".")[1]))):
        r = by_id[rid]
        impls = r.get("impl") or []
        tests = r.get("tests") or []
        impl_status = "—"
        if impls:
            ok_all = True
            for a in impls:
                ok, why = check_impl_anchor(a)
                if not ok:
                    errors.append(f"  ✗ {rid} impl {a!r}: {why}")
                    ok_all = False
            impl_status = "✓" if ok_all else "✗"
        tests_status = "—"
        if tests:
            ok_all = True
            for a in tests:
                ok, why = check_test_anchor(a)
                if not ok:
                    errors.append(f"  ✗ {rid} test {a!r}: {why}")
                    ok_all = False
            tests_status = "✓" if ok_all else "✗"
        rows.append((rid, r.get("domain", "?"), r.get("title", "?"),
                     r.get("priority", "?"), impl_status, tests_status))

    declared = set(by_id.keys())
    code_tags = scan_code_tags()
    test_tags = scan_test_tags()
    orphan_code = {rid: locs for rid, locs in code_tags.items() if rid not in declared}
    orphan_test = {rid: locs for rid, locs in test_tags.items() if rid not in declared}
    untested = {rid: locs for rid, locs in test_tags.items()
                if rid in declared and (by_id[rid].get("tests") or []) == []}
    untested_with_tags = {rid: locs for rid, locs in untested.items() if locs}

    if orphan_code:
        for rid, locs in orphan_code.items():
            errors.append(f"  ✗ code R-tag {rid} not declared in YAML — at {', '.join(locs)}")
    if orphan_test:
        for rid, locs in orphan_test.items():
            errors.append(f"  ✗ test R-tag {rid} not declared in YAML — at {', '.join(locs)}")
    for rid, locs in untested_with_tags.items():
        print(f"  ℹ {rid}: tests list empty in YAML but R-tag appears in {', '.join(locs)}")

    total = len(rows)
    impl_covered = sum(1 for r in rows if r[4] == "✓")
    test_covered = sum(1 for r in rows if r[5] == "✓")

    if not args.quiet:
        print(f"\n# Requirement Traceability Matrix")
        print(f"\n_Generated from docs/requirements.yaml_\n")
        print(f"| R-ID  | Dom | Title | Pri | Impl | Tests |")
        print(f"|-------|-----|-------|-----|------|-------|")
        for rid, dom, title, pri, impl_s, test_s in rows:
            print(f"| {rid:<5} | {dom:<3} | {title[:36]:<36} | {pri:<3} | {impl_s:<4} | {test_s:<5} |")
        print()
        print(f"**{total} requirements**, {impl_covered} with code anchor, {test_covered} with test coverage.")
        if errors:
            print("\n## Issues")
            for e in errors:
                print(e)
        if not errors:
            print("\nAll anchors resolve. No orphan R-tags found.")

    if args.markdown:
        with open(args.markdown, "w") as f:
            f.write("# Requirement Traceability Matrix\n\n")
            f.write("_Auto-generated by `tools/trace_requirements.py` — do not edit by hand._\n\n")
            f.write("| R-ID | Dom | Title | Pri | Impl | Tests |\n")
            f.write("|------|-----|-------|-----|------|-------|\n")
            for rid, dom, title, pri, impl_s, test_s in rows:
                f.write(f"| {rid} | {dom} | {title} | {pri} | {impl_s} | {test_s} |\n")
            f.write(f"\n**{total} requirements**, {impl_covered} with code anchor, {test_covered} with test coverage.\n")
            if errors:
                f.write("\n## Issues\n")
                for e in errors:
                    f.write(e + "\n")
        if not args.quiet:
            print(f"\nMatrix written to {args.markdown}")

    return 1 if (args.strict and errors) else 0


if __name__ == "__main__":
    sys.exit(main())
