"""cli: `ask`, `chat`, `graph` and `--trace`, with a scripted model in place of Claude."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import StampingModel, call_reply, rule_reply, sql_reply

from fsm_assistant import cli
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.graph import NODE_NAMES
from fsm_assistant.llm import (
    IntentModel,
    ModelError,
    ModelReply,
    ModelRequest,
    ModelSpec,
    ScriptedModel,
)

SECRET = "not-a-real-key-0123456789"
COUNT = "SELECT count(*) FROM transactions"
TAGS = "SELECT 'row-' || id || '-marker' AS tag FROM users ORDER BY id"
RULE = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, fixture_db: Path) -> None:
    """Settings that ignore the developer's `.env` and point at the fixture database."""

    def load() -> Settings:
        return load_settings(
            {"ANTHROPIC_API_KEY": SECRET, "FSM_DB_PATH": str(fixture_db)}, env_file=None
        )

    monkeypatch.setattr(cli, "load_settings", load)


def script(monkeypatch: pytest.MonkeyPatch, steps: list[ModelReply | ModelError]) -> ScriptedModel:
    model = ScriptedModel(steps)

    def factory(spec: ModelSpec, settings: Settings) -> IntentModel:
        return model

    monkeypatch.setattr(cli, "_live_model", factory)
    return model


def no_live_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(spec: ModelSpec, settings: Settings) -> IntentModel:
        raise AssertionError("no live model may be built here")

    monkeypatch.setattr(cli, "_live_model", forbidden)


def typed(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    """Feed `lines` to the chat prompt, then end the input as Ctrl-D does."""
    remaining: Iterator[str] = iter(lines)

    def read(prompt: str = "") -> str:
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", read)


def test_ask_prints_the_model_in_use_the_answer_and_the_sql(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model = script(monkeypatch, [sql_reply(COUNT, "All transactions")])
    assert cli.main(["ask", "How many transactions are there?"]) == 0
    output = capsys.readouterr().out
    assert model.calls == 1
    assert output.startswith("Model: claude-opus-5-5, live. Each question is billed")
    assert "count(*): 6" in output
    assert f"SQL that ran:\n    {COUNT}" in output
    assert "Model's explanation (not checked): What it returns." in output
    assert "Model's assumptions:\n  - All transactions" in output
    assert "[trace]" not in output
    assert SECRET not in output


def test_ask_shows_a_rule_with_its_backtest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script(monkeypatch, [rule_reply(RULE)])
    assert cli.main(["ask", "A rule for big debit payments"]) == 0
    output = capsys.readouterr().out
    assert f"Rule (a WHERE clause over transactions):\n    {RULE}" in output
    assert "Fraud caught          1 of 1 labelled fraud" in output
    assert "[small_sample]" in output


def test_ask_exits_with_1_when_the_turn_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script(monkeypatch, [])
    assert cli.main(["ask", "   "]) == 1
    assert "Failed (invalid_input): The message is empty." in capsys.readouterr().out


def test_a_clarification_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ask = call_reply("ask_clarification", question="What is risky?", suggestions=["a", "b"])
    script(monkeypatch, [ask])
    assert cli.main(["ask", "Show me the risky transactions"]) == 0
    assert "The assistant asks: What is risky?" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("replies", "code", "line"),
    [
        ([call_reply("decline", reason="No email column.")], 3, "The assistant declined: "),
        ([call_reply("profile_fraud")], 3, "The fraud-concentration scan is not available yet."),
        ([sql_reply("SELECT name FROM sqlite_master")] * 2, 4, "Rejected (sql_rejected): "),
        ([sql_reply("SELECT nope FROM users")] * 2, 1, "Failed (sql_execution): "),
    ],
    ids=["declined", "scan not available", "rejected", "failed"],
)
def test_ask_exit_codes_tell_the_outcomes_apart(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    replies: list[ModelReply | ModelError],
    code: int,
    line: str,
) -> None:
    script(monkeypatch, replies)
    assert cli.main(["ask", "A question"]) == code
    assert line in capsys.readouterr().out


class Interrupted:
    """A model during one of whose requests the user presses Ctrl-C."""

    def __init__(self, at_call: int) -> None:
        self.at_call = at_call
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        if len(self.requests) == self.at_call:
            raise KeyboardInterrupt
        return sql_reply(COUNT)


def interrupted(monkeypatch: pytest.MonkeyPatch, at_call: int) -> Interrupted:
    model = Interrupted(at_call)

    def factory(spec: ModelSpec, settings: Settings) -> IntentModel:
        return model

    monkeypatch.setattr(cli, "_live_model", factory)
    return model


def test_ctrl_c_during_a_chat_turn_returns_to_the_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model = interrupted(monkeypatch, at_call=2)
    typed(monkeypatch, "How many transactions?", "A slow question", "And then?")
    assert cli.main(["chat"]) == 0
    output = capsys.readouterr().out
    assert "Interrupted. That question was not answered." in output
    assert output.count("count(*): 6") == 2, "the conversation went on"
    assert "Traceback" not in output
    follow_up = model.requests[2].user
    assert "<earlier_question>\nHow many transactions?\n</earlier_question>" in follow_up
    assert "A slow question" not in follow_up


def test_ctrl_c_during_ask_exits_with_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    interrupted(monkeypatch, at_call=1)
    assert cli.main(["ask", "A slow question"]) == 130
    assert capsys.readouterr().out.rstrip().endswith("Interrupted.")


def test_the_trace_cannot_move_the_cursor_or_fail_to_print(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script(
        monkeypatch,
        [
            call_reply("run_sql\ud800\x1b[2J"),
            sql_reply("SELECT 1 /* \x1b[2J\x1b[31m */ -- \r overwritten"),
        ],
    )
    assert cli.main(["ask", "--trace", "A question"]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output and "\r" not in output
    output.encode("utf-8")
    assert "accepted SQL: SELECT 1 /* \\x1b[2J\\x1b[31m */ -- \\r overwritten" in output
    assert "[trace] check_intent: failure model_output (repairable)" in output


@pytest.mark.parametrize("command", [["ask", "q"], ["chat"], ["check-model"]])
def test_a_bad_setting_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    monkeypatch.setattr(
        cli, "load_settings", lambda: load_settings({"FSM_ROW_CAP": "abc"}, env_file=None)
    )
    no_live_model(monkeypatch)
    assert cli.main(command) == 2
    assert capsys.readouterr().out == "Cannot start: FSM_ROW_CAP must be a number, got 'abc'\n"


def test_graph_reads_no_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden() -> Settings:
        raise AssertionError("graph must not read the settings, the .env file or the key")

    monkeypatch.setattr(cli, "load_settings", forbidden)
    monkeypatch.setenv("FSM_ROW_CAP", "abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    assert cli.main(["graph"]) == 0
    assert capsys.readouterr().out.startswith("graph TD;")


def test_an_unusable_record_dir_is_refused_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    model = script(monkeypatch, [sql_reply(COUNT)])
    a_file = tmp_path / "taken"
    a_file.write_text("not a folder")
    assert cli.main(["ask", "--record-dir", str(a_file), "q"]) == 2
    assert cli.main(["ask", "--record-dir", str(a_file / "below"), "q"]) == 2
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    assert cli.main(["chat", "--record-dir", str(locked)]) == 2
    assert cli.main(["ask", "--record-dir", str(locked / "below"), "q"]) == 2
    output = capsys.readouterr().out
    assert output.count("Cannot start: --record-dir ") == 4
    assert "is not writable." in output and "cannot be used as a folder" in output
    assert model.calls == 0
    assert a_file.read_text() == "not a folder"


@pytest.mark.parametrize("command", [["ask", "How many?"], ["chat"]])
def test_a_missing_key_is_reported_and_nothing_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: load_settings({}, env_file=None))
    typed(monkeypatch, "How many?")
    assert cli.main(command) == 2
    output = capsys.readouterr().out
    assert output.startswith("Cannot start: ANTHROPIC_API_KEY is not set")
    assert "Traceback" not in output


def test_trace_prints_each_node_and_no_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script(monkeypatch, [sql_reply("SELECT name FROM sqlite_master"), sql_reply(TAGS)])
    assert cli.main(["ask", "--trace", "List the tags"]) == 0
    output = capsys.readouterr().out
    trace = "\n".join(line for line in output.splitlines() if line.startswith("  [trace] "))
    for node in ("begin_turn", "call_model", "check_intent", "guard_sql", "execute_sql",
                 "present", "remember"):  # fmt: skip
        assert f"[trace] {node}: " in trace
    assert "failure sql_rejected (repairable)" in trace
    assert "2 row(s)" in trace
    assert "row-1-marker" in output, "the rows are in the answer"
    assert "row-1-marker" not in trace, "and not in the trace"
    assert SECRET not in output


def test_graph_prints_every_node_and_needs_no_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: load_settings({}, env_file=None))
    no_live_model(monkeypatch)
    assert cli.main(["graph"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("graph TD;")
    for node in NODE_NAMES:
        assert node in output


def test_chat_keeps_one_conversation_until_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model = script(monkeypatch, [sql_reply(COUNT), sql_reply(TAGS), sql_reply(COUNT)])
    typed(monkeypatch, "How many transactions?", "", "And the tags?", "EXIT", "never asked")
    assert cli.main(["chat"]) == 0
    output = capsys.readouterr().out
    assert model.calls == 2
    assert "count(*): 6" in output and "row-2-marker" in output
    assert "<earlier_question>\nHow many transactions?\n</earlier_question>" in (
        model.requests[1].user
    )
    assert (model.requests[1].turn, model.requests[1].call) == (2, 1)


def test_chat_ends_at_end_of_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model = script(monkeypatch, [sql_reply(COUNT)])
    typed(monkeypatch, "How many transactions?")
    assert cli.main(["chat", "--trace"]) == 0
    assert model.calls == 1
    assert "[trace] present: outcome answered" in capsys.readouterr().out


def test_a_recorded_turn_replays_with_no_key_and_no_live_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    fixture_db: Path,
) -> None:
    def factory(spec: ModelSpec, settings: Settings) -> IntentModel:
        return StampingModel(spec, [sql_reply(COUNT)])

    monkeypatch.setattr(cli, "_live_model", factory)
    question = "How many transactions are there?"
    assert cli.main(["ask", "--record-dir", str(tmp_path), "--case", "count", question]) == 0
    recorded = capsys.readouterr().out
    assert [path.name for path in tmp_path.iterdir()] == ["count.turn1.call1.json"]
    assert SECRET not in (tmp_path / "count.turn1.call1.json").read_text()

    without_key = load_settings({"FSM_DB_PATH": str(fixture_db)}, env_file=None)
    monkeypatch.setattr(cli, "load_settings", lambda: without_key)
    no_live_model(monkeypatch)
    assert cli.main(["ask", "--replay-dir", str(tmp_path), "--case", "count", question]) == 0
    replayed = capsys.readouterr().out
    assert "Nothing is sent." in replayed
    assert "count(*): 6" in recorded and "count(*): 6" in replayed

    assert cli.main(["ask", "--replay-dir", str(tmp_path), "--case", "count", "Another one"]) == 1
    assert "stale recording" in capsys.readouterr().out


def test_replay_and_record_cannot_be_combined(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["ask", "--replay-dir", str(tmp_path), "--record-dir", str(tmp_path), "q"])
    assert stopped.value.code == 2


def test_a_bad_case_name_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    no_live_model(monkeypatch)
    assert cli.main(["ask", "--replay-dir", str(tmp_path), "--case", "../x", "q"]) == 2
    assert "Cannot start: case id must be" in capsys.readouterr().out
