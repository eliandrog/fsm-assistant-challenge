# FSM Assistant

A prototype assistant for Fraud Success Managers (FSMs): ask questions about transaction data in natural language, and turn the patterns found into fraud rules. A submission for the "Fraud Success Manager Assistant Challenge"; the original brief is preserved in [`docs/CHALLENGE.md`](docs/CHALLENGE.md).

> **Status: work in progress.** **Built** and tested: configuration, read-only query execution (row cap, timeout, write protection), the schema context that will ground the model, the rule backtest (a `WHERE` clause measured against the fraud labels) and the SQL guard (code that decides whether a query or a rule may run, without running it). These are Python functions with no interface yet. **Proposed**, with the design written down but no code: everything that involves a model. Nothing calls a model yet, so there is no natural-language → SQL, no rule proposal and no evaluation suite. Each section below says what is built and what is proposed; parts marked _To be completed_ are filled in as the work is done.

## Contents

- [What it is](#what-it-is)
- [Setup](#setup)
- [How to run](#how-to-run)
- [How to reproduce](#how-to-reproduce)
- [Solution overview](#solution-overview)
- [Design choices and trade-offs](#design-choices-and-trade-offs)
- [GenAI limitations and mitigations](#genai-limitations-and-mitigations)
- [Evaluation](#evaluation)
- [Data: differences from the brief](#data-differences-from-the-brief)
- [What is not built yet](#what-is-not-built-yet)
- [Repository layout](#repository-layout)

## What it is

An FSM's workflow, as the brief describes it, is: explore data to form hypotheses, validate them against history, and translate the patterns into fraud rules. It is iterative, and each loop needs SQL fluency and domain knowledge.

The scope follows the brief's MVP guidance, in priority order:

1. **Natural language → SQL** for data exploration, reliable enough to trust. The brief names this as the core feature.
2. **Insight → rule.** A rule is a valid SQL `WHERE` clause over the `transactions` table. A proposed rule is only useful with evidence, so each one is backtested (measured against history) using `fraud_labels`.
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

`uv sync` creates `.venv`, installs the `fsm_assistant` package in editable mode with its one runtime dependency (`pydantic`, for typed data models) and installs the dev tools (pytest, mypy).

**Data.** The SQLite database is in the repository at `data/data.db` (about 100 MB), with its schema in `data/schema.sql`. No download or import step is needed. The application treats it as read-only. The `data/` directory itself must be writable: the database is in WAL mode, and SQLite creates two git-ignored sidecar files next to it on the first read ([`docs/DATA.md`](docs/DATA.md#access)).

**Model.** Not needed yet: nothing calls a model, and the tests run offline. The assistant will use Claude through the Anthropic API ([D-14](docs/DECISIONS.md#d-14-model-provider-anthropic-claude-through-its-api)), which needs an API key. _The key setup (`ANTHROPIC_API_KEY` in `.env`) and the model settings arrive with the model layer: to be completed._ The evaluation suite is designed to be replayed offline from recorded model replies, so checking the reported results will not need a key.

**Configuration.** Optional: every setting has a default, so nothing needs to be set to run the tests. Settings are environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `FSM_DB_PATH` | `data/data.db` | SQLite database. A relative path is resolved against the project root, not the current directory |
| `FSM_ROW_CAP` | `1000` | Maximum rows a query returns; a longer result is cut and flagged as truncated |
| `FSM_QUERY_TIMEOUT_SECONDS` | `15` | A query still running after this long is interrupted |
| `FSM_MAX_RESULT_BYTES` | `5000000` | Largest result a query may return, counted in bytes of its values; a larger one is an error, not a cut result |

To keep them in a file, copy `.env.example` to `.env` (git-ignored) in the project root:

```bash
cp .env.example .env
```

`.env` is read by a small parser in `config.py`, not by a dotenv library: plain `KEY=VALUE` lines and `#` comment lines only (no `export`, no inline comments). A real environment variable overrides the file, and an empty value means "use the default". An invalid value (for example `FSM_ROW_CAP=abc`) stops with an error naming the variable.

`config.py` and `.env.example` still carry two settings from an earlier choice of a local model (`OLLAMA_HOST`, `OLLAMA_MODEL`). Nothing uses them; leave them as they are. They are replaced by the Claude settings when the model layer is built.

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
uv run pytest    # a few seconds, offline (no model, no key)
uv run mypy      # type check: strict on src/ and tests/
```

The tests cover configuration, read-only execution, the schema context, the rule backtest and the SQL guard (each accepted query shape, and each rejection with its reason). Most use small databases built in a temp directory from `data/schema.sql`, where every backtest metric is computed by hand; those in `tests/test_real_db.py` run against `data/data.db`, check the data facts the schema context states and compare the backtest with independently written queries, and are skipped if the file is missing. The database is opened read-only and the suite checks it is unchanged afterwards.

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

To ask the guard about a query and about two rules (nothing is run; the third is rejected because it reads the labels):

```bash
uv run python -c "
from fsm_assistant.config import load_settings
from fsm_assistant.sql_guard import check_query, check_rule_clause
p = load_settings().db_path
print(check_query(\"SELECT count(*) FROM transactions WHERE date >= date('now', '-30 day')\", db_path=p).reason)
print(check_rule_clause(\"amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')\", db_path=p).accepted)
print(check_rule_clause('id IN (SELECT CAST(transaction_id AS INTEGER) FROM fraud_labels WHERE is_fraud = 1)', db_path=p).reason)
"
```

_To be completed: the single command that replays the evaluation suite offline, where it writes results, the model and settings used for the reported numbers, and the run time and cost of a live run._

## Solution overview

**Proposed; the target design, not what runs today.** Full description, diagrams and the status of every component: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

The principle: the model only chooses and writes; code decides what runs, runs it and computes every number. For each message, Claude will be given the real schema and will pick exactly one of five actions: answer with a SQL query, propose a rule, ask for a fixed fraud scan, ask the FSM what was meant, or decline. Code will then check the SQL without running it, accept only a single read-only `SELECT` over the known tables, run it on a read-only connection with a timeout and a row cap, and show the SQL next to the result. A rule is a `WHERE` clause and will never be shown without its backtest against the fraud labels. If the first attempt fails, the model will get the error once; a second failure will be shown as a failure.

```
question ─► Claude picks one of five actions ─► SQL guard ─► read-only execution ─► SQL + result + caveats
              ▲ real schema, glossary             │ reject or error: one repair attempt
              │                                   ▼
              └─ rule clause ─► (same guard) ─► backtest vs fraud_labels ─► rule + precision/recall
```

What exists today:

- **Read-only execution** (`db.py`): every query runs through one function, on a connection that cannot write. Three layers enforced by SQLite itself, not by inspecting the SQL text: the file is opened read-only, `PRAGMA query_only` is on, and an authorizer callback denies everything except reads (including `ATTACH`, temp tables and `PRAGMA` changes, which a read-only file alone does not stop). A query is interrupted after a timeout, at most a fixed number of rows is fetched, with a flag when the result was cut. Size is bounded too, because a few rows of very large values could otherwise take hundreds of megabytes: no single value may exceed 100,000 bytes, a result has at most 100 columns, and a result over a byte budget (5 MB by default) is an error. A double-quoted name that does not exist is an error, not a string. The SQL is never rewritten. A second function, `inspect_query`, compiles a statement without running it and reports the tables, columns and functions it would use.
- **SQL guard** (`sql_guard.py`): `check_query` and `check_rule_clause` decide whether a model-written query or rule may run, from what SQLite reports when it compiles the statement, and return a decision with a reason that can be sent back to the model. A query must be one read-only `SELECT` over the database's own seven tables (not `sqlite_master`, `PRAGMA`, `EXPLAIN` or table-valued functions), and must not filter relative to today's date, because the data ends in 2019 and such a filter silently returns nothing. A rule must also not read `fraud_labels` or call `random()`. Before anything is compiled the text itself is checked, because compiling is not free: at most 10,000 characters, no Markdown code fence (refused with a request for the bare statement, never unwrapped), and a limit on how often common table expressions refer to each other, since a short statement built that way can take SQLite many seconds to compile. An ordinary check takes under a millisecond, and the worst input found that passes the text checks under 0.1 s. The rule backtest calls the guard; no model-driven turn does yet: it becomes a step of the turn when the graph is built.
- **Schema context** (`schema_context.py`): the description of the database that will go into every prompt, read from the database itself (tables, columns, types, keys, row counts, the actual values of categorical columns) plus short notes on what a model would otherwise get wrong (amounts in cents, case-sensitive values, how to join the fraud labels, partial label coverage). It is deterministic, about 3,300 characters.
- **Rule backtest** (`rules/backtest.py`): `backtest(clause, db_path=…, timeout_seconds=…)` takes a rule as a `WHERE` clause over `transactions` and measures it with one fixed SQL template: transactions flagged, how many of those are labelled, fraud caught, false positives, precision, recall, flag rate, and the amounts involved in cents. Precision and recall use labelled transactions only, and the unlabelled part of the flagged set is reported next to them; a ratio with a zero denominator is "undefined", not 0. The clause is untrusted text: it is wrapped in parentheses inside a subquery that sees only `transactions`, and rejected with a reason if it contains a statement separator, a comment, a bind parameter or a parenthesis that would close the template's own. It applies the guard's rule policy itself before measuring, so a clause that reads the labels is refused with the guard's reason and cannot be scored by calling the backtest directly. It is not yet called by anything.
- **Configuration** (`config.py`): see [Setup](#setup).

_A walk through one real exploration question and one real rule proposal, with output: to be completed once implemented._

## Design choices and trade-offs

Full entries, with the alternatives rejected, are in [`docs/DECISIONS.md`](docs/DECISIONS.md). The first fifteen rows are about built code; the last three are decided and not built yet.

| Choice | Why | Cost |
|---|---|---|
| Python 3.13 + uv, `src/` layout | One-command, locked, reproducible setup | Reviewers need uv |
| Core functionality before interface | The brief asks to focus on features and their evaluation | A plain first interface |
| Model-written SQL validated in code and run on a read-only connection | A prompt instruction cannot be enforced; two independent layers can | Two layers to keep consistent |
| Real schema as source of truth | The data differs from the brief (see below) | Rules will not match the brief's example literally |
| Read-only enforced by the engine in three layers (read-only file, `query_only`, authorizer) | Does not depend on parsing SQL; a read-only file alone still allows `ATTACH` and temp tables | SQLite creates two git-ignored sidecar files next to the WAL-mode database |
| Query timeout through SQLite's progress handler | SQLite has no statement timeout, and a mistaken join on this data runs for a very long time | Checked between VM instructions, not continuously |
| Row cap applied when fetching, never by editing the SQL | The FSM sees exactly the SQL that ran; truncation is reported | A truncated result is not the full answer and must be flagged |
| `.env` read by a small built-in parser | No dependency for twenty lines of code | Only plain `KEY=VALUE` lines |
| Schema context read from the live database, with one prescribed label join | Cannot drift from the data; the uncast join is a performance trap | About a second to build, once per process |
| Rule metrics on labelled transactions only; undefined ratios are "undefined" | An unlabelled transaction is unknown, not legitimate; "0% precision" for a rule that flagged nothing labelled would mislead | Every figure needs its coverage caveat shown with it |
| One backtest template; the rule is evaluated in a subquery that sees only `transactions` | Rules are written with bare column names, as in the brief; `id` cannot be confused with a label column | The dataset totals are recomputed on every backtest (well under a second) |
| Rule text contained by parentheses plus a lexical scan (no `;`, comments, bind parameters, unbalanced parentheses) | A rule must not be able to change what is measured; needs no SQL parser | `--` is refused even where it would be valid arithmetic; table and column allow-listing is left to the SQL guard |
| The SQL guard compiles a statement without running it and judges what SQLite reports (tables, columns, functions), not the text | SQLite resolves aliases, CTEs and `SELECT *` itself, so the guard cannot disagree with what will run; no SQL parser dependency | Depends on the engine's reporting, pinned by tests; date arguments still need a small text check; compiling has no timeout, so the text is bounded first (length, and how often CTEs refer to each other), which can refuse an unusually CTE-heavy statement |
| Unknown double-quoted names are errors; values capped at 100,000 bytes, results at 100 columns and 5 MB | `"amount" > 1000` used to match every row; one huge value could outrun the timeout; the row cap alone let 300 rows take 340 MB | A string written in double quotes is refused; a very long `group_concat` or a very large result is an error |
| Every structured value is a frozen pydantic model; the code is fully annotated and checked by mypy in strict mode | One way to write a data type, validated where data enters and immutable afterwards; a wrong field or type fails the check, not a run | A second command to keep passing; query results skip per-cell validation for speed, which a test covers |
| Model: Claude through the Anthropic API (decided, not built) | Stronger SQL than the small local models tried first, and no multi-gigabyte download for a reviewer; offline replay of the evaluation means checking results needs no model | An API key and a cost per call for live use; the question and schema are sent to a hosted service |
| One turn is a fixed state graph in LangGraph, not an agent (decided, not built) | See the paragraph below | Two dependencies; no multi-step reasoning within a turn |
| The model picks one of five tools; code executes it (decided, not built) | Code can route on a typed action instead of parsing free text; the model never sees a result | The default model does not accept a forced tool choice, so a plain-text reply is possible and uses the one repair |

**Why LangGraph, and why not an agent.** An agent is a loop in which the model sees the result of each action and chooses the next one. That is flexible, but it could not guarantee that every query passes the guard, or that a turn costs at most two model requests, and result rows would pass through the model, which could then restate them wrongly. So a turn will be a fixed graph instead: the steps and the routes between them are declared in code, and the model's only job is to pick one action and write it. LangGraph was chosen for that graph, rather than plain functions, because the structure is declared in one place and the diagram is drawn from the code, conversation memory comes as a store keyed by conversation, and each step can be traced. The cost is that the assistant will not chain several queries in one turn; the FSM asks a follow-up. ([D-15](docs/DECISIONS.md#d-15-langgraph-as-a-fixed-state-graph-not-an-agent-loop))

The model name is configuration, and only `llm.py` will talk to the model, so a second provider (for example a local model) could be added behind the same interface. It is not planned.

Still open (see [Open points](docs/ARCHITECTURE.md#open-points)): whether tools or a single structured object is the default output mode (to be measured), the model's effort level, whether the smaller Claude model becomes the default, and whether the web chat is built.

_Justification of the final interaction model, with what was observed: to be completed._

## GenAI limitations and mitigations

Mitigations by risk, and whether each is built. Where each sits in the design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#where-hallucination-mitigations-sit).

| Risk | Built | Proposed |
|---|---|---|
| Invented tables, columns or values | Schema context from the real database, including the actual values of categorical columns; the guard checks tables against the real ones and keeps SQLite's error on an unknown column | That error fed back to the model for one repair |
| Destructive or runaway SQL | Read-only connection, timeout, row cap, value-size cap, result-size budget; a guard that bounds the text, compiles the SQL without running it and accepts a single read-only `SELECT` | The guard wired into the turn, so nothing runs without passing it |
| SQL that runs but answers the wrong question | none yet | SQL always shown with the result, with the model's assumptions listed; execution accuracy measured in the evaluation suite |
| Made-up numbers | Backtest figures come from one fixed query | Every figure from executed SQL; the model is never shown result rows, and its text is labelled as model text |
| Plausible but poor rules | The backtest that measures a rule against the fraud labels | No rule shown without one; evidence month by month; the FSM decides, the tool does not deploy |
| A rule that reads the labels it is scored on | The guard rejects any clause that reads `fraud_labels`, and the backtest applies the guard itself. Not detectable: a clause that lists fraud transaction ids as literals | The guard wired into the turn |
| An empty result that looks like an answer (a filter relative to today, on data that ends in 2019) | The guard rejects `'now'` given to a date function and date functions that default to the current time | Relative dates resolved against the last date in the data |
| Misleading fraud rates from partial labels | The schema context tells the model the coverage and to use labelled rows only; rule metrics on labelled transactions only, with the unlabelled share reported | Coverage caveat shown with every such result |
| Vague or unanswerable questions answered anyway | none yet | The model can ask what was meant or decline; such cases are in the evaluation set |
| A rule that fits history and nothing else | none yet | Scoring on months the rule was not derived from |

Known weaknesses found in exploratory checks, each with its planned fix, are listed in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#known-weaknesses-and-planned-fixes). One matters for reading any result on this data: all 1,360 labelled frauds are at merchant locations in Rome, Italy, so strong backtest numbers here do not prove that a rule generalises.

_What was observed in testing with the real model, and residual risks: to be completed._

## Evaluation

The brief asks how to show a stakeholder such as the Head of Fraud that the tool is effective and safe, with offline and online metrics, and for an initial evaluation suite. **The suite does not exist yet.** Everything in this section is proposed; the design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#evaluation) and [`evals/README.md`](evals/README.md). The metrics below are the plan, not results.

**Offline** means measured on fixed cases before anyone uses the tool. The suite will run about 25 cases through the same code path the interface uses, and report counts per tier, not one accuracy percentage, because the sample is small. A live run records the model's replies; the suite can then be replayed from those recordings with no model, no key and no network. Replay shows that the recorded output, scored by the real code, gives the reported results. It does not show how the live model behaves today.

**Online** means measured while FSMs use the tool, which would need a deployment. The assistant is planned to write one structured log line per step of each turn (question, SQL, tool chosen, guard decision, outcome, row count, duration, token usage; never result rows or the key). Those log fields are what the online metrics below would be computed from.

Each offline measure is there because it predicts something that matters online:

| Offline measure (proposed) | Online metric it predicts (proposed) |
|---|---|
| Execution accuracy: the generated query returns the same result as a reference query, per tier | Share of generated queries the FSM uses as they are, against the share they correct or report as wrong |
| Guardrail: unsafe inputs (writes, injection) that reach execution; target zero | Unsafe statements reaching the database (must stay zero); rate of guard rejections |
| Clarification and refusal: vague or unanswerable questions are not answered with invented SQL | Share of clarifications followed by a successful answer; answers reported as wrong |
| Rule arithmetic: the reported backtest equals an independent recomputation | Trust in the numbers: audits of shown figures find no differences |
| Rule match and score on later months | Share of proposed rules accepted; live precision and false-positive rate of accepted rules after deployment |
| Fraud scan finds the known concentration | Time from first question to a validated insight |
| Model requests and time per turn | Response time and cost per question |

Further online metrics with no offline counterpart: time from insight to deployed rule against the current workflow; fraud value prevented against legitimate payments blocked.

_Results, the case set and known failure cases: to be completed._

## Data: differences from the brief

The database does not match the brief's description in several ways. Details and row counts: [`docs/DATA.md`](docs/DATA.md).

- The amount column is `amount_usd_cents` (integer cents), not `amount`.
- `card_type` values are `'Credit'`, `'Debit'`, `'Debit (Prepaid)'`, not `'debit'`.
- There are two tables the brief does not list: `merchants` and `merchant_locations`.
- `fraud_labels.transaction_id` is TEXT; `transactions.id` is INTEGER. The join needs `CAST(transactions.id AS TEXT)` to stay fast in every form.
- Only about 67% of transactions have a fraud label (777,339 of 1,159,966), and about 0.17% of labelled transactions are fraud (1,360). Unlabelled is treated as unknown, not as legitimate.
- All 1,360 labelled frauds are at merchant locations in Rome, Italy.

So the brief's example rule, `amount > 1000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'debit')`, does not run as written. Its equivalent on this data is `amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')`.

## What is not built yet

Built: configuration, read-only execution, the schema context, the rule backtest and the SQL guard. Not built, in planned order:

1. The model layer: the Claude client, the five tools, recording and replay of model replies.
2. The graph for one turn and the terminal chat: natural language → SQL, and a rule shown with its backtest.
3. Structured logs for every step, and opt-in hosted tracing with result rows filtered out.
4. The evaluation suite with offline replay.
5. Rule evidence month by month, and scoring on months a rule was not derived from.
6. The fraud-concentration scan.
7. The web chat and its HTTP API. First to be cut if time runs out.

Not planned for this prototype: authentication, conversation memory that survives a restart, streaming of replies, charts, deploying a rule, a second model provider, a free agent loop, and proof that a rule generalises beyond this dataset.

_What was cut for time and how it would be built: to be completed at the end, as the brief requests._

## Repository layout

```
README.md              This file
AGENTS.md, CLAUDE.md   Pointers to docs/AGENTS.md (working rules for coding agents)
docs/
  CHALLENGE.md         The original brief, verbatim
  ARCHITECTURE.md      Target design, what is built and what is proposed, evidence, known weaknesses
  DATA.md              Schema notes and differences from the brief
  DECISIONS.md         Decision log
  AGENTS.md            Working rules for coding agents
data/                  data.db (SQLite) and schema.sql; read-only
src/fsm_assistant/     The package. Built: config.py, db.py, schema_context.py,
                       sql_scan.py, sql_guard.py, rules/backtest.py; the others are
                       docstring-only placeholders
tests/                 pytest tests (fixture databases built from data/schema.sql)
evals/                 Evaluation suite (empty; see its README)
```
