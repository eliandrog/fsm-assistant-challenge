"""What a turn did, as events.

Responsibility: the one description of a turn that the terminal trace
(`--trace`) and a hosted trace are both made from, so the two cannot disagree
(docs/DECISIONS.md, D-27). A `StepEvent` is one node's step, a `TurnEvent`
the finished turn. They stay in the process: nothing here writes a file.

An event is built from a node's update and holds nothing read from the
database: rows appear as a count and column names, a backtest and its
evidence by month as the time the two statements took, the fraud scan as the
number of segments it listed, the number of statements it ran and the time
they took, and a failure as its kind, its code and the sentence code wrote for
it. A scan's segments, figures, headline, notes and suggested questions are
never read here, and of its statements only the profile is, through
`TurnResult.sql`: code wrote it and it holds no value of the data, while the
statements after it name segments. SQLite's own words for a statement that failed while it ran
(`failure_detail`, `TurnError.detail`) are never read here. No event carries
a wall-clock time; durations come from a monotonic clock.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from fsm_assistant.config import ToolMode
from fsm_assistant.db import QueryResult
from fsm_assistant.explore import ScanResult
from fsm_assistant.llm import ModelCall, ModelReply
from fsm_assistant.nl2sql import (
    AskClarification,
    Decline,
    ProfileFraud,
    ProposeRule,
    RunSql,
    ToolName,
    tool_name,
)
from fsm_assistant.presentation import safe_text
from fsm_assistant.result import CaveatCode, ErrorKind, Outcome, Status, TurnResult
from fsm_assistant.rules.backtest import BacktestResult
from fsm_assistant.rules.proposal import RuleEvidence
from fsm_assistant.state import Failure

Command = Literal["ask", "chat", "eval", "other"]
NodeName = Literal[
    "begin_turn",
    "call_model",
    "check_intent",
    "guard_sql",
    "execute_sql",
    "guard_rule",
    "backtest_rule",
    "run_scan",
    "present",
    "remember",
]
GuardVerdict = Literal["accepted", "rejected"]

# A question longer than this is cut in an event; `question_chars` keeps its length.
QUESTION_LIMIT = 2000


class Label(BaseModel):
    """Who ran the turn: the command, and in an evaluation run the case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: Command = "other"
    case_id: str | None = None


class StepEvent(BaseModel):
    """One node's step. A field is None when the step did not decide it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["step"] = "step"
    command: Command
    case_id: str | None = None
    conversation_id: str
    turn: int
    seq: int  # the step's place in the turn, from 1
    node: NodeName
    duration_ms: int  # from the end of the step before, or the start of the turn
    attempt: int  # model replies obtained so far in the turn
    model_calls: int  # requests made so far in the turn

    model_id: str | None = None
    model_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    response_id: str | None = None
    stop_reason: str | None = None
    reply_tools: tuple[str, ...] | None = None  # the tools the reply called, as it named them
    system_prompt_sha256: str | None = None

    tool: ToolName | None = None  # the tool chosen, once the reply's shape was accepted
    guard: GuardVerdict | None = None
    sql: str | None = None  # the statement the guard accepted
    where_clause: str | None = None  # the rule clause the guard accepted
    reads_labels: bool | None = None

    row_count: int | None = None
    columns: tuple[str, ...] | None = None
    truncated: bool | None = None
    query_ms: int | None = None
    scan_segments: int | None = None  # how many segments the fraud scan listed; never which
    scan_statements: int | None = None  # how many statements the scan ran; never their text

    error_kind: ErrorKind | None = None
    reason_code: str | None = None  # the shape check's or the guard's code for a refused reply
    repairable: bool | None = None
    failure_message: str | None = None  # the sentence code wrote; never SQLite's run-time words

    outcome: Outcome | None = None
    status: Status | None = None
    caveats: tuple[CaveatCode, ...] | None = None
    remembered: str | None = None  # the kind of record kept for a follow-up


class TurnEvent(BaseModel):
    """One finished turn, as a trace shows it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["turn"] = "turn"
    command: Command
    case_id: str | None = None
    conversation_id: str
    turn: int
    question: str
    question_chars: int
    outcome: Outcome
    status: Status
    error_kind: ErrorKind | None = None
    tool: ToolName | None = None
    refused: tuple[str, ...]  # one code per reply the shape check or the guard refused
    sql: str | None = None
    where_clause: str | None = None
    row_count: int | None = None
    columns: tuple[str, ...] | None = None
    truncated: bool | None = None
    reads_labels: bool
    caveats: tuple[CaveatCode, ...]
    model_id: str
    prompt_version: str
    system_prompt_sha256: str | None = None  # absent when no request was made
    tool_mode: ToolMode
    attempts: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    response_ids: tuple[str, ...]
    steps: int
    elapsed_ms: int


class TurnRecord(BaseModel):
    """A finished turn with its steps and its model requests: what a trace is built from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn: TurnEvent
    steps: tuple[StepEvent, ...]
    calls: tuple[ModelCall, ...]


def step_event(
    node: NodeName,
    fields: dict[str, object],
    *,
    label: Label = Label(),
    conversation_id: str = "",
    turn: int = 0,
    seq: int = 1,
    duration_ms: int = 0,
    attempt: int = 0,
    model_calls: int = 0,
    call: ModelCall | None = None,
) -> StepEvent:
    """Describe the update `fields` that `node` returned. Reads no row and no cell."""
    values: dict[str, object] = {}
    reply = fields.get("raw_reply")
    if isinstance(reply, ModelReply):
        values.update(
            model_id=reply.model_id,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            response_id=reply.response_id,
            stop_reason=reply.stop_reason,
            reply_tools=tuple(tool.name for tool in reply.tool_calls),
        )
    if call is not None:
        values.update(model_ms=call.duration_ms, system_prompt_sha256=call.system_sha256)
    action = fields.get("action")
    if isinstance(action, RunSql | ProposeRule | ProfileFraud | AskClarification | Decline):
        values["tool"] = tool_name(action)
    approved_sql, approved_clause = fields.get("approved_sql"), fields.get("approved_clause")
    if isinstance(approved_sql, str):
        values.update(sql=approved_sql, guard="accepted")
    if isinstance(approved_clause, str):
        values.update(where_clause=approved_clause, guard="accepted")
    if node in ("guard_sql", "run_scan") and isinstance(fields.get("reads_labels"), bool):
        values["reads_labels"] = fields["reads_labels"]
    result = fields.get("result")
    if isinstance(result, QueryResult):
        values.update(
            row_count=result.row_count,
            columns=tuple(result.columns),
            truncated=result.truncated,
            query_ms=round(result.elapsed_seconds * 1000),
        )
    elif isinstance(result, BacktestResult):
        # Of a rule's evidence only the time is read: no count, month or rate.
        evidence = fields.get("evidence")
        timed = evidence if isinstance(evidence, RuleEvidence) else result
        values["query_ms"] = round(timed.elapsed_seconds * 1000)
    elif isinstance(result, ScanResult):
        # Two counts and the time: a segment's name is a value read from the data.
        values.update(
            scan_segments=len(result.rows),
            scan_statements=len(result.statements),
            query_ms=round(result.elapsed_seconds * 1000),
        )
    failure = fields.get("failure")
    if isinstance(failure, Failure):
        values.update(
            error_kind=failure.kind,
            reason_code=None if failure.refusal is None else str(failure.refusal),
            repairable=failure.repair is not None,
            failure_message=failure.message,
        )
        if node in ("guard_sql", "guard_rule") and failure.refusal is not None:
            values["guard"] = "rejected"
    response = fields.get("response")
    if isinstance(response, TurnResult):
        values.update(
            outcome=response.outcome,
            status=response.status,
            tool=response.tool,
            caveats=tuple(caveat.code for caveat in response.caveats),
        )
    history = fields.get("history")
    if isinstance(history, tuple) and history:
        values["remembered"] = history[-1].kind
    return StepEvent.model_validate(
        {
            **values,
            "command": label.command,
            "case_id": label.case_id,
            "conversation_id": conversation_id,
            "turn": turn,
            "seq": seq,
            "node": node,
            "duration_ms": duration_ms,
            "attempt": attempt,
            "model_calls": model_calls,
        }
    )


def turn_event(
    result: TurnResult,
    question: str,
    steps: tuple[StepEvent, ...],
    calls: tuple[ModelCall, ...],
    *,
    label: Label = Label(),
) -> TurnEvent:
    """Describe a finished turn. Of the table it reads the count and the column names."""
    table, meta = result.table, result.meta
    labels = [step.reads_labels for step in steps if step.reads_labels is not None]
    return TurnEvent(
        command=label.command,
        case_id=label.case_id,
        conversation_id=meta.conversation_id,
        turn=meta.turn,
        question=question[:QUESTION_LIMIT],
        question_chars=len(question),
        outcome=result.outcome,
        status=result.status,
        error_kind=None if result.error is None else result.error.kind,
        tool=result.tool,
        refused=tuple(str(code) for code in result.refused),
        sql=result.sql,
        where_clause=result.where_clause,
        row_count=None if table is None else table.row_count,
        columns=None if table is None else tuple(column.name for column in table.columns),
        truncated=None if table is None else table.truncated,
        # A backtest always reads the labels; a query does when the guard saw it would,
        # and the scan says so itself.
        reads_labels=result.rule is not None or (result.sql is not None and labels[-1:] == [True]),
        caveats=tuple(caveat.code for caveat in result.caveats),
        model_id=meta.model_id,
        prompt_version=meta.prompt_version,
        system_prompt_sha256=calls[-1].system_sha256 if calls else None,
        tool_mode=meta.tool_mode,
        attempts=meta.attempts,
        model_calls=meta.model_calls,
        input_tokens=meta.input_tokens,
        output_tokens=meta.output_tokens,
        response_ids=tuple(
            call.reply.response_id
            for call in calls
            if call.reply is not None and call.reply.response_id is not None
        ),
        steps=len(steps),
        elapsed_ms=meta.elapsed_ms,
    )


def describe_step(event: StepEvent) -> str:
    """A step in one line that is safe to print. Rows appear as a count only.

    Every piece of text a model or a user wrote is escaped, so it cannot
    move the cursor, start a new line or fail to encode.
    """
    parts: list[str] = []
    if event.node == "begin_turn":
        parts.append(f"turn {event.turn}")
    if event.node == "call_model" and event.model_calls:
        parts.append(f"model request {event.model_calls}")
    if event.reply_tools is not None:
        tools = ", ".join(safe_text(name) for name in event.reply_tools) or "no tool call"
        parts.append(
            f"reply: {tools} ({event.input_tokens or 0:,} in, "
            f"{event.output_tokens or 0:,} out tokens)"
        )
    if event.tool is not None and event.outcome is None:
        parts.append(f"tool chosen: {event.tool}")
    if event.sql is not None:
        parts.append(f"accepted SQL: {safe_text(event.sql)}")
    if event.where_clause is not None:
        parts.append(f"accepted clause: {safe_text(event.where_clause)}")
    if event.reads_labels is True:
        parts.append("reads fraud_labels")
    seconds = (event.query_ms or 0) / 1000
    if event.row_count is not None:
        cut = ", cut at the row cap" if event.truncated else ""
        parts.append(f"{event.row_count:,} row(s) in {seconds:.2f} s{cut}")
    elif event.scan_segments is not None:
        parts.append(
            f"scan listed {event.scan_segments:,} segment(s) from "
            f"{event.scan_statements or 0} statement(s) in {seconds:.2f} s"
        )
    elif event.query_ms is not None:
        parts.append(f"backtest ran in {seconds:.2f} s")
    if event.error_kind is not None:
        again = "repairable" if event.repairable else "not repairable"
        code = f", refusal code {event.reason_code}" if event.reason_code is not None else ""
        parts.append(
            f"failure {event.error_kind} ({again}){code}: {safe_text(event.failure_message or '')}"
        )
    if event.outcome is not None:
        codes = ", ".join(event.caveats or ()) or "none"
        parts.append(
            f"outcome {event.outcome}, status {event.status}, "
            f"tool {event.tool or 'none'}, caveats: {codes}"
        )
    if event.remembered is not None:
        parts.append(f"remembered the turn as kind {event.remembered}")
    return "; ".join(parts) or "nothing to record"
