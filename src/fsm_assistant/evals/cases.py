"""Evaluation cases: the format, the pinned reference results and the loader.

Responsibility: turn the TOML files under `evals/cases/` and the pin file
`evals/expected.json` into validated, immutable values. Nothing here opens
the database or calls a model: a reference is checked for shape only (one
statement, quotes closed, a clause without the word WHERE). Whether the
guard accepts a reference and whether it still returns its pinned result is
checked by `evidence`, against a database.

A case file sets one `tier` and holds `[[case]]` tables, each with one or
more `[[case.turn]]` tables. What a turn expects depends on its `outcome`:

- `answered`: a reference `sql` of ours, and how to compare a result with it
  (`ordered`, `compare_columns`, `exact_columns`, `allow_extra_columns`,
  `tolerance`). A scan case has `action = "profile_fraud"` and `scan_top`
  instead of `sql`.
- `rule_proposed`: a reference `clause`, the four counts its backtest gives,
  and how far the proposed rule's flagged set may differ from the
  reference's (`min_jaccard`, `min_overlap_precision`, `min_overlap_recall`).
- `clarification`, `cannot_answer`, `refused` (declined, or refused twice by
  the guard): no reference. Nothing may be executed in such a turn, unless
  it has an `or_rule` table: then a proposed rule is accepted too, when it is
  repeatable and its flag rate lies in the table's band.

A case can carry `known_limitation`, one line saying why the system is known
not to pass it. Such a case is run and counted as any other; the report
shows its failure, or its pass, on a line of its own.
"""

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from fsm_assistant.config import PROJECT_ROOT
from fsm_assistant.sql_guard import normalise
from fsm_assistant.sql_scan import UnterminatedQuote, scan

DEFAULT_CASES_DIRECTORY = PROJECT_ROOT / "evals" / "cases"
DEFAULT_PINS_FILE = PROJECT_ROOT / "evals" / "expected.json"

DEFAULT_TOLERANCE = 1e-6

Tier = Literal[
    "aggregates",
    "joins",
    "fraud",
    "dates",
    "top_n",
    "traps",
    "follow_ups",
    "ambiguous",
    "unanswerable",
    "adversarial",
    "rules",
    "rule_policy",
    "scan",
    "paraphrases",
]
TIERS: tuple[Tier, ...] = get_args(Tier)

# `refused` is met by a decline or by the guard refusing both attempts.
ExpectedOutcome = Literal["answered", "rule_proposed", "clarification", "cannot_answer", "refused"]
ExpectedAction = Literal[
    "run_sql", "propose_rule", "profile_fraud", "ask_clarification", "decline"
]
# Things a turn can be seen to have done. `executed`: a statement or a
# backtest ran. `repaired`: the turn needed its second model request.
Fact = Literal[
    "executed",
    "repaired",
    "caveat_truncated",
    "caveat_label_coverage",
    "caveat_zero_rows",
    "caveat_small_sample",
    "caveat_high_flag_rate",
]
# A part of the system a case needs; the runner skips the case without it.
Feature = Literal["scan"]

# What a pinned result can hold: the data has no blob column.
type PinCell = str | int | float | None

_NO_EXECUTION: frozenset[ExpectedOutcome] = frozenset({"clarification", "cannot_answer", "refused"})
_COMPARISON_OPTIONS = frozenset(
    {"ordered", "compare_columns", "exact_columns", "allow_extra_columns", "tolerance"}
)
_ID = r"^[a-z0-9]+(-[a-z0-9]+)*$"
_TAG = r"^[a-z0-9]+([:_-][a-z0-9]+)*$"
_Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
_Count = Annotated[int, Field(ge=0)]


class CaseFileError(Exception):
    """A case file or the pin file cannot be used. `source` names the file."""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message


class Comparison(BaseModel):
    """How a result is compared with a reference result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ordered: bool = False
    compare_columns: tuple[int, ...] | None = None  # reference positions; None is all of them
    # Neither: the reference's columns lead and further ones may follow, with a note.
    exact_columns: bool = False  # a result wider than the reference fails
    allow_extra_columns: bool = False  # the reference's columns may be in any position
    tolerance: float = DEFAULT_TOLERANCE


class AcceptedRule(BaseModel):
    """A rule that is accepted in place of the refusal a turn expects.

    The rule must be repeatable (no random()) and flag a share of all
    transactions inside the band, both ends included.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_flag_rate: _Fraction
    max_flag_rate: _Fraction

    @model_validator(mode="after")
    def _a_band(self) -> Self:
        if self.min_flag_rate > self.max_flag_rate:
            raise ValueError("min_flag_rate must not be above max_flag_rate")
        return self


class Thresholds(BaseModel):
    """How close a proposed rule's flagged set must be to the reference's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_jaccard: float = 1.0
    min_overlap_precision: float = 1.0
    min_overlap_recall: float = 1.0


class ExpectedBacktest(BaseModel):
    """The counts the reference clause gives on the shipped data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flagged: int
    flagged_labelled: int
    true_positives: int
    false_positives: int


class Turn(BaseModel):
    """One message of a case and what the reply to it must be."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1)
    outcome: ExpectedOutcome
    action: ExpectedAction | None = None

    sql: str | None = None
    ordered: bool = False
    compare_columns: tuple[Annotated[int, Field(ge=0)], ...] | None = None
    exact_columns: bool = False
    allow_extra_columns: bool = False
    tolerance: Annotated[float, Field(ge=0.0, lt=1.0)] = DEFAULT_TOLERANCE

    clause: str | None = None
    min_jaccard: _Fraction = 1.0
    min_overlap_precision: _Fraction = 1.0
    min_overlap_recall: _Fraction = 1.0
    flagged: _Count | None = None
    flagged_labelled: _Count | None = None
    true_positives: _Count | None = None
    false_positives: _Count | None = None

    or_rule: AcceptedRule | None = None

    scan_top: tuple[str, ...] = ()
    require: tuple[Fact, ...] = ()
    forbid: tuple[Fact, ...] = ()

    @model_validator(mode="after")
    def _fields_match_outcome(self) -> Self:
        scan_turn = self.action == "profile_fraud"
        if (self.outcome == "answered" and not scan_turn) != (self.sql is not None):
            raise ValueError("a reference sql goes with outcome answered (not a scan), and only that")
        if (self.outcome == "rule_proposed") != (self.clause is not None):
            raise ValueError("a reference clause goes with outcome rule_proposed, and only that")
        if bool(self.scan_top) and not scan_turn:
            raise ValueError("scan_top goes with action profile_fraud only")
        counts = (self.flagged, self.flagged_labelled, self.true_positives, self.false_positives)
        if any(count is None for count in counts) == (self.clause is not None):
            raise ValueError(
                "flagged, flagged_labelled, true_positives and false_positives are all given "
                "with a clause, and only then"
            )
        if self.sql is None and _COMPARISON_OPTIONS & self.model_fields_set:
            raise ValueError("comparison options need a reference sql")
        if self.exact_columns and self.allow_extra_columns:
            raise ValueError("exact_columns and allow_extra_columns exclude each other")
        if self.or_rule is not None and self.outcome != "refused":
            raise ValueError("or_rule goes with outcome refused only")
        if self.clause is None and any(
            name.startswith("min_") for name in self.model_fields_set
        ):
            raise ValueError("overlap thresholds need a reference clause")
        if self.compare_columns is not None and (
            not self.compare_columns
            or len(set(self.compare_columns)) != len(self.compare_columns)
        ):
            raise ValueError("compare_columns must name at least one column, each once")
        if set(self.require) & self.forbidden:
            raise ValueError("a fact cannot be both required and forbidden")
        if self.sql is not None:
            _check_reference(self.sql, "sql")
        if self.clause is not None:
            _check_reference(self.clause, "clause")
        return self

    @property
    def comparison(self) -> Comparison:
        return Comparison(
            ordered=self.ordered,
            compare_columns=self.compare_columns,
            exact_columns=self.exact_columns,
            allow_extra_columns=self.allow_extra_columns,
            tolerance=self.tolerance,
        )

    @property
    def thresholds(self) -> Thresholds:
        return Thresholds(
            min_jaccard=self.min_jaccard,
            min_overlap_precision=self.min_overlap_precision,
            min_overlap_recall=self.min_overlap_recall,
        )

    @property
    def expected_backtest(self) -> ExpectedBacktest | None:
        if (
            self.flagged is None
            or self.flagged_labelled is None
            or self.true_positives is None
            or self.false_positives is None
        ):
            return None
        return ExpectedBacktest(
            flagged=self.flagged,
            flagged_labelled=self.flagged_labelled,
            true_positives=self.true_positives,
            false_positives=self.false_positives,
        )

    @property
    def forbidden(self) -> frozenset[Fact]:
        """The facts that fail this turn: `forbid`, and execution where nothing may run."""
        implied: frozenset[Fact] = (
            frozenset({"executed"}) if self.outcome in _NO_EXECUTION else frozenset()
        )
        return frozenset(self.forbid) | implied


class Case(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID)
    tier: Tier
    tests: str = Field(min_length=1)  # one line: what this case is there to show
    tags: tuple[Annotated[str, Field(pattern=_TAG)], ...] = ()
    # True when the question, or one nearly identical, is in the prompt's own
    # examples or in `model_check.CHECK_QUESTIONS`, or when a line of the
    # prompt was written because this case failed in a recorded run.
    tuned: bool
    paraphrase_of: str | None = None
    requires: Feature | None = None
    # Why the system is known not to pass this case. It is still run and counted.
    known_limitation: str | None = Field(default=None, min_length=1)
    turns: tuple[Turn, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if "\n" in self.tests:
            raise ValueError("tests is one line")
        if self.known_limitation is not None and "\n" in self.known_limitation:
            raise ValueError("known_limitation is one line")
        if (self.tier == "paraphrases") != (self.paraphrase_of is not None):
            raise ValueError("paraphrase_of is given for the paraphrases tier, and only there")
        if self.paraphrase_of is not None and self.tuned:
            raise ValueError("a paraphrase is never tuned")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("a tag is listed twice")
        scans = any(turn.action == "profile_fraud" for turn in self.turns)
        if scans != (self.requires == "scan"):
            raise ValueError('a case with a profile_fraud turn has requires = "scan", and only it')
        return self


class PinnedResult(BaseModel):
    """The rows a reference query returned when it was verified.

    `reference_sha256` is the digest of the reference text the rows belong
    to, so a reference edited without re-pinning is caught without a database.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    turn: int = Field(ge=1)
    reference_sha256: str
    rows: tuple[tuple[PinCell, ...], ...]


class Pins(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    results: tuple[PinnedResult, ...]

    def find(self, case_id: str, turn: int) -> PinnedResult | None:
        for pin in self.results:
            if pin.case_id == case_id and pin.turn == turn:
                return pin
        return None


class Suite(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cases: tuple[Case, ...]
    pins: Pins

    def case(self, case_id: str) -> Case:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(case_id)

    def pin(self, case: Case, turn: int) -> PinnedResult | None:
        """The pinned result of the case's turn (counted from 1), if it has a reference query."""
        return self.pins.find(case.id, turn)


def reference_digest(text: str) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def load_cases(directory: Path = DEFAULT_CASES_DIRECTORY) -> tuple[Case, ...]:
    """Every case of every `*.toml` file in `directory`, in file then case order.

    Raises `CaseFileError` for a file that is not TOML, an unknown tier or
    field, a malformed reference, an id used twice or a paraphrase of a case
    that is missing, tuned or itself a paraphrase.
    """
    paths = sorted(directory.glob("*.toml"))
    if not paths:
        raise CaseFileError(directory.name, "no case files found")
    cases: list[Case] = []
    owner: dict[str, str] = {}
    for path in paths:
        for case in _read_file(path):
            if case.id in owner:
                raise CaseFileError(
                    path.name, f"case id {case.id!r} is already used in {owner[case.id]}"
                )
            owner[case.id] = path.name
            cases.append(case)
    by_id = {case.id: case for case in cases}
    for case in cases:
        if case.paraphrase_of is None:
            continue
        original = by_id.get(case.paraphrase_of)
        if original is None:
            raise CaseFileError(
                owner[case.id], f"{case.id} paraphrases {case.paraphrase_of!r}, which does not exist"
            )
        if original.paraphrase_of is not None or original.tuned:
            raise CaseFileError(
                owner[case.id],
                f"{case.id} paraphrases {original.id}, which must be a held-out case and not "
                "itself a paraphrase",
            )
    return tuple(cases)


def load_pins(path: Path = DEFAULT_PINS_FILE) -> Pins:
    if not path.is_file():
        raise CaseFileError(path.name, "the pin file is missing")
    try:
        return Pins.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise CaseFileError(path.name, _first_error(error)) from None


def load_suite(
    directory: Path = DEFAULT_CASES_DIRECTORY, pins_file: Path = DEFAULT_PINS_FILE
) -> Suite:
    """The cases with their pinned results, checked against each other.

    Raises `CaseFileError` when a reference query has no pin, when its text
    changed since it was pinned, or when a pin belongs to no reference query.
    """
    cases = load_cases(directory)
    pins = load_pins(pins_file)
    expected: dict[tuple[str, int], str] = {
        (case.id, number): reference_digest(turn.sql)
        for case in cases
        for number, turn in enumerate(case.turns, start=1)
        if turn.sql is not None
    }
    seen: set[tuple[str, int]] = set()
    for pin in pins.results:
        key = (pin.case_id, pin.turn)
        where = f"{pin.case_id} turn {pin.turn}"
        if key in seen:
            raise CaseFileError(pins_file.name, f"{where} is pinned twice")
        seen.add(key)
        if key not in expected:
            raise CaseFileError(pins_file.name, f"{where} is pinned but has no reference query")
        if expected[key] != pin.reference_sha256:
            raise CaseFileError(
                pins_file.name, f"the reference query of {where} changed since it was pinned"
            )
    for case_id, number in sorted(set(expected) - seen):
        raise CaseFileError(pins_file.name, f"{case_id} turn {number} has no pinned result")
    return Suite(cases=cases, pins=pins)


def suite_digest(
    directory: Path = DEFAULT_CASES_DIRECTORY, pins_file: Path = DEFAULT_PINS_FILE
) -> str:
    """One digest of the case files and the pin file, to label a run with."""
    digest = hashlib.sha256()
    for path in [*sorted(directory.glob("*.toml")), pins_file]:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _read_file(path: Path) -> list[Case]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise CaseFileError(path.name, f"not valid TOML: {error}") from None
    unknown = set(document) - {"tier", "case"}
    if unknown:
        raise CaseFileError(path.name, f"unknown top-level keys: {sorted(unknown)}")
    tier = document.get("tier")
    if tier not in TIERS:
        raise CaseFileError(path.name, f"unknown tier {tier!r}; known tiers: {', '.join(TIERS)}")
    entries = document.get("case")
    if not isinstance(entries, list) or not entries:
        raise CaseFileError(path.name, "no [[case]] tables")
    cases: list[Case] = []
    for entry in entries:
        name = entry.get("id", "?") if isinstance(entry, dict) else "?"
        if not isinstance(entry, dict) or "tier" in entry:
            raise CaseFileError(path.name, f"case {name!r}: the tier is set once, at the top of the file")
        fields = {key: value for key, value in entry.items() if key != "turn"}
        try:
            cases.append(Case.model_validate({**fields, "tier": tier, "turns": entry.get("turn", [])}))
        except ValidationError as error:
            raise CaseFileError(path.name, f"case {name!r}: {_first_error(error)}") from None
    return cases


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"{location}: {first['msg']}" if location else first["msg"]


def _check_reference(text: str, kind: Literal["sql", "clause"]) -> None:
    body = normalise(text)
    if not body:
        raise ValueError(f"the reference {kind} is empty")
    try:
        tokens = list(scan(body))
    except UnterminatedQuote as error:
        raise ValueError(f"the reference {kind} has an unterminated quote: {error}") from None
    words = [token.text.lower() for token in tokens if token.kind == "word"]
    if any(token.kind == "symbol" and token.text == ";" for token in tokens):
        raise ValueError(f"the reference {kind} must be one statement, without ';' inside")
    if any(token.kind == "comment" for token in tokens):
        raise ValueError(f"the reference {kind} must not contain a comment")
    first = words[0] if words else ""
    if kind == "sql" and first not in {"select", "with"}:
        raise ValueError("the reference sql must start with SELECT or WITH")
    if kind == "clause" and first == "where":
        raise ValueError("the reference clause is the condition only, without the word WHERE")
