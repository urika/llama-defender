#!/usr/bin/env python3
"""
量化敏感代码质量评测 — 区分 Dense 4bit 与 UD(动态量化) 等变体的质量差异

背景: 传统质量评测(bench_quality.py / bench_baseline.py)用"子串包含"检查,
      对 35B-A3B 的 4bit 与 UD-MLX-4bit 两个变体得分为 14/14, 完全无区分力。
      UD 变体在注意力层与共享专家保留 8bit, 主要影响:
        1. 复杂代码逻辑正确性 (长代码、多步算法)
        2. 数值精度 (大数运算、精确输出)
        3. 长上下文信息保真 (事实提取、参数记忆)
      本工具针对这三个维度设计"可执行验证"测试 — 让模型生成完整程序,
      实际运行并校验输出, 而非简单检查文本子串。

测试维度:
  A. 可执行代码 (exec): 生成代码 -> subprocess 运行 -> 校验 stdout
  B. 数值精度 (num): 精确算术/格式输出
  C. 长上下文保真 (ctx): 长文档中抽取精确信息

用法:
    python3 tools/bench_code_quality.py                # 评测当前代理后端
    python3 tools/bench_code_quality.py --quick        # 快速模式(每维度2例)
    python3 tools/bench_code_quality.py --save NAME    # 保存结果到 logs/quality-exe/
    python3 tools/bench_code_quality.py --compare NAME1 NAME2   # 对比两份结果

返回: 每用例 pass/fail + 原因, 维度汇总, 总分(0-100)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime

HOST = os.environ.get("LLAMA_HOST", "http://127.0.0.1:4000")
HEADERS = {"Content-Type": "application/json", "x-api-key": "test", "anthropic-version": "2023-06-01"}
RESULT_DIR = os.path.join("logs", "quality-exe")

# ============ 测试用例 ============
# exec 用例: {"prompt", "setup", "stdin", "expected_stdout_contains", "desc"}
EXEC_TESTS = {
    "lru_cache": {
        "desc": "LRU Cache 正确性",
        "prompt": (
            "写一个完整的 Python 程序实现 LRU Cache。要求:\n"
            "1. 类名 LRUCache, 构造参数 capacity\n"
            "2. 方法 get(key) 返回 -1 若不存在; put(key,value) 满时淘汰最久未用\n"
            "3. 程序末尾用以下测试序列验证并打印结果:\n"
            "   c=LRUCache(2); c.put(1,1); c.put(2,2); print(c.get(1)); "
            "c.put(3,3); print(c.get(2)); c.put(4,4); print(c.get(1)); print(c.get(3)); print(c.get(4))\n"
            "只输出可运行的 Python 代码, 不要任何解释文字。"
        ),
        "expected": "1\n-1\n-1\n3\n4",
        "check": "stdout_lines",
    },
    "quicksort": {
        "desc": "快速排序正确性",
        "prompt": (
            "写一个完整 Python 程序: 实现快速排序函数 quicksort(arr), 返回排序后的列表。\n"
            "然后对 [5,2,9,1,7,6,3,8,0,4] 排序并 print 结果。\n"
            "只输出可运行的 Python 代码, 不要任何解释文字。"
        ),
        "expected": "[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]",
        "check": "stdout_contains",
    },
    "binary_search_tree": {
        "desc": "二叉搜索树插入+查找",
        "prompt": (
            "写一个完整 Python 程序实现二叉搜索树 BST。要求 insert 与 search 方法。\n"
            "程序末尾: bst=BST(); 依次插入 [8,3,10,1,6,14,4,7,13]; "
            "然后 print(search(6), search(7), search(11)) 分别输出布尔值。\n"
            "只输出可运行的 Python 代码, 不要任何解释文字。"
        ),
        "expected": "True True False",
        "check": "stdout_contains",
    },
    "long_code_function": {
        "desc": "长函数逻辑一致性 (30+ 行)",
        "prompt": (
            "写一个完整的 Python 程序: 实现学生成绩统计系统。\n"
            "函数 analyze(records) 接收 [(name,score)] 列表, 返回 dict: "
            "{'avg': 平均分(保留1位小数), 'max': 最高分人名, 'min': 最低分人名, "
            "'pass': 及格人数}。\n"
            "程序末尾: print(analyze([('Alice',92),('Bob',57),('Cindy',78),('Dan',63),('Eve',85)]))。\n"
            "只输出可运行的 Python 代码, 不要任何解释文字。"
        ),
        "expected": "'avg': 75.0",
        "check": "stdout_contains",
    },
    "tricky_logic": {
        "desc": "多步骤逻辑 (岛屿数量)",
        "prompt": (
            "写一个完整 Python 程序: 实现函数 num_islands(grid), 计算二维网格中岛屿数量 "
            "(1为陆地, 相邻上下左右相连算同一岛屿)。\n"
            "程序末尾: print(num_islands([\n"
            "  [1,1,0,0,0],\n  [1,1,0,0,0],\n  [0,0,1,0,0],\n  [0,0,0,1,1]\n]))\n"
            "只输出可运行的 Python 代码, 不要任何解释文字。"
        ),
        "expected": "3",
        "check": "stdout_contains",
    },
}

# 数值精度用例: {"prompt", "expected_contains", "desc"}
NUM_TESTS = {
    "big_mul": {
        "desc": "大数乘法 (8位x9位)",
        "prompt": "计算 12345678 × 987654321 = ? 只输出数字, 不要任何文字。",
        "expected": "12193262222374638",
    },
    "exact_float": {
        "desc": "浮点精确格式",
        "prompt": "计算 3.14159 × 2.71828 = ? 保留5位小数, 只输出数字, 不要任何文字。",
        "expected": "8.53973",
    },
    "mod_big": {
        "desc": "大数取模",
        "prompt": "计算 2^50 mod 97 = ? 只输出数字, 不要任何文字。",
        "expected": "4",
    },
}

# 长上下文保真用例: {"content": 注入的长文档, "question", "expected_contains", "desc"}
CTX_TESTS = {
    "sensor_report": {
        "desc": "长报告数据提取",
        "content": (
            "【机房环境监测日报】2026-08-03\n"
            "A 机房: 温度 22.4℃, 湿度 45%, 服务器 32 台, 告警 0\n"
            "B 机房: 温度 19.8℃, 湿度 52%, 服务器 48 台, 告警 2 (节点 B-07 风扇故障, B-19 内存 ECC 错误)\n"
            "C 机房: 温度 24.1℃, 湿度 38%, 服务器 21 台, 告警 0\n"
            + "机柜编号列表: " + ", ".join(f"RACK-{i:03d}" for i in range(1, 61)) + "\n"
            "电力负载: A=34.2kW, B=41.7kW, C=27.9kW, 总 103.8kW\n"
        ),
        "question": "根据以上报告: 1) B 机房有哪两个告警节点? 2) 总电力负载是多少 kW?",
        "expected": "B-07",
    },
    "param_stress": {
        "desc": "长参数列表精确回忆",
        "content": (
            "API 配置参数表(请精确记忆):\n"
            "rate_limit=3000, timeout_ms=15000, retries=4, batch_size=128, "
            "compression='zstd', region='ap-southeast-2', env='staging', "
            "feature_flags={'dark_mode':True,'beta_agent':False,'streaming':True,'cache_ttl':3600}\n"
            + "详细部署日志:\n" + "\n".join(
                f"  step {i}: host={['web','api','db','cache'][i%4]}-{i} "
                f"status={'ok' if i%7 else 'retry'} bytes={i*1379} latency_ms={i*3+12}"
                for i in range(1, 41)
            )
        ),
        "question": "请问: 1) rate_limit 是多少? 2) region 是哪个? 3) feature_flags 里 beta_agent 的值?",
        "expected": "3000",
    },
}


def _send(prompt, max_tokens=1500, temperature=0.0):
    body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": max_tokens,
                       "stream": False, "temperature": temperature,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(HOST + "/v1/messages", data=body, headers=HEADERS)
    t0 = time.perf_counter()
    resp = urllib.request.urlopen(req, timeout=180)
    elapsed = time.perf_counter() - t0
    data = json.loads(resp.read())
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, elapsed, data.get("usage", {}).get("output_tokens", 0)


def _extract_python(text):
    fence = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    fence2 = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if fence2:
        return fence2.group(1).strip()
    lines = text.strip().split("\n")
    start = next((i for i, l in enumerate(lines) if l.strip().startswith(("import ", "from ", "class ", "def ", "#"))), 0)
    return "\n".join(lines[start:]).strip()


def _run_python(code, timeout=15):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        r = subprocess.run([sys.executable, path], capture_output=True, text=True,
                           timeout=timeout, cwd=tempfile.gettempdir())
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -9, "", "timeout"
    finally:
        os.unlink(path)


def run_exec_tests(quick=False):
    print("\n  ── A. 可执行代码 (生成→运行→校验) ──")
    tests = dict(list(EXEC_TESTS.items())[:2]) if quick else EXEC_TESTS
    results = {}
    for tid, tc in tests.items():
        print(f"    [{tid}] {tc['desc']}...", end=" ", flush=True)
        try:
            text, elapsed, out_tok = _send(tc["prompt"], max_tokens=1200)
            code = _extract_python(text)
            rc, stdout, stderr = _run_python(code)
            exp = tc["expected"]
            if tc["check"] == "stdout_lines":
                got_lines = [l for l in stdout.split("\n") if l.strip()]
                expected_lines = [l for l in exp.split("\n") if l.strip()]
                pass_ = got_lines == expected_lines
                detail = f"rc={rc} got={got_lines[:6]}"
            else:
                norm_stdout = stdout.replace(" ", "")
                norm_exp = exp.replace(" ", "")
                pass_ = rc == 0 and norm_exp in norm_stdout
                detail = f"rc={rc} stdout='{stdout[:60]}'"
            if not pass_ and rc != 0:
                detail += f" err='{stderr[:80]}'"
            status = "✅" if pass_ else "❌"
            print(f"{status} {out_tok}tok {elapsed:.1f}s  {detail}")
            results[tid] = {"pass": pass_, "elapsed": round(elapsed, 2),
                            "output_tokens": out_tok, "rc": rc, "detail": detail,
                            "expected": exp, "stdout": stdout, "stderr": stderr[:200]}
        except Exception as e:
            print(f"❌ ERROR: {str(e)[:80]}")
            results[tid] = {"pass": False, "error": str(e)[:120]}
    return results


def run_num_tests(quick=False):
    print("\n  ── B. 数值精度 ──")
    tests = dict(list(NUM_TESTS.items())[:1]) if quick else NUM_TESTS
    results = {}
    for tid, tc in tests.items():
        print(f"    [{tid}] {tc['desc']}...", end=" ", flush=True)
        try:
            text, elapsed, out_tok = _send(tc["prompt"], max_tokens=100)
            pass_ = tc["expected"] in text.replace(" ", "").replace(",", "")
            status = "✅" if pass_ else "❌"
            print(f"{status} got='{text.strip()[:40]}' 期望'{tc['expected']}'  {elapsed:.1f}s")
            results[tid] = {"pass": pass_, "elapsed": round(elapsed, 2),
                            "got": text.strip()[:100], "expected": tc["expected"]}
        except Exception as e:
            print(f"❌ ERROR: {str(e)[:80]}")
            results[tid] = {"pass": False, "error": str(e)[:120]}
    return results


def run_ctx_tests(quick=False):
    print("\n  ── C. 长上下文保真 ──")
    tests = dict(list(CTX_TESTS.items())[:1]) if quick else CTX_TESTS
    results = {}
    for tid, tc in tests.items():
        print(f"    [{tid}] {tc['desc']}...", end=" ", flush=True)
        try:
            text, elapsed, out_tok = _send(tc["content"] + "\n\n" + tc["question"], max_tokens=300)
            pass_ = tc["expected"].lower() in text.lower()
            status = "✅" if pass_ else "❌"
            print(f"{status} {out_tok}tok {elapsed:.1f}s 含'{tc['expected']}' 回答'{text.strip()[:50]}'")
            results[tid] = {"pass": pass_, "elapsed": round(elapsed, 2),
                            "output_tokens": out_tok, "got": text.strip()[:200],
                            "expected": tc["expected"]}
        except Exception as e:
            print(f"❌ ERROR: {str(e)[:80]}")
            results[tid] = {"pass": False, "error": str(e)[:120]}
    return results


def summarize(results_by_dim, model_id):
    total = passed = 0
    per_dim = {}
    for dim, rs in results_by_dim.items():
        p = sum(1 for r in rs.values() if r.get("pass"))
        t = len(rs)
        total += t
        passed += p
        per_dim[dim] = {"passed": p, "total": t}
    score = passed / total * 100 if total else 0
    print("\n  " + "=" * 55)
    print(f"  评测模型: {model_id}")
    print("  " + "=" * 55)
    for dim, st in per_dim.items():
        print(f"    {dim}: {st['passed']}/{st['total']}")
    print(f"    总分: {passed}/{total} ({score:.0f}/100)")
    return {"per_dim": per_dim, "passed": passed, "total": total, "score": score}


def save_result(name, model_id, results_by_dim, summary):
    os.makedirs(RESULT_DIR, exist_ok=True)
    path = os.path.join(RESULT_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump({"model_id": model_id, "timestamp": datetime.now().isoformat(),
                   "results": results_by_dim, "summary": summary}, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ 已保存: {path}")
    return path


def get_model_id():
    try:
        req = urllib.request.Request(HOST + "/v1/models", headers=HEADERS)
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        models = [m["id"] for m in data.get("data", []) if not m["id"].startswith("claude") and m["id"] != "default"]
        return models[0] if models else "unknown"
    except Exception:
        return "unknown"


def load_result(name):
    path = os.path.join(RESULT_DIR, f"{name}.json")
    if not os.path.exists(path):
        print(f"  ❌ 结果不存在: {path}")
        sys.exit(1)
    with open(path) as f:
        return json.loads(f.read())


def compare(name1, name2):
    r1, r2 = load_result(name1), load_result(name2)
    print("\n" + "=" * 60)
    print("  量化变体质量对比")
    print("=" * 60)
    m1, m2 = r1.get("model_id", name1), r2.get("model_id", name2)
    print(f"  {name1}: {m1}  总分 {r1['summary']['score']}/100")
    print(f"  {name2}: {m2}  总分 {r2['summary']['score']}/100")
    dims = sorted(set(list(r1["results"].keys()) + list(r2["results"].keys())))
    for dim in dims:
        r1d, r2d = r1["results"].get(dim, {}), r2["results"].get(dim, {})
        p1 = sum(1 for x in r1d.values() if x.get("pass"))
        p2 = sum(1 for x in r2d.values() if x.get("pass"))
        sign = "=" if p1 == p2 else ("<" if p1 < p2 else ">")
        print(f"    {dim}: {name1}={p1}/{len(r1d)}  {name2}={p2}/{len(r2d)}  {sign}")
    print()
    for tid in sorted(set(k for d in r1["results"].values() for k in d) &
                      set(k for d in r2["results"].values() for k in d)):
        pass_set = set()
        for rn, r in [(name1, r1), (name2, r2)]:
            for dim, d in r["results"].items():
                if tid in d and d[tid].get("pass"):
                    pass_set.add(rn)
        if len(pass_set) == 1:
            only = (pass_set - {name1} and name2) or name1
            print(f"    ⚠️ {tid}: 仅 {only} 通过")
    # per-case detail diff
    print("\n  逐用例详情:")
    for rn, r in [(name1, r1), (name2, r2)]:
        print(f"    [{rn}] {r.get('model_id', rn)}")
        for dim, d in r["results"].items():
            for tid, tc in d.items():
                mark = "✅" if tc.get("pass") else "❌"
                extra = tc.get("detail") or tc.get("got", "")[:50] or tc.get("error", "")
                print(f"      {mark} {dim}/{tid}: {extra}")


def main():
    ap = argparse.ArgumentParser(description="量化敏感代码质量评测")
    ap.add_argument("--quick", action="store_true", help="快速模式(每维度2例)")
    ap.add_argument("--save", metavar="NAME", help="保存结果为 NAME.json")
    ap.add_argument("--compare", nargs=2, metavar=("NAME1", "NAME2"), help="对比两份保存结果")
    args = ap.parse_args()

    if args.compare:
        compare(args.compare[0], args.compare[1])
        return

    print("=" * 60)
    print("  量化敏感代码质量评测")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  端点: {HOST}")
    print("=" * 60)

    model_id = get_model_id()
    print(f"\n  模型: {model_id}")

    results_by_dim = {}
    results_by_dim["exec"] = run_exec_tests(quick=args.quick)
    results_by_dim["num"] = run_num_tests(quick=args.quick)
    results_by_dim["ctx"] = run_ctx_tests(quick=args.quick)

    summary = summarize(results_by_dim, model_id)

    if args.save:
        save_result(args.save, model_id, results_by_dim, summary)
    else:
        print("\n  使用 --save NAME 保存结果, 然后 --compare 对比两个变体")


if __name__ == "__main__":
    main()
