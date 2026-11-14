"""Correlation IDs for request, task, and run scoped logs."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """Identifiers carried through one synchronous or asynchronous operation."""

    request_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None


_current: ContextVar[CorrelationContext | None] = ContextVar(
    "super_agent_correlation", default=None
)


def get_context() -> CorrelationContext:
    """Return the current immutable correlation context."""

    return _current.get() or CorrelationContext()


def set_context(**values: str | None) -> CorrelationContext:
    """Update only provided correlation fields and return the new context."""

    current = get_context()
    updated = replace(
        current,
        request_id=values.get("request_id", current.request_id),
        task_id=values.get("task_id", current.task_id),
        run_id=values.get("run_id", current.run_id),
    )
    _current.set(updated)
    return updated

