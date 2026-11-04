"""Test-only in-memory backend. Never import this from production code."""

from __future__ import annotations

import io
from pathlib import PurePosixPath

from apfs_explorer_lite.backends.base import ApfsBackend, BackendAvailability, ReadableStream
from apfs_explorer_lite.errors import ApfsPathNotFoundError, BackendNotOpenError
from apfs_explorer_lite.models import ApfsEntry, ApfsVolumeInfo, EntryKind, SourceDevice


class InMemoryBackend(ApfsBackend):
    name = "test-in-memory"

    def __init__(self, files: dict[str, bytes], volumes: list[ApfsVolumeInfo] | None = None) -> None:
        self._files = {PurePosixPath(p): data for p, data in files.items()}
        self._volumes = volumes or [ApfsVolumeInfo(0, "Macintosh HD", "uuid-0", False)]
        self._open = False

    @classmethod
    def probe(cls) -> BackendAvailability:
        return BackendAvailability(cls.name, True, "test", "test double")

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self, source: SourceDevice) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def list_volumes(self) -> list[ApfsVolumeInfo]:
        self._check()
        return list(self._volumes)

    def _dirs(self) -> set[PurePosixPath]:
        dirs = {PurePosixPath("/")}
        for path in self._files:
            dirs.update(path.parents)
        return dirs

    def stat(self, volume_index: int, path: PurePosixPath) -> ApfsEntry:
        self._check()
        if path in self._files:
            return ApfsEntry(path, EntryKind.FILE, len(self._files[path]))
        if path in self._dirs():
            return ApfsEntry(path, EntryKind.DIRECTORY, 0)
        raise ApfsPathNotFoundError(f"{path} not found")

    def list_directory(self, volume_index: int, path: PurePosixPath) -> list[ApfsEntry]:
        self._check()
        children = {p for p in (*self._files, *self._dirs()) if p != path and p.parent == path}
        return [self.stat(volume_index, child) for child in children]

    def open_file(self, volume_index: int, path: PurePosixPath) -> ReadableStream:
        self._check()
        return io.BytesIO(self._files[path])

    def _check(self) -> None:
        if not self._open:
            raise BackendNotOpenError("not open")
