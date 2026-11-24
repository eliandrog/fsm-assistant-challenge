"""The state of one turn.

Responsibility: the one object the graph's nodes read and update
(docs/DECISIONS.md, D-23). Three kinds of field:

- kept across the turns of a conversation: `turn`, `history`;
- belonging to one turn, and reset by its first node: everything from
  `attempt` to `outcome`;
- never saved: `result`, `evidence` and `response`, which hold rows, scan
  segments or figures computed from them, and `failure_detail`, SQLite's
  message for a statement that failed while it
  ran, which can quote a value. They are marked `UntrackedValue`, so the
  checkpointer never receives them.

Nodes return updates through `update`, which refuses a name that is not a
field.
"""

from typing import Annotated

from langgraph.channels import UntrackedValue
from pydantic import BaseModel, ConfigDict

from fsm_assistant.db import QueryResult
from fsm_assistant.explore import ScanResult
from fsm_assistant.llm import ModelReply
from fsm_assistant.nl2sql import (
    Action,
    AskClarification,
    Decline,
    ProfileFraud,
    ProposeRule,
    RunSql,
)
from fsm_assistant.prompt import PreviousTurn, Repair
from fsm_assistant.result import ErrorKind, Outcome, RefusalCode, TurnResult
from fsm_assistant.rules.backtest import BacktestResult
from fsm_assistant.rules.proposal import RuleEvidence
from fsm_assistant.sql_guard import ReasonCode

# One attempt and one repair.
MAX_MODEL_CALLS = 2
# How many past turns the next prompt is told about.
HISTORY_LENGTH = 1


class TurnInput(BaseModel):
    """All an invocation may write: the saved state cannot be overwritten from outside."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str


class Failure(BaseModel):
    """Why a step of the turn did not succeed. `repair` is None when asking again cannot help.

    It is saved with the conversation and its repair is sent to the model, so
    no field may hold text that came out of the database.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ErrorKind
    message: str
    repair: Repair | None = None
    refusal: RefusalCode | None = None  # set when the shape check or the guard refused the reply


class TurnState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = ""

    turn: int = 0
    history: tuple[PreviousTurn, ...] = ()

    attempt: int = 0  # model replies obtained this turn
    model_calls: int = 0  # requests made this turn
    input_tokens: int = 0
    output_tokens: int = 0
    raw_reply: ModelReply | None = None
    action: Action | None = None
    approved_sql: str | None = None  # written only by guard_sql
    approved_clause: str | None = None  # written only by guard_rule
    reads_labels: bool = False  # the approved statement reads fraud_labels
    failure: Failure | None = None
    refused: tuple[RefusalCode, ...] = ()  # the refusals of earlier attempts of this turn
    outcome: Outcome | None = None

    result: Annotated[QueryResult | BacktestResult | ScanResult | None, UntrackedValue] = None
    # A rule's backtest with its monthly rows; `result` is then its overall part.
    evidence: Annotated[RuleEvidence | None, UntrackedValue] = None
    failure_detail: Annotated[str | None, UntrackedValue] = None
    response: Annotated[TurnResult | None, UntrackedValue] = None


# Every project type a saved state can hold, for the checkpointer's serializer.
SAVED_TYPES: tuple[type, ...] = (
    TurnInput,
    ReasonCode,
    PreviousTurn,
    Repair,
    Failure,
    ModelReply,
    RunSql,
    ProposeRule,
    ProfileFraud,
    AskClarification,
    Decline,
)

PER_TURN_RESET: dict[str, object] = {
    "attempt": 0,
    "model_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "raw_reply": None,
    "action": None,
    "approved_sql": None,
    "approved_clause": None,
    "reads_labels": False,
    "failure": None,
    "refused": (),
    "outcome": None,
    "result": None,
    "evidence": None,
    "failure_detail": None,
    "response": None,
}


def update(**fields: object) -> dict[str, object]:
    """A node's update. LangGraph takes a dict; this checks its keys are state fields."""
    unknown = fields.keys() - TurnState.model_fields.keys()
    if unknown:
        raise KeyError(f"not a field of the turn state: {sorted(unknown)}")
    return fields
