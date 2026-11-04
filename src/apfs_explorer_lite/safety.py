"""Safety guards for the two non-negotiable rules.

1. The APFS source is only ever opened read-only.
2. Writes land only inside the user-selected destination folder.

Every service that touches the source or the destination must go through
these helpers rather than re-implementing the checks.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

from .errors import DestinationSafetyError, SourceWriteBlockedError

logger = logging.getLogger(__name__)

_WRITE_MODE_CHARS = frozenset("wax+")

# Characters that are invalid in NTFS file names under the Win32 API.
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{dev}{n}" for dev in ("COM", "LPT") for n in (*"123456789", "¹", "²", "³")}
)
# NTFS limits one name to 255 UTF-16 code units.
MAX_NAME_UNITS = 255
# Paths that address devices or raw volumes instead of a folder.
_RAW_DEVICE_PREFIXES = ("\\\\.\\", "//./", "\\\\?\\globalroot", "//?/globalroot")
_EXTENDED_PREFIX = "\\\\?\\"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def require_read_only_mode(mode: str) -> None:
    """Raise unless ``mode`` is a pure read mode such as ``"rb"``."""
    if "r" not in mode or _WRITE_MODE_CHARS.intersection(mode):
        logger.error("Blocked attempt to open APFS source with mode %r", mode)
        raise SourceWriteBlockedError(
            "The APFS source can only be opened read-only.", detail=f"mode={mode!r}"
        )


def sanitize_component(name: str) -> str:
    """Turn one APFS file name into a valid single NTFS path component.

    Distinct APFS names can map to the same result (``a:b`` and ``a_b``);
    the copy service must detect such collisions rather than overwrite.
    Over-long names are shortened, keeping the extension. The result is
    stable: sanitizing it again returns it unchanged.
    """
    cleaned = _INVALID_NAME_CHARS.sub("_", name).rstrip(" .")
    if not cleaned:
        return "_"
    if cleaned.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_DEVICE_NAMES:
        cleaned = f"_{cleaned}"
    return shorten_name(cleaned, MAX_NAME_UNITS)


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def shorten_name(name: str, limit: int, *, suffix: str = "", keep_extension: bool = True) -> str:
    """Return ``name`` + ``suffix`` (before the extension) within ``limit`` UTF-16 units."""
    stem, dot, ext = name.rpartition(".")
    if not keep_extension or not dot or not stem or _utf16_units(ext) > 16:
        stem, ext = name, ""
    else:
        ext = f".{ext}"
    budget = limit - _utf16_units(suffix + ext)
    if _utf16_units(stem) > budget:
        while _utf16_units(stem) > budget:
            stem = stem[:-1]
        stem = stem.rstrip(" .") or "_"  # a cut may leave a trailing dot or space
    return f"{stem}{suffix}{ext}"


def numbered_name(name: str, number: int, *, is_folder: bool = False) -> str:
    """``report.docx`` -> ``report (2).docx``; folders keep dots in their name."""
    return shorten_name(name, MAX_NAME_UNITS, suffix=f" ({number})", keep_extension=not is_folder)


def extended_path(path: Path) -> str:
    """Windows extended-length form (``\\\\?\\C:\\...``), so long paths work everywhere.

    Only valid for absolute, already-normalized paths whose components were
    produced by :func:`sanitize_component` (the prefix disables Windows' own
    name clean-up, e.g. of trailing dots).
    """
    text = str(path)
    if os.name != "nt" or text.startswith(_EXTENDED_PREFIX):
        return text
    if text.startswith("\\\\"):
        return f"{_EXTENDED_PREFIX}UNC\\{text[2:]}"
    return f"{_EXTENDED_PREFIX}{text}"


def is_reparse_point(st: os.stat_result) -> bool:
    """True for symlinks, junctions and mount points (from ``os.lstat``)."""
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(st.st_mode)


def system_folders() -> list[Path]:
    """Windows folders that must never receive copied files."""
    names = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
    return [Path(os.environ[n]) for n in names if os.environ.get(n)]


def is_inside_system_folder(path: Path) -> bool:
    for folder in system_folders():
        try:
            resolved = folder.resolve()
        except OSError:
            continue
        if path == resolved or path.is_relative_to(resolved):
            return True
    return False


def validate_destination_root(root: Path) -> Path:
    """Check that ``root`` is an existing local folder and return it resolved."""
    if str(root).lower().startswith(_RAW_DEVICE_PREFIXES):
        raise DestinationSafetyError(
            "The destination must be a folder, not a device path.", detail=str(root)
        )
    if not root.is_absolute():
        raise DestinationSafetyError("The destination must be an absolute path.", detail=str(root))
    try:
        resolved = root.resolve(strict=True)
    except PermissionError as exc:
        raise DestinationSafetyError(
            "Windows doesn't allow access to this folder. Choose another folder, for example one inside Documents.",
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise DestinationSafetyError(
            "The destination folder does not exist.", detail=str(exc)
        ) from exc
    if not resolved.is_dir():
        raise DestinationSafetyError("The destination is not a folder.", detail=str(resolved))
    if is_inside_system_folder(resolved):
        raise DestinationSafetyError(
            "Windows system folders can't be used as the destination. Choose a folder such as Documents.",
            detail=str(resolved),
        )
    return resolved
