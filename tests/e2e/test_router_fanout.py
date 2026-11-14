"""E2E: two-level filtering and many-to-many subscription fan-out (§3.1, §3.2)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.domain import TaskStatus, Trigger
from super_agent.ingress.schemas import TriggerInput
from super_agent.policies.events import EventFilter
from super_agent.ports.model import ModelGateway, ModelRequest, ModelResponse
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import (
    RunRecord,
    SubscriptionRecord,
    TaskRecord,
    TriggerRecord,
    WorkerContextRecord,
    WorkerEventRecord,
    WorkerRecord,
)


class CapturingGateway(ModelGateway):
    """Scripted gateway that records every full request for assertions."""

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


def make_factory() -> sessionmaker[Session]:
    """Create a fresh in-memory database with the full schema."""

    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    return create_session_factory(engine)


def make_trigger(thread_id: UUID, message: str, *, source: str = "web") -> Trigger:
    """Build a normalized trigger on one thread."""

    return Trigger(
        source=source,
        actor="user",
        thread_id=thread_id,
        payload_ref=message,
        occurred_at=datetime.now(UTC),
        dedupe_key=f"fanout-{uuid4()}",
    )


def _next_seed_seq(session: Session) -> int:
    """Keep seeded subscriptions in insertion order for deterministic fan-out."""

    from sqlalchemy import func

    current = session.query(func.max(SubscriptionRecord.seq)).scalar()
    return (current or 0) + 1


def seed_worker(
    session: Session, thread_id: UUID, title: str, *, expires_at: datetime | None = None
) -> tuple[UUID, UUID]:
    """Persist a finished Task/Worker with an active Thread subscription."""

    task_id = uuid4()
    worker_id = uuid4()
    session.add(
        TaskRecord(
            id=task_id,
            title=title,
            status=TaskStatus.SUCCEEDED.value,
            thread_ids=json.dumps([str(thread_id)]),
            worker_id=worker_id,
            version=1,
        )
    )
    session.add(
        WorkerRecord(
            id=worker_id,
            task_id=task_id,
            status="succeeded",
            capabilities="[]",
            version=1,
        )
    )
    session.add(
        SubscriptionRecord(
            id=uuid4(),
            seq=_next_seed_seq(session),
            thread_id=thread_id,
            worker_id=worker_id,
            status="active",
            expires_at=expires_at,
            version=0,
        )
    )
    session.flush()
    return task_id, worker_id


def test_ignored_source_never_calls_the_model() -> None:
    factory = make_factory()
    gateway = CapturingGateway([ModelResponse(text="TASK: must not run")])
    thread_id = uuid4()
    trigger = make_trigger(thread_id, "noise event", source="chat")
    event_filter = EventFilter(allowed_sources=frozenset({"web"}))

    with session_scope(factory) as session:
        result = AgentOrchestrator(session, gateway, event_filter=event_filter).process(
            trigger, payload={"message": "noise event"}
        )
        assert result.action.value == "ignored"
        assert result.task_id is None
        assert gateway.requests == []
        assert session.query(TaskRecord).count() == 0
        assert session.query(RunRecord).count() == 0


def test_ignored_trigger_persists_filter_decision() -> None:
    factory = make_factory()
    gateway = CapturingGateway([])
    event = TriggerInput(
        source="chat",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "bot chatter"},
        occurred_at=datetime.now(UTC),
        dedupe_key=f"fanout-ignored-{uuid4()}",
    )
    event_filter = EventFilter(ignored_actors=frozenset({"user"}))

    with session_scope(factory) as session:
        result = AgentOrchestrator(session, gateway, event_filter=event_filter).submit(event)
        assert result.action.value == "ignored"

    with session_scope(factory) as session:
        record = session.scalar(select(TriggerRecord))
        assert record is not None and record.filter_decision == "ignored"
    assert gateway.requests == []


def test_empty_message_is_filtered_before_any_model_call() -> None:
    factory = make_factory()
    gateway = CapturingGateway([])
    trigger = make_trigger(uuid4(), "")

    with session_scope(factory) as session:
        result = AgentOrchestrator(session, gateway).process(trigger)

    assert result.action.value == "ignored"
    assert gateway.requests == []


def test_explicit_request_accepts_without_router_call() -> None:
    factory = make_factory()
    gateway = CapturingGateway([ModelResponse(text="explicit task completed")])
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "please do this now", "explicit": True},
        occurred_at=datetime.now(UTC),
        dedupe_key=f"fanout-explicit-{uuid4()}",
    )

    with session_scope(factory) as session:
        result = AgentOrchestrator(session, gateway).submit(event)
        assert result.status == TaskStatus.SUCCEEDED.value
        assert result.text == "explicit task completed"
        # Exactly one model call: the Worker turn. The Router was skipped.
        assert len(gateway.requests) == 1
        assert "worker" in gateway.requests[0].messages[0].content.lower()
        record = session.scalar(select(TriggerRecord))
        assert record is not None and record.filter_decision == "accepted"


def test_candidate_event_goes_through_router_semantic_layer() -> None:
    factory = make_factory()
    gateway = CapturingGateway(
        [ModelResponse(text="TASK: candidate work"), ModelResponse(text="candidate done")]
    )
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "maybe look at this"},
        occurred_at=datetime.now(UTC),
        dedupe_key=f"fanout-candidate-{uuid4()}",
    )

    with session_scope(factory) as session:
        result = AgentOrchestrator(session, gateway).submit(event)
        assert result.status == TaskStatus.SUCCEEDED.value
        assert len(gateway.requests) == 2
        assert gateway.requests[0].messages[0].content.startswith("Return exactly one route")
        record = session.scalar(select(TriggerRecord))
        assert record is not None and record.filter_decision == "accepted"


def test_fanout_delivers_event_to_both_subscribers_with_own_runs() -> None:
    factory = make_factory()
    thread_id = uuid4()
    gateway = CapturingGateway(
        [ModelResponse(text="worker one answer"), ModelResponse(text="worker two answer")]
    )

    with session_scope(factory) as session:
        task_one, worker_one = seed_worker(session, thread_id, "task one")
        task_two, worker_two = seed_worker(session, thread_id, "task two")
        trigger = make_trigger(thread_id, "shared update for both workers")
        result = AgentOrchestrator(session, gateway).process(
            trigger, payload={"message": "shared update for both workers"}
        )

        assert result.task_id == task_one  # primary = first subscriber
        assert result.worker_id == worker_one
        assert result.text == "worker one answer"
        assert len(result.fanout_results) == 2
        assert {item.task_id for item in result.fanout_results} == {task_one, task_two}
        assert {item.text for item in result.fanout_results} == {
            "worker one answer",
            "worker two answer",
        }
        assert all(item.status == TaskStatus.SUCCEEDED.value for item in result.fanout_results)

        runs = session.scalars(select(RunRecord)).all()
        assert len(runs) == 2
        assert {run.task_id for run in runs} == {task_one, task_two}
        assert all(run.trigger_id == trigger.id for run in runs)
        assert all(run.status == "succeeded" for run in runs)

        # Both workers received the same event in their own context.
        for worker_id in (worker_one, worker_two):
            entries = session.scalars(
                select(WorkerContextRecord).where(WorkerContextRecord.worker_id == worker_id)
            ).all()
            assert any(
                entry.role == "trigger" and "shared update for both workers" in entry.content
                for entry in entries
            )
            events = session.scalars(
                select(WorkerEventRecord).where(WorkerEventRecord.worker_id == worker_id)
            ).all()
            assert events, "each subscriber must persist its own lifecycle timeline"

    # Both worker turns reached the model; no Router call on the fast path.
    assert len(gateway.requests) == 2


def test_fanout_skips_missing_worker_and_keeps_other_subscriber() -> None:
    factory = make_factory()
    thread_id = uuid4()
    gateway = CapturingGateway([ModelResponse(text="healthy worker answer")])

    with session_scope(factory) as session:
        healthy_task, healthy_worker = seed_worker(session, thread_id, "healthy task")
        broken_task, broken_worker = seed_worker(session, thread_id, "broken task")
        # Simulate a lost Worker record for the second subscriber.
        broken_record = session.get(WorkerRecord, broken_worker)
        assert broken_record is not None
        session.delete(broken_record)
        session.flush()

        trigger = make_trigger(thread_id, "event after worker loss")
        result = AgentOrchestrator(session, gateway).process(
            trigger, payload={"message": "event after worker loss"}
        )

        assert result.task_id == healthy_task
        assert result.text == "healthy worker answer"
        assert len(result.fanout_results) == 1
        broken_subscription = session.scalar(
            select(SubscriptionRecord).where(SubscriptionRecord.worker_id == broken_worker)
        )
        assert broken_subscription is not None and broken_subscription.status == "expired"
        runs = session.scalars(select(RunRecord)).all()
        assert len(runs) == 1 and runs[0].task_id == healthy_task
        assert session.get(TaskRecord, broken_task) is not None

    assert len(gateway.requests) == 1


def test_fanout_ignores_expired_subscriptions() -> None:
    factory = make_factory()
    thread_id = uuid4()
    gateway = CapturingGateway(
        [ModelResponse(text="TASK: fresh start"), ModelResponse(text="fresh worker answer")]
    )

    with session_scope(factory) as session:
        seed_worker(
            session,
            thread_id,
            "expired task",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        trigger = make_trigger(thread_id, "event after expiry")
        result = AgentOrchestrator(session, gateway).process(
            trigger, payload={"message": "event after expiry"}
        )

        # The expired subscription is skipped; the Router cold-starts a task.
        assert result.status == TaskStatus.SUCCEEDED.value
        assert result.text == "fresh worker answer"
        assert result.fanout_results == ()
        assert len(gateway.requests) == 2
        assert session.query(SubscriptionRecord).count() == 2


def test_fanout_failure_of_one_worker_does_not_block_the_other() -> None:
    factory = make_factory()
    thread_id = uuid4()

    class FirstWorkerFailsGateway(ModelGateway):
        """Fail the first Worker turn, then answer the second normally."""

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request: ModelRequest) -> ModelResponse:
            """Raise on the first worker call; succeed afterwards."""

            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("model backend unavailable")
            return ModelResponse(text="second worker survived")

    gateway = FirstWorkerFailsGateway()
    with session_scope(factory) as session:
        task_one, _ = seed_worker(session, thread_id, "fragile task")
        task_two, _ = seed_worker(session, thread_id, "robust task")
        trigger = make_trigger(thread_id, "event during partial outage")
        result = AgentOrchestrator(session, gateway).process(
            trigger, payload={"message": "event during partial outage"}
        )

        assert len(result.fanout_results) == 2
        failed = next(item for item in result.fanout_results if item.task_id == task_one)
        survived = next(item for item in result.fanout_results if item.task_id == task_two)
        assert failed.status == "failed"
        assert survived.status == TaskStatus.SUCCEEDED.value
        assert survived.text == "second worker survived"
        runs = session.scalars(select(RunRecord)).all()
        assert len(runs) == 2
        statuses = {run.task_id: run.status for run in runs}
        assert statuses[task_one] == "failed"
        assert statuses[task_two] == "succeeded"