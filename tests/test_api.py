"""The HTTP API and the `serve` command, with a scripted model in place of Claude.

Every request goes through Starlette's in-process test client: the
application is called directly, no port is opened and no connection is made,
so the process-wide refusal of connections (`conftest.py`) stays in force and
is checked after each test.
"""

import logging
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from conftest import RecordingSink, _refuse_connection, call_reply, rule_reply, sql_reply
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scan_fixtures import MAIN, build
from test_tracing import LEAKY, NEVER_SENT

from fsm_assistant import api, applog, cli, config, demo, tracing
from fsm_assistant.api import ApiError, Health, create_app
from fsm_assistant.applog import LOG_FILE
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.events import Label
from fsm_assistant.graph import Assistant
from fsm_assistant.llm import (
    IntentModel,
    ModelError,
    ModelReply,
    ModelRequest,
    ModelSpec,
    ModelUnavailable,
    ScriptedModel,
)
from fsm_assistant.presentation import EVIDENCE_NOTE
from fsm_assistant.result import Outcome, Status, TurnResult
from fsm_assistant.tracing import Destination, Tracer

KEY = "sk-ant-not-a-real-key-0123456789"
LANGSMITH_KEY = "lsv2_pt_not-a-real-key-9876543210"
HOST = "http://127.0.0.1"
COUNT = "SELECT count(*) FROM transactions"
TAGS = "SELECT 'row-' || id || '-marker' AS tag FROM users ORDER BY id"
ROW_MARKER = "row-1-marker"
FORBIDDEN = "SELECT name FROM sqlite_master"
RULE = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"
QUESTION = "A question with question-marker-71 in it"
SCAN = call_reply("profile_fraud")
CLARIFY = call_reply("ask_clarification", question="Which year?", suggestions=["2018", "2019"])
DECLINE = call_reply("decline", reason="The data holds no forecast.")
UNAVAILABLE = ModelUnavailable("timeout", "The model did not reply in time.")

type Script = list[ModelReply | ModelError]


@pytest.fixture(autouse=True)
def no_thread_is_left_and_no_connection_is_possible() -> Iterator[None]:
    before = set(threading.enumerate())
    yield None
    left = [thread.name for thread in set(threading.enumerate()) - before if thread.is_alive()]
    assert not left, f"a test left a thread running: {left}"
    refused: object = _refuse_connection
    assert getattr(socket.socket, "connect") is refused, "connections are still refused"
    assert getattr(socket.socket, "connect_ex") is refused


def settings_with(db_path: Path, tmp_path: Path, **variables: str) -> Settings:
    return load_settings(
        {
            "FSM_DB_PATH": str(db_path),
            "FSM_LOG_DIR": str(tmp_path / "logs"),
            "ANTHROPIC_API_KEY": KEY,
            **variables,
        },
        env_file=None,
    )


@pytest.fixture
def settings(fixture_db: Path, tmp_path: Path) -> Settings:
    return settings_with(fixture_db, tmp_path)


@pytest.fixture
def no_page(tmp_path: Path) -> Path:
    """A folder where no interface was built, whatever the checkout holds."""
    return tmp_path / "no-dist"


def serve(
    settings: Settings,
    script: Script,
    *,
    dist: Path,
    tracer: Tracer | None = None,
    busy_wait_seconds: float = 5.0,
) -> tuple[TestClient, ScriptedModel]:
    model = ScriptedModel(script)
    assistant = Assistant(settings, model, label=Label(command="web"), tracer=tracer)
    app = create_app(
        settings, assistant, tracer=tracer, dist=dist, busy_wait_seconds=busy_wait_seconds
    )
    return TestClient(app, base_url=HOST), model


def ask(client: TestClient, message: str = QUESTION, conversation: str = "c1") -> TurnResult:
    response = client.post("/api/chat", json={"conversation_id": conversation, "message": message})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    return TurnResult.model_validate_json(response.content)


def refusal(response_text: str) -> ApiError:
    return ApiError.model_validate_json(response_text)


# --- One turn per outcome -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "outcome", "status"),
    [
        ([sql_reply(COUNT)], "answered", "ok"),
        ([rule_reply(RULE)], "rule_proposed", "ok"),
        ([SCAN], "answered", "ok"),
        ([CLARIFY], "clarification", "needs_clarification"),
        ([DECLINE], "cannot_answer", "cannot_answer"),
        ([sql_reply(FORBIDDEN), sql_reply(FORBIDDEN)], "rejected", "rejected"),
        ([UNAVAILABLE], "failed", "error"),
    ],
)
def test_every_outcome_is_a_200_whose_body_is_the_turn_result(
    settings: Settings, no_page: Path, script: Script, outcome: Outcome, status: Status
) -> None:
    client, model = serve(settings, script, dist=no_page)
    result = ask(client)
    assert (result.outcome, result.status) == (outcome, status)
    assert result.meta.conversation_id == "c1" and result.meta.turn == 1
    assert model.calls == len(script)


def test_the_body_is_exactly_what_the_model_writes_of_itself(
    settings: Settings, no_page: Path
) -> None:
    client, _ = serve(settings, [sql_reply(TAGS, "An assumption")], dist=no_page)
    response = client.post("/api/chat", json={"conversation_id": "c1", "message": QUESTION})
    result = TurnResult.model_validate_json(response.content)
    assert response.text == result.model_dump_json()
    assert set(response.json()) == set(TurnResult.model_fields), "every field, defaults included"
    assert result.table is not None and result.table.rows[0] == (ROW_MARKER,)
    assert result.sql == TAGS and result.assumptions == ("An assumption",)
    assert result.tool == "run_sql" and result.meta.model_calls == 1


def test_a_rule_turn_carries_its_evidence_by_month(settings: Settings, no_page: Path) -> None:
    client, _ = serve(settings, [rule_reply("id = 1002")], dist=no_page)
    result = ask(client)
    assert result.where_clause == "id = 1002" and result.rule is not None
    assert [row.month for row in result.rule.months if row.flagged] == ["2019-02"]
    assert result.rule.true_positives == 1 and result.rule.lift == 4.0
    assert result.rule.earlier.months + result.rule.later.months == len(result.rule.months)


def test_a_scan_turn_carries_its_table_notes_and_follow_up_questions(
    tmp_path: Path, no_page: Path
) -> None:
    settings = settings_with(build(tmp_path / "scan.db", MAIN), tmp_path)
    client, _ = serve(settings, [SCAN], dist=no_page)
    result = ask(client, "Where is fraud concentrated?")
    assert result.tool == "profile_fraud" and result.table is not None and result.notes
    assert result.table.rows[0][1] == "Rome, Italy"
    assert result.suggestions[0].startswith("Propose a rule for transactions with ")
    assert {caveat.code for caveat in result.caveats} >= {"label_coverage"}


def test_a_failure_inside_the_assistant_is_not_a_server_error(
    settings: Settings, no_page: Path
) -> None:
    client, _ = serve(settings, [], dist=no_page)  # the script runs out: an unexpected fault
    result = ask(client)
    assert result.outcome == "failed" and result.error is not None


# --- Memory -------------------------------------------------------------------------------


def test_a_follow_up_sees_the_previous_turn_and_another_conversation_does_not(
    settings: Settings, no_page: Path
) -> None:
    client, model = serve(settings, [sql_reply(COUNT)] * 3, dist=no_page)
    ask(client, "How many transactions?", "c1")
    second = ask(client, "And only the online ones?", "c1")
    other = ask(client, "And only the online ones?", "c2")

    assert (second.meta.turn, other.meta.turn) == (2, 1)
    follow_up, fresh = model.requests[1].user, model.requests[2].user
    assert "How many transactions?" in follow_up and COUNT in follow_up
    assert "How many transactions?" not in fresh and "Earlier in this conversation" not in fresh


# --- Requests that are refused -------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"message": "How many?"}, "conversation_id"),
        ({"conversation_id": "c1"}, "message"),
        ({"conversation_id": "c1", "message": "How many?", "extra": 1}, "extra"),
        ({"conversation_id": "c1", "message": 7}, "message"),
        ({"conversation_id": "", "message": "How many?"}, "conversation_id"),
        ({"conversation_id": "a b/c", "message": "How many?"}, "conversation_id"),
        ({"conversation_id": "c" * 65, "message": "How many?"}, "conversation_id"),
        ({"conversation_id": "c1", "message": ""}, "message"),
        ({"conversation_id": "c1", "message": " \n\t "}, "message"),
        ({"conversation_id": "c1", "message": "secret-in-message " * 3}, "message"),
        (["not", "an", "object"], "body"),
    ],
)
def test_a_body_that_is_not_a_request_is_a_422_and_runs_nothing(
    fixture_db: Path, tmp_path: Path, no_page: Path, body: object, field: str
) -> None:
    settings = settings_with(fixture_db, tmp_path, FSM_MAX_MESSAGE_CHARACTERS="20")
    client, model = serve(settings, [sql_reply(COUNT)], dist=no_page)
    response = client.post("/api/chat", json=body)
    assert response.status_code == 422
    error = refusal(response.text)
    assert error.code == "invalid_request" and error.message
    assert [problem.field for problem in error.fields] == [field]
    assert "secret-in-message" not in response.text, "the value sent is not echoed"
    assert model.calls == 0


def test_the_length_limit_is_the_one_of_the_settings(
    fixture_db: Path, tmp_path: Path, no_page: Path
) -> None:
    settings = settings_with(fixture_db, tmp_path, FSM_MAX_MESSAGE_CHARACTERS="20")
    client, model = serve(settings, [sql_reply(COUNT)], dist=no_page)
    assert ask(client, "  " + "x" * 20 + "  ").outcome == "answered"
    response = client.post("/api/chat", json={"conversation_id": "c1", "message": "x" * 21})
    assert response.status_code == 422
    assert refusal(response.text).message == "The message is 21 characters long; the limit is 20."
    assert model.calls == 1


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded"])
def test_only_a_json_body_is_read(
    settings: Settings, no_page: Path, content_type: str
) -> None:
    # A page on another site can post these types without the browser asking first.
    client, model = serve(settings, [sql_reply(COUNT)], dist=no_page)
    response = client.post(
        "/api/chat",
        content='{"conversation_id": "c1", "message": "How many?"}',
        headers={"Content-Type": content_type},
    )
    assert response.status_code == 422 and refusal(response.text).code == "invalid_request"
    assert model.calls == 0


def test_text_that_is_not_json_is_a_422(settings: Settings, no_page: Path) -> None:
    client, _ = serve(settings, [], dist=no_page)
    response = client.post(
        "/api/chat", content="{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422 and refusal(response.text).code == "invalid_request"


def test_unknown_addresses_and_methods_get_a_typed_body(settings: Settings, no_page: Path) -> None:
    client, _ = serve(settings, [], dist=no_page)
    missing = client.get("/api/nothing")
    assert missing.status_code == 404 and refusal(missing.text).code == "not_found"
    wrong = client.get("/api/chat")
    assert wrong.status_code == 404 and refusal(wrong.text).code == "not_found"
    method = client.delete("/api/health")
    assert method.status_code == 405 and refusal(method.text).code == "method_not_allowed"


def test_a_request_under_another_host_name_is_refused(settings: Settings, no_page: Path) -> None:
    client, model = serve(settings, [sql_reply(COUNT)], dist=no_page)
    response = client.post(
        "/api/chat",
        json={"conversation_id": "c1", "message": "How many?"},
        headers={"Host": "attacker.example"},
    )
    assert response.status_code == 400 and model.calls == 0
    assert client.get("/api/health", headers={"Host": "localhost:8000"}).status_code == 200


def test_no_cross_origin_access_is_granted(settings: Settings, no_page: Path) -> None:
    client, _ = serve(settings, [], dist=no_page)
    origin = {"Origin": "https://other.example", "Access-Control-Request-Method": "POST"}
    preflight = client.options("/api/chat", headers=origin)
    assert preflight.status_code == 405
    got = client.get("/api/health", headers={"Origin": "https://other.example"})
    assert not [name for name in (*preflight.headers, *got.headers) if "access-control" in name]


def test_a_fault_of_the_server_itself_is_a_500_that_says_nothing(
    settings: Settings, no_page: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken(self: Assistant, question: str, conversation_id: str) -> TurnResult:
        raise RuntimeError(f"fault with {question} in it")

    monkeypatch.setattr(Assistant, "run_turn", broken)
    model = ScriptedModel([])
    app = create_app(settings, Assistant(settings, model), dist=no_page)
    client = TestClient(app, base_url=HOST, raise_server_exceptions=False)
    with logged(settings) as log:
        response = client.post("/api/chat", json={"conversation_id": "c1", "message": QUESTION})
        assert response.status_code == 500 and refusal(response.text).code == "internal"
        assert "question-marker-71" not in response.text
        text = log.read_text()
    assert "web request failed: RuntimeError" in text and "question-marker-71" not in text
    # The conversation is free again: the lock was released.
    monkeypatch.undo()
    assert client.post(
        "/api/chat", json={"conversation_id": "c1", "message": QUESTION}
    ).status_code == 200


# --- Health -------------------------------------------------------------------------------


def health(client: TestClient) -> Health:
    response = client.get("/api/health")
    assert response.status_code == 200
    return Health.model_validate_json(response.content)


def test_health_with_a_key_says_what_the_interface_needs(
    settings: Settings, no_page: Path
) -> None:
    client, model = serve(settings, [], dist=no_page)
    response = client.get("/api/health")
    found = Health.model_validate_json(response.content)
    assert found == Health(
        status="ok",
        mode="live",
        database=api.DatabaseHealth(present=True, readable=True, schema_context=True),
        key_configured=True,
        model_id="claude-haiku-5-5",
        prompt_version=found.prompt_version,
        tracing=api.TracingHealth(enabled=False, destination=None),
        limits=api.Limits(max_message_characters=2000, row_cap=1000),
        evidence_note=EVIDENCE_NOTE,
        demo_questions=(),
    )
    assert found.prompt_version.startswith("turn-")
    assert KEY not in response.text and "key" not in set(response.json()) - {"key_configured"}
    assert model.calls == 0
    assert health(client) == found, "asked again, the schema context is not rebuilt"


def test_health_without_a_key_is_degraded_unless_the_demo_runs(
    fixture_db: Path, tmp_path: Path, no_page: Path
) -> None:
    settings = load_settings({"FSM_DB_PATH": str(fixture_db)}, env_file=None)
    assistant = Assistant(settings, ScriptedModel([]))
    live = health(TestClient(create_app(settings, assistant, dist=no_page), base_url=HOST))
    assert (live.status, live.key_configured, live.database.readable) == ("degraded", False, True)
    app = create_app(
        settings,
        assistant,
        mode="offline_demo",
        model_id=demo.MODEL_ID,
        demo_questions=demo.QUESTIONS,
        dist=no_page,
    )
    offline = health(TestClient(app, base_url=HOST))
    assert (offline.status, offline.mode, offline.key_configured) == ("ok", "offline_demo", False)
    assert offline.model_id == "offline-demo" and offline.demo_questions == demo.QUESTIONS


def test_health_with_a_missing_or_broken_database(tmp_path: Path, no_page: Path) -> None:
    client, _ = serve(settings_with(tmp_path / "absent.db", tmp_path), [], dist=no_page)
    missing = health(client)
    assert missing.status == "degraded"
    assert missing.database == api.DatabaseHealth(
        present=False, readable=False, schema_context=False
    )
    broken = tmp_path / "broken.db"
    broken.write_text("this is not a database, " * 100)
    client, _ = serve(settings_with(broken, tmp_path), [], dist=no_page)
    found = health(client)
    assert found.status == "degraded"
    assert found.database == api.DatabaseHealth(present=True, readable=False, schema_context=False)


def test_health_names_where_traces_go_and_never_a_key(
    fixture_db: Path, tmp_path: Path, no_page: Path
) -> None:
    settings = settings_with(
        fixture_db, tmp_path, LANGSMITH_TRACING="true", LANGSMITH_API_KEY=LANGSMITH_KEY
    )
    tracer = Tracer(RecordingSink(), Destination(project="fraud-desk"))
    client, _ = serve(settings, [], dist=no_page, tracer=tracer)
    response = client.get("/api/health")
    found = Health.model_validate_json(response.content)
    assert found.tracing == api.TracingHealth(enabled=True, destination="project 'fraud-desk'")
    assert KEY not in response.text and LANGSMITH_KEY not in response.text


# --- What stays out of the log and the trace ----------------------------------------------


class logged:
    """The application log of `settings`, written at DEBUG while the block runs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._level = logging.getLogger(applog.LOGGER_NAME).level

    def __enter__(self) -> Path:
        applog.configure(directory=self._settings.log_dir, level="DEBUG")
        return self._settings.log_dir / LOG_FILE

    def __exit__(self, *exc: object) -> None:
        applog.configure(directory=None)
        logging.getLogger(applog.LOGGER_NAME).setLevel(self._level)


def test_the_log_holds_no_message_sql_or_row(settings: Settings, no_page: Path) -> None:
    client, _ = serve(settings, [sql_reply(TAGS)], dist=no_page)
    with logged(settings) as log:
        result = ask(client)
        refused = client.post(
            "/api/chat", json={"conversation_id": "c1", "message": QUESTION, "extra": QUESTION}
        )
        too_long = client.post(
            "/api/chat", json={"conversation_id": "c1", "message": QUESTION * 100}
        )
        client.get("/api/health")
        text = log.read_text()
    assert result.table is not None and result.table.rows[0] == (ROW_MARKER,)
    assert refused.status_code == 422 and too_long.status_code == 422
    assert "turn finished: conversation=c1 turn=1 outcome=answered" in text
    assert "chat request refused: status=422 code=invalid_request" in text
    for value in (ROW_MARKER, "row-", "question-marker-71", "SELECT", "users", KEY):
        assert value not in text, f"{value!r} reached the log"
    assert sorted(path.name for path in settings.log_dir.iterdir()) == [LOG_FILE]


def test_a_web_turn_is_labelled_web_and_its_trace_carries_no_data(
    settings: Settings, no_page: Path
) -> None:
    sink = RecordingSink()
    tracer = Tracer(sink, Destination(project="p"))
    client, _ = serve(settings, list(LEAKY), dist=no_page, tracer=tracer)
    with logged(settings) as log:
        results = [ask(client, f"Question {number}") for number in range(1, 5)]
        text = log.read_text()

    shown = "".join(result.model_dump_json() for result in results)
    for value in ("row-1-marker", "Elm Street", "10:2500"):
        assert value in shown, f"{value!r} reached the browser, so the search means something"
    assert [result.outcome for result in results] == [
        "answered", "failed", "failed", "rule_proposed",
    ]  # fmt: skip

    assert [batch[0].name for batch in sink.batches] == [f"web turn {n}" for n in (1, 2, 3, 4)]
    for run in sink.runs:
        assert run.metadata["command"] == "web" and "command:web" in run.tags
    sent = sink.text()
    assert "Question 1" in sent and TAGS in sent, "a trace does carry the question and the SQL"
    for value in NEVER_SENT:
        assert value not in sent, f"{value!r} was sent in a trace"
        assert value not in text, f"{value!r} reached the log"
    assert text.count("trace queued: conversation=c1") == 4


def test_stopping_the_server_flushes_the_tracer(settings: Settings, no_page: Path) -> None:
    sink = RecordingSink()
    client, _ = serve(
        settings, [sql_reply(COUNT)], dist=no_page, tracer=Tracer(sink, Destination(project="p"))
    )
    with client:
        ask(client)
        assert not sink.closed
    assert sink.closed and len(sink.batches) == 1


# --- Two requests at once -----------------------------------------------------------------


class GatedModel:
    """Replies with a count; its first call waits until the test lets it go."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.requests: list[ModelRequest] = []
        self._lock = threading.Lock()

    def complete(self, request: ModelRequest) -> ModelReply:
        with self._lock:
            self.requests.append(request)
            first = len(self.requests) == 1
        if first:
            self.entered.set()
            assert self.release.wait(10), "the test never released the first call"
        return sql_reply(COUNT)


class Posted:
    """One chat request made from its own thread."""

    def __init__(self, app: FastAPI, conversation: str, message: str) -> None:
        self.status: int | None = None
        self.text = ""
        self._body = {"conversation_id": conversation, "message": message}
        self._app = app
        self._thread = threading.Thread(target=self._run, name=f"post-{message}")
        self._thread.start()

    def _run(self) -> None:
        response = TestClient(self._app, base_url=HOST).post("/api/chat", json=self._body)
        self.status, self.text = response.status_code, response.text

    def done(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


def gated_app(settings: Settings, no_page: Path, busy_wait_seconds: float) -> tuple[FastAPI, GatedModel]:
    model = GatedModel()
    assistant = Assistant(settings, model, label=Label(command="web"))
    app = create_app(settings, assistant, dist=no_page, busy_wait_seconds=busy_wait_seconds)
    return app, model


def test_two_requests_for_one_conversation_run_one_after_the_other(
    settings: Settings, no_page: Path
) -> None:
    app, model = gated_app(settings, no_page, busy_wait_seconds=10)
    first = Posted(app, "c1", "first-question")
    assert model.entered.wait(10)
    second = Posted(app, "c1", "second-question")
    other = Posted(app, "c2", "other-question")

    assert other.done(10) and other.status == 200, "another conversation is not held up"
    assert not second.done(0.3) and not first.done(0), "the second waits for the first"
    assert [request.turn for request in model.requests] == [1, 1]

    model.release.set()
    assert first.done(10) and second.done(10)
    assert (first.status, second.status) == (200, 200)
    turns = {TurnResult.model_validate_json(post.text).meta.turn for post in (first, second)}
    assert turns == {1, 2}
    # The second ran after the first had been remembered: it was told of it.
    assert "first-question" in model.requests[2].user
    assert "second-question" in model.requests[2].user


def test_a_conversation_that_stays_busy_is_a_409(settings: Settings, no_page: Path) -> None:
    app, model = gated_app(settings, no_page, busy_wait_seconds=0.05)
    first = Posted(app, "c1", "first-question")
    assert model.entered.wait(10)
    second = Posted(app, "c1", "second-question")
    assert second.done(10) and second.status == 409
    error = refusal(second.text)
    assert error.code == "conversation_busy" and "second-question" not in second.text
    assert len(model.requests) == 1, "the refused request ran nothing"
    model.release.set()
    assert first.done(10) and first.status == 200
    again = Posted(app, "c1", "second-question")
    assert again.done(10) and again.status == 200


# --- The built interface ------------------------------------------------------------------


@pytest.fixture
def built(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>built-page</title>")
    (dist / "assets" / "app.js").write_text("console.log('built-script');")
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "secret.txt").write_text("outside-the-build")
    return dist


def test_the_built_interface_is_served_with_a_fallback_to_its_page(
    settings: Settings, built: Path
) -> None:
    client, _ = serve(settings, [sql_reply(COUNT)], dist=built)
    page = client.get("/")
    assert page.status_code == 200 and "built-page" in page.text
    assert page.headers["content-type"].startswith("text/html")
    script = client.get("/assets/app.js")
    assert "built-script" in script.text and "javascript" in script.headers["content-type"]
    assert client.get("/favicon.svg").headers["content-type"].startswith("image/svg")
    for address in ("/conversations/abc", "/assets/missing.js", "/index.html"):
        assert "built-page" in client.get(address).text, address
    for address in ("/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt", "/..%5csecret.txt"):
        assert "outside-the-build" not in client.get(address).text, address
    # The API is not shadowed by the page.
    assert client.get("/api/nothing").status_code == 404
    assert ask(client).outcome == "answered"


def test_without_a_build_the_page_says_how_to_make_one(settings: Settings, no_page: Path) -> None:
    client, _ = serve(settings, [sql_reply(COUNT)], dist=no_page)
    for address in ("/", "/anything"):
        response = client.get(address)
        assert response.status_code == 404 and response.text == api.NOT_BUILT
        assert response.headers["content-type"].startswith("text/plain")
    assert "npm run build" in api.NOT_BUILT and "npm run dev" in api.NOT_BUILT
    assert health(client).status == "ok" and ask(client).outcome == "answered"
    assert "not built" in api.describe_page(no_page)


def test_the_page_line_of_the_command(built: Path) -> None:
    assert api.describe_page(built) == "Web interface: the build in frontend/dist is served at /."


# --- The serve command --------------------------------------------------------------------


class Served:
    """Stands in for `uvicorn.run`: keeps the application instead of listening."""

    def __init__(self) -> None:
        self.app: FastAPI | None = None
        self.host = ""
        self.port = 0

    def __call__(self, app: FastAPI, *, host: str, port: int, log_level: str) -> None:
        self.app, self.host, self.port = app, host, port

    def client(self) -> TestClient:
        assert self.app is not None, "the command never started the server"
        return TestClient(self.app, base_url=HOST)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    running = Served()
    monkeypatch.setattr(uvicorn, "run", running)
    return running


def forbid_live_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_model(spec: ModelSpec, settings: Settings) -> IntentModel:
        raise AssertionError("no live model may be built here")

    def no_tracer(settings: Settings, warn: object = None) -> Tracer | None:
        raise AssertionError("no tracer may be started here")

    def no_settings() -> Settings:
        raise AssertionError("the offline demo reads neither .env nor the environment")

    monkeypatch.setattr(cli, "_live_model", no_model)
    monkeypatch.setattr(tracing, "start", no_tracer)
    monkeypatch.setattr(cli, "load_settings", no_settings)


def test_the_offline_demo_uses_no_key_no_model_and_no_tracer(
    served: Served,
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forbid_live_parts(monkeypatch)
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", fixture_db)
    # What the owner's shell or `.env` may hold is not read.
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", LANGSMITH_KEY)
    monkeypatch.setenv("FSM_ROW_CAP", "3")

    assert cli.main(["serve", "--offline-demo"]) == 0
    output = capsys.readouterr()
    assert "OFFLINE DEMO: canned replies" in output.out and "Nothing is sent" in output.out
    assert "Hosted tracing: off (the offline demo is never traced)." in output.out
    assert "http://127.0.0.1:8000/" in output.out and output.err == ""
    assert KEY not in output.out and (served.host, served.port) == ("127.0.0.1", 8000)

    client = served.client()
    found = health(client)
    assert (found.status, found.mode, found.key_configured) == ("ok", "offline_demo", False)
    assert found.tracing.enabled is False and found.limits.row_cap == 1000
    assert found.model_id == "offline-demo" and found.demo_questions == demo.QUESTIONS

    counted = ask(client, "how many transactions are there")
    assert counted.outcome == "answered" and counted.table is not None
    assert counted.table.rows == ((6,),) and counted.meta.model_id == "offline-demo"
    assert counted.explanation is not None and "Offline demo" in counted.explanation
    unknown = ask(client, "What is the average amount?")
    assert unknown.outcome == "clarification"
    assert set(unknown.suggestions) <= set(demo.QUESTIONS) and len(unknown.suggestions) == 3


def test_every_demo_question_ends_as_intended(
    served: Served, fixture_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_live_parts(monkeypatch)
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", fixture_db)
    assert cli.main(["serve", "--offline-demo"]) == 0
    client = served.client()
    results = [
        ask(client, question, f"demo-{number}") for number, question in enumerate(demo.QUESTIONS)
    ]
    assert [(result.outcome, result.tool) for result in results] == [
        ("answered", "run_sql"),
        ("answered", "run_sql"),
        ("answered", "run_sql"),
        ("answered", "profile_fraud"),
        ("rule_proposed", "propose_rule"),
        ("clarification", "ask_clarification"),
        ("cannot_answer", "decline"),
        ("rejected", "run_sql"),
    ]
    assert [str(code) for code in results[-1].refused] == ["not_read_only", "not_read_only"]
    assert results[-1].table is None and results[-1].write_attempts == 2
    assert [result.write_attempts for result in results[:-1]] == [0] * 7
    formats = [
        tuple(column.format for column in result.table.columns)
        for result in results[1:3]
        if result.table is not None
    ]
    assert formats == [
        ("plain", "plain", "plain", "fraction"),
        ("identifier", "plain", "usd_cents"),
    ]
    # The chip a scan offers is answered with the canned rule.
    chip = ask(client, "Propose a rule for transactions with merchant city and state = X.")
    assert chip.outcome == "rule_proposed" and chip.where_clause == results[4].where_clause
    # Each suggestion the demo itself offers is a question it can answer.
    for result in (results[5], ask(client, "Anything else")):
        assert result.suggestions and set(result.suggestions) <= set(demo.QUESTIONS)


def test_real_data_every_demo_question_ends_as_intended(
    served: Served, real_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_live_parts(monkeypatch)
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", real_db)
    assert cli.main(["serve", "--offline-demo"]) == 0
    client = served.client()
    results = {question: ask(client, question, "real") for question in demo.QUESTIONS}
    assert [result.status for result in results.values()] == [
        "ok", "ok", "ok", "ok", "ok", "needs_clarification", "cannot_answer", "rejected",
    ]  # fmt: skip
    scan = results["Where is fraud concentrated?"]
    assert scan.suggestions[0] == (
        "Propose a rule for transactions with merchant city and state = Rome, Italy."
    )
    chip = ask(client, scan.suggestions[0], "real")
    assert chip.rule is not None and chip.rule.true_positives == chip.rule.total_fraud == 1360


def test_serve_runs_live_turns_labelled_web(
    served: Served,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = RecordingSink()

    def start(settings: Settings, warn: object = None) -> Tracer:
        return Tracer(sink, Destination(project="p"))

    model = ScriptedModel([sql_reply(COUNT)])
    monkeypatch.setattr(tracing, "start", start)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "_live_model", lambda spec, settings: model)

    assert cli.main(["serve", "--port", "8123"]) == 0
    output = capsys.readouterr().out
    assert "Model: claude-haiku-5-5, live. Each question asked in the browser is billed" in output
    assert "Hosted tracing: on, to LangSmith project 'p'" in output
    assert "http://127.0.0.1:8123/" in output and "Warning" not in output
    assert KEY not in output and served.port == 8123

    with served.client() as client:
        found = health(client)
        assert (found.mode, found.key_configured, found.tracing.enabled) == ("live", True, True)
        assert found.demo_questions == () and model.calls == 0
        assert ask(client).outcome == "answered"
    assert sink.batches[0][0].name == "web turn 1" and sink.closed
    started = (settings.log_dir / LOG_FILE).read_text().splitlines()[0]
    assert started.endswith(
        "INFO fsm_assistant.runtime: web started: model=claude-haiku-5-5 "
        f"prompt={found.prompt_version} tool_mode=tools replies=live tracing=project 'p'"
    )
    applog.configure(directory=None)


def test_serve_cannot_start_without_a_key_or_with_a_bad_setting(
    served: Served,
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_key() -> Settings:
        return load_settings({"FSM_DB_PATH": str(fixture_db)}, env_file=None)

    monkeypatch.setattr(cli, "load_settings", no_key)
    assert cli.main(["serve"]) == 2
    assert "Cannot start: " in capsys.readouterr().out and served.app is None

    def bad() -> Settings:
        return load_settings({"FSM_ROW_CAP": "many"}, env_file=None)

    monkeypatch.setattr(cli, "load_settings", bad)
    assert cli.main(["serve"]) == 2
    assert "Cannot start: FSM_ROW_CAP" in capsys.readouterr().out and served.app is None
    applog.configure(directory=None)


def test_serve_warns_when_it_listens_beyond_this_machine(
    served: Served,
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forbid_live_parts(monkeypatch)
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", fixture_db)
    assert cli.main(["serve", "--offline-demo", "--host", "192.0.2.7"]) == 0
    output = capsys.readouterr().out
    assert "Warning: listening on 192.0.2.7. There is no authentication" in output
    client = served.client()
    assert client.get("/api/health", headers={"Host": "192.0.2.7:8000"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "other.example"}).status_code == 400
    applog.configure(directory=None)
