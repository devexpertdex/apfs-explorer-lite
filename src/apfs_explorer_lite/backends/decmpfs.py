"""Detect APFS transparent (decmpfs) compression from the ``com.apple.decmpfs`` xattr.

Header layout: ``"fpmc"`` magic (``cmpf`` little-endian), compression type
(uint32 LE), uncompressed size (uint64 LE).

Method support follows the libfsapfs README (checked 2026-11-03): ZLIB (3, 4),
LZVN (7, 8), raw (9, 10) and LZFSE (11, 12) are listed as supported; type 1
("uncompressed") and LZBitmap (13, 14) are listed as unsupported. The wiki
home page disagrees about LZFSE and raw, and no compressed sample has been
tested yet, so reads of compressed files are length-checked as well.
"""

from __future__ import annotations

import struct

from ..models import CompressionInfo

DECMPFS_XATTR = "com.apple.decmpfs"
DECMPFS_MAGIC = b"fpmc"
DECMPFS_HEADER_SIZE = 16

METHOD_NAMES: dict[int, str] = {
    1: "uncompressed (inline)",
    3: "ZLIB (inline)",
    4: "ZLIB (resource fork)",
    5: "sparse",
    7: "LZVN (inline)",
    8: "LZVN (resource fork)",
    9: "raw (inline)",
    10: "raw (resource fork)",
    11: "LZFSE (inline)",
    12: "LZFSE (resource fork)",
    13: "LZBitmap (inline)",
    14: "LZBitmap (resource fork)",
}
SUPPORTED_METHODS = frozenset({3, 4, 7, 8, 9, 10, 11, 12})


def parse_decmpfs_header(data: bytes) -> CompressionInfo | None:
    """Return compression details, or None if ``data`` is not a decmpfs header."""
    if len(data) < DECMPFS_HEADER_SIZE or data[:4] != DECMPFS_MAGIC:
        return None
    method, uncompressed_size = struct.unpack_from("<IQ", data, 4)
    return CompressionInfo(
        method=method,
        method_name=METHOD_NAMES.get(method, f"unknown method {method}"),
        uncompressed_size=uncompressed_size,
        supported=method in SUPPORTED_METHODS,
    )
