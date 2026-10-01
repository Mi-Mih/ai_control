from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path


class ManagedProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    @property
    def pid(self) -> int | None:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    @property
    def stdin(self) -> asyncio.StreamWriter | None:
        return self.process.stdin

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        return self.process.stdout

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        return self.process.stderr

    async def wait(self) -> int:
        return await self.process.wait()


class PlatformAdapter(ABC):
    @abstractmethod
    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> ManagedProcess: ...

    @abstractmethod
    async def terminate_tree(self, process: ManagedProcess, grace_seconds: float = 5) -> None: ...

    def wrap_wsl(self, argv: Sequence[str], *, distribution: str, cwd: str) -> tuple[str, ...]:
        return (
            "wsl.exe",
            "--distribution",
            distribution,
            "--cd",
            cwd,
            "--exec",
            *argv,
        )
