"""Application logging.

Responsibility: set up the package's logging once, when a command starts
(docs/DECISIONS.md, D-27). Every other module only gets a logger
(`logging.getLogger(__name__)`) and never adds a handler, so the same set-up
serves any entry point.

Plain lines for checking on a running system: time, level, logger, message.
They say that a command started, how each turn ended, and what went wrong.
They never carry the question, the SQL, a result row, a database value, the
text of an exception or a key: a message is written by code from counts,
codes and class names. The steps of a turn are not logged; that is what
`--trace` and the hosted trace are for.

Lines go to `app.log` in the log directory, rotated by size so the directory
stays bounded, and also to the terminal's error stream when asked for.
Logging never fails a command: a line that cannot be written is dropped.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fsm_assistant.config import LogLevel

LOGGER_NAME = "fsm_assistant"
LOG_FILE = "app.log"
# The file is rotated before it would pass this size, and this many older files are kept.
MAX_BYTES = 2_000_000
BACKUPS = 3
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure(
    *,
    directory: Path | None,
    level: LogLevel = "INFO",
    to_terminal: bool = False,
    max_bytes: int = MAX_BYTES,
    backups: int = BACKUPS,
) -> None:
    """Send the package's log lines to a file in `directory` (None: no file) and, if asked, to stderr.

    Calling it again replaces the earlier set-up.
    """
    logger = logging.getLogger(LOGGER_NAME)
    for earlier in [handler for handler in logger.handlers if isinstance(handler, _Quiet)]:
        logger.removeHandler(earlier)
        earlier.close()
    handlers: list[logging.Handler] = []
    if directory is not None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # the handler then fails to open the file, quietly, line by line
        handlers.append(
            _QuietFile(
                directory / LOG_FILE,
                maxBytes=max_bytes,
                backupCount=backups,
                encoding="utf-8",
                errors="backslashreplace",
                delay=True,
            )
        )
    if to_terminal:
        handlers.append(_QuietStream(sys.stderr))
    if not handlers:
        handlers.append(_QuietNull())  # or Python prints warnings to stderr by itself
    for handler in handlers:
        handler.setFormatter(logging.Formatter(FORMAT))
        logger.addHandler(handler)
    logger.setLevel(level)


class _Quiet:
    """Marks the handlers this module added. A failed write is dropped, not reported."""

    def handleError(self, record: logging.LogRecord) -> None:
        return None


class _QuietFile(_Quiet, RotatingFileHandler):
    pass


class _QuietStream(_Quiet, logging.StreamHandler):  # type: ignore[type-arg]  # generic in the stubs only
    pass


class _QuietNull(_Quiet, logging.NullHandler):
    pass
