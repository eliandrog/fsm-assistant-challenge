# FSM Assistant

A prototype assistant for Fraud Success Managers (FSMs): ask questions about transaction data in natural language, and turn the patterns found into fraud rules. A submission for the "Fraud Success Manager Assistant Challenge"; the original brief is preserved in [`docs/CHALLENGE.md`](docs/CHALLENGE.md).

> **Status: work in progress, scaffold only.** The project structure, tooling and a placeholder entry point exist. **No assistant functionality is implemented yet**: no natural-language → SQL, no rule proposal and no evaluation suite. Sections marked _To be completed_ will be filled in as the work is done.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 if needed). Tested on macOS.

```bash
git clone <repository-url>
cd fsm-assistant-challenge-python
uv sync
```

`uv sync` creates `.venv`, installs the `fsm_assistant` package in editable mode and installs the dev tools (pytest). There are no runtime dependencies yet.

**Data.** The SQLite database is in the repository at `data/data.db` (about 100 MB), with its schema in `data/schema.sql`. No download or import step is needed. Nothing reads it yet.

**Configuration.** None yet: the scaffold reads no environment variables and needs no API key.

## How to run

```bash
uv run fsm-assistant
```

Today this prints a placeholder line (`fsm-assistant: scaffold only, no functionality implemented yet.`). `uv run python -m fsm_assistant` does the same.

## How to reproduce

```bash
uv sync          # install
uv run pytest    # offline
```

The only tests are smoke tests: the package imports and the entry point runs.

## Solution

_To be completed._

## Architecture

_To be completed._

## Design choices and trade-offs

_To be completed._

## GenAI limitations and mitigations

_To be completed._

## Evaluation

_To be completed._

## Left out and next steps

_To be completed._

## Repository layout

```
README.md              This file
AGENTS.md, CLAUDE.md   Pointers to docs/AGENTS.md (working rules for coding agents)
docs/
  CHALLENGE.md         The original brief, verbatim
  AGENTS.md            Working rules for coding agents
data/                  data.db (SQLite) and schema.sql; read-only
src/fsm_assistant/     The package: a placeholder entry point and docstring-only modules
tests/                 pytest smoke tests
```
