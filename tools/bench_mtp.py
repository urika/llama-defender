#!/usr/bin/env python3
"""
MTP 模型性能测评脚本

测评 unsloth/Qwen3.6-27B-MTP-GGUF 和 unsloth/Qwen3.6-35B-A3B-MTP-GGUF，
对比不同 --spec-draft-n-max (1-4) 以及非 MTP 基线的生成速度。

用法:
    python3 tools/bench_mtp.py                    # 完整测评 (需下载模型, 耗时较长)
    python3 tools/bench_mtp.py --quick             # 快速测评 (仅 draft-n-max=2)
    python3 tools/bench_mtp.py --model 27b         # 仅测评 27B
    python3 tools/bench_mtp.py --model 35b         # 仅测评 35B
    python3 tools/bench_mtp.py --baseline          # 仅测评非 MTP 基线

依赖: Python 3 stdlib only (http.client, json, time, subprocess, argparse)
"""

import argparse
import http.client
import json
import os
import signal
import subprocess
import sys
import time
import shutil
from collections import defaultdict

# ============================================================
# 配置
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# llama-server 二进制路径 (优先使用源码编译版，支持 MTP)
LLAMA_SERVER_BIN = os.environ.get(
    "LLAMA_SERVER_BIN",
    "/tmp/llama.cpp/build/bin/llama-server",
)

# 端口配置
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8082  # 使用独立端口避免冲突

# 测评模型
MODELS = {
    "27b-mtp": {
        "name": "Qwen3.6-27B-MTP",
        "model_id": "unsloth/Qwen3.6-27B-MTP-GGUF:UD-Q4_K_XL",
        "type": "dense",
    },
    "35b-mtp": {
        "name": "Qwen3.6-35B-A3B-MTP",
        "model_id": "unsloth/Qwen3.6-35B-A3B-MTP-GGUF:UD-Q4_K_XL",
        "type": "moe",
    },
}

# 测评模型 (非 MTP 基线)
BASELINE_MODELS = {
    "35b": {
        "name": "Qwen3.6-35B-A3B (baseline, no MTP)",
        "model_id": "unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS",
        "type": "moe",
    },
}

# 本地模型目录 (优先使用已下载的本地文件，避免重复下载)
LOCAL_MODELS_DIR = os.environ.get(
    "LOCAL_MODELS_DIR",
    os.path.join(REPO_ROOT, "models"),
)

# 本地模型文件映射
LOCAL_MODEL_FILES = {
    "27b-mtp": "Qwen3.6-27B-MTP-UD-Q4_K_XL.gguf",
    "35b-mtp": "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf",
}

# MTP draft-n-max 值
DRAFT_N_VALUES = [1, 2, 3, 4]
DRAFT_N_QUICK = [2]

# 评测 Prompt
PROMPTS = {
    "simple": {
        "description": "简单问候 (1 token)",
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "max_tokens": 20,
    },
    "code": {
        "description": "代码生成 (100 tokens)",
        "messages": [{"role": "user", "content": "Write a Python function fibonacci(n) that returns the nth Fibonacci number. Only output the code, no explanation."}],
        "max_tokens": 100,
    },
    "reasoning": {
        "description": "逻辑推理 (200 tokens)",
        "messages": [{"role": "user", "content": "If a bat and a ball cost $1.10 in total, and the bat costs $1.00 more than the ball, how much does the ball cost? Explain step by step."}],
        "max_tokens": 200,
    },
    "long_code": {
        "description": "长代码生成 (500 tokens)",
        "messages": [{"role": "user", "content": "Write a complete Python implementation of a binary search tree with insert, delete, search, and inorder traversal methods. Include type hints and docstrings."}],
        "max_tokens": 500,
    },
}

# 每个 prompt 的重复次数
REPEAT = 3
REPEAT_QUICK = 1


# ============================================================
# 工具函数
# ============================================================

def log(msg):
    print(f"  {msg}", flush=True)


def header(msg):
    print(f"\n{'='*60}", flush=True)
    print(f"  {msg}", flush=True)
    print(f"{'='*60}", flush=True)


def subheader(msg):
    print(f"\n  --- {msg} ---", flush=True)


def check_llama_server():
    """检查 llama-server 是否可用"""
    bin_path = LLAMA_SERVER_BIN
    if not shutil.which(bin_path):
        print(f"❌ llama-server 不可用: {bin_path}")
        print(f"   可通过 LLAMA_SERVER_BIN 环境变量指定路径")
        sys.exit(1)
    # 检查版本是否支持 --spec-type draft-mtp
    result = subprocess.run([bin_path, "--help"], capture_output=True, text=True)
    if "draft-mtp" not in result.stdout and "draft-mtp" not in result.stderr:
        print(f"⚠️  警告: {bin_path} 不支持 --spec-type draft-mtp")
        print("   请使用 2026年5月13日之后从源码编译的版本")
        sys.exit(1)
    print(f"✅ {bin_path} 支持 MTP")


def wait_for_server(port, timeout=900):
    """等待服务器就绪（含模型下载时间），返回 True/False"""
    start = time.time()
    last_log_check = 0
    logfile = os.path.join(REPO_ROOT, "logs", "bench-mtp.log")
    while time.time() - start < timeout:
        try:
            conn = http.client.HTTPConnection(BACKEND_HOST, port, timeout=5)
            conn.request("GET", "/v1/models")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(3)
        elapsed = int(time.time() - start)

        # 每 30 秒检查日志，显示下载进度或最后的日志行
        if elapsed - last_log_check >= 30 and os.path.exists(logfile):
            last_log_check = elapsed
            try:
                with open(logfile, "r") as f:
                    lines = f.readlines()
                    # 显示最近 3 行有内容的日志
                    recent = [l.strip() for l in lines[-5:] if l.strip() and "ggml_" not in l]
                    if recent:
                        log(f"[{elapsed}s] {recent[-1][:120]}")
            except Exception:
                pass

        if elapsed % 60 == 0 and elapsed > 0:
            log(f"等待服务器就绪... ({elapsed//60}分{elapsed%60}s)")


def kill_server():
    """停止可能残留的 llama-server 进程"""
    subprocess.run(["pkill", "-f", f"llama-server.*{BACKEND_PORT}"],
                   capture_output=True)
    time.sleep(2)


def resolve_model_path(model_key, model_id):
    """解析模型路径：优先使用本地文件，否则用 HuggingFace ID"""
    local_file = LOCAL_MODEL_FILES.get(model_key)
    if local_file:
        local_path = os.path.join(LOCAL_MODELS_DIR, local_file)
        if os.path.exists(local_path) and os.path.getsize(local_path) > 1024 * 1024 * 100:
            return "local", local_path
    return "hf", model_id


def start_server(model_key, model_id, extra_args, port):
    """启动 llama-server，返回进程对象"""
    model_type, model_path = resolve_model_path(model_key, model_id)
    if model_type == "local":
        log(f"使用本地模型: {model_path}")
        model_flag = "-m"
    else:
        log(f"使用 HF 模型: {model_path}")
        model_flag = "-hf"

    args = [
        LLAMA_SERVER_BIN,
        model_flag, model_path,
        "--host", BACKEND_HOST,
        "--port", str(port),
        "-c", "32768",       # 小上下文加速启动
        "-b", "2048",
        "-ub", "512",
        "-t", "8",
        "--cache-type-k", "q8_0",
        "--cache-type-v", "q8_0",
        "--temp", "0.6",
        "--top-p", "0.95",
        "--top-k", "20",
        "--presence-penalty", "0.0",
        "--min-p", "0.0",
        "--chat-template-kwargs", '{"enable_thinking":false}',
        "--jinja",
        "--flash-attn", "on",
        "--fit", "on",
    ] + extra_args

    log(f"启动 llama-server...")
    log(f"  模型: {model_id}")
    log(f"  端口: {port}")
    log(f"  额外参数: {' '.join(extra_args) if extra_args else '(无)'}")

    logfile = open(os.path.join(REPO_ROOT, "logs", "bench-mtp.log"), "w")
    proc = subprocess.Popen(
        args,
        stdout=logfile,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    return proc


def stop_server(proc):
    """停止 llama-server 进程"""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=15)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(2)


def run_chat_completion(messages, max_tokens, port, temperature=0.6):
    """发送 OpenAI chat completion 请求，返回 (response_json, ttft_ms, total_time_ms)"""
    body = {
        "model": "default",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    ttft_ms = None
    total_time_ms = None
    full_content = ""
    prompt_tokens = 0
    completion_tokens = 0
    t0 = time.time()

    try:
        conn = http.client.HTTPConnection(BACKEND_HOST, port, timeout=120)
        conn.request("POST", "/v1/chat/completions",
                     body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()

        buffer = ""
        while True:
            chunk = resp.read(1024)
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
                    break

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                choices = data.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    if "content" in delta and delta["content"]:
                        if ttft_ms is None:
                            ttft_ms = (time.time() - t0) * 1000
                        full_content += delta["content"]

                # llama-server reports token counts in "timings" field
                timings = data.get("timings", {})
                if timings:
                    if "prompt_n" in timings:
                        prompt_tokens = timings["prompt_n"]
                    if "predicted_n" in timings:
                        completion_tokens = timings["predicted_n"]

                # Fallback: OpenAI-style "usage" field
                usage = data.get("usage", {})
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage.get("completion_tokens", completion_tokens)

        total_time_ms = (time.time() - t0) * 1000
        conn.close()

    except Exception as e:
        total_time_ms = (time.time() - t0) * 1000
        return {"error": str(e), "content": full_content}, ttft_ms, total_time_ms

    result = {
        "content": full_content,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    return result, ttft_ms, total_time_ms


def run_benchmark(model_key, model_id, extra_args, port, prompts, repeat, label):
    """运行一组测评"""
    header(f"测评: {label}")

    proc = start_server(model_key, model_id, extra_args, port)

    if not wait_for_server(port):
        log("❌ 服务器启动超时")
        stop_server(proc)
        return None

    log("✅ 服务器就绪")

    results = {}

    for prompt_key, prompt_def in prompts.items():
        subheader(prompt_def["description"])

        prompt_results = []
        for i in range(repeat):
            log(f"第 {i+1}/{repeat} 次... ")
            resp, ttft, total_ms = run_chat_completion(
                prompt_def["messages"],
                prompt_def["max_tokens"],
                port,
            )

            if "error" in resp:
                log(f"❌ 错误: {resp['error']}")
                continue

            comp_tokens = resp.get("completion_tokens", 0)
            gen_tps = (comp_tokens / (total_ms - (ttft or 0))) * 1000 if ttft and comp_tokens > 0 else 0
            overall_tps = (comp_tokens / total_ms) * 1000 if total_ms and comp_tokens > 0 else 0

            log(f"TTFT={ttft:.0f}ms 总={total_ms:.0f}ms 生成={comp_tokens}tok "
                f"速度={gen_tps:.1f} t/s (总={overall_tps:.1f} t/s)")

            prompt_results.append({
                "ttft_ms": round(ttft, 1) if ttft else None,
                "total_ms": round(total_ms, 1),
                "completion_tokens": comp_tokens,
                "gen_tps": round(gen_tps, 1),
                "overall_tps": round(overall_tps, 1),
            })

        if prompt_results:
            # 计算平均
            avg_ttft = sum(r["ttft_ms"] for r in prompt_results if r["ttft_ms"]) / len([r for r in prompt_results if r["ttft_ms"]]) if any(r["ttft_ms"] for r in prompt_results) else 0
            avg_total = sum(r["total_ms"] for r in prompt_results) / len(prompt_results)
            avg_gen = sum(r["gen_tps"] for r in prompt_results) / len(prompt_results)
            avg_overall = sum(r["overall_tps"] for r in prompt_results) / len(prompt_results)
            avg_tokens = sum(r["completion_tokens"] for r in prompt_results) / len(prompt_results)

            log(f"📊 平均: TTFT={avg_ttft:.0f}ms 总={avg_total:.0f}ms "
                f"生成={avg_tokens:.0f}tok 速度={avg_gen:.1f} t/s")

            results[prompt_key] = {
                "avg_ttft_ms": round(avg_ttft, 1),
                "avg_total_ms": round(avg_total, 1),
                "avg_gen_tps": round(avg_gen, 1),
                "avg_overall_tps": round(avg_overall, 1),
                "avg_tokens": round(avg_tokens, 1),
                "runs": prompt_results,
            }

    stop_server(proc)
    log("🛑 服务器已停止")
    return results


def print_summary(all_results):
    """打印汇总对比表"""
    header("📊 测评汇总")

    # 按模型和 draft-n 列出关键指标
    print(f"\n{'模型':<35} {'Draft N':<8} {'Prompt':<16} {'TTFT(ms)':<10} {'耗时(ms)':<10} {'Token':<8} {'生成t/s':<10}", flush=True)
    print("-" * 100, flush=True)

    for model_label, model_data in all_results.items():
        if model_data is None:
            continue
        for config_label, config_results in model_data.items():
            if config_results is None:
                continue
            for prompt_key, r in config_results.items():
                if r is None:
                    continue
                print(f"{model_label:<35} {config_label:<8} {prompt_key:<16} "
                      f"{r['avg_ttft_ms']:<10.0f} {r['avg_total_ms']:<10.0f} "
                      f"{r['avg_tokens']:<8.0f} {r['avg_gen_tps']:<10.1f}", flush=True)

    # 重点对比: draft-n-max=2 的各模型
    print(f"\n{'='*60}")
    print("  🔑 关键对比: draft-n-max=2 代码生成速度")
    print(f"{'='*60}")

    for model_label, model_data in all_results.items():
        if model_data is None:
            continue
        for config_label, config_results in model_data.items():
            if config_results is None or config_label not in ("draft-2", "baseline"):
                continue
            for prompt_key in ("code", "long_code"):
                if prompt_key in config_results:
                    r = config_results[prompt_key]
                    if r:
                        print(f"  {model_label:<30} [{config_label}] {prompt_key:<12}: "
                              f"{r['avg_gen_tps']:.1f} t/s  (TTFT: {r['avg_ttft_ms']:.0f}ms)", flush=True)


def main():
    parser = argparse.ArgumentParser(description="MTP 模型性能测评")
    parser.add_argument("--quick", action="store_true", help="快速测评 (仅 draft-n-max=2, 不重复)")
    parser.add_argument("--model", choices=["27b", "35b", "all"], default="all", help="测评哪个模型")
    parser.add_argument("--baseline", action="store_true", help="仅测评非 MTP 基线")
    parser.add_argument("--draft-n", type=str, help="指定 draft-n-max 值, 逗号分隔 (如 '2,3')")
    parser.add_argument("--port", type=int, default=BACKEND_PORT, help="后端端口")
    args = parser.parse_args()

    port = args.port
    repeat = REPEAT_QUICK if args.quick else REPEAT
    prompts = PROMPTS

    print("=" * 60, flush=True)
    print("  MTP 模型性能测评", flush=True)
    print("=" * 60, flush=True)
    print(f"  快速模式: {'是' if args.quick else '否'} (每 prompt 重复 {repeat} 次)", flush=True)
    print(f"  后端端口: {port}", flush=True)
    print(f"  测评项目: {len(prompts)} 个 prompt", flush=True)

    check_llama_server()

    # 确保没有残留进程
    kill_server()

    all_results = defaultdict(dict)

    # 确定测评的模型和 draft-n 值
    models_to_test = MODELS.copy()
    if args.model == "27b":
        models_to_test = {"27b-mtp": MODELS["27b-mtp"]}
    elif args.model == "35b":
        models_to_test = {"35b-mtp": MODELS["35b-mtp"]}

    draft_n_values = DRAFT_N_QUICK if args.quick else DRAFT_N_VALUES
    if args.draft_n:
        draft_n_values = [int(x.strip()) for x in args.draft_n.split(",")]

    # 1. 测评 MTP 模型 (不同 draft-n-max)
    if not args.baseline:
        for model_key, model_info in models_to_test.items():
            for n in draft_n_values:
                config_label = f"draft-{n}"
                extra_args = [
                    "--spec-type", "draft-mtp",
                    "--spec-draft-n-max", str(n),
                ]
                label = f"{model_info['name']} (draft-n-max={n})"

                result = run_benchmark(
                    model_key,
                    model_info["model_id"],
                    extra_args,
                    port,
                    prompts,
                    repeat,
                    label,
                )
                all_results[model_info['name']][config_label] = result

                # 短暂冷却
                time.sleep(3)

    # 2. 测评基线模型 (非 MTP)
    if args.baseline or not args.quick:
        for model_key, model_info in BASELINE_MODELS.items():
            extra_args = []  # 无 MTP 参数
            label = model_info['name']

            result = run_benchmark(
                model_key,
                model_info["model_id"],
                extra_args,
                port,
                prompts,
                repeat,
                label,
            )
            all_results[model_info['name']]["baseline"] = result
            time.sleep(3)

    # 3. 打印汇总
    print_summary(all_results)

    # 4. 保存结果
    result_file = os.path.join(REPO_ROOT, "logs", f"bench-mtp-results-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(result_file), exist_ok=True)
    with open(result_file, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    log(f"📝 结果已保存到: {result_file}")

    print("\n✅ 测评完成", flush=True)


if __name__ == "__main__":
    main()
