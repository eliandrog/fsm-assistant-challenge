# evals/

**Status: empty. Everything below is proposed; nothing here runs yet.**

This folder will hold the evaluation suite the brief asks for ("an initial attempt at an evaluation suite"). The design is in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md#evaluation). In short:

- **Cases**: about 25 FSM questions in one file. Answerable ones carry a reference result; others must end in a clarification or a refusal (a delete request, vague wording, a missing column, a prompt injection, a rule that reads the fraud labels). Rule cases are compared with a reference rule.
- **Runner**: one command that runs every case through the same entry point the interface uses and writes the results, with the model, settings and prompt version that produced them.
- **Scorers**: execution accuracy (the generated query's result equals the reference result), guardrail (nothing unsafe executed), clarification and refusal, rule arithmetic (reported metrics equal an independent recomputation), rule match, and the fraud scan.
- **Offline replay**: a live run records each model reply. Replaying feeds the recordings through the real guard, database and scorers with no model, no key and no network. It shows that the recorded output gives the reported results; it does not show how the live model behaves today.

Results will be reported as counts per tier, not as one accuracy percentage: the sample is small.

Unit tests for the code itself live in `tests/`, not here.
