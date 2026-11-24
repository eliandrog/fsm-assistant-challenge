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
after a live run or a rescore, covering every case in the run's record, and
`replay.md` and `replay.json` after a replay, covering the cases replayed. A
repeated run writes `repeat.md` and `repeat.json`, and its replay
`replay-repeat.md` and `replay-repeat.json`.

How the command ends (`Finish`), which `cli.py` turns into an exit code:

| Finish | Exit | When |
|---|---|---|
| `passed` | 0 | `--list`; nothing was sent; a live run in which every case passed; a rescore that wrote its record |
| `reproduced` | 0 | a replay in which every selected case was replayed and scored exactly as recorded, failures included |
| `failed` | 1 | a live run in which a case failed |
| `cannot_start` | 2 | bad filters, no record, a stale recording, no key, or a live run that had to stop |
| `not_reproduced` | 3 | a replay in which a case scored differently from its record, or nothing was replayed |
| `partly_replayed` | 5 | a replay in which every replayed case scored exactly as recorded, but some selected cases have no recording and were not run |

So a replay that exits 0 covered the whole selection. 4 is not used by
`eval`: `ask` uses it for a turn the guard rejected.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    LiveRun,
    Replay,
    RunTotals,
    read_record,
    record_run,
    recorded_results,
    repeat_directory,
    replay_run,
    rescore_run,
    run_directory,
    run_info,
    totals,
)
from fsm_assistant.evals.report import (
    PRICE_NOTE,
    PRICES_USD_PER_MILLION,
    CaseResult,
    RepeatSummary,
    RepeatTally,
    Summary,
    Tally,
    breakdown_lines,
    operations_lines,
    render_json,
    render_markdown,
    render_repeat_markdown,
    skip_reason,
    summarise,
    summarise_repeats,
)
from fsm_assistant.evals.runner import (
    CaseRun,
    Selection,
    UnknownCase,
    available_features,
    select,
    to_result,
)
from fsm_assistant.evals.scorers import FailureReason, Note
from fsm_assistant.llm import IntentModel, ModelSpec, ModelUnavailable, StaleRecording
from fsm_assistant.nl2sql import model_spec
from fsm_assistant.tracing import Tracer, describe

RESULTS_DIR = PROJECT_ROOT / "evals" / "results"

# The system text with the schema context. The first recorded run, on prompt
# turn-3, averaged about 4,470 per request; the two lines added since are estimated.
TYPICAL_INPUT_TOKENS = 4_600
# What a reply took on average in the recorded run on prompt turn-4.
TYPICAL_OUTPUT_TOKENS = 350

# How the command ended; the table is in the module docstring.
Finish = Literal[
    "passed", "reproduced", "failed", "cannot_start", "not_reproduced", "partly_replayed"
]
_NOTE_TEXT: dict[Note, str] = {
    Note.EXTRA_COLUMNS: "with extra columns",
    Note.STOPPED_BY_GUARD: "after the guard refused a reply",
}

type Emit = Callable[[str], None]
type LiveModel = Callable[[ModelSpec, Settings], IntentModel]


class Options(BaseModel):
    """The command line of `eval`, parsed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str | None = None
    spend: bool = False
    replay: bool = False
    list_only: bool = False
    rescore: bool = False  # rebuild the record from its recorded replies; sends nothing
    repeat: int = Field(default=1, ge=1)  # send every case this many times
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
        if options.rescore:
            return _rescore(suite, settings, spec, directory, available, emit,
                            cases_directory, pins_file)  # fmt: skip
        if options.replay and options.repeat > 1:
            return _replay_repeats(cases, suite, settings, spec, directory, available, emit,
                                   cases_directory, pins_file, options.repeat)  # fmt: skip
        if options.replay:
            return _replay(cases, suite, settings, spec, directory, available, emit,
                           cases_directory, pins_file)  # fmt: skip
        emit(
            render_plan(
                spec, cases, available,
                whole_suite=options.selection.is_everything, repeat=options.repeat,
            )
        )  # fmt: skip
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
        unknown = _unknown_settings(directory)
        if unknown and options.repeat == 1 and not options.selection.is_everything:
            emit(
                f"Cannot start: {RUN_FILE} was made before {', '.join(unknown)} were kept, so "
                "a run of some of the cases cannot be merged into it. Rebuild it first with "
                "eval --rescore, which sends nothing, or run the whole suite, which starts "
                "the record again."
            )
            return "cannot_start"
        tracer = None if tracer_for is None else tracer_for(settings)
        if tracer_for is not None:
            emit(describe(tracer, settings))
        try:
            if options.repeat > 1:
                return _live_repeats(cases, suite, settings, client, directory, available, emit,
                                     cases_directory, pins_file, tracer, options.repeat)  # fmt: skip
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
    tier_width = max(len(tier) for tier in TIERS)
    lines = [f"{len(cases)} of {total} cases selected:"]
    lines += [
        f"  {case.id:<{width}}  {case.tier:<{tier_width}}  {'tuned' if case.tuned else 'held-out':<8}  "
        f"{len(case.turns)} turn{'' if len(case.turns) == 1 else 's'}"
        + (f"  needs {case.requires}" if case.requires else "")
        for case in cases
    ]
    counts = Counter(case.tier for case in cases)
    tuned = Counter(case.tier for case in cases if case.tuned)
    lines += ["", f"{'tier':<{tier_width}}  {'cases':>5}  {'tuned':>5}  {'held-out':>8}"]
    lines += [
        f"{tier:<{tier_width}}  {counts[tier]:>5}  {tuned[tier]:>5}  "
        f"{counts[tier] - tuned[tier]:>8}"
        for tier in TIERS
        if counts[tier]
    ]
    all_tuned = sum(tuned.values())
    lines.append(
        f"{'total':<{tier_width}}  {len(cases):>5}  {all_tuned:>5}  {len(cases) - all_tuned:>8}"
    )
    lines.append(f"turns: {sum(len(case.turns) for case in cases)}")
    return "\n".join(lines)


def render_plan(
    spec: ModelSpec,
    cases: Sequence[Case],
    available: frozenset[Feature],
    *,
    whole_suite: bool,
    repeat: int = 1,
) -> str:
    """What a live run would send and the most it could cost. Everything scales with `repeat`."""
    to_run = [case for case in cases if skip_reason(case, available) is None]
    skipped = len(cases) - len(to_run)
    turns = sum(len(case.turns) for case in to_run) * repeat
    requests = 2 * turns
    what = "the whole suite" if whole_suite else "a filtered run"
    times = f", each case sent {repeat} times as separate conversations" if repeat > 1 else ""
    price = PRICES_USD_PER_MILLION.get(spec.model)
    typical = (
        "no price is written in the code for this model"
        if price is None
        else f"about ${turns * (TYPICAL_INPUT_TOKENS * price[0] + TYPICAL_OUTPUT_TOKENS * price[1]) / 1_000_000:.2f} "
        f"at ${price[0]:g} and ${price[1]:g} per million input and output tokens"
    )
    return "\n".join(
        (
            f"eval: {len(to_run)} cases ({turns} turns) to {spec.model} in {spec.tool_mode} mode, "
            f"{what}{times}; {skipped} skipped",
            f"  effort {spec.effort}, at most {spec.max_tokens:,} output tokens per request, "
            f"prompt {spec.prompt_version}",
            "  COST WARNING: this calls the Anthropic API and is billed to the key in "
            "ANTHROPIC_API_KEY.",
            f"  Upper bound: {requests} requests (one per turn, and one more where the first "
            f"reply is refused) of about {TYPICAL_INPUT_TOKENS:,} input tokens each, "
            f"{requests * TYPICAL_INPUT_TOKENS:,} in total, and up to "
            f"{requests * spec.max_tokens:,} output tokens; a typical run is close to {turns} "
            "requests and a few hundred output tokens each.",
            f"  Typical cost ({turns} requests of {TYPICAL_INPUT_TOKENS:,} input and "
            f"{TYPICAL_OUTPUT_TOKENS} output tokens): {typical}. {PRICE_NOTE}",
        )
    )


def progress_line(run: CaseRun, *, known_limitation: bool = False) -> str:
    """One case of a run: its id, pass or fail, and the reason code of each failed score."""
    if run.skipped is not None:
        return f"SKIP  {run.case_id}  ({run.skipped})"
    if run.passed:
        notes = {score.note for turn in run.turns for score in turn.scores if score.note}
        return (
            f"PASS  {run.case_id}"
            + "".join(f"  ({text})" for note, text in _NOTE_TEXT.items() if note in notes)
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
        *(f"  {line}" for line in operations_lines(summary.operations)),
    ]
    return "\n".join(lines)


def render_repeat_summary(summary: RepeatSummary) -> str:
    """A repeated run in the terminal: cases by how many of their repetitions passed."""
    overall, times = summary.overall, summary.repetitions
    unstable = [case for case in summary.cases if case.passed < case.repetitions]
    lines = [
        "",
        f"{overall.passed_all} of {overall.cases} cases passed all {times} repetitions; "
        f"{overall.passed_some} passed some, {overall.passed_none} passed none. "
        "A small sample, not a rate.",
        "",
        *_repeat_table("tier", summary.by_tier, times),
        "",
        *_repeat_table("split", summary.by_split, times),
        "",
    ]
    if unstable:
        lines.append("Cases that did not pass every repetition:")
        lines += [f"  {case.passed} of {case.repetitions}  {case.case_id}" for case in unstable]
    else:
        lines.append("Every case passed every repetition.")
    lines += ["", *(f"  {line}" for line in operations_lines(summary.operations))]
    return "\n".join(lines)


def write_reports(
    directory: Path, name: str, summary: Summary | RepeatSummary
) -> tuple[Path, Path]:
    """Write `<name>.md` and `<name>.json` in `directory`; return the two paths."""
    directory.mkdir(parents=True, exist_ok=True)
    markdown, data = directory / f"{name}.md", directory / f"{name}.json"
    text = (
        render_markdown(summary) if isinstance(summary, Summary) else render_repeat_markdown(summary)
    )
    markdown.write_text(text, encoding="utf-8")
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
            "output mode, prompt version, schema context, system text, row cap, query timeout "
            "or result-size limit), or does not state them, so the record was started again: "
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
        _emit_unstated_limits(directory, settings, emit)
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
    return _replay_finish([replay])


def _rescore(
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
        f"eval --rescore: every recorded case of {spec.model} in {display_path(directory)} is "
        f"scored again from its recorded replies, and {RUN_FILE} is rewritten. Nothing is sent."
    )
    by_id = {case.id: case for case in suite.cases}
    try:
        rescore = rescore_run(
            suite, settings, directory, available=available,
            on_case=lambda run: emit(_progress(run, by_id)),
            cases_directory=cases_directory, pins_file=pins_file,
        )  # fmt: skip
    except StaleRecording as error:
        emit(f"Cannot rescore: {error}")
        _emit_unstated_limits(directory, settings, emit)
        emit("Nothing was written. Recordings are made by a paid run: eval --yes-spend.")
        return "cannot_start"
    record = rescore.record
    summary = summarise(recorded_results(record, suite), run_info(record, "rescored"))
    emit(render_summary(summary, record.totals))
    emit("Tokens and seconds are the recorded ones.")
    emit("")
    if rescore.differences:
        emit(f"{len(rescore.differences)} case(s) score differently from the earlier {RUN_FILE}:")
        for item in rescore.differences:
            emit(f"  {item.case_id}: {item.detail}")
    else:
        emit(f"Every case scored as in the earlier {RUN_FILE}.")
    if rescore.kept_as_recorded:
        emit(
            "Kept as recorded, because no reply was recorded to score again: "
            + ", ".join(rescore.kept_as_recorded) + "."
        )  # fmt: skip
    if rescore.dropped:
        emit("No longer in the suite, so left out: " + ", ".join(rescore.dropped) + ".")
    markdown, data = write_reports(RESULTS_DIR / record.model, "report", summary)
    emit(
        f"Rewrote {display_path(directory / RUN_FILE)} ({len(record.cases)} case(s)) and the "
        f"reports {display_path(markdown)} and {display_path(data)}."
    )
    return "passed"


def _live_repeats(
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
    repeat: int,
) -> Finish:
    by_id = {case.id: case for case in cases}
    lives: list[LiveRun] = []
    for number in range(1, repeat + 1):
        emit(f"Repetition {number} of {repeat}:")
        live = record_run(
            cases, suite, settings, client, repeat_directory(directory, number),
            available=available, on_case=lambda run: emit(_progress(run, by_id)),
            cases_directory=cases_directory, pins_file=pins_file, tracer=tracer,
        )  # fmt: skip
        lives.append(live)
        if live.stopped_by is not None:
            emit(
                f"Stopped in repetition {number} after {len(live.runs)} case(s): "
                f"{live.stopped_by}. Incomplete run; what ran before it is recorded."
            )
            return "cannot_start"
    info = run_info(lives[0].record, "live")
    summary = summarise_repeats(
        [[to_result(by_id[run.case_id], run) for run in live.runs] for live in lives], info
    )
    emit(render_repeat_summary(summary))
    emit("")
    emit(
        f"Recorded in {display_path(directory / 'repeats')}, one folder per repetition, each "
        f"with its own {RUN_FILE}. The record of the model's own run was not touched."
    )
    whole = summarise_repeats([recorded_results(live.record, suite) for live in lives], info)
    markdown, data = write_reports(RESULTS_DIR / info.model_id, "repeat", whole)
    emit(
        f"Reports of all {whole.overall.cases} case(s) recorded in every repetition: "
        f"{display_path(markdown)} and {display_path(data)}."
    )
    missing = sorted({case_id for live in lives for case_id in live.not_replayable})
    if missing:
        emit(
            "A request got no reply in: " + ", ".join(missing) + ". Those repetitions are "
            "scored as failed and cannot be replayed."
        )
    return "passed" if summary.overall.passed_all == summary.overall.cases else "failed"


def _replay_repeats(
    cases: Sequence[Case],
    suite: Suite,
    settings: Settings,
    spec: ModelSpec,
    directory: Path,
    available: frozenset[Feature],
    emit: Emit,
    cases_directory: Path,
    pins_file: Path,
    repeat: int,
) -> Finish:
    emit(
        f"eval --replay --repeat {repeat}: {len(cases)} case(s), {repeat} repetitions, from the "
        f"recorded replies of {spec.model} in {display_path(directory / 'repeats')}. "
        "Nothing is sent."
    )
    by_id = {case.id: case for case in cases}
    replays: list[Replay] = []
    try:
        for number in range(1, repeat + 1):
            emit(f"Repetition {number} of {repeat}:")
            replays.append(
                replay_run(
                    cases, suite, settings, repeat_directory(directory, number),
                    available=available, on_case=lambda run: emit(_progress(run, by_id)),
                    cases_directory=cases_directory, pins_file=pins_file,
                )  # fmt: skip
            )
    except StaleRecording as error:
        emit(f"Cannot replay: {error}")
        emit(f"A repeated run is recorded by: eval --repeat {repeat} --yes-spend.")
        return "cannot_start"
    results: list[list[CaseResult]] = [
        [to_result(by_id[run.case_id], run) for run in replay.runs] for replay in replays
    ]
    summary = summarise_repeats(results, run_info(replays[0].record, "replay"))
    emit(render_repeat_summary(summary))
    emit("Tokens and seconds are the recorded ones.")
    if summary.cases:
        markdown, data = write_reports(RESULTS_DIR / spec.model, "replay-repeat", summary)
        emit(f"Reports of this replay: {display_path(markdown)} and {display_path(data)}.")
    for number, replay in enumerate(replays, start=1):
        emit("")
        emit(f"Repetition {number}: " + render_comparison(replay))
    return _replay_finish(replays)


def render_comparison(replay: Replay) -> str:
    """How a replay stands next to `run.json`: what changed, what is missing, what differs."""
    lines: list[str] = []
    if replay.unchecked_settings:
        lines.append(
            f"{RUN_FILE} was made before {', '.join(replay.unchecked_settings)} were kept, so "
            "this replay could not check them against the present settings. eval --rescore "
            "rebuilds the record with them."
        )
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


def _replay_finish(replays: Sequence[Replay]) -> Finish:
    """A difference, or nothing replayed, comes first; then a selected case with no recording."""
    if not all(replay.reproduced for replay in replays):
        return "not_reproduced"
    return "reproduced" if all(replay.complete for replay in replays) else "partly_replayed"


def _progress(run: CaseRun, by_id: dict[str, Case]) -> str:
    return progress_line(run, known_limitation=by_id[run.case_id].known_limitation is not None)


def _emit_unstated_limits(directory: Path, settings: Settings, emit: Emit) -> None:
    """Name the limits in use when the record cannot say which ones it was made under."""
    if not _unknown_settings(directory):
        return
    emit(
        f"{RUN_FILE} does not state the limits it was recorded under. This attempt used "
        f"row_cap {settings.row_cap}, query_timeout_seconds {settings.query_timeout_seconds:g} "
        f"and max_result_bytes {settings.max_result_bytes}: if one of them differs from the "
        "recorded run's, a turn ends differently and its recorded replies no longer fit. "
        "Use the limits of the recorded run (the defaults, unless it was run otherwise)."
    )


def _unknown_settings(directory: Path) -> tuple[str, ...]:
    """The settings an earlier record in `directory` does not state; none when there is no record."""
    try:
        return read_record(directory).unknown_settings
    except StaleRecording:
        return ()


def _tally_table(heading: str, tallies: Sequence[Tally]) -> list[str]:
    width = max([len(heading), *(len(tally.name) for tally in tallies)])
    return [
        f"{heading:<{width}}  {'cases':>5}  {'passed':>6}  {'extra cols':>10}  {'guard stop':>10}  "
        f"{'failed':>6}  {'known lim.':>10}  {'skipped':>7}",
        *(
            f"{tally.name:<{width}}  {tally.cases:>5}  {tally.passed:>6}  "
            f"{tally.with_extra_columns:>10}  {tally.stopped_by_guard:>10}  {tally.failed:>6}  "
            f"{tally.known_limitation:>10}  {tally.skipped:>7}"
            for tally in tallies
        ),
    ]


def _repeat_table(heading: str, tallies: Sequence[RepeatTally], times: int) -> list[str]:
    width = max([len(heading), *(len(tally.name) for tally in tallies)])
    return [
        f"{heading:<{width}}  {'cases':>5}  {f'all {times}':>6}  {'some':>5}  {'none':>5}",
        *(
            f"{tally.name:<{width}}  {tally.cases:>5}  {tally.passed_all:>6}  "
            f"{tally.passed_some:>5}  {tally.passed_none:>5}"
            for tally in tallies
        ),
    ]
