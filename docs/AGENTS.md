# AGENTS.md

Instructions for any AI agent working in this repo. Read this file before doing anything.

## Rules (read first)

- **Never commit, stage, push, branch, or change git config/remotes unless the user explicitly says so.** A request to change code is not permission to commit; leave changes in the working tree for review.
- **`data/` is read-only.** Never modify `data/data.db` or `data/schema.sql`. Open the database read-only: `sqlite3 'file:data/data.db?mode=ro'`, or in Python `sqlite3.connect("file:data/data.db?mode=ro", uri=True)`.
- **Model-written SQL only ever runs on a read-only connection**, after validation in code (`sql_guard`). Never execute it on a writable connection, in tests or evals either.
- **Never commit or print secrets.** API keys go in `.env` (git-ignored); `.env.example` lists names only.
- **Scope priority: core functionality first.** In order: reliable natural language → SQL, rule proposal with backtesting, the evaluation suite. The interface stays deliberately minimal until those work.
- **Be honest in docs.** The README must describe only what exists. Mark anything unbuilt as "to be completed" or "proposed", and do not describe it in the present tense.
- **One task, one branch, one pull request.** Each task ships as its own branch and pull request, and its docs are updated in the same pull request: the decision entries it settles, the status it flips from Proposed to Built in `docs/ARCHITECTURE.md`, the current-state paragraph below and the README sections it affects. Only the user merges.
- **No dates about our work in docs** (no "as of", "last updated", dated decision entries). Dates that are facts about the data are fine.
- **Record decisions** in `docs/DECISIONS.md` (next D-number; never rewrite old entries), then update `docs/ARCHITECTURE.md` to match. Do not settle an item listed under "Open points" without the user.
- **No new dependencies without asking.** One runtime dependency is installed: `pydantic` (v2), approved for typed, immutable data models (D-19). The dev tools are `pytest` and `mypy`. The model provider is decided: Anthropic (Claude), through `langchain-anthropic`, with `langgraph` for the turn graph (D-14, D-15). Those two packages are approved and are added by the task that builds the model layer, not before. Nothing else is added without asking: no `langchain` meta-package, no direct `anthropic` SDK, no second provider.
- **No paid model call without the user's go-ahead for that run.** Tests never call a model.

## What this project is

A take-home submission for Checkout.com's "Fraud Success Manager Assistant Challenge" (full brief: `docs/CHALLENGE.md`). A Fraud Success Manager (FSM) explores transaction data, validates fraud hypotheses, and turns patterns into rules. The assistant should:

1. Translate natural-language questions into accurate SQL over the provided SQLite database (the brief's suggested MVP).
2. Help turn an insight into a **rule**: a valid SQL `WHERE` clause over the `transactions` table, backtested against `fraud_labels`.
3. Ship with an initial evaluation suite, and a README that justifies the design, covers GenAI limitations, and describes offline and online metrics.

Current state: the data layer and the SQL guard are built, and the target design is written down. `config.py`, `db.py` (`run_query` to execute, `inspect_query` to compile without running), `schema_context.py`, `sql_scan.py` (the SQL text scanner), `sql_guard.py` (`check_query`, `check_rule_clause`) and `rules/backtest.py` are implemented and tested. `backtest` applies the guard's rule policy itself; nothing else calls the guard yet. The package is fully annotated and passes `mypy` in strict mode, and every structured value it passes between modules is a frozen pydantic model. Everything else is proposed: the other modules are docstring-only, the only runtime dependency is `pydantic`, and there is no model call, turn graph, rule proposal or evaluation suite yet. The design they will follow is in `docs/ARCHITECTURE.md`; decisions D-14 to D-16 (Claude, a fixed LangGraph graph, five tools) are made but not built. `config.py`, `.env.example` and some placeholder docstrings still mention the earlier local provider (Ollama); they are replaced when the model layer is built.

## Where to find things

| Path | What |
|---|---|
| `README.md` | Submission README (the brief requires setup, solution explanation, architecture, design choices, reproduction steps) |
| `docs/CHALLENGE.md` | The original brief, verbatim. Do not edit |
| `docs/ARCHITECTURE.md` | Target design: system diagram, how one turn works, which parts are built and which proposed, evidence, known weaknesses, open points |
| `docs/DATA.md` | The real schema, row counts, and where the data differs from the brief |
| `docs/DECISIONS.md` | Decision log (D-01, D-02, …) |
| `data/` | `data.db` (SQLite, about 100 MB, WAL mode) and `schema.sql`. Read-only |
| `src/fsm_assistant/` | The package. One module per architecture component; each docstring states its responsibility |
| `tests/` | pytest tests (offline, no model calls). Unit tests use small fixture databases; `test_real_db.py` runs against `data/data.db` and is skipped if it is missing |
| `evals/` | Evaluation suite (empty; see `evals/README.md`) |
| `.env.example` | Names of the environment variables |

New project docs go in `docs/`. `README.md` stays at the root because the brief requires it.

## Commands

```bash
uv sync                  # create .venv, install the package (editable) and dev tools
uv run fsm-assistant     # run the entry point (placeholder for now)
uv run pytest            # run the tests
uv run mypy              # type-check src/ (strict) and tests/
```

Python 3.13, managed by uv. The package uses a `src/` layout built with `uv_build`.

## Conventions

- One responsibility per module, matching `docs/ARCHITECTURE.md`. Only `db.py` opens the database (every query goes through `db.run_query`); only `llm.py` talks to a model provider.
- Safety is enforced in code, not only in the prompt. Model-written SQL reaches `db` only after `sql_guard` accepted it: `db.inspect_query` has no timeout, and the guard's text limits are what bound a compile. A rule is measured only through `backtest`, which applies the rule policy itself.
- Numbers shown to the user (counts, fraud rates, precision, recall) come from executed SQL, never from model text.
- **Typed code, pydantic data (D-19).** `uv run mypy` must pass before a change is done, as `uv run pytest` must.
  - Every function and method has complete parameter and return annotations. No implicit `Any`; an explicit `Any` needs a one-line comment saying why (SQL bind parameters are the one case today).
  - Structured data is a frozen pydantic model (`model_config = ConfigDict(frozen=True, extra="forbid")`): settings, results, decisions, and later tool arguments, graph state, evaluation cases and API bodies. No dataclass, `NamedTuple` or `TypedDict` for these.
  - A closed set of values is a `Literal` or an enum, not a bare `str`.
  - No untyped `dict` passed between modules. A dict is fine inside one function, for example on its way into `model_validate`.
  - Exceptions stay ordinary exception classes with typed attributes.
  - Private working state that is updated in place and never leaves its module (`sql_guard._OpenCall`) may be a plain class.
  - No `# type: ignore` without an error code and a reason on the same line.
- Tests run offline. No test calls a real model; once code depends on one, tests use a scripted fake. Anything that calls a real model belongs in `evals/`.
- Small, explainable changes. Say what changed and why.

## Data gotchas

Full detail in `docs/DATA.md`. The ones that cause wrong SQL:

- The brief's example rule uses `amount` and `'debit'`. The real column is `amount_usd_cents` (integer cents) and `card_type` is one of `'Credit'`, `'Debit'`, `'Debit (Prepaid)'` (case-sensitive).
- Two tables are not in the brief: `merchants` and `merchant_locations`. MCC is reached through `transactions.merchant_id → merchants.mcc`.
- `fraud_labels.transaction_id` is TEXT; `transactions.id` is INTEGER. Join with `f.transaction_id = CAST(t.id AS TEXT)`: the uncast form is correct but runs away (every transaction against every label) as a `LEFT JOIN` or `EXISTS` from `transactions`.
- `transactions.date` holds timestamps. End a day range with `< 'next day'`; `BETWEEN '2019-01-01' AND '2019-01-31'` drops the 31st.
- Join locations on `transactions.merchant_location_id = merchant_locations.id`, never on `merchant_id` (about 440 million rows).
- All 1,360 fraud labels are at merchant locations in Rome, Italy. Towns named Rome also exist in GA and NY, so read `city` with `state`.
- Only about 67% of transactions have a fraud label (777,339 of 1,159,966). An unlabelled transaction is unknown, not legitimate: compute fraud metrics with an inner join.
- Fraud is rare: 1,360 of the labelled transactions (about 0.17%).
- `amount_usd_cents` can be negative (56,900 rows).
- The database is in WAL mode; `data.db-shm` and `data.db-wal` are git-ignored sidecar files that a read-only connection creates on its first query, so the `data/` directory must be writable.
- A string in double quotes is a name, not text: `"amount" > 1000` is an error on the application's connection. A filter relative to today (`date('now')`) matches nothing, because the data ends on 2019-10-31; the guard rejects it.
