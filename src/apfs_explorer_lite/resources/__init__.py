"""Files shipped with the app (icon). Works from source and from the PyInstaller build."""

from __future__ import annotations

from pathlib import Path

RESOURCE_DIR = Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    return RESOURCE_DIR / name
