# PyInstaller spec for APFS Explorer Lite. Use scripts\build_release.py (or build.bat),
# which checks the pinned versions first; or directly from the repository root:
#     python -m PyInstaller packaging\apfs_explorer_lite.spec --noconfirm --clean
#
# One-folder (COLLECT) mode keeps the LGPL libraries (Qt / PySide6 and libfsapfs's
# pyfsapfs.pyd) as separate, replaceable files, as the LGPL requires.
# Two programs share the folder:
#   APFS Explorer Lite.exe        the windowed app
#   APFS Explorer Lite Tools.exe  console tools: selftest, hwtest, diagnose, verify

import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
PKG = SRC / "apfs_explorer_lite"
APP_NAME = "APFS Explorer Lite"
TOOLS_NAME = "APFS Explorer Lite Tools"
VERSION = re.search(r'__version__ = "([^"]+)"', (PKG / "__init__.py").read_text(encoding="utf-8")).group(1)
ICON = str(PKG / "resources" / "app.ico")
DATAS = [(str(PKG / "resources" / "app.ico"), "apfs_explorer_lite/resources"),
         (str(PKG / "resources" / "app.png"), "apfs_explorer_lite/resources")]
# Not used by the app; keeps the build smaller.
EXCLUDES = ["tkinter", "pytest", "_pytest", "unittest", "pydoc_data", "lib2to3", "PySide6.QtNetwork",
            "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL", "PySide6.QtSql", "PySide6.QtTest"]


def version_info(description: str, internal_name: str) -> VSVersionInfo:
    numbers = tuple(int(n) for n in (VERSION.split(".") + ["0"] * 4)[:4])
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F, flags=0x0, OS=0x40004,
                          fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "APFS Explorer Lite project"),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", internal_name),
                StringStruct("LegalCopyright", "Uses Qt for Python (LGPL-3.0) and libfsapfs (LGPL-3.0-or-later)"),
                StringStruct("OriginalFilename", f"{internal_name}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])]),
        ],
    )


def analysis(script: str) -> Analysis:
    return Analysis(
        [str(PKG / script)],
        pathex=[str(SRC)],
        hiddenimports=["pyfsapfs"],
        datas=DATAS,
        excludes=EXCLUDES,
        noarchive=False,
    )


def without_unused(entries):
    """Drop Qt's software OpenGL renderer (widgets don't use OpenGL) and Qt translations (never loaded)."""
    def unused(dest: str) -> bool:
        dest = dest.replace("\\", "/").lower()
        return dest.endswith("/opengl32sw.dll") or "/translations/" in dest
    return [entry for entry in entries if not unused(entry[0])]


app_a = analysis("__main__.py")
tools_a = analysis("tools.py")
for a in (app_a, tools_a):
    a.binaries = without_unused(a.binaries)
    a.datas = without_unused(a.datas)

app_exe = EXE(
    PYZ(app_a.pure),
    app_a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    icon=ICON,
    version=version_info("APFS Explorer Lite - read-only APFS browser and copier", APP_NAME),
    console=False,
    upx=False,
)
tools_exe = EXE(
    PYZ(tools_a.pure),
    tools_a.scripts,
    [],
    exclude_binaries=True,
    name=TOOLS_NAME,
    icon=ICON,
    version=version_info("APFS Explorer Lite command-line tools (read-only)", TOOLS_NAME),
    console=True,
    upx=False,
)

coll = COLLECT(
    app_exe, app_a.binaries, app_a.datas,
    tools_exe, tools_a.binaries, tools_a.datas,
    name=APP_NAME,
    upx=False,
)
