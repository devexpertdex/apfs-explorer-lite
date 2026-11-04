"""Turn the user's selection into an ordered list of folders and files to copy.

Folders are walked on the APFS volume (read-only). Each source name is
mapped to a valid Windows name; names that clash inside one destination
folder (``a:b`` vs ``a_b``, or ``Report`` vs ``report`` from a
case-sensitive volume) get a ``(2)`` suffix instead of overwriting each other.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..backends.base import ApfsBackend
from ..errors import ApfsExplorerError, BackendNotOpenError, DiskDisconnectedError
from ..models import ApfsEntry, EntryKind
from ..safety import numbered_name, sanitize_component


class Outcome(enum.Enum):
    COPIED = "Copied"
    SKIPPED = "Skipped"
    FAILED = "Failed"


@dataclass(frozen=True, slots=True)
class ItemResult:
    source: PurePosixPath
    outcome: Outcome
    message: str = ""
    destination: str | None = None
    size: int = 0
    sha256: str | None = None
    is_folder: bool = False


@dataclass(frozen=True, slots=True)
class PlannedItem:
    entry: ApfsEntry
    parts: tuple[str, ...]  # destination path below the root, Windows names

    @property
    def renamed(self) -> bool:
        return self.parts[-1] != self.entry.name


@dataclass(slots=True)
class CopyPlan:
    items: list[PlannedItem] = field(default_factory=list)  # folders before their contents
    results: list[ItemResult] = field(default_factory=list)  # skipped/failed while scanning

    @property
    def files(self) -> list[PlannedItem]:
        return [i for i in self.items if not i.entry.is_directory]

    @property
    def total_bytes(self) -> int:
        return sum(i.entry.size for i in self.files)


class ScanCancelled(Exception):
    pass


def is_source_lost(error: Exception) -> bool:
    return isinstance(error, (DiskDisconnectedError, BackendNotOpenError))


class _Names:
    """Unique Windows names per destination folder (NTFS compares case-insensitively)."""

    def __init__(self) -> None:
        self._used: dict[tuple[str, ...], set[str]] = {}

    def assign(self, folder: tuple[str, ...], source_name: str, *, is_folder: bool) -> str:
        used = self._used.setdefault(folder, set())
        name = sanitize_component(source_name)
        number = 2
        candidate = name
        while candidate.casefold() in used:
            candidate = numbered_name(name, number, is_folder=is_folder)
            number += 1
        used.add(candidate.casefold())
        return candidate


def skip_reason(entry: ApfsEntry) -> str | None:
    """Why ``entry`` cannot be copied as a file or folder, or None."""
    if entry.kind is EntryKind.SYMLINK:
        return f"Symbolic link to “{entry.symlink_target}”; links are not followed."
    if entry.kind is EntryKind.OTHER:
        return "Special file (device, pipe or socket); it has no content to copy."
    return None


def build_plan(
    backend: ApfsBackend,
    volume_index: int,
    sources: Sequence[ApfsEntry],
    *,
    is_cancelled: Callable[[], bool] = lambda: False,
    on_folder: Callable[[PurePosixPath, int, int], None] | None = None,
) -> CopyPlan:
    """Walk ``sources`` (depth first, sorted by name).

    Raises the backend error if the APFS drive is lost, and
    :class:`ScanCancelled` if ``is_cancelled`` turns true.
    """
    plan = CopyPlan()
    names = _Names()
    file_count = 0
    byte_count = 0
    seen_folders: set[int] = set()
    # Stack of (entry, destination folder parts); popped in name order.
    stack: list[tuple[ApfsEntry, tuple[str, ...]]] = [
        (entry, ()) for entry in sorted(sources, key=_sort_key, reverse=True)
    ]
    while stack:
        if is_cancelled():
            raise ScanCancelled
        entry, folder = stack.pop()
        reason = skip_reason(entry)
        if reason is not None:
            plan.results.append(ItemResult(entry.path, Outcome.SKIPPED, reason))
            continue
        item = PlannedItem(entry, (*folder, names.assign(folder, entry.name, is_folder=entry.is_directory)))
        if not entry.is_directory:
            plan.items.append(item)
            file_count += 1
            byte_count += entry.size
            continue
        if entry.identifier is not None:
            if entry.identifier in seen_folders:
                plan.results.append(ItemResult(entry.path, Outcome.SKIPPED, "Folder already copied (loop).", is_folder=True))
                continue
            seen_folders.add(entry.identifier)
        if on_folder is not None:
            on_folder(entry.path, file_count, byte_count)
        try:
            children = backend.list_directory(volume_index, entry.path)
        except ApfsExplorerError as exc:
            if is_source_lost(exc):
                raise
            plan.results.append(
                ItemResult(entry.path, Outcome.FAILED, f"Couldn't read this folder: {exc.user_message}", is_folder=True)
            )
            continue
        plan.items.append(item)
        stack.extend((child, item.parts) for child in sorted(children, key=_sort_key, reverse=True))
    return plan


def _sort_key(entry: ApfsEntry) -> tuple[str, str]:
    return (entry.name.casefold(), entry.name)
