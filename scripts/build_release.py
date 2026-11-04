"""Build the Windows release: portable folder, portable .zip and (optionally) the installer.

    .venv\\Scripts\\python scripts\\build_release.py [--skip-tests] [--skip-check] [--skip-installer]

Steps (all offline; nothing is downloaded):
 1. check that the installed packages match the pinned versions in requirements*.txt;
 2. run the test suite;
 3. redraw the icon (scripts/make_icon.py, deterministic);
 4. build with PyInstaller (packaging/apfs_explorer_lite.spec), PYTHONHASHSEED=0;
 5. add README.txt, THIRD-PARTY-NOTICES.txt, licenses\\ and the read-only tool launchers;
 6. test the build outside the project with a stripped environment (scripts/check_package.py);
 7. zip the folder (sorted entries, fixed timestamps from SOURCE_DATE_EPOCH if set);
 8. compile the Inno Setup installer if ISCC.exe is found (or set ISCC=path);
 9. write dist\\BUILD-MANIFEST.txt with versions and SHA-256 hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_DIR_NAME = "APFS Explorer Lite"
PORTABLE_FILES = ROOT / "packaging" / "portable"
ISCC_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
]


def step(text: str) -> None:
    print(f"\n=== {text}", flush=True)


def app_version() -> str:
    text = (ROOT / "src" / "apfs_explorer_lite" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)  # type: ignore[union-attr]


def pinned_versions() -> dict[str, str]:
    pins = {}
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "==" in line:
                package, version = line.split("==", 1)
                pins[package.strip()] = version.strip()
    return pins


def check_pins() -> dict[str, str]:
    pins = pinned_versions()
    problems = []
    for package, wanted in pins.items():
        try:
            installed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{package} is not installed (needs {wanted})")
            continue
        if installed != wanted:
            problems.append(f"{package} {installed} is installed, but {wanted} is pinned")
    if problems:
        raise SystemExit("Build stopped: " + "; ".join(problems) + ". Run: pip install -r requirements-dev.txt")
    print("  pinned versions OK: " + ", ".join(f"{p} {v}" for p, v in pins.items()))
    return pins


def run(args: list[str], **kwargs: object) -> None:
    print("  $ " + subprocess.list2cmdline(args), flush=True)
    subprocess.run(args, check=True, cwd=ROOT, **kwargs)  # type: ignore[call-overload]


def safe_rmtree(path: Path) -> None:
    resolved = path.resolve()
    if resolved.parent != ROOT or resolved.name not in ("dist", "build"):
        raise SystemExit(f"Refusing to delete {resolved}")
    shutil.rmtree(resolved, ignore_errors=False) if resolved.exists() else None


def add_portable_files(app_dir: Path, version: str, pins: dict[str, str]) -> None:
    pyd = next((app_dir / "_internal").glob("pyfsapfs*.pyd"))
    values = {
        "VERSION": version, "LIBFSAPFS_VERSION": pins["libfsapfs-python"], "PYSIDE_VERSION": pins["PySide6-Essentials"],
        "PYINSTALLER_VERSION": pins["pyinstaller"], "PYTHON_VERSION": platform.python_version(), "PYFSAPFS_FILE": pyd.name,
    }  # fmt: skip
    for source in sorted(PORTABLE_FILES.iterdir()):
        text = source.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        newline = "\r\n" if source.suffix.lower() in (".bat", ".txt") else "\n"
        (app_dir / source.name).write_text(text, encoding="utf-8", newline=newline)
    licenses = app_dir / "licenses"
    licenses.mkdir()
    dist_info = Path(importlib.metadata.distribution("libfsapfs-python")._path)  # type: ignore[attr-defined]
    shutil.copyfile(dist_info / "licenses" / "COPYING.LESSER", licenses / "LGPL-3.0.txt")
    shutil.copyfile(dist_info / "licenses" / "COPYING", licenses / "GPL-3.0.txt")
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python-LICENSE.txt")


def make_zip(app_dir: Path, target: Path) -> None:
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))
    stamp = datetime.fromtimestamp(max(epoch, 315532800), UTC).timetuple()[:6]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in app_dir.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(f"{APP_DIR_NAME}/{path.relative_to(app_dir).as_posix()}", stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def find_iscc() -> Path | None:
    if os.environ.get("ISCC"):
        return Path(os.environ["ISCC"])
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    return next((p for p in ISCC_CANDIDATES if p.exists()), None)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-check", action="store_true", help="skip testing the build outside the project")
    parser.add_argument("--skip-installer", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("The Windows build must run on Windows.")
    version = app_version()
    started = datetime.now().astimezone()
    print(f"Building APFS Explorer Lite {version} with Python {platform.python_version()} ({sys.executable})")

    step("1/9 Pinned dependency versions")
    pins = check_pins()

    if not args.skip_tests:
        step("2/9 Test suite")
        # A private temp folder: pytest's shared one can hold leftovers from runs as administrator.
        run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={BUILD / 'pytest-tmp'}"])

    step("3/9 Icon")
    run([sys.executable, "scripts/make_icon.py"])

    step("4/9 PyInstaller")
    safe_rmtree(BUILD)
    safe_rmtree(DIST)
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    run([sys.executable, "-m", "PyInstaller", "packaging/apfs_explorer_lite.spec", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(BUILD)], env=env)  # fmt: skip
    app_dir = DIST / APP_DIR_NAME

    step("5/9 Notices, licenses and tool launchers")
    add_portable_files(app_dir, version, pins)

    check_result = "skipped"
    if not args.skip_check:
        step("6/9 Test the build outside the project (stripped environment)")
        code = subprocess.run([sys.executable, "scripts/check_package.py", str(app_dir),
                               "--out", str(DIST / "package-check.json")], cwd=ROOT).returncode  # fmt: skip
        check_result = "passed" if code == 0 else f"FAILED (exit {code})"
        if code != 0:
            raise SystemExit("Build stopped: the packaged app failed its checks (see dist/package-check.json).")

    step("7/9 Portable zip")
    zip_path = DIST / f"APFS-Explorer-Lite-{version}-portable-win64.zip"
    make_zip(app_dir, zip_path)

    setup_path: Path | None = None
    if not args.skip_installer:
        step("8/9 Installer (Inno Setup)")
        iscc = find_iscc()
        if iscc is None:
            print("  Inno Setup (ISCC.exe) not found; installer skipped. Install Inno Setup 6 or set ISCC=path.")
        else:
            run([str(iscc), "/Q", f"/DAppVersion={version}", f"/DSourceDir={app_dir}", f"/DOutputDir={DIST}",
                 str(ROOT / "packaging" / "installer.iss")])  # fmt: skip
            setup_path = DIST / f"APFS-Explorer-Lite-{version}-setup.exe"

    step("9/9 Manifest")
    outputs = [zip_path, app_dir / f"{APP_DIR_NAME}.exe", app_dir / f"{APP_DIR_NAME} Tools.exe"]
    if setup_path is not None:
        outputs.insert(1, setup_path)
    size = sum(p.stat().st_size for p in app_dir.rglob("*") if p.is_file())
    files = sum(1 for p in app_dir.rglob("*") if p.is_file())
    fields = [
        ("Built", f"{started.isoformat(timespec='seconds')} on {platform.platform()}"),
        ("Python", f"{platform.python_version()} ({platform.architecture()[0]})"),
        *pins.items(),
        ("Package check", check_result),
        ("Folder size", f"{size / 1e6:.1f} MB ({files} files)"),
    ]
    lines = [
        f"APFS Explorer Lite {version} - build manifest",
        *[f"{label + ':':<21}{value}" for label, value in fields],
        "",
        "SHA-256:",
        *[f"  {sha256(p)}  {p.relative_to(DIST).as_posix()}" for p in outputs],
    ]
    (DIST / "BUILD-MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nDone. Output in {DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
