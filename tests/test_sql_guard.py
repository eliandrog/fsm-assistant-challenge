"""sql_guard: what is accepted, what is rejected and why, on the fixture database."""

import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import file_sha256
from fsm_assistant import db
from fsm_assistant.rules import backtest as backtest_module
from fsm_assistant.rules.backtest import backtest
from fsm_assistant import sql_guard
from fsm_assistant.sql_guard import GuardDecision, ReasonCode, check_query, check_rule_clause, normalise

BRIEF_RULE = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"
# Never finishes if it is run.
ENDLESS = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"



def nested_ctes(step: str, levels: int) -> str:
    """A statement whose every CTE is built from the one before; `step` says how."""
    parts = ["a0 AS (SELECT 1 AS x)"] + [
        step.format(name=f"a{level}", previous=f"a{level - 1}") for level in range(1, levels + 1)
    ]
    return "WITH " + ", ".join(parts) + f" SELECT count(*) FROM a{levels}"


# Each step refers twice to the step before, so what SQLite compiles doubles per level.
DOUBLING_STEPS = {
    "join": "{name} AS (SELECT a.x FROM {previous} a, {previous} b)",
    "aggregate": "{name} AS (SELECT count(*) AS x FROM {previous} a, {previous} b)",
    "union all": "{name} AS (SELECT x FROM {previous} UNION ALL SELECT x FROM {previous})",
    "scalar subquery": "{name} AS (SELECT (SELECT x FROM {previous}) + (SELECT x FROM {previous}) AS x)",
}

ACCEPTED_QUERIES = {
    "join": "SELECT t.id, c.card_type FROM transactions t JOIN cards c ON c.id = t.card_id",
    "label join": (
        "SELECT count(*), sum(f.is_fraud) FROM transactions t "
        "JOIN fraud_labels f ON f.transaction_id = CAST(t.id AS TEXT)"
    ),
    "subquery": "SELECT count(*) FROM transactions WHERE card_id IN (SELECT id FROM cards WHERE has_chip = 1)",
    "derived table": "SELECT max(n) FROM (SELECT count(*) AS n FROM transactions GROUP BY card_id) AS per_card",
    "cte": "WITH big AS (SELECT id, card_id FROM transactions WHERE amount_usd_cents > 1000) SELECT card_id FROM big",
    "cte read without naming a column": (
        "WITH per_card AS (SELECT card_id, count(*) AS n FROM transactions GROUP BY card_id) "
        "SELECT count(*) FROM per_card"
    ),
    "recursive cte": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 10) SELECT x FROM c",
    "window function": (
        "SELECT id, date, lag(date) OVER (PARTITION BY card_id ORDER BY date) AS previous FROM transactions"
    ),
    "aggregate with filter": (
        "SELECT count(*) FILTER (WHERE amount_usd_cents < 0), sum(amount_usd_cents) FROM transactions"
    ),
    "case": "SELECT CASE WHEN amount_usd_cents >= 100000 THEN 'high' ELSE 'low' END AS band, count(*) FROM transactions GROUP BY band",
    "cast": "SELECT CAST(amount_usd_cents AS REAL) / 100.0 FROM transactions",
    "strftime on the date column": "SELECT strftime('%Y-%m', date) AS month, count(*) FROM transactions GROUP BY month",
    "date arithmetic from a literal": "SELECT count(*) FROM transactions WHERE date >= date('2019-10-31', '-30 day')",
    "a column named date next to a date function": (
        "SELECT strftime('%H', date) FROM transactions WHERE date IN ('2019-01-01 00:02:00')"
    ),
    "like": "SELECT name FROM merchants WHERE name LIKE 'Merchant%'",
    "select star": "SELECT * FROM transactions",
    "cte named like a real table": "WITH transactions AS (SELECT 1 AS id) SELECT * FROM transactions",
    "cte named like a real table, no column": "WITH cards AS (SELECT 1 AS id) SELECT count(*) FROM cards",
    "random sample": "SELECT * FROM transactions ORDER BY random() LIMIT 5",
    "table name in another case": "SELECT count(*) FROM TRANSACTIONS",
    "comments": "-- monthly volume\nSELECT count(*) /* all */ FROM transactions",
    "the text 'now' with no date function": "SELECT count(*) FROM merchant_locations WHERE city = 'now'",
    "the text 'now' compared, next to a date function": (
        "SELECT strftime('%Y', date) y, count(*) FROM transactions WHERE errors = 'Now' GROUP BY y"
    ),
    "the text 'now' selected, next to a date function": (
        "SELECT strftime('%Y', date) y, 'now' AS label FROM transactions LIMIT 1"
    ),
    "the text 'now' in a list, next to a date function": (
        "SELECT date(date) FROM transactions WHERE errors IN ('now', 'subsec')"
    ),
    "subsec as a modifier after a time value": "SELECT datetime(date, 'subsec'), strftime('%f', date, 'subsecond') FROM transactions",
    "a cte named date": "WITH date AS (SELECT 'now' AS label) SELECT label, strftime('%Y', '2019-01-01') FROM date",
    "a cte named date with a column list": "WITH date(now) AS (SELECT 'now') SELECT now, date('2019-01-01') FROM date",
    "a cte named now": "WITH now AS (SELECT max(date) AS d FROM transactions) SELECT strftime('%Y', d) FROM now",
    "an alias named current": "SELECT date(current.date) FROM transactions AS current WHERE current.id = 1001",
    "aliases named time and now": "SELECT strftime('%H', date) AS time, 'now' AS now FROM transactions AS date",
    "a cte used several times": (
        "WITH m AS (SELECT strftime('%Y-%m', date) AS month, count(*) AS n FROM transactions GROUP BY 1) "
        "SELECT a.month, a.n, b.n, (SELECT max(n) FROM m), (SELECT min(n) FROM m) "
        "FROM m a LEFT JOIN m b ON b.month < a.month"
    ),
    "a chain of ctes": (
        "WITH a AS (SELECT id, card_id FROM transactions), b AS (SELECT * FROM a WHERE id > 0), "
        "c AS (SELECT * FROM b), d AS (SELECT * FROM c JOIN a USING (id)) SELECT count(*) FROM d"
    ),
    "a cte whose name is also a column alias in another": (
        "WITH a AS (SELECT 1 AS b), b AS (SELECT b FROM a) SELECT b FROM b"
    ),
    "named windows": (
        "SELECT sum(amount_usd_cents) OVER w, count(*) OVER v FROM transactions "
        "WINDOW w AS (PARTITION BY card_id ORDER BY date), v AS (PARTITION BY card_id)"
    ),
    "compound select": "SELECT id FROM cards UNION SELECT id FROM users",
    "values": "VALUES (1), (2)",
}


@pytest.mark.parametrize("sql", ACCEPTED_QUERIES.values(), ids=ACCEPTED_QUERIES.keys())
def test_query_shapes_that_are_accepted(fixture_db: Path, sql: str) -> None:
    decision = check_query(sql, db_path=fixture_db)
    assert decision.accepted, decision.reason
    assert decision.reason is None
    assert decision.reason_code is None
    assert decision.sql == sql
    assert decision.reads is not None
    # An accepted statement is one that db.run_query will run.
    db.run_query(decision.sql, db_path=fixture_db, row_cap=5, timeout_seconds=5.0)


def test_an_accepted_decision_reports_what_is_read(fixture_db: Path) -> None:
    decision = check_query(
        "SELECT lower(c.card_type), COUNT(*) FROM transactions t JOIN cards c ON c.id = t.card_id GROUP BY 1",
        db_path=fixture_db,
    )
    assert decision.accepted
    assert decision.reads.tables == {"transactions", "cards"}
    assert decision.reads.columns_of("cards") == {"id", "card_type"}
    assert decision.reads.functions == {"lower", "count"}


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("  SELECT 1  ", "SELECT 1"),
        ("SELECT 1;", "SELECT 1"),
        ("SELECT 1 ;\n", "SELECT 1"),
        ("SELECT 1;;", "SELECT 1;"),
        ("SELECT ';'", "SELECT ';'"),
    ],
)
def test_normalisation_trims_and_drops_one_trailing_semicolon(raw: str, normalised: str) -> None:
    assert normalise(raw) == normalised


def test_the_decision_carries_the_normalised_text(fixture_db: Path) -> None:
    decision = check_query("\n SELECT count(*) FROM users ; ", db_path=fixture_db)
    assert decision.accepted
    assert decision.sql == "SELECT count(*) FROM users"


TABLES = "The tables are: cards, fraud_labels, mcc_codes, merchant_locations, merchants, transactions, users."

REJECTED_QUERIES = [
    # More than one statement.
    ("SELECT 1; SELECT 2", "Only one statement is allowed"),
    ("SELECT 1; DELETE FROM fraud_labels", "Only one statement is allowed"),
    ("SELECT 1;;", "Only one statement is allowed"),
    ("SELECT 1; -- done", "Only one statement is allowed"),
    # Not a read.
    ("INSERT INTO mcc_codes VALUES (1, 'x')", "denied: INSERT mcc_codes"),
    ("UPDATE cards SET card_type = 'Debit'", "denied: UPDATE cards"),
    ("DELETE FROM fraud_labels", "denied: DELETE fraud_labels"),
    ("WITH x AS (SELECT 1) DELETE FROM fraud_labels", "denied: DELETE fraud_labels"),
    ("CREATE TABLE t (a)", "Only read-only queries are allowed"),
    ("CREATE TEMP TABLE t (a)", "Only read-only queries are allowed"),
    ("DROP TABLE transactions", "Only read-only queries are allowed"),
    ("ATTACH DATABASE 'other.db' AS other", "denied: ATTACH"),
    ("PRAGMA journal_mode = DELETE", "denied: PRAGMA journal_mode"),
    ("BEGIN", "denied: TRANSACTION"),
    # Allowed by the read-only connection, but not a SELECT.
    ("PRAGMA table_info(cards)", "Only SELECT statements are allowed, not PRAGMA"),
    ("VACUUM", "VACUUM"),
    # A plan is not an answer.
    ("EXPLAIN SELECT 1", "EXPLAIN is not allowed"),
    ("explain query plan SELECT * FROM cards", "EXPLAIN is not allowed"),
    ("/* why */ EXPLAIN SELECT 1", "EXPLAIN is not allowed"),
    ("QUERY PLAN SELECT 1", 'near "QUERY": syntax error'),
    # Nothing to run.
    ("", "The statement is empty"),
    ("   \n", "The statement is empty"),
    ("-- only a comment", "The statement is empty"),
    ("/* only a comment */", "The statement is empty"),
    (";", "The statement is empty"),
    # System objects.
    ("SELECT name FROM sqlite_master", "sqlite_master is not a table that may be read. " + TABLES),
    ("SELECT name FROM sqlite_schema", "sqlite_master is not a table that may be read"),
    ("SELECT count(*) FROM sqlite_master", "sqlite_master is not a table that may be read"),
    ("SELECT 1 FROM sqlite_temp_master", "sqlite_temp_master is not a table that may be read"),
    ("SELECT name FROM pragma_table_info('cards')", "pragma_table_info is not a table that may be read"),
    ("SELECT count(*) FROM pragma_table_info('cards')", "pragma_table_info is not a table that may be read"),
    ("SELECT * FROM pragma_database_list", "pragma_database_list is not a table that may be read"),
    ("SELECT value FROM json_each('[1, 2]')", "json_each is not a table that may be read"),
    ("SELECT count(*) FROM json_tree('[1, 2]')", "json_tree is not a table that may be read"),
    # A CTE that borrows a system name does not hide a read of the real one.
    (
        "SELECT (WITH sqlite_master AS (SELECT 1 AS a) SELECT count(*) FROM sqlite_master), "
        "(SELECT count(*) FROM sqlite_master)",
        "sqlite_master is not a table that may be read",
    ),
    # Functions.
    ("SELECT load_extension('evil')", "The function load_extension is not allowed"),
    ("SELECT current_timestamp", "The function current_timestamp is not allowed"),
    ("SELECT count(*) FROM transactions WHERE date > CURRENT_DATE", "The function current_date is not allowed"),
    ("SELECT current_time", "The function current_time is not allowed"),
    # Today's date, on data that ends in 2019.
    ("SELECT count(*) FROM transactions WHERE date >= date('now')", "passes 'now' to a date function"),
    ("SELECT count(*) FROM transactions WHERE date >= date('now', '-30 day')", "passes 'now' to a date function"),
    ("SELECT datetime('NOW', 'start of month', '-1 month')", "passes 'now' to a date function"),
    ("SELECT strftime('%Y', 'now')", "passes 'now' to a date function"),
    ("SELECT julianday('now') - julianday(date) FROM transactions", "passes 'now' to a date function"),
    ("SELECT date(coalesce(NULL, 'now'))", "passes 'now' to a date function"),
    ("SELECT timediff('now', date) FROM transactions", "passes 'now' to a date function"),
    ("SELECT datetime('subsec')", "passes 'subsec' to datetime() as its time value"),
    ("SELECT unixepoch('subsec')", "passes 'subsec' to unixepoch() as its time value"),
    ("SELECT strftime('%Y-%m-%d', 'subsecond')", "passes 'subsecond' to strftime() as its time value"),
    ("SELECT date('SUBSEC', '-1 day')", "passes 'SUBSEC' to date() as its time value"),
    ("SELECT timediff(date, 'subsec') FROM transactions", "passes 'subsec' to timediff() as its time value"),
    ("SELECT date()", "date() is called without a time value"),
    ("SELECT count(*) FROM transactions WHERE date > DATE ( )", "date() is called without a time value"),
    ('SELECT "date"()', "date() is called without a time value"),
    ("SELECT datetime(/* today */)", "datetime() is called without a time value"),
    ("SELECT time()", "time() is called without a time value"),
    ("SELECT julianday()", "julianday() is called without a time value"),
    ("SELECT unixepoch()", "unixepoch() is called without a time value"),
    ("SELECT strftime('%Y')", "strftime() is called without a time value"),
    ("SELECT count(*) FROM transactions WHERE strftime('%Y-%m', date) = strftime('%Y-%m')", "strftime() is called"),
    # Engine and build details.
    ("SELECT sqlite_version()", "The function sqlite_version is not allowed"),
    ("SELECT sqlite_source_id()", "The function sqlite_source_id is not allowed"),
    ("SELECT sqlite_compileoption_get(0)", "The function sqlite_compileoption_get is not allowed"),
    ("SELECT sqlite_compileoption_used('ENABLE_FTS5')", "The function sqlite_compileoption_used is not allowed"),
    # A Markdown fence is not unwrapped.
    ("```sql\nSELECT 1\n```", "wrapped in a Markdown code fence"),
    ("```\nSELECT count(*) FROM cards;\n```", "Return the bare statement only"),
    ("SELECT 1\n```", "wrapped in a Markdown code fence"),
    # Text SQLite cannot be given.
    ("SELECT '\ud800'", "not valid Unicode"),
    # Names SQLite does not know.
    ("SELECT amount FROM transactions", "SQLite rejected the statement: no such column: amount"),
    ("SELECT * FROM payments", "SQLite rejected the statement: no such table: payments"),
    ('SELECT count(*) FROM transactions WHERE "amount" > 1000', 'no such column: "amount"'),
    ("SELEC 1", "syntax error"),
    ("SELECT 'abc", "unterminated ' quote"),
    ("SELECT id FROM cards WHERE id = ?", "bindings"),
]


@pytest.mark.parametrize(("sql", "reason"), REJECTED_QUERIES)
def test_rejected_queries_say_why(fixture_db: Path, sql: str, reason: str) -> None:
    decision = check_query(sql, db_path=fixture_db)
    assert not decision.accepted
    assert reason in decision.reason
    assert decision.reason_code is not None


def test_dbstat_is_rejected_whether_or_not_sqlite_has_it(fixture_db: Path) -> None:
    decision = check_query("SELECT name, pageno FROM dbstat", db_path=fixture_db)
    assert not decision.accepted
    assert "dbstat" in decision.reason


def test_the_guard_never_runs_the_statement(fixture_db: Path) -> None:
    before = file_sha256(fixture_db)
    started = time.monotonic()
    decision = check_query(ENDLESS, db_path=fixture_db)
    assert time.monotonic() - started < 0.5
    assert decision.accepted  # a read-only SELECT; the timeout is db.run_query's job
    assert not check_query("DELETE FROM fraud_labels", db_path=fixture_db).accepted
    assert file_sha256(fixture_db) == before


def test_decisions_are_immutable(fixture_db: Path) -> None:
    decision = check_query("SELECT 1", db_path=fixture_db)
    assert isinstance(decision, GuardDecision)
    with pytest.raises(ValidationError):
        decision.accepted = False  # type: ignore[misc]  # assigning to a frozen field is what is tested
    with pytest.raises(ValidationError):
        decision.reads.actions = ()  # type: ignore[misc]  # assigning to a frozen field is what is tested
    with pytest.raises(ValidationError):
        GuardDecision(accepted=True, sql="SELECT 1", reason="left over")


def test_a_missing_database_is_an_error_not_a_rejection(tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseNotFoundError):
        check_query("SELECT 1", db_path=tmp_path / "absent.db")
    with pytest.raises(db.DatabaseNotFoundError):
        check_rule_clause("1=1", db_path=tmp_path / "absent.db")


ACCEPTED_RULES = [
    BRIEF_RULE,
    "amount_usd_cents < 0",
    "transactions.transaction_type = 'Online Transaction' AND errors IS NOT NULL",
    "merchant_location_id IN (SELECT id FROM merchant_locations WHERE city = 'Rome' AND state = 'Italy')",
    "merchant_id IN (SELECT m.id FROM merchants m JOIN mcc_codes c ON c.mcc = m.mcc WHERE c.description LIKE '%Transfer%')",
    "CAST(strftime('%H', date) AS INTEGER) < 6",
    "date >= '2019-03-01' AND date < '2019-04-01'",
    "card_id IN (WITH prepaid AS (SELECT id FROM cards WHERE card_type = 'Debit (Prepaid)') SELECT id FROM prepaid)",
    "errors = 'now'",
]


@pytest.mark.parametrize("clause", ACCEPTED_RULES)
def test_rules_that_are_accepted(fixture_db: Path, clause: str) -> None:
    decision = check_rule_clause(clause, db_path=fixture_db)
    assert decision.accepted, decision.reason
    assert decision.sql == clause
    assert "transactions" in decision.reads.tables
    backtest(decision.sql, db_path=fixture_db, timeout_seconds=5.0)


def test_the_rule_decision_reports_what_the_clause_alone_reads(fixture_db: Path) -> None:
    decision = check_rule_clause(f"  {BRIEF_RULE} ; ", db_path=fixture_db)
    assert decision.accepted
    assert decision.sql == BRIEF_RULE
    assert decision.reads.tables == {"transactions", "cards"}
    assert decision.reads.columns_of("transactions") == {"amount_usd_cents", "card_id"}


LABEL_LEAKS = {
    "subquery": "id IN (SELECT CAST(transaction_id AS INTEGER) FROM fraud_labels WHERE is_fraud = 1)",
    "alias": (
        "EXISTS (SELECT 1 FROM fraud_labels AS f "
        "WHERE f.transaction_id = CAST(transactions.id AS TEXT) AND f.is_fraud = 1)"
    ),
    "cte inside a subquery": (
        "id IN (WITH known AS (SELECT transaction_id FROM fraud_labels WHERE is_fraud = 1) "
        "SELECT CAST(transaction_id AS INTEGER) FROM known)"
    ),
    "quoted name": 'id IN (SELECT CAST("transaction_id" AS INTEGER) FROM "fraud_labels" WHERE "is_fraud" = 1)',
    "bracket-quoted name in another case": "id IN (SELECT CAST(transaction_id AS INTEGER) FROM [FRAUD_LABELS])",
    "main-qualified": "id IN (SELECT CAST(transaction_id AS INTEGER) FROM main.fraud_labels WHERE is_fraud = 1)",
    "count(*)": "(SELECT count(*) FROM fraud_labels) > 0",
    "exists select 1": "EXISTS (SELECT 1 FROM fraud_labels)",
    "union": "id IN (SELECT id FROM cards UNION SELECT CAST(transaction_id AS INTEGER) FROM fraud_labels)",
    "union all, labels first": (
        "id IN (SELECT CAST(transaction_id AS INTEGER) FROM fraud_labels WHERE is_fraud = 1 UNION ALL SELECT 0)"
    ),
    "except": "id NOT IN (SELECT id FROM transactions EXCEPT SELECT CAST(transaction_id AS INTEGER) FROM fraud_labels)",
    "intersect": "id IN (SELECT id FROM transactions INTERSECT SELECT transaction_id + 0 FROM fraud_labels)",
    "join that names no column": "EXISTS (SELECT 1 FROM cards JOIN fraud_labels)",
    "comma join that names no column": "(SELECT count(*) FROM cards, fraud_labels) > 0",
    "left join that names no label column": "id IN (SELECT c.id FROM cards c LEFT JOIN fraud_labels ON 1)",
    "derived table": "(SELECT n FROM (SELECT count(*) AS n FROM fraud_labels)) > 0",
}


@pytest.mark.parametrize("clause", LABEL_LEAKS.values(), ids=LABEL_LEAKS.keys())
def test_a_rule_that_reads_the_labels_is_rejected(fixture_db: Path, clause: str) -> None:
    decision = check_rule_clause(clause, db_path=fixture_db)
    assert not decision.accepted
    assert decision.reason_code is ReasonCode.LABEL_READ
    assert "A rule must not read fraud_labels" in decision.reason
    assert "fraud_labels" in decision.reads.tables


def test_why_label_reads_are_rejected(fixture_db: Path) -> None:
    # Without the guard, a clause that selects the labelled fraud scores perfectly.
    leaked = backtest_module._backtest_unchecked(
        LABEL_LEAKS["subquery"], db_path=fixture_db, timeout_seconds=5.0
    )
    assert (leaked.precision, leaked.recall) == (1.0, 1.0)
    # The same table is fine in a query.
    assert check_query("SELECT count(*) FROM fraud_labels WHERE is_fraud = 1", db_path=fixture_db).accepted


REJECTED_RULES = [
    ("random() % 2 = 0", "The function random is not allowed in a rule"),
    ("changes() = 0", "The function changes is not allowed in a rule"),
    ("total_changes() = 0", "The function total_changes is not allowed in a rule"),
    ("last_insert_rowid() = id", "The function last_insert_rowid is not allowed in a rule"),
    ("sqlite_version() >= '3.40'", "The function sqlite_version is not allowed"),
    ("sqlite_source_id() <> ''", "The function sqlite_source_id is not allowed"),
    ("sqlite_compileoption_get(0) IS NULL", "The function sqlite_compileoption_get is not allowed"),
    ("sqlite_compileoption_used('THREADSAFE')", "The function sqlite_compileoption_used is not allowed"),
    ("date >= unixepoch('subsec')", "passes 'subsec' to unixepoch() as its time value"),
    ("```sql\namount_usd_cents < 0\n```", "Return the bare clause only"),
    ("id IN (SELECT id FROM transactions ORDER BY RANDOM() LIMIT 3)", "The function random is not allowed in a rule"),
    ("length(randomblob(4)) = 4", "The function randomblob is not allowed in a rule"),
    ("date >= date('now', '-7 day')", "passes 'now' to a date function"),
    ("date >= datetime()", "datetime() is called without a time value"),
    ("date < current_timestamp", "The function current_timestamp is not allowed"),
    ("amount > 1000", "SQLite rejected the clause: no such column: amount"),
    ('"amount" > 1000', 'no such column: "amount"'),
    ("id IN (SELECT rootpage FROM sqlite_master)", "sqlite_master is not a table that may be read"),
    ("id IN (SELECT cid FROM pragma_table_info('cards'))", "pragma_table_info is not a table that may be read"),
    ("SELECT 1", "syntax error"),
    ("EXPLAIN SELECT 1", "syntax error"),
    ("1=1 LIMIT 1", "syntax error"),
    # Containment (backtest.check_clause) comes first.
    ("1=1; DROP TABLE transactions", "';'"),
    ("1=1 -- the rest", "comment"),
    ("1=0) UNION SELECT id, amount_usd_cents FROM transactions WHERE (1=1", "')' with no matching '('"),
    ("id = ?", "bind parameters"),
    ("", "empty"),
    (";", "empty"),
]


@pytest.mark.parametrize(("clause", "reason"), REJECTED_RULES)
def test_rejected_rules_say_why(fixture_db: Path, clause: str, reason: str) -> None:
    decision = check_rule_clause(clause, db_path=fixture_db)
    assert not decision.accepted
    assert reason in decision.reason
    assert decision.reason_code is not None


def test_random_is_allowed_in_a_query_but_not_in_a_rule(fixture_db: Path) -> None:
    assert check_query("SELECT id FROM transactions ORDER BY random() LIMIT 3", db_path=fixture_db).accepted
    assert not check_rule_clause("abs(random()) % 10 = 0", db_path=fixture_db).accepted


def test_containment_is_checked_before_the_database_is_touched(tmp_path: Path) -> None:
    # The database does not exist: the rejection came from the lexical scan.
    decision = check_rule_clause("1=1) OR (1=1", db_path=tmp_path / "absent.db")
    assert not decision.accepted
    assert decision.reason_code is ReasonCode.CLAUSE_NOT_CONTAINED
    assert "')' with no matching '('" in decision.reason
    assert decision.reads is None


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("", ReasonCode.EMPTY),
        ("SELECT 1; SELECT 2", ReasonCode.MULTIPLE_STATEMENTS),
        ("EXPLAIN SELECT 1", ReasonCode.EXPLAIN),
        ("SELECT 'abc", ReasonCode.UNTERMINATED_QUOTE),
        ("SELECT amount FROM transactions", ReasonCode.SQLITE_ERROR),
        ("DELETE FROM users", ReasonCode.NOT_READ_ONLY),
        ("VACUUM", ReasonCode.NOT_SELECT),
        ("PRAGMA table_info(cards)", ReasonCode.NOT_SELECT),
        ("SELECT * FROM sqlite_master", ReasonCode.TABLE_NOT_ALLOWED),
        ("SELECT load_extension('evil')", ReasonCode.FUNCTION_NOT_ALLOWED),
        ("SELECT current_date", ReasonCode.RELATIVE_TO_TODAY),
        ("SELECT date('now')", ReasonCode.RELATIVE_TO_TODAY),
        ("SELECT date()", ReasonCode.RELATIVE_TO_TODAY),
        ("SELECT time('subsec')", ReasonCode.RELATIVE_TO_TODAY),
        ("SELECT sqlite_version()", ReasonCode.FUNCTION_NOT_ALLOWED),
        ("```sql\nSELECT 1\n```", ReasonCode.CODE_FENCE),
        ("SELECT '\ud800'", ReasonCode.INVALID_TEXT),
        ("SELECT '" + "a" * sql_guard.MAX_STATEMENT_CHARACTERS + "'", ReasonCode.TOO_LONG),
        (nested_ctes("{name} AS (SELECT a.x FROM {previous} a, {previous} b)", 20), ReasonCode.TOO_COMPLEX),
    ],
)
def test_query_reason_codes(fixture_db: Path, sql: str, code: ReasonCode) -> None:
    assert check_query(sql, db_path=fixture_db).reason_code is code


@pytest.mark.parametrize(
    ("clause", "code"),
    [
        ("1=1 -- x", ReasonCode.CLAUSE_NOT_CONTAINED),
        ("amount > 1000", ReasonCode.SQLITE_ERROR),
        ("1=1 LIMIT 1", ReasonCode.SQLITE_ERROR),
        ("EXISTS (SELECT 1 FROM fraud_labels)", ReasonCode.LABEL_READ),
        ("random() > 0", ReasonCode.NOT_DETERMINISTIC),
        ("id IN (SELECT rootpage FROM sqlite_master)", ReasonCode.TABLE_NOT_ALLOWED),
        ("date > date('now')", ReasonCode.RELATIVE_TO_TODAY),
        ("date > datetime('subsec')", ReasonCode.RELATIVE_TO_TODAY),
        ("", ReasonCode.EMPTY),
        ("errors = 'Bad PIN", ReasonCode.UNTERMINATED_QUOTE),
        ("changes() = 0", ReasonCode.NOT_DETERMINISTIC),
        ("sqlite_version() > '3'", ReasonCode.FUNCTION_NOT_ALLOWED),
        ("```\nid = 1\n```", ReasonCode.CODE_FENCE),
        ("errors = '\udc00'", ReasonCode.INVALID_TEXT),
        ("id IN (" + ", ".join(["1001"] * 2000) + ")", ReasonCode.TOO_LONG),
        (
            "id IN (" + nested_ctes("{name} AS (SELECT a.x FROM {previous} a, {previous} b)", 20) + ")",
            ReasonCode.TOO_COMPLEX,
        ),
    ],
)
def test_rule_reason_codes(fixture_db: Path, clause: str, code: ReasonCode) -> None:
    assert check_rule_clause(clause, db_path=fixture_db).reason_code is code


@pytest.mark.parametrize("step", DOUBLING_STEPS.values(), ids=DOUBLING_STEPS.keys())
def test_a_statement_that_would_take_long_to_compile_is_rejected_at_once(fixture_db: Path, step: str) -> None:
    # Nineteen levels: about a thousand characters, and many seconds for SQLite to compile.
    sql = nested_ctes(step, 19)
    assert len(sql) < 1300
    for check, text in ((check_query, sql), (check_rule_clause, f"id IN ({sql})")):
        started = time.monotonic()
        decision = check(text, db_path=fixture_db)
        assert time.monotonic() - started < 0.25
        assert decision.reason_code is ReasonCode.TOO_COMPLEX
        assert "Refer to each WITH name fewer times" in decision.reason
        assert decision.reads is None  # never compiled


@pytest.mark.parametrize("step", DOUBLING_STEPS.values(), ids=DOUBLING_STEPS.keys())
def test_whatever_passes_the_complexity_check_is_judged_quickly(fixture_db: Path, step: str) -> None:
    passed = 0
    for levels in range(1, 20):
        started = time.monotonic()
        decision = check_query(nested_ctes(step, levels), db_path=fixture_db)
        if decision.reason_code is ReasonCode.TOO_COMPLEX:
            break
        assert time.monotonic() - started < 0.5
        passed = levels
    assert 5 <= passed < 19


def test_the_reported_statement_is_rejected(fixture_db: Path) -> None:
    sql = nested_ctes("{name} AS (SELECT a.x FROM {previous} a, {previous} b)", 20)
    started = time.monotonic()
    assert check_query(sql, db_path=fixture_db).reason_code is ReasonCode.TOO_COMPLEX
    assert time.monotonic() - started < 0.25


def test_a_wide_fan_out_is_too_complex_in_few_steps(fixture_db: Path) -> None:
    # A count of CTEs alone would not bound the cost: four steps, each joining the one before sixty times.
    step = "{name} AS (SELECT a.x FROM {previous} a" + ", {previous}" * 59 + ")"
    started = time.monotonic()
    decision = check_query(nested_ctes(step, 3), db_path=fixture_db)
    assert time.monotonic() - started < 0.25
    assert decision.reason_code is ReasonCode.TOO_COMPLEX


def test_the_number_of_definitions_is_bounded(fixture_db: Path) -> None:
    def chain(length: int) -> str:
        parts = ["c0 AS (SELECT id FROM cards)"] + [f"c{n} AS (SELECT id FROM c{n - 1})" for n in range(1, length)]
        return "WITH " + ", ".join(parts) + f" SELECT count(*) FROM c{length - 1}"

    assert check_query(chain(sql_guard.MAX_DEFINITIONS), db_path=fixture_db).accepted
    decision = check_query(chain(sql_guard.MAX_DEFINITIONS + 1), db_path=fixture_db)
    assert decision.reason_code is ReasonCode.TOO_COMPLEX
    assert "has 21 definitions" in decision.reason and "the limit is 20" in decision.reason


def test_nested_with_clauses_are_not_counted_as_fan_out(fixture_db: Path) -> None:
    sql = "SELECT id FROM cards"
    for level in range(sql_guard.MAX_DEFINITIONS):
        sql = f"WITH n{level} AS ({sql}) SELECT id FROM n{level}"
    assert check_query(sql, db_path=fixture_db).accepted


def test_the_length_limit(fixture_db: Path) -> None:
    def padded(length: int) -> str:
        return "SELECT '" + "a" * (length - len("SELECT ''")) + "'"

    assert check_query(padded(sql_guard.MAX_STATEMENT_CHARACTERS), db_path=fixture_db).accepted
    started = time.monotonic()
    decision = check_query("SELECT " + ", ".join(["1"] * 500_000), db_path=fixture_db)
    assert time.monotonic() - started < 0.25
    assert decision.reason_code is ReasonCode.TOO_LONG
    assert "1,500,005 characters long; the limit is 10,000" in decision.reason
    rule = check_rule_clause(f"errors = '{'a' * sql_guard.MAX_STATEMENT_CHARACTERS}'", db_path=fixture_db)
    assert rule.reason_code is ReasonCode.TOO_LONG
    assert "Write a shorter clause" in rule.reason


def test_the_longest_statement_allowed_is_judged_quickly(fixture_db: Path) -> None:
    longest = ("SELECT " + "1+" * sql_guard.MAX_STATEMENT_CHARACTERS)[: sql_guard.MAX_STATEMENT_CHARACTERS - 1] + "1"
    started = time.monotonic()
    check_query(longest, db_path=fixture_db)
    assert time.monotonic() - started < 0.5


def test_text_checks_come_before_the_database_is_touched(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"
    for text in ("```sql\nSELECT 1\n```", "SELECT '\ud800'", "SELECT 'abc", nested_ctes(DOUBLING_STEPS["join"], 19)):
        assert not check_query(text, db_path=absent).accepted
        assert not check_rule_clause(text, db_path=absent).accepted


def test_known_limits_of_the_today_check(fixture_db: Path) -> None:
    # Documented, not chased: the current time reached without a literal 'now' in a date call.
    for sql in (
        "SELECT date('no' || 'w')",
        "SELECT date(x'6e6f77')",
        "WITH t AS (SELECT 'now' AS today) SELECT date(today) FROM t",
    ):
        assert check_query(sql, db_path=fixture_db).accepted


def test_a_rule_is_not_offered_the_label_table(fixture_db: Path) -> None:
    rule = check_rule_clause("id IN (SELECT rootpage FROM sqlite_master)", db_path=fixture_db)
    assert rule.reason.endswith("The tables are: cards, mcc_codes, merchant_locations, merchants, transactions, users.")
    query = check_query("SELECT rootpage FROM sqlite_master", db_path=fixture_db)
    assert query.reason.endswith(TABLES)


def test_a_compile_error_keeps_sqlites_own_message(fixture_db: Path) -> None:
    query = check_query("SELECT amount FROM transactions", db_path=fixture_db)
    assert query.sqlite_message == "no such column: amount"
    rule = check_rule_clause("amount > 1000", db_path=fixture_db)
    assert rule.sqlite_message == "no such column: amount"
    assert check_query("SELECT 1; SELECT 2", db_path=fixture_db).sqlite_message is None


def test_information_functions_are_allowed_nowhere_and_state_functions_only_in_queries(fixture_db: Path) -> None:
    assert check_query("SELECT changes(), total_changes(), last_insert_rowid()", db_path=fixture_db).accepted
    for name in ("sqlite_version", "sqlite_source_id"):
        assert check_query(f"SELECT {name}()", db_path=fixture_db).reason_code is ReasonCode.FUNCTION_NOT_ALLOWED
        assert check_rule_clause(f"{name}() <> ''", db_path=fixture_db).reason_code is ReasonCode.FUNCTION_NOT_ALLOWED
