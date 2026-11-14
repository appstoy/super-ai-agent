"""Transactional Trigger ingress service."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from super_agent.domain.entities import Trigger, TriggerFilterDecision
from super_agent.ingress.schemas import TriggerInput
from super_agent.storage.models import TriggerRecord
from super_agent.storage.outbox import OutboxRepository


class TriggerIngressService:
    """Validate, deduplicate, persist, and publish normalized triggers."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ingest(
        self, trigger_input: TriggerInput, *, enqueue_outbox: bool = True
    ) -> Trigger:
        """Persist one Trigger and its Outbox event in the same transaction.

        ``enqueue_outbox=False`` is used for synthetic continuation events that
        are themselves already durable Outbox messages (tool.completed); they
        must not publish a second trigger.received event.
        """

        existing = self._session.scalar(
            select(TriggerRecord).where(TriggerRecord.dedupe_key == trigger_input.dedupe_key)
        )
        if existing is not None:
            return self._to_domain(existing)

        trigger = Trigger(
            schema_version=trigger_input.schema_version,
            source=trigger_input.source,
            tenant_id=trigger_input.tenant_id,
            thread_id=trigger_input.thread_id,
            actor=trigger_input.actor,
            payload_ref=str(trigger_input.payload.get("message", "")),
            occurred_at=trigger_input.occurred_at,
            received_at=datetime.now(UTC),
            dedupe_key=trigger_input.dedupe_key,
            filter_decision=TriggerFilterDecision.PENDING,
        )
        record = TriggerRecord(
            id=trigger.id,
            schema_version=trigger.schema_version,
            source=trigger.source,
            tenant_id=trigger.tenant_id,
            thread_id=trigger.thread_id,
            actor=trigger.actor,
            payload_ref=trigger.payload_ref,
            occurred_at=trigger.occurred_at,
            received_at=trigger.received_at,
            dedupe_key=trigger.dedupe_key,
            filter_decision=trigger.filter_decision.value,
        )
        self._session.add(record)
        if not enqueue_outbox:
            self._session.flush()
            return trigger
        OutboxRepository(self._session).enqueue(
            topic="trigger.received",
            dedupe_key=f"trigger.received:{trigger.dedupe_key}",
            payload={"trigger": trigger.to_dict(), "payload": trigger_input.payload},
        )
        self._session.flush()
        return trigger

    @staticmethod
    def _to_domain(record: TriggerRecord) -> Trigger:
        return Trigger(
            id=record.id,
            schema_version=record.schema_version,
            source=record.source,
            tenant_id=record.tenant_id,
            thread_id=record.thread_id,
            actor=record.actor,
            payload_ref=record.payload_ref,
            occurred_at=record.occurred_at,
            received_at=record.received_at,
            dedupe_key=record.dedupe_key,
            filter_decision=TriggerFilterDecision(record.filter_decision),
        )

