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
- **No new dependencies without asking.** Three runtime dependencies are installed: `pydantic` (v2), for typed, immutable data models (D-19); `langchain-anthropic` (1.7), the Claude client; and `langgraph` (1.2), for the turn graph (D-14, D-15, D-20). The dev tools are `pytest` and `mypy`. Nothing else is added without asking: no `langchain` meta-package, no direct dependency on the `anthropic` SDK (it is installed because the wrapper needs it; the package does not import it), no `pydantic-settings`, no second provider. Only `llm.py` imports LangChain or LangSmith.
- **No paid model call without the user's go-ahead for that run.** Tests never call a model, and run with network connections refused. Four commands call Claude. `fsm-assistant check-model` and `fsm-assistant eval` send nothing without `--yes-spend`; do not pass that flag unless the user has said to for that run. `fsm-assistant ask` and `fsm-assistant chat` call Claude as soon as they are run, with no flag to confirm: do not run them unless the user has said to for that run, or with `--replay-dir`, which sends nothing. `fsm-assistant graph`, `check-model --replay`, `eval --list` and `eval --replay` are free.
- **Hosted tracing stays off.** `llm.py` forces LangSmith tracing off around every model call (D-20). Do not enable it; opt-in tracing is a later task.

## What this project is

A take-home submission for Checkout.com's "Fraud Success Manager Assistant Challenge" (full brief: `docs/CHALLENGE.md`). A Fraud Success Manager (FSM) explores transaction data, validates fraud hypotheses, and turns patterns into rules. The assistant should:

1. Translate natural-language questions into accurate SQL over the provided SQLite database (the brief's suggested MVP).
2. Help turn an insight into a **rule**: a valid SQL `WHERE` clause over the `transactions` table, backtested against `fraud_labels`.
3. Ship with an initial evaluation suite, and a README that justifies the design, covers GenAI limitations, and describes offline and online metrics.

Current state: a turn runs end to end in the terminal, and it has been tested with scripted model replies; against Claude, an earlier prompt (`turn-3`) has had one smoke test of `ask` (seven questions, run once each on `claude-haiku-5-5` and `claude-opus-5-5`, all answered correctly; `docs/ARCHITECTURE.md`, "Evidence"), which is not an evaluation. The evaluation suite has two recorded runs, both on `claude-haiku-5-5`: 109 of 117 cases passed on `turn-3`, and 116 of 117 on the current prompt (`turn-4`, D-26). `claude-opus-5-5` has not been run on the suite. The data layer, the SQL guard, the model layer, the prompt, the turn graph and the presentation are built. `config.py`, `db.py` (`run_query` to execute, `inspect_query` to compile without running), `schema_context.py`, `sql_scan.py` (the SQL text scanner), `sql_guard.py` (`check_query`, `check_rule_clause`) and `rules/backtest.py` are implemented and tested. `backtest` applies the guard's rule policy itself. The model layer: `llm.py` (the `IntentModel` interface with a live Claude client, a recording wrapper, a replaying one and a scripted one for tests), `nl2sql.py` (the five tool schemas, the typed actions, the fallback `Intent` object, `check_shape` and `PROMPT_VERSION`), `model_check.py` (the paid measurement of how often a model replies with exactly one valid tool call: its questions, run, summary, replay and text) and one command in `cli.py`, `check-model`, which runs it. It has been run once on each of `claude-opus-5-5` and `claude-haiku-5-5`: 11 of 11 on both, so `tools` is confirmed as the default output mode (D-16, D-21) and structured output stays as the fallback behind `FSM_TOOL_MODE`. It is a small check (eleven questions, a minimal system message, the shape of the reply and not the correctness of the SQL). The replies and a `summary.json` per model are in `evals/recordings/check-model/<model id>/`; `uv run fsm-assistant check-model --replay` and `uv run fsm-assistant check-model --replay --model claude-haiku-5-5` re-print the results offline with no key. The turn: `prompt.py` (the system text with its glossary, the previous-turn block and the repair paragraph, with every quoted piece of text inside a named tag; D-22), `state.py`, `nodes.py` and `graph.py` (the fixed LangGraph graph of D-15 as built: ten nodes, one repair bounded by a counter, an in-process memory keyed by conversation id that never holds rows or any text read from the database, keeps the last turn that was answered, proposed a rule or asked for clarification, and retains one checkpoint per conversation; D-23), `result.py` and `presentation.py` (the `TurnResult` with the tool chosen and the codes of refused replies, format hints, coded caveats and the terminal text; D-24), and three commands in `cli.py`: `ask`, `chat` and `graph`, with `--trace`. The evaluation suite (D-25): 119 cases in `evals/cases/*.toml` with pinned reference results in `evals/expected.json`, and the package `fsm_assistant.evals`: `cases.py` (format and loader), `scorers.py` (pure scorers with a closed set of reasons, and the note `extra_columns` on a passed result that carries further columns), `evidence.py` (what scoring reads from the database, and `score_turn`, which turns a database error while scoring into a `scoring_error` for that turn), `report.py` (counts per tier and split, with "correct, with extra columns" and "failed, known limitation" counted apart, Markdown and JSON), `runner.py` (selection, one `Assistant` and conversation per case, `TurnResult` to `Observation`), `recorded.py` (a recorded run under `evals/recordings/runs/<model id>/` with its `run.json`, the live run that writes it and merges a filtered run into it, and the replay that compares with it) and `command.py` (what the `eval` command does). It is tested with scripted replies. It was first run against Claude on `claude-haiku-5-5` with prompt `turn-3`: 109 of 117 cases passed, 8 failed, 2 skipped; the report and `run.json` of that run are kept in `evals/results/history/turn-3-claude-haiku-5-5/`. After that run, and because of its results, two prompt lines were added (D-26), the extra-columns rule was loosened, one case gained `or_rule` and one `known_limitation` (`evals/README.md`, "Results"). A second run was then recorded on the same model with the current prompt: 116 of 117 cases passed (5 of them correct with extra columns), 1 failed (`follow-after-decline`, the marked known limitation), 2 skipped; its replies and `run.json` are in `evals/recordings/runs/claude-haiku-5-5/`, its report in `evals/results/claude-haiku-5-5/`, and `eval --replay --model claude-haiku-5-5` reproduces every score offline. That run is one sample on one model, not a rate and not an independent test of the changes made before it: write it as counts, with that framing, wherever it is cited. No run exists for `claude-opus-5-5`; it is planned as the final recorded run. 22 cases are marked tuned and 97 held out, and the two scan cases are skipped. `Assistant(settings, model).run_turn(question, conversation_id)` is the single entry point and never raises; `graph.py` has no module-level `run_turn` and no module-level state. The `profile_fraud` action is routed to a "not available yet" outcome, because its scan is not built. The package and the tests are fully annotated and pass `mypy` in strict mode, and every structured value it passes between modules is a frozen pydantic model. Still proposed, with no code: the fraud-concentration scan (`explore.py`), rule evidence by month (`rules/proposal.py` is docstring-only), structured logs and opt-in tracing, and the web chat. The design they will follow is in `docs/ARCHITECTURE.md`. Two LangGraph behaviours the graph relies on are pinned by `tests/test_langgraph_behaviour.py`; the graph's own tests, one per invariant and one per outcome, are in `tests/test_graph.py`.

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
| `evals/` | Evaluation suite data (see `evals/README.md`): `cases/*.toml`, `expected.json` (pinned reference results), `recordings/check-model/` (the recorded replies of the `check-model` run on two models), `recordings/runs/<model id>/` and `results/<model id>/` (the latest recorded run of a model), and `results/history/` (the report and `run.json` of the first run, kept as a record). The code is in `src/fsm_assistant/evals/` |
| `.env.example` | Names of the environment variables |

New project docs go in `docs/`. `README.md` stays at the root because the brief requires it.

## Commands

```bash
uv sync                  # create .venv, install the package (editable) and dev tools
uv run fsm-assistant     # print the help
uv run fsm-assistant ask "How many transactions are there?"   # PAID: one turn, calls Claude
uv run fsm-assistant chat                    # PAID: a conversation; add --trace to see each step
uv run fsm-assistant graph                   # print the turn graph as Mermaid; free, no key
uv run fsm-assistant check-model --replay    # re-print the recorded check offline, no key
uv run fsm-assistant eval --list             # the evaluation cases and the count per tier; free
uv run fsm-assistant eval                    # what a run would send; sends nothing without --yes-spend
uv run fsm-assistant eval --replay --model claude-haiku-5-5   # re-score the recorded evaluation run offline, no key
uv run pytest            # run the tests
uv run mypy              # type-check src/ and tests/ (strict)
```

Python 3.13, managed by uv. The package uses a `src/` layout built with `uv_build`.

## Conventions

- One responsibility per module, matching `docs/ARCHITECTURE.md`. Only `db.py` opens the database (every query goes through `db.run_query`); only `llm.py` talks to a model provider. Everything else uses `llm.IntentModel` and its plain request and reply types.
- In the turn graph, only `call_model` calls the model, only `guard_sql` writes `approved_sql` and only `execute_sql` runs it (the same for a rule clause), and only `remember` writes conversation memory. Rows live in state fields marked `UntrackedValue` and are never put in a prompt. **No text read from the database goes into a `Failure`, a repair, a trace line or any tracked field.** SQLite's message for a statement that failed while it ran can quote a row's value: use a fixed sentence there and put the message in `failure_detail`, which reaches the user only. Its message for text that did not compile may be passed on. Text that code did not write goes into the prompt only through `prompt.tagged`. Anything model-written that is printed goes through `presentation.safe_text`. A field that belongs to one turn must be reset in `state.PER_TURN_RESET`, and a new type stored in tracked state must be added to `state.SAVED_TYPES`.
- A change to the prompt text (`prompt.py`) or to a tool description or schema (`nl2sql.py`) needs a new `nl2sql.PROMPT_VERSION`. A change to a tool description or schema also makes the committed `check-model` recordings stale: do not make one without the user. Either change, or a changed question in a case, makes a recorded evaluation run stale too; only a paid run renews it.
- Evaluation cases: a changed reference query needs `evals/expected.json` re-pinned (the snippet in `evals/README.md`; read the diff before keeping it), and a reference clause carries its four backtest counts in the case. `tuned = true` marks a case whose question, or one nearly identical, is in the prompt or in `model_check.CHECK_QUESTIONS`, or for which a prompt line was written because it failed in a run; when the prompt gains such an example or such a line, re-mark the case and update the list in `tests/test_eval_cases.py`. A change made to the prompt, a scorer or a case because of a run's results is recorded as such in `evals/README.md` ("Results"), and the report of the run that prompted it is kept under `evals/results/history/`. Results are reported as counts per tier with tuned and held-out apart, never as one percentage, and no figure is claimed without a recorded run.
- `cli.py` is a thin entry point: argument parsing, wiring settings to the module that does the work, printing and exit codes (for `ask`: 0 answered, rule or clarification; 1 failed; 2 could not start; 3 could not answer; 4 rejected; 130 interrupted). A command's logic lives in its own module (`check-model` in `model_check.py`, `eval` in `evals/command.py`, which hands its lines to a callback), which returns values and text and never prints; `cli` imports that module, never the other way round.
- The API key is `Settings.anthropic_api_key`, a `SecretStr`. Only `llm.py` reads its value. Never put it in a message, a log line, an exception or a recording.
- Safety is enforced in code, not only in the prompt. Model-written SQL reaches `db` only after `sql_guard` accepted it: `db.inspect_query` has no timeout, and the guard's text limits are what bound a compile. A rule is measured only through `backtest`, which applies the rule policy itself.
- Numbers shown to the user (counts, fraud rates, precision, recall) come from executed SQL, never from model text.
- **Typed code, pydantic data (D-19).** `uv run mypy` must pass before a change is done, as `uv run pytest` must.
  - Every function and method has complete parameter and return annotations, in `tests/` as in `src/`: tests, fixtures and helpers included. No implicit `Any`; an explicit `Any` needs a one-line comment saying why (SQL bind parameters are the one case today).
  - Structured data is a frozen pydantic model (`model_config = ConfigDict(frozen=True, extra="forbid")`): settings, results, decisions, tool arguments, graph state, the turn result, and later evaluation cases and API bodies. No dataclass, `NamedTuple` or `TypedDict` for these.
  - A closed set of values is a `Literal` or an enum, not a bare `str`.
  - No untyped `dict` passed between modules. A dict is fine inside one function, for example on its way into `model_validate`. The one exception is a graph node's return value, which LangGraph takes as a dict: build it with `state.update(...)`, which checks the keys.
  - Exceptions stay ordinary exception classes with typed attributes.
  - Private working state that is updated in place and never leaves its module (`sql_guard._OpenCall`) may be a plain class.
  - No `# type: ignore` without an error code and a reason on the same line. In a test it is for a line that is ill-typed on purpose (assigning to a frozen field); an optional value is narrowed with `assert x is not None`, not ignored.
- Tests run offline. No test calls a real model: code that depends on one is tested with `llm.ScriptedModel`, and the live client by replacing the one method that performs the HTTP call. Anything that calls a real model belongs behind `eval --yes-spend` or `check-model --yes-spend`.
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
