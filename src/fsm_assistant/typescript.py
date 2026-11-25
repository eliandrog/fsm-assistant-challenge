"""TypeScript declarations of the API's bodies.

Responsibility: write the types the web interface reads from the JSON schema
of the pydantic models the API returns, so the two cannot drift
(docs/DECISIONS.md, D-31). `render` returns the text of
`frontend/src/api/types.gen.ts`; a test fails when the committed file differs
from it. Run `uv run python -m fsm_assistant.typescript` to write the file.

It reads the part of JSON schema that pydantic writes for these models:
objects, references, unions with null, arrays, string enumerations and the
scalar types. Anything else is an error, not a guess.
"""

from collections.abc import Iterable
from typing import Final

from pydantic import BaseModel, JsonValue
from pydantic.json_schema import JsonSchemaMode, models_json_schema

from fsm_assistant.api import (
    ApiError,
    ChatRequest,
    DecisionRequest,
    FinderOverview,
    Health,
    ProposeRequest,
)
from fsm_assistant.finder import RunProgress
from fsm_assistant.config import PROJECT_ROOT
from fsm_assistant.result import TurnResult
from fsm_assistant.rule_bank import BankEntry, FinderRun

TYPES_FILE: Final = PROJECT_ROOT / "frontend" / "src" / "api" / "types.gen.ts"
# A body the server writes holds every field; one it reads may omit those with a default.
MODELS: Final[tuple[tuple[type[BaseModel], JsonSchemaMode], ...]] = (
    (ChatRequest, "validation"),
    (TurnResult, "serialization"),
    (Health, "serialization"),
    (ApiError, "serialization"),
    (DecisionRequest, "validation"),
    (ProposeRequest, "validation"),
    (RunProgress, "serialization"),
    (FinderOverview, "serialization"),
    (BankEntry, "serialization"),
    (FinderRun, "serialization"),
)
_HEADER: Final = (
    "// Generated from the API's pydantic models by src/fsm_assistant/typescript.py.\n"
    "// Do not edit: run `uv run python -m fsm_assistant.typescript`.\n"
)
_SCALARS: Final = {"string": "string", "integer": "number", "number": "number", "boolean": "boolean", "null": "null"}  # fmt: skip

type Schema = dict[str, JsonValue]


def render() -> str:
    """The declarations of `MODELS` and of every model they contain, in name order."""
    read = {model.__name__ for model, mode in MODELS if mode == "validation"}
    _, combined = models_json_schema(list(MODELS), ref_template="{model}")
    definitions = combined["$defs"]
    blocks = [
        _interface(name, schema, optional_defaults=name in read)
        if schema.get("type") == "object"
        else f"export type {name} = {_type(schema)};"
        for name, schema in sorted(definitions.items())
    ]
    return _HEADER + "\n" + "\n\n".join(blocks) + "\n"


def _interface(name: str, schema: Schema, *, optional_defaults: bool) -> str:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError(f"{name} is not an object schema")
    required = schema.get("required")
    always = set(required) if isinstance(required, list) else set()
    lines = [f"export interface {name} {{"]
    for field, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            raise ValueError(f"{name}.{field} has no schema")
        optional = "?" if optional_defaults and field not in always else ""
        lines.append(f"  {field}{optional}: {_type(field_schema)};")
    return "\n".join([*lines, "}"])


def _type(schema: Schema) -> str:
    reference, options, values = schema.get("$ref"), schema.get("anyOf"), schema.get("enum")
    if isinstance(reference, str):
        return reference
    if isinstance(options, list):
        return _union(_type(option) for option in options if isinstance(option, dict))
    if isinstance(values, list):
        return _union(_literal(value) for value in values)
    if "const" in schema:
        return _literal(schema["const"])
    kind = schema.get("type")
    if kind == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            raise ValueError(f"an array without one item schema: {schema}")
        inner = _type(items)
        return f"({inner})[]" if " | " in inner else f"{inner}[]"
    if isinstance(kind, str) and kind in _SCALARS:
        return _SCALARS[kind]
    raise ValueError(f"a schema this generator does not read: {schema}")


def _union(members: Iterable[str]) -> str:
    return " | ".join(dict.fromkeys(members))


def _literal(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ValueError(f"only string literals are read, got {value!r}")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


if __name__ == "__main__":
    TYPES_FILE.write_text(render(), encoding="utf-8")
    print(f"Wrote {TYPES_FILE.relative_to(PROJECT_ROOT)}.")
