"""Explicit backend selection.

There is deliberately no automatic fallback: if the requested backend is
unavailable the caller gets :class:`BackendUnavailableError`, never a
different (possibly write-capable) implementation.
"""

from __future__ import annotations

import logging

from ..errors import BackendUnavailableError, SafetyViolationError
from .base import ApfsBackend, BackendAvailability
from .libfsapfs_backend import LibfsapfsBackend

logger = logging.getLogger(__name__)

DEFAULT_BACKEND = LibfsapfsBackend.name


class BackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, type[ApfsBackend]] = {}

    def register(self, backend_cls: type[ApfsBackend]) -> None:
        if backend_cls.read_only is not True:
            raise SafetyViolationError(
                "Only read-only APFS backends can be registered.", detail=backend_cls.__name__
            )
        if backend_cls.name in self._backends:
            raise ValueError(f"Backend {backend_cls.name!r} is already registered")
        self._backends[backend_cls.name] = backend_cls

    def names(self) -> list[str]:
        return sorted(self._backends)

    def availability(self, name: str) -> BackendAvailability:
        return self._lookup(name).probe()

    def create(self, name: str) -> ApfsBackend:
        """Instantiate backend ``name`` or raise; never substitutes another."""
        backend_cls = self._lookup(name)
        status = backend_cls.probe()
        if not status.available:
            raise BackendUnavailableError(
                f"The {name} APFS backend is not available.", detail=status.message
            )
        logger.info("Using APFS backend %s %s", name, status.version)
        return backend_cls()

    def _lookup(self, name: str) -> type[ApfsBackend]:
        try:
            return self._backends[name]
        except KeyError:
            raise BackendUnavailableError(f"Unknown APFS backend {name!r}.") from None


def default_registry() -> BackendRegistry:
    registry = BackendRegistry()
    registry.register(LibfsapfsBackend)
    return registry
