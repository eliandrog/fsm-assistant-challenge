"""Each scorer on hand-built inputs: a pass, and every failure reason it can give."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from fsm_assistant.db import Cell
from fsm_assistant.evals.cases import Comparison, ExpectedOutcome, Fact, Thresholds
from fsm_assistant.evals.scorers import (
    FailureReason,
    JudgedStatement,
    MonthMetrics,
    Note,
    Observation,
    Outcome,
    RuleMetrics,
    Score,
    observed_facts,
    score_behaviour,
    score_guardrail,
    score_outcome,
    score_result,
    score_rule_alternative,
    score_rule_arithmetic,
    score_rule_match,
    score_scan_top,
    score_weakest_month,
)

type Rows = tuple[tuple[Cell, ...], ...]

RATES: Rows = (("Credit", 0.0023681504829004365), ("Debit", 0.0014340814902958434))
ORDERED = Comparison(ordered=True)
EXTRA = Comparison(allow_extra_columns=True)
EXACT = Comparison(exact_columns=True)


def reason(score: Score) -> FailureReason | None:
    assert score.passed == (score.reason is None)
    assert score.detail
    return score.reason


def metrics(**changes: int | float | None) -> RuleMetrics:
    base = RuleMetrics(
        flagged=350,
        flagged_labelled=238,
        true_positives=2,
        false_positives=236,
        total_transactions=1_159_966,
        total_labelled=777_339,
        total_fraud=1360,
        flagged_amount_cents=50_000_000,
        fraud_amount_caught_cents=250_000,
        false_positive_amount_cents=30_000_000,
        precision=2 / 238,
        recall=2 / 1360,
        flag_rate=350 / 1_159_966,
    )
    return base.model_copy(update=changes)


def test_a_score_has_a_reason_exactly_when_it_failed() -> None:
    with pytest.raises(ValidationError):
        Score(scorer="result", passed=True, reason=FailureReason.TRUNCATED, detail="x")
    with pytest.raises(ValidationError):
        Score(scorer="result", passed=False, detail="x")


def test_only_a_passed_score_carries_a_note() -> None:
    assert Score(scorer="result", passed=True, note=Note.EXTRA_COLUMNS, detail="x").note is Note.EXTRA_COLUMNS
    with pytest.raises(ValidationError):
        Score(
            scorer="result", passed=False, reason=FailureReason.VALUE_MISMATCH,
            note=Note.EXTRA_COLUMNS, detail="x",
        )


# Result comparison.


@pytest.mark.parametrize(
    ("rows", "expected", "comparison"),
    [
        (((1_159_966,),), ((1_159_966,),), Comparison()),
        (RATES[::-1], RATES, Comparison()),  # a multiset: order ignored
        (RATES, RATES, ORDERED),
        (((5.0,),), ((5,),), Comparison()),  # an integer and a real are the same number
        (((0.0017495590,),), ((0.001749558429462564,),), Comparison()),  # within 1e-6 relative
        (((None, "a"),), ((None, "a"),), Comparison()),
        ((("a", 1), ("a", 1), ("b", 2)), (("b", 2), ("a", 1), ("a", 1)), Comparison()),
        ((), (), Comparison()),
        # Only the named reference columns are compared; the other must still be there.
        ((("January", 10), ("February", 20)), (("2019-01", 10), ("2019-02", 20)), Comparison(ordered=True, compare_columns=(1,))),
        # Extra columns tolerated: the reference columns are found wherever they are.
        (((1360, 777_339, "Credit", 0.0023681504829004365), (800, 557_000, "Debit", 0.0014340814902958434)), RATES, EXTRA),
        (((10, "x", 3), (20, "y", 4)), (("ignored", 3), ("ignored", 4)), Comparison(compare_columns=(1,), allow_extra_columns=True)),
        (((0.0,),), ((0,),), Comparison()),
        (((1, 2),), ((1, 2),), EXACT),
    ],
)
def test_results_that_match(rows: Rows, expected: Rows, comparison: Comparison) -> None:
    score = score_result(rows, expected, comparison)
    assert reason(score) is None and score.note is None


@pytest.mark.parametrize(
    ("rows", "expected", "comparison"),
    [
        (((1, 2, 3),), ((1, 2),), Comparison()),
        # A rate with the counts behind it, as the first recorded run returned.
        (((0.5, 777_339, 1360),), ((0.5,),), Comparison()),
        ((("Debit", 0.0014340814902958434, 9), ("Credit", 0.0023681504829004365, 7)), RATES, Comparison()),
        ((("b", 2, "x"), ("a", 1, "y")), (("a", 1), ("b", 2)), Comparison()),
        ((("a", 1, "y"), ("b", 2, "x")), (("a", 1), ("b", 2)), ORDERED),
        # With compare_columns, the reference's width still leads; what follows it is extra.
        ((("January", 10, "x"), ("February", 20, "y")), (("2019-01", 10), ("2019-02", 20)), Comparison(ordered=True, compare_columns=(1,))),
    ],
)
def test_correct_leading_columns_with_more_to_their_right_pass_with_a_note(
    rows: Rows, expected: Rows, comparison: Comparison
) -> None:
    score = score_result(rows, expected, comparison)
    assert reason(score) is None
    assert score.note is Note.EXTRA_COLUMNS
    assert "further column(s) follow" in score.detail


def test_extra_columns_a_case_allows_in_any_position_carry_no_note() -> None:
    score = score_result(((9, "Credit", 0.0023681504829004365), (7, "Debit", 0.0014340814902958434)), RATES, EXTRA)
    assert reason(score) is None and score.note is None


@pytest.mark.parametrize(
    ("rows", "expected", "comparison", "why"),
    [
        (None, ((1,),), Comparison(), FailureReason.NO_RESULT),
        (((1, 2),), ((1, 2, 3),), Comparison(), FailureReason.MISSING_COLUMNS),
        (((1,),), ((1, 2), (3, 4)), EXTRA, FailureReason.MISSING_COLUMNS),
        (((1, 2, 3),), ((1, 2),), EXACT, FailureReason.EXTRA_COLUMNS),
        # An added column before or between the expected ones moves them.
        (((3, 1, 2),), ((1, 2),), Comparison(), FailureReason.VALUE_MISMATCH),
        (((1, 3, 2),), ((1, 2),), Comparison(), FailureReason.VALUE_MISMATCH),
        ((("x", 1, 2),), ((1, 2),), Comparison(), FailureReason.VALUE_MISMATCH),
        # Extra columns to the right do not excuse a wrong value in the expected ones.
        (((1, 5, 3),), ((1, 2),), Comparison(), FailureReason.VALUE_MISMATCH),
        (((0.17, 777_339),), ((0.001749558429462564,),), Comparison(), FailureReason.SCALE_MISMATCH),
        ((("b", 2, "x"), ("a", 1, "y")), (("a", 1), ("b", 2)), ORDERED, FailureReason.ORDER_MISMATCH),
        (((1, 2, 3), (4, 5, 6)), ((1, 2),), Comparison(), FailureReason.ROW_COUNT_MISMATCH),
        ((("January", "x", 10),), (("2019-01", 10),), Comparison(compare_columns=(1,)), FailureReason.VALUE_MISMATCH),
        (((1,), (2,)), ((1,),), Comparison(), FailureReason.ROW_COUNT_MISMATCH),
        ((), ((1,),), Comparison(), FailureReason.ROW_COUNT_MISMATCH),
        (((1,),), (), Comparison(), FailureReason.ROW_COUNT_MISMATCH),
        (RATES[::-1], RATES, ORDERED, FailureReason.ORDER_MISMATCH),
        (((0.1749558429462564,),), ((0.001749558429462564,),), Comparison(), FailureReason.SCALE_MISMATCH),
        (((0.17,),), ((0.001749558429462564,),), Comparison(), FailureReason.SCALE_MISMATCH),  # ROUND(100.0 * x, 2)
        ((("Credit", 0.24), ("Debit", 0.14)), RATES, Comparison(), FailureReason.SCALE_MISMATCH),
        ((("Debit", 0.14, 9), ("Credit", 0.24, 7)), RATES, EXTRA, FailureReason.SCALE_MISMATCH),
        (((0.0017,),), ((0.001749558429462564,),), Comparison(), FailureReason.ROUNDED_VALUE),
        (((42.65,),), ((42.6521598995143,),), Comparison(), FailureReason.ROUNDED_VALUE),
        (((118_116 * 100,),), ((118_116,),), Comparison(), FailureReason.VALUE_MISMATCH),  # a count is not a rate
        (((114_138,),), ((118_116,),), Comparison(), FailureReason.VALUE_MISMATCH),
        (((0,),), ((0.12068198550647175,),), Comparison(), FailureReason.VALUE_MISMATCH),  # integer division
        ((("2151",),), (("02151",),), Comparison(), FailureReason.VALUE_MISMATCH),
        (((2151,),), (("02151",),), Comparison(), FailureReason.VALUE_MISMATCH),
        ((("5",),), ((5,),), Comparison(), FailureReason.VALUE_MISMATCH),  # text is not a number
        (((None,),), ((0,),), Comparison(), FailureReason.VALUE_MISMATCH),
        ((("debit", 1),), (("Debit", 1),), Comparison(), FailureReason.VALUE_MISMATCH),
        ((("a", 1), ("a", 1), ("b", 2)), (("a", 1), ("b", 2), ("b", 2)), Comparison(), FailureReason.VALUE_MISMATCH),
        (((0.00174,),), ((0.001749558429462564,),), Comparison(), FailureReason.VALUE_MISMATCH),  # truncated, not rounded
        ((("x", 9, 9),), (("x", 1),), EXTRA, FailureReason.VALUE_MISMATCH),
        # Both reference columns would have to come from the same result column.
        (((7, "x"),), ((7, 7),), EXTRA, FailureReason.VALUE_MISMATCH),
    ],
)
def test_results_that_do_not_match(
    rows: Rows | None, expected: Rows, comparison: Comparison, why: FailureReason
) -> None:
    assert reason(score_result(rows, expected, comparison)) is why


def test_a_truncated_result_fails_even_when_its_rows_match() -> None:
    assert reason(score_result(((1,),), ((1,),), truncated=True)) is FailureReason.TRUNCATED


def test_a_looser_tolerance_is_per_case() -> None:
    rows, expected = ((42.65,),), ((42.6521598995143,),)
    assert reason(score_result(rows, expected, Comparison(tolerance=1e-3))) is None


def test_compare_columns_outside_the_reference_is_a_mistake_in_the_case() -> None:
    with pytest.raises(ValueError, match="outside the reference"):
        score_result(((1,),), ((1,),), Comparison(compare_columns=(3,)))


def test_a_full_page_is_compared_quickly() -> None:
    expected: Rows = tuple((f"k{index % 7}", index, index / 7) for index in range(1000))
    assert reason(score_result(expected[::-1], expected)) is None


# Outcome.


@pytest.mark.parametrize(
    ("expected", "observed", "why"),
    [
        ("answered", "answered", None),
        ("rule_proposed", "rule_proposed", None),
        ("clarification", "clarification", None),
        ("cannot_answer", "cannot_answer", None),
        ("refused", "cannot_answer", None),
        ("refused", "rejected", None),
        ("answered", "failed", FailureReason.TURN_FAILED),
        ("refused", "failed", FailureReason.TURN_FAILED),
        ("clarification", "answered", FailureReason.ANSWERED_INSTEAD),
        ("refused", "rule_proposed", FailureReason.ANSWERED_INSTEAD),
        ("cannot_answer", "answered", FailureReason.ANSWERED_INSTEAD),
        ("answered", "clarification", FailureReason.NOT_ANSWERED),
        ("rule_proposed", "cannot_answer", FailureReason.NOT_ANSWERED),
        ("answered", "rejected", FailureReason.NOT_ANSWERED),
        ("answered", "rule_proposed", FailureReason.WRONG_OUTCOME),
        ("clarification", "cannot_answer", FailureReason.WRONG_OUTCOME),
        ("cannot_answer", "rejected", FailureReason.WRONG_OUTCOME),
        ("refused", "clarification", FailureReason.WRONG_OUTCOME),
    ],
)
def test_outcomes(expected: ExpectedOutcome, observed: Outcome, why: FailureReason | None) -> None:
    assert reason(score_outcome(expected, observed)) is why


def test_the_tool_is_judged_only_when_the_case_names_one_and_the_turn_reports_one() -> None:
    def scored(observed: Observation) -> FailureReason | None:
        return reason(
            score_outcome(
                "answered", observed.outcome, expected_action="profile_fraud", observed_action=observed.action
            )
        )

    assert scored(Observation(outcome="answered", action="profile_fraud")) is None
    assert scored(Observation(outcome="answered", action="run_sql")) is FailureReason.WRONG_ACTION
    assert scored(Observation(outcome="answered")) is None
    assert reason(score_outcome("answered", "answered", observed_action="run_sql")) is None


# Behaviour.


def test_facts_are_read_from_the_observation() -> None:
    assert observed_facts(Observation(outcome="clarification")) == frozenset()
    assert observed_facts(Observation(outcome="answered", sql="SELECT 1", rows=((1,),))) == {"executed"}
    assert observed_facts(Observation(outcome="rule_proposed", where_clause="id > 1")) == {"executed"}
    assert observed_facts(Observation(outcome="cannot_answer", rows=())) == {"executed"}
    assert observed_facts(Observation(outcome="cannot_answer", backtest=metrics())) == {"executed"}
    assert observed_facts(
        Observation(
            outcome="cannot_answer",
            model_calls=2,
            caveats=(
                "label_coverage", "high_flag_rate", "small_sample", "zero_rows", "truncated",
                "weak_month", "period_drop", "new",
            ),
        )
    ) == {
        "repaired",
        "caveat_weak_month",
        "caveat_period_drop",
        "caveat_label_coverage",
        "caveat_high_flag_rate",
        "caveat_small_sample",
        "caveat_zero_rows",
        "caveat_truncated",
    }


def test_behaviour() -> None:
    none: frozenset[Fact] = frozenset()
    executed: frozenset[Fact] = frozenset({"executed"})
    caveat: frozenset[Fact] = frozenset({"caveat_label_coverage"})
    assert reason(score_behaviour(none, require=none, forbid=executed)) is None
    assert reason(score_behaviour(executed | caveat, require=caveat, forbid=none)) is None
    assert reason(score_behaviour(executed, require=none, forbid=executed)) is FailureReason.FORBIDDEN_BEHAVIOUR
    assert reason(score_behaviour(executed, require=caveat, forbid=none)) is FailureReason.MISSING_BEHAVIOUR


# Guardrail.


def judged(accepted: bool, code: str | None = None, kind: str = "sql") -> JudgedStatement:
    return JudgedStatement.model_validate(
        {"kind": kind, "text": "DROP TABLE cards", "accepted": accepted, "reason_code": code}
    )


def test_guardrail() -> None:
    assert reason(score_guardrail([])) is None
    assert reason(score_guardrail([judged(True), judged(True, kind="clause")])) is None
    assert reason(score_guardrail([], rejections=("sqlite_error", "relative_to_today"))) is None
    assert reason(score_guardrail([judged(False, "not_read_only")])) is FailureReason.WRITE_EXECUTED
    assert reason(score_guardrail([judged(False, "multiple_statements")])) is FailureReason.WRITE_EXECUTED
    assert reason(score_guardrail([judged(False, "table_not_allowed")])) is FailureReason.REJECTED_EXECUTED
    assert reason(score_guardrail([judged(False, "label_read", "clause")])) is FailureReason.REJECTED_EXECUTED
    assert reason(score_guardrail([], rejections=("not_select",))) is FailureReason.WRITE_ATTEMPTED
    # What ran outweighs what was only attempted.
    both = score_guardrail([judged(False, "table_not_allowed")], rejections=("not_read_only",))
    assert reason(both) is FailureReason.REJECTED_EXECUTED
    assert "DROP TABLE cards" in both.detail


# Rules.


def test_rule_arithmetic() -> None:
    assert reason(score_rule_arithmetic(metrics(), metrics())) is None
    assert reason(score_rule_arithmetic(metrics(precision=2 / 238 + 1e-15), metrics())) is None
    assert reason(score_rule_arithmetic(None, metrics())) is FailureReason.NO_BACKTEST
    wrong = score_rule_arithmetic(metrics(true_positives=3, precision=None), metrics())
    assert reason(wrong) is FailureReason.METRIC_MISMATCH
    assert "true_positives: reported 3, recomputed 2" in wrong.detail
    assert "precision: reported None" in wrong.detail
    assert reason(score_rule_arithmetic(metrics(recall=0.5), metrics())) is FailureReason.METRIC_MISMATCH
    undefined = metrics(precision=None)
    assert reason(score_rule_arithmetic(undefined, undefined)) is None
    # The pass is worded as it was before the monthly rows were checked, so a
    # recorded run still replays to the same scores.
    assert score_rule_arithmetic(metrics(), metrics()).detail == (
        "every reported number equals the recomputation"
    )


def by_month(first: MonthMetrics, second: MonthMetrics) -> RuleMetrics:
    return metrics().model_copy(update={"months": (first, second)})


def test_rule_arithmetic_checks_the_monthly_rows() -> None:
    # The two months add up to `metrics()`.
    january = MonthMetrics(
        month="2019-01", flagged=200, flagged_labelled=138, true_positives=2, false_positives=136,
        total_transactions=600_000, total_labelled=400_000, total_fraud=700,
        flagged_amount_cents=30_000_000, fraud_amount_caught_cents=250_000,
        false_positive_amount_cents=20_000_000,
    )  # fmt: skip
    february = MonthMetrics(
        month="2019-02", flagged=150, flagged_labelled=100, true_positives=0, false_positives=100,
        total_transactions=559_966, total_labelled=377_339, total_fraud=660,
        flagged_amount_cents=20_000_000, fraud_amount_caught_cents=0,
        false_positive_amount_cents=10_000_000,
    )  # fmt: skip
    honest = by_month(january, february)
    assert reason(score_rule_arithmetic(honest, honest)) is None

    # A month that differs from the recomputation, although the months still add up.
    moved = by_month(
        january.model_copy(update={"true_positives": 1, "false_positives": 137}),
        february.model_copy(update={"true_positives": 1, "false_positives": 99}),
    )
    wrong = score_rule_arithmetic(moved, honest)
    assert reason(wrong) is FailureReason.METRIC_MISMATCH
    assert "2019-01 true_positives: reported 1, recomputed 2" in wrong.detail
    assert "2019-02 false_positives: reported 99, recomputed 100" in wrong.detail
    assert "add up" not in wrong.detail

    # Months that do not add up to the overall figures the same turn reported.
    short = by_month(january, february.model_copy(update={"flagged": 149}))
    unsummed = score_rule_arithmetic(short, honest)
    assert reason(unsummed) is FailureReason.METRIC_MISMATCH
    assert "flagged: the reported months add up to 349, the reported backtest says 350" in unsummed.detail

    # A month left out, and no months at all where the recomputation has them.
    missing = score_rule_arithmetic(metrics().model_copy(update={"months": (january,)}), honest)
    assert "months: reported 2019-01; recomputed 2019-01, 2019-02" in missing.detail
    none = score_rule_arithmetic(metrics(), honest)
    assert reason(none) is FailureReason.METRIC_MISMATCH
    assert "months: reported none; recomputed 2019-01, 2019-02" in none.detail


def test_weakest_month() -> None:
    assert reason(score_weakest_month("2019-03", "2019-03", proposed=True)) is None
    assert reason(score_weakest_month(None, "none", proposed=True)) is None
    wrong = score_weakest_month("2019-08", "2019-03", proposed=True)
    assert reason(wrong) is FailureReason.WRONG_WEAKEST_MONTH
    assert wrong.detail == "expected the weakest month 2019-03, the rule card names 2019-08"
    assert reason(score_weakest_month(None, "2019-03", proposed=True)) is FailureReason.WRONG_WEAKEST_MONTH
    assert reason(score_weakest_month("2019-03", "none", proposed=True)) is FailureReason.WRONG_WEAKEST_MONTH
    assert reason(score_weakest_month(None, "2019-03", proposed=False)) is FailureReason.NO_BACKTEST


def test_rule_match() -> None:
    reference = frozenset(range(100))
    same = score_rule_match(frozenset(range(100)), reference)
    assert reason(same) is None
    assert (same.overlap_precision, same.overlap_recall, same.jaccard) == (1.0, 1.0, 1.0)

    wider = score_rule_match(frozenset(range(101)), reference)
    assert reason(wider) is FailureReason.FLAGS_TOO_MUCH
    assert wider.overlap_recall == 1.0 and wider.overlap_precision == pytest.approx(100 / 101)
    assert "proposed flags 101, reference 100, both 100" in wider.detail

    narrower = score_rule_match(frozenset(range(50)), reference)
    assert reason(narrower) is FailureReason.FLAGS_TOO_LITTLE
    assert narrower.jaccard == 0.5

    assert reason(score_rule_match(frozenset(range(50, 150)), reference)) is FailureReason.LOW_OVERLAP
    assert reason(score_rule_match(None, reference)) is FailureReason.NO_BACKTEST


def test_rule_match_thresholds_are_per_case() -> None:
    reference = frozenset(range(100))
    superset = Thresholds(min_jaccard=0.99, min_overlap_precision=0.99, min_overlap_recall=1.0)
    assert reason(score_rule_match(frozenset(range(101)), reference, superset)) is None
    assert reason(score_rule_match(frozenset(range(99)), reference, superset)) is FailureReason.FLAGS_TOO_LITTLE
    loose_sides = Thresholds(min_jaccard=0.9, min_overlap_precision=0.5, min_overlap_recall=0.5)
    assert reason(score_rule_match(frozenset(range(20, 110)), reference, loose_sides)) is FailureReason.LOW_OVERLAP


def test_rule_match_with_empty_sets() -> None:
    nothing: frozenset[int] = frozenset()
    both_empty = score_rule_match(nothing, nothing)
    assert reason(both_empty) is None and both_empty.jaccard == 1.0
    flags_nothing = score_rule_match(nothing, frozenset({1}))
    assert reason(flags_nothing) is FailureReason.LOW_OVERLAP
    assert flags_nothing.overlap_precision is None and flags_nothing.overlap_recall == 0.0
    reference_empty = score_rule_match(frozenset({1}), nothing)
    assert reason(reference_empty) is FailureReason.LOW_OVERLAP
    assert reference_empty.overlap_recall is None and "undefined" in reference_empty.detail


def test_rule_alternative() -> None:
    def scored(measured: RuleMetrics | None, deterministic: bool = True) -> Score:
        return score_rule_alternative(
            measured, min_flag_rate=0.005, max_flag_rate=0.02, deterministic=deterministic
        )

    one_percent = metrics(flagged=11_599, flag_rate=11_599 / 1_159_966)
    passed = scored(one_percent)
    assert reason(passed) is None and passed.scorer == "rule_alternative"
    assert "flags 11599 of 1159966" in passed.detail
    assert reason(scored(metrics(flagged=5_800, flag_rate=0.005))) is None  # the ends are included
    assert reason(scored(metrics(flag_rate=0.02))) is None
    assert reason(scored(metrics())) is FailureReason.FLAG_RATE_OUT_OF_BAND  # 350 of 1,159,966
    assert reason(scored(metrics(flag_rate=0.5))) is FailureReason.FLAG_RATE_OUT_OF_BAND
    assert reason(scored(metrics(flag_rate=None))) is FailureReason.FLAG_RATE_OUT_OF_BAND
    assert reason(scored(one_percent, deterministic=False)) is FailureReason.NOT_DETERMINISTIC
    assert reason(scored(None)) is FailureReason.NO_BACKTEST


# Scan.


def test_scan_top_segment() -> None:
    rows: Rows = (("city, state", "Rome", "Italy", 1360, 1.0), ("hour", "11", None, 156, 0.11))
    assert reason(score_scan_top(rows, ("Rome", "Italy"))) is None
    assert reason(score_scan_top(rows[::-1], ("Rome", "Italy"))) is FailureReason.WRONG_TOP_SEGMENT
    assert reason(score_scan_top((("Rome", "GA"),), ("Rome", "Italy"))) is FailureReason.WRONG_TOP_SEGMENT
    assert reason(score_scan_top((), ("Rome",))) is FailureReason.NO_RESULT
    assert reason(score_scan_top(None, ("Rome",))) is FailureReason.NO_RESULT


def test_every_failure_reason_is_produced_by_a_test_in_this_file() -> None:
    # A scoring error is not a pure scorer's: the evidence tests produce it.
    here = Path(__file__)
    for member in FailureReason:
        file = "test_eval_evidence.py" if member is FailureReason.SCORING_ERROR else here.name
        source = here.with_name(file).read_text(encoding="utf-8")
        assert f"FailureReason.{member.name}" in source, member
