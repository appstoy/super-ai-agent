"""Worker lifecycle runtime with replayable state events."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from super_agent.context import ContextEntry, WorkerContext
from super_agent.domain import Worker, WorkerStatus


class WorkerEventType(StrEnum):
    STATE_CHANGED = "state_changed"
    CONTEXT_APPENDED = "context_appended"
    RESULT_REPORTED = "result_reported"


@dataclass(frozen=True, slots=True)
class WorkerStateEvent:
    """Append-only event emitted by WorkerRuntime."""

    event_id: UUID
    worker_id: UUID
    task_id: UUID
    sequence: int
    event_type: WorkerEventType
    status: WorkerStatus
    context_revision: int
    summary: str = ""
    created_at: datetime = datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Internal result reported to Router; it is not a Delivery."""

    worker_id: UUID
    task_id: UUID
    status: WorkerStatus
    output_ref: str
    summary: str = ""


EventSink = Callable[[WorkerStateEvent], None]


class WorkerRuntime:
    """Own one Worker state and one isolated context snapshot."""

    def __init__(
        self,
        worker: Worker,
        *,
        event_sink: EventSink | None = None,
        context: WorkerContext | None = None,
    ) -> None:
        if context is not None and (
            context.worker_id != worker.id or context.task_id != worker.task_id
        ):
            raise ValueError("context does not belong to worker")
        self._worker = worker
        self._context = context or WorkerContext(worker_id=worker.id, task_id=worker.task_id)
        self._events: list[WorkerStateEvent] = []
        self._event_sink = event_sink

    @property
    def worker(self) -> Worker:
        return self._worker

    @property
    def context(self) -> WorkerContext:
        return self._context

    @property
    def events(self) -> tuple[WorkerStateEvent, ...]:
        return tuple(self._events)

    def start(self) -> WorkerStateEvent:
        return self._transition(WorkerStatus.RUNNING, "worker started")

    def wait(self, summary: str = "worker waiting") -> WorkerStateEvent:
        return self._transition(WorkerStatus.WAITING, summary)

    def resume(self) -> WorkerStateEvent:
        return self._transition(WorkerStatus.RUNNING, "worker resumed")

    def append_context(self, entry: ContextEntry) -> WorkerStateEvent:
        self._context = self._context.append(entry)
        return self._emit(WorkerEventType.CONTEXT_APPENDED, summary=entry.role)

    def succeed(self, output_ref: str, summary: str = "") -> WorkerResult:
        self._transition(WorkerStatus.SUCCEEDED, summary or "worker succeeded")
        self._emit(WorkerEventType.RESULT_REPORTED, summary=summary)
        return WorkerResult(
            self._worker.id, self._worker.task_id, self._worker.status, output_ref, summary
        )

    def fail(self, summary: str) -> WorkerResult:
        self._transition(WorkerStatus.FAILED, summary)
        self._emit(WorkerEventType.RESULT_REPORTED, summary=summary)
        return WorkerResult(self._worker.id, self._worker.task_id, self._worker.status, "", summary)

    def stop(self, summary: str = "worker stopped") -> WorkerResult:
        """Stop cooperatively at a safe checkpoint and emit a terminal result."""

        self._transition(WorkerStatus.STOPPED, summary)
        self._emit(WorkerEventType.RESULT_REPORTED, summary=summary)
        return WorkerResult(self._worker.id, self._worker.task_id, self._worker.status, "", summary)

    def _transition(self, target: WorkerStatus, summary: str) -> WorkerStateEvent:
        self._worker = self._worker.transition_to(target, expected_version=self._worker.version)
        return self._emit(WorkerEventType.STATE_CHANGED, summary=summary)

    def _emit(self, event_type: WorkerEventType, *, summary: str) -> WorkerStateEvent:
        event = WorkerStateEvent(
            event_id=uuid4(),
            worker_id=self._worker.id,
            task_id=self._worker.task_id,
            sequence=len(self._events) + 1,
            event_type=event_type,
            status=self._worker.status,
            context_revision=self._context.revision,
            summary=summary,
            created_at=datetime.now(UTC),
        )
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)
        return event

