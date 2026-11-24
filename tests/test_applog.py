"""Application logging: what a line says, what it never says, and that it cannot fail a turn.

Turns run the real graph with a scripted model against the fixture database
and log into a temp directory.
"""

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import (
    RecordingSink,
    marked_evidence,
    never_outside_the_answer,
    rule_reply,
    sql_reply,
)

from fsm_assistant import applog, cli, nodes
from fsm_assistant.applog import LOG_FILE
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.graph import Assistant
from fsm_assistant.llm import ModelError, ModelReply, ModelUnavailable, ScriptedModel
from fsm_assistant.tracing import Destination, Tracer

ANTHROPIC_KEY = "sk-ant-not-a-real-key-0123456789"
LANGSMITH_KEY = "lsv2_pt_not-a-real-key-9876543210"
COUNT = "SELECT count(*) FROM transactions"
TAGS = "SELECT 'row-' || id || '-marker' AS tag FROM users ORDER BY id"
FORBIDDEN = "SELECT name FROM sqlite_master"
RULE = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"
# Accepted by the guard, then failing as they run with a message that quotes a row's value.
ADDRESS_IN_ERROR = "SELECT json_extract('{}', (SELECT address FROM users ORDER BY id LIMIT 1))"
AMOUNT_IN_RULE_ERROR = "json_extract('{}', merchant_id || ':' || amount_usd_cents) IS NULL"
PLANTED = "planted-in-an-exception-4711"
# In rows, in a run-time SQLite message, in an exception's text, or in the settings only.
NEVER_LOGGED = ("row-1-marker", "Elm Street", "10:2500", PLANTED, ANTHROPIC_KEY, LANGSMITH_KEY)
LINE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (INFO|WARNING|ERROR) fsm_assistant\.\w+: \S")

type Script = list[ModelReply | ModelError]


@pytest.fixture
def settings(fixture_db: Path, tmp_path: Path) -> Settings:
    return load_settings(
        {
            "FSM_DB_PATH": str(fixture_db),
            "FSM_LOG_DIR": str(tmp_path / "logs"),
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "LANGSMITH_API_KEY": LANGSMITH_KEY,
        },
        env_file=None,
    )


@pytest.fixture(autouse=True)
def logging_as_it_was() -> Iterator[None]:
    logger = logging.getLogger(applog.LOGGER_NAME)
    level = logger.level
    yield
    applog.configure(directory=None)
    logger.setLevel(level)


def configured(settings: Settings) -> Path:
    applog.configure(directory=settings.log_dir, level=settings.log_level)
    return settings.log_dir / LOG_FILE


def test_a_finished_turn_is_one_plain_line(settings: Settings) -> None:
    path = configured(settings)
    result = Assistant(settings, ScriptedModel([sql_reply(COUNT)])).run_turn("How many?", "conv-1")
    (line,) = path.read_text().splitlines()
    assert LINE.match(line)
    assert line.endswith(
        "INFO fsm_assistant.graph: turn finished: conversation=conv-1 turn=1 outcome=answered "
        f"error=- requests=1 elapsed_ms={result.meta.elapsed_ms}"
    )


def test_what_went_wrong_is_logged_by_kind_and_class(settings: Settings, tmp_path: Path) -> None:
    path = configured(settings)
    script: Script = [
        sql_reply(FORBIDDEN),
        sql_reply(COUNT),
        ModelUnavailable("rate_limit", f"slow down, {ANTHROPIC_KEY}"),
        sql_reply(FORBIDDEN),
        sql_reply(FORBIDDEN),
    ]
    assistant = Assistant(settings, ScriptedModel(script))
    outcomes = [assistant.run_turn(f"Question {n}", "c1").outcome for n in (1, 2, 3)]
    nowhere = settings.model_copy(update={"db_path": tmp_path / "missing.db"})
    outcomes.append(Assistant(nowhere, ScriptedModel([])).run_turn("Question 4", "c2").outcome)

    assert outcomes == ["answered", "failed", "rejected", "failed"]
    text = path.read_text()
    assert all(LINE.match(line) for line in text.splitlines())
    assert (
        "WARNING fsm_assistant.graph: turn needed a repair: conversation=c1 turn=1 "
        "refused=table_not_allowed" in text
    )
    assert (
        "ERROR fsm_assistant.graph: turn finished: conversation=c1 turn=2 outcome=failed "
        "error=model_unavailable model_error='ModelUnavailable: rate_limit' requests=1" in text
    )
    assert "INFO fsm_assistant.graph: turn finished: conversation=c1 turn=3 outcome=rejected" in text
    assert "ERROR fsm_assistant.nodes: the database could not be read: DatabaseNotFound" in text
    assert "turn finished: conversation=c2 turn=1 outcome=failed error=internal" in text
    assert ANTHROPIC_KEY not in text and "slow down" not in text


def test_no_question_no_sql_no_data_no_exception_text_and_no_key_reaches_the_log(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = configured(settings.model_copy(update={"log_level": "DEBUG"}))
    script: Script = [
        sql_reply(TAGS),
        sql_reply(ADDRESS_IN_ERROR),
        sql_reply(ADDRESS_IN_ERROR),
        rule_reply(AMOUNT_IN_RULE_ERROR),
        rule_reply(AMOUNT_IN_RULE_ERROR),
        rule_reply(RULE),
        sql_reply(COUNT),
    ]
    sink = RecordingSink(fault=RuntimeError(f"{PLANTED} {LANGSMITH_KEY}"))
    assistant = Assistant(
        settings, ScriptedModel(script), tracer=Tracer(sink, Destination(project="p"))
    )
    results = [assistant.run_turn(f"Question {number}", "c1") for number in range(1, 5)]

    def fault(self: object, state: object) -> None:
        raise RuntimeError(f"{PLANTED}: 1 Elm Street, key {ANTHROPIC_KEY}")

    monkeypatch.setattr(nodes.TurnNodes, "execute_sql", fault)
    broken = Assistant(settings, ScriptedModel(script[-1:]))
    results.append(broken.run_turn("Question 5", "c2"))

    assert [result.outcome for result in results] == [
        "answered", "failed", "failed", "rule_proposed", "failed",
    ]  # fmt: skip
    shown = "".join(result.model_dump_json() for result in results)
    for value in NEVER_LOGGED[:4]:
        assert value in shown, "the user is shown it, so the search below means something"
    text = path.read_text()
    assert text.count("turn finished") == 5 and text.count("outcome=failed error=sql_execution") == 2
    assert "ERROR fsm_assistant.graph: internal failure: RuntimeError raised at " in text
    assert re.search(r"raised at test_applog\.py:\d+ in fault\n", text), "the place, not the message"
    assert text.count("a trace could not be sent to LangSmith: RuntimeError") == 4
    for value in (*NEVER_LOGGED, "Question", "SELECT", "amount_usd_cents", "json_extract"):
        assert value not in text, f"{value!r} was written to the log"


def test_no_figure_of_a_rules_evidence_by_month_reaches_the_log(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = configured(settings.model_copy(update={"log_level": "DEBUG"}))
    monkeypatch.setattr(nodes, "rule_evidence", marked_evidence)
    sink = RecordingSink()
    assistant = Assistant(
        settings, ScriptedModel([rule_reply(RULE)]), tracer=Tracer(sink, Destination(project="p"))
    )
    result = assistant.run_turn("A rule for big debit payments", "c1")

    card = result.rule
    assert card is not None and len(sink.batches) == 1
    assert {"weak_month", "period_drop"} <= {caveat.code for caveat in result.caveats}
    shown = result.model_dump_json()
    text = path.read_text()
    assert "turn finished: conversation=c1 turn=1 outcome=rule_proposed error=-" in text
    assert "trace queued: conversation=c1 turn=1" in text
    # The trace id on that line is made by code from the clock; the rest is searched.
    searched = re.sub(r"trace_id=\S+", "", text)
    figures = never_outside_the_answer(card)
    for figure in ("5831329", "2019-09", "0.03800", "weakest_month"):
        assert figure in figures and figure in shown, "shown to the user, so the search means something"
    for figure in figures:
        assert figure not in searched, f"{figure!r} was written to the log"
    for value in ("weak_month", "period_drop", "amount_usd_cents", "A rule for"):
        assert value not in text, f"{value!r} was written to the log"


def test_logging_that_cannot_write_does_not_fail_a_turn_or_print(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    applog.configure(directory=blocker / "logs")
    result = Assistant(settings, ScriptedModel([sql_reply(FORBIDDEN)] * 2)).run_turn("q", "c1")
    assert result.outcome == "rejected"
    assert capsys.readouterr().err == "" and blocker.read_text() == "not a directory"


def test_the_file_can_be_off_and_the_terminal_on(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    applog.configure(directory=None)
    assistant = Assistant(settings, ScriptedModel([sql_reply(FORBIDDEN)] * 4))
    assert assistant.run_turn("q", "c1").outcome == "rejected"
    assert capsys.readouterr().err == "" and not settings.log_dir.exists()

    applog.configure(directory=None, level="WARNING", to_terminal=True)
    assert assistant.run_turn("q", "c1").outcome == "rejected"
    (line,) = capsys.readouterr().err.splitlines()
    assert LINE.match(line) and "WARNING fsm_assistant.graph: turn needed a repair" in line
    assert not settings.log_dir.exists()


def test_the_log_directory_is_bounded_by_rotation(tmp_path: Path) -> None:
    applog.configure(directory=tmp_path, max_bytes=2000, backups=2)
    logger = logging.getLogger("fsm_assistant.test")
    for number in range(300):
        logger.info("line %d %s", number, "x" * 60)
    applog.configure(directory=None)
    files = sorted(tmp_path.iterdir())
    assert [path.name for path in files] == [LOG_FILE, f"{LOG_FILE}.1", f"{LOG_FILE}.2"]
    assert all(path.stat().st_size <= 2000 for path in files)
    assert "line 299 " in (tmp_path / LOG_FILE).read_text()


def test_only_configure_adds_handlers_and_a_second_call_replaces_them(settings: Settings) -> None:
    logger = logging.getLogger(applog.LOGGER_NAME)
    applog.configure(directory=None)
    before = list(logger.handlers)
    Assistant(settings, ScriptedModel([sql_reply(COUNT)])).run_turn("q", "c1")
    assert logger.handlers == before, "library code only gets loggers"
    applog.configure(directory=settings.log_dir, to_terminal=True)
    applog.configure(directory=settings.log_dir, to_terminal=True)
    assert len(logger.handlers) == len(before) + 1


# --- The commands -------------------------------------------------------------------------


def test_ask_logs_its_start_and_its_turn_and_prints_no_log_line(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model = ScriptedModel([sql_reply(COUNT), sql_reply(COUNT)])
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "_live_model", lambda spec, settings: model)

    assert cli.main(["ask", "How many?"]) == 0
    output = capsys.readouterr()
    assert "Hosted tracing: off.\n" in output.out and output.err == ""
    started, finished = (settings.log_dir / LOG_FILE).read_text().splitlines()
    assert re.search(
        r"INFO fsm_assistant\.runtime: ask started: model=claude-opus-5-5 prompt=turn-\d+ "
        r"tool_mode=tools replies=live tracing=off$",
        started,
    )
    assert "turn finished: conversation=" in finished and "outcome=answered" in finished

    assert cli.main(["ask", "--verbose", "How many?"]) == 0
    output = capsys.readouterr()
    assert "fsm_assistant.runtime: ask started" in output.err and "turn finished" in output.err
    assert ANTHROPIC_KEY not in output.out + output.err
    assert ANTHROPIC_KEY not in (settings.log_dir / LOG_FILE).read_text()


def test_the_level_and_the_file_switch_come_from_the_settings(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model = ScriptedModel([sql_reply(FORBIDDEN)] * 4)
    monkeypatch.setattr(cli, "_live_model", lambda spec, settings: model)
    quiet = settings.model_copy(update={"log_level": "WARNING"})
    monkeypatch.setattr(cli, "load_settings", lambda: quiet)
    assert cli.main(["ask", "q"]) == 4
    (line,) = (settings.log_dir / LOG_FILE).read_text().splitlines()
    assert "WARNING fsm_assistant.graph: turn needed a repair" in line

    off = settings.model_copy(update={"log_file": False})
    monkeypatch.setattr(cli, "load_settings", lambda: off)
    assert cli.main(["ask", "q"]) == 4
    assert len((settings.log_dir / LOG_FILE).read_text().splitlines()) == 1
    assert capsys.readouterr().err == ""


def test_what_stops_a_command_from_starting_is_logged(
    settings: Settings,
    log_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def bad() -> Settings:
        return load_settings(
            {"FSM_LOG_LEVEL": "LOUD", "LANGSMITH_API_KEY": LANGSMITH_KEY}, env_file=None
        )

    monkeypatch.setattr(cli, "load_settings", bad)
    assert cli.main(["ask", "q"]) == 2
    assert "Cannot start: FSM_LOG_LEVEL must be one of" in capsys.readouterr().out
    last = (log_directory / LOG_FILE).read_text().splitlines()[-1]
    assert "ERROR fsm_assistant.runtime: ask cannot start: invalid setting: FSM_LOG_LEVEL" in last

    half = settings.model_copy(
        update={"anthropic_api_key": None, "langsmith_tracing": True, "langsmith_agent_id": "fsm"}
    )
    monkeypatch.setattr(cli, "load_settings", lambda: half)
    assert cli.main(["chat"]) == 2
    text = (settings.log_dir / LOG_FILE).read_text()
    assert "ERROR fsm_assistant.cli: chat cannot start: no model client (missing_key)" in text

    monkeypatch.setattr(cli, "_live_model", lambda spec, settings: ScriptedModel([sql_reply(COUNT)]))
    assert cli.main(["ask", "q"]) == 0
    output = capsys.readouterr()
    warning = "hosted tracing is not set up and stays off: LANGSMITH_AGENT_ID and"
    assert output.err.count(warning) == 1 and "Hosted tracing: off.\n" in output.out
    text = (settings.log_dir / LOG_FILE).read_text()
    assert f"WARNING fsm_assistant.tracing: {warning}" in text
    assert LANGSMITH_KEY not in text + output.out + output.err
