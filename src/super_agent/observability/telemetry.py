"""Exporter-neutral tracing, metrics, and budget accounting."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class SpanRecord:
    span_id: UUID
    name: str
    trace_id: UUID
    task_id: UUID | None
    run_id: UUID | None
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    action: str
    used_tokens: int
    used_cost: float


class Telemetry:
    """Collect in-memory spans, counters, and budget accounting."""

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []
        self.counters: dict[str, int] = {}
        self.used_tokens = 0
        self.used_cost = 0.0

    @contextmanager
    def span(
        self,
        name: str,
        *,
        trace_id: UUID | None = None,
        task_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> Iterator[UUID]:
        active_trace = trace_id or uuid4()
        span_id = uuid4()
        started = perf_counter()
        try:
            yield span_id
        finally:
            self.spans.append(
                SpanRecord(
                    span_id,
                    name,
                    active_trace,
                    task_id,
                    run_id,
                    perf_counter() - started,
                )
            )

    def increment(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + value

    def account(
        self, *, tokens: int, cost: float, max_tokens: int, max_cost: float
    ) -> BudgetDecision:
        next_tokens = self.used_tokens + tokens
        next_cost = self.used_cost + cost
        if next_tokens > max_tokens or next_cost > max_cost:
            return BudgetDecision(False, "approval_required", self.used_tokens, self.used_cost)
        self.used_tokens = next_tokens
        self.used_cost = next_cost
        return BudgetDecision(True, "continue", next_tokens, next_cost)

