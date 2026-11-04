"""Locate the real, macOS-created APFS images fetched by scripts/fetch_test_images.py."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

IMAGE_DIR = Path(__file__).resolve().parent / "data" / "external"

SHA256 = {
    "apfs.raw": "e3e3adcbbf189403d892b013d6cba155f2e58e42ff5eb541ec681c37a91a3f29",
    "apfs.dmg": "3438860c7755de3939bca38b5b993bb5f84551fa2a72b0df498472521f03a82c",
    "apfs_encrypted.dmg": "33fe6f183aeb1a95fec68efdab17d59aedbad3d8ef1a117d411117376d9d8485",
}

# Facts about the images, cross-checked against raw extent bytes (not via the backend).
DMG_PARTITION_OFFSET = 20480
DMG_PARTITION_SIZE = 4_153_344
PASSWORDS_TXT = (
    b"place,user,password\nbank,joesmith,superrich\nalarm system,-,1234\n"
    b"treasure chest,-,1111\nuber secret laire,admin,admin\n"
)
A_FILE = b"This is a text file.\n\nWe should be able to parse it.\n"
ANOTHER_FILE = b"This is another file.\n"

FETCH_HINT = "real APFS images missing: run `python scripts/fetch_test_images.py`"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def image_path(name: str) -> Path:
    path = IMAGE_DIR / name
    if not path.exists():
        pytest.skip(FETCH_HINT)
    if sha256(path) != SHA256[name]:
        pytest.fail(f"{path} does not match its pinned SHA-256; re-run the fetch script")
    return path


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
