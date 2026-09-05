# 折叠召回线索（Recall Cue）设计（2026-09-03）

> 来源：`gate-ce-` 268 轮长跑出现 loop_l3×71，回查发现模型 0 调用 `ctx_recall`，
> 根因是折叠内容未留下召回线索；且 PDC-L1 只修了 A 路，B 路滞后（双轨漂移）。
> 相关文档：`model-performance-snapshot-20260902.md`（现象记录）、
> `rapidmlx-kvcache-mechanism-20260903.md`（缓存机制视角）。

---

## 1. 问题

模型在折叠后重读文件而非调 `ctx_recall`，相同工具调用叠到 ≥9 次触发 L3 剥工具。
两条折叠路径的线索口径不一致：

| | A 路（引擎关，fifo/DEF-107） | B 路（引擎开，epoch 面板） |
|---|---|---|
| 位置 | `truncation.py` fifo 分支 | `context_engine.py:_collapse` |
| 指引句 | 有（PDC-L1 2026-08-31） | 无 |
| 查询键 | sample anchors[:3] | 无（仅动作行，无 anchor） |
| 行为规则 | "instead of re-reading" | 无 |

工具可见性已排除：`tool_filter.py:82` 把 `ctx_recall` 免过滤追加，不占
`PROXY_TOOL_FILTER_MAX` 名额。缺的是折叠内容里的线索。

## 2. 模型需要的 4 类线索（按重要性）

1. **查询键**（动态，最重要）：本轮被折叠的精确 anchor/路径，上限 6 个。
   无键则模型只能猜关键词 → 重读。
2. **取回方法 + 示例**（静态）：`ctx_recall(query='src/auth.py')` /
   `ctx_recall(query='r:t3')`。
3. **可恢复性声明**（静态）：全文保留在本会话存储中（纠正"只剩摘要"的误读）。
4. **行为优先级**（静态）：recall first；仅当 `ctx_recall` 无结果时才重读。

## 3. 收敛决策

- **不引入设计模式**：2 个调用点、行为零差异，常量 + 纯函数足够；
  类层级与 deferred-import 循环规避纪律冲突；防漂移靠单测不断言结构。
- **收敛点 = `ctx_recall.py`**（文案指向谁，谁拥有文案；依赖方向干净，
  `ctx_recall` 不 import `truncation`/`context_engine`，两边 deferred import 接入）。
- **收敛内容**：`RECALL_CUE`（静态指引句唯一源头）+
  `FOLDED_KEYS_LIMIT=6` + `recall_keys_line()`（取键格式化纯函数）。
- **不收敛**：各路径正文（A 的 drop 统计 / B 的动作行）、触发条件
  （A 的 drop_ratio 门控 / B 的每次 epoch）、取键语义（仍在 `unit_model.py`）。

共享 cue 文案（字节级一致，跨路径/跨会话利于 prefix-cache）：

```text
Full text of folded content is preserved in this session store — use ctx_recall
to recover instead of re-reading files, e.g. ctx_recall(query='<file-path-or-keyword>')
or ctx_recall(query='<anchor>'). Recall first; re-read only if ctx_recall returns nothing.
```

## 4. 改动清单

| 文件 | 改动 |
|---|---|
| `ctx_recall.py` | 新增 `RECALL_CUE` / `FOLDED_KEYS_LIMIT` / `recall_keys_line()`（~15 行） |
| `truncation.py` | fifo 两分支指引句替换为 `RECALL_CUE`，sample anchors 改走 `recall_keys_line` |
| `context_engine.py` | `_collapse` 面板追加 `RECALL_CUE` + 折叠轮次 anchor 键（`unit_anchors` 复用 pin 写法） |
| `test/unit/test_recall_cue.py` | 新增：A/B 文案字节一致性、键上限 6、空键 fail-open |

## 5. 验证

`bash test/run_tests.sh --unit` + `--signature` + `--snapshot`。
生产代理下次 reload 生效（`RECALL_CUE` 为纯文案常量，无需新配置项）。
