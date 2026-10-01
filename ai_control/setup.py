from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from ai_control.security.secrets import SecretStore


def run_setup(config_path: Path) -> None:
    print("AI Control setup")
    name = input("1. Название установки: ").strip()
    token = getpass.getpass("2. Telegram bot token (ввод скрыт): ").strip()
    user_id = int(input("3. Разрешённый Telegram user ID: ").strip())
    default_data = Path(os.getenv("LOCALAPPDATA", "~/.ai-control")).expanduser() / "AIControl"
    data_raw = input(f"4. Каталог данных [{default_data}]: ").strip()
    data_dir = Path(data_raw).expanduser() if data_raw else default_data
    print("5–6. Найденные инструменты:")
    print("  Claude:", shutil.which("claude") or "не найден")
    print("  Codex:", shutil.which("codex") or "не найден")
    print("  Cursor:", shutil.which("agent") or "не найден")
    print("  Git:", shutil.which("git") or "не найден")
    project_name = input("7. Название первого проекта: ").strip()
    project_path = Path(input("   Абсолютный путь проекта: ").strip()).expanduser()
    if not project_path.is_absolute() or not project_path.is_dir():
        raise ValueError("project path must be an existing absolute directory")
    runtime = "windows" if os.name == "nt" else "linux"
    config = {
        "instance": {"name": name, "data_dir": str(data_dir)},
        "telegram": {"allowed_user_ids": [user_id], "allow_groups": False},
        "projects": [
            {
                "name": project_name,
                "path": str(project_path),
                "runtime": runtime,
                "agents": ["claude", "codex", "cursor"],
                "file_access": "project_only",
            }
        ],
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    location = SecretStore(data_dir).set("telegram_bot_token", token)
    print(f"Конфигурация: {config_path}")
    print(f"Bot token сохранён: {location}")
    if input("8. Настроить автозапуск? [y/N]: ").strip().casefold() == "y":
        _install_autostart(config_path, name)


def _install_autostart(config_path: Path, name: str) -> None:
    command = f'"{sys.executable}" -m ai_control.main --config "{config_path}" run'
    if os.name == "nt":
        subprocess.run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/TN",
                f"AI Control - {name}",
                "/TR",
                command,
            ],
            check=True,
        )
        print("Автозапуск создан в Task Scheduler для текущего пользователя.")
    else:
        unit_dir = Path("~/.config/systemd/user").expanduser()
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "ai-control.service"
        unit.write_text(
            "[Unit]\nDescription=AI Control\nAfter=network-online.target\n\n"
            f"[Service]\nExecStart={command}\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", unit.name], check=True)
        print("systemd user service включён.")
