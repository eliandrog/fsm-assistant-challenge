"""Model client.

Responsibility: the only module that talks to a model provider, and the only
one that imports LangChain's Claude wrapper. Everything else sees one small
interface, `IntentModel`: a `ModelRequest` (system text and user text) goes
in, a `ModelReply` (the raw tool calls or raw text, as the model sent them)
comes out. Nothing here judges the reply's shape or knows what the tools
mean; `nl2sql` owns that.

Four implementations:

- `ClaudeModel`: one request to Claude (docs/DECISIONS.md, D-14, D-20).
- `RecordingModel`: wraps another model and writes each reply to a file.
- `ReplayModel`: reads those files; needs no key and makes no request.
- `ScriptedModel`: returns queued replies, for tests.

`TappedModel` wraps any of them and notes each request of the turn in
progress as a `ModelCall` (what was sent, what came back, how long it took),
which is what a hosted trace shows of the model (D-27).

A reply is tied to the request that produced it by `request_hash`, which
covers the model settings, both messages, the schemas offered and the prompt
version. Replay refuses a recording whose hash differs (D-21).
"""

import hashlib
import json
import re
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal, Protocol

from langchain_anthropic import ChatAnthropic
from langchain_core.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelInvalidRequestError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, JsonValue, SecretStr, ValidationError

from fsm_assistant.config import Effort, Settings, ToolMode

ANTHROPIC_API_URL = "https://api.anthropic.com"
RECORDING_FORMAT_VERSION: Literal[1] = 1

JsonObject = dict[str, JsonValue]

UnavailableKind = Literal[
    "missing_key",
    "authentication",
    "permission",
    "model_not_found",
    "rate_limit",
    "overloaded_or_server_error",
    "connection",
    "timeout",
    "invalid_request",
    "other",
]
OutputErrorKind = Literal["refusal", "truncated"]
StaleKind = Literal["missing", "stale", "unreadable"]

_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_REDACTED = "[redacted]"


class ToolSpec(BaseModel):
    """One tool offered to the model: its name, what it is for, and its argument schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    input_schema: JsonObject


class ModelSpec(BaseModel):
    """Everything about a request that is the same for every question."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    effort: Effort
    max_tokens: int
    tool_mode: ToolMode
    tools: tuple[ToolSpec, ...]
    intent_schema: JsonObject
    prompt_version: str


class ModelRequest(BaseModel):
    """One request. `turn` and `call` say where it sits in a conversation: the
    second call of a turn is the repair attempt. They key a recording and are
    not sent to the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    user: str
    turn: int = 1
    call: int = 1


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    arguments: JsonObject


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int
    output_tokens: int


class ModelReply(BaseModel):
    """What the model sent back, unjudged.

    In tool mode the answer is `tool_calls`; in structured mode it is `text`,
    which holds one JSON object. `response_id` is the provider's message id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_mode: ToolMode
    tool_calls: tuple[ToolCall, ...]
    text: str
    stop_reason: str | None
    usage: TokenUsage
    model_id: str
    response_id: str | None
    request_hash: str


class Recording(BaseModel):
    """The file format of one recorded reply (`RECORDING_FORMAT_VERSION`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: Literal[1]
    case_id: str
    turn: int
    call: int
    reply: ModelReply


class ModelError(Exception):
    """Base class for everything this module raises."""


class ModelUnavailable(ModelError):
    """No reply could be obtained: no key, or the provider refused or could not be reached."""

    def __init__(self, kind: UnavailableKind, message: str) -> None:
        super().__init__(message)
        self.kind: UnavailableKind = kind


class ModelOutputError(ModelError):
    """The provider replied, but with a refusal or a reply cut off at the token limit."""

    def __init__(self, kind: OutputErrorKind, reply: ModelReply) -> None:
        super().__init__(
            "the model declined to answer (stop reason: refusal)"
            if kind == "refusal"
            else "the model's reply was cut off at the token limit (stop reason: max_tokens)"
        )
        self.kind: OutputErrorKind = kind
        self.reply = reply


class StaleRecording(ModelError):
    """Replay has no usable recording for this request."""

    def __init__(self, kind: StaleKind, path: Path, detail: str) -> None:
        super().__init__(f"stale recording: {detail} ({path.name})")
        self.kind: StaleKind = kind
        self.path = path


class ScriptExhausted(ModelError):
    """A scripted model was asked for more replies than its script holds."""


class IntentModel(Protocol):
    def complete(self, request: ModelRequest) -> ModelReply:
        """Return the model's reply to `request`.

        Raises `ModelUnavailable` or `ModelOutputError`; a replaying model
        raises `StaleRecording`.
        """
        ...


def request_hash(spec: ModelSpec, request: ModelRequest) -> str:
    """Hash of everything that decides a reply: settings, messages, schemas, prompt version.

    Only the schemas the tool mode sends are included, so editing the unused
    mode's schema does not invalidate recordings.
    """
    schemas: JsonValue = (
        [tool.model_dump(mode="json") for tool in spec.tools]
        if spec.tool_mode == "tools"
        else spec.intent_schema
    )
    canonical = json.dumps(
        {
            "model": spec.model,
            "effort": spec.effort,
            "max_tokens": spec.max_tokens,
            "tool_mode": spec.tool_mode,
            "prompt_version": spec.prompt_version,
            "schemas": schemas,
            "system": request.system,
            "user": request.user,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


def build_chat_model(spec: ModelSpec, settings: Settings) -> ChatAnthropic:
    """Construct the Claude client. Sends nothing.

    Retries are off (the turn graph owns the one repair), no sampling
    parameter is set, and the key and address are passed explicitly so that
    no environment variable can redirect the key to another host.
    """
    if settings.anthropic_api_key is None:
        raise ModelUnavailable(
            "missing_key",
            "ANTHROPIC_API_KEY is not set. Put it in .env or the environment to call Claude; "
            "tests and replayed runs do not need it.",
        )
    return ChatAnthropic(
        model=spec.model,
        max_tokens=spec.max_tokens,
        default_request_timeout=settings.model_timeout_seconds,
        max_retries=0,
        anthropic_api_key=settings.anthropic_api_key,
        anthropic_api_url=ANTHROPIC_API_URL,
        # Effort goes in as output_config, not as the wrapper's `effort`
        # shortcut: the shortcut also adds a `thinking` block to the request.
        output_config={"effort": spec.effort},
    )


class ClaudeModel:
    """One request to Claude per call, through LangChain's wrapper."""

    def __init__(
        self, spec: ModelSpec, chat: BaseChatModel, *, secret: SecretStr | None = None
    ) -> None:
        self._spec = spec
        self._secret = secret
        self._runnable: Runnable[LanguageModelInput, object]
        if spec.tool_mode == "tools":
            # Tool choice stays on auto: not every model accepts a forced choice.
            self._runnable = chat.bind_tools(
                [{**tool.model_dump(mode="json"), "strict": True} for tool in spec.tools],
                strict=True,
                parallel_tool_calls=False,
            )
        else:
            self._runnable = chat.with_structured_output(
                spec.intent_schema, method="json_schema", include_raw=True
            )

    @classmethod
    def from_settings(cls, spec: ModelSpec, settings: Settings) -> "ClaudeModel":
        return cls(spec, build_chat_model(spec, settings), secret=settings.anthropic_api_key)

    def complete(self, request: ModelRequest) -> ModelReply:
        messages: list[BaseMessage] = [
            SystemMessage(content=request.system),
            HumanMessage(content=request.user),
        ]
        try:
            # Hosted tracing stays off whatever the environment asks for.
            with tracing_context(enabled=False):
                message = self._invoke(messages)
        except ModelError:
            raise
        except Exception as error:  # the provider boundary: every failure becomes typed
            raise ModelUnavailable(_kind_of(error), self._describe(error)) from None
        return _checked(_to_reply(message, self._spec, request))

    def _invoke(self, messages: list[BaseMessage]) -> BaseMessage:
        output = self._runnable.invoke(messages)
        # The structured-output binding returns the message under "raw".
        message = output.get("raw") if isinstance(output, dict) else output
        if not isinstance(message, BaseMessage):
            raise ModelUnavailable("other", "the model binding returned no message")
        return message

    def _describe(self, error: Exception) -> str:
        text = f"{type(error).__name__}: {error}"
        if self._secret is not None and self._secret.get_secret_value():
            text = text.replace(self._secret.get_secret_value(), _REDACTED)
        return text


class RecordingModel:
    """Wraps a model and writes every reply to `directory`, one file per call."""

    def __init__(self, inner: IntentModel, directory: Path, case_id: str) -> None:
        self._inner = inner
        self._directory = directory
        self._case_id = _safe_case_id(case_id)

    def complete(self, request: ModelRequest) -> ModelReply:
        try:
            reply = self._inner.complete(request)
        except ModelOutputError as error:
            # A refusal is an outcome of the run, so replay must reproduce it.
            self._write(request, error.reply)
            raise
        self._write(request, reply)
        return reply

    def _write(self, request: ModelRequest, reply: ModelReply) -> None:
        recording = Recording(
            format_version=RECORDING_FORMAT_VERSION,
            case_id=self._case_id,
            turn=request.turn,
            call=request.call,
            reply=reply,
        )
        self._directory.mkdir(parents=True, exist_ok=True)
        path = recording_path(self._directory, self._case_id, request.turn, request.call)
        path.write_text(recording.model_dump_json(indent=2) + "\n", encoding="utf-8")


class ReplayModel:
    """Answers from recorded files. Never builds a client and needs no key."""

    def __init__(self, spec: ModelSpec, directory: Path, case_id: str) -> None:
        self._spec = spec
        self._directory = directory
        self._case_id = _safe_case_id(case_id)

    def complete(self, request: ModelRequest) -> ModelReply:
        path = recording_path(self._directory, self._case_id, request.turn, request.call)
        if not path.is_file():
            raise StaleRecording("missing", path, "no reply was recorded for this call")
        try:
            recording = Recording.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as error:
            first = error.errors()[0]
            raise StaleRecording(
                "unreadable",
                path,
                f"the file is not a version {RECORDING_FORMAT_VERSION} recording: {first['msg']}",
            ) from None
        if recording.reply.request_hash != request_hash(self._spec, request):
            raise StaleRecording(
                "stale",
                path,
                "the prompt, schemas or model settings changed since it was recorded",
            )
        return _checked(recording.reply)


class ScriptedModel:
    """Returns the queued replies in order, or raises a queued error. For tests."""

    def __init__(self, script: Sequence[ModelReply | ModelError]) -> None:
        self._script = list(script)
        self.requests: list[ModelRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def complete(self, request: ModelRequest) -> ModelReply:
        if self.calls >= len(self._script):
            raise ScriptExhausted(
                f"scripted model called {self.calls + 1} times "
                f"with {len(self._script)} replies queued"
            )
        step = self._script[self.calls]
        self.requests.append(request)
        if isinstance(step, ModelError):
            raise step
        return step


class ModelCall(BaseModel):
    """One request to the model and what came back. It stays in the process."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelRequest
    duration_ms: int
    reply: ModelReply | None  # None when no reply came back
    error: str | None = None  # the class and kind of what was raised, never its text

    @property
    def system_sha256(self) -> str:
        return hashlib.sha256(self.request.system.encode("utf-8", "surrogatepass")).hexdigest()


# The model requests of the turn running in this context.
_CALLS: ContextVar[list[ModelCall] | None] = ContextVar("fsm_assistant_model_calls", default=None)


@contextmanager
def model_calls() -> Iterator[list[ModelCall]]:
    """Collect the requests a `TappedModel` makes inside the block, in order."""
    calls: list[ModelCall] = []
    token = _CALLS.set(calls)
    try:
        yield calls
    finally:
        _CALLS.reset(token)


class TappedModel:
    """Passes requests on and notes each one for the turn in progress."""

    def __init__(self, inner: IntentModel) -> None:
        self._inner = inner

    def complete(self, request: ModelRequest) -> ModelReply:
        started = time.monotonic()
        reply: ModelReply | None = None
        error: str | None = None
        try:
            reply = self._inner.complete(request)
            return reply
        except ModelOutputError as raised:
            reply, error = raised.reply, f"ModelOutputError: {raised.kind}"
            raise
        except Exception as raised:
            kind = getattr(raised, "kind", None)
            error = type(raised).__name__ + (f": {kind}" if isinstance(kind, str) else "")
            raise
        finally:
            calls = _CALLS.get()
            if calls is not None:
                elapsed = round((time.monotonic() - started) * 1000)
                calls.append(
                    ModelCall(request=request, duration_ms=elapsed, reply=reply, error=error)
                )


def recording_path(directory: Path, case_id: str, turn: int, call: int) -> Path:
    return directory / f"{_safe_case_id(case_id)}.turn{turn}.call{call}.json"


def _safe_case_id(case_id: str) -> str:
    if not _CASE_ID.fullmatch(case_id):
        raise ValueError(f"case id must be letters, digits, '-' or '_', got {case_id!r}")
    return case_id


def _checked(reply: ModelReply) -> ModelReply:
    if reply.stop_reason == "refusal":
        raise ModelOutputError("refusal", reply)
    if reply.stop_reason == "max_tokens":
        raise ModelOutputError("truncated", reply)
    return reply


def _to_reply(message: BaseMessage, spec: ModelSpec, request: ModelRequest) -> ModelReply:
    metadata = message.response_metadata
    usage = message.usage_metadata if isinstance(message, AIMessage) else None
    calls = message.tool_calls if isinstance(message, AIMessage) else []
    stop_reason = metadata.get("stop_reason")
    response_id = metadata.get("id")
    return ModelReply(
        tool_mode=spec.tool_mode,
        tool_calls=tuple(ToolCall(name=call["name"], arguments=call["args"]) for call in calls),
        text=_text_of(message),
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        usage=TokenUsage(
            input_tokens=usage["input_tokens"] if usage else 0,
            output_tokens=usage["output_tokens"] if usage else 0,
        ),
        model_id=str(metadata.get("model_name") or metadata.get("model") or spec.model),
        response_id=response_id if isinstance(response_id, str) else None,
        request_hash=request_hash(spec, request),
    )


def _text_of(message: BaseMessage) -> str:
    """The text blocks of a message, joined. Thinking and tool-use blocks are left out."""
    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, str):
            parts.append(block)
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _kind_of(error: Exception) -> UnavailableKind:
    # Timeout before connection: the provider's timeout error is also a connection error.
    table: tuple[tuple[type[Exception], UnavailableKind], ...] = (
        (ModelAuthenticationError, "authentication"),
        (ModelPermissionDeniedError, "permission"),
        (ModelNotFoundError, "model_not_found"),
        (ModelRateLimitError, "rate_limit"),
        (ModelTimeoutError, "timeout"),
        (ModelConnectionError, "connection"),
        (ModelAPIError, "overloaded_or_server_error"),
        (ModelInvalidRequestError, "invalid_request"),
    )
    for kind, name in table:
        if isinstance(error, kind):
            return name
    return "other"
