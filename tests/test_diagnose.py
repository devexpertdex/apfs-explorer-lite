from __future__ import annotations

import pytest

from apfs_explorer_lite.diagnose import hexdump, identify

from .win_structs import build_nx_superblock


def _block(offset: int, signature: bytes, size: int = 8192) -> bytes:
    data = bytearray(size)
    data[offset : offset + len(signature)] = signature
    data[600] = 1  # not all zeros
    return bytes(data)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (build_nx_superblock() + bytes(4096), "APFS container superblock (checksum valid"),
        (build_nx_superblock(valid_checksum=False) + bytes(4096), "checksum INVALID"),
        (_block(3, b"NTFS    "), "NTFS volume"),
        (_block(3, b"EXFAT   "), "exFAT volume"),
        (_block(82, b"FAT32   "), "FAT volume"),
        (_block(1024, b"H+"), "HFS+ volume"),
        (_block(0, b"PRIVHEAD"), "dynamic disk (LDM)"),
        (bytes(8192), "all zeros"),
        (_block(100, b"random"), "unrecognized data"),
        (b"short", "too little data"),
    ],
)
def test_identify(data: bytes, expected: str) -> None:
    assert expected in identify(data)


def test_hexdump() -> None:
    (line,) = hexdump(b"NXSB\x00\x10")
    assert "4e 58 53 42 00 10" in line and line.rstrip().endswith("NXSB..")
