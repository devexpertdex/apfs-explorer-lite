from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from apfs_explorer_lite import APP_NAME
from apfs_explorer_lite.backends.base import BackendAvailability
from apfs_explorer_lite.disks.discovery import DiscoveryResult, WindowsDiskDiscovery
from apfs_explorer_lite.errors import DiskAccessDeniedError, DiskDiscoveryError
from apfs_explorer_lite.logging_config import configure_logging
from apfs_explorer_lite.models import (
    ApfsContainerCandidate,
    ApfsIdentification,
    Partition,
    PartitionStyle,
    PhysicalDisk,
)

from .win_structs import FakeDiskApi, apfs_disk_layout, build_nx_superblock, make_disk

APFS_OFFSET = 209_735_680
STATUS = BackendAvailability("libfsapfs", True, "20261030", "loaded")


def test_configure_logging_writes_file_and_is_idempotent(tmp_path: Path) -> None:
    root = logging.getLogger()
    before = len(root.handlers)
    try:
        log_file = configure_logging(tmp_path)
        after_first = len(root.handlers)
        configure_logging(tmp_path)
        assert len(root.handlers) == after_first

        logging.getLogger("test").info("hello log")
        for handler in root.handlers:
            handler.flush()
        assert "hello log" in log_file.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers[before:]:
            root.removeHandler(handler)
            handler.close()


# --- helpers ------------------------------------------------------------------


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    from PySide6.QtWidgets import QApplication

    # time.sleep releases the GIL so the backend worker thread can run;
    # QTest.qWait does not, which makes libfsapfs callbacks crawl.
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        QApplication.processEvents()
        time.sleep(0.002)


def apfs_api(*, admin: bool = True, read_error: DiskDiscoveryError | None = None) -> FakeDiskApi:
    return FakeDiskApi(
        {
            0: make_disk(bus_type=0x11, model=("", "Internal NVMe")),
            2: make_disk(
                layout=apfs_disk_layout(APFS_OFFSET),
                content={APFS_OFFSET: build_nx_superblock()},
                read_error=read_error,
            ),
        },
        admin=admin,
    )


def make_widget(scan: Callable[[], DiscoveryResult], **kwargs: object):
    from apfs_explorer_lite.gui.drive_selection import DriveSelectionWidget

    kwargs.setdefault("is_admin", True)
    return DriveSelectionWidget(scan, **kwargs)  # type: ignore[arg-type]


def refresh_and_wait(widget) -> None:  # noqa: ANN001
    results: list[object] = []
    widget.scanFinished.connect(results.append)
    widget.refresh()
    wait_until(lambda: bool(results))


def tree_rows(widget) -> list[tuple[int, str, str]]:  # noqa: ANN001
    """(depth, item text, kind) for every row in the tree."""
    rows: list[tuple[int, str, str]] = []

    def walk(item, depth: int) -> None:  # noqa: ANN001
        rows.append((depth, item.text(0), item.text(1)))
        for i in range(item.childCount()):
            walk(item.child(i), depth + 1)

    for i in range(widget.tree.topLevelItemCount()):
        walk(widget.tree.topLevelItem(i), 0)
    return rows


# --- drive selection screen -------------------------------------------------------


def test_initial_state_does_not_scan(qapp: object) -> None:
    from apfs_explorer_lite.gui.drive_selection import INITIAL_TITLE

    calls: list[int] = []

    def scan() -> DiscoveryResult:
        calls.append(1)
        return DiscoveryResult((), True)

    widget = make_widget(scan)
    qapp.processEvents()  # type: ignore[attr-defined]
    assert calls == []
    assert not widget.showing_tree
    assert widget.message_panel.title_label.text() == INITIAL_TITLE


def test_refresh_lists_disk_partitions_and_container(qapp: object) -> None:
    from apfs_explorer_lite.gui.drive_selection import KIND_CONTAINER, KIND_DISK, KIND_PARTITION

    api = apfs_api()
    widget = make_widget(WindowsDiskDiscovery(api).scan)
    refresh_and_wait(widget)

    assert widget.showing_tree
    assert tree_rows(widget) == [
        (0, "Disk 2: Samsung PSSD T7", KIND_DISK),
        (1, "Partition 1 (EFI System Partition)", KIND_PARTITION),
        (1, "Partition 2", KIND_PARTITION),
        (2, "APFS container", KIND_CONTAINER),
    ]
    disk_item = widget.tree.topLevelItem(0)
    assert disk_item.text(2) == "1.0 TB"
    assert disk_item.text(3) == "USB (external bus) · GPT"
    container_item = disk_item.child(1).child(0)
    assert container_item.text(3) == "Verified APFS"
    assert "Found 1 external APFS drive" in widget.status_label.text()
    assert "1 hidden" in widget.status_label.text()
    assert widget.refresh_button.isEnabled()


def test_selecting_container_shows_details(qapp: object) -> None:
    widget = make_widget(WindowsDiskDiscovery(apfs_api()).scan)
    selections: list[object] = []
    widget.selectionChanged.connect(selections.append)
    refresh_and_wait(widget)

    container_item = widget.tree.topLevelItem(0).child(1).child(0)
    widget.tree.setCurrentItem(container_item)
    selected = widget.selected_container()
    assert isinstance(selected, ApfsContainerCandidate)
    assert selected.identification is ApfsIdentification.CONFIRMED
    assert isinstance(selections[-1], ApfsContainerCandidate)
    assert "Container UUID" in widget.details_label.text()
    assert widget.open_button.isEnabled()  # verified containers can be opened
    opened: list[tuple[object, str]] = []
    widget.openRequested.connect(lambda c, t: opened.append((c, t)))
    widget.open_button.click()
    assert opened == [(selected, "Disk 2: Samsung PSSD T7")]

    widget.tree.setCurrentItem(widget.tree.topLevelItem(0))
    assert widget.selected_container() is None
    assert r"\\.\PhysicalDrive2" in widget.details_label.text()


def test_tentative_container_and_admin_banner(qapp: object) -> None:
    api = apfs_api(admin=False, read_error=DiskAccessDeniedError("denied"))
    widget = make_widget(WindowsDiskDiscovery(api).scan, is_admin=False, elevate=lambda: False)
    assert not widget.admin_banner.isHidden()
    refresh_and_wait(widget)

    container_item = widget.tree.topLevelItem(0).child(1).child(0)
    assert container_item.text(3) == "Tentative APFS"
    assert "administrator" in container_item.toolTip(3)
    widget.tree.setCurrentItem(container_item)
    assert not widget.open_button.isEnabled()  # must be verified first
    assert "administrator" in widget.open_button.toolTip()

    widget.elevate_button.click()
    assert "cancelled or failed" in widget.admin_label.text()


def test_admin_banner_hidden_for_admin(qapp: object) -> None:
    widget = make_widget(lambda: DiscoveryResult((), True), is_admin=True)
    assert widget.admin_banner.isHidden()


def test_no_candidates_shows_empty_state(qapp: object) -> None:
    from apfs_explorer_lite.gui.drive_selection import NONE_FOUND_TITLE

    api = FakeDiskApi({0: make_disk(bus_type=0x11)})
    widget = make_widget(WindowsDiskDiscovery(api).scan)
    refresh_and_wait(widget)
    assert not widget.showing_tree
    assert widget.message_panel.title_label.text() == NONE_FOUND_TITLE
    assert "1 hidden" in widget.status_label.text()


def test_scan_failure_is_shown(qapp: object) -> None:
    from apfs_explorer_lite.gui.drive_selection import SCAN_FAILED_TITLE

    def scan() -> DiscoveryResult:
        raise DiskDiscoveryError("Windows could not list the disks.")

    widget = make_widget(scan)
    refresh_and_wait(widget)
    assert widget.message_panel.title_label.text() == SCAN_FAILED_TITLE
    assert widget.message_panel.body_label.text() == "Windows could not list the disks."
    assert widget.refresh_button.isEnabled()


def test_unexpected_exception_is_shown(qapp: object) -> None:
    def scan() -> DiscoveryResult:
        raise RuntimeError("boom")

    widget = make_widget(scan)
    refresh_and_wait(widget)
    assert "Unexpected error" in widget.message_panel.body_label.text()


def test_warnings_are_displayed(qapp: object) -> None:
    widget = make_widget(lambda: DiscoveryResult((), True, ("Disk 3 was disconnected during the scan and was skipped.",)))
    refresh_and_wait(widget)
    assert not widget.warnings_label.isHidden()
    assert "Disk 3 was disconnected" in widget.warnings_label.text()


def test_selection_survives_refresh_and_reports_disconnect(qapp: object) -> None:
    from apfs_explorer_lite.gui.drive_selection import DISCONNECTED_TEXT

    api = apfs_api()
    widget = make_widget(WindowsDiskDiscovery(api).scan)
    refresh_and_wait(widget)
    widget.tree.setCurrentItem(widget.tree.topLevelItem(0).child(1).child(0))

    refresh_and_wait(widget)  # same disks: selection restored
    assert widget.selected_container() is not None

    del api.disks[2]  # drive unplugged
    refresh_and_wait(widget)
    assert widget.selected_container() is None
    assert widget.details_label.text() == DISCONNECTED_TEXT
    assert not widget.showing_tree


def test_refresh_ignored_while_scanning(qapp: object) -> None:
    import threading

    gate = threading.Event()
    calls: list[int] = []

    def scan() -> DiscoveryResult:
        calls.append(1)
        gate.wait(5)
        return DiscoveryResult((), True)

    widget = make_widget(scan)
    results: list[object] = []
    widget.scanFinished.connect(results.append)
    widget.refresh()
    widget.refresh()
    assert widget.is_scanning and not widget.refresh_button.isEnabled()
    gate.set()
    wait_until(lambda: bool(results))
    assert calls == [1]


# --- main window --------------------------------------------------------------------


def test_main_window(qapp: object) -> None:
    from apfs_explorer_lite.gui.drive_selection import DriveSelectionWidget
    from apfs_explorer_lite.gui.main_window import MainWindow

    window = MainWindow(STATUS, WindowsDiskDiscovery(apfs_api()).scan, is_admin=False)
    try:
        assert window.windowTitle() == APP_NAME
        assert window.pages.currentWidget() is window.drive_selection
        assert isinstance(window.drive_selection, DriveSelectionWidget)
        assert window.backend_label.text() == "Backend: libfsapfs 20261030 (read-only)"
        results: list[object] = []
        window.drive_selection.scanFinished.connect(results.append)
        window.refresh_drives_action.trigger()
        wait_until(lambda: bool(results))
        assert window.drive_selection.showing_tree
    finally:
        window.close()


def test_main_window_admin_title_and_unavailable_backend(qapp: object) -> None:
    from apfs_explorer_lite.gui.main_window import MainWindow

    status = BackendAvailability("libfsapfs", False, None, "missing")
    window = MainWindow(status, lambda: DiscoveryResult((), True), is_admin=True)
    try:
        assert window.windowTitle() == f"{APP_NAME} (Administrator)"
        assert window.backend_label.text() == "Backend: libfsapfs unavailable"
    finally:
        window.close()


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 bytes"),
        (999, "999 bytes"),
        (1000, "1.0 KB"),
        (500_107_862_016, "500.1 GB"),
        (999_995_129_856, "1.0 TB"),
        (2 * 10**12, "2.0 TB"),
    ],
)
def test_format_size(size: int, text: str) -> None:
    from apfs_explorer_lite.gui.formatting import format_size

    assert format_size(size) == text


def test_container_source_device() -> None:
    part = Partition(2, 2, APFS_OFFSET, 10**9, PartitionStyle.GPT, "X", "Apple APFS")
    container = ApfsContainerCandidate(part, ApfsIdentification.CONFIRMED, "ok")
    assert container.to_source_device().device_path == r"\\.\PhysicalDrive2"
    assert PhysicalDisk(2, r"\\.\PhysicalDrive2", "m", "s", 1, 512, "USB").number == 2
