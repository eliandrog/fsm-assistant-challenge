"""A recorded evaluation run: its files, a live run that writes them, and its replay.

Responsibility: everything kept on disk about a run on one model.

    evals/recordings/runs/<model id>/<case id>.turn<N>.call<M>.json   one model reply
    evals/recordings/runs/<model id>/run.json                         what was run and how it scored

`record_run` wraps the model it is given in `llm.RecordingModel`, one per
case, and rewrites `run.json` after every case, so an interrupted run leaves
a record that matches the recordings. `replay_run` sends the same cases
through the real graph, guard, database and scorers with `llm.ReplayModel`,
which builds no client, and compares what comes out with `run.json`.

A run of some of the cases is merged into the record: the cases it ran
replace their own recordings and their own entry, and every other entry and
file is left as it was. Only a run under other settings starts the record
again, because the earlier replies can no longer be replayed: when its
first case is recorded, the recorded replies of every other case are
removed, so the folder never holds replies made under two sets of settings.

What counts as the settings of a run (`IDENTITY`): the model, effort, token
limit, output mode, prompt version and schema context, and, kept since
later, the row cap, the query timeout, the result-size limit and a digest
of the system text. The last four are None in a record made before they
were kept: what that run ran under is then unknown. Such a record is still
replayed, with a line saying those settings could not be checked;
`rescore_run` rebuilds it with them, and a live run does not merge into it.

`rescore_run` rebuilds `run.json` from the recorded replies through the real
graph, guard, database and scorers, with no model. It is how a record is
brought up to date after a scorer changed: the replies are the recorded
ones and the scores are the ones the code gives now.

A repeated run keeps each repetition as a whole run of its own, with its
recordings and its `run.json`, in `repeats/<k>/` under the model's folder
(`repeat_directory`). It never touches the model's own record.

`run.json` holds no timestamp, no date, no absolute path and no key.
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from fsm_assistant.config import PROJECT_ROOT, Effort, Settings, ToolMode
from fsm_assistant.evals.cases import (
    DEFAULT_CASES_DIRECTORY,
    DEFAULT_PINS_FILE,
    Case,
    Feature,
    Suite,
    suite_digest,
)
from fsm_assistant.evals.report import CaseResult, RunInfo, Source, skip_reason
from fsm_assistant.evals.runner import CaseRun, run_case, to_result
from fsm_assistant.llm import (
    IntentModel,
    ModelOutputError,
    ModelReply,
    ModelRequest,
    ModelSpec,
    ModelUnavailable,
    RecordingModel,
    ReplayModel,
    StaleRecording,
    UnavailableKind,
    recording_path,
)
from fsm_assistant.model_check import LibraryVersions
from fsm_assistant.nl2sql import model_spec
from fsm_assistant.prompt import system_text
from fsm_assistant.schema_context import build_schema_context
from fsm_assistant.tracing import Tracer

RUN_RECORDINGS_DIR = PROJECT_ROOT / "evals" / "recordings" / "runs"
RUN_FILE = "run.json"
RUN_FORMAT_VERSION: Literal[1] = 1

# No later request of the run could succeed after one of these.
_STOP_AFTER: frozenset[str] = frozenset(
    {"missing_key", "authentication", "permission", "model_not_found"}
)
_MODEL_DIRECTORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
REPEATS_FOLDER = "repeats"
# The settings a recorded reply depends on; a record made under others cannot be replayed.
_IDENTITY = ("model", "effort", "max_tokens", "tool_mode", "prompt_version", "schema_context_sha256")
# Kept since later, so None in an older record. The limits decide what a turn
# returns (a cut result, a timeout and its repair); the system text is the
# prompt as sent, which catches an edit made without a new prompt version.
_LATER_IDENTITY = ("row_cap", "query_timeout_seconds", "max_result_bytes", "system_text_sha256")
IDENTITY = (*_IDENTITY, *_LATER_IDENTITY)


class CaseRecord(BaseModel):
    """One case in `run.json`.

    `case_sha256` is the digest of the case as it was when it ran, with its
    pinned results. `replayable` is false when a request of the case got no
    reply, since nothing was recorded for it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_sha256: str
    replayable: bool
    run: CaseRun


class RunTotals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cases: int
    passed: int
    failed: int
    skipped: int
    turns: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    seconds: float


class RunRecord(BaseModel):
    """The `run.json` of one model: the settings, every recorded case and the totals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: Literal[1]
    model: str
    effort: Effort
    max_tokens: int
    tool_mode: ToolMode
    prompt_version: str
    library_versions: LibraryVersions
    schema_context_sha256: str
    # None in a record made before these four were kept.
    row_cap: int | None = None
    query_timeout_seconds: float | None = None
    max_result_bytes: int | None = None
    system_text_sha256: str | None = None
    suite_sha256: str  # the case files and pins at the latest live run or rescore
    cases: tuple[CaseRecord, ...]
    totals: RunTotals

    def find(self, case_id: str) -> CaseRecord | None:
        for record in self.cases:
            if record.run.case_id == case_id:
                return record
        return None

    @property
    def unknown_settings(self) -> tuple[str, ...]:
        """The settings this record does not state, because it was made before they were kept."""
        return tuple(name for name in _LATER_IDENTITY if getattr(self, name) is None)


class LiveRun(BaseModel):
    """What a live run did. `stopped_by` is set when it ended early."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: tuple[CaseRun, ...]
    record: RunRecord
    restarted: bool  # an earlier record made under other settings was replaced
    not_replayable: tuple[str, ...]
    stopped_by: UnavailableKind | None = None


class Difference(BaseModel):
    """A case whose replay does not score as `run.json` says it did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    detail: str


class Replay(BaseModel):
    """A replay next to its record.

    `runs` carry the recorded seconds, since a replay's own timing says
    nothing about the model. `not_recorded` are the selected cases with no
    usable recording; they were not run. `unchecked_settings` are the
    settings the record does not state, which the replay could not compare.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: tuple[CaseRun, ...]
    record: RunRecord
    suite_changed: bool
    changed_cases: tuple[str, ...]  # recorded cases whose definition changed since
    not_recorded: tuple[str, ...]
    differences: tuple[Difference, ...]
    unchecked_settings: tuple[str, ...] = ()

    @property
    def reproduced(self) -> bool:
        """Every case that was replayed scored as recorded, and at least one was."""
        return bool(self.runs) and not self.differences

    @property
    def complete(self) -> bool:
        """Every selected case had a recording to replay."""
        return not self.not_recorded


class Rescore(BaseModel):
    """A record rebuilt from its recorded replies, next to the record it replaced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: RunRecord  # as written
    differences: tuple[Difference, ...]  # cases that score differently now
    kept_as_recorded: tuple[str, ...]  # not replayable: no reply to score again
    dropped: tuple[str, ...]  # in the earlier record, no longer in the suite


def run_directory(model: str) -> Path | None:
    """The folder of one model's recorded run, or None when the id cannot be a folder name."""
    if not _MODEL_DIRECTORY.fullmatch(model):
        return None
    return RUN_RECORDINGS_DIR / model


def repeat_directory(directory: Path, repetition: int) -> Path:
    """The folder of one repetition (counted from 1) of a repeated run."""
    return directory / REPEATS_FOLDER / str(repetition)


def schema_digest(settings: Settings) -> str:
    return hashlib.sha256(build_schema_context(settings.db_path).encode("utf-8")).hexdigest()


def system_text_digest(settings: Settings) -> str:
    """A digest of the system message as it is sent for this database."""
    text = system_text(build_schema_context(settings.db_path))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def case_digest(case: Case, suite: Suite) -> str:
    """A digest of a case and its pinned results: it changes when either does."""
    digest = hashlib.sha256(case.model_dump_json().encode("utf-8"))
    for number in range(1, len(case.turns) + 1):
        pin = suite.pin(case, number)
        if pin is not None:
            digest.update(pin.model_dump_json().encode("utf-8"))
    return digest.hexdigest()


def read_record(directory: Path) -> RunRecord:
    """The `run.json` in `directory`. Raises `StaleRecording` when there is none to use."""
    path = directory / RUN_FILE
    if not path.is_file():
        raise StaleRecording("missing", path, "no evaluation run was recorded for this model")
    try:
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise StaleRecording(
            "unreadable", path, f"the file is not a run record: {error.errors()[0]['msg']}"
        ) from None


def write_record(directory: Path, record: RunRecord) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RUN_FILE).write_text(record.model_dump_json(indent=1) + "\n", encoding="utf-8")


def run_info(record: RunRecord, source: Source) -> RunInfo:
    return RunInfo(
        model_id=record.model,
        effort=record.effort,
        tool_mode=record.tool_mode,
        prompt_version=record.prompt_version,
        source=source,
        schema_context_sha256=record.schema_context_sha256,
        suite_sha256=record.suite_sha256,
    )


def recorded_results(record: RunRecord, suite: Suite) -> tuple[CaseResult, ...]:
    """Every recorded case that is still in the suite, in suite order, for a report."""
    return tuple(
        to_result(case, found.run)
        for case in suite.cases
        if (found := record.find(case.id)) is not None
    )


def record_run(
    cases: Sequence[Case],
    suite: Suite,
    settings: Settings,
    client: IntentModel,
    directory: Path,
    *,
    available: frozenset[Feature],
    on_case: Callable[[CaseRun], None] | None = None,
    cases_directory: Path = DEFAULT_CASES_DIRECTORY,
    pins_file: Path = DEFAULT_PINS_FILE,
    tracer: Tracer | None = None,
) -> LiveRun:
    """Run `cases` on `client`, recording every reply and merging the results into `run.json`.

    A case whose request got no reply is scored as the failure it was and
    marked not replayable. The run stops, keeping what it has, at a failure
    every later request would repeat (no key, a refused key, an unknown
    model); the case it stopped at is not recorded.
    """
    spec = model_spec(settings)
    earlier = _earlier(directory)
    fresh = _record(spec, settings, (), cases_directory, pins_file)
    restarted = earlier is not None and not _same_settings(earlier, fresh)
    kept: dict[str, CaseRecord] = (
        {} if earlier is None or restarted else {item.run.case_id: item for item in earlier.cases}
    )
    in_suite = {case.id for case in suite.cases}
    runs: list[CaseRun] = []
    not_replayable: list[str] = []
    stopped_by: UnavailableKind | None = None

    def merged() -> RunRecord:
        ordered = [kept[case.id] for case in suite.cases if case.id in kept]
        return _record(spec, settings, ordered, cases_directory, pins_file)

    for case in cases:
        reason = skip_reason(case, available)
        if reason is not None:
            run = CaseRun(case_id=case.id, skipped=reason)
            watched = None
        else:
            watched = _Watched(RecordingModel(client, directory, case.id))
            run = run_case(case, suite, settings, watched, tracer=tracer)
            if watched.unavailable in _STOP_AFTER:
                stopped_by = watched.unavailable
                break
            _drop_other_recordings(directory, case.id, watched.requested)
        if (earlier is None or restarted) and not kept:
            _drop_earlier_run(directory, case.id)
        replayable = watched is None or not watched.no_reply
        if not replayable:
            not_replayable.append(case.id)
        kept[case.id] = CaseRecord(
            case_sha256=case_digest(case, suite), replayable=replayable, run=run
        )
        kept = {case_id: item for case_id, item in kept.items() if case_id in in_suite}
        write_record(directory, merged())
        runs.append(run)
        if on_case is not None:
            on_case(run)
    return LiveRun(
        runs=tuple(runs),
        record=merged(),
        restarted=restarted,
        not_replayable=tuple(not_replayable),
        stopped_by=stopped_by,
    )


def replay_run(
    cases: Sequence[Case],
    suite: Suite,
    settings: Settings,
    directory: Path,
    *,
    available: frozenset[Feature],
    on_case: Callable[[CaseRun], None] | None = None,
    cases_directory: Path = DEFAULT_CASES_DIRECTORY,
    pins_file: Path = DEFAULT_PINS_FILE,
) -> Replay:
    """Re-run `cases` from the recordings in `directory` and compare with `run.json`.

    Sends nothing and builds no client. Raises `StaleRecording` when there
    is no record, when it was made under other settings, or when a recorded
    reply is missing or no longer answers the request the code now makes
    (the prompt, a tool, a model setting or the question changed). A setting
    the record does not state is not compared.
    """
    spec = model_spec(settings)
    record = read_record(directory)
    current = _record(spec, settings, (), cases_directory, pins_file)
    _check_settings(record, current, directory)
    runs: list[CaseRun] = []
    changed: list[str] = []
    not_recorded: list[str] = []
    differences: list[Difference] = []
    for case in cases:
        found = record.find(case.id)
        reason = skip_reason(case, available)
        # A case the run skipped has no reply on file: once it can run, it is not recorded.
        skipped_then = found is not None and found.run.skipped is not None and reason is None
        if found is None or not found.replayable or skipped_then:
            not_recorded.append(case.id)
            continue
        if found.case_sha256 != case_digest(case, suite):
            changed.append(case.id)
        if reason is not None:
            run = CaseRun(case_id=case.id, skipped=reason)
        else:
            watched = _Watched(ReplayModel(spec, directory, case.id))
            run = _with_recorded_seconds(run_case(case, suite, settings, watched), found.run)
            if watched.stale is not None:
                if case.id in changed and watched.stale.kind == "stale":
                    raise StaleRecording(
                        "stale",
                        watched.stale.path,
                        f"the case {case.id} changed since it was recorded, and its recorded "
                        "reply no longer answers the request",
                    )
                raise watched.stale
        difference = _difference(found.run, run)
        if difference is not None:
            differences.append(Difference(case_id=case.id, detail=difference))
        runs.append(run)
        if on_case is not None:
            on_case(run)
    return Replay(
        runs=tuple(runs),
        record=record,
        suite_changed=record.suite_sha256 != current.suite_sha256,
        changed_cases=tuple(changed),
        not_recorded=tuple(not_recorded),
        differences=tuple(differences),
        unchecked_settings=record.unknown_settings,
    )


def rescore_run(
    suite: Suite,
    settings: Settings,
    directory: Path,
    *,
    available: frozenset[Feature],
    on_case: Callable[[CaseRun], None] | None = None,
    cases_directory: Path = DEFAULT_CASES_DIRECTORY,
    pins_file: Path = DEFAULT_PINS_FILE,
) -> Rescore:
    """Rebuild `run.json` in `directory` from its recorded replies, with no model.

    Every recorded case still in the suite goes through the real graph and
    scorers again with its recorded replies, and the record is written with
    the scores that gives, the settings it ran under and each case's present
    digest. Seconds stay the recorded ones. A case that could not be
    replayed when it was recorded keeps its entry, and so does a case the
    run skipped for a feature that has been built since: neither has a
    reply on file, and a replay names both as not recorded. Raises `StaleRecording`,
    and writes nothing, when the record was made under other settings or a
    recorded reply no longer answers its request.
    """
    spec = model_spec(settings)
    record = read_record(directory)
    current = _record(spec, settings, (), cases_directory, pins_file)
    _check_settings(record, current, directory)
    rebuilt: list[CaseRecord] = []
    differences: list[Difference] = []
    kept: list[str] = []
    for case in suite.cases:
        found = record.find(case.id)
        if found is None:
            continue
        reason = skip_reason(case, available)
        skipped_then = found.run.skipped is not None and reason is None
        if not found.replayable or skipped_then:
            kept.append(case.id)
            rebuilt.append(found)
            continue
        if reason is not None:
            run = CaseRun(case_id=case.id, skipped=reason)
        else:
            watched = _Watched(ReplayModel(spec, directory, case.id))
            run = _with_recorded_seconds(run_case(case, suite, settings, watched), found.run)
            if watched.stale is not None:
                raise watched.stale
        difference = _difference(found.run, run)
        if difference is not None:
            differences.append(Difference(case_id=case.id, detail=difference))
        rebuilt.append(
            CaseRecord(case_sha256=case_digest(case, suite), replayable=True, run=run)
        )
        if on_case is not None:
            on_case(run)
    # The replies were made under the libraries the earlier record names.
    written = _record(spec, settings, rebuilt, cases_directory, pins_file).model_copy(
        update={"library_versions": record.library_versions}
    )
    write_record(directory, written)
    in_suite = {case.id for case in suite.cases}
    return Rescore(
        record=written,
        differences=tuple(differences),
        kept_as_recorded=tuple(kept),
        dropped=tuple(
            item.run.case_id for item in record.cases if item.run.case_id not in in_suite
        ),
    )


def totals(runs: Sequence[CaseRun]) -> RunTotals:
    turns = [turn for run in runs for turn in run.turns]
    skipped = sum(run.skipped is not None for run in runs)
    passed = sum(run.passed for run in runs)
    return RunTotals(
        cases=len(runs),
        passed=passed,
        failed=len(runs) - passed - skipped,
        skipped=skipped,
        turns=len(turns),
        model_calls=sum(turn.model_calls for turn in turns),
        input_tokens=sum(turn.input_tokens for turn in turns),
        output_tokens=sum(turn.output_tokens for turn in turns),
        seconds=round(sum(turn.seconds for turn in turns), 3),
    )


class _Watched:
    """Passes requests on, and notes what a turn's result does not say.

    The graph turns a request that got no reply into a failed turn; a run
    needs to know which kind it was, and a replay must not mistake a stale
    recording for a result.
    """

    def __init__(self, inner: IntentModel) -> None:
        self._inner = inner
        self.requested: set[tuple[int, int]] = set()  # the (turn, call) of each request made
        self.no_reply = False  # a request ended with nothing to record
        self.unavailable: UnavailableKind | None = None
        self.stale: StaleRecording | None = None

    def complete(self, request: ModelRequest) -> ModelReply:
        self.requested.add((request.turn, request.call))
        try:
            return self._inner.complete(request)
        except ModelOutputError:
            # A refused or cut-off reply is a reply: it is recorded and can be replayed.
            raise
        except StaleRecording as error:
            self.stale = self.stale or error
            raise
        except ModelUnavailable as error:
            self.unavailable = self.unavailable or error.kind
            self.no_reply = True
            raise
        except Exception:  # whatever it was, no reply was written for this request
            self.no_reply = True
            raise


def _earlier(directory: Path) -> RunRecord | None:
    try:
        return read_record(directory)
    except StaleRecording:
        return None


def _record(
    spec: ModelSpec,
    settings: Settings,
    cases: Sequence[CaseRecord],
    cases_directory: Path,
    pins_file: Path,
) -> RunRecord:
    return RunRecord(
        format_version=RUN_FORMAT_VERSION,
        model=spec.model,
        effort=spec.effort,
        max_tokens=spec.max_tokens,
        tool_mode=spec.tool_mode,
        prompt_version=spec.prompt_version,
        library_versions=LibraryVersions(
            langchain_anthropic=version("langchain-anthropic"),
            langchain_core=version("langchain-core"),
            langgraph=version("langgraph"),
        ),
        schema_context_sha256=schema_digest(settings),
        row_cap=settings.row_cap,
        query_timeout_seconds=settings.query_timeout_seconds,
        max_result_bytes=settings.max_result_bytes,
        system_text_sha256=system_text_digest(settings),
        suite_sha256=suite_digest(cases_directory, pins_file),
        cases=tuple(cases),
        totals=totals([item.run for item in cases]),
    )


def _same_settings(one: RunRecord, other: RunRecord) -> bool:
    """A setting one of them does not state is not known to be the same."""
    return all(getattr(one, name) == getattr(other, name) for name in IDENTITY)


def _check_settings(record: RunRecord, current: RunRecord, directory: Path) -> None:
    """Raise `StaleRecording`, naming the setting, when the record states another value."""
    for name in IDENTITY:
        was, now = getattr(record, name), getattr(current, name)
        if was == now or (was is None and name in _LATER_IDENTITY):
            continue
        uses = "the settings now give" if name in _LATER_IDENTITY[:3] else "the code now uses"
        raise StaleRecording(
            "stale",
            directory / RUN_FILE,
            f"the run was recorded with {name} {_shown(was)} and {uses} {_shown(now)}",
        )


def _shown(value: object) -> str:
    text = str(value)
    return f"{text[:12]}…" if len(text) == 64 else text


def _drop_other_recordings(directory: Path, case_id: str, written: set[tuple[int, int]]) -> None:
    """Remove the case's recordings from an earlier run that this run did not rewrite."""
    keep = {recording_path(directory, case_id, turn, call) for turn, call in written}
    for path in directory.glob(f"{case_id}.turn*.call*.json"):
        if path not in keep:
            path.unlink()


def _drop_earlier_run(directory: Path, case_id: str) -> None:
    """Remove every recorded reply except those of `case_id`, the first case of a new record."""
    for path in directory.glob("*.turn*.call*.json"):
        if path.name.split(".turn", 1)[0] != case_id:
            path.unlink()


def _with_recorded_seconds(run: CaseRun, recorded: CaseRun) -> CaseRun:
    if len(run.turns) != len(recorded.turns):
        return run
    return run.model_copy(
        update={
            "turns": tuple(
                turn.model_copy(update={"seconds": was.seconds})
                for turn, was in zip(run.turns, recorded.turns, strict=True)
            )
        }
    )


def _difference(recorded: CaseRun, replayed: CaseRun) -> str | None:
    """How a replayed case differs from its record, in one line, or None."""
    if recorded.skipped != replayed.skipped:
        return f"recorded as {_state(recorded)}, replayed as {_state(replayed)}"
    for was, now in zip(recorded.turns, replayed.turns, strict=False):
        if was.outcome != now.outcome:
            return f"turn {was.turn}: recorded outcome {was.outcome}, replayed {now.outcome}"
        if was.scores != now.scores:
            before = {score.scorer: score for score in was.scores}
            for score in now.scores:
                old = before.get(score.scorer)
                if old == score:
                    continue
                recorded_as = "no score" if old is None else _verdict(old.passed, old.reason)
                same_verdict = old is not None and (old.passed, old.reason) == (
                    score.passed,
                    score.reason,
                )
                return (
                    f"turn {was.turn}, {score.scorer}: recorded {recorded_as}, "
                    f"replayed {_verdict(score.passed, score.reason)}"
                    + (
                        ""
                        if not same_verdict or old is None
                        else " (the note differs)"
                        if old.note != score.note
                        else " (the detail differs)"
                    )
                )
            return f"turn {was.turn}: the recorded scores are not the replayed ones"
    if len(recorded.turns) != len(replayed.turns):
        return f"recorded {len(recorded.turns)} turn(s), replayed {len(replayed.turns)}"
    return None


def _state(run: CaseRun) -> str:
    return "skipped" if run.skipped is not None else "run"


def _verdict(passed: bool, reason: object) -> str:
    return "pass" if passed else f"fail ({reason})"
