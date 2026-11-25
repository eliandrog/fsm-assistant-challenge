"""The offline demo's stand-in for the model.

Responsibility: an `IntentModel` that answers a short fixed list of questions
with replies written here, so the web chat can be tried with no key and no
network (docs/DECISIONS.md, D-31). Only the model is replaced: each reply is a
tool call that goes through the same shape check, guard, database, backtest,
scan and presentation as a live one, so every figure shown still comes from
executed SQL.

It understands nothing. A question on the list gets its reply, a message that
starts with "Propose a rule" gets the one canned rule whatever else it says,
and anything else gets a clarification that offers questions from the list.
The SQL here is written for the shipped schema.

The rule finder has a canned run too (D-35): `DemoScenarioModel` answers the
scenario request with the next of `FINDER_SCENARIOS` that the request does
not already quote as proposed, and each scenario's turn gets the
reply kept with it. Scenarios and clauses are written here, in advance; the
guard, the backtest, the verdict and the bank are the real ones.
"""

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, JsonValue

from fsm_assistant.llm import (
    JsonObject,
    ModelReply,
    ModelRequest,
    ModelSpec,
    TokenUsage,
    ToolCall,
    request_hash,
)
from fsm_assistant.finder import QUESTION_PREFIX
from fsm_assistant.nl2sql import ToolName
from fsm_assistant.scenarios import TOOL_NAME as SCENARIO_TOOL
from fsm_assistant.scenarios import Scenario

MODEL_ID: Final = "offline-demo"
_NOTE: Final = "Offline demo: this reply was written in advance, not by a model."
_RULE_PREFIX: Final = "propose a rule"
_ASKED = re.compile(r"Propose exactly (\d+) fraud scenario")
_QUESTION = re.compile(r"Question:\n<question>\n(.*?)\n</question>", re.DOTALL)

_ROME_RULE: Final = (
    "merchant_location_id IN (SELECT id FROM merchant_locations "
    "WHERE city = 'Rome' AND state = 'Italy')"
)
_FRAUD_BY_CARD_TYPE: Final = """\
SELECT c.card_type,
       count(*) AS labelled_transactions,
       sum(f.is_fraud) AS fraud_transactions,
       avg(f.is_fraud) AS fraud_rate
FROM transactions AS t
JOIN fraud_labels AS f ON f.transaction_id = CAST(t.id AS TEXT)
JOIN cards AS c ON c.id = t.card_id
GROUP BY c.card_type
ORDER BY fraud_rate DESC"""
_TOP_MERCHANTS: Final = """\
SELECT t.merchant_id,
       count(*) AS transactions,
       sum(t.amount_usd_cents) AS total_amount_usd_cents
FROM transactions AS t
GROUP BY t.merchant_id
ORDER BY total_amount_usd_cents DESC
LIMIT 10"""


class DemoReply(BaseModel):
    """One question of the demo and the tool call that answers it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str
    tool: ToolName
    arguments: JsonObject


def _sql(question: str, sql: str, explanation: str, *assumptions: str) -> DemoReply:
    return DemoReply(
        question=question,
        tool="run_sql",
        arguments={
            "sql": sql,
            "explanation": f"{explanation} {_NOTE}",
            "assumptions": list(assumptions),
        },
    )


_RULE = DemoReply(
    question="Propose a rule for transactions at merchant locations in Rome, Italy.",
    tool="propose_rule",
    arguments={
        "where_clause": _ROME_RULE,
        "explanation": "Flags every transaction at a merchant location in Rome, Italy. "
        f"{_NOTE} It is the same rule whatever the message asked for.",
        "assumptions": ["Rome means the city in Italy, not the towns of that name in the US."],
    },
)
_COUNT = _sql(
    "How many transactions are there?",
    "SELECT count(*) AS transactions FROM transactions",
    "Counts every transaction.",
)
_SCAN = DemoReply(question="Where is fraud concentrated?", tool="profile_fraud", arguments={})

CATALOGUE: Final[tuple[DemoReply, ...]] = (
    _COUNT,
    _sql(
        "What is the fraud rate by card type?",
        _FRAUD_BY_CARD_TYPE,
        "The share of labelled transactions that are fraud, for each card type.",
        "Only transactions with a fraud label are counted.",
    ),
    _sql(
        "Which ten merchants took the most money?",
        _TOP_MERCHANTS,
        "The ten merchants with the highest total amount, with their number of transactions.",
        "Refunds, which are negative amounts, are included in the totals.",
    ),
    _SCAN,
    _RULE,
    DemoReply(
        question="Show me the suspicious transactions.",
        tool="ask_clarification",
        arguments={
            "question": "What should count as suspicious? The data has fraud labels, but no "
            f"score of suspicion. {_NOTE}",
            "suggestions": [_SCAN.question, "What is the fraud rate by card type?"],
        },
    ),
    DemoReply(
        question="What will the fraud rate be next year?",
        tool="decline",
        arguments={
            "reason": "The data ends on 2019-10-31 and holds no forecast, so a future rate "
            f"cannot be read from it. {_NOTE}"
        },
    ),
    # Not a read: the guard refuses it, twice, and the turn ends as rejected.
    _sql(
        "Delete the fraud labels.",
        "DELETE FROM fraud_labels",
        "Removes every fraud label.",
    ),
)
QUESTIONS: Final[tuple[str, ...]] = tuple(reply.question for reply in CATALOGUE)

_FALLBACK = DemoReply(
    question="",
    tool="ask_clarification",
    arguments={
        "question": "The offline demo has no model behind it, so it cannot read a new "
        "question. It has canned replies for a short list of questions; which would you "
        "like to try?",
        "suggestions": [_COUNT.question, _SCAN.question, _RULE.question],
    },
)


class DemoScenario(BaseModel):
    """One canned scenario of the finder's demo run and the reply its turn gets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: Scenario
    tool: ToolName
    arguments: JsonObject


def _found(
    title: str,
    hypothesis: str,
    columns: tuple[str, ...],
    assumptions: tuple[str, ...],
    clause: str,
    explanation: str,
    *rule_assumptions: str,
) -> DemoScenario:
    return DemoScenario(
        scenario=Scenario(
            title=title, hypothesis=hypothesis, columns=columns, assumptions=assumptions
        ),
        tool="propose_rule",
        arguments={
            "where_clause": clause,
            "explanation": f"{explanation} {_NOTE}",
            "assumptions": list(rule_assumptions),
        },
    )


# The seven scenarios of a live run on claude-haiku-5-5 that became entries, with the clause
# the model wrote for each (docs/DECISIONS.md, D-35). Kept as the model wrote them.
# fmt: off
FINDER_SCENARIOS: Final[tuple[DemoScenario, ...]] = (
    _found(
        (
            "Card testing with micro-amount bursts"
        ),
        (
            "A person testing stolen card numbers makes several very small purchases on one card "
            "in quick succession before moving to larger charges. Flag a transaction whose card "
            "has at least 3 other transactions with amount_usd_cents between 1 and 200 (under "
            "$2.00) within the 60 minutes before it, dated on the same card_id."
        ),
        ("transactions.card_id", "transactions.date", "transactions.amount_usd_cents"),
        (
            (
                "Micro-amount means amount_usd_cents between 1 and 200 inclusive (0.01 to 2.00 US "
                "dollars); zero and negative amounts are excluded."
            ),
            (
                "Window is 60 minutes (3600 seconds) before the transaction, excluding the "
                "transaction itself."
            ),
            (
                "Minimum count of 3 prior micro-amount transactions is a chosen threshold, not "
                "derived from data."
            ),
        ),
        (
            "id IN (SELECT id FROM (SELECT id, SUM(CASE WHEN amount_usd_cents BETWEEN 1 AND 200 "
            "THEN 1 ELSE 0 END) OVER (PARTITION BY card_id ORDER BY CAST(strftime('%s', date) AS "
            "INTEGER) RANGE BETWEEN 3600 PRECEDING AND CURRENT ROW) - CASE WHEN amount_usd_cents "
            "BETWEEN 1 AND 200 THEN 1 ELSE 0 END AS prior_micro FROM transactions) WHERE "
            "prior_micro >= 3)"
        ),
        (
            "Flags a transaction when its card has at least 3 other micro-amount transactions "
            "(between 1 and 200 cents) in the 60 minutes up to and including the transaction's "
            "own timestamp, excluding the transaction itself."
        ),
        (
            "Micro-amount means amount_usd_cents between 1 and 200 inclusive; zero and negative "
            "amounts are excluded."
        ),
        (
            "Window is the 60 minutes (3600 seconds) before the transaction, measured on whole "
            "seconds; other transactions on the same card at the same second are counted, and the"
            " transaction itself is excluded."
        ),
        (
            "The minimum of 3 prior micro-amount transactions is a chosen threshold, not derived "
            "from data."
        ),
        (
            "The clause uses one window function over transactions partitioned by card_id instead"
            " of a self-join, to keep the backtest within the time limit."
        ),
    ),
    _found(
        (
            "Card flagged as compromised on dark web"
        ),
        (
            "Card numbers sold on dark web markets are reused by fraudsters. Flag any transaction"
            " whose card has card_on_dark_web = 1 in the cards table."
        ),
        ("cards.card_on_dark_web", "transactions.card_id"),
        (
            (
                "card_on_dark_web = 1 means the card is flagged; the schema lists only the value 0, "
                "so 1 is assumed to be the flagged state."
            ),
            (
                "The flag is treated as static for the whole data period, with no date of flagging "
                "available."
            ),
        ),
        (
            "card_id IN (SELECT id FROM cards WHERE card_on_dark_web = 1)"
        ),
        (
            "Flags every transaction made on a card that is marked as compromised on the dark web"
            " (card_on_dark_web = 1), across the whole data period."
        ),
        (
            "card_on_dark_web = 1 means the card is flagged as compromised; the schema lists only"
            " the value 0, so 1 is taken as the flagged state."
        ),
        (
            "The flag is static for the whole data period (2019-01-01 to 2019-10-31), since no "
            "date of flagging is available, so transactions before and after any hypothetical "
            "flagging date are all flagged."
        ),
        (
            "The rule is applied to all transactions regardless of amount, type, or label status."
        ),
    ),
    _found(
        (
            "Repeated PIN failures on a recently changed PIN"
        ),
        (
            "An attacker who has obtained a card but not its PIN guesses PINs at a terminal, "
            "producing Bad PIN errors, and a PIN that was changed recently may indicate account "
            "takeover. Flag a transaction on a card that has at least 2 transactions with errors "
            "= 'Bad PIN' in the previous 24 hours and whose cards.year_pin_last_changed equals "
            "the calendar year of the transaction (2019)."
        ),
        ("transactions.errors", "transactions.card_id", "transactions.date", "cards.year_pin_last_changed"),
        (
            (
                "Previous 24 hours means 86400 seconds before the transaction."
            ),
            (
                "Recently changed PIN is defined as year_pin_last_changed equal to 2019, the year of "
                "the data."
            ),
            (
                "Minimum of 2 Bad PIN errors is a chosen threshold."
            ),
        ),
        (
            "transactions.id IN (SELECT w.id FROM (SELECT t.id, COUNT(CASE WHEN t.errors = 'Bad "
            "PIN' THEN 1 END) OVER (PARTITION BY t.card_id ORDER BY CAST(strftime('%s', t.date) "
            "AS INTEGER) RANGE BETWEEN 86400 PRECEDING AND 1 PRECEDING) AS prior_bad FROM "
            "transactions t WHERE t.card_id IN (SELECT c.id FROM cards c WHERE "
            "c.year_pin_last_changed = 2019)) w WHERE w.prior_bad >= 2) AND strftime('%Y', "
            "transactions.date) = '2019'"
        ),
        (
            "Flags transactions on a card that already has at least 2 Bad PIN errors in the 24 "
            "hours before the transaction, where the card's PIN was last changed in 2019 and the "
            "transaction is in 2019."
        ),
        (
            "Previous 24 hours means the 86400 seconds before the transaction, excluding the "
            "transaction itself and any same-second transactions."
        ),
        (
            "Recently changed PIN means cards.year_pin_last_changed = 2019, the year of the data."
        ),
        (
            "Minimum of 2 Bad PIN errors is a chosen threshold."
        ),
        (
            "The rewrite uses one windowed count per card instead of a correlated self-join, to "
            "fit the clause time limit; the logic is unchanged."
        ),
    ),
    _found(
        (
            "High spend on a newly opened account"
        ),
        (
            "A fraudster who opens or obtains a new account spends heavily before it is noticed. "
            "Flag a transaction of at least $1,000 (100000 cents) made on a card whose "
            "acct_open_date is within 90 days before the transaction date."
        ),
        ("cards.acct_open_date", "transactions.date", "transactions.amount_usd_cents", "transactions.card_id"),
        (
            (
                "Amount threshold of $1,000 (100000 US cents) is a chosen value."
            ),
            (
                "New account means 0 to 90 days (2160 hours) between acct_open_date and the "
                "transaction date, with acct_open_date treated as midnight of that day."
            ),
            (
                "Date comparison is done on the YYYY-MM-DD part of transactions.date."
            ),
        ),
        (
            "transactions.amount_usd_cents >= 100000 AND EXISTS (SELECT 1 FROM cards c WHERE c.id"
            " = transactions.card_id AND julianday(substr(transactions.date, 1, 10)) - "
            "julianday(c.acct_open_date) BETWEEN 0 AND 90)"
        ),
        (
            "Flags transactions of at least $1,000 made on a card whose account was opened 0 to "
            "90 days before the transaction date, so heavy spend on a newly opened account is "
            "caught."
        ),
        (
            "The $1,000 threshold is 100000 US cents, a chosen value."
        ),
        (
            "New account means 0 to 90 days between acct_open_date and the transaction date, "
            "inclusive at both ends."
        ),
        (
            "acct_open_date is treated as midnight of that day, so the day difference is computed"
            " on the YYYY-MM-DD part of transactions.date."
        ),
        (
            "Transactions dated before the account opened are not flagged."
        ),
        (
            "Negative amounts are not flagged, since they fall below the threshold."
        ),
    ),
    _found(
        (
            "Merchant location jump between consecutive card uses"
        ),
        (
            "A card used in one state and then, within a short time, in a distant state indicates"
            " the card number is being used away from its owner. Flag a transaction whose "
            "merchant_locations.state differs from the state of the previous transaction on the "
            "same card, where that previous transaction was no more than 60 minutes earlier and "
            "both locations are not online."
        ),
        ("transactions.card_id", "transactions.date", "transactions.merchant_location_id", "merchant_locations.state", "merchant_locations.city"),
        (
            (
                "Previous transaction is the closest earlier transaction on the same card_id."
            ),
            (
                "60 minutes (3600 seconds) is a chosen window."
            ),
            (
                "Online locations (city = 'ONLINE', state = '') are excluded from the comparison."
            ),
            (
                "State values that are country names outside the US are compared as they are written."
            ),
        ),
        (
            "transactions.id IN (SELECT y.id FROM (SELECT x.id, x.date, x.state, x.city, "
            "LAG(x.date) OVER (PARTITION BY x.card_id ORDER BY x.date, x.id) AS prev_date, "
            "LAG(x.state) OVER (PARTITION BY x.card_id ORDER BY x.date, x.id) AS prev_state, "
            "LAG(x.city) OVER (PARTITION BY x.card_id ORDER BY x.date, x.id) AS prev_city FROM "
            "(SELECT t.id, t.card_id, t.date, l.state, l.city FROM transactions t JOIN "
            "merchant_locations l ON t.merchant_location_id = l.id) x) y WHERE y.prev_date IS NOT"
            " NULL AND y.city <> 'ONLINE' AND y.state <> '' AND y.prev_city <> 'ONLINE' AND "
            "y.prev_state <> '' AND y.state <> y.prev_state AND strftime('%s', y.date) - "
            "strftime('%s', y.prev_date) <= 3600)"
        ),
        (
            "Flags a transaction whose merchant state differs from the state of the card's "
            "previous transaction, when that earlier transaction was within 60 minutes and "
            "neither location is online."
        ),
        (
            "Previous transaction is the closest earlier transaction on the same card_id, ordered"
            " by date then id to break ties."
        ),
        (
            "The previous transaction is taken as the immediately preceding one on the card, "
            "whatever its location; if that one is online, the pair is not compared, so an "
            "earlier non-online transaction is not used."
        ),
        (
            "60 minutes is 3600 seconds, a chosen window."
        ),
        (
            "Online locations (city = 'ONLINE' or state = '') are excluded from the comparison."
        ),
        (
            "Country names outside the US in merchant_locations.state are compared as written."
        ),
    ),
    _found(
        (
            "Spend burst exhausting credit limit within a day"
        ),
        (
            "A fraudster maximises a stolen credit line quickly. Flag a transaction when the sum "
            "of amount_usd_cents on the same card over the previous 24 hours, including this one,"
            " exceeds 50% of the card's credit_limit_usd_cents."
        ),
        ("cards.credit_limit_usd_cents", "transactions.amount_usd_cents", "transactions.card_id", "transactions.date"),
        (
            (
                "The 50% share is a chosen threshold."
            ),
            (
                "Previous 24 hours means 86400 seconds before the transaction, including the "
                "transaction itself."
            ),
            (
                "Only card_type = 'Credit' cards are considered, since debit cards may have no "
                "meaningful limit; this is an assumption."
            ),
            (
                "Negative amounts are included in the sum as recorded, since their meaning is "
                "undocumented."
            ),
        ),
        (
            "transactions.id IN (SELECT w.id FROM (SELECT t.id, SUM(t.amount_usd_cents) OVER "
            "(PARTITION BY t.card_id ORDER BY CAST(strftime('%s', t.date) AS INTEGER) RANGE "
            "BETWEEN 86400 PRECEDING AND CURRENT ROW) AS s, c.credit_limit_usd_cents AS lim FROM "
            "transactions t JOIN cards c ON c.id = t.card_id WHERE c.card_type = 'Credit') w "
            "WHERE w.s > 0.5 * w.lim)"
        ),
        (
            "Flags a transaction on a credit card when the sum of amounts on that card over the "
            "previous 24 hours, including this transaction, exceeds half of the card's credit "
            "limit."
        ),
        (
            "The 50% share is a chosen threshold."
        ),
        (
            "The 24-hour window is 86400 seconds before the transaction, including the "
            "transaction itself."
        ),
        (
            "Only card_type = 'Credit' cards are considered; debit cards (including Debit "
            "(Prepaid)) are excluded."
        ),
        (
            "Negative amounts are included in the sum as recorded, since their meaning is "
            "undocumented."
        ),
        (
            "Transactions sharing the exact same second are treated as peers in the window, so "
            "they all count toward each other's 24-hour sum."
        ),
        (
            "A card with a zero or missing credit limit would be flagged whenever its window sum "
            "is positive."
        ),
    ),
    _found(
        (
            "Amount far above the card's own typical spend"
        ),
        (
            "A stolen card is often used for a purchase much larger than the owner's usual "
            "spending. Flag a transaction whose amount_usd_cents is at least 5 times the average "
            "positive amount_usd_cents of all earlier transactions on the same card_id, provided "
            "the card has at least 5 earlier positive transactions."
        ),
        ("transactions.amount_usd_cents", "transactions.card_id", "transactions.date"),
        (
            (
                "5 times the average is a chosen multiplier."
            ),
            (
                "Earlier means strictly before the transaction's date, using the full "
                "transactions.date string."
            ),
            (
                "Minimum history of 5 earlier positive transactions is a chosen threshold."
            ),
            (
                "Only positive amounts are used in the average, since negative amounts are "
                "undocumented."
            ),
        ),
        (
            "transactions.id IN (SELECT w.id FROM (SELECT id, amount_usd_cents AS amt, SUM(CASE "
            "WHEN amount_usd_cents > 0 THEN amount_usd_cents ELSE 0 END) OVER (PARTITION BY "
            "card_id ORDER BY date RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - SUM(CASE "
            "WHEN amount_usd_cents > 0 THEN amount_usd_cents ELSE 0 END) OVER (PARTITION BY "
            "card_id, date) AS prior_sum, COUNT(CASE WHEN amount_usd_cents > 0 THEN 1 END) OVER "
            "(PARTITION BY card_id ORDER BY date RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT "
            "ROW) - COUNT(CASE WHEN amount_usd_cents > 0 THEN 1 END) OVER (PARTITION BY card_id, "
            "date) AS prior_n FROM transactions) w WHERE w.prior_n >= 5 AND w.amt * w.prior_n >= "
            "5 * w.prior_sum)"
        ),
        (
            "Flags a transaction whose amount is at least 5 times the average positive amount of "
            "the same card's earlier transactions, for cards with at least 5 earlier positive "
            "transactions. It is rewritten with window functions so it runs as one uncorrelated "
            "lookup instead of a per-row self-join."
        ),
        (
            "Earlier means strictly earlier by the full date string: transactions on the same "
            "card and same timestamp are excluded from the history, computed by subtracting "
            "same-timestamp totals from the running totals."
        ),
        (
            "5 times the average and the minimum of 5 earlier positive transactions are the "
            "user's stated thresholds."
        ),
        (
            "Only positive amounts count toward the average and the history count; negative "
            "amounts are excluded from both."
        ),
        (
            "The average is compared without division (amount * count >= 5 * sum), which is "
            "equivalent and avoids division by zero."
        ),
    ),
)
# fmt: on


def reply_for(question: str) -> DemoReply:
    """The canned reply for `question`, or the clarification that lists what can be asked."""
    asked = _normalised(question)
    for found in FINDER_SCENARIOS:
        if asked.startswith(_normalised(f"{QUESTION_PREFIX}{found.scenario.title}:")):
            return DemoReply(question=question, tool=found.tool, arguments=found.arguments)
    for reply in CATALOGUE:
        if _normalised(reply.question) == asked:
            return reply
    return _RULE if asked.startswith(_RULE_PREFIX) else _FALLBACK


class DemoModel:
    """Answers from `CATALOGUE`. Builds no client, needs no key and sends nothing."""

    def __init__(self, spec: ModelSpec) -> None:
        self._spec = spec

    def complete(self, request: ModelRequest) -> ModelReply:
        asked = _QUESTION.findall(request.user)
        reply = reply_for(asked[-1] if asked else "")
        return ModelReply(
            tool_mode="tools",
            tool_calls=(ToolCall(name=reply.tool, arguments=reply.arguments),),
            text="",
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=0, output_tokens=0),
            model_id=MODEL_ID,
            response_id=None,
            request_hash=request_hash(self._spec, request),
        )


class DemoScenarioModel:
    """Answers the finder's scenario request with `FINDER_SCENARIOS`. Sends nothing."""

    def __init__(self, spec: ModelSpec) -> None:
        self._spec = spec

    def complete(self, request: ModelRequest) -> ModelReply:
        """The next canned scenarios the request does not already quote; none when all are."""
        asked = _ASKED.search(request.user)
        count = int(asked.group(1)) if asked else 1
        quoted = _normalised(request.user)
        fresh = [
            found
            for found in FINDER_SCENARIOS
            if f"- {_normalised(found.scenario.title)}:" not in quoted
        ]
        scenarios: list[JsonValue] = [
            found.scenario.model_dump(mode="json") for found in fresh[:count]
        ]
        return ModelReply(
            tool_mode="tools",
            tool_calls=(ToolCall(name=SCENARIO_TOOL, arguments={"scenarios": scenarios}),),
            text="",
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=0, output_tokens=0),
            model_id=MODEL_ID,
            response_id=None,
            request_hash=request_hash(self._spec, request),
        )


def _normalised(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".?! ")
