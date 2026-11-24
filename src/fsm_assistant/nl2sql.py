"""Natural language to SQL.

Responsibility: turn an FSM's question plus the schema context into one
candidate SQLite SELECT statement, or a clarifying question / refusal when
the question cannot be answered from the data. Output is untrusted until
it passes sql_guard.

Not implemented.
"""
