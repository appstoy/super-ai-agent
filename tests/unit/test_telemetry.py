from uuid import uuid4

from super_agent.observability.telemetry import Telemetry


def test_telemetry_records_correlated_span_and_counter() -> None:
    telemetry = Telemetry()
    trace_id = uuid4()
    with telemetry.span("router", trace_id=trace_id):
        telemetry.increment("router.decisions")

    assert telemetry.spans[0].trace_id == trace_id
    assert telemetry.spans[0].name == "router"
    assert telemetry.counters["router.decisions"] == 1


def test_telemetry_budget_stops_before_exceeding_limits() -> None:
    telemetry = Telemetry()
    allowed = telemetry.account(tokens=10, cost=0.1, max_tokens=20, max_cost=1.0)
    blocked = telemetry.account(tokens=15, cost=0.1, max_tokens=20, max_cost=1.0)

    assert allowed.allowed
    assert blocked.action == "approval_required"
    assert telemetry.used_tokens == 10

