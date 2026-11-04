"""Console companion: ``APFS Explorer Lite Tools.exe <command> [options]``.

Gives the packaged build the command-line tools that otherwise need Python.
All of them are read-only towards disks.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

# Absolute imports: PyInstaller runs this file as a top-level script.
from apfs_explorer_lite import APP_NAME, __version__

USAGE = f"""{APP_NAME} Tools {__version__}

Usage: "APFS Explorer Lite Tools.exe" <command> [options]   (add --help after a command)

Commands:
  selftest   check this build: modules, Qt, APFS reader, disk discovery (no admin needed)
  hwtest     read-only hardware test on the external APFS SSD (run as administrator)
  diagnose   read-only drive diagnostic report (run as administrator)
  verify     read-only APFS acceptance check of a disk or image
  version    print the version
"""


def _commands() -> dict[str, Callable[[Sequence[str]], int]]:
    from apfs_explorer_lite import diagnose, hwtest, selftest, verify

    return {"selftest": selftest.main, "hwtest": hwtest.main, "diagnose": diagnose.main, "verify": verify.main}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        # Redirected output uses the PC's code page; never fail on a character it lacks.
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if args[0] in ("version", "--version"):
        print(f"{APP_NAME} {__version__}")
        return 0
    command = _commands().get(args[0])
    if command is None:
        print(f"Unknown command {args[0]!r}.\n\n{USAGE}", file=sys.stderr)
        return 2
    return command(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
