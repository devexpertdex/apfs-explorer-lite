from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apfs_explorer_lite.errors import (
    DestinationSafetyError,
    SourceWriteBlockedError,
)
from apfs_explorer_lite.safety import (
    extended_path,
    numbered_name,
    require_read_only_mode,
    sanitize_component,
    validate_destination_root,
)


@pytest.mark.parametrize("mode", ["r", "rb"])
def test_read_modes_allowed(mode: str) -> None:
    require_read_only_mode(mode)


@pytest.mark.parametrize("mode", ["w", "wb", "r+", "rb+", "a", "ab", "x", "xb", "", "b"])
def test_write_modes_blocked(mode: str) -> None:
    with pytest.raises(SourceWriteBlockedError):
        require_read_only_mode(mode)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("a:b", "a_b"),
        ('q<>"|?*', "q______"),
        ("back\\slash", "back_slash"),
        ("trailing. ", "trailing"),
        ("..", "_"),
        (".", "_"),
        ("", "_"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("COM1", "_COM1"),
        ("CONSOLE", "CONSOLE"),
        ("tab\there", "tab_here"),
        ("日本語.txt", "日本語.txt"),
    ],
)
def test_sanitize_component(name: str, expected: str) -> None:
    assert sanitize_component(name) == expected


def test_validate_destination_root_accepts_existing_folder(tmp_path: Path) -> None:
    assert validate_destination_root(tmp_path) == tmp_path.resolve()


def test_validate_destination_root_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(DestinationSafetyError):
        validate_destination_root(tmp_path / "missing")


def test_validate_destination_root_rejects_file(tmp_path: Path) -> None:
    file = tmp_path / "f.txt"
    file.write_text("x")
    with pytest.raises(DestinationSafetyError):
        validate_destination_root(file)


def test_validate_destination_root_rejects_relative() -> None:
    with pytest.raises(DestinationSafetyError):
        validate_destination_root(Path("relative"))


@pytest.mark.parametrize(
    "device",
    [r"\\.\PhysicalDrive2", r"\\.\C:", r"\\?\GLOBALROOT\Device\Harddisk2\Partition2", "//./PhysicalDrive0"],
)
def test_validate_destination_root_rejects_device_paths(device: str) -> None:
    with pytest.raises(DestinationSafetyError):
        validate_destination_root(Path(device))


@pytest.mark.parametrize(
    ("name", "expected"),
    [("CONIN$", "_CONIN$"), ("com¹.txt", "_com¹.txt"), ("LPT9", "_LPT9"), ("CON .txt", "_CON .txt")],
)
def test_sanitize_more_reserved_names(name: str, expected: str) -> None:
    assert sanitize_component(name) == expected


def test_sanitize_shortens_long_names_keeping_extension() -> None:
    result = sanitize_component("a" * 400 + ".docx")
    assert len(result) == 255 and result.endswith(".docx")
    # Astral characters count as two UTF-16 units each.
    emoji = sanitize_component("😀" * 200 + ".txt")
    assert len(emoji.encode("utf-16-le")) // 2 <= 255 and emoji.endswith(".txt")
    assert sanitize_component(result) == result


@pytest.mark.parametrize(
    ("name", "folder", "expected"),
    [("report.docx", False, "report (2).docx"), ("My.Folder", True, "My.Folder (2)"),
     ("noext", False, "noext (2)"), (".hidden", False, ".hidden (2)")],
)  # fmt: skip
def test_numbered_name(name: str, folder: bool, expected: str) -> None:
    assert numbered_name(name, 2, is_folder=folder) == expected


def test_numbered_name_stays_within_limit() -> None:
    result = numbered_name("x" * 255, 12)
    assert len(result) == 255 and result.endswith(" (12)")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
def test_extended_path() -> None:
    assert extended_path(Path(r"C:\Users\me")) == r"\\?\C:\Users\me"
    assert extended_path(Path(r"\\server\share\dir")) == r"\\?\UNC\server\share\dir"
    assert extended_path(Path(r"\\?\C:\x")) == r"\\?\C:\x"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows folders")
def test_system_folders_rejected_as_destination() -> None:
    import os

    for name in ("SystemRoot", "ProgramFiles"):
        with pytest.raises(DestinationSafetyError, match="system folders"):
            validate_destination_root(Path(os.environ[name]))
