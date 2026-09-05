#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ctx_recall 离线召回质量评估（不触碰后端模型）。

对 logs/diag/ 下真实会话的 manifest/orig/archive 存量材料，模拟"模型查询"
的四类动作，量化三级穿透检索（锚点直查 / L2 FTS trigram / L1 降级）与
archive 全文恢复的可达性与保真度：

  E1 锚点直查: query=anchor → 应精确命中该登记行（工具描述承诺的契约）
  E2 语义检索: query=head/triggers 关键词 → 目标行是否进 top-limit（recall@k）
  E3 全文恢复: recover_full_content → 原文前缀保真 + 长度/截断统计
  E4 分页续读: offset 窗口与原文逐字对齐

用法:
  python3 tools/eval_ctx_recall.py                # 默认评估内置会话集
  python3 tools/eval_ctx_recall.py --session swedaaac --limit 8

只读磁盘（manifest/index/archive/orig），不写任何生产状态；
_ensure_fts 按需构建的 index/<sid>.db 与代理自身行为一致。
"""

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxy_state as _ps  # noqa: E402
import memory_stores  # noqa: E402
import ctx_recall  # noqa: E402

DEFAULT_SESSIONS = ["swedaaac", "cli_877f", "cli_2566", "swef5653"]


def load_orig(session):
    """orig/<sid>.jsonl → {anchor: content}（压缩寄存原文）。"""
    path = os.path.join(_ps._DIAG_DIR, "orig", session[:8] + ".jsonl")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if d.get("anchor") and d.get("content"):
                out[d["anchor"]] = d["content"]
    return out


def manifest_rows(session):
    """MANIFEST.lines() 为空时回退解析 .prev 轮转文件（活跃代理会把
    旧会话 rotate 成 <sid>.jsonl.prev，磁盘材料不丢）。"""
    rows = memory_stores.MANIFEST.lines(session)
    if rows:
        return rows
    return _load_prev(session)


_PREV_CACHE = {}


def _load_prev(session):
    if session in _PREV_CACHE:
        return _PREV_CACHE[session]
    path = memory_stores.MANIFEST._path(session) + ".prev"
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    _PREV_CACHE[session] = out
    return out


def install_prev_fallback():
    """给 MANIFEST.lines 挂 .prev 回退，让 lookup/fts_search/recover 等
    检索函数对已轮转会话也能看到磁盘存量（仅本评估进程内生效）。"""
    _orig_lines = memory_stores.MANIFEST.lines

    def _lines_with_prev(session_key, limit=None):
        rows = _orig_lines(session_key, limit)
        if rows:
            return rows
        return _load_prev(session_key)

    memory_stores.MANIFEST.lines = _lines_with_prev


def eval_anchor_direct(session, rows, limit):
    """E1: r: 锚点直查命中率（契约: 锚点可精确取回）。"""
    r_rows = [r for r in rows if str(r.get("anchor", "")).startswith("r:")]
    hit = 0
    for r in r_rows:
        got = ctx_recall.lookup(session, r["anchor"], limit=limit)
        if any(g.get("anchor") == r["anchor"] for g in got):
            hit += 1
    return len(r_rows), hit


def eval_semantic(session, rows, limit, trials_per_row=1, seed=7):
    """E2: 用 head/triggers 派生的"模型式"查询测 recall@limit。

    查询构造模拟两类真实行为:
      a) 路径/句柄式: handle.value 或 head 前 40 字符（模型复述路径原文）
      b) 关键词式: triggers 前 3 个词（模型记关键词重查）
    """
    rnd = random.Random(seed)
    r_rows = [r for r in rows if str(r.get("anchor", "")).startswith("r:")
              and (r.get("head") or r.get("triggers"))]
    n = hit_a = hit_b = 0
    for r in r_rows:
        n += 1
        qa = (r.get("handle") or {}).get("value") if isinstance(
            r.get("handle"), dict) else None
        qa = qa or (r.get("head") or "")[:40].strip()
        if len(qa) >= 3:
            got = ctx_recall.lookup(session, qa, limit=limit)
            if any(g.get("anchor") == r["anchor"] for g in got):
                hit_a += 1
        toks = [t for t in (r.get("triggers") or "").split() if len(t) >= 2]
        rnd.shuffle(toks)
        qb = " ".join(toks[:3])
        if len(qb) >= 3:
            got = ctx_recall.lookup(session, qb, limit=limit)
            if any(g.get("anchor") == r["anchor"] for g in got):
                hit_b += 1
    return n, hit_a, hit_b


def eval_recovery(session, limit, max_chars=4000):
    """E3+E4: orig 原文 → recover_full_content 保真与分页。"""
    orig = load_orig(session)
    rows = {r.get("anchor"): r for r in manifest_rows(session)}
    n = recover_ok = prefix_ok = 0
    sizes = []
    pag_ok = pag_n = 0
    for anchor, content in orig.items():
        row = rows.get(anchor)
        if not row:
            continue
        n += 1
        got = ctx_recall.recover_full_content(session, anchor, row.get("turn"),
                                              max_chars=max_chars)
        if got:
            recover_ok += 1
            sizes.append(len(got))
            if content[:200] and got[:200] == content[:200]:
                prefix_ok += 1
        # E4 分页: 从 offset=原长一半续读, 应与原文逐字对齐
        if content and len(got or "") >= max_chars:
            half = max(1, len(content) // 2)
            tail = ctx_recall.recover_full_content(
                session, anchor, row.get("turn"),
                max_chars=max_chars, offset=half)
            pag_n += 1
            if tail is not None and content[half:half + max_chars] == tail:
                pag_ok += 1
    return {"n": n, "recovered": recover_ok, "prefix_ok": prefix_ok,
            "sizes": sizes, "pag_n": pag_n, "pag_ok": pag_ok}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", action="append", default=None)
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()
    sessions = args.session or DEFAULT_SESSIONS
    install_prev_fallback()

    print(f"{'session':<12} {'manifest':>8} {'orig':>5} | "
          f"{'E1锚点':>10} | {'E2路径式':>9} {'E2关键词':>9} | "
          f"{'E3恢复':>12} {'前缀保真':>8} | {'E4分页':>7}")
    for s in sessions:
        rows = manifest_rows(s)
        orig = load_orig(s)
        if not rows and not orig:
            print(f"{s:<12} {'—':>8} {'—':>5} | (无存量材料, 跳过)")
            continue
        n1, h1 = eval_anchor_direct(s, rows, args.limit)
        n2, ha, hb = eval_semantic(s, rows, args.limit)
        e34 = eval_recovery(s, args.limit)
        sizes = e34["sizes"]
        med = sorted(sizes)[len(sizes) // 2] if sizes else 0
        print(f"{s:<12} {len(rows):>8} {len(orig):>5} | "
              f"{h1:>4}/{n1:<5} | "
              f"{ha:>3}/{n2:<5} {hb:>3}/{n2:<5} | "
              f"{e34['recovered']:>4}/{e34['n']:<3} 中位{med:>5} "
              f"{e34['prefix_ok']:>4}/{e34['n']:<3} | "
              f"{e34['pag_ok']}/{e34['pag_n']}")
    print("\nE1=锚点直查命中(契约100%%) E2=语义检索 recall@%d(路径式=handle/head,"
          "关键词式=triggers) E3=archive全文恢复+中位返回字节数+前缀200字保真 "
          "E4=offset分页逐字对齐" % args.limit)


if __name__ == "__main__":
    main()
