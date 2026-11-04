"""Human-readable sizes and durations (no Qt dependency)."""

from __future__ import annotations

_UNITS = ("bytes", "KB", "MB", "GB", "TB", "PB")


def format_size(size: int) -> str:
    """Decimal units, matching how drive capacities are printed on packaging."""
    value = float(size)
    for unit in _UNITS:
        # Compare the rounded value so 999.96 GB becomes "1.0 TB", not "1000.0 GB".
        if round(value, 1) < 1000 or unit == _UNITS[-1]:
            return f"{size} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1000
    raise AssertionError("unreachable")


def format_duration(seconds: float) -> str:
    """Rough remaining time: "about 5 seconds", "about 3 minutes", "about 2 hours"."""
    if seconds < 60:
        value, unit = max(1, round(seconds / 5) * 5 if seconds > 10 else round(seconds)), "second"
    elif seconds < 3600:
        value, unit = round(seconds / 60), "minute"
    else:
        value, unit = round(seconds / 3600, 1), "hour"
        if value == int(value):
            value = int(value)
    return f"about {value} {unit}{'' if value == 1 else 's'}"
