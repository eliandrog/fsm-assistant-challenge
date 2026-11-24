"""Fraud-concentration scan.

Responsibility: answer "where is labelled fraud concentrated?" with SQL that
code writes (docs/DECISIONS.md, D-29). `scan` profiles the labelled
transactions along a fixed list of dimensions and returns a ranked list of
segments; `headline`, `notes` and `follow_ups` say the result in words. The
model's only part is to ask for it.

- One profile statement groups the labelled transactions by every dimension
  and returns the segments that pass the support floor, one row per
  dimension for those that do not, and the totals.
- A segment is listed when it has at least `MIN_LABELLED` labelled
  transactions and `MIN_FRAUD` frauds, and a fraud rate above the base rate.
- Segments are ranked by the share of all labelled fraud they hold, then by
  lift. Two listed segments of different dimensions that hold the same
  labelled transactions are listed once, under the more specific dimension.
- The residual check removes the top segment and profiles what is left, so
  that side effects of that segment are not read as findings of their own.

Every statement is checked by `sql_guard.check_query` and run by
`db.run_query`. A result holds aggregates only: counts and ratios per
segment, and the segment's name, which is a value of the grouped columns.
It is cached per database path and period for the life of the process.
"""

import time
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from fsm_assistant import db
from fsm_assistant.schema_context import label_join
from fsm_assistant.sql_guard import check_query

# The support floor: a segment with fewer labelled transactions or fewer
# frauds than these is counted, not listed.
MIN_LABELLED = 100
MIN_FRAUD = 10
# Segments shown when the caller does not say how many.
DEFAULT_TOP = 10

# Amount bands, in cents: each band runs from the previous edge up to, not
# including, its own. A negative amount is a band of its own.
AMOUNT_BANDS: tuple[tuple[int, str], ...] = (
    (0, "negative"),
    (1_000, "$0 to under $10"),
    (5_000, "$10 to under $50"),
    (10_000, "$50 to under $100"),
    (50_000, "$100 to under $500"),
    (100_000, "$500 to under $1,000"),
)
TOP_AMOUNT_BAND = "$1,000 and over"
NO_ERROR = "No error"

DimensionName = Literal[
    "transaction_type",
    "merchant_state",
    "merchant_city",
    "merchant_category",
    "hour",
    "error",
    "amount_band",
    "card_type",
    "card_brand",
]
# A value a segment is grouped by.
type Key = str | int | None


class ScanError(Exception):
    """The scan could not produce a result it can stand behind. The message holds no data."""


class Period(BaseModel):
    """The days a scan covers, both included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start > self.end:
            raise ValueError("the period ends before it starts")
        if self.end == date.max:
            raise ValueError("the period must end before 9999-12-31")
        return self


class Dimension(BaseModel):
    """One way of grouping the labelled transactions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: DimensionName
    title: str
    # One or two SQL expressions over the tables of `_FROM`; a segment is one combination.
    keys: tuple[str, ...]
    label: str = "{0}"  # how a segment's keys are written as its name
    detail: int = 1  # of two segments holding the same transactions, the higher is listed


def _amount_band() -> str:
    cases = "".join(
        f"WHEN t.amount_usd_cents < {edge} THEN '{label}'\n      "
        for edge, label in AMOUNT_BANDS
    )
    return f"CASE {cases}ELSE '{TOP_AMOUNT_BAND}' END"


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(name="transaction_type", title="transaction type", keys=("t.transaction_type",)),
    Dimension(name="merchant_state", title="merchant state or country", keys=("l.state",)),
    Dimension(
        name="merchant_city",
        title="merchant city and state",
        keys=("l.city", "l.state"),
        label="{0}, {1}",
        detail=2,
    ),
    Dimension(
        name="merchant_category",
        title="merchant category (MCC)",
        keys=("m.mcc", "c.description"),
        label="{0} {1}",
    ),
    Dimension(
        name="hour", title="hour of day", keys=("CAST(strftime('%H', t.date) AS INTEGER)",)
    ),
    Dimension(name="error", title="error", keys=(f"COALESCE(t.errors, '{NO_ERROR}')",)),
    Dimension(name="amount_band", title="amount band", keys=(_amount_band(),)),
    Dimension(name="card_type", title="card type", keys=("k.card_type",)),
    Dimension(name="card_brand", title="card brand", keys=("k.card_brand",)),
)
_BY_NAME = {dimension.name: dimension for dimension in DIMENSIONS}
_ORDER = {dimension.name: index for index, dimension in enumerate(DIMENSIONS)}

# Left joins, so a labelled transaction whose location, merchant, category or
# card is missing is still counted, in a segment with no name.
_FROM = f"""\
  FROM transactions t
  JOIN fraud_labels f ON {label_join("f", "t")}
  LEFT JOIN merchant_locations l ON l.id = t.merchant_location_id
  LEFT JOIN merchants m ON m.id = t.merchant_id
  LEFT JOIN mcc_codes c ON c.mcc = m.mcc
  LEFT JOIN cards k ON k.id = t.card_id"""
_FLOOR = f"labelled >= {MIN_LABELLED} AND fraud >= {MIN_FRAUD}"


class SegmentRow(BaseModel):
    """One listed segment. Counts are of labelled transactions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: DimensionName
    title: str
    segment: str
    keys: tuple[Key, ...]
    labelled: int
    fraud: int
    fraud_rate: float  # fraud / labelled
    lift: float  # the fraud rate over the base rate of the profile it belongs to
    share_of_fraud: float  # of all labelled fraud in the period
    # Segments of other dimensions that hold exactly the same labelled transactions.
    same_as: tuple[str, ...] = ()

    @property
    def description(self) -> str:
        return f"{self.title} = {self.segment}"


class Residual(BaseModel):
    """What is left once the top segment is taken out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    excluded: str
    labelled: int
    fraud: int
    # False when fewer than `MIN_FRAUD` frauds are left: no segment can pass
    # the floor, so the profile was not run again.
    profiled: bool
    rows: tuple[SegmentRow, ...]  # lift is over what is left; share is of all labelled fraud


class ScanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    period: Period | None  # None: all the data
    total_labelled: int
    total_fraud: int
    base_rate: float | None  # None when nothing is labelled
    rows: tuple[SegmentRow, ...]  # every listed segment, best first
    segments: int  # every segment of every dimension, listed or not
    below_floor: int
    below_floor_with_fraud: int
    not_above_base_rate: int  # passed the floor, with a fraud rate at or under the base rate
    residual: Residual | None  # None when no segment is listed
    statements: tuple[str, ...]  # every statement that ran, the profile first
    elapsed_seconds: float


_CACHE: dict[tuple[Path, Period | None], ScanResult] = {}


def clear_cache() -> None:
    _CACHE.clear()


def scan(
    *,
    db_path: Path,
    row_cap: int,
    timeout_seconds: float,
    max_result_bytes: int = db.DEFAULT_MAX_RESULT_BYTES,
    period: Period | None = None,
) -> ScanResult:
    """Profile the labelled transactions of `period` (all of them by default).

    Raises `ScanError` when a statement is refused by the guard or cut at
    the row cap, and lets the errors of `db` through.
    """
    cached = _CACHE.get((db_path, period))
    if cached is not None:
        return cached
    started = time.monotonic()
    runner = _Runner(db_path, row_cap, timeout_seconds, max_result_bytes)
    overall = _profile(runner, period, exclude=None, all_fraud=None)
    residual: Residual | None = None
    if overall.rows:
        top = overall.rows[0]
        left_labelled, left_fraud = overall.labelled - top.labelled, overall.fraud - top.fraud
        rows: tuple[SegmentRow, ...] = ()
        profiled = left_fraud >= MIN_FRAUD
        if profiled:
            rest = _profile(runner, period, exclude=top, all_fraud=overall.fraud)
            if (rest.labelled, rest.fraud) != (left_labelled, left_fraud):
                raise ScanError("the residual profile does not add up with the top segment")
            rows = rest.rows
        residual = Residual(
            excluded=top.description,
            labelled=left_labelled,
            fraud=left_fraud,
            profiled=profiled,
            rows=rows,
        )
    result = ScanResult(
        period=period,
        total_labelled=overall.labelled,
        total_fraud=overall.fraud,
        base_rate=overall.fraud / overall.labelled if overall.labelled else None,
        rows=overall.rows,
        segments=overall.segments,
        below_floor=overall.below_floor,
        below_floor_with_fraud=overall.below_floor_with_fraud,
        not_above_base_rate=overall.not_above_base_rate,
        residual=residual,
        statements=tuple(runner.statements),
        elapsed_seconds=time.monotonic() - started,
    )
    _CACHE[(db_path, period)] = result
    return result


def profile_sql(period: Period | None = None, exclude: SegmentRow | None = None) -> str:
    """The profile statement: every dimension of the labelled transactions in one pass."""
    columns = ",\n".join(
        f"    {key} AS {dimension.name}_{number}"
        for dimension in DIMENSIONS
        for number, key in enumerate(dimension.keys, start=1)
    )
    branches = "\n  UNION ALL\n".join(_branch(dimension) for dimension in DIMENSIONS)
    return f"""\
WITH labelled AS MATERIALIZED (
  SELECT
{columns},
    f.is_fraud AS is_fraud
{_FROM}{_where(period, exclude)}
),
segments AS MATERIALIZED (
{branches}
)
SELECT 'listed' AS row_kind, dimension, key1, key2, labelled, fraud,
       1 AS segments, 1 AS segments_with_fraud
FROM segments WHERE {_FLOOR}
UNION ALL
SELECT 'below floor', dimension, NULL, NULL, sum(labelled), sum(fraud),
       count(*), sum(fraud > 0)
FROM segments WHERE NOT ({_FLOOR}) GROUP BY dimension
UNION ALL
SELECT 'total', NULL, NULL, NULL, count(*), sum(is_fraud), NULL, NULL
FROM labelled
ORDER BY row_kind, dimension, fraud DESC, key1, key2"""


def headline(result: ScanResult) -> str:
    """One line naming the top segment and its share of the labelled fraud."""
    if not result.rows:
        if result.total_fraud == 0:
            return "There is no labelled fraud in the scanned transactions."
        return (
            f"No segment passes the floor of {MIN_LABELLED} labelled transactions and "
            f"{MIN_FRAUD} frauds with a fraud rate above the base rate."
        )
    top = result.rows[0]
    return (
        f"Labelled fraud is most concentrated in {top.description}: {top.fraud:,} of "
        f"{_frauds(result.total_fraud)} ({top.share_of_fraud:.1%}), at {top.lift:,.1f} times "
        "the base rate."
    )


def notes(result: ScanResult, *, top: int = DEFAULT_TOP) -> tuple[str, ...]:
    """What the table does not say: the scope, the floor, equal segments and the residual."""
    span = (
        "all dates"
        if result.period is None
        else f"{result.period.start.isoformat()} to {result.period.end.isoformat()}"
    )
    rate = "undefined" if result.base_rate is None else f"{result.base_rate:.3%}"
    lines = [
        f"Scanned {result.total_labelled:,} labelled transactions ({span}): "
        f"{_frauds(result.total_fraud)}, a base rate of {rate}.",
        f"A segment is listed when it has at least {MIN_LABELLED} labelled transactions and "
        f"{MIN_FRAUD} frauds, and a fraud rate above the base rate. Of {result.segments:,} "
        f"segments in {len(DIMENSIONS)} dimensions, {result.below_floor:,} are below that "
        f"floor ({result.below_floor_with_fraud:,} of them with at least one fraud) and "
        f"{result.not_above_base_rate:,} pass it at or under the base rate.",
    ]
    if len(result.rows) > top:
        lines.append(f"The table shows the first {top} of {len(result.rows)} listed segments.")
    lines += [
        f"{row.description} holds exactly the same labelled transactions as "
        f"{'; '.join(row.same_as)}. It is listed once."
        for row in result.rows[:top]
        if row.same_as
    ]
    residual = result.residual
    if residual is not None:
        left = (
            f"Outside {residual.excluded}, {_frauds(residual.fraud)} "
            f"{'remains' if residual.fraud == 1 else 'remain'} in "
            f"{residual.labelled:,} labelled transactions"
        )
        if residual.rows:
            names = "; ".join(row.description for row in residual.rows[:top])
            lines.append(f"{left}, and {len(residual.rows)} segment(s) still pass the floor: {names}.")
        else:
            lines.append(f"{left}; no segment passes the floor.")
        if len(result.rows) > 1:
            independent = {(row.dimension, row.keys) for row in residual.rows}
            overlapping = sum(
                (row.dimension, row.keys) not in independent for row in result.rows[1:]
            )
            if overlapping:
                lines.append(
                    f"{overlapping} of the other {len(result.rows) - 1} listed segment(s) "
                    "do not pass the floor without the top segment: they overlap it and are "
                    "not findings of their own."
                )
    lines.append(
        f"{len(result.statements)} code-written statement(s) ran in "
        f"{result.elapsed_seconds:.1f} s; the SQL shown is the profile."
    )
    return tuple(lines)


def follow_ups(result: ScanResult) -> tuple[str, ...]:
    """Questions that take the top segment further. Shown to the user, never remembered."""
    if not result.rows:
        return ()
    segment = result.rows[0].description
    return (
        f"Propose a rule for transactions with {segment}.",
        f"What is the fraud rate by month for transactions with {segment}?",
        f"How many transactions, labelled or not, have {segment}?",
    )


class _Profile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labelled: int
    fraud: int
    rows: tuple[SegmentRow, ...]
    segments: int
    below_floor: int
    below_floor_with_fraud: int
    not_above_base_rate: int


class _Runner:
    """Runs the scan's statements through the guard and `db`, and keeps their text."""

    def __init__(
        self, db_path: Path, row_cap: int, timeout_seconds: float, max_result_bytes: int
    ) -> None:
        self._db_path = db_path
        self._row_cap = row_cap
        self._timeout_seconds = timeout_seconds
        self._max_result_bytes = max_result_bytes
        self.statements: list[str] = []

    def run(self, sql: str) -> tuple[tuple[db.Cell, ...], ...]:
        decision = check_query(sql, db_path=self._db_path)
        if not decision.accepted:
            raise ScanError(f"the SQL guard refused a scan statement ({decision.reason_code})")
        result = db.run_query(
            decision.sql,
            db_path=self._db_path,
            row_cap=self._row_cap,
            timeout_seconds=self._timeout_seconds,
            max_result_bytes=self._max_result_bytes,
        )
        if result.truncated:
            raise ScanError(
                f"a scan statement returned more than the row cap of {self._row_cap:,} rows"
            )
        self.statements.append(decision.sql)
        return result.rows


def _profile(
    runner: _Runner, period: Period | None, *, exclude: SegmentRow | None, all_fraud: int | None
) -> _Profile:
    """Run the profile and rank what it lists. `all_fraud` is the total a share is taken of."""
    rows = runner.run(profile_sql(period, exclude))
    labelled = fraud = segments = below_floor = below_floor_with_fraud = 0
    passed: list[tuple[Dimension, tuple[Key, ...], int, int]] = []
    for kind, name, key1, key2, row_labelled, row_fraud, count, with_fraud in rows:
        if kind == "total":
            labelled, fraud = _number(row_labelled), _number(row_fraud)
            continue
        dimension = _BY_NAME[_dimension_name(name)]
        segments += _number(count)
        if kind == "listed":
            keys = tuple(_key(value) for value in (key1, key2)[: len(dimension.keys)])
            passed.append((dimension, keys, _number(row_labelled), _number(row_fraud)))
        else:
            below_floor += _number(count)
            below_floor_with_fraud += _number(with_fraud)
    share_of = fraud if all_fraud is None else all_fraud
    listed = [
        SegmentRow(
            dimension=dimension.name,
            title=dimension.title,
            segment=dimension.label.format(*(_shown(key) for key in keys)),
            keys=keys,
            labelled=row_labelled,
            fraud=row_fraud,
            fraud_rate=row_fraud / row_labelled,
            lift=(row_fraud / row_labelled) / (fraud / labelled),
            share_of_fraud=row_fraud / share_of,
        )
        for dimension, keys, row_labelled, row_fraud in passed
        # Above the base rate, compared as whole numbers.
        if row_fraud * labelled > fraud * row_labelled
    ]
    merged = _merge_equal(listed, runner, period, exclude)
    return _Profile(
        labelled=labelled,
        fraud=fraud,
        rows=tuple(sorted(merged, key=_rank)),
        segments=segments,
        below_floor=below_floor,
        below_floor_with_fraud=below_floor_with_fraud,
        not_above_base_rate=len(passed) - len(listed),
    )


def _rank(row: SegmentRow) -> tuple[int, float, int, str]:
    """Share of fraud first (the fraud count, as the total is shared), then lift, then a fixed order."""
    return (-row.fraud, -row.lift, _ORDER[row.dimension], row.segment)


def _merge_equal(
    rows: Sequence[SegmentRow], runner: _Runner, period: Period | None, exclude: SegmentRow | None
) -> list[SegmentRow]:
    """List once the segments of different dimensions that hold the same labelled transactions.

    Equal counts only make two segments candidates; a statement counting
    the transactions in both decides.
    """
    kept: list[SegmentRow] = []
    by_detail = sorted(rows, key=lambda row: (-_BY_NAME[row.dimension].detail, _rank(row)))
    for row in by_detail:
        for index, other in enumerate(kept):
            if (
                other.dimension != row.dimension
                and (other.labelled, other.fraud) == (row.labelled, row.fraud)
                and _in_both(runner, other, row, period, exclude) == row.labelled
            ):
                names = (*other.same_as, row.description, *row.same_as)
                kept[index] = other.model_copy(update={"same_as": names})
                break
        else:
            kept.append(row)
    return kept


def _in_both(
    runner: _Runner,
    first: SegmentRow,
    second: SegmentRow,
    period: Period | None,
    exclude: SegmentRow | None,
) -> int:
    both = f"{_condition(first)} AND {_condition(second)}"
    sql = f"SELECT count(*) AS in_both\n{_FROM}{_where(period, exclude, both)}"
    return _number(runner.run(sql)[0][0])


def _branch(dimension: Dimension) -> str:
    names = [f"{dimension.name}_{number}" for number in range(1, len(dimension.keys) + 1)]
    key2 = names[1] if len(names) > 1 else "NULL"
    return (
        f"  SELECT '{dimension.name}' AS dimension, {names[0]} AS key1, {key2} AS key2,\n"
        "         count(*) AS labelled, sum(is_fraud) AS fraud\n"
        f"  FROM labelled GROUP BY {', '.join(names)}"
    )


def _where(period: Period | None, exclude: SegmentRow | None, also: str | None = None) -> str:
    conditions = ["f.is_fraud IS NOT NULL"]
    if period is not None:
        # `date` holds timestamps: the range ends before the day after the last one.
        after = period.end + timedelta(days=1)
        conditions.append(
            f"t.date >= '{period.start.isoformat()}' AND t.date < '{after.isoformat()}'"
        )
    if exclude is not None:
        conditions.append(f"NOT {_condition(exclude)}")
    if also is not None:
        conditions.append(also)
    return "\n  WHERE " + "\n    AND ".join(conditions)


def _condition(row: SegmentRow) -> str:
    """The segment as a condition over the tables of `_FROM`. `IS` also matches a NULL key."""
    keys = _BY_NAME[row.dimension].keys
    return "(" + " AND ".join(f"{key} IS {_literal(value)}" for key, value in zip(keys, row.keys)) + ")"


def _literal(value: Key) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


def _shown(value: Key) -> str:
    if value is None:
        return "(none)"
    return "(blank)" if value == "" else str(value)


def _frauds(count: int) -> str:
    return f"{count:,} labelled fraud{'' if count == 1 else 's'}"


def _key(value: db.Cell) -> Key:
    if isinstance(value, float | bytes):
        raise ScanError("a segment key is neither text nor a whole number")
    return value


def _number(value: db.Cell) -> int:
    if value is None:
        return 0
    if not isinstance(value, int):
        raise ScanError("a count came back as something other than a whole number")
    return value


def _dimension_name(value: db.Cell) -> DimensionName:
    for name in _BY_NAME:
        if value == name:
            return name
    raise ScanError("the profile returned a dimension the scan does not know")
