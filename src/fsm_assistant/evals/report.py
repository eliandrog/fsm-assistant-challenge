"""Aggregation and rendering of an evaluation run.

Responsibility: turn per-turn scores into counts and into text. Pure
functions over plain values: no model, no database, no clock. A report
carries no timestamp and no file path, so two runs that scored the same are
byte-identical.

A case passes when every score of every turn passed. Results are counts per
tier, per tag and per split (tuned or held-out), always with the number of
cases behind them; no percentage is printed, because a tier holds only a handful of cases.

Three counts sit inside the others and are always shown next to them. "With
extra columns" is the part of the passed cases whose result was correct but
carried columns after the expected ones. "Stopped by guard" is the part of
the passed cases in which the guard refused a reply of the model on the way:
the turn ended well because code stopped something, which a clean pass does
not need. "Known limitation" is the part of the failed cases the suite marks
as a known limitation of the system: they are failures and stay in every
total. A marked case that passes is listed too, since its note is then out
of date.

`Operations` is how the turns went and what they cost: repairs, refusals by
code, write attempts, seconds per turn, tokens and an estimated cost. The
cost comes from the token counts and `PRICES_USD_PER_MILLION`, constants
written when this code was, with no lookup: check them before relying on it.

A repeated run (`summarise_repeats`) is counted per case as how many of its
repetitions passed, and per tier as the cases that passed all, some or none.
"""

import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from fsm_assistant.evals.cases import TIERS, Case, Feature, Tier
from fsm_assistant.evals.scorers import GUARD_CODES, FailureReason, Note, Score, ScorerName

Split = Literal["tuned", "held_out"]
# `rescored`: recorded replies scored again offline, to rebuild a record.
Source = Literal["live", "replay", "rescored"]
RefusedBy = Literal["guard", "shape_check"]

# US dollars per million tokens, input then output. Constants written with
# this code and never looked up: a price may have changed since.
PRICES_USD_PER_MILLION: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
}
PRICE_NOTE = (
    "Prices are constants written in the code when it was built, not looked up; "
    "they may be out of date."
)
_SOURCE_TEXT: dict[Source, str] = {
    "live": "live model calls",
    "replay": "recorded replies, replayed",
    "rescored": "recorded replies, scored again offline",
}


class TurnScore(BaseModel):
    """One turn's scores, with what the turn took. The defaults are a turn nothing is known of."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn: int  # counted from 1
    scores: tuple[Score, ...]
    model_calls: int = 1
    refused: tuple[str, ...] = ()  # one code per refused reply
    write_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return all(score.passed for score in self.scores)


class CaseResult(BaseModel):
    """One case of a run: its turns' scores, or why it was skipped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    tier: Tier
    tags: tuple[str, ...]
    tuned: bool
    paraphrase_of: str | None
    known_limitation: str | None = None
    skipped: str | None = None
    turns: tuple[TurnScore, ...] = ()

    @property
    def passed(self) -> bool:
        return self.skipped is None and bool(self.turns) and all(turn.passed for turn in self.turns)

    @property
    def failed(self) -> bool:
        return self.skipped is None and not self.passed

    @property
    def with_extra_columns(self) -> bool:
        return self._passed_with(Note.EXTRA_COLUMNS)

    @property
    def stopped_by_guard(self) -> bool:
        return self._passed_with(Note.STOPPED_BY_GUARD)

    def _passed_with(self, note: Note) -> bool:
        return self.passed and any(
            score.note is note for turn in self.turns for score in turn.scores
        )


class RunInfo(BaseModel):
    """What produced a run. Nothing here changes between two identical runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    effort: str
    tool_mode: str
    prompt_version: str
    source: Source
    schema_context_sha256: str
    suite_sha256: str


class Tally(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    cases: int
    passed: int
    failed: int
    skipped: int
    with_extra_columns: int = 0  # of `passed`
    stopped_by_guard: int = 0  # of `passed`
    known_limitation: int = 0  # of `failed`


class Failure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    tier: Tier
    turn: int
    scorer: ScorerName
    reason: FailureReason
    detail: str


class ReasonCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer: ScorerName
    reason: FailureReason
    count: int


class KnownLimitation(BaseModel):
    """A case the suite marks as a known limitation, and how it scored in this run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    tier: Tier
    reason: str
    passed: bool  # True: the note is out of date


class ParaphrasePair(BaseModel):
    """A held-out case and one rewording of it. None: that case was skipped or not run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    original: str
    paraphrase: str
    original_passed: bool | None
    paraphrase_passed: bool | None


class RefusalCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    by: RefusedBy
    count: int


class Cost(BaseModel):
    """An estimate in US dollars, from the token counts and the price table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_usd_per_million: float
    output_usd_per_million: float
    run_usd: float
    per_turn_usd: float | None  # one turn is one question; None when no turn ran


class Operations(BaseModel):
    """How the turns of a run went and what they took. Counted in turns, not cases."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turns: int
    model_requests: int
    repairs: int  # turns that needed a second model request
    refusals: tuple[RefusalCount, ...]  # refused replies by code
    write_attempts: int  # refused replies that tried to change data or the schema
    seconds_p50: float | None  # nearest-rank percentiles of the seconds per turn
    seconds_p95: float | None
    seconds_max: float | None
    input_tokens: int
    output_tokens: int
    input_tokens_per_turn: float | None
    output_tokens_per_turn: float | None
    cost: Cost | None  # None when the model has no price in the table


class Summary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    info: RunInfo
    operations: Operations
    overall: Tally
    by_tier: tuple[Tally, ...]
    by_split: tuple[Tally, ...]
    by_tag: tuple[Tally, ...]
    by_scorer: tuple[Tally, ...]  # counted in scores, not in cases
    reasons: tuple[ReasonCount, ...]
    failures: tuple[Failure, ...]
    known_limitations: tuple[KnownLimitation, ...]
    paraphrases: tuple[ParaphrasePair, ...]


class RepeatCase(BaseModel):
    """One case of a repeated run: how many of its repetitions passed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    tier: Tier
    tuned: bool
    repetitions: int
    passed: int


class RepeatTally(BaseModel):
    """Cases by how their repetitions went: every one passed, some did, none did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    cases: int
    passed_all: int
    passed_some: int
    passed_none: int


class RepeatSummary(BaseModel):
    """A run in which every case was sent `repetitions` times, each as its own conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    info: RunInfo
    repetitions: int
    operations: Operations  # over every repetition
    overall: RepeatTally
    by_tier: tuple[RepeatTally, ...]
    by_split: tuple[RepeatTally, ...]
    cases: tuple[RepeatCase, ...]


def skip_reason(case: Case, available: frozenset[Feature]) -> str | None:
    """Why the runner must skip `case`, or None when everything it needs is there."""
    if case.requires is not None and case.requires not in available:
        return f"needs {case.requires}, which is not built"
    return None


def case_result(
    case: Case, turns: Sequence[TurnScore] = (), *, skipped: str | None = None
) -> CaseResult:
    return CaseResult(
        case_id=case.id,
        tier=case.tier,
        tags=case.tags,
        tuned=case.tuned,
        paraphrase_of=case.paraphrase_of,
        known_limitation=case.known_limitation,
        skipped=skipped,
        turns=tuple(turns),
    )


def summarise(results: Sequence[CaseResult], info: RunInfo) -> Summary:
    """Counts per tier, split, tag and scorer, the failures, and the paraphrase pairs."""
    by_id = {result.case_id: result for result in results}

    def split(result: CaseResult) -> Split:
        return "tuned" if result.tuned else "held_out"

    failures = tuple(
        Failure(
            case_id=result.case_id,
            tier=result.tier,
            turn=turn.turn,
            scorer=score.scorer,
            reason=score.reason,
            detail=score.detail,
        )
        for result in results
        for turn in result.turns
        for score in turn.scores
        if score.reason is not None
    )
    reason_counts = Counter((failure.scorer, failure.reason) for failure in failures)
    scores = [score for result in results for turn in result.turns for score in turn.scores]
    scorers = sorted({score.scorer for score in scores})
    return Summary(
        info=info,
        operations=operations(results, info.model_id),
        overall=_tally("all", results),
        by_tier=tuple(
            _tally(tier, [result for result in results if result.tier == tier])
            for tier in TIERS
            if any(result.tier == tier for result in results)
        ),
        by_split=tuple(
            _tally(name, [result for result in results if split(result) == name])
            for name in ("tuned", "held_out")
        ),
        by_tag=_grouped(results, lambda result: result.tags),
        by_scorer=tuple(
            Tally(
                name=scorer,
                cases=sum(score.scorer == scorer for score in scores),
                passed=sum(score.scorer == scorer and score.passed for score in scores),
                failed=sum(score.scorer == scorer and not score.passed for score in scores),
                skipped=0,
                with_extra_columns=sum(
                    score.scorer == scorer and score.note is Note.EXTRA_COLUMNS for score in scores
                ),
                stopped_by_guard=sum(
                    score.scorer == scorer and score.note is Note.STOPPED_BY_GUARD
                    for score in scores
                ),
            )
            for scorer in scorers
        ),
        reasons=tuple(
            ReasonCount(scorer=scorer, reason=reason, count=count)
            for (scorer, reason), count in sorted(
                reason_counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1].value)
            )
        ),
        failures=failures,
        known_limitations=tuple(
            KnownLimitation(
                case_id=result.case_id,
                tier=result.tier,
                reason=result.known_limitation,
                passed=result.passed,
            )
            for result in results
            if result.known_limitation is not None and result.skipped is None
        ),
        paraphrases=tuple(
            ParaphrasePair(
                original=result.paraphrase_of,
                paraphrase=result.case_id,
                original_passed=_verdict(by_id.get(result.paraphrase_of)),
                paraphrase_passed=_verdict(result),
            )
            for result in results
            if result.paraphrase_of is not None
        ),
    )


def operations(results: Sequence[CaseResult], model_id: str) -> Operations:
    """The turns of `results` added up, with the cost estimate for `model_id`."""
    turns = [turn for result in results for turn in result.turns]
    seconds = sorted(turn.seconds for turn in turns)
    refused = Counter(code for turn in turns for code in turn.refused)
    tokens_in = sum(turn.input_tokens for turn in turns)
    tokens_out = sum(turn.output_tokens for turn in turns)
    price = PRICES_USD_PER_MILLION.get(model_id)
    cost: Cost | None = None
    if price is not None:
        run_usd = (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
        cost = Cost(
            input_usd_per_million=price[0],
            output_usd_per_million=price[1],
            run_usd=round(run_usd, 6),
            per_turn_usd=round(run_usd / len(turns), 6) if turns else None,
        )
    return Operations(
        turns=len(turns),
        model_requests=sum(turn.model_calls for turn in turns),
        repairs=sum(turn.model_calls > 1 for turn in turns),
        refusals=tuple(
            RefusalCount(
                code=code, by="guard" if code in GUARD_CODES else "shape_check", count=count
            )
            for code, count in sorted(refused.items(), key=lambda item: (-item[1], item[0]))
        ),
        write_attempts=sum(turn.write_attempts for turn in turns),
        seconds_p50=_percentile(seconds, 50),
        seconds_p95=_percentile(seconds, 95),
        seconds_max=seconds[-1] if seconds else None,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        input_tokens_per_turn=round(tokens_in / len(turns), 1) if turns else None,
        output_tokens_per_turn=round(tokens_out / len(turns), 1) if turns else None,
        cost=cost,
    )


def summarise_repeats(
    repetitions: Sequence[Sequence[CaseResult]], info: RunInfo
) -> RepeatSummary:
    """Count, per case, how many repetitions passed.

    A case counts when it ran in every repetition; one that was skipped, or
    is missing from a repetition, is left out.
    """
    by_id = [
        {result.case_id: result for result in results if result.skipped is None}
        for results in repetitions
    ]
    cases = tuple(
        RepeatCase(
            case_id=first.case_id,
            tier=first.tier,
            tuned=first.tuned,
            repetitions=len(by_id),
            passed=sum(results[first.case_id].passed for results in by_id),
        )
        for first in (repetitions[0] if repetitions else ())
        if all(first.case_id in results for results in by_id)
    )
    counted = {case.case_id for case in cases}
    return RepeatSummary(
        info=info,
        repetitions=len(by_id),
        operations=operations(
            [
                result
                for results in by_id
                for result in results.values()
                if result.case_id in counted
            ],
            info.model_id,
        ),
        overall=_repeat_tally("all", cases),
        by_tier=tuple(
            _repeat_tally(tier, [case for case in cases if case.tier == tier])
            for tier in TIERS
            if any(case.tier == tier for case in cases)
        ),
        by_split=(
            _repeat_tally("tuned", [case for case in cases if case.tuned]),
            _repeat_tally("held_out", [case for case in cases if not case.tuned]),
        ),
        cases=cases,
    )


def render_json(summary: Summary | RepeatSummary) -> str:
    return summary.model_dump_json(indent=2) + "\n"


def render_repeat_markdown(summary: RepeatSummary) -> str:
    """A repeated run as Markdown: counts of cases, never a rate."""
    info, overall, times = summary.info, summary.overall, summary.repetitions
    return "\n".join(
        [
            "# Evaluation run, repeated",
            "",
            f"- Model: `{info.model_id}`, effort `{info.effort}`, output mode `{info.tool_mode}`, "
            f"prompt `{info.prompt_version}`",
            f"- Source of the model replies: {_SOURCE_TEXT[info.source]}",
            f"- Schema context `{info.schema_context_sha256[:12]}`, cases `{info.suite_sha256[:12]}`",
            f"- Every case was sent {times} times, each time as a new conversation.",
            "",
            f"**{overall.passed_all} of {overall.cases} cases passed all {times} repetitions**; "
            f"{overall.passed_some} passed some and {overall.passed_none} passed none. "
            f"{times} repetitions show whether a result is stable; they are still a small "
            "sample, not a rate.",
            "",
            "## By tier",
            "",
            *_repeat_table("Tier", summary.by_tier, times),
            "",
            "## Tuned and held-out",
            "",
            *_repeat_table("Split", summary.by_split, times),
            "",
            "## Cases that did not pass every repetition",
            "",
            *(
                [
                    "| Case | Tier | Passed |",
                    "|---|---|---|",
                    *(
                        f"| {case.case_id} | {case.tier} | {case.passed} of {case.repetitions} |"
                        for case in summary.cases
                        if case.passed < case.repetitions
                    ),
                ]
                if overall.passed_all < overall.cases
                else ["None."]
            ),
            "",
            *operations_markdown(summary.operations),
            "",
            "## Every case",
            "",
            "| Case | Tier | Split | Passed |",
            "|---|---|---|---|",
            *(
                f"| {case.case_id} | {case.tier} | {'tuned' if case.tuned else 'held_out'} "
                f"| {case.passed} of {case.repetitions} |"
                for case in summary.cases
            ),
        ]
    ) + "\n"


def operations_lines(item: Operations) -> list[str]:
    """Repairs, refusals, write attempts, seconds, tokens and cost, as plain lines."""
    refusals = ", ".join(f"{entry.code} {entry.count}" for entry in item.refusals) or "none"
    lines = [
        f"Repairs (turns that needed a second model request): {item.repairs} of {item.turns} "
        f"turns; {item.model_requests} model requests.",
        f"Refused replies by code: {refusals}.",
        f"Write attempts stopped by the guard: {item.write_attempts}.",
    ]
    if (
        item.seconds_p50 is not None
        and item.seconds_p95 is not None
        and item.seconds_max is not None
    ):
        lines.append(
            f"Seconds per turn: p50 {item.seconds_p50:.2f}, p95 {item.seconds_p95:.2f}, "
            f"max {item.seconds_max:.2f}."
        )
    if item.input_tokens_per_turn is not None and item.output_tokens_per_turn is not None:
        lines.append(
            f"Tokens: {item.input_tokens:,} in and {item.output_tokens:,} out; per turn "
            f"{item.input_tokens_per_turn:,.0f} in and {item.output_tokens_per_turn:,.0f} out."
        )
    lines.append(_cost_text(item.cost))
    return lines


def operations_markdown(item: Operations) -> list[str]:
    return [
        "## Repairs, refusals and cost",
        "",
        "Counted in turns. A repair is a turn that needed its second model request. A refused "
        "reply was stopped by the guard or by the check of the reply's shape; a write attempt "
        "is a refused reply that tried to change data or the schema, under whatever code. "
        "Seconds are per turn, model time included; percentiles are nearest-rank.",
        "",
        *(f"- {line}" for line in operations_lines(item)),
    ]


def render_markdown(summary: Summary) -> str:
    """The report as Markdown. The headline count is always followed by the per-tier table."""
    info, overall = summary.info, summary.overall
    lines = [
        "# Evaluation run",
        "",
        f"- Model: `{info.model_id}`, effort `{info.effort}`, output mode `{info.tool_mode}`, "
        f"prompt `{info.prompt_version}`",
        f"- Source of the model replies: {_SOURCE_TEXT[info.source]}",
        f"- Schema context `{info.schema_context_sha256[:12]}`, cases `{info.suite_sha256[:12]}`",
        "",
        f"**{overall.passed} of {overall.cases - overall.skipped} cases passed** "
        f"({overall.cases} cases in the suite, {overall.skipped} skipped). "
        "Read it with the table below: the tiers are small and differ in kind, "
        "so one number does not describe them.",
        "",
        *breakdown_lines(summary, bullet="- "),
        "",
        "## By tier",
        "",
        '"With extra columns" is a part of Passed: the expected columns were correct and '
        'further columns followed them. "Stopped by guard" is a part of Passed too: the guard '
        'refused a reply of the model during the case. "Known limitation" is a part of Failed.',
        "",
        *_table("Tier", summary.by_tier),
        "",
        "## Tuned and held-out",
        "",
        "A tuned case has its question, or one nearly identical, in the prompt's examples or "
        "in the tool-call check, or is a case whose failure in an earlier run led to a line of "
        "the prompt. Held-out cases are the ones the prompt was not written against.",
        "",
        *_table("Split", summary.by_split),
        "",
        "## By scorer",
        "",
        "Counted in scores: a turn gets one score from each scorer that applies to it.",
        "",
        "| Scorer | Scores | Passed | Failed |",
        "|---|---|---|---|",
        *(
            f"| {tally.name} | {tally.cases} | {tally.passed} | {tally.failed} |"
            for tally in summary.by_scorer
        ),
        "",
        "## Failure reasons",
        "",
    ]
    if summary.reasons:
        lines += [
            "| Scorer | Reason | Count |",
            "|---|---|---|",
            *(f"| {item.scorer} | {item.reason.value} | {item.count} |" for item in summary.reasons),
        ]
    else:
        lines.append("No score failed.")
    lines += ["", *operations_markdown(summary.operations)]
    lines += ["", "## Known limitations", ""]
    if summary.known_limitations:
        lines += [
            "Cases the suite marks as a known limitation of the system. They are run and "
            "counted like any other case.",
            "",
            "| Case | This run | Why it is marked |",
            "|---|---|---|",
            *(
                f"| {item.case_id} | {_limitation_text(item.passed)} | {_cell(item.reason)} |"
                for item in summary.known_limitations
            ),
        ]
    else:
        lines.append("No case marked as a known limitation was run.")
    lines += ["", "## Paraphrases", ""]
    if summary.paraphrases:
        lines += [
            "| Held-out case | Result | Paraphrase | Result |",
            "|---|---|---|---|",
            *(
                f"| {pair.original} | {_verdict_text(pair.original_passed)} | {pair.paraphrase} "
                f"| {_verdict_text(pair.paraphrase_passed)} |"
                for pair in summary.paraphrases
            ),
        ]
    else:
        lines.append("No paraphrase case was run.")
    lines += ["", "## Failures", ""]
    if summary.failures:
        lines += [
            "| Case | Turn | Scorer | Reason | Detail |",
            "|---|---|---|---|---|",
            *(
                f"| {failure.case_id} | {failure.turn} | {failure.scorer} | {failure.reason.value} "
                f"| {_cell(failure.detail)} |"
                for failure in summary.failures
            ),
        ]
    else:
        lines.append("None.")
    lines += ["", "## By tag", "", "A case counts once under each of its tags.", "", *_table("Tag", summary.by_tag)]
    return "\n".join(lines) + "\n"


def breakdown_lines(summary: Summary, *, bullet: str = "") -> list[str]:
    """The two counts inside passed and failed, and any known-limitation case that passed."""
    overall = summary.overall
    lines = [
        f"{bullet}Correct, with extra columns: {overall.with_extra_columns} "
        f"(counted in the {overall.passed} passed)",
        f"{bullet}Passed after the guard refused a reply: {overall.stopped_by_guard} "
        f"(counted in the {overall.passed} passed)",
        f"{bullet}Failed, known limitation: {overall.known_limitation} "
        f"(counted in the {overall.failed} failed)",
    ]
    out_of_date = [item.case_id for item in summary.known_limitations if item.passed]
    if out_of_date:
        lines.append(
            f"{bullet}Marked as a known limitation but passed, so the note is out of date: "
            + ", ".join(out_of_date)
        )
    return lines


def _tally(name: str, results: Iterable[CaseResult]) -> Tally:
    group = list(results)
    skipped = sum(result.skipped is not None for result in group)
    passed = sum(result.passed for result in group)
    return Tally(
        name=name,
        cases=len(group),
        passed=passed,
        failed=len(group) - passed - skipped,
        skipped=skipped,
        with_extra_columns=sum(result.with_extra_columns for result in group),
        stopped_by_guard=sum(result.stopped_by_guard for result in group),
        known_limitation=sum(
            result.failed and result.known_limitation is not None for result in group
        ),
    )


def _grouped(
    results: Sequence[CaseResult], keys: Callable[[CaseResult], Iterable[str]]
) -> tuple[Tally, ...]:
    names = sorted({key for result in results for key in keys(result)})
    return tuple(
        _tally(name, [result for result in results if name in keys(result)]) for name in names
    )


def _verdict(result: CaseResult | None) -> bool | None:
    return None if result is None or result.skipped is not None else result.passed


def _limitation_text(passed: bool) -> str:
    return "passed: the note is out of date" if passed else "failed, as noted"


def _verdict_text(passed: bool | None) -> str:
    return "not run" if passed is None else "pass" if passed else "fail"


def _table(heading: str, tallies: Sequence[Tally]) -> list[str]:
    return [
        f"| {heading} | Cases | Passed | With extra columns | Stopped by guard | Failed "
        "| Known limitation | Skipped |",
        "|---|---|---|---|---|---|---|---|",
        *(
            f"| {tally.name} | {tally.cases} | {tally.passed} | {tally.with_extra_columns} "
            f"| {tally.stopped_by_guard} | {tally.failed} | {tally.known_limitation} "
            f"| {tally.skipped} |"
            for tally in tallies
        ),
    ]


def _repeat_tally(name: str, cases: Sequence[RepeatCase]) -> RepeatTally:
    return RepeatTally(
        name=name,
        cases=len(cases),
        passed_all=sum(case.passed == case.repetitions for case in cases),
        passed_some=sum(0 < case.passed < case.repetitions for case in cases),
        passed_none=sum(case.passed == 0 for case in cases),
    )


def _repeat_table(heading: str, tallies: Sequence[RepeatTally], times: int) -> list[str]:
    return [
        f"| {heading} | Cases | Passed all {times} | Passed some | Passed none |",
        "|---|---|---|---|---|",
        *(
            f"| {tally.name} | {tally.cases} | {tally.passed_all} | {tally.passed_some} "
            f"| {tally.passed_none} |"
            for tally in tallies
        ),
    ]


def _percentile(ordered: Sequence[float], percent: int) -> float | None:
    """The nearest-rank percentile of values already in ascending order."""
    if not ordered:
        return None
    return ordered[max(math.ceil(percent / 100 * len(ordered)), 1) - 1]


def _cost_text(cost: Cost | None) -> str:
    if cost is None:
        return f"Estimated cost: no price is written in the code for this model. {PRICE_NOTE}"
    per_turn = "" if cost.per_turn_usd is None else f", ${cost.per_turn_usd:.5f} per question"
    return (
        f"Estimated cost: ${cost.run_usd:.4f} for these turns{per_turn}, at "
        f"${cost.input_usd_per_million:g} per million input tokens and "
        f"${cost.output_usd_per_million:g} per million output tokens. {PRICE_NOTE}"
    )


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")
