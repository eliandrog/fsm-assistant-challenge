"""rule_bank: the file is made on first use, holds no rule twice, keeps decisions, and is never the database."""

import shutil
import sqlite3
from pathlib import Path

import pytest
from conftest import _month, file_sha256

from fsm_assistant.presentation import rule_card
from fsm_assistant.result import RuleCard
from fsm_assistant.rule_bank import (
    BankError,
    DuplicateClause,
    EvidenceStatus,
    NewEntry,
    RuleBank,
    clause_key,
)
from fsm_assistant.rules.backtest import BacktestResult, MonthlyBacktest
from fsm_assistant.rules.proposal import build_evidence

CLAUSE = "amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')"
CLOCK = "2000-01-01T00:00:00+00:00"


def card(clause: str, flagged: int = 500, lift_rows: int = 200) -> RuleCard:
    months = (_month("2019-03", flagged, flagged - 50, lift_rows), _month("2019-09", 40, 30, 10))
    totals = {
        name: sum(getattr(row, name) for row in months)
        for name in MonthlyBacktest.model_fields
        if name != "month"
    }
    return rule_card(
        build_evidence(BacktestResult(clause=clause, elapsed_seconds=0.1, **totals), months)
    )


def entry(
    run_id: int,
    *,
    clause: str | None = CLAUSE,
    status: EvidenceStatus = "confirmed",
    title: str = "Large debit payments",
    flagged: int = 500,
    caught: int = 200,
) -> NewEntry:
    return NewEntry(
        run_id=run_id,
        position=1,
        title=title,
        hypothesis="A stolen debit card is emptied in one payment.",
        columns=("transactions.amount_usd_cents", "cards.card_type"),
        scenario_assumptions=("Large means above $1,000.",),
        status=status if clause is not None else "not_built",
        turn_outcome="rule_proposed" if clause is not None else "clarification",
        turn_message=None if clause is not None else "Which amount?",
        refused=(),
        clause=clause,
        explanation="Flags large debit payments." if clause is not None else None,
        rule_assumptions=("Debit means card_type = 'Debit' only.",) if clause is not None else (),
        rule=card(clause, flagged, caught) if clause is not None else None,
        caveats=(),
        criteria=(),
        duplicate_check="complete" if clause is not None else "not_applicable",
        model_id="scripted",
        scenario_prompt_version="scenarios-1",
        rule_prompt_version="turn-5",
    )


@pytest.fixture
def bank(tmp_path: Path) -> RuleBank:
    return RuleBank(tmp_path / "var" / "bank.db", clock=lambda: CLOCK)


def started(bank: RuleBank) -> int:
    return bank.start_run(
        kind="propose",
        source="scripted",
        batch_size=3,
        request_bound=7,
        model_id="scripted",
        scenario_prompt_version="scenarios-1",
        rule_prompt_version="turn-5",
    )


def test_the_file_and_its_folder_are_made_on_first_use_and_not_by_reading(bank: RuleBank) -> None:
    assert not bank.path.parent.exists()
    assert bank.entries() == () and bank.latest_run() is None
    assert not bank.path.exists(), "listing an absent bank creates nothing"
    assert started(bank) == 1
    assert bank.path.is_file()


def test_an_entry_is_read_back_whole_with_the_statements_that_measured_it(bank: RuleBank) -> None:
    run_id = started(bank)
    new = entry(run_id)
    entry_id = bank.add_entry(new)
    found = bank.entry(entry_id)
    assert NewEntry.model_validate(found.model_dump(include=set(NewEntry.model_fields))) == new
    assert (found.decision, found.decision_note, found.decided_at) == ("pending", None, None)
    assert found.created_at == CLOCK and found.matches == ()
    assert found.backtest_sql is not None and CLAUSE in found.backtest_sql
    assert found.monthly_sql is not None and "GROUP BY month" in found.monthly_sql
    assert found.scenario_assumptions and found.rule_assumptions, "both sets of assumptions"


def test_only_aggregates_are_stored(bank: RuleBank) -> None:
    bank.add_entry(entry(started(bank)))
    connection = sqlite3.connect(bank.path)
    columns = {
        table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        for table in ("runs", "entries", "matches")
    }
    connection.close()
    assert not {"transaction_id", "card_id", "rows"} & {name for names in columns.values() for name in names}
    stored = bank.entry(1).rule
    assert stored is not None and set(stored.model_dump()) == set(RuleCard.model_fields)


def test_the_same_clause_is_not_added_twice_whatever_its_spacing_or_case(bank: RuleBank) -> None:
    run_id = started(bank)
    first = bank.add_entry(entry(run_id))
    respaced = CLAUSE.replace(" AND ", "\n  and  ").replace("card_id", "CARD_ID") + " -- again"
    assert bank.find_clause(respaced) == first
    with pytest.raises(DuplicateClause) as raised:
        bank.add_entry(entry(run_id, clause=respaced))
    assert raised.value.entry_id == first
    assert len(bank.entries()) == 1


def test_a_clause_key_keeps_what_can_change_the_meaning() -> None:
    assert clause_key("card_type = 'Debit'") != clause_key("card_type = 'debit'")
    assert clause_key("a  =  'x  y'") != clause_key("a = 'x y'")
    assert clause_key("A = 1 /* note */ AND b=2") == clause_key("a=1 and B = 2")
    assert clause_key("errors = 'it''s'") == "errors = 'it''s'"


def test_candidates_are_the_rules_that_flag_as_many_transactions(bank: RuleBank) -> None:
    run_id = started(bank)
    same = bank.add_entry(entry(run_id, clause="id > 5", flagged=500))
    bank.add_entry(entry(run_id, clause="id > 6", flagged=499))
    bank.add_entry(entry(run_id, clause=None))
    stored = bank.entry(same).rule
    assert stored is not None
    assert bank.candidates(stored.flagged) == ((same, "id > 5"),)
    assert bank.candidates(7) == ()


def test_a_match_points_at_the_entry_and_adds_no_row(bank: RuleBank) -> None:
    run_id = started(bank)
    entry_id = bank.add_entry(entry(run_id))
    bank.add_match(
        entry_id, run_id=run_id, position=2, kind="same_transactions", title="Again",
        hypothesis="The same rule, said differently.", clause="id > 0",
    )  # fmt: skip
    assert len(bank.entries()) == 1 and bank.entries()[0].matches == 1
    match = bank.entry(entry_id).matches[0]
    assert (match.kind, match.title, match.position) == ("same_transactions", "Again", 2)
    run = bank.run(run_id)
    assert run is not None
    assert [(item.result, item.entry_id, item.match_kind) for item in run.items] == [
        ("proposed", entry_id, None),
        ("duplicate", entry_id, "same_transactions"),
    ]


def test_a_decision_is_kept_with_its_note_and_can_be_taken_back(bank: RuleBank) -> None:
    entry_id = bank.add_entry(entry(started(bank)))
    approved = bank.decide(entry_id, "approved", "  Holds for card-present fraud.  ")
    assert (approved.decision, approved.decision_note, approved.decided_at) == (
        "approved", "Holds for card-present fraud.", CLOCK,
    )  # fmt: skip
    assert bank.entries()[0].decision == "approved"
    assert bank.decide(entry_id, "rejected", None).decision_note is None
    pending = bank.decide(entry_id, "pending", "")
    assert (pending.decision, pending.decided_at) == ("pending", None)


def test_an_entry_without_a_rule_cannot_be_decided_and_an_unknown_one_is_named(
    bank: RuleBank,
) -> None:
    no_rule = bank.add_entry(entry(started(bank), clause=None))
    with pytest.raises(BankError) as not_reviewable:
        bank.decide(no_rule, "approved", None)
    assert not_reviewable.value.kind == "not_reviewable"
    with pytest.raises(BankError) as unknown:
        bank.entry(99)
    with pytest.raises(BankError) as undecidable:
        bank.decide(99, "approved", None)
    assert unknown.value.kind == undecidable.value.kind == "unknown_entry"


def test_everything_survives_reopening_the_file(bank: RuleBank) -> None:
    run_id = started(bank)
    entry_id = bank.add_entry(entry(run_id))
    bank.decide(entry_id, "rejected", "Too broad.")
    bank.finish_run(
        run_id, status="completed", error=None, scenarios=3, model_requests=4,
        input_tokens=10, output_tokens=5, elapsed_ms=1200,
    )  # fmt: skip
    reopened = RuleBank(bank.path)
    found = reopened.entry(entry_id)
    assert (found.decision, found.decision_note, found.clause) == ("rejected", "Too broad.", CLAUSE)
    run = reopened.latest_run()
    assert run is not None and (run.status, run.scenarios, run.model_requests) == ("completed", 3, 4)
    assert run.finished_at is not None and run.request_bound == 7
    with pytest.raises(DuplicateClause):
        reopened.add_entry(entry(started(reopened)))


def test_entries_are_ranked_by_status_then_by_lift_in_the_discovery_period(bank: RuleBank) -> None:
    run_id = started(bank)
    bank.add_entry(entry(run_id, clause=None, title="no rule"))
    bank.add_entry(entry(run_id, clause="id > 1", status="no_signal", title="none"))
    bank.add_entry(entry(run_id, clause="id > 2", status="confirmed", title="weak", caught=100))
    bank.add_entry(entry(run_id, clause="id > 3", status="confirmed", title="strong", caught=300))
    bank.add_entry(entry(run_id, clause="id > 4", status="not_confirmed", title="faded"))
    assert [summary.title for summary in bank.entries()] == [
        "strong", "weak", "faded", "none", "no rule",
    ]  # fmt: skip
    assert bank.entries()[-1].discovery is None


def test_a_bank_refuses_to_be_the_transaction_database(fixture_db: Path) -> None:
    with pytest.raises(BankError):
        RuleBank(fixture_db, not_the_database=fixture_db)


def test_a_file_that_is_not_a_bank_is_refused_and_left_as_it_is(
    fixture_db: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other.db"
    shutil.copy(fixture_db, other)
    before = file_sha256(other)
    bank = RuleBank(other)
    with pytest.raises(BankError) as raised:
        started(bank)
    assert raised.value.kind == "not_a_bank"
    with pytest.raises(BankError):
        bank.entries()
    assert file_sha256(other) == before, "no table was added to a foreign database"


def test_a_path_that_cannot_be_a_file_is_a_bank_error_without_the_path(tmp_path: Path) -> None:
    bank = RuleBank(tmp_path)  # a folder
    with pytest.raises(BankError) as raised:
        started(bank)
    assert raised.value.kind == "unavailable" and str(tmp_path) not in str(raised.value)


def test_a_bank_of_the_first_version_is_brought_up_to_date_and_keeps_its_rows(bank: RuleBank) -> None:
    run_id = started(bank)
    entry_id = bank.add_entry(entry(run_id))
    connection = sqlite3.connect(bank.path)
    connection.executescript("ALTER TABLE runs DROP COLUMN kind; PRAGMA user_version = 1;")
    connection.close()
    reopened = RuleBank(bank.path)
    assert reopened.entry(entry_id).clause == CLAUSE
    old = reopened.run(run_id)
    assert old is not None and old.kind == "batch"
    assert started(reopened) == run_id + 1


def test_deleting_an_entry_removes_its_matches_and_keeps_the_runs(bank: RuleBank) -> None:
    run_id = started(bank)
    kept = bank.add_entry(entry(run_id, clause="id > 1", title="kept"))
    gone = bank.add_entry(entry(run_id, clause="id > 2", title="gone"))
    for target in (kept, gone):
        bank.add_match(
            target, run_id=run_id, position=3, kind="same_clause", title=f"again {target}",
            hypothesis="h", clause="id > 0",
        )  # fmt: skip
    bank.decide(gone, "approved", "a note")
    bank.delete(gone)
    assert [summary.id for summary in bank.entries()] == [kept]
    assert {item.title for item in bank.earlier_scenarios(10)} == {f"again {kept}", "kept"}
    connection = sqlite3.connect(bank.path)
    assert connection.execute("SELECT count(*) FROM matches").fetchone() == (1,)
    assert "a note" not in "".join(connection.iterdump())
    connection.close()
    assert bank.run(run_id) is not None and bank.find_clause("id > 2") is None
    with pytest.raises(BankError) as raised:
        bank.delete(gone)
    assert raised.value.kind == "unknown_entry"
    assert bank.add_entry(entry(run_id, clause="id > 2")) > gone, "and the rule can come back"


def test_runs_left_running_are_marked_interrupted_once(bank: RuleBank) -> None:
    assert bank.interrupt_running() == 0 and not bank.path.exists()
    first, second = started(bank), started(bank)
    bank.finish_run(
        first, status="completed", error=None, scenarios=1, model_requests=1,
        input_tokens=0, output_tokens=0, elapsed_ms=1,
    )  # fmt: skip
    assert bank.interrupt_running() == 1 and bank.interrupt_running() == 0
    done, left = bank.run(first), bank.run(second)
    assert done is not None and left is not None
    assert (done.status, left.status) == ("completed", "interrupted")
    assert left.error is not None and left.error.kind == "interrupted"
