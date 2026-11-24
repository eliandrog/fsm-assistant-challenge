"""nl2sql: the tool and intent schemas, and the shape check on a raw reply."""

import json

import pytest
from pydantic import JsonValue, ValidationError

from fsm_assistant import nl2sql
from fsm_assistant.config import ToolMode, load_settings
from fsm_assistant.llm import JsonObject, ModelReply, TokenUsage, ToolCall
from fsm_assistant.nl2sql import (
    Action,
    AskClarification,
    Decline,
    Intent,
    ProfileFraud,
    ProposeRule,
    RunSql,
    ShapeFailure,
    check_shape,
)

# What the API's strict schemas accept and these schemas use. Length and range
# limits (minLength, maxItems, minimum, pattern, ...) are not in the list.
ALLOWED_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "anyOf",
    "description",
}


def reply(
    *calls: tuple[str, JsonObject], text: str = "", tool_mode: ToolMode = "tools"
) -> ModelReply:
    return ModelReply(
        tool_mode=tool_mode,
        tool_calls=tuple(ToolCall(name=name, arguments=arguments) for name, arguments in calls),
        text=text,
        stop_reason="tool_use" if calls else "end_turn",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
        model_id="scripted",
        response_id=None,
        request_hash="h",
    )


def intent_reply(**fields: JsonValue) -> ModelReply:
    full: JsonObject = {
        "kind": "sql",
        "sql": None,
        "where_clause": None,
        "explanation": None,
        "assumptions": [],
        "question": None,
        "suggestions": [],
        "reason": None,
        **fields,
    }
    return reply(text=json.dumps(full), tool_mode="structured")


def keywords(schema: JsonValue, *, inside_properties: bool = False) -> set[str]:
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if not inside_properties:
                found.add(key)
            found |= keywords(value, inside_properties=key == "properties" and not inside_properties)
    elif isinstance(schema, list):
        for item in schema:
            found |= keywords(item)
    return found


def failure(result: Action | ShapeFailure) -> ShapeFailure:
    assert isinstance(result, ShapeFailure), result
    return result


def test_the_five_tools_in_order() -> None:
    specs = nl2sql.tool_specs()
    assert [spec.name for spec in specs] == [
        "run_sql",
        "propose_rule",
        "profile_fraud",
        "ask_clarification",
        "decline",
    ]
    assert {spec.name: spec.input_schema["required"] for spec in specs} == {
        "run_sql": ["sql", "explanation", "assumptions"],
        "propose_rule": ["where_clause", "explanation", "assumptions"],
        "profile_fraud": [],
        "ask_clarification": ["question", "suggestions"],
        "decline": ["reason"],
    }


def test_every_tool_schema_is_strict() -> None:
    for spec in nl2sql.tool_specs():
        schema = spec.input_schema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        properties = schema["properties"]
        assert isinstance(properties, dict)
        assert list(properties) == schema["required"]
        assert keywords(schema) <= ALLOWED_KEYWORDS, spec.name
        assert len(spec.description) > 80
        for name, field in properties.items():
            assert isinstance(field, dict)
            assert field.get("description"), f"{spec.name}.{name} has no description"


def test_run_sql_schema_snapshot() -> None:
    schema = nl2sql.tool_specs()[0].input_schema
    properties = schema["properties"]
    assert isinstance(properties, dict)
    stripped = {
        name: {key: value for key, value in field.items() if key != "description"}
        for name, field in properties.items()
        if isinstance(field, dict)
    }
    assert stripped == {
        "sql": {"type": "string"},
        "explanation": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    }
    explanation = properties["explanation"]
    assert isinstance(explanation, dict)
    assert "what the query returns" in str(explanation["description"])


def test_profile_fraud_takes_no_arguments() -> None:
    assert nl2sql.tool_specs()[2].input_schema == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_the_intent_schema_is_flat_strict_and_nullable() -> None:
    schema = nl2sql.intent_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "kind",
        "sql",
        "where_clause",
        "explanation",
        "assumptions",
        "question",
        "suggestions",
        "reason",
    ]
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == schema["required"]
    kind = properties["kind"]
    assert isinstance(kind, dict)
    assert kind["enum"] == ["sql", "rule", "profile", "clarify", "refuse"]
    sql = properties["sql"]
    assert isinstance(sql, dict)
    assert sql["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert keywords(schema) <= ALLOWED_KEYWORDS | {"title"}
    assert "$defs" not in schema and "$ref" not in json.dumps(schema)


def test_model_spec_follows_the_settings() -> None:
    settings = load_settings({"ANTHROPIC_MODEL": "claude-haiku-5-5", "FSM_EFFORT": "low"}, None)
    spec = nl2sql.model_spec(settings)
    assert (spec.model, spec.effort, spec.max_tokens, spec.tool_mode) == (
        "claude-haiku-5-5",
        "low",
        8000,
        "tools",
    )
    assert spec.tools == nl2sql.tool_specs()
    assert spec.intent_schema == nl2sql.intent_schema()
    assert spec.prompt_version == nl2sql.PROMPT_VERSION


def test_each_tool_becomes_its_action() -> None:
    assert check_shape(
        reply(("run_sql", {"sql": "SELECT 1", "explanation": "One.", "assumptions": ["a"]}))
    ) == RunSql(sql="SELECT 1", explanation="One.", assumptions=("a",))
    assert check_shape(
        reply(
            (
                "propose_rule",
                {"where_clause": "amount_usd_cents > 100000", "explanation": "", "assumptions": []},
            )
        )
    ) == ProposeRule(where_clause="amount_usd_cents > 100000", explanation="", assumptions=())
    assert check_shape(reply(("profile_fraud", {}))) == ProfileFraud()
    assert check_shape(
        reply(("ask_clarification", {"question": "Which?", "suggestions": ["a", "b"]}))
    ) == AskClarification(question="Which?", suggestions=("a", "b"))
    assert check_shape(reply(("decline", {"reason": "No such column."}))) == Decline(
        reason="No such column."
    )


def test_text_is_trimmed_and_one_trailing_semicolon_dropped() -> None:
    action = check_shape(
        reply(
            (
                "run_sql",
                {
                    "sql": "  SELECT count(*) FROM transactions ;\n",
                    "explanation": "  The count. ",
                    "assumptions": [" none stated ", "  "],
                },
            )
        )
    )
    assert action == RunSql(
        sql="SELECT count(*) FROM transactions",
        explanation="The count.",
        assumptions=("none stated",),
    )
    rule = check_shape(
        reply(("propose_rule", {"where_clause": " id > 5; ", "explanation": "", "assumptions": []}))
    )
    assert isinstance(rule, ProposeRule) and rule.where_clause == "id > 5"
    two = check_shape(reply(("run_sql", {"sql": "SELECT 1;;", "explanation": "", "assumptions": []})))
    assert isinstance(two, RunSql) and two.sql == "SELECT 1;"


def test_a_fenced_statement_is_not_unwrapped() -> None:
    fenced = "```sql\nSELECT 1\n```"
    action = check_shape(reply(("run_sql", {"sql": fenced, "explanation": "", "assumptions": []})))
    assert isinstance(action, RunSql) and action.sql == fenced


def test_no_tool_call_is_a_failure() -> None:
    result = failure(check_shape(reply(text="There are 1,159,966 transactions.")))
    assert result.kind == "no_tool_call"
    assert "exactly one of: run_sql, propose_rule, profile_fraud" in result.reason


def test_two_tool_calls_are_a_failure() -> None:
    result = failure(check_shape(reply(("profile_fraud", {}), ("decline", {"reason": "x"}))))
    assert result.kind == "multiple_tool_calls"
    assert "2 tools (profile_fraud, decline)" in result.reason


def test_an_unknown_tool_is_a_failure() -> None:
    result = failure(check_shape(reply(("drop_table", {"table": "cards"}))))
    assert result.kind == "unknown_tool"
    assert "'drop_table'" in result.reason and "run_sql" in result.reason


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("run_sql", {"sql": " ; ", "explanation": "", "assumptions": []}, "sql: must not be empty"),
        ("run_sql", {"sql": "SELECT 1"}, "explanation: Field required"),
        (
            "run_sql",
            {"sql": "SELECT 1", "explanation": "", "assumptions": [], "limit": 5},
            "limit: Extra inputs are not permitted",
        ),
        ("run_sql", {"sql": 5, "explanation": "", "assumptions": []}, "sql: Input should be"),
        (
            "propose_rule",
            {"where_clause": "WHERE id > 5", "explanation": "", "assumptions": []},
            "where_clause: must be the condition only, without the word WHERE",
        ),
        ("profile_fraud", {"segment": "city"}, "segment: Extra inputs are not permitted"),
        (
            "ask_clarification",
            {"question": "Which?", "suggestions": ["only one"]},
            "suggestions: give 2 or 3 suggestions, got 1",
        ),
        (
            "ask_clarification",
            {"question": "Which?", "suggestions": ["a", "b", "c", "d"]},
            "suggestions: give 2 or 3 suggestions, got 4",
        ),
        (
            "ask_clarification",
            {"question": "  ", "suggestions": ["a", "b"]},
            "question: must not be empty",
        ),
        ("decline", {"reason": ""}, "reason: must not be empty"),
    ],
)
def test_invalid_arguments_are_a_failure(name: str, arguments: JsonObject, expected: str) -> None:
    result = failure(check_shape(reply((name, arguments))))
    assert result.kind == "invalid_arguments"
    assert result.reason.startswith(f"The arguments of {name} are not valid: ")
    assert expected in result.reason


def test_each_intent_kind_becomes_the_same_action_as_its_tool() -> None:
    assert check_shape(
        intent_reply(kind="sql", sql="SELECT 1;", explanation=" One. ", assumptions=["a"])
    ) == RunSql(sql="SELECT 1", explanation="One.", assumptions=("a",))
    assert check_shape(
        intent_reply(kind="rule", where_clause="id > 5", explanation=None)
    ) == ProposeRule(where_clause="id > 5", explanation="", assumptions=())
    assert check_shape(intent_reply(kind="profile")) == ProfileFraud()
    assert check_shape(
        intent_reply(kind="clarify", question="Which?", suggestions=["a", "b", "c"])
    ) == AskClarification(question="Which?", suggestions=("a", "b", "c"))
    assert check_shape(intent_reply(kind="refuse", reason="No.")) == Decline(reason="No.")


def test_stray_fields_of_another_kind_are_dropped() -> None:
    assert check_shape(
        intent_reply(kind="refuse", reason="No.", suggestions=["a"], question="?", explanation="x")
    ) == Decline(reason="No.")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"kind": "sql"}, "sql must be given when kind is sql"),
        ({"kind": "sql", "sql": "  "}, "sql must be given when kind is sql"),
        ({"kind": "refuse", "reason": "No.", "sql": "SELECT 1"}, "and null otherwise"),
        ({"kind": "rule"}, "where_clause must be given when kind is rule"),
        ({"kind": "sql", "sql": "SELECT 1", "where_clause": "id > 5"}, "where_clause must be"),
        ({"kind": "clarify", "suggestions": ["a", "b"]}, "question must be given"),
        ({"kind": "refuse"}, "reason must be given"),
        ({"kind": "clarify", "question": "Which?", "suggestions": ["a"]}, "give 2 or 3"),
        ({"kind": "rule", "where_clause": "where id > 5"}, "without the word WHERE"),
        ({"kind": "refuse", "reason": "  "}, "reason: must not be empty"),
    ],
)
def test_cross_field_violations_are_a_failure(fields: JsonObject, expected: str) -> None:
    result = failure(check_shape(intent_reply(**fields)))
    assert result.kind == "cross_field"
    assert expected in result.reason


def test_structured_text_that_is_not_the_object_is_a_failure() -> None:
    assert failure(check_shape(reply(text="SELECT 1", tool_mode="structured"))).kind == "invalid_json"
    assert failure(check_shape(reply(text="", tool_mode="structured"))).kind == "invalid_json"
    wrong_type = failure(check_shape(intent_reply(kind="explain")))
    assert wrong_type.kind == "invalid_arguments" and "kind: Input should be" in wrong_type.reason
    missing = failure(check_shape(reply(text='{"kind": "profile"}', tool_mode="structured")))
    assert missing.kind == "invalid_arguments" and "sql: Field required" in missing.reason


def test_tool_calls_are_ignored_in_structured_mode_and_text_in_tool_mode() -> None:
    both = reply(("profile_fraud", {}), text='{"kind": "x"}', tool_mode="tools")
    assert check_shape(both) == ProfileFraud()


def test_actions_and_intent_are_frozen_and_closed() -> None:
    action = RunSql(sql="SELECT 1", explanation="", assumptions=())
    with pytest.raises(ValidationError):
        action.sql = "SELECT 2"  # type: ignore[misc]  # the assignment is the thing under test
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Intent.model_validate({**json.loads(intent_reply(kind="profile").text), "extra": 1})
