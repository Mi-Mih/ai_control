import asyncio
from pathlib import Path
from typing import Any

from ai_control.bot.progress import ProgressReporter
from ai_control.config.models import AppConfig
from ai_control.core.models import AgentEvent, AgentKind, TaskRecord, TaskStatus


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.messages: list[str] = []

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.messages.append(text)


def test_progress_reporter_separates_and_deduplicates_stage(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig.model_validate(
            {
                "instance": {"name": "Test", "data_dir": tmp_path},
                "telegram": {"allowed_user_ids": [42], "progress_interval_seconds": 1},
            }
        )
        bot = FakeBot()
        reporter = ProgressReporter(bot, config)  # type: ignore[arg-type]
        task = TaskRecord(
            id=1,
            project_id="project",
            user_id=42,
            agent=AgentKind.CODEX,
            status=TaskStatus.RUNNING,
            checkout_path=tmp_path,
            prompt="test",
        )
        reporter.bind(task.id, 100, 200)

        await reporter(task, AgentEvent("progress", "Читает файлы…"))
        await reporter(task, AgentEvent("progress", "Читает файлы…"))
        await reporter(task, AgentEvent("status", "Codex turn started"))
        await reporter(task, AgentEvent("text_delta", "Длинный промежуточный лог"))
        await reporter(task, AgentEvent("final", "Готово"))
        await reporter(task, AgentEvent("completed"))

        state = reporter.states[task.id]
        assert state.response_text == "Готово"
        assert state.stage == "Codex turn started"
        assert len(bot.edits) == 3
        assert "Этап: Читает файлы…" in bot.edits[0]
        assert "Последняя активность:" in bot.edits[0]
        assert "<b>Итог:</b>\nГотово" in bot.edits[-1]
        assert "Длинный промежуточный лог" not in bot.edits[-1]
        assert "Codex turn startedГотово" not in bot.edits[-1]

    asyncio.run(scenario())


def test_progress_reporter_does_not_append_repeated_progress_to_answer(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig.model_validate(
            {
                "instance": {"name": "Test", "data_dir": tmp_path},
                "telegram": {"allowed_user_ids": [42]},
            }
        )
        reporter = ProgressReporter(FakeBot(), config)  # type: ignore[arg-type]
        task = TaskRecord(
            id=2,
            project_id="project",
            user_id=42,
            agent=AgentKind.CODEX,
            status=TaskStatus.RUNNING,
            checkout_path=tmp_path,
            prompt="test",
        )
        reporter.bind(task.id, 100, 201)

        for _ in range(5):
            await reporter(task, AgentEvent("progress", "Запускает тесты…"))

        state = reporter.states[task.id]
        assert state.stage == "Запускает тесты…"
        assert state.response_text == ""

    asyncio.run(scenario())


def test_approval_card_does_not_expose_command_or_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig.model_validate(
            {
                "instance": {"name": "Test", "data_dir": tmp_path},
                "telegram": {"allowed_user_ids": [42]},
            }
        )
        bot = FakeBot()
        reporter = ProgressReporter(bot, config)  # type: ignore[arg-type]
        task = TaskRecord(
            id=3,
            project_id="project",
            user_id=42,
            agent=AgentKind.CODEX,
            status=TaskStatus.WAITING_APPROVAL,
            checkout_path=tmp_path,
            prompt="test",
        )
        reporter.bind(task.id, 100, 202)

        await reporter(
            task,
            AgentEvent(
                "approval",
                "Bash",
                payload={
                    "request_id": "request-1",
                    "tool_name": "Bash",
                    "input": {
                        "file_path": str(tmp_path / "docs/config.md"),
                        "command": "env API_TOKEN=do-not-show deploy --force",
                        "content": "private file contents",
                    },
                },
            ),
        )

        assert len(bot.messages) == 1
        assert "Файл: docs/config.md" in bot.messages[0]
        assert "deploy --force" not in bot.messages[0]
        assert "private file contents" not in bot.messages[0]
        assert "do-not-show" not in bot.messages[0]

    asyncio.run(scenario())
