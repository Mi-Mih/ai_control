from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from ai_control.core.models import AgentKind, FileAccessMode, Runtime

_ENV = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


class InstanceConfig(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    data_dir: Path = Path("~/.ai-control").expanduser()
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    max_parallel_tasks: int = Field(default=4, ge=1, le=32)


class TelegramConfig(BaseModel):
    token: SecretStr | None = None
    allowed_user_ids: frozenset[int] = frozenset()
    allow_groups: bool = False
    requests_per_minute: int = Field(default=30, ge=5, le=300)
    progress_interval_seconds: float = Field(default=1.5, ge=1.0, le=10.0)

    @field_validator("allowed_user_ids")
    @classmethod
    def positive_ids(cls, value: frozenset[int]) -> frozenset[int]:
        if any(item <= 0 for item in value):
            raise ValueError("Telegram user IDs must be positive")
        return value


class ProjectConfig(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    path: Path
    runtime: Runtime
    wsl_distribution: str | None = None
    agents: frozenset[AgentKind] = frozenset({AgentKind.CLAUDE, AgentKind.CODEX, AgentKind.CURSOR})
    file_access: FileAccessMode = FileAccessMode.PROJECT_ONLY
    approved_paths: tuple[Path, ...] = ()
    worktrees_enabled: bool = True

    @model_validator(mode="after")
    def validate_runtime(self) -> "ProjectConfig":
        if self.runtime == Runtime.WSL and not self.wsl_distribution:
            raise ValueError("wsl_distribution is required for WSL projects")
        if self.runtime != Runtime.WSL and self.wsl_distribution:
            raise ValueError("wsl_distribution is only valid for WSL projects")
        return self


class ExportConfig(BaseModel):
    approved_paths: tuple[Path, ...] = ()
    max_file_size_mb: int = Field(default=45, ge=1, le=2000)


class AgentConfig(BaseModel):
    executable: str
    start_timeout_seconds: float = Field(default=20, ge=1, le=120)
    turn_timeout_seconds: float = Field(default=3600, ge=30, le=86400)
    models: tuple[str, ...] = ()
    default_model: str | None = None

    @model_validator(mode="after")
    def validate_models(self) -> "AgentConfig":
        model_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
        if any(not model_pattern.fullmatch(model) for model in self.models):
            raise ValueError("model names may contain only letters, numbers, dots, underscores, colons, and dashes")
        if len(self.models) != len(set(self.models)):
            raise ValueError("model names must be unique")
        if self.default_model and not model_pattern.fullmatch(self.default_model):
            raise ValueError("default_model has an invalid name")
        if self.default_model and self.models and self.default_model not in self.models:
            raise ValueError("default_model must be included in models")
        return self


class AppConfig(BaseModel):
    instance: InstanceConfig
    telegram: TelegramConfig
    projects: list[ProjectConfig] = []
    telegram_export: ExportConfig = ExportConfig()
    codex: AgentConfig = AgentConfig(executable="codex")
    claude: AgentConfig = AgentConfig(executable="claude")
    cursor: AgentConfig = AgentConfig(executable="agent")

    @model_validator(mode="after")
    def unique_projects(self) -> "AppConfig":
        names = [project.name.casefold() for project in self.projects]
        if len(names) != len(set(names)):
            raise ValueError("project names must be unique")
        return self


def _expand_env(value: object) -> object:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ValueError(f"required environment variable is missing: {name}")
            return os.environ[name]

        return _ENV.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def load_config(path: Path) -> AppConfig:
    load_dotenv(path.parent / ".env", override=False)
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    raw = _expand_env(raw)
    telegram = raw.setdefault("telegram", {})
    if token := os.getenv("AI_CONTROL_BOT_TOKEN"):
        telegram["token"] = token
    if allowed := os.getenv("ALLOWED_TELEGRAM_USER_IDS"):
        telegram["allowed_user_ids"] = [int(item.strip()) for item in allowed.split(",")]
    instance = raw.setdefault("instance", {})
    if name := os.getenv("INSTANCE_NAME"):
        instance["name"] = name
    config = AppConfig.model_validate(raw)
    if config.telegram.token is None:
        from ai_control.security.secrets import SecretStore

        token = SecretStore(config.instance.data_dir.expanduser()).get("telegram_bot_token")
        if token:
            config.telegram.token = SecretStr(token)
    return config
