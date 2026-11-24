"""The evaluation suite's code: cases, scorers, the runner, recorded runs and the command.

The case files, the pinned reference results, the recordings and the reports
are data and live in the repository's `evals/` folder. The code is here,
inside the package, so it is type-checked and tested with everything else
and `cli.py` can import it.

- `cases`: the case format and its loader.
- `scorers`: pure functions over plain values.
- `evidence`: what scoring needs from the database (reference results, a
  rule's flagged set, an independent recomputation of a backtest, the guard's
  verdict on what ran), and `score_turn`.
- `report`: aggregation and rendering.
- `runner`: the selected cases through the turn graph, then the scorers.
- `recorded`: a recorded run, the live run that writes it and its replay.
- `command`: what `fsm-assistant eval` does.
"""
