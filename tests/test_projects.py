import asyncio
from pathlib import Path

from ai_control.core.models import AgentKind, Project, Runtime
from ai_control.projects import ProjectRegistry
from ai_control.storage import Database


def test_fake_git_directory_is_not_accepted(tmp_path: Path) -> None:
    async def scenario() -> None:
        (tmp_path / ".git").mkdir()
        registry = ProjectRegistry(Database(tmp_path / "state.db"))
        project = Project(
            id="p",
            name="Project",
            path=tmp_path,
            runtime=Runtime.LINUX,
            agents=(AgentKind.CODEX,),
        )
        assert "not a Git repository or worktree" in await registry.validate(project)

    asyncio.run(scenario())
