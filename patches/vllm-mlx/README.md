# vllm-mlx 引擎补丁：P1a LCP 边界检查点复用（snap-down）

> 来源：llama-defender 上下文工程设计 §11（agent_go 仓库 docs/design/llama-defender-context-engineering-design.md）
> 日期：2026-09-06　|　目标版本：rapid-mlx 0.12.12（vllm_mlx 包）
> 状态：fetch 侧已落地（flag 默认关，行为与补丁前逐字节一致）；生产侧（scheduler 多边界检查点捕获）待做

## 背景（Phase 0 E1 实证）

混合注意力模型（Ornith/qwen3_5：线性注意力层 ArraysCache 递归态）的缓存条目
全部含不可 trim 层 → LCP 复用被结构性禁止 → 实测 6,918 次 LCP unavailable /
1.36 亿 tokens 可复用而全量重算（占总 prefill 算力 67%）。
递归态不可回卷是数学事实（正确拒绝）；修复 = **边界检查点**：B ≤ LCP 的
消息边界处线性态对共享前缀请求是合法续算状态。实测约束：单边界检查点
仅救 2.6%（现有 boundary_snapshot 位置在发散点之后），**必须多边界捕获**。

## 本补丁内容（fetch 侧消费基础）

`memory_cache.py` 相对 0.12.12 原版的增量（全文检索 "P1a" 标记）：
1. `_CacheEntry` 增加 `nontrim_layer_indices` / `linear_checkpoints`
2. `store()` 新增 kwarg `linear_checkpoints`（检查点内存计入条目）
3. `fetch()` LCP 分支：flag 开且条目带检查点 → `_build_snapdown_view`
   （KV 层切片到 B + 递归层恢复检查点态），失败安全回退 MISS
4. `CacheStats` 增加 `snapdown_hits` / `snapdown_tokens_saved`（进 /metrics）
5. 模块级：`_env_flag` / `snapshot_linear_states`（scheduler 生产侧调用接口）/ `_estimate_checkpoints_memory` / `_restore_recurrent_layer`

## 开关与生效条件

- `PROXY_CACHE_LCP_SNAPDOWN=1` 开启 fetch 侧
- 但**当前无生产者**：条目不带 linear_checkpoints 时行为不变——
  生产侧（scheduler 多边界捕获，见 §11.7 P1a.2）落地后才有实际效果

## 应用 / 回滚 / 验证

```bash
SP=/opt/homebrew/Cellar/rapid-mlx/0.12.12/libexec/lib/python3.14/site-packages
# 应用（brew upgrade 后需对 0.12.x+ 新原版重derive，勿盲拷）
cp $SP/vllm_mlx/memory_cache.py $SP/vllm_mlx/memory_cache.py.orig   # 首次先备份
cp patches/vllm-mlx/0.12.12/memory_cache.py $SP/vllm_mlx/memory_cache.py
# 回滚
cp $SP/vllm_mlx/memory_cache.py.orig $SP/vllm_mlx/memory_cache.py
# 验证（15 项单元测试）
/opt/homebrew/Cellar/rapid-mlx/0.12.12/libexec/bin/python test/unit/test_lcp_snapdown.py
```

## 剩余工作（P1a.2 生产侧）

- `request.py` prefix_boundary → 边界列表（消息边界抽样，min-spacing + M=8 上限）
- `engine_core.py` 透传
- `scheduler.py` insert_segments N 段 + 每 end_of_segment 调
  `snapshot_linear_states(extracted)` 累积 `request._linear_checkpoints`
  → 全量 store 时传入 `linear_checkpoints=`
- Gate A：模型级 greedy 输出逐 token 一致验证（静默窗口，phase0 probe 适配）
