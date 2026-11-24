"""Result presentation.

Responsibility: turn the end state of a turn into what the user is shown
(docs/DECISIONS.md, D-24). `build_result` chooses the one outcome, a format
hint per column, and caveats from a closed list in a fixed order.
`render_text` is the terminal form of the same result.

Every figure here comes from a `QueryResult`, a `RuleEvidence` or a
`ScanResult`, that is from executed SQL. The model's explanation and assumptions are passed
through unchanged and labelled as the model's.
"""

import re
import textwrap
from collections.abc import Sequence

from fsm_assistant import explore
from fsm_assistant.db import Cell, QueryResult
from fsm_assistant.explore import ScanResult
from fsm_assistant.nl2sql import (
    AskClarification,
    Decline,
    ProfileFraud,
    ProposeRule,
    RunSql,
    tool_name,
)
from fsm_assistant.result import (
    Caveat,
    Column,
    ColumnFormat,
    ErrorKind,
    Outcome,
    RuleCard,
    RuleMonth,
    RulePeriod,
    RuleStability,
    Status,
    TableResult,
    TurnError,
    TurnMeta,
    TurnResult,
)
from fsm_assistant.rules.backtest import BacktestResult, MonthlyBacktest
from fsm_assistant.rules.proposal import MIN_MONTH_LABELLED, PeriodBacktest, RuleEvidence
from fsm_assistant.state import TurnState

# A rule's precision resting on fewer labelled rows than this gets a caveat.
SMALL_SAMPLE_LABELLED = 30
# A rule flagging more than this share of all transactions gets a caveat.
HIGH_FLAG_RATE = 0.05
# A month, or the later period, is "well below" when its precision is under
# this share of the figure it is compared with. The base fraud rate of the
# shipped data itself moves between 0.67 and 1.29 times its average from
# month to month, so a threshold above 0.67 would fire on a rule that only
# follows the base rate.
WEAK_PRECISION_RATIO = 0.5
# ... and only when, at the compared precision, at least this many of its
# labelled flagged transactions would have been fraud. Below that, a month
# with none is what chance gives.
MIN_EXPECTED_FRAUD = 5
# Shown with every month-by-month table.
EVIDENCE_NOTE = (
    "The rule is scored here on the same history it was written from, so this is a "
    "stability check, not a test on unseen data. Only new data or a shadow run can show "
    "that the rule holds."
)
DEFAULT_PRINTED_ROWS = 20
MAX_CELL_CHARACTERS = 60
# Columns are printed until a line would pass this; the rest are named, not shown.
MAX_TABLE_CHARACTERS = 160
_SHOWN_BLOB_BYTES = 16

_STATUS: dict[Outcome, Status] = {
    "answered": "ok",
    "rule_proposed": "ok",
    "clarification": "needs_clarification",
    "cannot_answer": "cannot_answer",
    "rejected": "rejected",
    "failed": "error",
}

# The format hints, stated once (docs/DECISIONS.md, D-24, refers here).
#
# A column's name is read after an optional `table.` prefix and split into
# words at underscores. A hint needs a whole word, never part of one:
# `generated` and `separate` hold no word `rate`.
#
# - usd_cents: the last word is `cents`, or the name is sum, avg, min, max or
#   total of one such column; and the column's first word does not mark a
#   count (`count`, `counts`, `n`, `num`, `distinct`), so `n_cents` and
#   `distinct_amount_usd_cents` are plain numbers. Every value is a number.
# - fraction: one word is rate, fraction, share, ratio, precision or recall,
#   and none is pct, percent, percentage or count. Every value lies between 0
#   and 1 and at least one is not a whole number.
_COLUMN = r"(?:\w+\.)?(\w+)"
_AGGREGATE_OF = re.compile(rf"(?:sum|avg|min|max|total)\(\s*{_COLUMN}\s*\)", re.IGNORECASE)
_BARE_COLUMN = re.compile(_COLUMN)
_COUNT_PREFIX = re.compile(r"(?:counts?|n|num|distinct)_", re.IGNORECASE)
_FRACTION_WORDS = frozenset({"rate", "fraction", "share", "ratio", "precision", "recall"})
_NOT_FRACTION_WORDS = frozenset({"pct", "percent", "percentage", "count"})
# An integer column is an identifier or a code, shown without digit grouping,
# when its whole name (after an optional `table.`) is one of: id, *_id, code,
# *_code, mcc, zip, zipcode, year, year_*, *_year, or a card or account number.
# The name alone decides. Any other integer column, an aggregate such as
# count(user_id) included, is taken for a count or an amount and keeps grouping.
_IDENTIFIER_NAME = re.compile(
    r"(?:\w+\.)?(?:id|\w+_id|code|\w+_code|mcc|zip|zipcode|year|year_\w+|\w+_year"
    r"|(?:card|account|acct)_(?:number|num|no))",
    re.IGNORECASE,
)

LABEL_COVERAGE = (
    "This result uses fraud labels, which only about two thirds of transactions have. "
    "An unlabelled transaction is unknown, not legitimate."
)
SINGLE_SEGMENT = (
    "One segment holds all the labelled fraud. A rule on it will score well on this "
    "history, which says little about fraud elsewhere or later."
)
SCAN_COLUMNS = (
    "dimension", "segment", "labelled", "fraud", "fraud_rate", "lift", "share_of_fraud"
)  # fmt: skip


def status_of(outcome: Outcome) -> Status:
    return _STATUS[outcome]


def outcome_of(kind: ErrorKind) -> Outcome:
    """The outcome a turn ends in when its last failure is of `kind`."""
    return "rejected" if kind == "sql_rejected" else "failed"


def column_format(name: str, values: Sequence[Cell]) -> ColumnFormat:
    """A format hint from the column's name, kept only if every value fits it."""
    present = [value for value in values if value is not None]
    numbers = all(isinstance(value, int | float) for value in present)
    if _is_cents_name(name) and numbers:
        return "usd_cents"
    words = set(re.split(r"[^a-z0-9]+", name.lower()))
    if (
        present
        and words & _FRACTION_WORDS
        and not words & _NOT_FRACTION_WORDS
        and all(isinstance(value, int | float) and 0 <= value <= 1 for value in present)
        and any(isinstance(value, float) for value in present)
    ):
        return "fraction"
    if _IDENTIFIER_NAME.fullmatch(name) and all(isinstance(value, int) for value in present):
        return "identifier"
    return "plain"


def _is_cents_name(name: str) -> bool:
    match = _AGGREGATE_OF.fullmatch(name) or _BARE_COLUMN.fullmatch(name)
    if match is None:
        return False
    column: str = match.group(1)
    return column.lower().endswith("_cents") and not _COUNT_PREFIX.match(column)


def present_table(result: QueryResult) -> TableResult:
    columns = tuple(
        Column(name=name, format=column_format(name, [row[index] for row in result.rows]))
        for index, name in enumerate(result.columns)
    )
    # Built without validating each cell, as `db.run_query` builds its result.
    return TableResult.model_construct(
        columns=columns,
        rows=result.rows,
        row_count=result.row_count,
        truncated=result.truncated,
    )


def rule_card(evidence: RuleEvidence) -> RuleCard:
    backtest = evidence.overall
    return RuleCard(
        flagged=backtest.flagged,
        flagged_labelled=backtest.flagged_labelled,
        flagged_unlabelled=backtest.flagged_unlabelled,
        true_positives=backtest.true_positives,
        false_positives=backtest.false_positives,
        false_negatives=backtest.false_negatives,
        total_transactions=backtest.total_transactions,
        total_labelled=backtest.total_labelled,
        total_fraud=backtest.total_fraud,
        precision=backtest.precision,
        recall=backtest.recall,
        flag_rate=backtest.flag_rate,
        flagged_amount_cents=backtest.flagged_amount_cents,
        fraud_amount_caught_cents=backtest.fraud_amount_caught_cents,
        false_positive_amount_cents=backtest.false_positive_amount_cents,
        base_fraud_rate=evidence.base_fraud_rate,
        lift=evidence.lift,
        months=tuple(_rule_month(row) for row in evidence.months),
        weakest_month=None if evidence.weakest_month is None else evidence.weakest_month.month,
        earlier=_rule_period(evidence.earlier),
        later=_rule_period(evidence.later),
        stability=RuleStability(
            min_month_labelled=MIN_MONTH_LABELLED, **evidence.stability.model_dump()
        ),
    )


def _rule_month(row: MonthlyBacktest) -> RuleMonth:
    return RuleMonth(
        **row.model_dump(), precision=row.precision, recall=row.recall, flag_rate=row.flag_rate
    )


def _rule_period(period: PeriodBacktest) -> RulePeriod:
    return RulePeriod(
        **period.model_dump(), precision=period.precision, recall=period.recall, lift=period.lift
    )


def query_caveats(result: QueryResult, *, reads_labels: bool) -> tuple[Caveat, ...]:
    caveats: list[Caveat] = []
    if result.truncated:
        caveats.append(
            Caveat(
                code="truncated",
                message=f"Only the first {result.row_count:,} rows were fetched (the row cap). "
                "The query matches more rows than are shown.",
            )
        )
    if reads_labels:
        caveats.append(Caveat(code="label_coverage", message=LABEL_COVERAGE))
    if result.row_count == 0:
        caveats.append(
            Caveat(
                code="zero_rows",
                message="No rows matched. Check the spelling and case of the values in the "
                "SQL: text comparisons are case-sensitive ('Debit', not 'debit').",
            )
        )
    return tuple(caveats)


def rule_caveats(backtest: BacktestResult) -> tuple[Caveat, ...]:
    labelled, unlabelled = backtest.flagged_labelled, backtest.flagged_unlabelled
    if unlabelled == 0:
        rest = "Precision and recall use those only."
    else:
        rest = (
            f"Precision and recall use those only; the other {unlabelled:,} "
            f"{'is' if unlabelled == 1 else 'are'} unknown, not legitimate."
        )
    caveats = [
        Caveat(
            code="label_coverage",
            message=f"{labelled:,} of the {_count(backtest.flagged, 'flagged transaction')} "
            f"{'has' if labelled == 1 else 'have'} a fraud label. {rest}",
        )
    ]
    if backtest.flagged == 0:
        caveats.append(
            Caveat(
                code="zero_rows",
                message="The rule flags no transactions. Check the spelling and case of the "
                "values in the clause: text comparisons are case-sensitive ('Debit', not "
                "'debit').",
            )
        )
    elif labelled == 0:
        caveats.append(
            Caveat(
                code="small_sample",
                message="Nothing the rule flags has a fraud label, so its precision cannot "
                "be measured.",
            )
        )
    elif labelled < SMALL_SAMPLE_LABELLED:
        caveats.append(
            Caveat(
                code="small_sample",
                message=f"Precision rests on {_count(labelled, 'labelled transaction')}, "
                f"fewer than {SMALL_SAMPLE_LABELLED}: too few to rely on.",
            )
        )
    flag_rate = backtest.flag_rate
    if flag_rate is not None and flag_rate > HIGH_FLAG_RATE:
        caveats.append(
            Caveat(
                code="high_flag_rate",
                message=f"The rule flags {flag_rate:.1%} of all transactions, over "
                f"{HIGH_FLAG_RATE:.0%}: it would stop many legitimate payments.",
            )
        )
    return tuple(caveats)


def evidence_caveats(evidence: RuleEvidence) -> tuple[Caveat, ...]:
    """The caveats that need the monthly rows: a weak month, a drop in the later period."""
    caveats: list[Caveat] = []
    overall = evidence.overall.precision
    weak = [
        row
        for row in evidence.months
        if row.flagged_labelled >= MIN_MONTH_LABELLED
        and _well_below(row.precision, row.flagged_labelled, overall)
    ]
    if weak and overall is not None:
        months = "; ".join(
            f"{row.month} ({row.true_positives:,} of {row.flagged_labelled:,} labelled "
            "flagged were fraud)"
            for row in weak
        )
        caveats.append(
            Caveat(
                code="weak_month",
                message=f"Precision is below half the rule's overall {overall:.2%} in "
                f"{_count(len(weak), 'month')}: {months}. The overall figure does not "
                "describe those months.",
            )
        )
    earlier, later = evidence.earlier, evidence.later
    if (
        earlier.precision is not None
        and min(earlier.flagged_labelled, later.flagged_labelled) >= SMALL_SAMPLE_LABELLED
        and _well_below(later.precision, later.flagged_labelled, earlier.precision)
    ):
        caveats.append(
            Caveat(
                code="period_drop",
                message=f"Precision in the later period ({_period_span(later)}: "
                f"{later.true_positives:,} of {later.flagged_labelled:,} labelled flagged "
                f"were fraud) is below half that of the earlier period "
                f"({_period_span(earlier)}: {earlier.true_positives:,} of "
                f"{earlier.flagged_labelled:,}). The rule did worse in the most recent months.",
            )
        )
    return tuple(caveats)


def _well_below(precision: float | None, labelled: int, reference: float | None) -> bool:
    """Under `WEAK_PRECISION_RATIO` of `reference`, on enough rows for that to mean something."""
    if precision is None or reference is None:
        return False
    return (
        reference * labelled >= MIN_EXPECTED_FRAUD
        and precision < WEAK_PRECISION_RATIO * reference
    )


def _period_span(period: RulePeriod | PeriodBacktest) -> str:
    if period.first_month is None or period.last_month is None:
        return "no month of the data"
    if period.first_month == period.last_month:
        return period.first_month
    return f"{period.first_month} to {period.last_month}"


def scan_table(scan: ScanResult, *, top: int = explore.DEFAULT_TOP) -> TableResult:
    """The first `top` listed segments as an ordinary result table."""
    rows: tuple[tuple[Cell, ...], ...] = tuple(
        (row.title, row.segment, row.labelled, row.fraud, row.fraud_rate, row.lift,
         row.share_of_fraud)
        for row in scan.rows[:top]
    )  # fmt: skip
    return present_table(
        QueryResult(
            columns=SCAN_COLUMNS, rows=rows, truncated=False, elapsed_seconds=scan.elapsed_seconds
        )
    )


def scan_caveats(scan: ScanResult) -> tuple[Caveat, ...]:
    caveats = [Caveat(code="label_coverage", message=LABEL_COVERAGE)]
    if scan.rows and scan.rows[0].fraud == scan.total_fraud:
        caveats.append(Caveat(code="single_segment", message=SINGLE_SEGMENT))
    return tuple(caveats)


def render_scan(scan: ScanResult, *, top: int = explore.DEFAULT_TOP) -> str:
    """The scan as plain text, without a turn around it: headline, table, notes, caveats."""
    lines = [explore.headline(scan), "", *_table_lines(scan_table(scan, top=top), top)]
    lines += ["", "Notes:", *(f"  - {safe_text(note)}" for note in explore.notes(scan, top=top))]
    lines += ["", "Caveats:"]
    lines += [f"  - [{caveat.code}] {caveat.message}" for caveat in scan_caveats(scan)]
    return "\n".join(lines)


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}{'' if number == 1 else 's'}"


def build_result(state: TurnState, meta: TurnMeta) -> TurnResult:
    """The reply for the end state of a turn: exactly one outcome."""
    failure, action, result = state.failure, state.action, state.result
    evidence = state.evidence
    refused = state.refused
    if failure is not None and failure.refusal is not None:
        refused = (*refused, failure.refusal)
    write_attempts = state.write_attempts + (failure is not None and failure.write_attempt)

    def reply(
        outcome: Outcome,
        answer: str,
        *,
        by_model: bool = False,
        error: TurnError | None = None,
        sql: str | None = None,
        where_clause: str | None = None,
        table: TableResult | None = None,
        rule: RuleCard | None = None,
        explanation: str | None = None,
        assumptions: tuple[str, ...] = (),
        suggestions: tuple[str, ...] = (),
        caveats: tuple[Caveat, ...] = (),
        notes: tuple[str, ...] = (),
    ) -> TurnResult:
        return TurnResult(
            outcome=outcome,
            status=status_of(outcome),
            error=error,
            answer=answer,
            answer_source="model" if by_model else "code",
            tool=tool_name(action) if action is not None else None,
            refused=refused,
            write_attempts=write_attempts,
            sql=sql,
            where_clause=where_clause,
            table=table,
            rule=rule,
            explanation=explanation,
            assumptions=assumptions,
            suggestions=suggestions,
            caveats=caveats,
            meta=meta,
            notes=notes,
        )

    if failure is not None:
        return reply(
            outcome_of(failure.kind),
            failure.message,
            error=TurnError(
                kind=failure.kind, message=failure.message, detail=state.failure_detail
            ),
        )
    if isinstance(action, AskClarification):
        return reply(
            "clarification", action.question, by_model=True, suggestions=action.suggestions
        )
    if isinstance(action, Decline):
        return reply("cannot_answer", action.reason, by_model=True)
    if (
        isinstance(action, RunSql)
        and isinstance(result, QueryResult)
        and state.approved_sql is not None
    ):
        table = present_table(result)
        return reply(
            "answered",
            _table_summary(table),
            sql=state.approved_sql,
            table=table,
            explanation=action.explanation or None,
            assumptions=action.assumptions,
            caveats=query_caveats(result, reads_labels=state.reads_labels),
        )
    if (
        isinstance(action, ProposeRule)
        and evidence is not None
        and state.approved_clause is not None
    ):
        return reply(
            "rule_proposed",
            _rule_summary(evidence.overall),
            where_clause=state.approved_clause,
            rule=rule_card(evidence),
            explanation=action.explanation or None,
            assumptions=action.assumptions,
            caveats=rule_caveats(evidence.overall) + evidence_caveats(evidence),
        )
    if isinstance(action, ProfileFraud) and isinstance(result, ScanResult):
        return reply(
            "answered",
            explore.headline(result),
            sql=result.statements[0],
            table=scan_table(result),
            suggestions=explore.follow_ups(result),
            caveats=scan_caveats(result),
            notes=explore.notes(result),
        )
    # No route leads here; a turn that does is a fault of the graph, not of the user.
    message = "The turn ended without a result."
    return reply("failed", message, error=TurnError(kind="internal", message=message))


def _rule_summary(result: BacktestResult) -> str:
    flags = (
        f"The rule flags {result.flagged:,} of "
        f"{_count(result.total_transactions, 'transaction')}."
    )
    if result.flagged == 0:
        return flags
    if result.flagged_labelled == 0:
        return f"{flags} None of them has a fraud label."

    def are(number: int) -> str:
        return f"{number:,} {'is' if number == 1 else 'are'}"

    return (
        f"{flags} Of the {result.flagged_labelled:,} with a fraud label, "
        f"{are(result.true_positives)} fraud and {are(result.false_positives)} legitimate."
    )


def render_text(result: TurnResult, *, max_rows: int = DEFAULT_PRINTED_ROWS) -> str:
    """The reply as plain text for a terminal."""
    lines: list[str] = []
    match result.outcome:
        case "clarification":
            lines.append(f"The assistant asks: {safe_text(result.answer)}")
        case "cannot_answer" if result.answer_source == "model":
            lines.append(f"The assistant declined: {safe_text(result.answer)}")
        case "rejected" | "failed":
            kind = result.error.kind if result.error else "internal"
            lines.append(f"{result.outcome.capitalize()} ({kind}): {safe_text(result.answer)}")
            if result.error is not None and result.error.detail:
                lines.append(f"SQLite said: {safe_text(result.error.detail)}")
        case _:
            lines.append(result.answer)
    if result.suggestions:
        lines.append("You could ask:")
        lines += [f"  - {safe_text(suggestion)}" for suggestion in result.suggestions]
    if result.sql is not None:
        lines += ["", "SQL that ran:", *_block(result.sql)]
    if result.table is not None:
        lines += ["", *_table_lines(result.table, max_rows)]
    if result.where_clause is not None and result.rule is not None:
        lines += ["", "Rule (a WHERE clause over transactions):", *_block(result.where_clause)]
        lines += ["", *_rule_lines(result.rule)]
    if result.notes:
        lines += ["", "Notes:", *(f"  - {safe_text(note)}" for note in result.notes)]
    if result.explanation:
        lines += ["", f"Model's explanation (not checked): {safe_text(result.explanation)}"]
    if result.assumptions:
        lines += ["", "Model's assumptions:"]
        lines += [f"  - {safe_text(assumption)}" for assumption in result.assumptions]
    if result.caveats:
        lines += ["", "Caveats:"]
        lines += [f"  - [{caveat.code}] {caveat.message}" for caveat in result.caveats]
    meta = result.meta
    lines += [
        "",
        f"[{result.outcome} | tool: {result.tool or 'none'}{_refused_note(result)} | "
        f"{safe_text(meta.model_id)} | {meta.model_calls} model request(s) | "
        f"{meta.input_tokens:,} in, {meta.output_tokens:,} out tokens | "
        f"{meta.elapsed_ms / 1000:.1f} s]",
    ]
    return "\n".join(lines)


def _refused_note(result: TurnResult) -> str:
    if not result.refused:
        return ""
    return " | refused: " + ", ".join(str(code) for code in result.refused)


def format_cell(value: Cell, kind: ColumnFormat = "plain") -> str:
    """One value as text that is safe to print."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        shown = value[:_SHOWN_BLOB_BYTES].hex()
        more = "…" if len(value) > _SHOWN_BLOB_BYTES else ""
        return f"x'{shown}{more}' ({len(value):,} bytes)"
    if isinstance(value, str):
        return safe_text(value, MAX_CELL_CHARACTERS)
    if kind == "usd_cents":
        return f"{'-' if value < 0 else ''}${abs(value) / 100:,.2f}"
    if kind == "fraction":
        return f"{value:.6g} ({value:.3%})"
    if isinstance(value, int):
        return str(value) if kind == "identifier" else f"{value:,}"
    if value == 0 or abs(value) >= 0.01:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.6g}"


def safe_text(text: str, limit: int | None = None) -> str:
    """`text` on one line with control characters escaped, cut to `limit` characters."""
    cleaned = "".join(
        character if character.isprintable() else character.encode("unicode_escape").decode()
        for character in text
    )
    if limit is not None and len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def _table_summary(table: TableResult) -> str:
    if table.row_count == 1 and len(table.columns) == 1:
        column = table.columns[0]
        return f"{safe_text(column.name, MAX_CELL_CHARACTERS)}: " + format_cell(
            table.rows[0][0], column.format
        )
    if table.row_count == 0:
        return "The query returned no rows."
    rows = "1 row" if table.row_count == 1 else f"{table.row_count:,} rows"
    return f"The query returned {'the first ' if table.truncated else ''}{rows}."


def _block(sql: str) -> list[str]:
    return [f"    {safe_text(line)}" for line in sql.splitlines()]


def _table_lines(table: TableResult, max_rows: int) -> list[str]:
    shown = table.rows[:max_rows]
    header = [safe_text(column.name, MAX_CELL_CHARACTERS) for column in table.columns]
    cells = [
        [format_cell(value, column.format) for value, column in zip(row, table.columns)]
        for row in shown
    ]
    widths = [
        max(len(header[index]), *(len(row[index]) for row in cells)) if cells else len(header[index])
        for index in range(len(header))
    ]
    numeric = [
        bool(shown) and all(isinstance(row[index], int | float | None) for row in shown)
        for index in range(len(header))
    ]

    # At least one column, then as many as fit the line.
    fitting, used = 0, 0
    for width in widths:
        used += width + (2 if fitting else 0)
        if fitting and used > MAX_TABLE_CHARACTERS:
            break
        fitting += 1

    def line(values: list[str]) -> str:
        return "  ".join(
            value.rjust(width) if right else value.ljust(width)
            for value, width, right in zip(values[:fitting], widths, numeric)
        ).rstrip()

    lines = [line(header), line(["-" * width for width in widths]), *map(line, cells)]
    if fitting < len(header):
        lines.append(
            f"{len(header) - fitting} more column(s) not shown: "
            + safe_text(", ".join(header[fitting:]), MAX_TABLE_CHARACTERS)
        )
    if table.row_count > len(shown):
        lines.append(f"Showing {len(shown):,} of {table.row_count:,} rows.")
    elif table.row_count == 0:
        lines.append("(no rows)")
    return lines


def _rule_lines(rule: RuleCard) -> list[str]:
    def ratio(value: float | None, undefined: str) -> str:
        return f"undefined ({undefined})" if value is None else format_cell(value, "fraction")

    rows = [
        (
            "Flagged",
            f"{rule.flagged:,} of {rule.total_transactions:,} transactions"
            + ("" if rule.flag_rate is None else f" ({rule.flag_rate:.3%})"),
        ),
        (
            "Labelled",
            f"{rule.flagged_labelled:,} ({rule.flagged_unlabelled:,} flagged "
            f"{'has' if rule.flagged_unlabelled == 1 else 'have'} no label)",
        ),
        ("Fraud caught", f"{rule.true_positives:,} of {rule.total_fraud:,} labelled fraud"),
        ("False positives", f"{rule.false_positives:,}"),
        ("Precision", ratio(rule.precision, "nothing flagged is labelled")),
        ("Recall", ratio(rule.recall, "the data has no labelled fraud")),
        ("Lift", _lift_text(rule)),
        ("Amount flagged", format_cell(rule.flagged_amount_cents, "usd_cents")),
        ("Fraud amount caught", format_cell(rule.fraud_amount_caught_cents, "usd_cents")),
        ("False positive amount", format_cell(rule.false_positive_amount_cents, "usd_cents")),
    ]
    lines = ["Backtest against the fraud labels (labelled transactions only):"] + [
        f"  {label:<22}{value}" for label, value in rows
    ]
    if rule.months:
        lines += ["", *_month_lines(rule)]
    return lines


def _lift_text(rule: RuleCard) -> str:
    if rule.lift is None or rule.base_fraud_rate is None:
        return "undefined (no precision or no base fraud rate to compare)"
    return (
        f"{rule.lift:,.1f} times the base fraud rate "
        f"({rule.base_fraud_rate:.3%} of labelled transactions are fraud)"
    )


def _month_lines(rule: RuleCard) -> list[str]:
    """The month table, the weakest month, the two periods and what the evidence is."""
    header = ("Month", "Flagged", "Labelled", "Fraud caught", "False positives", "Precision")
    cells = [
        (
            row.month,
            f"{row.flagged:,}",
            f"{row.flagged_labelled:,}",
            f"{row.true_positives:,}",
            f"{row.false_positives:,}",
            "-" if row.precision is None else f"{row.precision:.2%}",
        )
        for row in rule.months
    ]
    widths = [max(len(line[index]) for line in (header, *cells)) for index in range(len(header))]

    def line(values: tuple[str, ...]) -> str:
        first, *rest = values
        return "  " + "  ".join(
            [first.ljust(widths[0]), *(value.rjust(width) for value, width in zip(rest, widths[1:]))]
        )

    stability, minimum = rule.stability, rule.stability.min_month_labelled
    weakest = next((row for row in rule.months if row.month == rule.weakest_month), None)
    if weakest is None or weakest.precision is None:
        summary = [
            ("Weakest month", f"none: no month has {minimum} or more labelled flagged transactions"),
        ]
    else:
        low, high = stability.min_precision, stability.max_precision
        spread = "" if low is None or high is None else f"{low:.2%} to {high:.2%} "
        summary = [
            (
                "Weakest month",
                f"{weakest.month}: {weakest.true_positives:,} of {weakest.flagged_labelled:,} "
                f"labelled flagged were fraud ({weakest.precision:.2%})",
            ),
            (
                "Monthly precision",
                f"{spread}over the {_count(stability.qualifying_months, 'month')} with "
                f"{minimum} or more labelled flagged, of {stability.months}",
            ),
        ]
    summary += [
        ("Earlier period", _period_text(rule.earlier)),
        ("Later period", _period_text(rule.later)),
    ]
    return [
        "Month by month, on the same history:",
        line(header),
        *map(line, cells),
        *(f"  {label:<22}{value}" for label, value in summary),
        *textwrap.wrap(EVIDENCE_NOTE, width=100, initial_indent="  ", subsequent_indent="  "),
    ]


def _period_text(period: RulePeriod) -> str:
    span = _period_span(period)
    if period.precision is None:
        return f"{span}: nothing flagged is labelled"
    lift = "undefined" if period.lift is None else f"{period.lift:,.1f}"
    return (
        f"{span}: {period.true_positives:,} of {period.flagged_labelled:,} labelled flagged "
        f"were fraud ({period.precision:.2%}); recall {period.true_positives:,} of "
        f"{period.total_fraud:,}; lift {lift}"
    )
