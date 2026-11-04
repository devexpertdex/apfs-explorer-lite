"""Application logging: rotating JSON-lines file log plus stderr (see structured_log)."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from . import APP_NAME
from .structured_log import JsonFormatter

LOG_FILE_NAME = "apfs_explorer_lite.log"
_HANDLER_MARKER = "_apfs_explorer_lite_handler"


def default_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME / "logs"


def configure_logging(log_dir: Path | None = None, level: int = logging.INFO) -> Path:
    """Configure the root logger and return the log file path.

    Safe to call more than once: handlers added by a previous call are replaced.
    """
    directory = log_dir if log_dir is not None else default_log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / LOG_FILE_NAME

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]:
        root.removeHandler(handler)
        handler.close()

    formatter = JsonFormatter()
    file_handler = RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handlers: list[logging.Handler] = [file_handler]
    # Windowed (PyInstaller --noconsole) builds have no stderr.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)
    root.setLevel(level)
    return log_file


def install_excepthook() -> None:
    """Log uncaught exceptions instead of losing them in a windowed build."""

    def _hook(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger(APP_NAME).critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook
