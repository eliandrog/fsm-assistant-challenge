# Decision log

One entry per significant decision: the problem, what was chosen, what was rejected, what it costs.

- Entries are numbered **D-01, D-02, …** in the order they were decided. The number is the only ordering; entries carry no dates.
- Entries are never rewritten. If a later decision changes an earlier one, the earlier entry's status becomes **Superseded by D-xx** or **Refined by D-xx**.
- Decisions not yet made are listed under "Open decisions" in [`ARCHITECTURE.md`](ARCHITECTURE.md#open-decisions). When one is settled it gets an entry here.

## Index

| D-NN | Title | Status |
|---|---|---|
| D-01 | Python 3.13, uv, `src/` layout | Accepted |
| D-02 | Core functionality first, minimal interface | Accepted |
| D-03 | Model-written SQL runs read-only, after validation in code | Accepted; refined by D-06, D-07, D-08 |
| D-04 | The real schema is the source of truth, not the brief | Accepted |
| D-05 | LLM provider: local Ollama with Qwen | Accepted |
| D-06 | Read-only is enforced in three engine-level layers | Accepted |
| D-07 | Query timeout through a progress handler | Accepted |
| D-08 | `db` owns the row cap | Accepted |
| D-09 | `.env` is read by a small built-in parser | Accepted |
| D-10 | The schema context prescribes one form of the label join | Accepted |
| D-11 | Backtest metrics use labelled transactions only; undefined ratios are `None` | Accepted |
| D-12 | One backtest template; the clause sees only `transactions` | Accepted |
| D-13 | A rule clause is contained by parentheses and a lexical scan | Accepted |

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

**Status:** Accepted; refined by D-06 (how read-only is enforced), D-07 (how the timeout works) and D-08 (the row limit belongs to `db`, not the validator)

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

**Status:** Accepted

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

**Status:** Accepted

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

**Status:** Accepted

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

**Status:** Accepted

**Context:** The brief defines a rule as a `WHERE` clause that filters `transactions`, with an example that uses bare column names and a subquery on `cards`. The backtest has to join `fraud_labels` too, and a clause placed in a query where both tables are in scope could name a column ambiguously or use `is_fraud` as if it were a transaction column. The label join must also be the cast form of D-10.

**Decision:** One fixed statement. A CTE selects the flagged rows from `transactions` alone, unaliased: `WITH flagged AS (SELECT id, amount_usd_cents FROM transactions WHERE (<clause>))`. The outer query left-joins `fraud_labels f ON f.transaction_id = CAST(flagged.id AS TEXT)` and aggregates every count and amount in one row; the three dataset totals are scalar subqueries in the same statement. A clause therefore uses bare column names or `transactions.<column>`; there is no alias to learn. The result is built from the statement's column names, not their positions.

**Alternatives:** A single scan of `transactions` joined to the labels with the clause inside `CASE` expressions (labels in the clause's scope, and an index lookup for all 1.16M rows on every backtest); aliasing the table as `t` (the brief's own example would need rewriting); separate queries for totals and flagged counts (more round trips for no gain); taking the totals from constants or a cache (wrong on any other database, and they cost about 0.04 s).

**Consequences:** Measured on the real database: 0.04 to 0.2 s for selective rules, about 0.9 s when every transaction is flagged. The totals count rows of `fraud_labels` without joining them to `transactions`, which is right only while every label belongs to exactly one transaction; an integration test checks that. A subquery inside a clause can still read any table, including `fraud_labels`; restricting that is left to `sql_guard`.

## D-13: A rule clause is contained by parentheses and a lexical scan

**Status:** Accepted

**Context:** The clause will be model-written text placed inside the backtest template. Writes are already impossible (D-06), but a clause could still change what is measured: `1=0) UNION SELECT id, amount_usd_cents FROM transactions WHERE (1=1` closes the template's parenthesis and replaces the flagged set, a trailing `--` or `/*` comment hides the rest of the template, and `1=1 LIMIT 10` adds to the query. The validator that will check tables and columns (`sql_guard`) is not built, its method is an open decision, and no SQL parser is available without a new dependency.

**Decision:** Three steps in `rules/backtest.py`, none needing a parser:

1. `check_clause` scans the text once, skipping quoted text the way SQLite's tokenizer pairs it (`'…'`, `"…"`, `` `…` `` with doubled quotes as escapes, and `[…]`). Outside quotes it rejects `;`, `--`, `/*`, the bind-parameter characters `? : @ $ #`, NUL, a `)` that has no earlier `(`, and a `(` left open; also an unterminated quote and an empty clause. Each rejection raises `RuleClauseError` with a reason, before any query runs.
2. The clause is compiled, not run, as `EXPLAIN SELECT 1 FROM transactions WHERE <clause>`, so text that is only valid inside parentheses (a bare `SELECT 1`, which would become a scalar subquery) is rejected.
3. The template places the clause as `WHERE (` newline clause newline `)`. With balanced parentheses and no comments, anything that is not one expression (`LIMIT`, `GROUP BY`, `UNION`) is a syntax error there.

SQLite's message is kept verbatim on the error for the repair attempt.

**Alternatives:** Relying on SQLite's syntax errors alone (the `UNION` escape above is valid SQL); a keyword deny-list (refuses legitimate rules, and is not what stops an escape); a SQL parser library (a dependency, and part of the open `sql_guard` decision); stripping comments instead of rejecting them (rewrites the rule the FSM is shown).

**Consequences:** `--` is refused even where it is arithmetic (`1--1`; write `1 - -1`), and a column name containing `$` or `#` cannot be used unquoted; neither occurs in this schema. Bind parameters were refused partly because SQLite's `$name(…)` form can hide parentheses from the scan. The scan guarantees structure, not meaning. Left for `sql_guard`, which is expected to run before `backtest` and can call or replace `check_clause`: which tables and columns a clause may read (today a clause can read `fraud_labels` in a subquery and reproduce the labels), non-deterministic functions such as `random()`, and expensive clauses, which only the timeout bounds.
