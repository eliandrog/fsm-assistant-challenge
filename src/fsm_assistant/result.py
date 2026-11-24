"""What one turn returns.

Responsibility: the types an interface reads. A `TurnResult` is the whole
reply to one message: exactly one outcome, the SQL or rule clause that ran,
the rows or the backtest, the model's own text kept apart from what code
computed, and caveats chosen by code from a closed list.

Types only. `presentation` fills them in and `Assistant.run_turn` returns them.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from fsm_assistant.config import ToolMode
from fsm_assistant.db import Cell
from fsm_assistant.nl2sql import ShapeFailureKind, ToolName
from fsm_assistant.sql_guard import ReasonCode

Outcome = Literal[
    "answered", "rule_proposed", "clarification", "cannot_answer", "rejected", "failed"
]
Status = Literal["ok", "needs_clarification", "cannot_answer", "rejected", "error"]
ErrorKind = Literal[
    "sql_rejected",
    "sql_execution",
    "timeout",
    "invalid_input",
    "model_unavailable",
    "model_output",
    "internal",
]
# Why one model reply of a turn was refused: the shape check's kind or the
# guard's reason code. The two sets share no value. A code, never any text.
RefusalCode = ReasonCode | ShapeFailureKind
# In the order caveats are shown.
CaveatCode = Literal[
    "truncated",
    "label_coverage",
    "zero_rows",
    "small_sample",
    "high_flag_rate",
    "weak_month",
    "period_drop",
    "single_segment",
]
# How a column's values are meant to be shown. `usd_cents` values are integer
# cents; `fraction` values lie between 0 and 1; `identifier` integers are
# shown without digit grouping.
ColumnFormat = Literal["plain", "usd_cents", "fraction", "identifier"]
# Who wrote `TurnResult.answer`: code, from executed SQL, or the model.
AnswerSource = Literal["code", "model"]


class Caveat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: CaveatCode
    message: str


class TurnError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ErrorKind
    message: str  # written by code and free of data: it is what memory, the model and a trace see
    # SQLite's own words when an approved statement failed while it ran. They
    # can quote a value from the data, so they exist only here, for the user.
    detail: str | None = None


class Column(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    format: ColumnFormat


class TableResult(BaseModel):
    """The rows a query returned, as SQLite returned them, with a format hint per column."""

    model_config = ConfigDict(frozen=True, extra="forbid", ser_json_bytes="base64")

    columns: tuple[Column, ...]
    rows: tuple[tuple[Cell, ...], ...]
    row_count: int
    truncated: bool  # more rows existed than the row cap allowed


class RuleMonth(BaseModel):
    """One calendar month of a rule's backtest. The totals and ratios are the month's own."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    month: str  # YYYY-MM
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


class RulePeriod(BaseModel):
    """The months from `first_month` to `last_month`, summed. Not a held-out test."""

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
    precision: float | None
    recall: float | None  # of the period's own labelled fraud
    lift: float | None  # precision over the period's own base fraud rate


class RuleStability(BaseModel):
    """How much monthly precision moves, over the months that qualify.

    A month qualifies when at least `min_month_labelled` of the transactions
    the rule flags in it are labelled.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_month_labelled: int
    months: int
    months_flagged: int
    months_with_labelled_flags: int
    qualifying_months: int
    min_precision: float | None
    max_precision: float | None


class RuleCard(BaseModel):
    """A rule's backtest. Every number comes from the two backtest statements.

    The first fifteen fields are the rule over the whole history. The rest
    is the same measurement month by month: a stability check on the history
    the rule was written from, not a test on unseen data. A ratio is None
    when its denominator is zero. Counts and rates only: no row of the data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    flagged: int
    flagged_labelled: int
    flagged_unlabelled: int
    true_positives: int
    false_positives: int
    false_negatives: int
    total_transactions: int
    total_labelled: int
    total_fraud: int
    precision: float | None
    recall: float | None
    flag_rate: float | None
    flagged_amount_cents: int
    fraud_amount_caught_cents: int
    false_positive_amount_cents: int
    base_fraud_rate: float | None  # labelled fraud / labelled transactions
    lift: float | None  # precision / base_fraud_rate
    months: tuple[RuleMonth, ...]  # every month that has transactions, in order
    weakest_month: str | None  # the qualifying month with the lowest precision
    earlier: RulePeriod
    later: RulePeriod
    stability: RuleStability


class TurnMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: str
    turn: int
    model_id: str
    prompt_version: str
    tool_mode: ToolMode
    attempts: int  # model replies obtained and judged: 0, 1 or 2
    model_calls: int  # requests made, whether or not a reply came back
    input_tokens: int
    output_tokens: int
    elapsed_ms: int


class TurnResult(BaseModel):
    """The reply to one message.

    `explanation` and `assumptions` are always the model's text, and so are
    `suggestions`, except after the fraud scan, whose follow-up questions
    code writes. `notes` are written by code. `answer` is the line to show
    first; `answer_source` says whether code or the model wrote it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Outcome
    status: Status
    error: TurnError | None
    answer: str
    answer_source: AnswerSource
    # The tool the model chose in the reply that decided the turn; None when
    # no usable reply was obtained.
    tool: ToolName | None
    # One code per model reply of this turn that the shape check or the guard
    # refused, in order, a reply that was then repaired included.
    refused: tuple[RefusalCode, ...]
    sql: str | None  # the statement that ran; for the scan, its profile statement
    where_clause: str | None  # the rule clause that was backtested
    table: TableResult | None
    rule: RuleCard | None
    explanation: str | None
    assumptions: tuple[str, ...]
    suggestions: tuple[str, ...]
    caveats: tuple[Caveat, ...]
    meta: TurnMeta
    # What the fraud scan found besides its table: scope, floor, residual check.
    notes: tuple[str, ...] = ()
