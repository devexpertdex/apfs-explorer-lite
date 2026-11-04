from __future__ import annotations

import pytest

from apfs_explorer_lite.disks.parsing import (
    parse_device_descriptor,
    parse_drive_layout,
    parse_geometry,
    parse_hotplug_info,
    parse_physical_drive_numbers,
    parse_volume_disk_extents,
)
from apfs_explorer_lite.errors import DiskDiscoveryError
from apfs_explorer_lite.models import APFS_PARTITION_TYPE_GUID, PartitionStyle

from .win_structs import (
    BASIC_DATA_GUID,
    BUS_NVME,
    BUS_USB,
    EFI_GUID,
    UNKNOWN_GUID,
    GptEntry,
    build_device_descriptor,
    build_geometry,
    build_gpt_layout,
    build_hotplug,
    build_mbr_layout,
    build_raw_layout,
    build_volume_extents,
)


def test_device_descriptor_strings_and_bus() -> None:
    buf = build_device_descriptor(
        vendor="Samsung ", product="PSSD T7", revision="0", serial="S5SX", bus_type=BUS_USB
    )
    d = parse_device_descriptor(buf)
    assert (d.vendor, d.product, d.serial) == ("Samsung", "PSSD T7", "S5SX")
    assert d.model == "Samsung PSSD T7"
    assert d.bus_name == "USB"
    assert not d.removable_media


def test_device_descriptor_missing_strings() -> None:
    d = parse_device_descriptor(build_device_descriptor(bus_type=BUS_NVME, removable=True))
    assert d.model == "Unknown disk"
    assert d.serial == ""
    assert d.bus_name == "NVMe"
    assert d.removable_media


def test_device_descriptor_unknown_bus() -> None:
    assert parse_device_descriptor(build_device_descriptor(bus_type=0x42)).bus_name == "Bus type 66"


def test_device_descriptor_truncated() -> None:
    with pytest.raises(DiskDiscoveryError):
        parse_device_descriptor(b"\0" * 20)


def test_geometry() -> None:
    g = parse_geometry(build_geometry(4096, 2_000_398_934_016))
    assert (g.bytes_per_sector, g.disk_size) == (4096, 2_000_398_934_016)


def test_hotplug() -> None:
    assert parse_hotplug_info(build_hotplug(device_hotplug=True)).device_hotplug
    assert not parse_hotplug_info(build_hotplug(device_hotplug=False)).device_hotplug


def test_gpt_layout() -> None:
    disk_id = "0A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9"
    buf = build_gpt_layout(
        [
            GptEntry(1, 20480, 209_715_200, EFI_GUID, "EFI System Partition"),
            GptEntry(2, 209_735_680, 999_995_129_856, APFS_PARTITION_TYPE_GUID, "Untitled"),
            GptEntry(3, 10**12, 10**9, UNKNOWN_GUID),
        ],
        disk_id,
    )
    layout = parse_drive_layout(buf, 3)
    assert layout.style is PartitionStyle.GPT
    assert layout.disk_id == disk_id
    efi, apfs, unknown = layout.partitions
    assert (efi.number, efi.offset, efi.size, efi.type_label, efi.name) == (
        1, 20480, 209_715_200, "EFI System", "EFI System Partition",
    )  # fmt: skip
    assert apfs.type_id == APFS_PARTITION_TYPE_GUID
    assert apfs.type_label == "Apple APFS"
    assert apfs.has_apfs_type
    assert apfs.disk_number == 3
    assert unknown.type_label == "Unrecognized type"
    assert not unknown.has_apfs_type
    assert not efi.has_apfs_type


def test_gpt_layout_skips_zero_length_entries() -> None:
    buf = build_gpt_layout([GptEntry(1, 0, 0, BASIC_DATA_GUID), GptEntry(2, 1048576, 4096, BASIC_DATA_GUID)])
    assert [p.number for p in parse_drive_layout(buf, 1).partitions] == [2]


def test_mbr_layout_skips_empty_slots() -> None:
    buf = build_mbr_layout([(1, 1048576, 10**9, 0x07), (0, 0, 0, 0), (2, 10**9 + 1048576, 10**9, 0xAF), (0, 0, 0, 0)])
    layout = parse_drive_layout(buf, 2)
    assert layout.style is PartitionStyle.MBR
    assert [(p.number, p.type_id, p.type_label) for p in layout.partitions] == [
        (1, "0x07", "NTFS / exFAT"),
        (2, "0xAF", "Apple HFS/HFS+"),
    ]
    # MBR partitions are never treated as APFS, even Apple-typed ones.
    assert not any(p.has_apfs_type for p in layout.partitions)


def test_raw_layout() -> None:
    layout = parse_drive_layout(build_raw_layout(), 1)
    assert layout.style is PartitionStyle.RAW
    assert layout.partitions == ()


def test_layout_truncated() -> None:
    buf = build_gpt_layout([GptEntry(1, 20480, 4096, EFI_GUID)])
    with pytest.raises(DiskDiscoveryError):
        parse_drive_layout(buf[:-10], 1)


def test_volume_disk_extents() -> None:
    assert parse_volume_disk_extents(build_volume_extents([0])) == {0}
    assert parse_volume_disk_extents(build_volume_extents([1, 3])) == {1, 3}


def test_physical_drive_numbers_from_dos_devices() -> None:
    names = "C:\0PhysicalDrive0\0COM1\0PhysicalDrive2\0Harddisk0Partition1\0physicaldrive10\0PhysicalDriveX\0\0"
    assert parse_physical_drive_numbers(names) == [0, 2, 10]
