"""The turn graph.

Responsibility: wire the nodes into the fixed graph of one turn, keep a
conversation's memory, and offer the single entry point every interface
uses, `Assistant` (docs/DECISIONS.md, D-15, D-23).

Code chooses every route, from the state. The one repair is a route back to
`call_model`, allowed while fewer than two replies have been judged; that
counter, not the framework's recursion limit, is what bounds a turn.

Memory is an in-process checkpointer keyed by the conversation id, owned by
one `Assistant`; the module keeps no state of its own. It holds the question
and accepted SQL or clause of the last turn a follow-up can build on, never
rows, and is lost when the process ends. One checkpoint is written per turn,
when it ends, and `LatestCheckpointSaver` drops the one before, so what the
process retains for a conversation is the state after its latest turn.

`Assistant.run_turn` returns a `TurnResult` for every input: an exception no
node expected becomes the outcome `failed` with kind `internal`.

Each node's update is described as a `StepEvent` and the finished turn as a
`TurnEvent` (`events.py`). The `--trace` line is made from the first, and a
hosted trace from both when the assistant was given a `Tracer` (D-27).
LangGraph's own tracing is off for the whole run: it would send the state,
rows included.

One log line is written when a turn finishes, with its outcome and no text
of the question, the SQL or the data; a failed turn is logged as an error
with the kind of failure and, for a fault no node expected, the class of the
exception and where it was raised, never its message.
"""

import logging
import threading
import time
import traceback
from collections.abc import Callable, Hashable
from typing import cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict

from fsm_assistant.config import Settings
from fsm_assistant.events import (
    Label,
    NodeName,
    StepEvent,
    TurnRecord,
    describe_step,
    step_event,
    turn_event,
)
from fsm_assistant.llm import (
    IntentModel,
    ModelCall,
    ModelReply,
    ModelSpec,
    TappedModel,
    model_calls,
)
from fsm_assistant.nl2sql import AskClarification, Decline, ProposeRule, RunSql, model_spec
from fsm_assistant.nodes import TurnNodes
from fsm_assistant.presentation import safe_text, status_of
from fsm_assistant.result import TurnError, TurnMeta, TurnResult
from fsm_assistant.state import MAX_MODEL_CALLS, SAVED_TYPES, TurnInput, TurnState
from fsm_assistant.tracing import Tracer, untraced

logger = logging.getLogger(__name__)

NODE_NAMES: tuple[NodeName, ...] = (
    "begin_turn",
    "call_model",
    "check_intent",
    "guard_sql",
    "execute_sql",
    "guard_rule",
    "backtest_rule",
    "run_scan",
    "present",
    "remember",
)

type TurnGraph = CompiledStateGraph[TurnState, None, TurnInput, TurnState]


class TraceStep(BaseModel):
    """One node's update, described for `--trace`. It never holds rows, only their count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    summary: str


class LatestCheckpointSaver(InMemorySaver):
    """An in-memory saver that keeps one checkpoint per conversation: the latest.

    The library's saver keeps every checkpoint it is given, so the questions
    and SQL of every past turn would stay in the process. Here a new
    checkpoint replaces the one before it, with that one's pending writes and
    the channel values only it referred to.
    """

    def __init__(self, *, serde: JsonPlusSerializer) -> None:
        super().__init__(serde=serde)
        self._lock = threading.RLock()

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        with self._lock:
            saved = super().put(config, checkpoint, metadata, new_versions)
            thread: str = config["configurable"]["thread_id"]
            namespace: str = config["configurable"]["checkpoint_ns"]
            latest, live = checkpoint["id"], checkpoint["channel_versions"]
            checkpoints = self.storage[thread][namespace]
            for old in [key for key in checkpoints if key != latest]:
                del checkpoints[old]
            for written in [
                key for key in self.writes if key[:2] == (thread, namespace) and key[2] != latest
            ]:
                del self.writes[written]
            for blob in [
                key
                for key in self.blobs
                if key[:2] == (thread, namespace) and live.get(key[2]) != key[3]
            ]:
                del self.blobs[blob]
            return saved

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with self._lock:
            return super().get_tuple(config)


def retry(state: TurnState) -> str:
    """After a failure: one repair request, or give up. A routing function, not a node."""
    failure = state.failure
    if failure is not None and failure.repair is not None and state.attempt < MAX_MODEL_CALLS:
        return "repair"
    return "failed"


def _after_begin_turn(state: TurnState) -> str:
    return "input invalid" if state.failure is not None else "ok"


def _after_call_model(state: TurnState) -> str:
    return "no usable reply" if state.failure is not None else "ok"


def _after_check_intent(state: TurnState) -> str:
    if state.failure is not None:
        return retry(state)
    match state.action:
        case RunSql():
            return "run_sql"
        case ProposeRule():
            return "propose_rule"
        case AskClarification() | Decline():
            return "ask_clarification or decline"
        case _:
            return "profile_fraud"


def _ok_or_retry(ok: str) -> Callable[[TurnState], str]:
    def route(state: TurnState) -> str:
        return ok if state.failure is None else retry(state)

    return route


def build_graph(nodes: TurnNodes, saver: InMemorySaver) -> TurnGraph:
    graph = StateGraph(TurnState, input_schema=TurnInput)
    graph.add_node("begin_turn", nodes.begin_turn)
    graph.add_node("call_model", nodes.call_model)
    graph.add_node("check_intent", nodes.check_intent)
    graph.add_node("guard_sql", nodes.guard_sql)
    graph.add_node("execute_sql", nodes.execute_sql)
    graph.add_node("guard_rule", nodes.guard_rule)
    graph.add_node("backtest_rule", nodes.backtest_rule)
    graph.add_node("run_scan", nodes.run_scan)
    graph.add_node("present", nodes.present)
    graph.add_node("remember", nodes.remember)

    repair: dict[Hashable, str] = {"repair": "call_model", "failed": "present"}
    graph.add_edge(START, "begin_turn")
    graph.add_conditional_edges(
        "begin_turn", _after_begin_turn, {"ok": "call_model", "input invalid": "present"}
    )
    graph.add_conditional_edges(
        "call_model", _after_call_model, {"ok": "check_intent", "no usable reply": "present"}
    )
    graph.add_conditional_edges(
        "check_intent",
        _after_check_intent,
        {
            "run_sql": "guard_sql",
            "propose_rule": "guard_rule",
            "profile_fraud": "run_scan",
            "ask_clarification or decline": "present",
            **repair,
        },
    )
    graph.add_conditional_edges(
        "guard_sql", _ok_or_retry("accepted"), {"accepted": "execute_sql", **repair}
    )
    graph.add_conditional_edges("execute_sql", _ok_or_retry("ok"), {"ok": "present", **repair})
    graph.add_conditional_edges(
        "guard_rule", _ok_or_retry("accepted"), {"accepted": "backtest_rule", **repair}
    )
    graph.add_conditional_edges("backtest_rule", _ok_or_retry("ok"), {"ok": "present", **repair})
    graph.add_edge("run_scan", "present")
    graph.add_edge("present", "remember")
    graph.add_edge("remember", END)
    return graph.compile(checkpointer=saver)


class Assistant:
    """The compiled graph with one model and one in-process memory."""

    def __init__(
        self,
        settings: Settings,
        model: IntentModel,
        *,
        label: Label = Label(),
        tracer: Tracer | None = None,
    ) -> None:
        """`label` says which command runs the turns; `tracer`, when given, is sent each one."""
        self._spec: ModelSpec = model_spec(settings)
        self._label = label
        self._tracer = tracer
        self.saver = LatestCheckpointSaver(
            serde=JsonPlusSerializer(allowed_msgpack_modules=SAVED_TYPES)
        )
        nodes = TurnNodes(settings, TappedModel(model), self._spec)
        self.graph: TurnGraph = build_graph(nodes, self.saver)

    def run_turn(
        self,
        question: str,
        conversation_id: str,
        *,
        on_step: Callable[[TraceStep], None] | None = None,
    ) -> TurnResult:
        """Answer one message of the conversation `conversation_id`. Never raises."""
        started = time.monotonic()
        response: TurnResult | None = None
        seen: dict[str, object] = {}  # the counters of the updates streamed so far
        steps: list[StepEvent] = []
        with model_calls() as calls:
            try:
                with untraced():
                    for chunk in self.graph.stream(
                        TurnInput(question=question),
                        {"configurable": {"thread_id": conversation_id}},
                        stream_mode="updates",
                        durability="exit",  # one checkpoint per turn, written when it ends
                    ):
                        for node, node_update in chunk.items():
                            fields: dict[str, object] = node_update or {}
                            seen.update(
                                {name: fields[name] for name in _COUNTERS if name in fields}
                            )
                            found = fields.get("response")
                            if isinstance(found, TurnResult):
                                response = found
                            step = self._step(
                                node, fields, seen, steps, calls, conversation_id, started
                            )
                            if step is not None and on_step is not None:
                                _tell(on_step, TraceStep(node=node, summary=describe_step(step)))
                if response is None:
                    raise RuntimeError("the graph ended without a response")
            except Exception as error:  # the interface boundary: every fault becomes an outcome
                response = self._internal(error, seen)
                logger.error(
                    "internal failure: %s raised at %s", type(error).__name__, _raised_at(error)
                )
        meta = response.meta.model_copy(
            update={
                "conversation_id": conversation_id,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
        result = response.model_copy(update={"meta": meta})
        self._finished(result, question, steps, calls)
        return result

    def _step(
        self,
        node: str,
        fields: dict[str, object],
        seen: dict[str, object],
        steps: list[StepEvent],
        calls: list[ModelCall],
        conversation_id: str,
        started: float,
    ) -> StepEvent | None:
        """Describe one node's update. A fault here loses the description only."""
        try:
            elapsed = round((time.monotonic() - started) * 1000)
            # A `call_model` step that failed before its request has no call of its own.
            used = sum(earlier.model_ms is not None for earlier in steps)
            call = calls[used] if node == "call_model" and len(calls) > used else None
            step = step_event(
                cast(NodeName, node),
                fields,
                label=self._label,
                conversation_id=conversation_id,
                turn=_counted(seen, "turn"),
                seq=len(steps) + 1,
                duration_ms=elapsed - sum(earlier.duration_ms for earlier in steps),
                attempt=_counted(seen, "attempt"),
                model_calls=_counted(seen, "model_calls"),
                call=call,
            )
            steps.append(step)
            return step
        except Exception as error:  # an event is a view of the turn, not part of it
            logger.warning("a step could not be described: %s", type(error).__name__)
            return None

    def _finished(
        self, result: TurnResult, question: str, steps: list[StepEvent], calls: list[ModelCall]
    ) -> None:
        """Log the finished turn and hand it to the tracer. Neither can fail the turn."""
        try:
            meta, error = result.meta, result.error
            failed = result.outcome == "failed"
            logger.log(
                logging.ERROR if failed else logging.INFO,
                "turn finished: conversation=%s turn=%d outcome=%s error=%s%s requests=%d "
                "elapsed_ms=%d",
                safe_text(meta.conversation_id, 64),
                meta.turn,
                result.outcome,
                "-" if error is None else error.kind,
                "".join(f" model_error={call.error!r}" for call in calls if call.error),
                meta.model_calls,
                meta.elapsed_ms,
            )
            if meta.model_calls > 1:
                logger.warning(
                    "turn needed a repair: conversation=%s turn=%d refused=%s",
                    safe_text(meta.conversation_id, 64),
                    meta.turn,
                    ",".join(str(code) for code in result.refused) or "-",
                )
            if self._tracer is not None:
                turn = turn_event(result, question, tuple(steps), tuple(calls), label=self._label)
                self._tracer.trace(TurnRecord(turn=turn, steps=tuple(steps), calls=tuple(calls)))
        except Exception as error:  # as above: observing a turn never fails it
            logger.warning("a finished turn could not be reported: %s", type(error).__name__)

    def mermaid(self) -> str:
        """The graph as a Mermaid diagram, drawn from the compiled graph."""
        return self.graph.get_graph().draw_mermaid(with_styles=False)

    def _internal(self, error: Exception, seen: dict[str, object]) -> TurnResult:
        """The reply for a fault no node expected, with the counts of the steps that had run."""
        reply = seen.get("raw_reply")
        message = f"An internal error stopped the turn ({type(error).__name__}): {error}"
        return TurnResult(
            outcome="failed",
            status=status_of("failed"),
            error=TurnError(kind="internal", message=message),
            answer=message,
            answer_source="code",
            tool=None,
            refused=(),
            write_attempts=0,
            sql=None,
            where_clause=None,
            table=None,
            rule=None,
            explanation=None,
            assumptions=(),
            suggestions=(),
            caveats=(),
            meta=TurnMeta(
                conversation_id="",
                turn=_counted(seen, "turn"),
                model_id=reply.model_id if isinstance(reply, ModelReply) else self._spec.model,
                prompt_version=self._spec.prompt_version,
                tool_mode=self._spec.tool_mode,
                attempts=_counted(seen, "attempt"),
                model_calls=_counted(seen, "model_calls"),
                input_tokens=_counted(seen, "input_tokens"),
                output_tokens=_counted(seen, "output_tokens"),
                elapsed_ms=0,
            ),
        )


_COUNTERS = ("turn", "attempt", "model_calls", "input_tokens", "output_tokens", "raw_reply")


def _tell(on_step: Callable[[TraceStep], None], step: TraceStep) -> None:
    """Hand a step to the caller's callback. A callback that fails does not fail the turn."""
    try:
        on_step(step)
    except Exception:  # the trace is a view of the turn, not part of it
        pass


def _raised_at(error: Exception) -> str:
    """Where `error` was raised, as file, line and function. Its message is not read."""
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return "an unknown place"
    last = frames[-1]
    return f"{last.filename.rsplit('/', 1)[-1]}:{last.lineno} in {last.name}"


def _counted(seen: dict[str, object], name: str) -> int:
    value = seen.get(name)
    return value if isinstance(value, int) else 0
