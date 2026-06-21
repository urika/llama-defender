#!/usr/bin/env python3
"""
Automated module extraction from anthropic_proxy.py.

Usage:
    python3 tools/extract_module.py <module_name> func1 func2 ...

Example:
    python3 tools/extract_module.py lifecycle _normalize_system_messages _apply_cache_aligner _classify_lifecycle_stage

Also:
    python3 tools/extract_module.py --fix-tests VAR1 VAR2 ...  # update test patches
"""
import ast, os, re, sys
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)

STDLIB_NAMES = set(dir(__builtins__)) | {
    'json','re','os','sys','hashlib','threading','collections',
    'urllib','datetime','signal','subprocess','time',
    'BaseHTTPRequestHandler','ThreadingHTTPServer',
}
KEYWORDS = {'True','False','None','and','or','not','if','else','elif',
    'for','while','in','is','with','as','try','except','finally',
    'def','class','return','import','from','global','nonlocal',
    'pass','break','continue','raise','yield','lambda'}


def get_ps_names():
    import proxy_state
    return set(proxy_state.__all__)


def parse_funcs(filepath):
    with open(filepath) as f: source = f.read()
    tree = ast.parse(source)
    funcs = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            end = _find_end(source, node.end_lineno)
            funcs[node.name] = (node.lineno, end)
    return funcs


def _find_end(source, start):
    lines = source.split('\n')
    i = start - 1
    while i < len(lines) and not lines[i].strip(): i += 1
    return i + 1 if i > start else start


def find_refs(code_lines):
    code = '\n'.join(code_lines)
    cleaned = re.sub(r'"[^"]*"', '""', code)
    cleaned = re.sub(r"'[^']*'", "''", cleaned)
    cleaned = re.sub(r'#.*$', '', cleaned, flags=re.MULTILINE)
    identifiers = set(re.findall(r'\b([A-Z][A-Z0-9_]*|[a-z_][a-z0-9_]*)\b', cleaned))
    return identifiers - KEYWORDS - STDLIB_NAMES


def analyze(target_funcs, proxy_path='anthropic_proxy.py'):
    all_funcs = parse_funcs(proxy_path)
    ps_names = get_ps_names()
    ranges = {n: all_funcs[n] for n in target_funcs if n in all_funcs}
    if not ranges: return None
    ranges = dict(sorted(ranges.items(), key=lambda x: x[1][0]))
    with open(proxy_path) as f: src = f.readlines()
    code_lines = []
    for name, (s, e) in ranges.items():
        code_lines.append(f"# --- {name} ---")
        code_lines.extend(src[s-1:e])
    all_refs = find_refs(code_lines)
    # Filter external: only function calls (identifier followed by '(')
    code_text = '\n'.join(code_lines)
    func_calls = set(re.findall(r'\b([a-z_][a-z0-9_]*)\s*\(', code_text))
    ps_refs = all_refs & ps_names
    internal = func_calls & set(target_funcs)
    external = (func_calls - ps_names - set(target_funcs) - STDLIB_NAMES
                - {'log','print','int','float','str','len','min','max','list','dict',
                   'set','tuple','isinstance','getattr','setattr','hasattr','any','all',
                   'sorted','enumerate','zip','range','round','abs','sum','next','iter',
                   'open','type','super','repr','id','hash'})
    log_refs = 'log' in func_calls
    test_vars = {v for v in ps_refs if v.startswith('PROXY_') or v == 'MODEL_NAME'}
    return {'ranges':ranges,'all_refs':all_refs,'ps_refs':ps_refs,'internal':internal,
            'external':external,'log_refs':log_refs,'code':code_lines,'test_vars':test_vars}


def gen_module(name, result):
    lines = [f'"""Auto-extracted {name} module."""','import proxy_state as _ps']
    # Add delegates for external functions (filter keywords and builtins)
    import keyword
    real_external = {e for e in result['external']
                     if not keyword.iskeyword(e) and e not in STDLIB_NAMES
                     and e not in ('_estimate_message_chars',)}  # handled via import
    if real_external:
        lines.append('# External function delegates — set by anthropic_proxy after import')
        for ext in sorted(real_external):
            lines.append(f'{ext} = None  # delegate')
    if result['log_refs']:
        lines += ['','def _log(msg, level="INFO"):','    pass']
    lines.append('')
    for cl in result['code']:
        line = cl.rstrip('\n')
        for ref in sorted(result['ps_refs'], key=len, reverse=True):
            if ref in line and '_ps.' not in line and not line.lstrip().startswith('#'):
                line = line.replace(ref, f'_ps.{ref}')
        if result['log_refs'] and 'log(' in line and 'def _log' not in line and not line.lstrip().startswith('#'):
            line = line.replace('log(', '_log(')
        lines.append(line)
    lines += ['',f'__all__ = ['] + [f'    "{n}",' for n in result['ranges']] + [']']
    return '\n'.join(lines)+'\n'


def fix_tests(vars_list):
    for tf in ['test/unit/test_proxy_fallback.py','test/unit/test_proxy_reload.py',
               'test/unit/test_text_loop.py','test/unit/test_payload_limit.py']:
        path = os.path.join(REPO_ROOT, tf)
        if not os.path.exists(path): continue
        with open(path) as f: c = f.read()
        changed = False
        for v in vars_list:
            # Pattern 1: patch.object(proxy, "VAR" → patch.object(proxy_state, "VAR"
            old = f'patch.object(proxy, "{v}"'
            nu = f'patch.object(proxy_state, "{v}"'
            if old in c: c = c.replace(old, nu); changed = True
            # Pattern 2: proxy.VAR → proxy_state.VAR (direct attribute access)
            old2 = f'proxy.{v}'
            nu2 = f'proxy_state.{v}'
            if old2 in c: c = c.replace(old2, nu2); changed = True
        if changed:
            with open(path,'w') as f: f.write(c)
            print(f"Updated {tf}")


if __name__ == '__main__':
    if len(sys.argv) < 2: print(__doc__); sys.exit(1)
    if sys.argv[1] == '--fix-tests':
        if len(sys.argv) < 3: print("Usage: extract_module.py --fix-tests VAR1 VAR2 ..."); sys.exit(1)
        fix_tests(sys.argv[2:]); sys.exit(0)
    name, funcs = sys.argv[1], sys.argv[2:]
    if not funcs:
        af = parse_funcs('anthropic_proxy.py')
        print(f"Functions: {len(af)}, proxy_state names: {len(get_ps_names())}")
        sys.exit(0)
    print(f"Analyzing: {name} <- {funcs}")
    r = analyze(funcs)
    if not r: print("No functions found"); sys.exit(1)
    for n,(s,e) in r['ranges'].items(): print(f"  {n}: {s}-{e} ({e-s+1} lines)")
    print(f"\nproxy_state refs ({len(r['ps_refs'])}): {sorted(r['ps_refs'])}")
    print(f"External refs ({len(r['external'])}): {sorted(r['external'])}")
    print(f"Log refs: {r['log_refs']}")
    mf = f"{name}.py"
    with open(mf,'w') as f: f.write(gen_module(name, r))
    print(f"\nGenerated {mf} ({len(gen_module(name,r).splitlines())} lines)")
    all_starts = [x[0] for x in r['ranges'].values()]
    all_ends = [x[1] for x in r['ranges'].values()]
    import_line = f'from {name} import *'
    if r['log_refs']: import_line += f'\n{name}._log = log  # wire at end of file'
    print(f"\nReplace lines {min(all_starts)}-{max(all_ends)} with:")
    print(f"  {import_line}")
    print(f"\nTest vars to fix ({len(r['test_vars'])}):")
    print(f"  python3 tools/extract_module.py --fix-tests {' '.join(sorted(r['test_vars']))}")
