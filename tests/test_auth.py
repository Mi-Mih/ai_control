import asyncio
import logging

from ai_control.security.auth import RateLimiter, TelegramAuthorizer, valid_callback_data
from ai_control.security.redaction import RedactingFilter


def test_authorizer_is_private_and_allowlisted() -> None:
    auth = TelegramAuthorizer(frozenset({42}))
    assert auth.check(42, "private").allowed
    assert auth.check(7, "private").reason == "user_not_allowed"
    assert auth.check(42, "group").reason == "groups_disabled"


def test_rate_limiter() -> None:
    async def scenario() -> None:
        limiter = RateLimiter(2, window_seconds=60)
        assert await limiter.allow(42)
        assert await limiter.allow(42)
        assert not await limiter.allow(42)
        assert await limiter.allow(7)

    asyncio.run(scenario())


def test_callback_data_is_strictly_validated() -> None:
    assert valid_callback_data("task:42")
    assert valid_callback_data("tasks:42")
    assert valid_callback_data("model:2")
    assert valid_callback_data("taskmodel:42")
    assert valid_callback_data("taskaccess:42")
    assert valid_callback_data("taskapproval:42")
    assert valid_callback_data("setmodel:42:2")
    assert valid_callback_data("setaccess:42:read_only")
    assert valid_callback_data("setaccess:42:approved_paths")
    assert not valid_callback_data("setaccess:42:full_access")
    assert valid_callback_data("setapproval:42:manual")
    assert valid_callback_data("setapproval:42:auto")
    assert valid_callback_data("menu:import")
    assert valid_callback_data("importproject:my-project")
    assert valid_callback_data("importagent:cursor")
    assert valid_callback_data("importsession:12")
    assert valid_callback_data("approve:42:request_id-1")
    assert not valid_callback_data("task:42:extra")
    assert not valid_callback_data("tasks:-1")
    assert not valid_callback_data("project:../../etc")
    assert not valid_callback_data("unknown:action")


def test_log_redaction_preserves_numeric_format_arguments() -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        "",
        0,
        "bot id=%d token=%s",
        (42, "token=secret"),
        None,
    )
    assert RedactingFilter().filter(record)
    assert record.getMessage() == "bot id=42 token=***"
