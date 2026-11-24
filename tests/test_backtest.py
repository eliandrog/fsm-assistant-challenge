"""Rule backtest on a small database where every metric can be computed by hand."""

import sqlite3
from pathlib import Path

import pytest

from conftest import DATA_DIR
from fsm_assistant import db
from fsm_assistant.rules.backtest import BacktestResult, RuleClauseError, backtest, check_clause

# Ten transactions: seven labelled (three fraud), three unlabelled.
#
#   id  card            amount   label
#    1  100 Credit        2500   legitimate
#    2  101 Debit       150000   fraud
#    3  101 Debit        -5000   legitimate
#    4  102 Prepaid        999   legitimate
#    5  100 Credit        4200   (none)
#    6  102 Prepaid     120000   (none)
#    7  101 Debit       200000   legitimate
#    8  100 Credit      300000   fraud
#    9  101 Debit        50000   fraud
#   10  101 Debit       110000   (none)
ROWS = """
INSERT INTO users VALUES (1, '1980-01-31', 'Female', '1 Elm Street', 40.0, -74.0, 3000000, 6000000, 0, 720);
INSERT INTO mcc_codes VALUES (5411, 'Grocery Stores, Supermarkets');
INSERT INTO merchants VALUES (10, 'Merchant 10', 5411);
INSERT INTO merchant_locations VALUES (1, 10, 'Sacramento', 'CA', 95814);
INSERT INTO cards VALUES
  (100, 1, 'Visa', 'Credit', '2024-12-31', 1, 1000000, '2010-05-01', 2015, 0),
  (101, 1, 'Mastercard', 'Debit', '2023-06-30', 1, 500000, '2012-03-01', 2016, 0),
  (102, 1, 'Amex', 'Debit (Prepaid)', '2022-01-31', 0, 5000, '2018-09-01', 2018, 0);
INSERT INTO transactions VALUES
  (1, '2019-01-01 00:02:00', 100, 2500, 'Chip Transaction', 10, 1, NULL),
  (2, '2019-02-10 12:30:00', 101, 150000, 'Online Transaction', 10, 1, NULL),
  (3, '2019-03-05 08:15:00', 101, -5000, 'Swipe Transaction', 10, 1, NULL),
  (4, '2019-04-20 19:45:00', 102, 999, 'Chip Transaction', 10, 1, 'Bad PIN'),
  (5, '2019-05-11 10:00:00', 100, 4200, 'Chip Transaction', 10, 1, NULL),
  (6, '2019-06-30 23:59:00', 102, 120000, 'Online Transaction', 10, 1, NULL),
  (7, '2019-07-01 09:00:00', 101, 200000, 'Chip Transaction', 10, 1, NULL),
  (8, '2019-08-01 09:00:00', 100, 300000, 'Online Transaction', 10, 1, NULL),
  (9, '2019-09-01 09:00:00', 101, 50000, 'Swipe Transaction', 10, 1, NULL),
  (10, '2019-10-01 09:00:00', 101, 110000, 'Chip Transaction', 10, 1, NULL);
"""
LABELS = "INSERT INTO fraud_labels VALUES ('1', 0), ('2', 1), ('3', 0), ('4', 0), ('7', 0), ('8', 1), ('9', 1);"

BRIEF_RULE = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"


def build_db(path: Path, script: str) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript((DATA_DIR / "schema.sql").read_text())
    connection.executescript(script)
    connection.commit()
    connection.close()
    return path


@pytest.fixture(scope="module")
def rules_db(tmp_path_factory) -> Path:
    return build_db(tmp_path_factory.mktemp("rules") / "rules.db", ROWS + LABELS)


def run(path: Path, clause: str) -> BacktestResult:
    return backtest(clause, db_path=path, timeout_seconds=10.0)


def test_brief_style_rule_with_a_subquery_on_cards(rules_db):
    result = run(rules_db, BRIEF_RULE)  # matches 2 (fraud), 7 (legitimate), 10 (unlabelled)
    assert result.clause == BRIEF_RULE
    assert result.flagged == 3
    assert result.flagged_labelled == 2
    assert result.flagged_unlabelled == 1
    assert result.true_positives == 1
    assert result.false_positives == 1
    assert result.false_negatives == 2
    assert (result.total_transactions, result.total_labelled, result.total_fraud) == (10, 7, 3)
    assert result.precision == 1 / 2
    assert result.recall == 1 / 3
    assert result.flag_rate == 3 / 10
    assert result.flagged_label_coverage == 2 / 3
    assert result.flagged_amount_cents == 150000 + 200000 + 110000
    assert result.fraud_amount_caught_cents == 150000
    assert result.false_positive_amount_cents == 200000
    assert result.elapsed_seconds >= 0


def test_everything_flagged(rules_db):
    result = run(rules_db, "1=1")
    assert (result.flagged, result.flagged_labelled, result.flagged_unlabelled) == (10, 7, 3)
    assert (result.true_positives, result.false_positives, result.false_negatives) == (3, 4, 0)
    assert result.precision == 3 / 7  # the base fraud rate of the labelled rows
    assert result.recall == 1.0
    assert result.flag_rate == 1.0
    assert result.flagged_amount_cents == 932699
    assert result.fraud_amount_caught_cents == 150000 + 300000 + 50000
    assert result.false_positive_amount_cents == 2500 - 5000 + 999 + 200000  # net of the negative amount


def test_nothing_flagged(rules_db):
    result = run(rules_db, "amount_usd_cents > 99999999")
    assert (result.flagged, result.flagged_labelled, result.flagged_unlabelled) == (0, 0, 0)
    assert (result.true_positives, result.false_positives, result.false_negatives) == (0, 0, 3)
    assert result.precision is None  # nothing flagged: undefined, not 0
    assert result.recall == 0.0
    assert result.flag_rate == 0.0
    assert result.flagged_label_coverage is None
    assert result.flagged_amount_cents == 0
    assert result.fraud_amount_caught_cents == 0
    assert result.false_positive_amount_cents == 0
    assert (result.total_transactions, result.total_labelled, result.total_fraud) == (10, 7, 3)


def test_only_unlabelled_rows_flagged(rules_db):
    result = run(rules_db, "id IN (5, 6)")
    assert (result.flagged, result.flagged_labelled, result.flagged_unlabelled) == (2, 0, 2)
    assert (result.true_positives, result.false_positives) == (0, 0)
    assert result.precision is None  # unknown, not "0% precise"
    assert result.recall == 0.0
    assert result.flag_rate == 2 / 10
    assert result.flagged_label_coverage == 0.0
    assert result.flagged_amount_cents == 4200 + 120000
    assert result.fraud_amount_caught_cents == 0
    assert result.false_positive_amount_cents == 0


@pytest.mark.parametrize("clause", ["id = 2", "transactions.id = 2", "  id = 2\n"])
def test_id_means_the_transaction_id(rules_db, clause):
    # fraud_labels is joined in the same statement; `id` must not be
    # ambiguous or resolve against it.
    result = run(rules_db, clause)
    assert result.clause == clause.strip()
    assert (result.flagged, result.true_positives, result.false_positives) == (1, 1, 0)
    assert result.precision == 1.0
    assert result.recall == 1 / 3


@pytest.mark.parametrize(
    "clause, message",
    [
        ("is_fraud = 1", "no such column: is_fraud"),  # label columns are out of scope
        ("f.is_fraud = 1", "no such column: f.is_fraud"),  # so is the template's alias
        ("id IN (SELECT id FROM flagged)", "no such table: flagged"),  # and its CTE
        ("amount > 1000", "no such column: amount"),  # the brief's column name
        ("amount_usd_cents >", "incomplete input"),
        ("count(*) > 1", "misuse of aggregate function count()"),
    ],
)
def test_invalid_sql_keeps_sqlites_message(rules_db, clause, message):
    with pytest.raises(RuleClauseError) as raised:
        run(rules_db, clause)
    assert raised.value.sqlite_message == message
    assert message in raised.value.reason


@pytest.mark.parametrize(
    "clause, reason",
    [
        ("1=1; DROP TABLE transactions", "';'"),
        ("1=1;", "';'"),
        ("1=1 -- the rest", "comment"),
        ("amount_usd_cents > 1--1", "comment"),
        ("1=1 /* the rest", "comment"),
        ("1=1 /* note */", "comment"),
        ("1=1) OR (1=1", "')' with no matching '('"),
        ("1=0) UNION SELECT id, amount_usd_cents FROM transactions WHERE (1=1", "')' with no matching '('"),
        ("(1=1", "'(' with no matching ')'"),
        ("1=1)", "')' with no matching '('"),
        ("errors = 'Bad PIN", "unterminated"),
        ('"id = 1', "unterminated"),
        ("[id = 1", "unterminated"),
        ("id = ?", "bind parameters"),
        ("id = :id", "bind parameters"),
        ("id = $a(()) OR (1=1", "bind parameters"),
        ("", "empty"),
        ("   \n", "empty"),
        ("1=1 \x00", "NUL"),
    ],
)
def test_break_out_attempts_are_rejected_before_any_query(clause, reason, tmp_path):
    # The database does not exist: a rejection here never reached SQLite.
    with pytest.raises(RuleClauseError) as raised:
        run(tmp_path / "missing.db", clause)
    assert reason in raised.value.reason
    assert raised.value.sqlite_message is None


@pytest.mark.parametrize(
    "clause",
    [
        "1=1 LIMIT 1",
        "1=1 GROUP BY card_id",
        "1=1 ORDER BY id",
        "1=0 UNION SELECT 1",
        "1=0 UNION SELECT id, amount_usd_cents FROM transactions",
        "SELECT 1",  # valid in parentheses (a scalar subquery), not as a WHERE clause
        "1, 2",
    ],
)
def test_text_that_is_not_one_where_expression_is_rejected_by_sqlite(rules_db, clause):
    with pytest.raises(RuleClauseError) as raised:
        run(rules_db, clause)
    assert raised.value.sqlite_message


def test_punctuation_inside_quotes_is_data(rules_db):
    # ';', ')', '--' and '?' inside a literal or a quoted name are not structure.
    assert check_clause("errors = 'a;b) -- c ''?'") == "errors = 'a;b) -- c ''?'"
    result = run(rules_db, """errors = 'Bad PIN' OR errors = ';) -- /* ?' OR "id" = 2 OR [id] = 7 OR `id` = 9""")
    assert (result.flagged, result.true_positives, result.false_positives) == (4, 2, 2)


def test_ratios_are_none_without_labelled_fraud_or_transactions(tmp_path):
    no_fraud = build_db(tmp_path / "no_fraud.db", ROWS + "INSERT INTO fraud_labels VALUES ('1', 0), ('2', 0);")
    result = run(no_fraud, "id IN (1, 5)")
    assert (result.flagged, result.flagged_labelled, result.true_positives, result.false_positives) == (2, 1, 0, 1)
    assert result.total_fraud == 0
    assert result.recall is None  # no fraud to catch: undefined, not 0
    assert result.precision == 0.0

    empty = run(build_db(tmp_path / "empty.db", ""), "1=1")
    assert (empty.flagged, empty.total_transactions, empty.total_labelled, empty.total_fraud) == (0, 0, 0, 0)
    assert (empty.precision, empty.recall, empty.flag_rate, empty.flagged_label_coverage) == (None,) * 4


def test_result_is_immutable(rules_db):
    result = run(rules_db, "1=1")
    with pytest.raises(AttributeError):
        result.flagged = 0


def test_timeout_propagates_as_the_db_error(rules_db):
    endless = "(WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n) > 0"
    with pytest.raises(db.QueryTimeout):
        backtest(endless, db_path=rules_db, timeout_seconds=0.2)


def test_missing_database_propagates(tmp_path):
    with pytest.raises(db.DatabaseNotFoundError):
        run(tmp_path / "missing.db", "1=1")
