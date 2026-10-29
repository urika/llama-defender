#!/usr/bin/env python3
"""fnf fixture 回放驱动＋断言（T2 仪器保真度门，2026-10-29）。

机制规格（spike 已核，tool_filter.py）：fnf 转译=出站请求 payload 重写；
F1 梯度=同 (sid,path) 计数 n>=3 → 强提示+fnf_guided；F2 候选=同请求 ≥2 次
成功 Read 证明工作区根（commonpath≥2 层）后 ls 父目录；成功读取同路径=清零
逃逸口；英文错误标记一次性消费（已改写中文块不重复计数）。

用法: python3 replay_driver.py [--proxy http://127.0.0.1:4001]
前置: stub_backend.py 已起（:8082）；测试代理 :4001（四旗标 env，见 README）。
"""
import json, os, shutil, sys, urllib.request

PROXY = "http://127.0.0.1:4001"
CAPTURE = "/tmp/fnf_fixture_captures.jsonl"
WS = "/tmp/fnf_fixture_ws/sub"
GHOST = WS + "/ghost.py"
HEADERS = {"Content-Type": "application/json",
           "authorization": "Bearer fnf-fixture",
           "anthropic-version": "2023-06-01"}
def _post(messages, meta, tools=None):
    body = {"model": "stub-model", "max_tokens": 16,
            "stream": False, "metadata": {"user_id": meta},
            "messages": messages}
    if tools:
        body["tools"] = [
            {"name": n, "description": n,
             "input_schema": {"type": "object", "properties": {}}}
            for n in tools]
    body = json.dumps(body).encode()
    req = urllib.request.Request(PROXY + "/v1/messages", data=body,
                                 headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def _captures_since(n):
    with open(CAPTURE, encoding="utf-8") as f:
        lines = f.readlines()
    out = []
    for line in lines[n:]:
        d = json.loads(line)
        if "/v1/" in d["path"]:
            out.append(d["body"])
    return out


def _err_block(tid, text):
    return {"type": "tool_result", "tool_use_id": tid, "is_error": True,
            "content": text}


def _tu_block(tid, path):
    return {"type": "tool_use", "id": tid, "name": "Read",
            "input": {"file_path": path}}


def _asst(blocks):
    return {"role": "assistant", "content": blocks}


def _user(blocks):
    return {"role": "user", "content": blocks}


def main():
    # 工作区：真实目录+已知文件（F2 候选=真实 ls）
    os.makedirs(WS, exist_ok=True)
    for f in ("alpha.py", "beta.py"):
        open(os.path.join(WS, f), "w").write("# fixture\n")
    n0 = len(open(CAPTURE).readlines()) if os.path.exists(CAPTURE) else 0
    meta = "fnf-fixture-A1A3"
    msgs = []
    results = []

    # ---- A1: F1 梯度（req1 basic → req2 escalated）----
    msgs += [_user([{"type": "text", "text": "read ghost"}]),
             _asst([_tu_block("t1", GHOST)]),
             _user([_err_block("t1", "File does not exist: " + GHOST)])]
    _post(msgs, meta)
    cap = _captures_since(n0)[-1]
    txt = json.dumps(cap, ensure_ascii=False)
    results.append(("A1a req1 basic 提示(无禁止语)",
                    "文件不存在：" + GHOST in txt and "禁止再次读取" not in txt))
    msgs += [_asst([{"type": "text", "text": "try again"}]),
             _asst([_tu_block("t2", GHOST)]),
             _user([_err_block("t2", "File does not exist: " + GHOST)])]
    _post(msgs, meta)
    cap = _captures_since(n0)[-1]
    txt = json.dumps(cap, ensure_ascii=False)
    results.append(("A1b req2 升级提示(第3次+禁止语)",
                    "该路径已确认不存在" in txt and "禁止再次读取" in txt))

    # ---- A2: F2 候选（工作区根证明+父目录实况）----
    meta2 = "fnf-fixture-A2"
    msgs2 = [_user([{"type": "text", "text": "explore"}]),
             _asst([_tu_block("s1", WS + "/alpha.py")]),
             _user([{"type": "tool_result", "tool_use_id": "s1",
                     "content": "# alpha\nok"}]),
             _asst([_tu_block("s2", WS + "/beta.py")]),
             _user([{"type": "tool_result", "tool_use_id": "s2",
                     "content": "# beta\nok"}]),
             _asst([_tu_block("s3", GHOST)]),
             _user([_err_block("s3", "File does not exist: " + GHOST)])]
    _post(msgs2, meta2)
    cap = _captures_since(n0)[-1]
    txt = json.dumps(cap, ensure_ascii=False)
    results.append(("A2 F2 父目录实况(alpha.py 候选)",
                    "父目录实况" in txt and "alpha.py" in txt))

    # ---- A3: 清零逃逸口（成功读取后计数重置）----
    msgs += [_asst([_tu_block("t3", GHOST)]),
             _user([{"type": "tool_result", "tool_use_id": "t3",
                     "content": "late success"}]),
             _asst([_tu_block("t4", GHOST)]),
             _user([_err_block("t4", "File does not exist: " + GHOST)])]
    _post(msgs, meta)
    cap = _captures_since(n0)[-1]
    txt = json.dumps(cap, ensure_ascii=False)
    results.append(("A3 清零后回退 basic(无禁止语)",
                    "禁止再次读取" not in txt))

    # ---- A4: wasted-call 重写（旗标无关面）----
    msgs4 = [_user([{"type": "text", "text": "reread"}]),
             _asst([_tu_block("w1", WS + "/alpha.py")]),
             _user([_err_block("w1", "Wasted call: file unchanged since last read")])]
    _post(msgs4, "fnf-fixture-A4")
    cap = _captures_since(n0)[-1]
    txt = json.dumps(cap, ensure_ascii=False)
    results.append(("A4 wasted-call 重写", "未发生变化" in txt or "ctx_recall" in txt))

    # ---- D: 循环触发 L2（max_run=6>=LEVEL2 → 撤高频工具+警告注入）----
    meta3 = "fnf-fixture-D-E"
    TOOLS = ["Bash", "Read", "Grep"]
    msgs3 = [_user([{"type": "text", "text": "loop"}])]
    for i in range(6):  # 6 连相同 Bash 失败 → max_run=6>=LEVEL2(=THRESHOLD*2=6)
        msgs3 += [_asst([{"type": "tool_use", "id": "d%d" % i, "name": "Bash",
                          "input": {"command": "cat /nope.py"}}]),
                  _user([_err_block("d%d" % i,
                                    "cat: /nope.py: No such file or directory")])]
    _post(msgs3, meta3, tools=TOOLS)
    cap = _captures_since(n0)[-1]
    out_tools = [t2.get("function", {}).get("name")
                 for t2 in cap.get("tools", [])]
    results.append(("D1 L2 撤高频工具(Bash 移除/Read 保留)",
                    "Bash" not in out_tools and "Read" in out_tools))
    results.append(("D2 循环警告注入(出站消息增长)",
                    len(cap.get("messages", [])) > len(msgs3)))

    # ---- E: F3' sticky 记忆 + fnf L 级互斥（紧随 D 的无重复请求）----
    msgs4 = [_user([{"type": "text", "text": "calm turn"}]),
             _asst([_tu_block("e1", GHOST)]),
             _user([_err_block("e1", "File does not exist: " + GHOST)])]
    _post(msgs4, meta3)
    cap = _captures_since(n0)[-1]
    txt = json.dumps(cap, ensure_ascii=False)
    results.append(("E1 F3' sticky persistent warning",
                    "previously looping" in txt))
    results.append(("E2 fnf 互斥退化 basic(无禁止语)",
                    "文件不存在：" + GHOST in txt and "禁止再次读取" not in txt))

    ok = 0
    print("=" * 60)
    for name, passed in results:
        print(("PASS " if passed else "FAIL ") + name)
        ok += passed
    print("=" * 60)
    print("%d/%d PASS" % (ok, len(results)))
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
