"""一个最小的「假节点」,实现 Admin 所依赖的节点契约子集,用于本地联调与测试。

可调状态:通过全局 STATE 改 equity/pnl 等,模拟盈亏变化、离线后的恢复等。
用法:
    python3 tests/mock_node.py 8001        # 起一个监听 :8001 的假节点
然后在 Admin 里把 base_url 指到 http://127.0.0.1:8001。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE: dict = {
    "name": "Mock Node",
    "initial_cash": 10_000_000.0,
    "equity": 10_500_000.0,
    "pnl": 500_000.0,
    "pnl_pct": 0.05,
    "day_pnl": 80_000.0,
    "exposure": 0.6,
    "position_count": 2,
    "account_count": 1,
    "accounts_extra": [],  # 额外 POST 进来的账户
}
_lock = threading.Lock()

# 极简 SSE 总线,用于测试节点 -> Admin 的事件触发重拉
_SUBS: set = set()
_SUBS_LOCK = threading.Lock()


def emit(event_type: str = "change") -> None:
    with _SUBS_LOCK:
        subs = list(_SUBS)
    for q in subs:
        try:
            q.put_nowait({"type": event_type, "ts": time.time()})
        except queue.Full:
            pass


def _summary() -> dict:
    s = STATE
    acct = {
        "id": "acct_mock", "name": s["name"], "currency": "CNY", "market": "CN_A",
        "initial_cash": s["initial_cash"], "equity": s["equity"],
        "pnl": s["pnl"], "pnl_pct": s["pnl_pct"], "exposure": s["exposure"],
        "day_pnl": s["day_pnl"], "day_realized_pnl": 0.0,
        "market_value": round(s["equity"] * s["exposure"], 2),
        "unrealized_pnl": s["pnl"],
        "positions": [
            {"symbol": "600519.SH", "name": "贵州茅台", "quantity": 100, "avg_cost": 1600.0,
             "last_price": 1700.0, "market_value": 170000.0, "cost_basis": 160000.0,
             "unrealized_pnl": 10000.0, "day_pnl": 800.0},
            {"symbol": "000001.SZ", "name": "平安银行", "quantity": 1000, "avg_cost": 11.0,
             "last_price": 11.5, "market_value": 11500.0, "cost_basis": 11000.0,
             "unrealized_pnl": 500.0, "day_pnl": 120.0},
        ],
        "sleeves": [],
    }
    return {
        "accounts": [acct],
        "totals": {
            "initial_cash": s["initial_cash"], "equity": s["equity"], "pnl": s["pnl"],
            "pnl_pct": s["pnl_pct"], "market_value": acct["market_value"],
            "exposure": s["exposure"], "account_count": s["account_count"],
            "position_count": s["position_count"],
        },
        "mark": {"mode": "connector_close", "data_source": "mock"},
    }


def _trades() -> dict:
    return {"trades": [
        {"kind": "trade", "id": "evt_1", "timestamp": "2026-06-17T09:31:00+08:00",
         "account_id": "acct_mock", "symbol": "600519.SH", "name": "贵州茅台",
         "side": "BUY", "quantity": 100, "price": 1700.0, "gross_amount": 170000.0,
         "fees": 13.6, "net_cash": -170013.6, "position_after": 100,
         "realized_pnl": None, "voided": False, "reason": "demo"},
    ]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: D401
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/health":
            return self._json({"status": "ok", "database": ":memory:"})
        if p == "/api/meta":
            return self._json({"name": STATE["name"], "version": "1.7.0", "api_version": 1,
                               "data_sources": ["mock"], "default_data_source": "mock",
                               "capabilities": {"accounts": True}})
        if p == "/api/portfolio/summary":
            with _lock:
                return self._json(_summary())
        if p == "/api/audit/trades":
            return self._json(_trades())
        if p == "/api/events/stream":
            return self._sse()
        return self._json({"error": "not found"}, 404)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.close_connection = False
        q = queue.Queue(maxsize=32)
        with _SUBS_LOCK:
            _SUBS.add(q)
        try:
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    evt = q.get(timeout=10.0)
                    self.wfile.write(f"event: change\ndata: {json.dumps(evt)}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _SUBS_LOCK:
                _SUBS.discard(q)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/accounts":
            acct = {"id": f"acct_new_{len(STATE['accounts_extra'])}",
                    "name": payload.get("name") or "Paper Account",
                    "initial_cash": payload.get("initial_cash") or 10_000_000.0}
            STATE["accounts_extra"].append(acct)
            emit("account_created")  # 触发 SSE → Admin 立即重拉
            return self._json({"account": acct}, 201)
        return self._json({"error": "not found"}, 404)


def serve(port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    print(f"mock node on http://127.0.0.1:{port}")
    serve(port).serve_forever()
