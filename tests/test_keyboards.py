from pathlib import Path

from ai_control.bot.keyboards import task_actions, task_list
from ai_control.core.models import AgentKind, TaskRecord, TaskStatus


def _task(task_id: int, status: TaskStatus) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        project_id="project",
        user_id=1,
        agent=AgentKind.CODEX,
        status=status,
        checkout_path=Path("project"),
        prompt="test",
    )


def test_task_list_uses_two_compact_localized_buttons_per_row() -> None:
    keyboard = task_list(
        [
            _task(1, TaskStatus.COMPLETED),
            _task(2, TaskStatus.STOPPED),
            _task(3, TaskStatus.RUNNING),
        ]
    )

    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["#1 ✅ готова", "#2 ⏹ остановлена"],
        ["#3 ▶️ запущена"],
        ["Назад"],
    ]
    assert [[button.callback_data for button in row] for row in keyboard.inline_keyboard] == [
        ["task:1", "task:2"],
        ["task:3"],
        ["menu:main"],
    ]


def test_task_list_with_no_tasks_only_shows_back_button() -> None:
    keyboard = task_list([])

    assert [button.text for button in keyboard.inline_keyboard[0]] == ["Назад"]


def test_task_list_adds_pagination_controls() -> None:
    keyboard = task_list([_task(11, TaskStatus.COMPLETED)], page=1, total_pages=3)

    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["#11 ✅ готова"],
        ["⬅️ Предыдущие", "Следующие ➡️"],
        ["Назад"],
    ]
    assert [button.callback_data for button in keyboard.inline_keyboard[1]] == ["tasks:0", "tasks:2"]


def test_task_actions_can_offer_full_result_on_demand() -> None:
    keyboard = task_actions(19, show_result=True)

    buttons = [button for row in keyboard.inline_keyboard for button in row]
    result = next(button for button in buttons if button.callback_data == "result:19")
    assert result.text == "Полный результат"
