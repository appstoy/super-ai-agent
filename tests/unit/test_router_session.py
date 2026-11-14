"""Resident Router session: durable memory, batching, and Inline & Spill."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.router import RouteAction, RouterService
from super_agent.application.router_session import ResidentRouterSession
from super_agent.domain import Trigger
from super_agent.ports.model import ModelGateway, ModelRequest, ModelResponse
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import RouterMessageRecord


class CapturingGateway(ModelGateway):
    """Record every full request so tests can inspect the model's messages."""

    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Capture the request and return the next scripted response."""

        self.requests.append(request)
        if self._index >= len(self._responses):
            raise AssertionError("capturing gateway script exhausted")
        response = self._responses[self._index]
        self._index += 1
        return response


def make_trigger(payload: str, *, source: str = "cli") -> Trigger:
    """Build a minimal candidate trigger with a unique identity."""

    return Trigger(
        source=source,
        actor="user",
        thread_id=uuid4(),
        payload_ref=payload,
        dedupe_key=f"router-session-{uuid4()}",
    )


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Provide a fresh in-memory database schema per test."""

    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    return create_session_factory(engine)


def test_second_route_sees_first_decision_in_messages(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = CapturingGateway(
        [
            ModelResponse(text="TASK: plan A"),
            ModelResponse(text="TASK: plan B replaces plan A"),
        ]
    )
    with session_scope(session_factory) as session:
        resident = ResidentRouterSession(session, gateway)
        first = resident.route(make_trigger("let's go with plan A"))
        second = resident.route(make_trigger("actually switch to plan B"))

    assert first.action is RouteAction.CREATED
    assert second.action is RouteAction.CREATED
    assert len(gateway.requests) == 2
    second_messages = gateway.requests[1].messages
    # The first turn's trigger summary and decision are inline in turn two.
    assert any("plan A" in message.content for message in second_messages)
    assert any(
        message.role.value == "assistant" and "TASK: plan A" in message.content
        for message in second_messages
    )


def test_router_service_delegates_to_resident_session(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = CapturingGateway(
        [ModelResponse(text="TASK: first"), ModelResponse(text="TASK: second")]
    )
    with session_scope(session_factory) as session:
        service = RouterService(gateway, session=session)
        service.route(make_trigger("first request"))
        service.route(make_trigger("second request"))

    assert len(gateway.requests) == 2
    assert any(
        "TASK: first" in message.content for message in gateway.requests[1].messages
    )


def test_transcript_persists_across_session_rebuilds(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = CapturingGateway(
        [ModelResponse(text="TASK: remember me"), ModelResponse(text="TASK: still here")]
    )
    with session_scope(session_factory) as session:
        ResidentRouterSession(session, gateway).route(make_trigger("original request"))
    with session_scope(session_factory) as session:
        rows = session.scalars(select(RouterMessageRecord)).all()
        assert [row.role for row in rows] == ["user", "assistant"]
        assert rows[1].content == "TASK: remember me"
        ResidentRouterSession(session, gateway).route(make_trigger("follow-up request"))

    assert any(
        "TASK: remember me" in message.content for message in gateway.requests[1].messages
    )


def test_route_batch_merges_five_triggers_into_one_call(
    session_factory: sessionmaker[Session],
) -> None:
    scripted = "\n".join(
        [
            "1: TASK: alpha",
            "2: IGNORE",
            "3: CLARIFY: which repo?",
            "4: TASK: beta",
            "5: TASK: gamma",
        ]
    )
    gateway = CapturingGateway([ModelResponse(text=scripted)])
    triggers = [make_trigger(f"backlog item {n}") for n in range(5)]
    with session_scope(session_factory) as session:
        resident = ResidentRouterSession(session, gateway)
        results = resident.route_batch(triggers)

    assert len(results) == 5
    assert len(gateway.requests) == 1
    assert [result.action for result in results] == [
        RouteAction.CREATED,
        RouteAction.IGNORED,
        RouteAction.CLARIFY,
        RouteAction.CREATED,
        RouteAction.CREATED,
    ]
    assert results[0].task is not None and results[0].task.title == "alpha"
    assert results[2].reason == "which repo?"
    metrics = resident.metrics
    assert metrics.total_calls == 1
    assert metrics.total_triggers == 5
    assert metrics.last_batch_size == 5
    assert metrics.max_batch_size == 5
    assert metrics.batched_calls == 1
    assert metrics.last_duration_seconds >= 0.0
    assert metrics.average_batch_size == 5.0


def test_route_batch_falls_back_to_task_for_unparseable_output(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = CapturingGateway([ModelResponse(text="sorry, I cannot answer in that format")])
    triggers = [make_trigger("important request one"), make_trigger("important request two")]
    with session_scope(session_factory) as session:
        results = ResidentRouterSession(session, gateway).route_batch(triggers)

    assert len(gateway.requests) == 1
    assert all(result.action is RouteAction.CREATED for result in results)
    assert results[0].task is not None
    assert results[0].task.title == "important request one"
    assert results[1].task is not None
    assert results[1].task.title == "important request two"


def test_route_batch_partial_lines_fall_back_per_trigger(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = CapturingGateway([ModelResponse(text="1: TASK: only first\n3: IGNORE")])
    triggers = [make_trigger(f"item {n}") for n in range(3)]
    with session_scope(session_factory) as session:
        results = ResidentRouterSession(session, gateway).route_batch(triggers)

    assert results[0].action is RouteAction.CREATED
    assert results[1].action is RouteAction.CREATED  # missing line -> safe TASK
    assert results[1].task is not None and results[1].task.title == "item 1"
    assert results[2].action is RouteAction.IGNORED


def test_route_batch_fast_path_ignores_never_reach_model(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = CapturingGateway([ModelResponse(text="1: TASK: real work")])
    triggers = [
        make_trigger("noise message", source="noise"),
        make_trigger("real work"),
    ]
    with session_scope(session_factory) as session:
        results = ResidentRouterSession(session, gateway).route_batch(triggers)

    assert results[0].action is RouteAction.IGNORED
    assert results[1].action is RouteAction.CREATED
    assert len(gateway.requests) == 1
    # Only the candidate trigger is numbered inside the merged request.
    assert "real work" in gateway.requests[0].messages[-1].content
    assert "noise message" not in gateway.requests[0].messages[-1].content


def test_transcript_spill_bounds_window_and_keeps_reference_summary(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = MockModelGateway([ModelResponse(text="TASK: turn") for _ in range(12)])
    with session_scope(session_factory) as session:
        resident = ResidentRouterSession(session, gateway, window_budget=6)
        for turn in range(12):
            resident.route(make_trigger(f"turn number {turn}"))
        window = resident.transcript_window()
        rows = session.scalars(
            select(RouterMessageRecord).order_by(RouterMessageRecord.sequence)
        ).all()

    # 12 turns persist 24 durable messages, but the window stays bounded:
    # first entry + one spill summary + (budget - 2) recent entries.
    assert len(rows) == 24
    assert len(window) == 6
    assert window[0].content == rows[0].content  # first entry kept as anchor
    summary = window[1].content
    assert "spilled router history" in summary
    assert str(rows[5].id) in summary  # recallable object IDs inline
    assert str(rows[0].id) not in summary  # the anchor is not double-counted
    assert window[-1].content == "TASK: turn"
    # The full history stays durable outside the window.
    assert len(rows) > len(window)





def test_transcript_within_budget_is_fully_inline(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = MockModelGateway([ModelResponse(text="TASK: one"), ModelResponse(text="TASK: two")])
    with session_scope(session_factory) as session:
        resident = ResidentRouterSession(session, gateway, window_budget=40)
        resident.route(make_trigger("first"))
        resident.route(make_trigger("second"))
        window = resident.transcript_window()

    assert len(window) == 4
    assert all("spilled" not in message.content for message in window)


def test_tenants_have_isolated_transcripts(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = CapturingGateway(
        [ModelResponse(text="TASK: tenant one"), ModelResponse(text="TASK: tenant two")]
    )
    with session_scope(session_factory) as session:
        ResidentRouterSession(session, gateway, tenant_id="tenant-a").route(
            make_trigger("tenant a secret plan")
        )
        ResidentRouterSession(session, gateway, tenant_id="tenant-b").route(
            make_trigger("tenant b request")
        )

    second_messages = gateway.requests[1].messages
    assert not any("tenant a secret plan" in message.content for message in second_messages)


def test_router_service_without_session_keeps_memory_contract() -> None:
    gateway = MockModelGateway([ModelResponse(text="TASK: legacy entry point")])
    result = RouterService(gateway).route(make_trigger("legacy request"))
    assert result.action is RouteAction.CREATED
    assert result.task is not None and result.task.title == "legacy entry point"


def test_window_budget_validation() -> None:
    gateway = MockModelGateway([])
    with pytest.raises(ValueError, match="window_budget"):
        ResidentRouterSession(None, gateway, window_budget=2)