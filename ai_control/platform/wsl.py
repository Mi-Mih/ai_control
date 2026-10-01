from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ai_control.platform.base import ManagedProcess, PlatformAdapter


class WslPlatform(PlatformAdapter):
    """Runs the agent entirely inside one configured WSL distribution."""

    def __init__(self, host: PlatformAdapter, distribution: str, host_cwd: Path) -> None:
        self.host = host
        self.distribution = distribution
        self.host_cwd = host_cwd
        self.host_cwd.mkdir(parents=True, exist_ok=True)

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> ManagedProcess:
        wrapped = self.wrap_wsl(argv, distribution=self.distribution, cwd=cwd.as_posix())
        return await self.host.spawn(wrapped, cwd=self.host_cwd, env=env)

    async def terminate_tree(self, process: ManagedProcess, grace_seconds: float = 5) -> None:
        await self.host.terminate_tree(process, grace_seconds)
