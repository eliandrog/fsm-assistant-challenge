"""Terminal entry point.

Responsibility: parse the command line, hand the settings to the module
that does the work, print what it returns and choose the exit code. No
business logic here.

`ask` answers one question and `chat` holds one conversation; both run the
turn graph (`graph.py`) and print `presentation.render_text`. They call
Claude, which is billed, unless `--replay-dir` names a folder of recorded
replies. `graph` prints the turn graph as a Mermaid diagram. `check-model`,
whose logic is in `model_check.py`, sends nothing unless `--yes-spend` is
given; its `--replay` re-prints a recorded run with no key and no request.
`eval`, whose logic is in `evals/command.py`, runs the evaluation suite under
the same rule: nothing is sent without `--yes-spend`, and `--replay` re-runs
a recorded run offline.

Exit codes. 0: the command did its job; for `ask`, the turn was answered,
proposed a rule or asked for clarification. 1: the turn failed. 2: the
command could not start (a bad argument or setting, no key, an unusable
recordings folder). 3: `ask` could not answer (the model declined, or the
scan is not available). 4: `ask` was rejected by the guard. 130: `ask` was
interrupted with Ctrl-C. `chat` exits 0 whatever its turns ended in. For
`eval`: 0 when every selected case that was not skipped passed (and for
`--list`, or when nothing was sent); 1 when it ran and a case failed; 2 when
it could not start or a recording is stale; 3 when a replay did not score as
its record says, or a selected case has no recording.
"""

import argparse
import os
import uuid
from collections.abc import Sequence
from pathlib import Path

from fsm_assistant import config, model_check
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
from fsm_assistant.presentation import render_text, safe_text
from fsm_assistant.result import Status

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
    "failed": 1,
    "cannot_start": EXIT_CANNOT_START,
    "not_reproduced": 3,
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
    arguments = parser.parse_args(argv)
    if arguments.command == "eval":
        if arguments.yes_spend and (arguments.replay or arguments.list):
            evaluate.error("--yes-spend cannot be used with --replay or --list")
        if arguments.limit is not None and arguments.limit < 1:
            evaluate.error("--limit must be at least 1")
        return _evaluate(
            eval_command.Options(
                model=arguments.model,
                spend=arguments.yes_spend,
                replay=arguments.replay,
                list_only=arguments.list,
                selection=Selection(
                    tiers=tuple(arguments.tier),
                    case_ids=tuple(arguments.case),
                    tag=arguments.tag,
                    split="held_out" if arguments.held_out else "tuned" if arguments.tuned else None,
                    limit=arguments.limit,
                ),
            )
        )
    if arguments.command == "check-model":
        if arguments.yes_spend and arguments.replay:
            check.error("--yes-spend and --replay cannot be used together")
        return _check_model(
            model=arguments.model, spend=arguments.yes_spend, replay=arguments.replay
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
        )
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
) -> int:
    """One question (`ask`) or, when `question` is None, a loop of them (`chat`)."""
    try:
        settings = load_settings()
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
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    assistant = Assistant(settings, model)
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


def _evaluate(options: eval_command.Options) -> int:
    try:
        settings = load_settings()
    except ConfigError as error:
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    return EXIT_BY_FINISH[eval_command.execute(options, settings, print, live_model=_live_model)]


def _check_model(*, model: str | None, spend: bool, replay: bool) -> int:
    try:
        settings = load_settings()
    except ConfigError as error:
        print(f"Cannot start: {error}")
        return EXIT_CANNOT_START
    spec = model_check.check_spec(settings, model)
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
