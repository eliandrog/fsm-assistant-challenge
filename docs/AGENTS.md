# AGENTS.md

Instructions for any AI agent working in this repo. Read this file before doing anything.

## Rules (read first)

- **Never commit, stage, push, branch, or change git config/remotes unless the user explicitly says so.** A request to change code is not permission to commit; leave changes in the working tree for review.
- **`data/` is read-only.** Never modify `data/data.db` or `data/schema.sql`. Open the database read-only: `sqlite3 'file:data/data.db?mode=ro'`, or in Python `sqlite3.connect("file:data/data.db?mode=ro", uri=True)`.
- **Model-written SQL only ever runs on a read-only connection**, after validation in code. Never execute it on a writable connection, in tests either.
- **Never commit or print secrets.** API keys go in `.env` (git-ignored); `.env.example` lists names only.
- **Scope priority: core functionality first.** In order: reliable natural language → SQL, rule proposal with backtesting, the evaluation suite. The interface stays deliberately minimal until those work.
- **Be honest in docs.** The README must describe only what exists. Mark anything unbuilt as "to be completed" or "proposed".
- **No dates about our work in docs** (no "as of", "last updated", dated decision entries). Dates that are facts about the data are fine.
- **Do not settle an open design decision without the user.**
- **No new dependencies without asking.** The model provider is not chosen yet, so no LLM SDK yet.

## What this project is

A take-home submission for Checkout.com's "Fraud Success Manager Assistant Challenge" (full brief: `docs/CHALLENGE.md`). A Fraud Success Manager (FSM) explores transaction data, validates fraud hypotheses, and turns patterns into rules. The assistant should:

1. Translate natural-language questions into accurate SQL over the provided SQLite database (the brief's suggested MVP).
2. Help turn an insight into a **rule**: a valid SQL `WHERE` clause over the `transactions` table, backtested against `fraud_labels`.
3. Ship with an initial evaluation suite, and a README that justifies the design, covers GenAI limitations, and describes offline and online metrics.

Current state: scaffold only. No module is implemented: the entry point prints a placeholder line and the other modules are docstring-only. Nothing reads the database or calls a model, and there is no evaluation suite.

## Where to find things

| Path | What |
|---|---|
| `README.md` | Submission README (the brief requires setup, solution explanation, architecture, design choices, reproduction steps) |
| `docs/CHALLENGE.md` | The original brief, verbatim. Do not edit |
| `data/` | `data.db` (SQLite, about 100 MB, WAL mode) and `schema.sql`. Read-only |
| `src/fsm_assistant/` | The package. One module per component; each docstring states its responsibility |
| `tests/` | pytest tests (offline, no model calls). Only a smoke test so far |
| `.env.example` | Names of the environment variables (none yet) |

New project docs go in `docs/`. `README.md` stays at the root because the brief requires it.

## Commands

```bash
uv sync                  # create .venv, install the package (editable) and dev tools
uv run fsm-assistant     # run the entry point (placeholder for now)
uv run pytest            # run the tests
```

Python 3.13, managed by uv. The package uses a `src/` layout built with `uv_build`.

## Conventions

- One responsibility per module. Only `llm.py` talks to a model provider.
- Safety is enforced in code, not only in the prompt.
- Numbers shown to the user (counts, fraud rates, precision, recall) come from executed SQL, never from model text.
- Tests run offline: no test calls a real model.
- Small, explainable changes. Say what changed and why.

## Data gotchas

Where the data differs from the brief, and what causes wrong SQL:

- The brief's example rule uses `amount` and `'debit'`. The real column is `amount_usd_cents` (integer cents) and `card_type` is one of `'Credit'`, `'Debit'`, `'Debit (Prepaid)'` (case-sensitive).
- Two tables are not in the brief: `merchants` and `merchant_locations`. MCC is reached through `transactions.merchant_id → merchants.mcc`.
- `fraud_labels.transaction_id` is TEXT; `transactions.id` is INTEGER. Join with `f.transaction_id = CAST(t.id AS TEXT)`: the uncast form is correct but runs away (every transaction against every label) as a `LEFT JOIN` or `EXISTS` from `transactions`.
- Only about 67% of transactions have a fraud label (777,339 of 1,159,966). An unlabelled transaction is unknown, not legitimate: compute fraud metrics with an inner join.
- Fraud is rare: 1,360 of the labelled transactions (about 0.17%).
- `amount_usd_cents` can be negative (56,900 rows).
- The database is in WAL mode; `data.db-shm` and `data.db-wal` are git-ignored sidecar files that a read-only connection creates on its first query.
