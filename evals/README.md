# evals/

**Status: the evaluation suite is not built. Everything below the next paragraph is proposed; the only files here are the recordings of one small check.** One piece it will use is built elsewhere: recording and replay of model replies, in `src/fsm_assistant/llm.py`.

One thing does write here: `fsm-assistant check-model --yes-spend`, a paid run, records each of its eleven replies and a `summary.json` under `recordings/check-model/<model id>/`. It has been run once on each of two models, so `recordings/check-model/claude-opus-5-5/` and `recordings/check-model/claude-haiku-5-5/` each hold eleven recordings (`q01.turn1.call1.json` to `q11.turn1.call1.json`) and a summary. On both models 11 of 11 replies were exactly one valid tool call, which keeps tools as the default output mode. `uv run fsm-assistant check-model --replay` and `uv run fsm-assistant check-model --replay --model claude-haiku-5-5` re-print each run's table and decision from the files, with no key and no network, and stop with "stale recording" if the files are missing or no longer match the request the code would send. The recordings are committed, not git-ignored.

This check is not the evaluation suite. It asks eleven questions once each with a minimal system message, and counts whether the reply was one valid tool call, not whether the SQL was right. The figures are in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md#evidence).

This folder will hold the evaluation suite the brief asks for ("an initial attempt at an evaluation suite"). The design is in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md#evaluation). In short:

- **Cases**: about 25 FSM questions in one file. Answerable ones carry a reference result; others must end in a clarification or a refusal (a delete request, vague wording, a missing column, a prompt injection, a rule that reads the fraud labels). Rule cases are compared with a reference rule.
- **Runner**: one command that runs every case through the same entry point the interface uses and writes the results, with the model, settings and prompt version that produced them.
- **Scorers**: execution accuracy (the generated query's result equals the reference result), guardrail (nothing unsafe executed), clarification and refusal, rule arithmetic (reported metrics equal an independent recomputation), rule match, and the fraud scan.
- **Offline replay**: a live run records each model reply. Replaying feeds the recordings through the real guard, database and scorers with no model, no key and no network. It shows that the recorded output gives the reported results; it does not show how the live model behaves today.

Results will be reported as counts per tier, not as one accuracy percentage: the sample is small.

Unit tests for the code itself live in `tests/`, not here.
