import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ai_control.agents.base import AgentAdapter
from ai_control.config.models import AppConfig
from ai_control.core.models import (
    AgentEvent,
    AgentKind,
    AgentSession,
    CapabilityReport,
    FileAccessMode,
    Project,
)
from ai_control.git import GitService
from ai_control.platform import current_platform
from ai_control.projects import ProjectRegistry
from ai_control.sessions import TaskManager
from ai_control.sessions.manager import TaskManagerError
from ai_control.storage import Database


class SessionAdapter(AgentAdapter):
    def __init__(self, sessions: list[AgentSession]) -> None:
        self.sessions = sessions

    async def capabilities(self) -> CapabilityReport:
        return CapabilityReport(True, "test", "test")

    async def list_sessions(self, *, cwd: Path, limit: int = 20) -> list[AgentSession]:
        return self.sessions[:limit]

    async def run_turn(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        attachments: tuple[Path, ...] = (),
    ) -> AsyncIterator[AgentEvent]:
        if False:
            yield AgentEvent("completed")

    async def stop(self) -> None:
        return None


class SessionTaskManager(TaskManager):
    def __init__(self, *args, sessions: list[AgentSession], **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.sessions = sessions

    def _adapter(
        self,
        kind: AgentKind,
        project: Project,
        model: str | None = None,
        access_mode: FileAccessMode = FileAccessMode.PROJECT_ONLY,
    ) -> AgentAdapter:
        return SessionAdapter(self.sessions)


class SessionProjectRegistry(ProjectRegistry):
    async def validate(self, project: Project) -> list[str]:
        return []


def test_imports_only_inactive_sessions_inside_project(tmp_path: Path) -> None:
    async def scenario() -> None:
        project_path = tmp_path / "project"
        project_path.mkdir()
        config = AppConfig.model_validate(
            {
                "instance": {"name": "Test", "data_dir": tmp_path / "data"},
                "telegram": {"allowed_user_ids": [42]},
                "projects": [
                    {
                        "id": "project",
                        "name": "Project",
                        "path": project_path,
                        "runtime": "linux",
                        "agents": ["codex"],
                    }
                ],
            }
        )
        database = Database(tmp_path / "state.db")
        await database.initialize()
        registry = SessionProjectRegistry(database)
        await registry.sync_config(config.projects)
        sessions = [
            AgentSession("session-ok", AgentKind.CODEX, "Existing task", project_path),
            AgentSession("session-active", AgentKind.CODEX, "Busy task", project_path, active=True),
            AgentSession("session-outside", AgentKind.CODEX, "Other project", tmp_path / "other"),
        ]
        manager = SessionTaskManager(
            config,
            database,
            registry,
            current_platform(),
            GitService(),
            sessions=sessions,
        )

        found = await manager.list_importable_sessions(project_id="project", user_id=42, agent=AgentKind.CODEX)
        assert [item.id for item in found] == ["session-ok", "session-active"]

        record = await manager.import_session(
            project_id="project",
            user_id=42,
            agent=AgentKind.CODEX,
            session_id="session-ok",
        )
        assert record.agent_session_id == "session-ok"
        assert record.status.value == "completed"
        assert record.model is None

        found_again = await manager.list_importable_sessions(
            project_id="project",
            user_id=42,
            agent=AgentKind.CODEX,
        )
        assert found_again[0].imported_task_id == record.id

        duplicate = await manager.import_session(
            project_id="project",
            user_id=42,
            agent=AgentKind.CODEX,
            session_id="session-ok",
        )
        assert duplicate.id == record.id

        with pytest.raises(TaskManagerError, match="session is active"):
            await manager.import_session(
                project_id="project",
                user_id=42,
                agent=AgentKind.CODEX,
                session_id="session-active",
            )
        with pytest.raises(TaskManagerError, match="session is unavailable"):
            await manager.import_session(
                project_id="project",
                user_id=42,
                agent=AgentKind.CODEX,
                session_id="session-outside",
            )

    asyncio.run(scenario())
