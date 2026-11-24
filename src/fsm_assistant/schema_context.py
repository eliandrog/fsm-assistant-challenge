"""Schema context provider.

Responsibility: build the text that grounds the model's SQL in the real
database. Structure (tables, columns, types, keys), value sets and the
figures in the notes are read from the database itself through `db`, so the
description cannot drift from the data. The prose of the notes is written
here; each note states a fact checked against the real database (see
docs/DATA.md and the integration tests).

The text goes into every prompt, so it is kept compact, and it is
deterministic (tables and values sorted, nothing time-dependent) so that
evaluation runs are comparable. It is built once per database path and
cached: building it scans the large tables a few times.
"""

from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from fsm_assistant import db

# A column's distinct values are listed only when there are at most this many.
MAX_LISTED_VALUES = 12

# Declared types whose columns may hold a small fixed set of values.
_CATEGORICAL_TYPES = frozenset({"TEXT", "BOOLEAN"})

# Limits for the metadata queries below. They are ours, not model-written,
# so they do not use the configured limits for user queries.
_ROW_CAP = 1000
_TIMEOUT_SECONDS = 60.0

# The label join has no declared foreign key and its two sides have different
# types. Casting the integer side lets SQLite use the index on
# fraud_labels.transaction_id in every join shape; without the cast a LEFT
# JOIN or EXISTS from transactions scans all labels for each transaction.
def label_join(labels: str, transactions: str) -> str:
    """The label join condition, for the given table names or aliases."""
    return f"{labels}.transaction_id = CAST({transactions}.id AS TEXT)"


LABEL_JOIN = label_join("fraud_labels", "transactions")


@cache
def build_schema_context(db_path: Path) -> str:
    """Return the schema description for the database at `db_path`.

    Cached per path for the life of the process; the database is read-only
    and does not change while the application runs.
    """

    def query(sql: str, row_cap: int = _ROW_CAP) -> db.QueryResult:
        return db.run_query(
            sql, db_path=db_path, row_cap=row_cap, timeout_seconds=_TIMEOUT_SECONDS
        )

    tables = [
        row[0]
        for row in query(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).rows
    ]

    lines = ["Database: SQLite. Tables, with row counts:", ""]
    joins: list[str] = []
    for table in tables:
        columns = query(f"PRAGMA table_info({_quote(table)})").rows
        references = {
            row[3]: f"{row[2]}.{row[4]}"
            for row in query(f"PRAGMA foreign_key_list({_quote(table)})").rows
        }
        joins += [f"{table}.{column} = {target}" for column, target in references.items()]

        # One scan per table gives the row count and each column's non-NULL count.
        counts = query(
            "SELECT count(*), "
            + ", ".join(f"count({_quote(column[1])})" for column in columns)
            + f" FROM {_quote(table)}"
        ).rows[0]
        total = counts[0]

        lines.append(f"{table} ({total} rows)")
        for (_, name, declared_type, _, _, is_primary_key), non_null in zip(
            columns, counts[1:]
        ):
            parts = [f"  {name} {declared_type}"]
            if is_primary_key:
                parts.append(" PRIMARY KEY")
            elif name in references:
                parts.append(f" -> {references[name]}")
            elif declared_type.upper() in _CATEGORICAL_TYPES:
                # Reusing the row cap: more than MAX_LISTED_VALUES distinct
                # values comes back truncated, and the column is not listed.
                distinct = query(
                    f"SELECT DISTINCT {_quote(name)} FROM {_quote(table)} "
                    f"WHERE {_quote(name)} IS NOT NULL",
                    row_cap=MAX_LISTED_VALUES,
                )
                if distinct.rows and not distinct.truncated:
                    values = sorted((row[0] for row in distinct.rows), key=_sort_key)
                    parts.append(" values: " + ", ".join(map(_literal, values)))
            if non_null < total:
                parts.append(f" (NULL in {total - non_null} rows)")
            lines.append("".join(parts))
        lines.append("")

    lines.append("Joins:")
    lines += [f"  {join}" for join in sorted(joins)]
    lines.append(f"  {LABEL_JOIN}")
    lines.append("")
    lines.append("Notes:")
    lines += [f"- {note}" for note in _notes(query)]
    return "\n".join(lines) + "\n"


def _notes(query: Callable[[str], db.QueryResult]) -> list[str]:
    """The facts a model most often gets wrong, with figures read live."""
    transactions, negative, first_date, last_date = query(
        "SELECT count(*), sum(amount_usd_cents < 0), min(date), max(date) "
        "FROM transactions"
    ).rows[0]
    labelled, fraud = query(
        "SELECT count(*), sum(fraud_labels.is_fraud) FROM transactions "
        f"JOIN fraud_labels ON {LABEL_JOIN}"
    ).rows[0]
    return [
        "Money: every *_usd_cents column is an integer number of US cents. "
        "$1,000 is 100000. Divide by 100.0 only to display dollars.",
        f"transactions.amount_usd_cents is negative in {negative or 0} rows; "
        "their meaning is not documented, so do not assume they are refunds.",
        "transactions.date is text 'YYYY-MM-DD HH:MM:SS', from "
        f"'{first_date}' to '{last_date}'. Other DATE columns are text "
        "'YYYY-MM-DD'. Compare as strings or use strftime().",
        "Text comparisons are case-sensitive. Use listed values exactly as "
        "written: card_type = 'Debit', not 'debit'.",
        "BOOLEAN columns hold 0 or 1.",
        "Fraud labels: fraud_labels.transaction_id is TEXT, transactions.id "
        f"is INTEGER. Always join with {LABEL_JOIN}.",
        f"Only {labelled} of {transactions} transactions "
        f"({_percent(labelled, transactions, 1)}) have a fraud label; the rest "
        "are unknown, not legitimate. Compute fraud rates over labelled "
        "transactions only (inner join to fraud_labels); never count a "
        "missing label as not fraud.",
        f"Fraud is rare: is_fraud = 1 for {fraud or 0} of {labelled} labelled "
        f"transactions ({_percent(fraud or 0, labelled, 2)}).",
        "Merchant category: transactions.merchant_id -> merchants.mcc -> "
        "mcc_codes.description. transactions has no mcc column.",
        "merchant_locations.state is a two-letter US state code, or a country "
        "name outside the US. Online locations have city = 'ONLINE', "
        "state = '' and zip NULL.",
        "transactions.errors is NULL when the transaction had no error.",
    ]


def _quote(identifier: str) -> str:
    """Quote an identifier read from the database for use in SQL."""
    return '"' + identifier.replace('"', '""') + '"'


def _literal(value: Any) -> str:
    """Render a value the way it is written in SQL."""
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def _sort_key(value: Any) -> tuple[str, Any]:
    """Order mixed values deterministically (by type name, then value)."""
    return (type(value).__name__, value)


def _percent(part: int, whole: int, decimals: int) -> str:
    return f"{100 * part / whole:.{decimals}f}%" if whole else "n/a"
