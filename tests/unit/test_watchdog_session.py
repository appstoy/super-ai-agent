"""Model-driven WatchdogSession: intervene, escalate, and graceful degradation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.errors import AgentError
from super_agent.ports.model import ModelResponse
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import (
    DeliveryRecord,
    OutboxMessage,
    RunRecord,
    TaskRecord,
    TriggerRecord,
    WorkerEventRecord,
)
from super_agent.watchdog.runtime import PersistentRunWatchdog
from super_agent.watchdog.session import WatchdogSession


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _seed_stalled_run(factory: sessionmaker[Session], *, retryable: bool = False) -> tuple:
    """One running Run whose heartbeat and lease are long expired.

    With ``retryable`` the Run also has a published trigger event, so a
    bounded retry can requeue it after the rule layer times it out.
    """

    run_id = uuid4()
    task_id = uuid4()
    trigger_id = uuid4() if retryable else None
    dedupe = f"watchdog-stalled:{uuid4()}"
    stale = datetime.now(UTC) - timedelta(minutes=10)
    with session_scope(factory) as session:
        if trigger_id is not None:
            session.add(
                TriggerRecord(
                    id=trigger_id,
                    schema_version="1",
                    source="test",
                    tenant_id="default",
                    thread_id=uuid4(),
                    actor="tester",
                    payload_ref="stalled work",
                    occurred_at=stale,
                    received_at=stale,
                    dedupe_key=dedupe,
                    filter_decision="accepted",
                )
            )
            session.add(
                OutboxMessage(
                    topic="trigger.received",
                    dedupe_key=f"trigger.received:{dedupe}",
                    payload="{}",
                    status="PUBLISHED",
                )
            )
        session.add(
            TaskRecord(id=task_id, title="stalled", status="running", thread_ids="[]", version=0)
        )
        session.add(
            RunRecord(
                id=run_id,
                task_id=task_id,
                trigger_id=trigger_id,
                status="running",
                attempt=1,
                started_at=stale,
                heartbeat_at=stale,
                lease_expires_at=stale,
                lease_owner="dead-worker",
            )
        )
        session.add(
            WorkerEventRecord(
                id=uuid4(),
                worker_id=uuid4(),
                task_id=task_id,
                sequence=1,
                event_type="model_round",
                status="running",
                summary="thinking about the same step again",
            )
        )
    return run_id, task_id


def _seed_retryable_failure(factory: sessionmaker[Session]) -> tuple:
    """One failed Run below the retry limit with a published trigger event."""

    run_id = uuid4()
    task_id = uuid4()
    trigger_id = uuid4()
    dedupe = f"watchdog-test:{uuid4()}"
    now = datetime.now(UTC)
    with session_scope(factory) as session:
        session.add(
            TriggerRecord(
                id=trigger_id,
                schema_version="1",
                source="test",
                tenant_id="default",
                thread_id=uuid4(),
                actor="tester",
                payload_ref="do the thing",
                occurred_at=now,
                received_at=now,
                dedupe_key=dedupe,
                filter_decision="accepted",
            )
        )
        session.add(
            TaskRecord(id=task_id, title="failing", status="failed", thread_ids="[]", version=0)
        )
        session.add(
            RunRecord(
                id=run_id,
                task_id=task_id,
                trigger_id=trigger_id,
                status="failed",
                attempt=1,
                error="provider hiccup",
            )
        )
        session.add(
            OutboxMessage(
                topic="trigger.received",
                dedupe_key=f"trigger.received:{dedupe}",
                payload="{}",
                status="PUBLISHED",
            )
        )
    return run_id, task_id, dedupe


def test_intervene_triggers_bounded_recovery(factory: sessionmaker[Session]) -> None:
    run_id, _ = _seed_stalled_run(factory, retryable=True)
    model = MockModelGateway([ModelResponse(text="INTERVENE: retry the stalled run once")])
    with session_scope(factory) as session:
        outcome = WatchdogSession(
            PersistentRunWatchdog(stalled_after_seconds=60)
        ).tick(session, model)
    assert outcome.decision == "intervene"
    assert outcome.reason == "retry the stalled run once"
    # The rule layer timed the stalled run out; INTERVENE requeued it.
    assert [item.run_id for item in outcome.expired] == [run_id]
    assert [item.run_id for item in outcome.intervention_retries] == [run_id]
    assert outcome.escalation_id is None
    assert outcome.model_error is None
    with session_scope(factory) as session:
        messages = session.scalars(select(OutboxMessage)).all()
        assert [message.status for message in messages] == ["PENDING"]


def test_failed_run_stays_put_without_intervention(factory: sessionmaker[Session]) -> None:
    run_id, _, dedupe = _seed_retryable_failure(factory)
    model = MockModelGateway([ModelResponse(text="HEALTHY")])
    with session_scope(factory) as session:
        outcome = WatchdogSession().tick(session, model)
    assert outcome.retries == ()
    assert outcome.intervention_retries == ()
    with session_scope(factory) as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "failed"
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.dedupe_key == f"trigger.received:{dedupe}")
        )
        assert message is not None
        assert message.status == "PUBLISHED"


def test_intervene_requeues_failed_run(factory: sessionmaker[Session]) -> None:
    run_id, _, dedupe = _seed_retryable_failure(factory)
    model = MockModelGateway([ModelResponse(text="INTERVENE: transient provider failure")])
    with session_scope(factory) as session:
        outcome = WatchdogSession().tick(session, model)
    assert [item.run_id for item in outcome.intervention_retries] == [run_id]
    with session_scope(factory) as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.dedupe_key == f"trigger.received:{dedupe}")
        )
        assert message is not None
        assert message.status == "PENDING"


def test_escalate_writes_pending_human_delivery(factory: sessionmaker[Session]) -> None:
    _seed_stalled_run(factory)
    model = MockModelGateway([ModelResponse(text="ESCALATE: worker repeats the same step")])
    with session_scope(factory) as session:
        outcome = WatchdogSession().tick(session, model)
    assert outcome.decision == "escalate"
    assert outcome.escalation_id is not None
    assert outcome.intervention_retries == ()
    with session_scope(factory) as session:
        delivery = session.get(DeliveryRecord, outcome.escalation_id)
        assert delivery is not None
        assert delivery.channel == "cli"
        assert delivery.status == "pending"
        assert delivery.payload_ref == "worker repeats the same step"


def test_model_failure_keeps_rule_layer_results(factory: sessionmaker[Session]) -> None:
    run_id, _ = _seed_stalled_run(factory)
    model = MockModelGateway([AgentError("provider down")])
    with session_scope(factory) as session:
        outcome = WatchdogSession(
            PersistentRunWatchdog(stalled_after_seconds=60)
        ).tick(session, model)
    assert outcome.model_error == "provider down"
    assert outcome.decision == "healthy"
    # The rule layer still ran: the stalled run was recovered to timed_out.
    assert [item.run_id for item in outcome.expired] == [run_id]
    assert outcome.findings[0].kind.value == "stalled_run"
    with session_scope(factory) as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "timed_out"
        assert session.scalars(select(DeliveryRecord)).all() == []


def test_healthy_verdict_mutates_nothing_extra(factory: sessionmaker[Session]) -> None:
    _seed_stalled_run(factory)
    model = MockModelGateway([ModelResponse(text="HEALTHY")])
    with session_scope(factory) as session:
        outcome = WatchdogSession(
            PersistentRunWatchdog(stalled_after_seconds=60)
        ).tick(session, model)
    assert outcome.decision == "healthy"
    assert outcome.intervention_retries == ()
    assert outcome.escalation_id is None
    with session_scope(factory) as session:
        assert session.scalars(select(DeliveryRecord)).all() == []


def test_unrecognized_verdict_is_treated_as_degraded(factory: sessionmaker[Session]) -> None:
    _seed_stalled_run(factory)
    model = MockModelGateway([ModelResponse(text="Sure! Let me think about this...")])
    with session_scope(factory) as session:
        outcome = WatchdogSession().tick(session, model)
    assert outcome.decision == "healthy"
    assert "unrecognized" in outcome.reason
    assert outcome.intervention_retries == ()
    assert outcome.escalation_id is None


def test_context_includes_truncated_event_summaries(factory: sessionmaker[Session]) -> None:
    _, task_id = _seed_stalled_run(factory)
    long_summary = "x" * 500
    with session_scope(factory) as session:
        session.add(
            WorkerEventRecord(
                id=uuid4(),
                worker_id=uuid4(),
                task_id=task_id,
                sequence=2,
                event_type="model_round",
                status="running",
                summary=long_summary,
            )
        )
    model = MockModelGateway([ModelResponse(text="HEALTHY")])
    with session_scope(factory) as session:
        WatchdogSession(summary_chars=100).tick(session, model)
    assert len(model.calls) == 1
    assert model.calls[0].prompt_chars < 500 + 600  # summary truncated, context bounded


def test_ask_answers_from_health_snapshot(factory: sessionmaker[Session]) -> None:
    _seed_stalled_run(factory)
    model = MockModelGateway([ModelResponse(text="The worker stalled after its lease expired.")])
    with session_scope(factory) as session:
        answer = WatchdogSession().ask(session, model, "why did the run stop?")
    assert answer == "The worker stalled after its lease expired."
    assert len(model.calls) == 1


def test_ask_degrades_when_model_fails(factory: sessionmaker[Session]) -> None:
    _seed_stalled_run(factory)
    model = MockModelGateway([AgentError("offline")])
    with session_scope(factory) as session:
        answer = WatchdogSession(
            PersistentRunWatchdog(stalled_after_seconds=60)
        ).ask(session, model, "what is wrong?")
    assert "model unavailable" in answer
    assert "stalled_run" in answer