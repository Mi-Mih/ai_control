from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ai_control.agents.base import AgentError
from ai_control.platform.base import ManagedProcess, PlatformAdapter


class JsonRpcProcess:
    def __init__(
        self,
        platform: PlatformAdapter,
        argv: tuple[str, ...],
        cwd: Path,
        *,
        jsonrpc_version: str | None = None,
    ) -> None:
        self.platform = platform
        self.argv = argv
        self.cwd = cwd
        self.jsonrpc_version = jsonrpc_version
        self.process: ManagedProcess | None = None
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._stderr: list[str] = []

    async def start(self) -> None:
        self.process = await self.platform.spawn(self.argv, cwd=self.cwd)
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    pending = self._pending.pop(message["id"], None)
                    if pending and not pending.done():
                        pending.set_result(message)
                elif "id" in message and "method" in message:
                    await self.requests.put(message)
                else:
                    await self.notifications.put(message)
        finally:
            error = AgentError("app-server closed: " + "".join(self._stderr[-8:]).strip())
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(error)

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            self._stderr.append(line.decode(errors="replace"))
            self._stderr = self._stderr[-100:]

    async def request(self, method: str, params: dict[str, Any], timeout_seconds: float = 30) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send(self._envelope({"id": request_id, "method": method, "params": params}))
        try:
            response = await asyncio.wait_for(future, timeout_seconds)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise AgentError(f"app-server request timed out: {method}") from None
        if "error" in response:
            raise AgentError(f"{method}: {response['error']}")
        return response.get("result", {})

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._send(self._envelope(payload))

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        await self._send(self._envelope({"id": request_id, "result": result}))

    def _envelope(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.jsonrpc_version:
            return {"jsonrpc": self.jsonrpc_version, **payload}
        return payload

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise AgentError("app-server is not running")
        self.process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await self.process.stdin.drain()

    async def close(self) -> None:
        if self.process:
            await self.platform.terminate_tree(self.process)
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
