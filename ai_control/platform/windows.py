from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from ai_control.platform.base import ManagedProcess, PlatformAdapter


class WindowsPlatform(PlatformAdapter):
    """Process groups are the dependency-free fallback; PyInstaller bundles can add Job Objects."""

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> ManagedProcess:
        # CreateProcess only appends ".exe", so npm shims like "claude.cmd" must be resolved via PATHEXT.
        executable = shutil.which(argv[0]) or argv[0]
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            cwd=cwd,
            env=dict(env) if env else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return ManagedProcess(process)

    async def terminate_tree(self, process: ManagedProcess, grace_seconds: float = 5) -> None:
        if process.returncode is not None or process.pid is None:
            return
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            await asyncio.wait_for(killer.wait(), grace_seconds)
            await asyncio.wait_for(process.wait(), grace_seconds)
        except TimeoutError:
            force = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F")
            await force.wait()
            await process.wait()
