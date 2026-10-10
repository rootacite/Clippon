"""Stderr logs with a fixed column layout, plus an optional session file."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

_LOGGER = "clippon"
_LEVELS = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRIT",
}
_COLORS = {
    logging.DEBUG: "\033[2m",
    logging.INFO: "\033[36m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}
_RESET = "\033[0m"
# "HH:MM:SS" + "  " + 5-wide level + "  "
_INDENT = " " * 17


class _Formatter(logging.Formatter):
    def __init__(self, color: bool) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        label = _LEVELS.get(record.levelno, record.levelname)
        lines = record.getMessage().splitlines() or [""]
        rendered = [f"{stamp}  {label:<5}  {lines[0]}"]
        rendered.extend(f"{_INDENT}{line}" for line in lines[1:])
        text = "\n".join(rendered)
        if record.exc_info:
            text = f"{text}\n{self.formatException(record.exc_info)}"
        if self.color:
            return f"{_COLORS.get(record.levelno, '')}{text}{_RESET}"
        return text


def _color_stderr() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stderr.isatty()


def setup_logging(verbose: bool = False) -> None:
    """Attach a stderr handler. Replaces any handler installed earlier."""
    logger = logging.getLogger(_LOGGER)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter(_color_stderr()))
    logger.addHandler(handler)


def add_log_file(path: Path) -> None:
    """Copy the same lines, without color, into the session log."""
    logger = logging.getLogger(_LOGGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_Formatter(color=False))
    logger.addHandler(handler)


def note(component: str, message: str, *args: object, level: int = logging.INFO) -> None:
    logging.getLogger(_LOGGER).log(level, "%-5s  " + message, component, *args)


def warn(component: str, message: str, *args: object) -> None:
    note(component, message, *args, level=logging.WARNING)


def error(component: str, message: str, *args: object) -> None:
    note(component, message, *args, level=logging.ERROR)


def debug(component: str, message: str, *args: object) -> None:
    note(component, message, *args, level=logging.DEBUG)


def block(text: str) -> None:
    """Log a multi-line summary. Percent signs in *text* are not interpolated."""
    logging.getLogger(_LOGGER).info("%s", text)
