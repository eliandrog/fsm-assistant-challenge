"""What scoring needs from the database.

Responsibility: fetch the facts the pure scorers are given, and put the
scorers together for one turn. Everything here reads the database through
`db.run_query`, and only after `sql_guard` accepted the text, the reference
queries included: a reference the project's own guard refuses is reported,
not run.

- `run_reference` and `build_pins` run the reference queries; `verify_reference`
  re-runs one and compares it with its pinned result, which is how drift in
  the data or in a reference is noticed.
- `flagged_ids` returns the set of transactions a rule clause flags, and
  `recompute` measures a clause, overall and per calendar month, with
  statements that share nothing with the two backtest templates (no CTE,
  inner joins grouped by label, the month taken with `strftime` and each
  month's totals from statements of their own), so agreement between the
  two is a check and not a repetition.
- `judge` asks the guard, again and independently of the turn, about a
  statement or clause a turn says it executed.
- `score_turn` runs every scorer that applies to a turn. The runner is a
  loop over cases and turns around it. A database error or a timeout while
  scoring fails that turn with `scoring_error` and does not reach the runner.
"""

from collections.abc import Callable, Sequence
from functools import cache
from pathlib import Path

from fsm_assistant import db
from fsm_assistant.evals.cases import (
    Case,
    ExpectedMonth,
    PinnedResult,
    Pins,
    Turn,
    reference_digest,
)
from fsm_assistant.evals.scorers import (
    FailureReason,
    JudgedStatement,
    MonthMetrics,
    Observation,
    RuleMetrics,
    Score,
    StatementKind,
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
from fsm_assistant.rules.backtest import RuleClauseError, backtest
from fsm_assistant.rules.proposal import rule_evidence
from fsm_assistant.schema_context import label_join
from fsm_assistant.sql_guard import RULE_DENIED_FUNCTIONS, check_query, check_rule_clause
from fsm_assistant.sql_scan import UnterminatedQuote, scan

# A flagged set can be every transaction, so these two statements are not
# held to the limits of a user query. They are ours, not model-written.
_ID_ROW_CAP = 5_000_000
_ID_RESULT_BYTES = 100_000_000

_FLAGGED_IDS = "SELECT id FROM transactions WHERE (\n{clause}\n)\n"
_FLAGGED_TOTALS = (
    "SELECT count(*), coalesce(sum(amount_usd_cents), 0) FROM transactions WHERE (\n{clause}\n)\n"
)
_FLAGGED_BY_LABEL = (
    "SELECT f.is_fraud, count(*), coalesce(sum(transactions.amount_usd_cents), 0) "
    "FROM transactions JOIN fraud_labels f ON "
    + label_join("f", "transactions")
    + " WHERE (\n{clause}\n)\nGROUP BY f.is_fraud"
)
_DATASET_TOTALS = (
    "SELECT (SELECT count(*) FROM transactions), count(*), coalesce(sum(is_fraud = 1), 0) "
    "FROM fraud_labels"
)
# The same four, per calendar month. The month is taken with strftime, where
# the backtest template cuts the text of the date.
_MONTH = "strftime('%Y-%m', transactions.date)"
_FLAGGED_BY_MONTH = (
    f"SELECT {_MONTH}, count(*), coalesce(sum(amount_usd_cents), 0) "
    "FROM transactions WHERE (\n{clause}\n)\nGROUP BY 1"
)
_FLAGGED_BY_MONTH_AND_LABEL = (
    f"SELECT {_MONTH}, f.is_fraud, count(*), coalesce(sum(transactions.amount_usd_cents), 0) "
    "FROM transactions JOIN fraud_labels f ON "
    + label_join("f", "transactions")
    + " WHERE (\n{clause}\n)\nGROUP BY 1, 2"
)
_TRANSACTIONS_BY_MONTH = f"SELECT {_MONTH}, count(*) FROM transactions GROUP BY 1"
_LABELS_BY_MONTH = (
    f"SELECT {_MONTH}, count(*), coalesce(sum(f.is_fraud = 1), 0) "
    "FROM transactions JOIN fraud_labels f ON " + label_join("f", "transactions") + " GROUP BY 1"
)
_MONTH_ROW_CAP = 5_000
_MONTH_TOTALS_TIMEOUT_SECONDS = 30.0


class ReferenceRejected(Exception):
    """The guard refused a reference of ours, or a clause handed in for measuring."""

    def __init__(self, kind: StatementKind, text: str, reason_code: str, reason: str) -> None:
        super().__init__(f"the guard rejected a {kind} ({reason_code}): {reason}")
        self.kind = kind
        self.text = text
        self.reason_code = reason_code
        self.reason = reason


def judge(kind: StatementKind, text: str, *, db_path: Path) -> JudgedStatement:
    """The guard's verdict on a statement or clause, asked for afresh."""
    decision = (
        check_query(text, db_path=db_path)
        if kind == "sql"
        else check_rule_clause(text, db_path=db_path)
    )
    return JudgedStatement(
        kind=kind,
        text=text,
        accepted=decision.accepted,
        reason_code=None if decision.reason_code is None else decision.reason_code.value,
    )


def run_reference(
    sql: str, *, db_path: Path, row_cap: int, timeout_seconds: float
) -> db.QueryResult:
    """Run a reference query the way a model-written one would run: guard, then `db`."""
    decision = check_query(sql, db_path=db_path)
    if decision.reason_code is not None:
        raise ReferenceRejected("sql", sql, decision.reason_code.value, decision.reason or "")
    return db.run_query(
        decision.sql, db_path=db_path, row_cap=row_cap, timeout_seconds=timeout_seconds
    )


def build_pins(
    cases: Sequence[Case], *, db_path: Path, row_cap: int, timeout_seconds: float
) -> Pins:
    """Run every reference query and return its rows as pins.

    Raises `ReferenceRejected` for a reference the guard refuses and
    `ValueError` for one whose result does not fit the row cap, since a
    truncated result cannot be an expected answer.
    """
    results: list[PinnedResult] = []
    for case in cases:
        for number, turn in enumerate(case.turns, start=1):
            if turn.sql is None:
                continue
            result = run_reference(
                turn.sql, db_path=db_path, row_cap=row_cap, timeout_seconds=timeout_seconds
            )
            if result.truncated:
                raise ValueError(f"{case.id} turn {number}: the reference result is truncated")
            results.append(
                PinnedResult.model_validate(
                    {
                        "case_id": case.id,
                        "turn": number,
                        "reference_sha256": reference_digest(turn.sql),
                        "rows": result.rows,
                    }
                )
            )
    return Pins(results=tuple(results))


def write_pins(path: Path, pins: Pins) -> None:
    path.write_text(pins.model_dump_json(indent=1) + "\n", encoding="utf-8")


def verify_reference(
    turn: Turn, pin: PinnedResult, *, db_path: Path, row_cap: int, timeout_seconds: float
) -> Score:
    """Re-run a turn's reference query and compare every column with its pin."""
    if turn.sql is None:
        raise ValueError("the turn has no reference query")
    result = run_reference(
        turn.sql, db_path=db_path, row_cap=row_cap, timeout_seconds=timeout_seconds
    )
    whole = turn.comparison.model_copy(
        update={"compare_columns": None, "exact_columns": True, "allow_extra_columns": False}
    )
    return score_result(result.rows, pin.rows, whole, truncated=result.truncated)


def verify_rule_reference(turn: Turn, *, db_path: Path, timeout_seconds: float) -> list[str]:
    """Backtest a turn's reference clause; return how it differs from what the case says.

    The four counts always; the month table and the weakest month when the
    case gives them, and the by-month statement is run only then.
    """
    expected = turn.expected_backtest
    if turn.clause is None or expected is None:
        raise ValueError("the turn has no reference clause")
    by_month = turn.months is not None or turn.weakest_month is not None
    try:
        if not by_month:
            measured = backtest(turn.clause, db_path=db_path, timeout_seconds=timeout_seconds)
        else:
            evidence = rule_evidence(turn.clause, db_path=db_path, timeout_seconds=timeout_seconds)
            measured = evidence.overall
    except RuleClauseError as error:
        raise ReferenceRejected(
            "clause", turn.clause, error.reason_code.value, error.reason
        ) from error
    differences = [
        f"{name}: the case says {wanted}, the backtest gives {getattr(measured, name)}"
        for name, wanted in expected.model_dump().items()
        if getattr(measured, name) != wanted
    ]
    if not by_month:
        return differences
    months = turn.expected_months
    if months is not None:
        counts = set(ExpectedMonth.model_fields) - {"month"}
        given = {row.month: row.model_dump(include=counts) for row in months}
        found = {row.month: row.model_dump(include=counts) for row in evidence.months}
        differences += [
            f"{month}: the case says {given.get(month, 'nothing')}, "
            f"the backtest gives {found.get(month, 'no such month')}"
            for month in sorted(given.keys() | found.keys())
            if given.get(month) != found.get(month)
        ]
    if turn.weakest_month is not None:
        weakest = "none" if evidence.weakest_month is None else evidence.weakest_month.month
        if weakest != turn.weakest_month:
            differences.append(
                f"weakest_month: the case says {turn.weakest_month}, the evidence gives {weakest}"
            )
    return differences


def flagged_ids(clause: str, *, db_path: Path, timeout_seconds: float) -> frozenset[int]:
    """The ids of the transactions the rule `clause` flags."""
    text = _accepted_clause(clause, db_path)
    result = db.run_query(
        _FLAGGED_IDS.format(clause=text),
        db_path=db_path,
        row_cap=_ID_ROW_CAP,
        timeout_seconds=timeout_seconds,
        max_result_bytes=_ID_RESULT_BYTES,
    )
    return frozenset(cell for (cell,) in result.rows if isinstance(cell, int))


def recompute(clause: str, *, db_path: Path, timeout_seconds: float) -> RuleMetrics:
    """Measure the rule `clause` without the backtest template."""
    text = _accepted_clause(clause, db_path)

    def run(sql: str, row_cap: int = 10) -> tuple[tuple[db.Cell, ...], ...]:
        return db.run_query(
            sql, db_path=db_path, row_cap=row_cap, timeout_seconds=timeout_seconds
        ).rows

    flagged, flagged_amount = (_whole(cell) for cell in run(_FLAGGED_TOTALS.format(clause=text))[0])
    by_label = {
        _whole(label): (_whole(count), _whole(amount))
        for label, count, amount in run(_FLAGGED_BY_LABEL.format(clause=text))
    }
    transactions, labelled, fraud = (_whole(cell) for cell in run(_DATASET_TOTALS)[0])
    caught, caught_amount = by_label.get(1, (0, 0))
    wrong, wrong_amount = by_label.get(0, (0, 0))
    return RuleMetrics(
        flagged=flagged,
        flagged_labelled=caught + wrong,
        true_positives=caught,
        false_positives=wrong,
        total_transactions=transactions,
        total_labelled=labelled,
        total_fraud=fraud,
        flagged_amount_cents=flagged_amount,
        fraud_amount_caught_cents=caught_amount,
        false_positive_amount_cents=wrong_amount,
        precision=_ratio(caught, caught + wrong),
        recall=_ratio(caught, fraud),
        flag_rate=_ratio(flagged, transactions),
        months=_recompute_months(text, run, _month_totals(db_path)),
    )


@cache
def _month_totals(db_path: Path) -> tuple[tuple[str, int, int, int], ...]:
    """Each month's transactions, labelled transactions and labelled fraud, in order.

    Two statements that involve no clause. Cached per path, as the schema
    context is: the database does not change while the application runs.
    """

    def run(sql: str) -> tuple[tuple[db.Cell, ...], ...]:
        return db.run_query(
            sql,
            db_path=db_path,
            row_cap=_MONTH_ROW_CAP,
            timeout_seconds=_MONTH_TOTALS_TIMEOUT_SECONDS,
        ).rows

    labels = {
        _text(month): (_whole(count), _whole(fraud)) for month, count, fraud in run(_LABELS_BY_MONTH)
    }
    return tuple(
        sorted(
            (_text(month), _whole(count), *labels.get(_text(month), (0, 0)))
            for month, count in run(_TRANSACTIONS_BY_MONTH)
        )
    )


def _recompute_months(
    clause: str,
    run: Callable[[str, int], tuple[tuple[db.Cell, ...], ...]],
    totals: Sequence[tuple[str, int, int, int]],
) -> tuple[MonthMetrics, ...]:
    """The clause per calendar month, by two statements that are not the backtest's."""
    flagged = {
        _text(month): (_whole(count), _whole(amount))
        for month, count, amount in run(_FLAGGED_BY_MONTH.format(clause=clause), _MONTH_ROW_CAP)
    }
    by_label = {
        (_text(month), _whole(label)): (_whole(count), _whole(amount))
        for month, label, count, amount in run(
            _FLAGGED_BY_MONTH_AND_LABEL.format(clause=clause), 2 * _MONTH_ROW_CAP
        )
    }
    months: list[MonthMetrics] = []
    for month, transactions, labelled, fraud in totals:
        caught, caught_amount = by_label.get((month, 1), (0, 0))
        wrong, wrong_amount = by_label.get((month, 0), (0, 0))
        count, amount = flagged.get(month, (0, 0))
        months.append(
            MonthMetrics(
                month=month,
                flagged=count,
                flagged_labelled=caught + wrong,
                true_positives=caught,
                false_positives=wrong,
                total_transactions=transactions,
                total_labelled=labelled,
                total_fraud=fraud,
                flagged_amount_cents=amount,
                fraud_amount_caught_cents=caught_amount,
                false_positive_amount_cents=wrong_amount,
            )
        )
    return tuple(months)


def score_turn(
    turn: Turn,
    observation: Observation,
    pin: PinnedResult | None,
    *,
    db_path: Path,
    timeout_seconds: float,
) -> tuple[Score, ...]:
    """Every score that applies to one turn of a case.

    Always: outcome, behaviour and guardrail. Then the result against the
    pin for a turn with a reference query, the top segment for a scan, and
    rule arithmetic and rule match for a turn with a reference clause, with
    the weakest month when the case names one. A
    turn with `or_rule` that proposed a rule is scored as a rule turn: its
    arithmetic, and whether the rule is one the case accepts.

    The scores that read the database are replaced by one `scoring_error`
    when it fails or times out; the detail names the kind of error only,
    because SQLite's message can quote a value from a row.
    """
    as_rule = turn.or_rule is not None and observation.outcome == "rule_proposed"
    scores: list[Score] = [
        score_outcome(
            "rule_proposed" if as_rule else turn.outcome,
            observation.outcome,
            expected_action="propose_rule" if as_rule else turn.action,
            observed_action=observation.action,
        ),
        score_behaviour(
            observed_facts(observation),
            require=frozenset(turn.require),
            forbid=turn.forbidden - {"executed"} if as_rule else turn.forbidden,
        ),
    ]
    try:
        scores += _database_scores(
            turn, observation, pin, as_rule, db_path=db_path, timeout_seconds=timeout_seconds
        )
    except db.DbError as error:
        scores.append(
            Score(
                scorer="scoring",
                passed=False,
                reason=FailureReason.SCORING_ERROR,
                detail=f"the database failed while this turn was scored: {type(error).__name__}",
            )
        )
    return tuple(scores)


def _database_scores(
    turn: Turn,
    observation: Observation,
    pin: PinnedResult | None,
    as_rule: bool,
    *,
    db_path: Path,
    timeout_seconds: float,
) -> list[Score]:
    ran: tuple[tuple[StatementKind, str | None], ...] = (
        ("sql", observation.sql),
        ("clause", observation.where_clause),
    )
    executed = [judge(kind, text, db_path=db_path) for kind, text in ran if text is not None]
    scores: list[Score] = [score_guardrail(executed, rejections=observation.rejections)]

    if turn.sql is not None:
        if pin is None:
            raise ValueError("a turn with a reference query needs its pinned result")
        scores.append(
            score_result(
                observation.rows, pin.rows, turn.comparison, truncated=observation.truncated
            )
        )
    if turn.scan_top:
        scores.append(score_scan_top(observation.rows, turn.scan_top))

    clause = observation.where_clause
    if clause is not None and not all(statement.accepted for statement in executed):
        clause = None
    if turn.clause is not None:
        if clause is None:
            scores.append(score_rule_arithmetic(None, _NOTHING))
            scores.append(score_rule_match(None, frozenset(), turn.thresholds))
        else:
            scores.append(
                score_rule_arithmetic(
                    observation.backtest,
                    recompute(clause, db_path=db_path, timeout_seconds=timeout_seconds),
                )
            )
            scores.append(
                score_rule_match(
                    flagged_ids(clause, db_path=db_path, timeout_seconds=timeout_seconds),
                    flagged_ids(turn.clause, db_path=db_path, timeout_seconds=timeout_seconds),
                    turn.thresholds,
                )
            )
        if turn.weakest_month is not None:
            scores.append(
                score_weakest_month(
                    observation.weakest_month, turn.weakest_month, proposed=clause is not None
                )
            )
    if as_rule and turn.or_rule is not None:
        measured = (
            None
            if clause is None
            else recompute(clause, db_path=db_path, timeout_seconds=timeout_seconds)
        )
        scores.append(
            score_rule_arithmetic(None, _NOTHING)
            if measured is None
            else score_rule_arithmetic(observation.backtest, measured)
        )
        scores.append(
            score_rule_alternative(
                measured,
                min_flag_rate=turn.or_rule.min_flag_rate,
                max_flag_rate=turn.or_rule.max_flag_rate,
                deterministic=clause is None or not _calls_denied_function(clause),
            )
        )
    return scores


def _calls_denied_function(clause: str) -> bool:
    """Whether the clause names a function the rule policy denies, random() among them."""
    try:
        return any(
            token.kind == "word" and token.text.lower() in RULE_DENIED_FUNCTIONS
            for token in scan(clause)
        )
    except UnterminatedQuote:
        return False

_NOTHING = RuleMetrics(
    flagged=0,
    flagged_labelled=0,
    true_positives=0,
    false_positives=0,
    total_transactions=0,
    total_labelled=0,
    total_fraud=0,
    flagged_amount_cents=0,
    fraud_amount_caught_cents=0,
    false_positive_amount_cents=0,
    precision=None,
    recall=None,
    flag_rate=None,
)


def _accepted_clause(clause: str, db_path: Path) -> str:
    decision = check_rule_clause(clause, db_path=db_path)
    if decision.reason_code is not None:
        raise ReferenceRejected("clause", clause, decision.reason_code.value, decision.reason or "")
    return decision.sql


def _text(cell: db.Cell) -> str:
    """A month as text; a transaction without a date is counted under the empty month."""
    if cell is None:
        return ""
    if not isinstance(cell, str):
        raise TypeError(f"expected a month as text from SQLite, got {cell!r}")
    return cell


def _whole(cell: db.Cell) -> int:
    if not isinstance(cell, int):
        raise TypeError(f"expected an integer from SQLite, got {cell!r}")
    return cell


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None
