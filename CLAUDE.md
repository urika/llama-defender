# CLAUDE.md

> **本文件是薄指针层（2026-10-17 重构）**：规范权威 = [`AGENTS.md`](AGENTS.md)（会话自动注入），
> 明细 = `docs/`。**禁止在本文件复制 AGENTS.md/docs 的内容**——更新只改 AGENTS.md 或对应 docs 文件，
> 否则会重演双真相源漂移（重构前的 CLAUDE.md 曾同时存有「haiku 永不云端」「ornith-oq4e active」等
> 已过期快照）。Claude-Code 特有、且无处安放的内容才允许写在这里。

## 核心原则（不可违反）

- Claude Code 永远只连 `http://127.0.0.1:4000`；后端/模型/本地云切换全部在代理层完成。
- **不要修改 Claude Code 自身配置**（`~/.claude/settings.local.json`、`ANTHROPIC_BASE_URL` 等）。
- **引擎重启协调（硬规则）**：多会话共享单引擎——重启 = 杀所有在途请求 + 清空 KV 缓存。
  重启前三查并公告（①批跑进程 ②引擎日志 10 分钟内推理事件 ③代理状态）；批跑期间引擎冻结；
  OOM/挂死可先斩后奏但须登记 changelog。完整协议：
  [`docs/05-operations-changelog/engine-restart-coordination-20260906.md`](docs/05-operations-changelog/engine-restart-coordination-20260906.md)

## 快速索引（按任务找文件）

| 任务 | 去处 |
|------|------|
| 项目概述 / 技术栈 / 命令 / 测试 / 风险 / 检查清单 | [`AGENTS.md`](AGENTS.md) §1-§9 |
| 模块清单 / 管线 25 stage / 旁路系统 / 配置文件明细 / 工具清单 / Admin API 表 / 源码构建 llama-server | [`docs/02-architecture-design/codebase-reference-20261017.md`](docs/02-architecture-design/codebase-reference-20261017.md) |
| 配置参数推荐组合（九张表） | [`docs/06-reference-metrics/config-parameters-guide-20261017.md`](docs/06-reference-metrics/config-parameters-guide-20261017.md) |
| 当前激活配置 / 后端形态 | `readlink configs/active.conf` + `./manage.sh current`（配置切换频繁，**不写入任何文档**） |
| 故障 / workaround | [`docs/06-reference-metrics/TROUBLESHOOTING.md`](docs/06-reference-metrics/TROUBLESHOOTING.md) |
| 缺陷登记 | [`docs/04-analysis-diagnostics/DEFECT-LIST.md`](docs/04-analysis-diagnostics/DEFECT-LIST.md) |
| 9B 质量门 / haiku 路由 | [`docs/06-reference-metrics/ornith-9b-capability-research-20261015.md`](docs/06-reference-metrics/ornith-9b-capability-research-20261015.md) |
| 跨仓协调（swe-eval） | `~/APP/swe-eval/docs/joint-coordination-llamacpp.md` |

## Claude Code 会话操作备忘

- 测试：`bash test/run_tests.sh --unit`（日常）；改 `pipeline.py`/`anthropic_proxy.py` 跑 `--all`。
- 提交钩子：`.githooks/pre-commit`（unit/signature/snapshot/ctx-case；批期自动跳 promptfoo）。
- 共享工作树：常有并行会话在途改动——**提交一律用 pathspec**（`git commit -- <files>`），
  `git status` 先看暂存区是否混入他人文件。
- config 里的 `active` 指针、批次窗口、引擎占用的最新状态以 **运行时探针**（`./manage.sh status`、
  `ps`、`/api/status`）为准，任何文档快照都可能过期。

## 本文件维护规则

1. 只允许三类内容：核心原则、Claude-Code 特有备忘、指针表。
2. 发现本文件与 AGENTS.md/docs 冲突时：**以 AGENTS.md/docs 为准**，并删除本文件的过期副本。
3. 新增「无处安放」的 Claude 特有知识时先问一句：它是否其实属于 AGENTS.md 或 docs/ 某文件？
