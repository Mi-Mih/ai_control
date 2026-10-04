from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_control import __version__
from ai_control.agents.base import AgentAdapter, AgentError
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


class CursorAdapter(AgentAdapter):
    def __init__(
        self,
        platform: PlatformAdapter,
        executable: str = "agent",
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
        self.session_id: str | None = None
        self._approval_requests: dict[str, dict[str, Any]] = {}

    async def capabilities(self) -> CapabilityReport:
        executable = shutil.which(self.executable)
        if not executable:
            return CapabilityReport(False, None, "Cursor agent executable not found")
        try:
            version_process = await asyncio.create_subprocess_exec(
                executable,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(version_process.communicate(), 10)
            version = stdout.decode(errors="replace").strip()
            if version_process.returncode:
                return CapabilityReport(False, None, stderr.decode(errors="replace").strip())
            help_process = await asyncio.create_subprocess_exec(
                executable,
                "help",
                "acp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            help_stdout, _ = await asyncio.wait_for(help_process.communicate(), 10)
            if help_process.returncode or b"Agent Client Protocol" not in help_stdout:
                return CapabilityReport(False, version, "Cursor ACP mode is unavailable; update Cursor CLI")
            return CapabilityReport(
                True,
                version,
                "structured ACP protocol",
                frozenset({"streaming", "resume", "interrupt", "approvals"}),
            )
        except (OSError, TimeoutError) as exc:
            return CapabilityReport(False, None, str(exc))

    def _argv(self) -> tuple[str, ...]:
        argv = [self.executable]
        if self.model:
            argv.extend(("--model", self.model))
        if self.file_access == FileAccessMode.READ_ONLY:
            argv.extend(("--mode", "plan"))
        if self.approval_mode == ApprovalMode.AUTO:
            argv.append("--auto-review")
        argv.extend(("--sandbox", "disabled" if self.file_access == FileAccessMode.FULL_ACCESS else "enabled"))
        if self.file_access == FileAccessMode.APPROVED_PATHS:
            for path in self.approved_paths:
                argv.extend(("--add-dir", str(path)))
        argv.append("acp")
        return tuple(argv)

    async def list_sessions(self, *, cwd: Path, limit: int = 20) -> list[AgentSession]:
        rpc = JsonRpcProcess(self.platform, self._argv(), cwd, jsonrpc_version="2.0")
        try:
            await rpc.start()
            await rpc.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "ai-control", "version": __version__},
                },
            )
            await rpc.request("authenticate", {"methodId": "cursor_login"})
            result = await rpc.request("session/list", {"cwd": str(cwd)}, timeout_seconds=30)
            sessions: list[AgentSession] = []
            for item in result.get("sessions", [])[: min(max(limit, 1), 100)]:
                if not isinstance(item, dict):
                    continue
                identifier = item.get("sessionId") or item.get("id")
                item_cwd = item.get("cwd") or str(cwd)
                if not identifier or not isinstance(item_cwd, str) or not _within(Path(item_cwd), cwd):
                    continue
                raw_updated = item.get("updatedAt")
                try:
                    updated_at = datetime.fromisoformat(str(raw_updated).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    updated_at = None
                title = str(item.get("title") or item.get("name") or "Сессия Cursor")
                sessions.append(
                    AgentSession(
                        id=str(identifier),
                        agent=AgentKind.CURSOR,
                        title=" ".join(title.split())[:80],
                        cwd=Path(item_cwd),
                        updated_at=updated_at,
                    )
                )
            return sessions
        finally:
            await rpc.close()

    async def _connect(self, cwd: Path, session_id: str | None) -> str:
        self.rpc = JsonRpcProcess(
            self.platform,
            self._argv(),
            cwd,
            jsonrpc_version="2.0",
        )
        await self.rpc.start()
        await self.rpc.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "ai-control", "version": __version__},
            },
        )
        await self.rpc.request("authenticate", {"methodId": "cursor_login"})
        params: dict[str, Any] = {"cwd": str(cwd), "mcpServers": []}
        if session_id:
            params["sessionId"] = session_id
            result = await self.rpc.request("session/load", params)
            while not self.rpc.notifications.empty():
                self.rpc.notifications.get_nowait()
        else:
            result = await self.rpc.request("session/new", params)
        identifier = result.get("sessionId") or session_id
        if not identifier:
            raise AgentError("Cursor did not return a session ID")
        self.session_id = str(identifier)
        return self.session_id

    async def run_turn(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        attachments: tuple[Path, ...] = (),
    ) -> AsyncIterator[AgentEvent]:
        identifier = await self._connect(cwd, session_id)
        yield AgentEvent("session", session_id=identifier)
        if attachments:
            prompt += "\n\nFiles uploaded for this task:\n" + "\n".join(f"- {path}" for path in attachments)
        assert self.rpc
        prompt_task = asyncio.create_task(
            self.rpc.request(
                "session/prompt",
                {"sessionId": identifier, "prompt": [{"type": "text", "text": prompt}]},
                timeout_seconds=self.turn_timeout,
            )
        )
        request_task = asyncio.create_task(self.rpc.requests.get())
        notification_task = asyncio.create_task(self.rpc.notifications.get())
        response_chunks: list[str] = []
        try:
            while True:
                done, _ = await asyncio.wait(
                    {prompt_task, request_task, notification_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if request_task in done:
                    request = request_task.result()
                    async for event in self._handle_request(request):
                        yield event
                    request_task = asyncio.create_task(self.rpc.requests.get())
                if notification_task in done:
                    note = notification_task.result()
                    event = self._notification_event(note)
                    if event:
                        if event.kind == "text_delta":
                            response_chunks.append(event.text)
                        yield event
                    notification_task = asyncio.create_task(self.rpc.notifications.get())
                if prompt_task in done:
                    result = prompt_task.result()
                    # The prompt response can be resolved in the same event-loop
                    # iteration as the last session/update notifications. Drain
                    # those queued chunks before constructing the final answer.
                    if not notification_task.done():
                        notification_task.cancel()
                    while not self.rpc.notifications.empty():
                        event = self._notification_event(self.rpc.notifications.get_nowait())
                        if event:
                            if event.kind == "text_delta":
                                response_chunks.append(event.text)
                            yield event
                    response_text = "".join(response_chunks).strip()
                    if response_text:
                        yield AgentEvent("final", response_text, payload=result)
                    yield AgentEvent("completed", payload=result)
                    return
        except TimeoutError:
            await self.stop()
            raise AgentError("Cursor turn timed out") from None
        finally:
            for task in (prompt_task, request_task, notification_task):
                if not task.done():
                    task.cancel()

    async def _handle_request(self, request: dict[str, Any]) -> AsyncIterator[AgentEvent]:
        assert self.rpc
        method = str(request.get("method", ""))
        request_id = request.get("id")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if request_id is None:
            return
        token = str(request_id)
        if method == "session/request_permission" and self.file_access != FileAccessMode.FULL_ACCESS:
            option_id = self._permission_option(params, allow=False)
            await self.rpc.respond(
                request_id,
                {"outcome": {"outcome": "selected", "optionId": option_id}},
            )
            yield AgentEvent(
                "policy_denied",
                "Запрос Cursor на повышение прав автоматически отклонён политикой задачи.",
            )
            return
        if self.approval_mode == ApprovalMode.AUTO and method in {
            "session/request_permission",
            "cursor/create_plan",
        }:
            if method == "cursor/create_plan":
                result = {"outcome": {"outcome": "accepted"}}
            else:
                result = {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": self._permission_option(params, allow=True),
                    }
                }
            await self.rpc.respond(request_id, result)
            yield AgentEvent(
                "progress",
                "Автопроверка разрешила действие Cursor внутри границ задачи.",
            )
            return
        if method in {"session/request_permission", "cursor/create_plan"}:
            self._approval_requests[token] = {
                "rpc_id": request_id,
                "method": method,
                "params": params,
            }
            tool = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            title = (
                params.get("title")
                or params.get("name")
                or tool.get("title")
                or tool.get("name")
                or ("План Cursor" if method == "cursor/create_plan" else "Действие Cursor")
            )
            arguments = tool.get("rawInput") or tool.get("input") or params
            yield AgentEvent(
                "approval",
                str(title),
                payload={
                    "request_id": token,
                    "tool_name": str(title),
                    "input": arguments if isinstance(arguments, dict) else {"detail": arguments},
                },
            )
            return
        if method == "cursor/ask_question":
            questions = params.get("questions")
            text = "Cursor запросил уточнение, которое нельзя выбрать этой кнопкой. Ответьте следующим сообщением."
            if isinstance(questions, list):
                prompts = [
                    str(item.get("prompt"))
                    for item in questions
                    if isinstance(item, dict) and item.get("prompt")
                ]
                if prompts:
                    text += "\n" + "\n".join(f"• {item}" for item in prompts)
            await self.rpc.respond(
                request_id,
                {"outcome": {"outcome": "skipped", "reason": "Answer will be provided in the next user message"}},
            )
            yield AgentEvent("progress", text, payload=params)
            return
        await self.rpc.respond(request_id, {"outcome": {"outcome": "cancelled"}})
        yield AgentEvent("progress", f"Неподдерживаемый запрос Cursor отклонён: {method}", payload=params)

    @staticmethod
    def _notification_event(note: dict[str, Any]) -> AgentEvent | None:
        method = str(note.get("method", ""))
        params = note.get("params") if isinstance(note.get("params"), dict) else {}
        if method != "session/update":
            return None
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = str(update.get("sessionUpdate", ""))
        content = update.get("content")
        text = content.get("text", "") if isinstance(content, dict) else ""
        if kind == "agent_message_chunk" and isinstance(text, str) and text:
            return AgentEvent("text_delta", text, payload=params)
        if kind in {"agent_thought_chunk", "tool_call", "tool_call_update", "plan"}:
            if not text:
                text = str(update.get("title") or update.get("status") or "")
            if text:
                return AgentEvent("progress", str(text), payload=params)
        return None

    async def answer_approval(self, request_id: str, decision: str) -> None:
        if not self.rpc or request_id not in self._approval_requests:
            raise AgentError("unknown or expired approval request")
        request = self._approval_requests.pop(request_id)
        method = request["method"]
        params = request["params"]
        rpc_id = request["rpc_id"]
        if decision not in {"accept", "decline", "cancel", "explain"}:
            raise AgentError("invalid approval decision")
        if (
            decision == "accept"
            and method == "session/request_permission"
            and self.file_access != FileAccessMode.FULL_ACCESS
        ):
            option_id = self._permission_option(params, allow=False)
            await self.rpc.respond(
                rpc_id,
                {"outcome": {"outcome": "selected", "optionId": option_id}},
            )
            raise AgentError("approval would exceed the task access policy")
        if method == "cursor/create_plan":
            if decision == "accept":
                result = {"outcome": {"outcome": "accepted"}}
            elif decision == "cancel":
                result = {"outcome": {"outcome": "cancelled"}}
            else:
                reason = "Please explain the plan before continuing" if decision == "explain" else "Rejected by user"
                result = {"outcome": {"outcome": "rejected", "reason": reason}}
        else:
            option_id = self._permission_option(params, allow=decision == "accept")
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        await self.rpc.respond(rpc_id, result)

    @staticmethod
    def _permission_option(params: dict[str, Any], *, allow: bool) -> str:
        fallback = "allow-once" if allow else "reject-once"
        options = params.get("options")
        if not isinstance(options, list):
            return fallback
        preferred = ("allow-once", "allow_once", "allow") if allow else ("reject-once", "reject_once", "deny")
        for option in options:
            if not isinstance(option, dict):
                continue
            option_id = option.get("optionId") or option.get("id")
            kind = str(option.get("kind", "")).casefold()
            candidate = str(option_id).casefold() if option_id is not None else ""
            if any(marker in candidate or marker in kind for marker in preferred):
                return str(option_id)
        return fallback

    async def stop(self) -> None:
        if self.rpc and self.session_id:
            try:
                await self.rpc.request("session/cancel", {"sessionId": self.session_id}, timeout_seconds=5)
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
