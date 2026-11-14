
from sqlalchemy import create_engine

from super_agent.application.outbox import OutboxProcessor
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.outbox import OutboxRepository


def test_outbox_processor_claims_dispatches_and_publishes() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    seen: list[dict[str, object]] = []

    with session_scope(factory) as session:
        OutboxRepository(session).enqueue(
            topic="test.event", dedupe_key="processor-1", payload={"message": "hello"}
        )
        processor = OutboxProcessor(session, lambda _message, payload: seen.append(payload))
        message = processor.process_next()
        assert message is not None and message.status == "PUBLISHED"
        assert seen == [{"message": "hello"}]

