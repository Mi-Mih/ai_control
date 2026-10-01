from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_control.storage import Database


def arguments_hash(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class PermissionService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self,
        *,
        task_id: int,
        user_id: int,
        action: str,
        arguments: dict[str, Any],
        ttl_seconds: int = 300,
    ) -> str:
        permission_id = secrets.token_urlsafe(18)
        expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        await self.database.execute(
            "INSERT INTO permissions(id,task_id,user_id,action,arguments_hash,expires_at) VALUES(?,?,?,?,?,?)",
            (
                permission_id,
                task_id,
                user_id,
                action,
                arguments_hash(arguments),
                expires.isoformat(),
            ),
        )
        return permission_id

    async def consume(
        self,
        permission_id: str,
        *,
        task_id: int,
        user_id: int,
        action: str,
        arguments: dict[str, Any],
        decision: str,
    ) -> bool:
        if decision not in {"allow_once", "deny", "explain"}:
            return False
        row = await self.database.fetch_one("SELECT * FROM permissions WHERE id=?", (permission_id,))
        if not row or row["used_at"] is not None:
            return False
        expires = datetime.fromisoformat(row["expires_at"])
        valid = (
            expires > datetime.now(UTC)
            and row["task_id"] == task_id
            and row["user_id"] == user_id
            and row["action"] == action
            and row["arguments_hash"] == arguments_hash(arguments)
        )
        if not valid:
            return False
        changed = await self.database.execute(
            "UPDATE permissions SET decision=?,used_at=CURRENT_TIMESTAMP WHERE id=? AND used_at IS NULL",
            (decision, permission_id),
        )
        return changed == 1
