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

Containment. The clause is untrusted text placed inside our template.
Writes are already impossible (`db` enforces read-only in the engine); what
this module adds is that the clause cannot change *what is measured* by
breaking out of its parentheses. `check_clause` rejects statement
separators, comments, bind parameters and any parenthesis that would close
the template's own, all outside quoted text. Whether the tables and columns
a clause names are allowed is not decided here: that is `sql_guard`'s job
(not built yet), which is expected to run before `backtest`.
"""

from dataclasses import dataclass
from pathlib import Path

from fsm_assistant import db

# The clause is wrapped in parentheses and followed by a newline. With the
# checks in `check_clause` this makes it one boolean expression: it cannot
# add a UNION, GROUP BY or LIMIT to the CTE, or end the statement early.
#
# The label join is the cast form prescribed by D-10, which uses the index on
# fraud_labels.transaction_id; the uncast LEFT JOIN compares every flagged
# row with every label. One row comes back even when nothing is flagged.
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
LEFT JOIN fraud_labels f ON f.transaction_id = CAST(flagged.id AS TEXT)
"""

# The clause exactly as it would be written after WHERE. Compiled (EXPLAIN
# does not run it) to reject text that is only valid inside our parentheses,
# such as a bare `SELECT 1`, which there would become a scalar subquery.
_BARE_CHECK = "EXPLAIN SELECT 1 FROM transactions WHERE {clause}\n"

# Opening quote character -> closing one, as SQLite's tokenizer pairs them:
# strings, and the three ways to quote an identifier.
_QUOTES = {"'": "'", '"': '"', "`": "`", "[": "]"}

# Characters that start a bind parameter in SQLite. Nothing is ever bound to
# a clause, and a `$name(...)` parameter can hide parentheses from a scan.
_PARAMETER_CHARACTERS = frozenset("?:@$#")


class RuleClauseError(Exception):
    """The clause cannot be backtested.

    `reason` says why, in words that can be shown or handed back to the
    model. `sqlite_message` is SQLite's own message, verbatim, when SQLite
    rejected the clause (syntax error, unknown column, ...), else `None`.
    """

    def __init__(self, reason: str, sqlite_message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.sqlite_message = sqlite_message


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """What a rule would have flagged, measured against the fraud labels.

    Counts are transactions; amounts are sums of `amount_usd_cents` (cents,
    and net of negative amounts, which exist in the data).
    """

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


def backtest(clause: str, *, db_path: Path, timeout_seconds: float) -> BacktestResult:
    """Measure the rule `clause` over every transaction in the database.

    Raises `RuleClauseError` when the clause is rejected by `check_clause`
    or by SQLite (then with `sqlite_message`). `db.QueryTimeout` and the
    other `db` errors propagate unchanged.
    """
    clause = check_clause(clause)

    def run(template: str) -> db.QueryResult:
        try:
            return db.run_query(
                template.format(clause=clause),
                db_path=db_path,
                row_cap=1,
                timeout_seconds=timeout_seconds,
            )
        except db.QueryError as error:
            raise RuleClauseError(
                f"SQLite rejected the clause: {error.sqlite_message}",
                sqlite_message=error.sqlite_message,
            ) from error

    run(_BARE_CHECK)
    result = run(_TEMPLATE)
    # By column name, so the template and the result type cannot drift apart.
    return BacktestResult(
        clause=clause,
        elapsed_seconds=result.elapsed_seconds,
        **dict(zip(result.columns, result.rows[0], strict=True)),
    )


def check_clause(clause: str) -> str:
    """Return `clause` stripped, or raise `RuleClauseError` if it could escape the template.

    A lexical scan that follows SQLite's quoting rules, so a `;` or `)`
    inside a string literal is left alone. It does not check that the clause
    is valid SQL (SQLite does that when the template is compiled) or which
    tables it reads (`sql_guard`'s job).
    """
    text = clause.strip()
    if not text:
        raise RuleClauseError("The clause is empty.")
    if "\x00" in text:
        raise RuleClauseError("The clause contains a NUL character.")

    depth = 0
    position = 0
    while position < len(text):
        character = text[position]
        if character in _QUOTES:
            position = _end_of_quoted(text, position)
            continue
        if character == ";":
            raise RuleClauseError("The clause contains ';': it must be one expression, not a statement.")
        if text.startswith(("--", "/*"), position):
            raise RuleClauseError(
                "The clause contains a SQL comment ('--' or '/*'), which could hide the rest "
                "of the query. Write a space between two minus signs."
            )
        if character in _PARAMETER_CHARACTERS:
            raise RuleClauseError(f"The clause contains {character!r}: bind parameters are not allowed.")
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise RuleClauseError("The clause has a ')' with no matching '('.")
        position += 1
    if depth:
        raise RuleClauseError("The clause has a '(' with no matching ')'.")
    return text


def _end_of_quoted(text: str, start: int) -> int:
    """Index just past the quoted text that opens at `start`."""
    close = _QUOTES[text[start]]
    position = start + 1
    while True:
        position = text.find(close, position)
        if position == -1:
            raise RuleClauseError(f"The clause has an unterminated {text[start]} quote.")
        position += 1
        # A doubled quote is an escaped quote, except in [bracket] names.
        if close != "]" and text.startswith(close, position):
            position += 1
            continue
        return position


def _ratio(numerator: int, denominator: int) -> float | None:
    """A fraction, or None when it is undefined."""
    return numerator / denominator if denominator else None
