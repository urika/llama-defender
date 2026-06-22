#!/usr/bin/env python3
"""智能路由性能测试 — 短/长上下文、路由切换、TTFT 对比。"""
import argparse, json, os, sys, time, urllib.request, urllib.error

PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "4000"))
PROXY_BASE = f"http://{PROXY_HOST}:{PROXY_PORT}"
MODEL = "claude-sonnet-4-6"
TIMEOUT = 120

BASE_TEXT = ("人工智能是计算机科学的分支，旨在创建模拟人类智能的系统。"
    "机器学习使计算机能从数据中学习模式。深度学习使用多层神经网络。"
    "Transformer架构自2017年提出以来，彻底改变了自然语言处理领域。")

def build_long_context(target_chars):
    text = (BASE_TEXT + " ") * max(1, target_chars // len(BASE_TEXT))
    return {"model": MODEL, "max_tokens": 100, "messages": [
        {"role": "user", "content": [{"type": "text", "text": text[:target_chars]}]},
        {"role": "user", "content": [{"type": "text", "text": "Summarize the above in one short sentence."}]}]}

def build_short():
    return {"model": MODEL, "max_tokens": 50,
            "messages": [{"role": "user", "content": "Say hello in one word."}]}

def send(body, extra_headers=None):
    raw = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if extra_headers: h.update(extra_headers)
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(f"{PROXY_BASE}/v1/messages", data=raw, headers=h, method="POST")
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        elapsed = (time.monotonic() - t0) * 1000
        return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8")), elapsed
    except urllib.error.HTTPError as e:
        elapsed = (time.monotonic() - t0) * 1000
        return e.code, {}, json.loads(e.read().decode("utf-8")), elapsed

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--quick", action="store_true")
    p.add_argument("--size", default="150k")
    args = p.parse_args()
    size_map = {"50k": 50000, "100k": 100000, "150k": 150000}
    target = size_map.get(args.size, 150000)
    if args.quick: target = min(target, 30000)

    print(f"=== Route Benchmark: {PROXY_BASE}  size={args.size} ===")

    # TC1: Short → local
    print("\n[TC1] Short context (local expected)")
    s, h, r, e = send(build_short())
    print(f"  route={h.get('X-Route-Target','?')} model={h.get('X-Actual-Model','?')} {r.get('content',[{}])[0].get('text','')[:50]} ({e:.0f}ms)")

    # TC2: Long → cloud
    print(f"\n[TC2] Long context {args.size} (cloud expected)")
    s, h, r, e = send(build_long_context(target))
    print(f"  route={h.get('X-Route-Target','?')} model={h.get('X-Actual-Model','?')} {r.get('content',[{}])[0].get('text','')[:80]} ({e:.0f}ms)")

    # TC3: TTFT comparison (with delays to avoid dedup)
    print(f"\n[TC3] TTFT comparison (3 samples each)")
    local_ttfts, cloud_ttfts = [], []
    for i in range(3):
        _, _, _, e = send(build_short(), {"X-Claude-Code-Session-Id": f"l{i}"})
        local_ttfts.append(e)
        time.sleep(2.5)  # avoid dedup window
    for i in range(3):
        _, _, _, e = send(build_long_context(target), {"X-Claude-Code-Session-Id": f"c{i}", "X-Proxy-Route-To": "cloud"})
        cloud_ttfts.append(e)
        time.sleep(2.5)
    print(f"  Local  (short): avg {sum(local_ttfts)/len(local_ttfts):.0f}ms  samples={[f'{x:.0f}' for x in local_ttfts]}")
    print(f"  Cloud  ({args.size}): avg {sum(cloud_ttfts)/len(cloud_ttfts):.0f}ms  samples={[f'{x:.0f}' for x in cloud_ttfts]}")

    # TC4: Session lifecycle
    print(f"\n[TC4] Session lifecycle: new→cloud→stay→new→local")
    sid = "bench-lifecycle"
    s, h, _, _ = send(build_short(), {"X-Claude-Code-Session-Id": sid})
    r1 = h.get('X-Route-Target','?')
    s, h, _, _ = send(build_long_context(target), {"X-Claude-Code-Session-Id": sid})
    r2 = h.get('X-Route-Target','?')
    s, h, _, _ = send(build_short(), {"X-Claude-Code-Session-Id": sid})
    r3 = h.get('X-Route-Target','?')
    s, h, _, _ = send(build_short(), {"X-Claude-Code-Session-Id": "bench-new-session"})
    r4 = h.get('X-Route-Target','?')
    print(f"  {r1} → {r2} → {r3} → {r4} (new session)")

    print(f"\n=== Done ===")

if __name__ == "__main__": main()
