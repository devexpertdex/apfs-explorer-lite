"""Opt-in end-to-end test on the physical APFS SSD through the real GUI (read-only).

Enable with ``APFS_LIVE_SSD_TESTS=1`` and run as administrator with the SSD attached.
It drives the same widgets a user clicks: Refresh Drives, select the container,
Open, double-click into a folder, Back, select a folder and some files, Copy
Selected, Browse, Start Copy. Only the folder picker is replaced. Each copy is then
compared with a fresh read of the SSD through a separate reader, and Windows'
write counter for the SSD must not change. Nothing here prints or records file names.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from functools import partial
from pathlib import Path

import pytest

from apfs_explorer_lite.copying import JobStatus, build_plan
from apfs_explorer_lite.models import ApfsContainerCandidate, EntryKind

from .test_explorer import settle, wait_until

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("APFS_LIVE_SSD_TESTS") != "1",
    reason="set APFS_LIVE_SSD_TESTS=1 and run as administrator with the APFS SSD attached",
)

MAX_FILE = 20 * 1024 * 1024
MAX_FOLDER_BYTES = 50 * 1024 * 1024
MAX_FOLDER_FILES = 300
USER_ROLE = 0x0100  # Qt.ItemDataRole.UserRole


def sha256_of_stream(stream) -> str:  # noqa: ANN001
    digest = hashlib.sha256()
    while chunk := stream.read(1 << 20):
        digest.update(chunk)
    return digest.hexdigest()


def test_browse_and_copy_from_the_real_ssd_through_the_gui(qapp: object, tmp_path: Path, record_property) -> None:  # noqa: ANN001
    from PySide6.QtCore import QItemSelectionModel
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    from apfs_explorer_lite.backends import DEFAULT_BACKEND, default_registry
    from apfs_explorer_lite.backends.libfsapfs_backend import LibfsapfsBackend
    from apfs_explorer_lite.disks import WindowsDiskDiscovery
    from apfs_explorer_lite.disks.win32 import Win32DiskApi
    from apfs_explorer_lite.gui.copy_dialog import CopyDialog
    from apfs_explorer_lite.gui.file_model import ENTRY_ROLE
    from apfs_explorer_lite.gui.main_window import MainWindow

    api = Win32DiskApi()
    if not api.is_admin():
        pytest.skip("needs administrator rights")
    registry = default_registry()
    window = MainWindow(
        registry.availability(DEFAULT_BACKEND), WindowsDiskDiscovery(api).scan, is_admin=True,
        backend_factory=lambda: registry.create(DEFAULT_BACKEND),
    )  # fmt: skip
    dest = tmp_path / "copied"
    dest.mkdir()
    explorer = window.explorer
    explorer._copy_dialog_factory = partial(CopyDialog, choose_folder=lambda _p, _s: str(dest))

    def rows():  # noqa: ANN202
        return [explorer.proxy.index(r, 0) for r in range(explorer.proxy.rowCount())]

    try:
        # 1. Refresh Drives, select the APFS container, Open.
        window.refresh_drives_action.trigger()
        wait_until(lambda: window.drive_selection.result is not None and not window.drive_selection.is_scanning, 60)
        container_item = None
        iterator = QTreeWidgetItemIterator(window.drive_selection.tree)
        while iterator.value():
            if isinstance(iterator.value().data(0, USER_ROLE), ApfsContainerCandidate):
                container_item = iterator.value()
                break
            iterator += 1
        assert container_item is not None, "no APFS container in the drive list"
        container: ApfsContainerCandidate = container_item.data(0, USER_ROLE)
        disk = container.partition.disk_number
        counters_before = api.query_counters(disk)
        window.drive_selection.tree.setCurrentItem(container_item)
        assert window.drive_selection.open_button.isEnabled()
        window.drive_selection.open_button.click()
        wait_until(lambda: explorer.showing_list, 120)
        settle(explorer)
        root_location = explorer.location
        assert rows(), "the volume's root folder listed nothing"

        # 2. Pick a folder small enough to copy quickly (sized with a read-only plan).
        backend = explorer._backend
        volume = root_location[0]
        folder_index = None
        for index in rows():
            entry = index.data(ENTRY_ROLE)
            if entry.is_directory:
                plan = build_plan(backend, volume, [entry])
                if 0 < len(plan.files) <= MAX_FOLDER_FILES and plan.total_bytes <= MAX_FOLDER_BYTES:
                    folder_index = index
                    break
        assert folder_index is not None, "no folder under 50 MB found in the root folder"
        folder = folder_index.data(ENTRY_ROLE)

        # 3. Browse: double-click into it, check it lists, then Back.
        explorer.file_view.doubleClicked.emit(folder_index)
        wait_until(lambda: explorer.location == (volume, folder.path) and not explorer.is_busy, 60)
        settle(explorer)
        assert explorer.showing_list or "empty" in explorer.message_label.text()
        explorer.back_action.trigger()
        wait_until(lambda: explorer.location == root_location and explorer.showing_list, 60)
        settle(explorer)

        # 4. Select the folder and up to three small files, Copy Selected, Browse, Start Copy.
        current = {i.data(ENTRY_ROLE).path: i for i in rows()}
        picks = [current[folder.path]]
        picks += [i for i in rows() if i.data(ENTRY_ROLE).kind is EntryKind.FILE
                  and 0 < i.data(ENTRY_ROLE).size <= MAX_FILE][:3]  # fmt: skip
        selection = explorer.file_view.selectionModel()
        selection.clearSelection()
        for index in picks:
            selection.select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        sources = explorer.selected_entries()
        expected_files = len(build_plan(backend, volume, sources).files)
        assert explorer.copy_action.isEnabled()
        explorer.copy_action.trigger()
        dialog = explorer.copy_dialog
        dialog.browse_button.click()
        dialog.start_button.click()
        wait_until(lambda: dialog.report is not None, 600)
        report = dialog.report
        assert report.status is JobStatus.COMPLETED, report.stop_reason
        assert len(report.copied) == expected_files and not report.failed
        folder_copies = [r for r in report.copied if folder.path in r.source.parents]
        assert folder_copies, "nothing from the selected folder was copied"
        dialog.accept()
        explorer.close_container()
        settle(explorer)

        # 5. Compare every copy with a fresh read through a separate reader.
        reader = LibfsapfsBackend()
        reader.open(container.to_source_device())
        try:
            for result in report.copied:
                with reader.open_file(volume, result.source) as stream:  # type: ignore[attr-defined]
                    source_hash = sha256_of_stream(stream)
                with open(result.destination, "rb") as copy:
                    assert sha256_of_stream(copy) == source_hash == result.sha256
                assert Path(result.destination).stat().st_size == result.size
                assert Path(result.destination).is_relative_to(dest.resolve())
        finally:
            reader.close()

        counters_after = api.query_counters(disk)
        assert counters_after.writes == counters_before.writes
        assert counters_after.bytes_written == counters_before.bytes_written
        record_property("metric", (
            f"real SSD via GUI: browsed into a folder and back; copied a folder ({len(folder_copies)} files) and "
            f"{len(report.copied) - len(folder_copies)} more files, {report.bytes_copied:,} bytes, all re-verified; "
            f"{counters_after.bytes_read - counters_before.bytes_read:,} bytes read, 0 written"
        ))  # fmt: skip
    finally:
        window.close()
        shutil.rmtree(dest, ignore_errors=True)  # these are copies of the owner's private files
