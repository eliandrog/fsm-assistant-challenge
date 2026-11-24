# Decision log

One entry per significant decision: the problem, what was chosen, what was rejected, what it costs.

- Entries are numbered **D-01, D-02, …** in the order they were decided. The number is the only ordering; entries carry no dates.
- Entries are never rewritten. If a later decision changes an earlier one, the earlier entry's status becomes **Superseded by D-xx** or **Refined by D-xx**.
- Decisions not yet made are listed under "Open points" in [`ARCHITECTURE.md`](ARCHITECTURE.md#open-points). When one is settled it gets an entry here.

## Index

| D-NN | Title | Status |
|---|---|---|
| D-01 | Python 3.13, uv, `src/` layout | Accepted |
| D-02 | Core functionality first, minimal interface | Accepted |
| D-03 | Model-written SQL runs read-only, after validation in code | Accepted; refined by D-06, D-07, D-08, D-17 |
| D-04 | The real schema is the source of truth, not the brief | Accepted |
| D-05 | LLM provider: local Ollama with Qwen | Superseded by D-14 |
| D-06 | Read-only is enforced in three engine-level layers | Accepted; refined by D-18 |
| D-07 | Query timeout through a progress handler | Accepted; refined by D-18 |
| D-08 | `db` owns the row cap | Accepted |
| D-09 | `.env` is read by a small built-in parser | Accepted |
| D-10 | The schema context prescribes one form of the label join | Accepted |
| D-11 | Backtest metrics use labelled transactions only; undefined ratios are `None` | Accepted |
| D-12 | One backtest template; the clause sees only `transactions` | Accepted; refined by D-17 |
| D-13 | A rule clause is contained by parentheses and a lexical scan | Accepted; refined by D-17 |
| D-14 | Model provider: Anthropic (Claude) through its API | Accepted; not built yet |
| D-15 | LangGraph as a fixed state graph, not an agent loop | Accepted; not built yet |
| D-16 | The model chooses one of five tools; code executes it | Accepted; not built yet; default mode to be set by a measurement |
| D-17 | The SQL guard reads SQLite's own account of a compiled statement | Accepted |
| D-18 | Connection hardening: unknown quoted names are errors, and values are capped in size | Accepted |

## Template

```
## D-NN: Title

**Status:** Accepted

**Context:** The problem or question, and the constraint that makes it matter.

**Decision:** What we chose.

**Alternatives:** What we rejected and why.

**Consequences:** What this costs or commits us to.
```

## D-01: Python 3.13, uv, `src/` layout

**Status:** Accepted

**Context:** The brief leaves tooling open but requires a reproducible project.

**Decision:** Python 3.13 managed by uv, with a lockfile. The package `fsm_assistant` lives under `src/` and is built with `uv_build`.

**Alternatives:** pip + `requirements.txt` (no lockfile by default); Docker (more setup than a local SQLite prototype needs).

**Consequences:** Reviewers need uv installed. Setup is one command, `uv sync`.

## D-02: Core functionality first, minimal interface

**Status:** Accepted

**Context:** The brief estimates 4-5 hours, suggests an MVP of reliable natural language → SQL, and asks candidates to focus on features and their evaluation rather than the interface.

**Decision:** Build in this order: natural language → SQL, rule proposal with backtesting, evaluation suite. The interface stays as simple as possible until those work.

**Alternatives:** A polished chat UI first (closer to the real UX, but spends the time budget where the brief weighs least).

**Consequences:** The first interface will look plain. Whatever is cut gets listed in the README, as the brief asks.

## D-03: Model-written SQL runs read-only, after validation in code

**Status:** Accepted; refined by D-06 (how read-only is enforced), D-07 (how the timeout works), D-08 (the row limit belongs to `db`, not the validator) and D-17 (how the validator works)

**Context:** The model writes SQL that is executed against the database. A prompt instruction alone does not stop a wrong or destructive statement.

**Decision:** Two independent layers in code: a validator (single `SELECT`, known tables and columns, row limit) and a read-only SQLite connection (`mode=ro`) with a timeout.

**Alternatives:** Prompt-only instruction (not enforceable); working on a copy of the database (protects the file but still allows wasteful or unintended statements).

**Consequences:** The validator needs a SQL parser or a strict allow-list, to be chosen at implementation time.

## D-04: The real schema is the source of truth, not the brief

**Status:** Accepted

**Context:** The database differs from the brief's description: column names, value casing, two extra tables, partial fraud labels (see [`DATA.md`](DATA.md)).

**Decision:** Schema context for the model is built from the database and `schema.sql`. The differences are documented in `DATA.md` and summarised in the README.

**Alternatives:** Follow the brief's description (the example rule would not run).

**Consequences:** Rules and example queries in this repo use the real names (`amount_usd_cents`, `'Debit'`), which will not match the brief's example literally.

## D-05: LLM provider: local Ollama with Qwen

**Status:** Superseded by D-14

**Context:** The assistant needs a model to write SQL and rule clauses. The choice decides who can reproduce the project, what leaves the machine, and how reliable the generated SQL is likely to be. This was open decision 1 in `ARCHITECTURE.md`, where the suggested default was a hosted model.

**Decision:** The model runs locally through [Ollama](https://ollama.com), using a Qwen model. No hosted API is used.

Two Qwen models are installed on the development machine (Ollama 0.40.16). Values are as reported by `ollama list` and `ollama show`:

| Tag | ID | Parameters | Quantization | Context length (model maximum) | Size on disk |
|---|---|---|---|---|---|
| `qwen3:14b` | `bdbd181c33f2` | 14.8B | Q4_K_M | 40,960 | 9.3 GB |
| `qwen3:30b-a3b` | `ad815644918f` | 30.5B, mixture-of-experts architecture (`qwen3moe`) | Q4_K_M | 262,144 | 18 GB |

**Which of the two is the default is not decided.** It will be chosen by running the evaluation suite against both and comparing the results; that comparison also goes in the README as evidence. Neither is a code-specialised variant, so there is no suggested starting default.

The model name is configuration (`OLLAMA_MODEL`), as is the server address (`OLLAMA_HOST`, default `http://localhost:11434`). `llm.py` stays the only module that talks to a model and stays a thin seam, so another provider could be swapped in without touching the rest of the code.

**Alternatives:** A hosted API (Anthropic, OpenAI, …): stronger and more consistent SQL generation and no local hardware requirement, but every reviewer needs an API key, each call costs money, and the schema and query results are sent to a third party.

**Consequences:**

- No API key and no per-call cost. The schema and transaction data never leave the machine. A reviewer reproduces the setup with `ollama pull <tag>`.
- Small local models are weaker and less consistent at SQL generation than large hosted ones. This raises the importance of the parts that do not trust the model: the SQL guard, schema grounding, the repair attempt and the evaluation suite.
- Eval results are specific to a model tag and quantization, so each run must record which model produced it. Tags can be repointed by the registry; the IDs above identify the exact builds.
- A reviewer needs Ollama installed, a multi-gigabyte model download (sizes above) and enough memory to load the model. Local inference is also slower than a hosted API. Neither memory use nor speed has been measured yet.
- Still open, to settle at implementation time:
  - How the client talks to Ollama: the official `ollama` Python package, plain HTTP against its REST API, or its OpenAI-compatible endpoint. The first and third may add a dependency, which needs approval.
  - Both models have a thinking mode that is on by default. Whether to keep it on affects latency and how the reply is parsed.
  - The context window Ollama actually runs with is a runtime setting and can be smaller than the model maximum above. The schema context has to fit in it; this has not been checked.

## D-06: Read-only is enforced in three engine-level layers

**Status:** Accepted; refined by D-18 (two more connection settings, and which read-only errors are the environment's fault)

**Context:** D-03 says model-written SQL runs on a read-only connection. Checked on a copy of the database, `mode=ro` alone is not enough: it protects the opened file, but `ATTACH DATABASE 'new.db'` followed by `CREATE TABLE` creates and writes another file, and temp tables are allowed. The database is also in WAL mode, which decides what a read-only connection does on disk (see [`DATA.md`](DATA.md#access)).

**Decision:** `db.py` opens every connection with three independent layers, none of which parses SQL text:

1. URI `mode=ro`: the database file cannot be written.
2. `PRAGMA query_only = ON`: no write through this connection to any database it can reach.
3. An authorizer callback (`sqlite3`'s `set_authorizer`), consulted by SQLite when it compiles a statement, that allows only reads (`SELECT`, column reads, function calls, recursive CTEs, and the pragmas `table_info` and `foreign_key_list`) and denies everything else: `INSERT`/`UPDATE`/`DELETE`, all DDL, `ATTACH`/`DETACH`, every other `PRAGMA`, transaction control, `VACUUM INTO`, `ANALYZE`, `REINDEX`.

Any of them firing raises `ReadOnlyViolation`. A connection lives for one query, so no state carries over between statements. `immutable=1` is not used.

**Alternatives:**

- `mode=ro` only: leaves `ATTACH` and temp tables open, as above.
- `immutable=1`: creates no `-shm`/`-wal` files and works on a read-only filesystem, but it tells SQLite the file cannot change, so it takes no locks and ignores the WAL. If anything did write to the database, or left changes in the WAL, queries would return stale or wrong results silently. Nothing writes today, but being wrong only when an assumption breaks is a poor trade for avoiding two git-ignored files.
- Relying on the SQL validator (`sql_guard`): it works on text and is not built yet; the engine-level layers hold whatever the text looks like.

**Consequences:** On a fresh clone the first query creates `data/data.db-shm` and an empty `data/data.db-wal` (git-ignored; `data.db` itself is not modified), and the directory holding the database must be writable when those files are absent. `EXPLAIN` and reads of `sqlite_master` are allowed. A statement that mixes a read with a denied action is rejected as a whole.

## D-07: Query timeout through a progress handler

**Status:** Accepted; refined by D-18 (a size cap bounds what a single long instruction can build)

**Context:** A wrong query can run for minutes: the tables have no secondary indexes, and one mistaken form of the label join compares every transaction with every label ([`DATA.md`](DATA.md#things-that-affect-correctness)). SQLite has no statement timeout; the `timeout` argument of `sqlite3.connect` only bounds waiting for a lock.

**Decision:** `db.run_query` installs a progress handler (`set_progress_handler`) that SQLite calls every 10,000 virtual-machine instructions. It compares a monotonic clock with a deadline and aborts the statement once the deadline has passed; the resulting error is raised as `QueryTimeout`. The deadline covers both `execute` and fetching rows. The default is 15 seconds (`FSM_QUERY_TIMEOUT_SECONDS`); the slowest sensible query measured, a six-table join with `GROUP BY`, takes about 1.3 seconds.

**Alternatives:** `Connection.interrupt()` from a timer thread (same effect, but adds a thread and a race between the timer and a statement that has just finished); running queries in a subprocess and killing it (heavier, and results must be serialised back).

**Consequences:** The check runs between VM instructions, so a single long-running instruction is not interrupted until it returns; none was observed on this data. The handler's overhead was not measurable on a full-table join (0.62 s with it, 0.65 s without).

## D-08: `db` owns the row cap

**Status:** Accepted

**Context:** D-03 and the architecture listed a "row limit" under the SQL validator and a "row cap" under execution, without saying which one limits the result. Enforcing it in the validator means either rejecting queries without `LIMIT` or rewriting the model's SQL.

**Decision:** `db.run_query` is the only place the limit is applied. It fetches at most `row_cap + 1` rows and returns the first `row_cap` with `truncated = True` when the extra row existed. The SQL text is never changed. The default cap is 1,000 rows (`FSM_ROW_CAP`). `sql_guard` does not check or add `LIMIT`.

**Alternatives:** Appending or rewriting `LIMIT` in the SQL (changes the query the FSM is shown, and is fragile with CTEs, compound selects and an existing `LIMIT`); wrapping the query in `SELECT * FROM (…) LIMIT n` (also rewrites it, and can change the plan).

**Consequences:** The FSM sees exactly the SQL that ran, and the interface can say when a result was cut. A truncated result is not the full answer: anything that compares result sets (the evaluation scorers) must check `truncated`. The cap bounds rows returned, not work done; the timeout (D-07) bounds the work.

## D-09: `.env` is read by a small built-in parser

**Status:** Accepted

**Context:** Settings come from environment variables, and the README tells the reader to copy `.env.example` to `.env`. Something has to read that file, and new dependencies need approval.

**Decision:** `config.py` contains a parser for simple `KEY=VALUE` lines: blank lines, `#` comment lines, and one pair of matching quotes around a value. Real environment variables take precedence over the file, and an empty value means "use the default". The file is looked up in the project root. A relative `FSM_DB_PATH` is resolved against the project root, not the current directory.

**Alternatives:** `python-dotenv` (a dependency for about twenty lines of code); environment variables only (then the documented `.env` would do nothing unless the reader exports it by hand).

**Consequences:** `export`, inline comments, multi-line values and variable expansion are not supported; a line that is not `KEY=VALUE` is an error rather than being skipped silently. The project root is located from the package's source path, which holds for the editable install that `uv sync` makes.

## D-10: The schema context prescribes one form of the label join

**Status:** Accepted

**Context:** `fraud_labels.transaction_id` is TEXT and `transactions.id` is INTEGER, and `fraud_labels` has no declared foreign key. Measured on the real database, `f.transaction_id = t.id` is fast as an inner join (the planner scans the labels and looks each transaction up by primary key) but was interrupted after 8 seconds without finishing as a `LEFT JOIN` or `EXISTS` driven from `transactions`: the integer cannot be looked up in the text index, so every transaction is compared with every label. With `f.transaction_id = CAST(t.id AS TEXT)` the label index is used in every shape (inner join 0.65 s, `LEFT JOIN` 0.19 s). Every label id is the canonical text of an integer, so both forms match the same 777,339 rows.

**Decision:** The schema context gives the model exactly one join to use, `fraud_labels.transaction_id = CAST(transactions.id AS TEXT)`, and the query timeout (D-07) catches the slow form if the model writes it anyway.

**Alternatives:** Listing the uncast join (correct, but a trap as soon as the model writes a `LEFT JOIN` or `EXISTS`); `CAST(f.transaction_id AS INTEGER) = t.id` (fast only when the labels drive the join).

**Consequences:** The backtest templates should use the same form. The equivalence depends on the label ids staying canonical; an integration test checks it.

## D-11: Backtest metrics use labelled transactions only; undefined ratios are `None`

**Status:** Accepted

**Context:** Only 777,339 of 1,159,966 transactions have a fraud label, and an unlabelled transaction is unknown, not legitimate ([`DATA.md`](DATA.md#things-that-affect-correctness)). A rule flags labelled and unlabelled transactions alike, so each metric needs a stated population. The architecture's list left two things open: whether "flagged" in the precision denominator includes unlabelled rows, and what a ratio is when its denominator is zero.

**Decision:** In `rules/backtest.py`:

- precision = fraud caught / flagged transactions that have a label;
- recall = fraud caught / all labelled fraud in the dataset (1,360 on the shipped data);
- false positives = flagged transactions labelled not fraud; unlabelled flagged transactions are neither true nor false positives and are reported as their own count (`flagged_unlabelled`), with the labelled share of the flagged set (`flagged_label_coverage`);
- flag rate = all flagged / all transactions, labelled or not, because it describes how much traffic the rule would stop, which does not depend on labels;
- a ratio with a zero denominator is `None`: precision when nothing flagged is labelled, recall when there is no labelled fraud;
- amounts are plain sums of `amount_usd_cents` in cents (all flagged, fraud caught, false positives), so negative amounts reduce them.

**Alternatives:** Counting unlabelled flagged rows as legitimate (a `LEFT JOIN` with missing treated as 0: understates precision by about a third on this data); returning 0 for an undefined ratio (reads as "this rule is always wrong" when the truth is "not measurable"); extrapolating labelled rates to the unlabelled rows (assumes labels are missing at random, which is not known); excluding negative amounts or summing absolute values (their meaning is undocumented, so any treatment is a guess, and the plain sum is the one that can be checked by hand).

**Consequences:** Whatever presents a backtest has to handle `None` and should show the unlabelled count next to precision. Precision can rest on very few rows (the brief's example rule flags 350 transactions, of which 2 are labelled fraud), and nothing here expresses that uncertainty. The total amount of labelled fraud, which an amount-based recall would need, is not computed.

## D-12: One backtest template; the clause sees only `transactions`

**Status:** Accepted; refined by D-17 (the guard rejects a clause that reads `fraud_labels`)

**Context:** The brief defines a rule as a `WHERE` clause that filters `transactions`, with an example that uses bare column names and a subquery on `cards`. The backtest has to join `fraud_labels` too, and a clause placed in a query where both tables are in scope could name a column ambiguously or use `is_fraud` as if it were a transaction column. The label join must also be the cast form of D-10.

**Decision:** One fixed statement. A CTE selects the flagged rows from `transactions` alone, unaliased: `WITH flagged AS (SELECT id, amount_usd_cents FROM transactions WHERE (<clause>))`. The outer query left-joins `fraud_labels f ON f.transaction_id = CAST(flagged.id AS TEXT)` and aggregates every count and amount in one row; the three dataset totals are scalar subqueries in the same statement. A clause therefore uses bare column names or `transactions.<column>`; there is no alias to learn. The result is built from the statement's column names, not their positions.

**Alternatives:** A single scan of `transactions` joined to the labels with the clause inside `CASE` expressions (labels in the clause's scope, and an index lookup for all 1.16M rows on every backtest); aliasing the table as `t` (the brief's own example would need rewriting); separate queries for totals and flagged counts (more round trips for no gain); taking the totals from constants or a cache (wrong on any other database, and they cost about 0.04 s).

**Consequences:** Measured on the real database: 0.04 to 0.2 s for selective rules, about 0.9 s when every transaction is flagged. The totals count rows of `fraud_labels` without joining them to `transactions`, which is right only while every label belongs to exactly one transaction; an integration test checks that. A subquery inside a clause can still read any table, including `fraud_labels`; restricting that is left to `sql_guard`.

## D-13: A rule clause is contained by parentheses and a lexical scan

**Status:** Accepted; refined by D-17 (the compile check is done in both positions and returns what the clause reads; authorizer denials are clause errors; the scanner is shared with the guard)

**Context:** The clause will be model-written text placed inside the backtest template. Writes are already impossible (D-06), but a clause could still change what is measured: `1=0) UNION SELECT id, amount_usd_cents FROM transactions WHERE (1=1` closes the template's parenthesis and replaces the flagged set, a trailing `--` or `/*` comment hides the rest of the template, and `1=1 LIMIT 10` adds to the query. The validator that will check tables and columns (`sql_guard`) is not built, its method is an open decision, and no SQL parser is available without a new dependency.

**Decision:** Three steps in `rules/backtest.py`, none needing a parser:

1. `check_clause` scans the text once, skipping quoted text the way SQLite's tokenizer pairs it (`'…'`, `"…"`, `` `…` `` with doubled quotes as escapes, and `[…]`). Outside quotes it rejects `;`, `--`, `/*`, the bind-parameter characters `? : @ $ #`, NUL, a `)` that has no earlier `(`, and a `(` left open; also an unterminated quote and an empty clause. Each rejection raises `RuleClauseError` with a reason, before any query runs.
2. The clause is compiled, not run, as `EXPLAIN SELECT 1 FROM transactions WHERE <clause>`, so text that is only valid inside parentheses (a bare `SELECT 1`, which would become a scalar subquery) is rejected.
3. The template places the clause as `WHERE (` newline clause newline `)`. With balanced parentheses and no comments, anything that is not one expression (`LIMIT`, `GROUP BY`, `UNION`) is a syntax error there.

SQLite's message is kept verbatim on the error for the repair attempt.

**Alternatives:** Relying on SQLite's syntax errors alone (the `UNION` escape above is valid SQL); a keyword deny-list (refuses legitimate rules, and is not what stops an escape); a SQL parser library (a dependency, and part of the open `sql_guard` decision); stripping comments instead of rejecting them (rewrites the rule the FSM is shown).

**Consequences:** `--` is refused even where it is arithmetic (`1--1`; write `1 - -1`), and a column name containing `$` or `#` cannot be used unquoted; neither occurs in this schema. Bind parameters were refused partly because SQLite's `$name(…)` form can hide parentheses from the scan. The scan guarantees structure, not meaning. Left for `sql_guard`, which is expected to run before `backtest` and can call or replace `check_clause`: which tables and columns a clause may read (today a clause can read `fraud_labels` in a subquery and reproduce the labels), non-deterministic functions such as `random()`, and expensive clauses, which only the timeout bounds.

## D-14: Model provider: Anthropic (Claude) through its API

**Status:** Accepted; not built yet. Supersedes D-05.

**Context:** D-05 chose a local Qwen model in Ollama, mainly so that no API key was needed and nothing left the machine. Exploratory runs on the two local models (see [`ARCHITECTURE.md`](ARCHITECTURE.md#evidence)) showed the approach was workable, but slower end to end, and a reviewer would need a download of 18.5 GB for the larger model and enough memory to load it. Two things changed the balance: the owner has an Anthropic API key, and the evaluation suite is designed to be replayed offline from recorded model replies (proposed), so a reviewer needs no model at all to check the reported results.

**Decision:** The assistant will use Claude through the Anthropic API. No local model is part of the build.

- The model is configuration (`ANTHROPIC_MODEL`). The default is `claude-opus-5-5`.
- The evaluation suite will also be run on `claude-haiku-5-5`, a smaller and cheaper model. Whether it becomes the default is open and will be decided from those results.
- The key is `ANTHROPIC_API_KEY`, kept in `.env` (git-ignored) and named only in `.env.example`.
- `llm.py` stays the only module that talks to a model provider, so a second provider could be added behind the same interface.

**Alternatives:**

- Local Ollama with Qwen (D-05): no key, no per-call cost, nothing leaves the machine. Rejected for the slower turn, the large download a reviewer would need, and weaker first-attempt results on a small sample (10 and 12 of 15 correct).
- Both providers behind a setting: twice the prompt tuning and twice the evaluation runs for a prototype. Named in the README as a possible extension, not built.

**Consequences:**

- Live use needs an API key and each model call costs money. Tests never call a model, and no paid call is made without the owner's go-ahead for that run.
- The question, the prompt and the schema context are sent to a hosted service. By design, result rows are never sent to the model.
- Claude's output cannot be reproduced from a seed, so two live runs can differ. Offline replay is the reproducible path, and each evaluation run must record the model that produced it.
- A reviewer without a key can still run the tests and, once it exists, the replayed evaluation.
- `config.py`, `.env.example` and their tests still carry `OLLAMA_HOST` and `OLLAMA_MODEL`. They are unused and will be replaced by the Claude settings when the model layer is built.

## D-15: LangGraph as a fixed state graph, not an agent loop

**Status:** Accepted; not built yet

**Context:** One turn has several steps with strict rules between them: model-written SQL must pass a guard before it runs, a rule must be backtested before it is shown, and a failed attempt may be repaired once and no more. Follow-up questions need a small memory per conversation. The question is what holds these steps together, and how much freedom the model gets in choosing them.

**Decision:** One turn will be a fixed state graph built with LangGraph: nodes are plain Python functions, and the routes between them are chosen by code (the graph is in [`ARCHITECTURE.md`](ARCHITECTURE.md#how-one-turn-works)). LangChain is used only for the Claude wrapper (`langchain-anthropic`). It is not a tool-calling agent loop: no prebuilt agent and no node that executes tool calls on the model's behalf. The model is asked once per attempt and never sees the result of what it asked for.

Two dependencies will be added by the task that builds the model layer: `langgraph` and `langchain-anthropic`. Not added: the `langchain` meta-package, or a direct dependency on the `anthropic` SDK.

**Alternatives:**

- A plain Python pipeline (functions calling functions): no framework dependency, and it could give the same guarantees. Rejected because the graph gives three things that would otherwise be written by hand: the structure is declared in one place and the diagram is drawn from the code; conversation memory is a checkpointer (LangGraph's store of saved state) keyed by conversation; and each step's update can be traced.
- A free agent loop, where the model sees each tool result and chooses its next action: more flexible, and able to chain several queries in one turn. Rejected because it cannot guarantee that every query passes the guard, a turn has no fixed cost, and result rows would enter the model's context, where the model could restate or alter the numbers.

**Consequences:**

- The safety properties are structural: a query can reach execution only by the route through the guard, and a turn costs at most two model requests.
- The assistant cannot plan several steps within one turn. An analysis that needs three queries takes three questions from the FSM.
- Two framework dependencies to pin and keep working together. Defaults in them that would break the design (automatic retries, for example) each need a test.
- "Tool" in this project means only the model's choice of action; see D-16.

## D-16: The model chooses one of five tools; code executes it

**Status:** Accepted; not built yet. Which of the two modes below is the default will be set by a measurement.

**Context:** Every reply from the model has to be one of five actions with known fields, so that code can route on it without parsing free text. Claude offers two ways to get that: tool calls (the model names an action and fills in typed arguments) and structured output (the reply is one object constrained to a schema).

**Decision:** Primary mode: five tools, one per action, each with a strict argument schema and at most one call per reply.

| Tool | Meaning |
|---|---|
| `run_sql` | Answer with a query |
| `propose_rule` | Propose a rule clause |
| `profile_fraud` | Ask for the code-written fraud scan |
| `ask_clarification` | Ask the FSM what was meant |
| `decline` | Say the question cannot be answered from this data |

- The model chooses a tool; a graph node validates the call, runs the guard and executes it. The framework never runs a tool body, and the model never receives a tool result.
- Tool choice is left on auto (the model decides whether to call a tool), because the default model rejects a forced tool choice.
- A reply without exactly one valid tool call (plain text, two calls, an unknown tool, invalid arguments) counts as a failed attempt and uses the single repair.

Fallback mode, behind a setting: the same five actions returned as one schema-constrained object with a `kind` field. Nodes, guard and evaluation are identical in both modes.

Which mode is the default is not decided here. It will be decided by a measurement in the task that builds the model layer: how often each model replies with exactly one valid tool call on a fixed set of questions. No rate has been measured yet.

**Alternatives:**

- Forced tool choice (the API guarantees a tool call): the simplest contract, but not accepted by the default model.
- Free text with the SQL extracted by pattern matching: fragile, and an explanation can be mistaken for a query.
- The single structured object as the only mode: kept as the fallback. Tools were preferred as the primary because each action has its own small schema and no fields that are unused for that action.

**Consequences:**

- With tool choice on auto, the model can answer in plain text. That costs the turn's one repair, so how often it happens matters and has to be measured before the default is fixed.
- Two modes to keep working until the measurement is done; both end in the same typed action, so the cost is one conversion function and its tests.
- The argument schemas must stay within what the API's strict mode supports (for example, no length or range limits), so those limits are checked in code.

## D-17: The SQL guard reads SQLite's own account of a compiled statement

**Status:** Accepted. Refines D-03, D-12 and D-13.

**Context:** D-03 requires model-written SQL to be validated in code before it runs and left the method open ("a SQL parser or a strict allow-list"). D-12 and D-13 left three things to that validator: which tables a rule clause may read (a clause that reads `fraud_labels` backtests at precision 1.0 and recall 1.0), non-deterministic rules, and a reason precise enough for one repair attempt. No SQL parser is available without a new dependency, and the read-only connection (D-06) already stops writes whatever the validator does. What the validator adds is deciding which reads are in scope, and saying why not.

One thing was learnt by review after the first version: compiling a statement is not free. SQLite copies a common table expression (CTE) at every place it is referred to, so a statement in which each CTE joins the one before to itself doubles in size per level. A 799-character statement with 20 such levels took 45 s and 1.7 GB to compile. Nothing in the engine stops it: the timeout (D-07) and `SQLITE_LIMIT_VDBE_OP` are not consulted while a statement is compiled, and `interrupt()` from another thread is only noticed once the compile has finished. So the guard has to bound that cost itself, from the text, before it compiles anything.

**Decision:** `sql_guard.py` judges one string and returns a decision; it never runs or rewrites it.

- **Text checks first.** Before anything is compiled the text is read with the scanner (`sql_scan.scan`, which pairs quotes and comments as SQLite does), cheapest check first:
  - Length: at most 10,000 characters (`too_long`). Analyst SQL is a few hundred.
  - A Markdown code fence at the start or end (`code_fence`) is refused with a message asking for the bare statement. It is not unwrapped: nothing is repaired beyond trimming and one trailing `;`.
  - Text that cannot be encoded as UTF-8, such as an unpaired surrogate (`invalid_text`), and an unterminated quote (`unterminated_quote`).
  - Compile cost (`too_complex`). The scanner finds every `name AS (…)` (a CTE or a named window); more than 20 of them is refused. Then the size of the statement once every reference to a CTE is replaced by the CTE's body is estimated in tokens, erring on the high side (any word, quoted name or string equal to a CTE's name counts as a reference to it), and more than 50,000 is refused. A plain statement of the maximum length is at most 10,000 tokens, so only repeated CTE references reach the limit.
- **Method.** `db.inspect_query` prepares `EXPLAIN QUERY PLAN <sql>` on the read-only connection. Preparing compiles the statement; the plan reads no table rows. The authorizer (D-06) still denies everything that is not a read, and also records every action. The record is the list of action codes, the tables read with the columns named for each, and the functions called. `EXPLAIN QUERY PLAN` was chosen over `EXPLAIN` as the prefix because then neither a statement beginning `EXPLAIN` nor one beginning `QUERY PLAN` compiles.
- **Query policy** (`check_query`). Exactly one statement. The first recorded action must be `SELECT`, which refuses `PRAGMA table_info(…)` (allowed by the connection for the schema context), a statement that leaves no record at all (`VACUUM`), and with a text check on the first word a model-written `EXPLAIN`. Every table read must be in an allow-list that is the database's own table list, read from its catalogue and cached per path. No call to `load_extension`, to `current_date`, `current_time` or `current_timestamp`, or to the functions that describe the engine instead of the data (`sqlite_version`, `sqlite_source_id`, `sqlite_compileoption_get`, `sqlite_compileoption_used`).
- **Today's date.** The authorizer reports that `date` was called but not with what, and on data that ends on 2019-10-31 a filter relative to today returns nothing and looks like an answer. When the record contains a date function, the tokens are read and three forms are refused: a `'now'` literal (any case) anywhere inside the parentheses of a date function; `'subsec'` or `'subsecond'` where a date function takes its time value (`datetime('subsec')` means now in SQLite; after a time value the same word is a harmless modifier); and a date function called with no time value (`date()`, `datetime()`, `time()`, `julianday()`, `unixepoch()`, `strftime('%Y')`). A `'now'` anywhere else is ordinary text: `WHERE errors = 'now'` and `SELECT 'now' AS label` are accepted, with or without a date function in the statement, and so are a CTE or alias named `date`, `time` or `now`.
- **Rule policy** (`check_rule_clause`). The text checks, then containment (D-13: no `;`, comment, bind parameter, NUL or parenthesis that would close the template's own). Then the clause alone is compiled as `SELECT 1 FROM transactions WHERE <clause>`, and again inside parentheses as the template places it, and the query policy is applied to the first record: the backtest template joins `fraud_labels` itself, so only the clause alone shows what the clause reads. Then two more rules: no read of `fraud_labels`, and no function whose value does not come from the data (`random`, `randomblob`, `changes`, `total_changes`, `last_insert_rowid`). When a rule is refused for reading a table that is not allowed, the list of tables in the reason leaves out `fraud_labels`.
- **The backtest applies the rule policy itself.** `backtest(clause)` calls `check_rule_clause` and measures only an accepted clause; a rejection is raised as `RuleClauseError` carrying the guard's `reason_code` and `reason`. Safety no longer depends on a caller remembering to run the guard first. The template without the policy is a private function (`_backtest_unchecked`) used by `backtest` and by the tests that show what an unguarded clause would score.
- **Text.** The input is trimmed and one trailing `;` is dropped. Nothing else is repaired: a second `;`, an empty or comment-only string, a code fence and an unterminated quote are refused.
- **Result.** `GuardDecision` has `accepted`, the judged text, a `reason` in words for the repair attempt, a `reason_code` (a closed set, for routing, logs and counts), SQLite's own message when it refused to compile the text, and the record. A missing or unreadable database is not a decision and propagates as a `db` error.
- **Types and layering.** `GuardDecision`, `QueryInspection`, `TableRead` and the scanner's `Token` are frozen pydantic models that forbid unknown fields, and `reason_code` is an enum. `pydantic` is the project's first runtime dependency, approved for this purpose. The dataclasses that existed before (`Settings`, `QueryResult`, `BacktestResult`) are unchanged here. The scanner is a module of its own, `sql_scan.py`, which imports nothing from the project. Imports go one way: `sql_guard` imports `db` and `sql_scan`; `rules/backtest` imports `sql_guard`, `db` and `schema_context`.

Refinements to earlier entries. D-12: a clause that reads `fraud_labels` is refused before it is backtested, by `backtest` itself. D-13: the containment scan (`check_clause` there) and the compile check of step 2 are now the guard's, inside `check_rule_clause`; the compile check goes through `db.inspect_query` and is done both bare and inside the template's parentheses (so `1=1 LIMIT 1`, valid bare, is refused before the template runs); a clause the authorizer denies raises `RuleClauseError`, not `db.ReadOnlyViolation`, so callers handle one error type. Because `backtest` now normalises as the guard does, one trailing `;` on a clause is dropped instead of refused.

Two things found by testing that the policy has to allow for:

- When no column of a table is named (`SELECT count(*) FROM CARDS`), SQLite reports the table as the statement spelled it, so names are compared in lower case.
- In the same case SQLite can report the name of a CTE as if it were a table (`WITH per_card AS (…) SELECT count(*) FROM per_card`). A read that names a column always reports the real table underneath. So a name outside the allow-list is accepted only when it reads no column and nothing real in the database answers to it (checked by compiling `SELECT 1 FROM "<name>"`). A CTE named `sqlite_master` therefore cannot hide a read of the real one, and a CTE named like a real table is accepted in a query. In a rule, a column-less read of the name `fraud_labels` is refused even if it is a CTE.

How the two complexity limits were chosen, on the real database. Compile time follows the estimated size closely: between 0.07 and 4 microseconds per estimated token over fourteen shapes (self-joins, aggregates, `UNION ALL`, scalar subqueries, `SELECT *` over a six-table join, window functions, fan-outs of 2 to 60 per level). The costliest shape found, a six-table `SELECT *` joined to itself at each level, was judged in 0.04 s at a limit of 30,000 tokens, 0.08 to 0.09 s at 50,000 and 75,000, 0.2 s at 100,000 and 0.54 s at 250,000. 50,000 keeps the worst case found under 0.1 s. Statements without CTEs at the maximum length (62-table joins, 600 nested subqueries, 2,000-item lists, 130 window functions) compiled in under 10 ms, so no separate limit on nesting depth was needed. The limit of 20 definitions is not what bounds compile time; it bounds the work of the estimate itself.

**Alternatives:**

- A SQL parser library (for example `sqlglot`): a dependency, and a second grammar that can disagree with SQLite's. To answer "which tables does this read" it would have to resolve aliases, CTE scope, `SELECT *` and views itself, and every disagreement is either a false rejection or a bypass. SQLite resolves names when it compiles, and what it reports is by construction what it will run.
- A function allow-list: SQLite has well over a hundred built-in functions that an analyst's query can legitimately use (aggregates, window, date, string, math, JSON). A list that omits one rejects a correct query, and each false rejection spends the turn's only repair. On this connection a function cannot write or reach outside the database, and D-18 bounds what one call can allocate, so the few with a known problem are denied by name instead.
- Scanning the text for keywords or table names: fooled by quoting, comments, aliases and CTEs, and it rejects harmless text such as a column value containing `delete`.
- Letting the inspection connection allow everything and judging the record afterwards: nothing would run either way, but keeping the denial means the guard and the execution layer cannot disagree about what a read is.
- Refusing every `'now'` literal, or every one in a statement that uses a date function (the first version): simpler, but it rejects a text comparison that has nothing to do with dates, with a reason that is untrue for it.
- A hard-coded list of the seven table names: wrong the moment the file changes, and the catalogue costs one query per process.
- Bounding compile cost in the engine: not possible, as described in the context. Compiling in a separate process that can be killed: heavier than the problem, and the text bound is enough.
- Only a limit on the number of CTEs: it does not bound the cost, because each CTE can refer to the one before many times (64 tables in a join, 500 terms in a compound select). Four CTEs with a fan-out of 60 are already 216,000 copies. A count low enough to be safe on its own (two or three) would refuse ordinary analyst queries.
- Unwrapping a Markdown fence: a silent repair, and the text that ran would no longer be the text the model sent.

**Consequences:**

- Measured on the real database: a median of about 0.3 ms for an ordinary query and 0.45 ms for a rule clause, which is compiled twice. The cost does not depend on the data, and no longer depends without bound on the statement: the worst input found that passes the text checks is judged in under 0.1 s, and the statement from the review is refused in under 1 ms without being compiled.
- The guard bounds what it costs to judge a statement, not what it costs to run it. An accepted query can still be slow; the timeout (D-07) bounds that.
- The complexity estimate reads text and errs on the high side, so it can refuse a legitimate statement that refers to its CTEs unusually often. The limits are far from ordinary use (a CTE of 100 tokens can be referred to several hundred times) and the reason says what to change.
- The compile-cost bound is empirical: it holds for the shapes measured. A shape that costs much more per token than those would be slower than 0.1 s, though still bounded by the 50,000-token estimate.
- It depends on what SQLite's authorizer reports, which is behaviour of the engine, not of this code. The tests pin each case used; they were written against SQLite 3.51.
- The today check reads literal text. Not seen: `'now'` built at run time (`'no' || 'w'`), carried in a column (`WITH t AS (SELECT 'now' AS d) SELECT date(d) FROM t`), or written as a blob (`date(x'6e6f77')`). It exists to catch an honest mistake.
- A rule that lists fraud transaction ids as literals reads no label and is accepted. No check on what a clause reads can catch it.
- Views are not in the allow-list (the database has none). `fraud_labels` and `transactions` are named in code, as they already were in the backtest template and the schema notes: the tool serves this one schema.
- `random()` stays allowed in queries, so "show me a random sample" works and its result differs between runs. `changes()`, `total_changes()` and `last_insert_rowid()` stay allowed in queries too; on this connection they are always 0.
- A clause is compiled twice by `check_rule_clause` and once more when the template runs. A caller that calls `check_rule_clause` and then `backtest` pays for the check twice (under 1 ms).

## D-18: Connection hardening: unknown quoted names are errors, and values are capped in size

**Status:** Accepted. Refines D-06 and D-07.

**Context:** Weaknesses in the built execution layer, each checked on the real database.

- SQLite reads a double-quoted name it does not recognise as a string literal. `"amount" > 1000` (the brief's column name, quoted) compares text with a number, which is always true, and returned all 1,159,966 rows instead of an error.
- The timeout (D-07) is checked between virtual-machine instructions, so one instruction that builds a very large value is not interrupted until it has finished.
- The row cap (D-08) does not bound the memory a result takes. With values capped at 1,000,000 bytes, `SELECT zeroblob(1000000) FROM transactions` used about 340 MB for 300 rows, and 100 such columns used 540 MB for 3 rows.
- Two errors were reported as if the SQL were at fault. A WAL-mode database in a directory that is not writable, with no `-shm` file, fails every read with "attempt to write a readonly database", which was raised as `ReadOnlyViolation`; a repair step would have asked the model to fix a correct query. And an empty or comment-only string came back as a successful result with no columns and no rows.
- Text containing an unpaired surrogate (`SELECT '\ud800'`) raised `UnicodeEncodeError`, which is not a `db` error.

**Decision:** In `db.py`, on every connection:

- `SQLITE_DBCONFIG_DQS_DML` is switched off. A double-quoted name must be a real identifier; an unknown one fails to compile with a message that suggests single quotes, which the repair can use.
- `SQLITE_LIMIT_LENGTH` is set to 100,000 bytes: the largest string or blob any expression may produce. `zeroblob(1000000000)` fails at once with "string or blob too big".
- `SQLITE_LIMIT_COLUMN` is set to 100: the most columns in a result. The seven tables have 40 columns between them.
- `run_query` fetches rows one at a time and adds up their values (the bytes of a blob, the UTF-8 bytes of a text, 8 for anything else). When the total passes `max_result_bytes` it raises `ResultTooLarge`. The default is 5,000,000 bytes, a setting (`FSM_MAX_RESULT_BYTES`). The row fetched only to detect truncation is not counted.
- `ResultTooLarge` is a subclass of `QueryError`: the statement is what has to change (fewer or shorter columns, or an aggregate), so whatever hands a `QueryError` back to the model for repair handles it without a special case, and code that wants to treat it differently can still catch it by name.
- A read-only error is the environment's fault when SQLite's extended code says so (`READONLY_DIRECTORY`, `READONLY_CANTINIT`, `READONLY_CANTLOCK`, `READONLY_RECOVERY`) and the authorizer denied nothing. It raises `DatabaseAccessError`, a `DbError` that is neither `ReadOnlyViolation` nor `QueryError`, with a message that the directory must be writable. A failure while opening or configuring the connection raises the same error, and the connection is closed.
- A statement with no result columns raises `QueryError("empty statement")`, and text or a bound value that is not valid Unicode raises `QueryError` too, in `run_query` and in `inspect_query`.

**Alternatives:**

- Looking for double-quoted names in the text: the engine knows which names resolve; the text does not.
- Treating every read-only error with nothing denied as an environment fault: a write stopped by `mode=ro` or `query_only` alone (the layers behind the authorizer) reports the plain read-only code with nothing denied, and that one is a violation. The extended code tells them apart.
- A value cap of 1,000,000 bytes (the first version): the longest value in the data is 51 characters, so it left no legitimate use between 100,000 and 1,000,000 and ten times the room for a result to grow. A much smaller cap would start to refuse a long `group_concat`.
- Only lowering the value cap, with no budget: 1,000 rows of 100 columns of 100,000 bytes is still 10 GB.
- A budget that cuts the result short and reports it as truncated: `truncated` means "more rows exist than the row cap", and a result cut by size at an arbitrary row would be read as the same thing. An error with a reason is what the repair can act on.
- `immutable=1` to avoid the writable-directory requirement: rejected in D-06, for the same reason.

**Consequences:**

- A model that writes a string in double quotes gets an error instead of a result. The schema context shows values in single quotes.
- The value cap also limits the SQL text itself to 100,000 bytes (the guard refuses anything over 10,000 characters first), and a legitimate value above it (a very long `group_concat`) fails with a clear message.
- A result is at most the budget plus one row, and one row is at most 100 columns of 100,000 bytes, so about 15 MB of values in the worst case instead of an amount that grows with the row cap. Measured: the two statements above, written with 100,000-byte values, now end in an error at about 35 and 52 MB of process memory (30 MB is the baseline); with 1,000,000-byte values they fail at once on the value cap. A full page of an ordinary result, 1,000 rows of a six-table `SELECT *`, is about 0.35 MB.
- The schema context is byte-identical with all of these settings on, and the backtest's numbers are unchanged.
- What is still not bounded: the time a statement takes to compile, which nothing on the connection can limit (the guard bounds it from the text, D-17), and the memory SQLite uses inside a running statement, for a sort or a `DISTINCT` over long values, which only the timeout and the value cap limit.
- The value cap bounds what one instruction can allocate; it does not make the timeout a hard bound for every possible instruction.
- `QueryError` now always means "the statement has to change", and `DatabaseNotFoundError` and `DatabaseAccessError` always mean "the setup is wrong", which is the split a repair step needs.
