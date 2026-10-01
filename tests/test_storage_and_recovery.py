import asyncio
from pathlib import Path

import pytest

from ai_control.config.models import AppConfig
from ai_control.core.models import AgentKind, ApprovalMode, FileAccessMode
from ai_control.git import GitService
from ai_control.platform import current_platform
from ai_control.projects import ProjectRegistry
from ai_control.sessions import TaskManager
from ai_control.sessions.manager import TaskManagerError
from ai_control.storage import Database


def test_schema_and_recovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = Database(tmp_path / "state.db")
        await database.initialize()
        await database.initialize()
        migration = await database.fetch_one("SELECT MAX(version) AS version FROM schema_migrations")
        assert migration == {"version": 5}
        await database.execute(
            "INSERT INTO projects(id,name,path,runtime,agents_json,file_access) VALUES(?,?,?,?,?,?)",
            ("p", "Project", str(tmp_path), "linux", '["codex"]', "project_only"),
        )
        task_id = await database.execute(
            "INSERT INTO tasks(project_id,user_id,agent,status,checkout_path,prompt) VALUES(?,?,?,?,?,?)",
            ("p", 42, "codex", "running", str(tmp_path), "test"),
        )
        config = AppConfig.model_validate(
            {
                "instance": {"name": "Test", "data_dir": tmp_path},
                "telegram": {"allowed_user_ids": [42]},
            }
        )
        manager = TaskManager(config, database, ProjectRegistry(database), current_platform(), GitService())
        assert await manager.recover() == 1
        record = await manager.get(task_id)
        assert record and record.status.value == "lost"
        assert record.model is None
        assert record.error == "Process lost during service restart"

        cursor_task_id = await database.execute(
            "INSERT INTO tasks(project_id,user_id,agent,status,checkout_path,prompt) VALUES(?,?,?,?,?,?)",
            ("p", 42, "cursor", "completed", str(tmp_path), "cursor test"),
        )
        cursor_record = await manager.get(cursor_task_id)
        assert cursor_record and cursor_record.agent.value == "cursor"

    asyncio.run(scenario())


def test_emergency_shutdown_cancels_tasks_and_rejects_new_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = Database(tmp_path / "state.db")
        await database.initialize()
        await database.execute(
            "INSERT INTO projects(id,name,path,runtime,agents_json,file_access) VALUES(?,?,?,?,?,?)",
            ("p", "Project", str(tmp_path), "linux", '["codex"]', "project_only"),
        )
        task_id = await database.execute(
            "INSERT INTO tasks(project_id,user_id,agent,status,checkout_path,prompt) VALUES(?,?,?,?,?,?)",
            ("p", 42, "codex", "running", str(tmp_path), "test"),
        )
        config = AppConfig.model_validate(
            {
                "instance": {"name": "Test", "data_dir": tmp_path},
                "telegram": {"allowed_user_ids": [42]},
            }
        )
        manager = TaskManager(config, database, ProjectRegistry(database), current_platform(), GitService())
        cancelled = asyncio.Event()

        async def running_task() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        runner = asyncio.create_task(running_task())
        await asyncio.sleep(0)
        manager._runners[task_id] = runner

        await manager.shutdown()

        assert cancelled.is_set()
        assert runner.cancelled()
        record = await manager.get(task_id)
        assert record and record.status.value == "stopped"
        with pytest.raises(TaskManagerError, match="shutting down"):
            await manager.create(
                project_id="p",
                user_id=42,
                agent=AgentKind.CODEX,
                prompt="too late",
            )

    asyncio.run(scenario())


def test_task_access_is_bounded_by_local_project_policy(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = Database(tmp_path / "state.db")
        await database.initialize()
        approved = tmp_path / "approved"
        approved.mkdir()
        await database.execute(
            "INSERT INTO projects("
            "id,name,path,runtime,agents_json,file_access,approved_paths_json"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                "p",
                "Project",
                str(tmp_path),
                "linux",
                '["codex"]',
                "approved_paths",
                f'["{approved}"]',
            ),
        )
        task_id = await database.execute(
            "INSERT INTO tasks("
            "project_id,user_id,agent,status,checkout_path,prompt,access_mode"
            ") VALUES(?,?,?,?,?,?,?)",
            ("p", 42, "codex", "completed", str(tmp_path), "test", "project_only"),
        )
        config = AppConfig.model_validate(
            {
                "instance": {"name": "Test", "data_dir": tmp_path},
                "telegram": {"allowed_user_ids": [42]},
            }
        )
        manager = TaskManager(config, database, ProjectRegistry(database), current_platform(), GitService())

        assert await manager.available_access_modes(task_id, 42) == (
            FileAccessMode.READ_ONLY,
            FileAccessMode.PROJECT_ONLY,
            FileAccessMode.APPROVED_PATHS,
        )
        record = await manager.change_access_mode(task_id, 42, FileAccessMode.READ_ONLY)
        assert record.access_mode == FileAccessMode.READ_ONLY
        with pytest.raises(TaskManagerError, match="exceeds the local project policy"):
            await manager.change_access_mode(task_id, 42, FileAccessMode.FULL_ACCESS)
        audit = await database.fetch_one(
            "SELECT event,detail_json FROM audit_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        assert audit and audit["event"] == "task_access_changed"
        assert '"to": "read_only"' in str(audit["detail_json"])

    asyncio.run(scenario())


def test_task_approval_mode_changes_only_between_runs_and_is_audited(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = Database(tmp_path / "state.db")
        await database.initialize()
        await database.execute(
            "INSERT INTO projects(id,name,path,runtime,agents_json,file_access) VALUES(?,?,?,?,?,?)",
            ("p", "Project", str(tmp_path), "linux", '["codex"]', "project_only"),
        )
        task_id = await database.execute(
            "INSERT INTO tasks(project_id,user_id,agent,status,checkout_path,prompt) VALUES(?,?,?,?,?,?)",
            ("p", 42, "codex", "completed", str(tmp_path), "test"),
        )
        config = AppConfig.model_validate(
            {
                "instance": {"name": "Test", "data_dir": tmp_path},
                "telegram": {"allowed_user_ids": [42]},
            }
        )
        manager = TaskManager(config, database, ProjectRegistry(database), current_platform(), GitService())

        record = await manager.change_approval_mode(task_id, 42, ApprovalMode.AUTO)
        assert record.approval_mode == ApprovalMode.AUTO
        audit = await database.fetch_one(
            "SELECT event,detail_json FROM audit_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        assert audit and audit["event"] == "task_approval_mode_changed"
        assert '"to": "auto"' in str(audit["detail_json"])

        await database.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        with pytest.raises(TaskManagerError, match="task is running"):
            await manager.change_approval_mode(task_id, 42, ApprovalMode.MANUAL)

    asyncio.run(scenario())
