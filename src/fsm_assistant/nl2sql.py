"""Natural language to a typed action.

Responsibility: what the model is offered and what it may answer. The five
tools (docs/DECISIONS.md, D-16), their argument schemas and the descriptions
the model reads; the single flat `Intent` object of the fallback mode; and
`check_shape`, which turns a raw `ModelReply` into exactly one typed `Action`
or a `ShapeFailure` with a reason the model can be shown for its one repair.

An action is still untrusted: `check_shape` settles which of the five things
was asked for, not whether the SQL may run. That is `sql_guard`'s decision.

The system prompt and glossary are in `prompt.py`. `PROMPT_VERSION` labels
that text and the descriptions below together: change either, change it.
"""

import json
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from fsm_assistant.config import Settings
from fsm_assistant.llm import JsonObject, ModelReply, ModelSpec, ToolSpec
from fsm_assistant.sql_guard import normalise

PROMPT_VERSION = "turn-4"

MIN_SUGGESTIONS = 2
MAX_SUGGESTIONS = 3


def _trimmed(value: str) -> str:
    return value.strip()


def _non_empty(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("must not be empty")
    return text


def _statement(value: str) -> str:
    text = normalise(value)
    if not text:
        raise ValueError("must not be empty")
    return text


def _clause(value: str) -> str:
    text = _statement(value)
    if text.split(maxsplit=1)[0].lower() == "where":
        raise ValueError("must be the condition only, without the word WHERE")
    return text


def _items(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(text for text in (value.strip() for value in values) if text)


def _two_or_three(values: tuple[str, ...]) -> tuple[str, ...]:
    items = _items(values)
    if not MIN_SUGGESTIONS <= len(items) <= MAX_SUGGESTIONS:
        raise ValueError(f"give {MIN_SUGGESTIONS} or {MAX_SUGGESTIONS} suggestions, got {len(items)}")
    return items


# The API's strict schemas carry no length or range limits, so the rules for a
# value are applied here, when the arguments are validated.
_Explanation = Annotated[str, AfterValidator(_trimmed)]
_Assumptions = Annotated[tuple[str, ...], AfterValidator(_items)]


class RunSql(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sql: Annotated[str, AfterValidator(_statement)] = Field(
        description="One complete SQLite SELECT statement (it may start with WITH). "
        "The bare statement, without a Markdown code fence."
    )
    explanation: _Explanation = Field(
        description="One or two sentences saying what the query returns, in the user's words. "
        "It is shown to the user next to the result. State no figures: you have not seen "
        "the result."
    )
    assumptions: _Assumptions = Field(
        description="Each interpretation you made that the user did not state, one per item, "
        "for example the date range you took a relative date to mean. Empty if there are none."
    )


class ProposeRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    where_clause: Annotated[str, AfterValidator(_clause)] = Field(
        description="A SQLite boolean expression that is true for the transactions to flag, "
        "written without the word WHERE. It sees only the columns of the transactions table "
        "(bare column names, or transactions.<column>); reach any other table through a "
        "subquery. It must not read fraud_labels: a rule cannot use the labels it is scored "
        "against."
    )
    explanation: _Explanation = Field(
        description="One or two sentences saying which transactions the rule flags, in the "
        "user's words. State no figures: the application measures the rule."
    )
    assumptions: _Assumptions = Field(
        description="Each interpretation you made that the user did not state, one per item, "
        "for example the unit you took an amount to be in. Empty if there are none."
    )


class ProfileFraud(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AskClarification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: Annotated[str, AfterValidator(_non_empty)] = Field(
        description="One short question to the user."
    )
    suggestions: Annotated[tuple[str, ...], AfterValidator(_two_or_three)] = Field(
        description="Two or three concrete readings of the request that the user can pick "
        "from, each one answerable from the data."
    )


class Decline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Annotated[str, AfterValidator(_non_empty)] = Field(
        description="One or two sentences for the user saying why this cannot be done, and "
        "what they could ask instead when there is an obvious alternative."
    )


Action = RunSql | ProposeRule | ProfileFraud | AskClarification | Decline

IntentKind = Literal["sql", "rule", "profile", "clarify", "refuse"]


class Intent(BaseModel):
    """The fallback mode's reply: the same five actions as one flat object.

    Every field is required and the unused ones are null, because that is the
    shape a schema-constrained reply can guarantee. Which fields go with
    which `kind` is checked here, not by the schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: IntentKind = Field(
        description="sql: answer with a query. rule: propose a fraud rule. profile: ask for "
        "the built-in scan of where fraud concentrates. clarify: ask the user what they "
        "mean. refuse: say this cannot be done from this data."
    )
    sql: str | None = Field(
        description="For kind sql: one complete SQLite SELECT statement. Otherwise null."
    )
    where_clause: str | None = Field(
        description="For kind rule: a boolean expression over the transactions table, without "
        "the word WHERE, that does not read fraud_labels. Otherwise null."
    )
    explanation: str | None = Field(
        description="For kind sql or rule: one or two sentences saying what the query returns "
        "or which transactions the rule flags. State no figures. Otherwise null."
    )
    assumptions: tuple[str, ...] = Field(
        description="For kind sql or rule: each interpretation you made that the user did "
        "not state. Empty if there are none."
    )
    question: str | None = Field(
        description="For kind clarify: one short question to the user. Otherwise null."
    )
    suggestions: tuple[str, ...] = Field(
        description="For kind clarify: two or three concrete readings the user can pick from. "
        "Otherwise empty."
    )
    reason: str | None = Field(
        description="For kind refuse: one or two sentences saying why. Otherwise null."
    )

    @model_validator(mode="after")
    def _fields_match_kind(self) -> "Intent":
        if (self.kind == "sql") != bool(self.sql and self.sql.strip()):
            raise ValueError("sql must be given when kind is sql, and null otherwise")
        if (self.kind == "rule") != bool(self.where_clause and self.where_clause.strip()):
            raise ValueError("where_clause must be given when kind is rule, and null otherwise")
        if self.kind == "clarify" and self.question is None:
            raise ValueError("question must be given when kind is clarify")
        if self.kind == "refuse" and self.reason is None:
            raise ValueError("reason must be given when kind is refuse")
        return self

    def to_action(self) -> Action:
        """The typed action. Fields that do not belong to `kind` are dropped."""
        match self.kind:
            case "sql":
                return RunSql(
                    sql=self.sql or "",
                    explanation=self.explanation or "",
                    assumptions=self.assumptions,
                )
            case "rule":
                return ProposeRule(
                    where_clause=self.where_clause or "",
                    explanation=self.explanation or "",
                    assumptions=self.assumptions,
                )
            case "profile":
                return ProfileFraud()
            case "clarify":
                return AskClarification(
                    question=self.question or "", suggestions=self.suggestions
                )
            case "refuse":
                return Decline(reason=self.reason or "")


ShapeFailureKind = Literal[
    "no_tool_call",
    "multiple_tool_calls",
    "unknown_tool",
    "invalid_arguments",
    "invalid_json",
    "cross_field",
]


class ShapeFailure(BaseModel):
    """Why a reply is not one valid action. `reason` is written for the model's repair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ShapeFailureKind
    reason: str


_TOOLS: dict[str, tuple[type[Action], str]] = {
    "run_sql": (
        RunSql,
        "Answer the question with one SQLite SELECT statement. Use this when the question "
        "can be answered from the tables in the schema. The application checks the statement "
        "and runs it on a read-only connection. You are not shown the result and there is no "
        "second step, so the one statement must return everything the answer needs.",
    ),
    "propose_rule": (
        ProposeRule,
        "Propose a fraud rule. Use this when the user asks for a rule, or asks to turn a "
        "pattern into one, and has said which transactions it should catch. A rule is a "
        "condition that flags transactions. The application measures it against the fraud "
        "labels and shows the user the rule together with those measurements.",
    ),
    "profile_fraud": (
        ProfileFraud,
        "Ask the application to run its built-in scan of where labelled fraud concentrates, "
        "ranked by segment. Use this for an open question about where or how fraud happens "
        "when the user names no filter of their own. A question with a specific filter or "
        "segment is a run_sql question. It takes no arguments.",
    ),
    "ask_clarification": (
        AskClarification,
        "Ask the user what they mean, instead of guessing. Use this when the request depends "
        "on a term the data does not define (for example 'risky', 'suspicious' or 'high "
        "value'), or can reasonably be read in more than one way that would change the "
        "answer. This applies to rule requests as well as questions.",
    ),
    "decline": (
        Decline,
        "Say that this cannot be done. Use this when the request needs data that is not in "
        "the schema, asks to change or delete data (the database is read-only), asks for a "
        "rule that reads the fraud labels, or is not about this transaction data.",
    ),
}

_INTENT_DESCRIPTION = (
    "Your whole reply: exactly one action, named by kind, with the fields that belong to it."
)


ToolName = Literal["run_sql", "propose_rule", "profile_fraud", "ask_clarification", "decline"]

_TOOL_NAMES: dict[type[Action], ToolName] = {
    RunSql: "run_sql",
    ProposeRule: "propose_rule",
    ProfileFraud: "profile_fraud",
    AskClarification: "ask_clarification",
    Decline: "decline",
}


def tool_name(action: Action) -> ToolName:
    """The tool an action came from, whichever output mode the reply used."""
    return _TOOL_NAMES[type(action)]


def tool_specs() -> tuple[ToolSpec, ...]:
    """The five tools as offered to the model, in a fixed order."""
    return tuple(
        ToolSpec(name=name, description=description, input_schema=_strict_schema(arguments))
        for name, (arguments, description) in _TOOLS.items()
    )


def intent_schema() -> JsonObject:
    """The JSON schema of the fallback mode's single object."""
    return {"title": "Intent", "description": _INTENT_DESCRIPTION, **_strict_schema(Intent)}


def model_spec(settings: Settings) -> ModelSpec:
    """The request settings for `settings`, with the tools and schema defined here."""
    return ModelSpec(
        model=settings.anthropic_model,
        effort=settings.effort,
        max_tokens=settings.max_tokens,
        tool_mode=settings.tool_mode,
        tools=tool_specs(),
        intent_schema=intent_schema(),
        prompt_version=PROMPT_VERSION,
    )


def check_shape(reply: ModelReply) -> Action | ShapeFailure:
    """Turn a raw reply into exactly one typed action, or say why it is not one.

    Text is trimmed and one trailing `;` is dropped from a statement or
    clause, as the guard does. Nothing else is repaired.
    """
    if reply.tool_mode == "structured":
        return _from_json(reply.text)
    if not reply.tool_calls:
        return ShapeFailure(
            kind="no_tool_call",
            reason="The reply called no tool. Reply by calling exactly one of: "
            f"{', '.join(_TOOLS)}.",
        )
    if len(reply.tool_calls) > 1:
        names = ", ".join(call.name for call in reply.tool_calls)
        return ShapeFailure(
            kind="multiple_tool_calls",
            reason=f"The reply called {len(reply.tool_calls)} tools ({names}). "
            "Call exactly one.",
        )
    call = reply.tool_calls[0]
    if call.name not in _TOOLS:
        return ShapeFailure(
            kind="unknown_tool",
            reason=f"There is no tool named {call.name!r}. The tools are: {', '.join(_TOOLS)}.",
        )
    arguments, _ = _TOOLS[call.name]
    try:
        return arguments.model_validate(call.arguments)
    except ValidationError as error:
        return ShapeFailure(
            kind="invalid_arguments",
            reason=f"The arguments of {call.name} are not valid: {_problems(error)}.",
        )


def describe_reply(reply: ModelReply) -> str:
    """A reply as text, to quote back to the model when its shape was not accepted."""
    calls = "; ".join(
        f"{call.name}({json.dumps(call.arguments, ensure_ascii=False)})"
        for call in reply.tool_calls
    )
    text = reply.text.strip()
    return " ".join(part for part in (calls, text) if part) or "(an empty reply)"


def _from_json(text: str) -> Action | ShapeFailure:
    try:
        data: JsonValue = json.loads(text)
    except ValueError:
        return ShapeFailure(
            kind="invalid_json", reason="The reply is not one JSON object matching the schema."
        )
    try:
        intent = Intent.model_validate(data)
    except ValidationError as error:
        cross_field = all(not problem["loc"] for problem in error.errors())
        return ShapeFailure(
            kind="cross_field" if cross_field else "invalid_arguments",
            reason=f"The reply does not match the schema: {_problems(error)}.",
        )
    try:
        return intent.to_action()
    except ValidationError as error:
        return ShapeFailure(
            kind="cross_field",
            reason=f"The fields do not fit kind {intent.kind!r}: {_problems(error)}.",
        )


def _problems(error: ValidationError) -> str:
    parts: list[str] = []
    for problem in error.errors():
        where = ".".join(str(step) for step in problem["loc"])
        message = problem["msg"].removeprefix("Value error, ")
        parts.append(f"{where}: {message}" if where else message)
    return "; ".join(parts)


_SCHEMA_NOISE = frozenset({"title"})


def _strict_schema(model: type[BaseModel]) -> JsonObject:
    """`model`'s JSON schema in the form the API's strict mode accepts.

    Closed (`additionalProperties: false`), every property required, and
    without the titles pydantic adds.
    """
    schema = _without_noise(model.model_json_schema())
    assert isinstance(schema, dict)
    schema.pop("description", None)
    schema.setdefault("properties", {})
    schema["required"] = list(model.model_fields)
    schema["additionalProperties"] = False
    return schema


def _without_noise(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: (
                {name: _without_noise(child) for name, child in item.items()}
                if key == "properties" and isinstance(item, dict)
                else _without_noise(item)
            )
            for key, item in value.items()
            if key not in _SCHEMA_NOISE
        }
    if isinstance(value, list):
        return [_without_noise(item) for item in value]
    return value
