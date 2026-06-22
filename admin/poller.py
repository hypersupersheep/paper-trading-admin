"""后台轮询器:并发拉每个节点的 summary + trades(+ 周期性 meta),落 node_state、
按节流落 equity_samples、求值告警。

隔离与健壮性:
- 一个节点慢/挂被超时隔离在自己的 worker 里,绝不阻塞其它节点。
- 拉取失败 → 保留上次已知指标与原文(node_state 不清空),只翻 status=offline。
- DB 写串行(db 内部锁);告警状态机在单线程的 _process 里更新,无竞态。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .alerts import AlertEngine
from .config import Config
from .db import Database
from .events import EventBus
from .node_client import NodeClient, NodeError
from .registry import Registry

META_EVERY_N_ROUNDS = 20  # data_source 已知时,每 N 轮才重拉一次 meta(取版本)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Poller:
    def __init__(self, cfg: Config, db: Database, registry: Registry, engine: AlertEngine,
                 bus: EventBus | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.engine = engine
        self.bus = bus  # 每轮/每次重拉后 publish tick,驱动浏览器 SSE
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._round = 0
        self._last_sample_at: dict[str, float] = {}

    # ---- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 轮询器绝不能因单轮异常死掉
                print(f"[poller] 本轮异常(已忽略): {exc}")
            elapsed = time.monotonic() - t0
            self._stop.wait(max(0.1, self.cfg.poll_interval - elapsed))

    # ---- 一轮 -----------------------------------------------------------
    def poll_once(self) -> None:
        nodes = self.registry.enabled()
        if not nodes:
            return
        self._round += 1
        want_meta = (self._round % META_EVERY_N_ROUNDS == 1)
        workers = max(1, min(self.cfg.poll_workers, len(nodes)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda n: self._fetch(n, want_meta), nodes))
        for node, res in zip(nodes, results):
            self._process(node, res)
        self._tick(reason="cycle")

    def repoll(self, node_id: str) -> bool:
        """事件触发的单节点立即重拉(节点 SSE 收到变更信号时调用)。返回是否拉到该节点。"""
        node = self.registry.get(node_id)
        if not node or not node.get("enabled", 1):
            return False
        self._process(node, self._fetch(node, want_meta=False))
        self._tick(reason="event", node_id=node_id)
        return True

    def _tick(self, *, reason: str, node_id: str | None = None) -> None:
        if self.bus is not None:
            self.bus.publish({"type": "tick", "reason": reason, "node_id": node_id, "ts": _now()})

    def _fetch(self, node: dict[str, Any], want_meta: bool) -> dict[str, Any]:
        """worker 线程:只做网络 I/O,收敛所有异常成结构化结果。"""
        client = NodeClient(node["base_url"], node.get("token"), self.cfg.poll_timeout)
        res: dict[str, Any] = {"ok": False, "meta": None, "summary": None,
                               "trades": None, "latency": None, "error": None,
                               "data_source": node.get("data_source")}
        try:
            ds = node.get("data_source")
            if want_meta or not ds:
                try:
                    meta, _ = client.get("/api/meta")
                    res["meta"] = meta
                    if not ds:
                        ds = (meta or {}).get("default_data_source")
                        res["data_source"] = ds
                except NodeError:
                    pass  # meta 拿不到不致命,继续试 summary

            # 带 data_source 才会触发节点实时盯市并返回 day_pnl;盯市可能略慢,给更宽超时。
            path = "/api/portfolio/summary"
            if ds:
                path += f"?data_source={quote(str(ds))}&frequency=5m"
            summary, lat = client.get(path, timeout=max(self.cfg.poll_timeout * 2, 4.0))
            res["summary"], res["latency"], res["ok"] = summary, lat, True

            try:
                trades, _ = client.get("/api/audit/trades?limit=50")
                res["trades"] = trades
            except NodeError:
                pass  # 成交拉不到不影响盯市监控
        except NodeError as exc:
            res["error"] = str(exc)
        return res

    def _process(self, node: dict[str, Any], res: dict[str, Any]) -> None:
        node_id, name = node["id"], node.get("name") or node["id"]
        prev = self.db.get_state(node_id) or {}
        now = _now()

        # 发现了 data_source 但注册表里还没存 → 回填,后续轮次免拉 meta
        if res.get("data_source") and not node.get("data_source"):
            updated = {**node, "data_source": res["data_source"], "updated_at": now}
            self.db.upsert_node(updated)

        if not res["ok"]:
            fails = int(prev.get("consecutive_fail") or 0) + 1
            status = "offline" if fails >= self.cfg.offline_after_fails else "degraded"
            state = {
                **{k: prev.get(k) for k in (  # 保留上次已知,离线展示最后状态
                    "equity", "pnl", "pnl_pct", "day_pnl", "exposure",
                    "position_count", "account_count",
                    "summary_json", "trades_json", "meta_json", "last_ok_at")},
                "node_id": node_id, "status": status, "consecutive_fail": fails,
                "last_error": res["error"], "latency_ms": None, "updated_at": now,
            }
            self.db.save_state(state)
            for alert in self.engine.evaluate(node_id, name, state):
                self.db.add_alert(alert)
            return

        summary = res["summary"] or {}
        totals = summary.get("totals") or {}
        accounts = summary.get("accounts") or []
        # day_pnl 在 totals 没有 → 对各账户求和(仅当节点真返回了该字段)
        day_pnl = None
        day_vals = [a.get("day_pnl") for a in accounts if a.get("day_pnl") is not None]
        if day_vals:
            day_pnl = round(sum(float(v) for v in day_vals), 2)

        trades_payload = res.get("trades")
        meta = res.get("meta")

        state = {
            "node_id": node_id, "status": "online", "consecutive_fail": 0,
            "last_error": None, "last_ok_at": now, "latency_ms": int(res["latency"] or 0),
            "equity": _num(totals.get("equity")), "pnl": _num(totals.get("pnl")),
            "pnl_pct": _num(totals.get("pnl_pct")), "exposure": _num(totals.get("exposure")),
            "day_pnl": day_pnl,
            "position_count": totals.get("position_count"),
            "account_count": totals.get("account_count"),
            "summary_json": json.dumps(summary, ensure_ascii=False),
            "trades_json": json.dumps(trades_payload, ensure_ascii=False) if trades_payload else prev.get("trades_json"),
            "meta_json": json.dumps(meta, ensure_ascii=False) if meta else prev.get("meta_json"),
            "updated_at": now,
        }
        self.db.save_state(state)

        # 时序采样:按 sample_every 节流,不是每轮都落
        last = self._last_sample_at.get(node_id, 0.0)
        if time.monotonic() - last >= self.cfg.sample_every:
            self._last_sample_at[node_id] = time.monotonic()
            self.db.add_sample({
                "node_id": node_id, "ts": now,
                "equity": state["equity"], "pnl": state["pnl"], "pnl_pct": state["pnl_pct"],
                "day_pnl": state["day_pnl"], "exposure": state["exposure"],
                "position_count": state["position_count"],
            })
            # 账户级时序:只采**已登记**账户(账户卡的 sparkline)
            registered = {a["account_id"] for a in self.registry.accounts(node_id)}
            for acct in accounts:
                aid = acct.get("id")
                if aid in registered:
                    self.db.add_account_sample({
                        "node_id": node_id, "account_id": aid, "ts": now,
                        "equity": _num(acct.get("equity")), "pnl": _num(acct.get("pnl")),
                        "pnl_pct": _num(acct.get("pnl_pct")), "day_pnl": _num(acct.get("day_pnl")),
                        "exposure": _num(acct.get("exposure")),
                    })

        for alert in self.engine.evaluate(node_id, name, state):
            self.db.add_alert(alert)
