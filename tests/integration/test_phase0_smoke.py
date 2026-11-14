from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.errors import ConfigurationError
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.outbox import OutboxRepository


def test_phase0_smoke_path() -> None:
    client = TestClient(create_app(Settings(app_name="smoke", version="test")))
    health = client.get("/health")
    missing = client.get("/does-not-exist")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "service": "smoke", "version": "test"}
    assert missing.status_code == 404

    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        repository = OutboxRepository(session)
        message = repository.enqueue(
            topic="phase0.smoke",
            dedupe_key="smoke-1",
            payload={"event_id": "smoke-1"},
        )
        assert repository.claim_next() is message
        repository.mark_published(message)

    with session_scope(factory) as session:
        claimed = OutboxRepository(session).claim_next()
        assert claimed is None


def test_phase0_smoke_rejects_invalid_configuration(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_AGENT_PORT", "not-a-port")

    try:
        Settings.from_env()
    except ConfigurationError as exc:
        assert exc.category.value == "configuration"
        assert not exc.retryable
    else:
        raise AssertionError("invalid configuration was accepted")

