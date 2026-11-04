"""Read-only acceptance check for the APFS backend.

Opens an APFS container, lists its volumes, walks the folder tree and reads
one real file completely, printing its SHA-256. Nothing is written anywhere.

    python -m apfs_explorer_lite.verify --disk 2                (run as administrator)
    python -m apfs_explorer_lite.verify --disk 2 --partition 2 --read "/Users/me/notes.txt"
    python -m apfs_explorer_lite.verify --image C:\\images\\ssd.dmg

Exit status: 0 = a volume was enumerated and a file was read in full,
1 = the check failed, 2 = the source could not be located or opened.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from collections import deque
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import TextIO

from . import APP_NAME, __version__
from .backends.libfsapfs_backend import LibfsapfsBackend
from .disks.gpt import find_gpt_sector_size, parse_gpt
from .disks.recovery import find_latest_superblock
from .errors import ApfsExplorerError
from .models import APFS_ROOT, ApfsEntry, ApfsIdentification, EntryKind, SourceDevice
from .source_io import ReadOnlyWindowStream, open_sector_reader

READ_CHUNK = 1024 * 1024


class SourceError(Exception):
    """The APFS source could not be located."""


def find_image_containers(path: Path) -> list[SourceDevice]:
    """APFS containers inside an image file: a bare container or GPT partitions."""
    reader = open_sector_reader(SourceDevice(str(path), 0, 1))
    try:
        stream = ReadOnlyWindowStream(reader, 0, reader.size)

        def read(offset: int, length: int) -> bytes:
            stream.seek(offset)
            return stream.read(length)

        if read(32, 4) == b"NXSB":
            return [SourceDevice(str(path), 0, reader.size, "whole image (bare container)")]
        sector = find_gpt_sector_size(read)
        if sector is None:
            raise SourceError("The image has neither an APFS container nor a GPT partition table.")
        sources = []
        for p in parse_gpt(read, sector):
            if not p.has_apfs_type:
                continue
            label = f"GPT partition {p.number} ({p.type_label})"
            superblock_offset = None
            if read(p.offset + 32, 4) != b"NXSB":
                found = find_latest_superblock(lambda o, n, p=p: read(p.offset + o, n), p.size)
                if found is not None:
                    superblock_offset = found.offset
                    label += f", first block damaged: using backup superblock {found.details.xid}"
            sources.append(SourceDevice(str(path), p.offset, p.size, label, superblock_offset))
        return sources
    finally:
        reader.close()


def find_disk_containers(disk_number: int, partition: int | None, out: TextIO) -> list[SourceDevice]:
    """Use Phase 2 discovery so the same external-disk rules apply."""
    from .disks.discovery import WindowsDiskDiscovery

    result = WindowsDiskDiscovery().scan()
    for warning in result.warnings:
        print(f"  warning: {warning}", file=out)
    report = next((r for r in result.disks if r.disk.number == disk_number), None)
    if report is None:
        raise SourceError(f"Disk {disk_number} was not found.")
    if report.is_system or not report.is_external:
        raise SourceError(
            f"Disk {disk_number} is {'the Windows system disk' if report.is_system else 'internal'}; "
            "only external disks are opened."
        )
    print(f"  disk {disk_number}: {report.disk.model}, {report.disk.size_bytes:,} bytes, "
          f"{report.external_reason}, admin={result.is_admin}", file=out)  # fmt: skip
    sources = []
    for container in report.displayable_containers:
        if partition is not None and container.partition.number != partition:
            continue
        print(f"  partition {container.partition.number}: {container.identification.value} "
              f"- {container.reason}", file=out)  # fmt: skip
        if container.identification is not ApfsIdentification.CONFIRMED and not result.is_admin:
            print("  note: not running as administrator; opening the disk will fail.", file=out)
        sources.append(container.to_source_device())
    return sources


def _describe(entry: ApfsEntry) -> str:
    kind = {EntryKind.DIRECTORY: "dir ", EntryKind.FILE: "file", EntryKind.SYMLINK: "link"}.get(
        entry.kind, "othr"
    )
    modified = entry.modified.strftime("%Y-%m-%d %H:%M:%S") if entry.modified else "-"
    extra = f" -> {entry.symlink_target}" if entry.symlink_target else ""
    if entry.compression:
        extra += f" [compressed: {entry.compression.method_name}]"
    return f"{kind} {entry.size:>12,}  {modified}  {entry.path}{extra}"


def check_container(
    source: SourceDevice,
    *,
    volume: int | None,
    read_path: str | None,
    max_entries: int,
    out: TextIO,
) -> bool:
    backend = LibfsapfsBackend()
    backend.open(source)
    enumerated = False
    target: tuple[int, ApfsEntry] | None = None
    try:
        volumes = backend.list_volumes()
        for info in volumes:
            state = "LOCKED (encrypted, skipped)" if info.is_locked else "unlocked"
            print(f"  volume {info.index}: {info.name!r} {info.identifier} {state}", file=out)

        for info in volumes:
            if info.is_locked or (volume is not None and info.index != volume):
                continue
            print(f"\n  Tree of volume {info.index} ({info.name}), first {max_entries} entries:", file=out)
            shown = 0
            pending: deque[PurePosixPath] = deque([APFS_ROOT])
            while pending and shown < max_entries:
                folder = pending.popleft()
                try:
                    children = backend.list_directory(info.index, folder)
                except ApfsExplorerError as exc:
                    print(f"    ! cannot list {folder}: {exc}", file=out)
                    continue
                enumerated = True
                for entry in sorted(children, key=lambda e: e.name.casefold()):
                    if shown < max_entries:
                        print(f"    {_describe(entry)}", file=out)
                        shown += 1
                    if entry.is_directory:
                        pending.append(entry.path)
                    elif (
                        target is None
                        and read_path is None
                        and entry.kind is EntryKind.FILE
                        and entry.size > 0
                        and (entry.compression is None or entry.compression.supported)
                    ):
                        target = (info.index, entry)
            if read_path is not None and target is None:
                target = (info.index, backend.stat(info.index, PurePosixPath(read_path)))

        if not enumerated:
            print("\n  FAIL: no unlocked volume could be enumerated.", file=out)
            return False
        if target is None:
            print("\n  FAIL: no readable regular file with content was found.", file=out)
            return False

        volume_index, entry = target
        print(f"\n  Reading {entry.path} from volume {volume_index} ({entry.size:,} bytes)", file=out)
        digest = hashlib.sha256()
        total = 0
        first = b""
        with backend.open_file(volume_index, entry.path) as stream:  # type: ignore[attr-defined]
            while chunk := stream.read(READ_CHUNK):
                if not first:
                    first = chunk[:64]
                digest.update(chunk)
                total += len(chunk)
        print(f"    bytes read : {total:,}", file=out)
        print(f"    sha256     : {digest.hexdigest()}", file=out)
        print(f"    first bytes: {first!r}", file=out)
        if total != entry.size:
            print(f"\n  FAIL: read {total} bytes but the file size is {entry.size}.", file=out)
            return False
        return True
    finally:
        backend.close()


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="python -m apfs_explorer_lite.verify", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--disk", type=int, help="physical disk number (see Disk Management)")
    where.add_argument("--image", type=Path, help="raw/DMG disk image or bare container file")
    parser.add_argument("--partition", type=int, help="partition number on --disk")
    parser.add_argument("--volume", type=int, help="only check this volume index")
    parser.add_argument("--read", dest="read_path", help="APFS path of the file to read")
    parser.add_argument("--max-entries", type=int, default=200, help="entries to print per volume")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    print(f"{APP_NAME} {__version__} read-only APFS acceptance check", file=out)
    print(f"libfsapfs: {LibfsapfsBackend.probe().version}", file=out)
    try:
        if args.image is not None:
            print(f"Source: image {args.image}", file=out)
            sources = find_image_containers(args.image)
        else:
            print(f"Source: physical disk {args.disk}", file=out)
            sources = find_disk_containers(args.disk, args.partition, out)
    except (SourceError, ApfsExplorerError, OSError) as exc:
        print(f"ERROR: {exc}", file=out)
        return 2
    if not sources:
        print("ERROR: no APFS container found on this source.", file=out)
        return 2

    passed = False
    for source in sources:
        print(f"\nContainer: {source.description or source.device_path} "
              f"(offset {source.offset:,}, size {source.size:,})", file=out)  # fmt: skip
        try:
            passed |= check_container(
                source, volume=args.volume, read_path=args.read_path,
                max_entries=args.max_entries, out=out,
            )  # fmt: skip
        except ApfsExplorerError as exc:
            print(f"  ERROR: {exc}", file=out)
    print(f"\nRESULT: {'PASS' if passed else 'FAIL'}", file=out)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
