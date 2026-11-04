"""Large-directory performance and GUI responsiveness.

Timings are recorded with ``record_property`` so the test report can show
them. Large folders come from a mock backend (no real APFS image with tens
of thousands of entries exists); per-entry listing cost is measured on the
real macOS-created image.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from apfs_explorer_lite.backends.libfsapfs_backend import LibfsapfsBackend
from apfs_explorer_lite.copying import build_plan
from apfs_explorer_lite.models import ApfsEntry, EntryKind, SourceDevice
from apfs_explorer_lite.source_io import PythonFileSectorReader

from .doubles import InMemoryBackend
from .real_images import DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE, image_path
from .test_explorer import settle, wait_until

ROOT = PurePosixPath("/")
STAMP = datetime(2024, 1, 1, tzinfo=UTC)


def make_entries(folder: PurePosixPath, count: int, folders: int = 0) -> list[ApfsEntry]:
    entries = [ApfsEntry(folder / f"folder {i:05d}", EntryKind.DIRECTORY, 0, STAMP) for i in range(folders)]
    entries += [
        ApfsEntry(folder / f"document {i:06d}.{('pdf', 'docx', 'jpg', 'txt')[i % 4]}", EntryKind.FILE, i * 37, STAMP)
        for i in range(count)
    ]
    return entries


class WideBackend(InMemoryBackend):
    """Mock: one folder with many entries, returned after a simulated drive delay."""

    def __init__(self, entries: list[ApfsEntry], delay: float = 0.0) -> None:
        super().__init__({"/placeholder": b""})
        self.entries = entries
        self.delay = delay

    def list_directory(self, volume_index: int, path: PurePosixPath) -> list[ApfsEntry]:
        time.sleep(self.delay)
        return list(self.entries) if path == ROOT else []


class EventLoopMonitor:
    """Largest gap between 10 ms timer ticks = longest time the window could not respond."""

    def __init__(self) -> None:
        from PySide6.QtCore import QTimer

        self.ticks: list[float] = []
        self.timer = QTimer()
        self.timer.setInterval(10)
        self.timer.timeout.connect(lambda: self.ticks.append(time.monotonic()))

    def __enter__(self) -> EventLoopMonitor:
        self.timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        # Count the time since the last tick too, or a pause right before exit goes unseen.
        self.ticks.append(time.monotonic())
        self.timer.stop()

    @property
    def max_gap(self) -> float:
        return max((b - a for a, b in zip(self.ticks, self.ticks[1:], strict=False)), default=0.0)


def timed(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


# --- large directories ------------------------------------------------------------------------------


def test_real_image_listing_cost_per_entry(record_property) -> None:  # noqa: ANN001
    path = image_path("apfs.dmg")
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(path))
    backend.open(SourceDevice(str(path), DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE))
    try:
        rounds = 200
        count = len(backend.list_directory(0, ROOT))
        seconds = timed(lambda: [backend.list_directory(0, ROOT) for _ in range(rounds)])
    finally:
        backend.close()
    per_entry_ms = seconds / (rounds * count) * 1000
    record_property("metric", f"real APFS image: {per_entry_ms:.3f} ms per directory entry (cached metadata)")
    assert per_entry_ms < 5


def test_file_list_model_with_50000_entries(qapp: object, record_property) -> None:  # noqa: ANN001
    from apfs_explorer_lite.gui.file_model import FileFilterProxy, FileListModel
    from apfs_explorer_lite.gui.file_types import FileTypeProvider

    entries = make_entries(ROOT, 49_000, folders=1_000)
    model = FileListModel(FileTypeProvider())
    proxy = FileFilterProxy()
    proxy.setSourceModel(model)
    proxy.sort(0)
    load = timed(lambda: model.set_entries(entries))
    search = timed(lambda: proxy.set_search("document 0123"))
    clear = timed(lambda: proxy.set_search(""))
    record_property("metric", f"50,000 entries: load+sort {load:.2f}s, search {search:.2f}s, clear {clear:.2f}s")
    assert proxy.rowCount() == 50_000
    assert load < 5 and search < 2 and clear < 5


def test_copy_plan_for_large_tree(tmp_path: Path, record_property) -> None:  # noqa: ANN001
    class TreeBackend(InMemoryBackend):
        def list_directory(self, volume_index: int, path: PurePosixPath) -> list[ApfsEntry]:
            if path == ROOT:
                return make_entries(ROOT, 0, folders=200)
            return make_entries(path, 100)

    backend = TreeBackend({"/x": b""})
    backend.open(SourceDevice("m", 0, 1))
    sources = backend.list_directory(0, ROOT)
    plan_holder = []
    seconds = timed(lambda: plan_holder.append(build_plan(backend, 0, sources)))
    record_property("metric", f"copy plan for 20,000 files in 200 folders: {seconds:.2f}s")
    assert len(plan_holder[0].files) == 20_000
    assert seconds < 5


# --- GUI responsiveness -------------------------------------------------------------------------------


def test_window_responsive_while_large_folder_loads(qapp: object, record_property) -> None:  # noqa: ANN001
    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    backend = WideBackend(make_entries(ROOT, 29_000, folders=1_000), delay=0.5)
    backend.open(SourceDevice("m", 0, 1))
    explorer = ExplorerWidget(lambda: backend)
    explorer.resize(1100, 700)
    explorer.show()  # a visible window also lays out the rows
    with EventLoopMonitor() as monitor:
        explorer.open_container(SourceDevice("memory", 0, 1), "Wide")
        wait_until(lambda: explorer.showing_list, timeout=30)
        settle(explorer)
    explorer.shutdown()
    record_property("metric", f"30,000-entry folder (0.5s drive delay): longest UI pause {monitor.max_gap * 1000:.0f} ms")
    assert explorer.proxy.rowCount() == 30_000
    # Reading happens in the background; only showing 30,000 rows briefly uses the UI thread.
    assert monitor.max_gap < 1.0


def test_window_responsive_during_copy(qapp: object, tmp_path: Path, record_property) -> None:  # noqa: ANN001
    from .test_copy_dialog import Recorder, SlowBackend, start_copy

    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    backend = SlowBackend({"/slow.bin": b""})
    dest = tmp_path / "dest"
    dest.mkdir()
    explorer = ExplorerWidget(lambda: backend, copy_dialog_factory=Recorder(dest).factory())
    explorer.resize(1100, 700)
    explorer.show()
    explorer.open_container(SourceDevice("memory", 0, 1), "Slow")
    wait_until(lambda: explorer.showing_list)
    settle(explorer)
    with EventLoopMonitor() as monitor:
        dialog = start_copy(explorer, "slow.bin")
        deadline = time.monotonic() + 1.5
        wait_until(lambda: time.monotonic() > deadline, timeout=5)
        # Browsing still works during the copy.
        explorer.refresh()
        settle(explorer)
        dialog.cancel()
        wait_until(lambda: dialog.report is not None)
    explorer.shutdown()
    record_property("metric", f"during a copy: longest UI pause {monitor.max_gap * 1000:.0f} ms")
    assert monitor.max_gap < 0.25
    assert explorer.proxy.rowCount() == 1


@pytest.mark.parametrize("count", [2000])
def test_many_small_files_copy_rate(tmp_path: Path, record_property, count: int) -> None:  # noqa: ANN001
    from .test_copy_engine import memory_backend, run

    backend = memory_backend({f"/f/{i:05d}.txt": b"0123456789" for i in range(count)})
    seconds = timed(lambda: run(backend, backend.list_directory(0, ROOT), tmp_path))
    record_property("metric", f"{count} small files: {count / seconds:.0f} files/s (includes flush to disk and read-back)")
    assert sum(1 for _ in (tmp_path / "f").iterdir()) == count


def test_sorting_keeps_selection_and_folders_first(qapp: object) -> None:
    from PySide6.QtCore import QItemSelectionModel, Qt
    from PySide6.QtWidgets import QTreeView

    from apfs_explorer_lite.gui.file_model import COL_NAME, COL_SIZE, ENTRY_ROLE, FileFilterProxy, FileListModel
    from apfs_explorer_lite.gui.file_types import FileTypeProvider

    entries = [
        ApfsEntry(ROOT / "b.txt", EntryKind.FILE, 5, STAMP), ApfsEntry(ROOT / "Zeta", EntryKind.DIRECTORY, 0, STAMP),
        ApfsEntry(ROOT / "a.txt", EntryKind.FILE, 50, STAMP), ApfsEntry(ROOT / "alpha", EntryKind.DIRECTORY, 0, STAMP),
    ]  # fmt: skip
    model = FileListModel(FileTypeProvider())
    proxy = FileFilterProxy()
    proxy.setSourceModel(model)
    view = QTreeView()
    view.setModel(proxy)
    view.setSortingEnabled(True)
    view.sortByColumn(COL_NAME, Qt.SortOrder.AscendingOrder)
    model.set_entries(entries)
    names = lambda: [proxy.index(r, 0).data() for r in range(proxy.rowCount())]  # noqa: E731
    assert names() == ["alpha", "Zeta", "a.txt", "b.txt"]
    view.selectionModel().select(proxy.index(3, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    view.sortByColumn(COL_SIZE, Qt.SortOrder.DescendingOrder)
    assert names() == ["a.txt", "b.txt", "Zeta", "alpha"]
    selected = [i.data(ENTRY_ROLE).name for i in view.selectionModel().selectedRows(0)]
    assert selected == ["b.txt"]
