"""SQL validation and guardrails.

Responsibility: decide in code whether model-written SQL may run. Intended
checks: a single statement, SELECT only, only known tables and columns.
Also validates rule WHERE clauses. The row limit is not checked here: db
caps the rows it fetches, without rewriting the SQL (docs/DECISIONS.md, D-08). Rejections carry a reason
that can be fed back to the model for one repair attempt.

Not implemented.
"""
