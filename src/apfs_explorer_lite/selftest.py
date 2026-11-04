"""Self-test of an installed or portable build (also runs from source).

    "APFS Explorer Lite Tools.exe" selftest [--image PATH ...] [--out results.json]

Checks that the bundled Python modules, Qt and the APFS reader load; that the
Qt GUI starts (without showing a window); that every loaded DLL comes from the
app folder or Windows itself (not from a Python installation); and that disk
discovery runs (read-only). With ``--image``, it also opens each APFS image,
walks it, copies its files to a temporary folder and verifies them, using the
same checks as the hardware test. No disk is written to; results hold no file names.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from . import APP_NAME, __version__
from .hwtest import FAILED, NOT_TESTED, PASSED, Session, run_checks

# The windowed app's own startup module (apfs_explorer_lite.app) is packed into the app
# .exe, not this one; the package check covers it by launching the app.
MODULES = (
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "pyfsapfs",
    "apfs_explorer_lite.gui.main_window", "apfs_explorer_lite.gui.explorer",
    "apfs_explorer_lite.gui.copy_dialog", "apfs_explorer_lite.copying", "apfs_explorer_lite.disks.discovery",
    "apfs_explorer_lite.backends.libfsapfs_backend", "apfs_explorer_lite.diagnose", "apfs_explorer_lite.verify",
)  # fmt: skip


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """The app folder: the .exe files sit here, the libraries in its _internal subfolder."""
    return Path(sys.executable).resolve().parent


def loaded_dlls() -> list[Path]:
    """Paths of every module (DLL/PYD/EXE) loaded in this process."""
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    process = k32.GetCurrentProcess()
    modules = (wintypes.HMODULE * 2048)()
    needed = wintypes.DWORD()
    psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    if not psapi.EnumProcessModules(process, modules, ctypes.sizeof(modules), ctypes.byref(needed)):
        raise OSError(ctypes.get_last_error(), "EnumProcessModules failed")
    count = min(needed.value // ctypes.sizeof(wintypes.HMODULE), len(modules))
    k32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    paths = []
    for handle in modules[:count]:
        buffer = ctypes.create_unicode_buffer(32768)
        if k32.GetModuleFileNameW(handle, buffer, len(buffer)):
            paths.append(Path(buffer.value))
    return paths


def check_modules() -> tuple[str, str, dict[str, Any]]:
    failed = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    if failed:
        return FAILED, "; ".join(failed), {}
    from PySide6 import __version__ as pyside_version

    return PASSED, f"{len(MODULES)} modules import (PySide6 {pyside_version})", {"pyside6": pyside_version}


def check_backend() -> tuple[str, str, dict[str, Any]]:
    from .backends import DEFAULT_BACKEND, default_registry

    status = default_registry().availability(DEFAULT_BACKEND)
    return (PASSED if status.available else FAILED), status.message, {"libfsapfs": status.version}


def check_gui() -> tuple[str, str, dict[str, Any]]:
    import shiboken6
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QStyleFactory

    from .backends import DEFAULT_BACKEND, default_registry
    from .disks.discovery import DiscoveryResult
    from .gui import MainWindow
    from .resources import resource_path

    app = QApplication.instance() or QApplication([sys.argv[0]])
    icon = QIcon(str(resource_path("app.ico")))
    status = default_registry().availability(DEFAULT_BACKEND)
    window = MainWindow(status, lambda: DiscoveryResult((), False), is_admin=False)  # never shown, never scans
    title = window.windowTitle()
    style = app.style().name()
    # Delete now: deleteLater needs a running event loop, and a window that outlives
    # QApplication can crash at exit.
    shiboken6.delete(window)
    ok = not icon.isNull() and "windows11" in [k.lower() for k in QStyleFactory.keys()] and title == APP_NAME
    detail = f"platform {app.platformName()}, style {style}, icon sizes {[s.width() for s in icon.availableSizes()]}"
    return (PASSED if ok else FAILED), detail, {"qt_platform": app.platformName(), "style": style}


def check_self_contained() -> tuple[str, str, dict[str, Any]]:
    if not is_frozen():
        return NOT_TESTED, "running from source, not a packaged build", {}
    bundle = bundle_dir()
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    outside = []
    for path in loaded_dlls():
        resolved = path.resolve()
        if not (resolved.is_relative_to(bundle) or resolved.is_relative_to(windows)):
            outside.append(str(resolved))
    detail = (f"all loaded DLLs come from the app folder or Windows" if not outside
              else f"{len(outside)} DLLs loaded from elsewhere: {outside[:5]}")  # fmt: skip
    return (PASSED if not outside else FAILED), detail, {"outside_dlls": outside}


def check_discovery() -> tuple[str, str, dict[str, Any]]:
    from .disks.discovery import WindowsDiskDiscovery

    result = WindowsDiskDiscovery().scan()
    candidates = len(result.candidates)
    detail = (f"{len(result.disks)} disk(s) found, {candidates} external APFS candidate(s), "
              f"administrator: {result.is_admin}")  # fmt: skip
    return (PASSED if result.disks else FAILED), detail, {
        "disks": len(result.disks), "candidates": candidates, "admin": result.is_admin, "warnings": len(result.warnings),
    }  # fmt: skip


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="selftest", description=__doc__.split("\n")[0])
    parser.add_argument("--image", type=Path, action="append", default=[], help="APFS image to open and copy from")
    parser.add_argument("--out", type=Path, help="save results as JSON")
    parser.add_argument("--no-gui", action="store_true", help="skip the Qt GUI start check")
    args = parser.parse_args(argv)

    session = Session(out)
    started = datetime.now().astimezone()
    print(f"{APP_NAME} {__version__} self-test ({'packaged' if is_frozen() else 'from source'}) "
          f"- {sys.executable}", file=out)  # fmt: skip
    session.guarded([], "Bundled Python modules load", check_modules)
    session.guarded([], "APFS reader (libfsapfs) loads", check_backend)
    if not args.no_gui:
        session.guarded([], "Qt GUI starts (window not shown)", check_gui)
    session.guarded([], "Disk discovery runs (read-only)", check_discovery)

    from .backends.libfsapfs_backend import LibfsapfsBackend
    from .verify import SourceError, find_image_containers

    for image in args.image:
        print(f"\n  Image {image.name}:", file=out)
        try:
            source = find_image_containers(image)[0]
        except (SourceError, IndexError, OSError) as exc:
            session.record([], f"Open image {image.name}", FAILED, str(exc))
            continue
        work = Path(tempfile.mkdtemp(prefix="apfs-selftest-"))
        try:
            run_checks(session, LibfsapfsBackend, source, work, max_large_bytes=64 * 2**20)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    # Last, after everything has loaded: no DLL may come from outside the app or Windows.
    session.guarded([], "Self-contained (no DLLs from a Python installation)", check_self_contained)

    failed = [c for c in session.checks if c.status == FAILED]
    print(f"\nRESULT: {'FAIL' if failed else 'PASS'} - {sum(c.status == PASSED for c in session.checks)} passed, "
          f"{len(failed)} failed, {sum(c.status == NOT_TESTED for c in session.checks)} not tested", file=out)  # fmt: skip
    if args.out:
        args.out.write_text(json.dumps({
            "tool": f"{APP_NAME} {__version__} selftest", "packaged": is_frozen(), "executable": sys.executable,
            "started": started.isoformat(timespec="seconds"), "checks": [asdict(c) for c in session.checks],
        }, indent=2), encoding="utf-8")  # fmt: skip
    return 1 if failed else 0
