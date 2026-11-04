"""Opt-in checks against the real Windows disk stack of this machine.

Read-only: metadata queries use query-only handles, and signature reads only
happen for APFS-typed partitions on external disks. Enable with
``APFS_LIVE_DISK_TESTS=1``.
"""

from __future__ import annotations

import os
import sys

import pytest

from apfs_explorer_lite.disks.discovery import WindowsDiskDiscovery
from apfs_explorer_lite.disks.win32 import Win32DiskApi

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("APFS_LIVE_DISK_TESTS") != "1",
    reason="set APFS_LIVE_DISK_TESTS=1 to run live Windows disk tests",
)


def test_live_scan_is_consistent() -> None:
    api = Win32DiskApi()
    numbers = api.list_physical_drive_numbers()
    assert numbers, "Windows reported no physical drives"
    system = api.system_disk_numbers()
    assert system <= set(numbers)

    result = WindowsDiskDiscovery(api).scan()
    assert {r.disk.number for r in result.disks} <= set(numbers)
    for report in result.disks:
        assert report.disk.size_bytes > 0
        assert report.disk.bytes_per_sector in (512, 4096)
        for p in report.partitions:
            assert 0 <= p.offset < report.disk.size_bytes
            assert p.offset + p.size <= report.disk.size_bytes
        if report.is_system or not report.is_external:
            assert report.containers == ()
            assert not report.is_candidate
