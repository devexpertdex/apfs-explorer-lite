# APFS Explorer Lite — Phase 6 test report

Generated 2026-11-04 22:28 by `scripts/make_test_report.py` · version 0.1.0 · Python 3.14.7 · Windows-11-10.0.26300-SP0

## Verdict

**Not production-ready.** The hardware test on the real drive passed its checks (run 2026-11-04T22:23:37+11:00), but these requirements still lack physical-SSD confirmation: 12 (SSD disconnection during copying). 

Requirements: **19 passed**, **0 failed**, **0 not tested**, **1 require the physical SSD**.  
Automated tests: 431 passed, 0 failed, 1 skipped (pytest exit code 0).

## Requirements

Evidence columns count passing automated tests of each kind. *Mock / simulated* uses in-memory data or injected faults and proves logic only; *Real APFS image* reads macOS-created APFS images through the real reader; *Real Windows* exercises real NTFS, permissions, handles and disk APIs on this PC; *Physical SSD* comes only from runs on the real drive: the packaged hardware test (`hwtest`) and the opt-in GUI test `tests/test_live_ssd.py` (`GUI`), both run as administrator.

| # | Requirement | Mock / simulated | Real APFS image | Real Windows | Physical SSD | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | External APFS SSD detection | ✔ 14 | — | ✔ 1 | hwtest passed; GUI ✔ 1 | **Passed** |
| 2 | Correct APFS partition identification | ✔ 19 | ✔ 9 | — | hwtest passed; GUI ✔ 1 | **Passed** |
| 3 | Read-only source access | ✔ 23 | ✔ 3 | ✔ 5 | hwtest passed; GUI ✔ 1 | **Passed** |
| 4 | Real directory enumeration | — | ✔ 19 | — | hwtest passed; GUI ✔ 1 | **Passed** |
| 5 | Small file copying | — | ✔ 5 | — | hwtest passed; GUI ✔ 1 | **Passed** |
| 6 | Large file copying | ✔ 1 | ✔ 4 | — | hwtest passed | **Passed** |
| 7 | Complete folder copying | — | ✔ 5 | — | hwtest passed; GUI ✔ 1 | **Passed** |
| 8 | Nested folder copying | — | ✔ 7 | ✔ 1 | hwtest passed | **Passed** |
| 9 | Files with Unicode names | ✔ 2 | — | ✔ 7 | hwtest passed | **Passed** |
| 10 | Windows-incompatible filenames | ✔ 24 | — | ✔ 2 | hwtest not tested | **Passed** |
| 11 | Duplicate destination filenames | ✔ 5 | ✔ 8 | ✔ 10 | hwtest passed | **Passed** |
| 12 | SSD disconnection during copying | — | ✔ 6 | — | needed | **Requires physical SSD** |
| 13 | Insufficient destination storage | — | ✔ 3 | ✔ 1 | — | **Passed** |
| 14 | Access-denied errors | ✔ 2 | ✔ 1 | ✔ 5 (1 skipped) | — | **Passed** |
| 15 | Application cancellation | ✔ 3 | ✔ 4 | — | hwtest passed | **Passed** |
| 16 | Copy integrity verification | — | ✔ 17 | — | hwtest not tested, passed; GUI ✔ 1 | **Passed** |
| 17 | Large directory performance | ✔ 5 | ✔ 1 | ✔ 1 | hwtest passed | **Passed** |
| 18 | GUI responsiveness | ✔ 5 | ✔ 1 | — | — | **Passed** |
| 19 | Resource cleanup and file-handle closure | ✔ 2 | ✔ 3 | ✔ 4 | hwtest passed | **Passed** |
| 20 | No accidental source writes | ✔ 35 | ✔ 5 | ✔ 5 | hwtest passed; GUI ✔ 1 | **Passed** |

Notes:

- **6. Large file copying:** No public APFS image contains a large file.
- **12. SSD disconnection during copying:** Physical unplug is a manual, optional step.
- **13. Insufficient destination storage:** A real full disk is simulated, not produced.
- **17. Large directory performance:** Real-drive listing speed depends on the SSD and USB link.

## Physical SSD (hardware test)

APFS Explorer Lite 0.1.0 hwtest, run 2026-11-04T22:23:37+11:00.

| Check | Requirements | Result | Details |
| --- | --- | --- | --- |
| Detect the external APFS SSD | 1 | passed | disk 1: SPCC Sol id State Disk, 256.1 GB, USB (external bus) |
| Identify the APFS partition | 2 | passed | partition 1: recoverable, APFS type GUID, 256.1 GB container (first block damaged: read via a backup superblock in memory) |
| Open the APFS container read-only | 3 | passed | opened with read access only; the write-access guard refused a test request before Windows |
| Walk the volume's folders | 4, 17 | passed | 42,669 files and 7,342 folders in 7.6s (6,560 entries/s) - stopped at the test limit |
| Copy 20 small files | 5, 16 | passed | 20 copies match the source (SHA-256), 0.1 MB in 0.4s (0.4 MB/s) |
| Copy a large file (1,806 MB) | 6, 16 | passed | 1 copies match the source (SHA-256), 1806.3 MB in 56.5s (32.0 MB/s) |
| Copy a complete nested folder | 7, 8, 16 | passed | 17 of 17 copies match the source (SHA-256), 34.0 MB in 1.2s |
| Copy files with Unicode names | 9, 16 | passed | 3 copies match the source (SHA-256), 0.6 MB in 0.1s (7.9 MB/s) |
| Copy files with names Windows doesn't allow | 10, 16 | not tested | no Windows-incompatible file names on this drive |
| Copy the same file three times (keep both, then skip) | 11 | passed | second copy kept as "(2)", third skipped; originals untouched |
| Cancel a copy part-way | 15, 19 | passed | status cancelled, 0 files left in the destination |
| Handles released after closing | 19 | passed | +1 open handles compared with the start |
| No temporary files left | 19 | passed | 0 found |
| Windows recorded no writes to the SSD | 20, 3 | passed | 0 writes, 0 bytes written, 3,895 MB read during the test |

## Measurements

- real SSD via GUI: browsed into a folder and back; copied a folder (17 files) and 3 more files, 36,293,490 bytes, all re-verified; 83,623,936 bytes read, 0 written (`test_live_ssd.py::test_browse_and_copy_from_the_real_ssd_through_the_gui`)
- real APFS image: 0.047 ms per directory entry (cached metadata) (`test_performance.py::test_real_image_listing_cost_per_entry`)
- 50,000 entries: load+sort 0.25s, search 0.26s, clear 0.13s (`test_performance.py::test_file_list_model_with_50000_entries`)
- copy plan for 20,000 files in 200 folders: 0.53s (`test_performance.py::test_copy_plan_for_large_tree`)
- 30,000-entry folder (0.5s drive delay): longest UI pause 467 ms (`test_performance.py::test_window_responsive_while_large_folder_loads`)
- during a copy: longest UI pause 13 ms (`test_performance.py::test_window_responsive_during_copy`)
- 2000 small files: 141 files/s (includes flush to disk and read-back) (`test_performance.py::test_many_small_files_copy_rate`)

## Logging privacy

15 of 15 tests passed (`tests/test_logging_privacy.py`). The log is JSON lines in `%LOCALAPPDATA%\APFS Explorer Lite\logs`; file and folder names appear only as `path_id` values (keyed hash, new key each run), with extension and depth; file contents are never logged. Set `APFS_EXPLORER_LOG_NAMES=1` to log real names for troubleshooting.

## Defects found and fixed in Phase 6

| # | Defect (confirmed before fixing) | Fix |
| --- | --- | --- |
| 1 | Private file and folder names were written to the app log (found in the real log on this PC: one copied file's full path). | Structured JSON log; names replaced by per-session keyed ids plus extension and depth; a safety net scrubs paths and quoted names from any message or exception. |
| 2 | Copying many small files sent one progress update per file (3,000 files: 3,002 updates), flooding the window's event queue. | Progress is throttled to 10 per second, except for the start of copying and the final update. |
| 3 | A destination where Windows refuses to create files produced one failure per file with a misleading "may be open in another program" message; a fully denied folder was reported as "does not exist". | A write check before the copy stops with one clear message; denied folders say so. |
| 4 | Save Report crashed (UnicodeEncodeError, nothing shown) when a name contained an invalid character (lone UTF-16 surrogate). | Such characters are written escaped; a per-file name error fails only that file. |
| 5 | A folder with 30,000 entries froze the window for 2.8 s while it was displayed (sorting called Python once per comparison, about 212,000 times). | The list model sorts with precomputed keys: the longest pause is now about 0.3 s with the window shown, and the selection survives re-sorting. |
| 6 | The diagnostic report (drive-report.txt) listed up to 25 file names from the drive's root folder. | It now prints counts only. |

Hardening without a confirmed failure:

- Files over 4 GB are failed individually with a clear message when the destination is FAT32 (instead of a misleading "destination is full" stop).
- Windows' per-disk I/O counters (read-only query) are used by the hardware test to show that no writes reached the SSD.

## Known limitations

- A truly full destination disk is simulated (free-space check and an injected "disk full" error); no real volume was filled.
- Large-file copying on real APFS data needs the physical SSD: no public APFS test image holds a large file. A 200 MB generated stream checks the streaming and memory use.
- Folders with tens of thousands of entries were tested with mock data; real-drive listing speed is measured by the hardware test.
- Copy verification reads the copy back through Windows; it may be served from Windows' cache, so it confirms what Windows stored, not the destination disk's surface.
- Encrypted APFS volumes, Fusion drives and snapshots are not supported (shown as such).

## Failed tests

None.

## Skipped tests

- `tests/test_reliability.py::test_source_disk_without_admin_is_access_denied`: Skipped: running as administrator

Other regression tests not mapped to a requirement: 210 (210 passed).
