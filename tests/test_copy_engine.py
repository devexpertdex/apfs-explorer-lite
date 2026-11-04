"""Copy engine tests.

Most tests copy from real, macOS-created APFS images through the real
libfsapfs backend. Faults (disconnect, full disk, failed verification) are
injected around the real code paths. The large-file test uses a generated
stream, because no public real APFS image contains a large file.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import shutil
import sys
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from apfs_explorer_lite.backends.libfsapfs_backend import LibfsapfsBackend
from apfs_explorer_lite.copying import (
    PARTIAL_SUFFIX,
    Conflict,
    ConflictChoice,
    ConflictDecision,
    ConflictPolicy,
    CopyJob,
    CopyProgress,
    Destination,
    JobStatus,
    Outcome,
    Phase,
    build_plan,
)
from apfs_explorer_lite.copying import job as job_module
from apfs_explorer_lite.disks.recovery import find_latest_superblock
from apfs_explorer_lite.errors import DestinationSafetyError, DiskDisconnectedError
from apfs_explorer_lite.models import ApfsEntry, ApfsVolumeInfo, EntryKind, SourceDevice
from apfs_explorer_lite.safety import extended_path
from apfs_explorer_lite.source_io import PythonFileSectorReader

from .doubles import InMemoryBackend
from .real_images import (
    A_FILE,
    ANOTHER_FILE,
    DMG_PARTITION_OFFSET,
    DMG_PARTITION_SIZE,
    PASSWORDS_TXT,
    SHA256,
    image_path,
    sha256,
    windows_only,
)

ROOT = PurePosixPath("/")


class FailingReader(PythonFileSectorReader):
    """Real image reader that can be "unplugged" mid-copy."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_with: Exception | None = None

    def read_at(self, offset: int, length: int) -> bytes:
        if self.fail_with is not None:
            raise self.fail_with
        return super().read_at(offset, length)


@pytest.fixture
def dmg() -> Iterator[tuple[LibfsapfsBackend, FailingReader]]:
    path = image_path("apfs.dmg")
    reader = FailingReader(path)
    # Tiny cache so every file read reaches the reader (needed for the unplug test).
    backend = LibfsapfsBackend(lambda _s: reader, chunk_size=4096, max_cached_chunks=1)
    backend.open(SourceDevice(str(path), DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE))
    yield backend, reader
    backend.close()
    assert sha256(path) == SHA256["apfs.dmg"], "source image changed"


@pytest.fixture
def raw() -> Iterator[LibfsapfsBackend]:
    path = image_path("apfs.raw")
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(path))
    backend.open(SourceDevice(str(path), 0, path.stat().st_size))
    yield backend
    backend.close()
    assert sha256(path) == SHA256["apfs.raw"], "source image changed"


def entries(backend, *names: str, folder: str = "/") -> list[ApfsEntry]:  # noqa: ANN001
    listing = {e.name: e for e in backend.list_directory(0, PurePosixPath(folder))}
    return [listing[n] for n in names] if names else list(listing.values())


def run(backend, sources, dest: Path, **kwargs):  # noqa: ANN001, ANN201
    kwargs.setdefault("policy", ConflictPolicy.SKIP)
    return CopyJob(backend, 0, sources, dest, **kwargs).run()


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def no_partials(root: Path) -> bool:
    return not any(p.name.endswith(PARTIAL_SUFFIX) for p in root.rglob("*"))


# --- real APFS data: basic copying ------------------------------------------------------


def test_copy_one_file(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    report = run(backend, entries(backend, "passwords.txt"), tmp_path)
    assert report.status is JobStatus.COMPLETED
    assert tree(tmp_path) == {"passwords.txt": PASSWORDS_TXT}
    [result] = report.copied
    assert result.sha256 == hashlib.sha256(PASSWORDS_TXT).hexdigest()
    assert result.destination == str(tmp_path.resolve() / "passwords.txt")


def test_copy_whole_volume_preserves_hierarchy(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    report = run(backend, entries(backend), tmp_path, chunk_size=7)  # many chunks per file
    assert report.status is JobStatus.COMPLETED
    copied = tree(tmp_path)
    assert copied["passwords.txt"] == PASSWORDS_TXT
    assert copied["a_directory/a_file"] == A_FILE
    assert copied["a_directory/another_file"] == ANOTHER_FILE
    assert {k for k in copied if k.startswith(".fseventsd/")} == {
        ".fseventsd/000000000003ae5b", ".fseventsd/000000000003ae5c", ".fseventsd/fseventsd-uuid",
    }  # fmt: skip
    assert len(report.copied) == 6 and not report.failed
    # The symbolic link is reported, not followed or copied.
    [link] = report.skipped
    assert link.source == PurePosixPath("/a_link") and "not followed" in link.message
    assert not (tmp_path / "a_link").exists()
    assert no_partials(tmp_path)


def test_every_copy_matches_source_hash(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    report = run(backend, entries(backend), tmp_path)
    for result in report.copied:
        with backend.open_file(0, result.source) as stream:  # type: ignore[attr-defined]
            source = stream.read()
        destination = Path(result.destination).read_bytes()
        assert hashlib.sha256(source).hexdigest() == hashlib.sha256(destination).hexdigest() == result.sha256


EXPECTED_VOLUME = {
    "passwords.txt": PASSWORDS_TXT, "a_directory/a_file": A_FILE, "a_directory/another_file": ANOTHER_FILE,
}  # fmt: skip


def visible(copied: dict[str, bytes]) -> dict[str, bytes]:
    return {k: v for k, v in copied.items() if not k.startswith(".")}


@windows_only
def test_copy_through_windows_unbuffered_reader(tmp_path: Path) -> None:
    """The same Win32 read path (aligned, unbuffered ReadFile) used for physical drives."""
    from apfs_explorer_lite.disks.win32 import Win32DiskApi

    path = image_path("apfs.dmg")
    backend = LibfsapfsBackend(lambda _s: Win32DiskApi().open_image_reader(path))
    backend.open(SourceDevice(str(path), DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE))
    try:
        report = run(backend, entries(backend), tmp_path)
    finally:
        backend.close()
    assert report.status is JobStatus.COMPLETED and len(report.copied) == 6
    assert visible(tree(tmp_path)) == EXPECTED_VOLUME
    assert sha256(path) == SHA256["apfs.dmg"]


def test_copy_from_container_with_damaged_first_block(tmp_path: Path) -> None:
    """Like the owner's SSD: block 0 overwritten, read via a checkpoint superblock."""
    damaged = tmp_path / "damaged.dmg"
    shutil.copyfile(image_path("apfs.dmg"), damaged)
    with damaged.open("r+b") as f:  # only the temporary copy is modified
        f.seek(DMG_PARTITION_OFFSET)
        f.write(bytes.fromhex("eb7690") + b"EXFAT   " + bytes(4085))
    reader = PythonFileSectorReader(damaged)
    found = find_latest_superblock(lambda o, n: reader.read_at(DMG_PARTITION_OFFSET + o, n), DMG_PARTITION_SIZE)
    reader.close()
    assert found is not None
    before = sha256(damaged)
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(damaged))
    backend.open(SourceDevice(str(damaged), DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE, "", found.offset))
    dest = tmp_path / "out"
    dest.mkdir()
    try:
        report = run(backend, entries(backend), dest)
    finally:
        backend.close()
    assert report.status is JobStatus.COMPLETED and len(report.copied) == 6
    assert visible(tree(dest)) == EXPECTED_VOLUME
    assert sha256(damaged) == before  # the damaged source was not "repaired" or touched


def test_copy_multiple_items_and_one_folder(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    run(backend, entries(backend, "a_directory", "passwords.txt"), tmp_path)
    assert tree(tmp_path) == {
        "a_directory/a_file": A_FILE, "a_directory/another_file": ANOTHER_FILE, "passwords.txt": PASSWORDS_TXT,
    }  # fmt: skip


def test_copy_from_subfolder_maps_relative_to_it(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    run(backend, entries(backend, "a_file", folder="/a_directory"), tmp_path)
    assert tree(tmp_path) == {"a_file": A_FILE}


def test_timestamps_preserved(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    run(backend, entries(backend, "a_directory", "passwords.txt"), tmp_path)
    for name, copy in (("passwords.txt", tmp_path / "passwords.txt"), ("a_directory", tmp_path / "a_directory")):
        source = backend.stat(0, ROOT / name).modified
        assert source is not None
        assert abs(copy.stat().st_mtime - source.timestamp()) < 1e-3, name
    a_file = backend.stat(0, PurePosixPath("/a_directory/a_file")).modified
    assert datetime.fromtimestamp((tmp_path / "a_directory" / "a_file").stat().st_mtime, UTC).replace(
        microsecond=0
    ) == a_file.replace(microsecond=0)  # type: ignore[union-attr]


def test_empty_file_and_other_volume(raw, tmp_path: Path) -> None:  # noqa: ANN001
    report = run(raw, entries(raw, "a_directory"), tmp_path)
    assert report.status is JobStatus.COMPLETED
    assert (tmp_path / "a_directory" / "a_resourcefork").read_bytes() == b""
    assert (tmp_path / "a_directory" / "a_file").read_bytes() == A_FILE


def test_progress_reports_name_bytes_and_speed(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    seen: list[CopyProgress] = []
    run(backend, entries(backend), tmp_path, chunk_size=7, progress=seen.append, progress_interval=0)
    copying = [p for p in seen if p.phase is Phase.COPYING]
    assert {p.current.name for p in copying if p.current} >= {"passwords.txt", "a_file"}
    assert copying[0].bytes_total == 435 and copying[0].files_total == 6
    assert seen[-1].phase is Phase.DONE
    assert seen[-1].bytes_done == 435 and seen[-1].fraction == 1.0


# --- conflicts ----------------------------------------------------------------------------


def test_conflict_skip_keeps_existing(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    (tmp_path / "passwords.txt").write_bytes(b"mine")
    report = run(backend, entries(backend, "passwords.txt"), tmp_path, policy=ConflictPolicy.SKIP)
    assert (tmp_path / "passwords.txt").read_bytes() == b"mine"
    assert [r.outcome for r in report.results] == [Outcome.SKIPPED]
    assert "already exists" in report.skipped[0].message


def test_conflict_rename_keeps_both(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    (tmp_path / "passwords.txt").write_bytes(b"mine")
    (tmp_path / "passwords (2).txt").write_bytes(b"also mine")
    report = run(backend, entries(backend, "passwords.txt"), tmp_path, policy=ConflictPolicy.RENAME)
    assert tree(tmp_path) == {
        "passwords (2).txt": b"also mine", "passwords (3).txt": PASSWORDS_TXT, "passwords.txt": b"mine",
    }  # fmt: skip
    assert "passwords (3).txt" in report.copied[0].message


def test_conflict_ask_replace_and_apply_to_all(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    (tmp_path / "a_directory").mkdir()
    (tmp_path / "a_directory" / "a_file").write_bytes(b"old 1")
    (tmp_path / "a_directory" / "another_file").write_bytes(b"old 2")
    asked: list[Conflict] = []

    def resolver(conflict: Conflict) -> ConflictDecision:
        asked.append(conflict)
        return ConflictDecision(ConflictChoice.REPLACE, apply_to_all=True)

    report = run(backend, entries(backend, "a_directory"), tmp_path, policy=ConflictPolicy.ASK, resolver=resolver)
    assert len(asked) == 1  # remembered for the second conflict
    assert asked[0].can_replace and asked[0].existing_size == 5
    assert asked[0].destination == tmp_path.resolve() / "a_directory" / "a_file"
    assert tree(tmp_path) == {"a_directory/a_file": A_FILE, "a_directory/another_file": ANOTHER_FILE}
    assert all("Replaced" in r.message for r in report.copied)


def test_conflict_ask_each_time(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    (tmp_path / "a_directory").mkdir()
    (tmp_path / "a_directory" / "a_file").write_bytes(b"old 1")
    (tmp_path / "a_directory" / "another_file").write_bytes(b"old 2")
    answers = iter([ConflictChoice.SKIP, ConflictChoice.RENAME])
    report = run(
        backend, entries(backend, "a_directory"), tmp_path,
        policy=ConflictPolicy.ASK, resolver=lambda _c: ConflictDecision(next(answers)),
    )  # fmt: skip
    assert tree(tmp_path) == {
        "a_directory/a_file": b"old 1", "a_directory/another_file": b"old 2",
        "a_directory/another_file (2)": ANOTHER_FILE,
    }  # fmt: skip
    assert len(report.copied) == 1 and len(report.skipped) == 1


def test_file_in_the_way_of_folder_gets_new_folder_name(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    (tmp_path / "a_directory").write_bytes(b"a file, not a folder")
    report = run(backend, entries(backend, "a_directory"), tmp_path)
    assert (tmp_path / "a_directory").read_bytes() == b"a file, not a folder"
    assert tree(tmp_path / "a_directory (2)") == {"a_file": A_FILE, "another_file": ANOTHER_FILE}
    assert any("a_directory (2)" in r.message for r in report.results if r.is_folder)


def test_replace_refused_for_folder_in_the_way(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    (tmp_path / "passwords.txt").mkdir()
    asked: list[Conflict] = []

    def resolver(conflict: Conflict) -> ConflictDecision:
        asked.append(conflict)
        return ConflictDecision(ConflictChoice.REPLACE)

    report = run(backend, entries(backend, "passwords.txt"), tmp_path, policy=ConflictPolicy.ASK, resolver=resolver)
    assert not asked[0].can_replace
    assert (tmp_path / "passwords.txt").is_dir()
    assert report.skipped and not report.copied


# --- interruptions --------------------------------------------------------------------------


def test_cancel_mid_file_leaves_no_partial_file(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    job_ref: list[CopyJob] = []

    def progress(p: CopyProgress) -> None:
        if p.current and p.current.name == "passwords.txt" and p.file_bytes_done >= 14:
            job_ref[0].cancel()

    job = CopyJob(backend, 0, entries(backend, "a_directory", "passwords.txt"), tmp_path,
                  policy=ConflictPolicy.SKIP, progress=progress, chunk_size=7, progress_interval=0)  # fmt: skip
    job_ref.append(job)
    report = job.run()
    assert report.status is JobStatus.CANCELLED
    assert tree(tmp_path) == {"a_directory/a_file": A_FILE, "a_directory/another_file": ANOTHER_FILE}
    assert no_partials(tmp_path)
    [not_copied] = report.skipped
    assert not_copied.source == PurePosixPath("/passwords.txt") and "cancelled" in not_copied.message


def test_cancel_during_replace_keeps_existing_file_intact(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    (tmp_path / "passwords.txt").write_bytes(b"precious existing data")
    job_ref: list[CopyJob] = []

    def progress(p: CopyProgress) -> None:
        if p.file_bytes_done >= 21:
            job_ref[0].cancel()

    job = CopyJob(backend, 0, entries(backend, "passwords.txt"), tmp_path, policy=ConflictPolicy.ASK,
                  resolver=lambda _c: ConflictDecision(ConflictChoice.REPLACE), progress=progress, chunk_size=7,
                  progress_interval=0)  # fmt: skip
    job_ref.append(job)
    assert job.run().status is JobStatus.CANCELLED
    assert tree(tmp_path) == {"passwords.txt": b"precious existing data"}


def test_drive_unplugged_mid_copy(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, reader = dmg

    def progress(p: CopyProgress) -> None:
        if p.phase is Phase.COPYING and p.current and p.current.name == "passwords.txt":
            reader.fail_with = DiskDisconnectedError("The disk is no longer available.")

    report = run(backend, entries(backend, "a_directory", "passwords.txt"), tmp_path, progress=progress, progress_interval=0)
    assert report.status is JobStatus.STOPPED
    assert "disconnected" in (report.stop_reason or "")
    assert tree(tmp_path) == {"a_directory/a_file": A_FILE, "a_directory/another_file": ANOTHER_FILE}
    assert no_partials(tmp_path)
    assert [r.source.name for r in report.skipped] == ["passwords.txt"]


def test_drive_unplugged_before_first_file(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, reader = dmg
    sources = entries(backend, "a_directory")
    reader.fail_with = DiskDisconnectedError("gone")
    report = run(backend, sources, tmp_path)
    assert report.status is JobStatus.STOPPED and "disconnected" in (report.stop_reason or "")
    assert tree(tmp_path) == {} and no_partials(tmp_path)
    assert not report.copied and len(report.skipped) == 2


def test_drive_unplugged_while_scanning(tmp_path: Path) -> None:
    class Unplugged(InMemoryBackend):
        def list_directory(self, volume_index: int, path: PurePosixPath) -> list[ApfsEntry]:
            if path != ROOT:
                raise DiskDisconnectedError("gone")
            return super().list_directory(volume_index, path)

    backend = Unplugged({"/folder/file.txt": b"x"})
    backend.open(SourceDevice("memory", 0, 1))
    report = run(backend, backend.list_directory(0, ROOT), tmp_path)
    assert report.status is JobStatus.STOPPED and "disconnected" in (report.stop_reason or "")
    assert list(tmp_path.iterdir()) == []


def test_not_enough_space_stops_before_writing(dmg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    backend, _ = dmg
    monkeypatch.setattr(Destination, "free_space", lambda self: 1000)
    report = run(backend, entries(backend), tmp_path)
    assert report.status is JobStatus.STOPPED
    assert "isn't enough free space" in (report.stop_reason or "")
    assert list(tmp_path.iterdir()) == []
    assert len(report.skipped) == 7  # 6 files not copied + the link


def test_disk_full_while_writing(dmg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    backend, _ = dmg
    real_write = job_module._write_all
    calls = {"n": 0}

    def write(out, data):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] > 3:
            raise OSError(errno.ENOSPC, "There is not enough space on the disk")
        return real_write(out, data)

    monkeypatch.setattr(job_module, "_write_all", write)
    report = run(backend, entries(backend, "a_directory", "passwords.txt"), tmp_path, chunk_size=20)
    assert report.status is JobStatus.STOPPED and "destination is full" in (report.stop_reason or "")
    assert tree(tmp_path) == {"a_directory/a_file": A_FILE}  # 3 chunks of 20 bytes
    assert no_partials(tmp_path)


def test_verification_failure_discards_copy(dmg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    backend, _ = dmg
    monkeypatch.setattr(Destination, "open_for_verify", lambda self, p: io.BytesIO(b"X" * 116))
    report = run(backend, entries(backend, "passwords.txt"), tmp_path)
    assert report.status is JobStatus.COMPLETED
    [failed] = report.failed
    assert "doesn't match" in failed.message
    assert list(tmp_path.iterdir()) == []


def test_one_failed_file_does_not_stop_the_rest(dmg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    backend, _ = dmg
    real_place = Destination.place

    def place(self, temp, final, *, replace):  # noqa: ANN001, ANN202
        if final.name == "a_file":
            raise PermissionError(13, "Access is denied")
        return real_place(self, temp, final, replace=replace)

    monkeypatch.setattr(Destination, "place", place)
    report = run(backend, entries(backend, "a_directory", "passwords.txt"), tmp_path)
    assert report.status is JobStatus.COMPLETED
    assert [r.source.name for r in report.failed] == ["a_file"]
    assert "denied access" in report.failed[0].message
    assert tree(tmp_path) == {"a_directory/another_file": ANOTHER_FILE, "passwords.txt": PASSWORDS_TXT}
    assert no_partials(tmp_path)


def test_destination_removed_mid_copy(dmg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    backend, _ = dmg

    def gone(self, folder):  # noqa: ANN001, ANN202
        raise FileNotFoundError(errno.ENOENT, "The system cannot find the path specified")

    monkeypatch.setattr(Destination, "create_temp", gone)
    monkeypatch.setattr(Destination, "is_available", lambda self: False)
    report = run(backend, entries(backend, "passwords.txt"), tmp_path)
    assert report.status is JobStatus.STOPPED and "no longer available" in (report.stop_reason or "")


# --- names, paths and safety ------------------------------------------------------------------


def memory_backend(files: dict[str, bytes]) -> InMemoryBackend:
    backend = InMemoryBackend(files, volumes=[ApfsVolumeInfo(0, "Test", "uuid", False)])
    backend.open(SourceDevice("memory", 0, 1))
    return backend


def test_invalid_and_colliding_names(tmp_path: Path) -> None:
    long_name = "L" * 300 + ".txt"
    files = {
        "/a:b.txt": b"colon", "/a_b.txt": b"underscore", "/CON": b"device name",
        "/trailing. ": b"trailing", "/Report.txt": b"upper", "/report.txt": b"lower",
        f"/{long_name}": b"long", "/dir?/x|y": b"nested",
    }  # fmt: skip
    backend = memory_backend(files)
    report = run(backend, backend.list_directory(0, ROOT), tmp_path)
    assert report.status is JobStatus.COMPLETED and not report.failed
    copied = tree(tmp_path)
    assert sorted(copied.values()) == sorted(files.values())  # nothing lost or overwritten
    assert copied["_CON"] == b"device name" and copied["trailing"] == b"trailing"
    assert copied["dir_/x_y"] == b"nested"
    assert {copied["a_b.txt"], copied["a_b (2).txt"]} == {b"colon", b"underscore"}
    assert {copied["Report.txt"], copied["report (2).txt"]} == {b"upper", b"lower"}
    long_copy = next(k for k in copied if k.startswith("LLL"))
    assert len(long_copy) == 255 and long_copy.endswith(".txt")
    renamed = {r.source.name for r in report.copied if r.message.startswith("Saved as")}
    assert {"CON", "trailing. ", long_name, "x|y"} <= renamed


def test_long_destination_paths(tmp_path: Path) -> None:
    deep = "/" + "/".join(f"folder-{i:02d}-" + "d" * 40 for i in range(8)) + "/file.bin"
    backend = memory_backend({deep: b"deep data"})
    report = run(backend, backend.list_directory(0, ROOT), tmp_path)
    assert report.status is JobStatus.COMPLETED, report.as_text()
    destination = report.copied[0].destination
    assert destination is not None and len(destination) > 300
    assert Path(destination) == tmp_path.resolve().joinpath(*PurePosixPath(deep).parts[1:])
    with open(extended_path(Path(destination)), "rb") as fh:
        assert fh.read() == b"deep data"


def test_special_files_are_skipped(tmp_path: Path) -> None:
    backend = memory_backend({"/ok.txt": b"ok"})
    pipe = ApfsEntry(PurePosixPath("/fifo"), EntryKind.OTHER, 0)
    report = run(backend, [*backend.list_directory(0, ROOT), pipe], tmp_path)
    assert tree(tmp_path) == {"ok.txt": b"ok"}
    assert "Special file" in report.skipped[0].message


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_junction_in_destination_is_not_followed(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    import _winapi

    backend, _ = dmg
    dest = tmp_path / "dest"
    outside = tmp_path / "outside"
    dest.mkdir()
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(dest / "a_directory"))
    report = run(backend, entries(backend, "a_directory", "passwords.txt"), dest)
    assert list(outside.iterdir()) == []  # nothing written through the link
    assert report.status is JobStatus.COMPLETED and not report.failed
    # The junction is left alone; the folder is copied next to it under a new name.
    assert tree(dest) == {
        "a_directory (2)/a_file": A_FILE, "a_directory (2)/another_file": ANOTHER_FILE, "passwords.txt": PASSWORDS_TXT,
    }  # fmt: skip


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_junction_swapped_in_after_folder_check_is_refused(
    dmg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    import _winapi

    backend, _ = dmg
    dest = tmp_path / "dest"
    outside = tmp_path / "outside"
    dest.mkdir()
    outside.mkdir()
    real_make = Destination.make_folder

    def make_then_swap(self, parts):  # noqa: ANN001, ANN202
        path, created = real_make(self, parts)
        if parts[-1] == "a_directory":  # replace the new folder with a link before files are written
            os.rmdir(path)
            _winapi.CreateJunction(str(outside), str(path))
        return path, created

    monkeypatch.setattr(Destination, "make_folder", make_then_swap)
    report = run(backend, entries(backend, "a_directory"), dest)
    assert list(outside.iterdir()) == []
    assert len(report.failed) == 2 and all("link" in r.message for r in report.failed)


def test_destination_must_be_safe(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    sources = entries(backend, "passwords.txt")
    for bad in (tmp_path / "missing", Path(r"\\.\PhysicalDrive1"), Path("relative")):
        with pytest.raises(DestinationSafetyError):
            CopyJob(backend, 0, sources, bad, policy=ConflictPolicy.SKIP)
    if sys.platform == "win32":
        with pytest.raises(DestinationSafetyError, match="system folders"):
            CopyJob(backend, 0, sources, Path(os.environ["SystemRoot"]) / "Temp", policy=ConflictPolicy.SKIP)


def test_destination_path_rejects_unsanitized_parts(tmp_path: Path) -> None:
    destination = Destination(tmp_path)
    for bad in ("..", "a/b", "a\\b", "C:", "CON", ""):
        with pytest.raises(DestinationSafetyError):
            destination.path(("ok", bad))
    with pytest.raises(DestinationSafetyError):
        destination.remove_temp(tmp_path / "not-a-temp-file.txt")


def test_job_runs_once_and_ask_needs_resolver(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    sources = entries(backend, "passwords.txt")
    with pytest.raises(ValueError):
        CopyJob(backend, 0, sources, tmp_path, policy=ConflictPolicy.ASK)
    with pytest.raises(ValueError):
        CopyJob(backend, 0, [], tmp_path)
    job = CopyJob(backend, 0, sources, tmp_path, policy=ConflictPolicy.SKIP)
    job.run()
    with pytest.raises(RuntimeError):
        job.run()


def test_plan_order_and_names(dmg) -> None:  # noqa: ANN001
    backend, _ = dmg
    plan = build_plan(backend, 0, entries(backend))
    assert [i.entry.path.as_posix() for i in plan.items] == [
        "/.fseventsd", "/.fseventsd/000000000003ae5b", "/.fseventsd/000000000003ae5c",
        "/.fseventsd/fseventsd-uuid", "/a_directory", "/a_directory/a_file", "/a_directory/another_file",
        "/passwords.txt",
    ]  # fmt: skip
    assert plan.total_bytes == 435


def test_report_text_lists_hashes(dmg, tmp_path: Path) -> None:  # noqa: ANN001
    backend, _ = dmg
    text = run(backend, entries(backend), tmp_path).as_text()
    assert "6 copied and verified" in text
    assert hashlib.sha256(PASSWORDS_TXT).hexdigest() in text
    assert "/a_link" in text and "SKIPPED (1)" in text


# --- large files -----------------------------------------------------------------------------------


class GeneratedStream(io.RawIOBase):
    """Deterministic content produced on the fly (never held in memory)."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.pos = 0

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        n = self.size - self.pos if n < 0 else min(n, self.size - self.pos)
        start = self.pos
        self.pos += n
        return block_bytes(start, n)


def block_bytes(start: int, n: int) -> bytes:
    pattern = bytes(range(251))  # prime length, so chunk boundaries never line up
    offset = start % 251
    repeated = pattern * ((n + offset) // 251 + 1)
    return repeated[offset : offset + n]


class LargeFileBackend(InMemoryBackend):
    def __init__(self, size: int) -> None:
        super().__init__({"/big.bin": b""})
        self.size = size

    def stat(self, volume_index: int, path: PurePosixPath) -> ApfsEntry:
        entry = super().stat(volume_index, path)
        if path.name == "big.bin":
            return ApfsEntry(path, EntryKind.FILE, self.size, modified=datetime(2024, 5, 1, tzinfo=UTC))
        return entry

    def open_file(self, volume_index: int, path: PurePosixPath):  # noqa: ANN201
        return GeneratedStream(self.size)


def test_large_file_is_streamed_with_bounded_memory(tmp_path: Path) -> None:
    size = 200 * 1024 * 1024 + 12345
    backend = LargeFileBackend(size)
    backend.open(SourceDevice("generated", 0, 1))
    expected = hashlib.sha256()
    for start in range(0, size, 8 << 20):
        expected.update(block_bytes(start, min(8 << 20, size - start)))
    seen: list[CopyProgress] = []
    tracemalloc.start()
    try:
        report = run(backend, backend.list_directory(0, ROOT), tmp_path, progress=seen.append)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.status is JobStatus.COMPLETED
    assert (tmp_path / "big.bin").stat().st_size == size
    assert report.copied[0].sha256 == expected.hexdigest()
    assert peak < 16 * 1024 * 1024, f"peak memory {peak}"  # 1 MiB chunks, not the whole file
    assert any(p.phase is Phase.VERIFYING for p in seen)
    assert (tmp_path / "big.bin").stat().st_mtime == datetime(2024, 5, 1, tzinfo=UTC).timestamp()
