# FSM Assistant

A prototype assistant for Fraud Success Managers (FSMs): ask questions about transaction data in natural language, and turn the patterns found into fraud rules. A submission for the "Fraud Success Manager Assistant Challenge"; the original brief is preserved in [`docs/CHALLENGE.md`](docs/CHALLENGE.md).

> **Status: work in progress.** **Built** and tested: a terminal assistant that takes a question in plain language, has Claude choose one of five actions, checks the SQL it wrote without running it, runs it on a read-only connection and shows the SQL next to the result; that shows a proposed rule only with its backtest against the fraud labels, overall and month by month; and that answers "where is fraud concentrated?" with a scan whose SQL is written by code, not by the model (`fsm-assistant scan` runs it with no model and no key). Under it: configuration, read-only execution, the schema context, the SQL guard, the rule backtest and its evidence by month, the model client with recording and replay, the prompt, the graph that runs one turn, and the presentation of a result with its caveats. **Measured once, on one model:** the evaluation suite is built (128 cases, scorers, a runner, recording and offline replay) and has two recorded runs, both on `claude-haiku-5-5`, every case that existed then asked once (119, when the fraud scan was not built). On the current prompt 116 of 117 cases passed, 1 failed (a marked known limitation) and the 2 scan cases were skipped ([Evaluation](#evaluation)). Eleven cases have not been asked of a model and have no recording: the five added since with the rule evidence by month, and the six of the scan tier, four of them added with the scan. That is a single run, made after the prompt and the suite had been adjusted to the first one (109 of 117), on cases of our own: one sample, not a rate, and not a statement of how well the model answers in general. `claude-opus-5-5` has not been run on the suite. Before the suite existed, the assistant was tried live on both models (`claude-haiku-5-5` and `claude-opus-5-5`) with seven questions asked once each through `ask`, and all fourteen answers were correct ([Evidence](docs/ARCHITECTURE.md#a-live-smoke-test-of-the-turn)). The graph and the prompt are otherwise tested with scripted model replies, and the only other live calls are one small paid check of tool calls (`check-model`, under [How to run](#how-to-run)). An application log and opt-in LangSmith tracing are built ([See what the assistant did](#see-what-the-assistant-did)). **Proposed**, with the design written down but no code: the web chat. Each section below says what is built and what is proposed; parts marked _To be completed_ are filled in as the work is done.

## Contents

- [What it is](#what-it-is)
- [Setup](#setup)
- [How to run](#how-to-run)
- [See what the assistant did](#see-what-the-assistant-did)
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
2. **Insight → rule.** A rule is a valid SQL `WHERE` clause over the `transactions` table. A proposed rule is only useful with evidence, so each one is backtested (measured against history) using `fraud_labels`, over the whole period and month by month, with its weakest month named.
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

`uv sync` creates `.venv`, installs the `fsm_assistant` package in editable mode with its four runtime dependencies (`pydantic` for typed data models, `langchain-anthropic` for the Claude client, `langgraph` for the graph that runs one turn, and `langsmith`, which those two already install, for opt-in tracing) and installs the dev tools (pytest, mypy).

**Data.** The SQLite database is in the repository at `data/data.db` (about 100 MB), with its schema in `data/schema.sql`. No download or import step is needed. The application treats it as read-only. The `data/` directory itself must be writable: the database is in WAL mode, and SQLite creates two git-ignored sidecar files next to it on the first read ([`docs/DATA.md`](docs/DATA.md#access)).

**Model and API key.** The assistant uses Claude through the Anthropic API ([D-14](docs/DECISIONS.md#d-14-model-provider-anthropic-claude-through-its-api), [D-20](docs/DECISIONS.md#d-20-model-settings-key-handling-and-hosted-tracing-off)).

- **The tests need no key and no network.** They never call a model, and every test runs with network connections refused.
- **A live call needs a key and costs money.** Put the key in `.env` in the project root, as `ANTHROPIC_API_KEY=...` (see Configuration below). `.env` is git-ignored; `.env.example` lists the names only. The key is never printed or logged, and a missing key is an error only when a live call is attempted.
- Three commands make live calls ([How to run](#how-to-run)): `fsm-assistant ask` and `fsm-assistant chat`, one or two requests per question, and `fsm-assistant check-model --yes-spend`. Each prints the model it is about to use. `ask` and `chat` stop with a message when the key is missing; `check-model` sends nothing without its flag.
- The evaluation suite replays offline from recorded model replies (`fsm-assistant eval --replay --model claude-haiku-5-5`), so checking reported results needs no key. One evaluation run is recorded, on `claude-haiku-5-5`; the recordings of `check-model`, for two models, replay with no key too.

**Configuration.** Optional: every setting has a default, so nothing needs to be set to run the tests. Settings are environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `FSM_DB_PATH` | `data/data.db` | SQLite database. A relative path is resolved against the project root, not the current directory |
| `FSM_ROW_CAP` | `1000` | Maximum rows a query returns; a longer result is cut and flagged as truncated |
| `FSM_QUERY_TIMEOUT_SECONDS` | `15` | A query still running after this long is interrupted |
| `FSM_MAX_RESULT_BYTES` | `5000000` | Largest result a query may return, counted in bytes of its values; a larger one is an error, not a cut result |
| `ANTHROPIC_API_KEY` | none | Anthropic API key. Needed only for a live model call |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude model id |
| `FSM_EFFORT` | `medium` | How much reasoning the model spends: `low`, `medium`, `high`, `xhigh` or `max` |
| `FSM_MAX_TOKENS` | `8000` | Most tokens the model may write in one reply |
| `FSM_MODEL_TIMEOUT_SECONDS` | `120` | How long to wait for one model reply |
| `FSM_MAX_MESSAGE_CHARACTERS` | `2000` | Longest message accepted from the user. A longer or empty one is refused before any model call |
| `FSM_TOOL_MODE` | `tools` | How the model states its action: `tools` (it calls one of five tools) or `structured` (one schema-constrained object) |
| `FSM_LOG_LEVEL` | `INFO` | Level of the application log: `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `FSM_LOG_DIR` | `logs` | Directory of the log file. A relative path is resolved against the project root |
| `FSM_LOG_FILE` | `on` | `off` writes no log file |
| `LANGSMITH_TRACING` | `false` | `true` sends a filtered trace of each turn to LangSmith, if `LANGSMITH_API_KEY` is also set ([See what the assistant did](#see-what-the-assistant-did)) |
| `LANGSMITH_API_KEY` | none | LangSmith API key. Needed only for hosted tracing |
| `LANGSMITH_ENDPOINT` | `https://api.smith.langchain.com` | LangSmith API address |
| `LANGSMITH_AGENT_ID`, `LANGSMITH_AGENT_ENVIRONMENT` | none | Where traces go: an agent and one of its environments (`local`, `development`, `staging`, `production`). Set both or neither |
| `LANGSMITH_PROJECT` | `fsm-assistant` | Where traces go when no agent is named. Not together with an agent |
| `FSM_TRACE_SYSTEM_PROMPT` | `hash` | What a trace carries of the system prompt: `hash` (its version and SHA-256) or `full` (its text, with the schema context) |

To keep them in a file, copy `.env.example` to `.env` (git-ignored) in the project root:

```bash
cp .env.example .env
```

`.env` is read by a small parser in `config.py`, not by a dotenv library: plain `KEY=VALUE` lines and `#` comment lines only (no `export`, no inline comments). A real environment variable overrides the file, and an empty value means "use the default". An invalid value (for example `FSM_ROW_CAP=abc`) stops with an error naming the variable.

## How to run

```bash
uv run fsm-assistant                                          # help; free
uv run fsm-assistant ask "How many transactions are there?"   # PAID: one question, one or two requests to Claude
uv run fsm-assistant ask --trace "What is the overall fraud rate?"   # PAID: also prints each step
uv run fsm-assistant chat                                     # PAID: a conversation; 'exit' or Ctrl-D to leave
uv run fsm-assistant graph                                    # the turn graph as a Mermaid diagram; free, no key
uv run fsm-assistant scan                                     # where labelled fraud is concentrated; free, no model, no key
```

`uv run python -m fsm_assistant` is the same program.

**`ask` and `chat` call the Anthropic API and are billed to your key.** They need `ANTHROPIC_API_KEY` ([Setup](#setup)) and stop with "Cannot start: ANTHROPIC_API_KEY is not set" when it is missing. A question costs one request, or two when the first reply has to be repaired: about 4,450 input tokens each (the schema, the rules and the tool definitions, sent every time; measured in the live smoke test, on both models, on the prompt as it then was; the two lines added since are an estimated 130 tokens more) and 87 to 471 output tokens in that test. They print the model in use before anything is sent.

- `ask "<question>"` answers one question in a new conversation. It prints the answer, the SQL that ran, the result table (the first 20 rows, with "Showing 20 of N rows"), the model's explanation and assumptions, labelled as the model's, any caveats, and a line with the outcome, the tool the model chose, the codes of any reply that was refused on the way, the model, the number of requests, tokens and time. For a rule it prints the clause, its backtest, and the same backtest month by month. A table wider than 160 characters shows the columns that fit and names the rest. Exit code: 0 for an answer, a rule or a question back to you; 1 when the turn failed; 2 when it could not start (a bad argument or setting, no key, an unusable `--record-dir`); 3 when the assistant could not answer (it declined); 4 when the guard rejected the SQL; 130 after Ctrl-C.
- `chat` does the same in a loop and keeps one conversation, so a follow-up ("and for October?") is told the previous question and the SQL that answered it. A question that failed or was refused is not kept, so a follow-up after an error still builds on the last one that worked. Ctrl-C while a question is being answered abandons that question and returns to the prompt. The process keeps only the state of the latest turn, and the conversation is forgotten when the command ends.
- Both print, before the first question, whether hosted tracing is on and where traces go (never the key). `--verbose` also prints the application log's lines to stderr.
- `--trace`, on either, prints one line per step as the turn runs: the tool the model called, what the guard decided and why, how many rows came back, the outcome. It shows rows as a count only and never the key, and escapes control characters in anything the model wrote.
- `--record-dir DIR` also writes each reply of a live run to `DIR` (created if missing; a path that is a file or cannot be written to stops the command before anything is sent); `--replay-dir DIR` answers from those files instead of calling Claude, with no key, and stops with "stale recording" if the question, the prompt or the settings differ from what was recorded. `--case NAME` names the files.
- `graph` prints the diagram shown in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#how-one-turn-works), drawn from the compiled graph. It reads no setting and no key.
- `scan [--period START END] [--top N] [--sql]` runs the fraud-concentration scan directly: no model, no key, nothing sent. It prints the ranked segments, the notes (scope, support floor, segments that are the same transactions, what is left without the top segment) and the caveats; the same table a turn shows when the model chooses `profile_fraud`. `--period` takes two days as `YYYY-MM-DD`, both included (default: all the data); `--top` sets how many segments are printed (default 10); `--sql` also prints every statement that ran. It takes about 4 seconds on the shipped database. Exit code: 0, 1 when the scan failed, 2 for a bad argument or setting.

What the output looks like. This is real output from an earlier live run of this question on `claude-haiku-5-5`, made with the first version of the turn prompt, trimmed of the lines printed before the answer and of the last line (outcome, model, requests, tokens and time). The figures come from the executed backtest and are the same on every run; the explanation and the assumptions are the model's text and can vary between runs. The same question on the current prompt (`turn-3`) gave the same clause and the same figures on both models, with slightly different wording of the assumptions.

```text
$ uv run fsm-assistant ask "Propose a rule for debit card transactions over \$1,000"
The rule flags 350 of 1,159,966 transactions. Of the 238 with a fraud label, 2 are fraud and 236 are legitimate.

Rule (a WHERE clause over transactions):
    amount_usd_cents > 100000 AND card_id IN (SELECT id FROM cards WHERE card_type = 'Debit')

Backtest against the fraud labels (labelled transactions only):
  Flagged               350 of 1,159,966 transactions (0.030%)
  Labelled              238 (112 flagged have no label)
  Fraud caught          2 of 1,360 labelled fraud
  False positives       236
  Precision             0.00840336 (0.840%)
  Recall                0.00147059 (0.147%)
  Amount flagged        $458,620.11
  Fraud amount caught   $2,355.42
  False positive amount $307,838.01

Model's explanation (not checked): Flags transactions over $1,000 made on debit cards.

Model's assumptions:
  - 'Debit card' means card_type = 'Debit' only, excluding 'Debit (Prepaid)'.
  - 'Over $1,000' means strictly greater than $1,000 (amount_usd_cents > 100000).
  - Negative amounts are not treated specially; they fail the threshold and are not flagged.

Caveats:
  - [label_coverage] 238 of the 350 flagged transactions have a fraud label. Precision and recall use those only; the other 112 are unknown, not legitimate.
```

Since that run the rule card has gained a `Lift` line after `Recall` and a month-by-month section after the amounts ([D-28](docs/DECISIONS.md#d-28-a-rule-is-shown-with-its-backtest-month-by-month-as-a-stability-check)). For this clause they read as below. This part is printed by a turn with a scripted model reply, with no model call; its figures come from executed SQL like the others.

```text
  Lift                  4.8 times the base fraud rate (0.175% of labelled transactions are fraud)

Month by month, on the same history:
  Month    Flagged  Labelled  Fraud caught  False positives  Precision
  2019-01       34        22             0               22      0.00%
  2019-02       22        15             0               15      0.00%
  2019-03       33        22             0               22      0.00%
  2019-04       36        27             0               27      0.00%
  2019-05       36        20             0               20      0.00%
  2019-06       29        17             0               17      0.00%
  2019-07       33        23             1               22      4.35%
  2019-08       50        33             0               33      0.00%
  2019-09       43        35             0               35      0.00%
  2019-10       34        24             1               23      4.17%
  Weakest month         2019-09: 0 of 35 labelled flagged were fraud (0.00%)
  Monthly precision     0.00% to 0.00% over the 2 months with 30 or more labelled flagged, of 10
  Earlier period        2019-01 to 2019-07: 1 of 146 labelled flagged were fraud (0.68%); recall 1 of 928; lift 4.0
  Later period          2019-08 to 2019-10: 1 of 92 labelled flagged were fraud (1.09%); recall 1 of 432; lift 5.9
  The rule is scored here on the same history it was written from, so this is a stability check, not
  a test on unseen data. Only new data or a shadow run can show that the rule holds.
```

What a turn can end in: an answer, a rule with its backtest, a question back to you ("risky" has no definition in the data, so the assistant asks what you mean), a refusal (data the tables do not hold, or a request to change data), or a failure with its reason. Asking where fraud concentrates in general, with no filter of your own, gets the fraud scan: a table written by code, described under [Solution overview](#solution-overview).

The scan on the shipped database (`uv run fsm-assistant scan`, real output):

```text
Labelled fraud is most concentrated in merchant city and state = Rome, Italy: 1,360 of 1,360 labelled frauds (100.0%), at 486.4 times the base rate.

dimension                segment                 labelled  fraud           fraud_rate      lift      share_of_fraud
-----------------------  ----------------------  --------  -----  -------------------  --------  ------------------
merchant city and state  Rome, Italy                1,598  1,360   0.851064 (85.106%)  486.4449        1 (100.000%)
transaction type         Chip Transaction         551,905  1,238  0.00224314 (0.224%)    1.2821  0.910294 (91.029%)
card type                Credit                   237,316    562  0.00236815 (0.237%)    1.3536  0.413235 (41.324%)
card brand               Visa                     289,551    557  0.00192367 (0.192%)    1.0995  0.409559 (40.956%)
amount band              $0 to under $10          171,594    327  0.00190566 (0.191%)    1.0892  0.240441 (24.044%)
amount band              $100 to under $500        87,235    305   0.0034963 (0.350%)    1.9984  0.224265 (22.426%)
merchant category (MCC)  5311 Department Stores    27,729    290   0.0104584 (1.046%)    5.9777  0.213235 (21.324%)
hour of day              11                        55,489    156  0.00281137 (0.281%)    1.6069  0.114706 (11.471%)
hour of day              14                        51,656    153   0.0029619 (0.296%)    1.6929    0.1125 (11.250%)
hour of day              13                        52,062    147  0.00282356 (0.282%)    1.6139  0.108088 (10.809%)

Notes:
  - Scanned 777,339 labelled transactions (all dates): 1,360 labelled frauds, a base rate of 0.175%.
  - A segment is listed when it has at least 100 labelled transactions and 10 frauds, and a fraud rate above the base rate. Of 9,417 segments in 9 dimensions, 9,360 are below that floor (21 of them with at least one fraud) and 18 pass it at or under the base rate.
  - The table shows the first 10 of 38 listed segments.
  - merchant city and state = Rome, Italy holds exactly the same labelled transactions as merchant state or country = Italy. It is listed once.
  - Outside merchant city and state = Rome, Italy, 0 labelled frauds remain in 775,741 labelled transactions; no segment passes the floor.
  - 37 of the other 37 listed segment(s) do not pass the floor without the top segment: they overlap it and are not findings of their own.
  - 2 code-written statement(s) ran in 4.1 s; the SQL shown is the profile.

Caveats:
  - [label_coverage] This result uses fraud labels, which only about two thirds of transactions have. An unlabelled transaction is unknown, not legitimate.
  - [single_segment] One segment holds all the labelled fraud. A rule on it will score well on this history, which says little about fraud elsewhere or later.
```

Rows two to ten are side effects of the first: chip transactions, credit cards and department stores are where the Rome transactions happen to be, and the last notes say so. In a turn the same result comes with the profile statement and three follow-up questions written by code, the first of which is "Propose a rule for transactions with merchant city and state = Rome, Italy."

One more command measures the model and does not answer questions:

```bash
uv run fsm-assistant check-model                              # prints what it would send; sends nothing
uv run fsm-assistant check-model --yes-spend                  # PAID: 11 requests to the default model
uv run fsm-assistant check-model --yes-spend --model claude-haiku-5-5
uv run fsm-assistant check-model --replay                     # re-prints the recorded run; no key, sends nothing
uv run fsm-assistant check-model --replay --model claude-haiku-5-5
```

`check-model` measures one thing: how often a Claude model replies with exactly one valid tool call, which decides whether tools or a single structured object is the default output mode ([D-16](docs/DECISIONS.md#d-16-the-model-chooses-one-of-five-tools-code-executes-it)). It sends eleven fixed questions (five answerable, five that should not be answered with a query, one rule request), each once, with the real schema context, and prints a table: the tool called, whether the reply was one valid call, tokens and seconds, then the count and the decision (tools stay the default if at most one of the eleven misses). **It calls the Anthropic API and is billed to your key**: eleven requests of about 3,000 input tokens each, and at most 8,000 output tokens each. Without `--yes-spend` it makes no call and writes nothing. A paid run writes each reply to `evals/recordings/check-model/<model id>/` (`q01.turn1.call1.json` to `q11.turn1.call1.json`) with a `summary.json` of the table and the decision; these files are meant to be committed. `--replay` (with `--model` for a model other than the default) re-prints the table and the decision from those files with no key and no network, and stops with "stale recording" if a file is missing or the prompt, the schemas or the model settings have changed since.

The check has been run once on each of two models, and both sets of recordings are in the repository. `claude-opus-5-5`: 11 of 11 replies were exactly one valid tool call (36,111 input tokens, 2,195 output tokens, 36.7 s for the eleven requests). `claude-haiku-5-5`: 11 of 11 (36,111 input tokens, 2,459 output tokens, 19.0 s). Both chose the same tool for every question. So tools are the default output mode, and structured output stays as the fallback behind `FSM_TOOL_MODE`. This is a small check: eleven questions asked once each, with a three-sentence system message and not the prompt the assistant now uses, and it counts whether the reply was one valid tool call, not whether the SQL was right. The two `--replay` commands above re-print both tables offline; detail is under [Evidence](docs/ARCHITECTURE.md#evidence).

_More example questions with the live model's output: to be completed._

## See what the assistant did

Three things, each for a different question ([D-27](docs/DECISIONS.md#d-27-logs-for-errors-and-health-langsmith-for-traces-opt-in-and-built-from-an-allow-list); [Observability](docs/ARCHITECTURE.md#observability)).

**While a turn runs: `--trace`.** `ask --trace` and `chat --trace` print one line per step. Nothing is stored.

**Whether something went wrong: the application log.** `logs/app.log`, in the project root, git-ignored, always written unless `FSM_LOG_FILE=off`. Plain lines: a command started, each turn's outcome, and warnings and errors.

```
2026-01-01 12:00:00,000 INFO fsm_assistant.cli: ask started: model=claude-haiku-5-5 prompt=turn-4 tool_mode=tools replies=live tracing=off
2026-01-01 12:00:02,300 WARNING fsm_assistant.graph: turn needed a repair: conversation=3f2a… turn=1 refused=table_not_allowed
2026-01-01 12:00:02,300 INFO fsm_assistant.graph: turn finished: conversation=3f2a… turn=1 outcome=answered error=- requests=2 elapsed_ms=2291
```

(The lines above are an illustration of the format, with made-up times and ids.) A line never holds the question, the SQL, a result row, a segment the scan found or the text of an exception: an unexpected fault is logged as the exception's class and where it was raised. `FSM_LOG_LEVEL` sets the level, `FSM_LOG_DIR` the directory, and `--verbose` on a command also prints the lines to stderr. The file is rotated at 2 MB and three older files are kept.

**What one turn did, step by step: LangSmith, if you switch it on.** Off by default. To turn it on, put these in `.env`:

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_AGENT_ID=...            # with LANGSMITH_AGENT_ENVIRONMENT=local, as LangSmith's set-up page gives them
LANGSMITH_AGENT_ENVIRONMENT=local
```

or, in place of the two agent lines, `LANGSMITH_PROJECT=<name>` (with neither, traces go to a project named `fsm-assistant`). Outside the US region, also set `LANGSMITH_ENDPOINT`. `ask` and `chat` then print `Hosted tracing: on, to LangSmith …` before the first question, and each turn becomes one trace: the turn, a run per step in order, and the model call with its token usage. A conversation's turns are grouped as one thread. `eval --yes-spend` traces too, with each case's id as a tag.

| | Tracing off (default) | Tracing on |
|---|---|---|
| Leaves the machine | The request to Claude, and nothing else | The request to Claude, and one trace per turn to LangSmith |
| In a trace | | The question; the model's reply (its tool call, with the SQL or clause, explanation and assumptions it wrote); the accepted SQL or clause; the tool chosen, the guard's verdict and reason codes; outcome and error kind; row count and column names; caveat codes; model id, prompt version, token counts; durations |
| Never in a trace | | Result rows, cell values, backtest figures (overall or month by month: a rule turn sends its clause, its caveat codes and the time the backtest took), what the fraud scan found (segment names, the figures of its table, its headline, notes and suggested questions, and the statements that name a segment: a scan turn sends how many segments it listed, how many statements it ran, the time they took, its caveat codes and its profile statement, which code writes with no value of the data in it), SQLite's message for a query that failed while running, the answer text, the Anthropic key, the LangSmith key, details of this machine |
| The system prompt | | Its version and SHA-256. `FSM_TRACE_SYSTEM_PROMPT=full` sends its text, which includes the schema context: table row counts, the values of low-cardinality columns such as card type, and the date range of the data |

A trace is built from a fixed list of fields and not from the assistant's internal state, so a result row has no way into it. If LangSmith cannot be reached, the turn is answered as usual and a warning is printed once and logged. Anything replayed from recordings (`--replay-dir`, `eval --replay`) is never traced, and the tests cannot reach LangSmith. **Not yet checked:** no trace has been sent to LangSmith from this code, so how it is displayed there has not been seen.

## How to reproduce

What can be reproduced today:

```bash
uv sync          # install
uv run pytest    # a few seconds, offline (no model, no key)
uv run mypy      # type check: strict on src/ and tests/
```

The tests cover configuration, read-only execution, the schema context, the rule backtest, the SQL guard (each accepted query shape, and each rejection with its reason), the model layer (the request the Claude client would send, checked without sending it; how provider errors are classified; recording and replay; the shape check on a reply) and the turn: the prompt's required content, one test per invariant of the graph, one per outcome, the single repair for each kind of failure, conversation memory, each caveat and format hint, and the terminal commands. The turn is driven by scripted model replies, so these tests show what the code does with a given reply, not what Claude replies. Most use small databases built in a temp directory from `data/schema.sql`, where every backtest metric, overall and by month, is computed by hand; those in `tests/test_real_db.py` run against `data/data.db`, check the data facts the schema context states, compare the backtest with independently written queries and pin the month tables of two rules, and are skipped if the file is missing. The database is opened read-only and the suite checks it is unchanged afterwards.

To see the diagram of one turn, and the schema description that is given to the model (both free):

```bash
uv run fsm-assistant graph
```


```bash
uv run python -c "from fsm_assistant.config import load_settings; from fsm_assistant.schema_context import build_schema_context; print(build_schema_context(load_settings().db_path))"
```

To backtest a rule (here the brief's example, adapted to the real schema) without any model (`rules.proposal.rule_evidence` takes the same arguments and adds the monthly rows, the weakest month, the two periods and lift):

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

The evaluation suite, offline. Listing the cases needs nothing; the references are checked by the tests, which re-run all 73 reference queries and 19 reference rules on the shipped database, and run the scan for the scan cases:

```bash
uv run fsm-assistant eval --list            # the 128 cases and the count per tier
uv run pytest tests/test_eval_evidence.py   # every reference against its pinned result
uv run fsm-assistant eval --replay --model claude-haiku-5-5   # re-scores the recorded run through the real code; no key, no network
```

The recorded run behind the reported numbers was made on `claude-haiku-5-5` (effort `medium`, output mode `tools`, prompt `turn-4`); its replies and `run.json` are in `evals/recordings/runs/claude-haiku-5-5/` and its report in `evals/results/claude-haiku-5-5/report.md`. The replay sends those replies through the real graph, guard, database and scorers, reports "All 117 replayed case(s) scored exactly as recorded in run.json" and writes `evals/results/claude-haiku-5-5/replay.md`. Just before that line it names eleven cases as having no recording: the five added after that run with the rule evidence by month (the `evidence-…` cases of the `rules` tier) and the six of the scan tier, which the run skipped or did not have because the scan was not built. It does not run them, and because of them the command ends with exit code 3, not 0. Only a paid run can record them (for the scan tier, `eval --yes-spend --model claude-haiku-5-5 --tier scan`). It stops with "stale recording" if the prompt, a tool, the model settings or a question has changed since. No run is recorded for `claude-opus-5-5`, the default model, so the replay needs `--model claude-haiku-5-5`. The first run, on an earlier prompt (`turn-3`), is kept as a report and a `run.json` in `evals/results/history/turn-3-claude-haiku-5-5/` and cannot be replayed. A live run of the whole suite is now 136 turns (the recorded one was 123): 136 requests to Claude, plus one for each turn that needs a repair; the recorded run, of 123 turns, averaged about 4,640 input tokens a request and took under five minutes, run one after another.

## Solution overview

**Built for the terminal; run against Claude as a seven-question smoke test on two models and as two recorded runs of the evaluation suite on one of them.** Full description, diagrams and the status of every component: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

The principle: the model only chooses and writes; code decides what runs, runs it and computes every number. For each message, Claude is given the real schema and a short glossary and picks exactly one of five actions: answer with a SQL query, propose a rule, ask for a fixed fraud scan, ask the FSM what was meant, or decline. Code then checks the SQL without running it, accepts only a single read-only `SELECT` over the known tables, runs it on a read-only connection with a timeout and a row cap, and shows the SQL next to the result. A rule is a `WHERE` clause and is never shown without its backtest against the fraud labels, over the whole period and month by month. If the first attempt fails, the model gets the exact error once; a second failure is shown as a failure. The model is never shown a result row, so it has no number to restate.

```
question ─► Claude picks one of five actions ─► SQL guard ─► read-only execution ─► SQL + result + caveats
              ▲ real schema, glossary             │ reject or error: one repair attempt
              │                                   ▼
              └─ rule clause ─► (same guard) ─► backtest vs fraud_labels, overall and by month ─► rule + precision/recall + month table
```

One turn is a fixed graph of ten steps ([diagram](docs/ARCHITECTURE.md#how-one-turn-works), printed by `fsm-assistant graph`). Seven properties hold by construction and each has a test: only the exact string the guard accepted is executed; every execution goes through one function; a turn makes at most two model requests; every figure shown comes from executed SQL; a rule is never returned without its backtest; every failure ends in exactly one outcome and no exception reaches the interface; result rows are never saved with the conversation or put in a prompt.

**Exploration without knowing what to ask: the fraud scan.** The other actions need the analyst to have a question. For "where is fraud concentrated?" the model chooses `profile_fraud`, which takes no arguments, and code does the rest ([D-29](docs/DECISIONS.md#d-29-the-fraud-scan-chosen-by-the-model-computed-by-code)). One code-written statement groups the labelled transactions along nine fixed dimensions: transaction type, merchant state or country, merchant city with its state, merchant category code, hour of day, error, amount band, card type and card brand. A segment is listed when it has at least 100 labelled transactions and 10 frauds and a fraud rate above the base rate. Segments are ranked by the share of all labelled fraud they hold, then by lift, so that a segment with a handful of frauds and a dramatic rate does not lead. Two segments of different dimensions that are the same transactions are listed once, under the more specific one. Then the residual check: the top segment is taken out and what is left is profiled again, so that its side effects are not read as separate signals. On the shipped data the answer is Rome, Italy with all 1,360 labelled frauds, nothing left outside it, and a caveat saying that a rule on a segment holding all the fraud says little about fraud elsewhere. The model is told afterwards that a scan ran and which dimensions it covered, never what it found: a segment's name is a value read from the data. So "turn that into a rule" gets a question back, and the scan prints a follow-up that names the segment for the analyst to send.

What exists today:

- **Read-only execution** (`db.py`): every query runs through one function, on a connection that cannot write. Three layers enforced by SQLite itself, not by inspecting the SQL text: the file is opened read-only, `PRAGMA query_only` is on, and an authorizer callback denies everything except reads (including `ATTACH`, temp tables and `PRAGMA` changes, which a read-only file alone does not stop). A query is interrupted after a timeout, at most a fixed number of rows is fetched, with a flag when the result was cut. Size is bounded too, because a few rows of very large values could otherwise take hundreds of megabytes: no single value may exceed 100,000 bytes, a result has at most 100 columns, and a result over a byte budget (5 MB by default) is an error. A double-quoted name that does not exist is an error, not a string. The SQL is never rewritten. A second function, `inspect_query`, compiles a statement without running it and reports the tables, columns and functions it would use.
- **SQL guard** (`sql_guard.py`): `check_query` and `check_rule_clause` decide whether a model-written query or rule may run, from what SQLite reports when it compiles the statement, and return a decision with a reason that can be sent back to the model. A query must be one read-only `SELECT` over the database's own seven tables (not `sqlite_master`, `PRAGMA`, `EXPLAIN` or table-valued functions), and must not filter relative to today's date, because the data ends in 2019 and such a filter silently returns nothing. A rule must also not read `fraud_labels` or call `random()`. Before anything is compiled the text itself is checked, because compiling is not free: at most 10,000 characters, no Markdown code fence (refused with a request for the bare statement, never unwrapped), and a limit on how often common table expressions refer to each other, since a short statement built that way can take SQLite many seconds to compile. An ordinary check takes under a millisecond, and the worst input found that passes the text checks under 0.1 s. The rule backtest calls the guard, and so does every turn: nothing the model writes runs without passing it.
- **Schema context** (`schema_context.py`): the description of the database that goes into every prompt, read from the database itself (tables, columns, types, keys, row counts, the actual values of categorical columns) plus short notes on what a model would otherwise get wrong (amounts in cents, case-sensitive values, how to join the fraud labels, partial label coverage). It is deterministic, about 3,300 characters.
- **Rule backtest** (`rules/backtest.py`): `backtest(clause, db_path=…, timeout_seconds=…)` takes a rule as a `WHERE` clause over `transactions` and measures it with one fixed SQL template: transactions flagged, how many of those are labelled, fraud caught, false positives, precision, recall, flag rate, and the amounts involved in cents. Precision and recall use labelled transactions only, and the unlabelled part of the flagged set is reported next to them; a ratio with a zero denominator is "undefined", not 0. The clause is untrusted text: it is wrapped in parentheses inside a subquery that sees only `transactions`, and rejected with a reason if it contains a statement separator, a comment, a bind parameter or a parenthesis that would close the template's own. It applies the guard's rule policy itself before measuring, so a clause that reads the labels is refused with the guard's reason and cannot be scored by calling the backtest directly. `backtest_by_month` measures the same clause with a second fixed statement, one row per calendar month with that month's own totals, under the same containment and the same policy.
- **Rule evidence** (`rules/proposal.py`, [D-28](docs/DECISIONS.md#d-28-a-rule-is-shown-with-its-backtest-month-by-month-as-a-stability-check)): `rule_evidence(clause, …)` puts the overall backtest next to the monthly rows and works out the weakest month (the lowest precision among months with at least 30 labelled flagged transactions), an earlier period (2019-01 to 2019-07) and a later one (2019-08 to 2019-10) with each period's own recall, and lift (precision as a multiple of the base fraud rate). **It is a stability check, not a held-out test:** the rule is scored on the same history it was written from, and nothing limits what the analyst or the model looked at, so no month is unseen. It shows whether a rule's score is steady or carried by a few months. For the rule "location is in Rome, Italy" (precision 85.1% overall) it shows March at 142 fraud of 284 labelled flagged transactions, 50%, with the other months between 78% and 98%. It cannot show that a rule holds on new data.
- **Model layer** (`llm.py`, `nl2sql.py`): one small interface, `complete(request) -> reply`, with four implementations. The live one sends a single request to Claude with retries off, a token limit, an effort level and no temperature, and turns provider failures, refusals and cut-off replies into typed errors. A recording one writes each reply to a JSON file with a hash of the request; a replaying one reads those files with no key and no network, and stops with "stale recording" if the prompt, the schemas or the model settings have changed since. A scripted one serves the tests. The five actions are defined as typed tools with strict schemas, plus the same actions as one flat object for the fallback mode, and one function turns a raw reply into exactly one typed action or a reason it is not one.
- **Prompt** (`prompt.py`): one system text, the same for every question: when to choose each action, the schema context, and a glossary that fixes what a model otherwise gets wrong on this data. A fraud rate is `1.0 * SUM(is_fraud) / COUNT(*)` over labelled rows, returned as a fraction. Relative dates resolve against the last day of the data, so "last month" is October 2019, and the resolved range is listed as an assumption. A day range ends with `<` the next day. Locations join on the location key. Velocity uses a window function and compares whole seconds. "Debit card" means `'Debit'` only, stated as an assumption. The data's range is stated, and a period entirely outside it is declined, not answered with a count of zero. SQL the user insists on running unchanged that breaks one of these rules is declined, not rewritten silently. "Risky" and similar words get a question back, not a guessed threshold. A follow-up is given the previous question and the SQL that answered it, never its rows. The question and every other quoted piece of text sit inside named tags, which the prompt declares to be data and not instructions.
- **Turn graph** (`state.py`, `nodes.py`, `graph.py`): the ten steps and the routes between them, built with LangGraph. Code chooses every route. The one repair is bounded by a counter. Conversation memory holds the last turn's question and accepted SQL, in the process, keyed by a conversation id; one checkpoint is kept per conversation, so older turns are not retained. Nothing read from a row is saved or sent to the model, including a value SQLite quotes when a statement fails while running: the model and the memory get a fixed sentence, and only the user is shown SQLite's words.
- **Result and presentation** (`result.py`, `presentation.py`): every turn returns one typed result with exactly one outcome (answered, rule proposed, clarification, cannot answer, rejected, failed), the tool the model chose, and the reason codes of any reply the guard or the shape check refused during the turn, a repaired one included. Caveats are chosen by code from a fixed list: the result was cut at the row cap; the query read the fraud labels, which a third of transactions lack; no rows matched (check the spelling and case of values); a rule's precision rests on fewer than 30 labelled transactions; a rule flags more than 5% of all transactions; a month's precision is below half the rule's overall precision; the later period's precision is below half the earlier period's (the last two only on at least 30 labelled rows and five expected fraud). The rule card shows the overall backtest, lift, the month table, the weakest month and the two periods, with a fixed sentence saying it is a stability check on the same history. Cents are shown as dollars and a fraction also as a percentage.
- **Fraud scan** (`explore.py`): `scan(db_path=…, period=…)` returns the ranked segments, the totals and base rate, how many segments the floor dropped, the residual check and the statements that ran, as aggregates only. Every statement passes the SQL guard and runs through the same read-only function as a model-written query. About 4 seconds on the shipped database, cached per database and period for the life of the process.
- **Terminal** (`cli.py`): `ask`, `chat`, `graph`, `scan` and `--trace` ([How to run](#how-to-run)).
- **Evaluation suite** (`evals/`, `src/fsm_assistant/evals/`): 128 cases with reference results, scorers, a runner on the same entry point, and a recorded run that replays offline ([Evaluation](#evaluation)). Built; two recorded runs on `claude-haiku-5-5`, one of them on the current prompt, and none on `claude-opus-5-5`; the six scan-tier cases are not recorded.
- **Configuration** (`config.py`): see [Setup](#setup).

_A walk through one real exploration question and one real rule proposal, with the live model's output: to be completed._

## Design choices and trade-offs

Full entries, with the alternatives rejected, are in [`docs/DECISIONS.md`](docs/DECISIONS.md). Every row is about built code; the last rows are about the model layer and the turn, which are built and have had one seven-question live smoke test on two models, which is not an evaluation, and two recorded runs of the evaluation suite on `claude-haiku-5-5` ([Evaluation](#evaluation)).

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
| Model: Claude through the Anthropic API | Stronger SQL than the small local models tried first, and no multi-gigabyte download for a reviewer; offline replay of the evaluation means checking results needs no model | An API key and a cost per call for live use; the question and schema are sent to a hosted service |
| One turn is a fixed state graph in LangGraph, not an agent | See the paragraph below | Two dependencies; no multi-step reasoning within a turn |
| The model picks one of five tools; code executes it | Code can route on a typed action instead of parsing free text; the model never sees a result | The default model does not accept a forced tool choice, so a plain-text reply is possible and uses the one repair |
| One model interface with our own record and replay, keyed by case, turn and call, with a hash of the request | A reviewer can replay an evaluation with no key; a recording made for a different prompt or setting is refused, not silently reused | Any change to a prompt, tool description or model setting means recording again, which is a paid run |
| Client retries off, key and address passed explicitly | A turn's cost stays countable; no environment variable can redirect the key | A rate-limited request fails at once; a proxy would need its own setting |
| Hosted tracing off unless switched on, and then built from an allow-list of fields, never from the graph's state ([D-27](docs/DECISIONS.md#d-27-logs-for-errors-and-health-langsmith-for-traces-opt-in-and-built-from-an-allow-list)) | By default nothing leaves the machine except the model call. LangGraph's own tracing would send the state, result rows included; a list of what may be sent fails safe when a field is added | A trace shows counts and column names, not the rows; no trace has been sent to LangSmith yet, so how it displays there is unchecked |
| A plain application log for errors and health; no local copy of the trace | Two tools with two jobs: the log says whether turns fail and why, LangSmith shows one turn step by step | Without a LangSmith account there is no stored record of a turn's steps, only `--trace` while it runs |
| A fixed glossary in the prompt: fraud rate as a fraction, "debit card" as `'Debit'` only, relative dates against the last day of the data | Two models read "debit card" differently and both returned a rounded percentage when not told; one stated convention makes runs comparable, and each reading is listed as an assumption the FSM can overrule | A user who meant prepaid cards too has to say so; the prompt is longer |
| One repair: a fresh request with the previous answer and the reason it failed | Many first failures are a wrong column name, and SQLite's compile message is enough to fix them; a counter keeps a turn at two requests | A question that needs a third try fails. A statement that fails while running gets only a generic reason, because SQLite's run-time message can quote a value from a row |
| Memory is the last turn's question and accepted SQL, never rows; one checkpoint per conversation | A follow-up works; result rows cannot reach the model through a growing transcript; old questions and SQL do not pile up in the process | No reference further back than one turn; lost when the process ends |
| Caveats come from a fixed list chosen by code; the model's text is shown labelled as the model's | A caveat can be tested and cannot contradict the result | Fixed thresholds (30 labelled rows, 5% of transactions), chosen by judgement |
| Evaluation by result, not by SQL text: pinned reference results, rule match by flagged set, counts per tier with tuned and held-out apart, recorded runs replayed through the real code ([D-25](docs/DECISIONS.md#d-25-the-evaluation-suite-results-compared-with-pinned-references-recorded-runs-replayed-through-the-real-code)) | Two correct queries rarely share their text; a reviewer can re-score a run with no key; a small sample is not read as a rate | The expected answers are ours; replay says nothing about the live model today; a prompt change makes recordings stale |

**Why LangGraph, and why not an agent.** An agent is a loop in which the model sees the result of each action and chooses the next one. That is flexible, but it could not guarantee that every query passes the guard, or that a turn costs at most two model requests, and result rows would pass through the model, which could then restate them wrongly. So a turn is a fixed graph instead: the steps and the routes between them are declared in code, and the model's only job is to pick one action and write it. LangGraph was chosen for that graph, rather than plain functions, because the structure is declared in one place and the diagram is drawn from the code, conversation memory comes as a store keyed by conversation, and each step can be traced. The cost is that the assistant does not chain several queries in one turn; the FSM asks a follow-up. ([D-15](docs/DECISIONS.md#d-15-langgraph-as-a-fixed-state-graph-not-an-agent-loop))

The model name is configuration, and only `llm.py` talks to the model, so a second provider (for example a local model) could be added behind the same interface. It is not planned.

Still open (see [Open points](docs/ARCHITECTURE.md#open-points)): whether an effort level other than the default `medium` scores better, whether the smaller Claude model becomes the default (both models passed the tool-call check, and the smaller one took about half the time; that check does not judge the SQL), and whether the web chat is built. No longer open: tools are the default output mode, confirmed by `check-model` on both models, with the single structured object kept as the fallback.

_Justification of the final interaction model, with what was observed with the live model: to be completed._

## GenAI limitations and mitigations

Mitigations by risk, and whether each is built. Where each sits in the design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#where-hallucination-mitigations-sit).

"Built" here means the code exists and is tested with scripted model replies. How often the live model needs each mitigation is not measured yet.

| Risk | Built | Proposed |
|---|---|---|
| Invented tables, columns or values | Schema context from the real database, including the actual values of categorical columns; the guard checks tables against the real ones; SQLite's compile error on an unknown column is sent back to the model for one repair | Measured in the evaluation suite |
| Destructive or runaway SQL | Read-only connection, timeout, row cap, value-size cap, result-size budget; a guard that bounds the text, compiles the SQL without running it and accepts a single read-only `SELECT`; nothing in a turn runs without passing it | none |
| SQL that runs but answers the wrong question | SQL always shown with the result, with the model's assumptions listed; a glossary that fixes the fraud rate, relative dates, day ranges and the location join | Execution accuracy measured by the evaluation suite (built; one recorded run on the current prompt, on `claude-haiku-5-5`, each case asked once) |
| Made-up numbers | Every figure comes from executed SQL; the model is never shown result rows, and its text is labelled as model text | none |
| Plausible but poor rules | No rule shown without its backtest, overall and month by month, with lift over the base fraud rate; caveats for a small labelled sample, a high flag rate, a weak month and a drop in the later period; the FSM decides, the tool does not deploy | none |
| A rule that reads the labels it is scored on | The guard rejects any clause that reads `fraud_labels`, in the turn and again in the backtest. Not detectable: a clause that lists fraud transaction ids as literals | none |
| An empty result that looks like an answer (a filter relative to today, on data that ends in 2019; a value in the wrong case) | The guard rejects `'now'` given to a date function; the glossary resolves relative dates against the last day of the data; a zero-row result carries a caveat to check spelling and case | none |
| Misleading fraud rates from partial labels | The schema context and the glossary tell the model to use labelled rows only; rule metrics on labelled transactions only, with the unlabelled share reported; a coverage caveat on every result that read the labels | none |
| Vague or unanswerable questions answered anyway | The two actions exist and the prompt says when to use them: ask for "risky", "suspicious", "high value"; decline for missing data and for requests to change data | Such cases are in the evaluation set (two recorded runs on `claude-haiku-5-5`; one such case failed in the first and a prompt line was added for it, so its pass in the second is a tuned one) |
| A rule that fits history and nothing else | Partly: the month table, the weakest month and the earlier and later period show a score that is carried by a few months or that fades ([D-28](docs/DECISIONS.md#d-28-a-rule-is-shown-with-its-backtest-month-by-month-as-a-stability-check)). It is a stability check on the same history and the card says so. **Not addressed:** a pattern that runs through the whole dataset, as fraud in Rome does here, passes any split by time | A test on data the rule was not written from: new data, or a shadow run in production. Not possible with this dataset |
| Instructions hidden in the question | The model can only choose one of five typed actions; whatever it writes still passes the guard and a read-only connection; the question and every quoted earlier text sit inside named tags that cannot be closed from inside, and the prompt says text in them is not an instruction | Such cases in the evaluation set |

Known weaknesses found in exploratory checks, each with its planned fix, are listed in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#known-weaknesses-and-planned-fixes). One matters for reading any result on this data: all 1,360 labelled frauds are at merchant locations in Rome, Italy, so strong backtest numbers here, steady months included, do not prove that a rule generalises.

_What was observed in testing with the real model, and residual risks: to be completed._

## Evaluation

The brief asks how to show a stakeholder such as the Head of Fraud that the tool is effective and safe, with offline and online metrics, and for an initial evaluation suite. **The suite is built and has two recorded runs, both on `claude-haiku-5-5`, every case asked once. On the current prompt (`turn-4`), 116 of 117 cases passed, 1 failed (a marked known limitation) and 2 were skipped because the fraud scan was not built then. The scan is built now and its tier has six cases, none of them yet asked of a model. That is one run on one model: model output varies between runs, so it is a sample and not a rate. It is also not an independent test. The first run (109 of 117, on prompt `turn-3`) came before it, and two prompt lines and two of the suite's expectations were changed after reading its failures, so the difference between the two runs is the sum of those changes and not evidence that the model got better. `claude-opus-5-5`, the default model, has not been run on the suite; that is planned as the final recorded run.** The suite itself is tested with scripted model replies; the full description is in [`evals/README.md`](evals/README.md), the design in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#evaluation) and the reasons in [D-25](docs/DECISIONS.md#d-25-the-evaluation-suite-results-compared-with-pinned-references-recorded-runs-replayed-through-the-real-code).

**What the suite is.** 128 cases in fourteen tiers, run through the same entry point the terminal uses. The "effective" half asks questions with a known answer (aggregates, joins, fraud rates, dates, rankings, known traps, follow-ups, rules, paraphrases); the "safe" half sends vague words, questions the data cannot answer, writes, injections and rules that would read the labels. An answer is right when its rows equal the result of a reference query of ours, not when its SQL looks like ours. A rule is right when it flags the same transactions as a reference rule and its reported numbers, overall and in every month, equal an independent recomputation; five cases also name the month the rule card must show as the rule's weakest. Every turn is also scored on its outcome, on whether anything ran that should not have, and on whether the model itself wrote a write. A correct answer that carries extra columns after the expected ones passes and is counted apart. Results are counts per tier, with the 25 tuned cases (their question is in the prompt, or a prompt line was written because they failed or for them) shown apart from the 103 held out, and never one accuracy percentage: a tier has between 6 and 15 cases. The two recorded runs below were made when the suite had 119 cases; nine cases were added afterwards, five about the rule evidence by month and four with the scan, and those five and the six of the scan tier have not been run on a model.

**How to run it.**

```bash
uv run fsm-assistant eval --list                                  # the cases, with the count per tier; free
uv run fsm-assistant eval                                         # what a run would send and its upper bound; sends nothing
uv run fsm-assistant eval --yes-spend --model claude-haiku-5-5    # PAID: runs the suite and records every reply
uv run fsm-assistant eval --replay --model claude-haiku-5-5       # re-runs the recorded replies offline; free, no key
```

`--tier`, `--case`, `--tag`, `--held-out`, `--tuned` and `--limit` narrow any of them. A run prints a line per case with pass, fail and the reason code, then the table per tier (with "correct, with extra columns" and "failed, known limitation" as counts of their own), the tuned and held-out split, the failure reasons and the token and time totals, and writes `evals/results/<model id>/report.md` and `report.json`. Exit code 0 when every case that ran passed, 1 when one failed, 2 when the command could not start, 3 when a replay did not reproduce its record.

**Offline** means measured on fixed cases before anyone uses the tool. A live run records the model's replies under `evals/recordings/runs/<model id>/` with a `run.json` of every score. A replay then sends the same cases through the real graph, guard, database and scorers with the recorded replies in place of the model: no key, no network. It stops with "stale recording" if the prompt, the tools, the model settings or a question changed since, and says whether every case scored as recorded. Replay shows that the recorded output, scored by the real code, gives the reported results. It does not show how the live model behaves today. A filtered run replaces only the cases it ran and leaves the rest of the record in place.

**Online** means measured while FSMs use the tool, which would need a deployment. What exists for it is the trace: with LangSmith tracing switched on ([See what the assistant did](#see-what-the-assistant-did)), every turn is recorded there with its question, SQL, tool chosen, guard decision and reason code, outcome, error kind, row count, durations and token usage, tagged by model and prompt version and grouped by conversation; never result rows or a key. **No dashboard, saved filter or aggregation is built, and no trace has been sent yet**: the table names the trace fields each metric would be computed from in LangSmith, as a plan. The fields are those of the turn's root run unless the table says otherwise. The application log is for errors and health, not for metrics.

Each offline measure is there because it predicts something that matters online. The offline column is built, except where it says otherwise; the online columns are proposed.

| Offline measure | Online metric it predicts (proposed) | Where it would come from in LangSmith (not built) |
|---|---|---|
| Execution accuracy: the generated query returns the same result as a reference query, per tier | Share of generated queries the FSM uses as they are, against the share they correct or report as wrong | Traces with `outcome` `answered`, grouped by conversation (`session_id`): a follow-up that rewrites the query is the next turn of the thread. "Reported as wrong" needs feedback attached to a trace, which no interface collects yet |
| Guardrail: unsafe inputs (writes, injection) that reach execution, and writes the model wrote at all; target zero | Unsafe statements reaching the database (must stay zero); rate of guard rejections | Step runs named `guard_sql` and `guard_rule`: the count with `guard` `rejected`, by `reason_code`, over all of them. Turns with `outcome` `rejected`. Nothing in a trace can show a statement that ran without `guard` `accepted`; that is the offline suite's job |
| Clarification and refusal: vague or unanswerable questions are not answered with invented SQL | Share of clarifications followed by a successful answer; answers reported as wrong | Traces with `outcome` `clarification` and the `outcome` of the next turn in the same thread; turns with `tool` `decline` over all turns |
| Rule arithmetic: the reported backtest equals an independent recomputation, overall and month by month | Trust in the numbers: audits of shown figures find no differences | Not from traces: a trace holds no figure. An audit re-runs the `sql` or `where_clause` of a sampled trace against the database |
| Rule match against a reference rule | Share of proposed rules accepted | Traces with `outcome` `rule_proposed` give the denominator and the `where_clause`. Acceptance comes from the system a rule is deployed to, which does not exist here |
| Rule stability on history: precision month by month, the weakest month, the later period against the earlier (built; a stability check, not a test on unseen data) | Shadow-mode precision: the rule run in production without blocking, its weekly precision and false-positive rate against the backtest figure and the weakest month. This is the first test on data the rule was not written from | Not from traces: a trace holds no backtest figure, overall or by month. It gives the `where_clause` of a proposed rule and its caveat codes, so the share of proposed rules carrying `weak_month` or `period_drop` can be counted. Shadow-mode precision comes from the system a rule is deployed to, which does not exist here |
| Fraud scan finds the known concentration: checked by the tests on the scan itself; whether the model chooses the scan is six cases with no recorded run yet | Time from first question to a validated insight | The span of a thread: the first and last trace with one `session_id`. "Validated" needs feedback, as above |
| Model requests, tokens and time per turn, recorded with each run | Response time and cost per question | Latency percentiles of the root run; `model_calls`, `input_tokens` and `output_tokens`; the token usage of the model run, by the `model:` and `prompt:` tags. Share of turns with `model_calls` 2 (a repair), and of `outcome` `failed` by `error_kind` |

Further online metrics with no offline counterpart: time from insight to deployed rule against the current workflow; fraud value prevented against legitimate payments blocked.

**Results: the first recorded run.** `claude-haiku-5-5`, prompt `turn-3`, every case asked once: **109 of 117 cases passed, 8 failed, 2 skipped** (the scan was not built then). Read it by tier, not as one rate:

| Tier | Cases | Passed | Failed | Skipped |
|---|---|---|---|---|
| aggregates | 9 | 9 | 0 | 0 |
| joins | 9 | 8 | 1 | 0 |
| fraud | 13 | 12 | 1 | 0 |
| dates | 9 | 9 | 0 | 0 |
| top_n | 6 | 6 | 0 | 0 |
| traps | 12 | 11 | 1 | 0 |
| follow_ups | 7 | 6 | 1 | 0 |
| ambiguous | 7 | 7 | 0 | 0 |
| unanswerable | 7 | 6 | 1 | 0 |
| adversarial | 13 | 12 | 1 | 0 |
| rules | 10 | 10 | 0 | 0 |
| rule_policy | 7 | 6 | 1 | 0 |
| scan | 2 | 0 | 0 | 2 |
| paraphrases | 8 | 7 | 1 | 0 |

Of the 20 cases then marked tuned, 19 passed and 1 was skipped; of the 99 held out, 90 passed, 8 failed and 1 was skipped. The eight failures, none of them a wrong number and none a guardrail failure:

- Four were correct answers that carried extra columns (the counts behind a rate or an average).
- Three answered where the case expected a decline or a refusal: a count of 0 for 2018, a year the data does not cover; SQL relative to today that the user said to run unchanged, rewritten and answered; and a fixed 1% rule (`transactions.id % 100 = 0`) proposed for "a random 1%".
- One was a follow-up to a declined delete, which got a clarification, because a declined turn is not remembered.

**What was changed after that run. These changes were made after seeing its results**, which makes the suite easier to pass; the first run's report is kept unchanged in `evals/results/history/turn-3-claude-haiku-5-5/` for that reason.

- Two prompt lines (`turn-4`, [D-26](docs/DECISIONS.md#d-26-two-prompt-lines-added-after-the-first-recorded-run)): a period entirely outside the data's range is declined; SQL the user insists on running unchanged that breaks a rule is declined, not rewritten. The two cases they describe are now counted as tuned, not held out.
- Extra columns after correct expected ones pass, and are counted on their own line.
- The random-sample case accepts a refusal or a repeatable rule that flags about 1%.
- The follow-up after a decline is kept as a failing case and marked as a known limitation, which the report counts apart and never leaves out.

**Results on the current prompt (`turn-4`).** `claude-haiku-5-5`, every case asked once: **116 of 117 cases passed, 1 failed, 2 skipped** (the scan was not built then; the suite had 119 cases). Five of the passes are correct answers with extra columns, and the one failure is the marked known limitation. 123 turns, 125 model requests, 579,530 input and 42,582 output tokens, 277.7 seconds in turns. By tier:

| Tier | Cases | Passed | With extra columns | Failed | Known limitation | Skipped |
|---|---|---|---|---|---|---|
| aggregates | 9 | 9 | 0 | 0 | 0 | 0 |
| joins | 9 | 9 | 2 | 0 | 0 | 0 |
| fraud | 13 | 13 | 1 | 0 | 0 | 0 |
| dates | 9 | 9 | 0 | 0 | 0 | 0 |
| top_n | 6 | 6 | 0 | 0 | 0 | 0 |
| traps | 12 | 12 | 1 | 0 | 0 | 0 |
| follow_ups | 7 | 6 | 0 | 1 | 1 | 0 |
| ambiguous | 7 | 7 | 0 | 0 | 0 | 0 |
| unanswerable | 7 | 7 | 0 | 0 | 0 | 0 |
| adversarial | 13 | 13 | 0 | 0 | 0 | 0 |
| rules | 10 | 10 | 0 | 0 | 0 | 0 |
| rule_policy | 7 | 7 | 0 | 0 | 0 | 0 |
| scan | 2 | 0 | 0 | 0 | 0 | 2 |
| paraphrases | 8 | 8 | 1 | 0 | 0 | 0 |
| Total | 119 | 116 | 5 | 1 | 1 | 2 |

Of the 22 cases then tuned, 21 passed and 1 was skipped; of the 97 held out, 95 passed (5 of them with extra columns), 1 failed and 1 was skipped. The failure is `follow-after-decline`: after a declined delete, "just tell me how many there are" got a clarification, because a declined turn is not remembered. No guardrail score failed in either run.

**How to read it.**

- **One run, one model.** Model output varies between runs, so this is one sample and not a rate. `claude-opus-5-5` has not been run on the suite.
- **Not an independent test of the changes.** Seven more cases passed than in the first run, and all seven are accounted for by what was changed after it. The four extra-column cases returned extra columns again and the random-sample case got the same fixed 1% rule again; they pass because the scoring changed. (The first run's replies, re-scored once under the new rules before they were replaced, gave 114 of 117; that check cannot be repeated from the repository.) The two prompt-fix cases were declined this time, under prompt lines written for them, and are counted as tuned. So the step from 109 to 116 is the sum of those changes, not the model getting better.
- **What it shows.** On these cases, asked once: result rows that equal our references, backtests that equal their recomputation, nothing run that the guard rejects, and vague, unanswerable and adversarial requests ending in a clarification, a decline or a refusal.
- **What it does not show.** The cases and the expected answers are ours, and a reference can share a blind spot with its author. 22 of the 119 cases of that run resemble wording in the prompt or had a prompt line written for them (25 of 128 now). The scan tier has no recorded run: that the scan finds Rome, Italy is shown by tests of the code, and whether a model chooses the scan for an open question and a query for a filtered one is not measured. Every case was asked once, so a rare failure cannot be told from none. The database is a single synthetic dataset in which all labelled fraud sits in one place (Rome, Italy), so the rule cases say nothing about whether a rule would hold elsewhere.
- **What a reader can check with no key and no network.** `uv run fsm-assistant eval --replay --model claude-haiku-5-5` sends the recorded replies through the real graph, guard, database and scorers and reports whether every case scored as recorded; `uv run pytest` runs the tests, which re-run every reference against its pinned result. Neither shows what the model would reply if asked again.

Case by case, with reasons: [`evals/README.md`](evals/README.md#results).

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

Built: configuration, read-only execution, the schema context, the rule backtest and its evidence by month, the SQL guard, the model layer (the Claude client, the five tools, recording and replay of model replies), the prompt, the graph for one turn, the presentation of a result, the fraud-concentration scan, the terminal commands `ask`, `chat`, `graph` and `scan`, the evaluation suite with its `eval` command, and the application log with opt-in LangSmith tracing. Natural language → SQL and a rule shown with its backtest, overall and month by month, work end to end with scripted model replies. Against Claude, an earlier prompt (`turn-3`) has had one smoke test (seven questions, asked once each through `ask` on `claude-haiku-5-5` and `claude-opus-5-5`, all answered correctly; not an evaluation), and the evaluation suite has two recorded runs on `claude-haiku-5-5`: 109 of 117 cases passed on `turn-3`, and 116 of 117 on the current prompt (`turn-4`) after the prompt and the suite were adjusted to the first run ([Evaluation](#evaluation)).

Not built, in planned order:

1. Live runs of `ask` and `chat` beyond the seven-question smoke test, and what they show about the prompt.
2. A live check of hosted tracing: no trace has been sent to LangSmith yet. And any dashboard or aggregation of the online metrics.
3. A recorded run of the evaluation suite on `claude-opus-5-5`, planned as the final recorded run. Only `claude-haiku-5-5` has been run.
4. A recorded run of the six scan-tier cases and of the five cases about the rule evidence by month. Both features are built; whether a model chooses the scan, and what it does with a rule request that has a month table to be scored, have not been measured.
5. The web chat and its HTTP API. First to be cut if time runs out.

Not planned for this prototype: authentication, conversation memory that survives a restart, streaming of replies, charts, deploying a rule, a second model provider, a free agent loop, and proof that a rule generalises beyond this dataset.

_What was cut for time and how it would be built: to be completed at the end, as the brief requests._

## Repository layout

```
README.md              This file
AGENTS.md, CLAUDE.md   Pointers to docs/AGENTS.md (working rules for coding agents)
docs/
  CHALLENGE.md         The original brief, verbatim
  ARCHITECTURE.md      Design, how one turn works, what is built and what is proposed, evidence, known weaknesses
  DATA.md              Schema notes and differences from the brief
  DECISIONS.md         Decision log
  AGENTS.md            Working rules for coding agents
data/                  data.db (SQLite) and schema.sql; read-only
src/fsm_assistant/     The package. Built: config.py, db.py, schema_context.py,
                       sql_scan.py, sql_guard.py, rules/backtest.py,
                       rules/proposal.py (rule evidence by month), llm.py,
                       nl2sql.py (tools, shape check), prompt.py, state.py,
                       nodes.py, graph.py (one turn), result.py, presentation.py,
                       explore.py (the fraud scan),
                       model_check.py (the check behind check-model), cli.py,
                       events.py, applog.py (the log), tracing.py (LangSmith),
                       evals/ (cases, scorers, evidence, report, runner,
                       recorded runs, the eval command).
tests/                 pytest tests (fixture databases built from data/schema.sql)
logs/                  The application log, app.log; git-ignored, made on first use
evals/                 Evaluation suite data (see its README): 128 cases in
                       cases/, pinned reference results, the recorded
                       replies of check-model for two models, the recorded
                       evaluation run on claude-haiku-5-5 (recordings/runs/,
                       results/), and the report of the first evaluation
                       run in results/history/
```
