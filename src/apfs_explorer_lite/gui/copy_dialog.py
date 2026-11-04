"""Copy-to-this-PC dialog: choose a folder, watch progress, read the result.

The copy runs on its own thread (``CopyJob.run``); progress, conflict
questions and the final report reach the dialog through queued Qt signals.
While the job waits for a conflict answer it polls its cancel flag, so
Cancel always wins.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..backends.base import ApfsBackend
from ..copying import (
    Conflict,
    ConflictChoice,
    ConflictDecision,
    ConflictPolicy,
    CopyJob,
    CopyProgress,
    CopyReport,
    JobStatus,
    Outcome,
    Phase,
)
from ..errors import ApfsExplorerError
from ..formatting import format_duration, format_size
from ..models import ApfsEntry, EntryKind
from .file_model import format_datetime
from .theme import banner_label

logger = logging.getLogger(__name__)

LAST_DESTINATION_KEY = "lastDestination"
POLICY_KEY = "conflictPolicy"
POLICY_LABELS = {
    ConflictPolicy.ASK: "Ask me what to do",
    ConflictPolicy.SKIP: "Skip it (keep the file that is already there)",
    ConflictPolicy.RENAME: "Keep both (save the new copy with a number)",
}
SAFETY_NOTE = (
    "Nothing on the APFS drive is changed. Each file is read back after copying and "
    "checked against the original (size and SHA-256); only matching copies are kept."
)

FolderChooser = Callable[[QWidget, str], str]
ReportFileChooser = Callable[[QWidget, str], str]
ConflictPrompt = Callable[[QWidget, Conflict], ConflictDecision]


def _choose_folder(parent: QWidget, start: str) -> str:
    return QFileDialog.getExistingDirectory(parent, "Choose where to save the copied files", start)


def _choose_report_file(parent: QWidget, suggested: str) -> str:
    path, _filter = QFileDialog.getSaveFileName(parent, "Save copy report", suggested, "Text files (*.txt)")
    return path


def ask_conflict(parent: QWidget, conflict: Conflict) -> ConflictDecision:
    """Ask what to do about one name that already exists in the destination."""
    box = QMessageBox(parent)
    box.setWindowTitle("File already exists")
    box.setIcon(QMessageBox.Icon.Question)
    source = conflict.source
    box.setText(f"“{conflict.destination.name}” already exists in “{conflict.destination.parent}”.")
    if conflict.can_replace:
        box.setInformativeText(
            f"Already in the folder: {format_size(conflict.existing_size or 0)}, "
            f"modified {format_datetime(conflict.existing_modified)}\n"
            f"From the APFS drive: {format_size(source.size)}, modified {format_datetime(source.modified)}"
        )
        replace = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
    else:
        box.setInformativeText("A folder or shortcut with that name is in the way, so it can't be replaced.")
        replace = None
    keep_both = box.addButton("Keep both", QMessageBox.ButtonRole.AcceptRole)
    skip = box.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(skip)
    box.setEscapeButton(skip)
    remember = QCheckBox("Do this for all remaining conflicts")
    box.setCheckBox(remember)
    box.exec()
    clicked = box.clickedButton()
    if replace is not None and clicked is replace:
        choice = ConflictChoice.REPLACE
    elif clicked is keep_both:
        choice = ConflictChoice.RENAME
    else:
        choice = ConflictChoice.SKIP
    return ConflictDecision(choice, remember.isChecked())


class _Bridge(QObject):
    progress = Signal(object)  # CopyProgress
    conflict = Signal(object)  # Conflict
    done = Signal(object)  # CopyReport


class CopyDialog(QDialog):
    """Non-modal: the explorer stays usable, but only one copy can run at a time."""

    copyStarted = Signal()
    copyFinished = Signal(object)  # CopyReport

    def __init__(
        self,
        backend: ApfsBackend,
        volume_index: int,
        volume_name: str,
        sources: Sequence[ApfsEntry],
        *,
        settings: QSettings | None = None,
        choose_folder: FolderChooser = _choose_folder,
        choose_report_file: ReportFileChooser = _choose_report_file,
        conflict_prompt: ConflictPrompt = ask_conflict,
        open_folder: Callable[[Path], object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("copyDialog")
        self.setWindowTitle("Copy to this PC")
        self.setMinimumWidth(560)
        self._backend = backend
        self._volume_index = volume_index
        self._sources = list(sources)
        self._settings = settings
        self._choose_folder = choose_folder
        self._choose_report_file = choose_report_file
        self._conflict_prompt = conflict_prompt
        self._open_folder = open_folder or (lambda p: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
        self._job: CopyJob | None = None
        self._thread: threading.Thread | None = None
        self._report: CopyReport | None = None
        self._answer: ConflictDecision | None = None
        self._answered = threading.Event()
        self._bridge = _Bridge(self)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.conflict.connect(self._on_conflict)
        self._bridge.done.connect(self._on_done)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_setup(volume_name))
        self.pages.addWidget(self._build_progress())
        self.pages.addWidget(self._build_result())
        layout = QVBoxLayout(self)
        layout.addWidget(self.pages)

    # --- pages -------------------------------------------------------------------------

    def _heading(self, text: str = "") -> QLabel:
        label = QLabel(text)
        font = label.font()
        font.setPointSizeF(font.pointSizeF() * 1.35)
        font.setWeight(QFont.Weight.DemiBold)
        label.setFont(font)
        label.setWordWrap(True)
        return label

    def _build_setup(self, volume_name: str) -> QWidget:
        page = QWidget()
        page.setObjectName("setupPage")
        count = len(self._sources)
        files = [e for e in self._sources if e.kind is EntryKind.FILE]
        folders = [e for e in self._sources if e.is_directory]
        self.setup_heading = self._heading(f"Copy {count} item{'s' if count != 1 else ''} to this PC")
        names = ", ".join(f"“{e.name}”" for e in self._sources[:3])
        if count > 3:
            names += f" and {count - 3} more"
        parts = []
        if files:
            parts.append(f"{len(files)} file{'s' if len(files) != 1 else ''} ({format_size(sum(e.size for e in files))})")
        if folders:
            parts.append(f"{len(folders)} folder{'s' if len(folders) != 1 else ''} with everything inside")
        self.selection_label = QLabel(f"From “{volume_name}”: {names}\n{' and '.join(parts)}")
        self.selection_label.setWordWrap(True)
        self.selection_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.destination_edit = QLineEdit(self._setting(LAST_DESTINATION_KEY, ""))
        self.destination_edit.setObjectName("destination")
        self.destination_edit.setPlaceholderText("Choose a folder on this PC")
        self.destination_edit.textChanged.connect(self._update_start)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination_edit, 1)
        destination_row.addWidget(self.browse_button)

        self.policy_combo = QComboBox()
        self.policy_combo.setObjectName("conflictPolicy")
        for policy, label in POLICY_LABELS.items():
            self.policy_combo.addItem(label, policy)
        saved = self._setting(POLICY_KEY, ConflictPolicy.ASK.value)
        index = self.policy_combo.findData(next((p for p in ConflictPolicy if p.value == saved), ConflictPolicy.ASK))
        self.policy_combo.setCurrentIndex(max(0, index))

        form = QFormLayout()
        form.addRow("Save to:", destination_row)
        form.addRow("If a file already exists:", self.policy_combo)

        self.setup_error = banner_label("setupError")
        note = QLabel(SAFETY_NOTE)
        note.setWordWrap(True)
        note.setEnabled(False)

        self.start_button = QPushButton("Start Copy")
        self.start_button.setObjectName("startCopy")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self.start)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.start_button)
        buttons.addWidget(cancel)

        layout = QVBoxLayout(page)
        layout.addWidget(self.setup_heading)
        layout.addWidget(self.selection_label)
        layout.addSpacing(8)
        layout.addLayout(form)
        layout.addWidget(self.setup_error)
        layout.addWidget(note)
        layout.addStretch(1)
        layout.addLayout(buttons)
        self._update_start()
        return page

    def _build_progress(self) -> QWidget:
        page = QWidget()
        page.setObjectName("progressPage")
        self.progress_heading = self._heading("Preparing…")
        self.file_label = QLabel()
        self.file_label.setObjectName("currentFile")
        self.folder_label = QLabel()
        self.folder_label.setEnabled(False)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("copyProgress")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("%p%")
        self.bytes_label = QLabel()
        self.bytes_label.setObjectName("bytes")
        self.count_label = QLabel()
        self.count_label.setObjectName("count")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelCopy")
        self.cancel_button.clicked.connect(self.cancel)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(page)
        layout.addWidget(self.progress_heading)
        layout.addWidget(self.file_label)
        layout.addWidget(self.folder_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.bytes_label)
        layout.addWidget(self.count_label)
        layout.addStretch(1)
        layout.addLayout(buttons)
        return page

    def _build_result(self) -> QWidget:
        page = QWidget()
        page.setObjectName("resultPage")
        self.result_heading = self._heading()
        self.result_reason = banner_label("stopReason")
        self.result_summary = QLabel()
        self.result_summary.setObjectName("summary")
        self.result_summary.setWordWrap(True)
        self.problem_list = QTreeWidget()
        self.problem_list.setObjectName("problems")
        self.problem_list.setHeaderLabels(["Item", "Result", "Reason"])
        self.problem_list.setRootIsDecorated(False)
        self.problem_list.setAlternatingRowColors(True)
        header = self.problem_list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 220)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.open_button = QPushButton("Open Folder")
        self.open_button.clicked.connect(lambda: self._open_folder(self._report.destination_root if self._report else Path()))
        self.save_button = QPushButton("Save Report…")
        self.save_button.clicked.connect(self.save_report)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.save_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(page)
        layout.addWidget(self.result_heading)
        layout.addWidget(self.result_reason)
        layout.addWidget(self.result_summary)
        layout.addWidget(self.problem_list, 1)
        layout.addLayout(buttons)
        return page

    # --- state -------------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._report is None

    @property
    def report(self) -> CopyReport | None:
        return self._report

    @Slot()
    def _browse(self) -> None:
        chosen = self._choose_folder(self, self.destination_edit.text())
        if chosen:
            self.destination_edit.setText(str(Path(chosen)))

    @Slot()
    def _update_start(self) -> None:
        self.start_button.setEnabled(bool(self.destination_edit.text().strip()))

    @Slot()
    def start(self) -> None:
        if self._thread is not None:
            return  # one job per dialog
        text = self.destination_edit.text().strip()
        if not text:
            return
        policy: ConflictPolicy = self.policy_combo.currentData()
        try:
            job = CopyJob(
                self._backend,
                self._volume_index,
                self._sources,
                Path(text),
                policy=policy,
                resolver=self._resolve_conflict,
                progress=self._bridge.progress.emit,
            )
        except ApfsExplorerError as exc:
            self.setup_error.setText(f"⚠ {exc.user_message}")
            self.setup_error.show()
            return
        self._save_setting(LAST_DESTINATION_KEY, str(job.destination_root))
        self._save_setting(POLICY_KEY, policy.value)
        self._job = job
        self.pages.setCurrentIndex(1)
        self.setWindowTitle("Copying to this PC")
        self._thread = threading.Thread(target=self._run_job, name="copy-job", daemon=True)
        self._thread.start()
        self.copyStarted.emit()

    def _run_job(self) -> None:  # copy thread
        assert self._job is not None
        report = self._job.run()
        try:
            self._bridge.done.emit(report)
        except RuntimeError:  # dialog already destroyed
            logger.info("Copy finished after its dialog closed: %s", report.summary_line())

    @Slot()
    def cancel(self) -> None:
        if self._job is not None and self.is_running:
            self._job.cancel()
            self._answered.set()
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("Cancelling…")
            self.progress_heading.setText("Cancelling…")

    def cancel_and_wait(self, timeout: float = 10.0) -> bool:
        """Cancel and block until the copy thread ends (app exit). True if it ended."""
        self.cancel()
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    # --- conflicts ----------------------------------------------------------------------

    def _resolve_conflict(self, conflict: Conflict) -> ConflictDecision:  # copy thread
        assert self._job is not None
        self._answer = None
        self._answered.clear()
        self._bridge.conflict.emit(conflict)
        while not self._answered.wait(0.1):
            if self._job.is_cancelled:
                break
        return self._answer or ConflictDecision(ConflictChoice.SKIP)

    @Slot(object)
    def _on_conflict(self, conflict: Conflict) -> None:
        if self._job is None or self._job.is_cancelled:
            self._answered.set()
            return
        self.progress_heading.setText("Waiting for your answer…")
        self._answer = self._conflict_prompt(self, conflict)
        self.progress_heading.setText("Copying…")
        self._answered.set()

    # --- progress and result ---------------------------------------------------------------

    @Slot(object)
    def _on_progress(self, progress: CopyProgress) -> None:
        if self._report is not None or (self._job is not None and self._job.is_cancelled):
            return
        if progress.phase is Phase.SCANNING:
            self.progress_heading.setText("Preparing…")
            self.file_label.setText(self._elide(f"Looking in {progress.current}"))
            self.bytes_label.setText(f"Found {progress.files_total} files ({format_size(progress.bytes_total)}) so far")
            return
        if progress.phase is Phase.DONE:
            return
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(round(progress.fraction * 1000))
        if progress.current is not None:
            verb = "Checking" if progress.phase is Phase.VERIFYING else "Copying"
            self.progress_heading.setText("Copying…")
            self.file_label.setText(self._elide(f"{verb} “{progress.current.name}”"))
            self.folder_label.setText(self._elide(f"from {progress.current.parent}"))
        details = [f"{format_size(progress.bytes_done)} of {format_size(progress.bytes_total)}"]
        if progress.speed > 0:
            details.append(f"{format_size(round(progress.speed))}/s")
        if (left := progress.seconds_left) is not None:
            details.append(f"{format_duration(left)} left")
        self.bytes_label.setText("   •   ".join(details))
        number = min(progress.files_done + 1, progress.files_total)
        self.count_label.setText(f"File {number} of {progress.files_total}")

    def _elide(self, text: str) -> str:
        return QFontMetrics(self.file_label.font()).elidedText(text, Qt.TextElideMode.ElideMiddle, 520)

    @Slot(object)
    def _on_done(self, report: CopyReport) -> None:
        self._report = report
        self.setWindowTitle("Copy to this PC")
        headings = {
            JobStatus.COMPLETED: "Copy complete" if not report.failed else "Copy finished with problems",
            JobStatus.CANCELLED: "Copy cancelled",
            JobStatus.STOPPED: "Copy stopped",
        }
        self.result_heading.setText(headings[report.status])
        self.result_reason.setText(f"⚠ {report.stop_reason}" if report.stop_reason else "")
        self.result_reason.setVisible(bool(report.stop_reason))
        lines = [
            f"✔  {len(report.copied)} file{'s' if len(report.copied) != 1 else ''} copied and verified "
            f"({format_size(report.bytes_copied)})"
        ]
        if report.failed:
            lines.append(f"✖  {len(report.failed)} failed (see the list below)")
        if report.skipped:
            lines.append(f"–  {len(report.skipped)} skipped (see the list below)")
        lines.append(f"Saved in: {report.destination_root}")
        self.result_summary.setText("\n".join(lines))
        self.problem_list.clear()
        for result in report.problems:
            item = QTreeWidgetItem([str(result.source), result.outcome.value, result.message])
            item.setToolTip(0, str(result.source))
            item.setToolTip(2, result.message)
            self.problem_list.addTopLevelItem(item)
        renamed = [r for r in report.results if r.outcome is Outcome.COPIED and r.message]
        for result in renamed:
            self.problem_list.addTopLevelItem(QTreeWidgetItem([str(result.source), "Note", result.message]))
        self.problem_list.setVisible(self.problem_list.topLevelItemCount() > 0)
        self.pages.setCurrentIndex(2)
        self.copyFinished.emit(report)

    @Slot()
    def save_report(self) -> None:
        if self._report is None:
            return
        suggested = str(self._report.destination_root / "APFS copy report.txt")
        target = self._choose_report_file(self, suggested)
        if not target:
            return
        try:
            # backslashreplace: a name with an invalid character must not stop the report being saved.
            Path(target).write_text(self._report.as_text(), encoding="utf-8-sig", errors="backslashreplace")
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(self, "Report not saved", f"The report couldn't be saved:\n{exc}")

    # --- closing -------------------------------------------------------------------------

    def reject(self) -> None:
        if self.is_running:
            self.cancel()  # Esc / window close while copying means "cancel"
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self.is_running:
            self.cancel()
            event.ignore()
            return
        super().closeEvent(event)

    def _setting(self, key: str, default: str) -> str:
        return default if self._settings is None else str(self._settings.value(key, default))

    def _save_setting(self, key: str, value: str) -> None:
        if self._settings is not None:
            self._settings.setValue(key, value)
