"""Run the test suite and write TEST_REPORT.md (Phase 6 requirements 1-20).

    python scripts/make_test_report.py [--live] [--hardware hardware-results.json]

Each requirement is shown with its evidence kept apart: mock (simulated data),
real APFS image, real Windows behaviour, and the physical SSD. Physical-SSD
results come only from a hardware-results.json written by
``python -m apfs_explorer_lite.hwtest`` on the real drive (not a dry run).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import platform
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from apfs_explorer_lite import __version__  # noqa: E402
from tests.requirements_map import LOGGING_TESTS, REQUIREMENTS, TEST_MAP  # noqa: E402

KINDS = ("mock", "image", "os")
KIND_TITLES = {"mock": "Mock / simulated", "image": "Real APFS image", "os": "Real Windows", "hardware": "Physical SSD"}

DEFECTS = [
    ("Private file and folder names were written to the app log (found in the real log on this PC: "
     "one copied file's full path).",
     "Structured JSON log; names replaced by per-session keyed ids plus extension and depth; "
     "a safety net scrubs paths and quoted names from any message or exception."),
    ("Copying many small files sent one progress update per file (3,000 files: 3,002 updates), "
     "flooding the window's event queue.",
     "Progress is throttled to 10 per second, except for the start of copying and the final update."),
    ("A destination where Windows refuses to create files produced one failure per file with a "
     "misleading \"may be open in another program\" message; a fully denied folder was reported as "
     "\"does not exist\".",
     "A write check before the copy stops with one clear message; denied folders say so."),
    ("Save Report crashed (UnicodeEncodeError, nothing shown) when a name contained an invalid "
     "character (lone UTF-16 surrogate).",
     "Such characters are written escaped; a per-file name error fails only that file."),
    ("A folder with 30,000 entries froze the window for 2.8 s while it was displayed (sorting called "
     "Python once per comparison, about 212,000 times).",
     "The list model sorts with precomputed keys: the longest pause is now about 0.3 s with the window "
     "shown, and the selection survives re-sorting."),
    ("The diagnostic report (drive-report.txt) listed up to 25 file names from the drive's root folder.",
     "It now prints counts only."),
]
HARDENING = [
    "Files over 4 GB are failed individually with a clear message when the destination is FAT32 "
    "(instead of a misleading \"destination is full\" stop).",
    "Windows' per-disk I/O counters (read-only query) are used by the hardware test to show that no "
    "writes reached the SSD.",
]
LIMITATIONS = [
    "A truly full destination disk is simulated (free-space check and an injected \"disk full\" "
    "error); no real volume was filled.",
    "Large-file copying on real APFS data needs the physical SSD: no public APFS test image holds a "
    "large file. A 200 MB generated stream checks the streaming and memory use.",
    "Folders with tens of thousands of entries were tested with mock data; real-drive listing speed is "
    "measured by the hardware test.",
    "Copy verification reads the copy back through Windows; it may be served from Windows' cache, so "
    "it confirms what Windows stored, not the destination disk's surface.",
    "Encrypted APFS volumes, Fusion drives and snapshots are not supported (shown as such).",
]


class Collector:
    def __init__(self) -> None:
        self.results: dict[str, dict] = {}

    def pytest_runtest_logreport(self, report) -> None:  # noqa: ANN001
        entry = self.results.setdefault(report.nodeid, {"outcome": "passed", "metrics": []})
        if report.failed:
            entry["outcome"] = "failed"
        elif report.skipped and entry["outcome"] != "failed":
            entry["outcome"] = "skipped"
            entry["skip_reason"] = str(report.longrepr[-1]) if isinstance(report.longrepr, tuple) else ""
        if report.when == "call":
            entry["metrics"] += [v for k, v in report.user_properties if k == "metric"]


def key_of(nodeid: str) -> str:
    name = nodeid.split("[", 1)[0]
    return name.removeprefix("tests/")


def evidence(results: dict[str, dict]) -> dict[int, dict[str, list[tuple[str, str]]]]:
    by_req: dict[int, dict[str, list[tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for nodeid, result in results.items():
        key = key_of(nodeid)
        seen: set[tuple[int, str]] = set()
        for pattern, kind, reqs in TEST_MAP:
            if fnmatch.fnmatch(key, pattern):
                for req in reqs:
                    if (req, kind) not in seen:
                        seen.add((req, kind))
                        by_req[req][kind].append((nodeid, result["outcome"]))
    return by_req


def cell(items: list[tuple[str, str]]) -> str:
    if not items:
        return "—"
    passed = sum(o == "passed" for _, o in items)
    failed = sum(o == "failed" for _, o in items)
    if failed:
        return f"✖ {failed} failed / {len(items)}"
    if passed:
        return f"✔ {passed}" + (f" ({len(items) - passed} skipped)" if passed < len(items) else "")
    return f"skipped ({len(items)})"


def load_hardware(path: Path) -> tuple[dict | None, str]:
    if not path.exists():
        return None, "not run"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("source_kind") != "physical":
        return None, "the results file is from a dry run on an image, not the SSD"
    return data, f"run {data.get('started', '?')}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="include the read-only live disk scan test")
    parser.add_argument("--hardware", type=Path, default=ROOT / "hardware-results.json")
    parser.add_argument("--out", type=Path, default=ROOT / "TEST_REPORT.md")
    args = parser.parse_args()
    if args.live:
        os.environ["APFS_LIVE_DISK_TESTS"] = "1"

    collector = Collector()
    # A fresh temp folder per run, so a run as administrator leaves nothing in pytest's shared one.
    basetemp = Path(tempfile.mkdtemp(prefix="apfs-report-")) / "pytest"
    exit_code = pytest.main(["-q", "-p", "no:cacheprovider", f"--basetemp={basetemp}", str(ROOT / "tests")],
                            plugins=[collector])  # fmt: skip
    results = collector.results
    by_req = evidence(results)
    hardware, hardware_note = load_hardware(args.hardware)
    hw_by_req: dict[int, list[dict]] = defaultdict(list)
    for check in (hardware or {}).get("checks", []):
        for req in check["reqs"]:
            hw_by_req[req].append(check)

    counts = defaultdict(int)
    for r in results.values():
        counts[r["outcome"]] += 1

    rows = []
    overall: dict[int, str] = {}
    for req in REQUIREMENTS:
        auto = by_req.get(req.number, {})
        hw = hw_by_req.get(req.number, [])
        auto_items = [i for kind in KINDS for i in auto.get(kind, [])]
        auto_failed = any(o == "failed" for _, o in auto_items)
        auto_passed = any(o == "passed" for _, o in auto_items)
        hw_tests = auto.get("hardware", [])  # e.g. the live GUI test on the SSD
        hw_failed = any(c["status"] == "failed" for c in hw) or any(o == "failed" for _, o in hw_tests)
        hw_passed = any(c["status"] == "passed" for c in hw) or any(o == "passed" for _, o in hw_tests)
        if auto_failed or hw_failed:
            status = "Failed"
        elif hw_passed:
            status = "Passed"
        elif req.needs_hardware:
            status = "Requires physical SSD"
        elif auto_passed:
            status = "Passed"
        else:
            status = "Not tested"
        overall[req.number] = status
        hw_parts = []
        if hw:
            hw_parts.append("hwtest " + ", ".join(sorted({c["status"].replace("_", " ") for c in hw})))
        if any(o != "skipped" for _, o in hw_tests):
            hw_parts.append("GUI " + cell(hw_tests))
        hw_cell = "; ".join(hw_parts) or ("needed" if req.needs_hardware else "—")
        rows.append(f"| {req.number} | {req.title} | " + " | ".join(cell(auto.get(k, [])) for k in KINDS)
                    + f" | {hw_cell} | **{status}** |")  # fmt: skip

    hardware_required = [r.number for r in REQUIREMENTS if r.needs_hardware]
    ready = all(overall[n] == "Passed" for n in hardware_required) and not any(s == "Failed" for s in overall.values())
    summary = defaultdict(int)
    for status in overall.values():
        summary[status] += 1

    logging_items = [(n, r["outcome"]) for n, r in results.items() if fnmatch.fnmatch(key_of(n), LOGGING_TESTS)]
    mapped = {n for req in by_req.values() for items in req.values() for n, _ in items}
    metrics = [(n, m) for n, r in results.items() for m in r["metrics"]]

    lines = [
        "# APFS Explorer Lite — Phase 6 test report",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `scripts/make_test_report.py` · version {__version__} · "
        f"Python {platform.python_version()} · {platform.platform()}",
        "",
        "## Verdict",
        "",
        ("**All requirements passed, including the physical-SSD checks.** Review the physical-SSD details "
         "below before relying on it." if ready else
         "**Not production-ready.** "
         + ("Automated tests pass, but the hardware test has not been run on the real drive ("
            + hardware_note + "). " if hardware is None else
            f"The hardware test on the real drive passed its checks ({hardware_note}), but these "
            "requirements still lack physical-SSD confirmation: "
            + ", ".join(f"{r.number} ({r.title})" for r in REQUIREMENTS
                        if overall[r.number] == "Requires physical SSD") + ". ")
         + ("Some requirements failed; see below." if summary["Failed"] else "")),
        "",
        f"Requirements: **{summary['Passed']} passed**, **{summary['Failed']} failed**, "
        f"**{summary['Not tested']} not tested**, **{summary['Requires physical SSD']} require the physical SSD**.  ",
        f"Automated tests: {counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped "
        f"(pytest exit code {int(exit_code)}).",
        "",
        "## Requirements",
        "",
        "Evidence columns count passing automated tests of each kind. *Mock / simulated* uses in-memory "
        "data or injected faults and proves logic only; *Real APFS image* reads macOS-created APFS images "
        "through the real reader; *Real Windows* exercises real NTFS, permissions, handles and disk APIs on "
        "this PC; *Physical SSD* comes only from runs on the real drive: the packaged hardware test "
        "(`hwtest`) and the opt-in GUI test `tests/test_live_ssd.py` (`GUI`), both run as administrator.",
        "",
        "| # | Requirement | " + " | ".join(KIND_TITLES[k] for k in KINDS) + " | Physical SSD | Result |",
        "| --- | --- | " + " | ".join("---" for _ in KINDS) + " | --- | --- |",
        *rows,
        "",
    ]
    notes = [f"- **{r.number}. {r.title}:** {r.note}" for r in REQUIREMENTS if r.note]
    if notes:
        lines += ["Notes:", "", *notes, ""]

    lines += ["## Physical SSD (hardware test)", ""]
    if hardware is None:
        lines += [
            f"Not run ({hardware_note}). To run it: connect the SSD, double-click **Run hardware test.bat**, "
            "allow administrator access, then run this report again.",
            "",
        ]
    else:
        lines += [f"{hardware['tool']}, {hardware_note}.", "", "| Check | Requirements | Result | Details |",
                  "| --- | --- | --- | --- |"]  # fmt: skip
        for c in hardware["checks"]:
            lines.append(f"| {c['name']} | {', '.join(map(str, c['reqs']))} | {c['status'].replace('_', ' ')} | {c['detail']} |")
        lines.append("")

    lines += ["## Measurements", ""]
    lines += [f"- {m} (`{key_of(n)}`)" for n, m in metrics] or ["- none recorded"]
    lines += ["", "## Logging privacy", "",
              f"{sum(o == 'passed' for _, o in logging_items)} of {len(logging_items)} tests passed "
              "(`tests/test_logging_privacy.py`). The log is JSON lines in "
              "`%LOCALAPPDATA%\\APFS Explorer Lite\\logs`; file and folder names appear only as `path_id` values "
              "(keyed hash, new key each run), with extension and depth; file contents are never logged. "
              "Set `APFS_EXPLORER_LOG_NAMES=1` to log real names for troubleshooting.", ""]  # fmt: skip
    lines += ["## Defects found and fixed in Phase 6", "", "| # | Defect (confirmed before fixing) | Fix |", "| --- | --- | --- |"]
    lines += [f"| {i} | {d} | {f} |" for i, (d, f) in enumerate(DEFECTS, 1)]
    lines += ["", "Hardening without a confirmed failure:", "", *[f"- {h}" for h in HARDENING], ""]
    lines += ["## Known limitations", "", *[f"- {item}" for item in LIMITATIONS], ""]
    failed = [n for n, r in results.items() if r["outcome"] == "failed"]
    lines += ["## Failed tests", ""] + ([f"- `{n}`" for n in failed] or ["None."]) + [""]
    skipped = [(n, r.get("skip_reason", "")) for n, r in results.items() if r["outcome"] == "skipped"]
    lines += ["## Skipped tests", ""] + ([f"- `{n}`: {why}" for n, why in skipped] or ["None."]) + [""]
    lines += [f"Other regression tests not mapped to a requirement: {len(results) - len(mapped)} "
              f"({sum(1 for n, r in results.items() if n not in mapped and r['outcome'] == 'passed')} passed).", ""]  # fmt: skip

    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {args.out}")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
