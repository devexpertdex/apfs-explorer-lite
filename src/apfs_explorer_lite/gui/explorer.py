"""Read-only file explorer for an opened APFS container.

Everything shown comes from the APFS backend. All backend calls run on one
background thread (TaskRunner); results that arrive after the user has moved
on are ignored.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import PurePosixPath

from PySide6.QtCore import QItemSelection, QModelIndex, QSettings, Qt, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QKeySequence, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..backends.base import ApfsBackend
from ..errors import (
    ApfsExplorerError,
    ApfsVolumeLockedError,
    BackendNotOpenError,
    DiskDisconnectedError,
)
from ..models import APFS_ROOT, ApfsEntry, ApfsVolumeInfo, EntryKind, SourceDevice
from ..structured_log import error_fields, log_event
from .copy_dialog import CopyDialog
from .file_model import (
    COL_MODIFIED,
    COL_NAME,
    COL_SIZE,
    COL_TYPE,
    ENTRY_ROLE,
    FileFilterProxy,
    FileListModel,
    is_hidden,
)
from .file_types import FileTypeProvider
from .formatting import format_size
from .theme import banner_label
from .workers import TaskRunner

logger = logging.getLogger(__name__)

BackendFactory = Callable[[], ApfsBackend]
CopyDialogFactory = Callable[..., CopyDialog]
Location = tuple[int, PurePosixPath]

VOLUME_ROLE = Qt.ItemDataRole.UserRole + 10
PATH_ROLE = Qt.ItemDataRole.UserRole + 11
LOADED_ROLE = Qt.ItemDataRole.UserRole + 12
PLACEHOLDER_ROLE = Qt.ItemDataRole.UserRole + 13

COPY_TOOLTIP = "Copy the selected files and folders to a folder on this PC"
COPY_SELECT_HINT = "Select files or folders to copy them to this PC"
COPY_BUSY_HINT = "A copy is already in progress"
RECOVERABLE_NOTICE = (
    "This drive's first block is damaged. It is being read using an intact backup copy "
    "of its APFS superblock. Nothing is written to the drive."
)


def _icon(theme: QIcon.ThemeIcon, fallback: QStyle.StandardPixmap, style: QStyle) -> QIcon:
    icon = QIcon.fromTheme(theme)
    return icon if not icon.isNull() else style.standardIcon(fallback)


def _drive_lost(error: ApfsExplorerError) -> bool:
    return isinstance(error, (DiskDisconnectedError, BackendNotOpenError))


class BreadcrumbBar(QWidget):
    """Clickable path segments: Volume › Folder › Subfolder."""

    segmentClicked = Signal(object)  # PurePosixPath

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("breadcrumb")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 0, 4, 0)
        self._layout.setSpacing(0)
        self._buttons: list[QToolButton] = []

    @property
    def segments(self) -> list[str]:
        return [b.text() for b in self._buttons]

    def set_location(self, volume_name: str, path: PurePosixPath) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons.clear()
        targets = [(volume_name, APFS_ROOT)]
        current = APFS_ROOT
        for part in path.parts[1:]:
            current = current / part
            targets.append((part, current))
        for index, (label, target) in enumerate(targets):
            if index:
                separator = QLabel("›")
                separator.setContentsMargins(4, 0, 4, 0)
                self._layout.addWidget(separator)
            button = QToolButton()
            button.setText(label)
            button.setAutoRaise(True)
            button.clicked.connect(lambda _checked=False, t=target: self.segmentClicked.emit(t))
            self._layout.addWidget(button)
            self._buttons.append(button)
        self._layout.addStretch(1)

    def click_segment(self, index: int) -> None:
        self._buttons[index].click()


class ExplorerWidget(QWidget):
    closed = Signal()  # user went back to the drive list
    failed = Signal(object)  # ApfsExplorerError: open failed or the drive was lost
    statusChanged = Signal(str)

    def __init__(
        self,
        backend_factory: BackendFactory,
        parent: QWidget | None = None,
        *,
        settings: QSettings | None = None,
        copy_dialog_factory: CopyDialogFactory = CopyDialog,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("explorer")
        self._backend_factory = backend_factory
        self._settings = settings
        self._copy_dialog_factory = copy_dialog_factory
        self.copy_dialog: CopyDialog | None = None
        self._backend: ApfsBackend | None = None
        self._runner = TaskRunner(self)
        self._types = FileTypeProvider()
        self._volumes: list[ApfsVolumeInfo] = []
        self._location: Location | None = None
        self._back: list[Location] = []
        self._forward: list[Location] = []
        self._token = 0
        self._show_hidden = False
        self._loading_folder = False

        self._build_toolbar()
        self.breadcrumb = BreadcrumbBar()
        self.breadcrumb.segmentClicked.connect(self._on_breadcrumb)

        self.notice_label = banner_label("notice")

        # Sidebar: volumes as top-level items, folders below them (loaded on expand).
        self.drive_label = QLabel()
        self.drive_label.setObjectName("driveTitle")
        font = self.drive_label.font()
        font.setBold(True)
        self.drive_label.setFont(font)
        self.drive_label.setWordWrap(True)
        self.tree_model = QStandardItemModel(self)
        self.tree = QTreeView()
        self.tree.setObjectName("folderTree")
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.expanded.connect(self._on_tree_expanded)
        self.tree.clicked.connect(self._on_tree_clicked)
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(self.drive_label)
        sidebar_layout.addWidget(self.tree, 1)

        # Main panel: file list, or a message (loading, empty, error).
        self.file_model = FileListModel(self._types, self)
        self.proxy = FileFilterProxy(self)
        self.proxy.setSourceModel(self.file_model)
        self.file_view = QTreeView()
        self.file_view.setObjectName("fileList")
        self.file_view.setModel(self.proxy)
        self.file_view.setRootIsDecorated(False)
        self.file_view.setItemsExpandable(False)
        self.file_view.setUniformRowHeights(True)
        self.file_view.setSortingEnabled(True)
        self.file_view.sortByColumn(COL_NAME, Qt.SortOrder.AscendingOrder)
        self.file_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_view.setAlternatingRowColors(True)
        header = self.file_view.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for column, width in ((COL_TYPE, 190), (COL_SIZE, 100), (COL_MODIFIED, 150)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(column, width)
        self.file_view.doubleClicked.connect(self._on_double_clicked)
        self.file_view.activated.connect(self._on_double_clicked)
        self.file_view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.context_copy_action = QAction("Copy to this PC…", self)
        self.context_copy_action.triggered.connect(self.copy_selected)
        self.file_view.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self.file_view.addAction(self.context_copy_action)

        self.message_label = QLabel()
        self.message_label.setObjectName("message")
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setWordWrap(True)
        self.content = QStackedWidget()
        self.content.addWidget(self.file_view)
        self.content.addWidget(self.message_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(sidebar)
        splitter.addWidget(self.content)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([260, 780])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.breadcrumb)
        layout.addWidget(self.notice_label)
        layout.addWidget(splitter, 1)

        # Status-bar widgets; the main window places them in its status bar.
        self.status_label = QLabel()
        self.status_label.setObjectName("explorerStatus")
        self.busy_bar = QProgressBar()
        self.busy_bar.setObjectName("busy")
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setMaximumWidth(120)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.hide()
        self._runner.busyChanged.connect(self.busy_bar.setVisible)

        self._update_actions()

    # --- toolbar --------------------------------------------------------------

    def _build_toolbar(self) -> None:
        style = self.style()
        self.toolbar = QToolBar("Navigation")
        self.toolbar.setObjectName("explorerToolbar")
        self.toolbar.setMovable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        def action(text: str, theme: QIcon.ThemeIcon, fallback: QStyle.StandardPixmap,
                   shortcut: QKeySequence | QKeySequence.StandardKey | None, slot: Callable[[], None]) -> QAction:  # fmt: skip
            act = QAction(_icon(theme, fallback, style), text, self)
            if shortcut is not None:
                act.setShortcut(shortcut)
                act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            act.triggered.connect(slot)
            self.toolbar.addAction(act)
            return act

        SP = QStyle.StandardPixmap
        TI = QIcon.ThemeIcon
        self.drives_action = action("Drives", TI.DriveHarddisk, SP.SP_DriveHDIcon, None, self.close_container)
        self.drives_action.setToolTip("Close this drive and go back to the drive list")
        self.toolbar.addSeparator()
        self.back_action = action("Back", TI.GoPrevious, SP.SP_ArrowBack, QKeySequence.StandardKey.Back, self.go_back)
        self.forward_action = action("Forward", TI.GoNext, SP.SP_ArrowForward, QKeySequence.StandardKey.Forward, self.go_forward)  # fmt: skip
        self.up_action = action("Up", TI.GoUp, SP.SP_ArrowUp, QKeySequence("Alt+Up"), self.go_up)
        self.refresh_action = action("Refresh", TI.ViewRefresh, SP.SP_BrowserReload, QKeySequence.StandardKey.Refresh, self.refresh)  # fmt: skip
        self.toolbar.addSeparator()
        self.copy_action = action("Copy Selected", TI.EditCopy, SP.SP_DialogSaveButton, None, self.copy_selected)
        self.copy_action.setEnabled(False)
        self.copy_action.setToolTip(COPY_SELECT_HINT)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(spacer)
        self.search_box = QLineEdit()
        self.search_box.setObjectName("search")
        self.search_box.setPlaceholderText("Search this folder")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMaximumWidth(260)
        self.search_box.addAction(_icon(TI.EditFind, SP.SP_FileDialogContentsView, style), QLineEdit.ActionPosition.LeadingPosition)  # fmt: skip
        self.search_box.textChanged.connect(self._on_search)
        self.toolbar.addWidget(self.search_box)
        find = QAction(self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find.triggered.connect(lambda: (self.search_box.setFocus(), self.search_box.selectAll()))
        self.addAction(find)

    # --- public API -----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._backend is not None

    @property
    def is_busy(self) -> bool:
        return self._runner.is_busy

    @property
    def location(self) -> Location | None:
        return self._location

    @property
    def volumes(self) -> list[ApfsVolumeInfo]:
        return list(self._volumes)

    def open_container(self, source: SourceDevice, title: str, *, damaged_first_block: bool = False) -> None:
        """Open ``source`` in the background, then show its first unlocked volume."""
        self._reset()
        self.drive_label.setText(title)
        self.notice_label.setText(RECOVERABLE_NOTICE)
        self.notice_label.setVisible(damaged_first_block)
        self._show_message("Opening the drive…")
        self._set_status("Opening the drive…")
        try:
            backend = self._backend_factory()
        except ApfsExplorerError as exc:
            self.failed.emit(exc)
            return
        token = self._next_token()

        def open_and_list() -> list[ApfsVolumeInfo]:
            backend.open(source)
            try:
                return backend.list_volumes()
            except BaseException:
                backend.close()
                raise

        def on_opened(volumes: list[ApfsVolumeInfo]) -> None:
            if token != self._token:
                self._runner.run(backend.close, lambda _r: None, lambda _e: None)
                return
            self._backend = backend
            self._volumes = volumes
            self._populate_volumes()
            unlocked = [v for v in volumes if not v.is_locked]
            if unlocked:
                self.navigate(unlocked[0].index, APFS_ROOT, record=False)
            else:
                self._show_message("All volumes on this drive are encrypted. Encrypted volumes are not supported.")
                self._set_status(f"{len(volumes)} volume(s), all encrypted")

        def on_failed(error: ApfsExplorerError) -> None:
            if token == self._token:
                log_event(logger, logging.WARNING, "explorer.open_failed", **error_fields(error))
                self.failed.emit(error)

        self._runner.run(open_and_list, on_opened, on_failed)

    @Slot()
    def close_container(self) -> None:
        if self.copy_running:
            self._set_status("Finish or cancel the copy before closing the drive.")
            return
        self._release_backend()
        self.closed.emit()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Stop any copy, close the backend and wait for background work (used on app exit)."""
        if self.copy_dialog is not None:
            self.copy_dialog.cancel_and_wait(timeout_ms / 1000)
        self._release_backend()
        self._runner.wait(timeout_ms)

    # --- copying ----------------------------------------------------------------

    @property
    def copy_running(self) -> bool:
        return self.copy_dialog is not None and self.copy_dialog.is_running

    @Slot()
    def copy_selected(self) -> None:
        """Open the copy dialog for the selection (only one copy dialog at a time)."""
        if self.copy_dialog is not None:
            self.copy_dialog.show()
            self.copy_dialog.raise_()
            self.copy_dialog.activateWindow()
            return
        entries = self.selected_entries()
        if self._backend is None or self._location is None or not entries:
            return
        volume = self._location[0]
        dialog = self._copy_dialog_factory(
            self._backend, volume, self._volume_name(volume), entries, settings=self._settings, parent=self
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._on_copy_dialog_closed)
        dialog.copyStarted.connect(self._update_actions)
        dialog.copyFinished.connect(lambda _report: self._update_actions())
        self.copy_dialog = dialog
        self._update_actions()
        dialog.show()

    @Slot()
    def _on_copy_dialog_closed(self) -> None:
        self.copy_dialog = None
        self._update_actions()

    def set_show_hidden(self, show: bool) -> None:
        self._show_hidden = show
        self.proxy.set_show_hidden(show)
        self._populate_volumes()
        if self._location is not None:
            self._select_tree_item(*self._location)
        self._update_status()

    def navigate(self, volume_index: int, path: PurePosixPath, *, record: bool = True) -> None:
        if self._backend is None:
            return
        volume = self._volume(volume_index)
        if volume is None:
            return
        if volume.is_locked:
            self._show_message(f"The volume “{volume.name}” is encrypted. Encrypted volumes are not supported.")
            return
        if record and self._location is not None and self._location != (volume_index, path):
            self._back.append(self._location)
            self._forward.clear()
        self._location = (volume_index, path)
        self.search_box.blockSignals(True)
        self.search_box.clear()
        self.search_box.blockSignals(False)
        self.proxy.set_search("")
        self.breadcrumb.set_location(volume.name, path)
        self._select_tree_item(volume_index, path)
        self._update_actions()
        self._load_folder()

    @Slot()
    def go_back(self) -> None:
        if self._back:
            assert self._location is not None
            self._forward.append(self._location)
            self.navigate(*self._back.pop(), record=False)

    @Slot()
    def go_forward(self) -> None:
        if self._forward:
            assert self._location is not None
            self._back.append(self._location)
            self.navigate(*self._forward.pop(), record=False)

    @Slot()
    def go_up(self) -> None:
        if self._location is not None and self._location[1] != APFS_ROOT:
            volume, path = self._location
            self.navigate(volume, path.parent)

    @Slot()
    def refresh(self) -> None:
        if self._location is not None:
            self._populate_volumes()
            self._select_tree_item(*self._location)
            self._load_folder()

    def selected_entries(self) -> list[ApfsEntry]:
        rows = self.file_view.selectionModel().selectedRows(COL_NAME)
        return [index.data(ENTRY_ROLE) for index in rows]

    # --- folder loading -------------------------------------------------------

    def _load_folder(self) -> None:
        assert self._backend is not None and self._location is not None
        backend = self._backend
        volume, path = self._location
        token = self._next_token()
        self._loading_folder = True
        self.file_model.clear()
        self._show_message("Loading…")
        self._update_status()

        def on_loaded(entries: list[ApfsEntry]) -> None:
            if token != self._token:
                return
            self._loading_folder = False
            self.file_model.set_entries(entries)
            self._fill_tree_children(volume, path, entries)
            self._show_list_or_empty()
            self._update_status()

        def on_failed(error: ApfsExplorerError) -> None:
            if token != self._token:
                return
            self._loading_folder = False
            self._handle_error(error, f"Couldn't open “{path.name or self._volume_name(volume)}”.")

        self._runner.run(lambda: backend.list_directory(volume, path), on_loaded, on_failed)

    def _handle_error(self, error: ApfsExplorerError, context: str) -> None:
        log_event(logger, logging.WARNING, "explorer.load_failed", **error_fields(error))
        if _drive_lost(error):
            self._release_backend()
            self.failed.emit(error)
            return
        if isinstance(error, ApfsVolumeLockedError):
            self._show_message(error.user_message)
        else:
            self._show_message(f"{context}\n\n{error.user_message}")
        self._set_status(context)

    # --- sidebar tree -----------------------------------------------------------

    def _populate_volumes(self) -> None:
        self.tree_model.clear()
        drive_icon = _icon(QIcon.ThemeIcon.DriveHarddisk, QStyle.StandardPixmap.SP_DriveHDIcon, self.style())
        for volume in self._volumes:
            item = QStandardItem(drive_icon, volume.name)
            item.setEditable(False)
            item.setData(volume.index, VOLUME_ROLE)
            item.setData(str(APFS_ROOT), PATH_ROLE)
            if volume.is_locked:
                item.setText(f"{volume.name} (encrypted)")
                item.setEnabled(False)
                item.setToolTip("Encrypted volumes are not supported.")
            else:
                item.setToolTip(f"APFS volume {volume.identifier}")
                self._add_placeholder(item)
            self.tree_model.appendRow(item)

    @staticmethod
    def _add_placeholder(item: QStandardItem) -> None:
        placeholder = QStandardItem("Loading…")
        placeholder.setEditable(False)
        placeholder.setEnabled(False)
        placeholder.setData(True, PLACEHOLDER_ROLE)
        item.appendRow(placeholder)
        item.setData(False, LOADED_ROLE)

    def _tree_item(self, volume: int, path: PurePosixPath) -> QStandardItem | None:
        item = next(
            (self.tree_model.item(r) for r in range(self.tree_model.rowCount())
             if self.tree_model.item(r).data(VOLUME_ROLE) == volume),
            None,
        )  # fmt: skip
        for part in path.parts[1:]:
            if item is None:
                return None
            item = next(
                (item.child(r) for r in range(item.rowCount())
                 if item.child(r).data(PATH_ROLE) and PurePosixPath(item.child(r).data(PATH_ROLE)).name == part),
                None,
            )  # fmt: skip
        return item

    def _select_tree_item(self, volume: int, path: PurePosixPath) -> None:
        item = self._tree_item(volume, path)
        if item is not None:
            index = item.index()
            self.tree.setCurrentIndex(index)
            self.tree.scrollTo(index)

    def _fill_tree_children(self, volume: int, path: PurePosixPath, entries: list[ApfsEntry]) -> None:
        item = self._tree_item(volume, path)
        if item is None or item.data(LOADED_ROLE):
            return
        item.removeRows(0, item.rowCount())
        item.setData(True, LOADED_ROLE)
        folder_icon = self._types.folder_icon()
        folders = sorted(
            (e for e in entries if e.kind is EntryKind.DIRECTORY and (self._show_hidden or not is_hidden(e))),
            key=lambda e: e.name.casefold(),
        )
        for entry in folders:
            child = QStandardItem(folder_icon, entry.name)
            child.setEditable(False)
            child.setData(volume, VOLUME_ROLE)
            child.setData(str(entry.path), PATH_ROLE)
            self._add_placeholder(child)
            item.appendRow(child)

    @Slot(QModelIndex)
    def _on_tree_expanded(self, index: QModelIndex) -> None:
        item = self.tree_model.itemFromIndex(index)
        if item is None or item.data(LOADED_ROLE) or self._backend is None:
            return
        backend = self._backend
        volume = item.data(VOLUME_ROLE)
        path = PurePosixPath(item.data(PATH_ROLE))

        def on_loaded(entries: list[ApfsEntry]) -> None:
            if self._backend is backend:
                self._fill_tree_children(volume, path, entries)
                self.tree.expand(self._tree_item(volume, path).index())  # type: ignore[union-attr]

        def on_failed(error: ApfsExplorerError) -> None:
            if self._backend is not backend:
                return
            if _drive_lost(error):
                self._handle_error(error, "The drive is no longer available.")
                return
            target = self._tree_item(volume, path)
            if target is not None and target.rowCount():
                target.child(0).setText("Couldn't read this folder")
                target.child(0).setToolTip(error.user_message)

        self._runner.run(lambda: backend.list_directory(volume, path), on_loaded, on_failed)

    @Slot(QModelIndex)
    def _on_tree_clicked(self, index: QModelIndex) -> None:
        item = self.tree_model.itemFromIndex(index)
        if item is None or item.data(PLACEHOLDER_ROLE) or not item.isEnabled():
            return
        location = (item.data(VOLUME_ROLE), PurePosixPath(item.data(PATH_ROLE)))
        if location != self._location:
            self.navigate(*location)

    # --- file list --------------------------------------------------------------

    @Slot(QModelIndex)
    def _on_double_clicked(self, index: QModelIndex) -> None:
        entry: ApfsEntry | None = index.siblingAtColumn(COL_NAME).data(ENTRY_ROLE)
        if entry is None or self._location is None:
            return
        if entry.kind is EntryKind.DIRECTORY:
            self.navigate(self._location[0], entry.path)
        elif entry.kind is EntryKind.SYMLINK:
            self._set_status(f"“{entry.name}” is a shortcut (symbolic link) to {entry.symlink_target}; it isn't followed.")
        else:
            self._set_status("Files can't be opened here. Select them and click Copy Selected to save them to this PC.")

    @Slot(QItemSelection, QItemSelection)
    def _on_selection_changed(self, *_args: object) -> None:
        self._update_status()
        self._update_actions()

    @Slot(str)
    def _on_search(self, text: str) -> None:
        self.proxy.set_search(text)
        self._show_list_or_empty()
        self._update_status()

    @Slot(object)
    def _on_breadcrumb(self, path: PurePosixPath) -> None:
        if self._location is not None and path != self._location[1]:
            self.navigate(self._location[0], path)

    # --- helpers -------------------------------------------------------------------

    def _next_token(self) -> int:
        self._token += 1
        return self._token

    def _volume(self, index: int) -> ApfsVolumeInfo | None:
        return next((v for v in self._volumes if v.index == index), None)

    def _volume_name(self, index: int) -> str:
        volume = self._volume(index)
        return volume.name if volume else f"Volume {index + 1}"

    def _show_message(self, text: str) -> None:
        self.message_label.setText(text)
        self.content.setCurrentWidget(self.message_label)

    def _show_list_or_empty(self) -> None:
        if self._loading_folder:
            return
        if self.proxy.rowCount():
            self.content.setCurrentWidget(self.file_view)
        elif self.proxy.search_text:
            self._show_message(f"No items in this folder match “{self.search_box.text()}”.")
        else:
            self._show_message("This folder is empty.")

    @property
    def showing_list(self) -> bool:
        return self.content.currentWidget() is self.file_view

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.statusChanged.emit(text)

    def _update_status(self) -> None:
        if self._loading_folder:
            self._set_status("Loading folder…")
            return
        shown = self.proxy.rowCount()
        hidden = sum(1 for e in self.file_model.entries if is_hidden(e)) if not self._show_hidden else 0
        total = self.file_model.rowCount() - hidden
        parts = [f"{shown} item{'s' if shown != 1 else ''}"]
        if self.proxy.search_text:
            parts[0] += f" (of {total}, filtered)"
        elif hidden:
            parts.append(f"{hidden} hidden")
        selected = self.selected_entries()
        if selected:
            size = sum(e.size for e in selected if e.kind is EntryKind.FILE)
            parts.append(f"{len(selected)} selected ({format_size(size)})")
        self._set_status("   |   ".join(parts))

    def _update_actions(self) -> None:
        has_location = self._location is not None
        self.back_action.setEnabled(bool(self._back))
        self.forward_action.setEnabled(bool(self._forward))
        self.up_action.setEnabled(has_location and self._location[1] != APFS_ROOT)  # type: ignore[index]
        self.refresh_action.setEnabled(has_location)
        self.search_box.setEnabled(has_location)
        running = self.copy_running
        self.drives_action.setEnabled(not running)
        has_selection = self._backend is not None and has_location and self.file_view.selectionModel().hasSelection()
        can_copy = has_selection and self.copy_dialog is None
        self.copy_action.setEnabled(can_copy)
        self.context_copy_action.setEnabled(can_copy)
        if running:
            self.copy_action.setToolTip(COPY_BUSY_HINT)
        else:
            self.copy_action.setToolTip(COPY_TOOLTIP if has_selection else COPY_SELECT_HINT)

    def _reset(self) -> None:
        self._next_token()
        self._release_backend()
        self._volumes = []
        self._location = None
        self._back.clear()
        self._forward.clear()
        self._loading_folder = False
        self.tree_model.clear()
        self.file_model.clear()
        self.breadcrumb.set_location("", APFS_ROOT)
        self._update_actions()

    def _release_backend(self) -> None:
        self._next_token()
        backend, self._backend = self._backend, None
        if backend is not None:
            self._runner.run(backend.close, lambda _r: None, lambda e: logger.warning("Close failed: %s", e))
