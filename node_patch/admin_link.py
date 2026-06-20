"""节点端「小改」drop-in 模块 —— 放进现有 app 的 backend/ 目录。

提供两件事,都不改动任何现有 API 的语义:
  1. self_register():节点启动时向 Admin 自注册(后台线程,best-effort,失败不影响启动)。
  2. admin_token_guard():给写操作(POST 等)加可选的 X-Admin-Token 鉴权。

全部基于环境变量,默认行为=关闭(不设环境变量时,节点行为与改之前完全一致)。
纯标准库,零新增依赖。接入步骤见同目录 PATCH.md。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.request


# ---- 1. 自注册 ----------------------------------------------------------

def self_register() -> None:
    """若设了 ADMIN_URL,则后台线程向 Admin 注册本节点。重复注册幂等。

    需要的环境变量:
      ADMIN_URL    Admin 地址,如 http://192.168.1.10:8800   (不设 = 不自注册)
      NODE_ID      本节点稳定标识(默认取 hostname)
      NODE_NAME    显示名(默认 NODE_ID)
      NODE_TOKEN   本节点的 admin-token(写鉴权用,Admin 反向控制时回传)
      ADMIN_TOKEN  Admin 侧若开启了注册鉴权,这里填共享密钥
      HOST / PORT  用于推断本节点 base_url(HOST 为 0.0.0.0 时用 LAN_IP 或本机 IP)
      LAN_IP       显式指定对外可达 IP(推荐,避免推断错网卡)
    """
    admin_url = os.environ.get("ADMIN_URL", "").strip().rstrip("/")
    if not admin_url:
        return

    node_id = os.environ.get("NODE_ID") or _hostname()
    payload = {
        "id": node_id,
        "name": os.environ.get("NODE_NAME") or node_id,
        "base_url": _self_base_url(),
        "token": os.environ.get("NODE_TOKEN") or None,
        "api_version": _api_version(),
    }
    headers = {"Content-Type": "application/json"}
    shared = os.environ.get("ADMIN_TOKEN")
    if shared:
        headers["X-Admin-Token"] = shared

    def _worker() -> None:
        data = json.dumps(payload).encode("utf-8")
        for attempt in range(5):  # Admin 可能比节点晚起,重试几次
            try:
                req = urllib.request.Request(admin_url + "/api/admin/register",
                                             data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status in (200, 201):
                        print(f"[admin_link] 已向 Admin 注册: {payload['base_url']} -> {admin_url}")
                        return
            except Exception as exc:  # noqa: BLE001 注册失败绝不影响节点启动
                print(f"[admin_link] 注册重试 {attempt + 1}/5 失败: {exc}")
            time.sleep(2.0)

    threading.Thread(target=_worker, name="admin-register", daemon=True).start()


# ---- 2. 写操作鉴权 ------------------------------------------------------

def admin_token_required() -> bool:
    """节点设了 NODE_TOKEN 才启用写鉴权。"""
    return bool(os.environ.get("NODE_TOKEN"))


def check_admin_token(handler) -> bool:
    """在 do_POST(及其它写方法)入口调用。返回 True 放行,False 表示已拒绝(调用方应 return)。

    用法见 PATCH.md。未设 NODE_TOKEN 时永远放行(与改之前行为一致)。
    """
    if not admin_token_required():
        return True
    token = handler.headers.get("X-Admin-Token")
    if token == os.environ.get("NODE_TOKEN"):
        return True
    body = json.dumps({"error": "需要有效的 X-Admin-Token"}).encode("utf-8")
    handler.send_response(401)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return False


# ---- 3. SSE 事件流(Phase 3b)------------------------------------------
# 节点对外暴露 GET /api/events/stream;成交/审计事件发生时 publish 一个轻量"变更信号"。
# Admin 收到信号即立即重拉本节点的 summary/trades(信号不搬运业务数据,契约不变)。

class _Bus:
    def __init__(self, maxq: int = 64) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._maxq = maxq

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxq)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # 慢消费者:丢弃,绝不阻塞交易线程


_BUS = _Bus()


def publish_event(event_type: str = "change", **fields: object) -> None:
    """从节点业务流里调用(如 audit_store.record_event 末尾),广播一个变更信号。

    必须绝对安全 —— 交易主流程不能因为发信号而出错,所以整体 try/except 吞掉。
    """
    try:
        _BUS.publish({"type": event_type, "ts": time.time(), **fields})
    except Exception:  # noqa: BLE001
        pass


def stream_sse(handler) -> None:
    """从节点 do_GET 路由 GET /api/events/stream 到这里。SSE 长连接,带心跳。"""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    handler.close_connection = False
    q = _BUS.subscribe()
    try:
        handler.wfile.write(b"event: hello\ndata: {}\n\n")
        handler.wfile.flush()
        while True:
            try:
                evt = q.get(timeout=15.0)
                payload = json.dumps(evt, ensure_ascii=False)
                handler.wfile.write(f"event: change\ndata: {payload}\n\n".encode("utf-8"))
                handler.wfile.flush()
            except queue.Empty:
                handler.wfile.write(b": keepalive\n\n")  # 心跳保活
                handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # Admin 断开,正常收尾
    finally:
        _BUS.unsubscribe(q)


# ---- 辅助 ---------------------------------------------------------------

def _hostname() -> str:
    import socket
    return socket.gethostname().replace(" ", "-")


def _api_version() -> int | None:
    try:
        from backend.version import API_VERSION  # type: ignore
        return int(API_VERSION)
    except Exception:  # noqa: BLE001
        return None


def _self_base_url() -> str:
    port = os.environ.get("PORT", "8000")
    ip = os.environ.get("LAN_IP")
    if not ip:
        host = os.environ.get("HOST", "127.0.0.1")
        ip = host if host not in ("0.0.0.0", "") else _guess_lan_ip()
    return f"http://{ip}:{port}"


def _guess_lan_ip() -> str:
    """推断本机对外 IP:连一个外部地址看本地 socket 绑到哪个网卡(不真正发包)。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()
