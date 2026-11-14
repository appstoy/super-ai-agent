"""Durable Run lease, heartbeat, cancellation, and recovery operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from super_agent.storage.models import RunRecord, TaskRecord, WorkerRecord


class RunLeaseError(RuntimeError):
    """Raised when a Run cannot be claimed or heartbeated by its owner."""


@dataclass(frozen=True, slots=True)
class RecoveredRun:
    """Summary of one Run moved to a terminal recovery state."""

    run_id: UUID
    status: str
    reason: str


class RunLeaseService:
    """Keep long-running Runs observable and safe to recover after a crash."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def claim(self, run_id: UUID, owner: str, *, lease_seconds: float = 60.0) -> RunRecord:
        """Claim a pending or expired Run for one worker owner."""

        if not owner:
            raise ValueError("owner must not be empty")
        self._validate_lease(lease_seconds)
        run = self._get(run_id)
        now = datetime.now(UTC)
        lease_expires_at = self._as_utc(run.lease_expires_at)
        active = run.status == "running" and lease_expires_at is not None
        another_owner = run.lease_owner not in {None, owner}
        if (
            active
            and lease_expires_at is not None
            and lease_expires_at > now
            and another_owner
        ):
            raise RunLeaseError(f"run {run_id} is leased by another owner")
        if run.status not in {"pending", "running"}:
            raise RunLeaseError(f"run {run_id} is already terminal: {run.status}")
        run.status = "running"
        run.lease_owner = owner
        run.started_at = run.started_at or now
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        run.version += 1
        self._session.flush()
        return run

    def heartbeat(self, run_id: UUID, owner: str, *, lease_seconds: float = 60.0) -> RunRecord:
        """Renew a Run lease and reject a stale or cancelled worker."""

        self._validate_lease(lease_seconds)
        run = self._get(run_id)
        now = datetime.now(UTC)
        if run.status != "running" or run.lease_owner != owner:
            raise RunLeaseError(f"run {run_id} is not owned by {owner}")
        lease_expires_at = self._as_utc(run.lease_expires_at)
        if lease_expires_at is not None and lease_expires_at <= now:
            raise RunLeaseError(f"run {run_id} lease expired")
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        run.version += 1
        self._session.flush()
        return run

    def request_cancel(self, run_id: UUID) -> RunRecord:
        """Ask a running Run to stop at its next cooperative checkpoint."""

        run = self._get(run_id)
        changed = False
        if run.status == "pending":
            run.status = "cancelled"
            run.finished_at = datetime.now(UTC)
            changed = True
        elif run.status == "running":
            run.status = "cancel_requested"
            changed = True
        if changed:
            run.version += 1
        self._session.flush()
        return run

    def recover_expired(self, *, now: datetime | None = None) -> tuple[RecoveredRun, ...]:
        """Mark Runs with expired leases as timed out for a later retry policy."""

        current = now or datetime.now(UTC)
        rows = self._session.scalars(
            select(RunRecord).where(
                RunRecord.status.in_({"running", "cancel_requested"}),
                RunRecord.lease_expires_at.is_not(None),
            )
        ).all()
        recovered: list[RecoveredRun] = []
        for run in rows:
            lease_expires_at = self._as_utc(run.lease_expires_at)
            if lease_expires_at is None or lease_expires_at > current:
                continue
            target = "cancelled" if run.status == "cancel_requested" else "timed_out"
            reason = (
                "cancel request lease expired"
                if target == "cancelled"
                else "run lease expired"
            )
            run.status = target
            run.error = reason
            run.finished_at = current
            run.lease_owner = None
            run.lease_expires_at = None
            run.version += 1
            task = self._session.get(TaskRecord, run.task_id)
            if task is not None:
                task.status = "cancelled" if target == "cancelled" else "failed"
                task.version += 1
                if task.worker_id is not None:
                    worker = self._session.get(WorkerRecord, task.worker_id)
                    if worker is not None:
                        worker.status = "stopped" if target == "cancelled" else "failed"
                        worker.version += 1
            recovered.append(RecoveredRun(run.id, target, reason))
        self._session.flush()
        return tuple(recovered)

    def _get(self, run_id: UUID) -> RunRecord:
        run = self._session.get(RunRecord, run_id)
        if run is None:
            raise RunLeaseError(f"run not found: {run_id}")
        # A long-running worker may share a Session identity map with an
        # operator request that changed this row in another transaction.
        self._session.refresh(run)
        return run

    @staticmethod
    def _validate_lease(lease_seconds: float) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
