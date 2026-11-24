"""The evidence shown with a proposed rule.

Responsibility: put a rule's overall backtest next to the same measurement
month by month, and work out from the monthly rows what a reader needs to
judge how steady the rule is: its weakest month, an earlier and a later
period, lift over the base fraud rate, and the range of monthly precision
(docs/DECISIONS.md, D-28). Every figure comes from the two statements of
`rules.backtest`; the sums and ratios here are arithmetic on those rows.

What this is, and is not. The rule is scored on the same history it was
written from: the analyst and the model saw every month, so no month is
unseen and neither period is a held-out test. The monthly rows are a
stability check. They can show that a rule's overall score comes from a few
months or fades in the later ones. They cannot show that it would hold on
new data; a pattern that runs through the whole dataset passes any split by
time.

Weakest month. The month with the lowest precision among those in which at
least `MIN_MONTH_LABELLED` flagged transactions are labelled, so a month
with two labelled rows cannot be it. Of several months with the same
precision, the one resting on more labelled rows, then the earlier one.

Periods. Months before `LATER_PERIOD_START` are the earlier period and the
others the later one. The boundary is fixed for the shipped data (2019-01 to
2019-07, then 2019-08 to 2019-10: roughly the first two thirds and the last
third) and is not chosen per rule. A period's recall uses that period's
labelled fraud.

Lift. Precision divided by the base fraud rate, which is labelled fraud over
labelled transactions: how many times more often than chance a flagged,
labelled transaction is fraud. `None` when either is undefined or the base
rate is zero.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from fsm_assistant.rules.backtest import (
    BacktestResult,
    MonthlyBacktest,
    backtest,
    backtest_by_month,
)

# A month needs this many labelled flagged transactions for its precision to
# be compared with other months. The same judgement as the small-sample
# caveat of the whole rule; not tuned.
MIN_MONTH_LABELLED = 30
# The first month of the later period, as `YYYY-MM`.
LATER_PERIOD_START = "2019-08"


class PeriodBacktest(BaseModel):
    """The monthly rows of one period, summed. Ratios use the period's own denominators."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    first_month: str | None  # None when the data has no month in the period
    last_month: str | None
    months: int
    flagged: int
    flagged_labelled: int
    true_positives: int
    false_positives: int
    total_transactions: int
    total_labelled: int
    total_fraud: int

    @property
    def precision(self) -> float | None:
        return _ratio(self.true_positives, self.flagged_labelled)

    @property
    def recall(self) -> float | None:
        """Fraud caught / the period's labelled fraud."""
        return _ratio(self.true_positives, self.total_fraud)

    @property
    def flag_rate(self) -> float | None:
        return _ratio(self.flagged, self.total_transactions)

    @property
    def base_fraud_rate(self) -> float | None:
        return _ratio(self.total_fraud, self.total_labelled)

    @property
    def lift(self) -> float | None:
        return lift(self.precision, self.base_fraud_rate)


class Stability(BaseModel):
    """How much the monthly precision moves.

    `qualifying_months` are those with at least `MIN_MONTH_LABELLED`
    labelled flagged transactions; the lowest and highest precision are
    taken over them, and are `None` when there is none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    months: int  # months that have transactions
    months_flagged: int  # months in which the rule flags anything
    months_with_labelled_flags: int  # months in which something flagged is labelled
    qualifying_months: int
    min_precision: float | None
    max_precision: float | None


class RuleEvidence(BaseModel):
    """A rule's overall backtest and its month-by-month stability check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overall: BacktestResult
    months: tuple[MonthlyBacktest, ...]  # every month with transactions, in order
    weakest_month: MonthlyBacktest | None  # None when no month qualifies
    earlier: PeriodBacktest
    later: PeriodBacktest
    stability: Stability
    elapsed_seconds: float  # the two statements together

    @property
    def base_fraud_rate(self) -> float | None:
        """Labelled fraud / labelled transactions, over the whole dataset."""
        return _ratio(self.overall.total_fraud, self.overall.total_labelled)

    @property
    def lift(self) -> float | None:
        return lift(self.overall.precision, self.base_fraud_rate)


def rule_evidence(clause: str, *, db_path: Path, timeout_seconds: float) -> RuleEvidence:
    """Backtest `clause` overall and month by month.

    Two statements, each under `timeout_seconds`, each applying the rule
    policy itself. Raises what `backtest` raises.
    """
    overall = backtest(clause, db_path=db_path, timeout_seconds=timeout_seconds)
    monthly = backtest_by_month(overall.clause, db_path=db_path, timeout_seconds=timeout_seconds)
    return build_evidence(overall, monthly.months, monthly_seconds=monthly.elapsed_seconds)


def build_evidence(
    overall: BacktestResult, months: Sequence[MonthlyBacktest], *, monthly_seconds: float = 0.0
) -> RuleEvidence:
    """The evidence for an overall backtest and its monthly rows. Arithmetic only."""
    rows = tuple(sorted(months, key=lambda row: row.month))
    qualifying = [row.precision for row in _qualifying(rows) if row.precision is not None]
    return RuleEvidence(
        overall=overall,
        months=rows,
        weakest_month=weakest_month(rows),
        earlier=sum_period([row for row in rows if row.month < LATER_PERIOD_START]),
        later=sum_period([row for row in rows if row.month >= LATER_PERIOD_START]),
        stability=Stability(
            months=len(rows),
            months_flagged=sum(row.flagged > 0 for row in rows),
            months_with_labelled_flags=sum(row.flagged_labelled > 0 for row in rows),
            qualifying_months=len(qualifying),
            min_precision=min(qualifying, default=None),
            max_precision=max(qualifying, default=None),
        ),
        elapsed_seconds=overall.elapsed_seconds + monthly_seconds,
    )


def weakest_month(months: Sequence[MonthlyBacktest]) -> MonthlyBacktest | None:
    """The qualifying month with the lowest precision, or None when no month qualifies."""
    return min(
        _qualifying(months),
        key=lambda row: (row.precision or 0.0, -row.flagged_labelled, row.month),
        default=None,
    )


def sum_period(months: Sequence[MonthlyBacktest]) -> PeriodBacktest:
    """The counts of `months` added up."""
    return PeriodBacktest(
        first_month=min((row.month for row in months), default=None),
        last_month=max((row.month for row in months), default=None),
        months=len(months),
        flagged=sum(row.flagged for row in months),
        flagged_labelled=sum(row.flagged_labelled for row in months),
        true_positives=sum(row.true_positives for row in months),
        false_positives=sum(row.false_positives for row in months),
        total_transactions=sum(row.total_transactions for row in months),
        total_labelled=sum(row.total_labelled for row in months),
        total_fraud=sum(row.total_fraud for row in months),
    )


def lift(precision: float | None, base_fraud_rate: float | None) -> float | None:
    """Precision as a multiple of the base fraud rate, or None when it is undefined."""
    if precision is None or not base_fraud_rate:
        return None
    return precision / base_fraud_rate


def _qualifying(months: Sequence[MonthlyBacktest]) -> list[MonthlyBacktest]:
    return [row for row in months if row.flagged_labelled >= MIN_MONTH_LABELLED]


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None
