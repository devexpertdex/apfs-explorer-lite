"""Drive-selection screen listing external APFS candidates."""

from __future__ import annotations

import html
import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..disks.discovery import DiscoveryResult, DiskReport
from ..errors import ApfsExplorerError
from ..models import ApfsContainerCandidate, ApfsIdentification, Partition
from .formatting import format_size
from .theme import banner_label

logger = logging.getLogger(__name__)

ScanFunction = Callable[[], DiscoveryResult]
ElevateFunction = Callable[[], bool]
# (disk number, disk serial, partition number) of the selected container.
_SelectionKey = tuple[int, str, int]

KIND_DISK = "Physical disk"
KIND_PARTITION = "Partition"
KIND_CONTAINER = "APFS container"

INITIAL_TITLE = "Find your APFS drive"
INITIAL_BODY = (
    "Connect the external APFS drive, then click Refresh Drives.\n"
    "Disks are only scanned when you ask, and the scan never writes to any disk."
)
NONE_FOUND_TITLE = "No external APFS drives found"
NONE_FOUND_BODY = (
    "Check that the drive is connected and shows up in Disk Management, then click "
    "Refresh Drives. Internal disks and partitions without an Apple APFS partition type "
    "are not listed."
)
SCAN_FAILED_TITLE = "Drive scan failed"
ADMIN_BANNER_TEXT = (
    "Running without administrator rights. APFS partitions are detected from the "
    "partition table only; restart as administrator to verify the APFS signature."
)
DISCONNECTED_TEXT = "The previously selected drive is no longer connected."
OPEN_HINT_SELECT = "Select an APFS container to open it."
OPENABLE = frozenset({ApfsIdentification.CONFIRMED, ApfsIdentification.RECOVERABLE})


class _ScanSignals(QObject):
    finished = Signal(object)
    failed = Signal(object)


class _ScanTask(QRunnable):
    def __init__(self, scan: ScanFunction) -> None:
        super().__init__()
        self._scan = scan
        self.signals = _ScanSignals()

    def run(self) -> None:
        try:
            result = self._scan()
        except ApfsExplorerError as exc:
            logger.warning("Drive scan failed: %s", exc)
            self.signals.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            logger.exception("Unexpected error during drive scan")
            self.signals.failed.emit(
                ApfsExplorerError("Unexpected error while scanning drives.", detail=repr(exc))
            )
        else:
            self.signals.finished.emit(result)


class _MessagePanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title_label = QLabel()
        self.title_label.setObjectName("messageTitle")
        font = self.title_label.font()
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        self.title_label.setFont(font)
        self.body_label = QLabel()
        self.body_label.setObjectName("messageBody")
        self.body_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        for label in (self.title_label, self.body_label):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        layout.addStretch(1)

    def show_message(self, title: str, body: str) -> None:
        self.title_label.setText(title)
        self.body_label.setText(body)


class DriveSelectionWidget(QWidget):
    """Lists external disks with APFS containers as disk > partition > container."""

    scanFinished = Signal(object)  # DiscoveryResult, or ApfsExplorerError on failure
    selectionChanged = Signal(object)  # ApfsContainerCandidate | None
    openRequested = Signal(object, str)  # ApfsContainerCandidate, drive title

    def __init__(
        self,
        scan: ScanFunction,
        *,
        is_admin: bool,
        elevate: ElevateFunction | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("driveSelection")
        self._scan = scan
        self._elevate = elevate
        self._scanning = False
        self._result: DiscoveryResult | None = None
        self._selected_key: _SelectionKey | None = None
        self._active_task: _ScanTask | None = None
        self._backend_problem: str | None = None

        heading = QLabel("Select an external APFS drive")
        heading_font = heading.font()
        heading_font.setPointSizeF(heading_font.pointSizeF() * 1.2)
        heading_font.setBold(True)
        heading.setFont(heading_font)

        self.refresh_button = QPushButton("Refresh Drives")
        self.refresh_button.setObjectName("refreshButton")
        self.refresh_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_button.clicked.connect(self.refresh)

        header = QHBoxLayout()
        header.addWidget(heading, 1)
        header.addWidget(self.refresh_button)

        self.admin_banner = self._build_admin_banner()
        self.admin_banner.setVisible(not is_admin)

        self.status_label = QLabel("Not scanned yet.")
        self.status_label.setObjectName("scanStatus")
        self.status_label.setWordWrap(True)
        self.warnings_label = QLabel()
        self.warnings_label.setObjectName("scanWarnings")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setVisible(False)

        self.tree = QTreeWidget()
        self.tree.setObjectName("driveTree")
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Item", "Kind", "Size", "Details"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._on_current_item_changed)

        self.details_label = QLabel("Select an item to see its details.")
        self.details_label.setObjectName("details")
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.details_label.setMinimumWidth(260)

        self.open_button = QPushButton("Open")
        self.open_button.setObjectName("openButton")
        self.open_button.setEnabled(False)
        self.open_button.setToolTip(OPEN_HINT_SELECT)
        self.open_button.clicked.connect(self._request_open)
        self.tree.itemDoubleClicked.connect(lambda _item, _col: self._request_open())

        details_box = QWidget()
        details_layout = QVBoxLayout(details_box)
        details_layout.setContentsMargins(8, 0, 0, 0)
        details_layout.addWidget(self.details_label, 1)
        details_layout.addWidget(self.open_button)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(details_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        self.message_panel = _MessagePanel()
        self.message_panel.show_message(INITIAL_TITLE, INITIAL_BODY)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.message_panel)
        self.stack.addWidget(splitter)

        self.notice_label = banner_label("notice")

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.notice_label)
        layout.addWidget(self.admin_banner)
        layout.addWidget(self.status_label)
        layout.addWidget(self.warnings_label)
        layout.addWidget(self.stack, 1)

    # --- public API ---------------------------------------------------------

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    @property
    def result(self) -> DiscoveryResult | None:
        return self._result

    @property
    def showing_tree(self) -> bool:
        return self.stack.currentIndex() == 1

    def selected_container(self) -> ApfsContainerCandidate | None:
        item = self.tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return data if isinstance(data, ApfsContainerCandidate) else None

    @Slot()
    def refresh(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self.refresh_button.setEnabled(False)
        self.status_label.setText("Scanning drives…")
        task = _ScanTask(self._scan)
        task.signals.finished.connect(self._on_scan_finished)
        task.signals.failed.connect(self._on_scan_failed)
        self._active_task = task  # keep the signals object alive
        QThreadPool.globalInstance().start(task)

    # --- scan results -------------------------------------------------------

    @Slot(object)
    def _on_scan_finished(self, result: DiscoveryResult) -> None:
        self._finish_scan()
        self._result = result
        self.admin_banner.setVisible(not result.is_admin)
        self._populate(result)
        self.scanFinished.emit(result)

    @Slot(object)
    def _on_scan_failed(self, error: ApfsExplorerError) -> None:
        self._finish_scan()
        self._result = None
        self.tree.clear()
        self.status_label.setText("The last scan failed.")
        self.warnings_label.setVisible(False)
        self.message_panel.show_message(SCAN_FAILED_TITLE, error.user_message)
        self.stack.setCurrentIndex(0)
        self.scanFinished.emit(error)

    def _finish_scan(self) -> None:
        self._scanning = False
        self._active_task = None
        self.refresh_button.setEnabled(True)

    def _populate(self, result: DiscoveryResult) -> None:
        previous_key = self._selected_key
        self.tree.blockSignals(True)
        self.tree.clear()
        restore: QTreeWidgetItem | None = None
        for report in result.candidates:
            disk_item = self._add_disk_item(report)
            for partition in report.partitions:
                part_item = self._add_partition_item(disk_item, partition)
                container = report.container_for(partition)
                if container is not None and container.is_displayable:
                    item = self._add_container_item(part_item, container)
                    if self._key_for(report, container) == previous_key:
                        restore = item
        self.tree.expandAll()
        self.tree.blockSignals(False)

        count = len(result.candidates)
        drives = "drive" if count == 1 else "drives"
        self.status_label.setText(
            f"Found {count} external APFS {drives}. Scanned {len(result.disks)} disk(s), "
            f"{result.hidden_count} hidden (internal, or no APFS container found). "
            f"Last refreshed {result.scanned_at:%H:%M:%S}."
        )
        self.warnings_label.setText("\n".join(f"⚠ {w}" for w in result.warnings))
        self.warnings_label.setVisible(bool(result.warnings))

        if count == 0:
            self.message_panel.show_message(NONE_FOUND_TITLE, NONE_FOUND_BODY)
            self.stack.setCurrentIndex(0)
        else:
            self.stack.setCurrentIndex(1)

        if restore is not None:
            self.tree.setCurrentItem(restore)
        else:
            self._selected_key = None
            self.tree.setCurrentItem(None)
            self.details_label.setText(
                DISCONNECTED_TEXT if previous_key is not None else "Select an item to see its details."
            )
            self._update_open_button(None)
            self.selectionChanged.emit(None)

    # --- tree items ---------------------------------------------------------

    def _add_disk_item(self, report: DiskReport) -> QTreeWidgetItem:
        disk = report.disk
        item = QTreeWidgetItem(
            self.tree,
            [
                f"Disk {disk.number}: {disk.model}",
                KIND_DISK,
                format_size(disk.size_bytes),
                f"{report.external_reason} · {report.partition_style.value}",
            ],
        )
        item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon))
        item.setData(0, Qt.ItemDataRole.UserRole, report)
        return item

    def _add_partition_item(self, parent: QTreeWidgetItem, partition: Partition) -> QTreeWidgetItem:
        label = f"Partition {partition.number}"
        if partition.name:
            label += f" ({partition.name})"
        item = QTreeWidgetItem(
            parent, [label, KIND_PARTITION, format_size(partition.size), partition.type_label]
        )
        item.setData(0, Qt.ItemDataRole.UserRole, partition)
        return item

    def _add_container_item(
        self, parent: QTreeWidgetItem, container: ApfsContainerCandidate
    ) -> QTreeWidgetItem:
        confirmed = container.identification is ApfsIdentification.CONFIRMED
        status = {
            ApfsIdentification.CONFIRMED: "Verified APFS",
            ApfsIdentification.RECOVERABLE: "Recoverable APFS (damaged first block)",
        }.get(container.identification, "Tentative APFS")
        item = QTreeWidgetItem(
            parent,
            ["APFS container", KIND_CONTAINER, format_size(container.partition.size), status],
        )
        icon = (
            QStyle.StandardPixmap.SP_DialogApplyButton
            if confirmed
            else QStyle.StandardPixmap.SP_MessageBoxWarning
        )
        item.setIcon(0, self.style().standardIcon(icon))
        item.setToolTip(3, container.reason)
        item.setData(0, Qt.ItemDataRole.UserRole, container)
        return item

    # --- selection / details ------------------------------------------------

    @staticmethod
    def _key_for(report: DiskReport, container: ApfsContainerCandidate) -> _SelectionKey:
        return (report.disk.number, report.disk.serial, container.partition.number)

    def _report_for_disk(self, disk_number: int) -> DiskReport | None:
        if self._result is None:
            return None
        return next((r for r in self._result.disks if r.disk.number == disk_number), None)

    @Slot(QTreeWidgetItem, QTreeWidgetItem)
    def _on_current_item_changed(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        data = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        container = data if isinstance(data, ApfsContainerCandidate) else None
        if container is not None:
            report = self._report_for_disk(container.partition.disk_number)
            self._selected_key = self._key_for(report, container) if report else None
        self.details_label.setText(self._details_html(data))
        self._update_open_button(container)
        self.selectionChanged.emit(container)

    # --- opening ------------------------------------------------------------------

    def set_backend_problem(self, message: str | None) -> None:
        """Show a backend-unavailable state; nothing can be opened while it is set."""
        self._backend_problem = message
        if message:
            self.show_notice(f"The APFS reader is not available, so drives can't be opened.\n{message}")
        self._update_open_button(self.selected_container())

    def show_notice(self, text: str | None) -> None:
        self.notice_label.setText(text or "")
        self.notice_label.setVisible(bool(text))

    def _can_open(self, container: ApfsContainerCandidate | None) -> tuple[bool, str]:
        if self._backend_problem:
            return False, "The APFS reader is not available."
        if container is None:
            return False, OPEN_HINT_SELECT
        if container.identification in OPENABLE:
            return True, "Open this drive and browse its files (read-only)."
        return False, "Restart as administrator and refresh, so the APFS signature can be verified first."

    def _update_open_button(self, container: ApfsContainerCandidate | None) -> None:
        enabled, tip = self._can_open(container)
        self.open_button.setEnabled(enabled)
        self.open_button.setToolTip(tip)

    @Slot()
    def _request_open(self) -> None:
        container = self.selected_container()
        enabled, _tip = self._can_open(container)
        if not enabled or container is None:
            return
        report = self._report_for_disk(container.partition.disk_number)
        title = f"Disk {report.disk.number}: {report.disk.model}" if report else container.partition.type_label
        self.openRequested.emit(container, title)

    def _details_html(self, data: object) -> str:
        def rows(pairs: list[tuple[str, object]]) -> str:
            body = "".join(
                f"<tr><td><b>{html.escape(k)}</b>&nbsp;</td><td>{html.escape(str(v))}</td></tr>"
                for k, v in pairs
            )
            return f"<table>{body}</table>"

        if isinstance(data, DiskReport):
            d = data.disk
            return f"<h3>{KIND_DISK}</h3>" + rows([
                ("Disk number", d.number),
                ("Model", d.model),
                ("Serial", d.serial or "Unknown"),
                ("Capacity", f"{format_size(d.size_bytes)} ({d.size_bytes:,} bytes)"),
                ("Interface", d.bus_type),
                ("Why external", data.external_reason),
                ("Sector size", f"{d.bytes_per_sector} bytes"),
                ("Partition table", data.partition_style.value),
                ("Device path", d.device_path),
            ])  # fmt: skip
        if isinstance(data, Partition):
            return f"<h3>{KIND_PARTITION}</h3>" + rows([
                ("Disk / partition", f"{data.disk_number} / {data.number}"),
                ("Type", data.type_label),
                ("Type ID", data.type_id),
                ("Name", data.name or "—"),
                ("Offset", f"{data.offset:,} bytes"),
                ("Size", f"{format_size(data.size)} ({data.size:,} bytes)"),
            ])  # fmt: skip
        if isinstance(data, ApfsContainerCandidate):
            pairs: list[tuple[str, object]] = [
                ("Identification", data.identification.value.capitalize()),
                ("Reason", data.reason),
                ("Location", f"Disk {data.partition.disk_number}, partition {data.partition.number}"),
            ]
            if data.block_size is not None:
                pairs.append(("Block size", f"{data.block_size} bytes"))
            if data.block_count is not None and data.block_size is not None:
                pairs.append(("Container size", format_size(data.block_count * data.block_size)))
            if data.container_uuid:
                pairs.append(("Container UUID", data.container_uuid))
            return f"<h3>{KIND_CONTAINER}</h3>" + rows(pairs)
        return "Select an item to see its details."

    # --- elevation ----------------------------------------------------------

    def _build_admin_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("adminBanner")
        banner.setFrameShape(QFrame.Shape.StyledPanel)
        icon = QLabel()
        icon.setPixmap(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation).pixmap(24, 24)
        )
        self.admin_label = QLabel(ADMIN_BANNER_TEXT)
        self.admin_label.setWordWrap(True)
        self.elevate_button = QPushButton("Restart as Administrator")
        self.elevate_button.setObjectName("elevateButton")
        self.elevate_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_VistaShield))
        self.elevate_button.setVisible(self._elevate is not None)
        self.elevate_button.clicked.connect(self._on_elevate_clicked)

        layout = QHBoxLayout(banner)
        layout.addWidget(icon)
        layout.addWidget(self.admin_label, 1)
        layout.addWidget(self.elevate_button)
        return banner

    @Slot()
    def _on_elevate_clicked(self) -> None:
        if self._elevate is None:
            return
        if self._elevate():
            QApplication.quit()
        else:
            self.admin_label.setText(
                ADMIN_BANNER_TEXT + "\n\nThe administrator restart was cancelled or failed."
            )
