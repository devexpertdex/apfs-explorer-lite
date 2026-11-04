"""Static and pre-call safety checks for the Win32 layer.

None of these tests open a disk: every check fires before Windows is called.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apfs_explorer_lite.disks import win32
from apfs_explorer_lite.errors import (
    DiskAccessDeniedError,
    DiskDisconnectedError,
    DiskDiscoveryError,
    SafetyViolationError,
    SourceWriteBlockedError,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

FILE_WRITE_ACCESS = 0x2
SRC = Path(win32.__file__).resolve().parents[1]


def ctl_code(device: int, function: int, method: int = 0, access: int = 0) -> int:
    return (device << 16) | (access << 14) | (function << 2) | method


def test_allowlisted_ioctls_match_winioctl_definitions() -> None:
    assert win32.IOCTL_STORAGE_QUERY_PROPERTY == ctl_code(0x2D, 0x500)
    assert win32.IOCTL_STORAGE_GET_HOTPLUG_INFO == ctl_code(0x2D, 0x305)
    assert win32.IOCTL_DISK_GET_DRIVE_GEOMETRY_EX == ctl_code(0x07, 0x28)
    assert win32.IOCTL_DISK_GET_DRIVE_LAYOUT_EX == ctl_code(0x07, 0x14)
    assert win32.IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS == ctl_code(0x56, 0x00)
    assert win32.IOCTL_DISK_PERFORMANCE == ctl_code(0x07, 0x08)


def test_allowlisted_ioctls_require_no_write_access() -> None:
    for code in win32.READ_ONLY_IOCTLS:
        assert (code >> 14) & FILE_WRITE_ACCESS == 0, hex(code)


@pytest.mark.parametrize(
    "code",
    [
        ctl_code(0x07, 0x15, access=3),  # IOCTL_DISK_SET_DRIVE_LAYOUT_EX
        ctl_code(0x07, 0x16, access=3),  # IOCTL_DISK_CREATE_DISK
        ctl_code(0x07, 0x40, access=3),  # IOCTL_DISK_DELETE_DRIVE_LAYOUT
        ctl_code(0x07, 0x3D, access=3),  # IOCTL_DISK_SET_DISK_ATTRIBUTES
        ctl_code(0x07, 0x50),  # IOCTL_DISK_UPDATE_PROPERTIES
        ctl_code(0x09, 6),  # FSCTL_LOCK_VOLUME
        ctl_code(0x09, 8),  # FSCTL_DISMOUNT_VOLUME
    ],
)
def test_modifying_ioctls_are_not_allowlisted(code: int) -> None:
    assert code not in win32.READ_ONLY_IOCTLS


def test_only_query_and_read_access_allowed() -> None:
    assert win32.ALLOWED_DESIRED_ACCESS == {0, 0x80000000}


@pytest.mark.parametrize(
    "forbidden",
    ["GENERIC_WRITE", "GENERIC_ALL", "WriteFile", "diskpart", "FSCTL_LOCK_VOLUME",
     "FSCTL_DISMOUNT_VOLUME", "IOCTL_DISK_SET", "Initialize-Disk", "Set-Disk",
     "Clear-Disk", "Format-Volume", "Set-Partition"],
)  # fmt: skip
def test_source_tree_has_no_write_primitives(forbidden: str) -> None:
    offenders = [
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if forbidden in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


@windows_only
@pytest.mark.parametrize("access", [0x40000000, 0x10000000, 0xC0000000])  # WRITE, ALL, READ|WRITE
def test_open_with_write_access_is_blocked_before_windows(access: int) -> None:
    api = win32.Win32DiskApi()
    with pytest.raises(SourceWriteBlockedError):
        with api._open(r"\\.\PhysicalDrive999", access):
            pass


@windows_only
def test_non_allowlisted_ioctl_is_blocked_before_windows() -> None:
    api = win32.Win32DiskApi()
    with pytest.raises(SafetyViolationError):
        api._ioctl(0, ctl_code(0x07, 0x15, access=3), None, 64)


@windows_only
@pytest.mark.parametrize(("offset", "length"), [(-512, 512), (0, 0), (0, win32.MAX_READ_BYTES + 512)])
def test_read_bytes_rejects_bad_ranges_before_opening(offset: int, length: int) -> None:
    with pytest.raises(ValueError):
        win32.Win32DiskApi().read_bytes(999, offset, length)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (5, DiskAccessDeniedError),
        (2, DiskDisconnectedError),
        (21, DiskDisconnectedError),
        (433, DiskDisconnectedError),
        (1167, DiskDisconnectedError),
        (1117, DiskDiscoveryError),  # ERROR_IO_DEVICE
    ],
)
def test_winerror_mapping(code: int, expected: type[DiskDiscoveryError]) -> None:
    exc = win32.winerror_to_exception(code, "opening disk", "message")
    assert type(exc) is expected
    assert f"WinError {code}" in (exc.detail or "")


@windows_only
def test_disk_counters_readable_without_write_access() -> None:
    """The no-writes check on the real SSD relies on these counters (query-only handle)."""
    api = win32.Win32DiskApi()
    disk = min(api.system_disk_numbers() or {0})
    first = api.query_counters(disk)
    second = api.query_counters(disk)
    assert first.reads > 0 and first.bytes_read > 0
    assert second.reads >= first.reads and second.writes >= first.writes


def test_parse_disk_performance() -> None:
    import struct

    data = struct.pack("<qqqqqIIII", 1000, 2000, 0, 0, 0, 10, 20, 0, 0) + bytes(36)
    assert win32.parse_disk_performance(data) == win32.DiskCounters(1000, 2000, 10, 20)
    with pytest.raises(DiskDiscoveryError):
        win32.parse_disk_performance(b"short")


@pytest.mark.parametrize(
    "forbidden",
    ["import socket", "import urllib", "from urllib", "http.client", "import requests", "QtNetwork",
     "QNetworkAccessManager", "pip install", "ensurepip"],
)  # fmt: skip
def test_app_has_no_network_or_runtime_download_code(forbidden: str) -> None:
    """The app works offline and never downloads anything at runtime."""
    offenders = [
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if forbidden in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
