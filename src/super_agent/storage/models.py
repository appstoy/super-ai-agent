"""SQLAlchemy persistence models for the Phase 0 state foundation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base metadata used by the application and Alembic."""


class TriggerRecord(Base):
    """Normalized external event retained for routing and replay."""

    __tablename__ = "triggers"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_triggers_dedupe_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_ref: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    filter_decision: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "schema_version": self.schema_version,
            "source": self.source,
            "tenant_id": self.tenant_id,
            "thread_id": str(self.thread_id),
            "actor": self.actor,
            "payload_ref": self.payload_ref,
            "occurred_at": self.occurred_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "dedupe_key": self.dedupe_key,
            "filter_decision": self.filter_decision,
        }


class TaskRecord(Base):
    """Persisted user-visible task created by Router."""

    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_ids: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[UUID | None] = mapped_column()
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WorkerRecord(Base):
    """Persisted execution worker associated with a Task."""

    __tablename__ = "workers"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    capabilities: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DeliveryRecord(Base):
    """Persisted outward-facing result or clarification."""

    __tablename__ = "deliveries"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    channel: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    payload_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RunRecord(Base):
    """Durable execution attempt associated with a task."""

    __tablename__ = "runs"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    trigger_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Execution telemetry, aggregated across every model call in this Run.
    # These answer "why was this slow / expensive?" without reading logs:
    # model_rounds counts model calls, and the token fields separate fresh
    # input from prompt-cache hits so a continued thread visibly reuses context.
    model_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WorkerEventRecord(Base):
    """Append-only worker lifecycle event for timeline and recovery."""

    __tablename__ = "worker_events"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    worker_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    context_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubscriptionRecord(Base):
    """Durable Thread-to-Worker subscription."""

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("thread_id", "worker_id", name="uq_subscriptions_pair"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thread_id: Mapped[UUID] = mapped_column(nullable=False)
    worker_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WorkerContextRecord(Base):
    """Durable copy-on-write context entry owned by a Worker."""

    __tablename__ = "worker_context_entries"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    worker_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class OutboxMessage(Base):
    """Durable event waiting for publication to an EventBus consumer."""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
        # The worker claims by (status, available_at) and retires by attempts;
        # without this index every poll scans the whole queue.
        Index("ix_outbox_claim", "status", "available_at", "created_at"),
        Index("ix_outbox_attempts", "status", "attempts"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def as_dict(self) -> dict[str, Any]:
        """Return a safe persistence representation for adapters and tests."""

        return {
            "id": str(self.id),
            "topic": self.topic,
            "dedupe_key": self.dedupe_key,
            "payload": self.payload,
            "status": self.status,
            "attempts": self.attempts,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class RouterMessageRecord(Base):
    """Durable transcript of the resident Router session (global memory)."""

    __tablename__ = "router_messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ToolRunRecord(Base):
    """Durable background tool run for the asynchronous ReAct loop."""

    __tablename__ = "tool_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    worker_id: Mapped[UUID] = mapped_column(nullable=False)
    origin_run_id: Mapped[UUID | None] = mapped_column()
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    arguments_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    result_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timeout_seconds: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceRecord(Base):
    """Registered BYOM execution device; the secret never enters model context."""

    __tablename__ = "devices"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    capabilities: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    revoked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class TaskFlagRecord(Base):
    """User-owned presentation state for one card (pin, archive, colour).

    Deliberately a separate table rather than columns on ``tasks``: these are
    cosmetic preferences a person sets on their own view, not runtime facts
    about the work. Keeping them apart means a task can be re-run, retried or
    reconciled without its pin state being touched, and a database that has
    never seen a flag behaves exactly as before.

    A row exists only once a user changes something, so an untouched task costs
    nothing to store.
    """

    __tablename__ = "task_flags"

    task_id: Mapped[UUID] = mapped_column(primary_key=True)
    pinned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # One of: "" (no colour), red, orange, yellow, green, blue, purple, gray.
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
