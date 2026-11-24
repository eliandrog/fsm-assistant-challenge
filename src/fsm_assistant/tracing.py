"""Hosted tracing, opt-in.

Responsibility: turn a finished turn into LangSmith runs and send them, and
be the only module that sends anything to LangSmith (docs/DECISIONS.md,
D-27). Off unless `LANGSMITH_TRACING` is true and `LANGSMITH_API_KEY` is set
in this project's settings.

A trace is built from the turn's events, not from the graph's state.
LangChain's own tracing would send every node's input and output, which is
the state with its rows, so it stays off (`untraced`) and nothing here reads
the state. One function, `build_runs`, makes every payload, and a `TraceRun`
refuses a key that is not on the allow-list for its kind of run.

Sending never fails or delays a turn. `Tracer.trace` hands the runs of one
turn to a sink and returns; `LangSmithSink` sends them from its own daemon
thread, one request per turn. A failure is logged as a warning with the
class of the error, and the first one is also passed to `warn`, which the
terminal commands print. `close` waits a bounded time for what is pending.

The client is given the key, the address and the destination explicitly and
told not to add what it reads from the machine (runtime details and
`LANGSMITH_*` environment variables), so the process environment decides
nothing.
"""

import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, Self
from uuid import UUID

from langsmith import Client, agent_address, tracing_context
from langsmith.uuid import uuid7_from_datetime
from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from fsm_assistant.config import DEFAULT_LANGSMITH_PROJECT, Settings, TraceSystemPrompt
from fsm_assistant.events import StepEvent, TurnEvent, TurnRecord
from fsm_assistant.llm import ModelCall

logger = logging.getLogger(__name__)

JsonObject = dict[str, JsonValue]
RunKind = Literal["turn", "step", "model"]

# How long `close` waits for pending traces, in seconds.
FLUSH_SECONDS = 5.0
_TICK = timedelta(microseconds=1)
# Seconds the client waits to connect to LangSmith, and for its answer.
CONNECT_SECONDS, READ_SECONDS = 3, 10
# Turns waiting to be sent; a turn that finds the queue full is not traced.
QUEUE_SIZE = 200
PROVIDER = "anthropic"
# Failures whose sentence repeats text code did not write (the provider's error,
# a path on this machine): a trace carries their kind and no sentence.
_UNVOUCHED = frozenset({"internal", "model_unavailable"})

# The allow-list: every key a run's inputs and outputs may carry, by kind of
# run. Each is a field of an event, or a message of the model request.
ALLOWED: dict[RunKind, tuple[frozenset[str], frozenset[str]]] = {
    "turn": (
        frozenset({"question", "question_chars"}),
        frozenset(
            {
                "outcome", "status", "error_kind", "tool", "refused", "sql", "where_clause",
                "row_count", "columns", "truncated", "reads_labels", "caveats", "attempts",
                "model_calls", "input_tokens", "output_tokens", "steps", "elapsed_ms",
            }
        ),
    ),
    "step": (
        frozenset({"seq", "attempt", "model_calls"}),
        frozenset(
            {
                "duration_ms", "model_id", "model_ms", "input_tokens", "output_tokens",
                "response_id", "stop_reason", "reply_tools", "system_prompt_sha256", "tool",
                "guard", "sql", "where_clause", "reads_labels", "row_count", "columns",
                "truncated", "query_ms", "error_kind", "reason_code", "repairable",
                "failure_message", "outcome", "status", "caveats", "remembered",
            }
        ),
    ),
    "model": (
        frozenset({"messages"}),
        frozenset({"messages", "usage_metadata", "stop_reason", "response_id", "error"}),
    ),
}  # fmt: skip
ALLOWED_METADATA = frozenset(
    {
        "session_id", "turn", "command", "case_id", "model_id", "prompt_version", "tool_mode",
        "ls_provider", "ls_model_name",
    }
)  # fmt: skip


class TracingError(Exception):
    """Tracing was asked for and cannot be set up as configured."""


class Destination(BaseModel):
    """Where traces go: a project, or an agent and one of its environments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: str | None = None
    agent_id: str | None = None
    agent_environment: str | None = None

    def describe(self) -> str:
        if self.project is not None:
            return f"project {self.project!r}"
        return f"agent {self.agent_id!r}, environment {self.agent_environment!r}"


class TraceRun(BaseModel):
    """One run as it leaves the process. A key off the allow-list cannot be built."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RunKind
    id: UUID
    trace_id: UUID
    parent_run_id: UUID | None
    dotted_order: str
    name: str
    run_type: Literal["chain", "llm"]
    start_time: datetime
    end_time: datetime
    inputs: JsonObject
    outputs: JsonObject
    error: str | None  # a kind and a code; never a message
    tags: tuple[str, ...]
    metadata: dict[str, str | int]

    @model_validator(mode="after")
    def _only_allowed_keys(self) -> Self:
        inputs, outputs = ALLOWED[self.kind]
        extra = (
            (self.inputs.keys() - inputs)
            | (self.outputs.keys() - outputs)
            | (self.metadata.keys() - ALLOWED_METADATA)
        )
        if extra:
            raise ValueError(f"not allowed in a {self.kind} run: {sorted(extra)}")
        return self


class TraceSink(Protocol):
    on_error: Callable[[Exception], None]  # set by the tracer; told of each failed delivery

    def send(self, runs: Sequence[TraceRun]) -> None:
        """Take the runs of one turn. Returns at once and never raises for a failed delivery."""
        ...

    def flush(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for what was taken. False when some is still pending."""
        ...

    def close(self) -> None: ...


def untraced() -> AbstractContextManager[None]:
    """LangChain's and LangGraph's own tracing off inside the block, whatever the environment says."""
    return tracing_context(enabled=False)


def wanted(settings: Settings) -> bool:
    return settings.langsmith_tracing and settings.langsmith_api_key is not None


def destination(settings: Settings) -> Destination:
    """Where `settings` send traces. Raises `TracingError` when they name no single place."""
    project = settings.langsmith_project
    agent, environment = settings.langsmith_agent_id, settings.langsmith_agent_environment
    if agent is None and environment is None:
        return Destination(project=project or DEFAULT_LANGSMITH_PROJECT)
    if agent is None or environment is None:
        raise TracingError(
            "LANGSMITH_AGENT_ID and LANGSMITH_AGENT_ENVIRONMENT must be set together"
        )
    if project is not None:
        raise TracingError(
            "LANGSMITH_PROJECT and LANGSMITH_AGENT_ID name two destinations; set one of them"
        )
    try:
        address = agent_address(agent, environment)
    except Exception as error:  # the client's own check of the two values
        raise TracingError(str(error)) from None
    return Destination(agent_id=address["id"], agent_environment=address["environment"].lower())


def build_runs(
    record: TurnRecord, *, system_prompt: TraceSystemPrompt, ended: datetime
) -> tuple[TraceRun, ...]:
    """The runs of one turn: the turn, a child per step in order, and the model call under its step.

    Everything a trace carries is made here, from the events. Times are laid
    out backwards from `ended` with the events' durations.
    """
    turn = record.turn
    session = turn.conversation_id
    tags = tuple(
        tag
        for tag in (
            f"command:{turn.command}",
            f"model:{turn.model_id}",
            f"prompt:{turn.prompt_version}",
            f"mode:{turn.tool_mode}",
            None if turn.case_id is None else f"case:{turn.case_id}",
        )
        if tag is not None
    )
    metadata: dict[str, str | int] = {
        "session_id": session,  # LangSmith groups the runs of a conversation by this key
        "turn": turn.turn,
        "command": turn.command,
        "model_id": turn.model_id,
        "prompt_version": turn.prompt_version,
        "tool_mode": turn.tool_mode,
    }
    if turn.case_id is not None:
        metadata["case_id"] = turn.case_id
    started = ended - timedelta(milliseconds=turn.elapsed_ms)
    subject = turn.command if turn.case_id is None else f"{turn.command} {turn.case_id}"
    root = _run(
        "turn", f"{subject} turn {turn.turn}", None, started, ended, turn,
        error=_error(turn.error_kind, None), tags=tags, metadata=metadata,
    )  # fmt: skip
    runs = [root]
    calls = list(record.calls)
    cursor = started
    for step in record.steps:
        # LangSmith orders runs by start time, so each starts a moment after the one before.
        begun = max(cursor, runs[-1].start_time + _TICK)
        finished = max(cursor + timedelta(milliseconds=step.duration_ms), begun + _TICK)
        child = _run(
            "step", step.node, root, begun, finished, step,
            error=_error(step.error_kind, step.reason_code), tags=tags, metadata=metadata,
        )  # fmt: skip
        runs.append(child)
        if step.model_ms is not None and calls:
            runs.append(_model_run(calls.pop(0), child, turn, system_prompt, tags, metadata))
        cursor = finished
    runs[0] = root.model_copy(update={"end_time": max(ended, cursor)})
    return tuple(runs)


class Tracer:
    """Sends each finished turn as one trace. No method raises."""

    def __init__(
        self,
        sink: TraceSink,
        target: Destination,
        *,
        system_prompt: TraceSystemPrompt = "hash",
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self._sink = sink
        sink.on_error = self.failed
        self._system_prompt: TraceSystemPrompt = system_prompt
        self._warn = warn
        self._warned = False
        self.target = target

    def trace(self, record: TurnRecord) -> None:
        try:
            runs = build_runs(record, system_prompt=self._system_prompt, ended=datetime.now(UTC))
            self._sink.send(runs)
            logger.info(
                "trace queued: conversation=%s turn=%d trace_id=%s",
                runs[0].metadata["session_id"], record.turn.turn, runs[0].trace_id,
            )  # fmt: skip
        except Exception as error:  # a trace is a view of the turn, not part of it
            self.failed(error)

    def failed(self, error: Exception) -> None:
        """Log that a trace was not sent. The error's class only: its text is not ours to vouch for."""
        logger.warning("a trace could not be sent to LangSmith: %s", type(error).__name__)
        if not self._warned and self._warn is not None:
            self._warned = True
            self._warn(
                f"Warning: hosted tracing failed ({type(error).__name__}); the turn went on "
                "without its trace. Later failures are in the log only."
            )

    def close(self, timeout: float = FLUSH_SECONDS) -> None:
        try:
            delivered = self._sink.flush(timeout)
            self._sink.close()
        except Exception as error:
            self.failed(error)
            return
        if not delivered:
            message = f"some traces were not delivered to LangSmith within {timeout:g} s"
            logger.warning(message)
            if self._warn is not None:
                self._warn(f"Warning: {message} and were dropped.")


class LangSmithSink:
    """Sends runs to LangSmith from a daemon thread, one request per turn."""

    def __init__(
        self,
        settings: Settings,
        target: Destination,
        *,
        session: object | None = None,  # replaces the HTTP session, for tests
    ) -> None:
        key = settings.langsmith_api_key
        if key is None:
            raise TracingError("LANGSMITH_API_KEY is not set")
        self.on_error: Callable[[Exception], None] = lambda error: None
        self._placement: dict[str, object] = (
            {"session_name": target.project}
            if target.project is not None
            else {"address": agent_address(target.agent_id or "", target.agent_environment or "")}
        )
        # Every argument the client would otherwise read from the environment
        # or a profile file is given, and nothing is added to a run on its way out.
        self._client = Client(
            api_urls={settings.langsmith_endpoint: key.get_secret_value()},
            auto_batch_tracing=False,
            tracing_mode="langsmith",
            tracing_sampling_rate=1.0,
            workspace_id="",
            hide_inputs=False,
            hide_outputs=False,
            hide_metadata=False,
            omit_traced_runtime_info=True,
            timeout_ms=(CONNECT_SECONDS * 1000, READ_SECONDS * 1000),
            info={},
            disable_prompt_cache=True,
            tracing_error_callback=lambda error: self.on_error(error),
            session=session,  # type: ignore[arg-type]  # any object with a session's methods
        )
        self._queue: queue.Queue[list[dict[str, object]] | None] = queue.Queue(QUEUE_SIZE)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def send(self, runs: Sequence[TraceRun]) -> None:
        batch = [self._payload(run) for run in runs]
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._work, name="fsm-assistant-tracing", daemon=True
                )
                self._worker.start()
        try:
            self._queue.put_nowait(batch)
        except queue.Full as error:
            self.on_error(error)

    def flush(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def close(self) -> None:
        with self._lock:
            worker, self._worker = self._worker, None
        if worker is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                return  # the daemon thread ends with the process
            worker.join(timeout=1.0)
        self._client.close(timeout=0)

    def _work(self) -> None:
        while (batch := self._queue.get()) is not None:
            try:
                self._client.batch_ingest_runs(create=batch)
            except Exception as error:
                self.on_error(error)
            finally:
                self._queue.task_done()
        self._queue.task_done()

    def _payload(self, run: TraceRun) -> dict[str, object]:
        return {
            "id": run.id,
            "trace_id": run.trace_id,
            "parent_run_id": run.parent_run_id,
            "dotted_order": run.dotted_order,
            "name": run.name,
            "run_type": run.run_type,
            "start_time": run.start_time,
            "end_time": run.end_time,
            "inputs": run.inputs,
            "outputs": run.outputs,
            "error": run.error,
            "tags": list(run.tags),
            "extra": {"metadata": dict(run.metadata)},
            **self._placement,
        }


def start(settings: Settings, warn: Callable[[str], None] | None = None) -> Tracer | None:
    """A tracer when the settings ask for one and it can be built; None otherwise, with a warning."""
    if not wanted(settings):
        return None
    try:
        target = destination(settings)
        sink = LangSmithSink(settings, target)
    except Exception as error:
        reason = str(error) if isinstance(error, TracingError) else type(error).__name__
        logger.warning("hosted tracing is not set up and stays off: %s", reason)
        if warn is not None:
            warn(f"Warning: hosted tracing is not set up and stays off: {reason}.")
        return None
    logger.info(
        "hosted tracing is on: %s at %s", target.describe(), settings.langsmith_endpoint
    )
    return Tracer(sink, target, system_prompt=settings.trace_system_prompt, warn=warn)


def describe(tracer: Tracer | None, settings: Settings, *, live: bool = True) -> str:
    """One line saying whether turns are traced and where to. Never the key."""
    if tracer is not None:
        return (
            f"Hosted tracing: on, to LangSmith {tracer.target.describe()} at "
            f"{settings.langsmith_endpoint}; result rows are never sent."
        )
    if not live:
        return "Hosted tracing: off (replayed replies are never traced)."
    return "Hosted tracing: off."


def _run(
    kind: Literal["turn", "step"],
    name: str,
    parent: TraceRun | None,
    started: datetime,
    ended: datetime,
    event: TurnEvent | StepEvent,
    *,
    error: str | None,
    tags: tuple[str, ...],
    metadata: dict[str, str | int],
) -> TraceRun:
    inputs, outputs = ALLOWED[kind]
    sent = set(outputs)
    if event.error_kind in _UNVOUCHED:
        sent.discard("failure_message")
    return _placed(
        kind, name, "chain", parent, started, ended,
        inputs=event.model_dump(mode="json", include=set(inputs), exclude_none=True),
        outputs=event.model_dump(mode="json", include=sent, exclude_none=True),
        error=error, tags=tags, metadata=metadata,
    )  # fmt: skip


def _model_run(
    call: ModelCall,
    step: TraceRun,
    turn: TurnEvent,
    system_prompt: TraceSystemPrompt,
    tags: tuple[str, ...],
    metadata: dict[str, str | int],
) -> TraceRun:
    system = (
        call.request.system
        if system_prompt == "full"
        else f"[system prompt not sent: version {turn.prompt_version}, "
        f"sha256 {call.system_sha256}, {len(call.request.system):,} characters]"
    )
    outputs: JsonObject = {}
    reply = call.reply
    model = turn.model_id
    if reply is not None:
        model = reply.model_id
        message: JsonObject = {"role": "assistant", "content": reply.text}
        if reply.tool_calls:
            message["tool_calls"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "arguments": json.dumps(tool.arguments, ensure_ascii=True),
                    },
                }
                for tool in reply.tool_calls
            ]
        total = reply.usage.input_tokens + reply.usage.output_tokens
        outputs = {
            "messages": [message],
            "usage_metadata": {
                "input_tokens": reply.usage.input_tokens,
                "output_tokens": reply.usage.output_tokens,
                "total_tokens": total,
            },
        }
        if reply.stop_reason is not None:
            outputs["stop_reason"] = reply.stop_reason
        if reply.response_id is not None:
            outputs["response_id"] = reply.response_id
    if call.error is not None:
        outputs["error"] = call.error
    started = max(
        step.start_time + _TICK, step.end_time - timedelta(milliseconds=call.duration_ms)
    )
    return _placed(
        "model", model, "llm", step, started, step.end_time,
        inputs={
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": call.request.user},
            ]
        },
        outputs=outputs,
        error=call.error, tags=tags,
        metadata={**metadata, "ls_provider": PROVIDER, "ls_model_name": model},
    )  # fmt: skip


def _placed(
    kind: RunKind,
    name: str,
    run_type: Literal["chain", "llm"],
    parent: TraceRun | None,
    started: datetime,
    ended: datetime,
    *,
    inputs: JsonObject,
    outputs: JsonObject,
    error: str | None,
    tags: tuple[str, ...],
    metadata: dict[str, str | int],
) -> TraceRun:
    """Give a run its id and its place in the trace, in the form LangSmith orders runs by."""
    run_id = uuid7_from_datetime(started)
    order = started.strftime("%Y%m%dT%H%M%S%fZ") + str(run_id)
    return TraceRun(
        kind=kind,
        id=run_id,
        trace_id=run_id if parent is None else parent.trace_id,
        parent_run_id=None if parent is None else parent.id,
        dotted_order=order if parent is None else f"{parent.dotted_order}.{order}",
        name=name,
        run_type=run_type,
        start_time=started,
        end_time=ended,
        inputs=inputs,
        outputs=outputs,
        error=error,
        tags=tags,
        metadata=metadata,
    )


def _error(kind: str | None, code: str | None) -> str | None:
    if kind is None:
        return None
    return kind if code is None else f"{kind}: {code}"
