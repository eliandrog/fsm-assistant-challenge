"""Aggregation and rendering of an evaluation run.

Responsibility: turn per-turn scores into counts and into text. Pure
functions over plain values: no model, no database, no clock. A report
carries no timestamp and no file path, so two runs that scored the same are
byte-identical.

A case passes when every score of every turn passed. Results are counts per
tier, per tag and per split (tuned or held-out), always with the number of
cases behind them; no percentage is printed, because a tier holds only a handful of cases.

Two counts sit inside the others and are always shown next to them. "With
extra columns" is the part of the passed cases whose result was correct but
carried columns after the expected ones. "Known limitation" is the part of
the failed cases the suite marks as a known limitation of the system: they
are failures and stay in every total. A marked case that passes is listed
too, since its note is then out of date.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from fsm_assistant.evals.cases import TIERS, Case, Feature, Tier
from fsm_assistant.evals.scorers import FailureReason, Note, Score, ScorerName

Split = Literal["tuned", "held_out"]
Source = Literal["live", "replay"]


class TurnScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn: int  # counted from 1
    scores: tuple[Score, ...]

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
        return self.passed and any(
            score.note is Note.EXTRA_COLUMNS for turn in self.turns for score in turn.scores
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


class Summary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    info: RunInfo
    overall: Tally
    by_tier: tuple[Tally, ...]
    by_split: tuple[Tally, ...]
    by_tag: tuple[Tally, ...]
    by_scorer: tuple[Tally, ...]  # counted in scores, not in cases
    reasons: tuple[ReasonCount, ...]
    failures: tuple[Failure, ...]
    known_limitations: tuple[KnownLimitation, ...]
    paraphrases: tuple[ParaphrasePair, ...]


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


def render_json(summary: Summary) -> str:
    return summary.model_dump_json(indent=2) + "\n"


def render_markdown(summary: Summary) -> str:
    """The report as Markdown. The headline count is always followed by the per-tier table."""
    info, overall = summary.info, summary.overall
    lines = [
        "# Evaluation run",
        "",
        f"- Model: `{info.model_id}`, effort `{info.effort}`, output mode `{info.tool_mode}`, "
        f"prompt `{info.prompt_version}`",
        f"- Source of the model replies: {'recorded replies, replayed' if info.source == 'replay' else 'live model calls'}",
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
        'further columns followed them. "Known limitation" is a part of Failed.',
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
        f"| {heading} | Cases | Passed | With extra columns | Failed | Known limitation | Skipped |",
        "|---|---|---|---|---|---|---|",
        *(
            f"| {tally.name} | {tally.cases} | {tally.passed} | {tally.with_extra_columns} "
            f"| {tally.failed} | {tally.known_limitation} | {tally.skipped} |"
            for tally in tallies
        ),
    ]


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")
