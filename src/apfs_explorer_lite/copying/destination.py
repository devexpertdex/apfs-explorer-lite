"""All writes to the Windows destination folder go through :class:`Destination`.

Rules enforced here:

* Every path is built from sanitized name components under the resolved,
  user-chosen root; ``..``, separators and device names cannot appear.
* Folders on the way to a file are checked one by one with ``lstat``; a
  junction, symlink or mount point anywhere below the root is refused, so a
  link cannot redirect writes outside the destination.
* Content is written to a new, uniquely named temporary file (exclusive
  create), then moved into place. An existing file is only replaced when the
  caller explicitly asks, and then atomically (``os.replace``), so an
  interrupted copy never leaves a half-written file under a real name.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import uuid
from collections.abc import Sequence
from pathlib import Path

from ..errors import DestinationOutsideRootError, DestinationSafetyError
from ..safety import (
    extended_path,
    is_reparse_point,
    numbered_name,
    sanitize_component,
    validate_destination_root,
)
from ..structured_log import error_fields, log_event, path_fields

logger = logging.getLogger(__name__)

PARTIAL_SUFFIX = ".apfs-partial"
_DISK_FULL_WINERRORS = frozenset({39, 112})  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOINHERIT = getattr(os, "O_NOINHERIT", 0)


def is_disk_full(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS or exc.errno == errno.ENOSPC


# Largest single file per file system; others (NTFS, exFAT, ReFS) are effectively unlimited.
_MAX_FILE_SIZE = {"FAT32": 4 * 1024**3 - 1, "FAT": 2 * 1024**3 - 1, "FAT16": 2 * 1024**3 - 1, "FAT12": 32 * 1024**2}


def volume_file_system(path: Path) -> str | None:
    """File system name of the volume holding ``path`` ("NTFS", "FAT32", ...), or None."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    volume = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(wintypes.LPCWSTR(extended_path(path)), volume, len(volume)):
        return None
    fs_name = ctypes.create_unicode_buffer(64)
    ok = k32.GetVolumeInformationW(
        wintypes.LPCWSTR(volume.value), None, 0, None, None, None, fs_name, len(fs_name)
    )
    return (fs_name.value or None) if ok else None


class Destination:
    def __init__(self, root: Path) -> None:
        self.root = validate_destination_root(root)
        self.file_system = volume_file_system(self.root)

    def max_file_size(self) -> int | None:
        return _MAX_FILE_SIZE.get((self.file_system or "").upper())

    def check_writable(self) -> None:
        """Create and remove one temporary file in the root; raises OSError if Windows refuses."""
        fd, temp = self.create_temp(self.root)
        os.close(fd)
        self.remove_temp(temp)

    # --- paths ---------------------------------------------------------------------

    def path(self, parts: Sequence[str]) -> Path:
        """Join already-sanitized ``parts`` under the root."""
        for part in parts:
            if part in ("", ".", "..") or sanitize_component(part) != part:
                raise DestinationOutsideRootError(
                    "Refusing to write outside the selected destination folder.", detail=repr(part)
                )
        return self.root.joinpath(*parts)

    def lstat(self, path: Path) -> os.stat_result | None:
        try:
            return os.lstat(extended_path(path))
        except FileNotFoundError:
            return None

    def is_available(self) -> bool:
        return os.path.isdir(extended_path(self.root))

    def free_space(self) -> int:
        return shutil.disk_usage(extended_path(self.root)).free

    # --- folders ---------------------------------------------------------------------

    def check_folder(self, parts: Sequence[str]) -> Path:
        """Return the folder for ``parts`` after checking every level is a real folder."""
        current = self.root
        for depth in range(1, len(parts) + 1):
            current = self.path(parts[:depth])
            st = self.lstat(current)
            if st is None:
                raise FileNotFoundError(errno.ENOENT, "Folder is missing", str(current))
            self._require_plain_folder(current, st)
        return current

    def make_folder(self, parts: Sequence[str]) -> tuple[Path, bool]:
        """Create the last level of ``parts`` (parents must exist). Returns (path, created)."""
        self.check_folder(parts[:-1])
        path = self.path(parts)
        st = self.lstat(path)
        if st is not None:
            self._require_plain_folder(path, st)
            return path, False
        os.mkdir(extended_path(path))
        return path, True

    def is_plain_folder(self, path: Path) -> bool:
        st = self.lstat(path)
        return st is not None and stat.S_ISDIR(st.st_mode) and not is_reparse_point(st)

    @staticmethod
    def is_plain_file(st: os.stat_result) -> bool:
        return stat.S_ISREG(st.st_mode) and not is_reparse_point(st)

    def free_name(self, folder: Path, name: str, *, is_folder: bool = False) -> str:
        """First ``name (N)`` that does not exist in ``folder``."""
        number = 2
        while True:
            candidate = numbered_name(name, number, is_folder=is_folder)
            if self.lstat(folder / candidate) is None:
                return candidate
            number += 1

    @staticmethod
    def _require_plain_folder(path: Path, st: os.stat_result) -> None:
        if is_reparse_point(st):
            log_event(logger, logging.ERROR, "destination.link_refused", **path_fields(path))
            raise DestinationSafetyError(
                "A folder in the destination is a shortcut (link) to another location; "
                "files are not copied through links.",
                detail=str(path),
            )
        if not stat.S_ISDIR(st.st_mode):
            raise DestinationSafetyError(
                "A file in the destination has the same name as a folder being copied.",
                detail=str(path),
            )

    # --- files -------------------------------------------------------------------------

    def create_temp(self, folder: Path) -> tuple[int, Path]:
        """Create a new, empty, uniquely named temporary file in ``folder``."""
        temp = folder / f"~{uuid.uuid4().hex[:12]}{PARTIAL_SUFFIX}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOINHERIT
        fd = os.open(extended_path(temp), flags, 0o666)
        return fd, temp

    def open_for_verify(self, path: Path):  # noqa: ANN201 - binary file object
        return open(extended_path(path), "rb", buffering=0)  # noqa: SIM115

    def set_times(self, path: Path, accessed_ns: int, modified_ns: int) -> None:
        os.utime(extended_path(path), ns=(accessed_ns, modified_ns))

    def place(self, temp: Path, final: Path, *, replace: bool) -> None:
        """Move ``temp`` to ``final``. Without ``replace`` an existing file is never touched."""
        if final.parent != temp.parent:
            raise DestinationSafetyError("Internal error: temporary file in wrong folder.", detail=str(final))
        if replace:
            existing = self.lstat(final)
            if existing is not None and (is_reparse_point(existing) or stat.S_ISDIR(existing.st_mode)):
                raise DestinationSafetyError("Only ordinary files can be replaced.", detail=str(final))
            os.replace(extended_path(temp), extended_path(final))
        elif os.name == "nt":
            # MoveFileEx without REPLACE_EXISTING: fails with FileExistsError instead of overwriting.
            os.rename(extended_path(temp), extended_path(final))
        else:
            os.link(temp, final)  # fails if final exists; POSIX rename would overwrite
            os.remove(temp)

    def remove_temp(self, temp: Path) -> None:
        if not temp.name.endswith(PARTIAL_SUFFIX):
            raise DestinationSafetyError("Only temporary copy files can be removed.", detail=str(temp))
        try:
            os.remove(extended_path(temp))
        except FileNotFoundError:
            pass
        except OSError as exc:
            log_event(logger, logging.WARNING, "destination.temp_remove_failed", **path_fields(temp), **error_fields(exc))
