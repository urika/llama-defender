#!/usr/bin/env python3
"""
模型质量评测脚本
用于评估不同模型的: 代码生成、数学推理、指令遵循、格式正确性
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

HOST = os.environ.get("LLAMA_HOST", "http://127.0.0.1:4000")

CATEGORY_TEMPLATES = {
    # ============ 代码生成 ============
    "code_hello": {
        "category": "代码生成",
        "prompt": "写一个Python函数，计算斐波那契数列第n项，返回整数。",
        "expected": "def fib",
        "eval": "contains",
    },
    "code_sort": {
        "category": "代码生成", 
        "prompt": "写一个快速排序函数，要求原地排序，返回排序后的列表。",
        "expected": "def quicksort",
        "eval": "contains",
    },
    "code_class": {
        "category": "代码生成",
        "prompt": "写一个栈类 Stack，包含 push、pop、is_empty 方法。",
        "expected": "class Stack",
        "eval": "contains",
    },
    "code_recursive": {
        "category": "代码生成",
        "prompt": "写一个递归函数计算二叉树深度。",
        "expected": "def depth",
        "eval": "contains",
    },
    
    # ============ 数学推理 ============
    "math_basic": {
        "category": "数学推理",
        "prompt": "计算: 123 + 456 = ?",
        "expected": "579",
        "eval": "contains",
    },
    "math_word": {
        "category": "数学推理",
        "prompt": "小明有15个苹果，给了小红7个，又买了5个，现在小明有多少个苹果？",
        "expected": "13",
        "eval": "contains",
    },
    "math_prime": {
        "category": "数学推理",
        "prompt": "判断 17 是不是质数，并解释原因。",
        "expected": "是",
        "eval": "contains",
    },
    
    # ============ 指令遵循 ============
    "instruction_format": {
        "category": "指令遵循",
        "prompt": "用JSON格式返回你的名字和年龄，格式: {\"name\": \"...\", \"age\": ...}",
        "expected": '"name"',
        "eval": "json_valid",
    },
    "instruction_list": {
        "category": "指令遵循",
        "prompt": "列出3种水果，每行一个，用数字编号。",
        "expected": "1.",
        "eval": "contains",
    },
    "instruction_no_say": {
        "category": "指令遵循",
        "prompt": "回答问题时不要包含'是'这个字，直接给出答案。2+2等于多少？",
        "expected": "4",
        "eval": "not_contains",
        "avoid": "是",
    },
    
    # ============ 格式正确性 ============
    "format_json": {
        "category": "格式正确性",
        "prompt": "返回一个有效的JSON对象，包含字段: city=北京, population=2000万",
        "expected": "北京",
        "eval": "json_valid",
    },
    "format_markdown": {
        "category": "格式正确性",
        "prompt": "用Markdown表格展示: 苹果 红, 香蕉 黄, 葡萄 紫",
        "expected": "|",
        "eval": "contains",
    },
    
    # ============ 常识推理 ============
    "common_light": {
        "category": "常识推理",
        "prompt": "白天太阳在天空中，晚上看不到太阳的原因是什么？",
        "expected": "地球",
        "eval": "contains",
    },
    "common_water": {
        "category": "常识推理",
        "prompt": "水在0度会变成什么？",
        "expected": "冰",
        "eval": "contains",
    },
}

def call_api(prompt, max_tokens=200):
    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f"{HOST}/v1/messages",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": "sk-test",
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )
    
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            duration = time.time() - start
            content = ""
            if "content" in result:
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        content = block.get("text", "")
            return {"success": True, "content": content, "duration": duration}
    except Exception as e:
        return {"success": False, "error": str(e), "duration": time.time() - start}

def evaluate_response(response, test_case):
    eval_type = test_case["eval"]
    
    if not response.get("success"):
        return {"pass": False, "reason": f"API失败: {response.get('error')}"}
    
    content = response["content"]
    
    if eval_type == "contains":
        expected = test_case["expected"]
        if expected in content:
            return {"pass": True, "reason": f"包含'{expected}'"}
        return {"pass": False, "reason": f"未包含'{expected}'"}
    
    elif eval_type == "not_contains":
        avoid = test_case["avoid"]
        if avoid not in content:
            return {"pass": True, "reason": f"未包含'{avoid}'"}
        return {"pass": False, "reason": f"包含禁止词'{avoid}'"}
    
    elif eval_type == "json_valid":
        try:
            for line in content.split('\n'):
                line = line.strip()
                if line.startswith('{') and line.endswith('}'):
                    json.loads(line)
                    return {"pass": True, "reason": "有效的JSON"}
            json.loads(content)
            return {"pass": True, "reason": "有效的JSON"}
        except:
            return {"pass": False, "reason": "无效的JSON格式"}
    
    return {"pass": False, "reason": "未知的评估类型"}

def run_quality_bench():
    print("=" * 60)
    print("模型质量评测")
    print("=" * 60)
    print(f"目标: {HOST}")
    print()
    
    results = {}
    categories = {}
    
    for test_id, test_case in CATEGORY_TEMPLATES.items():
        category = test_case["category"]
        if category not in categories:
            categories[category] = {"total": 0, "passed": 0}
        
        print(f"[{test_id}] {category}...", end=" ", flush=True)
        categories[category]["total"] += 1
        
        response = call_api(test_case["prompt"])
        evaluation = evaluate_response(response, test_case)
        
        if evaluation["pass"]:
            categories[category]["passed"] += 1
            print("✅")
        else:
            print(f"❌ ({evaluation['reason']})")
        
        results[test_id] = {
            "case": test_case,
            "response": response,
            "evaluation": evaluation
        }
        
        time.sleep(0.5)
    
    print()
    print("=" * 60)
    print("评测结果汇总")
    print("=" * 60)
    
    total = 0
    passed = 0
    
    for category, stats in sorted(categories.items()):
        total += stats["total"]
        passed += stats["passed"]
        rate = stats["passed"] / stats["total"] * 100 if stats["total"] > 0 else 0
        print(f"  {category}: {stats['passed']}/{stats['total']} ({rate:.0f}%)")
    
    overall_rate = passed / total * 100 if total > 0 else 0
    print("-" * 40)
    print(f"  总计: {passed}/{total} ({overall_rate:.0f}%)")
    
    # 保存结果
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_file = f"logs/quality-bench-{timestamp}.json"
    os.makedirs("logs", exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": timestamp,
            "host": HOST,
            "categories": categories,
            "total": {"passed": passed, "total": total, "rate": overall_rate},
            "results": results
        }, f, ensure_ascii=False, indent=2)
    
    print()
    print(f"详细结果已保存: {output_file}")
    
    return overall_rate

if __name__ == "__main__":
    run_quality_bench()

# ============ 多模型对比函数 ============

def compare_models(model_configs, output_file="logs/model-quality-comparison.json"):
    """
    对比多个模型的质量评测结果
    
    model_configs: [{"name": "模型名称", "host": "http://host:port", "config": "配置名"}, ...]
    """
    print("=" * 60)
    print("多模型质量对比评测")
    print("=" * 60)
    print()
    
    all_results = {}
    
    for model_info in model_configs:
        name = model_info["name"]
        host = model_info["host"]
        
        print(f"评测模型: {name}")
        print(f"地址: {host}")
        
        # 临时设置 HOST
        old_host = os.environ.get("LLAMA_HOST")
        os.environ["LLAMA_HOST"] = host
        
        try:
            rate = run_quality_bench()
            all_results[name] = {"rate": rate, "host": host}
        except Exception as e:
            print(f"❌ 评测失败: {e}")
            all_results[name] = {"rate": 0, "error": str(e)}
        
        os.environ["LLAMA_HOST"] = old_host
        print()
    
    # 汇总对比
    print("=" * 60)
    print("质量评分对比")
    print("=" * 60)
    
    sorted_results = sorted(all_results.items(), key=lambda x: x[1].get("rate", 0), reverse=True)
    
    for rank, (name, result) in enumerate(sorted_results, 1):
        rate = result.get("rate", 0)
        error = result.get("error", "")
        if error:
            print(f"  {rank}. {name}: ERROR - {error}")
        else:
            print(f"  {rank}. {name}: {rate:.0f}%")
    
    # 保存结果
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "results": all_results,
            "ranking": [name for name, _ in sorted_results]
        }, f, ensure_ascii=False, indent=2)
    
    print()
    print(f"对比结果已保存: {output_file}")

if __name__ == "__main__" and len(sys.argv) > 1:
    if sys.argv[1] == "--compare":
        # 默认对比配置
        compare_models([
            {"name": "Gemma 4 26B", "host": "http://127.0.0.1:4000"},
        ])
