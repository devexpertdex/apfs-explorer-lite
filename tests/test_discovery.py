from __future__ import annotations

import pytest

from apfs_explorer_lite.disks.discovery import WindowsDiskDiscovery, classify_external
from apfs_explorer_lite.disks.parsing import DeviceDescriptor, HotplugInfo
from apfs_explorer_lite.errors import DiskAccessDeniedError, DiskDisconnectedError, DiskDiscoveryError
from apfs_explorer_lite.models import APFS_PARTITION_TYPE_GUID, ApfsIdentification, PartitionStyle

from .win_structs import (
    BASIC_DATA_GUID,
    BUS_1394,
    BUS_NVME,
    BUS_SATA,
    BUS_USB,
    EFI_GUID,
    MSR_GUID,
    UNKNOWN_GUID,
    FakeDiskApi,
    GptEntry,
    apfs_disk_layout,
    build_gpt_layout,
    build_mbr_layout,
    build_nx_superblock,
    make_disk,
)

APFS_OFFSET = 209_735_680


def internal_system_disk(**kwargs: object):
    layout = build_gpt_layout(
        [
            GptEntry(1, 1048576, 104857600, EFI_GUID),
            GptEntry(2, 105906176, 16777216, MSR_GUID),
            GptEntry(3, 122683392, 500 * 10**9, BASIC_DATA_GUID),
        ]
    )
    return make_disk(bus_type=BUS_NVME, model=("", "SAMSUNG MZVL2512"), layout=layout, **kwargs)


def apfs_usb_disk(**kwargs: object):
    kwargs.setdefault("layout", apfs_disk_layout(APFS_OFFSET))
    kwargs.setdefault("content", {APFS_OFFSET: build_nx_superblock()})
    return make_disk(**kwargs)


def scan(api: FakeDiskApi):
    return WindowsDiskDiscovery(api).scan()


# --- positive identification ---------------------------------------------------


def test_confirms_apfs_on_external_usb_disk() -> None:
    api = FakeDiskApi({0: internal_system_disk(), 2: apfs_usb_disk()})
    result = scan(api)

    assert [r.disk.number for r in result.candidates] == [2]
    report = result.candidates[0]
    assert report.disk.model == "Samsung PSSD T7"
    assert report.disk.bus_type == "USB"
    assert report.disk.device_path == r"\\.\PhysicalDrive2"
    assert report.partition_style is PartitionStyle.GPT
    assert [p.type_label for p in report.partitions] == ["EFI System", "Apple APFS"]

    (container,) = report.containers
    assert container.identification is ApfsIdentification.CONFIRMED
    assert container.block_size == 4096
    assert container.partition.number == 2
    source = container.to_source_device()
    assert (source.device_path, source.offset) == (r"\\.\PhysicalDrive2", APFS_OFFSET)
    assert result.hidden_count == 1


def test_only_apfs_partitions_on_eligible_disks_are_read() -> None:
    api = FakeDiskApi({0: internal_system_disk(), 2: apfs_usb_disk()})
    scan(api)
    # Metadata is queried for every disk, but content is read only from the APFS partition.
    assert api.metadata_queries == [0, 2]
    assert api.reads == [(2, APFS_OFFSET, 4096)]


def test_large_block_size_triggers_full_block_read() -> None:
    api = FakeDiskApi({1: apfs_usb_disk(content={APFS_OFFSET: build_nx_superblock(block_size=16384)})})
    (container,) = scan(api).candidates[0].containers
    assert container.identification is ApfsIdentification.CONFIRMED
    assert api.reads == [(1, APFS_OFFSET, 4096), (1, APFS_OFFSET, 16384)]


def test_4k_sector_disk_reads_one_sector() -> None:
    api = FakeDiskApi({1: apfs_usb_disk(sector=4096)})
    assert scan(api).candidates[0].containers[0].identification is ApfsIdentification.CONFIRMED
    assert api.reads == [(1, APFS_OFFSET, 4096)]


# --- tentative identification ---------------------------------------------------


def test_access_denied_gives_tentative_with_admin_hint() -> None:
    api = FakeDiskApi(
        {1: apfs_usb_disk(read_error=DiskAccessDeniedError("denied"))}, admin=False
    )
    result = scan(api)
    assert not result.is_admin
    (container,) = result.candidates[0].containers
    assert container.identification is ApfsIdentification.TENTATIVE
    assert "administrator" in container.reason


def test_bad_checksum_gives_tentative() -> None:
    api = FakeDiskApi({1: apfs_usb_disk(content={APFS_OFFSET: build_nx_superblock(valid_checksum=False)})})
    (container,) = scan(api).candidates[0].containers
    assert container.identification is ApfsIdentification.TENTATIVE
    assert "checksum" in container.reason


def test_other_read_error_gives_tentative() -> None:
    api = FakeDiskApi({1: apfs_usb_disk(read_error=DiskDiscoveryError("I/O device error"))})
    (container,) = scan(api).candidates[0].containers
    assert container.identification is ApfsIdentification.TENTATIVE
    assert "I/O device error" in container.reason


def test_unaligned_partition_is_tentative_and_not_read() -> None:
    api = FakeDiskApi({1: apfs_usb_disk(layout=apfs_disk_layout(APFS_OFFSET + 100))})
    (container,) = scan(api).candidates[0].containers
    assert container.identification is ApfsIdentification.TENTATIVE
    assert api.reads == []


# --- negative cases ----------------------------------------------------------------


def test_apfs_type_without_superblock_is_hidden_with_warning() -> None:
    api = FakeDiskApi({1: apfs_usb_disk(content={})})
    result = scan(api)
    assert result.candidates == ()
    assert result.disks[0].containers[0].identification is ApfsIdentification.NOT_APFS
    (warning,) = result.warnings
    assert "partition 2 is labelled Apple APFS" in warning
    assert "Samsung PSSD T7" in warning


def _damaged_container(xids: list[int]) -> bytes:
    """1 MiB container start: exFAT boot sector in block 0, checkpoint superblocks after it."""
    data = bytearray(1024 * 1024)
    data[3:11] = b"EXFAT   "
    data[510:512] = b"\x55\xaa"
    for index, xid in enumerate(xids, start=2):
        block = bytearray(build_nx_superblock())
        block[16:24] = xid.to_bytes(8, "little")
        from .win_structs import reference_fletcher64

        block[0:8] = reference_fletcher64(bytes(block)).to_bytes(8, "little")
        data[index * 4096 : (index + 1) * 4096] = block
    return bytes(data)


def test_overwritten_block0_is_recoverable() -> None:
    api = FakeDiskApi({1: apfs_usb_disk(content={APFS_OFFSET: _damaged_container([7, 9, 8])})})
    result = scan(api)
    (container,) = result.candidates[0].containers
    assert container.identification is ApfsIdentification.RECOVERABLE
    assert container.superblock_offset == 3 * 4096  # the block with the newest xid (9)
    assert "exFAT volume" in container.reason
    assert container.to_source_device().superblock_offset == 3 * 4096
    assert result.warnings == ()


class ShortReadApi(FakeDiskApi):
    def read_bytes(self, disk_number: int, offset: int, length: int) -> bytes:
        self.reads.append((disk_number, offset, length))
        return b""


def test_short_read_is_tentative_not_hidden() -> None:
    api = ShortReadApi({1: apfs_usb_disk()})
    result = scan(api)
    (container,) = result.candidates[0].containers
    assert container.identification is ApfsIdentification.TENTATIVE
    assert "less data than requested" in container.reason


def test_unrecognized_partition_type_is_not_assumed_apfs() -> None:
    layout = build_gpt_layout([GptEntry(1, 1048576, 10**9, UNKNOWN_GUID)])
    api = FakeDiskApi({1: make_disk(layout=layout, content={1048576: build_nx_superblock()})})
    result = scan(api)
    assert result.candidates == ()
    assert result.disks[0].containers == ()
    assert api.reads == []


def test_ntfs_and_mbr_usb_disks_are_not_candidates() -> None:
    ntfs = make_disk(layout=build_gpt_layout([GptEntry(1, 1048576, 10**9, BASIC_DATA_GUID)]))
    mbr = make_disk(layout=build_mbr_layout([(1, 1048576, 10**9, 0xAF)]))
    api = FakeDiskApi({1: ntfs, 2: mbr})
    result = scan(api)
    assert result.candidates == ()
    assert result.hidden_count == 2
    assert api.reads == []


def test_internal_disk_with_apfs_partition_is_never_read() -> None:
    api = FakeDiskApi({1: apfs_usb_disk(bus_type=BUS_SATA)})
    result = scan(api)
    assert result.candidates == ()
    assert not result.disks[0].is_external
    assert api.reads == []


def test_system_disk_is_never_read_even_if_external() -> None:
    api = FakeDiskApi({1: apfs_usb_disk()}, system={1})
    result = scan(api)
    assert result.candidates == ()
    assert result.disks[0].is_system
    assert api.reads == []


def test_uninitialized_disk_is_not_candidate() -> None:
    api = FakeDiskApi({1: make_disk()})  # RAW layout
    result = scan(api)
    assert result.disks[0].partition_style is PartitionStyle.RAW
    assert result.candidates == ()


# --- external classification -----------------------------------------------------


def _descriptor(bus: int, removable: bool = False) -> DeviceDescriptor:
    return DeviceDescriptor("", "", "", "", bus, removable)


@pytest.mark.parametrize(
    ("bus", "hotplug", "removable", "external"),
    [
        (BUS_USB, None, False, True),
        (BUS_1394, None, False, True),
        (BUS_NVME, HotplugInfo(False, False, True), False, True),  # Thunderbolt enclosure
        (BUS_NVME, HotplugInfo(False, False, False), False, False),
        (BUS_NVME, None, False, False),
        (BUS_SATA, None, True, True),
        (BUS_SATA, None, False, False),
    ],
)
def test_classify_external(bus: int, hotplug: HotplugInfo | None, removable: bool, external: bool) -> None:
    is_external, reason = classify_external(_descriptor(bus, removable), hotplug)
    assert is_external is external
    assert reason


def test_hotplug_nvme_disk_with_apfs_is_candidate() -> None:
    api = FakeDiskApi({3: apfs_usb_disk(bus_type=BUS_NVME, hotplug=True)})
    report = scan(api).candidates[0]
    assert "hot-pluggable" in report.external_reason


# --- disconnects and errors --------------------------------------------------------


def test_disk_disconnected_during_metadata_query() -> None:
    api = FakeDiskApi(
        {
            0: internal_system_disk(),
            2: make_disk(),
            3: apfs_usb_disk(),
        }
    )
    api.disks[2].metadata = DiskDisconnectedError("gone")
    result = scan(api)
    assert [r.disk.number for r in result.disks] == [0, 3]
    assert any("Disk 2 was disconnected" in w for w in result.warnings)


def test_disk_disconnected_during_signature_read() -> None:
    api = FakeDiskApi({2: apfs_usb_disk(read_error=DiskDisconnectedError("gone"))})
    result = scan(api)
    assert result.disks == ()
    assert any("disconnected" in w for w in result.warnings)


def test_metadata_access_denied_on_one_disk_is_reported() -> None:
    api = FakeDiskApi({1: make_disk(), 2: apfs_usb_disk()})
    api.disks[1].metadata = DiskAccessDeniedError("Access denied.")
    result = scan(api)
    assert [r.disk.number for r in result.candidates] == [2]
    assert any("Disk 1 could not be queried" in w for w in result.warnings)


def test_unknown_system_disk_is_a_warning() -> None:
    api = FakeDiskApi({2: apfs_usb_disk()}, system=DiskDiscoveryError("no extents"))
    result = scan(api)
    assert len(result.candidates) == 1
    assert any("which disk holds Windows" in w for w in result.warnings)


def test_listing_failure_propagates() -> None:
    api = FakeDiskApi({}, list_error=DiskDiscoveryError("QueryDosDevice failed"))
    with pytest.raises(DiskDiscoveryError):
        scan(api)


def test_no_disks() -> None:
    result = scan(FakeDiskApi({}))
    assert result.disks == () and result.candidates == () and result.hidden_count == 0


def test_apfs_type_guid_constant() -> None:
    assert APFS_PARTITION_TYPE_GUID == "7C3457EF-0000-11AA-AA11-00306543ECAC"
