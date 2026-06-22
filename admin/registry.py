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

    # ---- 账户级登记 -----------------------------------------------------
    def register_accounts(self, payload: dict[str, Any], *, source: str = "app") -> dict[str, Any]:
        """账户登记。报文形如 {"node": {...}, "account": {...}} 或 {"node": {...}, "accounts": [...]}.

        node 段先 upsert(复用节点登记,拿到稳定 node_id 作传输层);account 段写账户注册表。
        幂等:主键 (node_id, account_id)。单条 / 批量(register-all)共用本入口。
        """
        node_payload = payload.get("node")
        if not isinstance(node_payload, dict):
            raise ValueError("缺少 node 段")
        node = self.register(node_payload, source=source)  # upsert 节点(已校验 base_url/id)
        node_id = node["id"]

        rows = payload.get("accounts")
        if rows is None:
            single = payload.get("account")
            if not isinstance(single, dict):
                raise ValueError("缺少 account 或 accounts 段")
            rows = [single]
        if not isinstance(rows, list) or not rows:
            raise ValueError("accounts 必须是非空数组")

        registered: list[dict[str, Any]] = []
        for acct in rows:
            registered.append(self._upsert_account(node_id, acct))
        return {"node_id": node_id, "accounts": registered}

    def _upsert_account(self, node_id: str, acct: dict[str, Any]) -> dict[str, Any]:
        account_id = str(acct.get("id") or "").strip()
        if not account_id:
            raise ValueError("account.id 必填")
        existing = self.db.get_account(node_id, account_id)
        row = {
            "node_id": node_id,
            "account_id": account_id,
            "owner": acct.get("owner") or acct.get("name") or None,  # 缺 owner 回退账户名
            "name": acct.get("name") or None,
            "currency": acct.get("currency") or None,
            "market": acct.get("market") or None,
            "initial_cash": acct.get("initial_cash"),
            "registered_at": (existing or {}).get("registered_at") or _now(),
            "updated_at": _now(),
        }
        self.db.upsert_account(row)
        row["_action"] = "updated" if existing else "created"
        return row

    def deregister_account(self, node_id: str, account_id: str) -> bool:
        return self.db.delete_account(node_id, account_id)

    def accounts(self, node_id: str | None = None) -> list[dict[str, Any]]:
        return self.db.list_accounts(node_id)
