from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class Runtime(StrEnum):
    WINDOWS = "windows"
    LINUX = "linux"
    WSL = "wsl"


class AgentKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    CURSOR = "cursor"


class FileAccessMode(StrEnum):
    READ_ONLY = "read_only"
    PROJECT_ONLY = "project_only"
    APPROVED_PATHS = "approved_paths"
    FULL_ACCESS = "full_access"


class ApprovalMode(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"
    LOST = "lost"
    CLOSED = "closed"


FINAL_TASK_STATUSES = {
    TaskStatus.STOPPED,
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CLOSED,
}


@dataclass(slots=True)
class Project:
    id: str
    name: str
    path: Path
    runtime: Runtime
    agents: tuple[AgentKind, ...]
    file_access: FileAccessMode = FileAccessMode.PROJECT_ONLY
    approved_paths: tuple[Path, ...] = ()
    wsl_distribution: str | None = None
    worktrees_enabled: bool = True
    last_used_at: datetime | None = None


@dataclass(slots=True)
class TaskRecord:
    id: int
    project_id: str
    user_id: int
    agent: AgentKind
    status: TaskStatus
    checkout_path: Path
    prompt: str
    access_mode: FileAccessMode = FileAccessMode.PROJECT_ONLY
    approval_mode: ApprovalMode = ApprovalMode.MANUAL
    model: str | None = None
    agent_session_id: str | None = None
    pid: int | None = None
    worktree_id: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    error: str | None = None


@dataclass(slots=True)
class AgentSession:
    id: str
    agent: AgentKind
    title: str
    cwd: Path
    updated_at: datetime | None = None
    active: bool = False
    imported_task_id: int | None = None


@dataclass(slots=True)
class AgentEvent:
    kind: str
    text: str = ""
    session_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CapabilityReport:
    available: bool
    version: str | None
    detail: str
    features: frozenset[str] = frozenset()
