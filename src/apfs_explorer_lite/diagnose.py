"""Read-only drive diagnostic: what is really at the start of each partition?

For every external, non-system disk (or the one given with --disk) this prints
the partition table and identifies the data at the start of each partition.
For APFS-labelled partitions without a container superblock at the start, it
also searches the first 64 MiB of the partition for one. Only reads are
performed; the report is written to the console and, with --out, to a text
file on the PC (never to the disk being examined).

    python -m apfs_explorer_lite.diagnose --out drive-report.txt    (as administrator)
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TextIO

from . import APP_NAME, __version__
from .backends.libfsapfs_backend import LibfsapfsBackend
from .disks.apfs_signature import NX_MAGIC, verify_block_checksum
from .disks.discovery import WindowsDiskDiscovery
from .disks.recovery import find_latest_superblock
from .disks.signatures import classify_sector
from .disks.signatures import identify_signature as identify
from .disks.win32 import Win32DiskApi
from .errors import ApfsExplorerError
from .models import Partition, SourceDevice, physical_drive_path
from .source_io import SectorReader

HEAD_BYTES = 8192
SEARCH_BYTES = 64 * 1024 * 1024
SEARCH_CHUNK = 1024 * 1024
MAX_HITS = 10




def hexdump(data: bytes, width: int = 16) -> list[str]:
    lines = []
    for offset in range(0, len(data), width):
        row = data[offset : offset + width]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"    {offset:04x}  {row.hex(' '):<{width * 3}} {text}")
    return lines




def describe_runs(data: bytes, unit: int, classify: Callable[[bytes], str]) -> list[str]:
    """Run-length summary like '0-11 (12 x 512 bytes): exFAT ...'.

    Blocks of APFS metadata interleaved with other data are merged into one run.
    """
    lines: list[str] = []
    labels = [classify(data[i : i + unit]) for i in range(0, len(data) - unit + 1, unit)]
    labels = ["valid APFS metadata" if label == "other data" and 0 < i < len(labels) - 1
              and labels[i - 1] == labels[i + 1] == "valid APFS metadata" else label
              for i, label in enumerate(labels)]  # fmt: skip
    start = 0
    for index in range(1, len(labels) + 1):
        if index == len(labels) or labels[index] != labels[start]:
            lines.append(f"{start}-{index - 1} ({index - start} x {unit} bytes): {labels[start]}")
            start = index
    return lines


def classify_block(block: bytes) -> str:
    if not any(block):
        return "zeros"
    if verify_block_checksum(block):
        return "valid APFS metadata"
    label = classify_sector(block[:512])
    return "other data" if label.startswith("ends with") or label.startswith("repeating") else label


def exfat_root_entries(reader: SectorReader, partition: Partition, boot: bytes) -> list[str]:
    """Read the exFAT root directory named by ``boot`` and describe its entries."""
    (heap_offset,) = struct.unpack_from("<I", boot, 88)
    (root_cluster,) = struct.unpack_from("<I", boot, 96)
    bps = 1 << boot[108]
    spc = 1 << boot[109]
    offset = partition.offset + (heap_offset + (root_cluster - 2) * spc) * bps
    length = min(spc * bps, 64 * 1024)
    if offset + length > partition.offset + partition.size or offset % reader.sector_size:
        return [f"root directory location {offset:,} is outside the partition"]
    data = reader.read_at(offset, length)
    lines = [f"root directory at disk offset {offset:,}; first block: {classify_block(data[:4096])}"]
    names: list[str] = []
    counts: dict[str, int] = {}
    current = ""
    for i in range(0, len(data), 32):
        kind = data[i]
        if kind == 0x00:
            break
        label = {0x81: "allocation bitmap", 0x82: "upcase table", 0x83: "volume label",
                 0x85: "file/folder", 0xC0: "stream", 0xC1: "file name"}.get(kind, f"other {kind:#x}")  # fmt: skip
        counts[label] = counts.get(label, 0) + 1
        if kind == 0x85 and current:
            names.append(current)
            current = ""
        if kind == 0xC1:
            current += data[i + 2 : i + 32].decode("utf-16-le", "replace").rstrip("\0")
        if kind == 0x83:
            lines.append(f"volume label: {data[i + 2 : i + 2 + 2 * data[i + 1]].decode('utf-16-le', 'replace')!r}")
    if current:
        names.append(current)
    lines.append(f"entry counts: {counts or 'none (directory empty or not exFAT)'}")
    if names:
        lines.append(f"{len(names)} named entries (names not shown)")
    return lines


def search_superblocks(reader: SectorReader, partition: Partition) -> list[int]:
    """Offsets (relative to the partition) of 4 KiB blocks that start with NXSB."""
    hits: list[int] = []
    limit = min(SEARCH_BYTES, partition.size)
    for start in range(0, limit, SEARCH_CHUNK):
        length = min(SEARCH_CHUNK, limit - start)
        length -= length % reader.sector_size
        if length <= 0:
            break
        chunk = reader.read_at(partition.offset + start, length)
        for block in range(0, len(chunk), 4096):
            if chunk[block + 32 : block + 36] == NX_MAGIC:
                hits.append(start + block)
                if len(hits) >= MAX_HITS:
                    return hits
    return hits


def deep_check(
    reader: SectorReader,
    partition: Partition,
    head: bytes,
    hits: list[int],
    out: TextIO,
    reopen: Callable[[], SectorReader],
    source_path: str,
) -> None:
    """Work out how much of an APFS container's start was overwritten (read-only)."""
    print("\n    --- Deep check ---", file=out)
    first_mib = reader.read_at(partition.offset, min(1024 * 1024, partition.size))
    print("    512-byte sectors at the partition start:", file=out)
    for line in describe_runs(first_mib[:16384], 512, classify_sector):
        print(f"      {line}", file=out)
    print("    4 KiB blocks in the first 1 MiB:", file=out)
    for line in describe_runs(first_mib, 4096, classify_block):
        print(f"      {line}", file=out)

    if head[3:11] == b"EXFAT   ":
        print("    exFAT header found at the start; checking whether it holds files:", file=out)
        try:
            for line in exfat_root_entries(reader, partition, head):
                print(f"      {line}", file=out)
        except (ApfsExplorerError, ValueError) as exc:
            print(f"      ERROR reading exFAT root directory: {exc}", file=out)

    def read(offset: int, length: int) -> bytes:
        return reader.read_at(partition.offset + offset, length)

    found = find_latest_superblock(read, partition.size)
    if found is None:
        print("    no valid backup superblock: the container cannot be opened this way", file=out)
        return
    d = found.details
    print(f"    valid backup superblocks: {found.candidates}; newest is checkpoint {d.xid} at offset "
          f"{found.offset:,} (block {d.block_size} x {d.block_count:,}, {len(d.volume_oids)} volume(s), "
          f"uuid {d.container_uuid})", file=out)  # fmt: skip
    print("    trying to open the container with that superblock in place of block 0 "
          "(in memory only; nothing is written):", file=out)  # fmt: skip
    backend = LibfsapfsBackend(lambda _s: reopen())
    try:
        backend.open(
            SourceDevice(source_path, partition.offset, partition.size, superblock_offset=found.offset)
        )
        for volume in backend.list_volumes():
            print(f"      volume {volume.index}: {volume.name!r} locked={volume.is_locked}", file=out)
            if volume.is_locked:
                continue
            entries = backend.list_directory(volume.index, PurePosixPath("/"))
            folders = sum(1 for e in entries if e.is_directory)
            # Counts only: names stay private even if this report is shared.
            print(f"        root folder: {len(entries)} entries ({folders} folders, "
                  f"{len(entries) - folders} other)", file=out)
        print("    RESULT: the APFS container is readable with the in-memory fix.", file=out)
    except ApfsExplorerError as exc:
        print(f"    RESULT: could not open the container: {exc}", file=out)
    finally:
        backend.close()


def diagnose(disk_filter: int | None, out: TextIO) -> int:
    api = Win32DiskApi()
    print(f"{APP_NAME} {__version__} drive diagnostic - {datetime.now():%Y-%m-%d %H:%M:%S}", file=out)
    if not api.is_admin():
        print("ERROR: run this as administrator (reading partitions needs it).", file=out)
        return 2
    result = WindowsDiskDiscovery(api).scan()
    for warning in result.warnings:
        print(f"warning: {warning}", file=out)

    examined = 0
    for report in result.disks:
        disk = report.disk
        if disk_filter is not None and disk.number != disk_filter:
            continue
        print(f"\nDisk {disk.number}: {disk.model!r} serial={disk.serial!r} {disk.size_bytes:,} bytes, "
              f"{disk.bytes_per_sector}-byte sectors, {report.external_reason}, "
              f"{report.partition_style.value}", file=out)  # fmt: skip
        if report.is_system or not report.is_external:
            print("  skipped: internal or Windows system disk (never read)", file=out)
            continue
        examined += 1
        try:
            reader = api.open_device_reader(disk.number)
        except ApfsExplorerError as exc:
            print(f"  ERROR opening disk: {exc}", file=out)
            continue
        with reader:
            for p in report.partitions:
                print(f"\n  Partition {p.number}: offset {p.offset:,}, size {p.size:,}, "
                      f"type {p.type_id} ({p.type_label}), name {p.name!r}", file=out)  # fmt: skip
                try:
                    head = reader.read_at(p.offset, min(HEAD_BYTES, p.size - p.size % reader.sector_size))
                except (ApfsExplorerError, ValueError) as exc:
                    print(f"    ERROR reading partition start: {exc}", file=out)
                    continue
                print(f"    read {len(head):,} bytes; contents: {identify(head)}", file=out)
                print("    first 128 bytes:", file=out)
                for line in hexdump(head[:128]):
                    print(line, file=out)
                if p.has_apfs_type and head[32:36] != NX_MAGIC:
                    print("    searching the first 64 MiB of this partition for an APFS superblock...",
                          file=out)  # fmt: skip
                    try:
                        hits = search_superblocks(reader, p)
                    except (ApfsExplorerError, ValueError) as exc:
                        print(f"    ERROR during search: {exc}", file=out)
                        continue
                    if hits:
                        print(f"    found NXSB blocks at partition offsets: {hits}", file=out)
                    else:
                        print("    no APFS superblock found in the first 64 MiB", file=out)
                        continue
                    deep_check(
                        reader, p, head, hits, out,
                        reopen=lambda n=disk.number: api.open_device_reader(n),
                        source_path=physical_drive_path(disk.number),
                    )  # fmt: skip
    if examined == 0:
        print("\nNo external disk was examined.", file=out)
        return 1
    return 0


class _Tee:
    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def write(self, text: str) -> int:
        for stream in self._streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apfs_explorer_lite.diagnose", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--disk", type=int, help="only examine this disk number")
    parser.add_argument("--out", type=Path, help="also save the report to this text file")
    args = parser.parse_args(argv)
    if args.out is None:
        return diagnose(args.disk, sys.stdout)
    with args.out.open("w", encoding="utf-8") as report:
        code = diagnose(args.disk, _Tee(sys.stdout, report))  # type: ignore[arg-type]
    print(f"\nReport saved to {args.out.resolve()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
