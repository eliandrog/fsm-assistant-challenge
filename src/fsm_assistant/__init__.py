"""FSM Assistant: natural-language data exploration and fraud-rule proposal.

Work in progress. Implemented: config, db (read-only execution),
schema_context, sql_scan, sql_guard, rules.backtest, llm (the model client),
nl2sql (the five tools and the shape check), prompt, the turn graph (state,
nodes, graph), result and presentation, model_check (the tool-call check)
and cli (ask, chat, graph, check-model). rules.proposal states its intended
responsibility in its docstring and contains no logic yet; the fraud scan
and the evaluation suite are not built. See docs/ARCHITECTURE.md.
"""
