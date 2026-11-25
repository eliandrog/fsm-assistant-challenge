"""The turn graph: its invariants, every outcome, the one repair, and memory.

Every test drives the real graph with a scripted model against the small
fixture database. Nothing is sent anywhere.
"""

import ast
import logging
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from conftest import call_reply, rule_reply, sql_reply, tool_reply
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata

from fsm_assistant import db, graph, nodes, sql_guard, state
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.events import NodeName, describe_step, step_event
from fsm_assistant.graph import NODE_NAMES, Assistant, TraceStep
from fsm_assistant.llm import (
    ModelError,
    ModelOutputError,
    ModelReply,
    ModelRequest,
    ModelUnavailable,
    ScriptedModel,
    StaleRecording,
)
from fsm_assistant.nl2sql import PROMPT_VERSION, RunSql, model_spec
from fsm_assistant.nodes import (
    QUERY_FAILED_RUNNING,
    RULE_FAILED_RUNNING,
    RUN_TIME_REASON,
    TurnNodes,
)
from fsm_assistant.presentation import render_text
from fsm_assistant.prompt import RULE_SCOPE
from fsm_assistant.result import ErrorKind, Outcome, Status, TurnResult
from fsm_assistant.rules.backtest import RuleClauseError
from fsm_assistant.rules.proposal import RuleEvidence
from fsm_assistant.sql_guard import GuardDecision, ReasonCode
from fsm_assistant.state import PER_TURN_RESET, Failure, TurnState

COUNT = "SELECT count(*) FROM transactions"
# The value exists only in the rows: neither the SQL nor the schema context spells it.
ADDRESSES = "SELECT 'row-' || id || '-marker' AS tag FROM users ORDER BY id"
ROW_MARKER = "row-1-marker"
FORBIDDEN = "SELECT name FROM sqlite_master"
UNKNOWN_COLUMN = "SELECT amount FROM transactions"
OVERFLOW = "SELECT abs(-9223372036854775807 - 1) FROM transactions"
ENDLESS = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
RULE = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"
OUT_OF_SCOPE_RULE = "card_type = 'Debit'"
LABEL_RULE = "id IN (SELECT CAST(transaction_id AS INTEGER) FROM fraud_labels WHERE is_fraud = 1)"
TEXT_ONLY = tool_reply()
# Statements the guard accepts and SQLite fails while running, with a message that
# quotes a value read from a row: an address, and a merchant id with an amount.
ADDRESS_IN_ERROR = "SELECT json_extract('{}', (SELECT address FROM users ORDER BY id LIMIT 1))"
ADDRESS_IN_RULE_ERROR = (
    "json_extract('{}', (SELECT address FROM users ORDER BY id LIMIT 1)) IS NULL"
)
AMOUNT_IN_RULE_ERROR = "json_extract('{}', merchant_id || ':' || amount_usd_cents) IS NULL"
ROW_VALUES = ("Elm Street", "10:2500")

type Script = list[ModelReply | ModelError]


@pytest.fixture
def settings(fixture_db: Path) -> Settings:
    return load_settings(
        {"FSM_DB_PATH": str(fixture_db), "FSM_QUERY_TIMEOUT_SECONDS": "0.3"}, env_file=None
    )


def turn(settings: Settings, script: Script, question: str = "A question") -> TurnResult:
    return Assistant(settings, ScriptedModel(script)).run_turn(question, "c1")


def saved(assistant: Assistant, conversation: str) -> dict[str, object]:
    """The state the checkpointer holds for a conversation."""
    values: dict[str, object] = assistant.graph.get_state(
        {"configurable": {"thread_id": conversation}}
    ).values
    return values


def facts(result: TurnResult) -> tuple[Outcome, Status, ErrorKind | None]:
    return result.outcome, result.status, result.error.kind if result.error else None


# --- Invariants ---------------------------------------------------------------------------


def test_only_the_exact_string_the_guard_accepted_is_executed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted: list[str] = []
    executed: list[str] = []
    real_check, real_run = sql_guard.check_query, db.run_query

    def check(sql: str, *, db_path: Path) -> GuardDecision:
        decision = real_check(sql, db_path=db_path)
        if decision.accepted:
            accepted.append(decision.sql)
        return decision

    def run(sql: str, *args: object, **kwargs: object) -> db.QueryResult:
        executed.append(sql)
        return real_run(sql, *args, **kwargs)  # type: ignore[arg-type]  # passed through as given

    assistant = Assistant(settings, ScriptedModel([sql_reply(FORBIDDEN), sql_reply(f" {COUNT} ; ")]))
    assistant.run_turn("warm the schema context and the guard's table list", "warm-up")
    monkeypatch.setattr(nodes, "check_query", check)
    monkeypatch.setattr(db, "run_query", run)

    assistant = Assistant(settings, ScriptedModel([sql_reply(FORBIDDEN), sql_reply(f" {COUNT} ; ")]))
    result = assistant.run_turn("How many?", "c1")

    assert result.outcome == "answered"
    assert accepted == [COUNT]
    assert executed == [COUNT]
    assert result.sql == COUNT


def test_execute_sql_reads_the_approved_string_and_nothing_else(settings: Settings) -> None:
    turn_nodes = TurnNodes(settings, ScriptedModel([]), model_spec(settings))
    action = RunSql(sql="SELECT count(*) FROM users", explanation="", assumptions=())

    with pytest.raises(RuntimeError, match="without a statement the guard approved"):
        turn_nodes.execute_sql(TurnState(action=action))
    with pytest.raises(RuntimeError, match="without a clause the guard approved"):
        turn_nodes.backtest_rule(TurnState())

    update = turn_nodes.execute_sql(TurnState(action=action, approved_sql=COUNT))
    result = update["result"]
    assert isinstance(result, db.QueryResult)
    assert result.rows == ((6,),), "the approved statement ran, not the action's"


def test_a_repair_request_clears_what_the_first_attempt_approved(settings: Settings) -> None:
    model = ScriptedModel([tool_reply("decline")])
    turn_nodes = TurnNodes(settings, model, model_spec(settings))
    update = turn_nodes.call_model(TurnState(question="q", approved_sql=COUNT, approved_clause=RULE))
    assert update["approved_sql"] is None
    assert update["approved_clause"] is None


def test_only_db_opens_the_database() -> None:
    package = Path(db.__file__).parent
    importers = set()
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(name.split(".")[0] == "sqlite3" for name in names):
                importers.add(path.name)
    # `rule_bank.py` opens the rule bank, a file of its own, and never the database.
    assert importers == {"db.py", "rule_bank.py"}
    bank = ast.parse((package / "rule_bank.py").read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(bank) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not {"fsm_assistant.db", "fsm_assistant.config", "fsm_assistant"} & imported
    assert "rule_bank" not in (package / "db.py").read_text(encoding="utf-8")


def test_every_figure_shown_comes_through_run_query(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assistant = Assistant(settings, ScriptedModel([sql_reply(COUNT), sql_reply(COUNT)]))
    assert assistant.run_turn("How many?", "c1").answer == "count(*): 6"
    canned = db.QueryResult(columns=("n",), rows=((424242,),), truncated=False, elapsed_seconds=0.0)

    def run(sql: str, *args: object, **kwargs: object) -> db.QueryResult:
        return canned

    monkeypatch.setattr(db, "run_query", run)
    result = assistant.run_turn("How many?", "c2")
    assert result.table is not None
    assert result.table.rows == ((424242,),)
    assert result.answer == "n: 424,242"


@pytest.mark.parametrize(
    "first",
    [TEXT_ONLY, sql_reply(FORBIDDEN), sql_reply(UNKNOWN_COLUMN), sql_reply(OVERFLOW),
     sql_reply(ENDLESS), rule_reply(LABEL_RULE), rule_reply(OUT_OF_SCOPE_RULE)],
)  # fmt: skip
def test_a_turn_makes_at_most_two_model_requests(settings: Settings, first: ModelReply) -> None:
    model = ScriptedModel([first, first, sql_reply(COUNT)])
    result = Assistant(settings, model).run_turn("A question", "c1")
    assert model.calls == 2
    assert result.meta.model_calls == 2
    assert result.meta.attempts == 2
    assert result.status in ("error", "rejected")


def test_call_model_refuses_a_third_request(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(COUNT)])
    turn_nodes = TurnNodes(settings, model, model_spec(settings))
    with pytest.raises(RuntimeError, match="at most 2 model requests"):
        turn_nodes.call_model(TurnState(question="q", model_calls=2))
    assert model.calls == 0


def test_no_result_row_is_saved_or_sent_to_the_model(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(ADDRESSES), rule_reply(RULE), sql_reply(ADDRESSES)])
    assistant = Assistant(settings, model)

    first = assistant.run_turn("List the addresses", "c1")
    assert first.table is not None
    assert first.table.rows[0] == (ROW_MARKER,), "the marker is in what the user is shown"
    assistant.run_turn("A rule for big debit payments", "c1")
    assistant.run_turn("And again?", "c1")

    snapshot = saved(assistant, "c1")
    assert "result" not in snapshot
    assert "evidence" not in snapshot
    assert "response" not in snapshot
    stored = kept(assistant)
    assert "approved_sql" in stored, "the search below looks in the right place"
    assert ROW_MARKER not in stored
    assert "flagged" not in stored, "nor a backtest"
    for name in ("weakest_month", "total_fraud", "min_precision", "2019-02"):
        assert name not in stored, "nor its month-by-month evidence"
    for request in model.requests:
        assert ROW_MARKER not in request.system + request.user
    assert ADDRESSES in model.requests[2].user or RULE in model.requests[2].user


def test_no_figure_of_a_rules_evidence_is_saved_sent_or_traced(settings: Settings) -> None:
    # The rule flags one transaction, in February. A follow-up is then told the clause only.
    steps: list[TraceStep] = []
    model = ScriptedModel([rule_reply("id = 1002"), sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    handed = everything_saved(assistant)

    proposed = assistant.run_turn("A rule for that one payment", "c1", on_step=steps.append)
    card = proposed.rule
    assert card is not None, "the figures are in what the user is shown"
    assert [row.month for row in card.months if row.flagged] == ["2019-02"]
    assert card.lift == 4.0 and card.later.first_month == "2019-10"
    assistant.run_turn("And how many are there?", "c1")

    untracked = {"result", "evidence", "response", "failure_detail"}
    assert untracked <= set(TurnState.model_fields)
    assert not untracked & set(saved(assistant, "c1"))
    stored = "\n".join(handed) + kept(assistant)
    sent = "\n".join(request.system + request.user for request in model.requests)
    traced = "\n".join(step.summary for step in steps)
    assert "id = 1002" in stored and "id = 1002" in model.requests[1].user and "id = 1002" in traced
    for name in ("2019-02", "weakest_month", "true_positives", "total_fraud", "stability", "lift"):
        assert name not in stored, name
    for text in (sent, traced):
        for name in ("weakest_month", "true_positives", "total_fraud", "min_precision"):
            assert name not in text, name
    assert "backtest ran in" in traced
    # The months of the data are not in the prompt as figures of this rule.
    assert "2019-02" not in model.requests[1].user


# What LangGraph writes beside the state: a clock time, ids and channel versions. Each is
# made from a clock or random digits, so a short figure occurs in one by chance. A search
# for a leak leaves them out by name and reads everything else.
CHECKPOINT_BOOKKEEPING = frozenset({"ts", "id", "channel_versions", "versions_seen"})
CONFIG_BOOKKEEPING = frozenset({"checkpoint_id", "checkpoint_map"})


def _own_config(config: RunnableConfig) -> dict[str, object]:
    configurable: Mapping[str, object] = config.get("configurable", {})
    own = {
        key: value
        for key, value in configurable.items()
        if key not in CONFIG_BOOKKEEPING and not key.startswith("__pregel_")
    }
    return {**config, "configurable": own}


def _state_part(checkpoint: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in checkpoint.items() if key not in CHECKPOINT_BOOKKEEPING}


def everything_saved(assistant: Assistant) -> list[str]:
    """Record all the saver is ever handed, so a value it kept only for a while still shows."""
    handed: list[str] = []
    saver = assistant.saver
    real_put, real_put_writes = saver.put, saver.put_writes

    def put(
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        handed.append(repr((_own_config(config), _state_part(checkpoint), metadata)))
        return real_put(config, checkpoint, metadata, new_versions)

    def put_writes(
        config: RunnableConfig,
        writes: Sequence[tuple[str, object]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        handed.append(repr((_own_config(config), writes, task_path)))
        real_put_writes(config, writes, task_id, task_path)

    setattr(saver, "put", put)
    setattr(saver, "put_writes", put_writes)
    return handed


def kept(assistant: Assistant) -> str:
    """All the saver holds now, as text to search: each value as stored and as read back.

    A number is stored as bytes, so only the value read back shows its digits.
    """
    saver = assistant.saver
    read = saver.serde.loads_typed
    checkpoints = [
        (thread, namespace, _state_part(read(checkpoint)), read(metadata))
        for thread, spaces in saver.storage.items()
        for namespace, by_id in spaces.items()
        for checkpoint, metadata, _ in by_id.values()
    ]
    writes = [
        (thread, namespace, channel, value, read(value), path)
        for (thread, namespace, _), by_task in saver.writes.items()
        for _, channel, value, path in by_task.values()
    ]
    blobs = [
        (thread, namespace, channel, blob, None if blob[0] == "empty" else read(blob))
        for (thread, namespace, channel, _), blob in saver.blobs.items()
    ]
    return repr((checkpoints, writes, blobs))


@pytest.mark.parametrize(
    ("replies", "outcome", "message", "detail"),
    [
        ([sql_reply(ADDRESS_IN_ERROR)] * 2, "failed", QUERY_FAILED_RUNNING, "1 Elm Street"),
        ([sql_reply(ADDRESS_IN_ERROR), sql_reply(COUNT)], "answered", None, None),
        ([rule_reply(ADDRESS_IN_RULE_ERROR)] * 2, "failed", RULE_FAILED_RUNNING, "1 Elm Street"),
        ([rule_reply(AMOUNT_IN_RULE_ERROR)] * 2, "failed", RULE_FAILED_RUNNING, "10:2500"),
        ([rule_reply(AMOUNT_IN_RULE_ERROR), rule_reply(RULE)], "rule_proposed", None, None),
    ],
    ids=["query twice", "query repaired", "rule twice", "rule on a column", "rule repaired"],
)
def test_a_value_quoted_by_a_run_time_error_is_never_saved_sent_or_traced(
    settings: Settings,
    replies: Script,
    outcome: Outcome,
    message: str | None,
    detail: str | None,
) -> None:
    model = ScriptedModel([*replies, sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    handed = everything_saved(assistant)
    steps: list[TraceStep] = []

    result = assistant.run_turn("A question", "c1", on_step=steps.append)
    follow_up = assistant.run_turn("And then?", "c1", on_step=steps.append)

    assert result.outcome == outcome
    assert follow_up.outcome == "answered"
    assert model.calls == 3
    repair = model.requests[1].user
    assert RUN_TIME_REASON in repair, "the model is told a fixed reason"
    assert handed and "approved_sql" in "".join(handed), "the search looks in the right place"
    searched = {
        # The user text only: the schema context in the system text lists the values of a
        # column with few distinct ones, which on a two-user fixture includes the addresses.
        "a model request": "".join(request.user for request in model.requests),
        "the saver, at any time": "".join(handed),
        "the saver, at the end": kept(assistant),
        "the trace": "".join(step.summary for step in steps),
        "the result, apart from the detail": result.model_dump_json(
            exclude={"error": {"detail"}}
        ),
        "the follow-up's result": follow_up.model_dump_json(),
    }
    for value in ROW_VALUES:
        for place, text in searched.items():
            assert value not in text, f"{value!r} reached {place}"
    if message is not None:
        assert result.error is not None
        assert (result.error.kind, result.error.message) == ("sql_execution", message)
        assert result.error.detail is not None and detail is not None
        assert detail in result.error.detail, "the user, and only the user, sees SQLite's words"
        assert detail in render_text(result)
    else:
        assert result.error is None
    assert follow_up.error is None


def test_a_compile_time_message_names_only_what_the_statement_wrote(settings: Settings) -> None:
    # SQLite evaluates no subquery while compiling, so its message cannot hold a row's value.
    statements = [
        "SELECT (SELECT address FROM users ORDER BY id LIMIT 1) FROM nope",
        "SELECT address FROM users GROUP BY 5",
        "SELECT json_extract('{}', (SELECT address FROM users LIMIT 1)) FROM users WHERE nope = 1",
        "SELECT * FROM users WHERE address = (SELECT address FROM users LIMIT 1) AND",
    ]
    for statement in statements:
        decision = sql_guard.check_query(statement, db_path=settings.db_path)
        assert decision.reason_code == ReasonCode.SQLITE_ERROR
        assert decision.reason is not None
        for value in ROW_VALUES:
            assert value not in decision.reason


def test_a_rule_is_never_returned_without_its_backtest(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposed = turn(settings, [rule_reply(RULE)])
    assert proposed.outcome == "rule_proposed"
    assert proposed.where_clause == RULE
    assert proposed.rule is not None
    assert (proposed.rule.flagged, proposed.rule.true_positives) == (1, 1)
    # ... nor without its months: six transactions in six months, the rule's one in February.
    months = {row.month: (row.flagged, row.true_positives, row.total_transactions) for row in proposed.rule.months}
    assert months == {
        "2019-01": (0, 0, 1),
        "2019-02": (1, 1, 1),
        "2019-03": (0, 0, 1),
        "2019-04": (0, 0, 1),
        "2019-05": (0, 0, 1),
        "2019-10": (0, 0, 1),
    }

    def broken(clause: str, *, db_path: Path, timeout_seconds: float) -> None:
        raise RuleClauseError("SQLite rejected the clause: broken", ReasonCode.SQLITE_ERROR, "broken")

    monkeypatch.setattr(nodes, "rule_evidence", broken)
    result = turn(settings, [rule_reply(RULE), rule_reply(RULE)])
    assert facts(result) == ("failed", "error", "sql_execution")
    assert result.where_clause is None
    assert result.rule is None


def test_no_route_leads_from_the_rule_guard_to_the_user_except_through_the_backtest(
    settings: Settings,
) -> None:
    edges = Assistant(settings, ScriptedModel([])).graph.get_graph().edges
    from_guard = {(edge.target, edge.data) for edge in edges if edge.source == "guard_rule"}
    assert from_guard == {
        ("backtest_rule", "accepted"),
        ("call_model", "repair"),
        ("present", "failed"),
    }
    assert {edge.source for edge in edges if edge.target == "execute_sql"} == {"guard_sql"}
    assert {edge.source for edge in edges if edge.target == "backtest_rule"} == {"guard_rule"}
    assert {edge.source for edge in edges if edge.target == "remember"} == {"present"}


@pytest.mark.parametrize("node", NODE_NAMES)
def test_an_unexpected_error_in_any_node_becomes_an_internal_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, node: str
) -> None:
    original = getattr(TurnNodes, node)
    faulty = [True]

    def explode(self: TurnNodes, state: TurnState) -> dict[str, object]:
        if faulty:
            raise ZeroDivisionError(f"fault in {node}")
        update: dict[str, object] = original(self, state)
        return update

    monkeypatch.setattr(TurnNodes, node, explode)
    first = {
        "guard_rule": rule_reply(RULE),
        "backtest_rule": rule_reply(RULE),
        "run_scan": tool_reply("profile_fraud"),
    }.get(node, sql_reply(COUNT))
    assistant = Assistant(settings, ScriptedModel([first, sql_reply(COUNT)]))

    result = assistant.run_turn("A question", "c1")
    assert facts(result) == ("failed", "error", "internal")
    assert f"fault in {node}" in result.answer
    assert result.meta.conversation_id == "c1"
    asked = node not in ("begin_turn", "call_model")
    assert result.meta.turn == (0 if node == "begin_turn" else 1)
    assert (result.meta.model_calls, result.meta.attempts) == ((1, 1) if asked else (0, 0))
    assert result.meta.input_tokens == (1000 if asked else 0), "what was billed is reported"

    faulty.clear()
    again = assistant.run_turn("Again", "c1")
    assert again.outcome == "answered", "the conversation still works"
    assert again.meta.model_calls == 1


def test_an_error_raised_by_the_model_object_itself_does_not_escape(settings: Settings) -> None:
    result = turn(settings, [])  # the script is exhausted: a fault of the caller
    assert facts(result) == ("failed", "error", "internal")
    assert "ScriptExhausted" in result.answer


def test_a_failing_trace_callback_does_not_fail_the_turn(settings: Settings) -> None:
    def broken(step: TraceStep) -> None:
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")

    result = Assistant(settings, ScriptedModel([sql_reply(COUNT)])).run_turn(
        "How many?", "c1", on_step=broken
    )
    assert facts(result) == ("answered", "ok", None)


# --- The outcome table --------------------------------------------------------------------


def test_answered(settings: Settings) -> None:
    result = turn(settings, [sql_reply(COUNT, "All transactions, labelled or not")])
    assert facts(result) == ("answered", "ok", None)
    assert result.sql == COUNT
    assert result.table is not None
    assert (result.table.rows, result.table.row_count, result.table.truncated) == (((6,),), 1, False)
    assert result.answer == "count(*): 6"
    assert result.answer_source == "code"
    assert result.explanation == "What it returns."
    assert result.assumptions == ("All transactions, labelled or not",)
    assert result.caveats == ()


def test_zero_rows_still_counts_as_answered(settings: Settings) -> None:
    result = turn(settings, [sql_reply("SELECT id FROM cards WHERE card_type = 'debit'")])
    assert facts(result) == ("answered", "ok", None)
    assert [caveat.code for caveat in result.caveats] == ["zero_rows"]


def test_rule_proposed(settings: Settings) -> None:
    result = turn(settings, [rule_reply(RULE, "Debit only, not prepaid")])
    assert facts(result) == ("rule_proposed", "ok", None)
    assert result.sql is None and result.table is None
    assert result.assumptions == ("Debit only, not prepaid",)
    card = result.rule
    assert card is not None
    # Four labelled transactions, one of them fraud: the rule's one catch is four times chance.
    assert (card.base_fraud_rate, card.lift, card.weakest_month) == (1 / 4, 4.0, None)
    assert (card.earlier.first_month, card.earlier.last_month, card.earlier.months) == ("2019-01", "2019-05", 5)
    assert (card.earlier.true_positives, card.earlier.total_fraud, card.earlier.recall) == (1, 1, 1.0)
    assert (card.later.first_month, card.later.months, card.later.flagged, card.later.recall) == ("2019-10", 1, 0, None)
    assert (card.stability.months, card.stability.months_flagged, card.stability.qualifying_months) == (6, 1, 0)
    text = render_text(result)
    assert "Month by month, on the same history:" in text and "this is a stability check" in text
    assert [caveat.code for caveat in result.caveats] == [
        "label_coverage", "small_sample", "high_flag_rate",
    ]  # fmt: skip


def test_the_backtest_node_returns_the_evidence_and_its_overall_part(settings: Settings) -> None:
    turn_nodes = TurnNodes(settings, ScriptedModel([]), model_spec(settings))
    update = turn_nodes.backtest_rule(TurnState(question="q", approved_clause=RULE))
    assert set(update) == {"result", "evidence"}
    evidence = update["evidence"]
    assert isinstance(evidence, RuleEvidence)
    assert update["result"] is evidence.overall
    assert len(evidence.months) == 6 and sum(row.flagged for row in evidence.months) == evidence.overall.flagged
    assert PER_TURN_RESET["evidence"] is None
    with pytest.raises(RuntimeError, match="without a clause the guard approved"):
        turn_nodes.backtest_rule(TurnState(question="q"))


def test_a_repair_request_clears_the_evidence_of_the_first_attempt(settings: Settings) -> None:
    model = ScriptedModel([tool_reply("decline")])
    turn_nodes = TurnNodes(settings, model, model_spec(settings))
    measured = turn_nodes.backtest_rule(TurnState(question="q", approved_clause=RULE))
    stale = TurnState.model_validate({"question": "q", **measured})
    assert stale.evidence is not None
    cleared = turn_nodes.call_model(stale)
    assert cleared["evidence"] is None and cleared["result"] is None


@pytest.mark.parametrize(
    ("clause", "answer"),
    [
        ("id = 1002", "The rule flags 1 of 6 transactions. Of the 1 with a fraud label, "
         "1 is fraud and 0 are legitimate."),
        ("id IN (1001, 1003)", "The rule flags 2 of 6 transactions. Of the 2 with a fraud "
         "label, 0 are fraud and 2 are legitimate."),
        ("id = 1005", "The rule flags 1 of 6 transactions. None of them has a fraud label."),
        ("amount_usd_cents > 99999999", "The rule flags 0 of 6 transactions."),
    ],
)  # fmt: skip
def test_the_rule_summary_agrees_with_its_counts(
    settings: Settings, clause: str, answer: str
) -> None:
    assert turn(settings, [rule_reply(clause)]).answer == answer


def test_clarification(settings: Settings) -> None:
    reply = call_reply("ask_clarification", question="What is risky?", suggestions=["a", "b"])
    result = turn(settings, [reply])
    assert facts(result) == ("clarification", "needs_clarification", None)
    assert (result.answer, result.answer_source) == ("What is risky?", "model")
    assert result.suggestions == ("a", "b")
    assert result.sql is None and result.table is None and result.rule is None


def test_cannot_answer(settings: Settings) -> None:
    result = turn(settings, [call_reply("decline", reason="There is no email column.")])
    assert facts(result) == ("cannot_answer", "cannot_answer", None)
    assert (result.answer, result.answer_source) == ("There is no email column.", "model")


def test_rejected_when_the_guard_refuses_twice(settings: Settings) -> None:
    result = turn(settings, [sql_reply(FORBIDDEN), sql_reply("SELECT date('now')")])
    assert facts(result) == ("rejected", "rejected", "sql_rejected")
    assert result.sql is None and result.table is None


@pytest.mark.parametrize(
    ("sql", "kind"),
    [(OVERFLOW, "sql_execution"), (UNKNOWN_COLUMN, "sql_execution"), (ENDLESS, "timeout")],
)
def test_failed_when_the_sql_fails_twice(settings: Settings, sql: str, kind: ErrorKind) -> None:
    result = turn(settings, [sql_reply(sql), sql_reply(sql)])
    assert facts(result) == ("failed", "error", kind)
    assert result.table is None


@pytest.mark.parametrize(
    "message",
    ["", "   \n ", "x" * 2001, "\u200b", "\u200b \u2060\ufeff\u00a0", "\x00\x1b"],
    ids=["empty", "blank", "too long", "zero-width", "invisible", "control"],
)
def test_an_empty_or_over_long_message_makes_no_model_call(settings: Settings, message: str) -> None:
    model = ScriptedModel([sql_reply(COUNT)])
    result = Assistant(settings, model).run_turn(message, "c1")
    assert facts(result) == ("failed", "error", "invalid_input")
    assert model.calls == 0
    assert (result.meta.model_calls, result.meta.attempts) == (0, 0)


def test_a_message_at_the_length_limit_is_accepted(settings: Settings) -> None:
    assert turn(settings, [sql_reply(COUNT)], "x" * 2000).outcome == "answered"


@pytest.mark.parametrize(
    "error",
    [
        ModelUnavailable("missing_key", "ANTHROPIC_API_KEY is not set."),
        ModelUnavailable("overloaded_or_server_error", "529"),
        StaleRecording("missing", Path("q.turn1.call1.json"), "no reply was recorded"),
    ],
)
def test_model_unavailable(settings: Settings, error: ModelError) -> None:
    model = ScriptedModel([error, sql_reply(COUNT)])
    result = Assistant(settings, model).run_turn("A question", "c1")
    assert facts(result) == ("failed", "error", "model_unavailable")
    assert model.calls == 1, "a request that got no reply is not repeated"
    assert (result.meta.model_calls, result.meta.attempts) == (1, 0)


def test_model_output_when_the_shape_is_invalid_twice(settings: Settings) -> None:
    result = turn(settings, [TEXT_ONLY, tool_reply("run_sql", "decline")])
    assert facts(result) == ("failed", "error", "model_output")


@pytest.mark.parametrize("kind", ["refusal", "truncated"])
def test_a_refusal_or_a_cut_off_reply_is_not_repaired(settings: Settings, kind: str) -> None:
    stop_reason = "refusal" if kind == "refusal" else "max_tokens"
    error = ModelOutputError(
        "refusal" if kind == "refusal" else "truncated", tool_reply(stop_reason=stop_reason)
    )
    model = ScriptedModel([error, sql_reply(COUNT)])
    result = Assistant(settings, model).run_turn("A question", "c1")
    assert facts(result) == ("failed", "error", "model_output")
    assert model.calls == 1
    assert result.meta.input_tokens == 1000, "the tokens of the unusable reply are counted"


def test_a_missing_database_is_an_internal_failure_with_no_model_call(tmp_path: Path) -> None:
    settings = load_settings({"FSM_DB_PATH": str(tmp_path / "absent.db")}, env_file=None)
    model = ScriptedModel([sql_reply(COUNT)])
    result = Assistant(settings, model).run_turn("How many?", "c1")
    assert facts(result) == ("failed", "error", "internal")
    assert model.calls == 0


def test_a_database_that_becomes_unreadable_is_an_internal_failure_with_no_repair(
    settings: Settings, tmp_path: Path
) -> None:
    copy = tmp_path / "copy.db"
    shutil.copy(settings.db_path, copy)
    model = ScriptedModel([sql_reply(COUNT)] * 3)
    assistant = Assistant(settings.model_copy(update={"db_path": copy}), model)
    assert assistant.run_turn("How many?", "c1").outcome == "answered"

    copy.write_bytes(b"not a database " * 100)
    result = assistant.run_turn("How many?", "c1")
    assert facts(result) == ("failed", "error", "internal")
    assert "could not be read" in result.answer
    assert model.calls == 2, "no repair request is spent on a broken file"

    copy.unlink()
    assert facts(assistant.run_turn("How many?", "c1")) == ("failed", "error", "internal")


# --- The tool chosen and what was refused --------------------------------------------------


@pytest.mark.parametrize(
    ("replies", "outcome", "tool", "refused"),
    [
        ([sql_reply(COUNT)], "answered", "run_sql", []),
        ([rule_reply(RULE)], "rule_proposed", "propose_rule", []),
        ([tool_reply("ask_clarification")], "clarification", "ask_clarification", []),
        ([tool_reply("decline")], "cannot_answer", "decline", []),
        ([tool_reply("profile_fraud")], "answered", "profile_fraud", []),
        ([sql_reply(FORBIDDEN), sql_reply(COUNT)], "answered", "run_sql", ["table_not_allowed"]),
        ([sql_reply("DELETE FROM users"), tool_reply("decline")], "cannot_answer", "decline",
         ["not_read_only"]),
        ([sql_reply(FORBIDDEN), sql_reply(UNKNOWN_COLUMN)], "failed", "run_sql",
         ["table_not_allowed", "sqlite_error"]),
        ([rule_reply(LABEL_RULE), rule_reply(RULE)], "rule_proposed", "propose_rule",
         ["label_read"]),
        ([rule_reply(LABEL_RULE), rule_reply(LABEL_RULE)], "rejected", "propose_rule",
         ["label_read", "label_read"]),
        ([TEXT_ONLY, sql_reply(COUNT)], "answered", "run_sql", ["no_tool_call"]),
        ([TEXT_ONLY, tool_reply("run_sql", "decline")], "failed", None,
         ["no_tool_call", "multiple_tool_calls"]),
        ([sql_reply(OVERFLOW), sql_reply(COUNT)], "answered", "run_sql", []),
        ([sql_reply(ENDLESS), sql_reply(ENDLESS)], "failed", "run_sql", []),
        ([ModelUnavailable("timeout", "no reply")], "failed", None, []),
    ],
)  # fmt: skip
def test_the_result_names_the_tool_chosen_and_each_refusal(
    settings: Settings, replies: Script, outcome: Outcome, tool: str | None, refused: list[str]
) -> None:
    result = turn(settings, replies)
    assert result.outcome == outcome
    assert result.tool == tool
    assert list(result.refused) == refused


FENCED_DELETE = "```sql\nDELETE FROM transactions\n```"


@pytest.mark.parametrize(
    ("replies", "refused", "write_attempts"),
    [
        ([sql_reply(COUNT)], [], 0),
        ([sql_reply(FORBIDDEN), sql_reply(COUNT)], ["table_not_allowed"], 0),
        ([sql_reply(UNKNOWN_COLUMN), sql_reply(UNKNOWN_COLUMN)], ["sqlite_error", "sqlite_error"], 0),
        ([sql_reply("DELETE FROM cards"), tool_reply("decline")], ["not_read_only"], 1),
        # A write refused under a code that does not say "write".
        ([sql_reply(FENCED_DELETE), tool_reply("decline")], ["code_fence"], 1),
        ([sql_reply(FENCED_DELETE), sql_reply("EXPLAIN DELETE FROM cards")], ["code_fence", "explain"], 2),
        ([sql_reply("DELETE FROM nosuchtable"), sql_reply(COUNT)], ["sqlite_error"], 1),
        (
            [rule_reply("1=1); DELETE FROM cards; --"), rule_reply("id > 1")],
            ["clause_not_contained"],
            1,
        ),
    ],
)
def test_the_result_counts_the_refused_replies_that_tried_to_write(
    settings: Settings, replies: Script, refused: list[str], write_attempts: int
) -> None:
    result = turn(settings, replies)
    assert list(result.refused) == refused
    assert result.write_attempts == write_attempts


def test_write_attempts_belong_to_one_turn(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(FENCED_DELETE), sql_reply(FENCED_DELETE), sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    assert assistant.run_turn("Delete everything", "c1").write_attempts == 2
    assert saved(assistant, "c1")["write_attempts"] == 1  # the first; the second is in the failure
    assert assistant.run_turn("How many?", "c1").write_attempts == 0
    assert saved(assistant, "c1")["write_attempts"] == 0


def test_refusals_belong_to_one_turn(settings: Settings) -> None:
    model = ScriptedModel(
        [sql_reply(FORBIDDEN), sql_reply(FORBIDDEN), sql_reply(COUNT), tool_reply("decline")]
    )
    assistant = Assistant(settings, model)
    first = assistant.run_turn("Show the internals", "c1")
    assert first.refused == ("table_not_allowed", "table_not_allowed")
    assert saved(assistant, "c1")["refused"] == [ReasonCode.TABLE_NOT_ALLOWED]

    second = assistant.run_turn("How many?", "c1")
    assert (second.tool, second.refused) == ("run_sql", ())
    assert not saved(assistant, "c1")["refused"]
    assert assistant.run_turn("Delete them", "c1").tool == "decline"


def test_a_refusal_is_a_code_and_holds_no_text(settings: Settings) -> None:
    marked = "SELECT name AS zebra_marker FROM sqlite_master"
    result = turn(settings, [sql_reply(marked), sql_reply("SELECT zebra_marker FROM users")])
    assert result.refused == (ReasonCode.TABLE_NOT_ALLOWED, ReasonCode.SQLITE_ERROR)
    assert "zebra_marker" not in repr(result.refused)
    assert "refused: table_not_allowed, sqlite_error" in render_text(result)


# --- The one repair -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "quoted", "reason"),
    [
        (TEXT_ONLY, "(an empty reply)", "The reply called no tool."),
        (sql_reply(FORBIDDEN), FORBIDDEN, "sqlite_master"),
        (sql_reply(UNKNOWN_COLUMN), UNKNOWN_COLUMN, "no such column: amount"),
        (sql_reply(OVERFLOW), OVERFLOW, RUN_TIME_REASON),
        (sql_reply(ENDLESS), ENDLESS, "ran past the 0.3-second limit"),
    ],
)
def test_a_query_is_repaired_on_the_second_attempt(
    settings: Settings, first: ModelReply, quoted: str, reason: str
) -> None:
    model = ScriptedModel([first, sql_reply(COUNT), sql_reply(ADDRESSES)])
    result = Assistant(settings, model).run_turn("How many transactions?", "c1")

    assert result.outcome == "answered"
    assert result.sql == COUNT
    assert model.calls == 2
    assert (result.meta.attempts, result.meta.model_calls) == (2, 2)
    assert (result.meta.input_tokens, result.meta.output_tokens) == (2000, 200)

    original, repair = model.requests
    assert (original.turn, original.call, repair.turn, repair.call) == (1, 1, 1, 2)
    assert repair.system == original.system
    assert repair.user.startswith(original.user), "a fresh request: the original plus a paragraph"
    paragraph = repair.user[len(original.user) :]
    assert f"You replied:\n<previous_reply>\n{quoted}\n</previous_reply>" in paragraph
    assert reason in paragraph
    assert RULE_SCOPE not in paragraph


@pytest.mark.parametrize(
    ("clause", "reason"),
    [(OUT_OF_SCOPE_RULE, "no such column: card_type"), (LABEL_RULE, "fraud_labels")],
)
def test_a_rule_clause_is_repaired_with_the_scope_sentence(
    settings: Settings, clause: str, reason: str
) -> None:
    model = ScriptedModel([rule_reply(clause), rule_reply(RULE)])
    result = Assistant(settings, model).run_turn("A rule for big debit payments", "c1")

    assert result.outcome == "rule_proposed"
    assert result.where_clause == RULE
    paragraph = model.requests[1].user[len(model.requests[0].user) :]
    assert f"You replied:\n<previous_reply>\n{clause}\n</previous_reply>" in paragraph
    assert reason in paragraph
    assert RULE_SCOPE in paragraph


def test_a_clause_that_reads_the_labels_twice_is_rejected(settings: Settings) -> None:
    result = turn(settings, [rule_reply(LABEL_RULE), rule_reply(LABEL_RULE)])
    assert facts(result) == ("rejected", "rejected", "sql_rejected")
    assert result.rule is None and result.where_clause is None


def test_the_last_failure_decides_the_outcome(settings: Settings) -> None:
    result = turn(settings, [sql_reply(FORBIDDEN), sql_reply(OVERFLOW)])
    assert facts(result) == ("failed", "error", "sql_execution")


def test_a_repair_may_choose_a_different_tool(settings: Settings) -> None:
    result = turn(settings, [sql_reply(FORBIDDEN), call_reply("decline", reason="Not possible.")])
    assert result.outcome == "cannot_answer"


# --- State and memory ---------------------------------------------------------------------


def user_texts(model: ScriptedModel) -> list[str]:
    return [request.user for request in model.requests]


def test_a_follow_up_sees_the_previous_question_and_sql(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(COUNT), sql_reply(ADDRESSES)])
    assistant = Assistant(settings, model)
    assistant.run_turn("How many transactions?", "c1")
    assistant.run_turn("And the addresses?", "c1")

    first, second = user_texts(model)
    assert "Earlier in this conversation" not in first
    assert "The user asked:\n<earlier_question>\nHow many transactions?\n</earlier_question>" in second
    assert f"which ran:\n<earlier_sql>\n{COUNT}\n</earlier_sql>" in second
    assert second.endswith("Question:\n<question>\nAnd the addresses?\n</question>")
    assert (model.requests[1].turn, model.requests[1].call) == (2, 1)


def test_a_follow_up_sees_the_previous_rule_clause(settings: Settings) -> None:
    model = ScriptedModel([rule_reply(RULE), sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    assistant.run_turn("A rule", "c1")
    assistant.run_turn("How many does it flag?", "c1")
    assert f"<earlier_rule_clause>\n{RULE}\n</earlier_rule_clause>" in user_texts(model)[1]


def test_a_reply_to_a_clarification_is_an_ordinary_turn(settings: Settings) -> None:
    ask = call_reply(
        "ask_clarification", question="What is risky?", suggestions=["Over $1,000", "At night"]
    )
    model = ScriptedModel([ask, sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    assert assistant.run_turn("Show risky transactions", "c1").outcome == "clarification"
    result = assistant.run_turn("The first one", "c1")

    assert result.outcome == "answered"
    assert result.meta.turn == 2
    second = user_texts(model)[1]
    assert "The user asked:\n<earlier_question>\nShow risky transactions\n</earlier_question>" in second
    assert "<earlier_clarification>\nWhat is risky?\n</earlier_clarification>" in second
    assert "<earlier_suggestions>\nOver $1,000\nAt night\n</earlier_suggestions>" in second


def test_only_the_last_turn_is_remembered(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(COUNT), sql_reply(ADDRESSES), sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    for question in ("First question", "Second question", "Third question"):
        assistant.run_turn(question, "c1")
    third = user_texts(model)[2]
    assert "Second question" in third
    assert "First question" not in third
    history = saved(assistant, "c1")["history"]
    assert isinstance(history, list | tuple) and len(history) == 1


def test_another_conversation_starts_clean(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(COUNT), sql_reply(ADDRESSES)])
    assistant = Assistant(settings, model)
    assistant.run_turn("How many transactions?", "c1")
    other = assistant.run_turn("And the addresses?", "c2")
    assert other.meta.turn == 1
    assert "Earlier in this conversation" not in user_texts(model)[1]


def test_per_turn_fields_are_reset_by_the_next_turn(settings: Settings) -> None:
    model = ScriptedModel(
        [sql_reply(FORBIDDEN), sql_reply(ADDRESSES), call_reply("decline", reason="No.")]
    )
    assistant = Assistant(settings, model)
    assistant.run_turn("List the addresses", "c1")
    first = saved(assistant, "c1")
    assert (first["attempt"], first["model_calls"], first["approved_sql"]) == (2, 2, ADDRESSES)

    result = assistant.run_turn("Delete them", "c1")
    second = saved(assistant, "c1")
    assert (second["turn"], second["attempt"], second["model_calls"]) == (2, 1, 1)
    assert second["approved_sql"] is None
    assert second["outcome"] == "cannot_answer"
    assert (second["input_tokens"], result.meta.input_tokens) == (1000, 1000)
    assert result.sql is None and result.table is None


def test_an_invalid_message_runs_nothing_and_keeps_the_memory(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(COUNT), sql_reply(ADDRESSES)])
    assistant = Assistant(settings, model)
    assistant.run_turn("How many transactions?", "c1")
    empty = assistant.run_turn("  ", "c1")
    assert empty.sql is None and empty.table is None, "the first turn's approved query did not run"
    assistant.run_turn("And the addresses?", "c1")
    assert "The user asked:\n<earlier_question>\nHow many transactions?\n</earlier_question>" in user_texts(model)[1]


def test_a_failed_turn_keeps_the_remembered_turn(settings: Settings) -> None:
    model = ScriptedModel(
        [sql_reply(COUNT), sql_reply(FORBIDDEN), sql_reply(FORBIDDEN), sql_reply(ADDRESSES)]
    )
    assistant = Assistant(settings, model)
    assistant.run_turn("How many transactions?", "c1")
    remembered = saved(assistant, "c1")["history"]
    assert assistant.run_turn("Show the internals", "c1").outcome == "rejected"
    assert saved(assistant, "c1")["history"] == remembered
    assert assistant.run_turn("And the addresses?", "c1").meta.turn == 3

    follow_up = user_texts(model)[3]
    assert "The user asked:\n<earlier_question>\nHow many transactions?\n</earlier_question>" in follow_up
    assert f"<earlier_sql>\n{COUNT}\n</earlier_sql>" in follow_up
    assert "Show the internals" not in follow_up
    assert FORBIDDEN not in follow_up


def test_a_failed_first_turn_leaves_nothing_to_remember(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(FORBIDDEN), sql_reply(FORBIDDEN), sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    assistant.run_turn("Show the internals", "c1")
    assistant.run_turn("How many transactions?", "c1")
    assert user_texts(model)[2] == "Question:\n<question>\nHow many transactions?\n</question>"


@pytest.mark.parametrize(
    "unusable",
    [
        call_reply("decline", reason="The database is read-only."),
        ModelUnavailable("timeout", "no reply"),
    ],
    ids=["decline", "model unavailable"],
)
def test_a_turn_a_follow_up_cannot_build_on_does_not_overwrite_the_memory(
    settings: Settings, unusable: ModelReply | ModelError
) -> None:
    model = ScriptedModel([rule_reply(RULE), unusable, sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    assistant.run_turn("A rule", "c1")
    assert assistant.run_turn("Delete them", "c1").outcome in ("cannot_answer", "failed")
    assistant.run_turn("How many does it flag?", "c1")

    follow_up = user_texts(model)[-1]
    assert "The user asked:\n<earlier_question>\nA rule\n</earlier_question>" in follow_up
    assert f"<earlier_rule_clause>\n{RULE}\n</earlier_rule_clause>" in follow_up
    assert "Delete them" not in follow_up


def refused_by_the_serializer(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> list[str]:
    """Run a turn of every kind, read the state back, and return what the serializer blocked."""
    ask = call_reply("ask_clarification", question="Which?", suggestions=["a", "b"])
    script: Script = [
        sql_reply(FORBIDDEN), sql_reply(COUNT), rule_reply(RULE), ask,
        tool_reply("decline"), tool_reply("profile_fraud"), TEXT_ONLY, TEXT_ONLY,
        sql_reply(FORBIDDEN), sql_reply(FORBIDDEN),
        ModelOutputError("refusal", tool_reply(stop_reason="refusal")), sql_reply(COUNT),
    ]  # fmt: skip
    assistant = Assistant(settings, ScriptedModel(script))
    config = {"configurable": {"thread_id": "c1"}}
    with caplog.at_level(logging.DEBUG):
        for number in range(9):
            assistant.run_turn(f"Question {number}", "c1")
            assistant.graph.get_state(config)  # type: ignore[arg-type]  # a plain config dict
            list(assistant.graph.get_state_history(config))  # type: ignore[arg-type]  # the same
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("langgraph") and record.levelno >= logging.WARNING
    ]


def test_saving_the_state_meets_no_unregistered_type(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    assert refused_by_the_serializer(settings, caplog) == []


# `TurnInput` and `Repair` are listed too, but nothing saved today holds one on its
# own: the input is not checkpointed, and a `Repair` travels inside its `Failure`.
@pytest.mark.parametrize(
    "missing",
    [kind for kind in state.SAVED_TYPES if kind.__name__ not in ("TurnInput", "Repair")],
    ids=lambda kind: str(kind.__name__),
)
def test_a_saved_type_missing_from_the_list_is_noticed(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    missing: type,
) -> None:
    kept = tuple(kind for kind in state.SAVED_TYPES if kind is not missing)
    monkeypatch.setattr(graph, "SAVED_TYPES", kept)
    blocked = refused_by_the_serializer(settings, caplog)
    assert blocked, "the check above can fail"
    assert all(missing.__name__ in message for message in blocked)


def test_the_process_keeps_one_checkpoint_per_conversation(settings: Settings) -> None:
    first_sql = "SELECT 'zebra-sql' AS tag"
    turns = 12
    model = ScriptedModel(
        [
            sql_reply(first_sql),
            *[sql_reply(FORBIDDEN), sql_reply(COUNT), sql_reply(COUNT)] * turns,
            sql_reply(COUNT),
        ]
    )
    assistant = Assistant(settings, model)
    assistant.run_turn("A zebra-question", "c1")
    saver = assistant.saver

    def held() -> tuple[int, int, int]:
        checkpoints = sum(
            len(by_id) for spaces in saver.storage.values() for by_id in spaces.values()
        )
        return checkpoints, len(saver.blobs), len(saver.writes)

    after_one = held()
    assert "zebra-question" in repr((saver.storage, saver.blobs)), "the search finds a kept turn"
    for number in range(turns):
        assert assistant.run_turn(f"Question {number}", "c1").outcome == "answered"
        assistant.run_turn("Another conversation", f"other-{number % 2}")
    assert assistant.run_turn("The last question", "c1").meta.turn == turns + 2

    stored = repr((saver.storage, saver.writes, saver.blobs))
    assert "zebra-question" not in stored and "zebra-sql" not in stored
    assert "Question 3" not in stored
    assert held() == (3, after_one[1] * 3, 0), "one checkpoint for each of three conversations"
    history = list(assistant.graph.get_state_history({"configurable": {"thread_id": "c1"}}))
    assert len(history) == 1
    assert f"<earlier_question>\nQuestion {turns - 1}\n" in model.requests[-1].user


def test_each_assistant_owns_its_memory_and_the_modules_hold_none(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(COUNT), sql_reply(ADDRESSES), sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    first = assistant.run_turn("How many transactions?", "c1")
    second = assistant.run_turn("And the addresses?", "c1")
    assert (first.meta.turn, second.meta.turn) == (1, 2)

    assert Assistant(settings, model).run_turn("How many transactions?", "c1").meta.turn == 1
    assert user_texts(model)[2] == "Question:\n<question>\nHow many transactions?\n</question>"
    assert not hasattr(graph, "run_turn")
    for module in (graph, nodes):
        mutable = [
            name
            for name, value in vars(module).items()
            if isinstance(value, dict | list | set)
            and not name.startswith("__")
            and value is not PER_TURN_RESET  # a constant of `state`, read and never written
        ]
        assert mutable == [], f"{module.__name__} holds module-level mutable state"


def test_meta_describes_the_turn(settings: Settings) -> None:
    result = turn(settings, [sql_reply(COUNT)])
    meta = result.meta
    assert (meta.conversation_id, meta.turn, meta.model_id) == ("c1", 1, "scripted")
    assert (meta.prompt_version, meta.tool_mode) == (PROMPT_VERSION, "tools")
    assert (meta.attempts, meta.model_calls) == (1, 1)
    assert (meta.input_tokens, meta.output_tokens) == (1000, 100)
    assert meta.elapsed_ms >= 0


def test_the_request_carries_the_schema_context_and_the_question(settings: Settings) -> None:
    model = ScriptedModel([sql_reply(COUNT)])
    Assistant(settings, model).run_turn("  How many transactions?  ", "c1")
    request: ModelRequest = model.requests[0]
    assert "transactions (6 rows)" in request.system
    assert request.user == "Question:\n<question>\nHow many transactions?\n</question>"


# --- The diagram and the trace ------------------------------------------------------------


def test_the_diagram_is_drawn_from_the_compiled_graph(settings: Settings) -> None:
    diagram = Assistant(settings, ScriptedModel([])).mermaid()
    for node in NODE_NAMES:
        assert node in diagram
    assert "retry" not in diagram, "retry is a route, not a node"
    assert "guard_sql -. &nbsp;accepted&nbsp; .-> execute_sql" in diagram
    assert "guard_rule -. &nbsp;accepted&nbsp; .-> backtest_rule" in diagram


def test_a_trace_line_escapes_what_the_model_wrote() -> None:
    hostile = "SELECT 1 /* \x1b[2J\x1b[31m */ -- \r overwritten\nsecond line \ud800"
    reply = call_reply("run_sql\x1b[2J\ud800", sql=hostile)
    updates: list[tuple[NodeName, dict[str, object]]] = [
        ("call_model", {"raw_reply": reply, "model_calls": 1}),
        ("guard_sql", {"approved_sql": hostile}),
        ("guard_rule", {"approved_clause": hostile}),
        ("guard_sql", {"failure": Failure(kind="sql_rejected", message=f"Refused: {hostile}")}),
    ]
    for node, update in updates:
        line = describe_step(step_event(node, update))
        assert line.isprintable(), "no control character, line break or lone surrogate"
        line.encode("utf-8")
        assert "\\x1b[2J" in line


def test_the_trace_names_the_tool_and_the_refusal_code(settings: Settings) -> None:
    steps: list[TraceStep] = []
    model = ScriptedModel([sql_reply(FORBIDDEN), sql_reply(COUNT)])
    Assistant(settings, model).run_turn("How many?", "c1", on_step=steps.append)
    text = "\n".join(step.summary for step in steps)
    assert "tool chosen: run_sql" in text
    assert "failure sql_rejected (repairable), refusal code table_not_allowed" in text
    assert "outcome answered, status ok, tool run_sql" in text


def test_the_trace_names_each_node_and_shows_rows_as_a_count(settings: Settings) -> None:
    steps: list[TraceStep] = []
    model = ScriptedModel([sql_reply(FORBIDDEN), sql_reply(ADDRESSES)])
    Assistant(settings, model).run_turn("List the addresses", "c1", on_step=steps.append)

    assert [step.node for step in steps] == [
        "begin_turn", "call_model", "check_intent", "guard_sql",
        "call_model", "check_intent", "guard_sql", "execute_sql", "present", "remember",
    ]  # fmt: skip
    text = "\n".join(step.summary for step in steps)
    assert "failure sql_rejected (repairable)" in text
    assert f"accepted SQL: {ADDRESSES}" in text
    assert "2 row(s)" in text
    assert "outcome answered" in text
    assert ROW_MARKER not in text
