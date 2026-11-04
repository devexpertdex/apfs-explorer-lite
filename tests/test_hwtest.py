"""The hardware test tool, run against real images (dry run) and mocked discovery.

These prove the tool works; they are not hardware evidence. Only a
``source_kind: "physical"`` results file counts as a physical-SSD result.
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from apfs_explorer_lite import hwtest

from .real_images import DMG_PARTITION_OFFSET, image_path
from .test_discovery import apfs_usb_disk, internal_system_disk
from .win_structs import FakeDiskApi


def run_tool(*args: str) -> tuple[int, str, dict]:
    out = io.StringIO()
    code = hwtest.main(list(args), out=out)
    results_path = Path(args[args.index("--out") + 1])
    return code, out.getvalue(), json.loads(results_path.read_text(encoding="utf-8"))


def test_dry_run_on_real_image(tmp_path: Path) -> None:
    code, output, data = run_tool("--image", str(image_path("apfs.dmg")), "--out", str(tmp_path / "r.json"))
    assert code == 0, output
    assert data["source_kind"] == "image"
    statuses = {c["name"]: c["status"] for c in data["checks"]}
    assert statuses["Open the APFS container read-only"] == "passed"
    assert statuses["Walk the volume's folders"] == "passed"
    assert statuses["Copy 6 small files"] == "passed"
    assert statuses["Cancel a copy part-way"] == "passed"
    assert statuses["Handles released after closing"] == "passed"
    assert statuses["Copy a large file"] == "not_tested"  # honest: the image has no large file


def test_dry_run_on_damaged_first_block_image(tmp_path: Path) -> None:
    damaged = tmp_path / "damaged.dmg"
    shutil.copyfile(image_path("apfs.dmg"), damaged)
    with damaged.open("r+b") as f:
        f.seek(DMG_PARTITION_OFFSET)
        f.write(bytes.fromhex("eb7690") + b"EXFAT   " + bytes(4085))
    code, output, data = run_tool("--image", str(damaged), "--out", str(tmp_path / "r.json"))
    assert code == 0, output
    opened = next(c for c in data["checks"] if c["name"].startswith("Open"))
    assert opened["metrics"]["checkpoint_superblock"] is True


def test_results_contain_no_file_names(tmp_path: Path) -> None:
    _, output, data = run_tool("--image", str(image_path("apfs.dmg")), "--out", str(tmp_path / "r.json"))
    text = json.dumps(data) + output
    for name in ("passwords", "a_directory", "a_file", "another_file", "fseventsd", "SingleVolume"):
        assert name not in text, name


def test_temporary_copies_are_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    monkeypatch.setattr(hwtest.tempfile, "mkdtemp", lambda prefix: (work.mkdir(), str(work))[1])
    run_tool("--image", str(image_path("apfs.dmg")), "--out", str(tmp_path / "r.json"))
    assert not work.exists()


def test_physical_mode_detection_with_mocked_disks(monkeypatch: pytest.MonkeyPatch) -> None:
    from apfs_explorer_lite.disks import discovery, win32

    api = FakeDiskApi({0: internal_system_disk(), 2: apfs_usb_disk()})
    monkeypatch.setattr(win32, "Win32DiskApi", lambda: api)
    monkeypatch.setattr(discovery.WindowsDiskDiscovery, "__init__", lambda self, _api=None: setattr(self, "_api", api))
    session = hwtest.Session(io.StringIO())
    found = hwtest._find_disk_source(None, session.out, session)
    assert found is not None
    source, disk = found
    assert disk == 2 and source.device_path == r"\\.\PhysicalDrive2"
    assert [(c.reqs, c.status) for c in session.checks] == [([1], "passed"), ([2], "passed")]


def test_physical_mode_reports_missing_ssd(monkeypatch: pytest.MonkeyPatch) -> None:
    from apfs_explorer_lite.disks import discovery

    api = FakeDiskApi({0: internal_system_disk()})
    monkeypatch.setattr(discovery.WindowsDiskDiscovery, "__init__", lambda self, _api=None: setattr(self, "_api", api))
    session = hwtest.Session(io.StringIO())
    assert hwtest._find_disk_source(None, session.out, session) is None
    assert session.checks[0].status == "failed"
