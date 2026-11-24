# evals/

Status: empty. Nothing here runs yet.

This folder will hold the evaluation suite the brief asks for ("an initial attempt at an evaluation suite"):

- **Cases**: FSM questions with a reference SQL query or expected result, plus cases that should be refused or clarified (unanswerable from the schema, write attempts, ambiguous wording).
- **Runner**: one command that runs every case through the same pipeline the interface uses and writes a pass/fail summary.
- **Scorers**: execution accuracy (the generated query's result matches the reference result), guardrail checks (unsafe SQL never executes), and rule checks (a proposed rule is a valid `WHERE` clause and its reported metrics match a recomputation).

The case format and dataset design are an open decision: see [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md#open-decisions). Unit tests for the code itself live in `tests/`, not here.
