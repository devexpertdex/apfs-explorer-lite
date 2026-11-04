"""Builders for mocked Windows IOCTL output and APFS blocks (test-only).

Each builder packs the documented x64 structure layout directly, independent
of the production parsers, so the tests check the parsers against the spec
rather than against themselves.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field

from apfs_explorer_lite.disks.api import RawDiskMetadata
from apfs_explorer_lite.errors import DiskDiscoveryError
from apfs_explorer_lite.models import APFS_PARTITION_TYPE_GUID

EFI_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
BASIC_DATA_GUID = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
MSR_GUID = "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"
UNKNOWN_GUID = "11111111-2222-3333-4444-555555555555"

BUS_USB = 0x07
BUS_SATA = 0x0B
BUS_NVME = 0x11
BUS_1394 = 0x04


def build_device_descriptor(
    *,
    vendor: str = "",
    product: str = "",
    revision: str = "",
    serial: str = "",
    bus_type: int = BUS_USB,
    removable: bool = False,
) -> bytes:
    header_size = 40  # 36 bytes of fields + RawDeviceProperties[1], padded
    strings = bytearray()
    offsets = []
    for text in (vendor, product, revision, serial):
        if text:
            offsets.append(header_size + len(strings))
            strings += text.encode("ascii") + b"\0"
        else:
            offsets.append(0)
    total = header_size + len(strings)
    header = struct.pack(
        "<IIBBBBIIIIII4x", 1, total, 0, 0, int(removable), 0, *offsets, bus_type, 0
    )
    return header + bytes(strings)


def build_geometry(bytes_per_sector: int, disk_size: int) -> bytes:
    cylinders = disk_size // (255 * 63 * bytes_per_sector)
    return struct.pack("<qIIIIq", cylinders, 12, 255, 63, bytes_per_sector, disk_size) + b"\0" * 8


@dataclass
class GptEntry:
    number: int
    offset: int
    length: int
    type_guid: str
    name: str = ""
    partition_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def _partition_prefix(style: int, entry_offset: int, length: int, number: int) -> bytes:
    return struct.pack("<I4xqqIBB2x", style, entry_offset, length, number, 0, 0)


def build_gpt_layout(entries: list[GptEntry], disk_id: str | None = None) -> bytes:
    disk_guid = uuid.UUID(disk_id) if disk_id else uuid.uuid4()
    out = bytearray(struct.pack("<II", 1, len(entries)))
    out += disk_guid.bytes_le + struct.pack("<qqI4x", 17408, 10**12, 128)
    assert len(out) == 48
    for e in entries:
        entry = bytearray(_partition_prefix(1, e.offset, e.length, e.number))
        entry += uuid.UUID(e.type_guid).bytes_le + uuid.UUID(e.partition_id).bytes_le
        entry += struct.pack("<Q", 0)
        entry += e.name.encode("utf-16-le").ljust(72, b"\0")
        assert len(entry) == 144
        out += entry
    return bytes(out)


def build_mbr_layout(entries: list[tuple[int, int, int, int]]) -> bytes:
    """``entries`` are (number, offset, length, mbr_type); type 0 = empty slot."""
    out = bytearray(struct.pack("<II", 0, len(entries)))
    out += struct.pack("<II", 0xDEADBEEF, 0) + b"\0" * 32
    for number, offset, length, mbr_type in entries:
        entry = bytearray(_partition_prefix(0, offset, length, number))
        entry += struct.pack("<BBBxI", mbr_type, 0, 1, offset // 512) + uuid.uuid4().bytes_le
        entry += b"\0" * (112 - 24)
        assert len(entry) == 144
        out += entry
    return bytes(out)


def build_raw_layout() -> bytes:
    return struct.pack("<II", 2, 0) + b"\0" * 40


def build_hotplug(*, device_hotplug: bool, media_removable: bool = False) -> bytes:
    return struct.pack("<IBBBB", 8, int(media_removable), 0, int(device_hotplug), 0)


def build_volume_extents(disk_numbers: list[int]) -> bytes:
    out = struct.pack("<I4x", len(disk_numbers))
    for n in disk_numbers:
        out += struct.pack("<I4xqq", n, 1048576, 10**9)
    return out


def reference_fletcher64(block: bytes) -> int:
    """Straightforward per-step modular Fletcher-64, as in Apple's reference."""
    mod = 0xFFFFFFFF
    sum1 = sum2 = 0
    for (word,) in struct.iter_unpack("<I", block[8:]):
        sum1 = (sum1 + word) % mod
        sum2 = (sum2 + sum1) % mod
    c1 = mod - ((sum1 + sum2) % mod)
    c2 = mod - ((sum1 + c1) % mod)
    return (c2 << 32) | c1


def build_nx_superblock(
    *,
    block_size: int = 4096,
    block_count: int = 244_190_646,
    container_uuid: str = "5A1C3C3E-0D4F-4F5E-9C3B-1A2B3C4D5E6F",
    valid_checksum: bool = True,
    magic: bytes = b"NXSB",
) -> bytes:
    block = bytearray(block_size)
    struct.pack_into("<QQII", block, 8, 1, 7, 0x80000001, 0)  # oid, xid, type, subtype
    block[32:36] = magic
    struct.pack_into("<IQ", block, 36, block_size, block_count)
    block[72:88] = uuid.UUID(container_uuid).bytes
    for i in range(256, block_size, 97):  # some non-zero payload
        block[i] = (i * 31) & 0xFF
    checksum = reference_fletcher64(bytes(block))
    if not valid_checksum:
        checksum ^= 0x1
    struct.pack_into("<Q", block, 0, checksum)
    return bytes(block)


# --- fake DiskApi -------------------------------------------------------------


@dataclass
class FakeDisk:
    metadata: RawDiskMetadata | DiskDiscoveryError
    content: dict[int, bytes] = field(default_factory=dict)  # offset -> bytes
    read_error: DiskDiscoveryError | None = None


def make_disk(
    *,
    bus_type: int = BUS_USB,
    model: tuple[str, str] = ("Samsung", "PSSD T7"),
    serial: str = "S5SXNG0R123456",
    sector: int = 512,
    size: int = 1_000_204_886_016,
    layout: bytes | None = None,
    hotplug: bool | None = False,
    removable: bool = False,
    content: dict[int, bytes] | None = None,
    read_error: DiskDiscoveryError | None = None,
) -> FakeDisk:
    metadata = RawDiskMetadata(
        descriptor=build_device_descriptor(
            vendor=model[0], product=model[1], serial=serial, bus_type=bus_type, removable=removable
        ),
        geometry=build_geometry(sector, size),
        layout=layout if layout is not None else build_raw_layout(),
        hotplug=None if hotplug is None else build_hotplug(device_hotplug=hotplug),
    )
    return FakeDisk(metadata, content or {}, read_error)


def apfs_disk_layout(apfs_offset: int = 209_735_680, apfs_size: int = 999_995_129_856) -> bytes:
    """GPT layout of a Mac-formatted external SSD: EFI + one APFS container."""
    return build_gpt_layout(
        [
            GptEntry(1, 20480, 209_715_200, EFI_GUID, "EFI System Partition"),
            GptEntry(2, apfs_offset, apfs_size, APFS_PARTITION_TYPE_GUID, ""),
        ]
    )


class FakeDiskApi:
    def __init__(
        self,
        disks: dict[int, FakeDisk],
        *,
        system: set[int] | DiskDiscoveryError = frozenset({0}),  # type: ignore[assignment]
        admin: bool = True,
        list_error: DiskDiscoveryError | None = None,
    ) -> None:
        self.disks = disks
        self.system = system
        self.admin = admin
        self.list_error = list_error
        self.metadata_queries: list[int] = []
        self.reads: list[tuple[int, int, int]] = []

    def list_physical_drive_numbers(self) -> list[int]:
        if self.list_error:
            raise self.list_error
        return sorted(self.disks)

    def system_disk_numbers(self) -> set[int]:
        if isinstance(self.system, DiskDiscoveryError):
            raise self.system
        return set(self.system)

    def is_admin(self) -> bool:
        return self.admin

    def query_metadata(self, disk_number: int) -> RawDiskMetadata:
        self.metadata_queries.append(disk_number)
        meta = self.disks[disk_number].metadata
        if isinstance(meta, DiskDiscoveryError):
            raise meta
        return meta

    def read_bytes(self, disk_number: int, offset: int, length: int) -> bytes:
        self.reads.append((disk_number, offset, length))
        disk = self.disks[disk_number]
        if disk.read_error:
            raise disk.read_error
        return disk.content.get(offset, b"").ljust(length, b"\0")[:length]
