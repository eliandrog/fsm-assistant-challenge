"""db: read-only enforcement, row cap, timeout, typed errors."""

import base64
import json
import os
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from conftest import file_sha256
from fsm_assistant import db

COUNT_TO_TEN = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 10) "
    "SELECT x FROM c"
)
# Never finishes: counts the rows of an endless recursive query.
ENDLESS = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"


def run(
    path: Path,
    sql: str,
    params: Sequence[db.Cell] | Mapping[str, db.Cell] = (),
    /,
    row_cap: int = 100,
    timeout_seconds: float = 10.0,
    **limits: int,
) -> db.QueryResult:
    return db.run_query(sql, params, db_path=path, row_cap=row_cap, timeout_seconds=timeout_seconds, **limits)


def test_select_returns_columns_and_rows(fixture_db: Path) -> None:
    result = run(fixture_db, "SELECT id, card_type FROM cards ORDER BY id")
    assert result.columns == ("id", "card_type")
    assert result.rows == ((100, "Credit"), (101, "Debit"), (102, "Debit (Prepaid)"))
    assert result.row_count == 3
    assert result.truncated is False
    assert result.elapsed_seconds >= 0


def test_parameters_are_bound(fixture_db: Path) -> None:
    assert run(fixture_db, "SELECT id FROM cards WHERE card_type = ?", ("Debit",)).rows == ((101,),)
    assert run(fixture_db, "SELECT :n + 1", {"n": 1}).rows == ((2,),)


def test_rows_are_capped_and_truncation_is_reported(fixture_db: Path) -> None:
    result = run(fixture_db, COUNT_TO_TEN, row_cap=4)
    assert result.rows == ((1,), (2,), (3,), (4,))
    assert result.row_count == 4
    assert result.truncated is True


def test_exactly_the_cap_is_not_truncated(fixture_db: Path) -> None:
    result = run(fixture_db, COUNT_TO_TEN, row_cap=10)
    assert result.row_count == 10
    assert result.truncated is False


def test_the_cap_does_not_depend_on_the_sql_text(fixture_db: Path) -> None:
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
def test_writes_are_rejected(fixture_db: Path, sql: str) -> None:
    before = file_sha256(fixture_db)
    with pytest.raises(db.ReadOnlyViolation, match="read-only"):
        run(fixture_db, sql)
    assert file_sha256(fixture_db) == before


@pytest.mark.parametrize("template", ["ATTACH DATABASE '{}' AS other", "VACUUM INTO '{}'"])
def test_statements_that_would_create_a_file_are_rejected(fixture_db: Path, tmp_path: Path, template: str) -> None:
    target = tmp_path / "new.db"
    with pytest.raises(db.ReadOnlyViolation):
        run(fixture_db, template.format(target))
    assert not target.exists()


def test_the_violation_message_names_what_was_denied(fixture_db: Path) -> None:
    with pytest.raises(db.ReadOnlyViolation, match="INSERT mcc_codes"):
        run(fixture_db, "INSERT INTO mcc_codes VALUES (1, 'x')")


def test_a_write_hidden_in_a_cte_is_rejected(fixture_db: Path) -> None:
    with pytest.raises(db.ReadOnlyViolation):
        run(fixture_db, "WITH x AS (SELECT 1) DELETE FROM fraud_labels")


def test_file_is_still_read_only_without_the_authorizer(fixture_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Defence in depth: with the authorizer allowing everything, mode=ro and
    # query_only must still stop writes.
    monkeypatch.setattr(db, "_is_read", lambda action, argument: True)
    before = file_sha256(fixture_db)
    for sql in ("INSERT INTO mcc_codes VALUES (1, 'x')", "DROP TABLE users", "CREATE TEMP TABLE t (a)"):
        with pytest.raises(db.ReadOnlyViolation):
            run(fixture_db, sql)
    assert file_sha256(fixture_db) == before


def test_no_files_are_created_next_to_the_database(fixture_db: Path) -> None:
    before = sorted(os.listdir(fixture_db.parent))
    run(fixture_db, "SELECT count(*) FROM transactions")
    with pytest.raises(db.ReadOnlyViolation):
        run(fixture_db, "DELETE FROM transactions")
    assert sorted(os.listdir(fixture_db.parent)) == before


def test_wal_mode_database_is_readable_and_unchanged(fixture_db: Path, tmp_path: Path) -> None:
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


def test_a_long_query_is_interrupted(fixture_db: Path) -> None:
    started = time.monotonic()
    with pytest.raises(db.QueryTimeout, match="0.2s"):
        run(fixture_db, ENDLESS, timeout_seconds=0.2)
    assert 0.2 <= time.monotonic() - started < 2


def test_the_timeout_also_covers_fetching_rows(fixture_db: Path) -> None:
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
def test_sql_errors_keep_the_sqlite_message(fixture_db: Path, sql: str, message: str) -> None:
    with pytest.raises(db.QueryError) as error:
        run(fixture_db, sql)
    assert message in error.value.sqlite_message
    assert message in str(error.value)


def test_only_one_statement_runs(fixture_db: Path) -> None:
    before = file_sha256(fixture_db)
    with pytest.raises(db.QueryError, match="one statement"):
        run(fixture_db, "SELECT 1; DELETE FROM fraud_labels")
    assert file_sha256(fixture_db) == before


def test_schema_reading_pragmas_are_allowed(fixture_db: Path) -> None:
    names = [row[1] for row in run(fixture_db, "PRAGMA table_info(fraud_labels)").rows]
    assert names == ["transaction_id", "is_fraud"]
    assert run(fixture_db, "PRAGMA foreign_key_list(cards)").rows[0][2] == "users"


def test_missing_database_file(tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseNotFoundError, match="FSM_DB_PATH"):
        run(tmp_path / "absent.db", "SELECT 1")
    assert not (tmp_path / "absent.db").exists()


def test_a_path_with_uri_characters_opens(fixture_db: Path, tmp_path: Path) -> None:
    path = tmp_path / "odd name ?#%.db"
    path.write_bytes(fixture_db.read_bytes())
    assert run(path, "SELECT count(*) FROM users").rows == ((2,),)


@pytest.mark.parametrize("kwargs", [{"row_cap": 0}, {"timeout_seconds": 0}, {"max_result_bytes": 0}])
def test_limits_must_be_positive(fixture_db: Path, kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        run(fixture_db, "SELECT 1", **kwargs)


def test_all_errors_share_a_base_class() -> None:
    for error in (db.DatabaseNotFoundError, db.ReadOnlyViolation, db.QueryTimeout, db.QueryError):
        assert issubclass(error, db.DbError)


@pytest.mark.parametrize("sql", ["", "   \n", "-- only a comment", "/* only a comment */", ";"])
def test_an_empty_statement_is_an_error(fixture_db: Path, sql: str) -> None:
    with pytest.raises(db.QueryError, match="empty statement"):
        run(fixture_db, sql)


def test_an_unknown_double_quoted_name_is_an_error_not_a_string(fixture_db: Path) -> None:
    # SQLite's default reads "amount" as the text 'amount', which is greater
    # than any number, so the filter would match every row.
    with pytest.raises(db.QueryError, match='no such column: "amount"'):
        run(fixture_db, 'SELECT count(*) FROM transactions WHERE "amount" > 1000')
    quoted = 'SELECT count(*) FROM "transactions" WHERE "amount_usd_cents" > 1000'
    assert run(fixture_db, quoted).rows == ((4,),)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT hex(zeroblob(2000000))",
        "SELECT zeroblob(1000000000)",
        "SELECT replace(hex(zeroblob(400000)), '0', '000')",
        "SELECT group_concat(hex(zeroblob(400000))) FROM (SELECT 1 UNION ALL SELECT 2)",
    ],
)
def test_a_value_over_the_size_limit_fails_at_once(fixture_db: Path, sql: str) -> None:
    started = time.monotonic()
    with pytest.raises(db.QueryError, match="too big"):
        run(fixture_db, sql)
    assert time.monotonic() - started < 1


def test_a_value_at_the_size_limit_is_allowed(fixture_db: Path) -> None:
    assert db.MAX_VALUE_BYTES == 100_000
    assert run(fixture_db, f"SELECT length(zeroblob({db.MAX_VALUE_BYTES}))").rows == ((100_000,),)
    with pytest.raises(db.QueryError, match="too big"):
        run(fixture_db, f"SELECT length(zeroblob({db.MAX_VALUE_BYTES + 1}))")


def test_a_result_over_the_byte_budget_is_an_error(fixture_db: Path) -> None:
    # Six rows of 100,000 bytes each: within the row cap and the value cap.
    sql = "SELECT zeroblob(100000) FROM transactions"
    assert run(fixture_db, sql).row_count == 6
    with pytest.raises(db.ResultTooLarge, match="over 300,000 bytes by row 4") as raised:
        run(fixture_db, sql, max_result_bytes=300_000)
    assert isinstance(raised.value, db.QueryError)
    assert "Select fewer or shorter columns" in raised.value.sqlite_message


def test_the_byte_budget_counts_text_blobs_and_other_values(fixture_db: Path) -> None:
    def fits(sql: str, budget: int) -> bool:
        try:
            run(fixture_db, sql, max_result_bytes=budget)
        except db.ResultTooLarge:
            return False
        return True

    assert fits("SELECT 'abcd'", 4) and not fits("SELECT 'abcd'", 3)
    assert fits("SELECT 'é'", 2) and not fits("SELECT 'é'", 1)  # bytes, not characters
    assert fits("SELECT x'00112233'", 4) and not fits("SELECT x'00112233'", 3)
    assert fits("SELECT 1, 2.5, NULL", 24) and not fits("SELECT 1, 2.5, NULL", 23)
    assert fits("SELECT 'ab' UNION ALL SELECT 'cd'", 4) and not fits("SELECT 'ab' UNION ALL SELECT 'cd'", 3)


def test_the_row_that_only_detects_truncation_is_not_counted(fixture_db: Path) -> None:
    result = run(fixture_db, "SELECT 'abcd' UNION ALL SELECT 'efgh'", row_cap=1, max_result_bytes=4)
    assert (result.rows, result.truncated) == ((("abcd",),), True)


def test_the_default_byte_budget_stops_a_result_of_large_values(fixture_db: Path) -> None:
    # 100 columns of 100,000 bytes is 10 MB a row: over the default budget on the first row.
    wide = "SELECT " + ", ".join(["zeroblob(100000)"] * db.MAX_COLUMNS)
    with pytest.raises(db.ResultTooLarge, match="by row 1"):
        run(fixture_db, wide)
    assert db.DEFAULT_MAX_RESULT_BYTES == 5_000_000


def test_a_result_has_a_bounded_number_of_columns(fixture_db: Path) -> None:
    assert len(run(fixture_db, "SELECT " + ", ".join(["1"] * db.MAX_COLUMNS)).columns) == db.MAX_COLUMNS
    with pytest.raises(db.QueryError, match="too many columns"):
        run(fixture_db, "SELECT " + ", ".join(["1"] * (db.MAX_COLUMNS + 1)))
    with pytest.raises(db.QueryError, match="too many columns"):
        inspect(fixture_db, "SELECT " + ", ".join(["1"] * (db.MAX_COLUMNS + 1)))
    # Every table can still be read whole, and joined whole.
    everything = (
        "SELECT * FROM transactions t JOIN cards c ON c.id = t.card_id JOIN users u ON u.id = c.user_id "
        "JOIN merchants m ON m.id = t.merchant_id JOIN mcc_codes k ON k.mcc = m.mcc "
        "JOIN merchant_locations l ON l.id = t.merchant_location_id "
        "LEFT JOIN fraud_labels f ON f.transaction_id = CAST(t.id AS TEXT)"
    )
    assert len(run(fixture_db, everything).columns) == 40


@pytest.mark.parametrize("sql", ["SELECT '\ud800'", "SELECT 1 -- \udfff"])
def test_text_that_is_not_unicode_is_a_query_error(fixture_db: Path, sql: str) -> None:
    with pytest.raises(db.QueryError, match="not valid Unicode"):
        run(fixture_db, sql)
    with pytest.raises(db.QueryError, match="not valid Unicode"):
        inspect(fixture_db, sql)
    with pytest.raises(db.QueryError, match="not valid Unicode"):
        run(fixture_db, "SELECT ?", ("\ud800",))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to any directory")
def test_a_wal_database_in_a_read_only_directory_is_not_blamed_on_the_sql(
    fixture_db: Path, tmp_path: Path
) -> None:
    directory = tmp_path / "locked"
    directory.mkdir()
    path = directory / "wal.db"
    path.write_bytes(fixture_db.read_bytes())
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.close()
    assert os.listdir(directory) == ["wal.db"]  # no -shm file to reuse

    directory.chmod(0o555)
    try:
        with pytest.raises(db.DatabaseAccessError, match="must be writable") as raised:
            run(path, "SELECT count(*) FROM transactions")
        assert not isinstance(raised.value, db.ReadOnlyViolation | db.QueryError)
        with pytest.raises(db.DatabaseAccessError, match="must be writable"):
            db.inspect_query("SELECT count(*) FROM transactions", db_path=path)
    finally:
        directory.chmod(0o755)
    assert run(path, "SELECT count(*) FROM transactions").rows == ((6,),)


def test_the_connection_is_closed_when_setup_fails(
    fixture_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class FailingSetup(sqlite3.Connection):
        def setlimit(self, category: int, limit: int, /) -> int:
            raise sqlite3.OperationalError("setup failed")

        def close(self) -> None:
            closed.append(True)
            super().close()

    connect = sqlite3.connect

    def connect_failing(database: str, **options: Any) -> sqlite3.Connection:
        return connect(database, factory=FailingSetup, **options)

    monkeypatch.setattr(sqlite3, "connect", connect_failing)
    with pytest.raises(db.DatabaseAccessError, match="setup failed"):
        run(fixture_db, "SELECT 1")
    assert closed == [True]


def test_environment_errors_share_the_base_class() -> None:
    assert issubclass(db.DatabaseAccessError, db.DbError)
    assert not issubclass(db.DatabaseAccessError, db.ReadOnlyViolation | db.QueryError)


def inspect(path: Path, sql: str) -> db.QueryInspection:
    return db.inspect_query(sql, db_path=path)


def test_inspect_reports_actions_tables_columns_and_functions(fixture_db: Path) -> None:
    record = inspect(
        fixture_db,
        "SELECT Lower(c.card_type), COUNT(*) FROM transactions t JOIN cards c ON c.id = t.card_id "
        "WHERE t.amount_usd_cents > (SELECT avg(credit_limit_usd_cents) FROM cards) GROUP BY 1",
    )
    assert record.actions[0] == db.SELECT_ACTION
    assert record.first_action == "SELECT"
    assert record.tables == {"transactions", "cards"}
    assert [read.table for read in record.reads] == ["cards", "transactions"]
    assert record.columns_of("cards") == {"id", "card_type", "credit_limit_usd_cents"}
    assert record.columns_of("TRANSACTIONS") == {"card_id", "amount_usd_cents"}
    assert record.columns_of("users") == frozenset()
    assert record.functions == {"lower", "count", "avg"}


def test_inspect_reports_the_real_table_behind_an_alias_a_subquery_and_a_cte(fixture_db: Path) -> None:
    record = inspect(
        fixture_db,
        "WITH labelled AS (SELECT transaction_id AS tid FROM fraud_labels) "
        "SELECT x.tid FROM (SELECT tid FROM labelled) AS x",
    )
    assert record.tables == {"fraud_labels"}
    assert record.columns_of("fraud_labels") == {"transaction_id"}


def test_inspect_lowercases_table_names(fixture_db: Path) -> None:
    # With no column named, SQLite reports the table as the statement spelled it.
    assert inspect(fixture_db, "SELECT count(*) FROM FRAUD_LABELS").tables == {"fraud_labels"}
    assert inspect(fixture_db, 'SELECT 1 FROM main."Fraud_Labels"').tables == {"fraud_labels"}


def test_inspect_can_report_a_cte_name_but_only_without_columns(fixture_db: Path) -> None:
    per_card = "WITH per_card AS (SELECT card_id, count(*) AS n FROM transactions GROUP BY card_id) "
    named = inspect(fixture_db, per_card + "SELECT card_id, n FROM per_card")
    assert named.tables == {"transactions"}
    unnamed = inspect(fixture_db, per_card + "SELECT count(*) FROM per_card")
    assert unnamed.tables == {"transactions", "per_card"}
    assert unnamed.columns_of("per_card") == frozenset()
    # A CTE that takes a real table's name hides it: nothing real is read.
    shadow = inspect(fixture_db, "WITH fraud_labels AS (SELECT 1 AS is_fraud) SELECT is_fraud FROM fraud_labels")
    assert shadow.tables == frozenset()


def test_inspect_never_runs_the_statement(fixture_db: Path) -> None:
    before = file_sha256(fixture_db)
    started = time.monotonic()
    record = inspect(fixture_db, ENDLESS)
    inspect(fixture_db, "SELECT hex(zeroblob(1000000000))")  # fails only when run
    assert time.monotonic() - started < 0.5
    assert record.first_action == "SELECT"
    assert file_sha256(fixture_db) == before


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT amount FROM transactions", "no such column: amount"),
        ('SELECT 1 FROM transactions WHERE "amount" > 1000', 'no such column: "amount"'),
        ("SELECT * FROM payments", "no such table: payments"),
        ("SELEC 1", "syntax error"),
        ("SELECT 1; DELETE FROM fraud_labels", "one statement"),
        ("", "incomplete input"),
        ("-- only a comment", "incomplete input"),
        ("EXPLAIN SELECT 1", 'near "EXPLAIN": syntax error'),
        ("QUERY PLAN SELECT 1", 'near "QUERY": syntax error'),
        ("SELECT ?", "bindings"),
    ],
)
def test_inspect_keeps_the_sqlite_message(fixture_db: Path, sql: str, message: str) -> None:
    with pytest.raises(db.QueryError) as error:
        inspect(fixture_db, sql)
    assert message in error.value.sqlite_message


@pytest.mark.parametrize("sql", ["DELETE FROM fraud_labels", "ATTACH DATABASE 'x.db' AS other", "PRAGMA user_version = 5"])
def test_inspect_denies_what_run_query_denies(fixture_db: Path, sql: str) -> None:
    before = file_sha256(fixture_db)
    with pytest.raises(db.ReadOnlyViolation, match="read-only"):
        inspect(fixture_db, sql)
    assert file_sha256(fixture_db) == before


def test_inspect_returns_an_empty_record_for_vacuum(fixture_db: Path) -> None:
    # The authorizer is never asked about VACUUM; the guard rejects an empty record.
    record = inspect(fixture_db, "VACUUM")
    assert record.actions == ()
    assert record.first_action is None


def test_inspect_shows_a_pragma_as_its_first_action(fixture_db: Path) -> None:
    assert inspect(fixture_db, "PRAGMA table_info(cards)").first_action == "PRAGMA"


def test_inspection_is_immutable(fixture_db: Path) -> None:
    record = inspect(fixture_db, "SELECT id FROM cards")
    with pytest.raises(ValidationError):
        record.functions = frozenset()  # type: ignore[misc]  # assigning to a frozen field is what is tested
    with pytest.raises(ValidationError):
        record.reads[0].table = "users"  # type: ignore[misc]  # assigning to a frozen field is what is tested


def test_inspect_missing_database(tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseNotFoundError):
        inspect(tmp_path / "absent.db", "SELECT 1")


# One value of each kind SQLite returns: text, integer, real, blob, NULL.
EVERY_KIND = "SELECT 'a' AS t, 1 AS i, 1.5 AS r, x'00ff' AS b, NULL AS n"


def test_result_is_immutable(fixture_db: Path) -> None:
    result = db.run_query("SELECT 1", db_path=fixture_db, row_cap=1, timeout_seconds=10.0)
    with pytest.raises(ValidationError):
        result.truncated = True  # type: ignore[misc]  # assigning to a frozen field is what is tested
    assert isinstance(result.rows, tuple) and isinstance(result.rows[0], tuple)


def test_result_rejects_unknown_fields_and_wrong_types() -> None:
    fields = {"columns": ("x",), "rows": ((1,),), "truncated": False, "elapsed_seconds": 0.0}
    assert db.QueryResult.model_validate(fields).row_count == 1
    with pytest.raises(ValidationError, match="extra_forbidden"):
        db.QueryResult.model_validate({**fields, "row_count": 1})
    with pytest.raises(ValidationError):
        db.QueryResult.model_validate({**fields, "rows": (([1],),)})


def test_a_result_from_run_query_is_what_validation_would_accept(fixture_db: Path) -> None:
    # run_query builds its result without validating it; this is the check that it could.
    for sql in (EVERY_KIND, "SELECT * FROM transactions", "SELECT * FROM cards WHERE 0"):
        result = db.run_query(sql, db_path=fixture_db, row_cap=3, timeout_seconds=10.0)
        validated = db.QueryResult.model_validate(result.model_dump())
        assert validated == result
        assert [[type(cell) for cell in row] for row in validated.rows] == [
            [type(cell) for cell in row] for row in result.rows
        ]


def test_result_round_trips_through_model_dump(fixture_db: Path) -> None:
    result = db.run_query(EVERY_KIND, db_path=fixture_db, row_cap=1, timeout_seconds=10.0)
    dumped = result.model_dump()
    assert dumped["columns"] == ("t", "i", "r", "b", "n")
    assert dumped["rows"] == (("a", 1, 1.5, b"\x00\xff", None),)
    assert db.QueryResult.model_validate(dumped) == result


def test_result_round_trips_through_json_when_it_holds_no_blob(fixture_db: Path) -> None:
    result = db.run_query(
        "SELECT * FROM transactions", db_path=fixture_db, row_cap=3, timeout_seconds=10.0
    )
    assert result.truncated
    assert db.QueryResult.model_validate_json(result.model_dump_json()) == result


def test_a_blob_is_written_to_json_as_base64_text(fixture_db: Path) -> None:
    # x'fffe' is not valid UTF-8: written as text it could not be serialised at all.
    result = db.run_query(
        "SELECT x'fffe' AS b", db_path=fixture_db, row_cap=1, timeout_seconds=10.0
    )
    encoded = base64.urlsafe_b64encode(b"\xff\xfe").decode()
    assert json.loads(result.model_dump_json())["rows"] == [[encoded]]
    assert result.model_dump(mode="json")["rows"] == [[encoded]]
    # JSON has no blob type, so the text is what comes back.
    assert db.QueryResult.model_validate_json(result.model_dump_json()).rows == ((encoded,),)


def test_a_file_that_is_not_a_database_is_a_fault_of_the_environment(tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database " * 100)
    with pytest.raises(db.DatabaseAccessError, match="Cannot read"):
        db.run_query("SELECT 1", db_path=broken, row_cap=1, timeout_seconds=1)
    with pytest.raises(db.DatabaseAccessError):
        db.inspect_query("SELECT 1", db_path=broken)
