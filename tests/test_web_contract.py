"""What the web interface is built against: its generated types and its fixtures.

The interface's types (`frontend/src/api/types.gen.ts`) are written from the
JSON schema of the API's models, and its tests read real responses kept under
`frontend/src/test/fixtures/`. Both are regenerated here and compared with
the committed files, so a change to `TurnResult` or to another API body fails
this test until they are renewed:

    FSM_UPDATE_WEB_FIXTURES=1 uv run pytest tests/test_web_contract.py

Each fixture is the body of a response of the API, made by a scripted model on
a small database. Only the time a turn took is set to a fixed number.
"""

import json
import os
import re
from pathlib import Path

import pytest
from pydantic import JsonValue
from conftest import RecordingSink, call_reply, rule_reply, sql_reply
from fastapi.testclient import TestClient
from finder_fixtures import BACKGROUND, FADING, LABEL_LEAK, ROME_BY_NAME, build_history
from scan_fixtures import MAIN, SACRAMENTO, Group, build

from fsm_assistant import demo, runtime, typescript
from fsm_assistant.api import ApiError, FinderOverview, create_app
from fsm_assistant.config import PROJECT_ROOT, Settings, load_settings
from fsm_assistant.events import Label
from fsm_assistant.finder import Finder, RunProgress
from fsm_assistant.graph import Assistant
from fsm_assistant.llm import ModelError, ModelReply, ModelUnavailable, ScriptedModel
from fsm_assistant.result import TurnResult
from fsm_assistant.rule_bank import BankEntry, FinderRun
from fsm_assistant.tracing import Destination, Tracer

FIXTURES = PROJECT_ROOT / "frontend" / "src" / "test" / "fixtures"
UPDATE = os.environ.get("FSM_UPDATE_WEB_FIXTURES") == "1"
RENEW = "renew it with: FSM_UPDATE_WEB_FIXTURES=1 uv run pytest tests/test_web_contract.py"

COUNT = "SELECT count(*) AS transactions FROM transactions"
BY_CARD = """\
SELECT c.card_type,
       t.merchant_id,
       count(*) AS transactions,
       sum(t.amount_usd_cents) AS total_amount_usd_cents,
       avg(f.is_fraud) AS fraud_rate
FROM transactions AS t
JOIN cards AS c ON c.id = t.card_id
LEFT JOIN fraud_labels AS f ON f.transaction_id = CAST(t.id AS TEXT)
GROUP BY c.card_type, t.merchant_id
ORDER BY c.card_type, t.merchant_id"""
ALL_ROWS = "SELECT id, date, amount_usd_cents, errors FROM transactions ORDER BY id"
NO_ROWS = "SELECT id, amount_usd_cents FROM transactions WHERE amount_usd_cents > 99999999"
HOSTILE = (
    "SELECT '<img src=x onerror=alert(1)>' AS html, "
    "'first' || char(10) || 'second' || char(27) || '[31m' || char(0) || 'end' AS control, "
    "'<b>bold</b>' AS \"<i>name</i>\""
)
FORBIDDEN = "SELECT name FROM sqlite_master"
WRITE = "DELETE FROM fraud_labels"
VALUE_IN_ERROR = "SELECT json_extract('{}', (SELECT address FROM users ORDER BY id LIMIT 1))"
ROME_RULE = (
    "merchant_location_id IN "
    "(SELECT id FROM merchant_locations WHERE city = 'Rome' AND state = 'Italy')"
)
# A rule on Rome, Italy that is strong early in the year and weak later: May is
# its weakest month, and the later period is well below the earlier one.
RULE_HISTORY = (
    Group(labelled=120, fraud=90, unlabelled=30, timestamp="2019-01-15 11:00:00"),
    Group(labelled=60, fraud=45, timestamp="2019-03-15 11:00:00"),
    Group(labelled=80, fraud=8, unlabelled=10, timestamp="2019-05-15 11:00:00"),
    Group(labelled=50, fraud=10, timestamp="2019-08-15 11:00:00"),
    Group(labelled=40, fraud=6, timestamp="2019-09-15 11:00:00"),
    Group(labelled=20, fraud=1, timestamp="2019-10-15 11:00:00"),
    *(
        Group(labelled=900, fraud=2, unlabelled=300, location=SACRAMENTO, merchant=20,
              timestamp=f"2019-{month:02d}-10 09:00:00", amount=517)
        for month in range(1, 11)
    ),
)  # fmt: skip


def _settings(db_path: Path, **variables: str) -> Settings:
    return load_settings(
        {"FSM_DB_PATH": str(db_path), "FSM_LOG_FILE": "off", **variables}, env_file=None
    )


def _stable(text: str) -> str:
    """A response as its fixture holds it: indented, with the one clock reading fixed."""
    body = json.loads(text)
    if "meta" in body:
        body["meta"]["elapsed_ms"] = 1234
        body["notes"] = [re.sub(r"ran in \d+\.\d s", "ran in 0.1 s", note) for note in body["notes"]]
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


FIXED_TIME = "2000-01-01T00:00:00+00:00"
FINDER_SCENARIOS: dict[str, JsonValue] = {
    "rome": {
        "title": "Card used in Rome, Italy",
        "hypothesis": "A card issued in the US is used at a merchant location in Rome, Italy, "
        "far from its owner. Flag every transaction at a location with city 'Rome' and state 'Italy'.",
        "columns": ["transactions.merchant_location_id", "merchant_locations.city", "merchant_locations.state"],
        "assumptions": [
            "merchant_locations.state holds a country name outside the US.",
            "Every cardholder lives in the US; the schema does not say so.",
        ],
    },
    "rome_again": {
        "title": "Purchases at Italian locations named Rome",
        "hypothesis": "A stolen card is used abroad. Flag transactions at merchant location 1.",
        "columns": ["transactions.merchant_location_id"],
        "assumptions": ["Location 1 is the one in Rome, Italy."],
    },
    "fading": {
        "title": "One merchant taking many cards",
        "hypothesis": "A colluding merchant runs stolen cards. Flag every transaction at merchant 30.",
        "columns": ["transactions.merchant_id"],
        "assumptions": ["Merchant 30 stands for a merchant with unusually many distinct cards."],
    },
    "background": {
        "title": "Card testing at $5.17",
        "hypothesis": "A stolen card is tested with a small fixed charge. Flag transactions of exactly "
        "$5.17 (517 cents).",
        "columns": ["transactions.amount_usd_cents"],
        "assumptions": ["$5.17 is a chosen amount; no data supports it.", "Negative amounts are left out."],
    },
    "vague": {
        "title": "Unusually large purchase",
        "hypothesis": "A stolen card is used for one purchase much larger than usual.",
        "columns": ["transactions.amount_usd_cents"],
        "assumptions": ["Larger than usual is left undefined."],
    },
    "dark_web": {
        "title": "Dark-web-exposed card spent on cash-equivalent merchants",
        "hypothesis": "A card whose number was sold is used to buy what turns into cash. Flag money "
        "transfers of at least $100 on a card flagged as seen on the dark web.",
        "columns": ["cards.card_on_dark_web", "merchants.mcc", "transactions.amount_usd_cents"],
        "assumptions": ["card_on_dark_web = 1 marks an exposed card; the schema lists only 0."],
    },
    "waiting": {
        "title": "Many declined attempts before one success",
        "hypothesis": "A stolen card number is tried with guessed details. Flag a successful "
        "transaction on a card that had at least 3 transactions with an error in the hour before.",
        "columns": ["transactions.errors", "transactions.card_id", "transactions.date"],
        "assumptions": ["Any non-NULL value of errors counts as a declined attempt."],
    },
    "unwanted": {
        "title": "Purchases at night",
        "hypothesis": "Fraud happens while the cardholder sleeps. Flag transactions between 01:00 and 05:00.",
        "columns": ["transactions.date"],
        "assumptions": ["transactions.date is in the cardholder's local time."],
    },
    "leak": {
        "title": "Transactions already known as fraud",
        "hypothesis": "Flag the transactions the fraud team has already labelled as fraud.",
        "columns": ["fraud_labels.is_fraud"],
        "assumptions": ["A label is known when the transaction happens."],
    },
}
# One long line, as a model writes it: an exposed card, a cash-like merchant, an amount.
DARK_WEB = (
    "transactions.amount_usd_cents >= 10000 AND transactions.card_id IN (SELECT c.id FROM cards c "
    "WHERE c.card_on_dark_web = 1) AND transactions.merchant_id IN (SELECT m.id FROM merchants m "
    "WHERE m.mcc IN (4829, 6011, 6051)) AND transactions.transaction_type IN ('Online Transaction', "
    "'Swipe Transaction', 'Chip Transaction')"
)
VAGUE = call_reply(
    "ask_clarification",
    question="How much larger than usual should a purchase be to count?",
    suggestions=["At least 5 times the card's average amount", "Above $1,000"],
)


def _stable_finder(text: str) -> str:
    """A finder response as its fixture holds it: the clock readings fixed."""

    def fixed(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: 1234
                if key == "elapsed_ms"
                else FIXED_TIME
                if key.endswith("_at") and isinstance(item, str)
                else fixed(item)
                for key, item in value.items()
            }
        return [fixed(item) for item in value] if isinstance(value, list) else value

    return json.dumps(fixed(json.loads(text)), indent=2, ensure_ascii=False) + "\n"


def _finder(
    settings: Settings, names: tuple[str, ...], *turns: ModelReply | ModelError, offline: bool = False
) -> tuple[TestClient, Finder]:
    """A server whose finder will be told `names` as its scenarios and answer their turns with `turns`."""
    batch = call_reply("propose_scenarios", scenarios=[FINDER_SCENARIOS[name] for name in names])
    finder = runtime.open_finder(
        settings,
        scenario_model=ScriptedModel([batch]),
        turn_model=ScriptedModel(turns),
        replies="demo" if offline else "recorded",
    )
    app = create_app(settings, Assistant(settings, ScriptedModel(())), finder=finder)
    return TestClient(app, base_url="http://127.0.0.1"), finder


def _frames(finder: Finder, entry_id: int) -> str:
    """Every progress body of processing one scenario, in order, as the page would poll them."""
    frames: list[JsonValue] = []
    finder.process(entry_id, lambda frame: frames.append(json.loads(frame.model_dump_json())))
    return _stable_finder(json.dumps(frames))


def _finder_fixtures(folder: Path) -> dict[str, str]:
    """The finder's responses: an empty bank, scenarios proposed, each processed, a duplicate, a decision."""
    history = build_history(folder / "history.db")

    def settings(name: str) -> Settings:
        return _settings(history, FSM_RULE_BANK_PATH=str(folder / f"{name}.db"))

    first, finder = _finder(
        settings("bank"),
        ("rome", "fading", "background", "vague", "leak", "rome_again", "waiting", "unwanted"),
        rule_reply(ROME_BY_NAME, "Rome means the city in Italy, not the towns of that name in the US."),
        rule_reply(FADING, "Merchant 30 is taken as the merchant meant."),
        rule_reply(BACKGROUND, "Only an amount of exactly 517 cents is flagged."),
        VAGUE,
        rule_reply(LABEL_LEAK),
        rule_reply(LABEL_LEAK),
        rule_reply("merchant_location_id = 1"),
    )
    empty = first.get("/api/finder/entries").text
    proposed = first.post("/api/finder/scenarios", json={"count": 8}).text
    waiting = first.get("/api/finder/entries").text
    scenario = first.get("/api/finder/entries/1").text
    frames = {name: _frames(finder, number) for number, name in enumerate(
        ("confirmed", "not_confirmed", "no_signal", "clarification", "rejected", "duplicate"), start=1
    )}  # fmt: skip
    discarded = first.post("/api/finder/scenarios/8/discard").text
    overview = first.get("/api/finder/entries").text
    as_found = {number: first.get(f"/api/finder/entries/{number}").text for number in range(1, 6)}
    approved = first.post(
        "/api/finder/entries/1/decision",
        json={"decision": "approved", "note": "Holds: every labelled fraud is in Rome, Italy."},
    ).text
    nothing, none_finder = _finder(
        settings("nothing"), ("fading", "background", "vague", "dark_web"),
        rule_reply(FADING), rule_reply(BACKGROUND), VAGUE,
        rule_reply(DARK_WEB, "An exposed card is one with card_on_dark_web = 1."),
    )  # fmt: skip
    for item in none_finder.propose(4).items:
        none_finder.process(item.entry_id)
    failed = _finder(settings("failed"), ())[0].post("/api/finder/scenarios").text
    exhausted = _finder(settings("demo"), (), offline=True)[0].post("/api/finder/scenarios").text
    silent, silent_finder = _finder(
        settings("silent"), ("rome",), ModelUnavailable("timeout", "The model did not reply in time.")
    )
    silent.post("/api/finder/scenarios")
    return {
        "finder_overview_empty.json": _stable_finder(empty),
        "finder_proposed.json": _stable_finder(proposed),
        "finder_proposed_failed.json": _stable_finder(failed),
        "finder_proposed_demo_exhausted.json": _stable_finder(exhausted),
        "finder_overview_waiting.json": _stable_finder(waiting),
        "finder_overview.json": _stable_finder(overview),
        "finder_overview_none_confirmed.json": _stable_finder(nothing.get("/api/finder/entries").text),
        **{f"finder_progress_{name}.json": text for name, text in frames.items()},
        "finder_progress_failed.json": _frames(silent_finder, 1),
        "finder_entry_proposed.json": _stable_finder(scenario),
        "finder_entry_flags_nothing.json": _stable_finder(nothing.get("/api/finder/entries/4").text),
        "finder_entry_discarded.json": _stable_finder(discarded),
        "finder_entry_confirmed.json": _stable_finder(as_found[1]),
        "finder_entry_approved.json": _stable_finder(approved),
        "finder_entry_not_confirmed.json": _stable_finder(as_found[2]),
        "finder_entry_no_signal.json": _stable_finder(as_found[3]),
        "finder_entry_clarification.json": _stable_finder(as_found[4]),
        "finder_entry_rejected.json": _stable_finder(as_found[5]),
        "api_error_finder_busy.json": _stable(
            ApiError(
                code="finder_busy",
                message="The rule finder is busy with another request. Wait for it to finish, "
                "then try again.",
            ).model_dump_json()
        ),
    }


def _turn(settings: Settings, question: str, *script: ModelReply | ModelError) -> str:
    assistant = Assistant(settings, ScriptedModel(script), label=Label(command="web"))
    client = TestClient(create_app(settings, assistant), base_url="http://127.0.0.1")
    response = client.post(
        "/api/chat", json={"conversation_id": "fixture-conversation", "message": question}
    )
    assert response.status_code == 200, response.text
    return _stable(response.text)


def _health(settings: Settings, *, offline: bool = False, traced: bool = False) -> str:
    app = create_app(
        settings,
        Assistant(settings, ScriptedModel(())),
        mode="offline_demo" if offline else "live",
        model_id=demo.MODEL_ID if offline else None,
        demo_questions=demo.QUESTIONS if offline else (),
        tracer=Tracer(RecordingSink(), Destination(project="fsm-assistant")) if traced else None,
    )
    return _stable(TestClient(app, base_url="http://127.0.0.1").get("/api/health").text)


@pytest.fixture(scope="module")
def fixtures(fixture_db: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Every fixture file's name and the text it should hold."""
    folder = tmp_path_factory.mktemp("web-fixtures")
    small = _settings(fixture_db)
    keyed = _settings(fixture_db, ANTHROPIC_API_KEY="sk-ant-not-a-real-key")
    scan = _settings(build(folder / "scan.db", MAIN))
    rule = _settings(build(folder / "rule.db", RULE_HISTORY))
    refused = TestClient(create_app(small, Assistant(small, ScriptedModel(()))), base_url="http://127.0.0.1")
    return {
        **_finder_fixtures(folder),
        "answered_scalar.json": _turn(
            small, "How many transactions are there?", sql_reply(COUNT, explanation="Counts every transaction.")
        ),
        "answered_table.json": _turn(
            small,
            "What are the totals and the fraud rate by card type and merchant?",
            sql_reply(
                BY_CARD,
                "A transaction without a fraud label does not count towards the fraud rate.",
                "Amounts include refunds.",
                explanation="One row per card type and merchant, with the total amount and the fraud rate.",
            ),
        ),
        "answered_truncated.json": _turn(
            _settings(fixture_db, FSM_ROW_CAP="3"), "List the transactions.", sql_reply(ALL_ROWS)
        ),
        "answered_empty.json": _turn(
            small, "Which transactions are above a million dollars?", sql_reply(NO_ROWS)
        ),
        "answered_hostile.json": _turn(
            small,
            "Show me <script>alert('question')</script>",
            sql_reply(
                HOSTILE,
                "<script>alert('assumption')</script>",
                explanation="<img src=x onerror=alert('explanation')> with a\ttab and an \x1b[31mescape.",
            ),
        ),
        "rule_proposed.json": _turn(
            rule, "Propose a rule for transactions in Rome, Italy.",
            rule_reply(ROME_RULE, "Rome means the city in Italy."),
        ),
        "scan.json": _turn(scan, "Where is fraud concentrated?", call_reply("profile_fraud")),
        "clarification.json": _turn(
            small,
            "Show me the risky ones.",
            call_reply(
                "ask_clarification",
                question="What should count as risky?",
                suggestions=["Transactions labelled as fraud", "Transactions above $1,000"],
            ),
        ),
        "cannot_answer.json": _turn(
            small,
            "What will the fraud rate be next year?",
            call_reply("decline", reason="The data ends in October 2019 and holds no forecast."),
        ),
        "rejected.json": _turn(
            small, "List the tables.", sql_reply(FORBIDDEN), sql_reply(FORBIDDEN)
        ),
        "rejected_write.json": _turn(
            small, "Delete the fraud labels.", sql_reply(WRITE), sql_reply(WRITE)
        ),
        "answered_after_write.json": _turn(
            small, "Delete the fraud labels, then count them.", sql_reply(WRITE), sql_reply(COUNT)
        ),
        "failed_sql.json": _turn(
            small, "Read a JSON path.", sql_reply(VALUE_IN_ERROR), sql_reply(VALUE_IN_ERROR)
        ),
        "failed_model.json": _turn(
            small,
            "How many transactions are there?",
            ModelUnavailable("timeout", "The model did not reply in time."),
        ),
        "health_live.json": _health(keyed, traced=True),
        "health_no_key.json": _health(small),
        "health_demo.json": _health(small, offline=True),
        "health_no_database.json": _health(_settings(folder / "absent.db", ANTHROPIC_API_KEY="k")),
        "api_error_invalid.json": _stable(
            refused.post("/api/chat", json={"conversation_id": "c1", "message": " "}).text
        ),
    }


def test_the_generated_types_are_up_to_date() -> None:
    wanted = typescript.render()
    if UPDATE:
        typescript.TYPES_FILE.write_text(wanted, encoding="utf-8")
    assert typescript.TYPES_FILE.is_file(), f"types.gen.ts is missing; {RENEW}"
    assert typescript.TYPES_FILE.read_text(encoding="utf-8") == wanted, (
        f"an API model changed and frontend/src/api/types.gen.ts did not; {RENEW}"
    )


def test_the_fixtures_are_what_the_api_returns_today(fixtures: dict[str, str]) -> None:
    if UPDATE:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for stale in set(path.name for path in FIXTURES.glob("*.json")) - set(fixtures):
            (FIXTURES / stale).unlink()
        for name, text in fixtures.items():
            (FIXTURES / name).write_text(text, encoding="utf-8")
    kept = {path.name: path.read_text(encoding="utf-8") for path in FIXTURES.glob("*.json")}
    assert sorted(kept) == sorted(fixtures), f"the fixture files differ from the list here; {RENEW}"
    changed = [name for name, text in fixtures.items() if kept[name] != text]
    assert not changed, f"the API no longer returns what these fixtures hold: {changed}; {RENEW}"


def test_the_types_declare_every_field_of_a_turn_result() -> None:
    declared = typescript.render()
    block = declared.split("export interface TurnResult {")[1].split("}")[0]
    fields = re.findall(r"^  (\w+)\??: ", block, flags=re.MULTILINE)
    assert fields == list(TurnResult.model_fields), "in the order the model declares them"
    assert "?" not in block, "a response always holds every field"
    assert "message: string;" in declared.split("export interface ChatRequest {")[1]


def test_the_fixtures_cover_every_outcome_and_what_the_interface_must_render(
    fixtures: dict[str, str],
) -> None:
    turns = {
        name: TurnResult.model_validate_json(text)
        for name, text in fixtures.items()
        if not name.startswith(("health", "api_error", "finder"))
    }
    assert {turn.outcome for turn in turns.values()} == {
        "answered", "rule_proposed", "clarification", "cannot_answer", "rejected", "failed",
    }  # fmt: skip
    assert {turn.status for turn in turns.values()} == {
        "ok", "needs_clarification", "cannot_answer", "rejected", "error",
    }  # fmt: skip
    table = turns["answered_table.json"].table
    assert table is not None
    assert {column.format for column in table.columns} == {
        "plain", "identifier", "usd_cents", "fraction",
    }  # fmt: skip
    assert any(None in row for row in table.rows), "a NULL cell"
    assert turns["answered_truncated.json"].table is not None
    assert turns["answered_truncated.json"].table.truncated
    assert turns["answered_empty.json"].caveats[0].code == "zero_rows"

    rule = turns["rule_proposed.json"]
    assert rule.rule is not None and rule.where_clause == ROME_RULE
    assert rule.rule.weakest_month == "2019-05" and len(rule.rule.months) == 10
    assert {caveat.code for caveat in rule.caveats} >= {"weak_month", "period_drop"}
    assert rule.rule.earlier.precision is not None and rule.rule.later.precision is not None

    scan = turns["scan.json"]
    assert scan.tool == "profile_fraud" and scan.notes and scan.suggestions and scan.table
    assert scan.sql is not None and scan.sql.count("\n") > 30, "a long statement"
    failed = turns["failed_sql.json"]
    assert failed.error is not None and failed.error.detail, "SQLite's own words, for the user"
    assert turns["rejected.json"].refused and turns["clarification.json"].suggestions
    assert "\x1b" in fixtures["answered_hostile.json"] or "\\u001b" in fixtures["answered_hostile.json"]


def test_the_finder_fixtures_cover_every_status_and_what_the_tab_must_render(
    fixtures: dict[str, str],
) -> None:
    overview = FinderOverview.model_validate_json(fixtures["finder_overview.json"])
    assert [entry.status for entry in overview.entries] == [
        "confirmed", "not_confirmed", "no_signal", "not_built", "not_built",
    ]  # fmt: skip
    assert overview.entries[0].matches == 1 and overview.last_run is not None
    assert [scenario.id for scenario in overview.proposed] == [7] and overview.discarded == 1
    waiting = FinderOverview.model_validate_json(fixtures["finder_overview_waiting.json"])
    assert len(waiting.proposed) == 8 and waiting.entries == () and waiting.proposed[0].assumptions
    empty = FinderOverview.model_validate_json(fixtures["finder_overview_empty.json"])
    assert (empty.entries, empty.proposed, empty.last_run) == ((), (), None)
    nothing = FinderOverview.model_validate_json(fixtures["finder_overview_none_confirmed.json"])
    assert nothing.entries and all(entry.status != "confirmed" for entry in nothing.entries)

    entries = {
        name: BankEntry.model_validate_json(text)
        for name, text in fixtures.items()
        if name.startswith("finder_entry")
    }
    confirmed = entries["finder_entry_confirmed.json"]
    assert confirmed.rule is not None and confirmed.backtest_sql and confirmed.monthly_sql
    assert confirmed.scenario_assumptions and confirmed.rule_assumptions and confirmed.matches
    assert confirmed.caveats and all(check.passed for check in confirmed.criteria)
    assert confirmed.decision == "pending" and confirmed.decision_note is None
    approved = entries["finder_entry_approved.json"]
    assert approved.decision == "approved" and approved.decision_note and approved.decided_at
    assert {entry.turn_outcome for entry in entries.values()} == {
        "rule_proposed", "clarification", "rejected", None,
    }  # fmt: skip
    assert entries["finder_entry_rejected.json"].refused == ("label_read", "label_read")
    assert entries["finder_entry_clarification.json"].turn_message
    assert entries["finder_entry_proposed.json"].status == "proposed"
    assert entries["finder_entry_discarded.json"].status == "discarded"
    nothing_flagged = entries["finder_entry_flags_nothing.json"]
    assert nothing_flagged.rule is not None and nothing_flagged.rule.flagged == 0
    assert "\n" not in (nothing_flagged.clause or "\n") and len(nothing_flagged.clause or "") > 300
    assert "no row of cards has card_on_dark_web = 1" in nothing_flagged.verdict
    assert all(entry.verdict and entry.verdict_short for entry in entries.values())

    proposed = FinderRun.model_validate_json(fixtures["finder_proposed.json"])
    assert (proposed.kind, proposed.status, proposed.scenarios, proposed.model_requests) == (
        "propose", "completed", 8, 1,
    )  # fmt: skip
    failed = FinderRun.model_validate_json(fixtures["finder_proposed_failed.json"])
    assert failed.status == "failed" and failed.error is not None
    exhausted = FinderRun.model_validate_json(fixtures["finder_proposed_demo_exhausted.json"])
    assert exhausted.error is not None and exhausted.error.kind == "demo_exhausted"

    def frames(name: str) -> list[RunProgress]:
        return [RunProgress.model_validate(frame) for frame in json.loads(fixtures[name])]

    built = frames("finder_progress_confirmed.json")
    assert [frame.step for frame in built][0] == "write_rule" and built[-1].step is None
    assert {frame.step for frame in built} >= {"check_rule", "backtest", "duplicates", "verdict"}
    assert (built[-1].state, built[-1].outcome, built[-1].status) == ("completed", "added", "confirmed")
    assert built[-1].verdict == confirmed.verdict
    duplicate = frames("finder_progress_duplicate.json")[-1]
    assert (duplicate.outcome, duplicate.result_entry_id, duplicate.match_kind) == (
        "duplicate", 1, "same_transactions",
    )  # fmt: skip
    assert max(frame.attempt for frame in frames("finder_progress_rejected.json")) == 2
    assert frames("finder_progress_clarification.json")[-1].status == "not_built"
    silent = frames("finder_progress_failed.json")[-1]
    assert silent.state == "failed" and silent.error is not None
    for name in (
        "FinderOverview", "BankEntry", "FinderRun", "DecisionRequest", "EntrySummary", "Criterion",
        "RunProgress", "ProposedScenario", "ProposeRequest",
    ):  # fmt: skip
        assert f"export interface {name} {{" in typescript.render()


def test_the_finder_tab_moves_only_for_a_reader_who_has_not_asked_for_less_motion() -> None:
    css = (PROJECT_ROOT / "frontend" / "src" / "styles" / "finder.css").read_text(encoding="utf-8")
    start = css.index("@media (prefers-reduced-motion: no-preference) {")
    end = css.index("\n}\n", start) + 3
    assert "animation:" in css[start:end]
    outside = css[:start] + css[end:]
    assert "animation" not in outside and "transition" not in outside
