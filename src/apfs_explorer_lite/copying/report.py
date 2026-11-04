"""What a copy job did, per file, in a form the GUI and a saved text report share."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import APP_NAME, __version__
from ..formatting import format_size
from .plan import ItemResult, Outcome


class JobStatus(enum.Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"  # ended early by an error (drive lost, destination full, ...)


@dataclass(slots=True)
class CopyReport:
    destination_root: Path
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    status: JobStatus = JobStatus.STOPPED
    stop_reason: str | None = None
    results: list[ItemResult] = field(default_factory=list)

    def _files(self, outcome: Outcome) -> list[ItemResult]:
        return [r for r in self.results if r.outcome is outcome and not r.is_folder]

    @property
    def copied(self) -> list[ItemResult]:
        return self._files(Outcome.COPIED)

    @property
    def skipped(self) -> list[ItemResult]:
        return [r for r in self.results if r.outcome is Outcome.SKIPPED]

    @property
    def failed(self) -> list[ItemResult]:
        return [r for r in self.results if r.outcome is Outcome.FAILED]

    @property
    def problems(self) -> list[ItemResult]:
        """Skipped and failed items, failures first."""
        return self.failed + self.skipped

    @property
    def bytes_copied(self) -> int:
        return sum(r.size for r in self.copied)

    def summary_line(self) -> str:
        return (
            f"{self.status.value}: {len(self.copied)} copied and verified ({format_size(self.bytes_copied)}), "
            f"{len(self.skipped)} skipped, {len(self.failed)} failed"
        )

    def as_text(self) -> str:
        local = lambda moment: moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        lines = [
            f"{APP_NAME} {__version__} copy report",
            f"Destination : {self.destination_root}",
            f"Started     : {local(self.started_at)}",
            f"Finished    : {local(self.finished_at) if self.finished_at else '-'}",
            f"Result      : {self.summary_line()}",
        ]
        if self.stop_reason:
            lines.append(f"Reason      : {self.stop_reason}")
        lines.append("Each copied file was read back and matched the original's size and SHA-256.")
        for title, items in (("FAILED", self.failed), ("SKIPPED", self.skipped)):
            if items:
                lines += ["", f"{title} ({len(items)})"]
                lines += [f"  {r.source}  —  {r.message}" for r in items]
        if self.copied:
            lines += ["", f"COPIED ({len(self.copied)})  SHA-256, size, source -> destination"]
            lines += [
                f"  {r.sha256}  {r.size:>14,}  {r.source} -> {r.destination}"
                + (f"  ({r.message})" if r.message else "")
                for r in self.copied
            ]
        return "\n".join(lines) + "\n"
