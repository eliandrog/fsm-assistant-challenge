"""The nodes of the turn graph.

Responsibility: one function per step of a turn (docs/ARCHITECTURE.md, "How
one turn works"). Each reads the state, does its one job with the built
modules, and returns an update. A node catches the typed errors of the
module it calls and writes a `Failure`; it never decides the route, which
`graph.py` does from the state.

Two rules hold by construction. `execute_sql` reads only `approved_sql`,
which only `guard_sql` writes, and `backtest_rule` reads only
`approved_clause`, which only `guard_rule` writes. `call_model` is the only
node that calls the model, and refuses a third request in a turn.

A `Failure` never carries text that came out of the database. SQLite's
message for a statement that failed to compile names only what the
statement itself wrote, and is passed on. Its message for a statement that
failed while it ran can quote a value from a row, so a fixed sentence takes
its place in the failure and in the repair; the message itself goes to
`failure_detail`, which is not saved and is shown to the user only.

The settings and the model are given to `TurnNodes`; nothing is global.
"""

import logging

from fsm_assistant import db
from fsm_assistant.config import Settings
from fsm_assistant.llm import (
    IntentModel,
    ModelOutputError,
    ModelRequest,
    ModelSpec,
    ModelUnavailable,
    StaleRecording,
)
from fsm_assistant.nl2sql import (
    AskClarification,
    ProposeRule,
    RunSql,
    ShapeFailure,
    check_shape,
    describe_reply,
)
from fsm_assistant.presentation import SCAN_NOT_AVAILABLE, build_result
from fsm_assistant.prompt import PreviousTurn, Repair, build_messages
from fsm_assistant.result import ErrorKind, RefusalCode, TurnMeta
from fsm_assistant.rules.backtest import RuleClauseError
from fsm_assistant.rules.proposal import rule_evidence
from fsm_assistant.schema_context import build_schema_context
from fsm_assistant.sql_guard import (
    LABEL_TABLE,
    GuardDecision,
    ReasonCode,
    check_query,
    check_rule_clause,
)
from fsm_assistant.state import (
    HISTORY_LENGTH,
    MAX_MODEL_CALLS,
    PER_TURN_RESET,
    Failure,
    TurnState,
    update,
)

type Update = dict[str, object]

logger = logging.getLogger(__name__)

# What stands in for SQLite's message when an approved statement fails while
# it runs. Fixed text: the message itself can quote a value from the data.
QUERY_FAILED_RUNNING = "The query passed the checks but failed while it was running."
RULE_FAILED_RUNNING = "The rule clause passed the checks but its backtest failed while running."
RUN_TIME_REASON = (
    "The statement compiled, then failed while it was running: a function or an operator "
    "was given a value it cannot handle (for example a number that overflows, a malformed "
    "JSON text or path, or a value of the wrong type). SQLite's own message is not repeated "
    "here. Write it another way, avoiding the operation that can fail on some rows."
)
NOT_A_READ = "Only reading is allowed, and it tried to do something other than read."


class TurnNodes:
    def __init__(self, settings: Settings, model: IntentModel, spec: ModelSpec) -> None:
        self._settings = settings
        self._model = model
        self._spec = spec

    def begin_turn(self, state: TurnState) -> Update:
        question = state.question.strip()
        limit = self._settings.max_message_characters
        failure: Failure | None = None
        # Zero-width and other invisible characters do not make a question.
        if not any(c.isprintable() and not c.isspace() for c in question):
            failure = Failure(kind="invalid_input", message="The message is empty.")
        elif len(question) > limit:
            failure = Failure(
                kind="invalid_input",
                message=f"The message is {len(question):,} characters long; "
                f"the limit is {limit:,}.",
            )
        fields = {**PER_TURN_RESET, "failure": failure}
        return update(question=question, turn=state.turn + 1, **fields)

    def call_model(self, state: TurnState) -> Update:
        if state.model_calls >= MAX_MODEL_CALLS:
            raise RuntimeError(f"a turn may make at most {MAX_MODEL_CALLS} model requests")
        earlier = state.failure
        repair = earlier.repair if earlier is not None else None
        # Nothing of the first attempt survives into the second, except the code of its refusal.
        refused = state.refused
        if earlier is not None and earlier.refusal is not None:
            refused = (*refused, earlier.refusal)
        cleared = update(
            action=None,
            approved_sql=None,
            approved_clause=None,
            reads_labels=False,
            failure=None,
            refused=refused,
            result=None,
            evidence=None,
            failure_detail=None,
        )
        try:
            schema_context = build_schema_context(self._settings.db_path)
        except db.DbError as error:
            return {**cleared, **update(failure=_environment(error))}
        messages = build_messages(
            state.question,
            schema_context=schema_context,
            previous=state.history[-1] if state.history else None,
            repair=repair,
        )
        request = ModelRequest(
            system=messages.system,
            user=messages.user,
            turn=state.turn,
            call=state.model_calls + 1,
        )
        cleared["model_calls"] = state.model_calls + 1
        try:
            reply = self._model.complete(request)
        except (ModelUnavailable, StaleRecording) as error:
            kind = error.kind if isinstance(error, ModelUnavailable) else "stale recording"
            return {
                **cleared,
                **update(
                    failure=Failure(
                        kind="model_unavailable",
                        message=f"No reply from the model ({kind}): {error}",
                    )
                ),
            }
        except ModelOutputError as error:
            return {
                **cleared,
                **update(
                    raw_reply=error.reply,
                    attempt=state.attempt + 1,
                    input_tokens=state.input_tokens + error.reply.usage.input_tokens,
                    output_tokens=state.output_tokens + error.reply.usage.output_tokens,
                    failure=Failure(kind="model_output", message=f"No usable reply: {error}."),
                ),
            }
        return {
            **cleared,
            **update(
                raw_reply=reply,
                attempt=state.attempt + 1,
                input_tokens=state.input_tokens + reply.usage.input_tokens,
                output_tokens=state.output_tokens + reply.usage.output_tokens,
            ),
        }

    def check_intent(self, state: TurnState) -> Update:
        if state.raw_reply is None:
            raise RuntimeError("check_intent ran without a model reply")
        shape = check_shape(state.raw_reply)
        if isinstance(shape, ShapeFailure):
            return update(
                failure=Failure(
                    kind="model_output",
                    message=f"The model's reply was not one valid action. {shape.reason}",
                    repair=Repair(
                        previous_answer=describe_reply(state.raw_reply),
                        reason=shape.reason,
                        about_rule=False,
                    ),
                    refusal=shape.kind,
                )
            )
        return update(action=shape)

    def guard_sql(self, state: TurnState) -> Update:
        if not isinstance(state.action, RunSql):
            raise RuntimeError("guard_sql ran without a run_sql action")
        try:
            decision = check_query(state.action.sql, db_path=self._settings.db_path)
        except db.DbError as error:
            return update(failure=_environment(error))
        if not decision.accepted:
            return update(failure=_refused(decision, "query", about_rule=False))
        reads_labels = decision.reads is not None and LABEL_TABLE in decision.reads.tables
        return update(approved_sql=decision.sql, reads_labels=reads_labels)

    def execute_sql(self, state: TurnState) -> Update:
        sql = state.approved_sql
        if sql is None:
            raise RuntimeError("execute_sql ran without a statement the guard approved")
        settings = self._settings
        try:
            result = db.run_query(
                sql,
                db_path=settings.db_path,
                row_cap=settings.row_cap,
                timeout_seconds=settings.query_timeout_seconds,
                max_result_bytes=settings.max_result_bytes,
            )
        except db.QueryTimeout:
            return update(failure=self._timed_out(sql, "statement", about_rule=False))
        except db.ResultTooLarge:
            reason = (
                f"The result is too large: over {settings.max_result_bytes:,} bytes. "
                "Select fewer or shorter columns, or aggregate."
            )
            return update(
                failure=_failed("sql_execution", reason, sql, reason, about_rule=False)
            )
        except db.QueryError as error:
            return update(
                failure=_failed(
                    "sql_execution", QUERY_FAILED_RUNNING, sql, RUN_TIME_REASON, about_rule=False
                ),
                failure_detail=error.sqlite_message,
            )
        except db.ReadOnlyViolation:
            return update(
                failure=_failed(
                    "sql_rejected",
                    f"The query was stopped while running. {NOT_A_READ}",
                    sql,
                    NOT_A_READ,
                    about_rule=False,
                    refusal=ReasonCode.NOT_READ_ONLY,
                )
            )
        except db.DbError as error:
            return update(failure=_environment(error))
        return update(result=result)

    def guard_rule(self, state: TurnState) -> Update:
        if not isinstance(state.action, ProposeRule):
            raise RuntimeError("guard_rule ran without a propose_rule action")
        try:
            decision = check_rule_clause(state.action.where_clause, db_path=self._settings.db_path)
        except db.DbError as error:
            return update(failure=_environment(error))
        if not decision.accepted:
            return update(failure=_refused(decision, "rule clause", about_rule=True))
        return update(approved_clause=decision.sql)

    def backtest_rule(self, state: TurnState) -> Update:
        clause = state.approved_clause
        if clause is None:
            raise RuntimeError("backtest_rule ran without a clause the guard approved")
        try:
            evidence = rule_evidence(
                clause,
                db_path=self._settings.db_path,
                timeout_seconds=self._settings.query_timeout_seconds,
            )
        except RuleClauseError as error:
            # The guard accepted this clause a moment ago, so this is a backtest
            # statement failing as it ran: its message is not passed on.
            if error.reason_code == ReasonCode.SQLITE_ERROR:
                return update(
                    failure=_failed(
                        "sql_execution", RULE_FAILED_RUNNING, clause, RUN_TIME_REASON,
                        about_rule=True,
                    ),
                    failure_detail=error.sqlite_message,
                )  # fmt: skip
            return update(
                failure=_failed(
                    "sql_rejected",
                    f"The rule clause was refused when the backtest ran it. {NOT_A_READ}",
                    clause,
                    NOT_A_READ,
                    about_rule=True,
                    refusal=error.reason_code,
                )
            )
        except db.QueryTimeout:
            return update(failure=self._timed_out(clause, "backtest of the clause", about_rule=True))
        except db.DbError as error:
            return update(failure=_environment(error))
        return update(result=evidence.overall, evidence=evidence)

    def run_scan(self, state: TurnState) -> Update:
        # The scan is a later task. Nothing runs and no figure is invented.
        return update(failure=Failure(kind="not_available", message=SCAN_NOT_AVAILABLE))

    def present(self, state: TurnState) -> Update:
        meta = TurnMeta(
            conversation_id="",  # filled in by `graph`, which knows them
            turn=state.turn,
            model_id=state.raw_reply.model_id if state.raw_reply else self._spec.model,
            prompt_version=self._spec.prompt_version,
            tool_mode=self._spec.tool_mode,
            attempts=state.attempt,
            model_calls=state.model_calls,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            elapsed_ms=0,
        )
        response = build_result(state, meta)
        return update(response=response, outcome=response.outcome)

    def remember(self, state: TurnState) -> Update:
        """Keep the turn if a follow-up can build on it; any other turn leaves memory as it was."""
        action = state.action
        if state.outcome == "answered":
            record = PreviousTurn(question=state.question, kind="sql", sql=state.approved_sql)
        elif state.outcome == "rule_proposed":
            record = PreviousTurn(
                question=state.question, kind="rule", where_clause=state.approved_clause
            )
        elif isinstance(action, AskClarification) and state.outcome == "clarification":
            record = PreviousTurn(
                question=state.question,
                kind="clarify",
                clarification=action.question,
                suggestions=action.suggestions,
            )
        else:
            return update()
        return update(history=(*state.history, record)[-HISTORY_LENGTH:])

    def _timed_out(self, text: str, subject: str, *, about_rule: bool) -> Failure:
        seconds = self._settings.query_timeout_seconds
        return _failed(
            "timeout",
            f"The {subject} was interrupted after {seconds:g} seconds.",
            text,
            f"The {subject} ran past the {seconds:g}-second limit. Write one that does less "
            "work: join on the keys given in the schema and avoid joining a table to itself.",
            about_rule=about_rule,
        )


def _failed(
    kind: ErrorKind,
    message: str,
    previous_answer: str,
    reason: str,
    *,
    about_rule: bool,
    refusal: RefusalCode | None = None,
) -> Failure:
    return Failure(
        kind=kind,
        message=message,
        repair=Repair(previous_answer=previous_answer, reason=reason, about_rule=about_rule),
        refusal=refusal,
    )


def _refused(decision: GuardDecision, subject: str, *, about_rule: bool) -> Failure:
    """A guard rejection. Text SQLite could not compile is an SQL error, not a refusal."""
    reason = decision.reason or ""
    if decision.reason_code == ReasonCode.SQLITE_ERROR:
        return _failed(
            "sql_execution", f"The {subject} is not valid SQL. {reason}", decision.sql, reason,
            about_rule=about_rule, refusal=decision.reason_code,
        )  # fmt: skip
    return _failed(
        "sql_rejected", f"The {subject} was not allowed to run. {reason}", decision.sql, reason,
        about_rule=about_rule, refusal=decision.reason_code,
    )  # fmt: skip


def _environment(error: db.DbError) -> Failure:
    """The database could not be used at all: no reply from the model can fix that."""
    logger.error("the database could not be read: %s", type(error).__name__)
    return Failure(kind="internal", message=f"The database could not be read. {error}")
