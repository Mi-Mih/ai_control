import asyncio
import json
from pathlib import Path
from typing import Any

from ai_control.agents.codex import CodexAdapter
from ai_control.core.models import ApprovalMode, FileAccessMode
from ai_control.platform.base import ManagedProcess, PlatformAdapter


class FakeWriter:
    def __init__(self, output: asyncio.StreamReader) -> None:
        self.output = output
        self.messages: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        message = json.loads(data)
        self.messages.append(message)
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None:
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
            self.output.feed_data(b'{"method":"item/agentMessage/delta","params":{"delta":"hello"}}\n')
            self.output.feed_data(b'{"method":"turn/completed","params":{"status":"completed"}}\n')

    async def drain(self) -> None:
        return None


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeWriter(self.stdout)
        self.pid = 123
        self.returncode = None

    async def wait(self) -> int:
        return 0


class FakePlatform(PlatformAdapter):
    def __init__(self) -> None:
        self.process: FakeProcess | None = None

    async def spawn(self, argv, *, cwd, env=None):  # type: ignore[no-untyped-def]
        self.process = FakeProcess()
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
        assert events[-1].kind == "completed"
        assert platform.process is not None
        resume = next(item for item in platform.process.stdin.messages if item.get("method") == "thread/resume")
        turn = next(item for item in platform.process.stdin.messages if item.get("method") == "turn/start")
        assert resume["params"]["model"] == "gpt-test"
        assert turn["params"]["model"] == "gpt-test"
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
