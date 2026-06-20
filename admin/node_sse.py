"""节点 SSE 消费者(Phase 3b)。

为每个启用的节点起一条后台线程,连它的 `GET /api/events/stream`;收到任何变更信号就
回调 on_event(node_id) —— 由 Poller.repoll 立即重拉该节点,实现成交秒级上墙。

设计取舍:
- SSE 只当"变更信号"用,不在 SSE 里搬运业务数据 —— Admin 仍走既有 summary/trades 契约
  重拉,避免在两处重复维护节点的数据模型。
- 节点没装 SSE 补丁(404)→ 长退避重试,期间完全退回 Poller 的周期轮询(隔离/降级)。
- 去抖:连续事件最多每 SSE_DEBOUNCE 秒触发一次重拉,防止成交风暴打爆节点。
- 线程均为 daemon:Admin 退出即随进程消亡,不阻塞。
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import Config
from .registry import Registry

SSE_PATH = "/api/events/stream"
SSE_DEBOUNCE = 0.3          # 秒:重拉去抖窗口
RETRY_NO_SSE = 60.0        # 节点不支持 SSE 时的重试间隔
RETRY_ERROR = 5.0          # 其它连接错误的重试间隔
READ_TIMEOUT = 60.0        # 流读超时(需 > 节点心跳间隔)


class _Consumer:
    def __init__(self, node: dict[str, Any], on_event: Callable[[str], Any]) -> None:
        self.node_id = node["id"]
        self.url = node["base_url"].rstrip("/") + SSE_PATH
        self.token = node.get("token")
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"sse-{self.node_id}", daemon=True)
        self._last_fire = 0.0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _fire(self) -> None:
        now = time.monotonic()
        if now - self._last_fire < SSE_DEBOUNCE:
            return
        self._last_fire = now
        try:
            self.on_event(self.node_id)
        except Exception as exc:  # noqa: BLE001 重拉失败不影响 SSE 线程
            print(f"[node_sse] {self.node_id} 重拉失败: {exc}")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._stream_once()
            except urllib.error.HTTPError as exc:
                # 404/501 = 节点没装 SSE 补丁 → 长退避,期间靠周期轮询兜底
                wait = RETRY_NO_SSE if exc.code in (404, 501) else RETRY_ERROR
                self._stop.wait(wait)
            except Exception:  # noqa: BLE001 连接拒绝/超时/节点离线 → 短退避重连
                self._stop.wait(RETRY_ERROR)

    def _stream_once(self) -> None:
        headers = {"Accept": "text/event-stream"}
        if self.token:
            headers["X-Admin-Token"] = self.token
        req = urllib.request.Request(self.url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
            for raw in resp:  # 按行读 SSE 字段;心跳/数据任意一条都视为"有变更"
                if self._stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:") or line.startswith("event:"):
                    self._fire()


class NodeSSEManager:
    """监督者:周期性把消费者集合与注册表对齐(新增起、删除停)。"""

    def __init__(self, cfg: Config, registry: Registry, on_event: Callable[[str], Any]) -> None:
        self.cfg = cfg
        self.registry = registry
        self.on_event = on_event
        self._consumers: dict[str, _Consumer] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._supervise, name="sse-manager", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for c in self._consumers.values():
            c.stop()

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self._reconcile()
            except Exception as exc:  # noqa: BLE001
                print(f"[node_sse] 监督异常(已忽略): {exc}")
            self._stop.wait(5.0)  # 每 5s 对齐一次注册表

    def _reconcile(self) -> None:
        nodes = {n["id"]: n for n in self.registry.enabled()}
        # 停掉已删除/禁用的
        for node_id in list(self._consumers):
            if node_id not in nodes:
                self._consumers.pop(node_id).stop()
        # 为新节点起消费者
        for node_id, node in nodes.items():
            if node_id not in self._consumers:
                c = _Consumer(node, self.on_event)
                self._consumers[node_id] = c
                c.start()
