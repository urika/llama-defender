#!/usr/bin/env python3
"""
长上下文压力测试脚本

测试模型在 50KB / 100KB 输入上下文下的性能表现，
包括 prompt processing 时间 (TTFT)、生成稳定性、内存变化等。

用法:
    python3 tools/context_stress_test.py              # 测试所有场景
    python3 tools/context_stress_test.py --size 50k   # 仅测试 50KB
    python3 tools/context_stress_test.py --size 100k  # 仅测试 100KB
    python3 tools/context_stress_test.py --quick      # 快速模式 (减少生成token)

依赖: Python 3 stdlib only
"""

import argparse
import http.client
import json
import os
import subprocess
import sys
import time

# ============================================================
# 配置
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "4000"))
MODEL = os.environ.get("STRESS_MODEL", "claude-sonnet-4-6")
TIMEOUT = int(os.environ.get("STRESS_TIMEOUT", "300"))

# ============================================================
# 文本素材生成
# ============================================================

BASE_PARAGRAPH = """人工智能（Artificial Intelligence）是计算机科学的一个分支，旨在创建能够模拟人类智能的系统。
机器学习是 AI 的核心技术之一，它使计算机能够从数据中学习模式而无需显式编程。深度学习作为机器学习的子集，
使用多层神经网络来处理复杂的数据表示。Transformer 架构自 2017 年提出以来，彻底改变了自然语言处理领域，
其自注意力机制能够捕捉文本中的长距离依赖关系。大语言模型（LLM）如 GPT、Claude、Qwen 等基于 Transformer，
通过在海量文本上预训练获得了强大的语言理解和生成能力。混合专家模型（MoE）通过稀疏激活机制在保持推理效率的
同时大幅扩展模型容量，成为当前大模型发展的重要方向。在实际应用中，AI 已广泛渗透到医疗诊断、金融风控、
自动驾驶、代码辅助、内容创作等领域，极大地提升了生产效率。然而，大模型的部署也面临着计算资源消耗高、
推理延迟大、幻觉问题等挑战，需要持续的算法优化和工程创新来推动技术落地。"""

CODE_CHUNK = """def process_data_batch(items, config):
    \"\"\"Process a batch of data items with given configuration.\"\"\"
    results = []
    cache = {}
    for idx, item in enumerate(items):
        if item['id'] in cache:
            results.append(cache[item['id']])
            continue
        try:
            transformed = apply_transform(item, config['transform_type'])
            validated = validate_output(transformed, config['schema'])
            cache[item['id']] = validated
            results.append(validated)
        except ValidationError as e:
            log_error(f"Validation failed for item {item['id']}: {e}")
            results.append({'id': item['id'], 'error': str(e)})
        except TransformError as e:
            log_error(f"Transform failed for item {item['id']}: {e}")
            results.append({'id': item['id'], 'error': str(e)})
    return results

class DataPipeline:
    def __init__(self, stages, max_workers=4):
        self.stages = stages
        self.max_workers = max_workers
        self.metrics = {'processed': 0, 'failed': 0, 'latency': []}

    def run(self, dataset):
        for stage in self.stages:
            dataset = self._execute_stage(stage, dataset)
        return dataset

    def _execute_stage(self, stage, data):
        start = time.time()
        output = stage.process(data)
        self.metrics['latency'].append(time.time() - start)
        self.metrics['processed'] += len(output)
        return output
"""

def generate_text(target_chars: int, variant: str = "text") -> str:
    """生成指定字符数的长文本"""
    base = BASE_PARAGRAPH if variant == "text" else CODE_CHUNK
    # 为每次重复添加变化，避免完全重复
    parts = []
    count = 0
    section = 1
    while count < target_chars:
        if variant == "text":
            part = f"\n【第{section}节】\n{base}\n"
        else:
            part = f"\n# Section {section}\n{base}\n"
        parts.append(part)
        count += len(part)
        section += 1
    result = "".join(parts)
    # 精确截断到目标长度
    if len(result) > target_chars:
        result = result[:target_chars]
    return result


def format_size(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}KB"
    return f"{n}B"


# ============================================================
# 测试场景
# ============================================================

def make_request(messages, max_tokens, stream=True):
    return {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": stream,
    }


SCENARIOS = {}

# 动态生成场景
def build_scenarios():
    global SCENARIOS

    # 50KB text
    text_50k = generate_text(50_000, "text")
    SCENARIOS["text_summary_50k"] = {
        "size": 50000,
        "description": "50KB 长文本摘要",
        "request": make_request([{
            "role": "user",
            "content": f"请对以下长文进行摘要，提炼出核心观点和关键结论（不超过300字）：\n\n{text_50k}"
        }], 400),
    }

    SCENARIOS["document_qa_50k"] = {
        "size": 50000,
        "description": "50KB 文档问答",
        "request": make_request([{
            "role": "user",
            "content": (
                f"以下是一篇关于人工智能的长文档。请回答：\n"
                f"1. 文档中提到的核心技术有哪些？\n"
                f"2. 文章最后三节主要讲了什么？\n\n"
                f"文档内容：\n\n{text_50k}"
            )
        }], 400),
    }

    # 100KB text
    text_100k = generate_text(100_000, "text")
    SCENARIOS["text_summary_100k"] = {
        "size": 100000,
        "description": "100KB 长文本摘要",
        "request": make_request([{
            "role": "user",
            "content": f"请对以下长文进行摘要，提炼出核心观点和关键结论（不超过300字）：\n\n{text_100k}"
        }], 400),
    }

    SCENARIOS["document_qa_100k"] = {
        "size": 100000,
        "description": "100KB 文档问答",
        "request": make_request([{
            "role": "user",
            "content": (
                f"以下是一篇关于人工智能的长文档。请回答：\n"
                f"1. 文档中提到的核心技术有哪些？\n"
                f"2. 文章最后三节主要讲了什么？\n\n"
                f"文档内容：\n\n{text_100k}"
            )
        }], 400),
    }

    # 50KB code
    code_50k = generate_text(50_000, "code")
    SCENARIOS["code_review_50k"] = {
        "size": 50000,
        "description": "50KB 长代码审查",
        "request": make_request([{
            "role": "user",
            "content": (
                f"请审查以下大型代码片段，找出其中的架构问题、性能瓶颈和安全隐患，"
                f"并给出改进建议：\n\n```python\n{code_50k}\n```"
            )
        }], 500),
    }

    # 100KB code
    code_100k = generate_text(100_000, "code")
    SCENARIOS["code_review_100k"] = {
        "size": 100000,
        "description": "100KB 长代码审查",
        "request": make_request([{
            "role": "user",
            "content": (
                f"请审查以下大型代码片段，找出其中的架构问题、性能瓶颈和安全隐患，"
                f"并给出改进建议：\n\n```python\n{code_100k}\n```"
            )
        }], 500),
    }


# ============================================================
# 请求执行
# ============================================================

def send_request(body):
    """发送 Anthropic Messages API 请求，返回 result_dict"""
    t0 = time.time()
    ttft_ms = None
    full_text = ""
    total_time_ms = None
    error = None
    event_count = 0
    token_count = 0
    req_size = len(json.dumps(body, ensure_ascii=False))

    try:
        conn = http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=TIMEOUT)
        headers = {
            "Content-Type": "application/json",
            "x-api-key": "stress-test",
            "anthropic-version": "2023-06-01",
        }
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        conn.request("POST", "/v1/messages", body=raw_body, headers=headers)
        resp = conn.getresponse()

        if body.get("stream"):
            buffer = ""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")

                while "data: " in buffer:
                    idx = buffer.find("data: ")
                    end = buffer.find("\n\n", idx)
                    if end == -1:
                        break
                    line = buffer[idx + 6:end].strip()
                    buffer = buffer[end + 2:]

                    if line == "[DONE]":
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_count += 1
                    ev_type = data.get("type", "")

                    if ttft_ms is None:
                        if ev_type in ("content_block_delta", "message_delta"):
                            ttft_ms = (time.time() - t0) * 1000
                        elif ev_type == "content_block_start":
                            block = data.get("content_block", {})
                            if block.get("text") or block.get("type") == "text":
                                ttft_ms = (time.time() - t0) * 1000

                    if ev_type == "content_block_delta":
                        delta = data.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            full_text += text
                    elif ev_type == "content_block_start":
                        block = data.get("content_block", {})
                        text = block.get("text", "")
                        if text:
                            full_text += text

                    usage = data.get("usage", {})
                    if usage:
                        token_count = max(token_count, usage.get("output_tokens", token_count))
        else:
            raw = resp.read().decode("utf-8", errors="replace")
            total_time_ms = (time.time() - t0) * 1000
            try:
                full_json = json.loads(raw)
                content = full_json.get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        full_text += block.get("text", "")
                usage = full_json.get("usage", {})
                token_count = usage.get("output_tokens", 0)
                ttft_ms = total_time_ms
            except json.JSONDecodeError as e:
                error = f"JSON decode error: {e}"

        total_time_ms = (time.time() - t0) * 1000
        conn.close()

        if resp.status != 200:
            error = f"HTTP {resp.status}"

    except Exception as e:
        total_time_ms = (time.time() - t0) * 1000
        error = str(e)

    return {
        "success": error is None and len(full_text) > 0,
        "error": error,
        "ttft_ms": ttft_ms,
        "total_ms": total_time_ms,
        "text_length": len(full_text),
        "token_count": token_count,
        "event_count": event_count,
        "request_chars": req_size,
    }


# ============================================================
# 系统监控
# ============================================================

def get_memory_info():
    """获取内存信息，返回 (used_gb, total_gb, percent)"""
    try:
        result = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        page_size = 4096
        stats = {}
        for line in lines:
            if ":" in line:
                key, val = line.split(":", 1)
                val = val.strip().replace(".", "").replace(" ", "")
                try:
                    stats[key.strip()] = int(val)
                except ValueError:
                    pass

        # vm_stat on macOS
        free_pages = stats.get("Pages free", 0)
        active_pages = stats.get("Pages active", 0)
        inactive_pages = stats.get("Pages inactive", 0)
        speculative_pages = stats.get("Pages speculative", 0)
        wired_pages = stats.get("Pages wired down", 0)
        compressed_pages = stats.get("Pages occupied by compressor", 0)

        used_pages = active_pages + inactive_pages + speculative_pages + wired_pages + compressed_pages
        total_pages = used_pages + free_pages

        used_gb = used_pages * page_size / (1024 ** 3)
        total_gb = total_pages * page_size / (1024 ** 3)
        percent = (used_pages / total_pages * 100) if total_pages > 0 else 0
        return used_gb, total_gb, percent
    except Exception:
        return None, None, None


def get_llama_memory_mb():
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(get_llama_pid())],
            capture_output=True, text=True, timeout=5
        )
        return int(result.stdout.strip()) / 1024
    except Exception:
        return None


def get_llama_pid():
    try:
        result = subprocess.run(
            ["pgrep", "-f", "llama-server"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                return int(line.strip())
    except Exception:
        pass
    return None


# ============================================================
# 测试执行
# ============================================================

def log(msg):
    print(f"  {msg}", flush=True)


def header(msg):
    print(f"\n{'='*60}", flush=True)
    print(f"  {msg}", flush=True)
    print(f"{'='*60}", flush=True)


def subheader(msg):
    print(f"\n  --- {msg} ---", flush=True)


def run_scenario(key, quick_mode=False):
    scenario = SCENARIOS[key]
    body = scenario["request"]
    if quick_mode:
        body["max_tokens"] = min(body.get("max_tokens", 500), 300)

    req_chars = len(json.dumps(body, ensure_ascii=False))
    log(f"请求大小: {format_size(req_chars)} (目标上下文: {format_size(scenario['size'])})")

    # 记录测试前内存
    mem_before = get_memory_info()
    llama_mem_before = get_llama_memory_mb()

    result = send_request(body)
    result["scenario"] = key
    result["description"] = scenario["description"]
    result["target_size"] = scenario["size"]

    # 记录测试后内存
    mem_after = get_memory_info()
    llama_mem_after = get_llama_memory_mb()

    if mem_before[0] is not None and mem_after[0] is not None:
        result["mem_used_before_gb"] = round(mem_before[0], 1)
        result["mem_used_after_gb"] = round(mem_after[0], 1)
        result["mem_delta_gb"] = round(mem_after[0] - mem_before[0], 2)
    else:
        result["mem_used_before_gb"] = None
        result["mem_used_after_gb"] = None
        result["mem_delta_gb"] = None

    if llama_mem_before is not None and llama_mem_after is not None:
        result["llama_mem_before_mb"] = round(llama_mem_before, 1)
        result["llama_mem_after_mb"] = round(llama_mem_after, 1)
        result["llama_mem_delta_mb"] = round(llama_mem_after - llama_mem_before, 1)
    else:
        result["llama_mem_before_mb"] = None
        result["llama_mem_after_mb"] = None
        result["llama_mem_delta_mb"] = None

    return result


def print_report(results):
    header("📊 长上下文压力测试报告")

    print("\n  详细结果:", flush=True)
    print(f"  {'场景':<25} {'输入':>8} {'TTFT':>10} {'总耗时':>10} {'生成字':>8} {'内存变化':>10} {'llama变化':>10}", flush=True)
    print("  " + "-" * 90, flush=True)

    for r in results:
        desc = r["description"][:24]
        status = "✅" if r["success"] else "❌"
        size = format_size(r["target_size"])
        ttft = f"{r['ttft_ms']:.0f}ms" if r["ttft_ms"] else "N/A"
        total = f"{r['total_ms']:.0f}ms" if r["total_ms"] else "N/A"
        gen_len = r["text_length"]
        mem_delta = f"{r['mem_delta_gb']:+.1f}GB" if r["mem_delta_gb"] is not None else "N/A"
        llama_delta = f"{r['llama_mem_delta_mb']:+.0f}MB" if r["llama_mem_delta_mb"] is not None else "N/A"
        print(f"  {desc:<25} {size:>8} {ttft:>10} {total:>10} {gen_len:>8} {mem_delta:>10} {llama_delta:>10}", flush=True)

    # 分组对比
    print("\n  按上下文大小汇总:", flush=True)
    for size_label, size_val in [("50KB", 50000), ("100KB", 100000)]:
        runs = [r for r in results if r["target_size"] == size_val and r["success"]]
        if not runs:
            continue
        avg_ttft = sum(r["ttft_ms"] for r in runs if r["ttft_ms"]) / len([r for r in runs if r["ttft_ms"]])
        avg_total = sum(r["total_ms"] for r in runs if r["total_ms"]) / len([r for r in runs if r["total_ms"]])
        avg_gen = sum(r["text_length"] for r in runs) / len(runs)
        avg_llama_delta = sum(r["llama_mem_delta_mb"] for r in runs if r["llama_mem_delta_mb"] is not None) / len([r for r in runs if r["llama_mem_delta_mb"] is not None]) if any(r["llama_mem_delta_mb"] is not None for r in runs) else 0
        print(f"    {size_label}: 平均 TTFT={avg_ttft:.0f}ms, 平均总耗时={avg_total:.0f}ms, "
              f"平均生成={avg_gen:.0f}字, llama内存变化={avg_llama_delta:+.0f}MB", flush=True)

    failed = [r for r in results if not r["success"]]
    if failed:
        print(f"\n  ❌ 失败场景:", flush=True)
        for r in failed:
            print(f"    - {r['description']}: {r.get('error', 'unknown')}", flush=True)


def save_results(results):
    result_file = os.path.join(REPO_ROOT, "logs", f"context-stress-test-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(result_file), exist_ok=True)
    with open(result_file, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log(f"📝 结果已保存: {result_file}")


# ============================================================
# Main
# ============================================================

def main():
    global PROXY_HOST, PROXY_PORT
    parser = argparse.ArgumentParser(description="长上下文压力测试")
    parser.add_argument("--size", choices=["50k", "100k", "all"], default="all",
                        help="仅测试指定大小的上下文")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式 (减少 max_tokens)")
    parser.add_argument("--host", default=PROXY_HOST, help="代理主机")
    parser.add_argument("--port", type=int, default=PROXY_PORT, help="代理端口")
    args = parser.parse_args()

    PROXY_HOST = args.host
    PROXY_PORT = args.port

    # Build scenarios
    build_scenarios()

    # Select scenarios
    all_keys = list(SCENARIOS.keys())
    if args.size == "50k":
        scenario_keys = [k for k in all_keys if "50k" in k]
    elif args.size == "100k":
        scenario_keys = [k for k in all_keys if "100k" in k]
    else:
        scenario_keys = all_keys

    header("📚 长上下文压力测试")
    print(f"  代理地址: http://{PROXY_HOST}:{PROXY_PORT}", flush=True)
    print(f"  模型: {MODEL}", flush=True)
    print(f"  场景: {len(scenario_keys)} 个", flush=True)
    print(f"  快速模式: {'是' if args.quick else '否'}", flush=True)

    # Preflight
    log("检查代理可用性...")
    try:
        conn = http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=10)
        conn.request("GET", "/v1/models")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status != 200:
            print(f"❌ 代理返回 HTTP {resp.status}", file=sys.stderr)
            sys.exit(1)
        log("✅ 代理正常")
    except Exception as e:
        print(f"❌ 无法连接代理: {e}", file=sys.stderr)
        sys.exit(1)

    # Run tests
    results = []
    t_start = time.time()
    for key in scenario_keys:
        desc = SCENARIOS[key]["description"]
        subheader(f"[{key}] {desc}")
        r = run_scenario(key, args.quick)
        results.append(r)
        status = "✅" if r["success"] else "❌"
        mem_info = f"系统内存{r['mem_delta_gb']:+.1f}GB" if r['mem_delta_gb'] is not None else ""
        llama_info = f"llama+{r['llama_mem_delta_mb']:.0f}MB" if r['llama_mem_delta_mb'] is not None else ""
        ttft_str = f"{r['ttft_ms']:.0f}ms" if r['ttft_ms'] is not None else "N/A"
        total_str = f"{r['total_ms']:.0f}ms" if r['total_ms'] is not None else "N/A"
        log(f"{status} TTFT={ttft_str} 总={total_str} "
            f"生成={r['text_length']}字 ({mem_info} {llama_info})")
        if r["error"]:
            log(f"  错误: {r['error']}")
        # 每个场景之间短暂冷却
        time.sleep(3)

    t_end = time.time()

    print_report(results)
    print(f"\n  总耗时: {t_end - t_start:.1f}s", flush=True)
    save_results(results)

    failed = sum(1 for r in results if not r["success"])
    if failed > 0:
        print(f"\n⚠️  {failed} 个测试失败", flush=True)
        sys.exit(1)
    else:
        print("\n✅ 全部测试通过", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
