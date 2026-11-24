"""Shared fixtures and helpers.

Unit tests use a small database built in a temp directory from
data/schema.sql, so they are fast and do not need the real 100 MB file.
Tests marked with the `real_db` fixture run against data/data.db and are
skipped when it is missing.
"""

import hashlib
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue

from fsm_assistant import llm
from fsm_assistant.llm import (
    JsonObject,
    ModelError,
    ModelOutputError,
    ModelReply,
    ModelRequest,
    ModelSpec,
    ScriptedModel,
    TokenUsage,
    ToolCall,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
REAL_DB = DATA_DIR / "data.db"

# Six transactions: four labelled (one fraud), one negative amount, one
# online, one abroad, one with an error.
FIXTURE_ROWS = """
INSERT INTO users VALUES
  (1, '1980-01-31', 'Female', '1 Elm Street', 40.0, -74.0, 3000000, 6000000, 1000000, 720),
  (2, '1975-06-15', 'Male', '2 Pine Avenue', 34.0, -118.0, 2000000, 4000000, 0, 650);
INSERT INTO mcc_codes VALUES (5411, 'Grocery Stores, Supermarkets'), (4829, 'Money Transfer');
INSERT INTO merchants VALUES (10, 'Merchant 10', 5411), (20, 'Merchant 20', 4829);
INSERT INTO merchant_locations VALUES
  (1, 10, 'Sacramento', 'CA', 95814),
  (2, 20, 'ONLINE', '', NULL),
  (3, 10, 'Rome', 'Italy', NULL);
INSERT INTO cards VALUES
  (100, 1, 'Visa', 'Credit', '2024-12-31', 1, 1000000, '2010-05-01', 2015, 0),
  (101, 1, 'Mastercard', 'Debit', '2023-06-30', 1, 500000, '2012-03-01', 2016, 0),
  (102, 2, 'Amex', 'Debit (Prepaid)', '2022-01-31', 0, 5000, '2018-09-01', 2018, 0);
INSERT INTO transactions VALUES
  (1001, '2019-01-01 00:02:00', 100, 2500, 'Chip Transaction', 10, 1, NULL),
  (1002, '2019-02-10 12:30:00', 101, 150000, 'Online Transaction', 20, 2, NULL),
  (1003, '2019-03-05 08:15:00', 101, -5000, 'Swipe Transaction', 10, 1, NULL),
  (1004, '2019-04-20 19:45:00', 102, 999, 'Chip Transaction', 10, 3, 'Bad PIN'),
  (1005, '2019-05-11 10:00:00', 100, 4200, 'Chip Transaction', 10, 1, NULL),
  (1006, '2019-10-31 23:59:00', 102, 120000, 'Online Transaction', 20, 2, NULL);
INSERT INTO fraud_labels VALUES ('1001', 0), ('1002', 1), ('1003', 0), ('1004', 0);
"""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tool_reply(*tools: str, stop_reason: str = "tool_use") -> ModelReply:
    """A reply in tool mode that calls the named tools with valid arguments."""
    arguments: dict[str, JsonObject] = {
        "run_sql": {"sql": "SELECT 1", "explanation": "", "assumptions": []},
        "decline": {"reason": "Not possible."},
        "ask_clarification": {"question": "Which?", "suggestions": ["a", "b"]},
        "propose_rule": {"where_clause": "id > 5", "explanation": "", "assumptions": []},
    }
    return ModelReply(
        tool_mode="tools",
        tool_calls=tuple(ToolCall(name=tool, arguments=arguments.get(tool, {})) for tool in tools),
        text="",
        stop_reason=stop_reason,
        usage=TokenUsage(input_tokens=1000, output_tokens=100),
        model_id="scripted",
        response_id=None,
        request_hash="h",
    )


def call_reply(
    tool: str, *, input_tokens: int = 1000, output_tokens: int = 100, **arguments: JsonValue
) -> ModelReply:
    """A reply in tool mode that calls `tool` with exactly `arguments`."""
    return ModelReply(
        tool_mode="tools",
        tool_calls=(ToolCall(name=tool, arguments=arguments),),
        text="",
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        model_id="scripted",
        response_id=None,
        request_hash="h",
    )


def sql_reply(sql: str, *assumptions: str, explanation: str = "What it returns.") -> ModelReply:
    return call_reply("run_sql", sql=sql, explanation=explanation, assumptions=list(assumptions))


def rule_reply(clause: str, *assumptions: str) -> ModelReply:
    return call_reply(
        "propose_rule",
        where_clause=clause,
        explanation="Which transactions it flags.",
        assumptions=list(assumptions),
    )


class StampingModel:
    """A scripted model whose replies carry the hash of the request they answer, as live ones do."""

    def __init__(self, spec: ModelSpec, steps: list[ModelReply | ModelError]) -> None:
        self._spec = spec
        self._inner = ScriptedModel(steps)

    def complete(self, request: ModelRequest) -> ModelReply:
        stamp = {"request_hash": llm.request_hash(self._spec, request)}
        try:
            return self._inner.complete(request).model_copy(update=stamp)
        except ModelOutputError as error:
            raise ModelOutputError(error.kind, error.reply.model_copy(update=stamp)) from None


# Eleven replies for the eleven questions of `check-model`: eight valid, three misses.
MIXED_RUN: list[ModelReply | ModelError] = [
    *[tool_reply("run_sql")] * 5,
    tool_reply("decline"),
    tool_reply("ask_clarification"),
    tool_reply(),
    tool_reply("run_sql", "decline"),
    ModelOutputError("refusal", tool_reply(stop_reason="refusal")),
    tool_reply("propose_rule"),
]


@pytest.fixture(scope="session")
def fixture_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small database with the real schema. Not in WAL mode: no sidecar files."""
    path = tmp_path_factory.mktemp("fixture") / "fixture.db"
    connection = sqlite3.connect(path)
    connection.executescript((DATA_DIR / "schema.sql").read_text())
    connection.executescript(FIXTURE_ROWS)
    connection.commit()
    connection.close()
    return path


@pytest.fixture(scope="session")
def real_db() -> Iterator[Path]:
    """Path to data/data.db, with a check that the test session left data/ as it found it.

    The database is in WAL mode, so a read-only connection creates
    `data.db-shm` / `data.db-wal` when they are absent (see docs/DATA.md).
    Any sidecar this session created is removed afterwards; ones that were
    already there are left alone.
    """
    if not REAL_DB.is_file():
        pytest.skip("data/data.db is not present")
    files_before = {path.name for path in DATA_DIR.iterdir()}
    hash_before = file_sha256(REAL_DB)
    mtime_before = REAL_DB.stat().st_mtime_ns

    yield REAL_DB

    for path in DATA_DIR.iterdir():
        if path.name not in files_before and path.suffix in {".db-shm", ".db-wal"}:
            path.unlink()
    assert {path.name for path in DATA_DIR.iterdir()} == files_before
    assert REAL_DB.stat().st_mtime_ns == mtime_before
    assert file_sha256(REAL_DB) == hash_before


def _refuse_connection(*args: object, **kwargs: object) -> None:
    raise AssertionError(f"a test tried to open a network connection: {args[1:]!r}")


# No test may open a connection, so an accidental model or tracing call fails
# loudly and sends nothing. Set for the whole process and never restored: a
# library's background thread can outlive the test that started it.
setattr(socket.socket, "connect", _refuse_connection)
setattr(socket.socket, "connect_ex", _refuse_connection)
