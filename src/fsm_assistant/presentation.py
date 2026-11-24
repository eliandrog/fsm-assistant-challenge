"""Result presentation.

Responsibility: turn the end state of a turn into what the user is shown
(docs/DECISIONS.md, D-24). `build_result` chooses the one outcome, a format
hint per column, and caveats from a closed list in a fixed order.
`render_text` is the terminal form of the same result.

Every figure here comes from a `QueryResult` or a `BacktestResult`, that is
from executed SQL. The model's explanation and assumptions are passed
through unchanged and labelled as the model's.
"""

import re
from collections.abc import Sequence

from fsm_assistant.db import Cell, QueryResult
from fsm_assistant.nl2sql import AskClarification, Decline, ProposeRule, RunSql, tool_name
from fsm_assistant.result import (
    Caveat,
    Column,
    ColumnFormat,
    ErrorKind,
    Outcome,
    RuleCard,
    Status,
    TableResult,
    TurnError,
    TurnMeta,
    TurnResult,
)
from fsm_assistant.rules.backtest import BacktestResult
from fsm_assistant.state import TurnState

# A rule's precision resting on fewer labelled rows than this gets a caveat.
SMALL_SAMPLE_LABELLED = 30
# A rule flagging more than this share of all transactions gets a caveat.
HIGH_FLAG_RATE = 0.05
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

SCAN_NOT_AVAILABLE = (
    "The fraud-concentration scan is not available yet. Ask a specific question instead."
)
SCAN_SUGGESTIONS = (
    "What is the fraud rate by merchant city and state?",
    "What is the fraud rate by hour of day?",
)


def status_of(outcome: Outcome) -> Status:
    return _STATUS[outcome]


def outcome_of(kind: ErrorKind) -> Outcome:
    """The outcome a turn ends in when its last failure is of `kind`."""
    if kind == "sql_rejected":
        return "rejected"
    return "cannot_answer" if kind == "not_available" else "failed"


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


def rule_card(backtest: BacktestResult) -> RuleCard:
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
        caveats.append(
            Caveat(
                code="label_coverage",
                message="This result uses fraud labels, which only about two thirds of "
                "transactions have. An unlabelled transaction is unknown, not legitimate.",
            )
        )
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


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}{'' if number == 1 else 's'}"


def build_result(state: TurnState, meta: TurnMeta) -> TurnResult:
    """The reply for the end state of a turn: exactly one outcome."""
    failure, action, result = state.failure, state.action, state.result
    refused = state.refused
    if failure is not None and failure.refusal is not None:
        refused = (*refused, failure.refusal)

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
    ) -> TurnResult:
        return TurnResult(
            outcome=outcome,
            status=status_of(outcome),
            error=error,
            answer=answer,
            answer_source="model" if by_model else "code",
            tool=tool_name(action) if action is not None else None,
            refused=refused,
            sql=sql,
            where_clause=where_clause,
            table=table,
            rule=rule,
            explanation=explanation,
            assumptions=assumptions,
            suggestions=suggestions,
            caveats=caveats,
            meta=meta,
        )

    if failure is not None:
        return reply(
            outcome_of(failure.kind),
            failure.message,
            error=TurnError(
                kind=failure.kind, message=failure.message, detail=state.failure_detail
            ),
            suggestions=SCAN_SUGGESTIONS if failure.kind == "not_available" else (),
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
        and isinstance(result, BacktestResult)
        and state.approved_clause is not None
    ):
        return reply(
            "rule_proposed",
            _rule_summary(result),
            where_clause=state.approved_clause,
            rule=rule_card(result),
            explanation=action.explanation or None,
            assumptions=action.assumptions,
            caveats=rule_caveats(result),
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
        ("Amount flagged", format_cell(rule.flagged_amount_cents, "usd_cents")),
        ("Fraud amount caught", format_cell(rule.fraud_amount_caught_cents, "usd_cents")),
        ("False positive amount", format_cell(rule.false_positive_amount_cents, "usd_cents")),
    ]
    return ["Backtest against the fraud labels (labelled transactions only):"] + [
        f"  {label:<22}{value}" for label, value in rows
    ]
