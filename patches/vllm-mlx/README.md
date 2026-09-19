# vllm-mlx 引擎补丁：P1a LCP 边界检查点复用（snap-down）

> 来源：llama-defender 上下文工程设计 §11（agent_go 仓库 docs/design/llama-defender-context-engineering-design.md）
> 日期：2026-09-06 起　|　目标版本：rapid-mlx 0.12.12（vllm_mlx 包）
> 状态（2026-09-20 更新）：**全链路上线运行中** —— Gate A 通过（`b77f643`，29.7×，逐 token 一致）、
> `PROXY_CACHE_LCP_SNAPDOWN=1` 已入生产 conf；EXP-9 批期实时增量占比 **0.1189**（3.2 万请求窗口，对照全史 0.3689）

## 背景（Phase 0 E1 实证）

混合注意力模型（Ornith/qwen3_5：线性注意力层 ArraysCache 递归态）的缓存条目
全部含不可 trim 层 → LCP 复用被结构性禁止 → 实测 6,918 次 LCP unavailable /
1.36 亿 tokens 可复用而全量重算（占总 prefill 算力 67%）。
递归态不可回卷是数学事实（正确拒绝）；修复 = **边界检查点**：B ≤ LCP 的
消息边界处线性态对共享前缀请求是合法续算状态。实测约束：单边界检查点
仅救 2.6%（现有 boundary_snapshot 位置在发散点之后），**必须多边界捕获**。

## 补丁内容（5 个文件，检索 "P1a" 标记）

**fetch 侧（`memory_cache.py`）**
1. `_CacheEntry` 增加 `nontrim_layer_indices` / `linear_checkpoints`
2. `store()` 新增 kwarg `linear_checkpoints`（检查点内存计入条目）
3. `fetch()` LCP 分支：flag 开且条目带检查点 → `_build_snapdown_view`
   （KV 层切片到 B + 递归层恢复检查点态），失败安全回退 MISS
4. `CacheStats` 增加 `snapdown_hits` / `snapdown_tokens_saved`（进 /metrics）
5. 模块级：`_env_flag` / `snapshot_linear_states` / `_estimate_checkpoints_memory`
   / `_restore_recurrent_layer`

**生产侧（`request.py` / `engine_core.py` / `engine/batched.py` / `scheduler.py`）**
- `request.py`：`prefix_boundaries` 字段（升序 token 位置列表）
- `engine_core.py`：`add_request` 透传
- `engine/batched.py`：`select_checkpoint_positions` 纯函数（去重/间距/limit 尾部优先）
  + `_compute_prefix_boundaries`（消息截断渲染 + token LCP，`PROXY_CACHE_LCP_SNAPDOWN`
  门控，异常 fail-closed 返回 []）+ generate 双路径透传；短上下文 <4K 跳过
- `scheduler.py`：`_schedule_waiting` N 段 insert_segments（预期局部切点存
  `request._lcp_expected_local_splits`）；`_snapshot_boundary_segments` 事件收集置于
  既有过滤之前 + `snapshot_linear_states` 现场累积（M=8 上限）；`_prompt_cache_save`
  与边界条目 store 均挂载 `linear_checkpoints`

## Gate A 调试中的三处修复（`b77f643`，均有实证）

1. **严格前缀守卫放宽**：Qwen3.5 模板对截断渲染自动追加 assistant 标记、对无
   user 结尾抛错 → 严格守卫致候选全空。正确性由 fetch 侧 `B ≤ lcp` 不变式独立
   保证，守卫非必要；异常路径补 INFO 日志（原静默返回 [] 无法诊断）
2. **LCP 扫描窗口 ±4 + 检查点候选优先**：多会话混缓存下带检查点条目未必是
   bisect 紧邻；全局最优候选为 non-trimmable 且无检查点（必全量 MISS）时，
   改选带检查点候选（部分复用严格占优于零复用）
3. **边界条目挂载检查点**：LCP 候选经常正是 boundary 条目，不挂载会漏复用

## Gate A 验收结果（tools/gate_a_lcp_snapdown.py，14,263 tokens 多轮对话）

```
LCP snapdown: B=13848 reused=13848 remaining=415（复用 97.1%）
snap 9.4s  vs  cold 279.0s = 29.7×
输出逐字一致（50/50 tokens）——检查点复用 ≡ 全量 prefill
```

## 开关与生效条件

- `PROXY_CACHE_LCP_SNAPDOWN=1`（**引擎进程 env**，conf export；注意不是代理 env）
  同时开启生产侧（多段 prefill + 检查点捕获）与 fetch 侧（snap-down 消费）；
  关闭时全部新路径 inert，行为与补丁前逐字节一致
- 短上下文（<4K tokens）自动跳过；无可用检查点/结构损坏 → 安全回退全量

## 应用 / 回滚 / 验证

```bash
SP=/opt/homebrew/Cellar/rapid-mlx/0.12.12/libexec/lib/python3.14/site-packages
# 应用（brew upgrade 后需对 0.12.x+ 新原版重derive，勿盲拷；5 个文件全部）
for f in memory_cache.py request.py engine_core.py scheduler.py engine/batched.py; do
  cp $SP/vllm_mlx/$f $SP/vllm_mlx/$f.orig 2>/dev/null   # 首次先备份
  cp patches/vllm-mlx/0.12.12/$f $SP/vllm_mlx/$f
done
# 回滚
for f in memory_cache.py request.py engine_core.py scheduler.py engine/batched.py; do
  cp $SP/vllm_mlx/$f.orig $SP/vllm_mlx/$f
done
# 验证（venv python；无 vllm_mlx 环境自动 skip）
V=/opt/homebrew/Cellar/rapid-mlx/0.12.12/libexec/bin/python
$V test/unit/test_lcp_snapdown.py            # fetch 侧 7 项
$V test/unit/test_lcp_checkpoints_producer.py # 生产侧 4 项
```

## 后续（与联合协调文档对齐）

- **Gate B off 臂对照**：搭 EXP-8 换模重启执行（LCP off/on 延迟对照，联合文档 J2 排程）
- 遥测离线分析：`tools/lcp_trace_analyzer.py`（D2 欠项②，2026-09-20 交付）
- P1b 段链去重 + 链感知驱逐：第二击穿源（LCP miss 残余 ~15% + 压力驱逐），
  动工前与 sweep 侧驱逐调优对齐（引擎现 `--cache-memory-mb 12288 --hybrid-cache-entries 32`）
