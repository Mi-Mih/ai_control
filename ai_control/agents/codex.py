from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_control import __version__
from ai_control.agents.base import AgentAdapter, AgentError, event_text
from ai_control.agents.jsonrpc import JsonRpcProcess
from ai_control.core.models import (
    AgentEvent,
    AgentKind,
    AgentSession,
    ApprovalMode,
    CapabilityReport,
    FileAccessMode,
)
from ai_control.platform import PlatformAdapter


class CodexAdapter(AgentAdapter):
    def __init__(
        self,
        platform: PlatformAdapter,
        executable: str = "codex",
        *,
        turn_timeout: float = 3600,
        model: str | None = None,
        file_access: FileAccessMode = FileAccessMode.PROJECT_ONLY,
        approval_mode: ApprovalMode = ApprovalMode.MANUAL,
        approved_paths: tuple[Path, ...] = (),
    ) -> None:
        self.platform = platform
        self.executable = executable
        self.turn_timeout = turn_timeout
        self.model = model
        self.file_access = file_access
        self.approval_mode = approval_mode
        self.approved_paths = approved_paths
        self.rpc: JsonRpcProcess | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self._approval_requests: dict[str, int] = {}

    async def capabilities(self) -> CapabilityReport:
        executable = shutil.which(self.executable)
        if not executable:
            return CapabilityReport(False, None, "codex executable not found")
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            version = stdout.decode(errors="replace").strip()
            if process.returncode:
                return CapabilityReport(False, None, stderr.decode(errors="replace").strip())
            help_process = await asyncio.create_subprocess_exec(
                executable,
                "app-server",
                "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            help_stdout, _ = await asyncio.wait_for(help_process.communicate(), 10)
            if help_process.returncode or b"--listen" not in help_stdout:
                return CapabilityReport(False, version, "Codex app-server is not supported; update Codex")
            return CapabilityReport(
                True,
                version,
                "structured app-server protocol",
                frozenset({"streaming", "resume", "interrupt", "approvals"}),
            )
        except (OSError, TimeoutError) as exc:
            return CapabilityReport(False, None, str(exc))

    async def account_rate_limits(self, cwd: Path) -> dict[str, Any]:
        rpc = JsonRpcProcess(self.platform, (self.executable, "app-server", "--listen", "stdio://"), cwd)
        try:
            await rpc.start()
            await rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "ai-control", "title": "AI Control", "version": __version__},
                    "capabilities": {"experimentalApi": False},
                },
            )
            await rpc.notify("initialized")
            return await rpc.request(
                "account/rateLimits/read",
                {"excludeResetCreditDetails": True},
                timeout_seconds=30,
            )
        finally:
            await rpc.close()

    async def list_sessions(self, *, cwd: Path, limit: int = 20) -> list[AgentSession]:
        rpc = JsonRpcProcess(self.platform, (self.executable, "app-server", "--listen", "stdio://"), cwd)
        try:
            await rpc.start()
            await rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "ai-control", "title": "AI Control", "version": __version__},
                    "capabilities": {"experimentalApi": False},
                },
            )
            await rpc.notify("initialized")
            result = await rpc.request(
                "thread/list",
                {"limit": min(max(limit, 1), 100), "cwd": str(cwd), "archived": False},
                timeout_seconds=30,
            )
            sessions: list[AgentSession] = []
            for item in result.get("data", []):
                if not isinstance(item, dict):
                    continue
                identifier = item.get("id") or item.get("threadId")
                item_cwd = item.get("cwd")
                if not identifier or not isinstance(item_cwd, str) or not _within(Path(item_cwd), cwd):
                    continue
                status = item.get("status")
                status_type = status.get("type") if isinstance(status, dict) else status
                updated = item.get("updatedAt")
                updated_at = (
                    datetime.fromtimestamp(float(updated), UTC)
                    if isinstance(updated, (int, float)) and not isinstance(updated, bool)
                    else None
                )
                title = str(item.get("name") or item.get("preview") or "Сессия Codex")
                sessions.append(
                    AgentSession(
                        id=str(identifier),
                        agent=AgentKind.CODEX,
                        title=" ".join(title.split())[:80],
                        cwd=Path(item_cwd),
                        updated_at=updated_at,
                        active=status_type == "active",
                    )
                )
            return sessions
        finally:
            await rpc.close()

    async def _connect(self, cwd: Path, session_id: str | None) -> str:
        self.rpc = JsonRpcProcess(self.platform, (self.executable, "app-server", "--listen", "stdio://"), cwd)
        await self.rpc.start()
        await self.rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "ai-control", "title": "AI Control", "version": __version__},
                "capabilities": {"experimentalApi": False},
            },
        )
        await self.rpc.notify("initialized")
        if self.file_access == FileAccessMode.READ_ONLY:
            sandbox = "read-only"
        elif self.file_access == FileAccessMode.FULL_ACCESS:
            sandbox = "danger-full-access"
        else:
            sandbox = "workspace-write"
        extra_config: dict[str, Any] | None = None
        if self.file_access == FileAccessMode.APPROVED_PATHS and self.approved_paths:
            extra_config = {"sandbox_workspace_write": {"writable_roots": [str(path) for path in self.approved_paths]}}
        approval_policy = (
            "on-request"
            if self.file_access == FileAccessMode.FULL_ACCESS or self.approval_mode == ApprovalMode.AUTO
            else "never"
        )
        approvals_reviewer = "auto_review" if self.approval_mode == ApprovalMode.AUTO else "user"
        if session_id:
            params: dict[str, Any] = {
                "threadId": session_id,
                "cwd": str(cwd),
                "approvalPolicy": approval_policy,
                "approvalsReviewer": approvals_reviewer,
                "sandbox": sandbox,
                "config": extra_config,
            }
            if self.model:
                params["model"] = self.model
            result = await self.rpc.request(
                "thread/resume",
                params,
            )
        else:
            params = {
                "cwd": str(cwd),
                "approvalPolicy": approval_policy,
                "approvalsReviewer": approvals_reviewer,
                "sandbox": sandbox,
                "config": extra_config,
                "ephemeral": False,
            }
            if self.model:
                params["model"] = self.model
            result = await self.rpc.request(
                "thread/start",
                params,
            )
        thread = result.get("thread", result)
        identifier = thread.get("id") or thread.get("threadId")
        if not identifier:
            raise AgentError("Codex did not return a thread ID")
        self.thread_id = str(identifier)
        return self.thread_id

    async def run_turn(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        attachments: tuple[Path, ...] = (),
    ) -> AsyncIterator[AgentEvent]:
        thread_id = await self._connect(cwd, session_id)
        yield AgentEvent("session", session_id=thread_id)
        inputs: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for path in attachments:
            if path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                inputs.append({"type": "localImage", "path": str(path)})
            else:
                inputs[0]["text"] += f"\nAttached local file: {path}"
        assert self.rpc
        turn_params: dict[str, Any] = {"threadId": thread_id, "input": inputs}
        if self.model:
            turn_params["model"] = self.model
        result = await self.rpc.request("turn/start", turn_params, timeout_seconds=30)
        turn = result.get("turn", result)
        self.turn_id = str(turn.get("id") or turn.get("turnId") or "")
        yield AgentEvent("status", "Codex turn started", payload={"turn_id": self.turn_id})

        request_task = asyncio.create_task(self.rpc.requests.get())
        notification_task = asyncio.create_task(self.rpc.notifications.get())
        try:
            async with asyncio.timeout(self.turn_timeout):
                while True:
                    done, _ = await asyncio.wait({request_task, notification_task}, return_when=asyncio.FIRST_COMPLETED)
                    if request_task in done:
                        request = request_task.result()
                        request_id = str(request["id"])
                        if self.file_access == FileAccessMode.FULL_ACCESS:
                            self._approval_requests[request_id] = int(request["id"])
                            yield AgentEvent(
                                "approval",
                                text=request.get("method", "approval requested"),
                                payload={"request_id": request_id, **request.get("params", {})},
                            )
                        else:
                            await self.rpc.respond(int(request["id"]), {"decision": "decline"})
                            yield AgentEvent(
                                "policy_denied",
                                "Запрос Codex на повышение прав автоматически отклонён политикой задачи.",
                            )
                        request_task = asyncio.create_task(self.rpc.requests.get())
                    if notification_task in done:
                        note = notification_task.result()
                        method = note.get("method", "")
                        params = note.get("params", {})
                        if method == "item/agentMessage/delta":
                            yield AgentEvent("text_delta", event_text(params), payload=params)
                        elif method in {"turn/completed", "turn/failed"}:
                            yield AgentEvent("completed", payload=params)
                            return
                        elif method in {"error", "turn/error"}:
                            yield AgentEvent("error", event_text(params), payload=params)
                        elif method.endswith("/delta"):
                            text = event_text(params)
                            if text:
                                yield AgentEvent("progress", text, payload=params)
                        notification_task = asyncio.create_task(self.rpc.notifications.get())
        except TimeoutError:
            await self.stop()
            raise AgentError("Codex turn timed out") from None
        finally:
            request_task.cancel()
            notification_task.cancel()

    async def answer_approval(self, request_id: str, decision: str) -> None:
        if not self.rpc or request_id not in self._approval_requests:
            raise AgentError("unknown or expired approval request")
        rpc_id = self._approval_requests.pop(request_id)
        if decision == "accept" and self.file_access != FileAccessMode.FULL_ACCESS:
            await self.rpc.respond(rpc_id, {"decision": "decline"})
            raise AgentError("approval would exceed the task access policy")
        if decision == "explain":
            decision = "decline"
        if decision not in {"accept", "decline", "cancel"}:
            raise AgentError("invalid approval decision")
        await self.rpc.respond(rpc_id, {"decision": decision})

    async def stop(self) -> None:
        if self.rpc and self.thread_id and self.turn_id:
            try:
                await self.rpc.request(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self.turn_id},
                    timeout_seconds=5,
                )
            except AgentError:
                pass
        if self.rpc:
            await self.rpc.close()
            self.rpc = None


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False
