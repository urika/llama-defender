#!/usr/bin/env python3
"""P1a LCP snap-down 单元测试（llama-defender 上下文工程 §11，2026-09-06）。

用真实 mlx 层（KVCache + ArraysCache）验证 snap-down 补丁：
  T1  flag 关（默认）→ 行为与补丁前逐字节一致（LCP non-trimmable → MISS）
  T2  flag 开 + 条目带检查点 → snap-down：KV 切片到 B、递归层恢复检查点态
  T3  检查点位置全部 > LCP → 安全回退 MISS
  T4  检查点结构损坏 → 安全回退 MISS
  T5  trim-free 路径（exact/prefix，含 boundary 条目）不受补丁影响
  T6  检查点内存计入条目
  T7  条目本体不被 snap-down 修改（复用视图隔离）

依赖 rapid-mlx venv（mlx/mlx_lm/vllm_mlx）。无该依赖的环境（如仓库
pre-commit 的系统 Python）自动 SKIP——测试必须用 venv python 跑：

  /opt/homebrew/Cellar/rapid-mlx/0.12.12/libexec/bin/python \
      test/unit/test_lcp_snapdown.py -v
"""
import os
import unittest

try:
    from mlx_lm.models.cache import ArraysCache, KVCache
    from vllm_mlx.memory_cache import (
        MemoryAwarePrefixCache,
        MemoryCacheConfig,
        snapshot_linear_states,
        _estimate_checkpoints_memory,
    )
    _VENV = True
    _SKIP_REASON = ""
except ImportError as _exc:  # 系统 Python / 无 rapid-mlx venv
    _VENV = False
    _SKIP_REASON = f"requires rapid-mlx venv (mlx/mlx_lm/vllm_mlx): {_exc}"


@unittest.skipUnless(_VENV, _SKIP_REASON)
class TestLCPSnapdown(unittest.TestCase):
    N = 1000            # 条目 token 数
    B = 600             # 检查点位置（< LCP）
    LCP = 900           # 发散点

    def _cache(self, n_tokens: int, seed: float):
        """双混合层 cache：KVCache（可 trim，offset=n）+ ArraysCache（递归态）。"""
        import mlx.core as mx
        kv = KVCache()
        kv.keys = mx.zeros((1, 2, 64, 8), mx.float16)
        kv.values = mx.zeros((1, 2, 64, 8), mx.float16)
        kv.offset = n_tokens
        lin = ArraysCache(size=2)
        lin.cache = [mx.full((1, 4, 8), seed, mx.float16),
                     mx.full((1, 4, 8), seed + 1, mx.float16)]
        return [kv, lin]

    def _instance(self, flag: str):
        os.environ["PROXY_CACHE_LCP_SNAPDOWN"] = flag
        # 生产部署已开启 hybrid 保留（日志 entries=14 且 LCP 候选
        # non_trimmable=True 证明条目在库）——hybrid_reuse_max_entries>0
        return MemoryAwarePrefixCache(
            model=object(), config=MemoryCacheConfig(hybrid_reuse_max_entries=16))

    def _state_matches(self, cache_list, seed: float) -> bool:
        import mlx.core as mx
        a = cache_list[0]
        return (a is not None and a.shape == (1, 4, 8)
                and bool(mx.allclose(a, mx.full(a.shape, seed, mx.float16))))

    def setUp(self):
        self.tokens_full = list(range(self.N))
        self.divergent = list(range(self.LCP)) + [10_000_000 + i for i in range(self.N - self.LCP)]

    def test_t1_flag_off_behavior_unchanged(self):
        mc = self._instance("0")
        ckpts = {self.B: snapshot_linear_states(self._cache(self.B, 7.0))}
        self.assertTrue(mc.store(self.tokens_full, self._cache(self.N, 1.0),
                                 linear_checkpoints=ckpts))
        out, rem = mc.fetch(self.divergent)
        self.assertIsNone(out)                      # 与补丁前一致：LCP→MISS
        self.assertEqual(rem, self.divergent)

    def test_t2_snapdown_hit(self):
        import mlx.core as mx
        mc = self._instance("1")
        ckpt_src = self._cache(self.B, 7.0)
        ckpts = {self.B: snapshot_linear_states(ckpt_src)}
        self.assertTrue(mc.store(self.tokens_full, self._cache(self.N, 1.0),
                                 linear_checkpoints=ckpts))
        out, rem = mc.fetch(self.divergent)
        self.assertIsNotNone(out)
        self.assertEqual(mc._last_match_type, "lcp_snapdown")
        self.assertEqual(rem, self.divergent[self.B:])
        kv, lin = out[0], out[1]
        self.assertEqual(int(kv.offset), self.B)            # KV 切片到 B
        self.assertTrue(self._state_matches(lin.state, 7.0))  # 检查点态恢复
        self.assertIsNot(lin.cache, ckpt_src[1].cache)      # 容器私有
        stats = mc.get_stats()
        self.assertEqual(stats.get("snapdown_hits"), 1)
        self.assertEqual(stats.get("snapdown_tokens_saved"), self.B)

    def test_t3_no_checkpoint_below_lcp_falls_back(self):
        mc = self._instance("1")
        ckpts = {950: snapshot_linear_states(self._cache(950, 7.0))}
        self.assertTrue(mc.store(self.tokens_full, self._cache(self.N, 1.0),
                                 linear_checkpoints=ckpts))
        out, rem = mc.fetch(self.divergent)
        self.assertIsNone(out)                              # 无 B≤LCP → MISS
        self.assertEqual(rem, self.divergent)

    def test_t4_corrupt_checkpoints_falls_back(self):
        import mlx.core as mx
        mc = self._instance("1")
        self.assertTrue(mc.store(self.tokens_full, self._cache(self.N, 1.0),
                                 linear_checkpoints={self.B: [[mx.zeros((1,))]]}))
        entry = mc._entries[tuple(self.tokens_full)]
        entry.nontrim_layer_indices = [0, 1, 2]             # 与状态数不齐
        out, _ = mc.fetch(self.divergent)
        self.assertIsNone(out)                              # 结构不一致 → 不崩溃

    def test_t5_prefix_path_unaffected(self):
        mc = self._instance("1")
        self.assertTrue(mc.store(self.tokens_full, self._cache(self.N, 1.0),
                                 linear_checkpoints={self.B: snapshot_linear_states(self._cache(self.B, 7.0))}))
        # 生产 boundary_snapshot 同构：边界条目（非 trimmable）在库，后续轮
        # 以它为严格前缀 → prefix 命中（#1103：trim-free 路径可服务递归态条目）
        self.assertTrue(mc.store(list(range(400)), self._cache(400, 3.0),
                                 message_boundary=True))
        out, rem = mc.fetch(list(range(400)) + [77, 78, 79])
        self.assertIsNotNone(out)
        self.assertIn(mc._last_match_type, ("prefix", "exact"))
        self.assertEqual(len(rem), 3)

    def test_t6_checkpoint_memory_counted(self):
        ckpts = {self.B: snapshot_linear_states(self._cache(self.B, 7.0))}
        self.assertGreater(_estimate_checkpoints_memory(ckpts), 0)
        self.assertEqual(_estimate_checkpoints_memory({}), 0)

    def test_t7_entry_body_untouched_by_snapdown(self):
        import mlx.core as mx
        mc = self._instance("1")
        ckpts = {self.B: snapshot_linear_states(self._cache(self.B, 7.0))}
        self.assertTrue(mc.store(self.tokens_full, self._cache(self.N, 1.0),
                                 linear_checkpoints=ckpts))
        entry = mc._entries[tuple(self.tokens_full)]
        orig_offset = int(entry.cache[0].offset)
        orig_lin = entry.cache[1].state[0]
        mc.fetch(self.divergent)
        self.assertEqual(int(entry.cache[0].offset), orig_offset)
        self.assertTrue(mx.allclose(entry.cache[1].state[0], orig_lin))


if __name__ == "__main__":
    unittest.main(verbosity=2)
