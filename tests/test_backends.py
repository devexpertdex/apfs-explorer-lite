from __future__ import annotations

import inspect
from pathlib import PurePosixPath

import pytest

from apfs_explorer_lite.backends import BackendRegistry, default_registry
from apfs_explorer_lite.backends.base import ApfsBackend, BackendAvailability, ReadableStream
from apfs_explorer_lite.backends.libfsapfs_backend import LibfsapfsBackend
from pathlib import Path

from apfs_explorer_lite.errors import (
    ApfsContainerOpenError,
    BackendNotOpenError,
    BackendUnavailableError,
    SafetyViolationError,
)
from apfs_explorer_lite.models import SourceDevice

from .doubles import InMemoryBackend

_WRITE_WORDS = ("write", "delete", "remove", "rename", "format", "create", "mkdir", "truncate", "repair")


def test_backend_interface_has_no_write_operations() -> None:
    names = [n for n, _ in inspect.getmembers(ApfsBackend) if not n.startswith("_")]
    names += [n for n in dir(ReadableStream) if not n.startswith("_")]
    offending = [n for n in names if any(word in n.lower() for word in _WRITE_WORDS)]
    assert offending == []


def test_libfsapfs_probe_reports_bindings() -> None:
    status = LibfsapfsBackend.probe()
    assert status.name == "libfsapfs"
    assert status.available, status.message
    assert status.version


def test_libfsapfs_requires_open_container(tmp_path: Path) -> None:
    backend = LibfsapfsBackend()
    assert not backend.is_open
    with pytest.raises(BackendNotOpenError):
        backend.list_volumes()
    with pytest.raises(BackendNotOpenError):
        backend.list_directory(0, PurePosixPath("/"))
    not_apfs = tmp_path / "zeros.img"
    not_apfs.write_bytes(bytes(1 << 20))
    with pytest.raises(ApfsContainerOpenError):
        backend.open(SourceDevice(str(not_apfs), offset=0, size=1 << 20))
    assert not backend.is_open
    backend.close()  # idempotent with nothing open


def test_default_registry_contains_libfsapfs() -> None:
    registry = default_registry()
    assert registry.names() == ["libfsapfs"]
    assert isinstance(registry.create("libfsapfs"), LibfsapfsBackend)


def test_registry_unknown_backend_raises() -> None:
    with pytest.raises(BackendUnavailableError):
        default_registry().create("does-not-exist")


class _UnavailableBackend(InMemoryBackend):
    name = "unavailable"

    @classmethod
    def probe(cls) -> BackendAvailability:
        return BackendAvailability(cls.name, False, None, "missing native library")


def test_registry_never_falls_back() -> None:
    registry = BackendRegistry()
    registry.register(_UnavailableBackend)
    registry.register(InMemoryBackend)
    with pytest.raises(BackendUnavailableError, match="unavailable"):
        registry.create("unavailable")


class _WritableBackend(InMemoryBackend):
    name = "writable"
    read_only = False


def test_registry_rejects_write_capable_backend() -> None:
    with pytest.raises(SafetyViolationError):
        BackendRegistry().register(_WritableBackend)


def test_registry_rejects_duplicate_names() -> None:
    registry = BackendRegistry()
    registry.register(InMemoryBackend)
    with pytest.raises(ValueError):
        registry.register(InMemoryBackend)


def test_context_manager_closes_backend() -> None:
    backend = InMemoryBackend({"/a.txt": b"x"})
    with backend:
        backend.open(SourceDevice("test", 0, 1))
        assert backend.is_open
    assert not backend.is_open


def test_stream_satisfies_protocol() -> None:
    backend = InMemoryBackend({"/a.txt": b"hello"})
    backend.open(SourceDevice("test", 0, 1))
    stream = backend.open_file(0, PurePosixPath("/a.txt"))
    assert isinstance(stream, ReadableStream)
    assert stream.read() == b"hello"
