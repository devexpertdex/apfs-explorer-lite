from __future__ import annotations

import io
import struct

import pyfsapfs
import pytest

from apfs_explorer_lite.disks.apfs_signature import (
    is_valid_block_size,
    parse_nx_superblock,
    verify_block_checksum,
)

from .win_structs import build_nx_superblock, reference_fletcher64


@pytest.mark.parametrize("block_size", [4096, 16384])
def test_checksum_matches_reference_and_verifies(block_size: int) -> None:
    block = build_nx_superblock(block_size=block_size)
    assert struct.unpack_from("<Q", block)[0] == reference_fletcher64(block)
    assert verify_block_checksum(block)


def test_corrupted_block_fails_verification() -> None:
    block = bytearray(build_nx_superblock())
    block[1000] ^= 0xFF
    assert not verify_block_checksum(bytes(block))


def test_valid_superblock() -> None:
    info = parse_nx_superblock(build_nx_superblock(block_count=1234, container_uuid="00112233-4455-6677-8899-AABBCCDDEEFF"))
    assert info is not None
    assert (info.block_size, info.block_count) == (4096, 1234)
    assert info.container_uuid == "00112233-4455-6677-8899-AABBCCDDEEFF"
    assert info.checksum_valid


def test_bad_checksum_is_reported() -> None:
    info = parse_nx_superblock(build_nx_superblock(valid_checksum=False))
    assert info is not None and not info.checksum_valid


def test_no_magic_returns_none() -> None:
    assert parse_nx_superblock(build_nx_superblock(magic=b"NTFS")) is None
    assert parse_nx_superblock(b"\0" * 4096) is None
    assert parse_nx_superblock(b"") is None


def test_partial_block_cannot_be_checksum_verified() -> None:
    full = build_nx_superblock(block_size=16384)
    info = parse_nx_superblock(full[:4096])
    assert info is not None and info.block_size == 16384 and not info.checksum_valid


@pytest.mark.parametrize(("size", "ok"), [(4096, True), (65536, True), (2048, False), (131072, False), (12288, False)])
def test_block_size_validation(size: int, ok: bool) -> None:
    assert is_valid_block_size(size) is ok


def test_libfsapfs_agrees_on_signature_location() -> None:
    """Independent check: libfsapfs recognises our synthetic superblock."""
    assert pyfsapfs.check_container_signature_file_object(io.BytesIO(build_nx_superblock()))
    assert not pyfsapfs.check_container_signature_file_object(io.BytesIO(build_nx_superblock(magic=b"XXXX")))
