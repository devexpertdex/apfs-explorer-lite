"""Application bootstrap."""

from __future__ import annotations

import faulthandler
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from PySide6.QtCore import QSettings
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import APP_NAME, __version__
from .backends import DEFAULT_BACKEND, default_registry
from .disks import WindowsDiskDiscovery
from .disks.win32 import Win32DiskApi, relaunch_as_admin
from .gui import MainWindow
from .logging_config import configure_logging, install_excepthook
from .resources import resource_path
from .structured_log import log_event

logger = logging.getLogger(__name__)

APP_USER_MODEL_ID = "APFSExplorerLite.App"
_crash_log: TextIO | None = None


def enable_crash_log(log_dir: Path) -> Path:
    """Record native crashes (e.g. inside a DLL) to crash.log; a windowed app has no console."""
    global _crash_log
    path = log_dir / "crash.log"
    if _crash_log is None:
        _crash_log = open(path, "a", encoding="utf-8")  # noqa: SIM115 - must stay open for faulthandler
        faulthandler.enable(_crash_log)
    return path


def set_app_user_model_id() -> None:
    """Group the taskbar button under the app's own icon, not python.exe's."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (OSError, AttributeError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    log_file = configure_logging()
    install_excepthook()
    enable_crash_log(log_file.parent)
    logger.info(
        "Starting %s %s (Python %s, %s)", APP_NAME, __version__, sys.version.split()[0],
        "packaged" if getattr(sys, "frozen", False) else "from source",
    )  # fmt: skip
    set_app_user_model_id()

    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(resource_path("app.ico"))))

    backend_status = default_registry().availability(DEFAULT_BACKEND)
    log_event(
        logger, logging.INFO, "backend.status", backend=backend_status.name,
        available=backend_status.available, version=backend_status.version,
    )  # fmt: skip

    disk_api = Win32DiskApi()
    is_admin = disk_api.is_admin()
    logger.info("Administrator: %s", is_admin)
    discovery = WindowsDiskDiscovery(disk_api)

    registry = default_registry()
    window = MainWindow(
        backend_status,
        discovery.scan,
        is_admin=is_admin,
        backend_factory=lambda: registry.create(DEFAULT_BACKEND),
        elevate=None if is_admin else relaunch_as_admin,
        log_file=log_file,
        settings=QSettings(APP_NAME, APP_NAME),
    )
    window.show()
    exit_code = app.exec()
    logger.info("Exiting with code %s", exit_code)
    return exit_code
