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
from ai_control.security.redaction import redact

_TEST_COMMAND_MARKERS = (
    " pytest",
    " py.test",
    " unittest",
    " npm test",
    " npm run test",
    " pnpm test",
    " yarn test",
    " cargo test",
    " go test",
    " dotnet test",
    " gradle test",
    " gradlew test",
)
_GIT_CHECK_MARKERS = (" git status", " git diff", " git show", " git log")
_LEGACY_MESSAGE_LIMIT = 64_000


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
        self._active_items: dict[str, dict[str, Any]] = {}
        self._agent_message_deltas: dict[str, str] = {}
        self._last_agent_message = ""
        self._final_answer_seen = False

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

    async def _connect(self, cwd: Path, session_id: str | None) -> tuple[str, bool]:
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
            try:
                result = await self.rpc.request(
                    "thread/resume",
                    params,
                )
            except AgentError as exc:
                # A forcibly interrupted Codex turn can occasionally leave one
                # local thread unreadable even though app-server itself is
                # healthy. Preserve the working tree and recover into a fresh
                # thread instead of failing the whole Telegram task.
                if "app-server closed" not in str(exc):
                    raise
                await self.rpc.close()
                self.rpc = None
                identifier, _ = await self._connect(cwd, None)
                return identifier, True
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
        return self.thread_id, False

    async def run_turn(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        attachments: tuple[Path, ...] = (),
    ) -> AsyncIterator[AgentEvent]:
        thread_id, recovered = await self._connect(cwd, session_id)
        yield AgentEvent("session", session_id=thread_id)
        if recovered:
            yield AgentEvent(
                "recovery",
                "Предыдущая сессия Codex не возобновилась; создана новая сессия "
                "с сохранением текущих файлов проекта.",
            )
            prompt = (
                "Предыдущая сессия Codex была прервана и не смогла возобновиться. "
                "Продолжи работу по текущему состоянию файлов и git diff. Не откатывай "
                "существующие изменения; проверь, что осталось незавершённым.\n\n"
                f"Последнее сообщение пользователя:\n{prompt}"
            )
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
        self._active_items.clear()
        self._agent_message_deltas.clear()
        self._last_agent_message = ""
        self._final_answer_seen = False
        yield AgentEvent("progress", "Начинает работу…", payload={"turn_id": self.turn_id})

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
                            delta = event_text(params)
                            item_id = str(params.get("itemId") or "")
                            if delta and item_id:
                                accumulated = self._agent_message_deltas.get(item_id, "") + delta
                                self._agent_message_deltas[item_id] = accumulated[-_LEGACY_MESSAGE_LIMIT:]
                                active_item = self._active_items.get(item_id, {})
                                if active_item.get("phase") != "commentary":
                                    self._last_agent_message = self._agent_message_deltas[item_id]
                            # Deltas are activity heartbeats only. The authoritative
                            # completed agentMessage decides what appears as the result.
                            yield AgentEvent("text_delta", delta, payload={"item_id": item_id})
                        elif method == "item/completed" and _is_agent_message(params):
                            item = params["item"]
                            item_id = str(item.get("id") or "")
                            text = str(item.get("text") or self._agent_message_deltas.pop(item_id, ""))
                            self._active_items.pop(item_id, None)
                            phase = item.get("phase")
                            if phase == "final_answer":
                                self._final_answer_seen = True
                                if text:
                                    yield AgentEvent("final", text, payload={"phase": phase, "item_id": item_id})
                            elif phase is None and text:
                                self._last_agent_message = text
                        elif method in {"turn/completed", "turn/failed"}:
                            if not self._final_answer_seen and self._last_agent_message:
                                yield AgentEvent(
                                    "final",
                                    self._last_agent_message,
                                    payload={"phase": "unknown"},
                                )
                            yield AgentEvent("completed", payload=params)
                            return
                        elif method in {"error", "turn/error"}:
                            yield AgentEvent("error", event_text(params), payload=params)
                        else:
                            progress = _codex_progress_event(note, cwd, self._active_items)
                            if progress:
                                yield progress
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


def _is_agent_message(params: Any) -> bool:
    return (
        isinstance(params, dict)
        and isinstance(params.get("item"), dict)
        and params["item"].get("type") == "agentMessage"
    )


def _codex_progress_event(
    notification: dict[str, Any],
    cwd: Path,
    active_items: dict[str, dict[str, Any]] | None = None,
) -> AgentEvent | None:
    """Convert app-server tool lifecycle notifications without exposing tool data."""
    method = str(notification.get("method") or "")
    raw_params = notification.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    raw_item = params.get("item")
    item = raw_item if isinstance(raw_item, dict) else None
    item_id = str(params.get("itemId") or (item or {}).get("id") or "")

    if method == "item/started" and item:
        if active_items is not None and item_id:
            active_items[item_id] = item
        text = _item_progress_text(item, cwd, completed=False)
    elif method == "item/completed" and item:
        text = _item_progress_text(item, cwd, completed=True)
        if active_items is not None and item_id:
            active_items.pop(item_id, None)
    elif method == "item/fileChange/patchUpdated":
        text = _file_change_text(params.get("changes"), cwd, completed=False)
    elif method == "item/commandExecution/outputDelta":
        known = active_items.get(item_id) if active_items is not None else None
        text = _command_progress_text(known or {}, cwd, completed=False)
    elif method == "item/mcpToolCall/progress":
        text = "Выполняет MCP-вызов…"
    else:
        return None

    if not text:
        return None
    # Only the synthesized description is retained. Raw command output, patches,
    # arguments and environment data stay inside the adapter and are never logged.
    return AgentEvent("progress", redact(text)[:240], payload={"method": method, "item_id": item_id})


def _item_progress_text(item: dict[str, Any], cwd: Path, *, completed: bool) -> str | None:
    item_type = item.get("type")
    if item_type == "commandExecution":
        return _command_progress_text(item, cwd, completed=completed)
    if item_type == "fileChange":
        return _file_change_text(item.get("changes"), cwd, completed=completed)
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        return "MCP-вызов завершён." if completed else "Выполняет MCP-вызов…"
    if item_type == "webSearch":
        return "Поиск в интернете завершён." if completed else "Ищет в интернете…"
    if item_type == "imageView":
        path = _display_path(item.get("path"), cwd)
        return f"Чтение {path} завершено." if completed and path else "Читает файл…"
    return None


def _command_progress_text(item: dict[str, Any], cwd: Path, *, completed: bool) -> str:
    raw_actions = item.get("commandActions")
    actions = [action for action in raw_actions if isinstance(action, dict)] if isinstance(raw_actions, list) else []
    action_types = {str(action.get("type") or "") for action in actions}
    path = next((_display_path(action.get("path"), cwd) for action in actions if action.get("path")), None)

    if "search" in action_types:
        return "Поиск по проекту завершён." if completed else "Ищет по проекту…"
    if action_types & {"read", "listFiles"}:
        if completed:
            return f"Чтение {path} завершено." if path else "Чтение файлов завершено."
        return f"Читает {path}…" if path else "Читает файлы…"

    # app-server's structured commandActions can be `unknown`; inspect the raw
    # command only for classification and never include it in the emitted event.
    command = f" {str(item.get('command') or '').casefold()} "
    if any(marker in command for marker in _TEST_COMMAND_MARKERS):
        return "Тесты завершены." if completed else "Запускает тесты…"
    if any(marker in command for marker in _GIT_CHECK_MARKERS):
        return "Проверка изменений Git завершена." if completed else "Проверяет изменения Git…"
    return "Команда завершена." if completed else "Запускает команду…"


def _file_change_text(changes: Any, cwd: Path, *, completed: bool) -> str:
    path: str | None = None
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict) and change.get("path"):
                path = _display_path(change["path"], cwd)
                break
    if completed:
        return f"Изменение {path} завершено." if path else "Изменение файлов завершено."
    return f"Изменяет {path}…" if path else "Изменяет файлы…"


def _display_path(value: Any, cwd: Path, *, max_length: int = 96) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidate = Path(raw)
    try:
        resolved = (
            candidate.resolve(strict=False) if candidate.is_absolute() else (cwd / candidate).resolve(strict=False)
        )
        display = resolved.relative_to(cwd.resolve(strict=False)).as_posix()
    except (OSError, ValueError):
        parts = [part for part in raw.replace("\\", "/").split("/") if part]
        display = "…/" + "/".join(parts[-2:]) if parts else "…"
    display = redact(display.replace("\\", "/"))
    if len(display) > max_length:
        display = "…" + display[-(max_length - 1) :]
    return display
