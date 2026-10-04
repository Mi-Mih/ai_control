from __future__ import annotations

import asyncio
import html
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile

from ai_control.bot.formatting import MAX_MESSAGES, MESSAGE_LIMIT, markdown_to_html, split_markdown
from ai_control.bot.keyboards import approval, task_actions
from ai_control.config.models import AppConfig
from ai_control.core.models import AgentEvent, TaskRecord
from ai_control.security.redaction import redact


@dataclass(slots=True)
class ProgressState:
    chat_id: int
    message_id: int
    response_text: str = ""
    stage: str = ""
    detail: str = ""
    stage_started: float = 0
    started_at: float = field(default_factory=time.monotonic)
    last_activity: datetime | None = None
    last_update: float = 0
    last_rendered: str = ""
    result_sent: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProgressReporter:
    def __init__(self, bot: Bot, config: AppConfig) -> None:
        self.bot = bot
        self.config = config
        self.states: dict[int, ProgressState] = {}

    def bind(self, task_id: int, chat_id: int, message_id: int) -> None:
        self.states[task_id] = ProgressState(chat_id, message_id)

    async def __call__(self, task: TaskRecord, event: AgentEvent) -> None:
        state = self.states.get(task.id)
        if not state:
            return
        if event.kind in {"usage", "session"}:
            return
        if event.kind == "approval":
            request_id = str(event.payload.get("request_id", ""))
            body = self._status(task, "ожидает подтверждения")
            tool_name = redact(str(event.payload.get("tool_name") or event.text or "Действие"))[:120]
            detail = f"Действие: {tool_name}"
            arguments = event.payload.get("input") or event.payload.get("tool_input")
            if isinstance(arguments, dict):
                file_path = arguments.get("file_path")
                if isinstance(file_path, str):
                    detail += f"\nФайл: {_safe_display_path(file_path, task.checkout_path)}"
            detail = redact(detail)
            await self.bot.send_message(
                state.chat_id,
                body + f"\n\n<b>Запрос:</b>\n<pre>{html.escape(detail)}</pre>",
                reply_markup=approval(task.id, request_id),
            )
            return
        now = time.monotonic()
        if event.kind in {"progress", "status", "recovery", "policy_denied"} and event.text:
            stage = redact(event.text).strip()[:240]
            if stage != state.stage:
                state.stage = stage
                state.stage_started = now
        elif event.kind == "final" and event.text:
            state.response_text = redact(event.text).strip()
        elif event.kind == "error" and event.text:
            state.detail = redact(event.text)[-1000:]
        state.last_activity = datetime.now().astimezone()
        final = event.kind in {"completed", "error", "final"}
        if not final and now - state.last_update < self.config.telegram.progress_interval_seconds:
            return
        async with state.lock:
            if event.kind == "error":
                status = "ошибка"
            elif event.kind == "completed":
                status = "завершена"
            else:
                status = task.status.value
            body = self._status(task, status)
            if status in {"running", "pending", "waiting_approval"} and state.stage:
                duration = _duration_text(now - state.stage_started) if state.stage_started else ""
                body += f"\nЭтап: {html.escape(state.stage)}"
                if duration:
                    body += f" ({duration})"
                if state.last_activity:
                    body += f"\nПоследняя активность: {state.last_activity.strftime('%H:%M:%S')}"
            elif status == "завершена":
                body += f"\nДлительность запуска: {_duration_text(now - state.started_at)}"
            detail = "\n\n<pre>" + html.escape(state.detail) + "</pre>" if state.detail else ""
            separate_result = False
            if state.response_text:
                inline = "\n\n<b>Итог:</b>\n" + markdown_to_html(state.response_text)
                if len(body) + len(inline) + len(detail) <= MESSAGE_LIMIT:
                    body += inline
                else:
                    separate_result = True
                    body += "\n\n<b>Итог</b> — полный ответ в следующих сообщениях ⬇️"
            body += detail
            if body == state.last_rendered and not final:
                return
            state.last_update = now
            try:
                await self.bot.edit_message_text(
                    body,
                    chat_id=state.chat_id,
                    message_id=state.message_id,
                    reply_markup=task_actions(task.id, show_result=bool(state.response_text)),
                )
                state.last_rendered = body
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(float(exc.retry_after), 5))
            except TelegramBadRequest:
                pass
            if separate_result and event.kind in {"completed", "error"} and not state.result_sent:
                state.result_sent = True
                await send_markdown(self.bot, state.chat_id, state.response_text, f"task-{task.id}-result")

    def _status(self, task: TaskRecord, status: str) -> str:
        return (
            f"<b>Задача #{task.id}</b>\n"
            f"Компьютер: {html.escape(self.config.instance.name)}\n"
            f"Проект: {html.escape(task.project_id)}\n"
            f"Агент: {task.agent.value}\n"
            f"Модель: {html.escape(task.model or 'по умолчанию')}\n"
            f"Состояние: {html.escape(status)}"
        )


async def send_markdown(bot: Bot, chat_id: int, text: str, filename: str, title: str = "") -> None:
    """Sends a Markdown answer as formatted messages, or as a file when it is too long.

    Args:
        bot: Telegram bot.
        chat_id: Target chat.
        text: Answer in Markdown, already redacted.
        filename: File name without extension for the document fallback.
        title: Optional HTML heading prepended to the first message.
    """
    chunks = split_markdown(text, MESSAGE_LIMIT - len(title) - 2)
    if len(chunks) <= MAX_MESSAGES:
        try:
            for index, chunk in enumerate(chunks):
                await bot.send_message(chat_id, f"{title}\n\n{chunk}" if title and index == 0 else chunk)
            return
        except TelegramBadRequest:
            pass
    document = BufferedInputFile(text.encode("utf-8"), filename=f"{filename}.md")
    await bot.send_document(chat_id, document, caption=title or None)


def _duration_text(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} сек"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} мин {rest:02d} сек"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


def _safe_display_path(value: str, root: Path) -> str:
    candidate = Path(value)
    try:
        resolved = (
            candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)
        )
        display = resolved.relative_to(root.resolve(strict=False)).as_posix()
    except (OSError, ValueError):
        parts = [part for part in value.replace("\\", "/").split("/") if part]
        display = "…/" + "/".join(parts[-2:]) if parts else "…"
    display = redact(display)
    return display if len(display) <= 96 else "…" + display[-95:]
