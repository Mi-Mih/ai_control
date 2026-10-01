from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path

from ai_control.platform.base import ManagedProcess, PlatformAdapter


class UnixPlatform(PlatformAdapter):
    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> ManagedProcess:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env) if env else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return ManagedProcess(process)

    async def terminate_tree(self, process: ManagedProcess, grace_seconds: float = 5) -> None:
        if process.returncode is not None or process.pid is None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), grace_seconds)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            await process.wait()
