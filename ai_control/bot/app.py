from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from ai_control.bot.handlers import create_router
from ai_control.bot.middleware import AccessMiddleware
from ai_control.bot.progress import ProgressReporter
from ai_control.config.models import AppConfig
from ai_control.git import GitService
from ai_control.projects import ProjectRegistry
from ai_control.security import RateLimiter, TelegramAuthorizer
from ai_control.sessions import TaskManager
from ai_control.storage import Database


async def run_bot(
    config: AppConfig,
    database: Database,
    projects: ProjectRegistry,
    tasks: TaskManager,
    git: GitService,
) -> None:
    if config.telegram.token is None:
        raise RuntimeError("Telegram token is missing; use setup or AI_CONTROL_BOT_TOKEN")
    if not config.telegram.allowed_user_ids:
        raise RuntimeError("ALLOWED_TELEGRAM_USER_IDS cannot be empty")
    bot = Bot(
        config.telegram.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    access = AccessMiddleware(
        TelegramAuthorizer(config.telegram.allowed_user_ids, allow_groups=config.telegram.allow_groups),
        RateLimiter(config.telegram.requests_per_minute),
        database,
    )
    dispatcher.message.outer_middleware(access)
    dispatcher.callback_query.outer_middleware(access)
    reporter = ProgressReporter(bot, config)
    tasks.subscribe(reporter)
    shutdown_lock = asyncio.Lock()

    async def request_shutdown() -> None:
        async with shutdown_lock:
            await tasks.shutdown()
            await dispatcher.stop_polling()

    dispatcher.include_router(create_router(config, database, projects, tasks, git, reporter, request_shutdown))
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
