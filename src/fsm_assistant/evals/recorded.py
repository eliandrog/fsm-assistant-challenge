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
file is left as it was. Only a run under other settings (model, effort,
token limit, output mode, prompt version or schema context) starts the
record again, because the earlier replies can no longer be replayed: when
its first case is recorded, the recorded replies of every other case are
removed, so the folder never holds replies made under two sets of settings.

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
from fsm_assistant.schema_context import build_schema_context

RUN_RECORDINGS_DIR = PROJECT_ROOT / "evals" / "recordings" / "runs"
RUN_FILE = "run.json"
RUN_FORMAT_VERSION: Literal[1] = 1

# No later request of the run could succeed after one of these.
_STOP_AFTER: frozenset[str] = frozenset(
    {"missing_key", "authentication", "permission", "model_not_found"}
)
_MODEL_DIRECTORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# The settings a recorded reply depends on; a record made under others cannot be replayed.
_IDENTITY = ("model", "effort", "max_tokens", "tool_mode", "prompt_version", "schema_context_sha256")


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
    suite_sha256: str  # the case files and pins as they were at the latest live run
    cases: tuple[CaseRecord, ...]
    totals: RunTotals

    def find(self, case_id: str) -> CaseRecord | None:
        for record in self.cases:
            if record.run.case_id == case_id:
                return record
        return None


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
    usable recording; they were not run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: tuple[CaseRun, ...]
    record: RunRecord
    suite_changed: bool
    changed_cases: tuple[str, ...]  # recorded cases whose definition changed since
    not_recorded: tuple[str, ...]
    differences: tuple[Difference, ...]

    @property
    def reproduced(self) -> bool:
        return not self.differences and not self.not_recorded


def run_directory(model: str) -> Path | None:
    """The folder of one model's recorded run, or None when the id cannot be a folder name."""
    if not _MODEL_DIRECTORY.fullmatch(model):
        return None
    return RUN_RECORDINGS_DIR / model


def schema_digest(settings: Settings) -> str:
    return hashlib.sha256(build_schema_context(settings.db_path).encode("utf-8")).hexdigest()


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
            run = run_case(case, suite, settings, watched)
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
    (the prompt, a tool, a model setting or the question changed).
    """
    spec = model_spec(settings)
    record = read_record(directory)
    current = _record(spec, settings, (), cases_directory, pins_file)
    for name in _IDENTITY:
        was, now = getattr(record, name), getattr(current, name)
        if was != now:
            raise StaleRecording(
                "stale",
                directory / RUN_FILE,
                f"the run was recorded with {name} {_shown(was)} and the code now uses {_shown(now)}",
            )
    runs: list[CaseRun] = []
    changed: list[str] = []
    not_recorded: list[str] = []
    differences: list[Difference] = []
    for case in cases:
        found = record.find(case.id)
        if found is None or not found.replayable:
            not_recorded.append(case.id)
            continue
        if found.case_sha256 != case_digest(case, suite):
            changed.append(case.id)
        reason = skip_reason(case, available)
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
        suite_sha256=suite_digest(cases_directory, pins_file),
        cases=tuple(cases),
        totals=totals([item.run for item in cases]),
    )


def _same_settings(one: RunRecord, other: RunRecord) -> bool:
    return all(getattr(one, name) == getattr(other, name) for name in _IDENTITY)


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
                    + (" (the detail differs)" if same_verdict else "")
                )
            return f"turn {was.turn}: the recorded scores are not the replayed ones"
    if len(recorded.turns) != len(replayed.turns):
        return f"recorded {len(recorded.turns)} turn(s), replayed {len(replayed.turns)}"
    return None


def _state(run: CaseRun) -> str:
    return "skipped" if run.skipped is not None else "run"


def _verdict(passed: bool, reason: object) -> str:
    return "pass" if passed else f"fail ({reason})"
