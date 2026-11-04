"""Backend tests against real APFS images created on macOS (not mocks).

Every test runs the full stack: source reader -> ReadOnlyWindowStream ->
pyfsapfs -> LibfsapfsBackend. The reader variants cover the real Win32
unbuffered ReadFile path and strict sector-alignment enforcement that mimics
raw disk handles.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from apfs_explorer_lite.backends.libfsapfs_backend import LibfsapfsBackend
from apfs_explorer_lite.disks.gpt import find_gpt_sector_size, parse_gpt
from apfs_explorer_lite.errors import (
    ApfsContainerOpenError,
    ApfsPathNotFoundError,
    ApfsUnsupportedFeatureError,
    ApfsVolumeLockedError,
    BackendError,
    BackendNotOpenError,
    DiskDisconnectedError,
    DiskDiscoveryError,
)
from apfs_explorer_lite.models import APFS_PARTITION_TYPE_GUID, EntryKind, SourceDevice
from apfs_explorer_lite.source_io import PythonFileSectorReader
from apfs_explorer_lite.verify import main as verify_main

from .real_images import (
    A_FILE,
    ANOTHER_FILE,
    DMG_PARTITION_OFFSET,
    DMG_PARTITION_SIZE,
    IMAGE_DIR,
    PASSWORDS_TXT,
    SHA256,
    image_path,
    sha256,
    windows_only,
)

ROOT = PurePosixPath("/")


class StrictSectorReader:
    """Wraps an image file and rejects any read a raw disk handle would reject."""

    def __init__(self, path: Path, sector_size: int) -> None:
        self._inner = PythonFileSectorReader(path)
        self._sector = sector_size
        self.reads: list[tuple[int, int]] = []
        self.fail_with: Exception | None = None

    @property
    def sector_size(self) -> int:
        return self._sector

    @property
    def size(self) -> int:
        return self._inner.size

    def read_at(self, offset: int, length: int) -> bytes:
        assert offset % self._sector == 0 and length % self._sector == 0, (offset, length)
        if self.fail_with is not None:
            raise self.fail_with
        self.reads.append((offset, length))
        return self._inner.read_at(offset, length)

    def close(self) -> None:
        self._inner.close()


def _win32_reader(path: Path):  # noqa: ANN202
    from apfs_explorer_lite.disks.win32 import Win32DiskApi

    return Win32DiskApi().open_image_reader(path)


READERS: dict[str, Callable[[Path], object]] = {
    "python-file": PythonFileSectorReader,
    "strict-512": lambda p: StrictSectorReader(p, 512),
    "strict-4096": lambda p: StrictSectorReader(p, 4096),
    "win32-unbuffered": pytest.param(_win32_reader, marks=windows_only),  # type: ignore[dict-item]
}


def dmg_source(path: Path) -> SourceDevice:
    return SourceDevice(str(path), DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE, "apfs.dmg partition 1")


def raw_source(path: Path) -> SourceDevice:
    return SourceDevice(str(path), 0, path.stat().st_size, "apfs.raw")


@pytest.fixture(scope="session", autouse=True)
def images_unchanged() -> Iterator[None]:
    """The source images must be byte-identical after the whole session."""
    yield
    for name, expected in SHA256.items():
        path = IMAGE_DIR / name
        if path.exists():
            assert sha256(path) == expected, f"{name} was modified"


@pytest.fixture(params=list(READERS.values()), ids=list(READERS))
def dmg_backend(request: pytest.FixtureRequest) -> Iterator[LibfsapfsBackend]:
    path = image_path("apfs.dmg")
    factory = request.param
    backend = LibfsapfsBackend(lambda _source: factory(path))
    backend.open(dmg_source(path))
    yield backend
    backend.close()


@pytest.fixture
def raw_backend() -> Iterator[LibfsapfsBackend]:
    path = image_path("apfs.raw")
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(path))
    backend.open(raw_source(path))
    yield backend
    backend.close()


def read_all(backend: LibfsapfsBackend, path: str, chunk: int = 7) -> bytes:
    with backend.open_file(0, PurePosixPath(path)) as stream:  # type: ignore[attr-defined]
        out = b""
        while data := stream.read(chunk):
            out += data
        return out


# --- volumes and directories ----------------------------------------------------------


def test_list_volumes(dmg_backend: LibfsapfsBackend) -> None:
    (volume,) = dmg_backend.list_volumes()
    assert volume.index == 0
    assert volume.name == "SingleVolume"
    assert volume.identifier == "44b5f357-e9b3-483d-893c-3802306b5132"
    assert not volume.is_locked


def test_list_root_directory(dmg_backend: LibfsapfsBackend) -> None:
    entries = {e.name: e for e in dmg_backend.list_directory(0, ROOT)}
    assert set(entries) == {"passwords.txt", "a_link", "a_directory", ".fseventsd"}
    assert entries["a_directory"].kind is EntryKind.DIRECTORY
    assert entries["passwords.txt"].kind is EntryKind.FILE
    assert entries["passwords.txt"].path == PurePosixPath("/passwords.txt")
    assert entries["a_link"].kind is EntryKind.SYMLINK
    assert entries["a_link"].symlink_target == "a_directory/a_file"


def test_nested_navigation(dmg_backend: LibfsapfsBackend) -> None:
    names = lambda path: sorted(e.name for e in dmg_backend.list_directory(0, PurePosixPath(path)))  # noqa: E731
    assert names("/") == [".fseventsd", "a_directory", "a_link", "passwords.txt"]
    assert names("/a_directory") == ["a_file", "another_file"]
    assert names("/a_directory/..") == names("/")  # ".." never climbs above the root
    with pytest.raises(BackendError, match="not a folder"):
        dmg_backend.list_directory(0, PurePosixPath("/passwords.txt"))


# --- metadata --------------------------------------------------------------------------


def test_file_metadata(dmg_backend: LibfsapfsBackend) -> None:
    entry = dmg_backend.stat(0, PurePosixPath("/a_directory/a_file"))
    assert entry.kind is EntryKind.FILE
    assert entry.size == len(A_FILE) == 53
    assert entry.modified == datetime(2018, 10, 12, 5, 20, 58, 140943, tzinfo=UTC)
    assert entry.created == datetime(2018, 10, 12, 5, 20, 58, 139259, tzinfo=UTC)
    assert entry.mode == 0o100644
    assert (entry.owner_id, entry.group_id, entry.link_count) == (99, 99, 1)
    assert entry.compression is None
    assert entry.identifier is not None


def test_root_metadata(dmg_backend: LibfsapfsBackend) -> None:
    root = dmg_backend.stat(0, ROOT)
    assert root.kind is EntryKind.DIRECTORY
    assert root.name == "/"


def test_extended_attribute_names(raw_backend: LibfsapfsBackend) -> None:
    assert raw_backend.stat(0, PurePosixPath("/a_directory/a_file")).xattr_names == ("myxattr",)
    assert raw_backend.stat(0, PurePosixPath("/a_directory/a_resourcefork")).xattr_names


# --- file contents ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/passwords.txt", PASSWORDS_TXT), ("/a_directory/a_file", A_FILE), ("/a_directory/another_file", ANOTHER_FILE)],
)
def test_read_file_contents(dmg_backend: LibfsapfsBackend, path: str, expected: bytes) -> None:
    assert read_all(dmg_backend, path) == expected


def test_content_matches_raw_on_disk_extent(raw_backend: LibfsapfsBackend) -> None:
    # Independent check: the bytes at the file's physical block in the image.
    raw = image_path("apfs.raw").read_bytes()
    assert raw[380928 : 380928 + 53] == A_FILE
    assert read_all(raw_backend, "/a_directory/a_file") == A_FILE


def test_seek_and_partial_reads(dmg_backend: LibfsapfsBackend) -> None:
    with dmg_backend.open_file(0, PurePosixPath("/a_directory/a_file")) as stream:  # type: ignore[attr-defined]
        assert stream.read(4) == b"This"
        assert stream.seek(10) == 10
        assert stream.read(4) == b"text"
        assert stream.seek(-6, 2) == 47
        assert stream.read() == A_FILE[-6:]
        assert stream.tell() == 53
        stream.seek(1000)
        assert stream.read(10) == b""


def test_empty_file(raw_backend: LibfsapfsBackend) -> None:
    assert read_all(raw_backend, "/a_directory/a_resourcefork") == b""


def test_large_chunk_read_and_hash(dmg_backend: LibfsapfsBackend) -> None:
    data = read_all(dmg_backend, "/passwords.txt", chunk=1 << 20)
    assert hashlib.sha256(data).hexdigest() == (
        "02a2a6af2f1ecf4720d7d49d640f0d0a269a7ec733e41973bdd34f09dad0e252"
    )


# --- error handling --------------------------------------------------------------------


def test_path_not_found(dmg_backend: LibfsapfsBackend) -> None:
    with pytest.raises(ApfsPathNotFoundError):
        dmg_backend.stat(0, PurePosixPath("/nope"))
    with pytest.raises(ApfsPathNotFoundError):
        dmg_backend.list_directory(0, PurePosixPath("/a_directory/nope"))


def test_cannot_list_file_or_open_folder_or_symlink(dmg_backend: LibfsapfsBackend) -> None:
    with pytest.raises(BackendError, match="not a folder"):
        dmg_backend.list_directory(0, PurePosixPath("/passwords.txt"))
    with pytest.raises(BackendError, match="is a folder"):
        dmg_backend.open_file(0, PurePosixPath("/a_directory"))
    with pytest.raises(ApfsUnsupportedFeatureError, match="symbolic link"):
        dmg_backend.open_file(0, PurePosixPath("/a_link"))


def test_missing_volume(dmg_backend: LibfsapfsBackend) -> None:
    with pytest.raises(BackendError, match="does not exist"):
        dmg_backend.list_directory(5, ROOT)


def test_encrypted_volume_is_reported_and_refused() -> None:
    path = image_path("apfs_encrypted.dmg")
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(path))
    backend.open(dmg_source(path))
    try:
        (volume,) = backend.list_volumes()
        assert volume.is_locked
        with pytest.raises(ApfsVolumeLockedError, match="encrypted"):
            backend.list_directory(0, ROOT)
        with pytest.raises(ApfsVolumeLockedError):
            backend.stat(0, ROOT)
    finally:
        backend.close()


def test_wrong_offset_is_not_an_apfs_container() -> None:
    path = image_path("apfs.dmg")
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(path))
    with pytest.raises(ApfsContainerOpenError, match="No APFS container"):
        backend.open(SourceDevice(str(path), 0, path.stat().st_size))
    assert not backend.is_open


def test_partition_larger_than_source() -> None:
    path = image_path("apfs.dmg")
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(path))
    with pytest.raises(ApfsContainerOpenError, match="does not fit"):
        backend.open(SourceDevice(str(path), DMG_PARTITION_OFFSET, 10**9))


def test_disconnect_during_reading_is_reported() -> None:
    path = image_path("apfs.dmg")
    reader = StrictSectorReader(path, 512)
    backend = LibfsapfsBackend(lambda _s: reader, chunk_size=4096, max_cached_chunks=1)
    backend.open(dmg_source(path))
    try:
        backend.list_directory(0, ROOT)
        reader.fail_with = DiskDisconnectedError("The disk is no longer available.")
        with pytest.raises(DiskDiscoveryError, match="no longer available"):
            read_all(backend, "/passwords.txt")
    finally:
        backend.close()


def test_close_invalidates_streams_and_reopen_works() -> None:
    path = image_path("apfs.dmg")
    backend = LibfsapfsBackend(lambda _s: PythonFileSectorReader(path))
    backend.open(dmg_source(path))
    stream = backend.open_file(0, PurePosixPath("/passwords.txt"))
    with pytest.raises(BackendError, match="already open"):
        backend.open(dmg_source(path))
    backend.close()
    assert not backend.is_open
    with pytest.raises(BackendNotOpenError):
        stream.read(1)
    with pytest.raises(BackendNotOpenError):
        backend.list_volumes()
    backend.open(dmg_source(path))
    assert read_all(backend, "/passwords.txt") == PASSWORDS_TXT
    backend.close()


def test_reads_are_aligned_and_bounded_to_partition() -> None:
    path = image_path("apfs.dmg")
    reader = StrictSectorReader(path, 4096)
    backend = LibfsapfsBackend(lambda _s: reader)
    backend.open(dmg_source(path))
    read_all(backend, "/passwords.txt")
    backend.close()
    assert reader.reads
    end = DMG_PARTITION_OFFSET + DMG_PARTITION_SIZE
    # Chunks are aligned to the device, never start past the partition end.
    assert all(offset < end for offset, _ in reader.reads)


def test_unsupported_compression_is_refused(dmg_backend: LibfsapfsBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    """No compressed sample exists in the images, so the decmpfs header is simulated."""
    from apfs_explorer_lite.backends import libfsapfs_backend as module
    from apfs_explorer_lite.models import CompressionInfo

    original = module.LibfsapfsBackend._build_entry

    def with_lzbitmap(self, raw, path):  # noqa: ANN001, ANN202
        entry = original(self, raw, path)
        info = CompressionInfo(13, "LZBitmap (inline)", entry.size, False)
        return type(entry)(**{**{f: getattr(entry, f) for f in entry.__slots__}, "compression": info})

    monkeypatch.setattr(module.LibfsapfsBackend, "_build_entry", with_lzbitmap)
    with pytest.raises(ApfsUnsupportedFeatureError, match="LZBitmap"):
        dmg_backend.open_file(0, PurePosixPath("/passwords.txt"))


# --- GPT and the acceptance tool -----------------------------------------------------------


def test_gpt_parser_on_real_image() -> None:
    data = image_path("apfs.dmg").read_bytes()

    def read(offset: int, length: int) -> bytes:
        return data[offset : offset + length]

    assert find_gpt_sector_size(read) == 512
    (partition,) = parse_gpt(read, 512)
    assert (partition.offset, partition.size) == (DMG_PARTITION_OFFSET, DMG_PARTITION_SIZE)
    assert partition.type_id == APFS_PARTITION_TYPE_GUID
    assert partition.has_apfs_type

    corrupt = bytearray(data)
    corrupt[1024 + 5] ^= 0xFF  # inside the partition entry table
    with pytest.raises(DiskDiscoveryError, match="checksum"):
        parse_gpt(lambda o, n: bytes(corrupt[o : o + n]), 512)


@pytest.mark.parametrize(("name", "code"), [("apfs.dmg", 0), ("apfs.raw", 0), ("apfs_encrypted.dmg", 1)])
def test_acceptance_tool(name: str, code: int) -> None:
    out = io.StringIO()
    assert verify_main(["--image", str(image_path(name))], out=out) == code
    text = out.getvalue()
    if code == 0:
        assert "RESULT: PASS" in text
        assert "02a2a6af2f1ecf4720d7d49d640f0d0a269a7ec733e41973bdd34f09dad0e252" in text
        assert "/a_directory/a_file" in text
    else:
        assert "LOCKED" in text and "RESULT: FAIL" in text


def test_acceptance_tool_missing_image(tmp_path: Path) -> None:
    out = io.StringIO()
    assert verify_main(["--image", str(tmp_path / "missing.dmg")], out=out) == 2
    assert "not found" in out.getvalue()


@windows_only
def test_acceptance_tool_rejects_internal_disk() -> None:
    """Runs real (metadata-only) discovery; disk 0 is internal on any normal PC."""
    out = io.StringIO()
    assert verify_main(["--disk", "0"], out=out) == 2
    assert "only external disks are opened" in out.getvalue()
