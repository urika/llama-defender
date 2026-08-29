#!/usr/bin/env python3
"""
queue_manager.py — 请求优先级队列（Request Queue）。

背景：裸 `threading.Semaphore(PROXY_MAX_CONCURRENT)` 无优先级、无超时、无背压，
240K+ chars 的超大请求 prefill 期间会让 16-token 的小请求 TTFT 高达 158s。

本模块提供叠加在 `_llama_lock` 之上的优先级排队层：
- 按 total_chars 分桶：interactive / standard / large / huge
- heapq 最小堆实现，优先级 interactive > standard > large，同 bucket FIFO
- huge bucket 默认不入队（由 Handler 准入控制处理：强制路由云端或 413 拒绝）
- acquire(timeout) 排队超时自动出队；cancel(ticket) 主动移除
- stats() 供 /api/queue 可观测

默认关闭（PROXY_QUEUE_ENABLED=false），开启前不改变任何现有行为。
stdlib only；不 import 项目内其他模块（避免循环依赖，配置由调用方传入）。
"""

import collections
import heapq
import itertools
import threading
import time

BUCKETS = ("interactive", "standard", "large", "huge")

# bucket 优先级（数值越小越先拿到 worker）；huge 不入队，无优先级
_BUCKET_PRIORITY = {"interactive": 0, "standard": 1, "large": 2}

# interactive bucket 的 chars 上限（约 8K tokens，按项目惯用的 chars/2 粗估）
INTERACTIVE_MAX_CHARS = 16000

# 无历史耗时数据时，各 bucket 的单次占用估计（毫秒），用于 estimated_wait_ms
_DEFAULT_DISPATCH_MS = {"interactive": 2000, "standard": 10000, "large": 60000}


def classify_bucket(total_chars, large_threshold, huge_threshold):
    """按请求总字符数分桶。

    huge:        >= huge_threshold（默认 200K chars，不入本地队列）
    large:       >= large_threshold（默认 80K chars）
    interactive: <  16000 chars（约 8K tokens，最高优先级）
    standard:    其余
    """
    if total_chars >= huge_threshold:
        return "huge"
    if total_chars >= large_threshold:
        return "large"
    if total_chars < INTERACTIVE_MAX_CHARS:
        return "interactive"
    return "standard"


def decide_huge_action(total_chars, client_route, huge_action,
                       route_enabled, ctx_chars_limit, force_local=False):
    """huge bucket 准入决策（纯函数，Handler do_POST 调用）。

    huge 请求不入本地队列，处理取决于客户端显式路由头与模型强制本地：

    返回 dict，含 action + 附加信息:
    - {"action": "cloud"}                    # 强制路由云端（无 local 头 + 路由开启 + 非强制本地模型）
    - {"action": "local"}                    # 显式 X-Proxy-Route-To: local 且未超本地上限 → 放行本地
    - {"action": "reject"}                   # 其余：413 拒绝（路由关闭 / 超本地上限 / 强制本地模型
                                             #         不得走云端——数据保密）
    force_local: 模型级强制本地（如 haiku behavior=force + prefer_local）——
                 永不路由云端；超本地上限时拒绝(413)而非 cloud。
    """
    within = ctx_chars_limit <= 0 or total_chars <= ctx_chars_limit
    if force_local:
        return {"action": "local"} if within else {"action": "reject"}
    if client_route == "local" and within:
        return {"action": "local"}
    if huge_action == "cloud" and route_enabled and client_route != "local":
        return {"action": "cloud"}
    return {"action": "reject"}


class QueueTicket:
    """入队凭证。Handler 持有它等待 worker、查询位置、取消排队。"""

    __slots__ = ("request_id", "bucket", "seq", "enqueue_ts", "stream")

    def __init__(self, request_id, bucket, seq, enqueue_ts, stream=False):
        self.request_id = request_id
        self.bucket = bucket
        self.seq = seq                  # 全局单调序号，同 bucket 内做 FIFO tie-break
        self.enqueue_ts = enqueue_ts    # time.monotonic() 时间戳
        self.stream = stream

    def elapsed_wait_ms(self):
        """从入队到当前的等待毫秒数。"""
        return int((time.monotonic() - self.enqueue_ts) * 1000)


class RequestQueueManager:
    """带优先级的请求队列，叠加在 `_llama_lock` 之上（不替代它）。

    - worker 数 = PROXY_MAX_CONCURRENT（构造时传入）
    - acquire(ticket, timeout)：等到"自己位于堆顶且有空闲 worker"才返回 True；
      超时返回 False 并自动把 ticket 从队列移除
    - cancel(ticket)：移除仍在排队的请求（懒惰删除，堆顶 purge 时生效）
    - 线程安全：所有内部状态由同一把 Condition 锁保护
    """

    def __init__(self, max_workers=1, large_threshold=80000, huge_threshold=200000):
        self._max_workers = max(1, int(max_workers))
        self._large_threshold = large_threshold
        self._huge_threshold = huge_threshold
        self._heap = []                      # (priority, seq) 最小堆
        self._tickets = {}                   # seq -> QueueTicket（仍在排队的）
        self._cancelled = set()              # 已取消/超时但尚未从堆中 purge 的 seq
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._active_workers = 0
        # 最近完成请求的 worker 占用毫秒，用于 estimated_wait_ms
        self._dispatch_history = collections.deque(maxlen=50)

    @property
    def max_workers(self):
        return self._max_workers

    def classify(self, total_chars):
        """用管理器构造时的阈值分桶（调用方也可直接用模块函数 classify_bucket）。"""
        return classify_bucket(total_chars, self._large_threshold, self._huge_threshold)

    # ------------------------------------------------------------------
    # 入队 / 取消
    # ------------------------------------------------------------------

    def enqueue(self, ctx_info):
        """入队。ctx_info 至少含 request_id / total_chars；可选 stream / bucket。

        huge bucket 拒绝入队（抛 ValueError）——应由准入控制直接处理。
        """
        total_chars = int(ctx_info.get("total_chars", 0))
        bucket = ctx_info.get("bucket") or self.classify(total_chars)
        if bucket == "huge":
            raise ValueError("huge bucket 不入队，请走准入控制（cloud/reject）")
        if bucket not in _BUCKET_PRIORITY:
            raise ValueError(f"未知 bucket: {bucket!r}")
        with self._cond:
            seq = next(self._seq)
            ticket = QueueTicket(
                request_id=ctx_info.get("request_id", ""),
                bucket=bucket,
                seq=seq,
                enqueue_ts=time.monotonic(),
                stream=bool(ctx_info.get("stream", False)),
            )
            heapq.heappush(self._heap, (_BUCKET_PRIORITY[bucket], seq))
            self._tickets[seq] = ticket
            self._cond.notify_all()
        return ticket

    def cancel(self, ticket):
        """移除仍在排队的请求。已在执行或已移除的返回 False。"""
        with self._cond:
            if ticket.seq not in self._tickets:
                return False
            del self._tickets[ticket.seq]
            self._cancelled.add(ticket.seq)  # 懒惰删除：浮到堆顶时 purge
            self._cond.notify_all()
            return True

    def position(self, ticket):
        """当前排队位置（1-based）。已不在队列中返回 0。"""
        with self._cond:
            if ticket.seq not in self._tickets:
                return 0
            prio = _BUCKET_PRIORITY[ticket.bucket]
            ahead = sum(
                1 for (p, s) in self._heap
                if s in self._tickets and (p, s) < (prio, ticket.seq)
            )
            return ahead + 1

    # ------------------------------------------------------------------
    # worker 获取 / 释放
    # ------------------------------------------------------------------

    def _purge_heap_top(self):
        """弹出堆顶的已取消项（调用时必须持锁）。"""
        while self._heap and self._heap[0][1] in self._cancelled:
            _, seq = heapq.heappop(self._heap)
            self._cancelled.discard(seq)

    def acquire(self, ticket, timeout=None):
        """等待轮到 ticket 且拿到 worker 名额。返回 True 表示已持有 worker。

        timeout（秒）到期仍未拿到：返回 False 并自动从队列移除。
        ticket 已被 cancel：返回 False。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                self._purge_heap_top()
                if ticket.seq not in self._tickets:
                    return False  # 已被 cancel
                if (self._heap and self._heap[0][1] == ticket.seq
                        and self._active_workers < self._max_workers):
                    heapq.heappop(self._heap)
                    del self._tickets[ticket.seq]
                    self._active_workers += 1
                    return True
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # 超时：自动出队
                        del self._tickets[ticket.seq]
                        self._cancelled.add(ticket.seq)
                        return False
                    self._cond.wait(remaining)
                else:
                    self._cond.wait()

    def release(self, ticket, occupied_ms=None):
        """释放 worker 名额。occupied_ms 用于记录本次占用耗时（估计等待用）。"""
        with self._cond:
            if self._active_workers > 0:
                self._active_workers -= 1
            if occupied_ms is not None:
                try:
                    self._dispatch_history.append(float(occupied_ms))
                except (TypeError, ValueError):
                    pass
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # 可观测性
    # ------------------------------------------------------------------

    def estimated_wait_ms(self, bucket):
        """粗略估计该 bucket 新请求的排队等待毫秒数。

        = 排在前面（优先级不更低）的请求数 × 平均 worker 占用耗时 / worker 数。
        无历史数据时按 bucket 常量估计（interactive 2s / standard 10s / large 60s）。
        """
        prio = _BUCKET_PRIORITY.get(bucket, _BUCKET_PRIORITY["standard"])
        with self._cond:
            ahead = sum(
                1 for t in self._tickets.values()
                if _BUCKET_PRIORITY[t.bucket] <= prio
            )
            busy = self._active_workers
            if self._dispatch_history:
                avg_ms = sum(self._dispatch_history) / len(self._dispatch_history)
            else:
                avg_ms = _DEFAULT_DISPATCH_MS.get(
                    bucket, _DEFAULT_DISPATCH_MS["standard"])
            workers = self._max_workers
        return int((ahead + busy) * avg_ms / workers)

    def stats(self):
        """队列深度、各 bucket 计数、最老等待时间（供 /api/queue）。"""
        by_bucket = {"interactive": 0, "standard": 0, "large": 0}
        with self._cond:
            now = time.monotonic()
            oldest_ms = 0
            for t in self._tickets.values():
                by_bucket[t.bucket] = by_bucket.get(t.bucket, 0) + 1
                oldest_ms = max(oldest_ms, int((now - t.enqueue_ts) * 1000))
            return {
                "workers": self._max_workers,
                "active": self._active_workers,
                "waiting": len(self._tickets),
                "by_bucket": by_bucket,
                "oldest_wait_ms": oldest_ms,
            }
