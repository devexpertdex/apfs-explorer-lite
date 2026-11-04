"""ctypes bindings for the Win32 storage calls used by disk discovery.

This is the only module that opens device handles. It uses two kinds:

* **Query handles** (``dwDesiredAccess = 0``). They allow only metadata
  IOCTLs defined with ``FILE_ANY_ACCESS``, cannot read or write disk content,
  and do not need administrator rights.
* **Read handles** (``GENERIC_READ``), used only by :meth:`read_bytes` to read
  a few sectors for the APFS signature check. Windows requires administrator
  rights for these.

No handle is ever opened with write access, and only the IOCTLs in
:data:`READ_ONLY_IOCTLS` can be sent. Both rules are checked before any call
reaches Windows.
"""

from __future__ import annotations

import ctypes
import logging
import mmap
import os
import struct
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from ctypes import wintypes

from ..errors import (
    DiskAccessDeniedError,
    DiskDisconnectedError,
    DiskDiscoveryError,
    SafetyViolationError,
    SourceWriteBlockedError,
)
from ..models import physical_drive_path
from .api import RawDiskMetadata
from .parsing import parse_geometry, parse_physical_drive_numbers, parse_volume_disk_extents

logger = logging.getLogger(__name__)

QUERY_ACCESS = 0
GENERIC_READ = 0x80000000
ALLOWED_DESIRED_ACCESS = frozenset({QUERY_ACCESS, GENERIC_READ})
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
# Unbuffered file reads follow the same alignment rules as raw disk reads.
FILE_FLAG_NO_BUFFERING = 0x20000000
ALLOWED_FLAGS = frozenset({0, FILE_FLAG_NO_BUFFERING})
IMAGE_FILE_SECTOR_SIZE = 4096  # a multiple of every common physical sector size
MAX_READER_CHUNK = 16 * 1024 * 1024

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_STORAGE_GET_HOTPLUG_INFO = 0x002D0C14
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_DISK_GET_DRIVE_LAYOUT_EX = 0x00070050
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
# Read/write counters since boot; used to show that nothing was written to the source.
IOCTL_DISK_PERFORMANCE = 0x00070020
READ_ONLY_IOCTLS = frozenset(
    {
        IOCTL_DISK_PERFORMANCE,
        IOCTL_STORAGE_QUERY_PROPERTY,
        IOCTL_STORAGE_GET_HOTPLUG_INFO,
        IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
        IOCTL_DISK_GET_DRIVE_LAYOUT_EX,
        IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
    }
)

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_NOT_READY = 21
ERROR_DEV_NOT_EXIST = 55
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_MORE_DATA = 234
ERROR_NO_SUCH_DEVICE = 433
ERROR_MEDIA_CHANGED = 1110
ERROR_DEVICE_NOT_CONNECTED = 1167
DISCONNECTED_ERRORS = frozenset(
    {
        ERROR_FILE_NOT_FOUND,
        ERROR_PATH_NOT_FOUND,
        ERROR_NOT_READY,
        ERROR_DEV_NOT_EXIST,
        ERROR_NO_SUCH_DEVICE,
        ERROR_MEDIA_CHANGED,
        ERROR_DEVICE_NOT_CONNECTED,
    }
)

MAX_READ_BYTES = 1024 * 1024
_MAX_IOCTL_BUFFER = 1024 * 1024
_STORAGE_DEVICE_PROPERTY_QUERY = struct.pack("<IIB3x", 0, 0, 0)  # StorageDeviceProperty, standard
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def winerror_to_exception(code: int, context: str, message: str = "") -> DiskDiscoveryError:
    detail = f"{context}: [WinError {code}] {message}".strip()
    if code == ERROR_ACCESS_DENIED:
        return DiskAccessDeniedError(
            "Access denied. Administrator rights are required for this operation.", detail=detail
        )
    if code in DISCONNECTED_ERRORS:
        return DiskDisconnectedError(
            "The disk is no longer available. It may have been disconnected.", detail=detail
        )
    return DiskDiscoveryError(f"Windows reported an error while {context}.", detail=detail)


def _last_error(context: str) -> DiskDiscoveryError:
    code = ctypes.get_last_error()
    return winerror_to_exception(code, context, ctypes.FormatError(code).strip())


@dataclass(frozen=True, slots=True)
class DiskCounters:
    """Windows' I/O counters for one disk (all processes, since boot)."""

    bytes_read: int
    bytes_written: int
    reads: int
    writes: int


def parse_disk_performance(data: bytes) -> DiskCounters:
    # DISK_PERFORMANCE: BytesRead, BytesWritten, ReadTime, WriteTime, IdleTime (LARGE_INTEGER),
    # ReadCount, WriteCount (DWORD), ...
    if len(data) < 48:
        raise DiskDiscoveryError("The disk returned incomplete I/O counters.", detail=f"{len(data)} bytes")
    bytes_read, bytes_written, _rt, _wt, _it, reads, writes = struct.unpack_from("<qqqqqII", data)
    return DiskCounters(bytes_read, bytes_written, reads, writes)


class Win32DiskApi:
    """Implementation of :class:`~apfs_explorer_lite.disks.api.DiskApi`."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise DiskDiscoveryError("Disk discovery is only supported on Windows.")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]  # fmt: skip
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        ]  # fmt: skip
        k32.DeviceIoControl.restype = wintypes.BOOL
        k32.ReadFile.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_OVERLAPPED),
        ]  # fmt: skip
        k32.ReadFile.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.QueryDosDeviceW.restype = wintypes.DWORD
        k32.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
        k32.GetFileSizeEx.restype = wintypes.BOOL
        self._k32 = k32

    # --- DiskApi ------------------------------------------------------------

    def list_physical_drive_numbers(self) -> list[int]:
        size = 64 * 1024
        while True:
            buf = ctypes.create_unicode_buffer(size)
            count = self._k32.QueryDosDeviceW(None, buf, size)
            if count:
                return parse_physical_drive_numbers(buf[:count])
            if ctypes.get_last_error() == ERROR_INSUFFICIENT_BUFFER and size < 4 * 1024 * 1024:
                size *= 4
                continue
            raise _last_error("listing physical drives")

    def system_disk_numbers(self) -> set[int]:
        drive = os.environ.get("SystemDrive", "C:")
        with self._open(rf"\\.\{drive}", QUERY_ACCESS) as handle:
            buf = self._ioctl(handle, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 256)
        return parse_volume_disk_extents(buf)

    def is_admin(self) -> bool:
        try:
            return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
        except (OSError, AttributeError):
            return False

    def query_metadata(self, disk_number: int) -> RawDiskMetadata:
        with self._open(physical_drive_path(disk_number), QUERY_ACCESS) as handle:
            descriptor = self._ioctl(
                handle, IOCTL_STORAGE_QUERY_PROPERTY, _STORAGE_DEVICE_PROPERTY_QUERY, 4096
            )
            geometry = self._ioctl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, 256)
            layout = self._ioctl(handle, IOCTL_DISK_GET_DRIVE_LAYOUT_EX, None, 8192)
            try:
                hotplug: bytes | None = self._ioctl(
                    handle, IOCTL_STORAGE_GET_HOTPLUG_INFO, None, 8
                )
            except DiskDisconnectedError:
                raise
            except DiskDiscoveryError as exc:
                logger.debug("Hotplug info unavailable for disk %s: %s", disk_number, exc)
                hotplug = None
        return RawDiskMetadata(descriptor, geometry, layout, hotplug)

    def query_counters(self, disk_number: int) -> DiskCounters:
        """Windows' read/write counters for the disk (query-only handle, no admin needed)."""
        with self._open(physical_drive_path(disk_number), QUERY_ACCESS) as handle:
            return parse_disk_performance(self._ioctl(handle, IOCTL_DISK_PERFORMANCE, None, 128))

    def read_bytes(self, disk_number: int, offset: int, length: int) -> bytes:
        if offset < 0 or not 0 < length <= MAX_READ_BYTES:
            raise ValueError(f"invalid read range offset={offset} length={length}")
        with self.open_device_reader(disk_number) as reader:
            return reader.read_at(offset, length)

    # --- persistent read-only readers ---------------------------------------

    def open_device_reader(self, disk_number: int) -> Win32SectorReader:
        """Keep a ``GENERIC_READ`` handle to a physical disk open (needs admin)."""
        path = physical_drive_path(disk_number)
        handle = self._create_handle(path, GENERIC_READ)
        try:
            geometry = parse_geometry(
                self._ioctl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, 256)
            )
        except BaseException:
            self._k32.CloseHandle(handle)
            raise
        if geometry.bytes_per_sector <= 0:
            self._k32.CloseHandle(handle)
            raise DiskDiscoveryError("The disk reported an invalid sector size.", detail=path)
        return Win32SectorReader(self, handle, geometry.bytes_per_sector, geometry.disk_size, path)

    def open_image_reader(self, path: os.PathLike[str] | str) -> Win32SectorReader:
        """Open a disk image file read-only and unbuffered (no admin needed)."""
        text = os.fspath(path)
        try:
            handle = self._create_handle(text, GENERIC_READ, FILE_FLAG_NO_BUFFERING)
        except DiskDisconnectedError as exc:
            raise DiskDiscoveryError("The image file was not found.", detail=exc.detail) from exc
        size = ctypes.c_longlong(0)
        if not self._k32.GetFileSizeEx(handle, ctypes.byref(size)):
            error = _last_error(f"getting the size of {text}")
            self._k32.CloseHandle(handle)
            raise error
        return Win32SectorReader(self, handle, IMAGE_FILE_SECTOR_SIZE, size.value, text)

    # --- internals ----------------------------------------------------------

    def _create_handle(self, path: str, desired_access: int, flags: int = 0) -> int:
        if desired_access not in ALLOWED_DESIRED_ACCESS:
            raise SourceWriteBlockedError(
                "Refusing to open a device with write access.", detail=f"{path} {desired_access:#x}"
            )
        if flags not in ALLOWED_FLAGS:
            raise SourceWriteBlockedError("Refusing unexpected open flags.", detail=f"{path} {flags:#x}")
        handle = self._k32.CreateFileW(
            path, desired_access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, flags, None
        )
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            raise _last_error(f"opening {path}")
        return handle

    def _close_handle(self, handle: int) -> None:
        self._k32.CloseHandle(handle)

    @contextmanager
    def _open(self, path: str, desired_access: int) -> Iterator[int]:
        handle = self._create_handle(path, desired_access)
        try:
            yield handle
        finally:
            self._k32.CloseHandle(handle)

    def _ioctl(self, handle: int, code: int, in_bytes: bytes | None, out_size: int) -> bytes:
        if code not in READ_ONLY_IOCTLS:
            raise SafetyViolationError(
                "Blocked a disk control code that is not on the read-only list.",
                detail=f"{code:#010x}",
            )
        in_buf = ctypes.create_string_buffer(in_bytes, len(in_bytes)) if in_bytes else None
        in_len = len(in_bytes) if in_bytes else 0
        size = out_size
        while True:
            out = ctypes.create_string_buffer(size)
            returned = wintypes.DWORD(0)
            ok = self._k32.DeviceIoControl(
                handle, code, in_buf, in_len, out, size, ctypes.byref(returned), None
            )
            if ok:
                return out.raw[: returned.value]
            code_err = ctypes.get_last_error()
            if code_err in (ERROR_INSUFFICIENT_BUFFER, ERROR_MORE_DATA) and size < _MAX_IOCTL_BUFFER:
                size *= 2
                continue
            raise winerror_to_exception(
                code_err, f"querying device (IOCTL {code:#010x})", ctypes.FormatError(code_err).strip()
            )

    def _read_aligned(self, handle: int, offset: int, length: int, description: str) -> bytes:
        # Raw disk reads are unbuffered; an anonymous mapping gives a page-aligned buffer.
        mapping = mmap.mmap(-1, length)
        try:
            buffer = (ctypes.c_char * length).from_buffer(mapping)
            try:
                overlapped = _OVERLAPPED(Offset=offset & 0xFFFFFFFF, OffsetHigh=offset >> 32)
                read = wintypes.DWORD(0)
                if not self._k32.ReadFile(
                    handle, buffer, length, ctypes.byref(read), ctypes.byref(overlapped)
                ):
                    raise _last_error(f"reading {description}")
                return buffer.raw[: read.value]
            finally:
                del buffer
        finally:
            mapping.close()


class Win32SectorReader:
    """A read-only handle kept open for repeated aligned reads.

    Implements :class:`apfs_explorer_lite.source_io.SectorReader`.
    """

    def __init__(
        self, api: Win32DiskApi, handle: int, sector_size: int, size: int, description: str
    ) -> None:
        self._api = api
        self._handle: int | None = handle
        self._sector_size = sector_size
        self._size = size
        self._description = description

    @property
    def sector_size(self) -> int:
        return self._sector_size

    @property
    def size(self) -> int:
        return self._size

    def read_at(self, offset: int, length: int) -> bytes:
        if self._handle is None:
            raise ValueError(f"reader for {self._description} is closed")
        sector = self._sector_size
        if offset < 0 or not 0 < length <= MAX_READER_CHUNK:
            raise ValueError(f"invalid read range offset={offset} length={length}")
        if offset % sector or length % sector:
            raise ValueError(f"read range is not aligned to {sector}-byte sectors")
        if offset + length > -(-self._size // sector) * sector:
            raise ValueError("read range extends past the end of the source")
        return self._api._read_aligned(self._handle, offset, length, self._description)

    def close(self) -> None:
        if self._handle is not None:
            self._api._close_handle(self._handle)
            self._handle = None

    def __enter__(self) -> Win32SectorReader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def relaunch_as_admin() -> bool:
    """Start a new elevated instance via the UAC prompt. True if it launched."""
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        params = subprocess.list2cmdline(["-m", "apfs_explorer_lite", *sys.argv[1:]])
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
    ]  # fmt: skip
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    result = shell32.ShellExecuteW(None, "runas", sys.executable, params, os.getcwd(), 1)
    launched = (result or 0) > 32
    logger.info("Elevated relaunch %s", "started" if launched else f"failed ({result})")
    return launched
