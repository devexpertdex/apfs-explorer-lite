"""File-type icons and names ("Microsoft Word Document") by extension.

On Windows these come from the shell (SHGetFileInfoW with
SHGFI_USEFILEATTRIBUTES), which looks up the registry by extension only and
never touches a file. Elsewhere, or if the shell call fails, generic Qt icons
and "<EXT> File" names are used.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from pathlib import PurePosixPath

from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import QFileIconProvider

from ..models import ApfsEntry, EntryKind

logger = logging.getLogger(__name__)

FOLDER_TYPE = "File folder"
SYMLINK_TYPE = "Symbolic link"
OTHER_TYPE = "Special file"

_SHGFI_ICON = 0x100
_SHGFI_SMALLICON = 0x1
_SHGFI_LARGEICON = 0x0
_SHGFI_TYPENAME = 0x400
_SHGFI_USEFILEATTRIBUTES = 0x10
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_NORMAL = 0x80


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HANDLE),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


class FileTypeProvider:
    """Caches icons and type names per extension. Use from the GUI thread only."""

    def __init__(self) -> None:
        self._cache: dict[tuple[bool, str], tuple[QIcon, str]] = {}
        self._fallback = QFileIconProvider()
        self._shell = None
        self._user32 = None
        if sys.platform == "win32":
            try:
                shell = ctypes.WinDLL("shell32")
                shell.SHGetFileInfoW.argtypes = [
                    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_SHFILEINFOW),
                    wintypes.UINT, wintypes.UINT,
                ]  # fmt: skip
                shell.SHGetFileInfoW.restype = ctypes.c_size_t
                user32 = ctypes.WinDLL("user32")
                user32.DestroyIcon.argtypes = [wintypes.HANDLE]
                self._shell, self._user32 = shell, user32
            except (OSError, AttributeError) as exc:
                logger.info("Shell file-type lookup unavailable: %s", exc)

    def icon(self, entry: ApfsEntry) -> QIcon:
        return self._lookup(entry)[0]

    def type_name(self, entry: ApfsEntry) -> str:
        if entry.kind is EntryKind.SYMLINK:
            return SYMLINK_TYPE
        if entry.kind is EntryKind.OTHER:
            return OTHER_TYPE
        return self._lookup(entry)[1]

    def folder_icon(self) -> QIcon:
        return self._lookup_key(True, "")[0]

    def _lookup(self, entry: ApfsEntry) -> tuple[QIcon, str]:
        if entry.kind is EntryKind.DIRECTORY:
            return self._lookup_key(True, "")
        return self._lookup_key(False, PurePosixPath(entry.name).suffix.lower())

    def _lookup_key(self, is_folder: bool, suffix: str) -> tuple[QIcon, str]:
        key = (is_folder, suffix)
        cached = self._cache.get(key)
        if cached is None:
            cached = self._shell_lookup(is_folder, suffix) or self._generic(is_folder, suffix)
            self._cache[key] = cached
        return cached

    def _generic(self, is_folder: bool, suffix: str) -> tuple[QIcon, str]:
        if is_folder:
            return self._fallback.icon(QFileIconProvider.IconType.Folder), FOLDER_TYPE
        name = f"{suffix[1:].upper()} File" if suffix else "File"
        return self._fallback.icon(QFileIconProvider.IconType.File), name

    def _shell_lookup(self, is_folder: bool, suffix: str) -> tuple[QIcon, str] | None:
        if self._shell is None or self._user32 is None:
            return None
        name = "folder" if is_folder else f"file{suffix}"
        attributes = _FILE_ATTRIBUTE_DIRECTORY if is_folder else _FILE_ATTRIBUTE_NORMAL
        icon = QIcon()
        type_name = ""
        for size_flag in (_SHGFI_SMALLICON, _SHGFI_LARGEICON):
            info = _SHFILEINFOW()
            flags = _SHGFI_ICON | _SHGFI_TYPENAME | _SHGFI_USEFILEATTRIBUTES | size_flag
            if not self._shell.SHGetFileInfoW(name, attributes, ctypes.byref(info), ctypes.sizeof(info), flags):
                return None
            type_name = info.szTypeName
            if info.hIcon:
                image = QImage.fromHICON(info.hIcon)
                self._user32.DestroyIcon(info.hIcon)
                if not image.isNull():
                    icon.addPixmap(QPixmap.fromImage(image))
        if icon.isNull():
            return None
        if is_folder:
            type_name = FOLDER_TYPE
        return icon, type_name or self._generic(is_folder, suffix)[1]
