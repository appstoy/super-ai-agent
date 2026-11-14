"""Bounded retry coordination for durable failed and timed-out Runs."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from super_agent.storage.models import OutboxMessage, RunRecord, TriggerRecord
from super_agent.storage.outbox import OutboxRepository


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """One persisted Run selected for another attempt."""

    run_id: UUID
    attempt: int
    dedupe_key: str


class RunRecoveryService:
    """Requeue retryable Runs without duplicating their Trigger identity."""

    def __init__(self, session: Session, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._session = session
        self._max_attempts = max_attempts

    def requeue_retryable(self) -> tuple[RetryDecision, ...]:
        """Requeue published Trigger events for failed Runs below the limit."""

        rows = self._session.scalars(
            select(RunRecord)
            .where(RunRecord.status.in_({"failed", "timed_out"}))
            .order_by(RunRecord.attempt, RunRecord.id)
        ).all()
        decisions: list[RetryDecision] = []
        seen_triggers: set[UUID] = set()
        repository = OutboxRepository(self._session)
        for run in rows:
            if run.attempt >= self._max_attempts or run.trigger_id is None:
                continue
            if run.trigger_id in seen_triggers:
                continue
            latest = self._session.scalar(
                select(RunRecord)
                .where(RunRecord.trigger_id == run.trigger_id)
                .order_by(RunRecord.attempt.desc())
                .limit(1)
            )
            if latest is not None and latest.id != run.id:
                # A newer attempt already exists for this Trigger; the older
                # failed Run must not requeue the event again.
                continue
            trigger = self._session.get(TriggerRecord, run.trigger_id)
            if trigger is None:
                continue
            dedupe_key = f"trigger.received:{trigger.dedupe_key}"
            message = self._session.scalar(
                select(OutboxMessage).where(OutboxMessage.dedupe_key == dedupe_key)
            )
            if message is None or message.status != "PUBLISHED":
                continue
            if repository.requeue_published(dedupe_key) is None:
                continue
            seen_triggers.add(run.trigger_id)
            decisions.append(RetryDecision(run.id, run.attempt + 1, dedupe_key))
        self._session.flush()
        return tuple(decisions)
