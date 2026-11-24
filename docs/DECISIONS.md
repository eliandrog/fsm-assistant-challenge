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
| D-12 | One backtest template; the clause sees only `transactions` | Accepted; refined by D-17; D-28 adds a second fixed template, by month |
| D-13 | A rule clause is contained by parentheses and a lexical scan | Accepted; refined by D-17 |
| D-14 | Model provider: Anthropic (Claude) through its API | Accepted; built (D-20) |
| D-15 | LangGraph as a fixed state graph, not an agent loop | Accepted; the packages are installed (D-20), the graph is not built yet |
| D-16 | The model chooses one of five tools; code executes it | Accepted; refined by D-21: both modes built, and tools confirmed as the default by a small live check. The graph that routes on them is not built |
| D-17 | The SQL guard reads SQLite's own account of a compiled statement | Accepted; refined by D-19 |
| D-18 | Connection hardening: unknown quoted names are errors, and values are capped in size | Accepted |
| D-19 | Pydantic models at every structured boundary, and strict type checking | Accepted |
| D-20 | Model settings, key handling, and hosted tracing off | Accepted; refined by D-27 |
| D-21 | One model interface, with our own record and replay | Accepted |
| D-27 | Logs for errors and health; LangSmith for traces, opt-in and built from an allow-list | Accepted |
| D-28 | A rule is shown with its backtest month by month, as a stability check | Accepted |
| D-29 | The fraud scan: chosen by the model, computed by code | Accepted |

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

**Status:** Accepted; refined by D-17 (the guard rejects a clause that reads `fraud_labels`); D-28 adds a second fixed template, which measures the same clause by calendar month

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

**Status:** Accepted; built, with the settings in D-20. Supersedes D-05.

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

**Status:** Accepted. The two packages are installed (D-20) and two facts the graph depends on are pinned by tests (D-21); the graph itself is not built yet.

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

**Status:** Accepted; refined by D-21. The five tools, the fallback object and the check that turns either into one typed action are built (D-21); the graph that routes on the action is not. Default mode: tools, confirmed by the live measurement recorded in D-21 (`fsm-assistant check-model`). Rate of replies with exactly one valid tool call: 11 of 11 on each of two models, `claude-opus-5-5` and `claude-haiku-5-5`. Structured output stays as the fallback behind the `FSM_TOOL_MODE` setting. The check is small: eleven questions, each asked once, with a minimal system message and not the final prompt, and it measures whether the reply was one valid tool call, not whether the SQL was right. The text below is as it was written before the measurement.

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

**Status:** Accepted. Refines D-03, D-12 and D-13. Refined by D-19 (the three dataclasses left unchanged here are pydantic models too).

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

## D-19: Pydantic models at every structured boundary, and strict type checking

**Status:** Accepted. Refines D-17.

**Context:** The code had two ways to write a data type. The guard's types (D-17) were frozen pydantic models; `Settings`, `QueryResult` and `BacktestResult` were frozen dataclasses, which check nothing when they are built. Annotations were a habit, not something a command checked: a query result's cells were `Any`, so nothing downstream of `run_query` was really typed. The parts still to be built are mostly data that crosses a boundary where it can be wrong: the arguments of a tool call written by a model, the state passed between graph nodes, evaluation cases read from files, and the bodies of an HTTP API.

**Decision:**

- Every structured value passed between modules is a pydantic v2 model with `ConfigDict(frozen=True, extra="forbid")`. `Settings`, `QueryResult` and `BacktestResult` are converted, with the same field names and behaviour. A closed set of values is a `Literal` or an enum. Errors stay ordinary exception classes.
- Every function and method is fully annotated. `mypy` runs in strict mode over `src/` and `tests/` with the pydantic plugin (`init_typed`, `init_forbid_extra`), as a dev-only dependency, and `uv run mypy` must pass as `uv run pytest` must. The package ships a `py.typed` marker. `Any` is kept in one place, the bind parameters of `run_query`, with a comment.
- Tests are held to the same standard as the package, with no per-module override: every test, fixture and helper is annotated, and a test narrows an optional value (`assert decision.reason is not None`) before it reads it. A `# type: ignore` in a test marks a line that is ill-typed on purpose, such as assigning to a frozen field, and carries its error code and reason.
- `Settings` holds the rules for its values (a positive, finite number, read from text with Python's `int()` or `float()`; an `http` or `https` host). `load_settings` turns the model's first error, in field order, into a `ConfigError` that names the environment variable, with the same wording as before.
- A cell of a `QueryResult` is `str | int | float | bytes | None`. `run_query` builds its result with `model_construct`, without validation: the column names come from the cursor and the cells from `sqlite3` with no converter or row factory, which returns only those five types. Validating 1,000 rows cost 0.2 ms for 8 columns and 1.1 ms for 38, about a fifth of the query's own time. A `QueryResult` built any other way is validated.
- A blob has no JSON form. `QueryResult` writes it to JSON as URL-safe base64 text, so serialising never fails on bytes that are not UTF-8; read back, the cell is that text. `model_dump` keeps the bytes. No column of the data holds a blob; only an expression such as `zeroblob(4)` produces one. How a blob is shown to the FSM is for the presentation step.
- The backtest's ratios (precision, recall, flag rate, label coverage) stay properties worked out from the counts, `None` when undefined (D-11). They are not fields, so a dumped `BacktestResult` holds the measured numbers only and can be read back.

**Alternatives:**

- Dataclasses only: no dependency, but nothing is checked when a value is built, and tool arguments, evaluation cases and API bodies would each need hand-written parsing and JSON code. `pydantic` is already installed and already needed by the model layer's packages.
- `TypedDict`: checked by the type checker only, not when the program runs; a dict is mutable, cannot forbid an unknown key at run time and cannot carry methods such as `QueryInspection.columns_of`.
- Dataclasses inside, pydantic only where data enters: two ways to write a type, and a rule about which to use that has to be re-argued for each new one.
- `pydantic-settings` for `Settings`: a second dependency to replace twenty lines, and its errors name fields, not environment variables.
- Validating every `QueryResult`: the measured cost above, on every query, to re-check values the engine has just produced. A test validates real results of every cell type instead.
- Rejecting a blob in a result: the engine returns one for a legitimate expression, and `db` reports what ran. Writing bytes to JSON as text (pydantic's default) fails on bytes that are not UTF-8.
- Relaxing the check for the tests (no annotations required, optional values read without narrowing): less to write, but a second standard to explain, a helper that returns the wrong type goes unnoticed, and a test that reads a `None` fails with an `AttributeError` or `TypeError` instead of an assertion that says what was missing.
- Annotations with no checker: they drift, as the `Any` cells showed.

**Consequences:**

- Later tasks follow this without a new decision: tool arguments, typed actions, graph state, evaluation cases and API request and response bodies are frozen pydantic models, and their modules pass strict `mypy` from the first change.
- Assigning to a field raises pydantic's `ValidationError`, where a frozen dataclass raised `FrozenInstanceError`. Building `Settings` in code with an invalid value raises `ValidationError`; only `load_settings` raises `ConfigError`.
- Code that reads a cell has to say which type it expects. `schema_context` does, through one small helper that raises `TypeError` on a surprise.
- `run_query` is the one place that trusts its own construction. A row factory or a converter added to the connection would break that assumption; a test that validates real results guards it.
- The schema context is byte-identical after the conversion, the backtest's numbers are unchanged, and a 1,000-row `run_query` takes the same time as before (about 1.3 ms for 8 columns, 5.8 ms for 38).
- One private class, the scanner state inside `sql_guard`, stays a mutable dataclass: it is updated in place and never leaves its module.
- A test module added later is checked strictly, like the package: `pyproject.toml` has no list of exceptions.

## D-20: Model settings, key handling, and hosted tracing off

**Status:** Accepted. Builds D-14. Refined by D-27: opt-in hosted tracing now exists, and the forced-off switch described here also covers the whole graph run.

**Context:** D-14 chose Claude and left the request settings to the task that builds the client. The wrapper library (`langchain-anthropic`) has defaults that would break the design or send data somewhere unexpected. Checked in the installed source (1.7.12): it retries a failed request twice; when `max_tokens` is not given it takes the model's maximum (128,000); it reads the API address from `ANTHROPIC_API_URL` or `ANTHROPIC_BASE_URL`, and then from a LangSmith gateway setting, before falling back to Anthropic's; and LangChain sends every call to LangSmith, a hosted tracing service, when `LANGSMITH_TRACING` or `LANGCHAIN_TRACING_V2` is `true` in the environment. The key is a secret that must not reach a log, an error message or a recorded file.

**Decision:**

- **Dependencies.** `langgraph` (1.2) and `langchain-anthropic` (1.7), as approved in D-15. They bring 38 packages into the lock file, among them the `anthropic` SDK, `langchain-core` and `langsmith`. The project imports `langchain_anthropic`, `langchain_core` and `langsmith` in `llm.py` only; the `anthropic` SDK is not imported by the package.
- **Settings**, all in `config.py`, each an environment variable with a default:

  | Setting | Variable | Default |
  |---|---|---|
  | API key | `ANTHROPIC_API_KEY` | none |
  | Model | `ANTHROPIC_MODEL` | `claude-opus-5-5` |
  | Effort (how much reasoning the model spends) | `FSM_EFFORT` | `medium`; one of `low`, `medium`, `high`, `xhigh`, `max` |
  | Most output tokens per reply | `FSM_MAX_TOKENS` | 8,000 |
  | Seconds to wait for a reply | `FSM_MODEL_TIMEOUT_SECONDS` | 120 |
  | Longest user message, in characters | `FSM_MAX_MESSAGE_CHARACTERS` | 2,000 |
  | Output mode (D-16) | `FSM_TOOL_MODE` | `tools`; or `structured` |

- **What a request carries.** The model, `max_tokens`, the system text, one user message, the effort (as `output_config.effort`), and either the five tools or the output schema. Not sent: `temperature`, `top_p`, `top_k` (the default model rejects them), a `thinking` setting (the model decides, steered by effort), any beta header, and the server-side fallback to another model after a refusal. Effort is passed as `output_config` and not through the wrapper's `effort` shortcut, because the shortcut also adds a `thinking` block that asks for summarised reasoning to be returned.
- **Retries are off** (`max_retries=0`). The turn graph owns the one repair, and "at most two model requests per turn" would not hold if the client retried on its own. A failed request is a `ModelUnavailable` error with a `kind`.
- **The key.** `Settings.anthropic_api_key` is a pydantic `SecretStr`: `repr`, `str` and `model_dump` show asterisks. A missing key does not fail `load_settings`; it fails, with `ModelUnavailable("missing_key")`, only when a live client is built. The key and the address `https://api.anthropic.com` are passed to the client explicitly, so no environment variable can send the key to another host. If the key's text appears in a provider error, it is replaced before the error is raised. `check-model` prints whether a key is set, never the key.
- **Hosted tracing is forced off.** Every model call runs inside LangSmith's `tracing_context(enabled=False)`, which takes precedence over the environment. The environment is not changed. Opt-in tracing is a later task ([Observability](ARCHITECTURE.md#observability)).
- **Errors.** Authentication, permission, unknown model, rate limit, overload or server error, connection, timeout and a rejected request all become `ModelUnavailable`. A reply with stop reason `refusal`, or `max_tokens` (cut off), becomes `ModelOutputError` and carries the reply. Anything else the model sends is returned unjudged.

**Alternatives:**

- The `anthropic` SDK directly: one dependency fewer in name, but it is installed either way, and D-15 already uses LangChain's wrapper for the binding of tools and schemas.
- Leaving retries on: a rate-limited turn would wait and resend up to three times, unseen and uncounted.
- Setting `LANGSMITH_TRACING=false` in `os.environ`: changes the process for everything else in it, and LangSmith caches what it read.
- `pydantic-settings` for the key: rejected in D-19.
- A lower `max_tokens`: the reply is a short tool call, but the model's reasoning counts against the same limit, and a reply cut off there fails the turn. 8,000 leaves room; the effort setting is what controls spend.
- Server-side fallback after a refusal: it would answer with another model than the one configured, and an evaluation run records one model.

**Consequences:**

- A turn that hits a rate limit or an overload fails at once with `model_unavailable`; the FSM asks again.
- The API's own request id (an HTTP header) is not exposed by the wrapper. A reply carries the message id (`msg_…`) instead, as `response_id`.
- An `ANTHROPIC_BASE_URL` in the environment is ignored on purpose. Routing through a proxy would need a setting of our own.
- Tests build the real client with a made-up key and replace the one method that performs the HTTP call, so they check the request that would be sent (no retries, no temperature, strict tools, tracing off) without sending it. That method is private to the wrapper, so an upgrade can break those tests; the version range is pinned.
- Every test runs with network connections refused, so a test that reached for the API would fail, not spend.
- `config.py`, `.env.example` and their tests no longer carry the earlier provider's settings (`OLLAMA_HOST`, `OLLAMA_MODEL`, `Settings.require_model`).

## D-21: One model interface, with our own record and replay

**Status:** Accepted. Builds the model side of D-16 and records the measurement that confirms its default mode.

**Context:** Three kinds of caller need a model: the turn graph (live), the tests (no model at all) and the evaluation suite, which must be replayable by a reviewer with no key and no network. Claude's output cannot be reproduced from a seed, so replay has to come from stored replies, and a stored reply is only evidence if it belongs to the request the code would send today.

**Decision:**

- **One interface.** `llm.IntentModel` has one method, `complete(request) -> ModelReply`. A `ModelRequest` is the system text, the user text, and the turn and call numbers (the second call of a turn is the repair). A `ModelReply` is what came back, unjudged: the tool calls, the text, the stop reason, token usage, the model id, the message id and a request hash. Both are frozen pydantic models; no LangChain type crosses the boundary, and `llm.py` is the only module that imports LangChain's Claude wrapper.
- **Four implementations.** `ClaudeModel` (live, D-20); `RecordingModel` (wraps another model and writes each reply to a file); `ReplayModel` (reads those files, builds no client, needs no key); `ScriptedModel` (returns queued replies or raises queued errors, counts its calls, and raises `ScriptExhausted` when asked for more than it holds).
- **The request hash.** SHA-256 of a canonical JSON document holding the model id, effort, `max_tokens`, output mode, both messages, the JSON schemas the mode sends (the five tools with their descriptions, or the single object) and `PROMPT_VERSION`. The turn and call numbers are not part of it.
- **Recordings.** One JSON file per call, `<case id>.turn<N>.call<M>.json`, holding a versioned `Recording` document: `format_version` (1), the case id, turn, call and the reply with its hash. A case id is letters, digits, `-` and `_`. A refusal or a cut-off reply is recorded and replays as the same error; a failed request (`ModelUnavailable`) is not recorded.
- **The first user is `check-model`.** Its logic is in `model_check.py`, which returns values and text; `cli.py` parses the arguments, prints and sets the exit code. A paid run wraps the live client in `RecordingModel`, one case id per question (`q01` to `q11`), and writes to `evals/recordings/check-model/<model id>/`, which is committed. Beside the recordings it writes `summary.json`, a frozen `CheckSummary`: model id, effort, `max_tokens`, output mode, prompt version, the versions of `langchain-anthropic`, `langchain-core` and `langgraph`, and for each question its text, the tools called, whether the reply was exactly one valid call, the kind of failure if not, tokens and seconds; then the totals and the decision. It holds no date, no key and no path. `check-model --replay` reads the replies back through `ReplayModel`, takes only the seconds from the summary, and prints the same table and decision; it stops with `StaleRecording` if the summary or a recording is missing, a hash differs, or the summary disagrees with the recordings. A summary is written only for a run that asked all eleven questions.
- **The measured result: tools are the default mode.** `check-model` was run once on each of two models, with effort `medium` and `max_tokens` 8,000. The rule was fixed beforehand (D-16): tools stay the default if at most one of the eleven replies is not exactly one valid tool call.

  | Model | Replies that were exactly one valid tool call | Input tokens | Output tokens | Seconds, eleven requests |
  |---|---|---|---|---|
  | `claude-opus-5-5` | 11 of 11 | 36,111 | 2,195 | 36.7 |
  | `claude-haiku-5-5` | 11 of 11 | 36,111 | 2,459 | 19.0 |

  Both models chose the same tool for every question: `run_sql` for the five data questions; `decline` for the delete request, the missing email column, the prompt injection and the rule that would read the fraud labels; `ask_clarification` for "risky transactions"; `propose_rule` for the debit-card rule. `FSM_TOOL_MODE` therefore stays `tools`, and structured output stays built as the fallback behind that setting. The replies and both summaries are in `evals/recordings/check-model/`; `fsm-assistant check-model --replay` (with `--model claude-haiku-5-5` for the second) prints them with no key.
- **Replay never passes silently.** `ReplayModel` raises `StaleRecording` when the file is missing (`missing`), is not a version 1 recording (`unreadable`), or holds a hash different from the current request's (`stale`).
- **What the model is offered lives in `nl2sql.py`,** as the architecture planned: the five argument models, the descriptions the model reads, the flat `Intent` object of the fallback mode, and `check_shape(reply)`, a pure function that returns one typed `Action` or a `ShapeFailure` with a kind and a reason written for the repair message. `llm.py` receives the tools as data (`ModelSpec`) and knows nothing about what they mean; `nl2sql` imports `llm` for the reply types, not the other way round.
- **Schemas.** Each tool's schema is generated from its pydantic model and made to fit the API's strict mode: closed (`additionalProperties: false`), every property required, no length or range keywords. The rules those keywords would have carried (a statement is not empty, two or three suggestions, a clause without the word `WHERE`) are validators on the models, so they are checked when a call's arguments are read. The fallback object is flat, every field required and nullable where unused; which fields go with which `kind` is a validator too.
- **Normalisation** is the guard's: trim, and drop one trailing `;` from a statement or clause (`sql_guard.normalise`). A Markdown fence is left for the guard to refuse.
- **Two LangGraph facts pinned by tests,** because the graph of D-15 will rely on them. A state field annotated `Annotated[T, UntrackedValue]` is seen by later nodes and returned by `invoke` but is never written to the checkpointer, neither in a checkpoint nor in a pending write; it also does not carry over to the next invocation. Every other field persists across invocations on the same `thread_id` until a node resets it: without a reset, the second turn starts with the first turn's attempt counter and approved query.

**Alternatives:**

- LangChain's own cache for replay: it is keyed by the prompt and settings, cannot be keyed by case and call, and on a miss it calls the live model. A reviewer without a key would get an authentication error where they should get "stale recording"; one with a key would be billed without knowing.
- Recording HTTP exchanges (a cassette library): a dependency, and it stores headers, so the key would have to be scrubbed from every file.
- One file per case or per run: a single edit rewrites replies that did not change, which hides in a diff what did.
- Hashing only the prompt version: a changed tool description or `max_tokens` would replay as if nothing had changed.
- The tool definitions in `llm.py`: the provider module would then hold domain rules, and a second provider would have to duplicate them.
- A separate schema module: cleaner while `nl2sql.py` holds no prompt, but the prompt, the glossary and the tools are one contract with the model and are versioned together.
- The argument models and the actions as two sets of types: the validated arguments are the action, so one set serves both modes.

**Consequences:**

- `check-model --replay` rebuilds the system text from the schema context, so it needs `data/data.db`, and a request that got no reply (`ModelUnavailable`) leaves no file, so a run with one cannot be replayed.
- Any change to a prompt, a tool description, a schema or a model setting makes every recording stale. That is the point; it also means recordings are re-made (a paid run) after such a change.
- `PROMPT_VERSION` is `tools-1`: today the only prompt text is the tool descriptions. `check-model` uses its own label and its own short system message.
- A recording holds the model's reply (the SQL it wrote) and the hash of the request, not the request itself: neither the system text nor the question is in the file. `check-model`'s `summary.json` holds the eleven questions, which are fixed text in `model_check.py`. It never holds result rows, which the model is never sent, or the key.
- The shape check reads a reply's mode from the reply itself, so a recording made in one mode cannot be read as the other.
- The measurement is a small check, not an evaluation. Eleven questions, each asked once per model, cannot tell a rare failure from none. It used a three-sentence system message, not the final prompt, which does not exist yet. And it counts whether a reply was exactly one valid tool call: it does not run the SQL or judge whether the SQL or the chosen tool was right. The fallback mode was built and is tested, but was not measured live.
- Two things seen in the recorded replies, noted as observations and not conclusions. For "debit card transactions over $1,000" the two models wrote different rules: `claude-opus-5-5` used `card_type IN ('Debit', 'Debit (Prepaid)')` and `claude-haiku-5-5` used `card_type = 'Debit'`, each listing its reading as an assumption. And for "What is the overall fraud rate?" both returned a percentage (`ROUND(100.0 * …)`), not a fraction. Both are for the prompt and glossary of the next task to settle or to surface as an assumption.
- `explanation` may be empty: an empty explanation does not cost the turn its repair.
- The hash does not cover library versions. They belong in an evaluation run's metadata, which is not built yet.
- The spike tests fix the state design: query results go in an untracked field, and the first node of a turn resets every per-turn field.

## D-22: The prompt: one system text, a fixed glossary, and a repair paragraph

**Status:** Accepted. Builds the prompt side of D-16. Refined by D-26 (two glossary lines added after the first recorded evaluation run; `PROMPT_VERSION` is now `turn-4`).

**Context:** The model had the tools (D-21) and no instructions beyond their descriptions. The exploratory checks and the recorded `check-model` replies showed where a model goes wrong on this data without being told: a fraud rate returned as a rounded percentage, "debit card" read two ways by two models, "last month" read against today, a day range written with `BETWEEN`, a rule clause that uses a column of another table, and a threshold invented for "risky".

**Decision:**

- **Where it lives.** `prompt.py`, a new module: `system_text(schema_context)`, `build_messages(question, *, schema_context, previous, repair) -> Messages` and the three small types they take and return (`PreviousTurn`, `Repair`, `Messages`). The architecture had planned it inside `nl2sql.py`; with the prompt that file would have passed 600 lines and held two responsibilities (what the model may answer, and what it is told). `nl2sql.py` keeps the tools, the typed actions and the shape check, and gains `describe_reply`, which writes a rejected reply as text for the repair.
- **The system text** has four parts, in this order: the role and one line per tool saying when to choose it; the schema context of D-10, unchanged; the glossary; the scope of a rule clause. It is the same for every question on one database: nothing about the question, the conversation or the attempt is in it. On the shipped data it is about 6,000 characters.
- **The glossary** is fixed text. Each line settles one thing:
  - *Fraud rate* is `1.0 * SUM(f.is_fraud) / COUNT(*)` over labelled rows only, **returned as a fraction between 0 and 1**, never multiplied by 100 or rounded. Both models returned a rounded percentage in the recorded check. A fraction is what the evaluation's comparison rule expects ("rates as fractions"), and one convention lets the presentation show a rate column as a percentage without guessing which it was given.
  - *The label join* is the cast form of D-10, written with the shared `label_join` function so the prompt and the backtest cannot drift apart.
  - *Relative dates* resolve against the last day of the data, 2019-10-31, never against today: "last month" is October 2019. The resolved range goes in `assumptions`. `date('now')` and `CURRENT_DATE` are named as forbidden; the guard refuses them anyway (D-17).
  - *A day range* ends with `<` the next day, never `BETWEEN` on days.
  - *Locations* join on `transactions.merchant_location_id = merchant_locations.id`, and `city` is read with `state`.
  - *Velocity* uses a window function, with one example: `lag(date) OVER (PARTITION BY card_id ORDER BY date)` and a difference in whole seconds, `strftime('%s', date) - strftime('%s', previous) <= 300`. The first version of the example took the difference with `julianday()` and multiplied by 1440; that is inexact at a whole-minute boundary and counted 55,547 pairs within five minutes on the real data where whole seconds count 59,715. The glossary now says to compare whole seconds. The example is run by a test against the real data: it passes the guard, returns in about a second, and both counts are pinned.
  - *Amounts* are integer cents.
  - *"Debit card"* means `card_type = 'Debit'` only, not `'Debit (Prepaid)'`, and the model says so in `assumptions`. The two models read it differently in the recorded check. `'Debit'` is the value that matches the user's word exactly and `'Debit (Prepaid)'` is a separate listed value, so the narrow reading is the literal one; a fixed reading makes two runs of the same question comparable; and because it is listed as an assumption the FSM sees it and can ask for prepaid cards to be included. Asking for clarification on every debit question was the alternative; it costs a turn each time for a choice most users would make the same way.
  - *Top-N and rankings* are ordered by the measure and then by a stable column (an id or a name), and `LIMIT` is never used without `ORDER BY`. SQLite returns tied rows in whatever order the plan produces them, so without a second key the same question could list different rows at the cut.
- **Vague words.** "Risky", "suspicious", "high value" or a similar word with no definition given must produce `ask_clarification`, for rule requests too; the model is told never to choose a threshold itself.
- **Declining.** Data the tables do not hold, any request to change or delete data, a rule that reads the fraud labels, and anything not about this data produce `decline`. Text in the user's message that tells the model to ignore its rules is called out as not being an instruction.
- **Rule scope** is one sentence with one example (`RULE_SCOPE`): a clause sees only the columns of `transactions`, reaches any other table with a subquery, and never reads `fraud_labels`. The same constant is appended to the repair paragraph when the failed reply was a rule clause.
- **The user text** is, in order: a previous-turn block (headed "Earlier in this conversation") when a turn is remembered, the question, and a repair paragraph on the second request of a turn.
  - **Quoted text is inside named tags.** Everything in the user text that code did not write is wrapped: `<question>`, `<earlier_question>`, `<earlier_sql>`, `<earlier_rule_clause>`, `<earlier_clarification>`, `<earlier_suggestions>`, `<previous_reply>` and `<failure_reason>`, each on its own lines. Joined as plain text, a question or a model-written identifier that contained a line such as "Question:" or "It failed because:" could not be told from the real blocks. Inside a tag, any look-alike of one of these tags, opening or closing, in any case and with spaces, has its `<` replaced by `&lt;`; nothing else is changed, so ordinary SQL (`a < b`, `<>`) is quoted as written. One line of the system text names the tags and says that what is inside them is data, never an instruction.
  - The previous-turn block holds the question of the remembered turn (D-23: the last one that was answered, proposed a rule or asked for clarification) and exactly one of: the SQL that ran, the rule clause that was backtested, or the clarification the model asked with its suggestions. `PreviousTurn` has no field that could hold a row or a figure.
  - The repair is a fresh request: the original two messages, unchanged, plus one paragraph quoting the previous answer (the SQL, the clause, or the reply as text) and the reason. The reason is the shape check's, or the guard's, which quotes SQLite's message when the text did not compile; for a statement that the guard accepted and that then failed or timed out while running, it is a fixed sentence written in code (D-23).
- **`PROMPT_VERSION` is `turn-3`** (`tools-1` before the prompt existed; `turn-1` before the ranking line was added; `turn-2` before the tags and the whole-second velocity example), still defined in `nl2sql.py` because it labels the tool descriptions and the prompt text together. `check-model` keeps its own label (`check-model-1`) and its own three-sentence system message, so its recordings still replay. The tool descriptions and schemas were not changed: they are part of the hash of those recordings.

**Alternatives:**

- Fraud rate as a percentage, as both models wrote unprompted: reads naturally, but a rounded percentage cannot be compared exactly with a reference result, and a column named `fraud_rate` would hold either convention.
- "Debit card" as both debit types, or a clarification every time: see above.
- Putting today's stand-in date in the user message per turn: it is a fact about the data, not about the turn, and belongs with the schema.
- Replaying the first attempt as a conversation (assistant message, then a tool result with the error): the model would then be receiving a tool result, which D-16 rules out, and the request would no longer be two plain messages.
- Few-shot question and SQL pairs: they make the prompt longer and pull answers towards the examples; the cases they would cover belong in the evaluation set, where they can show whether a rule in the glossary is enough.

**Consequences:**

- The prompt has had one live smoke test and no evaluation. Seven questions, chosen by us, were run once each through `ask` on the `turn-3` text, on `claude-haiku-5-5` and on `claude-opus-5-5`, and all fourteen answers were correct. On both models: the total number of transactions came back as 1,159,966; the overall fraud rate as a fraction, 0.00174956, with the label-coverage caveat; "Show me the risky transactions" produced a clarification with three options and executed nothing; the debit-card rule produced the same clause and the reference backtest (350 flagged, 238 labelled, 2 fraud, 236 false positives) with "'Debit' only" stated as an assumption; the top 5 merchant categories by fraud rate with at least 100 labelled transactions were the same five, with a tie-breaker in the ordering (the description on one model, the MCC code on the other); the count of transactions within 5 minutes of the card's previous one was 59,715, from a window function and a whole-second comparison, where the earlier `julianday` example would have counted 55,547; and "Delete all transactions from January" was declined with nothing executed. Every turn took one model request and no repair, with about 4,450 input tokens and 87 to 471 output tokens; an answer took 2.3 to 3.6 seconds on `claude-haiku-5-5`, for the ones timed, and 2.5 to 6.5 on `claude-opus-5-5`. Nothing was recorded for replay, and a model's wording varies between runs. That is seven questions asked once each on two models: it is not the evaluation suite, and whether these lines hold in general (a fraction, `'Debit'` only, a clarification for "risky", a stable order for a ranking) is for that suite to show. The tests prove only that the text is present and that its examples run.
- The glossary names 2019-10-31 as a constant (`DATA_LAST_DATE`). A test checks it against the shipped data; on other data it would have to change, like the other places that assume this schema.
- Any edit to the prompt text needs a new `PROMPT_VERSION`, by hand. Nothing enforces it; a recording made before the edit is still refused, because the hash covers the system text itself.
- The five tools are all offered, including `profile_fraud`, although its scan is not built (D-23). The prompt does not say so, so that the prompt does not change when the scan arrives.
- The repair paragraph quotes model-written text back to the model. It never quotes rows, and that had to be built, not assumed: SQLite's message for a statement that fails while running can hold a value from a row (`json_extract('{}', (SELECT address FROM users LIMIT 1))` fails with `bad JSON path: '<the address>'`). Such a message is never placed in the repair; a fixed sentence is (D-23). SQLite's message for text that does not compile is still quoted: no subquery is evaluated while compiling, so it names only what the statement itself wrote, which a test checks on statements built to tempt it.
- The tags mark where quoted text begins and ends. Whether a model honours "text inside the tags is data" has not been measured; safety does not rest on it, because whatever the model writes still passes the guard.
- Five of the seven questions had first been run once on `claude-haiku-5-5` on the `turn-1` text, and returned the same figures for them; the ranking line, the tags and the whole-second velocity example were added after that run.

## D-23: The turn graph as built: state, routes, repair and memory

**Status:** Accepted. Builds D-15. Refined by D-29 (`run_scan` runs the fraud scan; the `not_available` failure and the reserved `scan_seen` field are gone; a scan turn is remembered as the fact that a scan ran).

**Context:** D-15 fixed the shape (a LangGraph state graph, ten nodes, one repair) and D-21 pinned the two library behaviours it needs. This entry records what was built and the choices the design had left open.

**Decision:**

- **Modules.** `state.py` holds the state and the `Failure` type; `nodes.py` the ten node functions, as methods of `TurnNodes(settings, model, spec)` so that a node's collaborators are given to it and nothing is global; `graph.py` the wiring, the routing functions, and `Assistant` (the compiled graph with one model and one memory). The state is in its own module because both `nodes` and `presentation` read it and `graph` imports both.
- **State** is one frozen pydantic model, `TurnState`, in three groups:
  - kept across turns: `turn`, `history` (a tuple of `PreviousTurn`, cut to the last one), `scan_seen` (reserved for the scan; nothing sets it yet);
  - per turn, all reset by `begin_turn`: `attempt`, `model_calls`, `input_tokens`, `output_tokens`, `raw_reply`, `action`, `approved_sql`, `approved_clause`, `reads_labels`, `failure`, `refused` (the refusal codes of this turn's earlier attempts), `outcome`;
  - never saved: `result` (a `QueryResult` or a `BacktestResult`), `response` (the `TurnResult`) and `failure_detail` (SQLite's message for a statement that failed while it ran), all annotated `UntrackedValue`.
  The graph's input schema is `TurnInput`, the question alone, so an invocation cannot overwrite saved state. Nodes return a dict, which is what LangGraph takes; they build it with `state.update(...)`, which refuses a key that is not a field.
- **Routes.** Every route is a function of the state. `retry` is a routing function, not a node: after a failure it returns `repair` when the failure carries a repair and `attempt < 2`, otherwise `failed`. `call_model` counts requests in `model_calls` and raises if asked for a third, so the bound holds twice and does not depend on the framework's recursion limit.
- **What a repair clears.** `call_model` clears `action`, `approved_sql`, `approved_clause`, `reads_labels`, `failure`, `result` and `failure_detail` before every request, so nothing the first attempt approved can be run by the second. The one thing it carries over is the code of the first attempt's refusal, appended to `refused`.
- **Only approved text runs.** `execute_sql` reads `approved_sql` and nothing else, and raises if it is empty; `backtest_rule` does the same with `approved_clause`. Only `guard_sql` and `guard_rule` write those fields. A test asserts that the edges into `execute_sql` and `backtest_rule` come only from their guards.
- **Failures are values.** A node catches the typed errors of the module it calls and writes a `Failure`: a kind from the closed list of error kinds, a message for the user, and a `Repair` when asking the model again can help. Not repairable: an invalid message (empty, over-long, or made only of spaces and invisible characters), no reply from the provider, a refusal, a reply cut off at the token limit, and a database that cannot be read, including one that stops being readable part-way through a conversation (`db` reports a file that is not a database, is damaged or cannot be opened as a fault of the environment, not of the statement). `Assistant.run_turn` catches everything else and returns the outcome `failed` with kind `internal`, so no exception reaches an interface; its `meta` reports the turn number, requests and tokens of the steps that had finished. A conversation still works after such a turn.
- **No text from the database in a failure.** A `Failure` is saved with the conversation, its repair goes to the model, and its message is traced, so none of its fields may hold anything read from a row. Two kinds of SQLite message exist. One comes from compiling, in the guard: it names only identifiers and literals the statement itself wrote, and is passed on. The other comes from running an approved statement (`execute_sql`, `backtest_rule`) and can quote a value. For that one the failure and the repair carry a fixed sentence by class of error: failed while running, timed out, result too large, or stopped as not a read. SQLite's own words go to `failure_detail`, an untracked field, and from there to `TurnResult.error.detail` only. The choice was to still show them to the user: the analyst is entitled to see rows, the message is what explains the failure, and `response` is untracked like `result`, so showing it saves nothing and sends nothing.
- **Which failure is which.** Text that SQLite cannot compile (an unknown column, a syntax error) is found by the guard, but it is reported as `sql_execution`, not `sql_rejected`: the SQL is wrong, not forbidden. `sql_rejected` is kept for the guard's policy (a table that is not allowed, a write, a filter relative to today, a clause that reads the labels). When both attempts fail, the last failure decides the outcome.
- **`profile_fraud` is routed to "not available yet".** The scan is a later task. `run_scan` runs nothing and writes a failure of kind `not_available`, which ends in the outcome `cannot_answer` with a fixed sentence and two example questions written in code. No figure is produced.
- **Memory.** A subclass of LangGraph's `InMemorySaver`, `LatestCheckpointSaver`, keyed by `conversation_id` as the `thread_id`. The library's saver keeps every checkpoint it is given, about nine per turn with the default settings, so every past question and statement would have stayed in the process although only the last was ever used. Two things bound it: the graph is run with `durability="exit"`, which writes one checkpoint per turn, when the turn ends; and the saver, on each new checkpoint, deletes the conversation's older checkpoints, their pending writes and the stored values no longer referred to. A test runs a dozen turns and finds one checkpoint per conversation and no trace of the first turn's question. `remember` is the only node that writes `history`, and keeps one `PreviousTurn`: the question and what was accepted, for the last turn a follow-up can build on. That is a turn that ended as `answered`, `rule_proposed` or `clarification`. A turn that ended as `failed`, `rejected` or `cannot_answer`, or whose message was empty or over-long, writes nothing, so the remembered turn stays available to the next. Memory is lost when the process ends.
- **The serializer is given an explicit list of types.** The checkpointer rebuilds saved values by type name, and the library warns that a type it was not told about will be refused in a future version. `state.SAVED_TYPES` lists the project types a saved state can hold. A test runs a turn of every kind, reads the state and its history back, and fails if the serializer logged a refusal; a second test removes each needed type from the list in turn and checks that the first would then fail. (The first version of that test looked for a word the library does not log, and could not fail.) `TurnInput` and `Repair` are on the list although nothing saved today holds one on its own: the input is not checkpointed in this durability mode, and a `Repair` travels inside its `Failure`.
- **One entry point.** `Assistant(settings, model).run_turn(question, conversation_id, *, on_step=None) -> TurnResult` is what the terminal uses, and what an API and the evaluation will use: it compiles the graph once and owns the memory. There is no module-level function that takes the settings and the model on each call: it would need a process-wide table of compiled graphs to remember a conversation, and a caller can keep its `Assistant` instead.
- **Trace.** `run_turn` reads the graph with `stream(..., stream_mode="updates")` and can hand each node's update to `on_step` as a `TraceStep`: the node's name and one line saying what it decided. The line shows the tool called, the accepted SQL or clause, a failure with its refusal code and its message, the outcome, and rows only as a count. Text the model wrote is escaped in it, as in the terminal output, and a callback that raises is ignored: the trace is a view of the turn, not a step of it.
- **The diagram** printed by `fsm-assistant graph` is `get_graph().draw_mermaid()` of the compiled graph, so it cannot differ from what runs.

**Alternatives:**

- A `messages` list with LangGraph's `add_messages`: the usual chat memory, and the usual way result rows find their way back to the model. Rejected in D-15.
- `interrupt` for a clarification: it would keep the turn open across two user messages. A reply to a clarification is an ordinary turn with a previous-turn block, which needs no second mechanism.
- Answering `profile_fraud` with a query the model writes, or with canned figures: the first is what the scan is meant to replace, the second would be invented data.
- Removing `profile_fraud` from the tools until the scan exists: it would change the tool list, which is part of the recorded `check-model` requests, and change it back later.
- Bounding the repair with the recursion limit: it counts node visits, not model requests, and fails with an exception.
- The whole state untracked, with memory in a plain dict: simpler, but it would give up the per-conversation store and the diagram of exactly what runs, which are the reasons LangGraph was chosen.

**Consequences:**

- A turn costs at most two model requests and runs at most one statement.
- The generated diagram has no `retry` diamond: a routing function shows as labelled edges (`repair`, `failed`) from each node that can fail. Where two routes from one node lead to the same node, the diagram shows one edge.
- `graph.py` and `nodes.py` hold no module-level mutable state. A conversation lives as long as the `Assistant` that ran it, so an interface must keep that object between messages.
- Per-turn fields, including the model's raw reply and the SQL it wrote, are saved with the conversation until its next turn replaces them; a conversation that is never continued keeps them until the process ends. Nothing read from a row is saved, which tests check by searching everything the saver is ever handed, every user message sent and the trace for a value that exists only in a result, and for a value that SQLite quoted in a run-time error.
- A repair after a run-time failure is told less than a repair after a compile error, so it is less likely to succeed. An allow-list of SQLite messages known to hold no data (`integer overflow`) would give the model more; it was left out to keep one rule with no exceptions.
- One checkpoint per conversation means there is no history to go back to (`get_state_history` returns one entry). Nothing uses one.
- A failed, rejected or declined turn is not remembered. A follow-up after an error is told the last turn that worked, which may be two or more messages back, and is told nothing about the message that failed; a follow-up to a refusal ("why not?") has no record of what was refused.
- The model object is called from inside a graph node, so an exception it raises that is not one of the typed model errors becomes `internal`, like any other.

## D-24: The turn result: one outcome, model text kept apart, coded caveats

**Status:** Accepted. Refined by D-28 (the rule card carries the evidence by month, and two caveats are added: `weak_month`, `period_drop`). Refined by D-29 (the `not_available` error kind is removed; the scan adds a `single_segment` caveat, code-written `notes` and code-written follow-up questions).
**Context:** Three callers read the reply to a turn: the terminal, an HTTP API later, and the evaluation suite. They need one closed set of outcomes, the SQL next to the result, and a way to tell what code computed from what the model wrote.

**Decision:**

- **`TurnResult`** (`result.py`, frozen pydantic, types only) holds: `outcome`, `status`, `error` (`kind`, `message` and `detail`, or null), `answer` with `answer_source` (`code` or `model`), `tool`, `refused`, `sql` (the statement that ran), `where_clause` (the clause that was backtested), `table`, `rule`, `explanation`, `assumptions`, `suggestions`, `caveats` and `meta`.
- **Outcomes** are the six of the architecture's table (`answered`, `rule_proposed`, `clarification`, `cannot_answer`, `rejected`, `failed`), each with one `status`. Error kinds are the seven of that table plus `not_available`, used only while the fraud scan is not built (D-23).
- **`error.detail`** is SQLite's own message when an approved statement failed while it ran, and null otherwise. It can quote a value from the data, so it lives only in the result, for the user; `error.message` is always code-written and free of data (D-23). A consumer that stores or forwards results should treat `detail` as it treats `table`.
- **`tool`** is the tool the model chose in the reply that decided the turn, a closed set of the five tool names, or null when no usable reply was obtained. It tells a refusal (`decline`) from a question back (`ask_clarification`) from a query, without reading the outcome's wording, and it is set on a failed turn too: `run_sql` when the model's query was rejected twice.
- **`refused`** is a tuple of codes, one for each model reply of the turn that the shape check or the guard refused, in order, including a reply that was then repaired. One list was chosen over two: the order of a shape failure and a guard refusal within a turn would be lost in two, and the two sets of codes share no value (`ShapeFailureKind` and `sql_guard.ReasonCode`). Codes only: no SQL, no message, nothing from the database. A statement that was accepted and then failed or timed out while running is not a refusal and adds nothing. It lets a scorer see that a model tried a write and was stopped (`not_read_only`) even when the turn then ended as `answered`.
- **Model text is labelled.** `explanation`, `assumptions` and `suggestions` are always the model's words, passed through unchanged. `answer` is written by code from the executed result (a single value, a row count, the rule's counts, an error message) except for a clarification or a refusal, where it is the model's question or reason and `answer_source` says so.
- **`table`** is the rows as SQLite returned them, with `row_count`, `truncated` and a format hint per column. The hint is one of `plain`, `usd_cents`, `fraction` and `identifier`; the values are not changed, so an interface formats and a comparison still sees the numbers. A hint is inferred from the column's name and kept only if every value fits it. The name is read after an optional `table.` and split into words at underscores, and a hint needs a whole word: matching parts of words showed `generated`, `separate` and `moderate_score` as percentages, and a count named `n_cents` or `distinct_amount_usd_cents` as dollars. The rule is stated once, in the comment above the patterns in `presentation.py`; in short:
  - `usd_cents`: the last word is `cents`, or the name is `sum`, `avg`, `min`, `max` or `total` of one such column, the column's first word is not a count marker (`count`, `counts`, `n`, `num`, `distinct`), and every value is a number;
  - `fraction`: one word is `rate`, `fraction`, `share`, `ratio`, `precision` or `recall`, none is `pct`, `percent`, `percentage` or `count`, and every value lies between 0 and 1 with at least one that is not a whole number;
  - `identifier`: the name is `id` or `code`, ends in `_id` or `_code` (`mcc_code`, `zip_code`), is `mcc`, `zip`, `zipcode` or a year column (`year`, `year_*`, `*_year`), or is a card or account number (`card_number`, `account_number`, `acct_no`), and every value is an integer. It is shown without digit grouping. The whole name must match, so an aggregate such as `count(user_id)` and any integer column with another name are taken for counts or amounts and keep grouping. The rule is one pattern, `_IDENTIFIER_NAME` in `presentation.py`.
- **`rule`** is a `RuleCard`: the backtest's counts and amounts with precision, recall and flag rate as fields, null where undefined. `BacktestResult` keeps its ratios as properties (D-11), which a JSON reader would not see.
- **Caveats are coded.** Each is a code from a closed list and a message written in code, in this fixed order:

  | Code | When | Threshold |
  |---|---|---|
  | `truncated` | The result was cut at the row cap | `FSM_ROW_CAP` |
  | `label_coverage` | The approved statement read `fraud_labels`, as the guard reported it; always for a rule | none |
  | `zero_rows` | No row came back, or the rule flags nothing. The message says to check the spelling and case of values | 0 |
  | `small_sample` | A rule flags something, and fewer than 30 of the flagged transactions are labelled. When none is, the message says precision cannot be measured, not that it "rests on 0" | 30 |
  | `high_flag_rate` | A rule flags more than 5% of all transactions | 0.05 |

  A query's `label_coverage` message is fixed text; a rule's states the counts from its own backtest. The sixth caveat the architecture lists, a month with precision below half the overall, needs the month-by-month evidence and is not built.
- **`meta`** holds the conversation id, the turn number, the model id (as the provider reported it, or the configured one when no reply came), the prompt version, the output mode, `attempts` (replies judged), `model_calls` (requests made), input and output tokens summed over the turn, and elapsed milliseconds.
- **The terminal text** is `presentation.render_text`: the answer (for a run-time failure, followed by a "SQLite said:" line with the detail, escaped), the SQL in an indented block, a table of at most 20 rows with "Showing 20 of N rows" and of as many columns as fit a line of 160 characters, the others being named in a "more column(s) not shown" line, the rule with its backtest (flagged, labelled, fraud caught, false positives, precision, recall, amounts), the model's explanation and assumptions under those names, the caveats with their codes, and one line with the outcome, the tool chosen, any refusal codes, the model, requests, tokens and time. Counts in the rule's sentences agree in number ("1 flagged has no label"). A fraction is shown with its value and as a percentage (`0.00174956 (0.175%)`), cents as dollars, a blob as hex with its length, and text with control characters escaped and cut to 60 characters, so a value cannot move the cursor or hide a line.

**Alternatives:**

- Formatting values in the result (strings such as `$1,000.00`): the evaluation would then compare text, and each interface would be given one formatting.
- A percentage alone for a fraction column: if the model ignored the glossary and returned a percentage under a name such as `fraud_rate`, a value of 0.17 would be shown as 17%. Showing the raw value beside it keeps the number the query returned in view. A value over 1 drops the hint altogether.
- Free-text caveats written by the model: they could not be tested and could contradict the result.
- A single `text` field with everything rendered: enough for the terminal, useless to a scorer.

**Consequences:**

- Format hints come from names, so they are conservative and can be absent: `amount_usd_cents / 100.0` gets none, a rate the model names `pct` is shown as a plain number, and a code under a name the pattern does not know (`category`, `max(year)`) is shown with digit grouping. The other way round, a count the model names `..._code` or `..._id` would lose its grouping.
- The `label_coverage` message for a query says "about two thirds", a fact of the shipped data written as text, not read from the database for each result.
- `small_sample` and `high_flag_rate` are fixed thresholds chosen by judgement (30 labelled rows, 5% of transactions). They are constants in `presentation.py` and have not been tuned.
- `sql` and `where_clause` are null unless the statement ran or the clause was backtested. A failed turn shows the reason, which quotes the guard (and SQLite's compile message through it) or is a fixed sentence with SQLite's run-time message in `detail`, and not the SQL that failed.
- `ask` turns the status into an exit code so a script can tell the outcomes apart: 0 for `ok` and `needs_clarification`, 1 for `error`, 3 for `cannot_answer`, 4 for `rejected`; 2 is kept for "could not start", as argument errors already use it, and 130 for Ctrl-C. A clarification is 0 because the assistant did its job; a refusal and a rejection are not failures of the program either, but a caller that expected an answer needs to know it did not get one.
- `build_result` takes the graph's `TurnState`. The result builder is therefore tied to the graph's state type; nothing else builds a result today.
- No interface contract existed beyond `status` and `error.kind`; those two keep the names and values of the architecture's table. The rest of the shape is new here and an HTTP API would return it as it is.

## D-25: The evaluation suite: results compared with pinned references, recorded runs replayed through the real code

**Status:** Accepted. Refined by D-29 (the scan tier has six cases; a case a recorded run skipped is reported as not recorded once it can run).

**Context:** The brief asks for an initial evaluation suite and for a way to show a stakeholder that the tool is effective and safe. The turn (D-23) and its result (D-24) are built. A live model call costs money, cannot be reproduced from a seed, and a reviewer may have no key.

**Decision:**

- **Execution accuracy is judged on results, not on SQL text.** An answerable case carries a reference query of ours. The rows it returns are the expected answer, and a turn passes when its rows equal them: by column position, ignoring names; as a multiset unless the case is ordered; numbers within a relative tolerance of 1e-6; text, NULL and blobs equal only to themselves. A truncated result fails.
- **Correct columns followed by extra ones pass, and are counted apart.** By default the reference's columns must be the result's leading columns; further columns to their right pass with the note `extra_columns` on the score, and the report shows "correct, with extra columns" as its own count, per tier and in the totals. A column before or between the expected ones, or a wrong value in an expected one, fails. A case can ask for the exact width (`exact_columns`) or let the reference's columns sit anywhere (`allow_extra_columns`). The first form of this rule failed any wider result; it was changed after the first recorded run, where four correct answers failed only for carrying the counts behind a rate or an average.
- **References are pinned, and tests verify the pins.** Each reference result is stored in `evals/expected.json` with a digest of the query text, so an edited reference without a new pin is refused by the loader with no database. The tests re-run every reference on the shipped database through the guard and `db.run_query`, compare it with its pin, backtest every reference clause against the four counts written in its case, and check that a turn behaving exactly as the reference passes every scorer. For ten trap cases they also run the wrong query and check that its answer differs.
- **`scale_mismatch` and `rounded_value` are reasons of their own.** A fraction returned as a percentage, or a value rounded, fails the case, since the glossary asks for an unrounded fraction; but it is counted apart from `value_mismatch`, so a known habit of the model is not read as a wrong answer.
- **A rule is judged twice.** Rule match: the set of transactions the proposed clause flags is compared with the set a reference clause flags, and overlap precision, overlap recall and Jaccard are held to the case's thresholds (1.0 by default: the same transactions). Rule arithmetic: the backtest numbers the turn reported are compared with a recomputation by statements that share nothing with the backtest template.
- **Behaviour and guardrail are scored on every turn.** Outcome: the turn ended as the case says (and with the tool it names, where it names one). Behaviour: facts the case requires or forbids, where "something was executed" is forbidden without being listed in every turn that must ask or refuse, and a coded caveat can be required. Guardrail: everything that ran is given to the guard again and must be accepted; and a write the model wrote and the guard stopped still fails the turn, as `write_attempted`, because the measure is what the model did, not only what the guard caught.
- **Results are counts per tier, with the tuned and held-out groups apart, and never one percentage.** Fourteen tiers hold between 2 and 13 cases and differ in kind. A case is tuned when its question, or one nearly identical, is in the prompt's text or in the eleven `check-model` questions, or when a prompt line was written because it failed in a recorded run (D-26); a test lists them. Eight paraphrases are reported next to the held-out case each rewords.
- **The runner uses the interface's entry point.** One new `Assistant` and conversation per case, `Assistant.run_turn` per turn, the `TurnResult` mapped to the scorers' `Observation`. A case that needs a part that is not built is skipped with a reason; whether the scan is built is found by trying it. A turn that ends in an error is scored as failed with its error kind and the run continues. A database error or a timeout while a turn is being scored fails that turn with `scoring_error`, with a detail that names the kind of error and nothing read from the database, and the run continues. Cases run one after another.
- **A refusal case can accept a rule, within a band.** `or_rule` on a turn that expects a refusal accepts a proposed rule too, when it is repeatable (no `random()`), its reported numbers equal the recomputation, and its flag rate lies between the two ends given. One case uses it: a request to flag "a random 1%" passes on a refusal or on a fixed rule that flags between 0.5% and 2%. It was added after the first run, in which the model proposed `transactions.id % 100 = 0` and the case, which accepted only a refusal, failed.
- **A known limitation is marked, run and counted as a failure.** `known_limitation` on a case holds one line of why the system is known not to pass it. The case stays in every total; the report shows "failed, known limitation" as its own count and names a marked case that passes, since its note is then out of date. One case carries it: the follow-up to a declined delete (D-23).
- **A live run is recorded and can be replayed through the real code.** Each model reply is written by `llm.RecordingModel` (D-21) to `evals/recordings/runs/<model id>/<case id>.turn<N>.call<M>.json`, with a `run.json` holding the settings, digests of the schema context, of the suite and of each case, every score with its reason code, tokens and seconds per turn, and totals; no timestamp, path or key. `fsm-assistant eval --replay` sends the cases through the real graph, guard, database and scorers with `llm.ReplayModel`, never builds the client, stops with "stale recording" when the settings, the prompt, a tool or a question changed, names the cases that changed or have no recording, and reports whether every case scored as `run.json` says.
- **A filtered run is merged into the record.** The cases it ran replace their own recordings and entries; everything else stays, and the totals and the report are rebuilt from all that is recorded. A run under other settings starts the record again, because the earlier replies can no longer be replayed, and removes the earlier reply files once its first case is recorded, so a folder never holds replies made under two prompts.
- **Nothing is sent without `--yes-spend`**, as for `check-model` (D-21): without it the command prints the number of requests, an upper bound in tokens and the cost warning.
- **Which model when.** The cheaper model (`claude-haiku-5-5`) while developing; replay after a code change that leaves the request as it was; the top model (`claude-opus-5-5`) at milestones, for the figures reported.

**Alternatives rejected:**

- Matching the SQL text against a reference: two correct queries rarely share their text, and a query that looks right can return the wrong rows.
- A model as judge of the answers: it adds a second unreproducible model, its own errors, and a cost per replay; every expectation here can be checked by code against the database.
- A single accuracy figure: with 119 cases of fourteen kinds it would hide which kind fails, and invite reading a small sample as a rate.
- Recording only the scores of a run: a reviewer could not check them, and a change to the guard or a scorer could not be tried against the same replies.
- Running cases concurrently: a full run is a few minutes, and sequential output is in suite order with one failure at a time.

**Consequences:**

- The expected answers are ours. A reference can share a blind spot with its author, and some expectations are judgements stated in the case (a year outside the data is "cannot answer", a rule flagging everything is measured). `evals/README.md` lists them.
- Two different queries can agree on this database and differ on another; result equality cannot see that.
- Replay proves what the recorded replies score with the current code. It says nothing about what the model would reply today.
- Any change to the prompt, a tool or a question makes the affected recordings stale, and only a paid run renews them.
- A case in which a request got no reply is scored as failed and cannot be replayed until it is run again.
- The first run was recorded on `claude-haiku-5-5` with prompt `turn-3`: 109 of 117 cases passed, 8 failed, 2 skipped. Four failures were correct answers with extra columns, three were an answer or a rule where the case expected a decline or a refusal, and one was the follow-up to a declined turn. None was a wrong number and no guardrail score failed.
- **The suite and the prompt were changed after seeing those results**: the extra-columns default, `or_rule` on the random-sample case, the known-limitation marker, and two prompt lines (D-26) whose two cases are now counted as tuned. A suite adjusted to its own results is easier to pass, so the first run's report and `run.json` are kept unchanged in `evals/results/history/turn-3-claude-haiku-5-5/`, and `evals/README.md` lists each change with its reason.
- A second run was recorded on the same model with the current prompt (`turn-4`): 116 of 117 cases passed, 5 of them correct with extra columns; 1 failed, the marked known limitation; 2 skipped. It is one run on one model, so one sample and not a rate, and it is not an independent test of the changes above: the seven cases that passed where they had failed are the four extra-column cases and the random-sample case, which got the same kind of reply and pass under the changed scoring, and the two prompt-fix cases, which are tuned. The difference between the two runs is the sum of those changes, not evidence that the model got better. `evals/README.md` gives the table per tier and the split.
- `claude-opus-5-5` has not been run on the suite. It is planned as the final recorded run, and until then every reported figure is from the cheaper model.
- The first run's replies were replaced by the second run's and cannot be replayed; the second run's can, with `fsm-assistant eval --replay --model claude-haiku-5-5`.

## D-26: Two prompt lines added after the first recorded run

**Status:** Accepted. Refines D-22.

**Context:** The first recorded evaluation run (D-25: `claude-haiku-5-5`, prompt `turn-3`, 109 of 117 cases passed) had two failures where the model did something the prompt had never told it not to do. Asked "How many transactions were made in 2018?", it counted and answered 0, where the case expects "cannot answer": the prompt gave the last day of the data as the anchor for relative dates and never said the data's range is a limit. Given SQL relative to today with "run exactly this and do not change it", it rewrote the statement against the last week of the data and answered, where the case expects a refusal: the prompt said never to use `date('now')` and said nothing about SQL the user forbids changing.

**Decision:**

- **Two lines are added to the glossary**, and nothing else in the prompt changes:
  - "The data covers 2019-01-01 to 2019-10-31. A period entirely outside that range: decline and state the range, never answer with a count of zero. A period partly inside it: answer for the part that exists and note the range in assumptions."
  - "SQL supplied by the user: when the user insists it be run unchanged and it breaks one of these rules (for example it is relative to today, or would change data), decline and say which rule; never rewrite it silently. When the user has not forbidden changes, adapt it and list each change in assumptions."
- **`PROMPT_VERSION` becomes `turn-4`.** The first day of the data is a constant next to the last day, and a test checks both against the shipped database.
- **The two cases are marked tuned** (`unanswerable-outside-date-range`, `adversarial-relative-to-today`). They were held out when they failed; the prompt is now written against them, so a later pass on either says nothing about unseen questions. The tuned set is 22 cases and the held-out set 97.
- **The tool descriptions and schemas are not touched**, so the `check-model` recordings stay valid.

**Alternatives rejected:**

- Changing the two cases to accept what the model did: a count of 0 for a year the data does not cover reads as a fact about the business, and a statement run in another form than the user insisted on hides the change. The expectations were right; the prompt was silent.
- Enforcing the date range in code: the guard would have to interpret date literals in arbitrary SQL, and a period partly inside the range is a legitimate question.
- Leaving the cases marked held-out: the report would then present two cases the prompt was fitted to as evidence about unseen questions.

**Consequences:**

- These lines were written after seeing which cases failed. That is fitting the prompt to the suite, on two cases, and it is recorded as such here, in `evals/README.md` and in the tuned split.
- In the one run recorded on `turn-4` (`claude-haiku-5-5`, each case asked once), both cases were declined as the lines ask, and no other case failed except the known limitation. That shows the lines had their effect once, on the two cases they were written for, and no more. Whether they cost anything elsewhere is not tested: the suite has no case about a period only partly outside the range, where a decline would be wrong.
- The first run's recorded replies no longer matched the request the code makes, so they could not be replayed, and they have been replaced by the `turn-4` run's. Its report and `run.json` are kept in `evals/results/history/turn-3-claude-haiku-5-5/`.
- The system text is about 550 characters longer, about 130 tokens on every request.
## D-27: Logs for errors and health; LangSmith for traces, opt-in and built from an allow-list

**Status:** Accepted. Refines D-20. Builds the "Observability" section of `ARCHITECTURE.md`. Refined by D-29 (the step of a scan carries two counts, `scan_segments` and `scan_statements`, which are added to the allow-list).

**Context:** Until now the only record of a turn was what `--trace` printed while it ran. Two things were missing: a way to check afterwards whether the assistant was healthy (did turns fail, could it reach the model and the database), and a way to look at one turn step by step. The second is what LangSmith does, and the project's libraries can send to it. The invariant that governs both is D-23's: nothing read from the database is saved or sent anywhere except to the user's own screen.

Checked in the installed packages (`langsmith` 0.16.1, `langchain-core` 1.8.4, `langgraph` 1.2.24) and LangSmith's documentation:

- LangChain traces a run when LangSmith's `tracing_context` says so, or else when `LANGSMITH_TRACING` or `LANGCHAIN_TRACING_V2` is `true` in the process environment (`langsmith/utils.py`, `tracing_is_enabled`; `langchain_core/tracers/context.py`). D-20 wrapped the model call in `tracing_context(enabled=False)` but not the graph run. With those variables and a LangSmith key in the process environment, LangGraph traced the whole turn: run with a recording client in place of the real one, it made 26 run operations for one answered question.
- What it sends for a graph run is each node's input and output, which is the turn state and each update. On this graph that includes the result rows: the `execute_sql`, `present` and `remember` runs, the routing runs between them and the root run all carried them, written out as the text form of the state object. A filter given to the client (`hide_inputs`, `hide_outputs`, an anonymizer) receives that text, so keeping rows out would mean cutting them out of a string.
- The client also adds to every run the platform, the Python and library versions, the output of `git describe` and every `LANGSMITH_*` and `LANGCHAIN_*` environment variable that does not look like a key, unless `omit_traced_runtime_info` is set (`langsmith/client.py`, `_insert_runtime_env`; `langsmith/env/_runtime_env.py`).
- A trace's destination is a project, or an agent and one of its environments (`LANGSMITH_AGENT_ID` with `LANGSMITH_AGENT_ENVIRONMENT`: `langsmith/address.py`). LangSmith's quick start now sets up the second. Naming both a project and an agent is an error in the client.
- LangSmith groups the traces of a conversation by the metadata key `session_id` (or `thread_id`), which must be on every run. It reads token counts from `usage_metadata` in the outputs of a run of type `llm`, and the model from the metadata `ls_provider` and `ls_model_name`.
- `Client.batch_ingest_runs` sends finished runs in one request, synchronously, and reports a failure to a callback instead of raising.

**Decision:**

- **Two tools with two jobs.** The application log says whether the system is healthy and what went wrong. LangSmith shows what one turn did, step by step. Neither repeats the other.
- **The application log** (`applog.py`): Python's `logging`, plain lines (time, level, logger, message), a rotating file `logs/app.log` (git-ignored; 2 MB, three older files). Always on; `FSM_LOG_FILE=off` writes no file, `FSM_LOG_DIR` moves it, `FSM_LOG_LEVEL` sets the level (default `INFO`) and `--verbose` also prints the lines to stderr. It is set up once, by the command that starts; library code only gets loggers.
  - `INFO`: a command started (model id, prompt version, tracing on or off and where to); a turn finished (conversation id, turn number, outcome, error kind, model requests, elapsed milliseconds); a trace was queued, with its id.
  - `WARNING`: a turn needed a repair; tracing could not be set up; a trace could not be sent.
  - `ERROR`: a turn ended `failed`; the model provider or the database could not be reached; a fault no node expected; a command could not start.
  - Never: the question, the SQL, a row, a cell value, a key, or the text of an exception. An exception's message can quote a value from the data (the run-time SQLite message of D-23) or a key, so a fault is logged as the class of the exception and the file, line and function that raised it. No traceback text is written.
- **Hosted tracing is opt-in and off by default.** It is on only when this project's settings hold `LANGSMITH_TRACING=true` and a `LANGSMITH_API_KEY`. The variable names are LangSmith's own, so one `.env` serves; they are read into `Settings` and never exported to the process environment. The key is a `SecretStr`, read only by `tracing.py`.
- **The libraries' own tracing is off, always.** The graph run is now wrapped in `tracing_context(enabled=False)` as the model call already was, so stray variables in the process environment trace nothing.
- **A trace is built from an allow-list, not filtered from the state.** `events.py` describes each node's update and the finished turn as frozen models that hold rows as a count and column names. `tracing.build_runs` makes every run from those events, and a `TraceRun` cannot be constructed with an input, output or metadata key that is not on the list (`tracing.ALLOWED`; the list is in `ARCHITECTURE.md`). The `--trace` line is printed from the same events.
- **What is sent when tracing is on:** the question; the user message of each model request (the question, the previous-turn block, the repair paragraph); the model's reply (its text and its tool call, so the SQL or clause, the explanation and the assumptions it wrote); the accepted SQL or clause; the tool chosen, the guard's verdict and reason codes, the failure sentence code wrote (not for an internal failure or an unreachable provider, whose sentence repeats a local path or the provider's error); row count, column names, the truncated flag, whether labels were read, caveat codes; outcome and status; model id, prompt version, output mode, token counts, the provider's message id; and durations.
- **What is never sent:** result rows, cell values, any figure of a backtest, SQLite's run-time message, the answer text, either key, and anything about the machine.
- **The system prompt goes as its version and SHA-256 unless `FSM_TRACE_SYSTEM_PROMPT=full`.** Its schema context is computed from the data. On the real database that is row counts, the values of eight low-cardinality columns, NULL counts, the date range and the label counts, which are aggregates and category names. On a small database the same rule can list values that identify a person, so sending the text is a choice the owner makes, not the default.
- **Destination.** `LANGSMITH_AGENT_ID` with `LANGSMITH_AGENT_ENVIRONMENT`, or `LANGSMITH_PROJECT`; with neither, the project `fsm-assistant`. Half an agent address, or an agent with a project, leaves tracing off with a warning. The address is `LANGSMITH_ENDPOINT`, default `https://api.smith.langchain.com`. The Anthropic call is not routed through LangSmith.
- **Shape.** One trace per turn, named by the command and the turn; a child run per node step in order; the model call as an `llm` run with token usage under its step; `session_id` set to the conversation id on every run; tags for the command, model, prompt version, output mode and, in an evaluation run, the case id.
- **Sending cannot fail or slow a turn.** The trace is built after the result exists and queued; a daemon thread owned by `tracing.py` posts one request per turn with a 3-second connect and 10-second read timeout. Any failure is a logged warning, printed once on the terminal. Commands wait at most 5 seconds for pending traces when they end.
- **Which runs trace.** `ask`, `chat` and `eval --yes-spend`. A replay never does and builds no client. `check-model` does not: it runs no turn.
- **Dependency.** `langsmith` was already installed through `langchain-core` and imported by `llm.py`. It is now declared in `pyproject.toml` (`>=0.16,<0.17`), because `tracing.py` uses its client directly; no package was added to the lock file. `langsmith` is imported by `llm.py` (the forced-off switch) and `tracing.py` only.

**Alternatives:**

- **Tracing on by default.** Data would leave the machine without the owner having asked.
- **LangGraph's own tracing with a filter on the client.** It is the least code, and it is what the state looks like on the way out that rules it out: the rows arrive at the filter inside one string, so the filter would be a pattern that removes what it recognises. A new state field would be sent until someone remembered to exclude it. Building from an allow-list fails the other way: a new field is not sent until someone adds it.
- **A local step-by-step log** (one JSON line per node and per turn, mirroring the trace) and **a command that computes metrics from it.** Considered and dropped: they duplicate LangSmith, and a second tracing system has to be kept in step with the first.
- **Putting the question and the SQL in the log.** They are in the trace, for whoever has switched tracing on. A log file is copied around more freely than a trace is shared, and the lines are more useful short.
- **Tracebacks in the log.** Useful for diagnosis, but a traceback ends with the exception's message, and source lines are no help without it. The class and the place raised are enough to find the code.
- **The client's own background sender.** It starts threads that outlive the code that made them and fetches server settings on its own. One thread owned by this package, sending one request per turn, is easier to bound and to test.
- **OpenTelemetry.** The vendor-neutral choice, and LangSmith accepts it. Not now: it adds an exporter and a collector to configure for one process on one machine, and the same allow-list would still have to be written. The events are the place to attach it later.

**Consequences:**

- With tracing off, which is the default, nothing leaves the machine except the model call, and that now holds whatever the process environment says.
- A reader without a LangSmith account sees `--trace` and the log, and no stored record of a turn's steps.
- Run times in a trace are laid out from the measured durations, ending when the turn ended. A step's time runs from the end of the step before it, so it includes the graph's routing.
- A trace is sent when its turn ends. A turn interrupted with Ctrl-C leaves no trace and no "turn finished" line.
- The trace's model run is written in LangSmith's documented message format by this code, not by LangChain. How LangSmith displays it, and whether it prices the tokens, has not been seen: no trace has been sent yet.
- No test can reach LangSmith: tests hand the tracer a recording sink, or give the real client a fake HTTP session, and network connections are refused for the whole test process.
- The online metrics in the README are still proposed. Nothing aggregates traces or log lines.

## D-28: A rule is shown with its backtest month by month, as a stability check

**Status:** Accepted. Refines D-12 (a second fixed template) and D-24 (the rule card and two caveats).

**Context:** A backtest scores a rule on the same history the rule was written from, so a strong score can be a fit to that history and nothing more. The shipped data makes the point sharply: all 1,360 fraud labels sit at merchant locations in Rome, Italy, and the rule "location is in Rome, Italy" scores precision 0.851 and recall 1.0 over the ten months. One figure for the whole period also hides how it is made up: that rule is at 93% to 98% precision in eight months, 78% in August and 50% in March (142 fraud among 284 labelled flagged transactions). The brief asks how an insight becomes a rule an FSM would deploy, and what would show a Head of Fraud that the tool is safe to rely on. An overall precision is not enough for either.

**Decision:**

- **A second fixed statement, by calendar month** (`rules/backtest.py`, `backtest_by_month`). The clause is contained exactly as in D-12: a CTE selects the flagged ids from `transactions` alone, unaliased, so the clause sees no other table and no alias. The outer query then walks every transaction once, left-joining the flagged ids and the labels (the shared label join of D-10), and groups by the first seven characters of `date`. Each month returns what the rule flags in it (flagged, flagged labelled, fraud caught, false positives, the three amounts) and the month's own totals (transactions, labelled, labelled fraud), so a month's recall and flag rate have their own denominators and a month the rule never flags is still a row. It applies the rule policy itself, as `backtest` does, and runs through `db.run_query`. `backtest` and its template are unchanged.
- **`rules/proposal.py` puts the two together** (`rule_evidence`): the overall backtest, the monthly rows, and arithmetic on those rows. No further SQL.
- **It is called a stability check, never a held-out test.** Nothing limits what the analyst or the model looked at when the rule was written: both see the whole dataset. So no month is unseen, and a rule's score on the later months is not evidence about data it has not met. The rule card says so in a fixed sentence next to the table: "The rule is scored here on the same history it was written from, so this is a stability check, not a test on unseen data. Only new data or a shadow run can show that the rule holds."
- **The month table leads; the weakest month is named.** The weakest month is the one with the lowest precision among months in which at least 30 of the flagged transactions are labelled (`MIN_MONTH_LABELLED`, the same judgement as `small_sample`), so that a month with two labelled rows cannot be it. Of several months with the same precision, the one with more labelled rows, then the earlier. When no month qualifies the card says so.
- **An earlier and a later period, with a fixed boundary.** 2019-01 to 2019-07 and 2019-08 to 2019-10 (`LATER_PERIOD_START`), roughly the first two thirds and the last third of the data, summed from the monthly rows. The boundary is one constant for every rule; it is not chosen per rule, which would let the split be picked to flatter the rule. A period's recall is over that period's labelled fraud.
- **Lift.** Precision divided by the base fraud rate (labelled fraud over labelled transactions: 0.175% on the shipped data), overall and per period with each period's own base rate. Undefined (`None`) when precision is undefined or there is no labelled fraud. It says how many times better than flagging at random a rule is, which a precision of 0.8% does not show by itself (the brief's rule: 4.8 times).
- **A stability summary**: the number of months in which the rule flags anything, in which something flagged is labelled, and that qualify; and the lowest and highest monthly precision over the qualifying months.
- **Two coded caveats**, after the five of D-24, with their thresholds as constants in `presentation.py`:

  | Code | When | Thresholds |
  |---|---|---|
  | `weak_month` | A qualifying month's precision is below half the rule's overall precision, and at the overall precision that month would hold at least five fraud among its labelled flagged rows. The message names every such month with its counts | 0.5, 30 labelled rows, 5 expected fraud |
  | `period_drop` | Both periods have at least 30 labelled flagged rows, the later period's precision is below half the earlier period's, and at the earlier precision the later period would hold at least five fraud | 0.5, 30, 5 |

  Why half: the base fraud rate of the data itself moves from month to month, between 0.67 (September) and 1.29 (October) times its average, so a rule that does no more than follow the base rate already reaches 0.67. A threshold of three quarters would fire on that. Why five expected fraud: a rule with precision 0.8% and 35 labelled rows in a month expects 0.3 fraud there, and a month with none is what chance gives; without this condition the caveat fires on every low-precision rule.
- **What fires on the two reference rules.** The Rome rule: neither. March, at 50%, is 0.59 of the overall 85.1%, above the threshold, and the later period (87.8%) is above the earlier (83.9%). March is still shown as the weakest month with its counts. The brief's rule: neither; only August and September have 30 labelled flagged rows, with no fraud among them, and each expects 0.3. A rule that does fire both: "the card is American Express" catches 74 fraud in the earlier period and 3 in the later, none at all from July to September.
- **In the turn.** `backtest_rule` returns the evidence in a state field of its own, `evidence`, never saved, with its overall part still in `result`. `RuleCard` gains `base_fraud_rate`, `lift`, `months`, `weakest_month`, `earlier`, `later` and `stability`: counts and rates, no row of the data. Like the rest of the card they are not checkpointed and never sent to the model.
- **In the evaluation.** `score_rule_arithmetic` also compares every monthly row with a recomputation that shares no statement with the template (inner joins, the month taken with `strftime`, each month's totals from statements of their own), and checks that the reported months add up to the reported overall figures. Five cases give a reference rule's month table and the weakest month the card must name.

**Alternatives rejected:**

- **One train and test split presented as validation** (derive on the early months, report the score on the late ones as "held out"). Nothing confines how a rule is written to the early months, so the late months are not held out; calling them so would claim more than was measured. And on this data it cannot fail a sensible rule: the Rome rule scores 83.9% early and 87.8% late.
- **Cross-validation over random rows.** Random folds of one history share its patterns by construction; a rule that matches a place or a card passes every fold. It would add figures and no information about time.
- **Restricting what the analyst may look at** (hiding the later months from queries until a rule is fixed). It would make the later period a real test, at the cost of the tool's main use, which is exploring all the data; and the guard would have to bound dates in arbitrary SQL.
- **A boundary chosen per rule, or reporting the best and worst split.** A free boundary is a parameter that can be fitted.
- **A statistical test per month** in place of the two thresholds. The fraud a low-lift rule catches is spread unevenly over the months (the American Express rule catches 25 in May and none from July to September), so monthly counts are not independent draws at one rate, and a p-value would claim a precision the data does not support. Two stated thresholds can be read and argued with.
- **Computing the months in Python from fetched rows.** It would pull row-level data out of the database for a figure SQL can aggregate.

**Consequences:**

- **What it still cannot show.** A pattern that runs through the whole dataset passes any split by time. The Rome rule is steady in nine months of ten and that says nothing about whether the next month's fraud is in Rome. Only data the rule was not written from, or a shadow run in production, can test that. The card states it; nothing in the tool measures it.
- A rule turn now runs two statements. Measured on the real database, the by-month statement takes about 1.2 s whatever the rule flags (a selective threshold, the Rome rule, every transaction: it walks all 1,159,966 transactions each time), next to 0.04 to 0.9 s for the overall one. Each runs under the query timeout, so a rule turn can take up to twice that limit before it is stopped.
- The monthly `total_labelled` and `total_fraud` are counted through the join from `transactions`, the overall ones from `fraud_labels` alone (D-12). They agree while every label belongs to exactly one transaction; tests check on the real data that the monthly rows add up to the overall backtest in every field.
- The thresholds (half, 30 rows, 5 expected fraud) and the period boundary are judgements made on one synthetic dataset and are not tuned. `weak_month` fires readily on rules with little lift, because the few fraud they catch cluster in some months; that is true of those rules and is what the caveat is for, but it is not a test of significance.
- The boundary is a date of the shipped data. On another dataset `LATER_PERIOD_START` has to be set again; months before it are "earlier" whatever they are.
- A month is the text of `date` up to the month. A transaction with no date would be counted under an empty month; the shipped data has none.
- A rule that lists fraud transaction ids as literals still scores perfectly in every month. The month table does not catch it; reading the clause does.
- None of the evidence is traced or logged (D-27). The allow-list is unchanged: the `backtest_rule` step carries the time the two statements took and nothing else, and the turn carries the clause and the caveat codes, where `weak_month` and `period_drop` now appear. `RuleEvidence` keeps that time (`elapsed_seconds`) so the step does not report the overall statement alone. Tests search a traced rule turn, its `--trace` lines and the log for every figure of a rule card.

## D-29: The fraud scan: chosen by the model, computed by code

**Status:** Accepted. Builds the scan side of D-16. Refines D-23, D-24, D-25 and D-27.

**Context:** Every other action needs the analyst to arrive with a question. The brief asks how the tool can help an FSM "explore the data and uncover suspicious patterns", and the first question of an exploration is "where is the fraud?". D-16 gave that question a tool, `profile_fraud`, with no arguments, and D-23 routed it to "not available yet". Three facts about the data shape what a useful answer is. Fraud is rare: 1,360 of 777,339 labelled transactions. All of it is at merchant locations in one city, Rome, Italy, so anything more common there (chip transactions, some merchant categories, some hours, a PIN error) has a raised fraud rate without being a signal of its own. And among 9,417 segments, small ones with a handful of frauds have the most dramatic rates.

**Decision:**

- **The model chooses the scan; code computes it.** `nodes.run_scan` calls `explore.scan`, which writes every statement, runs it and ranks the result. The model's part ends with the tool call: it writes no SQL for the scan, is shown none of its result and states no figure. A scan turn ends as `answered` and costs one model request.
- **Nine fixed dimensions**, each one way of grouping the labelled transactions (inner join to `fraud_labels` with the join of D-10): transaction type; merchant state or country; merchant city read with its state (Rome is in Italy, GA and NY: [`DATA.md`](DATA.md)); merchant category, grouped by MCC code and shown with its description, because two codes share one description; hour of day, from the timestamp; error, with "No error" for none; amount band; card type; card brand. Amount bands, in cents, each from the previous edge up to and not including its own: negative (below 0), then 1,000, 5,000, 10,000, 50,000 and 100,000, then $1,000 and over. Negative amounts are a band of their own because their meaning is not documented.
- **One profile statement, not one per dimension.** The labelled transactions are joined once to locations, merchants, categories and cards and held as a materialised CTE; each dimension is one `SELECT … GROUP BY` over it, and the statement returns the segments that pass the support floor, one row per dimension counting those that do not, and the totals. Nine separate statements took about 10 seconds on the shipped database, because each repeated the same 777,339 index lookups; the single statement takes about 4. It also returns a few dozen rows however many cities there are, so the configured row cap (1,000 by default, against 9,146 city segments) never cuts it; a result cut at the cap raises an error. The other tables are left-joined, so a labelled transaction whose card or location is missing is counted in a segment with no name, not dropped.
- **Support floor: 100 labelled transactions and 10 frauds** (`MIN_LABELLED`, `MIN_FRAUD`). Below either, a rate is a few events and a few more or fewer would change it beyond recognition. The result says how many segments were below the floor and how many of those held any fraud (9,360 and 21 on the shipped data).
- **A listed segment has a fraud rate above the base rate.** This is added to the floor. Without it the second row on the shipped data is "No error": 97% of all labelled fraud at 0.98 times the base rate, which is a description of the data and not a place where fraud is concentrated. The result counts the segments that pass the floor at or under the base rate (18).
- **Ranked by the share of all labelled fraud a segment holds, then by lift**, then by the order of the dimensions and the segment's name, so the order is the same on every run. Share first because an FSM's rule has to catch fraud: a segment holding 3% of it cannot lead to a rule worth having, whatever its rate. By lift alone, the rows after Rome, Italy on the shipped data would be segments of a few hundred transactions.
- **Two segments that are the same transactions are listed once.** On the shipped data `state = 'Italy'` and `city = 'Rome', state = 'Italy'` are the same 1,598 labelled transactions. Listed segments of different dimensions with equal counts are candidates; a statement counting the transactions in both decides, since equal counts can be a coincidence. The more specific dimension is the one listed (city with state over state; otherwise the earlier dimension) and the other is named beside it.
- **The residual check.** After the top segment, the scan says what is left without it: the labelled transactions and frauds outside it, and which segments still pass the floor when the profile is run again without it. That is the answer to confounding that needs no model and no statistics: a segment that was listed only because it overlaps the top one disappears, and one that survives is a finding of its own. The totals outside the top segment are a subtraction. When fewer than 10 frauds are left no segment can pass the floor, so the profile is not run again (the case on the shipped data: 0 frauds left in 775,741 transactions); otherwise it is, and its totals must equal the subtraction or the scan fails. The notes then count the listed segments that do not survive: 37 of the other 37.
- **A period is a parameter; the default is all the data.** `Period(start, end)`, both days included, written as `date >= start AND date < the day after end`, because `date` holds timestamps ([`DATA.md`](DATA.md)). The turn always scans everything; `fsm-assistant scan --period` narrows it. Scanning only the earlier months, so that the later ones are unseen by a rule derived from the scan, was considered as the default and not adopted: nothing in the turn connects a scan's period to a rule's backtest, and a default that hides a third of the data from an exploratory question would surprise. An analyst can do it by hand: scan January to July, then read the rule's backtest on later dates.
- **Every statement passes the guard and runs through `db.run_query`** with the configured timeout, row cap and result budget. The guard accepts all of them. It refuses a statement with bind parameters, because it compiles without values; so a segment's keys are written into the residual and overlap statements as SQL literals with quotes doubled. A statement the guard refuses stops the scan with an error; there is no path around it.
- **What a turn shows.** A table of the first ten listed segments (dimension, segment, labelled, fraud, fraud rate, lift, share of fraud) through the ordinary table type, so every interface and the evaluation read it like any result. A one-line answer naming the top segment and its share. `notes`, a new field of `TurnResult` written by code: the scope and base rate, the floor and what it dropped, segments that are the same transactions, the residual, how many statements ran. `sql` is the profile statement, the one that produces the table; the two or three small statements after it (equal counts, a second profile) are printed by `fsm-assistant scan --sql` and are in `ScanResult.statements`. Three follow-up questions written by code that name the top segment: a rule for it, its fraud rate by month, its count with unlabelled transactions included.
- **Caveats.** `label_coverage`, always, with the wording a query that reads the labels gets. A new one, `single_segment`, when the top segment holds all the labelled fraud: "One segment holds all the labelled fraud. A rule on it will score well on this history, which says little about fraud elsewhere or later." It is stated as a fact about this result, which is all the scan can know.
- **Failures.** A timeout ends the turn as `failed` with kind `timeout`; any other fault of the scan as `internal`, with SQLite's words in `error.detail` only (D-23). Neither is repaired: the model wrote nothing. The temporary `not_available` error kind is removed, and with it the one case where a failure ended as `cannot_answer`.
- **Memory: a scan turn is remembered as the fact that a scan ran and the names of its dimensions, and nothing it found.** A segment's name is a value read from the database. "Rome, Italy" is two cells of `merchant_locations`, and a merchant category, a city or a state is as much data as a row is; that it comes out of a `GROUP BY` does not change where it came from. D-23's invariant is that nothing read from a row is saved with a conversation or placed in a model message, and the stricter reading is kept: the remembered turn holds the question, the kind `scan` and the dimension titles, which are constants of the code. The next request tells the model that the scan ran over those dimensions, that its result was shown to the user and not to the model, and that a reference to a segment the user does not name is to be asked about. The reserved state field `scan_seen` is removed: the remembered turn says the same thing.
  - The other option was to remember the top segment's condition as SQL text, so that "turn that into a rule" works in one step. The argument for it is that a segment that passed the floor describes at least 100 transactions and that the schema context already sends the values of columns with at most twelve distinct ones. It was not taken. The city, the state and the category description are not such columns, the floor is a property of this scan and not of the invariant, and an invariant with an exception for "values that are probably harmless" is no longer one that a test can state. The leak tests now search for segment names and figures as they search for row values.
  - What it costs the user: one step. After a scan, "turn that into a rule" gets a question back ("which segment?"). The scan prints the follow-up that avoids it, "Propose a rule for transactions with merchant city and state = Rome, Italy.", and when the user sends it the segment's name reaches the model as the user's own words, in the question, like any other value a user types. The terminal has no way to pick a suggestion by number, so the user retypes or pastes it.
- **Events, traces and logs: a scan appears as counts, codes and code-written text, and nothing it found** (D-27). A segment's name is a value read from the database, and so is every figure of the table, the headline, the notes and the suggested questions, which name the top segment. None of them is an event field. The `run_scan` step carries the number of segments listed (`scan_segments`), the number of statements run (`scan_statements`), their time and that the labels were read; those two counts are the only keys added to `tracing.ALLOWED`. The finished turn carries what any answered turn does: the tool, the caveat codes, the count of rows in the table, its column names, which are constants, and the SQL shown. That SQL is the profile statement, which code writes with no value of the data in it: its literals are the dimension names, the amount-band labels and "No error". The statements after it (the check that two segments are the same transactions, and the residual profile) name a segment as a SQL literal; they are in no event, and `--sql` prints them on the terminal only. A turn after a scan sends its request, with the dimension titles, as any model request is sent. The `scan` command logs that it finished with the same two counts and a time, or that it failed with the class of the error. A test runs a scan turn on a fixture whose segment names and counts occur nowhere else and searches every traced payload, `--trace` line and log line for them.
- **Caching.** A result is kept per database path and period for the life of the process: the database is read-only and the scan is deterministic. A failed scan is not kept.
- **The terminal command.** `fsm-assistant scan [--period START END] [--top N] [--sql]` runs the same function with no model and no key and prints the same table, notes and caveats.
- **Evaluation.** The scan tier has six cases: three wordings of the open question; the scan tool's own wording with a filter of the user's, which must be a query ("with no filter of the user's own" is the prompt's line); a scan followed by the scan's own suggested follow-up, which must give the known-good rule; and a scan followed by "Turn that into a rule.", which must be asked about. A scan case names the top row by two whole cells, the dimension and the segment. The tests run the real scan for each of them and score it; no model has been asked. In a replay, a case the recorded run skipped and that can now run is reported as having no recording, since it has none, where it used to stop the replay as stale.

**Alternatives rejected:**

- **Letting the model write the profiling SQL**, as a series of `run_sql` turns or one long query. It would have to write nine correct groupings with the label join, the city read with its state, the location key and a band expression, choose a floor, and then rank by something sensible, on every run, and the result would differ between runs. None of that needs a language model. It also needs the model to see the first result to decide the next step, which the design does not allow (D-15, D-16).
- **Ranking by lift.** It puts the smallest segments that pass the floor first and makes the list depend on where the floor is. It is the second key.
- **A statistical test per segment** (a chi-squared or a binomial test with a correction for 9,417 comparisons). With one city holding all the fraud at 486 times the base rate, a segment that merely overlaps it can be significant too (chip transactions hold 1,238 labelled frauds where the base rate predicts about 966), so a test does not tell a finding from a side effect. What separates a finding from a side effect here is the overlap, which the residual check measures directly and an FSM can read without a p-value. A test would earn its place on data where the signals are weak.
- **Listing both of two equal segments**, or always the broader one. Two rows for one set of transactions read as two findings; the broader name ("Italy") hides that it is one city.
- **A second, model-written step that interprets the table.** It would put the segments in a model message, and it would let the model state figures.
- **Running the residual profile always.** Uniform, and about 4 seconds more on the shipped data for a result known in advance to be empty.

**Consequences:**

- **Fixed dimensions only.** A pattern on a column that is not one of the nine is not found. Adding a dimension is one entry in `DIMENSIONS`: a name, a title and one or two SQL expressions.
- **Single-dimension segments, no interactions.** Each segment is one value of one dimension; city with state is the only pair. Fraud that needs two conditions together is seen only as a raised rate on each. The residual check removes one segment, the top one, once.
- **Share-first ranking favours broad segments.** A broad segment that contains a concentrated one holds at least as much fraud, and outranks it unless the concentrated one holds all of it. On the shipped data that does not matter, because Rome, Italy holds everything; on data where the main concentration held four fifths of the fraud, a broad segment that contains it, such as a transaction type, could be the first row, and the residual check would then be run on that. The lift column shows it; the ranking does not correct for it. This is the weakest part of the design and is listed under the known weaknesses.
- **A pattern planted in synthetic data is found trivially.** One city with every fraud is the easiest case there is, and it is the only case the shipped data offers. The floor, the equal segments and a residual with something in it are exercised on fixture databases built for them, not on real fraud.
- **Counts are of labelled transactions.** A segment's label coverage is not shown, and a segment that is mostly unlabelled looks smaller than it is.
- **About 4 seconds for the first scan in a process** on the shipped database, under the 15-second default timeout; a lower timeout setting would make the scan fail, with a message that says so. Later scans are served from the cache.
- **The text a follow-up to a scan is given is new prompt text** that only a turn after a scan receives. The system text, the tool descriptions and every request the recorded runs made are unchanged, so `PROMPT_VERSION` stays `turn-4` and the recordings stay valid. Whether the model follows that text has not been measured: the case written for it is marked tuned and has no recording.
- **`TurnResult` gained `notes`**, empty for every other kind of turn, and `suggestions` can now be code-written; an interface has to show both.
