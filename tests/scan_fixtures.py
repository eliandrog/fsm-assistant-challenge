"""Databases for the fraud-scan tests, built from data/schema.sql.

A database is a list of groups; every transaction of a group has the same
type, location, merchant, card, timestamp, error and amount, so a segment's
counts can be added up by hand. Row-level values (transaction ids, card
ids, merchant names, an address, a ZIP code) are unusual on purpose: a test
can search for them where no row may appear.
"""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

SCHEMA = Path(__file__).resolve().parents[1] / "data" / "schema.sql"

FIRST_TRANSACTION_ID = 880_000_001
VISA_CREDIT, MASTERCARD_DEBIT, VISA_DEBIT, AMEX_PREPAID = 770_001, 770_002, 770_003, 770_004
MISSING_CARD = 770_999
ROME_ITALY, ROME_GA, SACRAMENTO, AQUILA = 1, 2, 3, 4
# Values that exist only in rows, never as the name of a segment.
ROW_MARKERS = ("88000", "77000", "Marker", "91919")

REFERENCE_ROWS = f"""
INSERT INTO users VALUES
  (1, '1980-01-31', 'Female', '1 Marker Street', 40.0, -74.0, 3000000, 6000000, 1000000, 720);
INSERT INTO mcc_codes VALUES
  (5411, 'Grocery Stores, Supermarkets'), (4829, 'Money Transfer'),
  (3722, 'Passenger Railways'), (3723, 'Passenger Railways');
INSERT INTO merchants VALUES
  (10, 'Marker Merchant 10', 5411), (20, 'Marker Merchant 20', 4829),
  (30, 'Marker Merchant 30', 3722), (31, 'Marker Merchant 31', 3723);
INSERT INTO merchant_locations VALUES
  ({ROME_ITALY}, 10, 'Rome', 'Italy', NULL),
  ({ROME_GA}, 10, 'Rome', 'GA', 91919),
  ({SACRAMENTO}, 20, 'Sacramento', 'CA', 91919),
  ({AQUILA}, 10, 'L''Aquila', 'Italy', NULL);
INSERT INTO cards VALUES
  ({VISA_CREDIT}, 1, 'Visa', 'Credit', '2024-12-31', 1, 1000000, '2010-05-01', 2015, 0),
  ({MASTERCARD_DEBIT}, 1, 'Mastercard', 'Debit', '2023-06-30', 1, 500000, '2012-03-01', 2016, 0),
  ({VISA_DEBIT}, 1, 'Visa', 'Debit', '2023-06-30', 1, 500000, '2012-03-01', 2016, 0),
  ({AMEX_PREPAID}, 1, 'Amex', 'Debit (Prepaid)', '2022-01-31', 0, 5000, '2018-09-01', 2018, 0);
"""


class Group(BaseModel):
    """Transactions that differ in nothing but their id and their label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    labelled: int
    fraud: int
    unlabelled: int = 0
    transaction_type: str = "Chip Transaction"
    location: int = ROME_ITALY
    merchant: int = 10
    card: int = VISA_CREDIT
    timestamp: str = "2019-03-15 11:00:00"
    error: str | None = None
    amount: int = 2513


# 799 labelled transactions, 189 of them fraud. Group A is the concentration:
# it alone is in Rome, Italy, a chip transaction, at 11:00, without an error,
# between $10 and $50, on the Visa credit card. D and E sit just under the
# support floor, one by a transaction and one by a fraud.
A = Group(labelled=200, fraud=150, unlabelled=50)
B = Group(
    labelled=300, fraud=20, transaction_type="Swipe Transaction", location=SACRAMENTO,
    merchant=20, card=MASTERCARD_DEBIT, timestamp="2019-01-31 09:00:00",
    error="Insufficient Balance", amount=517,
)  # fmt: skip
C = Group(
    labelled=100, fraud=0, transaction_type="Swipe Transaction", location=ROME_GA,
    merchant=10, card=MASTERCARD_DEBIT, timestamp="2019-02-10 09:00:00",
    error="Bad PIN", amount=-113,
)  # fmt: skip
D = Group(
    labelled=99, fraud=10, transaction_type="Swipe Transaction", location=SACRAMENTO,
    merchant=30, card=MASTERCARD_DEBIT, timestamp="2019-04-20 23:00:00",
    error="Bad PIN", amount=150017,
)  # fmt: skip
E = Group(
    labelled=100, fraud=9, transaction_type="Swipe Transaction", location=SACRAMENTO,
    merchant=31, card=MASTERCARD_DEBIT, timestamp="2019-05-05 22:00:00",
    error="Insufficient Balance", amount=7519,
)  # fmt: skip
MAIN = (A, B, C, D, E)

# A thousand transactions without fraud that share no segment with group A.
BACKGROUND = Group(
    labelled=1000, fraud=0, transaction_type="Swipe Transaction", location=SACRAMENTO,
    merchant=20, card=MASTERCARD_DEBIT, timestamp="2019-06-01 09:00:00",
    error="Bad PIN", amount=517,
)  # fmt: skip


def build(path: Path, groups: Sequence[Group]) -> Path:
    """Write a database holding `groups` at `path`. Not in WAL mode: no sidecar files."""
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA.read_text())
    connection.executescript(REFERENCE_ROWS)
    number = FIRST_TRANSACTION_ID
    for group in groups:
        for index in range(group.labelled + group.unlabelled):
            connection.execute(
                "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (number, group.timestamp, group.card, group.amount, group.transaction_type,
                 group.merchant, group.location, group.error),
            )  # fmt: skip
            if index < group.labelled:
                connection.execute(
                    "INSERT INTO fraud_labels VALUES (?, ?)", (str(number), int(index < group.fraud))
                )
            number += 1
    connection.commit()
    connection.close()
    return path
