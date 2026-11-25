"""Rule backtesting.

Responsibility: measure how a rule would have performed against
`fraud_labels`. A rule is a SQL WHERE clause over `transactions`; the caller
supplies only the clause, and every number comes from one fixed SQL template
written here and run through `db.run_query` (read-only, with a timeout).
Nothing is computed or stated by a model.

How the clause sees the data. The template selects the flagged rows first,
in a CTE whose only table is `transactions`, unaliased:

    WITH flagged AS (SELECT id, amount_usd_cents FROM transactions WHERE (<clause>))

so the clause uses bare column names (`amount_usd_cents`, `card_id`) or
`transactions.<column>`, and may contain subqueries on other tables, as in
the brief's example (`card_id IN (SELECT id FROM cards WHERE ...)`). The
labels are joined to `flagged` afterwards, outside the clause's scope, so a
name such as `id` can only mean `transactions.id` and the label columns
cannot be used as if they were transaction columns.

Metrics. Only about 67% of transactions have a label, and an unlabelled
transaction is unknown, not legitimate. Precision and recall therefore use
labelled transactions only; the unlabelled part of the flagged set is
reported separately so the caveat can be shown. A ratio whose denominator is
zero is `None`, never 0.

Policy. The clause is untrusted text placed inside our template, and a rule
that reads `fraud_labels` scores perfectly and means nothing. So `backtest`
asks `sql_guard.check_rule_clause` first and measures only a clause it
accepted: one that cannot break out of the template's parentheses, compiles
as one WHERE expression, and reads neither the labels nor anything outside
the data. A rejection is raised as `RuleClauseError` with the guard's reason
code and reason. Writes are impossible either way (`db` enforces read-only
in the engine).

By month. `backtest_by_month` measures the same clause once per calendar
month with a second fixed statement (docs/DECISIONS.md, D-28). The clause is
contained the same way and passes the same policy; the statement then walks
every transaction once, so each month also carries its own totals and a
month the rule never flags is still a row. A month is the first seven
characters of `transactions.date` (`YYYY-MM`).
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from fsm_assistant import db
from fsm_assistant.schema_context import label_join
from fsm_assistant.sql_guard import ReasonCode, check_rule_clause

# The clause is wrapped in parentheses and followed by a newline. With the
# guard's containment check this makes it one boolean expression: it cannot
# add a UNION, GROUP BY or LIMIT to the CTE, or end the statement early.
#
# The label join is the cast form prescribed by D-10 (`label_join`), which
# uses the index on fraud_labels.transaction_id; the uncast LEFT JOIN compares
# every flagged row with every label. One row comes back even when nothing is
# flagged.
# Dataset totals come from fraud_labels alone: every label belongs to exactly
# one transaction (checked by an integration test).
_TEMPLATE = """\
WITH flagged AS (
  SELECT id, amount_usd_cents FROM transactions WHERE (
{clause}
  )
)
SELECT
  count(flagged.id) AS flagged,
  count(f.is_fraud) AS flagged_labelled,
  coalesce(sum(f.is_fraud = 1), 0) AS true_positives,
  coalesce(sum(f.is_fraud = 0), 0) AS false_positives,
  coalesce(sum(flagged.amount_usd_cents), 0) AS flagged_amount_cents,
  coalesce(sum(CASE WHEN f.is_fraud = 1 THEN flagged.amount_usd_cents END), 0)
    AS fraud_amount_caught_cents,
  coalesce(sum(CASE WHEN f.is_fraud = 0 THEN flagged.amount_usd_cents END), 0)
    AS false_positive_amount_cents,
  (SELECT count(*) FROM transactions) AS total_transactions,
  (SELECT count(is_fraud) FROM fraud_labels) AS total_labelled,
  (SELECT coalesce(sum(is_fraud = 1), 0) FROM fraud_labels) AS total_fraud
FROM flagged
LEFT JOIN fraud_labels f ON """ + label_join("f", "flagged") + "\n"

# One pass over every transaction: the flagged ids and the labels are both
# left-joined to it, so a month's totals and what the rule flags in it come
# from the same rows. The clause still sees `transactions` alone, inside the
# CTE. A month's `total_labelled` and `total_fraud` are counted through the
# join, where the single-row template counts `fraud_labels` directly; the
# two agree while every label belongs to exactly one transaction.
_MONTHLY_TEMPLATE = """\
WITH flagged AS (
  SELECT id FROM transactions WHERE (
{clause}
  )
)
SELECT
  coalesce(substr(t.date, 1, 7), '') AS month,
  count(flagged.id) AS flagged,
  count(CASE WHEN flagged.id IS NOT NULL THEN f.is_fraud END) AS flagged_labelled,
  coalesce(sum(flagged.id IS NOT NULL AND f.is_fraud = 1), 0) AS true_positives,
  coalesce(sum(flagged.id IS NOT NULL AND f.is_fraud = 0), 0) AS false_positives,
  coalesce(sum(CASE WHEN flagged.id IS NOT NULL THEN t.amount_usd_cents END), 0)
    AS flagged_amount_cents,
  coalesce(sum(CASE WHEN flagged.id IS NOT NULL AND f.is_fraud = 1 THEN t.amount_usd_cents END), 0)
    AS fraud_amount_caught_cents,
  coalesce(sum(CASE WHEN flagged.id IS NOT NULL AND f.is_fraud = 0 THEN t.amount_usd_cents END), 0)
    AS false_positive_amount_cents,
  count(*) AS total_transactions,
  count(f.is_fraud) AS total_labelled,
  coalesce(sum(f.is_fraud = 1), 0) AS total_fraud
FROM transactions t
LEFT JOIN flagged ON flagged.id = t.id
LEFT JOIN fraud_labels f ON """ + label_join("f", "t") + """
GROUP BY month
ORDER BY month
"""
# More months than any dataset this tool is pointed at will hold: 100 years.
_MONTH_ROW_CAP = 1200


class RuleClauseError(Exception):
    """The clause cannot be backtested.

    `reason` says why and `reason_code` is the guard's code for it.
    `sqlite_message` is SQLite's own message, verbatim, when SQLite rejected
    the clause (syntax error, unknown column, ...), else `None`. When the
    guard refused the clause, `reason` can be shown or handed back to the
    model. When SQLite failed the measurement as it ran, `reason` and
    `sqlite_message` can quote a value from the data: they are for the user
    only, never for a model or a saved state. A clause the read-only
    authorizer denied is reported here too, so callers handle one error
    type for everything that is the clause's fault.
    """

    def __init__(
        self, reason: str, reason_code: ReasonCode, sqlite_message: str | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.reason_code = reason_code
        self.sqlite_message = sqlite_message


class BacktestResult(BaseModel):
    """What a rule would have flagged, measured against the fraud labels.

    Counts are transactions; amounts are sums of `amount_usd_cents` (cents,
    and net of negative amounts, which exist in the data). The ratios are
    properties worked out from the counts, not fields, so `model_dump` holds
    the measured numbers only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    clause: str  # the clause as measured (surrounding whitespace removed)
    flagged: int  # transactions matching the rule, labelled or not
    flagged_labelled: int  # of those, the ones that have a fraud label
    true_positives: int  # flagged and labelled fraud ("fraud caught")
    false_positives: int  # flagged and labelled not fraud
    total_transactions: int
    total_labelled: int  # labelled transactions in the dataset
    total_fraud: int  # labelled fraud in the dataset (the recall denominator)
    flagged_amount_cents: int  # all flagged transactions, labelled or not
    fraud_amount_caught_cents: int  # the true positives
    false_positive_amount_cents: int  # legitimate payments the rule would block
    elapsed_seconds: float

    @property
    def flagged_unlabelled(self) -> int:
        """Flagged transactions with no label: neither fraud nor legitimate is known."""
        return self.flagged - self.flagged_labelled

    @property
    def false_negatives(self) -> int:
        """Labelled fraud the rule did not flag."""
        return self.total_fraud - self.true_positives

    @property
    def precision(self) -> float | None:
        """Fraud caught / flagged labelled. None when nothing flagged is labelled."""
        return _ratio(self.true_positives, self.flagged_labelled)

    @property
    def recall(self) -> float | None:
        """Fraud caught / all labelled fraud. None when the dataset has no labelled fraud."""
        return _ratio(self.true_positives, self.total_fraud)

    @property
    def flag_rate(self) -> float | None:
        """Flagged / all transactions, labelled or not."""
        return _ratio(self.flagged, self.total_transactions)

    @property
    def flagged_label_coverage(self) -> float | None:
        """Share of the flagged set that is labelled, i.e. that precision is based on."""
        return _ratio(self.flagged_labelled, self.flagged)


class MonthlyBacktest(BaseModel):
    """One calendar month of a rule's backtest, with that month's own totals.

    The counts and amounts mean what they mean in `BacktestResult`, limited
    to the transactions dated in `month`. The ratios use the month's own
    denominators and are `None` when undefined.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    month: str  # YYYY-MM
    flagged: int
    flagged_labelled: int
    true_positives: int
    false_positives: int
    flagged_amount_cents: int
    fraud_amount_caught_cents: int
    false_positive_amount_cents: int
    total_transactions: int  # all transactions of the month
    total_labelled: int  # of those, the labelled ones
    total_fraud: int  # labelled fraud in the month (its recall denominator)

    @property
    def flagged_unlabelled(self) -> int:
        return self.flagged - self.flagged_labelled

    @property
    def precision(self) -> float | None:
        """Fraud caught / flagged labelled, in this month."""
        return _ratio(self.true_positives, self.flagged_labelled)

    @property
    def recall(self) -> float | None:
        """Fraud caught / the month's labelled fraud."""
        return _ratio(self.true_positives, self.total_fraud)

    @property
    def flag_rate(self) -> float | None:
        """Flagged / the month's transactions."""
        return _ratio(self.flagged, self.total_transactions)


class BacktestByMonth(BaseModel):
    """A rule measured month by month: one row per month that has transactions, in order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clause: str
    months: tuple[MonthlyBacktest, ...]
    elapsed_seconds: float


def backtest(clause: str, *, db_path: Path, timeout_seconds: float) -> BacktestResult:
    """Measure the rule `clause` over every transaction in the database.

    The clause is judged by `sql_guard.check_rule_clause` first (trimmed,
    one trailing `;` dropped) and measured only if accepted. Raises
    `RuleClauseError` when the guard rejects it, or when SQLite or the
    read-only authorizer stops the measurement itself. `db.QueryTimeout`
    and the other `db` errors propagate unchanged.
    """
    decision = check_rule_clause(clause, db_path=db_path)
    if decision.reason_code is not None:
        raise RuleClauseError(
            decision.reason or "", decision.reason_code, decision.sqlite_message
        )
    return _backtest_unchecked(decision.sql, db_path=db_path, timeout_seconds=timeout_seconds)


def backtest_by_month(clause: str, *, db_path: Path, timeout_seconds: float) -> BacktestByMonth:
    """Measure the rule `clause` once per calendar month.

    The same policy and the same errors as `backtest`. Every month that has
    a transaction is a row, also when the rule flags nothing in it.
    """
    decision = check_rule_clause(clause, db_path=db_path)
    if decision.reason_code is not None:
        raise RuleClauseError(
            decision.reason or "", decision.reason_code, decision.sqlite_message
        )
    result = _run(
        _MONTHLY_TEMPLATE.format(clause=decision.sql),
        db_path=db_path,
        row_cap=_MONTH_ROW_CAP,
        timeout_seconds=timeout_seconds,
    )
    return BacktestByMonth(
        clause=decision.sql,
        months=tuple(
            MonthlyBacktest.model_validate(dict(zip(result.columns, row, strict=True)))
            for row in result.rows
        ),
        elapsed_seconds=result.elapsed_seconds,
    )


def backtest_statements(clause: str) -> tuple[str, str]:
    """The two statements that measure `clause`, as text: overall, then by month.

    For showing a reader what ran. `clause` is one the guard accepted; nothing
    is checked or run here.
    """
    return _TEMPLATE.format(clause=clause), _MONTHLY_TEMPLATE.format(clause=clause)


def _backtest_unchecked(clause: str, *, db_path: Path, timeout_seconds: float) -> BacktestResult:
    """Run the template with `clause` as it is. No policy is applied.

    For `backtest`, after the guard has accepted the clause, and for tests
    that show what an unguarded clause would score. Nothing else may call it.
    """
    result = _run(
        _TEMPLATE.format(clause=clause), db_path=db_path, row_cap=1, timeout_seconds=timeout_seconds
    )
    # By column name, so the template and the result type cannot drift apart:
    # a column that is not a field, or a field with no column, fails validation.
    return BacktestResult.model_validate(
        {
            "clause": clause,
            "elapsed_seconds": result.elapsed_seconds,
            **dict(zip(result.columns, result.rows[0], strict=True)),
        }
    )


def _run(sql: str, *, db_path: Path, row_cap: int, timeout_seconds: float) -> db.QueryResult:
    """Run one of the two templates; what SQLite or the authorizer refuses is the clause's fault."""
    try:
        return db.run_query(sql, db_path=db_path, row_cap=row_cap, timeout_seconds=timeout_seconds)
    except db.QueryError as error:
        raise RuleClauseError(
            f"SQLite rejected the clause: {error.sqlite_message}",
            ReasonCode.SQLITE_ERROR,
            error.sqlite_message,
        ) from error
    except db.ReadOnlyViolation as error:
        raise RuleClauseError(
            f"The clause is not a read-only expression. {error}", ReasonCode.NOT_READ_ONLY
        ) from error


def _ratio(numerator: int, denominator: int) -> float | None:
    """A fraction, or None when it is undefined."""
    return numerator / denominator if denominator else None
