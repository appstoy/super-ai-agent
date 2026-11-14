"""Framework-independent entities and state machines for Phase 1."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Self, cast
from uuid import UUID, uuid4

from super_agent.errors import StateTransitionError, VersionConflictError


def _now() -> datetime:
    return datetime.now(UTC)


def _serialize(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    return value


class TriggerFilterDecision(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    REJECTED = "rejected"


class ThreadStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _transition[Status: StrEnum](
    *,
    entity: str,
    current: Status,
    target: Status,
    allowed: dict[Status, frozenset[Status]],
    version: int,
    expected_version: int | None,
) -> int:
    if expected_version is not None and expected_version != version:
        raise VersionConflictError(
            f"{entity} version mismatch: expected {expected_version}, current {version}"
        )
    if target not in allowed.get(current, frozenset()):
        raise StateTransitionError(
            f"illegal {entity} transition: {current.value} -> {target.value}"
        )
    return version + 1


class SerializableEntity:
    """Common serialization contract for domain entities."""

    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _serialize(getattr(self, item.name))
            for item in fields(cast(Any, self))
        }


@dataclass(frozen=True, slots=True)
class Trigger(SerializableEntity):
    id: UUID = field(default_factory=uuid4)
    schema_version: str = "1"
    source: str = "unknown"
    tenant_id: str = "default"
    thread_id: UUID = field(default_factory=uuid4)
    actor: str = "unknown"
    payload_ref: str = ""
    occurred_at: datetime = field(default_factory=_now)
    received_at: datetime = field(default_factory=_now)
    dedupe_key: str = ""
    filter_decision: TriggerFilterDecision = TriggerFilterDecision.PENDING


@dataclass(frozen=True, slots=True)
class Thread(SerializableEntity):
    id: UUID = field(default_factory=uuid4)
    source: str = "unknown"
    external_key: str = ""
    tenant_id: str = "default"
    status: ThreadStatus = ThreadStatus.OPEN
    last_activity_at: datetime = field(default_factory=_now)
    version: int = 0

    _allowed: ClassVar[dict[ThreadStatus, frozenset[ThreadStatus]]] = {
        ThreadStatus.OPEN: frozenset({ThreadStatus.CLOSED}),
        ThreadStatus.CLOSED: frozenset(),
    }

    def transition_to(
        self, target: ThreadStatus, *, expected_version: int | None = None
    ) -> Self:
        version = _transition(
            entity="Thread",
            current=self.status,
            target=target,
            allowed=self._allowed,
            version=self.version,
            expected_version=expected_version,
        )
        return replace(self, status=target, version=version, last_activity_at=_now())


@dataclass(frozen=True, slots=True)
class Task(SerializableEntity):
    id: UUID = field(default_factory=uuid4)
    title: str = ""
    status: TaskStatus = TaskStatus.PENDING
    thread_ids: tuple[UUID, ...] = ()
    worker_id: UUID | None = None
    version: int = 0

    _allowed: ClassVar[dict[TaskStatus, frozenset[TaskStatus]]] = {
        TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
        TaskStatus.RUNNING: frozenset(
            {TaskStatus.WAITING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.WAITING: frozenset(
            {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
        ),
        TaskStatus.SUCCEEDED: frozenset(),
        TaskStatus.FAILED: frozenset({TaskStatus.RUNNING}),
        TaskStatus.CANCELLED: frozenset(),
    }

    def transition_to(self, target: TaskStatus, *, expected_version: int | None = None) -> Self:
        version = _transition(
            entity="Task",
            current=self.status,
            target=target,
            allowed=self._allowed,
            version=self.version,
            expected_version=expected_version,
        )
        return replace(self, status=target, version=version)


@dataclass(frozen=True, slots=True)
class Worker(SerializableEntity):
    id: UUID = field(default_factory=uuid4)
    task_id: UUID = field(default_factory=uuid4)
    status: WorkerStatus = WorkerStatus.STARTING
    capabilities: tuple[str, ...] = ()
    version: int = 0

    _allowed: ClassVar[dict[WorkerStatus, frozenset[WorkerStatus]]] = {
        WorkerStatus.STARTING: frozenset(
            {WorkerStatus.RUNNING, WorkerStatus.FAILED, WorkerStatus.STOPPED}
        ),
        WorkerStatus.RUNNING: frozenset(
            {
                WorkerStatus.WAITING,
                WorkerStatus.SUCCEEDED,
                WorkerStatus.FAILED,
                WorkerStatus.STOPPED,
            }
        ),
        WorkerStatus.WAITING: frozenset(
            {WorkerStatus.RUNNING, WorkerStatus.FAILED, WorkerStatus.STOPPED}
        ),
        WorkerStatus.SUCCEEDED: frozenset(),
        WorkerStatus.FAILED: frozenset({WorkerStatus.RUNNING, WorkerStatus.STOPPED}),
        WorkerStatus.STOPPED: frozenset(),
    }

    def transition_to(self, target: WorkerStatus, *, expected_version: int | None = None) -> Self:
        version = _transition(
            entity="Worker",
            current=self.status,
            target=target,
            allowed=self._allowed,
            version=self.version,
            expected_version=expected_version,
        )
        return replace(self, status=target, version=version)


@dataclass(frozen=True, slots=True)
class Run(SerializableEntity):
    id: UUID = field(default_factory=uuid4)
    task_id: UUID = field(default_factory=uuid4)
    trigger_id: UUID | None = None
    status: RunStatus = RunStatus.PENDING
    attempt: int = 1
    version: int = 0

    _allowed: ClassVar[dict[RunStatus, frozenset[RunStatus]]] = {
        RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
        RunStatus.RUNNING: frozenset(
            {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.CANCEL_REQUESTED,
                RunStatus.TIMED_OUT,
            }
        ),
        RunStatus.CANCEL_REQUESTED: frozenset(
            {RunStatus.CANCELLED, RunStatus.SUCCEEDED, RunStatus.FAILED}
        ),
        RunStatus.FAILED: frozenset({RunStatus.PENDING}),
        RunStatus.TIMED_OUT: frozenset({RunStatus.PENDING}),
        RunStatus.SUCCEEDED: frozenset(),
        RunStatus.CANCELLED: frozenset(),
    }

    def transition_to(self, target: RunStatus, *, expected_version: int | None = None) -> Self:
        version = _transition(
            entity="Run",
            current=self.status,
            target=target,
            allowed=self._allowed,
            version=self.version,
            expected_version=expected_version,
        )
        attempt = self.attempt + 1 if target is RunStatus.PENDING else self.attempt
        return replace(self, status=target, attempt=attempt, version=version)


@dataclass(frozen=True, slots=True)
class Delivery(SerializableEntity):
    id: UUID = field(default_factory=uuid4)
    task_id: UUID = field(default_factory=uuid4)
    channel: str = "unknown"
    idempotency_key: str = ""
    payload_ref: str = ""
    status: DeliveryStatus = DeliveryStatus.PENDING
    version: int = 0

    _allowed: ClassVar[dict[DeliveryStatus, frozenset[DeliveryStatus]]] = {
        DeliveryStatus.PENDING: frozenset(
            {DeliveryStatus.SENT, DeliveryStatus.FAILED, DeliveryStatus.CANCELLED}
        ),
        DeliveryStatus.FAILED: frozenset({DeliveryStatus.PENDING, DeliveryStatus.CANCELLED}),
        DeliveryStatus.SENT: frozenset(),
        DeliveryStatus.CANCELLED: frozenset(),
    }

    def transition_to(self, target: DeliveryStatus, *, expected_version: int | None = None) -> Self:
        version = _transition(
            entity="Delivery",
            current=self.status,
            target=target,
            allowed=self._allowed,
            version=self.version,
            expected_version=expected_version,
        )
        return replace(self, status=target, version=version)

