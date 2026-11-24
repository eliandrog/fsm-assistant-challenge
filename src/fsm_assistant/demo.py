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
"""

import re
from typing import Final

from pydantic import BaseModel, ConfigDict

from fsm_assistant.llm import (
    JsonObject,
    ModelReply,
    ModelRequest,
    ModelSpec,
    TokenUsage,
    ToolCall,
    request_hash,
)
from fsm_assistant.nl2sql import ToolName

MODEL_ID: Final = "offline-demo"
_NOTE: Final = "Offline demo: this reply was written in advance, not by a model."
_RULE_PREFIX: Final = "propose a rule"
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


def reply_for(question: str) -> DemoReply:
    """The canned reply for `question`, or the clarification that lists what can be asked."""
    asked = _normalised(question)
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


def _normalised(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".?! ")
