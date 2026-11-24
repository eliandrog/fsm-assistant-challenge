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
from conftest import RecordingSink, call_reply, rule_reply, sql_reply
from fastapi.testclient import TestClient
from scan_fixtures import MAIN, SACRAMENTO, Group, build

from fsm_assistant import demo, typescript
from fsm_assistant.api import create_app
from fsm_assistant.config import PROJECT_ROOT, Settings, load_settings
from fsm_assistant.events import Label
from fsm_assistant.graph import Assistant
from fsm_assistant.llm import ModelError, ModelReply, ModelUnavailable, ScriptedModel
from fsm_assistant.result import TurnResult
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
        if not name.startswith(("health", "api_error"))
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
