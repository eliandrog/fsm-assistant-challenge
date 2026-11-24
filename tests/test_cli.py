"""cli: arguments, exit codes, what is printed, and nothing sent without `--yes-spend`.

The check itself is tested in test_model_check.py.
"""

from pathlib import Path

import pytest
from conftest import MIXED_RUN, StampingModel
from langchain_anthropic import ChatAnthropic

from fsm_assistant import cli, config, model_check
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.llm import (
    IntentModel,
    ModelError,
    ModelReply,
    ModelSpec,
    ModelUnavailable,
    ScriptedModel,
)

SECRET = "not-a-real-key-0123456789"
RECORDING_NAMES = [f"q{number:02d}.turn1.call1.json" for number in range(1, 12)]


@pytest.fixture(autouse=True)
def recordings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No test writes into the repository's recordings folder."""
    directory = tmp_path / "recordings"
    monkeypatch.setattr(model_check, "CHECK_RECORDINGS_DIR", directory)
    return directory


@pytest.fixture
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


def record(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    """Run `check-model --yes-spend` against a scripted model; return what it printed."""

    def factory(spec: ModelSpec, settings: Settings) -> IntentModel:
        return StampingModel(spec, list(MIXED_RUN))

    monkeypatch.setattr(cli, "_live_model", factory)
    assert cli.main(["check-model", "--yes-spend"]) == 0
    return capsys.readouterr().out


def no_live_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(spec: ModelSpec, settings: Settings) -> IntentModel:
        raise AssertionError("replay may not build a live model")

    monkeypatch.setattr(cli, "_live_model", forbidden)


def table(output: str) -> str:
    """The table and the decision: from the header row to the decision line."""
    lines = output.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(" #  question"))
    end = next(index for index, line in enumerate(lines) if line.startswith("Decision rule"))
    return "\n".join(lines[start : end + 1])


def test_without_the_flag_nothing_is_sent(
    isolated: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(spec: ModelSpec, settings: Settings) -> IntentModel:
        raise AssertionError("no model may be built without --yes-spend")

    monkeypatch.setattr(cli, "_live_model", forbidden)
    assert cli.main(["check-model"]) == 0
    output = capsys.readouterr().out
    assert "COST WARNING" in output
    assert "Nothing was sent." in output
    assert "claude-opus-5-5" in output
    assert "ANTHROPIC_API_KEY is set." in output
    assert "To run it, add --yes-spend." in output
    assert all(question in output for question in model_check.CHECK_QUESTIONS)
    assert SECRET not in output


def test_nothing_is_written_without_a_flag(
    isolated: None, recordings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check-model"]) == 0
    assert "Nothing was sent." in capsys.readouterr().out
    assert not recordings.exists()


def test_the_model_flag_is_shown_in_the_plan(
    isolated: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check-model", "--model", "claude-haiku-5-5"]) == 0
    assert "to claude-haiku-5-5 in tool mode" in capsys.readouterr().out


def test_a_complete_run_prints_the_table_the_decision_and_what_it_recorded(
    isolated: None,
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = record(monkeypatch, capsys)
    assert "COST WARNING" in output
    assert "8 of 11 replies were exactly one valid tool call (3 missed)." in output
    assert "structured output should become the default mode" in output
    assert "(text only)" in output and "no (no_tool_call)" in output
    assert "no (multiple_tool_calls)" in output and "no (refusal)" in output
    assert "total: 11,000 input tokens, 1,100 output tokens" in output
    assert "Recorded 11 of 11 replies and summary.json" in output
    assert "--replay will stop there" not in output
    assert SECRET not in output
    directory = recordings / "claude-opus-5-5"
    assert sorted(path.name for path in directory.iterdir()) == [*RECORDING_NAMES, "summary.json"]


def test_a_run_stops_at_an_authentication_failure(
    isolated: None,
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = script(monkeypatch, [ModelUnavailable("authentication", "invalid x-api-key")])
    assert cli.main(["check-model", "--yes-spend"]) == 2
    output = capsys.readouterr().out
    assert model.calls == 1
    assert "Stopped after the first request: authentication." in output
    assert "Incomplete run: no decision." in output
    assert "Decision rule" not in output
    assert not recordings.exists()


def test_a_missing_key_is_reported_without_a_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: load_settings({}, env_file=None))
    assert cli.main(["check-model", "--yes-spend"]) == 2
    output = capsys.readouterr().out
    assert "Cannot start: ANTHROPIC_API_KEY is not set" in output
    assert cli.main(["check-model"]) == 0
    assert "ANTHROPIC_API_KEY is NOT set." in capsys.readouterr().out


def test_the_check_uses_tool_mode_and_its_own_prompt_version(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[ModelSpec] = []

    def factory(spec: ModelSpec, settings: Settings) -> IntentModel:
        seen.append(spec)
        return ScriptedModel([ModelUnavailable("authentication", "stop")])

    monkeypatch.setattr(cli, "_live_model", factory)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: load_settings(
            {"FSM_TOOL_MODE": "structured", "FSM_DB_PATH": str(config.DEFAULT_DB_PATH)}, None
        ),
    )
    cli.main(["check-model", "--yes-spend", "--model", "claude-haiku-5-5"])
    assert (seen[0].model, seen[0].tool_mode, seen[0].prompt_version) == (
        "claude-haiku-5-5",
        "tools",
        "check-model-1",
    )


def test_a_provider_error_that_quotes_the_key_leaves_no_key_on_disk(
    isolated: None,
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def create(chat: ChatAnthropic, payload: object) -> object:
        raise RuntimeError(f"bad header x-api-key: {SECRET}")

    monkeypatch.setattr(ChatAnthropic, "_create", create)
    assert cli.main(["check-model", "--yes-spend"]) == 0
    output = capsys.readouterr().out
    assert "Recorded 0 of 11 replies" in output and SECRET not in output
    assert "A request that got no reply is not recorded, so --replay will stop there." in output
    written = [path for path in recordings.rglob("*") if path.is_file()]
    assert [path.name for path in written] == ["summary.json"]
    assert SECRET not in written[0].read_text()
    summary = model_check.CheckSummary.model_validate_json(written[0].read_text())
    assert {row.failure for row in summary.questions} == {"other"}


def test_replay_reprints_the_same_table_with_no_key_and_no_model(
    isolated: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fixture_db: Path,
) -> None:
    recorded = record(monkeypatch, capsys)
    no_live_model(monkeypatch)
    monkeypatch.setattr(
        cli, "load_settings", lambda: load_settings({"FSM_DB_PATH": str(fixture_db)}, None)
    )
    assert cli.main(["check-model", "--replay"]) == 0
    replayed = capsys.readouterr().out
    assert table(replayed) == table(recorded)
    assert "no (refusal)" in replayed and "3 missed" in replayed
    assert "Nothing is sent." in replayed and "COST WARNING" not in replayed


def test_replay_without_recordings_fails_as_stale(
    isolated: None,
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    no_live_model(monkeypatch)
    assert cli.main(["check-model", "--replay"]) == 2
    output = capsys.readouterr().out
    assert "Cannot replay: stale recording: no run was recorded for this model" in output
    assert not recordings.exists()


def test_replay_of_another_model_fails_as_stale(
    isolated: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record(monkeypatch, capsys)
    assert cli.main(["check-model", "--replay", "--model", "claude-haiku-5-5"]) == 2
    assert "Cannot replay: stale recording" in capsys.readouterr().out


def test_a_stale_replay_exits_with_2_and_prints_no_decision(
    isolated: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record(monkeypatch, capsys)
    monkeypatch.setattr(model_check, "CHECK_PROMPT_VERSION", "check-model-2")
    assert cli.main(["check-model", "--replay"]) == 2
    output = capsys.readouterr().out
    assert "Cannot replay: stale recording: the prompt, schemas or model settings changed" in output
    assert "(q01.turn1.call1.json)" in output and "Decision rule" not in output
    assert "Recordings are made by a paid run: check-model --yes-spend." in output


def test_spend_and_replay_cannot_be_combined(isolated: None, recordings: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["check-model", "--yes-spend", "--replay"])
    assert raised.value.code == 2
    assert not recordings.exists()


def test_a_model_id_that_is_not_a_folder_name_is_refused(
    isolated: None, recordings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check-model", "--replay", "--model", "../elsewhere"]) == 2
    assert "Cannot name a recordings folder" in capsys.readouterr().out
    assert cli.main(["check-model", "--yes-spend", "--model", "../elsewhere"]) == 2
    assert "Cannot name a recordings folder" in capsys.readouterr().out
    assert not recordings.exists()
