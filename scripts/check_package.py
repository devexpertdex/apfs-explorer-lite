"""Test a packaged build the way it runs on a PC without Python.

    python scripts/check_package.py ["dist/APFS Explorer Lite"] [--out dist/package-check.json]

1. Copies the build to a new folder outside the project and runs it with a
   stripped environment: PATH holds only Windows folders, no PYTHON* or Qt
   variables, the working folder is the copy.
2. Runs "APFS Explorer Lite Tools.exe selftest" with the real APFS test images
   (also copied out of the project): modules, Qt, APFS reader, disk discovery,
   open/walk/copy/verify, and "every loaded DLL comes from the app or Windows".
3. Starts "APFS Explorer Lite.exe", waits for its window, checks every DLL the
   running process loaded, saves a screenshot, and closes it normally.
4. Simulates a missing or blocked APFS reader (pyfsapfs*.pyd removed from a
   second copy): the self-test must report it cleanly and the window must still open.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_EXE = "APFS Explorer Lite.exe"
TOOLS_EXE = "APFS Explorer Lite Tools.exe"
IMAGES = ("apfs.dmg", "apfs.raw")
WM_CLOSE = 0x0010
PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x0400, 0x0010
LIST_MODULES_ALL = 0x03

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
kernel32.OpenProcess.restype = wintypes.HANDLE
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]  # fmt: skip
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def clean_env(work: Path) -> dict[str, str]:
    """What a PC without Python or developer tools provides."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    keep = ("SystemRoot", "SystemDrive", "windir", "USERPROFILE", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
            "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "ProgramData", "ProgramFiles",
            "ProgramFiles(x86)", "ProgramW6432", "PUBLIC", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
            "PATHEXT", "ComSpec")  # fmt: skip
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = os.pathsep.join([rf"{system_root}\System32", system_root, rf"{system_root}\System32\Wbem"])
    env["TEMP"] = env["TMP"] = str(work / "temp")
    (work / "temp").mkdir(exist_ok=True)
    return env


def process_modules(pid: int) -> list[Path]:
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        modules = (wintypes.HMODULE * 4096)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModulesEx(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), LIST_MODULES_ALL):
            raise OSError(ctypes.get_last_error(), "EnumProcessModulesEx failed")
        paths = []
        for module in modules[: needed.value // ctypes.sizeof(wintypes.HMODULE)]:
            buffer = ctypes.create_unicode_buffer(32768)
            if psapi.GetModuleFileNameExW(handle, module, buffer, len(buffer)):
                paths.append(Path(buffer.value))
        return paths
    finally:
        kernel32.CloseHandle(handle)


def find_window(pid: int, title: str) -> int | None:
    found: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buffer, 512)
            if buffer.value.startswith(title):
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found[0] if found else None


def window_title(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def screenshot(hwnd: int, path: Path) -> bool:
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.8)
    pixmap = app.primaryScreen().grabWindow(hwnd)
    return not pixmap.isNull() and pixmap.save(str(path))


def outside(paths: list[Path], bundle: Path) -> list[str]:
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    return [str(p) for p in paths if not (p.resolve().is_relative_to(bundle) or p.resolve().is_relative_to(windows))]


def run_selftest(bundle: Path, env: dict[str, str], images: list[Path], out: Path) -> dict:
    args = [str(bundle / TOOLS_EXE), "selftest", "--out", str(out)]
    for image in images:
        args += ["--image", str(image)]
    proc = subprocess.run(args, cwd=bundle, env=env, capture_output=True, text=True, errors="replace",
                          timeout=300)  # fmt: skip
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"checks": []}
    return {"exit_code": proc.returncode, "output": proc.stdout + proc.stderr, "checks": data.get("checks", [])}


def launch_gui(bundle: Path, env: dict[str, str], shot: Path) -> dict:
    proc = subprocess.Popen([str(bundle / APP_EXE)], cwd=bundle, env=env)
    result: dict = {"pid": proc.pid}
    try:
        hwnd = None
        deadline = time.monotonic() + 30
        while hwnd is None and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.25)
            hwnd = find_window(proc.pid, "APFS Explorer Lite")
        result["window"] = hwnd is not None
        result["exited_early"] = proc.poll()
        if hwnd is None:
            return result
        time.sleep(2.0)  # let it finish drawing
        result["title"] = window_title(hwnd)
        modules = process_modules(proc.pid)
        result["modules_loaded"] = len(modules)
        result["modules_outside_app_or_windows"] = outside(modules, bundle.resolve())
        result["qt_platform_plugin"] = any(p.name.lower() == "qwindows.dll" for p in modules)
        result["screenshot"] = str(shot) if screenshot(hwnd, shot) else None
        # Look the window up again: Qt may have replaced the native window since it first appeared.
        current = find_window(proc.pid, "APFS Explorer Lite") or hwnd
        result["window_replaced"] = current != hwnd
        hwnd = current
        result["close_posted"] = bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))
        try:
            result["exit_code"] = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            # Tell a lost message apart from an app that ignores closing.
            result["first_close_ignored"] = True
            result["window_after_timeout"] = find_window(proc.pid, "APFS Explorer Lite") is not None
            retry = find_window(proc.pid, "APFS Explorer Lite")
            result["retry_posted"] = bool(retry and user32.PostMessageW(retry, WM_CLOSE, 0, 0))
            result["exit_code"] = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            result["killed"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", nargs="?", type=Path, default=ROOT / "dist" / "APFS Explorer Lite")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "package-check.json")
    parser.add_argument("--keep", action="store_true", help="keep the temporary copies")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    source = args.bundle.resolve()
    if not (source / APP_EXE).exists():
        print(f"No build at {source}; run scripts/build_release.py first.", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="apfs-package-check-"))
    shots = args.out.parent
    try:
        bundle = work / "APFS Explorer Lite"
        shutil.copytree(source, bundle)
        env = clean_env(work)
        images = []
        for name in IMAGES:
            image = ROOT / "tests" / "data" / "external" / name
            if image.exists():
                shutil.copyfile(image, work / name)
                images.append(work / name)

        print(f"Testing a copy of the build in {bundle} with a stripped environment (no Python on PATH)")
        report: dict = {"bundle_copy": str(bundle), "path": env["PATH"], "images": [i.name for i in images]}
        report["selftest"] = run_selftest(bundle, env, images, work / "selftest.json")
        print(report["selftest"]["output"])
        report["gui"] = launch_gui(bundle, env, shots / "package-check-window.png")
        print("GUI:", json.dumps({k: v for k, v in report["gui"].items() if k != "modules_outside_app_or_windows"}))

        broken = work / "broken" / "APFS Explorer Lite"
        shutil.copytree(bundle, broken)
        for pyd in (broken / "_internal").glob("pyfsapfs*.pyd"):
            pyd.unlink()
        report["missing_reader_selftest"] = run_selftest(broken, env, [], work / "broken.json")
        report["missing_reader_gui"] = launch_gui(broken, env, shots / "package-check-missing-reader.png")

        checks = report["selftest"]["checks"]
        missing = {c["name"]: c for c in report["missing_reader_selftest"]["checks"]}
        verdicts = {
            "selftest passed": report["selftest"]["exit_code"] == 0 and checks and all(c["status"] != "failed" for c in checks),
            "packaged copy is self-contained": any(c["name"].startswith("Self-contained") and c["status"] == "passed" for c in checks),
            "APFS images opened, copied and verified": len(images) > 0
                and sum(c["name"].startswith("Copy ") and c["status"] == "passed" for c in checks) >= len(images),
            "window opened and closed normally": report["gui"].get("window") and report["gui"].get("exit_code") == 0
                and not report["gui"].get("first_close_ignored"),
            "running app loaded no DLLs from outside": report["gui"].get("window")
                and not report["gui"].get("modules_outside_app_or_windows"),
            "missing APFS reader reported cleanly": missing.get("APFS reader (libfsapfs) loads", {}).get("status") == "failed"
                and "could not be loaded" in missing.get("APFS reader (libfsapfs) loads", {}).get("detail", "")
                and missing.get("Qt GUI starts (window not shown)", {}).get("status") == "passed",
            "window still opens without the APFS reader": report["missing_reader_gui"].get("window")
                and report["missing_reader_gui"].get("exit_code") == 0,
        }  # fmt: skip
        report["verdicts"] = verdicts
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("\nPackage check:")
        for name, ok in verdicts.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"Details: {args.out}")
        return 0 if all(verdicts.values()) else 1
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
