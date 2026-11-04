"""Resource cleanup and "no source writes" on real APFS images with real Windows APIs."""

from __future__ import annotations

import ctypes
import gc
import os
import shutil
import stat
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path, PurePosixPath

import pytest
import shiboken6

from apfs_explorer_lite.backends.libfsapfs_backend import LibfsapfsBackend
from apfs_explorer_lite.copying import ConflictPolicy, CopyJob, JobStatus
from apfs_explorer_lite.models import SourceDevice
from apfs_explorer_lite.source_io import PythonFileSectorReader

from .real_images import DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE, PASSWORDS_TXT, image_path, sha256, windows_only

ROOT = PurePosixPath("/")
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3


def _k32():  # noqa: ANN202
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]  # fmt: skip
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    return k32


def can_open_exclusively(path: Path) -> bool:
    """True only if no other handle to ``path`` is open (share mode 0)."""
    k32 = _k32()
    handle = k32.CreateFileW(str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        return False
    k32.CloseHandle(handle)
    return True


def handle_count() -> int:
    k32 = _k32()
    count = wintypes.DWORD()
    assert k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(count))
    return count.value


def win32_backend(path: Path) -> LibfsapfsBackend:
    from apfs_explorer_lite.disks.win32 import Win32DiskApi

    return LibfsapfsBackend(lambda _s: Win32DiskApi().open_image_reader(path))


def dmg_source(path: Path) -> SourceDevice:
    return SourceDevice(str(path), DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE)


def browse_and_copy(backend: LibfsapfsBackend, source: SourceDevice, dest: Path) -> None:
    backend.open(source)
    try:
        backend.list_volumes()
        entries = backend.list_directory(0, ROOT)
        with backend.open_file(0, ROOT / "passwords.txt") as stream:  # type: ignore[attr-defined]
            assert stream.read() == PASSWORDS_TXT
        report = CopyJob(backend, 0, entries, dest, policy=ConflictPolicy.RENAME).run()
        assert report.status is JobStatus.COMPLETED and not report.failed
    finally:
        backend.close()


# --- file handles --------------------------------------------------------------------------------


@windows_only
def test_source_image_handle_released_on_close(tmp_path: Path) -> None:
    path = image_path("apfs.dmg")
    backend = win32_backend(path)
    backend.open(dmg_source(path))
    backend.list_directory(0, ROOT)
    assert not can_open_exclusively(path)  # sanity: the app holds it while open
    backend.close()
    assert can_open_exclusively(path)


@windows_only
def test_source_handle_released_after_failed_open(tmp_path: Path) -> None:
    from apfs_explorer_lite.errors import ApfsContainerOpenError

    path = image_path("apfs.dmg")
    backend = win32_backend(path)
    with pytest.raises(ApfsContainerOpenError):
        backend.open(SourceDevice(str(path), 0, DMG_PARTITION_SIZE))  # wrong offset: not APFS
    assert can_open_exclusively(path)


@windows_only
def test_copied_files_are_closed(tmp_path: Path) -> None:
    path = image_path("apfs.dmg")
    browse_and_copy(win32_backend(path), dmg_source(path), tmp_path)
    copied = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(copied) == 6
    assert all(can_open_exclusively(p) for p in copied)


@windows_only
def test_no_handle_leak_over_repeated_sessions(tmp_path: Path) -> None:
    path = image_path("apfs.dmg")
    (tmp_path / "warmup").mkdir()
    browse_and_copy(win32_backend(path), dmg_source(path), tmp_path / "warmup")  # imports, caches
    gc.collect()
    before = handle_count()
    for i in range(25):
        dest = tmp_path / f"run{i}"
        dest.mkdir()
        browse_and_copy(win32_backend(path), dmg_source(path), dest)
    gc.collect()
    leaked = handle_count() - before
    assert leaked <= 5, f"{leaked} handles leaked over 25 open/browse/copy/close sessions"


def test_failed_and_cancelled_copies_leave_no_temp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apfs_explorer_lite.copying import PARTIAL_SUFFIX, Destination

    path = image_path("apfs.dmg")
    monkeypatch.setattr(Destination, "place", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")))
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(path))
    backend.open(dmg_source(path))
    try:
        report = CopyJob(backend, 0, backend.list_directory(0, ROOT), tmp_path, policy=ConflictPolicy.SKIP).run()
    finally:
        backend.close()
    assert len(report.failed) == 6
    assert not [p for p in tmp_path.rglob("*") if p.name.endswith(PARTIAL_SUFFIX)]


def test_gui_copy_and_explorer_threads_end(qapp: object, tmp_path: Path) -> None:
    from apfs_explorer_lite.gui.copy_dialog import CopyDialog
    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    from .test_explorer import wait_until

    path = image_path("apfs.dmg")
    baseline = threading.active_count()
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(path))
    explorer = ExplorerWidget(lambda: backend)
    explorer.open_container(dmg_source(path), "test")
    wait_until(lambda: explorer.showing_list)
    entries = backend.list_directory(0, ROOT)
    dialog = CopyDialog(backend, 0, "v", entries, choose_folder=lambda _p, _s: str(tmp_path))
    dialog.browse_button.click()
    dialog.start()
    wait_until(lambda: dialog.report is not None)
    dialog._thread.join(5)
    assert not dialog._thread.is_alive()
    shiboken6.delete(dialog)  # parentless: must not outlive QApplication
    explorer.shutdown()
    assert not backend.is_open
    assert threading.active_count() <= baseline


# --- no writes to the source ------------------------------------------------------------------------


@pytest.mark.parametrize("reader", ["win32-unbuffered", "python-file"])
def test_browse_and_copy_from_read_only_image(tmp_path: Path, reader: str) -> None:
    """Every write attempt would fail on a read-only file; the whole session must still work."""
    if reader == "win32-unbuffered" and os.name != "nt":
        pytest.skip("Windows only")
    image = tmp_path / "readonly.dmg"
    shutil.copyfile(image_path("apfs.dmg"), image)
    os.chmod(image, stat.S_IREAD)
    before_hash, before_mtime = sha256(image), image.stat().st_mtime_ns
    try:
        factory: Callable[[SourceDevice], object] = (
            (lambda _s: __import__("apfs_explorer_lite.disks.win32", fromlist=["x"]).Win32DiskApi().open_image_reader(image))
            if reader == "win32-unbuffered" else (lambda _s: PythonFileSectorReader(image))
        )  # fmt: skip
        dest = tmp_path / "out"
        dest.mkdir()
        browse_and_copy(LibfsapfsBackend(factory), dmg_source(image), dest)  # type: ignore[arg-type]
        assert sha256(image) == before_hash
        assert image.stat().st_mtime_ns == before_mtime
    finally:
        os.chmod(image, stat.S_IREAD | stat.S_IWRITE)
