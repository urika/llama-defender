#!/usr/bin/env python3
"""semantic_search.py —— 基于 EmbeddingGemma 2 的本地语义检索（纯标准库）

把一棵目录（markdown/文本）切成片段、调本地嵌入引擎编码成向量，落成一个
可复用的索引；之后用自然语言问句做按“意思”而非字面匹配的检索。

引擎默认 http://127.0.0.1:8083/v1/embeddings —— 由
`./manage.sh start-aux embedgemma2` 拉起（见 configs/embedgemma2.conf）。
密钥取 $LLAMA_API_KEY，未设置时回退读取 configs/secret.local.conf。

用法:
    # 建索引（首次全量；再次运行按 mtime/size 增量复用未变文件的向量）
    python3 tools/semantic_search.py index --root docs --out .semantic/docs
    python3 tools/semantic_search.py index --root /path/to/wiki/wiki --out .semantic/wiki

    # 检索
    python3 tools/semantic_search.py query "怎么避免 Metal 显存不足" --index .semantic/docs -k 5
    python3 tools/semantic_search.py query "多智能体 RL 的信用分配" --index .semantic/wiki --task search
    python3 tools/semantic_search.py query "..." --index .semantic/docs --json

    # 体检 / 一次性小目录检索（不落索引）
    python3 tools/semantic_search.py info --index .semantic/docs
    python3 tools/semantic_search.py oneshot --root docs/02-architecture-design "KV 量化"

设计约束（重要）:
  * 纯标准库（array/json/urllib），与仓库其它 Python 模块一致，可被
    ctx_recall 之类的运行期模块直接 import。
  * 向量以 float32 裸数组存盘（vectors.f32），行序与 chunks.jsonl 对齐。
  * 查询向量与文档向量都做 L2 归一化，点积即余弦。
  * 注意：EmbeddingGemma 的余弦值域很窄（实测无关句 ~0.75、同义句 ~0.87），
    只能按排序取 top-k，不要用绝对阈值判断“是否相关”。
"""

from __future__ import annotations

import argparse
import array
import fnmatch
import heapq
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ENGINE = os.environ.get("EG2_EMBED_URL", "http://127.0.0.1:8083/v1/embeddings")
DEFAULT_MODEL = os.environ.get("EG2_EMBED_MODEL", "embeddinggemma-2-Q8_0")
INDEX_VERSION = 1

# 任务前缀（模型卡规定；漏掉不报错但精度下降）。asymmetric=True 表示
# 查询/文档两侧前缀不同（检索类）；False 表示两侧同前缀（对称类任务）。
TASKS = {
    "search":       (True,  "task: search result | query: ",       "title: {title} | text: {text}"),
    "qa":           (True,  "task: question answering | query: ",  "title: {title} | text: {text}"),
    "fact":         (True,  "task: fact checking | query: ",       "title: {title} | text: {text}"),
    "code":         (True,  "task: code retrieval | query: ",      "title: {title} | text: {text}"),
    "classification": (False, "task: classification | query: ",     "task: classification | query: {text}"),
    "clustering":   (False, "task: clustering | query: ",          "task: clustering | query: {text}"),
    "similarity":   (False, "task: sentence similarity | query: ",  "task: sentence similarity | query: {text}"),
}

FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
SKIP_DIRS = {".git", ".obsidian", "node_modules", "__pycache__", ".venv", ".semantic", "archive"}
SKIP_EXT = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".bin", ".f32", ".jsonl"}


# --------------------------------------------------------------------------
# 引擎客户端
# --------------------------------------------------------------------------
def load_api_key(explicit=None):
    """$LLAMA_API_KEY → --api-key → configs/secret.local.conf。"""
    if explicit:
        return explicit
    key = os.environ.get("LLAMA_API_KEY", "")
    if key:
        return key
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(here, "configs", "secret.local.conf"),
                 os.path.join(os.getcwd(), "configs", "secret.local.conf")):
        if not os.path.isfile(cand):
            continue
        try:
            with open(cand, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r'\s*(?:export\s+)?LLAMA_API_KEY\s*=\s*["\']?([^"\'\s#]+)', line)
                    if m:
                        return m.group(1)
        except OSError:
            pass
    return ""


def embed(texts, engine=DEFAULT_ENGINE, model=DEFAULT_MODEL, key="", batch=16,
          timeout=180, retries=2, verbose=False):
    """批量编码，返回 list[list[float]]（顺序与输入一致）。"""
    out = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        body = json.dumps({"input": part, "model": model}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        last = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(engine, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                rows = sorted(payload["data"], key=lambda d: d.get("index", 0))
                out.extend([r["embedding"] for r in rows])
                if verbose:
                    print(f"  [{i + len(part)}/{len(texts)}] {len(part)} 条", file=sys.stderr)
                break
            except Exception as e:  # noqa: BLE001 - 统一重试
                last = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        else:
            raise SystemExit(f"嵌入请求失败（{len(part)} 条）: {last}")
    return out


def l2_normalize(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


def dot(a, b):
    s = 0.0
    for x, y in zip(a, b):
        s += x * y
    return s


# --------------------------------------------------------------------------
# 切块
# --------------------------------------------------------------------------
def _split_long(text, max_chars):
    """把超长正文按段落边界切到 <= max_chars。"""
    if len(text) <= max_chars:
        return [text]
    parts, buf, size = [], [], 0
    for para in re.split(r"\n\s*\n", text):
        p = para.strip("\n")
        if not p:
            continue
        if size + len(p) + 2 > max_chars and buf:
            parts.append("\n\n".join(buf))
            buf, size = [], 0
        while len(p) > max_chars:           # 单个超长段落再硬切
            parts.append(p[:max_chars])
            p = p[max_chars:]
        buf.append(p)
        size += len(p) + 2
    if buf:
        parts.append("\n\n".join(buf))
    return parts


def chunk_markdown(path, root, max_chars=1200, min_chars=40):
    """按 markdown 标题切块；返回 [{src,title,line,text}]。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return []
    raw = FRONTMATTER_RE.sub("", raw)
    lines = raw.split("\n")
    rel = os.path.relpath(path, root)
    base = os.path.splitext(os.path.basename(path))[0]
    chunks, cur_title, cur_line, buf, hier = [], base, 1, [], {}

    def flush():
        body = "\n".join(buf).strip()
        if len(body) >= min_chars:
            title = " > ".join([hier[k] for k in sorted(hier) if hier[k]]) or base
            for piece in _split_long(body, max_chars):
                chunks.append({"src": rel, "title": title, "line": cur_line, "text": piece})
        buf.clear()

    for idx, line in enumerate(lines, 1):
        m = HEADING_RE.match(line)
        if m:
            flush()
            level, text = len(m.group(1)), m.group(2).strip()
            hier[level] = text
            for k in [k for k in hier if k > level]:
                del hier[k]
            cur_title, cur_line = text, idx
        else:
            buf.append(line)
    flush()
    return chunks


def iter_files(root, exts, patterns, excludes=None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            if exts and os.path.splitext(name)[1].lower() not in exts:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if patterns and not any(fnmatch.fnmatch(rel, p) for p in patterns):
                continue
            if excludes and any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.rstrip("/") + "/*")
                                for p in excludes):
                continue
            yield full, rel


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------
def _read_vectors(path, dim):
    """读 vectors.f32 → list[row]，每行是 list[float]。"""
    if not os.path.isfile(path):
        return []
    a = array.array("f")
    with open(path, "rb") as f:
        a.frombytes(f.read())
    if dim <= 0:
        return []
    return [list(a[i * dim:(i + 1) * dim]) for i in range(len(a) // dim)]


def cmd_index(args):
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"目录不存在: {root}")
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    _, q_pfx, d_pfx = TASKS[args.task]
    exts = {e if e.startswith(".") else "." + e for e in args.ext.split(",") if e}

    old_meta, old_chunks, old_rows = {}, [], {}
    meta_path = os.path.join(out, "meta.json")
    if args.incremental and os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                old_meta = json.load(f)
            if old_meta.get("max_chars") == args.max_chars and old_meta.get("task") == args.task:
                with open(os.path.join(out, "chunks.jsonl"), "r", encoding="utf-8") as f:
                    old_chunks = [json.loads(l) for l in f if l.strip()]
                old_vecs = _read_vectors(os.path.join(out, "vectors.f32"), old_meta.get("dim", 0))
                for i, c in enumerate(old_chunks):
                    if i < len(old_vecs):
                        old_rows.setdefault(c["src"], []).append(old_vecs[i])
        except (OSError, ValueError, KeyError):
            old_meta, old_chunks, old_rows = {}, [], {}

    files = list(iter_files(root, exts, args.filter, args.exclude))
    new_chunks, new_vecs, files_meta = [], [], {}
    reused = fresh = 0
    todo = []           # (src, [chunk,...]) 需要重新编码的
    for full, rel in files:
        st = os.stat(full)
        files_meta[rel] = {"mtime": int(st.st_mtime), "size": st.st_size}
        chunks = chunk_markdown(full, root, args.max_chars)
        if not chunks:
            continue
        prev = old_meta.get("files", {}).get(rel)
        cache = old_rows.get(rel, [])
        if (args.incremental and prev and cache and len(cache) == len(chunks)
                and prev.get("mtime") == int(st.st_mtime) and prev.get("size") == st.st_size):
            for c, v in zip(chunks, cache):
                new_chunks.append(c)
                new_vecs.append(v)
            reused += len(chunks)
        else:
            todo.append((rel, chunks))

    if todo:
        texts = [d_pfx.format(title=c["title"][:200] or "none", text=c["text"]) for _, cs in todo for c in cs]
        print(f"编码 {len(texts)} 个新片段（共 {len(files)} 个文件，复用 {reused} 段）…", file=sys.stderr)
        vecs = embed(texts, engine=args.engine, model=args.model, key=load_api_key(args.api_key),
                     batch=args.batch, verbose=args.verbose)
        k = 0
        for rel, cs in todo:
            for c in cs:
                new_chunks.append(c)
                new_vecs.append(l2_normalize(vecs[k]))
                k += 1
            fresh += len(cs)
            print(f"  ✓ {rel}", file=sys.stderr)

    dim = len(new_vecs[0]) if new_vecs else 0
    with open(os.path.join(out, "vectors.f32"), "wb") as f:
        for v in new_vecs:
            array.array("f", v).tofile(f)
    with open(os.path.join(out, "chunks.jsonl"), "w", encoding="utf-8") as f:
        for i, c in enumerate(new_chunks):
            f.write(json.dumps({"i": i, **c}, ensure_ascii=False) + "\n")
    meta = {
        "version": INDEX_VERSION, "index": os.path.basename(out), "root": root,
        "engine": args.engine, "model": args.model, "dim": dim, "task": args.task,
        "query_prefix": q_pfx, "doc_prefix": d_pfx, "max_chars": args.max_chars,
        "count": len(new_chunks), "files": files_meta,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"✅ 索引 {out}: {len(new_chunks)} 段 / {len(files_meta)} 文件 / {dim} 维"
          f"（新编码 {fresh}，复用 {reused}）")
    return 0


def load_index(path):
    with open(os.path.join(path, "meta.json"), "r", encoding="utf-8") as f:
        meta = json.load(f)
    chunks = []
    with open(os.path.join(path, "chunks.jsonl"), "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                chunks.append(json.loads(line))
    vecs = _read_vectors(os.path.join(path, "vectors.f32"), meta.get("dim", 0))
    if len(vecs) != len(chunks):
        raise SystemExit(f"索引损坏：{len(vecs)} 向量 vs {len(chunks)} 片段，请重建")
    return meta, chunks, vecs


def search(meta, chunks, vecs, queries, k=5, task=None, engine=None, model=None,
           api_key=None, filt=None, min_score=None):
    task = task or meta.get("task", "search")
    _, q_pfx, _ = TASKS[task]
    key = load_api_key(api_key)
    qs = [q_pfx + q for q in queries]
    qv = [l2_normalize(v) for v in embed(qs, engine=engine or meta["engine"],
                                        model=model or meta["model"], key=key)]
    results = []
    for q, v in zip(queries, qv):
        hits = []
        for i, row in enumerate(vecs):
            if filt and not fnmatch.fnmatch(chunks[i]["src"], filt):
                continue
            s = dot(row, v)
            if min_score is not None and s < min_score:
                continue
            hits.append((s, i))
        top = heapq.nlargest(k, hits)
        results.append({"query": q, "hits": [
            {"score": round(s, 4), "src": chunks[i]["src"], "line": chunks[i].get("line"),
             "title": chunks[i].get("title"),
             "text": " ".join(chunks[i]["text"].split())[:200]} for s, i in top]})
    return results


def cmd_query(args):
    meta, chunks, vecs = load_index(args.index)
    res = search(meta, chunks, vecs, [args.text], k=args.k, task=args.task,
                 engine=args.engine, model=args.model, api_key=args.api_key,
                 filt=args.filter, min_score=args.min_score)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    for r in res:
        print(f"问：{r['query']}   （索引 {meta['index']}，{meta['count']} 段 / {meta['dim']} 维）")
        for n, h in enumerate(r["hits"], 1):
            loc = f"{h['src']}:{h['line']}" if h.get("line") else h["src"]
            print(f"  {n}. {h['score']:.3f}  {loc}")
            print(f"       [{h['title']}] {h['text']}…")
    return 0


def cmd_oneshot(args):
    """不落索引的小目录检索（文件少、临时用）。"""
    root = os.path.abspath(args.root)
    _, q_pfx, d_pfx = TASKS[args.task]
    chunks = []
    for full, rel in iter_files(root, {e if e.startswith(".") else "." + e
                                       for e in args.ext.split(",") if e}, args.filter, args.exclude):
        chunks.extend(chunk_markdown(full, root, args.max_chars))
    if not chunks:
        raise SystemExit("没有切出任何片段")
    key = load_api_key(args.api_key)
    dv = [l2_normalize(v) for v in embed([d_pfx.format(title=c["title"][:200] or "none", text=c["text"])
                                          for c in chunks], engine=args.engine, model=args.model, key=key)]
    qv = l2_normalize(embed([q_pfx + args.text], engine=args.engine, model=args.model, key=key)[0])
    hits = heapq.nlargest(args.k, ((dot(d, qv), i) for i, d in enumerate(dv)))
    print(f"问：{args.text}   （一次性，{len(chunks)} 段）")
    for n, (s, i) in enumerate(hits, 1):
        c = chunks[i]
        print(f"  {n}. {s:.3f}  {c['src']}:{c['line']}")
        print(f"       [{c['title']}] {' '.join(c['text'].split())[:150]}…")
    return 0


def cmd_info(args):
    meta, chunks, vecs = load_index(args.index)
    size = sum(os.path.getsize(os.path.join(args.index, f))
               for f in ("meta.json", "chunks.jsonl", "vectors.f32"))
    print(f"索引   : {args.index}")
    print(f"根目录 : {meta['root']}")
    print(f"引擎   : {meta['engine']}  model={meta['model']}")
    print(f"规模   : {meta['count']} 段 / {len(meta['files'])} 文件 / {meta['dim']} 维")
    print(f"任务   : {meta['task']}  query_prefix={meta['query_prefix']!r}")
    print(f"占盘   : {size / 1048576:.1f} MB   建于 {meta['created']}")
    srcs = sorted({c["src"] for c in chunks})
    print(f"来源   : {len(srcs)} 个文件，示例 {srcs[:3]}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="EmbeddingGemma 2 本地语义检索（纯标准库）",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="引擎: ./manage.sh start-aux embedgemma2  （端口 8083）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, out=False):
        sp.add_argument("--engine", default=DEFAULT_ENGINE, help="嵌入端点")
        sp.add_argument("--model", default=DEFAULT_MODEL)
        sp.add_argument("--api-key", default=None, help="默认 $LLAMA_API_KEY 或 secret.local.conf")
        sp.add_argument("--task", default="search", choices=sorted(TASKS), help="任务前缀（默认 search）")

    pi = sub.add_parser("index", help="建索引（可增量）")
    pi.add_argument("--root", required=True, help="要索引的目录")
    pi.add_argument("--out", required=True, help="索引输出目录")
    pi.add_argument("--ext", default=".md", help="纳入的扩展名，逗号分隔（默认 .md）")
    pi.add_argument("--filter", nargs="*", default=None, help="相对路径 glob 过滤，如 'docs/02-*'")
    pi.add_argument("--exclude", nargs="*", default=None, help="排除的 glob，如 'templates/*'")
    pi.add_argument("--max-chars", type=int, default=1200, help="片段最大字符数（默认 1200）")
    pi.add_argument("--batch", type=int, default=16)
    pi.add_argument("--no-incremental", dest="incremental", action="store_false",
                    help="全量重建（默认按 mtime/size 复用未变文件）")
    pi.add_argument("--verbose", action="store_true")
    common(pi, out=True)
    pi.set_defaults(func=cmd_index)

    pq = sub.add_parser("query", help="检索")
    pq.add_argument("text")
    pq.add_argument("--index", required=True)
    pq.add_argument("-k", type=int, default=5)
    pq.add_argument("--filter", default=None, help="只在匹配的 src 上检索")
    pq.add_argument("--min-score", type=float, default=None, help="可选，慎用绝对阈值")
    pq.add_argument("--json", action="store_true")
    common(pq)
    pq.set_defaults(func=cmd_query)

    po = sub.add_parser("oneshot", help="小目录直接检索，不落索引")
    po.add_argument("text")
    po.add_argument("--root", required=True)
    po.add_argument("-k", type=int, default=5)
    po.add_argument("--ext", default=".md")
    po.add_argument("--filter", nargs="*", default=None)
    po.add_argument("--exclude", nargs="*", default=None)
    po.add_argument("--max-chars", type=int, default=1200)
    common(po)
    po.set_defaults(func=cmd_oneshot)

    pn = sub.add_parser("info", help="看索引信息")
    pn.add_argument("--index", required=True)
    pn.set_defaults(func=cmd_info)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
