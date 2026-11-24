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
    "not_available",
    "internal",
]
# Why one model reply of a turn was refused: the shape check's kind or the
# guard's reason code. The two sets share no value. A code, never any text.
RefusalCode = ReasonCode | ShapeFailureKind
# In the order caveats are shown.
CaveatCode = Literal[
    "truncated", "label_coverage", "zero_rows", "small_sample", "high_flag_rate"
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


class RuleCard(BaseModel):
    """A rule's backtest. Every number comes from the backtest statement.

    A ratio is None when its denominator is zero.
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

    `explanation`, `assumptions` and `suggestions` are always the model's
    text. `answer` is the line to show first; `answer_source` says whether
    code or the model wrote it.
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
    sql: str | None  # the statement that ran
    where_clause: str | None  # the rule clause that was backtested
    table: TableResult | None
    rule: RuleCard | None
    explanation: str | None
    assumptions: tuple[str, ...]
    suggestions: tuple[str, ...]
    caveats: tuple[Caveat, ...]
    meta: TurnMeta
