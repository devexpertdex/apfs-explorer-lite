"""Light, dark or system theme (Qt colour-scheme hints, honoured by the Windows 11 style)."""

from __future__ import annotations

import enum

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import QLabel


def banner_label(object_name: str) -> QLabel:
    """Hidden, word-wrapped notice label that follows theme changes.

    Uses palette roles rather than a style sheet, because style-sheet colours
    are fixed when set and would not update on a light/dark switch.
    """
    label = QLabel()
    label.setObjectName(object_name)
    label.setWordWrap(True)
    label.setMargin(8)
    label.setAutoFillBackground(True)
    label.setBackgroundRole(QPalette.ColorRole.AlternateBase)
    label.setForegroundRole(QPalette.ColorRole.Text)
    label.hide()
    return label


class ThemeMode(enum.Enum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"

    @property
    def label(self) -> str:
        return {"system": "Use system setting", "light": "Light", "dark": "Dark"}[self.value]


def apply_theme(mode: ThemeMode) -> None:
    hints = QGuiApplication.styleHints()
    if mode is ThemeMode.SYSTEM:
        hints.unsetColorScheme()
    elif mode is ThemeMode.LIGHT:
        hints.setColorScheme(Qt.ColorScheme.Light)
    else:
        hints.setColorScheme(Qt.ColorScheme.Dark)


def parse_theme(value: object) -> ThemeMode:
    try:
        return ThemeMode(str(value))
    except ValueError:
        return ThemeMode.SYSTEM
