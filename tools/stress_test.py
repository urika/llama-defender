#!/usr/bin/env python3
"""
编程与文字处理压力测试脚本

针对已运行的模型服务 (通过 anthropic_proxy.py :4000) 进行压力测试，
覆盖编程生成、代码分析、长文本处理、创意写作、结构化输出等场景。

用法:
    python3 tools/stress_test.py                    # 默认顺序执行所有场景 1 次
    python3 tools/stress_test.py --concurrent 3     # 并发 3 个请求
    python3 tools/stress_test.py --iterations 5     # 每个场景重复 5 次
    python3 tools/stress_test.py --category code    # 仅运行编程类测试
    python3 tools/stress_test.py --category text    # 仅运行文字处理类测试
    python3 tools/stress_test.py --quick            # 快速模式 (减少 max_tokens)

依赖: Python 3 stdlib only
"""

import argparse
import http.client
import json
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 配置
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "4000"))
MODEL = os.environ.get("STRESS_MODEL", "claude-sonnet-4-6")
TIMEOUT = int(os.environ.get("STRESS_TIMEOUT", "180"))

# ============================================================
# 长文本素材 (用于长上下文测试)
# ============================================================

LONG_ARTICLE = """
人工智能（Artificial Intelligence，AI）亦称智械、机器智能，指由人制造出来的机器所表现出来的智能。
通常人工智能是指通过普通计算机程序来呈现人类智能的技术。通过医学、神经科学、机器人学及统计
学等的进步，有些预测则认为人类的无数职业也逐渐被人工智能取代。

人工智能的定义可以分为两部分，即"人工"和"智能"。"人工"比较好理解，争议性也不大。有时我们会
要考虑什么是人力所能及制造的，或者人自身的智能程度有没有高到可以创造人工智能的地步，等等。
但总的来说，"人工系统"就是通常意义下的人工系统。

关于什么是"智能"，就问题多多了。这涉及到其它诸如意识（CONSCIOUSNESS）、自我（SELF）、
思维（MIND）（包括无意识的思维（UNCONSCIOUS_MIND））等等问题。人唯一了解的智能是人本身的
智能，这是普遍认同的观点。但是我们对我们自身智能的理解都非常有限，对构成人的智能的必要元素也
了解有限，所以就很难定义什么是"人工"制造的"智能"了。因此人工智能的研究往往涉及对人的智能本身
的研究。其它关于动物或其它人造系统的智能也普遍被认为是人工智能相关的研究课题。

人工智能在计算机领域内，得到了愈加广泛的重视。并在机器人，经济政治决策，控制系统，仿真系统中
得到应用。尼尔逊教授对人工智能下了这样一个定义："人工智能是关于知识的学科――怎样表示知识以及
怎样获得知识并使用知识的科学。"而另一个美国麻省理工学院的温斯顿教授认为："人工智能就是研究如何
使计算机去做过去只有人才能做的智能工作。"这些说法反映了人工智能学科的基本思想和基本内容。即
人工智能是研究人类智能活动的规律，构造具有一定智能的人工系统，研究如何让计算机去完成以往需要
人的智力才能胜任的工作，也就是研究如何应用计算机的软硬件来模拟人类某些智能行为的基本理论、
方法和技术。

人工智能是计算机学科的一个分支，二十世纪七十年代以来被称为世界三大尖端技术之一（空间技术、
能源技术、人工智能）。也被认为是二十一世纪三大尖端技术（基因工程、纳米科学、人工智能）之一。
这是因为近三十年来它获得了迅速的发展，在很多学科领域都获得了广泛应用，并取得了丰硕的成果，
人工智能已逐步成为一个独立的分支，无论在理论和实践上都已自成一个系统。

人工智能是研究使计算机来模拟人的某些思维过程和智能行为（如学习、推理、思考、规划等）的学科，
主要包括计算机实现智能的原理、制造类似于人脑智能的计算机，使计算机能实现更高层次的应用。
人工智能将涉及到计算机科学、心理学、哲学和语言学等学科。可以说几乎是自然科学和社会科学的所有
学科，其范围已远远超出了计算机科学的范畴，人工智能与思维科学的关系是实践和理论的关系，人工智能
是处于思维科学的技术应用层次，是它的一个应用分支。从思维观点看，人工智能不仅限于逻辑思维，要
考虑形象思维、灵感思维才能促进人工智能的突破性的发展，数学常被认为是多种学科的基础科学，数学
也进入语言、思维领域，人工智能学科也必须借用数学工具，数学不仅在标准逻辑、模糊数学等范围发挥
作用，数学进入人工智能学科，它们将互相促进而更快地发展。

从实用观点来看，人工智能是一门知识工程学：以知识为对象，研究知识的获取、知识的表示方法和知识
的使用。人工智能的发展已经进入了一个新阶段，特别是大语言模型（LLM）的出现，使得自然语言处理
能力得到了质的飞跃。GPT、Claude、Qwen 等模型展现了惊人的文本理解和生成能力，能够进行复杂的
推理、编程、翻译和创作。

大语言模型的训练需要海量数据和巨大的计算资源。通常使用 Transformer 架构，通过自注意力机制来
捕捉文本中的长距离依赖关系。预训练阶段模型在大量无标注文本上学习语言的通用表示，然后通过
微调或提示工程来适应特定任务。近年来，混合专家模型（MoE）架构也越来越受到关注，它通过稀疏
激活的方式在保持推理效率的同时扩大模型容量。

人工智能的应用已经渗透到生活的方方面面：智能助手帮助人们管理日程和回答问题；推荐系统为用户
提供个性化的内容和商品；自动驾驶技术正在改变交通出行方式；医疗 AI 辅助医生进行疾病诊断和药物
研发；金融科技利用 AI 进行风险评估和欺诈检测。随着技术的不断进步，人工智能将在更多领域发挥
重要作用，同时也带来了伦理、隐私和就业等方面的挑战，需要社会各界共同思考和应对。
""".strip()

LONG_CODE_SNIPPET = '''
import random
import time

def process_data(data):
    result = []
    for i in range(len(data)):
        if data[i] % 2 == 0:
            result.append(data[i] * 2)
        else:
            result.append(data[i] * 3)
    return result

def fetch_user(user_id):
    # 模拟数据库查询
    time.sleep(0.1)
    if user_id < 0:
        return None
    return {"id": user_id, "name": "User" + str(user_id), "active": random.choice([True, False])}

def calculate_discount(price, user_type):
    if user_type == "vip":
        return price * 0.8
    elif user_type == "svip":
        return price * 0.6
    elif user_type == "normal":
        return price
    else:
        return price * 0.9

class Cache:
    def __init__(self):
        self.store = {}
    
    def get(self, key):
        return self.store.get(key)
    
    def set(self, key, value):
        self.store[key] = value
    
    def delete(self, key):
        if key in self.store:
            del self.store[key]

class DataProcessor:
    def __init__(self):
        self.cache = Cache()
        self.processed_count = 0
    
    def process_batch(self, items):
        output = []
        for item in items:
            cached = self.cache.get(item["id"])
            if cached:
                output.append(cached)
            else:
                processed = self._transform(item)
                self.cache.set(item["id"], processed)
                output.append(processed)
        self.processed_count += len(items)
        return output
    
    def _transform(self, item):
        # 复杂的转换逻辑
        temp = item.copy()
        temp["score"] = random.randint(1, 100)
        temp["timestamp"] = time.time()
        temp["hash"] = hash(str(temp))
        return temp

def main():
    processor = DataProcessor()
    test_data = [{"id": i, "value": random.random()} for i in range(100)]
    result = processor.process_batch(test_data)
    print(f"Processed {len(result)} items")
    
    # 一些奇怪的逻辑
    for r in result:
        if r["score"] > 80:
            print("High score:", r)
        elif r["score"] < 20:
            print("Low score:", r)
        else:
            pass

if __name__ == "__main__":
    main()
'''.strip()

# ============================================================
# 测试场景定义
# ============================================================

def make_anthropic_request(messages, max_tokens, tools=None, stream=True):
    """构造 Anthropic Messages API 请求体"""
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": stream,
    }
    if tools:
        body["tools"] = tools
    return body


SCENARIOS = {
    # ---------- 编程类 ----------
    "code_generation_complex": {
        "category": "code",
        "description": "复杂代码生成：实现一个线程安全的 LRU + LFU 组合缓存",
        "request": lambda: make_anthropic_request(
            messages=[{
                "role": "user",
                "content": (
                    "请用 Python 实现一个线程安全的缓存类，同时支持 LRU（最近最少使用）"
                    "和 LFU（最少使用频率）两种淘汰策略，并允许在初始化时选择使用哪种策略。"
                    "要求：\n"
                    "1. 使用 typing 类型注解\n"
                    "2. 包含完整的 docstring\n"
                    "3. 包含单元测试代码\n"
                    "4. 使用 threading.Lock 保证线程安全\n"
                    "5. O(1) 时间复杂度的 get 和 put 操作"
                )
            }],
            max_tokens=1200,
        ),
    },
    "code_analysis_long": {
        "category": "code",
        "description": "长代码分析：找出代码中的所有问题和改进点",
        "request": lambda: make_anthropic_request(
            messages=[{
                "role": "user",
                "content": (
                    f"请分析以下 Python 代码，找出所有潜在的问题（包括但不限于：\n"
                    f"性能问题、线程安全问题、内存泄漏、异常处理缺失、设计模式问题等），\n"
                    f"并对每个问题给出改进建议和修改后的代码：\n\n```python\n{LONG_CODE_SNIPPET}\n```"
                )
            }],
            max_tokens=1500,
        ),
    },
    "algorithm_design": {
        "category": "code",
        "description": "算法设计：分布式一致性算法",
        "request": lambda: make_anthropic_request(
            messages=[{
                "role": "user",
                "content": (
                    "请设计一个简化版的 Raft 一致性算法实现方案，包括：\n"
                    "1. 节点状态机设计（Follower/Candidate/Leader）\n"
                    "2. 选举流程的详细伪代码\n"
                    "3. 日志复制的流程图描述\n"
                    "4. 处理网络分区的策略\n"
                    "5. Python 实现的核心类结构"
                )
            }],
            max_tokens=1200,
        ),
    },
    "code_refactor": {
        "category": "code",
        "description": "代码重构：将混乱代码重构为清晰架构",
        "request": lambda: make_anthropic_request(
            messages=[{
                "role": "user",
                "content": (
                    "请将以下混乱的代码重构为清晰、可维护的面向对象设计。"
                    "使用策略模式、工厂模式和依赖注入。提供重构前后的对比说明：\n\n"
                    "```python\n"
                    "def handle_payment(method, amount, user):\n"
                    "    if method == 'credit':\n"
                    "        print('Processing credit card...')\n"
                    "        if amount > 1000:\n"
                    "            print('Need extra verification')\n"
                    "        return True\n"
                    "    elif method == 'paypal':\n"
                    "        print('Redirecting to PayPal...')\n"
                    "        return True\n"
                    "    elif method == 'crypto':\n"
                    "        print('Crypto payment')\n"
                    "        if user['verified']:\n"
                    "            return True\n"
                    "        else:\n"
                    "            return False\n"
                    "    else:\n"
                    "        print('Unknown method')\n"
                    "        return False\n"
                    "```"
                )
            }],
            max_tokens=1000,
        ),
    },
    "multi_step_coding": {
        "category": "code",
        "description": "多步推理编程：设计并优化数据库查询系统",
        "request": lambda: make_anthropic_request(
            messages=[{
                "role": "user",
                "content": (
                    "请帮我设计一个简单的内存数据库查询引擎，要求：\n"
                    "1. 支持 CREATE TABLE、INSERT、SELECT 基本语法\n"
                    "2. 支持 WHERE 条件过滤（等于、大于、小于、LIKE）\n"
                    "3. 支持简单的 JOIN 操作（Nested Loop Join）\n"
                    "4. 使用 B+ 树索引加速点查\n"
                    "5. 给出完整的类设计和核心方法的实现\n"
                    "6. 分析各操作的时间复杂度"
                )
            }],
            max_tokens=1500,
        ),
    },
    # ---------- 文字处理类 ----------
    "long_text_summary": {
        "category": "text",
        "description": "长文本摘要：对长文章提取关键信息",
        "request": lambda: make_anthropic_request(
            messages=[{
                "role": "user",
                "content": (
                    f"请对以下文章进行摘要，要求：\n"
                    f"1. 用 200 字左右概括核心观点\n"
                    f"2. 列出 5 个关键要点\n"
                    f"3. 评价文章逻辑的严密性\n\n"
                    f"文章：\n{LONG_ARTICLE}"
                )
            }],
            max_tokens=800,
        ),
    },
    "creative_writing": {
        "category": "text",
        "description": "创意写作：根据复杂设定写短篇故事",
        "request": lambda: make_anthropic_request(
            messages=[{
                "role": "user",
                "content": (
                    "请根据以下设定写一个 800 字左右的科幻短篇故事：\n"
                    "背景：2145 年，人类发现了一种可以储存意识的量子晶体。\n"
                    "主角：一位即将执行深空探索任务的宇航员，决定将意识备份到晶体中。\n"
                    "冲突：备份完成后，原始身体和备份意识都认为自己是'真正的自己'。\n"
                    "要求：情节有张力，人物心理描写细腻，结尾有反转。"
                )
            }],
            max_tokens=1200,
        ),
    },
    "structured_output": {
        "category": "text",
        "description": "结构化输出：生成复杂 JSON 报告",
        "request": lambda: make_anthropic_request(
            messages=[{
                "role": "user",
                "content": (
                    "请生成一份关于'新能源汽车行业发展趋势'的结构化分析报告，"
                    "严格使用以下 JSON 格式输出（不要包含 markdown 代码块标记）：\n\n"
                    "{\n"
                    "  \"executive_summary\": \"...\",\n"
                    "  \"market_size\": {\"global_2024\": \"...\", \"projected_2030\": \"...\", \"cagr\": \"...\"},\n"
                    "  \"key_players\": [\n"
                    "    {\"name\": \"...\", \"country\": \"...\", \"market_share\": \"...\", \"strengths\": [...]}\n"
                    "  ],\n"
                    "  \"technology_trends\": [\n"
                    "    {\"trend\": \"...\", \"description\": \"...\", \"impact_level\": \"high|medium|low\"}\n"
                    "  ],\n"
                    "  \"challenges\": [...],\n"
                    "  \"opportunities\": [...],\n"
                    "  \"policy_analysis\": {\"china\": \"...\", \"eu\": \"...\", \"us\": \"...\"},\n"
                    "  \"investment_recommendation\": \"...\"\n"
                    "}"
                )
            }],
            max_tokens=1000,
        ),
    },
    "translation": {
        "category": "text",
        "description": "长文本翻译：中英互译",
        "request": lambda: make_anthropic_request(
            messages=[{
                "role": "user",
                "content": (
                    f"请将以下中文文章翻译成英文，要求：\n"
                    f"1. 保持学术性写作风格\n"
                    f"2. 技术术语准确\n"
                    f"3. 语句通顺自然\n\n"
                    f"{LONG_ARTICLE}"
                )
            }],
            max_tokens=1500,
        ),
    },
    "multi_turn_simulation": {
        "category": "text",
        "description": "多轮对话上下文处理：模拟 6 轮对话",
        "request": lambda: make_anthropic_request(
            messages=[
                {"role": "user", "content": "我想学习 Python 编程，请问从何入手？"},
                {"role": "assistant", "content": "建议从基础语法开始，包括变量、数据类型、控制流和函数。推荐《Python编程：从入门到实践》这本书。"},
                {"role": "user", "content": "我已经学完了基础，接下来应该学什么？"},
                {"role": "assistant", "content": "接下来可以学习面向对象编程、异常处理、文件操作，然后选择一个方向深入学习，比如 Web 开发（Django/Flask）、数据分析（pandas/numpy）或自动化运维。"},
                {"role": "user", "content": "我对 Web 开发感兴趣，能推荐一个学习路线吗？"},
                {"role": "assistant", "content": "Web 开发路线建议：1) HTTP 协议基础；2) HTML/CSS/JavaScript 前端基础；3) Python Web 框架（Flask -> Django）；4) 数据库（SQL 基础 -> PostgreSQL/MySQL）；5) RESTful API 设计；6) 部署和运维（Docker、Linux）。"},
                {"role": "user", "content": "前面的学习中，你提到了异常处理和文件操作，能详细讲讲 Python 中如何正确处理文件读写异常吗？请给出具体的代码示例。"},
            ],
            max_tokens=800,
        ),
    },
}

# ============================================================
# 请求执行
# ============================================================

def send_request(body):
    """发送 Anthropic Messages API 请求，返回 (success, result_dict)"""
    t0 = time.time()
    ttft_ms = None
    full_text = ""
    full_json = None
    total_time_ms = None
    error = None
    event_count = 0
    token_count = 0

    try:
        conn = http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=TIMEOUT)
        headers = {
            "Content-Type": "application/json",
            "x-api-key": "stress-test",
            "anthropic-version": "2023-06-01",
        }
        conn.request("POST", "/v1/messages", body=json.dumps(body), headers=headers)
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

                    # TTFT: first content_block_delta or content with text
                    if ttft_ms is None:
                        if ev_type in ("content_block_delta", "message_delta"):
                            ttft_ms = (time.time() - t0) * 1000
                        elif ev_type == "content_block_start":
                            block = data.get("content_block", {})
                            if block.get("text") or block.get("type") == "text":
                                ttft_ms = (time.time() - t0) * 1000

                    # Accumulate text
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

                    # Token count from usage
                    usage = data.get("usage", {})
                    if usage:
                        token_count = max(token_count, usage.get("output_tokens", token_count))

                    # Stop reason
                    if ev_type == "message_delta":
                        delta = data.get("delta", {})
                        if delta.get("stop_reason"):
                            full_json = {"stop_reason": delta.get("stop_reason")}
        else:
            # Non-streaming
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
                ttft_ms = total_time_ms  # non-streaming: TTFT = total
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
        "stop_reason": full_json.get("stop_reason") if full_json else None,
    }


# ============================================================
# 测试执行器
# ============================================================

def log(msg):
    print(f"  {msg}", flush=True)


def header(msg):
    print(f"\n{'='*60}", flush=True)
    print(f"  {msg}", flush=True)
    print(f"{'='*60}", flush=True)


def subheader(msg):
    print(f"\n  --- {msg} ---", flush=True)


def run_single(scenario_key, iteration=1):
    """运行单个测试场景一次"""
    scenario = SCENARIOS[scenario_key]
    body = scenario["request"]()

    # quick mode: reduce max_tokens
    if getattr(run_single, "quick_mode", False):
        body["max_tokens"] = min(body.get("max_tokens", 500), 400)

    result = send_request(body)
    result["scenario"] = scenario_key
    result["iteration"] = iteration
    result["description"] = scenario["description"]
    result["category"] = scenario["category"]
    return result


def run_sequential(scenario_keys, iterations, quick_mode=False):
    """顺序执行所有场景"""
    run_single.quick_mode = quick_mode
    results = []
    for key in scenario_keys:
        desc = SCENARIOS[key]["description"]
        subheader(f"[{key}] {desc}")
        for i in range(iterations):
            log(f"  迭代 {i+1}/{iterations}...")
            r = run_single(key, i + 1)
            results.append(r)
            status = "✅" if r["success"] else "❌"
            tok_info = f"tok={r['token_count']}" if r['token_count'] else "tok=N/A"
            ttft_str = f"{r['ttft_ms']:.0f}ms" if r['ttft_ms'] is not None else "N/A"
            total_str = f"{r['total_ms']:.0f}ms" if r['total_ms'] is not None else "N/A"
            log(f"  {status} TTFT={ttft_str} 总={total_str} "
                f"字={r['text_length']} {tok_info} "
                f"({r.get('error', '')})")
    return results


def run_concurrent(scenario_keys, iterations, concurrency, quick_mode=False):
    """并发执行场景"""
    run_single.quick_mode = quick_mode
    # Build task list
    tasks = []
    for key in scenario_keys:
        for i in range(iterations):
            tasks.append((key, i + 1))

    results = []
    completed = 0
    total = len(tasks)

    subheader(f"并发测试: {concurrency} 并发, 共 {total} 个任务")

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_task = {
            executor.submit(run_single, key, it): (key, it)
            for key, it in tasks
        }
        for future in as_completed(future_to_task):
            key, it = future_to_task[future]
            try:
                r = future.result()
            except Exception as e:
                r = {
                    "success": False, "error": str(e),
                    "scenario": key, "iteration": it,
                    "description": SCENARIOS[key]["description"],
                    "category": SCENARIOS[key]["category"],
                    "ttft_ms": None, "total_ms": None,
                    "text_length": 0, "token_count": 0, "event_count": 0,
                }
            results.append(r)
            completed += 1
            status = "✅" if r["success"] else "❌"
            tok_info = f"tok={r['token_count']}" if r['token_count'] else "tok=N/A"
            log(f"[{completed}/{total}] {status} [{key}] TTFT={r['ttft_ms']:.0f}ms "
                f"总={r['total_ms']:.0f}ms 字={r['text_length']} {tok_info}")

    return results


# ============================================================
# 结果汇总
# ============================================================

def print_report(results):
    """打印测试报告"""
    header("📊 压力测试报告")

    # Group by scenario
    by_scenario = defaultdict(list)
    for r in results:
        by_scenario[r["scenario"]].append(r)

    # Group by category
    by_category = defaultdict(list)
    for r in results:
        by_category[r["category"]].append(r)

    # Summary table by scenario
    print("\n  按场景汇总:", flush=True)
    print(f"  {'场景':<30} {'成功':>4} {'失败':>4} {'平均TTFT':>10} {'平均耗时':>10} "
          f"{'平均字数':>8} {'成功率':>6}", flush=True)
    print("  " + "-" * 82, flush=True)

    for key in sorted(by_scenario.keys()):
        runs = by_scenario[key]
        successes = [r for r in runs if r["success"]]
        failures = [r for r in runs if not r["success"]]
        avg_ttft = sum(r["ttft_ms"] for r in successes if r["ttft_ms"]) / len([r for r in successes if r["ttft_ms"]]) if successes else 0
        avg_total = sum(r["total_ms"] for r in successes if r["total_ms"]) / len([r for r in successes if r["total_ms"]]) if successes else 0
        avg_len = sum(r["text_length"] for r in successes) / len(successes) if successes else 0
        rate = len(successes) / len(runs) * 100 if runs else 0
        desc = SCENARIOS[key]["description"][:28]
        print(f"  {desc:<30} {len(successes):>4} {len(failures):>4} "
              f"{avg_ttft:>9.0f}ms {avg_total:>9.0f}ms {avg_len:>7.0f} {rate:>5.0f}%", flush=True)

    # Category summary
    print("\n  按类别汇总:", flush=True)
    print(f"  {'类别':<12} {'成功':>4} {'失败':>4} {'平均TTFT':>10} {'平均耗时':>10} "
          f"{'平均字数':>8} {'成功率':>6}", flush=True)
    print("  " + "-" * 64, flush=True)

    for cat in ["code", "text"]:
        runs = by_category.get(cat, [])
        if not runs:
            continue
        successes = [r for r in runs if r["success"]]
        failures = [r for r in runs if not r["success"]]
        avg_ttft = sum(r["ttft_ms"] for r in successes if r["ttft_ms"]) / len([r for r in successes if r["ttft_ms"]]) if successes else 0
        avg_total = sum(r["total_ms"] for r in successes if r["total_ms"]) / len([r for r in successes if r["total_ms"]]) if successes else 0
        avg_len = sum(r["text_length"] for r in successes) / len(successes) if successes else 0
        rate = len(successes) / len(runs) * 100 if runs else 0
        cat_name = "编程" if cat == "code" else "文字处理"
        print(f"  {cat_name:<12} {len(successes):>4} {len(failures):>4} "
              f"{avg_ttft:>9.0f}ms {avg_total:>9.0f}ms {avg_len:>7.0f} {rate:>5.0f}%", flush=True)

    # Overall
    all_success = [r for r in results if r["success"]]
    all_fail = [r for r in results if not r["success"]]
    total = len(results)
    print(f"\n  总计: {len(all_success)}/{total} 成功 ({len(all_success)/total*100:.1f}%)", flush=True)
    if all_fail:
        print(f"  失败场景:", flush=True)
        for r in all_fail[:5]:
            print(f"    - {r['scenario']} (iter={r['iteration']}): {r.get('error', 'unknown')}", flush=True)

    # Performance percentiles
    if all_success:
        ttfts = sorted([r["ttft_ms"] for r in all_success if r["ttft_ms"]])
        totals = sorted([r["total_ms"] for r in all_success if r["total_ms"]])
        print(f"\n  TTFT 分位数: P50={percentile(ttfts, 50):.0f}ms P90={percentile(ttfts, 90):.0f}ms P99={percentile(ttfts, 99):.0f}ms", flush=True)
        print(f"  总耗时 分位数: P50={percentile(totals, 50):.0f}ms P90={percentile(totals, 90):.0f}ms P99={percentile(totals, 99):.0f}ms", flush=True)


def percentile(sorted_arr, p):
    if not sorted_arr:
        return 0
    k = (len(sorted_arr) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_arr) - 1)
    if f == c:
        return sorted_arr[f]
    return sorted_arr[f] + (k - f) * (sorted_arr[c] - sorted_arr[f])


def save_results(results):
    result_file = os.path.join(REPO_ROOT, "logs", f"stress-test-results-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(result_file), exist_ok=True)
    with open(result_file, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log(f"📝 详细结果已保存到: {result_file}")


# ============================================================
# Main
# ============================================================

def main():
    global PROXY_HOST, PROXY_PORT
    parser = argparse.ArgumentParser(description="编程与文字处理压力测试")
    parser.add_argument("--concurrent", type=int, default=0, metavar="N",
                        help="并发请求数 (默认 0=顺序执行)")
    parser.add_argument("--iterations", type=int, default=1, metavar="N",
                        help="每个场景重复次数 (默认 1)")
    parser.add_argument("--category", choices=["code", "text", "all"], default="all",
                        help="仅运行指定类别的测试")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式 (减少 max_tokens)")
    parser.add_argument("--host", default=PROXY_HOST, help="代理主机")
    parser.add_argument("--port", type=int, default=PROXY_PORT, help="代理端口")
    args = parser.parse_args()

    PROXY_HOST = args.host
    PROXY_PORT = args.port

    # Select scenarios
    if args.category == "all":
        scenario_keys = list(SCENARIOS.keys())
    else:
        scenario_keys = [k for k, v in SCENARIOS.items() if v["category"] == args.category]

    header("🚀 编程与文字处理压力测试")
    print(f"  代理地址: http://{PROXY_HOST}:{PROXY_PORT}", flush=True)
    print(f"  模型: {MODEL}", flush=True)
    print(f"  模式: {'并发 ' + str(args.concurrent) if args.concurrent > 0 else '顺序'}", flush=True)
    print(f"  迭代: {args.iterations}", flush=True)
    print(f"  场景: {len(scenario_keys)} 个", flush=True)
    print(f"  快速模式: {'是' if args.quick else '否'}", flush=True)

    # Preflight check
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
    t_start = time.time()
    if args.concurrent > 0:
        results = run_concurrent(scenario_keys, args.iterations, args.concurrent, args.quick)
    else:
        results = run_sequential(scenario_keys, args.iterations, args.quick)
    t_end = time.time()

    # Report
    print_report(results)
    print(f"\n  总耗时: {t_end - t_start:.1f}s", flush=True)

    # Save
    save_results(results)

    # Exit code
    failed = sum(1 for r in results if not r["success"])
    if failed > 0:
        print(f"\n⚠️  {failed} 个测试失败", flush=True)
        sys.exit(1)
    else:
        print("\n✅ 全部测试通过", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
