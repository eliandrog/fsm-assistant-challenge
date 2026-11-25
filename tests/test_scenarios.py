"""scenarios: a request made of the schema context and a count, and a reply read strictly."""

import json
from pathlib import Path

from conftest import call_reply, tool_reply
from finder_fixtures import scenario, scenarios_reply

from fsm_assistant import nl2sql, scenarios
from fsm_assistant.config import load_settings
from fsm_assistant.llm import request_hash
from fsm_assistant.prompt import system_text
from fsm_assistant.scenarios import (
    SCENARIO_PROMPT_VERSION,
    Scenario,
    ScenarioFailure,
    build_request,
    read_reply,
    scenario_spec,
)
from fsm_assistant.schema_context import build_schema_context


def test_the_request_is_the_fixed_text_the_schema_context_and_a_count(fixture_db: Path) -> None:
    context = build_schema_context(fixture_db)
    request = build_request(context, 7)
    assert request.system == scenarios.system_text(context)
    assert request.system.endswith(context.strip() + "\n") and "fraud_labels" in request.system
    assert request.user == "Propose exactly 7 fraud scenarios."
    # Values that exist only in rows of the fixture database.
    for row_value in ("150000", "-5000", "2019-02-10", "08:15:00"):
        assert row_value not in request.system + request.user


def test_it_is_a_separate_request_that_leaves_the_turn_request_alone(fixture_db: Path) -> None:
    settings = load_settings({"FSM_DB_PATH": str(fixture_db)}, env_file=None)
    spec, turn = scenario_spec(settings), nl2sql.model_spec(settings)
    assert spec.prompt_version == SCENARIO_PROMPT_VERSION != nl2sql.PROMPT_VERSION == "turn-5"
    assert [tool.name for tool in spec.tools] == ["propose_scenarios"]
    assert [tool.name for tool in turn.tools] == [
        "run_sql", "propose_rule", "profile_fraud", "ask_clarification", "decline",
    ]  # fmt: skip
    assert (spec.model, spec.effort, spec.max_tokens, spec.tool_mode) == (
        turn.model, turn.effort, turn.max_tokens, turn.tool_mode,
    )  # fmt: skip
    context = build_schema_context(fixture_db)
    assert scenarios.system_text(context) != system_text(context)
    request = build_request(context, 3)
    assert request_hash(spec, request) != request_hash(turn, request)


def test_the_tool_schema_is_strict_all_the_way_down() -> None:
    def closed(schema: object) -> bool:
        if isinstance(schema, dict):
            if schema.get("type") == "object":
                properties = schema.get("properties")
                assert isinstance(properties, dict)
                if schema.get("additionalProperties") is not False:
                    return False
                if sorted(schema.get("required", [])) != sorted(properties):
                    return False
            return all(closed(value) for value in schema.values())
        return all(closed(item) for item in schema) if isinstance(schema, list) else True

    schema = scenarios.tool_spec().input_schema
    assert closed(schema), json.dumps(schema)
    assert scenarios.batch_schema()["title"] == "ScenarioBatch"


def test_a_reply_is_read_into_trimmed_scenarios_and_cut_to_the_count_asked_for() -> None:
    found = read_reply(scenarios_reply("one", "two", "three"), 2)
    assert isinstance(found, tuple) and [item.title for item in found] == ["one", "two"]
    padded = call_reply(
        "propose_scenarios",
        scenarios=[
            {"title": "  Card \n testing ", "hypothesis": " Many  small payments. ",
             "columns": [" transactions.amount_usd_cents ", " "], "assumptions": ["", " Small is under $2. "]}
        ],
    )  # fmt: skip
    assert read_reply(padded, 5) == (
        Scenario(
            title="Card testing",
            hypothesis="Many small payments.",
            columns=("transactions.amount_usd_cents",),
            assumptions=("Small is under $2.",),
        ),
    )


def test_the_structured_mode_reads_one_json_object() -> None:
    reply = tool_reply().model_copy(
        update={"tool_mode": "structured", "text": json.dumps({"scenarios": [scenario("one")]})}
    )
    found = read_reply(reply, 5)
    assert isinstance(found, tuple) and found[0].title == "one"
    broken = read_reply(reply.model_copy(update={"text": "not json"}), 5)
    assert isinstance(broken, ScenarioFailure) and broken.kind == "invalid_json"


def test_every_other_shape_is_a_failure_that_quotes_nothing_the_model_wrote() -> None:
    secret = "text-the-model-wrote"
    cases = {
        "no_tool_call": tool_reply(),
        "multiple_tool_calls": tool_reply("run_sql", "decline"),
        "unknown_tool": tool_reply("run_sql"),
        "invalid_arguments": call_reply(
            "propose_scenarios", scenarios=[{"title": secret, "hypothesis": " ", "columns": [], "assumptions": []}]
        ),
        "empty": call_reply("propose_scenarios", scenarios=[]),
    }  # fmt: skip
    for kind, reply in cases.items():
        failure = read_reply(reply, 5)
        assert isinstance(failure, ScenarioFailure) and failure.kind == kind, kind
        assert secret not in failure.reason
