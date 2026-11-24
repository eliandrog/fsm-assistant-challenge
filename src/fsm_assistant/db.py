"""Read-only database access.

Responsibility: the only module that opens the database, and the single
place a query runs. `run_query` executes one statement and returns a capped
result; `inspect_query` compiles one statement without running it and
reports what it would read. Both assume the SQL may be wrong or hostile (it
will be model-written) and enforce their limits in the engine, without
parsing or rewriting the SQL:

- Read-only, three independent layers. The file is opened with the SQLite
  URI `mode=ro`; `PRAGMA query_only` blocks writes to anything else the
  connection could reach; and an authorizer callback denies, at statement
  compile time, every action that is not a read. The authorizer is what
  stops `ATTACH` and temp tables, which `mode=ro` alone allows.
- Row cap. At most `row_cap` rows are fetched; one extra row is requested
  only to learn whether the result was truncated.
- Result size. Rows are fetched one at a time and their values added up; a
  result over `max_result_bytes` raises `ResultTooLarge`. The row cap alone
  does not bound memory, since a row can be wide and its values long.
- Timeout. SQLite has no statement timeout (the `timeout` argument of
  `connect` only waits for locks), so a progress handler interrupts the
  statement once a deadline has passed.
- Value size and width. No single string or blob may exceed
  `MAX_VALUE_BYTES`, so one heavy operation (`zeroblob(1000000000)`) fails
  at once instead of allocating between two deadline checks, and a result
  has at most `MAX_COLUMNS` columns. Together they bound one row, which is
  the unit the result-size budget is checked in.
- Unknown names are errors. SQLite's fallback that reads an unknown
  double-quoted name as a string literal is switched off, so `"amount" > 1000`
  fails to compile instead of comparing text with a number.

What is not bounded here. The timeout and the size limits apply while a
statement runs, not while it is compiled, and compiling is not free: SQLite
copies a common table expression at every reference to it, so a short
statement can take seconds and gigabytes to compile. `sql_guard` bounds that
from the text before it calls `inspect_query`; model-written SQL must not
reach either function without passing the guard. Memory SQLite uses inside
a running statement (a sort, a DISTINCT over long values) is bounded only
by the timeout and the value cap.

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

from pydantic import BaseModel, ConfigDict

# How often (in SQLite virtual-machine instructions) the deadline is checked.
# Low enough to stop a runaway query within milliseconds of the deadline,
# high enough that the Python callback costs nothing measurable.
_PROGRESS_INTERVAL = 10_000

# Largest string or blob, in bytes, a statement may produce (also bounds the
# SQL text itself). The longest value in the data is 51 characters.
MAX_VALUE_BYTES = 100_000

# Most columns in a result (also in a GROUP BY or ORDER BY). The seven
# tables have 40 columns between them.
MAX_COLUMNS = 100

# Default budget for the values of one result. A full page of an ordinary
# result (1,000 rows of a six-table `SELECT *`) is under 1 MB.
DEFAULT_MAX_RESULT_BYTES = 5_000_000

# What a value that is not text or a blob counts as (integer, real, NULL).
_SCALAR_BYTES = 8

# The authorizer action that opens every SELECT, for callers of `inspect_query`.
SELECT_ACTION = sqlite3.SQLITE_SELECT

# Compiles the statement and returns its plan; nothing is read from tables.
# `EXPLAIN QUERY PLAN` rather than `EXPLAIN`, so that neither a statement that
# starts with EXPLAIN nor one that starts with QUERY PLAN can compile.
_COMPILE_ONLY = "EXPLAIN QUERY PLAN "

# Read-only errors that describe the file's surroundings, not the statement.
_UNREADABLE_CODES = frozenset(
    {
        sqlite3.SQLITE_READONLY_DIRECTORY,
        sqlite3.SQLITE_READONLY_CANTINIT,
        sqlite3.SQLITE_READONLY_CANTLOCK,
        sqlite3.SQLITE_READONLY_RECOVERY,
    }
)

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

# Names for the actions most likely to appear in an error message.
_ACTION_NAMES = {
    getattr(sqlite3, f"SQLITE_{name}"): name.replace("_", " ")
    for name in (
        "SELECT",
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


class DatabaseAccessError(DbError):
    """The database file exists but cannot be read where it is.

    A fault of the environment, never of the statement: the usual cause is a
    WAL-mode database in a directory that is not writable.
    """


class ReadOnlyViolation(DbError):
    """The statement tried to do something other than read."""


class QueryTimeout(DbError):
    """The statement was interrupted because it ran past the timeout."""


class QueryError(DbError):
    """The statement is at fault (syntax error, unknown column, ...).

    `sqlite_message` is SQLite's own message where it has one, kept verbatim
    so it can be handed back to the model for a repair attempt.
    """

    def __init__(self, sqlite_message: str) -> None:
        super().__init__(sqlite_message)
        self.sqlite_message = sqlite_message


class ResultTooLarge(QueryError):
    """The rows fetched hold more bytes than the result budget allows.

    A `QueryError` because the statement is what has to change (fewer or
    shorter columns, or an aggregate), so whatever hands a `QueryError`
    back for repair handles this one too.
    """


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


class TableRead(BaseModel):
    """One table a statement reads, and the columns of it that the statement names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str  # lowercased
    columns: frozenset[str]  # empty when no column was named: `SELECT count(*) FROM cards`


class QueryInspection(BaseModel):
    """What one statement would do, as SQLite's authorizer reported it at compile time.

    Table names are lowercased: SQLite reports a table as the statement
    spelled it when no column of it is named (`SELECT count(*) FROM CARDS`).
    In that case it can also report the name of a CTE, so `reads` can hold a
    CTE name with no columns; a read that names a column always reports the
    real table underneath.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    actions: tuple[int, ...]  # authorizer action codes, in the order reported
    reads: tuple[TableRead, ...]  # one per table, sorted by table name
    functions: frozenset[str]  # lowercased

    @property
    def tables(self) -> frozenset[str]:
        """Names of the tables read."""
        return frozenset(read.table for read in self.reads)

    def columns_of(self, table: str) -> frozenset[str]:
        """Columns of `table` (any case) the statement names; empty when it reads none."""
        name = table.lower()
        return next((read.columns for read in self.reads if read.table == name), frozenset())

    @property
    def first_action(self) -> str | None:
        """Name of the first action (`SELECT` for a query), None when nothing was reported."""
        return _action_name(self.actions[0]) if self.actions else None


def run_query(
    sql: str,
    # Any: a bound value is whatever SQLite stores (int, float, str, bytes, None).
    params: Sequence[Any] | Mapping[str, Any] = (),
    *,
    db_path: Path,
    row_cap: int,
    timeout_seconds: float,
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
) -> QueryResult:
    """Run one SQL statement read-only and return at most `row_cap` rows.

    Raises `ReadOnlyViolation` for anything that is not a read,
    `QueryTimeout` when the statement runs past `timeout_seconds`,
    `ResultTooLarge` when the rows returned hold more than
    `max_result_bytes`, `QueryError` for any other fault of the statement
    (a SQLite error, more than one statement, an empty or comment-only one,
    text that is not valid Unicode), `DatabaseNotFoundError` when the file
    is missing and `DatabaseAccessError` when it cannot be read.
    """
    if row_cap < 1 or timeout_seconds <= 0 or max_result_bytes < 1:
        raise ValueError("row_cap, timeout_seconds and max_result_bytes must be positive")

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
        # covers the fetch too. Rows are taken one at a time so the size
        # budget stops the fetch at the row that crosses it.
        rows: list[tuple[Any, ...]] = []
        size = 0
        truncated = False
        for row in cursor:
            if len(rows) == row_cap:
                truncated = True  # one row beyond the cap, only to detect truncation
                break
            size += sum(_value_bytes(value) for value in row)
            if size > max_result_bytes:
                raise ResultTooLarge(
                    f"The result is too large: over {max_result_bytes:,} bytes by row "
                    f"{len(rows) + 1}. Select fewer or shorter columns, or aggregate."
                )
            rows.append(row)
        if cursor.description is None:
            raise QueryError("empty statement")
        columns = tuple(column[0] for column in cursor.description)
    except (sqlite3.Error, sqlite3.Warning) as error:
        raise _translate(error, denied, db_path, timeout_seconds) from error
    except UnicodeError as error:
        raise _not_unicode(error) from error
    finally:
        connection.close()

    return QueryResult(
        columns=columns,
        rows=tuple(rows),
        truncated=truncated,
        elapsed_seconds=time.monotonic() - started,
    )


def inspect_query(sql: str, *, db_path: Path) -> QueryInspection:
    """Compile one statement without running it and return what it would do.

    The statement is compiled as `EXPLAIN QUERY PLAN <sql>` on the same
    read-only connection `run_query` uses, with the authorizer recording
    every action as well as denying the ones that are not reads. No row of
    any table is read, so the cost does not depend on the data. It does
    depend on the statement: compiling has no timeout, and a statement that
    refers to its CTEs many times over can take seconds to compile. The
    caller bounds that (`sql_guard` does, from the text) before calling.

    Raises `QueryError` with SQLite's message when the statement does not
    compile: unknown table or column, syntax error, more than one statement
    ("You can only execute one statement at a time."), an empty or
    comment-only string ("incomplete input"), bind parameters, and a
    statement that itself starts with `EXPLAIN` (a syntax error after the
    prefix); also when the text is not valid Unicode. Raises `ReadOnlyViolation` when it contains a denied action. A
    statement the authorizer is never asked about (`VACUUM`) returns a
    record with no actions; callers must treat that as a rejection.
    """
    denied: list[str] = []
    seen: list[tuple[int, str | None, str | None]] = []
    connection = _connect(db_path, denied, seen)
    try:
        connection.execute(_COMPILE_ONLY + sql)
    except (sqlite3.Error, sqlite3.Warning) as error:
        raise _translate(error, denied, db_path) from error
    except UnicodeError as error:
        raise _not_unicode(error) from error
    finally:
        connection.close()

    columns: dict[str, set[str]] = {}
    functions: set[str] = set()
    for action, first, second in seen:
        if action == sqlite3.SQLITE_READ and first is not None:
            named = columns.setdefault(first.lower(), set())
            if second:
                named.add(second)
        elif action == sqlite3.SQLITE_FUNCTION and second is not None:
            functions.add(second.lower())
    return QueryInspection(
        actions=tuple(action for action, _, _ in seen),
        reads=tuple(
            TableRead(table=table, columns=frozenset(names))
            for table, names in sorted(columns.items())
        ),
        functions=frozenset(functions),
    )


def _connect(
    db_path: Path,
    denied: list[str],
    seen: list[tuple[int, str | None, str | None]] | None = None,
) -> sqlite3.Connection:
    """Open `db_path` read-only.

    Denied actions are appended to `denied`; when `seen` is given, every
    action the authorizer is asked about is appended to it.
    """
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
    except sqlite3.Error as error:
        raise DatabaseAccessError(f"Cannot open {db_path} read-only: {error}.") from error
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS)
        connection.set_authorizer(_authorizer(denied, seen))
    except BaseException as error:
        connection.close()
        if isinstance(error, sqlite3.Error):
            raise DatabaseAccessError(f"Cannot open {db_path} read-only: {error}.") from error
        raise
    return connection


def _value_bytes(value: object) -> int:
    """Bytes one fetched value counts for against the result budget."""
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", "surrogatepass"))
    return _SCALAR_BYTES


def _not_unicode(error: UnicodeError) -> QueryError:
    return QueryError(f"the statement is not valid Unicode text: {error}")


def _is_read(action: int, argument: str | None) -> bool:
    """Whether an authorizer action only reads. `argument` is the pragma name."""
    if action == sqlite3.SQLITE_PRAGMA:
        return argument in _READ_ONLY_PRAGMAS
    return action in _READ_ACTIONS


def _action_name(action: int) -> str:
    return _ACTION_NAMES.get(action, f"action code {action}")


def _authorizer(
    denied: list[str], seen: list[tuple[int, str | None, str | None]] | None = None
) -> Callable[..., int]:
    """Build the authorizer callback: allow reads, deny everything else."""

    def authorize(action: int, arg1: str | None, arg2: str | None, *_: object) -> int:
        if seen is not None:
            seen.append((action, arg1, arg2))
        if _is_read(action, arg1):
            return sqlite3.SQLITE_OK
        name = _action_name(action)
        denied.append(f"{name} {arg1}" if arg1 else name)
        return sqlite3.SQLITE_DENY

    return authorize


def _translate(
    error: Exception, denied: list[str], db_path: Path, timeout_seconds: float | None = None
) -> DbError:
    """Map a sqlite3 exception to this module's exception types."""
    extended = getattr(error, "sqlite_errorcode", 0)
    # Extended result codes keep the primary code in the low byte.
    code = extended & 0xFF
    if code == sqlite3.SQLITE_AUTH or denied:
        detail = denied[0] if denied else str(error)
        return ReadOnlyViolation(
            f"Only read-only queries are allowed; denied: {detail}."
        )
    if extended in _UNREADABLE_CODES:
        return DatabaseAccessError(
            f"Cannot read {db_path}: the directory holding the database must be writable "
            "(the database is in WAL mode, and SQLite creates its -shm and -wal files "
            f"there on the first read). SQLite: {error}."
        )
    if code == sqlite3.SQLITE_READONLY:
        return ReadOnlyViolation(f"Only read-only queries are allowed; SQLite: {error}.")
    if code == sqlite3.SQLITE_INTERRUPT and timeout_seconds is not None:
        return QueryTimeout(
            f"Query interrupted after exceeding the {timeout_seconds:g}s timeout."
        )
    return QueryError(str(error))
