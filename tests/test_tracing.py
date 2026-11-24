"""Hosted tracing: off unless asked for, and what a trace may carry.

No test here can reach LangSmith. Most hand the tracer a recording sink; the
ones that build the real sink give its client a fake HTTP session, so the
bytes it would post are read and nothing is opened. Connections are refused
for the whole test process as well (`conftest.py`).
"""

import json
import socket
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import get_args

import langsmith
import pytest
from conftest import (
    RecordingSink,
    _refuse_connection,
    marked_evidence,
    never_outside_the_answer,
    rule_reply,
    sql_reply,
)
from langsmith import utils as langsmith_utils
from pydantic import ValidationError

from fsm_assistant import cli, nodes, tracing
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.events import Label, NodeName, StepEvent, TurnEvent, step_event
from fsm_assistant.graph import NODE_NAMES, Assistant, TraceStep
from fsm_assistant.llm import (
    ModelError,
    ModelReply,
    ModelUnavailable,
    ReplayModel,
    ScriptedModel,
)
from fsm_assistant.nl2sql import model_spec
from fsm_assistant.presentation import render_text
from fsm_assistant.result import RuleCard, RuleMonth
from fsm_assistant.tracing import (
    ALLOWED,
    ALLOWED_METADATA,
    Destination,
    LangSmithSink,
    RunKind,
    TraceRun,
    Tracer,
    TracingError,
)

ANTHROPIC_KEY = "sk-ant-not-a-real-key-0123456789"
LANGSMITH_KEY = "lsv2_pt_not-a-real-key-9876543210"
ENDPOINT = "https://langsmith.invalid"
COUNT = "SELECT count(*) FROM transactions"
TAGS = "SELECT 'row-' || id || '-marker' AS tag FROM users ORDER BY id"
FORBIDDEN = "SELECT name FROM sqlite_master"
RULE = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"
ADDRESS_IN_ERROR = "SELECT json_extract('{}', (SELECT address FROM users ORDER BY id LIMIT 1))"
AMOUNT_IN_RULE_ERROR = "json_extract('{}', merchant_id || ':' || amount_usd_cents) IS NULL"
# In rows, in a run-time message, in the fixture's system prompt, or in the settings only.
NEVER_SENT = ("row-1-marker", "Elm Street", "10:2500", ANTHROPIC_KEY, LANGSMITH_KEY)
LEAKY: list[ModelReply | ModelError] = [
    sql_reply(TAGS),
    sql_reply(ADDRESS_IN_ERROR),
    sql_reply(ADDRESS_IN_ERROR),
    rule_reply(AMOUNT_IN_RULE_ERROR),
    rule_reply(AMOUNT_IN_RULE_ERROR),
    rule_reply(RULE),
]


def settings_with(fixture_db: Path, tmp_path: Path, **variables: str) -> Settings:
    return load_settings(
        {
            "FSM_DB_PATH": str(fixture_db),
            "FSM_LOG_DIR": str(tmp_path / "logs"),
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            **variables,
        },
        env_file=None,
    )


@pytest.fixture
def settings(fixture_db: Path, tmp_path: Path) -> Settings:
    """Tracing asked for, as the owner's `.env` would."""
    return settings_with(
        fixture_db,
        tmp_path,
        LANGSMITH_TRACING="true",
        LANGSMITH_API_KEY=LANGSMITH_KEY,
        LANGSMITH_ENDPOINT=ENDPOINT,
    )


@pytest.fixture(autouse=True)
def no_thread_is_left_and_no_connection_is_possible() -> Iterator[None]:
    before = set(threading.enumerate())
    yield None
    left = [thread.name for thread in set(threading.enumerate()) - before if thread.is_alive()]
    assert not left, f"a test left a thread running: {left}"
    refused: object = _refuse_connection
    assert getattr(socket.socket, "connect") is refused, "connections are still refused"
    assert getattr(socket.socket, "connect_ex") is refused


class Warnings:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        self.lines.append(message)


ASK = Label(command="ask")


def traced(sink: RecordingSink, warn: Warnings | None = None) -> Tracer:
    return Tracer(sink, Destination(project="p"), warn=warn or Warnings())


def forbid_any_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make building or using a LangSmith client an error."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a LangSmith client was built or used")

    for name in ("__init__", "create_run", "update_run", "batch_ingest_runs", "multipart_ingest"):
        monkeypatch.setattr(langsmith.Client, name, forbidden)


class FakeResponse:
    status_code = 202
    text = "{}"
    content = b"{}"
    reason = "Accepted"
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {}

    def close(self) -> None:
        return None


class FakeSession:
    """Stands in for the HTTP session of the LangSmith client: records, opens nothing."""

    def __init__(self, fault: Exception | None = None, gate: threading.Event | None = None) -> None:
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.headers: dict[str, str] = {}
        self._fault = fault
        self._gate = gate

    def mount(self, prefix: str, adapter: object) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        if self._gate is not None:
            self._gate.wait(5)
        if self._fault is not None:
            raise self._fault
        data, headers = kwargs["data"], kwargs["headers"]
        assert isinstance(data, bytes) and isinstance(headers, dict)
        self.requests.append((method, url, headers, data))
        return FakeResponse()

    def close(self) -> None:
        return None


# --- The events a trace is made from -------------------------------------------------------


def test_events_are_frozen_closed_and_carry_no_clock() -> None:
    step = step_event("guard_sql", {"approved_sql": COUNT, "reads_labels": False})
    assert (step.kind, step.guard, step.sql, step.reads_labels) == ("step", "accepted", COUNT, False)
    with pytest.raises(ValidationError):
        step.node = "present"  # type: ignore[misc]  # assigning to a frozen field is what is tested
    fields = step.model_dump()
    changes: list[dict[str, object]] = [{"node": "somewhere"}, {"kind": "turn"}, {"rows": [[1]]}]
    for change in changes:
        with pytest.raises(ValidationError):
            StepEvent.model_validate({**fields, **change})
    names = set(StepEvent.model_fields) | set(TurnEvent.model_fields)
    assert not [name for name in names if "time" in name or name in ("ts", "date", "at")]
    assert not names & {"rows", "table", "result", "rule", "answer", "detail", "failure_detail"}
    assert get_args(NodeName) == NODE_NAMES


# --- Off unless asked for -----------------------------------------------------------------


def test_tracing_is_off_by_default(
    fixture_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_any_client(monkeypatch)
    warn = Warnings()
    for variables in (
        {},
        {"LANGSMITH_TRACING": "true"},
        {"LANGSMITH_API_KEY": LANGSMITH_KEY},
        {"LANGSMITH_TRACING": "false", "LANGSMITH_API_KEY": LANGSMITH_KEY},
    ):
        settings = settings_with(fixture_db, tmp_path, **variables)
        assert not tracing.wanted(settings)
        assert tracing.start(settings, warn) is None
        assert tracing.describe(None, settings) == "Hosted tracing: off."
    assert warn.lines == []


def test_stray_environment_variables_do_not_switch_tracing_on(
    fixture_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LangGraph would trace the whole run, state and rows included, on these variables alone."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_API_KEY", LANGSMITH_KEY)
    monkeypatch.setenv("LANGSMITH_PROJECT", "stray")
    # Nowhere to send a trace even if the switch under test were broken.
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    cached = getattr(langsmith_utils.get_env_var, "cache_clear")
    cached()
    forbid_any_client(monkeypatch)
    try:
        assert langsmith_utils.tracing_is_enabled() is True, "the environment switch is live"
        settings = settings_with(fixture_db, tmp_path)
        seen: list[object] = []

        def watch(step: TraceStep) -> None:
            seen.append(langsmith_utils.tracing_is_enabled())

        tracer = tracing.start(settings)
        assert tracer is None
        result = Assistant(settings, ScriptedModel([sql_reply(TAGS)]), tracer=tracer).run_turn(
            "List them", "c1", on_step=watch
        )
        assert result.outcome == "answered"
        assert seen == [False] * 7, "tracing is off inside the graph run"
    finally:
        cached()


# --- On: one filtered trace per turn ------------------------------------------------------


def test_a_turn_is_one_trace_with_its_nodes_in_order(settings: Settings) -> None:
    sink = RecordingSink()
    shown: list[TraceStep] = []
    assistant = Assistant(
        settings, ScriptedModel([sql_reply(FORBIDDEN), sql_reply(COUNT), sql_reply(COUNT)]),
        label=ASK, tracer=traced(sink),
    )  # fmt: skip
    first = assistant.run_turn("How many?", "conversation-1", on_step=shown.append)
    assistant.run_turn("And again?", "conversation-1")

    assert len(sink.batches) == 2, "one trace per turn"
    runs = sink.batches[0]
    root, children = runs[0], runs[1:]
    assert (root.kind, root.name, root.run_type, root.parent_run_id) == (
        "turn", "ask turn 1", "chain", None,
    )  # fmt: skip
    assert {run.trace_id for run in runs} == {root.id}
    steps = [run for run in children if run.kind == "step"]
    assert [run.name for run in steps] == [step.node for step in shown]
    assert all(run.parent_run_id == root.id and run.run_type == "chain" for run in steps)
    assert [run.dotted_order for run in runs] == sorted(run.dotted_order for run in runs)
    assert all(run.dotted_order.startswith(root.dotted_order) for run in runs)
    assert all(root.start_time <= run.start_time <= run.end_time <= root.end_time for run in runs)
    assert root.end_time - root.start_time >= timedelta(milliseconds=first.meta.elapsed_ms)
    assert [run.start_time for run in runs] == sorted({run.start_time for run in runs})

    calls = [run for run in runs if run.kind == "model"]
    requests = [run for run in steps if run.name == "call_model"]
    assert [run.parent_run_id for run in calls] == [run.id for run in requests]
    assert all(run.run_type == "llm" and run.name == "scripted" for run in calls)
    assert calls[0].outputs["usage_metadata"] == {
        "input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
    }  # fmt: skip
    assert (calls[0].metadata["ls_provider"], calls[0].metadata["ls_model_name"]) == (
        "anthropic", "scripted",
    )  # fmt: skip
    assert FORBIDDEN in json.dumps(calls[0].outputs), "the tool call is in the trace"
    assert "How many?" in json.dumps(calls[0].inputs)

    refused = next(run for run in steps if run.name == "guard_sql")
    assert refused.error == "sql_rejected: table_not_allowed"
    assert refused.outputs["guard"] == "rejected" and refused.outputs["reason_code"] == "table_not_allowed"
    assert root.inputs == {"question": "How many?", "question_chars": 9}
    assert root.outputs["outcome"] == "answered" and root.outputs["sql"] == COUNT
    assert root.outputs["row_count"] == 1 and root.outputs["columns"] == ["count(*)"]
    assert root.outputs["refused"] == ["table_not_allowed"] and root.error is None

    spec = model_spec(settings)
    for run in sink.runs:
        assert set(run.tags) == {
            "command:ask", "model:scripted", f"prompt:{spec.prompt_version}", "mode:tools",
        }  # fmt: skip
        assert run.metadata["session_id"] == "conversation-1", "what groups a conversation"
        assert run.metadata["prompt_version"] == spec.prompt_version
    assert [batch[0].name for batch in sink.batches] == ["ask turn 1", "ask turn 2"]
    assert [batch[0].metadata["turn"] for batch in sink.batches] == [1, 2]
    assert sink.batches[0][0].trace_id != sink.batches[1][0].trace_id


def test_every_payload_passes_the_allow_list_and_carries_no_data(settings: Settings) -> None:
    sink = RecordingSink()
    assistant = Assistant(settings, ScriptedModel(LEAKY), label=ASK, tracer=traced(sink))
    results = [assistant.run_turn(f"Question {number}", "c1") for number in range(1, 5)]

    assert [result.outcome for result in results] == ["answered", "failed", "failed", "rule_proposed"]
    shown = "".join(result.model_dump_json() for result in results)
    for value in NEVER_SENT[:3]:
        assert value in shown, "the user is shown it, so the search below means something"
    assert len(sink.batches) == 4 and {run.kind for run in sink.runs} == set(get_args(RunKind))
    for run in sink.runs:
        inputs, outputs = ALLOWED[run.kind]
        assert run.inputs.keys() <= inputs and run.outputs.keys() <= outputs
        assert run.metadata.keys() <= ALLOWED_METADATA
        assert set(run.model_dump()) == set(TraceRun.model_fields)
    text = sink.text()
    assert TAGS in text and RULE in text and "Question 1" in text
    assert '"row_count":2' in text and "sql_execution" in text
    for value in NEVER_SENT:
        assert value not in text, f"{value!r} would have been sent"
    assert "transactions (6 rows)" not in text, "the system prompt goes as a hash by default"
    assert "system prompt not sent: version" in text


def test_a_run_cannot_be_built_with_a_key_off_the_allow_list(settings: Settings) -> None:
    sink = RecordingSink()
    Assistant(settings, ScriptedModel([sql_reply(COUNT)]), label=ASK, tracer=traced(sink)).run_turn("q", "c")
    for run in sink.runs:
        fields = run.model_dump()
        changes: list[dict[str, object]] = [
            {"inputs": {**run.inputs, "state": "x"}},
            {"outputs": {**run.outputs, "rows": [[1]]}},
            {"outputs": {**run.outputs, "result": "x"}},
            {"metadata": {**run.metadata, "LANGSMITH_PROJECT": "x"}},
            {"extra": {}},
        ]
        for change in changes:
            with pytest.raises(ValidationError):
                TraceRun.model_validate({**fields, **change})
    assert not {"rows", "result", "response", "failure_detail", "detail", "answer", "rule"} & (
        ALLOWED["turn"][1] | ALLOWED["step"][1] | ALLOWED["model"][1]
    )


def test_a_rule_turn_sends_the_clause_and_caveat_codes_and_no_figure_of_its_evidence(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nodes, "rule_evidence", marked_evidence)
    sink = RecordingSink()
    shown: list[TraceStep] = []
    assistant = Assistant(settings, ScriptedModel([rule_reply(RULE)]), label=ASK, tracer=traced(sink))
    result = assistant.run_turn("A rule for big debit payments", "c1", on_step=shown.append)

    card = result.rule
    assert card is not None and card.weakest_month == "2019-09" and len(card.months) == 3
    figures = never_outside_the_answer(card)
    answer = result.model_dump_json() + render_text(result)
    assert len(figures) > 60
    for figure in ("5,831,329", "5831329", "2019-09", "3.80%", "0.03800", "weakest_month"):
        assert figure in figures and figure in answer, "shown to the user, so the search means something"

    # Ids and clock times aside (digits code made), all that would leave the process.
    runs = sink.runs
    sent = json.dumps(
        [(run.name, run.inputs, run.outputs, run.error, run.tags, run.metadata) for run in runs]
    )
    lines = "\n".join(step.summary for step in shown)
    for figure in figures:
        assert figure not in sent, f"{figure!r} would have been sent"
        assert figure not in lines, f"{figure!r} would have been printed by --trace"
    evidence_names = set(RuleCard.model_fields) | set(RuleMonth.model_fields) | {"evidence"}
    for run in runs:
        assert not (run.inputs.keys() | run.outputs.keys()) & (evidence_names - {"where_clause"})
    assert not (evidence_names - {"where_clause"}) & (
        ALLOWED["turn"][1] | ALLOWED["step"][1] | ALLOWED["model"][1]
    )

    root = runs[0]
    measured = next(run for run in runs if run.name == "backtest_rule")
    assert measured.inputs.keys() == {"seq", "attempt", "model_calls"}
    assert measured.outputs.keys() == {"duration_ms", "query_ms"}
    assert measured.outputs["query_ms"] == 1750, "the two statements together, and only their time"
    assert "backtest ran in 1.75 s" in lines
    assert root.outputs["outcome"] == "rule_proposed" and root.outputs["where_clause"] == RULE
    assert root.outputs["reads_labels"] is True and "row_count" not in root.outputs
    codes = [caveat.code for caveat in result.caveats]
    assert {"weak_month", "period_drop"} <= set(codes), "the two caveats of the evidence by month"
    assert root.outputs["caveats"] == codes
    assert next(run for run in runs if run.name == "present").outputs["caveats"] == codes
    assert "caveats: " + ", ".join(codes) in lines


def test_a_failure_sentence_code_did_not_write_is_not_sent(
    settings: Settings, tmp_path: Path
) -> None:
    sink = RecordingSink()
    script: list[ModelReply | ModelError] = [ModelUnavailable("connection", "provider-words")]
    Assistant(settings, ScriptedModel(script), tracer=traced(sink)).run_turn("q", "c1")
    nowhere = settings.model_copy(update={"db_path": tmp_path / "missing-database.db"})
    Assistant(nowhere, ScriptedModel([]), tracer=traced(sink)).run_turn("q", "c2")

    text = sink.text()
    assert [batch[0].error for batch in sink.batches] == ["model_unavailable", "internal"]
    assert "ModelUnavailable: connection" in text
    assert "provider-words" not in text and "missing-database" not in text
    assert "failure_message" not in text


def test_the_system_prompt_is_sent_only_when_asked_for(settings: Settings) -> None:
    sink = RecordingSink()
    tracer = Tracer(sink, Destination(project="p"), system_prompt="full")
    Assistant(settings, ScriptedModel([sql_reply(COUNT)]), tracer=tracer).run_turn("q", "c1")
    text = sink.text()
    assert "transactions (6 rows)" in text
    assert ANTHROPIC_KEY not in text and LANGSMITH_KEY not in text and "row-1-marker" not in text


# --- Tracing never fails a turn -----------------------------------------------------------


def test_a_failing_tracer_does_not_fail_the_turn_and_warns_once(settings: Settings) -> None:
    warn = Warnings()
    sink = RecordingSink(fault=RuntimeError(f"cannot send with {LANGSMITH_KEY}"))
    assistant = Assistant(
        settings, ScriptedModel([sql_reply(COUNT), sql_reply(COUNT)]), label=ASK, tracer=traced(sink, warn)
    )
    results = [assistant.run_turn("How many?", "c1"), assistant.run_turn("Again?", "c1")]
    assert [result.outcome for result in results] == ["answered", "answered"]
    assert len(warn.lines) == 1 and "hosted tracing failed (RuntimeError)" in warn.lines[0]
    assert LANGSMITH_KEY not in warn.lines[0]


def test_tracing_that_cannot_be_set_up_stays_off_with_one_warning(
    fixture_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": LANGSMITH_KEY}
    for variables, reason in (
        ({"LANGSMITH_AGENT_ID": "fsm"}, "must be set together"),
        ({"LANGSMITH_AGENT_ENVIRONMENT": "local"}, "must be set together"),
        ({"LANGSMITH_AGENT_ID": "fsm", "LANGSMITH_AGENT_ENVIRONMENT": "moon"}, "environment"),
        (
            {"LANGSMITH_AGENT_ID": "fsm", "LANGSMITH_AGENT_ENVIRONMENT": "local",
             "LANGSMITH_PROJECT": "p"},
            "two destinations",
        ),
    ):  # fmt: skip
        settings = settings_with(fixture_db, tmp_path, **on, **variables)
        with pytest.raises(TracingError, match=reason):
            tracing.destination(settings)
        warn = Warnings()
        assert tracing.start(settings, warn) is None
        assert len(warn.lines) == 1 and "stays off" in warn.lines[0] and reason in warn.lines[0]
        assert LANGSMITH_KEY not in warn.lines[0]

    def broken(self: object, **kwargs: object) -> None:
        raise RuntimeError(f"bad client {kwargs}")

    monkeypatch.setattr(langsmith.Client, "__init__", broken)
    warn = Warnings()
    assert tracing.start(settings_with(fixture_db, tmp_path, **on), warn) is None
    assert warn.lines == ["Warning: hosted tracing is not set up and stays off: RuntimeError."]


def test_the_destination_is_a_project_or_an_agent(fixture_db: Path, tmp_path: Path) -> None:
    assert tracing.destination(settings_with(fixture_db, tmp_path)) == Destination(
        project="fsm-assistant"
    )
    named = settings_with(fixture_db, tmp_path, LANGSMITH_PROJECT="mine")
    assert tracing.destination(named).describe() == "project 'mine'"
    agent = settings_with(
        fixture_db, tmp_path, LANGSMITH_AGENT_ID="fsm-assistant", LANGSMITH_AGENT_ENVIRONMENT="Local"
    )
    assert tracing.destination(agent) == Destination(
        agent_id="fsm-assistant", agent_environment="local"
    )
    assert tracing.destination(agent).describe() == "agent 'fsm-assistant', environment 'local'"


# --- The real sink, with a fake HTTP session ----------------------------------------------


def test_the_sink_posts_one_filtered_request_per_turn_to_the_configured_address(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Variables LangSmith's client reads on its own: none may change where a run goes.
    monkeypatch.setenv("LANGSMITH_PROJECT", "stray-project")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("LANGSMITH_API_KEY", "stray-key")
    monkeypatch.setenv("LANGSMITH_WORKSPACE_ID", "stray-workspace")
    monkeypatch.setenv("LANGSMITH_HIDE_INPUTS", "true")
    monkeypatch.setenv("LANGSMITH_TRACING_SAMPLING_RATE", "0")
    session, warn = FakeSession(), Warnings()
    target = tracing.destination(settings)
    tracer = Tracer(LangSmithSink(settings, target, session=session), target, warn=warn)
    assistant = Assistant(settings, ScriptedModel(LEAKY[:3]), label=ASK, tracer=tracer)
    assistant.run_turn("Question 1", "c1")
    assistant.run_turn("Question 2", "c1")
    tracer.close()

    assert warn.lines == []
    assert [(method, url) for method, url, _, _ in session.requests] == [
        ("POST", f"{ENDPOINT}/runs/batch")
    ] * 2
    for _, _, headers, body in session.requests:
        assert headers["x-api-key"] == LANGSMITH_KEY, "the key goes in its header and nowhere else"
        assert not {name.lower() for name in headers} & {"x-tenant-id", "authorization"}
        text = body.decode("utf-8")
        for value in (*NEVER_SENT, "stray"):
            assert value not in text, f"{value!r} would have been sent"
        posted = json.loads(text)
        assert set(posted) == {"post"}
        for run in posted["post"]:
            assert set(run) <= {
                "id", "trace_id", "parent_run_id", "dotted_order", "name", "run_type",
                "start_time", "end_time", "inputs", "outputs", "error", "tags", "extra",
                "session_name",
            }  # fmt: skip
            assert run["session_name"] == "fsm-assistant"
            assert set(run["extra"]) == {"metadata"}, "no runtime or environment details added"
            assert set(run["extra"]["metadata"]) <= ALLOWED_METADATA
            assert run["inputs"], "inputs are sent as built, not hidden or replaced"
    first = json.loads(session.requests[0][3])["post"]
    assert [run["name"] for run in first] == [
        "ask turn 1", "begin_turn", "call_model", "scripted", "check_intent", "guard_sql",
        "execute_sql", "present", "remember",
    ]  # fmt: skip
    assert first[0]["outputs"]["row_count"] == 2 and TAGS in first[0]["outputs"]["sql"]


def test_the_sink_addresses_an_agent_when_one_is_configured(
    fixture_db: Path, tmp_path: Path
) -> None:
    settings = settings_with(
        fixture_db, tmp_path, LANGSMITH_TRACING="true", LANGSMITH_API_KEY=LANGSMITH_KEY,
        LANGSMITH_AGENT_ID="fsm-assistant", LANGSMITH_AGENT_ENVIRONMENT="local",
    )  # fmt: skip
    session = FakeSession()
    target = tracing.destination(settings)
    tracer = Tracer(LangSmithSink(settings, target, session=session), target, warn=Warnings())
    Assistant(settings, ScriptedModel([sql_reply(COUNT)]), tracer=tracer).run_turn("q", "c1")
    tracer.close()

    ((method, url, _, body),) = session.requests
    assert (method, url) == ("POST", "https://api.smith.langchain.com/runs/batch")
    for run in json.loads(body)["post"]:
        assert run["address"] == "lrn:agents/fsm-assistant/environments/local"
        assert "session_name" not in run
    assert tracing.describe(tracer, settings) == (
        "Hosted tracing: on, to LangSmith agent 'fsm-assistant', environment 'local' at "
        "https://api.smith.langchain.com; result rows are never sent."
    )


def test_a_delivery_that_fails_warns_once_and_the_turns_go_on(settings: Settings) -> None:
    session = FakeSession(fault=ConnectionError(f"refused, key {LANGSMITH_KEY}"))
    warn = Warnings()
    target = tracing.destination(settings)
    tracer = Tracer(LangSmithSink(settings, target, session=session), target, warn=warn)
    assistant = Assistant(settings, ScriptedModel([sql_reply(COUNT)] * 2), tracer=tracer)
    results = [assistant.run_turn("How many?", "c1"), assistant.run_turn("Again?", "c1")]
    tracer.close()

    assert [result.outcome for result in results] == ["answered", "answered"]
    assert len(warn.lines) == 1 and "hosted tracing failed" in warn.lines[0]
    assert LANGSMITH_KEY not in warn.lines[0]


def test_closing_waits_a_bounded_time_for_pending_traces(settings: Settings) -> None:
    gate = threading.Event()
    session, warn = FakeSession(gate=gate), Warnings()
    target = tracing.destination(settings)
    sink = LangSmithSink(settings, target, session=session)
    tracer = Tracer(sink, target, warn=warn)
    Assistant(settings, ScriptedModel([sql_reply(COUNT)]), tracer=tracer).run_turn("q", "c1")

    assert sink.flush(0.05) is False, "the request is still waiting"
    gate.set()
    assert sink.flush(5.0) is True
    tracer.close()
    assert warn.lines == [] and len(session.requests) == 1


# --- Replay never traces ------------------------------------------------------------------


def test_a_replayed_turn_is_never_traced_and_builds_no_client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forbid_any_client(monkeypatch)
    assert tracing.wanted(settings)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "_live_model", lambda spec, settings: pytest.fail("no live model"))
    recordings = tmp_path / "recordings"
    assert not isinstance(ReplayModel(model_spec(settings), recordings, "ask"), ScriptedModel)
    assert cli.main(["ask", "--replay-dir", str(recordings), "How many?"]) == 1
    output = capsys.readouterr()
    assert "Hosted tracing: off (replayed replies are never traced)." in output.out
    assert "stale recording" in output.out and output.err == ""


def test_ask_says_where_traces_go_and_never_prints_the_key(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sink = RecordingSink()

    def start(settings: Settings, warn: object = None) -> Tracer:
        return Tracer(sink, tracing.destination(settings))

    monkeypatch.setattr(tracing, "start", start)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    model = ScriptedModel([sql_reply(COUNT)])
    monkeypatch.setattr(cli, "_live_model", lambda spec, settings: model)

    assert cli.main(["ask", "How many?"]) == 0
    output = capsys.readouterr()
    assert (
        f"Hosted tracing: on, to LangSmith project 'fsm-assistant' at {ENDPOINT}; "
        "result rows are never sent." in output.out
    )
    assert LANGSMITH_KEY not in output.out + output.err
    assert len(sink.batches) == 1 and sink.closed, "the trace is flushed when ask ends"
    assert sink.batches[0][0].name == "ask turn 1"
