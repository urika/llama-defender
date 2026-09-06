#!/usr/bin/env python3
"""Gate A：LCP snap-down 检查点复用 vs 全量 prefill 的逐 token 一致性验证。

上下文工程 §11.2 一票否决门禁。前提：引擎 8081 已开 PROXY_CACHE_LCP_SNAPDOWN
（manage.sh conf export + restart-backend），且处于静默窗口（无其他推理流量）。

阶段（由调用方编排，输出落 /tmp/gate_a/）：
  warm  POST M1（多轮对话，~6K tokens，构建条目 + 多边界检查点）
  snap  POST M2（与 M1 共享前缀但中段发散 → 应触发 LCP snapdown）
  cold  POST M2（缓存已清空/重启后 → 全量 prefill，作为 ground truth）
  check 对比 snap vs cold 的输出文本逐字一致 + 验证 snap 阶段引擎日志含
        "LCP snapdown"（证明路径真实被行使）

用法:
  libexec/bin/python tools/gate_a_lcp_snapdown.py warm|snap|cold|check \
      [--url http://127.0.0.1:8081] [--model M]
"""
import argparse
import json
import os
import sys
import time
import urllib.request

OUT_DIR = "/tmp/gate_a"
LOG = "/Users/jinsongwang/APP/llama.cpp/logs/llama-server.log"

# ~2.5K tokens 的确定性系统填充 + 8 轮用户/助手交换 → 总 ~6K tokens、≥10 消息
_SYSTEM = (
    "You are a careful assistant. Internal context pack follows.\n"
    + "\n".join(
        f"pack-{i:03d}: " + " ".join(f"kw{i}x{j}" for j in range(40))
        for i in range(60)
    )
)


def _turns():
    turns = []
    for i in range(1, 9):
        turns.append((
            f"Question {i}: summarize pack-{i:03d} in one short sentence.",
            f"Answer {i}: pack-{i:03d} covers kw{i} keywords {i * 11} through {i * 11 + 39}.",
        ))
    return turns


def build_messages(variant: str) -> list:
    """variant: m1（warm 条目）/ m2（中段发散：替换第 3 条助手回复）。"""
    msgs = [{"role": "system", "content": _SYSTEM}]
    for idx, (u, a) in enumerate(_turns(), start=1):
        if variant == "m2" and idx == 3:
            a = f"Answer 3: revised — pack-003 emphasizes kw3 clusters {idx * 7}."
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user",
                 "content": "Final: list every pack id mentioned in the answers, comma separated."})
    return msgs


def post_chat(url: str, model: str, messages: list) -> dict:
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": 96, "stream": False,
                       "temperature": 0}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    data["_wall_s"] = round(time.time() - t0, 2)
    return data


def log_size() -> int:
    return os.path.getsize(LOG) if os.path.exists(LOG) else 0


def snapdown_logged(since_offset: int) -> int:
    """统计 since_offset 之后的引擎日志中 LCP snapdown 事件数。"""
    n = 0
    with open(LOG, errors="replace") as f:
        f.seek(since_offset)
        for line in f:
            if "LCP snapdown" in line:
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["warm", "snap", "cold", "check"])
    ap.add_argument("--url", default="http://127.0.0.1:8081")
    ap.add_argument("--model", default="pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    if args.phase in ("warm", "snap"):
        variant = "m1" if args.phase == "warm" else "m2"
        marker = log_size()
        r = post_chat(args.url, args.model, build_messages(variant))
        with open(f"{OUT_DIR}/{args.phase}.json", "w") as f:
            json.dump(r, f)
        if args.phase == "snap":
            n = snapdown_logged(marker)
            print(f"[snap] wall={r['_wall_s']}s snapdown_events={n}")
            if n == 0:
                print("⚠️ snap 阶段引擎日志无 'LCP snapdown'——路径未被行使，"
                      "check 对比将无意义。检查 flag/边界计算（chat 路由日志）。")
        else:
            print(f"[warm] wall={r['_wall_s']}s（条目+检查点应已构建）")
        return 0

    if args.phase == "cold":
        r = post_chat(args.url, args.model, build_messages("m2"))
        with open(f"{OUT_DIR}/cold.json", "w") as f:
            json.dump(r, f)
        print(f"[cold] wall={r['_wall_s']}s")
        return 0

    # check
    with open(f"{OUT_DIR}/snap.json") as f:
        snap = json.load(f)
    with open(f"{OUT_DIR}/cold.json") as f:
        cold = json.load(f)
    t_snap = snap["choices"][0]["message"]["content"]
    t_cold = cold["choices"][0]["message"]["content"]
    identical = t_snap == t_cold
    print(f"snap   wall={snap['_wall_s']}s tokens={snap.get('usage', {}).get('completion_tokens')}")
    print(f"cold   wall={cold['_wall_s']}s tokens={cold.get('usage', {}).get('completion_tokens')}")
    if identical:
        print("\n✅ Gate A PASS：输出逐字一致（检查点复用 ≡ 全量 prefill）")
        return 0
    # 首个分歧位置诊断
    i = next((k for k, (a, b) in enumerate(zip(t_snap, t_cold)) if a != b),
             min(len(t_snap), len(t_cold)))
    print(f"\n❌ Gate A FAIL：输出分歧 @ char {i}")
    print(f"  snap: ...{t_snap[max(0, i - 40):i + 40]!r}")
    print(f"  cold: ...{t_cold[max(0, i - 40):i + 40]!r}")
    print("  对策：启用降级模式（检查点仅用于 KV 层）或回退 flag")
    return 1


if __name__ == "__main__":
    sys.exit(main())
