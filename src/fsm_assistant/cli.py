"""Terminal entry point.

Responsibility: parse the command line, hand the settings to the module
that does the work, print what it returns and choose the exit code. No
business logic here.

One command exists today, `check-model`, whose logic is in
`model_check.py`. It sends nothing unless `--yes-spend` is given;
`--replay` re-prints a recorded run with no key and no request.
The chat commands arrive with the turn graph.
"""

import argparse
from collections.abc import Sequence
from pathlib import Path

from fsm_assistant import model_check
from fsm_assistant.config import Settings, load_settings
from fsm_assistant.llm import ClaudeModel, IntentModel, ModelSpec, ModelUnavailable, StaleRecording


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fsm-assistant",
        description="FSM assistant. The chat is not built yet; one command exists.",
    )
    commands = parser.add_subparsers(dest="command")
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
    arguments = parser.parse_args(argv)
    if arguments.command == "check-model":
        if arguments.yes_spend and arguments.replay:
            check.error("--yes-spend and --replay cannot be used together")
        return _check_model(
            model=arguments.model, spend=arguments.yes_spend, replay=arguments.replay
        )
    parser.print_help()
    return 0


def _live_model(spec: ModelSpec, settings: Settings) -> IntentModel:
    return ClaudeModel.from_settings(spec, settings)


def _check_model(*, model: str | None, spend: bool, replay: bool) -> int:
    settings = load_settings()
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
