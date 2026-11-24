"""SQL guard.

Responsibility: decide, in code, whether one model-written string may run.
`check_query` judges a full statement; `check_rule_clause` judges a rule, a
WHERE clause over `transactions`. Neither runs anything and neither repairs
anything: the text is trimmed, one trailing `;` is dropped, and the result
is accepted or rejected as it stands, with a `reason` written to be handed
back to the model for one repair attempt and a `reason_code` for code that
routes, logs or counts rejections.

Method, in two steps.

1. Text checks, before anything is compiled, with the scanner in
   `sql_scan`: no Markdown code fence, valid Unicode, at most
   `MAX_STATEMENT_CHARACTERS`, quotes closed, and a bound on how much
   SQLite would have to compile (`_complexity`). The last one exists
   because compiling is not free: SQLite copies a common table expression
   at every reference to it, so a short statement whose CTEs each refer to
   the one before twice doubles in size per level, and the timeout in `db`
   does not cover compiling.
2. The statement is compiled, not run, by `db.inspect_query`, and the
   policies read SQLite's own account of it: the actions, the tables and
   columns read, the functions called. So a table is known because SQLite
   resolved it, through any alias, join, subquery or CTE, not because a name
   matched in the text. The arguments of date functions, which SQLite does
   not report, are read from the text.

Query policy:
- exactly one statement, which is a SELECT (not EXPLAIN, PRAGMA, VACUUM);
- every table read is one of the database's own tables, read from its
  catalogue; system objects (`sqlite_master`, `pragma_*`, `json_each`,
  `dbstat`) are not;
- no call to a function in `DENIED_FUNCTIONS`;
- no date function that reads today's date (`date('now')`, `date()`,
  `strftime('%Y')`, `datetime('subsec')`): the data is historical, so such
  a filter returns nothing and looks like an answer.

Rule policy: the clause must stay inside the backtest template (no `;`,
comment, bind parameter or parenthesis that closes the template's own), must
compile as one WHERE expression both bare and in parentheses, and must pass
the query policy; plus no read of `fraud_labels` (a rule scored against the
labels must not read them) and no function in `RULE_DENIED_FUNCTIONS`.
`rules.backtest.backtest` applies this policy itself before it measures.

Database faults (missing file, unreadable directory) are not decisions and
propagate as `db.DbError`. The row limit is not checked here: `db` caps the
rows it fetches (docs/DECISIONS.md, D-08).

Known limits. Today's date is recognised from literal text only: `'now'`
built at run time (`'no' || 'w'`, a value read from a column) and the blob
form (`date(x'6e6f77')`) are not seen. A rule that lists fraud transaction
ids as literals reads no label and cannot be told from an honest rule.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from fsm_assistant import db
from fsm_assistant.sql_scan import Token, UnterminatedQuote, scan

# Analyst SQL is a few hundred characters; this is some twenty times that.
MAX_STATEMENT_CHARACTERS = 10_000
# `name AS (...)` definitions (CTEs and named windows) in one text.
MAX_DEFINITIONS = 20
# Tokens SQLite would compile once every CTE reference is replaced by the
# CTE's body. Measured on the real database, the costliest shape found
# compiles in under 0.1 s at this size and in 0.2 s at twice it. A plain
# statement of the maximum length is at most 10,000 tokens, so only repeated
# CTE references reach the limit.
MAX_EXPANDED_TOKENS = 50_000

_INFORMATION_FUNCTIONS = frozenset(
    {"sqlite_version", "sqlite_source_id", "sqlite_compileoption_get", "sqlite_compileoption_used"}
)
_CLOCK_FUNCTIONS = frozenset({"current_date", "current_time", "current_timestamp"})
DENIED_FUNCTIONS = frozenset({"load_extension"}) | _CLOCK_FUNCTIONS | _INFORMATION_FUNCTIONS
# Functions whose value does not come from the transaction being judged.
RULE_DENIED_FUNCTIONS = frozenset(
    {"random", "randomblob", "changes", "total_changes", "last_insert_rowid"}
)
LABEL_TABLE = "fraud_labels"

# Date function -> the position of its time value, which is also the number
# of arguments at which the function means "now".
_TIME_VALUE_POSITION = {
    "date": 0,
    "time": 0,
    "datetime": 0,
    "julianday": 0,
    "unixepoch": 0,
    "strftime": 1,
}
_DATE_FUNCTIONS = frozenset(_TIME_VALUE_POSITION) | {"timediff"}
# As a time value these mean the current time, like 'now'; after a time value they are a modifier.
_SUBSECOND = frozenset({"subsec", "subsecond"})

_TODAY = (
    "The data is historical, so a filter relative to today matches nothing. "
    "Write explicit dates inside the range of transactions.date."
)

# The clause exactly as it would be written after WHERE. Compiled to reject
# text that is only valid inside the backtest template's parentheses, such as
# a bare `SELECT 1`, which there would become a scalar subquery. Its record
# shows what the clause alone reads: the template joins fraud_labels itself,
# so the full statement cannot show that.
_BARE_CLAUSE = "SELECT 1 FROM transactions WHERE {clause}\n"

# The clause as the backtest template places it. Compiled to reject text
# that is only valid without the parentheses, such as `1=1 LIMIT 1`.
_WRAPPED_CLAUSE = "SELECT 1 FROM transactions WHERE (\n{clause}\n)\n"

# Characters that start a bind parameter in SQLite. Nothing is ever bound to
# a clause, and a `$name(...)` parameter can hide parentheses from a scan.
_PARAMETER_CHARACTERS = frozenset("?:@$#")

_FENCE = "```"

# Limits for the catalogue query, which is ours, not model-written.
_CATALOGUE_ROW_CAP = 1000
_CATALOGUE_TIMEOUT_SECONDS = 60.0


class ReasonCode(StrEnum):
    """Why a statement or clause was rejected, for code; `reason` is the wording."""

    EMPTY = "empty"
    CODE_FENCE = "code_fence"  # wrapped in a Markdown fence; not unwrapped here
    INVALID_TEXT = "invalid_text"  # not encodable as UTF-8
    TOO_LONG = "too_long"
    TOO_COMPLEX = "too_complex"  # would cost too much to compile
    MULTIPLE_STATEMENTS = "multiple_statements"
    EXPLAIN = "explain"
    UNTERMINATED_QUOTE = "unterminated_quote"
    SQLITE_ERROR = "sqlite_error"  # did not compile: syntax, unknown table or column
    NOT_READ_ONLY = "not_read_only"
    NOT_SELECT = "not_select"
    TABLE_NOT_ALLOWED = "table_not_allowed"
    FUNCTION_NOT_ALLOWED = "function_not_allowed"
    RELATIVE_TO_TODAY = "relative_to_today"
    CLAUSE_NOT_CONTAINED = "clause_not_contained"  # rules: could escape the backtest template
    LABEL_READ = "label_read"  # rules
    NOT_DETERMINISTIC = "not_deterministic"  # rules


class GuardDecision(BaseModel):
    """Whether one statement or rule clause may run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    sql: str  # the text that was judged: trimmed, one trailing ';' removed
    reason_code: ReasonCode | None = None  # None exactly when accepted
    reason: str | None = None  # None exactly when accepted
    sqlite_message: str | None = None  # SQLite's own words, when it refused to compile the text
    reads: db.QueryInspection | None = None  # None when the text did not compile

    @model_validator(mode="after")
    def _rejections_carry_a_reason(self) -> Self:
        if self.accepted == (self.reason_code is None) == (self.reason is None):
            return self
        raise ValueError("a decision has a reason code and a reason exactly when it is rejected")


type _Rejection = tuple[ReasonCode, str]


def check_query(sql: str, *, db_path: Path) -> GuardDecision:
    """Decide whether `sql` may be run as a query. Never runs it."""
    text = normalise(sql)
    tokens, rejection = _read_text(text, "statement")
    rejection = rejection or _statement_shape(tokens)
    if rejection:
        return _decide(text, rejection)
    try:
        reads = db.inspect_query(text, db_path=db_path)
    except db.QueryError as error:
        return _decide(
            text,
            (ReasonCode.SQLITE_ERROR, f"SQLite rejected the statement: {error.sqlite_message}"),
            sqlite_message=error.sqlite_message,
        )
    except db.ReadOnlyViolation as error:
        return _decide(text, (ReasonCode.NOT_READ_ONLY, str(error)))
    return _decide(text, _policy(tokens, reads, db_path, rule=False), reads)


def check_rule_clause(clause: str, *, db_path: Path) -> GuardDecision:
    """Decide whether `clause` may be backtested as a rule. Never runs it.

    `reads` is what the clause alone reads, in the form
    `SELECT 1 FROM transactions WHERE <clause>`.
    """
    text = normalise(clause)
    tokens, rejection = _read_text(text, "clause")
    rejection = rejection or _containment(text, tokens)
    if rejection:
        return _decide(text, rejection)
    try:
        reads = db.inspect_query(_BARE_CLAUSE.format(clause=text), db_path=db_path)
        db.inspect_query(_WRAPPED_CLAUSE.format(clause=text), db_path=db_path)
    except db.QueryError as error:
        return _decide(
            text,
            (ReasonCode.SQLITE_ERROR, f"SQLite rejected the clause: {error.sqlite_message}"),
            sqlite_message=error.sqlite_message,
        )
    except db.ReadOnlyViolation as error:
        return _decide(
            text, (ReasonCode.NOT_READ_ONLY, f"The clause is not a read-only expression. {error}")
        )
    return _decide(
        text, _policy(tokens, reads, db_path, rule=True) or _rule_policy(reads), reads
    )


def normalise(sql: str) -> str:
    """Trim `sql` and remove one trailing `;`. Nothing else is changed."""
    text = sql.strip()
    return text[:-1].rstrip() if text.endswith(";") else text


def _decide(
    text: str,
    rejection: _Rejection | None,
    reads: db.QueryInspection | None = None,
    sqlite_message: str | None = None,
) -> GuardDecision:
    code, reason = rejection or (None, None)
    return GuardDecision(
        accepted=rejection is None,
        sql=text,
        reason_code=code,
        reason=reason,
        sqlite_message=sqlite_message,
        reads=reads,
    )


def _read_text(text: str, subject: str) -> tuple[tuple[Token, ...], _Rejection | None]:
    """Scan `text` into tokens and make the checks that need no database.

    These run before anything is compiled, cheapest first, so that the cost
    of judging a text is bounded by its length. `subject` names the text in
    the reason ("statement" or "clause").
    """
    if len(text) > MAX_STATEMENT_CHARACTERS:
        return (), (
            ReasonCode.TOO_LONG,
            f"The {subject} is {len(text):,} characters long; the limit is "
            f"{MAX_STATEMENT_CHARACTERS:,}. Write a shorter {subject}.",
        )
    if text.startswith(_FENCE) or text.endswith(_FENCE):
        return (), (
            ReasonCode.CODE_FENCE,
            f"The {subject} is wrapped in a Markdown code fence ({_FENCE}). Return the bare "
            f"{subject} only, with no fence and no other text around it.",
        )
    try:
        text.encode("utf-8")
    except UnicodeError:
        return (), (
            ReasonCode.INVALID_TEXT,
            f"The {subject} contains a character that is not valid Unicode text "
            "(an unpaired surrogate). Remove it.",
        )
    try:
        tokens = tuple(scan(text))
    except UnterminatedQuote as error:
        return (), (
            ReasonCode.UNTERMINATED_QUOTE,
            f"The {subject} has an unterminated {error.quote} quote.",
        )
    return tokens, _complexity(tokens, subject)


def _statement_shape(tokens: Sequence[Token]) -> _Rejection | None:
    """Reject text that is not one statement, or is an EXPLAIN, before compiling it."""
    first: Token | None = None
    for token in tokens:
        if token.kind == "comment":
            continue
        if token.kind == "symbol" and token.text == ";":
            return (
                ReasonCode.MULTIPLE_STATEMENTS,
                "Only one statement is allowed: the SQL contains ';' before its end. "
                "Send a single SELECT.",
            )
        first = first or token
    if first is None:
        return ReasonCode.EMPTY, "The statement is empty."
    if first.kind == "word" and first.text.lower() == "explain":
        return (
            ReasonCode.EXPLAIN,
            "EXPLAIN is not allowed: send the SELECT itself, not its query plan.",
        )
    return None


def _containment(text: str, tokens: Sequence[Token]) -> _Rejection | None:
    """Reject a clause that could change what the backtest template measures.

    The clause is untrusted text placed inside the template's parentheses.
    Outside quoted text it must hold no statement separator, comment or
    bind parameter, and no parenthesis that would close the template's own.
    """
    if not text:
        return ReasonCode.EMPTY, "The clause is empty."

    def escapes(reason: str) -> _Rejection:
        return ReasonCode.CLAUSE_NOT_CONTAINED, reason

    if "\x00" in text:
        return escapes("The clause contains a NUL character.")
    depth = 0
    for token in tokens:
        if token.kind == "comment":
            return escapes(
                "The clause contains a SQL comment ('--' or '/*'), which could hide the rest "
                "of the query. Write a space between two minus signs."
            )
        if token.kind != "symbol":
            continue
        character = token.text
        if character == ";":
            return escapes("The clause contains ';': it must be one expression, not a statement.")
        if character in _PARAMETER_CHARACTERS:
            return escapes(f"The clause contains {character!r}: bind parameters are not allowed.")
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return escapes("The clause has a ')' with no matching '('.")
    if depth:
        return escapes("The clause has a '(' with no matching ')'.")
    return None


class _Definition(BaseModel):
    """One `name AS ( ... )` in the text: a CTE, or a named window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str  # lowercased
    site: int  # index of the name's token
    start: int  # index of the body's '('
    end: int  # index of the matching ')'


def _complexity(tokens: Sequence[Token], subject: str) -> _Rejection | None:
    """Reject text whose CTE references would make SQLite compile too much.

    An estimate from the text, made to err on the high side: every word,
    quoted name or string equal to a defined name counts as a reference to
    it, whatever its scope.
    """
    significant = [token for token in tokens if token.kind != "comment"]
    definitions = _definitions(significant)
    if len(definitions) > MAX_DEFINITIONS:
        return (
            ReasonCode.TOO_COMPLEX,
            f"The {subject} has {len(definitions)} definitions of the form `name AS (...)` "
            f"(common table expressions and named windows); the limit is {MAX_DEFINITIONS}. "
            "Use fewer WITH steps.",
        )
    if _expanded_size(significant, definitions) > MAX_EXPANDED_TOKENS:
        return (
            ReasonCode.TOO_COMPLEX,
            f"The {subject} is too complex to check: its common table expressions refer to "
            "each other so many times that SQLite would compile more than "
            f"{MAX_EXPANDED_TOKENS:,} tokens. Refer to each WITH name fewer times, or do the "
            "work in fewer WITH steps.",
        )
    return None


def _definitions(tokens: Sequence[Token]) -> list[_Definition]:
    """Every `name AS (`, `name (columns) AS (` and `name AS [NOT] MATERIALIZED (`."""
    open_of: dict[int, int] = {}
    close_of: dict[int, int] = {}
    unclosed: list[int] = []
    for index, token in enumerate(tokens):
        if token.kind != "symbol":
            continue
        if token.text == "(":
            unclosed.append(index)
        elif token.text == ")" and unclosed:
            start = unclosed.pop()
            open_of[index], close_of[start] = start, index

    found: list[_Definition] = []
    for index, token in enumerate(tokens):
        if token.kind != "word" or token.text.lower() != "as":
            continue
        start = index + 1
        while start < len(tokens) and _is_word(tokens[start], "not", "materialized"):
            start += 1
        if start == len(tokens) or tokens[start].kind != "symbol" or tokens[start].text != "(":
            continue
        site = open_of.get(index - 1, index) - 1  # steps back over a column list
        if site < 0 or tokens[site].kind == "symbol":
            continue
        found.append(
            _Definition(
                name=tokens[site].text.lower(),
                site=site,
                start=start,
                end=close_of.get(start, len(tokens) - 1),
            )
        )
    return found


def _is_word(token: Token, *words: str) -> bool:
    return token.kind == "word" and token.text.lower() in words


def _expanded_size(tokens: Sequence[Token], definitions: Sequence[_Definition]) -> int:
    """Upper bound on the tokens SQLite compiles when each reference is a copy of its body.

    Stops early, with a value over `MAX_EXPANDED_TOKENS`, once any body is
    larger than that.
    """
    if not definitions:
        return len(tokens)
    names = {definition.name for definition in definitions}
    sites = {definition.site for definition in definitions}
    # References by the innermost body that holds them (None: outside every
    # body). A body nested in another is paid for where it is referred to.
    used: dict[_Definition | None, Counter[str]] = {None: Counter()}
    for definition in definitions:
        used[definition] = Counter()
    for index, token in enumerate(tokens):
        name = token.text.lower()
        if token.kind == "symbol" or index in sites or name not in names:
            continue
        holders = [d for d in definitions if d.start <= index <= d.end]
        holder = max(holders, key=lambda d: d.start, default=None)
        if holder is None or holder.name != name:  # a recursive CTE names itself
            used[holder][name] += 1
    bodies = [
        (definition.name, definition.end - definition.start + 1, used[definition])
        for definition in definitions
    ]

    # size(body) = its own tokens + the size of each body it refers to. A
    # reference chain is no longer than the number of definitions, so that
    # many passes reach every chain, and a cycle (names reused across
    # scopes) cannot loop.
    size = dict.fromkeys(names, 0)
    for _ in range(len(definitions) + 1):
        grown = dict.fromkeys(names, 0)
        for name, own, referred in bodies:
            total = own + sum(count * size[other] for other, count in referred.items())
            grown[name] = max(grown[name], total)
        if grown == size:
            break
        size = grown
        if max(size.values()) > MAX_EXPANDED_TOKENS:
            return MAX_EXPANDED_TOKENS + 1
    return len(tokens) + sum(count * size[name] for name, count in used[None].items())


def _policy(
    tokens: Sequence[Token], reads: db.QueryInspection, db_path: Path, *, rule: bool
) -> _Rejection | None:
    """The checks shared by queries and rules. Returns the first reason to reject."""
    if not reads.actions:
        return (
            ReasonCode.NOT_SELECT,
            "Only SELECT statements are allowed: SQLite reported nothing this statement "
            "reads (VACUUM is one such statement).",
        )
    if reads.actions[0] != db.SELECT_ACTION:
        return (
            ReasonCode.NOT_SELECT,
            f"Only SELECT statements are allowed, not {reads.first_action}.",
        )

    allowed = _catalogue_tables(db_path)
    for read in reads.reads:
        if read.table in allowed:
            continue
        # A name with no column read may be a CTE (see db.QueryInspection);
        # it is one exactly when nothing real answers to that name.
        if not read.columns and not _exists(read.table, db_path):
            continue
        readable = allowed - {LABEL_TABLE} if rule else allowed
        return (
            ReasonCode.TABLE_NOT_ALLOWED,
            f"{read.table} is not a table that may be read. "
            f"The tables are: {', '.join(sorted(readable))}.",
        )

    denied = sorted(reads.functions & DENIED_FUNCTIONS)
    if denied:
        if denied[0] in _CLOCK_FUNCTIONS:
            return (
                ReasonCode.RELATIVE_TO_TODAY,
                f"The function {denied[0]} is not allowed. {_TODAY}",
            )
        return ReasonCode.FUNCTION_NOT_ALLOWED, f"The function {denied[0]} is not allowed."

    if reads.functions & _DATE_FUNCTIONS:
        return _reads_today(tokens)
    return None


def _rule_policy(reads: db.QueryInspection) -> _Rejection | None:
    if LABEL_TABLE in reads.tables:
        return (
            ReasonCode.LABEL_READ,
            f"A rule must not read {LABEL_TABLE}: it is scored against those labels. "
            "Use only what is known about a transaction when it happens.",
        )
    denied = sorted(reads.functions & RULE_DENIED_FUNCTIONS)
    if denied:
        return (
            ReasonCode.NOT_DETERMINISTIC,
            f"The function {denied[0]} is not allowed in a rule: a rule must depend only on "
            "the data and flag the same transactions every time.",
        )
    return None


@dataclass(slots=True)
class _OpenCall:
    """An unclosed '(' while scanning, with the word before it.

    Working state of `_reads_today`, updated as tokens are read; it never
    leaves this module, so it is a plain mutable class and not a model.
    """

    name: str | None
    has_arguments: bool = False
    commas: int = 0


def _reads_today(tokens: Sequence[Token]) -> _Rejection | None:
    """Reject a date function that is given the current time, or no time value.

    Called only when the statement uses a date function. The authorizer
    reports function names but not arguments, so this reads the text. A
    `'now'` that is not inside a date function's parentheses is ordinary
    text and is left alone.
    """
    calls: list[_OpenCall] = []
    previous: Token | None = None
    for token in tokens:
        if token.kind == "comment":
            continue
        if token.kind == "symbol" and token.text == ")":
            if calls:
                call = calls.pop()
                count = call.commas + 1 if call.has_arguments else 0
                if call.name is not None and _TIME_VALUE_POSITION.get(call.name) == count:
                    return (
                        ReasonCode.RELATIVE_TO_TODAY,
                        f"{call.name}() is called without a time value, which means the "
                        f"current time. {_TODAY}",
                    )
        else:
            if calls:
                calls[-1].has_arguments = True
            if token.kind == "string":
                value = token.text.lower()
                if value == "now" and any(call.name in _DATE_FUNCTIONS for call in calls):
                    return (
                        ReasonCode.RELATIVE_TO_TODAY,
                        f"The SQL passes 'now' to a date function. {_TODAY}",
                    )
                if value in _SUBSECOND and calls and _is_time_value(calls[-1]):
                    return (
                        ReasonCode.RELATIVE_TO_TODAY,
                        f"The SQL passes '{token.text}' to {calls[-1].name}() as its time "
                        f"value, which means the current time. {_TODAY}",
                    )
            if token.kind == "symbol" and token.text == "(":
                named = previous is not None and previous.kind in ("word", "name")
                calls.append(_OpenCall(previous.text.lower() if previous and named else None))
            elif token.kind == "symbol" and token.text == "," and calls:
                calls[-1].commas += 1
        previous = token
    return None


def _is_time_value(call: _OpenCall) -> bool:
    """Whether the argument now being read is where `call` takes its time value."""
    if call.name == "timediff":
        return True
    return call.name is not None and _TIME_VALUE_POSITION.get(call.name) == call.commas


@cache
def _catalogue_tables(db_path: Path) -> frozenset[str]:
    """The database's own tables, lowercased. Cached: the database does not change."""
    result = db.run_query(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        db_path=db_path,
        row_cap=_CATALOGUE_ROW_CAP,
        timeout_seconds=_CATALOGUE_TIMEOUT_SECONDS,
    )
    return frozenset(str(row[0]).lower() for row in result.rows)


def _exists(name: str, db_path: Path) -> bool:
    """Whether `name` is something the database can read from, outside any CTE."""
    quoted = '"' + name.replace('"', '""') + '"'
    try:
        db.inspect_query(f"SELECT 1 FROM {quoted}", db_path=db_path)
    except db.QueryError as error:
        return not error.sqlite_message.startswith("no such table")
    return True
