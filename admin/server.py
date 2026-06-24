"""Admin HTTP 服务:/api/admin/* 只读聚合 + 注册 + 反向控制代理 + 静态前端。

与节点同构:stdlib ThreadingHTTPServer。服务只读 DB(轮询器单独线程写),
唯一对节点的写通道是 /control 代理 —— 显式、用户触发、可选 token 鉴权。
"""

from __future__ import annotations

import json
import mimetypes
import queue
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .alerts import AlertEngine
from .config import Config
from .db import Database
from .events import EventBus
from .node_client import NodeClient, NodeError
from .registry import Registry


class Services:
    """装配好的依赖集合,注入给 Handler。"""

    def __init__(self, cfg: Config, db: Database, registry: Registry, engine: AlertEngine,
                 poller: Any, bus: EventBus) -> None:
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.engine = engine
        self.poller = poller
        self.bus = bus


# ---- 聚合视图构建 -------------------------------------------------------

_STATE_FIELDS = ("status", "last_ok_at", "last_error", "latency_ms", "consecutive_fail",
                 "equity", "pnl", "pnl_pct", "day_pnl", "exposure",
                 "position_count", "account_count", "updated_at")


def _node_card(node: dict[str, Any], state: dict[str, Any] | None, spark: list[float]) -> dict[str, Any]:
    card = {
        "id": node["id"], "name": node.get("name") or node["id"],
        "base_url": node["base_url"], "data_source": node.get("data_source"),
        "api_version": node.get("api_version"), "enabled": bool(node.get("enabled", 1)),
        "status": "unknown", "spark": spark,
    }
    if state:
        card.update({k: state.get(k) for k in _STATE_FIELDS})
    return card


def _account_card(reg: dict[str, Any], node: dict[str, Any] | None,
                  state: dict[str, Any] | None, metrics: dict[str, Any] | None,
                  spark: list[float]) -> dict[str, Any]:
    """一张账户卡:身份(注册表)+ 实时指标(节点 summary 里该账户)+ 节点连通性。"""
    node_status = (state or {}).get("status", "unknown")
    present = metrics is not None
    if node_status == "online":
        status = "online" if present else "missing"   # 节点在线但 summary 里没这账户(已删/未同步)
    elif node_status in ("offline", "degraded"):
        status = node_status                           # 用最后已知指标兜底展示
    else:
        status = "unknown"
    m = metrics or {}
    return {
        "node_id": reg["node_id"], "account_id": reg["account_id"],
        "owner": reg.get("owner") or reg.get("name") or reg["account_id"],
        "name": reg.get("name") or reg["account_id"],
        "node_name": (node or {}).get("name") or reg["node_id"],
        "status": status, "last_ok_at": (state or {}).get("last_ok_at"),
        "equity": _to_num(m.get("equity")), "pnl": _to_num(m.get("pnl")),
        "pnl_pct": _to_num(m.get("pnl_pct")), "exposure": _to_num(m.get("exposure")),
        "day_pnl": _to_num(m.get("day_pnl")),
        "position_count": len(m.get("positions") or []),
        "spark": spark,
    }


def _to_num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_overview(db: Database, registry: Registry) -> dict[str, Any]:
    nodes = {n["id"]: n for n in registry.list()}
    states = db.all_states()

    # 节点卡(连通性视图,仍保留)
    node_cards = [
        _node_card(node, states.get(node["id"]),
                   [s["equity"] for s in db.samples(node["id"], limit=60) if s.get("equity") is not None])
        for node in nodes.values()
    ]

    # 账户卡(监控单元):按已登记账户,从其节点最近 summary 里取该账户实时指标
    summaries: dict[str, dict] = {}
    for nid, st in states.items():
        if st.get("summary_json"):
            try:
                summaries[nid] = json.loads(st["summary_json"])
            except (ValueError, TypeError):
                pass

    account_cards: list[dict[str, Any]] = []
    for reg in registry.accounts():
        nid, aid = reg["node_id"], reg["account_id"]
        accounts = (summaries.get(nid) or {}).get("accounts") or []
        metrics = next((a for a in accounts if a.get("id") == aid), None)
        spark = [s["equity"] for s in db.account_samples(nid, aid, limit=60) if s.get("equity") is not None]
        account_cards.append(_account_card(reg, nodes.get(nid), states.get(nid), metrics, spark))

    live = [c for c in account_cards if c["status"] == "online"]
    totals = {
        "account_count": len(account_cards),
        "online": len(live),
        "node_count": len(node_cards),
        "node_online": sum(1 for c in node_cards if c["status"] == "online"),
        "equity": round(sum(c.get("equity") or 0.0 for c in live), 2),
        "pnl": round(sum(c.get("pnl") or 0.0 for c in live), 2),
        "position_count": sum(int(c.get("position_count") or 0) for c in live),
    }
    # 排行榜:按账户总收益率降序(离线账户不参与排名)
    leaderboard = [
        {"account_id": c["account_id"], "owner": c["owner"], "name": c["name"],
         "pnl_pct": c["pnl_pct"], "pnl": c.get("pnl"), "day_pnl": c.get("day_pnl"), "equity": c.get("equity")}
        for c in sorted(
            [c for c in account_cards if c.get("pnl_pct") is not None and c["status"] != "offline"],
            key=lambda c: c["pnl_pct"], reverse=True)
    ]
    alerts = db.list_alerts(limit=50, unack_only=True)
    return {"accounts": account_cards, "nodes": node_cards,
            "totals": totals, "leaderboard": leaderboard, "alerts": alerts}


# ---- Handler ------------------------------------------------------------

def build_handler(services: Services) -> type[BaseHTTPRequestHandler]:
    cfg = services.cfg
    db = services.db
    registry = services.registry
    bus = services.bus

    class Handler(BaseHTTPRequestHandler):
        server_version = "ptadmin/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # 静默默认访问日志(降噪)
            pass

        # ---- 工具 ----
        def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionError):
                # 浏览器在响应写完前断开(刷新/切走/SSE 重连),正常现象,静默放弃
                self.close_connection = True

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("请求体非合法 JSON")
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

        def _auth_ok(self) -> bool:
            """写操作鉴权:设了 ADMIN_TOKEN 才校验 X-Admin-Token。"""
            if not cfg.admin_token:
                return True
            return self.headers.get("X-Admin-Token") == cfg.admin_token

        def _query(self) -> dict[str, str]:
            q = urlparse(self.path).query
            return {k: v[-1] for k, v in parse_qs(q).items()}

        # ---- 路由 ----
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            try:
                if path == "/api/admin/overview":
                    self._json(build_overview(db, registry))
                    return
                if path == "/api/admin/nodes":
                    self._json({"nodes": registry.list()})
                    return
                if path == "/api/admin/alerts":
                    q = self._query()
                    self._json({"alerts": db.list_alerts(
                        limit=int(q.get("limit") or 100),
                        unack_only=q.get("unack") in ("1", "true"))})
                    return
                if path == "/api/admin/accounts":
                    self._json({"accounts": registry.accounts(self._query().get("node_id"))})
                    return
                if path == "/api/admin/events":
                    self._sse_events()
                    return
                if path.startswith("/api/admin/nodes/"):
                    rest = path.removeprefix("/api/admin/nodes/")
                    node_id = unquote(rest.split("/")[0])
                    if rest.endswith("/trades"):
                        self._node_trades(node_id)
                        return
                    if rest.endswith("/history"):
                        self._json({"samples": db.samples(node_id, int(self._query().get("limit") or 240))})
                        return
                    self._node_detail(node_id)
                    return
                self._static(path)
            except (BrokenPipeError, ConnectionError):
                self.close_connection = True   # 客户端断开,无需也无法回错
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                payload = self._read_json()
                # 注册类(节点自注册 / 手动加)
                if path in ("/api/admin/nodes", "/api/admin/register"):
                    if not self._auth_ok():
                        self._json({"error": "需要有效的 X-Admin-Token"}, HTTPStatus.UNAUTHORIZED)
                        return
                    source = "self" if path.endswith("register") else "manual"
                    node = registry.register(payload, source=source)
                    self._json({"node": node}, HTTPStatus.CREATED)
                    return
                # 账户级登记(app 开户/登记后调用;支持单条 account 或批量 accounts[])
                if path == "/api/admin/accounts/register":
                    if not self._auth_ok():
                        self._json({"error": "需要有效的 X-Admin-Token"}, HTTPStatus.UNAUTHORIZED)
                        return
                    self._json(registry.register_accounts(payload), HTTPStatus.CREATED)
                    return
                # 账户注销:/api/admin/accounts/{node_id}/{account_id}/delete
                if path.startswith("/api/admin/accounts/") and path.endswith("/delete"):
                    if not self._auth_ok():
                        self._json({"error": "需要有效的 X-Admin-Token"}, HTTPStatus.UNAUTHORIZED)
                        return
                    rest = path.removeprefix("/api/admin/accounts/").removesuffix("/delete")
                    parts = rest.split("/")
                    if len(parts) < 2:
                        raise ValueError("路径需为 /api/admin/accounts/{node_id}/{account_id}/delete")
                    node_id = unquote(parts[0])
                    account_id = unquote("/".join(parts[1:]))
                    self._json({"deregistered": registry.deregister_account(node_id, account_id)})
                    return
                if path.startswith("/api/admin/nodes/") and path.endswith("/delete"):
                    if not self._auth_ok():
                        self._json({"error": "需要有效的 X-Admin-Token"}, HTTPStatus.UNAUTHORIZED)
                        return
                    node_id = unquote(path.removeprefix("/api/admin/nodes/").removesuffix("/delete"))
                    self._json({"deleted": registry.delete(node_id)})
                    return
                if path.startswith("/api/admin/nodes/") and path.endswith("/control"):
                    node_id = unquote(path.removeprefix("/api/admin/nodes/").removesuffix("/control"))
                    self._control(node_id, payload)
                    return
                if path.startswith("/api/admin/alerts/") and path.endswith("/ack"):
                    alert_id = int(unquote(path.removeprefix("/api/admin/alerts/").removesuffix("/ack")))
                    db.ack_alert(alert_id)
                    self._json({"acknowledged": alert_id})
                    return
                self._json({"error": f"未知端点: {path}"}, HTTPStatus.NOT_FOUND)
            except (BrokenPipeError, ConnectionError):
                self.close_connection = True   # 客户端断开,无需也无法回错
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        # ---- 端点实现 ----
        def _node_detail(self, node_id: str) -> None:
            node = registry.get(node_id)
            if not node:
                self._json({"error": f"未知节点: {node_id}"}, HTTPStatus.NOT_FOUND)
                return
            state = db.get_state(node_id) or {}
            summary = json.loads(state["summary_json"]) if state.get("summary_json") else None
            meta = json.loads(state["meta_json"]) if state.get("meta_json") else None
            flat = {k: state.get(k) for k in _STATE_FIELDS}
            self._json({"node": node, "state": flat, "summary": summary, "meta": meta})

        def _node_trades(self, node_id: str) -> None:
            state = db.get_state(node_id)
            if not state or not state.get("trades_json"):
                self._json({"trades": []})
                return
            self._json(json.loads(state["trades_json"]))

        def _control(self, node_id: str, payload: dict[str, Any]) -> None:
            """反向控制:把请求代理到节点(带节点 token)。唯一对节点的写通道。"""
            if not self._auth_ok():
                self._json({"error": "需要有效的 X-Admin-Token"}, HTTPStatus.UNAUTHORIZED)
                return
            node = registry.get(node_id)
            if not node:
                self._json({"error": f"未知节点: {node_id}"}, HTTPStatus.NOT_FOUND)
                return
            method = str(payload.get("method") or "POST").upper()
            target = str(payload.get("path") or "")
            if not target.startswith("/api/"):
                raise ValueError("path 必须以 /api/ 开头")
            if method not in ("GET", "POST"):
                raise ValueError("method 仅支持 GET / POST")
            client = NodeClient(node["base_url"], node.get("token"), timeout=max(cfg.poll_timeout * 2, 5.0))
            try:
                result, status, latency = client.request(method, target, payload.get("body"))
            except NodeError as exc:
                self._json({"error": f"代理到节点失败: {exc}"}, HTTPStatus.BAD_GATEWAY)
                return
            self._json({"ok": True, "node_id": node_id, "status": status,
                        "latency_ms": latency, "result": result})

        def _static(self, path: str) -> None:
            if path in ("", "/"):
                path = "/index.html"
            target = (cfg.public_dir / path.lstrip("/")).resolve()
            # 防目录穿越:必须落在 public/ 内
            if cfg.public_dir.resolve() not in target.parents and target != cfg.public_dir.resolve():
                self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
                return
            if not target.is_file():
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            body = target.read_bytes()
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        # ---- SSE:把最新 overview 推给浏览器(Phase 3a)----
        def _sse_events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # 防反向代理缓冲
            self.end_headers()
            self.close_connection = False
            q = bus.subscribe()
            try:
                self._sse_emit(build_overview(db, registry))  # 首帧:立即给一份当前快照
                while True:
                    try:
                        q.get(timeout=15.0)
                        self._sse_emit(build_overview(db, registry))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")  # 心跳,保活
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # 浏览器断开,正常收尾
            finally:
                bus.unsubscribe(q)
                # 标记连接关闭:别让 keep-alive 循环再去 recv 已断开的 socket(否则刷 traceback)
                self.close_connection = True

        def _sse_emit(self, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"event: overview\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

    return Handler


def serve(services: Services) -> ThreadingHTTPServer:
    handler = build_handler(services)
    httpd = ThreadingHTTPServer((services.cfg.host, services.cfg.port), handler)
    return httpd
