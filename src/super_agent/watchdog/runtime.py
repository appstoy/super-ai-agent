"""Watchdog inspection and recovery for persisted Agent Runs."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from super_agent.application.run_leases import RecoveredRun, RunLeaseService
from super_agent.application.run_recovery import RetryDecision, RunRecoveryService
from super_agent.storage.models import RunRecord
from super_agent.watchdog.rules import FindingKind, WatchdogFinding


class PersistentRunWatchdog:
    """Inspect durable Run health and apply only bounded recovery actions."""

    def __init__(
        self,
        *,
        stalled_after_seconds: float = 300.0,
        failure_threshold: int = 3,
        max_attempts: int = 3,
    ) -> None:
        if stalled_after_seconds <= 0 or failure_threshold < 1:
            raise ValueError("Watchdog thresholds must be positive")
        self._stalled_after = stalled_after_seconds
        self._failure_threshold = failure_threshold
        self._max_attempts = max_attempts

    def inspect(
        self, session: Session, *, checked_at: datetime | None = None
    ) -> tuple[WatchdogFinding, ...]:
        """Return explainable findings without mutating Run state."""

        now = checked_at or datetime.now(UTC)
        findings: list[WatchdogFinding] = []
        rows = session.scalars(
            select(RunRecord).where(
                RunRecord.status.in_({"running", "failed", "timed_out"})
            )
        ).all()
        for run in rows:
            if run.status == "running":
                heartbeat = self._as_utc(run.heartbeat_at) or self._as_utc(run.started_at)
                if heartbeat is not None:
                    age = (now - heartbeat).total_seconds()
                    if age > self._stalled_after:
                        findings.append(
                            WatchdogFinding(
                                FindingKind.STALLED_RUN,
                                run.id,
                                f"heartbeat age {age:.1f}s exceeds threshold",
                                "recover expired lease",
                            )
                        )
            if run.status in {"failed", "timed_out"} and run.attempt >= self._failure_threshold:
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

    def recover(
        self, session: Session
    ) -> tuple[tuple[RecoveredRun, ...], tuple[RetryDecision, ...]]:
        """Recover expired leases, then requeue bounded retry candidates."""

        expired = RunLeaseService(session).recover_expired()
        retries = RunRecoveryService(session, max_attempts=self._max_attempts).requeue_retryable()
        return expired, retries

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
