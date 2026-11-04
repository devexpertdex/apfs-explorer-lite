"""Copy dialog tests: the real explorer and backend on a macOS-created APFS image.

Only the Windows folder picker, the conflict question box and "Open Folder"
are replaced (they would block an automated run).
"""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path, PurePosixPath

import pytest

from apfs_explorer_lite.backends.libfsapfs_backend import LibfsapfsBackend
from apfs_explorer_lite.copying import PARTIAL_SUFFIX, Conflict, ConflictChoice, ConflictDecision, JobStatus
from apfs_explorer_lite.errors import DiskDisconnectedError
from apfs_explorer_lite.models import ApfsEntry, EntryKind, SourceDevice

from .doubles import InMemoryBackend
from .real_images import A_FILE, ANOTHER_FILE, PASSWORDS_TXT, image_path
from .test_copy_engine import FailingReader
from .test_explorer import dmg_source, settle, wait_until


class Recorder:
    def __init__(self, folder: Path | None = None, decision: ConflictChoice = ConflictChoice.SKIP) -> None:
        self.folder = folder
        self.decision = decision
        self.conflicts: list[Conflict] = []
        self.conflict_threads: list[bool] = []
        self.opened: list[Path] = []
        self.report_file: Path | None = None

    def choose_folder(self, _parent: object, _start: str) -> str:
        return str(self.folder) if self.folder else ""

    def choose_report_file(self, _parent: object, suggested: str) -> str:
        self.suggested_report = suggested
        return str(self.report_file) if self.report_file else ""

    def prompt(self, _parent: object, conflict: Conflict) -> ConflictDecision:
        self.conflicts.append(conflict)
        self.conflict_threads.append(threading.current_thread() is threading.main_thread())
        return ConflictDecision(self.decision)

    def factory(self):  # noqa: ANN201
        from apfs_explorer_lite.gui.copy_dialog import CopyDialog

        return partial(
            CopyDialog,
            choose_folder=self.choose_folder,
            choose_report_file=self.choose_report_file,
            conflict_prompt=self.prompt,
            open_folder=self.opened.append,
        )


@pytest.fixture
def setup(qapp: object, tmp_path: Path) -> Iterator[tuple[object, FailingReader, Recorder, Path]]:
    from PySide6.QtCore import QSettings

    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    path = image_path("apfs.dmg")
    reader = FailingReader(path)
    backend = LibfsapfsBackend(lambda _s: reader, chunk_size=4096, max_cached_chunks=1)
    dest = tmp_path / "Recovered"
    dest.mkdir()
    recorder = Recorder(dest)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    explorer = ExplorerWidget(lambda: backend, settings=settings, copy_dialog_factory=recorder.factory())
    explorer.open_container(dmg_source(path), "Disk 9: Test image")
    wait_until(lambda: explorer.showing_list)
    settle(explorer)
    yield explorer, reader, recorder, dest
    explorer.shutdown()


def select(explorer, *names: str) -> None:  # noqa: ANN001
    from PySide6.QtCore import QItemSelectionModel

    selection = explorer.file_view.selectionModel()
    selection.clearSelection()
    proxy = explorer.proxy
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    for row in range(proxy.rowCount()):
        if proxy.index(row, 0).data() in names:
            selection.select(proxy.index(row, 0), flags)


def start_copy(explorer, *names: str):  # noqa: ANN001, ANN201
    select(explorer, *names)
    assert explorer.copy_action.isEnabled()
    explorer.copy_action.trigger()
    dialog = explorer.copy_dialog
    assert dialog is not None and dialog.isVisible()
    dialog.browse_button.click()
    dialog.start_button.click()
    return dialog


def finish(dialog) -> None:  # noqa: ANN001
    wait_until(lambda: dialog.report is not None)


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_copy_button_needs_a_selection(setup) -> None:  # noqa: ANN001
    explorer, *_ = setup
    assert not explorer.copy_action.isEnabled()
    assert "Select files" in explorer.copy_action.toolTip()
    select(explorer, "passwords.txt")
    assert explorer.copy_action.isEnabled() and explorer.context_copy_action.isEnabled()


def test_setup_page_describes_selection(setup) -> None:  # noqa: ANN001
    explorer, _, recorder, dest = setup
    select(explorer, "a_directory", "passwords.txt")
    explorer.copy_action.trigger()
    dialog = explorer.copy_dialog
    assert dialog.setup_heading.text() == "Copy 2 items to this PC"
    assert "“a_directory”, “passwords.txt”" in dialog.selection_label.text()
    assert "1 file (116 bytes)" in dialog.selection_label.text()
    assert not dialog.start_button.isEnabled()  # no destination yet
    dialog.browse_button.click()
    assert dialog.destination_edit.text() == str(dest)
    assert dialog.start_button.isEnabled()
    dialog.reject()
    wait_until(lambda: explorer.copy_dialog is None)


def test_copy_files_and_folder_end_to_end(setup) -> None:  # noqa: ANN001
    explorer, _, recorder, dest = setup
    dialog = start_copy(explorer, "a_directory", "passwords.txt")
    finish(dialog)
    assert dialog.report.status is JobStatus.COMPLETED
    assert tree(dest) == {
        "a_directory/a_file": A_FILE, "a_directory/another_file": ANOTHER_FILE, "passwords.txt": PASSWORDS_TXT,
    }  # fmt: skip
    assert dialog.pages.currentIndex() == 2
    assert dialog.result_heading.text() == "Copy complete"
    assert "3 files copied and verified (191 bytes)" in dialog.result_summary.text()
    assert dialog.problem_list.isHidden()
    dialog.open_button.click()
    assert recorder.opened == [dest.resolve()]


def test_one_copy_at_a_time(setup) -> None:  # noqa: ANN001
    explorer, *_ = setup
    dialog = start_copy(explorer, "passwords.txt")
    # While the dialog is open, the toolbar cannot start a second job.
    assert not explorer.copy_action.isEnabled()
    explorer.copy_selected()
    assert explorer.copy_dialog is dialog
    dialog.start()  # a second start on the same dialog is ignored
    finish(dialog)
    dialog.accept()
    wait_until(lambda: explorer.copy_dialog is None)
    select(explorer, "passwords.txt")
    assert explorer.copy_action.isEnabled()


def test_conflict_question_is_asked_on_gui_thread(setup) -> None:  # noqa: ANN001
    explorer, _, recorder, dest = setup
    (dest / "passwords.txt").write_bytes(b"older copy")
    recorder.decision = ConflictChoice.REPLACE
    dialog = start_copy(explorer, "passwords.txt")
    finish(dialog)
    assert [c.destination.name for c in recorder.conflicts] == ["passwords.txt"]
    assert recorder.conflict_threads == [True]
    assert (dest / "passwords.txt").read_bytes() == PASSWORDS_TXT
    assert "Replaced the existing file." in [
        dialog.problem_list.topLevelItem(i).text(2) for i in range(dialog.problem_list.topLevelItemCount())
    ]


def test_skip_policy_from_combo(setup) -> None:  # noqa: ANN001
    from apfs_explorer_lite.copying import ConflictPolicy

    explorer, _, recorder, dest = setup
    (dest / "passwords.txt").write_bytes(b"keep me")
    select(explorer, "passwords.txt")
    explorer.copy_action.trigger()
    dialog = explorer.copy_dialog
    dialog.policy_combo.setCurrentIndex(dialog.policy_combo.findData(ConflictPolicy.SKIP))
    dialog.browse_button.click()
    dialog.start_button.click()
    finish(dialog)
    assert recorder.conflicts == []
    assert (dest / "passwords.txt").read_bytes() == b"keep me"
    assert "1 skipped" in dialog.result_summary.text()
    assert dialog.problem_list.topLevelItem(0).text(1) == "Skipped"


def test_invalid_destination_stays_on_setup(setup, tmp_path: Path) -> None:  # noqa: ANN001
    explorer, *_ = setup
    select(explorer, "passwords.txt")
    explorer.copy_action.trigger()
    dialog = explorer.copy_dialog
    dialog.destination_edit.setText(str(tmp_path / "does not exist"))
    dialog.start_button.click()
    assert dialog.pages.currentIndex() == 0
    assert dialog.setup_error.isVisible() and "does not exist" in dialog.setup_error.text()
    assert not dialog.is_running


def test_drive_unplugged_during_copy(setup) -> None:  # noqa: ANN001
    explorer, reader, _, dest = setup
    reader.fail_with = DiskDisconnectedError("The disk is no longer available.")
    dialog = start_copy(explorer, "passwords.txt")
    finish(dialog)
    assert dialog.result_heading.text() == "Copy stopped"
    assert dialog.result_reason.isVisible() and "disconnected" in dialog.result_reason.text()
    assert not any(p.name.endswith(PARTIAL_SUFFIX) for p in dest.iterdir())
    assert tree(dest) == {}


def test_save_report(setup, tmp_path: Path) -> None:  # noqa: ANN001
    explorer, _, recorder, dest = setup
    recorder.report_file = tmp_path / "report.txt"
    dialog = start_copy(explorer, "passwords.txt")
    finish(dialog)
    dialog.save_button.click()
    assert recorder.suggested_report == str(dest.resolve() / "APFS copy report.txt")
    text = recorder.report_file.read_text(encoding="utf-8-sig")
    assert "02a2a6af2f1ecf4720d7d49d640f0d0a269a7ec733e41973bdd34f09dad0e252" in text


def test_destination_and_policy_are_remembered(setup) -> None:  # noqa: ANN001
    explorer, _, recorder, dest = setup
    dialog = start_copy(explorer, "passwords.txt")
    finish(dialog)
    dialog.accept()
    wait_until(lambda: explorer.copy_dialog is None)
    select(explorer, "passwords.txt")
    explorer.copy_action.trigger()
    assert explorer.copy_dialog.destination_edit.text() == str(dest.resolve())
    explorer.copy_dialog.reject()


# --- cancelling a slow copy ------------------------------------------------------------------------


class SlowStream(io.RawIOBase):
    def __init__(self, size: int) -> None:
        self.size = size
        self.pos = 0

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        time.sleep(0.01)
        n = min(n, self.size - self.pos)
        self.pos += n
        return b"z" * n


class SlowBackend(InMemoryBackend):
    SIZE = 64 * 1024 * 1024

    def stat(self, volume_index: int, path: PurePosixPath) -> ApfsEntry:
        entry = super().stat(volume_index, path)
        return ApfsEntry(path, EntryKind.FILE, self.SIZE) if path.name == "slow.bin" else entry

    def open_file(self, volume_index: int, path: PurePosixPath):  # noqa: ANN201
        return SlowStream(self.SIZE)


@pytest.fixture
def slow(qapp: object, tmp_path: Path) -> Iterator[tuple[object, Path]]:
    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    backend = SlowBackend({"/slow.bin": b""})
    dest = tmp_path / "dest"
    dest.mkdir()
    explorer = ExplorerWidget(lambda: backend, copy_dialog_factory=Recorder(dest).factory())
    explorer.open_container(SourceDevice("memory", 0, 1), "Slow")
    wait_until(lambda: explorer.showing_list)
    settle(explorer)
    yield explorer, dest
    explorer.shutdown()


def test_progress_and_cancel(slow) -> None:  # noqa: ANN001
    explorer, dest = slow
    dialog = start_copy(explorer, "slow.bin")
    wait_until(lambda: dialog.progress_bar.maximum() == 1000 and dialog.progress_bar.value() > 0)
    assert dialog.is_running
    assert "slow.bin" in dialog.file_label.text()
    assert " of 67.1 MB" in dialog.bytes_label.text()
    assert dialog.count_label.text() == "File 1 of 1"
    assert not explorer.drives_action.isEnabled()  # the drive can't be closed mid-copy
    wait_until(lambda: "/s" in dialog.bytes_label.text())  # transfer speed appears
    dialog.reject()  # Esc / closing the window cancels instead of closing
    assert dialog.isVisible()
    finish(dialog)
    assert dialog.report.status is JobStatus.CANCELLED
    assert dialog.result_heading.text() == "Copy cancelled"
    assert list(dest.iterdir()) == []  # the partial file was removed
    assert explorer.drives_action.isEnabled()


def test_shutdown_cancels_running_copy(slow) -> None:  # noqa: ANN001
    explorer, dest = slow
    dialog = start_copy(explorer, "slow.bin")
    wait_until(lambda: dialog.progress_bar.value() > 0)
    assert explorer.copy_running
    explorer.shutdown()
    assert not dialog._thread.is_alive()
    assert list(dest.iterdir()) == []


def test_main_window_asks_before_exit_during_copy(qapp: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apfs_explorer_lite.backends.base import BackendAvailability
    from apfs_explorer_lite.disks.discovery import DiscoveryResult
    from apfs_explorer_lite.gui.main_window import MainWindow

    dest = tmp_path / "dest"
    dest.mkdir()
    backend = SlowBackend({"/slow.bin": b""})
    window = MainWindow(
        BackendAvailability("libfsapfs", True, "test", "ok"),
        lambda: DiscoveryResult((), True),
        is_admin=True,
        backend_factory=lambda: backend,
    )
    window.explorer._copy_dialog_factory = Recorder(dest).factory()
    window.explorer.open_container(SourceDevice("memory", 0, 1), "Slow")
    wait_until(lambda: window.explorer.showing_list)
    settle(window.explorer)
    dialog = start_copy(window.explorer, "slow.bin")
    wait_until(lambda: dialog.progress_bar.value() > 0)
    answers = iter([False, True])
    monkeypatch.setattr(window, "confirm_exit_during_copy", lambda: next(answers))
    assert not window.close()  # "No": keep running
    assert dialog.is_running
    assert window.close()  # "Yes": cancel and exit
    assert not dialog._thread.is_alive()
    assert list(dest.iterdir()) == []
