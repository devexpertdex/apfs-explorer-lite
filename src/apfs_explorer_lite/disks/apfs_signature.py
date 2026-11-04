"""Recognise an APFS container superblock (``nx_superblock_t``) in raw bytes.

Block 0 of an APFS container starts with a 32-byte ``obj_phys_t`` header
(Fletcher-64 checksum, oid, xid, type, subtype) followed by the magic
``NXSB``, the block size and the block count. See Apple's "Apple File System
Reference".
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass

NX_MAGIC = b"NXSB"
NX_MIN_BLOCK_SIZE = 4096
NX_MAX_BLOCK_SIZE = 65536
_FLETCHER_MOD = 0xFFFFFFFF
_SUPERBLOCK_HEADER_SIZE = 88  # through nx_uuid


@dataclass(frozen=True, slots=True)
class NxSuperblockInfo:
    block_size: int
    block_count: int
    container_uuid: str
    checksum_valid: bool

    @property
    def block_size_valid(self) -> bool:
        return is_valid_block_size(self.block_size)


def is_valid_block_size(block_size: int) -> bool:
    return (
        NX_MIN_BLOCK_SIZE <= block_size <= NX_MAX_BLOCK_SIZE
        and block_size & (block_size - 1) == 0
    )


def _fletcher_sums(data: bytes, sum1: int = 0, sum2: int = 0) -> tuple[int, int]:
    for (word,) in struct.iter_unpack("<I", data):
        sum1 += word
        sum2 += sum1
    return sum1 % _FLETCHER_MOD, sum2 % _FLETCHER_MOD


def verify_block_checksum(block: bytes) -> bool:
    """True if the stored checksum is valid: summing data then checksum yields 0."""
    if len(block) < 16 or len(block) % 4:
        return False
    sum1, sum2 = _fletcher_sums(block[8:])
    sum1, sum2 = _fletcher_sums(block[:8], sum1, sum2)
    return sum1 == 0 and sum2 == 0


@dataclass(frozen=True, slots=True)
class NxSuperblockDetails:
    """Fields of ``nx_superblock_t`` used to locate the latest checkpoint."""

    xid: int
    block_size: int
    block_count: int
    container_uuid: str
    next_xid: int
    xp_desc_base: int
    xp_desc_blocks: int
    xp_data_base: int
    xp_data_blocks: int
    omap_oid: int
    volume_oids: tuple[int, ...]
    checksum_valid: bool


def parse_nx_details(block: bytes) -> NxSuperblockDetails | None:
    """Detailed superblock fields (layout per Apple File System Reference)."""
    info = parse_nx_superblock(block)
    if info is None:
        return None
    (xid,) = struct.unpack_from("<Q", block, 16)
    (next_xid,) = struct.unpack_from("<Q", block, 96)
    desc_blocks, data_blocks, desc_base, data_base = struct.unpack_from("<IIQQ", block, 104)
    (omap_oid,) = struct.unpack_from("<Q", block, 160)
    (max_fs,) = struct.unpack_from("<I", block, 180)
    count = min(max_fs, 100)
    oids = struct.unpack_from(f"<{count}Q", block, 184) if count else ()
    return NxSuperblockDetails(
        xid=xid,
        block_size=info.block_size,
        block_count=info.block_count,
        container_uuid=info.container_uuid,
        next_xid=next_xid,
        xp_desc_base=desc_base,
        xp_desc_blocks=desc_blocks & 0x7FFFFFFF,
        xp_data_base=data_base,
        xp_data_blocks=data_blocks & 0x7FFFFFFF,
        omap_oid=omap_oid,
        volume_oids=tuple(o for o in oids if o),
        checksum_valid=info.checksum_valid,
    )


def parse_nx_superblock(data: bytes) -> NxSuperblockInfo | None:
    """Return superblock details, or ``None`` if ``data`` has no NXSB magic.

    ``checksum_valid`` is only True when ``data`` holds the full block.
    """
    if len(data) < _SUPERBLOCK_HEADER_SIZE or data[32:36] != NX_MAGIC:
        return None
    block_size, block_count = struct.unpack_from("<IQ", data, 36)
    container_uuid = str(uuid.UUID(bytes=bytes(data[72:88]))).upper()
    checksum_valid = (
        is_valid_block_size(block_size)
        and len(data) >= block_size
        and verify_block_checksum(data[:block_size])
    )
    return NxSuperblockInfo(block_size, block_count, container_uuid, checksum_valid)
