from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ai_control.agents import AgentAdapter, ClaudeAdapter, CodexAdapter, CursorAdapter
from ai_control.agents.base import AgentError
from ai_control.config.models import AppConfig
from ai_control.core.models import (
    AgentEvent,
    AgentKind,
    AgentSession,
    ApprovalMode,
    FileAccessMode,
    Project,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from ai_control.git import GitService
from ai_control.platform import PlatformAdapter
from ai_control.platform.wsl import WslPlatform
from ai_control.projects import ProjectRegistry
from ai_control.security.redaction import redact
from ai_control.storage import Database

EventHandler = Callable[[TaskRecord, AgentEvent], Awaitable[None]]
logger = logging.getLogger(__name__)


class TaskManagerError(RuntimeError):
    pass


class TaskManager:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        projects: ProjectRegistry,
        platform: PlatformAdapter,
        git: GitService,
    ) -> None:
        self.config = config
        self.database = database
        self.projects = projects
        self.platform = platform
        self.git = git
        self._semaphore = asyncio.Semaphore(config.instance.max_parallel_tasks)
        self._runners: dict[int, asyncio.Task[None]] = {}
        self._adapters: dict[int, AgentAdapter] = {}
        self._checkout_owners: dict[str, int] = {}
        self._handlers: list[EventHandler] = []
        self._activity_updates: dict[int, float] = {}
        self._guard = asyncio.Lock()
        self._shutting_down = False

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def _adapter(
        self,
        kind: AgentKind,
        project: Project,
        model: str | None = None,
        access_mode: FileAccessMode = FileAccessMode.PROJECT_ONLY,
        approval_mode: ApprovalMode = ApprovalMode.MANUAL,
    ) -> AgentAdapter:
        platform = self.platform
        if project.runtime.value == "wsl":
            assert project.wsl_distribution
            platform = WslPlatform(
                self.platform,
                project.wsl_distribution,
                self.config.instance.data_dir.expanduser(),
            )
        if kind == AgentKind.CODEX:
            return CodexAdapter(
                platform,
                self.config.codex.executable,
                turn_timeout=self.config.codex.turn_timeout_seconds,
                model=model,
                file_access=access_mode,
                approval_mode=approval_mode,
                approved_paths=project.approved_paths,
            )
        if kind == AgentKind.CLAUDE:
            return ClaudeAdapter(
                platform,
                self.config.claude.executable,
                turn_timeout=self.config.claude.turn_timeout_seconds,
                model=model,
                file_access=access_mode,
                approval_mode=approval_mode,
                approved_paths=project.approved_paths,
            )
        if kind == AgentKind.CURSOR:
            return CursorAdapter(
                platform,
                self.config.cursor.executable,
                turn_timeout=self.config.cursor.turn_timeout_seconds,
                model=model,
                file_access=access_mode,
                approval_mode=approval_mode,
                approved_paths=project.approved_paths,
            )
        raise TaskManagerError(f"unsupported agent: {kind}")

    async def create(
        self,
        *,
        project_id: str,
        user_id: int,
        agent: AgentKind,
        model: str | None = None,
        prompt: str,
        isolated: bool = False,
        attachments: tuple[Path, ...] = (),
    ) -> TaskRecord:
        if self._shutting_down:
            raise TaskManagerError("AI Control is shutting down")
        project = await self.projects.get(project_id)
        if not project:
            raise TaskManagerError("project is not registered")
        if agent not in project.agents:
            raise TaskManagerError("agent is not allowed for this project")
        model = self._validate_model(agent, model)
        issues = await self.projects.validate(project)
        if issues:
            raise TaskManagerError("project validation failed: " + "; ".join(issues))
        task_id = await self.database.execute(
            "INSERT INTO tasks(project_id,user_id,agent,model,status,checkout_path,prompt,access_mode) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                project.id,
                user_id,
                agent.value,
                model,
                TaskStatus.PENDING.value,
                str(project.path),
                prompt,
                project.file_access.value,
            ),
        )
        try:
            checkout = await self._select_checkout(task_id, project, isolated)
        except Exception as exc:
            await self.database.execute(
                "UPDATE tasks SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (TaskStatus.FAILED.value, str(exc)[:4000], task_id),
            )
            raise TaskManagerError(str(exc)) from exc
        await self.database.execute(
            "UPDATE tasks SET checkout_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(checkout), task_id),
        )
        record = await self.get(task_id)
        assert record
        if self._shutting_down:
            await self._set_status(task_id, TaskStatus.STOPPED)
            async with self._guard:
                self._checkout_owners.pop(self._checkout_key(checkout), None)
            raise TaskManagerError("AI Control is shutting down")
        runner = asyncio.create_task(self._run(record, prompt, attachments), name=f"task-{task_id}")
        self._runners[task_id] = runner
        await self.database.audit("task_created", user_id=user_id, task_id=task_id)
        return record

    async def _select_checkout(self, task_id: int, project: Project, isolated: bool) -> Path:
        async with self._guard:
            is_wsl = project.runtime.value == "wsl"
            checkout = project.path if is_wsl else project.path.resolve()
            key = self._checkout_key(checkout)
            needs_worktree = isolated or key in self._checkout_owners
            if needs_worktree:
                if not project.worktrees_enabled:
                    raise TaskManagerError("checkout is busy and worktrees are disabled")
                branch = f"ai-control/task-{task_id}"
                if is_wsl:
                    target = Path(
                        project.path.as_posix().rstrip("/") + f"/.ai-control-worktrees/{project.id}/task-{task_id}"
                    )
                    assert project.wsl_distribution
                    await self.git.create_wsl_worktree(
                        project.wsl_distribution,
                        project.path.as_posix(),
                        target.as_posix(),
                        branch,
                    )
                else:
                    target = (
                        self.config.instance.data_dir.expanduser() / "worktrees" / project.id / f"task-{task_id}"
                    ).resolve()
                    await self.git.create_worktree(project.path, target, branch)
                worktree_id = await self.database.execute(
                    "INSERT INTO worktrees(project_id,path,branch,task_id) VALUES(?,?,?,?)",
                    (project.id, str(target), branch, task_id),
                )
                await self.database.execute("UPDATE tasks SET worktree_id=? WHERE id=?", (worktree_id, task_id))
                checkout = target
            self._checkout_owners[self._checkout_key(checkout)] = task_id
            return checkout

    async def _run(self, record: TaskRecord, prompt: str, attachments: tuple[Path, ...]) -> None:
        async with self._semaphore:
            project = await self.projects.get(record.project_id)
            if not project:
                await self._set_status(record.id, TaskStatus.FAILED)
                async with self._guard:
                    self._checkout_owners.pop(self._checkout_key(record.checkout_path), None)
                return
            adapter = self._adapter(
                record.agent,
                project,
                record.model,
                record.access_mode,
                record.approval_mode,
            )
            self._adapters[record.id] = adapter
            await self._set_status(record.id, TaskStatus.RUNNING)
            record.status = TaskStatus.RUNNING
            log_path = self._log_path(record.id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with log_path.open("a", encoding="utf-8") as log:
                    async for event in adapter.run_turn(
                        prompt,
                        cwd=record.checkout_path,
                        session_id=record.agent_session_id,
                        attachments=attachments,
                    ):
                        event.text = redact(event.text)
                        log.write(json.dumps({"kind": event.kind, "text": event.text}, ensure_ascii=False) + "\n")
                        log.flush()
                        if event.session_id:
                            record.agent_session_id = event.session_id
                            await self.database.execute(
                                "UPDATE tasks SET agent_session_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (event.session_id, record.id),
                            )
                        if event.kind in {
                            "progress",
                            "status",
                            "text_delta",
                            "final",
                            "approval",
                            "policy_denied",
                            "recovery",
                        }:
                            await self._touch_activity(record)
                        if event.kind == "approval":
                            record.status = TaskStatus.WAITING_APPROVAL
                            await self._set_status(record.id, record.status)
                        elif event.kind not in {"completed", "error"} and record.status == TaskStatus.WAITING_APPROVAL:
                            record.status = TaskStatus.RUNNING
                            await self._set_status(record.id, record.status)
                        await self.database.execute(
                            "INSERT INTO messages(task_id,role,kind,content) VALUES(?,?,?,?)",
                            (record.id, "agent", event.kind, event.text),
                        )
                        if event.kind == "policy_denied":
                            await self.database.audit(
                                "agent_action_policy_denied",
                                user_id=record.user_id,
                                task_id=record.id,
                                detail={"agent": record.agent.value, "access_mode": record.access_mode.value},
                            )
                        await self._emit(record, event)
                if record.status not in {TaskStatus.STOPPED, TaskStatus.FAILED}:
                    await self._record_changed_files(record)
                    record.status = TaskStatus.COMPLETED
                    await self._set_status(record.id, record.status)
            except asyncio.CancelledError:
                record.status = TaskStatus.STOPPED
                await self._set_status(record.id, record.status)
                raise
            except (AgentError, OSError, RuntimeError, ValueError) as exc:
                logger.exception("Task %s failed", record.id)
                record.status = TaskStatus.FAILED
                record.error = str(exc)
                await self.database.execute(
                    "UPDATE tasks SET status=?,error=?,pid=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (record.status.value, str(exc)[:4000], record.id),
                )
                await self._emit(record, AgentEvent("error", str(exc)))
            finally:
                await adapter.stop()
                self._adapters.pop(record.id, None)
                self._runners.pop(record.id, None)
                self._activity_updates.pop(record.id, None)
                async with self._guard:
                    self._checkout_owners.pop(self._checkout_key(record.checkout_path), None)

    async def continue_task(self, task_id: int, user_id: int, prompt: str) -> TaskRecord:
        if self._shutting_down:
            raise TaskManagerError("AI Control is shutting down")
        record = await self.get(task_id)
        if not record or record.user_id != user_id:
            raise TaskManagerError("task not found")
        if task_id in self._runners:
            raise TaskManagerError("task is already running")
        if not record.agent_session_id:
            raise TaskManagerError("agent session ID is unavailable")
        async with self._guard:
            checkout_key = self._checkout_key(record.checkout_path)
            if checkout_key in self._checkout_owners:
                raise TaskManagerError("checkout is busy")
            self._checkout_owners[checkout_key] = task_id
            refreshed = await self.get(task_id)
            if not refreshed or refreshed.user_id != user_id:
                self._checkout_owners.pop(checkout_key, None)
                raise TaskManagerError("task not found")
            record = refreshed
        await self.database.execute(
            "UPDATE tasks SET prompt=?,status=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (prompt, TaskStatus.PENDING.value, task_id),
        )
        record.prompt = prompt
        record.status = TaskStatus.PENDING
        if self._shutting_down:
            await self._set_status(task_id, TaskStatus.STOPPED)
            async with self._guard:
                self._checkout_owners.pop(self._checkout_key(record.checkout_path), None)
            raise TaskManagerError("AI Control is shutting down")
        runner = asyncio.create_task(self._run(record, prompt, ()), name=f"task-{task_id}")
        self._runners[task_id] = runner
        return record

    async def list_importable_sessions(
        self,
        *,
        project_id: str,
        user_id: int,
        agent: AgentKind,
        limit: int = 20,
    ) -> list[AgentSession]:
        project = await self.projects.get(project_id)
        if not project:
            raise TaskManagerError("project is not registered")
        if agent not in project.agents:
            raise TaskManagerError("agent is not allowed for this project")
        issues = await self.projects.validate(project)
        if issues:
            raise TaskManagerError("project validation failed: " + "; ".join(issues))
        adapter = self._adapter(agent, project)
        try:
            sessions = await adapter.list_sessions(cwd=project.path, limit=limit)
        except (AgentError, OSError, RuntimeError) as exc:
            raise TaskManagerError(str(exc)) from exc
        finally:
            await adapter.stop()
        rows = await self.database.fetch_all(
            "SELECT id,agent_session_id FROM tasks "
            "WHERE user_id=? AND agent=? AND agent_session_id IS NOT NULL",
            (user_id, agent.value),
        )
        imported = {str(row["agent_session_id"]): int(row["id"]) for row in rows}
        visible = []
        for session in sessions:
            if not self._path_within(session.cwd, project.path):
                continue
            session.imported_task_id = imported.get(session.id)
            visible.append(session)
        return visible

    async def import_session(
        self,
        *,
        project_id: str,
        user_id: int,
        agent: AgentKind,
        session_id: str,
    ) -> TaskRecord:
        project = await self.projects.get(project_id)
        if not project:
            raise TaskManagerError("project is not registered")
        sessions = await self.list_importable_sessions(
            project_id=project_id,
            user_id=user_id,
            agent=agent,
            limit=100,
        )
        session = next((item for item in sessions if item.id == session_id), None)
        if not session:
            existing = await self.database.fetch_one(
                "SELECT * FROM tasks WHERE user_id=? AND agent=? AND agent_session_id=?",
                (user_id, agent.value, session_id),
            )
            if existing:
                return self._from_row(existing)
            raise TaskManagerError("session is unavailable or outside the selected project")
        claimed = await self.database.fetch_one(
            "SELECT id,user_id FROM tasks WHERE agent=? AND agent_session_id=?",
            (agent.value, session.id),
        )
        if claimed:
            if int(claimed["user_id"]) != user_id:
                raise TaskManagerError("session is already imported by another user")
            record = await self.get(int(claimed["id"]))
            assert record
            return record
        if session.active:
            raise TaskManagerError("session is active")
        task_id = await self.database.execute(
            "INSERT INTO tasks("
            "project_id,user_id,agent,model,status,checkout_path,prompt,agent_session_id,access_mode"
            ") "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                user_id,
                agent.value,
                None,
                TaskStatus.COMPLETED.value,
                str(session.cwd),
                f"Импортированная сессия: {session.title}",
                session.id,
                project.file_access.value,
            ),
        )
        await self.database.audit(
            "session_imported",
            user_id=user_id,
            task_id=task_id,
            detail={"agent": agent.value, "session_id": session.id},
        )
        record = await self.get(task_id)
        assert record
        return record

    async def change_model(self, task_id: int, user_id: int, model: str) -> TaskRecord:
        record = await self.get(task_id)
        if not record or record.user_id != user_id:
            raise TaskManagerError("task not found")
        if task_id in self._runners or record.status in {
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_APPROVAL,
        }:
            raise TaskManagerError("task is running")
        selected = self._validate_model(record.agent, model)
        await self.database.execute(
            "UPDATE tasks SET model=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (selected, task_id),
        )
        await self.database.audit(
            "task_model_changed",
            user_id=user_id,
            task_id=task_id,
            detail={"model": selected},
        )
        record.model = selected
        return record

    async def available_access_modes(self, task_id: int, user_id: int) -> tuple[FileAccessMode, ...]:
        record = await self.get(task_id)
        if not record or record.user_id != user_id:
            raise TaskManagerError("task not found")
        project = await self.projects.get(record.project_id)
        if not project:
            raise TaskManagerError("project is not registered")
        modes = [FileAccessMode.READ_ONLY]
        if project.file_access != FileAccessMode.READ_ONLY:
            modes.append(FileAccessMode.PROJECT_ONLY)
        if (
            project.file_access in {FileAccessMode.APPROVED_PATHS, FileAccessMode.FULL_ACCESS}
            and project.approved_paths
        ):
            modes.append(FileAccessMode.APPROVED_PATHS)
        return tuple(modes)

    async def change_access_mode(
        self,
        task_id: int,
        user_id: int,
        access_mode: FileAccessMode,
    ) -> TaskRecord:
        async with self._guard:
            record = await self.get(task_id)
            if not record or record.user_id != user_id:
                raise TaskManagerError("task not found")
            checkout_key = self._checkout_key(record.checkout_path)
            if task_id in self._runners or checkout_key in self._checkout_owners or record.status in {
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.WAITING_APPROVAL,
            }:
                raise TaskManagerError("task is running")
            if access_mode not in await self.available_access_modes(task_id, user_id):
                raise TaskManagerError("access mode exceeds the local project policy")
            await self.database.execute(
                "UPDATE tasks SET access_mode=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (access_mode.value, task_id),
            )
        await self.database.audit(
            "task_access_changed",
            user_id=user_id,
            task_id=task_id,
            detail={"from": record.access_mode.value, "to": access_mode.value},
        )
        record.access_mode = access_mode
        return record

    async def change_approval_mode(
        self,
        task_id: int,
        user_id: int,
        approval_mode: ApprovalMode,
    ) -> TaskRecord:
        async with self._guard:
            record = await self.get(task_id)
            if not record or record.user_id != user_id:
                raise TaskManagerError("task not found")
            checkout_key = self._checkout_key(record.checkout_path)
            if task_id in self._runners or checkout_key in self._checkout_owners or record.status in {
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.WAITING_APPROVAL,
            }:
                raise TaskManagerError("task is running")
            await self.database.execute(
                "UPDATE tasks SET approval_mode=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (approval_mode.value, task_id),
            )
        await self.database.audit(
            "task_approval_mode_changed",
            user_id=user_id,
            task_id=task_id,
            detail={"from": record.approval_mode.value, "to": approval_mode.value},
        )
        record.approval_mode = approval_mode
        return record

    async def codex_usage(self, task_id: int, user_id: int) -> dict[str, object]:
        record = await self.get(task_id)
        if not record or record.user_id != user_id:
            raise TaskManagerError("task not found")
        if record.agent != AgentKind.CODEX:
            raise TaskManagerError("task does not use Codex")
        project = await self.projects.get(record.project_id)
        if not project:
            raise TaskManagerError("project is not registered")
        adapter = self._adapter(
            record.agent,
            project,
            record.model,
            record.access_mode,
            record.approval_mode,
        )
        if not isinstance(adapter, CodexAdapter):
            raise TaskManagerError("Codex adapter is unavailable")
        try:
            return await adapter.account_rate_limits(record.checkout_path)
        except AgentError as exc:
            raise TaskManagerError(str(exc)) from exc

    def available_models(self, kind: AgentKind) -> tuple[str, ...]:
        return self._agent_config(kind).models

    def default_model(self, kind: AgentKind) -> str | None:
        settings = self._agent_config(kind)
        return settings.default_model or (settings.models[0] if settings.models else None)

    def _agent_config(self, kind: AgentKind):  # type: ignore[no-untyped-def]
        if kind == AgentKind.CODEX:
            return self.config.codex
        if kind == AgentKind.CLAUDE:
            return self.config.claude
        if kind == AgentKind.CURSOR:
            return self.config.cursor
        raise TaskManagerError(f"unsupported agent: {kind}")

    def _validate_model(self, kind: AgentKind, model: str | None) -> str | None:
        selected = model or self.default_model(kind)
        available = self.available_models(kind)
        if selected and available and selected not in available:
            raise TaskManagerError("model is not allowed for this agent")
        return selected

    async def stop(self, task_id: int, user_id: int) -> None:
        record = await self.get(task_id)
        if not record or record.user_id != user_id:
            raise TaskManagerError("task not found")
        adapter = self._adapters.get(task_id)
        if adapter:
            await adapter.stop()
        runner = self._runners.get(task_id)
        if runner:
            runner.cancel()
        await self._set_status(task_id, TaskStatus.STOPPED)
        await self.database.audit("task_stopped", user_id=user_id, task_id=task_id)

    async def shutdown(self) -> None:
        """Stop accepting work and cancel every task owned by this process."""
        self._shutting_down = True
        runners = list(self._runners.items())
        for task_id, runner in runners:
            await self._set_status(task_id, TaskStatus.STOPPED)
            runner.cancel()
        if runners:
            await asyncio.gather(*(runner for _, runner in runners), return_exceptions=True)

    async def answer_approval(self, task_id: int, user_id: int, request_id: str, decision: str) -> None:
        record = await self.get(task_id)
        if not record or record.user_id != user_id:
            raise TaskManagerError("task not found")
        adapter = self._adapters.get(task_id)
        if not adapter:
            raise TaskManagerError("task process is not running")
        await adapter.answer_approval(request_id, decision)
        await self._set_status(task_id, TaskStatus.RUNNING)

    async def recover(self) -> int:
        return await self.database.execute(
            "UPDATE tasks SET status='lost',pid=NULL,error='Process lost during service restart',"
            "updated_at=CURRENT_TIMESTAMP WHERE status IN ('pending','running','waiting_approval')"
        )

    async def get(self, task_id: int) -> TaskRecord | None:
        row = await self.database.fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return self._from_row(row) if row else None

    async def list_active(self, user_id: int, *, limit: int = 50, offset: int = 0) -> list[TaskRecord]:
        rows = await self.database.fetch_all(
            "SELECT * FROM tasks WHERE user_id=? AND status NOT IN ('closed') "
            "ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        return [self._from_row(row) for row in rows]

    async def count_active(self, user_id: int) -> int:
        row = await self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM tasks WHERE user_id=? AND status NOT IN ('closed')",
            (user_id,),
        )
        return int(row["count"]) if row else 0

    async def _set_status(self, task_id: int, status: TaskStatus) -> None:
        await self.database.execute(
            "UPDATE tasks SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status.value, task_id),
        )

    async def _touch_activity(self, record: TaskRecord) -> None:
        """Persist a heartbeat without writing SQLite for every streamed token."""
        now = time.monotonic()
        record.updated_at = utc_now()
        previous = self._activity_updates.get(record.id, 0)
        if now - previous < 1:
            return
        self._activity_updates[record.id] = now
        await self.database.execute(
            "UPDATE tasks SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (record.id,),
        )

    async def _emit(self, record: TaskRecord, event: AgentEvent) -> None:
        for handler in self._handlers:
            try:
                await handler(record, event)
            except Exception:
                logger.warning("Task event handler failed for task %s", record.id, exc_info=True)

    def _log_path(self, task_id: int) -> Path:
        return self.config.instance.data_dir.expanduser() / "tasks" / str(task_id) / "events.jsonl"

    async def _record_changed_files(self, record: TaskRecord) -> None:
        try:
            paths = await self.git.changed_files(record.checkout_path)
        except Exception:
            logger.warning("Cannot enumerate changed files for task %s", record.id, exc_info=True)
            return
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            await self.database.execute(
                "INSERT INTO files(task_id,path,direction,size) VALUES(?,?,?,?)",
                (record.id, str(path), "outbound", size),
            )

    @staticmethod
    def _checkout_key(path: Path) -> str:
        return str(path).replace("\\", "/").rstrip("/").casefold()

    @staticmethod
    def _path_within(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _from_row(row: dict[str, object]) -> TaskRecord:
        return TaskRecord(
            id=int(row["id"]),
            project_id=str(row["project_id"]),
            user_id=int(row["user_id"]),
            agent=AgentKind(str(row["agent"])),
            status=TaskStatus(str(row["status"])),
            checkout_path=Path(str(row["checkout_path"])),
            prompt=str(row["prompt"]),
            access_mode=FileAccessMode(str(row.get("access_mode") or FileAccessMode.PROJECT_ONLY.value)),
            approval_mode=ApprovalMode(str(row.get("approval_mode") or ApprovalMode.MANUAL.value)),
            model=str(row["model"]) if row.get("model") else None,
            agent_session_id=str(row["agent_session_id"]) if row["agent_session_id"] else None,
            pid=int(row["pid"]) if row["pid"] else None,
            worktree_id=int(row["worktree_id"]) if row["worktree_id"] else None,
            error=str(row["error"]) if row["error"] else None,
        )
