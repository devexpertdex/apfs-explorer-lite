"""APFS backend built on libfsapfs (``pyfsapfs`` Python bindings).

Verified behaviour of libfsapfs-python 20261030 (see tests/test_real_apfs_images.py):

* ``container.open_file_object(obj, "r")`` is the only open mode; ``"w"`` is
  rejected by the binding itself.
* The file object needs ``read``, ``seek`` and ``get_offset``; reads are small
  and unaligned, so the source is wrapped in ``ReadOnlyWindowStream``.
* ``volume.size`` raises on real volumes and is not used.
* Times are read as integer nanoseconds since 1970 UTC.

libfsapfs objects are not documented as thread-safe, so every call into the
binding is serialized with one lock per backend.
"""

from __future__ import annotations

import logging
import stat
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from ..errors import (
    ApfsContainerOpenError,
    ApfsExplorerError,
    ApfsPathNotFoundError,
    ApfsReadError,
    ApfsUnsupportedFeatureError,
    ApfsVolumeLockedError,
    BackendError,
    BackendNotOpenError,
)
from ..models import (
    ApfsEntry,
    ApfsVolumeInfo,
    CompressionInfo,
    EntryKind,
    SourceDevice,
    normalize_apfs_path,
)
from ..disks.apfs_signature import NX_MAX_BLOCK_SIZE, parse_nx_details
from ..structured_log import log_event, path_id
from ..source_io import (
    DEFAULT_CACHED_CHUNKS,
    DEFAULT_CHUNK_SIZE,
    OverlayReader,
    ReadOnlyWindowStream,
    SectorReader,
    open_sector_reader,
)
from .base import ApfsBackend, BackendAvailability, ReadableStream
from .decmpfs import DECMPFS_HEADER_SIZE, DECMPFS_XATTR, parse_decmpfs_header

logger = logging.getLogger(__name__)

ReaderFactory = Callable[[SourceDevice], SectorReader]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_KINDS = {stat.S_IFDIR: EntryKind.DIRECTORY, stat.S_IFREG: EntryKind.FILE, stat.S_IFLNK: EntryKind.SYMLINK}
_OPEN_HINT = (
    "The APFS container could not be opened. It may be damaged or use a feature "
    "libfsapfs does not support (APFS version 1, Fusion drives)."
)


def _ns_to_datetime(value: int | None) -> datetime | None:
    if not value:
        return None
    return _EPOCH + timedelta(microseconds=value // 1000)


class LibfsapfsBackend(ApfsBackend):
    name = "libfsapfs"

    def __init__(
        self,
        reader_factory: ReaderFactory = open_sector_reader,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_cached_chunks: int = DEFAULT_CACHED_CHUNKS,
    ) -> None:
        self._reader_factory = reader_factory
        self._chunk_size = chunk_size
        self._max_cached_chunks = max_cached_chunks
        self._lock = threading.RLock()
        self._reader: SectorReader | None = None
        self._stream: ReadOnlyWindowStream | None = None
        self._container: Any | None = None
        self._volumes: dict[int, Any] = {}
        self._generation = 0

    @classmethod
    def probe(cls) -> BackendAvailability:
        try:
            import pyfsapfs

            version = str(pyfsapfs.get_version())
        except Exception as exc:  # noqa: BLE001 - a broken or blocked native DLL must not crash the app
            logger.warning("pyfsapfs is not usable: %s: %s", type(exc).__name__, exc)
            return BackendAvailability(
                cls.name, False, None,
                f"The APFS reader (libfsapfs) could not be loaded: {exc}. "
                "Reinstall the app; if it persists, security software may be blocking pyfsapfs.",
            )  # fmt: skip
        return BackendAvailability(cls.name, True, version, "libfsapfs bindings loaded (read-only).")

    # --- container ------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._container is not None

    def open(self, source: SourceDevice) -> None:
        import pyfsapfs

        with self._lock:
            if self._container is not None:
                raise BackendError("An APFS container is already open; close it first.")
            reader = self._reader_factory(source)
            stream: ReadOnlyWindowStream | None = None
            try:
                if source.superblock_offset is not None:
                    reader = self._substitute_block_zero(reader, source)
                stream = ReadOnlyWindowStream(
                    reader,
                    source.offset,
                    source.size,
                    chunk_size=self._chunk_size,
                    max_cached_chunks=self._max_cached_chunks,
                )
                if not pyfsapfs.check_container_signature_file_object(stream):
                    raise ApfsContainerOpenError(
                        "No APFS container was found at this location.",
                        detail=f"{source.device_path} offset {source.offset}",
                    )
                container = pyfsapfs.container()
                container.open_file_object(stream, "r")
            except ApfsExplorerError:
                reader.close()
                raise
            except ValueError as exc:
                reader.close()
                raise ApfsContainerOpenError(
                    "The partition does not fit on the source.", detail=str(exc)
                ) from exc
            except OSError as exc:
                reader.close()
                raise self._translate(exc, stream, ApfsContainerOpenError, _OPEN_HINT) from exc
            except BaseException:
                reader.close()
                raise
            self._reader, self._stream, self._container = reader, stream, container
            log_event(
                logger, logging.INFO, "backend.container_opened", source_id=path_id(source.device_path),
                offset=source.offset, volumes=container.number_of_volumes,
                checkpoint_superblock=source.superblock_offset is not None,
            )  # fmt: skip

    @staticmethod
    def _substitute_block_zero(reader: SectorReader, source: SourceDevice) -> SectorReader:
        """Present a checkpoint superblock as block 0, in memory only (damaged block 0)."""
        offset = source.superblock_offset or 0
        probe = ReadOnlyWindowStream(reader, source.offset, source.size)
        probe.seek(offset)
        block = probe.read(NX_MAX_BLOCK_SIZE)
        details = parse_nx_details(block)
        if details is None or not details.checksum_valid or offset % details.block_size:
            raise ApfsContainerOpenError(
                "The backup container superblock is not valid.", detail=f"offset {offset}"
            )
        logger.warning(
            "Using checkpoint superblock xid %s at container offset %d in place of block 0 "
            "(in memory only)", details.xid, offset,
        )  # fmt: skip
        return OverlayReader(reader, source.offset, block[: details.block_size])

    def close(self) -> None:
        with self._lock:
            if self._container is not None:
                try:
                    self._container.close()
                except OSError as exc:
                    logger.warning("Error closing APFS container: %s", exc)
            if self._reader is not None:
                self._reader.close()
            self._container = self._stream = self._reader = None
            self._volumes.clear()
            self._generation += 1

    # --- volumes ----------------------------------------------------------------

    def list_volumes(self) -> list[ApfsVolumeInfo]:
        with self._lock:
            container = self._require_open()
            volumes = []
            for index in range(self._call(lambda: container.number_of_volumes)):
                volume = self._raw_volume(index)
                volumes.append(
                    ApfsVolumeInfo(
                        index=index,
                        name=self._call(lambda: volume.name) or f"Volume {index + 1}",
                        identifier=str(self._call(lambda: volume.identifier)),
                        is_locked=bool(self._call(volume.is_locked)),
                    )
                )
            return volumes

    def _raw_volume(self, index: int) -> Any:
        container = self._require_open()
        volume = self._volumes.get(index)
        if volume is None:
            count = self._call(lambda: container.number_of_volumes)
            if not 0 <= index < count:
                raise BackendError(f"Volume {index} does not exist in this container.")
            volume = self._call(lambda: container.get_volume(index))
            self._volumes[index] = volume
        return volume

    def _unlocked_volume(self, index: int) -> Any:
        volume = self._raw_volume(index)
        if self._call(volume.is_locked):
            name = self._call(lambda: volume.name) or f"Volume {index + 1}"
            raise ApfsVolumeLockedError(
                f"Volume {name!r} is encrypted. Encrypted APFS volumes are not supported."
            )
        return volume

    # --- entries ----------------------------------------------------------------

    def stat(self, volume_index: int, path: PurePosixPath) -> ApfsEntry:
        with self._lock:
            normalized = normalize_apfs_path(path)
            return self._build_entry(self._file_entry(volume_index, normalized), normalized)

    def list_directory(self, volume_index: int, path: PurePosixPath) -> list[ApfsEntry]:
        with self._lock:
            normalized = normalize_apfs_path(path)
            directory = self._file_entry(volume_index, normalized)
            if self._kind(directory) is not EntryKind.DIRECTORY:
                raise BackendError(f"{normalized} is not a folder.")
            count = self._call(lambda: directory.number_of_sub_file_entries)
            entries = []
            for i in range(count):
                child = self._call(lambda i=i: directory.get_sub_file_entry(i))
                name = self._call(lambda: child.name)
                entries.append(self._build_entry(child, normalized / name))
            return entries

    def open_file(self, volume_index: int, path: PurePosixPath) -> ReadableStream:
        with self._lock:
            normalized = normalize_apfs_path(path)
            raw = self._file_entry(volume_index, normalized)
            entry = self._build_entry(raw, normalized)
            if entry.kind is EntryKind.DIRECTORY:
                raise BackendError(f"{normalized} is a folder, not a file.")
            if entry.kind is EntryKind.SYMLINK:
                raise ApfsUnsupportedFeatureError(
                    f"{normalized} is a symbolic link to {entry.symlink_target!r}; it has no content of its own."
                )
            if entry.kind is not EntryKind.FILE:
                raise ApfsUnsupportedFeatureError(f"{normalized} is a special file and cannot be read.")
            if entry.compression is not None and not entry.compression.supported:
                raise ApfsUnsupportedFeatureError(
                    f"{normalized} is compressed with {entry.compression.method_name}, "
                    "which libfsapfs cannot decompress."
                )
            return LibfsapfsFileStream(self, raw, normalized, entry.size, entry.compression)

    def _file_entry(self, volume_index: int, path: PurePosixPath) -> Any:
        volume = self._unlocked_volume(volume_index)
        entry = self._call(lambda: volume.get_file_entry_by_path(str(path)))
        if entry is None:
            raise ApfsPathNotFoundError(f"{path} was not found on the volume.")
        return entry

    def _kind(self, raw: Any) -> EntryKind:
        mode = self._call(lambda: raw.file_mode) or 0
        return _KINDS.get(stat.S_IFMT(mode), EntryKind.OTHER)

    def _build_entry(self, raw: Any, path: PurePosixPath) -> ApfsEntry:
        kind = self._kind(raw)
        xattr_names = tuple(
            self._call(lambda: [x.name for x in raw.extended_attributes])
        )
        compression: CompressionInfo | None = None
        if kind is EntryKind.FILE and DECMPFS_XATTR in xattr_names:
            header = self._call(
                lambda: raw.get_extended_attribute_by_name(DECMPFS_XATTR).read_buffer_at_offset(
                    DECMPFS_HEADER_SIZE, 0
                )
            )
            compression = parse_decmpfs_header(header)
        return ApfsEntry(
            path=path,
            kind=kind,
            size=self._call(lambda: raw.size) or 0,
            modified=_ns_to_datetime(self._call(raw.get_modification_time_as_integer)),
            identifier=self._call(lambda: raw.identifier),
            symlink_target=self._call(lambda: raw.symbolic_link_target)
            if kind is EntryKind.SYMLINK
            else None,
            created=_ns_to_datetime(self._call(raw.get_creation_time_as_integer)),
            accessed=_ns_to_datetime(self._call(raw.get_access_time_as_integer)),
            changed=_ns_to_datetime(self._call(raw.get_inode_change_time_as_integer)),
            mode=self._call(lambda: raw.file_mode),
            owner_id=self._call(lambda: raw.owner_identifier),
            group_id=self._call(lambda: raw.group_identifier),
            link_count=self._call(lambda: raw.number_of_links),
            xattr_names=xattr_names,
            compression=compression,
        )

    # --- error handling -----------------------------------------------------------

    def _require_open(self) -> Any:
        if self._container is None:
            raise BackendNotOpenError("No APFS container is open.")
        return self._container

    def _call[T](self, fn: Callable[[], T]) -> T:
        """Run one binding call, translating libfsapfs ``OSError`` into app errors."""
        try:
            return fn()
        except OSError as exc:
            raise self._translate(exc, self._stream, ApfsReadError, "Reading APFS data failed.") from exc

    @staticmethod
    def _translate(
        exc: OSError,
        stream: ReadOnlyWindowStream | None,
        error_cls: type[BackendError],
        message: str,
    ) -> ApfsExplorerError:
        # pyfsapfs reports failures of our file object only as a generic OSError;
        # surface the original cause (e.g. drive disconnected) when there is one.
        cause = stream.last_error if stream is not None else None
        if stream is not None:
            stream.last_error = None
        if isinstance(cause, ApfsExplorerError):
            return cause
        detail = str(exc) if cause is None else f"{cause}; {exc}"
        return error_cls(message, detail=detail)

    def _check_generation(self, generation: int) -> None:
        if generation != self._generation or self._container is None:
            raise BackendNotOpenError("The APFS container was closed.")


class LibfsapfsFileStream:
    """Read-only stream over one file's content. Implements ``ReadableStream``."""

    def __init__(
        self,
        backend: LibfsapfsBackend,
        raw: Any,
        path: PurePosixPath,
        size: int,
        compression: CompressionInfo | None,
    ) -> None:
        self._backend = backend
        self._raw = raw
        self._generation = backend._generation
        self.path = path
        self.size = size
        self.compression = compression
        self._pos = 0
        self._closed = False

    def read(self, size: int = -1, /) -> bytes:
        if self._closed:
            raise ValueError("stream is closed")
        remaining = self.size - self._pos
        count = remaining if size < 0 else min(size, remaining)
        if count <= 0:
            return b""
        backend = self._backend
        with backend._lock:
            backend._check_generation(self._generation)
            data = backend._call(lambda: self._raw.read_buffer_at_offset(count, self._pos))
        if len(data) != count:
            raise ApfsReadError(
                f"Reading {self.path} returned {len(data)} of {count} expected bytes.",
                detail=f"offset {self._pos}, compression {self.compression}",
            )
        self._pos += count
        return data

    def seek(self, offset: int, whence: int = 0, /) -> int:
        if whence == 0:
            target = offset
        elif whence == 1:
            target = self._pos + offset
        elif whence == 2:
            target = self.size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if target < 0:
            raise ValueError("negative seek position")
        self._pos = target
        return target

    def tell(self) -> int:
        return self._pos

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> LibfsapfsFileStream:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
