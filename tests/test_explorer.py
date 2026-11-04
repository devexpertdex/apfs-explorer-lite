"""Explorer GUI tests against real, macOS-created APFS images (offscreen)."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

import pytest

from apfs_explorer_lite.backends.base import BackendAvailability
from apfs_explorer_lite.backends.libfsapfs_backend import LibfsapfsBackend
from apfs_explorer_lite.disks.discovery import DiscoveryResult
from apfs_explorer_lite.disks.recovery import find_latest_superblock
from apfs_explorer_lite.errors import (
    ApfsContainerOpenError,
    ApfsReadError,
    BackendUnavailableError,
    DiskDisconnectedError,
)
from apfs_explorer_lite.models import (
    ApfsContainerCandidate,
    ApfsEntry,
    ApfsIdentification,
    EntryKind,
    Partition,
    PartitionStyle,
    SourceDevice,
)
from apfs_explorer_lite.source_io import PythonFileSectorReader

from .real_images import DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE, image_path

ROOT_VISIBLE = ["a_directory", "a_link", "passwords.txt"]


def wait_until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    from PySide6.QtWidgets import QApplication

    # time.sleep releases the GIL so the backend worker thread can run;
    # QTest.qWait does not, which makes libfsapfs callbacks crawl.
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        QApplication.processEvents()
        time.sleep(0.002)


def dmg_source(path: Path, superblock_offset: int | None = None) -> SourceDevice:
    return SourceDevice(str(path), DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE, "test", superblock_offset)


def names(explorer) -> list[str]:  # noqa: ANN001
    proxy = explorer.proxy
    return [proxy.index(r, 0).data() for r in range(proxy.rowCount())]


def column(explorer, name: str, col: int) -> str:  # noqa: ANN001
    proxy = explorer.proxy
    row = next(r for r in range(proxy.rowCount()) if proxy.index(r, 0).data() == name)
    return proxy.index(row, col).data()


def settle(explorer) -> None:  # noqa: ANN001
    wait_until(lambda: not explorer.is_busy)


class ControlledBackend(LibfsapfsBackend):
    """The real backend; tests can make one call fail to simulate faults."""

    def __init__(self, path: Path) -> None:
        super().__init__(lambda _s: PythonFileSectorReader(path))
        self.fail_listing: dict[PurePosixPath | None, Exception] = {}

    def list_directory(self, volume_index: int, path: PurePosixPath) -> list[ApfsEntry]:
        error = self.fail_listing.get(path) or self.fail_listing.get(None)
        if error is not None:
            raise error
        return super().list_directory(volume_index, path)


@pytest.fixture
def opened(qapp: object) -> Iterator[tuple[object, ControlledBackend]]:
    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    backend = ControlledBackend(image_path("apfs.dmg"))
    explorer = ExplorerWidget(lambda: backend)
    failures: list[object] = []
    explorer.failed.connect(failures.append)
    explorer.open_container(dmg_source(image_path("apfs.dmg")), "Disk 9: Test image")
    wait_until(lambda: explorer.showing_list or failures)
    assert not failures, failures
    settle(explorer)
    yield explorer, backend
    explorer.shutdown()


# --- opening and listing ---------------------------------------------------------------


def test_open_lists_real_root_folder(opened) -> None:  # noqa: ANN001
    explorer, _ = opened
    assert names(explorer) == ROOT_VISIBLE  # folders first; .fseventsd hidden
    assert explorer.breadcrumb.segments == ["SingleVolume"]
    assert explorer.drive_label.text() == "Disk 9: Test image"
    assert [v.name for v in explorer.volumes] == ["SingleVolume"]
    assert explorer.tree_model.item(0).text() == "SingleVolume"
    assert "3 items" in explorer.status_label.text() and "1 hidden" in explorer.status_label.text()
    assert not explorer.notice_label.isVisibleTo(explorer)


def test_columns_types_sizes_and_icons(opened) -> None:  # noqa: ANN001
    from apfs_explorer_lite.gui.file_model import COL_MODIFIED, COL_SIZE, COL_TYPE
    from PySide6.QtCore import Qt

    explorer, _ = opened
    assert column(explorer, "a_directory", COL_TYPE) == "File folder"
    assert column(explorer, "a_link", COL_TYPE) == "Symbolic link"
    assert column(explorer, "passwords.txt", COL_TYPE) not in ("", "File folder")
    assert column(explorer, "passwords.txt", COL_SIZE) == "116 bytes"
    assert column(explorer, "a_directory", COL_SIZE) == ""
    assert column(explorer, "passwords.txt", COL_MODIFIED)  # local date/time text
    icon = explorer.proxy.index(0, 0).data(Qt.ItemDataRole.DecorationRole)
    assert icon is not None and not icon.isNull()
    assert [explorer.proxy.headerData(c, Qt.Orientation.Horizontal) for c in range(4)] == [
        "Name", "Type", "Size", "Date Modified",
    ]  # fmt: skip


# --- navigation -------------------------------------------------------------------------


def double_click(explorer, name: str) -> None:  # noqa: ANN001
    proxy = explorer.proxy
    row = next(r for r in range(proxy.rowCount()) if proxy.index(r, 0).data() == name)
    explorer.file_view.doubleClicked.emit(proxy.index(row, 0))
    settle(explorer)


def test_double_click_back_forward_up_and_breadcrumb(opened) -> None:  # noqa: ANN001
    explorer, _ = opened
    assert not explorer.back_action.isEnabled() and not explorer.up_action.isEnabled()

    double_click(explorer, "a_directory")
    assert names(explorer) == ["a_file", "another_file"]
    assert explorer.breadcrumb.segments == ["SingleVolume", "a_directory"]
    assert explorer.location == (0, PurePosixPath("/a_directory"))
    assert explorer.back_action.isEnabled() and explorer.up_action.isEnabled()

    explorer.go_back()
    settle(explorer)
    assert names(explorer) == ROOT_VISIBLE
    assert explorer.forward_action.isEnabled()

    explorer.go_forward()
    settle(explorer)
    assert names(explorer) == ["a_file", "another_file"]

    explorer.go_up()
    settle(explorer)
    assert explorer.location == (0, PurePosixPath("/"))

    double_click(explorer, "a_directory")
    explorer.breadcrumb.click_segment(0)
    settle(explorer)
    assert explorer.location == (0, PurePosixPath("/"))


def test_double_click_file_and_symlink_do_not_navigate(opened) -> None:  # noqa: ANN001
    explorer, _ = opened
    double_click(explorer, "passwords.txt")
    assert "can't be opened" in explorer.status_label.text()
    double_click(explorer, "a_link")
    assert "a_directory/a_file" in explorer.status_label.text()
    assert explorer.location == (0, PurePosixPath("/"))


def test_refresh_reloads(opened) -> None:  # noqa: ANN001
    explorer, _ = opened
    explorer.refresh_action.trigger()
    settle(explorer)
    assert names(explorer) == ROOT_VISIBLE


# --- sorting, search, selection, hidden items ------------------------------------------------


def test_sorting(opened) -> None:  # noqa: ANN001
    from apfs_explorer_lite.gui.file_model import COL_NAME, COL_SIZE, COL_TYPE
    from PySide6.QtCore import Qt

    explorer, _ = opened
    explorer.file_view.sortByColumn(COL_NAME, Qt.SortOrder.DescendingOrder)
    assert names(explorer) == ["passwords.txt", "a_link", "a_directory"]
    explorer.file_view.sortByColumn(COL_SIZE, Qt.SortOrder.DescendingOrder)
    assert names(explorer)[0] == "passwords.txt"
    explorer.file_view.sortByColumn(COL_TYPE, Qt.SortOrder.AscendingOrder)
    assert names(explorer)[0] == "a_directory"  # folders first


def test_search_in_current_folder(opened) -> None:  # noqa: ANN001
    explorer, _ = opened
    explorer.search_box.setText("PASS")
    assert names(explorer) == ["passwords.txt"]
    assert "of 3, filtered" in explorer.status_label.text()
    explorer.search_box.setText("zzz")
    assert not explorer.showing_list
    assert "No items in this folder match" in explorer.message_label.text()
    explorer.search_box.clear()
    assert names(explorer) == ROOT_VISIBLE and explorer.showing_list

    explorer.search_box.setText("pass")
    double_click(explorer, "passwords.txt")
    explorer.search_box.setText("a_")
    double_click(explorer, "a_directory")
    assert explorer.search_box.text() == ""  # navigating clears the search


def test_multi_selection_status(opened) -> None:  # noqa: ANN001
    from PySide6.QtCore import QItemSelectionModel

    explorer, _ = opened
    explorer.file_view.selectAll()
    assert "3 selected (116 bytes)" in explorer.status_label.text()
    selection = explorer.file_view.selectionModel()
    selection.clearSelection()
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    selection.select(explorer.proxy.index(2, 0), flags)
    assert [e.name for e in explorer.selected_entries()] == ["passwords.txt"]
    assert explorer.copy_action.isEnabled()
    selection.clearSelection()
    assert not explorer.copy_action.isEnabled()


def test_hidden_items_toggle(opened) -> None:  # noqa: ANN001
    explorer, _ = opened
    explorer.set_show_hidden(True)
    assert names(explorer) == [".fseventsd", "a_directory", "a_link", "passwords.txt"]
    explorer.set_show_hidden(False)
    assert names(explorer) == ROOT_VISIBLE


# --- folder tree -----------------------------------------------------------------------


def test_tree_loads_lazily_and_navigates(opened) -> None:  # noqa: ANN001
    explorer, _ = opened
    volume = explorer.tree_model.item(0)
    assert [volume.child(r).text() for r in range(volume.rowCount())] == ["a_directory"]
    folder = volume.child(0)
    assert folder.child(0).text() == "Loading…"  # not read until expanded
    explorer.tree.expand(folder.index())
    settle(explorer)
    folder = explorer.tree_model.item(0).child(0)
    assert folder.rowCount() == 0  # a_directory has no subfolders
    explorer.tree.clicked.emit(folder.index())
    settle(explorer)
    assert names(explorer) == ["a_file", "another_file"]


# --- errors -----------------------------------------------------------------------------------


def test_unreadable_folder_shows_message_and_keeps_working(opened) -> None:  # noqa: ANN001
    explorer, backend = opened
    backend.fail_listing[PurePosixPath("/a_directory")] = ApfsReadError("Reading APFS data failed.")
    double_click(explorer, "a_directory")
    assert "Couldn't open “a_directory”" in explorer.message_label.text()
    assert explorer.is_open
    explorer.go_back()
    settle(explorer)
    assert names(explorer) == ROOT_VISIBLE


def test_unplugged_drive_is_reported(opened) -> None:  # noqa: ANN001
    explorer, backend = opened
    failures: list[object] = []
    explorer.failed.connect(failures.append)
    backend.fail_listing[None] = DiskDisconnectedError("The disk is no longer available.")
    explorer.refresh()
    wait_until(lambda: bool(failures))
    assert isinstance(failures[0], DiskDisconnectedError)
    assert not explorer.is_open
    settle(explorer)
    assert not backend.is_open


def test_encrypted_volume(qapp: object) -> None:
    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    path = image_path("apfs_encrypted.dmg")
    explorer = ExplorerWidget(lambda: LibfsapfsBackend(lambda _s: PythonFileSectorReader(path)))
    explorer.open_container(dmg_source(path), "Encrypted image")
    wait_until(lambda: "encrypted" in explorer.message_label.text())
    item = explorer.tree_model.item(0)
    assert "(encrypted)" in item.text() and not item.isEnabled()
    explorer.shutdown()


def test_open_failure_is_reported(qapp: object, tmp_path: Path) -> None:
    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    zeros = tmp_path / "zeros.img"
    zeros.write_bytes(bytes(1 << 20))
    explorer = ExplorerWidget(lambda: LibfsapfsBackend(lambda _s: PythonFileSectorReader(zeros)))
    failures: list[object] = []
    explorer.failed.connect(failures.append)
    explorer.open_container(SourceDevice(str(zeros), 0, 1 << 20), "Not APFS")
    wait_until(lambda: bool(failures))
    assert isinstance(failures[0], ApfsContainerOpenError)
    explorer.shutdown()


def test_backend_unavailable_is_reported(qapp: object) -> None:
    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    def factory() -> LibfsapfsBackend:
        raise BackendUnavailableError("The libfsapfs APFS backend is not available.")

    explorer = ExplorerWidget(factory)
    failures: list[object] = []
    explorer.failed.connect(failures.append)
    explorer.open_container(SourceDevice("x", 0, 1), "Drive")
    assert isinstance(failures[0], BackendUnavailableError)


def test_damaged_first_block_drive(qapp: object, tmp_path: Path) -> None:
    from apfs_explorer_lite.gui.explorer import RECOVERABLE_NOTICE, ExplorerWidget

    damaged = tmp_path / "damaged.dmg"
    shutil.copyfile(image_path("apfs.dmg"), damaged)
    with damaged.open("r+b") as f:  # only the temporary copy is modified
        f.seek(DMG_PARTITION_OFFSET)
        f.write(bytes.fromhex("eb7690") + b"EXFAT   " + bytes(4085))
    reader = PythonFileSectorReader(damaged)
    found = find_latest_superblock(lambda o, n: reader.read_at(DMG_PARTITION_OFFSET + o, n), DMG_PARTITION_SIZE)
    reader.close()
    assert found is not None

    explorer = ExplorerWidget(lambda: LibfsapfsBackend(lambda _s: PythonFileSectorReader(damaged)))
    explorer.open_container(dmg_source(damaged, found.offset), "Damaged", damaged_first_block=True)
    wait_until(lambda: explorer.showing_list)
    assert names(explorer) == ROOT_VISIBLE
    assert explorer.notice_label.text() == RECOVERABLE_NOTICE
    assert explorer.notice_label.isVisibleTo(explorer)
    explorer.shutdown()


# --- main window integration -------------------------------------------------------------------


def _candidate() -> ApfsContainerCandidate:
    partition = Partition(9, 1, DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE, PartitionStyle.GPT,
                          "7C3457EF-0000-11AA-AA11-00306543ECAC", "Apple APFS")  # fmt: skip
    return ApfsContainerCandidate(partition, ApfsIdentification.CONFIRMED, "ok")


STATUS = BackendAvailability("libfsapfs", True, "20261030", "loaded")


def test_main_window_opens_explorer_and_returns(qapp: object) -> None:
    from apfs_explorer_lite.gui.main_window import MainWindow

    path = image_path("apfs.dmg")
    window = MainWindow(
        STATUS, lambda: DiscoveryResult((), True), is_admin=True,
        backend_factory=lambda: LibfsapfsBackend(lambda _s: PythonFileSectorReader(path)),
    )  # fmt: skip
    try:
        window.drive_selection.openRequested.emit(_candidate(), "Disk 9: Test image")
        assert window.showing_explorer
        wait_until(lambda: window.explorer.showing_list)
        assert names(window.explorer) == ROOT_VISIBLE
        window.hidden_action.setChecked(True)
        assert ".fseventsd" in names(window.explorer)
        window.explorer.drives_action.trigger()
        assert not window.showing_explorer
        settle(window.explorer)
        assert not window.explorer.is_open
    finally:
        window.close()


def test_main_window_shows_open_failure_on_drive_list(qapp: object, tmp_path: Path) -> None:
    from apfs_explorer_lite.gui.main_window import MainWindow

    zeros = tmp_path / "zeros.img"
    zeros.write_bytes(bytes(8 << 20))
    window = MainWindow(
        STATUS, lambda: DiscoveryResult((), True), is_admin=True,
        backend_factory=lambda: LibfsapfsBackend(lambda _s: PythonFileSectorReader(zeros)),
    )  # fmt: skip
    try:
        window.drive_selection.openRequested.emit(_candidate(), "Disk 9")
        wait_until(lambda: not window.showing_explorer)
        assert "couldn't be opened" in window.drive_selection.notice_label.text()
    finally:
        window.close()


def test_main_window_backend_unavailable_state(qapp: object) -> None:
    from apfs_explorer_lite.gui.main_window import MainWindow

    status = BackendAvailability("libfsapfs", False, None, "pyfsapfs could not be loaded")
    window = MainWindow(status, lambda: DiscoveryResult((), True), is_admin=True)
    try:
        assert "APFS reader is not available" in window.drive_selection.notice_label.text()
        assert "pyfsapfs could not be loaded" in window.drive_selection.notice_label.text()
        assert not window.drive_selection.open_button.isEnabled()
        assert window.backend_label.text() == "Backend: libfsapfs unavailable"
    finally:
        window.close()


def test_themes(qapp: object) -> None:
    from apfs_explorer_lite.gui.main_window import MainWindow
    from apfs_explorer_lite.gui.theme import ThemeMode
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    # The offscreen test platform has no colour-scheme support; on Windows the
    # scheme really switches (checked by the visual check in the README).
    switches = QGuiApplication.platformName() != "offscreen"
    window = MainWindow(STATUS, lambda: DiscoveryResult((), True), is_admin=True)
    try:
        window.set_theme(ThemeMode.DARK)
        assert window.theme_actions[ThemeMode.DARK].isChecked()
        if switches:
            assert QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        window.set_theme(ThemeMode.LIGHT)
        assert window.theme_actions[ThemeMode.LIGHT].isChecked()
        if switches:
            assert QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Light
    finally:
        window.set_theme(ThemeMode.SYSTEM)
        window.close()


# --- model performance -----------------------------------------------------------------------


def test_large_folder_model_is_fast(qapp: object) -> None:
    """10,000 rows sort and filter quickly (model-level test; nothing is displayed)."""
    from apfs_explorer_lite.gui.file_model import COL_SIZE, FileFilterProxy, FileListModel
    from apfs_explorer_lite.gui.file_types import FileTypeProvider
    from PySide6.QtCore import Qt

    entries = [ApfsEntry(PurePosixPath(f"/f/file{i:05}.txt"), EntryKind.FILE, i) for i in range(10_000)]
    model = FileListModel(FileTypeProvider())
    proxy = FileFilterProxy()
    proxy.setSourceModel(model)
    start = time.perf_counter()
    model.set_entries(entries)
    proxy.sort(COL_SIZE, Qt.SortOrder.DescendingOrder)
    proxy.set_search("file09")
    elapsed = time.perf_counter() - start
    assert proxy.rowCount() == 1000
    assert elapsed < 2.0, elapsed
