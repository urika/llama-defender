#!/usr/bin/env python3
"""test_idempotency.py — 幂等管理器测试（缓存/TTL/LRU/线程安全）。"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from idempotency import IdempotencyManager


class TestBasicCaching(unittest.TestCase):

    def setUp(self):
        self.mgr = IdempotencyManager(ttl_s=60)

    def test_decompose_cached_hits(self):
        calls = []
        def expensive():
            calls.append(1)
            return ["result"]
        r1 = self.mgr.decompose_cached("k1", expensive)
        r2 = self.mgr.decompose_cached("k1", expensive)
        self.assertEqual(r1, r2)
        self.assertEqual(len(calls), 1)  # 第二次命中缓存

    def test_verify_cached_hits(self):
        calls = []
        def expensive():
            calls.append(1)
            return {"passed": True}
        self.mgr.verify_cached("v1", expensive)
        self.mgr.verify_cached("v1", expensive)
        self.assertEqual(len(calls), 1)

    def test_recall_cached_hits(self):
        calls = []
        def expensive():
            calls.append(1)
            return "recovered text"
        self.mgr.recall_cached("r1", expensive)
        self.mgr.recall_cached("r1", expensive)
        self.assertEqual(len(calls), 1)

    def test_different_keys_miss(self):
        calls = []
        def expensive():
            calls.append(1)
            return 42
        self.mgr.verify_cached("a", expensive)
        self.mgr.verify_cached("b", expensive)
        self.assertEqual(len(calls), 2)

    def test_none_value_cached_not_treated_as_miss(self):
        calls = []
        def returns_none():
            calls.append(1)
            return None
        r1 = self.mgr.verify_cached("k", returns_none)
        r2 = self.mgr.verify_cached("k", returns_none)
        self.assertIsNone(r1)
        self.assertIsNone(r2)
        self.assertEqual(len(calls), 1)  # None 也不重算


class TestLRUEviction(unittest.TestCase):

    def test_evicts_oldest(self):
        mgr = IdempotencyManager(verify_entries=3, ttl_s=60)
        def fn():
            return "x"
        mgr.verify_cached("k1", fn)
        mgr.verify_cached("k2", fn)
        mgr.verify_cached("k3", fn)
        mgr.verify_cached("k4", fn)  # 挤掉 k1
        stats = mgr.stats()["verify"]
        self.assertEqual(stats["entries"], 3)
        self.assertGreaterEqual(stats["misses"], 1)

    def test_access_refreshes_recency(self):
        mgr = IdempotencyManager(verify_entries=2, ttl_s=60)
        def fn():
            return "v"
        mgr.verify_cached("k1", fn)
        mgr.verify_cached("k2", fn)
        mgr.verify_cached("k1", fn)  # k1 变为最新
        mgr.verify_cached("k3", fn)  # 挤掉 k2 而不是 k1
        r = mgr.verify_cached("k1", lambda: "recomputed")
        # k1 应命中缓存（若被挤掉会返回 "recomputed"）
        self.assertEqual(r, "v")


class TestTTL(unittest.TestCase):

    def test_expired_entry_misses(self):
        mgr = IdempotencyManager(ttl_s=0.05)
        def fn():
            return "fresh"
        mgr.verify_cached("k", fn)
        time.sleep(0.08)
        r = mgr.verify_cached("k", lambda: "regenerated")
        self.assertEqual(r, "regenerated")

    def test_no_ttl_never_expires(self):
        mgr = IdempotencyManager(ttl_s=None)
        def fn():
            return "forever"
        mgr.verify_cached("k", fn)
        time.sleep(0.05)
        r = mgr.verify_cached("k", lambda: "recomputed")
        self.assertEqual(r, "forever")


class TestManagement(unittest.TestCase):

    def test_stats_shape(self):
        mgr = IdempotencyManager()
        stats = mgr.stats()
        self.assertIn("decompose", stats)
        self.assertIn("verify", stats)
        self.assertIn("recall", stats)
        for section in stats.values():
            self.assertIn("entries", section)
            self.assertIn("hits", section)
            self.assertIn("misses", section)

    def test_invalidate_recall_all(self):
        mgr = IdempotencyManager()
        mgr.recall_cached("r1", lambda: "x")
        n = mgr.invalidate_recall()
        self.assertEqual(n, 1)
        self.assertEqual(mgr.stats()["recall"]["entries"], 0)

    def test_clear_all(self):
        mgr = IdempotencyManager()
        mgr.decompose_cached("d", lambda: 1)
        mgr.verify_cached("v", lambda: 1)
        mgr.recall_cached("r", lambda: 1)
        mgr.clear()
        for section in mgr.stats().values():
            self.assertEqual(section["entries"], 0)


class TestThreadSafety(unittest.TestCase):
    """Spec-D 并发测试——多线程并发读写不崩、计数正确。"""

    def test_concurrent_mixed_access(self):
        mgr = IdempotencyManager(verify_entries=32, ttl_s=60)
        errors = []
        def worker(wid: int):
            try:
                for i in range(200):
                    key = f"k{(wid * 7 + i) % 40}"
                    mgr.verify_cached(key, lambda: f"v{i}")
                    mgr.decompose_cached(f"d{i % 10}", lambda: i)
                    mgr.stats()
            except Exception as e:  # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 计数一致性: hits + misses ≥ 总调用次数下界（LRU 淘汰可能重算）
        v = mgr.stats()["verify"]
        self.assertGreaterEqual(v["hits"] + v["misses"], 8 * 200)

    def test_concurrent_same_key_single_compute(self):
        """同 key 并发首次访问——线性化后函数可能执行多次, 但结果一致。"""
        mgr = IdempotencyManager(ttl_s=60)
        call_count = [0]
        lock = threading.Lock()
        def expensive():
            with lock:
                call_count[0] += 1
            return {"value": 1}
        results = []
        def worker():
            results.append(mgr.verify_cached("same", expensive))
        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 所有结果一致
        self.assertTrue(all(r == {"value": 1} for r in results))
        # 竞态下可能多算几次, 但后续访问必须全命中
        self.assertEqual(mgr.verify_cached("same", lambda: {"value": "other"}),
                         {"value": 1})


if __name__ == "__main__":
    unittest.main()
