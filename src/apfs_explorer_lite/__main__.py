"""Entry point for ``python -m apfs_explorer_lite`` and the PyInstaller build."""

from __future__ import annotations

import sys


def _fatal(message: str) -> int:
    """Show a message box when the GUI itself cannot start (no console in a windowed build)."""
    if sys.platform == "win32":
        import ctypes

        ctypes.WinDLL("user32").MessageBoxW(None, message, "APFS Explorer Lite", 0x10)  # MB_ICONERROR
    else:
        print(message, file=sys.stderr)
    return 1


def run() -> int:
    try:
        # Absolute import so this file also works as a PyInstaller entry script.
        from apfs_explorer_lite.app import main
    except ImportError as exc:
        return _fatal(
            "APFS Explorer Lite could not start because part of the program is missing or "
            f"blocked:\n\n{exc}\n\nReinstall or re-extract the app. Your drive was not touched."
        )
    return main()


if __name__ == "__main__":
    sys.exit(run())
