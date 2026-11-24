# FSM Assistant

A prototype assistant for Fraud Success Managers (FSMs): ask questions about transaction data in natural language, and turn the patterns found into fraud rules. A submission for the "Fraud Success Manager Assistant Challenge"; the original brief is preserved in [`docs/CHALLENGE.md`](docs/CHALLENGE.md).

> **Status: work in progress.** Built and tested so far: configuration, read-only query execution (row cap, timeout, write protection), the schema context that will ground the model, and the rule backtest (a `WHERE` clause measured against the fraud labels), which is a Python function with no interface yet. **No assistant functionality is implemented yet**: nothing calls a model, so there is no natural-language → SQL, no rule proposal and no evaluation suite. Sections marked _To be completed_ will be filled in as the work is done.

## Contents

- [Problem framing](#problem-framing)
- [Setup](#setup)
- [How to run](#how-to-run)
- [How to reproduce](#how-to-reproduce)
- [Solution](#solution)
- [Architecture](#architecture)
- [Design choices and trade-offs](#design-choices-and-trade-offs)
- [GenAI limitations and mitigations](#genai-limitations-and-mitigations)
- [Evaluation](#evaluation)
- [Data: differences from the brief](#data-differences-from-the-brief)
- [Left out and next steps](#left-out-and-next-steps)
- [Repository layout](#repository-layout)

## Problem framing

An FSM's workflow, as the brief describes it, is: explore data to form hypotheses, validate them against history, and translate the patterns into fraud rules. It is iterative, and each loop needs SQL fluency and domain knowledge.

The planned scope follows the brief's MVP guidance, in priority order:

1. **Natural language → SQL** for data exploration, reliable enough to trust. The brief names this as the core feature.
2. **Insight → rule.** A rule is a valid SQL `WHERE` clause over the `transactions` table. A proposed rule is only useful with evidence, so each one is backtested against `fraud_labels`.
3. **An evaluation suite** that measures whether the above is accurate and safe.

The interface is deliberately minimal so that effort goes into these three.

_Product reasoning (who the user is, what "effective" means for them, which interactions matter most): to be completed._

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 if needed). Tested on macOS.

```bash
git clone <repository-url>
cd fsm-assistant-challenge-python
uv sync
```

`uv sync` creates `.venv`, installs the `fsm_assistant` package in editable mode and installs the dev tools (pytest). There are no runtime dependencies yet.

**Data.** The SQLite database is in the repository at `data/data.db` (about 100 MB), with its schema in `data/schema.sql`. No download or import step is needed. The application treats it as read-only.

**Model (Ollama).** Not needed yet: nothing calls a model, and the tests run offline. Once the assistant exists it will use a Qwen model running locally in [Ollama](https://ollama.com), so there is no API key and nothing is sent to a hosted service. To prepare:

```bash
# install Ollama from https://ollama.com/download, then pull one of the two candidate models
ollama pull qwen3:14b        # 9.3 GB download
ollama pull qwen3:30b-a3b    # 18 GB download
```

The machine needs enough memory to load the chosen model. Which of the two is the default is not chosen yet; it will be picked by running the evaluation suite against both. _Default model, tested Ollama version and memory needed: to be completed._

**Configuration.** Optional: every setting has a default, so nothing needs to be set to run the tests. Settings are environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `FSM_DB_PATH` | `data/data.db` | SQLite database. A relative path is resolved against the project root, not the current directory |
| `FSM_ROW_CAP` | `1000` | Maximum rows a query returns; a longer result is cut and flagged as truncated |
| `FSM_QUERY_TIMEOUT_SECONDS` | `15` | A query still running after this long is interrupted |
| `OLLAMA_HOST` | `http://localhost:11434` | Address of the Ollama server (not used yet) |
| `OLLAMA_MODEL` | none | Ollama model tag (not used yet; will be required only when a model is called) |

To keep them in a file, copy `.env.example` to `.env` (git-ignored) in the project root:

```bash
cp .env.example .env
```

`.env` is read by a small parser in `config.py`, not by a dotenv library: plain `KEY=VALUE` lines and `#` comment lines only (no `export`, no inline comments). A real environment variable overrides the file, and an empty value means "use the default". An invalid value (for example `FSM_ROW_CAP=abc`) stops with an error naming the variable.

## How to run

```bash
uv run fsm-assistant
```

Today this prints a placeholder line (`fsm-assistant: scaffold only, no functionality implemented yet.`). `uv run python -m fsm_assistant` does the same.

_Usage of the real assistant, with example questions and output: to be completed._

## How to reproduce

What can be reproduced today:

```bash
uv sync          # install
uv run pytest    # a few seconds, offline (no model needed)
```

The tests cover configuration, read-only execution, the schema context and the rule backtest. Most use small databases built in a temp directory from `data/schema.sql`, where every backtest metric is computed by hand; those in `tests/test_real_db.py` run against `data/data.db`, check the data facts the schema context states and compare the backtest with independently written queries, and are skipped if the file is missing. The database is opened read-only and the suite checks it is unchanged afterwards.

To see the schema description that will be given to the model:

```bash
uv run python -c "from fsm_assistant.config import load_settings; from fsm_assistant.schema_context import build_schema_context; print(build_schema_context(load_settings().db_path))"
```

To backtest a rule (here the brief's example, adapted to the real schema) without any model:

```bash
uv run python -c "
from fsm_assistant.config import load_settings
from fsm_assistant.rules.backtest import backtest
s = load_settings()
r = backtest(\"amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')\", db_path=s.db_path, timeout_seconds=s.query_timeout_seconds)
print(r)
print('precision', r.precision, 'recall', r.recall, 'unlabelled', r.flagged_unlabelled)
"
```

On the shipped data this rule flags 350 transactions, of which 238 are labelled: 2 fraud and 236 legitimate (precision 0.8%, recall 2 of 1,360), and 112 unlabelled.

_To be completed: the single command that runs the evaluation suite, where it writes results, the model and settings used for the reported numbers, and expected run time and cost._

## Solution

_To be completed once implemented._ This section will explain what the assistant does, walk through one exploration question end to end (question → SQL → result) and one rule proposal (insight → `WHERE` clause → backtest).

## Architecture

**Partly implemented**: configuration, read-only execution, the schema context and the rule backtest are built; generation, guardrails on the SQL text, presentation, rule proposal and the evaluation harness are proposed. Full description, diagram, status per component and open decisions: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

In short: the model writes SQL and prose; code decides what runs and computes every number.

```
question ─► NL→SQL generation ─► SQL guardrails ─► read-only execution ─► result + SQL shown
               ▲ schema context      │ reject: one repair attempt
               │                     ▼
insight ──► rule proposal ─► (same guardrails) ─► backtest vs fraud_labels ─► rule + precision/recall
```

The diagram is the target design, not what runs today. The evaluation harness (proposed) will run fixed cases through this same pipeline.

What exists today:

- **Read-only execution** (`db.py`): every query runs through one function, on a connection that cannot write. Three layers enforced by SQLite itself, not by inspecting the SQL text: the file is opened read-only, `PRAGMA query_only` is on, and an authorizer callback denies everything except reads (including `ATTACH`, temp tables and `PRAGMA` changes, which a read-only file alone does not stop). A query is interrupted after a timeout, and at most a fixed number of rows is fetched, with a flag when the result was cut. The SQL is never rewritten.
- **Schema context** (`schema_context.py`): the description of the database that will go into every prompt, read from the database itself (tables, columns, types, keys, row counts, the actual values of categorical columns) plus short notes on what a model would otherwise get wrong (amounts in cents, case-sensitive values, how to join the fraud labels, partial label coverage). It is deterministic, about 3,300 characters.
- **Rule backtest** (`rules/backtest.py`): `backtest(clause, db_path=…, timeout_seconds=…)` takes a rule as a `WHERE` clause over `transactions` and measures it with one fixed SQL template: transactions flagged, how many of those are labelled, fraud caught, false positives, precision, recall, flag rate, and the amounts involved in cents. Precision and recall use labelled transactions only, and the unlabelled part of the flagged set is reported next to them; a ratio with a zero denominator is "undefined", not 0. The clause is untrusted text: it is wrapped in parentheses inside a subquery that sees only `transactions`, and rejected with a reason if it contains a statement separator, a comment, a bind parameter or a parenthesis that would close the template's own. It is not yet called by anything: there is no rule proposal and no interface. It does not check which tables a clause reads; that belongs to the SQL guard, which is not built (so, for example, a clause that reads `fraud_labels` in a subquery is not refused).
- **Configuration** (`config.py`): see [Setup](#setup).

## Design choices and trade-offs

Decisions made so far (full entries in [`docs/DECISIONS.md`](docs/DECISIONS.md)):

| Choice | Why | Cost |
|---|---|---|
| Python 3.13 + uv, `src/` layout | One-command, locked, reproducible setup | Reviewers need uv |
| Core functionality before interface | The brief asks to focus on features and their evaluation | A plain first interface |
| Model-written SQL validated in code and run on a read-only connection | A prompt instruction cannot be enforced; two independent layers can | Needs a validator |
| Real schema as source of truth | The data differs from the brief (see below) | Rules will not match the brief's example literally |
| Read-only enforced by the engine in three layers (read-only file, `query_only`, authorizer) | Does not depend on parsing SQL; a read-only file alone still allows `ATTACH` and temp tables | SQLite creates two git-ignored sidecar files next to the WAL-mode database |
| Query timeout through SQLite's progress handler | SQLite has no statement timeout, and a mistaken join on this data runs for a very long time | Checked between VM instructions, not continuously |
| Row cap applied when fetching, never by editing the SQL | The FSM sees exactly the SQL that ran; truncation is reported | A truncated result is not the full answer and must be flagged |
| `.env` read by a small built-in parser | No dependency for twenty lines of code | Only plain `KEY=VALUE` lines |
| Schema context read from the live database, with one prescribed label join | Cannot drift from the data; the uncast join is a performance trap | About a second to build, once per process |
| Rule metrics on labelled transactions only; undefined ratios are "undefined" | An unlabelled transaction is unknown, not legitimate; "0% precision" for a rule that flagged nothing labelled would mislead | Every figure needs its coverage caveat shown with it |
| One backtest template; the rule is evaluated in a subquery that sees only `transactions` | Rules are written with bare column names, as in the brief; `id` cannot be confused with a label column | The dataset totals are recomputed on every backtest (well under a second) |
| Rule text contained by parentheses plus a lexical scan (no `;`, comments, bind parameters, unbalanced parentheses) | A rule must not be able to change what is measured; needs no SQL parser | `--` is refused even where it would be valid arithmetic; table and column allow-listing is left to the SQL guard |
| Local model: Ollama running Qwen | No API key or per-call cost; schema and transaction data stay on the machine; reproducible with `ollama pull` | Small local models are weaker and less consistent at SQL than large hosted ones; reviewers need Ollama, a large download and enough memory |

The model name is configuration, and only `llm.py` talks to the model, so another provider could be swapped in.

Still open (see [Open decisions](docs/ARCHITECTURE.md#open-decisions)): which of the two Qwen models (`qwen3:14b`, `qwen3:30b-a3b`) is the default, interface (CLI vs minimal web UI), evaluation dataset design.

_Justification of the final architecture and interaction model: to be completed._

## GenAI limitations and mitigations

Mitigations by risk. Implemented so far: the read-only connection, timeout and row cap, the schema context, and the rule backtest (not yet wired to a model or interface); the rest is planned. Where each sits in the design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#where-hallucination-mitigations-sit).

| Risk | Mitigation |
|---|---|
| Invented tables, columns or values | Schema context from the real database, including the actual values of categorical columns (implemented); identifiers checked before execution (planned) |
| Destructive or runaway SQL | Read-only connection, timeout, row cap (implemented); `SELECT`-only validation of the SQL text (planned) |
| SQL that runs but answers the wrong question | SQL always shown with the result; execution accuracy measured in evals (both planned) |
| Made-up numbers | Every figure comes from an executed query, never from model text (implemented for backtest figures; planned for query results) |
| Plausible but poor rules | The backtest that measures a rule against the fraud labels (implemented); no rule shown without one, and the FSM decides, the tool does not deploy (planned, with rule proposal) |
| Misleading fraud rates | The schema context tells the model the coverage and to use labelled rows only (implemented); rule metrics on labelled transactions only, with the unlabelled share of the flagged set reported (implemented in the backtest; presentation planned) |
| A small local model writes wrong or inconsistent SQL more often than a large hosted one | The same guardrails, schema context and single repair attempt, none of which trust the model; the evaluation suite run against both candidate Qwen models to measure how often it happens (schema context implemented; the rest planned) |

_What was actually built, what was observed in testing, and residual risks: to be completed._

## Evaluation

The brief asks how to show a stakeholder such as the Head of Fraud that the tool is effective and safe, with offline and online metrics, and for an initial evaluation suite. **The suite does not exist yet**; [`evals/README.md`](evals/README.md) describes what will live there. The metrics below are the plan, not results.

**Offline (planned)**

- Execution accuracy: generated SQL returns the same result as a reference query, by difficulty tier.
- Safety: share of unsafe inputs (write attempts, injection) that reach execution; target zero.
- Refusal quality: unanswerable or ambiguous questions are refused or clarified rather than answered.
- Rule quality: proposed rules are valid, and their precision/recall against `fraud_labels` is reported correctly.
- Latency per question (the model runs locally, so there is no per-call cost).
- The same suite run against both candidate Qwen models, to choose the default and show the difference.

**Online (proposed, would need a deployment)**

- Time from question to validated insight, and from insight to deployed rule, against the current workflow.
- Share of generated queries the FSM runs unedited versus corrects.
- Share of proposed rules accepted, and their live precision and false-positive rate after deployment.
- Fraud value prevented versus legitimate payments blocked.
- FSM-reported errors and trust (feedback on each answer).

_Results, eval set description and known failure cases: to be completed._

## Data: differences from the brief

The database does not match the brief's description in several ways. Details and row counts: [`docs/DATA.md`](docs/DATA.md).

- The amount column is `amount_usd_cents` (integer cents), not `amount`.
- `card_type` values are `'Credit'`, `'Debit'`, `'Debit (Prepaid)'`, not `'debit'`.
- There are two tables the brief does not list: `merchants` and `merchant_locations`.
- `fraud_labels.transaction_id` is TEXT; `transactions.id` is INTEGER. The join needs `CAST(transactions.id AS TEXT)` to stay fast in every form.
- Only about 67% of transactions have a fraud label (777,339 of 1,159,966), and about 0.17% of labelled transactions are fraud (1,360). Unlabelled is treated as unknown, not as legitimate.

So the brief's example rule, `amount > 1000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'debit')`, does not run as written. Its equivalent on this data is `amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')`.

## Left out and next steps

Configuration, read-only execution, the schema context and the rule backtest are done. Still to do, in planned order:

1. Natural language → SQL: the model client, generation, validation of the SQL text, presentation.
2. Rule proposal (insight → clause), validation of the clause's tables and columns, and showing a rule with its backtest.
3. Evaluation suite for both.
4. Minimal interface polish.

_What was cut for time and how it would be built: to be completed at the end, as the brief requests._

## Repository layout

```
README.md              This file
AGENTS.md, CLAUDE.md   Pointers to docs/AGENTS.md (working rules for coding agents)
docs/
  CHALLENGE.md         The original brief, verbatim
  ARCHITECTURE.md      Architecture (implemented and proposed parts), data flow, open decisions
  DATA.md              Schema notes and differences from the brief
  DECISIONS.md         Decision log
  AGENTS.md            Working rules for coding agents
data/                  data.db (SQLite) and schema.sql; read-only
src/fsm_assistant/     The package. Implemented: config.py, db.py, schema_context.py,
                       rules/backtest.py; the other modules are docstring-only placeholders
tests/                 pytest tests (fixture databases built from data/schema.sql)
evals/                 Evaluation suite (empty; see its README)
```
