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


@dataclass
class RunMetrics:
    """Execution telemetry accumulated while one Worker runs.

    Kept on the runtime instead of being derived afterwards: token usage only
    exists in the model response, so it has to be captured at the moment of the
    call. Aggregating here means one place to read from when the Run closes.
    """

    model_rounds: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    tool_calls: int = 0

    def record_model_call(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> None:
        self.model_rounds += 1
        self.prompt_tokens += max(0, prompt_tokens)
        self.completion_tokens += max(0, completion_tokens)
        self.cached_tokens += max(0, cached_tokens)

    def record_tool_call(self, count: int = 1) -> None:
        self.tool_calls += max(0, count)

    @property
    def cache_hit_rate(self) -> float:
        """Share of prompt tokens served from cache, 0.0-1.0."""

        if self.prompt_tokens <= 0:
            return 0.0
        return min(1.0, self.cached_tokens / self.prompt_tokens)

    def to_dict(self) -> dict[str, object]:
        return {
            "model_rounds": self.model_rounds,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "tool_calls": self.tool_calls,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
        }


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
        self.metrics = RunMetrics()

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

