from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from apfs_explorer_lite.models import SourceDevice, normalize_apfs_path


# --- models -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", "/"), ("a/b", "/a/b"), ("/a/./b/../c", "/a/c"), ("/../../x", "/x"), ("/a/b/", "/a/b")],
)
def test_normalize_apfs_path(raw: str, expected: str) -> None:
    assert normalize_apfs_path(raw) == PurePosixPath(expected)


def test_source_device_validates_range() -> None:
    with pytest.raises(ValueError):
        SourceDevice("x", offset=-1, size=10)
    with pytest.raises(ValueError):
        SourceDevice("x", offset=0, size=0)
