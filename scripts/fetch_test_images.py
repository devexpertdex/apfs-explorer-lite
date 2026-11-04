"""Download the real APFS test images used by tests/test_real_apfs_images.py.

The images were created on macOS by the dfVFS and Plaso projects (Apache-2.0)
and are pinned to exact commits and SHA-256 hashes. They are stored in
tests/data/external/ (git-ignored) and are opened read-only by the tests.

    python scripts/fetch_test_images.py
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

DEST = Path(__file__).resolve().parents[1] / "tests" / "data" / "external"

IMAGES = {
    "apfs.raw": (
        "https://raw.githubusercontent.com/log2timeline/dfvfs/"
        "a28741fa310faabeb18fa643939d537f975e1ebd/test_data/apfs.raw",
        "e3e3adcbbf189403d892b013d6cba155f2e58e42ff5eb541ec681c37a91a3f29",
    ),
    "apfs.dmg": (
        "https://raw.githubusercontent.com/log2timeline/plaso/"
        "c71b6b6ea4c9c93682702a94893dc69d2b57559b/test_data/apfs.dmg",
        "3438860c7755de3939bca38b5b993bb5f84551fa2a72b0df498472521f03a82c",
    ),
    "apfs_encrypted.dmg": (
        "https://raw.githubusercontent.com/log2timeline/dfvfs/"
        "b7d6282044a65e1d192ba40dacb94f9757d835a6/test_data/apfs_encrypted.dmg",
        "33fe6f183aeb1a95fec68efdab17d59aedbad3d8ef1a117d411117376d9d8485",
    ),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    failed = False
    for name, (url, expected) in IMAGES.items():
        target = DEST / name
        if target.exists() and sha256(target) == expected:
            print(f"ok       {name}")
            continue
        print(f"download {name}")
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - pinned https URL
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            print(f"ERROR    {name}: SHA-256 mismatch, not saved", file=sys.stderr)
            failed = True
            continue
        target.write_bytes(data)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
