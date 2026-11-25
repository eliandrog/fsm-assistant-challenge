"""The rule bank.

Responsibility: keep what the rule finder found, and what a reviewer decided
about it, in a SQLite file of its own (docs/DECISIONS.md, D-35). This is the
only module that opens that file, and it opens nothing else: the transaction
database is read by `db.py` alone, read-only, and a bank refuses to be
pointed at it.

What is stored. Per entry: the scenario (model-written text), the rule
clause the guard accepted, the model's assumptions, the backtest as counts
and rates (a `RuleCard`: aggregates only, no transaction, no id, no value of
a row), the status the evidence earned, the checks behind it, the reviewer's
decision and note, and the run, model and prompt versions it came from. A
scenario that did not become a rule is an entry too, with the outcome of its
turn. A scenario whose rule was already in the bank is not an entry: it is a
match, a line that points at the entry it duplicates.

No duplicates. `clause_key` is a clause with its spacing, comments and the
case of its keywords and names removed; a unique index on it makes "the same
clause twice" impossible, whoever writes. Whether a different clause flags
exactly the same transactions is decided by the finder, by SQL on the
transaction database, before it calls `add_entry`; `candidates` hands it the
entries worth comparing with (those that flag the same number of
transactions).

Every method opens a connection, does one thing and closes it, so the bank
can be used from any thread. Every fault of the file is raised as
`BankError`, with a sentence code wrote.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from fsm_assistant.result import Caveat, Outcome, RuleCard, RulePeriod
from fsm_assistant.rules.backtest import backtest_statements
from fsm_assistant.sql_scan import UnterminatedQuote, scan

SCHEMA_VERSION: Final = 2
MAX_NOTE_CHARACTERS: Final = 1000
_BUSY_SECONDS: Final = 5.0

# Where an entry stands, in the order entries are ranked. `proposed` is a
# scenario waiting for a reviewer to have it processed (D-36); `discarded` is
# one the reviewer did not want processed. The other four are what the
# evidence of a processed scenario amounts to.
EvidenceStatus = Literal[
    "confirmed", "not_confirmed", "no_signal", "not_built", "proposed", "discarded"
]
STATUS_ORDER: Final[tuple[EvidenceStatus, ...]] = (
    "confirmed", "not_confirmed", "no_signal", "not_built", "proposed", "discarded",
)  # fmt: skip
WAITING: Final[frozenset[EvidenceStatus]] = frozenset({"proposed", "discarded"})
Decision = Literal["pending", "approved", "rejected"]
# Why a scenario's rule counts as already in the bank.
MatchKind = Literal["same_clause", "same_transactions"]
# Whether the comparison with the entries already in the bank could be made.
DuplicateCheck = Literal["complete", "incomplete", "not_applicable"]
Period = Literal["discovery", "confirmation"]
# `interrupted`: the process that ran it ended before the run did.
RunStatus = Literal["running", "completed", "stopped", "failed", "interrupted"]
RunErrorKind = Literal[
    "bank_unavailable", "database", "model_unavailable", "model_output", "scenario_shape",
    "demo_exhausted", "interrupted", "internal",
]  # fmt: skip
# `propose` asks the model for scenarios; `process` turns one into a rule.
# `batch` is a run recorded before the two were separate.
RunKind = Literal["propose", "process", "batch"]
# Where the model replies of a run came from.
RunSource = Literal["live", "offline_demo", "scripted"]
BankErrorKind = Literal[
    "unavailable", "not_a_bank", "unknown_entry", "not_reviewable", "not_processable"
]


class BankError(Exception):
    """The bank could not do what was asked. `kind` says why, for code."""

    def __init__(self, kind: BankErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: BankErrorKind = kind


class DuplicateClause(Exception):
    """The clause is already in the bank, as entry `entry_id`."""

    def __init__(self, entry_id: int) -> None:
        super().__init__(f"the clause is already entry {entry_id}")
        self.entry_id = entry_id


class Criterion(BaseModel):
    """One check of the evidence, with the figures it was decided on. Written by code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    period: Period
    passed: bool
    detail: str


class ScenarioMatch(BaseModel):
    """A later scenario whose rule was this entry again, so no entry was added for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    position: int
    title: str
    hypothesis: str
    clause: str
    kind: MatchKind
    created_at: str


class NewEntry(BaseModel):
    """What the finder hands the bank for one scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    position: int  # the scenario's place in its run, from 1
    title: str
    hypothesis: str
    columns: tuple[str, ...]
    scenario_assumptions: tuple[str, ...]
    status: EvidenceStatus
    turn_outcome: Outcome | None  # None until the scenario has been processed
    turn_message: str | None  # what the turn said when it built no rule; written free of data
    refused: tuple[str, ...]  # the codes of the replies the shape check or the guard refused
    clause: str | None
    explanation: str | None
    rule_assumptions: tuple[str, ...]
    rule: RuleCard | None
    caveats: tuple[Caveat, ...]
    criteria: tuple[Criterion, ...]
    duplicate_check: DuplicateCheck
    model_id: str
    scenario_prompt_version: str
    rule_prompt_version: str
    processed_run_id: int | None = None  # the run that turned the scenario into a rule
    turn_error_kind: str | None = None  # why the turn failed, as a code, when it did


class BankEntry(NewEntry):
    """An entry as it is read back, with the reviewer's decision and what matched it since."""

    id: int
    created_at: str
    decision: Decision
    decision_note: str | None
    decided_at: str | None
    matches: tuple[ScenarioMatch, ...]
    # The two statements that measured the clause, as text. None without a clause.
    backtest_sql: str | None
    monthly_sql: str | None
    # What became of the scenario, in one plain sentence and in a few words. Written by code
    # from the status and the stored figures.
    verdict: str
    verdict_short: str


class EntrySummary(BaseModel):
    """One row of the ranked table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    run_id: int
    title: str
    status: EvidenceStatus
    decision: Decision
    turn_outcome: Outcome | None
    discovery: RulePeriod | None  # None when no rule was built
    confirmation: RulePeriod | None
    verdict_short: str  # why it ended as it did, in a few words
    matches: int
    created_at: str


class ProposedScenario(BaseModel):
    """A scenario waiting for the reviewer: to be processed into a rule, or discarded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    run_id: int
    title: str
    hypothesis: str
    columns: tuple[str, ...]
    assumptions: tuple[str, ...]
    created_at: str


class EarlierScenario(BaseModel):
    """A scenario the bank already holds, as the next scenario request is told of it.

    The model's own words and nothing measured: no status, no figure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    hypothesis: str


class RunItem(BaseModel):
    """What became of one scenario of a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int
    title: str
    # `proposed`: the run wrote the scenario. `added`: it processed it into an
    # entry. `duplicate`: its rule was already in the bank.
    result: Literal["proposed", "added", "duplicate"]
    entry_id: int  # the entry added, or the one the scenario's rule duplicates
    status: EvidenceStatus  # of that entry
    match_kind: MatchKind | None = None


class RunError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RunErrorKind
    message: str  # written by code


class FinderRun(BaseModel):
    """One run of the rule finder: what it asked for, what it cost and what it left."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int | None  # None when the bank could not be opened, so nothing was recorded
    kind: RunKind
    status: RunStatus
    error: RunError | None
    source: RunSource
    batch_size: int
    scenarios: int  # how many the model returned
    items: tuple[RunItem, ...]
    model_requests: int
    request_bound: int
    input_tokens: int
    output_tokens: int
    elapsed_ms: int
    model_id: str
    scenario_prompt_version: str
    rule_prompt_version: str
    started_at: str
    finished_at: str | None


_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  error_kind TEXT,
  error_message TEXT,
  source TEXT NOT NULL,
  batch_size INTEGER NOT NULL,
  scenarios INTEGER NOT NULL DEFAULT 0,
  model_requests INTEGER NOT NULL DEFAULT 0,
  request_bound INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  elapsed_ms INTEGER NOT NULL DEFAULT 0,
  model_id TEXT NOT NULL,
  scenario_prompt_version TEXT NOT NULL,
  rule_prompt_version TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  kind TEXT NOT NULL DEFAULT 'batch'
);
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  position INTEGER NOT NULL,
  status TEXT NOT NULL,
  clause_key TEXT,
  flagged INTEGER,
  decision TEXT NOT NULL DEFAULT 'pending',
  decision_note TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS entries_clause_key
  ON entries(clause_key) WHERE clause_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entries(id),
  run_id INTEGER NOT NULL REFERENCES runs(id),
  position INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  hypothesis TEXT NOT NULL,
  clause TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""
_TABLES: Final = frozenset({"runs", "entries", "matches"})


def clause_key(clause: str) -> str:
    """`clause` with what cannot change its meaning removed.

    Spacing and comments are dropped and bare words (keywords, names,
    numbers) are lowercased; a string keeps its exact text, because
    'Debit' and 'debit' are different values.
    """
    try:
        tokens = list(scan(clause))
    except UnterminatedQuote:
        return " ".join(clause.split())
    parts: list[str] = []
    for token in tokens:
        if token.kind == "comment":
            continue
        if token.kind == "string":
            parts.append("'" + token.text.replace("'", "''") + "'")
        elif token.kind == "name":
            parts.append('"' + token.text.replace('"', '""') + '"')
        else:
            parts.append(token.text.lower())
    return " ".join(parts)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RuleBank:
    """The bank at `path`. The file and its folder are made on first use."""

    def __init__(
        self, path: Path, *, not_the_database: Path | None = None, clock: Callable[[], str] = _now
    ) -> None:
        """`not_the_database` is the transaction database: a bank at that path is refused."""
        if not_the_database is not None and path.resolve() == not_the_database.resolve():
            raise BankError(
                "unavailable", "The rule bank cannot be the transaction database: that file is read-only."
            )  # fmt: skip
        self.path = path
        self._clock = clock

    # --- runs ------------------------------------------------------------------------------

    def start_run(
        self,
        *,
        kind: RunKind,
        source: RunSource,
        batch_size: int,
        request_bound: int,
        model_id: str,
        scenario_prompt_version: str,
        rule_prompt_version: str,
    ) -> int:
        with self._write() as connection:
            cursor = connection.execute(
                "INSERT INTO runs (status, kind, source, batch_size, request_bound, model_id, "
                "scenario_prompt_version, rule_prompt_version, started_at) "
                "VALUES ('running', ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, source, batch_size, request_bound, model_id, scenario_prompt_version,
                 rule_prompt_version, self._clock()),
            )  # fmt: skip
            return _row_id(cursor)

    def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        error: RunError | None,
        scenarios: int,
        model_requests: int,
        input_tokens: int,
        output_tokens: int,
        elapsed_ms: int,
    ) -> None:
        with self._write() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, error_kind = ?, error_message = ?, scenarios = ?, "
                "model_requests = ?, input_tokens = ?, output_tokens = ?, elapsed_ms = ?, "
                "finished_at = ? WHERE id = ?",
                (status, None if error is None else error.kind,
                 None if error is None else error.message, scenarios, model_requests,
                 input_tokens, output_tokens, elapsed_ms, self._clock(), run_id),
            )  # fmt: skip

    def interrupt_running(self) -> int:
        """Mark every run still recorded as running as interrupted; return how many.

        For a process that starts: a run it finds running was left by one
        that ended first. A bank that does not exist yet is left absent.
        """
        if not self.path.is_file():
            return 0
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = 'interrupted', error_kind = 'interrupted', "
                "error_message = ?, finished_at = ? WHERE status = 'running'",
                ("The process that ran it ended before the run did.", self._clock()),
            )
            return cursor.rowcount

    def run(self, run_id: int) -> FinderRun | None:
        with self._read() as connection:
            return self._run(connection, "WHERE id = ?", (run_id,))

    def latest_run(self) -> FinderRun | None:
        if not self.path.is_file():
            return None
        with self._read() as connection:
            return self._run(connection, "ORDER BY id DESC LIMIT 1", ())

    def _run(
        self, connection: sqlite3.Connection, where: str, values: tuple[int, ...]
    ) -> FinderRun | None:
        row = connection.execute(
            "SELECT id, status, error_kind, error_message, source, batch_size, scenarios, "
            "model_requests, request_bound, input_tokens, output_tokens, elapsed_ms, model_id, "
            f"scenario_prompt_version, rule_prompt_version, started_at, finished_at, kind "
            f"FROM runs {where}",
            values,
        ).fetchone()
        if row is None:
            return None
        run_id = row[0]
        added = [
            RunItem(
                position=position,
                title=_body(body).title,
                result="added" if processed == run_id else "proposed",
                entry_id=entry_id,
                status=status,
            )
            for entry_id, position, status, body, processed in connection.execute(
                "SELECT id, position, status, body, json_extract(body, '$.processed_run_id') "
                "FROM entries WHERE run_id = ?1 OR json_extract(body, '$.processed_run_id') = ?1",
                (run_id,),
            )
        ]
        matched = [
            RunItem(
                position=position, title=title, result="duplicate", entry_id=entry_id,
                status=status, match_kind=kind,
            )
            for position, title, entry_id, kind, status in connection.execute(
                "SELECT m.position, m.title, m.entry_id, m.kind, e.status FROM matches m "
                "JOIN entries e ON e.id = m.entry_id WHERE m.run_id = ?",
                (run_id,),
            )
        ]  # fmt: skip
        return _validated(
            FinderRun,
            {
                "id": run_id,
                "kind": row[17],
                "status": row[1],
                "error": None if row[2] is None else {"kind": row[2], "message": row[3] or ""},
                "source": row[4],
                "batch_size": row[5],
                "scenarios": row[6],
                "items": sorted(added + matched, key=lambda item: item.position),
                "model_requests": row[7],
                "request_bound": row[8],
                "input_tokens": row[9],
                "output_tokens": row[10],
                "elapsed_ms": row[11],
                "model_id": row[12],
                "scenario_prompt_version": row[13],
                "rule_prompt_version": row[14],
                "started_at": row[15],
                "finished_at": row[16],
            },
        )

    # --- entries ---------------------------------------------------------------------------

    def find_clause(self, clause: str) -> int | None:
        """The entry that holds this clause, spacing and case of keywords aside."""
        with self._read() as connection:
            row = connection.execute(
                "SELECT id FROM entries WHERE clause_key = ?", (clause_key(clause),)
            ).fetchone()
        return None if row is None else int(row[0])

    def candidates(self, flagged: int) -> tuple[tuple[int, str], ...]:
        """The entries whose rule flags `flagged` transactions, oldest first: id and clause.

        Two rules that flag the same transactions flag the same number of
        them, so these are the only entries a new rule can duplicate.
        """
        with self._read() as connection:
            rows = connection.execute(
                "SELECT id, body FROM entries WHERE clause_key IS NOT NULL AND flagged = ? "
                "ORDER BY id",
                (flagged,),
            ).fetchall()
        found: list[tuple[int, str]] = []
        for entry_id, body in rows:
            clause = _body(body).clause
            if clause is not None:
                found.append((int(entry_id), clause))
        return tuple(found)

    def add_entry(self, entry: NewEntry) -> int:
        """Add `entry` and return its id. Raises `DuplicateClause` when its clause is in the bank."""
        key = None if entry.clause is None else clause_key(entry.clause)
        flagged = None if entry.rule is None else entry.rule.flagged
        with self._write() as connection:
            if key is not None:
                row = connection.execute(
                    "SELECT id FROM entries WHERE clause_key = ?", (key,)
                ).fetchone()
                if row is not None:
                    raise DuplicateClause(int(row[0]))
            cursor = connection.execute(
                "INSERT INTO entries (run_id, position, status, clause_key, flagged, created_at, "
                "body) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry.run_id, entry.position, entry.status, key, flagged, self._clock(),
                 entry.model_dump_json()),
            )  # fmt: skip
            return _row_id(cursor)

    def add_match(
        self,
        entry_id: int,
        *,
        run_id: int,
        position: int,
        kind: MatchKind,
        title: str,
        hypothesis: str,
        clause: str,
    ) -> None:
        """Record that a scenario's rule is entry `entry_id` again."""
        with self._write() as connection:
            connection.execute(
                "INSERT INTO matches (entry_id, run_id, position, kind, title, hypothesis, clause, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry_id, run_id, position, kind, title, hypothesis, clause, self._clock()),
            )

    def proposed(self) -> tuple[ProposedScenario, ...]:
        """The scenarios waiting to be processed, oldest first."""
        if not self.path.is_file():
            return ()
        with self._read() as connection:
            rows = connection.execute(
                "SELECT id, created_at, body FROM entries WHERE status = 'proposed' ORDER BY id"
            ).fetchall()
        found: list[ProposedScenario] = []
        for entry_id, created_at, body in rows:
            entry = _body(body)
            found.append(
                ProposedScenario(
                    id=entry_id, run_id=entry.run_id, title=entry.title,
                    hypothesis=entry.hypothesis, columns=entry.columns,
                    assumptions=entry.scenario_assumptions, created_at=created_at,
                )
            )  # fmt: skip
        return tuple(found)

    def count(self, status: EvidenceStatus) -> int:
        if not self.path.is_file():
            return 0
        with self._read() as connection:
            row = connection.execute(
                "SELECT count(*) FROM entries WHERE status = ?", (status,)
            ).fetchone()
        return int(row[0])

    def earlier_scenarios(self, limit: int) -> tuple[EarlierScenario, ...]:
        """The `limit` scenarios written most recently, newest first, whatever became of them.

        Entries of every status and the scenarios recorded as matches. Their
        title and hypothesis only: nothing that was measured.
        """
        if not self.path.is_file():
            return ()
        with self._read() as connection:
            rows = connection.execute(
                "SELECT created_at, id, json_extract(body, '$.title'), "
                "json_extract(body, '$.hypothesis') FROM entries "
                "UNION ALL SELECT created_at, id, title, hypothesis FROM matches "
                "ORDER BY 1 DESC, 2 DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(EarlierScenario(title=str(row[2]), hypothesis=str(row[3])) for row in rows)

    def discard(self, entry_id: int) -> BankEntry:
        """Set a proposed scenario aside. It stays in the bank, so it is not proposed again."""
        if not self.path.is_file():
            raise BankError("unknown_entry", f"The rule bank has no entry {entry_id}.")
        with self._write() as connection:
            current = self._entry(connection, entry_id)
            if current.status != "proposed":
                raise BankError(
                    "not_processable", f"Entry {entry_id} is not a scenario waiting to be processed."
                )
            self._store(connection, entry_id, _as_new(current, status="discarded"))
            return self._entry(connection, entry_id)

    def complete(self, entry_id: int, entry: NewEntry) -> None:
        """Replace a proposed scenario by what processing it gave.

        Raises `DuplicateClause` when its clause is another entry's.
        """
        key = None if entry.clause is None else clause_key(entry.clause)
        with self._write() as connection:
            if key is not None:
                row = connection.execute(
                    "SELECT id FROM entries WHERE clause_key = ? AND id <> ?", (key, entry_id)
                ).fetchone()
                if row is not None:
                    raise DuplicateClause(int(row[0]))
            self._store(connection, entry_id, entry)

    def complete_as_match(
        self, entry_id: int, target: int, *, run_id: int, kind: MatchKind, clause: str
    ) -> None:
        """The proposed scenario's rule is entry `target` again: record the match, drop the scenario."""
        with self._write() as connection:
            scenario = self._entry(connection, entry_id)
            connection.execute(
                "INSERT INTO matches (entry_id, run_id, position, kind, title, hypothesis, clause, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (target, run_id, scenario.position, kind, scenario.title, scenario.hypothesis,
                 clause, self._clock()),
            )  # fmt: skip
            connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

    def _store(self, connection: sqlite3.Connection, entry_id: int, entry: NewEntry) -> None:
        connection.execute(
            "UPDATE entries SET status = ?, clause_key = ?, flagged = ?, body = ? WHERE id = ?",
            (entry.status, None if entry.clause is None else clause_key(entry.clause),
             None if entry.rule is None else entry.rule.flagged, entry.model_dump_json(), entry_id),
        )  # fmt: skip

    def entries(self) -> tuple[EntrySummary, ...]:
        """Every processed entry, ranked: by status, then by lift and fraud caught in discovery."""
        if not self.path.is_file():
            return ()
        with self._read() as connection:
            rows = connection.execute(
                "SELECT e.id, e.decision, e.created_at, e.body, "
                "(SELECT count(*) FROM matches m WHERE m.entry_id = e.id) FROM entries e "
                "WHERE e.status NOT IN ('proposed', 'discarded')"
            ).fetchall()
        summaries: list[EntrySummary] = []
        for entry_id, decision, created_at, body, matches in rows:
            entry = _body(body)
            summaries.append(
                _validated(
                    EntrySummary,
                    {
                        "id": entry_id,
                        "run_id": entry.run_id,
                        "title": entry.title,
                        "status": entry.status,
                        "decision": decision,
                        "turn_outcome": entry.turn_outcome,
                        "discovery": None if entry.rule is None else entry.rule.earlier,
                        "confirmation": None if entry.rule is None else entry.rule.later,
                        "verdict_short": verdict(entry)[1],
                        "matches": matches,
                        "created_at": created_at,
                    },
                )
            )
        return tuple(sorted(summaries, key=_rank))

    def entry(self, entry_id: int) -> BankEntry:
        """One entry in full. Raises `BankError` of kind `unknown_entry`."""
        if not self.path.is_file():
            raise BankError("unknown_entry", f"The rule bank has no entry {entry_id}.")
        with self._read() as connection:
            return self._entry(connection, entry_id)

    def delete(self, entry_id: int) -> None:
        """Remove an entry and everything attached to it, for good.

        The scenario, the rule, its evidence, the decision and note, and the
        matches recorded against it. The runs it came from stay, as history
        of what was asked of the model. A deleted scenario is forgotten: it
        is no longer in `earlier_scenarios`, and no duplicate check sees it.
        """
        if not self.path.is_file():
            raise BankError("unknown_entry", f"The rule bank has no entry {entry_id}.")
        with self._write() as connection:
            self._entry(connection, entry_id)
            connection.execute("DELETE FROM matches WHERE entry_id = ?", (entry_id,))
            connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

    def decide(self, entry_id: int, decision: Decision, note: str | None) -> BankEntry:
        """Set the reviewer's decision on a rule. An entry without a rule cannot be reviewed."""
        text = (note or "").strip()[:MAX_NOTE_CHARACTERS] or None
        if not self.path.is_file():
            raise BankError("unknown_entry", f"The rule bank has no entry {entry_id}.")
        with self._write() as connection:
            current = self._entry(connection, entry_id)
            if current.clause is None:
                raise BankError(
                    "not_reviewable",
                    f"Entry {entry_id} holds no rule, so there is nothing to approve or reject.",
                )
            connection.execute(
                "UPDATE entries SET decision = ?, decision_note = ?, decided_at = ? WHERE id = ?",
                (decision, text, None if decision == "pending" else self._clock(), entry_id),
            )
            return self._entry(connection, entry_id)

    def _entry(self, connection: sqlite3.Connection, entry_id: int) -> BankEntry:
        row = connection.execute(
            "SELECT decision, decision_note, decided_at, created_at, body FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise BankError("unknown_entry", f"The rule bank has no entry {entry_id}.")
        stored = _body(row[4])
        matches = [
            {
                "run_id": run_id, "position": position, "title": title, "hypothesis": hypothesis,
                "clause": clause, "kind": kind, "created_at": created_at,
            }
            for run_id, position, title, hypothesis, clause, kind, created_at in connection.execute(
                "SELECT run_id, position, title, hypothesis, clause, kind, created_at FROM matches "
                "WHERE entry_id = ? ORDER BY id",
                (entry_id,),
            )
        ]  # fmt: skip
        statements = None if stored.clause is None else backtest_statements(stored.clause)
        return _validated(
            BankEntry,
            {
                **stored.model_dump(),
                "id": entry_id,
                "created_at": row[3],
                "decision": row[0],
                "decision_note": row[1],
                "decided_at": row[2],
                "matches": matches,
                "backtest_sql": None if statements is None else statements[0],
                "monthly_sql": None if statements is None else statements[1],
                "verdict": verdict(stored)[0],
                "verdict_short": verdict(stored)[1],
            },
        )

    # --- the file --------------------------------------------------------------------------

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._open(write=False) as connection:
            yield connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._open(write=True) as connection:
            yield connection

    @contextmanager
    def _open(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=_BUSY_SECONDS, isolation_level=None)
            self._prepare(connection)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except (sqlite3.Error, OSError) as error:
            # The class of the fault only: SQLite's sentence can name a path.
            raise BankError(
                "unavailable",
                f"The rule bank could not be read or written ({type(error).__name__}).",
            ) from None
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass  # closing the connection drops the transaction too
                connection.close()

    def _prepare(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if version == 0 and not tables:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version == 1 and _TABLES <= tables:
            # A bank written before runs had a kind: its runs were batches.
            connection.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'batch'")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION or not _TABLES <= tables:
            raise BankError(
                "not_a_bank",
                "The file at the rule bank's path is not a rule bank of this version; it was "
                "left as it is.",
            )


def _rank(summary: EntrySummary) -> tuple[int, float, int, int]:
    discovery = summary.discovery
    lift = 0.0 if discovery is None or discovery.lift is None else discovery.lift
    caught = 0 if discovery is None else discovery.true_positives
    return (STATUS_ORDER.index(summary.status), -lift, -caught, summary.id)


_NOT_BUILT: Final[dict[str, tuple[str, str]]] = {
    "clarification": (
        "the model asked what the scenario meant instead of writing a rule.",
        "model asked a question",
    ),
    "cannot_answer": ("the model declined to write a rule for this scenario.", "model declined"),
    "answered": ("the model answered with a query, not a rule.", "no rule written"),
    "rejected": (
        "the SQL guard refused the clause the model wrote, and the one it wrote after that.",
        "refused by the guard",
    ),
}


def verdict(entry: NewEntry) -> tuple[str, str]:
    """What became of a scenario: one plain sentence, and its short form for a table row.

    One wording per status and per cause, from the stored figures and the
    checks that failed. Arithmetic on nothing: every number is one the
    backtest returned.
    """
    if entry.status == "proposed":
        return "Proposed: waiting to be processed. No rule has been written and nothing has run.", "waiting"
    if entry.status == "discarded":
        return "Discarded by a reviewer. It was never processed.", "discarded"
    card = entry.rule
    if card is None:
        timed_out = entry.turn_error_kind == "timeout" or "interrupted after" in (entry.turn_message or "")
        if timed_out:
            return (
                "No rule built: the rule's query ran past the time limit on every attempt, so it "
                "could not be measured. That says the query was too slow, not that the idea is wrong.",
                "query too slow",
            )
        text, short = _NOT_BUILT.get(
            entry.turn_outcome or "", ("the turn that should have written it failed.", "turn failed")
        )
        return f"No rule built: {text}", short
    found, later = card.earlier, card.later
    if card.flagged == 0:
        return (
            f"No signal: this rule flagged 0 of {card.total_transactions:,} transactions, so there "
            "was nothing to measure.",
            "flagged nothing",
        )
    if card.flagged_labelled == 0:
        return (
            f"No signal: this rule flagged {card.flagged:,} of {card.total_transactions:,} "
            "transactions and none of them has a fraud label, so it cannot be measured.",
            "none labelled",
        )
    discovery = (
        f"{found.true_positives:,} fraud among {found.flagged_labelled:,} labelled flagged "
        f"transactions{_rate(found)}"
    )
    confirmation = (
        f"{later.true_positives:,} fraud among {later.flagged_labelled:,} labelled flagged"
        f"{_rate(later)}"
    )
    failed = next((check for check in entry.criteria if not check.passed), None)
    if entry.status == "confirmed" or failed is None:
        return (
            f"Confirmed: in the discovery period it flagged {found.flagged:,} transactions, with "
            f"{discovery}; in the confirmation period, {confirmation}.",
            f"lift {_times(found.lift)}, then {_times(later.lift)}",
        )
    period = "the discovery period" if failed.period == "discovery" else "the confirmation period"
    where = "on discovery" if failed.period == "discovery" else "on confirmation"
    figures = found if failed.period == "discovery" else later
    short = {
        "labelled": f"too few labelled {where}",
        "fraud_caught": f"{figures.true_positives:,} fraud caught {where}",
    }.get(failed.code, f"lift {_times(figures.lift)} {where}")
    why = {
        "labelled": f"too few of what it flagged in {period} has a fraud label to judge it",
        "fraud_caught": f"it caught too few frauds in {period}",
        "lift": f"in {period} it did not find fraud clearly more often than flagging at random would",
        "lift_kept": "it lost more than half of its lift in the confirmation period",
    }.get(failed.code, f"a check failed in {period}")
    if entry.status == "no_signal":
        return f"No signal: {why}. It flagged {found.flagged:,} transactions there, with {discovery}.", short
    return (
        f"Not confirmed: it stood out in the discovery period ({discovery}), but {why} "
        f"({confirmation}).",
        short,
    )


def _rate(period: RulePeriod) -> str:
    if period.precision is None:
        return ""
    return f" (precision {period.precision:.2%}, lift {_times(period.lift)})"


def _times(value: float | None) -> str:
    return "undefined" if value is None else f"{value:,.1f}"


def _as_new(entry: BankEntry, **changes: object) -> NewEntry:
    fields = entry.model_dump(include=set(NewEntry.model_fields))
    return _validated(NewEntry, {**fields, **changes})


def _row_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise sqlite3.DatabaseError("no row id")
    return cursor.lastrowid


def _body(text: str) -> NewEntry:
    try:
        data: object = json.loads(text)
    except ValueError:
        data = None
    return _validated(NewEntry, data)


def _validated[M: BaseModel](model: type[M], data: object) -> M:
    try:
        return model.model_validate(data)
    except ValidationError:
        raise BankError(
            "not_a_bank", "The rule bank holds a record this version cannot read."
        ) from None
