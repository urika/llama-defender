#!/usr/bin/env python3
"""
JSON 格式输出专项测试
针对 bench_quality 中 format_json 失败项的深入诊断:
- 区分"模型不遵守指令"与"评测器不容忍 markdown 代码块"
- 每个用例同时做严格判定(整体 json.loads)和宽松判定(提取代码块后 json.loads)
"""
import json
import os
import re
import time
import urllib.request

HOST = os.environ.get("LLAMA_HOST", "http://127.0.0.1:4000")

CASES = [
    {
        "id": "json_strict_simple",
        "desc": "严格模式:简单对象,明确禁止代码块",
        "prompt": '返回一个JSON对象,包含字段 city=北京, population=2000万。'
                  '要求:只输出JSON本身,不要使用markdown代码块,不要输出任何其他文字。',
        "check": lambda o: o.get("city") == "北京",
    },
    {
        "id": "json_plain_default",
        "desc": "原始用例:不提格式要求(复现原失败)",
        "prompt": "返回一个有效的JSON对象,包含字段: city=北京, population=2000万",
        "check": lambda o: o.get("city") == "北京",
    },
    {
        "id": "json_nested",
        "desc": "嵌套对象+数组+数字类型",
        "prompt": '只输出JSON(不要用代码块): 一个用户对象,含 name(字符串)、age(数字)、'
                  'tags(字符串数组)、address(嵌套对象,含 city 和 zip)。',
        "check": lambda o: isinstance(o.get("age"), (int, float))
        and isinstance(o.get("tags"), list)
        and isinstance(o.get("address"), dict)
        and "city" in o["address"],
    },
    {
        "id": "json_array",
        "desc": "顶层 JSON 数组",
        "prompt": '只输出一个JSON数组(不要用代码块),包含3个元素,每个元素是'
                  ' {"name": 水果名, "color": 颜色} 形式的对象。',
        "check": lambda o: isinstance(o, list) and len(o) == 3
        and all("name" in x and "color" in x for x in o),
    },
    {
        "id": "json_no_trailing",
        "desc": "JSON 后不得有解释性文字",
        "prompt": '用JSON表示: 温度25度,天气晴。只输出JSON,'
                  '输出后不要添加任何解释、注释或markdown标记。',
        "check": lambda o: len(o) >= 1,
    },
]

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def extract_json(text):
    """宽松提取:优先 ```json 代码块,其次第一个 {...} 或 [...] 片段"""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        end = text.rfind(close_c)
        if start != -1 and end > start:
            return text[start:end + 1]
    return text


def send(prompt, max_tokens=512):
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        f"{HOST}/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": "test",
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    return "".join(b.get("text", "") for b in data.get("content", []))


def main():
    print("=" * 64)
    print("JSON 格式输出专项测试")
    print(f"目标: {HOST}")
    print("=" * 64)
    results = []
    for case in CASES:
        print(f"\n[{case['id']}] {case['desc']}")
        try:
            raw = send(case["prompt"])
        except Exception as e:
            print(f"  ❌ 请求失败: {e}")
            results.append({"id": case["id"], "strict": False, "loose": False,
                            "error": str(e)})
            continue

        strict_ok, loose_ok, semantic_ok = False, False, False
        # 严格: 整个响应必须是一个 JSON 文档
        try:
            obj = json.loads(raw)
            strict_ok = True
            semantic_ok = case["check"](obj)
        except (json.JSONDecodeError, Exception):
            pass
        # 宽松: 提取代码块/片段后解析
        loose_obj = None
        if not strict_ok:
            try:
                loose_obj = json.loads(extract_json(raw))
                loose_ok = True
                semantic_ok = case["check"](loose_obj)
            except (json.JSONDecodeError, Exception):
                pass

        verdict = "✅ 严格通过" if strict_ok else ("⚠️ 宽松通过" if loose_ok else "❌ 失败")
        print(f"  判定: {verdict} | 语义校验: {'✅' if semantic_ok else '❌'}")
        print(f"  响应: {raw[:200]!r}")
        results.append({"id": case["id"], "desc": case["desc"], "strict": strict_ok,
                        "loose": loose_ok or strict_ok, "semantic": semantic_ok,
                        "response": raw})
        time.sleep(0.5)

    print("\n" + "=" * 64)
    print("汇总")
    print("=" * 64)
    n_strict = sum(r["strict"] for r in results)
    n_loose = sum(r["loose"] for r in results)
    n_sem = sum(r["semantic"] for r in results)
    for r in results:
        mark = "✅" if r["strict"] else ("⚠️" if r["loose"] else "❌")
        print(f"  {mark} {r['id']}: strict={r['strict']} loose={r['loose']} semantic={r['semantic']}")
    print("-" * 64)
    print(f"  严格通过(纯JSON): {n_strict}/{len(results)}")
    print(f"  宽松通过(含代码块): {n_loose}/{len(results)}")
    print(f"  语义正确: {n_sem}/{len(results)}")

    os.makedirs("logs", exist_ok=True)
    out = f"logs/json-bench-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"host": HOST, "strict": n_strict, "loose": n_loose,
                   "semantic": n_sem, "results": results}, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果: {out}")


if __name__ == "__main__":
    main()
