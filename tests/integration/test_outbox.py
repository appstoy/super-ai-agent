from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import OutboxMessage
from super_agent.storage.outbox import OutboxRepository


def test_outbox_enqueue_claim_publish_and_retry() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)

    with session_scope(factory) as session:
        message = OutboxRepository(session).enqueue(
            topic="trigger.received",
            dedupe_key="event-1",
            payload={"event_id": "event-1", "value": "hello"},
        )
        assert message.status == "PENDING"

    with session_scope(factory) as session:
        repository = OutboxRepository(session)
        claimed = repository.claim_next()
        assert claimed is not None
        assert claimed.status == "CLAIMED"
        assert claimed.attempts == 1
        repository.mark_failed(claimed, "temporary")

    with session_scope(factory) as session:
        repository = OutboxRepository(session)
        claimed = repository.claim_next()
        assert claimed is not None
        repository.mark_published(claimed)

    with session_scope(factory) as session:
        stored = session.scalar(select(OutboxMessage))
        assert stored is not None
        assert stored.status == "PUBLISHED"
        assert stored.last_error == "temporary"
        assert stored.attempts == 2


def test_outbox_rejects_duplicate_dedupe_key() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)

    with session_scope(factory) as session:
        OutboxRepository(session).enqueue(
            topic="trigger.received",
            dedupe_key="event-duplicate",
            payload={"event_id": "event-duplicate"},
        )

    with pytest.raises(IntegrityError):
        with session_scope(factory) as session:
            OutboxRepository(session).enqueue(
                topic="trigger.received",
                dedupe_key="event-duplicate",
                payload={"event_id": "event-duplicate"},
            )


def test_outbox_reclaims_an_expired_consumer_lease() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)

    with session_scope(factory) as session:
        message = OutboxRepository(session).enqueue(
            topic="trigger.received",
            dedupe_key="expired-lease",
            payload={"event_id": "expired-lease"},
        )
        claimed = OutboxRepository(session).claim_next()
        assert claimed is not None
        claimed.claimed_at = datetime.now(UTC) - timedelta(minutes=5)

    with session_scope(factory) as session:
        reclaimed = OutboxRepository(session).claim_next(lease_seconds=30)
        assert reclaimed is not None
        assert reclaimed.id == message.id
        assert reclaimed.attempts == 2


def test_concurrent_consumers_only_claim_one_outbox_message(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'claims.db'}"
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    create_schema(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        OutboxRepository(session).enqueue(
            topic="trigger.received",
            dedupe_key="concurrent-claim",
            payload={"event_id": "concurrent-claim"},
        )

    def claim() -> bool:
        worker_engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        worker_factory = create_session_factory(worker_engine)
        try:
            with session_scope(worker_factory) as session:
                return OutboxRepository(session).claim_next() is not None
        finally:
            worker_engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert sum(results) == 1

