"""Packaging pieces that can be checked without building (the build itself is checked by
scripts/check_package.py, which build_release.py runs on every build)."""

from __future__ import annotations

import io
import re
import struct
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from apfs_explorer_lite import __version__, selftest, tools
from apfs_explorer_lite.resources import resource_path

from .real_images import image_path

ROOT = Path(__file__).resolve().parents[1]


def test_tools_usage_version_and_unknown_command() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        assert tools.main([]) == 0
        assert tools.main(["version"]) == 0
    assert "selftest" in out.getvalue() and "hwtest" in out.getvalue()
    assert f"APFS Explorer Lite {__version__}" in out.getvalue()
    assert tools.main(["format-disk"]) == 2


def test_tools_entry_uses_absolute_imports() -> None:
    """PyInstaller runs tools.py as a top-level script, where relative imports fail."""
    source = (ROOT / "src" / "apfs_explorer_lite" / "tools.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*from \.", source, re.MULTILINE)
    main = (ROOT / "src" / "apfs_explorer_lite" / "__main__.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*from \.", main, re.MULTILINE)


def test_selftest_from_source(qapp: object, tmp_path: Path) -> None:
    out = io.StringIO()
    code = selftest.main(["--image", str(image_path("apfs.dmg")), "--out", str(tmp_path / "r.json")], out=out)
    text = out.getvalue()
    assert code == 0, text
    assert "[PASS] APFS reader (libfsapfs) loads" in text
    assert "[PASS] Qt GUI starts" in text
    assert "running from source" in text  # the self-contained check only applies to a build
    assert "passwords" not in (tmp_path / "r.json").read_text(encoding="utf-8")


def test_icon_has_all_sizes() -> None:
    data = resource_path("app.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    sizes = sorted(struct.unpack_from("<B", data, 6 + 16 * i)[0] or 256 for i in range(count))
    assert (reserved, kind) == (0, 1)
    assert sizes == [16, 20, 24, 32, 40, 48, 64, 128, 256]
    assert resource_path("app.png").read_bytes().startswith(b"\x89PNG")


def test_spec_and_installer_reference_existing_files() -> None:
    spec = (ROOT / "packaging" / "apfs_explorer_lite.spec").read_text(encoding="utf-8")
    assert '"__main__.py"' in spec and '"tools.py"' in spec and "console=False" in spec
    assert "COLLECT(" in spec  # one-folder: LGPL libraries stay replaceable files
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in iss
    icon = re.search(r"SetupIconFile=(.+)", iss).group(1).strip()  # type: ignore[union-attr]
    assert (ROOT / "packaging" / icon).resolve().exists()


@pytest.mark.parametrize("bat", ["Run hardware test.bat", "Diagnose drive.bat"])
def test_portable_launchers_use_the_bundled_tools(bat: str) -> None:
    text = (ROOT / "packaging" / "portable" / bat).read_text(encoding="utf-8")
    assert '"%~dp0APFS Explorer Lite Tools.exe"' in text
    assert "python" not in text.lower()  # the packaged app needs no Python
    assert "$env:SELF" in text  # self-elevation that survives apostrophes in paths


def test_notice_placeholders_are_known() -> None:
    text = (ROOT / "packaging" / "portable" / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    text += (ROOT / "packaging" / "portable" / "README.txt").read_text(encoding="utf-8")
    known = {"VERSION", "LIBFSAPFS_VERSION", "PYSIDE_VERSION", "PYINSTALLER_VERSION", "PYTHON_VERSION", "PYFSAPFS_FILE"}
    assert set(re.findall(r"\{([A-Z_]+)\}", text)) <= known


def test_build_script_refuses_to_delete_outside_the_project(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import build_release
    finally:
        sys.path.pop(0)
    with pytest.raises(SystemExit, match="Refusing"):
        build_release.safe_rmtree(tmp_path)
    with pytest.raises(SystemExit, match="Refusing"):
        build_release.safe_rmtree(ROOT / "src")
