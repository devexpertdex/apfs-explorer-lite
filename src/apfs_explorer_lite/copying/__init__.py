"""One-way copy from an APFS volume (read-only) to a folder on this PC."""

from __future__ import annotations

from .destination import PARTIAL_SUFFIX, Destination
from .job import (
    Conflict,
    ConflictChoice,
    ConflictDecision,
    ConflictPolicy,
    ConflictResolver,
    CopyJob,
    CopyProgress,
    Phase,
)
from .plan import CopyPlan, ItemResult, Outcome, PlannedItem, build_plan
from .report import CopyReport, JobStatus

__all__ = [
    "PARTIAL_SUFFIX",
    "Conflict",
    "ConflictChoice",
    "ConflictDecision",
    "ConflictPolicy",
    "ConflictResolver",
    "CopyJob",
    "CopyPlan",
    "CopyProgress",
    "CopyReport",
    "Destination",
    "ItemResult",
    "JobStatus",
    "Outcome",
    "Phase",
    "PlannedItem",
    "build_plan",
]
