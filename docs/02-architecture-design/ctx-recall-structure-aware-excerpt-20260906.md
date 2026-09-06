# ctx_recall 结构感知摘录设计（auto-recall 注入窗口优化）

> 2026-09-06。来源：EXP-2 实战（auto-recall 首次触发注入 4000 字符头部窗口）+
> RAG 分块策略调研评估。关联：自闭环方案
> `docs/01-requirements-product/ctx-recall-auto-closed-loop-20260905.md`；
> EXP-2 跟踪在 swe-eval `docs/roadmap-issues.md`。

## 1. 问题

auto-recall 首次注入的窗口选择是 **head-first 固定截断**
（`recover_full_content` [0:4000] + anchor@offset 分页续读）：
模型重读已折叠目标时，它要找的内容大概率不在文件头部，注入无关内容
既浪费窗口预算（4000 chars）又可能误导；续读每页 = 1 个额外轮次
（≈30-60s + 数 K tokens）。

三个设计轴需分开评价（调研结论：固定预算本身不是缺陷）：

| 轴 | 现状 | 评价 |
|---|---|---|
| 窗口选择策略 | head-first | **真正的粗暴点** |
| 注入预算 | 固定 4000 chars（≈1-1.3K tokens） | 合理的防挤占护栏 |
| 续读协议 | anchor@offset 分页 | 可用，成本是轮次 |

## 2. RAG 分块策略调研（2026-09-06）

- **固定长度**仍是严肃 baseline：2025 NAACL 评测显示语义分块的成本
  不被稳定收益 justify（[综述](https://vertexfrontier.com/rag-data-preprocessing/)）。
- **结构感知/AST 分块**是代码场景的 SOTA 方向：cAST
  （[arXiv 2506.15655](https://arxiv.org/abs/2506.15655)）按 AST 递归
  切/合块，RepoEval Recall@5 +4.3、SWE-bench Pass@1 +2.67；工业侧有
  [supermemory/code-chunk](https://github.com/supermemoryai/code-chunk)
  （AST-aware，服务 Claude Code/Cursor 记忆场景）。
- **Contextual Retrieval**（Anthropic）：ingestion 时给块补上下文摘要；
  本代理 manifest 索引行（handle/triggers）已是同构轻量版。
- **语义分块**（embedding/LLM 切块）：热路径成本不可接受，排除。

**我们的场景优于 RAG**：召回的是模型自己刚读过的内容，且触发 dup 的
Read 调用带 offset/limit（模型想读哪段是已知信号）——无需 embedding
即可做到相关窗口。cAST 的收益数字（检索场景）要打折扣看，我们的收益
主要在"一次注对"省续读轮次 + 避免注入无关内容挤占窗口。

## 3. 成本分析（三条实现路线）

| 维度 | ① stdlib `ast`（仅 Python） | ② tree-sitter 多语言 | ③ 正则/缩进启发式 |
|---|---|---|---|
| 依赖 | 零（stdlib） | native binding + per-language grammar，破坏 stdlib 纪律 | 零 |
| 实现量 | ~150 行 + 测试 | 3-5 天 + 持续维护 | ~50 行 |
| 运行耗时 | 100KB 文件 parse ≈ 10-30ms | 同量级 | <5ms |
| 精度 | 真 AST（Python 精确） | 真 AST 多语言 | 边界误判可见 |
| 维护 | 几乎无 | grammar 版本跟进 | 低 |

成本缓释因素：①注入路径是冷路径（PER_SESSION ≤5），运行耗时无关热
路径；②任务语料以 Python 为主（ansible/openlibrary/rich/textual），
stdlib `ast` 零依赖拿到真 AST，tree-sitter 多语言能力当前用不上
（js/ts 仓库进任务集再评估，接口预留）。

失败模式预算：mid-edit 损坏代码 parse 失败 → 行边界 fallback；
非代码 tool_result（日志/测试输出）→ AST 不适用，走行边界窗口。

## 4. 设计（路线 ① + 行边界 fallback）

新增 `ctx_recall.structure_aware_excerpt(content, target_path, budget_chars)`：

- **非 .py 或 `ast.parse` 失败** → 行边界 fallback：[0:budget] 对齐到
  最后完整行尾，`strategy="line"`。
- **Python 且 parse 成功** → `ast` 提取顶层块（class/def/assign/import）
  行区间；输出 = **文件骨架**（所有 class/def 签名 + 行号区间 + 每块
  **字符偏移**）+ 按序填充完整顶层块至预算耗尽，`strategy="ast"`。
  骨架内 char offset 与既有 anchor@offset 分页协议直接兼容（模型可按
  骨架定位目标块，经 `ctx_recall query="锚点@偏移"` 精确续读）。

接入点：`auto_recall_for_target` 在 offset==0（首次注入）时用摘录替代
裸头部截断；续读路径（offset>0）保持原契约不变。注入文案
（pipeline.py [System: AUTO-RECALL ...]）在 strategy="ast" 时补充一句
骨架 offset 用法。预算仍走 `PROXY_AUTO_RECALL_MAX_CHARS`，不新增开关。

### 4.1 追加：第三级多语言启发式梯队（2026-09-06）

梯队扩展为三级（`strategy` 枚举 `"ast"|"heuristic"|"line"`）：

1. **.py 且 `ast.parse` 成功** → ast 精确摘录（原路线 ①，不变）；
2. **其他代码语言或 ast 失败** → 启发式结构摘录（`_heuristic_blocks`，
   `strategy="heuristic"`）。语言按扩展名识别：`.js/.jsx/.ts/.tsx/.mjs/.cjs`、
   `.go`、`.java`、`.rs`、`.c/.h/.cpp/.cc/.hpp`（以上花括号配平）、`.rb`
   （列 0 `def/class/module` 计数 + 列 0 `end` 配对）、`.sh/.bash`
   （`name() {` + 花括号配平）；`.py` 在 ast 失败时以「下一列 0 起始行」
   定块尾。起始正则一律列 0 锚定（嵌套声明不单独成块）；命中数 <2
   视为无结构；
3. **非代码 / 启发式无命中 / 骨架超预算** → 行边界 fallback（不变）。

heuristic 与 ast 共用同一渲染（骨架头 + `[L起-L止 @偏移] 签名行` +
按序整块填充），骨架 offset 与 anchor@offset 分页协议同口径；pipeline
注入文案的骨架提示相应放宽为 `strategy in ("ast", "heuristic")`。

启发式已知边界（有意取舍，换取零依赖与确定性）：

- **字符串内花括号不感知**：`"}"` 之类的字面量会参与配平；
- 仅跳过**整行** `//` 注释与 `#!` 行；行尾注释、块注释（`/* } */`）、
  Ruby `#` 注释里的花括号/关键字会误计；
- 花括号语言的「无花括号起始」（如 `const f = x => x;`）块尾取下一
  起始正则命中前一行，块间非块代码并入前块；
- ast 失败的 `.py` 走 nextstart 模式，块尾同上（缩进不感知）；
- 起始行之后配平深度回到 0 即收块；收不到则延伸至文件尾——宁多取
  一块正文，不截半。

## 5. 验收与验证

- 单测：ast 摘录（骨架/预算/块完整性）、损坏代码回退、非代码行边界、
  极小预算退化、续读协议不变。
- 实证：后续 EXP（A/B：head-first vs structure-aware），指标 =
  召回采纳率 / 续读次数每 run / 注入后 dup 下降幅度。
  （EXP-2 的注入样本同时可作定性对照素材。）
