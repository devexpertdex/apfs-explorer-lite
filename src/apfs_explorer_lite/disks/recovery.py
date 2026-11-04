"""Find a usable container superblock when block 0 of an APFS container is damaged.

APFS keeps its authoritative superblock in the checkpoint descriptor area;
block 0 holds a copy. If block 0 has been overwritten (for example by another
file system's boot sector), the newest valid checkpoint superblock can be
used in its place, in memory only. Nothing here writes to the source.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from .apfs_signature import NX_MIN_BLOCK_SIZE, NxSuperblockDetails, parse_nx_details

ReadFunction = Callable[[int, int], bytes]  # (offset within container, length) -> bytes

PROBE_BYTES = 1024 * 1024
MAX_DESCRIPTOR_AREA = 64 * 1024 * 1024
READ_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class CheckpointSuperblock:
    offset: int  # byte offset within the container
    details: NxSuperblockDetails
    candidates: int  # valid superblocks seen


def _scan(data: bytes, base: int, step: int, found: dict[int, NxSuperblockDetails]) -> None:
    for i in range(0, len(data) - step + 1, step):
        details = parse_nx_details(data[i : i + step])
        if details is not None and details.checksum_valid:
            found[base + i] = details


def find_latest_superblock(read: ReadFunction, container_size: int) -> CheckpointSuperblock | None:
    """Newest valid superblock outside block 0, or None if there is none.

    Scans the first MiB, then reads the checkpoint descriptor area named by the
    superblocks found there. Only superblocks of the dominant container UUID
    are considered.
    """
    found: dict[int, NxSuperblockDetails] = {}
    probe = min(PROBE_BYTES, container_size - container_size % NX_MIN_BLOCK_SIZE)
    if probe <= NX_MIN_BLOCK_SIZE:
        return None
    _scan(read(0, probe), 0, NX_MIN_BLOCK_SIZE, found)
    found.pop(0, None)
    if not found:
        return None

    first = next(iter(found.values()))
    area = first.xp_desc_base * first.block_size
    length = first.xp_desc_blocks * first.block_size
    if 0 < length <= MAX_DESCRIPTOR_AREA and area + length <= container_size:
        for start in range(0, length, READ_CHUNK):
            chunk = read(area + start, min(READ_CHUNK, length - start))
            _scan(chunk, area + start, first.block_size, found)
    found.pop(0, None)

    uuid, _ = Counter(d.container_uuid for d in found.values()).most_common(1)[0]
    matching = {o: d for o, d in found.items() if d.container_uuid == uuid}
    offset, details = max(matching.items(), key=lambda item: item[1].xid)
    return CheckpointSuperblock(offset, details, len(matching))
