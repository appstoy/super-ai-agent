from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine, select

from super_agent.application.run_recovery import RunRecoveryService
from super_agent.ingress.schemas import TriggerInput
from super_agent.ingress.service import TriggerIngressService
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import OutboxMessage, RunRecord
from super_agent.storage.outbox import OutboxRepository


def test_failed_run_requeues_its_published_trigger_for_another_attempt() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    trigger_input = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "retry me"},
        occurred_at=datetime.now(UTC),
        dedupe_key="run-recovery-1",
    )

    with session_scope(factory) as session:
        trigger = TriggerIngressService(session).ingest(trigger_input)
        message = OutboxRepository(session).claim_next()
        assert message is not None
        OutboxRepository(session).mark_published(message)
        session.add(
            RunRecord(
                id=uuid4(),
                task_id=uuid4(),
                trigger_id=trigger.id,
                status="failed",
                attempt=1,
            )
        )

    with session_scope(factory) as session:
        decisions = RunRecoveryService(session, max_attempts=3).requeue_retryable()
        assert decisions[0].attempt == 2
        message = session.scalar(select(OutboxMessage))
        assert message is not None and message.status == "PENDING"


def test_retry_limit_does_not_requeue_the_final_attempt() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    trigger_input = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "stop retrying"},
        occurred_at=datetime.now(UTC),
        dedupe_key="run-recovery-limit",
    )

    with session_scope(factory) as session:
        trigger = TriggerIngressService(session).ingest(trigger_input)
        message = OutboxRepository(session).claim_next()
        assert message is not None
        OutboxRepository(session).mark_published(message)
        session.add(
            RunRecord(
                id=uuid4(),
                task_id=uuid4(),
                trigger_id=trigger.id,
                status="timed_out",
                attempt=3,
            )
        )

    with session_scope(factory) as session:
        assert RunRecoveryService(session, max_attempts=3).requeue_retryable() == ()
        message = session.scalar(select(OutboxMessage))
        assert message is not None and message.status == "PUBLISHED"
