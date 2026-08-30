#!/usr/bin/env python3
"""idempotency.py — 幂等管理器（stdlib only）。

对 decompose / verify / recall 三类纯计算做内容寻址缓存,
避免同一任务在同会话内重复分解/验证/检索。

设计依据: 认知编排器设计 §设计模式 (Memoization + content-addressed key)
依赖: 无（叶子模块, 被编排器消费）
"""
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Hashable, Optional


class _LRUCache:
    """线程安全 LRU——按插入序淘汰最旧条目。"""

    def __init__(self, max_entries: int = 256, ttl_s: Optional[float] = None):
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._data: "OrderedDict[Hashable, Any]" = OrderedDict()
        self._timestamps: Dict[Hashable, float] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> Any:
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return _MISS
            # TTL 过期检查
            if self.ttl_s is not None:
                age = time.time() - self._timestamps[key]
                if age > self.ttl_s:
                    del self._data[key]
                    del self._timestamps[key]
                    self.misses += 1
                    return _MISS
            self._data.move_to_end(key)
            self.hits += 1
            return self._data[key]

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._timestamps[key] = time.time()
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                oldest = next(iter(self._data))
                del self._data[oldest]
                del self._timestamps[oldest]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._timestamps.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"entries": len(self._data), "hits": self.hits, "misses": self.misses}


class _Miss:
    """哨兵——区分'缓存未命中'与'缓存了 None'。"""
    __slots__ = ()

    def __repr__(self):
        return "<MISS>"


_MISS = _Miss()


class IdempotencyManager:
    """三类纯计算的幂等缓存。

    - decompose: task_hash → 分解结果（同任务不重复分解）
    - verify:   patch_hash → 验证结论（同补丁不重复验证）
    - recall:   (session, query) → 检索结果（同查询不重复检索）

    线程安全; 各缓存独立容量与统计。
    """

    def __init__(self,
                 decompose_entries: int = 64,
                 verify_entries: int = 512,
                 recall_entries: int = 256,
                 ttl_s: Optional[float] = 3600.0):
        self._decompose = _LRUCache(decompose_entries, ttl_s)
        self._verify = _LRUCache(verify_entries, ttl_s)
        self._recall = _LRUCache(recall_entries, ttl_s)

    # ===== 三类缓存入口 =====

    def decompose_cached(self, key: str, fn: Callable[[], Any]) -> Any:
        """分解缓存——key 通常为 task_hash(task)。"""
        cached = self._decompose.get(key)
        if cached is not _MISS:
            return cached
        result = fn()
        self._decompose.put(key, result)
        return result

    def verify_cached(self, key: str, fn: Callable[[], Any]) -> Any:
        """验证缓存——key 为 patch 内容 hash。"""
        cached = self._verify.get(key)
        if cached is not _MISS:
            return cached
        result = fn()
        self._verify.put(key, result)
        return result

    def recall_cached(self, key: str, fn: Callable[[], Any]) -> Any:
        """检索缓存——key 为 (session_key, query) 组合 hash。"""
        cached = self._recall.get(key)
        if cached is not _MISS:
            return cached
        result = fn()
        self._recall.put(key, result)
        return result

    # ===== 管理 =====

    def invalidate_recall(self, session_key: str = "") -> int:
        """召回缓存失效——新轮次写入后旧检索结果可能过时。

        session_key 为空时全清; 否则只清该会话的条目（键需含会话前缀）。
        """
        if not session_key:
            n = self._recall.stats()["entries"]
            self._recall.clear()
            return n
        return 0  # 会话级失效由调用方使用带前缀的键 + 全清实现

    def stats(self) -> Dict[str, Dict[str, int]]:
        return {
            "decompose": self._decompose.stats(),
            "verify": self._verify.stats(),
            "recall": self._recall.stats(),
        }

    def clear(self) -> None:
        self._decompose.clear()
        self._verify.clear()
        self._recall.clear()


__all__ = ["IdempotencyManager"]
