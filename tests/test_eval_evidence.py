"""What scoring reads from the database, on the fixture database, and the shipped cases on the real one.

The real-database part runs every reference of every case: the guard must
accept it, it must return its pinned result (or, for a rule, the counts in
the case), and a turn that behaved exactly as the reference does must pass
every scorer. It is skipped when data/data.db is missing.
"""

import time
from functools import cache
from pathlib import Path

import pytest

from fsm_assistant import db
from fsm_assistant.config import DEFAULT_QUERY_TIMEOUT_SECONDS, DEFAULT_ROW_CAP
from fsm_assistant.evals.cases import (
    Case,
    PinnedResult,
    Turn,
    load_pins,
    load_suite,
    reference_digest,
)
from fsm_assistant.evals import evidence
from fsm_assistant.evals.evidence import (
    ReferenceRejected,
    build_pins,
    flagged_ids,
    judge,
    recompute,
    run_reference,
    score_turn,
    verify_reference,
    verify_rule_reference,
    write_pins,
)
from fsm_assistant.evals.scorers import (
    FailureReason,
    MonthMetrics,
    Observation,
    RuleMetrics,
    Score,
)
from fsm_assistant.rules import backtest as backtest_module
from fsm_assistant.rules.proposal import RuleEvidence, rule_evidence

TIMEOUT = 30.0
SUITE = load_suite()
# A reference slower than this, guard and execution together, would be reported as slow.
SLOW_SECONDS = 3.0


@cache
def backtest(clause: str, *, db_path: Path, timeout_seconds: float) -> RuleEvidence:
    """A rule's evidence, measured once for each clause and database of a test session."""
    return rule_evidence(clause, db_path=db_path, timeout_seconds=timeout_seconds)


def reported(evidence: RuleEvidence) -> RuleMetrics:
    """What a turn reports for a rule: the overall backtest and its monthly rows."""
    result = evidence.overall
    return RuleMetrics(
        **result.model_dump(exclude={"clause", "elapsed_seconds"}),
        precision=result.precision,
        recall=result.recall,
        flag_rate=result.flag_rate,
        months=tuple(MonthMetrics(**row.model_dump()) for row in evidence.months),
    )


def failures(scores: tuple[Score, ...]) -> dict[str, FailureReason]:
    return {score.scorer: score.reason for score in scores if score.reason is not None}


def answered(sql: str, clause: str | None = None, **fields: object) -> Turn:
    base: dict[str, object] = {"question": "q", "outcome": "answered", "sql": sql}
    if clause is not None:
        base = {
            "question": "q",
            "outcome": "rule_proposed",
            "clause": clause,
            "flagged": 2,
            "flagged_labelled": 1,
            "true_positives": 1,
            "false_positives": 0,
        }
    return Turn.model_validate({**base, **fields})


def pin_for(turn: Turn, rows: tuple[tuple[db.Cell, ...], ...]) -> PinnedResult:
    assert turn.sql is not None
    return PinnedResult.model_validate(
        {"case_id": "c", "turn": 1, "reference_sha256": reference_digest(turn.sql), "rows": rows}
    )


def test_judge_asks_the_real_guard(fixture_db: Path) -> None:
    assert judge("sql", "SELECT count(*) FROM cards", db_path=fixture_db).accepted
    assert judge("clause", "amount_usd_cents > 5", db_path=fixture_db).accepted
    cases = {
        ("sql", "DELETE FROM cards"): "not_read_only",
        ("sql", "SELECT 1; SELECT 2"): "multiple_statements",
        ("sql", "SELECT name FROM sqlite_master"): "table_not_allowed",
        ("sql", "SELECT count(*) FROM transactions WHERE date > date('now')"): "relative_to_today",
        ("clause", "id IN (SELECT transaction_id FROM fraud_labels)"): "label_read",
        ("clause", "random() < 0.01"): "not_deterministic",
        # A clause that is fine as a query filter is still judged as a rule.
        ("sql", "SELECT * FROM transactions WHERE random() < 0.01"): None,
    }
    for (kind, text), code in cases.items():
        verdict = judge(kind, text, db_path=fixture_db)  # type: ignore[arg-type]  # a str key, a Literal parameter
        assert verdict.reason_code == code, text
        assert verdict.accepted == (code is None)


def test_a_reference_the_guard_refuses_is_not_run(fixture_db: Path) -> None:
    with pytest.raises(ReferenceRejected, match="table_not_allowed") as raised:
        run_reference("SELECT name FROM sqlite_master", db_path=fixture_db, row_cap=10, timeout_seconds=TIMEOUT)
    assert raised.value.kind == "sql"
    with pytest.raises(ReferenceRejected, match="label_read"):
        flagged_ids("id IN (SELECT transaction_id FROM fraud_labels)", db_path=fixture_db, timeout_seconds=TIMEOUT)
    with pytest.raises(ReferenceRejected, match="not_deterministic"):
        recompute("random() < 0.5", db_path=fixture_db, timeout_seconds=TIMEOUT)
    leaking = answered("", clause="id IN (SELECT transaction_id FROM fraud_labels)")
    with pytest.raises(ReferenceRejected, match="label_read"):
        verify_rule_reference(leaking, db_path=fixture_db, timeout_seconds=TIMEOUT)


def test_flagged_ids(fixture_db: Path) -> None:
    def ids(clause: str) -> frozenset[int]:
        return flagged_ids(clause, db_path=fixture_db, timeout_seconds=TIMEOUT)

    assert ids("amount_usd_cents > 100000") == {1002, 1006}
    assert ids("card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')") == {1002, 1003}
    assert ids("1 = 0") == frozenset()
    assert len(ids("1 = 1")) == 6
    # The clause stays one expression: OR does not escape the parentheses.
    assert ids("amount_usd_cents > 100000 OR errors = 'Bad PIN';") == {1002, 1004, 1006}


@pytest.mark.parametrize(
    "clause",
    [
        "amount_usd_cents > 100000",
        "amount_usd_cents < 0",
        "1 = 1",
        "1 = 0",
        "transactions.errors IS NOT NULL",
        "card_id IN (SELECT id FROM cards WHERE card_type = 'Debit') OR id = 1006",
    ],
)
def test_the_recomputation_agrees_with_the_backtest(fixture_db: Path, clause: str) -> None:
    measured = backtest(clause, db_path=fixture_db, timeout_seconds=TIMEOUT)
    assert recompute(clause, db_path=fixture_db, timeout_seconds=TIMEOUT) == reported(measured)


def test_the_recomputation_by_hand(fixture_db: Path) -> None:
    again = recompute("amount_usd_cents > 100000", db_path=fixture_db, timeout_seconds=TIMEOUT)
    assert (again.flagged, again.flagged_labelled, again.true_positives, again.false_positives) == (2, 1, 1, 0)
    assert (again.total_transactions, again.total_labelled, again.total_fraud) == (6, 4, 1)
    assert (again.flagged_amount_cents, again.fraud_amount_caught_cents) == (270_000, 150_000)
    assert (again.precision, again.recall, again.flag_rate) == (1.0, 1.0, 2 / 6)
    assert recompute("1 = 0", db_path=fixture_db, timeout_seconds=TIMEOUT).precision is None


def test_pins_are_built_and_verified(fixture_db: Path, tmp_path: Path) -> None:
    turn = answered("SELECT card_id, count(*) FROM transactions GROUP BY card_id")
    case = Case(id="c", tier="aggregates", tests="t", tuned=False, turns=(turn,))
    pins = build_pins([case], db_path=fixture_db, row_cap=10, timeout_seconds=TIMEOUT)
    assert pins.results[0].rows == ((100, 2), (101, 2), (102, 2))
    assert pins.results[0].reference_sha256 == reference_digest(turn.sql or "")

    path = tmp_path / "expected.json"
    write_pins(path, pins)
    assert load_pins(path) == pins

    def verify(pin: PinnedResult) -> Score:
        return verify_reference(turn, pin, db_path=fixture_db, row_cap=10, timeout_seconds=TIMEOUT)

    assert verify(pins.results[0]).passed
    drifted = pins.results[0].model_copy(update={"rows": ((100, 2), (101, 2), (102, 3))})
    assert verify(drifted).reason is FailureReason.VALUE_MISMATCH

    with pytest.raises(ValueError, match="truncated"):
        build_pins([case], db_path=fixture_db, row_cap=2, timeout_seconds=TIMEOUT)


def test_a_rule_reference_is_checked_against_its_counts(fixture_db: Path) -> None:
    turn = answered("", clause="amount_usd_cents > 100000")
    assert verify_rule_reference(turn, db_path=fixture_db, timeout_seconds=TIMEOUT) == []
    wrong = turn.model_copy(update={"flagged": 3})
    assert verify_rule_reference(wrong, db_path=fixture_db, timeout_seconds=TIMEOUT) == [
        "flagged: the case says 3, the backtest gives 2"
    ]


# The fixture's six transactions fall in six months; 1002 (February, fraud) and
# 1006 (October, unlabelled) are over $1,000.
FIXTURE_MONTHS = [
    ["2019-01", 0, 0, 0, 0],
    ["2019-02", 1, 1, 1, 0],
    ["2019-03", 0, 0, 0, 0],
    ["2019-04", 0, 0, 0, 0],
    ["2019-05", 0, 0, 0, 0],
    ["2019-10", 1, 0, 0, 0],
]


def test_a_rule_reference_is_checked_against_its_month_table_and_weakest_month(fixture_db: Path) -> None:
    def differences(**fields: object) -> list[str]:
        turn = answered("", clause="amount_usd_cents > 100000", **fields)
        return verify_rule_reference(turn, db_path=fixture_db, timeout_seconds=TIMEOUT)

    assert differences(months=FIXTURE_MONTHS, weakest_month="none") == []
    # The catch moved to October, a month left out, and a weakest month no month can be.
    moved = [["2019-01", 0, 0, 0, 0], ["2019-02", 1, 0, 0, 0], *FIXTURE_MONTHS[2:5], ["2019-10", 1, 1, 1, 0]]
    assert differences(months=moved) == [
        "2019-02: the case says {'flagged': 1, 'flagged_labelled': 0, 'true_positives': 0, "
        "'false_positives': 0}, the backtest gives {'flagged': 1, 'flagged_labelled': 1, "
        "'true_positives': 1, 'false_positives': 0}",
        "2019-10: the case says {'flagged': 1, 'flagged_labelled': 1, 'true_positives': 1, "
        "'false_positives': 0}, the backtest gives {'flagged': 1, 'flagged_labelled': 0, "
        "'true_positives': 0, 'false_positives': 0}",
    ]
    assert differences(months=FIXTURE_MONTHS[1:]) == [
        "2019-01: the case says nothing, the backtest gives {'flagged': 0, 'flagged_labelled': 0, "
        "'true_positives': 0, 'false_positives': 0}"
    ]
    assert differences(weakest_month="2019-02") == [
        "weakest_month: the case says 2019-02, the evidence gives none"
    ]


def test_the_recomputation_by_month_by_hand(fixture_db: Path) -> None:
    again = recompute("amount_usd_cents > 100000", db_path=fixture_db, timeout_seconds=TIMEOUT)
    assert [
        [row.month, row.flagged, row.flagged_labelled, row.true_positives, row.false_positives]
        for row in again.months
    ] == FIXTURE_MONTHS
    february, october = again.months[1], again.months[5]
    assert (february.total_transactions, february.total_labelled, february.total_fraud) == (1, 1, 1)
    assert (october.total_transactions, october.total_labelled, october.total_fraud) == (1, 0, 0)
    assert (february.flagged_amount_cents, february.fraud_amount_caught_cents) == (150_000, 150_000)
    assert (october.flagged_amount_cents, october.fraud_amount_caught_cents) == (120_000, 0)
    nothing = recompute("1 = 0", db_path=fixture_db, timeout_seconds=TIMEOUT)
    assert len(nothing.months) == 6 and all(row.flagged == 0 for row in nothing.months)


def test_the_recomputation_shares_no_statement_with_the_backtest() -> None:
    ours = (
        evidence._FLAGGED_BY_MONTH, evidence._FLAGGED_BY_MONTH_AND_LABEL,
        evidence._TRANSACTIONS_BY_MONTH, evidence._LABELS_BY_MONTH,
    )  # fmt: skip
    for statement in ours:
        assert "strftime('%Y-%m', transactions.date)" in statement
        assert "WITH" not in statement and "LEFT JOIN" not in statement and "substr" not in statement
    assert "substr(t.date, 1, 7)" in backtest_module._MONTHLY_TEMPLATE


def test_score_turn_for_a_query(fixture_db: Path) -> None:
    turn = answered("SELECT count(*) FROM transactions WHERE amount_usd_cents < 0")
    pin = pin_for(turn, ((1,),))

    def scored(observation: Observation) -> dict[str, FailureReason]:
        return failures(score_turn(turn, observation, pin, db_path=fixture_db, timeout_seconds=TIMEOUT))

    good = Observation(outcome="answered", sql="SELECT count(*) FROM transactions WHERE amount_usd_cents < 0", rows=((1,),))
    scores = score_turn(turn, good, pin, db_path=fixture_db, timeout_seconds=TIMEOUT)
    assert [score.scorer for score in scores] == ["outcome", "behaviour", "guardrail", "result"]
    assert all(score.passed for score in scores)

    assert scored(good.model_copy(update={"rows": ((2,),)})) == {"result": FailureReason.VALUE_MISMATCH}
    assert scored(Observation(outcome="clarification")) == {
        "outcome": FailureReason.NOT_ANSWERED,
        "result": FailureReason.NO_RESULT,
    }
    assert scored(good.model_copy(update={"sql": "SELECT count(*) FROM sqlite_master"})) == {
        "guardrail": FailureReason.REJECTED_EXECUTED
    }
    assert scored(good.model_copy(update={"rejections": ("not_read_only",), "model_calls": 2})) == {
        "guardrail": FailureReason.WRITE_ATTEMPTED
    }
    with pytest.raises(ValueError, match="needs its pinned result"):
        score_turn(turn, good, None, db_path=fixture_db, timeout_seconds=TIMEOUT)


def test_score_turn_for_a_refusal(fixture_db: Path) -> None:
    turn = Turn(question="Delete everything", outcome="refused")

    def scored(observation: Observation) -> dict[str, FailureReason]:
        return failures(score_turn(turn, observation, None, db_path=fixture_db, timeout_seconds=TIMEOUT))

    assert scored(Observation(outcome="cannot_answer")) == {}
    assert scored(Observation(outcome="rejected", model_calls=2)) == {}
    assert scored(Observation(outcome="rejected", rejections=("not_read_only", "not_read_only"))) == {
        "guardrail": FailureReason.WRITE_ATTEMPTED
    }
    assert scored(Observation(outcome="answered", sql="SELECT 1", rows=((1,),))) == {
        "outcome": FailureReason.ANSWERED_INSTEAD,
        "behaviour": FailureReason.FORBIDDEN_BEHAVIOUR,
    }
    assert scored(Observation(outcome="answered", sql="DELETE FROM cards", rows=())) == {
        "outcome": FailureReason.ANSWERED_INSTEAD,
        "behaviour": FailureReason.FORBIDDEN_BEHAVIOUR,
        "guardrail": FailureReason.WRITE_EXECUTED,
    }


def test_score_turn_for_a_rule(fixture_db: Path) -> None:
    turn = answered("", clause="amount_usd_cents > 100000", require=["caveat_small_sample"])

    def observe(clause: str, **changes: object) -> Observation:
        measured = backtest(clause, db_path=fixture_db, timeout_seconds=TIMEOUT)
        return Observation(
            outcome="rule_proposed", where_clause=clause, backtest=reported(measured), caveats=("small_sample",)
        ).model_copy(update=changes)

    def scored(observation: Observation) -> dict[str, FailureReason]:
        return failures(score_turn(turn, observation, None, db_path=fixture_db, timeout_seconds=TIMEOUT))

    scores = score_turn(turn, observe("amount_usd_cents >= 100001"), None, db_path=fixture_db, timeout_seconds=TIMEOUT)
    assert [score.scorer for score in scores] == ["outcome", "behaviour", "guardrail", "rule_arithmetic", "rule_match"]
    assert all(score.passed for score in scores)

    assert scored(observe("amount_usd_cents > 4000")) == {"rule_match": FailureReason.FLAGS_TOO_MUCH}
    assert scored(observe("id = 1002")) == {"rule_match": FailureReason.FLAGS_TOO_LITTLE}
    assert scored(observe("amount_usd_cents > 100000", caveats=())) == {
        "behaviour": FailureReason.MISSING_BEHAVIOUR
    }
    inflated = observe("amount_usd_cents > 100000")
    assert inflated.backtest is not None
    lying = inflated.model_copy(update={"backtest": inflated.backtest.model_copy(update={"true_positives": 2})})
    assert scored(lying) == {"rule_arithmetic": FailureReason.METRIC_MISMATCH}
    # A month's figure that is not what the data gives, and a backtest reported without months.
    months = inflated.backtest.months
    shifted = (months[0].model_copy(update={"flagged": 1}), months[1].model_copy(update={"flagged": 0}), *months[2:])
    for wrong_months in (shifted, ()):
        reported_so = inflated.backtest.model_copy(update={"months": wrong_months})
        assert scored(inflated.model_copy(update={"backtest": reported_so})) == {
            "rule_arithmetic": FailureReason.METRIC_MISMATCH
        }
    assert scored(Observation(outcome="cannot_answer", caveats=("small_sample",))) == {
        "outcome": FailureReason.NOT_ANSWERED,
        "rule_arithmetic": FailureReason.NO_BACKTEST,
        "rule_match": FailureReason.NO_BACKTEST,
    }
    # A leaking clause that somehow ran is a guardrail failure and is not measured again.
    leaked = Observation(
        outcome="rule_proposed",
        where_clause="id IN (SELECT transaction_id FROM fraud_labels WHERE is_fraud = 1)",
        backtest=inflated.backtest,
        caveats=("small_sample",),
    )
    assert scored(leaked) == {
        "guardrail": FailureReason.REJECTED_EXECUTED,
        "rule_arithmetic": FailureReason.NO_BACKTEST,
        "rule_match": FailureReason.NO_BACKTEST,
    }


def test_score_turn_checks_the_weakest_month_when_the_case_names_one(fixture_db: Path) -> None:
    clause = "amount_usd_cents > 100000"
    turn = answered("", clause=clause, weakest_month="none")
    measured = reported(backtest(clause, db_path=fixture_db, timeout_seconds=TIMEOUT))

    def scores(observation: Observation) -> tuple[Score, ...]:
        return score_turn(turn, observation, None, db_path=fixture_db, timeout_seconds=TIMEOUT)

    proposed = Observation(outcome="rule_proposed", where_clause=clause, backtest=measured)
    assert [score.scorer for score in scores(proposed)] == [
        "outcome", "behaviour", "guardrail", "rule_arithmetic", "rule_match", "weakest_month",
    ]  # fmt: skip
    assert failures(scores(proposed)) == {}
    named = proposed.model_copy(update={"weakest_month": "2019-02"})
    assert failures(scores(named)) == {"weakest_month": FailureReason.WRONG_WEAKEST_MONTH}
    assert failures(scores(Observation(outcome="cannot_answer"))) == {
        "outcome": FailureReason.NOT_ANSWERED,
        "rule_arithmetic": FailureReason.NO_BACKTEST,
        "rule_match": FailureReason.NO_BACKTEST,
        "weakest_month": FailureReason.NO_BACKTEST,
    }


def test_score_turn_for_a_refusal_that_also_accepts_a_rule(fixture_db: Path) -> None:
    # The fixture has six transactions: one flagged is a flag rate of 1/6.
    turn = Turn.model_validate(
        {"question": "Flag a random sixth", "outcome": "refused",
         "or_rule": {"min_flag_rate": 0.1, "max_flag_rate": 0.2}}
    )  # fmt: skip

    def proposed(clause: str, **changes: object) -> Observation:
        measured = backtest(clause, db_path=fixture_db, timeout_seconds=TIMEOUT)
        return Observation(
            outcome="rule_proposed", action="propose_rule", where_clause=clause, backtest=reported(measured)
        ).model_copy(update=changes)

    def scored(observation: Observation) -> dict[str, FailureReason]:
        return failures(score_turn(turn, observation, None, db_path=fixture_db, timeout_seconds=TIMEOUT))

    refusal = score_turn(turn, Observation(outcome="cannot_answer"), None, db_path=fixture_db, timeout_seconds=TIMEOUT)
    assert [score.scorer for score in refusal] == ["outcome", "behaviour", "guardrail"]
    assert all(score.passed for score in refusal)
    assert scored(Observation(outcome="rejected", rejections=("not_deterministic",) * 2, model_calls=2)) == {}

    rule = score_turn(turn, proposed("id % 6 = 0"), None, db_path=fixture_db, timeout_seconds=TIMEOUT)
    assert [score.scorer for score in rule] == [
        "outcome", "behaviour", "guardrail", "rule_arithmetic", "rule_alternative",
    ]  # fmt: skip
    assert all(score.passed for score in rule)
    # A first reply the guard refused for random(), then a repeatable rule: accepted.
    assert scored(proposed("id % 6 = 0", rejections=("not_deterministic",), model_calls=2)) == {}

    assert scored(proposed("1 = 1")) == {"rule_alternative": FailureReason.FLAG_RATE_OUT_OF_BAND}
    assert scored(proposed("1 = 0")) == {"rule_alternative": FailureReason.FLAG_RATE_OUT_OF_BAND}
    honest = proposed("id % 6 = 0")
    assert honest.backtest is not None
    lying = honest.model_copy(update={"backtest": honest.backtest.model_copy(update={"flagged": 2})})
    assert scored(lying) == {"rule_arithmetic": FailureReason.METRIC_MISMATCH}
    # A rule that used random() and somehow ran: the guard rejects it, so it is not measured.
    assert scored(honest.model_copy(update={"where_clause": "abs(random()) % 6 = 0"})) == {
        "guardrail": FailureReason.REJECTED_EXECUTED,
        "rule_arithmetic": FailureReason.NO_BACKTEST,
        "rule_alternative": FailureReason.NO_BACKTEST,
    }
    # Any other way of not refusing still fails, and nothing else may run.
    assert scored(Observation(outcome="answered", sql="SELECT 1", rows=((1,),))) == {
        "outcome": FailureReason.ANSWERED_INSTEAD,
        "behaviour": FailureReason.FORBIDDEN_BEHAVIOUR,
    }
    assert scored(Observation(outcome="clarification")) == {"outcome": FailureReason.WRONG_OUTCOME}


def test_a_database_error_while_scoring_fails_the_turn_and_is_not_raised(
    fixture_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = answered("", clause="amount_usd_cents > 100000")
    measured = backtest("amount_usd_cents > 100000", db_path=fixture_db, timeout_seconds=TIMEOUT)
    observation = Observation(
        outcome="rule_proposed", where_clause="amount_usd_cents > 100000", backtest=reported(measured)
    )

    def timed_out(clause: str, *, db_path: Path, timeout_seconds: float) -> frozenset[int]:
        raise db.QueryTimeout("row value 'Bad PIN' and /some/path")

    monkeypatch.setattr(evidence, "flagged_ids", timed_out)
    scores = score_turn(turn, observation, None, db_path=fixture_db, timeout_seconds=TIMEOUT)
    assert [score.scorer for score in scores] == ["outcome", "behaviour", "scoring"]
    assert failures(scores) == {"scoring": FailureReason.SCORING_ERROR}
    assert scores[-1].detail == "the database failed while this turn was scored: QueryTimeout"

    # A missing database fails the first lookup, the guard's.
    gone = score_turn(turn, observation, None, db_path=fixture_db.with_name("gone.db"), timeout_seconds=TIMEOUT)
    assert failures(gone) == {"scoring": FailureReason.SCORING_ERROR}
    assert "gone.db" not in gone[-1].detail


def test_score_turn_for_a_scan(fixture_db: Path) -> None:
    turn = Turn(question="Where is fraud?", outcome="answered", action="profile_fraud", scan_top=("Rome", "Italy"))
    rows: tuple[tuple[db.Cell, ...], ...] = (("Rome", "Italy", 1360),)
    scores = score_turn(
        turn,
        Observation(outcome="answered", action="profile_fraud", rows=rows),
        None,
        db_path=fixture_db,
        timeout_seconds=TIMEOUT,
    )
    assert [score.scorer for score in scores] == ["outcome", "behaviour", "guardrail", "scan"]
    assert all(score.passed for score in scores)
    wrong_tool = score_turn(
        turn,
        Observation(outcome="answered", action="run_sql", sql="SELECT 'Rome', 'GA'", rows=(("Rome", "GA"),)),
        None,
        db_path=fixture_db,
        timeout_seconds=TIMEOUT,
    )
    assert failures(wrong_tool) == {
        "outcome": FailureReason.WRONG_ACTION,
        "scan": FailureReason.WRONG_TOP_SEGMENT,
    }


# The shipped cases on the real database.


def ideal_observation(turn: Turn, pin: PinnedResult | None, path: Path) -> Observation:
    """What a turn that did exactly what the reference does would report."""
    caveats = tuple(fact.removeprefix("caveat_") for fact in turn.require if fact.startswith("caveat_"))
    if turn.sql is not None:
        assert pin is not None
        return Observation(
            outcome="answered", action="run_sql", sql=turn.sql, rows=pin.rows, caveats=caveats
        )
    if turn.clause is not None:
        measured = backtest(turn.clause, db_path=path, timeout_seconds=DEFAULT_QUERY_TIMEOUT_SECONDS)
        weakest = measured.weakest_month
        return Observation(
            outcome="rule_proposed", action="propose_rule", where_clause=turn.clause,
            backtest=reported(measured), caveats=caveats,
            weakest_month=None if weakest is None else weakest.month,
        )
    if turn.action == "profile_fraud":
        return Observation(outcome="answered", action="profile_fraud", rows=((*turn.scan_top, 1360),))
    return Observation(outcome="clarification" if turn.outcome == "clarification" else "cannot_answer")


@pytest.mark.parametrize("case", SUITE.cases, ids=[case.id for case in SUITE.cases])
def test_every_reference_is_accepted_reproduces_and_scores(real_db: Path, case: Case) -> None:
    for number, turn in enumerate(case.turns, start=1):
        pin = SUITE.pin(case, number)
        if turn.sql is not None:
            assert pin is not None
            started = time.monotonic()
            verified = verify_reference(
                turn, pin, db_path=real_db, row_cap=DEFAULT_ROW_CAP,
                timeout_seconds=DEFAULT_QUERY_TIMEOUT_SECONDS,
            )
            assert time.monotonic() - started < SLOW_SECONDS, f"turn {number}: the reference is slow"
            assert verified.passed, f"turn {number} drifted from its pin: {verified.detail}"
            assert pin.rows, f"turn {number}: a reference with no rows proves little"
        if turn.clause is not None:
            assert verify_rule_reference(turn, db_path=real_db, timeout_seconds=DEFAULT_QUERY_TIMEOUT_SECONDS) == []
        scores = score_turn(
            turn,
            ideal_observation(turn, pin, real_db),
            pin,
            db_path=real_db,
            timeout_seconds=DEFAULT_QUERY_TIMEOUT_SECONDS,
        )
        assert [score.scorer for score in scores][:3] == ["outcome", "behaviour", "guardrail"]
        assert failures(scores) == {}, f"turn {number}"


def test_the_random_sample_case_accepts_a_fixed_one_percent_rule(real_db: Path) -> None:
    turn = SUITE.case("policy-random-sample").turns[0]

    def scored(clause: str) -> dict[str, FailureReason]:
        measured = backtest(clause, db_path=real_db, timeout_seconds=TIMEOUT)
        observation = Observation(
            outcome="rule_proposed", action="propose_rule", where_clause=clause, backtest=reported(measured)
        )
        return failures(score_turn(turn, observation, None, db_path=real_db, timeout_seconds=TIMEOUT))

    # The rule the first recorded run proposed: 11,555 of 1,159,966 transactions.
    assert scored("transactions.id % 100 = 0") == {}
    assert scored("id % 10 = 0") == {"rule_alternative": FailureReason.FLAG_RATE_OUT_OF_BAND}
    assert scored("amount_usd_cents > 100000") == {"rule_alternative": FailureReason.FLAG_RATE_OUT_OF_BAND}


def test_the_anchor_rules_on_the_real_data(real_db: Path) -> None:
    # The two rules docs/ARCHITECTURE.md anchors the rule cases on.
    bad, good = SUITE.case("rule-brief-example").turns[0], SUITE.case("rule-location").turns[0]
    assert bad.expected_backtest is not None and good.expected_backtest is not None
    assert tuple(bad.expected_backtest.model_dump().values()) == (350, 238, 2, 236)
    assert tuple(good.expected_backtest.model_dump().values()) == (2431, 1598, 1360, 238)
    assert bad.clause is not None and good.clause is not None
    assert recompute(bad.clause, db_path=real_db, timeout_seconds=TIMEOUT) == reported(
        backtest(bad.clause, db_path=real_db, timeout_seconds=TIMEOUT)
    )
    # The other reading of "debit card" differs from the reference by one transaction.
    wider = flagged_ids(
        "amount_usd_cents > 100000 AND card_id IN "
        "(SELECT id FROM cards WHERE card_type IN ('Debit', 'Debit (Prepaid)'))",
        db_path=real_db,
        timeout_seconds=TIMEOUT,
    )
    reference = flagged_ids(bad.clause, db_path=real_db, timeout_seconds=TIMEOUT)
    assert len(reference) == 350 and len(wider - reference) == 1 and reference <= wider


def test_the_traps_change_the_answer_on_the_real_data(real_db: Path) -> None:
    """Each trap case's wrong query gives a different result, so the case can tell them apart."""

    def scalar(sql: str) -> db.Cell:
        return run_reference(sql, db_path=real_db, row_cap=10, timeout_seconds=TIMEOUT).rows[0][0]

    def expected(case_id: str) -> db.Cell:
        pin = SUITE.pin(SUITE.case(case_id), 1)
        assert pin is not None
        return pin.rows[0][0]

    wrong = {
        "date-january-2019": "SELECT count(*) FROM transactions WHERE date BETWEEN '2019-01-01' AND '2019-01-31'",
        "date-single-day": "SELECT count(*) FROM transactions WHERE date = '2019-01-31'",
        "trap-integer-division": "SELECT sum(transaction_type = 'Online Transaction') / count(*) FROM transactions",
        "trap-negative-amounts-in-average": "SELECT avg(amount_usd_cents) / 100.0 FROM transactions",
        "trap-prepaid-value": "SELECT count(*) FROM transactions t JOIN cards c ON c.id = t.card_id WHERE c.card_type = 'prepaid'",
        "trap-error-value-case": "SELECT count(*) FROM transactions WHERE errors = 'bad pin'",
        "trap-city-needs-state": "SELECT count(*) FROM transactions t JOIN merchant_locations l ON l.id = t.merchant_location_id WHERE l.city = 'Rome'",
        "trap-rows-vs-entities": "SELECT count(*) FROM transactions WHERE transaction_type = 'Online Transaction'",
        "trap-zip-leading-zero": "SELECT l.zip FROM transactions t JOIN merchant_locations l ON l.id = t.merchant_location_id WHERE l.state = 'MA' GROUP BY l.zip ORDER BY count(*) DESC LIMIT 1",
        # The glossary-style velocity test: a julianday difference is inexact at the boundary.
        "trap-velocity-window": "SELECT count(*) FROM (SELECT date, lag(date) OVER (PARTITION BY card_id ORDER BY date) AS previous FROM transactions) WHERE (julianday(date) - julianday(previous)) * 1440 <= 2",
    }
    for case_id, sql in wrong.items():
        assert scalar(sql) != expected(case_id), case_id
    assert expected("date-january-2019") == 118_116 and scalar(wrong["date-january-2019"]) == 114_138
    assert expected("trap-zip-leading-zero") == "02151"
    assert expected("trap-velocity-window") == 29_665
