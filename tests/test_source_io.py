from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from apfs_explorer_lite.backends.decmpfs import parse_decmpfs_header
from apfs_explorer_lite.errors import BackendError, DiskDisconnectedError, SourceWriteBlockedError
from apfs_explorer_lite.models import SourceDevice
from apfs_explorer_lite.source_io import (
    PythonFileSectorReader,
    ReadOnlyWindowStream,
    open_sector_reader,
    physical_drive_number,
)

from .real_images import windows_only


class MemoryReader:
    """Synthetic sector reader that enforces alignment and records calls."""

    def __init__(self, data: bytes, sector: int = 512) -> None:
        self.data = data
        self.sector_size = sector
        self.size = len(data)
        self.calls: list[tuple[int, int]] = []
        self.error: Exception | None = None

    def read_at(self, offset: int, length: int) -> bytes:
        assert offset % self.sector_size == 0 and length % self.sector_size == 0
        if self.error:
            raise self.error
        self.calls.append((offset, length))
        return self.data[offset : offset + length]

    def close(self) -> None:
        pass


DATA = bytes(range(256)) * 64  # 16 KiB


def test_window_reads_unaligned_ranges_with_aligned_calls() -> None:
    reader = MemoryReader(DATA)
    stream = ReadOnlyWindowStream(reader, 1024, 8192, chunk_size=1024)
    stream.seek(32)
    assert stream.read(88) == DATA[1024 + 32 : 1024 + 120]
    assert stream.get_offset() == stream.tell() == 120
    assert all(o % 512 == 0 and n % 512 == 0 for o, n in reader.calls)


def test_window_is_bounded() -> None:
    stream = ReadOnlyWindowStream(MemoryReader(DATA), 1024, 2048, chunk_size=1024)
    assert stream.get_size() == 2048
    assert stream.read() == DATA[1024:3072]
    assert stream.read(10) == b""
    stream.seek(-10, os.SEEK_END)
    assert stream.read(100) == DATA[3062:3072]
    with pytest.raises(ValueError):
        stream.seek(-1)


def test_window_must_fit_source() -> None:
    with pytest.raises(ValueError):
        ReadOnlyWindowStream(MemoryReader(DATA), 8192, 9000)
    with pytest.raises(ValueError):
        ReadOnlyWindowStream(MemoryReader(DATA), -512, 512)


def test_cache_avoids_repeat_reads_and_evicts() -> None:
    reader = MemoryReader(DATA)
    stream = ReadOnlyWindowStream(reader, 0, len(DATA), chunk_size=1024, max_cached_chunks=2)
    for _ in range(3):
        stream.seek(10)
        stream.read(10)
    assert len(reader.calls) == 1
    for offset in (2048, 4096, 0):
        stream.seek(offset)
        stream.read(1)
    assert len(reader.calls) == 4  # chunk 0 was evicted and re-read


def test_reader_errors_are_recorded() -> None:
    reader = MemoryReader(DATA)
    reader.error = DiskDisconnectedError("gone")
    stream = ReadOnlyWindowStream(reader, 0, len(DATA))
    with pytest.raises(DiskDisconnectedError):
        stream.read(1)
    assert isinstance(stream.last_error, DiskDisconnectedError)


def test_window_has_no_write_interface() -> None:
    stream = ReadOnlyWindowStream(MemoryReader(DATA), 0, 512)
    assert not any(hasattr(stream, name) for name in ("write", "writelines", "truncate", "flush"))
    stream.close()
    with pytest.raises(ValueError):
        stream.read(1)


def test_python_file_reader(tmp_path: Path) -> None:
    path = tmp_path / "img.bin"
    path.write_bytes(DATA)
    reader = PythonFileSectorReader(path)
    assert (reader.size, reader.sector_size) == (len(DATA), 1)
    assert reader.read_at(7, 5) == DATA[7:12]
    reader.close()


def test_physical_drive_number() -> None:
    assert physical_drive_number(r"\\.\PhysicalDrive3") == 3
    assert physical_drive_number(r"\\.\physicaldrive12") == 12
    assert physical_drive_number(r"C:\image.dmg") is None


@pytest.mark.parametrize("path", [r"\\.\C:", r"\\?\GLOBALROOT\Device\Harddisk1\Partition2", r"\\.\Tape0"])
def test_open_sector_reader_rejects_other_devices(path: str) -> None:
    with pytest.raises(BackendError):
        open_sector_reader(SourceDevice(path, 0, 512))


# --- Win32 image reader (real ReadFile path, regular temp file) --------------------------


@windows_only
def test_win32_image_reader_enforces_alignment(tmp_path: Path) -> None:
    from apfs_explorer_lite.disks.win32 import Win32DiskApi

    path = tmp_path / "img.bin"
    path.write_bytes(DATA)
    with Win32DiskApi().open_image_reader(path) as reader:
        assert (reader.size, reader.sector_size) == (len(DATA), 4096)
        assert reader.read_at(4096, 8192) == DATA[4096:12288]
        with pytest.raises(ValueError, match="aligned"):
            reader.read_at(100, 4096)
        with pytest.raises(ValueError, match="past the end"):
            reader.read_at(16384, 4096)
    with pytest.raises(ValueError, match="closed"):
        reader.read_at(0, 4096)


@windows_only
def test_win32_rejects_unexpected_open_flags() -> None:
    from apfs_explorer_lite.disks.win32 import Win32DiskApi

    with pytest.raises(SourceWriteBlockedError):
        Win32DiskApi()._create_handle(r"C:\nonexistent", 0x80000000, 0x04000000)  # DELETE_ON_CLOSE


# --- decmpfs --------------------------------------------------------------------------------


def _header(method: int, size: int) -> bytes:
    return b"fpmc" + struct.pack("<IQ", method, size)


@pytest.mark.parametrize(
    ("method", "name", "supported"),
    [(3, "ZLIB (inline)", True), (8, "LZVN (resource fork)", True), (11, "LZFSE (inline)", True),
     (1, "uncompressed (inline)", False), (13, "LZBitmap (inline)", False), (99, "unknown method 99", False)],
)  # fmt: skip
def test_decmpfs_header(method: int, name: str, supported: bool) -> None:
    info = parse_decmpfs_header(_header(method, 12345))
    assert info is not None
    assert (info.method, info.method_name, info.uncompressed_size, info.supported) == (method, name, 12345, supported)


def test_decmpfs_header_invalid() -> None:
    assert parse_decmpfs_header(b"cmpf" + bytes(12)) is None
    assert parse_decmpfs_header(b"fpmc") is None
