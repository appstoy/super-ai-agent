"""Thread subscription routing and idempotent event delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from super_agent.application.worker import WorkerRuntime
from super_agent.context import ContextEntry
from super_agent.domain import Trigger


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class DeliveryDisposition(StrEnum):
    DIRECT = "direct"
    DUPLICATE = "duplicate"
    ROUTER = "router"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class Subscription:
    """A versioned Thread-to-Worker route with an optional expiry."""

    thread_id: UUID
    worker_id: UUID
    subscription_id: UUID = field(default_factory=uuid4, init=False)
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    expires_at: datetime | None = None
    version: int = 0

    def is_active(self, now: datetime | None = None) -> bool:
        """Return whether this subscription can receive a trigger now."""

        if self.status is not SubscriptionStatus.ACTIVE:
            return False
        return self.expires_at is None or self.expires_at > (now or datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Result of routing one Trigger to subscribed Workers."""

    event_id: UUID
    disposition: DeliveryDisposition
    worker_ids: tuple[UUID, ...] = ()


class SubscriptionRouter:
    """Manage subscriptions and deliver each event at most once per worker."""

    def __init__(self) -> None:
        self._subscriptions: dict[tuple[UUID, UUID], Subscription] = {}
        self._workers: dict[UUID, WorkerRuntime] = {}
        self._consumed: set[tuple[UUID, UUID]] = set()

    def register_worker(self, runtime: WorkerRuntime) -> None:
        self._workers[runtime.worker.id] = runtime

    def subscribe(
        self,
        thread_id: UUID,
        worker_id: UUID,
        *,
        expires_in: timedelta | None = None,
    ) -> Subscription:
        subscription = Subscription(
            thread_id=thread_id,
            worker_id=worker_id,
            expires_at=(datetime.now(UTC) + expires_in) if expires_in else None,
        )
        self._subscriptions[(thread_id, worker_id)] = subscription
        return subscription

    def cancel(self, thread_id: UUID, worker_id: UUID) -> Subscription:
        current = self._subscriptions[(thread_id, worker_id)]
        updated = Subscription(
            thread_id=current.thread_id,
            worker_id=current.worker_id,
            status=SubscriptionStatus.CANCELLED,
            expires_at=current.expires_at,
            version=current.version + 1,
        )
        self._subscriptions[(thread_id, worker_id)] = updated
        return updated

    def renew(self, thread_id: UUID, worker_id: UUID, expires_in: timedelta) -> Subscription:
        current = self._subscriptions[(thread_id, worker_id)]
        updated = Subscription(
            thread_id=current.thread_id,
            worker_id=current.worker_id,
            expires_at=datetime.now(UTC) + expires_in,
            version=current.version + 1,
        )
        self._subscriptions[(thread_id, worker_id)] = updated
        return updated

    def deliver(self, trigger: Trigger) -> DeliveryReceipt:
        """Direct a Trigger to all active subscribers, exactly once per pair."""

        matches = [
            subscription
            for (thread_id, _), subscription in self._subscriptions.items()
            if thread_id == trigger.thread_id and subscription.is_active()
        ]
        if not matches:
            return DeliveryReceipt(trigger.id, DeliveryDisposition.NO_MATCH)
        delivered: list[UUID] = []
        duplicates = 0
        for subscription in matches:
            key = (subscription.worker_id, trigger.id)
            if key in self._consumed:
                duplicates += 1
                continue
            runtime = self._workers.get(subscription.worker_id)
            if runtime is None:
                continue
            self._consumed.add(key)
            runtime.append_context(
                ContextEntry(
                    role="trigger",
                    content=trigger.payload_ref,
                    metadata=(("event_id", str(trigger.id)),),
                )
            )
            delivered.append(subscription.worker_id)
        if delivered:
            return DeliveryReceipt(trigger.id, DeliveryDisposition.DIRECT, tuple(delivered))
        if duplicates:
            return DeliveryReceipt(trigger.id, DeliveryDisposition.DUPLICATE)
        return DeliveryReceipt(trigger.id, DeliveryDisposition.ROUTER)

