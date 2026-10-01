from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_control.agents.base import AgentAdapter, AgentError, event_text
from ai_control.core.models import (
    AgentEvent,
    AgentKind,
    AgentSession,
    ApprovalMode,
    CapabilityReport,
    FileAccessMode,
)
from ai_control.files.policy import PathPolicy
from ai_control.platform import PlatformAdapter
from ai_control.platform.base import ManagedProcess


class ClaudeAdapter(AgentAdapter):
    def __init__(
        self,
        platform: PlatformAdapter,
        executable: str = "claude",
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
        self.process: ManagedProcess | None = None
        self._approval_requests: dict[str, dict[str, Any]] = {}
        self._approval_waiters: dict[str, asyncio.Future[str]] = {}
        self._tool_uses: dict[str, dict[str, Any]] = {}
        self._fallback_writes: list[Path] = []
        self._cwd: Path | None = None

    async def capabilities(self) -> CapabilityReport:
        executable = shutil.which(self.executable)
        if not executable:
            return CapabilityReport(False, None, "claude executable not found")
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
                "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            help_stdout, _ = await asyncio.wait_for(help_process.communicate(), 10)
            required = (b"stream-json", b"--input-format", b"--permission-prompts", b"--restricted")
            if help_process.returncode or not all(item in help_stdout for item in required):
                return CapabilityReport(
                    False,
                    version,
                    "bidirectional stream-json permissions are unavailable; update Claude Code",
                )
            return CapabilityReport(
                True,
                version,
                "structured stream-json mode",
                frozenset({"streaming", "resume", "interrupt"}),
            )
        except (OSError, TimeoutError) as exc:
            return CapabilityReport(False, None, str(exc))

    async def list_sessions(self, *, cwd: Path, limit: int = 20) -> list[AgentSession]:
        active_ids = await self._active_session_ids(cwd)
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")
        return _read_claude_sessions(
            config_dir,
            cwd,
            min(max(limit, 1), 100),
            active_ids,
        )

    async def _active_session_ids(self, cwd: Path) -> set[str]:
        process: ManagedProcess | None = None
        try:
            process = await self.platform.spawn(
                (self.executable, "agents", "--json", "--all", "--cwd", str(cwd)),
                cwd=cwd,
            )
            if not process.stdout:
                return set()
            async with asyncio.timeout(15):
                stdout = await process.stdout.read()
                code = await process.wait()
            if code:
                return set()
            payload = json.loads(stdout)
        except (AgentError, OSError, TimeoutError, json.JSONDecodeError):
            if process:
                await self.platform.terminate_tree(process)
            return set()
        if not isinstance(payload, list):
            return set()
        active: set[str] = set()
        for item in payload:
            if not isinstance(item, dict):
                continue
            identifier = item.get("sessionId") or item.get("session_id") or item.get("id")
            status = str(item.get("status", "running")).casefold()
            if identifier and status not in {"completed", "stopped", "failed", "exited"}:
                active.add(str(identifier))
        return active

    async def run_turn(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        attachments: tuple[Path, ...] = (),
    ) -> AsyncIterator[AgentEvent]:
        identifier = session_id or str(uuid.uuid4())
        self._cwd = cwd
        self._tool_uses.clear()
        self._fallback_writes.clear()
        enriched = prompt
        if attachments:
            enriched += "\n\nFiles uploaded for this task:\n" + "\n".join(f"- {path}" for path in attachments)
        argv = [
            self.executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--include-partial-messages",
            "--permission-mode",
            (
                "plan"
                if self.file_access == FileAccessMode.READ_ONLY
                else "auto" if self.approval_mode == ApprovalMode.AUTO else "manual"
            ),
            "--permission-prompts",
            "host",
        ]
        if self.file_access != FileAccessMode.FULL_ACCESS:
            argv.append("--restricted")
        if self.model:
            argv.extend(("--model", self.model))
        if session_id:
            argv.extend(("--resume", session_id))
        else:
            argv.extend(("--session-id", identifier))
        if self.file_access == FileAccessMode.APPROVED_PATHS:
            for path in self.approved_paths:
                argv.extend(("--add-dir", str(path)))
        self.process = await self.platform.spawn(tuple(argv), cwd=cwd)
        await self._initialize_control_channel()
        assert self.process.stdin
        self.process.stdin.write(
            (
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": enriched},
                        "parent_tool_use_id": None,
                        "session_id": identifier,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
        await self.process.stdin.drain()
        yield AgentEvent("session", session_id=identifier)
        assert self.process.stdout
        try:
            async with asyncio.timeout(self.turn_timeout):
                while line := await self.process.stdout.readline():
                    try:
                        item: dict[str, Any] = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = item.get("type", "event")
                    discovered = item.get("session_id") or item.get("sessionId")
                    if discovered and discovered != identifier:
                        identifier = str(discovered)
                        yield AgentEvent("session", session_id=identifier)
                    if kind == "stream_event":
                        event = item.get("event", {})
                        delta = event.get("delta", {}) if isinstance(event, dict) else {}
                        text = event_text(delta)
                        if text and not self._fallback_writes:
                            yield AgentEvent("text_delta", text, payload=item)
                    elif kind in {"control_request", "permission_request"}:
                        request_id = str(item.get("request_id") or item.get("requestId") or "")
                        raw_request = item.get("request", item)
                        request = raw_request if isinstance(raw_request, dict) else {}
                        if request_id and self._permission_allowed(request):
                            if self.approval_mode == ApprovalMode.AUTO:
                                permission: dict[str, Any] = {"behavior": "allow"}
                                tool_input = request.get("input") or request.get("tool_input")
                                if isinstance(tool_input, dict):
                                    permission["updatedInput"] = tool_input
                                await self._respond_permission(request_id, permission)
                                yield AgentEvent(
                                    "progress",
                                    "Автопроверка разрешила действие Claude внутри границ задачи.",
                                )
                            else:
                                self._approval_requests[request_id] = request
                                title = request.get("title") or request.get("tool_name") or request.get("subtype")
                                yield AgentEvent(
                                    "approval",
                                    str(title or "Claude requests permission"),
                                    payload={"request_id": request_id, **request},
                                )
                        elif request_id:
                            await self._respond_permission(
                                request_id,
                                {
                                    "behavior": "deny",
                                    "message": "Blocked by the AI Control task access policy",
                                    "interrupt": False,
                                },
                            )
                            yield AgentEvent(
                                "policy_denied",
                                "Запрос Claude на повышение прав автоматически отклонён политикой задачи.",
                            )
                    elif kind == "result":
                        usage = self._usage_summary(item)
                        if usage:
                            yield AgentEvent("usage", json.dumps(usage, separators=(",", ":")), payload=item)
                        if item.get("is_error"):
                            yield AgentEvent("error", str(item.get("result", "Claude failed")), payload=item)
                        elif self._fallback_writes:
                            paths = "\n".join(f"• {path}" for path in self._fallback_writes)
                            yield AgentEvent("final", f"Изменения подтверждены и записаны:\n{paths}", payload=item)
                        else:
                            text = item.get("result")
                            if isinstance(text, str) and text:
                                yield AgentEvent("final", text, payload=item)
                        yield AgentEvent("completed", payload=item)
                        return
                    elif kind in {"assistant", "tool_use", "tool_result"}:
                        self._remember_tool_uses(item)
                        if not self._fallback_writes:
                            yield AgentEvent("progress", event_text(item), payload=item)
                    elif kind == "system" and item.get("subtype") == "permission_denied":
                        async for event in self._handle_permission_denial(item):
                            yield event
                code = await self.process.wait()
                if code:
                    stderr = ""
                    if self.process.stderr:
                        stderr = (await self.process.stderr.read()).decode(errors="replace")[-4000:]
                    raise AgentError(f"Claude exited with {code}: {stderr}")
        except TimeoutError:
            await self.stop()
            raise AgentError("Claude turn timed out") from None

    async def stop(self) -> None:
        if self.process:
            await self.platform.terminate_tree(self.process)
            self.process = None

    async def answer_approval(self, request_id: str, decision: str) -> None:
        if not self.process or not self.process.stdin or request_id not in self._approval_requests:
            raise AgentError("unknown or expired approval request")
        request = self._approval_requests.pop(request_id)
        waiter = self._approval_waiters.pop(request_id, None)
        if request.get("fallback"):
            if decision not in {"accept", "decline", "cancel", "explain"}:
                raise AgentError("invalid approval decision")
            if waiter and not waiter.done():
                waiter.set_result(decision)
            return
        if decision == "accept" and not self._permission_allowed(request):
            await self._respond_permission(
                request_id,
                {
                    "behavior": "deny",
                    "message": "Blocked by the AI Control task access policy",
                    "interrupt": False,
                },
            )
            raise AgentError("approval would exceed the task access policy")
        if decision == "accept":
            permission: dict[str, Any] = {"behavior": "allow"}
            tool_input = request.get("input") or request.get("tool_input")
            if isinstance(tool_input, dict):
                permission["updatedInput"] = tool_input
        elif decision in {"decline", "cancel", "explain"}:
            permission = {
                "behavior": "deny",
                "message": (
                    "Explain why this action is necessary, without performing it"
                    if decision == "explain"
                    else "Denied by the authorized AI Control user"
                ),
                "interrupt": decision == "cancel",
            }
        else:
            raise AgentError("invalid approval decision")
        await self._respond_permission(request_id, permission)

    async def _respond_permission(self, request_id: str, permission: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise AgentError("Claude process is unavailable")
        message = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": permission,
            },
        }
        self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await self.process.stdin.drain()

    def _permission_allowed(self, request: dict[str, Any]) -> bool:
        if self.file_access == FileAccessMode.FULL_ACCESS:
            return True
        tool_name = str(request.get("tool_name") or request.get("toolName") or request.get("name") or "")
        tool_input = request.get("input") or request.get("tool_input") or request.get("toolInput")
        if tool_name not in {"Write", "Edit"} or not isinstance(tool_input, dict):
            return False
        return self._file_tool_allowed(tool_input)

    def _file_tool_allowed(self, tool_input: dict[str, Any]) -> bool:
        if self._cwd is None:
            return False
        raw_path = tool_input.get("file_path")
        if not isinstance(raw_path, str) or not raw_path:
            return False
        target = Path(raw_path)
        if not target.is_absolute():
            target = self._cwd / target
        policy = PathPolicy(self._cwd, self.file_access, self.approved_paths)
        return policy.check(target, write=True, must_exist=False).allowed

    def _remember_tool_uses(self, item: dict[str, Any]) -> None:
        candidates: list[dict[str, Any]] = []
        if item.get("type") == "tool_use":
            candidates.append(item)
        message = item.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                candidates.extend(part for part in content if isinstance(part, dict) and part.get("type") == "tool_use")
        for tool_use in candidates:
            tool_id = tool_use.get("id")
            if isinstance(tool_id, str):
                self._tool_uses[tool_id] = tool_use

    @staticmethod
    def _usage_summary(item: dict[str, Any]) -> dict[str, int | float]:
        summary: dict[str, int | float] = {}
        usage = item.get("usage")
        if isinstance(usage, dict):
            keys = {
                "input_tokens": "input_tokens",
                "output_tokens": "output_tokens",
                "cache_creation_input_tokens": "cache_creation_tokens",
                "cache_read_input_tokens": "cache_read_tokens",
            }
            for source, target in keys.items():
                value = usage.get(source)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    summary[target] = value
        cost = item.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            summary["cost_usd"] = cost
        return summary

    async def _handle_permission_denial(self, item: dict[str, Any]) -> AsyncIterator[AgentEvent]:
        tool_id = item.get("tool_use_id")
        tool_use = self._tool_uses.get(str(tool_id), {})
        tool_name = str(item.get("tool_name") or tool_use.get("name") or "")
        tool_input = tool_use.get("input")
        if tool_name not in {"Write", "Edit"} or not isinstance(tool_input, dict):
            return
        if not self._file_tool_allowed(tool_input):
            yield AgentEvent(
                "policy_denied",
                f"Запрос Claude {tool_name} автоматически отклонён политикой задачи.",
            )
            return
        request_id = f"cf-{uuid.uuid4().hex[:24]}"
        request = {
            "fallback": True,
            "subtype": "can_use_tool",
            "tool_name": tool_name,
            "input": tool_input,
            "tool_use_id": tool_id,
        }
        self._approval_requests[request_id] = request
        waiter = asyncio.get_running_loop().create_future()
        self._approval_waiters[request_id] = waiter
        yield AgentEvent(
            "approval",
            f"Claude запрашивает {tool_name}",
            payload={"request_id": request_id, **request},
        )
        decision = await waiter
        if decision == "accept":
            path = self._apply_file_tool(tool_name, tool_input)
            self._fallback_writes.append(path)
            yield AgentEvent("progress", f"Подтверждено: {tool_name} {path}")
        elif decision == "explain":
            yield AgentEvent("progress", "Действие не выполнено: запрошено объяснение")

    def _apply_file_tool(self, tool_name: str, tool_input: dict[str, Any]) -> Path:
        if self._cwd is None:
            raise AgentError("Claude working directory is unavailable")
        raw_path = tool_input.get("file_path")
        if not isinstance(raw_path, str) or not raw_path:
            raise AgentError(f"Claude {tool_name} request has no file path")
        target = Path(raw_path)
        if not target.is_absolute():
            target = self._cwd / target
        policy = PathPolicy(self._cwd, self.file_access, self.approved_paths)
        decision = policy.check(target, write=True, must_exist=False)
        if not decision.allowed:
            raise AgentError(f"File write blocked by path policy: {decision.reason}")
        resolved = Path(decision.path)
        if tool_name == "Write":
            content = tool_input.get("content")
            if not isinstance(content, str):
                raise AgentError("Claude Write request has invalid content")
        else:
            if not resolved.is_file():
                raise AgentError("Claude Edit target does not exist")
            old = tool_input.get("old_string")
            new = tool_input.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                raise AgentError("Claude Edit request is invalid")
            original = resolved.read_text(encoding="utf-8")
            occurrences = original.count(old)
            if occurrences == 0:
                raise AgentError("Claude Edit text was not found")
            if not tool_input.get("replace_all") and occurrences != 1:
                raise AgentError("Claude Edit text is not unique")
            content = original.replace(old, new, -1 if tool_input.get("replace_all") else 1)
        if len(content.encode("utf-8")) > 10 * 1024 * 1024:
            raise AgentError("Claude file write exceeds the 10 MiB safety limit")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        temporary = resolved.with_name(f".{resolved.name}.ai-control-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, resolved)
        finally:
            temporary.unlink(missing_ok=True)
        return resolved

    async def _initialize_control_channel(self) -> None:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise AgentError("Claude process streams are unavailable")
        request_id = f"ai-control-init-{uuid.uuid4()}"
        message = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "initialize", "hooks": None},
        }
        self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await self.process.stdin.drain()
        try:
            async with asyncio.timeout(15):
                while line := await self.process.stdout.readline():
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    control = response.get("response", {})
                    if response.get("type") == "control_response" and control.get("request_id") == request_id:
                        if control.get("subtype") != "success":
                            raise AgentError(f"Claude control initialization failed: {control}")
                        return
        except TimeoutError:
            raise AgentError("Claude control initialization timed out; update Claude Code") from None
        raise AgentError("Claude closed before control initialization completed")


def _read_claude_sessions(
    config_dir: str,
    root: Path,
    limit: int,
    active_ids: set[str],
) -> list[AgentSession]:
    projects_dir = Path(config_dir).expanduser() / "projects"
    try:
        candidates = sorted(
            projects_dir.glob("*/*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    sessions: list[AgentSession] = []
    seen: set[str] = set()
    for path in candidates:
        if len(sessions) >= limit:
            break
        session = _read_claude_session(path, root, active_ids)
        if not session or session.id in seen:
            continue
        seen.add(session.id)
        sessions.append(session)
    return sessions


def _read_claude_session(path: Path, root: Path, active_ids: set[str]) -> AgentSession | None:
    identifier = path.stem
    title = ""
    session_cwd: Path | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= 500:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict) or item.get("isSidechain") is True:
                    continue
                discovered = item.get("sessionId") or item.get("session_id")
                if discovered:
                    identifier = str(discovered)
                raw_cwd = item.get("cwd")
                if isinstance(raw_cwd, str) and raw_cwd:
                    candidate = Path(raw_cwd)
                    if not _within(candidate, root):
                        return None
                    session_cwd = candidate
                if not title and item.get("type") == "user":
                    message = item.get("message")
                    if isinstance(message, dict):
                        title = _message_text(message.get("content"))
                    elif isinstance(message, str):
                        title = message
                if title and session_cwd:
                    break
        if not session_cwd:
            return None
        stat = path.stat()
    except OSError:
        return None
    clean_title = " ".join(title.split())[:80] or "Сессия Claude"
    return AgentSession(
        id=identifier,
        agent=AgentKind.CLAUDE,
        title=clean_title,
        cwd=session_cwd,
        updated_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        active=identifier in active_ids,
    )


def _message_text(content: object) -> str:
    if isinstance(content, str):
        if "<local-command-caveat>" in content:
            return ""
        if "<command-name>" in content:
            command = content.partition("<command-name>")[2].partition("</command-name>")[0].strip()
            return f"Команда {command}" if command else ""
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = _message_text(item.get("text"))
            if text:
                parts.append(text)
        return " ".join(parts)
    return ""


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False
