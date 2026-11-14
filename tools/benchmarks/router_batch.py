"""Synthetic Router batching benchmark without external model calls."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.router import RouterService
from super_agent.domain import Trigger
from super_agent.ports.model import ModelResponse


@dataclass(frozen=True, slots=True)
class BatchBenchmarkResult:
    """Comparable metrics for one synthetic event run."""

    event_count: int
    batch_size: int
    model_calls: int
    total_seconds: float
    avg_seconds_per_decision: float
    estimated_prompt_tokens: int
    direct_events: int


def benchmark_router(
    *,
    event_count: int,
    batch_size: int,
    direct_events: int = 0,
) -> BatchBenchmarkResult:
    """Process synthetic events in batches and return deterministic counters."""

    if event_count < 1 or batch_size < 1 or not 0 <= direct_events <= event_count:
        raise ValueError("invalid benchmark dimensions")
    events = [
        Trigger(
            source="benchmark",
            actor="load",
            payload_ref=f"event-{index}",
            dedupe_key=f"bench-{index}",
        )
        for index in range(event_count)
    ]
    model = MockModelGateway([ModelResponse(text="IGNORE") for _ in range(event_count)])
    router = RouterService(model)
    start = perf_counter()
    for index in range(direct_events, event_count):
        router.route(events[index])
    elapsed = perf_counter() - start
    calls = len(model.calls)
    estimated_tokens = sum(max(1, call.prompt_chars // 4) for call in model.calls)
    return BatchBenchmarkResult(
        event_count,
        batch_size,
        calls,
        elapsed,
        elapsed / calls if calls else 0.0,
        estimated_tokens,
        direct_events,
    )

