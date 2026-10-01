from __future__ import annotations

from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ai_control.core.models import (
    AgentKind,
    AgentSession,
    ApprovalMode,
    FileAccessMode,
    Project,
    TaskRecord,
    TaskStatus,
)

TASK_STATUS_LABELS = {
    TaskStatus.PENDING: "🕒 ожидает",
    TaskStatus.RUNNING: "▶️ запущена",
    TaskStatus.WAITING_APPROVAL: "⏳ ждёт",
    TaskStatus.STOPPED: "⏹ остановлена",
    TaskStatus.COMPLETED: "✅ готова",
    TaskStatus.FAILED: "❌ ошибка",
    TaskStatus.LOST: "⚠️ потеряна",
    TaskStatus.CLOSED: "🔒 закрыта",
}
TASKS_PER_PAGE = 10


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Проекты", callback_data="menu:projects")],
            [InlineKeyboardButton(text="Активные задачи", callback_data="menu:tasks")],
            [InlineKeyboardButton(text="Новая задача", callback_data="menu:new")],
            [InlineKeyboardButton(text="Подхватить сессию", callback_data="menu:import")],
            [InlineKeyboardButton(text="Файлы", callback_data="menu:files")],
            [InlineKeyboardButton(text="Состояние компьютера", callback_data="menu:health")],
            [InlineKeyboardButton(text="Настройки", callback_data="menu:settings")],
        ]
    )


def project_list(projects: list[Project], prefix: str = "project") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=item.name, callback_data=f"{prefix}:{item.id}")] for item in projects]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def agent_list(project: Project, prefix: str = "agent") -> InlineKeyboardMarkup:
    labels = {AgentKind.CODEX: "Codex", AgentKind.CLAUDE: "Claude", AgentKind.CURSOR: "Cursor"}
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=labels[agent], callback_data=f"{prefix}:{agent.value}")]
            for agent in project.agents
        ]
    )


def session_list(sessions: list[AgentSession]) -> InlineKeyboardMarkup:
    rows = []
    for index, session in enumerate(sessions):
        if session.imported_task_id is not None:
            status = "✅ "
        elif session.active:
            status = "🔴 "
        else:
            status = ""
        timestamp = session.updated_at.astimezone().strftime("%d.%m %H:%M") if session.updated_at else ""
        label = f"{status}{session.title[:38]}"
        if timestamp:
            label += f" · {timestamp}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"importsession:{index}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def checkout_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Текущий checkout", callback_data="checkout:current")],
            [InlineKeyboardButton(text="Изолированный worktree", callback_data="checkout:isolated")],
        ]
    )


def model_choice(
    models: tuple[str, ...],
    *,
    default_model: str | None = None,
    task_id: int | None = None,
    current_model: str | None = None,
) -> InlineKeyboardMarkup:
    rows = []
    for index, model in enumerate(models):
        label = model
        if model == current_model:
            label = "✓ " + label
        elif model == default_model:
            label += " · по умолчанию"
        callback = f"setmodel:{task_id}:{index}" if task_id is not None else f"model:{index}"
        rows.append([InlineKeyboardButton(text=label, callback_data=callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def task_list(
    tasks: list[TaskRecord],
    *,
    page: int = 0,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"#{task.id} {TASK_STATUS_LABELS[task.status]}",
            callback_data=f"task:{task.id}",
        )
        for task in tasks
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️ Предыдущие", callback_data=f"tasks:{page - 1}"))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(text="Следующие ➡️", callback_data=f"tasks:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def task_actions(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Отправить сообщение", callback_data=f"continue:{task_id}"),
                InlineKeyboardButton(text="Остановить", callback_data=f"stop:{task_id}"),
            ],
            [
                InlineKeyboardButton(text="Статус", callback_data=f"task:{task_id}"),
                InlineKeyboardButton(text="Git diff", callback_data=f"diff:{task_id}"),
            ],
            [
                InlineKeyboardButton(text="Файлы", callback_data=f"files:{task_id}"),
                InlineKeyboardButton(text="Загрузить", callback_data=f"upload:{task_id}"),
            ],
            [
                InlineKeyboardButton(text="Сменить модель", callback_data=f"taskmodel:{task_id}"),
                InlineKeyboardButton(text="Доступ", callback_data=f"taskaccess:{task_id}"),
            ],
            [
                InlineKeyboardButton(text="Подтверждения", callback_data=f"taskapproval:{task_id}"),
            ],
            [
                InlineKeyboardButton(text="Закрыть задачу", callback_data=f"close:{task_id}"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu:tasks")],
        ]
    )


def access_choice(
    task_id: int,
    current: FileAccessMode,
    modes: tuple[FileAccessMode, ...],
) -> InlineKeyboardMarkup:
    labels = {
        FileAccessMode.READ_ONLY: "Анализ без записи",
        FileAccessMode.PROJECT_ONLY: "Запись только в проект",
        FileAccessMode.APPROVED_PATHS: "Запись: проект + разрешённые пути",
        FileAccessMode.FULL_ACCESS: "Полный доступ (только локально)",
    }
    rows = []
    for mode in modes:
        label = labels[mode]
        if mode == current:
            label = "✓ " + label
        rows.append([InlineKeyboardButton(text=label, callback_data=f"setaccess:{task_id}:{mode.value}")])
    rows.append([InlineKeyboardButton(text="Назад к задаче", callback_data=f"task:{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def approval_choice(task_id: int, current: ApprovalMode) -> InlineKeyboardMarkup:
    labels = {
        ApprovalMode.MANUAL: "Ручные подтверждения",
        ApprovalMode.AUTO: "Автопроверка",
    }
    rows = []
    for mode in ApprovalMode:
        label = labels[mode]
        if mode == current:
            label = "✓ " + label
        rows.append([InlineKeyboardButton(text=label, callback_data=f"setapproval:{task_id}:{mode.value}")])
    rows.append([InlineKeyboardButton(text="Назад к задаче", callback_data=f"task:{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def approval(task_id: int, request_id: str) -> InlineKeyboardMarkup:
    token = request_id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Разрешить один раз", callback_data=f"approve:{task_id}:{token}")],
            [InlineKeyboardButton(text="Запретить", callback_data=f"deny:{task_id}:{token}")],
            [InlineKeyboardButton(text="Попросить объяснить", callback_data=f"explain:{task_id}:{token}")],
        ]
    )


def file_list(files: list[dict[str, object]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=Path(str(item["path"])).name[:45],
                callback_data=f"sendfile:{int(item['id'])}",
            )
        ]
        for item in files
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def export_approval(file_id: int, permission_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отправить один раз",
                    callback_data=f"export:{file_id}:{permission_id}:allow",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Запретить",
                    callback_data=f"export:{file_id}:{permission_id}:deny",
                )
            ],
        ]
    )
