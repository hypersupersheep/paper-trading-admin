"""装配启动入口:`python3 -m admin`。

启动顺序:建 DB → 注册表/告警引擎 → 起轮询线程 → 起 HTTP 服务。
轮询器是独立 daemon 线程;HTTP 服务挂了或 Ctrl-C,轮询器随进程退出,不影响任何节点。
"""

from __future__ import annotations

import os
import signal
import sys

from .alerts import AlertEngine
from .config import Config
from .db import Database
from .events import EventBus
from .node_sse import NodeSSEManager
from .poller import Poller
from .registry import Registry
from .server import Services, serve


def main() -> int:
    cfg = Config.from_env()
    db = Database(cfg.db_path)
    registry = Registry(db)
    engine = AlertEngine(cfg)
    bus = EventBus()
    poller = Poller(cfg, db, registry, engine, bus=bus)
    # 节点 SSE 消费者:某节点推来变更信号 → 立即重拉该节点(事件触发轮询)
    sse_mgr = NodeSSEManager(cfg, registry, on_event=poller.repoll)
    services = Services(cfg, db, registry, engine, poller, bus)

    poller.start()
    sse_mgr.start()
    httpd = serve(services)

    def _shutdown(*_: object) -> None:
        print("\n[admin] 收到退出信号,停止中…")
        poller.stop()
        sse_mgr.stop()
        httpd.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 打包成 app 双击运行时,自动在浏览器打开监控墙(可用 ADMIN_NO_OPEN=1 关闭)
    if getattr(sys, "frozen", False) and not os.environ.get("ADMIN_NO_OPEN"):
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{cfg.port}")).start()

    n = len(registry.list())
    auth = "开启 (X-Admin-Token)" if cfg.admin_token else "关闭(可信局域网)"
    print(f"Paper Trading Admin — 监听 http://{cfg.host}:{cfg.port}")
    print(f"  DB:        {cfg.db_path}")
    print(f"  轮询:      每 {cfg.poll_interval}s,超时 {cfg.poll_timeout}s,并发 {cfg.poll_workers}")
    print(f"  写鉴权:    {auth}")
    print(f"  已注册节点: {n}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _shutdown()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
