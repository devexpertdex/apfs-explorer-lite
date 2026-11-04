"""Plain data types shared between services, backends and the GUI."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

# GPT partition type GUID that identifies an APFS container.
APFS_PARTITION_TYPE_GUID = "7C3457EF-0000-11AA-AA11-00306543ECAC"
# Apple Silicon system disks also carry APFS containers under these types.
APFS_ISC_PARTITION_TYPE_GUID = "69646961-6700-11AA-AA11-00306543ECAC"
APFS_RECOVERY_PARTITION_TYPE_GUID = "52637672-7900-11AA-AA11-00306543ECAC"
APFS_CONTAINER_TYPE_GUIDS = frozenset(
    {APFS_PARTITION_TYPE_GUID, APFS_ISC_PARTITION_TYPE_GUID, APFS_RECOVERY_PARTITION_TYPE_GUID}
)

APFS_ROOT = PurePosixPath("/")


def physical_drive_path(disk_number: int) -> str:
    return rf"\\.\PhysicalDrive{disk_number}"


class EntryKind(enum.Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


class PartitionStyle(enum.Enum):
    MBR = "MBR"
    GPT = "GPT"
    RAW = "RAW"
    UNKNOWN = "Unknown"


class ApfsIdentification(enum.Enum):
    CONFIRMED = "confirmed"  # APFS partition type + valid container superblock
    RECOVERABLE = "recoverable"  # block 0 damaged; a valid checkpoint superblock exists
    TENTATIVE = "tentative"  # APFS partition type, signature not verifiable
    NOT_APFS = "not_apfs"  # APFS partition type but no container superblock


@dataclass(frozen=True, slots=True)
class PhysicalDisk:
    """A physical disk as reported by Windows (metadata only)."""

    number: int
    device_path: str  # e.g. r"\\.\PhysicalDrive2"
    model: str
    serial: str
    size_bytes: int
    bytes_per_sector: int
    bus_type: str
    removable_media: bool = False
    device_hotplug: bool | None = None


@dataclass(frozen=True, slots=True)
class Partition:
    """One entry of a disk's partition table."""

    disk_number: int
    number: int
    offset: int
    size: int
    style: PartitionStyle
    type_id: str  # GPT type GUID (upper case) or MBR type such as "0x07"
    type_label: str
    name: str = ""
    partition_id: str | None = None

    @property
    def has_apfs_type(self) -> bool:
        return self.style is PartitionStyle.GPT and self.type_id in APFS_CONTAINER_TYPE_GUIDS


@dataclass(frozen=True, slots=True)
class SourceDevice:
    """A byte range on a physical disk that holds an APFS container."""

    device_path: str
    offset: int
    size: int
    description: str = ""
    # When block 0 is damaged: offset (within the container) of the checkpoint
    # superblock to present in its place, in memory only.
    superblock_offset: int | None = None

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError(f"offset must be >= 0, got {self.offset}")
        if self.size <= 0:
            raise ValueError(f"size must be > 0, got {self.size}")


@dataclass(frozen=True, slots=True)
class ApfsContainerCandidate:
    """A partition that is (or probably is) an APFS container."""

    partition: Partition
    identification: ApfsIdentification
    reason: str
    block_size: int | None = None
    block_count: int | None = None
    container_uuid: str | None = None
    superblock_offset: int | None = None

    @property
    def is_displayable(self) -> bool:
        return self.identification is not ApfsIdentification.NOT_APFS

    def to_source_device(self) -> SourceDevice:
        p = self.partition
        return SourceDevice(
            physical_drive_path(p.disk_number),
            p.offset,
            p.size,
            f"Disk {p.disk_number}, partition {p.number}",
            self.superblock_offset,
        )


@dataclass(frozen=True, slots=True)
class ApfsVolumeInfo:
    index: int
    name: str
    identifier: str
    is_locked: bool


@dataclass(frozen=True, slots=True)
class CompressionInfo:
    """Transparent (decmpfs) compression details of a file."""

    method: int
    method_name: str
    uncompressed_size: int
    supported: bool


@dataclass(frozen=True, slots=True)
class ApfsEntry:
    """Metadata for one file system object inside an APFS volume.

    Times are timezone-aware UTC. ``size`` is the logical (uncompressed) size.
    """

    path: PurePosixPath
    kind: EntryKind
    size: int
    modified: datetime | None = None
    identifier: int | None = None
    symlink_target: str | None = None
    created: datetime | None = None
    accessed: datetime | None = None
    changed: datetime | None = None
    mode: int | None = None
    owner_id: int | None = None
    group_id: int | None = None
    link_count: int | None = None
    xattr_names: tuple[str, ...] = ()
    compression: CompressionInfo | None = None

    @property
    def name(self) -> str:
        return self.path.name or "/"

    @property
    def is_directory(self) -> bool:
        return self.kind is EntryKind.DIRECTORY


def normalize_apfs_path(path: str | PurePosixPath) -> PurePosixPath:
    """Return an absolute APFS path with ``.`` and ``..`` collapsed.

    ``..`` never climbs above the volume root.
    """
    parts: list[str] = []
    for part in PurePosixPath(path).parts:
        if part in ("/", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return APFS_ROOT.joinpath(*parts)
