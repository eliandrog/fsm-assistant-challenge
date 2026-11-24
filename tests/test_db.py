"""db: read-only enforcement, row cap, timeout, typed errors."""

import os
import sqlite3
import time

import pytest

from conftest import file_sha256
from fsm_assistant import db

COUNT_TO_TEN = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 10) "
    "SELECT x FROM c"
)
# Never finishes: counts the rows of an endless recursive query.
ENDLESS = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"


def run(path, sql, params=(), row_cap=100, timeout_seconds=10.0):
    return db.run_query(sql, params, db_path=path, row_cap=row_cap, timeout_seconds=timeout_seconds)


def test_select_returns_columns_and_rows(fixture_db):
    result = run(fixture_db, "SELECT id, card_type FROM cards ORDER BY id")
    assert result.columns == ("id", "card_type")
    assert result.rows == ((100, "Credit"), (101, "Debit"), (102, "Debit (Prepaid)"))
    assert result.row_count == 3
    assert result.truncated is False
    assert result.elapsed_seconds >= 0


def test_parameters_are_bound(fixture_db):
    assert run(fixture_db, "SELECT id FROM cards WHERE card_type = ?", ("Debit",)).rows == ((101,),)
    assert run(fixture_db, "SELECT :n + 1", {"n": 1}).rows == ((2,),)


def test_rows_are_capped_and_truncation_is_reported(fixture_db):
    result = run(fixture_db, COUNT_TO_TEN, row_cap=4)
    assert result.rows == ((1,), (2,), (3,), (4,))
    assert result.row_count == 4
    assert result.truncated is True


def test_exactly_the_cap_is_not_truncated(fixture_db):
    result = run(fixture_db, COUNT_TO_TEN, row_cap=10)
    assert result.row_count == 10
    assert result.truncated is False


def test_the_cap_does_not_depend_on_the_sql_text(fixture_db):
    # The query's own LIMIT is respected, and a larger one is still capped.
    assert run(fixture_db, COUNT_TO_TEN + " LIMIT 2", row_cap=4).truncated is False
    assert run(fixture_db, COUNT_TO_TEN + " LIMIT 9", row_cap=4).row_count == 4


WRITES = [
    "INSERT INTO mcc_codes VALUES (1, 'x')",
    "UPDATE cards SET card_type = 'Debit'",
    "DELETE FROM fraud_labels",
    "CREATE TABLE t (a)",
    "CREATE TEMP TABLE t (a)",
    "CREATE INDEX i ON transactions (card_id)",
    "CREATE VIEW v AS SELECT 1",
    "DROP TABLE transactions",
    "ALTER TABLE users ADD COLUMN x",
    "REINDEX",
    "ANALYZE",
    "VACUUM",
    "BEGIN",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA user_version = 5",
    "PRAGMA query_only = OFF",
    "PRAGMA writable_schema = ON",
]


@pytest.mark.parametrize("sql", WRITES)
def test_writes_are_rejected(fixture_db, sql):
    before = file_sha256(fixture_db)
    with pytest.raises(db.ReadOnlyViolation, match="read-only"):
        run(fixture_db, sql)
    assert file_sha256(fixture_db) == before


@pytest.mark.parametrize("template", ["ATTACH DATABASE '{}' AS other", "VACUUM INTO '{}'"])
def test_statements_that_would_create_a_file_are_rejected(fixture_db, tmp_path, template):
    target = tmp_path / "new.db"
    with pytest.raises(db.ReadOnlyViolation):
        run(fixture_db, template.format(target))
    assert not target.exists()


def test_the_violation_message_names_what_was_denied(fixture_db):
    with pytest.raises(db.ReadOnlyViolation, match="INSERT mcc_codes"):
        run(fixture_db, "INSERT INTO mcc_codes VALUES (1, 'x')")


def test_a_write_hidden_in_a_cte_is_rejected(fixture_db):
    with pytest.raises(db.DbError):
        run(fixture_db, "WITH x AS (SELECT 1) DELETE FROM fraud_labels")


def test_file_is_still_read_only_without_the_authorizer(fixture_db, monkeypatch):
    # Defence in depth: with the authorizer allowing everything, mode=ro and
    # query_only must still stop writes.
    monkeypatch.setattr(db, "_is_read", lambda action, argument: True)
    before = file_sha256(fixture_db)
    for sql in ("INSERT INTO mcc_codes VALUES (1, 'x')", "DROP TABLE users", "CREATE TEMP TABLE t (a)"):
        with pytest.raises(db.ReadOnlyViolation):
            run(fixture_db, sql)
    assert file_sha256(fixture_db) == before


def test_no_files_are_created_next_to_the_database(fixture_db):
    before = sorted(os.listdir(fixture_db.parent))
    run(fixture_db, "SELECT count(*) FROM transactions")
    with pytest.raises(db.ReadOnlyViolation):
        run(fixture_db, "DELETE FROM transactions")
    assert sorted(os.listdir(fixture_db.parent)) == before


def test_wal_mode_database_is_readable_and_unchanged(fixture_db, tmp_path):
    # The real database is in WAL mode; reproduce that on a copy.
    path = tmp_path / "wal.db"
    path.write_bytes(fixture_db.read_bytes())
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    connection.close()
    before = file_sha256(path)

    assert run(path, "SELECT count(*) FROM transactions").rows == ((6,),)
    with pytest.raises(db.ReadOnlyViolation):
        run(path, "DELETE FROM transactions")
    assert file_sha256(path) == before


def test_a_long_query_is_interrupted(fixture_db):
    started = time.monotonic()
    with pytest.raises(db.QueryTimeout, match="0.2s"):
        run(fixture_db, ENDLESS, timeout_seconds=0.2)
    assert 0.2 <= time.monotonic() - started < 2


def test_the_timeout_also_covers_fetching_rows(fixture_db):
    # Rows only appear after an endless sort, so the time is spent in fetch.
    with pytest.raises(db.QueryTimeout):
        run(fixture_db, ENDLESS.replace("count(*)", "x") + " ORDER BY x DESC", timeout_seconds=0.2)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT amount FROM transactions", "no such column: amount"),
        ("SELECT * FROM payments", "no such table: payments"),
        ("SELEC 1", "syntax error"),
    ],
)
def test_sql_errors_keep_the_sqlite_message(fixture_db, sql, message):
    with pytest.raises(db.QueryError) as error:
        run(fixture_db, sql)
    assert message in error.value.sqlite_message
    assert message in str(error.value)


def test_only_one_statement_runs(fixture_db):
    before = file_sha256(fixture_db)
    with pytest.raises(db.QueryError, match="one statement"):
        run(fixture_db, "SELECT 1; DELETE FROM fraud_labels")
    assert file_sha256(fixture_db) == before


def test_schema_reading_pragmas_are_allowed(fixture_db):
    names = [row[1] for row in run(fixture_db, "PRAGMA table_info(fraud_labels)").rows]
    assert names == ["transaction_id", "is_fraud"]
    assert run(fixture_db, "PRAGMA foreign_key_list(cards)").rows[0][2] == "users"


def test_missing_database_file(tmp_path):
    with pytest.raises(db.DatabaseNotFoundError, match="FSM_DB_PATH"):
        run(tmp_path / "absent.db", "SELECT 1")
    assert not (tmp_path / "absent.db").exists()


def test_a_path_with_uri_characters_opens(fixture_db, tmp_path):
    path = tmp_path / "odd name ?#%.db"
    path.write_bytes(fixture_db.read_bytes())
    assert run(path, "SELECT count(*) FROM users").rows == ((2,),)


@pytest.mark.parametrize("kwargs", [{"row_cap": 0}, {"timeout_seconds": 0}])
def test_limits_must_be_positive(fixture_db, kwargs):
    with pytest.raises(ValueError):
        run(fixture_db, "SELECT 1", **kwargs)


def test_all_errors_share_a_base_class():
    for error in (db.DatabaseNotFoundError, db.ReadOnlyViolation, db.QueryTimeout, db.QueryError):
        assert issubclass(error, db.DbError)
