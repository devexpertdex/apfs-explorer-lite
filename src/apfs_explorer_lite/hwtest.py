"""Hardware acceptance test on the physical APFS SSD (read-only).

    python -m apfs_explorer_lite.hwtest [--disk N] [--out hardware-results.json]
    python -m apfs_explorer_lite.hwtest --image PATH      # dry run on an image file

What it does, in order:

1. finds the external APFS disk with the app's own discovery (needs admin);
2. records Windows' write counter for that disk;
3. opens the container read-only and walks the first unlocked volume;
4. copies samples (small files, the largest file up to a limit, one nested
   folder, Unicode and Windows-incompatible names, a duplicate) into a new
   temporary folder, then checks every copy against a fresh read of the
   source (size and SHA-256);
5. cancels a copy part-way and checks that nothing half-written remains;
6. closes everything, checks for leaked handles and that the disk's write
   counter did not change;
7. optionally (``--unplug-test``) asks you to unplug the SSD during a copy;
8. deletes the temporary copies.

The results file contains counts, sizes and timings only: no file or folder names.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

from . import APP_NAME, __version__
from .backends.base import ApfsBackend
from .backends.libfsapfs_backend import LibfsapfsBackend
from .copying import PARTIAL_SUFFIX, ConflictPolicy, CopyJob, JobStatus
from .errors import ApfsExplorerError, SourceWriteBlockedError
from .models import APFS_ROOT, ApfsEntry, ApfsIdentification, EntryKind, SourceDevice
from .safety import sanitize_component

SMALL_FILE_MAX = 1024 * 1024
SMALL_FILE_COUNT = 20
LARGE_FILE_MIN = 32 * 1024 * 1024
FOLDER_MAX_BYTES = 300 * 1024 * 1024
FOLDER_MAX_FILES = 2000
WALK_MAX_ENTRIES = 50_000
WALK_MAX_SECONDS = 180.0

PASSED, FAILED, NOT_TESTED = "passed", "failed", "not_tested"


@dataclass
class Check:
    reqs: list[int]
    name: str
    status: str
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class Walk:
    files: list[ApfsEntry] = field(default_factory=list)
    folders: list[ApfsEntry] = field(default_factory=list)
    largest_folder_entries: int = 0
    seconds: float = 0.0
    truncated: bool = False


class Session:
    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.checks: list[Check] = []

    def record(self, reqs: Sequence[int], name: str, status: str, detail: str = "", **metrics: Any) -> Check:
        check = Check(list(reqs), name, status, detail, metrics)
        self.checks.append(check)
        mark = {PASSED: "PASS", FAILED: "FAIL", NOT_TESTED: "----"}[status]
        print(f"  [{mark}] {name}" + (f": {detail}" if detail else ""), file=self.out, flush=True)
        return check

    def guarded(self, reqs: Sequence[int], name: str, fn: Callable[[], tuple[str, str, dict[str, Any]]]) -> None:
        try:
            status, detail, metrics = fn()
        except Exception as exc:  # noqa: BLE001 - every check must produce a result
            message = getattr(exc, "user_message", None) or f"{type(exc).__name__}: {exc}"
            self.record(reqs, name, FAILED, f"error: {message}")
            return
        self.record(reqs, name, status, detail, **metrics)


# --- helpers ------------------------------------------------------------------------------------


def handle_count() -> int | None:
    if os.name != "nt":
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    count = ctypes.c_ulong()
    if not k32.GetProcessHandleCount(ctypes.c_void_p(k32.GetCurrentProcess()), ctypes.byref(count)):
        return None
    return count.value


def source_sha256(backend: ApfsBackend, volume: int, path: PurePosixPath) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with backend.open_file(volume, path) as stream:  # type: ignore[attr-defined]
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def file_sha256(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def walk_volume(backend: ApfsBackend, volume: int) -> Walk:
    walk = Walk()
    queue = [APFS_ROOT]
    start = time.monotonic()
    while queue:
        if len(walk.files) + len(walk.folders) >= WALK_MAX_ENTRIES or time.monotonic() - start > WALK_MAX_SECONDS:
            walk.truncated = True
            break
        folder = queue.pop(0)
        try:
            entries = backend.list_directory(volume, folder)
        except ApfsExplorerError:
            continue
        walk.largest_folder_entries = max(walk.largest_folder_entries, len(entries))
        for entry in entries:
            if entry.is_directory:
                walk.folders.append(entry)
                queue.append(entry.path)
            elif entry.kind is EntryKind.FILE:
                walk.files.append(entry)
    walk.seconds = time.monotonic() - start
    return walk


def verify_copies(backend: ApfsBackend, volume: int, report: Any) -> tuple[int, list[str]]:
    """Re-read each source file and compare with its copy. Returns (verified, problems)."""
    problems = []
    verified = 0
    for result in report.copied:
        source_hash, source_size = source_sha256(backend, volume, result.source)
        copy_hash, copy_size = file_sha256(result.destination)
        if (source_hash, source_size) != (copy_hash, copy_size) or source_hash != result.sha256:
            problems.append(f"mismatch #{len(problems) + 1}")
        else:
            verified += 1
    return verified, problems


def copy_and_verify(
    backend: ApfsBackend, volume: int, sources: Sequence[ApfsEntry], dest: Path, policy: ConflictPolicy = ConflictPolicy.RENAME
) -> tuple[str, str, dict[str, Any]]:
    dest.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    report = CopyJob(backend, volume, sources, dest, policy=policy).run()
    seconds = time.monotonic() - start
    verified, problems = verify_copies(backend, volume, report)
    metrics = {
        "files_copied": len(report.copied), "files_failed": len(report.failed), "files_skipped": len(report.skipped),
        "bytes": report.bytes_copied, "seconds": round(seconds, 2),
        "mb_per_s": round(report.bytes_copied / seconds / 1e6, 1) if seconds > 0 else None,
    }  # fmt: skip
    leftovers = [p for p in dest.rglob("*") if p.name.endswith(PARTIAL_SUFFIX)]
    ok = report.status is JobStatus.COMPLETED and not report.failed and not problems and not leftovers and report.copied
    detail = (
        f"{verified} of {len(report.copied)} copies match the source (SHA-256), "
        f"{report.bytes_copied / 1e6:.1f} MB in {seconds:.1f}s"
    )
    if report.failed:
        detail += f"; {len(report.failed)} failed: " + "; ".join(sorted({r.message for r in report.failed})[:3])
    if leftovers:
        detail += f"; {len(leftovers)} temporary files left behind"
    if report.stop_reason:
        detail += f"; stopped: {report.stop_reason}"
    return (PASSED if ok else FAILED), detail, metrics


# --- the test run ------------------------------------------------------------------------------------


def run_checks(
    session: Session,
    backend_factory: Callable[[], ApfsBackend],
    source: SourceDevice,
    work_dir: Path,
    *,
    max_large_bytes: int,
    counters: Callable[[], Any] | None = None,
    unplug_test: bool = False,
) -> None:
    out = session.out
    handles_before = handle_count()
    counters_before = counters() if counters else None
    backend = backend_factory()

    # 3: read-only access
    def open_read_only() -> tuple[str, str, dict[str, Any]]:
        backend.open(source)
        from .disks.win32 import ALLOWED_DESIRED_ACCESS, GENERIC_READ, Win32DiskApi

        # Show the write guard works, on a disk number that does not exist (never the SSD):
        # if the guard were broken, Windows would only answer "not found".
        write_access = 0x40000000
        blocked = False
        try:
            Win32DiskApi()._create_handle(r"\\.\PhysicalDrive999", GENERIC_READ | write_access)
        except SourceWriteBlockedError:
            blocked = True
        except ApfsExplorerError:
            blocked = False
        blocked = blocked and not any(access & write_access for access in ALLOWED_DESIRED_ACCESS)
        detail = "opened with read access only; the write-access guard refused a test request before Windows"
        return (PASSED if blocked else FAILED), detail, {"checkpoint_superblock": source.superblock_offset is not None}

    session.guarded([3], "Open the APFS container read-only", open_read_only)
    if not backend.is_open:
        return

    try:
        volumes = backend.list_volumes()
        unlocked = [v for v in volumes if not v.is_locked]
        print(f"  {len(volumes)} volume(s), {len(unlocked)} unlocked", file=out)
        if not unlocked:
            session.record([4], "List folders", FAILED, "no unlocked volume")
            return
        volume = unlocked[0].index

        # 4 and 17: enumeration and listing speed
        walk_holder: list[Walk] = []

        def enumerate_volume() -> tuple[str, str, dict[str, Any]]:
            walk = walk_volume(backend, volume)
            walk_holder.append(walk)
            entries = len(walk.files) + len(walk.folders)
            rate = entries / walk.seconds if walk.seconds else 0
            detail = (f"{len(walk.files):,} files and {len(walk.folders):,} folders in {walk.seconds:.1f}s "
                      f"({rate:,.0f} entries/s)" + (" - stopped at the test limit" if walk.truncated else ""))  # fmt: skip
            metrics = {"files": len(walk.files), "folders": len(walk.folders), "seconds": round(walk.seconds, 2),
                       "entries_per_s": round(rate), "largest_folder_entries": walk.largest_folder_entries,
                       "truncated": walk.truncated, "bytes_listed": sum(f.size for f in walk.files)}  # fmt: skip
            return (PASSED if entries else FAILED), detail, metrics

        session.guarded([4, 17], "Walk the volume's folders", enumerate_volume)
        if not walk_holder:
            return
        walk = walk_holder[0]

        # 5: small files
        small = sorted((f for f in walk.files if 0 < f.size <= SMALL_FILE_MAX), key=lambda f: hashlib.sha1(str(f.path).encode("utf-8", "surrogatepass")).digest())
        if small:
            session.guarded([5, 16], f"Copy {min(len(small), SMALL_FILE_COUNT)} small files",
                            lambda: _copy_from_folders(backend, volume, small[:SMALL_FILE_COUNT], work_dir / "small"))  # fmt: skip
        else:
            session.record([5], "Copy small files", NOT_TESTED, "no files of 1 MB or less found")

        # 6: the largest file within the limit
        large = max((f for f in walk.files if f.size <= max_large_bytes), key=lambda f: f.size, default=None)
        if large is not None and large.size >= LARGE_FILE_MIN:
            session.guarded([6, 16], f"Copy a large file ({large.size / 1e6:,.0f} MB)",
                            lambda: _copy_from_folders(backend, volume, [large], work_dir / "large"))  # fmt: skip
        else:
            session.record([6], "Copy a large file", NOT_TESTED,
                           f"no file between {LARGE_FILE_MIN // 2**20} MB and {max_large_bytes // 2**20} MB found")  # fmt: skip

        # 7 and 8: one nested folder
        folder = _pick_nested_folder(backend, volume, walk)
        if folder is not None:
            session.guarded([7, 8, 16], "Copy a complete nested folder",
                            lambda: copy_and_verify(backend, volume, [folder], work_dir / "folder"))  # fmt: skip
        else:
            session.record([7, 8], "Copy a complete nested folder", NOT_TESTED,
                           f"no folder with sub-folders under {FOLDER_MAX_BYTES // 2**20} MB found")  # fmt: skip

        # 9 and 10: special names, if the drive has any
        unicode_files = [f for f in walk.files if not f.name.isascii() and f.size <= 50 * 10**6][:10]
        _optional(session, [9, 16], "Copy files with Unicode names", unicode_files, backend, volume, work_dir / "unicode",
                  "no file names with non-ASCII characters on this drive")  # fmt: skip
        odd_files = [f for f in walk.files if sanitize_component(f.name) != f.name and f.size <= 50 * 10**6][:10]
        _optional(session, [10, 16], "Copy files with names Windows doesn't allow", odd_files, backend, volume,
                  work_dir / "names", "no Windows-incompatible file names on this drive")  # fmt: skip

        # 11: duplicates
        if small:
            def duplicate() -> tuple[str, str, dict[str, Any]]:
                target = work_dir / "duplicate"
                first = copy_and_verify(backend, volume, small[:1], target)
                second = copy_and_verify(backend, volume, small[:1], target, ConflictPolicy.RENAME)
                skipped = CopyJob(backend, volume, small[:1], target, policy=ConflictPolicy.SKIP).run()
                names = sorted(p.name for p in target.iterdir())
                ok = first[0] == second[0] == PASSED and len(names) == 2 and len(skipped.skipped) == 1
                return (PASSED if ok else FAILED), "second copy kept as \"(2)\", third skipped; originals untouched", {}

            session.guarded([11], "Copy the same file three times (keep both, then skip)", duplicate)

        # 15: cancel part-way
        target = large or (max(small, key=lambda f: f.size) if small else None)
        if target is not None:
            session.guarded([15, 19], "Cancel a copy part-way",
                            lambda: _cancel_check(backend, volume, target, work_dir / "cancel"))  # fmt: skip
    finally:
        backend.close()

    # 19: handles; 20: write counter
    handles_after = handle_count()
    if handles_before is not None and handles_after is not None:
        leaked = handles_after - handles_before
        session.record([19], "Handles released after closing", PASSED if leaked <= 10 else FAILED,
                       f"{leaked:+d} open handles compared with the start", leaked_handles=leaked)  # fmt: skip
    leftovers = [p for p in work_dir.rglob("*") if p.name.endswith(PARTIAL_SUFFIX)]
    session.record([19], "No temporary files left", PASSED if not leftovers else FAILED, f"{len(leftovers)} found")
    if counters is not None and counters_before is not None:
        after = counters()
        writes = after.writes - counters_before.writes
        written = after.bytes_written - counters_before.bytes_written
        read = after.bytes_read - counters_before.bytes_read
        session.record([20, 3], "Windows recorded no writes to the SSD", PASSED if writes == 0 and written == 0 else FAILED,
                       f"{writes} writes, {written:,} bytes written, {read / 1e6:,.0f} MB read during the test",
                       writes=writes, bytes_written=written, bytes_read=read)  # fmt: skip
    else:
        session.record([20], "Windows recorded no writes to the SSD", NOT_TESTED, "disk counters not available")

    if unplug_test:
        _unplug_check(session, backend_factory, source, work_dir / "unplug", max_large_bytes)


def _copy_from_folders(backend: ApfsBackend, volume: int, files: Sequence[ApfsEntry], dest: Path) -> tuple[str, str, dict[str, Any]]:
    """Copy files that may live in different folders (one job per folder)."""
    by_folder: dict[PurePosixPath, list[ApfsEntry]] = {}
    for entry in files:
        by_folder.setdefault(entry.path.parent, []).append(entry)
    results = [copy_and_verify(backend, volume, group, dest / f"group{i}") for i, group in enumerate(by_folder.values())]
    status = PASSED if all(r[0] == PASSED for r in results) else FAILED
    metrics: dict[str, Any] = {}
    for key in ("files_copied", "files_failed", "bytes", "seconds"):
        metrics[key] = round(sum(r[2][key] for r in results), 2)
    metrics["mb_per_s"] = round(metrics["bytes"] / metrics["seconds"] / 1e6, 1) if metrics["seconds"] else None
    detail = "; ".join(r[1] for r in results if r[0] != PASSED) or (
        f"{metrics['files_copied']} copies match the source (SHA-256), {metrics['bytes'] / 1e6:.1f} MB "
        f"in {metrics['seconds']:.1f}s" + (f" ({metrics['mb_per_s']} MB/s)" if metrics["mb_per_s"] else "")
    )
    return status, detail, metrics


def _optional(session: Session, reqs: list[int], name: str, files: list[ApfsEntry], backend: ApfsBackend,
              volume: int, dest: Path, missing: str) -> None:  # fmt: skip
    if files:
        session.guarded(reqs, name, lambda: _copy_from_folders(backend, volume, files, dest))
    else:
        session.record(reqs, name, NOT_TESTED, missing)


def _pick_nested_folder(backend: ApfsBackend, volume: int, walk: Walk) -> ApfsEntry | None:
    from .copying import build_plan

    parents_with_subfolders = {f.path.parent for f in walk.folders}
    for folder in walk.folders:
        if folder.path not in parents_with_subfolders:
            continue
        plan = build_plan(backend, volume, [folder])
        files = plan.files
        if 0 < len(files) <= FOLDER_MAX_FILES and plan.total_bytes <= FOLDER_MAX_BYTES:
            return folder
    return None


def _cancel_check(backend: ApfsBackend, volume: int, entry: ApfsEntry, dest: Path) -> tuple[str, str, dict[str, Any]]:
    dest.mkdir(parents=True, exist_ok=True)
    job = CopyJob(backend, volume, [entry], dest, policy=ConflictPolicy.RENAME, chunk_size=64 * 1024)
    started = threading.Event()

    def progress(p: Any) -> None:
        if p.file_bytes_done > 0:
            started.set()
            job.cancel()

    job._progress = progress  # noqa: SLF001 - test hook: cancel as soon as data flows
    job._progress_interval = 0  # noqa: SLF001
    report = job.run()
    files = [p for p in dest.rglob("*") if p.is_file()]
    if report.status is JobStatus.COMPLETED:
        return NOT_TESTED, "the file finished before it could be cancelled", {}
    ok = report.status is JobStatus.CANCELLED and not files
    return (PASSED if ok else FAILED), f"status {report.status.value}, {len(files)} files left in the destination", {}


def _unplug_check(session: Session, backend_factory: Callable[[], ApfsBackend], source: SourceDevice,
                  dest: Path, max_large_bytes: int) -> None:  # fmt: skip
    out = session.out
    backend = backend_factory()
    try:
        backend.open(source)
        volume = next(v.index for v in backend.list_volumes() if not v.is_locked)
        walk = walk_volume(backend, volume)
        files = sorted(walk.files, key=lambda f: f.size, reverse=True)[:200]
        if not files:
            session.record([12], "Unplug during a copy", NOT_TESTED, "no files to copy")
            return
        dest.mkdir(parents=True, exist_ok=True)
        by_folder = files[0].path.parent
        group = [f for f in files if f.path.parent == by_folder] or files[:1]
        print("\n  >>> UNPLUG THE SSD NOW (pull the USB cable). Waiting up to 2 minutes...", file=out, flush=True)
        reports = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            report = CopyJob(backend, volume, group, dest, policy=ConflictPolicy.RENAME, chunk_size=256 * 1024).run()
            reports.append(report)
            if report.status is not JobStatus.COMPLETED:
                break
        last = reports[-1]
        leftovers = [p for p in dest.rglob("*") if p.name.endswith(PARTIAL_SUFFIX)]
        if last.status is JobStatus.COMPLETED:
            session.record([12], "Unplug during a copy", NOT_TESTED, "the SSD was not unplugged within 2 minutes")
            return
        ok = last.status is JobStatus.STOPPED and "disconnected" in (last.stop_reason or "") and not leftovers
        session.record([12], "Unplug during a copy", PASSED if ok else FAILED,
                       f"copy stopped with: {last.stop_reason}; {len(leftovers)} temporary files left")  # fmt: skip
        print("  You can plug the SSD back in now.", file=out, flush=True)
    except Exception as exc:  # noqa: BLE001
        session.record([12], "Unplug during a copy", FAILED, f"error: {getattr(exc, 'user_message', exc)}")
    finally:
        backend.close()


# --- command line ----------------------------------------------------------------------------------


def _find_disk_source(disk: int | None, out: TextIO, session: Session) -> tuple[SourceDevice, int] | None:
    from .disks.discovery import WindowsDiskDiscovery
    from .disks.win32 import Win32DiskApi

    api = Win32DiskApi()
    result = WindowsDiskDiscovery(api).scan()
    candidates = [r for r in result.candidates if disk is None or r.disk.number == disk]
    if not candidates:
        session.record([1], "Detect the external APFS SSD", FAILED,
                       "no external disk with an APFS container was found" + (f" (disk {disk})" if disk is not None else ""))  # fmt: skip
        return None
    report = candidates[0]
    d = report.disk
    session.record([1], "Detect the external APFS SSD", PASSED,
                   f"disk {d.number}: {d.model}, {d.size_bytes / 1e9:.1f} GB, {report.external_reason}",
                   disk=d.number, bus=d.bus_type, size_bytes=d.size_bytes, candidates=len(result.candidates))  # fmt: skip
    container = next(c for c in report.displayable_containers)
    p = container.partition
    good = container.identification in (ApfsIdentification.CONFIRMED, ApfsIdentification.RECOVERABLE)
    session.record([2], "Identify the APFS partition", PASSED if good and p.has_apfs_type else FAILED,
                   f"partition {p.number}: {container.identification.value}, APFS type GUID, "
                   f"{(container.block_count or 0) * (container.block_size or 0) / 1e9:.1f} GB container"
                   + (" (first block damaged: read via a backup superblock in memory)"
                      if container.identification is ApfsIdentification.RECOVERABLE else ""),
                   identification=container.identification.value, block_size=container.block_size)  # fmt: skip
    if not good:
        return None
    return container.to_source_device(), d.number


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="python -m apfs_explorer_lite.hwtest", description=__doc__.split("\n")[0])
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--disk", type=int, help="disk number (default: the external APFS disk found)")
    source_group.add_argument("--image", type=Path, help="dry run against an image file instead of the SSD")
    parser.add_argument("--out", type=Path, default=Path("hardware-results.json"))
    parser.add_argument("--max-large-mb", type=int, default=2048, help="largest file to copy (MB, default 2048)")
    parser.add_argument("--unplug-test", action="store_true", help="ask to unplug the SSD during a copy (last step)")
    parser.add_argument("--keep-copies", action="store_true", help="keep the temporary copies for inspection")
    args = parser.parse_args(argv)

    session = Session(out)
    started = datetime.now().astimezone()
    print(f"{APP_NAME} {__version__} hardware test - {started:%Y-%m-%d %H:%M}", file=out)
    print("Read-only: nothing is written to the APFS drive. Copies go to a temporary folder that is deleted.\n", file=out)
    counters: Callable[[], Any] | None = None
    if args.image is not None:
        from .verify import SourceError, find_image_containers

        kind = "image"
        try:
            source = find_image_containers(args.image)[0]
        except (SourceError, IndexError, ApfsExplorerError) as exc:
            print(f"ERROR: {exc}", file=out)
            return 2
    else:
        kind = "physical"
        if os.name != "nt":
            print("ERROR: the hardware test needs Windows.", file=out)
            return 2
        from .disks.win32 import Win32DiskApi

        api = Win32DiskApi()
        if not api.is_admin():
            print("ERROR: run as administrator (use 'Run hardware test.bat').", file=out)
            return 2
        found = _find_disk_source(args.disk, out, session)
        if found is None:
            _write(args.out, session, started, kind)
            return 1
        source, disk_number = found
        counters = lambda: api.query_counters(disk_number)  # noqa: E731
        try:
            counters()
        except ApfsExplorerError:
            counters = None

    work_dir = Path(tempfile.mkdtemp(prefix="apfs-hwtest-"))
    # Stay well inside the free space of the PC's temp drive.
    max_large = min(args.max_large_mb * 2**20, shutil.disk_usage(work_dir).free // 4)
    try:
        run_checks(session, LibfsapfsBackend, source, work_dir, max_large_bytes=max_large,
                   counters=counters, unplug_test=args.unplug_test)  # fmt: skip
    finally:
        if args.keep_copies:
            print(f"\nCopies kept in {work_dir}", file=out)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)
    _write(args.out, session, started, kind)
    failed = [c for c in session.checks if c.status == FAILED]
    print(f"\nRESULT: {'FAIL' if failed else 'PASS'} - {sum(c.status == PASSED for c in session.checks)} passed, "
          f"{len(failed)} failed, {sum(c.status == NOT_TESTED for c in session.checks)} not tested", file=out)  # fmt: skip
    print(f"Results saved to {args.out}", file=out)
    return 1 if failed else 0


def _write(path: Path, session: Session, started: datetime, kind: str) -> None:
    data = {
        "tool": f"{APP_NAME} {__version__} hwtest",
        "source_kind": kind,  # "physical" or "image" (a dry run, not hardware evidence)
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
        "checks": [asdict(c) for c in session.checks],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
