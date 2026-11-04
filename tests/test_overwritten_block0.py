"""A real APFS image whose block 0 was overwritten by an exFAT boot sector.

This mirrors a real drive seen in the field. The damage is applied to a
temporary COPY of the macOS-created test image; the original is untouched.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path, PurePosixPath

import pytest

from apfs_explorer_lite.backends.libfsapfs_backend import LibfsapfsBackend
from apfs_explorer_lite.diagnose import deep_check, search_superblocks
from apfs_explorer_lite.disks.gpt import parse_gpt
from apfs_explorer_lite.errors import ApfsContainerOpenError
from apfs_explorer_lite.models import SourceDevice
from apfs_explorer_lite.source_io import OverlayReader, PythonFileSectorReader

from .real_images import DMG_PARTITION_OFFSET, PASSWORDS_TXT, image_path

EXFAT_BOOT = bytes.fromhex("eb7690") + b"EXFAT   " + bytes(501) + b"\x55\xaa"


@pytest.fixture
def damaged(tmp_path: Path) -> Path:
    copy = tmp_path / "damaged.dmg"
    shutil.copyfile(image_path("apfs.dmg"), copy)
    with copy.open("r+b") as f:  # only the temporary copy is modified
        f.seek(DMG_PARTITION_OFFSET)
        f.write(EXFAT_BOOT + bytes(4096 - len(EXFAT_BOOT)))
    return copy


def partition_of(path: Path):  # noqa: ANN201
    data = path.read_bytes()
    (partition,) = parse_gpt(lambda o, n: data[o : o + n], 512)
    return partition


def test_normal_open_fails(damaged: Path) -> None:
    p = partition_of(damaged)
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(damaged))
    with pytest.raises(ApfsContainerOpenError, match="No APFS container"):
        backend.open(SourceDevice(str(damaged), p.offset, p.size))


def test_overlay_with_checkpoint_superblock_reads_files(damaged: Path) -> None:
    p = partition_of(damaged)
    reader = PythonFileSectorReader(damaged)
    hits = search_superblocks(reader, p)
    reader.close()
    assert hits and 0 not in hits
    out = io.StringIO()
    reader = PythonFileSectorReader(damaged)
    head = reader.read_at(p.offset, 8192)
    deep_check(reader, p, head, hits, out,
               reopen=lambda: PythonFileSectorReader(damaged), source_path=str(damaged))  # fmt: skip
    reader.close()
    report = out.getvalue()
    assert "exFAT boot sector" in report
    assert "newest is checkpoint 6" in report
    assert "'SingleVolume'" in report
    assert "readable with the in-memory fix" in report

    # And the content of a real file is correct through the overlay.
    block = PythonFileSectorReader(damaged).read_at(p.offset + hits[0], 4096)
    backend = LibfsapfsBackend(lambda _s: OverlayReader(PythonFileSectorReader(damaged), p.offset, block))
    backend.open(SourceDevice(str(damaged), p.offset, p.size))
    try:
        with backend.open_file(0, PurePosixPath("/passwords.txt")) as stream:  # type: ignore[attr-defined]
            assert stream.read() == PASSWORDS_TXT
    finally:
        backend.close()


def test_backend_superblock_offset_reads_files(damaged: Path) -> None:
    from apfs_explorer_lite.disks.recovery import find_latest_superblock

    p = partition_of(damaged)
    reader = PythonFileSectorReader(damaged)
    found = find_latest_superblock(lambda o, n: reader.read_at(p.offset + o, n), p.size)
    reader.close()
    assert found is not None and found.offset > 0 and found.details.checksum_valid
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(damaged))
    backend.open(SourceDevice(str(damaged), p.offset, p.size, superblock_offset=found.offset))
    try:
        assert [v.name for v in backend.list_volumes()] == ["SingleVolume"]
        with backend.open_file(0, PurePosixPath("/passwords.txt")) as stream:  # type: ignore[attr-defined]
            assert stream.read() == PASSWORDS_TXT
    finally:
        backend.close()


def test_invalid_superblock_offset_is_rejected(damaged: Path) -> None:
    p = partition_of(damaged)
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(damaged))
    with pytest.raises(ApfsContainerOpenError, match="backup container superblock"):
        backend.open(SourceDevice(str(damaged), p.offset, p.size, superblock_offset=4096 * 40))


def test_acceptance_tool_on_damaged_image(damaged: Path) -> None:
    from apfs_explorer_lite.verify import main as verify_main

    out = io.StringIO()
    assert verify_main(["--image", str(damaged)], out=out) == 0
    text = out.getvalue()
    assert "first block damaged" in text and "RESULT: PASS" in text


def test_overlay_reader_patches_only_in_memory(tmp_path: Path) -> None:
    path = tmp_path / "img.bin"
    path.write_bytes(bytes(range(256)) * 32)
    overlay = OverlayReader(PythonFileSectorReader(path), 100, b"XYZ")
    assert overlay.read_at(98, 7) == bytes([98, 99]) + b"XYZ" + bytes([103, 104])
    assert overlay.read_at(0, 50) == bytes(range(50))
    assert overlay.read_at(101, 3) == b"YZ" + bytes([103])
    overlay.close()
    assert path.read_bytes()[100:103] == bytes([100, 101, 102])
