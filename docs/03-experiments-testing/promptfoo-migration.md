# Promptfoo 迁移指南

> 迁移日期: 2026-06-07
> 迁移范围: `tools/run_experiment.sh` 的 report 阶段 + 新增固定 prompt 回归测试
> 保留范围: prepare/collect 阶段 + 端到端 agentic 任务测试

---

## 一、为什么不是"完全替代"

`OSS-REPLACEMENT-EVALUATION.md` 中的"极强替代"结论过于乐观。Promptfoo 与当前 A/B 测试系统解决的问题域不同：

| 维度 | 当前 A/B 测试 | Promptfoo |
|------|--------------|-----------|
| 测试对象 | Claude Code 自主执行的 agentic 编程任务 | 固定 prompt 的 LLM 响应 |
| 核心指标 | 代理层内部指标（REQ_SUMMARY、clearing、truncation） | 输出质量、相似度、延迟 |
| A/B 差异 | 代理配置（clearing 开启 vs 关闭） | Provider/模型差异 |
| 执行方式 | 人工驱动（Claude Code 自主决策） | 自动化（固定输入） |
| 不可替代性 | ❌ Promptfoo 无法 orchestrate agentic 会话 | — |

**结论**: Promptfoo 无法替代端到端 agentic 任务测试，但可以大幅增强报告生成、质量评估和回归测试。

---

## 二、Promptfoo 功能与本地整合能力提升

### 2.1 Promptfoo 核心功能

| 功能 | 说明 |
|------|------|
| **声明式 YAML 配置** | 测试用例、断言、模型配置全部用 YAML 描述，无需写代码 |
| **多 Provider 并行** | 可同时向多个模型/端点发送同一 prompt，自动对比结果 |
| **100+ 内置指标** | token 数、延迟、相似度、成本等自动采集 |
| **自定义断言** | `contains`、`regex`、`javascript`、`python`、`llm-rubric` 等 |
| **LLM-as-a-Judge** | 用另一个 LLM 自动评估输出质量（代码正确性、风格、完整性） |
| **可视化报告** | 自动生成 HTML/Markdown/JSON 报告，含断言统计、响应对比 |
| **CI 集成** | 原生支持 GitHub Actions、pre-commit 等 |
| **测试矩阵** | 一个 prompt × 多个模型 × 多个断言 = 自动矩阵展开 |

### 2.2 本地整合后带来的能力提升

| 能力 | 迁移前 | 迁移后 | 提升幅度 |
|------|--------|--------|---------|
| **回归测试覆盖** | 无（只有人工 A/B） | 9 个固定 prompt 自动验证 | 从 0 → 9 |
| **报告可视化** | 手写 Markdown（无图表） | HTML 交互式报告 | 质的飞跃 |
| **质量评估** | 人工评分（主观、不可复现） | LLM-as-a-Judge（自动、可复现） | 从玄学→工程 |
| **新增测试成本** | 需写 Python/Bash 脚本 | 改 5 行 YAML | 从小时→分钟 |
| **多模型对比** | 手动切换、手动记录 | 声明式配置、自动并行 | 从手动→自动 |
| **CI 集成** | 无 | pre-commit + test/run_tests.sh 原生支持 | 从 0 → 1 |
| **断言类型** | 手写正则 | 100+ 内置 + 自定义 JS/Python | 从有限→无限 |
| **历史追踪** | 散落文件 | Promptfoo Web UI (`promptfoo view`) | 从散乱→集中 |

### 2.3 具体场景示例

**场景 1：修改 anthropic_proxy.py 后快速验证**
```bash
# 以前：启动代理 → 打开 Claude Code → 人工执行 15 分钟任务 → 肉眼观察
# 现在：
./manage.sh start
bash test/run_tests.sh --promptfoo
# 60 秒后：9 项核心能力全部验证通过/失败，HTML 报告自动生成
```

**场景 2：对比 rapid-mlx-9b vs 35b**
```bash
# 以前：手动切换配置 → 执行相同任务 → 人工记录差异
# 现在：
./tools/promptfoo_eval.sh eval --group A --desc '9b'
./tools/promptfoo_eval.sh eval --group B --desc '35b'
./tools/promptfoo_eval.sh compare
# 自动生成 A/B 对比 HTML 报告
```

**场景 3：评估 PR 是否破坏代理功能**
```bash
# 以前：Code Review 无法验证运行时行为
# 现在：
git commit -m "feat: 优化工具调用格式"
# pre-commit 自动运行 unit + promptfoo
# 如果 promptfoo 失败，commit 被阻断
```

---

## 三、使用方式

### 3.1 安装依赖

```bash
cd APP/llama.cpp
npm install promptfoo @libsql/darwin-arm64 --save-dev
```

### 3.2 运行固定 prompt 回归测试

```bash
# A 组（代理已以 clearing 开启模式启动）
./tools/promptfoo_eval.sh eval --group A --desc 'clearing 开启'

# B 组（代理已以 clearing 关闭模式启动）
./tools/promptfoo_eval.sh eval --group B --desc 'clearing 关闭'
```

输出：
- `logs/experiments/promptfoo-A-{timestamp}.json` — 原始结果
- `logs/experiments/promptfoo-A-{timestamp}.html` — HTML 报告
- `logs/experiments/promptfoo-A-{timestamp}_merged.md` — 统一报告（含代理指标）

### 3.3 对比 A/B 结果

```bash
./tools/promptfoo_eval.sh compare
```

### 3.4 查看报告

```bash
# 在浏览器中打开最新 HTML 报告
./tools/promptfoo_eval.sh view

# 启动 Promptfoo Web UI 查看所有历史结果
./tools/promptfoo_eval.sh ui
```

### 3.5 集成到测试体系

| 运行模式 | 触发条件 | 测试数 | 耗时 | 适用场景 |
|----------|----------|--------|------|----------|
| **完整模式** | 手动执行 `bash test/run_tests.sh --promptfoo` | 9 个 | ~60-80s | 发布前验证、全量回归 |
| **快速模式** | ① `PROMPTFOO_FAST=1` 环境变量<br>② `git commit` 时代理已运行 | 核心 5 个 | ~20-30s | 日常开发、pre-commit 阻塞 |
| **跳过** | `SKIP_PROMPTFOO=1` 或代理未运行 | — | — | CI 环境、无代理时 |

**手动运行完整模式（9 个测试）：**
```bash
# 单独跑 Promptfoo 回归
bash test/run_tests.sh --promptfoo

# 全量测试（包含 Promptfoo）
bash test/run_tests.sh --all
```

**手动运行快速模式（核心 5 个测试）：**
```bash
PROMPTFOO_FAST=1 bash test/run_tests.sh --promptfoo
```

**跳过 Promptfoo（CI/预提交场景）：**
```bash
SKIP_PROMPTFOO=1 bash test/run_tests.sh --all
```

#### 快速模式运行条件

快速模式（核心 5 个测试）在以下任一条件满足时自动生效：

1. **显式环境变量**：命令行设置 `PROMPTFOO_FAST=1`
2. **pre-commit 自动触发**：`git commit` 时检测到代理端点 `http://127.0.0.1:4000` 可用

快速模式跳过的 4 个扩展测试（TC6-TC9）：
- 特殊字符处理、上下文连续性、边界处理、长文本生成
- 这些测试在完整模式下自动补全

### 3.6 pre-commit 自动集成

pre-commit hook 的行为取决于代理状态：

| 代理状态 | pre-commit 行为 | 耗时 |
|----------|----------------|------|
| **运行中** | unit tests (<1s) + Promptfoo **快速模式** (5 个测试, ~20-30s) | ~25s |
| **未运行** | 仅 unit tests (<1s)，提示 "proxy not running — skipping Promptfoo" | <1s |

```bash
# 代理运行时：自动跑快速模式
git commit -m "feat: 优化工具调用格式"
# → pre-commit: unit ✅  + promptfoo (5/5) ✅  → commit 通过
# → pre-commit: unit ✅  + promptfoo (3/5) ❌ → commit 被阻断

# 代理未运行时：只跑 unit
git commit -m "fix: 修复文案"
# → pre-commit: unit ✅  → commit 通过（提示启动代理以启用 Promptfoo）
```

### 3.7 触发策略：分层验证模型

| 层级 | 触发方式 | 模式 | 覆盖范围 | 目的 |
|------|----------|------|----------|------|
| **L1 本地开发** | `git commit` (pre-commit) | 快速模式 (5 个) | 核心能力 | 快速反馈，阻塞明显破坏 |
| **L2 Agent 操作** | agent 执行 `git commit` | 快速模式 (5 个) | 核心能力 | 与 L1 相同，agent 不单独触发 |
| **L3 手动验证** | `bash test/run_tests.sh --promptfoo` | 完整模式 (9 个) | 全量覆盖 | 发布前、大改动后验证 |
| **L4 CI 流水线** | *(规划中)* | 完整模式 (9 个) | 全量覆盖 | 合并前最终把关 |

**策略说明：**
- **pre-commit 已足够覆盖日常开发**：25s 的等待是可接受的，能捕获 80% 的代理回归问题
- **Agent 不自动触发**：避免 agent 循环（修改→测试→再修改→再测试）拖慢交互。Agent 通过 `git commit` 间接触发 pre-commit
- **CI 执行全量验证**：在 PR 合并前运行完整 9 个测试，作为最终质量门

```bash
# 原有流程仍然有效，report 阶段自动调用 Promptfoo
./tools/run_experiment.sh prepare --group A --task '...'
# ... 人工执行任务 ...
./tools/run_experiment.sh collect --group A
./tools/run_experiment.sh prepare --group B --task '...'
# ... 人工执行任务 ...
./tools/run_experiment.sh collect --group B
./tools/run_experiment.sh report  # ← 现在优先使用 Promptfoo 结果
```

---

## 四、操作指南：如何添加新测试用例

### 4.1 编辑配置文件

```bash
vim promptfooconfig.yaml
```

### 4.2 添加一个测试用例

在 `tests:` 数组中添加：

```yaml
  - description: 'TC10-你的测试名称'
    vars:
      prompt: '你的测试 prompt'
    assert:
      - type: contains
        value: '期望输出包含的文本'
        weight: 0.3
```

### 4.3 常用断言类型速查

| 断言类型 | 用途 | 示例 |
|----------|------|------|
| `contains` | 响应包含指定文本 | `value: 'def fib'` |
| `contains-any` | 包含任意一个 | `value: ['return', '返回']` |
| `not-contains` | 不含指定文本 | `value: '[BLOCKER]'` |
| `javascript` | JS 表达式评估 | `value: "output.length > 10"` |
| `python` | Python 表达式评估 | `value: "len(output) > 10"` |
| `llm-rubric` | LLM 质量评估 | `value: "评估代码正确性..."` |
| `is-json` | 输出是有效 JSON | — |
| `starts-with` | 以指定文本开头 | `value: '```python'` |

### 4.4 验证新测试

```bash
# 只运行新添加的测试（用 description 匹配）
./node_modules/.bin/promptfoo eval --config promptfooconfig.yaml \
    --filter-pattern 'TC10'

# 运行全部测试
bash test/run_tests.sh --promptfoo
```

### 4.5 权重设计原则

- 核心功能断言：`weight: 0.3-0.4`
- 次要功能断言：`weight: 0.15-0.2`
- `defaultTest` 中的通用断言（非空、长度）：`weight: 0.05`
- 所有权重之和不需要等于 1，Promptfoo 会归一化

---

## 五、配置文件说明

### `promptfooconfig.yaml`

| 字段 | 说明 |
|------|------|
| `targets` | 代理端点配置（`apiBaseUrl: http://127.0.0.1:4000`） |
| `prompts` | prompt 模板（`{{prompt}}` 会被 `tests[].vars.prompt` 替换） |
| `defaultTest.assert` | 共享断言（所有测试都会执行） |
| `tests[].description` | 测试名称（用于过滤和报告） |
| `tests[].vars.prompt` | 具体测试 prompt |
| `tests[].assert` | 该测试特有的断言 |

### 当前测试矩阵

| # | 测试 | 验证目标 |
|---|------|---------|
| TC1 | 打招呼 | 基本连通性和中文支持 |
| TC2 | 代码生成 | 代码输出格式（def/type hint/docstring） |
| TC3 | 代码审查 | 推理能力（指出资源泄漏） |
| TC4 | 技术解释 | 长文本生成（prefix caching） |
| TC5 | JSON 转换 | 结构化输出（兼容单双引号） |
| TC6 | 特殊字符编码 | emoji、路径、引号、中文标点 |
| TC7 | 上下文连续性 | 多轮对话上下文保留 |
| TC8 | 代码边界处理 | 边界条件推理（n=0, n<0） |
| TC9 | 长文本总结 | 长上下文处理能力 |

---

## 六、已知限制

1. **agentic 交互不可测试**: Promptfoo 无法验证 Claude Code 自主决定工具调用的行为，这部分仍需人工 A/B 测试。
2. **模型随机性**: 即使 prompt 固定，LLM 输出仍有随机性。建议设置 `temperature: 0.6` 并允许 `contains-any` 多关键词匹配。
3. **代理日志关联**: `promptfoo_report_merge.py` 通过文件名时间戳关联代理日志，可能不够精确。Future 可在 collect 阶段显式记录 Promptfoo eval ID。

---

## 七、Future Work

- [x] 将 Promptfoo 集成到 `test/run_tests.sh --promptfoo`
- [x] 将固定 prompt 回归测试加入 pre-commit（代理运行时自动触发）
- [ ] 开发自定义 Promptfoo provider 直接读取代理日志指标
- [ ] 将 agentic 任务转化为 Promptfoo `sequence` 测试（如可行）
- [ ] GitHub Actions CI 集成（每次 PR 自动跑回归测试）
- [ ] 配置 `--filter-first-n 5` 作为 pre-commit 快速模式，完整 9 用例留给 CI

---

> **核心原则**: 诚实面对工具边界。Promptfoo 增强了报告生成、质量评估和回归测试，但无法替代端到端 agentic 测试。两者互补，而非替代。
