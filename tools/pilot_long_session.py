#!/usr/bin/env python3
"""长会话操作点试点（0920-15 影响二 / Runbook §18.4）。

目的：在 **≥50K 视图** 的 append-only 长会话上，复验"驱逐是否真归零 + S 余量"，
采集四件套：epoch_collapse / [prefix-pressure-evict] / 引擎 /metrics 驱逐+snapdown / 每轮墙钟。

形态：固定 system 前缀 + 逐轮 append（user/assistant 对），前缀单调增长
      → prefix cache 可持续复用（append-only 形态，避免内容全变致每轮全冷）。

用法：
  python3 tools/pilot_long_session.py [--target-tokens 55000] [--max-tokens 128]
      [--url http://127.0.0.1:8081] [--proxy http://127.0.0.1:4000]
      [--out /tmp/ornith-staging/logs/pilot-long-session.json]

依赖：Python 3 stdlib only。
"""
import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.request

BACKEND_LOG = "/Users/jinsongwang/APP/llama.cpp/logs/llama-server.log"
CHARS_PER_TOKEN = 5.15

# 固定 system 前缀（~2K tokens），保证跨轮不变
_SYSTEM = (
    "You are a careful assistant working through a long document set. "
    "Internal context pack follows; treat it as stable reference material.\n"
    + "\n".join(
        f"doc-{i:03d}: " + " ".join(f"ref{i}a{j}" for j in range(30))
        for i in range(80)
    )
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _post(url, body, timeout=600):
    host, port = url.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    data = json.dumps(body).encode("utf-8")
    conn.request("POST", "/v1/chat/completions", data,
                 {"Content-Type": "application/json"})
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    return json.loads(raw.decode("utf-8"))


def _get(url, path):
    host, port = url.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=15)
    conn.request("GET", path)
    r = conn.getresponse()
    raw = r.read().decode("utf-8")
    conn.close()
    return raw


def _log_size():
    try:
        return os.path.getsize(BACKEND_LOG)
    except OSError:
        return 0


def _scan_log(since_bytes):
    """从 since_bytes 起扫描后端日志，返回 (驱逐次数, evict 明细, 折叠行, 冷重建迹象)。"""
    if not os.path.exists(BACKEND_LOG):
        return 0, [], [], 0
    size = os.path.getsize(BACKEND_LOG)
    with open(BACKEND_LOG, "rb") as f:
        if since_bytes > size:
            since_bytes = 0  # 轮转
        f.seek(since_bytes)
        chunk = f.read().decode("utf-8", "replace")
    evicts = re.findall(r"\[prefix-pressure-evict\][^\n]*", chunk)
    folds = re.findall(r"[^\n]*(?:epoch_collapse|epoch transitional|fold)[^\n]*", chunk, re.I)
    cold = len(re.findall(r"adaptive_prefill \d+→\d+|LCP unavailable", chunk))
    return len(evicts), evicts[-5:], folds[-5:], cold


def _metrics_snap(url):
    """引擎 /metrics 里驱逐 + snapdown 计数。"""
    try:
        txt = _get(url, "/metrics")
    except Exception as e:
        return {"error": str(e)}
    out = {}
    for key in ("prefix_cache_evict", "snapdown_hits", "snapdown_tokens_saved",
                "prefix_cache_hits", "prompt_tokens", "cached_tokens"):
        m = re.findall(rf"^{key}\w*[^\n]*", txt, re.M | re.I)
        if m:
            out[key] = m[-1].strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-tokens", type=int, default=55000)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--url", default="http://127.0.0.1:8081")
    ap.add_argument("--proxy", default="http://127.0.0.1:4000")
    ap.add_argument("--out", default="/tmp/ornith-staging/logs/pilot-long-session.json")
    args = ap.parse_args()

    model = os.environ.get("BENCH_MODEL") or "pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp"
    log(f"模型 {model}  目标视图 ~{args.target_tokens} tokens  后端 {args.url}")

    # 引擎起始点
    log_marks = _log_size()
    m0 = _metrics_snap(args.url)
    log(f"起始 metrics: {m0}")

    messages = [{"role": "system", "content": _SYSTEM}]
    rows = []
    turn = 0
    est_tokens = int(len(_SYSTEM) / CHARS_PER_TOKEN)

    while est_tokens < args.target_tokens and turn < 80:
        turn += 1
        # append-only：新 user 提问 + 上轮 assistant 回复（增长前缀）
        user = (f"Turn {turn}: append a new section to the running notes. "
                f"Reference doc-{turn:03d}, list three key points, keep it under 60 words.")
        messages.append({"role": "user", "content": user})

        body = {"model": model, "max_tokens": args.max_tokens,
                "temperature": 0.0, "messages": messages}
        t0 = time.perf_counter()
        try:
            resp = _post(args.url, body)
        except Exception as e:
            log(f"  turn {turn}: ❌ {e}")
            break
        dt = time.perf_counter() - t0
        usage = resp.get("usage", {}) or {}
        try:
            reply = resp["choices"][0]["message"]["content"] or ""
        except Exception:
            reply = ""

        prompt_toks = usage.get("prompt_tokens") or est_tokens
        comp_toks = usage.get("completion_tokens") or 0
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)

        ev_n, ev_last, folds, cold_n = _scan_log(log_marks)
        row = {
            "turn": turn,
            "wall_s": round(dt, 2),
            "prompt_tokens": prompt_toks,
            "cached_tokens": cached,
            "completion_tokens": comp_toks,
            "decode_tok_s": round(comp_toks / dt, 1) if dt > 0 else 0,
            "evict_cum": ev_n,
            "cold_markers": cold_n,
        }
        rows.append(row)
        est_tokens = prompt_toks + comp_toks + int(len(user) / CHARS_PER_TOKEN)

        log(f"  turn {turn:2d}: {dt:6.2f}s  prompt={prompt_toks}  cached={cached}  "
            f"comp={comp_toks}  {row['decode_tok_s']:5.1f} tok/s  evict={ev_n}")

        if not reply:
            log("  ⚠ 空回复，停止")
            break

        messages.append({"role": "assistant", "content": reply})
        est_tokens += int(len(reply) / CHARS_PER_TOKEN)

    ev_total, ev_sample, folds, cold_total = _scan_log(log_marks)
    m1 = _metrics_snap(args.url)
    result = {
        "target_tokens": args.target_tokens,
        "final_est_tokens": est_tokens,
        "turns": len(rows),
        "rows": rows,
        "evict_total": ev_total,
        "evict_samples": ev_sample,
        "fold_markers": folds,
        "cold_markers_total": cold_total,
        "metrics_before": m0,
        "metrics_after": m1,
        "verdict": "PASS" if ev_total == 0 else "FAIL_EVICT",
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log("=" * 70)
    log(f"视图终点 ~{est_tokens} tokens / {len(rows)} 轮")
    log(f"驱逐累计 = {ev_total}    折叠标记 = {len(folds)}    冷重建迹象 = {cold_total}")
    fps = [r["wall_s"] for r in rows[-5:]]
    if fps:
        log(f"末 5 轮墙钟: {fps}")
    log(f"metrics before: {m0}")
    log(f"metrics after : {m1}")
    log(f"判定: {result['verdict']}")
    log(f"结果落盘: {args.out}")
    return 0 if ev_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
