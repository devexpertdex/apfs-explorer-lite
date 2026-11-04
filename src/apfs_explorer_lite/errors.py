"""Structured exception hierarchy.

Every error raised deliberately by the application derives from
:class:`ApfsExplorerError`, so the GUI can catch one type and show
``user_message`` while the log keeps the technical ``detail``.
"""

from __future__ import annotations


class ApfsExplorerError(Exception):
    """Base class for all application errors."""

    def __init__(self, user_message: str, *, detail: str | None = None) -> None:
        super().__init__(user_message if detail is None else f"{user_message} ({detail})")
        self.user_message = user_message
        self.detail = detail


# --- Safety -----------------------------------------------------------------


class SafetyViolationError(ApfsExplorerError):
    """An operation would break one of the read-only / destination rules."""


class SourceWriteBlockedError(SafetyViolationError):
    """Something attempted to open or use the APFS source with write access."""


class DestinationSafetyError(SafetyViolationError):
    """The chosen destination folder is not acceptable."""


class DestinationOutsideRootError(DestinationSafetyError):
    """A computed destination path escapes the user-selected folder."""


# --- Backends ---------------------------------------------------------------


class BackendError(ApfsExplorerError):
    """Generic failure inside an APFS backend."""


class BackendUnavailableError(BackendError):
    """The requested backend is not installed or cannot be loaded."""


class BackendNotOpenError(BackendError):
    """An operation needs an opened container but none is open."""


class ApfsPathNotFoundError(BackendError):
    """A path does not exist inside the APFS volume."""


class ApfsContainerOpenError(BackendError):
    """The source could not be opened as an APFS container."""


class ApfsReadError(BackendError):
    """libfsapfs failed while reading metadata or file content."""


class ApfsUnsupportedFeatureError(BackendError):
    """The data uses an APFS feature this backend cannot read."""


class ApfsVolumeLockedError(ApfsUnsupportedFeatureError):
    """The volume is encrypted (locked); encrypted volumes are not supported."""


# --- Other services ---------------------------------------------------------


class DiskDiscoveryError(ApfsExplorerError):
    """Enumerating physical disks or partitions failed."""


class DiskAccessDeniedError(DiskDiscoveryError):
    """Windows denied access; usually administrator rights are missing."""


class DiskDisconnectedError(DiskDiscoveryError):
    """The disk disappeared (unplugged or not ready) while being queried."""

