"""Minimal-permission rule Watchdog for leases and stalled runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from super_agent.application.runs import ToolRun
from super_agent.scheduler.memory import Lease


class FindingKind(StrEnum):
    HEALTHY = "healthy"
    EXPIRED_LEASE = "expired_lease"
    STALLED_RUN = "stalled_run"
    REPEATED_FAILURE = "repeated_failure"


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Inputs observed by one Watchdog tick."""

    checked_at: datetime
    leases: tuple[Lease, ...]
    runs: tuple[ToolRun, ...]


@dataclass(frozen=True, slots=True)
class WatchdogFinding:
    """Explainable finding and suggested next action."""

    kind: FindingKind
    subject_id: UUID | None
    evidence: str
    action: str


class Watchdog:
    """Inspect state without mutating unrelated tasks or granting tools."""

    def __init__(self, *, stalled_after_seconds: float = 300.0, failure_threshold: int = 3) -> None:
        if stalled_after_seconds <= 0 or failure_threshold < 1:
            raise ValueError("Watchdog thresholds must be positive")
        self._stalled_after = stalled_after_seconds
        self._failure_threshold = failure_threshold

    def inspect(self, snapshot: HealthSnapshot) -> tuple[WatchdogFinding, ...]:
        """Return findings; healthy snapshots produce one explicit healthy result."""

        findings: list[WatchdogFinding] = []
        now = snapshot.checked_at
        for lease in snapshot.leases:
            if not lease.active(now):
                findings.append(
                    WatchdogFinding(
                        FindingKind.EXPIRED_LEASE,
                        lease.lease_id,
                        f"lease expired at {lease.expires_at.isoformat()}",
                        "reap lease and reschedule task",
                    )
                )
        for run in snapshot.runs:
            if run.status == "RUNNING" and run.started_at is not None:
                age = (now - run.started_at).total_seconds()
                if age > self._stalled_after:
                    findings.append(
                        WatchdogFinding(
                            FindingKind.STALLED_RUN,
                            run.id,
                            f"run age {age:.1f}s exceeds threshold",
                            "request stop or manual inspection",
                        )
                    )
            if run.status == "FAILED" and run.attempt >= self._failure_threshold:
                findings.append(
                    WatchdogFinding(
                        FindingKind.REPEATED_FAILURE,
                        run.id,
                        f"attempt {run.attempt} reached threshold",
                        "pause retries and request human review",
                    )
                )
        if not findings:
            findings.append(
                WatchdogFinding(FindingKind.HEALTHY, None, "no rule violations", "none")
            )
        return tuple(findings)

