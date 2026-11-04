"""The low-level disk API that discovery depends on.

:class:`~apfs_explorer_lite.disks.win32.Win32DiskApi` implements it with
real Windows calls; tests substitute a fake that returns crafted buffers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RawDiskMetadata:
    """Unparsed IOCTL output buffers for one physical disk."""

    descriptor: bytes  # STORAGE_DEVICE_DESCRIPTOR
    geometry: bytes  # DISK_GEOMETRY_EX
    layout: bytes  # DRIVE_LAYOUT_INFORMATION_EX
    hotplug: bytes | None  # STORAGE_HOTPLUG_INFO, None if unsupported


class DiskApi(Protocol):
    def list_physical_drive_numbers(self) -> list[int]:
        """Numbers N of every ``\\\\.\\PhysicalDriveN`` currently present."""

    def system_disk_numbers(self) -> set[int]:
        """Disk numbers backing the Windows system volume."""

    def is_admin(self) -> bool: ...

    def query_metadata(self, disk_number: int) -> RawDiskMetadata:
        """Metadata via a query-only handle; never reads disk content."""

    def read_bytes(self, disk_number: int, offset: int, length: int) -> bytes:
        """Read a small, sector-aligned range through a read-only handle."""
