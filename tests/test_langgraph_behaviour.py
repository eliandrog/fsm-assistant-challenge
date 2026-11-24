"""Two facts about LangGraph that the turn graph relies on, checked on the installed version.

1. A state field annotated with `UntrackedValue` is visible to later nodes
   and returned by `invoke`, but never written to the checkpointer. Query
   results live in such a field, so rows are not saved with a
   conversation.
2. Every other field persists across invocations on the same `thread_id`. A
   field that belongs to one turn therefore has to be reset by the first
   node; nothing resets it for us.

The graph takes only the question as input (`input_schema`), so an invocation
cannot overwrite saved state by passing a whole state object.

These use a three-node graph of their own, so a change in the library shows
here first. The real graph's tests are in test_graph.py.
"""

from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langgraph.channels import UntrackedValue
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict

MARKER = "ROW-VALUE-THAT-MUST-NOT-BE-SAVED"
Rows = tuple[tuple[int, str], ...]


class TurnInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str


class State(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = ""
    turn: int = 0
    attempt: int = 0
    approved_sql: str | None = None
    history: tuple[str, ...] = ()
    result: Annotated[Rows | None, UntrackedValue] = None
    result_seen_at_start: Annotated[bool, UntrackedValue] = False
    result_seen_by_last_node: Annotated[bool, UntrackedValue] = False


def begin_turn(state: State) -> dict[str, object]:
    return {
        "turn": state.turn + 1,
        "attempt": 0,
        "approved_sql": None,
        "result_seen_at_start": state.result is not None,
    }


def begin_turn_without_reset(state: State) -> dict[str, object]:
    return {"turn": state.turn + 1}


def execute(state: State) -> dict[str, object]:
    if state.question == "no query":
        return {"attempt": state.attempt + 1}
    return {
        "attempt": state.attempt + 1,
        "approved_sql": "SELECT 1",
        "result": ((1, MARKER),),
    }


def remember(state: State) -> dict[str, object]:
    return {
        "history": (*state.history, state.question),
        "result_seen_by_last_node": state.result is not None,
    }


def build(*, reset: bool, saver: InMemorySaver) -> CompiledStateGraph[State, None, TurnInput, State]:
    graph = StateGraph(State, input_schema=TurnInput)
    graph.add_node("begin_turn", begin_turn if reset else begin_turn_without_reset)
    graph.add_node("execute", execute)
    graph.add_node("remember", remember)
    graph.add_edge(START, "begin_turn")
    graph.add_edge("begin_turn", "execute")
    graph.add_edge("execute", "remember")
    graph.add_edge("remember", END)
    return graph.compile(checkpointer=saver)


def thread(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def test_an_untracked_field_is_returned_but_never_checkpointed() -> None:
    saver = InMemorySaver()
    app = build(reset=True, saver=saver)
    config = thread("conversation-1")

    output = app.invoke(TurnInput(question="first"), config)
    assert output["result"] == ((1, MARKER),)
    assert output["result_seen_by_last_node"] is True

    snapshot = app.get_state(config)
    assert "result" not in snapshot.values
    assert snapshot.values["approved_sql"] == "SELECT 1"

    checkpoints = list(saver.list(config))
    assert len(checkpoints) >= 4
    for saved in checkpoints:
        assert "result" not in saved.checkpoint["channel_values"]
        assert all(channel != "result" for _, channel, _ in saved.pending_writes or [])
    everything_stored = repr((saver.storage, saver.writes, saver.blobs))
    assert "approved_sql" in everything_stored, "the search below looks in the right place"
    assert MARKER not in everything_stored


def test_an_untracked_field_does_not_carry_over_to_the_next_turn() -> None:
    saver = InMemorySaver()
    app = build(reset=True, saver=saver)
    config = thread("conversation-1")
    app.invoke(TurnInput(question="first"), config)

    output = app.invoke(TurnInput(question="no query"), config)
    assert output["result_seen_at_start"] is False
    assert "result" not in output or output["result"] is None


def test_tracked_fields_persist_across_turns_until_a_node_resets_them() -> None:
    config = thread("conversation-1")

    leaky = build(reset=False, saver=InMemorySaver())
    leaky.invoke(TurnInput(question="first"), config)
    second = leaky.invoke(TurnInput(question="no query"), config)
    assert second["turn"] == 2
    assert second["history"] == ("first", "no query")
    assert second["attempt"] == 2, "the attempt counter carried over from the first turn"
    assert second["approved_sql"] == "SELECT 1", "so did the first turn's approved query"

    clean = build(reset=True, saver=InMemorySaver())
    clean.invoke(TurnInput(question="first"), config)
    second = clean.invoke(TurnInput(question="no query"), config)
    assert second["turn"] == 2
    assert second["history"] == ("first", "no query")
    assert second["attempt"] == 1
    assert second["approved_sql"] is None


def test_another_thread_starts_empty() -> None:
    app = build(reset=True, saver=InMemorySaver())
    app.invoke(TurnInput(question="first"), thread("a"))
    other = app.invoke(TurnInput(question="no query"), thread("b"))
    assert (other["turn"], other["history"]) == (1, ("no query",))
