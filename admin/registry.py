"""节点注册表:手动增删 + 节点自注册的统一落点。薄封装,校验后写 DB。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .db import Database

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_URL_RE = re.compile(r"^https?://[^\s/]+(:\d+)?(/.*)?$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    """把任意显示名压成安全 id。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return cleaned[:64] or f"node-{int(datetime.now().timestamp())}"


class Registry:
    def __init__(self, db: Database) -> None:
        self.db = db

    def register(self, payload: dict[str, Any], *, source: str = "manual") -> dict[str, Any]:
        """新增/更新一个节点。manual(手填) 与 self(自注册) 共用,幂等。"""
        base_url = str(payload.get("base_url") or "").strip()
        if not _URL_RE.match(base_url):
            raise ValueError("base_url 非法,需形如 http://host:port")

        node_id = str(payload.get("id") or "").strip()
        if not node_id:
            node_id = _slug(str(payload.get("name") or base_url))
        if not _ID_RE.match(node_id):
            raise ValueError("id 只能含字母数字 . _ - ,且 ≤64 字符")

        existing = self.db.get_node(node_id)
        api_version = payload.get("api_version")
        node = {
            "id": node_id,
            "name": payload.get("name") or (existing or {}).get("name") or node_id,
            "base_url": base_url.rstrip("/"),
            "token": payload.get("token") or None,
            "data_source": payload.get("data_source") or None,
            "api_version": int(api_version) if api_version is not None else None,
            "enabled": 1,
            "created_at": (existing or {}).get("created_at") or _now(),
            "updated_at": _now(),
        }
        self.db.upsert_node(node)
        node["_action"] = "updated" if existing else "created"
        node["_source"] = source
        return node

    def list(self) -> list[dict[str, Any]]:
        return self.db.list_nodes()

    def enabled(self) -> list[dict[str, Any]]:
        return self.db.list_nodes(enabled_only=True)

    def get(self, node_id: str) -> dict[str, Any] | None:
        return self.db.get_node(node_id)

    def delete(self, node_id: str) -> bool:
        if not self.db.get_node(node_id):
            return False
        self.db.delete_node(node_id)
        return True
