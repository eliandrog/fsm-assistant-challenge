"""Rule evidence: periods, lift, the weakest month and the stability summary, by hand."""

from pathlib import Path

import pytest
from test_backtest import build_db
from test_backtest_by_month import BIG_DEBIT, LABEL_LEAK, ROWS

from fsm_assistant import db
from fsm_assistant.rules.backtest import BacktestResult, MonthlyBacktest, RuleClauseError
from fsm_assistant.rules.proposal import (
    LATER_PERIOD_START,
    MIN_MONTH_LABELLED,
    RuleEvidence,
    build_evidence,
    lift,
    rule_evidence,
    sum_period,
    weakest_month,
)
from fsm_assistant.sql_guard import ReasonCode


def month(
    name: str,
    labelled: int = 0,
    caught: int = 0,
    *,
    flagged: int | None = None,
    transactions: int = 1000,
    total_labelled: int = 700,
    fraud: int = 10,
) -> MonthlyBacktest:
    """A month in which `caught` of the `labelled` flagged transactions are fraud."""
    return MonthlyBacktest(
        month=name,
        flagged=labelled if flagged is None else flagged,
        flagged_labelled=labelled,
        true_positives=caught,
        false_positives=labelled - caught,
        flagged_amount_cents=0,
        fraud_amount_caught_cents=0,
        false_positive_amount_cents=0,
        total_transactions=transactions,
        total_labelled=total_labelled,
        total_fraud=fraud,
    )


def overall(months: tuple[MonthlyBacktest, ...]) -> BacktestResult:
    """The overall backtest the months add up to."""

    def total(name: str) -> int:
        return sum(getattr(row, name) for row in months)

    return BacktestResult(
        clause="id > 0",
        elapsed_seconds=0.1,
        **{name: total(name) for name in MonthlyBacktest.model_fields if name != "month"},
    )


def evidence(*months: MonthlyBacktest) -> RuleEvidence:
    return build_evidence(overall(months), months)


@pytest.fixture(scope="module")
def months_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_db(tmp_path_factory.mktemp("proposal") / "months.db", ROWS)


def test_the_constants_are_the_documented_ones() -> None:
    assert (MIN_MONTH_LABELLED, LATER_PERIOD_START) == (30, "2019-08")


def test_evidence_from_the_database(months_db: Path) -> None:
    found = rule_evidence(BIG_DEBIT, db_path=months_db, timeout_seconds=10.0)
    assert found.overall.clause == BIG_DEBIT
    assert (found.overall.flagged, found.overall.flagged_labelled, found.overall.true_positives) == (5, 4, 3)
    assert [row.month for row in found.months] == ["2019-01", "2019-02", "2019-03", "2019-08", "2019-10"]

    # January to March: 2, 3 and 4 are flagged; 2 and 3 are labelled, 2 is fraud.
    earlier = found.earlier
    assert (earlier.first_month, earlier.last_month, earlier.months) == ("2019-01", "2019-03", 3)
    assert (earlier.flagged, earlier.flagged_labelled, earlier.true_positives, earlier.false_positives) == (3, 2, 1, 1)
    assert (earlier.total_transactions, earlier.total_labelled, earlier.total_fraud) == (6, 5, 2)
    assert (earlier.precision, earlier.recall, earlier.flag_rate) == (1 / 2, 1 / 2, 3 / 6)
    assert earlier.base_fraud_rate == 2 / 5 and earlier.lift == (1 / 2) / (2 / 5)

    # August and October: 7 and 10, both fraud. Its recall counts the period's fraud only.
    later = found.later
    assert (later.first_month, later.last_month, later.months) == ("2019-08", "2019-10", 2)
    assert (later.flagged, later.flagged_labelled, later.true_positives, later.false_positives) == (2, 2, 2, 0)
    assert (later.total_transactions, later.total_labelled, later.total_fraud) == (4, 4, 2)
    assert (later.precision, later.recall) == (1.0, 1.0)
    assert later.lift == 1.0 / (2 / 4)

    assert found.base_fraud_rate == 4 / 9
    assert found.lift == (3 / 4) / (4 / 9)
    # No month has thirty labelled flagged transactions.
    assert found.weakest_month is None
    stability = found.stability
    assert (stability.months, stability.months_flagged, stability.months_with_labelled_flags) == (5, 4, 3)
    assert (stability.qualifying_months, stability.min_precision, stability.max_precision) == (0, None, None)


def test_the_periods_add_up_to_the_overall_backtest(months_db: Path) -> None:
    for clause in (BIG_DEBIT, "1=1", "1=0", "amount_usd_cents < 0"):
        found = rule_evidence(clause, db_path=months_db, timeout_seconds=10.0)
        for name in ("flagged", "flagged_labelled", "true_positives", "false_positives",
                     "total_transactions", "total_labelled", "total_fraud"):  # fmt: skip
            both = getattr(found.earlier, name) + getattr(found.later, name)
            assert both == getattr(found.overall, name) == sum(getattr(row, name) for row in found.months)


def test_evidence_applies_the_rule_policy_and_passes_errors_on(months_db: Path, tmp_path: Path) -> None:
    with pytest.raises(RuleClauseError) as raised:
        rule_evidence(LABEL_LEAK, db_path=months_db, timeout_seconds=10.0)
    assert raised.value.reason_code is ReasonCode.LABEL_READ
    endless = "(WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n) > 0"
    with pytest.raises(db.QueryTimeout):
        rule_evidence(endless, db_path=months_db, timeout_seconds=0.2)
    with pytest.raises(db.DatabaseNotFoundError):
        rule_evidence("1=1", db_path=tmp_path / "missing.db", timeout_seconds=10.0)


def test_the_period_boundary_is_fixed() -> None:
    found = evidence(month("2019-07", 40, 10), month("2019-08", 40, 30), month("2019-09", 40, 20))
    assert (found.earlier.first_month, found.earlier.last_month) == ("2019-07", "2019-07")
    assert (found.later.first_month, found.later.last_month) == ("2019-08", "2019-09")
    assert (found.earlier.true_positives, found.later.true_positives) == (10, 50)
    # Each period's recall is over its own labelled fraud: 10 a month in these rows.
    assert (found.earlier.recall, found.later.recall) == (10 / 10, 50 / 20)


def test_a_period_with_no_month_is_empty_and_undefined() -> None:
    found = evidence(month("2019-02", 40, 10), month("2019-03", 40, 20))
    later = found.later
    assert (later.first_month, later.last_month, later.months, later.flagged) == (None, None, 0, 0)
    assert (later.precision, later.recall, later.flag_rate, later.base_fraud_rate, later.lift) == (None,) * 5
    assert sum_period([]) == later
    assert evidence().months == () and evidence().weakest_month is None


def test_lift_is_precision_over_the_base_fraud_rate() -> None:
    assert lift(0.5, 0.01) == 50.0
    assert lift(0.0, 0.01) == 0.0
    assert lift(None, 0.01) is None  # nothing flagged is labelled
    assert lift(0.5, None) is None  # no labelled transaction
    assert lift(0.5, 0.0) is None  # no labelled fraud: nothing to be a multiple of
    # 20 of 40 against a month where 10 of 700 labelled transactions are fraud.
    found = evidence(month("2019-01", 40, 20))
    assert found.lift == (20 / 40) / (10 / 700) == found.earlier.lift
    # Flagged, none labelled: precision, and so lift, is undefined.
    assert evidence(month("2019-01", 0, 0, flagged=5)).lift is None
    assert evidence(month("2019-01", 40, 0, fraud=0)).lift is None


def test_the_weakest_month_needs_thirty_labelled_flagged_rows() -> None:
    lowest_of_all = month("2019-02", MIN_MONTH_LABELLED - 1, 0)  # 0 of 29: too few to compare
    months = (month("2019-01", 40, 30), lowest_of_all, month("2019-03", MIN_MONTH_LABELLED, 15), month("2019-04", 50, 40))
    weakest = weakest_month(months)
    assert weakest is not None and weakest.month == "2019-03"
    assert evidence(*months).weakest_month == weakest
    assert weakest_month([lowest_of_all, month("2019-05", 0, 0, flagged=9)]) is None
    assert weakest_month([]) is None


def test_weakest_month_ties_go_to_the_month_with_more_rows_then_the_earlier() -> None:
    more_rows = weakest_month([month("2019-08", 33, 0), month("2019-09", 35, 0), month("2019-10", 60, 30)])
    assert more_rows is not None and more_rows.month == "2019-09"
    earlier = weakest_month([month("2019-05", 40, 10), month("2019-02", 40, 10)])
    assert earlier is not None and earlier.month == "2019-02"


def test_the_stability_summary_counts_months_and_spans_the_qualifying_ones() -> None:
    found = evidence(
        month("2019-01", 40, 30),  # 75%
        month("2019-02", 29, 0),  # labelled flagged rows, too few to qualify
        month("2019-03", 0, 0, flagged=4),  # flagged, none labelled
        month("2019-04"),  # nothing flagged
        month("2019-08", 50, 10),  # 20%
    )
    stability = found.stability
    assert (stability.months, stability.months_flagged) == (5, 4)
    assert (stability.months_with_labelled_flags, stability.qualifying_months) == (3, 2)
    assert (stability.min_precision, stability.max_precision) == (10 / 50, 30 / 40)
    assert found.weakest_month is not None and found.weakest_month.month == "2019-08"


def test_months_are_kept_in_order_whatever_order_they_arrive_in() -> None:
    rows = (month("2019-10", 40, 1), month("2019-01", 40, 2), month("2019-08", 40, 3))
    assert [row.month for row in build_evidence(overall(rows), rows).months] == ["2019-01", "2019-08", "2019-10"]
