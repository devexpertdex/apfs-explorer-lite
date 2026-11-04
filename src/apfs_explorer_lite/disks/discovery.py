"""Physical disk and APFS container discovery.

Detection happens in two stages:

1. **Partition metadata (every disk, no admin rights needed).** Each disk is
   queried through a query-only handle for its descriptor (model, bus type),
   geometry and partition table. A partition is an APFS *candidate* only if its
   GPT type GUID is an Apple APFS type. Unrecognized types are never assumed
   to be APFS.
2. **Container signature (eligible partitions only, admin rights needed).**
   For candidate partitions on external, non-system disks, the first block is
   read through a read-only handle and checked for the ``NXSB`` magic and a
   valid Fletcher-64 checksum. No other disk or partition is read.

Result per candidate partition:

* CONFIRMED: APFS type GUID and a valid container superblock.
* TENTATIVE: APFS type GUID, but the signature could not be checked or its
  checksum did not match.
* NOT_APFS: APFS type GUID, but no container superblock (hidden in the UI).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from ..errors import DiskAccessDeniedError, DiskDisconnectedError, DiskDiscoveryError
from ..models import (
    APFS_PARTITION_TYPE_GUID,
    ApfsContainerCandidate,
    ApfsIdentification,
    Partition,
    PartitionStyle,
    PhysicalDisk,
    physical_drive_path,
)
from .api import DiskApi
from .apfs_signature import NX_MIN_BLOCK_SIZE, NxSuperblockInfo, parse_nx_superblock
from .recovery import find_latest_superblock
from .signatures import identify_signature
from .parsing import (
    EXTERNAL_BUS_TYPES,
    DeviceDescriptor,
    HotplugInfo,
    parse_device_descriptor,
    parse_drive_layout,
    parse_geometry,
    parse_hotplug_info,
)

__all__ = [
    "APFS_PARTITION_TYPE_GUID",
    "DiscoveryResult",
    "DiskDiscovery",
    "DiskReport",
    "WindowsDiskDiscovery",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiskReport:
    disk: PhysicalDisk
    partition_style: PartitionStyle
    partitions: tuple[Partition, ...]
    containers: tuple[ApfsContainerCandidate, ...]
    is_external: bool
    external_reason: str
    is_system: bool

    @property
    def displayable_containers(self) -> tuple[ApfsContainerCandidate, ...]:
        return tuple(c for c in self.containers if c.is_displayable)

    @property
    def is_candidate(self) -> bool:
        return self.is_external and not self.is_system and bool(self.displayable_containers)

    def container_for(self, partition: Partition) -> ApfsContainerCandidate | None:
        return next((c for c in self.containers if c.partition == partition), None)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    disks: tuple[DiskReport, ...]
    is_admin: bool
    warnings: tuple[str, ...] = ()
    scanned_at: datetime = field(default_factory=datetime.now)

    @property
    def candidates(self) -> tuple[DiskReport, ...]:
        return tuple(d for d in self.disks if d.is_candidate)

    @property
    def hidden_count(self) -> int:
        return len(self.disks) - len(self.candidates)


class DiskDiscovery(ABC):
    @abstractmethod
    def scan(self) -> DiscoveryResult: ...


def classify_external(descriptor: DeviceDescriptor, hotplug: HotplugInfo | None) -> tuple[bool, str]:
    """Decide whether a disk is external, with a short human-readable reason."""
    bus = descriptor.bus_name
    if descriptor.bus_type in EXTERNAL_BUS_TYPES:
        return True, f"{bus} (external bus)"
    if hotplug is not None and hotplug.device_hotplug:
        return True, f"{bus}, hot-pluggable (e.g. Thunderbolt enclosure)"
    if descriptor.removable_media:
        return True, f"{bus}, removable media"
    return False, f"internal {bus} disk"


def _round_up(value: int, multiple: int) -> int:
    return -(-value // multiple) * multiple


class WindowsDiskDiscovery(DiskDiscovery):
    def __init__(self, api: DiskApi | None = None) -> None:
        if api is None:
            from .win32 import Win32DiskApi

            api = Win32DiskApi()
        self._api = api

    def scan(self) -> DiscoveryResult:
        """Enumerate disks. Raises DiskDiscoveryError only if listing itself fails."""
        warnings: list[str] = []
        numbers = self._api.list_physical_drive_numbers()
        try:
            system_disks = self._api.system_disk_numbers()
        except DiskDiscoveryError as exc:
            logger.warning("Could not determine the system disk: %s", exc)
            warnings.append(f"Could not determine which disk holds Windows: {exc.user_message}")
            system_disks = set()
        is_admin = self._api.is_admin()

        reports: list[DiskReport] = []
        for number in numbers:
            try:
                reports.append(self._scan_disk(number, number in system_disks))
            except DiskDisconnectedError:
                logger.info("Disk %s disappeared during the scan", number)
                warnings.append(f"Disk {number} was disconnected during the scan and was skipped.")
            except DiskDiscoveryError as exc:
                logger.warning("Skipping disk %s: %s", number, exc)
                warnings.append(f"Disk {number} could not be queried: {exc.user_message}")

        # Never hide an APFS-labelled partition silently: say why it is not listed.
        for report in reports:
            for container in report.containers:
                if not container.is_displayable:
                    p = container.partition
                    logger.warning(
                        "Disk %s partition %s is typed APFS but has no container superblock",
                        p.disk_number, p.number,
                    )  # fmt: skip
                    warnings.append(
                        f"Disk {p.disk_number} ({report.disk.model}): partition {p.number} is "
                        "labelled Apple APFS, but no APFS container signature was found at its "
                        "start, so it is not listed. Run 'Diagnose drive.bat' for details."
                    )

        logger.info(
            "Scanned %d disk(s): %d APFS candidate(s), admin=%s",
            len(reports), sum(r.is_candidate for r in reports), is_admin,
        )  # fmt: skip
        return DiscoveryResult(tuple(reports), is_admin, tuple(warnings))

    def _scan_disk(self, number: int, is_system: bool) -> DiskReport:
        raw = self._api.query_metadata(number)
        descriptor = parse_device_descriptor(raw.descriptor)
        geometry = parse_geometry(raw.geometry)
        layout = parse_drive_layout(raw.layout, number)
        hotplug = parse_hotplug_info(raw.hotplug) if raw.hotplug else None
        is_external, external_reason = classify_external(descriptor, hotplug)

        disk = PhysicalDisk(
            number=number,
            device_path=physical_drive_path(number),
            model=descriptor.model,
            serial=descriptor.serial,
            size_bytes=geometry.disk_size,
            bytes_per_sector=geometry.bytes_per_sector,
            bus_type=descriptor.bus_name,
            removable_media=descriptor.removable_media,
            device_hotplug=hotplug.device_hotplug if hotplug else None,
        )

        containers: tuple[ApfsContainerCandidate, ...] = ()
        # Only external, non-system disks are ever read, and only APFS-typed partitions.
        if is_external and not is_system:
            containers = tuple(
                self._identify(disk, p) for p in layout.partitions if p.has_apfs_type
            )
        return DiskReport(
            disk, layout.style, layout.partitions, containers, is_external, external_reason, is_system
        )

    def _identify(self, disk: PhysicalDisk, partition: Partition) -> ApfsContainerCandidate:
        sector = disk.bytes_per_sector
        if sector <= 0 or partition.offset % sector:
            return ApfsContainerCandidate(
                partition,
                ApfsIdentification.TENTATIVE,
                "APFS partition type; the partition is not sector-aligned so the signature was not read.",
            )
        try:
            info = self._read_superblock(disk, partition)
        except DiskAccessDeniedError:
            return ApfsContainerCandidate(
                partition,
                ApfsIdentification.TENTATIVE,
                "APFS partition type. Run as administrator to verify the container signature.",
            )
        except DiskDisconnectedError:
            raise
        except DiskDiscoveryError as exc:
            logger.warning("Signature read failed on disk %s: %s", disk.number, exc)
            return ApfsContainerCandidate(
                partition,
                ApfsIdentification.TENTATIVE,
                f"APFS partition type. The container signature could not be read: {exc.user_message}",
            )

        if info is None:
            return self._try_checkpoint_superblock(disk, partition)
        details = {
            "block_size": info.block_size,
            "block_count": info.block_count,
            "container_uuid": info.container_uuid,
        }
        if not info.checksum_valid:
            return ApfsContainerCandidate(
                partition,
                ApfsIdentification.TENTATIVE,
                "APFS signature found, but the superblock checksum did not verify.",
                **details,
            )
        return ApfsContainerCandidate(
            partition,
            ApfsIdentification.CONFIRMED,
            "APFS container superblock found and checksum verified.",
            **details,
        )

    def _try_checkpoint_superblock(
        self, disk: PhysicalDisk, partition: Partition
    ) -> ApfsContainerCandidate:
        """Block 0 has no superblock: look for a valid copy in the checkpoint area."""

        def read(offset: int, length: int) -> bytes:
            return self._api.read_bytes(disk.number, partition.offset + offset, length)

        try:
            found = find_latest_superblock(read, partition.size)
            what = identify_signature(read(0, NX_MIN_BLOCK_SIZE))
        except DiskDisconnectedError:
            raise
        except DiskDiscoveryError as exc:
            logger.warning("Checkpoint search failed on disk %s: %s", disk.number, exc)
            found, what = None, ""
        if found is None:
            return ApfsContainerCandidate(
                partition,
                ApfsIdentification.NOT_APFS,
                "Partition is typed APFS but contains no APFS container superblock.",
            )
        d = found.details
        logger.warning(
            "Disk %s partition %s: block 0 is %s; using checkpoint superblock xid %s at offset %s",
            disk.number, partition.number, what, d.xid, found.offset,
        )  # fmt: skip
        return ApfsContainerCandidate(
            partition,
            ApfsIdentification.RECOVERABLE,
            f"The first block of this APFS container was overwritten (it now holds: {what}). "
            f"An intact backup of the container superblock (checkpoint {d.xid}) was found, so "
            "the container can be read using it. The fix is applied in memory only; nothing "
            "is written to the drive.",
            block_size=d.block_size,
            block_count=d.block_count,
            container_uuid=d.container_uuid,
            superblock_offset=found.offset,
        )

    def _read_superblock(self, disk: PhysicalDisk, partition: Partition) -> NxSuperblockInfo | None:
        sector = disk.bytes_per_sector
        length = _round_up(NX_MIN_BLOCK_SIZE, sector)
        data = self._api.read_bytes(disk.number, partition.offset, length)
        if len(data) < length:
            # A short read is a read failure, not evidence that the partition is not APFS.
            raise DiskDiscoveryError(
                "The disk returned less data than requested.",
                detail=f"{len(data)} of {length} bytes at offset {partition.offset}",
            )
        info = parse_nx_superblock(data)
        if (
            info is not None
            and info.block_size_valid
            and info.block_size > len(data)
            and info.block_size <= partition.size
        ):
            data = self._api.read_bytes(
                disk.number, partition.offset, _round_up(info.block_size, sector)
            )
            info = parse_nx_superblock(data)
        return info
