#!/usr/bin/env python3
"""gate_test_ctx_engine.py — R8 上下文工程引擎验收门禁测试（方案 A 合成会话）。

门禁（设计 §5 Phase 1 验收 + §12.4 修正）:
  G1 非 epoch 轮 duration P90 < 15s
  G2 epoch 轮 duration P90 < 60s
  G3 0 客户端断连（本脚本无超时断连即通过；4xx/5xx 记失败）
  G4 epoch 后 hit_ratio 恢复（下一非 epoch 轮 cached_tokens/prompt_tokens > 0.8）

会话形态: 模拟 agentic 工具会话——每轮 user 追加 + assistant tool_use +
tool_result(4K chars 正文), 驱动每轮净增 ~5K chars;65 轮后 est tokens 超
S(70K) 触发 epoch;继续跑到 80 轮覆盖 1-2 次 epoch。

数据源: 代理 /v1/messages 响应 usage.prompt_tokens_details.cached_tokens
(§12.1 主路径) + logs/diag/sessions.jsonl 的 is_epoch_turn(事后对齐)。
用法: python3 tools/gate_test_ctx_engine.py [--turns 80] [--session KEY]
      [--endpoint http://127.0.0.1:4000] [--out logs/gate-ctx-engine.json]
"""
import argparse
import json
import os
import time
import urllib.request

BIG = ("The quick brown fox jumps over the lazy dog. " * 90)[:4000]  # 4K/轮


class HistoryBuilder:
    """claude CLI 真实形态的 append-only 历史。

    每轮: assistant(tool_use) + user(tool_result) + user(问题) → 发送;
    响应文本作为 assistant 回复回显进下一轮历史——rapid-mlx 缓存语义
    要求"已存条目 ⊂ 新请求"(严格前缀可扩展), 丢弃模型回复会立即分叉
    (gate-ce-02 全 MISS 的根因, 与 claude CLI 行为对齐后命中恢复)。
    """

    def __init__(self):
        self.msgs = [{"role": "user", "content": [
            {"type": "text", "text": "task: summarize repo state turn 1"}]}]

    def next_request(self, turn):
        self.msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": "t%d" % turn, "name": "Read",
             "input": {"file_path": "/repo/src/module_%d.py" % turn}}]})
        self.msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t%d" % turn,
             "content": "[module %d content]\n%s" % (turn, BIG)}]})
        self.msgs.append({"role": "user", "content": [
            {"type": "text", "text": "turn %d: continue" % turn}]})
        return self.msgs

    def echo_reply(self, payload):
        text = "".join(b.get("text", "") for b in payload.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "text")
        self.msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": text or "(empty)"}]})


def post(endpoint, body, sid):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/messages", data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "anthropic-version": "2023-06-01",
                 "X-Claude-Code-Session-Id": sid,
                 "X-Proxy-Route-To": "local"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return (time.monotonic() - t0) * 1000, payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=80)
    ap.add_argument("--session", default="gate-ce-%d" % int(time.time()))
    ap.add_argument("--endpoint", default="http://127.0.0.1:4000")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = []
    builder = HistoryBuilder()
    print("session=%s turns=%d" % (args.session, args.turns))
    for turn in range(1, args.turns + 1):
        body = {"model": args.model, "max_tokens": 16,
                "messages": builder.next_request(turn)}
        try:
            dur_ms, payload = post(args.endpoint, body, args.session)
        except Exception as e:
            rows.append({"turn": turn, "error": str(e)[:200]})
            print("turn %d ERROR %s" % (turn, e))
            break
        builder.echo_reply(payload)
        usage = payload.get("usage") or {}
        cached = usage.get("cache_read_input_tokens") or 0
        prompt_toks = usage.get("input_tokens") or 0
        hit = (cached / prompt_toks) if prompt_toks else None
        rows.append({
            "turn": turn,
            "duration_ms": round(dur_ms, 1),
            "prompt_tokens": prompt_toks,
            "cached_tokens": cached,
            "hit_ratio": round(hit, 4) if hit is not None else None,
        })
        print("turn %3d dur=%6.0fms prompt=%6d cached=%6d hit=%s" % (
            turn, dur_ms, prompt_toks, cached,
            "%.3f" % hit if hit is not None else "-"))
        time.sleep(0.3)

    out = args.out or os.path.join("logs", "gate-ctx-engine-%s.json" % args.session)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"session": args.session, "rows": rows}, f, ensure_ascii=False, indent=1)
    print("written: %s" % out)


if __name__ == "__main__":
    main()
