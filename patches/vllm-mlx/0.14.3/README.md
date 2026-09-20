# 0.14.3 补丁：CacheStats 检查点计数（P1a-cnts，联合协调 0920-12a / 待办 #9）

> 状态：**已应用 + 运行时验证通过**（2026-09-20，Step 3 升级窗随 0.14.3 落地）。
> 应用位置：`.venv-rapidmlx-0143/.../rapid_mlx/{memory_cache.py,routes/metrics.py}`；
> 运行时证据：`:8081/metrics` 已出 `rapid_mlx_prefix_cache_snapdown_{hits,tokens_saved}_total`，
> 且长会话实测 `snapdown_hits=1 / tokens_saved=24576`（见 runbook §12/§15）。
> 来源：swe-eval `joint-coordination-llamacpp.md` 0920-12 提请 / 0920-14 登记。
> 基准：PyPI `rapid_mlx-0.14.3-py3-none-any.whl`（09-20 下载，pip 需 Python ≥3.10）。

## 缺口（为什么需要）

上游 0.14.3 的 `_snap_hybrid_trim` 命中（LCP / supersequence 检查点续算）只累加
通用 `hits` / `tokens_saved`——与普通前缀命中不可区分，`/metrics` 无实时归因；
本地 0.12.x 补丁原有 `snapdown_hits` / `snapdown_tokens_saved`（进 /metrics），
升级后倒退。本补丁把同名字段移植到 0.14.3，保持下游（dashboard / metering）连续。

## 文件

| 文件 | 说明 |
|------|------|
| `cachestats-counters.diff` | unified diff（review 用，79 行） |
| `memory_cache.py` | 补丁后全文件（4 处：CacheStats 字段 / to_dict / 两命中点递增） |
| `routes_metrics.py` | 补丁后全文件（1 处：metrics 三元组表 +2 counter） |

## 修改点

1. `CacheStats`：+`snapdown_hits` / `snapdown_tokens_saved`（int=0；随 `hits`
   同 reset 契约，Prometheus 单调性由 /metrics accumulator 保证——与
   `load_skipped` 的跨 reset 携带契约不同，是有意选择：对齐 `hits` 语义）
2. `to_dict()`：+两字段输出
3. 两命中点（supersequence ~L2090 / LCP ~L2215）：`snapdown_hits += 1`、
   `snapdown_tokens_saved += position`
4. `routes/metrics.py` counter 表：`rapid_mlx_prefix_cache_snapdown_hits_total`
   / `rapid_mlx_prefix_cache_snapdown_tokens_saved_total`

## 应用（Step 3 窗口，B2 runbook 序列内）

```bash
# 升级 venv 到 0.14.3 后：
cp memory_cache.py  <venv>/lib/python3.14/site-packages/rapid_mlx/memory_cache.py
cp routes_metrics.py <venv>/lib/python3.14/site-packages/rapid_mlx/routes/metrics.py
# 验证：重启引擎后 curl :8081/metrics | grep snapdown
# 预期：rapid_mlx_prefix_cache_snapdown_hits_total 出现（0 起步，命中后递增）
```

## 验证状态

- ✅ py_compile（venv Python 3.14）
- ✅ 语义自检 5/5（字段/to_dict/两递增/两导出/raw_key 对齐）
- ✅ **运行时验证（2026-09-20 完成）**：`:8081/metrics` 出
  `rapid_mlx_prefix_cache_snapdown_hits_total` / `..._tokens_saved_total`，
  长会话命中实测 `snapdown_hits=1`、`snapdown_tokens_saved=24576`
- ⚠️ 若采用的版本 ≠ 0.14.3（如 0.14.4+），需对本 diff 重定位
  （锚点均为唯一字符串，冲突概率低）
