"""presentation: format hints, coded caveats, safe cells and the terminal text."""

import pytest
from test_proposal import evidence, month

from fsm_assistant.db import Cell, QueryResult
from fsm_assistant.presentation import (
    EVIDENCE_NOTE,
    HIGH_FLAG_RATE,
    MIN_EXPECTED_FRAUD,
    SMALL_SAMPLE_LABELLED,
    WEAK_PRECISION_RATIO,
    column_format,
    evidence_caveats,
    format_cell,
    outcome_of,
    present_table,
    query_caveats,
    render_text,
    rule_caveats,
    rule_card,
    safe_text,
    status_of,
)
from fsm_assistant.result import (
    Caveat,
    ColumnFormat,
    ErrorKind,
    Outcome,
    RuleCard,
    Status,
    TableResult,
    TurnError,
    TurnMeta,
    TurnResult,
)
from fsm_assistant.rules.backtest import BacktestResult
from fsm_assistant.rules.proposal import build_evidence

META = TurnMeta(
    conversation_id="c1",
    turn=1,
    model_id="claude-test",
    prompt_version="turn-3",
    tool_mode="tools",
    attempts=1,
    model_calls=1,
    input_tokens=3300,
    output_tokens=120,
    elapsed_ms=1234,
)


def query(columns: tuple[str, ...], *rows: tuple[Cell, ...], truncated: bool = False) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, truncated=truncated, elapsed_seconds=0.1)


def measured(flagged: int, labelled: int, fraud: int, total: int = 1000) -> BacktestResult:
    return BacktestResult(
        clause="amount_usd_cents > 100000",
        flagged=flagged,
        flagged_labelled=labelled,
        true_positives=fraud,
        false_positives=labelled - fraud,
        total_transactions=total,
        total_labelled=700,
        total_fraud=40,
        flagged_amount_cents=123456,
        fraud_amount_caught_cents=2500,
        false_positive_amount_cents=-500,
        elapsed_seconds=0.1,
    )


def card(backtest: BacktestResult) -> RuleCard:
    """The card of a backtest with no monthly rows."""
    return rule_card(build_evidence(backtest, ()))


def shown(
    outcome: Outcome = "answered",
    *,
    answer: str = "The query returned 2 rows.",
    table: TableResult | None = None,
    error: TurnError | None = None,
    **fields: object,
) -> TurnResult:
    base = TurnResult(
        outcome=outcome,
        status=status_of(outcome),
        error=error,
        answer=answer,
        answer_source="code",
        tool="run_sql",
        refused=(),
        sql=None,
        where_clause=None,
        table=table,
        rule=None,
        explanation=None,
        assumptions=(),
        suggestions=(),
        caveats=(),
        meta=META,
    )
    return base.model_copy(update=fields)


# --- Outcomes -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("answered", "ok"),
        ("rule_proposed", "ok"),
        ("clarification", "needs_clarification"),
        ("cannot_answer", "cannot_answer"),
        ("rejected", "rejected"),
        ("failed", "error"),
    ],
)
def test_each_outcome_has_one_status(outcome: Outcome, status: Status) -> None:
    assert status_of(outcome) == status


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("sql_rejected", "rejected"),
        ("sql_execution", "failed"),
        ("timeout", "failed"),
        ("invalid_input", "failed"),
        ("model_unavailable", "failed"),
        ("model_output", "failed"),
        ("internal", "failed"),
        ("not_available", "cannot_answer"),
    ],
)
def test_each_error_kind_maps_to_one_outcome(kind: ErrorKind, outcome: Outcome) -> None:
    assert outcome_of(kind) == outcome


# --- Format hints -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "values", "expected"),
    [
        ("amount_usd_cents", [2500, -5000, None], "usd_cents"),
        ("t.amount_usd_cents", [2500], "usd_cents"),
        ("SUM(amount_usd_cents)", [2500], "usd_cents"),
        ("avg(t.amount_usd_cents)", [2500.5], "usd_cents"),
        ("amount_usd_cents", ["a lot"], "plain"),
        ("count(amount_usd_cents)", [12], "plain"),
        ("amount_usd_cents / 100.0", [25.0], "plain"),
        ("fraud_rate", [0.00175, 0.0, 1], "fraction"),
        ("precision", [0.5], "fraction"),
        ("fraud_rate", [17.5], "plain"),
        ("fraud_rate", [0, 1], "plain"),
        ("fraud_rate", [], "plain"),
        ("fraud_rate_pct", [0.17], "plain"),
        ("fraud_rate_percent", [0.17], "plain"),
        ("rate_count", [0.5], "plain"),
        ("error_rate", [0.5], "fraction"),
        ("t.fraud_rate", [0.5], "fraction"),
        ("share", [0.25], "fraction"),
        ("generated", [0.25], "plain"),
        ("separate", [0.75], "plain"),
        ("moderate_score", [0.5], "plain"),
        ("shared_cards", [0.3], "plain"),
        ("fraud_count_rate", [0.5], "plain"),
        ("fraud_rate_percentage", [0.17], "plain"),
        ("distinct_amount_usd_cents", [5], "plain"),
        ("n_cents", [6], "plain"),
        ("num_amount_usd_cents", [6], "plain"),
        ("count_amount_usd_cents", [6], "plain"),
        ("count(DISTINCT amount_usd_cents)", [5], "plain"),
        ("sum(n_cents)", [6], "plain"),
        ("max(yearly_income_usd_cents)", [6000000], "usd_cents"),
        ("country_spend_cents", [2500], "usd_cents"),
        ("cents", [2500], "plain"),
        ("id", [22326479], "identifier"),
        ("card_id", [100, None], "identifier"),
        ("t.merchant_id", [10], "identifier"),
        ("zip", [95814], "identifier"),
        ("mcc", [5411], "identifier"),
        ("year_pin_last_changed", [2015], "identifier"),
        ("mcc_code", [5045], "identifier"),
        ("m.mcc_code", [5045], "identifier"),
        ("year", [2019], "identifier"),
        ("zip_code", [95814], "identifier"),
        ("user_id", [42], "identifier"),
        ("code", [7], "identifier"),
        ("card_number", [4344676511950444], "identifier"),
        ("account_number", [1234567], "identifier"),
        ("mcc_code", ["5045"], "plain"),
        ("transaction_count", [5045], "plain"),
        ("count(user_id)", [5045], "plain"),
        ("labelled", [5045], "plain"),
        ("decoded", [5045], "plain"),
        ("transaction_id", ["1001"], "plain"),
        ("count(*)", [1159966], "plain"),
        ("paid", [1], "plain"),
    ],
)
def test_format_hint(name: str, values: list[Cell], expected: ColumnFormat) -> None:
    assert column_format(name, values) == expected


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        (1159966, "plain", "1,159,966"),
        (22326479, "identifier", "22326479"),
        (100000, "usd_cents", "$1,000.00"),
        (-5000, "usd_cents", "-$50.00"),
        (2575.0, "usd_cents", "$25.75"),
        (0.0017495584294625639, "fraction", "0.00174956 (0.175%)"),
        (1, "fraction", "1 (100.000%)"),
        (1234567.891, "plain", "1,234,567.891"),
        (2.5, "plain", "2.5"),
        (0.0, "plain", "0"),
        (0.00012345678, "plain", "0.000123457"),
        (None, "plain", "NULL"),
        ("Rome", "plain", "Rome"),
        (b"\x00\x01\xff", "plain", "x'0001ff' (3 bytes)"),
        (b"\x00" * 40, "plain", "x'" + "00" * 16 + "…' (40 bytes)"),
    ],
)
def test_format_cell(value: Cell, kind: ColumnFormat, expected: str) -> None:
    assert format_cell(value, kind) == expected


def test_text_is_printed_on_one_line_without_control_characters() -> None:
    assert safe_text("a\nb\x1b[31mred\tc") == "a\\nb\\x1b[31mred\\tc"
    assert format_cell("x" * 100) == "x" * 59 + "…"
    assert safe_text("Zürich ✓") == "Zürich ✓"


def test_a_table_keeps_the_values_as_sqlite_returned_them() -> None:
    table = present_table(
        query(("id", "amount_usd_cents", "fraud_rate", "blob"), (7, 2500, 0.25, b"\x00"))
    )
    assert [column.format for column in table.columns] == [
        "identifier", "usd_cents", "fraction", "plain",
    ]  # fmt: skip
    assert table.rows == ((7, 2500, 0.25, b"\x00"),)
    assert (table.row_count, table.truncated) == (1, False)
    assert '"AA=="' in table.model_dump_json(), "a blob has a JSON form"


# --- Caveats ------------------------------------------------------------------------------


def codes(caveats: tuple[Caveat, ...]) -> list[str]:
    return [caveat.code for caveat in caveats]


def test_a_plain_result_has_no_caveat() -> None:
    assert query_caveats(query(("n",), (6,)), reads_labels=False) == ()


def test_truncated_caveat() -> None:
    caveats = query_caveats(query(("id",), (1,), (2,), truncated=True), reads_labels=False)
    assert codes(caveats) == ["truncated"]
    assert "Only the first 2 rows were fetched (the row cap)" in caveats[0].message


def test_label_coverage_caveat_when_the_statement_read_the_labels() -> None:
    caveats = query_caveats(query(("fraud_rate",), (0.5,)), reads_labels=True)
    assert codes(caveats) == ["label_coverage"]
    assert "unknown, not legitimate" in caveats[0].message


def test_zero_rows_caveat_hints_at_spelling_and_case() -> None:
    caveats = query_caveats(query(("id",)), reads_labels=False)
    assert codes(caveats) == ["zero_rows"]
    assert "spelling and case" in caveats[0].message
    assert "'Debit', not 'debit'" in caveats[0].message


def test_query_caveats_come_in_a_fixed_order() -> None:
    assert codes(query_caveats(query(("id",), (1,), truncated=True), reads_labels=True)) == [
        "truncated",
        "label_coverage",
    ]
    assert codes(query_caveats(query(("id",)), reads_labels=True)) == ["label_coverage", "zero_rows"]


def test_a_rule_always_states_its_label_coverage() -> None:
    caveats = rule_caveats(measured(flagged=40, labelled=30, fraud=10))
    assert codes(caveats) == ["label_coverage"]
    assert "30 of the 40 flagged transactions have a fraud label" in caveats[0].message
    assert "the other 10 are unknown" in caveats[0].message
    one = rule_caveats(measured(flagged=2, labelled=1, fraud=1))[0].message
    assert one.startswith("1 of the 2 flagged transactions has a fraud label.")
    assert "the other 1 is unknown, not legitimate." in one
    single = rule_caveats(measured(flagged=1, labelled=1, fraud=0))[0].message
    assert single == (
        "1 of the 1 flagged transaction has a fraud label. Precision and recall use those only."
    )


def test_small_sample_caveat_below_thirty_labelled_rows() -> None:
    assert SMALL_SAMPLE_LABELLED == 30
    assert codes(rule_caveats(measured(40, 29, 10))) == ["label_coverage", "small_sample"]
    assert codes(rule_caveats(measured(40, 30, 10))) == ["label_coverage"]
    assert codes(rule_caveats(measured(40, 0, 0))) == ["label_coverage", "small_sample"]
    assert "rests on 29 labelled transactions" in rule_caveats(measured(40, 29, 10))[1].message
    assert "rests on 1 labelled transaction," in rule_caveats(measured(40, 1, 1))[1].message


def test_small_sample_with_no_labelled_row_says_precision_cannot_be_measured() -> None:
    message = rule_caveats(measured(40, 0, 0))[1].message
    assert message == (
        "Nothing the rule flags has a fraud label, so its precision cannot be measured."
    )
    assert "rests on 0" not in message


def test_a_rule_that_flags_nothing_gets_zero_rows_not_small_sample() -> None:
    assert codes(rule_caveats(measured(0, 0, 0))) == ["label_coverage", "zero_rows"]


def test_high_flag_rate_caveat_over_five_percent() -> None:
    assert HIGH_FLAG_RATE == 0.05
    assert codes(rule_caveats(measured(50, 40, 10))) == ["label_coverage"]
    caveats = rule_caveats(measured(51, 40, 10))
    assert codes(caveats) == ["label_coverage", "high_flag_rate"]
    assert "5.1% of all transactions" in caveats[-1].message
    assert codes(rule_caveats(measured(60, 20, 10))) == [
        "label_coverage", "small_sample", "high_flag_rate",
    ]  # fmt: skip


def test_the_rule_card_carries_the_ratios_as_fields() -> None:
    plain = card(measured(40, 30, 10))
    assert (plain.flagged, plain.flagged_labelled, plain.flagged_unlabelled) == (40, 30, 10)
    assert (plain.true_positives, plain.false_positives, plain.false_negatives) == (10, 20, 30)
    assert (plain.precision, plain.recall, plain.flag_rate) == (10 / 30, 10 / 40, 40 / 1000)
    assert card(measured(5, 0, 0)).precision is None
    assert '"precision":null' in card(measured(5, 0, 0)).model_dump_json()


# --- The month-by-month evidence ----------------------------------------------------------
#
# `month(name, labelled, caught)` is a month in which `caught` of `labelled`
# flagged transactions are fraud; `evidence(*months)` is the evidence of the
# rule those months add up to.


def test_weak_month_fires_below_half_the_overall_precision() -> None:
    assert (WEAK_PRECISION_RATIO, MIN_EXPECTED_FRAUD) == (0.5, 5)
    # Overall 100 of 200, so half of it is 25%: 10 of 40 is at the line, 9 of 40 below it.
    at_the_line = evidence(month("2019-01", 160, 90), month("2019-02", 40, 10))
    assert at_the_line.overall.precision == 0.5
    assert codes(evidence_caveats(at_the_line)) == []
    below = evidence(month("2019-01", 160, 91), month("2019-02", 40, 9))
    assert below.overall.precision == 0.5
    caveats = evidence_caveats(below)
    assert codes(caveats) == ["weak_month"]
    assert caveats[0].message == (
        "Precision is below half the rule's overall 50.00% in 1 month: 2019-02 (9 of 40 "
        "labelled flagged were fraud). The overall figure does not describe those months."
    )


def test_weak_month_names_every_weak_month() -> None:
    found = evidence(month("2019-01", 100, 90), month("2019-02", 50, 5), month("2019-03", 50, 5))
    message = evidence_caveats(found)[0].message
    assert "in 2 months: 2019-02 (5 of 50 labelled flagged were fraud); 2019-03 (5 of 50" in message


def test_weak_month_needs_thirty_labelled_rows_in_the_month() -> None:
    # 0 of 29 is far below the overall figure and too few rows to say so.
    too_few = evidence(month("2019-01", 171, 100), month("2019-02", 29, 0))
    assert codes(evidence_caveats(too_few)) == []
    enough = evidence(month("2019-01", 170, 100), month("2019-02", 30, 0))
    assert codes(evidence_caveats(enough)) == ["weak_month"]


def test_weak_month_needs_five_expected_fraud_in_the_month() -> None:
    # Overall 10%. A month of 49 labelled rows would hold 4.9 fraud at that rate: none is chance.
    by_chance = evidence(month("2019-01", 151, 20), month("2019-02", 49, 0))
    assert by_chance.overall.precision == 0.1
    assert codes(evidence_caveats(by_chance)) == []
    expected_five = evidence(month("2019-01", 150, 20), month("2019-02", 50, 0))
    assert expected_five.overall.precision == 0.1
    assert codes(evidence_caveats(expected_five)) == ["weak_month"]


def test_no_weak_month_when_precision_is_undefined_or_zero() -> None:
    assert evidence_caveats(evidence(month("2019-01", 0, 0, flagged=7))) == ()
    assert evidence_caveats(evidence(month("2019-01", 200, 0), month("2019-02", 200, 0))) == ()
    assert evidence_caveats(evidence()) == ()


def test_period_drop_fires_when_the_later_period_is_below_half_the_earlier() -> None:
    # Earlier 50 of 100. Later 10 of 40 is exactly half that precision; 9 of 40 is below.
    # The later month is not weak by itself in the first case only.
    at_the_line = evidence(month("2019-07", 100, 50), month("2019-08", 40, 10))
    assert "period_drop" not in codes(evidence_caveats(at_the_line))
    below = evidence(month("2019-07", 100, 50), month("2019-08", 40, 9))
    caveats = {caveat.code: caveat.message for caveat in evidence_caveats(below)}
    assert caveats["period_drop"] == (
        "Precision in the later period (2019-08: 9 of 40 labelled flagged were fraud) is below "
        "half that of the earlier period (2019-07: 50 of 100). The rule did worse in the most "
        "recent months."
    )


def test_period_drop_is_about_the_periods_not_any_one_month() -> None:
    # Three later months of 20 labelled rows each: none qualifies alone, the period does.
    found = evidence(
        month("2019-06", 100, 50), month("2019-08", 20, 2), month("2019-09", 20, 2), month("2019-10", 20, 2)
    )
    caveats = evidence_caveats(found)
    assert codes(caveats) == ["period_drop"]
    assert "2019-08 to 2019-10: 6 of 60" in caveats[0].message


def test_period_drop_needs_enough_rows_in_both_periods() -> None:
    assert SMALL_SAMPLE_LABELLED == 30
    thin_later = evidence(month("2019-07", 100, 50), month("2019-08", 29, 0))
    assert evidence_caveats(thin_later) == ()
    thin_earlier = evidence(month("2019-07", 29, 29), month("2019-08", 100, 10))
    assert "period_drop" not in codes(evidence_caveats(thin_earlier))
    # Earlier 10%: 49 later rows would hold 4.9 fraud at that rate, 50 would hold 5.
    by_chance = evidence(month("2019-07", 100, 10), month("2019-08", 49, 0))
    assert "period_drop" not in codes(evidence_caveats(by_chance))
    expected_five = evidence(month("2019-07", 100, 10), month("2019-08", 50, 0))
    assert "period_drop" in codes(evidence_caveats(expected_five))
    # A later period that did better, or no later period at all, is no drop.
    assert evidence_caveats(evidence(month("2019-07", 100, 40), month("2019-08", 100, 50))) == ()
    assert evidence_caveats(evidence(month("2019-07", 100, 50))) == ()


def test_the_rule_card_carries_the_evidence_as_fields() -> None:
    found = evidence(
        month("2019-07", 100, 50, flagged=150, fraud=60),
        month("2019-08", 40, 9),
        month("2019-09", 0, 0, flagged=3),
    )
    shown_card = rule_card(found)
    assert shown_card.base_fraud_rate == 80 / 2100
    assert shown_card.lift == (59 / 140) / (80 / 2100)
    assert [row.month for row in shown_card.months] == ["2019-07", "2019-08", "2019-09"]
    july, august, september = shown_card.months
    assert (july.flagged, july.flagged_labelled, july.true_positives, july.false_positives) == (150, 100, 50, 50)
    assert (july.total_transactions, july.total_labelled, july.total_fraud) == (1000, 700, 60)
    assert (july.precision, july.recall, july.flag_rate) == (0.5, 50 / 60, 0.15)
    assert (september.precision, september.flag_rate) == (None, 0.003)
    assert shown_card.weakest_month == "2019-08"
    assert (shown_card.earlier.first_month, shown_card.earlier.last_month, shown_card.earlier.months) == (
        "2019-07", "2019-07", 1,
    )  # fmt: skip
    assert (shown_card.later.first_month, shown_card.later.last_month) == ("2019-08", "2019-09")
    assert (shown_card.later.true_positives, shown_card.later.flagged_labelled) == (9, 40)
    assert shown_card.later.precision == 9 / 40
    assert shown_card.later.lift == (9 / 40) / (20 / 1400)
    stability = shown_card.stability
    assert (stability.min_month_labelled, stability.months, stability.months_flagged) == (30, 3, 3)
    assert (stability.months_with_labelled_flags, stability.qualifying_months) == (2, 2)
    assert (stability.min_precision, stability.max_precision) == (9 / 40, 0.5)
    # Undefined values are null in JSON, and a card without months says so with empty fields.
    empty = card(measured(5, 0, 0))
    assert (empty.months, empty.weakest_month, empty.lift) == ((), None, None)
    assert empty.later.first_month is None and '"lift":null' in empty.model_dump_json()


# --- Terminal text ------------------------------------------------------------------------


def test_the_terminal_text_of_an_answer() -> None:
    result = query(("card_id", "amount_usd_cents", "city"), (100, 2500, "Rome"), (101, -5000, None))
    text = render_text(
        shown(
            table=present_table(result),
            sql="SELECT card_id, amount_usd_cents, city\nFROM somewhere",
            explanation="Two payments.",
            assumptions=("Debit only",),
            caveats=query_caveats(result, reads_labels=True),
        )
    )
    assert text.startswith("The query returned 2 rows.\n")
    assert "SQL that ran:\n    SELECT card_id, amount_usd_cents, city\n    FROM somewhere\n" in text
    assert (
        "card_id  amount_usd_cents  city\n"
        "-------  ----------------  ----\n"
        "    100            $25.00  Rome\n"
        "    101           -$50.00  NULL\n"
    ) in text
    assert "Model's explanation (not checked): Two payments." in text
    assert "Model's assumptions:\n  - Debit only" in text
    assert "Caveats:\n  - [label_coverage] " in text
    assert text.endswith(
        "[answered | tool: run_sql | claude-test | 1 model request(s) | "
        "3,300 in, 120 out tokens | 1.2 s]"
    )


def test_a_long_table_is_capped_with_a_showing_line() -> None:
    result = query(("id",), *((number,) for number in range(1, 1001)), truncated=True)
    text = render_text(shown(table=present_table(result)), max_rows=20)
    lines = text.splitlines()
    assert "20" in lines and "21" not in lines
    assert "Showing 20 of 1,000 rows." in text
    assert "Showing" not in render_text(shown(table=present_table(query(("id",), (1,), (2,)))))


def test_a_code_is_printed_without_grouping_and_a_count_with_it() -> None:
    result = query(
        ("mcc_code", "year", "zip", "user_id", "transactions"),
        (5045, 2019, 95814, 1234, 5045),
    )
    lines = render_text(shown(table=present_table(result))).splitlines()
    row = lines[lines.index("mcc_code  year    zip  user_id  transactions") + 2]
    assert row.split() == ["5045", "2019", "95814", "1234", "5,045"]


def test_an_empty_table_says_so() -> None:
    assert "id\n--\n(no rows)" in render_text(shown(table=present_table(query(("id",)))))


def test_the_rule_card_in_the_terminal() -> None:
    backtest = measured(40, 30, 10)
    text = render_text(
        shown(
            "rule_proposed",
            answer="The rule flags 40 of 1,000 transactions.",
            where_clause="amount_usd_cents > 100000",
            rule=card(backtest),
            caveats=rule_caveats(backtest),
        )
    )
    assert "Rule (a WHERE clause over transactions):\n    amount_usd_cents > 100000" in text
    for line in (
        "  Flagged               40 of 1,000 transactions (4.000%)",
        "  Labelled              30 (10 flagged have no label)",
        "  Fraud caught          10 of 40 labelled fraud",
        "  False positives       20",
        "  Precision             0.333333 (33.333%)",
        "  Recall                0.25 (25.000%)",
        "  Amount flagged        $1,234.56",
        "  False positive amount -$5.00",
    ):
        assert line in text
    undefined = render_text(shown(where_clause="id < 0", rule=card(measured(5, 0, 0))))
    assert "Precision             undefined (nothing flagged is labelled)" in undefined
    one = render_text(shown(where_clause="id = 1", rule=card(measured(2, 1, 1))))
    assert "  Labelled              1 (1 flagged has no label)" in one
    none = render_text(shown(where_clause="id = 1", rule=card(measured(1, 1, 1))))
    assert "  Labelled              1 (0 flagged have no label)" in none
    # Lift: 10 of 30 against a base rate of 40 in 700. No monthly rows, so no month section.
    assert "  Lift                  5.8 times the base fraud rate (5.714% of labelled transactions are fraud)" in text
    assert "  Lift                  undefined (no precision or no base fraud rate to compare)" in undefined
    assert "Month by month" not in text and EVIDENCE_NOTE[:40] not in text


def test_the_month_table_in_the_terminal() -> None:
    found = evidence(
        month("2019-06", 1200, 900, flagged=1500, transactions=5000, total_labelled=3500, fraud=1000),
        month("2019-07", 100, 50, flagged=150, fraud=60),
        month("2019-08", 40, 9),
        month("2019-09", 0, 0, flagged=3),
        month("2019-10"),
    )
    text = render_text(shown("rule_proposed", where_clause="id > 0", rule=rule_card(found)))
    lines = text.splitlines()
    start = lines.index("Month by month, on the same history:")
    assert lines[start - 1] == "" and lines[start - 2].startswith("  False positive amount")
    assert lines[start + 1 : start + 13] == [
        "  Month    Flagged  Labelled  Fraud caught  False positives  Precision",
        "  2019-06    1,500     1,200           900              300     75.00%",
        "  2019-07      150       100            50               50     50.00%",
        "  2019-08       40        40             9               31     22.50%",
        "  2019-09        3         0             0                0          -",
        "  2019-10        0         0             0                0          -",
        "  Weakest month         2019-08: 9 of 40 labelled flagged were fraud (22.50%)",
        "  Monthly precision     22.50% to 75.00% over the 3 months with 30 or more labelled flagged, of 5",
        "  Earlier period        2019-06 to 2019-07: 950 of 1,300 labelled flagged were fraud (73.08%); "
        "recall 950 of 1,060; lift 2.9",
        "  Later period          2019-08 to 2019-10: 9 of 40 labelled flagged were fraud (22.50%); "
        "recall 9 of 30; lift 15.8",
        "  The rule is scored here on the same history it was written from, so this is a stability check, not",
        "  a test on unseen data. Only new data or a shadow run can show that the rule holds.",
    ]
    assert " ".join(line.strip() for line in lines[start + 11 : start + 13]) == EVIDENCE_NOTE
    assert "not a test on unseen data" in EVIDENCE_NOTE and "held" not in EVIDENCE_NOTE.replace("holds", "")


def test_the_month_section_says_when_nothing_can_be_compared() -> None:
    thin = evidence(month("2019-07", 29, 20), month("2019-08", 0, 0, flagged=2))
    text = render_text(shown("rule_proposed", where_clause="id > 0", rule=rule_card(thin)))
    assert "  Weakest month         none: no month has 30 or more labelled flagged transactions" in text
    assert "Monthly precision" not in text
    assert "  Later period          2019-08: nothing flagged is labelled" in text
    one = evidence(month("2019-07", 30, 20))
    alone = render_text(shown("rule_proposed", where_clause="id > 0", rule=rule_card(one)))
    assert "over the 1 month with 30 or more labelled flagged, of 1" in alone
    assert "  Later period          no month of the data: nothing flagged is labelled" in alone


def test_the_footer_names_the_tool_and_what_was_refused() -> None:
    repaired = shown(refused=("no_tool_call", "table_not_allowed"))
    assert "[answered | tool: run_sql | refused: no_tool_call, table_not_allowed | " in (
        render_text(repaired)
    )
    no_reply = shown("failed", tool=None, answer="No reply.")
    assert "[failed | tool: none | claude-test | " in render_text(no_reply)


def test_a_run_time_failure_shows_sqlites_words_escaped_on_their_own_line() -> None:
    failed = shown(
        "failed",
        answer="The query passed the checks but failed while it was running.",
        error=TurnError(
            kind="sql_execution",
            message="The query passed the checks but failed while it was running.",
            detail="bad JSON path: '1 Elm\x1b[2J Street'",
        ),
    )
    lines = render_text(failed).splitlines()
    assert lines[0] == (
        "Failed (sql_execution): The query passed the checks but failed while it was running."
    )
    assert lines[1] == "SQLite said: bad JSON path: '1 Elm\\x1b[2J Street'"


def test_a_wide_table_shows_the_columns_that_fit_and_names_the_rest() -> None:
    names = tuple(f"col_{number}" for number in range(60))
    wide = query(("long", *names), ("x" * 300, *range(60)))
    lines = render_text(shown(table=present_table(wide))).splitlines()
    header = next(line for line in lines if line.startswith("long"))
    table = lines[lines.index(header) : lines.index(header) + 3]
    assert all(len(line) <= 160 for line in table)
    assert "col_0" in header and "col_59" not in header
    note = lines[lines.index(header) + 3]
    assert " more column(s) not shown: " in note and note.endswith("…")
    assert len(note) < 200
    shown_columns = len(header.split())
    assert note.startswith(f"{61 - shown_columns} more column(s) not shown: col_{shown_columns - 1},")

    narrow = render_text(shown(table=present_table(query(("id", "name"), (1, "a")))))
    assert "not shown" not in narrow


def test_a_clarification_a_refusal_and_a_failure_in_the_terminal() -> None:
    asked = shown(
        "clarification", answer="What is risky?", answer_source="model", suggestions=("a", "b")
    )
    assert render_text(asked).startswith("The assistant asks: What is risky?\nYou could ask:\n  - a")
    declined = shown("cannot_answer", answer="No email column.", answer_source="model")
    assert render_text(declined).startswith("The assistant declined: No email column.")
    failed = shown(
        "rejected",
        answer="The query was not allowed to run.",
        error=TurnError(kind="sql_rejected", message="The query was not allowed to run."),
    )
    assert render_text(failed).startswith(
        "Rejected (sql_rejected): The query was not allowed to run."
    )


def test_model_text_cannot_move_the_cursor() -> None:
    text = render_text(
        shown(explanation="fine\x1b[2J", assumptions=("a\rb",), sql="SELECT '\x1b[31m'")
    )
    assert "\x1b" not in text and "\r" not in text
