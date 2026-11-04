# APFS Explorer Lite

A Windows 11 desktop app for **reading** files from an unencrypted, APFS-formatted
external SSD and copying selected data to a folder on a local NTFS drive.

> **Status: final audit done (2026-11-04).** On the owner's real, unencrypted APFS
> SSD, the app (from source and as the packaged .exe) browsed the drive and copied
> files and a whole folder to Windows, each copy SHA-256-verified. Windows' I/O counters
> showed **0 writes** to the SSD in every run. 19 of 20 test requirements pass with
> physical-SSD evidence where needed. The one left, unplugging the SSD during a copy,
> was not done on the real drive. See [AUDIT.md](AUDIT.md) for the final checklist and
> [TEST_REPORT.md](TEST_REPORT.md) for the test evidence.

## Guide

### 1. Run from VS Code

1. Open the `apfs` folder in VS Code (File > Open Folder). Install the **Python**
   extension if VS Code suggests it.
2. First time only, in the VS Code terminal (Terminal > New Terminal):
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python -m pip install -r requirements-dev.txt
   .\.venv\Scripts\python -m pip install -e . --no-deps
   ```
   `.vscode/settings.json` already selects `.venv` as the interpreter.
3. Run: **Run and Debug** (Ctrl+Shift+D) > **APFS Explorer Lite** > Start (F5).
   Or double-click **Start APFS Explorer Lite.bat**.
4. Tests: the **Testing** panel, or Terminal > Run Task > **Run tests**. The tests that
   read real APFS images need them downloaded once: Run Task > **Fetch APFS test images**.

VS Code itself does not run as administrator, so clicking **Restart as Administrator**
in the app starts a new, elevated copy outside the debugger. That's normal.

### 2. Connect and select the external SSD

1. Plug in the SSD. If Windows offers to **initialize** or **format** the disk, choose
   **Cancel**. Windows can't read APFS itself and would erase it.
2. Start the app and click **Restart as Administrator**, then **Yes**. Windows only lets
   administrators read a whole disk, even read-only.
3. Click **Refresh Drives**. Your SSD appears with its APFS container underneath, for
   example "APFS container — Recoverable APFS (damaged first block)" on the
   owner's drive.
4. Select the container and click **Open**, or double-click it.

If the drive doesn't appear, run **Diagnose drive.bat** and keep `drive-report.txt`.
It is read-only and lists counts, not file names.

### 3. Browse files

- The left side lists the volume and its folder tree; the right side lists the files.
- Double-click a folder to open it. Use **Back**, **Forward** and **Up**, the path bar,
  or the folder tree to move around.
- Click a column header to sort by Name, Type, Size or Date Modified. **Ctrl+F**
  searches the current folder.
- **View > Show hidden items** shows macOS files such as `.DS_Store`. **View > Theme**
  switches light/dark.
- **Drives** closes the drive and returns to the drive list.

### 4. Copy folders (and files)

1. Select folders and/or files. Use Ctrl-click and Shift-click to select several, and
   Ctrl+A to select everything in the folder.
2. Click **Copy Selected**, or right-click > **Copy to this PC…**.
3. **Save to:** click **Browse…** and choose a folder on this PC, for example a new
   folder in Documents. Windows system folders are refused.
4. **If a file already exists:** *Ask me* (Replace / Skip / Keep both), *Skip it*, or
   *Keep both* (saved as `name (2).ext`). Nothing is replaced unless you click Replace.
5. Click **Start Copy**. Folders are copied with everything inside, keeping the folder
   structure. The window shows the current file, percentage, amount copied, speed and
   time left. **Cancel** stops cleanly, without leaving any half-copied file.
6. The result window lists anything skipped or failed, with the reason.

### 5. Verify copied data

- **Automatic:** every file is read back after copying, and its size and SHA-256 are
  compared with what was read from the SSD. Only matching copies are saved under their
  real names; a mismatch is listed as failed.
- **Report:** after a copy, **Save Report…** writes a text file with the SHA-256 of every
  copied file. To spot-check a file yourself, run this in PowerShell and compare the
  hash with the report:
  ```powershell
  Get-FileHash -Algorithm SHA256 "C:\path\to\copied\file.docx"
  ```
- **Hardware test:** **Run hardware test.bat** copies samples into a temporary folder,
  checks them against a fresh read of the SSD, and confirms Windows recorded no writes
  to the SSD.

### 6. Build the EXE

Double-click **build.bat**, or in VS Code: Terminal > Run Task > **Build EXE and
installer**. It checks the pinned versions, runs the tests, builds with PyInstaller,
and tests the result outside the project. It then writes the following to `dist\`:
- the portable folder;
- `APFS-Explorer-Lite-<version>-portable-win64.zip`;
- `APFS-Explorer-Lite-<version>-setup.exe` (needs Inno Setup 6);
- `BUILD-MANIFEST.txt` with SHA-256 hashes.

Details: [Building the release](#building-the-release).

### 7. Install the application

- **Portable:** unzip the zip anywhere and run `APFS Explorer Lite.exe`; keep the
  folder together.
- **Installer:** run the setup .exe. By default it installs for you only, needing no
  administrator rights; remove it through Settings > Apps.

Unsigned files may trigger SmartScreen: click **More info > Run anyway**. Details:
[Installing](#installing).

### 8. Known limitations

- **Read-only, one way.** The app never writes to the APFS drive and cannot repair
  it. On the owner's drive the damaged first block stays as it is; the app only reads
  around it.
- **Not supported:** encrypted APFS volumes, Fusion drives and snapshots, which are
  shown as unsupported. Some rare compression methods are reported as unreadable,
  per file.
- **What is not copied:**
  - File contents and modification and access times are copied. Creation dates,
    macOS permissions, extended attributes, Finder tags and resource forks are not.
  - Symbolic links are listed as skipped.
  - Hard links become separate copies.
- **Renamed files:** names Windows doesn't allow (for example with `:` or `?`, or longer
  than 255 characters) are saved with adjusted names, and the report notes each one.
  Paths longer than 260 characters work, but some older programs can't open them.
- **Verification** reads the copy back through Windows. It may come from Windows'
  cache, so it proves Windows stored the right bytes, not that the destination disk's
  surface is healthy.
- **Speed is limited by the drive's connection.** The owner's SSD reads at about
  38 MB/s, which looks like a USB 2.0 link. The app reads at about 95% of that. A
  USB 3 port or cable may be much faster.
- **FAT32 destinations** (some USB sticks) can't hold files over 4 GB; those files are
  listed as failed.
- **Testing coverage:**
  - Tested on Windows 11 (build 26300) with Python 3.14.7, on one physical APFS SSD.
  - Unplugging the SSD in the middle of a copy was tested only with simulated
    disconnects.
  - Installing "for all users" and upgrading over an older version were not tested.
- **Unsigned:** the program is not digitally signed, so SmartScreen and some
  antivirus programs may warn.

## Installing

Two options; both work offline and include everything (Python, Qt and the APFS
reader). Neither changes anything on the APFS drive.

**Portable (no installation):**
1. Unzip `APFS-Explorer-Lite-<version>-portable-win64.zip` anywhere, for example
   Documents or a USB stick. Keep the whole `APFS Explorer Lite` folder together.
2. Double-click `APFS Explorer Lite.exe`.
3. To remove it, delete the folder.

**Installer:**
1. Run `APFS-Explorer-Lite-<version>-setup.exe`.
2. By default it installs for you only, in `%LOCALAPPDATA%\Programs\APFS Explorer Lite`.
   That needs **no administrator rights**. Choosing "install for all users" puts it
   in Program Files and needs administrator rights.
3. It adds Start menu entries for the app, the read-only hardware test, the drive
   diagnostic, the read-me and Uninstall. A desktop icon is optional.
4. To remove it, use Settings > Apps > Installed apps, or Start menu > Uninstall
   APFS Explorer Lite.

**First start:** the files are not digitally signed, so Windows SmartScreen may show
"Windows protected your PC". Click **More info**, then **Run anyway**. Some antivirus
programs are wary of unsigned PyInstaller apps; compare the file's SHA-256 with
`BUILD-MANIFEST.txt` if in doubt.

**Settings and logs** stay after uninstalling:
- Settings: `HKEY_CURRENT_USER\Software\APFS Explorer Lite`
- Logs: `%LOCALAPPDATA%\APFS Explorer Lite`

Delete them by hand if you want them gone.

### Administrator rights: what needs them and why

| Action | Admin needed? | Why |
| --- | --- | --- |
| Install for the current user / run the portable app | No | Files go to your own profile or folder |
| Install for all users | Yes | Writes to Program Files |
| Start the app and list drives | No | Disk metadata can be read with a query-only handle |
| Confirm the APFS signature, open, browse and copy from the SSD | **Yes** | Windows only lets administrators open a whole disk (`\\.\PhysicalDriveN`), even read-only |
| Hardware test, drive diagnostic | Yes | Same reason; their launchers ask for it |
| Save copies to a folder | No (the app refuses Windows system folders anyway) | Ordinary file writes in a folder you choose |

The app starts without administrator rights. Click **Restart as Administrator** and
approve the Windows prompt (UAC). The app opens the disk with read access only:
there's no code path that writes to it, and the hardware test checks Windows'
write counter for the disk. The app does not ask for administrator rights on every
start (its manifest is "as invoker"); it asks only when you need to read the drive.

### What's in the package

| File | Purpose |
| --- | --- |
| `APFS Explorer Lite.exe` | The app (windowed) |
| `APFS Explorer Lite Tools.exe` | Console tools: `selftest` (checks this copy of the program), `hwtest`, `diagnose`, `verify` |
| `Run hardware test.bat`, `Diagnose drive.bat` | Double-click launchers for the read-only tools (ask for admin) |
| `_internal\` | Python, Qt / PySide6 and `pyfsapfs` (libfsapfs) as separate, replaceable files |
| `README.txt`, `THIRD-PARTY-NOTICES.txt`, `licenses\` | Instructions and the LGPL-3.0, GPL-3.0 and Python licence texts |

The APFS reader (libfsapfs, LGPL-3.0-or-later) and Qt (LGPL-3.0) are redistributed
unmodified, as separate DLL/PYD files in the one-folder layout. Users can replace
them, which is what the LGPL asks for. Their source locations are listed in
`THIRD-PARTY-NOTICES.txt`.

If the app can't load its APFS reader (a damaged install, or security software
blocking it), it still opens and says so. If Qt itself can't load, a message box
explains it instead of a silent exit. Native crashes are recorded in
`%LOCALAPPDATA%\APFS Explorer Lite\logs\crash.log`.

## How it works

### How copying works

- Selected folders are walked on the APFS drive; the folder structure is recreated
  under the destination. Symbolic links and special files are listed as skipped,
  never followed.
- Names Windows doesn't allow are adjusted (`a:b` → `a_b`, `CON` → `_CON`, trailing
  dots and spaces removed, names over 255 characters shortened). Names that would
  then clash (including `Report` vs `report`) get a `(2)` suffix instead of
  overwriting each other. Paths over 260 characters work (`\\?\` paths), though
  some older programs may not open files with very long paths.
- Each file is streamed in 1 MiB chunks into a temporary file `~xxxx.apfs-partial` in
  the target folder, hashed while reading, flushed to disk, read back and compared
  (size and SHA-256), given the original modification date, then renamed to its real
  name. Folder dates are set at the end.
- An existing file is replaced only after an explicit **Replace**, and then
  atomically, so cancelling or a failure never damages it.
- Free space is checked before starting and before each file. A full destination,
  an unplugged APFS drive or a destination that disappears stops the job and the
  report says why. Other per-file problems (for example a file open in another
  program) fail that file only.
- Only one copy runs at a time. Closing the app during a copy asks first, then
  cancels cleanly.
- The read-back may be served from Windows' file cache, so it proves Windows stored
  exactly the original bytes; it is not a surface test of the destination disk.

If the drive stops responding (for example, it's unplugged), the app returns to the
drive list with a message. **Diagnose drive.bat** writes a read-only report to
`drive-report.txt` if a drive is not detected.

## Testing

| What | How |
| --- | --- |
| All automated tests | `python -m pytest` (about 25 s) |
| Test report (requirements 1-20, evidence by kind) | `python scripts/make_test_report.py` → `TEST_REPORT.md` |
| Hardware test on the APFS SSD | double-click **Run hardware test.bat** (asks for administrator access; also rebuilds the report) |
| GUI test on the real SSD (as administrator, SSD attached) | `$env:APFS_LIVE_SSD_TESTS="1"; python -m pytest tests/test_live_ssd.py`: drives the real windows (Refresh Drives, Open, browse, Copy Selected), re-verifies every copy and checks the SSD's write counter; the copies are deleted afterwards |
| Hardware test dry run on an image | `python -m apfs_explorer_lite.hwtest --image tests\data\external\apfs.dmg --out dry.json` |

The hardware test is read-only. It finds the external APFS disk and records Windows'
write counter for it. It then walks the volume, and copies samples into a temporary
folder: small files, the largest file up to 2 GB, one nested folder, and any Unicode
or Windows-incompatible names. Each copy is checked against a fresh read of the
source. It also cancels a copy part-way and checks for leaked handles, and confirms
that the disk's write counter did not change. Optionally it asks you to unplug the
SSD during a copy. The temporary copies are deleted. `hardware-results.json` holds
counts, sizes and timings only, no file names.

`tests/requirements_map.py` lists which tests cover each requirement and whether
that evidence is *mock* (simulated), *image* (real macOS-made APFS image), *os*
(real Windows behaviour) or *hardware* (the physical SSD, only from the hardware
test).

## Logging and privacy

The log is JSON lines in `%LOCALAPPDATA%\APFS Explorer Lite\logs\apfs_explorer_lite.log`
(rotating, 5 x 2 MB). Each line has `ts`, `level`, `logger`, `event` and fields
such as sizes, counts, durations, error types and Windows error codes.

- File contents are never logged.
- File and folder names are logged only as a `path_id`: a keyed hash, plus the
  extension and folder depth. The key is random per run, so ids can't be
  matched across runs or looked up from a list of common names.
- A safety net replaces anything in a message or exception that looks like a
  Windows or APFS path, or a quoted name. Traceback frames (this program's source
  code locations) are kept.
- Per-file events are at DEBUG level (not written by default). Failures and
  summaries are written.
- For troubleshooting only, set `APFS_EXPLORER_LOG_NAMES=1` to log real names.

The saved copy report (**Save Report…**) does list names and SHA-256 values, because
you choose to create it and where it goes. The diagnostic report
(`drive-report.txt`) shows counts only.

## Safety rules (enforced in code, not just documented)

| Rule | Where it is enforced |
| --- | --- |
| The source is opened read-only, never with `w`, `a`, `x` or `+` | `safety.require_read_only_mode` |
| The backend interface has no write, delete, rename, format or repair methods | `backends/base.py`; checked by `test_backend_interface_has_no_write_operations` |
| Only backends that declare `read_only = True` can be registered | `BackendRegistry.register` |
| No silent fallback: an unavailable backend raises, it is never substituted | `BackendRegistry.create` |
| Destination must be an existing absolute folder, not a device path | `safety.validate_destination_root` |
| No source writes, checked on hardware: Windows' per-disk write counter must not change during the hardware test | `hwtest.py` using the read-only `IOCTL_DISK_PERFORMANCE` query |
| Destination must not be a Windows system folder (Windows, Program Files, ProgramData) | `safety.validate_destination_root` |
| Every output path is built from sanitized NTFS names under the resolved destination (no `..`, separators, device names) | `copying.Destination.path`, `safety.sanitize_component` |
| Writes never go through a junction, symlink or mount point below the destination; each folder level is checked with `lstat` right before writing | `Destination.check_folder` |
| Files are written to a new temporary file (exclusive create), verified, then renamed; an existing file is never overwritten without an explicit Replace | `Destination.create_temp`, `Destination.place` |
| Cancel, errors and disconnects remove only the job's own `.apfs-partial` temporary file | `Destination.remove_temp` refuses any other name |
| Copying only reads from the backend (`list_directory`, `open_file`) | `copying/job.py`; tests check source images are byte-identical afterwards |
| Device handles are opened only with access `0` (metadata queries) or `GENERIC_READ` | `Win32DiskApi._open` raises before calling `CreateFileW` |
| Only five read-only metadata IOCTLs can be sent | `READ_ONLY_IOCTLS` allow-list in `disks/win32.py`; tests check their access bits and that layout, lock and dismount IOCTLs are rejected |
| No diskpart, PowerShell disk cmdlets, `WriteFile` or `GENERIC_WRITE` anywhere | `test_source_tree_has_no_write_primitives` scans `src/` |
| Disk content is read only from APFS-typed partitions on external, non-system disks, at most 1 MiB and sector-aligned | `WindowsDiskDiscovery._scan_disk` and `Win32DiskApi.read_bytes` |
| Disks are only scanned when the user clicks **Refresh Drives** | `DriveSelectionWidget`; nothing scans at startup |
| APFS containers are opened only in libfsapfs's read mode `"r"` (the binding rejects `"w"`), through a stream with no `write` method | `LibfsapfsBackend.open`, `source_io.ReadOnlyWindowStream` |
| Every source read is aligned and limited to the selected partition | `ReadOnlyWindowStream`; `Win32SectorReader.read_at` checks alignment and bounds |
| Test images are byte-identical after the test run | `images_unchanged` fixture compares SHA-256 hashes |
| No fake filesystem data in production | The only fake disk data is in `tests/` |

## Project layout

```
src/apfs_explorer_lite/
  __main__.py          entry point (python -m apfs_explorer_lite, PyInstaller)
  app.py               bootstrap: logging, QApplication, main window
  errors.py            exception hierarchy (user_message + detail)
  models.py            shared dataclasses, APFS path normalization
  safety.py            read-only and destination-containment guards
  logging_config.py    rotating log in %LOCALAPPDATA%\APFS Explorer Lite\logs
  structured_log.py    JSON log lines, path ids and scrubbing (no names, no contents)
  hwtest.py            hardware acceptance test on the SSD (python -m apfs_explorer_lite.hwtest)
  selftest.py          checks an installed/portable build (modules, Qt, reader, DLL origins)
  tools.py             entry point of "APFS Explorer Lite Tools.exe" (selftest, hwtest, ...)
  resources/           app icon (app.ico, app.png; drawn by scripts/make_icon.py)
  source_io.py         read-only sector readers + aligned, cached partition window
  verify.py            read-only acceptance tool (python -m apfs_explorer_lite.verify)
  backends/
    base.py            ApfsBackend ABC + ReadableStream protocol (read-only)
    libfsapfs_backend.py   libfsapfs (pyfsapfs) backend
    decmpfs.py         detects transparent compression and its method
    registry.py        explicit backend selection, no fallback
  disks/
    api.py             DiskApi protocol (what discovery needs from Windows)
    win32.py           ctypes Win32 calls; the only code that opens device handles
    parsing.py         parsers for the IOCTL output structures
    gpt.py             GPT parser for disk image files
    apfs_signature.py  NXSB superblock and Fletcher-64 checksum check
    discovery.py       classifies disks, partitions and APFS containers
  formatting.py        sizes and durations for display
  copying/
    plan.py            walks the selection, maps names to unique Windows names
    destination.py     the only code that writes: safe folders, temp files, rename
    job.py             CopyJob: stream, hash, verify, conflicts, cancel, progress
    report.py          per-file results and the saved text report
  gui/
    main_window.py     main window, menus, status bar
    drive_selection.py drive-selection screen (disk > partition > container tree)
    explorer.py        file explorer (folder tree, file list, toolbar, search)
    copy_dialog.py     copy dialog: destination, progress, result, conflict question
tests/                 pytest suite; doubles.py and win_structs.py are test-only;
                       requirements_map.py links tests to the Phase 6 requirements
scripts/               fetch_test_images.py (real APFS test images, SHA-256 pinned),
                       make_test_report.py (runs the tests, writes TEST_REPORT.md),
                       build_release.py, check_package.py, make_icon.py
packaging/             PyInstaller spec, Inno Setup script, portable/ (files added to the build)
build.bat              builds the release into the dist folder
```

To add another APFS reader, subclass `ApfsBackend`, implement its methods using
read-only access only, and register it in `backends/registry.py`.

## Setup

Requires Windows 10/11 x64 and Python 3.12 or newer (developed on 3.14.7).

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
pip install -e . --no-deps
```

Run the app and the tests:

```powershell
python -m apfs_explorer_lite
python scripts/fetch_test_images.py   # once; real APFS images for the backend tests
python -m pytest

# Optional: read-only checks against this PC's real disks
$env:APFS_LIVE_DISK_TESTS = "1"; python -m pytest tests/test_live_windows.py
```

### Building the release

Double-click **build.bat**, or run `.venv\Scripts\python scripts\build_release.py`.
Optional flags: `--skip-tests`, `--skip-check`, `--skip-installer`. Everything runs
offline from the installed, pinned packages. The script does the following:

1. Stops if any installed package differs from the pins in `requirements*.txt`.
2. Runs the test suite.
3. Redraws the icon (`scripts/make_icon.py`).
4. Builds with PyInstaller (`packaging/apfs_explorer_lite.spec`, one folder,
   `PYTHONHASHSEED=0`).
5. Adds the read-me, notices, licences and tool launchers from `packaging/portable/`.
6. Tests the build outside the project (`scripts/check_package.py`). It copies it to
   a temporary folder and runs it with a PATH holding only Windows folders. It
   checks that modules, Qt and the APFS reader load, that disk discovery works, and
   that APFS test images open, copy and verify. It checks that every loaded DLL is
   the app's own or Windows', and that the window opens and closes. It also checks
   that a missing APFS reader is reported cleanly. The build stops if any check fails.
7. Zips the folder (sorted entries; set `SOURCE_DATE_EPOCH` for fixed timestamps).
8. Compiles the installer with Inno Setup 6 (`packaging/installer.iss`), if
   `ISCC.exe` is found or `ISCC` is set.
9. Writes `dist\BUILD-MANIFEST.txt` with versions and SHA-256 hashes.

Outputs in `dist\`: `APFS Explorer Lite\` (portable folder),
`APFS-Explorer-Lite-<version>-portable-win64.zip`,
`APFS-Explorer-Lite-<version>-setup.exe`, `BUILD-MANIFEST.txt`,
`package-check.json` and screenshots from the check.

The build is scripted and pinned, but not bit-for-bit identical between runs
(PyInstaller embeds build paths and timestamps). The manifest records exactly what
was built. To change the version, edit `__version__` in
`src/apfs_explorer_lite/__init__.py` (and `pyproject.toml`).

## How drive detection works

All of this uses the Win32 API through `ctypes`. Nothing calls PowerShell, WMI or diskpart.

1. **List disks:** `QueryDosDeviceW` lists every `PhysicalDriveN` that currently exists.
2. **Find the Windows disk:** `IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS` on `%SystemDrive%`.
   That disk is never read.
3. **Read disk metadata (no admin rights needed):** each disk is opened with access `0`
   and sent these queries:
   - `IOCTL_STORAGE_QUERY_PROPERTY`: model, serial, bus type, removable flag
   - `IOCTL_DISK_GET_DRIVE_GEOMETRY_EX`: capacity, sector size
   - `IOCTL_DISK_GET_DRIVE_LAYOUT_EX`: partition style and partitions with GPT type GUIDs
   - `IOCTL_STORAGE_GET_HOTPLUG_INFO`: device hot-plug flag
4. **Decide if a disk is external:** bus type USB, IEEE 1394, SD or MMC, *or* the
   device hot-plug flag (Thunderbolt or USB4 NVMe enclosures), *or* removable media.
5. **Pick APFS candidates:** only GPT partitions whose type GUID is Apple APFS
   (`7C3457EF-…`, plus the Apple Silicon ISC and Recovery types). Unrecognized types,
   MBR partitions and Apple HFS+ partitions are never treated as APFS.
6. **Check the signature (needs admin rights):** for candidates on external, non-system
   disks only, the first 4 KiB of the partition is read through a `GENERIC_READ` handle.
   It is checked for the `NXSB` magic, a valid block size and the Fletcher-64 checksum.
   Results:
   - **Verified:** the superblock is valid.
   - **Tentative:** the read was denied, failed, or the checksum didn't match.
   - **Hidden:** there is no superblock.

Running without admin rights, the app shows a banner and a **Restart as Administrator**
button (a UAC prompt), and candidates appear as *Tentative*.

## APFS backend

### Choosing a reader (researched 2026-11-03)

| Option | Windows | Enumerate and read | License | Verdict |
| --- | --- | --- | --- | --- |
| **libfsapfs** (`libfsapfs-python` 20261030) | Prebuilt cp314 Windows wheel; one self-contained `.pyd` (imports only Python, the MSVC runtime and Windows system DLLs) | Yes, verified on real images | LGPL-3.0-or-later | **Selected** |
| 7-Zip `7z.exe` | Native | Extracts APFS images since 22.00; reading a live physical drive not confirmed | LGPL-2.1 (+ unRAR restriction) | Possible CLI fallback; not evaluated in depth |
| dissect.apfs (Fox-IT) | Pure Python | Parser for APFS | AGPL-3.0 | Not chosen: AGPL would cover the whole distributed app |
| apfs-fuse, Linux apfs module | No (FUSE or Linux kernel) | Not applicable | GPL | Not usable on Windows |
| Paragon / commercial drivers | Yes | Mounts volumes, including write support | Proprietary | Ruled out: write-capable driver |

Facts about libfsapfs, from its README and wiki: status **experimental**; read-only;
supports APFS v2, encryption (password), extended attributes, and decmpfs ZLIB and
LZVN. The README also lists LZFSE and raw, while the wiki home page lists them as
unsupported. Not supported: APFS v1, Fusion drives, snapshots, T2 encryption, and
decmpfs type 1 and LZBitmap.

Behaviour I measured on libfsapfs-python 20261030 (it isn't in the documentation):

- `container.open_file_object(obj, "r")` is the only open mode; `"w"` raises
  `ValueError: unsupported mode`.
- On the file object it calls only `seek(offset, 0)`, `get_offset()` and `read(n)`,
  and the reads are small and unaligned (32 or 88 bytes). Raw Windows disk handles
  reject reads like that, so `ReadOnlyWindowStream` reads aligned 256 KiB chunks and
  serves requests from an LRU cache.
- `container.open(path)` puts `\\?\` in front of paths. The app always uses file
  objects instead, which also lets it open one partition at an offset.
- `volume.size` raises `OSError` on real volumes, so it isn't used.
- Times come back as naive `datetime`s; the backend uses `get_*_time_as_integer()`
  (nanoseconds since 1970 UTC) and builds timezone-aware values.
- On an encrypted volume, `is_locked()` returns `True` and reads fail with a
  decryption error. The backend reports it as unsupported up front.
- Its file content matched the raw bytes at the file's on-disk block, so content was
  verified without going through libfsapfs.

### Administrator rights

libfsapfs needs none. Opening `\\.\PhysicalDriveN` with `GENERIC_READ` does; without
it the backend raises `DiskAccessDeniedError` ("Administrator rights are required").
Image files open without admin rights.

### Unsupported features

| Case | Behaviour |
| --- | --- |
| Encrypted (locked) volume | Listed as locked; browsing raises `ApfsVolumeLockedError` |
| Compression method listed as unsupported (1, 13, 14, unknown) | `open_file` raises `ApfsUnsupportedFeatureError`, naming the method |
| Compression method listed as supported | Read through libfsapfs; every read is length-checked (no compressed sample has been tested yet) |
| Symlinks | Listed with their target; not followed; opening one raises a clear error |
| Fusion drives, APFS v1, damaged containers | `ApfsContainerOpenError` with the libfsapfs detail |
| Snapshots | Not exposed; only the live file system is read |
| Drive unplugged while reading | The original `DiskDisconnectedError` is raised instead of a generic libfsapfs error |

### Real-SSD acceptance test

Use the **hardware test**, which supersedes the older `verify` tool for real drives:
double-click **Run hardware test.bat** (packaged build or project folder). It ran on the
owner's SSD on 2026-11-04 and passed (see TEST_REPORT.md).

`python -m apfs_explorer_lite.verify --disk N [--read /path]` still works as a quick
read-only listing and SHA-256 check (run as administrator). It has been tested on the
APFS test images only, not on the physical SSD.

## Dependencies

| Package | Version | Purpose | License |
| --- | --- | --- | --- |
| PySide6-Essentials (+ shiboken6) | 6.12.0 | Qt GUI (QtCore, QtGui, QtWidgets) | LGPL-3.0 / GPL-2.0 / GPL-3.0, or commercial Qt license |
| libfsapfs-python (`pyfsapfs`) | 20261030 | Read-only APFS parsing (libyal libfsapfs) | LGPL-3.0-or-later |
| pytest (dev) | 9.2.0 | Tests | MIT |
| PyInstaller (dev) | 6.22.3 | Packaging | GPL-2.0-or-later with bootloader exception |

Everything installs from prebuilt wheels and runs fully offline. The app makes no
network calls.

The `libfsapfs-python` wheel also installs a top-level `_build.py` module into
site-packages. It's harmless, but don't name a project module `_build`.

`PySide6-Essentials` is used instead of the `PySide6` metapackage because the app
needs only QtWidgets. The metapackage adds WebEngine and other modules worth several
hundred MB.

## Licensing notes

This is a summary, not legal advice.

- **This project has no license yet.** Pick one before distributing (for example
  MIT, or LGPL/GPL to match the dependencies).
- **LGPL components (Qt/PySide6, libfsapfs):** you can ship a closed-source app that
  links to them dynamically if you (a) include their license texts, (b) say that the
  app uses them and where to get their source, and (c) let users replace the LGPL
  libraries with their own builds. PyInstaller's **one-folder** mode keeps the Qt
  DLLs and the `pyfsapfs` `.pyd` as separate replaceable files, which meets (c). A
  one-file build makes that harder, so the spec uses one-folder.
- **PyInstaller:** its bootloader exception lets you distribute the built
  executable under any license.
- Do not modify the LGPL libraries without publishing those changes.
- **Test images** (`apfs.raw`, `apfs_encrypted.dmg` from dfVFS; `apfs.dmg` from
  Plaso) are Apache-2.0. They are downloaded for testing and not committed or shipped.

## Roadmap

1. **Phase 1 (done):** project structure, interfaces, safety guards, empty GUI, tests.
2. **Phase 2 (done):** read-only external disk and APFS container discovery, and the
   drive-selection screen.
3. **Phase 3 (done):** a read-only libfsapfs backend verified on real APFS images,
   plus recovery for a damaged first block (an in-memory backup superblock).
4. **Phase 4 (done):** graphical explorer: volumes and folder tree, file list,
   toolbar, path bar, search, sorting, light and dark themes, background loading.
5. **Phase 5 (done):** one-way copy engine and copy dialog: streaming, SHA-256
   verification, conflicts (ask, skip, keep both), cancel, free-space checks, long
   and invalid names, timestamps, per-file report.
6. **Phase 6 (done):** reliability, security and performance tests mapped to 20
   requirements, structured private logging, hardware test tool, test report, and
   fixes for six confirmed defects (see TEST_REPORT.md).
7. **Phase 7 (done):** Windows packaging: portable folder and zip, Inno Setup
   installer, icon, version information, console tools, packaged-build checks.
8. **Final audit (done):** unused code removed, safety re-audited, GUI and packaged
   hardware tests on the real SSD, guide and final checklist (AUDIT.md).
