"""Main application window: drive list, then the explorer for an opened drive."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSettings, Slot
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox, QStackedWidget, QWidget

from .. import APP_NAME, __version__
from ..backends.base import BackendAvailability
from ..errors import ApfsExplorerError, BackendNotOpenError, DiskDisconnectedError
from ..models import ApfsContainerCandidate, ApfsIdentification
from .drive_selection import DriveSelectionWidget, ElevateFunction, ScanFunction
from .explorer import BackendFactory, ExplorerWidget
from ..structured_log import log_event
from .theme import ThemeMode, apply_theme, parse_theme

logger = logging.getLogger(__name__)

DRIVE_LOST_TEXT = (
    "The drive was disconnected or stopped responding. Reconnect it and click Refresh Drives."
)


class MainWindow(QMainWindow):
    def __init__(
        self,
        backend_status: BackendAvailability,
        scan: ScanFunction,
        *,
        is_admin: bool,
        backend_factory: BackendFactory | None = None,
        elevate: ElevateFunction | None = None,
        log_file: Path | None = None,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend_status = backend_status
        self._log_file = log_file
        self._settings = settings

        self.setWindowTitle(f"{APP_NAME} (Administrator)" if is_admin else APP_NAME)
        self.resize(1180, 700)

        self.drive_selection = DriveSelectionWidget(scan, is_admin=is_admin, elevate=elevate)
        self.explorer = ExplorerWidget(backend_factory or _unavailable_factory(backend_status), settings=settings)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.drive_selection)
        self.pages.addWidget(self.explorer)
        self.setCentralWidget(self.pages)

        if not backend_status.available:
            self.drive_selection.set_backend_problem(backend_status.message)
        self.drive_selection.openRequested.connect(self._open_container)
        self.drive_selection.scanFinished.connect(lambda _r: self.statusBar().showMessage("Drive scan finished.", 5000))
        self.explorer.closed.connect(self._show_drives)
        self.explorer.failed.connect(self._on_explorer_failed)

        self._build_actions()
        self._build_menus()

        self.backend_label = QLabel(self._backend_status_text())
        self.backend_label.setObjectName("backendStatus")
        self.backend_label.setToolTip(backend_status.message)
        self.statusBar().addWidget(self.explorer.status_label, 1)
        self.statusBar().addPermanentWidget(self.explorer.busy_bar)
        self.statusBar().addPermanentWidget(self.backend_label)
        self.explorer.status_label.hide()

        self.set_theme(parse_theme(self._setting("theme", ThemeMode.SYSTEM.value)), save=False)
        self.hidden_action.setChecked(self._setting("showHidden", "false") == "true")

    # --- pages -------------------------------------------------------------------

    @property
    def showing_explorer(self) -> bool:
        return self.pages.currentWidget() is self.explorer

    @Slot(object, str)
    def _open_container(self, container: ApfsContainerCandidate, title: str) -> None:
        self.drive_selection.show_notice(None)
        self.pages.setCurrentWidget(self.explorer)
        self.explorer.status_label.show()
        self.explorer.open_container(
            container.to_source_device(),
            title,
            damaged_first_block=container.identification is ApfsIdentification.RECOVERABLE,
        )

    @Slot()
    def _show_drives(self) -> None:
        self.pages.setCurrentWidget(self.drive_selection)
        self.explorer.status_label.hide()

    @Slot(object)
    def _on_explorer_failed(self, error: ApfsExplorerError) -> None:
        self._show_drives()
        if isinstance(error, (DiskDisconnectedError, BackendNotOpenError)):
            text = DRIVE_LOST_TEXT
        else:
            text = f"The drive couldn't be opened: {error.user_message}"
        self.drive_selection.show_notice(f"⚠ {text}")

    # --- actions and menus ----------------------------------------------------------

    def _build_actions(self) -> None:
        self.refresh_drives_action = QAction("&Refresh Drives", self)
        self.refresh_drives_action.triggered.connect(self.drive_selection.refresh)

        self.exit_action = QAction("E&xit", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)

        self.hidden_action = QAction("Show &hidden items", self)
        self.hidden_action.setCheckable(True)
        self.hidden_action.setToolTip("Show macOS system files such as .DS_Store")
        self.hidden_action.toggled.connect(self._on_hidden_toggled)

        self.theme_group = QActionGroup(self)
        self.theme_actions: dict[ThemeMode, QAction] = {}
        for mode in ThemeMode:
            act = QAction(mode.label, self, checkable=True)
            act.triggered.connect(lambda _checked=False, m=mode: self.set_theme(m))
            self.theme_group.addAction(act)
            self.theme_actions[mode] = act

        self.about_action = QAction(f"&About {APP_NAME}", self)
        self.about_action.triggered.connect(self._show_about)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.refresh_drives_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.hidden_action)
        theme_menu = view_menu.addMenu("&Theme")
        for act in self.theme_actions.values():
            theme_menu.addAction(act)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self.about_action)

    def set_theme(self, mode: ThemeMode, *, save: bool = True) -> None:
        apply_theme(mode)
        self.theme_actions[mode].setChecked(True)
        if save and self._settings is not None:
            self._settings.setValue("theme", mode.value)

    @Slot(bool)
    def _on_hidden_toggled(self, checked: bool) -> None:
        self.explorer.set_show_hidden(checked)
        if self._settings is not None:
            self._settings.setValue("showHidden", "true" if checked else "false")

    def _setting(self, key: str, default: str) -> str:
        if self._settings is None:
            return default
        return str(self._settings.value(key, default))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        log_event(logger, logging.INFO, "window.close_requested", copy_running=self.explorer.copy_running)
        if self.explorer.copy_running and not self.confirm_exit_during_copy():
            log_event(logger, logging.INFO, "window.close_cancelled")
            event.ignore()
            return
        self.explorer.shutdown()
        super().closeEvent(event)
        log_event(logger, logging.INFO, "window.closed")

    def confirm_exit_during_copy(self) -> bool:
        answer = QMessageBox.question(
            self,
            "Copy in progress",
            "A copy is still running. Stop it and exit?\n\n"
            "Files already copied are kept. The file being copied is not kept, and nothing "
            "in the destination is left half-written.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _backend_status_text(self) -> str:
        status = self._backend_status
        if status.available:
            return f"Backend: {status.name} {status.version} (read-only)"
        return f"Backend: {status.name} unavailable"

    def _show_about(self) -> None:
        log_line = f"<p>Log file: {self._log_file}</p>" if self._log_file else ""
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>Reads APFS volumes without modifying them and copies files to a "
            "folder you choose.</p>"
            f"<p>{self._backend_status_text()}<br>{self._backend_status.message}</p>"
            "<p>Uses Qt for Python (PySide6, LGPL-3.0) and libfsapfs "
            "(LGPL-3.0-or-later).</p>"
            f"{log_line}",
        )


def _unavailable_factory(status: BackendAvailability) -> BackendFactory:
    from ..backends.registry import default_registry

    def factory():  # noqa: ANN202
        return default_registry().create(status.name)

    return factory
