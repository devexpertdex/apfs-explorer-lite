"""Structured (JSON lines) logging that keeps private data out of the log.

* File contents are never logged.
* File and folder names are logged only as a short keyed hash (``path_id``)
  plus extension and depth, so one file can be followed through a session
  without revealing its name. The key is random per run, so ids cannot be
  matched across runs or looked up from a list of common names.
* As a safety net, the formatter replaces anything in a message or exception
  text that looks like a Windows or APFS path. Traceback frames (source code
  locations of this program) are kept.

Set ``APFS_EXPLORER_LOG_NAMES=1`` to log real names, for troubleshooting only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import traceback
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

LOG_NAMES_ENV = "APFS_EXPLORER_LOG_NAMES"
_SESSION_KEY = secrets.token_bytes(16)
_FIELDS_ATTR = "fields"

# Device paths say which disk is used and are needed for support; they are not personal.
_DEVICE_PATH = re.compile(r"^\\\\\.\\PhysicalDrive\d+$", re.IGNORECASE)
# Paths may contain spaces, so a match runs to the next quote, line end or ": ".
_PATH_END = r"(?=$|['\"\n<>|“”‘’]|: )"
_WINDOWS_PATH = re.compile(
    r"(?:\\\\\?\\(?:UNC\\)?|\\\\(?!\.\\PhysicalDrive)|\b[A-Za-z]:[\\/])[^'\"\n<>|]*?" + _PATH_END
)
_POSIX_PATH = re.compile(r"(?<![\w.:/\\])/(?![\s/])[^'\"\n<>]*?" + _PATH_END)
# Names quoted in messages ('x', "x", “x”, ‘x’).
_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’")


def names_allowed() -> bool:
    return os.environ.get(LOG_NAMES_ENV) == "1"


def path_id(path: str | PurePath) -> str:
    text = str(path)
    if names_allowed() or _DEVICE_PATH.match(text):
        return text
    digest = hmac.new(_SESSION_KEY, text.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
    return f"p-{digest[:10]}"


def path_fields(path: str | PurePath, prefix: str = "path") -> dict[str, Any]:
    """Loggable description of a path: id, extension and depth, never the name."""
    text = str(path)
    pure = PurePath(text)
    suffix = pure.suffix.lower()
    fields: dict[str, Any] = {f"{prefix}_id": path_id(text)}
    if suffix and len(suffix) <= 10:
        fields[f"{prefix}_ext"] = suffix
    fields[f"{prefix}_depth"] = max(0, len(pure.parts) - 1)
    return fields


def scrub_text(text: str) -> str:
    """Replace path-like and quoted substrings with ids (no-op when names are allowed).

    Over-redaction is accepted: this is the safety net behind logging ids instead of names.
    """
    if names_allowed():
        return text

    def replace(match: re.Match[str]) -> str:
        found = match.group(0)
        return found if _DEVICE_PATH.match(found.strip("'\"")) else f"<{path_id(found)}>"

    text = _QUOTED.sub(replace, text)
    text = _WINDOWS_PATH.sub(replace, text)
    return _POSIX_PATH.sub(replace, text)


def error_fields(exc: BaseException) -> dict[str, Any]:
    """Error type and codes, without the message (which may contain names)."""
    fields: dict[str, Any] = {"error": type(exc).__name__}
    for attr in ("winerror", "errno"):
        value = getattr(exc, attr, None)
        if value is not None:
            fields[attr] = value
    user_message = getattr(exc, "user_message", None)
    if user_message:
        fields["error_message"] = scrub_text(user_message)
    return fields


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Log ``event`` (a fixed, name-free string) with structured ``fields``."""
    if logger.isEnabledFor(level):
        logger.log(level, event, extra={_FIELDS_ATTR: fields}, stacklevel=2)


def _format_exception(exc_info: Any) -> dict[str, Any]:
    exc_type, exc, tb = exc_info
    frames = [f"{PurePath(f.filename).name}:{f.lineno} in {f.name}" for f in traceback.extract_tb(tb)]
    result: dict[str, Any] = {"type": exc_type.__name__, "message": scrub_text(str(exc)), "frames": frames}
    cause = (exc.__cause__ or exc.__context__) if exc is not None else None
    if cause is not None:
        result["cause"] = {"type": type(cause).__name__, "message": scrub_text(str(cause))}
    return result


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, event, fields..., exception."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": scrub_text(record.getMessage()),
        }
        fields = getattr(record, _FIELDS_ATTR, None)
        if fields:
            for key, value in fields.items():
                payload[key] = scrub_text(value) if isinstance(value, str) and not key.endswith("_id") else value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = _format_exception(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, default=str)
