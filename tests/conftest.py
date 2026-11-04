from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Must be set before QApplication is created so tests never open real windows.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
