"""A database and scripted replies for the rule-finder tests.

The database holds one rule of each kind the finder tells apart: Rome, Italy
is fraud-heavy in both periods (confirmed), merchant 30 is fraud-heavy until
July and clean afterwards (not confirmed), and the $5.17 background carries
almost no fraud (no signal).
"""

from pathlib import Path

from conftest import call_reply
from pydantic import JsonValue
from scan_fixtures import SACRAMENTO, Group, build

from fsm_assistant.llm import ModelReply

ROME = "merchant_location_id = 1"
ROME_RESPACED = "MERCHANT_LOCATION_ID   =\n   1"
ROME_BY_NAME = (
    "merchant_location_id IN "
    "(SELECT id FROM merchant_locations WHERE city = 'Rome' AND state = 'Italy')"
)
FADING = "merchant_id = 30"
BACKGROUND = "amount_usd_cents = 517"
LABEL_LEAK = "CAST(id AS TEXT) IN (SELECT transaction_id FROM fraud_labels WHERE is_fraud = 1)"
WRITE = "1); DELETE FROM fraud_labels; --"

HISTORY = (
    Group(labelled=200, fraud=60, unlabelled=20, timestamp="2019-03-15 11:00:00"),
    Group(labelled=100, fraud=30, timestamp="2019-09-15 11:00:00"),
    Group(labelled=100, fraud=30, location=SACRAMENTO, merchant=30, amount=150017,
          timestamp="2019-04-20 23:00:00"),
    Group(labelled=100, fraud=0, location=SACRAMENTO, merchant=30, amount=150017,
          timestamp="2019-09-20 23:00:00"),
    *(
        Group(labelled=300, fraud=1, location=SACRAMENTO, merchant=20, amount=517,
              timestamp=f"2019-{month:02d}-10 09:00:00")
        for month in range(1, 11)
    ),
)  # fmt: skip


def build_history(path: Path) -> Path:
    return build(path, HISTORY)


def scenario(title: str, *assumptions: str) -> JsonValue:
    return {
        "title": title,
        "hypothesis": f"A person attempting fraud shows up as: {title}.",
        "columns": ["transactions.merchant_location_id"],
        "assumptions": list(assumptions),
    }


def scenarios_reply(*titles: str, input_tokens: int = 2000, output_tokens: int = 500) -> ModelReply:
    return call_reply(
        "propose_scenarios",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        scenarios=[scenario(title, f"assumed for {title}") for title in titles],
    )
