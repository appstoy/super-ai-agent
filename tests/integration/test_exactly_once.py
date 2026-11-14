"""A submitted request must execute exactly once.

`POST /api/tasks/async` ingests a trigger and hands it to the in-process runner,
while a durable Outbox worker polls the same queue. Both used to reach the same
event: `submit_via_outbox` fell back to `submit()` whenever it failed to claim,
so one request produced two overlapping Runs, ran the model twice, and wrote the
same artifacts twice. This test pins the invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import ModelRequest, ModelResponse
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import OutboxMessage, RunRecord
from super_agent.storage.outbox import OutboxRepository


class CountingGateway:
    """Router answers once; every Worker call is counted."""

    def __init__(self) -> None:
        self.worker_calls = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.messages[0].content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: count me")
        self.worker_calls += 1
        return ModelResponse(text="done")


def _factory(tmp_path):
    engine = create_engine_for_url(f"sqlite:///{tmp_path / 'once.db'}")
    create_schema(engine)
    return create_session_factory(engine)


def _trigger(dedupe_key: str) -> TriggerInput:
    return TriggerInput(
        source="web",
        actor="tester",
        thread_id=uuid4(),
        payload={"message": "只跑一次", "model": "test-model"},
        occurred_at=datetime.now(UTC),
        dedupe_key=dedupe_key,
    )


def test_a_claimed_event_is_not_executed_again(tmp_path) -> None:
    """When another consumer owns the event, the caller must not run it."""

    factory = _factory(tmp_path)
    gateway = CountingGateway()
    trigger_input = _trigger(f"web:{uuid4()}:once")
    key = f"trigger.received:{trigger_input.dedupe_key}"

    # Stand in for the durable Outbox worker: let it be the owner.
    with session_scope(factory) as session:
        from super_agent.ingress.service import TriggerIngressService

        TriggerIngressService(session).ingest(trigger_input)
        session.flush()
        claimed = OutboxRepository(session).claim_next(dedupe_key=key)
        assert claimed is not None, "the event should be claimable"
        assert claimed.status == "CLAIMED"

    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session, gateway, max_rounds=2
        ).submit_via_outbox(trigger_input)

    assert gateway.worker_calls == 0, "the model must not run for a claimed event"
    assert result.action.value == "ignored"
    assert result.task_id is None

    with session_scope(factory) as session:
        runs = session.scalars(select(RunRecord)).all()
        assert runs == [], "no Run may be created for an event owned elsewhere"


def test_the_uncontended_path_still_executes(tmp_path) -> None:
    """The normal case must keep working: nobody else claimed it, so run it."""

    factory = _factory(tmp_path)
    gateway = CountingGateway()

    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session, gateway, max_rounds=2
        ).submit_via_outbox(_trigger(f"web:{uuid4()}:normal"))

    assert gateway.worker_calls == 1, "the model should run exactly once"
    assert result.task_id is not None
    assert result.action.value in {"created", "appended"}

    with session_scope(factory) as session:
        runs = session.scalars(select(RunRecord)).all()
        assert len(runs) == 1, "one request is one Run"
        assert runs[0].attempt == 1


def test_submitting_the_same_message_twice_does_not_duplicate_work(tmp_path) -> None:
    """The dedupe key is the identity of a request, not just a label."""

    factory = _factory(tmp_path)
    gateway = CountingGateway()
    dedupe = f"web:{uuid4()}:twice"
    trigger_input = _trigger(dedupe)

    with session_scope(factory) as session:
        AgentOrchestrator(session, gateway, max_rounds=2).submit_via_outbox(trigger_input)
    with session_scope(factory) as session:
        AgentOrchestrator(session, gateway, max_rounds=2).submit_via_outbox(trigger_input)

    with session_scope(factory) as session:
        runs = session.scalars(select(RunRecord)).all()
        messages = session.scalars(
            select(OutboxMessage).where(OutboxMessage.dedupe_key == f"trigger.received:{dedupe}")
        ).all()
    assert len(messages) == 1, "one dedupe key is one queue entry"
    assert len(runs) <= 1, f"duplicate submission created {len(runs)} runs"
