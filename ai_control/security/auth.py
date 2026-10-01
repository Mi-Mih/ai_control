from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    reason: str


class TelegramAuthorizer:
    def __init__(self, allowed_user_ids: frozenset[int], *, allow_groups: bool = False) -> None:
        self.allowed_user_ids = allowed_user_ids
        self.allow_groups = allow_groups

    def check(self, user_id: int | None, chat_type: str) -> AccessDecision:
        if user_id is None or user_id not in self.allowed_user_ids:
            return AccessDecision(False, "user_not_allowed")
        if chat_type not in {"private", "sender"} and not self.allow_groups:
            return AccessDecision(False, "groups_disabled")
        return AccessDecision(True, "allowed")


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[int, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            events = self._events[key]
            while events and events[0] <= now - self.window_seconds:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


_CALLBACK_PATTERNS = (
    re.compile(r"^menu:(?:main|projects|tasks|new|import|files|health|settings)$"),
    re.compile(r"^tasks:[0-9]{1,6}$"),
    re.compile(r"^(?:project|importproject):[a-z0-9][a-z0-9-]{1,48}$"),
    re.compile(r"^(?:agent|importagent):(?:claude|codex|cursor)$"),
    re.compile(r"^importsession:[0-9]{1,2}$"),
    re.compile(r"^model:[0-9]{1,3}$"),
    re.compile(r"^checkout:(?:current|isolated)$"),
    re.compile(
        r"^(?:task|stop|continue|diff|upload|files|close|sendfile|taskmodel|taskaccess|taskapproval):"
        r"[1-9][0-9]{0,18}$"
    ),
    re.compile(r"^setmodel:[1-9][0-9]{0,18}:[0-9]{1,3}$"),
    re.compile(r"^setaccess:[1-9][0-9]{0,18}:(?:read_only|project_only|approved_paths)$"),
    re.compile(r"^setapproval:[1-9][0-9]{0,18}:(?:manual|auto)$"),
    re.compile(r"^(?:approve|deny|explain):[1-9][0-9]{0,18}:[A-Za-z0-9_-]{1,40}$"),
    re.compile(r"^export:[1-9][0-9]{0,18}:[A-Za-z0-9_-]{16,32}:(?:allow|deny)$"),
)


def valid_callback_data(value: str | None) -> bool:
    return bool(value and len(value.encode()) <= 64 and any(pattern.fullmatch(value) for pattern in _CALLBACK_PATTERNS))
