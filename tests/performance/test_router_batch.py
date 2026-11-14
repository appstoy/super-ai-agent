import pytest

from tools.benchmarks.router_batch import benchmark_router


def test_router_batch_benchmark_reports_comparable_metrics() -> None:
    result = benchmark_router(event_count=20, batch_size=5, direct_events=8)

    assert result.event_count == 20
    assert result.batch_size == 5
    assert result.model_calls == 12
    assert result.direct_events == 8
    assert result.avg_seconds_per_decision >= 0


def test_router_batch_benchmark_validates_dimensions() -> None:
    with pytest.raises(ValueError):
        benchmark_router(event_count=0, batch_size=1)
    with pytest.raises(ValueError):
        benchmark_router(event_count=1, batch_size=2, direct_events=2)

