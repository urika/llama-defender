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


# Map of stdlib symbol patterns to required import
_STDLIB_IMPORT_MAP = {
    r'\bjson\.': 'import json',
    r'\bos\.path\.|\bos\.makedirs|\bos\.chmod|\bos\.environ|\bos\.listdir|\bos\.remove|\bos\.urandom': 'import os',
    r'\bre\.findall|\bre\.sub|\bre\.search|\bre\.compile|\bre\.match|\bre\.finditer': 'import re',
    r'\bsubprocess\.': 'import subprocess',
    r'\bdatetime\.': 'from datetime import datetime',
    r'\btime\.': 'import time',
    r'\bhashlib\.': 'import hashlib',
    r'\bthreading\.(?!Semaphore)': 'import threading',
    r'\bcollections\.': 'import collections',
}


def detect_stdlib_imports(code_text):
    """Detect which stdlib modules are used and return import lines."""
    imports = []
    for pattern, import_line in _STDLIB_IMPORT_MAP.items():
        if re.search(pattern, code_text):
            imports.append(import_line)
    return imports


def find_refs(code_lines):
    code = '\n'.join(code_lines)
    cleaned = re.sub(r'"[^"]*"', '""', code)
    cleaned = re.sub(r"'[^']*'", "''", cleaned)
    cleaned = re.sub(r'#.*$', '', cleaned, flags=re.MULTILINE)
    identifiers = set(re.findall(r'\b([A-Z][A-Z0-9_]*|[a-z_][_a-zA-Z0-9]*)\b', cleaned))
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
    lines = [f'"""Auto-extracted {name} module."""']
    # Auto-detect stdlib imports
    code_text = '\n'.join(result['code'])
    stdlib_imports = detect_stdlib_imports(code_text)
    lines.extend(stdlib_imports)
    lines.append('import proxy_state as _ps')
    # Add delegates for external functions (only for actual functions in proxy)
    import keyword
    proxy_funcs = set(parse_funcs('anthropic_proxy.py').keys()) | {'log'}
    real_external = {e for e in result['external']
                     if e in proxy_funcs and e not in ('_estimate_message_chars',)}
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
            if ref in line and f'_ps.{ref}' not in line and not line.lstrip().startswith('#'):
                line = line.replace(ref, f'_ps.{ref}')
        if result['log_refs'] and 'log(' in line and 'def _log' not in line and not line.lstrip().startswith('#'):
            line = line.replace('log(', '_log(')
        lines.append(line)
    lines += ['',f'__all__ = ['] + [f'    "{n}",' for n in result['ranges']] + [']']
    return '\n'.join(lines)+'\n'


def fix_tests(vars_list):
    """Add proxy_state patches alongside existing proxy patches (dual strategy)."""
    import re
    for tf in ['test/unit/test_proxy_fallback.py','test/unit/test_proxy_reload.py',
               'test/unit/test_text_loop.py','test/unit/test_payload_limit.py']:
        path = os.path.join(REPO_ROOT, tf)
        if not os.path.exists(path): continue
        with open(path) as f: c = f.read()
        changed = False
        for v in vars_list:
            if f'patch.object(proxy_state, "{v}"' in c: continue  # idempotent
            c = re.sub(rf'(\s*)with patch\.object\(proxy, "{v}", ([^)]+)\):',
                       rf'\1with patch.object(proxy, "{v}", \2), patch.object(proxy_state, "{v}", \2):', c)
            def _dual(m):
                return f'{m.group(0)}, patch.object(proxy_state, "{v}", {m.group(1)})'
            c = re.sub(rf'patch\.object\(proxy, "{v}", ([^)]+)\)', _dual, c)
            c = re.sub(rf'^(\s*)(proxy)\.({v})\s*=\s*(.+)$',
                       rf'\1\2.\3 = \4\n\1proxy_state.\3 = \4', c, flags=re.MULTILINE)
            changed = True
        if changed:
            with open(path,'w') as f: f.write(c)
            print(f"Updated {tf}")


def apply_extraction(result, name, proxy_path='anthropic_proxy.py'):
    """Generate module file and modify anthropic_proxy.py."""
    mc = gen_module(name, result)
    # Add special imports for known cross-module dependencies
    if '_estimate_message_chars' in result.get('external', set()):
        mc = mc.replace('import proxy_state as _ps',
                        'import proxy_state as _ps\nfrom message_converter import _estimate_message_chars')
    if '_classify_content_for_ratio' in result.get('external', set()):
        mc = mc.replace('import proxy_state as _ps',
                        'import proxy_state as _ps\nfrom message_converter import _classify_content_for_ratio')
    with open(f'{name}.py','w') as f: f.write(mc)
    with open(proxy_path) as f: cur = f.readlines()
    ss = [x[0] for x in result['ranges'].values()]
    ee = [x[1] for x in result['ranges'].values()]
    mn, mx = min(ss), max(ee)
    # Wire delegate at end before main()
    delegate_lines = []
    if result.get('log_refs'): delegate_lines.append(f'{name}._log = log')
    proxy_funcs = set(parse_funcs('anthropic_proxy.py').keys()) | {'log'}
    for ext in sorted(result.get('external',set())):
        if ext in proxy_funcs and ext not in ('_estimate_message_chars',):
            delegate_lines.append(f'{name}.{ext} = {ext}')
    if delegate_lines:
        for i, line in enumerate(cur):
            if 'def main():' in line:
                for dl in reversed(delegate_lines): cur.insert(i, dl + '\n')
                if delegate_lines: cur.insert(i, '\n')
                break
    # Check if segments are contiguous
    ranges_list = sorted(result['ranges'].values(), key=lambda x: x[0])
    contiguous = True
    for i in range(len(ranges_list) - 1):
        if ranges_list[i + 1][0] > ranges_list[i][1] + 2:
            contiguous = False
            break

    pre_lines = [f'import {name}\n', f'from {name} import *\n']
    if name == 'lifecycle':
        pre_lines += ['import content_compressor\n', 'from content_compressor import *\n']
    pre_lines.append('\n')

    if contiguous:
        cur = cur[:mn-1] + pre_lines + cur[mx:]
    else:
        # Non-contiguous: remove each segment individually (highest first)
        for s, e in reversed(ranges_list):
            cur = cur[:s-1] + [f'from {name} import *\n'] + cur[e:]
        # Add the 'import' line at the first segment
        first_s = ranges_list[0][0]
        cur.insert(first_s - 1, f'import {name}\n')
    with open(proxy_path,'w') as f: f.writelines(cur)
    print(f'{name}.py: {len(mc.splitlines())}l, proxy: {len(cur)}l')


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
