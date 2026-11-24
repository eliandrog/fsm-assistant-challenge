"""config: the Claude settings and how the API key is kept out of sight."""

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from fsm_assistant import config
from fsm_assistant.config import ConfigError, Settings, load_settings, parse_env_file

SECRET = "not-a-real-key-0123456789"


def load(environ: dict[str, str] | None = None, env_file: Path | None = None) -> Settings:
    return load_settings(environ or {}, env_file)


def test_model_defaults() -> None:
    settings = load()
    assert settings.anthropic_api_key is None
    assert settings.anthropic_model == "claude-opus-5-5"
    assert settings.effort == "medium"
    assert settings.max_tokens == 8000
    assert settings.model_timeout_seconds == 120.0
    assert settings.max_message_characters == 2000
    assert settings.tool_mode == "tools"


def test_model_settings_from_the_environment() -> None:
    settings = load(
        {
            "ANTHROPIC_API_KEY": SECRET,
            "ANTHROPIC_MODEL": "claude-haiku-5-5",
            "FSM_EFFORT": "low",
            "FSM_MAX_TOKENS": "4000",
            "FSM_MODEL_TIMEOUT_SECONDS": "30.5",
            "FSM_MAX_MESSAGE_CHARACTERS": "500",
            "FSM_TOOL_MODE": "structured",
        }
    )
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == SECRET
    assert settings.anthropic_model == "claude-haiku-5-5"
    assert settings.effort == "low"
    assert settings.max_tokens == 4000
    assert settings.model_timeout_seconds == 30.5
    assert settings.max_message_characters == 500
    assert settings.tool_mode == "structured"


def test_a_missing_key_does_not_fail_loading() -> None:
    assert load({"ANTHROPIC_API_KEY": "  "}).anthropic_api_key is None


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("FSM_EFFORT", "huge", "must be one of 'low', 'medium', 'high', 'xhigh' or 'max'"),
        ("FSM_TOOL_MODE", "json", "must be one of 'tools' or 'structured'"),
        ("FSM_MAX_TOKENS", "0", "greater than zero"),
        ("FSM_MAX_TOKENS", "8k", "must be a number"),
        ("FSM_MODEL_TIMEOUT_SECONDS", "inf", "greater than zero"),
        ("FSM_MAX_MESSAGE_CHARACTERS", "-1", "greater than zero"),
    ],
)
def test_invalid_model_settings_name_the_variable(name: str, value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=name) as error:
        load({name: value})
    assert message in str(error.value)


def test_the_key_is_not_in_repr_str_or_dump(caplog: pytest.LogCaptureFixture) -> None:
    settings = load({"ANTHROPIC_API_KEY": SECRET})
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert SECRET not in repr(settings.model_dump())
    assert SECRET not in f"{settings.anthropic_api_key}"
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("settings: %s", settings)
    assert SECRET not in caplog.text
    assert "**********" in caplog.text


def test_the_key_is_not_in_an_error_about_another_setting() -> None:
    with pytest.raises(ConfigError) as error:
        load({"ANTHROPIC_API_KEY": SECRET, "FSM_EFFORT": "huge"})
    assert SECRET not in str(error.value)
    fields = load({"ANTHROPIC_API_KEY": SECRET}).model_dump()
    with pytest.raises(ValidationError) as invalid:
        Settings.model_validate({**fields, "anthropic_api_key": SecretStr(SECRET), "row_cap": 0})
    assert SECRET not in str(invalid.value)


def test_env_example_names_every_variable_and_holds_no_secret() -> None:
    values = parse_env_file((config.PROJECT_ROOT / ".env.example").read_text())
    assert {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "FSM_EFFORT",
        "FSM_MAX_TOKENS",
        "FSM_MODEL_TIMEOUT_SECONDS",
        "FSM_MAX_MESSAGE_CHARACTERS",
        "FSM_TOOL_MODE",
        "FSM_DB_PATH",
        "FSM_ROW_CAP",
        "FSM_QUERY_TIMEOUT_SECONDS",
        "FSM_MAX_RESULT_BYTES",
    } == values.keys()
    assert all(value == "" for value in values.values())
    assert load_settings({}, config.PROJECT_ROOT / ".env.example") == load()


def test_the_earlier_provider_settings_are_gone() -> None:
    assert not {"ollama_host", "ollama_model"} & Settings.model_fields.keys()
    assert not hasattr(Settings, "require_model")
