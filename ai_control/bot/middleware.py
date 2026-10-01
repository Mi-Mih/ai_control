from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from ai_control.security import RateLimiter, TelegramAuthorizer
from ai_control.security.auth import valid_callback_data
from ai_control.storage import Database

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, authorizer: TelegramAuthorizer, limiter: RateLimiter, database: Database) -> None:
        self.authorizer = authorizer
        self.limiter = limiter
        self.database = database

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message):
            return None
        user_id = event.from_user.id if event.from_user else None
        decision = self.authorizer.check(user_id, message.chat.type)
        if not decision.allowed:
            logger.warning("Rejected Telegram access user_id=%s reason=%s", user_id, decision.reason)
            await self.database.audit(
                "unauthorized_telegram_access",
                user_id=user_id,
                detail={"reason": decision.reason, "chat_type": message.chat.type},
            )
            return None
        assert user_id is not None
        if isinstance(event, CallbackQuery) and not valid_callback_data(event.data):
            await self.database.audit("invalid_callback", user_id=user_id)
            await event.answer("Некорректная или устаревшая кнопка", show_alert=True)
            return None
        if not await self.limiter.allow(user_id):
            await self.database.audit("telegram_rate_limited", user_id=user_id)
            if isinstance(event, CallbackQuery):
                await event.answer("Слишком много запросов. Попробуйте позже.", show_alert=True)
            return None
        return await handler(event, data)
