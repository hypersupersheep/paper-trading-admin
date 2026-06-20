"""进程内事件总线 —— 连接「轮询器/SSE 消费者(生产者)」与「浏览器 SSE(消费者)」。

轮询器每轮完成、或某节点被事件触发重拉后,向总线 publish 一个 tick;
每个浏览器 SSE 连接 subscribe 一个有界队列,收到 tick 就把最新 overview 推给前端。

有界队列 + 丢最旧:某个慢客户端不会把内存撑爆,也不阻塞生产者(隔离)。
"""

from __future__ import annotations

import queue
import threading
from typing import Any


class EventBus:
    def __init__(self, max_queue: int = 64) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 慢消费者:丢最旧,塞最新,绝不阻塞生产者
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
