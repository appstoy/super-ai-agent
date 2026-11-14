from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select

from super_agent.ingress.schemas import MAX_PAYLOAD_BYTES, TriggerInput
from super_agent.ingress.service import TriggerIngressService
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import OutboxMessage, TriggerRecord


def make_input(
    *, dedupe_key: str = "event-1", payload: dict[str, object] | None = None
) -> TriggerInput:
    return TriggerInput(
        source="test",
        thread_id=uuid4(),
        actor="user",
        payload=payload or {"message": "hello"},
        occurred_at=datetime.now(UTC),
        dedupe_key=dedupe_key,
    )


def test_ingress_persists_trigger_and_outbox_once() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    first_input = make_input()

    with session_scope(factory) as session:
        first = TriggerIngressService(session).ingest(first_input)
        duplicate = TriggerIngressService(session).ingest(first_input)
        assert duplicate.id == first.id

    with session_scope(factory) as session:
        triggers = session.scalars(select(TriggerRecord)).all()
        messages = session.scalars(select(OutboxMessage)).all()
        assert len(triggers) == 1
        assert len(messages) == 1
        assert messages[0].topic == "trigger.received"


def test_ingress_rejects_naive_time_and_large_payload() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        TriggerInput(
            source="test",
            thread_id=uuid4(),
            actor="user",
            payload={"message": "hello"},
            occurred_at=datetime.now(),
            dedupe_key="naive-time",
        )

    oversized = {"blob": "x" * (MAX_PAYLOAD_BYTES + 1)}
    with pytest.raises(ValidationError, match="exceeds"):
        make_input(payload=oversized)

