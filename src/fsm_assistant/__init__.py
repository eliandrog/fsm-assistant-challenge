"""FSM Assistant: natural-language data exploration and fraud-rule proposal.

Work in progress. Implemented: config, db (read-only execution),
schema_context, sql_scan, sql_guard, rules.backtest, llm (the model client)
and model_check (the tool-call check); partly: nl2sql (the five tools and
the shape check) and cli (one command, check-model). The other modules state
their intended responsibility in their docstring and contain no logic yet.
See docs/ARCHITECTURE.md.
"""
