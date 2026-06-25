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
from admin.registry import _fix_base_url_host
from admin.server import _health, _public_node, build_overview
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


class TestAccountRegistry(unittest.TestCase):
    """账户级登记:单条 / 批量 / 幂等 / owner 回退 / 注销。"""

    def _reg(self):
        return Registry(_tmp_db("acct.db"))

    NODE = {"id": "alice-mbp", "name": "Alice", "base_url": "http://10.0.0.5:8000"}

    def test_register_single_and_owner_fallback(self):
        reg = self._reg()
        out = reg.register_accounts({"node": self.NODE,
                                     "account": {"id": "acct_1", "name": "主账户"}})
        self.assertEqual(out["node_id"], "alice-mbp")
        self.assertEqual(out["accounts"][0]["_action"], "created")
        self.assertEqual(out["accounts"][0]["owner"], "主账户")  # 缺 owner → 回退账户名
        self.assertIsNotNone(reg.db.get_node("alice-mbp"))       # 节点也被 upsert

    def test_register_bulk_and_idempotent(self):
        reg = self._reg()
        reg.register_accounts({"node": self.NODE, "accounts": [
            {"id": "a1", "owner": "Alice"}, {"id": "a2", "owner": "Alice"}]})
        self.assertEqual(len(reg.accounts("alice-mbp")), 2)
        again = reg.register_accounts({"node": self.NODE,
                                       "account": {"id": "a1", "owner": "Alice"}})
        self.assertEqual(again["accounts"][0]["_action"], "updated")
        self.assertEqual(len(reg.accounts("alice-mbp")), 2)  # 幂等,不增

    def test_missing_account_id_rejected(self):
        reg = self._reg()
        with self.assertRaises(ValueError):
            reg.register_accounts({"node": self.NODE, "account": {"owner": "x"}})

    def test_deregister(self):
        reg = self._reg()
        reg.register_accounts({"node": self.NODE, "account": {"id": "a1"}})
        self.assertTrue(reg.deregister_account("alice-mbp", "a1"))
        self.assertFalse(reg.deregister_account("alice-mbp", "a1"))  # 再删 → False
        self.assertEqual(reg.accounts("alice-mbp"), [])


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
            # 登记节点里的账户,才会进入账户级监控
            reg.register_accounts({"node": {"id": "mock", "base_url": "http://127.0.0.1:8731"},
                                   "account": {"id": "acct_mock", "owner": "Mock", "name": "Mock 账户"}})
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
            self.assertEqual(ov["totals"]["node_online"], 1)
            self.assertEqual(ov["totals"]["online"], 1)          # 1 个在线账户
            acct = ov["accounts"][0]
            self.assertEqual(acct["account_id"], "acct_mock")
            self.assertEqual(acct["status"], "online")
            self.assertAlmostEqual(acct["pnl_pct"], 0.05)
            self.assertEqual(acct["owner"], "Mock")
            self.assertEqual(acct["position_count"], 2)          # 从 summary 的 positions 数
            self.assertTrue(acct["spark"])                        # 账户级 sparkline
            self.assertEqual(ov["leaderboard"][0]["account_id"], "acct_mock")
            self.assertTrue(ov["nodes"][0]["spark"])             # 节点级 sparkline 仍在
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

    def test_consumer_connects_with_token_and_fires_on_event(self):
        # 节点 v1.12.0:/api/stream 走入站鉴权。强制 token → 同时验证消费者 SSE 连接带了 node.token。
        mock_node.REQUIRE_TOKEN = "sse-tok"
        httpd = mock_node.serve(8733)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        fired: list[str] = []
        try:
            reg = Registry(_tmp_db("sse.db"))
            reg.register({"id": "sse1", "name": "SSE1", "base_url": "http://127.0.0.1:8733",
                          "token": "sse-tok"})  # 不带 token,/api/stream 会 401 → 永不触发
            mgr = NodeSSEManager(Config(), reg, on_event=lambda nid: fired.append(nid))
            mgr._reconcile()  # 直接起消费者,免等 5s 监督周期
            time.sleep(0.5)   # 等 SSE 连上(带 token 才能连上)
            mock_node.emit("trade_filled")  # 节点推 data: 事件
            deadline = time.time() + 3.0
            while not fired and time.time() < deadline:
                time.sleep(0.05)
            mgr.stop()
            self.assertEqual(fired[:1], ["sse1"])  # 连上了 + data: 事件触发了重拉
        finally:
            httpd.shutdown()
            mock_node.REQUIRE_TOKEN = None


class TestNodeTokenAuth(unittest.TestCase):
    """节点 v1.11.0 鉴权:Admin 轮询 / 反控必须带 X-Admin-Token = node.token。
    用强制校验 token 的 mock 节点确证 poller 与 control 真的带上了 token。"""

    def setUp(self):
        mock_node.REQUIRE_TOKEN = "node-secret-xyz"

    def tearDown(self):
        mock_node.REQUIRE_TOKEN = None

    def test_poller_and_control_carry_node_token(self):
        httpd = mock_node.serve(8736)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            db = _tmp_db("auth.db"); cfg = Config(poll_timeout=2.0)
            reg = Registry(db)
            # 带正确 node.token 登记
            reg.register({"id": "n", "name": "N", "base_url": "http://127.0.0.1:8736",
                          "token": "node-secret-xyz", "data_source": "mock"})
            reg.register_accounts({"node": {"id": "n", "base_url": "http://127.0.0.1:8736",
                                            "token": "node-secret-xyz"},
                                   "account": {"id": "acct_mock", "owner": "N"}})
            poller = Poller(cfg, db, reg, AlertEngine(cfg))
            poller.poll_once()
            self.assertEqual(db.get_state("n")["status"], "online")  # 带 token 才能 200

            # 把 token 改错 → 轮询应被节点 401 → 离线(证明 token 是真在用、且必需)
            reg.register({"id": "n", "base_url": "http://127.0.0.1:8736", "token": "WRONG",
                          "data_source": "mock"})
            poller.poll_once()  # degraded
            poller.poll_once()  # offline
            self.assertEqual(db.get_state("n")["status"], "offline")
        finally:
            httpd.shutdown()


class TestHardening(unittest.TestCase):
    """P0/P1 加固:token 脱敏、base_url 兜底、数据源退回、保留清理、health。"""

    def test_token_redaction(self):
        pub = _public_node({"id": "n", "base_url": "x", "token": "secret-xyz"})
        self.assertNotIn("token", pub)
        self.assertTrue(pub["has_token"])
        self.assertFalse(_public_node({"id": "n", "token": None})["has_token"])

    def test_base_url_host_fix(self):
        # 容器段 172.19.x 且与来源 IP 不符 → 改用来源 IP(保留端口)
        self.assertEqual(_fix_base_url_host("http://172.19.0.1:8000", "192.168.0.186"),
                         "http://192.168.0.186:8000")
        # 正常 LAN 地址 → 不动
        self.assertEqual(_fix_base_url_host("http://192.168.0.50:8000", "192.168.0.186"),
                         "http://192.168.0.50:8000")
        # 来源是回环(本机联调)→ 不动
        self.assertEqual(_fix_base_url_host("http://172.19.0.1:8000", "127.0.0.1"),
                         "http://172.19.0.1:8000")
        # 域名 → 不动
        self.assertEqual(_fix_base_url_host("http://node.lan:8000", "192.168.0.186"),
                         "http://node.lan:8000")

    def test_prune_removes_old_rows(self):
        db = _tmp_db("prune.db")
        db.add_sample({"node_id": "n", "ts": "2020-01-01T00:00:00+00:00", "equity": 1,
                       "pnl": 0, "pnl_pct": 0, "day_pnl": 0, "exposure": 0, "position_count": 0})
        db.add_sample({"node_id": "n", "ts": "2999-01-01T00:00:00+00:00", "equity": 2,
                       "pnl": 0, "pnl_pct": 0, "day_pnl": 0, "exposure": 0, "position_count": 0})
        db.add_alert({"node_id": "n", "ts": "2020-01-01T00:00:00+00:00", "severity": "info", "rule": "x", "message": "old"})
        deleted = db.prune("2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00")
        self.assertEqual(deleted["equity_samples"], 1)
        self.assertEqual(deleted["alerts"], 1)
        self.assertEqual(len(db.samples("n")), 1)  # 未来那条还在

    def test_health(self):
        db = _tmp_db("health.db"); reg = Registry(db)
        reg.register({"id": "n", "base_url": "http://1.2.3.4:8000"})
        h = _health(db, reg)
        self.assertEqual(h["status"], "ok")
        self.assertEqual(h["nodes"], 1)
        self.assertIn("version", h)
        self.assertIn("uptime_seconds", h)

    def test_data_source_fallback_keeps_node_online(self):
        mock_node.FAIL_DATA_SOURCE = True
        httpd = mock_node.serve(8738)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            db = _tmp_db("ds.db"); cfg = Config()
            reg = Registry(db)
            reg.register({"id": "m", "base_url": "http://127.0.0.1:8738", "data_source": "tongdaxin"})
            poller = Poller(cfg, db, reg, AlertEngine(cfg))
            poller.poll_once()
            # 带 data_source 失败 → 退回不带 → 仍在线(保住可见性)
            self.assertEqual(db.get_state("m")["status"], "online")
            self.assertIsNotNone(db.get_state("m")["equity"])
        finally:
            httpd.shutdown()
            mock_node.FAIL_DATA_SOURCE = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
