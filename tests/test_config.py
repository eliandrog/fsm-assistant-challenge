"""config: defaults, environment overrides, .env parsing, validation."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from fsm_assistant import config, db
from fsm_assistant.config import ConfigError, Settings, load_settings, parse_env_file


def load(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> Settings:
    """load_settings isolated from the developer's real environment and .env."""
    return load_settings(environ or {}, env_file)


def test_defaults() -> None:
    settings = load()
    assert settings.db_path == config.PROJECT_ROOT / "data" / "data.db"
    assert settings.db_path.is_absolute()
    assert settings.row_cap == config.DEFAULT_ROW_CAP
    assert settings.query_timeout_seconds == config.DEFAULT_QUERY_TIMEOUT_SECONDS
    assert settings.max_result_bytes == db.DEFAULT_MAX_RESULT_BYTES == 5_000_000


def test_project_root_is_the_repository() -> None:
    assert (config.PROJECT_ROOT / "pyproject.toml").is_file()


def test_environment_overrides() -> None:
    settings = load(
        {
            "FSM_DB_PATH": "/tmp/other.db",
            "FSM_ROW_CAP": "50",
            "FSM_QUERY_TIMEOUT_SECONDS": "2.5",
            "FSM_MAX_RESULT_BYTES": "20000",
        }
    )
    assert settings.db_path == Path("/tmp/other.db")
    assert settings.row_cap == 50
    assert settings.query_timeout_seconds == 2.5
    assert settings.max_result_bytes == 20_000


def test_relative_db_path_is_resolved_against_the_project_not_the_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load({"FSM_DB_PATH": "data/copy.db"}).db_path == config.PROJECT_ROOT / "data/copy.db"
    assert load().db_path == config.PROJECT_ROOT / "data" / "data.db"


def test_empty_values_count_as_unset() -> None:
    settings = load({"FSM_DB_PATH": "", "ANTHROPIC_MODEL": "  ", "FSM_ROW_CAP": ""})
    assert settings.db_path == config.DEFAULT_DB_PATH
    assert settings.anthropic_model == config.DEFAULT_ANTHROPIC_MODEL
    assert settings.row_cap == config.DEFAULT_ROW_CAP


def test_settings_are_immutable() -> None:
    with pytest.raises(ValidationError):
        load().row_cap = 5  # type: ignore[misc]  # assigning to a frozen field is what is tested


def test_settings_reject_unknown_fields() -> None:
    fields = load().model_dump()
    assert Settings.model_validate(fields) == load()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Settings.model_validate({**fields, "row_limit": 5})


def test_the_model_holds_the_rules_for_values_built_in_code() -> None:
    fields = load().model_dump()
    for field, value in [
        ("row_cap", 0),
        ("row_cap", 1.5),
        ("query_timeout_seconds", float("inf")),
        ("max_result_bytes", -1),
    ]:
        with pytest.raises(ValidationError, match=field):
            Settings.model_validate({**fields, field: value})


def test_the_first_invalid_variable_in_field_order_is_the_one_named() -> None:
    with pytest.raises(ConfigError) as error:
        load({"FSM_MAX_RESULT_BYTES": "0", "FSM_ROW_CAP": "many"})
    assert str(error.value) == "FSM_ROW_CAP must be a number, got 'many'"
    with pytest.raises(ConfigError) as error:
        load({"FSM_MAX_RESULT_BYTES": "0"})
    assert str(error.value) == "FSM_MAX_RESULT_BYTES must be greater than zero, got '0'"


def test_missing_db_file_is_not_an_error_at_load_time() -> None:
    assert load({"FSM_DB_PATH": "/nonexistent/x.db"}).db_path == Path("/nonexistent/x.db")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("FSM_ROW_CAP", "many", "must be a number"),
        ("FSM_ROW_CAP", "1.5", "must be a number"),
        ("FSM_ROW_CAP", "0", "greater than zero"),
        ("FSM_ROW_CAP", "-3", "greater than zero"),
        ("FSM_QUERY_TIMEOUT_SECONDS", "soon", "must be a number"),
        ("FSM_QUERY_TIMEOUT_SECONDS", "0", "greater than zero"),
        ("FSM_QUERY_TIMEOUT_SECONDS", "inf", "greater than zero"),
        ("FSM_QUERY_TIMEOUT_SECONDS", "nan", "greater than zero"),
        ("FSM_MAX_RESULT_BYTES", "5MB", "must be a number"),
        ("FSM_MAX_RESULT_BYTES", "0", "greater than zero"),
    ],
)
def test_invalid_values_name_the_variable(name: str, value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=name) as error:
        load({name: value})
    assert message in str(error.value)


def test_parse_env_file() -> None:
    text = """
        # a comment
        ANTHROPIC_MODEL=claude-haiku-5-5
        FSM_ROW_CAP = 25
        QUOTED="with spaces"
        SINGLE='x=y'
        EMPTY=
    """
    assert parse_env_file(text) == {
        "ANTHROPIC_MODEL": "claude-haiku-5-5",
        "FSM_ROW_CAP": "25",
        "QUOTED": "with spaces",
        "SINGLE": "x=y",
        "EMPTY": "",
    }


@pytest.mark.parametrize("line", ["JUST_A_WORD", "=value", "export FOO=bar"])
def test_parse_env_file_rejects_other_syntax(line: str) -> None:
    with pytest.raises(ConfigError, match="line 2"):
        parse_env_file(f"OK=1\n{line}\n")


def test_env_file_is_read_and_the_environment_wins(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FSM_ROW_CAP=25\nANTHROPIC_MODEL=from-file\n")
    assert load(env_file=env_file).row_cap == 25
    settings = load({"FSM_ROW_CAP": "7"}, env_file)
    assert settings.row_cap == 7
    assert settings.anthropic_model == "from-file"


def test_missing_env_file_is_ignored(tmp_path: Path) -> None:
    assert load(env_file=tmp_path / "absent.env").row_cap == config.DEFAULT_ROW_CAP


def test_os_environ_is_the_default_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FSM_ROW_CAP", "3")
    assert load_settings(env_file=None).row_cap == 3


def test_env_example_is_a_valid_env_file() -> None:
    values = parse_env_file((config.PROJECT_ROOT / ".env.example").read_text())
    assert {"FSM_DB_PATH", "FSM_ROW_CAP",
            "FSM_QUERY_TIMEOUT_SECONDS", "FSM_MAX_RESULT_BYTES"} <= values.keys()
    load_settings({}, config.PROJECT_ROOT / ".env.example")


def test_an_empty_environment_variable_does_not_hide_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FSM_ROW_CAP=25\nANTHROPIC_MODEL=from-file\n")
    settings = load({"FSM_ROW_CAP": "", "ANTHROPIC_MODEL": "   "}, env_file)
    assert settings.row_cap == 25
    assert settings.anthropic_model == "from-file"


def test_env_file_with_a_byte_order_mark(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xef\xbb\xbfFSM_ROW_CAP=25\nANTHROPIC_MODEL=from-file\n")
    settings = load(env_file=env_file)
    assert settings.row_cap == 25
    assert settings.anthropic_model == "from-file"


def test_logging_and_tracing_defaults() -> None:
    settings = load()
    assert (settings.log_file, settings.log_level) == (True, "INFO")
    assert settings.log_dir == config.DEFAULT_LOG_DIR and settings.log_dir.is_absolute()
    assert settings.langsmith_tracing is False and settings.langsmith_api_key is None
    assert settings.langsmith_endpoint == "https://api.smith.langchain.com"
    assert settings.langsmith_project is None and settings.langsmith_agent_id is None
    assert settings.langsmith_agent_environment is None
    assert settings.trace_system_prompt == "hash"


def test_logging_and_tracing_settings_are_read_from_their_variables() -> None:
    settings = load(
        {
            "FSM_LOG_FILE": "OFF",
            "FSM_LOG_DIR": "var/log",
            "FSM_LOG_LEVEL": "debug",
            "LANGSMITH_TRACING": "True",
            "LANGSMITH_API_KEY": "lsv2_not-a-real-key",
            "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com/",
            "LANGSMITH_PROJECT": "mine",
            "LANGSMITH_AGENT_ID": "fsm",
            "LANGSMITH_AGENT_ENVIRONMENT": "local",
            "FSM_TRACE_SYSTEM_PROMPT": "full",
            # Names of LangChain's that are not this project's settings.
            "LANGCHAIN_TRACING_V2": "true",
            "LANGCHAIN_API_KEY": "another",
        }
    )
    assert (settings.log_file, settings.log_level) == (False, "DEBUG")
    assert settings.log_dir == config.PROJECT_ROOT / "var" / "log"
    assert settings.langsmith_tracing is True
    assert settings.langsmith_endpoint == "https://eu.api.smith.langchain.com"
    assert (settings.langsmith_project, settings.trace_system_prompt) == ("mine", "full")
    assert (settings.langsmith_agent_id, settings.langsmith_agent_environment) == ("fsm", "local")
    assert settings.langsmith_api_key is not None
    assert settings.langsmith_api_key.get_secret_value() == "lsv2_not-a-real-key"
    assert "lsv2_not-a-real-key" not in repr(settings) + repr(settings.model_dump())
    assert "another" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("FSM_LOG_FILE", "yes", "FSM_LOG_FILE must be on or off, got 'yes'"),
        ("FSM_LOG_LEVEL", "LOUD", "FSM_LOG_LEVEL must be one of"),
        ("LANGSMITH_TRACING", "1", "LANGSMITH_TRACING must be true or false, got '1'"),
        ("LANGSMITH_ENDPOINT", "smith.example", "LANGSMITH_ENDPOINT must start with https://"),
        ("FSM_TRACE_SYSTEM_PROMPT", "all", "FSM_TRACE_SYSTEM_PROMPT must be one of"),
    ],
)
def test_invalid_logging_and_tracing_values_name_the_variable(
    name: str, value: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load({name: value})
