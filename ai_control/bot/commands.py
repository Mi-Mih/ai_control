from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ai_control.core.models import AgentKind, TaskRecord
from ai_control.git import GitService
from ai_control.sessions import TaskManager
from ai_control.sessions.manager import TaskManagerError
from ai_control.storage import Database

SUPPORTED_COMMANDS = {"/help", "/status", "/usage", "/model", "/access", "/approvals", "/diff"}


async def execute_task_command(
    text: str,
    *,
    record: TaskRecord,
    user_id: int,
    tasks: TaskManager,
    database: Database,
    git: GitService,
) -> str | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    command = stripped.split(maxsplit=1)[0].casefold()
    if command not in SUPPORTED_COMMANDS:
        return (
            f"Команда {command} недоступна в фоновом режиме.\n"
            "Поддерживаются: /usage, /status, /model, /access, /approvals, /diff, /help"
        )
    if command == "/help":
        return (
            "Команды задачи:\n"
            "/usage — использование и лимиты\n"
            "/status — состояние задачи и сессии\n"
            "/model — текущая модель\n"
            "/access — файловые права следующего запуска\n"
            "/approvals — режим подтверждений следующего запуска\n"
            "/diff — изменения Git\n"
            "/help — эта справка"
        )
    if command == "/status":
        return (
            f"Задача #{record.id}\n"
            f"Агент: {record.agent.value}\n"
            f"Модель: {record.model or 'по умолчанию'}\n"
            f"Доступ: {record.access_mode.value}\n"
            f"Подтверждения: {record.approval_mode.value}\n"
            f"Состояние: {record.status.value}\n"
            f"Сессия: {record.agent_session_id or 'ещё не создана'}\n"
            f"Каталог: {record.checkout_path}"
        )
    if command == "/model":
        available = ", ".join(tasks.available_models(record.agent)) or "не настроены"
        return f"Текущая модель: {record.model or 'по умолчанию'}\nДоступные модели: {available}"
    if command == "/access":
        return f"Текущий режим доступа: {record.access_mode.value}"
    if command == "/approvals":
        return f"Текущий режим подтверждений: {record.approval_mode.value}"
    if command == "/diff":
        diff = await git.diff(record.checkout_path, max_chars=12000)
        return diff or "Изменений нет."
    if record.agent == AgentKind.CODEX:
        try:
            payload = await tasks.codex_usage(record.id, user_id)
        except TaskManagerError as exc:
            return "Не удалось получить лимиты Codex: " + str(exc)
        return format_codex_usage(payload)
    if record.agent == AgentKind.CLAUDE:
        rows = await database.fetch_all(
            "SELECT content FROM messages WHERE task_id=? AND kind='usage' ORDER BY id",
            (record.id,),
        )
        return format_claude_usage([str(row["content"]) for row in rows])
    return (
        "Cursor ACP не предоставляет боту данные об использовании подписки. "
        "Проверьте лимиты в приложении Cursor или в личном кабинете."
    )


def format_codex_usage(payload: dict[str, object]) -> str:
    lines = ["Использование Codex"]
    allowed = payload.get("ordinaryUsageAllowed")
    if isinstance(allowed, bool):
        lines.append("Обычные запросы: " + ("доступны" if allowed else "лимит исчерпан"))
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        snapshot = payload.get("rateLimits")
        buckets = {"codex": snapshot} if isinstance(snapshot, dict) else {}
    for identifier, raw in buckets.items():
        if not isinstance(raw, dict):
            continue
        name = raw.get("limitName") or identifier
        lines.append(f"\n{name}:")
        for label, key in (("Основное окно", "primary"), ("Дополнительное окно", "secondary")):
            window = raw.get(key)
            if isinstance(window, dict):
                lines.append(_format_rate_window(label, window))
    if len(lines) == 1:
        return "Codex не вернул сведения об использовании."
    return "\n".join(lines)


def format_claude_usage(contents: list[str]) -> str:
    totals: dict[str, float] = {}
    turns = 0
    for content in contents:
        try:
            item = json.loads(content)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        turns += 1
        for key, value in item.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + float(value)
    if not turns:
        return (
            "Для этой задачи пока нет статистики Claude. "
            "Claude Code в фоновом режиме не предоставляет лимиты подписки, "
            "но бот будет учитывать токены следующих запусков."
        )
    labels = {
        "input_tokens": "Входные токены",
        "output_tokens": "Выходные токены",
        "cache_creation_tokens": "Создание кэша",
        "cache_read_tokens": "Чтение из кэша",
        "cost_usd": "Стоимость, USD",
    }
    lines = ["Использование Claude в задаче", f"Зафиксировано запусков: {turns}"]
    for key in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"):
        if key in totals:
            lines.append(f"{labels[key]}: {int(totals[key])}")
    if "cost_usd" in totals:
        lines.append(f"{labels['cost_usd']}: {totals['cost_usd']:.6f}")
    return "\n".join(lines)


def _format_rate_window(label: str, window: dict[str, Any]) -> str:
    used = int(window.get("usedPercent", 0))
    remaining = max(0, 100 - used)
    duration = _format_duration(window.get("windowDurationMins"))
    result = f"{label}{duration}: использовано {used}%, осталось {remaining}%"
    resets_at = window.get("resetsAt")
    if isinstance(resets_at, int):
        reset = datetime.fromtimestamp(resets_at).astimezone().strftime("%d.%m.%Y %H:%M")
        result += f", сброс {reset}"
    return result


def _format_duration(value: object) -> str:
    if not isinstance(value, int) or value <= 0:
        return ""
    if value % 1440 == 0:
        return f" ({value // 1440} дн.)"
    if value % 60 == 0:
        return f" ({value // 60} ч.)"
    return f" ({value} мин.)"
