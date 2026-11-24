"""model_check: the eleven questions, a run that records, the summary, the replay, the text."""

import json
import re
from pathlib import Path

import pytest
from conftest import MIXED_RUN, StampingModel, tool_reply
from pydantic import JsonValue

from fsm_assistant import config, model_check, nl2sql
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.llm import (
    ModelError,
    ModelOutputError,
    ModelReply,
    ModelSpec,
    ModelUnavailable,
    ScriptedModel,
    StaleRecording,
)
from fsm_assistant.model_check import CheckSummary

SECRET = "not-a-real-key-0123456789"
RECORDING_NAMES = [f"q{number:02d}.turn1.call1.json" for number in range(1, 12)]


@pytest.fixture(autouse=True)
def recordings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No test writes into the repository's recordings folder."""
    directory = tmp_path / "recordings"
    monkeypatch.setattr(model_check, "CHECK_RECORDINGS_DIR", directory)
    return directory


@pytest.fixture
def settings(fixture_db: Path) -> Settings:
    """Settings that ignore the developer's `.env` and point at the fixture database."""
    return load_settings(
        {"ANTHROPIC_API_KEY": SECRET, "FSM_DB_PATH": str(fixture_db)}, env_file=None
    )


def folder(settings: Settings) -> Path:
    directory = model_check.recordings_directory(settings.anthropic_model)
    assert directory is not None
    return directory


def run(settings: Settings, model: ScriptedModel) -> CheckSummary:
    spec = model_check.check_spec(settings)
    return model_check.run_check(
        model, spec, model_check.system_message(settings), folder(settings)
    )


def record(settings: Settings, steps: list[ModelReply | ModelError] = MIXED_RUN) -> CheckSummary:
    """A complete run against a scripted model, with its summary written beside the recordings."""
    spec = model_check.check_spec(settings)
    summary = model_check.run_check(
        StampingModel(spec, list(steps)),
        spec,
        model_check.system_message(settings),
        folder(settings),
    )
    model_check.write_summary(folder(settings), summary)
    return summary


def replay(settings: Settings) -> CheckSummary:
    return model_check.replay_check(
        model_check.check_spec(settings), model_check.system_message(settings), folder(settings)
    )


def rendered(summary: CheckSummary) -> str:
    table = model_check.render_table(summary.questions)
    return f"{table}\n{model_check.render_decision(summary)}"


def strings_and_keys(value: JsonValue) -> list[str]:
    if isinstance(value, dict):
        return [*value, *(text for item in value.values() for text in strings_and_keys(item))]
    if isinstance(value, list):
        return [text for item in value for text in strings_and_keys(item)]
    return [value] if isinstance(value, str) else []


def test_eleven_fixed_questions() -> None:
    assert len(model_check.CHECK_QUESTIONS) == 11
    assert "Delete all transactions from January" in model_check.CHECK_QUESTIONS
    assert (
        model_check.CHECK_QUESTIONS[-1] == "Propose a rule for debit card transactions over $1,000"
    )


def test_a_run_with_one_miss_keeps_tools_as_the_default(settings: Settings) -> None:
    steps: list[ModelReply | ModelError] = [tool_reply("run_sql")] * 5
    steps += [tool_reply("decline"), tool_reply("ask_clarification"), tool_reply("decline")]
    steps += [tool_reply(), tool_reply("decline"), tool_reply("propose_rule")]
    model = ScriptedModel(steps)
    summary = run(settings, model)
    output = rendered(summary)
    assert model.calls == 11
    assert [request.user for request in model.requests] == list(model_check.CHECK_QUESTIONS)
    assert all("transactions" in request.system for request in model.requests)
    assert (summary.totals.missed, summary.decision) == (1, "tools")
    assert "10 of 11 replies were exactly one valid tool call (1 missed)." in output
    assert "tools stay the default mode" in output
    assert "(text only)" in output and "no (no_tool_call)" in output
    assert "total: 11,000 input tokens, 1,100 output tokens" in output
    assert SECRET not in output


def test_a_run_with_two_misses_recommends_structured_output(settings: Settings) -> None:
    steps: list[ModelReply | ModelError] = [tool_reply("run_sql")] * 9
    steps.append(tool_reply("run_sql", "decline"))
    steps.append(ModelOutputError("refusal", tool_reply(stop_reason="refusal")))
    summary = run(settings, ScriptedModel(steps))
    output = rendered(summary)
    assert summary.decision == "structured"
    assert "9 of 11 replies were exactly one valid tool call (2 missed)." in output
    assert "structured output should become the default mode" in output
    assert "no (multiple_tool_calls)" in output and "no (refusal)" in output


def test_a_run_stops_at_an_authentication_failure(settings: Settings, recordings: Path) -> None:
    model = ScriptedModel([ModelUnavailable("authentication", "invalid x-api-key")])
    with pytest.raises(model_check.IncompleteRun) as raised:
        run(settings, model)
    assert model.calls == 1
    assert raised.value.stopped_by == "authentication"
    assert [row.failure for row in raised.value.rows] == ["authentication"]
    assert "no (authentication)" in model_check.render_table(raised.value.rows)
    assert not recordings.exists()


def test_the_check_uses_tool_mode_and_its_own_prompt_version() -> None:
    structured = load_settings(
        {"FSM_TOOL_MODE": "structured", "FSM_DB_PATH": str(config.DEFAULT_DB_PATH)}, None
    )
    spec = model_check.check_spec(structured, "claude-haiku-5-5")
    assert (spec.model, spec.tool_mode, spec.prompt_version) == (
        "claude-haiku-5-5",
        "tools",
        "check-model-1",
    )
    assert model_check.check_spec(structured).model == structured.anthropic_model


def test_the_plan_names_the_model_and_the_upper_bound(settings: Settings) -> None:
    plan = model_check.render_plan(model_check.check_spec(settings, "claude-haiku-5-5"))
    assert "11 questions, one request each, to claude-haiku-5-5 in tool mode" in plan
    assert "COST WARNING" in plan and "up to 88,000 output tokens in total" in plan
    assert SECRET not in plan


def test_a_run_records_every_reply_and_a_summary(settings: Settings, recordings: Path) -> None:
    returned = record(settings)
    directory = recordings / "claude-opus-5-5"
    assert sorted(path.name for path in directory.iterdir()) == [*RECORDING_NAMES, "summary.json"]
    assert model_check.recorded_count(returned) == 11

    first = json.loads((directory / RECORDING_NAMES[0]).read_text())
    assert set(first) == {"format_version", "case_id", "turn", "call", "reply"}
    assert (first["case_id"], first["reply"]["tool_calls"][0]["name"]) == ("q01", "run_sql")
    refusal = json.loads((directory / RECORDING_NAMES[9]).read_text())
    assert refusal["reply"]["stop_reason"] == "refusal"

    summary = CheckSummary.model_validate_json((directory / "summary.json").read_text())
    assert summary == returned == model_check.read_summary(directory)
    assert (summary.model, summary.effort) == ("claude-opus-5-5", "medium")
    assert summary.max_tokens == 8000
    assert (summary.tool_mode, summary.prompt_version) == ("tools", "check-model-1")
    versions = summary.library_versions.model_dump()
    assert set(versions) == {"langchain_anthropic", "langchain_core", "langgraph"}
    assert all(re.fullmatch(r"\d+\.\d+.*", number) for number in versions.values())
    assert [row.question for row in summary.questions] == list(model_check.CHECK_QUESTIONS)
    assert [row.case_id for row in summary.questions] == [f"q{n:02d}" for n in range(1, 12)]
    assert summary.questions[0].tools_called == ("run_sql",)
    assert summary.questions[0].one_valid_call and summary.questions[0].failure is None
    assert [row.failure for row in summary.questions[7:10]] == [
        "no_tool_call",
        "multiple_tool_calls",
        "refusal",
    ]
    assert summary.questions[8].tools_called == ("run_sql", "decline")
    assert (summary.totals.one_valid_call, summary.totals.missed) == (8, 3)
    assert (summary.totals.input_tokens, summary.totals.output_tokens) == (11000, 1100)
    assert (summary.allowed_misses, summary.decision) == (1, "structured")


def test_a_request_that_got_no_reply_is_not_counted_as_recorded(
    settings: Settings, recordings: Path
) -> None:
    steps: list[ModelReply | ModelError] = [tool_reply("run_sql")] * 10
    steps.append(ModelUnavailable("timeout", "no answer"))
    summary = run(settings, ScriptedModel(steps))
    assert model_check.recorded_count(summary) == 10
    assert len(list((recordings / "claude-opus-5-5").iterdir())) == 10
    assert model_check.render_table(summary.questions).splitlines()[11].split()[-6] == "-"


def test_the_summary_holds_no_timestamp_no_path_and_no_key(
    settings: Settings, recordings: Path
) -> None:
    record(settings)
    text = (recordings / "claude-opus-5-5" / "summary.json").read_text()
    document: JsonValue = json.loads(text)
    assert isinstance(document, dict)
    assert set(document) == {
        "model",
        "effort",
        "max_tokens",
        "tool_mode",
        "prompt_version",
        "library_versions",
        "questions",
        "totals",
        "allowed_misses",
        "decision",
    }
    for value in strings_and_keys(document):
        assert not re.search(r"date|time|stamp|created|_at$|path|directory|key", value.lower())
        assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}", value)
        assert not value.startswith(("/", "~")) and "\\" not in value
    assert SECRET not in text and str(recordings) not in text and str(Path.home()) not in text


def test_a_recording_holds_neither_the_system_prompt_nor_the_question(
    settings: Settings, recordings: Path
) -> None:
    record(settings)
    for name in RECORDING_NAMES:
        text = (recordings / "claude-opus-5-5" / name).read_text()
        assert set(json.loads(text)["reply"]) == {
            "tool_mode",
            "tool_calls",
            "text",
            "stop_reason",
            "usage",
            "model_id",
            "response_id",
            "request_hash",
        }
        assert "fraud analyst" not in text and "CREATE TABLE" not in text
        assert not any(question in text for question in model_check.CHECK_QUESTIONS)
        assert SECRET not in text


def test_replay_rebuilds_the_same_summary_and_the_same_text(
    settings: Settings, fixture_db: Path
) -> None:
    recorded = record(settings)
    without_key = load_settings({"FSM_DB_PATH": str(fixture_db)}, None)
    replayed = replay(without_key)
    assert replayed == recorded
    assert rendered(replayed) == rendered(recorded)
    assert "no (refusal)" in rendered(replayed) and "3 missed" in rendered(replayed)


def test_reading_a_summary_that_was_never_written_fails_as_stale(
    settings: Settings, recordings: Path
) -> None:
    with pytest.raises(StaleRecording, match="no run was recorded for this model") as raised:
        replay(settings)
    assert raised.value.kind == "missing"
    assert not recordings.exists()


def test_a_file_that_is_not_a_summary_or_lists_other_questions_fails_as_stale(
    settings: Settings,
) -> None:
    recorded = record(settings)
    path = folder(settings) / model_check.SUMMARY_FILE
    shorter = recorded.model_copy(update={"questions": recorded.questions[:10]})
    path.write_text(shorter.model_dump_json())
    with pytest.raises(StaleRecording, match="the list of questions changed"):
        model_check.read_summary(folder(settings))
    path.write_text('{"model": "claude-opus-5-5"}')
    with pytest.raises(StaleRecording, match="the file is not a run summary") as raised:
        model_check.read_summary(folder(settings))
    assert raised.value.kind == "unreadable"


@pytest.mark.parametrize("changed", ["prompt", "prompt_version", "schema"])
def test_replay_after_a_changed_prompt_or_schema_fails_as_stale(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    record(settings)
    if changed == "prompt":
        monkeypatch.setattr(model_check, "_CHECK_INSTRUCTIONS", "Answer briefly.")
    elif changed == "prompt_version":
        monkeypatch.setattr(model_check, "CHECK_PROMPT_VERSION", "check-model-2")
    else:
        original = nl2sql.model_spec

        def edited(settings: Settings) -> ModelSpec:
            spec = original(settings)
            tool = spec.tools[0].model_copy(update={"description": "Edited."})
            return spec.model_copy(update={"tools": (tool, *spec.tools[1:])})

        monkeypatch.setattr(model_check, "model_spec", edited)
    with pytest.raises(StaleRecording) as raised:
        replay(settings)
    message = str(raised.value)
    assert "stale recording: the prompt, schemas or model settings changed" in message
    assert "(q01.turn1.call1.json)" in message


def test_replay_fails_when_a_recording_is_missing_or_edited(
    settings: Settings, recordings: Path
) -> None:
    record(settings)
    path = recordings / "claude-opus-5-5" / RECORDING_NAMES[2]
    document = json.loads(path.read_text())
    document["reply"]["tool_calls"] = []
    path.write_text(json.dumps(document))
    with pytest.raises(StaleRecording, match="the summary does not match the recorded replies"):
        replay(settings)
    path.unlink()
    with pytest.raises(StaleRecording) as raised:
        replay(settings)
    assert "no reply was recorded for this call (q03.turn1.call1.json)" in str(raised.value)


def test_a_model_id_that_is_not_a_folder_name_has_no_recordings_folder(recordings: Path) -> None:
    assert model_check.recordings_directory("../elsewhere") is None
    assert model_check.recordings_directory("") is None
    assert model_check.recordings_directory("claude-haiku-5-5") == recordings / "claude-haiku-5-5"


def test_a_folder_inside_the_project_is_shown_relative_to_it(tmp_path: Path) -> None:
    inside = config.PROJECT_ROOT / "evals" / "recordings" / "check-model" / "claude-opus-5-5"
    assert model_check.display_path(inside) == "evals/recordings/check-model/claude-opus-5-5"
    assert model_check.display_path(tmp_path) == str(tmp_path)
