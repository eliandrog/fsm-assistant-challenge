"""The rule finder.

Responsibility: the automatic search for fraud rules, in two steps that a
reviewer starts one at a time (docs/DECISIONS.md, D-35, D-36).

Propose. One model request asks for fraud scenarios written from the schema
alone (`scenarios.py`). Each is saved in the rule bank as `proposed` and
nothing else happens: no clause is written and no query runs until a
reviewer has read the hypothesis and asked for it.

Process. One proposed scenario is put to the ordinary turn as a rule request
(`Assistant.run_turn`: the model writes a clause with `propose_rule`, the
guard judges it, the two backtest statements measure it), its evidence is
judged against the criteria below, and the entry is completed in the bank,
unless its rule is already there. Processing runs on a worker thread and
describes itself step by step as a `RunProgress`, which the page polls. A
person then approves or rejects the rule; nothing here does.

There is no second path to a rule: a scenario's clause reaches the database
only through the turn's `guard_rule` and `backtest_rule`. The one statement
this module runs itself compares two clauses that the guard accepted, and
accepts again here, through `db.run_query`, which is read-only.

Cost. Propose is one request, whatever the number of scenarios asked for.
Process is one turn, at most `MAX_MODEL_CALLS` requests. A batch of n from
the terminal is therefore at most `request_bound(n)` = 1 + 2n. Nothing is
retried and there is no second round.

What the model sees. The scenario request carries the schema context, a
count and the titles and hypotheses of scenarios already in the bank, which
are the model's own earlier words, so that it proposes something else. A
scenario's turn carries that scenario's text. Nothing measured is ever put
in a request: no backtest figure, no status, no verdict. Figures go to the
bank and to the person reading it.

Find on one period, confirm on the next. A rule is judged on the months
before `LATER_PERIOD_START` (the discovery period: 2019-01 to 2019-07 in the
shipped data) and must then hold on the months from it (the confirmation
period: 2019-08 to 2019-10), which played no part in judging it. The rule's
text was written with no data at all, so neither period shaped it; what the
discovery period does is select. The confirmation period is a later part of
the same history, measured for every candidate: a check that a selected rule
did not pass by chance in one period, and not a test on new data. Several
candidates are tried, so one can pass both periods by chance; the criteria
ask for support in both periods to make that less likely, and do not rule
it out.

One action at a time. A propose or a process in progress holds a lock; any
other asked for meanwhile is refused (`FinderBusy`), not queued. Every other
failure ends as a `FinderRun` whose status says so. A run that a dead
process left as running is marked interrupted when the next one starts.
"""

import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict

from fsm_assistant import db
from fsm_assistant.config import Settings
from fsm_assistant.events import Label
from fsm_assistant.graph import Assistant, TraceStep
from fsm_assistant.llm import IntentModel, ModelError, ModelOutputError, ModelUnavailable
from fsm_assistant.nl2sql import PROMPT_VERSION
from fsm_assistant.presentation import (
    MIN_EXPECTED_FRAUD,
    SMALL_SAMPLE_LABELLED,
    WEAK_PRECISION_RATIO,
    safe_text,
)
from fsm_assistant.result import RuleCard, RulePeriod, TurnResult
from fsm_assistant.rule_bank import (
    BankEntry,
    BankError,
    Criterion,
    DuplicateCheck,
    DuplicateClause,
    EntrySummary,
    EvidenceStatus,
    FinderRun,
    MatchKind,
    NewEntry,
    Period,
    RuleBank,
    RunError,
    RunKind,
    RunSource,
    RunStatus,
)
from fsm_assistant.rules.proposal import LATER_PERIOD_START
from fsm_assistant.scenarios import (
    SCENARIO_PROMPT_VERSION,
    Scenario,
    ScenarioFailure,
    build_request,
    read_reply,
)
from fsm_assistant.schema_context import build_schema_context
from fsm_assistant.sql_guard import check_rule_clause
from fsm_assistant.sql_scan import Token, UnterminatedQuote, scan
from fsm_assistant.state import MAX_MODEL_CALLS
from fsm_assistant.tracing import Tracer

logger = logging.getLogger(__name__)

# --- The criteria (D-35). Judgements, stated before any run and not tuned on one. ---------
# Discovery: enough labelled flagged transactions for a precision to mean
# something (the floor of the small-sample caveat), ...
DISCOVERY_MIN_LABELLED: Final = SMALL_SAMPLE_LABELLED
# ... enough frauds caught that the figure is not one or two transactions, ...
DISCOVERY_MIN_FRAUD: Final = MIN_EXPECTED_FRAUD
# ... and precision clearly above the base rate. The base fraud rate of the
# shipped data itself moves between 0.67 and 1.29 times its average from month
# to month, so a lift under 1.5 can be a rule that only follows the calendar.
DISCOVERY_MIN_LIFT: Final = 1.5
# Confirmation: the same support floor, more than one fraud caught (a single
# one cannot be told from chance), precision still above the base rate, and a
# lift that kept at least this share of the discovery period's.
CONFIRMATION_MIN_LABELLED: Final = SMALL_SAMPLE_LABELLED
CONFIRMATION_MIN_FRAUD: Final = 2
CONFIRMATION_MIN_LIFT: Final = 1.0
CONFIRMATION_LIFT_RATIO: Final = WEAK_PRECISION_RATIO

# Shown wherever a finder rule's evidence is shown.
FINDER_NOTE: Final = (
    "The rule's text was written from the schema alone, with no data. It was selected on the "
    "discovery period and then measured on the confirmation period, which played no part in "
    "selecting it. That is a check on a later part of the same history, made for several "
    "candidates at once, so a rule can pass it by chance. It is not a test on new data: only "
    "new data or a shadow run can show that a rule holds."
)
QUESTION_PREFIX: Final = "Propose a rule that flags the transactions of this fraud scenario. "


class Criteria(BaseModel):
    """The thresholds a rule's evidence is judged against, for whoever reads the verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmation_starts: str  # the first month of the confirmation period, YYYY-MM
    discovery_min_labelled: int
    discovery_min_fraud: int
    discovery_min_lift: float
    confirmation_min_labelled: int
    confirmation_min_fraud: int
    confirmation_min_lift: float
    confirmation_lift_ratio: float


CRITERIA: Final = Criteria(
    confirmation_starts=LATER_PERIOD_START,
    discovery_min_labelled=DISCOVERY_MIN_LABELLED,
    discovery_min_fraud=DISCOVERY_MIN_FRAUD,
    discovery_min_lift=DISCOVERY_MIN_LIFT,
    confirmation_min_labelled=CONFIRMATION_MIN_LABELLED,
    confirmation_min_fraud=CONFIRMATION_MIN_FRAUD,
    confirmation_min_lift=CONFIRMATION_MIN_LIFT,
    confirmation_lift_ratio=CONFIRMATION_LIFT_RATIO,
)

# Each clause in its own parentheses and on its own lines, as the backtest
# template places it, so the containment the guard checked holds here too.
# A clause that is NULL for a row does not flag it.
_SAME_TRANSACTIONS: Final = """\
SELECT
  (SELECT count(*) FROM transactions WHERE (
{new}
  ) AND NOT coalesce((
{old}
  ), 0)) AS only_new,
  (SELECT count(*) FROM transactions WHERE (
{old}
  ) AND NOT coalesce((
{new}
  ), 0)) AS only_old
"""

# How many earlier scenarios the scenario request is told of: the most recent
# ones. Older ones are left out, so one of them can be proposed again; the
# duplicate check still catches it if it leads to the same rule.
MAX_EARLIER_SCENARIOS: Final = 40
PROPOSE_REQUESTS: Final = 1
PROCESS_REQUESTS: Final = MAX_MODEL_CALLS

# The steps of processing one scenario, in order.
StepName = Literal["write_rule", "check_rule", "backtest", "duplicates", "verdict"]
STEPS: Final[tuple[StepName, ...]] = (
    "write_rule", "check_rule", "backtest", "duplicates", "verdict",
)  # fmt: skip


class RunProgress(BaseModel):
    """Where the processing of one scenario stands. What the page polls.

    Step names, the scenario's and the clause's text, and how it ended. While
    the run lasts it holds no figure; at the end `verdict` states what
    happened in one sentence, with counts and rates of the backtest, as the
    entry does. Never a row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    entry_id: int  # the proposed scenario being processed
    state: RunStatus
    step: StepName | None  # the step in progress; None once the run has ended
    done: tuple[StepName, ...]
    attempt: int  # 1, or 2 once the model was asked to repair its first reply
    scenario: Scenario
    clause: str | None  # the clause the guard accepted, once there is one
    # How it ended. `added`: the entry now holds the result. `duplicate`: the
    # rule was already entry `result_entry_id`, and the scenario was dropped.
    outcome: Literal["added", "duplicate"] | None = None
    result_entry_id: int | None = None
    status: EvidenceStatus | None = None
    match_kind: MatchKind | None = None
    error: RunError | None = None
    # What happened, in one plain sentence written by code. It may state counts
    # and rates of the backtest, as the entry does; never a row.
    verdict: str | None = None


Listener = Callable[[RunProgress], None]


class FinderBusy(Exception):
    """A propose or a process is in progress in this process; another is refused, not queued."""


def request_bound(batch_size: int) -> int:
    """The most model requests it takes to propose and process `batch_size` scenarios."""
    return PROPOSE_REQUESTS + PROCESS_REQUESTS * batch_size


def rule_question(scenario: Scenario, limit: int) -> str:
    """The message a scenario is put to the turn as: its own text, and nothing measured.

    Its assumptions are added when the whole stays within `limit` characters.
    """
    question = f"{QUESTION_PREFIX}{scenario.title}: {scenario.hypothesis}"
    if scenario.assumptions:
        longer = f"{question} Assumptions made for it: {'; '.join(scenario.assumptions)}"
        if len(longer) <= limit:
            return longer
    return question


def judge(card: RuleCard) -> tuple[EvidenceStatus, tuple[Criterion, ...]]:
    """The status a rule's evidence earns, and every check behind it."""
    found, later = card.earlier, card.later
    discovery = (
        _at_least("labelled", "discovery", found.flagged_labelled, DISCOVERY_MIN_LABELLED,
                  "labelled flagged transactions"),
        _at_least("fraud_caught", "discovery", found.true_positives, DISCOVERY_MIN_FRAUD,
                  "frauds caught"),
        _lift("lift", "discovery", found, DISCOVERY_MIN_LIFT),
    )  # fmt: skip
    kept = None if not found.lift or later.lift is None else later.lift / found.lift
    confirmation = (
        _at_least("labelled", "confirmation", later.flagged_labelled, CONFIRMATION_MIN_LABELLED,
                  "labelled flagged transactions"),
        _at_least("fraud_caught", "confirmation", later.true_positives, CONFIRMATION_MIN_FRAUD,
                  "frauds caught"),
        _lift("lift", "confirmation", later, CONFIRMATION_MIN_LIFT),
        Criterion(
            code="lift_kept",
            period="confirmation",
            passed=kept is not None and kept >= CONFIRMATION_LIFT_RATIO,
            detail=f"Lift at least {CONFIRMATION_LIFT_RATIO:.0%} of the discovery period's: "
            + ("cannot be compared." if kept is None else f"it kept {kept:.0%}."),
        ),
    )  # fmt: skip
    status: EvidenceStatus = "confirmed"
    if not all(check.passed for check in discovery):
        status = "no_signal"
    elif not all(check.passed for check in confirmation):
        status = "not_confirmed"
    return status, (*discovery, *confirmation)


class _Tally:
    """What a run has cost so far. Private working state."""

    def __init__(self) -> None:
        self.scenarios = 0
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0


class Finder:
    """Proposes scenarios and processes them, over one database into one bank, one at a time."""

    def __init__(
        self,
        settings: Settings,
        scenario_model: IntentModel,
        turn_model: IntentModel,
        bank: RuleBank,
        *,
        source: RunSource,
        model_id: str | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        """`scenario_model` answers the scenario request and `turn_model` the turns'.

        They are two bindings of one model: each offers its own tools. A run
        the bank still records as running was left by a process that ended:
        it is marked interrupted here.
        """
        self._settings = settings
        self._scenario_model = scenario_model
        self._turn_model = turn_model
        self._source: RunSource = source
        self._model_id = model_id or settings.anthropic_model
        self._tracer = tracer
        self._lock = threading.Lock()
        self._watch = threading.Lock()  # guards `_progress`
        self._progress: RunProgress | None = None
        self._worker: threading.Thread | None = None
        self.bank = bank
        try:
            left = bank.interrupt_running()
        except BankError as error:
            logger.warning("finder: the bank could not be checked for interrupted runs (%s)", error.kind)
        else:
            if left:
                logger.warning("finder: %d run(s) left running were marked interrupted", left)

    @property
    def running(self) -> bool:
        return self._lock.locked()

    @property
    def batch_size(self) -> int:
        return self._settings.finder_batch_size

    @property
    def active(self) -> RunProgress | None:
        """The processing in progress, if any."""
        with self._watch:
            found = self._progress
        return found if found is not None and found.state == "running" else None

    def progress(self, run_id: int) -> RunProgress | None:
        """The processing run `run_id`, while this process remembers it: its latest one."""
        with self._watch:
            found = self._progress
        return found if found is not None and found.run_id == run_id else None

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the processing started by `start_process` to end."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    # --- propose ---------------------------------------------------------------------------

    def propose(self, count: int = 1) -> FinderRun:
        """Ask the model for `count` scenarios and save them as proposed. One request.

        Raises `FinderBusy`, and nothing else: a failure is the run's status.
        """
        if not self._lock.acquire(blocking=False):
            raise FinderBusy()
        try:
            return self._recorded("propose", count, PROPOSE_REQUESTS, lambda run_id, tally: (
                self._propose(run_id, count, tally)
            ))  # fmt: skip
        finally:
            self._lock.release()

    def _propose(self, run_id: int, count: int, tally: _Tally) -> tuple[RunStatus, RunError | None]:
        settings = self._settings
        try:
            schema_context = build_schema_context(settings.db_path)
        except db.DbError as error:
            return "failed", RunError(
                kind="database",
                message=f"The transaction database could not be read ({type(error).__name__}).",
            )
        earlier = self.bank.earlier_scenarios(MAX_EARLIER_SCENARIOS)
        tally.requests += 1
        try:
            reply = self._scenario_model.complete(build_request(schema_context, count, earlier))
        except ModelOutputError as error:
            tally.input_tokens += error.reply.usage.input_tokens
            tally.output_tokens += error.reply.usage.output_tokens
            return "failed", RunError(
                kind="model_output", message=f"No usable scenarios: the reply was {error.kind}."
            )
        except ModelError as error:
            kind = error.kind if isinstance(error, ModelUnavailable) else type(error).__name__
            return "failed", RunError(
                kind="model_unavailable", message=f"No reply from the model ({kind})."
            )
        tally.input_tokens += reply.usage.input_tokens
        tally.output_tokens += reply.usage.output_tokens
        found = read_reply(reply, count)
        if isinstance(found, ScenarioFailure):
            if found.kind == "empty" and self._source == "offline_demo":
                return "failed", RunError(
                    kind="demo_exhausted",
                    message="The offline demo has no canned scenario left: every one of them is "
                    "in the rule bank already. Delete one to see it proposed again.",
                )
            return "failed", RunError(kind="scenario_shape", message=found.reason)
        tally.scenarios = len(found)
        for position, scenario in enumerate(found, start=1):
            self.bank.add_entry(
                NewEntry(
                    run_id=run_id,
                    position=position,
                    title=scenario.title,
                    hypothesis=scenario.hypothesis,
                    columns=scenario.columns,
                    scenario_assumptions=scenario.assumptions,
                    status="proposed",
                    turn_outcome=None,
                    turn_message=None,
                    refused=(),
                    clause=None,
                    explanation=None,
                    rule_assumptions=(),
                    rule=None,
                    caveats=(),
                    criteria=(),
                    duplicate_check="not_applicable",
                    model_id=reply.model_id,
                    scenario_prompt_version=SCENARIO_PROMPT_VERSION,
                    rule_prompt_version=PROMPT_VERSION,
                )
            )
        return "completed", None

    # --- process ---------------------------------------------------------------------------

    def start_process(self, entry_id: int) -> RunProgress:
        """Begin processing a proposed scenario on a worker thread and return at once.

        Raises `FinderBusy`, or `BankError` when the entry is unknown, is not
        a proposed scenario, or the bank cannot record the run.
        """
        if not self._lock.acquire(blocking=False):
            raise FinderBusy()
        try:
            first, entry, run_id = self._begin(entry_id, None)
        except BaseException:
            self._lock.release()
            raise

        def work() -> None:
            try:
                self._process(run_id, entry, None)
            finally:
                self._lock.release()

        self._worker = threading.Thread(target=work, name="fsm-assistant-finder", daemon=True)
        self._worker.start()
        return first

    def process(self, entry_id: int, listener: Listener | None = None) -> FinderRun:
        """Process a proposed scenario and return when it is done. For the terminal."""
        if not self._lock.acquire(blocking=False):
            raise FinderBusy()
        try:
            _, entry, run_id = self._begin(entry_id, listener)
            return self._process(run_id, entry, listener)
        finally:
            self._lock.release()

    def entry(self, entry_id: int) -> BankEntry:
        """One entry in full. A rule that flags nothing also gets its likely reason, when one is plain.

        Raises `BankError`. The reason is read from the schema context and the
        clause, for the reader of the entry: it is not stored, sent or logged.
        """
        found = self.bank.entry(entry_id)
        if found.clause is None or found.rule is None or found.rule.flagged != 0:
            return found
        try:
            reason = absent_value(found.clause, build_schema_context(self._settings.db_path))
        except db.DbError:
            reason = None
        if reason is None:
            return found
        return found.model_copy(update={"verdict": f"{found.verdict} {reason}"})

    def discard(self, entry_id: int) -> BankEntry:
        """Set a proposed scenario aside without a model call. Raises `FinderBusy`, `BankError`."""
        self._not_in_progress(entry_id)
        return self.bank.discard(entry_id)

    def delete(self, entry_id: int) -> None:
        """Remove an entry from the bank for good. Raises `FinderBusy`, `BankError`."""
        self._not_in_progress(entry_id)
        self.bank.delete(entry_id)
        logger.info("finder entry deleted: entry=%d", entry_id)

    def _not_in_progress(self, entry_id: int) -> None:
        active = self.active
        if active is not None and active.entry_id == entry_id:
            raise FinderBusy()

    def _begin(self, entry_id: int, listener: Listener | None) -> tuple[RunProgress, BankEntry, int]:
        entry = self.bank.entry(entry_id)
        if entry.status != "proposed":
            raise BankError(
                "not_processable", f"Entry {entry_id} is not a scenario waiting to be processed."
            )
        run_id = self.bank.start_run(
            kind="process",
            source=self._source,
            batch_size=1,
            request_bound=PROCESS_REQUESTS,
            model_id=self._model_id,
            scenario_prompt_version=entry.scenario_prompt_version,
            rule_prompt_version=PROMPT_VERSION,
        )
        logger.info("finder process started: run=%d entry=%d", run_id, entry_id)
        first = RunProgress(
            run_id=run_id,
            entry_id=entry_id,
            state="running",
            step="write_rule",
            done=(),
            attempt=1,
            scenario=Scenario(
                title=entry.title,
                hypothesis=entry.hypothesis,
                columns=entry.columns,
                assumptions=entry.scenario_assumptions,
            ),
            clause=None,
        )
        self._tell(first, listener)
        return first, entry, run_id

    def _tell(self, progress: RunProgress, listener: Listener | None) -> None:
        with self._watch:
            self._progress = progress
        if listener is not None:
            try:
                listener(progress)
            except Exception:  # a view of the run, not part of it
                pass

    def _process(self, run_id: int, entry: BankEntry, listener: Listener | None) -> FinderRun:
        started = time.monotonic()
        tally = _Tally()
        tally.scenarios = 1
        with self._watch:
            current = self._progress
        assert current is not None
        box = [current]

        def move(**changes: object) -> None:
            step = changes.get("step", box[0].step)
            if step is not None:
                changes["done"] = STEPS[: STEPS.index(cast(StepName, step))]
            box[0] = box[0].model_copy(update=changes)
            self._tell(box[0], listener)

        status: RunStatus
        failure: RunError | None
        try:
            status, failure = self._work(run_id, entry, tally, move)
        except BankError as error:
            status, failure = "failed", RunError(kind="bank_unavailable", message=str(error))
        except Exception as error:  # the interface boundary: every fault becomes an outcome
            logger.error("finder process failed: %s", type(error).__name__)
            status, failure = "failed", RunError(
                kind="internal",
                message=f"An internal error stopped the run ({type(error).__name__}); the "
                "scenario is still waiting to be processed.",
            )
        elapsed = round((time.monotonic() - started) * 1000)
        logger.log(
            logging.INFO if status == "completed" else logging.ERROR,
            "finder process finished: run=%d entry=%d status=%s error=%s outcome=%s requests=%d "
            "elapsed_ms=%d",
            run_id, entry.id, status, "-" if failure is None else failure.kind,
            box[0].outcome or "-", tally.requests, elapsed,
        )  # fmt: skip
        recorded = self._close(run_id, "process", status, failure, tally, elapsed, PROCESS_REQUESTS)
        ended: dict[str, object] = {"state": recorded.status, "step": None, "error": recorded.error}
        if recorded.status == "completed":
            # The steps that were gone through: all of them for a measured rule, up to
            # the duplicate check for a duplicate, the writing alone when no rule came.
            reached = len(STEPS) if box[0].status != "not_built" else 1
            ended["done"] = STEPS[: reached - (box[0].outcome == "duplicate")]
        box[0] = box[0].model_copy(update=ended)
        self._tell(box[0], listener)
        return recorded

    def _work(
        self, run_id: int, entry: BankEntry, tally: _Tally, move: Callable[..., None]
    ) -> tuple[RunStatus, RunError | None]:
        settings = self._settings
        scenario = Scenario(
            title=entry.title,
            hypothesis=entry.hypothesis,
            columns=entry.columns,
            assumptions=entry.scenario_assumptions,
        )
        calls = [0]

        def on_step(step: TraceStep) -> None:
            """Where the turn is, from the node that just finished. Never a row or a figure."""
            failed = "failure " in step.summary
            if step.node == "call_model":
                calls[0] += 1
                move(step="check_rule", attempt=max(1, calls[0]))
            elif step.node in ("check_intent", "guard_rule", "backtest_rule") and failed:
                if calls[0] < MAX_MODEL_CALLS:  # the model is asked once more; else the turn ends
                    move(step="write_rule", attempt=calls[0] + 1)
            elif step.node == "guard_rule":
                move(step="backtest")

        # A conversation of its own: no turn sees another's.
        assistant = Assistant(
            settings, self._turn_model, label=Label(command="finder"), tracer=self._tracer
        )
        question = rule_question(scenario, settings.max_message_characters)
        result = assistant.run_turn(question, f"finder-{run_id}", on_step=on_step)
        tally.requests += result.meta.model_calls
        tally.input_tokens += result.meta.input_tokens
        tally.output_tokens += result.meta.output_tokens
        if result.error is not None and result.error.kind == "model_unavailable":
            return "failed", RunError(
                kind="model_unavailable",
                message="The model did not answer, so no rule was written. The scenario is still "
                "waiting to be processed.",
            )

        clause, card = result.where_clause, result.rule
        status: EvidenceStatus = "not_built"
        criteria: tuple[Criterion, ...] = ()
        checked: DuplicateCheck = "not_applicable"
        if result.outcome != "rule_proposed" or clause is None or card is None:
            clause, card = None, None
        else:
            move(step="duplicates", clause=clause)
            match = self._match(clause, card.flagged, entry.id)
            if isinstance(match, tuple):
                return self._matched(entry.id, match[0], match[1], run_id, clause, move)
            checked = match
            move(step="verdict")
            status, criteria = judge(card)
        completed = NewEntry(
            run_id=entry.run_id,
            position=entry.position,
            title=entry.title,
            hypothesis=entry.hypothesis,
            columns=entry.columns,
            scenario_assumptions=entry.scenario_assumptions,
            status=status,
            turn_outcome=result.outcome,
            turn_message=None if clause is not None else _turn_message(result),
            refused=tuple(str(code) for code in result.refused),
            clause=clause,
            explanation=result.explanation if clause is not None else None,
            rule_assumptions=result.assumptions if clause is not None else (),
            rule=card,
            caveats=result.caveats if clause is not None else (),
            criteria=criteria,
            duplicate_check=checked,
            model_id=result.meta.model_id,
            scenario_prompt_version=entry.scenario_prompt_version,
            rule_prompt_version=result.meta.prompt_version,
            processed_run_id=run_id,
            turn_error_kind=None if result.error is None else result.error.kind,
        )
        try:
            self.bank.complete(entry.id, completed)
        except DuplicateClause as error:  # another writer added the clause meanwhile
            assert clause is not None
            return self._matched(entry.id, error.entry_id, "same_clause", run_id, clause, move)
        move(
            outcome="added",
            result_entry_id=entry.id,
            status=status,
            verdict=self.entry(entry.id).verdict,
        )
        return "completed", None

    def _matched(
        self,
        entry_id: int,
        target: int,
        kind: MatchKind,
        run_id: int,
        clause: str,
        move: Callable[..., None],
    ) -> tuple[RunStatus, RunError | None]:
        self.bank.complete_as_match(entry_id, target, run_id=run_id, kind=kind, clause=clause)
        same = "the same clause" if kind == "same_clause" else "a clause that flags exactly the same transactions"
        move(
            outcome="duplicate",
            result_entry_id=target,
            status=self.bank.entry(target).status,
            match_kind=kind,
            verdict=f"Duplicate: the model wrote {same} as rule {target}, so no entry was added.",
        )
        return "completed", None

    def _match(
        self, clause: str, flagged: int, own: int
    ) -> tuple[int, MatchKind] | DuplicateCheck:
        """The entry `clause` duplicates and why, or whether the comparison was complete."""
        same = self.bank.find_clause(clause)
        if same is not None and same != own:
            return same, "same_clause"
        if flagged == 0:
            # Rules that flag nothing all flag "the same transactions" and are not
            # the same rule: only the same clause makes one of them a duplicate.
            return "complete"
        complete = True
        for entry_id, other in self.bank.candidates(flagged):
            verdict = self._same_transactions(clause, other)
            if verdict is None:
                complete = False
            elif verdict:
                return entry_id, "same_transactions"
        return "complete" if complete else "incomplete"

    def _same_transactions(self, new: str, old: str) -> bool | None:
        """Whether the two clauses flag exactly the same transactions; None when it cannot be said.

        Two counts are read and nothing else: the transactions one flags and
        the other does not, each way. A clause read back from the bank is
        judged by the guard again before it is placed in a statement.
        """
        settings = self._settings
        try:
            texts = []
            for clause in (new, old):
                decision = check_rule_clause(clause, db_path=settings.db_path)
                if not decision.accepted:
                    return None
                texts.append(decision.sql)
            result = db.run_query(
                _SAME_TRANSACTIONS.format(new=texts[0], old=texts[1]),
                db_path=settings.db_path,
                row_cap=1,
                timeout_seconds=settings.query_timeout_seconds,
            )
        except db.DbError as error:
            logger.warning("finder: two rules could not be compared: %s", type(error).__name__)
            return None
        only_new, only_old = result.rows[0]
        return only_new == 0 and only_old == 0

    # --- a run, recorded -------------------------------------------------------------------

    def _recorded(
        self,
        kind: RunKind,
        size: int,
        bound: int,
        work: Callable[[int, _Tally], tuple[RunStatus, RunError | None]],
    ) -> FinderRun:
        started = time.monotonic()
        tally = _Tally()
        try:
            run_id = self.bank.start_run(
                kind=kind,
                source=self._source,
                batch_size=size,
                request_bound=bound,
                model_id=self._model_id,
                scenario_prompt_version=SCENARIO_PROMPT_VERSION,
                rule_prompt_version=PROMPT_VERSION,
            )
        except BankError as error:
            logger.error("finder %s not started: the bank is unavailable (%s)", kind, error.kind)
            return self._unrecorded(
                kind, RunError(kind="bank_unavailable", message=str(error)), tally, 0, size, bound
            )
        logger.info(
            "finder %s started: run=%d scenarios=%d model=%s source=%s",
            kind, run_id, size, self._model_id, self._source,
        )  # fmt: skip
        status: RunStatus
        failure: RunError | None
        try:
            status, failure = work(run_id, tally)
        except BankError as error:
            status, failure = "failed", RunError(kind="bank_unavailable", message=str(error))
        except Exception as error:  # the interface boundary: every fault becomes an outcome
            logger.error("finder %s failed: %s", kind, type(error).__name__)
            status, failure = "failed", RunError(
                kind="internal", message=f"An internal error stopped the run ({type(error).__name__})."
            )  # fmt: skip
        elapsed = round((time.monotonic() - started) * 1000)
        logger.log(
            logging.INFO if status == "completed" else logging.ERROR,
            "finder %s finished: run=%d status=%s error=%s scenarios=%d requests=%d elapsed_ms=%d",
            kind, run_id, status, "-" if failure is None else failure.kind, tally.scenarios,
            tally.requests, elapsed,
        )  # fmt: skip
        return self._close(run_id, kind, status, failure, tally, elapsed, bound, size)

    def _close(
        self,
        run_id: int,
        kind: RunKind,
        status: RunStatus,
        failure: RunError | None,
        tally: _Tally,
        elapsed: int,
        bound: int,
        size: int = 1,
    ) -> FinderRun:
        try:
            self.bank.finish_run(
                run_id,
                status=status,
                error=failure,
                scenarios=tally.scenarios,
                model_requests=tally.requests,
                input_tokens=tally.input_tokens,
                output_tokens=tally.output_tokens,
                elapsed_ms=elapsed,
            )
            recorded = self.bank.run(run_id)
        except BankError as error:
            recorded = None
            failure = failure or RunError(kind="bank_unavailable", message=str(error))
        return recorded or self._unrecorded(kind, failure, tally, elapsed, size, bound)

    def _unrecorded(
        self, kind: RunKind, failure: RunError | None, tally: _Tally, elapsed: int, size: int, bound: int
    ) -> FinderRun:
        """The run as a value when the bank could not hold it."""
        return FinderRun(
            id=None,
            kind=kind,
            status="failed",
            error=failure,
            source=self._source,
            batch_size=size,
            scenarios=tally.scenarios,
            items=(),
            model_requests=tally.requests,
            request_bound=bound,
            input_tokens=tally.input_tokens,
            output_tokens=tally.output_tokens,
            elapsed_ms=elapsed,
            model_id=self._model_id,
            scenario_prompt_version=SCENARIO_PROMPT_VERSION,
            rule_prompt_version=PROMPT_VERSION,
            started_at="",
            finished_at=None,
        )


_SCHEMA_TABLE = re.compile(r"^(\w+) \(\d+ rows\)$")
_SCHEMA_VALUES = re.compile(r"^  (\w+) \w+.* values: (.*?)(?: \(NULL in \d+ rows\))?$")
# With one of these in a condition, a comparison that can never be true does
# not by itself make the condition false: an alternative, a negation, an outer
# join, or an aggregate, which returns a row over no rows.
_NOT_A_PLAIN_CONJUNCTION: Final = frozenset(
    {
        "or", "not", "case", "else", "union", "except", "coalesce", "ifnull", "left", "outer",
        "cross", "group", "having", "count", "sum", "avg", "min", "max", "total",
    }
)  # fmt: skip


def absent_value(clause: str, schema_context: str) -> str | None:
    """Why `clause` can flag nothing, when the schema context alone shows it. Else None.

    The schema context lists every value of a column that holds few of them.
    The clause is split at its top-level ANDs; when one of those conditions is
    itself plain (no OR, NOT, outer join or aggregate anywhere in it) and
    compares such a column with a value that is not listed, that condition is
    never true, so no row can match. Anything less plain (a top-level OR, a
    column of that name in two tables) gives None: no guess is made.
    """
    listed: dict[str, list[tuple[str, frozenset[str], str]]] = {}
    table = ""
    for line in schema_context.splitlines():
        header, values = _SCHEMA_TABLE.match(line), _SCHEMA_VALUES.match(line)
        if header:
            table = header.group(1)
        elif values and table:
            try:
                tokens = frozenset(token.text for token in scan(values.group(2)) if token.kind != "symbol")
            except UnterminatedQuote:
                continue
            listed.setdefault(values.group(1).lower(), []).append((table, tokens, values.group(2)))
    try:
        tokens_ = [token for token in scan(clause) if token.kind != "comment"]
    except UnterminatedQuote:
        return None
    conditions: list[list[Token]] = [[]]
    depth = 0
    for token in tokens_:
        word = token.text.lower() if token.kind == "word" else ""
        if token.kind == "symbol" and token.text in "()":
            depth += 1 if token.text == "(" else -1
        if depth == 0 and word == "or":
            return None
        if depth == 0 and word == "and":
            conditions.append([])
        else:
            conditions[-1].append(token)
    plain = [
        token
        for condition in conditions
        if not any(t.kind == "word" and t.text.lower() in _NOT_A_PLAIN_CONJUNCTION for t in condition)
        for token in (*condition, Token(kind="symbol", text=";"))
    ]
    for column, equals, value in zip(plain, plain[1:], plain[2:]):
        tables = listed.get(column.text.lower(), []) if column.kind == "word" else []
        literal = value.kind == "string" or (value.kind == "word" and value.text.isdigit())
        if len(tables) != 1 or equals.kind != "symbol" or equals.text != "=" or not literal:
            continue
        name, known, shown = tables[0]
        if value.text not in known:
            written = f"'{value.text}'" if value.kind == "string" else value.text
            only = "the value" if len(known) == 1 else "the values"
            return (
                f"The likely reason: no row of {name} has {column.text} = {written}. "
                f"The schema lists only {only} {shown} for that column."
            )
    return None


def _turn_message(result: TurnResult) -> str:
    """What a turn that built no rule said. Never SQLite's run-time words, nor an exception's."""
    error = result.error
    if error is None:
        return result.answer  # the model's question, or its reason for declining
    if error.kind in ("internal", "model_unavailable"):
        return f"The turn failed ({error.kind})."
    return error.message


def _at_least(code: str, period: Period, value: int, floor: int, noun: str) -> Criterion:
    return Criterion(
        code=code, period=period, passed=value >= floor,
        detail=f"At least {floor:,} {noun}: {value:,}.",
    )  # fmt: skip


def _lift(code: str, period: Period, figures: RulePeriod, floor: float) -> Criterion:
    lift = figures.lift
    return Criterion(
        code=code,
        period=period,
        passed=lift is not None and lift > floor,
        detail=f"Lift above {floor:g} times the period's base fraud rate: "
        + ("undefined (nothing flagged is labelled)." if lift is None else f"{lift:,.2f}."),
    )


def render_plan(settings: Settings, bank_path: str) -> str:
    """What the command would send, for one that has not been told to spend."""
    size = settings.finder_batch_size
    return "\n".join(
        [
            f"find-rules, on {settings.anthropic_model}, in two phases:",
            f"  1. Propose: one request for {size} fraud scenario{'' if size == 1 else 's'} written "
            "from the schema alone.",
            f"  2. Process: one rule turn per scenario, at most {PROCESS_REQUESTS} requests each.",
            f"  At most {request_bound(size)} model requests (1 + {PROCESS_REQUESTS} x {size}), "
            "each billed to ANTHROPIC_API_KEY.",
            f"  Results go to the rule bank at {bank_path}; the transaction database is only read.",
        ]
    )


STEP_TEXT: Final[dict[StepName, str]] = {
    "write_rule": "writing the rule (model request)",
    "check_rule": "checking the rule (SQL guard)",
    "backtest": "testing on the discovery period, then the confirmation period",
    "duplicates": "checking for duplicates in the bank",
    "verdict": "judging the evidence",
}


def render_step(progress: RunProgress) -> str | None:
    """One line for a step that has just begun; None for a frame that begins none."""
    if progress.step is None:
        return None
    again = " again" if progress.attempt > 1 and progress.step == "write_rule" else ""
    return f"     … {STEP_TEXT[progress.step]}{again}"


def render_run(run: FinderRun) -> str:
    """One run as plain text for a terminal: a propose, or the processing of one scenario."""
    name = "Run" if run.id is None else f"Run {run.id}"
    cost = (
        f"{run.model_requests} request{'' if run.model_requests == 1 else 's'} (at most "
        f"{run.request_bound}), {run.input_tokens:,} input and {run.output_tokens:,} output tokens, "
        f"{run.elapsed_ms / 1000:.1f} s"
    )
    if run.kind == "propose":
        lines = [f"{name} (propose): {run.status}. {run.scenarios} of {run.batch_size} "
                 f"scenario{'' if run.batch_size == 1 else 's'} returned; {cost}."]  # fmt: skip
    else:
        lines = [f"{name} (process): {run.status}; {cost}."]
    if run.error is not None:
        lines.append(f"  Stopped: {safe_text(run.error.message)} [{run.error.kind}]")
    for item in run.items:
        if item.result == "proposed":
            what = f"entry {item.entry_id}, {_STATUS_TEXT[item.status]}"
        elif item.result == "added":
            what = f"entry {item.entry_id}, {_STATUS_TEXT[item.status]}"
        else:
            same = "the same clause" if item.match_kind == "same_clause" else "the same transactions"
            what = f"already in the bank as entry {item.entry_id} ({same}); no entry was added"
        lines.append(f"  {item.position:>2}. {safe_text(item.title, 60)}: {what}")
    return "\n".join(lines)


def render_bank(entries: Sequence[EntrySummary]) -> str:
    """The bank's ranked table as plain text, with the discovery period's figures."""
    if not entries:
        return "The rule bank is empty."
    header = (
        "Id", "Status", "Decision", "Flagged", "Fraud", "Genuine", "Precision", "Recall", "Lift",
        "Confirmed lift", "Rule",
    )  # fmt: skip
    rows = [header]
    for entry in entries:
        found, later = entry.discovery, entry.confirmation
        rows.append(
            (
                str(entry.id),
                _STATUS_TEXT[entry.status],
                entry.decision,
                *(
                    ("-",) * 6
                    if found is None
                    else (
                        f"{found.flagged:,}", f"{found.true_positives:,}",
                        f"{found.false_positives:,}", _percent(found.precision),
                        _percent(found.recall), _times(found.lift),
                    )
                ),
                "-" if later is None else _times(later.lift),
                safe_text(entry.title, 48),
            )
        )  # fmt: skip
    widths = [max(len(row[index]) for row in rows) for index in range(len(header))]
    confirmed = sum(entry.status == "confirmed" for entry in entries)
    return "\n".join(
        [
            f"Rule bank: {len(entries)} entries, {confirmed} confirmed. Figures are the "
            f"discovery period's (the months before {LATER_PERIOD_START}); the confirmation "
            "period is the months from it.",
            *("  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
              for row in rows),
        ]
    )  # fmt: skip


_STATUS_TEXT: Final[dict[EvidenceStatus, str]] = {
    "confirmed": "confirmed",
    "not_confirmed": "not confirmed",
    "no_signal": "no signal",
    "not_built": "no rule built",
    "proposed": "proposed, waiting to be processed",
    "discarded": "discarded",
}


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.2%}"


def _times(value: float | None) -> str:
    return "-" if value is None else f"{value:,.1f}"
