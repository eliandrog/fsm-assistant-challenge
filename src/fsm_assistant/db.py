"""Read-only database access.

Responsibility: the only module that opens the database, and the single
place a query runs. `run_query` executes one statement and returns a capped
result. It assumes the SQL may be wrong or hostile (it will be model-written)
and enforces its limits in the engine, without parsing or rewriting the SQL:

- Read-only, three independent layers. The file is opened with the SQLite
  URI `mode=ro`; `PRAGMA query_only` blocks writes to anything else the
  connection could reach; and an authorizer callback denies, at statement
  compile time, every action that is not a read. The authorizer is what
  stops `ATTACH` and temp tables, which `mode=ro` alone allows.
- Row cap. At most `row_cap` rows are fetched; one extra row is requested
  only to learn whether the result was truncated.
- Timeout. SQLite has no statement timeout (the `timeout` argument of
  `connect` only waits for locks), so a progress handler interrupts the
  statement once a deadline has passed.

A connection lives for one query. Opening SQLite is cheap, and it means no
state (attached databases, pragmas, temp objects) can carry over between
statements.
"""

import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# How often (in SQLite virtual-machine instructions) the deadline is checked.
# Low enough to stop a runaway query within milliseconds of the deadline,
# high enough that the Python callback costs nothing measurable.
_PROGRESS_INTERVAL = 10_000

# Schema-reading pragmas that `schema_context` needs. Every other PRAGMA is
# denied, including the ones that change connection state (`query_only`).
_READ_ONLY_PRAGMAS = frozenset({"table_info", "foreign_key_list"})

_READ_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,  # recursive CTEs
    }
)

# Names for the denied actions most likely to appear in an error message.
_ACTION_NAMES = {
    getattr(sqlite3, f"SQLITE_{name}"): name.replace("_", " ")
    for name in (
        "INSERT",
        "UPDATE",
        "DELETE",
        "CREATE_TABLE",
        "CREATE_TEMP_TABLE",
        "CREATE_INDEX",
        "CREATE_VIEW",
        "CREATE_TRIGGER",
        "DROP_TABLE",
        "DROP_INDEX",
        "DROP_VIEW",
        "DROP_TRIGGER",
        "ALTER_TABLE",
        "ATTACH",
        "DETACH",
        "PRAGMA",
        "TRANSACTION",
        "SAVEPOINT",
        "ANALYZE",
        "REINDEX",
    )
}


class DbError(Exception):
    """Base class for every error raised by this module."""


class DatabaseNotFoundError(DbError):
    """The database file does not exist."""


class ReadOnlyViolation(DbError):
    """The statement tried to do something other than read."""


class QueryTimeout(DbError):
    """The statement was interrupted because it ran past the timeout."""


class QueryError(DbError):
    """SQLite rejected the statement (syntax error, unknown column, ...).

    `sqlite_message` is SQLite's own message, kept verbatim so it can be
    handed back to the model for a repair attempt.
    """

    def __init__(self, sqlite_message: str) -> None:
        super().__init__(sqlite_message)
        self.sqlite_message = sqlite_message


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Rows returned by one statement, capped at the row cap."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool  # True when more rows existed than the cap allowed
    elapsed_seconds: float

    @property
    def row_count(self) -> int:
        """Number of rows returned, which is at most the row cap."""
        return len(self.rows)


def run_query(
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] = (),
    *,
    db_path: Path,
    row_cap: int,
    timeout_seconds: float,
) -> QueryResult:
    """Run one SQL statement read-only and return at most `row_cap` rows.

    Raises `ReadOnlyViolation` for anything that is not a read,
    `QueryTimeout` when the statement runs past `timeout_seconds`,
    `QueryError` for any other SQLite error (including more than one
    statement), and `DatabaseNotFoundError` when the file is missing.
    """
    if row_cap < 1 or timeout_seconds <= 0:
        raise ValueError("row_cap and timeout_seconds must be positive")

    denied: list[str] = []
    connection = _connect(db_path, denied)
    started = time.monotonic()
    deadline = started + timeout_seconds
    connection.set_progress_handler(
        lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_INTERVAL
    )
    try:
        cursor = connection.execute(sql, params)
        # The statement runs lazily as rows are fetched, so the deadline
        # covers the fetch too. One row beyond the cap detects truncation.
        fetched = cursor.fetchmany(row_cap + 1)
        columns = tuple(column[0] for column in cursor.description or ())
    except (sqlite3.Error, sqlite3.Warning) as error:
        raise _translate(error, denied, timeout_seconds) from error
    finally:
        connection.close()

    return QueryResult(
        columns=columns,
        rows=tuple(fetched[:row_cap]),
        truncated=len(fetched) > row_cap,
        elapsed_seconds=time.monotonic() - started,
    )


def _connect(db_path: Path, denied: list[str]) -> sqlite3.Connection:
    """Open `db_path` read-only; denied actions are appended to `denied`."""
    if not db_path.is_file():
        raise DatabaseNotFoundError(
            f"Database file not found: {db_path}. The repository ships it at "
            "data/data.db; set FSM_DB_PATH if it lives somewhere else."
        )
    # as_uri() percent-encodes characters such as '?' and '#' in the path.
    # isolation_level=None stops the sqlite3 module opening transactions of
    # its own. `immutable=1` is deliberately not used, see docs/DATA.md.
    try:
        connection = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None
        )
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as error:
        raise QueryError(f"cannot open {db_path} read-only: {error}") from error
    connection.set_authorizer(_authorizer(denied))
    return connection


def _is_read(action: int, argument: str | None) -> bool:
    """Whether an authorizer action only reads. `argument` is the pragma name."""
    if action == sqlite3.SQLITE_PRAGMA:
        return argument in _READ_ONLY_PRAGMAS
    return action in _READ_ACTIONS


def _authorizer(denied: list[str]) -> Callable[..., int]:
    """Build the authorizer callback: allow reads, deny everything else."""

    def authorize(action: int, arg1: str | None, *_: object) -> int:
        if _is_read(action, arg1):
            return sqlite3.SQLITE_OK
        name = _ACTION_NAMES.get(action, f"action code {action}")
        denied.append(f"{name} {arg1}" if arg1 else name)
        return sqlite3.SQLITE_DENY

    return authorize


def _translate(error: Exception, denied: list[str], timeout_seconds: float) -> DbError:
    """Map a sqlite3 exception to this module's exception types."""
    # Extended result codes keep the primary code in the low byte.
    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    if code == sqlite3.SQLITE_AUTH or denied:
        detail = denied[0] if denied else str(error)
        return ReadOnlyViolation(
            f"Only read-only queries are allowed; denied: {detail}."
        )
    if code == sqlite3.SQLITE_READONLY:
        return ReadOnlyViolation(f"Only read-only queries are allowed; SQLite: {error}.")
    if code == sqlite3.SQLITE_INTERRUPT:
        return QueryTimeout(
            f"Query interrupted after exceeding the {timeout_seconds:g}s timeout."
        )
    return QueryError(str(error))
