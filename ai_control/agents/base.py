from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ai_control.core.models import AgentEvent, AgentSession, CapabilityReport


class AgentError(RuntimeError):
    pass


class AgentAdapter(ABC):
    @abstractmethod
    async def capabilities(self) -> CapabilityReport: ...

    @abstractmethod
    async def run_turn(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        attachments: tuple[Path, ...] = (),
    ) -> AsyncIterator[AgentEvent]: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def list_sessions(self, *, cwd: Path, limit: int = 20) -> list[AgentSession]:
        return []

    async def answer_approval(self, request_id: str, decision: str) -> None:
        raise AgentError("adapter does not support live approvals")


def event_text(payload: dict[str, Any]) -> str:
    for key in ("delta", "text", "content", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""
