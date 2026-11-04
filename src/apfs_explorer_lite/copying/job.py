"""One copy job: APFS (read-only) -> a folder on this PC.

For each file:

1. open the APFS file read-only and a new temporary file in the target folder;
2. stream it in chunks, hashing the source bytes (SHA-256) as they are read;
3. flush to disk, read the temporary file back and compare size and SHA-256;
4. set the modification time, then move it to its final name.

The final name therefore only ever holds a complete, verified copy. Cancel,
errors and disconnection remove the temporary file; existing files are
replaced only after an explicit "Replace" decision, and atomically.

The job never calls anything on the backend except listing and reading.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ..backends.base import ApfsBackend
from ..errors import ApfsExplorerError, DestinationSafetyError
from ..formatting import format_size as _size
from ..models import ApfsEntry
from ..safety import extended_path
from ..structured_log import error_fields, log_event, path_fields
from .destination import Destination, is_disk_full
from .plan import CopyPlan, ItemResult, Outcome, PlannedItem, ScanCancelled, build_plan, is_source_lost
from .report import CopyReport, JobStatus

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 1024 * 1024
# Keep this much free on the destination beyond the file being written.
SPACE_RESERVE = 16 * 1024 * 1024
PROGRESS_INTERVAL = 0.1  # seconds between progress callbacks
SPEED_WINDOW = 3.0  # seconds of history for the transfer speed


class ConflictPolicy(enum.Enum):
    ASK = "ask"
    SKIP = "skip"
    RENAME = "rename"


class ConflictChoice(enum.Enum):
    REPLACE = "replace"
    SKIP = "skip"
    RENAME = "rename"  # keep both: the new copy gets a "(2)" name


@dataclass(frozen=True, slots=True)
class Conflict:
    source: ApfsEntry
    destination: Path
    existing_size: int | None
    existing_modified: datetime | None
    can_replace: bool  # False when the existing item is a folder or a link


@dataclass(frozen=True, slots=True)
class ConflictDecision:
    choice: ConflictChoice
    apply_to_all: bool = False


ConflictResolver = Callable[[Conflict], ConflictDecision]


class Phase(enum.Enum):
    SCANNING = "scanning"
    COPYING = "copying"
    VERIFYING = "verifying"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class CopyProgress:
    phase: Phase
    current: PurePosixPath | None = None
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    speed: float = 0.0  # bytes per second
    file_bytes_done: int = 0
    file_size: int = 0

    @property
    def fraction(self) -> float:
        if self.bytes_total:
            return min(1.0, self.bytes_done / self.bytes_total)
        return min(1.0, self.files_done / self.files_total) if self.files_total else 0.0

    @property
    def seconds_left(self) -> float | None:
        if self.phase is not Phase.COPYING or self.speed <= 0:
            return None
        return (self.bytes_total - self.bytes_done) / self.speed


ProgressCallback = Callable[[CopyProgress], None]


class _Cancelled(Exception):
    pass


class _Stop(Exception):
    """Ends the whole job (drive lost, destination full or gone)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(slots=True)
class _Counters:
    files_total: int = 0
    bytes_total: int = 0
    files_done: int = 0
    bytes_done: int = 0
    samples: deque[tuple[float, int]] = field(default_factory=deque)


class CopyJob:
    """Copy ``sources`` (entries of one APFS folder) into ``destination_root``.

    ``run`` may be called once; ``cancel`` is safe from any thread.
    """

    def __init__(
        self,
        backend: ApfsBackend,
        volume_index: int,
        sources: Sequence[ApfsEntry],
        destination_root: Path,
        *,
        policy: ConflictPolicy = ConflictPolicy.ASK,
        resolver: ConflictResolver | None = None,
        progress: ProgressCallback | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        progress_interval: float = PROGRESS_INTERVAL,
    ) -> None:
        if not sources:
            raise ValueError("nothing to copy")
        if policy is ConflictPolicy.ASK and resolver is None:
            raise ValueError("ConflictPolicy.ASK needs a resolver")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._backend = backend
        self._volume = volume_index
        self._sources = list(sources)
        self._destination = Destination(destination_root)
        self._policy = policy
        self._resolver = resolver
        self._progress = progress
        self._chunk_size = chunk_size
        self._progress_interval = progress_interval
        self._cancel = threading.Event()
        self._started = False
        self._remembered: ConflictChoice | None = None
        self._counters = _Counters()
        self._last_emit = 0.0
        self._last_was_scanning: bool | None = None
        # Planned folder parts -> actual folder parts (differs when renamed on conflict).
        self._folders: dict[tuple[str, ...], tuple[str, ...]] = {(): ()}
        self._created_folders: list[tuple[Path, ApfsEntry]] = []

    @property
    def destination_root(self) -> Path:
        return self._destination.root

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    # --- main loop ---------------------------------------------------------------------

    def run(self) -> CopyReport:
        if self._started:
            raise RuntimeError("a copy job can only run once")
        self._started = True
        report = CopyReport(self._destination.root, started_at=datetime.now(UTC))
        try:
            self._check_writable()
            plan = self._scan()
            report.results.extend(plan.results)
            self._copy_items(plan, report)
            report.status = JobStatus.CANCELLED if self.is_cancelled else JobStatus.COMPLETED
        except (_Cancelled, ScanCancelled):
            report.status = JobStatus.CANCELLED
        except _Stop as stop:
            report.status = JobStatus.STOPPED
            report.stop_reason = stop.message
        except Exception as exc:  # noqa: BLE001 - the report must always be produced
            logger.exception("Copy job failed unexpectedly")
            report.status = JobStatus.STOPPED
            report.stop_reason = f"Something unexpected went wrong: {exc}"
        finally:
            self._restore_folder_times()
            report.finished_at = datetime.now(UTC)
            self._emit(Phase.DONE, None, force=True)
        log_event(
            logger, logging.INFO, "copy.finished", status=report.status.value, copied=len(report.copied),
            skipped=len(report.skipped), failed=len(report.failed), bytes_copied=report.bytes_copied,
            seconds=round((report.finished_at - report.started_at).total_seconds(), 2),
        )  # fmt: skip
        return report

    def _check_writable(self) -> None:
        try:
            self._destination.check_writable()
        except PermissionError as exc:
            raise _Stop(
                "Windows doesn't allow saving files in this folder. Choose another folder, "
                "for example one inside Documents."
            ) from exc
        except OSError as exc:
            self._raise_if_fatal(exc)
            raise _Stop(f"Files can't be saved in this folder: {_describe(exc)}") from exc

    def _scan(self) -> CopyPlan:
        def on_folder(path: PurePosixPath, files: int, size: int) -> None:
            self._counters.files_total, self._counters.bytes_total = files, size
            self._emit(Phase.SCANNING, path)

        try:
            plan = build_plan(
                self._backend, self._volume, self._sources, is_cancelled=self._cancel.is_set, on_folder=on_folder
            )
        except ApfsExplorerError as exc:
            raise self._stop_for_source(exc) from exc
        self._counters.files_total = len(plan.files)
        self._counters.bytes_total = plan.total_bytes
        return plan

    def _check_space(self, plan: CopyPlan) -> None:
        # Files that already exist will most likely be skipped or replaced in place.
        needed = sum(
            i.entry.size for i in plan.files if self._destination.lstat(self._destination.path(i.parts)) is None
        )
        free = self._destination.free_space()
        if needed + SPACE_RESERVE > free:
            raise _Stop(
                f"There isn't enough free space in the destination. The copy needs {_size(needed)} "
                f"but only {_size(free)} is free. Choose another destination or fewer files."
            )

    def _copy_items(self, plan: CopyPlan, report: CopyReport) -> None:
        index = 0
        try:
            self._check_space(plan)
            for index, item in enumerate(plan.items):  # noqa: B007 - index used on stop
                self._check_cancel()
                if item.entry.is_directory:
                    if (result := self._copy_folder(item)) is not None:
                        report.results.append(result)
                else:
                    report.results.append(self._copy_file(item))
                    self._counters.files_done += 1
        except (_Cancelled, _Stop) as exc:
            # Everything from the interrupted item on was not copied.
            reason = "Not copied: the copy was cancelled." if isinstance(exc, _Cancelled) else "Not copied: the copy stopped."
            report.results.extend(
                ItemResult(i.entry.path, Outcome.SKIPPED, reason, size=i.entry.size)
                for i in plan.items[index:] if not i.entry.is_directory
            )  # fmt: skip
            raise

    # --- folders -------------------------------------------------------------------------

    def _copy_folder(self, item: PlannedItem) -> ItemResult | None:
        parent = self._folders.get(item.parts[:-1])
        if parent is None:
            return None  # the parent failed; its failure is already reported
        name = item.parts[-1]
        try:
            parent_path = self._destination.check_folder(parent)
            target = parent_path / name
            existing = self._destination.lstat(target)
            if existing is not None and not self._destination.is_plain_folder(target):
                name = self._destination.free_name(parent_path, name, is_folder=True)
            path, created = self._destination.make_folder((*parent, name))
        except (OSError, DestinationSafetyError) as exc:
            self._raise_if_fatal(exc)
            return ItemResult(item.entry.path, Outcome.FAILED, f"Couldn't create the folder: {_describe(exc)}", is_folder=True)
        self._folders[item.parts] = (*parent, name)
        if created:
            self._created_folders.append((path, item.entry))
        if name != item.parts[-1] or item.renamed:
            return ItemResult(
                item.entry.path, Outcome.COPIED, f"Folder saved as “{name}”.", str(path), is_folder=True
            )
        return None

    def _restore_folder_times(self) -> None:
        # Deepest first, after their contents were written.
        for path, entry in reversed(self._created_folders):
            self._set_times(path, entry)

    def _set_times(self, path: Path, entry: ApfsEntry) -> None:
        """Copy the modification (and access) time; a refused time never fails the copy."""
        if entry.modified is None:
            return
        mtime = _ns(entry.modified)
        try:
            self._destination.set_times(path, _ns(entry.accessed) if entry.accessed else mtime, mtime)
        except (OSError, OverflowError, ValueError) as exc:
            log_event(logger, logging.WARNING, "copy.set_time_failed", **path_fields(path), **error_fields(exc))

    # --- files ---------------------------------------------------------------------------

    def _copy_file(self, item: PlannedItem) -> ItemResult:
        entry = item.entry
        folder_parts = self._folders.get(item.parts[:-1])
        if folder_parts is None:
            return ItemResult(entry.path, Outcome.FAILED, "Its folder couldn't be created.", size=entry.size)
        self._emit(Phase.COPYING, entry.path, file_size=entry.size)
        limit = self._destination.max_file_size()
        if limit is not None and entry.size > limit:
            self._counters.bytes_done += entry.size
            return ItemResult(
                entry.path, Outcome.FAILED,
                f"This file is {_size(entry.size)}, larger than the destination drive "
                f"({self._destination.file_system}) can store in one file. Copy it to an NTFS or exFAT drive.",
                size=entry.size,
            )  # fmt: skip
        try:
            folder = self._destination.check_folder(folder_parts)
            final = folder / item.parts[-1]
            replace = False
            existing = self._destination.lstat(final)
            if existing is not None:
                choice = self._decide(entry, final, existing)
                if choice is ConflictChoice.SKIP:
                    self._counters.bytes_done += entry.size
                    return ItemResult(
                        entry.path, Outcome.SKIPPED, "A file with this name already exists in the destination.",
                        str(final), entry.size,
                    )  # fmt: skip
                if choice is ConflictChoice.RENAME:
                    final = folder / self._destination.free_name(folder, final.name)
                else:
                    replace = True
            digest = self._transfer(entry, folder, final, replace=replace)
        except _Cancelled:
            raise
        except (ApfsExplorerError, OSError, ValueError) as exc:
            # ValueError covers names Windows APIs can't encode; only that file fails.
            self._raise_if_fatal(exc)
            log_event(logger, logging.WARNING, "copy.file_failed", **path_fields(entry.path), **error_fields(exc))
            self._counters.bytes_done += entry.size
            return ItemResult(entry.path, Outcome.FAILED, _describe(exc), size=entry.size)
        notes = []
        if final.name != entry.name:
            notes.append(f"Saved as “{final.name}”.")
        if replace:
            notes.append("Replaced the existing file.")
        return ItemResult(entry.path, Outcome.COPIED, " ".join(notes), str(final), entry.size, digest)

    def _transfer(self, entry: ApfsEntry, folder: Path, final: Path, *, replace: bool) -> str:
        """Copy, verify and place one file. Returns its SHA-256."""
        dest = self._destination
        start_bytes = self._counters.bytes_done
        if dest.free_space() < entry.size + SPACE_RESERVE:
            raise _Stop(self._full_message())
        source_hash = hashlib.sha256()
        temp: Path | None = None
        try:
            with self._backend.open_file(self._volume, entry.path) as stream:  # type: ignore[attr-defined]
                fd, temp = dest.create_temp(folder)
                written = 0
                with os.fdopen(fd, "wb", buffering=0) as out:
                    while written < entry.size:
                        self._check_cancel()
                        chunk = stream.read(min(self._chunk_size, entry.size - written))
                        if not chunk:
                            break
                        source_hash.update(chunk)
                        _write_all(out, chunk)
                        written += len(chunk)
                        self._counters.bytes_done = start_bytes + written
                        self._emit(Phase.COPYING, entry.path, file_bytes_done=written, file_size=entry.size)
                    os.fsync(out.fileno())
            if written != entry.size:
                raise ApfsExplorerError(
                    "The file on the APFS drive ended early.", detail=f"{written} of {entry.size} bytes"
                )
            digest = source_hash.hexdigest()
            self._verify(entry, temp, digest)
            self._set_times(temp, entry)
            self._check_cancel()
            dest.place(temp, final, replace=replace)
            temp = None
            log_event(logger, logging.DEBUG, "copy.file_copied", **path_fields(entry.path), size=entry.size)
            return digest
        except BaseException:
            self._counters.bytes_done = start_bytes
            raise
        finally:
            if temp is not None:
                dest.remove_temp(temp)

    def _verify(self, entry: ApfsEntry, temp: Path, expected: str) -> None:
        size = os.stat(extended_path(temp)).st_size
        if size != entry.size:
            raise ApfsExplorerError(
                "The copy has the wrong size, so it was not kept.", detail=f"{size} != {entry.size}"
            )
        check = hashlib.sha256()
        done = 0
        with self._destination.open_for_verify(temp) as copy:
            while chunk := copy.read(self._chunk_size):
                self._check_cancel()
                check.update(chunk)
                done += len(chunk)
                self._emit(Phase.VERIFYING, entry.path, file_bytes_done=done, file_size=entry.size)
        if check.hexdigest() != expected:
            raise ApfsExplorerError(
                "The copy doesn't match the original (SHA-256 differs), so it was not kept.",
                detail=f"{check.hexdigest()} != {expected}",
            )

    # --- conflicts and errors ------------------------------------------------------------------

    def _decide(self, entry: ApfsEntry, final: Path, existing: os.stat_result) -> ConflictChoice:
        can_replace = self._destination.is_plain_file(existing)
        if self._policy is ConflictPolicy.SKIP:
            return ConflictChoice.SKIP
        if self._policy is ConflictPolicy.RENAME:
            return ConflictChoice.RENAME
        remembered = self._remembered
        if remembered is not None and (remembered is not ConflictChoice.REPLACE or can_replace):
            return remembered
        assert self._resolver is not None
        conflict = Conflict(
            entry,
            final,
            existing.st_size if can_replace else None,
            datetime.fromtimestamp(existing.st_mtime, UTC) if can_replace else None,
            can_replace,
        )
        decision = self._resolver(conflict)
        self._check_cancel()
        if decision.choice is ConflictChoice.REPLACE and not can_replace:
            return ConflictChoice.SKIP
        if decision.apply_to_all:
            self._remembered = decision.choice
        return decision.choice

    def _raise_if_fatal(self, exc: Exception) -> None:
        if isinstance(exc, ApfsExplorerError) and is_source_lost(exc):
            raise self._stop_for_source(exc) from exc
        if isinstance(exc, OSError):
            if is_disk_full(exc):
                raise _Stop(self._full_message()) from exc
            if not self._destination.is_available():
                raise _Stop("The destination folder is no longer available. Was its drive disconnected?") from exc

    @staticmethod
    def _stop_for_source(exc: ApfsExplorerError) -> Exception:
        if is_source_lost(exc):
            return _Stop("The APFS drive was disconnected or stopped responding. Reconnect it and copy the rest again.")
        return exc

    def _full_message(self) -> str:
        return (
            f"The destination is full ({_size(self._destination.free_space())} free). "
            "Free up space or choose another destination, then copy the rest again."
        )

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled

    # --- progress ------------------------------------------------------------------------------

    def _emit(
        self,
        phase: Phase,
        current: PurePosixPath | None,
        *,
        file_bytes_done: int = 0,
        file_size: int = 0,
        force: bool = False,
    ) -> None:
        if self._progress is None:
            return
        now = time.monotonic()
        counters = self._counters
        samples = counters.samples
        if phase is Phase.COPYING:
            samples.append((now, counters.bytes_done))
            while len(samples) > 2 and now - samples[0][0] > SPEED_WINDOW:
                samples.popleft()
        # Throttled, except when scanning turns into copying and at the end, so
        # thousands of small files (copy + verify each) don't flood the GUI.
        scanning = phase is Phase.SCANNING
        if not force and scanning == self._last_was_scanning and now - self._last_emit < self._progress_interval:
            return
        self._last_emit = now
        self._last_was_scanning = scanning
        speed = 0.0
        if len(samples) >= 2 and samples[-1][0] > samples[0][0]:
            speed = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
        self._progress(
            CopyProgress(
                phase, current, counters.files_done, counters.files_total, counters.bytes_done,
                counters.bytes_total, max(0.0, speed), file_bytes_done, file_size,
            )  # fmt: skip
        )


def _write_all(out, data: bytes) -> None:  # noqa: ANN001 - raw FileIO
    view = memoryview(data)
    while view:
        count = out.write(view)
        view = view[count:]


def _ns(moment: datetime) -> int:
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _describe(exc: Exception) -> str:
    if isinstance(exc, ApfsExplorerError):
        return exc.user_message
    if isinstance(exc, UnicodeError):
        return "The name contains characters that Windows can't store."
    if isinstance(exc, PermissionError):
        return "Windows denied access to the destination (the file may be open in another program or read-only)."
    if isinstance(exc, FileExistsError):
        return "A file with this name appeared in the destination during the copy; it was left untouched."
    if isinstance(exc, OSError):
        return f"Windows reported an error: {exc.strerror or exc}"
    return str(exc)
