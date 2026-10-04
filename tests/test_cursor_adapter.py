import asyncio
import json
from pathlib import Path
from typing import Any

from ai_control.agents.cursor import CursorAdapter
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
        if method in {"initialize", "authenticate"}:
            result: dict[str, Any] = {}
        elif method == "session/new":
            result = {"sessionId": "cursor-session-1"}
        elif method == "session/load":
            result = {"sessionId": message["params"]["sessionId"]}
        elif method == "session/list":
            result = {
                "sessions": [
                    {
                        "sessionId": "external-cursor-session",
                        "title": "Existing Cursor task",
                        "cwd": message["params"]["cwd"],
                        "updatedAt": "2026-10-01T10:00:00Z",
                    }
                ]
            }
        elif method == "session/prompt":
            update = {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": message["params"]["sessionId"],
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}},
                },
            }
            self.output.feed_data((json.dumps(update) + "\n").encode())
            result = {"stopReason": "end_turn"}
        else:
            result = {}
        self.output.feed_data((json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n").encode())

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
        self.argv: tuple[str, ...] | None = None

    async def spawn(self, argv, *, cwd, env=None):  # type: ignore[no-untyped-def]
        self.argv = tuple(argv)
        self.process = FakeProcess()
        return self.process  # type: ignore[return-value]

    async def terminate_tree(self, process: ManagedProcess, grace_seconds: float = 5) -> None:
        process.returncode = 0  # type: ignore[misc]


class ResponseRpc:
    def __init__(self) -> None:
        self.responses: list[tuple[object, dict[str, Any]]] = []

    async def respond(self, request_id: object, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))


def test_cursor_acp_protocol_model_and_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        platform = FakePlatform()
        adapter = CursorAdapter(platform, executable="agent", model="composer-2.5")
        events = [
            event async for event in adapter.run_turn("hello", cwd=tmp_path, session_id="existing-session")
        ]

        assert events[0].session_id == "existing-session"
        assert any(event.kind == "text_delta" and event.text == "hello" for event in events)
        assert any(event.kind == "final" and event.text == "hello" for event in events)
        assert events[-1].kind == "completed"
        assert platform.argv == ("agent", "--model", "composer-2.5", "--sandbox", "enabled", "acp")
        assert platform.process is not None
        messages = platform.process.stdin.messages
        assert all(message.get("jsonrpc") == "2.0" for message in messages)
        assert any(message.get("method") == "session/load" for message in messages)
        await adapter.stop()

    asyncio.run(scenario())


def test_cursor_combines_message_chunks_into_final_answer(tmp_path: Path) -> None:
    class ChunkedWriter(FakeWriter):
        def write(self, data: bytes) -> None:
            message = json.loads(data)
            if message.get("method") != "session/prompt":
                super().write(data)
                return

            self.messages.append(message)
            for text in ("Привет", ", мир!"):
                update = {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": message["params"]["sessionId"],
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": text},
                        },
                    },
                }
                self.output.feed_data((json.dumps(update) + "\n").encode())
            result = {"stopReason": "end_turn"}
            self.output.feed_data(
                (json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n").encode()
            )

    class ChunkedProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stdin = ChunkedWriter(self.stdout)

    class ChunkedPlatform(FakePlatform):
        async def spawn(self, argv, *, cwd, env=None):  # type: ignore[no-untyped-def]
            self.argv = tuple(argv)
            self.process = ChunkedProcess()
            return self.process  # type: ignore[return-value]

    async def scenario() -> None:
        adapter = CursorAdapter(ChunkedPlatform(), executable="agent")

        events = [event async for event in adapter.run_turn("hello", cwd=tmp_path)]

        assert [event.text for event in events if event.kind == "text_delta"] == ["Привет", ", мир!"]
        assert [event.text for event in events if event.kind == "final"] == ["Привет, мир!"]
        assert events[-1].kind == "completed"
        await adapter.stop()

    asyncio.run(scenario())


def test_cursor_read_only_uses_plan_mode_and_sandbox(tmp_path: Path) -> None:
    adapter = CursorAdapter(
        FakePlatform(),
        executable="agent",
        file_access=FileAccessMode.READ_ONLY,
    )

    assert adapter._argv() == ("agent", "--mode", "plan", "--sandbox", "enabled", "acp")


def test_cursor_auto_review_keeps_sandbox_enabled() -> None:
    adapter = CursorAdapter(
        FakePlatform(),
        executable="agent",
        approval_mode=ApprovalMode.AUTO,
    )

    assert adapter._argv() == ("agent", "--auto-review", "--sandbox", "enabled", "acp")


def test_cursor_rejects_permission_escalation_in_project_mode() -> None:
    async def scenario() -> None:
        adapter = CursorAdapter(FakePlatform(), executable="agent")
        rpc = ResponseRpc()
        adapter.rpc = rpc  # type: ignore[assignment]
        request = {
            "id": 7,
            "method": "session/request_permission",
            "params": {
                "options": [
                    {"optionId": "allow-once", "kind": "allow_once"},
                    {"optionId": "reject-once", "kind": "reject_once"},
                ]
            },
        }

        events = [event async for event in adapter._handle_request(request)]

        assert [event.kind for event in events] == ["policy_denied"]
        assert rpc.responses == [(7, {"outcome": {"outcome": "selected", "optionId": "reject-once"}})]
        assert not adapter._approval_requests

    asyncio.run(scenario())


def test_cursor_lists_existing_sessions(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = CursorAdapter(FakePlatform(), executable="agent")
        sessions = await adapter.list_sessions(cwd=tmp_path)

        assert len(sessions) == 1
        assert sessions[0].id == "external-cursor-session"
        assert sessions[0].title == "Existing Cursor task"
        assert sessions[0].cwd == tmp_path

    asyncio.run(scenario())
