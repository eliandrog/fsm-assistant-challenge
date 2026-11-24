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
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated, immutable settings."""

    db_path: Path
    ollama_host: str
    ollama_model: str | None
    row_cap: int
    query_timeout_seconds: float
    max_result_bytes: int

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

    ollama_host = get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST
    if not ollama_host.startswith(("http://", "https://")):
        raise ConfigError(
            f"OLLAMA_HOST must start with http:// or https://, got {ollama_host!r}"
        )

    return Settings(
        db_path=db_path,
        ollama_host=ollama_host.rstrip("/"),
        ollama_model=get("OLLAMA_MODEL"),
        row_cap=_positive(int, "FSM_ROW_CAP", get("FSM_ROW_CAP"), DEFAULT_ROW_CAP),
        query_timeout_seconds=_positive(
            float,
            "FSM_QUERY_TIMEOUT_SECONDS",
            get("FSM_QUERY_TIMEOUT_SECONDS"),
            DEFAULT_QUERY_TIMEOUT_SECONDS,
        ),
        max_result_bytes=_positive(
            int, "FSM_MAX_RESULT_BYTES", get("FSM_MAX_RESULT_BYTES"), DEFAULT_MAX_RESULT_BYTES
        ),
    )


def _positive[N: (int, float)](kind: type[N], name: str, raw: str | None, default: N) -> N:
    """Parse `raw` as a positive, finite number of type `kind`."""
    if raw is None:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not 0 < value < float("inf"):
        raise ConfigError(f"{name} must be greater than zero, got {raw!r}")
    return value
