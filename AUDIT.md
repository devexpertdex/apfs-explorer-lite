# APFS Explorer Lite: final engineering audit

Date: 2026-11-04 · Version 0.1.0 · Windows 11 build 26300 (x64) · Python 3.14.7

**Primary goal:** a Windows 11 user connects an unencrypted APFS external SSD, browses
its files, and copies selected data to their laptop, without modifying the drive.

**Result: met on one real drive.** On the owner's unencrypted APFS SSD (Disk 1, "SPCC
Solid State Disk", USB, 256 GB, first block damaged), the app found the drive and
browsed it. It copied single files, a 1.8 GB file and whole folders to Windows, each
copy SHA-256-verified against a fresh read of the SSD. In every run, Windows' own
I/O counters for the SSD recorded **0 writes and 0 bytes written**.

What remains unverified on real hardware is listed in the checklist below and in
"Not verified". Nothing below is claimed beyond what was run.

## Final checklist: real-SSD verification

Sources of physical-SSD evidence, all on 2026-11-04 with the owner's drive:

- **H**: the packaged hardware test, `APFS Explorer Lite Tools.exe hwtest`, using the
  final build: 13 passed, 0 failed, 1 not applicable.
- **G**: the GUI test `tests/test_live_ssd.py`, run from source as administrator. It
  drove the real windows: Refresh Drives, Open, double-click into a folder, Back,
  select, Copy Selected, Start Copy.
- **P**: the throughput measurement: raw reads and reads through the APFS reader.
- **O**: owner-reported. In Phase 5 the owner said the app (run from source) detected the
  drive and copied data to the PC; not observed by the developer.

| Feature | Real SSD | Evidence |
| --- | --- | --- |
| Detect the external APFS SSD | **Verified** | H, G; the packaged self-test (no admin) also listed it as an APFS candidate |
| Identify the APFS partition (incl. damaged first block) | **Verified** | H, G: "recoverable", read via a checkpoint superblock held in memory |
| Open the source read-only | **Verified** | H, G: read-only handle; 0 writes in Windows' counters |
| List folders (APFS directory enumeration) | **Verified** | H: 42,669 files and 7,342 folders walked (stopped at the 50,000-entry test limit) |
| Browse in the GUI (open a folder, Back) | **Verified** | G (source build); O |
| Copy small files | **Verified** | H: 20 files; G: 3 files via the GUI |
| Copy a large file | **Verified** | H: 1.8 GB file, 32 MB/s including verification |
| Copy a complete folder | **Verified** | H: nested folder, 17 files; G: one folder of 17 files via the GUI |
| Copy nested folders (sub-folders kept) | **Verified** | H |
| Unicode file names | **Verified** | H: 3 files with non-ASCII names |
| Duplicate destination names (keep both / skip) | **Verified** | H |
| Cancel a copy | **Verified** | H: cancelled part-way, nothing half-written left |
| Copy integrity (size + SHA-256, fresh re-read) | **Verified** | H, G: every copy matched |
| Handles released, no temporary files left | **Verified** | H |
| No writes to the source disk | **Verified** | H, G, P: 0 writes and 0 bytes written each time |
| Read speed of the app vs the drive | **Measured** | P: drive 38 MB/s raw; app 35–37 MB/s |
| Packaged app (.exe) reading the SSD | **Verified** (engine) | H ran from the packaged Tools.exe, using the same libraries as the app .exe |
| Packaged app **window** on the SSD | **Not verified** | No one has clicked through the packaged .exe's window with the SSD; the window path was verified from source (G) |
| Windows-incompatible file names | **Not verified** | The drive has none; verified on real NTFS with mock data |
| Unplugging the SSD during a copy | **Not verified** | Simulated disconnects only (real APFS images and mock readers) |
| Destination disk full | Not applicable to the SSD | Simulated: free-space check and an injected "disk full" error |
| Access denied on the destination | Not applicable to the SSD | Real Windows ACLs, files in use and read-only files (with mock or image sources) |
| GUI responsiveness | Not measured on the SSD | Measured with mock data: the longest pause was 0.25–0.6 s for a 30,000-item folder and about 14 ms during a copy |

## Source-disk safety

**No code opens the source APFS disk for writing.** Checked three ways:

1. **Guards at the only entry points to the disk** (`disks/win32.py`):
   - `CreateFileW` is called only from `_create_handle`. That function refuses any
     access other than query (`0`) or `GENERIC_READ`, and any flags other than none or
     `FILE_FLAG_NO_BUFFERING`.
   - `DeviceIoControl` is called only from `_ioctl`. That function refuses any code
     outside five read-only queries plus the read-only disk counters.
   - libfsapfs opens the container only in mode `"r"`, through a stream that has no
     `write` method.
2. **Every write-capable call in `src/`** was listed and its target checked:

   | Call sites | Target |
   | --- | --- |
   | `copying/destination.py`: `os.open(O_CREAT/O_EXCL)`, `os.mkdir`, `os.utime`, `os.replace`/`os.rename`, `os.remove` | Only inside the destination folder you choose, through sanitized names and with link checks; `os.remove` only for the app's own `.apfs-partial` temp files |
   | `copying/job.py`: `os.fdopen(fd, "wb")` | The temp file created above |
   | `app.py`: `crash.log` | `%LOCALAPPDATA%\APFS Explorer Lite\logs` |
   | `gui/copy_dialog.py`: Save Report | A file you choose in a Save dialog |
   | `diagnose.py`, `hwtest.py`, `selftest.py`: `--out` | Report files you name; `rmtree` only on their own `mkdtemp` folders |
   | `QSettings.setValue` | `HKCU\Software\APFS Explorer Lite` |
   | `ShellExecuteW("runas")` | Relaunches this app elevated; nothing else |

3. **No disk-modifying functionality exists.** Searches of `src/` for diskpart,
   PowerShell disk cmdlets (`Format-Volume`, `Initialize-Disk`, `Clear-Disk`,
   `Set-Partition`, `New-Partition`, `Set-Disk`), chkdsk/fsck/mkfs/repair,
   `IOCTL_DISK_SET/CREATE/DELETE/FORMAT/UPDATE`, `FSCTL_*`, `GENERIC_WRITE`,
   `GENERIC_ALL`, `FILE_WRITE_DATA`, `WriteFile` and `SetFilePointer` found nothing.
   `tests/test_win32_safety.py` repeats the main searches on every test run.

Hardware confirmation: Windows' `IOCTL_DISK_PERFORMANCE` counters for the SSD, read
before and after each run, showed 0 writes and 0 bytes written:
- the hardware test, twice: 3,895 MB read each time;
- the throughput measurement;
- the GUI test: 84 MB read.

The damaged first block is handled by substituting a backup superblock **in memory**
(`OverlayReader`). The disk itself is never changed.

## Review by area

| Area | Checked | Outcome |
| --- | --- | --- |
| Correctness | Unused-code scan of every function, class and constant in `src/`; unused-import scan of `src/`, `tests/` and `scripts/` | Unused code removed (below); no TODOs, debug shortcuts, print debugging or fake data in `src/`. Test doubles live only in `tests/`. |
| Source-disk safety | See above | No write path to the source; confirmed on hardware |
| Reliability | Phase 6 tests (disconnect, disk full, access denied, cancel, invalid names, handle leaks) and repeated full runs | All pass. One unidentified failure in 1 of 10 full test runs today (named below) |
| UI usability | Screenshots in light/dark themes; GUI tests; the real-SSD GUI test | Workflow is drive list → Open → browse → Copy Selected → result; errors shown in plain language. No changes made in this audit. |
| Copy integrity | Size + SHA-256 per file before the final rename; independent re-reads on the SSD | Every verified copy matched; mismatches are never kept under the real name (tested) |
| Dependency compatibility | Pinned: PySide6-Essentials 6.12.0, libfsapfs-python 20261030, PyInstaller 6.22.3, pytest 9.2.0; the build stops on any mismatch | Tested only with Python 3.14.7 x64. `pyproject.toml` allows Python 3.12+, but 3.12/3.13 were not tested |
| Windows 11 compatibility | Windows 11 build 26300 x64; Windows 11 style; long paths with or without the LongPathsEnabled setting | Works. Windows 10 is allowed by the installer (10.0.17763+) but untested |
| Installer quality | Per-user silent install → self-test and package check on the installed copy → silent uninstall | Installed 70 files and 5 Start menu entries; uninstall removed the folder, shortcuts and registry entry. This was done with a Phase 7 build of the same installer script; the final build's installer was compiled and hashed but not installed again |
| Performance | Raw vs app throughput on the SSD; 50,000-item lists; 20,000-file copy plans; small-file copy rate | The app reads at about 95% of the drive's speed (the drive's USB link limits it); 50,000 entries load and sort in under 0.3 s; about 170–240 small files/s including flush and verification |
| Error handling | Backend unavailable, missing reader DLL in the package, drive unplugged, damaged container, encrypted volume, destination full, gone, denied or FAT32, invalid names | Each has a test and a plain-language message; native crashes are logged to `crash.log` |

### Removed in this audit

- `browser/` (`FileBrowserService`): a Phase 1 navigation layer the app never used.
- `safety.resolve_destination_path`: superseded by `copying.Destination`.
- `disks.apfs_signature.compute_block_checksum`: only tests used it; a reference
  implementation stays in the tests.
- `NotYetImplementedError`, `BrowserError`, `CopyError`: unused exception classes,
  including a placeholder "later phase" error.
- `FileListModel.entry_at`: unused.
- The tests that only exercised the removed code. Model tests moved to
  `tests/test_models.py`.

### Fixed or added in this audit

- `pyproject.toml` now ships the icon files as package data; a wheel install had no icon.
- The build and report scripts run pytest in a private temp folder. Earlier runs as
  administrator had left administrator-owned files in pytest's shared folder, which
  broke later normal runs.
- `tests/test_live_ssd.py`: the opt-in GUI test on the real SSD, which deletes its copies
  afterwards. Copies of the owner's files left in temp folders by earlier elevated runs
  were deleted.
- `.vscode/` launch, settings and tasks; README guide sections 1–8.

## Test results (this audit)

| Run | Result |
| --- | --- |
| Full suite, normal user | 430 passed, 2 skipped (the two opt-in live tests); **1 unidentified failure in 1 of 10 runs**. It was not reproduced in the next 7 runs, and its name was lost (only the summary line was kept). The suspect is a timing-based responsiveness test whose measured pause varied from 0.33 to 0.60 s against a 1.0 s limit under load; this is not confirmed |
| Full suite as administrator, live tests on (`TEST_REPORT.md`) | 431 passed, 0 failed, 1 skipped (a test that only applies without administrator rights) |
| Packaged build check (stripped PATH, copy outside the project) | 7 of 7: self-test, self-contained (no DLLs from outside the app or Windows), APFS images opened, copied and verified, window opens and closes, no foreign DLLs in the running app, missing reader reported cleanly, window still opens without the reader |
| Hardware test, packaged final build, real SSD | 13 passed, 0 failed, 1 not applicable; 0 writes |
| GUI test, real SSD | Passed: browsed into a folder and back; copied a folder (17 files) and 3 files, 36.3 MB, all re-verified; 0 writes |
| Requirement coverage (`TEST_REPORT.md`) | 19 of 20 passed; #12 (unplug during a copy) still needs the physical SSD |

Build `dist\BUILD-MANIFEST.txt` (SHA-256):
- `3f3b0e74ee1c30fab64d1fd736fe379a10cffd0bcd2e5bb765aa0b014956608a` portable zip
- `00af75f02ff5957a8e314461465ccb1481e43ad5db8e52901c57dd9e014ef77b` installer
- `4f94d955791b9381e84fe610d45129c468f56e5972da2924147754912596bf2a` `APFS Explorer Lite.exe`
- `93ff917094bb4509f6c66d7599cb61c5028dd7536cb2534d980f03f119c36308` `APFS Explorer Lite Tools.exe`

## Not verified, and open observations

- **Unplugging the SSD during a copy** on real hardware (requirement 12). It's an
  optional step: run **Run hardware test.bat** and answer Y.
- **The packaged app's window with the SSD.** Open `dist\APFS Explorer Lite\APFS
  Explorer Lite.exe`, click Restart as Administrator, open the drive and copy a folder.
- **Windows-incompatible names on the real SSD:** the drive has none.
- **Installer:** "install for all users" and upgrading over an older version were not
  tested; the final build's installer was not installed again.
- **Platforms:** Windows 10, and Python 3.12/3.13, were not tested.
- **Intermittent issues:**
  - One unidentified test failure in 10 full runs (above).
  - During Phase 7, the packaged window ignored an automated close request 3 times in
    about 14 launches. It was not reproduced in 52 later launches, and the cause is
    unknown. The package check fails the build if it recurs.
- **Privacy:** two files from earlier phases still contain file names from the SSD,
  and the owner has not yet decided whether to delete them:
  - `drive-report.txt` in the project folder (written before the redaction fix);
  - one line in the app log.
