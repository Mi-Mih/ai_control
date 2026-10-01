from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        path TEXT NOT NULL UNIQUE,
        runtime TEXT NOT NULL CHECK(runtime IN ('windows','linux','wsl')),
        wsl_distribution TEXT,
        agents_json TEXT NOT NULL,
        file_access TEXT NOT NULL,
        approved_paths_json TEXT NOT NULL DEFAULT '[]',
        worktrees_enabled INTEGER NOT NULL DEFAULT 1,
        last_used_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL REFERENCES projects(id),
        user_id INTEGER NOT NULL,
        agent TEXT NOT NULL CHECK(agent IN ('claude','codex')),
        status TEXT NOT NULL,
        checkout_path TEXT NOT NULL,
        prompt TEXT NOT NULL,
        agent_session_id TEXT,
        pid INTEGER,
        worktree_id INTEGER,
        error TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
    CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        role TEXT NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS permissions (
        id TEXT PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        user_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        arguments_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decision TEXT,
        used_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS worktrees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL REFERENCES projects(id),
        path TEXT NOT NULL UNIQUE,
        branch TEXT NOT NULL,
        task_id INTEGER,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        path TEXT NOT NULL,
        direction TEXT NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        task_id INTEGER,
        event TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    ALTER TABLE tasks ADD COLUMN model TEXT;
    """,
    """
    PRAGMA foreign_keys=OFF;
    BEGIN IMMEDIATE;
    CREATE TABLE tasks_v3 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL REFERENCES projects(id),
        user_id INTEGER NOT NULL,
        agent TEXT NOT NULL CHECK(agent IN ('claude','codex','cursor')),
        status TEXT NOT NULL,
        checkout_path TEXT NOT NULL,
        prompt TEXT NOT NULL,
        agent_session_id TEXT,
        pid INTEGER,
        worktree_id INTEGER,
        error TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        model TEXT
    );
    INSERT INTO tasks_v3(
        id,project_id,user_id,agent,status,checkout_path,prompt,agent_session_id,
        pid,worktree_id,error,created_at,updated_at,model
    )
    SELECT
        id,project_id,user_id,agent,status,checkout_path,prompt,agent_session_id,
        pid,worktree_id,error,created_at,updated_at,model
    FROM tasks;
    DROP TABLE tasks;
    ALTER TABLE tasks_v3 RENAME TO tasks;
    CREATE INDEX idx_tasks_status ON tasks(status);
    CREATE INDEX idx_tasks_project ON tasks(project_id);
    COMMIT;
    PRAGMA foreign_keys=ON;
    """,
    """
    ALTER TABLE tasks ADD COLUMN access_mode TEXT NOT NULL DEFAULT 'project_only';
    """,
    """
    ALTER TABLE tasks ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'manual';
    """,
]


class Database:
    """Small async facade; each operation uses a short-lived SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def migrate() -> None:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
                for version, migration in enumerate(MIGRATIONS, start=1):
                    if version not in applied:
                        connection.executescript(migration)
                        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))

        async with self._write_lock:
            # Migrations run only at startup. Keeping SQLite calls on the event-loop
            # thread also avoids platform-specific executor shutdown issues in frozen apps.
            migrate()

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        def run() -> int:
            with self._connect() as connection:
                cursor = connection.execute(sql, tuple(params))
                return int(cursor.lastrowid or cursor.rowcount)

        async with self._write_lock:
            return run()

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        def run() -> dict[str, Any] | None:
            with self._connect() as connection:
                row = connection.execute(sql, tuple(params)).fetchone()
                return dict(row) if row else None

        return run()

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            with self._connect() as connection:
                return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]

        return run()

    async def audit(
        self,
        event: str,
        *,
        user_id: int | None = None,
        task_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await self.execute(
            "INSERT INTO audit_events(user_id,task_id,event,detail_json) VALUES(?,?,?,?)",
            (user_id, task_id, event, json.dumps(detail or {}, ensure_ascii=False)),
        )

    async def health(self) -> tuple[bool, str]:
        try:
            row = await self.fetch_one("PRAGMA integrity_check")
            value = next(iter(row.values())) if row else "no result"
            return value == "ok", str(value)
        except sqlite3.Error as exc:
            return False, str(exc)
