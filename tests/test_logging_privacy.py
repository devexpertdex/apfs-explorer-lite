"""The log is structured (JSON lines) and keeps file names and contents private."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

from apfs_explorer_lite.copying import Destination, JobStatus
from apfs_explorer_lite.errors import ApfsPathNotFoundError
from apfs_explorer_lite.logging_config import configure_logging
from apfs_explorer_lite.structured_log import LOG_NAMES_ENV, path_fields, scrub_text

from .test_copy_engine import memory_backend, run

SECRET_NAME = "Medical results Jane Doe 2024.pdf"
SECRET_FOLDER = "Private Tax Returns"
SECRET_CONTENT = b"TOP-SECRET-CONTENT-7f3a"


@pytest.fixture
def log_file(tmp_path: Path) -> Iterator[Path]:
    root = logging.getLogger()
    old_level = root.level
    path = configure_logging(tmp_path / "logs", level=logging.DEBUG)
    yield path
    for handler in [h for h in root.handlers if getattr(h, "_apfs_explorer_lite_handler", False)]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(old_level)


def records(path: Path) -> list[dict]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_copy_log_has_no_names_or_contents(log_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = memory_backend({
        f"/{SECRET_FOLDER}/{SECRET_NAME}": SECRET_CONTENT,
        f"/{SECRET_FOLDER}/fails - Jane Doe.docx": SECRET_CONTENT,
    })  # fmt: skip
    real_place = Destination.place

    def place(self, temp, final, *, replace):  # noqa: ANN001, ANN202
        if final.name.startswith("fails"):
            raise PermissionError(13, "Access is denied", str(final))
        return real_place(self, temp, final, replace=replace)

    monkeypatch.setattr(Destination, "place", place)
    dest = tmp_path / "Recovered for Jane"
    dest.mkdir()
    report = run(backend, backend.list_directory(0, PurePosixPath("/")), dest)
    assert report.status is JobStatus.COMPLETED and len(report.copied) == 1 and len(report.failed) == 1

    text = log_file.read_text(encoding="utf-8")
    for secret in ("Jane", "Medical", "Private Tax", "TOP-SECRET", SECRET_CONTENT.hex()):
        assert secret not in text, secret
    events = {r["event"]: r for r in records(log_file)}
    failed = events["copy.file_failed"]
    assert failed["path_id"].startswith("p-") and failed["path_ext"] == ".docx"
    assert failed["error"] == "PermissionError" and failed["errno"] == 13
    copied = events["copy.file_copied"]
    assert copied["path_ext"] == ".pdf" and copied["size"] == len(SECRET_CONTENT)
    assert events["copy.finished"]["copied"] == 1


def test_every_line_is_json_with_standard_fields(log_file: Path) -> None:
    logging.getLogger("apfs_explorer_lite.test").info("plain message %d", 5)
    [record] = [r for r in records(log_file) if r["logger"] == "apfs_explorer_lite.test"]
    assert record["event"] == "plain message 5" and record["level"] == "INFO" and "ts" in record


def test_exceptions_keep_frames_but_not_paths(log_file: Path) -> None:
    try:
        open(Path("C:/Users/jane/Documents") / SECRET_NAME, "rb")  # noqa: SIM115
    except OSError:
        logging.getLogger("apfs_explorer_lite.test").exception("open failed")
    [record] = [r for r in records(log_file) if r["event"] == "open failed"]
    assert record["exception"]["type"] == "FileNotFoundError"
    assert any("test_logging_privacy.py" in frame for frame in record["exception"]["frames"])
    assert "Jane" not in json.dumps(record) and "jane" not in json.dumps(record)


def test_explorer_errors_do_not_log_folder_names(qapp: object, log_file: Path) -> None:
    from .test_explorer import ControlledBackend, dmg_source, settle, wait_until
    from .real_images import image_path

    from apfs_explorer_lite.gui.explorer import ExplorerWidget

    backend = ControlledBackend(image_path("apfs.dmg"))
    explorer = ExplorerWidget(lambda: backend)
    explorer.open_container(dmg_source(image_path("apfs.dmg")), "Disk 9")
    wait_until(lambda: explorer.showing_list)
    settle(explorer)
    backend.fail_listing[None] = ApfsPathNotFoundError(f"/{SECRET_FOLDER} was not found on the volume.")
    explorer.navigate(0, PurePosixPath(f"/{SECRET_FOLDER}"))
    settle(explorer)
    explorer.shutdown()
    text = log_file.read_text(encoding="utf-8")
    assert "explorer.load_failed" in text
    assert "Private" not in text


def test_names_can_be_enabled_for_troubleshooting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert path_fields(f"/{SECRET_NAME}")["path_id"].startswith("p-")
    monkeypatch.setenv(LOG_NAMES_ENV, "1")
    assert path_fields(f"/{SECRET_NAME}")["path_id"] == f"/{SECRET_NAME}"
    assert scrub_text(f"/{SECRET_NAME} missing") == f"/{SECRET_NAME} missing"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Copy of /Personal/tax 2024.pdf failed: boom", "tax"),
        (r"[Errno 13] Permission denied: '\\?\C:\Users\jane\R\~a.apfs-partial'", "jane"),
        (r"C:\Users\jane\Desktop\x y.docx is gone", "jane"),
        (r"\\server\share\jane notes.txt", "jane"),
        ("Volume 'Jane MacBook' is encrypted", "Jane"),
        ("/Projects/Q4 (draft)/a.xlsx was not found on the volume.", "draft"),
    ],
)
def test_scrub_text_removes_paths_and_quoted_names(text: str, secret: str) -> None:
    assert secret not in scrub_text(text)


@pytest.mark.parametrize(
    "text",
    [r"opening \\.\PhysicalDrive1", "read/write 3/4 done, 1.2 MB/s", "unable to read data block at offset: 499712 (0x7a000)."],
)
def test_scrub_text_keeps_technical_detail(text: str) -> None:
    assert scrub_text(text) == text


def test_path_ids_are_stable_within_a_session_only() -> None:
    a, b = path_fields("/x/report.pdf"), path_fields("/x/report.pdf")
    assert a == b and a["path_depth"] == 2
    assert path_fields("/x/other.pdf")["path_id"] != a["path_id"]
