"""The evaluation runner: cases through the turn graph, then the scorers.

Responsibility: choose the cases of a run (`select`), send each one through
`Assistant.run_turn`, the entry point every interface uses, turn each
`TurnResult` into the scorers' `Observation` and score it. One case is one
conversation with its own `Assistant`, so nothing is remembered from one
case to the next; the turns of a case share that conversation.

The model is handed in as a factory, one `IntentModel` per case, so a run
can be live, recorded, replayed or scripted without this module knowing
which. It builds no client, writes no file and prints nothing: `recorded`
owns the files of a run and `command` its text.

Cases run one after another. A run is a few hundred requests at most, the
order of the output stays the order of the suite, and a failure is easier
to read when nothing else was in flight.
"""

import uuid
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fsm_assistant.config import Settings
from fsm_assistant.evals.cases import Case, Feature, Suite, Tier
from fsm_assistant.evals.evidence import score_turn
from fsm_assistant.evals.report import CaseResult, TurnScore, case_result, skip_reason
from fsm_assistant.evals.scorers import Observation, RuleMetrics, Score
from fsm_assistant.graph import Assistant
from fsm_assistant.llm import IntentModel, ModelReply, ScriptedModel, TokenUsage, ToolCall
from fsm_assistant.nl2sql import ToolName
from fsm_assistant.result import ErrorKind, Outcome, RuleCard, TurnResult

Split = Literal["tuned", "held_out"]

type ModelFactory = Callable[[Case], IntentModel]


class Selection(BaseModel):
    """Which cases a run takes. An empty filter leaves every case in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tiers: tuple[Tier, ...] = ()
    case_ids: tuple[str, ...] = ()
    tag: str | None = None
    split: Split | None = None
    limit: int | None = Field(default=None, ge=1)

    @property
    def is_everything(self) -> bool:
        return self == Selection()


class UnknownCase(Exception):
    """A selection names a case id the suite does not have."""

    def __init__(self, case_ids: Sequence[str]) -> None:
        super().__init__(f"no case with id: {', '.join(case_ids)}")
        self.case_ids = tuple(case_ids)


class TurnRun(BaseModel):
    """One turn of a run: what the turn did, what it cost and how it scored.

    `sql` and `where_clause` are the text that ran, which the model wrote.
    No row of a result is kept here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn: int  # counted from 1
    outcome: Outcome
    error_kind: ErrorKind | None
    tool: ToolName | None
    refused: tuple[str, ...]  # one code per reply the shape check or the guard refused
    sql: str | None
    where_clause: str | None
    model_calls: int
    input_tokens: int
    output_tokens: int
    seconds: float
    scores: tuple[Score, ...]

    @property
    def passed(self) -> bool:
        return all(score.passed for score in self.scores)


class CaseRun(BaseModel):
    """One case of a run: its turns, or why it was skipped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    skipped: str | None = None
    turns: tuple[TurnRun, ...] = ()

    @property
    def passed(self) -> bool:
        return self.skipped is None and bool(self.turns) and all(turn.passed for turn in self.turns)


def select(cases: Sequence[Case], selection: Selection) -> tuple[Case, ...]:
    """The cases `selection` keeps, in suite order. Raises `UnknownCase` for an id not there."""
    unknown = sorted(set(selection.case_ids) - {case.id for case in cases})
    if unknown:
        raise UnknownCase(unknown)
    kept = [
        case
        for case in cases
        if (not selection.tiers or case.tier in selection.tiers)
        and (not selection.case_ids or case.id in selection.case_ids)
        and (selection.tag is None or selection.tag in case.tags)
        and (selection.split is None or (selection.split == "tuned") == case.tuned)
    ]
    return tuple(kept if selection.limit is None else kept[: selection.limit])


def available_features(settings: Settings) -> frozenset[Feature]:
    """The optional parts of the system that are built, found by trying them.

    The scan counts as built when a turn whose model reply asks for it ends
    without an error. The reply is scripted: nothing is sent.
    """
    probe = ModelReply(
        tool_mode="tools",
        tool_calls=(ToolCall(name="profile_fraud", arguments={}),),
        text="",
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=0, output_tokens=0),
        model_id="probe",
        response_id=None,
        request_hash="",
    )
    result = Assistant(settings, ScriptedModel((probe,))).run_turn(
        "Where is fraud concentrated?", uuid.uuid4().hex
    )
    return frozenset({"scan"}) if result.error is None else frozenset()


def observe(result: TurnResult) -> Observation:
    """What the scorers read of a turn's result."""
    table = result.table
    return Observation(
        outcome=result.outcome,
        action=result.tool,
        sql=result.sql,
        where_clause=result.where_clause,
        rows=None if table is None else table.rows,
        truncated=table is not None and table.truncated,
        backtest=None if result.rule is None else _metrics(result.rule),
        caveats=tuple(caveat.code for caveat in result.caveats),
        rejections=tuple(str(code) for code in result.refused),
        model_calls=result.meta.model_calls,
    )


def run_case(case: Case, suite: Suite, settings: Settings, model: IntentModel) -> CaseRun:
    """Send every turn of `case` through one new conversation and score each."""
    assistant = Assistant(settings, model)
    conversation = uuid.uuid4().hex
    turns: list[TurnRun] = []
    for number, turn in enumerate(case.turns, start=1):
        result = assistant.run_turn(turn.question, conversation)
        scores = score_turn(
            turn,
            observe(result),
            suite.pin(case, number),
            db_path=settings.db_path,
            timeout_seconds=settings.query_timeout_seconds,
        )
        turns.append(
            TurnRun(
                turn=number,
                outcome=result.outcome,
                error_kind=None if result.error is None else result.error.kind,
                tool=result.tool,
                refused=tuple(str(code) for code in result.refused),
                sql=result.sql,
                where_clause=result.where_clause,
                model_calls=result.meta.model_calls,
                input_tokens=result.meta.input_tokens,
                output_tokens=result.meta.output_tokens,
                seconds=round(result.meta.elapsed_ms / 1000, 3),
                # As plain scores: a rule-match score keeps its ratios in `detail`.
                scores=tuple(Score.model_validate(score.model_dump(include=_SCORE)) for score in scores),
            )
        )
    return CaseRun(case_id=case.id, turns=tuple(turns))


def run_suite(
    cases: Sequence[Case],
    suite: Suite,
    settings: Settings,
    model_for: ModelFactory,
    *,
    available: frozenset[Feature],
    on_case: Callable[[CaseRun], None] | None = None,
) -> tuple[CaseRun, ...]:
    """Run `cases` in order. A case that needs a part not in `available` is skipped."""
    runs: list[CaseRun] = []
    for case in cases:
        reason = skip_reason(case, available)
        if reason is not None:
            run = CaseRun(case_id=case.id, skipped=reason)
        else:
            run = run_case(case, suite, settings, model_for(case))
        runs.append(run)
        if on_case is not None:
            on_case(run)
    return tuple(runs)


def to_result(case: Case, run: CaseRun) -> CaseResult:
    """A case's run in the form `report.summarise` counts."""
    return case_result(
        case,
        [TurnScore(turn=turn.turn, scores=turn.scores) for turn in run.turns],
        skipped=run.skipped,
    )


_SCORE = set(Score.model_fields)


def _metrics(rule: RuleCard) -> RuleMetrics:
    return RuleMetrics.model_validate(rule.model_dump(include=set(RuleMetrics.model_fields)))
