#!/usr/bin/env python3
"""P1a.2 生产侧纯函数与累积逻辑单测（llama-defender 上下文工程 §11.2）。

覆盖：
  S1  select_checkpoint_positions（去重/间距/limit/越界）
  S2  snapshot_linear_states 只提取 nontrim 层（KVCache 跳过、ArraysCache 提取）
  S3  检查点累积 + 上限护栏语义（模拟 _snapshot_boundary_segments 的累积路径）
  S4  fetch 侧端到端：生产者模拟（多边界累积）→ store → fetch snap-down 命中

运行（venv python）：
  /opt/homebrew/Cellar/rapid-mlx/0.12.12/libexec/bin/python \
      test/unit/test_lcp_checkpoints_producer.py -v
无 vllm_mlx 环境自动 skip（pre-commit 系统 Python 兼容）。
"""
import os
import unittest

try:
    from mlx_lm.models.cache import ArraysCache, KVCache
    from vllm_mlx.engine.batched import select_checkpoint_positions
    from vllm_mlx.memory_cache import (
        MemoryAwarePrefixCache,
        MemoryCacheConfig,
        snapshot_linear_states,
    )
    _VENV = True
    _SKIP_REASON = ""
except ImportError as _exc:
    _VENV = False
    _SKIP_REASON = f"requires rapid-mlx venv: {_exc}"


@unittest.skipUnless(_VENV, _SKIP_REASON)
class TestSelectCheckpointPositions(unittest.TestCase):
    def test_s1_dedupe_spacing_limit(self):
        self.assertEqual(
            select_checkpoint_positions([500, 300, 300, 1500, 2600], 4000),
            [300, 1500, 2600],
        )
        self.assertEqual(
            select_checkpoint_positions([100, 2000, 3000, 3500], 4000,
                                        limit=2, min_spacing=400),
            [3000, 3500],
        )
        self.assertEqual(select_checkpoint_positions([0, 4000, -5, 2000], 4000), [2000])
        self.assertEqual(select_checkpoint_positions([], 4000), [])


@unittest.skipUnless(_VENV, _SKIP_REASON)
class TestLinearStateSnapshot(unittest.TestCase):
    def _cache(self, seed: float):
        import mlx.core as mx
        kv = KVCache()
        kv.keys = mx.zeros((1, 2, 8, 4), mx.float16)
        kv.values = mx.zeros((1, 2, 8, 4), mx.float16)
        kv.offset = 100
        lin = ArraysCache(size=2)
        lin.cache = [mx.full((1, 2, 2), seed, mx.float16)] * 2
        return [kv, lin]

    def test_s2_only_nontrim_layers_extracted(self):
        import mlx.core as mx
        states = snapshot_linear_states(self._cache(5.0))
        self.assertEqual(len(states), 1)          # 只提取 ArraysCache 层
        self.assertTrue(mx.allclose(states[0][0], mx.full((1, 2, 2), 5.0, mx.float16)))

    def test_s2b_snapshot_is_private_copy(self):
        c = self._cache(5.0)
        states = snapshot_linear_states(c)
        c[1].cache = [None] * 2                    # 原地改写原层容器
        self.assertEqual(len(states[0]), 2)        # 快照持有私有容器，不受影响
        import mlx.core as mx
        self.assertTrue(mx.allclose(states[0][0], mx.full((1, 2, 2), 5.0, mx.float16)))


@unittest.skipUnless(_VENV, _SKIP_REASON)
class TestProducerToFetchEndToEnd(unittest.TestCase):
    """模拟生产者累积路径：多边界 → store(linear_checkpoints) → fetch snap-down。"""

    def _cache(self, n_tokens: int, seed: float):
        import mlx.core as mx
        kv = KVCache()
        kv.keys = mx.zeros((1, 2, 64, 8), mx.float16)
        kv.values = mx.zeros((1, 2, 64, 8), mx.float16)
        kv.offset = n_tokens
        lin = ArraysCache(size=2)
        lin.cache = [mx.full((1, 4, 8), seed, mx.float16),
                     mx.full((1, 4, 8), seed + 1, mx.float16)]
        return [kv, lin]

    def test_s4_multi_checkpoint_end_to_end(self):
        os.environ["PROXY_CACHE_LCP_SNAPDOWN"] = "1"
        mc = MemoryAwarePrefixCache(
            model=object(), config=MemoryCacheConfig(hybrid_reuse_max_entries=16))
        N = 1000
        tokens = list(range(N))

        # 模拟 scheduler 在两个边界累积检查点（S3 累积语义）
        checkpoints = {}
        for b, seed in ((300, 3.0), (600, 7.0)):
            states = snapshot_linear_states(self._cache(b, seed))
            checkpoints[b] = states
        self.assertEqual(sorted(checkpoints), [300, 600])

        self.assertTrue(mc.store(tokens, self._cache(N, 1.0),
                                 linear_checkpoints=checkpoints))

        # 发散点在 900（81% 处，E1 中位形态）→ 应选 B=600
        divergent = list(range(900)) + [10_000_000 + i for i in range(N - 900)]
        out, rem = mc.fetch(divergent)
        self.assertIsNotNone(out)
        self.assertEqual(mc._last_match_type, "lcp_snapdown")
        self.assertEqual(int(out[0].offset), 600)          # KV 切片到 B=600
        self.assertEqual(rem, divergent[600:])
        # 递归层是 B=600 的检查点态（seed=7.0 而非条目末态 1.0）
        import mlx.core as mx
        self.assertTrue(mx.allclose(out[1].state[0],
                                    mx.full((1, 4, 8), 7.0, mx.float16)))
        stats = mc.get_stats()
        self.assertEqual(stats.get("snapdown_hits"), 1)
        self.assertEqual(stats.get("snapdown_tokens_saved"), 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
