"""Parse a GUID Partition Table from raw bytes (used for disk image files).

Physical disks get their partition table from Windows
(IOCTL_DISK_GET_DRIVE_LAYOUT_EX); image files have no such API, so the GPT
header and entries are read directly. Read-only: callers supply a read
function.
"""

from __future__ import annotations

import struct
import uuid
import zlib
from collections.abc import Callable

from ..errors import DiskDiscoveryError
from ..models import Partition, PartitionStyle
from .parsing import partition_type_label

GPT_SIGNATURE = b"EFI PART"
_MAX_ENTRIES = 1024

ReadFunction = Callable[[int, int], bytes]  # (offset, length) -> bytes


def find_gpt_sector_size(read: ReadFunction) -> int | None:
    """512 or 4096 if a GPT header is present at LBA 1, else None."""
    for sector in (512, 4096):
        if read(sector, 8) == GPT_SIGNATURE:
            return sector
    return None


def parse_gpt(read: ReadFunction, sector_size: int, disk_number: int = -1) -> list[Partition]:
    header = read(sector_size, 92)
    if len(header) < 92 or header[:8] != GPT_SIGNATURE:
        raise DiskDiscoveryError("No GPT partition table was found.")
    (header_size,) = struct.unpack_from("<I", header, 12)
    (header_crc,) = struct.unpack_from("<I", header, 16)
    crc_input = bytearray(read(sector_size, header_size))
    crc_input[16:20] = b"\0\0\0\0"
    if zlib.crc32(crc_input) != header_crc:
        raise DiskDiscoveryError("The GPT header checksum is invalid.")

    entries_lba, count, entry_size = struct.unpack_from("<QII", header, 72)
    (entries_crc,) = struct.unpack_from("<I", header, 88)
    if not 0 < count <= _MAX_ENTRIES or entry_size < 128:
        raise DiskDiscoveryError("The GPT header describes an invalid entry table.")
    table = read(entries_lba * sector_size, count * entry_size)
    if zlib.crc32(table) != entries_crc:
        raise DiskDiscoveryError("The GPT partition entry checksum is invalid.")

    partitions: list[Partition] = []
    for index in range(count):
        entry = table[index * entry_size : (index + 1) * entry_size]
        type_guid = uuid.UUID(bytes_le=bytes(entry[:16]))
        if type_guid.int == 0:
            continue
        first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
        type_id = str(type_guid).upper()
        partitions.append(
            Partition(
                disk_number=disk_number,
                number=index + 1,
                offset=first_lba * sector_size,
                size=(last_lba - first_lba + 1) * sector_size,
                style=PartitionStyle.GPT,
                type_id=type_id,
                type_label=partition_type_label(PartitionStyle.GPT, type_id),
                name=bytes(entry[56:128]).decode("utf-16-le", "replace").split("\0", 1)[0],
                partition_id=str(uuid.UUID(bytes_le=bytes(entry[16:32]))).upper(),
            )
        )
    return partitions
