"""The rule finder over HTTP and from the terminal, with scripted replies in place of Claude."""

import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import pytest
from conftest import call_reply, file_sha256, rule_reply, sql_reply
from fastapi.testclient import TestClient
from finder_fixtures import FADING, ROME, ROME_BY_NAME, build_history, scenarios_reply

from fsm_assistant import cli, config, demo, runtime, scenarios
from fsm_assistant.api import ApiError, FinderOverview, create_app
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.finder import FINDER_NOTE, RunProgress
from fsm_assistant.graph import Assistant
from fsm_assistant.llm import IntentModel, ModelError, ModelReply, ModelRequest, ModelSpec, ScriptedModel
from fsm_assistant.rule_bank import BankEntry, FinderRun

HOST = "http://127.0.0.1"
CLARIFY = call_reply("ask_clarification", question="Which amount?", suggestions=["$100", "$1,000"])

type Script = Sequence[ModelReply | ModelError]


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_history(tmp_path_factory.mktemp("finder-api") / "history.db")


@pytest.fixture
def settings(history: Path, tmp_path: Path) -> Settings:
    return load_settings(
        {
            "FSM_DB_PATH": str(history),
            "FSM_LOG_FILE": "off",
            "FSM_RULE_BANK_PATH": str(tmp_path / "var" / "bank.db"),
            "FSM_FINDER_BATCH_SIZE": "3",
        },
        env_file=None,
    )


def serve(
    settings: Settings, scenario_script: Script, turns: IntentModel | Script, chat: Script = ()
) -> TestClient:
    finder = runtime.open_finder(
        settings,
        scenario_model=ScriptedModel(scenario_script),
        turn_model=ScriptedModel(turns) if isinstance(turns, Sequence) else turns,
        replies="recorded",
    )
    app = create_app(
        settings, Assistant(settings, ScriptedModel(chat)), finder=finder, dist=Path("/nonexistent")
    )
    return TestClient(app, base_url=HOST)


def overview(client: TestClient) -> FinderOverview:
    response = client.get("/api/finder/entries")
    assert response.status_code == 200, response.text
    return FinderOverview.model_validate_json(response.content)


def propose(client: TestClient, count: int = 1) -> FinderRun:
    response = client.post("/api/finder/scenarios", json={"count": count})
    assert response.status_code == 200, response.text
    return FinderRun.model_validate_json(response.content)


def process(client: TestClient, entry_id: int) -> RunProgress:
    """Start processing and poll until the run has ended, as the page does."""
    response = client.post(f"/api/finder/scenarios/{entry_id}/process")
    assert response.status_code == 202, response.text
    progress = RunProgress.model_validate_json(response.content)
    for _ in range(2000):
        if progress.state != "running":
            return progress
        time.sleep(0.005)
        polled = client.get(f"/api/finder/runs/{progress.run_id}")
        assert polled.status_code == 200, polled.text
        progress = RunProgress.model_validate_json(polled.content)
    raise AssertionError("the run did not end")


class _Answer(Protocol):
    @property
    def status_code(self) -> int: ...
    @property
    def content(self) -> bytes: ...


def code(response: _Answer) -> tuple[int, str]:
    return response.status_code, ApiError.model_validate_json(response.content).code


def test_an_empty_bank_is_listed_without_being_created(settings: Settings) -> None:
    found = overview(serve(settings, [], []))
    assert (found.entries, found.proposed, found.discarded) == ((), (), 0)
    assert (found.last_run, found.running, found.active) == (None, False, None)
    assert (found.propose_requests, found.process_requests, found.note) == (1, 2, FINDER_NOTE)
    assert found.criteria.confirmation_starts == "2019-08"
    assert not settings.rule_bank_path.exists()


def test_find_proposes_one_scenario_and_process_turns_it_into_a_rule(settings: Settings) -> None:
    client = serve(settings, [scenarios_reply("rome"), scenarios_reply("vague")], [rule_reply(ROME)])
    found = FinderRun.model_validate_json(client.post("/api/finder/scenarios").content)
    assert (found.kind, found.status, found.scenarios, found.model_requests) == ("propose", "completed", 1, 1)
    waiting = overview(client)
    assert [scenario.title for scenario in waiting.proposed] == ["rome"] and waiting.entries == ()
    assert waiting.proposed[0].assumptions == ("assumed for rome",)

    ended = process(client, waiting.proposed[0].id)
    assert (ended.state, ended.outcome, ended.status, ended.clause) == ("completed", "added", "confirmed", ROME)
    after = overview(client)
    assert (after.proposed, after.running, after.active) == ((), False, None)
    assert [(entry.title, entry.status) for entry in after.entries] == [("rome", "confirmed")]
    rome = after.entries[0]
    assert rome.discovery is not None
    assert (rome.discovery.flagged, rome.discovery.true_positives, rome.discovery.false_positives) == (220, 60, 140)
    assert after.last_run is not None and after.last_run.kind == "process"

    detail = client.get(f"/api/finder/entries/{rome.id}")
    entry = BankEntry.model_validate_json(detail.content)
    assert entry.clause == ROME and entry.backtest_sql is not None and ROME in entry.backtest_sql
    assert entry.rule is not None and len(entry.rule.months) == 10
    assert entry.scenario_assumptions and entry.criteria and entry.caveats

    # The second find is told of the first scenario, and nothing measured about it.
    assert propose(client).items[0].title == "vague"


def test_several_scenarios_wait_and_each_is_processed_or_discarded_by_itself(
    settings: Settings,
) -> None:
    client = serve(
        settings, [scenarios_reply("fading", "unwanted", "rome")], [rule_reply(ROME), rule_reply(FADING)]
    )
    fading, unwanted, rome = (item.entry_id for item in propose(client, 3).items)
    assert process(client, rome).status == "confirmed"
    discarded = client.post(f"/api/finder/scenarios/{unwanted}/discard")
    assert discarded.status_code == 200
    assert BankEntry.model_validate_json(discarded.content).status == "discarded"
    found = overview(client)
    assert [scenario.id for scenario in found.proposed] == [fading] and found.discarded == 1
    assert process(client, fading).status == "not_confirmed"
    assert [entry.title for entry in overview(client).entries] == ["rome", "fading"]

    assert code(client.post(f"/api/finder/scenarios/{unwanted}/process")) == (409, "not_processable")
    assert code(client.post(f"/api/finder/scenarios/{rome}/discard")) == (409, "not_processable")
    assert code(client.post("/api/finder/scenarios/99/process")) == (404, "not_found")
    assert code(client.post("/api/finder/scenarios/99/discard")) == (404, "not_found")
    assert code(client.get("/api/finder/runs/99")) == (404, "not_found")
    assert client.post("/api/finder/scenarios", json={"count": 0}).status_code == 422
    assert client.post("/api/finder/scenarios", json={"count": 26}).status_code == 422


def test_a_duplicate_ends_as_a_match_that_names_the_rule(settings: Settings) -> None:
    client = serve(
        settings, [scenarios_reply("rome", "rome by name")], [rule_reply(ROME), rule_reply(ROME_BY_NAME)]
    )
    first, second = (item.entry_id for item in propose(client, 2).items)
    process(client, first)
    ended = process(client, second)
    assert (ended.state, ended.outcome, ended.result_entry_id, ended.match_kind) == (
        "completed", "duplicate", first, "same_transactions",
    )  # fmt: skip
    found = overview(client)
    assert found.proposed == () and [entry.matches for entry in found.entries] == [1]


def test_a_decision_is_recorded_and_survives_a_new_server(settings: Settings) -> None:
    client = serve(settings, [scenarios_reply("rome", "vague")], [rule_reply(ROME), CLARIFY])
    rule, no_rule = (item.entry_id for item in propose(client, 2).items)
    process(client, rule)
    process(client, no_rule)
    decided = client.post(
        f"/api/finder/entries/{rule}/decision", json={"decision": "approved", "note": "Holds."}
    )
    assert decided.status_code == 200, decided.text
    entry = BankEntry.model_validate_json(decided.content)
    assert (entry.decision, entry.decision_note) == ("approved", "Holds.")
    assert entry.decided_at is not None

    again = serve(settings, [], [])
    assert overview(again).entries[0].decision == "approved"
    rejected = again.post(f"/api/finder/entries/{rule}/decision", json={"decision": "rejected"})
    assert BankEntry.model_validate_json(rejected.content).decision_note is None

    assert code(again.post("/api/finder/entries/99/decision", json={"decision": "approved"})) == (404, "not_found")
    assert code(again.post(f"/api/finder/entries/{no_rule}/decision", json={"decision": "approved"})) == (
        409, "not_reviewable",
    )  # fmt: skip
    assert again.post(f"/api/finder/entries/{rule}/decision", json={"decision": "maybe"}).status_code == 422
    too_long = again.post(
        f"/api/finder/entries/{rule}/decision", json={"decision": "approved", "note": "x" * 1001}
    )
    assert too_long.status_code == 422
    assert again.get("/api/finder/entries/99").status_code == 404
    assert again.get("/api/finder/entries/abc").status_code == 422


def test_delete_removes_an_entry_of_any_state_and_touches_only_the_bank(settings: Settings) -> None:
    before = file_sha256(settings.db_path)
    client = serve(
        settings,
        [scenarios_reply("rome", "vague", "waiting", "unwanted"), scenarios_reply("next")],
        [rule_reply(ROME), CLARIFY],
    )
    rome, vague, waiting, unwanted = (item.entry_id for item in propose(client, 4).items)
    process(client, rome)
    process(client, vague)
    client.post(f"/api/finder/scenarios/{unwanted}/discard")
    client.post(f"/api/finder/entries/{rome}/decision", json={"decision": "approved", "note": "kept"})
    for entry_id in (rome, vague, waiting, unwanted):
        removed = client.delete(f"/api/finder/entries/{entry_id}")
        assert (removed.status_code, removed.content) == (204, b"")
        assert client.get(f"/api/finder/entries/{entry_id}").status_code == 404
    assert code(client.delete(f"/api/finder/entries/{rome}")) == (404, "not_found")
    found = overview(client)
    assert (found.entries, found.proposed, found.discarded) == ((), (), 0)
    assert found.last_run is not None, "the runs are kept as history"
    # Forgotten: the next scenario request quotes none of them.
    finder = runtime.open_finder(
        settings, scenario_model=(model := ScriptedModel([scenarios_reply("next")])),
        turn_model=ScriptedModel([]), replies="recorded",
    )  # fmt: skip
    finder.propose()
    assert "earlier_scenarios" not in model.requests[0].user
    assert file_sha256(settings.db_path) == before


def test_a_failed_find_is_a_result_and_not_an_http_error(settings: Settings) -> None:
    client = serve(settings, [call_reply("run_sql")], [])
    run = propose(client)
    assert run.status == "failed" and run.error is not None and run.error.kind == "scenario_shape"


class _Held:
    def __init__(self) -> None:
        self.entered, self.release = threading.Event(), threading.Event()

    def complete(self, request: ModelRequest) -> ModelReply:
        self.entered.set()
        assert self.release.wait(10)
        return rule_reply(ROME)


def test_everything_else_is_a_409_while_a_scenario_is_being_processed(settings: Settings) -> None:
    held = _Held()
    client = serve(settings, [scenarios_reply("rome", "other")], held)
    first, second = (item.entry_id for item in propose(client, 2).items)
    started = client.post(f"/api/finder/scenarios/{first}/process")
    assert started.status_code == 202
    run_id = RunProgress.model_validate_json(started.content).run_id
    try:
        assert held.entered.wait(10)
        found = overview(client)
        assert found.running and found.active is not None
        assert (found.active.run_id, found.active.entry_id, found.active.state) == (run_id, first, "running")
        assert found.active.scenario.title == "rome"
        for refused in (
            client.post("/api/finder/scenarios"),
            client.post(f"/api/finder/scenarios/{second}/process"),
            client.post(f"/api/finder/scenarios/{first}/discard"),
            client.delete(f"/api/finder/entries/{first}"),
        ):
            assert code(refused) == (409, "finder_busy")
        assert client.delete(f"/api/finder/entries/{second}").status_code == 204, "another entry may go"
    finally:
        held.release.set()
    ended = process_poll(client, run_id)
    assert (ended.state, ended.status) == ("completed", "confirmed")
    assert not overview(client).running


def process_poll(client: TestClient, run_id: int) -> RunProgress:
    for _ in range(2000):
        progress = RunProgress.model_validate_json(client.get(f"/api/finder/runs/{run_id}").content)
        if progress.state != "running":
            return progress
        time.sleep(0.005)
    raise AssertionError("the run did not end")


def test_a_restarted_server_shows_an_interrupted_run_and_the_scenario_still_waiting(
    settings: Settings,
) -> None:
    client = serve(settings, [scenarios_reply("rome")], [])
    entry_id = propose(client).items[0].entry_id
    bank = runtime.open_bank(settings, "recorded")
    abandoned = bank.start_run(
        kind="process", source="scripted", batch_size=1, request_bound=2, model_id="m",
        scenario_prompt_version="scenarios-2", rule_prompt_version="turn-5",
    )  # fmt: skip
    restarted = serve(settings, [], [rule_reply(ROME)])
    found = overview(restarted)
    assert (found.running, found.active) == (False, None)
    assert found.last_run is not None and (found.last_run.id, found.last_run.status) == (abandoned, "interrupted")
    assert code(restarted.get(f"/api/finder/runs/{abandoned}")) == (404, "not_found")
    assert [scenario.id for scenario in found.proposed] == [entry_id]
    assert process(restarted, entry_id).status == "confirmed"


def test_without_a_finder_and_with_a_broken_bank_the_answer_is_a_typed_503(
    settings: Settings, tmp_path: Path
) -> None:
    bare = TestClient(
        create_app(settings, Assistant(settings, ScriptedModel(())), dist=tmp_path / "none"),
        base_url=HOST,
    )
    broken = serve(settings.model_copy(update={"rule_bank_path": tmp_path}), [], [])
    (tmp_path / "x").mkdir()
    for response in (
        bare.get("/api/finder/entries"), bare.get("/api/finder/entries/1"),
        bare.post("/api/finder/scenarios"), bare.post("/api/finder/scenarios/1/process"),
        bare.post("/api/finder/scenarios/1/discard"), bare.get("/api/finder/runs/1"),
        bare.delete("/api/finder/entries/1"),
    ):  # fmt: skip
        assert code(response) == (503, "finder_unavailable")
    run = propose(broken)
    assert run.error is not None and run.error.kind == "bank_unavailable"


def test_a_chat_turn_never_opens_the_bank(settings: Settings) -> None:
    client = serve(settings, [], [], chat=[sql_reply("SELECT count(*) FROM transactions"), rule_reply(ROME)])
    for message in ("How many?", "Propose a rule for Rome."):
        response = client.post("/api/chat", json={"conversation_id": "c1", "message": message})
        assert response.status_code == 200 and '"outcome":"failed"' not in response.text
    assert client.get("/api/health").status_code == 200
    assert not settings.rule_bank_path.exists() and not settings.rule_bank_path.parent.exists()


# --- The command ------------------------------------------------------------------------------


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    return settings


def scripted(monkeypatch: pytest.MonkeyPatch, scenario_script: Script, turns: Script) -> list[str]:
    built: list[str] = []
    models = {
        scenarios.SCENARIO_PROMPT_VERSION: ScriptedModel(scenario_script),
        "turn-5": ScriptedModel(turns),
    }

    def factory(spec: ModelSpec, settings: Settings) -> IntentModel:
        built.append(spec.prompt_version)
        return models[spec.prompt_version]

    monkeypatch.setattr(cli, "_live_model", factory)
    return built


def test_find_rules_sends_nothing_without_yes_spend(
    isolated: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built = scripted(monkeypatch, [], [])
    assert cli.main(["find-rules"]) == 0
    printed = capsys.readouterr().out
    assert "At most 7 model requests (1 + 2 x 3)" in printed and "Nothing was sent." in printed
    assert "1. Propose" in printed and "2. Process" in printed
    assert built == [] and not isolated.rule_bank_path.exists()
    assert cli.main(["find-rules", "--batch", "10"]) == 0
    assert "At most 21 model requests" in capsys.readouterr().out


def test_find_rules_proposes_then_processes_and_prints_the_two_phases_apart(
    isolated: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built = scripted(
        monkeypatch,
        [scenarios_reply("rome", "rome by name", "vague")],
        [rule_reply(ROME), rule_reply(ROME_BY_NAME), CLARIFY],
    )
    assert cli.main(["find-rules", "--yes-spend"]) == 0
    printed = capsys.readouterr().out
    assert sorted(built) == ["scenarios-2", "turn-5"]
    assert printed.index("Phase 1, propose") < printed.index("Run 1 (propose): completed. 3 of 3 scenarios")
    assert printed.index("Run 1 (propose)") < printed.index("Phase 2, process") < printed.index("Run 2 (process)")
    assert "1. rome: entry 1, proposed, waiting to be processed" in printed
    assert "… writing the rule (model request)" in printed and "… judging the evidence" in printed
    assert "1. rome: entry 1, confirmed" in printed
    assert "2. rome by name: already in the bank as entry 1 (the same transactions); no entry was added" in printed
    assert "3. vague: entry 3, no rule built" in printed
    assert "Rule bank: 2 entries, 1 confirmed." in printed
    assert "Hosted tracing: off." in printed

    assert cli.main(["find-rules", "--list"]) == 0
    listed = capsys.readouterr().out
    assert "confirmed" in listed and "30.00%" in listed and "7.4" in listed and "rome" in listed


def test_find_rules_exits_1_for_a_failed_run_and_2_when_it_cannot_start(
    isolated: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(monkeypatch, [call_reply("run_sql")], [])
    assert cli.main(["find-rules", "--yes-spend"]) == 1
    assert "Run 1 (propose): failed." in capsys.readouterr().out
    with pytest.raises(SystemExit) as bad:
        cli.main(["find-rules", "--batch", "0"])
    assert bad.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["find-rules", "--yes-spend", "--list"])
    monkeypatch.setattr(cli, "load_settings", lambda: load_settings({"FSM_FINDER_BATCH_SIZE": "99"}, env_file=None))
    assert cli.main(["find-rules"]) == 2
    assert "FSM_FINDER_BATCH_SIZE must be at most 25" in capsys.readouterr().out


def test_the_list_of_an_empty_bank_says_so(isolated: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["find-rules", "--list"]) == 0
    assert capsys.readouterr().out.strip() == "The rule bank is empty."


# --- The offline demo -------------------------------------------------------------------------


def test_the_offline_demo_runs_its_canned_scenarios_into_a_bank_of_its_own(
    history: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def defaults(environ: dict[str, str], env_file: Path | None) -> Settings:
        assert environ == {} and env_file is None, "the demo reads no .env"
        return load_settings(
            {"FSM_DB_PATH": str(history), "FSM_LOG_FILE": "off",
             "FSM_RULE_BANK_PATH": str(tmp_path / "bank.db")}, env_file=None,
        )  # fmt: skip

    def no_live_model(spec: ModelSpec, settings: Settings) -> IntentModel:
        raise AssertionError("the demo may not build a live model")

    monkeypatch.setattr(config, "load_settings", defaults)
    monkeypatch.setattr(cli, "_live_model", no_live_model)
    count = len(demo.FINDER_SCENARIOS)
    assert cli.main(["find-rules", "--offline-demo", "--batch", "2"]) == 0
    printed = capsys.readouterr().out
    assert "OFFLINE DEMO" in printed and "Run 1 (propose): completed. 2 of 2 scenarios" in printed
    assert "no rule built" not in printed, "every canned clause passes the guard"
    assert (tmp_path / "bank.demo.db").is_file() and not (tmp_path / "bank.db").exists()
    # One canned scenario per find, each a new one, until the list is exhausted.
    seen = [found.scenario.title for found in demo.FINDER_SCENARIOS[:2]]
    for _ in range(count - 2):
        assert cli.main(["find-rules", "--offline-demo"]) == 0
        title = next(found.scenario.title for found in demo.FINDER_SCENARIOS if found.scenario.title not in seen)
        assert f"1. {title[:60]}" in capsys.readouterr().out
        seen.append(title)
    assert cli.main(["find-rules", "--offline-demo"]) == 1
    exhausted = capsys.readouterr().out
    assert "The offline demo has no canned scenario left" in exhausted and "[demo_exhausted]" in exhausted


def test_the_canned_scenarios_state_their_assumptions(history: Path) -> None:
    assert len(demo.FINDER_SCENARIOS) >= 5
    for found in demo.FINDER_SCENARIOS:
        assert found.scenario.assumptions and found.arguments["assumptions"], found.scenario.title
        assert found.tool == "propose_rule"
    titles = [found.scenario.title for found in demo.FINDER_SCENARIOS]
    assert len(set(titles)) == len(titles)
