"""Parsers for the Windows storage structures returned by DeviceIoControl.

Layouts follow the x64 definitions in winioctl.h:

* ``STORAGE_DEVICE_DESCRIPTOR`` (IOCTL_STORAGE_QUERY_PROPERTY)
* ``DISK_GEOMETRY_EX`` (IOCTL_DISK_GET_DRIVE_GEOMETRY_EX)
* ``DRIVE_LAYOUT_INFORMATION_EX`` / ``PARTITION_INFORMATION_EX``
  (IOCTL_DISK_GET_DRIVE_LAYOUT_EX)
* ``STORAGE_HOTPLUG_INFO`` (IOCTL_STORAGE_GET_HOTPLUG_INFO)
* ``VOLUME_DISK_EXTENTS`` (IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS)
"""

from __future__ import annotations

import re
import struct
import uuid
from dataclasses import dataclass

from ..errors import DiskDiscoveryError
from ..models import (
    APFS_ISC_PARTITION_TYPE_GUID,
    APFS_PARTITION_TYPE_GUID,
    APFS_RECOVERY_PARTITION_TYPE_GUID,
    Partition,
    PartitionStyle,
)

# STORAGE_BUS_TYPE
BUS_TYPE_NAMES: dict[int, str] = {
    0x00: "Unknown",
    0x01: "SCSI",
    0x02: "ATAPI",
    0x03: "ATA",
    0x04: "IEEE 1394",
    0x05: "SSA",
    0x06: "Fibre Channel",
    0x07: "USB",
    0x08: "RAID",
    0x09: "iSCSI",
    0x0A: "SAS",
    0x0B: "SATA",
    0x0C: "SD",
    0x0D: "MMC",
    0x0E: "Virtual",
    0x0F: "File-backed virtual",
    0x10: "Storage Spaces",
    0x11: "NVMe",
    0x12: "SCM",
    0x13: "UFS",
}
BUS_TYPE_USB = 0x07
EXTERNAL_BUS_TYPES = frozenset({0x04, BUS_TYPE_USB, 0x0C, 0x0D})

GPT_TYPE_LABELS: dict[str, str] = {
    APFS_PARTITION_TYPE_GUID: "Apple APFS",
    APFS_ISC_PARTITION_TYPE_GUID: "Apple APFS (ISC)",
    APFS_RECOVERY_PARTITION_TYPE_GUID: "Apple APFS (Recovery)",
    "48465300-0000-11AA-AA11-00306543ECAC": "Apple HFS+",
    "426F6F74-0000-11AA-AA11-00306543ECAC": "Apple Boot",
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EFI System",
    "E3C9E316-0B5C-4DB8-817D-F92DF00215AE": "Microsoft Reserved",
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "Microsoft Basic Data",
    "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": "Windows Recovery",
    "5808C8AA-7E8F-42E0-85D2-E1E90434CFB3": "Windows LDM Metadata",
    "AF9B60A0-1431-4F62-BC68-3311714A69AD": "Windows LDM Data",
    "E75CAF8F-F680-4CEE-AFA3-B001E56EFC2D": "Windows Storage Spaces",
    "0FC63DAF-8483-4772-8E79-3D69D8477DE4": "Linux Filesystem",
    "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F": "Linux Swap",
    "E6D6D379-F507-44C2-A23C-238F2A3DF928": "Linux LVM",
}

MBR_TYPE_LABELS: dict[int, str] = {
    0x01: "FAT12",
    0x04: "FAT16",
    0x05: "Extended",
    0x06: "FAT16",
    0x07: "NTFS / exFAT",
    0x0B: "FAT32",
    0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)",
    0x0F: "Extended (LBA)",
    0x27: "Windows Recovery",
    0x82: "Linux Swap",
    0x83: "Linux",
    0xAF: "Apple HFS/HFS+",
    0xEE: "GPT Protective",
    0xEF: "EFI System",
}

_PARTITION_STYLES = {0: PartitionStyle.MBR, 1: PartitionStyle.GPT, 2: PartitionStyle.RAW}

# x64 sizes: DRIVE_LAYOUT_INFORMATION_EX header (2 DWORDs + 40-byte union)
# and PARTITION_INFORMATION_EX.
_LAYOUT_HEADER_SIZE = 48
_PARTITION_ENTRY_SIZE = 144
_PHYSICAL_DRIVE_NAME = re.compile(r"^PhysicalDrive(\d+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DeviceDescriptor:
    vendor: str
    product: str
    revision: str
    serial: str
    bus_type: int
    removable_media: bool

    @property
    def bus_name(self) -> str:
        return BUS_TYPE_NAMES.get(self.bus_type, f"Bus type {self.bus_type}")

    @property
    def model(self) -> str:
        return " ".join(part for part in (self.vendor, self.product) if part) or "Unknown disk"


@dataclass(frozen=True, slots=True)
class DiskGeometry:
    bytes_per_sector: int
    disk_size: int


@dataclass(frozen=True, slots=True)
class HotplugInfo:
    media_removable: bool
    media_hotplug: bool
    device_hotplug: bool


@dataclass(frozen=True, slots=True)
class DriveLayout:
    style: PartitionStyle
    partitions: tuple[Partition, ...]
    disk_id: str | None = None


def _require_length(buf: bytes, needed: int, what: str) -> None:
    if len(buf) < needed:
        raise DiskDiscoveryError(
            f"Windows returned an incomplete {what}.", detail=f"{len(buf)} < {needed} bytes"
        )


def _c_string(buf: bytes, offset: int) -> str:
    if offset == 0 or offset >= len(buf):
        return ""
    end = buf.find(b"\0", offset)
    raw = buf[offset:] if end == -1 else buf[offset:end]
    return raw.decode("ascii", errors="replace").strip()


def _guid(buf: bytes, offset: int) -> str:
    return str(uuid.UUID(bytes_le=bytes(buf[offset : offset + 16]))).upper()


def parse_device_descriptor(buf: bytes) -> DeviceDescriptor:
    _require_length(buf, 36, "storage device descriptor")
    (_, _, _, _, removable, _, vendor_off, product_off, rev_off, serial_off, bus_type, _) = (
        struct.unpack_from("<IIBBBBIIIIII", buf, 0)
    )
    return DeviceDescriptor(
        vendor=_c_string(buf, vendor_off),
        product=_c_string(buf, product_off),
        revision=_c_string(buf, rev_off),
        serial=_c_string(buf, serial_off),
        bus_type=bus_type,
        removable_media=bool(removable),
    )


def parse_geometry(buf: bytes) -> DiskGeometry:
    _require_length(buf, 32, "disk geometry")
    (bytes_per_sector,) = struct.unpack_from("<I", buf, 20)
    (disk_size,) = struct.unpack_from("<q", buf, 24)
    return DiskGeometry(bytes_per_sector, disk_size)


def parse_hotplug_info(buf: bytes) -> HotplugInfo:
    _require_length(buf, 8, "hotplug information")
    return HotplugInfo(bool(buf[4]), bool(buf[5]), bool(buf[6]))


def partition_type_label(style: PartitionStyle, type_id: str) -> str:
    if style is PartitionStyle.GPT:
        return GPT_TYPE_LABELS.get(type_id, "Unrecognized type")
    if style is PartitionStyle.MBR:
        return MBR_TYPE_LABELS.get(int(type_id, 16), "Unrecognized type")
    return "Unknown"


def parse_drive_layout(buf: bytes, disk_number: int) -> DriveLayout:
    _require_length(buf, 8, "drive layout")
    style_raw, count = struct.unpack_from("<II", buf, 0)
    style = _PARTITION_STYLES.get(style_raw, PartitionStyle.UNKNOWN)
    if style in (PartitionStyle.RAW, PartitionStyle.UNKNOWN):
        return DriveLayout(style, ())
    _require_length(buf, _LAYOUT_HEADER_SIZE + count * _PARTITION_ENTRY_SIZE, "drive layout")

    disk_id = _guid(buf, 8) if style is PartitionStyle.GPT else None
    partitions: list[Partition] = []
    for index in range(count):
        base = _LAYOUT_HEADER_SIZE + index * _PARTITION_ENTRY_SIZE
        (entry_style,) = struct.unpack_from("<I", buf, base)
        offset, length = struct.unpack_from("<qq", buf, base + 8)
        (number,) = struct.unpack_from("<I", buf, base + 24)
        info = base + 32  # PARTITION_INFORMATION_EX union
        if length <= 0:
            continue
        if entry_style == 1:  # GPT
            type_id = _guid(buf, info)
            partition_id: str | None = _guid(buf, info + 16)
            name = bytes(buf[info + 40 : info + 112]).decode("utf-16-le", "replace").split("\0", 1)[0]
            part_style = PartitionStyle.GPT
        elif entry_style == 0:  # MBR
            mbr_type = buf[info]
            if mbr_type == 0:
                continue
            type_id = f"0x{mbr_type:02X}"
            partition_id = _guid(buf, info + 8)
            name = ""
            part_style = PartitionStyle.MBR
        else:
            continue
        partitions.append(
            Partition(
                disk_number=disk_number,
                number=number,
                offset=offset,
                size=length,
                style=part_style,
                type_id=type_id,
                type_label=partition_type_label(part_style, type_id),
                name=name,
                partition_id=partition_id,
            )
        )
    return DriveLayout(style, tuple(partitions), disk_id)


def parse_volume_disk_extents(buf: bytes) -> set[int]:
    _require_length(buf, 8, "volume disk extents")
    (count,) = struct.unpack_from("<I", buf, 0)
    _require_length(buf, 8 + count * 24, "volume disk extents")
    return {struct.unpack_from("<I", buf, 8 + i * 24)[0] for i in range(count)}


def parse_physical_drive_numbers(dos_device_names: str) -> list[int]:
    """Extract N from ``PhysicalDriveN`` entries of a QueryDosDevice list."""
    numbers = set()
    for name in dos_device_names.split("\0"):
        match = _PHYSICAL_DRIVE_NAME.match(name)
        if match:
            numbers.add(int(match.group(1)))
    return sorted(numbers)
