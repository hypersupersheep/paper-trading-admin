"""Admin 自己的 SQLite。与任何节点 DB 物理隔离 —— Admin 永不碰节点的库。

线程模型:轮询线程 + HTTP 处理线程会并发读写。SQLite 用 check_same_thread=False +
每次操作短连接 + WAL,配合 ThreadingHTTPServer 足够(写量极低:每轮几十行覆盖写)。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    base_url     TEXT NOT NULL,
    token        TEXT,
    data_source  TEXT,
    api_version  INTEGER,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS node_state (
    node_id          TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'unknown',
    last_ok_at       TEXT,
    last_error       TEXT,
    latency_ms       INTEGER,
    consecutive_fail INTEGER NOT NULL DEFAULT 0,
    equity           REAL,
    pnl              REAL,
    pnl_pct          REAL,
    day_pnl          REAL,
    exposure         REAL,
    position_count   INTEGER,
    account_count    INTEGER,
    summary_json     TEXT,
    trades_json      TEXT,
    meta_json        TEXT,
    updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS equity_samples (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id        TEXT NOT NULL,
    ts             TEXT NOT NULL,
    equity         REAL,
    pnl            REAL,
    pnl_pct        REAL,
    day_pnl        REAL,
    exposure       REAL,
    position_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_node_ts ON equity_samples(node_id, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT,
    ts           TEXT NOT NULL,
    severity     TEXT NOT NULL,
    rule         TEXT NOT NULL,
    message      TEXT,
    context_json TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # 串行化写,避免 SQLite "database is locked"
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写事务:持锁 + 自动 commit/rollback。"""
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ---- nodes ----------------------------------------------------------
    def upsert_node(self, node: dict[str, Any]) -> None:
        with self.write() as conn:
            conn.execute(
                """
                INSERT INTO nodes (id, name, base_url, token, data_source, api_version,
                                   enabled, created_at, updated_at)
                VALUES (:id, :name, :base_url, :token, :data_source, :api_version,
                        :enabled, :created_at, :updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    name=COALESCE(excluded.name, nodes.name),
                    base_url=excluded.base_url,
                    token=COALESCE(excluded.token, nodes.token),
                    data_source=COALESCE(excluded.data_source, nodes.data_source),
                    api_version=COALESCE(excluded.api_version, nodes.api_version),
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                node,
            )

    def list_nodes(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM nodes"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id ASC"
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql)]

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return dict(row) if row else None

    def delete_node(self, node_id: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            conn.execute("DELETE FROM node_state WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM equity_samples WHERE node_id = ?", (node_id,))

    # ---- node_state -----------------------------------------------------
    def save_state(self, state: dict[str, Any]) -> None:
        cols = ("node_id", "status", "last_ok_at", "last_error", "latency_ms",
                "consecutive_fail", "equity", "pnl", "pnl_pct", "day_pnl",
                "exposure", "position_count", "account_count",
                "summary_json", "trades_json", "meta_json", "updated_at")
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "node_id")
        row = {c: state.get(c) for c in cols}
        with self.write() as conn:
            conn.execute(
                f"INSERT INTO node_state ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(node_id) DO UPDATE SET {updates}",
                row,
            )

    def get_state(self, node_id: str) -> dict[str, Any] | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM node_state WHERE node_id = ?", (node_id,)).fetchone()
        return dict(row) if row else None

    def all_states(self) -> dict[str, dict[str, Any]]:
        with self.read() as conn:
            return {r["node_id"]: dict(r) for r in conn.execute("SELECT * FROM node_state")}

    # ---- equity_samples -------------------------------------------------
    def add_sample(self, sample: dict[str, Any]) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO equity_samples
                   (node_id, ts, equity, pnl, pnl_pct, day_pnl, exposure, position_count)
                   VALUES (:node_id, :ts, :equity, :pnl, :pnl_pct, :day_pnl, :exposure, :position_count)""",
                sample,
            )

    def samples(self, node_id: str, limit: int = 240) -> list[dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM equity_samples WHERE node_id = ? ORDER BY ts DESC LIMIT ?",
                (node_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]  # 时间正序返回

    # ---- alerts ---------------------------------------------------------
    def add_alert(self, alert: dict[str, Any]) -> int:
        with self.write() as conn:
            cur = conn.execute(
                """INSERT INTO alerts (node_id, ts, severity, rule, message, context_json, acknowledged)
                   VALUES (:node_id, :ts, :severity, :rule, :message, :context_json, 0)""",
                {**alert, "context_json": json.dumps(alert.get("context") or {}, ensure_ascii=False)},
            )
            return int(cur.lastrowid)

    def list_alerts(self, limit: int = 100, unack_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alerts"
        if unack_only:
            sql += " WHERE acknowledged = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        with self.read() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def ack_alert(self, alert_id: int) -> None:
        with self.write() as conn:
            conn.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
