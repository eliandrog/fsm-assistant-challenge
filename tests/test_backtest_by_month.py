"""The by-month backtest on a small database where every month can be worked out by hand."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from test_backtest import build_db

from fsm_assistant import db
from fsm_assistant.rules import backtest as backtest_module
from fsm_assistant.rules.backtest import (
    BacktestByMonth,
    MonthlyBacktest,
    RuleClauseError,
    backtest,
    backtest_by_month,
)
from fsm_assistant.sql_guard import ReasonCode

# Ten transactions in five months; nine labelled, four of them fraud.
#
#   id  date              card          amount   label
#    1  2019-01-05        100 Credit      1000   legitimate
#    2  2019-01-20        101 Debit     150000   fraud
#    3  2019-01-31 23:59  101 Debit     200000   legitimate
#    4  2019-02-01 00:00  101 Debit     120000   (none)
#    5  2019-02-10        100 Credit      5000   fraud
#    6  2019-03-03        100 Credit      2000   legitimate
#    7  2019-08-01        101 Debit     300000   fraud
#    8  2019-08-15        102 Prepaid   110000   legitimate
#    9  2019-08-20        101 Debit      -5000   legitimate
#   10  2019-10-31 23:59  101 Debit     250000   fraud
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
  (1, '2019-01-05 10:00:00', 100, 1000, 'Chip Transaction', 10, 1, NULL),
  (2, '2019-01-20 10:00:00', 101, 150000, 'Chip Transaction', 10, 1, NULL),
  (3, '2019-01-31 23:59:00', 101, 200000, 'Chip Transaction', 10, 1, NULL),
  (4, '2019-02-01 00:00:00', 101, 120000, 'Chip Transaction', 10, 1, NULL),
  (5, '2019-02-10 10:00:00', 100, 5000, 'Chip Transaction', 10, 1, NULL),
  (6, '2019-03-03 10:00:00', 100, 2000, 'Chip Transaction', 10, 1, NULL),
  (7, '2019-08-01 10:00:00', 101, 300000, 'Chip Transaction', 10, 1, NULL),
  (8, '2019-08-15 10:00:00', 102, 110000, 'Chip Transaction', 10, 1, NULL),
  (9, '2019-08-20 10:00:00', 101, -5000, 'Chip Transaction', 10, 1, NULL),
  (10, '2019-10-31 23:59:00', 101, 250000, 'Chip Transaction', 10, 1, NULL);
INSERT INTO fraud_labels VALUES
  ('1', 0), ('2', 1), ('3', 0), ('5', 1), ('6', 0), ('7', 1), ('8', 0), ('9', 0), ('10', 1);
"""
# Flags 2, 3, 4, 7 and 10: the subquery reaches `cards`.
BIG_DEBIT = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"
LABEL_LEAK = "id IN (SELECT CAST(transaction_id AS INTEGER) FROM fraud_labels WHERE is_fraud = 1)"
COUNTS = ("flagged", "flagged_labelled", "true_positives", "false_positives")
TOTALS = ("total_transactions", "total_labelled", "total_fraud")
AMOUNTS = ("flagged_amount_cents", "fraud_amount_caught_cents", "false_positive_amount_cents")


@pytest.fixture(scope="module")
def months_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_db(tmp_path_factory.mktemp("months") / "months.db", ROWS)


def run(path: Path, clause: str) -> BacktestByMonth:
    return backtest_by_month(clause, db_path=path, timeout_seconds=10.0)


def table(result: BacktestByMonth, names: tuple[str, ...]) -> dict[str, tuple[int, ...]]:
    return {row.month: tuple(getattr(row, name) for name in names) for row in result.months}


def test_each_month_is_measured_with_its_own_totals(months_db: Path) -> None:
    result = run(months_db, BIG_DEBIT)
    assert result.clause == BIG_DEBIT and result.elapsed_seconds >= 0
    # Months as YYYY-MM, in order; a month with no transaction is not a row.
    assert [row.month for row in result.months] == ["2019-01", "2019-02", "2019-03", "2019-08", "2019-10"]
    assert table(result, COUNTS) == {
        "2019-01": (2, 2, 1, 1),  # 2 (fraud) and 3 (legitimate), up to the last minute of the 31st
        "2019-02": (1, 0, 0, 0),  # 4 is flagged and has no label
        "2019-03": (0, 0, 0, 0),  # nothing flagged, still a row
        "2019-08": (1, 1, 1, 0),  # 7; the prepaid card and the negative amount are not flagged
        "2019-10": (1, 1, 1, 0),
    }
    assert table(result, TOTALS) == {
        "2019-01": (3, 3, 1),
        "2019-02": (2, 1, 1),  # the month's fraud, 5, is not flagged
        "2019-03": (1, 1, 0),
        "2019-08": (3, 3, 1),
        "2019-10": (1, 1, 1),
    }
    assert table(result, AMOUNTS) == {
        "2019-01": (350000, 150000, 200000),
        "2019-02": (120000, 0, 0),
        "2019-03": (0, 0, 0),
        "2019-08": (300000, 300000, 0),
        "2019-10": (250000, 250000, 0),
    }


def test_a_months_ratios_use_its_own_denominators(months_db: Path) -> None:
    january, february, march, august, _ = run(months_db, BIG_DEBIT).months
    assert (january.precision, january.recall, january.flag_rate) == (1 / 2, 1.0, 2 / 3)
    assert january.flagged_unlabelled == 0
    # Flagged, none of it labelled: precision is unknown, and the month's fraud was missed.
    assert (february.precision, february.recall, february.flag_rate) == (None, 0.0, 1 / 2)
    assert february.flagged_unlabelled == 1
    # Nothing flagged in a month with no fraud: neither ratio is defined.
    assert (march.precision, march.recall, march.flag_rate) == (None, None, 0.0)
    assert (august.precision, august.recall, august.flag_rate) == (1.0, 1.0, 1 / 3)


@pytest.mark.parametrize(
    "clause",
    [BIG_DEBIT, "1=1", "1=0", "amount_usd_cents < 0", "id IN (4, 6)", "date >= '2019-08-01'"],
)
def test_the_months_add_up_to_the_overall_backtest(months_db: Path, clause: str) -> None:
    overall = backtest(clause, db_path=months_db, timeout_seconds=10.0)
    months = run(months_db, clause).months
    for name in (*COUNTS, *TOTALS, *AMOUNTS):
        assert sum(getattr(row, name) for row in months) == getattr(overall, name), name


def test_everything_and_nothing_flagged(months_db: Path) -> None:
    everything = run(months_db, "1=1")
    assert table(everything, COUNTS) == {
        "2019-01": (3, 3, 1, 2),
        "2019-02": (2, 1, 1, 0),
        "2019-03": (1, 1, 0, 1),
        "2019-08": (3, 3, 1, 2),
        "2019-10": (1, 1, 1, 0),
    }
    assert everything.months[3].false_positive_amount_cents == 110000 - 5000  # net of the negative amount
    nothing = run(months_db, "amount_usd_cents > 99999999")
    assert len(nothing.months) == 5
    assert all(row.flagged == 0 and row.precision is None for row in nothing.months)
    assert table(nothing, TOTALS) == table(everything, TOTALS)


def test_the_clause_sees_only_transactions(months_db: Path) -> None:
    # The statement's own aliases and CTE are out of the clause's scope, as in the overall template.
    for clause, message in (
        ("is_fraud = 1", "no such column: is_fraud"),
        ("f.is_fraud = 1", "no such column: f.is_fraud"),
        ("t.id = 2", "no such column: t.id"),
        ("id IN (SELECT id FROM flagged)", "no such table: flagged"),
    ):
        with pytest.raises(RuleClauseError) as raised:
            run(months_db, clause)
        assert raised.value.sqlite_message == message
        assert raised.value.reason_code is ReasonCode.SQLITE_ERROR
    assert table(run(months_db, "transactions.id = 2"), COUNTS)["2019-01"] == (1, 1, 1, 0)
    assert run(months_db, " id = 2 ; ").clause == "id = 2"


@pytest.mark.parametrize(
    ("clause", "code"),
    [
        (LABEL_LEAK, ReasonCode.LABEL_READ),
        ("EXISTS (SELECT 1 FROM fraud_labels)", ReasonCode.LABEL_READ),
        ("abs(random()) % 2 = 0", ReasonCode.NOT_DETERMINISTIC),
        ("1=1) GROUP BY id HAVING (1=1", ReasonCode.CLAUSE_NOT_CONTAINED),
        ("date >= date('now', '-7 day')", ReasonCode.RELATIVE_TO_TODAY),
    ],
)
def test_the_rule_policy_is_applied_as_in_the_overall_backtest(
    months_db: Path, clause: str, code: ReasonCode
) -> None:
    with pytest.raises(RuleClauseError) as by_month:
        run(months_db, clause)
    with pytest.raises(RuleClauseError) as overall:
        backtest(clause, db_path=months_db, timeout_seconds=10.0)
    assert by_month.value.reason_code is code
    assert by_month.value.reason == overall.value.reason


def test_what_the_policy_prevents_month_by_month(months_db: Path) -> None:
    # The statement alone, with no policy: a clause that reads the labels is perfect in every month.
    leaked = db.run_query(
        backtest_module._MONTHLY_TEMPLATE.format(clause=LABEL_LEAK),
        db_path=months_db,
        row_cap=100,
        timeout_seconds=10.0,
    )
    caught, wrong, fraud = (leaked.columns.index(name) for name in ("true_positives", "false_positives", "total_fraud"))
    assert all(row[caught] == row[fraud] and row[wrong] == 0 for row in leaked.rows)


def test_timeout_and_a_missing_database_propagate(months_db: Path, tmp_path: Path) -> None:
    endless = "(WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n) > 0"
    with pytest.raises(db.QueryTimeout):
        backtest_by_month(endless, db_path=months_db, timeout_seconds=0.2)
    with pytest.raises(db.DatabaseNotFoundError):
        run(tmp_path / "missing.db", "1=1")


def test_an_empty_database_has_no_months(tmp_path: Path) -> None:
    assert run(build_db(tmp_path / "empty.db", ""), "1=1").months == ()


def test_the_statement_is_one_pass_with_the_shared_label_join() -> None:
    template = backtest_module._MONTHLY_TEMPLATE
    assert "SELECT id FROM transactions WHERE (\n{clause}\n  )" in template
    assert "LEFT JOIN fraud_labels f ON f.transaction_id = CAST(t.id AS TEXT)\n" in template
    assert template.count("FROM transactions") == 2  # the flagged ids, and the one pass over all rows


def test_a_month_holds_the_measured_numbers_and_is_immutable(months_db: Path) -> None:
    january = run(months_db, BIG_DEBIT).months[0]
    dumped = january.model_dump()
    assert set(dumped) == set(MonthlyBacktest.model_fields) and "precision" not in dumped
    assert MonthlyBacktest.model_validate(dumped) == january
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MonthlyBacktest.model_validate({**dumped, "precision": 1.0})
    with pytest.raises(ValidationError):
        january.flagged = 0  # type: ignore[misc]  # assigning to a frozen field is what is tested
