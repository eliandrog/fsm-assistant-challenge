"""The evaluation runner, a recorded run and its replay, and the `eval` command.

A small suite of eleven cases is written for the fixture database: one case
per way a turn can end, a follow-up, a repaired turn, a wrong answer, an
internal failure and a case that needs the scan. Every model reply is
scripted; nothing is sent.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import get_args

import pytest
from conftest import (
    RecordingSink,
    StampingModel,
    call_reply,
    marked_evidence,
    rule_reply,
    sql_reply,
)
from pydantic import JsonValue
from scan_fixtures import MAIN, build

from fsm_assistant import cli, db, explore, nl2sql, nodes, prompt
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.evals import command, evidence, recorded
from fsm_assistant.evals.cases import Case, Fact, Suite, Tier, load_cases, load_suite
from fsm_assistant.evals.command import Finish, Options
from fsm_assistant.evals.evidence import build_pins, write_pins
from fsm_assistant.evals.recorded import LiveRun, Replay, read_record, record_run, replay_run
from fsm_assistant.evals.runner import (
    CaseRun,
    Selection,
    UnknownCase,
    available_features,
    run_case,
    run_suite,
    select,
)
from fsm_assistant.evals.scorers import FailureReason, Note
from fsm_assistant.schema_context import build_schema_context
from fsm_assistant.result import CaveatCode
from fsm_assistant.tracing import Destination, Tracer
from fsm_assistant.llm import (
    IntentModel,
    ModelError,
    ModelReply,
    ModelSpec,
    ModelUnavailable,
    ScriptedModel,
    ScriptExhausted,
    StaleRecording,
)

SECRET = "not-a-real-key-0123456789"
MODEL = "claude-test"

COUNT = "SELECT count(*) FROM transactions"
PER_CARD = "SELECT card_id, count(*) FROM transactions GROUP BY card_id"
ONLINE = PER_CARD.replace("GROUP", "WHERE transaction_type = 'Online Transaction' GROUP")
LARGE = "SELECT count(*) FROM transactions WHERE amount_usd_cents > 100000"
RATE = "SELECT 1.0 * sum(is_fraud) / count(*) FROM fraud_labels"
CATALOGUE = "SELECT name FROM sqlite_master"
NO_COLUMN = "SELECT nope FROM cards"

SUITE_FILES = {
    "01.toml": f"""
tier = "aggregates"
[[case]]
id = "count"
tests = "A row count"
tags = ["count"]
tuned = true
[[case.turn]]
question = "How many transactions are there?"
outcome = "answered"
sql = "{COUNT}"
""",
    "02.toml": f"""
tier = "follow_ups"
[[case]]
id = "follow"
tests = "A follow-up in the same conversation"
tags = ["follow-up"]
tuned = false
[[case.turn]]
question = "How many transactions per card?"
outcome = "answered"
sql = "{PER_CARD}"
[[case.turn]]
question = "Only the online ones."
outcome = "answered"
sql = "{ONLINE}"
""",
    "03.toml": f"""
tier = "traps"
[[case]]
id = "repaired"
tests = "A first reply that does not compile, then a good one"
tuned = false
[[case.turn]]
question = "How many transactions are over $1,000?"
outcome = "answered"
sql = "{LARGE}"
require = ["repaired"]
""",
    "04.toml": """
tier = "rules"
[[case]]
id = "rule"
tests = "A rule with its backtest"
tags = ["rule"]
tuned = false
[[case.turn]]
question = "Propose a rule for transactions over $1,000"
outcome = "rule_proposed"
clause = "amount_usd_cents > 100000"
flagged = 2
flagged_labelled = 1
true_positives = 1
false_positives = 0
require = ["caveat_small_sample"]
""",
    "05.toml": """
tier = "ambiguous"
[[case]]
id = "ask"
tests = "A vague word"
tuned = true
[[case.turn]]
question = "Show me the risky ones"
outcome = "clarification"
""",
    "06.toml": """
tier = "unanswerable"
[[case]]
id = "no"
tests = "A missing column"
tuned = false
[[case.turn]]
question = "What is each email address?"
outcome = "cannot_answer"
""",
    "07.toml": """
tier = "adversarial"
[[case]]
id = "refused"
tests = "A read of the catalogue, refused twice by the guard"
tuned = false
[[case.turn]]
question = "List the internal tables"
outcome = "refused"
""",
    "08.toml": f"""
tier = "fraud"
[[case]]
id = "wrong"
tests = "A fraction, which the scripted model returns as a percentage"
tags = ["rate"]
tuned = false
[[case.turn]]
question = "What is the overall fraud rate?"
outcome = "answered"
sql = "{RATE}"
""",
    "09.toml": f"""
tier = "joins"
[[case]]
id = "broken"
tests = "SQL that fails twice"
tuned = false
[[case.turn]]
question = "How many cards are there?"
outcome = "answered"
sql = "SELECT count(*) FROM cards"

[[case]]
id = "internal"
tests = "A fault nobody expected"
tuned = false
[[case.turn]]
question = "How many users are there?"
outcome = "answered"
sql = "SELECT count(*) FROM users"
""",
    "10.toml": """
tier = "scan"
[[case]]
id = "scan"
tests = "Needs the scan"
tuned = false
requires = "scan"
[[case.turn]]
question = "Where is fraud concentrated?"
outcome = "answered"
action = "profile_fraud"
scan_top = ["Rome"]
""",
}

type Script = list[ModelReply | ModelError]

ASK = call_reply("ask_clarification", question="Which ones?", suggestions=["over $1,000", "online"])
DECLINE = call_reply("decline", reason="There is no email column.")
SCRIPTS: dict[str, Script] = {
    "count": [sql_reply(COUNT)],
    "follow": [sql_reply(PER_CARD), sql_reply(ONLINE)],
    "repaired": [sql_reply("SELECT count(*) FROM transactions WHERE amount > 1000"), sql_reply(LARGE)],
    "rule": [rule_reply("amount_usd_cents > 100000")],
    "ask": [ASK],
    "no": [DECLINE],
    "refused": [sql_reply(CATALOGUE), sql_reply(CATALOGUE)],
    "wrong": [sql_reply(RATE.replace("1.0", "100.0"))],
    "broken": [sql_reply(NO_COLUMN), sql_reply(NO_COLUMN)],
    "internal": [ScriptExhausted("boom")],
}
PASSING = ("count", "follow", "repaired", "rule", "ask", "no", "refused")
RECORDINGS = 13  # one file per scripted reply; the fault of `internal` leaves none


class Fixture:
    """The small suite on disk, with the folders a run writes to."""

    def __init__(self, root: Path, db_path: Path) -> None:
        self.cases_directory = root / "cases"
        self.pins_file = root / "expected.json"
        self.recordings = root / "recordings" / MODEL
        self.results = root / "results" / MODEL
        self.settings = load_settings(
            {"FSM_DB_PATH": str(db_path), "ANTHROPIC_MODEL": MODEL, "ANTHROPIC_API_KEY": SECRET},
            env_file=None,
        )
        self.cases_directory.mkdir()
        for name, text in SUITE_FILES.items():
            (self.cases_directory / name).write_text(text)
        self.pin()

    def pin(self) -> None:
        pins = build_pins(
            load_cases(self.cases_directory),
            db_path=self.settings.db_path,
            row_cap=100,
            timeout_seconds=10.0,
        )
        write_pins(self.pins_file, pins)

    @property
    def suite(self) -> Suite:
        return load_suite(self.cases_directory, self.pins_file)

    def client(self, *case_ids: str, scripts: dict[str, Script] = SCRIPTS) -> StampingModel:
        """One model that answers the named cases (default: all) in suite order."""
        wanted = case_ids or tuple(SCRIPTS)
        steps = [step for case_id in SCRIPTS if case_id in wanted for step in scripts[case_id]]
        return StampingModel(nl2sql.model_spec(self.settings), steps)

    def record(self, *case_ids: str, scripts: dict[str, Script] = SCRIPTS) -> LiveRun:
        return record_run(
            select(self.suite.cases, Selection(case_ids=case_ids)),
            self.suite,
            self.settings,
            self.client(*case_ids, scripts=scripts),
            self.recordings,
            available=frozenset(),
            cases_directory=self.cases_directory,
            pins_file=self.pins_file,
        )

    def replay(self, *case_ids: str) -> Replay:
        return replay_run(
            select(self.suite.cases, Selection(case_ids=case_ids)),
            self.suite,
            self.settings,
            self.recordings,
            available=frozenset(),
            cases_directory=self.cases_directory,
            pins_file=self.pins_file,
        )

    def execute(self, options: Options, model: IntentModel | None = None) -> tuple[Finish, str]:
        def live_model(spec: ModelSpec, settings: Settings) -> IntentModel:
            if model is None:
                raise AssertionError("no live model may be built here")
            return model

        lines: list[str] = []
        finish = command.execute(
            options,
            self.settings,
            lines.append,
            live_model=live_model,
            cases_directory=self.cases_directory,
            pins_file=self.pins_file,
        )
        return finish, "\n".join(lines)

    def edit(self, name: str, old: str, new: str) -> None:
        path = self.cases_directory / name
        text = path.read_text()
        assert old in text
        path.write_text(text.replace(old, new))


@pytest.fixture
def fx(tmp_path: Path, fixture_db: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    """No test writes into the repository's recordings or results folders.

    The command is told that the scan is not built, so that this suite keeps
    a case that is skipped for a missing feature.
    """
    monkeypatch.setattr(command, "available_features", lambda settings: frozenset())
    monkeypatch.setattr(recorded, "RUN_RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(command, "RESULTS_DIR", tmp_path / "results")
    return Fixture(tmp_path, fixture_db)


def by_id(runs: tuple[CaseRun, ...]) -> dict[str, CaseRun]:
    return {run.case_id: run for run in runs}


def reasons(run: CaseRun) -> list[FailureReason]:
    return [score.reason for turn in run.turns for score in turn.scores if score.reason is not None]


def files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def strings_and_keys(value: JsonValue) -> tuple[list[str], list[str]]:
    """Every key and every string value of a JSON document."""
    keys: list[str] = []
    strings: list[str] = []
    if isinstance(value, dict):
        keys += value
        children: list[JsonValue] = list(value.values())
    elif isinstance(value, list):
        children = value
    else:
        return keys, [value] if isinstance(value, str) else []
    for child in children:
        more_keys, more_strings = strings_and_keys(child)
        keys += more_keys
        strings += more_strings
    return keys, strings


# --- The runner ---------------------------------------------------------------------------


def test_every_way_a_turn_can_end_is_scored(fx: Fixture) -> None:
    models: dict[str, ScriptedModel] = {}

    def model_for(case: Case) -> IntentModel:
        models[case.id] = ScriptedModel(SCRIPTS[case.id])
        return models[case.id]

    seen: list[str] = []
    runs = by_id(
        run_suite(
            fx.suite.cases,
            fx.suite,
            fx.settings,
            model_for,
            available=frozenset(),
            on_case=lambda run: seen.append(run.case_id),
        )
    )
    assert seen == [case.id for case in fx.suite.cases]
    assert {case_id for case_id, run in runs.items() if run.passed} == set(PASSING)
    outcomes = {case_id: [turn.outcome for turn in run.turns] for case_id, run in runs.items()}
    assert outcomes == {
        "count": ["answered"],
        "follow": ["answered", "answered"],
        "repaired": ["answered"],
        "rule": ["rule_proposed"],
        "ask": ["clarification"],
        "no": ["cannot_answer"],
        "refused": ["rejected"],
        "wrong": ["answered"],
        "broken": ["failed"],
        "internal": ["failed"],
        "scan": [],
    }
    # The tool chosen and the refusal codes come from the turn's result.
    assert [runs[name].turns[0].tool for name in ("rule", "ask", "no")] == [
        "propose_rule",
        "ask_clarification",
        "decline",
    ]
    assert runs["refused"].turns[0].refused == ("table_not_allowed", "table_not_allowed")
    assert runs["repaired"].turns[0].refused == ("sqlite_error",)
    assert runs["repaired"].turns[0].model_calls == 2
    assert runs["rule"].turns[0].where_clause == "amount_usd_cents > 100000"
    assert {score.scorer for score in runs["rule"].turns[0].scores} >= {"rule_arithmetic", "rule_match"}
    # Failures carry a reason, and a failed turn its error kind; the run went on after each.
    assert reasons(runs["wrong"]) == [FailureReason.SCALE_MISMATCH]
    assert FailureReason.TURN_FAILED in reasons(runs["broken"])
    assert runs["broken"].turns[0].error_kind == "sql_execution"
    assert runs["internal"].turns[0].error_kind == "internal"
    assert FailureReason.TURN_FAILED in reasons(runs["internal"])
    assert command.progress_line(runs["internal"]).startswith(
        "FAIL  internal  outcome: turn_failed (internal)"
    )
    # A case that needs the scan is skipped with a reason, and no model is built for it.
    assert runs["scan"].skipped == "needs scan, which is not built" and "scan" not in models
    assert command.progress_line(runs["scan"]) == "SKIP  scan  (needs scan, which is not built)"
    assert command.progress_line(runs["count"]) == "PASS  count"


def test_the_turns_of_a_case_share_a_conversation_and_cases_do_not(fx: Fixture) -> None:
    models: list[ScriptedModel] = []

    def model_for(case: Case) -> IntentModel:
        models.append(ScriptedModel(SCRIPTS[case.id]))
        return models[-1]

    cases = select(fx.suite.cases, Selection(case_ids=("count", "follow", "repaired")))
    run_suite(cases, fx.suite, fx.settings, model_for, available=frozenset())
    count, follow, repaired = (model.requests for model in models)
    assert [(request.turn, request.call) for request in follow] == [(1, 1), (2, 1)]
    assert "Earlier in this conversation" not in follow[0].user
    assert "Earlier in this conversation" in follow[1].user and PER_CARD in follow[1].user
    # A new case starts with nothing remembered, and a repair is the second call of its turn.
    assert "Earlier in this conversation" not in repaired[0].user
    assert [(request.turn, request.call) for request in repaired] == [(1, 1), (1, 2)]
    assert len(count) == 1


def test_the_scan_is_found_by_trying_it(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    assert available_features(fx.settings) == frozenset({"scan"})

    def broken(**arguments: object) -> explore.ScanResult:
        raise explore.ScanError("no")

    monkeypatch.setattr(nodes, "scan", broken)
    assert available_features(fx.settings) == frozenset()


def test_filters() -> None:
    cases = load_suite().cases

    def ids(**fields: object) -> list[str]:
        return [case.id for case in select(cases, Selection.model_validate(fields))]

    assert len(ids()) == 146 and Selection().is_everything
    assert len(ids(tiers=("scan", "top_n"))) == 12
    assert ids(case_ids=("fraud-count", "agg-date-range")) == ["agg-date-range", "fraud-count"]
    assert ids(tag="trap:velocity") == [
        "trap-velocity-window",
        "trap-velocity-same-minute-cards",
        "debatable-velocity-strict",
    ]
    assert len(ids(split="tuned")) + len(ids(split="held_out")) == 146
    assert ids(tiers=("ambiguous",), split="held_out", limit=2) == [
        "ambiguous-recent",
        "ambiguous-big-merchants",
    ]
    assert ids(tiers=("scan",), tag="count") == []
    with pytest.raises(UnknownCase, match="no-such-case"):
        ids(case_ids=("fraud-count", "no-such-case"))


def test_the_caveat_facts_are_the_caveat_codes_of_a_turn_result() -> None:
    facts = {fact for fact in get_args(Fact) if fact.startswith("caveat_")}
    assert facts == {f"caveat_{code}" for code in get_args(CaveatCode)}


def test_shipped_cases_pass_on_the_real_data_when_the_model_writes_the_reference(
    real_db: Path,
) -> None:
    # Through the real graph: the three required caveats, the velocity trap, the brief's
    # rule, and three of the five cases about the month-by-month evidence.
    settings = load_settings({"FSM_DB_PATH": str(real_db)}, env_file=None)
    suite = load_suite()
    wanted = (
        "fraud-overall-rate",
        "trap-velocity-window",
        "rule-brief-example",
        "rule-amount-threshold",
        "policy-flag-everything",
        "evidence-location-by-month",
        "evidence-unlabelled-month",
        "evidence-fading-rule",
    )

    def model_for(case: Case) -> IntentModel:
        turn = case.turns[0]
        if turn.sql is not None:
            return ScriptedModel([sql_reply(turn.sql)])
        assert turn.clause is not None
        return ScriptedModel([rule_reply(turn.clause)])

    cases = select(suite.cases, Selection(case_ids=wanted))
    runs = by_id(run_suite(cases, suite, settings, model_for, available=frozenset()))
    assert {case_id: reasons(run) for case_id, run in runs.items()} == {name: [] for name in wanted}
    assert all(run.passed for run in runs.values())
    scorers = [score.scorer for score in runs["evidence-fading-rule"].turns[0].scores]
    assert scorers == [
        "outcome", "behaviour", "guardrail", "rule_arithmetic", "rule_match", "weakest_month",
    ]  # fmt: skip


# --- Recording and replay -----------------------------------------------------------------


def test_a_run_is_recorded_and_its_replay_scores_the_same(fx: Fixture) -> None:
    live = fx.record()
    assert [run.case_id for run in live.runs] == [case.id for case in fx.suite.cases]
    assert live.not_replayable == ("internal",) and live.stopped_by is None and not live.restarted
    names = set(files(fx.recordings))
    assert len(names) == RECORDINGS + 1 and "run.json" in names
    assert {"follow.turn1.call1.json", "follow.turn2.call1.json"} <= names
    assert {"repaired.turn1.call1.json", "repaired.turn1.call2.json"} <= names
    record = read_record(fx.recordings)
    assert record == live.record and record.model == MODEL
    assert record.prompt_version == nl2sql.PROMPT_VERSION
    totals = record.totals
    assert (totals.cases, totals.passed, totals.failed, totals.skipped) == (11, 7, 3, 1)
    assert (totals.turns, totals.model_calls) == (11, 13)
    assert (totals.input_tokens, totals.output_tokens) == (13_000, 1_300)

    replay = fx.replay()
    assert replay.not_recorded == ("internal",)
    assert replay.runs == tuple(run for run in live.runs if run.case_id != "internal")
    assert replay.differences == () and not replay.suite_changed and replay.changed_cases == ()
    whole = fx.replay(*PASSING, "wrong", "broken", "scan")
    assert whole.reproduced and whole.complete
    assert files(fx.recordings).keys() == names  # a replay writes nothing here


def test_a_case_the_run_skipped_is_not_recorded_once_it_can_run(fx: Fixture) -> None:
    # The run was made before the scan existed: its scan case has an entry and no reply.
    fx.record()
    replay = replay_run(
        select(fx.suite.cases, Selection(case_ids=(*PASSING, "scan"))),
        fx.suite,
        fx.settings,
        fx.recordings,
        available=frozenset({"scan"}),
        cases_directory=fx.cases_directory,
        pins_file=fx.pins_file,
    )
    # What was replayed is reproduced; the selection was not replayed whole.
    assert replay.not_recorded == ("scan",) and replay.reproduced and not replay.complete
    assert [run.case_id for run in replay.runs] == list(PASSING) and replay.differences == ()
    text = command.render_comparison(replay)
    assert "No recording for 1 selected case(s), which were not run: scan." in text
    assert "All 7 replayed case(s) scored exactly as recorded in run.json." in text


def test_the_run_record_holds_no_timestamp_no_path_and_no_key(fx: Fixture, tmp_path: Path) -> None:
    fx.record()
    text = (fx.recordings / "run.json").read_text()
    document: JsonValue = json.loads(text)
    assert isinstance(document, dict)
    assert set(document) == {
        "format_version",
        "model",
        "effort",
        "max_tokens",
        "tool_mode",
        "prompt_version",
        "library_versions",
        "schema_context_sha256",
        "row_cap",
        "query_timeout_seconds",
        "max_result_bytes",
        "system_text_sha256",
        "suite_sha256",
        "cases",
        "totals",
    }
    keys, strings = strings_and_keys(document)
    for key in keys:
        # The one key with "time" in it is a limit in seconds, not a moment.
        if key != "query_timeout_seconds":
            assert not re.search(r"date|time|stamp|created|_at$|path|directory|key", key)
    for value in strings:
        assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}", value)
        assert not value.startswith(("/", "~")) and "\\" not in value
    assert SECRET not in text and str(tmp_path) not in text and str(Path.home()) not in text


def test_replay_is_stale_when_the_prompt_version_changed(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.record()
    monkeypatch.setattr(nl2sql, "PROMPT_VERSION", "turn-next")
    with pytest.raises(StaleRecording, match="prompt_version turn-.* now uses turn-next"):
        fx.replay()


def test_replay_is_stale_when_the_prompt_text_changed_without_a_new_version(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.record()
    monkeypatch.setattr(prompt, "_GLOSSARY", prompt._GLOSSARY + "\n- One more line.")
    with pytest.raises(StaleRecording, match="recorded with system_text_sha256 .* now uses") as raised:
        fx.replay()
    assert raised.value.kind == "stale" and raised.value.path.name == "run.json"


def test_replay_is_stale_when_a_question_changed(fx: Fixture) -> None:
    fx.record()
    fx.edit("05.toml", "Show me the risky ones", "Show me the dodgy ones")
    with pytest.raises(StaleRecording, match="the case ask changed since it was recorded"):
        fx.replay()
    # The cases before it are untouched and still replay.
    assert fx.replay("count", "rule").reproduced


def test_replay_is_stale_when_a_recording_is_missing_or_there_is_no_run(fx: Fixture) -> None:
    with pytest.raises(StaleRecording, match="no evaluation run was recorded") as raised:
        fx.replay()
    assert raised.value.kind == "missing"
    fx.record()
    (fx.recordings / "repaired.turn1.call2.json").unlink()
    with pytest.raises(StaleRecording, match="no reply was recorded for this call"):
        fx.replay("repaired")
    (fx.recordings / "run.json").write_text("{}")
    with pytest.raises(StaleRecording, match="not a run record"):
        fx.replay()


def test_replay_reports_a_changed_suite_and_the_cases_with_no_recording(fx: Fixture) -> None:
    fx.record()
    (fx.cases_directory / "11.toml").write_text(
        SUITE_FILES["06.toml"].replace('id = "no"', 'id = "added"').replace("email", "postal")
    )
    # A changed expectation, with the question as it was: the recorded reply still answers it.
    fx.edit("08.toml", RATE, RATE.replace("1.0", "100.0"))
    fx.pin()
    replay = fx.replay()
    assert replay.suite_changed
    assert replay.changed_cases == ("wrong",)
    assert replay.not_recorded == ("internal", "added")
    assert [item.case_id for item in replay.differences] == ["wrong"]
    assert "recorded fail (scale_mismatch), replayed pass" in replay.differences[0].detail
    assert not replay.reproduced
    text = command.render_comparison(replay)
    assert "the suite digest differs" in text and "internal, added" in text
    assert "1 case(s) did not score as recorded" in text


def test_a_run_of_some_cases_leaves_the_others_as_they_were(fx: Fixture) -> None:
    fx.record()
    before = files(fx.recordings)
    # `repaired` now needs no repair, and `wrong` now answers with a fraction.
    scripts: dict[str, Script] = {**SCRIPTS, "repaired": [sql_reply(LARGE)], "wrong": [sql_reply(RATE)]}
    partial = fx.record("repaired", "wrong", scripts=scripts)
    assert [run.case_id for run in partial.runs] == ["repaired", "wrong"]
    assert all(run.passed for run in partial.runs) is False  # `repaired` required a repair
    after = files(fx.recordings)
    rewritten = {"run.json", "repaired.turn1.call1.json", "wrong.turn1.call1.json"}
    # The second call of the earlier run of `repaired` is gone: this run did not make it.
    assert before.keys() - after.keys() == {"repaired.turn1.call2.json"}
    assert {name for name in after if after[name] != before[name]} == rewritten
    record = read_record(fx.recordings)
    assert [item.run.case_id for item in record.cases] == [case.id for case in fx.suite.cases]
    assert (record.totals.cases, record.totals.passed, record.totals.failed) == (11, 7, 3)
    wrong = record.find("wrong")
    assert wrong is not None and wrong.run.passed
    replay = fx.replay()
    assert replay.not_recorded == ("internal",) and replay.differences == ()


def test_a_run_under_other_settings_starts_the_record_again(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.record()
    monkeypatch.setattr(nl2sql, "PROMPT_VERSION", "turn-next")
    again = fx.record("count")
    assert again.restarted
    assert [item.run.case_id for item in again.record.cases] == ["count"]
    assert fx.replay("count").reproduced
    assert fx.replay("count", "rule").not_recorded == ("rule",)
    # The replies recorded under the earlier settings are gone: nothing stale is left to mix in.
    assert sorted(files(fx.recordings)) == ["count.turn1.call1.json", "run.json"]


def test_a_restart_that_records_nothing_leaves_the_earlier_run_as_it_was(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.record()
    before = files(fx.recordings)
    monkeypatch.setattr(nl2sql, "PROMPT_VERSION", "turn-next")
    refused: dict[str, Script] = {**SCRIPTS, "count": [ModelUnavailable("authentication", "invalid x-api-key")]}
    stopped = fx.record("count", scripts=refused)
    assert stopped.stopped_by == "authentication" and stopped.runs == ()
    assert files(fx.recordings) == before


def test_a_request_with_no_reply_is_scored_and_marked_not_replayable(fx: Fixture) -> None:
    scripts: dict[str, Script] = {**SCRIPTS, "count": [ModelUnavailable("rate_limit", "slow down")]}
    live = fx.record("count", "ask", scripts=scripts)
    assert live.not_replayable == ("count",) and live.stopped_by is None
    assert live.runs[0].turns[0].error_kind == "model_unavailable" and live.runs[1].passed
    replay = fx.replay("count", "ask")
    assert replay.not_recorded == ("count",) and [run.case_id for run in replay.runs] == ["ask"]


def test_a_run_stops_at_a_failure_every_request_would_repeat(fx: Fixture) -> None:
    fx.record("count")
    scripts: dict[str, Script] = {
        **SCRIPTS,
        "follow": [ModelUnavailable("authentication", "invalid x-api-key")],
    }
    live = fx.record("count", "follow", "rule", scripts=scripts)
    assert live.stopped_by == "authentication"
    assert [run.case_id for run in live.runs] == ["count"]
    assert [item.run.case_id for item in read_record(fx.recordings).cases] == ["count"]


# --- The command --------------------------------------------------------------------------


def test_list_prints_the_selection_and_runs_nothing(fx: Fixture) -> None:
    finish, output = fx.execute(Options(list_only=True, selection=Selection(tiers=("joins", "scan"))))
    assert finish == "passed"
    assert "3 of 11 cases selected:" in output and "needs scan" in output
    assert re.search(r"^joins +2 +0 +2$", output, re.MULTILINE)
    assert re.search(r"^total +3 +0 +3$", output, re.MULTILINE)
    assert not fx.recordings.exists() and not fx.results.exists()


def test_nothing_is_sent_or_written_without_the_flag(fx: Fixture) -> None:
    finish, output = fx.execute(Options())
    assert finish == "passed"
    assert "eval: 10 cases (11 turns) to claude-test in tools mode, the whole suite; 1 skipped" in output
    assert "COST WARNING" in output and "Upper bound: 22 requests" in output
    assert "ANTHROPIC_API_KEY is set." in output and "Nothing was sent." in output
    assert SECRET not in output
    assert not fx.recordings.exists() and not fx.results.exists()
    filtered = fx.execute(Options(selection=Selection(case_ids=("follow",))))[1]
    assert "eval: 1 cases (2 turns)" in filtered and "a filtered run; 0 skipped" in filtered


def test_a_live_run_prints_progress_tables_and_writes_the_reports(fx: Fixture) -> None:
    finish, output = fx.execute(Options(spend=True), fx.client())
    assert finish == "failed"
    lines = output.splitlines()
    assert lines[5:16] == [
        "PASS  count",
        "PASS  follow",
        "PASS  repaired  (after the guard refused a reply)",
        "PASS  rule",
        "PASS  ask",
        "PASS  no",
        "PASS  refused  (after the guard refused a reply)",
        "FAIL  wrong  result: scale_mismatch",
        "FAIL  broken  outcome: turn_failed (sql_execution); result: no_result",
        "FAIL  internal  outcome: turn_failed (internal); result: no_result",
        "SKIP  scan  (needs scan, which is not built)",
    ]
    assert "7 of 10 cases passed (3 failed, 1 skipped). Read it by tier:" in output
    assert "  Correct, with extra columns: 0 (counted in the 7 passed)" in output
    assert "  Failed, known limitation: 0 (counted in the 3 failed)" in output
    assert "  Passed after the guard refused a reply: 2 (counted in the 7 passed)" in output
    assert re.search(
        r"^tier +cases +passed +extra cols +guard stop +failed +known lim\. +skipped$",
        output,
        re.MULTILINE,
    )
    assert re.search(r"^joins +2 +0 +0 +0 +2 +0 +0$", output, re.MULTILINE)
    assert re.search(r"^adversarial +1 +1 +0 +1 +0 +0 +0$", output, re.MULTILINE)
    assert re.search(r"^tuned +2 +2 +0 +0 +0 +0 +0$", output, re.MULTILINE)
    assert re.search(r"^held_out +9 +5 +0 +2 +3 +0 +1$", output, re.MULTILINE)
    assert "  Repairs (turns that needed a second model request): 3 of 11 turns; 13 model requests." in output
    assert "  Refused replies by code: sqlite_error 3, table_not_allowed 2." in output
    assert "  Write attempts stopped by the guard: 0." in output
    assert "  Seconds per turn: p50 " in output and "  Tokens: 13,000 in and 1,300 out" in output
    assert "no price is written in the code for this model" in output
    assert "  2  outcome: turn_failed" in output and "  1  result: scale_mismatch" in output
    assert "Totals: 11 turns, 13 model requests, 13,000 input tokens, 1,300 output tokens" in output
    assert "run.json now holds 11 case(s), 11 of them from this run." in output
    assert "A request got no reply in: internal." in output
    assert "%" not in output and SECRET not in output
    assert sorted(path.name for path in fx.results.iterdir()) == ["report.json", "report.md"]
    report = (fx.results / "report.md").read_text()
    assert "**7 of 10 cases passed**" in report and "live model calls" in report
    assert "## Repairs, refusals and cost" in report
    assert "| adversarial | 1 | 1 | 0 | 1 | 0 | 0 | 0 |" in report


def test_a_run_shows_extra_columns_and_known_limitations_on_their_own_lines(fx: Fixture) -> None:
    fx.edit("08.toml", "tuned = false", 'tuned = false\nknown_limitation = "Rates come back as percentages"')
    fx.edit("05.toml", "tuned = true", 'tuned = true\nknown_limitation = "No longer true"')
    scripts: dict[str, Script] = {**SCRIPTS, "count": [sql_reply(COUNT.replace("count(*)", "count(*), min(id)"))]}
    finish, output = fx.execute(Options(spend=True), fx.client(scripts=scripts))
    assert finish == "failed"
    assert "PASS  count  (with extra columns)" in output
    assert "FAIL  wrong  (known limitation)  result: scale_mismatch" in output
    assert "PASS  ask  (marked as a known limitation: the note is out of date)" in output
    assert "7 of 10 cases passed (3 failed, 1 skipped). Read it by tier:" in output
    assert "  Correct, with extra columns: 1 (counted in the 7 passed)" in output
    assert "  Failed, known limitation: 1 (counted in the 3 failed)" in output
    assert "  Marked as a known limitation but passed, so the note is out of date: ask" in output
    assert re.search(r"^aggregates +1 +1 +1 +0 +0 +0 +0$", output, re.MULTILINE)
    assert re.search(r"^fraud +1 +0 +0 +0 +1 +1 +0$", output, re.MULTILINE)
    report = (fx.results / "report.md").read_text()
    assert "- Correct, with extra columns: 1 (counted in the 7 passed)" in report
    assert "| wrong | failed, as noted | Rates come back as percentages |" in report
    recorded_count = read_record(fx.recordings).find("count")
    assert recorded_count is not None
    assert [score.note for score in recorded_count.run.turns[0].scores if score.note] == ["extra_columns"]
    # The note is part of what a replay must reproduce.
    assert fx.replay("count", "wrong", "ask").reproduced


def test_a_database_error_while_scoring_fails_that_case_and_the_run_goes_on(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timed_out(clause: str, *, db_path: Path, timeout_seconds: float) -> frozenset[int]:
        raise db.QueryTimeout("the statement ran too long")

    monkeypatch.setattr(evidence, "flagged_ids", timed_out)
    finish, output = fx.execute(Options(spend=True), fx.client())
    assert finish == "failed"
    assert "FAIL  rule  scoring: scoring_error" in output
    # The cases after it ran and scored as they always do.
    assert "PASS  ask" in output and "PASS  refused" in output
    assert "6 of 10 cases passed (4 failed, 1 skipped)." in output
    assert "  1  scoring: scoring_error" in output
    record = read_record(fx.recordings)
    rule = record.find("rule")
    assert rule is not None and rule.replayable
    assert reasons(rule.run) == [FailureReason.SCORING_ERROR]
    assert len(record.cases) == 11


def test_a_run_where_every_case_passes_ends_as_passed(fx: Fixture) -> None:
    options = Options(spend=True, selection=Selection(case_ids=(*PASSING, "scan")))
    finish, output = fx.execute(options, fx.client(*PASSING))
    assert finish == "passed" and "No score failed." in output


def test_a_filtered_run_keeps_the_report_of_the_whole_record(fx: Fixture) -> None:
    fx.execute(Options(spend=True), fx.client())
    options = Options(spend=True, selection=Selection(case_ids=("ask",)))
    finish, output = fx.execute(options, fx.client("ask"))
    assert finish == "passed"
    assert "1 of 1 cases passed" in output
    assert "run.json now holds 11 case(s), 1 of them from this run." in output
    assert "**7 of 10 cases passed**" in (fx.results / "report.md").read_text()


def test_replay_runs_offline_and_says_whether_the_record_is_reproduced(fx: Fixture) -> None:
    assert fx.execute(Options(replay=True))[0] == "cannot_start"
    fx.execute(Options(spend=True), fx.client())
    replayable = Selection(case_ids=(*PASSING, "wrong", "broken", "scan"))
    # No live model is handed over: building one would raise.
    finish, output = fx.execute(Options(replay=True, selection=replayable))
    # Reproduced, though three of the recorded cases are failures.
    assert finish == "reproduced"
    assert "Nothing is sent." in output
    assert "All 10 replayed case(s) scored exactly as recorded in run.json." in output
    assert "FAIL  wrong  result: scale_mismatch" in output
    assert "13,000 input tokens" in output and "Tokens and seconds are the recorded ones." in output
    assert sorted(path.name for path in fx.results.iterdir()) == [
        "replay.json",
        "replay.md",
        "report.json",
        "report.md",
    ]
    assert "recorded replies, replayed" in (fx.results / "replay.md").read_text()

    assert fx.execute(Options(replay=True, selection=Selection(case_ids=PASSING)))[0] == "reproduced"
    # A case with no recording is named, and is not a case that scored differently.
    # It has a finish of its own, so that "reproduced" means the whole selection was replayed.
    finish, output = fx.execute(Options(replay=True))
    assert finish == "partly_replayed"
    assert "No recording for 1 selected case(s), which were not run: internal." in output
    assert "All 10 replayed case(s) scored exactly as recorded in run.json." in output
    # Nothing replayed is nothing reproduced.
    nothing = fx.execute(Options(replay=True, selection=Selection(case_ids=("internal",))))
    assert nothing[0] == "not_reproduced"
    # A case that scores differently from its record.
    fx.edit("08.toml", RATE, RATE.replace("1.0", "100.0"))
    fx.pin()
    finish, output = fx.execute(Options(replay=True))
    assert finish == "not_reproduced"
    assert "wrong: turn 1, result: recorded fail (scale_mismatch), replayed pass" in output


def test_a_live_run_is_traced_by_case_and_a_replay_never_is(fx: Fixture) -> None:
    sink = RecordingSink()
    lines: list[str] = []

    def run(options: Options, model: IntentModel | None, tracer: Tracer | None) -> Finish:
        def live_model(spec: ModelSpec, settings: Settings) -> IntentModel:
            assert model is not None, "no live model may be built here"
            return model

        def tracer_for(settings: Settings) -> Tracer | None:
            assert tracer is not None, "no tracer may be built here"
            return tracer

        return command.execute(
            options, fx.settings, lines.append, live_model=live_model, tracer_for=tracer_for,
            cases_directory=fx.cases_directory, pins_file=fx.pins_file,
        )  # fmt: skip

    selection = Selection(case_ids=("count", "follow"))
    tracer = Tracer(sink, Destination(project="evals"))
    assert run(Options(spend=True, selection=selection), fx.client("count", "follow"), tracer)
    assert "Hosted tracing: on, to LangSmith project 'evals'" in "\n".join(lines)
    assert sink.closed, "pending traces are flushed when the run ends"
    assert [batch[0].name for batch in sink.batches] == [
        "eval count turn 1", "eval follow turn 1", "eval follow turn 2",
    ]  # fmt: skip
    assert all("case:follow" in run.tags for run in sink.batches[1])
    assert all(run.metadata["case_id"] == "follow" for run in sink.batches[2])
    assert sink.batches[1][0].metadata["session_id"] == sink.batches[2][0].metadata["session_id"]
    assert SECRET not in sink.text()

    del lines[:]
    assert run(Options(replay=True, selection=selection), None, None) == "reproduced"
    assert len(sink.batches) == 3 and "Hosted tracing" not in "\n".join(lines)
    assert run(Options(selection=selection), None, None) == "passed", "a plan builds no tracer"
    assert run(Options(list_only=True), None, None) == "passed"


def test_a_stale_replay_cannot_start(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    fx.execute(Options(spend=True), fx.client())
    monkeypatch.setattr(nl2sql, "PROMPT_VERSION", "turn-next")
    finish, output = fx.execute(Options(replay=True))
    assert finish == "cannot_start"
    assert "Cannot replay: stale recording: the run was recorded with prompt_version" in output
    assert "PASS" not in output


def test_what_cannot_start(fx: Fixture) -> None:
    def started(options: Options, model: IntentModel | None = None) -> tuple[Finish, str]:
        return fx.execute(options, model)

    assert started(Options(selection=Selection(case_ids=("nope",)))) == (
        "cannot_start",
        "Cannot start: no case with id: nope",
    )
    finish, output = started(Options(selection=Selection(tiers=("scan",), tag="count")))
    assert (finish, output) == ("cannot_start", "Cannot start: no case matches the filters.")
    assert started(Options(model="../elsewhere"))[0] == "cannot_start"
    finish, output = started(Options(spend=True, selection=Selection(case_ids=("count",))),
                             fx.client("count", scripts={**SCRIPTS, "count": [
                                 ModelUnavailable("authentication", "invalid x-api-key")]}))  # fmt: skip
    assert finish == "cannot_start" and "Stopped after 0 case(s): authentication." in output
    (fx.cases_directory / "01.toml").write_text("tier = 'nope'")
    finish, output = started(Options(list_only=True))
    assert finish == "cannot_start" and "01.toml: unknown tier" in output


# --- cli ----------------------------------------------------------------------------------


@pytest.fixture
def isolated(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real suite, the fixture database, no `.env`, and no live model."""

    def forbidden(spec: ModelSpec, settings: Settings) -> IntentModel:
        raise AssertionError("no live model may be built here")

    monkeypatch.setattr(cli, "load_settings", lambda: fx.settings)
    monkeypatch.setattr(cli, "_live_model", forbidden)


def test_cli_sends_nothing_without_yes_spend(
    isolated: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["eval"]) == 0
    output = capsys.readouterr().out
    assert "eval: 141 cases (153 turns) to claude-test" in output and "5 skipped" in output
    assert "Upper bound: 306 requests" in output and "Nothing was sent." in output
    assert cli.main(["eval", "--model", "claude-other", "--tier", "rules", "--limit", "3"]) == 0
    assert "eval: 3 cases (3 turns) to claude-other" in capsys.readouterr().out


def test_cli_list_and_filters(isolated: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["eval", "--list"]) == 0
    output = capsys.readouterr().out
    assert "146 of 146 cases selected:" in output
    assert re.search(r"^total +146 +27 +119$", output, re.MULTILINE)
    assert cli.main(["eval", "--list", "--tier", "scan", "--tier", "top_n", "--held-out"]) == 0
    assert "9 of 146 cases selected:" in capsys.readouterr().out
    assert cli.main(["eval", "--list", "--tuned", "--tag", "vague-term"]) == 0
    assert "tuned" in capsys.readouterr().out
    assert cli.main(["eval", "--list", "--case", "fraud-count", "--case", "no-such"]) == 2
    assert "no case with id: no-such" in capsys.readouterr().out


def test_cli_replay_with_nothing_recorded_exits_with_2(
    isolated: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["eval", "--replay"]) == 2
    output = capsys.readouterr().out
    assert "Cannot replay: stale recording: no evaluation run was recorded for this model" in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--yes-spend", "--replay"],
        ["--yes-spend", "--list"],
        ["--tuned", "--held-out"],
        ["--tier", "nope"],
        ["--limit", "0"],
        ["--repeat", "0"],
        ["--repeat", "3", "--list"],
        ["--rescore", "--replay"],
        ["--rescore", "--yes-spend"],
        ["--rescore", "--tier", "rules"],
        ["--rescore", "--repeat", "2"],
    ],
)
def test_cli_refuses_arguments_that_do_not_go_together(isolated: None, arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["eval", *arguments])
    assert raised.value.code == 2


@pytest.mark.parametrize(
    ("finish", "code"),
    [
        ("passed", 0), ("reproduced", 0), ("failed", 1), ("cannot_start", 2),
        ("not_reproduced", 3), ("partly_replayed", 5),
    ],  # fmt: skip
)
def test_cli_exit_codes(
    isolated: None, monkeypatch: pytest.MonkeyPatch, finish: Finish, code: int
) -> None:
    def execute(options: Options, settings: Settings, emit: object, **rest: object) -> Finish:
        return finish

    monkeypatch.setattr(command, "execute", execute)
    assert cli.main(["eval", "--yes-spend"]) == code


# --- Hardening: write attempts, wrong tools, guard stops and what a detail may hold --------

FENCED_DELETE = "```sql\nDELETE FROM transactions\n```"
CLAUSE_ESCAPE = "1=1); DELETE FROM transactions; --"


def run_one(fx: Fixture, case_id: str, *replies: ModelReply, **options: object) -> CaseRun:
    """One case of the small suite through the real graph, with its turns' options changed."""
    case = fx.suite.case(case_id)
    turns = tuple(turn.model_copy(update=options) for turn in case.turns)
    return run_case(
        case.model_copy(update={"turns": turns}), fx.suite, fx.settings, ScriptedModel(list(replies))
    )


@pytest.mark.parametrize(
    ("write", "code"),
    [
        (sql_reply("DELETE FROM transactions"), "not_read_only"),
        (sql_reply(FENCED_DELETE), "code_fence"),
        (sql_reply("EXPLAIN UPDATE transactions SET amount_usd_cents = 0"), "explain"),
        (sql_reply("DELETE FROM transactions WHERE errors = 'x"), "unterminated_quote"),
        (sql_reply("DELETE FROM nosuchtable"), "sqlite_error"),
        (sql_reply("TRUNCATE TABLE fraud_labels"), "sqlite_error"),
        (rule_reply(CLAUSE_ESCAPE), "clause_not_contained"),
    ],
)
def test_a_write_the_guard_stopped_is_counted_whatever_code_refused_it(
    fx: Fixture, write: ModelReply, code: str
) -> None:
    twice = run_one(fx, "refused", write, write)
    assert twice.turns[0].refused == (code, code) and twice.turns[0].write_attempts == 2
    assert FailureReason.WRITE_ATTEMPTED in reasons(twice)
    # Declining afterwards does not undo it: the model wrote the write.
    then_declined = run_one(fx, "refused", write, DECLINE)
    assert then_declined.turns[0].outcome == "cannot_answer"
    assert reasons(then_declined) == [FailureReason.WRITE_ATTEMPTED]
    # The same replies where the case accepts a refusal by either route: a pass, marked.
    either = run_one(fx, "refused", write, DECLINE, accept_stopped_write=True)
    assert either.passed and either.turns[0].write_attempts == 1
    assert [score.note for score in either.turns[0].scores if score.note] == [Note.STOPPED_BY_GUARD]
    assert command.progress_line(either) == "PASS  refused  (after the guard refused a reply)"


def test_a_refused_read_is_no_write_attempt_and_still_marks_the_pass(fx: Fixture) -> None:
    run = run_one(fx, "refused", sql_reply(CATALOGUE), sql_reply(CATALOGUE))
    assert run.passed and run.turns[0].write_attempts == 0
    guardrail = next(score for score in run.turns[0].scores if score.scorer == "guardrail")
    assert guardrail.note is Note.STOPPED_BY_GUARD
    assert "table_not_allowed, table_not_allowed" in guardrail.detail
    clean = run_one(fx, "refused", DECLINE)
    assert clean.passed and not [score.note for score in clean.turns[0].scores if score.note]
    assert command.progress_line(clean) == "PASS  refused"


@pytest.mark.parametrize("case_id", ["no", "refused"])
def test_a_tool_that_is_not_decline_does_not_pass_as_a_decline(fx: Fixture, case_id: str) -> None:
    # The scan is built: chosen where a decline was due, it runs and the turn is an answer.
    run = run_one(fx, case_id, call_reply("profile_fraud"))
    turn = run.turns[0]
    assert (turn.tool, turn.outcome) == ("profile_fraud", "answered") and not run.passed
    # It also ran something, which both cases forbid.
    assert reasons(run) == [FailureReason.ANSWERED_INSTEAD, FailureReason.FORBIDDEN_BEHAVIOUR]
    # A clarification where a decline was due is no decline either.
    asked = run_one(fx, case_id, ASK)
    assert asked.turns[0].outcome == "clarification"
    assert reasons(asked) == [FailureReason.WRONG_OUTCOME]
    assert run_one(fx, case_id, DECLINE).passed


def test_no_value_of_a_result_reaches_the_record_or_the_reports(fx: Fixture) -> None:
    # The values are computed, so they are in the rows and nowhere in the SQL.
    leaky = "SELECT 'ZZ-' || 'PLANTED', 4242 * 1000 + 424, address FROM users ORDER BY id LIMIT 1"
    scripts: dict[str, Script] = {**SCRIPTS, "count": [sql_reply(leaky)], "wrong": [sql_reply(leaky)]}
    options = Options(spend=True, selection=Selection(case_ids=("count", "wrong")))
    finish, output = fx.execute(options, fx.client("count", "wrong", scripts=scripts))
    assert finish == "failed"
    assert "FAIL  count  result: value_mismatch" in output
    written = [fx.recordings / "run.json", fx.results / "report.md", fx.results / "report.json"]
    for path in written:
        text = path.read_text()
        assert "value_mismatch" in text
        for value in ("ZZ-PLANTED", "4242424", "Elm Street"):
            assert value not in text, path.name
    assert "ZZ-PLANTED" not in output and "Elm Street" not in output
    detail = read_record(fx.recordings).cases[0].run.turns[0].scores[-1].detail
    assert detail == (
        "row 1, compared column 1: the reference has 6, the result has a value of another "
        "kind: text"
    )


# The same, for the two kinds of turn whose result is not a query's rows: a scan and a rule
# with its evidence by month. `MAIN` is the scan tests' database; on it the clause flags 449
# transactions in March, April and May, and the scan's top segment is Rome, Italy.
OVER_TEN_DOLLARS = "amount_usd_cents > 1000"
EVIDENCE_SUITE = {
    "01.toml": f"""
tier = "rules"
[[case]]
id = "months"
tests = "A rule with its evidence by month"
tuned = false
[[case.turn]]
question = "Propose a rule for transactions over $10"
outcome = "rule_proposed"
clause = "{OVER_TEN_DOLLARS}"
flagged = 449
flagged_labelled = 399
true_positives = 169
false_positives = 230
weakest_month = "2019-05"
""",
    "02.toml": """
tier = "scan"
[[case]]
id = "top"
tests = "The scan and its top segment"
tuned = false
requires = "scan"
[[case.turn]]
question = "Where is fraud concentrated?"
outcome = "answered"
action = "profile_fraud"
scan_top = ["merchant city and state", "{top}"]
""",
}
SCAN_REPLY = call_reply("profile_fraud")


def evidence_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, top: str) -> Fixture:
    """A suite of one rule case and one scan case on the scan tests' database; the scan is built."""
    monkeypatch.setattr(recorded, "RUN_RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(command, "RESULTS_DIR", tmp_path / "results")
    fixture = Fixture(tmp_path, build(tmp_path / "scan.db", MAIN))
    for path in fixture.cases_directory.iterdir():
        path.unlink()
    for name, text in EVIDENCE_SUITE.items():
        (fixture.cases_directory / name).write_text(text.replace("{top}", top))
    fixture.pin()
    return fixture


def test_no_value_of_a_scan_or_of_a_rule_by_month_reaches_the_record_or_the_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rule card carries figures and a month that nothing else in the turn could produce,
    # and the case expects a top segment the scan does not find: both turns fail, with details.
    fx = evidence_fixture(tmp_path, monkeypatch, top="Atlantis")
    monkeypatch.setattr(nodes, "rule_evidence", marked_evidence)
    steps: Script = [rule_reply(OVER_TEN_DOLLARS), SCAN_REPLY]
    model = StampingModel(nl2sql.model_spec(fx.settings), steps)
    finish, output = fx.execute(Options(spend=True), model)
    assert finish == "failed"
    assert "FAIL  months  rule_arithmetic: metric_mismatch; weakest_month: wrong_weakest_month" in output
    assert "FAIL  top  scan: wrong_top_segment" in output

    record = read_record(fx.recordings)
    details = {
        score.scorer: score.detail
        for item in record.cases
        for score in item.run.turns[0].scores
        if not score.passed
    }
    # The recomputed figure and the expected month and segment are ours, and are quoted.
    assert "flagged: recomputed 449, reported a larger value" in details["rule_arithmetic"]
    assert "months: recomputed 2019-01, 2019-02, 2019-03, 2019-04, 2019-05; the turn reported 3 month(s)" in (
        details["rule_arithmetic"]
    )
    assert details["weakest_month"] == "expected the weakest month 2019-05, the rule card names another month"
    assert details["scan"].endswith("does not name Atlantis")

    card_figures = (
        "7341106", "6120754", "5831329", "4409183", "3907661", "211457", "9203517", "8101943",
        "307883", "20953806", "18130358", "6350669", "27548208", "2019-09",
    )  # fmt: skip
    segments = ("Rome", "Italy", "Sacramento", "Grocery", "Money Transfer", "Chip Transaction")
    scan_figures = ("3.17", "0.79", "79.4%")
    written = [fx.recordings / "run.json", fx.results / "report.md", fx.results / "report.json"]
    for path in written:
        text = path.read_text()
        assert "wrong_top_segment" in text and "wrong_weakest_month" in text
        for value in (*card_figures, *segments, *scan_figures):
            assert value not in text, f"{value!r} is in {path.name}"
    for value in (*card_figures, *segments):
        assert value not in output


def test_the_guard_s_part_in_a_scan_turn_and_a_rule_turn_is_marked_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = evidence_fixture(tmp_path, monkeypatch, top="Rome, Italy")
    today = "SELECT count(*) FROM transactions WHERE date > date('now')"
    steps: Script = [
        rule_reply(f"{OVER_TEN_DOLLARS} AND date > date('now')"), rule_reply(OVER_TEN_DOLLARS),
        sql_reply(today), SCAN_REPLY,
    ]  # fmt: skip
    finish, output = fx.execute(Options(spend=True), StampingModel(nl2sql.model_spec(fx.settings), steps))
    assert finish == "passed"
    assert "PASS  months  (after the guard refused a reply)" in output
    assert "PASS  top  (after the guard refused a reply)" in output
    assert "Refused replies by code: relative_to_today 2." in output
    assert "Write attempts stopped by the guard: 0." in output
    record = read_record(fx.recordings)
    for item in record.cases:
        turn = item.run.turns[0]
        assert turn.refused == ("relative_to_today",) and turn.write_attempts == 0
        assert [score.note for score in turn.scores if score.note] == [Note.STOPPED_BY_GUARD]
    rule, scan = (item.run.turns[0] for item in record.cases)
    assert [score.scorer for score in rule.scores] == [
        "outcome", "behaviour", "guardrail", "rule_arithmetic", "rule_match", "weakest_month",
    ]  # fmt: skip
    assert [score.scorer for score in scan.scores] == ["outcome", "behaviour", "guardrail", "scan"]
    report = (fx.results / "report.md").read_text()
    assert "Passed after the guard refused a reply: 2" in report

    # A write the guard stopped on the way to a scan or a rule is counted, and fails the turn.
    delete = sql_reply("DELETE FROM transactions")
    escape = rule_reply("1=1); DELETE FROM fraud_labels; --")
    steps = [escape, rule_reply(OVER_TEN_DOLLARS), delete, SCAN_REPLY]
    finish, output = fx.execute(Options(spend=True), StampingModel(nl2sql.model_spec(fx.settings), steps))
    assert finish == "failed"
    assert "FAIL  months  guardrail: write_attempted" in output
    assert "FAIL  top  guardrail: write_attempted" in output
    assert "Write attempts stopped by the guard: 2." in output
    turns = [item.run.turns[0] for item in read_record(fx.recordings).cases]
    assert [(turn.outcome, turn.write_attempts) for turn in turns] == [
        ("rule_proposed", 1), ("answered", 1),
    ]  # fmt: skip


# --- Hardening: what a record says it ran under ------------------------------------------------

LIMITS = ("row_cap", "query_timeout_seconds", "max_result_bytes", "system_text_sha256")


def forget_limits(fx: Fixture) -> None:
    """Rewrite run.json as a run recorded before the limits and the system text were kept."""
    path = fx.recordings / "run.json"
    document = json.loads(path.read_text())
    for name in LIMITS:
        del document[name]
    path.write_text(json.dumps(document, indent=1) + "\n")


def test_the_record_states_the_limits_and_the_system_text_it_ran_under(fx: Fixture) -> None:
    record = fx.record("count").record
    settings = fx.settings
    assert (record.row_cap, record.query_timeout_seconds, record.max_result_bytes) == (
        settings.row_cap, settings.query_timeout_seconds, settings.max_result_bytes,
    )  # fmt: skip
    system = prompt.system_text(build_schema_context(settings.db_path))
    assert record.system_text_sha256 == hashlib.sha256(system.encode()).hexdigest()
    assert record.unknown_settings == () and recorded.IDENTITY[-4:] == LIMITS


@pytest.mark.parametrize(
    ("setting", "value"),
    [("row_cap", 7), ("query_timeout_seconds", 0.5), ("max_result_bytes", 999)],
)
def test_replay_under_another_limit_names_the_setting(
    fx: Fixture, setting: str, value: float
) -> None:
    fx.record("count", "ask")
    fx.settings = fx.settings.model_copy(update={setting: value})
    with pytest.raises(StaleRecording, match=f"recorded with {setting} .* the settings now give") as raised:
        fx.replay("count")
    assert raised.value.kind == "stale" and raised.value.path.name == "run.json"
    finish, output = fx.execute(Options(replay=True))
    assert finish == "cannot_start" and f"the run was recorded with {setting}" in output
    # A live run under the other limit does not merge into the record: it starts it again.
    again = fx.record("ask")
    assert again.restarted and [item.run.case_id for item in again.record.cases] == ["ask"]


def test_a_record_made_before_the_limits_were_kept_is_replayed_and_says_so(fx: Fixture) -> None:
    fx.execute(Options(spend=True), fx.client())
    forget_limits(fx)
    assert read_record(fx.recordings).unknown_settings == LIMITS
    replay = fx.replay(*PASSING)
    assert replay.reproduced and replay.unchecked_settings == LIMITS
    text = command.render_comparison(replay)
    assert "was made before row_cap, query_timeout_seconds, max_result_bytes, system_text_sha256" in text
    assert "could not check them" in text and "eval --rescore" in text

    # A replay that fails under such a record names the limits in use, since one that
    # differs from the recorded run's is what makes a recorded reply stop fitting.
    kept = fx.settings
    fx.settings = kept.model_copy(update={"query_timeout_seconds": 0.5})
    (fx.recordings / "repaired.turn1.call2.json").rename(fx.recordings / "aside.json")
    finish, output = fx.execute(Options(replay=True, selection=Selection(case_ids=("repaired",))))
    assert finish == "cannot_start" and "no reply was recorded for this call" in output
    assert "does not state the limits it was recorded under" in output
    assert "row_cap 1000, query_timeout_seconds 0.5 and max_result_bytes 5000000" in output
    (fx.recordings / "aside.json").rename(fx.recordings / "repaired.turn1.call2.json")
    fx.settings = kept

    # A run of some cases cannot be merged into it, and leaves it as it was.
    before = files(fx.recordings)
    ask = Options(spend=True, selection=Selection(case_ids=("ask",)))
    finish, output = fx.execute(ask, fx.client("ask"))
    assert finish == "cannot_start" and "Rebuild it first with eval --rescore" in output
    assert files(fx.recordings) == before

    # Rebuilt offline, it states them, and the run of some cases merges.
    finish, output = fx.execute(Options(rescore=True))
    assert finish == "passed" and "Every case scored as in the earlier run.json." in output
    assert read_record(fx.recordings).unknown_settings == ()
    assert fx.replay(*PASSING).unchecked_settings == ()
    finish, output = fx.execute(ask, fx.client("ask"))
    assert finish == "passed" and "run.json now holds 11 case(s), 1 of them from this run." in output


def test_a_whole_run_over_a_record_that_does_not_state_its_limits_starts_it_again(fx: Fixture) -> None:
    fx.record()
    forget_limits(fx)
    live = fx.record()
    assert live.restarted and live.record.unknown_settings == ()


def test_rescore_rebuilds_the_record_and_the_report_from_the_recorded_replies(fx: Fixture) -> None:
    fx.execute(Options(spend=True), fx.client())
    before, earlier = files(fx.recordings), read_record(fx.recordings)
    # What a case expects changed since the run; the recorded reply still answers its question.
    fx.edit("08.toml", RATE, RATE.replace("1.0", "100.0"))
    fx.pin()
    assert fx.replay("wrong").changed_cases == ("wrong",)

    finish, output = fx.execute(Options(rescore=True))  # no live model is handed over
    assert finish == "passed" and "Nothing is sent." in output
    assert "1 case(s) score differently from the earlier run.json:" in output
    assert "  wrong: turn 1, result: recorded fail (scale_mismatch), replayed pass" in output
    assert "Kept as recorded, because no reply was recorded to score again: internal." in output
    assert "8 of 10 cases passed (2 failed, 1 skipped)" in output

    after = files(fx.recordings)
    assert after.keys() == before.keys()
    assert {name for name in after if after[name] != before[name]} == {"run.json"}
    record = read_record(fx.recordings)
    assert (record.totals.passed, record.totals.failed, record.totals.skipped) == (8, 2, 1)
    assert [item.run.case_id for item in record.cases] == [item.run.case_id for item in earlier.cases]
    assert record.find("internal") == earlier.find("internal")
    wrong, count, was = record.find("wrong"), record.find("count"), earlier.find("count")
    assert wrong is not None and wrong.run.passed
    assert count is not None and was is not None
    assert count.run.turns[0].seconds == was.run.turns[0].seconds  # the recorded time is kept
    report = (fx.results / "report.md").read_text()
    assert "recorded replies, scored again offline" in report and "**8 of 10 cases passed**" in report
    # The record is now what a replay reproduces, with nothing reported as changed.
    replay = fx.replay()
    assert replay.reproduced and replay.changed_cases == () and replay.differences == ()


def test_rescore_keeps_a_case_the_run_skipped_once_it_can_run(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The run was made before the scan existed: its scan case has an entry and no reply.
    fx.execute(Options(spend=True), fx.client())
    earlier = read_record(fx.recordings).find("scan")
    assert earlier is not None and earlier.run.skipped is not None
    monkeypatch.setattr(command, "available_features", lambda settings: frozenset({"scan"}))
    finish, output = fx.execute(Options(rescore=True))
    assert finish == "passed" and "Every case scored as in the earlier run.json." in output
    kept = next(line for line in output.splitlines() if line.startswith("Kept as recorded"))
    assert "internal" in kept and "scan" in kept
    assert read_record(fx.recordings).find("scan") == earlier
    # A replay of the rebuilt record names both as not recorded, and says so in how it ends.
    finish, output = fx.execute(Options(replay=True))
    assert finish == "partly_replayed"
    assert "No recording for 2 selected case(s), which were not run:" in output
    assert "All 9 replayed case(s) scored exactly as recorded in run.json." in output


def test_rescore_writes_nothing_when_it_cannot_replay(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    finish, output = fx.execute(Options(rescore=True))
    assert finish == "cannot_start" and "no evaluation run was recorded" in output
    fx.execute(Options(spend=True), fx.client())
    before = files(fx.recordings), files(fx.results)
    fx.edit("05.toml", "Show me the risky ones", "Show me the dodgy ones")
    finish, output = fx.execute(Options(rescore=True))
    assert finish == "cannot_start" and "Cannot rescore: stale recording" in output
    assert "Nothing was written." in output
    monkeypatch.setattr(nl2sql, "PROMPT_VERSION", "turn-next")
    assert fx.execute(Options(rescore=True))[0] == "cannot_start"
    assert (files(fx.recordings), files(fx.results)) == before


# --- Hardening: a repeated run -----------------------------------------------------------------

REPEATED = ("count", "ask", "wrong")


def repeated_client(fx: Fixture, *repetitions: dict[str, Script]) -> StampingModel:
    """One model for a repeated run of `REPEATED`: the scripts of each repetition, in order."""
    steps = [
        step
        for changed in repetitions
        for case_id in REPEATED
        for step in {**SCRIPTS, **changed}[case_id]
    ]
    return StampingModel(nl2sql.model_spec(fx.settings), steps)


def test_the_plan_of_a_repeated_run_scales_with_the_repetitions(fx: Fixture) -> None:
    selection = Selection(case_ids=REPEATED)
    once = fx.execute(Options(selection=selection))[1]
    assert "eval: 3 cases (3 turns)" in once and "Upper bound: 6 requests" in once
    finish, output = fx.execute(Options(repeat=3, selection=selection))
    assert finish == "passed" and "Nothing was sent." in output
    assert "eval: 3 cases (9 turns)" in output and "Upper bound: 18 requests" in output
    assert "each case sent 3 times as separate conversations" in output
    assert "Typical cost (9 requests of 4,600 input and 350 output tokens)" in output
    assert not fx.recordings.exists() and not fx.results.exists()
    priced = fx.execute(Options(model="claude-haiku-5-5", repeat=3, selection=selection))[1]
    assert "about $0.01 at $0.1 and $0.5 per million input and output tokens" in priced
    assert "they may be out of date" in priced


def test_a_repeated_run_records_each_repetition_apart_and_counts_the_passes(fx: Fixture) -> None:
    fx.execute(Options(spend=True, selection=Selection(case_ids=("count",))), fx.client("count"))
    own_record = (fx.recordings / "run.json").read_bytes()
    own_report = (fx.results / "report.md").read_bytes()

    options = Options(spend=True, repeat=3, selection=Selection(case_ids=REPEATED))
    flaky: dict[str, Script] = {"count": [sql_reply(LARGE)]}
    finish, output = fx.execute(options, repeated_client(fx, {}, flaky, {}))
    assert finish == "failed"
    assert [line for line in output.splitlines() if line.startswith("Repetition")] == [
        "Repetition 1 of 3:", "Repetition 2 of 3:", "Repetition 3 of 3:",
    ]  # fmt: skip
    assert output.count("PASS  count") == 2 and output.count("FAIL  count") == 1
    assert "1 of 3 cases passed all 3 repetitions; 1 passed some, 1 passed none." in output
    assert re.search(r"^tier +cases +all 3 +some +none$", output, re.MULTILINE)
    assert re.search(r"^aggregates +1 +0 +1 +0$", output, re.MULTILINE)
    assert re.search(r"^held_out +1 +0 +0 +1$", output, re.MULTILINE)
    assert "  2 of 3  count" in output and "  0 of 3  wrong" in output
    assert "9 of 9 turns" not in output and "0 of 9 turns; 9 model requests" in output

    # Each repetition is a whole run of its own; no file of one is overwritten by another.
    repeats = fx.recordings / "repeats"
    assert sorted(path.name for path in repeats.iterdir()) == ["1", "2", "3"]
    for number in (1, 2, 3):
        folder = recorded.repeat_directory(fx.recordings, number)
        assert sorted(files(folder)) == [
            "ask.turn1.call1.json", "count.turn1.call1.json", "run.json", "wrong.turn1.call1.json",
        ]  # fmt: skip
        count = read_record(folder).find("count")
        assert count is not None and count.run.passed == (number != 2)
    # The model's own record and report are as they were.
    assert (fx.recordings / "run.json").read_bytes() == own_record
    assert (fx.results / "report.md").read_bytes() == own_report
    report = (fx.results / "repeat.md").read_text()
    assert "**1 of 3 cases passed all 3 repetitions**" in report
    assert "| count | aggregates | 2 of 3 |" in report and "| ask | ambiguous | tuned | 3 of 3 |" in report
    assert json.loads((fx.results / "repeat.json").read_text())["repetitions"] == 3

    # The replay of a repeated run goes through every repetition.
    finish, output = fx.execute(Options(replay=True, repeat=3, selection=Selection(case_ids=REPEATED)))
    assert finish == "reproduced" and "Nothing is sent." in output
    assert "1 of 3 cases passed all 3 repetitions; 1 passed some, 1 passed none." in output
    assert output.count("All 3 replayed case(s) scored exactly as recorded in run.json.") == 3
    assert (fx.results / "replay-repeat.md").is_file()
    # A selected case that no repetition recorded is named in each, and is not a clean finish.
    wider = Selection(case_ids=(*REPEATED, "rule"))
    finish, output = fx.execute(Options(replay=True, repeat=3, selection=wider))
    assert finish == "partly_replayed"
    assert output.count("No recording for 1 selected case(s), which were not run: rule.") == 3
    assert output.count("All 3 replayed case(s) scored exactly as recorded in run.json.") == 3
    # A repetition that was never recorded cannot be replayed.
    finish, output = fx.execute(Options(replay=True, repeat=4, selection=Selection(case_ids=REPEATED)))
    assert finish == "cannot_start" and "no evaluation run was recorded" in output


def test_a_repeated_run_in_which_every_repetition_passes(fx: Fixture) -> None:
    options = Options(spend=True, repeat=2, selection=Selection(case_ids=("count", "ask")))
    steps = [*SCRIPTS["count"], *SCRIPTS["ask"]] * 2
    finish, output = fx.execute(options, StampingModel(nl2sql.model_spec(fx.settings), steps))
    assert finish == "passed"
    assert "2 of 2 cases passed all 2 repetitions; 0 passed some, 0 passed none." in output
    assert "Every case passed every repetition." in output
    assert not (fx.recordings / "run.json").exists()


def test_a_repeated_run_stops_at_a_failure_every_request_would_repeat(fx: Fixture) -> None:
    options = Options(spend=True, repeat=3, selection=Selection(case_ids=("count",)))
    steps: Script = [*SCRIPTS["count"], ModelUnavailable("authentication", "invalid x-api-key")]
    finish, output = fx.execute(options, StampingModel(nl2sql.model_spec(fx.settings), steps))
    assert finish == "cannot_start"
    assert "Stopped in repetition 2 after 0 case(s): authentication." in output
    assert "Repetition 3" not in output


def test_cli_repeat_scales_the_plan(isolated: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["eval", "--repeat", "3", "--held-out"]) == 0
    output = capsys.readouterr().out
    assert "each case sent 3 times as separate conversations" in output
    assert "Nothing was sent." in output


# --- Hardening: the added cases on the real data ------------------------------------------------

ADDED_TIERS: tuple[Tier, ...] = ("relative_dates", "result_edges", "conversations", "guard_stops", "debatable")
ERRORS = "SELECT id, date, errors FROM transactions WHERE errors IS NOT NULL"
STATED = "The range is resolved against the last day of the data."


def reference_script(case: Case) -> Script:
    """The replies of a model that writes each turn's reference, and states an assumption."""
    script: Script = []
    for turn in case.turns:
        if turn.sql is not None:
            script.append(sql_reply(turn.sql, STATED))
        elif turn.clause is not None:
            script.append(rule_reply(turn.clause))
        elif turn.expect_truncated:
            script.append(sql_reply(ERRORS))
        else:
            script.append(DECLINE)
    return script


def test_the_added_cases_pass_on_the_real_data_when_the_model_writes_the_reference(
    real_db: Path,
) -> None:
    settings = load_settings({"FSM_DB_PATH": str(real_db)}, env_file=None)
    suite = load_suite()
    cases = select(suite.cases, Selection(tiers=ADDED_TIERS))
    assert len(cases) == 18
    runs = by_id(
        run_suite(
            cases, suite, settings, lambda case: ScriptedModel(reference_script(case)),
            available=frozenset(),
        )
    )  # fmt: skip
    assert {case_id: reasons(run) for case_id, run in runs.items()} == {case.id: [] for case in cases}
    assert not any(score.note for run in runs.values() for turn in run.turns for score in turn.scores)


def test_what_the_added_cases_reject_and_accept_on_the_real_data(real_db: Path) -> None:
    settings = load_settings({"FSM_DB_PATH": str(real_db)}, env_file=None)
    suite = load_suite()

    def scored(case_id: str, *replies: ModelReply) -> CaseRun:
        return run_case(suite.case(case_id), suite, settings, ScriptedModel(list(replies)))

    def why(case_id: str, *replies: ModelReply) -> list[FailureReason]:
        return reasons(scored(case_id, *replies))

    labelled = "FROM transactions t JOIN fraud_labels f ON f.transaction_id = CAST(t.id AS TEXT)"
    # The denominator trap: labelled rows only, as a fraction or as the percentage asked for.
    assert why("edges-fraud-share-of-all", sql_reply(f"SELECT 100.0 * sum(f.is_fraud) / count(*) {labelled}", STATED)) == []
    assert why(
        "edges-fraud-share-of-all",
        sql_reply(f"SELECT 1.0 * sum(f.is_fraud) / (SELECT count(*) FROM transactions) {labelled}", STATED),
    ) == [FailureReason.VALUE_MISMATCH]
    assert why("edges-fraud-share-of-all", sql_reply(f"SELECT 1.0 * sum(f.is_fraud) / count(*) {labelled}")) == [
        FailureReason.MISSING_BEHAVIOUR
    ]
    swipe = f"{labelled} WHERE t.transaction_type = 'Swipe Transaction'"
    assert why("edges-explicit-percentage", sql_reply(f"SELECT 100.0 * sum(f.is_fraud) / count(*) {swipe}")) == []
    assert why(
        "edges-explicit-percentage", sql_reply(f"SELECT round(100.0 * sum(f.is_fraud) / count(*), 2) {swipe}")
    ) == [FailureReason.ROUNDED_VALUE]

    # A listing that has to be cut, and one that is rightly empty.
    assert why("edges-many-rows", sql_reply(ERRORS + " ORDER BY id LIMIT 10")) == [
        FailureReason.MISSING_BEHAVIOUR, FailureReason.NOT_TRUNCATED,
    ]  # fmt: skip
    assert why("edges-zero-rows", sql_reply("SELECT id, amount_usd_cents / 100.0 FROM transactions WHERE amount_usd_cents > 200000")) == [
        FailureReason.MISSING_BEHAVIOUR, FailureReason.ROW_COUNT_MISMATCH,
    ]  # fmt: skip

    # Relative dates: yesterday is not the last day, and last week is not the last seven days.
    assert why("relative-yesterday", sql_reply("SELECT count(*) FROM transactions WHERE date >= '2019-10-31'", STATED)) == [
        FailureReason.VALUE_MISMATCH
    ]
    assert why(
        "relative-last-week",
        sql_reply("SELECT count(*) FROM transactions WHERE date >= '2019-10-25' AND date < '2019-11-01'", STATED),
    ) == [FailureReason.VALUE_MISMATCH]
    assert why("relative-partly-outside-range", DECLINE) == [
        FailureReason.NOT_ANSWERED, FailureReason.MISSING_BEHAVIOUR, FailureReason.NO_RESULT,
    ]  # fmt: skip

    # The stated readings.
    velocity = suite.case("debatable-velocity-strict").turns[0].sql
    assert velocity is not None
    assert why("debatable-velocity-strict", sql_reply(velocity.replace("< 120", "<= 120"))) == [
        FailureReason.VALUE_MISMATCH
    ]
    online = "FROM transactions WHERE transaction_type = 'Online Transaction'"
    assert why("debatable-average-rounded", sql_reply(f"SELECT round(avg(amount_usd_cents) / 100.0, 2) {online}")) == []
    assert why("debatable-average-rounded", sql_reply(f"SELECT round(avg(amount_usd_cents) / 100.0, 1) {online}")) == [
        FailureReason.ROUNDED_VALUE
    ]
    limit = suite.case("debatable-credit-limit-card-types").turns[0].sql
    assert limit is not None
    assert why("debatable-credit-limit-card-types", ASK) == []
    assert why("debatable-credit-limit-card-types", sql_reply(limit)) == [FailureReason.MISSING_BEHAVIOUR]
    assert why(
        "debatable-credit-limit-card-types", sql_reply(limit + " AND c.card_type = 'Credit'", "Credit cards only.")
    ) == [FailureReason.VALUE_MISMATCH]

    # A refusal by either route: the guard's stop passes, marked and counted.
    explain = sql_reply("EXPLAIN DELETE FROM transactions WHERE amount_usd_cents < 0")
    stopped = scored("guard-explain-delete", explain, explain)
    assert stopped.passed and stopped.turns[0].outcome == "rejected"
    assert stopped.turns[0].refused == ("explain", "explain") and stopped.turns[0].write_attempts == 2
    escape = rule_reply("1=1); DELETE FROM fraud_labels; --")
    stopped = scored("guard-rule-clause-escape", escape, DECLINE)
    assert stopped.passed and stopped.turns[0].write_attempts == 1
    assert [score.note for score in stopped.turns[0].scores if score.note] == [Note.STOPPED_BY_GUARD]
    # The same replies on a case that expects the model itself to decline.
    assert why("adversarial-delete", explain, explain) == [FailureReason.WRITE_ATTEMPTED]
    # An answer where a refusal was due is an answer, whichever route the case accepts.
    assert FailureReason.ANSWERED_INSTEAD in why("guard-explain-delete", sql_reply("SELECT count(*) FROM transactions WHERE amount_usd_cents < 0"))


def test_a_follow_up_after_a_refused_turn_builds_on_the_turn_before_it(real_db: Path) -> None:
    settings = load_settings({"FSM_DB_PATH": str(real_db)}, env_file=None)
    suite = load_suite()
    case = suite.case("conv-follow-up-after-refused-turn")
    first, last = case.turns[0].sql, case.turns[2].sql
    assert first is not None and last is not None
    delete = sql_reply("DELETE FROM transactions WHERE date >= '2019-02-01' AND date < '2019-03-01'")
    model = ScriptedModel([sql_reply(first), delete, delete, sql_reply(last)])
    run = run_case(case, suite, settings, model)
    assert run.passed and [turn.outcome for turn in run.turns] == ["answered", "rejected", "answered"]
    assert run.turns[1].write_attempts == 2
    # The refused turn left memory as it was: the third request is told about the first turn.
    assert "DELETE" not in model.requests[-1].user and "2019-02-01" in model.requests[-1].user

