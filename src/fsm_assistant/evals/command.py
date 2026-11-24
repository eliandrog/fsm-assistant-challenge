"""What `fsm-assistant eval` does.

Responsibility: put the pieces of the suite together for the command: load
and filter the cases, list them, say what a live run would send, run it or
replay it, and write the lines the terminal shows and the two report files.

It prints nothing itself and chooses no exit code. Text goes to the `emit`
callback, one line or block at a time, so a case's line appears when that
case finishes; `execute` returns how the command ended and `cli.py` turns
that into an exit code. A live client is built only by the `live_model`
function it is handed, and only after `--yes-spend`; a replay never calls it.

Reports go to `evals/results/<model id>/`: `report.md` and `report.json`
after a live run, covering every case in the run's record, and `replay.md`
and `replay.json` after a replay, covering the cases replayed.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from fsm_assistant import db
from fsm_assistant.config import PROJECT_ROOT, Settings
from fsm_assistant.evals.cases import (
    DEFAULT_CASES_DIRECTORY,
    DEFAULT_PINS_FILE,
    TIERS,
    Case,
    CaseFileError,
    Feature,
    Suite,
    load_suite,
)
from fsm_assistant.evals.recorded import (
    RUN_FILE,
    Replay,
    RunTotals,
    record_run,
    recorded_results,
    replay_run,
    run_directory,
    run_info,
    totals,
)
from fsm_assistant.evals.report import (
    Summary,
    Tally,
    breakdown_lines,
    render_json,
    render_markdown,
    skip_reason,
    summarise,
)
from fsm_assistant.evals.runner import (
    CaseRun,
    Selection,
    UnknownCase,
    available_features,
    select,
    to_result,
)
from fsm_assistant.evals.scorers import FailureReason
from fsm_assistant.llm import IntentModel, ModelSpec, ModelUnavailable, StaleRecording
from fsm_assistant.nl2sql import model_spec
from fsm_assistant.tracing import Tracer, describe

RESULTS_DIR = PROJECT_ROOT / "evals" / "results"

# The system text with the schema context. The first recorded run, on prompt
# turn-3, averaged about 4,470 per request; the two lines added since are estimated.
TYPICAL_INPUT_TOKENS = 4_600

# How the command ended. `not_reproduced`: a replay scored differently from
# its record, or a selected case has no recording.
Finish = Literal["passed", "failed", "cannot_start", "not_reproduced"]

type Emit = Callable[[str], None]
type LiveModel = Callable[[ModelSpec, Settings], IntentModel]


class Options(BaseModel):
    """The command line of `eval`, parsed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str | None = None
    spend: bool = False
    replay: bool = False
    list_only: bool = False
    selection: Selection = Selection()


def execute(
    options: Options,
    settings: Settings,
    emit: Emit,
    *,
    live_model: LiveModel,
    tracer_for: Callable[[Settings], Tracer | None] | None = None,
    cases_directory: Path = DEFAULT_CASES_DIRECTORY,
    pins_file: Path = DEFAULT_PINS_FILE,
) -> Finish:
    """Run the command. Sends nothing unless `options.spend` is set and `replay` is not.

    `tracer_for` builds the tracer of a live run, when hosted tracing is on.
    It is not called for a replay: a replay is never traced.
    """
    try:
        suite = load_suite(cases_directory, pins_file)
        cases = select(suite.cases, options.selection)
    except (CaseFileError, UnknownCase) as error:
        emit(f"Cannot start: {error}")
        return "cannot_start"
    if not cases:
        emit("Cannot start: no case matches the filters.")
        return "cannot_start"
    if options.list_only:
        emit(render_list(cases, total=len(suite.cases)))
        return "passed"

    if options.model is not None:
        settings = settings.model_copy(update={"anthropic_model": options.model})
    spec = model_spec(settings)
    directory = run_directory(spec.model)
    if directory is None:
        emit(f"Cannot name a recordings folder after model id {spec.model!r}.")
        return "cannot_start"
    try:
        available = available_features(settings)
        if options.replay:
            return _replay(cases, suite, settings, spec, directory, available, emit,
                           cases_directory, pins_file)  # fmt: skip
        emit(render_plan(spec, cases, available, whole_suite=options.selection.is_everything))
        if not options.spend:
            key = "set" if settings.anthropic_api_key is not None else "NOT set"
            emit(f"  ANTHROPIC_API_KEY is {key}.")
            emit(
                "Nothing was sent. --list prints the cases; to run them, add --yes-spend. "
                "A run records its replies; --replay re-runs them offline."
            )
            return "passed"
        try:
            client = live_model(spec, settings)
        except ModelUnavailable as error:
            emit(f"Cannot start: {error}")
            return "cannot_start"
        tracer = None if tracer_for is None else tracer_for(settings)
        if tracer_for is not None:
            emit(describe(tracer, settings))
        try:
            return _live(cases, suite, settings, client, directory, available, emit,
                         cases_directory, pins_file, tracer)  # fmt: skip
        finally:
            if tracer is not None:
                tracer.close()
    except db.DbError as error:
        emit(f"Cannot start: the database could not be read. {error}")
        return "cannot_start"


def display_path(path: Path) -> str:
    if path.is_relative_to(PROJECT_ROOT):
        return path.relative_to(PROJECT_ROOT).as_posix()
    return str(path)


def render_list(cases: Sequence[Case], *, total: int) -> str:
    """The selected cases, one per line, then how many per tier."""
    width = max(len(case.id) for case in cases)
    lines = [f"{len(cases)} of {total} cases selected:"]
    lines += [
        f"  {case.id:<{width}}  {case.tier:<12}  {'tuned' if case.tuned else 'held-out':<8}  "
        f"{len(case.turns)} turn{'' if len(case.turns) == 1 else 's'}"
        + (f"  needs {case.requires}" if case.requires else "")
        for case in cases
    ]
    counts = Counter(case.tier for case in cases)
    tuned = Counter(case.tier for case in cases if case.tuned)
    lines += ["", f"{'tier':<12}  {'cases':>5}  {'tuned':>5}  {'held-out':>8}"]
    lines += [
        f"{tier:<12}  {counts[tier]:>5}  {tuned[tier]:>5}  {counts[tier] - tuned[tier]:>8}"
        for tier in TIERS
        if counts[tier]
    ]
    all_tuned = sum(tuned.values())
    lines.append(f"{'total':<12}  {len(cases):>5}  {all_tuned:>5}  {len(cases) - all_tuned:>8}")
    lines.append(f"turns: {sum(len(case.turns) for case in cases)}")
    return "\n".join(lines)


def render_plan(
    spec: ModelSpec, cases: Sequence[Case], available: frozenset[Feature], *, whole_suite: bool
) -> str:
    """What a live run would send and the most it could cost."""
    to_run = [case for case in cases if skip_reason(case, available) is None]
    skipped = len(cases) - len(to_run)
    turns = sum(len(case.turns) for case in to_run)
    requests = 2 * turns
    what = "the whole suite" if whole_suite else "a filtered run"
    return "\n".join(
        (
            f"eval: {len(to_run)} cases ({turns} turns) to {spec.model} in {spec.tool_mode} mode, "
            f"{what}; {skipped} skipped",
            f"  effort {spec.effort}, at most {spec.max_tokens:,} output tokens per request, "
            f"prompt {spec.prompt_version}",
            "  COST WARNING: this calls the Anthropic API and is billed to the key in "
            "ANTHROPIC_API_KEY.",
            f"  Upper bound: {requests} requests (one per turn, and one more where the first "
            f"reply is refused) of about {TYPICAL_INPUT_TOKENS:,} input tokens each, "
            f"{requests * TYPICAL_INPUT_TOKENS:,} in total, and up to "
            f"{requests * spec.max_tokens:,} output tokens; a typical run is close to {turns} "
            "requests and a few hundred output tokens each.",
        )
    )


def progress_line(run: CaseRun, *, known_limitation: bool = False) -> str:
    """One case of a run: its id, pass or fail, and the reason code of each failed score."""
    if run.skipped is not None:
        return f"SKIP  {run.case_id}  ({run.skipped})"
    if run.passed:
        noted = any(score.note is not None for turn in run.turns for score in turn.scores)
        return (
            f"PASS  {run.case_id}"
            + ("  (with extra columns)" if noted else "")
            + ("  (marked as a known limitation: the note is out of date)" if known_limitation else "")
        )
    reasons: list[str] = []
    for turn in run.turns:
        for score in turn.scores:
            if score.reason is None:
                continue
            kind = f" ({turn.error_kind})" if score.reason is FailureReason.TURN_FAILED else ""
            where = f"turn {turn.turn} " if len(run.turns) > 1 else ""
            reasons.append(f"{where}{score.scorer}: {score.reason.value}{kind}")
    marked = "(known limitation)  " if known_limitation else ""
    return f"FAIL  {run.case_id}  {marked}{'; '.join(reasons)}"


def render_summary(summary: Summary, run_totals: RunTotals) -> str:
    """The tables shown after the progress lines. Counts only, never one percentage."""
    overall = summary.overall
    lines = [
        "",
        f"{overall.passed} of {overall.cases - overall.skipped} cases passed "
        f"({overall.failed} failed, {overall.skipped} skipped). Read it by tier:",
        *(f"  {line}" for line in breakdown_lines(summary)),
        "",
        *_tally_table("tier", summary.by_tier),
        "",
        *_tally_table("split", summary.by_split),
        "",
    ]
    if summary.reasons:
        lines.append("Failure reasons (counted in scores):")
        lines += [
            f"  {item.count:>3}  {item.scorer}: {item.reason.value}" for item in summary.reasons
        ]
    else:
        lines.append("No score failed.")
    lines += [
        "",
        f"Totals: {run_totals.turns} turns, {run_totals.model_calls} model requests, "
        f"{run_totals.input_tokens:,} input tokens, {run_totals.output_tokens:,} output tokens, "
        f"{run_totals.seconds:.1f} s in turns.",
    ]
    return "\n".join(lines)


def write_reports(directory: Path, name: str, summary: Summary) -> tuple[Path, Path]:
    """Write `<name>.md` and `<name>.json` in `directory`; return the two paths."""
    directory.mkdir(parents=True, exist_ok=True)
    markdown, data = directory / f"{name}.md", directory / f"{name}.json"
    markdown.write_text(render_markdown(summary), encoding="utf-8")
    data.write_text(render_json(summary), encoding="utf-8")
    return markdown, data


def _live(
    cases: Sequence[Case],
    suite: Suite,
    settings: Settings,
    client: IntentModel,
    directory: Path,
    available: frozenset[Feature],
    emit: Emit,
    cases_directory: Path,
    pins_file: Path,
    tracer: Tracer | None,
) -> Finish:
    by_id = {case.id: case for case in cases}
    live = record_run(
        cases, suite, settings, client, directory, available=available,
        on_case=lambda run: emit(_progress(run, by_id)),
        cases_directory=cases_directory, pins_file=pins_file, tracer=tracer,
    )  # fmt: skip
    record = live.record
    info = run_info(record, "live")
    if live.runs:
        emit(
            render_summary(
                summarise([to_result(by_id[run.case_id], run) for run in live.runs], info),
                totals(live.runs),
            )
        )
    emit("")
    if live.restarted:
        emit(
            f"The earlier {RUN_FILE} was made under other settings (model, effort, token limit, "
            "output mode, prompt version or schema context), so the record was started again: "
            "only the cases of this run are in it, and the recorded replies of the earlier run "
            "were removed."
        )
    emit(
        f"Recorded in {display_path(directory)}: {RUN_FILE} now holds {len(record.cases)} "
        f"case(s), {len(live.runs)} of them from this run."
    )
    if live.runs:
        whole = summarise(recorded_results(record, suite), info)
        markdown, data = write_reports(RESULTS_DIR / record.model, "report", whole)
        emit(
            f"Reports of all {whole.overall.cases} recorded case(s): {display_path(markdown)} "
            f"and {display_path(data)}."
        )
    if live.not_replayable:
        emit(
            "A request got no reply in: " + ", ".join(live.not_replayable) + ". Those cases "
            "are scored as failed and cannot be replayed; run them again with --case."
        )
    if live.stopped_by is not None:
        emit(
            f"Stopped after {len(live.runs)} case(s): {live.stopped_by}. "
            "Incomplete run; what ran before it is recorded."
        )
        return "cannot_start"
    return "passed" if all(run.passed or run.skipped for run in live.runs) else "failed"


def _replay(
    cases: Sequence[Case],
    suite: Suite,
    settings: Settings,
    spec: ModelSpec,
    directory: Path,
    available: frozenset[Feature],
    emit: Emit,
    cases_directory: Path,
    pins_file: Path,
) -> Finish:
    emit(
        f"eval --replay: {len(cases)} case(s) from the recorded replies of {spec.model} in "
        f"{display_path(directory)}. Nothing is sent."
    )
    by_id = {case.id: case for case in cases}
    try:
        replay = replay_run(
            cases, suite, settings, directory, available=available,
            on_case=lambda run: emit(_progress(run, by_id)),
            cases_directory=cases_directory, pins_file=pins_file,
        )  # fmt: skip
    except StaleRecording as error:
        emit(f"Cannot replay: {error}")
        emit("Recordings are made by a paid run: eval --yes-spend.")
        return "cannot_start"
    info = run_info(replay.record, "replay")
    if replay.runs:
        summary = summarise([to_result(by_id[run.case_id], run) for run in replay.runs], info)
        emit(render_summary(summary, totals(replay.runs)))
        emit("Tokens and seconds are the recorded ones.")
        markdown, data = write_reports(RESULTS_DIR / replay.record.model, "replay", summary)
        emit(f"Reports of this replay: {display_path(markdown)} and {display_path(data)}.")
    emit("")
    emit(render_comparison(replay))
    if not replay.reproduced:
        return "not_reproduced"
    return "passed" if all(run.passed or run.skipped for run in replay.runs) else "failed"


def render_comparison(replay: Replay) -> str:
    """How a replay stands next to `run.json`: what changed, what is missing, what differs."""
    lines: list[str] = []
    if replay.suite_changed:
        lines.append(
            "The case files or pins changed since the latest recorded run (the suite digest "
            "differs)."
        )
    if replay.changed_cases:
        lines.append("Changed since they were recorded: " + ", ".join(replay.changed_cases) + ".")
    if replay.not_recorded:
        lines.append(
            f"No recording for {len(replay.not_recorded)} selected case(s), which were not "
            "run: " + ", ".join(replay.not_recorded) + "."
        )
    if replay.differences:
        lines.append(f"{len(replay.differences)} case(s) did not score as recorded in {RUN_FILE}:")
        lines += [f"  {item.case_id}: {item.detail}" for item in replay.differences]
    elif replay.runs:
        lines.append(
            f"All {len(replay.runs)} replayed case(s) scored exactly as recorded in {RUN_FILE}."
        )
    return "\n".join(lines)


def _progress(run: CaseRun, by_id: dict[str, Case]) -> str:
    return progress_line(run, known_limitation=by_id[run.case_id].known_limitation is not None)


def _tally_table(heading: str, tallies: Sequence[Tally]) -> list[str]:
    width = max([len(heading), *(len(tally.name) for tally in tallies)])
    return [
        f"{heading:<{width}}  {'cases':>5}  {'passed':>6}  {'extra cols':>10}  {'failed':>6}  "
        f"{'known lim.':>10}  {'skipped':>7}",
        *(
            f"{tally.name:<{width}}  {tally.cases:>5}  {tally.passed:>6}  "
            f"{tally.with_extra_columns:>10}  {tally.failed:>6}  {tally.known_limitation:>10}  "
            f"{tally.skipped:>7}"
            for tally in tallies
        ),
    ]
