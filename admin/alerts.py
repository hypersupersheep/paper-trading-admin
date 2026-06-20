"""告警引擎 —— 模拟盘只做**连通性**告警(节点离线 / 恢复),不做任何交易阈值告警。

为什么只留连通性:这是 paper trading 监控,盈亏回撤本身不是"风险事件";
真正需要老板知道的是"某同事的 app 掉线了 / 又上线了"。

边沿触发:仅状态翻转的瞬间落一条 alert,不每轮刷屏。状态机在内存(_offline),
Admin 重启后首轮可能对仍离线的节点补发一次,可接受(宁可重报不漏报)。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import Config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AlertEngine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._offline: dict[str, bool] = {}  # node_id -> 当前是否处于离线告警态

    def evaluate(self, node_id: str, name: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        """返回本轮需要落库的告警(0 或 1 条)。"""
        offline = state.get("status") == "offline"
        was_offline = self._offline.get(node_id, False)

        if offline and not was_offline:
            self._offline[node_id] = True
            return [{
                "node_id": node_id, "ts": _now(), "severity": "critical",
                "rule": "node_offline",
                "message": f"[{name}] 节点离线(连续失败 {state.get('consecutive_fail')} 次)",
                "context": {"last_error": state.get("last_error")},
            }]
        if not offline and was_offline:
            self._offline[node_id] = False
            return [{
                "node_id": node_id, "ts": _now(), "severity": "info",
                "rule": "node_recovered", "message": f"[{name}] 节点恢复在线", "context": {},
            }]
        return []
