"""Integration tests against data/data.db. Skipped when the file is missing.

They check that read-only access works on the real (WAL-mode) file, and that
the facts stated in the schema context and docs/DATA.md hold for the data.
"""

import time
from pathlib import Path

import pytest

from fsm_assistant import db
from fsm_assistant.rules import backtest as backtest_module
from fsm_assistant.rules.backtest import RuleClauseError, backtest
from fsm_assistant.sql_guard import ReasonCode, check_query, check_rule_clause
from fsm_assistant.schema_context import LABEL_JOIN, build_schema_context


def run(path: Path, sql: str, row_cap: int = 100, timeout_seconds: float = 30.0) -> db.QueryResult:
    return db.run_query(sql, db_path=path, row_cap=row_cap, timeout_seconds=timeout_seconds)


def scalar(path: Path, sql: str) -> db.Cell:
    return run(path, sql).rows[0][0]


def as_int(cell: db.Cell) -> int:
    assert isinstance(cell, int)
    return cell


def test_select_is_capped(real_db: Path) -> None:
    result = run(real_db, "SELECT id FROM transactions", row_cap=10)
    assert result.row_count == 10
    assert result.truncated is True


@pytest.mark.parametrize("sql", ["INSERT INTO mcc_codes VALUES (1, 'x')", "DROP TABLE transactions"])
def test_writes_are_rejected(real_db: Path, sql: str) -> None:
    with pytest.raises(db.ReadOnlyViolation):
        run(real_db, sql)
    assert scalar(real_db, "SELECT count(*) FROM transactions") == 1_159_966
    assert scalar(real_db, "SELECT count(*) FROM mcc_codes") == 109


def test_a_runaway_join_is_interrupted(real_db: Path) -> None:
    # The label join without the cast, driven from transactions, compares
    # every transaction with every label. It is the mistake the timeout is for.
    started = time.monotonic()
    with pytest.raises(db.QueryTimeout):
        run(
            real_db,
            "SELECT count(*) FROM transactions t LEFT JOIN fraud_labels f "
            "ON f.transaction_id = t.id WHERE f.transaction_id IS NULL",
            timeout_seconds=0.5,
        )
    assert time.monotonic() - started < 3


def test_schema_context_on_the_real_data(real_db: Path) -> None:
    build_schema_context.cache_clear()
    context = build_schema_context(real_db)
    for table, rows in {
        "cards": 3437,
        "fraud_labels": 777_339,
        "mcc_codes": 109,
        "merchant_locations": 70_539,
        "merchants": 27_516,
        "transactions": 1_159_966,
        "users": 1206,
    }.items():
        assert f"{table} ({rows} rows)" in context
    assert "amount_usd_cents INTEGER" in context
    assert "card_type TEXT values: 'Credit', 'Debit', 'Debit (Prepaid)'" in context
    assert "transaction_type TEXT values: 'Chip Transaction', 'Online Transaction', 'Swipe Transaction'" in context
    assert "(NULL in 1141312 rows)" in context
    assert LABEL_JOIN in context
    assert "Only 777339 of 1159966 transactions (67.0%) have a fraud label" in context
    assert "is_fraud = 1 for 1360 of 777339 labelled transactions (0.17%)" in context
    assert "negative in 56900 rows" in context
    assert "from '2019-01-01 00:02:00' to '2019-10-31 23:59:00'" in context
    assert len(context) < 4000  # it goes in every prompt

    build_schema_context.cache_clear()
    assert build_schema_context(real_db).encode() == context.encode()


def test_label_join_facts(real_db: Path) -> None:
    # Every label id is the canonical text of an integer, so casting
    # transactions.id to text matches exactly the same rows as comparing numbers.
    assert scalar(
        real_db,
        "SELECT count(*) FROM fraud_labels "
        "WHERE transaction_id != CAST(CAST(transaction_id AS INTEGER) AS TEXT)",
    ) == 0
    cast = run(
        real_db,
        "SELECT count(*), sum(f.is_fraud) FROM transactions t "
        "JOIN fraud_labels f ON f.transaction_id = CAST(t.id AS TEXT)",
    ).rows[0]
    uncast = run(
        real_db,
        "SELECT count(*), sum(f.is_fraud) FROM transactions t "
        "JOIN fraud_labels f ON f.transaction_id = t.id",
    ).rows[0]
    assert cast == uncast == (777_339, 1360)
    # With the cast, a LEFT JOIN uses the label index and finishes quickly.
    unlabelled = run(
        real_db,
        "SELECT count(*) FROM transactions t LEFT JOIN fraud_labels f "
        "ON f.transaction_id = CAST(t.id AS TEXT) WHERE f.transaction_id IS NULL",
        timeout_seconds=10,
    )
    assert unlabelled.rows == ((1_159_966 - 777_339,),)


def test_note_facts_hold(real_db: Path) -> None:
    date_format = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
    assert scalar(
        real_db,
        f"SELECT count(*) FROM transactions WHERE date NOT GLOB '{date_format} [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'",
    ) == 0
    assert scalar(
        real_db,
        f"SELECT (SELECT count(*) FROM users WHERE birth_date NOT GLOB '{date_format}') + "
        f"(SELECT count(*) FROM cards WHERE expires NOT GLOB '{date_format}' "
        f"OR acct_open_date NOT GLOB '{date_format}')",
    ) == 0
    # Online locations: city 'ONLINE', empty state, no zip; and every online
    # transaction points at one.
    assert run(
        real_db,
        "SELECT count(*), sum(city = 'ONLINE'), sum(state = ''), sum(zip IS NULL) "
        "FROM merchant_locations WHERE city = 'ONLINE' OR state = ''",
    ).rows == ((105, 105, 105, 105),)
    assert scalar(
        real_db,
        "SELECT count(*) FROM transactions t JOIN merchant_locations l ON l.id = t.merchant_location_id "
        "WHERE (t.transaction_type = 'Online Transaction') != (l.city = 'ONLINE')",
    ) == 1084  # chip transactions at an online location; none the other way
    assert scalar(
        real_db,
        "SELECT count(*) FROM transactions t JOIN merchant_locations l ON l.id = t.merchant_location_id "
        "WHERE t.transaction_type = 'Online Transaction' AND l.city != 'ONLINE'",
    ) == 0
    # Two-letter states have a zip (US); longer ones (countries) do not.
    assert run(
        real_db,
        "SELECT length(state) = 2, count(*), count(zip) FROM merchant_locations "
        "WHERE state != '' GROUP BY 1 ORDER BY 1",
    ).rows == ((0, 1253, 0), (1, 69_181, 69_181))
    assert scalar(real_db, "SELECT count(*) FROM transactions WHERE errors IS NULL") == 1_141_312


def test_backtest_of_the_brief_rule_matches_an_independent_query(real_db: Path) -> None:
    # The brief's example ($1,000, debit cards) with the real column and value.
    result = backtest(
        "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')",
        db_path=real_db,
        timeout_seconds=30.0,
    )
    # Written differently on purpose: joins instead of a subquery, and the
    # labelled side driven from fraud_labels with the cast on the other column.
    flagged, flagged_amount = run(
        real_db,
        "SELECT count(*), sum(t.amount_usd_cents) FROM cards c JOIN transactions t ON t.card_id = c.id "
        "WHERE c.card_type = 'Debit' AND t.amount_usd_cents >= 100001",
    ).rows[0]
    labelled, fraud, fraud_amount, legitimate_amount = run(
        real_db,
        "SELECT count(*), sum(f.is_fraud), sum(f.is_fraud * t.amount_usd_cents), "
        "sum((1 - f.is_fraud) * t.amount_usd_cents) "
        "FROM fraud_labels f JOIN transactions t ON t.id = CAST(f.transaction_id AS INTEGER) "
        "JOIN cards c ON c.id = t.card_id "
        "WHERE c.card_type = 'Debit' AND t.amount_usd_cents >= 100001",
    ).rows[0]
    assert isinstance(flagged, int) and isinstance(labelled, int) and isinstance(fraud, int)
    assert (result.flagged, result.flagged_amount_cents) == (flagged, flagged_amount)
    assert (result.flagged_labelled, result.true_positives) == (labelled, fraud)
    assert result.false_positives == labelled - fraud
    assert result.flagged_unlabelled == flagged - labelled
    assert result.fraud_amount_caught_cents == fraud_amount
    assert result.false_positive_amount_cents == legitimate_amount
    assert (result.total_transactions, result.total_labelled, result.total_fraud) == (1_159_966, 777_339, 1360)
    assert result.precision == fraud / labelled
    assert result.recall == fraud / 1360
    assert result.flag_rate == flagged / 1_159_966
    # The measured values, so a change in either query shows up.
    assert (flagged, labelled, fraud) == (350, 238, 2)
    assert result.elapsed_seconds < 5


def test_backtest_of_a_rule_that_flags_everything(real_db: Path) -> None:
    result = backtest("1=1", db_path=real_db, timeout_seconds=30.0)
    assert (result.flagged, result.flagged_labelled, result.flagged_unlabelled) == (1_159_966, 777_339, 382_627)
    assert (result.true_positives, result.false_positives) == (1360, 775_979)
    assert result.recall == 1.0
    assert result.precision == 1360 / 777_339  # the base fraud rate
    assert result.flag_rate == 1.0
    assert result.elapsed_seconds < 5


BRIEF_RULE = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"
ROME_RULE = "merchant_location_id IN (SELECT id FROM merchant_locations WHERE city = 'Rome' AND state = 'Italy')"
LABEL_LEAK = "id IN (SELECT CAST(transaction_id AS INTEGER) FROM fraud_labels WHERE is_fraud = 1)"
MONTHLY_FRAUD_RATE = (
    "SELECT strftime('%Y-%m', transactions.date) AS month, count(*) AS labelled, "
    "sum(fraud_labels.is_fraud) AS fraud, 1.0 * sum(fraud_labels.is_fraud) / count(*) AS fraud_rate "
    f"FROM transactions JOIN fraud_labels ON {LABEL_JOIN} GROUP BY month ORDER BY month;"
)


def test_guard_accepts_the_reference_rules(real_db: Path) -> None:
    brief = check_rule_clause(BRIEF_RULE, db_path=real_db)
    assert brief.accepted, brief.reason
    assert brief.reads is not None
    assert brief.reads.tables == {"transactions", "cards"}
    rome = check_rule_clause(ROME_RULE, db_path=real_db)
    assert rome.accepted, rome.reason
    result = backtest(rome.sql, db_path=real_db, timeout_seconds=30.0)
    assert (result.flagged, result.flagged_labelled, result.true_positives, result.false_positives) == (
        2431,
        1598,
        1360,
        238,
    )


def test_guard_rejects_a_rule_that_reads_the_labels(real_db: Path) -> None:
    decision = check_rule_clause(LABEL_LEAK, db_path=real_db)
    assert not decision.accepted
    assert decision.reason_code is ReasonCode.LABEL_READ
    # What it would have scored: every labelled fraud and nothing else.
    leaked = backtest_module._backtest_unchecked(LABEL_LEAK, db_path=real_db, timeout_seconds=30.0)
    assert (leaked.flagged, leaked.true_positives, leaked.precision, leaked.recall) == (1360, 1360, 1.0, 1.0)


def test_backtest_refuses_a_rule_that_reads_the_labels(real_db: Path) -> None:
    with pytest.raises(RuleClauseError) as raised:
        backtest(LABEL_LEAK, db_path=real_db, timeout_seconds=30.0)
    assert raised.value.reason_code is ReasonCode.LABEL_READ


def test_guard_rejects_a_statement_that_would_hang_the_compiler(real_db: Path) -> None:
    # Each CTE joins the one before to itself: 799 characters that took 45 s and 1.7 GB to compile.
    parts = ["a0 AS (SELECT 1 x)"] + [f"a{n} AS (SELECT a.x FROM a{n - 1} a, a{n - 1} b)" for n in range(1, 21)]
    sql = "WITH " + ", ".join(parts) + " SELECT count(*) FROM a20"
    assert len(sql) == 799
    check_query("SELECT 1", db_path=real_db)
    for check, text in ((check_query, sql), (check_rule_clause, f"id IN ({sql})")):
        started = time.monotonic()
        decision = check(text, db_path=real_db)
        assert time.monotonic() - started < 0.25
        assert decision.reason_code is ReasonCode.TOO_COMPLEX


def test_a_result_of_large_values_is_stopped_by_the_byte_budget(real_db: Path) -> None:
    decision = check_query("SELECT zeroblob(100000) FROM transactions", db_path=real_db)
    assert decision.accepted  # the guard decides scope; the size of a result is db's to bound
    with pytest.raises(db.ResultTooLarge, match="over 5,000,000 bytes by row 51"):
        run(real_db, decision.sql, row_cap=1000)
    with pytest.raises(db.QueryError, match="too big"):
        run(real_db, "SELECT zeroblob(1000000) FROM transactions")
    # An ordinary full page is far below the budget.
    page = run(real_db, "SELECT * FROM transactions", row_cap=1000)
    assert page.row_count == 1000 and page.truncated


def test_the_briefs_column_name_in_double_quotes_is_an_error(real_db: Path) -> None:
    decision = check_rule_clause('"amount" > 1000', db_path=real_db)
    assert not decision.accepted
    assert decision.reason_code is ReasonCode.SQLITE_ERROR
    assert decision.reason is not None
    assert 'no such column: "amount"' in decision.reason
    with pytest.raises(db.QueryError):
        run(real_db, 'SELECT count(*) FROM transactions WHERE "amount" > 1000')


def test_guard_accepts_the_monthly_fraud_rate_query_quickly(real_db: Path) -> None:
    check_query("SELECT 1", db_path=real_db)  # reads the table list once
    started = time.monotonic()
    decision = check_query(MONTHLY_FRAUD_RATE, db_path=real_db)
    assert time.monotonic() - started < 0.1  # compiled, not run: the query itself takes about a second
    assert decision.accepted, decision.reason
    assert decision.reads is not None
    assert decision.reads.tables == {"transactions", "fraud_labels"}
    result = run(real_db, decision.sql)
    assert result.row_count == 10
    assert sum(as_int(row[1]) for row in result.rows) == 777_339
    assert sum(as_int(row[2]) for row in result.rows) == 1360


def test_guard_allows_exactly_the_seven_tables(real_db: Path) -> None:
    decision = check_query("SELECT * FROM sqlite_master", db_path=real_db)
    assert decision.reason_code is ReasonCode.TABLE_NOT_ALLOWED
    assert decision.reason is not None
    assert decision.reason.endswith(
        "The tables are: cards, fraud_labels, mcc_codes, merchant_locations, merchants, transactions, users."
    )
