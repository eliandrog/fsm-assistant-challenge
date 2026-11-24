"""schema_context: content and determinism, on the fixture database."""

import sqlite3
from pathlib import Path
from typing import NoReturn

import pytest

from fsm_assistant import db
from fsm_assistant.schema_context import LABEL_JOIN, MAX_LISTED_VALUES, build_schema_context

TABLES = ["cards", "fraud_labels", "mcc_codes", "merchant_locations", "merchants", "transactions", "users"]


@pytest.fixture
def context(fixture_db: Path) -> str:
    build_schema_context.cache_clear()
    return build_schema_context(fixture_db)


def test_all_seven_tables_with_row_counts(context: str) -> None:
    headings = [line.split(" (")[0] for line in context.splitlines() if line.endswith(" rows)") and not line.startswith(" ")]
    assert headings == TABLES  # sorted, so the order is stable
    assert "transactions (6 rows)" in context
    assert "fraud_labels (4 rows)" in context


def test_columns_types_and_keys_come_from_the_database(context: str) -> None:
    assert "  amount_usd_cents INTEGER\n" in context
    assert "  id INTEGER PRIMARY KEY\n" in context
    assert "  transaction_id TEXT PRIMARY KEY\n" in context
    assert "  date DATETIME\n" in context
    assert "  card_id INTEGER -> cards.id\n" in context
    assert "  mcc INTEGER -> mcc_codes.mcc\n" in context


def test_card_type_values_are_listed_exactly(context: str) -> None:
    assert "  card_type TEXT values: 'Credit', 'Debit', 'Debit (Prepaid)'\n" in context


def test_listed_values_are_the_ones_present_in_the_data(context: str) -> None:
    # The fixture uses three of the four brands and one of the seven errors.
    assert "  card_brand TEXT values: 'Amex', 'Mastercard', 'Visa'\n" in context
    assert "  errors TEXT values: 'Bad PIN' (NULL in 5 rows)\n" in context
    assert "  has_chip BOOLEAN values: 0, 1\n" in context


def test_joins_include_the_label_join_with_its_cast(context: str) -> None:
    joins = context.split("Joins:\n")[1].split("\n\n")[0].splitlines()
    assert joins == [
        "  cards.user_id = users.id",
        "  merchant_locations.merchant_id = merchants.id",
        "  merchants.mcc = mcc_codes.mcc",
        "  transactions.card_id = cards.id",
        "  transactions.merchant_id = merchants.id",
        "  transactions.merchant_location_id = merchant_locations.id",
        f"  {LABEL_JOIN}",
    ]
    assert LABEL_JOIN == "fraud_labels.transaction_id = CAST(transactions.id AS TEXT)"


def test_notes_carry_the_gotchas_with_live_figures(context: str) -> None:
    notes = context.split("Notes:\n")[1]
    assert "integer number of US cents" in notes
    assert "negative in 1 rows" in notes
    assert "from '2019-01-01 00:02:00' to '2019-10-31 23:59:00'" in notes
    assert "transaction_id is TEXT, transactions.id is INTEGER" in notes
    assert f"Always join with {LABEL_JOIN}" in notes
    assert "Only 4 of 6 transactions (66.7%) have a fraud label" in notes
    assert "labelled transactions only" in notes
    assert "is_fraud = 1 for 1 of 4 labelled transactions (25.00%)" in notes
    assert "'Debit', not 'debit'" in notes


def test_high_cardinality_columns_are_not_listed(fixture_db: Path, tmp_path: Path) -> None:
    # More distinct values than the cap: the column appears without a value list.
    path = tmp_path / "many.db"
    path.write_bytes(fixture_db.read_bytes())
    connection = sqlite3.connect(path)
    connection.executemany(
        "INSERT INTO merchant_locations VALUES (?, 10, ?, 'CA', 1)",
        [(100 + n, f"City {n}") for n in range(MAX_LISTED_VALUES + 1)],
    )
    connection.commit()
    connection.close()
    context = build_schema_context(path)
    assert "  city TEXT\n" in context
    assert "  state TEXT values: '', 'CA', 'Italy'\n" in context


def test_output_is_byte_identical_across_builds(fixture_db: Path) -> None:
    build_schema_context.cache_clear()
    first = build_schema_context(fixture_db)
    build_schema_context.cache_clear()
    second = build_schema_context(fixture_db)
    assert first.encode() == second.encode()


def test_result_is_cached_per_path(fixture_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_schema_context.cache_clear()
    first = build_schema_context(fixture_db)

    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("the database was queried again")

    monkeypatch.setattr(db, "run_query", fail)
    assert build_schema_context(fixture_db) is first


def test_missing_database_is_reported(tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseNotFoundError):
        build_schema_context(tmp_path / "absent.db")
