from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from ai_control.agents import ClaudeAdapter, CodexAdapter, CursorAdapter
from ai_control.config.models import AppConfig
from ai_control.platform import current_platform
from ai_control.projects import ProjectRegistry
from ai_control.security.redaction import redact
from ai_control.storage import Database


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str


async def run_doctor(config: AppConfig) -> list[Check]:
    data_dir = config.instance.data_dir.expanduser()
    database = Database(data_dir / "state.db")
    await database.initialize()
    projects = ProjectRegistry(database)
    await projects.sync_config(config.projects)
    platform = current_platform()
    checks = [
        Check("Python", sys.version_info >= (3, 12), sys.version.split()[0]),
        Check("Git", bool(shutil.which("git")), shutil.which("git") or "not found"),
    ]
    db_ok, db_detail = await database.health()
    checks.append(Check("SQLite", db_ok, f"{sqlite3.sqlite_version}: {db_detail}"))
    for name, adapter in (
        ("Codex", CodexAdapter(platform, config.codex.executable)),
        ("Claude", ClaudeAdapter(platform, config.claude.executable)),
        ("Cursor", CursorAdapter(platform, config.cursor.executable)),
    ):
        capability = await adapter.capabilities()
        checks.append(Check(name, capability.available, capability.version or capability.detail))
    codex_auth = await _command_output(config.codex.executable, "login", "status")
    checks.append(Check("Codex auth", codex_auth[0], _codex_auth_detail(codex_auth[1])))
    claude_auth = await _command_output(config.claude.executable, "auth", "status")
    checks.append(
        Check(
            "Claude auth",
            claude_auth[0] and _claude_logged_in(claude_auth[1]),
            _claude_auth_detail(claude_auth[1]),
        )
    )
    cursor_auth = await _command_output(config.cursor.executable, "status")
    checks.append(
        Check(
            "Cursor auth",
            cursor_auth[0]
            and any(marker in cursor_auth[1].casefold() for marker in ("login successful", "logged in")),
            _cursor_auth_detail(cursor_auth[1]),
        )
    )
    for project in await projects.list():
        issues = await projects.validate(project)
        checks.append(Check(f"Project: {project.name}", not issues, "; ".join(issues) or str(project.path)))
    writable = os.access(data_dir, os.W_OK) if data_dir.exists() else os.access(data_dir.parent, os.W_OK)
    checks.append(Check("Data directory", writable, str(data_dir)))
    try:
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass", stdout=asyncio.subprocess.DEVNULL)
        child_ok = await child.wait() == 0
    except OSError as exc:
        child_ok = False
        child_detail = str(exc)
    else:
        child_detail = "child process created"
    checks.append(Check("Child processes", child_ok, child_detail))
    if any(project.runtime.value == "wsl" for project in await projects.list()):
        checks.append(Check("WSL", bool(shutil.which("wsl.exe")), shutil.which("wsl.exe") or "not found"))
    if config.telegram.token:
        try:
            from aiogram import Bot

            bot = Bot(config.telegram.token.get_secret_value())
            try:
                identity = await asyncio.wait_for(bot.get_me(), 15)
                checks.append(Check("Telegram", True, f"@{identity.username}"))
            finally:
                await bot.session.close()
        except Exception as exc:
            checks.append(Check("Telegram", False, redact(str(exc))))
    else:
        checks.append(Check("Telegram", False, "bot token is missing"))
    if os.name == "nt":
        autostart = await _command_output("schtasks", "/Query", "/TN", f"AI Control - {config.instance.name}")
        checks.append(Check("Autostart", autostart[0], "Task Scheduler" if autostart[0] else "not configured"))
    else:
        service = Path("~/.config/systemd/user/ai-control.service").expanduser()
        checks.append(
            Check("Autostart", service.exists(), str(service) if service.exists() else "optional; not configured")
        )
    return checks


def format_doctor(checks: list[Check]) -> str:
    return "\n".join(f"[{'OK' if item.ok else 'FAIL'}] {item.name}: {item.detail}" for item in checks)


async def _command_output(*argv: str) -> tuple[bool, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), 15)
        return process.returncode == 0, stdout.decode(errors="replace").strip()
    except (OSError, TimeoutError) as exc:
        return False, str(exc)


def _codex_auth_detail(output: str) -> str:
    for line in output.splitlines():
        if "logged in" in line.casefold() or "not logged" in line.casefold():
            return line.strip()
    return "authentication status unavailable"


def _claude_logged_in(output: str) -> bool:
    try:
        return bool(json.loads(output).get("loggedIn"))
    except (json.JSONDecodeError, AttributeError):
        return False


def _claude_auth_detail(output: str) -> str:
    try:
        value = json.loads(output)
        return f"loggedIn={bool(value.get('loggedIn'))}, method={value.get('authMethod', 'unknown')}"
    except (json.JSONDecodeError, AttributeError):
        return "authentication status unavailable"


def _cursor_auth_detail(output: str) -> str:
    for line in output.splitlines():
        if any(marker in line.casefold() for marker in ("login successful", "logged in", "not logged")):
            return line.strip()
    return "authentication status unavailable"
