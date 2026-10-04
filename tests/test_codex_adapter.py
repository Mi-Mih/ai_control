import asyncio
import json
from pathlib import Path
from typing import Any

from ai_control.agents.codex import CodexAdapter, _codex_progress_event
from ai_control.core.models import ApprovalMode, FileAccessMode
from ai_control.platform.base import ManagedProcess, PlatformAdapter


class FakeWriter:
    def __init__(
        self,
        output: asyncio.StreamReader,
        stderr: asyncio.StreamReader,
        *,
        fail_resume: bool = False,
    ) -> None:
        self.output = output
        self.stderr = stderr
        self.fail_resume = fail_resume
        self.messages: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        message = json.loads(data)
        self.messages.append(message)
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None:
            return
        if method == "thread/resume" and self.fail_resume:
            self.stderr.feed_data(b"resume failed\n")
            self.stderr.feed_eof()
            self.output.feed_eof()
            return
        if method == "initialize":
            result: dict[str, Any] = {}
        elif method == "thread/start":
            result = {"thread": {"id": "thread-123"}}
        elif method == "thread/resume":
            result = {"thread": {"id": message["params"]["threadId"]}}
        elif method == "turn/start":
            result = {"turn": {"id": "turn-456"}}
        elif method == "account/rateLimits/read":
            result = {
                "ordinaryUsageAllowed": True,
                "rateLimits": {"primary": {"usedPercent": 25, "windowDurationMins": 300}},
            }
        elif method == "thread/list":
            result = {
                "data": [
                    {
                        "id": "external-thread",
                        "preview": "Existing Codex task",
                        "cwd": message["params"]["cwd"],
                        "updatedAt": 1_700_000_000,
                        "status": {"type": "notLoaded"},
                    }
                ]
            }
        else:
            result = {}
        self.output.feed_data((json.dumps({"id": request_id, "result": result}) + "\n").encode())
        if method == "turn/start":
            self.output.feed_data(
                b'{"method":"item/started","params":{"item":{"id":"message-1","type":"agentMessage",'
                b'"text":"","phase":"final_answer"}}}\n'
            )
            self.output.feed_data(
                b'{"method":"item/agentMessage/delta","params":{"itemId":"message-1","delta":"hello"}}\n'
            )
            self.output.feed_data(
                b'{"method":"item/completed","params":{"item":{"id":"message-1","type":"agentMessage",'
                b'"text":"hello","phase":"final_answer"}}}\n'
            )
            self.output.feed_data(b'{"method":"turn/completed","params":{"status":"completed"}}\n')

    async def drain(self) -> None:
        return None


class FakeProcess:
    def __init__(self, *, fail_resume: bool = False) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeWriter(self.stdout, self.stderr, fail_resume=fail_resume)
        self.pid = 123
        self.returncode = None

    async def wait(self) -> int:
        return 0


class FakePlatform(PlatformAdapter):
    def __init__(self, *, fail_first_resume: bool = False) -> None:
        self.process: FakeProcess | None = None
        self.processes: list[FakeProcess] = []
        self.fail_first_resume = fail_first_resume

    async def spawn(self, argv, *, cwd, env=None):  # type: ignore[no-untyped-def]
        self.process = FakeProcess(fail_resume=self.fail_first_resume and not self.processes)
        self.processes.append(self.process)
        return self.process  # type: ignore[return-value]

    async def terminate_tree(self, process: ManagedProcess, grace_seconds: float = 5) -> None:
        process.returncode = 0  # type: ignore[misc]


def test_codex_structured_protocol_and_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        platform = FakePlatform()
        adapter = CodexAdapter(platform, executable="codex", model="gpt-test")
        events = [event async for event in adapter.run_turn("hello", cwd=tmp_path, session_id="existing-thread")]
        assert events[0].session_id == "existing-thread"
        assert any(event.kind == "text_delta" and event.text == "hello" for event in events)
        assert any(event.kind == "final" and event.text == "hello" for event in events)
        assert all("Codex turn started" not in event.text for event in events)
        assert events[-1].kind == "completed"
        assert platform.process is not None
        resume = next(item for item in platform.process.stdin.messages if item.get("method") == "thread/resume")
        turn = next(item for item in platform.process.stdin.messages if item.get("method") == "turn/start")
        assert resume["params"]["model"] == "gpt-test"
        assert turn["params"]["model"] == "gpt-test"
        await adapter.stop()

    asyncio.run(scenario())


def test_codex_recovers_with_new_thread_when_resume_process_closes(tmp_path: Path) -> None:
    async def scenario() -> None:
        platform = FakePlatform(fail_first_resume=True)
        adapter = CodexAdapter(platform, executable="codex", model="gpt-test")

        events = [event async for event in adapter.run_turn("continue", cwd=tmp_path, session_id="broken-thread")]

        assert len(platform.processes) == 2
        assert events[0].session_id == "thread-123"
        assert events[1].kind == "recovery"
        assert events[-1].kind == "completed"
        turn = next(
            item for item in platform.processes[1].stdin.messages if item.get("method") == "turn/start"
        )
        prompt = turn["params"]["input"][0]["text"]
        assert "git diff" in prompt
        assert "continue" in prompt
        await adapter.stop()

    asyncio.run(scenario())


def test_codex_read_only_uses_read_only_sandbox(tmp_path: Path) -> None:
    async def scenario() -> None:
        platform = FakePlatform()
        adapter = CodexAdapter(
            platform,
            executable="codex",
            file_access=FileAccessMode.READ_ONLY,
        )
        _ = [event async for event in adapter.run_turn("inspect", cwd=tmp_path)]

        assert platform.process is not None
        start = next(item for item in platform.process.stdin.messages if item.get("method") == "thread/start")
        assert start["params"]["sandbox"] == "read-only"
        assert start["params"]["approvalPolicy"] == "never"

    asyncio.run(scenario())


def test_codex_full_access_is_the_only_mode_that_allows_approvals(tmp_path: Path) -> None:
    async def scenario() -> None:
        platform = FakePlatform()
        adapter = CodexAdapter(
            platform,
            executable="codex",
            file_access=FileAccessMode.FULL_ACCESS,
        )
        _ = [event async for event in adapter.run_turn("inspect", cwd=tmp_path)]

        assert platform.process is not None
        start = next(item for item in platform.process.stdin.messages if item.get("method") == "thread/start")
        assert start["params"]["sandbox"] == "danger-full-access"
        assert start["params"]["approvalPolicy"] == "on-request"

    asyncio.run(scenario())


def test_codex_auto_review_is_separate_from_file_sandbox(tmp_path: Path) -> None:
    async def scenario() -> None:
        platform = FakePlatform()
        adapter = CodexAdapter(
            platform,
            executable="codex",
            file_access=FileAccessMode.PROJECT_ONLY,
            approval_mode=ApprovalMode.AUTO,
        )
        _ = [event async for event in adapter.run_turn("inspect", cwd=tmp_path)]

        assert platform.process is not None
        start = next(item for item in platform.process.stdin.messages if item.get("method") == "thread/start")
        assert start["params"]["sandbox"] == "workspace-write"
        assert start["params"]["approvalPolicy"] == "on-request"
        assert start["params"]["approvalsReviewer"] == "auto_review"

    asyncio.run(scenario())


def test_codex_reads_account_usage(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = CodexAdapter(FakePlatform(), executable="codex")
        usage = await adapter.account_rate_limits(tmp_path)
        assert usage["ordinaryUsageAllowed"] is True
        assert usage["rateLimits"]["primary"]["usedPercent"] == 25

    asyncio.run(scenario())


def test_codex_lists_existing_sessions(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = CodexAdapter(FakePlatform(), executable="codex")
        sessions = await adapter.list_sessions(cwd=tmp_path)

        assert len(sessions) == 1
        assert sessions[0].id == "external-thread"
        assert sessions[0].cwd == tmp_path
        assert not sessions[0].active

    asyncio.run(scenario())


def test_codex_maps_file_read_and_search_progress(tmp_path: Path) -> None:
    read = _codex_progress_event(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "read-1",
                    "type": "commandExecution",
                    "command": "sed -n '1,20p' docs/envs/backend.md",
                    "commandActions": [
                        {
                            "type": "read",
                            "command": "sed -n '1,20p' docs/envs/backend.md",
                            "name": "backend.md",
                            "path": str(tmp_path / "docs/envs/backend.md"),
                        }
                    ],
                    "cwd": str(tmp_path),
                    "status": "inProgress",
                }
            },
        },
        tmp_path,
    )
    search = _codex_progress_event(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "search-1",
                    "type": "commandExecution",
                    "command": "rg needle",
                    "commandActions": [{"type": "search", "command": "rg needle", "path": None, "query": "needle"}],
                    "cwd": str(tmp_path),
                    "status": "inProgress",
                }
            },
        },
        tmp_path,
    )

    assert read and read.text == "Читает docs/envs/backend.md…"
    assert search and search.text == "Ищет по проекту…"


def test_codex_maps_file_change_without_patch_content(tmp_path: Path) -> None:
    event = _codex_progress_event(
        {
            "method": "item/fileChange/patchUpdated",
            "params": {
                "itemId": "patch-1",
                "changes": [
                    {
                        "path": str(tmp_path / "docs/envs/backend.md"),
                        "kind": {"type": "update"},
                        "diff": "-TOKEN=old\n+TOKEN=secret-value",
                    }
                ],
            },
        },
        tmp_path,
    )

    assert event and event.text == "Изменяет docs/envs/backend.md…"
    assert "TOKEN" not in event.text
    assert "diff" not in event.payload

    completed = _codex_progress_event(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "patch-1",
                    "type": "fileChange",
                    "status": "completed",
                    "changes": [{"path": str(tmp_path / "docs/envs/backend.md"), "diff": "private"}],
                }
            },
        },
        tmp_path,
    )
    assert completed and completed.text == "Изменение docs/envs/backend.md завершено."


def test_codex_maps_test_command_start_and_completion_without_command(tmp_path: Path) -> None:
    item = {
        "id": "cmd-1",
        "type": "commandExecution",
        "command": "API_TOKEN=top-secret python -m pytest tests/test_api.py -q",
        "commandActions": [{"type": "unknown", "command": "private command"}],
        "cwd": str(tmp_path),
        "status": "inProgress",
    }
    started = _codex_progress_event(
        {"method": "item/started", "params": {"item": item}},
        tmp_path,
    )
    completed = _codex_progress_event(
        {"method": "item/completed", "params": {"item": {**item, "status": "completed", "exitCode": 0}}},
        tmp_path,
    )

    assert started and started.text == "Запускает тесты…"
    assert completed and completed.text == "Тесты завершены."
    assert "pytest" not in started.text
    assert "API_TOKEN" not in started.text
    assert set(started.payload) == {"method", "item_id"}


def test_codex_safely_shortens_external_path_and_redacts_secret(tmp_path: Path) -> None:
    secret_path = tmp_path / ("token=" + "a" * 40) / "config.toml"
    redacted = _codex_progress_event(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "read-secret",
                    "type": "commandExecution",
                    "command": "private",
                    "commandActions": [
                        {"type": "read", "command": "private", "name": "config", "path": str(secret_path)}
                    ],
                    "cwd": str(tmp_path),
                    "status": "inProgress",
                }
            },
        },
        tmp_path,
    )
    external = _codex_progress_event(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "read-external",
                    "type": "commandExecution",
                    "command": "private",
                    "commandActions": [
                        {
                            "type": "read",
                            "command": "private",
                            "name": "secrets.env",
                            "path": "/very/long/private/home/location/that/is/outside/the/project/secrets.env",
                        }
                    ],
                    "cwd": str(tmp_path),
                    "status": "inProgress",
                }
            },
        },
        tmp_path,
    )

    assert redacted and "***" in redacted.text and "a" * 20 not in redacted.text
    assert external and "/very/long/private/home" not in external.text
    assert "…/project/secrets.env" in external.text


def test_codex_maps_mcp_and_ignores_unknown_notification(tmp_path: Path) -> None:
    mcp = _codex_progress_event(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "mcp-1",
                    "type": "mcpToolCall",
                    "server": "github",
                    "tool": "search",
                    "arguments": {"token": "do-not-show"},
                    "status": "inProgress",
                }
            },
        },
        tmp_path,
    )
    unknown = _codex_progress_event(
        {
            "method": "item/started",
            "params": {"item": {"id": "future-1", "type": "futureTool", "content": "do-not-show"}},
        },
        tmp_path,
    )

    assert mcp and mcp.text == "Выполняет MCP-вызов…"
    assert "arguments" not in mcp.payload
    assert unknown is None
