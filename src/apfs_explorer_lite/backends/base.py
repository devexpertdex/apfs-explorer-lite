"""Backend interface that every APFS implementation must satisfy.

The interface is read-only by construction: there are no methods that
create, modify, rename or delete anything, and file content is exposed only
through :class:`ReadableStream`, which has no ``write``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import TracebackType
from typing import ClassVar, Protocol, Self, runtime_checkable

from ..models import ApfsEntry, ApfsVolumeInfo, SourceDevice


@runtime_checkable
class ReadableStream(Protocol):
    """Read-only, seekable byte stream for one APFS file."""

    def read(self, size: int = -1, /) -> bytes: ...
    def seek(self, offset: int, whence: int = 0, /) -> int: ...
    def tell(self) -> int: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackendAvailability:
    name: str
    available: bool
    version: str | None
    message: str


class ApfsBackend(ABC):
    """Abstract read-only APFS reader.

    Implementations must open the source device read-only (see
    :func:`apfs_explorer_lite.safety.require_read_only_mode`) and must never
    load a write-capable driver as a fallback.
    """

    name: ClassVar[str]
    # The registry refuses any backend that does not declare this as True.
    read_only: ClassVar[bool] = True

    @classmethod
    @abstractmethod
    def probe(cls) -> BackendAvailability:
        """Report whether this backend can be used, without touching any disk."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def open(self, source: SourceDevice) -> None:
        """Open the APFS container stored in ``source`` read-only."""

    @abstractmethod
    def close(self) -> None:
        """Release the container and any device handle. Safe to call twice."""

    @abstractmethod
    def list_volumes(self) -> list[ApfsVolumeInfo]: ...

    @abstractmethod
    def list_directory(self, volume_index: int, path: PurePosixPath) -> list[ApfsEntry]: ...

    @abstractmethod
    def stat(self, volume_index: int, path: PurePosixPath) -> ApfsEntry: ...

    @abstractmethod
    def open_file(self, volume_index: int, path: PurePosixPath) -> ReadableStream: ...

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
