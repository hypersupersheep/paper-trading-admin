"""Admin 单元 + 集成测试。用 unittest(标准库),零依赖。

运行:  python3 -m pytest tests/  或  python3 -m unittest tests.test_admin
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin.alerts import AlertEngine
from admin.config import Config
from admin.db import Database
from admin.events import EventBus
from admin.node_sse import NodeSSEManager
from admin.poller import Poller
from admin.registry import Registry
from admin.server import build_overview
from tests import mock_node


def _tmp_db(name: str) -> Database:
    import tempfile
    path = Path(tempfile.mkdtemp()) / name
    return Database(path)


class TestDatabase(unittest.TestCase):
    def test_state_roundtrip_and_offline_keeps_last_known(self):
        db = _tmp_db("a.db")
        db.save_state({"node_id": "n1", "status": "online", "equity": 100.0,
                       "pnl": 5.0, "pnl_pct": 0.05, "consecutive_fail": 0})
        st = db.get_state("n1")
        self.assertEqual(st["status"], "online")
        self.assertAlmostEqual(st["equity"], 100.0)
        # 覆盖写保留语义:再写一次离线但不带 equity → 调用方需自己保留(poller 的责任)
        db.save_state({"node_id": "n1", "status": "offline", "equity": st["equity"],
                       "consecutive_fail": 2})
        self.assertEqual(db.get_state("n1")["status"], "offline")
        self.assertAlmostEqual(db.get_state("n1")["equity"], 100.0)

    def test_samples_time_order(self):
        db = _tmp_db("b.db")
        for i, ts in enumerate(["2026-01-01T00:00:03", "2026-01-01T00:00:01", "2026-01-01T00:00:02"]):
            db.add_sample({"node_id": "n", "ts": ts, "equity": float(i), "pnl": 0,
                           "pnl_pct": 0, "day_pnl": 0, "exposure": 0, "position_count": 0})
        rows = db.samples("n")
        self.assertEqual([r["ts"] for r in rows],
                         ["2026-01-01T00:00:01", "2026-01-01T00:00:02", "2026-01-01T00:00:03"])


class TestRegistry(unittest.TestCase):
    def test_register_validation_and_idempotent(self):
        reg = Registry(_tmp_db("r.db"))
        with self.assertRaises(ValueError):
            reg.register({"base_url": "ftp://bad"})
        node = reg.register({"name": "Alice", "base_url": "http://10.0.0.1:8000/"})
        self.assertEqual(node["base_url"], "http://10.0.0.1:8000")  # 去尾斜杠
        self.assertEqual(node["_action"], "created")
        again = reg.register({"id": node["id"], "base_url": "http://10.0.0.2:9000"})
        self.assertEqual(again["_action"], "updated")
        self.assertEqual(len(reg.list()), 1)

    def test_slug_id_from_name(self):
        reg = Registry(_tmp_db("r2.db"))
        node = reg.register({"name": "张三 的机器!", "base_url": "http://1.2.3.4:8000"})
        self.assertRegex(node["id"], r"^[A-Za-z0-9._-]+$")


class TestAlertEngine(unittest.TestCase):
    """模拟盘只做连通性告警:离线 / 恢复,边沿触发,不重复刷屏。"""

    def setUp(self):
        self.eng = AlertEngine(Config())

    def test_offline_edge_triggered_once_then_recovered(self):
        st_off = {"status": "offline", "consecutive_fail": 2}
        a1 = self.eng.evaluate("n", "N", st_off)
        a2 = self.eng.evaluate("n", "N", st_off)  # 仍离线 → 不重复
        self.assertEqual([a["rule"] for a in a1], ["node_offline"])
        self.assertEqual(a1[0]["severity"], "critical")
        self.assertEqual(a2, [])
        rec = self.eng.evaluate("n", "N", {"status": "online"})
        self.assertEqual([a["rule"] for a in rec], ["node_recovered"])
        self.assertEqual(rec[0]["severity"], "info")

    def test_no_threshold_alerts_for_paper_trading(self):
        # 大回撤 / 高仓位都不应产生任何告警(模拟盘无交易阈值)
        a = self.eng.evaluate("n", "N", {"status": "online", "pnl_pct": -0.5, "exposure": 0.99})
        self.assertEqual(a, [])


class TestPollerIntegration(unittest.TestCase):
    """起一个真 mock 节点,跑一轮 poll,验证 state/sample/overview。"""

    def test_poll_once_against_mock_node(self):
        httpd = mock_node.serve(8731)
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            db = _tmp_db("poll.db")
            cfg = Config(sample_every=0.0)  # 强制每轮采样
            reg = Registry(db)
            reg.register({"id": "mock", "name": "Mock", "base_url": "http://127.0.0.1:8731",
                          "data_source": "mock"})
            poller = Poller(cfg, db, reg, AlertEngine(cfg))
            poller.poll_once()

            st = db.get_state("mock")
            self.assertEqual(st["status"], "online")
            self.assertAlmostEqual(st["equity"], 10_500_000.0)
            self.assertAlmostEqual(st["pnl_pct"], 0.05)
            self.assertEqual(st["position_count"], 2)
            self.assertAlmostEqual(st["day_pnl"], 80_000.0)
            self.assertGreaterEqual(len(db.samples("mock")), 1)

            ov = build_overview(db, reg)
            self.assertEqual(ov["totals"]["online"], 1)
            self.assertEqual(ov["leaderboard"][0]["id"], "mock")
            self.assertTrue(ov["nodes"][0]["spark"])  # 有 sparkline 数据
        finally:
            httpd.shutdown()

    def test_offline_node_marked_after_fails(self):
        db = _tmp_db("off.db")
        cfg = Config(offline_after_fails=2)
        reg = Registry(db)
        reg.register({"id": "dead", "name": "Dead", "base_url": "http://127.0.0.1:9", "data_source": "x"})
        poller = Poller(cfg, db, reg, AlertEngine(cfg))
        poller.poll_once()
        self.assertEqual(db.get_state("dead")["status"], "degraded")  # 第 1 次失败
        poller.poll_once()
        self.assertEqual(db.get_state("dead")["status"], "offline")   # 第 2 次 → 离线
        # 离线触发了告警
        self.assertIn("node_offline", [a["rule"] for a in db.list_alerts()])


class TestEventBus(unittest.TestCase):
    def test_publish_to_subscribers_and_drop_oldest_when_full(self):
        bus = EventBus(max_queue=2)
        q = bus.subscribe()
        bus.publish({"n": 1}); bus.publish({"n": 2}); bus.publish({"n": 3})  # 队列满 → 丢最旧
        got = [q.get_nowait()["n"], q.get_nowait()["n"]]
        self.assertEqual(got, [2, 3])
        bus.unsubscribe(q)
        self.assertEqual(bus.subscriber_count, 0)

    def test_poller_publishes_tick_after_repoll(self):
        httpd = mock_node.serve(8732)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            db = _tmp_db("tick.db"); cfg = Config(sample_every=0.0)
            reg = Registry(db); bus = EventBus()
            reg.register({"id": "m", "name": "M", "base_url": "http://127.0.0.1:8732", "data_source": "mock"})
            poller = Poller(cfg, db, reg, AlertEngine(cfg), bus=bus)
            q = bus.subscribe()
            self.assertTrue(poller.repoll("m"))
            evt = q.get(timeout=2.0)
            self.assertEqual(evt["type"], "tick")
            self.assertEqual(evt["reason"], "event")
            self.assertEqual(evt["node_id"], "m")
        finally:
            httpd.shutdown()


class TestNodeSSE(unittest.TestCase):
    """节点推 SSE 信号 → Admin 消费者触发 on_event(立即重拉)。"""

    def test_consumer_fires_on_node_event(self):
        httpd = mock_node.serve(8733)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        fired: list[str] = []
        try:
            reg = Registry(_tmp_db("sse.db"))
            reg.register({"id": "sse1", "name": "SSE1", "base_url": "http://127.0.0.1:8733"})
            mgr = NodeSSEManager(Config(), reg, on_event=lambda nid: fired.append(nid))
            mgr._reconcile()  # 直接起消费者,免等 5s 监督周期
            time.sleep(0.5)   # 等 SSE 连上
            mock_node.emit("trade")  # 节点广播变更信号
            deadline = time.time() + 3.0
            while not fired and time.time() < deadline:
                time.sleep(0.05)
            mgr.stop()
            self.assertEqual(fired[:1], ["sse1"])
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
