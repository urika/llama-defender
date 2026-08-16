"""Unit tests for queue_manager — 请求优先级队列（Phase 1，默认关闭）。

覆盖：bucket 分类边界、优先级顺序、同 bucket FIFO、acquire 超时自动移除、
cancel、stats、estimated_wait 常量/历史估计、worker 数上限、huge 不入队。
"""
import os
import sys
import threading
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import queue_manager
from queue_manager import RequestQueueManager, classify_bucket, decide_huge_action


class TestClassifyBucket(unittest.TestCase):
    """bucket 分类边界（默认阈值：large=80000, huge=200000）。"""

    def test_boundaries(self):
        self.assertEqual(classify_bucket(0, 80000, 200000), "interactive")
        self.assertEqual(classify_bucket(15999, 80000, 200000), "interactive")
        self.assertEqual(classify_bucket(16000, 80000, 200000), "standard")
        self.assertEqual(classify_bucket(79999, 80000, 200000), "standard")
        self.assertEqual(classify_bucket(80000, 80000, 200000), "large")
        self.assertEqual(classify_bucket(199999, 80000, 200000), "large")
        self.assertEqual(classify_bucket(200000, 80000, 200000), "huge")
        self.assertEqual(classify_bucket(724000, 80000, 200000), "huge")

    def test_custom_thresholds(self):
        self.assertEqual(classify_bucket(50000, 40000, 100000), "large")
        self.assertEqual(classify_bucket(100000, 40000, 100000), "huge")


class TestDecideHugeAction(unittest.TestCase):
    """huge bucket 准入决策（Handler do_POST 调用）。

    覆盖：显式 local 未超本地上限 → 放行本地；显式 local 超上限 → reject；
    无 local 头 + 路由开启 → cloud；路由关闭 → reject。
    """

    def test_explicit_local_within_limit_forwards_local(self):
        r = decide_huge_action(220000, "local", "cloud", True, 400000)
        self.assertEqual(r["action"], "local")

    def test_explicit_local_at_limit_boundary(self):
        self.assertEqual(decide_huge_action(400000, "local", "cloud", True, 400000)["action"], "local")
        self.assertEqual(decide_huge_action(400001, "local", "cloud", True, 400000)["action"], "reject")

    def test_explicit_local_over_limit_rejected(self):
        r = decide_huge_action(500000, "local", "cloud", True, 400000)
        self.assertEqual(r["action"], "reject")

    def test_no_header_routes_cloud(self):
        r = decide_huge_action(220000, "", "cloud", True, 400000)
        self.assertEqual(r["action"], "cloud")

    def test_cloud_header_routes_cloud(self):
        r = decide_huge_action(220000, "cloud", "cloud", True, 400000)
        self.assertEqual(r["action"], "cloud")

    def test_routing_disabled_rejects(self):
        r = decide_huge_action(220000, "", "cloud", False, 400000)
        self.assertEqual(r["action"], "reject")

    def test_huge_action_not_cloud_rejects(self):
        r = decide_huge_action(220000, "", "reject", True, 400000)
        self.assertEqual(r["action"], "reject")

    def test_zero_limit_treats_as_unbounded_for_local(self):
        r = decide_huge_action(999999, "local", "cloud", True, 0)
        self.assertEqual(r["action"], "local")


def _ctx(request_id, total_chars, stream=False, bucket=None):
    info = {"request_id": request_id, "total_chars": total_chars, "stream": stream}
    if bucket:
        info["bucket"] = bucket
    return info


class TestEnqueueBasics(unittest.TestCase):
    def test_huge_not_enqueued(self):
        qm = RequestQueueManager(max_workers=1)
        with self.assertRaises(ValueError):
            qm.enqueue(_ctx("r1", 250000))  # classify → huge
        with self.assertRaises(ValueError):
            qm.enqueue(_ctx("r2", 100, bucket="huge"))
        self.assertEqual(qm.stats()["waiting"], 0)

    def test_unknown_bucket_rejected(self):
        qm = RequestQueueManager(max_workers=1)
        with self.assertRaises(ValueError):
            qm.enqueue(_ctx("r1", 100, bucket="vip"))

    def test_enqueue_returns_ticket(self):
        qm = RequestQueueManager(max_workers=1)
        t = qm.enqueue(_ctx("req_abc", 5000, stream=True))
        self.assertEqual(t.request_id, "req_abc")
        self.assertEqual(t.bucket, "interactive")
        self.assertTrue(t.stream)
        self.assertGreaterEqual(t.enqueue_ts, 0)


class TestPriorityOrdering(unittest.TestCase):
    """interactive 插队 large；同 bucket FIFO。"""

    def test_interactive_jumps_ahead_of_large(self):
        qm = RequestQueueManager(max_workers=1)
        t_large = qm.enqueue(_ctx("large", 100000))
        t_inter = qm.enqueue(_ctx("inter", 1000))
        # large 先入队但优先级低：堆顶是 interactive，large 拿不到 worker
        self.assertFalse(qm.acquire(t_large, timeout=0.1))
        # interactive 是堆顶且 worker 空闲 → 立即拿到
        self.assertTrue(qm.acquire(t_inter, timeout=0.1))
        qm.release(t_inter)
        # large 的超时 acquire 已自动出队，需要重新入队
        t_large2 = qm.enqueue(_ctx("large2", 100000))
        self.assertTrue(qm.acquire(t_large2, timeout=0.5))

    def test_same_bucket_fifo(self):
        qm = RequestQueueManager(max_workers=1)
        t1 = qm.enqueue(_ctx("s1", 30000))
        t2 = qm.enqueue(_ctx("s2", 30000))
        self.assertTrue(qm.acquire(t1, timeout=0.5))   # 先入队先拿
        self.assertFalse(qm.acquire(t2, timeout=0.1))  # worker 被占
        qm.release(t1)
        t2b = qm.enqueue(_ctx("s2b", 30000))
        self.assertTrue(qm.acquire(t2b, timeout=0.5))

    def test_position(self):
        qm = RequestQueueManager(max_workers=1)
        t1 = qm.enqueue(_ctx("a", 1000))
        t2 = qm.enqueue(_ctx("b", 50000))
        t3 = qm.enqueue(_ctx("c", 90000))
        self.assertEqual(qm.position(t1), 1)  # interactive 最前
        self.assertEqual(qm.position(t2), 2)  # standard
        self.assertEqual(qm.position(t3), 3)  # large 最后（尽管先入队的是 t2）
        qm.cancel(t1)
        self.assertEqual(qm.position(t1), 0)  # 已不在队列
        self.assertEqual(qm.position(t2), 1)


class TestAcquireTimeoutAndCancel(unittest.TestCase):
    def test_acquire_timeout_auto_removes(self):
        qm = RequestQueueManager(max_workers=1)
        t1 = qm.enqueue(_ctx("w1", 30000))
        self.assertTrue(qm.acquire(t1, timeout=0.5))  # 占住唯一 worker
        t2 = qm.enqueue(_ctx("w2", 30000))
        t0 = time.monotonic()
        self.assertFalse(qm.acquire(t2, timeout=0.2))
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.18)
        # 超时后自动从队列移除
        self.assertEqual(qm.stats()["waiting"], 0)
        self.assertFalse(qm.cancel(t2))  # 已移除，cancel 返回 False
        qm.release(t1)

    def test_cancel(self):
        qm = RequestQueueManager(max_workers=1)
        t1 = qm.enqueue(_ctx("c1", 30000))
        t2 = qm.enqueue(_ctx("c2", 30000))
        self.assertTrue(qm.acquire(t1, timeout=0.5))
        self.assertTrue(qm.cancel(t2))
        self.assertFalse(qm.cancel(t2))  # 重复 cancel
        self.assertEqual(qm.stats()["waiting"], 0)
        # cancel 后 acquire 立即返回 False（不阻塞）
        self.assertFalse(qm.acquire(t2, timeout=1.0))
        qm.release(t1)

    def test_cancelled_entry_purged_lazily(self):
        # 被取消的低优先级条目留在堆里，浮到堆顶时才 purge；不影响后续 acquire
        qm = RequestQueueManager(max_workers=1)
        t1 = qm.enqueue(_ctx("p1", 100000))   # large
        t2 = qm.enqueue(_ctx("p2", 1000))     # interactive
        qm.cancel(t1)                          # large 被取消但沉在堆底
        self.assertTrue(qm.acquire(t2, timeout=0.5))
        qm.release(t2)
        self.assertEqual(qm.stats()["waiting"], 0)

    def test_worker_limit(self):
        qm = RequestQueueManager(max_workers=2)
        t1 = qm.enqueue(_ctx("m1", 30000))
        t2 = qm.enqueue(_ctx("m2", 30000))
        t3 = qm.enqueue(_ctx("m3", 30000))
        self.assertTrue(qm.acquire(t1, timeout=0.5))
        self.assertTrue(qm.acquire(t2, timeout=0.5))
        # 第 3 个请求：两个 worker 都被占 → 超时
        self.assertFalse(qm.acquire(t3, timeout=0.1))
        self.assertEqual(qm.stats()["active"], 2)
        qm.release(t1)
        qm.release(t2)
        self.assertEqual(qm.stats()["active"], 0)


class TestStatsAndEstimate(unittest.TestCase):
    def test_stats_accuracy(self):
        qm = RequestQueueManager(max_workers=1)
        t1 = qm.enqueue(_ctx("s1", 1000))     # interactive
        t2 = qm.enqueue(_ctx("s2", 30000))    # standard
        t3 = qm.enqueue(_ctx("s3", 90000))    # large
        time.sleep(0.05)
        s = qm.stats()
        self.assertEqual(s["workers"], 1)
        self.assertEqual(s["waiting"], 3)
        self.assertEqual(s["active"], 0)
        self.assertEqual(s["by_bucket"], {"interactive": 1, "standard": 1, "large": 1})
        self.assertGreaterEqual(s["oldest_wait_ms"], 40)

    def test_estimated_wait_no_history_uses_constants(self):
        qm = RequestQueueManager(max_workers=1)
        t1 = qm.enqueue(_ctx("e1", 30000))
        self.assertTrue(qm.acquire(t1, timeout=0.5))  # worker 忙
        # standard 无历史 → 常量 10s；(0 等待 + 1 忙) × 10000 / 1
        self.assertEqual(qm.estimated_wait_ms("standard"), 10000)
        self.assertEqual(qm.estimated_wait_ms("interactive"), 2000)
        self.assertEqual(qm.estimated_wait_ms("large"), 60000)
        qm.release(t1)

    def test_estimated_wait_with_history(self):
        qm = RequestQueueManager(max_workers=1)
        t1 = qm.enqueue(_ctx("h1", 30000))
        self.assertTrue(qm.acquire(t1, timeout=0.5))
        qm.release(t1, occupied_ms=4000)  # 写入历史：平均占用 4s
        t2 = qm.enqueue(_ctx("h2", 30000))
        self.assertTrue(qm.acquire(t2, timeout=0.5))
        # 1 个在执行 × 4000ms / 1 worker
        self.assertEqual(qm.estimated_wait_ms("standard"), 4000)
        qm.release(t2, occupied_ms=4000)

    def test_estimated_wait_counts_queue_ahead(self):
        qm = RequestQueueManager(max_workers=1)
        t1 = qm.enqueue(_ctx("q1", 30000))
        self.assertTrue(qm.acquire(t1, timeout=0.5))  # worker 忙
        qm.enqueue(_ctx("q2", 30000))                  # 1 个在等
        qm.enqueue(_ctx("q3", 30000))                  # 2 个在等
        # (2 等待 + 1 忙) × 10000 = 30000
        self.assertEqual(qm.estimated_wait_ms("standard"), 30000)
        # interactive 只统计优先级不更高的：2 个 standard 排在它后面，不计入
        self.assertEqual(qm.estimated_wait_ms("interactive"), 2000)
        qm.release(t1)


class TestConcurrency(unittest.TestCase):
    def test_multithreaded_enqueue_acquire(self):
        """多线程并发入队/获取/释放，验证无死锁、worker 数不超发。"""
        qm = RequestQueueManager(max_workers=2)
        max_active = []
        errors = []

        def worker(i):
            try:
                t = qm.enqueue(_ctx(f"t{i}", 30000))
                if qm.acquire(t, timeout=5.0):
                    max_active.append(qm.stats()["active"])
                    time.sleep(0.01)
                    qm.release(t, occupied_ms=10)
            except Exception as e:  # pragma: no cover - 调试辅助
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)
        self.assertFalse(errors)
        self.assertFalse([a for a in max_active if a > 2])  # 从不超发 worker
        self.assertEqual(qm.stats()["waiting"], 0)
        self.assertEqual(qm.stats()["active"], 0)


class TestProxyStateIntegration(unittest.TestCase):
    """proxy_state 中的队列配置常量与单例。"""

    def test_defaults_registered(self):
        import proxy_config
        for key in ("PROXY_QUEUE_ENABLED", "PROXY_QUEUE_TIMEOUT_SECONDS",
                    "PROXY_QUEUE_LARGE_THRESHOLD_CHARS",
                    "PROXY_QUEUE_HUGE_THRESHOLD_CHARS", "PROXY_QUEUE_HUGE_ACTION"):
            entry = proxy_config.get_registry_entry(key)
            self.assertIsNotNone(entry, key)
            self.assertEqual(entry["scope"], "reloadable", key)

    def test_state_defaults(self):
        import proxy_state
        self.assertFalse(proxy_state.PROXY_QUEUE_ENABLED)  # 默认关闭
        self.assertEqual(proxy_state.PROXY_QUEUE_TIMEOUT_SECONDS, 300)
        self.assertEqual(proxy_state.PROXY_QUEUE_LARGE_THRESHOLD_CHARS, 80000)
        self.assertEqual(proxy_state.PROXY_QUEUE_HUGE_THRESHOLD_CHARS, 200000)
        self.assertEqual(proxy_state.PROXY_QUEUE_HUGE_ACTION, "cloud")

    def test_reload_spec_entries(self):
        import proxy_state
        keys = {e[0] for e in proxy_state._RELOAD_SPEC}
        for key in ("PROXY_QUEUE_ENABLED", "PROXY_QUEUE_TIMEOUT_SECONDS",
                    "PROXY_QUEUE_LARGE_THRESHOLD_CHARS",
                    "PROXY_QUEUE_HUGE_THRESHOLD_CHARS", "PROXY_QUEUE_HUGE_ACTION"):
            self.assertIn(key, keys)

    def test_get_queue_manager_singleton(self):
        import proxy_state
        qm1 = proxy_state.get_queue_manager()
        qm2 = proxy_state.get_queue_manager()
        self.assertIs(qm1, qm2)
        self.assertEqual(qm1.max_workers, proxy_state.PROXY_MAX_CONCURRENT)


class TestAdminQueueJson(unittest.TestCase):
    def test_disabled_returns_static(self):
        import admin_server
        import proxy_state
        if proxy_state.PROXY_QUEUE_ENABLED:
            self.skipTest("需要 PROXY_QUEUE_ENABLED=false 的环境")
        data = admin_server._build_queue_json()
        self.assertEqual(data, {
            "enabled": False,
            "workers": proxy_state.PROXY_MAX_CONCURRENT,
            "waiting": 0,
            "by_bucket": {"interactive": 0, "standard": 0, "large": 0},
            "oldest_wait_ms": 0,
        })


if __name__ == "__main__":
    unittest.main()
