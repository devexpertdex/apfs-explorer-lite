"""Read-only access to the bytes of an APFS source (physical disk or image file).

``SectorReader`` reads aligned ranges from a device or file.
``ReadOnlyWindowStream`` turns a byte range of a reader (one partition) into
the file-like object that pyfsapfs expects. pyfsapfs issues small unaligned
reads (32 or 88 bytes at a time, observed with libfsapfs 20261030), while raw
Windows disk handles accept only sector-aligned reads. The stream therefore
always reads whole aligned chunks and serves requests from a small cache.

Nothing here can write: readers expose ``read_at`` only, and the stream has
no ``write`` method.
"""

from __future__ import annotations

import os
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Protocol

from .errors import BackendError
from .models import SourceDevice
from .safety import require_read_only_mode

_PHYSICAL_DRIVE = re.compile(r"^\\\\\.\\PhysicalDrive(\d+)$", re.IGNORECASE)
_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\", "//./", "//?/")

DEFAULT_CHUNK_SIZE = 256 * 1024
DEFAULT_CACHED_CHUNKS = 64  # 16 MiB with the default chunk size


class SectorReader(Protocol):
    @property
    def sector_size(self) -> int: ...

    @property
    def size(self) -> int: ...

    def read_at(self, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at ``offset``; both must be sector multiples.

        May return fewer bytes only at the end of the source.
        """

    def close(self) -> None: ...


class PythonFileSectorReader:
    """Portable reader for image files using Python's buffered ``open(..., "rb")``."""

    def __init__(self, path: Path) -> None:
        mode = "rb"
        require_read_only_mode(mode)
        self._file = open(path, mode)  # noqa: SIM115 - closed in close()
        self._size = os.fstat(self._file.fileno()).st_size

    @property
    def sector_size(self) -> int:
        return 1

    @property
    def size(self) -> int:
        return self._size

    def read_at(self, offset: int, length: int) -> bytes:
        self._file.seek(offset)
        return self._file.read(length)

    def close(self) -> None:
        self._file.close()


class OverlayReader:
    """Present one byte range of a reader with substitute bytes, in memory only.

    Used to read an APFS container whose block 0 was overwritten, by showing a
    valid superblock copy from its checkpoint area in its place. The source is
    never modified; the substitution exists only in the returned data.
    """

    def __init__(self, inner: SectorReader, patch_offset: int, patch: bytes) -> None:
        self._inner = inner
        self._patch_offset = patch_offset
        self._patch = bytes(patch)

    @property
    def sector_size(self) -> int:
        return self._inner.sector_size

    @property
    def size(self) -> int:
        return self._inner.size

    def read_at(self, offset: int, length: int) -> bytes:
        data = self._inner.read_at(offset, length)
        start = max(offset, self._patch_offset)
        end = min(offset + len(data), self._patch_offset + len(self._patch))
        if start >= end:
            return data
        patched = bytearray(data)
        patched[start - offset : end - offset] = self._patch[start - self._patch_offset : end - self._patch_offset]
        return bytes(patched)

    def close(self) -> None:
        self._inner.close()


def _round_up(value: int, multiple: int) -> int:
    return -(-value // multiple) * multiple


class ReadOnlyWindowStream:
    """Read-only, seekable view of ``[offset, offset + size)`` of a reader.

    Provides ``read``, ``seek``, ``tell``, ``get_offset`` and ``get_size``,
    the methods pyfsapfs uses on file objects. The first exception raised by
    the underlying reader is kept in ``last_error``, because pyfsapfs reports
    callback failures only as a generic ``OSError``.
    """

    def __init__(
        self,
        reader: SectorReader,
        offset: int,
        size: int,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_cached_chunks: int = DEFAULT_CACHED_CHUNKS,
    ) -> None:
        if offset < 0 or size <= 0 or offset + size > reader.size:
            raise ValueError(
                f"window offset={offset} size={size} does not fit a source of {reader.size} bytes"
            )
        self._reader = reader
        self._offset = offset
        self._size = size
        self._chunk_size = _round_up(chunk_size, reader.sector_size)
        self._max_cached = max(1, max_cached_chunks)
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._pos = 0
        self._closed = False
        self.last_error: BaseException | None = None

    def read(self, size: int | None = -1) -> bytes:
        self._check_open()
        remaining = self._size - self._pos
        count = remaining if size is None or size < 0 else min(size, remaining)
        if count <= 0:
            return b""
        out = bytearray()
        position = self._offset + self._pos
        end = position + count
        while position < end:
            index = position // self._chunk_size
            chunk = self._chunk(index)
            start = position - index * self._chunk_size
            piece = chunk[start : start + (end - position)]
            if not piece:
                error = OSError(f"unexpected end of source at byte {position}")
                self.last_error = error
                raise error
            out += piece
            position += len(piece)
        self._pos += count
        return bytes(out)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._check_open()
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._pos + offset
        elif whence == os.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if target < 0:
            raise ValueError("negative seek position")
        self._pos = target
        return self._pos

    def tell(self) -> int:
        return self._pos

    def get_offset(self) -> int:
        return self._pos

    def get_size(self) -> int:
        return self._size

    def close(self) -> None:
        self._closed = True
        self._cache.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("stream is closed")

    def _chunk(self, index: int) -> bytes:
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        start = index * self._chunk_size
        length = min(self._chunk_size, _round_up(self._reader.size - start, self._reader.sector_size))
        try:
            data = self._reader.read_at(start, length)
        except BaseException as exc:
            self.last_error = exc
            raise
        self._cache[index] = data
        if len(self._cache) > self._max_cached:
            self._cache.popitem(last=False)
        return data


def physical_drive_number(device_path: str) -> int | None:
    match = _PHYSICAL_DRIVE.match(device_path)
    return int(match.group(1)) if match else None


def open_sector_reader(source: SourceDevice) -> SectorReader:
    """Open the source read-only: a ``\\\\.\\PhysicalDriveN`` disk or an image file."""
    disk_number = physical_drive_number(source.device_path)
    if disk_number is not None:
        from .disks.win32 import Win32DiskApi

        return Win32DiskApi().open_device_reader(disk_number)
    if source.device_path.lower().startswith(_DEVICE_PREFIXES):
        raise BackendError(
            "Only physical disks (\\\\.\\PhysicalDriveN) and image files can be opened.",
            detail=source.device_path,
        )
    path = Path(source.device_path)
    if sys.platform == "win32":
        from .disks.win32 import Win32DiskApi

        return Win32DiskApi().open_image_reader(path)
    return PythonFileSectorReader(path)
