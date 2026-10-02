import json
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from ai_control.bot.commands import format_claude_usage, format_codex_usage
from ai_control.bot.handlers import answer_callback, panic_confirmed


def test_format_codex_usage() -> None:
    text = format_codex_usage(
        {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "primary": {"usedPercent": 25, "windowDurationMins": 300},
                "secondary": {"usedPercent": 60, "windowDurationMins": 10080},
            },
        }
    )
    assert "осталось 75%" in text
    assert "7 дн." in text


def test_format_claude_usage_sums_turns() -> None:
    text = format_claude_usage(
        [
            json.dumps({"input_tokens": 10, "output_tokens": 2, "cost_usd": 0.01}),
            json.dumps({"input_tokens": 5, "output_tokens": 3, "cost_usd": 0.02}),
        ]
    )
    assert "Зафиксировано запусков: 2" in text
    assert "Входные токены: 15" in text
    assert "Стоимость, USD: 0.030000" in text


def test_panic_requires_exact_confirmation() -> None:
    assert panic_confirmed("/panic STOP")
    assert panic_confirmed("/panic@ai_control_bot STOP")
    assert not panic_confirmed("/panic")
    assert not panic_confirmed("/panic stop")
    assert not panic_confirmed("/panic STOP now")


@pytest.mark.asyncio
async def test_answer_callback_ignores_expired_query() -> None:
    query = AsyncMock()
    query.data = "agent:codex"
    query.answer.side_effect = TelegramBadRequest(
        method=object(),
        message="Bad Request: query is too old and response timeout expired or query ID is invalid",
    )

    await answer_callback(query)


@pytest.mark.asyncio
async def test_answer_callback_reraises_other_bad_requests() -> None:
    query = AsyncMock()
    query.data = "agent:codex"
    query.answer.side_effect = TelegramBadRequest(method=object(), message="Bad Request: another error")

    with pytest.raises(TelegramBadRequest):
        await answer_callback(query)
