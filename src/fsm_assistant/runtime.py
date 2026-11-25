"""Start-up shared by the interfaces.

Responsibility: what a terminal command and the web server both do before
their first turn, in one place, so a turn is logged and traced the same way
whichever interface ran it (docs/DECISIONS.md, D-27, D-31). `start` loads the
settings and sets up logging; `open_assistant` builds the one `Assistant` of
the process with its label and, for live replies only, its tracer, and writes
the start-up log line.

Nothing here prints, parses arguments or chooses a model: the caller does.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from fsm_assistant import applog, config, tracing
from fsm_assistant.config import ConfigError, Settings, load_settings
from fsm_assistant.events import Command, Label
from fsm_assistant.finder import Finder
from fsm_assistant.graph import Assistant
from fsm_assistant.llm import IntentModel
from fsm_assistant.nl2sql import model_spec
from fsm_assistant.rule_bank import RuleBank, RunSource

logger = logging.getLogger(__name__)

# Where the model's replies come from: Claude, recorded files, or the canned
# replies of the offline demo. Only live replies are traced.
Replies = Literal["live", "recorded", "demo"]


def start(
    command: str, *, verbose: bool, load: Callable[[], Settings] = load_settings
) -> Settings:
    """Load the settings and set up logging. A bad setting is logged, then raised."""
    try:
        settings = load()
    except ConfigError as error:
        applog.configure(directory=config.DEFAULT_LOG_DIR, to_terminal=verbose)
        logger.error("%s cannot start: invalid setting: %s", command, error)
        raise
    applog.configure(
        directory=settings.log_dir if settings.log_file else None,
        level=settings.log_level,
        to_terminal=verbose,
    )
    return settings


def open_assistant(
    command: Command,
    settings: Settings,
    model: IntentModel,
    *,
    replies: Replies,
    warn: Callable[[str], None] | None = None,
) -> tuple[Assistant, tracing.Tracer | None]:
    """The assistant for `command` and its tracer, which the caller closes when it ends."""
    tracer = tracing.start(settings, warn) if replies == "live" else None
    spec = model_spec(settings)
    logger.info(
        "%s started: model=%s prompt=%s tool_mode=%s replies=%s tracing=%s",
        command,
        spec.model,
        spec.prompt_version,
        spec.tool_mode,
        replies,
        "off" if tracer is None else tracer.target.describe(),
    )
    return Assistant(settings, model, label=Label(command=command), tracer=tracer), tracer


def bank_path(settings: Settings, replies: Replies) -> Path:
    """Where a finder run's results go. The offline demo's canned rules get a file of their own."""
    path = settings.rule_bank_path
    return path.with_name(f"{path.stem}.demo{path.suffix}") if replies == "demo" else path


def open_bank(settings: Settings, replies: Replies) -> RuleBank:
    """The rule bank. Raises `rule_bank.BankError` when its path is the transaction database."""
    return RuleBank(bank_path(settings, replies), not_the_database=settings.db_path)


def open_finder(
    settings: Settings,
    *,
    scenario_model: IntentModel,
    turn_model: IntentModel,
    replies: Replies,
    model_id: str | None = None,
    tracer: tracing.Tracer | None = None,
) -> Finder:
    """The rule finder of the process. Raises `rule_bank.BankError` for an unusable bank path."""
    sources: dict[Replies, RunSource] = {
        "live": "live", "demo": "offline_demo", "recorded": "scripted"
    }  # fmt: skip
    bank = open_bank(settings, replies)
    return Finder(
        settings,
        scenario_model,
        turn_model,
        bank,
        source=sources[replies],
        model_id=model_id,
        tracer=tracer,
    )
