import asyncio
import json
from pathlib import Path

from ai_control.agents.claude import ClaudeAdapter
from ai_control.core.models import ApprovalMode, FileAccessMode
from ai_control.platform.base import ManagedProcess, PlatformAdapter
from ai_control.security.auth import valid_callback_data


class FakeWriter:
    def __init__(self, output: asyncio.StreamReader) -> None:
        self.output = output
        self.messages: list[dict[str, object]] = []

    def write(self, data: bytes) -> None:
        message = json.loads(data)
        self.messages.append(message)
        if message.get("type") == "control_request":
            request_id = message["request_id"]
            response = {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": {}},
            }
            self.output.feed_data((json.dumps(response) + "\n").encode())
        elif message.get("type") == "user":
            self.output.feed_data(
                b'{"type":"result","result":"done","is_error":false,'
                b'"usage":{"input_tokens":12,"output_tokens":4},"total_cost_usd":0.01}\n'
            )
            self.output.feed_eof()

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


class PermissionWriter(FakeWriter):
    def write(self, data: bytes) -> None:
        message = json.loads(data)
        self.messages.append(message)
        if message.get("type") == "control_request":
            request_id = message["request_id"]
            response = {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": {}},
            }
            self.output.feed_data((json.dumps(response) + "\n").encode())
        elif message.get("type") == "user":
            path = Path(message["message"]["content"].split("TARGET=", 1)[1])
            assistant = {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-write-1",
                            "name": "Write",
                            "input": {"file_path": str(path), "content": "approved\n"},
                        }
                    ]
                },
            }
            denial = {
                "type": "system",
                "subtype": "permission_denied",
                "tool_name": "Write",
                "tool_use_id": "tool-write-1",
            }
            result = {"type": "result", "result": "not written", "is_error": False}
            for item in (assistant, denial, result):
                self.output.feed_data((json.dumps(item) + "\n").encode())
            self.output.feed_eof()


class PermissionProcess(FakeProcess):
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = PermissionWriter(self.stdout)
        self.pid = 123
        self.returncode = None


class PermissionPlatform(FakePlatform):
    async def spawn(self, argv, *, cwd, env=None):  # type: ignore[no-untyped-def]
        self.process = PermissionProcess()
        return self.process  # type: ignore[return-value]


def test_claude_initializes_control_channel_before_user_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        platform = FakePlatform()
        adapter = ClaudeAdapter(platform, executable="claude", model="sonnet")

        events = [event async for event in adapter.run_turn("hello", cwd=tmp_path)]

        assert platform.process is not None
        messages = platform.process.stdin.messages
        assert messages[0]["type"] == "control_request"
        assert messages[0]["request"] == {"subtype": "initialize", "hooks": None}
        assert messages[1]["type"] == "user"
        assert platform.argv is not None
        assert platform.argv[platform.argv.index("--model") + 1] == "sonnet"
        assert "--restricted" in platform.argv
        assert any(event.kind == "final" and event.text == "done" for event in events)
        assert any(event.kind == "usage" and '"input_tokens":12' in event.text for event in events)
        assert events[-1].kind == "completed"

    asyncio.run(scenario())


def test_claude_read_only_uses_plan_and_restricted_mode(tmp_path: Path) -> None:
    async def scenario() -> None:
        platform = FakePlatform()
        adapter = ClaudeAdapter(
            platform,
            executable="claude",
            file_access=FileAccessMode.READ_ONLY,
        )
        _ = [event async for event in adapter.run_turn("inspect", cwd=tmp_path)]

        assert platform.argv is not None
        assert platform.argv[platform.argv.index("--permission-mode") + 1] == "plan"
        assert "--restricted" in platform.argv

    asyncio.run(scenario())


def test_claude_auto_review_keeps_restricted_mode(tmp_path: Path) -> None:
    async def scenario() -> None:
        platform = FakePlatform()
        adapter = ClaudeAdapter(
            platform,
            executable="claude",
            approval_mode=ApprovalMode.AUTO,
        )
        _ = [event async for event in adapter.run_turn("inspect", cwd=tmp_path)]

        assert platform.argv is not None
        assert platform.argv[platform.argv.index("--permission-mode") + 1] == "auto"
        assert "--restricted" in platform.argv

    asyncio.run(scenario())


def test_claude_fallback_write_waits_for_approval(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ClaudeAdapter(PermissionPlatform(), executable="claude")
        target = tmp_path / "created.py"
        events = adapter.run_turn(f"TARGET={target}", cwd=tmp_path)

        assert (await anext(events)).kind == "session"
        assert (await anext(events)).kind == "progress"
        approval = await anext(events)
        assert approval.kind == "approval"
        assert approval.payload["input"]["file_path"] == str(target)
        assert valid_callback_data(f"approve:2:{approval.payload['request_id']}")
        assert not target.exists()

        next_event = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        await adapter.answer_approval(approval.payload["request_id"], "accept")
        assert (await next_event).kind == "progress"
        remaining = [event async for event in events]

        assert target.read_text(encoding="utf-8") == "approved\n"
        assert remaining[-2].kind == "final"
        assert "created.py" in remaining[-2].text
        assert remaining[-1].kind == "completed"

    asyncio.run(scenario())


def test_claude_rejects_write_outside_project_without_approval(tmp_path: Path) -> None:
    async def scenario() -> None:
        project = tmp_path / "project"
        project.mkdir()
        target = tmp_path / "outside.py"
        adapter = ClaudeAdapter(PermissionPlatform(), executable="claude")

        events = [event async for event in adapter.run_turn(f"TARGET={target}", cwd=project)]

        assert any(event.kind == "policy_denied" for event in events)
        assert not any(event.kind == "approval" for event in events)
        assert not target.exists()

    asyncio.run(scenario())


def test_claude_permission_policy_only_allows_scoped_file_changes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = ClaudeAdapter(FakePlatform(), executable="claude")
    adapter._cwd = project

    assert adapter._permission_allowed(
        {"tool_name": "Write", "input": {"file_path": str(project / "inside.py")}}
    )
    assert not adapter._permission_allowed(
        {"tool_name": "Write", "input": {"file_path": str(tmp_path / "outside.py")}}
    )
    assert not adapter._permission_allowed({"tool_name": "Bash", "input": {"command": "whoami"}})

    read_only = ClaudeAdapter(
        FakePlatform(),
        executable="claude",
        file_access=FileAccessMode.READ_ONLY,
    )
    read_only._cwd = project
    assert not read_only._permission_allowed(
        {"tool_name": "Write", "input": {"file_path": str(project / "inside.py")}}
    )


class SessionListProcess(FakeProcess):
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeWriter(self.stdout)
        self.stdout.feed_data(b"[]")
        self.stdout.feed_eof()
        self.pid = 123
        self.returncode = 0


class SessionListPlatform(FakePlatform):
    async def spawn(self, argv, *, cwd, env=None):  # type: ignore[no-untyped-def]
        self.process = SessionListProcess()
        return self.process  # type: ignore[return-value]


def test_claude_lists_local_sessions(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        config_dir = tmp_path / "claude"
        history_dir = config_dir / "projects" / "project-key"
        history_dir.mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        session_id = "12345678-1234-1234-1234-123456789abc"
        item = {
            "type": "user",
            "sessionId": session_id,
            "cwd": str(project),
            "message": {"content": "Existing Claude task"},
        }
        (history_dir / f"{session_id}.jsonl").write_text(json.dumps(item) + "\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        adapter = ClaudeAdapter(SessionListPlatform(), executable="claude")
        sessions = await adapter.list_sessions(cwd=project)

        assert len(sessions) == 1
        assert sessions[0].id == session_id
        assert sessions[0].title == "Existing Claude task"
        assert sessions[0].cwd == project

    asyncio.run(scenario())
