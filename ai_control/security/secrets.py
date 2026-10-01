from __future__ import annotations

import json
import os
from pathlib import Path

SERVICE_NAME = "ai-control"


class SecretStore:
    """Uses the OS keyring first and a mode-0600 local file only as a documented fallback."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.fallback = data_dir / "secrets.json"

    def set(self, name: str, value: str) -> str:
        try:
            import keyring

            keyring.set_password(SERVICE_NAME, name, value)
            return "system keyring"
        except Exception:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            values = self._read_fallback()
            values[name] = value
            fd = os.open(self.fallback, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(values, handle)
            try:
                self.fallback.chmod(0o600)
            except OSError:
                pass
            return "protected local file"

    def get(self, name: str) -> str | None:
        try:
            import keyring

            value = keyring.get_password(SERVICE_NAME, name)
            if value:
                return value
        except Exception:
            pass
        return self._read_fallback().get(name)

    def delete(self, name: str) -> None:
        try:
            import keyring

            keyring.delete_password(SERVICE_NAME, name)
        except Exception:
            pass
        values = self._read_fallback()
        if name in values:
            del values[name]
            fd = os.open(self.fallback, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(values, handle)

    def _read_fallback(self) -> dict[str, str]:
        try:
            value = json.loads(self.fallback.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
