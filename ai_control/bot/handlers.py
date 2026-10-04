from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from ai_control.bot.commands import execute_task_command
from ai_control.bot.keyboards import (
    TASKS_PER_PAGE,
    access_choice,
    agent_list,
    approval_choice,
    checkout_choice,
    export_approval,
    file_list,
    main_menu,
    model_choice,
    project_list,
    session_list,
    task_actions,
    task_list,
)
from ai_control.bot.progress import ProgressReporter, send_markdown
from ai_control.config.models import AppConfig
from ai_control.core.models import AgentKind, ApprovalMode, FileAccessMode, TaskRecord, TaskStatus
from ai_control.files.policy import ExportPolicy, is_sensitive, safe_inbox_destination
from ai_control.files.service import file_sha256
from ai_control.git import GitService
from ai_control.permissions import PermissionService
from ai_control.projects import ProjectRegistry
from ai_control.security.redaction import redact
from ai_control.sessions import TaskManager
from ai_control.sessions.manager import TaskManagerError
from ai_control.storage import Database

CALLBACK_RE = re.compile(r"^[a-z]+:[A-Za-z0-9._~/-]{1,48}(?::[A-Za-z0-9._~-]{1,24})?$")
logger = logging.getLogger(__name__)


async def answer_callback(
    query: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool | None = None,
) -> None:
    """Acknowledge callbacks without aborting a flow for an expired Telegram query."""
    try:
        await query.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        detail = str(exc).casefold()
        if "query is too old" not in detail and "query id is invalid" not in detail:
            raise
        logger.info("Ignoring expired callback query data=%r", query.data)


def panic_confirmed(text: str) -> bool:
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        return False
    command = parts[0].split("@", 1)[0].casefold()
    return command == "/panic" and parts[1].strip() == "STOP"


class NewTask(StatesGroup):
    project = State()
    agent = State()
    model = State()
    checkout = State()
    prompt = State()
    continue_prompt = State()
    upload = State()


class ImportSession(StatesGroup):
    project = State()
    agent = State()
    session = State()


def create_router(
    config: AppConfig,
    database: Database,
    projects: ProjectRegistry,
    tasks: TaskManager,
    git: GitService,
    reporter: ProgressReporter,
    request_shutdown: Callable[[], Awaitable[None]],
) -> Router:
    router = Router()
    permissions = PermissionService(database)

    async def task_page(user_id: int, requested_page: int) -> tuple[list[TaskRecord], int, int]:
        total = await tasks.count_active(user_id)
        total_pages = max(1, (total + TASKS_PER_PAGE - 1) // TASKS_PER_PAGE)
        page = min(max(requested_page, 0), total_pages - 1)
        items = await tasks.list_active(user_id, limit=TASKS_PER_PAGE, offset=page * TASKS_PER_PAGE)
        return items, page, total_pages

    async def show_menu(target: Message) -> None:
        await target.answer(f"<b>AI Control — {html.escape(config.instance.name)}</b>", reply_markup=main_menu())

    async def answer_task_command(message: Message, state: FSMContext, record: TaskRecord) -> bool:
        try:
            result = await execute_task_command(
                message.text or "",
                record=record,
                user_id=message.from_user.id,
                tasks=tasks,
                database=database,
                git=git,
            )
        except Exception as exc:
            await message.answer("Не удалось выполнить команду: " + html.escape(str(exc)))
            return True
        if result is None:
            return False
        for start in range(0, len(result), 3500):
            await message.answer("<pre>" + html.escape(result[start : start + 3500]) + "</pre>")
        await state.set_data({"active_task_id": record.id})
        return True

    async def show_task_access(target: Message, record: TaskRecord) -> None:
        labels = {
            FileAccessMode.READ_ONLY: "анализ без записи",
            FileAccessMode.PROJECT_ONLY: "запись только внутри проекта",
            FileAccessMode.APPROVED_PATHS: "запись в проекте и разрешённых путях",
            FileAccessMode.FULL_ACCESS: "полный доступ (задан локально)",
        }
        modes = await tasks.available_access_modes(record.id, record.user_id)
        await target.answer(
            f"<b>Доступ задачи #{record.id}</b>\n"
            f"Текущий режим: {labels[record.access_mode]}\n\n"
            "Режим ограничивает изменения. Чтение вне проекта зависит от песочницы CLI.\n"
            "Запросы на выход за выбранные границы отклоняются автоматически. "
            "Telegram не может включить полный доступ.",
            reply_markup=access_choice(record.id, record.access_mode, modes),
        )

    async def show_task_approvals(target: Message, record: TaskRecord) -> None:
        labels = {
            ApprovalMode.MANUAL: "ручные подтверждения",
            ApprovalMode.AUTO: "автоматическая проверка",
        }
        await target.answer(
            f"<b>Подтверждения задачи #{record.id}</b>\n"
            f"Текущий режим: {labels[record.approval_mode]}\n\n"
            "Эта настройка не расширяет файловый доступ. Автопроверка может разрешать только то, "
            "что допускает выбранный режим доступа; выход за его границы блокируется.",
            reply_markup=approval_choice(record.id, record.approval_mode),
        )

    @router.message(Command("start", "menu"))
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_menu(message)

    @router.message(Command("panic"))
    async def panic(message: Message, state: FSMContext) -> None:
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("Аварийная остановка доступна только в личном чате.")
            return
        if not panic_confirmed(message.text or ""):
            await message.answer(
                "Для аварийной остановки всех задач и бота отправьте точную команду:\n<code>/panic STOP</code>"
            )
            return
        await state.clear()
        try:
            await database.audit("emergency_shutdown", user_id=message.from_user.id)
        except Exception:
            logger.exception("Cannot record emergency shutdown")
        await message.answer("Аварийная остановка принята. Активные задачи завершаются, бот выключается.")
        await request_shutdown()

    @router.message(Command("access"))
    async def access_command(message: Message, state: FSMContext) -> None:
        assert message.from_user
        data = await state.get_data()
        task_id = data.get("active_task_id")
        if not isinstance(task_id, int):
            await message.answer("Сначала откройте нужную задачу через список активных задач.")
            return
        record = await tasks.get(task_id)
        if not record or record.user_id != message.from_user.id:
            await message.answer("Задача не найдена.")
            return
        await show_task_access(message, record)

    @router.message(Command("approvals"))
    async def approvals_command(message: Message, state: FSMContext) -> None:
        assert message.from_user
        data = await state.get_data()
        task_id = data.get("active_task_id")
        if not isinstance(task_id, int):
            await message.answer("Сначала откройте нужную задачу через список активных задач.")
            return
        record = await tasks.get(task_id)
        if not record or record.user_id != message.from_user.id:
            await message.answer("Задача не найдена.")
            return
        await show_task_approvals(message, record)

    @router.callback_query(F.data == "menu:main")
    async def menu_callback(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await query.answer()
        assert query.message
        await show_menu(query.message)

    @router.callback_query(F.data == "menu:projects")
    async def show_projects(query: CallbackQuery) -> None:
        await query.answer()
        assert query.message
        items = await projects.list()
        lines = [f"• {html.escape(item.name)} — <code>{html.escape(str(item.path))}</code>" for item in items]
        await query.message.answer("<b>Разрешённые проекты</b>\n" + ("\n".join(lines) or "Нет проектов"))

    @router.callback_query(F.data == "menu:new")
    async def new_task(query: CallbackQuery, state: FSMContext) -> None:
        await answer_callback(query)
        assert query.message
        items = await projects.list()
        if not items:
            await query.message.answer("Нет зарегистрированных проектов. Добавьте их в config.yaml.")
            return
        await state.set_state(NewTask.project)
        await query.message.answer("Выберите проект:", reply_markup=project_list(items, "project"))

    @router.callback_query(F.data == "menu:import")
    async def import_existing_session(query: CallbackQuery, state: FSMContext) -> None:
        await query.answer()
        assert query.message
        items = await projects.list()
        if not items:
            await query.message.answer("Нет зарегистрированных проектов. Добавьте их в config.yaml.")
            return
        await state.set_state(ImportSession.project)
        await query.message.answer(
            "Выберите проект, в котором была запущена сессия:",
            reply_markup=project_list(items, "importproject"),
        )

    @router.callback_query(ImportSession.project, F.data.startswith("importproject:"))
    async def choose_import_project(query: CallbackQuery, state: FSMContext) -> None:
        identifier = (query.data or "").split(":", 1)[1]
        project = await projects.get(identifier)
        if not project:
            await query.answer("Проект не найден", show_alert=True)
            return
        await state.update_data(import_project_id=identifier)
        await state.set_state(ImportSession.agent)
        await query.answer()
        assert query.message
        await query.message.answer("Выберите агента:", reply_markup=agent_list(project, "importagent"))

    @router.callback_query(ImportSession.agent, F.data.startswith("importagent:"))
    async def choose_import_agent(query: CallbackQuery, state: FSMContext) -> None:
        assert query.from_user and query.message
        data = await state.get_data()
        try:
            agent = AgentKind((query.data or "").split(":", 1)[1])
            project_id = str(data["import_project_id"])
            sessions = await tasks.list_importable_sessions(
                project_id=project_id,
                user_id=query.from_user.id,
                agent=agent,
            )
        except (ValueError, KeyError):
            await query.answer("Некорректные данные", show_alert=True)
            return
        except TaskManagerError as exc:
            await query.answer()
            await query.message.answer("Не удалось получить сессии: " + html.escape(str(exc)))
            return
        await query.answer()
        if not sessions:
            await query.message.answer("Сохранённых сессий этого агента в выбранном проекте не найдено.")
            return
        await state.update_data(
            import_agent=agent.value,
            import_sessions=[
                {
                    "id": item.id,
                    "title": item.title,
                    "active": item.active,
                    "imported_task_id": item.imported_task_id,
                }
                for item in sessions
            ],
        )
        await state.set_state(ImportSession.session)
        await query.message.answer(
            "Выберите сессию. ✅ — уже подключена к задаче, 🔴 — занята внешним процессом:",
            reply_markup=session_list(sessions),
        )

    @router.callback_query(ImportSession.session, F.data.regexp(r"^importsession:[0-9]{1,2}$"))
    async def choose_import_session(query: CallbackQuery, state: FSMContext) -> None:
        assert query.from_user and query.message
        data = await state.get_data()
        try:
            index = int((query.data or "").split(":", 1)[1])
            selected = data["import_sessions"][index]
            if selected.get("active") and not selected.get("imported_task_id"):
                await query.answer(
                    "Сессия ещё выполняется вне бота. Завершите её и обновите список.",
                    show_alert=True,
                )
                return
            record = await tasks.import_session(
                project_id=str(data["import_project_id"]),
                user_id=query.from_user.id,
                agent=AgentKind(data["import_agent"]),
                session_id=str(selected["id"]),
            )
        except (ValueError, KeyError, IndexError, TypeError):
            await query.answer("Некорректная или устаревшая кнопка", show_alert=True)
            return
        except TaskManagerError as exc:
            detail = {
                "session is active": "Сессия ещё выполняется вне бота. Завершите её и повторите импорт.",
                "session is unavailable or outside the selected project": (
                    "Сессия больше недоступна или относится к другому проекту."
                ),
            }.get(str(exc), str(exc))
            await query.answer(detail, show_alert=True)
            return
        already_imported = selected.get("imported_task_id") is not None
        await query.answer("Открыта существующая задача" if already_imported else "Сессия подключена")
        await state.clear()
        await state.update_data(active_task_id=record.id)
        await query.message.answer(
            f"<b>{'Сессия уже подключена' if already_imported else 'Сессия подключена'} "
            f"как задача #{record.id}</b>\n"
            f"Агент: {record.agent.value}\n"
            f"Модель для следующих сообщений: {html.escape(record.model or 'по умолчанию')}\n"
            f"Состояние: {record.status.value}\n"
            f"Каталог: <code>{html.escape(str(record.checkout_path))}</code>\n\n"
            "Нажмите «Отправить сообщение», чтобы продолжить эту сессию.",
            reply_markup=task_actions(record.id),
        )

    @router.callback_query(NewTask.project, F.data.startswith("project:"))
    async def choose_project(query: CallbackQuery, state: FSMContext) -> None:
        data = query.data or ""
        if not CALLBACK_RE.fullmatch(data):
            await answer_callback(query, "Некорректные данные", show_alert=True)
            return
        identifier = data.split(":", 1)[1]
        project = await projects.get(identifier)
        if not project:
            await answer_callback(query, "Проект не найден", show_alert=True)
            return
        await state.update_data(project_id=identifier)
        await state.set_state(NewTask.agent)
        await answer_callback(query)
        assert query.message
        await query.message.answer("Выберите агента:", reply_markup=agent_list(project))

    @router.callback_query(NewTask.agent, F.data.startswith("agent:"))
    async def choose_agent(query: CallbackQuery, state: FSMContext) -> None:
        try:
            agent = AgentKind((query.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            await answer_callback(query, "Некорректный агент", show_alert=True)
            return
        models = tasks.available_models(agent)
        if not models:
            await answer_callback(query, "Для агента не настроены модели", show_alert=True)
            return
        await state.update_data(agent=agent.value)
        await state.set_state(NewTask.model)
        await answer_callback(query)
        assert query.message
        await query.message.answer(
            "Выберите модель:",
            reply_markup=model_choice(models, default_model=tasks.default_model(agent)),
        )

    @router.callback_query(NewTask.model, F.data.regexp(r"^model:[0-9]{1,3}$"))
    async def choose_model(query: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            agent = AgentKind(data["agent"])
            index = int((query.data or "").split(":", 1)[1])
            model = tasks.available_models(agent)[index]
        except (ValueError, KeyError, IndexError):
            await answer_callback(query, "Некорректная модель", show_alert=True)
            return
        await state.update_data(model=model)
        await state.set_state(NewTask.checkout)
        await answer_callback(query)
        assert query.message
        await query.message.answer("Где выполнять задачу?", reply_markup=checkout_choice())

    @router.callback_query(NewTask.checkout, F.data.in_({"checkout:current", "checkout:isolated"}))
    async def choose_checkout(query: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(isolated=query.data == "checkout:isolated")
        await state.set_state(NewTask.prompt)
        await answer_callback(query)
        assert query.message
        await query.message.answer("Отправьте текст задания. Файлы можно добавить после создания задачи.")

    @router.message(NewTask.prompt, F.text)
    async def receive_prompt(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            record = await tasks.create(
                project_id=data["project_id"],
                user_id=message.from_user.id,
                agent=AgentKind(data["agent"]),
                model=data["model"],
                prompt=message.text or "",
                isolated=bool(data["isolated"]),
            )
        except TaskManagerError as exc:
            await message.answer("Не удалось создать задачу: " + html.escape(str(exc)))
            return
        progress = await message.answer(
            f"<b>Задача #{record.id}</b>\nСостояние: запускается",
            reply_markup=task_actions(record.id),
        )
        reporter.bind(record.id, message.chat.id, progress.message_id)
        await state.clear()
        await state.update_data(active_task_id=record.id)

    @router.callback_query(F.data == "menu:tasks")
    async def active_tasks(query: CallbackQuery) -> None:
        await query.answer()
        assert query.message and query.from_user
        items, page, total_pages = await task_page(query.from_user.id, 0)
        title = "Задачи:" if total_pages == 1 else f"Задачи · страница {page + 1}/{total_pages}"
        await query.message.answer(title, reply_markup=task_list(items, page=page, total_pages=total_pages))

    @router.callback_query(F.data.startswith("tasks:"))
    async def active_tasks_page(query: CallbackQuery) -> None:
        await query.answer()
        assert query.message and query.from_user
        try:
            requested_page = int((query.data or "").split(":", 1)[1])
        except ValueError:
            return
        items, page, total_pages = await task_page(query.from_user.id, requested_page)
        title = "Задачи:" if total_pages == 1 else f"Задачи · страница {page + 1}/{total_pages}"
        await query.message.edit_text(
            title,
            reply_markup=task_list(items, page=page, total_pages=total_pages),
        )

    @router.callback_query(F.data.startswith("task:"))
    async def task_status(query: CallbackQuery, state: FSMContext) -> None:
        await query.answer()
        assert query.message and query.from_user
        try:
            task_id = int((query.data or "").split(":", 1)[1])
        except ValueError:
            return
        record = await tasks.get(task_id)
        if not record or record.user_id != query.from_user.id:
            return
        text = (
            f"<b>Задача #{record.id}</b>\nПроект: {html.escape(record.project_id)}\n"
            f"Агент: {record.agent.value}\nМодель: {html.escape(record.model or 'по умолчанию')}\n"
            f"Доступ: {record.access_mode.value}\n"
            f"Подтверждения: {record.approval_mode.value}\n"
            f"Состояние: {record.status.value}\n"
            f"Каталог: <code>{html.escape(str(record.checkout_path))}</code>"
        )
        if record.error:
            text += "\nОшибка: " + html.escape(record.error)
        await state.update_data(active_task_id=record.id)
        await query.message.answer(
            text,
            reply_markup=task_actions(task_id, show_result=record.status == TaskStatus.COMPLETED),
        )

    @router.callback_query(F.data.startswith("taskaccess:"))
    async def task_access(query: CallbackQuery) -> None:
        assert query.message and query.from_user
        try:
            task_id = int((query.data or "").split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная задача", show_alert=True)
            return
        record = await tasks.get(task_id)
        if not record or record.user_id != query.from_user.id:
            await query.answer("Задача не найдена", show_alert=True)
            return
        await query.answer()
        await show_task_access(query.message, record)

    @router.callback_query(F.data.regexp(r"^setaccess:[1-9][0-9]{0,18}:(?:read_only|project_only|approved_paths)$"))
    async def set_task_access(query: CallbackQuery) -> None:
        assert query.message and query.from_user
        try:
            _, task_raw, mode_raw = (query.data or "").split(":", 2)
            record = await tasks.change_access_mode(
                int(task_raw),
                query.from_user.id,
                FileAccessMode(mode_raw),
            )
        except (ValueError, TaskManagerError) as exc:
            detail = {
                "task not found": "Задача не найдена",
                "task is running": "Изменить доступ можно после завершения или остановки текущего запуска",
                "access mode exceeds the local project policy": "Этот режим запрещён локальной политикой проекта",
            }.get(str(exc), str(exc))
            await query.answer(detail, show_alert=True)
            return
        await query.answer("Режим доступа изменён")
        await show_task_access(query.message, record)

    @router.callback_query(F.data.startswith("taskapproval:"))
    async def task_approval_mode(query: CallbackQuery) -> None:
        assert query.message and query.from_user
        try:
            task_id = int((query.data or "").split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная задача", show_alert=True)
            return
        record = await tasks.get(task_id)
        if not record or record.user_id != query.from_user.id:
            await query.answer("Задача не найдена", show_alert=True)
            return
        await query.answer()
        await show_task_approvals(query.message, record)

    @router.callback_query(F.data.regexp(r"^setapproval:[1-9][0-9]{0,18}:(?:manual|auto)$"))
    async def set_task_approval_mode(query: CallbackQuery) -> None:
        assert query.message and query.from_user
        try:
            _, task_raw, mode_raw = (query.data or "").split(":", 2)
            record = await tasks.change_approval_mode(
                int(task_raw),
                query.from_user.id,
                ApprovalMode(mode_raw),
            )
        except (ValueError, TaskManagerError) as exc:
            detail = {
                "task not found": "Задача не найдена",
                "task is running": "Изменить подтверждения можно после завершения или остановки текущего запуска",
            }.get(str(exc), str(exc))
            await query.answer(detail, show_alert=True)
            return
        await query.answer("Режим подтверждений изменён")
        await show_task_approvals(query.message, record)

    @router.callback_query(F.data.startswith("taskmodel:"))
    async def task_model(query: CallbackQuery) -> None:
        assert query.message and query.from_user
        try:
            task_id = int((query.data or "").split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная задача", show_alert=True)
            return
        record = await tasks.get(task_id)
        if not record or record.user_id != query.from_user.id:
            await query.answer("Задача не найдена", show_alert=True)
            return
        models = tasks.available_models(record.agent)
        if not models:
            await query.answer("Для агента не настроены модели", show_alert=True)
            return
        await query.answer()
        await query.message.answer(
            f"Модель задачи #{task_id}: <b>{html.escape(record.model or 'по умолчанию')}</b>\n"
            "Выберите модель для следующих сообщений:",
            reply_markup=model_choice(
                models,
                default_model=tasks.default_model(record.agent),
                task_id=task_id,
                current_model=record.model,
            ),
        )

    @router.callback_query(F.data.regexp(r"^setmodel:[1-9][0-9]{0,18}:[0-9]{1,3}$"))
    async def set_task_model(query: CallbackQuery) -> None:
        assert query.from_user
        try:
            _, task_raw, index_raw = (query.data or "").split(":", 2)
            task_id = int(task_raw)
            index = int(index_raw)
            record = await tasks.get(task_id)
            if not record or record.user_id != query.from_user.id:
                raise TaskManagerError("task not found")
            model = tasks.available_models(record.agent)[index]
            record = await tasks.change_model(task_id, query.from_user.id, model)
        except IndexError:
            await query.answer("Некорректная модель", show_alert=True)
            return
        except (ValueError, TaskManagerError) as exc:
            detail = {
                "task not found": "Задача не найдена",
                "task is running": "Сменить модель можно после завершения или остановки текущего запуска",
            }.get(str(exc), str(exc))
            await query.answer(detail, show_alert=True)
            return
        await query.answer("Модель изменена")
        if query.message:
            await query.message.answer(
                f"Для задачи #{record.id} выбрана модель <b>{html.escape(record.model or '')}</b>. "
                "Она будет использована в следующем сообщении."
            )

    @router.callback_query(F.data.startswith("stop:"))
    async def stop_task(query: CallbackQuery) -> None:
        assert query.from_user
        try:
            task_id = int((query.data or "").split(":", 1)[1])
            await tasks.stop(task_id, query.from_user.id)
            await query.answer("Остановлено")
        except (ValueError, TaskManagerError) as exc:
            await query.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("continue:"))
    async def continue_task(query: CallbackQuery, state: FSMContext) -> None:
        task_id = int((query.data or "").split(":", 1)[1])
        assert query.from_user
        record = await tasks.get(task_id)
        if not record or record.user_id != query.from_user.id:
            await query.answer("Задача не найдена", show_alert=True)
            return
        if record.status == TaskStatus.WAITING_APPROVAL:
            await query.answer("Сначала подтвердите или запретите действие в карточке ниже.", show_alert=True)
            return
        if record.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            await query.answer("Задача ещё выполняется. Дождитесь её завершения.", show_alert=True)
            return
        await state.set_state(NewTask.continue_prompt)
        await state.update_data(task_id=task_id)
        await query.answer()
        assert query.message
        await query.message.answer(
            "Отправьте следующее сообщение агенту или введите команду "
            "(/usage, /status, /model, /access, /diff, /help):"
        )

    @router.message(NewTask.continue_prompt, F.text)
    async def continue_prompt(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        task_id = data.get("task_id")
        if not isinstance(task_id, int):
            await state.clear()
            await message.answer("Задача не выбрана. Откройте её снова через список активных задач.")
            return
        record = await tasks.get(task_id)
        if not record or record.user_id != message.from_user.id:
            await state.clear()
            await message.answer("Задача не найдена.")
            return
        if await answer_task_command(message, state, record):
            return
        try:
            record = await tasks.continue_task(task_id, message.from_user.id, message.text or "")
        except TaskManagerError as exc:
            await state.clear()
            detail = "Задача ещё выполняется. Дождитесь завершения или ответьте на запрос подтверждения."
            if str(exc) != "task is already running":
                detail = str(exc)
            await message.answer(html.escape(detail))
            return
        progress = await message.answer(f"Задача #{record.id} продолжается…")
        reporter.bind(record.id, message.chat.id, progress.message_id)
        await state.clear()
        await state.update_data(active_task_id=record.id)

    @router.callback_query(F.data.startswith("diff:"))
    async def git_diff(query: CallbackQuery) -> None:
        await query.answer()
        assert query.message and query.from_user
        task_id = int((query.data or "").split(":", 1)[1])
        record = await tasks.get(task_id)
        if not record or record.user_id != query.from_user.id:
            return
        try:
            diff = await git.diff(record.checkout_path, max_chars=12000)
        except Exception as exc:
            await query.message.answer("Git diff недоступен: " + html.escape(str(exc)))
            return
        if not diff:
            await query.message.answer("Изменений нет.")
        else:
            for start in range(0, len(diff), 3500):
                await query.message.answer("<pre>" + html.escape(diff[start : start + 3500]) + "</pre>")

    @router.callback_query(F.data.startswith("result:"))
    async def task_result(query: CallbackQuery) -> None:
        assert query.message and query.from_user
        try:
            task_id = int((query.data or "").split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная задача", show_alert=True)
            return
        record = await tasks.get(task_id)
        if not record or record.user_id != query.from_user.id:
            await query.answer("Задача не найдена", show_alert=True)
            return
        row = await database.fetch_one(
            "SELECT content FROM messages WHERE task_id=? AND kind='final' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        result = str(row["content"]) if row and row.get("content") else ""
        if not result:
            # Compatibility for tasks completed before final-answer events were
            # stored separately. This fallback is only sent on explicit request.
            rows = await database.fetch_all(
                "SELECT content FROM messages WHERE task_id=? AND kind='text_delta' ORDER BY id",
                (task_id,),
            )
            result = "".join(str(item["content"]) for item in rows)
        result = redact(result).strip()
        if not result:
            await query.answer("Итоговый ответ не найден", show_alert=True)
            return
        await query.answer()
        await send_markdown(
            query.message.bot,
            query.message.chat.id,
            result,
            f"task-{task_id}-result",
            title=f"<b>Результат задачи #{task_id}</b>",
        )

    @router.callback_query(F.data.startswith("upload:"))
    async def request_upload(query: CallbackQuery, state: FSMContext) -> None:
        task_id = int((query.data or "").split(":", 1)[1])
        await state.set_state(NewTask.upload)
        await state.update_data(task_id=task_id)
        await query.answer()
        assert query.message
        await query.message.answer("Пришлите документ или изображение. Telegram не использует E2E-шифрование.")

    @router.callback_query(F.data.startswith("files:"))
    async def task_files(query: CallbackQuery) -> None:
        await query.answer()
        assert query.message and query.from_user
        task_id = int((query.data or "").split(":", 1)[1])
        record = await tasks.get(task_id)
        if not record or record.user_id != query.from_user.id:
            return
        rows = await database.fetch_all(
            "SELECT id,path,size FROM files WHERE task_id=? AND direction='outbound' ORDER BY id DESC LIMIT 30",
            (task_id,),
        )
        if not rows and record.status not in {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}:
            try:
                for path in await git.changed_files(record.checkout_path):
                    if path.is_file():
                        await database.execute(
                            "INSERT INTO files(task_id,path,direction,size) VALUES(?,?,?,?)",
                            (task_id, str(path), "outbound", path.stat().st_size),
                        )
                rows = await database.fetch_all(
                    "SELECT id,path,size FROM files WHERE task_id=? AND direction='outbound' ORDER BY id DESC LIMIT 30",
                    (task_id,),
                )
            except OSError:
                pass
        if not rows:
            if record.status == TaskStatus.WAITING_APPROVAL:
                detail = "Сначала подтвердите или запретите запрошенное действие в карточке задачи."
            elif record.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                detail = "Задача ещё выполняется. Файлы появятся здесь после её завершения."
            else:
                detail = "Созданные или изменённые файлы не найдены."
            await query.message.answer(detail)
            return
        await query.message.answer("Выберите файл для отправки:", reply_markup=file_list(rows))

    @router.callback_query(F.data.startswith("sendfile:"))
    async def send_task_file(query: CallbackQuery) -> None:
        await query.answer()
        assert query.message and query.from_user
        file_id = int((query.data or "").split(":", 1)[1])
        row = await database.fetch_one(
            "SELECT f.*,t.user_id,t.checkout_path FROM files f JOIN tasks t ON t.id=f.task_id WHERE f.id=?",
            (file_id,),
        )
        if not row or int(row["user_id"]) != query.from_user.id:
            return
        path = Path(str(row["path"]))
        policy = ExportPolicy(Path(str(row["checkout_path"])), config.telegram_export.approved_paths)
        decision = policy.check(path)
        max_bytes = config.telegram_export.max_file_size_mb * 1024 * 1024
        if not decision.allowed:
            if decision.sensitive or decision.reason != "external_export_requires_approval":
                await query.message.answer("Отправка заблокирована политикой: " + html.escape(decision.reason))
                return
            size = path.stat().st_size
            if size > max_bytes:
                await query.message.answer("Файл превышает установленный лимит Telegram.")
                return
            digest = file_sha256(path)
            permission_id = await permissions.create(
                task_id=int(row["task_id"]),
                user_id=query.from_user.id,
                action="telegram_export",
                arguments={"path": str(path), "size": size, "sha256": digest},
            )
            await query.message.answer(
                "<b>Файл находится вне проекта</b>\n"
                f"Путь: <code>{html.escape(str(path))}</code>\n"
                f"Размер: {size} байт\n\n"
                "Telegram не использует E2E-шифрование.",
                reply_markup=export_approval(file_id, permission_id),
            )
            return
        if path.stat().st_size > max_bytes:
            await query.message.answer("Файл превышает установленный лимит Telegram.")
            return
        attachment = FSInputFile(path)
        if _is_preview_image(path):
            await query.message.answer_photo(attachment, caption=html.escape(path.name))
        else:
            await query.message.answer_document(attachment, caption=html.escape(path.name))
        await database.audit(
            "file_sent",
            user_id=query.from_user.id,
            task_id=int(row["task_id"]),
            detail={"file_id": file_id, "size": path.stat().st_size},
        )

    @router.callback_query(F.data.startswith("export:"))
    async def confirm_external_export(query: CallbackQuery) -> None:
        assert query.message and query.from_user
        try:
            _, file_raw, permission_id, choice = (query.data or "").split(":", 3)
            file_id = int(file_raw)
        except (ValueError, TypeError):
            await query.answer("Некорректное подтверждение", show_alert=True)
            return
        row = await database.fetch_one(
            "SELECT f.*,t.user_id FROM files f JOIN tasks t ON t.id=f.task_id WHERE f.id=?",
            (file_id,),
        )
        if not row or int(row["user_id"]) != query.from_user.id:
            await query.answer("Файл не найден", show_alert=True)
            return
        path = Path(str(row["path"])).resolve(strict=True)
        if not path.is_file() or is_sensitive(path):
            await query.answer("Файл заблокирован политикой безопасности", show_alert=True)
            return
        size = path.stat().st_size
        digest = file_sha256(path)
        consumed = await permissions.consume(
            permission_id,
            task_id=int(row["task_id"]),
            user_id=query.from_user.id,
            action="telegram_export",
            arguments={"path": str(path), "size": size, "sha256": digest},
            decision="allow_once" if choice == "allow" else "deny",
        )
        if not consumed:
            await query.answer("Подтверждение истекло или уже использовано", show_alert=True)
            return
        await query.message.edit_reply_markup(reply_markup=None)
        if choice != "allow":
            await query.answer("Отправка запрещена")
            return
        max_bytes = config.telegram_export.max_file_size_mb * 1024 * 1024
        if size > max_bytes:
            await query.answer("Файл превышает лимит", show_alert=True)
            return
        attachment = FSInputFile(path)
        if _is_preview_image(path):
            await query.message.answer_photo(attachment, caption=html.escape(path.name))
        else:
            await query.message.answer_document(attachment, caption=html.escape(path.name))
        await database.audit(
            "external_file_sent",
            user_id=query.from_user.id,
            task_id=int(row["task_id"]),
            detail={"file_id": file_id, "size": size},
        )
        await query.answer("Файл отправлен")

    @router.message(NewTask.upload, F.document | F.photo)
    async def receive_upload(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        record = await tasks.get(data["task_id"])
        if not record or record.user_id != message.from_user.id:
            await state.clear()
            return
        if message.document:
            remote = message.document
            original = remote.file_name or f"document-{remote.file_unique_id}"
        else:
            remote = message.photo[-1]
            original = f"image-{remote.file_unique_id}.jpg"
        max_bytes = config.telegram_export.max_file_size_mb * 1024 * 1024
        if remote.file_size and remote.file_size > max_bytes:
            await message.answer("Файл превышает установленный лимит.")
            return
        inbox = config.instance.data_dir.expanduser() / "tasks" / str(record.id) / "inbox"
        destination = safe_inbox_destination(inbox, original)
        await bot.download(remote, destination=destination)
        size = destination.stat().st_size
        await database.execute(
            "INSERT INTO files(task_id,path,direction,size) VALUES(?,?,?,?)",
            (record.id, str(destination), "inbound", size),
        )
        await database.audit("file_received", user_id=message.from_user.id, task_id=record.id, detail={"size": size})
        await message.answer(
            f"Файл сохранён: <code>{html.escape(str(destination))}</code>\n"
            "Отправьте агенту сообщение с просьбой использовать этот путь."
        )
        await state.clear()

    @router.callback_query(F.data.startswith("close:"))
    async def close_task(query: CallbackQuery) -> None:
        assert query.from_user
        task_id = int((query.data or "").split(":", 1)[1])
        record = await tasks.get(task_id)
        if not record or record.user_id != query.from_user.id:
            return
        await database.execute("UPDATE tasks SET status='closed',updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
        await query.answer("Задача закрыта")

    @router.callback_query(F.data == "menu:health")
    async def health(query: CallbackQuery) -> None:
        ok, detail = await database.health()
        await query.answer()
        assert query.message
        await query.message.answer(
            f"Компьютер: {html.escape(config.instance.name)}\n"
            f"База данных: {'OK' if ok else 'ошибка'} ({html.escape(detail)})\n"
            f"Активных процессов: {len(tasks._runners)}"
        )

    @router.callback_query(F.data.in_({"menu:files", "menu:settings"}))
    async def informational(query: CallbackQuery) -> None:
        await query.answer()
        assert query.message
        await query.message.answer(
            "Файлы доступны из карточки задачи. Настройки проектов и безопасности изменяются локально в config.yaml."
        )

    @router.callback_query(F.data.regexp(r"^(approve|deny|explain):"))
    async def approval_answer(query: CallbackQuery) -> None:
        assert query.from_user
        try:
            action, task_raw, request_id = (query.data or "").split(":", 2)
            decision = {"approve": "accept", "deny": "decline", "explain": "explain"}[action]
            await tasks.answer_approval(int(task_raw), query.from_user.id, request_id, decision)
            await query.answer("Решение отправлено")
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
        except (ValueError, TaskManagerError, Exception) as exc:
            await query.answer(str(exc), show_alert=True)

    @router.message(StateFilter(None))
    async def fallback(message: Message, state: FSMContext) -> None:
        if (message.text or "").lstrip().startswith("/"):
            data = await state.get_data()
            task_id = data.get("active_task_id")
            if isinstance(task_id, int):
                record = await tasks.get(task_id)
                if record and record.user_id == message.from_user.id:
                    if await answer_task_command(message, state, record):
                        return
            await message.answer("Сначала откройте нужную задачу, затем введите команду.")
            return
        await message.answer(
            "Используйте меню. Команды произвольной оболочки не поддерживаются.", reply_markup=main_menu()
        )

    return router


def _is_preview_image(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return False
    return (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or header.startswith((b"GIF87a", b"GIF89a"))
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )
