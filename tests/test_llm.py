"""llm: request hashing, the Claude client's settings, error mapping, record and replay.

Nothing here sends a request. The live client is exercised by replacing the
one method of LangChain's wrapper that performs the HTTP call
(`ChatAnthropic._create`), so everything around it runs as it would for real:
building the payload, classifying an error, reading the provider's message.
"""

import json
import os
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from langchain_anthropic import ChatAnthropic
from langsmith import utils as langsmith_utils
from pydantic import JsonValue

from fsm_assistant import llm, nl2sql
from fsm_assistant.config import Settings, ToolMode, load_settings
from fsm_assistant.llm import (
    ClaudeModel,
    ModelOutputError,
    ModelReply,
    ModelRequest,
    ModelSpec,
    ModelUnavailable,
    Recording,
    RecordingModel,
    ReplayModel,
    ScriptedModel,
    ScriptExhausted,
    StaleRecording,
    TokenUsage,
    ToolCall,
    UnavailableKind,
    request_hash,
)

SECRET = "not-a-real-key-0123456789"
REQUEST = ModelRequest(system="You answer questions.", user="How many transactions are there?")
HTTP_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

# A payload is the wrapper's own untyped dict, and `_create` returns the SDK's raw response.
Payload = dict[str, Any]


def settings(**environ: str) -> Settings:
    return load_settings({"ANTHROPIC_API_KEY": SECRET, **environ}, None)


def spec(tool_mode: ToolMode = "tools", **environ: str) -> ModelSpec:
    return nl2sql.model_spec(settings(FSM_TOOL_MODE=tool_mode, **environ))


def message(content: list[dict[str, JsonValue]], stop_reason: str = "tool_use") -> Message:
    return Message.model_validate(
        {
            "id": "msg_test_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 85},
        }
    )


TOOL_USE: list[dict[str, JsonValue]] = [
    {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "run_sql",
        "input": {"sql": "SELECT count(*) FROM transactions", "explanation": "", "assumptions": []},
    }
]


class FakeResponse:
    def __init__(self, parsed: Message) -> None:
        self._parsed = parsed

    def parse(self) -> Message:
        return self._parsed


class Provider:
    """Stands in for the HTTP call: records each payload, then replies or raises."""

    def __init__(self, outcome: Message | Exception) -> None:
        self.outcome = outcome
        self.payloads: list[Payload] = []
        self.tracing_seen: list[object] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "Provider":
        def create(chat: ChatAnthropic, payload: Payload) -> FakeResponse:
            self.payloads.append(payload)
            self.tracing_seen.append(langsmith_utils.tracing_is_enabled())
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return FakeResponse(self.outcome)

        monkeypatch.setattr(ChatAnthropic, "_create", create)
        return self


def clear_langsmith_cache() -> None:
    """LangSmith caches what it read from the environment; forget it."""
    cached: Any = langsmith_utils.get_env_var  # an lru_cache behind overloads, which hide cache_clear
    cached.cache_clear()


def live(model_spec: ModelSpec) -> ClaudeModel:
    return ClaudeModel.from_settings(model_spec, settings())


def scripted_reply(text: str = "", stop_reason: str = "tool_use") -> ModelReply:
    return ModelReply(
        tool_mode="tools",
        tool_calls=(ToolCall(name="profile_fraud", arguments={}),),
        text=text,
        stop_reason=stop_reason,
        usage=TokenUsage(input_tokens=3, output_tokens=2),
        model_id="scripted",
        response_id="msg_scripted",
        request_hash=request_hash(spec(), REQUEST),
    )


# --- request hash


def test_the_hash_is_stable_and_covers_every_input() -> None:
    base = spec()
    assert request_hash(base, REQUEST) == request_hash(spec(), REQUEST)
    assert len(request_hash(base, REQUEST)) == 64
    tool = base.tools[0]
    changed_tool = tool.model_copy(update={"description": tool.description + " More."})
    variants = [
        (base.model_copy(update={"model": "claude-opus-5-5"}), REQUEST),
        (base.model_copy(update={"effort": "high"}), REQUEST),
        (base.model_copy(update={"max_tokens": 4000}), REQUEST),
        (base.model_copy(update={"tool_mode": "structured"}), REQUEST),
        (base.model_copy(update={"prompt_version": "other"}), REQUEST),
        (base.model_copy(update={"tools": (changed_tool, *base.tools[1:])}), REQUEST),
        (base, REQUEST.model_copy(update={"system": REQUEST.system + " "})),
        (base, REQUEST.model_copy(update={"user": "How many cards are there?"})),
    ]
    hashes = {request_hash(variant, request) for variant, request in variants}
    assert len(hashes) == len(variants)
    assert request_hash(base, REQUEST) not in hashes


def test_the_hash_ignores_the_position_in_the_conversation_and_the_unused_schema() -> None:
    base = spec()
    assert request_hash(base, REQUEST.model_copy(update={"turn": 3, "call": 2})) == request_hash(
        base, REQUEST
    )
    other_intent = base.model_copy(update={"intent_schema": {"type": "object"}})
    assert request_hash(other_intent, REQUEST) == request_hash(base, REQUEST)
    structured = spec("structured")
    assert request_hash(
        structured.model_copy(update={"tools": ()}), REQUEST
    ) == request_hash(structured, REQUEST)
    assert request_hash(
        structured.model_copy(update={"intent_schema": {"type": "object"}}), REQUEST
    ) != request_hash(structured, REQUEST)


# --- the Claude client


def test_a_missing_key_fails_only_when_a_live_model_is_built() -> None:
    without_key = load_settings({}, None)
    model_spec = nl2sql.model_spec(without_key)
    with pytest.raises(ModelUnavailable, match="ANTHROPIC_API_KEY is not set") as error:
        ClaudeModel.from_settings(model_spec, without_key)
    assert error.value.kind == "missing_key"


def test_the_client_is_built_with_our_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL", "LANGSMITH_GATEWAY"):
        monkeypatch.setenv(name, "https://elsewhere.example")
    chat = llm.build_chat_model(spec(), settings(FSM_MODEL_TIMEOUT_SECONDS="45"))
    assert chat.model == "claude-haiku-5-5"
    assert chat.max_tokens == 8000
    assert chat.max_retries == 0
    assert chat.default_request_timeout == 45.0
    assert chat.temperature is None and chat.top_p is None and chat.top_k is None
    assert chat.thinking is None
    assert chat.output_config == {"effort": "medium"}
    assert chat.anthropic_api_url == "https://api.anthropic.com"
    assert chat.anthropic_api_key.get_secret_value() == SECRET
    assert SECRET not in repr(chat)
    assert chat._client_params["max_retries"] == 0
    assert chat._client_params["base_url"] == "https://api.anthropic.com"
    assert chat._client_params["timeout"] == 45.0


def test_tool_mode_sends_strict_tools_one_call_at_a_time_and_no_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Provider(message(TOOL_USE)).install(monkeypatch)
    live(spec()).complete(REQUEST)
    (payload,) = provider.payloads
    assert payload["model"] == "claude-haiku-5-5"
    assert payload["max_tokens"] == 8000
    assert payload["system"] == REQUEST.system
    assert payload["messages"] == [{"role": "user", "content": REQUEST.user}]
    assert payload["output_config"] == {"effort": "medium"}
    assert payload["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert [tool["name"] for tool in payload["tools"]] == [
        "run_sql",
        "propose_rule",
        "profile_fraud",
        "ask_clarification",
        "decline",
    ]
    assert all(tool["strict"] is True for tool in payload["tools"])
    assert [tool["input_schema"] for tool in payload["tools"]] == [
        tool.input_schema for tool in nl2sql.tool_specs()
    ]
    assert not {"temperature", "top_p", "top_k", "thinking", "betas", "fallbacks"} & payload.keys()


def test_structured_mode_sends_the_json_schema_format_and_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = json.dumps({"kind": "profile"})
    provider = Provider(message([{"type": "text", "text": text}], "end_turn")).install(monkeypatch)
    reply = live(spec("structured")).complete(REQUEST)
    (payload,) = provider.payloads
    output_format = payload["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["schema"]["required"] == nl2sql.intent_schema()["required"]
    assert output_format["schema"]["additionalProperties"] is False
    assert payload["output_config"]["effort"] == "medium"
    assert not {"tools", "tool_choice", "temperature", "thinking"} & payload.keys()
    assert reply.tool_mode == "structured"
    assert reply.text == text
    assert reply.tool_calls == ()


def test_a_tool_call_is_read_into_a_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    Provider(message(TOOL_USE)).install(monkeypatch)
    model_spec = spec()
    reply = live(model_spec).complete(REQUEST)
    assert reply == ModelReply(
        tool_mode="tools",
        tool_calls=(
            ToolCall(
                name="run_sql",
                arguments={
                    "sql": "SELECT count(*) FROM transactions",
                    "explanation": "",
                    "assumptions": [],
                },
            ),
        ),
        text="",
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=1200, output_tokens=85),
        model_id="claude-haiku-5-5",
        response_id="msg_test_1",
        request_hash=request_hash(model_spec, REQUEST),
    )


def test_text_and_two_tool_calls_come_back_unjudged(monkeypatch: pytest.MonkeyPatch) -> None:
    content: list[dict[str, JsonValue]] = [
        {"type": "text", "text": "I will run two things. "},
        {"type": "tool_use", "id": "toolu_1", "name": "profile_fraud", "input": {}},
        {"type": "tool_use", "id": "toolu_2", "name": "made_up", "input": {"x": 1}},
    ]
    Provider(message(content)).install(monkeypatch)
    reply = live(spec()).complete(REQUEST)
    assert [call.name for call in reply.tool_calls] == ["profile_fraud", "made_up"]
    assert reply.text == "I will run two things. "
    assert nl2sql.check_shape(reply) == nl2sql.ShapeFailure(
        kind="multiple_tool_calls",
        reason="The reply called 2 tools (profile_fraud, made_up). Call exactly one.",
    )


def test_plain_text_comes_back_with_no_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    Provider(message([{"type": "text", "text": "About a million."}], "end_turn")).install(
        monkeypatch
    )
    reply = live(spec()).complete(REQUEST)
    assert (reply.tool_calls, reply.text, reply.stop_reason) == ((), "About a million.", "end_turn")


@pytest.mark.parametrize(
    ("stop_reason", "kind"), [("refusal", "refusal"), ("max_tokens", "truncated")]
)
def test_a_refusal_or_a_truncated_reply_is_an_output_error(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str, kind: str
) -> None:
    Provider(message([{"type": "text", "text": "partial"}], stop_reason)).install(monkeypatch)
    with pytest.raises(ModelOutputError) as error:
        live(spec()).complete(REQUEST)
    assert error.value.kind == kind
    assert error.value.reply.stop_reason == stop_reason
    assert error.value.reply.usage.output_tokens == 85


def status_error(kind: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    response = httpx2.Response(status, request=HTTP_REQUEST)
    return kind(f"provider said {status}", response=response, body=None)


PROVIDER_ERRORS: list[tuple[Exception, UnavailableKind]] = [
    (status_error(anthropic.AuthenticationError, 401), "authentication"),
    (status_error(anthropic.PermissionDeniedError, 403), "permission"),
    (status_error(anthropic.NotFoundError, 404), "model_not_found"),
    (status_error(anthropic.RateLimitError, 429), "rate_limit"),
    (status_error(anthropic.InternalServerError, 500), "overloaded_or_server_error"),
    (status_error(anthropic.OverloadedError, 529), "overloaded_or_server_error"),
    (status_error(anthropic.BadRequestError, 400), "invalid_request"),
    (anthropic.APITimeoutError(HTTP_REQUEST), "timeout"),
    (anthropic.APIConnectionError(request=HTTP_REQUEST), "connection"),
    (status_error(anthropic.ConflictError, 409), "other"),
    (RuntimeError("something else"), "other"),
]


@pytest.mark.parametrize(("raised", "kind"), PROVIDER_ERRORS)
def test_provider_errors_become_model_unavailable(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, kind: UnavailableKind
) -> None:
    provider = Provider(raised).install(monkeypatch)
    with pytest.raises(ModelUnavailable) as error:
        live(spec()).complete(REQUEST)
    assert error.value.kind == kind
    assert len(provider.payloads) == 1, "a failed request is not retried"


def test_the_key_is_removed_from_an_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    Provider(RuntimeError(f"bad header x-api-key: {SECRET}")).install(monkeypatch)
    with pytest.raises(ModelUnavailable) as error:
        live(spec()).complete(REQUEST)
    assert SECRET not in str(error.value)
    assert "[redacted]" in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_tracing_is_off_during_a_call_even_when_the_environment_asks_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    # Nowhere to send a trace even if the switch under test were broken.
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    clear_langsmith_cache()
    try:
        assert langsmith_utils.tracing_is_enabled() is True, "the environment switch is live"
        provider = Provider(message(TOOL_USE)).install(monkeypatch)
        live(spec()).complete(REQUEST)
        live(spec("structured")).complete(REQUEST)
        assert provider.tracing_seen == [False, False]
        assert os.environ["LANGSMITH_TRACING"] == "true", "the environment is left alone"
    finally:
        clear_langsmith_cache()


def test_forced_tool_choice_is_not_available_on_every_model() -> None:
    from langchain_anthropic.chat_models import _supports_forced_tool_choice

    assert _supports_forced_tool_choice("claude-opus-5-5") is False
    assert _supports_forced_tool_choice("claude-haiku-5-5") is True


# --- recording and replay


def test_record_then_replay_round_trip(tmp_path: Path) -> None:
    model_spec = spec()
    first, repair = scripted_reply("one"), scripted_reply("two")
    inner = ScriptedModel([first, repair])
    recorder = RecordingModel(inner, tmp_path / "recordings", "total-transactions")
    second_call = REQUEST.model_copy(update={"call": 2})
    assert recorder.complete(REQUEST) == first
    assert recorder.complete(second_call) == repair
    assert sorted(path.name for path in (tmp_path / "recordings").iterdir()) == [
        "total-transactions.turn1.call1.json",
        "total-transactions.turn1.call2.json",
    ]

    replay = ReplayModel(model_spec, tmp_path / "recordings", "total-transactions")
    assert replay.complete(second_call) == repair
    assert replay.complete(REQUEST) == first
    assert inner.calls == 2, "replay did not reach the wrapped model"


def test_the_recording_file_is_a_versioned_document(tmp_path: Path) -> None:
    RecordingModel(ScriptedModel([scripted_reply()]), tmp_path, "case_1").complete(
        REQUEST.model_copy(update={"turn": 2})
    )
    path = llm.recording_path(tmp_path, "case_1", 2, 1)
    stored = json.loads(path.read_text())
    assert stored["format_version"] == 1
    assert (stored["case_id"], stored["turn"], stored["call"]) == ("case_1", 2, 1)
    assert stored["reply"]["request_hash"] == request_hash(spec(), REQUEST)
    assert Recording.model_validate(stored).reply == scripted_reply()
    assert SECRET not in path.read_text()


def test_replay_needs_no_key_and_builds_no_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("replay must not build the Claude client")

    RecordingModel(ScriptedModel([scripted_reply()]), tmp_path, "c").complete(REQUEST)
    monkeypatch.setattr(ChatAnthropic, "__init__", forbidden)
    monkeypatch.setattr(llm, "build_chat_model", forbidden)
    keyless = nl2sql.model_spec(load_settings({}, None))
    assert ReplayModel(keyless, tmp_path, "c").complete(REQUEST) == scripted_reply()


def test_a_missing_recording_is_stale(tmp_path: Path) -> None:
    replay = ReplayModel(spec(), tmp_path, "c")
    with pytest.raises(StaleRecording, match="stale recording: no reply was recorded") as error:
        replay.complete(REQUEST)
    assert error.value.kind == "missing"
    assert error.value.path == tmp_path / "c.turn1.call1.json"


@pytest.mark.parametrize(
    "change",
    [
        {"prompt_version": "tools-2"},
        {"model": "claude-opus-5-5"},
        {"effort": "high"},
        {"max_tokens": 4000},
    ],
)
def test_a_recording_made_for_another_request_is_stale(
    tmp_path: Path, change: dict[str, JsonValue]
) -> None:
    RecordingModel(ScriptedModel([scripted_reply()]), tmp_path, "c").complete(REQUEST)
    replay = ReplayModel(spec().model_copy(update=change), tmp_path, "c")
    with pytest.raises(StaleRecording, match="stale recording: the prompt, schemas or") as error:
        replay.complete(REQUEST)
    assert error.value.kind == "stale"


def test_a_changed_question_makes_the_recording_stale(tmp_path: Path) -> None:
    RecordingModel(ScriptedModel([scripted_reply()]), tmp_path, "c").complete(REQUEST)
    with pytest.raises(StaleRecording) as error:
        ReplayModel(spec(), tmp_path, "c").complete(REQUEST.model_copy(update={"user": "Other?"}))
    assert error.value.kind == "stale"


@pytest.mark.parametrize("content", ["not json", "{}", '{"format_version": 2}'])
def test_a_file_that_is_not_a_recording_is_stale(tmp_path: Path, content: str) -> None:
    llm.recording_path(tmp_path, "c", 1, 1).write_text(content)
    with pytest.raises(StaleRecording) as error:
        ReplayModel(spec(), tmp_path, "c").complete(REQUEST)
    assert error.value.kind == "unreadable"


def test_a_refusal_is_recorded_and_replayed_as_a_refusal(tmp_path: Path) -> None:
    refusal = ModelOutputError("refusal", scripted_reply(stop_reason="refusal"))
    recorder = RecordingModel(ScriptedModel([refusal]), tmp_path, "c")
    with pytest.raises(ModelOutputError):
        recorder.complete(REQUEST)
    with pytest.raises(ModelOutputError) as error:
        ReplayModel(spec(), tmp_path, "c").complete(REQUEST)
    assert error.value.kind == "refusal"


def test_an_unavailable_model_is_not_recorded(tmp_path: Path) -> None:
    recorder = RecordingModel(ScriptedModel([ModelUnavailable("timeout", "t")]), tmp_path, "c")
    with pytest.raises(ModelUnavailable):
        recorder.complete(REQUEST)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("case_id", ["", "../escape", "a/b", "a b", ".hidden"])
def test_a_case_id_must_be_a_safe_file_name(tmp_path: Path, case_id: str) -> None:
    with pytest.raises(ValueError, match="case id"):
        ReplayModel(spec(), tmp_path, case_id)
    with pytest.raises(ValueError, match="case id"):
        RecordingModel(ScriptedModel([]), tmp_path, case_id)


# --- the scripted model


def test_the_scripted_model_returns_its_script_in_order_and_counts() -> None:
    one, two = scripted_reply("one"), scripted_reply("two")
    model = ScriptedModel([one, ModelUnavailable("rate_limit", "slow down"), two])
    assert model.calls == 0
    assert model.complete(REQUEST) == one
    with pytest.raises(ModelUnavailable, match="slow down"):
        model.complete(REQUEST)
    assert model.complete(REQUEST.model_copy(update={"call": 2})) == two
    assert model.calls == 3
    assert [request.call for request in model.requests] == [1, 1, 2]


def test_the_scripted_model_raises_when_its_script_runs_out() -> None:
    model = ScriptedModel([scripted_reply()])
    model.complete(REQUEST)
    with pytest.raises(ScriptExhausted, match="called 2 times with 1 replies queued"):
        model.complete(REQUEST)
    assert model.calls == 1


def test_replies_and_specs_are_frozen() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        scripted_reply().text = "changed"  # type: ignore[misc]  # the assignment is the thing under test
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelRequest.model_validate({"system": "s", "user": "u", "history": []})
