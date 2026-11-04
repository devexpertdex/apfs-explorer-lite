"""Recognise common file system signatures at the start of a partition (read-only helpers)."""

from __future__ import annotations

from .apfs_signature import NX_MAGIC, parse_nx_superblock


def identify_signature(head: bytes) -> str:
    """Name the file system whose signature is at the start of ``head``."""
    if len(head) < 1536:
        return f"too little data ({len(head)} bytes)"
    if head[32:36] == NX_MAGIC:
        info = parse_nx_superblock(head)
        checksum = "valid" if info and info.checksum_valid else "INVALID or partial block"
        size = f", block size {info.block_size}, {info.block_count:,} blocks" if info else ""
        return f"APFS container superblock (checksum {checksum}{size})"
    if head[3:11] == b"NTFS    ":
        return "NTFS volume"
    if head[3:11] == b"EXFAT   ":
        return "exFAT volume"
    if head[82:90] == b"FAT32   " or head[54:62] in (b"FAT16   ", b"FAT12   "):
        return "FAT volume"
    if head[1024:1026] in (b"H+", b"HX"):
        return "HFS+ volume (Mac OS Extended)"
    if head[:8] == b"PRIVHEAD" or head[:8] == b"TOCBLOCK":
        return "Windows dynamic disk (LDM) metadata"
    if head[:4] == b"\x00\x00\x00\x00" and not any(head):
        return "all zeros (empty, wiped, or encrypted at a lower level)"
    if head[:4] == b"LUKS":
        return "LUKS encrypted volume"
    return "unrecognized data"


def classify_sector(sector: bytes) -> str:
    if not any(sector):
        return "zeros"
    if sector[3:11] == b"EXFAT   ":
        return "exFAT boot sector"
    if sector[32:36] == NX_MAGIC:
        return "APFS superblock header"
    if sector[-2:] == b"\x55\xaa":
        return "ends with 55AA (boot-region sector)"
    if len(set(sector[i : i + 4] for i in range(0, len(sector), 4))) == 1:
        return f"repeating 4-byte value {sector[:4].hex()} (exFAT boot checksum?)"
    return "other data"
