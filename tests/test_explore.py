"""The fraud-concentration scan: dimensions, floor, ranking, equal segments, residual, period.

Each expectation on a fixture database is added up by hand from the groups
in `scan_fixtures`. The last tests run the scan on data/data.db and pin
what it finds there; they are skipped when the file is missing.
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError
from scan_fixtures import (
    AMEX_PREPAID,
    AQUILA,
    BACKGROUND,
    MAIN,
    MASTERCARD_DEBIT,
    MISSING_CARD,
    ROME_GA,
    ROW_MARKERS,
    VISA_CREDIT,
    VISA_DEBIT,
    Group,
    build,
)

from fsm_assistant import db, explore, sql_guard
from fsm_assistant.config import DEFAULT_QUERY_TIMEOUT_SECONDS, DEFAULT_ROW_CAP
from fsm_assistant.explore import Period, ScanError, ScanResult, SegmentRow
from fsm_assistant.sql_guard import GuardDecision, check_query

CITY = "merchant city and state"
type Facts = tuple[str, str, int, int, tuple[str, ...]]


@pytest.fixture(autouse=True)
def no_cached_scan() -> Iterator[None]:
    explore.clear_cache()
    yield
    explore.clear_cache()


@pytest.fixture(scope="session")
def main_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("scan") / "main.db", MAIN)


def scan(path: Path, period: Period | None = None, row_cap: int = 1000) -> ScanResult:
    return explore.scan(db_path=path, row_cap=row_cap, timeout_seconds=30.0, period=period)


def facts(rows: tuple[SegmentRow, ...]) -> list[Facts]:
    return [(row.title, row.segment, row.labelled, row.fraud, row.same_as) for row in rows]


def varied(first: dict[str, object], second: dict[str, object], rest: dict[str, object]) -> list[Group]:
    """Three groups alike in everything but the given fields: 100/60, 200/20 and 1000/0."""
    return [
        Group(labelled=100, fraud=60).model_copy(update=first),
        Group(labelled=200, fraud=20).model_copy(update=second),
        Group(labelled=1000, fraud=0).model_copy(update=rest),
    ]


# --- The fixed list ---------------------------------------------------------------------------


def test_the_dimensions_bands_and_floor_are_the_documented_ones() -> None:
    assert [dimension.name for dimension in explore.DIMENSIONS] == [
        "transaction_type", "merchant_state", "merchant_city", "merchant_category", "hour",
        "error", "amount_band", "card_type", "card_brand",
    ]  # fmt: skip
    assert (explore.MIN_LABELLED, explore.MIN_FRAUD) == (100, 10)
    assert [edge for edge, _ in explore.AMOUNT_BANDS] == [0, 1_000, 5_000, 10_000, 50_000, 100_000]
    # Only the city is read with a second column, and only it outranks an equal segment.
    assert [d.name for d in explore.DIMENSIONS if d.detail > 1] == ["merchant_city"]
    assert all(1 <= len(dimension.keys) <= 2 for dimension in explore.DIMENSIONS)


# --- Each dimension ---------------------------------------------------------------------------

HOUR_0, HOUR_23 = "2019-03-15 00:00:00", "2019-03-15 23:59:00"


@pytest.mark.parametrize(
    ("first", "second", "rest", "expected"),
    [
        (
            {"transaction_type": "Chip Transaction"},
            {"transaction_type": "Online Transaction"},
            {"transaction_type": "Swipe Transaction"},
            [("transaction type", "Chip Transaction", 100, 60, ()),
             ("transaction type", "Online Transaction", 200, 20, ())],
        ),
        (
            # Two cities of one country: the country is one segment and each city another.
            {"location": 1},
            {"location": AQUILA},
            {"location": 3},
            [("merchant state or country", "Italy", 300, 80, ()),
             (CITY, "Rome, Italy", 100, 60, ()),
             (CITY, "L'Aquila, Italy", 200, 20, ())],
        ),
        (
            # A city name in two states is two segments; each equals its state here.
            {"location": 1},
            {"location": ROME_GA},
            {"location": 3},
            [(CITY, "Rome, Italy", 100, 60, ("merchant state or country = Italy",)),
             (CITY, "Rome, GA", 200, 20, ("merchant state or country = GA",))],
        ),
        (
            # Two codes share a description: grouped by code.
            {"merchant": 30},
            {"merchant": 31},
            {"merchant": 20},
            [("merchant category (MCC)", "3722 Passenger Railways", 100, 60, ()),
             ("merchant category (MCC)", "3723 Passenger Railways", 200, 20, ())],
        ),
        (
            {"timestamp": HOUR_0},
            {"timestamp": HOUR_23},
            {"timestamp": "2019-03-15 09:30:00"},
            [("hour of day", "0", 100, 60, ()), ("hour of day", "23", 200, 20, ())],
        ),
        (
            {"error": None},
            {"error": "Bad PIN"},
            {"error": "Insufficient Balance"},
            [("error", "No error", 100, 60, ()), ("error", "Bad PIN", 200, 20, ())],
        ),
        (
            # Three cards: two share a brand, two share a type.
            {"card": VISA_CREDIT},
            {"card": VISA_DEBIT},
            {"card": MASTERCARD_DEBIT},
            [("card brand", "Visa", 300, 80, ()), ("card type", "Credit", 100, 60, ())],
        ),
    ],
    ids=["transaction type", "state and city", "city with state", "category", "hour", "error", "card"],
)  # fmt: skip
def test_a_dimension_groups_the_labelled_transactions(
    tmp_path: Path,
    first: dict[str, object],
    second: dict[str, object],
    rest: dict[str, object],
    expected: list[Facts],
) -> None:
    result = scan(build(tmp_path / "one.db", varied(first, second, rest)))
    assert (result.total_labelled, result.total_fraud) == (1300, 80)
    assert facts(result.rows) == expected
    for row in result.rows:
        assert row.fraud_rate == pytest.approx(row.fraud / row.labelled)
        assert row.lift == pytest.approx(row.fraud_rate / (80 / 1300))
        assert row.share_of_fraud == pytest.approx(row.fraud / 80)


def test_amount_bands_have_the_documented_edges_and_negatives_are_their_own(tmp_path: Path) -> None:
    # One group of 100 on each side of every edge, with 10, 11, ... 21 frauds.
    amounts = [-1, 0, 999, 1_000, 4_999, 5_000, 9_999, 10_000, 49_999, 50_000, 99_999, 100_000]
    groups = [
        Group(labelled=100, fraud=10 + index, amount=amount) for index, amount in enumerate(amounts)
    ]
    groups.append(Group(labelled=1000, fraud=0, amount=-50_000))
    result = scan(build(tmp_path / "bands.db", groups))

    assert (result.total_labelled, result.total_fraud) == (2200, 186)
    assert [(row.segment, row.labelled, row.fraud) for row in result.rows] == [
        ("$500 to under $1,000", 200, 39),
        ("$100 to under $500", 200, 35),
        ("$50 to under $100", 200, 31),
        ("$10 to under $50", 200, 27),
        ("$0 to under $10", 200, 23),
        ("$1,000 and over", 100, 21),
    ]
    # The negative band holds 1,100 transactions and 10 frauds: it passes the
    # floor under the base rate, and so does the one segment of every other dimension.
    assert {row.title for row in result.rows} == {"amount band"}
    assert (result.segments, result.below_floor, result.not_above_base_rate) == (15, 0, 9)


def test_a_missing_card_is_a_segment_and_a_quote_in_a_name_is_escaped(tmp_path: Path) -> None:
    odd = Group(labelled=100, fraud=50, location=AQUILA, card=MISSING_CARD)
    quiet = BACKGROUND.model_copy(update={"fraud": 20})
    result = scan(build(tmp_path / "odd.db", [odd, quiet]))

    assert result.total_labelled == 1100, "a transaction without a card is still counted"
    top = result.rows[0]
    assert (top.title, top.segment, top.keys) == (CITY, "L'Aquila, Italy", ("L'Aquila", "Italy"))
    assert {"card type = (none)", "card brand = (none)"} <= set(top.same_as)
    assert result.residual is not None and result.residual.profiled
    assert (result.residual.labelled, result.residual.fraud) == (1000, 20)
    text = "\n".join(result.statements)
    assert "NOT (l.city IS 'L''Aquila' AND l.state IS 'Italy')" in text
    assert "k.card_type IS NULL" in text


# --- The whole result on the main fixture -----------------------------------------------------


def test_the_scan_of_the_main_fixture(main_db: Path) -> None:
    result = scan(main_db)

    assert result.period is None
    assert (result.total_labelled, result.total_fraud) == (799, 189)
    assert result.base_rate == pytest.approx(189 / 799)
    # Group A is one set of transactions under eight names; the grocery code adds group C.
    assert facts(result.rows) == [
        (
            CITY, "Rome, Italy", 200, 150,
            (
                "transaction type = Chip Transaction",
                "merchant state or country = Italy",
                "hour of day = 11",
                "error = No error",
                "amount band = $10 to under $50",
                "card type = Credit",
                "card brand = Visa",
            ),
        ),
        ("merchant category (MCC)", "5411 Grocery Stores, Supermarkets", 300, 150, ()),
    ]  # fmt: skip
    top, second = result.rows
    assert top.keys == ("Rome", "Italy") and second.keys == (5411, "Grocery Stores, Supermarkets")
    assert top.fraud_rate == 0.75 and second.fraud_rate == 0.5
    assert top.lift == pytest.approx(0.75 / (189 / 799))
    assert second.lift == pytest.approx(0.5 / (189 / 799))
    assert top.share_of_fraud == second.share_of_fraud == pytest.approx(150 / 189)
    # 28 segments: 9 under the floor (6 of them with fraud), 10 at or under the base rate,
    # and 9 listed, which the equal ones reduce to 2.
    assert (result.segments, result.below_floor, result.below_floor_with_fraud) == (28, 9, 6)
    assert result.not_above_base_rate == 10
    # The profile, seven checks of equal counts, then the same again without group A.
    assert len(result.statements) == 11
    assert result.statements[0] == explore.profile_sql()
    assert result.elapsed_seconds > 0


def test_the_residual_check_profiles_what_is_left_without_the_top_segment(main_db: Path) -> None:
    result = scan(main_db)
    residual = result.residual

    assert residual is not None
    assert residual.excluded == "merchant city and state = Rome, Italy"
    assert (residual.labelled, residual.fraud, residual.profiled) == (599, 39, True)
    assert facts(residual.rows) == [
        (CITY, "Sacramento, CA", 499, 39, ("merchant state or country = CA",)),
        ("error", "Insufficient Balance", 400, 29, ()),
        ("merchant category (MCC)", "4829 Money Transfer", 300, 20, ("amount band = $0 to under $10",)),
    ]  # fmt: skip
    # Lift is over what is left; the share is still of all 189 labelled frauds.
    assert [row.lift for row in residual.rows] == pytest.approx(
        [(39 / 499) / (39 / 599), (29 / 400) / (39 / 599), (20 / 300) / (39 / 599)]
    )
    assert [row.share_of_fraud for row in residual.rows] == pytest.approx([39 / 189, 29 / 189, 20 / 189])
    # The grocery code was listed only because it contains group A.
    assert "5411" not in "".join(row.segment for row in residual.rows)
    assert explore.profile_sql(None, result.rows[0]) in result.statements


def test_the_residual_is_not_profiled_when_too_little_fraud_is_left(tmp_path: Path) -> None:
    few = BACKGROUND.model_copy(update={"fraud": explore.MIN_FRAUD - 1})
    result = scan(build(tmp_path / "few.db", [Group(labelled=200, fraud=150), few]))
    assert result.residual is not None
    assert (result.residual.labelled, result.residual.fraud) == (1000, 9)
    assert result.residual.profiled is False and result.residual.rows == ()
    assert not any("NOT (l.city" in statement for statement in result.statements)


def test_a_residual_that_does_not_add_up_is_an_error(
    main_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An exclusion that removed nothing would describe the same data twice.
    monkeypatch.setattr(explore, "_condition", lambda row: "(1 = 0)")
    with pytest.raises(ScanError, match="does not add up"):
        scan(main_db)


# --- The support floor ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("labelled", "fraud", "listed"),
    [(100, 10, True), (101, 11, True), (99, 10, False), (100, 9, False), (99, 9, False)],
)
def test_the_support_floor_at_and_around_its_thresholds(
    tmp_path: Path, labelled: int, fraud: int, listed: bool
) -> None:
    result = scan(build(tmp_path / "floor.db", [Group(labelled=labelled, fraud=fraud), BACKGROUND]))

    assert result.segments == 18, "two segments in each of nine dimensions"
    if listed:
        assert facts(result.rows)[0][:4] == (CITY, "Rome, Italy", labelled, fraud)
        assert len(result.rows) == 1 and len(result.rows[0].same_as) == 8
        # The nine segments of the group without fraud.
        assert (result.below_floor, result.below_floor_with_fraud) == (9, 0)
    else:
        assert result.rows == () and result.residual is None
        assert (result.below_floor, result.below_floor_with_fraud) == (18, 9)
        assert explore.headline(result).startswith("No segment passes the floor of 100 labelled")
    assert result.not_above_base_rate == 0


def test_a_segment_at_the_base_rate_is_not_listed(tmp_path: Path) -> None:
    # Two groups with the same fraud rate: every segment passes the floor and none stands out.
    groups = [Group(labelled=100, fraud=10), BACKGROUND.model_copy(update={"fraud": 100})]
    result = scan(build(tmp_path / "flat.db", groups))
    assert result.rows == ()
    assert (result.below_floor, result.not_above_base_rate) == (0, 18)


# --- Ranking and equal segments ---------------------------------------------------------------


def test_ranking_is_by_fraud_held_then_lift_then_name(tmp_path: Path) -> None:
    def at(hour: int, labelled: int, fraud: int) -> Group:
        return Group(labelled=labelled, fraud=fraud, timestamp=f"2019-03-15 {hour:02d}:00:00")

    groups = [at(3, 200, 50), at(2, 100, 50), at(1, 100, 50), at(4, 150, 60), at(5, 1000, 0)]
    result = scan(build(tmp_path / "rank.db", groups))
    # Hour 4 holds the most fraud; hours 1 and 2 tie on both measures; hour 3 has their
    # share at half their lift.
    assert [(row.segment, row.fraud) for row in result.rows] == [("4", 60), ("1", 50), ("2", 50), ("3", 50)]
    assert result.rows[1].lift == result.rows[2].lift > result.rows[3].lift
    assert result.rows[0].lift < result.rows[1].lift, "a higher lift does not outrank a larger share"
    explore.clear_cache()
    assert scan(tmp_path / "rank.db").rows == result.rows, "the order is the same on every run"


def test_equal_counts_alone_do_not_make_two_segments_one(tmp_path: Path) -> None:
    chip_credit = Group(labelled=100, fraud=50)
    swipe_debit = Group(
        labelled=100, fraud=50, transaction_type="Swipe Transaction", card=MASTERCARD_DEBIT
    )
    rest = Group(labelled=1000, fraud=0, transaction_type="Online Transaction", card=AMEX_PREPAID)
    result = scan(build(tmp_path / "equal.db", [chip_credit, swipe_debit, rest]))

    # Chip and Debit have the same counts and share no transaction: they stay apart.
    # Of two equal segments of equal detail, the earlier dimension is the one listed.
    assert facts(result.rows) == [
        ("transaction type", "Chip Transaction", 100, 50, ("card type = Credit", "card brand = Visa")),
        ("transaction type", "Swipe Transaction", 100, 50, ("card type = Debit", "card brand = Mastercard")),
    ]  # fmt: skip
    assert result.residual is not None
    assert facts(result.residual.rows) == [facts(result.rows)[1]]


def test_the_more_specific_of_two_equal_segments_is_the_one_listed(main_db: Path) -> None:
    top = scan(main_db).rows[0]
    assert top.dimension == "merchant_city"
    assert "merchant state or country = Italy" in top.same_as
    assert "holds exactly the same labelled transactions as" in "\n".join(explore.notes(scan(main_db)))


# --- The period -------------------------------------------------------------------------------


def test_the_default_covers_all_the_data_and_a_period_narrows_it(main_db: Path) -> None:
    assert "t.date" not in scan(main_db).statements[0].split("FROM transactions t")[1]

    # January and February hold groups B and C: 400 labelled, 20 frauds, all in B.
    period = Period(start=date(2019, 1, 1), end=date(2019, 2, 28))
    result = scan(main_db, period)
    assert result.period == period
    assert (result.total_labelled, result.total_fraud) == (400, 20)
    assert facts(result.rows) == [
        (
            CITY, "Sacramento, CA", 300, 20,
            (
                "merchant state or country = CA",
                "merchant category (MCC) = 4829 Money Transfer",
                "error = Insufficient Balance",
                "amount band = $0 to under $10",
            ),
        )
    ]  # fmt: skip
    assert (result.segments, result.below_floor, result.not_above_base_rate) == (14, 5, 4)
    assert result.residual is not None
    assert (result.residual.labelled, result.residual.fraud, result.residual.profiled) == (100, 0, False)
    assert "t.date >= '2019-01-01' AND t.date < '2019-03-01'" in result.statements[0]
    assert "(2019-01-01 to 2019-02-28)" in explore.notes(result)[0]


def test_the_last_day_of_a_period_is_included(main_db: Path) -> None:
    # Group B is at 09:00 on 31 January.
    through = scan(main_db, Period(start=date(2019, 1, 1), end=date(2019, 1, 31)))
    before = scan(main_db, Period(start=date(2019, 1, 1), end=date(2019, 1, 30)))
    assert (through.total_labelled, through.total_fraud) == (300, 20)
    assert (before.total_labelled, before.total_fraud, before.base_rate) == (0, 0, None)
    assert before.rows == () and before.residual is None
    assert explore.headline(before) == "There is no labelled fraud in the scanned transactions."
    assert "a base rate of undefined" in explore.notes(before)[0]


def test_a_period_must_run_forwards() -> None:
    assert Period(start=date(2019, 1, 1), end=date(2019, 1, 1)).start == date(2019, 1, 1)
    with pytest.raises(ValidationError, match="ends before it starts"):
        Period(start=date(2019, 2, 1), end=date(2019, 1, 31))
    with pytest.raises(ValidationError, match="before 9999-12-31"):
        Period(start=date(2019, 2, 1), end=date.max)


# --- Caching ----------------------------------------------------------------------------------


def test_a_result_is_cached_per_database_and_period(
    main_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []
    real_run = db.run_query

    def run(sql: str, *args: object, **kwargs: object) -> db.QueryResult:
        ran.append(sql)
        return real_run(sql, *args, **kwargs)  # type: ignore[arg-type]  # passed through as given

    monkeypatch.setattr(db, "run_query", run)
    first = scan(main_db)
    count = len(ran)
    assert count == len(first.statements) == 11
    assert scan(main_db) is first and len(ran) == count, "the second call ran nothing"

    january = Period(start=date(2019, 1, 1), end=date(2019, 1, 31))
    narrowed = scan(main_db, january)
    assert narrowed is not first and len(ran) > count
    assert scan(main_db, Period(start=date(2019, 1, 1), end=date(2019, 1, 31))) is narrowed
    other = scan(build(tmp_path / "other.db", MAIN))
    assert other is not first and other.rows == first.rows

    explore.clear_cache()
    assert scan(main_db) is not first


def test_a_failed_scan_is_not_cached(main_db: Path) -> None:
    with pytest.raises(ScanError, match="row cap of 2 rows"):
        scan(main_db, row_cap=2)
    assert scan(main_db).total_labelled == 799


# --- The guard and the limits -----------------------------------------------------------------


def test_every_statement_is_accepted_by_the_guard_before_it_runs(
    main_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted: list[str] = []
    executed: list[str] = []
    real_check, real_run = sql_guard.check_query, db.run_query

    def check(sql: str, *, db_path: Path) -> GuardDecision:
        decision = real_check(sql, db_path=db_path)
        assert decision.accepted, decision.reason
        accepted.append(decision.sql)
        return decision

    def run(sql: str, *args: object, **kwargs: object) -> db.QueryResult:
        assert sql in accepted, "a statement ran that the guard had not seen"
        executed.append(sql)
        return real_run(sql, *args, **kwargs)  # type: ignore[arg-type]  # passed through as given

    monkeypatch.setattr(explore, "check_query", check)
    monkeypatch.setattr(db, "run_query", run)
    results = [scan(main_db), scan(main_db, Period(start=date(2019, 1, 1), end=date(2019, 2, 28)))]

    assert executed == accepted == [s for result in results for s in result.statements]
    kinds = {statement.split()[0] for statement in executed}
    assert kinds == {"WITH", "SELECT"}
    for statement in executed:
        decision = check_query(statement, db_path=main_db)
        assert decision.accepted and decision.reads is not None
        assert "fraud_labels" in decision.reads.tables


def test_a_statement_the_guard_refuses_stops_the_scan_and_nothing_runs(
    main_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(sql: str, *, db_path: Path) -> GuardDecision:
        return check_query("SELECT name FROM sqlite_master", db_path=db_path)

    def run(*args: object, **kwargs: object) -> db.QueryResult:
        raise AssertionError("nothing may run")

    monkeypatch.setattr(explore, "check_query", refuse)
    monkeypatch.setattr(db, "run_query", run)
    with pytest.raises(ScanError, match=r"the SQL guard refused a scan statement \(table_not_allowed\)"):
        scan(main_db)


def test_the_configured_limits_are_passed_to_every_query(
    main_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []
    real_run = db.run_query

    def run(sql: str, **limits: object) -> db.QueryResult:
        seen.append(limits)
        return real_run(sql, **limits)  # type: ignore[arg-type]  # passed through as given

    monkeypatch.setattr(db, "run_query", run)
    explore.scan(db_path=main_db, row_cap=77, timeout_seconds=9.0, max_result_bytes=123_456)
    assert len(seen) == 11
    assert all(
        limits == {"db_path": main_db, "row_cap": 77, "timeout_seconds": 9.0, "max_result_bytes": 123_456}
        for limits in seen
    )


def test_a_timeout_or_a_missing_database_is_an_error_of_db(main_db: Path, tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseNotFoundError):
        scan(tmp_path / "gone.db")
    big = build(tmp_path / "big.db", [Group(labelled=20_000, fraud=100), BACKGROUND])
    with pytest.raises(db.QueryTimeout):
        explore.scan(db_path=big, row_cap=1000, timeout_seconds=0.0005)


# --- Aggregates only --------------------------------------------------------------------------


def test_a_result_holds_aggregates_and_no_value_of_a_row(main_db: Path) -> None:
    result = scan(main_db)
    said = "\n".join((explore.headline(result), *explore.notes(result), *explore.follow_ups(result)))
    everything = result.model_dump_json() + said
    for marker in ROW_MARKERS:
        assert marker not in everything, f"{marker!r} is a value of a row"
    assert set(SegmentRow.model_fields) == {
        "dimension", "title", "segment", "keys", "labelled", "fraud", "fraud_rate", "lift",
        "share_of_fraud", "same_as",
    }  # fmt: skip
    assert all(isinstance(key, str | int | None) for row in result.rows for key in row.keys)


# --- In words ---------------------------------------------------------------------------------


def test_the_headline_notes_and_follow_ups_of_the_main_fixture(main_db: Path) -> None:
    result = scan(main_db)
    assert explore.headline(result) == (
        "Labelled fraud is most concentrated in merchant city and state = Rome, Italy: 150 of "
        "189 labelled frauds (79.4%), at 3.2 times the base rate."
    )
    notes = explore.notes(result)
    assert notes[0] == (
        "Scanned 799 labelled transactions (all dates): 189 labelled frauds, a base rate of 23.655%."
    )
    assert notes[1] == (
        "A segment is listed when it has at least 100 labelled transactions and 10 frauds, and "
        "a fraud rate above the base rate. Of 28 segments in 9 dimensions, 9 are below that "
        "floor (6 of them with at least one fraud) and 10 pass it at or under the base rate."
    )
    assert notes[3] == (
        "Outside merchant city and state = Rome, Italy, 39 labelled frauds remain in 599 "
        "labelled transactions, and 3 segment(s) still pass the floor: merchant city and state "
        "= Sacramento, CA; error = Insufficient Balance; merchant category (MCC) = 4829 Money "
        "Transfer."
    )
    assert notes[4] == (
        "1 of the other 1 listed segment(s) do not pass the floor without the top segment: "
        "they overlap it and are not findings of their own."
    )
    assert notes[5].startswith("11 code-written statement(s) ran in ")
    assert len(notes) == 6
    assert "The table shows the first 1 of 2 listed segments." in explore.notes(result, top=1)
    assert explore.follow_ups(result) == (
        "Propose a rule for transactions with merchant city and state = Rome, Italy.",
        "What is the fraud rate by month for transactions with merchant city and state = Rome, Italy?",
        "How many transactions, labelled or not, have merchant city and state = Rome, Italy?",
    )


def test_a_scan_that_lists_nothing_says_so_and_suggests_nothing(tmp_path: Path) -> None:
    result = scan(build(tmp_path / "clean.db", [BACKGROUND]))
    assert (result.total_labelled, result.total_fraud, result.base_rate) == (1000, 0, 0.0)
    assert result.rows == () and result.residual is None
    assert explore.headline(result) == "There is no labelled fraud in the scanned transactions."
    assert explore.follow_ups(result) == ()
    assert len(explore.notes(result)) == 3


def test_one_remaining_fraud_is_written_in_the_singular(tmp_path: Path) -> None:
    result = scan(build(tmp_path / "one.db", [Group(labelled=200, fraud=150), BACKGROUND.model_copy(update={"fraud": 1})]))
    assert "1 labelled fraud remains in 1,000 labelled transactions; no segment passes" in (
        "\n".join(explore.notes(result))
    )


# --- The real database ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_scan(real_db: Path) -> ScanResult:
    explore.clear_cache()
    return explore.scan(
        db_path=real_db, row_cap=DEFAULT_ROW_CAP, timeout_seconds=DEFAULT_QUERY_TIMEOUT_SECONDS
    )


def test_real_data_all_labelled_fraud_is_in_rome_italy(real_scan: ScanResult) -> None:
    assert (real_scan.total_labelled, real_scan.total_fraud) == (777_339, 1_360)
    assert real_scan.base_rate == pytest.approx(1_360 / 777_339)
    top = real_scan.rows[0]
    assert (top.dimension, top.segment, top.keys) == ("merchant_city", "Rome, Italy", ("Rome", "Italy"))
    assert (top.labelled, top.fraud, top.share_of_fraud) == (1_598, 1_360, 1.0)
    assert top.fraud_rate == pytest.approx(1_360 / 1_598)
    assert top.lift == pytest.approx((1_360 / 1_598) / (1_360 / 777_339))
    # Italy is the same 1,598 labelled transactions and is not a second finding.
    assert top.same_as == ("merchant state or country = Italy",)
    assert not [row for row in real_scan.rows if row.dimension == "merchant_state"]


def test_real_data_nothing_is_left_outside_the_top_segment(real_scan: ScanResult) -> None:
    residual = real_scan.residual
    assert residual is not None
    assert (residual.labelled, residual.fraud) == (775_741, 0)
    assert residual.profiled is False and residual.rows == ()
    notes = "\n".join(explore.notes(real_scan))
    assert (
        "Outside merchant city and state = Rome, Italy, 0 labelled frauds remain in 775,741 "
        "labelled transactions; no segment passes the floor."
    ) in notes
    # Every other listed segment is a side effect of the first.
    assert "37 of the other 37 listed segment(s) do not pass the floor without the top" in notes


def test_real_data_counts_of_segments_and_the_ranking_below_the_top(real_scan: ScanResult) -> None:
    assert len(real_scan.rows) == 38
    assert (real_scan.segments, real_scan.below_floor) == (9_417, 9_360)
    assert (real_scan.below_floor_with_fraud, real_scan.not_above_base_rate) == (21, 18)
    assert [(row.title, row.segment, row.labelled, row.fraud) for row in real_scan.rows[1:4]] == [
        ("transaction type", "Chip Transaction", 551_905, 1_238),
        ("card type", "Credit", 237_316, 562),
        ("card brand", "Visa", 289_551, 557),
    ]
    frauds = [row.fraud for row in real_scan.rows]
    assert frauds == sorted(frauds, reverse=True)
    assert all(row.lift > 1 and row.labelled >= 100 and row.fraud >= 10 for row in real_scan.rows)
    # Ranked by lift alone, a segment of a few hundred transactions would come second.
    by_lift = sorted(real_scan.rows, key=lambda row: -row.lift)
    assert (by_lift[0], by_lift[1].labelled < 1_000) == (real_scan.rows[0], True)


def test_real_data_the_scan_is_two_statements_within_the_time_limit(
    real_scan: ScanResult, real_db: Path
) -> None:
    assert len(real_scan.statements) == 2
    assert all(check_query(s, db_path=real_db).accepted for s in real_scan.statements)
    # About 4 s on a laptop; one statement must stay under the 15 s default timeout.
    assert real_scan.elapsed_seconds < 12.0
