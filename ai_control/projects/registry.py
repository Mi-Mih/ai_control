from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from ai_control.config.models import ProjectConfig
from ai_control.core.models import AgentKind, FileAccessMode, Project, Runtime
from ai_control.storage import Database


def project_id(name: str, path: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:30] or "project"
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:10]
    return f"{slug}-{digest}"


class ProjectRegistry:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def sync_config(self, projects: list[ProjectConfig]) -> None:
        for item in projects:
            identifier = item.id or project_id(item.name, item.path)
            await self.database.execute(
                "INSERT INTO projects(id,name,path,runtime,wsl_distribution,agents_json,file_access,"
                "approved_paths_json,worktrees_enabled) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,path=excluded.path,"
                "runtime=excluded.runtime,wsl_distribution=excluded.wsl_distribution,"
                "agents_json=excluded.agents_json,file_access=excluded.file_access,"
                "approved_paths_json=excluded.approved_paths_json,"
                "worktrees_enabled=excluded.worktrees_enabled",
                (
                    identifier,
                    item.name,
                    str(item.path),
                    item.runtime.value,
                    item.wsl_distribution,
                    json.dumps(sorted(agent.value for agent in item.agents)),
                    item.file_access.value,
                    json.dumps([str(path) for path in item.approved_paths]),
                    int(item.worktrees_enabled),
                ),
            )

    async def list(self) -> list[Project]:
        rows = await self.database.fetch_all("SELECT * FROM projects ORDER BY name COLLATE NOCASE")
        return [self._from_row(row) for row in rows]

    async def get(self, identifier: str) -> Project | None:
        row = await self.database.fetch_one("SELECT * FROM projects WHERE id=?", (identifier,))
        return self._from_row(row) if row else None

    async def validate(self, project: Project) -> list[str]:
        issues: list[str] = []
        if project.runtime != Runtime.WSL:
            if not project.path.is_dir():
                issues.append("directory does not exist")
            elif not await self._is_git_repository(project):
                issues.append("not a Git repository or worktree")
        wsl_path = PurePosixPath(str(project.path).replace("\\", "/"))
        if project.runtime == Runtime.WSL and not wsl_path.is_absolute():
            issues.append("WSL path must be absolute")
        elif project.runtime == Runtime.WSL and not await self._is_git_repository(project):
            issues.append("not a Git repository or worktree in the configured WSL distribution")
        if not project.agents:
            issues.append("no agents are allowed")
        return issues

    @staticmethod
    async def _is_git_repository(project: Project) -> bool:
        if project.runtime == Runtime.WSL:
            if not project.wsl_distribution:
                return False
            argv = (
                "wsl.exe",
                "--distribution",
                project.wsl_distribution,
                "--cd",
                project.path.as_posix(),
                "--exec",
                "git",
                "rev-parse",
                "--is-inside-work-tree",
            )
        else:
            argv = ("git", "-C", str(project.path), "rev-parse", "--is-inside-work-tree")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), 10)
            return process.returncode == 0 and stdout.strip() == b"true"
        except (OSError, TimeoutError):
            return False

    @staticmethod
    def _from_row(row: dict[str, object]) -> Project:
        return Project(
            id=str(row["id"]),
            name=str(row["name"]),
            path=Path(str(row["path"])),
            runtime=Runtime(str(row["runtime"])),
            wsl_distribution=str(row["wsl_distribution"]) if row["wsl_distribution"] else None,
            agents=tuple(AgentKind(item) for item in json.loads(str(row["agents_json"]))),
            file_access=FileAccessMode(str(row["file_access"])),
            approved_paths=tuple(Path(item) for item in json.loads(str(row["approved_paths_json"]))),
            worktrees_enabled=bool(row["worktrees_enabled"]),
        )
