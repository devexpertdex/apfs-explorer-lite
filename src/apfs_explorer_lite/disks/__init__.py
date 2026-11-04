"""Read-only disk discovery."""

from __future__ import annotations

from .api import DiskApi, RawDiskMetadata
from .discovery import (
    APFS_PARTITION_TYPE_GUID,
    DiscoveryResult,
    DiskDiscovery,
    DiskReport,
    WindowsDiskDiscovery,
)

__all__ = [
    "APFS_PARTITION_TYPE_GUID",
    "DiscoveryResult",
    "DiskApi",
    "DiskDiscovery",
    "DiskReport",
    "RawDiskMetadata",
    "WindowsDiskDiscovery",
]
