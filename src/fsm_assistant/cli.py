"""Terminal entry point.

Responsibility: parse the command line, hand the settings to the module
that does the work, print what it returns and choose the exit code. No
business logic here.

`ask` answers one question and `chat` holds one conversation; both run the
turn graph (`graph.py`) and print `presentation.render_text`. They call
Claude, which is billed, unless `--replay-dir` names a folder of recorded
replies. `graph` prints the turn graph as a Mermaid diagram. `scan` runs the
fraud-concentration scan of `explore.py` with no model and no key. `check-model`,
whose logic is in `model_check.py`, sends nothing unless `--yes-spend` is
given; its `--replay` re-prints a recorded run with no key and no request.
`eval`, whose logic is in `evals/command.py`, runs the evaluation suite under
the same rule: nothing is sent without `--yes-spend`, and `--replay` re-runs
a recorded run offline. `serve` runs the HTTP API of `api.py` and the built
web interface on the loopback address; its turns call Claude, unless
`--offline-demo` puts the canned replies of `demo.py` in the model's place,
which also ignores `.env`, so no key is read and nothing is sent or traced.
`find-rules` runs the rule finder of `finder.py` in one go, as two phases it
prints apart: it proposes scenarios, then processes each into a rule.
Nothing is sent without `--yes-spend`; `--offline-demo` runs the canned
scenarios of `demo.py` instead, and `--list` prints the rule bank and runs
nothing.

What `ask`, `chat` and `serve` share before their first turn (settings,
logging, the assistant with its label and its tracer) is in `runtime.py`.

A command that reads the settings also sets up logging (`applog.py`): a file
in the log directory and, with `--verbose`, the terminal's error stream.
`ask`, `chat`, a live `serve` and a live `eval` send a filtered trace of each turn to
LangSmith when the settings switch that on (`tracing.py`), and say in one
line whether they do.

Exit codes. 0: the command did its job; for `ask`, the turn was answered,
proposed a rule or asked for clarification. 1: the turn failed. 2: the
command could not start (a bad argument or setting, no key, an unusable
recordings folder). 3: `ask` could not answer (the model declined). 4: `ask`
was rejected by the guard. 130: `ask` was interrupted with Ctrl-C. `chat`
exits 0 whatever its turns ended in. `scan`: 0, 1 when the scan failed, 2
when it could not start. `serve`: 0 when the server was stopped, 2 when it
could not start. `find-rules`: 0 when every run completed, when the bank was
listed or when nothing was sent; 1 when the run failed or stopped early; 2
when it could not start. For
`eval`: 0 when a live run's cases all passed (skipped ones aside), when a
replay replayed every selected case and scored each exactly as recorded,
recorded failures included, and for `--list`, `--rescore`, or when nothing
was sent; 1 when a live run had a failed case; 2 when it could not start or
a recording is stale; 3 when a replay did not score as its record says, or
replayed nothing; 5 when every replayed case scored as recorded but some
selected cases have no recording.
"""

import argparse
import logging
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Literal

import uvicorn

from fsm_assistant import (
    api,
    config,
    db,
    demo,
    explore,
    finder,
    model_check,
    runtime,
    scenarios,
    tracing,
)
from fsm_assistant.config import ConfigError, Settings, load_settings
from fsm_assistant.evals import command as eval_command
from fsm_assistant.evals.cases import TIERS
from fsm_assistant.evals.runner import Selection
from fsm_assistant.graph import Assistant, TraceStep
from fsm_assistant.llm import (
    ClaudeModel,
    IntentModel,
    ModelSpec,
    ModelUnavailable,
    RecordingModel,
    ReplayModel,
    ScriptedModel,
    StaleRecording,
)
from fsm_assistant.nl2sql import model_spec
from fsm_assistant.presentation import render_scan, render_text, safe_text
from fsm_assistant.result import Status
from fsm_assistant.rule_bank import BankError

logger = logging.getLogger(__name__)

EXIT_WORDS = frozenset({"exit", "quit"})

EXIT_CANNOT_START = 2
EXIT_INTERRUPTED = 130
# What `ask` exits with, by the status of its one turn.
EXIT_BY_STATUS: dict[Status, int] = {
    "ok": 0,
    "needs_clarification": 0,
    "error": 1,
    "cannot_answer": 3,
    "rejected": 4,
}
EXIT_BY_FINISH: dict[eval_command.Finish, int] = {
    "passed": 0,
    "reproduced": 0,
    "failed": 1,
    "cannot_start": EXIT_CANNOT_START,
    "not_reproduced": 3,
    "partly_replayed": 5,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fsm-assistant",
        description="FSM assistant: ask questions about the transaction data in plain "
        "language, and turn patterns into backtested fraud rules.",
    )
    commands = parser.add_subparsers(dest="command")
    ask = commands.add_parser(
        "ask", help="answer one question (calls Claude: billed, unless --replay-dir)"
    )
    ask.add_argument("question", help="the question, in quotes")
    chat = commands.add_parser(
        "chat", help="hold one conversation (calls Claude: billed, unless --replay-dir)"
    )
    for command, case in ((ask, "ask"), (chat, "chat")):
        command.add_argument(
            "--trace", action="store_true", help="print what each step of the turn decided"
        )
        command.add_argument(
            "--replay-dir",
            type=Path,
            help="answer from the replies recorded in this folder; needs no key, sends nothing",
        )
        command.add_argument(
            "--record-dir", type=Path, help="also write each live reply to this folder"
        )
        command.add_argument(
            "--case",
            default=case,
            help=f"name of the recording files (default: {case})",
        )
    commands.add_parser("graph", help="print the turn graph as a Mermaid diagram")
    scan = commands.add_parser(
        "scan", help="rank the segments where labelled fraud is concentrated (no model, no key)"
    )
    scan.add_argument(
        "--period",
        nargs=2,
        metavar=("START", "END"),
        help="only these days, both included, as YYYY-MM-DD (default: all the data)",
    )
    scan.add_argument(
        "--top",
        type=int,
        default=explore.DEFAULT_TOP,
        metavar="N",
        help=f"how many segments to print (default: {explore.DEFAULT_TOP})",
    )
    scan.add_argument("--sql", action="store_true", help="also print every statement that ran")
    check = commands.add_parser(
        "check-model",
        help="measure how often a Claude model replies with exactly one valid tool call "
        "(paid; needs --yes-spend)",
    )
    check.add_argument("--model", help="Claude model id (default: ANTHROPIC_MODEL)")
    check.add_argument(
        "--yes-spend",
        action="store_true",
        help="really send the requests; each one is billed to your Anthropic account",
    )
    check.add_argument(
        "--replay",
        action="store_true",
        help="re-print the table from the recorded replies; needs no key and sends nothing",
    )
    evaluate = commands.add_parser(
        "eval",
        help="run the evaluation suite (paid; needs --yes-spend), or replay a recorded run",
    )
    evaluate.add_argument("--model", help="Claude model id (default: ANTHROPIC_MODEL)")
    evaluate.add_argument(
        "--yes-spend",
        action="store_true",
        help="really send the requests; each one is billed to your Anthropic account",
    )
    evaluate.add_argument(
        "--replay",
        action="store_true",
        help="re-run the cases from the recorded replies; needs no key and sends nothing",
    )
    evaluate.add_argument(
        "--list", action="store_true", help="print the selected cases and run nothing"
    )
    evaluate.add_argument(
        "--rescore",
        action="store_true",
        help="rebuild run.json and the report from the recorded replies; sends nothing",
    )
    evaluate.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="send every selected case N times, as separate conversations (N times the cost)",
    )
    evaluate.add_argument(
        "--tier", action="append", choices=TIERS, default=[], help="only this tier (repeatable)"
    )
    evaluate.add_argument(
        "--case", action="append", default=[], metavar="ID", help="only this case (repeatable)"
    )
    evaluate.add_argument("--tag", help="only cases with this tag")
    split = evaluate.add_mutually_exclusive_group()
    split.add_argument("--held-out", action="store_true", help="only held-out cases")
    split.add_argument("--tuned", action="store_true", help="only tuned cases")
    evaluate.add_argument("--limit", type=int, metavar="N", help="at most the first N cases")
    serve = commands.add_parser(
        "serve",
        help="run the web chat on this machine (calls Claude: billed, unless --offline-demo)",
    )
    serve.add_argument(
        "--host",
        default=api.DEFAULT_HOST,
        help=f"address to listen on (default: {api.DEFAULT_HOST}, this machine only)",
    )
    serve.add_argument(
        "--port", type=int, default=api.DEFAULT_PORT, help=f"default: {api.DEFAULT_PORT}"
    )
    serve.add_argument(
        "--offline-demo",
        action="store_true",
        help="answer a fixed list of questions with canned model replies; ignores .env, "
        "needs no key and sends nothing",
    )
    find = commands.add_parser(
        "find-rules",
        help="run the rule finder once: scenarios from the schema, each backtested and "
        "banked (paid; needs --yes-spend)",
    )
    find.add_argument(
        "--yes-spend",
        action="store_true",
        help="really send the requests; each one is billed to your Anthropic account",
    )
    find.add_argument(
        "--offline-demo",
        action="store_true",
        help="run the canned scenarios instead; ignores .env, needs no key and sends nothing",
    )
    find.add_argument(
        "--list", action="store_true", help="print the rule bank and run nothing"
    )
    find.add_argument(
        "--batch",
        type=int,
        metavar="N",
        help=f"how many scenarios to propose and process (default: FSM_FINDER_BATCH_SIZE, "
        f"{config.DEFAULT_FINDER_BATCH_SIZE}; at most {config.MAX_FINDER_BATCH_SIZE})",
    )
    for command in (ask, chat, check, evaluate, serve, find):
        command.add_argument(
            "--verbose", action="store_true", help="also print the log lines to stderr"
        )
    arguments = parser.parse_args(argv)
    if arguments.command == "eval":
        if arguments.yes_spend and (arguments.replay or arguments.list):
            evaluate.error("--yes-spend cannot be used with --replay or --list")
        if arguments.limit is not None and arguments.limit < 1:
            evaluate.error("--limit must be at least 1")
        if arguments.repeat < 1:
            evaluate.error("--repeat must be at least 1")
        filtered = bool(
            arguments.tier or arguments.case or arguments.tag or arguments.held_out
            or arguments.tuned or arguments.limit
        )  # fmt: skip
        if arguments.rescore and (
            arguments.yes_spend or arguments.replay or arguments.list or filtered
            or arguments.repeat > 1
        ):  # fmt: skip
            evaluate.error("--rescore rebuilds the whole record: it takes --model only")
        if arguments.list and arguments.repeat > 1:
            evaluate.error("--repeat cannot be used with --list")
        return _evaluate(
            eval_command.Options(
                model=arguments.model,
                spend=arguments.yes_spend,
                replay=arguments.replay,
                list_only=arguments.list,
                rescore=arguments.rescore,
                repeat=arguments.repeat,
                selection=Selection(
                    tiers=tuple(arguments.tier),
                    case_ids=tuple(arguments.case),
                    tag=arguments.tag,
                    split="held_out" if arguments.held_out else "tuned" if arguments.tuned else None,
                    limit=arguments.limit,
                ),
            ),
            verbose=arguments.verbose,
        )
    if arguments.command == "check-model":
        if arguments.yes_spend and arguments.replay:
            check.error("--yes-spend and --replay cannot be used together")
        return _check_model(
            model=arguments.model,
            spend=arguments.yes_spend,
            replay=arguments.replay,
            verbose=arguments.verbose,
        )
    if arguments.command in ("ask", "chat"):
        if arguments.replay_dir and arguments.record_dir:
            parser.error("--replay-dir and --record-dir cannot be used together")
        return _converse(
            question=arguments.question if arguments.command == "ask" else None,
            trace=arguments.trace,
            replay_dir=arguments.replay_dir,
            record_dir=arguments.record_dir,
            case=arguments.case,
            verbose=arguments.verbose,
        )
    if arguments.command == "serve":
        return _serve(
            host=arguments.host,
            port=arguments.port,
            offline_demo=arguments.offline_demo,
            verbose=arguments.verbose,
        )
    if arguments.command == "find-rules":
        if sum((arguments.yes_spend, arguments.offline_demo, arguments.list)) > 1:
            find.error("--yes-spend, --offline-demo and --list cannot be used together")
        if arguments.batch is not None and not (
            1 <= arguments.batch <= config.MAX_FINDER_BATCH_SIZE
        ):
            find.error(f"--batch must be between 1 and {config.MAX_FINDER_BATCH_SIZE}")
        return _find_rules(
            spend=arguments.yes_spend,
            offline_demo=arguments.offline_demo,
            list_only=arguments.list,
            batch=arguments.batch,
            verbose=arguments.verbose,
        )
    if arguments.command == "scan":
        if arguments.top < 1:
            scan.error("--top must be at least 1")
        return _scan(arguments.period, arguments.top, show_sql=arguments.sql)
    if arguments.command == "graph":
        # The diagram depends on no setting, so none is read: no `.env`, no key.
        defaults = config.load_settings({}, env_file=None)
        print(Assistant(defaults, ScriptedModel(())).mermaid())
        return 0
    parser.print_help()
    return 0


def _converse(
    *,
    question: str | None,
    trace: bool,
    replay_dir: Path | None,
    record_dir: Path | None,
    case: str,
    verbose: bool,
) -> int:
    """One question (`ask`) or, when `question` is None, a loop of them (`chat`)."""
    command: Literal["ask", "chat"] = "ask" if question is not None else "chat"
    try:
        settings = _start(command, verbose)
        spec = model_spec(settings)
        model: IntentModel
        if replay_dir is not None:
            model = ReplayModel(spec, replay_dir, case)
            print(f"Model: recorded replies of {spec.model} from {replay_dir}. Nothing is sent.")
        else:
            model = _live_model(spec, settings)
            if record_dir is not None:
                model = RecordingModel(model, record_dir, case)
                _writable_directory(record_dir)
            print(f"Model: {spec.model}, live. Each question is billed to ANTHROPIC_API_KEY.")
    except (ModelUnavailable, ValueError) as error:  # ValueError: a bad setting or case name
        if isinstance(error, ModelUnavailable):
            logger.error("%s cannot start: no model client (%s)", command, error.kind)
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    live = replay_dir is None
    assistant, tracer = runtime.open_assistant(
        command, settings, model, replies="live" if live else "recorded", warn=_warn(verbose)
    )
    print(tracing.describe(tracer, settings, live=live))
    try:
        return _turns(assistant, question, trace)
    finally:
        if tracer is not None:
            tracer.close()


def _turns(assistant: Assistant, question: str | None, trace: bool) -> int:
    conversation = uuid.uuid4().hex
    on_step = _print_step if trace else None
    if question is not None:
        try:
            result = assistant.run_turn(question, conversation, on_step=on_step)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return EXIT_INTERRUPTED
        print(render_text(result))
        return EXIT_BY_STATUS[result.status]
    print("Ask a question; 'exit' or Ctrl-D to leave. Ctrl-C stops the question being answered.")
    while (line := _read_line()) is not None:
        if line.strip().lower() in EXIT_WORDS:
            break
        if line.strip():
            try:
                print(render_text(assistant.run_turn(line, conversation, on_step=on_step)))
            except KeyboardInterrupt:
                # Back to the prompt; the conversation keeps what it remembered before.
                print("\nInterrupted. That question was not answered.")
            print()
    return 0


def _scan(period: Sequence[str] | None, top: int, *, show_sql: bool) -> int:
    try:
        settings = _start("scan", verbose=False)
        chosen = None
        if period is not None:
            start, end = (date.fromisoformat(day) for day in period)
            chosen = explore.Period(start=start, end=end)
    except ValueError as error:  # a bad setting, a day that is not a date, or an end before the start
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    try:
        result = explore.scan(
            db_path=settings.db_path,
            row_cap=settings.row_cap,
            timeout_seconds=settings.query_timeout_seconds,
            max_result_bytes=settings.max_result_bytes,
            period=chosen,
        )
    except (db.DbError, explore.ScanError) as error:
        logger.error("scan failed: %s", type(error).__name__)
        print(f"The scan failed: {error}")
        return 1
    # Counts and a time only: a segment's name is a value read from the data.
    logger.info(
        "scan finished: listed=%d statements=%d elapsed_ms=%d",
        len(result.rows),
        len(result.statements),
        round(result.elapsed_seconds * 1000),
    )
    print(render_scan(result, top=top))
    if show_sql:
        for number, statement in enumerate(result.statements, start=1):
            print(f"\nStatement {number} of {len(result.statements)}:\n{statement}")
    return 0


def _serve(*, host: str, port: int, offline_demo: bool, verbose: bool) -> int:
    try:
        model: IntentModel
        if offline_demo:
            # Defaults only: `.env` and the environment are not read, so no key and no tracing.
            settings = runtime.start(
                "serve", verbose=verbose, load=lambda: config.load_settings({}, env_file=None)
            )
            model = demo.DemoModel(model_spec(settings))
            print(
                "Model: none. OFFLINE DEMO: canned replies to a fixed list of questions, run "
                "through the real guard, database and presentation. Nothing is sent or billed."
            )
        else:
            settings = _start("serve", verbose)
            model = _live_model(model_spec(settings), settings)
            print(
                f"Model: {settings.anthropic_model}, live. Each question asked in the "
                "browser is billed to ANTHROPIC_API_KEY."
            )
    except (ModelUnavailable, ValueError) as error:  # ValueError: a bad setting
        if isinstance(error, ModelUnavailable):
            logger.error("serve cannot start: no model client (%s)", error.kind)
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    replies: runtime.Replies = "demo" if offline_demo else "live"
    assistant, tracer = runtime.open_assistant(
        "web", settings, model, replies=replies, warn=_warn(verbose)
    )
    try:
        rule_finder = _finder(settings, replies, turn_model=model, tracer=tracer)
    except (BankError, ModelUnavailable) as error:
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    print(
        "Hosted tracing: off (the offline demo is never traced)."
        if offline_demo
        else tracing.describe(tracer, settings)
    )
    local = host in api.LOOPBACK_HOSTS
    if not local:
        print(
            f"Warning: listening on {host}. There is no authentication: anyone who can reach "
            "this address can ask questions, read the data and spend the key."
        )
    print(api.describe_page())
    app = api.create_app(
        settings,
        assistant,
        mode="offline_demo" if offline_demo else "live",
        model_id=demo.MODEL_ID if offline_demo else None,
        demo_questions=demo.QUESTIONS if offline_demo else (),
        tracer=tracer,
        finder=rule_finder,
        allowed_hosts=api.LOOPBACK_HOSTS if local else (*api.LOOPBACK_HOSTS, host),
    )
    # Flushed: the server then runs until it is stopped, and the lines above are wanted now.
    print(f"Open http://{host}:{port}/ in a browser. Ctrl-C stops the server.", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def _finder(
    settings: Settings,
    replies: runtime.Replies,
    *,
    turn_model: IntentModel | None = None,
    tracer: tracing.Tracer | None = None,
) -> finder.Finder:
    """The rule finder: the scenario request's own binding of the model, and the turns'."""
    spec = scenarios.scenario_spec(settings)
    if replies == "demo":
        return runtime.open_finder(
            settings,
            scenario_model=demo.DemoScenarioModel(spec),
            turn_model=turn_model or demo.DemoModel(model_spec(settings)),
            replies=replies,
            model_id=demo.MODEL_ID,
        )
    return runtime.open_finder(
        settings,
        scenario_model=_live_model(spec, settings),
        turn_model=turn_model or _live_model(model_spec(settings), settings),
        replies=replies,
        tracer=tracer,
    )


def _find_rules(
    *, spend: bool, offline_demo: bool, list_only: bool, batch: int | None, verbose: bool
) -> int:
    replies: runtime.Replies = "demo" if offline_demo else "live"
    try:
        if offline_demo:
            settings = runtime.start(
                "find-rules", verbose=verbose, load=lambda: config.load_settings({}, env_file=None)
            )
        else:
            settings = _start("find-rules", verbose)
        if batch is not None:
            settings = settings.model_copy(update={"finder_batch_size": batch})
        bank = runtime.bank_path(settings, replies)
        if list_only:
            listed = runtime.open_bank(settings, replies)
            print(finder.render_bank(listed.entries()))
            for waiting in listed.proposed():
                print(f"  proposed, entry {waiting.id}: {safe_text(waiting.title, 80)}")
            return 0
        if not (spend or offline_demo):
            print(finder.render_plan(settings, str(bank)))
            key = "set" if settings.anthropic_api_key is not None else "NOT set"
            print(f"  ANTHROPIC_API_KEY is {key}.")
            print("Nothing was sent. To run it, add --yes-spend; --offline-demo runs canned scenarios.")
            return 0
        tracer = None if offline_demo else tracing.start(settings, _warn(verbose))
        rule_finder = _finder(settings, replies, tracer=tracer)
    except (ModelUnavailable, BankError, ValueError) as error:  # ValueError: a bad setting
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    if offline_demo:
        print("Model: none. OFFLINE DEMO: canned scenarios and clauses, run through the real "
              "guard, backtest and bank. Nothing is sent or billed.")  # fmt: skip
    else:
        print(f"Model: {settings.anthropic_model}, live. Each request is billed to ANTHROPIC_API_KEY.")
        print(tracing.describe(tracer, settings))
    print(f"Rule bank: {bank}")
    count = settings.finder_batch_size
    failed = False
    try:
        print(f"Phase 1, propose: asking for {count} scenario{'' if count == 1 else 's'}.")
        proposed = rule_finder.propose(count)
        print(finder.render_run(proposed))
        failed = proposed.status != "completed"
        if proposed.items:
            print("Phase 2, process: one rule turn per scenario.")
        for item in proposed.items:
            print(f"  {item.position:>2}. {safe_text(item.title, 60)}")
            run = rule_finder.process(item.entry_id, _print_progress)
            print(finder.render_run(run))
            if run.status != "completed":
                failed = True
                if run.error is not None and run.error.kind == "model_unavailable":
                    print("The model stopped answering; the scenarios left stay proposed.")
                    break
    except BankError as error:
        print(f"The rule bank could not be used: {error}")
        failed = True
    finally:
        if tracer is not None:
            tracer.close()
    try:
        print(finder.render_bank(rule_finder.bank.entries()))
        left = len(rule_finder.bank.proposed())
        if left:
            print(f"{left} proposed scenario(s) are waiting to be processed, in the web page.")
    except BankError as error:
        print(f"The rule bank could not be listed: {error}")
    return 1 if failed else 0


_printed_steps: list[tuple[int, str | None, int]] = []


def _print_progress(progress: finder.RunProgress) -> None:
    """Print each step of a processing run once, as it begins."""
    key = (progress.run_id, progress.step, progress.attempt)
    if _printed_steps[-1:] == [key]:
        return
    _printed_steps[:] = [key]
    line = finder.render_step(progress)
    if line is not None:
        print(line, flush=True)


def _start(command: str, verbose: bool) -> Settings:
    """Load the settings and set up logging. A bad setting is logged, then raised."""
    return runtime.start(command, verbose=verbose, load=load_settings)


def _warn(verbose: bool) -> Callable[[str], None] | None:
    # With --verbose the log already shows a tracing warning on the terminal.
    return None if verbose else _to_stderr


def _tracer(settings: Settings, verbose: bool) -> tracing.Tracer | None:
    return tracing.start(settings, _warn(verbose))


def _to_stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _writable_directory(directory: Path) -> None:
    """Create `directory` if needed, or raise a `ValueError` saying why it cannot be written to."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError(
            f"--record-dir {directory} cannot be used as a folder: {error.strerror}."
        ) from None
    if not os.access(directory, os.W_OK | os.X_OK):
        raise ValueError(f"--record-dir {directory} is not writable.")


def _read_line() -> str | None:
    try:
        return input("> ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _print_step(step: TraceStep) -> None:
    print(f"  [trace] {safe_text(step.node)}: {safe_text(step.summary)}")


def _live_model(spec: ModelSpec, settings: Settings) -> IntentModel:
    return ClaudeModel.from_settings(spec, settings)


def _evaluate(options: eval_command.Options, *, verbose: bool) -> int:
    try:
        settings = _start("eval", verbose)
    except ConfigError as error:
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    logger.info(
        "eval started: model=%s replay=%s rescore=%s repeat=%s spend=%s",
        options.model or settings.anthropic_model, options.replay, options.rescore,
        options.repeat, options.spend,
    )  # fmt: skip
    finish = eval_command.execute(
        options,
        settings,
        print,
        live_model=_live_model,
        tracer_for=lambda settings: _tracer(settings, verbose),
    )
    logger.info("eval finished: %s", finish)
    return EXIT_BY_FINISH[finish]


def _check_model(*, model: str | None, spend: bool, replay: bool, verbose: bool) -> int:
    try:
        settings = _start("check-model", verbose)
    except ConfigError as error:
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    spec = model_check.check_spec(settings, model)
    logger.info("check-model started: model=%s replay=%s spend=%s", spec.model, replay, spend)
    if replay:
        return _replay(spec, settings)
    count = len(model_check.CHECK_QUESTIONS)
    print(model_check.render_plan(spec))
    if not spend:
        key = "set" if settings.anthropic_api_key is not None else "NOT set"
        print(f"  ANTHROPIC_API_KEY is {key}.")
        print("Nothing was sent. The questions that would be sent:")
        for number, question in enumerate(model_check.CHECK_QUESTIONS, start=1):
            print(f"  {number:>2}. {question}")
        print("To run it, add --yes-spend. A run records its replies; --replay re-prints them.")
        return 0

    directory = _directory(spec.model)
    if directory is None:
        return 2
    try:
        client = _live_model(spec, settings)
    except ModelUnavailable as error:
        print(f"Cannot start: {error}")
        return 2
    system = model_check.system_message(settings)
    try:
        summary = model_check.run_check(client, spec, system, directory)
    except model_check.IncompleteRun as error:
        print(f"Stopped after the first request: {error.stopped_by}.")
        print(model_check.render_table(error.rows))
        print("Incomplete run: no decision.")
        return 2
    print(model_check.render_table(summary.questions))
    print(model_check.render_decision(summary))
    model_check.write_summary(directory, summary)
    recorded = model_check.recorded_count(summary)
    shown = model_check.display_path(directory)
    print(f"Recorded {recorded} of {count} replies and {model_check.SUMMARY_FILE} in {shown}.")
    if recorded < count:
        print("A request that got no reply is not recorded, so --replay will stop there.")
    return 0


def _replay(spec: ModelSpec, settings: Settings) -> int:
    directory = _directory(spec.model)
    if directory is None:
        return 2
    count = len(model_check.CHECK_QUESTIONS)
    print(
        f"check-model --replay: reading the {count} recorded replies of {spec.model} "
        f"from {model_check.display_path(directory)}. Nothing is sent."
    )
    system = model_check.system_message(settings)
    try:
        summary = model_check.replay_check(spec, system, directory)
    except StaleRecording as error:
        print(f"Cannot replay: {error}")
        print("Recordings are made by a paid run: check-model --yes-spend.")
        return 2
    print(model_check.render_table(summary.questions))
    print(model_check.render_decision(summary))
    return 0


def _directory(model: str) -> Path | None:
    directory = model_check.recordings_directory(model)
    if directory is None:
        print(f"Cannot name a recordings folder after model id {model!r}.")
    return directory
