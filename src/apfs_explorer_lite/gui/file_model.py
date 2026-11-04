"""Qt model for the file list: real APFS entries only, sorted and filtered."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QDateTime,
    QLocale,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)

from ..models import ApfsEntry, EntryKind
from .file_types import FileTypeProvider
from .formatting import format_size

COLUMNS = ("Name", "Type", "Size", "Date Modified")
COL_NAME, COL_TYPE, COL_SIZE, COL_MODIFIED = range(4)
ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2

_Index = QModelIndex | QPersistentModelIndex


def is_hidden(entry: ApfsEntry) -> bool:
    """macOS hides dot-files (.DS_Store, .Spotlight-V100, ._resource forks...)."""
    return entry.name.startswith(".")


def format_datetime(moment: datetime | None) -> str:
    if moment is None:
        return ""
    local = QDateTime.fromMSecsSinceEpoch(int(moment.timestamp() * 1000)).toLocalTime()
    return QLocale.system().toString(local, QLocale.FormatType.ShortFormat)


class FileListModel(QAbstractTableModel):
    """Entries of one folder. Sorting happens here, in Python with precomputed keys:
    letting the proxy call a Python ``lessThan`` per comparison froze the window
    for seconds on folders with tens of thousands of entries."""

    def __init__(self, types: FileTypeProvider, parent: Any = None) -> None:
        super().__init__(parent)
        self._types = types
        self._entries: list[ApfsEntry] = []
        self._folded: list[str] = []  # casefolded names, for search and hidden checks
        self._sort_column = COL_NAME
        self._sort_order = Qt.SortOrder.AscendingOrder

    @property
    def entries(self) -> list[ApfsEntry]:
        return list(self._entries)

    def folded_name(self, row: int) -> str:
        return self._folded[row]

    def set_entries(self, entries: list[ApfsEntry]) -> None:
        self.beginResetModel()
        self._entries = self._sorted(list(entries))
        self._folded = [e.name.casefold() for e in self._entries]
        self.endResetModel()

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self._sort_column, self._sort_order = column, order
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        moved = [self._entries[i.row()] for i in old]
        self._entries = self._sorted(self._entries)
        self._folded = [e.name.casefold() for e in self._entries]
        row_of = {id(e): row for row, e in enumerate(self._entries)}
        self.changePersistentIndexList(old, [self.index(row_of[id(e)], i.column()) for i, e in zip(old, moved, strict=True)])
        self.layoutChanged.emit()

    def _sorted(self, entries: list[ApfsEntry]) -> list[ApfsEntry]:
        column = self._sort_column
        if not 0 <= column < len(COLUMNS):
            return entries
        reverse = self._sort_order == Qt.SortOrder.DescendingOrder
        return sorted(entries, key=lambda e: self.sort_key(e, column), reverse=reverse)

    def sort_key(self, entry: ApfsEntry, column: int) -> tuple[int, Any]:
        folders_first = 0 if entry.kind is EntryKind.DIRECTORY else 1
        name = entry.name.casefold()
        if column == COL_NAME:
            key: Any = name
        elif column == COL_TYPE:
            key = (self._types.type_name(entry).casefold(), name)
        elif column == COL_SIZE:
            key = (entry.size if entry.kind is EntryKind.FILE else -1, name)
        else:
            key = (entry.modified.timestamp() if entry.modified else 0.0, name)
        return (folders_first, key)

    def clear(self) -> None:
        self.set_entries([])

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation is Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return COLUMNS[section]
            if role == Qt.ItemDataRole.TextAlignmentRole and section == COL_SIZE:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        entry = self._entries[index.row()]
        column = index.column()
        if role == ENTRY_ROLE:
            return entry
        if role == Qt.ItemDataRole.DisplayRole:
            if column == COL_NAME:
                return entry.name
            if column == COL_TYPE:
                return self._types.type_name(entry)
            if column == COL_SIZE:
                return format_size(entry.size) if entry.kind is EntryKind.FILE else ""
            if column == COL_MODIFIED:
                return format_datetime(entry.modified)
        if role == Qt.ItemDataRole.DecorationRole and column == COL_NAME:
            return self._types.icon(entry)
        if role == Qt.ItemDataRole.TextAlignmentRole and column == COL_SIZE:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            if entry.kind is EntryKind.SYMLINK:
                return f"{entry.path}\nSymbolic link to {entry.symlink_target} (not followed)"
            if entry.compression is not None and not entry.compression.supported:
                return f"{entry.path}\nCompressed with {entry.compression.method_name}, which cannot be read"
            return str(entry.path)
        if role == SORT_ROLE:
            return self.sort_key(entry, column)
        return None


class FileFilterProxy(QSortFilterProxyModel):
    """Search by name and optional hiding of macOS dot-files.

    Sorting is passed to :class:`FileListModel` (the proxy keeps source order).
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._search = ""
        self._show_hidden = False

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        source = self.sourceModel()
        if isinstance(source, FileListModel):
            source.sort(column, order)

    @property
    def search_text(self) -> str:
        return self._search

    def set_search(self, text: str) -> None:
        self.beginFilterChange()
        self._search = text.strip().casefold()
        self.endFilterChange()

    def set_show_hidden(self, show: bool) -> None:
        self.beginFilterChange()
        self._show_hidden = show
        self.endFilterChange()

    def filterAcceptsRow(self, source_row: int, source_parent: _Index) -> bool:
        source: FileListModel = self.sourceModel()  # type: ignore[assignment]
        name = source.folded_name(source_row)
        if not self._show_hidden and name.startswith("."):
            return False
        return not self._search or self._search in name
