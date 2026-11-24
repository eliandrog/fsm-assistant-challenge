"""Configuration.

Responsibility: the single place other modules get settings from. Settings
come from environment variables, with defaults, and are validated once into
an immutable `Settings` value.

A `.env` file in the project root is also read, by a deliberately small
parser (`parse_env_file`), so the project needs no dotenv dependency. Real
environment variables win over `.env`, and an empty value counts as unset
(an empty environment variable does not hide a value in `.env`), so
`.env.example` can be copied as it is.

Loading never touches the database or the model server: a missing database
file is reported by `db` when a query runs, and a missing model name by
`Settings.require_model()` when a real model is asked for. That keeps the
offline parts (tests, schema context) usable without a model.

`Settings` holds the rules for each value; `load_settings` reports a broken
one as a `ConfigError` that names the environment variable it came from.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from fsm_assistant.db import DEFAULT_MAX_RESULT_BYTES

# src/fsm_assistant/config.py -> project root. Valid for a source checkout,
# which is how `uv sync` installs the package (editable).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "data.db"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
# Enough rows to read a result table; the cap bounds memory and output size.
DEFAULT_ROW_CAP = 1000
# The slowest sensible query measured on the real data (a six-table join with
# GROUP BY) takes about 1.3 s; a mistaken join can run for minutes.
DEFAULT_QUERY_TIMEOUT_SECONDS = 15.0


class ConfigError(ValueError):
    """A setting is missing or has an invalid value."""


class Settings(BaseModel):
    """Validated, immutable settings.

    A number may be given as text, as it comes from the environment; it is
    read with Python's own `int()` or `float()`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    db_path: Path
    ollama_host: str  # without a trailing slash
    ollama_model: str | None
    row_cap: int
    query_timeout_seconds: float
    max_result_bytes: int

    @field_validator("ollama_host")
    @classmethod
    def _host_is_an_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("row_cap", "max_result_bytes", mode="before")
    @classmethod
    def _whole_number(cls, value: object) -> object:
        return _number(int, value)

    @field_validator("query_timeout_seconds", mode="before")
    @classmethod
    def _decimal_number(cls, value: object) -> object:
        return _number(float, value)

    @field_validator("row_cap", "query_timeout_seconds", "max_result_bytes")
    @classmethod
    def _positive_and_finite[N: (int, float)](cls, value: N) -> N:
        if not 0 < value < float("inf"):
            raise ValueError("must be greater than zero")
        return value

    def require_model(self) -> str:
        """Return the model tag, or fail if it is not configured.

        Called only when a real model is about to be used, so everything
        that runs without a model does not need OLLAMA_MODEL.
        """
        if not self.ollama_model:
            raise ConfigError(
                "OLLAMA_MODEL is not set. Set it to an installed Ollama model "
                "tag (for example qwen3:14b) in the environment or in .env."
            )
        return self.ollama_model


# Settings field -> the environment variable it is read from.
_VARIABLES = {
    "db_path": "FSM_DB_PATH",
    "ollama_host": "OLLAMA_HOST",
    "ollama_model": "OLLAMA_MODEL",
    "row_cap": "FSM_ROW_CAP",
    "query_timeout_seconds": "FSM_QUERY_TIMEOUT_SECONDS",
    "max_result_bytes": "FSM_MAX_RESULT_BYTES",
}


def parse_env_file(text: str) -> dict[str, str]:
    """Parse simple KEY=VALUE lines.

    Supported: blank lines, `#` comment lines, and one pair of matching
    quotes around a value. Not supported, on purpose: `export`, inline
    comments, multi-line values, variable expansion.
    """
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or " " in key:
            raise ConfigError(f".env line {number}: expected KEY=VALUE, got {raw!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = DEFAULT_ENV_FILE,
) -> Settings:
    """Build `Settings` from `environ` (default: `os.environ`) and `env_file`.

    A non-empty value in `environ` takes precedence over the file. A missing
    file is fine; pass `env_file=None` to skip it. Raises `ConfigError` on an
    invalid value.
    """
    values: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        # utf-8-sig: a byte-order mark would otherwise become part of the first key.
        values.update(parse_env_file(env_file.read_text(encoding="utf-8-sig")))
    environment = os.environ if environ is None else environ
    values.update({name: value for name, value in environment.items() if value.strip()})

    def get(name: str) -> str | None:
        return values.get(name, "").strip() or None

    db_path = Path(get("FSM_DB_PATH") or DEFAULT_DB_PATH).expanduser()
    if not db_path.is_absolute():
        # Relative to the project, not the shell's current directory, so the
        # same .env works wherever the command is run from.
        db_path = PROJECT_ROOT / db_path

    fields: dict[str, object] = {
        "db_path": db_path,
        "ollama_host": get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST,
        "ollama_model": get("OLLAMA_MODEL"),
        "row_cap": get("FSM_ROW_CAP") or DEFAULT_ROW_CAP,
        "query_timeout_seconds": get("FSM_QUERY_TIMEOUT_SECONDS")
        or DEFAULT_QUERY_TIMEOUT_SECONDS,
        "max_result_bytes": get("FSM_MAX_RESULT_BYTES") or DEFAULT_MAX_RESULT_BYTES,
    }
    try:
        return Settings.model_validate(fields)
    except ValidationError as error:
        # The first error, in field order, with the variable's name in place of the field's.
        first = error.errors()[0]
        field = str(first["loc"][0])
        reason = first.get("ctx", {}).get("error", first["msg"])
        raise ConfigError(f"{_VARIABLES[field]} {reason}, got {fields[field]!r}") from None


def _number(kind: type[int] | type[float], value: object) -> object:
    """Read text as a number of type `kind`; anything else is left for the field's own check."""
    if not isinstance(value, str):
        return value
    try:
        return kind(value)
    except ValueError:
        raise ValueError("must be a number") from None
