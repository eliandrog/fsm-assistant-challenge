"""The fraud scan inside a turn, and as a terminal command.

A scripted model asks for `profile_fraud`; the scan, the result, what is
remembered and what a follow-up is told are the real code. The leak tests
search every place a segment's name may not reach.
"""

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import RecordingSink, call_reply, rule_reply, sql_reply
from scan_fixtures import (
    AMEX_PREPAID,
    AQUILA,
    BACKGROUND,
    FIRST_TRANSACTION_ID,
    MAIN,
    MASTERCARD_DEBIT,
    ROME_GA,
    ROW_MARKERS,
    SACRAMENTO,
    Group,
    build,
)
from test_graph import everything_saved, kept, saved

from fsm_assistant import applog, cli, db, explore, nodes
from fsm_assistant.applog import LOG_FILE
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.evals.cases import load_suite
from fsm_assistant.evals.runner import run_case
from fsm_assistant.events import Label
from fsm_assistant.graph import Assistant, TraceStep
from fsm_assistant.llm import IntentModel, ModelSpec, ScriptedModel
from fsm_assistant.nl2sql import model_spec
from fsm_assistant.nodes import SCAN_FAILED_RUNNING
from fsm_assistant.presentation import (
    LABEL_COVERAGE,
    SCAN_COLUMNS,
    SINGLE_SEGMENT,
    render_scan,
    render_text,
    scan_caveats,
    scan_table,
)
from fsm_assistant.prompt import SCAN_RESULT_UNSEEN, PreviousTurn
from fsm_assistant.result import ErrorKind, TurnResult
from fsm_assistant.sql_guard import check_query
from fsm_assistant.state import TurnState
from fsm_assistant.tracing import ALLOWED, Destination, Tracer

SCAN = call_reply("profile_fraud")
COUNT = "SELECT count(*) FROM transactions"
ROME_RULE = "merchant_location_id IN (SELECT id FROM merchant_locations WHERE city = 'Rome' AND state = 'Italy')"
# Names of segments the scan finds on the main fixture: values read from the data.
SEGMENT_NAMES = ("Rome", "Italy", "Sacramento", "Grocery", "5411", "Money Transfer", "Chip Transaction")


@pytest.fixture(scope="module")
def main_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("scan-turn") / "main.db", MAIN)


@pytest.fixture
def settings(main_db: Path) -> Settings:
    return load_settings({"FSM_DB_PATH": str(main_db)}, env_file=None)


def scan_turn(settings: Settings) -> TurnResult:
    return Assistant(settings, ScriptedModel([SCAN])).run_turn("Where is fraud concentrated?", "c1")


# --- The turn ---------------------------------------------------------------------------------


def test_a_scan_turn_is_answered_with_a_table_written_by_code(settings: Settings) -> None:
    model = ScriptedModel([SCAN])
    result = Assistant(settings, model).run_turn("Where is fraud concentrated?", "c1")

    assert (result.outcome, result.status, result.error) == ("answered", "ok", None)
    assert (result.tool, result.answer_source, model.calls) == ("profile_fraud", "code", 1)
    assert result.answer == (
        "Labelled fraud is most concentrated in merchant city and state = Rome, Italy: 150 of "
        "189 labelled frauds (79.4%), at 3.2 times the base rate."
    )
    table = result.table
    assert table is not None and not table.truncated
    assert tuple(column.name for column in table.columns) == SCAN_COLUMNS
    assert [column.format for column in table.columns] == [
        "plain", "plain", "plain", "plain", "fraction", "plain", "fraction",
    ]  # fmt: skip
    assert table.rows == (
        ("merchant city and state", "Rome, Italy", 200, 150, 0.75, 0.75 / (189 / 799), 150 / 189),
        ("merchant category (MCC)", "5411 Grocery Stores, Supermarkets", 300, 150, 0.5,
         0.5 / (189 / 799), 150 / 189),
    )  # fmt: skip
    # The model wrote nothing that is shown.
    assert result.explanation is None and result.assumptions == ()
    assert result.rule is None and result.where_clause is None
    assert result.notes == explore.notes(explore.scan(db_path=settings.db_path, row_cap=1, timeout_seconds=1))
    assert result.suggestions == (
        "Propose a rule for transactions with merchant city and state = Rome, Italy.",
        "What is the fraud rate by month for transactions with merchant city and state = Rome, Italy?",
        "How many transactions, labelled or not, have merchant city and state = Rome, Italy?",
    )


def test_the_sql_shown_is_the_profile_statement_and_the_guard_accepts_it(settings: Settings) -> None:
    result = scan_turn(settings)
    assert result.sql == explore.profile_sql()
    assert check_query(result.sql, db_path=settings.db_path).accepted
    rows = db.run_query(result.sql, db_path=settings.db_path, row_cap=100, timeout_seconds=5).rows
    assert ("listed", "merchant_city", "Rome", "Italy", 200, 150, 1, 1) in rows
    assert ("total", None, None, None, 799, 189, None, None) in rows


def test_caveats_come_from_the_coded_list(settings: Settings, tmp_path: Path) -> None:
    # On the main fixture a fifth of the fraud is outside the top segment.
    assert [(caveat.code, caveat.message) for caveat in scan_turn(settings).caveats] == [
        ("label_coverage", LABEL_COVERAGE)
    ]
    alone = build(tmp_path / "alone.db", [Group(labelled=200, fraud=150), BACKGROUND])
    everything = explore.scan(db_path=alone, row_cap=100, timeout_seconds=5)
    assert [(caveat.code, caveat.message) for caveat in scan_caveats(everything)] == [
        ("label_coverage", LABEL_COVERAGE),
        ("single_segment", SINGLE_SEGMENT),
    ]
    nothing = explore.scan(db_path=build(tmp_path / "none.db", [BACKGROUND]), row_cap=100, timeout_seconds=5)
    assert [caveat.code for caveat in scan_caveats(nothing)] == ["label_coverage"]


def test_a_scan_that_lists_nothing_is_still_an_answer(fixture_db: Path) -> None:
    settings = load_settings({"FSM_DB_PATH": str(fixture_db)}, env_file=None)
    result = scan_turn(settings)
    assert (result.outcome, result.error) == ("answered", None)
    assert result.answer.startswith("No segment passes the floor of 100 labelled transactions")
    assert result.table is not None and result.table.row_count == 0
    assert result.suggestions == ()
    assert "(no rows)" in render_text(result)


def test_the_terminal_text_of_a_scan_turn(settings: Settings) -> None:
    text = render_text(scan_turn(settings))
    lines = text.splitlines()
    assert lines[0].startswith("Labelled fraud is most concentrated in merchant city and state = Rome")
    assert lines[1] == "You could ask:"
    assert lines[2] == "  - Propose a rule for transactions with merchant city and state = Rome, Italy."
    assert "SQL that ran:" in lines and "    WITH labelled AS MATERIALIZED (" in lines
    header = next(line for line in lines if line.startswith("dimension"))
    assert header.split() == list(SCAN_COLUMNS)
    row = next(line for line in lines if line.startswith("merchant city and state  Rome, Italy"))
    assert row.split()[-7:] == ["200", "150", "0.75", "(75.000%)", "3.1706", "0.793651", "(79.365%)"]
    assert "Notes:" in lines and "Caveats:" in lines
    assert "  - Outside merchant city and state = Rome, Italy, 39 labelled frauds remain in 599 " in text
    assert "Model's explanation" not in text and "Model's assumptions" not in text
    assert lines[-1].startswith("[answered | tool: profile_fraud | ")


def test_the_trace_shows_the_scan_as_counts(settings: Settings) -> None:
    steps: list[TraceStep] = []
    Assistant(settings, ScriptedModel([SCAN])).run_turn("Where?", "c1", on_step=steps.append)
    assert [step.node for step in steps] == [
        "begin_turn", "call_model", "check_intent", "run_scan", "present", "remember",
    ]  # fmt: skip
    by_node = {step.node: step.summary for step in steps}
    assert by_node["run_scan"].startswith("reads fraud_labels; scan listed 2 segment(s) from 11 statement(s)")
    assert "caveats: label_coverage" in by_node["present"]
    assert by_node["remember"] == "remembered the turn as kind scan"
    for name in SEGMENT_NAMES:
        assert name not in "".join(by_node.values())


def test_run_scan_needs_the_action_the_model_chose(settings: Settings) -> None:
    turn_nodes = nodes.TurnNodes(settings, ScriptedModel([]), model_spec(settings))
    with pytest.raises(RuntimeError, match="without a profile_fraud action"):
        turn_nodes.run_scan(TurnState())


# --- Failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "kind", "message", "detail"),
    [
        (db.QueryTimeout("slow"), "timeout",
         "The fraud-concentration scan was interrupted after 15 seconds.", None),
        (db.QueryError("bad JSON path: '1 Marker Street'"), "internal", SCAN_FAILED_RUNNING,
         "bad JSON path: '1 Marker Street'"),
        (explore.ScanError("the SQL guard refused a scan statement (too_complex)"), "internal",
         "The fraud-concentration scan could not finish: the SQL guard refused a scan statement "
         "(too_complex).", None),
        (db.DatabaseNotFoundError("gone"), "internal", "The database could not be read. gone", None),
    ],
    ids=["timeout", "failed while running", "scan error", "no database"],
)  # fmt: skip
def test_a_scan_that_fails_is_a_failed_turn_with_no_repair_and_no_memory(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    kind: ErrorKind,
    message: str,
    detail: str | None,
) -> None:
    def broken(**arguments: object) -> explore.ScanResult:
        raise error

    model = ScriptedModel([sql_reply(COUNT), SCAN, sql_reply(COUNT)])
    assistant = Assistant(settings, model)
    handed = everything_saved(assistant)
    assistant.run_turn("How many?", "c1")
    monkeypatch.setattr(nodes, "scan", broken)
    result = assistant.run_turn("Where is fraud concentrated?", "c1")
    assistant.run_turn("And then?", "c1")

    assert (result.outcome, result.status, result.tool) == ("failed", "error", "profile_fraud")
    assert result.error is not None
    assert (result.error.kind, result.error.message, result.error.detail) == (kind, message, detail)
    assert result.table is None and result.sql is None and result.notes == ()
    assert model.calls == 3, "one request per turn: a failed scan is not repaired"
    # The follow-up is told the turn before the failed scan, and the row's value went nowhere.
    assert f"<earlier_sql>\n{COUNT}\n</earlier_sql>" in model.requests[2].user
    assert "Marker Street" not in "".join(handed) + "".join(r.user for r in model.requests)


# --- Memory -----------------------------------------------------------------------------------


def test_a_scan_turn_is_remembered_as_a_fact_and_its_dimensions(settings: Settings) -> None:
    assistant = Assistant(settings, ScriptedModel([SCAN]))
    assistant.run_turn("Where is fraud concentrated?", "c1")
    history = saved(assistant, "c1")["history"]
    assert isinstance(history, list | tuple)
    assert list(history) == [
        PreviousTurn(
            question="Where is fraud concentrated?",
            kind="scan",
            scan_dimensions=tuple(dimension.title for dimension in explore.DIMENSIONS),
        )
    ]
    assert "scan_seen" not in saved(assistant, "c1")


def test_a_follow_up_is_told_a_scan_ran_and_not_what_it_found(settings: Settings) -> None:
    ask = call_reply("ask_clarification", question="Which segment?", suggestions=["a", "b"])
    model = ScriptedModel([SCAN, ask])
    assistant = Assistant(settings, model)
    assistant.run_turn("Where is fraud concentrated?", "c1")
    follow_up = assistant.run_turn("Turn that into a rule", "c1")

    assert follow_up.outcome == "clarification"
    assert model.requests[1].system == model.requests[0].system
    assert model.requests[1].user == (
        "Earlier in this conversation (context for a follow-up; it may be unrelated):\n"
        "The user asked:\n"
        "<earlier_question>\nWhere is fraud concentrated?\n</earlier_question>\n"
        "You called profile_fraud, and the built-in fraud-concentration scan ran. It profiled "
        "the labelled fraud by: transaction type; merchant state or country; merchant city and "
        "state; merchant category (MCC); hour of day; error; amount band; card type; card brand.\n"
        f"{SCAN_RESULT_UNSEEN}\n\n"
        "Question:\n<question>\nTurn that into a rule\n</question>"
    )


def test_naming_the_segment_in_the_follow_up_gives_the_rule(settings: Settings) -> None:
    model = ScriptedModel([SCAN, rule_reply(ROME_RULE)])
    assistant = Assistant(settings, model)
    first = assistant.run_turn("Where is fraud concentrated?", "c1")
    # The user sends the scan's own suggestion: the segment's name reaches the model
    # as the user's words, in the question, and nowhere else.
    second = assistant.run_turn(first.suggestions[0], "c1")

    assert second.outcome == "rule_proposed" and second.rule is not None
    assert (second.rule.flagged, second.rule.flagged_labelled) == (250, 200)
    assert (second.rule.true_positives, second.rule.false_positives) == (150, 50)
    before, question = model.requests[1].user.split("Question:\n")
    assert "Rome, Italy" in question and "Rome" not in before


SHOWN_FIGURES = ("of 189", "79.4%", "3.2 times", "labelled frauds")
NEVER_KEPT = (*SEGMENT_NAMES, *ROW_MARKERS, *SHOWN_FIGURES)


def scan_conversation(settings: Settings) -> tuple[TurnResult, ScriptedModel, dict[str, str]]:
    """Five turns, three of them scans: the first result, the model, and every place to search."""
    model = ScriptedModel([SCAN, sql_reply(COUNT), SCAN, rule_reply("amount_usd_cents > 100000"), SCAN])
    assistant = Assistant(settings, model)
    handed = everything_saved(assistant)
    steps: list[TraceStep] = []

    first = assistant.run_turn("Where is fraud concentrated?", "c1", on_step=steps.append)
    assistant.run_turn("How many transactions is that?", "c1", on_step=steps.append)
    assistant.run_turn("Scan again", "c1", on_step=steps.append)
    assistant.run_turn("A rule for payments over $1,000", "c1", on_step=steps.append)
    assistant.run_turn("And scan once more", "c1", on_step=steps.append)

    snapshot = saved(assistant, "c1")
    assert "result" not in snapshot and "response" not in snapshot
    searched = {
        # The user text only: the schema context in the system text lists the values of a
        # column with few distinct ones, which on a small fixture includes the states.
        "a model request": "".join(request.user for request in model.requests),
        "the saver, at any time": "".join(handed),
        "the saver, at the end": kept(assistant),
        "the trace": "".join(step.summary for step in steps),
    }
    return first, model, searched


def reached(searched: dict[str, str]) -> list[tuple[str, str]]:
    """Each value of the data found in a place it may not reach, with the place."""
    return [(value, place) for value in NEVER_KEPT for place, text in searched.items() if value in text]


def test_no_segment_name_or_row_value_is_saved_sent_or_traced(settings: Settings) -> None:
    first, model, searched = scan_conversation(settings)

    shown = first.model_dump_json()
    assert all(name in shown for name in SEGMENT_NAMES), "the user is shown the segments"
    assert all(figure in shown for figure in SHOWN_FIGURES), "the user is shown the figures"
    for place in ("the saver, at any time", "the saver, at the end"):
        assert "scan_dimensions" in searched[place], "the search looks in the right place"
    assert "profile_fraud" in searched["a model request"] and model.calls == 5
    assert reached(searched) == []
    for marker in ROW_MARKERS:
        assert marker not in shown, f"{marker!r} is a value of a row, and the scan shows aggregates"


@pytest.mark.parametrize(
    ("field", "planted", "found"),
    [
        ("approved_sql", "SELECT 1 WHERE 'Rome' = 'Rome'", "Rome"),
        ("approved_clause", "1 Marker Street", "Marker"),
        ("write_attempts", 5411, "5411"),
        ("write_attempts", FIRST_TRANSACTION_ID, "88000"),
    ],
    ids=["a segment in text", "a row's text", "a segment's code as a number", "a row's id as a number"],
)
def test_a_value_of_the_data_in_a_saved_field_is_found(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, field: str, planted: str | int, found: str
) -> None:
    real = nodes.TurnNodes.run_scan

    def leaky(self: nodes.TurnNodes, state: TurnState) -> dict[str, object]:
        return {**real(self, state), field: planted}

    monkeypatch.setattr(nodes.TurnNodes, "run_scan", leaky)
    found_in = {place for value, place in reached(scan_conversation(settings)[2]) if value == found}
    assert {"the saver, at any time", "the saver, at the end"} <= found_in, "the test above can fail"


def test_the_text_searched_in_the_saver_holds_nothing_made_by_chance(settings: Settings) -> None:
    # A checkpoint id, a clock time or a channel version in the text would hold any short
    # figure once in a while, and the search would then report a leak that is not one.
    once, again = scan_conversation(settings)[2], scan_conversation(settings)[2]
    for place in ("the saver, at any time", "the saver, at the end"):
        assert once[place] == again[place]
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-|\d{4}-\d\d-\d\dT|\d\.\d{9}", once[place])


# --- Events, the hosted trace and the log -----------------------------------------------------

# Three groups whose counts are four digits that nothing else in a turn
# produces. The scan lists the first (L'Aquila, Italy) and the third (Rome,
# GA); the residual statement names the first as a SQL literal.
MARKED = (
    Group(labelled=4321, fraud=3217, unlabelled=7, location=AQUILA),
    Group(
        labelled=2468, fraud=357, transaction_type="Swipe Transaction", location=SACRAMENTO,
        merchant=20, card=MASTERCARD_DEBIT, timestamp="2019-01-31 09:00:00",
        error="Insufficient Balance", amount=517,
    ),
    Group(
        labelled=1111, fraud=1023, transaction_type="Online Transaction", location=ROME_GA,
        merchant=30, card=AMEX_PREPAID, timestamp="2019-04-20 23:00:00",
        error="Bad CVV", amount=150017,
    ),
)  # fmt: skip
MARKED_NAMES = (
    "Aquila", "Italy", "Rome", "GA", "Grocery", "5411", "Passenger Railways", "3722",
    "Chip Transaction", "Online Transaction", "Bad CVV", "Amex", "Visa",
)  # fmt: skip
MARKED_FIGURES = (
    "4321", "4,321", "3217", "3,217", "1111", "1,111", "1023", "1,023", "7,900", "4,597",
    "3,579", "1,380", "70.0%", "58.190%", "0.7445", "0.6998", "1.279", "0.9207", "1.582",
    "1.3 times",
)  # fmt: skip


@pytest.fixture(scope="module")
def marked_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("scan-marked") / "marked.db", MARKED)


@pytest.fixture
def marked(marked_db: Path, tmp_path: Path) -> Settings:
    return load_settings(
        {"FSM_DB_PATH": str(marked_db), "FSM_LOG_DIR": str(tmp_path / "logs")}, env_file=None
    )


@pytest.fixture
def log_file(marked: Settings) -> Iterator[Path]:
    logger = logging.getLogger(applog.LOGGER_NAME)
    level = logger.level
    applog.configure(directory=marked.log_dir, level="DEBUG")
    yield marked.log_dir / LOG_FILE
    applog.configure(directory=None)
    logger.setLevel(level)


def without_code_made_digits(text: str) -> str:
    """`text` without what code makes from a clock or a hash: times, a trace id, a digest."""
    text = re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ", "", text, flags=re.MULTILINE)
    text = re.sub(r"\b\w+_ms\\?\"?[=:] ?\d+", "", text)
    return re.sub(r"trace_id=\S+|\b[0-9a-f]{64}\b", "", text)


def test_a_scan_turn_is_traced_and_logged_as_counts_codes_and_code_written_text(
    marked: Settings, log_file: Path
) -> None:
    sink = RecordingSink()
    lines: list[TraceStep] = []
    assistant = Assistant(
        marked,
        ScriptedModel([SCAN, sql_reply(COUNT)]),
        label=Label(command="ask"),
        tracer=Tracer(sink, Destination(project="p")),
    )
    result = assistant.run_turn("Where is fraud concentrated?", "c1", on_step=lines.append)
    assistant.run_turn("How many transactions is that?", "c1", on_step=lines.append)

    scan = explore.scan(db_path=marked.db_path, row_cap=1, timeout_seconds=1)
    assert result.outcome == "answered" and result.table is not None and len(scan.rows) == 2
    assert result.notes and result.suggestions and result.sql == scan.statements[0]
    shown = result.model_dump_json() + render_text(result)
    for value in (*MARKED_NAMES, *MARKED_FIGURES):
        assert value in shown, f"{value!r} is shown to the user, so the search means something"

    runs = sink.batches[0]
    by_name = {run.name: run for run in runs}
    root, ran = runs[0], by_name["run_scan"]
    # A scan ran, how much it ran and listed, how long it took, and codes.
    assert ran.inputs.keys() == {"seq", "attempt", "model_calls"}
    assert ran.outputs == {
        "duration_ms": ran.outputs["duration_ms"],
        "query_ms": round(scan.elapsed_seconds * 1000),
        "reads_labels": True,
        "scan_segments": 2,
        "scan_statements": len(scan.statements),
    }
    assert root.outputs["tool"] == "profile_fraud" and root.outputs["reads_labels"] is True
    assert root.outputs["row_count"] == 2 and root.outputs["columns"] == list(SCAN_COLUMNS)
    assert root.outputs["caveats"] == ["label_coverage"]
    assert by_name["remember"].outputs["remembered"] == "scan"
    # Of the scan's SQL, the profile only: code wrote all of it. The statements after it
    # name a segment as a literal, and none of them is sent.
    assert root.outputs["sql"] == scan.statements[0] == explore.profile_sql()
    assert len(scan.statements) > 1
    assert all("'L''Aquila'" in sql or "'Rome'" in sql for sql in scan.statements[1:])
    assert [key for run in runs for key, value in run.outputs.items() if key == "sql"] == ["sql"]
    assert not {"answer", "notes", "suggestions", "table", "rows", "segments", "statements"} & (
        ALLOWED["turn"][1] | ALLOWED["step"][1] | ALLOWED["model"][1]
    )
    # The turn after it: the request names the dimensions, which are code's words.
    follow_up = next(run for run in sink.batches[1] if run.kind == "model")
    assert "merchant city and state" in json.dumps(follow_up.inputs)

    sent = json.dumps(
        [
            (run.name, run.inputs, run.outputs, run.error, run.tags, run.metadata)
            for run in sink.runs
        ]
    )
    places = {
        "a hosted trace": without_code_made_digits(sent),
        "a --trace line": without_code_made_digits("\n".join(step.summary for step in lines)),
        "the log": without_code_made_digits(log_file.read_text()),
    }
    assert "scan listed 2 segment(s) from" in places["a --trace line"]
    assert "turn finished: conversation=c1 turn=1 outcome=answered" in places["the log"]
    assert places["the log"].count("trace queued: conversation=c1") == 2
    for value in (*MARKED_NAMES, *MARKED_FIGURES, "Labelled fraud is most", "Propose a rule for"):
        for place, text in places.items():
            assert value not in text, f"{value!r} reached {place}"
    assert "Where is fraud" not in places["the log"] and "SELECT" not in places["the log"]


def test_the_scan_command_logs_counts_and_never_a_segment(
    marked: Settings, log_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: marked)
    explore.clear_cache()
    assert cli.main(["scan", "--sql"]) == 0
    output = capsys.readouterr()
    assert "L'Aquila, Italy" in output.out and "L''Aquila" in output.out and output.err == ""

    def broken(**_: object) -> explore.ScanResult:
        raise explore.ScanError("planted Aquila 4321")

    monkeypatch.setattr(explore, "scan", broken)
    assert cli.main(["scan"]) == 1
    assert "planted Aquila 4321" in capsys.readouterr().out, "the terminal shows why"
    finished, failed = log_file.read_text().splitlines()
    assert re.search(
        r"INFO fsm_assistant\.cli: scan finished: listed=2 statements=\d+ elapsed_ms=\d+$", finished
    )
    assert failed.endswith("ERROR fsm_assistant.cli: scan failed: ScanError")
    text = without_code_made_digits(log_file.read_text())
    for value in (*MARKED_NAMES, *MARKED_FIGURES, "SELECT", "planted"):
        assert value not in text, f"{value!r} was written to the log"


# --- The table and the standalone text --------------------------------------------------------


def test_the_table_shows_the_first_segments_and_the_notes_say_how_many_more(main_db: Path) -> None:
    scan = explore.scan(db_path=main_db, row_cap=100, timeout_seconds=5)
    assert scan_table(scan).row_count == 2
    one = scan_table(scan, top=1)
    assert (one.row_count, one.rows[0][1]) == (1, "Rome, Italy")
    text = render_scan(scan, top=1)
    assert "The table shows the first 1 of 2 listed segments." in text
    assert "5411" not in text.split("Notes:")[0]
    assert text.splitlines()[0] == explore.headline(scan)
    assert f"  - [label_coverage] {LABEL_COVERAGE}" in text


# --- The command ------------------------------------------------------------------------------


@pytest.fixture
def command(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """The fixture database, no `.env`, and no model of any kind."""

    def forbidden(spec: ModelSpec, settings: Settings) -> IntentModel:
        raise AssertionError("no model may be built here")

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "_live_model", forbidden)
    explore.clear_cache()


def test_the_scan_command_prints_the_table_and_the_residual(
    command: None, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    assert cli.main(["scan"]) == 0
    output = capsys.readouterr().out
    scan = explore.scan(db_path=settings.db_path, row_cap=1, timeout_seconds=1)
    assert output == render_scan(scan) + "\n"
    assert "merchant city and state  Rome, Italy" in output
    assert "Outside merchant city and state = Rome, Italy, 39 labelled frauds remain" in output
    assert settings.anthropic_api_key is None, "it ran without a key"


def test_the_scan_command_takes_a_period_and_a_number_of_rows(
    command: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["scan", "--top", "1"]) == 0
    assert "The table shows the first 1 of 2 listed segments." in capsys.readouterr().out
    assert cli.main(["scan", "--period", "2019-01-01", "2019-02-28"]) == 0
    output = capsys.readouterr().out
    assert "Scanned 400 labelled transactions (2019-01-01 to 2019-02-28): 20 labelled frauds" in output
    assert "merchant city and state  Sacramento, CA" in output


def test_the_scan_command_can_print_every_statement(
    command: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["scan", "--sql"]) == 0
    output = capsys.readouterr().out
    assert "\nStatement 1 of 11:\nWITH labelled AS MATERIALIZED (" in output
    assert "\nStatement 11 of 11:\nSELECT count(*) AS in_both" in output


@pytest.mark.parametrize(
    "arguments",
    [["--period", "2019-13-01", "2019-12-31"], ["--period", "2019-02-01", "2019-01-01"]],
    ids=["not a date", "backwards"],
)
def test_a_bad_period_cannot_start(
    command: None, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    assert cli.main(["scan", *arguments]) == 2
    assert capsys.readouterr().out.startswith("Cannot start: ")


def test_bad_arguments_and_a_missing_database(
    command: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for arguments in (["--top", "0"], ["--period", "2019-01-01"]):
        with pytest.raises(SystemExit) as stopped:
            cli.main(["scan", *arguments])
        assert stopped.value.code == 2
    capsys.readouterr()
    gone = load_settings({"FSM_DB_PATH": str(tmp_path / "gone.db")}, env_file=None)
    monkeypatch.setattr(cli, "load_settings", lambda: gone)
    assert cli.main(["scan"]) == 1
    assert capsys.readouterr().out.startswith("The scan failed: ")


def test_ask_prints_a_scan_and_exits_with_0(
    command: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    keyed = settings.model_copy(update={"anthropic_api_key": "not-a-real-key"})
    monkeypatch.setattr(cli, "load_settings", lambda: keyed)
    monkeypatch.setattr(cli, "_live_model", lambda spec, settings: ScriptedModel([SCAN]))
    assert cli.main(["ask", "Where is fraud concentrated?"]) == 0
    assert "merchant city and state  Rome, Italy" in capsys.readouterr().out


# --- The shipped scan cases, with scripted replies, on the real data --------------------------


def test_real_data_the_scan_cases_pass_when_the_model_does_what_they_expect(real_db: Path) -> None:
    settings = load_settings({"FSM_DB_PATH": str(real_db)}, env_file=None)
    suite = load_suite()
    ask = call_reply("ask_clarification", question="Which segment?", suggestions=["a", "b"])
    scripts = {
        "scan-where-is-fraud": [SCAN],
        "scan-profile-request": [SCAN],
        "scan-hotspots-casual": [SCAN],
        "scan-then-rule-for-a-named-segment": [SCAN, rule_reply(ROME_RULE)],
        "scan-then-that-without-naming-it": [SCAN, ask],
    }
    assert set(scripts) == {case.id for case in suite.cases if case.requires == "scan"}
    for case_id, script in scripts.items():
        run = run_case(suite.case(case_id), suite, settings, ScriptedModel(script))
        failed = [score for turn in run.turns for score in turn.scores if not score.passed]
        assert run.passed, (case_id, failed)

    # The follow-up that names the segment is the scan's own first suggestion, word for word.
    named = suite.case("scan-then-rule-for-a-named-segment").turns[1]
    shown = Assistant(settings, ScriptedModel([SCAN])).run_turn("Where is fraud concentrated?", "c")
    assert named.question == shown.suggestions[0]
    assert named.clause == ROME_RULE
    assert [caveat.code for caveat in shown.caveats] == ["label_coverage", "single_segment"]
    # A scan row that led with another city would fail the case.
    wrong = run_case(suite.case("scan-where-is-fraud"), suite, settings, ScriptedModel([sql_reply(COUNT)]))
    assert not wrong.passed
