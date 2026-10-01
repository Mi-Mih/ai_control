from pathlib import Path

import pytest

from ai_control.config.models import AgentConfig, load_config


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "instance:\n  name: ${INSTANCE_NAME}\n  data_dir: " + str(tmp_path) + "\ntelegram:\n  allowed_user_ids: [1]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INSTANCE_NAME", "Work PC")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "42,43")
    monkeypatch.setenv("AI_CONTROL_BOT_TOKEN", "12345678:" + "a" * 35)
    config = load_config(config_file)
    assert config.instance.name == "Work PC"
    assert config.telegram.allowed_user_ids == frozenset({42, 43})
    assert config.telegram.token is not None


def test_missing_environment_placeholder_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("UNSET_INSTANCE_NAME", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "instance:\n  name: ${UNSET_INSTANCE_NAME}\n  data_dir: " + str(tmp_path) + "\ntelegram: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="UNSET_INSTANCE_NAME"):
        load_config(config_file)


def test_dotenv_next_to_config_is_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AI_CONTROL_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    (tmp_path / ".env").write_text(
        "AI_CONTROL_BOT_TOKEN=12345678:" + "b" * 35 + "\nALLOWED_TELEGRAM_USER_IDS=77\n",
        encoding="utf-8",
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "instance:\n  name: Test\n  data_dir: " + str(tmp_path) + "\ntelegram: {}\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.telegram.token is not None
    assert config.telegram.allowed_user_ids == frozenset({77})


def test_agent_model_configuration_is_validated() -> None:
    config = AgentConfig(
        executable="codex",
        models=("model-a", "model-b"),
        default_model="model-a",
    )
    assert config.default_model == "model-a"
    with pytest.raises(ValueError, match="default_model"):
        AgentConfig(executable="codex", models=("model-a",), default_model="model-b")


def test_cursor_has_safe_default_configuration(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "instance:\n  name: Test\n  data_dir: " + str(tmp_path) + "\ntelegram: {}\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.cursor.executable == "agent"
