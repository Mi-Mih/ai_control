from __future__ import annotations

import asyncio
import html
import json
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from ai_control.bot.keyboards import approval, task_actions
from ai_control.config.models import AppConfig
from ai_control.core.models import AgentEvent, TaskRecord
from ai_control.security.redaction import redact


@dataclass(slots=True)
class ProgressState:
    chat_id: int
    message_id: int
    text: str = ""
    last_update: float = 0
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
        if event.kind == "usage":
            return
        if event.kind == "approval":
            request_id = str(event.payload.get("request_id", ""))
            body = self._status(task, "ожидает подтверждения")
            tool_name = str(event.payload.get("tool_name") or event.text or "Действие")
            detail = f"Действие: {tool_name}"
            arguments = event.payload.get("input") or event.payload.get("tool_input")
            if isinstance(arguments, dict):
                file_path = arguments.get("file_path")
                if isinstance(file_path, str):
                    detail += f"\nФайл: {file_path}"
                content = arguments.get("content") or arguments.get("new_string")
                if isinstance(content, str):
                    preview = content[:350]
                    if len(content) > len(preview):
                        preview += "\n…"
                    detail += f"\n\nСодержимое:\n{preview}"
                elif not file_path:
                    serialized = json.dumps(arguments, ensure_ascii=False, indent=2)
                    detail += "\n" + serialized[:600]
            detail = redact(detail)
            await self.bot.send_message(
                state.chat_id,
                body + f"\n\n<b>Запрос:</b>\n<pre>{html.escape(detail)}</pre>",
                reply_markup=approval(task.id, request_id),
            )
            return
        if event.text:
            state.text = (state.text + event.text)[-3000:]
        final = event.kind in {"completed", "error", "final"}
        now = time.monotonic()
        if not final and now - state.last_update < self.config.telegram.progress_interval_seconds:
            return
        async with state.lock:
            state.last_update = now
            if event.kind == "error":
                status = "ошибка"
            elif event.kind == "completed":
                status = "completed"
            else:
                status = task.status.value
            body = self._status(task, status)
            if state.text:
                body += "\n\n<pre>" + html.escape(state.text[-3000:]) + "</pre>"
            try:
                await self.bot.edit_message_text(
                    body,
                    chat_id=state.chat_id,
                    message_id=state.message_id,
                    reply_markup=task_actions(task.id),
                )
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(float(exc.retry_after), 5))
            except TelegramBadRequest:
                pass

    def _status(self, task: TaskRecord, status: str) -> str:
        return (
            f"<b>Задача #{task.id}</b>\n"
            f"Компьютер: {html.escape(self.config.instance.name)}\n"
            f"Проект: {html.escape(task.project_id)}\n"
            f"Агент: {task.agent.value}\n"
            f"Модель: {html.escape(task.model or 'по умолчанию')}\n"
            f"Состояние: {html.escape(status)}"
        )
