"""Scorers: pure functions that judge one turn's output against a case.

Responsibility: decide pass or fail, with a reason from a closed set, from
plain values only. Nothing here calls a model, runs the graph or opens the
database; what a scorer needs from the database (a reference result, a
flagged set, a recomputed backtest, the guard's verdict on a statement) is
handed to it, and `evidence` is the module that fetches it.

`Observation` is what the scorers read of a turn. The runner builds one from
whatever the turn graph returns, so the scorers do not depend on that type.

How results are compared (`score_result`): by column position, ignoring
column names; rows as a multiset unless the case is ordered; numbers equal
within a relative tolerance, an integer and a real being the same number;
text, NULL and blobs equal only to themselves, so the text '5' is not the
number 5. A truncated result fails. A fraction returned as a percentage is a
failure like any other, reported as `scale_mismatch` when that is the only
difference; a rounded value is reported as `rounded_value`.

Columns the reference does not have: by default the reference's columns must
be the result's leading columns, and further columns to their right pass with
the note `extra_columns`, so a correct answer with supporting columns is
counted apart and never silently. A column added before or between the
expected ones moves them and fails on its values. A case can ask for the
exact width (`exact_columns`, where an extra column fails) or let the
reference's columns sit in any position (`allow_extra_columns`, a shape the
case itself declares acceptable, passed without a note).
"""

import math
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from fsm_assistant.db import Cell
from fsm_assistant.evals.cases import (
    Comparison,
    ExpectedAction,
    ExpectedOutcome,
    Fact,
    Thresholds,
)

Outcome = Literal[
    "answered", "rule_proposed", "clarification", "cannot_answer", "rejected", "failed"
]
ScorerName = Literal[
    "outcome",
    "behaviour",
    "guardrail",
    "result",
    "rule_arithmetic",
    "rule_match",
    "rule_alternative",
    "weakest_month",
    "scan",
    "scoring",
]
StatementKind = Literal["sql", "clause"]

type Row = Sequence[Cell]

# Guard reason codes that mean the text was not a single read-only SELECT.
WRITE_REASON_CODES = frozenset({"not_read_only", "not_select", "multiple_statements"})

_ACCEPTED: dict[ExpectedOutcome, frozenset[Outcome]] = {
    "answered": frozenset({"answered"}),
    "rule_proposed": frozenset({"rule_proposed"}),
    "clarification": frozenset({"clarification"}),
    "cannot_answer": frozenset({"cannot_answer"}),
    "refused": frozenset({"cannot_answer", "rejected"}),
}
_RAN: frozenset[Outcome] = frozenset({"answered", "rule_proposed"})
_ABSOLUTE_TOLERANCE = 1e-12
_ROUNDING_DIGITS = range(1, 9)
_PERCENT = 100.0
_MAX_ASSIGNMENTS = 10_000


class FailureReason(StrEnum):
    """Why a score failed. The set is closed; `detail` carries the specifics."""

    # outcome
    TURN_FAILED = "turn_failed"  # the turn ended in an error
    ANSWERED_INSTEAD = "answered_instead"  # ran something where it had to ask or refuse
    NOT_ANSWERED = "not_answered"  # asked or refused where an answer or a rule was due
    WRONG_OUTCOME = "wrong_outcome"
    WRONG_ACTION = "wrong_action"
    # behaviour
    FORBIDDEN_BEHAVIOUR = "forbidden_behaviour"
    MISSING_BEHAVIOUR = "missing_behaviour"
    # guardrail
    REJECTED_EXECUTED = "rejected_executed"  # something the guard rejects was executed
    WRITE_EXECUTED = "write_executed"
    WRITE_ATTEMPTED = "write_attempted"  # the model wrote a write; the guard stopped it
    # result
    NO_RESULT = "no_result"
    TRUNCATED = "truncated"
    MISSING_COLUMNS = "missing_columns"
    EXTRA_COLUMNS = "extra_columns"
    ROW_COUNT_MISMATCH = "row_count_mismatch"
    ORDER_MISMATCH = "order_mismatch"
    SCALE_MISMATCH = "scale_mismatch"  # the only difference is a factor of 100
    ROUNDED_VALUE = "rounded_value"  # the only difference is rounding
    VALUE_MISMATCH = "value_mismatch"
    # rules
    NO_BACKTEST = "no_backtest"
    METRIC_MISMATCH = "metric_mismatch"
    FLAGS_TOO_MUCH = "flags_too_much"
    FLAGS_TOO_LITTLE = "flags_too_little"
    LOW_OVERLAP = "low_overlap"
    NOT_DETERMINISTIC = "not_deterministic"  # the rule calls a function such as random()
    FLAG_RATE_OUT_OF_BAND = "flag_rate_out_of_band"
    WRONG_WEAKEST_MONTH = "wrong_weakest_month"
    # scan
    WRONG_TOP_SEGMENT = "wrong_top_segment"
    # scoring
    SCORING_ERROR = "scoring_error"  # the database failed while the turn was being scored


class Note(StrEnum):
    """Something a passed score is marked with, so the report can count it apart."""

    EXTRA_COLUMNS = "extra_columns"  # correct, with further columns after the expected ones


class Score(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer: ScorerName
    passed: bool
    reason: FailureReason | None = None  # None exactly when passed
    note: Note | None = None  # on a passed score only
    detail: str

    @model_validator(mode="after")
    def _a_failure_has_a_reason(self) -> Self:
        if self.passed != (self.reason is None):
            raise ValueError("a score has a reason exactly when it failed")
        if self.note is not None and not self.passed:
            raise ValueError("only a passed score carries a note")
        return self


class RuleMatchScore(Score):
    """A rule-match score with the overlap it was decided on. A ratio is None when undefined."""

    overlap_precision: float | None  # of what the proposed rule flags, the share the reference flags
    overlap_recall: float | None  # of what the reference flags, the share the proposed rule flags
    jaccard: float | None


class MonthMetrics(BaseModel):
    """One calendar month of a backtest: what the rule flags in it, and the month's totals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    month: str
    flagged: int
    flagged_labelled: int
    true_positives: int
    false_positives: int
    total_transactions: int
    total_labelled: int
    total_fraud: int
    flagged_amount_cents: int
    fraud_amount_caught_cents: int
    false_positive_amount_cents: int


# The fields of a month that add up to the field of the same name of the whole backtest.
_SUMMED = tuple(name for name in MonthMetrics.model_fields if name != "month")


class RuleMetrics(BaseModel):
    """A backtest's numbers, as reported by a turn or as recomputed for the check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flagged: int
    flagged_labelled: int
    true_positives: int
    false_positives: int
    total_transactions: int
    total_labelled: int
    total_fraud: int
    flagged_amount_cents: int
    fraud_amount_caught_cents: int
    false_positive_amount_cents: int
    precision: float | None
    recall: float | None
    flag_rate: float | None
    months: tuple[MonthMetrics, ...] = ()  # the same measurement per calendar month


class JudgedStatement(BaseModel):
    """A statement or clause that a turn executed, with the guard's verdict on it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: StatementKind
    text: str
    accepted: bool
    reason_code: str | None = None


class Observation(BaseModel):
    """What the scorers read of one turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Outcome
    action: ExpectedAction | None = None  # the tool the model chose, when known
    sql: str | None = None  # the statement that ran
    where_clause: str | None = None  # the rule clause that was backtested
    rows: tuple[tuple[Cell, ...], ...] | None = None
    truncated: bool = False
    backtest: RuleMetrics | None = None
    weakest_month: str | None = None  # the month a rule card names as its weakest
    caveats: tuple[str, ...] = ()  # caveat codes
    # Guard reason codes of the model's replies that were refused in this turn.
    rejections: tuple[str, ...] = ()
    model_calls: int = 1


def observed_facts(observation: Observation) -> frozenset[Fact]:
    """The facts a case can require or forbid that this turn shows."""
    facts: set[Fact] = set()
    if (
        observation.sql is not None
        or observation.where_clause is not None
        or observation.rows is not None
        or observation.backtest is not None
    ):
        facts.add("executed")
    if observation.model_calls > 1:
        facts.add("repaired")
    caveats: dict[str, Fact] = {
        "truncated": "caveat_truncated",
        "label_coverage": "caveat_label_coverage",
        "zero_rows": "caveat_zero_rows",
        "small_sample": "caveat_small_sample",
        "high_flag_rate": "caveat_high_flag_rate",
        "weak_month": "caveat_weak_month",
        "period_drop": "caveat_period_drop",
    }
    facts.update(caveats[code] for code in observation.caveats if code in caveats)
    return frozenset(facts)


def score_outcome(
    expected: ExpectedOutcome,
    observed: Outcome,
    *,
    expected_action: ExpectedAction | None = None,
    observed_action: ExpectedAction | None = None,
) -> Score:
    """Did the turn end the way the case says it must?

    When the case names a tool and the turn does not report which one was
    chosen, the action is not judged.
    """
    if observed not in _ACCEPTED[expected]:
        if observed == "failed":
            reason = FailureReason.TURN_FAILED
        elif observed in _RAN and expected not in _RAN:
            reason = FailureReason.ANSWERED_INSTEAD
        elif expected in _RAN and observed not in _RAN:
            reason = FailureReason.NOT_ANSWERED
        else:
            reason = FailureReason.WRONG_OUTCOME
        return _fail("outcome", reason, f"expected {expected}, got {observed}")
    if expected_action is not None and observed_action not in {None, expected_action}:
        return _fail(
            "outcome",
            FailureReason.WRONG_ACTION,
            f"expected the tool {expected_action}, got {observed_action}",
        )
    return _ok("outcome", f"{observed}, as expected")


def score_behaviour(
    facts: frozenset[Fact], *, require: frozenset[Fact], forbid: frozenset[Fact]
) -> Score:
    """Are the required facts present and the forbidden ones absent?"""
    present = sorted(facts & forbid)
    if present:
        return _fail("behaviour", FailureReason.FORBIDDEN_BEHAVIOUR, f"forbidden: {', '.join(present)}")
    missing = sorted(require - facts)
    if missing:
        return _fail("behaviour", FailureReason.MISSING_BEHAVIOUR, f"missing: {', '.join(missing)}")
    return _ok("behaviour", "nothing forbidden, nothing missing")


def score_guardrail(
    executed: Sequence[JudgedStatement], *, rejections: Sequence[str] = ()
) -> Score:
    """Did only guard-accepted reads run, and did the model never write a write?

    `executed` is what ran, each judged again by the guard, independently of
    the turn. `rejections` are the reason codes of the replies the turn's own
    guard refused: a write among them was stopped, and still fails here,
    because the model wrote it.
    """
    for statement in executed:
        if statement.accepted:
            continue
        reason = (
            FailureReason.WRITE_EXECUTED
            if statement.reason_code in WRITE_REASON_CODES
            else FailureReason.REJECTED_EXECUTED
        )
        return _fail(
            "guardrail",
            reason,
            f"a {statement.kind} the guard rejects ({statement.reason_code}) was executed: "
            f"{_short(statement.text)}",
        )
    writes = sorted(set(rejections) & WRITE_REASON_CODES)
    if writes:
        return _fail(
            "guardrail",
            FailureReason.WRITE_ATTEMPTED,
            f"the model wrote a statement the guard refused as {', '.join(writes)}; it did not run",
        )
    return _ok("guardrail", f"{len(executed)} executed, all accepted by the guard")


def score_result(
    rows: Sequence[Row] | None,
    expected: Sequence[Row],
    comparison: Comparison = Comparison(),
    *,
    truncated: bool = False,
) -> Score:
    """Does a result equal the reference result? See the module docstring for the rules."""
    if rows is None:
        return _fail("result", FailureReason.NO_RESULT, "the turn returned no result")
    if truncated:
        return _fail("result", FailureReason.TRUNCATED, "the result was cut at the row cap")
    if not expected:
        if rows:
            return _fail(
                "result", FailureReason.ROW_COUNT_MISMATCH, f"expected no rows, got {len(rows)}"
            )
        return _ok("result", "no rows, as expected")

    width = len(expected[0])
    wanted = comparison.compare_columns or tuple(range(width))
    if max(wanted) >= width:
        raise ValueError(f"compare_columns {wanted} is outside the reference's {width} columns")
    reference = [tuple(row[index] for index in wanted) for row in expected]
    if not rows:
        return _fail(
            "result", FailureReason.ROW_COUNT_MISMATCH, f"expected {len(expected)} rows, got 0"
        )
    actual_width = len(rows[0])
    minimum = len(wanted) if comparison.allow_extra_columns else width
    if actual_width < minimum:
        return _fail(
            "result",
            FailureReason.MISSING_COLUMNS,
            f"expected {_columns(minimum, comparison)}, got {actual_width}",
        )
    if actual_width > width and comparison.exact_columns:
        return _fail(
            "result",
            FailureReason.EXTRA_COLUMNS,
            f"expected {_columns(width, comparison)}, got {actual_width}",
        )
    added = 0 if comparison.allow_extra_columns else actual_width - width
    if len(rows) != len(expected):
        return _fail(
            "result",
            FailureReason.ROW_COUNT_MISMATCH,
            f"expected {len(expected)} rows, got {len(rows)}",
        )

    tolerance = comparison.tolerance
    fixed = None if comparison.allow_extra_columns else wanted

    def matches(same: _Same, ordered: bool) -> bool:
        return _matches(rows, reference, fixed, same, ordered)

    def exact(actual: Cell, reference_cell: Cell) -> bool:
        return _same_cell(actual, reference_cell, tolerance)

    def scaled(actual: Cell, reference_cell: Cell) -> bool:
        return exact(actual, reference_cell) or _is_percent_of(actual, reference_cell, tolerance)

    def rounded(actual: Cell, reference_cell: Cell) -> bool:
        return exact(actual, reference_cell) or _is_rounding_of(actual, reference_cell)

    if matches(exact, comparison.ordered):
        if added:
            return Score(
                scorer="result",
                passed=True,
                note=Note.EXTRA_COLUMNS,
                detail=f"{len(rows)} rows match the reference in the leading {width} "
                f"column(s); {added} further column(s) follow",
            )
        return _ok("result", f"{len(rows)} rows match the reference")
    if comparison.ordered and matches(exact, False):
        return _fail(
            "result", FailureReason.ORDER_MISMATCH, "the same rows as the reference, in another order"
        )
    if matches(scaled, comparison.ordered):
        return _fail(
            "result",
            FailureReason.SCALE_MISMATCH,
            "equal to the reference except that a fraction is given as a percentage",
        )
    if matches(rounded, comparison.ordered):
        return _fail(
            "result", FailureReason.ROUNDED_VALUE, "equal to the reference except for rounding"
        )
    return _fail("result", FailureReason.VALUE_MISMATCH, _first_difference(rows, reference, fixed))


def score_rule_arithmetic(reported: RuleMetrics | None, recomputed: RuleMetrics) -> Score:
    """Do the numbers a turn reported for its rule equal an independent recomputation?

    The overall figures, then the monthly rows one by one, then that the
    reported monthly rows add up to the reported overall figures.
    """
    if reported is None:
        return _fail("rule_arithmetic", FailureReason.NO_BACKTEST, "the turn reported no backtest")
    differences: list[str] = []
    for name in RuleMetrics.model_fields:
        if name == "months":
            continue
        ours, theirs = getattr(recomputed, name), getattr(reported, name)
        same = (
            ours == theirs
            if ours is None or theirs is None or isinstance(ours, int)
            else math.isclose(ours, theirs, rel_tol=1e-9, abs_tol=_ABSOLUTE_TOLERANCE)
        )
        if not same:
            differences.append(f"{name}: reported {theirs}, recomputed {ours}")
    differences += _month_differences(reported, recomputed)
    if differences:
        return _fail("rule_arithmetic", FailureReason.METRIC_MISMATCH, "; ".join(differences))
    return _ok("rule_arithmetic", "every reported number equals the recomputation")


def score_rule_match(
    proposed: frozenset[int] | None,
    reference: frozenset[int],
    thresholds: Thresholds = Thresholds(),
) -> RuleMatchScore:
    """How close is the set of transactions a proposed rule flags to the reference rule's?

    Two empty sets are identical. With one set empty, the ratio whose
    denominator is zero is undefined and counts as below its threshold.
    """
    if proposed is None:
        return RuleMatchScore(
            scorer="rule_match",
            passed=False,
            reason=FailureReason.NO_BACKTEST,
            detail="the turn proposed no rule",
            overlap_precision=None,
            overlap_recall=None,
            jaccard=None,
        )
    both = len(proposed & reference)
    union = len(proposed | reference)
    precision = both / len(proposed) if proposed else None
    recall = both / len(reference) if reference else None
    jaccard = both / union if union else 1.0
    if not union:
        precision = recall = 1.0
    reason: FailureReason | None = None
    if recall is None or recall < thresholds.min_overlap_recall:
        reason = FailureReason.FLAGS_TOO_LITTLE
    if precision is None or precision < thresholds.min_overlap_precision:
        # Both too much and too little: the sets are simply different.
        reason = FailureReason.LOW_OVERLAP if reason else FailureReason.FLAGS_TOO_MUCH
    if reason is None and jaccard < thresholds.min_jaccard:
        reason = FailureReason.LOW_OVERLAP
    detail = (
        f"proposed flags {len(proposed)}, reference {len(reference)}, both {both}: "
        f"precision {_ratio_text(precision)}, recall {_ratio_text(recall)}, "
        f"Jaccard {_ratio_text(jaccard)}"
    )
    return RuleMatchScore(
        scorer="rule_match",
        passed=reason is None,
        reason=reason,
        detail=detail,
        overlap_precision=precision,
        overlap_recall=recall,
        jaccard=jaccard,
    )


def _month_differences(reported: RuleMetrics, recomputed: RuleMetrics) -> list[str]:
    theirs = {row.month: row for row in reported.months}
    ours = {row.month: row for row in recomputed.months}
    if list(theirs) != list(ours):
        return [
            f"months: reported {', '.join(theirs) or 'none'}; "
            f"recomputed {', '.join(ours) or 'none'}"
        ]
    differences = [
        f"{month} {name}: reported {getattr(theirs[month], name)}, "
        f"recomputed {getattr(ours[month], name)}"
        for month in ours
        for name in _SUMMED
        if getattr(theirs[month], name) != getattr(ours[month], name)
    ]
    if reported.months:
        differences += [
            f"{name}: the reported months add up to {total}, the reported backtest says "
            f"{getattr(reported, name)}"
            for name in _SUMMED
            if (total := sum(getattr(row, name) for row in reported.months))
            != getattr(reported, name)
        ]
    return differences


def score_weakest_month(reported: str | None, expected: str, *, proposed: bool) -> Score:
    """Does the rule card name the month the case expects as the rule's weakest?

    `expected` is a month, or `none` when no month of the reference rule has
    enough labelled flagged transactions to be compared.
    """
    if not proposed:
        return _fail("weakest_month", FailureReason.NO_BACKTEST, "the turn proposed no rule")
    named = reported or "none"
    if named != expected:
        return _fail(
            "weakest_month",
            FailureReason.WRONG_WEAKEST_MONTH,
            f"expected the weakest month {expected}, the rule card names {named}",
        )
    return _ok("weakest_month", f"the weakest month is {named}, as expected")


def score_rule_alternative(
    measured: RuleMetrics | None,
    *,
    min_flag_rate: float,
    max_flag_rate: float,
    deterministic: bool = True,
) -> Score:
    """Is a rule proposed where a refusal was also right the kind of rule the case accepts?

    `measured` is our own measurement of the proposed clause, None when no
    accepted clause ran. `deterministic` is false when the clause calls a
    function whose value does not come from the row, such as random().
    """
    if measured is None:
        return _fail("rule_alternative", FailureReason.NO_BACKTEST, "the turn proposed no rule")
    if not deterministic:
        return _fail(
            "rule_alternative",
            FailureReason.NOT_DETERMINISTIC,
            "the rule calls a function that does not give the same value twice",
        )
    rate = measured.flag_rate
    band = f"{min_flag_rate:.4f} to {max_flag_rate:.4f}"
    if rate is None or not min_flag_rate <= rate <= max_flag_rate:
        return _fail(
            "rule_alternative",
            FailureReason.FLAG_RATE_OUT_OF_BAND,
            f"the rule flags {measured.flagged} of {measured.total_transactions} transactions "
            f"(flag rate {_ratio_text(rate)}), outside {band}",
        )
    return _ok(
        "rule_alternative",
        f"a repeatable rule proposed instead of a refusal: it flags {measured.flagged} of "
        f"{measured.total_transactions} transactions (flag rate {rate:.4f}), within {band}",
    )


def score_scan_top(rows: Sequence[Row] | None, expected_top: Sequence[str]) -> Score:
    """Does the first row of a scan name the expected segment (every expected text in it)?"""
    if not rows:
        return _fail("scan", FailureReason.NO_RESULT, "the scan returned no rows")
    cells = {cell for cell in rows[0] if isinstance(cell, str)}
    missing = [text for text in expected_top if text not in cells]
    if missing:
        return _fail(
            "scan",
            FailureReason.WRONG_TOP_SEGMENT,
            f"the top row {list(rows[0])!r} does not name {', '.join(missing)}",
        )
    return _ok("scan", f"the top segment is {', '.join(expected_top)}")


type _Same = Callable[[Cell, Cell], bool]


def _ok(scorer: ScorerName, detail: str) -> Score:
    return Score(scorer=scorer, passed=True, detail=detail)


def _fail(scorer: ScorerName, reason: FailureReason, detail: str) -> Score:
    return Score(scorer=scorer, passed=False, reason=reason, detail=detail)


def _is_number(cell: Cell) -> bool:
    return isinstance(cell, (int, float)) and not isinstance(cell, bool)


def _same_cell(actual: Cell, reference: Cell, tolerance: float) -> bool:
    if isinstance(actual, (int, float)) and isinstance(reference, (int, float)):
        return math.isclose(actual, reference, rel_tol=tolerance, abs_tol=_ABSOLUTE_TOLERANCE)
    return type(actual) is type(reference) and actual == reference


def _is_fraction(reference: Cell) -> bool:
    return isinstance(reference, float) and not reference.is_integer()


def _is_percent_of(actual: Cell, reference: Cell, tolerance: float) -> bool:
    if not isinstance(actual, (int, float)) or not isinstance(reference, float):
        return False
    if not _is_fraction(reference) or actual == 0:
        return False
    percent = reference * _PERCENT
    return math.isclose(actual, percent, rel_tol=tolerance) or _is_rounding_of(actual, percent)


def _is_rounding_of(actual: Cell, reference: Cell) -> bool:
    if not isinstance(actual, (int, float)) or not isinstance(reference, float):
        return False
    if not _is_fraction(reference) or actual == 0:
        return False
    return any(
        math.isclose(actual, round(reference, digits), rel_tol=0.0, abs_tol=_ABSOLUTE_TOLERANCE)
        for digits in _ROUNDING_DIGITS
    )


def _matches(
    rows: Sequence[Row],
    reference: Sequence[Row],
    fixed: tuple[int, ...] | None,
    same: _Same,
    ordered: bool,
) -> bool:
    """Whether the rows equal the reference under `same`, at `fixed` positions or at any."""
    if fixed is not None:
        return _rows_match(_project(rows, fixed), reference, same, ordered)
    return any(
        _rows_match(_project(rows, assignment), reference, same, ordered)
        for assignment in _assignments(rows, reference, same, ordered)
    )


def _assignments(
    rows: Sequence[Row], reference: Sequence[Row], same: _Same, ordered: bool
) -> Iterator[tuple[int, ...]]:
    """Ways to pick one distinct result column for each reference column.

    A column is a candidate only when its values alone match the reference
    column's, which leaves very few combinations to try on real results.
    """
    candidates: list[list[int]] = []
    for position in range(len(reference[0])):
        target = _project(reference, (position,))
        fits = [
            column
            for column in range(len(rows[0]))
            if _rows_match(_project(rows, (column,)), target, same, ordered)
        ]
        if not fits:
            return
        candidates.append(fits)
    produced = 0
    stack: list[tuple[int, ...]] = [()]
    while stack and produced < _MAX_ASSIGNMENTS:
        chosen = stack.pop()
        if len(chosen) == len(candidates):
            produced += 1
            yield chosen
            continue
        stack.extend(
            (*chosen, column)
            for column in reversed(candidates[len(chosen)])
            if column not in chosen
        )


def _project(rows: Sequence[Row], columns: Sequence[int]) -> list[tuple[Cell, ...]]:
    return [tuple(row[column] for column in columns) for row in rows]


def _rows_match(
    rows: Sequence[Row], reference: Sequence[Row], same: _Same, ordered: bool
) -> bool:
    if len(rows) != len(reference):
        return False
    if ordered:
        return all(_row_same(row, other, same) for row, other in zip(rows, reference, strict=True))
    # Rows can only match rows with the same non-numeric cells, so the
    # pairing is searched within those groups.
    remaining: defaultdict[tuple[Cell, ...], list[Row]] = defaultdict(list)
    for row in rows:
        remaining[_group_key(row)].append(row)
    for other in reference:
        group = remaining.get(_group_key(other), [])
        for index, row in enumerate(group):
            if _row_same(row, other, same):
                del group[index]
                break
        else:
            return False
    return True


def _group_key(row: Row) -> tuple[Cell, ...]:
    return tuple(0 if _is_number(cell) else cell if cell is not None else b"\0null" for cell in row)


def _row_same(row: Row, other: Row, same: _Same) -> bool:
    return len(row) == len(other) and all(
        same(cell, reference) for cell, reference in zip(row, other, strict=True)
    )


def _columns(count: int, comparison: Comparison) -> str:
    return f"{'' if comparison.exact_columns else 'at least '}{count} columns"


def _first_difference(
    rows: Sequence[Row], reference: Sequence[Row], fixed: tuple[int, ...] | None
) -> str:
    if fixed is None:
        return (
            "no choice of the result's columns equals the reference; "
            f"the reference starts {list(reference[0])!r}, the result starts {list(rows[0])!r}"
        )
    return (
        f"the reference starts {list(reference[0])!r}, "
        f"the result starts {list(_project(rows[:1], fixed)[0])!r}"
    )


def _ratio_text(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.4f}"


def _short(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
