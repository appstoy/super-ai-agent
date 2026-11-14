from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine, select

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.background import DurableOutboxWorker
from super_agent.ingress.schemas import TriggerInput
from super_agent.ingress.service import TriggerIngressService
from super_agent.ports.model import ModelResponse
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import RunRecord, TaskRecord


def test_durable_worker_replays_a_persisted_outbox_event() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    trigger = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "recover this", "model": "mock"},
        occurred_at=datetime.now(UTC),
        dedupe_key="durable-worker-1",
    )
    with session_scope(factory) as session:
        TriggerIngressService(session).ingest(trigger)

    gateway = MockModelGateway(
        [ModelResponse(text="TASK: recover this"), ModelResponse(text="recovered")]
    )
    worker = DurableOutboxWorker(factory, lambda _model: gateway)

    assert worker.process_once()
    assert not worker.process_once()
    with session_scope(factory) as session:
        task = session.scalar(select(TaskRecord))
        run = session.scalar(select(RunRecord))
        assert task is not None and task.status == "succeeded"
        assert run is not None and run.status == "succeeded"
