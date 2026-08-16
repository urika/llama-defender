# SWE-bench Pro 测评（独立项目 ~/APP/swe-eval）

> 状态：2026-08-15 已搭骨架，Pilot 尚未开跑。
> 本文档只讲接口层；项目本体、脚本、结果口径全部在 `~/APP/swe-eval`（本仓库之外）。

## 为什么独立成项目

1. **stdlib-only 约束**：本仓库 Python 代码禁止第三方依赖，而 SWE-bench Pro 拉取（`datasets`/`huggingface_hub`）、仓库克隆、测试执行都需要完整 Python 生态。
2. **产物隔离**：评测要克隆多 GB 的 GitHub 仓库、生成大量中间产物，不适合混入 llama.cpp 工作树。
3. **生命周期**：评测项目按「阶段 1 Pilot → 阶段 2 全量」推进，节奏与代理开发解耦。

## 项目结构（~/APP/swe-eval）

```
README.md              项目文档（定位/口径/两阶段计划/roadmap）
config/targets.yaml    被测端点：llama-defender-38（本地臂）/ llama-defender-cloud（云臂）
config/tasks.yaml      任务子集配置（dataset/filter/selection/repo_test_cmds）
scripts/fetch_tasks.py 拉取 ScaleAI/SWE-bench_Pro + 过滤 + 抽样 → tasks.json
scripts/run_agent.py   Claude Code headless 编排 + patch 提取 → results/runs.jsonl
scripts/evaluate.py    应用 patch + fail_to_pass/pass_to_pass 原生测试 → 回填 verdict
scripts/report.py      resolve rate + 墙钟/token/成本/路由分布 → results/report.md
```

## 快速开始

```bash
cd ~/APP/swe-eval
python3 -m venv .venv && source .venv/bin/activate
pip install datasets huggingface_hub pyyaml
python3 scripts/fetch_tasks.py                      # → tasks.json（Pilot: 2 仓库 × 3 任务）
python3 scripts/run_agent.py --target llama-defender-38 --only <instance_id>   # 1 任务试跑
python3 scripts/evaluate.py --all
python3 scripts/report.py
```

## 与本系统的接口约定（重要）

- **`ANTHROPIC_BASE_URL=http://127.0.0.1:4000`** —— 完全遵守本仓库核心原则，代理层切换后端，不碰 Claude Code 配置。
- **每个任务独立 `X-Claude-Code-Session-Id`**（前缀 `swe38-`/`swecloud-`）——避免会话状态（循环检测、路由粘性）跨任务污染。
- **本地臂必须每请求强制 `X-Proxy-Route-To: local`** —— 否则长上下文任务会被 SmartRouter 静默切到云端，A/B 臂混淆。经 `ANTHROPIC_CUSTOM_HEADERS` 下发（脚本内 TODO 实测调通）。
- 结果从 R8 响应头 / `logs/proxy_metrics.jsonl` 回填：`X-Proxy-Route-Target/Actual-Model/Reason/Cost` → `usage`/`cost_cny`/`proxy_events`，用于路由分布统计。

## 两阶段计划

| 阶段 | 规模 | 环境 | 目标 |
|------|------|------|------|
| 1 Pilot | 6 任务（2 仓库 × 3）× 2 臂 | 原生 macOS（本机） | 调通链路、验证「35B 与 3.8 质量是否有别」的假设方向、测出每任务成本 |
| 2 全量 | 30–60 任务 × 2 臂 | x86 runner（QEMU 过慢）或 ARM64 原生镜像 | 有统计意义的 resolve rate 对比 |

## 口径声明

**内部 A/B 对比专用，非官方 leaderboard 可比**：Qwen 官方 61.7% 来自自建 harness（Claude Code + 精炼任务集）；本评测原生 macOS 环境与 SWE-bench Pro 官方 amd64 Docker 存在环境差异，两臂同环境保证内部一致性。

## 结果回灌

Pilot 完成后，结果摘要回灌本仓库 `BENCHMARK.md`（与 bench_quality.py 质量对比并列），本文档更新状态与结论。
