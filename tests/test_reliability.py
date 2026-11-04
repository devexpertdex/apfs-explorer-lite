"""Phase 6 reliability tests: Unicode names, access-denied errors, limits, throttling.

Source data here comes from the in-memory test backend (mock) unless a test
uses the ``dmg`` fixture (real macOS-created APFS image). Destination
behaviour is always the real Windows file system (NTFS) in a temp folder.
"""

from __future__ import annotations

import getpass
import os
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest
import shiboken6

from apfs_explorer_lite.copying import CopyJob, CopyProgress, Destination, JobStatus, Outcome
from apfs_explorer_lite.copying import ConflictChoice, ConflictDecision, ConflictPolicy
from apfs_explorer_lite.errors import DestinationSafetyError, DiskAccessDeniedError

from .real_images import A_FILE, PASSWORDS_TXT, windows_only
from .test_copy_engine import dmg, entries, memory_backend, no_partials, run, tree  # noqa: F401 - fixture

ROOT = PurePosixPath("/")


# --- Unicode names ---------------------------------------------------------------------------

UNICODE_FILES = {
    "/Fotos/Café/crème brûlée.jpg": b"nfc folder",
    "/Fotos/Café/nfd folder.txt": b"nfd folder",  # same look, different code points
    "/日本語/ファイル.txt": b"japanese",
    "/العربية/ملف.txt": b"arabic",
    "/emoji 😀🎉/🐱 cat.png": b"emoji",
    "/Ελληνικά/σημείωση.txt": b"greek",
    "/Русский/файл.docx": b"cyrillic",
    "/עברית/קובץ.pdf": b"hebrew",
    "/한국어/파일.hwp": b"korean",
}


def test_unicode_names_are_preserved(tmp_path: Path) -> None:
    backend = memory_backend(UNICODE_FILES)
    report = run(backend, backend.list_directory(0, ROOT), tmp_path)
    assert report.status is JobStatus.COMPLETED and not report.failed and not report.skipped
    copied = tree(tmp_path)
    for path, data in UNICODE_FILES.items():
        assert copied[path.lstrip("/")] == data, path  # exact names, including NFD
    assert not any(r.message for r in report.copied)  # nothing needed renaming


# Pairs from a real NTFS check: (name, other, NTFS treats them as the same name)
NTFS_PAIRS = [
    ("ä.txt", "Ä.txt", True),
    ("Résumé.docx", "RÉSUMÉ.docx", True),
    ("ı.txt", "i.txt", False),
    ("ß.txt", "ss.txt", False),
    ("ﬁle.txt", "file.txt", False),
    ("σ.txt", "ς.txt", False),
    ("k.txt", "K.txt", False),  # Kelvin sign
]


@pytest.mark.parametrize(("first", "second", "ntfs_same"), NTFS_PAIRS)
def test_names_that_ntfs_may_merge_never_lose_a_file(tmp_path: Path, first: str, second: str, ntfs_same: bool) -> None:
    """Every source file arrives, whether or not NTFS sees the two names as equal."""
    backend = memory_backend({f"/{first}": b"first", f"/{second}": b"second"})
    report = run(backend, backend.list_directory(0, ROOT), tmp_path)
    assert report.status is JobStatus.COMPLETED and len(report.copied) == 2 and not report.skipped
    assert sorted(tree(tmp_path).values()) == [b"first", b"second"]
    if ntfs_same:
        assert any("Saved as" in r.message for r in report.copied)


def test_invalid_unicode_name_fails_alone_and_report_still_saves(qapp: object, tmp_path: Path) -> None:
    from apfs_explorer_lite.gui.copy_dialog import CopyDialog

    backend = memory_backend({"/bad\udc80name.txt": b"lone surrogate", "/ok.txt": b"ok"})
    dest = tmp_path / "dest"
    dest.mkdir()
    report_file = tmp_path / "report.txt"
    sources = backend.list_directory(0, ROOT)
    dialog = CopyDialog(backend, 0, "Test", sources, choose_report_file=lambda _p, _s: str(report_file))
    try:
        dialog._report = run(backend, sources, dest)
        assert dialog._report.status is JobStatus.COMPLETED
        assert (dest / "ok.txt").read_bytes() == b"ok"
        dialog.save_report()
    finally:
        # A parentless dialog left alive until interpreter exit can crash Qt's shutdown.
        shiboken6.delete(dialog)
    text = report_file.read_text(encoding="utf-8-sig")
    assert "bad\\udc80name.txt" in text  # written escaped instead of crashing


# --- access denied ------------------------------------------------------------------------------


def _icacls(*args: str) -> None:
    subprocess.run(["icacls", *args], check=True, capture_output=True)


@pytest.fixture
def denied_folder(tmp_path: Path):  # noqa: ANN201
    """A real folder where Windows refuses to create files (ACL deny for this user)."""
    folder = tmp_path / "no-write"
    folder.mkdir()
    user = getpass.getuser()
    _icacls(str(folder), "/deny", f"{user}:(OI)(CI)(WD,AD)")
    try:
        yield folder
    finally:
        _icacls(str(folder), "/remove:d", user)


@windows_only
def test_unwritable_destination_stops_with_one_clear_message(dmg, denied_folder: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    report = run(backend, entries(backend), denied_folder)
    assert report.status is JobStatus.STOPPED
    assert "doesn't allow saving files in this folder" in (report.stop_reason or "")
    assert not report.failed  # not one failure per file
    assert list(denied_folder.iterdir()) == []


@windows_only
def test_inaccessible_destination_is_explained(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    folder = tmp_path / "no-access"
    folder.mkdir()
    user = getpass.getuser()
    _icacls(str(folder), "/deny", f"{user}:(OI)(CI)(W,AD,WD)")
    try:
        with pytest.raises(DestinationSafetyError, match="doesn't allow access"):
            CopyJob(backend, 0, entries(backend), folder, policy=ConflictPolicy.SKIP)
    finally:
        _icacls(str(folder), "/remove:d", user)


@windows_only
def test_replace_of_file_open_in_another_program_fails_safely(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    existing = tmp_path / "passwords.txt"
    existing.write_bytes(b"in use")
    with open(existing, "rb"):  # another handle without delete sharing, like an open document
        report = run(
            backend, entries(backend, "a_directory", "passwords.txt"), tmp_path,
            policy=ConflictPolicy.ASK, resolver=lambda _c: ConflictDecision(ConflictChoice.REPLACE),
        )  # fmt: skip
    assert existing.read_bytes() == b"in use"
    assert [r.source.name for r in report.failed] == ["passwords.txt"]
    assert "denied access" in report.failed[0].message
    assert (tmp_path / "a_directory" / "a_file").read_bytes() == A_FILE
    assert no_partials(tmp_path)


@windows_only
def test_replace_of_read_only_file_fails_safely(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    existing = tmp_path / "passwords.txt"
    existing.write_bytes(b"read-only")
    os.chmod(existing, stat.S_IREAD)
    try:
        report = run(backend, entries(backend, "passwords.txt"), tmp_path, policy=ConflictPolicy.ASK,
                     resolver=lambda _c: ConflictDecision(ConflictChoice.REPLACE))  # fmt: skip
        assert existing.read_bytes() == b"read-only"
        assert report.failed and "denied access" in report.failed[0].message
        assert no_partials(tmp_path)
    finally:
        os.chmod(existing, stat.S_IREAD | stat.S_IWRITE)


@windows_only
def test_source_disk_without_admin_is_access_denied() -> None:
    from apfs_explorer_lite.disks.win32 import Win32DiskApi

    api = Win32DiskApi()
    if api.is_admin():
        pytest.skip("running as administrator")
    with pytest.raises(DiskAccessDeniedError):
        api.open_device_reader(min(api.system_disk_numbers() or {0}))


# --- destination limits -------------------------------------------------------------------------


def test_file_too_large_for_fat32_fails_that_file_only(dmg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    backend, _ = dmg
    monkeypatch.setattr("apfs_explorer_lite.copying.destination.volume_file_system", lambda _p: "FAT32")
    monkeypatch.setattr("apfs_explorer_lite.copying.destination._MAX_FILE_SIZE", {"FAT32": 100})
    report = run(backend, entries(backend, "a_directory", "passwords.txt"), tmp_path)
    assert report.status is JobStatus.COMPLETED
    assert [r.source.name for r in report.failed] == ["passwords.txt"]  # 116 bytes > 100
    assert "FAT32" in report.failed[0].message
    assert (tmp_path / "a_directory" / "a_file").read_bytes() == A_FILE


def test_destination_file_system_is_detected(tmp_path: Path) -> None:
    destination = Destination(tmp_path)
    if sys.platform == "win32":
        assert destination.file_system == "NTFS"
        assert destination.max_file_size() is None


# --- cancellation and progress ------------------------------------------------------------------


def test_cancel_before_start_copies_nothing(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    job = CopyJob(backend, 0, entries(backend), tmp_path, policy=ConflictPolicy.SKIP)
    job.cancel()
    report = job.run()
    assert report.status is JobStatus.CANCELLED
    assert list(tmp_path.iterdir()) == []


def test_cancel_while_waiting_for_conflict_answer(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    (tmp_path / "passwords.txt").write_bytes(b"existing")
    holder: list[CopyJob] = []

    def resolver(_conflict):  # noqa: ANN001, ANN202
        holder[0].cancel()  # user closes the app while the question is open
        return ConflictDecision(ConflictChoice.REPLACE)

    job = CopyJob(backend, 0, entries(backend, "passwords.txt"), tmp_path, policy=ConflictPolicy.ASK, resolver=resolver)
    holder.append(job)
    assert job.run().status is JobStatus.CANCELLED
    assert (tmp_path / "passwords.txt").read_bytes() == b"existing"


def test_many_small_files_do_not_flood_progress(tmp_path: Path) -> None:
    count = 1500
    backend = memory_backend({f"/many/f{i:05d}.txt": b"x" for i in range(count)})
    calls: list[CopyProgress] = []
    started = time.monotonic()
    report = run(backend, backend.list_directory(0, ROOT), tmp_path, progress=calls.append)
    elapsed = time.monotonic() - started
    assert report.status is JobStatus.COMPLETED and len(report.copied) == count
    assert len(calls) <= elapsed / 0.1 + 10, f"{len(calls)} progress updates in {elapsed:.1f}s"
    assert calls[-1].files_done == count


def test_skipped_and_failed_counts_add_up(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    (tmp_path / "passwords.txt").write_bytes(b"x")
    report = run(backend, entries(backend), tmp_path, policy=ConflictPolicy.SKIP)
    outcomes = [r.outcome for r in report.results if not r.is_folder]
    assert outcomes.count(Outcome.COPIED) == 5 and outcomes.count(Outcome.SKIPPED) == 2  # existing + link
    assert len(report.copied) + len(report.skipped) + len(report.failed) == 7
    assert (tmp_path / "passwords.txt").read_bytes() == b"x"
    assert PASSWORDS_TXT not in tree(tmp_path).values()
