"""Asynchronous in-process Tool Run executor for Phase 2."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from super_agent.errors import AgentError
from super_agent.ports.tool import ExecutionContext
from super_agent.tools.registry import ToolRegistry


class RunEventType(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class ToolRun:
    """Persistable in-memory description of one async tool attempt."""

    id: UUID = field(default_factory=uuid4)
    tool_name: str = ""
    status: str = "PENDING"
    attempt: int = 1
    result: Any = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RunEvent:
    """Internal completion signal consumed by Worker/Router layers."""

    run_id: UUID
    event_type: RunEventType
    status: str
    result: Any = None
    error: str | None = None


class AsyncToolExecutor:
    """Run registered tools asynchronously with cancellation and retry controls."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._runs: dict[UUID, ToolRun] = {}
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._events: list[RunEvent] = []

    @property
    def events(self) -> tuple[RunEvent, ...]:
        return tuple(self._events)

    def get(self, run_id: UUID) -> ToolRun:
        return self._runs[run_id]

    async def start(
        self,
        tool_name: str,
        arguments: dict[str, object],
        context: ExecutionContext,
        *,
        timeout: float | None = None,
        max_attempts: int = 1,
    ) -> ToolRun:
        """Start a background tool and return before it finishes."""

        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        run = ToolRun(
            id=uuid4(),
            tool_name=tool_name,
            status="RUNNING",
            started_at=datetime.now(UTC),
        )
        self._runs[run.id] = run
        self._events.append(RunEvent(run.id, RunEventType.STARTED, run.status))
        task = asyncio.create_task(
            self._execute(run, arguments, context, timeout=timeout, max_attempts=max_attempts)
        )
        self._tasks[run.id] = task
        return run

    async def stop(self, run_id: UUID) -> ToolRun:
        """Cancel a running task and emit one terminal cancellation event."""

        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._runs[run_id].status not in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            self._finish(run_id, status="CANCELLED", event_type=RunEventType.CANCELLED)
        return self._runs[run_id]

    async def wait(self, run_id: UUID) -> ToolRun:
        """Wait for a started run and return its terminal state."""

        task = self._tasks.get(run_id)
        if task is not None:
            await task
        return self._runs[run_id]

    async def _execute(
        self,
        run: ToolRun,
        arguments: dict[str, object],
        context: ExecutionContext,
        *,
        timeout: float | None,
        max_attempts: int,
    ) -> None:
        for attempt in range(1, max_attempts + 1):
            self._runs[run.id] = replace(self._runs[run.id], attempt=attempt)
            try:
                coroutine = asyncio.to_thread(
                    self._registry.execute, run.tool_name, arguments, context
                )
                result = await asyncio.wait_for(coroutine, timeout=timeout)
            except asyncio.CancelledError:
                if self._runs[run.id].status != "CANCELLED":
                    self._finish(run.id, status="CANCELLED", event_type=RunEventType.CANCELLED)
                return
            except TimeoutError as exc:
                self._finish(
                    run.id,
                    status="TIMED_OUT",
                    event_type=RunEventType.TIMED_OUT,
                    error=str(exc) or "tool timed out",
                )
                return
            except AgentError as exc:
                if exc.retryable and attempt < max_attempts:
                    continue
                self._finish(
                    run.id,
                    status="FAILED",
                    event_type=RunEventType.FAILED,
                    error=str(exc),
                )
                return
            except Exception as exc:
                self._finish(
                    run.id,
                    status="FAILED",
                    event_type=RunEventType.FAILED,
                    error=str(exc),
                )
                return
            self._finish(
                run.id,
                status="SUCCEEDED",
                event_type=RunEventType.COMPLETED,
                result=result,
            )
            return

    def _finish(
        self,
        run_id: UUID,
        *,
        status: str,
        event_type: RunEventType,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        current = self._runs[run_id]
        if current.status in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            return
        finished = replace(
            current,
            status=status,
            result=result,
            error=error,
            finished_at=datetime.now(UTC),
        )
        self._runs[run_id] = finished
        self._events.append(RunEvent(run_id, event_type, status, result=result, error=error))

