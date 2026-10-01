import json

from ai_control.bot.commands import format_claude_usage, format_codex_usage
from ai_control.bot.handlers import panic_confirmed


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
